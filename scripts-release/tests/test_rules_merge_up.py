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
import subprocess
import sys
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
        extra = dict(env or {}, GIT_AUTHOR_DATE=FIXED_DATE, GIT_COMMITTER_DATE=FIXED_DATE)
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
        self.assertIn('dispatch release-merge-up.yml on the default branch', body)
        self.assertNotIn('<<<<<<<', body)
        self.assertNotIn('comments', prs[0])
        self.assertEqual(self.rule_lines(r), [
            'resolved docs/notes.txt (1 of 1 conflict); no markers left',
            f'every conflict resolved by rule in {prs[0]["url"]} (1 file(s)); the PR has no markers and waits for one approval'])
        self.assertIn(f'pushed {self.branch()}: {head[:7]}, {PARENT} plus the merge of {CHILD}, all 1 conflicted file(s) resolved by rule, no markers', r.stdout)
        self.assertIn(f'::error::merge-up: {CHILD} -> {PARENT}: every conflict was resolved by rule in {prs[0]["url"]}', r.stdout)
        self.assertIn(f'Then dispatch release-merge-up.yml with child={CHILD} pr=1 on the default branch\n', r.stdout)
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
        # rule-resolved PR is merged only by a pr=N run after a person's approval
        head = self.resolved_pr()
        green = {'reviewDecision': None, 'mergeStateStatus': 'CLEAN', 'reviews': [],
                 'statusCheckRollup': [{'__typename': 'CheckRun', 'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(green))
        before = self.snapshot()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('pr merge', self.fx.gh_log())
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertNotIn('pr merge', self.fx.gh_log())
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


if __name__ == '__main__':
    unittest.main()
