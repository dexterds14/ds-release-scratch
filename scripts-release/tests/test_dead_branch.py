"""merge-up.sh and a merge-up branch a person closed (the owner's decision of 2026-11-18).

merge-up/<child>/<sha7> is dead when the most recent PR with that head against
the parent was closed without a merge by a person (anyone but the bot's own
supersession). On a dead branch a run opens no PR, pushes nothing to it,
merges nothing from it and applies no rule to it, whatever started the run.
Here: the ordinary conflict PR, the PR on the child commit (a conflict without
markers) and the rule-resolved PR. Nothing here sets DS_MERGE_UP_AUTOMATION
except the rule-resolved cases: the rule is live without the variable.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import test_release_git as t  # noqa: E402  (the modules, not their classes: discovery would run them twice)
import test_rules_merge_up as rules  # noqa: E402

MERGE_UP, REPO = t.MERGE_UP, t.REPO
git, write = t.git, t.write
CHILD, PARENT = '1.x.x', 'main'
ALICE = {'login': 'alice', 'type': 'User'}
# however a run was started, the rule is the same
STARTS = ({'GITHUB_EVENT_NAME': 'workflow_run'}, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}, {})
TOUCHES = ('pr create', 'pr merge', 'pr close', 'pr comment', 'pr edit')
CLOSED_BY_ALICE = 'alice closed it without merging'
MERGE = 'merge:              '


def url(number):
    return f'https://github.com/{REPO}/pull/{number}'


class DeadCase(unittest.TestCase):
    """The MergeUpTests fixture and what a dead branch must look like. No test of its own."""

    child_work = t.MergeUpTests.child_work
    conflict_pr = t.MergeUpTests.conflict_pr
    child_moves = t.MergeUpTests.child_moves
    resolve = t.MergeUpTests.resolve
    approve = t.MergeUpTests.approve
    inject_pr = t.MergeUpTests.inject_pr

    def setUp(self):
        t.MergeUpTests.setUp(self)

    def up(self, *args, env=None, **kw):
        self.mark = self.fx.gh_log()   # what gh was asked before this run
        return self.fx.run(MERGE_UP, '--child', CHILD, *args, extra_env=dict(env or {}), **kw)

    def everything(self):
        """All a run can change: the refs of the repo and the PRs with their comments."""
        return self.fx.heads(), self.fx.prs()

    def assert_dead(self, r, branch, why, pr=None, before=None):
        """Red like a conflict left for people, with one error that names the closed PR and the two
        ways forward; nothing opened, pushed, merged, closed, reopened or commented."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [t.dead_error(branch, why, pr)])
        calls = self.fx.gh_log()[len(self.mark):]
        for call in TOUCHES:
            self.assertNotIn(call, calls)
        # and no write of any spelling (round 4 review, finding 2)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv(self.mark), allowed=()), [])
        self.assertNotIn('merge rules: ', r.stdout)
        if before is not None:
            self.assertEqual(self.everything(), before)


class DeadConflictBranchTests(DeadCase):
    """The ordinary conflict PR, with the variable absent."""

    def closed(self):
        """Conflict PR 1, closed by a person. Returns its branch."""
        _, self.parent_tip, branch = self.conflict_pr()
        self.fx.close_pr(1, by=ALICE)
        return branch

    def test_a_conflict_pr_a_person_closed_is_dead_for_every_run(self):
        branch = self.closed()
        before = self.everything()
        for env in STARTS:
            with self.subTest(env=env):
                summary = os.path.join(self.fx.top, 'summary.md')
                r = self.up(env=dict(env, MERGE_UP_SUMMARY=summary))
                self.assert_dead(r, branch, CLOSED_BY_ALICE, url(1), before=before)
                self.assertIn(f'{MERGE}a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                              f'No pull request is opened on {branch}, nothing is pushed to it or merged from it, and no merge-up rule is applied', r.stdout)
                with open(summary) as f:
                    self.assertIn(f'- merge: a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                                  f'Nothing was opened, pushed, merged or resolved by rule on {branch}', f.read())
                os.remove(summary)

    def test_the_error_names_the_default_branch_when_it_is_known(self):
        branch = self.closed()
        r = self.up(env={'DEFAULT_BRANCH': 'main'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'with a dispatch of release-merge-up.yml on the default branch (main) with child={CHILD} pr=1', r.stdout)
        self.assertIn(f'so {branch} is treated as closed', r.stdout)

    def test_a_resolvers_work_on_a_dead_branch_is_left_alone(self):
        _, _, branch = self.conflict_pr()
        head = self.resolve(branch)
        self.fx.close_pr(1, by=ALICE)
        before = self.everything()
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(1), before=before)
        self.assertEqual(self.fx.heads()[branch], head)

    def test_a_dead_branch_that_was_deleted_is_not_pushed_again(self):
        branch = self.closed()
        git(self.fx.bare, 'update-ref', '-d', f'refs/heads/{branch}')
        for env in STARTS:
            self.assert_dead(self.up(env=env), branch, CLOSED_BY_ALICE, url(1))
            self.assertNotIn(branch, self.fx.heads())

    def test_a_dry_run_prints_the_finding_and_what_it_would_not_do(self):
        branch = self.closed()
        before = self.everything()
        for env in STARTS:
            r = self.up(env=env, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'{MERGE}dry run: a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                          f'A real run would open no pull request on {branch}, push nothing to it, merge nothing from it and apply no merge-up rule to it, '
                          f'and would end with an error. Two ways forward: the next green commit on {CHILD} gets a new branch and a new PR by itself; '
                          f'or a person reopens {url(1)} (the branch then counts as open again) and resolves or finishes it as usual', r.stdout)
            self.assertIn('dry run: nothing pushed', r.stdout)
            self.assertNotIn('would push', r.stdout)
            self.assertNotIn('a real run leaves it as it is and reuses its PR', r.stdout)
            self.assertNotIn('::error::', r.stdout)
            self.assertEqual(self.everything(), before)

    def test_what_cannot_be_read_counts_as_dead_and_says_so(self):
        _, _, branch = self.conflict_pr()
        before = self.everything()
        for env in STARTS:
            # the PR list: nothing shows that no person closed one (PR 1 is in fact open)
            write(os.path.join(self.fx.state, 'fail-pr-list-all'), '')
            why = f'the pull requests of {branch} in {REPO} could not be read (HTTP 502: Bad Gateway), so nothing shows that no person closed one'
            self.assert_dead(self.up(env=env), branch, why, before=before)
            r = self.up(env=env, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'{MERGE}dry run: {why}, so {branch} is treated as closed. A real run would open no pull request on {branch}', r.stdout)
            os.remove(os.path.join(self.fx.state, 'fail-pr-list-all'))
        # who closed it: the bot's own supersession, if only it could be read
        self.fx.supersede_pr(1)
        write(os.path.join(self.fx.state, 'fail-issues'), '')
        for env in STARTS:
            self.assert_dead(self.up(env=env), branch, 'who closed it could not be read (HTTP 502: Bad Gateway)', url(1))
        os.remove(os.path.join(self.fx.state, 'fail-issues'))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()], [(1, 'CLOSED', branch), (2, 'OPEN', branch)])

    def test_the_bots_own_supersession_close_is_not_dead(self):
        _, _, branch = self.conflict_pr()
        self.fx.supersede_pr(1)
        r = self.up(env=STARTS[0])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()], [(1, 'CLOSED', branch), (2, 'OPEN', branch)])
        self.assertIn(f'conflicts (foo.txt); resolve {url(2)}', r.stdout)

    def test_a_pr_the_script_itself_superseded_is_not_dead(self):
        # here the close of PR 1 is made by merge-up.sh's own close_superseded, not written by the
        # test: the child moves and PR 2 replaces PR 1
        _, _, old_branch = self.conflict_pr()
        old_green = self.green_sha
        self.child_moves()
        self.up()
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'OPEN')])
        self.assertIn('Superseded by ', self.fx.prs()[0]['close_comment'])
        # the child line is back on the old green commit (and PR 2 gone): the old branch is needed
        # again, and the bot's own close of PR 1 does not make it dead
        self.fx.supersede_pr(2)
        git(self.fx.bare, 'update-ref', f'refs/heads/{CHILD}', old_green)
        write(self.fx.green, old_green + '\n')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertIn(f'{old_branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()][2:], [(3, 'OPEN', old_branch)])

    def test_a_close_that_only_looks_like_a_supersession_is_dead(self):
        bot = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}
        _, _, branch = self.conflict_pr()
        # a bot that closed it without the comment; a person whose comment borrows the words; nobody named
        self.fx.close_pr(1, by=bot)
        self.assert_dead(self.up(), branch, 'ds-os-release-bot[bot] closed it without merging', url(1))
        self.fx.close_pr(1, by=ALICE, comment='Superseded by my own PR')
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(1))
        self.fx.close_pr(1)
        self.assert_dead(self.up(), branch, 'someone GitHub does not name closed it without merging', url(1))
        # 2c review, trust nit 4: another bot (any branch workflow's github-actions[bot]) that closes it and leaves
        # close_superseded's words: a person's no, not the release App's supersession
        actions = {'login': 'github-actions[bot]', 'type': 'Bot'}
        self.fx.close_pr(1, by=actions, comment='Superseded by the clean merge: the child moved on. The branch is kept.')
        self.assert_dead(self.up(), branch, 'github-actions[bot] closed it without merging', url(1))

    def test_only_the_most_recent_pr_of_the_head_counts(self):
        # a person closed PR 1; the bot's supersession closed a later PR 2 on the same head: not dead
        _, _, branch = self.conflict_pr()
        self.fx.close_pr(1, by=ALICE)
        self.inject_pr(2, branch, self.fx.heads()[branch])
        self.fx.supersede_pr(2)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'CLOSED'), (3, 'OPEN')])
        # and the other way round: the bot's close first, a person's close last
        self.fx.close_pr(3, by=ALICE)
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(3))

    def test_an_older_pr_a_person_reopened_brings_the_branch_back(self):
        # a person closed PR 1, opened PR 2 on the same head by hand and closed that too: dead, by PR 2
        branch = self.closed()
        self.inject_pr(2, branch, self.fx.heads()[branch])
        self.fx.close_pr(2, by=ALICE)
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(2))
        # PR 1 reopened: the head has an open PR, whatever its number, so the branch is not dead
        self.fx.reopen_pr(1)
        before = self.everything()
        for env in STARTS:
            r = self.up(env=env)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn('is treated as closed', r.stdout)
            self.assertIn(f'conflicts (foo.txt); resolve {url(1)}', r.stdout)
            self.assertNotIn('pr create', self.fx.gh_log()[len(self.mark):])
            self.assertEqual(self.everything(), before)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN'), (2, 'CLOSED')])

    def test_a_reopened_pr_is_not_dead_and_finishes_as_usual(self):
        branch = self.closed()
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(1))
        # a finish dispatch on the closed PR is refused, as before
        self.approve(1)
        r = self.up('--pr', '1', env=STARTS[1])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR #1 is not an open merge-up/{CHILD}/* PR against {PARENT}', r.stdout)
        # a person reopens it: the next run reuses the PR, and its finish works as today
        self.fx.reopen_pr(1)
        os.remove(os.path.join(self.fx.state, 'pr-1-review.json'))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertIn(f'conflicts (foo.txt); resolve {url(1)}', r.stdout)
        self.assertNotIn('pr create', self.fx.gh_log()[len(self.mark):])
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        head = self.resolve(branch)
        self.approve(1)
        r = self.up('--pr', '1', env=STARTS[1])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        self.assertEqual(git(self.fx.bare, 'rev-parse', self.fx.heads()[PARENT] + '^2'), head)

    def test_a_new_child_commit_gets_a_new_branch_and_a_new_pr(self):
        old_branch = self.closed()
        old_head = self.fx.heads()[old_branch]
        self.assert_dead(self.up(), old_branch, CLOSED_BY_ALICE, url(1))
        closed = self.fx.prs()[0]
        # another open merge-up PR of the child, on a branch of its own: supersession still closes it
        self.inject_pr(2, f'merge-up/{CHILD}/0ld5ha7', old_head)
        new_child = self.child_moves()
        new_branch = f'merge-up/{CHILD}/{new_child[:7]}'
        r = self.up(env=STARTS[0])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['head']) for p in prs],
                         [(1, 'CLOSED', old_branch), (2, 'CLOSED', f'merge-up/{CHILD}/0ld5ha7'), (3, 'OPEN', new_branch)])
        self.assertIn(f'conflicts (foo.txt); resolve {url(3)}', r.stdout)
        self.assertIn(f'Superseded by {url(3)}', prs[1]['close_comment'])
        # the dead branch and its PR: not reopened, not commented on, not moved
        self.assertEqual(prs[0], closed)
        self.assertEqual(self.fx.heads()[old_branch], old_head)
        self.assertNotIn('pr close 1', self.fx.gh_log())
        self.assertNotIn('pr comment 1', self.fx.gh_log())

    def test_a_versions_bump_on_the_child_does_not_revive_a_dead_branch(self):
        # the branch is named after the green commit, and a [skip ci] bump on top is no new green commit
        branch = self.closed()
        self.fx.commit(CHILD, {'versions/ds': '1.3.0\n'}, 'release: pending 1.3.0 [skip ci]')
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(1))

    def test_an_open_pr_on_another_branch_that_holds_the_child_is_still_reused(self):
        # PR 1 on the old sha's branch; the child moves and PR 2 replaces it. The resolver would
        # rather keep PR 1: reopens it, merges the child into its branch, and closes PR 2 by hand.
        _, _, old_branch = self.conflict_pr()
        new_child = self.child_moves()
        r = self.up()
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'OPEN')])
        new_branch = self.fx.prs()[1]['head']
        self.fx.reopen_pr(1)
        self.resolve(old_branch, content='all three fixes\n', merge_child=True)
        self.fx.close_pr(2, by=ALICE)
        before = self.everything()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'open conflict PR {REPO}#1 ({old_branch}) already contains {CHILD} at {new_child[:7]}; reusing it', r.stdout)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertEqual(self.everything(), before)   # nothing on the dead branch, nothing new
        self.assertEqual(before[1][1]['head'], new_branch)

    def test_a_clean_merge_has_no_branch_and_is_unaffected(self):
        self.child_work()
        branch = f'merge-up/{CHILD}/{self.green_sha[:7]}'
        self.inject_pr(1, branch, self.green_sha)
        self.fx.close_pr(1, by=ALICE)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertNotIn('--state all', self.fx.gh_log())   # no conflict: the branch's PRs are not even read
        tip = self.fx.heads()[PARENT]
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), self.child_tip)

    def test_nothing_to_merge_is_unaffected(self):
        branch = self.closed()
        # the parent takes the child whole, by hand
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', PARENT, f'origin/{PARENT}')
        git(self.fx.repo, 'merge', '-q', '-s', 'ours', '--no-edit', f'origin/{CHILD}')
        git(self.fx.repo, 'push', '-q', 'origin', PARENT)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertNotIn(f'--head {branch} --state all', self.fx.gh_log()[len(self.mark):])


class DeadChildTipBranchTests(DeadCase):
    """A conflict that leaves no text markers: the PR is on the child commit itself."""

    def no_marker_pr(self):
        # modify/delete: the child deletes foo.txt, the parent edits it
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.repo, 'push', '-q', 'origin', CHILD)
        self.child_tip = git(self.fx.repo, 'rev-parse', 'HEAD')
        git(self.fx.repo, 'checkout', '-q', '--detach')
        write(self.fx.green, self.child_tip + '\n')
        self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        branch = f'merge-up/{CHILD}/{self.child_tip[:7]}'
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('Git cannot write conflict markers for the files marked above', self.fx.prs()[0]['body'])
        self.assertEqual(self.fx.heads()[branch], self.child_tip)
        return branch

    def test_the_pr_on_the_child_commit_a_person_closed_is_dead(self):
        branch = self.no_marker_pr()
        self.fx.close_pr(1, by=ALICE)
        before = self.everything()
        for env in STARTS:
            self.assert_dead(self.up(env=env), branch, CLOSED_BY_ALICE, url(1), before=before)
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'so {branch} is treated as closed. A real run would open no pull request on {branch}', r.stdout)
        self.assertNotIn('would push', r.stdout)
        # the branch gone as well: it is not pushed again
        git(self.fx.bare, 'update-ref', '-d', f'refs/heads/{branch}')
        self.assert_dead(self.up(env=STARTS[1]), branch, CLOSED_BY_ALICE, url(1))
        self.assertNotIn(branch, self.fx.heads())
        # reopened (its branch back on the child commit), the run reuses it
        git(self.fx.bare, 'update-ref', f'refs/heads/{branch}', self.child_tip)
        self.fx.reopen_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'conflicts (foo.txt); resolve {url(1)}', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])


class DeadRuleBranchTests(rules.RulesCase):
    """The rule-resolved PR (the splice): closed by a person, its branch is dead, and no rule is
    applied to the merge again. With the rules in shadow or off the same branch is just as dead."""

    adjacent = rules.SpliceMergeUpTests.adjacent
    resolved_pr = rules.SpliceMergeUpTests.resolved_pr

    def up(self, *args, **kw):
        self.mark = self.fx.gh_log()
        return rules.RulesCase.up(self, *args, **kw)

    def assert_dead(self, r, why=CLOSED_BY_ALICE, branch=None, pr=None):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [t.dead_error(branch or self.branch(), why, pr or url(1))])
        self.assertEqual(self.rule_lines(r), [])
        calls = self.fx.gh_log()[len(self.mark):]
        for call in TOUCHES:
            self.assertNotIn(call, calls)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv(self.mark), allowed=()), [])

    def test_a_rule_resolved_pr_a_person_closed_is_dead_for_every_run(self):
        bot = self.resolved_pr()
        self.assertIn('(resolved by rule)', self.fx.prs()[0]['title'])
        self.fx.close_pr(1, by=ALICE)
        before = self.snapshot()
        for env in STARTS:
            for auto in ('splice', 'shadow', None):
                with self.subTest(env=env, auto=auto):
                    self.assert_dead(self.up(env=env, auto=auto))
                    self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fx.heads()[self.branch()], bot)
        # a dry run: the finding, and no claim that the rules would resolve anything
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'so {self.branch()} is treated as closed. A real run would open no pull request on {self.branch()}', r.stdout)
        self.assertEqual(self.rule_lines(r), [])
        self.assertNotIn('would push', r.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_a_rule_resolved_pr_with_a_commit_on_top_is_dead_too(self):
        # the stop signal of 2026-11-17 (a person pushed on top) and then the close: nothing is retitled
        self.resolved_pr()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', self.branch(), f'origin/{self.branch()}')
        write(os.path.join(self.fx.repo, 'docs/notes.txt'), rules.NOTES_BOTH + 'and one more line\n')
        t.commit_all(self.fx.repo, 'a correction')
        git(self.fx.repo, 'push', '-q', 'origin', self.branch())
        self.fx.close_pr(1, by=ALICE)
        before = self.snapshot()
        self.assert_dead(self.up())
        self.assertEqual(self.snapshot(), before)
        self.assertIn('(resolved by rule)', self.fx.prs()[0]['title'])

    def test_a_reopened_rule_resolved_pr_is_reused_and_finished_with_one_approval(self):
        bot = self.resolved_pr()
        self.fx.close_pr(1, by=ALICE)
        self.assert_dead(self.up())
        self.fx.reopen_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'every conflict was resolved by rule in {url(1)}', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(git(self.fx.bare, 'rev-parse', self.fx.heads()[PARENT] + '^2'), bot)

    def test_a_new_child_commit_gets_a_new_rule_resolved_pr(self):
        self.resolved_pr()
        old_branch = self.branch()
        self.fx.close_pr(1, by=ALICE)
        closed = self.fx.prs()[0]
        self.green_sha = self.child_tip = self.fx.commit(CHILD, {'child-only.txt': 'more child work\n'}, 'more child work')
        write(self.fx.green, self.green_sha + '\n')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['head']) for p in prs], [(1, 'CLOSED', old_branch), (2, 'OPEN', self.branch())])
        self.assertIn('(resolved by rule)', prs[1]['title'])
        self.assertEqual(prs[0], closed)


if __name__ == '__main__':
    unittest.main()
