"""merge-up.sh with the merge rules (ENG-2234, plan 5.3 and 5.4). This repo runs one
rule, the splice.

The MergeUpTests fixture of test_release_git.py (one bare repo, the stub gh), with
a few multi-line files seeded on main before the line is opened, so the two
lines can edit neighbouring lines of one file. The rules themselves are tested
in test_merge_rules.py; this file is about what release_git.py
resolve-conflicts and merge-up.sh do with their result.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)

MERGE_UP, REPO = t.MERGE_UP, t.REPO
git, write = t.git, t.write
CHILD, PARENT = '1.x.x', 'main'
FIXED_DATE = '2026-01-01T00:00:00+00:00'
RULES = 'merge rules:        '
SUDOERS_FILE = 'ds-sensor-portal.sudoers'
WORKFLOW_FILE = '.github/workflows/build.yml'
# the layer file the pins rule of the OS distro repo works on; here it is a file like any other
PIN_FILE = 'conf/distro/include/ds-src-pins.inc'


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
SUDOERS_CHILD = SUDOERS.replace('/bin/a', '/bin/a2')
SUDOERS_PARENT = SUDOERS.replace('/bin/b', '/bin/b2')


def pin(rev):
    return text('SENSOR_PORTAL_BRANCH ?= "main"', f'SENSOR_PORTAL_REV ?= "{rev * 40}"')


class RulesCase(unittest.TestCase):
    """The fixture and its helpers. No test of its own."""

    def setUp(self):
        self.new_fixture()

    def new_fixture(self):
        """The MergeUpTests fixture, with the multi-line files on main first."""
        self.fx = t.Fixture()
        self.addCleanup(self.fx.close)
        self.fx.commit(PARENT, {'docs/notes.txt': NOTES, 'docs/more.txt': NOTES, WORKFLOW_FILE: WORKFLOW, SUDOERS_FILE: SUDOERS,
                                PIN_FILE: pin('1')}, 'files to conflict in')
        self.main_tip = self.fx.heads()[PARENT]
        self.fx.tag('v1.0.0', self.main_tip)
        self.fx.open_line('1.0.0')
        return self.fx

    def edits(self, child, parent):
        """The child line's edits (its tip is green), then the parent's."""
        self.green_sha = self.child_tip = self.fx.commit(CHILD, child, 'child work')
        write(self.fx.green, self.green_sha + '\n')
        self.parent_tip = self.fx.commit(PARENT, parent, 'parent work')

    # --- queries ---

    def up(self, *args, auto='splice', env=None, **kw):
        # fixed dates (the fixture drops GIT_* from the inherited environment): a re-run, or the
        # same merge under another value of the variable, then makes the same commit, sha included
        # (a caller's env may name other dates: a re-run on another day). A bot finish (ENG-2235)
        # waits 0s and polls every 0s unless the test says otherwise.
        extra = dict({'GIT_AUTHOR_DATE': FIXED_DATE, 'GIT_COMMITTER_DATE': FIXED_DATE,
                      'MERGE_UP_FINISH_WAIT_SECONDS': '0', 'MERGE_UP_FINISH_POLL_SECONDS': '0'}, **(env or {}))
        if auto is not None:
            extra['DS_MERGE_UP_AUTOMATION'] = auto
        return self.fx.run(MERGE_UP, '--child', CHILD, *args, extra_env=extra, **kw)

    def branch(self):
        return f'merge-up/{CHILD}/{self.green_sha[:7]}'

    def rule_lines(self, r):
        return [l.split(RULES, 1)[1] for l in r.stdout.splitlines() if l.startswith(RULES)]

    def message(self, sha):
        return git(self.fx.bare, 'log', '-1', '--format=%B', sha)

    def show(self, ref, path):
        return self.fx.show(ref, path) + '\n'

    def approve(self, number):
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': 'ds-os-release-bot'}]}]}
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps(review))

    def shape(self):
        """What a run left behind, comparable between two fixtures, whose commits differ by their
        dates (so every sha does, the ones in branch names and conflict markers included): per
        branch every file with its content and the commit message, and the PRs with their bodies."""
        import re
        plain = lambda text: re.sub(r'\b[0-9a-f]{7,40}\b', '<sha>', text)
        heads = {}
        for name, sha in self.fx.heads().items():
            files = git(self.fx.bare, 'ls-tree', '-r', '--name-only', sha).splitlines()
            heads[plain(name)] = (plain(self.message(sha)), {f: plain(self.fx.show(sha, f)) for f in files})
        return {'heads': heads, 'prs': [{k: plain(v) if isinstance(v, str) else v for k, v in p.items()} for p in self.fx.prs()]}

    def snapshot(self):
        return {'heads': self.fx.heads(), 'prs': [{k: v for k, v in p.items() if k != 'body'} for p in self.fx.prs()]}


class SpliceMergeUpTests(RulesCase):
    """A conflict the splice resolves, in whole or in part."""

    def adjacent(self):
        self.edits({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})

    def test_everything_resolved_is_a_pr_without_markers_never_a_push(self):
        self.resolved_pr()

    def resolved_pr(self):
        self.adjacent()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        # the parent line did not move: the result is a PR
        self.assertEqual(self.fx.heads()[PARENT], self.parent_tip)
        head = self.fx.heads()[self.branch()]
        self.assertEqual(git(self.fx.bare, 'rev-parse', head + '^1', head + '^2').split(), [self.parent_tip, self.child_tip])
        self.assertEqual(self.show(head, 'docs/notes.txt'), NOTES_BOTH)
        self.assertEqual(self.message(head),
                         f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=1 regions=1')
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        self.assertEqual((prs[0]['repo'], prs[0]['base'], prs[0]['head'], prs[0]['title']),
                         (REPO, PARENT, self.branch(), f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        body = prs[0]['body']
        self.assertIn('the merge-up rules resolved every one, so this branch has no conflict markers', body)
        self.assertIn('| `docs/notes.txt` | splice | 1 of 1 conflict resolved, no markers left', body)
        self.assertIn('This PR needs one approval.', body)
        self.assertIn('- Dispatch release-merge-up.yml on the default branch', body)
        self.assertIn('then finish it one of two ways:', body)
        self.assertNotIn('<<<<<<<', body)
        self.assertNotIn('comments', prs[0])
        self.assertEqual(self.rule_lines(r), [
            'resolved docs/notes.txt (1 of 1 conflict); no markers left',
            f'every conflict resolved by rule in {prs[0]["url"]} (1 file(s)); the PR has no markers and waits for one approval'])
        self.assertIn(f'pushed {self.branch()}: {head[:7]}, {PARENT} plus the merge of {CHILD}, all 1 conflicted file(s) resolved by rule, no markers', r.stdout)
        self.assertIn(f'::error::merge-up: {CHILD} -> {PARENT}: every conflict was resolved by rule in {prs[0]["url"]}', r.stdout)
        self.assertIn(f'Then dispatch release-merge-up.yml with child={CHILD} pr=1 on the default branch\n', r.stdout)
        return head

    def test_a_rerun_on_another_day_names_the_commit_on_origin(self):
        # Scratch run 44415920430 (2026-11-18) printed "already on origin at 0ebd320", its own
        # recomputed commit (same tree, parents and message, a new date), while origin held
        # 4eb8eff. The fixed dates of this fixture hide that, so this re-run gets another date.
        head = self.resolved_pr()
        before = self.snapshot()
        later = '2026-01-02T00:00:00+00:00'
        r = self.up(env={'GIT_AUTHOR_DATE': later, 'GIT_COMMITTER_DATE': later})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{self.branch()} is already on origin at {head[:7]}: the commit this run computes, untouched; left as it is, reusing its PR', r.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_a_rerun_reuses_the_branch_and_the_pr(self):
        head = self.resolved_pr()
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn(f'{self.branch()} is already on origin at {head[:7]}: the commit this run computes, untouched; left as it is, reusing its PR', r.stdout)
        self.assertNotIn('was not produced by this run', r.stdout)
        self.assertNotIn('holds commits this run did not make', r.stdout)
        self.assertEqual(self.fx.heads()[self.branch()], head)
        self.assertIn('every conflict was resolved by rule in https://github.com/ds/repo/pull/1', r.stdout)

    def test_one_approval_finishes_it(self):
        head = self.resolved_pr()
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()[PARENT]
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), head)
        self.assertEqual(self.show(tip, 'docs/notes.txt'), NOTES_BOTH)

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
        shadow = self.shape()
        head = self.fx.heads()[self.branch()]
        self.assertIn('<<<<<<< ', self.show(head, 'docs/notes.txt'))
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertNotIn('Merge-Up-Rule', self.message(head))
        # the same merge with the variable absent: the same branches, files, PRs and PR body, and no rule line
        self.new_fixture()
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), [])
        self.assertEqual(self.shape(), shadow)
        self.assertIn('<<<<<<< ', shadow['heads']['merge-up/1.x.x/<sha>'][1]['docs/notes.txt'])

    def test_a_partly_resolved_merge_is_a_conflict_pr_with_the_rule_table(self):
        self.edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads()[self.branch()]
        self.assertEqual(self.show(head, 'docs/notes.txt'), NOTES_BOTH)
        self.assertIn('<<<<<<< ', self.show(head, 'foo.txt'))
        self.assertEqual(self.message(head),
                         f'Merge {CHILD} into {PARENT} (conflicts to resolve)\n\nConflict markers left in: foo.txt\n\n'
                         'Merge-Up-Rule: splice files=1 regions=1')
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        # the list of files to decide holds only what still has markers
        self.assertIn('conflicts in:\n\n- `foo.txt`\n\n', pr['body'])
        self.assertIn('**Resolved by rule in this commit.**', pr['body'])
        self.assertIn('| `docs/notes.txt` | splice | 1 of 1 conflict resolved, no markers left', pr['body'])
        self.assertIn('Why the rules left the rest to a person:\n\n- `foo.txt`: conflict 1: both sides changed the same base lines', pr['body'])
        self.assertIn(f'::error::merge-up: {CHILD} -> {PARENT} conflicts (foo.txt); resolve {pr["url"]}', r.stdout)
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
        self.assertEqual(self.fx.heads()[PARENT], self.parent_tip)

    def test_a_commit_pushed_on_top_of_the_rule_commit_is_a_stop_signal(self):
        # The review's reproduction: the branch starts with the commit this run computes and
        # holds one more commit. It used to get the bot's "(resolved by rule)" PR and body.
        bot = self.resolved_pr()
        extra = self.fx.commit(self.branch(), {'scripts-release/lib.sh': 'echo changed\n'}, 'one more change, not a rule')
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads()[self.branch()], extra)
        self.assert_stopped(r, self.fx.prs()[1])
        self.assertIn('starts with the commit this run computes, and 1 more commit(s) were pushed on top of it', r.stdout)
        self.assertIn(f'`{self.branch()}` already existed on origin at `{extra[:7]}`', self.fx.prs()[1]['body'])
        self.assertIn('the rules resolve it, but ' + self.branch() + ' already exists on origin with other content and is left as it is', r.stdout)
        self.assertNotEqual(bot, extra)
        # and it is finished like any conflict PR: not without a person's approval
        r = self.up('--pr', '2')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual(self.fx.heads()[PARENT], self.parent_tip)

    def test_an_open_rule_pr_whose_branch_got_a_commit_loses_its_title(self):
        self.resolved_pr()
        self.fx.commit(self.branch(), {'docs/notes.txt': NOTES_BOTH + 'i\n'}, 'a fix by hand')
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
        self.edits({'docs/notes.txt': NOTES_CHILD, 'docs/more.txt': NOTES.replace('e\n', 'E child\n')}, {'docs/notes.txt': NOTES_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state'], p['title']) for p in self.fx.prs()], [(1, 'OPEN', f'Merge-up {CHILD} into {PARENT} (resolved by rule)')])
        extra = self.fx.commit(self.branch(), {'scripts-release/lib.sh': 'echo by a person\n'}, 'pushed by a person')
        self.parent_tip = self.fx.commit(PARENT, {'docs/more.txt': NOTES.replace('e\n', 'E parent\n')}, 'the parent overlaps the child')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads()[self.branch()], extra)
        self.assertEqual(self.fx.heads()[PARENT], self.parent_tip)
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
        self.edits({'foo.txt': 'child fix\n'}, {'foo.txt': 'main fix\n'})
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
        self.fx.commit(self.branch(), {'docs/notes.txt': NOTES_BOTH + 'i\n'}, 'a fix by hand')
        write(os.path.join(self.fx.state, 'fail-pr-edit'), '')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'::warning::merge-up: https://github.com/{REPO}/pull/1 still says "(resolved by rule)" and could not be retitled', r.stdout)
        self.assertNotIn('every conflict was resolved by rule in', r.stdout)

    def test_the_head_must_be_the_bots_commit_with_its_exact_message(self):
        # the tree, the parents and the bot's committer address, but a message of its own:
        # a commit someone made by hand in place of the bot's
        bot = self.resolved_pr()
        email = git(self.fx.bare, 'log', '-1', '--format=%ce', bot)
        tree = git(self.fx.bare, 'rev-parse', bot + '^{tree}')
        forged = git(self.fx.bare, 'commit-tree', tree, '-p', self.parent_tip, '-p', self.child_tip, '-m',
                     f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=9 regions=9', env={'GIT_COMMITTER_EMAIL': email})
        git(self.fx.bare, 'update-ref', f'refs/heads/{self.branch()}', forged)
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads()[self.branch()], forged)
        self.assert_stopped(r, self.fx.prs()[1])
        self.assertIn("has the tree this run computes, but not the bot's committer address and message", r.stdout)

    def test_a_partly_resolved_branch_with_a_commit_on_top_makes_no_rule_claim(self):
        self.edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('**Resolved by rule in this commit.**', self.fx.prs()[0]['body'])
        extra = self.fx.commit(self.branch(), {'docs/notes.txt': NOTES + 'changed by hand\n'}, 'a resolution in progress')
        self.close_prs()
        r = self.up()
        self.assertEqual(self.fx.heads()[self.branch()], extra)
        # every file the merge conflicts in is to decide again, also the one the rule resolved
        self.assert_stopped(r, self.fx.prs()[1], files='- `docs/notes.txt`\n- `foo.txt`\n')
        self.assertIn(f'::error::merge-up: {CHILD} -> {PARENT} conflicts (docs/notes.txt foo.txt); resolve', r.stdout)

    def test_without_a_rule_result_a_resolvers_commits_are_not_labelled(self):
        # the plain conflict PR of a run without the rules: a resolver's commits on the
        # branch are the normal case there and get no label
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        self.fx.commit(self.branch(), {'docs/notes.txt': NOTES_BOTH}, 'resolved by hand')
        self.close_prs()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('holds commits this run did not make', r.stdout + self.fx.prs()[1]['body'])
        self.assertNotIn('was not produced by this run', r.stdout + self.fx.prs()[1]['body'])

    def test_no_run_merges_a_rule_resolved_pr_without_an_approval(self):
        # green required checks change nothing: this repo has no bot finish, and a
        # rule-resolved PR is merged only by a pr=N run after a person's approval. No merge
        # call of either kind is made: neither `gh pr merge` nor the REST merge, the call a
        # bot merge with no approval makes (ENG-2235), which the stub performs
        head = self.resolved_pr()
        green = {'reviewDecision': None, 'mergeStateStatus': 'CLEAN', 'reviews': [],
                 'statusCheckRollup': [{'__typename': 'CheckRun', 'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(green))
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual((self.fx.heads()[PARENT], self.fx.heads()[self.branch()]), (self.parent_tip, head))

    def test_a_branch_that_holds_something_else_is_left_alone(self):
        # an earlier run without the rule pushed the conflict commit with markers
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        head = self.fx.heads()[self.branch()]
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fx.heads()[self.branch()], head)
        self.assertIn(f'{self.branch()} was not produced by this run', r.stdout)
        self.assertIn('the rules resolve it, but ' + self.branch() + ' already exists on origin with other content and is left as it is', r.stdout)
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')

    def test_a_branch_that_holds_something_else_lists_every_conflicted_file(self):
        # An earlier run without the rule pushed the conflict commit with markers in both files,
        # and the bot closed its PR as superseded. This run's rule resolves docs/notes.txt, but not on that branch:
        # the PR it opens there must name both files, not only the one the rule left.
        self.edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1)
        head = self.fx.heads()[self.branch()]
        self.assertIn('<<<<<<< ', self.show(head, 'docs/notes.txt'))
        self.fx.supersede_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads()[self.branch()], head)
        self.assertIn(f'{self.branch()} was not produced by this run', r.stdout)
        pr = self.fx.prs()[1]
        self.assertEqual((pr['head'], pr['state']), (self.branch(), 'OPEN'))
        self.assertIn('conflicts in:\n\n- `docs/notes.txt`\n- `foo.txt`\n\n', pr['body'])
        self.assertIn('**This branch was not produced by this run.**', pr['body'])
        self.assertNotIn('Resolved by rule in this commit', pr['body'])
        self.assertIn(f'::error::merge-up: {CHILD} -> {PARENT} conflicts (docs/notes.txt foo.txt); resolve {pr["url"]}', r.stdout)

    def test_a_conflict_without_text_markers_skips_the_rules(self):
        self.edits({'docs/notes.txt': NOTES_CHILD, 'blob.bin': 'c\0child\n'}, {'docs/notes.txt': NOTES_PARENT, 'blob.bin': 'p\0parent\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.rule_lines(r), [f'not applied: a conflict here leaves no text markers, so the PR goes on {CHILD} itself '
                                              'and a rule\'s result would not be on it'])
        self.assertEqual(self.fx.heads()[self.branch()], self.child_tip)
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
        head = self.fx.heads()[self.branch()]
        self.assertIn('<<<<<<< ', self.show(head, 'docs/notes.txt'))
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
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={
            'DS_MERGE_UP_AUTOMATION': 'splice', 'GIT_AUTHOR_DATE': FIXED_DATE, 'GIT_COMMITTER_DATE': FIXED_DATE})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('no text markers', r.stdout)
        self.assertEqual(self.rule_lines(r), ["not evaluated (the rules' outputs could not be written ([Errno 28] No space left on device))"])
        # the phase-0 conflict PR: the merge with its markers, exactly what a run without the rules pushes
        head = self.fx.heads()[self.branch()]
        self.assertNotEqual(head, self.child_tip)
        marked = self.show(head, 'docs/notes.txt')
        self.assertIn('<<<<<<< HEAD\n', marked)
        self.assertIn(f'>>>>>>> {self.child_tip}\n', marked)
        self.assertNotIn('Merge-Up-Rule', self.message(head))
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('The conflict markers are committed', pr['body'])
        got = self.shape()
        self.new_fixture()
        self.adjacent()
        self.up(auto=None)
        self.assertEqual(got, self.shape())

    def test_the_summary_gets_the_rule_lines(self):
        self.adjacent()
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.up(env={'MERGE_UP_SUMMARY': summary})
        self.assertEqual(r.returncode, 1)
        with open(summary) as f:
            got = f.read()
        self.assertIn('- merge rules: resolved docs/notes.txt (1 of 1 conflict); no markers left', got)


class StubRestMergeTests(RulesCase):
    """The stub gh's REST merge (ENG-2235), the call a bot merge with no approval makes: it merges
    a PR at the pinned head, GitHub's refusals come back as gh prints them, and merge_calls sees
    every call, so a test that asserts no merge call cannot miss one."""

    adjacent = SpliceMergeUpTests.adjacent
    resolved_pr = SpliceMergeUpTests.resolved_pr

    def put(self, sha):
        return subprocess.run([os.path.join(self.fx.binp, 'gh'), 'api', '-X', 'PUT', f'repos/{REPO}/pulls/1/merge',
                               '-f', 'merge_method=merge', '-f', f'sha={sha}'], capture_output=True, text=True, env=self.fx.env())

    def test_it_merges_at_the_pinned_head_refuses_like_github_and_is_a_merge_call(self):
        head = self.resolved_pr()
        # a moved head: 409, and nothing merged
        r = self.put('0' * 40)
        self.assertEqual((r.returncode, json.loads(r.stdout)['status']), (1, '409'))
        self.assertIn('(HTTP 409)', r.stderr)
        # GitHub refusing it (a ruleset, a validation): the status of rest-merge-error.json
        for status in ('405', '422'):
            with self.subTest(status=status):
                write(os.path.join(self.fx.state, 'rest-merge-error.json'), json.dumps({'status': status, 'message': 'refused'}))
                r = self.put(head)
                self.assertEqual((r.returncode, json.loads(r.stdout)['status']), (1, status))
                self.assertIn(f'gh: refused (HTTP {status})', r.stderr)
        os.remove(os.path.join(self.fx.state, 'rest-merge-error.json'))
        self.assertEqual((self.fx.heads()[PARENT], self.fx.prs()[0]['state']), (self.parent_tip, 'OPEN'))
        # the merge
        r = self.put(head)
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual((doc['merged'], doc['sha']), (True, self.fx.heads()[PARENT]))
        self.assertEqual(git(self.fx.bare, 'rev-parse', doc['sha'] + '^1', doc['sha'] + '^2').split(), [self.parent_tip, head])
        self.assertEqual((self.fx.prs()[0]['state'], self.fx.prs()[0]['merged_head']), ('MERGED', head))
        # a PR that is no longer open: 405
        self.assertEqual(json.loads(self.put(head).stdout)['status'], '405')
        # every one of the five calls is a merge call
        self.assertEqual(len(t.merge_calls(self.fx.gh_argv())), 5)
        self.assertNotIn('pr merge', self.fx.gh_log())

    def test_every_spelling_of_the_path_is_the_same_merge(self):
        # 2c re-review, should-fix 2: a leading /, the https://api.github.com/ prefix and the fields in the query string
        # reach the same endpoint on GitHub; the stub performs each, and merge_calls counts each
        head = self.resolved_pr()
        env = self.fx.env()
        paths = (f'/repos/{REPO}/pulls/1/merge', f'https://api.github.com/repos/{REPO}/pulls/1/merge',
                 f'repos/{REPO}/pulls/1/merge?merge_method=merge&sha={"0" * 40}')
        for path in paths:
            with self.subTest(path=path):
                extra = [] if '?' in path else ['-f', 'merge_method=merge', '-f', 'sha=' + '0' * 40]
                r = subprocess.run([os.path.join(self.fx.binp, 'gh'), 'api', '-X', 'PUT', path, *extra], capture_output=True, text=True, env=env)
                self.assertEqual((r.returncode, json.loads(r.stdout)['status']), (1, '409'), r.stderr)
        r = subprocess.run([os.path.join(self.fx.binp, 'gh'), 'api', '--method', 'PUT', paths[0], '-f', f'sha={head}'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.fx.prs()[0]['state'], self.fx.prs()[0]['merged_head']), ('MERGED', head))
        self.assertEqual(len(t.merge_calls(self.fx.gh_argv())), 4)


class NeverAutoMergeUpTests(RulesCase):
    """No rule writes a never_auto file; the splice's answer is a PR comment with a patch."""

    def test_a_workflow_file_keeps_its_markers_and_gets_the_candidate_comment(self):
        path = '.github/workflows/build.yml'
        self.edits({path: WORKFLOW_CHILD, 'docs/notes.txt': NOTES_CHILD}, {path: WORKFLOW_PARENT, 'docs/notes.txt': NOTES_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads()[self.branch()]
        marked = self.show(head, path)
        self.assertIn('<<<<<<< ', marked)
        self.assertEqual(self.show(head, 'docs/notes.txt'), NOTES_BOTH)
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
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '--detach', head)
        subprocess.run(['git', '-C', self.fx.repo, 'apply', '-'], input=patch, text=True, check=True)
        with open(os.path.join(self.fx.repo, path)) as f:
            self.assertEqual(f.read(), WORKFLOW_BOTH)
        git(self.fx.repo, 'checkout', '-q', '--', path)
        self.assertIn(f'never_auto: {path} matches .github/workflows/**, so no rule writes it; the rules have an answer for 1 of 1 conflict, '
                      'kept as a candidate patch', self.rule_lines(r))
        self.assertIn(f'commented on {pr["url"]} with the rules\' candidate for the never_auto file(s)', r.stdout)
        # the commit's trailer counts only the file a rule wrote
        self.assertIn('Merge-Up-Rule: splice files=1 regions=1', self.message(head))
        # a re-run reuses the PR and does not comment again
        r = self.up()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.fx.prs()[0]['comments']), 1)
        self.assertEqual(self.fx.heads()[self.branch()], head)

    def test_a_comment_that_fails_is_a_warning(self):
        path = '.github/workflows/build.yml'
        self.edits({path: WORKFLOW_CHILD}, {path: WORKFLOW_PARENT})
        # break gh pr comment in the stub
        gh = os.path.join(self.fx.binp, 'gh')
        with open(gh) as f:
            stub = f.read()
        with open(gh, 'w') as f:
            f.write(stub.replace("if pos[:2] == ['pr', 'comment']:", "if pos[:2] == ['pr', 'comment']:\n        print('HTTP 403', file=sys.stderr)\n        return 1\n    if False:"))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::warning::merge-up: could not comment on https://github.com/ds/repo/pull/1 with the rules\' candidate (HTTP 403)', r.stdout)
        self.assertEqual(len(self.fx.prs()), 1)

    def test_in_shadow_no_comment_is_posted(self):
        path = '.github/workflows/build.yml'
        self.edits({path: WORKFLOW_CHILD}, {path: WORKFLOW_PARENT})
        r = self.up(auto='shadow')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('comments', self.fx.prs()[0])
        self.assertEqual(self.rule_lines(r), [f'never_auto: {path} matches .github/workflows/**, so no rule writes it; '
                                              'shadow: the rules would have an answer for 1 of 1 conflict'])

    def test_a_sudoers_conflict_keeps_its_markers(self):
        """sensor-portal #297's file: two neighbouring edits the splice could join, and never does."""
        self.edits({SUDOERS_FILE: SUDOERS_CHILD}, {SUDOERS_FILE: SUDOERS_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads()[PARENT], self.parent_tip)
        head = self.fx.heads()[self.branch()]
        self.assertIn('<<<<<<< ', self.show(head, SUDOERS_FILE))
        pr = self.fx.prs()[0]
        self.assertEqual((pr['repo'], pr['title']), (REPO, f'Merge-up {CHILD} into {PARENT} (conflict)'))
        self.assertIn(f'- `{SUDOERS_FILE}` (matches `*sudoers*`)', pr['body'])
        self.assertEqual(self.rule_lines(r), [f'never_auto: {SUDOERS_FILE} matches *sudoers*, so no rule writes it; '
                                              'the rules have an answer for 1 of 1 conflict, kept as a candidate patch'])
        self.assertEqual(len(pr['comments']), 1)
        self.assertNotIn('Merge-Up-Rule', self.message(head))
        # the bot does not finish it on one approval by the rule: it is a conflict PR with markers
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'still has conflict markers in: {SUDOERS_FILE}', r.stdout)

    def test_with_the_variable_absent_the_rules_are_never_run(self):
        """Phase-0 behaviour: no rule line, and resolve-conflicts is not even called (here it would fail)."""
        self.edits({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})
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


class ResolveConflictsCliTests(RulesCase):
    """release_git.py resolve-conflicts on an open merge."""

    def open_merge(self, child, parent):
        self.edits(child, parent)
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '--detach', f'origin/{PARENT}')
        subprocess.run(['git', '-C', self.fx.repo, '-c', 'user.name=t', '-c', 'user.email=t@x', '-c', 'merge.conflictStyle=zdiff3',
                        'merge', '--no-ff', '--no-commit', f'origin/{CHILD}'], capture_output=True)
        self.out = os.path.join(self.fx.top, 'rules-out')

    def cli(self, *args, auto='splice'):
        env = {k: v for k, v in os.environ.items() if k != 'DS_MERGE_UP_AUTOMATION'}
        if auto is not None:
            env['DS_MERGE_UP_AUTOMATION'] = auto
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'resolve-conflicts', '--git-dir', self.fx.repo,
                               '--out', self.out, *args], capture_output=True, text=True, env=env)

    def read(self, name):
        with open(os.path.join(self.out, name)) as f:
            return f.read()

    def unmerged(self):
        return git(self.fx.repo, 'diff', '--name-only', '--diff-filter=U').split()

    def test_it_resolves_and_writes_its_outputs(self):
        self.open_merge({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.splitlines(), ['resolved docs/notes.txt (1 of 1 conflict); no markers left',
                                                 'no rule resolves foo.txt (conflict 1: both sides changed the same base lines)'])
        self.assertEqual(self.unmerged(), ['foo.txt'])
        with open(os.path.join(self.fx.repo, 'docs/notes.txt')) as f:
            self.assertEqual(f.read(), NOTES_BOTH)
        self.assertEqual(self.read('remaining.txt'), 'foo.txt\n')
        self.assertEqual(self.read('trailers.txt'), 'Merge-Up-Rule: splice files=1 regions=1\n')
        self.assertEqual(self.read('log.txt'), r.stdout)
        self.assertIn('| `docs/notes.txt` | splice |', self.read('body.md'))
        self.assertNotIn('comment.md', os.listdir(self.out))

    def test_the_variable_decides_and_check_writes_nothing(self):
        self.open_merge({'docs/notes.txt': NOTES_CHILD}, {'docs/notes.txt': NOTES_PARENT})
        for args, auto, line in (((), None, 'no rule resolves docs/notes.txt (conflict 1: no rule is on for it)'),
                                 ((), 'off', 'no rule resolves docs/notes.txt (conflict 1: no rule is on for it)'),
                                 ((), 'shadow', 'shadow, nothing changes: would resolve docs/notes.txt (1 of 1 conflict)'),
                                 (('--check',), 'splice', 'would have resolved docs/notes.txt (1 of 1 conflict); no markers left')):
            r = self.cli(*args, auto=auto)
            self.assertEqual((r.returncode, r.stdout.splitlines()), (0, [line]), r.stderr)
            self.assertEqual(self.unmerged(), ['docs/notes.txt'])
        for bad, why in (('bogus', "'bogus' is not a rule"), ('splice,pins', "'pins' is not a rule of this repo")):
            r = self.cli(auto=bad)
            self.assertEqual(r.returncode, 1)
            self.assertIn(why, r.stderr)
            self.assertEqual(self.unmerged(), ['docs/notes.txt'])

    def test_never_auto_comes_from_the_config_and_skip_leaves_a_path_alone(self):
        self.open_merge({WORKFLOW_FILE: WORKFLOW_CHILD, SUDOERS_FILE: SUDOERS_CHILD, 'docs/notes.txt': NOTES_CHILD},
                        {WORKFLOW_FILE: WORKFLOW_PARENT, SUDOERS_FILE: SUDOERS_PARENT, 'docs/notes.txt': NOTES_PARENT})
        r = self.cli('--skip', 'docs/notes.txt')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), [
            f'never_auto: {WORKFLOW_FILE} matches .github/workflows/**, so no rule writes it; '
            'the rules have an answer for 1 of 1 conflict, kept as a candidate patch',
            f'never_auto: {SUDOERS_FILE} matches *sudoers*, so no rule writes it; '
            'the rules have an answer for 1 of 1 conflict, kept as a candidate patch'])
        self.assertEqual(sorted(self.unmerged()), [WORKFLOW_FILE, 'docs/notes.txt', SUDOERS_FILE])
        self.assertEqual(self.read('remaining.txt'), f'{WORKFLOW_FILE}\ndocs/notes.txt\n{SUDOERS_FILE}\n')
        self.assertEqual(json.loads(self.read('report.json'))['skipped'], ['docs/notes.txt'])
        self.assertIn('```diff', self.read('comment.md'))
        self.assertEqual(self.read('trailers.txt'), '')

    def test_the_list_is_the_one_of_the_config_in_use(self):
        """--root names another release/config.json: its never_auto list applies, not this checkout's."""
        self.open_merge({SUDOERS_FILE: SUDOERS_CHILD}, {SUDOERS_FILE: SUDOERS_PARENT})
        root = os.path.join(self.fx.top, 'other-root')
        write(os.path.join(root, 'release/config.json'),
              json.dumps({'products': ['ds'], 'merge_up': {'never_auto': ['.github/workflows/**']}}))
        r = self.cli('--check', '--root', root)
        self.assertEqual(r.stdout.splitlines(), [f'would have resolved {SUDOERS_FILE} (1 of 1 conflict); no markers left'], r.stderr)
        r = self.cli('--check')
        self.assertEqual(r.stdout.splitlines(), [f'never_auto: {SUDOERS_FILE} matches *sudoers*, so no rule writes it; '
                                                 'the rules have an answer for 1 of 1 conflict, kept as a candidate patch'], r.stderr)

    def test_the_pins_rule_is_never_on_here(self):
        """merge_rules.py carries the pins rule for the OS distro repo; this repo's caller keeps
        it off and passes no pin configuration, whatever a file is called."""
        self.open_merge({PIN_FILE: pin('2')}, {PIN_FILE: pin('3')})
        r = self.cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(self.read('report.json'))
        self.assertEqual(report['modes'], {'splice': 'act', 'pins': 'off'})
        self.assertEqual(self.unmerged(), [PIN_FILE])
        self.assertNotIn('pins rule', r.stdout)
        self.assertEqual(r.stdout.splitlines(), [f'no rule resolves {PIN_FILE} (conflict 1: both sides changed the same base lines)'])


# --- bot-finish (ENG-2235, 2c) ----------------------------------------------------

BOT = 'bot-finish:         '
APP = 'ds-os-release-bot[bot]'   # the release App's login (GIT_BOT_NAME)
ACT = 'splice,bot-finish'
GREEN = [{'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
CHECKS = 'pr-checks'
ALICE = {'login': 'alice', 'type': 'User'}
PR1, PR2, PR3 = (f'https://github.com/{REPO}/pull/{n}' for n in (1, 2, 3))
MARK = '<!-- merge-up-bot-finish -->'
RUN_URL = f'https://github.com/{REPO}/actions/runs/4242'
TOUCHES = ('pr create', 'pr merge', t.REST_MERGE, 'pr close', 'pr comment', 'pr edit')


def bot_merge(sha, number=1):
    """The bot finish's merge call as the stub logs it: the REST merge pinned to the bot's commit."""
    return f'api -X PUT repos/{REPO}/pulls/{number}/merge -f merge_method=merge -f sha={sha}'


class BotFinishCase(RulesCase):
    """The helpers of the bot-finish tests. No test of its own."""

    adjacent = SpliceMergeUpTests.adjacent

    def bot_lines(self, r):
        return [l[len(BOT):] for l in r.stdout.splitlines() if l.startswith(BOT)]

    def errors(self, r):
        return [l for l in r.stdout.splitlines() if l.startswith('::error::')]

    def checks(self, rollup, number=1):
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps({'statusCheckRollup': rollup}))

    def reviews(self, *reviews, number=1):
        write(os.path.join(self.fx.state, f'pr-{number}-rest-reviews.json'),
              json.dumps([{'user': user, 'state': state} for user, state in reviews]))

    def view_hook(self, script):
        write(os.path.join(self.fx.state, 'pr-view-hook'), script)

    def push_hook(self, branch, files, message):
        """A view hook: someone pushes a commit to <branch> from a clone of their own."""
        writes = ''.join(f"mkdir -p \"$(dirname '{path}')\"\nprintf '%s' '{content}' > '{path}'\n" for path, content in files.items())
        self.view_hook(f"""set -e
d=$(mktemp -d)
git clone -q {self.fx.bare} "$d"
cd "$d"
git checkout -q {branch}
{writes}git add -A
git -c user.name=mallory -c user.email=m@x commit -q -m '{message}'
git push -q origin {branch}
""")

    def head(self):
        return self.fx.heads()[self.branch()]

    def parent(self):
        return self.fx.heads()[PARENT]

    def calls(self, since):
        return self.fx.gh_log()[len(since):]

    def assert_open_and_untouched(self, r, why, merged=0, number=1):
        """Red, one error that names the PR and why, the PR open, the parent where it was."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        pr = self.fx.prs()[number - 1]
        self.assertEqual(pr['state'], 'OPEN')
        self.assertEqual(self.parent(), self.parent_tip)
        self.assertEqual(len(t.merge_calls(self.fx.gh_argv())), merged)
        # the refused merges are its only writes outside the allowlist (round 4 review, finding 2)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), t.merge_calls(self.fx.gh_argv()))
        errors = self.errors(r)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'bot-finished PR {pr["url"]} was not merged: ', errors[0])
        self.assertIn(why, errors[0])
        self.assertIn('The PR stays open', errors[0])
        self.assertIn(f'child={CHILD} pr={number}', errors[0])

    def assert_waits(self, r, why, number=1):
        """The rule-resolved PR is open and waits for one approval, as without the word, and the
        bot says why it does not merge it."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        url = f'https://github.com/{REPO}/pull/{number}'
        pr = self.fx.prs()[number - 1]
        self.assertEqual((pr['state'], pr['title']), ('OPEN', f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        self.assertIn('This PR needs one approval', pr['body'])
        self.assertNotIn(MARK, pr['body'])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.parent(), self.parent_tip)
        self.assertEqual(self.bot_lines(r), [f'not applied to {url}: {why}; it waits for one approval'])
        self.assertEqual(self.errors(r), [f'::error::merge-up: {CHILD} -> {PARENT}: every conflict was resolved by rule in {url}, '
                                          f'which has no markers and waits for one approval. Then dispatch release-merge-up.yml '
                                          f'with child={CHILD} pr={number} on the default branch'])

    def next_child_commit(self, content):
        """A new green commit on the child that keeps the conflict: a new branch and a new PR."""
        self.green_sha = self.child_tip = self.fx.commit(CHILD, {'docs/other.txt': content}, 'more child work')
        write(self.fx.green, self.green_sha + '\n')

    def waiting_pr(self, auto='splice'):
        """Run 1 opened the rule-resolved PR, which waits (no check reported, or the word absent)."""
        self.adjacent()
        r = self.up(auto=auto)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.checks(GREEN)
        return self.head()


class RuleBotFinishTests(BotFinishCase):
    """bot-finish acting: the bot merges a PR the splice resolved, with no approval, once its
    required checks succeed on the bot's own commit. Every stop leaves the PR open and the run red."""

    def test_the_word_merges_a_rule_resolved_pr_once_its_checks_are_green(self):
        self.adjacent()
        self.checks(GREEN)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        head, tip = self.head(), self.parent()
        pr = self.fx.prs()[0]
        self.assertEqual((pr['state'], pr['merged_head'], pr['title']), ('MERGED', head, f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        self.assertEqual(git(self.fx.bare, 'rev-parse', head + '^1', head + '^2').split(), [self.parent_tip, self.child_tip])
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^1', tip + '^2').split(), [self.parent_tip, head])
        self.assertEqual(self.show(tip, 'docs/notes.txt'), NOTES_BOTH)
        # the REST merge pinned to the bot's commit, never gh pr merge, never --admin
        log = self.fx.gh_log()
        self.assertEqual(t.merge_calls(self.fx.gh_argv()), [bot_merge(head)])
        self.assertNotIn('pr merge', log)
        self.assertNotIn('--admin', log)
        # the body says the bot merges it and how to stop it; the comment is the record
        body = pr['body']
        self.assertIn(f'The bot merges this PR itself, with no approval (`bot-finish` is in DS_MERGE_UP_AUTOMATION), once `pr-checks` '
                      f'succeed on `{head[:7]}`. It waits up to 25 minutes', body)
        self.assertIn('request changes on this PR or close it: either stops the bot', body)
        self.assertNotIn('This PR needs one approval', body)
        self.assertTrue(body.endswith(MARK + '\n'), body)
        self.assertEqual(pr['comments'], [f'Merged by release-merge-up at {tip} with no approving review: the merge-up rules resolved every '
                                          f'conflict of this merge in this commit (`{head[:7]}`), and {CHECKS} succeeded on it. Run: {RUN_URL}'])
        self.assertEqual(self.rule_lines(r)[-1], f'every conflict resolved by rule in {PR1} (1 file(s)); the PR has no markers and the bot '
                                                 f'merges it once {CHECKS} succeed')
        self.assertIn(f'waiting for pr-checks on {head[:7]} (at most 0s)', r.stdout)
        self.assertIn(f'required checks green on {head[:7]}: pr-checks: SUCCESS', r.stdout)
        self.assertIn(f'required checks from their sources on {head[:7]}: pr-checks: .github/workflows/pr-checks.yml', r.stdout)
        self.assertIn(f'merged PR {REPO}#1 (head {head}, no approval: the merge rules and the required checks)', r.stdout)
        self.assertIn('post-check ok', r.stdout)
        self.assertIn(f'done: {CHILD} merged into {PARENT} at {tip} (bot-finished PR 1)', r.stdout)
        self.assertEqual(self.errors(r), [])
        self.assertEqual(self.bot_lines(r), [])
        # the sources were read after the view said green, and before the merge
        reads = (f'api --paginate repos/{REPO}/commits/{head}/check-runs?per_page=100',
                 f'api --paginate repos/{REPO}/actions/runs?head_sha={head}&per_page=100',
                 f'api --paginate repos/{REPO}/actions/runs/2000/jobs?filter=all&per_page=100')
        for read in reads:
            self.assertIn(read, log)
        self.assertTrue(log.index('pr view 1') < log.index(reads[0]) < log.index(bot_merge(head)), log)
        # a second run has nothing to do
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)

    def test_the_wait_is_finish_wait_minutes(self):
        # no override: the config's 25 minutes (green at once, so no wait); then a config of 2
        self.adjacent()
        self.checks(GREEN)
        r = self.fx.run(MERGE_UP, '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': ACT})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'waiting for pr-checks on {self.head()[:7]} (at most 1500s)', r.stdout)
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        scripts = self.fx.scripts_copy({'finish.wait_minutes': 2})
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': ACT})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('(at most 120s)', r.stdout)
        self.assertIn('It waits up to 2 minutes', self.fx.prs()[0]['body'])

    def test_it_waits_for_checks_that_are_still_running(self):
        self.adjacent()
        running = [{'name': 'pr-checks', 'status': 'IN_PROGRESS', 'conclusion': None}]
        write(os.path.join(self.fx.state, 'pr-1-review.json'),
              json.dumps([{'statusCheckRollup': []}, {'statusCheckRollup': running}, {'statusCheckRollup': GREEN}]))
        r = self.up(auto=ACT, env={'MERGE_UP_FINISH_WAIT_SECONDS': '60'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        self.assertEqual(self.fx.gh_log().count(f'pr view 1 -R {REPO} --json state,headRefOid,statusCheckRollup'), 3)
        self.assertIn('(at most 60s)', r.stdout)

    def test_a_red_check_leaves_the_pr_open(self):
        self.adjacent()
        self.checks([{'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}])
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, f'a required check failed on {self.head()[:7]} (pr-checks: FAILURE)')

    def test_a_wait_that_runs_out_leaves_the_pr_open(self):
        self.adjacent()
        self.checks([])
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, f'the required checks were not green on {self.head()[:7]} after 0s '
                                          '(pr-checks: not reported yet)')
        # the stop says the next run of the child finishes it (plan 7.2 c)
        self.assertIn(f'or the next merge-up run of {CHILD} finishes it once its required checks are green', self.errors(r)[0])

    def test_a_required_check_from_another_source_stops_it(self):
        self.adjacent()
        self.checks(GREEN)
        forged = [{'id': 3500, 'name': 'pr-checks', 'status': 'completed', 'conclusion': 'success', 'head_sha': None,
                   'started_at': '2026-11-20T11:30:00Z', 'completed_at': '2026-11-20T11:40:00Z',
                   'app': {'slug': 'github-actions'}, 'check_suite': {'id': 1500}}]
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps(forged))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps([
            {'id': 2500, 'path': '.github/workflows/build.yml', 'event': 'pull_request', 'head_branch': self.branch(),
             'head_sha': None, 'status': 'completed', 'conclusion': 'success', 'check_suite_id': 1500}]))
        r = self.up(auto=ACT)
        # no run of this PR vouches for pr-checks: the bot waits for one (2c review, trust should-fix 1), and the
        # timeout stops it
        self.assert_open_and_untouched(r, f'the required checks were not green on {self.head()[:7]} after 0s (pr-checks: no check run '
                                          'of that name comes from a .github/workflows/pr-checks.yml run for this pull request '
                                          f'(#1 into {PARENT} from {self.branch()} in {REPO}), and its newest comes from '
                                          '.github/workflows/build.yml')
        self.assertNotIn('required checks green on', r.stdout)
        # a check run posted through the checks API into the suite of this PR's own run, newer than its job: a stop
        # like a red check
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([
            {'id': 3000, 'name': 'pr-checks', 'status': 'completed', 'conclusion': 'success', 'head_sha': None,
             'started_at': '2026-11-20T11:30:00Z', 'completed_at': '2026-11-20T11:40:00Z',
             'app': {'slug': 'github-actions'}, 'check_suite': {'id': 1000}},
            {'id': 3500, 'name': 'pr-checks', 'status': 'completed', 'conclusion': 'success', 'head_sha': None,
             'started_at': '2026-11-20T11:30:00Z', 'completed_at': '2026-11-20T11:50:00Z',
             'app': {'slug': 'github-actions'}, 'check_suite': {'id': 1000}}]))
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, 'did not come from its source in merge_up.finish.check_sources (pr-checks: its newest '
                                          'check run is in the check suite of the .github/workflows/pr-checks.yml run 2000 but is no '
                                          'job of it')
        self.assertNotIn('required checks green on', r.stdout)

    def test_another_pr_of_the_bot_branch_stops_the_finish(self):
        # 2c re-review, should-fix 1: GitHub lists every open PR of the branch in each run of it, so the run of
        # another PR of the bot's branch into another base can stand in for this PR's own. The bot reads every PR of
        # the branch and the PR's events: another base or a changed base is a stop like a red check
        self.adjacent()
        self.checks(GREEN)
        branch = self.branch()
        write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps(
            [{'number': 9, 'state': 'closed', 'head': {'ref': branch, 'repo': {'full_name': REPO}}, 'base': {'ref': 'attacker'}}]))
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, 'did not come from its source in merge_up.finish.check_sources '
                                          f'(#9 (closed) into attacker also comes from {branch}')
        owner = REPO.split('/')[0]
        for call in (f'api --paginate repos/{REPO}/pulls?state=all&head={owner}:{branch}&per_page=100',
                     f'api --paginate repos/{REPO}/issues/1/events?per_page=100'):
            self.assertIn(call, self.fx.gh_log())
        # this PR's base changed and back
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'issue-events.json'), json.dumps({f'{REPO}#1': [{'event': 'base_ref_changed'}]}))
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, '(the base of #1 was changed')
        # the list cannot be read: the bot waits, and the timeout stops it
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'fail-head-prs'), '')
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, 'the required checks were not green')
        self.assertIn(f'(the pull requests of {self.branch()} or the events of #1 could not be read', r.stdout)
        # an earlier PR of the branch into the same base, the release App's and never retargeted: merged as usual
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps([self.same_base(0, APP)]))
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        self.assertIn(f'api --paginate repos/{REPO}/issues/0/events?per_page=100', self.fx.gh_log())

    def same_base(self, n, user, state='closed'):
        """Another PR of the bot's branch into the parent, as REST lists it, opened by user."""
        return {'number': n, 'state': state, 'user': {'login': user, 'type': 'Bot' if user.endswith('[bot]') else 'User'},
                'head': {'ref': self.branch(), 'repo': {'full_name': REPO}}, 'base': {'ref': PARENT}}

    def test_a_same_base_pr_of_the_bot_branch(self):
        # round 4 review, finding 1, for the bot finish: M (#9) was opened by a person into another base, retargeted
        # onto the parent and closed. Its runs list this PR into the parent. Whoever opened M, a same-base PR is
        # another source unless the release App opened it and its base never changed: a stop like a red check
        moved = [{'event': 'base_ref_changed', 'created_at': '2026-11-20T12:00:00Z'}]
        for user, theirs, said in (('mallory', moved, 'opened by mallory, not the release App'),
                                   ('mallory', [], 'opened by mallory, not the release App'),
                                   (APP, moved, 'whose base was changed (2026-11-20T12:00:00Z)')):
            with self.subTest(user=user, events=len(theirs)):
                self.new_fixture()
                self.adjacent()
                self.checks(GREEN)
                write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps([self.same_base(9, user)]))
                write(os.path.join(self.fx.state, 'issue-events.json'), json.dumps({f'{REPO}#9': theirs}))
                r = self.up(auto=ACT)
                self.assert_open_and_untouched(r, 'did not come from its source in merge_up.finish.check_sources '
                                                  f'(#9 (closed) into {PARENT}, {said} also comes from {self.branch()}')
        # the App's, never retargeted, but its events could not be read: the bot waits, and the timeout stops it
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps([self.same_base(9, APP)]))
        write(os.path.join(self.fx.state, 'fail-issue-events-9'), '')
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, 'the required checks were not green')
        self.assertIn(f'(the events of #9, a PR of {self.branch()} into {PARENT} the release App opened, could not be read', r.stdout)
        # read: merged
        os.remove(os.path.join(self.fx.state, 'fail-issue-events-9'))
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')

    def test_checks_whose_source_cannot_be_read_wait(self):
        # the bot waits for them, and the timeout stops it
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'fail-check-runs'), '')
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, 'the required checks were not green')
        self.assertIn('after 0s (the check runs or the Actions runs of the head could not be read', r.stdout)

    def test_a_moved_head_is_not_merged(self):
        self.adjacent()
        self.checks(GREEN)
        # someone pushes to the PR branch after the bot opened it and before it reads the checks
        self.push_hook(self.branch(), {'foo.txt': 'extra\n'}, 'a push the bot did not make')
        # a wait long enough to show the stop comes at once, not when the wait runs out
        r = self.up(auto=ACT, env={'MERGE_UP_FINISH_WAIT_SECONDS': '5', 'MERGE_UP_FINISH_POLL_SECONDS': '1'})
        pushed = self.head()
        self.assert_open_and_untouched(r, f'the PR head is {pushed}, not the commit the bot pushed')
        self.assertNotIn('were not green', self.errors(r)[0])
        self.assertEqual(self.fx.gh_log().count(f'pr view 1 -R {REPO} --json state,headRefOid,statusCheckRollup'), 1)
        # the next run calls nothing on that branch resolved by rule: a conflict PR, never merged by the bot
        self.checks(GREEN)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{self.branch()} holds commits this run did not make', r.stdout)
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.bot_lines(r), [])

    def test_a_moved_parent_is_not_merged(self):
        self.adjacent()
        self.checks(GREEN)
        self.push_hook(PARENT, {'NOTES.txt': 'doc\n'}, 'the parent moves while the checks run')
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotEqual(self.parent(), self.parent_tip)
        self.assertEqual(len(self.errors(r)), 1)
        self.assertIn(f'bot-finished PR {PR1} was not merged: {PARENT} moved from {self.parent_tip[:7]} to {self.parent()[:7]} '
                      'while the checks ran', self.errors(r)[0])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')

    def test_a_parent_that_moves_just_before_the_merge_is_said(self):
        # 2c review, trust nit 3: a push to the parent between the ls-remote and the REST merge. GitHub merges
        # the checked head onto the new tip; the bot cannot undo that, so it says so and does not end red.
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'rest-merge-hook'), f"""set -e
d=$(mktemp -d)
git clone -q {self.fx.bare} "$d"
cd "$d"
git checkout -q {PARENT}
echo doc > NOTES.txt
git add NOTES.txt
git -c user.name=dev -c user.email=d@x commit -q -m 'the parent moves just before the merge'
git push -q origin {PARENT}
""")
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.up(auto=ACT, env={'MERGE_UP_SUMMARY': summary})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        tip = self.parent()
        moved = git(self.fx.bare, 'rev-parse', f'{tip}^1')
        self.assertNotEqual(moved, self.parent_tip)
        self.assertEqual(git(self.fx.bare, 'rev-parse', f'{moved}^1'), self.parent_tip)
        warnings = [l for l in r.stdout.splitlines() if l.startswith('::warning::')]
        self.assertEqual(warnings, [f'::warning::merge-up: PR {REPO}#1 was merged onto {moved} of {PARENT}, not onto '
                                    f'{self.parent_tip}, the tip its required checks vouched for: {PARENT} moved between the last '
                                    f'check and the merge. The merge stands; check {PARENT}'])
        with open(summary) as f:
            self.assertIn(f'- PR {PR1} was merged onto {moved[:7]} of {PARENT}, not onto {self.parent_tip[:7]}, the tip its required '
                          f'checks vouched for', f.read())
        # the parent that does not move: no warning
        self.new_fixture()
        self.adjacent()
        self.checks(GREEN)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('::warning::', r.stdout)

    def test_github_refusing_the_merge_leaves_the_pr_open(self):
        cases = [('405', 'Repository rule violations found', 'GitHub refused the merge (HTTP 405: Repository rule violations found)'),
                 ('409', 'Head branch was modified.', 'GitHub refused the merge (HTTP 409: Head branch was modified.)'),
                 ('422', 'Validation Failed', 'GitHub refused the merge (HTTP 422: Validation Failed)')]
        for i, (status, message, why) in enumerate(cases):
            with self.subTest(status=status):
                if i:
                    self.new_fixture()
                self.adjacent()
                self.checks(GREEN)
                write(os.path.join(self.fx.state, 'rest-merge-error.json'), json.dumps({'status': status, 'message': message}))
                r = self.up(auto=ACT)
                self.assert_open_and_untouched(r, f'was not merged: {why}. The PR stays open.', merged=1)
                self.assertNotIn('pr merge', self.fx.gh_log())
                self.assertNotIn('pr comment', self.fx.gh_log())

    def test_changes_requested_before_the_wait_stops_the_bot(self):
        head = self.waiting_pr()
        self.reviews((ALICE, 'CHANGES_REQUESTED'))
        mark = self.fx.gh_log()
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.errors(r), [
            f'::error::merge-up: a person said no to bot-finished PR {PR1} (alice requested changes), so the bot does not merge '
            f'{self.branch()}. The PR stays open. Once the review is dismissed or its author approves, the next merge-up run of {CHILD} '
            f'finishes it. Or approve the PR and dispatch release-merge-up.yml on the default branch with child={CHILD} pr=1. '
            f'A new commit on {CHILD} gets a branch of its own and runs as normal'])
        self.assertEqual(self.bot_lines(r), [f'not applied: a person said no to bot-finished PR {PR1} (alice requested changes). '
                                             'The bot does not merge it, and the PR stays open for people'])
        # nothing waited for and nothing said on the PR
        self.assertNotIn(f'pr view 1 -R {REPO} --json state', self.calls(mark))
        self.assertNotIn('pr comment', self.calls(mark))
        self.assertEqual((self.fx.prs()[0]['state'], self.parent()), ('OPEN', self.parent_tip))
        # the same person approving takes it back: the next run merges it
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'APPROVED'))
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git(self.fx.bare, 'rev-parse', self.parent() + '^2'), head)

    def test_changes_requested_during_the_wait_stops_the_bot(self):
        self.adjacent()
        self.checks(GREEN)
        reviews = os.path.join(self.fx.state, 'pr-1-rest-reviews.json')
        self.view_hook(f"""cat > {reviews} <<'JSON'
[{{"user": {json.dumps(ALICE)}, "state": "CHANGES_REQUESTED"}}]
JSON
""")
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('required checks green on', r.stdout)
        self.assertEqual(len(self.errors(r)), 1)
        self.assertIn(f'a person said no to bot-finished PR {PR1} (alice requested changes)', self.errors(r)[0])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')

    def test_reviews_that_cannot_be_read_stop_the_bot(self):
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'fail-reviews'), '')
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'a person said no to bot-finished PR {PR1} (its reviews could not be read (HTTP 502: Bad Gateway))', self.errors(r)[0])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')

    def test_a_pr_a_person_closed_before_the_run_is_a_dead_branch(self):
        self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        mark = self.fx.gh_log()
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.errors(r), [t.dead_error(self.branch(), 'alice closed it without merging', PR1)])
        for call in TOUCHES:
            self.assertNotIn(call, self.calls(mark))
        # and no write of any spelling (round 4 review, finding 2)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv(mark), allowed=()), [])
        self.assertEqual(self.bot_lines(r), [])

    def close_during_the_wait(self, state, by=None):
        prs = os.path.join(self.fx.state, 'prs.json')
        self.view_hook(f"""python3 - <<'PY'
import json
p = {prs!r}
d = json.load(open(p))
d[0].update(state={state!r}, closed_by={by!r})
json.dump(d, open(p, 'w'))
PY
""")

    def test_a_pr_a_person_closed_during_the_wait_is_a_dead_branch(self):
        self.adjacent()
        self.checks(GREEN)
        self.close_during_the_wait('CLOSED', ALICE)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        branch = self.branch()
        self.assertEqual(self.errors(r), [
            f'::error::merge-up: bot-finished PR {PR1} was not merged: a person closed {PR1} without merging (alice closed it without merging), '
            f'so {branch} is treated as closed. No run opens a pull request on {branch}, pushes to it or merges from it while it is closed. '
            f'Two ways forward: the next green commit on {CHILD} gets a new branch and a new PR by itself; or a person reopens {PR1} '
            '(the branch then counts as open again) and resolves or finishes it as usual, with a dispatch of release-merge-up.yml '
            f'on the default branch with child={CHILD} pr=1'])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        # and the next run agrees: the branch is dead
        r = self.up(auto=ACT)
        self.assertEqual(self.errors(r), [t.dead_error(branch, 'alice closed it without merging', PR1)])

    def test_a_pr_the_bot_closed_during_the_wait_is_not_a_dead_branch(self):
        self.adjacent()
        self.checks(GREEN)
        prs = os.path.join(self.fx.state, 'prs.json')
        bot = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}
        self.view_hook(f"""python3 - <<'PY'
import json
p = {prs!r}
d = json.load(open(p))
d[0].update(state='CLOSED', closed_by={bot!r})
d[0].setdefault('issue_comments', []).append({{'user': {bot!r}, 'body': 'Superseded by https://x/pull/9: moved on'}})
json.dump(d, open(p, 'w'))
PY
""")
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.errors(r), [
            f'::error::merge-up: bot-finished PR {PR1} was not merged: the PR is CLOSED, and not by a person (the bot superseded it, or '
            f'another PR on {self.branch()} is open), so {self.branch()} is not treated as closed. The next merge-up run of {CHILD} goes on as normal'])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def test_a_pr_someone_merged_during_the_wait(self):
        self.adjacent()
        self.checks(GREEN)
        self.close_during_the_wait('MERGED')
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(len(self.errors(r)), 1)
        self.assertIn(f'bot-finished PR {PR1} was not merged by the bot: the PR is MERGED, so someone merged it while the bot waited',
                      self.errors(r)[0])

    def test_a_commit_on_top_is_never_finished(self):
        self.waiting_pr(auto=ACT)   # the wait ran out: no check had reported
        extra = self.fx.commit(self.branch(), {'scripts-release/lib.sh': 'echo changed\n'}, 'one more change, not a rule')
        self.checks(GREEN)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.head(), extra)
        self.assertIn('starts with the commit this run computes, and 1 more commit(s) were pushed on top of it', r.stdout)
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.bot_lines(r), [])
        self.assertEqual(self.parent(), self.parent_tip)

    def test_a_forged_commit_with_the_bots_tree_is_never_finished(self):
        bot = self.waiting_pr(auto=ACT)
        email = git(self.fx.bare, 'log', '-1', '--format=%ce', bot)
        tree = git(self.fx.bare, 'rev-parse', bot + '^{tree}')
        forged = git(self.fx.bare, 'commit-tree', tree, '-p', self.parent_tip, '-p', self.child_tip, '-m',
                     f'Merge {CHILD} into {PARENT}\n\nMerge-Up-Rule: splice files=9 regions=9', env={'GIT_COMMITTER_EMAIL': email})
        git(self.fx.bare, 'update-ref', f'refs/heads/{self.branch()}', forged)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.head(), forged)
        self.assertIn("has the tree this run computes, but not the bot's committer address and message", r.stdout)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.bot_lines(r), [])

    def test_a_rerun_finishes_an_open_rule_resolved_pr_and_says_so_on_it(self):
        # the PR was opened before the word was set: its body asks for one approval
        head = self.waiting_pr()
        body = self.fx.prs()[0]['body']
        self.assertIn('This PR needs one approval', body)
        mark = self.fx.gh_log()
        # another day: this run's own commit has another sha, and the bot merges the one on origin
        later = '2026-01-02T00:00:00+00:00'
        r = self.up(auto=ACT, env={'GIT_AUTHOR_DATE': later, 'GIT_COMMITTER_DATE': later})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'{self.branch()} is already on origin at {head[:7]}: the commit this run computes, untouched', r.stdout)
        self.assertNotIn('pr create', self.calls(mark))
        self.assertNotIn('pr edit', self.calls(mark))
        self.assertEqual(t.merge_calls(self.fx.gh_argv(mark)), [bot_merge(head)])
        pr = self.fx.prs()[0]
        self.assertEqual((pr['state'], pr['merged_head'], pr['body']), ('MERGED', head, body))   # the body is not edited
        self.assertEqual(len(pr['comments']), 2)
        notice, merged = pr['comments']
        self.assertTrue(notice.startswith(f'`bot-finish` is in DS_MERGE_UP_AUTOMATION now, so release-merge-up merges this PR itself, '
                                          f'with no approval, once `pr-checks` succeed on `{head[:7]}`'), notice)
        self.assertIn('The description above says this PR needs one approval: that no longer holds.', notice)
        self.assertTrue(notice.endswith('\n\n' + MARK), notice)
        self.assertIn('with no approving review', merged)
        self.assertIn(f'commented on {PR1}: the bot now merges it itself', r.stdout)

    def test_the_notice_is_left_once(self):
        # a wait that runs out under the word: the next run says nothing more on the PR (its body says it),
        # and neither does a run after a notice
        self.waiting_pr(auto=ACT)
        self.checks([])
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('pr comment', self.fx.gh_log())
        self.assertNotIn('comments', self.fx.prs()[0])
        self.new_fixture()
        self.waiting_pr()
        self.checks([])
        for _ in range(2):
            r = self.up(auto=ACT)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(len(self.fx.prs()[0]['comments']), 1)

    def test_a_pr_still_open_after_the_merge_is_closed(self):
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'leave-open-after-merge'), '')
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'Merged by release-merge-up: `{PARENT}` holds this PR\'s head in the merge commit {self.parent()}', pr['close_comment'])

    def test_a_dry_run_says_the_bot_would_finish_it(self):
        self.adjacent()
        self.checks(GREEN)
        before = self.snapshot()
        r = self.up(auto=ACT, dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.bot_lines(r), [f'dry run: would then merge that PR with no approval once {CHECKS} succeed on its commit '
                                             '(a wait of at most 25 minutes)'])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertNotIn('pr view', self.fx.gh_log())


class RuleBotFinishShadowTests(BotFinishCase):
    """bot-finish not acting: the word absent from a list, the value shadow, or the word with splice
    not acting. It logs, at most one line per run, and nothing changes."""

    def test_a_list_without_the_word_logs_what_the_bot_would_do(self):
        self.adjacent()
        self.checks(GREEN)
        r = self.up(auto='splice')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.head()
        self.assertEqual(self.bot_lines(r), [f'shadow, nothing changes: would merge {PR1} with no approval once {CHECKS} succeed '
                                             f'on {head[:7]} (a wait of at most 25 minutes); it waits for one approval'])
        self.assertIn('bot finish:         shadow (log only: a rule-resolved PR waits for one approval)', r.stdout)
        pr = self.fx.prs()[0]
        self.assertIn('This PR needs one approval', pr['body'])
        self.assertNotIn('The bot merges this PR itself', pr['body'])
        self.assertNotIn(MARK, pr['body'])
        self.assertEqual(self.rule_lines(r)[-1], f'every conflict resolved by rule in {PR1} (1 file(s)); the PR has no markers and '
                                                 'waits for one approval')
        log = self.fx.gh_log()
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        for call in ('pr view 1', '/check-runs', 'pr comment', 'pr edit'):
            self.assertNotIn(call, log)
        self.assertEqual(self.parent(), self.parent_tip)

    def test_full_shadow_says_what_the_bot_would_do(self):
        self.adjacent()
        r = self.up(auto='shadow')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.bot_lines(r), [f'shadow, nothing changes: the rules would resolve every conflict here, and the bot would merge '
                                             f'the resulting PR with no approval once {CHECKS} succeed (unless a person said no to it or '
                                             'to a PR it supersedes)'])
        shadow = self.shape()
        # the variable absent: the same branches, files, PRs and body, and no bot-finish line
        self.new_fixture()
        self.adjacent()
        r = self.up(auto=None)
        self.assertEqual(self.bot_lines(r), [])
        self.assertEqual(self.shape(), shadow)

    def test_full_shadow_says_nothing_when_the_rules_would_leave_a_conflict(self):
        self.edits({'docs/notes.txt': NOTES_CHILD, 'foo.txt': 'child fix\n'}, {'docs/notes.txt': NOTES_PARENT, 'foo.txt': 'main fix\n'})
        r = self.up(auto='shadow')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.bot_lines(r), [])

    def test_full_shadow_with_an_empty_required_list_says_the_bot_would_not(self):
        self.adjacent()
        scripts = self.fx.scripts_copy({'finish.required_checks': [], 'finish.check_sources': {}})
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'shadow'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.bot_lines(r), ['shadow, nothing changes: the rules would resolve every conflict here; the bot would not merge '
                                             'the resulting PR: merge_up.finish.required_checks is empty, so no check would vouch for the commit'])

    def test_the_word_without_splice_acting_finishes_nothing(self):
        self.adjacent()
        self.checks(GREEN)
        r = self.up(auto='bot-finish')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('bot finish:         acting, but splice does not act, so no rule-resolved PR is made and nothing is finished', r.stdout)
        self.assertEqual(self.bot_lines(r), ['not applied: the rules would resolve every conflict here, but splice does not act, so there is '
                                             'no rule-resolved PR to finish'])
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assertEqual(self.parent(), self.parent_tip)

    def test_off_says_nothing_and_reads_nothing_more(self):
        # with the variable absent or off the rules do not run, so nothing of bot-finish runs
        self.adjacent()
        r = self.up(auto='splice')
        log_word_shadow = self.fx.gh_log()
        self.assertIn('--state all --limit 1000 --json number,state,headRefName,url', log_word_shadow)
        for auto in (None, 'off'):
            with self.subTest(auto=auto):
                self.new_fixture()
                self.adjacent()
                r = self.up(auto=auto)
                self.assertEqual(self.bot_lines(r), [])
                self.assertNotIn('--state all --limit 1000 --json number,state,headRefName,url', self.fx.gh_log())


class RuleBotFinishEligibilityTests(BotFinishCase):
    """Which rule-resolved PRs the word lets the bot merge (plan 1.5.3 a)."""

    def test_an_empty_required_list_waits(self):
        self.adjacent()
        self.checks(GREEN)
        scripts = self.fx.scripts_copy({'finish.required_checks': [], 'finish.check_sources': {}})
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': ACT})
        self.assert_waits(r, 'merge_up.finish.required_checks is empty, so no check would vouch for the commit')
        self.assertNotIn('pr view 1', self.fx.gh_log())

    def test_a_superseded_pr_with_changes_requested_makes_the_new_one_wait(self):
        # A person asked for changes on the rule's result for one child commit. The next child commit's
        # PR holds the same result: the bot must not merge it (2c review, trust 4), however many child
        # commits later, until a merge-up holds that commit.
        self.adjacent()
        r = self.up(auto=ACT)   # PR 1 waits: no check reported
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.reviews((ALICE, 'CHANGES_REQUESTED'))
        self.next_child_commit('y\n')
        self.checks(GREEN, number=2)
        r = self.up(auto=ACT)
        self.assert_waits(r, f'a person said no to {PR1}, which this PR supersedes (alice requested changes)', number=2)
        self.assertEqual(self.fx.prs()[0]['state'], 'CLOSED')   # superseded by the bot, as before
        # one more child commit: PR 1 is closed by the bot now, and its review still counts
        self.next_child_commit('z\n')
        self.checks(GREEN, number=3)
        r = self.up(auto=ACT)
        self.assert_waits(r, f'a person said no to {PR1}, which this PR supersedes (alice requested changes)', number=3)
        # the person takes it back: the bot merges the newest PR
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'DISMISSED'))
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[2]['state'], 'MERGED')

    def test_a_no_to_a_superseded_pr_during_the_wait_stops_the_bot(self):
        # 2c review, nit 2: the superseded PRs are read again after the wait, next to this PR's reviews. Run 2
        # reads PR 1 (no no), closes it as superseded, and waits; meanwhile a person requests changes on PR 1.
        self.adjacent()
        r = self.up(auto=ACT)   # PR 1 waits: no check reported
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.next_child_commit('y\n')
        self.checks(GREEN, number=2)
        refused = json.dumps([{'user': ALICE, 'state': 'CHANGES_REQUESTED'}])
        self.view_hook(f"""set -e
cat > {self.fx.state}/pr-1-rest-reviews.json <<'EOF_REVIEWS'
{refused}
EOF_REVIEWS
""")
        mark = self.fx.gh_log()
        r = self.up(auto=ACT)
        self.assert_open_and_untouched(r, f'a person said no to {PR1}, which this PR supersedes (alice requested changes), read again '
                                          'after the wait', number=2)
        self.assertEqual(self.calls(mark).count(f'api --paginate repos/{REPO}/pulls/1/reviews'), 2)
        # nobody said no: the same read after the wait lets it merge
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'DISMISSED'))
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[1]['state'], 'MERGED')

    def test_a_previous_branch_a_person_closed_makes_the_new_one_wait(self):
        self.adjacent()
        r = self.up(auto='splice')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.fx.close_pr(1, by=ALICE)
        self.next_child_commit('y\n')
        self.checks(GREEN, number=2)
        r = self.up(auto=ACT)
        self.assert_waits(r, f'a person said no to {PR1}, which this PR supersedes (alice closed it without merging)', number=2)
        # a branch the bot closed as superseded is nobody's no
        self.new_fixture()
        self.adjacent()
        self.up(auto='splice')
        self.fx.supersede_pr(1)
        self.next_child_commit('y\n')
        self.checks(GREEN, number=2)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[1]['state'], 'MERGED')

    def test_merge_up_prs_that_cannot_be_read_make_it_wait(self):
        self.adjacent()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'fail-pr-list-siblings'), '')
        r = self.up(auto=ACT)
        self.assert_waits(r, f'the merge-up PRs of {CHILD} in {REPO} could not be read (HTTP 502: Bad Gateway), so a person\'s no '
                             'to a PR this one supersedes cannot be ruled out')

    def test_a_pr_of_a_child_commit_the_parent_holds_is_not_superseded(self):
        # an old PR a person closed, whose child commit the parent took by hand since: it says
        # nothing about this PR
        self.adjacent()
        self.up(auto='splice')
        self.fx.close_pr(1, by=ALICE)
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', PARENT, f'origin/{PARENT}')
        git(self.fx.repo, 'merge', '-q', '--no-ff', '-s', 'ours', '-m', 'by hand', f'origin/{CHILD}')
        git(self.fx.repo, 'push', '-q', 'origin', PARENT)
        # then both lines move again into a new splice
        self.green_sha = self.child_tip = self.fx.commit(CHILD, {'docs/more.txt': NOTES.replace('b\n', 'B child\n')}, 'child again')
        write(self.fx.green, self.green_sha + '\n')
        self.parent_tip = self.fx.commit(PARENT, {'docs/more.txt': NOTES.replace('c\n', 'C parent\n')}, 'parent again')
        self.checks(GREEN, number=2)
        r = self.up(auto=ACT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[1]['state'], 'MERGED')


class BotFinishUnitTests(unittest.TestCase):
    """release_git.sources_state, merge_up_siblings and rules_would_resolve_all, and their
    commands (ENG-2235, 2c)."""

    # The scripts merge-up.sh runs on its path, beside itself: none of them holds a merge call or a GraphQL mutation.
    ON_THE_PATH = ('release_git.py', 'auto_finish.py', 'merge_rules.py', 'lines.py', 'lib.sh')

    @staticmethod
    def merge_sites(text):
        """(REST merge call sites, GraphQL sites) of a script, as [(function, line number)], on normalised text: each
        word of a line that is not a comment (a ${...} expansion read as one segment, quotes dropped as the shell
        joins them, then split on white space, =, parentheses and shell operators) read through api_parts, the
        normaliser the stub uses, so neither a prefix, the case of the host, :443, a leading /, #... nor ?... hides one.
        A REST merge site is a word that reads as repos/<...>/pulls/<n>/merge or repos/<...>/merges; a GraphQL site
        is a word that reads as graphql, or a line that says mutation."""
        fn, rest, graphql = None, [], []
        for no, line in enumerate(text.splitlines(), 1):
            m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\(\) \{', line)
            if m:
                fn = m.group(1)
            if line.lstrip().startswith('#'):
                continue
            bare = re.sub(r'\$\{[^}]*\}', 'X', line).replace('"', '').replace("'", '')
            words = [t.api_parts(w)[0] for w in re.split(r'[\s=()`;|&<>]+', bare) if w]
            if any(re.fullmatch(r'repos/\S+/pulls/[^/]+/merge|repos/\S+/merges', w) for w in words):
                rest.append((fn, no))
            if any(w.lower() == 'graphql' for w in words) or 'mutation' in line.lower():
                graphql.append((fn, no))
        return rest, graphql

    def test_merge_up_sh_merges_by_rest_only_in_bot_finish(self):
        # round 4 review, finding 2 (replacing the regex pin of the 2c re-review): merge-up.sh holds exactly one REST
        # merge call site, in bot_finish, however the endpoint is spelled, and no GraphQL call or mutation at all; the
        # scripts it runs hold none. The no-approval tests catch what this cannot see (a path built at run time).
        with open(MERGE_UP) as f:
            rest, graphql = self.merge_sites(f.read())
        self.assertEqual([f for f, _ in rest], ['bot_finish'], rest)
        self.assertEqual(graphql, [])
        for name in self.ON_THE_PATH:
            with self.subTest(script=name), open(os.path.join(SCRIPTS, name)) as f:
                self.assertEqual(self.merge_sites(f.read()), ([], []))
        # the spellings the round 4 review found, each a site of its own
        for line in ('gh api -X PUT "repos/$R/pulls/$n/merge#" -f merge_method=merge',
                     'gh api -X PUT "repos/$repo/pulls/${url##*/}/merge" -f sha="$head"',
                     'gh api -X PUT "repos/$repo/pulls/${url##*/}/mer""ge"',
                     'gh api -X PUT "https://API.GITHUB.COM/repos/$R/pulls/$n/merge"',
                     'gh api -X PUT "https://api.github.com:443/repos/$R/pulls/$n/merge?sha=$s"',
                     'out=$(gh api repos/$R/merges -f base=$PARENT -f head=$branch)',
                     'gh api --method=PUT /repos/$R/pulls/$n/merge'):
            with self.subTest(line=line):
                self.assertEqual(self.merge_sites(f'x() {{\n  {line}\n}}\n'), ([('x', 2)], []))
        for line in ('gh api /graphql -f query="$q"', 'gh api "graphql#" -f query="$q"', "gh api https://API.github.com:443/graphql -f q=1",
                     'q="Mutation{x}"'):
            with self.subTest(line=line):
                self.assertEqual(self.merge_sites(f'x() {{\n  {line}\n}}\n'), ([], [('x', 2)]))

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), *args], capture_output=True, text=True)

    def test_sources_state(self):
        # where each required check of a bot-finished head came from
        head, branch = 'a' * 40, 'merge-up/1.x.x/abcdef0'
        sources = {'pr-checks': {'workflow': '.github/workflows/pr-checks.yml'}, 'other': {'app': 'some-app'}}
        need = ['pr-checks', 'other']

        def run(i, name, status='completed', conclusion='success', slug='github-actions', suite=None):
            return {'id': i, 'name': name, 'head_sha': head, 'status': status, 'conclusion': conclusion,
                    'completed_at': '2026-11-20T11:40:00Z' if status == 'completed' else None, 'started_at': '2026-11-20T11:30:00Z',
                    'app': {'slug': slug}, 'check_suite': {'id': suite or 1000 + i}}
        repo, base = 'ds/repo', 'main'
        actions = [{'id': 20, 'path': '.github/workflows/pr-checks.yml', 'event': 'pull_request', 'head_branch': branch,
                    'head_sha': head, 'check_suite_id': 1001, 'head_repository': {'full_name': repo},
                    'pull_requests': [{'number': 1, 'base': {'ref': base}}]}]
        jobs = {'20': [{'id': 1, 'name': 'pr-checks'}]}
        good = [run(1, 'pr-checks'), run(2, 'other', slug='some-app')]

        alone = [{'number': 1, 'state': 'open', 'head': {'ref': branch}, 'base': {'ref': base}}]

        app = 'ds-os-release-bot[bot]'

        def state(check_runs, actions_runs=actions, jobs=jobs, pr=1, head_prs=alone, events=(), other_events=None):
            return release_git.sources_state(need, sources, branch, head, check_runs, actions_runs, jobs, pr=pr, base=base, repo=repo,
                                             head_prs=head_prs, events=events, release_app=app, other_events=other_events)
        self.assertEqual(state(good), ('green', ['pr-checks: .github/workflows/pr-checks.yml', 'other: the App some-app']))
        # red: posted by another App, or the newest run failed
        self.assertEqual(state([good[0], run(2, 'other', slug='someone-else')])[0], 'red')
        self.assertEqual(state([good[0], run(2, 'other', slug='some-app', conclusion='failure')])[0], 'red')
        # red: in the right run's check suite, but no job of it (posted through the checks API)
        self.assertEqual(state(good, jobs={'20': [{'id': 99, 'name': 'pr-checks'}]})[0], 'red')
        # pending: missing, still running, or the runs could not be read; red wins over pending
        self.assertEqual(state([good[0]]), ('pending', ['other: no check run of that name on the head aaaaaaa '
                                                        '(a commit status never counts)']))
        self.assertEqual(state([good[0], run(2, 'other', status='in_progress', conclusion=None)])[0], 'pending')
        self.assertEqual(state(None)[0], 'pending')
        self.assertEqual(state(good, jobs={})[0], 'pending')
        self.assertEqual(state([run(2, 'other', slug='someone-else')])[0], 'red')
        # 2c review, trust should-fix 1: another PR from the same branch into another base, newer and green,
        # while this PR's own run failed: red; with no run of this PR at all: pending (its own may yet come)
        theirs = dict(run(9, 'pr-checks', suite=1009), completed_at='2026-11-20T12:40:00Z')
        other = dict(actions[0], id=29, check_suite_id=1009, pull_requests=[{'number': 2, 'base': {'ref': 'x'}}])
        failed = [run(1, 'pr-checks', conclusion='failure'), theirs, good[1]]
        self.assertEqual(state(failed, actions_runs=actions + [other], jobs={**jobs, '29': [{'id': 9}]})[0], 'red')
        self.assertEqual(state([theirs, good[1]], actions_runs=[other], jobs={'29': [{'id': 9}]})[0], 'pending')
        # (#1, the App's PR of the branch into the same base and never retargeted, is then no other source; one nobody
        # shows the App opened is: red)
        self.assertEqual(state(good, pr=2, head_prs=[dict(alone[0], user={'login': app, 'type': 'Bot'})], other_events={'1': []})[0], 'pending')
        self.assertEqual(state(good, pr=2)[0], 'red')
        # 2c re-review, should-fix 1: another PR of the branch into another base (open or closed), or a base that was
        # changed: red, it does not go away by itself; the list or the events that could not be read: pending
        for state_ in ('open', 'closed'):
            other_pr = alone + [{'number': 2, 'state': state_, 'head': {'ref': branch}, 'base': {'ref': 'x'}}]
            self.assertEqual(state(good, head_prs=other_pr),
                             ('red', [f'#2 ({state_}) into x also comes from {branch}: GitHub lists this PR in the runs of every PR of the '
                                      f'branch, so no run vouches for #1 into {base} alone; a person finishes it']))
        self.assertEqual(state(good, events=[{'event': 'base_ref_changed'}])[0], 'red')
        # round 4 review, finding 1: another PR of the branch into this base is no other source only when the release
        # App opened it and its own events hold no base change (read: other_events); otherwise red, and unread pending
        earlier = dict(alone[0], number=0, state='closed', user={'login': app, 'type': 'Bot'})
        moved = [{'event': 'base_ref_changed'}]
        self.assertEqual(state(good, head_prs=alone + [earlier], other_events={'0': [{'event': 'closed'}]})[0], 'green')
        self.assertEqual(state(good, head_prs=alone + [earlier], other_events={'0': moved})[0], 'red')
        self.assertEqual(state(good, head_prs=alone + [dict(earlier, user={'login': 'mallory', 'type': 'User'})], other_events={'0': []})[0], 'red')
        self.assertEqual(state(good, head_prs=alone + [dict(earlier, user=None)], other_events={'0': []})[0], 'red')
        self.assertEqual(state(good, head_prs=alone + [earlier])[0], 'pending')
        for kw in ({'head_prs': None}, {'events': None}):
            self.assertEqual(state(good, **kw)[0], 'pending')

    def test_check_sources_cli(self):
        head, branch = 'a' * 40, 'merge-up/1.x.x/abcdef0'
        with tempfile.TemporaryDirectory() as tmp:
            def put(name, doc):
                path = os.path.join(tmp, name)
                write(path, json.dumps(doc))
                return path
            sources = put('sources.json', {'pr-checks': {'workflow': '.github/workflows/pr-checks.yml'}})
            runs = put('runs.json', {'total_count': 1, 'check_runs': [
                {'id': 1, 'name': 'pr-checks', 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
                 'started_at': '2026-11-20T11:30:00Z', 'completed_at': '2026-11-20T11:40:00Z',
                 'app': {'slug': 'github-actions'}, 'check_suite': {'id': 1001}}]})
            actions = put('actions.json', {'total_count': 1, 'workflow_runs': [
                {'id': 20, 'path': '.github/workflows/pr-checks.yml', 'event': 'pull_request', 'head_branch': branch,
                 'head_sha': head, 'check_suite_id': 1001, 'head_repository': {'full_name': 'ds/repo'},
                 'pull_requests': [{'number': 1, 'base': {'ref': 'main'}}]}]})
            os.makedirs(os.path.join(tmp, 'jobs'))
            put('jobs/20.json', {'total_count': 1, 'jobs': [{'id': 1, 'name': 'pr-checks'}]})
            prs = put('prs.json', [{'number': 1, 'state': 'open', 'head': {'ref': branch}, 'base': {'ref': 'main'}}])
            events = put('events.json', [])
            args = ('check-sources', '--required', 'pr-checks', '--sources', sources, '--head-ref', branch, '--head', head,
                    '--pr', '1', '--base', 'main', '--repo', 'ds/repo')
            reads = ('--head-prs', prs, '--events', events)
            r = self.cli(*args, '--check-runs', runs, '--actions-runs', actions, '--jobs-dir', os.path.join(tmp, 'jobs'), *reads)
            self.assertEqual((r.returncode, r.stdout), (0, 'green\npr-checks: .github/workflows/pr-checks.yml\n'), r.stderr)
            # 2c re-review, should-fix 1: another PR of the branch into another base is red; the list or the events
            # absent (could not be read) is pending
            other = put('other.json', [{'number': 1, 'head': {'ref': branch}, 'base': {'ref': 'main'}},
                                       {'number': 2, 'state': 'closed', 'head': {'ref': branch}, 'base': {'ref': 'x'}}])
            r = self.cli(*args, '--check-runs', runs, '--actions-runs', actions, '--jobs-dir', os.path.join(tmp, 'jobs'),
                         '--head-prs', other, '--events', events)
            self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'red'), r.stderr)
            for i in (0, 2):
                r = self.cli(*args, '--check-runs', runs, '--actions-runs', actions, '--jobs-dir', os.path.join(tmp, 'jobs'), *reads[i:i + 2])
                self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'pending'), r.stderr)
            # a run for another PR of the same branch vouches for nothing here: pending, when #1 is the release App's PR
            # of the branch into the same base and never retargeted (--release-app, --other-events); red otherwise
            # (round 4 review, finding 1)
            os.makedirs(os.path.join(tmp, 'other-events'))
            bot = 'ds-os-release-bot[bot]'   # the release App's login (GIT_BOT_NAME)
            bots = put('bots.json', [{'number': 1, 'state': 'open', 'user': {'login': bot, 'type': 'Bot'}, 'head': {'ref': branch},
                                      'base': {'ref': 'main'}}])
            two = (*args[:-6], '--pr', '2', '--base', 'main', '--repo', 'ds/repo', '--check-runs', runs, '--actions-runs', actions,
                   '--jobs-dir', os.path.join(tmp, 'jobs'), '--events', events)
            app = ('--release-app', bot, '--other-events', os.path.join(tmp, 'other-events'))
            # (without --other-events the App's PR's events count as unread: pending)
            for said, listed, extra in (('red', prs, app), ('red', bots, ()), ('red', bots, app[2:]), ('pending', bots, app[:2])):
                with self.subTest(said=said, extra=extra):
                    r = self.cli(*two, '--head-prs', listed, *extra)
                    self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, said), r.stderr)
            # its events: unread (no file) is pending; read and empty, #2 has no run of its own: pending; moved, red
            r = self.cli(*two, '--head-prs', bots, *app)
            self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'pending'), r.stderr)
            self.assertIn('the events of #1, a PR of', r.stdout)
            put('other-events/1.json', [])
            r = self.cli(*two, '--head-prs', bots, *app)
            self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'pending'), r.stderr)
            self.assertNotIn('the events of #1', r.stdout)
            put('other-events/1.json', [{'event': 'base_ref_changed'}])
            r = self.cli(*two, '--head-prs', bots, *app)
            self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'red'), r.stderr)
            # the runs could not be read: pending, which the wait goes on with
            r = self.cli(*args, '--jobs-dir', os.path.join(tmp, 'jobs'))
            self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, 'pending'))

    def test_merge_up_siblings(self):
        lines = ['9 OPEN merge-up/1.x.x/aaaaaaa u9',          # the PR this run opens or reuses: skipped
                 '8 CLOSED merge-up/1.x.x/bbbbbbb u8',
                 '7 OPEN merge-up/1.x.x/bbbbbbb u7',          # reopened: the open one speaks for the head
                 '6 MERGED merge-up/1.x.x/ccccccc u6',
                 '5 CLOSED merge-up/1.x.x/ddddddd u5',
                 '4 CLOSED merge-up/1.x.x/ddddddd u4',
                 '3 OPEN merge-up/1.0.x/eeeeeee u3',          # another child
                 '2 OPEN feature/x u2', 'not a line', '1 OPEN merge-up/1.x.x/notasha u1']
        want = [(7, 'OPEN', 'merge-up/1.x.x/bbbbbbb', 'u7'),
                (6, 'MERGED', 'merge-up/1.x.x/ccccccc', 'u6'),
                (5, 'CLOSED', 'merge-up/1.x.x/ddddddd', 'u5')]
        self.assertEqual(release_git.merge_up_siblings(lines, '1.x.x', 'merge-up/1.x.x/aaaaaaa'), want)
        self.assertEqual(release_git.merge_up_siblings([], '1.x.x', ''), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'list.txt')
            write(path, '\n'.join(lines) + '\n')
            r = self.cli('siblings', path, '--child', '1.x.x', '--skip', 'merge-up/1.x.x/aaaaaaa')
            self.assertEqual((r.returncode, r.stdout), (0, ''.join(f'{n} {s} {h} {u}\n' for n, s, h, u in want)))

    def test_rules_would_resolve_all(self):
        report = {'files': [{'path': 'a', 'would': 'resolve', 'never_auto': False, 'refused': False},
                            {'path': 'b', 'would': 'partial', 'never_auto': False, 'refused': False},
                            {'path': 'c', 'would': 'resolve', 'never_auto': True, 'refused': False},
                            {'path': 'd', 'would': 'resolve', 'never_auto': False, 'refused': True}]}
        would = release_git.rules_would_resolve_all
        self.assertTrue(would(report, ['a']))
        for listed in (['a', 'b'], ['c'], ['d'], ['a', 'e'], []):
            with self.subTest(listed=listed):
                self.assertFalse(would(report, listed))
        with tempfile.TemporaryDirectory() as tmp:
            rep, listed = os.path.join(tmp, 'report.json'), os.path.join(tmp, 'listed.txt')
            write(rep, json.dumps(report))
            write(listed, 'a\n')
            self.assertEqual(self.cli('rules-would-resolve', rep, '--listed', listed).returncode, 0)
            write(listed, 'a\nb\n')
            self.assertEqual(self.cli('rules-would-resolve', rep, '--listed', listed).returncode, 1)


if __name__ == '__main__':
    unittest.main()
