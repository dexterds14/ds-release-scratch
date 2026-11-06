"""Tests for scripts-release/lines.py: line names, parents and children.

Two trees. The prefixed one (product_prefix true, the reference's mode):
vertex/main, vertex/1.x.x, vertex/1.2.x, vertex/2.0.x (no vertex/2.x.x, so
its parent is main), aggregator/main, aggregator/1.x.x, plus branches that
are not lines; trees must never cross. The bare one (product_prefix false,
this repo's mode): main, 1.x.x, 1.2.x, 2.0.x, plus names with a slash and
near misses, which are never lines.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import lines  # noqa: E402

PRODUCTS = ['vertex', 'aggregator']
HEADS = {
    'vertex/main', 'vertex/1.x.x', 'vertex/1.2.x', 'vertex/2.0.x',
    'aggregator/main', 'aggregator/1.x.x',
    'ota-dev', 'ds-scarthgap', 'dev/main', 'dev/1.x.x', 'vertex/feature', 'vertex/1.x.x/extra',
    'merge-up/vertex/1.2.x/abc1234', 'sync/vertex/abc1234',
}
LS_REMOTE = ''.join(f'{"%040x" % i}\trefs/heads/{h}\n' for i, h in enumerate(sorted(HEADS)))
BARE = ['ds']
BARE_HEADS = {
    'main', '1.x.x', '1.2.x', '2.0.x',
    'dev/main', 'dev/1.x.x', 'release/vl', '1.x.x/extra', '1foo.x.x', 'feature', 'ds/main', 'ds/1.x.x',
    'merge-up/1.2.x/abc1234',
}
BARE_LS_REMOTE = ''.join(f'{"%040x" % i}\trefs/heads/{h}\n' for i, h in enumerate(sorted(BARE_HEADS)))


class ParseTests(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(lines.parse_line('vertex/main', PRODUCTS),
                         {'product': 'vertex', 'kind': 'main', 'version_prefix': ''})
        self.assertEqual(lines.parse_line('vertex/1.x.x', PRODUCTS),
                         {'product': 'vertex', 'kind': 'minor', 'version_prefix': '1.'})
        self.assertEqual(lines.parse_line('vertex/1.2.x', PRODUCTS),
                         {'product': 'vertex', 'kind': 'patch', 'version_prefix': '1.2.'})
        self.assertEqual(lines.parse_line('aggregator/10.0.x', PRODUCTS),
                         {'product': 'aggregator', 'kind': 'patch', 'version_prefix': '10.0.'})

    def test_ref_prefixes_are_stripped(self):
        for ref in ('refs/heads/vertex/1.x.x', 'refs/remotes/origin/vertex/1.x.x'):
            self.assertEqual(lines.parse_line(ref, PRODUCTS)['kind'], 'minor')

    def test_not_lines(self):
        for ref in ('ota-dev', 'ds-scarthgap', 'main', 'dev/main', 'dev/1.x.x', 'vertex', 'vertex/',
                    'vertex/feature', 'vertex/1.x', 'vertex/1.2.3', 'vertex/x.x.x', 'vertex/01.x.x',
                    'vertex/1.x.x/extra', 'vertex/1.X.x', 'Vertex/main', 'vertex/main/x', 'vertex/v1.x.x',
                    'merge-up/vertex/1.2.x/abc1234'):
            with self.subTest(ref=ref):
                self.assertEqual(lines.parse_line(ref, PRODUCTS), lines.NONE)

    def test_unconfigured_product_is_not_a_line(self):
        self.assertEqual(lines.parse_line('vertex/main', ['aggregator']), lines.NONE)


class BareParseTests(unittest.TestCase):
    """product_prefix false: bare names, the product is the configured one."""

    def test_lines(self):
        self.assertEqual(lines.parse_line('main', BARE, False), {'product': 'ds', 'kind': 'main', 'version_prefix': ''})
        self.assertEqual(lines.parse_line('1.x.x', BARE, False), {'product': 'ds', 'kind': 'minor', 'version_prefix': '1.'})
        self.assertEqual(lines.parse_line('1.2.x', BARE, False), {'product': 'ds', 'kind': 'patch', 'version_prefix': '1.2.'})
        self.assertEqual(lines.parse_line('10.0.x', BARE, False), {'product': 'ds', 'kind': 'patch', 'version_prefix': '10.0.'})
        for ref in ('refs/heads/1.x.x', 'refs/remotes/origin/1.x.x'):
            self.assertEqual(lines.parse_line(ref, BARE, False)['kind'], 'minor')

    def test_not_lines(self):
        for ref in ('dev/main', 'release/vl', '1.x.x/extra', '1foo.x.x', 'ds/main', 'ds/1.x.x', 'main/x',
                    'feature', 'ota-dev', '1.x', '1.2.3', 'x.x.x', '01.x.x', '1.X.x', 'Main', 'v1.x.x', '',
                    'merge-up/1.2.x/abc1234'):
            with self.subTest(ref=ref):
                self.assertEqual(lines.parse_line(ref, BARE, False), lines.NONE)

    def test_line_name(self):
        self.assertEqual(lines.line_name('ds', '1.x.x', False), '1.x.x')
        self.assertEqual(lines.line_name('ds', '1.x.x', True), 'ds/1.x.x')
        self.assertEqual(lines.line_name('ds', 'main'), 'ds/main')

    def test_line_key_on_bare_and_prefixed_names(self):
        self.assertEqual(lines.line_key('main'), (0,))
        self.assertEqual(lines.line_key('vertex/main'), (0,))
        self.assertEqual(lines.line_key('1.x.x'), (1, 1))
        self.assertEqual(lines.line_key('1.10.x'), (1, 1, 10))
        self.assertEqual(lines.line_key('vertex/1.10.x'), (1, 1, 10))


class HeadsTests(unittest.TestCase):
    def test_parse_ls_remote_and_bare_names(self):
        self.assertEqual(lines.parse_heads(LS_REMOTE), HEADS)
        self.assertEqual(lines.parse_heads('vertex/main\n\n  aggregator/main \n'), {'vertex/main', 'aggregator/main'})


class ParentTests(unittest.TestCase):
    def test_minor_parent_is_main(self):
        self.assertEqual(lines.parent_of('vertex/1.x.x', HEADS, PRODUCTS), 'vertex/main')
        self.assertEqual(lines.parent_of('aggregator/1.x.x', HEADS, PRODUCTS), 'aggregator/main')

    def test_patch_parent_is_minor_when_it_exists(self):
        self.assertEqual(lines.parent_of('vertex/1.2.x', HEADS, PRODUCTS), 'vertex/1.x.x')

    def test_patch_parent_falls_back_to_main(self):
        self.assertEqual(lines.parent_of('vertex/2.0.x', HEADS, PRODUCTS), 'vertex/main')
        self.assertEqual(lines.parent_of('vertex/1.2.x', set(), PRODUCTS), 'vertex/main')

    def test_main_and_non_lines_have_no_parent(self):
        for ref in ('vertex/main', 'ota-dev', 'dev/1.2.x'):
            with self.assertRaises(ValueError):
                lines.parent_of(ref, HEADS, PRODUCTS)

    def test_trees_never_cross(self):
        # aggregator has no 1.2.x; a vertex minor line must not become its parent.
        self.assertEqual(lines.parent_of('aggregator/1.2.x', HEADS, PRODUCTS), 'aggregator/1.x.x')
        self.assertEqual(lines.parent_of('aggregator/1.2.x', HEADS - {'aggregator/1.x.x'}, PRODUCTS),
                         'aggregator/main')


class ChildrenTests(unittest.TestCase):
    def test_children_of_main(self):
        self.assertEqual(lines.children_of('vertex/main', HEADS, PRODUCTS), ['vertex/1.x.x', 'vertex/2.0.x'])
        self.assertEqual(lines.children_of('aggregator/main', HEADS, PRODUCTS), ['aggregator/1.x.x'])

    def test_children_of_minor_and_patch(self):
        self.assertEqual(lines.children_of('vertex/1.x.x', HEADS, PRODUCTS), ['vertex/1.2.x'])
        self.assertEqual(lines.children_of('aggregator/1.x.x', HEADS, PRODUCTS), [])
        self.assertEqual(lines.children_of('vertex/1.2.x', HEADS, PRODUCTS), [])

    def test_children_sorted_by_version(self):
        heads = {'vertex/main', 'vertex/10.x.x', 'vertex/2.x.x', 'vertex/1.x.x', 'vertex/1.10.x', 'vertex/1.2.x'}
        self.assertEqual(lines.children_of('vertex/main', heads, PRODUCTS),
                         ['vertex/1.x.x', 'vertex/2.x.x', 'vertex/10.x.x'])
        self.assertEqual(lines.children_of('vertex/1.x.x', heads, PRODUCTS), ['vertex/1.2.x', 'vertex/1.10.x'])

    def test_non_line_has_no_children(self):
        with self.assertRaises(ValueError):
            lines.children_of('ota-dev', HEADS, PRODUCTS)


class BareTreeTests(unittest.TestCase):
    def test_parent_with_and_without_the_minor_line(self):
        self.assertEqual(lines.parent_of('1.x.x', BARE_HEADS, BARE, False), 'main')
        self.assertEqual(lines.parent_of('1.2.x', BARE_HEADS, BARE, False), '1.x.x')
        self.assertEqual(lines.parent_of('2.0.x', BARE_HEADS, BARE, False), 'main')
        self.assertEqual(lines.parent_of('1.2.x', set(), BARE, False), 'main')
        self.assertEqual(lines.parent_of('1.2.x', BARE_HEADS - {'1.x.x'}, BARE, False), 'main')
        # a prefixed head never counts as the minor line in bare mode
        self.assertEqual(lines.parent_of('1.2.x', {'ds/1.x.x', 'main'}, BARE, False), 'main')

    def test_main_and_non_lines_have_no_parent(self):
        for ref in ('main', 'dev/1.2.x', '1foo.x.x', 'ds/1.2.x'):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                lines.parent_of(ref, BARE_HEADS, BARE, False)

    def test_children(self):
        self.assertEqual(lines.children_of('main', BARE_HEADS, BARE, False), ['1.x.x', '2.0.x'])
        self.assertEqual(lines.children_of('1.x.x', BARE_HEADS, BARE, False), ['1.2.x'])
        self.assertEqual(lines.children_of('1.2.x', BARE_HEADS, BARE, False), [])
        heads = {'main', '10.x.x', '2.x.x', '1.x.x', '1.10.x', '1.2.x'}
        self.assertEqual(lines.children_of('main', heads, BARE, False), ['1.x.x', '2.x.x', '10.x.x'])
        self.assertEqual(lines.children_of('1.x.x', heads, BARE, False), ['1.2.x', '1.10.x'])
        with self.assertRaises(ValueError):
            lines.children_of('dev/main', BARE_HEADS, BARE, False)


class CliTests(unittest.TestCase):
    """The CLI reads the product list from release/config.json under --root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, 'release'))
        with open(os.path.join(self.tmp.name, 'release/config.json'), 'w') as f:
            json.dump({'products': PRODUCTS}, f)
        self.heads = os.path.join(self.tmp.name, 'heads.txt')
        with open(self.heads, 'w') as f:
            f.write(LS_REMOTE)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None):
        r = subprocess.run([sys.executable, lines.__file__, '--root', self.tmp.name, *args],
                           input=stdin or '', capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def test_parse(self):
        rc, out, err = self.run_cli('parse', 'refs/heads/vertex/1.2.x')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {'product': 'vertex', 'kind': 'patch', 'version_prefix': '1.2.'})
        rc, out, err = self.run_cli('parse', 'dev/main')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['kind'], 'none')

    def test_parent_from_file_and_stdin(self):
        rc, out, err = self.run_cli('--heads-file', self.heads, 'parent', 'vertex/1.2.x')
        self.assertEqual((rc, out), (0, 'vertex/1.x.x'), err)
        rc, out, err = self.run_cli('parent', 'vertex/1.2.x', stdin=LS_REMOTE)
        self.assertEqual((rc, out), (0, 'vertex/1.x.x'), err)
        rc, out, err = self.run_cli('parent', 'vertex/1.2.x', stdin='vertex/main\n')
        self.assertEqual((rc, out), (0, 'vertex/main'), err)

    def test_children(self):
        rc, out, err = self.run_cli('--heads-file', self.heads, 'children', 'vertex/main')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), ['vertex/1.x.x', 'vertex/2.0.x'])

    def test_errors_exit_1(self):
        rc, out, err = self.run_cli('--heads-file', self.heads, 'parent', 'vertex/main')
        self.assertEqual(rc, 1)
        self.assertIn('no parent', err)
        rc, out, err = self.run_cli('--heads-file', self.heads, 'children', 'ota-dev')
        self.assertEqual(rc, 1)
        self.assertIn('not a release line', err)

    def test_missing_config(self):
        os.remove(os.path.join(self.tmp.name, 'release/config.json'))
        rc, out, err = self.run_cli('parse', 'vertex/main')
        self.assertEqual(rc, 1)
        self.assertIn('release/config.json', err)


class BareCliTests(unittest.TestCase):
    """product_prefix false in release/config.json switches the CLI to bare names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, 'release'))
        with open(os.path.join(self.tmp.name, 'release/config.json'), 'w') as f:
            json.dump({'products': BARE, 'product_prefix': False}, f)
        self.heads = os.path.join(self.tmp.name, 'heads.txt')
        with open(self.heads, 'w') as f:
            f.write(BARE_LS_REMOTE)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=None):
        r = subprocess.run([sys.executable, lines.__file__, '--root', self.tmp.name, *args],
                           input=stdin or '', capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    def test_parse(self):
        rc, out, err = self.run_cli('parse', 'refs/heads/1.2.x')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out), {'product': 'ds', 'kind': 'patch', 'version_prefix': '1.2.'})
        rc, out, err = self.run_cli('parse', 'main')
        self.assertEqual(json.loads(out), {'product': 'ds', 'kind': 'main', 'version_prefix': ''})
        for ref in ('dev/main', '1foo.x.x', '1.x.x/x', 'ds/main'):
            rc, out, err = self.run_cli('parse', ref)
            self.assertEqual(rc, 0, err)
            self.assertEqual(json.loads(out), lines.NONE, ref)

    def test_parent_and_children(self):
        rc, out, err = self.run_cli('--heads-file', self.heads, 'parent', '1.2.x')
        self.assertEqual((rc, out), (0, '1.x.x'), err)
        rc, out, err = self.run_cli('parent', '1.2.x', stdin='main\n')
        self.assertEqual((rc, out), (0, 'main'), err)
        rc, out, err = self.run_cli('--heads-file', self.heads, 'children', 'main')
        self.assertEqual(json.loads(out), ['1.x.x', '2.0.x'])
        rc, out, err = self.run_cli('--heads-file', self.heads, 'parent', 'main')
        self.assertEqual(rc, 1)
        self.assertIn('no parent', err)
        rc, out, err = self.run_cli('--heads-file', self.heads, 'children', 'dev/main')
        self.assertEqual(rc, 1)
        self.assertIn('not a release line', err)

    def test_two_products_without_prefix_refused(self):
        with open(os.path.join(self.tmp.name, 'release/config.json'), 'w') as f:
            json.dump({'products': ['ds', 'portal'], 'product_prefix': False}, f)
        rc, out, err = self.run_cli('parse', 'main')
        self.assertEqual(rc, 1)
        self.assertIn('exactly one product', err)


if __name__ == '__main__':
    unittest.main()
