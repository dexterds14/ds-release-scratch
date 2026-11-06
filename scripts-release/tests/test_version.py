"""Tests for scripts-release/version.py: check, config, rc, dev, bump, deb-version, matrix, products.

A fake repo root is built in a temp dir for every test: release/config.json,
release/matrix.json and versions/. The prefixed two-product root is the
reference's; the bare single-product root (product_prefix false) is this
repo's. The git tests run git init in a temp dir; nothing touches the
network or this checkout.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import version  # noqa: E402

FLAVORS = {
    'vertex': ['vertex_jetson-orin-nx-a603-8gb_release', 'vertex_jetson-orin-nx-a603-8gb_release-hardened'],
    'aggregator': ['aggregator_jetson-orin-nx-a603-8gb_release'],
    'ds': ['ds_ds-arm64_release'],
}
DEBS = ['ds-ais-publisher', 'ds-announcer', 'ds-controller', 'ds-cot-xlate', 'ds-gps-publisher', 'ds-interpreter',
        'ds-mqtt-xlate', 'ds-planner', 'ds-processing', 'ds-sdr', 'ds-services', 'ds-utils']
PROBE = {'deb': 'ds-utils', 'path': './usr/include/utils/version_config.h', 'pattern': '#define DS_VERSION "{version}"'}
DEBS_ARTIFACT = {'kind': 'debs', 'debs': DEBS, 'deb_arch': 'arm64', 'deb_version_rule': 'recipe-pv'}
# A single-product config of the debs kind, as the fixture writes it (the shape the
# sensor-portal repo uses; this repo's own config is the install-trees kind).
DS_CONFIG = {'products': ['ds'], 'product_prefix': False, 'bookkeeping_files': ['.github/sdk.env'],
             'artifact': DEBS_ARTIFACT, 'version_probe': PROBE}
# The install-trees kind: one tarball with a directory per service plus the info file.
TREES_ARTIFACT = {'kind': 'install-trees', 'services': DEBS, 'info_file': 'sdk-build-info.txt'}
TREES_PROBE = {'path': './ds-cot-xlate/opt/ds/bin/cot-xlate', 'pattern': '{version}'}
TREES_CONFIG = {'products': ['ds'], 'product_prefix': False, 'bookkeeping_files': ['.github/sdk.env'],
                'artifact': TREES_ARTIFACT, 'version_probe': TREES_PROBE}


def make_root(d, products=('vertex', 'aggregator'), versions=None, matrix=None, config=None):
    """Write a minimal repo root. versions and matrix default to a consistent set; config
    defaults to {"products": [...]} (the prefixed mode), DS_CONFIG is the bare one."""
    versions = {p: '0.0.1' for p in products} if versions is None else versions
    matrix = {p: FLAVORS[p] for p in products} if matrix is None else matrix
    config = {'products': list(products)} if config is None else config
    os.makedirs(os.path.join(d, 'release'))
    os.makedirs(os.path.join(d, 'versions'))
    with open(os.path.join(d, 'release/config.json'), 'w') as f:
        json.dump(config, f)
    with open(os.path.join(d, 'release/matrix.json'), 'w') as f:
        json.dump({'products': {p: {'flavors': fl} for p, fl in matrix.items()}}, f)
    for p, v in versions.items():
        with open(os.path.join(d, 'versions', p), 'w') as f:
            f.write(v if v.endswith('\n') or v == '' else v + '\n')
    with open(os.path.join(d, 'versions/README.md'), 'w') as f:
        f.write('# versions\n')
    return d


def make_ds_root(d, **kw):
    """The bare single-product root: product ds, product_prefix false, the deb keys set."""
    return make_root(d, products=('ds',), config=dict(DS_CONFIG, **kw))


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

    def test_matrix_flavor_shape(self):
        for bad in ('vertex', 'vertex_a603', 'vertex_a603_release_extra', 'Vertex_a603_release', 'vertex__release'):
            with self.subTest(bad=bad):
                tmp = tempfile.TemporaryDirectory()
                make_root(tmp.name, matrix={'vertex': FLAVORS['vertex'] + [bad], 'aggregator': FLAVORS['aggregator']})
                errors = version.check(tmp.name)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f'flavor {bad} is not <product>_<machine>_<type>', errors[0])
                tmp.cleanup()

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

    def test_missing_config(self):
        os.makedirs(os.path.join(self.root, 'release'))
        self.assertEqual(version.check(self.root), ['release/config.json: missing'])

    def test_bad_product_name(self):
        make_root(self.root, products=('Vertex', 'aggregator'), versions={'Vertex': '0.0.1', 'aggregator': '0.0.1'},
                  matrix={'Vertex': [], 'aggregator': []})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("'Vertex'", errors[0])

    def test_bare_root_passes(self):
        make_ds_root(self.root)
        self.assertEqual(version.check(self.root), [])
        self.assertEqual(quiet_main('check', self.root), 0)

    def test_bare_flavor_under_wrong_product(self):
        make_root(self.root, products=('ds',), config=DS_CONFIG, matrix={'ds': ['vertex_ds-arm64_release']})
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn('flavor vertex_ds-arm64_release is listed under product ds', errors[0])


class ConfigTests(unittest.TestCase):
    """release/config.json validation and the defaults `config` fills in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, doc):
        shutil.rmtree(self.root)
        os.makedirs(os.path.join(self.root, 'release'))
        with open(os.path.join(self.root, 'release/config.json'), 'w') as f:
            json.dump(doc, f)

    def test_defaults(self):
        self.write({'products': ['vertex', 'aggregator']})
        self.assertEqual(version.config(self.root), {
            'products': ['vertex', 'aggregator'], 'product_prefix': True, 'bookkeeping_files': [],
            'artifact': {'kind': 'debs', 'debs': [], 'deb_arch': '', 'deb_version_rule': 'recipe-pv'},
            'version_probe': None})
        self.assertTrue(version.product_prefix(self.root))
        self.assertEqual(version.products(self.root), ['vertex', 'aggregator'])

    def test_ds_config(self):
        self.write(DS_CONFIG)
        self.assertEqual(version.config(self.root), DS_CONFIG)
        self.assertFalse(version.product_prefix(self.root))

    def test_install_trees_config(self):
        self.write(TREES_CONFIG)
        self.assertEqual(version.config(self.root), TREES_CONFIG)
        # a debs-kind artifact fills in its defaults; install-trees has none to fill
        self.write({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'arm64'}})
        self.assertEqual(version.config(self.root)['artifact'],
                         {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'arm64', 'deb_version_rule': 'recipe-pv'})

    def test_kinds_do_not_mix(self):
        mixed = [
            # debs keys under install-trees, and the other way round
            (dict(TREES_ARTIFACT, debs=['ds-utils']), 'belong to another kind'),
            (dict(TREES_ARTIFACT, deb_arch='arm64'), 'belong to another kind'),
            (dict(DEBS_ARTIFACT, services=DEBS), 'belong to another kind'),
            (dict(DEBS_ARTIFACT, info_file='x'), 'belong to another kind'),
            # the old flat shape is refused, naming where the keys went
            ({'kind': 'debs'}, '"artifact"'),
        ]
        for art, needle in mixed[:-1]:
            with self.subTest(artifact=art):
                self.write({'products': ['ds'], 'artifact': art})
                with self.assertRaises(version.CheckError) as cm:
                    version.config(self.root)
                self.assertIn(needle, str(cm.exception))
        for legacy in ({'debs': DEBS, 'deb_arch': 'arm64'}, {'deb_version_rule': 'recipe-pv'},
                       {'artifact': TREES_ARTIFACT, 'debs': DEBS}):
            with self.subTest(legacy=legacy):
                self.write(dict({'products': ['ds']}, **legacy))
                with self.assertRaises(version.CheckError) as cm:
                    version.config(self.root)
                self.assertIn('moved under "artifact"', str(cm.exception))
        # a probe of the wrong shape for the kind
        for art, probe in ((TREES_ARTIFACT, PROBE), (DEBS_ARTIFACT, TREES_PROBE)):
            with self.subTest(artifact=art['kind'], probe=sorted(probe)):
                self.write({'products': ['ds'], 'artifact': art, 'version_probe': probe})
                with self.assertRaises(version.CheckError) as cm:
                    version.config(self.root)
                self.assertIn('version_probe', str(cm.exception))

    def test_two_products_without_prefix_refused(self):
        self.write({'products': ['vertex', 'aggregator'], 'product_prefix': False})
        with self.assertRaises(version.CheckError) as cm:
            version.config(self.root)
        self.assertIn('exactly one product', str(cm.exception))
        self.assertIn('exactly one product', version.check(self.root)[0])

    def test_refusals(self):
        bad = [
            ({'products': ['ds'], 'product_prefix': 'no'}, 'product_prefix'),
            ({'products': ['ds'], 'bookkeeping_files': '.github/yocto-ci.env'}, 'bookkeeping_files'),
            ({'products': ['ds'], 'bookkeeping_files': ['/etc/x']}, 'repo-relative'),
            ({'products': ['ds'], 'bookkeeping_files': ['../x']}, 'repo-relative'),
            ({'products': ['ds'], 'bookkeeping_files': ['versions/ds']}, 'outside versions/'),
            ({'products': ['ds'], 'bookkeeping_files': ['a', 'a']}, 'duplicate'),
            ({'products': ['ds'], 'artifact': 'debs'}, '"artifact"'),
            ({'products': ['ds'], 'artifact': {}}, '"artifact"'),
            ({'products': ['ds'], 'artifact': {'kind': 'rpms'}}, 'artifact.kind'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'extra': 1}}, 'unknown artifact key'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils', 'ds-utils'], 'deb_arch': 'arm64'}}, 'duplicate'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['DS-Utils'], 'deb_arch': 'arm64'}}, 'DS-Utils'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils']}}, 'deb_arch'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'ARM 64'}}, 'deb_arch'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'deb_version_rule': 'pv'}}, 'deb_version_rule'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'arm64'},
              'version_probe': {'deb': 'ds-utils', 'path': 'x'}}, 'version_probe'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'arm64'},
              'version_probe': {'deb': 'ds-sdr', 'path': 'x', 'pattern': '{version}'}}, 'not in artifact.debs'),
            ({'products': ['ds'], 'artifact': {'kind': 'debs', 'debs': ['ds-utils'], 'deb_arch': 'arm64'},
              'version_probe': {'deb': 'ds-utils', 'path': 'x', 'pattern': 'DS_VERSION'}}, '{version}'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'services': DEBS}}, 'needs info_file'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'info_file': 'i.txt'}}, 'needs services'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'services': [], 'info_file': 'i.txt'}}, 'at least one service'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'services': ['ds-utils', 'ds-utils'], 'info_file': 'i.txt'}}, 'duplicate'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'services': ['ds-utils'], 'info_file': 'a/i.txt'}}, 'info_file'),
            ({'products': ['ds'], 'artifact': {'kind': 'install-trees', 'services': ['ds-utils'], 'info_file': 'ds-utils'}}, 'also listed in services'),
            ({'products': ['ds'], 'artifact': TREES_ARTIFACT, 'version_probe': {'path': 'x', 'pattern': 'v'}}, '{version}'),
            ({'products': ['ds'], 'extra': 1}, 'unknown key'),
            ({'products': []}, 'products'),
            (['ds'], 'object'),
        ]
        for doc, needle in bad:
            with self.subTest(doc=doc):
                self.write(doc)
                with self.assertRaises(version.CheckError) as cm:
                    version.config(self.root)
                self.assertIn(needle, str(cm.exception))

    def test_config_cli(self):
        self.write(DS_CONFIG)
        r = subprocess.run([sys.executable, version.__file__, '--root', self.root, 'config'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), DS_CONFIG)
        for key, want in (('product_prefix', 'false'), ('artifact', json.dumps(DEBS_ARTIFACT)),
                          ('version_probe', json.dumps(PROBE)), ('bookkeeping_files', json.dumps(['.github/sdk.env']))):
            r = subprocess.run([sys.executable, version.__file__, '--root', self.root, 'config', '--key', key],
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, want), r.stderr)
        self.write({'products': ['vertex', 'aggregator'], 'product_prefix': False})
        r = subprocess.run([sys.executable, version.__file__, '--root', self.root, 'config'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, '')
        self.assertIn('exactly one product', r.stderr)


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

    def test_deb_version(self):
        self.assertEqual(version.deb_version('1.1.0'), '1.1.0')
        self.assertEqual(version.deb_version('1.1.0-rc.5.github-abc1234'), '1.1.0~rc.5.github.abc1234')
        self.assertEqual(version.deb_version('1.1.0-dev.3.gabc1234.dirty'), '1.1.0~dev.3.gabc1234.dirty')
        for bad in ('v1.0.0', '1.0.0+x', '1.0.0-RC.1', '1.0'):
            with self.subTest(bad=bad), self.assertRaises(version.CheckError):
                version.deb_version(bad)

    @unittest.skipUnless(shutil.which('dpkg'), 'dpkg --compare-versions is not available')
    def test_deb_version_orders_like_semver(self):
        """dev < rc.N < rc.N+1 < release, the order ds-ota applies, under dpkg --compare-versions."""
        sha = 'abc1234'
        chain = ['1.1.0-dev.3.g' + sha, '1.1.0-rc.9.github-' + sha, '1.1.0-rc.10.github-' + sha, '1.1.0']
        debs = [version.deb_version(v) for v in chain]
        for lo, hi in zip(debs, debs[1:]):
            with self.subTest(lo=lo, hi=hi):
                r = subprocess.run(['dpkg', '--compare-versions', lo, 'lt', hi])
                self.assertEqual(r.returncode, 0, f'{lo} is not lt {hi}')
        # the next pending version sorts above the previous release
        self.assertEqual(subprocess.run(['dpkg', '--compare-versions', '1.1.0', 'lt', version.deb_version('1.1.1-rc.1.github-' + sha)]).returncode, 0)


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
        rc, out, err = self.run_cli('matrix', 'ghost')
        self.assertEqual(rc, 1)
        rc, out, err = self.run_cli('products')
        self.assertEqual(out.splitlines(), ['vertex', 'aggregator'])
        rc, out, err = self.run_cli('products', '--json')
        self.assertEqual(json.loads(out), ['vertex', 'aggregator'])

    def test_rc_refuses_bad_versions_file(self):
        with open(os.path.join(self.root, 'versions/vertex'), 'w') as f:
            f.write('v0.0.1\n')
        rc, out, err = self.run_cli('rc', 'vertex')
        self.assertEqual(rc, 1)
        self.assertEqual(out, '')
        self.assertIn('versions/vertex', err)

    def test_deb_version_cli(self):
        rc, out, err = self.run_cli('deb-version', '0.0.1-rc.3.github-' + self.head[:7])
        self.assertEqual((rc, out), (0, '0.0.1~rc.3.github.' + self.head[:7]), err)
        rc, out, err = self.run_cli('deb-version', '1.0.0+x')
        self.assertEqual((rc, out), (1, ''))
        self.assertIn('not lowercase X.Y.Z', err)


class BareRootGitTests(unittest.TestCase):
    """The same CLI on this repo's shape: one product, product_prefix false."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_ds_root(self.tmp.name)
        self.head = make_repo(self.root, 2)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        r = subprocess.run([sys.executable, version.__file__, '--root', self.root, *args],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def test_rc_bump_matrix_products(self):
        rc, out, err = self.run_cli('rc', 'ds')
        self.assertEqual((rc, out), (0, f'0.0.1-rc.2.github-{self.head[:7]}'), err)
        rc, out, err = self.run_cli('bump', 'ds', 'major')
        self.assertEqual((rc, out), (0, '1.0.0'), err)
        rc, out, err = self.run_cli('matrix', 'ds')
        self.assertEqual(json.loads(out), FLAVORS['ds'])
        rc, out, err = self.run_cli('products')
        self.assertEqual(out, 'ds')
        rc, out, err = self.run_cli('check')
        self.assertEqual(rc, 0, err)
        self.assertIn('ok (ds)', out)


if __name__ == '__main__':
    unittest.main()
