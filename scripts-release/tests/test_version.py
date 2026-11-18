"""Tests for scripts-release/version.py: check, rc, dev, bump, matrix, products.

A fake repo root is built in a temp dir for every test: release/config.json,
release/matrix.json, versions/, the product fragments and the committed
flavors. The git tests run git init in a temp dir; nothing touches the
network or this checkout.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import version  # noqa: E402

FLAVORS = {
    'vertex': ['vertex_jetson-orin-nx-a603-8gb_release', 'vertex_jetson-orin-nx-a603-8gb_release-hardened'],
    'aggregator': ['aggregator_jetson-orin-nx-a603-8gb_release'],
}


def make_root(d, products=('vertex', 'aggregator'), versions=None, matrix=None, fragments=None,
              committed=None):
    """Write a minimal repo root. versions, matrix, fragments and committed default to a consistent set."""
    versions = {p: '0.0.1' for p in products} if versions is None else versions
    matrix = {p: FLAVORS[p] for p in products} if matrix is None else matrix
    fragments = list(products) if fragments is None else fragments
    committed = sum(FLAVORS.values(), []) if committed is None else committed
    os.makedirs(os.path.join(d, 'release'))
    os.makedirs(os.path.join(d, 'versions'))
    os.makedirs(os.path.join(d, 'kas/include/product'))
    with open(os.path.join(d, 'release/config.json'), 'w') as f:
        json.dump({'products': list(products)}, f)
    with open(os.path.join(d, 'release/matrix.json'), 'w') as f:
        json.dump({'products': {p: {'flavors': fl} for p, fl in matrix.items()}}, f)
    for p, v in versions.items():
        with open(os.path.join(d, 'versions', p), 'w') as f:
            f.write(v if v.endswith('\n') or v == '' else v + '\n')
    with open(os.path.join(d, 'versions/README.md'), 'w') as f:
        f.write('# versions\n')
    for p in fragments:
        with open(os.path.join(d, 'kas/include/product', p + '.yml'), 'w') as f:
            f.write('header:\n  version: 23\n')
    for fl in committed:
        with open(os.path.join(d, 'kas', fl + '.yml'), 'w') as f:
            f.write('header:\n  version: 23\n')
    # overlays that must not count as flavors
    for n in ('local', 'ci-override', 'ci-strict', 'ci-version'):
        with open(os.path.join(d, 'kas', n + '.yml'), 'w') as f:
            f.write('header:\n  version: 23\n')
    return d


def quiet_main(*args):
    """version.main() with its output swallowed (the exit code is what the test checks)."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return version.main(list(args))


def git(d, *args):
    return subprocess.run(['git', '-C', d, *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(d, commits):
    git(d, 'init', '-q', '-b', 'vertex/main')
    git(d, 'config', 'user.email', 'test@example.com')
    git(d, 'config', 'user.name', 'test')
    git(d, 'config', 'commit.gpgsign', 'false')
    for i in range(commits):
        with open(os.path.join(d, 'tracked.txt'), 'a') as f:
            f.write(f'{i}\n')
        git(d, 'add', 'tracked.txt')
        git(d, 'commit', '-q', '-m', f'commit {i}')
    return git(d, 'rev-parse', 'HEAD')


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_consistent_root_passes(self):
        make_root(self.root)
        self.assertEqual(version.check(self.root), [])
        self.assertEqual(quiet_main('check', self.root), 0)

    def test_readme_is_ignored(self):
        make_root(self.root)
        self.assertEqual(version.check(self.root), [])

    def test_missing_versions_file(self):
        make_root(self.root, versions={'vertex': '0.0.1'})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('versions/aggregator: missing', errors[0])
        self.assertEqual(quiet_main('check', self.root), 1)

    def test_v_prefix_rejected(self):
        make_root(self.root, versions={'vertex': 'v1.0.0', 'aggregator': '0.0.1'})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('versions/vertex', errors[0])
        self.assertIn("'v1.0.0'", errors[0])

    def test_build_metadata_rejected(self):
        make_root(self.root, versions={'vertex': '1.0.0+x', 'aggregator': '0.0.1'})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('versions/vertex', errors[0])

    def test_prerelease_suffix_and_leading_zero_rejected(self):
        for bad in ('1.0.0-rc.1', '01.0.0', '1.0', '', '1.0.0\n1.0.1'):
            with self.subTest(bad=bad):
                tmp = tempfile.TemporaryDirectory()
                make_root(tmp.name, versions={'vertex': bad, 'aggregator': '0.0.1'})
                errors = version.check(tmp.name)
                self.assertTrue(any('versions/vertex' in e for e in errors), (bad, errors))
                tmp.cleanup()

    def test_extra_versions_file_rejected(self):
        make_root(self.root, versions={'vertex': '0.0.1', 'aggregator': '0.0.1', 'stray': '0.0.1'})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('versions/stray', errors[0])

    def test_product_without_fragment(self):
        make_root(self.root, fragments=['vertex'])
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('release/config.json', errors[0])
        self.assertIn('kas/include/product', errors[0])

    def test_fragment_without_product(self):
        make_root(self.root, fragments=['vertex', 'aggregator', 'ghost'])
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('ghost', errors[0])

    def test_matrix_flavor_not_committed(self):
        matrix = {'vertex': FLAVORS['vertex'] + ['vertex_jetson-orin-nx-jackson_release'],
                  'aggregator': FLAVORS['aggregator']}
        make_root(self.root, matrix=matrix)
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('vertex_jetson-orin-nx-jackson_release has no kas/', errors[0])

    def test_matrix_flavor_under_wrong_product(self):
        matrix = {'vertex': FLAVORS['vertex'],
                  'aggregator': FLAVORS['aggregator'] + ['vertex_jetson-orin-nx-a603-8gb_release']}
        make_root(self.root, matrix=matrix)
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('listed under product aggregator', errors[0])

    def test_matrix_product_set_differs(self):
        make_root(self.root, matrix={'vertex': FLAVORS['vertex']})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('release/matrix.json', errors[0])

    def test_overlays_are_not_committed_flavors(self):
        make_root(self.root)
        for n in ('local', 'ci-override', 'ci-strict', 'ci-version'):
            self.assertNotIn(n, version.committed_flavors(self.root))

    def test_missing_config(self):
        os.makedirs(os.path.join(self.root, 'release'))
        self.assertEqual(version.check(self.root), ['release/config.json: missing'])

    def test_bad_product_name(self):
        make_root(self.root, products=('Vertex', 'aggregator'), versions={'Vertex': '0.0.1', 'aggregator': '0.0.1'},
                  matrix={'Vertex': [], 'aggregator': []})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("'Vertex'", errors[0])


class StringTests(unittest.TestCase):
    def test_rc_and_dev_format(self):
        sha = '0123456789abcdef0123456789abcdef01234567'
        self.assertEqual(version.rc_version('1.0.0', 42, sha), '1.0.0-rc.42.github-0123456')
        self.assertEqual(version.dev_version('1.0.0', 42, sha, False), '1.0.0-dev.42.g0123456')
        self.assertEqual(version.dev_version('1.0.0', 42, sha, True), '1.0.0-dev.42.g0123456.dirty')

    def test_strings_are_lowercase_without_plus(self):
        for s in ('1.0.0-rc.42.github-0123456', '1.0.0-dev.42.g0123456.dirty', '1.0.0'):
            self.assertEqual(version.checked_string(s), s)
        for s in ('1.0.0+x', '1.0.0-RC.1', 'v1.0.0', '1.0.0-rc.1.github-ABCDEF0'):
            with self.assertRaises(version.CheckError):
                version.checked_string(s)

    def test_bump(self):
        self.assertEqual(version.bump('1.2.3', 'major'), '2.0.0')
        self.assertEqual(version.bump('1.2.3', 'minor'), '1.3.0')
        self.assertEqual(version.bump('1.2.3', 'patch'), '1.2.4')
        self.assertEqual(version.bump('0.0.1', 'major'), '1.0.0')
        with self.assertRaises(ValueError):
            version.bump('1.2.3', 'release')


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        self.head = make_repo(self.root, 3)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, version.__file__, '--root', self.root, *args],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def test_rc_counts_commits_of_the_sha(self):
        rc, out, err = self.run_cli('rc', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f'0.0.1-rc.3.github-{self.head[:7]}')
        parent = git(self.root, 'rev-parse', 'HEAD~1')
        rc, out, err = self.run_cli('rc', 'vertex', '--sha', parent)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f'0.0.1-rc.2.github-{parent[:7]}')

    def test_rc_unknown_product_or_sha(self):
        rc, out, err = self.run_cli('rc', 'ghost')
        self.assertEqual(rc, 1)
        self.assertIn("no product 'ghost'", err)
        rc, out, err = self.run_cli('rc', 'vertex', '--sha', 'nosuchref')
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')

    def test_rc_with_separate_git_dir(self):
        other = tempfile.TemporaryDirectory()
        head = make_repo(other.name, 5)
        rc, out, err = self.run_cli('rc', 'aggregator', '--git-dir', other.name)
        other.cleanup()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f'0.0.1-rc.5.github-{head[:7]}')

    def test_dev_clean_and_dirty(self):
        rc, out, err = self.run_cli('dev', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f'0.0.1-dev.3.g{self.head[:7]}')
        with open(os.path.join(self.root, 'tracked.txt'), 'a') as f:
            f.write('dirty\n')
        rc, out, err = self.run_cli('dev', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, f'0.0.1-dev.3.g{self.head[:7]}.dirty')

    def test_dev_ignores_untracked_files(self):
        with open(os.path.join(self.root, 'untracked.txt'), 'w') as f:
            f.write('x\n')
        rc, out, err = self.run_cli('dev', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertFalse(out.endswith('.dirty'), out)

    def test_bump_rewrites_file(self):
        rc, out, err = self.run_cli('bump', 'vertex', 'minor')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, '0.1.0')
        with open(os.path.join(self.root, 'versions/vertex')) as f:
            self.assertEqual(f.read(), '0.1.0\n')
        with open(os.path.join(self.root, 'versions/aggregator')) as f:
            self.assertEqual(f.read(), '0.0.1\n')
        rc, out, err = self.run_cli('bump', 'vertex', 'release')
        self.assertEqual(rc, 2)

    def test_matrix_and_products(self):
        rc, out, err = self.run_cli('matrix', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), FLAVORS['vertex'])
        rc, out, err = self.run_cli('matrix-include', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        self.assertNotIn('\n', out.strip())
        rc, out, err = self.run_cli('matrix-include', 'ghost')
        self.assertEqual(rc, 1)
        rc, out, err = self.run_cli('matrix', 'ghost')
        self.assertEqual(rc, 1)
        rc, out, err = self.run_cli('products')
        self.assertEqual(out.splitlines(), ['vertex', 'aggregator'])
        rc, out, err = self.run_cli('products', '--json')
        self.assertEqual(json.loads(out), ['vertex', 'aggregator'])

    def test_sdk_list(self):
        # ENG-2126: the optional "sdk" list marks the matrix entries that build the SDK,
        # and only matrix-include --sdk (the promote build) applies it; plain
        # matrix-include (the RC build) never carries the key.
        def write(sdk):
            doc = {'products': {'vertex': {'flavors': FLAVORS['vertex'], 'sdk': sdk},
                                'aggregator': {'flavors': FLAVORS['aggregator']}}}
            with open(os.path.join(self.root, 'release/matrix.json'), 'w') as f:
                json.dump(doc, f)
        write([FLAVORS['vertex'][0]])
        self.assertEqual(version.check(self.root), [])
        self.assertEqual(version.sdk_flavors(self.root), {'vertex': [FLAVORS['vertex'][0]], 'aggregator': []})
        self.assertEqual(version.matrix_include(self.root, 'vertex', sdk=True),
                         {'include': [{'flavor': FLAVORS['vertex'][0], 'sdk': True}, {'flavor': FLAVORS['vertex'][1]}]})
        self.assertEqual(version.matrix_include(self.root, 'aggregator', sdk=True),
                         {'include': [{'flavor': FLAVORS['aggregator'][0]}]})
        # the RC matrix: no sdk key, whatever the list says
        self.assertEqual(version.matrix_include(self.root, 'vertex'), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        self.assertEqual(version.matrix_include(self.root, 'vertex', sdk=False), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        rc, out, err = self.run_cli('matrix-include', '--sdk', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['include'][0], {'flavor': FLAVORS['vertex'][0], 'sdk': True})
        rc, out, err = self.run_cli('matrix-include', 'vertex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        # the matrix command itself is unchanged
        rc, out, err = self.run_cli('matrix', 'vertex')
        self.assertEqual(json.loads(out), FLAVORS['vertex'])
        write([])
        self.assertEqual(version.check(self.root), [])
        self.assertEqual(version.matrix_include(self.root, 'vertex', sdk=True), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        write(['vertex_jetson-orin-nx-jackson_release'])
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn('products.vertex.sdk lists vertex_jetson-orin-nx-jackson_release, which is not in products.vertex.flavors', errors[0])
        write([FLAVORS['vertex'][0], FLAVORS['vertex'][0]])
        self.assertIn('products.vertex.sdk has a duplicate', version.check(self.root)[0])
        write('not-a-list')
        self.assertIn('products.vertex.sdk must be a list', version.check(self.root)[0])
        # a bad list fails both shapes: the RC matrix validates it even though it never uses it
        rc, out, err = self.run_cli('matrix-include', '--sdk', 'vertex')
        self.assertEqual(rc, 1)
        rc, out, err = self.run_cli('matrix-include', 'vertex')
        self.assertEqual(rc, 1)

    def test_rc_refuses_bad_versions_file(self):
        with open(os.path.join(self.root, 'versions/vertex'), 'w') as f:
            f.write('v0.0.1\n')
        rc, out, err = self.run_cli('rc', 'vertex')
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('versions/vertex', err)


def write_config(root, doc):
    with open(os.path.join(root, 'release/config.json'), 'w') as f:
        json.dump(doc, f)


def merge_up_block(**changes):
    """A complete, valid merge_up block; changes are dotted paths -> value (DROP removes the key)."""
    block = {
        'finish': {'strict': False, 'required_checks': ['pr-checks']},
        'lock_pin': {'verify_finish': False, 'exact': 'push', 'extra': 'bot-pr', 'wait_minutes': 15},
        'never_auto': ['.github/workflows/**', '*sudoers*'],
        'meta_dsensos': {'never_auto': ['.github/workflows/**', '*.rules'], 'required_checks': []},
    }
    for path, value in changes.items():
        *parents, leaf = path.split('__')
        obj = block
        for part in parents:
            obj = obj[part]
        if value is DROP:
            del obj[leaf]
        else:
            obj[leaf] = value
    return block


DROP = object()


class ConfigTests(unittest.TestCase):
    """release/config.json as a whole: unknown keys and the merge_up block (ENG-2234)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def refused(self, doc, *needles):
        write_config(self.root, doc)
        with self.assertRaises(version.CheckError) as cm:
            version.config(self.root)
        for needle in needles:
            self.assertIn(needle, str(cm.exception))
        # the same violation fails `check` (lint, pr-checks) and every caller of products()
        self.assertEqual(version.check(self.root), [str(cm.exception)])
        with self.assertRaises(version.CheckError):
            version.products(self.root)

    def bad_block(self, needle, **changes):
        self.refused({'products': ['vertex', 'aggregator'], 'merge_up': merge_up_block(**changes)}, needle)

    def test_without_the_block_every_default_is_filled_in(self):
        cfg = version.config(self.root)
        self.assertEqual(cfg['products'], ['vertex', 'aggregator'])
        self.assertEqual(cfg['merge_up'], {
            'finish': {'strict': False, 'required_checks': []},
            # exact: bot-pr since 2026-11-18 (a PR as the record of every merge a rule makes); it was push
            'lock_pin': {'verify_finish': False, 'exact': 'bot-pr', 'extra': 'bot-pr', 'wait_minutes': 15},
            'never_auto': ['.github/workflows/**'],
            'meta_dsensos': {'never_auto': ['.github/workflows/**'], 'required_checks': []},
        })
        self.assertEqual(version.check(self.root), [])

    def test_defaults_are_copies(self):
        version.config(self.root)['merge_up']['never_auto'].append('x')
        version.config(self.root)['merge_up']['meta_dsensos']['never_auto'].append('x')
        self.assertEqual(version.MERGE_UP_DEFAULTS['never_auto'], ['.github/workflows/**'])
        self.assertEqual(version.MERGE_UP_DEFAULTS['meta_dsensos']['never_auto'], ['.github/workflows/**'])

    def test_a_complete_block_is_returned_as_written(self):
        block = merge_up_block(finish__strict=True, lock_pin__exact='bot-pr', lock_pin__extra='pr',
                               lock_pin__verify_finish=True, lock_pin__wait_minutes=10)
        write_config(self.root, {'products': ['vertex', 'aggregator'], 'merge_up': block})
        self.assertEqual(version.merge_up(self.root), block)
        self.assertEqual(version.check(self.root), [])

    def test_a_partial_block_keeps_the_other_defaults(self):
        write_config(self.root, {'products': ['vertex', 'aggregator'],
                                 'merge_up': {'finish': {'strict': True}, 'lock_pin': {'exact': 'push'}}})
        mu = version.merge_up(self.root)
        self.assertEqual(mu['finish'], {'strict': True, 'required_checks': []})
        # push is the value that is not the default, so this shows the key was read
        self.assertEqual(mu['lock_pin'], {'verify_finish': False, 'exact': 'push', 'extra': 'bot-pr', 'wait_minutes': 15})
        write_config(self.root, {'products': ['vertex', 'aggregator'], 'merge_up': {'lock_pin': {'extra': 'pr'}}})
        self.assertEqual(version.merge_up(self.root)['lock_pin'], {'verify_finish': False, 'exact': 'bot-pr', 'extra': 'pr', 'wait_minutes': 15})
        self.assertEqual(mu['never_auto'], ['.github/workflows/**'])

    def test_unknown_top_level_key(self):
        self.refused({'products': ['vertex', 'aggregator'], 'merge-up': {}},
                     'unknown key(s) merge-up', 'known: products, merge_up')
        self.refused(['vertex'], 'must be an object')

    def test_unknown_keys_inside_the_block(self):
        self.bad_block('unknown key(s) splice in "merge_up"', splice=True)
        self.bad_block('unknown key(s) stric in "merge_up.finish"', finish__stric=True)
        self.bad_block('unknown key(s) mode in "merge_up.lock_pin"', lock_pin__mode='push')
        self.bad_block('unknown key(s) finish in "merge_up.meta_dsensos"', meta_dsensos__finish={})

    def test_objects_must_be_objects(self):
        self.refused({'products': ['vertex', 'aggregator'], 'merge_up': []}, '"merge_up" must be an object')
        self.bad_block('"merge_up.finish" must be an object', finish=True)
        self.bad_block('"merge_up.lock_pin" must be an object', lock_pin='push')
        self.bad_block('"merge_up.meta_dsensos" must be an object', meta_dsensos=['*.rules'])

    def test_switches_must_be_json_booleans(self):
        for bad in ('true', 1, 0, None, 'false'):
            with self.subTest(bad=bad):
                self.bad_block('merge_up.finish.strict must be true or false', finish__strict=bad)
                self.bad_block('merge_up.lock_pin.verify_finish must be true or false', lock_pin__verify_finish=bad)

    def test_lock_pin_enums_are_closed(self):
        for bad in ('pr', 'Push', '', None, True, ['push']):
            with self.subTest(exact=bad):
                self.bad_block('merge_up.lock_pin.exact must be one of push, bot-pr', lock_pin__exact=bad)
        for bad in ('push', 'bot', '', None, False, ['pr']):
            with self.subTest(extra=bad):
                self.bad_block('merge_up.lock_pin.extra must be one of bot-pr, pr', lock_pin__extra=bad)

    def test_wait_minutes(self):
        # 51 and up: the App token that merges after the wait lives 60 minutes
        for bad in (0, -5, 51, 60, 360, 45.0, '45', True, None):
            with self.subTest(bad=bad):
                self.bad_block('merge_up.lock_pin.wait_minutes must be a whole number of minutes from 1 to 50', lock_pin__wait_minutes=bad)
        for good in (1, 45, 50):
            write_config(self.root, {'products': ['vertex', 'aggregator'],
                                     'merge_up': merge_up_block(lock_pin__wait_minutes=good)})
            self.assertEqual(version.merge_up(self.root)['lock_pin']['wait_minutes'], good)

    def test_check_name_lists(self):
        for where in ('finish', 'meta_dsensos'):
            for bad in ('pr-checks', [''], [' pr-checks'], [1], {'pr-checks': True}, None):
                with self.subTest(where=where, bad=bad):
                    self.bad_block(f'merge_up.{where}.required_checks must be a list of non-empty strings',
                                   **{f'{where}__required_checks': bad})
            self.bad_block(f'merge_up.{where}.required_checks has a duplicate',
                           **{f'{where}__required_checks': ['a', 'a']})

    def test_never_auto_lists(self):
        for where, key in (('merge_up', 'never_auto'), ('merge_up.meta_dsensos', 'meta_dsensos__never_auto')):
            with self.subTest(where=where):
                self.bad_block(f'{where}.never_auto must be a list of non-empty strings', **{key: '*sudoers*'})
                self.bad_block(f'{where}.never_auto has a duplicate',
                               **{key: ['.github/workflows/**', '*.rules', '*.rules']})
                self.bad_block(f'{where}.never_auto: glob \'a/**b\': ** must be a whole path segment',
                               **{key: ['.github/workflows/**', 'a/**b']})
                # the list can grow and shrink, but never lose the workflow files
                self.bad_block(f'{where}.never_auto must list .github/workflows/**', **{key: []})
                self.bad_block(f'{where}.never_auto must list .github/workflows/**',
                               **{key: ['.github/workflows/*', '*sudoers*']})

    def test_the_shipped_config(self):
        """This checkout's release/config.json: the Part 1 shipped state (plan section 7)."""
        mu = version.merge_up(version.SCRIPT_ROOT)
        self.assertIs(mu['finish']['strict'], False)
        self.assertIs(mu['lock_pin']['verify_finish'], False)
        self.assertEqual(mu['finish']['required_checks'], ['pr-checks', 'pin-propagation-safety'])
        # exact: bot-pr (Dexter, 2026-11-18): every merge the lockfile pin rule makes has a PR as its record
        self.assertEqual(mu['lock_pin'], {'verify_finish': False, 'exact': 'bot-pr', 'extra': 'bot-pr', 'wait_minutes': 15})
        self.assertEqual(mu['never_auto'], ['.github/workflows/**', '*sudoers*', '*keys*.sh',
                                            'scripts-secboot/**', 'kas/include/security/**'])
        self.assertEqual(mu['meta_dsensos'], {
            'never_auto': ['.github/workflows/**', '*sudoers*', '*.rules', 'recipes-bsp/uefi/**',
                           'recipes-bsp/tegra-binaries/**', 'recipes-core/ds-ssh-authorized-keys/**',
                           'recipes-core/ds-unit-hardening/**', 'recipes-core/ds-identity/**',
                           'recipes-core/initrdscripts/**'],
            'required_checks': [],
        })
        with open(os.path.join(version.SCRIPT_ROOT, 'release/config.json')) as f:
            raw = json.load(f)['merge_up']
        self.assertEqual(raw, mu, 'the shipped block spells out every key (no reliance on a default)')

    def test_config_cli(self):
        write_config(self.root, {'products': ['vertex', 'aggregator'], 'merge_up': merge_up_block()})

        def run(*args):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = version.main(['--root', self.root, 'config', *args])
            return rc, out.getvalue(), err.getvalue()

        rc, out, _ = run()
        self.assertEqual((rc, json.loads(out)['merge_up']), (0, merge_up_block()))
        self.assertEqual(run('--key', 'merge_up.finish.strict')[:2], (0, 'false\n'))
        self.assertEqual(run('--key', 'merge_up.lock_pin.exact')[:2], (0, 'push\n'))
        self.assertEqual(run('--key', 'merge_up.lock_pin.wait_minutes')[:2], (0, '15\n'))
        self.assertEqual(run('--key', 'merge_up.never_auto')[:2], (0, '[".github/workflows/**", "*sudoers*"]\n'))
        rc, _, err = run('--key', 'merge_up.finish.nope')
        self.assertEqual(rc, 1)
        self.assertIn("no key 'merge_up.finish.nope'", err)
        write_config(self.root, {'products': ['vertex', 'aggregator'], 'merge_up': {'finish': {'strict': 'yes'}}})
        rc, out, err = run('--key', 'merge_up.finish.strict')
        self.assertEqual((rc, out), (1, ''))
        self.assertIn('merge_up.finish.strict must be true or false', err)


class NeverAutoRegexTests(unittest.TestCase):
    """version.never_auto_regex: the never_auto match, wider than glob_regex."""

    def match(self, pattern, path):
        return version.never_auto_regex(pattern).fullmatch(path) is not None

    def test_a_pattern_without_a_slash_matches_any_one_segment(self):
        for path in ('sudoers', 'a/b/50-ds-sudoers', 'etc/sudoers.d/ds-portal', 'packaging/sudoers/portal', 'x/sudoers.d/a/b'):
            self.assertTrue(self.match('*sudoers*', path), path)
        self.assertFalse(self.match('*sudoers*', 'etc/sudo/ers'))
        self.assertFalse(self.match('sudoers', 'etc/sudoers.d/x'))
        self.assertTrue(self.match('sudoers', 'etc/sudoers/x'))
        self.assertTrue(self.match('?.rules', 'a/b.rules/c'))
        self.assertFalse(self.match('?.rules', 'a/bb.rules/c'))

    def test_case_does_not_count(self):
        self.assertTrue(self.match('*sudoers*', 'debian/ds-portal.SUDOERS'))
        self.assertTrue(self.match('*SUDOERS*', 'etc/sudoers.d/x'))
        self.assertTrue(self.match('.github/workflows/**', '.GitHub/WORKFLOWS/a.yml'))
        self.assertTrue(self.match('a/**/b', 'A/x/B'))

    def test_a_pattern_with_a_slash_keeps_its_meaning(self):
        self.assertTrue(self.match('a/**', 'a/x/y'))
        self.assertFalse(self.match('a/**', 'a'))
        self.assertFalse(self.match('a/**', 'x/a/b'))
        self.assertFalse(self.match('a/*.sh', 'a/b/c.sh'))
        self.assertFalse(self.match('a/b', 'a/b/c'))

    def test_a_newline_in_a_file_name_does_not_get_a_path_past_the_list(self):
        # git allows the name, and the conflicted paths are read NUL-separated
        self.assertTrue(self.match('.github/workflows/**', '.github/workflows/a\nb.yml'))
        self.assertTrue(self.match('.github/workflows/**', '.github/workflows/x/a\nb/c.yml'))
        self.assertTrue(self.match('a/**/b', 'a/x\ny/b'))
        self.assertTrue(self.match('*sudoers*', 'etc/sudoers.d/a\nb'))
        self.assertTrue(self.match('*sudoers*', 'a\nb/ds.sudoers'))
        self.assertTrue(self.match('a/?.sh', 'a/\n.sh'))
        self.assertFalse(self.match('.github/workflows/**', '.github/workflows\n/a.yml'))
        self.assertFalse(self.match('a/**', 'x\na/b'))

    def test_it_never_matches_less_than_glob_regex(self):
        paths = ['a', 'a/b', 'a/b/c.sh', 'x/keys.sh', 'x/my_keys_2.sh', 'sudoers', 'a/b/50-ds-sudoers', '.github/workflows/x.yml', 'kas/include/security/a/b']
        for pattern in ('*sudoers*', '*keys*.sh', '.github/workflows/**', 'kas/include/security/**', 'a/*', 'a/**/c.sh', '?'):
            for path in paths:
                if version.glob_regex(pattern).fullmatch(path):
                    self.assertTrue(self.match(pattern, path), (pattern, path))

    def test_glob_regex_itself_is_unchanged(self):
        glob = lambda pattern, path: version.glob_regex(pattern).fullmatch(path) is not None
        self.assertFalse(glob('*sudoers*', 'etc/sudoers.d/ds-portal'))
        self.assertFalse(glob('*sudoers*', 'debian/ds-portal.SUDOERS'))
        self.assertFalse(glob('.github/workflows/**', '.GitHub/workflows/a.yml'))

    def test_it_refuses_what_glob_regex_refuses(self):
        for bad in ('a/**b', '[a]', '**', '/a', 'a//b', '!a'):
            with self.assertRaises(version.CheckError, msg=bad):
                version.never_auto_regex(bad)


class GlobTests(unittest.TestCase):
    """version.glob_regex: the one implementation of the never_auto glob semantics."""

    def matches(self, pattern, path):
        return version.glob_regex(pattern).fullmatch(path) is not None

    def test_table(self):
        cases = [
            # ** as the last segment: everything beneath, at any depth, not the directory itself
            ('.github/workflows/**', '.github/workflows/build.yml', True),
            ('.github/workflows/**', '.github/workflows/sub/dir/x.yml', True),
            ('.github/workflows/**', '.github/workflows', False),
            ('.github/workflows/**', '.github/workflowsx/build.yml', False),
            ('.github/workflows/**', 'x/.github/workflows/build.yml', False),
            ('.github/workflows/**', '.github/dependabot.yml', False),
            # a pattern with a slash is anchored at the repo root
            ('scripts-secboot/**', 'scripts-secboot/burn_fuses.sh', True),
            ('scripts-secboot/**', 'tools/scripts-secboot/burn_fuses.sh', False),
            # ** in front and in the middle: zero or more directories
            ('**/files/*.rules', 'files/a.rules', True),
            ('**/files/*.rules', 'recipes/x/files/a.rules', True),
            ('**/files/*.rules', 'recipes/x/files/sub/a.rules', False),
            ('a/**/b.txt', 'a/b.txt', True),
            ('a/**/b.txt', 'a/x/b.txt', True),
            ('a/**/b.txt', 'a/x/y/z/b.txt', True),
            ('a/**/b.txt', 'a/xb.txt', False),
            ('a/**/b.txt', 'za/x/b.txt', False),
            # * and ? stay inside one path segment
            ('a/*/b.txt', 'a/x/b.txt', True),
            ('a/*/b.txt', 'a/x/y/b.txt', False),
            ('a/*/b.txt', 'a/b.txt', False),
            ('kas/*.yml', 'kas/local.yml', True),
            ('kas/*.yml', 'kas/include/base.yml', False),
            ('a/?.txt', 'a/b.txt', True),
            ('a/?.txt', 'a/bc.txt', False),
            ('a/?.txt', 'a//.txt', False),
            # no slash: the file name alone, at any depth
            ('*sudoers*', 'sudoers', True),
            ('*sudoers*', 'recipes-core/x/files/50-ds-sudoers', True),
            ('*sudoers*', 'recipes-core/x/files/sudoers.d.conf', True),
            ('*sudoers*', 'recipes-core/ds-sudoers/files/rules.conf', False),  # the name, not a directory
            ('*keys*.sh', 'scripts-luks/fetch_luks_keys.sh', True),
            ('*keys*.sh', 'gen_uefi_keys_dts.sh', True),
            ('*keys*.sh', 'scripts/keys.sh.orig', False),
            ('*keys*.sh', 'keys/run.sh', False),
            ('*.rules', 'recipes/udev/files/99-ds.rules', True),
            ('*.rules', 'recipes/udev/files/99-ds.rules.in', False),
            ('Makefile', 'Makefile', True),
            ('Makefile', 'sub/Makefile', True),
            ('Makefile', 'sub/makefile', False),  # case-sensitive
            ('Makefile', 'sub/Makefile.am', False),
            # everything but * ? ** is literal
            ('a.b/c+d/(e)/**', 'a.b/c+d/(e)/f', True),
            ('a.b/c+d/(e)/**', 'axb/c+d/(e)/f', False),
            ('a.b/c+d/(e)/**', 'a.b/ccd/(e)/f', False),
            ('*.y?l', 'x/a.yml', True),
            ('*.y?l', 'x/a.yl', False),
            ('*.y?l', 'x/a$yml', False),
        ]
        for pattern, path, want in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(self.matches(pattern, path), want)

    def test_refused_patterns(self):
        for bad in ('', None, 5, ['a'], '/a', 'a/', 'a//b', './a', 'a/../b', 'a/**b', '**a/b', 'a**',
                    '*', '**', '*/**', '**/*', '[ab].sh', 'a/{b,c}', 'a\\b', '!a', ' a', 'a '):
            with self.subTest(bad=bad):
                with self.assertRaises(version.CheckError):
                    version.glob_regex(bad)


if __name__ == '__main__':
    unittest.main()
