"""merge-up.sh and a merge-up branch a person closed (the owner's decision of 2026-11-18).

merge-up/<child>/<sha7> is dead when the most recent PR with that head against
the parent was closed without a merge by a person (anyone but the bot's own
supersession). On a dead branch a run opens no PR, pushes nothing to it,
merges nothing from it and applies no rule to it, whatever started the run.
Here: the ordinary conflict PR in both repos, the PR on the child commit (a
conflict without markers) and the rule-resolved PR. The lockfile pin rule's
own PRs are in test_lock_merge_up.py. Nothing here sets DS_MERGE_UP_AUTOMATION
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

MERGE_UP, DISTRO_REPO, META_REPO = t.MERGE_UP, t.DISTRO_REPO, t.META_REPO
git, write = t.git, t.write
CHILD, PARENT = 'vertex/1.x.x', 'vertex/main'
ALICE = {'login': 'alice', 'type': 'User'}
# however a run was started, the rule is the same
STARTS = ({'GITHUB_EVENT_NAME': 'workflow_run'}, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}, {})
TOUCHES = ('pr create', 'pr merge', 'pr close', 'pr comment', 'pr edit')
CLOSED_BY_ALICE = 'alice closed it without merging'


def url(number, repo=DISTRO_REPO):
    return f'https://github.com/{repo}/pull/{number}'


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
        """All a run can change: the refs of both repos and the PRs with their comments."""
        return self.fx.heads(self.fx.distro_bare), self.fx.heads(self.fx.meta_bare), self.fx.prs()

    def assert_dead(self, r, branch, why, pr=None, what='ds-tegra-distro', before=None):
        """Red like a conflict left for people, with one error that names the closed PR and the two
        ways forward; nothing opened, pushed, merged, closed, reopened or commented."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [t.dead_error(what, branch, why, pr)])
        calls = self.fx.gh_log()[len(self.mark):]
        for call in TOUCHES:
            self.assertNotIn(call, calls)
        self.assertNotIn(': merge rules: ', r.stdout)
        self.assertNotIn('lockfile pin rule: ', r.stdout)
        if before is not None:
            self.assertEqual(self.everything(), before)


class DeadConflictBranchTests(DeadCase):
    """The ordinary conflict PR, with the variable absent."""

    def closed(self):
        """Conflict PR 1 of the distro step, closed by a person. Returns its branch."""
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
                self.assertIn(f'ds-tegra-distro: a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                              f'No pull request is opened on {branch}, nothing is pushed to it or merged from it, and no merge-up rule is applied', r.stdout)
                with open(summary) as f:
                    self.assertIn(f'- ds-tegra-distro: a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                                  f'Nothing was opened, pushed, merged or resolved by rule on {branch}', f.read())
                os.remove(summary)

    def test_a_resolvers_work_on_a_dead_branch_is_left_alone(self):
        _, _, branch = self.conflict_pr()
        head = self.resolve(branch)
        self.fx.close_pr(1, by=ALICE)
        before = self.everything()
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(1), before=before)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[branch], head)

    def test_a_dead_branch_that_was_deleted_is_not_pushed_again(self):
        branch = self.closed()
        git(self.fx.distro_bare, 'update-ref', '-d', f'refs/heads/{branch}')
        for env in STARTS:
            self.assert_dead(self.up(env=env), branch, CLOSED_BY_ALICE, url(1))
            self.assertNotIn(branch, self.fx.heads(self.fx.distro_bare))

    def test_a_dry_run_prints_the_finding_and_what_it_would_not_do(self):
        branch = self.closed()
        before = self.everything()
        for env in STARTS:
            r = self.up(env=env, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'ds-tegra-distro: dry run: a person closed {url(1)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
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
            why = f'the pull requests of {branch} in {DISTRO_REPO} could not be read (HTTP 502: Bad Gateway), so nothing shows that no person closed one'
            self.assert_dead(self.up(env=env), branch, why, before=before)
            r = self.up(env=env, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'ds-tegra-distro: dry run: {why}, so {branch} is treated as closed. A real run would open no pull request on {branch}', r.stdout)
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
        for env in STARTS[:1]:
            r = self.up(env=env)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn('is treated as closed', r.stdout)
            self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
            self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()], [(1, 'CLOSED', branch), (2, 'OPEN', branch)])
            self.assertIn(f'conflicts (foo.txt); resolve {url(2)}', r.stdout)

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

    def test_an_older_pr_a_person_reopened_brings_the_branch_back(self):
        # a person closed PR 1, opened PR 2 on the same head by hand and closed that too: dead, by PR 2
        branch = self.closed()
        self.inject_pr(2, branch, self.fx.heads(self.fx.distro_bare)[branch])
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
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.fx.heads(self.fx.distro_bare)[PARENT] + '^2'), head)

    def test_a_new_child_commit_gets_a_new_branch_and_a_new_pr(self):
        old_branch = self.closed()
        old_head = self.fx.heads(self.fx.distro_bare)[old_branch]
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
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[old_branch], old_head)
        self.assertNotIn('pr close 1', self.fx.gh_log())
        self.assertNotIn('pr comment 1', self.fx.gh_log())

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
        self.assertIn(f'open conflict PR {DISTRO_REPO}#1 ({old_branch}) already contains {CHILD} at {new_child[:7]}; reusing it', r.stdout)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertEqual(self.everything(), before)   # nothing on the dead branch, nothing new
        self.assertEqual(before[2][1]['head'], new_branch)

    def test_a_clean_merge_has_no_branch_and_is_unaffected(self):
        self.child_work()
        branch = f'merge-up/{CHILD}/{self.green_sha[:7]}'
        self.inject_pr(1, branch, self.green_sha)
        self.fx.close_pr(1, by=ALICE)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertNotIn('--state all', self.fx.gh_log())   # no conflict: the branch's PRs are not even read
        tip = self.fx.heads(self.fx.distro_bare)[PARENT]
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), self.child_tip)


class DeadChildTipBranchTests(DeadCase):
    """A conflict that leaves no text markers: the PR is on the child commit itself."""

    def no_marker_pr(self):
        # modify/delete: the child deletes foo.txt, the parent edits it
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.distro, 'rm', '-q', 'foo.txt')
        git(self.fx.distro, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.distro, 'push', '-q', 'origin', CHILD)
        self.child_tip = git(self.fx.distro, 'rev-parse', 'HEAD')
        git(self.fx.distro, 'checkout', '-q', '--detach')
        write(self.fx.green, self.child_tip + '\n')
        self.fx.distro_commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        branch = f'merge-up/{CHILD}/{self.child_tip[:7]}'
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('leave no text markers', self.fx.prs()[0]['body'])
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[branch], self.child_tip)
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
        git(self.fx.distro_bare, 'update-ref', '-d', f'refs/heads/{branch}')
        self.assert_dead(self.up(env=STARTS[1]), branch, CLOSED_BY_ALICE, url(1))
        self.assertNotIn(branch, self.fx.heads(self.fx.distro_bare))
        # reopened (its branch back on the child commit), the run reuses it
        git(self.fx.distro_bare, 'update-ref', f'refs/heads/{branch}', self.child_tip)
        self.fx.reopen_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'conflicts (foo.txt); resolve {url(1)}', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])


class DeadMetaBranchTests(DeadCase):
    """The meta-dsensos step: its conflict PR lives in the layer repo, and the same rule holds there."""

    def meta_conflict(self):
        self.child_work(trailing_versions=False)
        self.m4 = self.fx.meta_commit(PARENT, 'main layer change\n', 'main layer change')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'::error::merge-up: meta-dsensos {CHILD} -> {PARENT} conflicts (layer.txt)', r.stdout)
        self.assertEqual([(p['number'], p['repo'], p['state']) for p in self.fx.prs()], [(1, META_REPO, 'OPEN')])
        return f'merge-up/{CHILD}/{self.m3[:7]}'

    def test_a_layer_conflict_pr_a_person_closed_is_dead_and_stops_before_the_distro_step(self):
        branch = self.meta_conflict()
        self.fx.close_pr(1, by=ALICE)
        before = self.everything()
        for env in STARTS:
            r = self.up(env=env)
            self.assert_dead(r, branch, CLOSED_BY_ALICE, url(1, META_REPO), what='meta-dsensos', before=before)
            self.assertNotIn('ds-tegra-distro:', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)[PARENT], self.m4)
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'meta-dsensos: dry run: a person closed {url(1, META_REPO)} without merging ({CLOSED_BY_ALICE}), so {branch} is treated as closed. '
                      f'A real run would open no pull request on {branch}', r.stdout)
        self.assertEqual(self.everything(), before)

    def test_what_cannot_be_read_in_the_layer_repo_counts_as_dead(self):
        branch = self.meta_conflict()
        write(os.path.join(self.fx.state, 'fail-pr-list-all'), '')
        why = f'the pull requests of {branch} in {META_REPO} could not be read (HTTP 502: Bad Gateway), so nothing shows that no person closed one'
        for env in STARTS:
            self.assert_dead(self.up(env=env), branch, why, what='meta-dsensos')

    def test_supersession_and_a_reopened_layer_pr_are_not_dead(self):
        branch = self.meta_conflict()
        self.fx.supersede_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()], [(1, 'CLOSED', branch), (2, 'OPEN', branch)])
        self.fx.close_pr(2, by=ALICE)
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(2, META_REPO), what='meta-dsensos')
        self.fx.reopen_pr(2)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'conflicts (layer.txt); resolve {url(2, META_REPO)}', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'OPEN')])

    def test_a_new_distro_green_commit_alone_meets_the_same_dead_layer_branch(self):
        # the layer branch is named after the tip of the layer's child line. A new green commit on
        # the ds-tegra-distro child line does not move that tip, so the run stops on the same dead
        # branch, and its error does not promise that such a commit gets a new branch
        branch = self.meta_conflict()
        self.fx.close_pr(1, by=ALICE)
        self.green_sha = self.fx.distro_commit(CHILD, {'docs/new.txt': 'distro work only\n'}, 'distro work only')
        write(self.fx.green, self.green_sha + '\n')
        self.assertNotEqual(self.green_sha[:7], self.m3[:7])
        before = self.everything()
        for env in STARTS:
            r = self.up(env=env)
            self.assert_dead(r, branch, CLOSED_BY_ALICE, url(1, META_REPO), what='meta-dsensos', before=before)
            self.assertNotIn(f'the next green commit on {CHILD} gets a new branch', r.stdout)
            self.assertIn(f'the next commit on meta-dsensos {CHILD} gets a new branch and a new PR by itself, at the first merge-up run of {CHILD} after it '
                          f'(the branch is named after the tip of meta-dsensos {CHILD}, so a new green commit on ds-tegra-distro {CHILD} that leaves that tip '
                          f'where it is stops here again); or a person reopens {url(1, META_REPO)}', r.stdout)
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'Two ways forward: the next commit on meta-dsensos {CHILD} gets a new branch', r.stdout)
        self.assertNotIn(f'the next green commit on {CHILD} gets a new branch', r.stdout)
        self.assertEqual(self.everything(), before)

    def test_an_older_layer_pr_a_person_reopened_brings_the_branch_back(self):
        branch = self.meta_conflict()
        self.fx.close_pr(1, by=ALICE)
        self.inject_pr(2, branch, self.fx.heads(self.fx.meta_bare)[branch], repo=META_REPO)
        self.fx.close_pr(2, by=ALICE)
        self.assert_dead(self.up(), branch, CLOSED_BY_ALICE, url(2, META_REPO), what='meta-dsensos')
        self.fx.reopen_pr(1)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertIn(f'conflicts (layer.txt); resolve {url(1, META_REPO)}', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN'), (2, 'CLOSED')])

    def test_a_new_layer_commit_on_the_child_gets_a_new_branch(self):
        old_branch = self.meta_conflict()
        self.fx.close_pr(1, by=ALICE)
        closed = self.fx.prs()[0]
        # the child line moves in the layer and pins it: a new sha7 in the layer repo
        self.m3 = self.fx.meta_commit(CHILD, 'm3 fix, again\n', 'another layer fix')
        self.green_sha = self.fx.distro_commit(CHILD, {'kas/include/base.lock.yml': t.LOCK_YML.format(pin=self.m3)}, 'pin the new layer fix')
        write(self.fx.green, self.green_sha + '\n')
        new_branch = f'merge-up/{CHILD}/{self.m3[:7]}'
        self.assertNotEqual(new_branch, old_branch)
        r = self.up(env=STARTS[0])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('is treated as closed', r.stdout)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['head']) for p in prs], [(1, 'CLOSED', old_branch), (2, 'OPEN', new_branch)])
        self.assertEqual(prs[0], closed)


class DeadRuleBranchTests(rules.RulesCase):
    """The rule-resolved PR (the splice): closed by a person, its branch is dead, and no rule is
    applied to the merge again. With the rules in shadow or off the same branch is just as dead."""

    adjacent = rules.SpliceMergeUpTests.adjacent
    resolved_pr = rules.SpliceMergeUpTests.resolved_pr

    def up(self, *args, **kw):
        self.mark = self.fx.gh_log()
        return rules.RulesCase.up(self, *args, **kw)

    def assert_dead(self, r, why=CLOSED_BY_ALICE, what='ds-tegra-distro', branch=None, pr=None):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [t.dead_error(what, branch or self.branch(), why, pr or url(1))])
        self.assertEqual(self.rule_lines(r), [])
        calls = self.fx.gh_log()[len(self.mark):]
        for call in TOUCHES:
            self.assertNotIn(call, calls)

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
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], bot)
        # a dry run: the finding, and no claim that the rules would resolve anything
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'so {self.branch()} is treated as closed. A real run would open no pull request on {self.branch()}', r.stdout)
        self.assertEqual(self.rule_lines(r), [])
        self.assertNotIn('would push', r.stdout)
        self.assertEqual(self.snapshot(), before)

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
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.fx.heads(self.fx.distro_bare)[PARENT] + '^2'), bot)

    def test_a_new_child_commit_gets_a_new_rule_resolved_pr(self):
        self.resolved_pr()
        old_branch = self.branch()
        self.fx.close_pr(1, by=ALICE)
        closed = self.fx.prs()[0]
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, {'child-only.txt': 'more child work\n'}, 'more child work')
        write(self.fx.green, self.green_sha + '\n')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        prs = self.fx.prs()
        self.assertEqual([(p['number'], p['state'], p['head']) for p in prs], [(1, 'CLOSED', old_branch), (2, 'OPEN', self.branch())])
        self.assertIn('(resolved by rule)', prs[1]['title'])
        self.assertEqual(prs[0], closed)

    def test_a_rule_resolved_layer_pr_a_person_closed_is_dead(self):
        recipe = rules.RECIPE
        self.meta_edits('recipes/app.bb', recipe.replace('DEPENDS += "x"', 'DEPENDS += "x y"'), recipe.replace('EXTRA = "1"\n', ''))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual((pr['repo'], pr['title']), (META_REPO, f'Merge-up {CHILD} into {PARENT} (resolved by rule)'))
        self.fx.close_pr(1, by=ALICE)
        for env in STARTS[:2]:
            self.assert_dead(self.up(env=env), what='meta-dsensos', branch=self.meta_branch(), pr=url(1, META_REPO))


if __name__ == '__main__':
    unittest.main()
