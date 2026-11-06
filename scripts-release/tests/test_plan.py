"""Tests for scripts-release/plan.py: the rc-build plan job's outputs.

Reuses test_version's fake repo root and throwaway git repo; nothing touches
the network or this checkout.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import plan  # noqa: E402
from test_version import FLAVORS, git, make_ds_root, make_repo, make_root  # noqa: E402


def quiet_main(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = plan.main(list(args))
    return rc, out.getvalue(), err.getvalue()


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_root(self.tmp.name)
        self.sha = make_repo(self.root, 3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_line(self):
        p = plan.plan('vertex/main', self.root, self.root)
        self.assertEqual(p['skip'], False)
        self.assertEqual(p['product'], 'vertex')
        self.assertEqual(p['kind'], 'main')
        self.assertEqual(p['line'], 'vertex/main')
        self.assertEqual(p['pending'], '0.0.1')
        self.assertEqual(p['rc_version'], f'0.0.1-rc.3.github-{self.sha[:7]}')
        self.assertEqual(p['matrix'], {'include': [{'flavor': f} for f in FLAVORS['vertex']]})

    def test_patch_line_and_explicit_sha(self):
        first = git(self.root, 'rev-list', '--max-parents=0', 'HEAD')
        p = plan.plan('refs/heads/aggregator/1.2.x', self.root, self.root, sha=first)
        self.assertEqual((p['product'], p['kind'], p['line']), ('aggregator', 'patch', 'aggregator/1.2.x'))
        self.assertEqual(p['rc_version'], f'0.0.1-rc.1.github-{first[:7]}')
        self.assertEqual(p['matrix'], {'include': [{'flavor': FLAVORS['aggregator'][0]}]})

    def test_non_lines_skip(self):
        for ref in ('dev/main', 'vertex/feature', 'ota-dev', '1.x.x', 'vertex/main/x', 'unknown/main'):
            with self.subTest(ref=ref):
                self.assertEqual(plan.plan(ref, self.root, self.root), {'skip': True, 'line': ref})

    def test_output_lines_and_github_output(self):
        out_file = os.path.join(self.tmp.name, 'out')
        rc, out, err = quiet_main('vertex/main', '--root', self.root, '--github-output', out_file)
        self.assertEqual(rc, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], 'skip=false')
        got = dict(line.split('=', 1) for line in lines)
        self.assertEqual(sorted(got), sorted(plan.KEYS))
        self.assertEqual(json.loads(got['matrix']), {'include': [{'flavor': f} for f in FLAVORS['vertex']]})
        self.assertNotIn('\n', got['matrix'])
        with open(out_file) as f:
            self.assertEqual(f.read(), out)

    def test_skip_output(self):
        rc, out, err = quiet_main('dev/main', '--root', self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines(), ['skip=true', 'line=dev/main'])
        self.assertIn('not a release line', err)

    def test_json_output(self):
        rc, out, _ = quiet_main('vertex/main', '--root', self.root, '--json')
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)['kind'], 'main')

    def test_bad_version_file_fails(self):
        with open(os.path.join(self.root, 'versions/vertex'), 'w') as f:
            f.write('v1.0.0\n')
        rc, out, err = quiet_main('vertex/main', '--root', self.root)
        self.assertEqual(rc, 1)
        self.assertIn('versions/vertex', err)
        self.assertEqual(out, '')

    def test_bad_sha_fails(self):
        rc, _, err = quiet_main('vertex/main', '--root', self.root, '--sha', 'nope')
        self.assertEqual(rc, 1)
        self.assertIn('plan.py:', err)


class BarePlanTests(unittest.TestCase):
    """product_prefix false: main, N.x.x and N.M.x are the lines of the one product."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_ds_root(self.tmp.name)
        self.sha = make_repo(self.root, 3)

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_line(self):
        p = plan.plan('main', self.root, self.root)
        self.assertEqual(p, {'skip': False, 'product': 'ds', 'kind': 'main', 'line': 'main',
                             'rc_version': f'0.0.1-rc.3.github-{self.sha[:7]}', 'pending': '0.0.1',
                             'matrix': {'include': [{'flavor': 'ds_ds-arm64_release'}]}})

    def test_minor_and_patch_lines(self):
        p = plan.plan('refs/heads/1.x.x', self.root, self.root)
        self.assertEqual((p['product'], p['kind'], p['line']), ('ds', 'minor', '1.x.x'))
        p = plan.plan('1.2.x', self.root, self.root)
        self.assertEqual((p['product'], p['kind'], p['line']), ('ds', 'patch', '1.2.x'))

    def test_non_lines_skip(self):
        for ref in ('dev/main', 'release/vl', '1.x.x/extra', '1foo.x.x', 'ds/main', 'feature', 'main/x'):
            with self.subTest(ref=ref):
                self.assertEqual(plan.plan(ref, self.root, self.root), {'skip': True, 'line': ref})

    def test_output_lines(self):
        rc, out, err = quiet_main('main', '--root', self.root)
        self.assertEqual(rc, 0, err)
        got = dict(line.split('=', 1) for line in out.strip().splitlines())
        self.assertEqual((got['skip'], got['product'], got['line'], got['kind']), ('false', 'ds', 'main', 'main'))
        rc, out, err = quiet_main('1foo.x.x', '--root', self.root)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip().splitlines(), ['skip=true', 'line=1foo.x.x'])
        self.assertIn('not a release line', err)


if __name__ == '__main__':
    unittest.main()
