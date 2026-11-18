"""artifacts.py build-manifest: the install-trees kind beside debs (ENG-2110).

Stdlib only. The deb cases need dpkg-deb on PATH and are skipped without it.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ARTIFACTS = os.path.join(HERE, '..', 'artifacts.py')
SERVICES = ['ds-ais-publisher', 'ds-announcer', 'ds-controller', 'ds-cot-xlate', 'ds-gps-publisher',
            'ds-interpreter', 'ds-mqtt-xlate', 'ds-planner', 'ds-processing', 'ds-sdr', 'ds-services',
            'ds-utils']
SHA = '0' * 40
SDK_SHA = 'a' * 64


def sha256(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def run(*args, check=True):
    # Without GITHUB_REPOSITORY, so the first commit_hashes entry is the product
    # whichever repository's CI runs this test.
    env = {k: v for k, v in os.environ.items() if k != 'GITHUB_REPOSITORY'}
    return subprocess.run([sys.executable, '-I', ARTIFACTS, 'build-manifest', *args],
                          capture_output=True, text=True, check=check, env=env)


def common(out, **kw):
    args = ['--out', out, '--product', 'ds', '--flavor', 'ds_ds-arm64_release', '--machine', 'ds-arm64',
            '--sha', SHA, '--branch', 'main', '--run-id', '1', '--build-datetime', '20261030T120000Z']
    for k, v in kw.items():
        args += ['--' + k.replace('_', '-'), v]
    return args


def make_tarball(path, services=SERVICES, with_info=True, extra_top_file=None):
    """An install-tree tarball the way sdk-build.sh packs it: ./<service>/... plus ./sdk-build-info.txt."""
    root = tempfile.mkdtemp()
    try:
        for s in services:
            d = os.path.join(root, s, 'opt', 'ds', 'bin')
            os.makedirs(d)
            with open(os.path.join(d, s), 'w') as f:
                f.write('binary 0.0.1-rc.1.github-abc1234\n')
        if with_info:
            with open(os.path.join(root, 'sdk-build-info.txt'), 'w') as f:
                f.write('ds_version=0.0.1-rc.1.github-abc1234\n')
        if extra_top_file:
            with open(os.path.join(root, extra_top_file), 'w') as f:
                f.write('x\n')
        with tarfile.open(path, 'w:gz') as tf:
            tf.add(root, arcname='.')
    finally:
        shutil.rmtree(root)


class InstallTrees(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.src = os.path.join(self.tmp, 'src')
        os.makedirs(self.src)
        self.tarball = os.path.join(self.src, 'ds-sdk-build-jetson-orin-nx-a603-8gb-0000000.tar.gz')
        make_tarball(self.tarball)
        self.info = os.path.join(self.src, 'sdk-build-info.txt')
        with open(self.info, 'w') as f:
            f.write('ds_version=0.0.1-rc.1.github-abc1234\nds_commit=%s\n' % SHA)
        self.out = os.path.join(self.tmp, 'out')

    def manifest(self):
        with open(os.path.join(self.out, 'manifest.json')) as f:
            return json.load(f)

    def test_good_tarball(self):
        run('--kind', 'install-trees', *common(self.out, build_version='0.0.1-rc.1.github-abc1234',
                                                release_line='main'),
            '--layer', 'dsensos-sdk=' + SDK_SHA, '--layer', 'schemas=' + 'b' * 40,
            '--sdk-uri', 's3://bucket/release/vertex/v2.0.0/jetson-orin-nx-a603-8gb/standard/x.sh',
            '--sdk-sha256', SDK_SHA, '--sdk-machine', 'jetson-orin-nx-a603-8gb', '--sdk-os-release', 'v2.0.0',
            '--extra', self.info, self.tarball)
        m = self.manifest()
        self.assertEqual(m['artifacts']['debs'], [])
        keys = [e['key'] for e in m['artifacts']['extra']]
        self.assertEqual(keys, [os.path.basename(self.tarball), 'sdk-build-info.txt'])
        self.assertEqual(m['artifacts']['install_tree'],
                         {'key': os.path.basename(self.tarball), 'services': SERVICES})
        self.assertEqual(m['buildparams']['sdk'], {
            'uri': 's3://bucket/release/vertex/v2.0.0/jetson-orin-nx-a603-8gb/standard/x.sh',
            'sha256': SDK_SHA, 'machine': 'jetson-orin-nx-a603-8gb', 'os_release': 'v2.0.0'})
        self.assertEqual(m['buildparams']['build_version'], '0.0.1-rc.1.github-abc1234')
        self.assertEqual(m['buildparams']['release_line'], 'main')
        self.assertEqual([c['name'] for c in m['commit_hashes']], ['ds', 'dsensos-sdk', 'schemas'])
        # every file in out/ is copied and summed; manifest.json is the last line
        with open(os.path.join(self.out, 'SHA256SUMS')) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[-1].split('  ')[1], 'manifest.json')
        self.assertEqual([l.split('  ')[1] for l in lines[:-1]], keys)
        for line in lines:
            digest, key = line.split('  ')
            self.assertEqual(sha256(os.path.join(self.out, key)), digest, key)
        self.assertFalse(os.path.exists(os.path.join(self.out, 'deb')))

    def test_services_are_the_top_level_dirs_only(self):
        tb = os.path.join(self.src, 'two.tar.gz')
        make_tarball(tb, services=['ds-utils', 'ds-cot-xlate'], extra_top_file='README')
        run('--kind', 'install-trees', *common(self.out), tb)
        self.assertEqual(self.manifest()['artifacts']['install_tree']['services'], ['ds-cot-xlate', 'ds-utils'])

    def test_no_sdk_options_no_sdk_field(self):
        run('--kind', 'install-trees', *common(self.out), self.tarball)
        m = self.manifest()
        self.assertNotIn('sdk', m['buildparams'])
        self.assertEqual(m['artifacts']['debs'], [])

    def test_sdk_uri_without_sha_fails(self):
        r = run('--kind', 'install-trees', *common(self.out), '--sdk-uri', 's3://x/y', self.tarball, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--sdk-uri and --sdk-sha256 go together', r.stderr)

    def test_exactly_one_tarball(self):
        r = run('--kind', 'install-trees', *common(self.out), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('exactly one tarball, got 0', r.stderr)
        r = run('--kind', 'install-trees', *common(self.out), self.tarball, self.tarball, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('exactly one tarball, got 2', r.stderr)

    def test_not_a_tarball(self):
        r = run('--kind', 'install-trees', *common(self.out), self.info, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('is not a tarball', r.stderr)

    def test_tarball_without_directories(self):
        tb = os.path.join(self.src, 'flat.tar.gz')
        make_tarball(tb, services=[], with_info=True)
        r = run('--kind', 'install-trees', *common(self.out), tb, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('has no top-level directory', r.stderr)


@unittest.skipUnless(shutil.which('dpkg-deb') and shutil.which('fakeroot'), 'dpkg-deb and fakeroot needed')
class Debs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = os.path.join(self.tmp, 'out')
        self.debs = [self.make_deb('ds-utils', '0.0.1~rc.1.github.abc1234'),
                     self.make_deb('ds-cot-xlate', '0.0.1~rc.1.github.abc1234')]

    def make_deb(self, package, version):
        root = os.path.join(self.tmp, package + '-root')
        os.makedirs(os.path.join(root, 'DEBIAN'))
        os.makedirs(os.path.join(root, 'opt', 'ds', 'bin'))
        with open(os.path.join(root, 'DEBIAN', 'control'), 'w') as f:
            f.write(f'Package: {package}\nVersion: {version}\nArchitecture: arm64\nMaintainer: t\nDescription: t\n')
        with open(os.path.join(root, 'opt', 'ds', 'bin', package), 'w') as f:
            f.write('x\n')
        deb = os.path.join(self.tmp, f'{package}_{version}_arm64.deb')
        subprocess.run(['fakeroot', 'dpkg-deb', '-Zgzip', '-b', root, deb], check=True, capture_output=True)
        return deb

    def test_debs_unchanged(self):
        run(*common(self.out), *self.debs)
        with open(os.path.join(self.out, 'manifest.json')) as f:
            m = json.load(f)
        self.assertEqual([d['package'] for d in m['artifacts']['debs']], ['ds-cot-xlate', 'ds-utils'])
        self.assertEqual(m['artifacts']['debs'][0]['key'], 'deb/' + os.path.basename(self.debs[1]))
        self.assertEqual(m['artifacts']['extra'], [])
        self.assertNotIn('install_tree', m['artifacts'])
        self.assertNotIn('sdk', m['buildparams'])
        with open(os.path.join(self.out, 'SHA256SUMS')) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1].split('  ')[1], 'manifest.json')

    def test_debs_with_sdk_fields(self):
        run(*common(self.out), '--sdk-uri', 's3://b/k.sh', '--sdk-sha256', SDK_SHA, *self.debs)
        with open(os.path.join(self.out, 'manifest.json')) as f:
            m = json.load(f)
        self.assertEqual(m['buildparams']['sdk'], {'uri': 's3://b/k.sh', 'sha256': SDK_SHA, 'machine': '', 'os_release': ''})

    def test_debs_need_one(self):
        r = run(*common(self.out), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('needs at least one deb', r.stderr)
        r = run(*common(self.out), '--exclude=ds-', *self.debs, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('no debs after --exclude', r.stderr)


if __name__ == '__main__':
    unittest.main()
