"""Finishing a merge-up PR (ENG-2234, plan 6.1 and 6.2): who counts as an approver,
what merge-up.sh reads before it merges with the App's ruleset bypass, and what it
does around the merge.

The MergeUpTests fixture of test_release_git.py (one bare repo, the stub gh):
1.x.x merges up into main and foo.txt conflicts, so PR 1 is the conflict PR on
merge-up/1.x.x/<sha7>.
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)
import test_rules_merge_up as trm  # noqa: E402  (the same)

MERGE_UP, REPO = t.MERGE_UP, t.REPO
git, write = t.git, t.write
CHILD, PARENT = '1.x.x', 'main'
HEAD = 'a' * 40
OLD = 'b' * 40


def review(login, state='APPROVED', kind='User', commit=HEAD):
    """One review as the REST API lists it."""
    return {'user': {'login': login, 'type': kind}, 'state': state, 'commit_id': commit}


def view(**over):
    """A `gh pr view` answer of a PR that is ready: approved, clean, last commit by resolver."""
    doc = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'headRefOid': HEAD,
           'statusCheckRollup': [{'name': 'pr-checks', 'conclusion': 'SUCCESS'}],
           'reviews': [], 'commits': [{'authors': [{'login': 'resolver'}]}]}
    doc.update(over)
    return doc


class ReviewTests(unittest.TestCase):
    """Bot reviews never count (6.1)."""

    def test_a_person_counts(self):
        reasons, pusher, approvers = release_git.pr_check(view(), [review('alice')])
        self.assertEqual((reasons, pusher, approvers), ([], 'resolver', ['alice']))

    def test_a_bot_only_approval_is_refused(self):
        # github-actions approving through GITHUB_TOKEN: the aggregate says APPROVED
        reasons, _, approvers = release_git.pr_check(view(), [review('github-actions[bot]', kind='Bot')])
        self.assertEqual(approvers, [])
        self.assertEqual(len(reasons), 1)
        self.assertIn("no APPROVED review by someone other than the last pusher ('resolver')", reasons[0])
        self.assertIn('the approval by github-actions[bot] is a bot review and never counts', reasons[0])

    def test_an_app_submitted_approval_is_refused(self):
        # an App other than github-actions, whatever its login looks like
        for login in ('ds-os-release-bot[bot]', 'copilot-pull-request-reviewer[bot]', 'looks-like-a-person'):
            with self.subTest(login=login):
                reasons, _, approvers = release_git.pr_check(view(), [review(login, kind='Bot')])
                self.assertEqual(approvers, [])
                self.assertIn(f'the approval by {login} is a bot review and never counts', reasons[0])
        # an account type nobody listed is not a person either
        self.assertTrue(release_git.pr_check(view(), [review('acme', kind='Organization')])[0])
        self.assertTrue(release_git.pr_check(view(), [{'user': {'login': 'x'}, 'state': 'APPROVED'}])[0])

    def test_a_bot_next_to_a_person_changes_nothing(self):
        reviews = [review('github-actions[bot]', kind='Bot'), review('alice'),
                   review('github-actions[bot]', 'CHANGES_REQUESTED', kind='Bot')]
        self.assertEqual(release_git.pr_check(view(), reviews), ([], 'resolver', ['alice']))

    def test_the_last_review_of_a_person_decides(self):
        took_it_back = [review('alice'), review('alice', 'CHANGES_REQUESTED')]
        self.assertTrue(release_git.pr_check(view(), took_it_back)[0])
        dismissed = [review('alice'), review('alice', 'DISMISSED')]
        self.assertTrue(release_git.pr_check(view(), dismissed)[0])
        # a comment after the approval leaves it standing
        self.assertEqual(release_git.pr_check(view(), [review('alice'), review('alice', 'COMMENTED')])[0], [])

    def test_the_last_pusher_cannot_approve(self):
        reasons, _, _ = release_git.pr_check(view(), [review('resolver')])
        self.assertIn("other than the last pusher ('resolver')", reasons[0])

    def test_the_views_own_reviews_are_not_read(self):
        # gh pr view reports a bot's review without a type; with the REST list given it is ignored
        doc = view(reviews=[{'author': {'login': 'github-actions'}, 'state': 'APPROVED'}])
        self.assertTrue(release_git.pr_check(doc, [])[0])
        # without the REST list the view's reviews count, known bot logins aside, as they always did
        self.assertTrue(release_git.pr_check(doc)[0])
        doc = view(reviews=[{'author': {'login': 'alice'}, 'state': 'APPROVED'}])
        self.assertEqual(release_git.pr_check(doc), ([], 'resolver', ['alice']))
        self.assertTrue(release_git.pr_check(doc, [])[0])

    def test_the_other_reasons_stay(self):
        ok = [review('alice')]
        self.assertTrue(any('not CLEAN' in r for r in release_git.pr_check(view(mergeStateStatus='DIRTY'), ok)[0]))
        self.assertTrue(any('not APPROVED' in r for r in release_git.pr_check(view(reviewDecision='REVIEW_REQUIRED'), ok)[0]))
        failing = view(statusCheckRollup=[{'name': 'build', 'conclusion': 'FAILURE'}])
        self.assertTrue(any('check build: FAILURE' in r for r in release_git.pr_check(failing, ok)[0]))

    def test_read_json_stream(self):
        top = tempfile.mkdtemp(prefix='finish-')
        self.addCleanup(shutil.rmtree, top, True)
        path = os.path.join(top, 'x.json')
        for text, want in (('[{"a": 1}, {"a": 2}]', [1, 2]), ('[{"a": 1}]\n[{"a": 2}]\n', [1, 2]),
                           ('{"a": 1}\n{"a": 2}\n', [1, 2]), ('[]', []), ('', [])):
            with self.subTest(text=text):
                write(path, text)
                self.assertEqual([d['a'] for d in release_git.read_json_stream(path)], want)


class FinishCase(unittest.TestCase):
    """The fixture: PR 1 is an open conflict PR, resolved by `resolver` on its branch."""

    child_work = t.MergeUpTests.child_work
    conflict_pr = t.MergeUpTests.conflict_pr
    resolve = t.MergeUpTests.resolve

    def setUp(self):
        t.MergeUpTests.setUp(self)

    def resolved_pr(self):
        _, self.parent_tip, self.branch = self.conflict_pr()
        self.bot_commit = self.fx.heads()[self.branch]
        self.pr_head = self.resolve(self.branch)
        return self.pr_head

    def view(self, number=1, **over):
        doc = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
               'reviews': [], 'commits': [{'authors': [{'login': 'resolver'}]}]}
        doc.update(over)
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps(doc))

    def reviews(self, *reviews, number=1):
        write(os.path.join(self.fx.state, f'pr-{number}-rest-reviews.json'), json.dumps(list(reviews)))

    def finish(self, *args, scripts=None, **kw):
        script = os.path.join(scripts, 'merge-up.sh') if scripts else MERGE_UP
        return self.fx.run(script, '--child', CHILD, '--pr', '1', *args, **kw)

    def parent_now(self):
        return self.fx.heads()[PARENT]

    def assert_refused(self, r, why):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(why, r.stdout + r.stderr)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual(self.parent_now(), self.parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def assert_merged(self, r):
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('via PR 1', r.stdout)
        self.assertEqual(git(self.fx.bare, 'rev-parse', self.parent_now() + '^2'), self.pr_head)


class FinishReviewTests(FinishCase):
    """merge-up.sh --pr reads the reviews through the REST API (6.1)."""

    def test_a_person_finishes(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('alice', commit=head))
        r = self.finish()
        self.assert_merged(r)
        self.assertIn(f'api --paginate repos/{REPO}/pulls/1/reviews', self.fx.gh_log())

    def test_a_bot_only_approval_does_not_finish(self):
        head = self.resolved_pr()
        # what the aggregate shows after github-actions approved: APPROVED, and gh lists the review
        self.view(reviews=[{'author': {'login': 'github-actions'}, 'state': 'APPROVED'}])
        self.reviews(review('github-actions[bot]', kind='Bot', commit=head))
        self.assert_refused(self.finish(), 'the approval by github-actions[bot] is a bot review and never counts')
        # a dry run refuses the same way
        r = self.finish(dry_run='true')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('would gh pr merge', r.stdout)

    def test_an_app_submitted_approval_does_not_finish(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('ds-os-release-bot[bot]', kind='Bot', commit=head))
        self.assert_refused(self.finish(), 'the approval by ds-os-release-bot[bot] is a bot review and never counts')

    def test_reviews_that_cannot_be_read_do_not_finish(self):
        self.resolved_pr()
        self.view(reviews=[{'author': {'login': 'alice'}, 'state': 'APPROVED'}])
        write(os.path.join(self.fx.state, 'fail-reviews'), '')
        r = self.finish()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'::error::merge-up: PR {REPO}#1: its reviews could not be read (HTTP 502: Bad Gateway)', r.stdout)
        self.assertEqual(self.parent_now(), self.parent_tip)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])


def push(login, kind='User', what='push'):
    """One entry of the repository activity of a branch."""
    return {'activity_type': what, 'actor': {'login': login, 'type': kind}}


def above(sha, author, committer=None, kind='User'):
    """One REST commit object; a login of None is an email GitHub maps to no account."""
    user = lambda login: {'login': login, 'type': kind} if login else None
    return {'sha': sha, 'author': user(author), 'committer': user(committer or author),
            'commit': {'author': {'email': 'a@x'}, 'committer': {'email': 'c@x'}}}


# the unit cases require two checks, so that one can be missing next to a green one;
# the shipped config requires SHIPPED_GREEN's one
REQUIRED = ['pr-checks', 'another-check']
GREEN = [{'name': n, 'conclusion': 'SUCCESS'} for n in REQUIRED]
SHIPPED_GREEN = GREEN[:1]
BOT_PUSH = [push('ds-os-release-bot[bot]', 'Bot', 'branch_creation')]


class StrictTests(unittest.TestCase):
    """merge_up.finish.strict (6.2). Every case is checked with strict off and on."""

    def check(self, reviews, strict, activity=BOT_PUSH + [push('resolver')], commits=None, **over):
        doc = view(statusCheckRollup=over.pop('rollup', GREEN), **over)
        notes = []
        reasons, _, approvers = release_git.pr_check(doc, reviews, strict=strict, required=REQUIRED if strict else (),
                                                     activity=activity, commits=commits, notes=notes)
        return reasons, approvers, notes

    def test_an_approval_on_the_head_by_someone_who_did_not_push(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.assertEqual(self.check([review('alice')], strict)[:2], ([], ['alice']))
        self.assertEqual(self.check([review('alice')], True)[2], ['push actors, from the repository activity: resolver'])

    def test_a_stale_approval(self):
        # approved before the resolution was pushed (meta-dsensos #182)
        stale = [review('alice', commit=OLD)]
        self.assertEqual(self.check(stale, False)[:2], ([], ['alice']))
        reasons, approvers, _ = self.check(stale, True)
        self.assertEqual(approvers, [])
        self.assertIn(f'strict: alice approved {OLD[:7]}, not the PR head {HEAD[:7]}; an approval counts only for the commit it was given on', reasons)
        self.assertIn(f'strict: no APPROVED review on the PR head {HEAD[:7]} by a person who did not push to the branch', reasons)
        # approving again on the head counts; one stale approval next to a good one does not block
        self.assertEqual(self.check(stale + [review('alice')], True)[:2], ([], ['alice']))
        reasons, approvers, notes = self.check(stale + [review('carol')], True)
        self.assertEqual((reasons, approvers), ([], ['carol']))
        # the stale one is still named, in the log
        self.assertIn(f'not counted, alice approved {OLD[:7]}, not the PR head {HEAD[:7]}; an approval counts only for the commit it was given on', notes)

    def test_the_approver_pushed_the_resolution(self):
        # alice pushed part of the resolution, bob pushed last: today only bob is "the pusher"
        activity = BOT_PUSH + [push('alice'), push('bob')]
        doc = dict(commits=[{'authors': [{'login': 'bob'}]}])
        self.assertEqual(self.check([review('alice')], False, activity, **doc)[:2], ([], ['alice']))
        reasons, approvers, _ = self.check([review('alice')], True, activity, **doc)
        self.assertEqual(approvers, [])
        self.assertIn('strict: alice approved but also pushed to the branch (the repository activity)', reasons)
        # the resolver also clicked Approve (11 of 21 past PRs), and a second person approved the
        # head: the second approval counts, the resolver's is a note and does not block
        reasons, approvers, notes = self.check([review('alice'), review('carol')], True, activity, **doc)
        self.assertEqual((reasons, approvers), ([], ['carol']))
        self.assertIn('not counted, alice approved but also pushed to the branch (the repository activity)', notes)
        # a force push and creating the branch count as pushing; a pr_merge entry does not
        for what in ('force_push', 'branch_creation'):
            self.assertTrue(self.check([review('alice')], True, [push('alice', what=what)])[0], what)
        self.assertEqual(self.check([review('alice')], True, BOT_PUSH + [push('alice', what='pr_merge')])[0], [])
        # the App aside: its pushes never name a person
        self.assertEqual(self.check([review('alice')], True, BOT_PUSH)[2], ['push actors, from the repository activity: nobody but the bot'])

    def test_the_fallback_when_the_activity_cannot_be_read(self):
        resolver = [above('1' * 40, 'resolver')]
        reasons, approvers, notes = self.check([review('alice')], True, None, resolver)
        self.assertEqual((reasons, approvers), ([], ['alice']))
        self.assertEqual(notes, ["push actors, from the authors and committers of the commits above the bot's commit: resolver"])
        # the author and the committer both count
        for commits in ([above('1' * 40, 'alice', 'resolver')], [above('1' * 40, 'resolver', 'alice')]):
            reasons, approvers, _ = self.check([review('alice')], True, None, commits)
            self.assertEqual(approvers, [])
            self.assertIn("strict: alice approved but also pushed to the branch (the authors and committers of the commits above the bot's commit)", reasons)
        # a commit GitHub maps to no account: nothing shows who wrote it
        reasons, _, _ = self.check([review('alice')], True, None, [above('2' * 40, None, 'resolver')])
        self.assertIn(f'strict: commit {"2" * 7} (author a@x) maps to no GitHub account, so nothing shows the approver did not write it', reasons)
        # nothing readable at all
        reasons, _, _ = self.check([review('alice')], True, None, None)
        self.assertTrue(any('neither the repository activity of the branch nor its commits could be read' in r for r in reasons))
        # nobody pushed above the bot's commit (a PR the rule resolved)
        self.assertEqual(self.check([review('alice')], True, None, [])[:2], ([], ['alice']))
        # without strict none of this is read
        self.assertEqual(self.check([review('alice')], False, None, [above('1' * 40, 'alice')])[:2], ([], ['alice']))

    def test_a_bot_only_approval(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                reasons, approvers, _ = self.check([review('github-actions[bot]', kind='Bot')], strict)
                self.assertEqual(approvers, [])
                self.assertTrue(any('the approval by github-actions[bot] is a bot review and never counts' in r for r in reasons))

    def test_an_app_submitted_approval(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                reasons, approvers, _ = self.check([review('ds-os-release-bot[bot]', kind='Bot')], strict)
                self.assertEqual(approvers, [])
                self.assertTrue(any('the approval by ds-os-release-bot[bot] is a bot review and never counts' in r for r in reasons))

    def test_a_missing_required_check(self):
        ok = [review('alice')]
        missing = [GREEN[0]]
        self.assertEqual(self.check(ok, False, rollup=missing)[:2], ([], ['alice']))
        self.assertEqual(self.check(ok, True, rollup=missing)[0], ['required check another-check: not reported on the head'])
        # no check at all: "none failed" holds, and strict names both
        self.assertEqual(self.check(ok, False, rollup=[])[0], [])
        self.assertEqual(len(self.check(ok, True, rollup=[])[0]), 2)
        # still running, skipped or neutral is not success
        for run in ({'name': 'another-check', 'status': 'IN_PROGRESS', 'conclusion': ''},
                    {'name': 'another-check', 'conclusion': 'SKIPPED'},
                    {'name': 'another-check', 'conclusion': 'NEUTRAL'},
                    {'context': 'another-check', 'state': 'PENDING'}):
            with self.subTest(run=run):
                self.assertEqual(self.check(ok, False, rollup=[GREEN[0], run])[0], [])
                reasons = self.check(ok, True, rollup=[GREEN[0], run])[0]
                self.assertEqual(len(reasons), 1)
                self.assertIn('required check another-check: ', reasons[0])
                self.assertIn(', not SUCCESS', reasons[0])
        # a failed one is named once, by the check that always ran
        failed = [GREEN[0], {'name': 'another-check', 'conclusion': 'FAILURE'}]
        self.assertEqual(self.check(ok, True, rollup=failed)[0], ['check another-check: FAILURE'])
        # a check nobody requires may be missing or pending
        extra = GREEN + [{'name': 'optional', 'status': 'QUEUED', 'conclusion': ''}]
        self.assertEqual(self.check(ok, True, rollup=extra)[0], [])

    def test_only_the_newest_run_of_a_check_counts(self):
        # The rollup of dexterds14/ds-release-scratch#25 after a reopen (2026-11-18): the older
        # pr-checks run's job CANCELLED by the newer run's concurrency group (cancel-in-progress,
        # the same block as this repo's pr-checks.yml), the newer run green. GitHub read CLEAN;
        # a pr=N finish must not refuse it, with strict off or on.
        ok = [review('alice')]
        cancelled = {'__typename': 'CheckRun', 'name': 'another-check', 'workflowName': 'PR checks', 'status': 'COMPLETED',
                     'conclusion': 'CANCELLED', 'startedAt': '2026-11-18T23:32:05Z', 'completedAt': '2026-11-18T23:33:26Z'}
        newer = dict(cancelled, conclusion='SUCCESS', startedAt='2026-11-18T23:33:26Z', completedAt='2026-11-18T23:35:59Z')
        for rollup in ([GREEN[0], cancelled, newer], [GREEN[0], newer, cancelled]):   # the list order is not GitHub's promise
            for strict in (False, True):
                with self.subTest(strict=strict, first=rollup[1]['conclusion']):
                    self.assertEqual(self.check(ok, strict, rollup=rollup)[0], [])
        # the newest run is the cancelled one: red either way, named once
        older = dict(newer, completedAt='2026-11-18T23:33:00Z')
        for strict in (False, True):
            with self.subTest(strict=strict, newest='CANCELLED'):
                self.assertEqual(self.check(ok, strict, rollup=[GREEN[0], older, cancelled])[0], ['check another-check: CANCELLED'])
        # a newer run still going (gh prints its completedAt as the zero time) hides no failure
        running = dict(newer, status='IN_PROGRESS', conclusion='', completedAt='0001-01-01T00:00:00Z')
        failed = dict(cancelled, conclusion='FAILURE')
        self.assertEqual(self.check(ok, False, rollup=[GREEN[0], failed, running])[0], [])
        reasons = self.check(ok, True, rollup=[GREEN[0], failed, running])[0]
        self.assertEqual(reasons, ['required check another-check: IN_PROGRESS, not SUCCESS'])

    def test_the_review_trigger_is_not_read(self):
        # ENG-2235: "Merge-up review trigger" runs on every review of a merge-up PR and does
        # nothing. A cancelled, timed-out or still queued run of it blocks no finish, in either mode.
        ok = [review('alice')]
        trigger = {'__typename': 'CheckRun', 'name': 'review', 'workflowName': release_git.REVIEW_TRIGGER_WORKFLOW}
        for run in (dict(trigger, status='COMPLETED', conclusion='CANCELLED'),
                    dict(trigger, status='COMPLETED', conclusion='TIMED_OUT'),
                    dict(trigger, status='COMPLETED', conclusion='STARTUP_FAILURE'),
                    dict(trigger, status='QUEUED', conclusion='')):
            for strict in (False, True):
                with self.subTest(run=run['status'] + run['conclusion'], strict=strict):
                    self.assertEqual(self.check(ok, strict, rollup=GREEN + [run])[0], [])
        # a required check of that name posted by that workflow does not count as reported either
        posing = dict(trigger, name='pr-checks', status='COMPLETED', conclusion='SUCCESS')
        self.assertEqual(self.check(ok, True, rollup=[GREEN[1], posing])[0], ['required check pr-checks: not reported on the head'])
        # the same job name in another workflow is read as before
        other = dict(trigger, workflowName='PR checks', status='COMPLETED', conclusion='CANCELLED')
        for strict in (False, True):
            with self.subTest(other=True, strict=strict):
                self.assertEqual(self.check(ok, strict, rollup=GREEN + [other])[0], ['check review: CANCELLED'])
        self.assertEqual(release_git.REVIEW_TRIGGER_WORKFLOW, 'Merge-up review trigger')

    def test_unstable_only_for_the_review_trigger(self):
        # ENG-2235 review: GitHub calls the PR UNSTABLE while the trigger's run is unfinished or after it
        # was cancelled. When those runs are the only unfinished or unsuccessful ones, a finish is not
        # refused for the merge state, in either mode.
        ok = [review('alice')]
        trigger = {'__typename': 'CheckRun', 'name': 'review', 'workflowName': release_git.REVIEW_TRIGGER_WORKFLOW}
        for run in (dict(trigger, status='QUEUED', conclusion=''), dict(trigger, status='IN_PROGRESS', conclusion=''),
                    dict(trigger, status='COMPLETED', conclusion='CANCELLED'), dict(trigger, status='COMPLETED', conclusion='FAILURE')):
            for strict in (False, True):
                with self.subTest(run=run['status'] + run['conclusion'], strict=strict):
                    self.assertEqual(self.check(ok, strict, rollup=GREEN + [run], mergeStateStatus='UNSTABLE')[0], [])
        unstable = "mergeStateStatus is 'UNSTABLE', not CLEAN"
        running = dict(trigger, status='IN_PROGRESS', conclusion='')
        for strict in (False, True):
            with self.subTest(strict=strict):
                # another check still running next to it: UNSTABLE is a reason again
                other = {'__typename': 'CheckRun', 'name': 'lint', 'workflowName': 'PR checks', 'status': 'IN_PROGRESS', 'conclusion': ''}
                self.assertIn(unstable, self.check(ok, strict, rollup=GREEN + [running, other], mergeStateStatus='UNSTABLE')[0])
                # a commit status that is not a success, or a check run with no workflow name
                status = {'__typename': 'StatusContext', 'context': 'ci/x', 'state': 'PENDING'}
                self.assertIn(unstable, self.check(ok, strict, rollup=GREEN + [running, status], mergeStateStatus='UNSTABLE')[0])
                nameless = dict(running, workflowName='')
                self.assertIn(unstable, self.check(ok, strict, rollup=GREEN + [nameless], mergeStateStatus='UNSTABLE')[0])
                # UNSTABLE with nothing unfinished or unsuccessful: GitHub's own reason, not the trigger's
                self.assertIn(unstable, self.check(ok, strict, rollup=GREEN + [dict(trigger, status='COMPLETED', conclusion='SUCCESS')],
                                                   mergeStateStatus='UNSTABLE')[0])
                # only UNSTABLE is set aside: BLOCKED with the trigger running is still refused
                self.assertIn("mergeStateStatus is 'BLOCKED', not CLEAN",
                              self.check(ok, strict, rollup=GREEN + [running], mergeStateStatus='BLOCKED')[0])
        # a newer successful run of the trigger replaces an older cancelled one
        older = dict(trigger, status='COMPLETED', conclusion='CANCELLED', completedAt='2026-11-20T10:00:00Z')
        newer = dict(trigger, status='COMPLETED', conclusion='SUCCESS', completedAt='2026-11-20T11:00:00Z')
        self.assertIn(unstable, self.check(ok, True, rollup=GREEN + [older, newer], mergeStateStatus='UNSTABLE')[0])

    def test_the_review_trigger_name_is_the_workflows(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up-review.yml')) as f:
            first = f.readline()
        self.assertEqual(first, f'name: {release_git.REVIEW_TRIGGER_WORKFLOW}\n')

    def test_strict_needs_its_inputs(self):
        with self.assertRaises(release_git.Error):
            release_git.pr_check(view(), None, strict=True)
        with self.assertRaises(release_git.Error):
            release_git.pr_check(view(headRefOid=None), [review('alice')], strict=True, activity=[])

    def test_shipped_off(self):
        self.assertIs(release_git.merge_up_config()['finish']['strict'], False)


class FinishStrictTests(FinishCase):
    """merge-up.sh --pr under merge_up.finish.strict. The same PR is first read by the shipped
    scripts in a dry run (strict off: it would merge), then by a copy with strict on."""

    def setUp(self):
        super().setUp()
        self.strict = self.fx.scripts_copy(merge_up={'finish.strict': True})

    def activity(self, *entries):
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps(BOT_PUSH + list(entries)))

    def logins(self, **emails):
        write(os.path.join(self.fx.state, 'logins.json'), json.dumps({e.replace('_at_', '@'): v for e, v in emails.items()}))

    def off_would_merge(self):
        r = self.finish(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: would gh pr merge 1', r.stdout)
        self.assertNotIn('strict finish:', r.stdout)
        self.assertNotIn('/activity', self.fx.gh_log())
        self.assertNotIn('/commits/', self.fx.gh_log())

    def off_refuses(self, why):
        r = self.finish(dry_run='true')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(why, r.stderr)

    def test_a_good_finish(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=head))
        self.activity(push('resolver'))
        self.off_would_merge()
        r = self.finish(scripts=self.strict)
        self.assert_merged(r)
        self.assertIn('finish.strict=true', r.stdout)
        self.assertIn('strict finish:      push actors, from the repository activity: resolver', r.stdout)
        self.assertIn(f'approved by alice on the head {head[:7]}, none of whom pushed to the branch; mergeable, '
                      'required checks green (pr-checks)', r.stdout)
        self.assertIn(f'api --paginate -X GET repos/{REPO}/activity -f ref=refs/heads/{self.branch}', self.fx.gh_log())

    def test_a_superseded_cancelled_run_does_not_block_the_finish(self):
        # dexterds14/ds-release-scratch#25 (2026-11-18): a reopen started a second pr-checks run
        # whose concurrency group cancelled the first run's job. The rollup held CANCELLED next
        # to the newer SUCCESS of the same check while GitHub read CLEAN, and the finish refused.
        head = self.resolved_pr()
        cancelled = {'__typename': 'CheckRun', 'name': 'pr-checks', 'workflowName': 'PR checks', 'status': 'COMPLETED',
                     'conclusion': 'CANCELLED', 'startedAt': '2026-11-18T23:32:05Z', 'completedAt': '2026-11-18T23:33:26Z'}
        newer = dict(cancelled, conclusion='SUCCESS', startedAt='2026-11-18T23:33:26Z', completedAt='2026-11-18T23:35:59Z')
        self.view(statusCheckRollup=[newer, cancelled])
        self.reviews(review('alice', commit=head))
        self.activity(push('resolver'))
        self.off_would_merge()
        self.assert_merged(self.finish(scripts=self.strict))

    def test_a_stale_approval(self):
        self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=self.bot_commit))   # given before the resolution was pushed
        self.activity(push('resolver'))
        self.off_would_merge()
        self.assert_refused(self.finish(scripts=self.strict),
                            f'strict: alice approved {self.bot_commit[:7]}, not the PR head {self.pr_head[:7]}')
        # the same refusal in a dry run
        r = self.finish(scripts=self.strict, dry_run='true')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('would gh pr merge', r.stdout)

    def test_strict_is_the_value_the_run_printed_not_a_second_read(self):
        # finish.strict is read once, at the top of the run, where a failed read stops it. A
        # second read inside the finish would sit in a test, and a read that fails there (python
        # cannot start, an I/O error) would pass for "not strict" and merge on the old checks.
        self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=self.bot_commit))   # stale: only strict refuses it
        self.activity(push('resolver'))
        count = os.path.join(self.fx.top, 'strict-reads')
        path = os.path.join(self.strict, 'release_git.py')
        with open(path) as f:
            source = f.read()
        entry = "if __name__ == '__main__':\n"
        self.assertEqual(source.count(entry), 1)
        fault = (entry +
                 "    if sys.argv[1:] == ['merge-up-config', '--key', 'finish.strict']:\n"
                 f"        with open({count!r}, 'a') as f:\n"
                 "            f.write('read\\n')\n"
                 f"        if open({count!r}).read().count('read') > 1:\n"
                 "            sys.exit('release_git.py: Input/output error')\n")
        with open(path, 'w') as f:
            f.write(source.replace(entry, fault))
        r = self.finish(scripts=self.strict)
        self.assertIn('finish.strict=true', r.stdout)
        self.assert_refused(r, f'strict: alice approved {self.bot_commit[:7]}, not the PR head {self.pr_head[:7]}')
        with open(count) as f:
            self.assertEqual(f.read(), 'read\n')

    def test_the_approver_pushed_the_resolution(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN, commits=[{'authors': [{'login': 'bob'}]}])
        self.reviews(review('alice', commit=head))
        self.activity(push('alice'), push('bob'))
        self.off_would_merge()
        self.assert_refused(self.finish(scripts=self.strict), 'strict: alice approved but also pushed to the branch (the repository activity)')

    def test_a_second_approver_finishes_next_to_approvals_that_do_not_count(self):
        # the resolver approved their own resolution, someone approved an older commit, and a
        # third person approved the head: that one approval is what strict asks for
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('resolver', commit=head), review('early', commit='0' * 40), review('alice', commit=head))
        self.activity(push('resolver'))
        r = self.finish(scripts=self.strict)
        self.assert_merged(r)
        self.assertIn('not counted, resolver approved but also pushed to the branch', r.stdout)
        self.assertIn('not counted, early approved 0000000, not the PR head', r.stdout)

    def test_the_fallback_is_logged_and_finds_the_approver(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN, commits=[{'authors': [{'login': 'bob'}]}])
        self.reviews(review('alice', commit=head))
        # no activity.json: the stub answers 403. The resolver's commit (t@x) is alice's.
        self.logins(t_at_x=['alice', 'User'])
        self.off_would_merge()
        r = self.finish(scripts=self.strict)
        self.assert_refused(r, "strict: alice approved but also pushed to the branch (the authors and committers of the commits above the bot's commit)")
        self.assertIn(f'strict finish:      the repository activity of {self.branch} could not be read (HTTP 403: Resource not accessible by integration); '
                      "falling back to the authors and committers of the commits above the bot's commit", r.stdout)
        # only the resolver's commit was asked for: not the bot's merge, not the child's commits
        asked = [l.split('/commits/')[1] for l in self.fx.gh_log().splitlines() if l.startswith('api repos/') and '/commits/' in l]
        self.assertEqual(asked, [head])

    def test_the_fallback_passes_someone_else(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=head))
        self.logins(t_at_x=['resolver', 'User'])
        r = self.finish(scripts=self.strict)
        self.assert_merged(r)
        self.assertIn("strict finish:      push actors, from the authors and committers of the commits above the bot's commit: resolver", r.stdout)

    def test_the_fallback_refuses_a_commit_without_an_account(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=head))
        self.assert_refused(self.finish(scripts=self.strict), f'strict: commit {head[:7]} (author t@x) maps to no GitHub account')

    def test_nothing_readable_refuses(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('alice', commit=head))
        write(os.path.join(self.fx.state, 'fail-commits'), '')
        self.assert_refused(self.finish(scripts=self.strict), 'neither the repository activity of the branch nor its commits could be read')

    def test_a_bot_only_approval(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('github-actions[bot]', kind='Bot', commit=head))
        self.activity(push('resolver'))
        self.off_refuses('the approval by github-actions[bot] is a bot review and never counts')
        self.assert_refused(self.finish(scripts=self.strict), 'the approval by github-actions[bot] is a bot review and never counts')

    def test_an_app_submitted_approval(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=SHIPPED_GREEN)
        self.reviews(review('ds-os-release-bot[bot]', kind='Bot', commit=head))
        self.activity(push('resolver'))
        self.off_refuses('the approval by ds-os-release-bot[bot] is a bot review and never counts')
        self.assert_refused(self.finish(scripts=self.strict), 'the approval by ds-os-release-bot[bot] is a bot review and never counts')

    def test_a_missing_required_check(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=[])
        self.reviews(review('alice', commit=head))
        self.activity(push('resolver'))
        self.off_would_merge()
        self.assert_refused(self.finish(scripts=self.strict), 'required check pr-checks: not reported on the head')


def stamped(login, state='APPROVED', commit=HEAD, at='2026-11-20T12:00:00Z'):
    """A REST review with its time, as the auto-finish reads it."""
    return dict(review(login, state, commit=commit), submitted_at=at)


class AutoFinishTests(FinishCase):
    """merge-up.sh --pr N --auto-finish (ENG-2235, plan 1.4.5): the finish release-auto-finish.yml
    starts. PR 1 is ready: resolver pushed at 11:00, alice (write) approved the head at 12:00."""

    BOT = 'ds-os-release-bot[bot]'

    def ready_pr(self):
        head = self.resolved_pr()
        self.view(statusCheckRollup=GREEN)
        self.reviews(stamped('alice', commit=head))
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps([
            dict(push(self.BOT, 'Bot', 'branch_creation'), timestamp='2026-11-20T10:00:00Z'),
            dict(push('resolver'), timestamp='2026-11-20T11:00:00Z')]))
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'write'}))
        return head

    def auto(self, *args, **kw):
        env = dict(kw.pop('extra_env', None) or {}, AUTO_FINISH_RETRY_SECONDS='0')
        return self.finish('--auto-finish', *args, extra_env=env, **kw)

    def comments(self):
        return [c['body'] for c in self.fx.prs()[0].get('issue_comments', [])]

    def stops(self):
        return [b for b in self.comments() if 'merge-up-auto-finish: stopped head=' in b]

    def assert_no_comment(self):
        self.assertEqual([b for b in self.comments() if 'merge-up-auto-finish' in b], [])

    def test_it_needs_pr(self):
        r = self.fx.run(MERGE_UP, '--child', CHILD, '--auto-finish')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: --auto-finish is only for a finish: it needs --pr N', r.stdout)
        self.assertEqual(self.fx.gh_log(), '')

    def test_it_merges_and_comments(self):
        head = self.ready_pr()
        r = self.auto()
        self.assert_merged(r)
        self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]} after the last push to the branch', r.stdout)
        merged = [b for b in self.comments() if 'merge-up-auto-finish' in b]
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].startswith(f'Merged by release-merge-up, started by release-auto-finish.yml: alice approved {head[:7]} '
                                             'after the last push to the branch without pushing to it'), merged[0])
        self.assertTrue(merged[0].endswith(f'\n\n<!-- merge-up-auto-finish: merged head={head} -->'), merged[0])
        log = self.fx.gh_log()
        for call in (f'api repos/{REPO}/collaborators/alice/permission --jq .permission',
                     f'api --paginate repos/{REPO}/issues/1/comments'):
            self.assertIn(call, log)
        self.assertNotIn('--admin', log)

    def test_it_is_strict_whatever_the_config_says(self):
        # shipped config: finish.strict false. An approval of an older commit finishes by hand, never automatically.
        self.ready_pr()
        self.reviews(stamped('alice', commit=self.bot_commit))
        r = self.finish(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: would gh pr merge 1', r.stdout)
        r = self.auto(dry_run='true')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('finish.strict=false', r.stdout)   # the config line prints the config
        self.assertIn(f'auto-finish:        not ready: stale-approval: alice approved {self.bot_commit[:7]}, not the PR head', r.stdout)
        self.assertIn('not ready to merge', r.stdout)
        self.assertNotIn('would gh pr merge', r.stdout)

    def test_the_block_says_so_after_the_config_line(self):
        # the auto-finish line follows the config line; the bot finish line (2c) is the block's last
        self.ready_pr()
        r = self.auto(dry_run='true')
        lines = r.stdout.splitlines()
        start = lines.index('== merge-up automation')
        self.assertEqual(lines[start + 5], 'auto-finish:        yes (PR #1, from release-auto-finish.yml): the strict finish checks apply '
                                           'whatever finish.strict says, and the outcome is left on the PR')
        self.assertTrue(lines[start + 4].startswith('config:'))
        self.assertTrue(lines[start + 6].startswith('bot finish:'), lines[start:start + 8])
        self.assertNotIn('auto-finish:', self.finish(dry_run='true').stdout)

    def test_a_refusal_leaves_one_stop_comment(self):
        head = self.ready_pr()
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'read'}))
        r = self.auto()
        self.assert_refused(r, 'approver-cannot-write: alice has read permission, not write')
        self.assertEqual(len(self.stops()), 1)
        stop = self.stops()[0]
        self.assertTrue(stop.startswith('release-merge-up tried to finish this pull request automatically (release-auto-finish.yml) and stopped: '
                                        f'PR {REPO}#1 is not ready to merge (approver-cannot-write, no-approval; the reasons are listed below). '
                                        'Nothing was merged.'), stop)
        # the reasons are in the comment, not only in the run log (2a review, nit 2)
        self.assertIn('\n\nThe reasons:\n- approver-cannot-write: alice has read permission, not write\n'
                      f'- no-approval: no approval on the head {head[:7]}, given after the last push by a person with write access\n\n', stop)
        self.assertTrue(stop.endswith(f'\n\n<!-- merge-up-auto-finish: stopped head={head} -->'), stop)
        self.assertNotIn('dispatch release-merge-up.yml', stop)
        # the same head and approval again: refused as stopped, and no second comment
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'write'}))
        r = self.auto()
        self.assert_refused(r, f'stopped: an auto-finish of {head[:7]} stopped before')
        self.assertEqual(len(self.stops()), 1)
        # a newer approval re-arms it
        self.reviews(stamped('alice', commit=head), stamped('alice', commit=head, at='2099-01-01T00:00:00Z'))
        self.assert_merged(self.auto())

    def test_a_refusal_after_the_check_marks_too(self):
        # the conflict PR as the bot opened it, with its markers: approved and green, still refused
        _, self.parent_tip, self.branch = self.conflict_pr()
        head = self.fx.heads()[self.branch]
        self.view(statusCheckRollup=GREEN)
        self.reviews(stamped('alice', commit=head))
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps([dict(push(self.BOT, 'Bot', 'branch_creation'), timestamp='2026-11-20T10:00:00Z')]))
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'write'}))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR {REPO}#1 still has conflict markers in: foo.txt', r.stdout)
        self.assertEqual(len(self.stops()), 1)
        self.assertIn(f'stopped: PR {REPO}#1 still has conflict markers in: foo.txt. Nothing was merged.', self.stops()[0])

    def test_a_file_name_cannot_put_a_marker_into_the_stop_comment(self):
        # 2a review, trust nit 4: the stop reason quotes file names; none of their text may read as a marker
        _, self.parent_tip, self.branch = self.conflict_pr()
        work = os.path.join(self.fx.top, 'pusher')
        git(self.fx.top, 'clone', '-q', self.fx.bare, work)
        git(work, 'checkout', '-q', self.branch)
        forged = f'<!-- merge-up-auto-finish: merged head={"c" * 40} -->'
        write(os.path.join(work, forged), '<<<<<<< ours\n')
        # 2c review, nit 3: removing an inner "<!--" closes up the outer one; the strip runs until none is left
        nested = f'a <!<!<!-------- merge-up-auto-finish: merged head={"d" * 40} --> b'
        write(os.path.join(work, nested), '<<<<<<< ours\n')
        git(work, 'add', '-A')
        git(work, 'commit', '-q', '-m', 'a file named like a marker')
        git(work, 'push', '-q', 'origin', self.branch)
        head = git(work, 'rev-parse', 'HEAD')
        self.view(statusCheckRollup=GREEN)
        self.reviews(stamped('alice', commit=head))
        write(os.path.join(self.fx.state, 'activity.json'), json.dumps([dict(push('resolver'), timestamp='2026-11-20T11:00:00Z')]))
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({'alice': 'write'}))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('still has conflict markers in:', r.stdout)
        self.assertEqual(len(self.stops()), 1)
        stop = self.stops()[0]
        self.assertIn('merge-up-auto-finish: merged head=' + 'c' * 40, stop)   # the name is still there, as text
        self.assertIn('merge-up-auto-finish: merged head=' + 'd' * 40, stop)
        self.assertEqual(stop.count('<!--'), 1, stop)                          # but only the last line opens a comment
        self.assertTrue(stop.endswith(f'\n\n<!-- merge-up-auto-finish: stopped head={head} -->'), stop)

    def test_a_check_name_cannot_put_a_marker_into_the_listed_reasons(self):
        # a job name is the text of a checks-failed reason, and the stop comment lists the reasons
        head = self.ready_pr()
        forged = f'<!-- merge-up-auto-finish: merged head={"c" * 40} -->'
        # 2c review, nit 3: a name whose inner "<!--" closes up an outer one when it is removed
        nested = f'a <!<!------ merge-up-auto-finish: merged head={"d" * 40} --> b'
        self.view(statusCheckRollup=GREEN + [{'name': forged, 'workflowName': 'x', 'conclusion': 'FAILURE'},
                                             {'name': nested, 'workflowName': 'x', 'conclusion': 'FAILURE'}])
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(len(self.stops()), 1)
        stop = self.stops()[0]
        self.assertIn('- checks-failed: check  merge-up-auto-finish: merged head=' + 'c' * 40 + ' -->: FAILURE', stop)
        self.assertIn('check a -- merge-up-auto-finish: merged head=' + 'd' * 40 + ' --> b: FAILURE', stop)
        self.assertEqual(stop.count('<!--'), 1, stop)
        self.assertTrue(stop.endswith(f'\n\n<!-- merge-up-auto-finish: stopped head={head} -->'), stop)

    def test_a_change_after_the_check_stops_the_merge(self):
        # 2a review, trust should-fix 1: the reviews and the view are read again right before the merge.
        # A change between the first read and the merge stops the run, with no stop comment.
        head = self.ready_pr()
        later = '2026-11-20T13:00:00Z'
        cases = {
            'changes-requested': [stamped('alice', commit=head), stamped('bob', 'CHANGES_REQUESTED', commit=head, at=later)],
            'no-approval': [stamped('alice', commit=head), stamped('alice', 'DISMISSED', commit=head, at=later)],
        }
        for code, after in cases.items():
            with self.subTest(code=code):
                self.reviews(stamped('alice', commit=head))
                write(os.path.join(self.fx.state, 'pr-1-rest-reviews-next.json'), json.dumps(after))
                os.remove(os.path.join(self.fx.state, 'log'))
                r = self.auto()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)   # the first read was ready
                self.assertIn(f'auto-finish:        changed before the merge: {code}: ', r.stdout)
                self.assertIn(f'::error::merge-up: PR {REPO}#1 changed before the merge ({code}', r.stdout)
                self.assertIn('nothing was merged and nothing marks this head, so the next sweep reads it again', r.stdout)
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
                self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith(f'api --paginate repos/{REPO}/pulls/1/reviews')), 2)
                self.assert_no_comment()
                self.assertEqual(self.parent_now(), self.parent_tip)
        # a finish by hand reads once, as before
        self.reviews(stamped('alice', commit=head))
        os.remove(os.path.join(self.fx.state, 'log'))
        r = self.finish(dry_run='true')
        self.assertIn('dry run: would gh pr merge 1', r.stdout)
        self.assertNotIn('read again before the merge', r.stdout)
        self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith(f'api --paginate repos/{REPO}/pulls/1/reviews')), 1)
        # unchanged, it merges, and says it read again
        r = self.auto()
        self.assert_merged(r)
        self.assertIn(f'auto-finish:        read again before the merge: still ready, approved by alice on {head[:7]}', r.stdout)

    def test_a_state_github_settles_is_read_again_before_the_merge(self):
        # 2c review, nit 1: UNKNOWN, or UNSTABLE while only the review trigger runs (a second review in the
        # window starts it), at the read before the merge is read again, as auto_ready does
        head = self.ready_pr()
        clean = dict(reviewDecision='APPROVED', mergeStateStatus='CLEAN', statusCheckRollup=GREEN, reviews=[],
                     commits=[{'authors': [{'login': 'resolver'}]}])
        trigger = {'__typename': 'CheckRun', 'name': 'review', 'workflowName': 'Merge-up review trigger', 'status': 'IN_PROGRESS'}
        settles = {'UNKNOWN': dict(clean, mergeStateStatus='UNKNOWN'),
                   'UNSTABLE': dict(clean, mergeStateStatus='UNSTABLE', statusCheckRollup=GREEN + [trigger])}
        for name, state in settles.items():
            code = 'merge-state-unknown' if name == 'UNKNOWN' else 'merge-state-trigger'
            with self.subTest(state=name):
                # the fourth view the stub answers is the read before the merge: one retry, then CLEAN
                write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps([clean, clean, clean, state, clean]))
                os.remove(os.path.join(self.fx.state, 'log'))
                r = self.auto(dry_run='true')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        read again before the merge: {code}, which GitHub settles by itself; '
                              'reading once more (2 of 3)', r.stdout)
                self.assertIn(f'auto-finish:        read again before the merge: still ready, approved by alice on {head[:7]}', r.stdout)
                self.assertIn('dry run: would gh pr merge 1', r.stdout)
                # it stays: the tries run out, and the run stops with no merge and no stop comment
                write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps([clean, clean, clean, state]))
                os.remove(os.path.join(self.fx.state, 'log'))
                r = self.auto(extra_env={'AUTO_FINISH_TRIES': '4'})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('reading once more (4 of 4)', r.stdout)
                self.assertIn(f'auto-finish:        changed before the merge: {code}: ', r.stdout)
                self.assertIn(f'::error::merge-up: PR {REPO}#1 changed before the merge ({code})', r.stdout)
                self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith(f'api --paginate repos/{REPO}/pulls/1/reviews')), 5)
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
                self.assert_no_comment()
                self.assertEqual(self.parent_now(), self.parent_tip)

    def test_a_new_head_after_the_check_stops_the_merge(self):
        head = self.ready_pr()
        # a push between the first read and the merge: the view of the last read shows another head
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(
            [dict(statusCheckRollup=GREEN, mergeStateStatus='CLEAN', reviewDecision='APPROVED')] * 3
            + [dict(statusCheckRollup=GREEN, mergeStateStatus='CLEAN', reviewDecision='APPROVED', headRefOid='f' * 40)]))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR {REPO}#1 moved off {head[:7]} (to fffffff) before the merge', r.stdout)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assert_no_comment()

    def git_wrapper(self):
        """A git in front of the real one that fails one kind of call while a state file says so:
        git-fail-fetch (a fetch of a merge-up branch), git-fail-cat-file (cat-file -e of the commit
        the file names), git-fail-ls-tree (the bookkeeping's ls-tree of versions/)."""
        real = shutil.which('git')
        write(os.path.join(self.fx.binp, 'git'), '#!/bin/sh\n'
              'case " $* " in\n'
              '  *" fetch "*"refs/heads/merge-up/"*) [ ! -e "$GH_STUB_STATE/git-fail-fetch" ] || { echo "fatal: stub: no fetch" >&2; exit 128; } ;;\n'
              '  *" cat-file -e "*"^{commit}"*) [ ! -e "$GH_STUB_STATE/git-fail-cat-file" ] || case "$*" in\n'
              '    *"cat-file -e $(cat "$GH_STUB_STATE/git-fail-cat-file")^{commit}"*) exit 1 ;; esac ;;\n'
              '  *" ls-tree -z --name-only "*" versions/ "*) [ ! -e "$GH_STUB_STATE/git-fail-ls-tree" ] || { echo "fatal: stub: no ls-tree" >&2; exit 128; } ;;\n'
              'esac\n'
              f'exec {real} "$@"\n')
        os.chmod(os.path.join(self.fx.binp, 'git'), 0o755)

    def test_reads_that_fail_after_the_check_leave_no_comment(self):
        # 2a review (port review, nit 1): a failed fetch, a head origin does not hold and bookkeeping
        # that cannot be read are no refusal of the head: die_unmarked, no stop comment
        head = self.ready_pr()
        self.git_wrapper()
        for flag, why in (('git-fail-fetch', f'could not fetch {self.branch} from {REPO}'),
                          ('git-fail-cat-file', f'PR {REPO}#1 head {head} is not what origin/{self.branch} holds; re-run'),
                          ('git-fail-ls-tree', f'PR {REPO}#1: its bookkeeping files could not be compared with {PARENT}\'s')):
            with self.subTest(flag=flag):
                write(os.path.join(self.fx.state, flag), head if flag == 'git-fail-cat-file' else '')
                try:
                    r = self.auto()
                finally:
                    os.remove(os.path.join(self.fx.state, flag))
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)   # past the check
                self.assertIn(f'::error::merge-up: {why}', r.stdout)
                self.assert_no_comment()
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        # the wrapper changes nothing else: the same PR merges
        self.assert_merged(self.auto())

    def test_a_post_check_that_fails_after_the_merge_leaves_no_stop_comment(self):
        # 2a review (port review, nit 1): once merged, a red post-check says nothing about the head:
        # no "stopped ... Nothing was merged" on a merged PR (AUTO_STOP_HEAD is cleared at the merge)
        head = self.ready_pr()
        work = os.path.join(self.fx.top, 'late-pusher')
        git(self.fx.top, 'clone', '-q', self.fx.bare, work)
        write(os.path.join(self.fx.state, 'pr-merge-hook'),
              f'set -e\ncd {work}\ngit fetch -q origin\ngit checkout -q -B {PARENT} origin/{PARENT}\n'
              f'echo late > late.txt\ngit add late.txt\n'
              f'git -c user.name=t -c user.email=t@x commit -q -m "a push right after the merge"\ngit push -q origin {PARENT}\n')
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'merged PR {REPO}#1 (head {head})', r.stdout)
        self.assertIn('::error::merge-up: post-check: ', r.stdout)
        self.assertEqual(self.stops(), [])
        self.assertEqual(len([b for b in self.comments() if f'merge-up-auto-finish: merged head={head}' in b]), 1)

    def test_the_gone_check_reads_only_this_childs_pr_into_this_parent(self):
        # 2a review (port review, nit 1): a closed PR of that number ends the run green only when its
        # head is merge-up/<child>/* and its base is the parent line
        self.ready_pr()
        for name, change, args in (('another head', {'head': 'feature/x'}, ()),
                                   ('another base', {'base': 'another-line'}, ('--parent', PARENT))):
            with self.subTest(name):
                prs = self.fx.prs()
                keep = dict(prs[0])
                prs[0].update(change, state='CLOSED')
                write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
                try:
                    r = self.auto(*args)
                finally:
                    prs[0] = keep
                    write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn('nothing to finish', r.stdout)
                self.assertIn(f'::error::merge-up: PR #1 is not an open merge-up/{CHILD}/* PR against {PARENT}', r.stdout)
                self.assert_no_comment()
        # the same closed PR on this child's head into the parent: green and quiet
        prs = self.fx.prs()
        prs[0]['state'] = 'CLOSED'
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
        r = self.auto('--parent', PARENT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to finish', r.stdout)

    def ready_pr_again(self, head):
        self.view(statusCheckRollup=GREEN)
        self.reviews(stamped('alice', commit=head))

    def test_transient_refusals_leave_no_comment(self):
        head = self.ready_pr()
        cases = {
            'checks-pending': lambda: self.view(statusCheckRollup=[dict(GREEN[0], status='IN_PROGRESS', conclusion=''), GREEN[1]]),
            'merge-state-unknown': lambda: self.view(statusCheckRollup=GREEN, mergeStateStatus='UNKNOWN'),
            'changes-requested': lambda: self.reviews(stamped('alice', commit=head), stamped('bob', 'CHANGES_REQUESTED', commit=head)),
        }
        for code, arrange in cases.items():
            with self.subTest(code=code):
                self.ready_pr_again(head)
                arrange()
                r = self.auto()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        not ready: {code}: ', r.stdout)
                self.assertIn('nothing marks this head, so the next sweep reads it again', r.stdout)
                self.assert_no_comment()
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def test_a_permission_that_cannot_be_read_leaves_no_comment(self):
        # 2a review, should-fix 1: a failed permission read is no refusal of the head
        self.ready_pr()
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({}))   # alice: HTTP 404
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('auto-finish:        not ready: permission-unreadable: the permission of alice could not be read', r.stdout)
        self.assertIn('nothing marks this head, so the next sweep reads it again', r.stdout)
        self.assert_no_comment()
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def test_a_required_check_from_elsewhere_leaves_no_comment(self):
        # 2a review, trust should-fix 2: the gate counts a required check only from its source; a refusal
        # for that marks nothing (a newer run from the right source can follow on the same head)
        head = self.ready_pr()
        # pr-checks green in the rollup as a commit status; the check runs have another check only
        self.view(statusCheckRollup=[{'__typename': 'StatusContext', 'context': 'pr-checks', 'state': 'SUCCESS'}, GREEN[1]])
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([
            {'id': 3001, 'name': 'another-check', 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
             'completed_at': '2026-11-20T11:40:00Z', 'app': {'slug': 'some-app'}, 'check_suite': {'id': 1001}}]))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'auto-finish:        not ready: check-source-missing: pr-checks: no check run of that name on the head {head[:7]}', r.stdout)
        self.assert_no_comment()
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        log = self.fx.gh_log()
        for call in (f'api --paginate repos/{REPO}/commits/{head}/check-runs?per_page=100',
                     f'api --paginate repos/{REPO}/actions/runs?head_sha={head}&per_page=100'):
            self.assertIn(call, log)
        # the check runs cannot be read: not ready, no comment
        os.remove(os.path.join(self.fx.state, 'check-runs.json'))
        self.view(statusCheckRollup=GREEN)
        write(os.path.join(self.fx.state, 'fail-check-runs'), '')
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'auto-finish:        the check runs or the Actions runs of {head[:7]} could not be read (gh: Bad Gateway (HTTP 502))', r.stdout)
        self.assertIn('not ready: check-runs-unreadable', r.stdout)
        self.assert_no_comment()
        # read, and from their sources: it merges, and the PR checks run's jobs were read
        os.remove(os.path.join(self.fx.state, 'fail-check-runs'))
        self.assert_merged(self.auto())
        self.assertIn(f'api --paginate repos/{REPO}/actions/runs/2000/jobs?filter=all&per_page=100', self.fx.gh_log())

    def test_a_newer_success_of_another_pr_does_not_hide_this_ones_failure(self):
        # 2c review, trust should-fix 1: a second PR from the same head branch into another base gets a real
        # pull_request run of pr-checks.yml; it succeeds after this PR's run failed
        head = self.ready_pr()
        branch = self.fx.prs()[0]['head']
        # what the stub lists by default (default_check_runs): pr-checks from run 2000
        runs = [{'id': 3000, 'name': 'pr-checks', 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
                 'completed_at': '2026-11-20T11:40:00Z', 'app': {'slug': 'github-actions'}, 'check_suite': {'id': 1000}}]
        actions = [{'id': 2000, 'path': '.github/workflows/pr-checks.yml', 'event': 'pull_request', 'head_branch': branch,
                    'head_sha': head, 'check_suite_id': 1000, 'head_repository': {'full_name': REPO},
                    'pull_requests': [{'number': 1, 'base': {'ref': PARENT}}]}]
        theirs = dict(runs[0], id=3999, check_suite={'id': 1999}, completed_at='2026-11-20T13:00:00Z')
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([dict(runs[0], conclusion='failure'), theirs]))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(
            actions + [dict(actions[0], id=2999, check_suite_id=1999, pull_requests=[{'number': 9, 'base': {'ref': 'attacker'}}])]))
        write(os.path.join(self.fx.state, 'jobs-2999.json'), json.dumps([{'id': 3999, 'name': 'pr-checks'}]))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('auto-finish:        not ready: check-source-failed: pr-checks: its newest check run ended failure', r.stdout)
        self.assert_no_comment()
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        # only this PR's run had its jobs read
        self.assertNotIn(f'repos/{REPO}/actions/runs/2999/jobs', self.fx.gh_log())
        # the other PR's run alone, its own run gone: no run of this PR
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([theirs]))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps(
            [dict(actions[0], id=2999, check_suite_id=1999, pull_requests=[{'number': 9, 'base': {'ref': 'attacker'}}])]))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('auto-finish:        not ready: check-source-missing: pr-checks: no check run of that name comes from a '
                      f'.github/workflows/pr-checks.yml run for this pull request (#1 into {PARENT} from ', r.stdout)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def test_another_pr_of_the_head_branch_leaves_no_comment(self):
        # 2c re-review, should-fix 1: GitHub lists every open PR of the branch in each run of it. While another PR of the
        # branch has another base (open or closed), or this PR's base was ever changed, no run vouches: not ready, no mark
        head = self.ready_pr()
        branch = self.fx.prs()[0]['head']
        owner = REPO.split('/')[0]
        nine = lambda state: [{'number': 9, 'state': state, 'head': {'ref': branch, 'repo': {'full_name': REPO}}, 'base': {'ref': 'attacker'}}]
        extra = os.path.join(self.fx.state, 'head-prs-extra.json')
        for state in ('open', 'closed'):
            with self.subTest(other=state):
                write(extra, json.dumps(nine(state)))
                r = self.auto()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        not ready: check-source-other-pr: #9 ({state}) into attacker also comes from {branch}', r.stdout)
                self.assert_no_comment()
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        for call in (f'api --paginate repos/{REPO}/pulls?state=all&head={owner}:{branch}&per_page=100',
                     f'api --paginate repos/{REPO}/issues/1/events?per_page=100'):
            self.assertIn(call, self.fx.gh_log())
        os.remove(extra)
        write(os.path.join(self.fx.state, 'issue-events.json'), json.dumps({f'{REPO}#1': [{'event': 'base_ref_changed'}]}))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('auto-finish:        not ready: check-source-base-changed: the base of #1 was changed', r.stdout)
        self.assert_no_comment()
        os.remove(os.path.join(self.fx.state, 'issue-events.json'))
        # a read that fails: said, not ready, no mark
        for flag, said in (('fail-head-prs', f'the pull requests of {branch} could not be read (gh: Bad Gateway (HTTP 502))'),
                           ('fail-issue-events', f'the events of PR {REPO}#1 could not be read (gh: Bad Gateway (HTTP 502))')):
            with self.subTest(flag=flag):
                write(os.path.join(self.fx.state, flag), '')
                r = self.auto()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        {said}', r.stdout)
                self.assertIn('auto-finish:        not ready: check-source-unreadable', r.stdout)
                self.assert_no_comment()
                os.remove(os.path.join(self.fx.state, flag))
        # read again before the merge: a PR opened, or a read that fails, after the first read stops the merge
        write(os.path.join(self.fx.state, 'head-prs-extra-next.json'), json.dumps(nine('open')))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)
        self.assertIn('auto-finish:        changed before the merge: check-source-other-pr: ', r.stdout)
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        self.assert_no_comment()
        os.remove(extra)
        for flag, said in (('fail-head-prs', f'the pull requests of {branch} could not be read again before the merge'),
                           ('fail-issue-events', 'its events could not be read again before the merge')):
            with self.subTest(next=flag):
                write(os.path.join(self.fx.state, flag + '-next'), '')
                r = self.auto()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)
                self.assertIn(f'::error::merge-up: PR {REPO}#1: {said} (gh: Bad Gateway (HTTP 502)); nothing was merged', r.stdout)
                self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
                self.assert_no_comment()
                os.remove(os.path.join(self.fx.state, flag))
        # an earlier PR of the branch into the same base, the release App's and never retargeted, is no other source:
        # it merges
        write(extra, json.dumps([dict(nine('closed')[0], number=0, base={'ref': PARENT}, user={'login': self.BOT, 'type': 'Bot'})]))
        os.remove(os.path.join(self.fx.state, 'log'))
        self.assert_merged(self.auto())
        # read by auto_ready and again by auto_recheck, and the events of #0 too
        for call in (f'api --paginate repos/{REPO}/pulls?state=all', f'api --paginate repos/{REPO}/issues/0/events?per_page=100'):
            self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith(call)), 2, call)

    def same_base(self, n, user, state='closed'):
        """Another PR of PR 1's head branch into its base, as REST lists it, opened by user."""
        return {'number': n, 'state': state, 'user': {'login': user, 'type': 'Bot' if user.endswith('[bot]') else 'User'},
                'head': {'ref': self.fx.prs()[0]['head'], 'repo': {'full_name': REPO}}, 'base': {'ref': PARENT}}

    def assert_not_merged(self, r):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assert_no_comment()
        self.assertEqual(self.parent_now(), self.parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def test_a_same_base_pr_of_the_head_branch(self):
        # round 4 review, finding 1, at the gate: M (#9) was opened by a person into another base, retargeted onto the
        # parent and closed; #1 was reopened. #1's own pr-checks failed, and M's newer green run lists #1 into the
        # parent. Refused, not ready and no mark, whoever opened M: a person's same-base PR is another source; so is
        # the App's once its base changed
        head = self.ready_pr()
        branch = self.fx.prs()[0]['head']
        run = lambda cid, suite, conclusion, done: {'id': cid, 'name': 'pr-checks', 'head_sha': head, 'status': 'completed',
                                                    'conclusion': conclusion, 'started_at': '2026-11-20T11:30:00Z', 'completed_at': done,
                                                    'app': {'slug': 'github-actions'}, 'check_suite': {'id': suite}}
        actions = lambda rid, suite: {'id': rid, 'path': '.github/workflows/pr-checks.yml', 'event': 'pull_request', 'head_branch': branch,
                                      'head_sha': head, 'status': 'completed', 'conclusion': 'success', 'check_suite_id': suite,
                                      'head_repository': {'full_name': REPO},
                                      'pull_requests': [{'number': 1, 'head': {'ref': branch, 'sha': head}, 'base': {'ref': PARENT}}]}
        write(os.path.join(self.fx.state, 'check-runs.json'), json.dumps([run(3000, 1000, 'failure', '2026-11-20T11:40:00Z'),
                                                                          run(3999, 1999, 'success', '2026-11-20T13:00:00Z')]))
        write(os.path.join(self.fx.state, 'head-runs.json'), json.dumps([actions(2000, 1000), actions(2999, 1999)]))
        write(os.path.join(self.fx.state, 'jobs-2000.json'), json.dumps([{'id': 3000, 'name': 'pr-checks'}]))
        write(os.path.join(self.fx.state, 'jobs-2999.json'), json.dumps([{'id': 3999, 'name': 'pr-checks'}]))
        extra = os.path.join(self.fx.state, 'head-prs-extra.json')
        events = os.path.join(self.fx.state, 'issue-events.json')
        moved = [{'event': 'base_ref_changed', 'created_at': '2026-11-20T12:00:00Z'}]
        reopened = [{'event': 'closed'}, {'event': 'reopened'}]
        for user, theirs, said in (('mallory', moved, 'opened by mallory, not the release App'),
                                   ('mallory', [], 'opened by mallory, not the release App'),
                                   (self.BOT, moved, 'whose base was changed (2026-11-20T12:00:00Z)')):
            with self.subTest(user=user, events=len(theirs)):
                write(extra, json.dumps([self.same_base(9, user)]))
                write(events, json.dumps({f'{REPO}#9': theirs, f'{REPO}#1': reopened}))
                r = self.auto()
                self.assertIn(f'auto-finish:        not ready: check-source-other-pr: #9 (closed) into {PARENT}, {said} '
                              f'also comes from {branch}', r.stdout)
                self.assert_not_merged(r)
        # the App's M, never retargeted, ran the same checks against the same base: its newest run vouches
        write(events, json.dumps({f'{REPO}#9': [{'event': 'closed'}], f'{REPO}#1': reopened}))
        write(extra, json.dumps([self.same_base(9, self.BOT)]))
        # its events could not be read: said, not ready, no mark
        write(os.path.join(self.fx.state, 'fail-issue-events-9'), '')
        r = self.auto()
        self.assertIn(f'auto-finish:        the events of PR {REPO}#9, another PR of {branch} into {PARENT}, could not be read '
                      '(gh: Bad Gateway (HTTP 502))', r.stdout)
        self.assertIn(f'auto-finish:        not ready: check-source-unreadable: the events of #9, a PR of {branch} into {PARENT} '
                      'the release App opened, could not be read', r.stdout)
        self.assert_not_merged(r)
        os.remove(os.path.join(self.fx.state, 'fail-issue-events-9'))
        # read fine the first time, not again before the merge: stops unmarked
        write(os.path.join(self.fx.state, 'fail-issue-events-9-next'), '')
        r = self.auto()
        self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)
        self.assertIn(f'::error::merge-up: PR {REPO}#1: the events of {REPO}#9, another PR of {branch} into {PARENT}, could not '
                      'be read again before the merge (gh: Bad Gateway (HTTP 502)); nothing was merged', r.stdout)
        self.assert_not_merged(r)
        os.remove(os.path.join(self.fx.state, 'fail-issue-events-9'))
        os.remove(os.path.join(self.fx.state, 'log'))
        self.assert_merged(self.auto())
        self.assertEqual(sum(1 for l in self.fx.gh_log().splitlines() if l.startswith(f'api --paginate repos/{REPO}/issues/9/events')), 2)

    def test_a_base_change_after_the_first_read_stops_the_merge(self):
        # round 4 review, finding 3: auto_recheck reads the events again before the merge. A base change of this PR,
        # or of the App's other PR of the branch into the parent, between the two reads stops the merge, unmarked
        head = self.ready_pr()
        events = os.path.join(self.fx.state, 'issue-events.json')
        later = os.path.join(self.fx.state, 'issue-events-next.json')
        moved = [{'event': 'base_ref_changed', 'created_at': '2026-11-20T12:30:00Z'}]
        extra = os.path.join(self.fx.state, 'head-prs-extra.json')
        for who, code in ((1, 'check-source-base-changed'), (9, 'check-source-other-pr')):
            with self.subTest(changed=who):
                if who == 9:
                    write(extra, json.dumps([self.same_base(9, self.BOT)]))
                write(events, json.dumps({f'{REPO}#9': []}))
                write(later, json.dumps({f'{REPO}#{who}': moved}))
                r = self.auto()
                self.assertIn(f'auto-finish:        approved by alice on the head {head[:7]}', r.stdout)
                self.assertIn(f'auto-finish:        changed before the merge: {code}: ', r.stdout)
                self.assertIn(f'::error::merge-up: PR {REPO}#1 changed before the merge ({code}); nothing was merged and nothing '
                              'marks this head', r.stdout)
                self.assert_not_merged(r)
                self.assertFalse(os.path.exists(later))

    def test_the_activity_is_never_replaced_by_the_commits(self):
        self.ready_pr()
        os.remove(os.path.join(self.fx.state, 'activity.json'))
        write(os.path.join(self.fx.state, 'logins.json'), json.dumps({'t@x': ['someone', 'User']}))
        r = self.auto()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('an auto-finish never falls back to the commits', r.stdout)
        self.assertIn('not ready: activity-unreadable', r.stdout)
        # no commit is read for its author (the head's check runs, repos/R/commits/<sha>/check-runs, are another read)
        self.assertNotRegex(self.fx.gh_log(), r'repos/\S+/commits/[0-9a-f]{40}(\s|$)')
        self.assert_no_comment()

    def test_unreadable_comments_stop_without_a_comment(self):
        self.ready_pr()
        write(os.path.join(self.fx.state, 'fail-issues'), '')
        r = self.auto()
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'PR {REPO}#1: its comments could not be read (HTTP 502: Bad Gateway)', r.stdout)
        self.assert_no_comment()

    def test_unknown_is_read_again(self):
        head = self.ready_pr()
        unknown = dict(reviewDecision='APPROVED', mergeStateStatus='UNKNOWN', statusCheckRollup=GREEN)
        clean = dict(unknown, mergeStateStatus='CLEAN')
        # the gone check and the PR lookup read the view first, then the finish twice
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps([unknown, unknown, unknown, clean]))
        self.assert_merged(self.auto())
        self.assertEqual(len(self.stops()), 0)
        self.assertIn(f'merge-up-auto-finish: merged head={head}', '\n'.join(self.comments()))

    def test_a_pr_already_merged_or_closed_is_quiet(self):
        self.ready_pr()
        for state in ('MERGED', 'CLOSED'):
            with self.subTest(state=state):
                prs = self.fx.prs()
                prs[0]['state'] = state
                write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))
                os.remove(os.path.join(self.fx.state, 'log'))
                r = self.auto()
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f'auto-finish:        PR {REPO}#1 is {state} now; nothing to finish', r.stdout)
                # before the green lookup and anything else
                self.assertEqual([l.split(' -R ')[0] for l in self.fx.gh_log().splitlines()], ['pr view 1'])
                self.assert_no_comment()
        # by hand the same PR is refused as before: no open merge-up PR
        r = self.finish()
        self.assertEqual(r.returncode, 1)

    def test_a_dry_run_writes_no_comment(self):
        self.ready_pr()
        r = self.auto(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: would gh pr merge 1', r.stdout)
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({}))
        r = self.auto(dry_run='true')
        self.assertEqual(r.returncode, 1)
        self.assert_no_comment()
        self.assertNotIn('pr comment', self.fx.gh_log())

    def test_a_failure_outside_the_finish_leaves_no_comment(self):
        self.ready_pr()
        write(self.fx.green, '')   # the child has no green RC run: the run stops before the PR is read
        write(os.path.join(self.fx.state, 'permissions.json'), json.dumps({}))
        r = self.auto()
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run', r.stdout)
        self.assert_no_comment()

    def test_a_failed_comment_is_a_warning(self):
        head = self.ready_pr()
        write(os.path.join(self.fx.state, 'fail-pr-comment'), '')
        r = self.auto()
        self.assert_merged(r)
        self.assertIn(f'::warning::merge-up: the merged PR {REPO}#1 got no auto-finish comment (HTTP 502: Bad Gateway)', r.stdout)
        self.assertEqual(git(self.fx.bare, 'rev-parse', self.parent_now() + '^2'), head)


class StillOpenTests(FinishCase):
    """A PR that GitHub still shows open after the bot merged it is closed (6.1; meta-dsensos #182)."""

    def test_a_merged_pr_left_open_is_closed(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('alice', commit=head))
        write(os.path.join(self.fx.state, 'leave-open-after-merge'), '')
        r = self.finish()
        self.assert_merged(r)
        tip = self.parent_now()
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'`{PARENT}` holds this PR\'s head in the merge commit {tip}', pr['close_comment'])
        self.assertIn('GitHub still showed the PR as open after the merge', pr['close_comment'])
        self.assertNotIn('Superseded', pr['close_comment'])
        self.assertIn(f'PR {REPO}#1 was still open after its merge; closed it with a comment naming the merge commit {tip}', r.stdout)

    def test_a_pr_github_closed_itself_is_left_alone(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('alice', commit=head))
        r = self.finish()
        self.assert_merged(r)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        self.assertNotIn('pr close', self.fx.gh_log())
        self.assertNotIn('still open', r.stdout)

    def test_a_close_that_fails_is_a_warning(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('alice', commit=head))
        write(os.path.join(self.fx.state, 'leave-open-after-merge'), '')
        write(os.path.join(self.fx.state, 'fail-pr-close'), '')
        r = self.finish()
        self.assert_merged(r)
        self.assertIn(f'::warning::merge-up: PR {REPO}#1 is still open after its merge at {self.parent_now()} and could not be closed', r.stdout)
        self.assertIn('close it by hand', r.stdout)


class DefaultBranchTests(FinishCase):
    """Every finish instruction says to dispatch on the default branch (6.1): a run uses the
    scripts and the config of the ref it is dispatched on."""

    def test_the_conflict_pr_and_the_error_say_so(self):
        self.conflict_pr()
        body = self.fx.prs()[0]['body']
        self.assertIn(f'- Dispatch release-merge-up.yml on the default branch with child=`{CHILD}`, pr=<this PR\'s number> and dry_run=false (recommended', body)
        self.assertIn('- Merge this PR from GitHub. The required checks still apply, but none of the checks above do.', body)
        self.assertIn('then finish it one of two ways:', body)
        # the re-run's error line, and with the workflow's DEFAULT_BRANCH the branch by name
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertIn(f'then dispatch release-merge-up.yml with child={CHILD} pr=1 on the default branch\n', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', CHILD, extra_env={'DEFAULT_BRANCH': PARENT})
        self.assertIn(f'then dispatch release-merge-up.yml with child={CHILD} pr=1 on the default branch ({PARENT})\n', r.stdout)

    def test_a_new_pr_names_the_default_branch(self):
        self.child_work()
        self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', CHILD, extra_env={'DEFAULT_BRANCH': PARENT})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'- Dispatch release-merge-up.yml on the default branch ({PARENT}) with child=`{CHILD}`', self.fx.prs()[0]['body'])

    def test_a_conflict_without_markers_says_so_too(self):
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.repo, 'push', '-q', 'origin', CHILD)
        write(self.fx.green, git(self.fx.repo, 'rev-parse', 'HEAD') + '\n')
        self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        body = self.fx.prs()[0]['body']
        self.assertIn('no text markers', body)
        self.assertIn('Dispatch release-merge-up.yml on the default branch with child=', body)
        self.assertIn('then finish it one of two ways', body)
        self.assertIn('Merge this PR from GitHub. The required checks still apply', body)

    def test_no_instruction_in_the_script_leaves_the_ref_open(self):
        with open(MERGE_UP) as f:
            text = f.read()
        # case-insensitive: the no-marker body's list item starts with "Dispatch" (2026-11-19 wording)
        lines = [line for line in text.splitlines() if 'dispatch release-merge-up.yml' in line.lower()]
        # the header comment has none: it says "a pr=N dispatch". One is the dead-branch stop for a
        # PR list that could not be read (its other form says "a dispatch of ... $ON_DEFAULT"). The
        # PR bodies share one line, finish_choices's "- Dispatch ..." (2026-11-20: the marker and
        # rule-resolved bodies use it too), so the count went from 8 to 6. bot-finish (ENG-2235) adds
        # two, both before the inputs: the body of a PR the bot merges itself and the stop for a
        # person's change request, so 8.
        self.assertEqual(len(lines), 8)
        for line in lines:
            # the PR bodies say it before the inputs, the ::error:: lines after them
            self.assertEqual(line.lower().count('dispatch release-merge-up.yml'), 1, line)
            self.assertRegex(line, r'(?i)dispatch release-merge-up\.yml (\$ON_DEFAULT with child=|with child=\$CHILD pr=\$\{[^}]+\} \$ON_DEFAULT"$)')
        # "a person approves it and dispatches ..." (a bot finish's stop, ENG-2235) names the ref too
        uses = text.count('dispatches release-merge-up.yml')
        self.assertGreater(uses, 0)
        self.assertEqual(text.count('dispatches release-merge-up.yml $ON_DEFAULT with child='), uses)

    def test_a_finish_on_another_ref_warns(self):
        head = self.resolved_pr()
        self.view()
        self.reviews(review('alice', commit=head))
        r = self.finish(dry_run='true', extra_env={'DEFAULT_BRANCH': 'some/line'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('::warning::merge-up: this finish runs the scripts and release/config.json of ', r.stdout)
        self.assertIn(', not of the default branch some/line. Dispatch finishes on some/line', r.stdout)
        # a merge-up that finishes nothing does not warn, and neither does a finish on the default branch
        r = self.fx.run(MERGE_UP, '--child', CHILD, dry_run='true', extra_env={'DEFAULT_BRANCH': 'some/line'})
        self.assertNotIn('this finish runs', r.stdout)
        # the ref merge-up.sh names: the checkout's branch, or GITHUB_REF_NAME when it is detached,
        # as actions/checkout leaves a pull_request job (at the PR merge commit)
        ref = t.subprocess.run(['git', '-C', os.path.dirname(SCRIPTS), 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                               capture_output=True, text=True).stdout.strip() or 'from-the-runner'
        r = self.finish(dry_run='true', extra_env={'GITHUB_REF_NAME': 'from-the-runner', 'DEFAULT_BRANCH': ref})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('::warning::', r.stdout)
        self.assertNotIn('::warning::', self.finish(dry_run='true').stdout)

    def test_the_workflow_says_so(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up.yml')) as f:
            text = f.read()
        notice = text[text.index('  merge-up-skipped:'):]
        self.assertIn('DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n', notice)
        self.assertIn('--ref %s -f child=%s -f dry_run=false` (on the default branch)', notice)
        self.assertIn('"${DEFAULT_BRANCH}" "${CHILD}"', notice)
        self.assertNotIn('"${CHILD}" "${CHILD}"', notice)
        self.assertIn('Run the workflow from the default branch"', text)


WORKFLOW = os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/pr-checks.yml')
STEP_NAME = '      - name: No conflict markers on a merge-up branch\n'
MARKED = '::error::this merge-up PR still has conflict markers in: '


def marker_step():
    """pr-checks.yml (text), its marker step (from the name to the blank line that ends it) and the
    step's run: block dedented, which is the script the runner hands to bash."""
    with open(WORKFLOW) as f:
        text = f.read()
    start = text.index(STEP_NAME)
    step = text[start:text.index('\n\n', start)]
    run = '        run: |\n'
    lines = step[step.index(run) + len(run):].split('\n')
    assert all(line.startswith(' ' * 10) for line in lines), 'the run block of the marker step changed shape'
    return text, step, '\n'.join(line[10:] for line in lines) + '\n'


def escape(value):
    """A workflow command's message as it must be written: %, CR and LF as %25, %0D and %0A."""
    return value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def unescape(value):
    """What the runner reads back from an escaped message (in the runner's order)."""
    return value.replace('%0D', '\r').replace('%0A', '\n').replace('%25', '%')


def decode(raw):
    """Git's bytes as Python names them: a file name need not be UTF-8."""
    return raw.decode('utf-8', 'surrogateescape')


def run_step(script, cwd, base, head, branch='merge-up/x', path=None):
    """The marker step as the runner runs it: its script as a file to `bash -e`, in the checkout, with
    the names of its env: block and the event's GITHUB_HEAD_REF set; path replaces PATH.
    (exit status, the files its ::error:: line names as one string or None, stdout).
    The runner reads stdout a line at a time (CR, LF and CRLF each end one) and takes a line that
    starts with :: as a command: only the step's own ::error:: lines may, whatever the file names."""
    fd, file = tempfile.mkstemp(suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write(script)
    try:
        env = dict(os.environ, BASE_SHA=base, HEAD_SHA=head, GITHUB_HEAD_REF=branch)
        if path is not None:
            env['PATH'] = path
        r = t.subprocess.run([shutil.which('bash'), '-e', file], cwd=cwd, env=env, capture_output=True)
    finally:
        os.unlink(file)
    out = decode(r.stdout)
    marked, tail = None, f'. Resolve them on {branch} and push'
    for line in re.split(r'\r\n|\r|\n', out):
        assert not line.startswith('::') or line.startswith('::error::'), f'a line the runner takes as a command: {line!r}'
        if line.startswith(MARKED):
            text = unescape(line[len(MARKED):])
            assert text.endswith(tail), line
            marked = text[:-len(tail)]
    return r.returncode, marked, out


class MarkerTests(unittest.TestCase):
    """release_git.py markers, the rule the pr-checks step applies on a merge-up/** head (6.1)."""

    CONFLICT = 'top\n<<<<<<< HEAD\nours\n||||||| base\nold\n=======\ntheirs\n>>>>>>> child\nend\n'

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='markers-')
        self.addCleanup(shutil.rmtree, self.top, True)
        git(self.top, 'init', '-q', '-b', 'main')
        # a doc on the base that shows a conflict, and a file the branch will delete
        self.commit({'docs/conflicts.md': self.CONFLICT, 'gone.txt': self.CONFLICT, 'a.txt': 'a\n'}, 'base')
        self.base = git(self.top, 'rev-parse', 'HEAD')
        git(self.top, 'checkout', '-q', '-b', 'merge-up/x')

    def commit(self, files, msg):
        for path, text in files.items():
            write(os.path.join(self.top, path), text)
        git(self.top, 'add', '-A')
        git(self.top, 'commit', '-q', '-m', msg)
        return git(self.top, 'rev-parse', 'HEAD')

    def cli(self, base, head):
        return t.subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'markers', '--base', base, '--head', head,
                                 '--git-dir', self.top], capture_output=True, text=True)

    def test_markers_in_changed_files_are_found(self):
        git(self.top, 'rm', '-q', 'gone.txt')
        head = self.commit({'a.txt': self.CONFLICT, 'dir/b c.txt': 'x\n>>>>>>> child\n', 'clean.txt': 'fine\n'}, 'conflict commit')
        self.assertEqual(sorted(release_git.marked_files(self.top, self.base, head)), ['a.txt', 'dir/b c.txt'])
        r = self.cli(self.base, head)
        self.assertEqual((r.returncode, sorted(r.stdout.splitlines())), (1, ['a.txt', 'dir/b c.txt']))
        self.assertIn('2 file(s) changed by', r.stderr)

    def test_each_marker_line_counts_alone(self):
        for line in ('<<<<<<< HEAD', '||||||| base', '>>>>>>> child'):
            with self.subTest(line=line):
                head = self.commit({'a.txt': f'x\n{line}\ny\n'}, line)
                self.assertEqual(release_git.marked_files(self.top, self.base, head), ['a.txt'])
        # not a marker: a separator line, a marker without its space, a marker inside a line
        head = self.commit({'a.txt': '=======\n<<<<<<<\n  <<<<<<< x\n>>>>>>>>\n'}, 'lookalikes')
        self.assertEqual(release_git.marked_files(self.top, self.base, head), [])

    def test_a_resolved_branch_is_clean(self):
        self.commit({'a.txt': self.CONFLICT}, 'conflict commit')
        head = self.commit({'a.txt': 'both\n'}, 'resolve')
        self.assertEqual(release_git.marked_files(self.top, self.base, head), [])
        r = self.cli(self.base, head)
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def test_only_what_the_branch_changes_is_read(self):
        head = self.commit({'a.txt': 'changed\n'}, 'a change')
        # docs/conflicts.md and gone.txt hold markers on the base and are not this PR's
        self.assertEqual(release_git.marked_files(self.top, self.base, head), [])
        # the base moving on changes nothing: the comparison is against the merge base
        git(self.top, 'checkout', '-q', 'main')
        moved = self.commit({'other.txt': self.CONFLICT}, 'base moves')
        self.assertEqual(release_git.marked_files(self.top, moved, head), [])

    def test_a_commit_that_cannot_be_read_fails(self):
        r = self.cli(self.base, 'f' * 40)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, '')

    def test_the_finish_and_the_check_use_one_pattern(self):
        # merge-up.sh's finish, this command and the pr-checks step grep for one pattern
        with open(MERGE_UP) as f:
            self.assertIn("grep -l -E '" + release_git.MARKER_RE + "'", f.read())
        self.assertIn("git grep -l -z -E '" + release_git.MARKER_RE + "' \"${HEAD_SHA}\"", marker_step()[2])


class PrChecksWorkflowTests(unittest.TestCase):
    """pr-checks.yml refuses markers on a merge-up/** head, so the UI cannot merge them either."""

    # sensor-portal and ds carry this step, the comment above it and the fetch-depth: 0 checkout it
    # needs byte for byte (this file is the same in both), and a line that takes the step copies the
    # hunk unchanged, so that the line's merge-up merges pr-checks.yml clean (workflow files are never_auto: a conflict there always
    # needs a person). A change to the step goes into both repos together, with a new digest here.
    STEP_SHA256 = 'f68f613badd23ad45d6b3a7b38072308e955177b96fad2cd475895d73b20ccee'

    def setUp(self):
        self.text, self.step, self.script = marker_step()

    def test_the_step(self):
        self.assertIn("        if: github.event_name == 'pull_request' && startsWith(github.head_ref, 'merge-up/')\n", self.step)
        self.assertIn('BASE_SHA: ${{ github.event.pull_request.base.sha }}\n', self.step)
        self.assertIn('HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n', self.step)
        # the shas come in through env: only, and are checked as 40-hex before git sees either
        self.assertNotIn('${{', self.script)
        self.assertIn('=~ ^[0-9a-f]{40}$ ]]', self.script)
        self.assertLess(self.script.index('=~ ^[0-9a-f]{40}$ ]]'), self.script.index('git '))
        self.assertIn('if ! merge_base="$(git merge-base "${BASE_SHA}" "${HEAD_SHA}")"; then\n', self.script)
        self.assertIn('git diff -z --name-only --diff-filter=d "${merge_base}" "${HEAD_SHA}"', self.script)
        # every value in an ::error:: line goes through esc, so a file name cannot end the line
        self.assertIn("esc() { local s=\"${1//[%]/%25}\"; s=\"${s//$'\\r'/%0D}\"; printf '%s' \"${s//$'\\n'/%0A}\"; }\n", self.script)
        self.assertIn('''echo "::error::${name} is not a 40-hex commit sha: '$(esc "${!name:-}")'"\n''', self.script)
        self.assertIn('echo "::error::this merge-up PR still has conflict markers in: $(esc "${marked[*]}"). '
                      'Resolve them on $(esc "${GITHUB_HEAD_REF}") and push"\n  exit 1\n', self.script)
        # MarkerStepTests runs it and compares it with release_git.py markers

    def test_a_line_can_carry_the_step_unchanged(self):
        # bash and git alone: nothing from scripts-release/, which a line older than the merge-up
        # tooling does not have (MarkerStepTests runs it with git as the only program on PATH)
        for word in ('python', 'scripts-release', 'release_git', 'merge_rules'):
            self.assertNotIn(word, self.script)
        # but it needs the history: a line's plain checkout is depth 1 (MarkerStepTests runs the
        # step there), so the carried hunk starts at the checkout's fetch-depth: 0 and the comment
        # above the step says the two go together
        start = self.text.index('      # Full history: the marker check below')
        hunk = self.text[start:self.text.index(STEP_NAME) + len(self.step)]
        self.assertTrue(hunk.startswith('      # Full history: the marker check below compares the PR head with its\n'
                                        '      # merge base.\n'
                                        '      - uses: actions/checkout@v4\n'
                                        '        with:\n'
                                        '          fetch-depth: 0\n\n'
                                        '      # A merge-up conflict PR is opened with the markers committed'), hunk)
        comment = hunk[hunk.index('      # A merge-up conflict PR'):hunk.index(STEP_NAME)]
        self.assertIn('fetch-depth: 0', comment)
        self.assertIn('together', comment)
        self.assertEqual(hashlib.sha256(hunk.encode()).hexdigest(), self.STEP_SHA256,
                         'the marker step changed: change it the same way in sensor-portal and ds, then update STEP_SHA256')

    def test_the_job(self):
        # in a job the required pr-checks job needs, after a full-history checkout and before the tests
        jobs = self.text[self.text.index('\njobs:'):]
        tooling = jobs[jobs.index('\n  release-tooling:'):jobs.index('\n  checks:')]
        self.assertIn(self.step, tooling)
        self.assertLess(tooling.index('fetch-depth: 0'), tooling.index('- name: No conflict markers'))
        self.assertLess(tooling.index('- name: No conflict markers'), tooling.index('- name: Check tools'))
        checks = jobs[jobs.index('\n  checks:'):]
        self.assertIn('    name: pr-checks\n', checks)
        self.assertIn('    needs: [build, release-tooling]\n', checks)
        self.assertIn('    if: ${{ always() }}\n', checks)


class MarkerStepTests(unittest.TestCase):
    """pr-checks.yml's marker step, run as the runner runs it against fixture repos, agrees with
    release_git.py markers on the same base and head: the same exit status and the same files, in
    the same order. The step is bash and git alone so that a line older than the merge-up tooling
    can carry it unchanged, which leaves these tests as what keeps the two the same."""

    CONFLICT = MarkerTests.CONFLICT

    def setUp(self):
        self.script = marker_step()[2]
        self.top = tempfile.mkdtemp(prefix='marker-step-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.repo = os.path.join(self.top, 'repo')
        os.makedirs(self.repo)
        git(self.repo, 'init', '-q', '-b', 'main')
        # on the base: a doc that shows a conflict, a marked file the branch may delete, and a
        # marked file that the pattern odd/*.txt would match
        self.base = self.commit({'docs/conflicts.md': self.CONFLICT, 'gone.txt': self.CONFLICT, 'odd/x.txt': self.CONFLICT,
                                 'a.txt': 'a\n'}, 'base')
        git(self.repo, 'checkout', '-q', '-b', 'merge-up/x')

    def commit(self, files, msg, rm=()):
        for path in rm:
            git(self.repo, 'rm', '-q', path)
        for path, data in files.items():
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb' if isinstance(data, bytes) else 'w') as f:
                f.write(data)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', msg)
        return git(self.repo, 'rev-parse', 'HEAD')

    def step(self, base, head, **kw):
        return run_step(self.script, self.repo, base, head, **kw)

    def agree(self, base, head):
        """Runs both on <base> and <head>, asserts they agree, and returns (exit status, files).
        The files are release_git.marked_files' list (a name may hold a newline, which one name per
        line cannot show), and the command prints exactly those, as bytes."""
        r = t.subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'markers', '--base', base, '--head', head,
                              '--git-dir', self.repo], capture_output=True)
        try:
            files = release_git.marked_files(self.repo, base, head)
        except release_git.Error:
            files = []
        self.assertEqual(r.stdout, b''.join(f.encode('utf-8', 'surrogateescape') + b'\n' for f in files), decode(r.stderr))
        self.assertNotIn(b'Traceback', r.stderr)
        rc, marked, out = self.step(base, head)
        self.assertEqual(rc, r.returncode, out)
        self.assertEqual(marked, ' '.join(files) if files else None, out)
        return rc, files

    def test_a_clean_branch(self):
        head = self.commit({'a.txt': 'changed\n', 'new.txt': 'new\n'}, 'clean')
        self.assertEqual(self.agree(self.base, head), (0, []))
        self.assertIn('no conflict markers in the 2 file(s) this PR changes\n', self.step(self.base, head)[2])

    def test_markers_in_changed_files(self):
        head = self.commit({'a.txt': self.CONFLICT, 'dir/b c.txt': 'x\n>>>>>>> child\n', 'clean.txt': 'fine\n'}, 'conflict commit')
        self.assertEqual(self.agree(self.base, head), (1, ['a.txt', 'dir/b c.txt']))

    def test_markers_only_in_files_the_branch_does_not_change(self):
        head = self.commit({'a.txt': 'changed\n'}, 'a change')
        self.assertEqual(self.agree(self.base, head), (0, []))
        # the base moving on changes nothing, a marked file added or one cleaned there (which the
        # head still has as the merge base had it): the comparison is against the merge base
        git(self.repo, 'checkout', '-q', 'main')
        moved = self.commit({'other.txt': self.CONFLICT, 'docs/conflicts.md': 'cleaned on the base\n'}, 'base moves')
        self.assertEqual(self.agree(moved, head), (0, []))

    def test_each_marker_line_alone_and_the_zdiff3_base_line(self):
        for line in ('||||||| base', '<<<<<<< HEAD', '>>>>>>> child'):
            with self.subTest(line=line):
                head = self.commit({'a.txt': f'x\n{line}\ny\n'}, line)
                self.assertEqual(self.agree(self.base, head), (1, ['a.txt']))
        # not markers: the separator, markers without their space or inside a line, eight of them
        head = self.commit({'a.txt': '=======\n<<<<<<<\n|||||||\n  <<<<<<< x\n>>>>>>>>\n'}, 'lookalikes')
        self.assertEqual(self.agree(self.base, head), (0, []))

    def test_a_deleted_file_is_not_read(self):
        head = self.commit({}, 'delete gone.txt', rm=['gone.txt'])
        self.assertEqual(self.agree(self.base, head), (0, []))
        self.assertIn('no conflict markers in the 0 file(s) this PR changes\n', self.step(self.base, head)[2])

    def test_a_binary_file(self):
        head = self.commit({'bin.dat': b'\x00\x01\n<<<<<<< HEAD\n\x00\xff\n', 'clean.dat': b'\x00\x01\n=======\n'}, 'binary')
        self.assertEqual(self.agree(self.base, head), (1, ['bin.dat']))

    def test_a_rename(self):
        git(self.repo, 'mv', 'docs/conflicts.md', 'docs/moved.md')   # unchanged, its markers too
        git(self.repo, 'mv', 'a.txt', 'b.txt')
        head = self.commit({}, 'renames')
        self.assertIn('R100\tdocs/conflicts.md\tdocs/moved.md', git(self.repo, 'diff', '-M', '--name-status', self.base, head))
        self.assertEqual(self.agree(self.base, head), (1, ['docs/moved.md']))
        # the same answer when the checkout's git does not detect renames
        git(self.repo, 'config', 'diff.renames', 'false')
        self.assertEqual(self.agree(self.base, head), (1, ['docs/moved.md']))

    def test_names_are_paths_not_patterns(self):
        # as a pattern odd/*.txt would match odd/x.txt, which is marked but not changed
        head = self.commit({'odd/*.txt': 'fine\n', 'a:b.txt': self.CONFLICT, '-x.txt': '>>>>>>> child\n'}, 'odd names')
        self.assertEqual(self.agree(self.base, head), (1, ['-x.txt', 'a:b.txt']))

    def test_more_paths_than_one_git_grep_takes(self):
        files = {f'many/f{i:03}.txt': 'x\n' for i in range(205)}
        files['many/f000.txt'] = files['many/f204.txt'] = self.CONFLICT
        head = self.commit(files, 'many')
        self.assertEqual(self.agree(self.base, head), (1, ['many/f000.txt', 'many/f204.txt']))

    def test_a_commit_that_cannot_be_read(self):
        self.assertEqual(self.agree(self.base, 'f' * 40), (1, []))
        self.assertIn(f'::error::no merge base of the base {self.base} and the head {"f" * 40}', self.step(self.base, 'f' * 40)[2])

    def test_the_shas_are_checked_before_git_sees_them(self):
        head = self.commit({'a.txt': self.CONFLICT}, 'conflict commit')
        for bad in ('--output=x', 'merge-up/x', head[:7], head.upper(), '', head + '\n' + head, head + '\r\n::warning::x%'):
            with self.subTest(bad=bad):
                self.assertEqual(self.step(bad, head), (1, None, f"::error::BASE_SHA is not a 40-hex commit sha: '{escape(bad)}'\n"))
                self.assertEqual(self.step(self.base, bad), (1, None, f"::error::HEAD_SHA is not a 40-hex commit sha: '{escape(bad)}'\n"))

    def test_a_name_that_is_not_utf8(self):
        # git names files in bytes; release_git.py markers read its output as UTF-8 text and failed
        name = decode(b'caf\xe9.txt')
        head = self.commit({name: 'clean\n'}, 'latin-1 name')
        self.assertEqual(self.agree(self.base, head), (0, []))
        self.assertIn('no conflict markers in the 1 file(s) this PR changes\n', self.step(self.base, head)[2])
        head = self.commit({name: self.CONFLICT, 'a.txt': self.CONFLICT}, 'latin-1 name with markers')
        self.assertEqual(self.agree(self.base, head), (1, ['a.txt', name]))

    def test_a_name_cannot_start_a_workflow_command(self):
        # the names go into the ::error:: line escaped (% CR LF), so the runner reads one line and
        # gives back the names as they are (run_step fails on any other line starting with ::)
        names = ['cr\r::warning::cr', 'lf\n::warning::lf', 'pct%0A%25.txt', 'tail\n']
        head = self.commit({n: self.CONFLICT for n in names}, 'names with line ends')
        self.assertEqual(self.agree(self.base, head), (1, sorted(names)))
        rc, marked, out = self.step(self.base, head, branch='merge-up/%0A::warning::ref')
        self.assertEqual((rc, marked), (1, ' '.join(sorted(names))), out)
        self.assertEqual(len(re.split(r'\r\n|\r|\n', out.rstrip('\n'))), 1, out)

    def test_a_plain_checkout_is_not_enough(self):
        # a line's actions/checkout without fetch-depth: 0 fetches the PR merge commit alone; the step
        # has no merge base there and fails rather than passing, which is why a line carries the
        # checkout change with it (PrChecksWorkflowTests)
        head = self.commit({'a.txt': self.CONFLICT}, 'conflict commit')
        merge = git(self.repo, 'commit-tree', f'{head}^{{tree}}', '-p', self.base, '-p', head, '-m', 'pr merge')
        git(self.repo, 'branch', '-f', 'pr-merge', merge)
        work = os.path.join(self.top, 'work')
        git(self.top, 'clone', '-q', '--depth', '1', '--branch', 'pr-merge', 'file://' + self.repo, work)
        self.assertEqual(git(work, 'rev-list', '--count', 'HEAD'), '1')
        rc, marked, out = run_step(self.script, work, self.base, head)
        self.assertEqual((rc, marked), (1, None), out)
        self.assertIn(f'::error::no merge base of the base {self.base} and the head {head}: the check needs both commits with their history', out)
        # with the full history the same step finds the file
        git(work, 'fetch', '-q', '--unshallow', 'origin', 'merge-up/x', 'main')
        self.assertEqual(run_step(self.script, work, self.base, head)[:2], (1, 'a.txt'))

    def test_bash_and_git_alone(self):
        # git is the only program on PATH, and the checkout has no scripts-release/
        bin_dir = os.path.join(self.top, 'bin')
        os.makedirs(bin_dir)
        os.symlink(shutil.which('git'), os.path.join(bin_dir, 'git'))
        head = self.commit({'a.txt': self.CONFLICT}, 'conflict commit')
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'scripts-release')))
        rc, marked, out = self.step(self.base, head, path=bin_dir)
        self.assertEqual((rc, marked), (1, 'a.txt'), out)
        head = self.commit({'a.txt': 'both\n'}, 'resolve')
        rc, marked, out = self.step(self.base, head, path=bin_dir)
        self.assertEqual((rc, marked, out), (0, None, 'no conflict markers in the 1 file(s) this PR changes\n'))


BOT = {'GIT_AUTHOR_NAME': 'bot', 'GIT_AUTHOR_EMAIL': 'bot@x', 'GIT_COMMITTER_NAME': 'bot', 'GIT_COMMITTER_EMAIL': 'bot@x'}


class BranchOriginTests(unittest.TestCase):
    """release_git.branch_origin: is the first commit above the parent the bot's merge? (6.1)"""

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='origin-')
        self.addCleanup(shutil.rmtree, self.top, True)
        git(self.top, 'init', '-q', '-b', 'main')
        self.commit('a.txt', 'base\n', 'base')
        git(self.top, 'branch', 'child')
        self.parent = self.commit('a.txt', 'parent\n', 'parent work')
        git(self.top, 'checkout', '-q', 'child')
        self.child = self.commit('b.txt', 'child\n', 'child work')

    def commit(self, path, text, msg, env=None):
        write(os.path.join(self.top, path), text)
        git(self.top, 'add', '-A')
        git(self.top, 'commit', '-q', '-m', msg, env=env or {})
        return git(self.top, 'rev-parse', 'HEAD')

    def merge(self, onto, other, branch, env=None, extra=None):
        """<other> merged into a new branch at <onto>; extra: a file changed in the same commit."""
        git(self.top, 'checkout', '-q', '-B', branch, onto)
        git(self.top, 'merge', '-q', '--no-ff', '--no-commit', other, env=env or {})
        if extra:
            write(os.path.join(self.top, extra[0]), extra[1])
            git(self.top, 'add', '-A')
        git(self.top, 'commit', '-q', '-m', f'Merge into {branch}', env=env or {})
        return git(self.top, 'rev-parse', 'HEAD')

    def origin(self, head, like=None, bot_email=None, parent='main'):
        return release_git.branch_origin(self.top, head, parent, like, bot_email)

    def test_the_bots_merge_is_ours(self):
        bot = self.merge(self.parent, self.child, 'pr', BOT)
        # this run made the same merge again, at another time: another sha, the same commit
        like = self.merge(self.parent, self.child, 'again', dict(BOT, GIT_COMMITTER_DATE='2026-01-01T00:00:00+00:00'))
        self.assertNotEqual(bot, like)
        self.assertEqual(self.origin(bot, like), ('ours', bot, ''))
        self.assertEqual(self.origin(bot, bot_email='bot@x'), ('ours', bot, ''))

    def test_a_resolvers_commits_on_top_leave_it_ours(self):
        bot = self.merge(self.parent, self.child, 'pr', BOT)
        like = self.merge(self.parent, self.child, 'again', BOT)
        git(self.top, 'checkout', '-q', 'pr')
        self.commit('a.txt', 'resolved\n', 'resolve')
        # the parent moves and the resolver merges it into the branch
        git(self.top, 'checkout', '-q', 'main')
        self.commit('c.txt', 'more\n', 'parent moves')
        git(self.top, 'checkout', '-q', 'pr')
        git(self.top, 'merge', '-q', '--no-ff', '-m', 'merge main again', 'main')
        head = git(self.top, 'rev-parse', 'HEAD')
        self.assertEqual(self.origin(head, like, parent=self.parent), ('ours', bot, ''))
        self.assertEqual(self.origin(head, bot_email='bot@x'), ('ours', bot, ''))

    def test_someone_elses_merge_is_foreign(self):
        theirs = self.merge(self.parent, self.child, 'pr')   # committed by t@x
        like = self.merge(self.parent, self.child, 'again', BOT)
        state, first, why = self.origin(theirs, like)
        self.assertEqual((state, first), ('foreign', theirs))
        self.assertIn(f'{theirs[:7]}, was committed by t@x, not by the bot', why)
        self.assertEqual(self.origin(theirs, bot_email='bot@x')[0], 'foreign')

    def test_another_tree_is_foreign(self):
        # the bot's identity on a merge that holds something this run did not compute
        seeded = self.merge(self.parent, self.child, 'pr', BOT, extra=('a.txt', 'slipped in\n'))
        like = self.merge(self.parent, self.child, 'again', BOT)
        state, first, why = self.origin(seeded, like)
        self.assertEqual((state, first), ('foreign', seeded))
        self.assertIn('does not hold the tree this run computed', why)
        # a finish computes no merge, so it can only go by the committer
        self.assertEqual(self.origin(seeded, bot_email='bot@x')[0], 'ours')

    def test_a_merge_onto_another_parent_tip_is_foreign(self):
        old = self.merge(self.parent, self.child, 'pr', BOT)
        git(self.top, 'checkout', '-q', 'main')
        moved = self.commit('c.txt', 'more\n', 'parent moves')
        like = self.merge(moved, self.child, 'again', BOT)
        state, first, why = self.origin(old, like, parent=moved)
        self.assertEqual((state, first), ('foreign', old))
        self.assertIn(f'merges {self.child[:7]} onto {self.parent[:7]}, while this run merges {self.child[:7]} onto {moved[:7]}', why)

    def test_a_plain_commit_or_nothing_above_the_parent_is_foreign(self):
        git(self.top, 'checkout', '-q', '-B', 'pr', self.parent)
        plain = self.commit('a.txt', 'by hand\n', 'by hand', BOT)
        self.assertEqual(self.origin(plain, bot_email='bot@x'), ('foreign', plain, f'its first commit above the parent line, {plain[:7]}, is not a merge of two lines'))
        self.assertEqual(self.origin(self.parent, bot_email='bot@x'), ('foreign', '', 'it holds no commit above the parent line'))
        # the child itself (the branch of a conflict that leaves no markers): its oldest own commit
        self.assertEqual(self.origin(self.child, bot_email='bot@x')[0], 'foreign')

    def test_cli(self):
        bot = self.merge(self.parent, self.child, 'pr', BOT)
        run = lambda *a: t.subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'branch-origin', *a, '--git-dir', self.top],
                                          capture_output=True, text=True)
        r = run(bot, '--parent', 'main', '--bot-email', 'bot@x')
        self.assertEqual((r.returncode, r.stdout), (0, f'ours {bot}\n'))
        r = run(bot, '--parent', 'main', '--bot-email', 'someone@x')
        self.assertEqual((r.returncode, r.stdout.splitlines()[0]), (0, f'foreign {bot}'))
        self.assertIn('was committed by bot@x, not by the bot', r.stdout.splitlines()[1])
        self.assertEqual(run('nonsense', '--parent', 'main').returncode, 1)


NOT_THIS_RUN = '**This branch was not produced by this run.**'


class ProvenanceTests(FinishCase):
    """A merge-up/<child>/<sha7> branch that is already on origin (6.1)."""

    def seed(self, resolve_to='slipped in\n'):
        """Someone pushes merge-up/<child>/<sha7> before the bot gets there: the child merged
        into the parent by hand, foo.txt decided, no markers."""
        self.child_work()
        self.parent_tip = self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        self.branch = f'merge-up/{CHILD}/{self.green_sha[:7]}'
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', self.branch, f'origin/{PARENT}')
        t.subprocess.run(['git', '-C', self.fx.repo, 'merge', '-q', '--no-ff', '--no-commit', f'origin/{CHILD}'], capture_output=True)
        write(os.path.join(self.fx.repo, 'foo.txt'), resolve_to)
        git(self.fx.repo, 'checkout', f'origin/{PARENT}', '--', 'versions/ds', t.ENV_FILE)
        git(self.fx.repo, 'add', '-A')
        git(self.fx.repo, 'commit', '-q', '-m', 'Merge by hand')
        git(self.fx.repo, 'push', '-q', 'origin', self.branch)
        git(self.fx.repo, 'checkout', '-q', '--detach')
        return git(self.fx.repo, 'rev-parse', self.branch)

    def test_a_foreign_branch_is_labelled_and_left_alone(self):
        seeded = self.seed()
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{self.branch} already exists on origin; left as it is', r.stdout)
        self.assertIn(f'{self.branch} was not produced by this run: its first commit above the parent line, {seeded[:7]}, was committed by t@x, not by the bot. '
                      'No rule and no bot finish applies to it', r.stdout)
        self.assertEqual(self.fx.heads()[self.branch], seeded)
        self.assertEqual(self.parent_now(), self.parent_tip)
        pr = self.fx.prs()[0]
        self.assertEqual((pr['head'], pr['state']), (self.branch, 'OPEN'))
        self.assertIn(NOT_THIS_RUN, pr['body'])
        self.assertIn(f'`{self.branch}` already existed on origin at `{seeded[:7]}`', pr['body'])
        self.assertIn('the bot does not merge it on its own', pr['body'])
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])
        # it is a person's PR like any other: it finishes only with a person's approval
        self.pr_head = seeded
        r = self.finish()
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.view()
        self.reviews(review('alice', commit=seeded))
        self.assert_merged(self.finish())

    def test_the_bots_own_branch_is_not_labelled(self):
        _, _, branch = self.conflict_pr()
        self.assertNotIn(NOT_THIS_RUN, self.fx.prs()[0]['body'])
        # a resolver works on it, the bot closes the PR as superseded, and the next run opens a new one
        # on the branch (a PR a person closed makes the branch dead instead: test_dead_branch.py)
        self.resolve(branch)
        self.fx.supersede_pr(1)
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertNotIn('was not produced by this run', r.stdout)
        pr = self.fx.prs()[1]
        self.assertEqual((pr['head'], pr['state']), (branch, 'OPEN'))
        self.assertNotIn(NOT_THIS_RUN, pr['body'])

    def test_an_earlier_runs_branch_on_an_older_parent_is_labelled(self):
        _, parent_tip, branch = self.conflict_pr()
        self.fx.supersede_pr(1)
        moved = self.fx.commit(PARENT, {'bar.txt': 'more\n'}, 'main moves')
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'onto {parent_tip[:7]}, while this run merges {self.child_tip[:7]} onto {moved[:7]}', r.stdout)
        self.assertIn(NOT_THIS_RUN, self.fx.prs()[1]['body'])

    def test_a_foreign_branch_for_a_conflict_without_markers(self):
        # modify/delete leaves no markers, so the bot's branch would be the child itself
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.repo, 'push', '-q', 'origin', CHILD)
        child_tip = git(self.fx.repo, 'rev-parse', 'HEAD')
        write(self.fx.green, child_tip + '\n')
        parent_tip = self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        branch = f'merge-up/{CHILD}/{child_tip[:7]}'
        git(self.fx.bare, 'update-ref', f'refs/heads/{branch}', parent_tip)   # seeded: not the child
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{branch} was not produced by this run: it does not contain {CHILD} at {child_tip[:7]}', r.stdout)
        self.assertIn(NOT_THIS_RUN, self.fx.prs()[0]['body'])
        self.assertEqual(self.fx.heads()[branch], parent_tip)
        # the bot's own branch for that shape, on a re-run after the bot closed the PR as superseded: no label
        git(self.fx.bare, 'update-ref', f'refs/heads/{branch}', child_tip)
        self.fx.supersede_pr(1)
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('was not produced by this run', r.stdout)
        self.assertNotIn(NOT_THIS_RUN, self.fx.prs()[1]['body'])


# --- the child line's bookkeeping (ENG-2234, decided 2026-11-20) -------------------------------

BOOKKEEPING_STEP_NAME = '      - name: No child bookkeeping on a merge-up branch\n'
CARRIED = "::error::this merge-up PR would carry the child line's bookkeeping into "
# the bookkeeping files the step names besides versions/ (release/config.json bookkeeping_files)
STEP_BOOKKEEPING = ['.github/sdk.env']


def bookkeeping_step():
    """pr-checks.yml (text), its bookkeeping step (from the name to the blank line that ends it)
    and the step's run: block dedented, which is the script the runner hands to bash."""
    with open(WORKFLOW) as f:
        text = f.read()
    start = text.index(BOOKKEEPING_STEP_NAME)
    step = text[start:text.index('\n\n', start)]
    run = '        run: |\n'
    lines = step[step.index(run) + len(run):].split('\n')
    assert all(line.startswith(' ' * 10) for line in lines), 'the run block of the bookkeeping step changed shape'
    return text, step, '\n'.join(line[10:] for line in lines) + '\n'


def carried_tail(base, head, files):
    return (f' against its merge base, and a merge-up keeps the parent\'s copy. Restore it on {head} and push: '
            f'git checkout origin/{base} -- {files}, then commit (if {base} moved since {head} last merged it, '
            f'merge origin/{base} into {head} first)')


def run_bookkeeping_step(script, cwd, base, head, base_ref='main', branch='merge-up/x', path=None):
    """The bookkeeping step as the runner runs it (see run_step), with GITHUB_BASE_REF set too.
    (exit status, the files its ::error:: line names as one string or None, stdout)."""
    fd, file = tempfile.mkstemp(suffix='.sh')
    with os.fdopen(fd, 'w') as f:
        f.write(script)
    try:
        env = dict(os.environ, BASE_SHA=base, HEAD_SHA=head, GITHUB_HEAD_REF=branch, GITHUB_BASE_REF=base_ref)
        if path is not None:
            env['PATH'] = path
        r = t.subprocess.run([shutil.which('bash'), '-e', file], cwd=cwd, env=env, capture_output=True)
    finally:
        os.unlink(file)
    out = decode(r.stdout)
    files = None
    for line in re.split(r'\r\n|\r|\n', out):
        assert not line.startswith('::') or line.startswith('::error::'), f'a line the runner takes as a command: {line!r}'
        if line.startswith(CARRIED):
            text = unescape(line[len(CARRIED):])
            lead = f'{base_ref}: it changes '
            assert text.startswith(lead), line
            files = text[len(lead):text.index(' against its merge base, and a merge-up keeps')]
            assert text == lead + files + carried_tail(base_ref, branch, files), line
    return r.returncode, files, out


class BookkeepingRepo:
    """A repo with the parent line main (versions/portal, versions/README.md, .github/sdk.env) and
    the child line 1.x.x cut from it, which moved its own versions file and sdk.env, as a line
    does. The commits a merge-up PR head can be are made with the helpers below."""

    def __init__(self, top, bookkeeping=('.github/sdk.env',)):
        self.dir = os.path.join(top, 'repo')
        os.makedirs(self.dir)
        git(self.dir, 'init', '-q', '-b', 'main')
        # GitHub serves any reachable commit by sha
        git(self.dir, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        self.bookkeeping = list(bookkeeping)
        self.fork = self.commit('main', {'versions/portal': '4.0.0\n', 'versions/README.md': '# versions\n',
                                         '.github/sdk.env': 'SDK_OS_LINE=2.x.x\n', 'foo.txt': 'trunk\n'}, 'seed', new=True)
        self.child = self.commit('1.x.x', {'versions/portal': '3.1.0\n', '.github/sdk.env': 'SDK_OS_LINE=1.x.x\n'},
                                 'open 1.x.x', start='main')
        self.child = self.commit('1.x.x', {'foo.txt': 'child fix\n'}, 'child fix')
        self.parent = self.commit('main', {'foo.txt': 'main fix\n'}, 'main fix')

    def commit(self, branch, files, msg, start=None, new=False, rm=()):
        if not new:
            git(self.dir, 'checkout', '-q', '-B', branch, start or branch)
        for path in rm:
            git(self.dir, 'rm', '-q', path)
        for path, data in files.items():
            write(os.path.join(self.dir, path), data)
        git(self.dir, 'add', '-A')
        git(self.dir, 'commit', '-q', '--allow-empty', '-m', msg)
        return git(self.dir, 'rev-parse', 'HEAD')

    def merge(self, ours, theirs, keep=(), take=None, resolve=None, msg='merge'):
        """A merge commit: <ours> checked out, <theirs> merged in. keep: paths restored from HEAD
        (what merge-keep-target does), take: {path: rev} restored from another commit; resolve:
        {path: text} written by hand (a conflict decided). Returns the commit."""
        git(self.dir, 'checkout', '-q', '--detach', ours)
        t.subprocess.run(['git', '-C', self.dir, 'merge', '-q', '--no-ff', '--no-commit', theirs], capture_output=True,
                         env=dict(os.environ, **t.TEST_IDENTITY))
        for path in keep:
            git(self.dir, 'checkout', 'HEAD', '--', path)
        for path, rev in (take or {}).items():
            git(self.dir, 'checkout', rev, '--', path)
        for path, text in (resolve or {}).items():
            write(os.path.join(self.dir, path), text)
            git(self.dir, 'add', path)
        git(self.dir, 'commit', '-q', '-m', msg)
        return git(self.dir, 'rev-parse', 'HEAD')

    def show(self, rev, path):
        return git(self.dir, 'show', f'{rev}:{path}')

    def bot_merge(self, parent=None, child=None, resolve=None):
        """What merge-up.sh commits: the parent checked out, the child merged in, merge-keep-target."""
        return self.merge(parent or self.parent, child or self.child, keep=['versions/portal'] + self.bookkeeping,
                          resolve=resolve or {'foo.txt': 'both fixes\n'})


class BookkeepingTests(unittest.TestCase):
    """release_git.py bookkeeping: what a merge-up PR head changes of the parent's bookkeeping."""

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='bookkeeping-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.r = BookkeepingRepo(self.top)

    def changes(self, head, parent=None):
        return release_git.bookkeeping_changes(self.r.dir, parent or self.r.parent, head, self.r.bookkeeping)

    def test_the_bots_merges_leave_them_alone(self):
        self.assertEqual(self.changes(self.r.bot_merge()), [])
        # the marker commit: foo.txt still marked, versions/ and sdk.env the parent's
        marked = self.r.bot_merge(resolve={'foo.txt': MarkerTests.CONFLICT})
        self.assertEqual(self.changes(marked), [])

    def test_the_child_commit_itself_carries_them(self):
        self.assertEqual(self.changes(self.r.child), ['.github/sdk.env', 'versions/portal'])

    def test_the_wrong_way_round_carries_them(self):
        wrong = self.r.merge(self.r.child, self.r.parent, resolve={'foo.txt': 'both fixes\n'})
        self.assertEqual(self.changes(wrong), ['.github/sdk.env', 'versions/portal'])
        # restoring the parent's copy on the branch, as the refusal says, passes
        git(self.r.dir, 'checkout', '-q', '-B', 'fix', wrong)
        git(self.r.dir, 'checkout', self.r.parent, '--', '.github/sdk.env', 'versions/portal')
        git(self.r.dir, 'commit', '-q', '-m', 'restore the parent\'s bookkeeping')
        fixed = git(self.r.dir, 'rev-parse', 'HEAD')
        self.assertEqual(self.changes(fixed), [])

    def test_one_file_kept_from_the_child(self):
        head = self.r.merge(self.r.parent, self.r.child, keep=['versions/portal'], resolve={'foo.txt': 'both\n'})
        self.assertEqual(self.changes(head), ['.github/sdk.env'])

    def test_the_parent_moving_on(self):
        head = self.r.bot_merge()
        bumped = self.r.commit('main', {'versions/portal': '4.1.0\n', '.github/sdk.env': 'SDK_OS_LINE=3.x.x\n'}, 'bump', start=self.r.parent)
        # before the branch merges it in: the merge base is the old parent tip, which the head keeps
        self.assertEqual(self.changes(head, bumped), [])
        # after: the merge base is the new tip, whose files merged in
        merged = self.r.merge(head, bumped)
        self.assertEqual(self.r.show(merged, 'versions/portal'), '4.1.0')
        self.assertEqual(self.changes(merged, bumped), [])
        # merge-keep-target run with the branch checked out puts the old files back: refused
        wrong = self.r.merge(head, bumped, keep=['versions/portal', '.github/sdk.env'], msg='wrong')
        self.assertEqual(self.changes(wrong, bumped), ['.github/sdk.env', 'versions/portal'])

    def test_what_is_not_the_parents_bookkeeping(self):
        # versions/README.md merges like any doc, a versions file only the child has is not the
        # parent's, and a bookkeeping file the parent lacks is not kept by merge-keep-target either
        head = self.r.commit('h', {'versions/README.md': '# fixed\n', 'versions/newprod': '0.1.0\n'}, 'readme and new',
                             start=self.r.bot_merge())
        self.assertEqual(self.changes(head), [])
        self.assertEqual(release_git.bookkeeping_changes(self.r.dir, self.r.parent, self.r.child, []), ['versions/portal'])
        bare = self.r.commit('main', {}, 'no sdk.env', start=self.r.parent, rm=['.github/sdk.env'])
        self.assertEqual(self.changes(self.r.child, bare), ['versions/portal'])

    def test_a_deletion_and_a_mode_change_count(self):
        head = self.r.commit('h', {}, 'drop', start=self.r.bot_merge(), rm=['versions/portal'])
        self.assertEqual(self.changes(head), ['versions/portal'])
        head = self.r.commit('h', {}, 'restore', start=self.r.bot_merge())
        os.chmod(os.path.join(self.r.dir, '.github/sdk.env'), 0o755)
        head = self.r.commit('h', {}, 'mode', start=head)
        self.assertEqual(self.changes(head), ['.github/sdk.env'])

    def test_cli(self):
        def cli(head):
            return t.subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'bookkeeping', '--parent', self.r.parent,
                                     '--head', head, '--git-dir', self.r.dir, '--root', self.root], capture_output=True, text=True)
        self.root = os.path.join(self.top, 'root')
        t.write_root(self.root, config=dict(t.CONFIG, bookkeeping_files=['.github/sdk.env']))
        r = cli(self.r.child)
        self.assertEqual((r.returncode, r.stdout), (1, '.github/sdk.env\nversions/portal\n'), r.stderr)
        r = cli(self.r.bot_merge())
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))
        # the bookkeeping files come from --root's config
        t.write_root(self.root, config=dict(t.CONFIG, bookkeeping_files=[]))
        r = cli(self.r.child)
        self.assertEqual((r.returncode, r.stdout), (1, 'versions/portal\n'), r.stderr)
        # a commit that cannot be read: exit 1 and nothing on stdout, which merge-up.sh tells from a finding
        r = cli('f' * 40)
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        self.assertIn('release_git.py: git merge-base', r.stderr)


class BookkeepingFinishTests(FinishCase):
    """merge-up.sh --pr refuses a head that changes the parent's versions/<product> or bookkeeping
    file against its merge base with the parent (ENG-2234, decided 2026-11-20). The fixture's
    bookkeeping file is .github/yocto-ci.env (t.ENV_FILE); the child moved it and its versions/ds."""

    def refusal(self, files, head_ref, base, number=1):
        files = ' '.join(files)
        return (f"::error::merge-up: PR {REPO}#{number} would carry {CHILD}'s bookkeeping into {PARENT}: {head_ref} changes "
                f"{files} against its merge base with {PARENT} ({base[:7]}), and a merge-up keeps the parent's copy. "
                f"Restore it on {head_ref} and push: git checkout origin/{PARENT} -- {files}, then commit "
                f"(if {PARENT} moved since {head_ref} last merged it, merge origin/{PARENT} into {head_ref} first)")

    def assert_carried(self, r, files, head_ref, base):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(self.refusal(files, head_ref, base) + '\n', r.stdout)
        self.assertNotIn('would gh pr merge', r.stdout)
        self.assertEqual(self.parent_now(), self.parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.assertEqual(t.unallowed_writes(self.fx.gh_argv()), [])

    def approve(self):
        self.view()
        self.reviews(review('alice', commit=self.fx.heads()[self.branch]))

    def no_marker_pr(self):
        """A modify/delete conflict: the PR goes on the child commit itself (no markers). The child
        moved its versions/ds (the line's opening commit) and the bookkeeping file."""
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        write(os.path.join(self.fx.repo, t.ENV_FILE), 'DS_TEGRA_DISTRO_REF=child\n')
        git(self.fx.repo, 'commit', '-q', '-am', 'drop foo, child env')
        git(self.fx.repo, 'push', '-q', 'origin', CHILD)
        self.child_tip = git(self.fx.repo, 'rev-parse', 'HEAD')
        write(self.fx.green, self.child_tip + '\n')
        self.parent_tip = self.fx.commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')
        self.branch = f'merge-up/{CHILD}/{self.child_tip[:7]}'
        r = self.fx.run(MERGE_UP, '--child', CHILD)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads()[self.branch], self.child_tip)
        self.fork = git(self.fx.bare, 'merge-base', self.parent_tip, self.child_tip)
        self.assertNotEqual(self.fx.show(self.child_tip, 'versions/ds'), self.fx.show(self.parent_tip, 'versions/ds'))

    def push_head(self):
        git(self.fx.repo, 'push', '-q', 'origin', f'HEAD:{self.branch}')
        return git(self.fx.repo, 'rev-parse', 'HEAD')

    def test_the_bots_marker_commit_resolved_finishes(self):
        self.resolved_pr()
        self.approve()
        r = self.finish()
        self.assert_merged(r)
        tip = self.parent_now()
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '0.0.1')
        self.assertEqual(self.fx.show(tip, t.ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')

    def test_an_unresolved_no_marker_pr_is_refused(self):
        self.no_marker_pr()
        self.approve()
        r = self.finish()
        self.assert_carried(r, [t.ENV_FILE, 'versions/ds'], self.branch, self.fork)
        # a dry run prints the same finding
        r = self.finish(dry_run='true')
        self.assert_carried(r, [t.ENV_FILE, 'versions/ds'], self.branch, self.fork)

    def test_a_correct_no_marker_resolution_finishes(self):
        # the PR body's procedure: the parent checked out, the branch merged in, merge-keep-target
        self.no_marker_pr()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', 'resolve', f'origin/{PARENT}')
        t.subprocess.run(['git', 'merge', '--no-ff', '--no-commit', f'origin/{self.branch}'], cwd=self.fx.repo, capture_output=True)
        r = t.subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'merge-keep-target', '--root', self.fx.repo],
                             cwd=self.fx.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', f'Merge {CHILD} into {PARENT}')
        self.pr_head = self.push_head()
        self.approve()
        self.assert_merged(self.finish())
        self.assertEqual(self.fx.show(self.parent_now(), t.ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')

    def test_the_wrong_way_round_is_refused_until_the_parents_copy_is_back(self):
        # the branch checked out, the parent merged in: the child's versions/ds and env file stay
        self.no_marker_pr()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', 'wrong', f'origin/{self.branch}')
        t.subprocess.run(['git', 'merge', '--no-ff', '--no-commit', f'origin/{PARENT}'], cwd=self.fx.repo, capture_output=True)
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', f'Merge {PARENT} into {self.branch}')
        head = self.push_head()
        self.approve()
        r = self.finish()
        self.assert_carried(r, [t.ENV_FILE, 'versions/ds'], self.branch, self.parent_tip)
        # what the refusal says to do: restore the parent's copy, commit, push
        git(self.fx.repo, 'checkout', f'origin/{PARENT}', '--', t.ENV_FILE, 'versions/ds')
        git(self.fx.repo, 'commit', '-q', '-m', "Keep the parent's bookkeeping")
        self.pr_head = self.push_head()
        self.assertNotEqual(self.pr_head, head)
        self.approve()
        self.assert_merged(self.finish())
        self.assertEqual(self.fx.show(self.parent_now(), 'versions/ds'), self.fx.show(self.parent_tip, 'versions/ds'))

    def test_a_resolution_that_kept_the_childs_bookkeeping_file_is_refused(self):
        # the right direction, but the versions file restored by hand instead of merge-keep-target:
        # the env file only the child changed merged clean to the child's copy
        self.no_marker_pr()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', 'resolve', f'origin/{PARENT}')
        t.subprocess.run(['git', 'merge', '--no-ff', '--no-commit', f'origin/{self.branch}'], cwd=self.fx.repo, capture_output=True)
        git(self.fx.repo, 'checkout', 'HEAD', '--', 'versions/ds')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', f'Merge {CHILD} into {PARENT}')
        self.push_head()
        self.approve()
        self.assert_carried(self.finish(), [t.ENV_FILE], self.branch, self.parent_tip)

    def test_the_parent_bumping_its_version_after_the_pr_opened(self):
        head = self.resolved_pr()
        bumped = self.fx.commit(PARENT, {'versions/ds': '2.0.0\n', t.ENV_FILE: 'DS_TEGRA_DISTRO_REF=main2\n'}, 'release: pending 2.0.0 [skip ci]')
        self.parent_tip = bumped
        # before the resolver merges the new parent into the branch
        self.approve()
        self.assert_merged(self.finish())
        tip = self.parent_now()
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^1'), bumped)
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '2.0.0')
        self.assertEqual(self.fx.show(tip, t.ENV_FILE), 'DS_TEGRA_DISTRO_REF=main2')
        self.assertEqual(head, self.pr_head)

    def test_the_parent_bumping_its_version_merged_into_the_branch(self):
        self.resolved_pr()
        bumped = self.fx.commit(PARENT, {'versions/ds': '2.0.0\n', t.ENV_FILE: 'DS_TEGRA_DISTRO_REF=main2\n'}, 'release: pending 2.0.0 [skip ci]')
        self.parent_tip = bumped
        # the resolver merges the moved parent into the branch: a plain merge (no merge-keep-target)
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', self.branch, f'origin/{self.branch}')
        git(self.fx.repo, 'merge', '-q', '--no-edit', f'origin/{PARENT}')
        self.pr_head = self.push_head()
        self.approve()
        self.assert_merged(self.finish())
        self.assertEqual(self.fx.show(self.parent_now(), 'versions/ds'), '2.0.0')

    def test_merge_keep_target_on_the_branch_after_a_parent_bump_is_refused(self):
        # the caution of "Resolving a no-marker PR": merge-keep-target with the branch checked out
        # keeps the branch's (older) files over the parent's new bump
        self.resolved_pr()
        bumped = self.fx.commit(PARENT, {'versions/ds': '2.0.0\n'}, 'release: pending 2.0.0 [skip ci]')
        self.parent_tip = bumped
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', self.branch, f'origin/{self.branch}')
        t.subprocess.run(['git', 'merge', '--no-ff', '--no-commit', f'origin/{PARENT}'], cwd=self.fx.repo, capture_output=True)
        git(self.fx.repo, 'checkout', 'HEAD', '--', 'versions/ds')
        git(self.fx.repo, 'commit', '-q', '-m', 'merge the parent, keep the branch\'s versions')
        self.push_head()
        self.approve()
        self.assert_carried(self.finish(), ['versions/ds'], self.branch, bumped)


class BookkeepingRuleTests(trm.RulesCase):
    """A PR the splice rule resolved in full finishes: the bot's commit keeps the parent's files."""

    def test_a_rule_resolved_pr_finishes(self):
        self.edits({'docs/notes.txt': trm.NOTES_CHILD, t.ENV_FILE: 'DS_TEGRA_DISTRO_REF=child\n'},
                   {'docs/notes.txt': trm.NOTES_PARENT})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads()[self.branch()]
        self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (resolved by rule)')
        self.assertEqual(release_git.bookkeeping_changes(self.fx.bare, self.parent_tip, head, [t.ENV_FILE]), [])
        self.assertEqual(release_git.bookkeeping_changes(self.fx.bare, self.parent_tip, self.child_tip, [t.ENV_FILE]),
                         [t.ENV_FILE, 'versions/ds'])
        self.approve(1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()[PARENT]
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), head)
        self.assertEqual(self.fx.show(tip, t.ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')


class BookkeepingWorkflowTests(unittest.TestCase):
    """pr-checks.yml refuses a merge-up/** head that changes the parent's bookkeeping, so the UI
    cannot merge one either."""

    # sensor-portal and ds carry this step and the comment above it byte for byte; a change goes into
    # both repos together, with a new digest here
    STEP_SHA256 = 'b5ddeca1ea55d88b7196387f19af5ef609f4c56687c77345ac35bc6a7501b479'

    def setUp(self):
        self.text, self.step, self.script = bookkeeping_step()

    def test_the_step(self):
        self.assertIn("        if: github.event_name == 'pull_request' && startsWith(github.head_ref, 'merge-up/')\n", self.step)
        self.assertIn('BASE_SHA: ${{ github.event.pull_request.base.sha }}\n', self.step)
        self.assertIn('HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n', self.step)
        self.assertNotIn('${{', self.script)
        self.assertLess(self.script.index('=~ ^[0-9a-f]{40}$ ]]'), self.script.index('git '))
        self.assertIn('if ! merge_base="$(git merge-base "${BASE_SHA}" "${HEAD_SHA}")"; then\n', self.script)
        # the files are what the parent's tree holds: versions/ but README.md, and the bookkeeping files
        self.assertIn('git ls-tree -z --name-only "${BASE_SHA}" -- versions/ ' + ' '.join(STEP_BOOKKEEPING) + ')\n', self.script)
        self.assertIn('[ "${f}" = versions/README.md ] || files+=("${f}")\n', self.script)
        self.assertIn('git diff -z --no-renames --name-only "${merge_base}" "${HEAD_SHA}" -- "${specs[@]}"', self.script)
        esc = [line for line in marker_step()[2].split('\n') if line.startswith('esc() ')]
        self.assertEqual(len(esc), 1)
        self.assertIn('\n' + esc[0] + '\n', self.script)   # the same esc as the marker step

    def test_the_files_are_the_configs_bookkeeping_files(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), 'release/config.json')) as f:
            self.assertEqual(json.load(f)['bookkeeping_files'], STEP_BOOKKEEPING)

    def test_a_line_can_carry_it_with_the_marker_step(self):
        for word in ('python', 'scripts-release', 'release_git', 'merge_rules', 'config.json'):
            self.assertNotIn(word, self.script)
        # the hunk a line carries: the checkout's fetch-depth: 0, the marker step and this one, one block
        start = self.text.index('      # Full history: the marker check below')
        end = self.text.index(BOOKKEEPING_STEP_NAME) + len(self.step)
        hunk = self.text[start:end]
        marker = self.text[start:self.text.index(STEP_NAME) + len(marker_step()[1])]
        self.assertEqual(hashlib.sha256(marker.encode()).hexdigest(), PrChecksWorkflowTests.STEP_SHA256)
        own = hunk[len(marker):]
        self.assertTrue(own.startswith('\n\n      # A merge-up never carries the child line\'s bookkeeping'), own)
        comment = own[:own.index(BOOKKEEPING_STEP_NAME)]
        self.assertIn('fetch-depth: 0', comment)
        self.assertIn('together', comment)
        self.assertEqual(hashlib.sha256(own.encode()).hexdigest(), self.STEP_SHA256,
                         'the bookkeeping step changed: change it the same way in sensor-portal and ds, then update STEP_SHA256')
        self.assertTrue(self.text[end:].startswith('\n\n      - name: Check tools\n'))

    def test_the_job(self):
        jobs = self.text[self.text.index('\njobs:'):]
        tooling = jobs[jobs.index('\n  release-tooling:'):jobs.index('\n  checks:')]
        self.assertIn(self.step, tooling)
        self.assertLess(tooling.index('- name: No conflict markers'), tooling.index(BOOKKEEPING_STEP_NAME))
        self.assertLess(tooling.index(BOOKKEEPING_STEP_NAME), tooling.index('- name: Check tools'))


class BookkeepingStepTests(unittest.TestCase):
    """pr-checks.yml's bookkeeping step, run as the runner runs it against fixture repos, agrees with
    release_git.py bookkeeping on the same parent and head: the same exit status and the same files,
    in the same order. The step is bash and git alone so that a line older than the merge-up tooling
    can carry it unchanged, which leaves these tests as what keeps the two the same."""

    def setUp(self):
        self.script = bookkeeping_step()[2]
        self.top = tempfile.mkdtemp(prefix='bookkeeping-step-')
        self.addCleanup(shutil.rmtree, self.top, True)
        self.r = BookkeepingRepo(self.top)

    def step(self, base, head, **kw):
        return run_bookkeeping_step(self.script, self.r.dir, base, head, **kw)

    def agree(self, base, head):
        try:
            files = release_git.bookkeeping_changes(self.r.dir, base, head, STEP_BOOKKEEPING)
        except release_git.Error:
            files = []
        rc, found, out = self.step(base, head)
        self.assertEqual(rc, 1 if files else 0, out)
        self.assertEqual(found, ' '.join(files) if files else None, out)
        return rc, files

    def test_the_bots_merges(self):
        self.assertEqual(self.agree(self.r.parent, self.r.bot_merge()), (0, []))
        self.assertEqual(self.agree(self.r.parent, self.r.bot_merge(resolve={'foo.txt': MarkerTests.CONFLICT})), (0, []))
        out = self.step(self.r.parent, self.r.bot_merge())[2]
        self.assertEqual(out, "the PR leaves the parent's 2 bookkeeping file(s) as its merge base has them\n")

    def test_the_child_commit_and_the_wrong_way_round(self):
        self.assertEqual(self.agree(self.r.parent, self.r.child), (1, ['.github/sdk.env', 'versions/portal']))
        wrong = self.r.merge(self.r.child, self.r.parent, resolve={'foo.txt': 'both\n'})
        self.assertEqual(self.agree(self.r.parent, wrong), (1, ['.github/sdk.env', 'versions/portal']))
        kept_env = self.r.merge(self.r.parent, self.r.child, keep=['versions/portal'], resolve={'foo.txt': 'both\n'})
        self.assertEqual(self.agree(self.r.parent, kept_env), (1, ['.github/sdk.env']))

    def test_the_parent_moving_on(self):
        head = self.r.bot_merge()
        bumped = self.r.commit('main', {'versions/portal': '4.1.0\n'}, 'bump', start=self.r.parent)
        self.assertEqual(self.agree(bumped, head), (0, []))
        self.assertEqual(self.agree(bumped, self.r.merge(head, bumped)), (0, []))
        self.assertEqual(self.agree(bumped, self.r.merge(head, bumped, keep=['versions/portal'])), (1, ['versions/portal']))

    def test_what_is_not_the_parents_bookkeeping(self):
        head = self.r.commit('h', {'versions/README.md': '# fixed\n', 'versions/newprod': '0.1.0\n'}, 'readme and new',
                             start=self.r.bot_merge())
        self.assertEqual(self.agree(self.r.parent, head), (0, []))
        bare = self.r.commit('main', {}, 'no sdk.env', start=self.r.parent, rm=['.github/sdk.env'])
        self.assertEqual(self.agree(bare, self.r.child), (1, ['versions/portal']))
        # no versions/ and no sdk.env at all: nothing to compare, the step passes
        empty = self.r.commit('main', {}, 'nothing', start=bare, rm=['versions/portal', 'versions/README.md'])
        self.assertEqual(self.agree(empty, self.r.child), (0, []))
        self.assertIn("the PR leaves the parent's 0 bookkeeping file(s)", self.step(empty, self.r.child)[2])

    def test_a_deletion_counts(self):
        head = self.r.commit('h', {}, 'drop', start=self.r.bot_merge(), rm=['versions/portal'])
        self.assertEqual(self.agree(self.r.parent, head), (1, ['versions/portal']))

    def test_the_message(self):
        rc, found, out = self.step(self.r.parent, self.r.child, base_ref='3.x.x', branch='merge-up/3.0.x/abc1234')
        self.assertEqual(rc, 1)
        self.assertEqual(out, CARRIED + '3.x.x: it changes .github/sdk.env versions/portal'
                         + carried_tail('3.x.x', 'merge-up/3.0.x/abc1234', '.github/sdk.env versions/portal') + '\n')

    def test_a_ref_cannot_start_a_workflow_command(self):
        rc, found, out = self.step(self.r.parent, self.r.child, base_ref='m%0A::warning::x\r', branch='merge-up/%\n::warning::y')
        self.assertEqual((rc, found), (1, '.github/sdk.env versions/portal'), out)
        self.assertEqual(len(re.split(r'\r\n|\r|\n', out.rstrip('\n'))), 1, out)

    def test_a_commit_that_cannot_be_read(self):
        rc, found, out = self.step(self.r.parent, 'f' * 40)
        self.assertEqual((rc, found), (1, None))
        self.assertIn(f'::error::no merge base of the base {self.r.parent} and the head {"f" * 40}', out)

    def test_the_shas_are_checked_before_git_sees_them(self):
        for bad in ('--output=x', 'merge-up/x', self.r.child[:7], '', self.r.child + '\n::warning::x%'):
            with self.subTest(bad=bad):
                self.assertEqual(self.step(bad, self.r.child), (1, None, f"::error::BASE_SHA is not a 40-hex commit sha: '{escape(bad)}'\n"))
                self.assertEqual(self.step(self.r.parent, bad), (1, None, f"::error::HEAD_SHA is not a 40-hex commit sha: '{escape(bad)}'\n"))

    def test_a_plain_checkout_is_not_enough(self):
        # without fetch-depth: 0 the step has no merge base and fails rather than passing
        merge = git(self.r.dir, 'commit-tree', f'{self.r.child}^{{tree}}', '-p', self.r.parent, '-p', self.r.child, '-m', 'pr merge')
        git(self.r.dir, 'branch', '-f', 'pr-merge', merge)
        work = os.path.join(self.top, 'work')
        git(self.top, 'clone', '-q', '--depth', '1', '--branch', 'pr-merge', 'file://' + self.r.dir, work)
        rc, found, out = run_bookkeeping_step(self.script, work, self.r.parent, self.r.child)
        self.assertEqual((rc, found), (1, None), out)
        self.assertIn('the check needs both commits with their history', out)
        git(work, 'fetch', '-q', '--unshallow', 'origin', 'main', '1.x.x')
        self.assertEqual(run_bookkeeping_step(self.script, work, self.r.parent, self.r.child)[:2], (1, '.github/sdk.env versions/portal'))

    def test_bash_and_git_alone(self):
        bin_dir = os.path.join(self.top, 'bin')
        os.makedirs(bin_dir)
        os.symlink(shutil.which('git'), os.path.join(bin_dir, 'git'))
        self.assertEqual(self.step(self.r.parent, self.r.child, path=bin_dir)[:2], (1, '.github/sdk.env versions/portal'))
        self.assertEqual(self.step(self.r.parent, self.r.bot_merge(), path=bin_dir)[:2], (0, None))


if __name__ == '__main__':
    unittest.main()
