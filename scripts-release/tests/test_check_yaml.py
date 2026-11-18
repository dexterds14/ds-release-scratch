"""Tests for check_yaml.py: the workflow lint that catches what kas lint does not (ENG-2233)."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), 'check_yaml.py')
REPO = os.path.dirname(os.path.dirname(HERE))

# The shape of the 2026-11-02 vertex/1.x.x -> vertex/main conflict in release-rc-build.yml: the
# child changed `runner:`, the parent added lines right after it. A union of both sides keeps
# the old and the new `runner:` line, and PyYAML's safe_load takes the last one silently.
UNION = textwrap.dedent('''\
    jobs:
      build:
        uses: ./.github/workflows/yocto-build.yml
        with:
          runner: ${{ endsWith(matrix.flavor, '-hardened') && 'yocto-ondemand' || 'yocto-spot' }}
          # No build_sdk here
          runner: ${{ (github.run_attempt != '1' || endsWith(matrix.flavor, '-hardened')) && 'yocto-ondemand' || 'yocto-spot' }}
        secrets: inherit
    ''')
MARKED = textwrap.dedent('''\
    jobs:
      build:
        with:
    <<<<<<< vertex/main
          runner: a
    ||||||| base
          runner: b
    =======
          runner: c
    >>>>>>> vertex/1.x.x
    ''')


def run(*paths):
    # The same interpreter and site setup as this process (no -I): on the GKE runners PyYAML is
    # only in the user site (pip install --user kas pulls it in), and -I would hide it from the
    # script while HAVE_YAML below still saw it, failing every test instead of skipping.
    return subprocess.run([sys.executable, SCRIPT, *paths], capture_output=True, text=True, cwd=REPO)


try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


# pr-checks runs check_yaml.py in its own step with PyYAML installed; this suite is the
# script's tests and needs PyYAML too, so it skips (visibly) on a host without it.
@unittest.skipUnless(HAVE_YAML, 'PyYAML not installed (pr-checks installs it for check_yaml.py)')
class CheckYamlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='check-yaml-')

    def file(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, 'w') as f:
            f.write(text)
        return p

    def test_the_repo_workflows_pass(self):
        r = run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r'check_yaml.py: \d+ file\(s\), 0 with problems')

    def test_duplicate_key_from_a_union_merge_fails(self):
        import yaml
        self.assertIn('runner', yaml.safe_load(UNION)['jobs']['build']['with'])  # a plain parse passes it
        r = run(self.file('union.yml', UNION))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("union.yml:7: duplicate key 'runner' (first at line 5)", r.stdout)

    def test_conflict_markers_fail_with_their_lines(self):
        r = run(self.file('marked.yml', MARKED))
        self.assertEqual(r.returncode, 1, r.stdout)
        for line in (4, 6, 8, 10):
            self.assertIn(f'marked.yml:{line}: conflict marker', r.stdout)

    def test_same_key_in_different_mappings_is_fine(self):
        text = 'a:\n  runner: x\nb:\n  runner: y\n'
        self.assertEqual(run(self.file('ok.yml', text)).returncode, 0)

    def test_merge_key_is_not_a_duplicate(self):
        text = 'base: &b\n  x: 1\njob:\n  <<: *b\n  y: 2\n'
        self.assertEqual(run(self.file('merge.yml', text)).returncode, 0)

    def test_invalid_yaml_fails(self):
        r = run(self.file('bad.yml', 'a: [1, 2\n'))
        self.assertEqual(r.returncode, 1)
        self.assertIn('bad.yml:', r.stdout)

    def test_a_bad_file_does_not_hide_the_good_ones(self):
        good = self.file('good.yml', 'a: 1\n')
        bad = self.file('dup.yml', 'a: 1\na: 2\n')
        r = run(good, bad)
        self.assertEqual(r.returncode, 1)
        self.assertIn('2 file(s), 1 with problems', r.stdout)


if __name__ == '__main__':
    unittest.main()
