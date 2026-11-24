"""auto_finish.py: whether a merge-up PR is ready for an automatic finish (ENG-2235).

One file in every repository that carries the merge-up scripts, like auto_finish.py itself:
it names no repository and no product. The PR is merge-up/x/1.x.x/<sha7> into x/main; the
release App is app[bot]. Standard library only, no network.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import auto_finish  # noqa: E402
import release_git  # noqa: E402

CLI = os.path.join(SCRIPTS, 'auto_finish.py')
APP = 'app[bot]'
HEAD = 'a' * 40
OLD = 'b' * 40
T0, T1, T2, T3 = '2026-11-20T10:00:00Z', '2026-11-20T11:00:00Z', '2026-11-20T12:00:00Z', '2026-11-20T13:00:00Z'
REQUIRED = ['pr-checks', 'other-check']
GREEN = [{'__typename': 'CheckRun', 'name': n, 'workflowName': 'PR checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
         for n in REQUIRED]
TRIGGER = {'__typename': 'CheckRun', 'name': 'review', 'workflowName': auto_finish.REVIEW_TRIGGER}


def view(**over):
    doc = {'number': 7, 'state': 'OPEN', 'isDraft': False, 'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN',
           'headRefOid': HEAD, 'statusCheckRollup': list(GREEN), 'commits': [{'authors': [{'login': 'resolver'}]}]}
    doc.update(over)
    return doc


def review(login, state='APPROVED', kind='User', commit=HEAD, at=T2):
    return {'user': {'login': login, 'type': kind}, 'state': state, 'commit_id': commit, 'submitted_at': at}


def push(login, kind='User', what='push', at=T1):
    return {'activity_type': what, 'actor': {'login': login, 'type': kind}, 'timestamp': at}


CREATED = push(APP, 'Bot', 'branch_creation', T0)


def comment(body, login=APP, at=T3):
    return {'user': {'login': login, 'type': 'Bot' if login.endswith('[bot]') else 'User'}, 'body': body, 'created_at': at}


def run(pr=7, status='completed', conclusion='failure', at=T3, auto=True, repo='repo'):
    title = f'Merge-up x/1.x.x: finish {repo}#{pr}' + (' (auto-finish)' if auto else '')
    return {'id': hash((pr, status, conclusion, at, auto, repo)) & 0xffff, 'display_title': title, 'status': status,
            'conclusion': conclusion if status == 'completed' else None, 'created_at': at,
            'html_url': f'https://example.invalid/runs/{pr}'}


class ReadyCase(unittest.TestCase):
    """A PR that is ready: resolver pushed at T1, alice (write) approved the head at T2."""

    def ready(self, v=None, reviews=None, required=REQUIRED, activity='default', permissions='default',
              comments='default', runs=None, extra=(), check_runs='default', sources='default', head_prs='default', events=(),
              other_events=None):
        # the head's check runs come from their sources (CheckSourceTests has each way they do not)
        return auto_finish.ready(
            v or view(), [review('alice')] if reviews is None else reviews, required, APP,
            activity=[CREATED, push('resolver')] if activity == 'default' else activity,
            permissions={'alice': 'write', 'carol': 'admin', 'resolver': 'write'} if permissions == 'default' else permissions,
            comments=[] if comments == 'default' else comments, runs=runs, extra=extra,
            repo=REPO, base=BASE,
            sources=SOURCES if sources == 'default' else sources, head_ref=HEAD_REF,
            check_runs=CHECK_RUNS if check_runs == 'default' else check_runs, actions_runs=ACTIONS_RUNS, jobs=JOBS,
            head_prs=HEAD_PRS if head_prs == 'default' else head_prs, events=events, other_events=other_events)

    def codes(self, *args, **kw):
        status, reasons, _, _ = self.ready(*args, **kw)
        return status, [c for c, _ in reasons]

    def assert_only(self, code, *args, status='not-ready', **kw):
        got = self.codes(*args, **kw)
        self.assertEqual(got, (status, [code]), self.ready(*args, **kw)[1])


class ReadyTests(ReadyCase):
    """One test per reason code; each changes one thing in a ready PR."""

    def test_the_ready_pr(self):
        status, reasons, counted, notes = self.ready()
        self.assertEqual((status, reasons, counted), ('ready', [], ['alice']))
        self.assertEqual(notes, ['push actors, from the repository activity: resolver'])

    def test_merged_and_closed(self):
        self.assertEqual(self.codes(view(state='MERGED')), ('not-ready', ['merged']))
        self.assertEqual(self.codes(view(state='CLOSED')), ('not-ready', ['closed']))

    def test_draft(self):
        self.assert_only('draft', view(isDraft=True))

    def test_changes_requested(self):
        # next to an approval that would count
        status, reasons, _, _ = self.ready(reviews=[review('alice'), review('bob', 'CHANGES_REQUESTED')])
        self.assertEqual((status, reasons), ('not-ready', [('changes-requested', 'bob requested changes')]))
        # a later approval or a dismissal by the same person takes it back
        self.assertEqual(self.codes(reviews=[review('alice'), review('bob', 'CHANGES_REQUESTED'), review('bob', 'DISMISSED')])[0], 'ready')
        # a bot's no is not a person's
        self.assertEqual(self.codes(reviews=[review('alice'), review('ci[bot]', 'CHANGES_REQUESTED', kind='Bot')])[0], 'ready')

    def test_review_decision(self):
        self.assert_only('review-decision', view(reviewDecision='REVIEW_REQUIRED'))

    def test_stale_approval_strict_whatever_the_config_says(self):
        # nothing passes strict in: an approval of an older commit never counts
        status, codes = self.codes(reviews=[review('alice', commit=OLD)])
        self.assertEqual((status, codes), ('not-ready', ['stale-approval', 'no-approval']))

    def test_approver_pushed(self):
        self.assertEqual(self.codes(activity=[CREATED, push('alice')]), ('not-ready', ['approver-pushed', 'no-approval']))
        # a force push and creating the branch are pushes too
        for what in ('force_push', 'branch_creation'):
            self.assertIn('approver-pushed', self.codes(activity=[CREATED, push('alice', what=what)])[1], what)

    def test_no_approval(self):
        self.assert_only('no-approval', reviews=[])
        self.assertEqual(self.codes(reviews=[review('alice', 'COMMENTED')]), ('not-ready', ['no-approval']))

    def test_a_bot_approval_never_counts(self):
        self.assert_only('no-approval', reviews=[review('ci[bot]', kind='Bot')])
        self.assert_only('no-approval', reviews=[review(APP, kind='Bot')])

    def test_activity_unreadable_and_no_fallback(self):
        # no fallback to the commit authors, ever: commits are not even an input
        self.assert_only('activity-unreadable', activity=None)
        # an activity that lists no push, or a push without a time
        self.assert_only('activity-unreadable', activity=[])
        self.assert_only('activity-unreadable', activity=[CREATED, dict(push('resolver'), timestamp=None)])

    def test_checks_missing(self):
        self.assert_only('checks-missing', view(statusCheckRollup=GREEN[:1]))

    def test_checks_pending(self):
        running = dict(GREEN[1], status='IN_PROGRESS', conclusion='')
        self.assert_only('checks-pending', view(statusCheckRollup=[GREEN[0], running]))
        self.assert_only('checks-pending', view(statusCheckRollup=[GREEN[0], {'context': 'other-check', 'state': 'PENDING'}]))

    def test_checks_skipped(self):
        for c in ('SKIPPED', 'NEUTRAL'):
            self.assert_only('checks-skipped', view(statusCheckRollup=[GREEN[0], dict(GREEN[1], conclusion=c)]))

    def test_checks_failed(self):
        self.assert_only('checks-failed', view(statusCheckRollup=GREEN + [dict(GREEN[0], name='lint', conclusion='FAILURE')]))
        # a required check that failed is named once, as failed
        self.assert_only('checks-failed', view(statusCheckRollup=[GREEN[0], dict(GREEN[1], conclusion='TIMED_OUT')]))

    def test_bot_pushed(self):
        # only the release App's own pushes are set aside; any other bot names no person
        for actor in ('github-actions[bot]', 'other-app[bot]'):
            with self.subTest(actor=actor):
                status, reasons, _, _ = self.ready(activity=[CREATED, push('resolver'), push(actor, 'Bot', at=T1)])
                self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['bot-pushed']))
                self.assertIn(f'{actor} pushed to the branch', reasons[0][1])
        self.assertEqual(self.codes(activity=[CREATED, push(None, 'Bot')])[1], ['bot-pushed'])
        self.assertEqual(self.codes(activity=[CREATED, push(APP, 'Bot', at=T1)])[0], 'ready')

    def test_approved_before_push(self):
        # GitHub moved alice's review to the head after a clean base merge; its submitted_at stays older
        status, reasons, _, _ = self.ready(activity=[CREATED, push('resolver', at=T3)])
        self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['approved-before-push', 'no-approval']))
        self.assertIn(f'alice approved at {T2}, not after the last push to the branch ({T3})', reasons[0][1])
        # the push by anyone counts, the App's included, and an equal time is not after
        self.assertEqual(self.codes(activity=[push(APP, 'Bot', 'branch_creation', T2)])[1], ['approved-before-push', 'no-approval'])
        # no time on the approval: not after
        self.assertIn('approved-before-push', self.codes(reviews=[review('alice', at=None)])[1])
        # approving again after the push counts
        self.assertEqual(self.codes(reviews=[review('alice'), review('alice', at='2026-11-20T14:00:00Z')],
                                    activity=[CREATED, push('resolver', at=T3)])[0], 'ready')

    def test_approver_cannot_write(self):
        status, reasons, _, _ = self.ready(permissions={'alice': 'read'})
        self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['approver-cannot-write', 'no-approval']))
        self.assertIn('alice has read permission, not write', reasons[0][1])
        for perms in ({'alice': 'none'}, {'alice': 'triage'}, {'alice': 'read', 'bob': 'write'}):
            with self.subTest(perms=perms):
                self.assertIn('approver-cannot-write', self.codes(permissions=perms)[1])
        for p in ('write', 'maintain', 'admin'):
            self.assertEqual(self.codes(permissions={'alice': p})[0], 'ready', p)
        # a second approver with write access is enough; the first is a note
        status, _, counted, notes = self.ready(reviews=[review('alice'), review('carol')], permissions={'alice': 'read', 'carol': 'admin'})
        self.assertEqual((status, counted), ('ready', ['carol']))
        self.assertIn('not counted, alice has read permission, not write', notes)

    def test_permission_unreadable(self):
        # a permission read that failed is its own reason, not "cannot write" (2a review, should-fix 1)
        for perms in ({}, None, {'carol': 'admin'}):
            with self.subTest(perms=perms):
                status, reasons, _, _ = self.ready(permissions=perms)
                self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['permission-unreadable', 'no-approval']))
                self.assertEqual(reasons[0][1], 'the permission of alice could not be read')
                # and nothing marks the head for it
                self.assertFalse(auto_finish.stop_due([c for c, _ in reasons]))
        # next to an approver who cannot write, still nothing about the approvals marks
        codes = self.codes(reviews=[review('alice'), review('bob')], permissions={'bob': 'read'})[1]
        self.assertEqual(codes, ['permission-unreadable', 'approver-cannot-write', 'no-approval'])
        self.assertFalse(auto_finish.stop_due(codes))
        # a second approver whose permission was read and is write counts; the unread one is a note
        status, _, counted, notes = self.ready(reviews=[review('alice'), review('carol')], permissions={'carol': 'admin'})
        self.assertEqual((status, counted), ('ready', ['carol']))
        self.assertIn('not counted, the permission of alice could not be read', notes)

    def test_no_required_checks(self):
        self.assert_only('no-required-checks', required=[])

    def test_merge_state(self):
        for s in ('BLOCKED', 'BEHIND', 'DIRTY', 'HAS_HOOKS', 'DRAFT', None):
            with self.subTest(state=s):
                self.assert_only('merge-state', view(mergeStateStatus=s))
        # UNSTABLE for a check that is not the review trigger's
        self.assert_only('merge-state', view(mergeStateStatus='UNSTABLE',
                                             statusCheckRollup=GREEN + [dict(GREEN[0], name='opt', status='IN_PROGRESS', conclusion='')]))

    def test_merge_state_unknown_is_retry(self):
        self.assert_only('merge-state-unknown', view(mergeStateStatus='UNKNOWN'), status='retry')
        # with another reason it is not ready
        self.assertEqual(self.codes(view(mergeStateStatus='UNKNOWN'), reviews=[])[0], 'not-ready')

    def test_merge_state_trigger_is_retry(self):
        for run_ in (dict(TRIGGER, status='QUEUED', conclusion=''), dict(TRIGGER, status='IN_PROGRESS', conclusion=''),
                     # one sample of the scratch repo listed a queued job without its workflow yet
                     {'__typename': 'CheckRun', 'name': 'review', 'workflowName': '', 'status': 'QUEUED', 'conclusion': ''}):
            with self.subTest(run=run_):
                self.assert_only('merge-state-trigger', view(mergeStateStatus='UNSTABLE', statusCheckRollup=GREEN + [run_]), status='retry')
        # a trigger run that finished and is not a success does not change by itself: no retry (plan 1.4.4 h)
        for conclusion in ('CANCELLED', 'FAILURE', 'TIMED_OUT'):
            with self.subTest(conclusion=conclusion):
                self.assert_only('merge-state', view(mergeStateStatus='UNSTABLE',
                                                     statusCheckRollup=GREEN + [dict(TRIGGER, status='COMPLETED', conclusion=conclusion)]))

    def test_stopped(self):
        stop = comment('stopped\n\n' + auto_finish.marker('stopped', HEAD))
        status, reasons, _, _ = self.ready(comments=[stop])
        self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['stopped']))
        # only the release App's comment, only for this head
        self.assertEqual(self.codes(comments=[dict(stop, user={'login': 'mallory', 'type': 'User'})])[0], 'ready')
        self.assertEqual(self.codes(comments=[comment(auto_finish.marker('stopped', OLD))])[0], 'ready')
        self.assertEqual(self.codes(comments=[comment(auto_finish.marker('merged', HEAD))])[0], 'ready')
        # only a marker on the last line counts: one quoted above it (a file name in a reason) does not
        quoted = comment(f'stopped: conflict markers in: {auto_finish.marker("stopped", HEAD)}\n\n{auto_finish.marker("stopped", OLD)}')
        self.assertEqual(self.codes(comments=[quoted])[0], 'ready')
        self.assertEqual(self.codes(comments=[comment(auto_finish.marker('stopped', HEAD) + '\n\nmore text')])[0], 'ready')
        # trailing white space after the marker line is not a line of its own
        self.assertEqual(self.codes(comments=[comment('stopped\n\n' + auto_finish.marker('stopped', HEAD) + '\n \n')])[1], ['stopped'])
        # a newer approval re-arms; an approval older than the stop does not
        later = '2026-11-20T14:00:00Z'
        self.assertEqual(self.codes(comments=[stop], reviews=[review('alice'), review('alice', at=later)])[0], 'ready')
        self.assertEqual(self.codes(comments=[stop], reviews=[review('alice', at=later), review('carol', at=T2)],
                                    permissions={'alice': 'read', 'carol': 'write'})[1],
                         ['stopped'])

    def test_comments_unreadable(self):
        self.assert_only('comments-unreadable', comments=None)

    def test_in_flight(self):
        for status in auto_finish.ACTIVE:
            with self.subTest(status=status):
                self.assert_only('in-flight', runs=[run(status=status)])
        # another PR's run, or a finish a person dispatched, is not this PR's auto-finish
        self.assertEqual(self.codes(runs=[run(pr=8, status='queued'), run(status='queued', auto=False)])[0], 'ready')
        # nor is the run of the same number in another repository (2a review, nit 3)
        self.assertEqual(self.codes(runs=[run(status='queued', repo='other')])[0], 'ready')
        self.assertEqual(self.codes(runs=[run(at=f'2026-11-20T13:0{i}:00Z', repo='other') for i in range(3)])[0], 'ready')
        # the runs mean nothing without the repository: ready refuses to guess
        with self.assertRaises(release_git.Error):
            auto_finish.ready(view(), [review('alice')], REQUIRED, APP, activity=[CREATED, push('resolver')],
                              permissions={'alice': 'write'}, comments=[], runs=[])

    def test_retries_exhausted(self):
        three = [run(at=f'2026-11-20T13:0{i}:00Z') for i in range(3)]
        self.assert_only('retries-exhausted', runs=three)
        self.assertEqual(self.codes(runs=three[:2])[0], 'ready')
        # runs before the last approval or the last push are another head's or approval's
        self.assertEqual(self.codes(runs=[run(at='2026-11-20T11:30:00Z')] + three[:2])[0], 'ready')
        self.assertEqual(self.codes(runs=three, reviews=[review('alice', at='2026-11-20T14:00:00Z')])[0], 'ready')
        # cancelled and successful runs do not count; timed out and startup failures do
        self.assertEqual(self.codes(runs=three[:2] + [run(conclusion='cancelled'), run(conclusion='success')])[0], 'ready')
        self.assertEqual(self.codes(runs=three[:2] + [run(conclusion='timed_out')])[1], ['retries-exhausted'])
        self.assertEqual(self.codes(runs=three[:2] + [run(conclusion='startup_failure')])[1], ['retries-exhausted'])
        # a run with no start time counts
        self.assertEqual(self.codes(runs=three[:2] + [run(at=None)])[1], ['retries-exhausted'])

    def test_every_required_check_from_its_source(self):
        # 2a review, trust should-fix 2: a commit status named pr-checks is green in the rollup, and no
        # check run of that name is on the head
        forged = view(statusCheckRollup=[{'__typename': 'StatusContext', 'context': 'pr-checks', 'state': 'SUCCESS'}, GREEN[1]])
        status, reasons, _, _ = self.ready(forged, check_runs=CHECK_RUNS[1:])
        self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['check-source-missing']))
        self.assertFalse(auto_finish.stop_due([c for c, _ in reasons]))
        # a check run of the name from another App
        self.assert_only('check-source', check_runs=[CHECK_RUNS[0], dict(CHECK_RUNS[1], app={'slug': 'mallory'})])
        # check runs that could not be read, and no sources at all: never ready
        self.assert_only('check-runs-unreadable', check_runs=None)
        self.assertEqual(self.codes(sources=None), ('not-ready', ['check-source', 'check-source']))
        # the caller passes nothing: refused, not waved through
        status, reasons, _, _ = auto_finish.ready(view(), [review('alice')], REQUIRED, APP, activity=[CREATED, push('resolver')],
                                                  permissions={'alice': 'write'}, comments=[])
        self.assertEqual((status, [c for c, _ in reasons]), ('not-ready', ['check-runs-unreadable']))
        # the check runs are those of the view's head: a push since they were read leaves them on the old head
        self.assertEqual(self.codes(view(headRefOid=OLD), reviews=[review('alice', commit=OLD)]),
                         ('not-ready', ['check-source-missing', 'check-source-missing']))
        # another PR of the head branch into another base, or into this base opened by a person or retargeted, a
        # changed base, or a read that failed: not ready, no mark
        moved = {'8': [{'event': 'base_ref_changed', 'created_at': T1}]}
        for kw, code in (({'head_prs': HEAD_PRS + [rest_pr(8, 'x/attacker', state='closed')]}, 'check-source-other-pr'),
                         ({'head_prs': HEAD_PRS + [rest_pr(8, state='closed', user='mallory')]}, 'check-source-other-pr'),
                         ({'head_prs': HEAD_PRS + [rest_pr(8, state='closed')], 'other_events': moved}, 'check-source-other-pr'),
                         ({'events': [{'event': 'base_ref_changed'}]}, 'check-source-base-changed'),
                         ({'head_prs': None}, 'check-source-unreadable'), ({'events': None}, 'check-source-unreadable'),
                         ({'head_prs': HEAD_PRS + [rest_pr(8, state='closed')]}, 'check-source-unreadable')):
            with self.subTest(kw=kw):
                self.assert_only(code, **kw)
                self.assertFalse(auto_finish.stop_due([code]))
        # the App's own earlier PR of the branch into this base, never retargeted: ready
        self.assertEqual(self.codes(head_prs=HEAD_PRS + [rest_pr(8, state='closed')], other_events={'8': []})[0], 'ready')

    def test_extra_reasons(self):
        status, reasons, _, _ = self.ready(extra=['layer-first: open first', 'ambiguous:both'])
        self.assertEqual((status, reasons), ('not-ready', [('layer-first', 'open first'), ('ambiguous', 'both')]))

    def test_the_review_trigger_is_ignored(self):
        for run_ in (dict(TRIGGER, status='COMPLETED', conclusion='CANCELLED'), dict(TRIGGER, status='COMPLETED', conclusion='FAILURE'),
                     dict(TRIGGER, status='COMPLETED', conclusion='SKIPPED')):
            with self.subTest(run=run_):
                self.assertEqual(self.codes(view(statusCheckRollup=GREEN + [run_]))[0], 'ready')

    def test_only_the_newest_run_counts(self):
        older = dict(GREEN[1], conclusion='FAILURE', completedAt='2026-11-20T11:00:00Z')
        newer = dict(GREEN[1], completedAt='2026-11-20T11:30:00Z')
        self.assertEqual(self.codes(view(statusCheckRollup=[GREEN[0], older, newer]))[0], 'ready')

    def test_the_view_must_carry_its_fields(self):
        for key in ('state', 'isDraft', 'headRefOid', 'mergeStateStatus'):
            v = view()
            del v[key]
            with self.subTest(key=key), self.assertRaises(release_git.Error):
                self.ready(v)

    def test_reasons_come_in_order_and_together(self):
        status, codes = self.codes(view(isDraft=True, mergeStateStatus='BLOCKED'), reviews=[review('bob', 'CHANGES_REQUESTED')],
                                   comments=None, extra=['layer-first: x'])
        self.assertEqual(status, 'not-ready')
        self.assertEqual(codes, ['draft', 'changes-requested', 'no-approval', 'comments-unreadable', 'merge-state', 'layer-first'])


WF = '.github/workflows/pr-checks.yml'
HEAD_REF = 'merge-up/x/1.x.x/abc1234'
REPO, BASE, PR = 'owner/repo', 'x/main', 7
SOURCES = {'pr-checks': {'workflow': WF}, 'other-check': {'app': 'other-app'}}


def check_run(name, cid, suite, app='github-actions', status='completed', conclusion='success', done=T2, head=HEAD):
    """One REST check run of the head, as GET repos/R/commits/<sha>/check-runs lists it."""
    return {'id': cid, 'name': name, 'head_sha': head, 'status': status, 'conclusion': conclusion if status == 'completed' else None,
            'started_at': T1, 'completed_at': done if status == 'completed' else None, 'app': {'slug': app}, 'check_suite': {'id': suite}}


def pr_entry(number=PR, base=BASE, ref=HEAD_REF):
    """One entry of a workflow run's pull_requests, as GitHub lists it."""
    return {'number': number, 'head': {'ref': ref, 'sha': HEAD}, 'base': {'ref': base}}


def rest_pr(number=PR, base=BASE, ref=HEAD_REF, state='open', repo=REPO, user=APP):
    """One pull request of the head branch, as GET repos/R/pulls?state=all&head=<owner>:<branch> lists it: by
    default opened by the release App."""
    return {'number': number, 'state': state, 'user': {'login': user, 'type': 'Bot' if user.endswith('[bot]') else 'User'},
            'head': {'ref': ref, 'sha': HEAD, 'repo': {'full_name': repo}}, 'base': {'ref': base}}


# The head branch's pull requests as the sweep and the gate read them: this one alone.
HEAD_PRS = [rest_pr()]


def actions_run(rid, suite, path=WF, branch=HEAD_REF, event='pull_request', head=HEAD, prs='default', repo=REPO):
    """One REST workflow run of the head, as GET repos/R/actions/runs?head_sha=<sha> lists it: by default
    a run for this PR (#7 into x/main, from owner/repo)."""
    return {'id': rid, 'path': path, 'event': event, 'head_branch': branch, 'head_sha': head, 'status': 'completed',
            'conclusion': 'success', 'check_suite_id': suite, 'head_repository': {'full_name': repo},
            'pull_requests': [pr_entry()] if prs == 'default' else prs}


def job(jid, rid, name='pr-checks'):
    return {'id': jid, 'run_id': rid, 'name': name, 'check_run_url': f'https://api.github.invalid/repos/o/r/check-runs/{jid}'}


# The head as the sweep and the gate read it: pr-checks from the PR checks workflow's run 10 (its
# job 101), other-check from the App other-app.
CHECK_RUNS = [check_run('pr-checks', 101, 1), check_run('other-check', 201, 2, app='other-app')]
ACTIONS_RUNS = [actions_run(10, 1)]
JOBS = {10: [job(101, 10)]}


class CheckSourceTests(unittest.TestCase):
    """check_sources: a required check counts only from its configured source (ENG-2235; 2a review,
    trust should-fix 2). Each case changes one thing in the head of a ready PR."""

    def reasons(self, check_runs=CHECK_RUNS, actions_runs=ACTIONS_RUNS, jobs=JOBS, required=REQUIRED, sources=SOURCES, ref=HEAD_REF,
                pr=PR, base=BASE, repo=REPO, head_prs=HEAD_PRS, events=(), release_app=APP, other_events=None):
        return auto_finish.check_sources(required, sources, ref, HEAD, check_runs, actions_runs, jobs, pr=pr, base=base, repo=repo,
                                         head_prs=head_prs, events=events, release_app=release_app, other_events=other_events)

    def source_runs(self, required=REQUIRED, check_runs=CHECK_RUNS, actions_runs=ACTIONS_RUNS, sources=SOURCES):
        return auto_finish.source_runs(required, sources, HEAD_REF, HEAD, check_runs, actions_runs, pr=PR, base=BASE, repo=REPO)

    def codes(self, **kw):
        return [c for c, _ in self.reasons(**kw)]

    def test_all_good(self):
        self.assertEqual(self.reasons(), [])
        self.assertEqual(self.source_runs(), [10])
        # only the run whose check suite holds the newest check run of the name: an older run of the same file is not read
        older = check_run('pr-checks', 99, 3, done=T0)
        self.assertEqual(self.source_runs(check_runs=CHECK_RUNS + [older], actions_runs=ACTIONS_RUNS + [actions_run(11, 3)]), [10])
        # nothing to read for an App source, an unknown name or a run of another file
        self.assertEqual(self.source_runs(required=['other-check', 'nope']), [])
        self.assertEqual(self.source_runs(actions_runs=[actions_run(10, 1, path='.github/workflows/x.yml')]), [])
        # nor for a run of another PR, even when its check run is newer
        other = check_run('pr-checks', 102, 3, done=T3)
        self.assertEqual(self.source_runs(check_runs=CHECK_RUNS + [other], actions_runs=ACTIONS_RUNS + [actions_run(11, 3, prs=[pr_entry(8, 'x')])]), [10])
        # the PR, its base and its repo are needed: without them no run is this PR's
        for kw in ({'pr': None}, {'base': None}, {'repo': None}):
            with self.subTest(missing=list(kw)):
                args = {'pr': PR, 'base': BASE, 'repo': REPO, **kw}
                self.assertEqual(auto_finish.source_runs(REQUIRED, SOURCES, HEAD_REF, HEAD, CHECK_RUNS, ACTIONS_RUNS, **args), [])
                # (without the base, this PR's own entry in the head branch's list reads as another base; without the
                # number, as another PR of the App's into this base, whose events nobody read)
                self.assertEqual([c for c, _ in self.reasons(**kw)], ['check-source-base-changed'] * ('base' in kw)
                                 + ['check-source-unreadable'] * ('pr' in kw) + ['check-source-missing'])

    def test_the_recorded_head(self):
        # ds-tegra-distro #199's head as GitHub listed it (read-only gh api, 2026-11-23), against the shipped sources
        with open(os.path.join(HERE, 'fixtures', 'auto-finish', 'ds-tegra-distro-199-sources.json')) as f:
            rec = json.load(f)
        # this repo's config, validated (a repo whose finish list is pr-checks alone reads the same answer for it)
        finish = release_git.merge_up_config()['finish']
        head, ref, pr, base = rec['pr']['sha'], rec['pr']['head'], rec['pr']['number'], rec['pr']['base']
        this = dict(pr=pr, base=base, repo='DistributedSpectrum/ds-tegra-distro')
        # The recording keeps no pull_requests: GitHub lists a run's pull requests when it is read, and
        # only open ones (run 44809708540 lists none since #199 closed; read-only gh api, 2026-11-23). An
        # open PR's run lists it so (ds-tegra-distro #200's runs, read the same day): added here.
        runs = [dict(r, head_repository={'full_name': this['repo']},
                     pull_requests=[{'number': pr, 'head': {'ref': ref, 'sha': head}, 'base': {'ref': base}}])
                for r in rec['actions_runs']]
        self.assertEqual(auto_finish.source_runs(finish['required_checks'], finish['check_sources'], ref, head,
                                                 rec['check_runs'], runs, **this), [44809708540])
        alone = dict(head_prs=[{'number': pr, 'state': 'open', 'head': {'ref': ref}, 'base': {'ref': base}}], events=[])
        self.assertEqual(auto_finish.check_sources(finish['required_checks'], finish['check_sources'], ref, head,
                                                   rec['check_runs'], runs, rec['jobs'], **this, **alone), [])
        # the same head, read for another branch that points at it, another PR or another base: refused
        for over in ({'ref': 'merge-up/x/9.9.x/0000000'}, {'pr': 198}, {'base': 'vertex/main'}):
            with self.subTest(over=over):
                kw = dict(this, ref=ref, **{k: v for k, v in over.items() if k != 'ref'})
                kw['ref'] = over.get('ref', ref)
                got = auto_finish.check_sources(finish['required_checks'], finish['check_sources'], kw.pop('ref'), head,
                                                rec['check_runs'], runs, rec['jobs'], **kw, **alone)
                # (another number: #199, into this base, is then another PR that no release App login vouches for)
                self.assertEqual([c for c, _ in got], ['check-source-other-pr'] * ('pr' in over) + ['check-source-base-changed'] * ('base' in over)
                                 + ['check-source-missing'])
        # as recorded, after the PR closed: its runs list no pull request, so none is this PR's
        self.assertEqual([c for c, _ in auto_finish.check_sources(finish['required_checks'], finish['check_sources'], ref, head,
                                                                  rec['check_runs'], rec['actions_runs'], rec['jobs'], **this, **alone)],
                         ['check-source-missing'])
        # 2c re-review, should-fix 1, measured: the run lists every open PR of the branch, whichever PR started it. A
        # second PR from the branch into x/attacker (#500) gets a newer run that lists #199 as well: refused, so long as
        # #500 is listed, open or closed
        attacker = dict(runs[0], id=777, check_suite_id=7770,
                        pull_requests=[{'number': 500, 'head': {'ref': ref, 'sha': head}, 'base': {'ref': 'x/attacker'}}]
                        + runs[0]['pull_requests'])
        theirs = [dict(c, id=7771, check_suite={'id': 7770}, completed_at='2099-01-01T00:00:00Z') for c in rec['check_runs']
                  if c['name'] == 'pr-checks']
        jobs = dict(rec['jobs'], **{'777': [{'id': 7771, 'run_id': 777, 'name': 'pr-checks'}]})
        mine = [dict(c, conclusion='failure') if c['name'] == 'pr-checks' else c for c in rec['check_runs']]
        for state in ('open', 'closed'):
            with self.subTest(attacker=state):
                listed = alone['head_prs'] + [{'number': 500, 'state': state, 'head': {'ref': ref}, 'base': {'ref': 'x/attacker'}}]
                got = auto_finish.check_sources(finish['required_checks'], finish['check_sources'], ref, head, mine + theirs,
                                                runs + [attacker], jobs, **this, head_prs=listed, events=[])
                self.assertEqual([c for c, _ in got], ['check-source-other-pr'])

    def test_a_commit_status_never_counts(self):
        # a status context named pr-checks is in the rollup only; the check runs have none of that name
        self.assertEqual(self.codes(check_runs=CHECK_RUNS[1:]), ['check-source-missing'])
        self.assertIn('pr-checks: no check run of that name on the head', self.reasons(check_runs=CHECK_RUNS[1:])[0][1])

    def test_a_check_run_from_another_workflow(self):
        for path in ('.github/workflows/other.yml', '.github/workflows/pr-checks.yaml', WF + '@refs/heads/x'):
            with self.subTest(path=path):
                got = self.reasons(actions_runs=[actions_run(10, 1, path=path)])
                self.assertEqual([c for c, _ in got], ['check-source-missing'])
                self.assertIn(f'no check run of that name comes from a {WF} run for this pull request (#7 into x/main from {HEAD_REF} '
                              f'in owner/repo), and its newest comes from {path} (pull_request on {HEAD_REF}, PRs #7 into x/main)', got[0][1])

    def test_the_right_workflow_for_another_branch(self):
        # a PR from another branch that points at the same commit runs the same file for the same sha
        self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, branch='feature/x')]), ['check-source-missing'])
        self.assertEqual(self.codes(ref='merge-up/x/1.x.x/0000000'), ['check-source-missing'])

    def test_the_right_workflow_for_another_event_or_head(self):
        for event in ('push', 'workflow_dispatch', 'workflow_run', 'schedule'):
            with self.subTest(event=event):
                self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, event=event)]), ['check-source-missing'])
        self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, head=OLD)]), ['check-source-missing'])
        self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, event='pull_request_target')]), [])

    def test_a_run_for_another_pull_request(self):
        # 2c review, trust should-fix 1; 2c re-review, should-fix 1: a second PR (#8) from the same head branch into
        # another base gets a real pull_request run of the same file, head and branch. GitHub fills a run's
        # pull_requests when it is read, with every open PR of the branch: that run lists this PR (#7 into x/main) too,
        # while #8 is open, and alone once #8 is closed. Its newer success must not hide this PR's failure, so
        # nothing vouches while #8 is a PR of the branch, open or closed.
        mine = check_run('pr-checks', 101, 1, conclusion='failure', done=T2)
        theirs = check_run('pr-checks', 777, 7, done=T3)
        jobs = {10: [job(101, 10)], 20: [job(777, 20)]}
        for prs, state in (([pr_entry(8, 'x/attacker'), pr_entry()], 'open'), ([pr_entry()], 'closed')):
            with self.subTest(attacker=state):
                other = actions_run(20, 7, prs=prs)
                listed = HEAD_PRS + [rest_pr(8, 'x/attacker', state=state)]
                got = self.reasons(check_runs=[mine, theirs, CHECK_RUNS[1]], actions_runs=ACTIONS_RUNS + [other], jobs=jobs, head_prs=listed)
                self.assertEqual([c for c, _ in got], ['check-source-other-pr'])
                self.assertIn(f'#8 ({state}) into x/attacker also comes from {HEAD_REF}', got[0][1])
        # A run that lists #8 alone (no such read on GitHub while #7 is open) is no run of this PR
        other = actions_run(20, 7, prs=[pr_entry(8, 'x/attacker')])
        got = self.reasons(check_runs=[mine, theirs, CHECK_RUNS[1]], actions_runs=ACTIONS_RUNS + [other], jobs=jobs)
        self.assertEqual([c for c, _ in got], ['check-source-failed'])
        self.assertEqual(self.source_runs(check_runs=[mine, theirs, CHECK_RUNS[1]], actions_runs=ACTIONS_RUNS + [other]), [10])
        # with no run of this PR at all, the other PR's success is no source
        got = self.reasons(check_runs=[theirs, CHECK_RUNS[1]], actions_runs=[other], jobs={20: [job(777, 20)]})
        self.assertEqual([c for c, _ in got], ['check-source-missing'])
        self.assertIn('its newest comes from .github/workflows/pr-checks.yml (pull_request on '
                      f'{HEAD_REF}, PRs #8 into x/attacker)', got[0][1])
        # this PR's number into another base, or another number into this base: neither is this PR's run
        for entry in (pr_entry(PR, 'x/attacker'), pr_entry(8, BASE)):
            with self.subTest(entry=entry):
                self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, prs=[entry])]), ['check-source-missing'])
        # an entry for this PR among others counts (GitHub lists every open PR of the head)
        self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, prs=[pr_entry(8, 'x/attacker'), pr_entry()])]), [])
        # the number may come as a string
        self.assertEqual(self.reasons(pr=str(PR)), [])

    def test_a_run_is_this_prs_only_when_the_pr_base_and_repo_are_known(self):
        # mutation A6: without the pr/base/repo guard, a missing value would match a missing field of the run
        bare = actions_run(10, 1, prs=[{'head': {'ref': HEAD_REF, 'sha': HEAD}}])
        del bare['head_repository']
        args = dict(pr=PR, base=BASE, repo=REPO)
        self.assertTrue(auto_finish.this_prs_run(actions_run(10, 1), WF, HEAD_REF, HEAD, **args))
        for missing in ('pr', 'base', 'repo'):
            with self.subTest(missing=missing):
                run_ = dict(bare, head_repository={'full_name': REPO}) if missing != 'repo' else bare
                entry = {'head': {'ref': HEAD_REF, 'sha': HEAD}}
                if missing != 'pr':
                    entry['number'] = PR
                if missing != 'base':
                    entry['base'] = {'ref': BASE}
                run_ = dict(run_, pull_requests=[entry])
                self.assertFalse(auto_finish.this_prs_run(run_, WF, HEAD_REF, HEAD, **dict(args, **{missing: None})))

    def test_a_run_that_lists_no_pull_request(self):
        # GitHub lists none for a fork's run and once the PR has closed: nothing ties the run to this PR
        for prs in ([], None, ['#7'], [{'number': 7}]):
            with self.subTest(prs=prs):
                self.assertEqual(self.codes(actions_runs=[actions_run(10, 1, prs=prs)]), ['check-source-missing'])

    def test_a_run_from_another_head_repository(self):
        for repo in ('fork/repo', 'owner/other', None):
            with self.subTest(repo=repo):
                run_ = actions_run(10, 1, repo=repo)
                if repo is None:
                    del run_['head_repository']
                self.assertEqual(self.codes(actions_runs=[run_]), ['check-source-missing'])

    def test_the_check_suite_must_be_the_runs_own(self):
        # a check run in the suite of another workflow's run is that run's, whatever a jobs list claims
        other = check_run('pr-checks', 777, 7, done=T3)
        got = self.reasons(check_runs=[other, CHECK_RUNS[1]], actions_runs=ACTIONS_RUNS + [actions_run(20, 7, path='.github/workflows/other.yml')],
                           jobs={10: [job(101, 10), job(777, 10)]})
        self.assertEqual([c for c, _ in got], ['check-source-missing'])
        self.assertIn('its newest comes from .github/workflows/other.yml (pull_request on', got[0][1])

    def test_a_check_run_in_no_actions_run(self):
        got = self.reasons(actions_runs=[])
        self.assertEqual([c for c, _ in got], ['check-source-missing'])
        self.assertIn('its newest comes from no Actions run of the head', got[0][1])

    def test_a_check_run_posted_into_the_right_runs_suite(self):
        # posted through the checks API (any workflow's token can), filed by GitHub in the PR checks run's suite
        forged = check_run('pr-checks', 999, 1, done=T3)
        got = self.reasons(check_runs=CHECK_RUNS + [forged])
        self.assertEqual([c for c, _ in got], ['check-source'])
        self.assertIn('is in the check suite of the .github/workflows/pr-checks.yml run 10 but is no job of it', got[0][1])
        # the job's id or its check_run_url names the check run
        self.assertEqual(self.reasons(jobs={10: [{'id': 5, 'check_run_url': '.../check-runs/101'}]}), [])
        self.assertEqual(self.reasons(jobs={'10': [job(101, 10)]}), [])

    def test_an_app_check_from_another_app(self):
        for app in ('github-actions', 'other-app-2', None):
            with self.subTest(app=app):
                runs = [CHECK_RUNS[0], dict(CHECK_RUNS[1], app={'slug': app} if app else None)]
                got = self.reasons(check_runs=runs)
                self.assertEqual([c for c, _ in got], ['check-source'])
                self.assertIn('not by the App other-app', got[0][1])

    def test_a_newer_failing_run_of_the_right_source(self):
        newer = check_run('pr-checks', 102, 3, conclusion='failure', done=T3)
        self.assertEqual(self.codes(check_runs=CHECK_RUNS + [newer], actions_runs=ACTIONS_RUNS + [actions_run(11, 3)],
                                    jobs={**JOBS, 11: [job(102, 11)]}), ['check-source-failed'])
        # and one that has not finished is newer than every finished one
        running = check_run('pr-checks', 102, 3, status='in_progress')
        self.assertEqual(self.codes(check_runs=CHECK_RUNS + [running], actions_runs=ACTIONS_RUNS + [actions_run(11, 3)]), ['check-source-pending'])
        # an older failure behind a newer success is history
        older = check_run('pr-checks', 100, 0, conclusion='failure', done=T0)
        self.assertEqual(self.codes(check_runs=CHECK_RUNS + [older]), [])
        for conclusion in ('skipped', 'neutral', 'cancelled', 'timed_out', 'action_required'):
            with self.subTest(conclusion=conclusion):
                self.assertEqual(self.codes(check_runs=[dict(CHECK_RUNS[0], conclusion=conclusion), CHECK_RUNS[1]]), ['check-source-failed'])

    def test_a_newer_success_from_another_source(self):
        # the newest run decides: one from another App after the right one's success refuses
        other = check_run('other-check', 202, 4, app='mallory-app', done=T3)
        self.assertEqual(self.codes(check_runs=CHECK_RUNS + [other]), ['check-source'])

    def test_check_runs_of_another_head(self):
        self.assertEqual(self.codes(check_runs=[dict(c, head_sha=OLD) for c in CHECK_RUNS]), ['check-source-missing'] * 2)

    def test_what_could_not_be_read(self):
        for kw in ({'check_runs': None}, {'actions_runs': None}):
            with self.subTest(kw=list(kw)):
                self.assertEqual(self.codes(**kw), ['check-runs-unreadable'])
        got = self.reasons(jobs={})
        self.assertEqual([c for c, _ in got], ['check-runs-unreadable'])
        self.assertIn('the jobs of the .github/workflows/pr-checks.yml run 10 could not be read', got[0][1])

    def test_a_name_without_a_source(self):
        self.assertEqual(self.codes(sources={'pr-checks': {'workflow': WF}}), ['check-source'])
        self.assertEqual(self.codes(sources=None), ['check-source', 'check-source'])

    def test_no_code_marks_a_head(self):
        for code in ('check-source', 'check-source-missing', 'check-source-pending', 'check-source-failed', 'check-runs-unreadable',
                     'check-source-other-pr', 'check-source-base-changed', 'check-source-unreadable'):
            self.assertFalse(auto_finish.stop_due([code]), code)


class HeadBranchPrTests(unittest.TestCase):
    """head_branch_prs: a run of the head branch is this PR's only while no other PR has the branch with another base
    and this PR's base never changed (2c re-review, should-fix 1)."""

    def codes(self, head_prs=HEAD_PRS, events=(), pr=PR, base=BASE, release_app=APP, other_events=None):
        return [c for c, _ in auto_finish.head_branch_prs(pr, base, HEAD_REF, head_prs, events, release_app=release_app,
                                                          other_events=other_events)]

    def test_this_pr_alone(self):
        self.assertEqual(self.codes(), [])
        self.assertEqual(self.codes(head_prs=[]), [])
        # events that are not a base change say nothing about the runs
        self.assertEqual(self.codes(events=[{'event': 'head_ref_force_pushed'}, {'event': 'labeled'}, 'junk']), [])

    def test_another_pr_of_the_branch_into_another_base(self):
        for state in ('open', 'closed'):
            with self.subTest(state=state):
                got = auto_finish.head_branch_prs(PR, BASE, HEAD_REF, HEAD_PRS + [rest_pr(8, 'x/attacker', state=state)], [])
                self.assertEqual([c for c, _ in got], ['check-source-other-pr'])
                self.assertIn(f'#8 ({state}) into x/attacker also comes from {HEAD_REF}: GitHub lists this PR in the runs of every PR '
                              f'of the branch, so no run vouches for #7 into {BASE} alone', got[0][1])
        # a PR from another repository with a branch of the same name counts too: nothing is waved through on its name
        self.assertEqual(self.codes(head_prs=HEAD_PRS + [rest_pr(8, 'x/attacker', repo='fork/repo')]), ['check-source-other-pr'])
        # the number may come as a string
        self.assertEqual(self.codes(pr=str(PR), head_prs=HEAD_PRS + [rest_pr(8, 'x/attacker')]), ['check-source-other-pr'])

    def test_an_earlier_pr_of_the_branch_into_the_same_base(self):
        # an earlier bot PR of the same branch, closed (or still open: GitHub allows one open PR per head and base),
        # opened by the release App and never retargeted (its own events read: other_events)
        for state in ('closed', 'open'):
            with self.subTest(state=state):
                self.assertEqual(self.codes(head_prs=[rest_pr(5, state=state)] + HEAD_PRS,
                                            other_events={'5': [{'event': 'head_ref_force_pushed'}, {'event': 'closed'}]}), [])
        # the number may come as an int key
        self.assertEqual(self.codes(head_prs=[rest_pr(5)] + HEAD_PRS, other_events={5: []}), [])
        # a PR of another branch is not of this one
        self.assertEqual(self.codes(head_prs=HEAD_PRS + [rest_pr(8, 'x/attacker', ref='merge-up/x/1.x.x/0000000')]), [])
        # same_base_prs names the PRs whose events that needs: the App's same-base PRs of the branch, not this one
        listed = [rest_pr(5, state='closed'), rest_pr(6, user='mallory'), rest_pr(9, 'x/y'), rest_pr(4, ref='merge-up/x/1.x.x/0000000')] + HEAD_PRS
        self.assertEqual(auto_finish.same_base_prs(PR, BASE, HEAD_REF, listed, APP), [5])
        self.assertEqual(auto_finish.same_base_prs(str(PR), BASE, HEAD_REF, listed, None), [])

    def test_a_same_base_pr_counts_only_from_the_release_app_never_retargeted(self):
        # round 4 review, finding 1: M (#8) is opened by a person into x/attacker, retargeted onto x/main and closed; its
        # runs list #7 into x/main once #7 is reopened. Whoever opened it, a same-base PR whose base ever changed is
        # another source; so is one any account but the release App opened, whatever its events say
        moved = [{'event': 'base_ref_changed', 'created_at': T1}]
        for user, theirs in (('mallory', moved), ('mallory', []), (APP, moved), (APP, moved + moved)):
            with self.subTest(user=user, events=theirs):
                got = auto_finish.head_branch_prs(PR, BASE, HEAD_REF, HEAD_PRS + [rest_pr(8, state='closed', user=user)], [],
                                                  release_app=APP, other_events={'8': theirs})
                self.assertEqual([c for c, _ in got], ['check-source-other-pr'])
                self.assertIn(f'#8 (closed) into {BASE}, ' + (f'opened by {user}, not the release App' if user != APP
                                                                else f'whose base was changed ({T1}'), got[0][1])
        # no release App login: no same-base PR is set aside
        self.assertEqual(self.codes(head_prs=HEAD_PRS + [rest_pr(8)], release_app=None, other_events={'8': []}),
                         ['check-source-other-pr'])
        # a user GitHub does not name
        got = auto_finish.head_branch_prs(PR, BASE, HEAD_REF, HEAD_PRS + [dict(rest_pr(8), user=None)], [], release_app=APP, other_events={})
        self.assertIn('opened by an account GitHub does not name', got[0][1])
        # the App's same-base PR whose events could not be read: not ready, and no mark
        for other in (None, {}, {'9': []}):
            with self.subTest(other_events=other):
                got = auto_finish.head_branch_prs(PR, BASE, HEAD_REF, HEAD_PRS + [rest_pr(8)], [], release_app=APP, other_events=other)
                self.assertEqual([c for c, _ in got], ['check-source-unreadable'])
                self.assertIn(f'the events of #8, a PR of {HEAD_REF} into {BASE} the release App opened, could not be read', got[0][1])
        self.assertFalse(auto_finish.stop_due(['check-source-unreadable']))

    def test_a_retargeted_pr(self):
        # retargeted to x/attacker and back: the runs started meanwhile list it into x/main now
        back = [{'event': 'base_ref_changed', 'created_at': T1}, {'event': 'base_ref_changed', 'created_at': T2}]
        got = auto_finish.head_branch_prs(PR, BASE, HEAD_REF, HEAD_PRS, back)
        self.assertEqual([c for c, _ in got], ['check-source-base-changed'])
        self.assertIn(f'the base of #7 was changed ({T1}, {T2})', got[0][1])
        # the list gives this PR another base: changed since the caller read it
        self.assertEqual(self.codes(head_prs=[rest_pr(base='x/attacker')]), ['check-source-base-changed'])
        # both at once
        self.assertEqual(self.codes(head_prs=HEAD_PRS + [rest_pr(8, 'x/y')], events=back), ['check-source-other-pr', 'check-source-base-changed'])

    def test_an_entry_without_a_head_ref_or_a_base(self):
        # round 5 review, finding 5: the REST list always gives both, so neither is reachable today. An entry without
        # head.ref is still a PR of the branch (GitHub listed it for the branch), not set aside
        for entry in (dict(rest_pr(8, 'x/attacker'), head=None), dict(rest_pr(8, 'x/attacker'), head={}),
                      {k: v for k, v in rest_pr(8, 'x/attacker').items() if k != 'head'}):
            with self.subTest(entry=entry):
                self.assertEqual(self.codes(head_prs=HEAD_PRS + [entry]), ['check-source-other-pr'])
        # and this PR's own entry without head.ref, into another base
        self.assertEqual(self.codes(head_prs=[dict(rest_pr(base='x/attacker'), head=None)]), ['check-source-base-changed'])
        # another PR without base.ref is not shown to be into base: another source, though the release App opened it
        # and its events are read and hold no base change
        for entry in (dict(rest_pr(8), base=None), dict(rest_pr(8), base={}), {k: v for k, v in rest_pr(8).items() if k != 'base'}):
            with self.subTest(entry=entry):
                self.assertEqual(self.codes(head_prs=HEAD_PRS + [entry], other_events={'8': []}), ['check-source-other-pr'])
                self.assertEqual(auto_finish.same_base_prs(PR, BASE, HEAD_REF, HEAD_PRS + [entry], APP), [])

    def test_what_could_not_be_read(self):
        for kw in ({'head_prs': None}, {'events': None}):
            with self.subTest(kw=list(kw)):
                self.assertEqual(self.codes(**kw), ['check-source-unreadable'])
        # and check_sources refuses with it, after its own unreadable check runs
        got = auto_finish.check_sources(REQUIRED, SOURCES, HEAD_REF, HEAD, CHECK_RUNS, ACTIONS_RUNS, JOBS, pr=PR, base=BASE, repo=REPO)
        self.assertEqual([c for c, _ in got], ['check-source-unreadable'])

    def test_the_verifiers_scenario(self):
        # round 4 review, finding 1, at check_sources: #7's own pr-checks failed; M (#8, opened by mallory into
        # x/attacker, whose pr-checks passes slowly) was retargeted onto x/main and closed, and #7 reopened. M's run
        # finished after #7's and, read now, lists only the open #7 into x/main: this_prs_run takes it for #7's
        theirs = check_run('pr-checks', 777, 7, done=T3)
        mine = check_run('pr-checks', 101, 1, conclusion='failure', done=T2)
        runs = [mine, theirs, CHECK_RUNS[1]]
        actions = ACTIONS_RUNS + [actions_run(20, 7)]
        jobs = {10: [job(101, 10)], 20: [job(777, 20)]}
        moved = [{'event': 'base_ref_changed', 'created_at': T1}]
        listed = HEAD_PRS + [rest_pr(8, state='closed', user='mallory')]
        def codes(head_prs, other_events, events=()):
            return [c for c, _ in auto_finish.check_sources(REQUIRED, SOURCES, HEAD_REF, HEAD, runs, actions, jobs, pr=PR, base=BASE,
                                                            repo=REPO, head_prs=head_prs, events=events, release_app=APP,
                                                            other_events=other_events)]
        # without the guard the newer success vouches: the hole
        self.assertEqual(auto_finish.source_runs(REQUIRED, SOURCES, HEAD_REF, HEAD, runs, actions, pr=PR, base=BASE, repo=REPO), [20])
        self.assertEqual(codes(HEAD_PRS, {}), [])
        # with it: refused, whether M's events are read or not, and #7's own events hold only closed and reopened
        reopened = [{'event': 'closed'}, {'event': 'reopened'}]
        self.assertEqual(codes(listed, {'8': moved}, reopened), ['check-source-other-pr'])
        self.assertEqual(codes(listed, {}, reopened), ['check-source-other-pr'])
        # the same with M opened by the release App: refused on its base change
        self.assertEqual(codes(HEAD_PRS + [rest_pr(8, state='closed')], {'8': moved}, reopened), ['check-source-other-pr'])
        # the App's M, never retargeted, into x/main all along: it ran the same checks against the same base, and vouches
        self.assertEqual(codes(HEAD_PRS + [rest_pr(8, state='closed')], {'8': [{'event': 'closed'}]}, reopened), [])
        # its events unread: not ready
        self.assertEqual(codes(HEAD_PRS + [rest_pr(8, state='closed')], {}, reopened), ['check-source-unreadable'])

    def test_a_retargeted_prs_run_counts_without_the_guard(self):
        # a run another base's PR checks started while #7 pointed at x/attacker lists #7 into x/main once it is back
        theirs = check_run('pr-checks', 777, 7, done=T3)
        mine = check_run('pr-checks', 101, 1, conclusion='failure', done=T2)
        got = auto_finish.check_sources(REQUIRED, SOURCES, HEAD_REF, HEAD, [mine, theirs, CHECK_RUNS[1]], ACTIONS_RUNS + [actions_run(20, 7)],
                                        {10: [job(101, 10)], 20: [job(777, 20)]}, pr=PR, base=BASE, repo=REPO,
                                        head_prs=HEAD_PRS, events=[{'event': 'base_ref_changed'}])
        self.assertEqual([c for c, _ in got], ['check-source-base-changed'])


class ReasonCodeTests(unittest.TestCase):
    """Every reason pr_check gives in strict mode maps to a code (U10)."""

    def strict_reasons(self):
        """Each form of pr_check's strict reasons, produced by pr_check itself."""
        out = []
        def add(doc, reviews, activity=None, commits=None, required=REQUIRED):
            out.extend(release_git.pr_check(dict(view(), **doc), reviews, strict=True, required=required,
                                            activity=activity, commits=commits)[0])
        act = [CREATED]
        add({'reviewDecision': 'REVIEW_REQUIRED', 'mergeStateStatus': 'DIRTY'}, [review('alice')], act)
        add({}, [review('alice', commit=OLD)], act)
        add({}, [review('alice')], [push('alice')])
        add({}, [review('ci[bot]', kind='Bot')], act)
        add({}, [review('alice')], None, [{'sha': '1' * 40, 'author': None, 'committer': None,
                                           'commit': {'author': {'email': 'x@y'}, 'committer': {'email': 'x@y'}}}])
        add({}, [review('alice')], None, None)
        add({'statusCheckRollup': []}, [review('alice')], act)
        for c in ({'conclusion': 'SKIPPED'}, {'conclusion': 'NEUTRAL'}, {'status': 'IN_PROGRESS', 'conclusion': ''},
                  {'status': 'QUEUED', 'conclusion': None}, {'context': 'other-check', 'state': 'PENDING', 'name': None},
                  {'context': 'other-check', 'state': 'EXPECTED', 'name': None}, {'conclusion': 'FAILURE'}, {'conclusion': 'ACTION_REQUIRED'},
                  {'context': 'other-check', 'state': 'ERROR', 'name': None}):
            entry = {k: v for k, v in dict(GREEN[1], **c).items() if v is not None or k == 'conclusion'}
            if 'context' in c:
                entry = {'context': c['context'], 'state': c['state']}
            add({'statusCheckRollup': [GREEN[0], entry]}, [review('alice')], act)
        return out

    def test_every_strict_reason_has_a_code(self):
        reasons = self.strict_reasons()
        self.assertGreaterEqual(len(reasons), 14)
        for why in reasons:
            with self.subTest(reason=why):
                self.assertNotEqual(auto_finish.code_of(why), 'unrecognised')

    def test_the_codes(self):
        got = {auto_finish.code_of(why) for why in self.strict_reasons()}
        self.assertEqual(got, {'review-decision', 'merge-state', 'stale-approval', 'no-approval', 'approver-pushed',
                               'unknown-author', 'activity-unreadable', 'checks-missing', 'checks-skipped', 'checks-pending',
                               'checks-failed'})
        self.assertEqual(auto_finish.code_of('something pr_check never said'), 'unrecognised')

    def test_an_unknown_reason_is_not_ready(self):
        # a reason no code knows still stops the PR: it is reported as unrecognised
        real = release_git.pr_check
        try:
            auto_finish.pr_check = lambda *a, **k: (['a new kind of reason'], None, ['alice'])
            status, reasons, _, _ = ReadyCase.ready(self)
        finally:
            auto_finish.pr_check = real
        self.assertEqual((status, reasons), ('not-ready', [('unrecognised', 'a new kind of reason')]))

    ready = ReadyCase.ready


class SwitchTests(unittest.TestCase):
    """DS_MERGE_UP_AUTO_FINISH. An Actions `if:` compares strings ignoring case, so the parser
    does too: what the workflow treats as off, the script treats as off."""

    def test_off(self):
        for value in (None, '', '  ', 'off', 'OFF', ' Off\n'):
            with self.subTest(value=value):
                self.assertEqual(auto_finish.switch(value), 'off')

    def test_shadow_and_on(self):
        for value, want in (('shadow', 'shadow'), ('Shadow', 'shadow'), (' SHADOW ', 'shadow'), ('on', 'on'), ('ON', 'on'), ('\ton\n', 'on')):
            with self.subTest(value=value):
                self.assertEqual(auto_finish.switch(value), want)

    def test_anything_else_is_an_error(self):
        for value in ('true', '1', 'yes', 'shadow,on', 'on,', 'o n', 'enabled', 'of'):
            with self.subTest(value=value), self.assertRaises(release_git.Error) as e:
                auto_finish.switch(value)
            self.assertEqual(str(e.exception), f'DS_MERGE_UP_AUTO_FINISH={value!r}: not a mode; the accepted values are off, shadow and on '
                                               '(absent or empty is off; case and surrounding spaces are ignored)')

    def test_cli(self):
        env = {k: v for k, v in os.environ.items() if k != auto_finish.VAR}
        r = subprocess.run([CLI, 'switch'], capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (0, 'off\n'))
        r = subprocess.run([CLI, 'switch'], capture_output=True, text=True, env=dict(env, DS_MERGE_UP_AUTO_FINISH='Shadow'))
        self.assertEqual((r.returncode, r.stdout), (0, 'shadow\n'))
        r = subprocess.run([CLI, 'switch', '--value', 'nope'], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, '')
        self.assertIn("auto_finish.py: DS_MERGE_UP_AUTO_FINISH='nope': not a mode; the accepted values are off, shadow and on", r.stderr)


def pr(n, head, **over):
    doc = {'number': n, 'headRefName': head, 'baseRefName': 'x/main', 'headRefOid': f'{n:040d}',
           'isCrossRepository': False, 'isDraft': False, 'state': 'OPEN'}
    doc.update(over)
    return doc


class CandidateTests(unittest.TestCase):

    def test_heads(self):
        prs = [pr(5, 'merge-up/x/1.x.x/abc1234'), pr(3, 'merge-up/1.x.x/0123456'), pr(4, 'feature/foo'),
               pr(6, 'merge-up/x/1.x.x/abc123'), pr(8, 'merge-up/x/1.x.x/' + 'a' * 40), pr(9, 'merge-up/abc1234'),
               pr(10, 'merge-up/x/1.x.x/ABC1234')]
        kept, skipped = auto_finish.candidates(prs)
        self.assertEqual(kept, [(3, '1.x.x', 'x/main', f'{3:040d}', 'merge-up/1.x.x/0123456'),
                                (5, 'x/1.x.x', 'x/main', f'{5:040d}', 'merge-up/x/1.x.x/abc1234')])
        self.assertEqual([n for n, _ in skipped], [6, 8, 9, 10])
        self.assertIn('is not merge-up/<line>/<sha7>', skipped[0][1])

    def test_forks_drafts_and_closed(self):
        prs = [pr(1, 'merge-up/x/1.x.x/abc1234', isCrossRepository=True), pr(2, 'merge-up/x/1.x.x/abc1234', isDraft=True),
               pr(3, 'merge-up/x/1.x.x/abc1234', state='CLOSED'), pr(4, 'merge-up/x/1.x.x/abc1234', isCrossRepository=None)]
        kept, skipped = auto_finish.candidates(prs)
        self.assertEqual(kept, [])
        self.assertEqual(skipped, [(1, 'head merge-up/x/1.x.x/abc1234 is not a branch of this repository'), (2, 'a draft'),
                                   (3, 'CLOSED, not open'), (4, 'head merge-up/x/1.x.x/abc1234 is not a branch of this repository')])

    def test_the_hint_comes_first(self):
        prs = [pr(n, f'merge-up/x/1.{n}.x/abc123{n}') for n in (4, 2, 9)]
        order = lambda **h: [c[0] for c in auto_finish.candidates(prs, **h)[0]]
        self.assertEqual(order(), [2, 4, 9])
        self.assertEqual(order(hint_pr='9'), [9, 2, 4])
        self.assertEqual(order(hint_pr=4), [4, 2, 9])
        self.assertEqual(order(hint_head='merge-up/x/1.9.x/abc1239'), [9, 2, 4])
        # a hint that names nothing open, or is not a number, changes nothing
        self.assertEqual(order(hint_pr='77'), [2, 4, 9])
        self.assertEqual(order(hint_pr='x; rm -rf'), [2, 4, 9])
        self.assertEqual(order(hint_head='merge-up/x/1.9.x/0000000'), [2, 4, 9])


class BusyTests(unittest.TestCase):
    WORKFLOWS = ['release-merge-up.yml', 'release-promote.yml', 'release-abandon.yml', 'release-create-branch.yml']
    JOBS = {'pin-propagation.yml': 'finish'}

    def r(self, wf, status, rid=1):
        return {'id': rid, 'path': f'.github/workflows/{wf}', 'status': status, 'html_url': f'https://example.invalid/{rid}'}

    def test_each_active_status_of_each_workflow(self):
        for wf in self.WORKFLOWS:
            for status in auto_finish.ACTIVE:
                with self.subTest(wf=wf, status=status):
                    self.assertEqual(auto_finish.busy([self.r(wf, status)], self.WORKFLOWS),
                                     [f'{wf} run https://example.invalid/1 is {status}'])

    def test_not_busy(self):
        runs = [self.r('release-merge-up.yml', 'completed'), self.r('release-rc-build.yml', 'in_progress', 2),
                self.r('release-auto-finish.yml', 'in_progress', 3)]
        self.assertEqual(auto_finish.busy(runs, self.WORKFLOWS), [])
        self.assertEqual(auto_finish.busy([], self.WORKFLOWS), [])

    def test_a_run_on_another_ref_counts(self):
        # a rollback dispatch on an older line runs that line's copy of the file
        self.assertTrue(auto_finish.busy([dict(self.r('release-promote.yml', 'pending'), path='.github/workflows/release-promote.yml@refs/heads/x/1.x.x')],
                                         self.WORKFLOWS))

    def test_a_run_listed_twice_counts_once(self):
        self.assertEqual(len(auto_finish.busy([self.r('release-merge-up.yml', 'queued')] * 2, self.WORKFLOWS)), 1)

    def test_the_pin_finish_job(self):
        pin = self.r('pin-propagation.yml', 'in_progress', 9)
        building = [{'name': 'prepare', 'status': 'completed'}, {'name': 'build', 'status': 'in_progress'}]
        self.assertEqual(auto_finish.busy([pin], self.WORKFLOWS, self.JOBS, {9: building}), [])
        for status in ('queued', 'in_progress', 'waiting', 'pending'):
            with self.subTest(status=status):
                self.assertEqual(auto_finish.busy([pin], self.WORKFLOWS, self.JOBS, {9: building + [{'name': 'finish', 'status': status}]}),
                                 [f'pin-propagation.yml run https://example.invalid/9: job finish is {status}'])
        self.assertEqual(auto_finish.busy([pin], self.WORKFLOWS, self.JOBS, {9: building + [{'name': 'finish', 'status': 'completed'}]}), [])
        # jobs that could not be read: busy
        self.assertEqual(auto_finish.busy([pin], self.WORKFLOWS, self.JOBS, {}),
                         ['pin-propagation.yml run https://example.invalid/9 is in_progress and its jobs could not be read'])
        # without the rule the workflow is not in the group
        self.assertEqual(auto_finish.busy([pin], self.WORKFLOWS), [])
        self.assertEqual(auto_finish.job_runs([pin, self.r('pin-propagation.yml', 'completed', 10), pin], self.JOBS), [9])


class MarkerTests(unittest.TestCase):

    def test_round_trip(self):
        for kind in ('merged', 'stopped'):
            m = auto_finish.marker(kind, HEAD)
            self.assertEqual(m, f'<!-- merge-up-auto-finish: {kind} head={HEAD} -->')
            self.assertEqual(auto_finish.FINISH_MARKER_RE.findall('text\n\n' + m), [(kind, HEAD)])

    def test_refused(self):
        for kind, head in (('merged', HEAD[:7]), ('done', HEAD), ('stopped', 'g' * 40), ('stopped', '')):
            with self.subTest(kind=kind, head=head), self.assertRaises(release_git.Error):
                auto_finish.marker(kind, head)
        self.assertEqual(auto_finish.FINISH_MARKER_RE.findall(f'<!-- merge-up-auto-finish: stopped head={HEAD[:7]} -->'), [])

    def test_only_the_last_line(self):
        m = auto_finish.marker('stopped', HEAD)
        self.assertEqual(auto_finish.last_marker(f'text\n\n{m}'), ('stopped', HEAD))
        self.assertEqual(auto_finish.last_marker(f'{m}\n'), ('stopped', HEAD))
        for body in (f'{m}\n\ntext', f'a file named {m}', f'text\n\nx {m}', '', None, f'{m} x'):
            with self.subTest(body=body):
                self.assertIsNone(auto_finish.last_marker(body))

    def test_the_conflict_marker_pattern_is_another_one(self):
        self.assertNotEqual(auto_finish.FINISH_MARKER_RE.pattern, release_git.MARKER_RE)


class StopDueTests(unittest.TestCase):

    def test_stop_due(self):
        for code in auto_finish.MARKING_CODES:
            self.assertTrue(auto_finish.stop_due([code]), code)
            self.assertFalse(auto_finish.stop_due([code, 'stopped']), code)
        for code in ('checks-pending', 'checks-missing', 'merge-state', 'merge-state-unknown', 'merge-state-trigger', 'activity-unreadable',
                     'comments-unreadable', 'changes-requested', 'draft', 'closed', 'merged', 'review-decision', 'layer-first',
                     'ambiguous', 'no-required-checks', 'checks-skipped', 'in-flight', 'retries-exhausted'):
            self.assertFalse(auto_finish.stop_due([code]), code)
        self.assertTrue(auto_finish.stop_due(['checks-pending', 'stale-approval']))
        self.assertFalse(auto_finish.stop_due([]))

    def test_an_unread_permission_marks_no_approval_code(self):
        self.assertFalse(auto_finish.stop_due(['permission-unreadable']))
        for code in auto_finish.APPROVAL_CODES:
            self.assertFalse(auto_finish.stop_due(['permission-unreadable', code]), code)
        # the codes that are not about the approvals still mark
        for code in sorted(set(auto_finish.MARKING_CODES) - set(auto_finish.APPROVAL_CODES)):
            self.assertTrue(auto_finish.stop_due(['permission-unreadable', 'no-approval', code]), code)
        self.assertEqual(set(auto_finish.APPROVAL_CODES) - set(auto_finish.MARKING_CODES), set())


class CliTests(unittest.TestCase):

    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='auto-finish-')
        self.addCleanup(shutil.rmtree, self.top, True)

    def file(self, name, doc):
        path = os.path.join(self.top, name)
        with open(path, 'w') as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def cli(self, *args):
        return subprocess.run([CLI, *args], capture_output=True, text=True)

    def ready_args(self, v=None, reviews=None, activity=True, perms='alice write\n', comments=True, repo=True):
        args = ['ready', self.file('view.json', v or view()), '--reviews', self.file('reviews.json', reviews or [review('alice')]),
                '--required', ','.join(REQUIRED), '--release-app', APP]
        # the head's check runs, from their sources: paginated, one object per page
        os.makedirs(os.path.join(self.top, 'jobs'), exist_ok=True)
        self.file('jobs/10.json', {'total_count': 1, 'jobs': JOBS[10]})
        # the events of #5, the App's earlier PR of the branch into the same base: paginated, one array per page
        os.makedirs(os.path.join(self.top, 'other-events'), exist_ok=True)
        self.file('other-events/5.json', json.dumps([{'event': 'closed'}]) + '\n' + json.dumps([{'event': 'labeled'}]) + '\n')
        args += ['--sources', self.file('sources.json', SOURCES), '--head-ref', HEAD_REF, '--base', BASE, '--jobs-dir', os.path.join(self.top, 'jobs'),
                 '--check-runs', self.file('check-runs.json', json.dumps({'total_count': 1, 'check_runs': CHECK_RUNS[:1]}) + '\n'
                                           + json.dumps({'total_count': 1, 'check_runs': CHECK_RUNS[1:]}) + '\n'),
                 '--actions-runs', self.file('actions-runs.json', {'total_count': 1, 'workflow_runs': ACTIONS_RUNS}),
                 # paginated: one array per page
                 '--head-prs', self.file('head-prs.json', json.dumps(HEAD_PRS) + '\n' + json.dumps([rest_pr(5, state='closed')]) + '\n'),
                 '--events', self.file('events.json', [{'event': 'head_ref_force_pushed'}]),
                 '--other-events', os.path.join(self.top, 'other-events')]
        if repo:
            args += ['--repo', REPO]
        if activity:
            # paginated: one array per page
            args += ['--activity', self.file('activity.json', json.dumps([CREATED]) + '\n' + json.dumps([push('resolver')]) + '\n')]
        if perms is not None:
            args += ['--permissions', self.file('perms.txt', perms)]
        if comments:
            args += ['--comments', self.file('comments.json', [])]
        return args

    def test_ready(self):
        r = self.cli(*self.ready_args())
        self.assertEqual((r.returncode, r.stdout), (0, 'ready\napprovers: alice\n'))
        self.assertIn('auto_finish.py: note: push actors, from the repository activity: resolver', r.stderr)

    def test_not_ready_and_retry_exit_0(self):
        r = self.cli(*self.ready_args(activity=False, perms='alice read\n'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], 'not-ready')
        self.assertEqual([l.split(':')[0] for l in r.stdout.splitlines()[1:]], ['activity-unreadable', 'approver-cannot-write', 'no-approval'])
        r = self.cli(*self.ready_args(v=view(mergeStateStatus='UNKNOWN')))
        self.assertEqual((r.returncode, r.stdout), (0, 'retry\nmerge-state-unknown: mergeStateStatus is UNKNOWN: GitHub has not computed it yet\n'))
        r = self.cli(*self.ready_args(), '--extra', 'layer-first: a layer PR goes first')
        self.assertEqual((r.returncode, r.stdout), (0, 'not-ready\nlayer-first: a layer PR goes first\n'))

    def test_runs_and_comments(self):
        runs = {'total_count': 3, 'workflow_runs': [run(at=f'2026-11-20T13:0{i}:00Z') for i in range(3)]}
        r = self.cli(*self.ready_args(), '--runs', self.file('runs.json', runs))
        self.assertEqual(r.stdout.splitlines()[:2][0], 'not-ready')
        self.assertTrue(r.stdout.splitlines()[1].startswith('retries-exhausted: 3 auto-finish runs'))
        # runs without the repository that holds the PR cannot be read
        r = self.cli(*self.ready_args(repo=False), '--runs', self.file('runs.json', runs))
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        self.assertIn('ready: the runs need the name of the repository that holds the PR (--repo)', r.stderr)
        r = self.cli(*self.ready_args(comments=False))
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'comments-unreadable')

    def test_unreadable_inputs_exit_1(self):
        r = self.cli('ready', os.path.join(self.top, 'missing.json'), '--reviews', self.file('r.json', []), '--release-app', APP)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, '')
        v = view()
        del v['state']
        r = self.cli(*self.ready_args(v=v))
        self.assertEqual(r.returncode, 1)
        self.assertIn('auto_finish.py: ready: the view has no state', r.stderr)

    def test_check_sources_and_source_runs(self):
        args = self.ready_args()
        r = self.cli(*args)
        self.assertEqual((r.returncode, r.stdout), (0, 'ready\napprovers: alice\n'))
        # without the jobs of run 10: not ready, unreadable
        os.remove(os.path.join(self.top, 'jobs', '10.json'))
        r = self.cli(*args)
        self.assertEqual(r.stdout.splitlines()[:2], ['not-ready', 'check-runs-unreadable: pr-checks: the jobs of the '
                                                     '.github/workflows/pr-checks.yml run 10 could not be read'])
        # without --check-runs: not ready
        i = args.index('--check-runs')
        r = self.cli(*(args[:i] + args[i + 2:]))
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'check-runs-unreadable')
        # without --head-prs or --events: not ready; another PR of the branch in the list: not ready
        for flag in ('--head-prs', '--events', '--other-events'):
            with self.subTest(without=flag):
                k = args.index(flag)
                r = self.cli(*(args[:k] + args[k + 2:]))
                # (the jobs of run 10 are gone since above: check-runs-unreadable follows)
                self.assertEqual(r.stdout.splitlines()[:1] + [l.split(':')[0] for l in r.stdout.splitlines()[1:]],
                                 ['not-ready', 'check-source-unreadable', 'check-runs-unreadable'])
        k = args.index('--head-prs')
        r = self.cli(*(args[:k + 1] + [self.file('other.json', HEAD_PRS + [rest_pr(8, 'x/attacker', state='closed')])] + args[k + 2:]))
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'check-source-other-pr')
        k = args.index('--events')
        r = self.cli(*(args[:k + 1] + [self.file('moved.json', [{'event': 'base_ref_changed'}])] + args[k + 2:]))
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'check-source-base-changed')
        # round 4 review, finding 1: #5's own events show a base change, or are missing: not ready
        self.file('other-events/5.json', [{'event': 'base_ref_changed'}])
        r = self.cli(*args)
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'check-source-other-pr')
        self.assertIn(f'#5 (closed) into {BASE}, whose base was changed', r.stdout)
        os.remove(os.path.join(self.top, 'other-events', '5.json'))
        self.file('other-events/junk.json', [])
        r = self.cli(*args)
        self.assertEqual(r.stdout.splitlines()[1].split(':')[0], 'check-source-unreadable')
        # same-base-prs: the numbers whose events the caller reads
        r = self.cli('same-base-prs', args[args.index('--head-prs') + 1], '--pr', str(PR), '--base', BASE, '--head-ref', HEAD_REF,
                     '--release-app', APP)
        self.assertEqual((r.returncode, r.stdout), (0, '5\n'))
        r = self.cli('same-base-prs', args[args.index('--head-prs') + 1], '--pr', str(PR), '--base', BASE, '--head-ref', HEAD_REF,
                     '--release-app', 'other[bot]')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        # sources that are not an object cannot be read
        j = args.index('--sources')
        r = self.cli(*(args[:j + 1] + [self.file('bad.json', [])] + args[j + 2:]))
        self.assertEqual(r.returncode, 1)
        this = ['--pr', str(PR), '--base', BASE, '--repo', REPO]
        r = self.cli('source-runs', '--required', ','.join(REQUIRED), '--sources', args[j + 1], '--head-ref', HEAD_REF, '--head', HEAD,
                     '--check-runs', args[i + 1], '--actions-runs', args[args.index('--actions-runs') + 1], *this)
        self.assertEqual((r.returncode, r.stdout), (0, '10\n'))
        # another PR's number or base: no run of this PR to read
        for k, v in (('--pr', '8'), ('--base', 'x/other')):
            with self.subTest(flag=k):
                other = list(this)
                other[other.index(k) + 1] = v
                r = self.cli('source-runs', '--required', ','.join(REQUIRED), '--sources', args[j + 1], '--head-ref', HEAD_REF, '--head', HEAD,
                             '--check-runs', args[i + 1], '--actions-runs', args[args.index('--actions-runs') + 1], *other)
                self.assertEqual((r.returncode, r.stdout), (0, ''))
        # ready without the base or the repo ties no run to this PR
        for flag in ('--base', '--repo'):
            with self.subTest(without=flag):
                os.makedirs(os.path.join(self.top, 'jobs'), exist_ok=True)
                a = self.ready_args()
                k = a.index(flag)
                r = self.cli(*(a[:k] + a[k + 2:]))
                self.assertEqual(r.stdout.splitlines()[:2][0], 'not-ready')
                # without the base, every PR of the head branch's list reads as one into another base
                self.assertEqual([l.split(':')[0] for l in r.stdout.splitlines()[1:]],
                                 ['check-source-other-pr', 'check-source-base-changed'] * (flag == '--base') + ['check-source-missing'])

    def test_approvers(self):
        reviews = [review('alice'), review('bob', 'CHANGES_REQUESTED'), review('ci[bot]', kind='Bot'), review('carol', commit=OLD)]
        r = self.cli('approvers', '--reviews', self.file('reviews.json', reviews))
        self.assertEqual((r.returncode, r.stdout), (0, 'alice\ncarol\n'))

    def test_candidates(self):
        prs = [pr(5, 'merge-up/x/1.x.x/abc1234'), pr(3, 'merge-up/x/1.2.x/0123456', isDraft=True), pr(4, 'feature')]
        r = self.cli('candidates', self.file('prs.json', prs), '--hint-pr', '5')
        self.assertEqual((r.returncode, r.stdout), (0, f'candidate 5 x/1.x.x x/main {5:040d} merge-up/x/1.x.x/abc1234\nskipped 3 a draft\n'))

    def test_busy_and_job_runs(self):
        runs = [{'id': 1, 'path': '.github/workflows/release-merge-up.yml', 'status': 'pending', 'html_url': 'u1'}]
        pin = [{'id': 9, 'path': '.github/workflows/pin-propagation.yml', 'status': 'in_progress', 'html_url': 'u9'}]
        path = self.file('runs.json', json.dumps({'workflow_runs': runs}) + '\n' + json.dumps({'workflow_runs': pin}) + '\n')
        r = self.cli('busy', path, '--workflow', 'release-merge-up.yml')
        self.assertEqual((r.returncode, r.stdout), (0, 'release-merge-up.yml run u1 is pending\n'))
        r = self.cli('busy', path, '--workflow', 'release-promote.yml', '--job', 'pin-propagation.yml=finish', '--jobs-dir', self.top)
        self.assertEqual((r.returncode, r.stdout), (0, 'pin-propagation.yml run u9 is in_progress and its jobs could not be read\n'))
        self.file('9.json', {'total_count': 1, 'jobs': [{'name': 'finish', 'status': 'queued'}]})
        r = self.cli('busy', path, '--workflow', 'release-promote.yml', '--job', 'pin-propagation.yml=finish', '--jobs-dir', self.top)
        self.assertEqual((r.returncode, r.stdout), (0, 'pin-propagation.yml run u9: job finish is queued\n'))
        r = self.cli('busy', self.file('none.json', {'total_count': 0, 'workflow_runs': []}), '--workflow', 'release-merge-up.yml')
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        r = self.cli('busy', os.path.join(self.top, 'missing.json'), '--workflow', 'release-merge-up.yml')
        self.assertEqual(r.returncode, 1)
        r = self.cli('job-runs', path, '--job', 'pin-propagation.yml=finish')
        self.assertEqual((r.returncode, r.stdout), (0, '9\n'))
        r = self.cli('job-runs', path, '--job', 'nonsense')
        self.assertEqual(r.returncode, 1)

    def test_marker_and_stop_due(self):
        r = self.cli('marker', 'stopped', HEAD)
        self.assertEqual((r.returncode, r.stdout), (0, f'<!-- merge-up-auto-finish: stopped head={HEAD} -->\n'))
        self.assertEqual(self.cli('marker', 'stopped', 'abc').returncode, 1)
        self.assertEqual(self.cli('stop-due', 'checks-pending', 'stale-approval').returncode, 0)
        self.assertEqual(self.cli('stop-due', 'checks-pending').returncode, 1)
        self.assertEqual(self.cli('stop-due').returncode, 1)


class IsolationTests(unittest.TestCase):
    """auto_finish.py is one file in several repositories: it names none of them and no product."""

    def test_it_names_no_repository_and_no_product(self):
        with open(os.path.join(SCRIPTS, 'auto_finish.py')) as f:
            text = f.read()
        with open(os.path.join(os.path.dirname(SCRIPTS), 'release', 'config.json')) as f:
            products = json.load(f).get('products') or []
        names = set(products)
        url = subprocess.run(['git', '-C', SCRIPTS, 'remote', 'get-url', 'origin'], capture_output=True, text=True).stdout.strip()
        m = re.search(r'[/:]([^/:]+)/([^/]+?)(\.git)?$', url)
        if m:
            names.update({m.group(1), m.group(2)})
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertIsNone(re.search(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', text))
        for word in ('repository_dispatch', '/dispatches', 'gh workflow run'):
            self.assertFalse(word in text, word)

    def test_it_imports_only_shared_helpers(self):
        with open(os.path.join(SCRIPTS, 'auto_finish.py')) as f:
            text = f.read()
        imported = re.search(r'from release_git import \(.*?\n(.*?)\)', text, re.S).group(1)
        self.assertEqual(sorted(n.strip() for n in imported.replace('\n', ' ').split(',') if n.strip()),
                         ['Error', 'latest_runs', 'person_reviews', 'pr_check', 'read_json_stream'])


if __name__ == '__main__':
    unittest.main()
