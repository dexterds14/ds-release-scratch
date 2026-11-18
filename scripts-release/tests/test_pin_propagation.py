"""Acceptance fixtures: real temporary git histories, stubbed GitHub, no network."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import io
import contextlib
import base64
import os
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pin_policy as p
import pin_bot as b
import pin_artifacts as a
import pin_rules
import pin_token

A, B, C, D = (x * 40 for x in 'abcd')
PINS = f'# source\nDSREV ?= "{A}"\n\nDS_BRANCH ?= "main"\nSENSOR_PORTAL_REV ?= "{A}"\n'
LOCK = f'header:\n  version: 14\noverrides:\n  repos:\n    poky:\n      commit: {A}\n    meta-dsensos:\n      commit: {A}\n'


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = p.policy()
        self.allowed = self.config['sources']

    def test_single_and_multiple(self):
        self.assertEqual(set(p.pin_changes(PINS, PINS.replace(A, B), self.allowed)),
                         {'DSREV', 'SENSOR_PORTAL_REV'})

    def test_adjacent_comment(self):
        after = PINS.replace('# source', '# updated\n# release').replace(A, B, 1)
        self.assertEqual(set(p.pin_changes(PINS, after, self.allowed)), {'DSREV'})

    def test_rejections(self):
        pin = PINS.replace(A, B, 1)
        for after in [pin.replace('main', '1.x.x'), pin + 'PV = "1"\n',
                      pin + f'NEW_REV ?= "{B}"\n', pin.replace('?=', '=', 1),
                      pin.replace('DSREV', 'UNKNOWN_REV'), pin.replace(B, 'short'),
                      pin.replace('\n\nDS_BRANCH', '\n# unrelated\n\nDS_BRANCH'),
                      pin.replace('\n\nDS_BRANCH', '\n\n# unrelated\nDS_BRANCH'),
                      pin + 'python () { evil(); }\n']:
            with self.subTest(after=after), self.assertRaises(p.Ineligible):
                p.pin_changes(PINS, after, self.allowed)

    def test_continued_line_not_comment(self):
        before = 'VALUE = "\\\n' + PINS
        with self.assertRaises(p.Ineligible):
            p.pin_changes(before, before.replace(A, B, 1), self.allowed)

    def test_duplicate_pin(self):
        with self.assertRaises(p.Review):
            p.pin_changes(PINS + PINS, PINS + PINS, self.allowed)

    def test_lock_replacement_preserves_other_layer(self):
        result = p.replace_lock(LOCK, A, B)
        self.assertIn(f'poky:\n      commit: {A}', result)
        self.assertEqual(p.lock_pin(result), B)
        p.verify_pr_delta(LOCK, result, A, B, [p.LOCKFILE])
        for after, files in [(result + '# unexpected\n', [p.LOCKFILE]),
                             (result, [p.LOCKFILE, 'README.md'])]:
            with self.assertRaises(p.Review):
                p.verify_pr_delta(LOCK, after, A, B, files)

    def test_route_and_source_branch(self):
        config = self.config
        base = 'repos:\n  meta-dsensos:\n    branch: vertex/1.2.x\n'
        p.route('vertex/1.2.x', base, config)
        for line in ['vertex/main', 'aggregator/1.x.x', 'dev/main', 'vertex/1foo.x.x']:
            with self.assertRaises(p.Review):
                p.route(line, base, config)
        mapping = config['sources']['DSREV']
        self.assertEqual(p.source_branch(PINS, mapping), 'main')
        for text in [PINS + 'DS_BRANCH ?= "other"\n', PINS.replace('?= "main"', '= "main"'),
                     PINS.replace('main', '${OTHER}'), PINS.replace('main', '../main')]:
            with self.assertRaises(p.Review):
                p.source_branch(text, mapping)

    def test_explicit_route_keeps_source_and_destination_histories_distinct(self):
        config = dict(self.config, routes={'fixture-source': 'vertex/1.x.x'})
        self.assertEqual(p.destination_line('fixture-source', config), 'vertex/1.x.x')
        self.assertEqual(p.destination_line('vertex/main', config), 'vertex/main')
        base = 'repos:\n  meta-dsensos:\n    branch: fixture-source\n'
        p.route('vertex/1.x.x', base, config, 'fixture-source')
        with self.assertRaises(p.Review):
            p.route('vertex/1.x.x', base, config, 'other-source')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.com')
        self.git('config', 'user.name', 'Fixture')
        self.path = self.root / p.policy()['pin_file']
        self.path.parent.mkdir(parents=True)
        self.path.write_text(PINS)
        self.first = self.commit()

    def git(self, *args):
        return p.git(self.root, *args)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')
        return self.git('rev-parse', 'HEAD')

    def classify(self, last):
        return p.classify_range(self.root, self.first, last, p.policy())

    def test_forward_pin_bumps(self):
        self.path.write_text(PINS.replace(A, B))
        self.commit()
        self.path.write_text(PINS.replace(A, C))
        self.assertEqual(self.classify(self.commit())['DSREV'], {'old': A, 'new': C})

    def test_reverted_functional_change(self):
        file = self.root / 'functional.bb'
        file.write_text('do_install() { evil; }\n')
        self.commit()
        file.unlink()
        self.path.write_text(PINS.replace(A, B))
        with self.assertRaises(p.Ineligible):
            self.classify(self.commit())

    def test_reverted_functional_pin_file_edit(self):
        self.path.write_text(PINS + 'INHERIT += "extra"\n')
        self.commit()
        self.path.write_text(PINS.replace(A, B))
        with self.assertRaises(p.Ineligible):
            self.classify(self.commit())

    def test_file_mode_change(self):
        self.path.write_text(PINS.replace(A, B))
        self.path.chmod(0o755)
        with self.assertRaises(p.Ineligible):
            self.classify(self.commit())

    def test_diverged_history(self):
        self.path.write_text(PINS.replace(A, B))
        left = self.commit()
        self.git('checkout', '-q', '--detach', self.first)
        self.path.write_text(PINS.replace(A, C))
        right = self.commit()
        with self.assertRaises(p.Review):
            p.classify_range(self.root, left, right, p.policy())

    def write(self, name, text='x\n'):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_doc_commits_do_not_block_a_pin_bump(self):
        # .ai/, docs/ and top-level *.md are read by no build (policy
        # non_build_paths), so they may sit between the lock and the candidate.
        for name in ('docs/source-pinning.md', '.ai/notes.md', 'README.md', 'CLAUDE.md'):
            self.write(name)
            self.commit()
        self.path.write_text(PINS.replace(A, B, 1))
        self.assertEqual(self.classify(self.commit())['DSREV'], {'old': A, 'new': B})

    def test_docs_only_range_has_no_changes(self):
        self.write('docs/x.md')
        self.assertEqual(self.classify(self.commit()), {})

    def test_build_paths_that_look_like_docs_still_block(self):
        # A recipe can ship a file from its own directory, a nested README
        # included. Anything outside .ai/, docs/ and top-level *.md counts.
        for name in ('recipes-ds-app/x/files/notes.md', 'recipes-ds-app/x/README.md',
                     'docs.bb', 'conf/docs/x.inc', 'classes/x.bbclass'):
            with self.subTest(name=name):
                self.git('checkout', '-q', '--detach', self.first)
                self.write(name)
                self.commit()
                self.path.write_text(PINS.replace(A, B, 1))
                with self.assertRaises(p.Ineligible):
                    self.classify(self.commit())

    def test_rename_out_of_docs_into_the_build_blocks(self):
        self.write('docs/x.bb', 'do_install() { evil; }\n')
        self.commit()
        (self.root / 'recipes-ds-app/x').mkdir(parents=True)
        self.git('mv', 'docs/x.bb', 'recipes-ds-app/x/x.bb')
        self.path.write_text(PINS.replace(A, B, 1))
        with self.assertRaises(p.Ineligible):
            self.classify(self.commit())

    def test_non_build_paths_match_only_what_the_policy_names(self):
        config = p.policy()
        for name, expected in [('.ai/x.md', True), ('docs/a/b.png', True), ('README.md', True),
                               ('recipes-x/y/README.md', False), ('docsfoo/x.md', False),
                               ('conf/distro/include/ds-src-pins.inc', False), ('x.bb', False)]:
            with self.subTest(name=name):
                self.assertEqual(p.non_build(name, config), expected)

    def test_merge_second_parent_cannot_hide_functional_edit(self):
        self.git('checkout', '-qb', 'feature')
        self.path.write_text(PINS + 'PV = "2"\n')
        self.commit()
        self.path.write_text(PINS.replace(A, B))
        feature = self.commit()
        self.git('checkout', '-q', '--detach', self.first)
        self.git('merge', '--no-ff', '-qm', 'merge', feature)
        with self.assertRaises(p.Ineligible):
            self.classify(self.git('rev-parse', 'HEAD'))


def pr_fixture():
    data = dict(branch='pin-bot/vertex/1.x.x/10', line='vertex/1.x.x', source_line='vertex/1.x.x', head=A,
                base=B, merge=C, candidate=D, old=A, source_pr=10, changes={'DSREV': {'old': A, 'new': B}})
    pr = dict(number=20, user={'login': 'fixture[bot]'}, head={'sha': A, 'ref': data['branch'],
              'repo': {'full_name': 'owner/distro'}}, base={'sha': B, 'ref': data['line']},
              state='open', draft=False, mergeable=True, merge_commit_sha=C, assignees=[])
    return pr, data


class FakeAPI:
    repo, meta, app_id, login = 'owner/distro', 'owner/meta-dsensos', 100, 'fixture[bot]'

    def __init__(self):
        self.pull, self.data = pr_fixture()
        self.blocked, self.calls = [], []
        self.host = [dict(name='pr-checks', app={'id': 15368}, id=1, conclusion='success')]
        self.switch = 'merge'
        self.refs = {self.data['line']: B, self.data['branch']: A}

    def branch_sha(self, branch):
        return self.refs[branch]

    def ensure_review_anchor(self):
        return D

    def pr(self, n, repo=None):
        return copy.deepcopy(self.pull)

    def record(self, pr):
        return {'id': 1}, copy.deepcopy(self.data)

    def flagged(self, *args, **kwargs):
        return False

    def items(self, path, key=None):
        if '/commits?' in path:
            return [{'sha': A, 'parents': [{'sha': B}]}]
        if '/files?' in path:
            return [{'filename': p.LOCKFILE}]
        raise AssertionError(path)

    def checks(self, sha):
        assert sha == A  # GitHub attaches PR check-runs to head, not merge SHA.
        return self.host

    def update(self, *args):
        self.calls.append(('update', args))

    def block(self, pr, reason):
        self.blocked.append(reason)

    def call(self, path, method='GET', data=None, **kw):
        self.calls.append((path, method, data))
        if 'variables/' in path:
            return {'value': self.switch}
        if path.endswith('/merge'):
            return {'merged': True}
        raise AssertionError(path)


class ControllerTests(unittest.TestCase):
    def test_live_refs_reject_moves_hidden_by_pr_snapshots(self):
        for branch in ('line', 'branch'):
            api = FakeAPI()
            api.refs[api.data[branch]] = D
            with self.assertRaises(p.Review):
                b.provenance(api.pull, api.data, api)

    def test_regenerated_merge_accepts_only_identical_tree_and_parents(self):
        api = FakeAPI()
        first = {'tree': {'sha': 'e' * 40}, 'parents': [{'sha': B}, {'sha': A}]}
        with patch.object(api, 'call', side_effect=[first, copy.deepcopy(first)]):
            b.verify_merge(api, api.data, D)
        self.assertEqual(api.data['merge'], C)
        self.assertEqual(api.data['verified_merge'], D)
        for changed in [dict(first, tree={'sha': 'f' * 40}),
                        dict(first, parents=[{'sha': D}, {'sha': A}])]:
            with patch.object(api, 'call', side_effect=[first, changed]), self.assertRaises(p.Review):
                b.verify_merge(api, api.data, D)
        with self.assertRaises(p.Review):
            b.verify_merge(api, api.data, None)

    def test_provenance_rejects_head_base_route_owner_and_copilot_changes(self):
        api = FakeAPI()
        for mutate in [lambda pr: pr['head'].update(sha=D), lambda pr: pr['base'].update(sha=D),
                       lambda pr: pr['base'].update(ref='vertex/main'),
                       lambda pr: pr['user'].update(login='person'),
                       lambda pr: pr.update(assignees=[{'login': 'copilot-swe-agent'}]),
                       lambda pr: pr.update(draft=True)]:
            pr = copy.deepcopy(api.pull)
            mutate(pr)
            with self.assertRaises(p.Review):
                b.provenance(pr, api.data, api)

    def finish(self, api, mode='merge', ok=True, sha=C):
        with patch.object(b, 'clone', return_value=Path('/unused')), \
             patch.object(b, 'validate_source', return_value=api.data), \
             patch.object(p, 'show', side_effect=[LOCK, p.replace_lock(LOCK, A, D)]):
            b.finish(api, 20, mode, ok, sha)

    def test_valid_merge(self):
        api = FakeAPI()
        self.finish(api)
        merges = [c for c in api.calls if c[0].endswith('/merge')]
        self.assertEqual(merges[0][2], {'sha': A, 'merge_method': 'merge'})

    def test_no_merge_on_failed_missing_stale_or_wrong_app_check(self):
        for host in [[], [dict(id=1, name='pr-checks', app={'id': 15368}, conclusion='failure')],
                     [dict(id=1, name='pr-checks', app={'id': 999}, conclusion='success')],
                     [dict(id=1, name='pr-checks', app={'id': 15368}, conclusion='cancelled')]]:
            api = FakeAPI()
            api.host = host
            with self.assertRaises(p.Review):
                self.finish(api)
            self.assertTrue(api.blocked)
            self.assertFalse(any(c[0].endswith('/merge') for c in api.calls))

    def test_build_failure_wrong_sha_and_conflict_block(self):
        for ok, sha, mergeable in [(False, C, True), (True, D, True), (True, C, False), (True, C, None)]:
            api = FakeAPI()
            api.pull['mergeable'] = mergeable
            with self.assertRaises(p.Review):
                self.finish(api, ok=ok, sha=sha)
            self.assertTrue(api.blocked)

    def test_unverifiable_or_ineligible_source_in_finish_blocks_the_pr(self):
        # Inside finish a bot PR already exists, so these must latch a block
        # rather than end the run with the PR unmarked.
        for error in (b.release_git.Error('base.yml: expected one meta-dsensos: stanza'),
                      p.Ineligible('no ds-tegra-distro line vertex/9.x.x')):
            api = FakeAPI()
            with self.subTest(error=error), \
                 patch.object(b, 'clone', return_value=Path('/unused')), \
                 patch.object(b, 'validate_source', side_effect=error):
                with self.assertRaises(type(error)):
                    b.finish(api, 20, 'merge', True, C)
                self.assertTrue(api.blocked)
                self.assertFalse(any(c[0].endswith('/merge') for c in api.calls))

    def test_kill_switch_rechecked_after_build(self):
        api = FakeAPI()
        api.switch = 'disabled'
        with self.assertRaises(p.Review):
            self.finish(api)
        self.assertFalse(any(c[0].endswith('/merge') for c in api.calls))

    def test_create_mode_never_merges(self):
        api = FakeAPI()
        self.finish(api, mode='create')
        self.assertFalse(any(c[0].endswith('/merge') for c in api.calls))

    def test_persistent_block_survives_repair(self):
        api = FakeAPI()
        api.record = lambda pr: (_ for _ in ()).throw(p.Review('persistent human-review block'))
        with self.assertRaises(p.Review):
            self.finish(api)
        self.assertFalse(any(c[0].endswith('/merge') for c in api.calls))


class ArtifactTests(unittest.TestCase):
    def test_four_builds_and_exact_meta_pin(self):
        evidence = {'builds': [dict(flavor=str(i), s3_prefix=str(i)) for i in range(4)]}
        a.verify(evidence, A, lambda _: {'commit_hashes': [{'name': 'meta-dsensos', 'commit': A}]})
        for actual in [B, None]:
            with self.assertRaises(ValueError):
                a.verify(evidence, A, lambda _: {'commit_hashes': [{'name': 'meta-dsensos', 'commit': actual}]})
        with self.assertRaises(ValueError):
            a.verify({'builds': evidence['builds'][:3]}, A, lambda _: {})


class LifecycleAPI(FakeAPI):
    def __init__(self, existing=False, closed=False):
        super().__init__()
        self.existing, self.closed = existing, closed
        self.data['old'] = A
        self.source = dict(number=10, merged_at='2026-11-04T12:00:00Z', base={'ref': self.data['line']})

    def pr(self, n, repo=None):
        return self.source if repo == self.meta else copy.deepcopy(self.pull)

    def items(self, path, key=None):
        if 'pulls?state=all' in path:
            if self.existing:
                return [dict(number=20, state='closed' if self.closed else 'open')]
            return []
        if 'pulls?state=closed' in path:
            return [self.source, self.source]  # duplicated recovery event
        return super().items(path, key)

    def call(self, path, method='GET', data=None, **kw):
        self.calls.append((path, method, data))
        if path.endswith('/git/blobs'):
            return {'sha': C}
        if '/git/commits/' in path:
            return {'tree': {'sha': C}}
        if path.endswith('/git/trees'):
            assert data['base_tree'] == C
            return {'sha': C}
        if path.endswith('/git/commits'):
            return {'sha': A}
        if path.endswith('/check-runs'):
            return {'id': 1}
        if path.endswith('/git/refs'):
            return {}
        if path.endswith('/pulls'):
            self.existing = True
            return copy.deepcopy(self.pull)
        raise AssertionError(path)


class LifecycleTests(unittest.TestCase):
    def prepare(self, api, mode='create', source=True):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(b, 'clone', return_value=Path('/unused')), \
             patch.object(b, 'validate_source', return_value=copy.deepcopy(api.data)), \
             patch.object(p, 'show', side_effect=lambda root, ref, file: LOCK if file == p.LOCKFILE else '1.0.1\n'):
            output = Path(tmp) / 'outputs'
            b.prepare(api, mode, 10 if source else None, str(output))
            return output.read_text() if output.exists() else ''

    def test_create_then_reuse_same_pr(self):
        api = LifecycleAPI()
        self.assertIn('pr=20', self.prepare(api))
        self.assertIn('pr=20', self.prepare(api))
        self.assertEqual(sum(c[0].endswith('/pulls') for c in api.calls), 1)

    def test_audit_and_disabled_make_no_mutations(self):
        for mode in ('audit', 'disabled'):
            api = LifecycleAPI()
            self.assertEqual(self.prepare(api, mode), '')
            self.assertFalse(api.calls)

    def test_closed_pr_never_reopens(self):
        api = LifecycleAPI(existing=True, closed=True)
        self.assertEqual(self.prepare(api), '')
        self.assertFalse(api.calls)

    def test_recovery_scan_deduplicates(self):
        api = LifecycleAPI()
        self.assertIn('pr=20', self.prepare(api, source=False))
        self.assertEqual(sum(c[0].endswith('/pulls') for c in api.calls), 1)

    def test_unknown_mergeability_waits_without_builds(self):
        api = LifecycleAPI(existing=True)
        api.pull['mergeable'] = None
        with patch.object(b.time, 'sleep') as sleep:
            self.assertEqual(self.prepare(api), '')
        self.assertEqual(sleep.call_count, b.MERGEABLE_POLLS)
        self.assertFalse(api.blocked)

    def test_mergeability_resolved_within_poll_builds_in_same_run(self):
        api = LifecycleAPI(existing=True)
        answers = iter([None, None])
        base_pr = api.pr

        def pr(n, repo=None):
            result = base_pr(n, repo)
            if repo != api.meta and result.get('number') == 20:
                result['mergeable'] = next(answers, True)
            return result
        api.pr = pr
        with patch.object(b.time, 'sleep') as sleep:
            self.assertIn('pr=20', self.prepare(api))
        self.assertGreaterEqual(sleep.call_count, 1)
        self.assertLess(sleep.call_count, b.MERGEABLE_POLLS)
        self.assertFalse(api.blocked)

    def test_ineligible_never_creates_pr(self):
        api = LifecycleAPI()
        with patch.object(b, 'clone', return_value=Path('/unused')), \
             patch.object(b, 'validate_source', side_effect=p.Ineligible('functional recipe change')):
            b.prepare(api, 'create', 10, None)
        self.assertFalse(api.calls)

    def test_malformed_line_files_stop_the_scan_without_ledgering(self):
        # A bad lockfile or base.yml is a property of the destination line, so
        # every source on it would hit the same error. Recording each one in the
        # permanent source ledger would strand them after the file is fixed;
        # the scan stops loudly instead and nothing is written.
        api = LifecycleAPI()
        flagged = []
        api.flag = lambda *args, **kwargs: flagged.append(args)
        with patch.object(b, 'clone', return_value=Path('/unused')), \
             patch.object(b, 'validate_source', side_effect=b.release_git.Error('base.lock.yml: bad indentation')):
            with self.assertRaises(b.release_git.Error):
                b.prepare(api, 'create', None, None)
        self.assertEqual(flagged, [])
        self.assertFalse(api.calls)


class SourceLineTests(unittest.TestCase):
    """validate_source over real temporary distro and meta-dsensos histories."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.meta, self.distro = root / 'meta', root / 'distro'
        for repo in (self.meta, self.distro):
            repo.mkdir()
            self.git(repo, 'init', '-q')
            self.git(repo, 'config', 'user.email', 'fixture@example.com')
            self.git(repo, 'config', 'user.name', 'Fixture')
        self.pins = self.meta / p.policy()['pin_file']
        self.pins.parent.mkdir(parents=True)
        self.pins.write_text(PINS)
        self.first = self.commit(self.meta)
        # Layer vertex/1.3.x moves one way, layer vertex/1.x.x another.
        self.pins.write_text(PINS.replace(A, B, 1))
        self.child = self.commit(self.meta)
        self.git(self.meta, 'checkout', '-q', '--detach', self.first)
        self.pins.write_text(PINS.replace(A, C, 1))
        self.candidate = self.commit(self.meta)
        self.git(self.meta, 'update-ref', 'refs/remotes/origin/vertex/1.x.x', self.candidate)
        self.api = FakeAPI()

    @staticmethod
    def git(repo, *args):
        return p.git(repo, *args)

    def commit(self, repo):
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-qm', 'fixture')
        return self.git(repo, 'rev-parse', 'HEAD')

    def distro_line(self, line, lock_commit):
        kas = self.distro / 'kas/include'
        kas.mkdir(parents=True, exist_ok=True)
        (kas / 'base.yml').write_text(f'repos:\n  meta-dsensos:\n    branch: {line}\n')
        (kas / 'base.lock.yml').write_text(LOCK.replace(f'meta-dsensos:\n      commit: {A}',
                                                        f'meta-dsensos:\n      commit: {lock_commit}'))
        self.git(self.distro, 'update-ref', 'refs/remotes/origin/' + line, self.commit(self.distro))

    def validate(self, line):
        source = dict(number=10, merged_at='2026-11-07T00:00:00Z', merge_commit_sha=self.candidate,
                      base={'ref': line, 'repo': {'full_name': self.api.meta}},
                      head={'repo': {'full_name': self.api.meta}})
        return b.validate_source(self.api, source, self.distro, self.meta, p.policy())

    def test_missing_destination_line_is_ineligible_without_api_calls(self):
        self.distro_line('vertex/1.x.x', self.first)
        with self.assertRaisesRegex(p.Ineligible, 'no ds-tegra-distro line vertex/1.0.x'):
            self.validate('vertex/1.0.x')
        self.assertFalse(self.api.calls)

    def test_source_already_in_lock_is_skipped(self):
        self.distro_line('vertex/1.x.x', self.candidate)
        self.assertIsNone(self.validate('vertex/1.x.x'))

    def test_lock_on_other_line_history_is_a_review_case(self):
        # The window after a merge-up conflict PR carried the child pin: the
        # parent's lock is child history, and a parent-line source descends
        # from neither. Today that is Review (recorded in the source ledger),
        # not Ineligible. This pins the behaviour so a change to it is deliberate.
        self.distro_line('vertex/1.x.x', self.child)
        with self.assertRaises(p.Review):
            self.validate('vertex/1.x.x')
        self.assertFalse(self.api.calls)


class CloneTests(unittest.TestCase):
    """clone() retries transient failures and keeps git's error text."""

    def run_clone(self, results):
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return subprocess.CompletedProcess(args, result[0], '', result[1])
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(b.subprocess, 'run', side_effect=fake_run), \
             patch.object(b.time, 'sleep') as sleep, \
             patch.dict(os.environ, {'SOURCE_GH_TOKEN': 'ghs_secret'}), \
             contextlib.redirect_stderr(err):
            target = Path(tmp) / 'meta'
            try:
                result = b.clone('owner/meta-dsensos', target)
            except p.Review as e:
                result = e
        return result, calls, sleep.call_count, err.getvalue()

    def test_transient_failure_then_success(self):
        result, calls, sleeps, err = self.run_clone([(128, 'fatal: unable to access: 502'), (0, '')])
        self.assertIsInstance(result, Path)
        self.assertEqual((len(calls), sleeps), (2, 1))
        self.assertIn('attempt 1/3 failed: fatal: unable to access: 502', err)

    def test_timeout_then_success(self):
        timeout = subprocess.TimeoutExpired(['git'], b.CLONE_TIMEOUT)
        result, calls, sleeps, err = self.run_clone([timeout, (0, '')])
        self.assertIsInstance(result, Path)
        self.assertEqual(len(calls), 2)
        self.assertIn('timed out', err)

    def test_persistent_failure_is_review_with_git_message(self):
        result, calls, sleeps, err = self.run_clone([(128, 'fatal: repository not found')] * b.CLONE_ATTEMPTS)
        self.assertIsInstance(result, p.Review)
        self.assertIn('fatal: repository not found', str(result))
        self.assertEqual((len(calls), sleeps), (b.CLONE_ATTEMPTS, b.CLONE_ATTEMPTS - 1))

    def test_token_only_in_environment(self):
        _, calls, _, err = self.run_clone([(0, '')])
        args, kwargs = calls[0]
        self.assertNotIn('ghs_secret', ' '.join(args))
        self.assertEqual(kwargs['env']['GH_TOKEN'], 'ghs_secret')
        self.assertEqual(kwargs['timeout'], b.CLONE_TIMEOUT)
        self.assertNotIn('ghs_secret', err)


class PermissionsTests(unittest.TestCase):
    def test_pr_marker_needs_only_checks_write_and_survives_retry(self):
        api = b.API('owner/distro', '100', 'fixture')
        marker = dict(name=b.REVIEW_CHECK + '/10', app={'id': 100}, external_id='pr-review/10', conclusion='failure')
        with patch.object(api, 'review_anchor', return_value=D), \
             patch.object(api, 'checks', side_effect=[[], [marker], [marker]]), \
             patch.object(api, 'call', return_value={'id': 1}) as call:
            api.pr_review(10, 'external edit')
            self.assertTrue(api.pr_review_blocked(10))
            api.pr_review(10, 'retry after repair')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.args[:2], ('repos/owner/distro/check-runs', 'POST'))
        self.assertEqual(call.call_args.args[2]['head_sha'], D)

    def test_source_review_persists_only_in_destination_and_survives_retry(self):
        api = b.API('owner/distro', '100', 'fixture')
        source = 'owner/distro'  # Aliased fixture repo still uses the source ledger.
        ledger = {'schema': 1, 'blocked': {source + '#10': {'reason': 'unverifiable source'}}}
        refs = [{'ref': 'refs/heads/' + api.ledger_branch, 'object': {'sha': D}}]
        content = {'content': base64.b64encode(json.dumps(ledger).encode()).decode()}
        responses = [[], {'sha': C}, {'sha': D}, {}, refs, content, refs, content]
        with patch.object(api, 'call', side_effect=responses) as call:
            api.flag(source, 10, 'unverifiable source', source=True)
            self.assertTrue(api.flagged(source, 10, source=True))
            api.flag(source, 10, 'later retry still requires review', source=True)
        for request in call.call_args_list:
            self.assertTrue(request.args[0].startswith('repos/owner/distro/'))
        self.assertEqual(sum(len(r.args) > 1 and r.args[1] in ('POST', 'PATCH')
                             for r in call.call_args_list), 3)

    def test_token_request_uses_bearer_and_explicit_permissions(self):
        response = io.BytesIO(json.dumps({'token': 'fixture-token'}).encode())
        with patch.object(pin_token.urllib.request, 'urlopen', return_value=response) as send:
            self.assertEqual(pin_token.request_token('fixture-jwt', 42, 'distro'), {'token': 'fixture-token'})
        request = send.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-jwt')
        self.assertEqual(request.full_url, 'https://api.github.com/app/installations/42/access_tokens')
        self.assertEqual(json.loads(request.data), {'repositories': ['distro'], 'permissions': pin_token.PERMISSIONS})

    def test_finish_token_uses_explicit_scope_and_removes_private_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'output'
            result = dict(token='masked-fixture-token', permissions=pin_token.PERMISSIONS,
                          repositories=[{'name': 'distro'}])
            def sign(args, input):
                key = Path(args[-1])
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
                self.assertEqual(key.read_text(), 'fixture-private-key')
                self.key = key
                return b'fixture-signature'
            env = dict(APP_CLIENT_ID='fixture-client', APP_PRIVATE_KEY='fixture-private-key',
                       APP_INSTALLATION_ID='1', GITHUB_REPOSITORY='owner/distro', GITHUB_OUTPUT=str(output))
            with patch.dict(os.environ, env), patch.object(subprocess, 'check_output', side_effect=sign), \
                 patch.object(pin_token, 'request_token', return_value=result) as request, \
                 contextlib.redirect_stdout(io.StringIO()) as printed:
                pin_token.mint()
            self.assertEqual(request.call_args.args[1:], (1, 'distro'))
            self.assertFalse(self.key.exists())
            self.assertNotIn('fixture-private-key', printed.getvalue())
            self.assertEqual(output.read_text(), 'token=masked-fixture-token\n')

    def test_rules_preserve_old_bypass_and_enforce_new_app_checks(self):
        current = json.loads((p.ROOT / 'release/admin/ruleset-release-lines.json').read_text())
        current['enforcement'] = 'active'
        current['bypass_actors'][0]['actor_id'] = 123
        rules = pin_rules.generate(current, 456)
        self.assertEqual(rules['safety']['bypass_actors'], current['bypass_actors'])
        self.assertEqual(rules['existing-release-lines']['bypass_actors'], current['bypass_actors'])
        self.assertNotIn('pull_request', [r['type'] for r in rules['existing-release-lines']['rules']])
        self.assertEqual(rules['approval']['bypass_actors'][-1]['bypass_mode'], 'pull_request')
        required = rules['safety']['rules'][0]['parameters']
        self.assertTrue(required['strict_required_status_checks_policy'])
        self.assertIn({'context': b.CHECK, 'integration_id': 456}, required['required_status_checks'])
        self.assertEqual(pin_token.PERMISSIONS, {
            'contents': 'write', 'pull_requests': 'write', 'checks': 'write', 'actions_variables': 'read'})
        with self.assertRaises(ValueError):
            pin_rules.generate(current, 123)

    def test_record_rejects_block_even_after_branch_repair(self):
        api = b.API('owner/distro', '100', 'fixture')
        pr, data = pr_fixture()
        check = dict(id=1, name=b.CHECK, app={'id': 100},
                     external_id='pin-propagation/v1/10',
                     output={'title': b.BLOCKED, 'text': json.dumps(data)})
        with patch.object(api, 'checks', return_value=[check]), self.assertRaises(p.Review):
            api.record(pr)


if __name__ == '__main__':
    unittest.main()
