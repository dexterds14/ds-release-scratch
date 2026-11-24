"""auto-finish.sh, the sweep release-auto-finish.yml runs (ENG-2235), and the workflow file.

The MergeUpTests fixture of test_release_git.py (one bare repo, the stub gh): PR 1 is the
conflict PR of 1.x.x into main, resolved by `resolver`. The sweep runs from the fixture's
checkout on its default branch, main, with the real scripts and config.
"""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import auto_finish  # noqa: E402
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)
import test_finish as tf  # noqa: E402  (the same)

SWEEP = os.path.join(SCRIPTS, 'auto-finish.sh')
WORKFLOW = os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-auto-finish.yml')
REPO, CHILD, PARENT = t.REPO, tf.CHILD, tf.PARENT
# the name release-merge-up.yml's run-name gives the repo (github.event.repository.name), and another repo's
NAME, OTHER_NAME = REPO.split('/')[1], 'another-repo'
git, write = t.git, t.write
BOT = 'ds-os-release-bot[bot]'
T0, T1, T2 = '2026-11-20T10:00:00Z', '2026-11-20T11:00:00Z', '2026-11-20T12:00:00Z'
GREEN = [{'__typename': 'CheckRun', 'name': n, 'workflowName': 'PR checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
         for n in release_git.merge_up_config()['finish']['required_checks']]
WRITES = ('pr merge', 'pr comment', 'pr close', 'pr edit', 'pr create', 'api -X PUT', 'api -X POST', 'api -X PATCH',
          'api -X DELETE', 'api --method')
DISPATCH = (f'workflow run release-merge-up.yml --ref main -f child={CHILD} -f pr=1 -f dry_run=false -f auto_finish=true')


def review(login='alice', state='APPROVED', commit=None, at=T2):
    return {'user': {'login': login, 'type': 'User'}, 'state': state, 'commit_id': commit, 'submitted_at': at}


def push(login, kind='User', what='push', at=T1):
    return {'activity_type': what, 'actor': {'login': login, 'type': kind}, 'timestamp': at}


class SweepCase(tf.FinishCase):
    """A ready PR 1 (alice, write access, approved the head at T2 after resolver's push at T1)."""

    def setUp(self):
        super().setUp()
        self.ready_pr()

    def ready_pr(self, number=1, head=None):
        head = head or self.resolved_pr()
        self.view(number=number, statusCheckRollup=GREEN)
        self.reviews(review(commit=head), number=number)
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps([push(BOT, 'Bot', 'branch_creation', T0), push('resolver')]))
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'write'}))
        return head

    def clear_log(self):
        path = os.path.join(self.fx.state, 'log')
        if os.path.exists(path):
            os.remove(path)

    def sweep(self, mode='on', ref='main', script=SWEEP, **env):
        self.clear_log()
        extra = {'DEFAULT_BRANCH': 'main', 'GITHUB_REF_NAME': ref, 'GH_REPO': REPO,
                 'AUTO_FINISH_RETRY_SECONDS': '0', 'EVENT': 'schedule'}
        if mode is not None:
            extra['DS_MERGE_UP_AUTO_FINISH'] = mode
        extra.update(env)
        e = self.fx.env()
        e.pop('DS_MERGE_UP_AUTO_FINISH', None)
        e.update(extra)
        return t.subprocess.run([script], cwd=self.fx.repo, capture_output=True, text=True, env=e)

    def dispatches(self):
        return [l for l in self.fx.gh_log().splitlines() if l.startswith('workflow run')]

    def assert_quiet(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('::error::', r.stdout + r.stderr)
        self.assertEqual(self.dispatches(), [])

    def assert_writes_nothing(self):
        for line in self.fx.gh_log().splitlines():
            if line.startswith('workflow run'):
                continue
            for w in WRITES:
                self.assertFalse(line.startswith(w), line)
        # and no write of any spelling but the dispatch (round 4 review, finding 2), with the flags auto-finish.sh
        # gives it (round 5 review, finding 4)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv(), allowed=(t.GhWrite('workflow run', frozenset({'--ref', '-f'}), 1),)), [])


class SwitchAndRefTests(SweepCase):

    def test_off_reads_and_dispatches_nothing(self):
        for mode in (None, '', 'off', 'OFF', ' Off '):
            with self.subTest(mode=mode):
                r = self.sweep(mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn('auto-finish: DS_MERGE_UP_AUTO_FINISH is off; nothing was read or dispatched', r.stdout)
                self.assertEqual(self.fx.gh_log(), '')

    def test_a_bad_value_stops_before_any_read(self):
        for mode in ('true', 'shadow,on', 'yes'):
            with self.subTest(mode=mode):
                r = self.sweep(mode)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(r.stdout.strip(),
                                 f"::error::auto-finish: DS_MERGE_UP_AUTO_FINISH={mode!r}: not a mode; the accepted values are off, shadow and on "
                                 '(absent or empty is off; case and surrounding spaces are ignored); nothing was read or dispatched. '
                                 'Set the variable to off, shadow or on, or delete it')
                self.assertEqual(self.fx.gh_log(), '')

    def test_case_and_spaces(self):
        r = self.sweep(' Shadow ')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('mode:               shadow (logs what it would finish, changes nothing)', r.stdout)

    def test_another_ref_is_refused(self):
        r = self.sweep(ref=CHILD)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::auto-finish: runs on the default branch (main) only, not on {CHILD}', r.stdout)
        self.assertEqual(self.fx.gh_log(), '')
        r = self.sweep(ref='', DEFAULT_BRANCH='')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::auto-finish: DEFAULT_BRANCH is not set', r.stdout)
        self.assertEqual(self.fx.gh_log(), '')

    def test_no_repository_is_refused(self):
        r = self.sweep(GITHUB_REPOSITORY='')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::auto-finish: GITHUB_REPOSITORY is not set', r.stdout)
        self.assertEqual(self.fx.gh_log(), '')


class DispatchTests(SweepCase):

    def test_shadow_dispatches_nothing(self):
        r = self.sweep('shadow')
        self.assert_quiet(r)
        self.assertIn(f'auto-finish: {REPO}#1 {CHILD} -> {PARENT} at {self.pr_head[:7]}: ready: approved by alice after the last push, checks green', r.stdout)
        self.assertIn(f'auto-finish: {REPO}#1: shadow, nothing changes: would dispatch release-merge-up.yml on main with child={CHILD} pr=1 dry_run=false auto_finish=true', r.stdout)
        self.assertIn(f'started none (shadow; it would have started the finish of {REPO}#1)', r.stdout)
        self.assert_writes_nothing()

    def test_on_dispatches_the_finish_once(self):
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.sweep(AUTO_FINISH_SUMMARY=summary)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.dispatches(), [DISPATCH])
        self.assertIn(f'auto-finish: {REPO}#1: dispatched release-merge-up.yml on main with child={CHILD} pr=1 dry_run=false auto_finish=true '
                      f'(https://github.com/{REPO}/actions/runs/9001)', r.stdout)
        self.assertIn(f'1 open merge-up PR(s) looked at: 1 ready, 0 not ready, 0 skipped; started the finish of {REPO}#1', r.stdout)
        with open(summary) as f:
            text = f.read()
        self.assertTrue(text.startswith('### Merge-up auto-finish\n\n```\n== merge-up auto-finish\n'), text)
        self.assertIn(f'- {REPO}#1: dispatched release-merge-up.yml', text)
        # the dispatch names no repo: GH_REPO does
        self.assertNotIn(' -R ', self.dispatches()[0])
        with open(os.path.join(self.fx.state, 'dispatches.json')) as f:
            self.assertEqual(json.load(f)[0]['repo'], REPO)
        self.assert_writes_nothing()

    def test_the_sweep_reads_what_ready_needs(self):
        self.sweep()
        log = self.fx.gh_log()
        for call in (f'pr list -R {REPO} --state open --limit 1000 --json number,headRefName,baseRefName,headRefOid,isCrossRepository,isDraft',
                     f'api --paginate repos/{REPO}/pulls/1/reviews',
                     f'api --paginate -X GET repos/{REPO}/activity -f ref=refs/heads/{self.branch}',
                     f'api repos/{REPO}/collaborators/alice/permission --jq .permission',
                     f'api --paginate repos/{REPO}/issues/1/comments',
                     f'api repos/{REPO}/actions/workflows/release-merge-up.yml/runs?event=workflow_dispatch&per_page=100',
                     f'pr view 1 -R {REPO} --json number,state,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup,commits,headRefOid',
                     # where each required check came from (ENG-2235)
                     f'api --paginate repos/{REPO}/commits/{self.pr_head}/check-runs?per_page=100',
                     f'api --paginate repos/{REPO}/actions/runs?head_sha={self.pr_head}&per_page=100',
                     f'api --paginate repos/{REPO}/actions/runs/2000/jobs?filter=all&per_page=100'):
            self.assertIn(call, log)
        for status in auto_finish.ACTIVE:
            self.assertIn(f'api --paginate repos/{REPO}/actions/runs?status={status}&per_page=100', log)

    def test_a_failed_dispatch_is_red(self):
        write(os.path.join(self.fx.state, 'fail-workflow-run'), '')
        r = self.sweep()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::auto-finish: {REPO}#1: could not dispatch release-merge-up.yml on main with child={CHILD} pr=1 auto_finish=true: '
                      'could not create workflow dispatch event: HTTP 403', r.stdout)

    def test_an_unreadable_pr_list_is_red(self):
        write(os.path.join(self.fx.state, 'fail-pr-list-open'), '')
        r = self.sweep()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'::error::auto-finish: the open pull requests of {REPO} could not be listed (HTTP 502: Bad Gateway); nothing was dispatched', r.stdout)
        self.assertEqual(self.dispatches(), [])

    def test_a_branch_list_that_fails_is_quiet(self):
        # 2a review, nit 4: a failed git ls-remote is one line, nothing dispatched, green
        bare = self.fx.bare
        os.rename(bare, bare + '.away')
        try:
            r = self.sweep()
        finally:
            os.rename(bare + '.away', bare)
        self.assert_quiet(r)
        self.assertIn('auto-finish: the branches of origin could not be listed (', r.stdout)
        self.assertIn('so no PR\'s parent line can be checked; nothing was dispatched, and the next event or sweep looks again', r.stdout)
        self.assertNotIn('pr list', self.fx.gh_log())
        # the config that cannot be read stays red (here lib.sh, which reads it first, stops the run)
        scripts = self.fx.scripts_copy()
        with open(os.path.join(os.path.dirname(scripts), 'release', 'config.json'), 'w') as f:
            f.write('{')
        r = self.sweep(script=os.path.join(scripts, 'auto-finish.sh'))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::error::lib.sh: release/config.json is invalid', r.stdout)
        self.assertEqual(self.dispatches(), [])

    def second_pr(self):
        """PR 2: another open merge-up PR of the same lines, on a branch one commit above PR 1's head
        (the newest check run of a name on a commit is one branch's run: two PRs on one commit cannot
        both have theirs), ready too."""
        branch = f'merge-up/{CHILD}/1234567'
        tree = git(self.fx.bare, 'rev-parse', self.pr_head + '^{tree}')
        head2 = git(self.fx.bare, 'commit-tree', tree, '-p', self.pr_head, '-m', 'PR 2')
        git(self.fx.bare, 'branch', branch, head2)
        prs = self.fx.prs()
        prs.append({'number': 2, 'repo': REPO, 'base': PARENT, 'head': branch, 'title': 'x', 'body': '', 'state': 'OPEN',
                    'url': f'https://github.com/{REPO}/pull/2'})
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
        self.view(number=2, statusCheckRollup=GREEN)
        self.reviews(review(commit=head2), number=2)

    def test_two_ready_prs_one_dispatch(self):
        self.second_pr()
        r = self.sweep()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.dispatches(), [DISPATCH])
        self.assertIn(f'auto-finish: {REPO}#2: deferred: this run already started the finish of {REPO}#1; the next event or sweep takes it', r.stdout)
        self.assertIn('2 open merge-up PR(s) looked at: 2 ready, 0 not ready, 0 skipped', r.stdout)

    def test_the_hinted_pr_comes_first(self):
        self.second_pr()
        r = self.sweep(HINT_PR='2', EVENT='workflow_run')
        self.assertEqual(self.dispatches(), [DISPATCH.replace('pr=1', 'pr=2')])
        self.assertIn('trigger:            workflow_run, looking first at PR #2', r.stdout)
        r = self.sweep(HINT_HEAD=f'merge-up/{CHILD}/1234567')
        self.assertEqual(self.dispatches(), [DISPATCH.replace('pr=1', 'pr=2')])

    def test_unknown_merge_state_is_read_again(self):
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps([
            dict(reviewDecision='APPROVED', mergeStateStatus='UNKNOWN', statusCheckRollup=GREEN),
            dict(reviewDecision='APPROVED', mergeStateStatus='UNSTABLE', statusCheckRollup=GREEN + [
                {'__typename': 'CheckRun', 'name': 'review', 'workflowName': 'Merge-up review trigger', 'status': 'IN_PROGRESS', 'conclusion': ''}]),
            dict(reviewDecision='APPROVED', mergeStateStatus='CLEAN', statusCheckRollup=GREEN)]))
        r = self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH], r.stdout)
        self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith('pr view 1 ')), 3)
        # still UNKNOWN after the tries: not ready, quiet
        self.view(statusCheckRollup=GREEN, mergeStateStatus='UNKNOWN')
        r = self.sweep()
        self.assert_quiet(r)
        self.assertIn('not ready: merge-state-unknown (mergeStateStatus is UNKNOWN: GitHub has not computed it yet)', r.stdout)
        self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith('pr view 1 ')), 3)


class NotReadyTests(SweepCase):
    """A PR that is not ready is one line with its codes, and the run stays green."""

    def check(self, code, **env):
        r = self.sweep(**env)
        self.assert_quiet(r)
        line = [l for l in r.stdout.splitlines() if l.startswith(f'auto-finish: {REPO}#1 ')][0]
        self.assertIn(': not ready: ', line)
        self.assertIn(f'{code} (', line)
        self.assertIn('1 open merge-up PR(s) looked at: 0 ready, 1 not ready, 0 skipped; started none', r.stdout)
        self.assert_writes_nothing()
        return r

    def test_no_approval(self):
        self.reviews()
        self.check('no-approval')

    def test_a_stale_approval(self):
        self.reviews(review(commit=self.bot_commit))
        self.check('stale-approval')

    def test_approved_before_the_last_push(self):
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps([push(BOT, 'Bot', 'branch_creation', T0), push('resolver', at='2026-11-20T13:00:00Z')]))
        self.check('approved-before-push')

    def test_the_approver_cannot_write(self):
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'read'}))
        self.check('approver-cannot-write')

    def test_a_permission_that_cannot_be_read(self):
        # its own code, not approver-cannot-write (2a review, should-fix 1)
        os.remove(os.path.join(self.fx.state, 'permissions.json'))
        r = self.check('permission-unreadable')
        self.assertIn('permission-unreadable (the permission of alice could not be read)', r.stdout)
        self.assertNotIn('approver-cannot-write', r.stdout)

    def test_the_activity_cannot_be_read(self):
        os.remove(os.path.join(self.fx.state, 'activity.json'))
        self.check('activity-unreadable')
        # no commit is read for its author (the head's check runs, repos/R/commits/<sha>/check-runs, are another read)
        self.assertNotRegex(self.fx.gh_log(), r'repos/\S+/commits/[0-9a-f]{40}(\s|$)')

    def test_a_pending_check(self):
        self.view(statusCheckRollup=[dict(GREEN[0], status='IN_PROGRESS', conclusion='')])
        self.check('checks-pending')

    def test_changes_requested(self):
        self.reviews(review(commit=self.pr_head), review('bob', 'CHANGES_REQUESTED', commit=self.pr_head))
        self.check('changes-requested')

    def test_a_stop_comment(self):
        prs = self.fx.prs()
        prs[0]['issue_comments'] = [{'user': {'login': BOT, 'type': 'Bot'}, 'created_at': '2026-11-20T12:30:00Z',
                                     'body': 'stopped\n\n' + auto_finish.marker('stopped', self.pr_head)}]
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
        self.check('stopped')

    def test_comments_that_cannot_be_read(self):
        write(os.path.join(self.fx.state, 'fail-issues'), '')
        self.check('comments-unreadable')

    def test_a_finish_in_flight(self):
        write(os.path.join(self.fx.state, 'finish-runs.json'), json.dumps([
            {'id': 5, 'display_title': f'Merge-up {CHILD}: finish {NAME}#1 (auto-finish)', 'status': 'queued', 'conclusion': None,
             'created_at': '2026-11-20T12:10:00Z', 'html_url': 'https://example.invalid/5'}]))
        self.check('in-flight')

    def test_a_run_of_the_same_number_in_another_repo_is_not_this_prs(self):
        # 2a review, nit 3: runs are matched on (repo, number)
        write(os.path.join(self.fx.state, 'finish-runs.json'), json.dumps([
            {'id': 5, 'display_title': f'Merge-up {CHILD}: finish {OTHER_NAME}#1 (auto-finish)', 'status': 'queued', 'conclusion': None,
             'created_at': '2026-11-20T12:10:00Z', 'html_url': 'https://example.invalid/5'}] + [
            {'id': i, 'display_title': f'Merge-up {CHILD}: finish {OTHER_NAME}#1 (auto-finish)', 'status': 'completed', 'conclusion': 'failure',
             'created_at': f'2026-11-20T12:1{i}:00Z'} for i in range(3)]))
        r = self.sweep()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.dispatches(), [DISPATCH])

    def test_three_failed_runs(self):
        write(os.path.join(self.fx.state, 'finish-runs.json'), json.dumps([
            {'id': i, 'display_title': f'Merge-up {CHILD}: finish {NAME}#1 (auto-finish)', 'status': 'completed', 'conclusion': 'failure',
             'created_at': f'2026-11-20T12:1{i}:00Z'} for i in range(3)]))
        self.check('retries-exhausted')

    def test_a_required_check_from_elsewhere(self):
        # 2a review, trust should-fix 2: the rollup says pr-checks is green; the head's check runs say where it came from
        runs, actions = self.head_checks()
        # a commit status: no check run of the name
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([]))
        self.check('check-source-missing')
        # the PR checks workflow, run for another branch at the same commit
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps(runs))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps([dict(actions[0], head_branch='feature/x')]))
        self.check('check-source-missing')
        # 2c review, trust should-fix 1: a run of the PR checks workflow for another PR from the same branch into
        # another base, newer and green, while this PR's own run failed
        theirs = dict(runs[0], id=3999, check_suite={'id': 1999}, completed_at='2026-11-20T13:00:00Z')
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([dict(runs[0], conclusion='failure'), theirs]))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(
            actions + [dict(actions[0], id=2999, check_suite_id=1999, pull_requests=[{'number': 9, 'base': {'ref': 'x'}}])]))
        write(os.path.join(self.fx.state, 'jobs-2999.json'), json.dumps([{'id': 3999, 'name': 'pr-checks'}]))
        self.check('check-source-failed')
        # a run that lists no pull request, or comes from another head repository
        for other in ({'pull_requests': []}, {'head_repository': {'full_name': 'fork/repo'}}):
            with self.subTest(other=list(other)):
                write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps(runs))
                write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps([dict(actions[0], **other)]))
                self.check('check-source-missing')
        # pr-checks posted by an App, in a check suite of no Actions run: no check run of that name from this PR's runs
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(actions))
        write(os.path.join(self.fx.state, 'jobs-2000.json'), json.dumps([{'id': 3000, 'name': 'pr-checks'}]))
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([dict(runs[0], app={'slug': 'mallory-bot'}, check_suite={'id': 9999})]))
        self.check('check-source-missing')
        # back as it should be: ready
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps(runs))
        self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH])

    def test_another_pr_of_the_head_branch(self):
        # 2c re-review, should-fix 1: GitHub lists every open PR of the branch in each run of it, so a run another PR
        # of the branch (#9 into another base) started lists #1 too; nothing vouches while #9 is there, open or closed
        runs, actions = self.head_checks()
        theirs = dict(runs[0], id=3999, check_suite={'id': 1999}, completed_at='2026-11-20T13:00:00Z')
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([dict(runs[0], conclusion='failure'), theirs]))
        write(os.path.join(self.fx.state, 'jobs-2000.json'), json.dumps([{'id': 3000, 'name': 'pr-checks'}]))
        write(os.path.join(self.fx.state, 'jobs-2999.json'), json.dumps([{'id': 3999, 'name': 'pr-checks'}]))
        nine = {'number': 9, 'head': {'ref': self.branch}, 'base': {'ref': 'attacker'}}
        for state, listed in (('open', [nine, {'number': 1, 'base': {'ref': PARENT}}]), ('closed', [{'number': 1, 'base': {'ref': PARENT}}])):
            with self.subTest(other=state):
                write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(
                    actions + [dict(actions[0], id=2999, check_suite_id=1999, pull_requests=listed)]))
                write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps(
                    [{'number': 9, 'state': state, 'head': {'ref': self.branch, 'repo': {'full_name': REPO}}, 'base': {'ref': 'attacker'}}]))
                r = self.check('check-source-other-pr')
                self.assertIn(f'#9 ({state}) into attacker also comes from {self.branch}', r.stdout)
        owner = REPO.split('/')[0]
        for call in (f'api --paginate repos/{REPO}/pulls?state=all&head={owner}:{self.branch}&per_page=100',
                     f'api --paginate repos/{REPO}/issues/1/events?per_page=100'):
            self.assertIn(call, self.fx.gh_log())
        # an earlier PR of the branch into the same base, the release App's and never retargeted, is no other source:
        # with this PR's own run green, ready
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps(runs))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(actions))
        write(os.path.join(self.fx.state, 'head-prs-extra.json'), json.dumps([self.same_base(0, BOT)]))
        self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH])
        self.assertIn(f'api --paginate repos/{REPO}/issues/0/events?per_page=100', self.fx.gh_log())

    def same_base(self, n, user, state='closed'):
        """Another PR of PR 1's head branch into its base, as REST lists it, opened by user."""
        return {'number': n, 'state': state, 'user': {'login': user, 'type': 'Bot' if user.endswith('[bot]') else 'User'},
                'head': {'ref': self.branch, 'repo': {'full_name': REPO}}, 'base': {'ref': PARENT}}

    def test_a_same_base_pr_of_the_head_branch(self):
        # round 4 review, finding 1: M (#9) was opened by a person into another base, retargeted onto the parent and
        # closed; its newer green run lists #1 (reopened) into the parent, and #1's own run failed. Refused whoever
        # opened M: a person's same-base PR is another source; so is the App's once its base changed
        runs, actions = self.head_checks()
        theirs = dict(runs[0], id=3999, check_suite={'id': 1999}, completed_at='2026-11-20T13:00:00Z')
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([dict(runs[0], conclusion='failure'), theirs]))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(
            actions + [dict(actions[0], id=2999, check_suite_id=1999, pull_requests=[{'number': 1, 'base': {'ref': PARENT}}])]))
        write(os.path.join(self.fx.state, 'jobs-2000.json'), json.dumps([{'id': 3000, 'name': 'pr-checks'}]))
        write(os.path.join(self.fx.state, 'jobs-2999.json'), json.dumps([{'id': 3999, 'name': 'pr-checks'}]))
        extra = os.path.join(self.fx.state, 'head-prs-extra.json')
        events = os.path.join(self.fx.state, 'issue-events.json')
        moved = [{'event': 'base_ref_changed', 'created_at': '2026-11-20T12:00:00Z'}]
        for user, theirs_events, said in (('mallory', moved, 'opened by mallory, not the release App'),
                                          ('mallory', [], 'opened by mallory, not the release App'),
                                          (BOT, moved, 'whose base was changed (2026-11-20T12:00:00Z)'),
                                          (BOT, moved + moved, 'whose base was changed (2026-11-20T12:00:00Z, 2026-11-20T12:00:00Z)')):
            with self.subTest(user=user, events=len(theirs_events)):
                write(extra, json.dumps([self.same_base(9, user)]))
                write(events, json.dumps({f'{REPO}#9': theirs_events, f'{REPO}#1': [{'event': 'closed'}, {'event': 'reopened'}]}))
                r = self.check('check-source-other-pr')
                self.assertIn(f'#9 (closed) into {PARENT}, {said}', r.stdout)
                # a person's PR is refused without reading its events; the App's are read
                self.assertEqual(f'api --paginate repos/{REPO}/issues/9/events?per_page=100' in self.fx.gh_log(), user == BOT)
        # the App's, never retargeted: its run vouches (it ran the same checks against the same base); the newest is green
        write(events, json.dumps({f'{REPO}#9': [{'event': 'closed'}]}))
        write(extra, json.dumps([self.same_base(9, BOT)]))
        self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH])
        # its events could not be read: not ready, no mark
        write(os.path.join(self.fx.state, 'fail-issue-events-9'), '')
        r = self.check('check-source-unreadable')
        self.assertIn(f'the events of #9, a PR of {self.branch} into {PARENT} the release App opened, could not be read', r.stdout)

    def test_a_retargeted_pr_and_reads_that_fail(self):
        # 2c re-review, should-fix 1: a base that was changed (and back) at any time, or a read that failed: not ready, no mark
        write(os.path.join(self.fx.state, 'issue-events.json'), json.dumps({f'{REPO}#1': [{'event': 'base_ref_changed'}]}))
        self.check('check-source-base-changed')
        os.remove(os.path.join(self.fx.state, 'issue-events.json'))
        for flag in ('fail-head-prs', 'fail-issue-events'):
            with self.subTest(flag=flag):
                write(os.path.join(self.fx.state, flag), '')
                self.check('check-source-unreadable')
                os.remove(os.path.join(self.fx.state, flag))
        self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH])

    def head_checks(self):
        """The stub's default check runs and Actions runs of PR 1's head: every required check from its source."""
        runs, actions = [], []
        for i, (name, kind, value) in enumerate((('pr-checks', 'workflow', '.github/workflows/pr-checks.yml'),)):
            runs.append({'id': 3000 + i, 'name': name, 'head_sha': self.pr_head, 'status': 'completed', 'conclusion': 'success',
                         'completed_at': T1, 'app': {'slug': 'github-actions' if kind == 'workflow' else value}, 'check_suite': {'id': 1000 + i}})
            if kind == 'workflow':
                actions.append({'id': 2000 + i, 'path': value, 'event': 'pull_request', 'head_branch': self.branch,
                                'head_sha': self.pr_head, 'check_suite_id': 1000 + i, 'head_repository': {'full_name': REPO},
                                'pull_requests': [{'number': 1, 'base': {'ref': PARENT}}]})
        return runs, actions

    def test_the_head_checks_that_cannot_be_read(self):
        for flag in ('fail-check-runs', 'fail-head-runs'):
            with self.subTest(flag=flag):
                write(os.path.join(self.fx.state, flag), '')
                self.check('check-runs-unreadable')
                os.remove(os.path.join(self.fx.state, flag))
        # the jobs of the PR checks run
        runs, actions = self.head_checks()
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(actions))
        r = self.check('check-runs-unreadable')
        self.assertIn('the jobs of the .github/workflows/pr-checks.yml run 2000 could not be read', r.stdout)

    def test_runs_that_cannot_be_read(self):
        write(os.path.join(self.fx.state, 'fail-finish-runs'), '')
        self.check('runs-unreadable')

    def test_reviews_that_cannot_be_read(self):
        write(os.path.join(self.fx.state, 'fail-reviews'), '')
        r = self.sweep()
        self.assert_quiet(r)
        self.assertIn(f'{REPO}#1 {CHILD} -> {PARENT} at {self.pr_head[:7]}: not looked at: its reviews could not be read (HTTP 502: Bad Gateway)', r.stdout)

    def test_a_base_that_is_not_the_parent(self):
        prs = self.fx.prs()
        prs[0]['base'] = CHILD
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
        r = self.sweep()
        self.assert_quiet(r)
        self.assertIn(f'skipped: base {CHILD} is not the parent line of {CHILD} ({PARENT})', r.stdout)
        self.assertIn('0 ready, 0 not ready, 1 skipped', r.stdout)

    def test_a_cross_repository_or_draft_pr_is_skipped(self):
        for key, why in (('cross', f'head merge-up/{CHILD}/'), ('draft', 'a draft')):
            with self.subTest(key=key):
                prs = self.fx.prs()
                prs[0].pop('cross', None)
                prs[0].pop('draft', None)
                prs[0][key] = True
                write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
                r = self.sweep()
                self.assert_quiet(r)
                self.assertIn(f'auto-finish: {REPO}#1: skipped: {why}', r.stdout)
                self.assertIn('0 open merge-up PR(s) looked at', r.stdout)


class BusyTests(SweepCase):

    def test_a_busy_group_defers(self):
        for wf in ('release-merge-up.yml', 'release-promote.yml', 'release-abandon.yml', 'release-create-branch.yml'):
            for status in ('queued', 'in_progress', 'pending', 'waiting'):
                with self.subTest(wf=wf, status=status):
                    write(os.path.join(self.fx.state, 'active-runs.json'), json.dumps([
                        {'id': 77, 'path': f'.github/workflows/{wf}', 'status': status, 'html_url': 'https://example.invalid/77'}]))
                    r = self.sweep()
                    self.assert_quiet(r)
                    self.assertIn(f'auto-finish: {REPO}#1: deferred: the release-line group is busy ({wf} run https://example.invalid/77 is {status}); '
                                  'the next event or sweep takes it', r.stdout)
                    self.assertIn('started none (the release-line group is busy)', r.stdout)

    def test_a_run_on_another_ref_counts(self):
        # a rollback dispatch on an older line runs that line's copy of the file: busy all the same
        write(os.path.join(self.fx.state, 'active-runs.json'), json.dumps([
            {'id': 78, 'path': '.github/workflows/release-promote.yml@refs/heads/1.x.x', 'head_branch': '1.x.x', 'status': 'queued'}]))
        r = self.sweep()
        self.assert_quiet(r)
        self.assertIn('deferred: the release-line group is busy (release-promote.yml run id 78 is queued)', r.stdout)

    def test_active_runs_that_cannot_be_read(self):
        write(os.path.join(self.fx.state, 'fail-active-runs'), '')
        r = self.sweep()
        self.assert_quiet(r)
        self.assertIn('deferred: the release-line group is busy (the active runs (queued) could not be read: gh: Bad Gateway (HTTP 502))', r.stdout)

    def test_other_runs_are_not_the_group(self):
        write(os.path.join(self.fx.state, 'active-runs.json'), json.dumps([
            {'id': 1, 'path': '.github/workflows/release-rc-build.yml', 'status': 'in_progress'},
            {'id': 2, 'path': '.github/workflows/release-auto-finish.yml', 'status': 'in_progress'},
            {'id': 3, 'path': '.github/workflows/release-merge-up.yml', 'status': 'completed'}]))
        self.sweep()
        self.assertEqual(self.dispatches(), [DISPATCH])


class WorkflowTests(unittest.TestCase):
    """release-auto-finish.yml. Text checks: the suite is standard library only."""

    def setUp(self):
        with open(WORKFLOW) as f:
            self.text = f.read()

    def test_triggers(self):
        self.assertIn('  workflow_run:\n    workflows: ["Merge-up review trigger", "PR checks"]\n'
                      '    types: [completed]\n    branches: [\'merge-up/**\']\n', self.text)
        self.assertIn("  schedule:\n    - cron: '23 * * * *'\n", self.text)
        self.assertIn('  workflow_dispatch:\n', self.text)
        on = self.text[self.text.index('\non:\n'):self.text.index('\npermissions:')]
        self.assertEqual(re.findall(r'^  ([a-z_]+):', on, re.M), ['workflow_run', 'schedule', 'workflow_dispatch'])
        # the review trigger with any conclusion, a check workflow with success
        self.assertIn("github.event.workflow_run.path == '.github/workflows/release-merge-up-review.yml' ||\n"
                      "       github.event.workflow_run.conclusion == 'success')", self.text)
        # the names it listens for are the workflows' own
        root = os.path.dirname(SCRIPTS)
        for path, name in (('release-merge-up-review.yml', 'Merge-up review trigger'), ('pr-checks.yml', 'PR checks')):
            with open(os.path.join(root, '.github/workflows', path)) as f:
                self.assertEqual(f.readline(), f'name: {name}\n', path)

    def test_one_group_and_read_only(self):
        self.assertIn('concurrency:\n  group: release-auto-finish\n  cancel-in-progress: false\n', self.text)
        self.assertIn('permissions: {}\n', self.text)
        job = self.text[self.text.index('    permissions:\n'):self.text.index('    steps:\n')]
        perms = re.findall(r'^      ([a-z-]+): (\w+)$', job, re.M)
        self.assertEqual(dict(perms), {'contents': 'read', 'pull-requests': 'read', 'checks': 'read', 'statuses': 'read',
                                       'issues': 'read', 'actions': 'write'})
        self.assertNotIn('write', self.text.replace('actions: write', '').split('jobs:')[1].replace('# Reads only', ''))

    def test_no_app_token_and_no_secret(self):
        # GITHUB_TOKEN reads and dispatches; the finish's own job mints the token that merges
        self.assertNotIn('create-github-app-token', self.text)
        self.assertNotIn('secrets.', self.text)
        self.assertIn('GH_TOKEN: ${{ github.token }}\n', self.text)

    def test_the_switch_and_the_script(self):
        self.assertIn("vars.DS_MERGE_UP_AUTO_FINISH != '' && vars.DS_MERGE_UP_AUTO_FINISH != 'off'", self.text)
        self.assertIn('DS_MERGE_UP_AUTO_FINISH: ${{ vars.DS_MERGE_UP_AUTO_FINISH }}\n', self.text)
        self.assertIn('DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n', self.text)
        self.assertIn('./scripts-release/auto-finish.sh\n', self.text)
        # PR text never reaches the shell: hints go through env only
        run = self.text[self.text.index('        run: |\n'):]
        self.assertNotIn('${{', run)

    def test_the_script_dispatches_only_the_finish(self):
        with open(SWEEP) as f:
            text = f.read()
        self.assertEqual(re.findall(r'gh workflow run [^\n]*', text),
                         ['gh workflow run release-merge-up.yml --ref <default> -f child=<child>',
                          'gh workflow run "$FINISH_WORKFLOW" --ref "$DEFAULT_BRANCH" -f child="$child" -f pr="$n" -f dry_run=false -f auto_finish=true 2>&1) \\'])
        self.assertNotIn(' -R ', '\n'.join(l for l in text.splitlines() if 'workflow run' in l))
        for w in ('gh pr merge', 'gh pr comment', 'gh pr close', 'gh pr edit', 'gh pr create', 'git push', '-X POST', '-X PUT', '-X PATCH', '-X DELETE'):
            self.assertNotIn(w, text)

    def test_the_merge_up_workflow_never_reads_the_variable(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up.yml')) as f:
            self.assertNotIn('DS_MERGE_UP_AUTO_FINISH', f.read())
        with open(os.path.join(SCRIPTS, 'merge-up.sh')) as f:
            self.assertNotIn('DS_MERGE_UP_AUTO_FINISH', f.read())


class MergeUpWorkflowTests(unittest.TestCase):
    """release-merge-up.yml takes auto_finish from release-auto-finish.yml (ENG-2235): no new job,
    no new trigger, the flag only on a dispatch, and a run-name the sweep reads back."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up.yml')) as f:
            self.text = f.read()

    def test_the_input(self):
        inputs = self.text[self.text.index('  workflow_dispatch:\n'):self.text.index('\npermissions:\n')]
        self.assertIn('      auto_finish:\n', inputs)
        block = inputs[inputs.index('      auto_finish:\n'):]
        self.assertIn('        type: boolean\n        default: false\n', block)

    def test_only_a_dispatch_passes_the_flag(self):
        merge = t.MergeUpWorkflowTests.step(self, 'Merge up')
        self.assertIn("AUTO_FINISH: ${{ github.event_name == 'workflow_dispatch' && inputs.auto_finish || false }}\n", merge)
        self.assertIn('[ "${AUTO_FINISH}" != true ] || args+=(--auto-finish)\n', merge)
        self.assertEqual(self.text.count('--auto-finish'), 2)   # the header comment and that line
        self.assertEqual(self.text.count('inputs.auto_finish'), 2)   # the env line and run-name

    def test_no_new_job_or_trigger(self):
        self.assertEqual(re.findall(r'^  ([a-z-]+):\n', self.text[self.text.index('\njobs:\n'):], re.M),
                         ['resolve', 'merge', 'merge-up-skipped'])
        on = self.text[self.text.index('\non:\n'):self.text.index('\npermissions:\n')]
        self.assertEqual(re.findall(r'^  ([a-z_]+):', on, re.M), ['workflow_run', 'workflow_dispatch'])
        self.assertNotIn('DS_MERGE_UP_AUTO_FINISH', self.text)

    def test_the_run_name_says_pr_and_auto(self):
        line = [l for l in self.text.splitlines() if l.startswith('run-name: ')][0]
        self.assertIn("format('Merge-up {0}{1}{2}', inputs.child || github.event.workflow_run.head_branch, "
                      "inputs.pr && format(': finish {0}#{1}', github.event.repository.name, inputs.pr) || '', "
                      "inputs.auto_finish && ' (auto-finish)' || '')", line)
        # what GitHub makes of it for a dispatched auto-finish is what the sweep counts: repo and number
        title = lambda repo, pr, auto: 'Merge-up {0}{1}{2}'.format(CHILD, ': finish {0}#{1}'.format(repo, pr), ' (auto-finish)' if auto else '')
        self.assertEqual(auto_finish._auto_pr({'display_title': title(NAME, 12, True)}), (NAME, 12))
        self.assertIsNone(auto_finish._auto_pr({'display_title': title(NAME, 12, False)}))
        self.assertIsNone(auto_finish._auto_pr({'display_title': f'Merge-up {CHILD}'}))
        # the old run-name, without the repo, is no longer counted as one
        self.assertIsNone(auto_finish._auto_pr({'display_title': f'Merge-up {CHILD}: finish PR #12 (auto-finish)'}))
        # the repo comes from the event, never written out (2a review, nit 3)
        self.assertNotIn('inputs.pr_repo', self.text)
        self.assertEqual(line.count('github.event.repository.name'), 1)

    def test_the_finish_can_read_where_a_check_came_from(self):
        # the gate reads the head's check runs (checks: read) and Actions runs and jobs (actions: read),
        # with the App token or, without it, GITHUB_TOKEN
        top = self.text[self.text.index('\npermissions:\n'):self.text.index('\njobs:\n')]
        self.assertEqual(dict(re.findall(r'^  ([a-z-]+): (\w+)$', top, re.M)),
                         {'contents': 'write', 'pull-requests': 'write', 'actions': 'read', 'checks': 'read'})
        start = self.text.index('      - name: App token\n')
        app = self.text[start:self.text.index('      - name: ', start + 1)]
        self.assertIn('permission-checks: read\n', app)
        self.assertIn('permission-actions: read\n', app)


class QueueTests(unittest.TestCase):
    """queue: max on every job of the release-line group (ENG-2235, plan 1.4.6 b). Measured on
    dexterds14/ds-release-scratch (2026-11-23, 78 runs): queue: max keeps every waiting run only
    when every run of the group declares it; a plain arrival cancels every waiter."""

    def test_every_member_of_the_group_keeps_its_place(self):
        root = os.path.join(os.path.dirname(SCRIPTS), '.github/workflows')
        members = []
        for name in sorted(os.listdir(root)):
            with open(os.path.join(root, name)) as f:
                text = f.read()
            for m in re.finditer(r'^( *)concurrency:\n((?:\1  .*\n)+)', text, re.M):
                block = m.group(2)
                if 'release-line' not in block:
                    continue
                members.append(name)
                with self.subTest(file=name, at=m.start()):
                    self.assertIn('cancel-in-progress: false\n', block)
                    self.assertIn('queue: max\n', block)
        self.assertEqual(members, ['release-abandon.yml', 'release-create-branch.yml', 'release-merge-up.yml',
                                   'release-promote.yml', 'release-promote.yml', 'release-promote.yml'])

    def test_the_sweep_knows_the_same_members(self):
        with open(SWEEP) as f:
            text = f.read()
        self.assertIn('GROUP=(release-merge-up.yml release-promote.yml release-abandon.yml release-create-branch.yml)\n', text)


if __name__ == '__main__':
    unittest.main()
