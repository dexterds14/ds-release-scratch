"""merge-up.sh with the merge rules, splice and pins (ENG-2234, plan 5.2 to 5.4).

The MergeUpTests fixture of test_release_git.py (two bare repos, the stub gh),
with a few multi-line files seeded on the trunk of both repos before the lines
are opened, so the two lines can edit neighbouring lines of one file. The
rules themselves are tested in test_merge_rules.py; this file is about what
merge-up.sh does with their result in both steps.
"""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)

LOCK = release_git.LOCKFILE
MERGE_UP, DISTRO_REPO, META_REPO, LOCK_YML = t.MERGE_UP, t.DISTRO_REPO, t.META_REPO, t.LOCK_YML
git, write = t.git, t.write
CHILD, PARENT = 'vertex/1.x.x', 'vertex/main'
FIXED_DATE = '2026-01-01T00:00:00+00:00'
RULES = 'merge rules: '
PINS_FILE = 'conf/distro/include/ds-src-pins.inc'
P1, P2, P3 = '1' * 40, '2' * 40, '3' * 40


def text(*names):
    return ''.join(n + '\n' for n in names)


NOTES = text('a', 'b', 'c', 'd', 'e', 'f', 'g', 'h')
NOTES_CHILD = NOTES.replace('b\n', 'B child\n')        # the child edits line b
NOTES_PARENT = NOTES.replace('c\n', 'C parent\n')      # the parent edits its neighbour
NOTES_BOTH = NOTES_CHILD.replace('c\n', 'C parent\n')
WORKFLOW = text('name: build', 'on: push', 'jobs:', '  build:', '    runs-on: small', '    steps:', '      - run: make')
WORKFLOW_CHILD = WORKFLOW.replace('small', 'large')
WORKFLOW_PARENT = WORKFLOW.replace('    steps:', '    timeout-minutes: 5\n    steps:')
WORKFLOW_BOTH = WORKFLOW_CHILD.replace('    steps:', '    timeout-minutes: 5\n    steps:')
SUDOERS = text('# ds may run:', 'Cmnd_Alias A = /bin/a', 'Cmnd_Alias B = /bin/b', 'ds ALL = NOPASSWD: A, B')
RECIPE = text('SUMMARY = "app"', 'DEPENDS += "x"', 'EXTRA = "1"', 'inherit cmake', 'do_install() {', '}')


def pins(portal_branch, portal_rev, note='', ds_branch='main', ds_rev=P1, branch_line=True):
    out = ['# pins', f'DS_BRANCH ?= "{ds_branch}"', f'DSREV ?= "{ds_rev}"', '', '# sensor-portal']
    if branch_line:
        out.append(f'SENSOR_PORTAL_BRANCH ?= "{portal_branch}"')
    if note:
        out.append(f'# {note}')
    out += [f'SENSOR_PORTAL_REV ?= "{portal_rev}"', '', 'OTHER_REV ?= "' + 'f' * 40 + '"']
    return text(*out)


class RulesCase(unittest.TestCase):
    """The fixture and its helpers. No test of its own."""

    child_work = t.MergeUpTests.child_work

    def setUp(self):
        fixed = dict(GIT_AUTHOR_DATE=FIXED_DATE, GIT_COMMITTER_DATE=FIXED_DATE,
                     MERGE_UP_LOCK_WAIT_SECONDS='0', MERGE_UP_LOCK_POLL_SECONDS='0')
        saved = {k: os.environ.get(k) for k in fixed}
        os.environ.update(fixed)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        self.new_fixture()

    def new_fixture(self):
        """The MergeUpTests fixture, with the multi-line files on the trunk of both repos first."""
        self.fx = t.Fixture()
        self.addCleanup(self.fx.close)
        self.fx.distro_commit('ota-dev', {'docs/notes.txt': NOTES, 'udev/90-ds.rules': NOTES, '.github/workflows/build.yml': WORKFLOW}, 'files to conflict in')
        self.fx.meta_commit('ota-dev', RECIPE, 'a recipe', path='recipes/app.bb')
        self.fx.meta_commit('ota-dev', SUDOERS, 'a sudoers file', path='recipes/files/ds.sudoers')
        self.m_base = self.fx.meta_commit('ota-dev', pins('main', P1), 'the pins', path=PINS_FILE)
        self.fx.distro_commit('ota-dev', {LOCK: LOCK_YML.format(pin=self.m_base)}, 'pin the layer')
        self.fx.seed_vertex()
        self.main_tip = self.fx.heads(self.fx.distro_bare)[PARENT]
        self.fx.tag('vertex/v1.0.0', self.main_tip)
        self.fx.open_line('1.0.0')
        return self.fx

    # --- shapes ---

    def distro_edits(self, child, parent):
        """The child line's edits (with the layer pin bump that makes it green), then the parent's."""
        self.m3 = self.fx.meta_commit(CHILD, 'm3 fix\n', 'layer fix')
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, dict(child, **{LOCK: LOCK_YML.format(pin=self.m3)}), 'child work')
        write(self.fx.green, self.green_sha + '\n')
        self.parent_tip = self.fx.distro_commit(PARENT, parent, 'parent work')

    def meta_edits(self, path, child, parent):
        """Both layer lines edit one file; the child's distro line pins its layer tip and is green."""
        self.m3 = self.fx.meta_commit(CHILD, child, 'child layer work', path=path)
        self.m4 = self.fx.meta_commit(PARENT, parent, 'parent layer work', path=path)
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, {LOCK: LOCK_YML.format(pin=self.m3)}, 'pin m3')
        write(self.fx.green, self.green_sha + '\n')

    # --- queries ---

    def up(self, *args, auto='splice', env=None, **kw):
        extra = dict(env or {})
        if auto is not None:
            extra['DS_MERGE_UP_AUTOMATION'] = auto
        return self.fx.run(MERGE_UP, '--child', CHILD, *args, extra_env=extra, **kw)

    def branch(self):
        return f'merge-up/{CHILD}/{self.green_sha[:7]}'

    def meta_branch(self):
        return f'merge-up/{CHILD}/{self.m3[:7]}'

    def rule_lines(self, r):
        return [l.split(RULES, 1)[1] for l in r.stdout.splitlines() if RULES in l]

    def message(self, bare, sha):
        return git(bare, 'log', '-1', '--format=%B', sha)

    def show(self, bare, ref, path):
        return self.fx.show(bare, ref, path) + '\n'

    def approve(self, number):
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': 'ds-os-release-bot'}]}]}
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps(review))

    def compare(self, answers):
        """The stub's compare API: {'<repo> <base>...<head>': status}, in the owner of the layer repo."""
        write(os.path.join(self.fx.state, 'compare.json'), json.dumps({f'ds/{k}': v for k, v in answers.items()}))

    def compare_tokens(self):
        path = os.path.join(self.fx.state, 'compare-tokens')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().split('\n')[:-1]

    def snapshot(self):
        return {'distro': self.fx.heads(self.fx.distro_bare), 'meta': self.fx.heads(self.fx.meta_bare),
                'prs': [{k: v for k, v in p.items() if k != 'body'} for p in self.fx.prs()]}


class SpliceMergeUpTests(RulesCase):
    """The distro step: a conflict the splice resolves, in whole or in part."""

    def adjacent(self):
        self.distro_edits({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})

    def test_everything_resolved_is_a_pr_without_markers_never_a_push(self):
        self.resolved_pr()

    def resolved_pr(self):
        self.adjacent()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        # the parent line did not move: the result is a PR
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.parent_tip)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', head + '^1', head + '^2').split(), [self.parent_tip, self.child_tip])
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        self.assertEqual(self.message(self.fx.distro_bare, head),
                         f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=1 regions=1')
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        self.assertEqual((prs[0]['repo'], prs[0]['base'], prs[0]['head'], prs[0]['title']),
                         (DISTRO_REPO, PARENT, self.branch(), f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        body = prs[0]['body']
        self.assertIn('the merge-up rules resolved every one, so this branch has no conflict markers', body)
        self.assertIn('| `docs/notes.txt` | splice | 1 of 1 conflict resolved, no markers left', body)
        self.assertIn('This PR needs one approval.', body)
        self.assertIn('dispatch release-merge-up.yml on the default branch', body)
        self.assertNotIn('<<<<<<<', body)
        self.assertNotIn('comments', prs[0])
        self.assertEqual(self.rule_lines(r), [
            'resolved docs/notes.txt (1 of 1 conflict); no markers left',
            f'every conflict resolved by rule in {prs[0]["url"]} (1 file(s)); the PR has no markers and waits for one approval'])
        self.assertIn(f'pushed {self.branch()}: {head[:7]}, {PARENT} plus the merge of {CHILD}, all 1 conflicted file(s) resolved by rule, no markers', r.stdout)
        self.assertIn(f'::error::merge-up: ds-tegra-distro {CHILD} -> {PARENT}: every conflict was resolved by rule in {prs[0]["url"]}', r.stdout)
        self.assertIn(f'with child={CHILD} pr=1', r.stdout)
        return head

    def test_a_rerun_reuses_the_branch_and_the_pr(self):
        head = self.resolved_pr()
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f'{self.branch()} is already on origin at {head[:7]}: the commit this run computes, untouched; left as it is, reusing its PR', r.stdout)
        self.assertNotIn('was not produced by this run', r.stdout)
        self.assertNotIn('holds commits this run did not make', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], head)
        self.assertIn('every conflict was resolved by rule in https://github.com/ds/distro/pull/1', r.stdout)

    def test_one_approval_finishes_it(self):
        head = self.resolved_pr()
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)[PARENT]
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), head)
        self.assertEqual(self.show(self.fx.distro_bare, tip, 'docs/notes.txt'), NOTES_BOTH)

    def test_a_dry_run_says_what_it_would_do(self):
        self.adjacent()
        before = self.snapshot()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.rule_lines(r), ['would have resolved docs/notes.txt (1 of 1 conflict); no markers left'])
        self.assertIn(f'dry run: would push {self.branch()} ({PARENT} plus the merge of {CHILD}, all 1 conflicted file(s) resolved by rule, '
                      f'no markers) and open the PR "Merge-up {CHILD} into {PARENT} (resolved by rule)"', r.stdout)

    def test_shadow_logs_and_changes_nothing(self):
        self.adjacent()
        r = self.up(auto='shadow')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), ['shadow, nothing changes: would resolve docs/notes.txt (1 of 1 conflict)'])
        shadow = self.snapshot()
        body = self.fx.prs()[0]['body']
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertIn('<<<<<<< ', self.show(self.fx.distro_bare, head, 'docs/notes.txt'))
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertNotIn('Merge-Up-Rule', self.message(self.fx.distro_bare, head))
        # the same merge with the variable absent: the same refs, PRs and PR body, and no rule line
        self.new_fixture()
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), [])
        self.assertEqual(self.snapshot(), shadow)
        self.assertEqual(self.fx.prs()[0]['body'], body)

    def test_the_other_rule_names_leave_the_splice_in_shadow(self):
        self.adjacent()
        r = self.up(auto='lock-pin,pins')
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.rule_lines(r), ['shadow, nothing changes: would resolve docs/notes.txt (1 of 1 conflict)'])
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')

    def test_a_partly_resolved_merge_is_a_conflict_pr_with_the_rule_table(self):
        self.distro_edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        self.assertIn('<<<<<<< ', self.show(self.fx.distro_bare, head, 'foo.txt'))
        self.assertEqual(self.message(self.fx.distro_bare, head),
                         f'Merge {CHILD} into {PARENT} (conflicts to resolve)\n\nConflict markers left in: foo.txt\n\n'
                         'Merge-Up-Rule: splice files=1 regions=1')
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        # the list of files to decide holds only what still has markers
        self.assertIn('conflicts in:\n\n- `foo.txt`\n\n', pr['body'])
        self.assertIn('**Resolved by rule in this commit.**', pr['body'])
        self.assertIn('| `docs/notes.txt` | splice | 1 of 1 conflict resolved, no markers left', pr['body'])
        self.assertIn('Why the rules left the rest to a person:\n\n- `foo.txt`: conflict 1: both sides changed the same base lines', pr['body'])
        self.assertIn(f'::error::merge-up: ds-tegra-distro {CHILD} -> {PARENT} conflicts (foo.txt); resolve {pr["url"]}', r.stdout)
        self.assertEqual(self.rule_lines(r), ['resolved docs/notes.txt (1 of 1 conflict); no markers left',
                                              'no rule resolves foo.txt (conflict 1: both sides changed the same base lines)'])
        # a re-run finds its own commit on the branch: no label, nothing new
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('was not produced by this run', r.stdout)

    # --- commits pushed on top of the bot's commit are a stop signal (owner decision of 2026-11-17) ---

    def close_prs(self):
        """Every PR closed as the bot's own supersession: the one close after which a run still
        opens a PR on the branch (a PR a person closed makes the branch dead, test_dead_branch.py)."""
        for pr in self.fx.prs():
            self.fx.supersede_pr(pr['number'])

    def assert_stopped(self, r, pr, files='- `docs/notes.txt`\n'):
        """An ordinary conflict PR: no claim that a rule resolved anything, and the label."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((pr['head'], pr['state'], pr['title']), (self.branch(), 'OPEN', f'Merge-up {CHILD} into {PARENT} (conflict)'))
        self.assertIn(f'conflicts in:\n\n{files}\n', pr['body'])
        self.assertIn('**This branch holds commits this run did not make.**', pr['body'])
        self.assertIn('it needs a resolver and an approver other than the last pusher', pr['body'])
        for claim in ('resolved by rule', 'Resolved by rule', 'the merge-up rules resolved every one', 'This PR needs one approval', '| splice |'):
            self.assertNotIn(claim, pr['body'].replace('Nothing on it counts as resolved by rule', ''), claim)
        self.assertIn(f'{self.branch()} holds commits this run did not make', r.stdout)
        self.assertNotIn('every conflict was resolved by rule in', r.stdout)
        self.assertNotIn('every conflict resolved by rule in', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.parent_tip)

    def test_a_commit_pushed_on_top_of_the_rule_commit_is_a_stop_signal(self):
        # The review's reproduction: the branch starts with the commit this run computes and
        # holds one more commit. It used to get the bot's "(resolved by rule)" PR and body.
        bot = self.resolved_pr()
        extra = self.fx.distro_commit(self.branch(), {'scripts-release/lib.sh': 'echo changed\n'}, 'one more change, not a rule')
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], extra)
        self.assert_stopped(r, self.fx.prs()[1])
        self.assertIn(f'starts with the commit this run computes, and 1 more commit(s) were pushed on top of it', r.stdout)
        self.assertIn(f'`{self.branch()}` already existed on origin at `{extra[:7]}`', self.fx.prs()[1]['body'])
        self.assertIn('the rules resolve it, but ' + self.branch() + ' already exists on origin with other content and is left as it is', r.stdout)
        self.assertNotEqual(bot, extra)
        # and it is finished like any conflict PR: not without a person's approval
        r = self.up('--pr', '2')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.parent_tip)

    def test_an_open_rule_pr_whose_branch_got_a_commit_loses_its_title(self):
        self.resolved_pr()
        self.fx.distro_commit(self.branch(), {'docs/notes.txt': NOTES_BOTH + 'i\n'}, 'a fix by hand')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['title']) for p in prs], [(1, 'OPEN', f'Merge-up {CHILD} into {PARENT} (conflict)')])
        self.assertEqual(len(prs[0]['comments']), 1)
        self.assertIn('**This branch holds commits this run did not make.**', prs[0]['comments'][0])
        self.assertIn('This PR was opened as "(resolved by rule)". It is retitled', prs[0]['comments'][0])
        self.assertIn('retitled as a conflict PR, with a comment that says why', r.stdout)
        self.assertNotIn('every conflict was resolved by rule in', r.stdout)
        # the next run finds the conflict title and says nothing twice
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('retitled', r.stdout)

    def test_an_open_rule_pr_loses_its_title_when_the_next_run_resolves_only_part(self):
        # The review's reproduction: the retitle used to happen only where the rules again
        # resolved every file (rules_pr). Here the parent moves into a second conflict the
        # splice refuses, so the run goes through the conflict path and reuses the PR.
        more = 'udev/90-ds.rules'
        self.distro_edits({'docs/notes.txt': NOTES_CHILD, more: NOTES.replace('e\n', 'E child\n')}, {'docs/notes.txt': NOTES_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state'], p['title']) for p in self.fx.prs()], [(1, 'OPEN', f'Merge-up {CHILD} into {PARENT} (resolved by rule)')])
        extra = self.fx.distro_commit(self.branch(), {'scripts-release/lib.sh': 'echo by a person\n'}, 'pushed by a person')
        self.parent_tip = self.fx.distro_commit(PARENT, {more: NOTES.replace('e\n', 'E parent\n')}, 'the parent overlaps the child')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], extra)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.parent_tip)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['title']) for p in prs], [(1, 'OPEN', f'Merge-up {CHILD} into {PARENT} (conflict)')])
        self.assertEqual(len(prs[0]['comments']), 1)
        self.assertIn('**This branch was not produced by this run.**', prs[0]['comments'][0])
        self.assertIn('This PR was opened as "(resolved by rule)". It is retitled', prs[0]['comments'][0])
        self.assertIn(f'{self.branch()} was not produced by this run', r.stdout)
        self.assertIn('retitled as a conflict PR, with a comment that says why', r.stdout)
        # the next run finds the conflict title and says nothing twice
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('retitled', r.stdout)

    def test_a_conflict_pr_this_run_opens_or_reuses_is_not_asked_for_its_title(self):
        # the conflict path looks at a title only on a reused PR whose branch carries the label
        self.distro_edits({'foo.txt': 'child fix\n'}, {'foo.txt': 'main fix\n'})
        for _ in range(2):
            log = self.fx.gh_log()
            r = self.up()
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn('pr view', self.fx.gh_log()[len(log):])
            self.assertNotIn('pr edit', self.fx.gh_log()[len(log):])
        self.assertEqual([(p['number'], p['title'], len(p.get('comments', []))) for p in self.fx.prs()],
                         [(1, f'Merge-up {CHILD} into {PARENT} (conflict)', 0)])

    def test_a_title_that_cannot_be_changed_is_a_warning(self):
        self.resolved_pr()
        self.fx.distro_commit(self.branch(), {'docs/notes.txt': NOTES_BOTH + 'i\n'}, 'a fix by hand')
        write(os.path.join(self.fx.state, 'fail-pr-edit'), '')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::warning::merge-up: https://github.com/ds/distro/pull/1 still says "(resolved by rule)" and could not be retitled', r.stdout)
        self.assertNotIn('every conflict was resolved by rule in', r.stdout)

    def test_the_head_must_be_the_bots_commit_with_its_exact_message(self):
        # the tree, the parents and the bot's committer address, but a message of its own:
        # a commit someone made by hand in place of the bot's
        bot = self.resolved_pr()
        email = git(self.fx.distro_bare, 'log', '-1', '--format=%ce', bot)
        git(self.fx.distro, 'fetch', '-q', 'origin')
        tree = git(self.fx.distro, 'rev-parse', bot + '^{tree}')
        forged = git(self.fx.distro, 'commit-tree', tree, '-p', self.parent_tip, '-p', self.child_tip, '-m',
                     f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=9 regions=9', env={'GIT_COMMITTER_EMAIL': email})
        git(self.fx.distro, 'push', '-q', '--force', 'origin', f'{forged}:refs/heads/{self.branch()}')
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], forged)
        self.assert_stopped(r, self.fx.prs()[1])
        self.assertIn("has the tree this run computes, but not the bot's committer address and message", r.stdout)

    def test_a_partly_resolved_branch_with_a_commit_on_top_makes_no_rule_claim(self):
        self.distro_edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('**Resolved by rule in this commit.**', self.fx.prs()[0]['body'])
        extra = self.fx.distro_commit(self.branch(), {'docs/notes.txt': NOTES + 'changed by hand\n'}, 'a resolution in progress')
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], extra)
        # every file the merge conflicts in is to decide again, also the one the rule resolved
        self.assert_stopped(r, self.fx.prs()[1], files='- `docs/notes.txt`\n- `foo.txt`\n')
        self.assertIn(f'::error::merge-up: ds-tegra-distro {CHILD} -> {PARENT} conflicts (docs/notes.txt foo.txt); resolve', r.stdout)

    def test_without_a_rule_result_a_resolvers_commits_are_not_labelled(self):
        # the plain conflict PR of a run without the rules: a resolver's commits on the
        # branch are the normal case there and get no label
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        self.fx.distro_commit(self.branch(), {'docs/notes.txt': NOTES_BOTH}, 'resolved by hand')
        self.close_prs()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('holds commits this run did not make', r.stdout + self.fx.prs()[1]['body'])
        self.assertNotIn('was not produced by this run', r.stdout + self.fx.prs()[1]['body'])

    def test_no_run_merges_a_rule_resolved_pr_without_an_approval(self):
        # green required checks and the lockfile rule acting change nothing: only lock outcome B
        # is finished by the bot, and a rule-resolved PR is never that
        head = self.resolved_pr()
        green = {'reviewDecision': None, 'mergeStateStatus': 'CLEAN', 'reviews': [],
                 'statusCheckRollup': [{'__typename': 'CheckRun', 'name': n, 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
                                       for n in ('pr-checks', 'pin-propagation-safety')]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(green))
        before = self.snapshot()
        for auto in ('splice', 'lock-pin,splice,pins'):
            r = self.up(auto=auto)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertEqual(self.snapshot(), before)
            self.assertNotIn('pr merge', self.fx.gh_log())
        r = self.up('--pr', '1', auto='lock-pin,splice,pins')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual((self.fx.heads(self.fx.distro_bare)[PARENT], self.fx.heads(self.fx.distro_bare)[self.branch()]), (self.parent_tip, head))

    def test_a_branch_that_holds_something_else_is_left_alone(self):
        # an earlier run without the rule pushed the conflict commit with markers
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], head)
        self.assertIn(f'{self.branch()} was not produced by this run', r.stdout)
        self.assertIn('the rules resolve it, but ' + self.branch() + ' already exists on origin with other content and is left as it is', r.stdout)
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')

    def test_a_branch_that_holds_something_else_lists_every_conflicted_file(self):
        # An earlier run without the rule pushed the conflict commit with markers in both files,
        # and the bot closed its PR as superseded. This run's rule resolves docs/notes.txt, but not on that branch:
        # the PR it opens there must name both files, not only the one the rule left.
        self.distro_edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertIn('<<<<<<< ', self.show(self.fx.distro_bare, head, 'docs/notes.txt'))
        self.fx.supersede_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], head)
        self.assertIn(f'{self.branch()} was not produced by this run', r.stdout)
        pr = self.fx.prs()[1]
        self.assertEqual((pr['head'], pr['state']), (self.branch(), 'OPEN'))
        self.assertIn('conflicts in:\n\n- `docs/notes.txt`\n- `foo.txt`\n\n', pr['body'])
        self.assertIn('**This branch was not produced by this run.**', pr['body'])
        self.assertNotIn('Resolved by rule in this commit', pr['body'])
        self.assertIn(f'::error::merge-up: ds-tegra-distro {CHILD} -> {PARENT} conflicts (docs/notes.txt foo.txt); resolve {pr["url"]}', r.stdout)

    def test_a_conflict_without_text_markers_skips_the_rules(self):
        self.fx.distro_commit('ota-dev', {'x': 'unused\n'}, 'unused')   # the trunk is not a line: no effect
        self.distro_edits({'docs/notes.txt': NOTES_CHILD, 'blob.bin': 'c\0child\n'}, {'docs/notes.txt': NOTES_PARENT, 'blob.bin': 'p\0parent\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), [f'not applied: a conflict here leaves no text markers, so the PR goes on {CHILD} itself '
                                              'and a rule\'s result would not be on it'])
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], self.child_tip)
        self.assertNotIn('Resolved by rule', self.fx.prs()[0]['body'])

    def test_an_evaluation_that_fails_is_logged_and_the_run_goes_on(self):
        self.adjacent()
        scripts = self.fx.scripts_copy()
        with open(os.path.join(scripts, 'merge_rules.py'), 'a') as f:
            f.write('\n\ndef unmerged_paths(git_dir):\n    raise Error("the rules broke")\n')
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'splice'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), ['not evaluated (the rules broke)'])
        # the phase-0 conflict PR, markers and all
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertIn('<<<<<<< ', self.show(self.fx.distro_bare, head, 'docs/notes.txt'))
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')

    def test_a_failure_after_the_rules_wrote_leaves_the_merge_as_git_made_it(self):
        # The rules resolve and stage the file, then their outputs cannot be written (a full
        # disk). The run says "not evaluated" and goes on without the rules, so the file must
        # be in conflict again: a resolved file would have no markers, pass for a binary or
        # modify/delete conflict, and send the PR to the child itself.
        self.adjacent()
        scripts = self.fx.scripts_copy()
        with open(os.path.join(scripts, 'merge_rules.py'), 'a') as f:
            f.write('\n\ndef write_outputs(report, out_dir):\n    raise OSError(28, "No space left on device")\n')
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'splice'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('no text markers', r.stdout)
        self.assertEqual(self.rule_lines(r), ["not evaluated (the rules' outputs could not be written ([Errno 28] No space left on device))"])
        # the phase-0 conflict PR: the merge with its markers, exactly what a run without the rules pushes
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertNotEqual(head, self.child_tip)
        marked = self.show(self.fx.distro_bare, head, 'docs/notes.txt')
        self.assertIn(f'<<<<<<< HEAD\n', marked)
        self.assertIn(f'>>>>>>> {self.child_tip}\n', marked)
        self.assertNotIn('Merge-Up-Rule', self.message(self.fx.distro_bare, head))
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('The conflict markers are committed', pr['body'])
        twin = self.new_fixture()
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(self.fx.heads(twin.distro_bare)[self.branch()], head)   # fixed dates: the same commit

    def test_with_the_variable_absent_the_rules_are_never_run(self):
        """Phase-0 behaviour: no rule line, and merge_rules.py is not even loaded (here it would
        fail), so a broken merge_rules.py cannot break a release script that does not need it."""
        self.distro_edits({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})
        scripts = self.fx.scripts_copy()
        with open(os.path.join(scripts, 'merge_rules.py'), 'w') as f:
            f.write('raise SystemExit("merge_rules.py must not be loaded for a run without the variable")\n')
        for auto in (None, 'off'):
            extra = {} if auto is None else {'DS_MERGE_UP_AUTOMATION': auto}
            r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env=extra, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(self.rule_lines(r), [])
            self.assertIn('conflicts in: docs/notes.txt', r.stdout)
            self.assertIn('would push', r.stdout)

    def test_the_summary_gets_the_rule_lines(self):
        self.adjacent()
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.up(env={'MERGE_UP_SUMMARY': summary})
        self.assertEqual(r.returncode, 1)
        with open(summary) as f:
            got = f.read()
        self.assertIn('- ds-tegra-distro merge rules: resolved docs/notes.txt (1 of 1 conflict); no markers left', got)

    def test_the_lockfile_and_base_yml_are_left_to_their_own_rules(self):
        # both lines moved the layer pin (R-lock is in shadow here) and edited neighbouring lines of notes.txt
        self.m3 = self.fx.meta_commit(CHILD, 'm3 fix\n', 'layer fix')
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, {LOCK: LOCK_YML.format(pin=self.m3), 'docs/notes.txt': NOTES_CHILD}, 'child')
        write(self.fx.green, self.green_sha + '\n')
        m4 = self.fx.meta_commit(PARENT, 'main only\n', 'main pin target', path='other.txt')
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=m4), 'docs/notes.txt': NOTES_PARENT}, 'parent')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertIn('<<<<<<< ', self.show(self.fx.distro_bare, head, LOCK))
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn(f'conflicts in:\n\n- `{LOCK}`\n\n', pr['body'])
        self.assertNotIn(LOCK, ' '.join(l for l in self.rule_lines(r) if 'lockfile pin rule' not in l and 'resolved' in l))

    def test_a_base_yml_conflict_the_index_does_not_show_stays_in_the_list(self):
        # merge-keep-target merges base.yml itself. If that merge conflicts in a file git had
        # staged clean, base.yml is in the conflict list but not unmerged in the index, which is
        # all the rules read. Resolving every other file must not turn the merge into a
        # "(resolved by rule)" PR that commits git's own base.yml.
        self.adjacent()
        scripts = self.fx.scripts_copy()
        path = os.path.join(scripts, 'release_git.py')
        with open(path) as f:
            source = f.read()
        fake = ("def base_merge(git_dir, branch):\n"
                "    with open(os.path.join(git_dir, BASE_YML), 'a') as f:\n"
                "        f.write('<<<<<<< HEAD\\nours\\n||||||| merge-base\\n=======\\ntheirs\\n>>>>>>> MERGE_HEAD\\n')\n"
                "    return False\n\n\n")
        marker = "VERSIONS_README = 'versions/README.md'\n"
        self.assertEqual(source.count(marker), 1)
        with open(path, 'w') as f:
            f.write(source.replace(marker, marker + '\n\n' + fake))
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'splice'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('conflicts in: docs/notes.txt kas/include/base.yml', r.stdout)
        self.assertEqual(self.rule_lines(r), ['resolved docs/notes.txt (1 of 1 conflict); no markers left'])
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('conflicts in:\n\n- `kas/include/base.yml`\n\n', pr['body'])
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        self.assertIn('<<<<<<< HEAD\n', self.show(self.fx.distro_bare, head, 'kas/include/base.yml'))
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        self.assertIn('Conflict markers left in: kas/include/base.yml\n', self.message(self.fx.distro_bare, head))

    def test_with_the_lock_rule_the_pr_carries_both_trailers(self):
        self.m3 = self.fx.meta_commit(CHILD, 'm3 fix\n', 'layer fix')
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, {LOCK: LOCK_YML.format(pin=self.m3), 'docs/notes.txt': NOTES_CHILD}, 'child')
        write(self.fx.green, self.green_sha + '\n')
        m4 = self.fx.meta_commit(PARENT, 'main only\n', 'main pin target', path='other.txt')
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=m4), 'docs/notes.txt': NOTES_PARENT}, 'parent')
        r = self.up(auto='lock-pin,splice')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.parent_tip)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        tip = self.fx.heads(self.fx.meta_bare)[PARENT]
        self.assertEqual(release_git.lock_pin(self.show(self.fx.distro_bare, head, LOCK)), tip)
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        trailers = self.message(self.fx.distro_bare, head).split('\n\n')[-1].splitlines()
        self.assertEqual(len(trailers), 2)
        self.assertTrue(trailers[0].startswith(f'Merge-Up-Rule: lock-pin tip={tip} '), trailers)
        self.assertEqual(trailers[1], 'Merge-Up-Rule: splice files=1 regions=1')
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (resolved by rule)')
        self.assertIn('already resolved in this commit by the lockfile pin rule', pr['body'])
        self.assertIn('| `docs/notes.txt` | splice |', pr['body'])


class NeverAutoMergeUpTests(RulesCase):
    """No rule writes a never_auto file; the splice's answer is a PR comment with a patch."""

    def test_a_workflow_file_keeps_its_markers_and_gets_the_candidate_comment(self):
        path = '.github/workflows/build.yml'
        self.distro_edits({path: WORKFLOW_CHILD, 'docs/notes.txt': NOTES_CHILD}, {path: WORKFLOW_PARENT, 'docs/notes.txt': NOTES_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        marked = self.show(self.fx.distro_bare, head, path)
        self.assertIn('<<<<<<< ', marked)
        self.assertEqual(self.show(self.fx.distro_bare, head, 'docs/notes.txt'), NOTES_BOTH)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn(f'conflicts in:\n\n- `{path}`\n\n', pr['body'])
        self.assertIn('**On the `never_auto` list.** No rule writes these files. Each needs a resolver and a different approver:', pr['body'])
        self.assertIn(f'- `{path}` (matches `.github/workflows/**`). What the rules would have written is in a comment on this PR', pr['body'])
        # one comment, whose patch turns the branch's file into the splice's answer
        self.assertEqual(len(pr['comments']), 1)
        comment = pr['comments'][0]
        self.assertIn('Treat it as a suggestion: read it', comment)
        patch = comment.split('```diff\n', 1)[1].split('\n```', 1)[0] + '\n'
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '--detach', head)
        subprocess.run(['git', '-C', self.fx.distro, 'apply', '-'], input=patch, text=True, check=True)
        with open(os.path.join(self.fx.distro, path)) as f:
            self.assertEqual(f.read(), WORKFLOW_BOTH)
        git(self.fx.distro, 'checkout', '-q', '--', path)
        self.assertIn(f'never_auto: {path} matches .github/workflows/**, so no rule writes it; the rules have an answer for 1 of 1 conflict, '
                      'kept as a candidate patch', self.rule_lines(r))
        self.assertIn(f'commented on {pr["url"]} with the rules\' candidate for the never_auto file(s)', r.stdout)
        # the commit's trailer counts only the file a rule wrote
        self.assertIn('Merge-Up-Rule: splice files=1 regions=1', self.message(self.fx.distro_bare, head))
        # a re-run reuses the PR and does not comment again
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.fx.prs()[0]['comments']), 1)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], head)

    def test_a_comment_that_fails_is_a_warning(self):
        path = '.github/workflows/build.yml'
        self.distro_edits({path: WORKFLOW_CHILD}, {path: WORKFLOW_PARENT})
        # the stub has no PR 1 in the layer repo, so a comment sent there fails; here: break gh pr comment
        gh = os.path.join(self.fx.binp, 'gh')
        with open(gh) as f:
            stub = f.read()
        with open(gh, 'w') as f:
            f.write(stub.replace("if pos[:2] == ['pr', 'comment']:", "if pos[:2] == ['pr', 'comment']:\n        print('HTTP 403', file=sys.stderr)\n        return 1\n    if False:"))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::warning::merge-up: could not comment on https://github.com/ds/distro/pull/1 with the rules\' candidate (HTTP 403)', r.stdout)
        self.assertEqual(len(self.fx.prs()), 1)

    def test_in_shadow_no_comment_is_posted(self):
        path = '.github/workflows/build.yml'
        self.distro_edits({path: WORKFLOW_CHILD}, {path: WORKFLOW_PARENT})
        r = self.up(auto='shadow')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('comments', self.fx.prs()[0])
        self.assertEqual(self.rule_lines(r), [f'never_auto: {path} matches .github/workflows/**, so no rule writes it; '
                                              'shadow: the rules would have an answer for 1 of 1 conflict'])

    def test_a_meta_dsensos_sudoers_conflict_keeps_its_markers(self):
        path = 'recipes/files/ds.sudoers'
        child = SUDOERS.replace('/bin/a', '/bin/a2')
        parent = SUDOERS.replace('/bin/b', '/bin/b2')
        self.meta_edits(path, child, parent)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads(self.fx.meta_bare)[self.meta_branch()]
        self.assertIn('<<<<<<< ', self.show(self.fx.meta_bare, head, path))
        self.assertEqual(self.fx.heads(self.fx.meta_bare)[PARENT], self.m4)
        pr = self.fx.prs()[0]
        self.assertEqual((pr['repo'], pr['title']), (META_REPO, f'Merge-up {CHILD} into {PARENT} (conflict)'))
        self.assertIn(f'- `{path}` (matches `*sudoers*`)', pr['body'])
        self.assertIn(f'never_auto: {path} matches *sudoers*, so no rule writes it; the rules have an answer for 1 of 1 conflict, '
                      'kept as a candidate patch', self.rule_lines(r))
        self.assertEqual(len(pr['comments']), 1)
        self.assertNotIn('Merge-Up-Rule', self.message(self.fx.meta_bare, head))
        # the layer step stopped the run: ds-tegra-distro is untouched
        self.assertNotIn(self.branch(), self.fx.heads(self.fx.distro_bare))
        self.assertTrue(all(l.startswith('meta-dsensos: ') for l in r.stdout.splitlines() if RULES in l))


class MetaSpliceMergeUpTests(RulesCase):
    """The layer step: a recipe conflict the splice resolves."""

    def test_a_recipe_conflict_is_a_rule_resolved_pr_in_the_layer_repo(self):
        path = 'recipes/app.bb'
        child, parent = RECIPE.replace('DEPENDS += "x"', 'DEPENDS += "x y"'), RECIPE.replace('EXTRA = "1"\n', '')
        self.meta_edits(path, child, parent)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)[PARENT], self.m4)
        head = self.fx.heads(self.fx.meta_bare)[self.meta_branch()]
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', head + '^1', head + '^2').split(), [self.m4, self.m3])
        self.assertEqual(self.show(self.fx.meta_bare, head, path), child.replace('EXTRA = "1"\n', ''))
        self.assertEqual(self.message(self.fx.meta_bare, head), f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=1 regions=1')
        pr = self.fx.prs()[0]
        self.assertEqual((pr['repo'], pr['base'], pr['head'], pr['title']),
                         (META_REPO, PARENT, self.meta_branch(), f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        self.assertIn(f'::error::merge-up: meta-dsensos {CHILD} -> {PARENT}: every conflict was resolved by rule in {pr["url"]}', r.stdout)
        # ds-tegra-distro waits for the layer
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[PARENT], self.main_tip)
        self.assertNotIn(self.branch(), self.fx.heads(self.fx.distro_bare))
        # a re-run reuses it; one approval finishes it and the distro merge follows
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.fx.prs()), 1)
        self.assertIn('the commit this run computes, untouched; left as it is, reusing its PR', r.stdout)
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', f'{PARENT}^2'), head)
        tip = self.fx.heads(self.fx.distro_bare)[PARENT]
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), self.child_tip)


def with_release(text, rev, release):
    """A pins() file with SENSOR_PORTAL_RELEASE a few comment lines below the portal pin, as in the layer."""
    line = f'SENSOR_PORTAL_REV ?= "{rev}"\n'
    assert text.count(line) == 1
    return text.replace(line, line + '# the release at SENSOR_PORTAL_REV\n# X.Y.Z or empty\n# the build checks it\n'
                        f'SENSOR_PORTAL_RELEASE ?= "{release}"\n')


class PinsMergeUpTests(RulesCase):
    """R-pins in the layer step, through merge-up.sh and the stub compare API."""

    TOKEN = {'MERGE_UP_READ_TOKEN': 'the-read-token'}

    def pins_up(self, child, parent, answers=None, auto='pins', env=TOKEN, **kw):
        self.meta_edits(PINS_FILE, child, parent)
        if answers is not None:
            self.compare(answers)
        r = self.up(auto=auto, env=env, **kw)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)[PARENT], self.m4)   # never a direct push
        self.head = self.fx.heads(self.fx.meta_bare)[self.meta_branch()]
        self.pr = self.fx.prs()[0]
        return r

    def resolved(self, r, want, code):
        self.assertEqual(self.show(self.fx.meta_bare, self.head, PINS_FILE), want)
        self.assertEqual(self.pr['title'], f'Merge-up {CHILD} into {PARENT} (resolved by rule)')
        self.assertIn(f'Merge-Up-Rule: pins SENSOR_PORTAL_REV={code} parent=', self.message(self.fx.meta_bare, self.head))
        self.assertIn('| Variable | Branch (parent / child) | Pin (parent / child) | Compare | What the rule did |', self.pr['body'])

    def person(self, r, text):
        self.assertIn('<<<<<<< ', self.show(self.fx.meta_bare, self.head, PINS_FILE))
        self.assertEqual(self.pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertNotIn('Merge-Up-Rule', self.message(self.fx.meta_bare, self.head))
        self.assertIn(text, ' '.join(self.rule_lines(r)))
        self.assertIn(text, self.pr['body'])

    def test_different_branches_keep_the_parent(self):
        child, parent = pins('main', P2, 'child bump'), pins('2.x', P3, 'the parent moved to 2.x')
        r = self.pins_up(child, parent)
        self.resolved(r, parent, 'keep-parent')
        self.assertIn(f'| `SENSOR_PORTAL_REV` | `2.x` / `main` | `{P3[:8]}` / `{P2[:8]}` | not needed | kept the parent\'s branch and pin', self.pr['body'])
        self.assertEqual(self.compare_tokens(), [])   # the branches settle it, the API is not asked
        self.assertIn(f'pins rule: SENSOR_PORTAL_REV (parent 2.x at {P3[:8]}, child main at {P2[:8]}): kept the parent\'s branch and pin', self.rule_lines(r)[0])

    def test_same_branch_and_the_child_ahead_flows_up(self):
        child, parent = pins('main', P3, 'child bump'), pins('main', P2, 'parent bump')
        r = self.pins_up(child, parent, {f'sensor-portal {P2}...{P3}': 'ahead', f'sensor-portal {P3}...main': 'ahead'})
        self.resolved(r, child, 'take-child')
        self.assertIn(f'| `SENSOR_PORTAL_REV` | `main` / `main` | `{P2[:8]}` / `{P3[:8]}` | ahead | took the child\'s pin', self.pr['body'])
        # both questions went out with the read-only token, never the write token
        self.assertEqual(self.compare_tokens(), ['the-read-token', 'the-read-token'])
        self.assertIn(f'api repos/ds/sensor-portal/compare/{P2}...{P3} --jq .status', self.fx.gh_log())

    def test_the_release_value_goes_with_the_parent_unit(self):
        # pin-propagation.json names SENSOR_PORTAL_RELEASE: the parent's pin and release value together
        child = with_release(pins('main', P2, 'child bump'), P2, '3.2.0')
        parent = with_release(pins('2.x', P3, 'the parent moved to 2.x'), P3, '')
        r = self.pins_up(child, parent)
        self.resolved(r, parent, 'keep-parent')
        self.assertIn('kept the parent\'s branch, pin and SENSOR_PORTAL_RELEASE ""', self.pr['body'])
        self.assertIn(f'Merge-Up-Rule: pins SENSOR_PORTAL_REV=keep-parent parent={P3} child={P2} SENSOR_PORTAL_RELEASE=\n',
                      self.message(self.fx.meta_bare, self.head) + '\n')

    def test_a_parent_without_the_release_value_goes_to_a_person(self):
        # the child adds SENSOR_PORTAL_RELEASE with its bump; the parent, which the rule would
        # keep, does not set it, so its pin would get the child's release value
        child = with_release(pins('main', P2, 'child bump'), P2, '3.2.0')
        r = self.pins_up(child, pins('2.x', P3, 'the parent moved to 2.x'))
        self.person(r, 'left to a person: the parent line does not set SENSOR_PORTAL_RELEASE exactly once (does not set it)')

    def test_a_newer_parent_pin_is_not_rolled_back(self):
        child, parent = pins('main', P2, 'child bump'), pins('main', P3, 'parent is ahead')
        r = self.pins_up(child, parent, {f'sensor-portal {P3}...{P2}': 'behind'})
        self.resolved(r, parent, 'keep-parent-newer')
        self.assertIn('a pin never rolls back', self.pr['body'])

    def test_diverged_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c'), pins('main', P2, 'p'), {f'sensor-portal {P2}...{P3}': 'diverged'})
        self.person(r, 'left to a person: the two pins have diverged')

    def test_a_404_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c'), pins('main', P2, 'p'), {})
        self.person(r, 'left to a person: sensor-portal does not have the two pins (HTTP 404)')

    def test_an_api_error_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c'), pins('main', P2, 'p'), {f'sensor-portal {P2}...{P3}': '502'})
        self.person(r, 'left to a person: the compare API failed (gh: Bad Gateway (HTTP 502))')

    def test_without_the_read_token_it_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c'), pins('main', P2, 'p'), {f'sensor-portal {P2}...{P3}': 'ahead'}, env={})
        self.person(r, 'left to a person: no read-only token for the app repos, so the pins could not be compared')
        self.assertEqual(self.compare_tokens(), [])

    def test_a_child_pin_off_the_branch_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c'), pins('main', P2, 'p'),
                         {f'sensor-portal {P2}...{P3}': 'ahead', f'sensor-portal {P3}...main': 'diverged'})
        self.person(r, 'left to a person: the child\'s pin is not on the branch main of sensor-portal')

    def test_a_missing_branch_variable_goes_to_a_person(self):
        r = self.pins_up(pins('main', P3, 'c', branch_line=False), pins('main', P2, 'p'))
        self.person(r, 'left to a person: the child line does not set SENSOR_PORTAL_BRANCH exactly once')

    def test_the_ds_pair_only_logs(self):
        child = pins('main', P1, ds_branch='1.x.x', ds_rev=P3)
        parent = pins('main', P1, ds_branch='main', ds_rev=P2)
        r = self.pins_up(child, parent)
        self.assertIn('<<<<<<< ', self.show(self.fx.meta_bare, self.head, PINS_FILE))
        self.assertEqual(self.pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn(f'pins rule: shadow, nothing changes: DSREV (parent main at {P2[:8]}, child 1.x.x at {P3[:8]}): '
                      'kept the parent\'s branch and pin', self.rule_lines(r)[0])
        self.assertIn('the pins rule only logs for DSREV', self.pr['body'])
        self.assertNotIn('`DSREV` |', self.pr['body'])

    def test_shadow_logs_and_changes_nothing(self):
        child, parent = pins('main', P2, 'child bump'), pins('2.x', P3, 'the parent moved to 2.x')
        r = self.pins_up(child, parent, auto='shadow')
        self.assertIn('<<<<<<< ', self.show(self.fx.meta_bare, self.head, PINS_FILE))
        self.assertEqual(self.pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('pins rule: shadow, nothing changes: SENSOR_PORTAL_REV', self.rule_lines(r)[0])
        self.assertNotIn('pins rule', self.pr['body'])
        self.assertNotIn('never_auto', self.pr['body'])

    def test_with_only_the_splice_on_a_pin_is_not_spliced(self):
        # adjacent edits: the parent adds a comment above the pin, the child moves the pin
        base = pins('main', P1)
        child = pins('main', P3)
        parent = base.replace('SENSOR_PORTAL_REV', '# a note\nSENSOR_PORTAL_REV')
        r = self.pins_up(child, parent, {f'sensor-portal {P1}...{P3}': 'ahead', f'sensor-portal {P3}...main': 'ahead'}, auto='splice')
        self.assertIn('<<<<<<< ', self.show(self.fx.meta_bare, self.head, PINS_FILE))
        self.assertEqual(self.pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('pins rule: shadow, nothing changes: SENSOR_PORTAL_REV', self.rule_lines(r)[0])

    def test_the_pins_rule_does_not_run_in_the_distro_step(self):
        # a file of the same name in ds-tegra-distro is not the layer's pin file
        self.fx.distro_commit('ota-dev', {'x': 'unused\n'}, 'unused')
        self.new_fixture_with_distro_pins()
        r = self.up(auto='pins,splice', env=self.TOKEN)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([l for l in self.rule_lines(r) if l.startswith('pins rule')], [])

    def new_fixture_with_distro_pins(self):
        self.fx = t.Fixture()
        self.addCleanup(self.fx.close)
        self.fx.distro_commit('ota-dev', {PINS_FILE: pins('main', P1)}, 'a pins file in the distro repo')
        self.fx.seed_vertex()
        self.main_tip = self.fx.heads(self.fx.distro_bare)[PARENT]
        self.fx.tag('vertex/v1.0.0', self.main_tip)
        self.fx.open_line('1.0.0')
        self.distro_edits({PINS_FILE: pins('main', P2, 'c')}, {PINS_FILE: pins('2.x', P3, 'p')})


class ResolveConflictsCliTests(RulesCase):
    """release_git.py resolve-conflicts on an open merge."""

    def open_merge(self, child, parent):
        self.distro_edits(child, parent)
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '--detach', f'origin/{PARENT}')
        subprocess.run(['git', '-C', self.fx.distro, '-c', 'user.name=t', '-c', 'user.email=t@x', '-c', 'merge.conflictStyle=zdiff3',
                        'merge', '--no-ff', '--no-commit', f'origin/{CHILD}'], capture_output=True)
        self.out = os.path.join(self.fx.top, 'rules-out')

    def cli(self, *args, auto='splice'):
        env = {k: v for k, v in os.environ.items() if k != 'DS_MERGE_UP_AUTOMATION'}
        if auto is not None:
            env['DS_MERGE_UP_AUTOMATION'] = auto
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'resolve-conflicts', '--git-dir', self.fx.distro,
                               '--out', self.out, *args], capture_output=True, text=True, env=env)

    def read(self, name):
        with open(os.path.join(self.out, name)) as f:
            return f.read()

    def unmerged(self):
        return git(self.fx.distro, 'diff', '--name-only', '--diff-filter=U').split()

    def test_it_resolves_and_writes_its_outputs(self):
        self.open_merge({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['resolved docs/notes.txt (1 of 1 conflict); no markers left',
                                                 'no rule resolves foo.txt (conflict 1: both sides changed the same base lines)'])
        self.assertEqual(self.unmerged(), ['foo.txt'])
        self.assertEqual(self.read('remaining.txt'), 'foo.txt\n')
        self.assertEqual(self.read('trailers.txt'), 'Merge-Up-Rule: splice files=1 regions=1\n')
        self.assertEqual(self.read('log.txt'), r.stdout)
        self.assertIn('| `docs/notes.txt` | splice |', self.read('body.md'))
        self.assertNotIn('comment.md', os.listdir(self.out))

    def test_the_variable_decides_and_check_writes_nothing(self):
        self.open_merge({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})
        for args, auto, line in (((), None, 'no rule resolves docs/notes.txt (conflict 1: no rule is on for it)'),
                                 ((), 'shadow', 'shadow, nothing changes: would resolve docs/notes.txt (1 of 1 conflict)'),
                                 ((), 'lock-pin', 'shadow, nothing changes: would resolve docs/notes.txt (1 of 1 conflict)'),
                                 (('--check',), 'splice', 'would have resolved docs/notes.txt (1 of 1 conflict); no markers left')):
            r = self.cli(*args, auto=auto)
            self.assertEqual((r.returncode, r.stdout.splitlines()), (0, [line]), r.stderr)
            self.assertEqual(self.unmerged(), ['docs/notes.txt'])
        r = self.cli(auto='bogus')
        self.assertEqual(r.returncode, 1)
        self.assertIn("'bogus' is not a rule", r.stderr)

    def test_never_auto_comes_from_the_config_and_skip_leaves_a_path_alone(self):
        path = '.github/workflows/build.yml'
        self.open_merge({path: WORKFLOW_CHILD, 'docs/notes.txt': NOTES_CHILD}, {path: WORKFLOW_PARENT, 'docs/notes.txt': NOTES_PARENT})
        r = self.cli('--skip', 'docs/notes.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [f'never_auto: {path} matches .github/workflows/**, so no rule writes it; '
                                                 'the rules have an answer for 1 of 1 conflict, kept as a candidate patch'])
        self.assertEqual(sorted(self.unmerged()), [path, 'docs/notes.txt'])
        self.assertEqual(self.read('remaining.txt'), f'{path}\ndocs/notes.txt\n')
        self.assertEqual(json.loads(self.read('report.json'))['skipped'], ['docs/notes.txt'])
        self.assertIn('```diff', self.read('comment.md'))
        # with --layer the meta-dsensos list applies, which names no workflow of this shape either way:
        # the workflow pattern is on every list
        r = self.cli('--layer')
        self.assertIn(f'never_auto: {path} matches .github/workflows/**', r.stdout)

    def test_the_layer_step_reads_the_layer_list(self):
        # *.rules is on the meta-dsensos list only
        path = 'udev/90-ds.rules'
        self.open_merge({path: NOTES_CHILD}, {path: NOTES_PARENT})
        r = self.cli('--check', '--layer')
        self.assertEqual(r.stdout.splitlines(), [f'never_auto: {path} matches *.rules, so no rule writes it; '
                                                 'the rules have an answer for 1 of 1 conflict, kept as a candidate patch'], r.stderr)
        r = self.cli('--check')
        self.assertEqual(r.stdout.splitlines(), [f'would have resolved {path} (1 of 1 conflict); no markers left'], r.stderr)

    def test_pins_config(self):
        cfg = release_git.pins_config()
        self.assertEqual(cfg['pin_file'], PINS_FILE)
        acting = [p['rev'] for p in cfg['pairs'] if p['acts']]
        self.assertEqual(acting, ['SENSOR_PORTAL_REV'])
        pair = {p['rev']: p for p in cfg['pairs']}
        self.assertEqual((pair['DSREV']['branch'], pair['DSREV']['repo'], pair['DSREV']['acts']), ('DS_BRANCH', 'ds', False))
        # the release at each pin moves with it, named by the config, never derived from the pin's name
        self.assertEqual({p['rev']: p['release'] for p in cfg['pairs'] if p['release']},
                         {'DSREV': 'DSREV_RELEASE', 'SENSOR_PORTAL_REV': 'SENSOR_PORTAL_RELEASE'})
        self.assertNotIn('MRSILIB_REV', pair)   # no branch_variable: not a pair
        # a checkout without the pin bot's file: the rule is inert
        self.assertIsNone(release_git.pins_config(self.fx.top))
        write(os.path.join(self.fx.top, 'release', 'pin-propagation.json'), '{"sources": 3}')
        with self.assertRaises(release_git.Error):
            release_git.pins_config(self.fx.top)
        for bad in ('3', '"DSREV"', '"DS_BRANCH"', '"not a name"', '""'):
            write(os.path.join(self.fx.top, 'release', 'pin-propagation.json'),
                  '{"pin_file": "p.inc", "sources": {"DSREV": {"repo": "ds", "branch_variable": "DS_BRANCH", "release_variable": %s}}}' % bad)
            with self.assertRaises(release_git.Error, msg=bad):
                release_git.pins_config(self.fx.top)


if __name__ == '__main__':
    unittest.main()
