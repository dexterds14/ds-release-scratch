"""merge_up_audit.py (ENG-2234, plan section 8): the merge-up census as a command.

No network: the real answers of the four repos, as `merge_up_audit.py --since
2026-11-02 --record` saved them on 2026-11-13 (fixtures/merge-up-audit/), are
replayed, and the shapes that have not happened yet (a rule-resolved PR, the
lockfile rule's PR, a direct push with a Merge-Up-Rule trailer) are built here
in the form the GitHub REST API answers.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import merge_up_audit as audit  # noqa: E402

RECORDING = os.path.join(HERE, 'fixtures', 'merge-up-audit', 'recorded-2026-11-13.json')
DISTRO, META, PORTAL, DS = (f'DistributedSpectrum/{r}' for r in audit.REPOS)
BOT = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}
NEVER = ('.github/workflows/**', '*sudoers*')
LOCK = 'kas/include/base.lock.yml'
PINS = 'conf/distro/include/ds-src-pins.inc'


def user(login):
    return {'login': login, 'type': 'User'}


def sha(c):
    return (c * 40)[:40]


def commit(c, parents=(), author='alice', message='work'):
    """One commit as the REST API lists it. author: a login, a user object, or None (unlinked)."""
    who = user(author) if isinstance(author, str) else author
    return {'sha': sha(c), 'parents': [{'sha': sha(p)} for p in parents], 'author': who,
            'commit': {'message': message, 'author': {'name': 'Some Name'}, 'committer': {'date': '2026-12-01T10:00:00Z'}}}


def pull(number, child='1.0.x', tip='c', base='1.x.x', title=None, state='closed', created='2026-12-01T09:00:00Z', files=('a.txt',)):
    body = 'release-merge-up stopped: conflicts in:\n\n' + ''.join(f'- `{f}`\n' for f in files) + '\nResolve on this branch.\n'
    return {'number': number, 'title': title or f'Merge-up {child} into {base} (conflict)', 'state': state,
            'created_at': created, 'closed_at': None, 'merged_at': None, 'user': BOT,
            'head': {'ref': f'merge-up/{child}/{sha(tip)[:7]}', 'sha': sha('f')}, 'base': {'ref': base}, 'body': body}


def detail(number, head='d', merged=True, merged_by=None, state='closed', commits=None):
    return {'number': number, 'state': state, 'merged': merged, 'merged_by': merged_by, 'head': {'sha': sha(head)},
            'commits': commits, 'changed_files': 1}


def review(login, state='APPROVED', on='d', at='2026-12-01T11:00:00Z', kind='User'):
    return {'user': {'login': login, 'type': kind}, 'state': state, 'commit_id': sha(on), 'submitted_at': at}


class Fake:
    """A repo as raw REST answers, keyed the way the audit asks for them."""

    def __init__(self, repo='DistributedSpectrum/ds', since='2026-12-01', branches=('main', '1.x.x', '1.0.x', 'feature/x')):
        self.repo, self.since = repo, since
        self.raw = {f'repos/{repo}/branches?per_page=100&page=1': [{'name': b} for b in branches],
                    f'repos/{repo}/pulls?state=all&sort=created&direction=desc&per_page=100&page=1': []}
        for b in branches:
            if audit.LINE_RE.fullmatch(b):
                self.line(b, [])
        self.asked = []

    def line(self, branch, commits):
        self.raw[f'repos/{self.repo}/commits?sha={branch}&since={self.since}T00:00:00Z&per_page=100&page=1'] = commits

    def pr(self, listed, det, commits, reviews=()):
        n = listed['number']
        self.raw[f'repos/{self.repo}/pulls?state=all&sort=created&direction=desc&per_page=100&page=1'].append(listed)
        self.raw[f'repos/{self.repo}/pulls/{n}'] = det
        self.raw[f'repos/{self.repo}/pulls/{n}/commits?per_page=100&page=1'] = list(commits)
        self.raw[f'repos/{self.repo}/pulls/{n}/reviews?per_page=100&page=1'] = list(reviews)

    def fetch(self, path):
        self.asked.append(path)
        return self.raw.get(path)

    def run(self):
        return audit.audit_repo(audit.Reader(fetch=self.fetch), self.repo, self.since)


class RecordedTests(unittest.TestCase):
    """The four repos since 2026-11-02, as read on 2026-11-13."""

    @classmethod
    def setUpClass(cls):
        with open(RECORDING) as f:
            cls.doc = json.load(f)
        reader = audit.Reader(replay=cls.doc['responses'], fetch=lambda path: cls.fail(cls, f'network: {path}'))
        cls.report = audit.audit(reader, cls.doc['repos'], cls.doc['since'])
        cls.repos = {r['repo']: r for r in cls.report['repos']}
        cls.prs = {(r['repo'], p['number']): p for r in cls.report['repos'] for p in r['prs']}

    def pr(self, repo, number):
        return self.prs[(repo, number)]

    def test_counts_per_repo(self):
        self.assertEqual({r: v['counts']['prs'] for r, v in self.repos.items()}, {DISTRO: 11, META: 9, PORTAL: 9, DS: 2})
        self.assertEqual(sum(v['counts']['rule_commits'] for v in self.repos.values()), 0)

    def test_what_was_examined_is_reported(self):
        ex = self.repos[DISTRO]['examined']
        self.assertEqual((ex['prs_listed'], ex['pr_pages']), (100, 1))
        self.assertEqual(ex['lines'], ['aggregator/main', 'vertex/1.3.x', 'vertex/1.x.x', 'vertex/main'])
        self.assertGreater(ex['line_commits'], 0)

    def test_the_three_prs_the_first_census_did_not_hold(self):
        # 28 on 2026-11-12 (11, 8, 8, 1); these three were opened that day
        late = sorted((r.rsplit('/', 1)[-1], p['number']) for (r, _), p in self.prs.items() if p['created_at'] >= '2026-11-12T18:50')
        self.assertEqual(late, [('ds', 618), ('meta-dsensos', 187), ('sensor-portal', 333)])

    def test_a_bot_finish(self):
        p = self.pr(DISTRO, 173)
        self.assertEqual((p['class'], p['files']), ('lock-only', [LOCK]))
        self.assertEqual((p['child'], p['parent'], p['kind']), ('vertex/1.x.x', 'vertex/main', 'conflict'))
        self.assertEqual((p['resolvers'], p['approvers']), (['eric-distributed-spectrum'], ['dexterds14']))
        self.assertEqual((p['approval_on_head'], p['second_person']), (True, True))
        self.assertEqual((p['merged_how'], p['merged_by']), ('bot finish', 'ds-os-release-bot[bot]'))

    def test_an_approval_older_than_the_resolution(self):
        p = self.pr(DISTRO, 162)
        self.assertEqual((p['approval_on_head'], p['second_person'], p['merged_how']), (False, True, 'UI'))
        self.assertEqual(p['merged_by'], 'eric-distributed-spectrum')
        not_on_head = sorted((r.rsplit('/', 1)[-1], n) for (r, n), q in self.prs.items() if q['approval_on_head'] is False)
        self.assertEqual(not_on_head, [('ds', 616), ('ds-tegra-distro', 162), ('meta-dsensos', 182), ('sensor-portal', 297)])

    def test_resolver_approved_their_own_resolution(self):
        p = self.pr(DISTRO, 170)
        self.assertEqual((p['resolvers'], p['approvers'], p['second_person']), (['dexterds14'], ['dexterds14'], False))
        self.assertEqual(sum(v['counts']['approved_only_by_the_resolver'] for v in self.repos.values()), 12)

    def test_superseded_with_no_resolution(self):
        p = self.pr(DISTRO, 160)
        self.assertEqual((p['resolvers'], p['merged_how']), ([], 'closed, head reached the parent elsewhere'))
        self.assertEqual(self.pr(PORTAL, 295)['merged_how'], 'closed, not merged')
        self.assertEqual(self.pr(PORTAL, 289)['merged_how'], 'indirect')
        untouched = [k for k, q in self.prs.items() if q['state'] == 'closed' and q['resolvers'] == []]
        self.assertEqual(len(untouched), 7)

    def test_bot_merged_pr_that_github_shows_closed(self):
        p = self.pr(META, 182)
        self.assertEqual((p['state'], p['merged_how'], p['merged_by']), ('closed', 'bot finish (PR left closed)', 'ds-os-release-bot[bot]'))
        finishes = sum(n for v in self.repos.values() for k, n in v['counts']['by_merge'].items() if k.startswith('bot finish'))
        self.assertEqual(finishes, 3)

    def test_a_bot_review_is_listed_and_not_counted(self):
        p = self.pr(PORTAL, 287)
        self.assertEqual((p['approvers'], p['bot_approvals'], p['approval_on_head']), ([], ['github-actions[bot]'], None))
        self.assertEqual(self.repos[PORTAL]['counts']['bot_approval_only'], 1)
        p = self.pr(PORTAL, 297)  # a person approved the bot's commit, then github-actions the head
        self.assertEqual((p['approvers'], p['bot_approvals'], p['approval_on_head']), (['eric-distributed-spectrum'], ['github-actions[bot]'], False))

    def test_classes(self):
        self.assertEqual(self.repos[DISTRO]['counts']['by_class'], {'lock-only': 7, 'never-auto': 4})
        self.assertEqual(self.repos[META]['counts']['by_class'], {'other': 3, 'pins': 6})
        self.assertEqual(self.repos[PORTAL]['counts']['by_class'], {'never-auto': 5, 'other': 4})
        self.assertEqual(self.pr(DISTRO, 172)['tags'], ['never-auto', 'lock+other', 'other'])
        self.assertEqual(self.pr(PORTAL, 297)['tags'], ['never-auto', 'other'])
        self.assertEqual((self.pr(META, 187)['class'], self.pr(META, 187)['merged_how']), ('pins', 'open'))
        self.assertEqual(self.pr(DS, 618)['files'], ['containers/sdr/BladeRFInterface.cc'])

    def test_cli_replay_prints_the_report_and_asks_nothing(self):
        out = io.StringIO()
        with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('network')), contextlib.redirect_stdout(out):
            self.assertEqual(audit.main(['--replay', RECORDING]), 0)
        text = out.getvalue()
        self.assertIn('total: 31 merge-up PR(s) since 2026-11-02 (ds-tegra-distro 11, meta-dsensos 9, sensor-portal 9, ds 2); '
                      '0 Merge-Up-Rule commit(s)', text)
        self.assertIn('  #173 2026-11-12 vertex/1.x.x -> vertex/main  conflict  class=lock-only\n'
                      '       resolver=eric-distributed-spectrum  approver=dexterds14  on head=yes  second person=yes  '
                      'merged: bot finish by ds-os-release-bot[bot]\n       files: kas/include/base.lock.yml\n', text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(audit.main(['--replay', RECORDING, '--repo', 'ds', '--json']), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual([(r['repo'], r['counts']['prs']) for r in doc['repos']], [(DS, 2)])

    def test_replay_never_reaches_past_the_recording(self):
        responses = dict(self.doc['responses'])
        del responses[f'repos/{DS}/pulls/618/reviews?per_page=100&page=1']
        with self.assertRaisesRegex(audit.Error, 'pulls/618/reviews.* is not in the recording'):
            audit.audit(audit.Reader(replay=responses), [DS], self.doc['since'])


class ShapeTests(unittest.TestCase):
    """What the rules will leave behind, and the edges of each column."""

    def test_rule_resolved_pr_with_nobody_above_the_bot(self):
        fake = Fake()
        bot = commit('b', parents=('p', 'c'), author=BOT,
                     message='Merge 1.0.x into 1.x.x\n\nMerge-Up-Rule: splice files=2 regions=3\nMerge-Up-Rule: pins SENSOR_PORTAL_REV=child parent=1 child=2\n')
        fake.pr(pull(7, title='Merge-up 1.0.x into 1.x.x (resolved by rule)'), detail(7, head='b', merged_by=BOT),
                [commit('c'), bot], [review('alice', on='b')])
        fake.line('1.x.x', [commit('m', parents=('p', 'b'), author=BOT, message='Merge pull request #7 from x/merge-up/1.0.x/ccccccc'), bot])
        r = fake.run()
        p = r['prs'][0]
        self.assertEqual((p['kind'], p['rules'], p['resolvers']), ('rule-resolved', ['pins', 'splice'], []))
        self.assertEqual((p['approvers'], p['approval_on_head'], p['second_person'], p['merged_how']), (['alice'], True, True, 'bot finish'))
        self.assertEqual([(k['sha'], k['landed'], k['pr'], k['lines']) for k in r['rule_commits']], [(sha('b'), 'in PR', 7, ['1.x.x'])])
        self.assertEqual(r['rule_commits'][0]['rules'], [{'rule': 'splice', 'detail': 'files=2 regions=3'},
                                                        {'rule': 'pins', 'detail': 'SENSOR_PORTAL_REV=child parent=1 child=2'}])
        self.assertEqual((r['counts']['rule_commits'], r['counts']['rule_commits_direct_push']), (1, 0))

    def test_direct_push_of_the_lock_rule_and_its_bot_pr(self):
        fake = Fake(repo=DISTRO, branches=('vertex/main', 'vertex/1.x.x', 'pin-bot/vertex/1.x.x/9', 'main-old'))
        trailer = 'Merge vertex/1.x.x into vertex/main\n\nMerge-Up-Rule: lock-pin tip=aa parent-pin=bb child-pin=cc\n'
        push = commit('e', parents=('p', 'c'), author=BOT, message=trailer)
        fake.line('vertex/main', [push])
        fake.line('vertex/1.x.x', [])
        fake.pr(pull(9, child='vertex/1.x.x', base='vertex/main', title='Merge-up vertex/1.x.x into vertex/main (lockfile pin set by rule)',
                     files=(LOCK,)), detail(9, head='b', merged_by=BOT), [commit('c'), commit('b', parents=('p', 'c'), author=BOT, message=trailer)])
        r = fake.run()
        self.assertEqual(r['examined']['lines'], ['vertex/1.x.x', 'vertex/main'])
        p = r['prs'][0]
        self.assertEqual((p['kind'], p['class'], p['rules'], p['approvers'], p['merged_how']), ('lock-rule', 'lock-only', ['lock-pin'], [], 'bot finish'))
        k = r['rule_commits'][0]
        self.assertEqual((k['sha'], k['landed'], k['pr'], k['rules'][0]['rule']), (sha('e'), 'direct push', None, 'lock-pin'))
        self.assertEqual(r['counts']['rule_commits_direct_push'], 1)
        text = audit.render({'since': '2026-12-01', 'repos': [r]})
        self.assertIn(f'  {sha("e")[:7]} 2026-12-01 on vertex/main  direct push  lock-pin tip=aa parent-pin=bb child-pin=cc  | Merge vertex/1.x.x into vertex/main', text)

    def test_a_trailer_only_counts_at_the_start_of_a_line(self):
        self.assertEqual(audit._trailers('x\n\nsee Merge-Up-Rule: splice\n  Merge-Up-Rule: pins\nMerge-Up-Rule: lock-pin tip=1\n'), [['lock-pin', 'tip=1']])

    def test_resolver_is_whoever_committed_above_the_child_tip(self):
        commits = [audit.slim_commit(c) for c in (
            commit('1', author='old'),                                   # the child's own history
            commit('c', parents=('1',), author='old'),                   # the child tip the branch is named after
            commit('b', parents=('p', 'c'), author=BOT),                 # the bot's conflict commit
            commit('2', parents=('b',), author='bob'),
            commit('3', parents=('2',), author=None),                    # an email no account is linked to
            commit('4', parents=('3', 'q'), author='bob'))]
        top = audit.above(commits, sha('c')[:7])
        self.assertEqual([c['sha'][0] for c in top], ['b', '2', '3', '4'])
        self.assertEqual([audit.commit_person(c) for c in top], [None, 'bob', 'Some Name', 'bob'])
        self.assertIsNone(audit.above(commits, 'abcdef0'))

    def test_child_tip_missing_from_the_listing_is_unknown_not_nobody(self):
        fake = Fake()
        fake.pr(pull(3, tip='9'), detail(3, merged_by=user('alice'), commits=300), [commit('2', author='bob')], [review('alice')])
        p = fake.run()['prs'][0]
        self.assertEqual((p['resolvers'], p['second_person']), (None, None))
        self.assertEqual(len(p['notes']), 2)
        self.assertIn('the API lists 1 of 300 commits', p['notes'][0])
        self.assertIn('resolver=?', audit.render({'since': 's', 'repos': [dict(fake.run(), prs=[p])]}))

    def test_the_latest_verdict_of_each_person_counts(self):
        slim = audit.slim_reviews([
            review('alice', at='2026-12-01T10:00:00Z'), review('alice', state='CHANGES_REQUESTED', at='2026-12-01T12:00:00Z'),
            review('bob', state='CHANGES_REQUESTED', at='2026-12-01T10:00:00Z'), review('bob', on='e', at='2026-12-01T12:00:00Z'),
            review('bob', state='COMMENTED', at='2026-12-01T13:00:00Z'),
            review('carol', at='2026-12-01T10:00:00Z'), review('carol', state='DISMISSED', at='2026-12-01T11:00:00Z'),
            review('github-actions[bot]', kind='Bot'), review('looks-like-a-person', kind='Bot')])
        people, bots = audit.approvals(slim)
        self.assertEqual(people, [{'login': 'bob', 'commit_id': sha('e')}])
        self.assertEqual(bots, ['github-actions[bot]', 'looks-like-a-person'])

    def test_classes(self):
        for files, want in (
                ([], ('unknown', ['unknown'])),
                ([LOCK], ('lock-only', ['lock-only'])),
                ([LOCK, 'a.txt'], ('lock+other', ['lock+other'])),
                ([LOCK, PINS], ('pins', ['pins', 'lock+other'])),
                ([PINS], ('pins', ['pins'])),
                ([PINS, 'a.txt'], ('pins', ['pins', 'other'])),
                (['a/b/50-ds-sudoers'], ('never-auto', ['never-auto'])),
                (['.github/workflows/x.yml', LOCK], ('never-auto', ['never-auto', 'lock+other'])),
                (['.github/workflows/x.yml', 'a.txt'], ('never-auto', ['never-auto', 'other'])),
                (['a.txt'], ('other', ['other']))):
            self.assertEqual(audit.classify(files, NEVER), want, files)

    def test_never_auto_lists_per_repo(self):
        self.assertIn('scripts-secboot/**', audit.never_auto_patterns(DISTRO))
        self.assertIn('recipes-bsp/uefi/**', audit.never_auto_patterns(META))
        self.assertNotIn('recipes-bsp/uefi/**', audit.never_auto_patterns(DISTRO))
        self.assertEqual(audit.never_auto_patterns(PORTAL), audit.never_auto_patterns(DS))
        self.assertEqual(audit.classify(['recipes-bsp/uefi/files/x'], audit.never_auto_patterns(META))[0], 'never-auto')

    def test_body_files(self):
        body = 'conflicts in:\n\n- `a b.txt`\n- `x/y.sh`\n- `a b.txt`\n\ncheck out `merge-up/1.0.x/abc1234`, edit\n- not a file\n'
        self.assertEqual(audit.body_files(body), ['a b.txt', 'x/y.sh'])
        self.assertEqual(audit.body_files(None), [])

    def test_closed_pr_asks_whether_the_parent_holds_its_head(self):
        for status, want in (('behind', 'closed, head reached the parent elsewhere'), ('identical', 'closed, head reached the parent elsewhere'),
                             ('diverged', 'closed, not merged'), ('ahead', 'closed, not merged'), (None, 'closed, head not found')):
            fake = Fake()
            fake.pr(pull(5), detail(5, merged=False), [commit('c')])
            path = f'repos/{fake.repo}/compare/1.x.x...{sha("d")}'
            if status:
                fake.raw[path] = {'status': status}
            self.assertEqual(fake.run()['prs'][0]['merged_how'], want)
            self.assertIn(path, fake.asked)

    def test_open_and_merged_prs_need_no_compare(self):
        fake = Fake()
        fake.pr(pull(5, state='open'), detail(5, merged=False, state='open'), [commit('c')])
        fake.pr(pull(6), detail(6, merged=True, merged_by=None), [commit('c')])
        fake.pr(pull(8), detail(8, merged=True, merged_by=user('alice')), [commit('c')])
        self.assertEqual([(p['merged_how'], p['merged_by']) for p in fake.run()['prs']], [('open', None), ('indirect', None), ('UI', 'alice')])
        self.assertFalse([p for p in fake.asked if '/compare/' in p])

    def test_person_merged_a_pr_github_shows_closed(self):
        fake = Fake()
        fake.pr(pull(5), detail(5, merged=False), [commit('c')])
        fake.line('1.x.x', [commit('m', author='alice', message='Merge pull request #5 from x/y\n\nbody')])
        p = fake.run()['prs'][0]
        self.assertEqual((p['merged_how'], p['merged_by']), ('UI (PR left closed)', 'alice'))

    def test_only_merge_up_branches_and_only_since_the_date(self):
        fake = Fake()
        fake.pr(pull(5), detail(5, merged_by=user('a')), [commit('c')])
        fake.pr(pull(4, created='2026-11-30T23:59:59Z'), detail(4), [commit('c')])
        other = pull(3)
        other['head']['ref'] = 'dexter/merge-up-docs'
        fake.pr(other, detail(3), [commit('c')])
        r = fake.run()
        self.assertEqual([p['number'] for p in r['prs']], [5])
        self.assertEqual(r['examined']['prs_listed'], 3)
        self.assertFalse([p for p in fake.asked if '/pulls/4' in p or '/pulls/3' in p])

    def test_the_pr_listing_is_walked_to_the_date_and_no_further(self):
        fake = Fake()
        key = f'repos/{fake.repo}/pulls?state=all&sort=created&direction=desc&per_page=100&page='
        other = pull(1, created='2026-12-02T00:00:00Z')
        other['head']['ref'] = 'feature'
        fake.raw[key + '1'] = [other] * 100
        fake.raw[key + '2'] = [other] * 99 + [dict(other, created_at='2026-11-01T00:00:00Z')]
        fake.raw[key + '3'] = [other] * 100
        r = fake.run()
        self.assertEqual((r['examined']['prs_listed'], r['examined']['pr_pages']), (200, 2))
        self.assertNotIn(key + '3', fake.asked)

    def test_a_long_line_history_is_read_to_its_end(self):
        fake = Fake()
        key = f'repos/{fake.repo}/commits?sha=main&since=2026-12-01T00:00:00Z&per_page=100&page='
        fake.raw[key + '1'] = [commit('1')] * 100
        fake.raw[key + '2'] = [commit('e', author=BOT, message='Merge 1.x.x into main\n\nMerge-Up-Rule: lock-pin tip=1\n')]
        r = fake.run()
        self.assertEqual((r['examined']['line_commits'], len(r['rule_commits'])), (101, 1))

    def test_a_failed_read_is_exit_2_and_no_report(self):
        fake = Fake()
        fake.pr(pull(5), detail(5, merged_by=user('a')), [commit('c')])
        del fake.raw[f'repos/{fake.repo}/pulls/5/reviews?per_page=100&page=1']  # a 404 where an answer is required
        out, err = io.StringIO(), io.StringIO()

        def fake_run(argv, **kw):
            raw = fake.raw.get(argv[-1])
            if raw is None:
                return mock.Mock(returncode=1, stdout='', stderr='gh: Not Found (HTTP 404)')
            return mock.Mock(returncode=0, stdout=json.dumps(raw), stderr='')
        with mock.patch.object(audit.subprocess, 'run', fake_run), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = audit.main(['--since', '2026-12-01', '--repo', 'ds'])
        self.assertEqual((rc, out.getvalue()), (2, ''))
        self.assertIn('pulls/5/reviews?per_page=100&page=1: not found (404)', err.getvalue())

    def test_a_failed_request_is_exit_2_and_no_report(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(audit.subprocess, 'run', lambda argv, **kw: mock.Mock(returncode=1, stdout='', stderr='gh: Bad credentials (HTTP 401)')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = audit.main(['--since', '2026-12-01'])
        self.assertEqual((rc, out.getvalue()), (2, ''))
        self.assertIn('Bad credentials (HTTP 401)', err.getvalue())


class ReadOnlyTests(unittest.TestCase):
    """Every request is a GET of a repos/ path through gh."""

    def run_gh(self, path, rc=0, out='[]', err=''):
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            return mock.Mock(returncode=rc, stdout=out, stderr=err)
        with mock.patch.object(audit.subprocess, 'run', fake_run):
            return audit.gh_get(path), seen

    def test_argv(self):
        got, seen = self.run_gh('repos/DistributedSpectrum/ds/pulls/5', out='{"number": 5}')
        self.assertEqual(got, {'number': 5})
        self.assertEqual(seen, [['gh', 'api', '--method', 'GET', '-H', 'Accept: application/vnd.github+json', 'repos/DistributedSpectrum/ds/pulls/5']])

    def test_a_whole_audit_only_ever_gets(self):
        fake = Fake(repo=DISTRO, branches=('vertex/main', 'vertex/1.x.x'))
        fake.pr(pull(5, child='vertex/1.x.x', base='vertex/main'), detail(5, merged=False), [commit('c')], [review('alice')])
        seen = []

        def fake_run(argv, **kw):
            seen.append(argv)
            raw = fake.raw.get(argv[-1])
            if raw is None:
                return mock.Mock(returncode=1, stdout='{"message":"Not Found"}', stderr='gh: Not Found (HTTP 404)')
            return mock.Mock(returncode=0, stdout=json.dumps(raw), stderr='')
        with mock.patch.object(audit.subprocess, 'run', fake_run):
            r = audit.audit_repo(audit.Reader(), DISTRO, '2026-12-01')
        self.assertEqual(r['prs'][0]['merged_how'], 'closed, head not found')
        self.assertGreaterEqual(len(seen), 7)
        for argv in seen:
            self.assertEqual(argv[:6], ['gh', 'api', '--method', 'GET', '-H', 'Accept: application/vnd.github+json'])
            self.assertEqual(len(argv), 7)
            self.assertTrue(argv[6].startswith(f'repos/{DISTRO}/'), argv)

    def test_paths_that_are_not_a_repo_read_are_refused(self):
        for path in ('graphql', 'repos/o/r', '/repos/o/r/pulls', 'repos/o/r/pulls -X POST', 'orgs/o/actions/variables', 'repos/o/r/pulls\n-f x=1', ''):
            with self.assertRaises(audit.Error, msg=path):
                audit.gh_argv(path)

    def test_404_is_none_and_any_other_failure_raises(self):
        self.assertEqual(self.run_gh('repos/o/r/pulls/1', rc=1, err='gh: Not Found (HTTP 404)')[0], None)
        with self.assertRaisesRegex(audit.Error, r'failed \(exit 1\): gh: API rate limit exceeded \(HTTP 403\)'):
            self.run_gh('repos/o/r/pulls/1', rc=1, err='gh: API rate limit exceeded (HTTP 403)')
        with self.assertRaisesRegex(audit.Error, 'not JSON'):
            self.run_gh('repos/o/r/pulls/1', out='<html>')

    def test_cli_refusals(self):
        for argv, want in ((['--since', '2026-12-01', '--repo', 'DistributedSpectrum/ai-marketplace'], 'not one of'),
                           (['--since', '2026-12-01', '--repo', 'someone/ds'], 'not one of'),
                           ([], '--since YYYY-MM-DD is required'), (['--since', 'yesterday'], '--since YYYY-MM-DD is required'),
                           (['--since', '2026-13-01'], 'month must be'),
                           (['--replay', RECORDING, '--since', '2026-11-05'], 'the recording was made with --since 2026-11-02'),
                           (['--replay', RECORDING, '--record', 'x.json'], 'do not go together')):
            err = io.StringIO()
            with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('network')), contextlib.redirect_stderr(err):
                self.assertEqual(audit.main(argv), 2, argv)
            self.assertIn(want, err.getvalue(), argv)

    def test_record_then_replay_gives_the_same_report(self):
        fake = Fake()
        fake.pr(pull(5), detail(5, merged_by=BOT), [commit('c'), commit('2', parents=('c',), author='bob')], [review('alice')])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'rec.json')
            first, second = io.StringIO(), io.StringIO()
            with mock.patch.object(audit.subprocess, 'run',
                                   lambda argv, **kw: mock.Mock(returncode=0, stdout=json.dumps(fake.raw[argv[-1]]), stderr='')):
                with contextlib.redirect_stdout(first):
                    self.assertEqual(audit.main(['--since', '2026-12-01', '--repo', 'ds', '--record', path]), 0)
            with mock.patch.object(audit.subprocess, 'run', side_effect=AssertionError('network')), contextlib.redirect_stdout(second):
                self.assertEqual(audit.main(['--replay', path]), 0)
            with open(path) as f:
                doc = json.load(f)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertIn('#5 2026-12-01 1.0.x -> 1.x.x  conflict  class=other', first.getvalue())
        self.assertEqual((doc['since'], doc['repos']), ('2026-12-01', [DS]))


if __name__ == '__main__':
    unittest.main()
