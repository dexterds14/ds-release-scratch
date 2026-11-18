"""Finishing a merge-up PR (ENG-2234, plan 6.1 and 6.2): who counts as an approver,
what merge-up.sh reads before it merges with the App's ruleset bypass, and what it
does around the merge.

The MergeUpTests fixture of test_release_git.py (one bare repo, the stub gh):
1.x.x merges up into main and foo.txt conflicts, so PR 1 is the conflict PR on
merge-up/1.x.x/<sha7>.
"""
import json
import os
import shutil
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
        self.assertNotIn('pr merge', self.fx.gh_log())

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
        self.assertNotIn('pr merge', self.fx.gh_log())


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
        self.assertIn(f'then dispatch release-merge-up.yml on the default branch with child=`{CHILD}` and pr=<this PR\'s number>, dry_run=false.', body)
        self.assertIn(f'Dispatch it on the default branch, not on `{CHILD}`: a run uses the scripts and the release/config.json of the ref it is dispatched on', body)
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
        self.assertIn(f'dispatch release-merge-up.yml on the default branch ({PARENT}) with child=`{CHILD}`', self.fx.prs()[0]['body'])
        self.assertIn(f'Dispatch it on the default branch ({PARENT}), not on `{CHILD}`', self.fx.prs()[0]['body'])

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
        self.assertIn('dispatch release-merge-up.yml on the default branch with child=', body)
        self.assertIn('Dispatch it on the default branch, not on', body)

    def test_no_instruction_in_the_script_leaves_the_ref_open(self):
        with open(MERGE_UP) as f:
            text = f.read()
        lines = [line for line in text.splitlines() if 'dispatch release-merge-up.yml' in line]
        # the header comment has none: it says "a pr=N dispatch". The eighth is the dead-branch stop
        # for a PR list that could not be read (its other form says "a dispatch of ... $ON_DEFAULT")
        self.assertEqual(len(lines), 8)
        for line in lines:
            # the PR bodies say it before the inputs, the ::error:: lines after them
            self.assertEqual(line.count('dispatch release-merge-up.yml'), 1, line)
            self.assertRegex(line, r'dispatch release-merge-up\.yml (\$ON_DEFAULT with child=|with child=\$CHILD pr=\$\{[^}]+\} \$ON_DEFAULT"$)')

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
        ref = git(os.path.dirname(SCRIPTS), 'symbolic-ref', '--short', 'HEAD')
        r = self.finish(dry_run='true', extra_env={'DEFAULT_BRANCH': ref})
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


class MarkerTests(unittest.TestCase):
    """release_git.py markers, what pr-checks runs on a merge-up/** head (6.1)."""

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
        with open(MERGE_UP) as f:
            self.assertIn("grep -l -E '" + release_git.MARKER_RE + "'", f.read())


class PrChecksWorkflowTests(unittest.TestCase):
    """pr-checks.yml refuses markers on a merge-up/** head, so the UI cannot merge them either."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/pr-checks.yml')) as f:
            self.text = f.read()
        start = self.text.index('      - name: No conflict markers on a merge-up branch\n')
        self.step = self.text[start:self.text.index('\n\n', start)]

    def test_the_step(self):
        self.assertIn("        if: github.event_name == 'pull_request' && startsWith(github.head_ref, 'merge-up/')\n", self.step)
        self.assertIn('BASE_SHA: ${{ github.event.pull_request.base.sha }}\n', self.step)
        self.assertIn('HEAD_SHA: ${{ github.event.pull_request.head.sha }}\n', self.step)
        self.assertIn('python3 scripts-release/release_git.py markers --base "${BASE_SHA}" --head "${HEAD_SHA}"', self.step)
        self.assertIn('exit 1\n', self.step)
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
        self.assertNotIn('pr merge', self.fx.gh_log())
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


if __name__ == '__main__':
    unittest.main()
