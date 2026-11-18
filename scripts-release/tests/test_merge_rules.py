"""Tests for merge_rules.py: the splice, the pins rule, never_auto (ENG-2234).

The same file in ds-tegra-distro, sensor-portal and ds. It imports nothing but
merge_rules. Conflicts are made by a real `git merge` in a throwaway repo, so
the rules read the index and the work tree as merge-up.sh leaves them. The
real-conflict cases under fixtures/merge-up/ run where that folder has them and
are skipped where it does not. No network: the compare API is a function the
test passes, or a stub `gh` on PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import merge_rules as mr  # noqa: E402

FIXTURES = os.path.join(HERE, 'fixtures', 'merge-up')
ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x',
           GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
ACT = {'splice': 'act', 'pins': 'act'}


def git(cwd, *args, check=True, data=None):
    r = subprocess.run(['git', '-C', cwd, *args], capture_output=True, env=ENV, input=data)
    if check and r.returncode != 0:
        raise AssertionError(f'git {" ".join(args)}: {r.stderr.decode()}')
    return r


def lines(*items):
    """b'a\\nb\\n' from 'a', 'b'."""
    return ''.join(i + '\n' for i in items).encode()


def L(text):
    return [l + b'\n' for l in text.encode().split()]


class Repo:
    """A repo with one commit per version of each file, and the merge of child into parent open."""

    def __init__(self, case, files):
        """files: {path: (base, parent, child)}, each bytes, or None for a version without the file."""
        self.top = tempfile.mkdtemp(prefix='merge-rules-')
        case.addCleanup(shutil.rmtree, self.top, True)
        subprocess.run(['git', 'init', '-q', '-b', 'base', self.top], check=True, env=ENV)
        self.put({p: v[0] for p, v in files.items()}, 'base', keep=True)
        git(self.top, 'checkout', '-q', '-b', 'child')
        self.put({p: v[2] for p, v in files.items()}, 'child')
        git(self.top, 'checkout', '-q', '-b', 'parent', 'base')
        self.put({p: v[1] for p, v in files.items()}, 'parent')
        self.merge = git(self.top, '-c', 'merge.conflictStyle=zdiff3', 'merge', '--no-ff', '--no-commit', 'child', check=False)

    def put(self, files, msg, keep=False):
        for path, data in files.items():
            full = os.path.join(self.top, path)
            if data is None:
                if os.path.exists(full):
                    os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(data)
        if keep:
            with open(os.path.join(self.top, '.keep'), 'w') as f:
                f.write('keep\n')
        git(self.top, 'add', '-A')
        git(self.top, 'commit', '-q', '--allow-empty', '-m', msg)

    def read(self, path):
        with open(os.path.join(self.top, path), 'rb') as f:
            return f.read()

    def unmerged(self):
        return mr.unmerged_paths(self.top)

    def staged(self, path):
        return git(self.top, 'show', f':{path}', check=False).stdout


# One conflict of each shape. Each is (base, parent, child) as lists of line names.
ADJACENT = ('a b c d e', 'a B c d e', 'a b C d e')                 # neighbouring lines, one each
DELETE_BELOW = ('a b c d e', 'a B x c d e', 'a b e')               # the meta-dsensos #176 shape
INSERT_AFTER_SAME_SIZE = ('a b c', 'a b n1 n2 c', 'a B c')         # the ds-tegra-distro #137 shape
INSERT_BEFORE_SAME_SIZE = ('a b c', 'a n1 b c', 'a B c')
INSERT_AFTER_DELETION = ('a b c d', 'a b c n1 d', 'a d')
OVERLAP = ('a b c', 'a B c', 'a X c')
SAME_POINT = ('a b', 'a n1 b', 'a n2 b')
INSIDE = ('a b c d', 'a b n1 c d', 'a X d')
END_GROWS = ('a b c', 'a b n1 c', 'a B B2 c')
START_GROWS = ('a b c', 'a n1 b c', 'a B B2 c')
# Text both sides wrote (the owner's decision of 2026-11-17: keep it once). One line replaces
# a line with `new`, the other adds `new` next to that line.
SAME_AFTER = ('a old c', 'a new c', 'a old new c')                 # a migration finished on one line, half done on the other
SAME_BEFORE = ('a old c', 'a new c', 'a new old c')
SAME_SHRINK = ('x a b c', 'x ab c', 'x a b ab c')
SAME_GROWS = ('a b c', 'a b B2 c', 'a B B2 c')                     # next to an edit that adds lines
SAME_IN_PART = ('a b c', 'a b n1 n2 c', 'a n1 c')                  # only some of the inserted lines are the other side's


def region(shape):
    """The single conflict region git makes of a shape: (ours, base, theirs) line lists."""
    base, ours, theirs = (lines(*s.split()) for s in shape)
    parts = [p for p in mr.regions(base, ours, theirs) if p[0] == 'conflict']
    assert len(parts) == 1, parts
    return parts[0][1], parts[0][2], parts[0][3]


def merged(shape):
    """The whole file after the splice, or the refusal."""
    base, ours, theirs = (lines(*s.split()) for s in shape)
    out = []
    for p in mr.regions(base, ours, theirs):
        if p[0] == 'clean':
            out += p[1]
        else:
            got, why = mr.splice_region(p[1], p[2], p[3])
            if why:
                return why
            out += got
    return b''.join(out).decode().split()


class SpliceRegionTests(unittest.TestCase):
    """The splice on one region: what resolves and each refusal of plan 5.3."""

    def test_adjacent_edits_resolve_in_base_order(self):
        self.assertEqual(merged(ADJACENT), 'a B C d e'.split())
        self.assertEqual(merged(DELETE_BELOW), 'a B x e'.split())

    def test_it_is_symmetric(self):
        for shape in (ADJACENT, DELETE_BELOW, INSERT_AFTER_SAME_SIZE, INSERT_BEFORE_SAME_SIZE, INSERT_AFTER_DELETION):
            base, ours, theirs = shape
            self.assertEqual(merged(shape), merged((base, theirs, ours)), shape)

    def test_overlapping_edits_refuse(self):
        self.assertEqual(merged(OVERLAP), 'overlap')
        self.assertEqual(merged(('a b c d', 'a X Y d', 'a b Z d')), 'overlap')

    def test_two_insertions_at_one_point_refuse(self):
        self.assertEqual(merged(SAME_POINT), 'same-point')
        # an add/add region: no base lines at all
        self.assertEqual(mr.splice_region(L('x'), [], L('y')), (None, 'same-point'))

    def test_an_insertion_inside_the_other_sides_range_refuses(self):
        self.assertEqual(merged(INSIDE), 'inside')
        self.assertEqual(merged((INSIDE[0], INSIDE[2], INSIDE[1])), 'inside')

    def test_an_insertion_right_after_an_edit_that_adds_lines_refuses(self):
        self.assertEqual(merged(END_GROWS), 'end')
        self.assertEqual(merged((END_GROWS[0], END_GROWS[2], END_GROWS[1])), 'end')

    def test_an_insertion_right_before_an_edit_that_adds_lines_refuses(self):
        self.assertEqual(merged(START_GROWS), 'start')
        self.assertEqual(merged((START_GROWS[0], START_GROWS[2], START_GROWS[1])), 'start')

    def test_an_insertion_at_the_boundary_of_an_edit_that_adds_no_line_resolves(self):
        # after a pure deletion, and on either side of a line-for-line replacement
        self.assertEqual(merged(INSERT_AFTER_DELETION), 'a n1 d'.split())
        self.assertEqual(merged(INSERT_AFTER_SAME_SIZE), 'a B n1 n2 c'.split())
        self.assertEqual(merged(INSERT_BEFORE_SAME_SIZE), 'a n1 B c'.split())

    def test_clash_names_every_refusal(self):
        ins = lambda p, *new: (p, p, list(new))
        self.assertEqual(mr.clash(ins(1, 'x'), ins(1, 'y')), 'same-point')
        self.assertIsNone(mr.clash(ins(1, 'x'), ins(2, 'y')))
        self.assertEqual(mr.clash(ins(2, 'x'), (1, 3, ['y'])), 'inside')
        self.assertEqual(mr.clash(ins(3, 'x'), (1, 3, ['y', 'z', 'w'])), 'end')
        self.assertEqual(mr.clash(ins(1, 'x'), (1, 3, ['y', 'z', 'w'])), 'start')
        self.assertIsNone(mr.clash(ins(3, 'x'), (1, 3, [])))
        self.assertIsNone(mr.clash(ins(3, 'x'), (1, 3, ['y', 'z'])))
        self.assertEqual(mr.clash((0, 2, ['x']), (1, 3, ['y'])), 'overlap')
        self.assertIsNone(mr.clash((0, 1, ['x']), (1, 3, ['y'])))
        self.assertEqual(sorted(mr.REFUSALS), ['absorbed', 'add-add', 'end', 'hunks', 'inside', 'overlap', 'same-point', 'start', 'twice'])

    def test_an_insertion_that_repeats_the_other_sides_adjoining_text_is_kept_once(self):
        for shape, want in ((SAME_AFTER, 'a new c'), (SAME_BEFORE, 'a new c'), (SAME_SHRINK, 'x ab c'), (SAME_GROWS, 'a B B2 c')):
            base, ours, theirs = shape
            self.assertEqual(merged(shape), want.split(), shape)
            self.assertEqual(merged((base, theirs, ours)), want.split(), shape)
            o, b, t = region(shape)
            self.assertEqual(mr.splice(o, b, t)[1:], (None, 1), shape)

    def test_same_text_tells_whole_blocks_at_the_touching_end_only(self):
        ins = lambda p, *new: (p, p, [x.encode() for x in new])
        rep = lambda a, b, *new: (a, b, [x.encode() for x in new])
        self.assertTrue(mr.same_text(ins(3, 'y', 'z'), rep(1, 3, 'x', 'y', 'z')))    # the last lines, where it ends
        self.assertTrue(mr.same_text(ins(1, 'x', 'y'), rep(1, 3, 'x', 'y', 'z')))    # the first lines, where it starts
        self.assertFalse(mr.same_text(ins(3, 'x', 'y'), rep(1, 3, 'x', 'y', 'z')))   # the first lines, at the wrong end
        self.assertFalse(mr.same_text(ins(1, 'y', 'z'), rep(1, 3, 'x', 'y', 'z')))
        self.assertFalse(mr.same_text(ins(3, 'z', 'w'), rep(1, 3, 'x', 'y', 'z')))   # one line more than the other side wrote
        self.assertFalse(mr.same_text(ins(3, 'w', 'x', 'y', 'z'), rep(1, 3, 'x', 'y', 'z')))
        self.assertFalse(mr.same_text(ins(2, 'y'), rep(1, 3, 'x', 'y', 'z')))        # not next to it
        self.assertFalse(mr.same_text(ins(3, 'x'), ins(3, 'x')))                     # two insertions are the same edit, or same-point
        self.assertFalse(mr.same_text(ins(3), rep(1, 3)))

    def test_the_same_edit_on_both_sides_is_applied_once(self):
        # both replaced `a` by `A`; next to it each changed a line of its own
        self.assertEqual(mr.splice(L('A x B c'), L('a x b c'), L('A x b C')), (L('A x B C'), None, 1))
        # both inserted `n` at the same point, and that is all they share
        self.assertEqual(mr.splice(L('a n b C'), L('a b c'), L('a n B c')), (L('a n B C'), None, 1))
        # the same insertion with one line more on one side is not the same edit
        self.assertEqual(mr.splice(L('a n b'), L('a b'), L('a n m b')), (None, 'same-point', 0))
        # an edit that only overlaps the other side's is still refused
        self.assertEqual(mr.splice(L('A B c'), L('a b c'), L('A b C'))[:2], (None, 'overlap'))

    def test_sameness_in_part_is_refused_not_deduplicated(self):
        self.assertEqual(merged(SAME_IN_PART), 'twice')
        self.assertEqual(merged((SAME_IN_PART[0], SAME_IN_PART[2], SAME_IN_PART[1])), 'twice')
        # each side added the same line at a place of its own
        self.assertEqual(mr.splice(L('a x b c'), L('a b c'), L('a b x c')), (None, 'twice', 0))
        # a line both sides already hold twice may be there twice
        self.assertEqual(mr.splice(L('x A x b'), L('x a x b'), L('x a x B')), (L('x A x B'), None, 0))

    def test_one_replacement_never_absorbs_two_insertions(self):
        # one side wrote `L` before and after `x`, the other replaced `x` with `L`: keeping the
        # text once would leave one `L` where a side wrote two (Dexter, 2026-11-18: no lost lines)
        self.assertEqual(mr.splice(L('L x L'), L('x'), L('L')), (None, 'absorbed', 0))
        self.assertEqual(mr.splice(L('L'), L('x'), L('L x L')), (None, 'absorbed', 0))
        # the two insertions claim different lines of a two-line replacement: nothing is lost
        self.assertEqual(mr.splice(L('P x Q'), L('x'), L('P Q')), (L('P Q'), None, 2))
        # one insertion next to the replacement is still kept once
        self.assertEqual(mr.splice(L('x L'), L('x'), L('L')), (L('L'), None, 1))

    def test_no_resolution_holds_a_line_more_often_than_either_side(self):
        # every three-way of up to three lines over a small alphabet, both ways round
        import collections
        import itertools
        texts = [list(t) for n in range(4) for t in itertools.product(L('a b n'), repeat=n)]
        resolved = 0
        for base in texts:
            for ours, theirs in itertools.combinations(texts, 2):
                got, why, kept = mr.splice(ours, base, theirs)
                self.assertEqual((got, why, kept), mr.splice(theirs, base, ours), (base, ours, theirs))
                if got is None:
                    continue
                resolved += 1
                have = collections.Counter(ours), collections.Counter(theirs)
                for line, n in collections.Counter(got).items():
                    self.assertLessEqual(n, max(have[0][line], have[1][line]), (base, ours, theirs, got))
        self.assertGreater(resolved, 1000)

    def test_regions_alternate_and_survive_marker_like_text(self):
        base = lines('a', '<<<<<<< not a marker', '=======', 'b', 'c')
        parts = mr.regions(base, base.replace(b'b\n', b'B\n'), base.replace(b'b\n', b'X\n'))
        self.assertEqual([p[0] for p in parts], ['clean', 'conflict', 'clean'])
        self.assertEqual(parts[1][1:], ([b'B\n'], [b'b\n'], [b'X\n']))
        self.assertIn(b'=======\n', parts[0][1])


def shape_files(shape, path='f.txt'):
    return {path: tuple(lines(*s.split()) for s in shape)}


class SpliceFileTests(unittest.TestCase):
    """resolve_conflicts on real merges: what is written, staged and left."""

    def test_an_adjacent_edit_is_resolved_and_staged(self):
        repo = Repo(self, shape_files(DELETE_BELOW))
        self.assertEqual(repo.unmerged(), ['f.txt'])
        self.assertIn(b'<<<<<<< ', repo.read('f.txt'))
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertEqual(repo.read('f.txt'), lines(*'a B x e'.split()))
        self.assertEqual(repo.unmerged(), [])
        self.assertEqual(repo.staged('f.txt'), repo.read('f.txt'))
        self.assertEqual((report['resolved'], report['remaining']), (['f.txt'], []))
        self.assertEqual(report['files'][0]['outcome'], 'resolved')
        self.assertEqual(report['trailers'], ['Merge-Up-Rule: splice files=1 regions=1'])
        self.assertEqual(mr.log_lines(report), ['resolved f.txt (1 of 1 conflict); no markers left'])

    def test_text_both_sides_wrote_is_kept_once_and_the_region_is_named(self):
        for shape in (SAME_AFTER, (SAME_AFTER[0], SAME_AFTER[2], SAME_AFTER[1])):
            repo = Repo(self, shape_files(shape))
            self.assertEqual(repo.unmerged(), ['f.txt'])
            report = mr.resolve_conflicts(repo.top, ACT)
            self.assertEqual(repo.read('f.txt'), lines(*'a new c'.split()))
            self.assertEqual(repo.unmerged(), [])
            item = report['files'][0]['items'][0]
            self.assertEqual((item['same'], item['reason']), (1, 'adjacent edits, both applied in base order; same text on both sides, kept once'))
            self.assertEqual(mr.log_lines(report), ['resolved f.txt (1 of 1 conflict); no markers left; conflict 1: same text on both sides, kept once (1 line)'])
            self.assertIn('Conflict 1: same text on both sides, kept once (1 line) |', mr.body_section(report))
        # shadow says it too, and a region with nothing kept once says nothing of the kind
        repo = Repo(self, shape_files(SAME_AFTER))
        self.assertEqual(mr.log_lines(mr.resolve_conflicts(repo.top, {'splice': 'shadow'})),
                         ['shadow, nothing changes: would resolve f.txt (1 of 1 conflict); conflict 1: same text on both sides, kept once (1 line)'])
        repo = Repo(self, shape_files(ADJACENT))
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertEqual(report['files'][0]['items'][0]['same'], 0)
        self.assertNotIn('kept once', mr.body_section(report) + ''.join(mr.log_lines(report)))

    def test_each_refusal_leaves_the_file_as_git_wrote_it(self):
        for shape, why in ((OVERLAP, 'overlap'), (SAME_POINT, 'same-point'), (INSIDE, 'inside'), (END_GROWS, 'end'), (START_GROWS, 'start'),
                           (SAME_IN_PART, 'twice')):
            with self.subTest(why=why):
                repo = Repo(self, shape_files(shape))
                before = repo.read('f.txt')
                report = mr.resolve_conflicts(repo.top, ACT)
                self.assertEqual(repo.read('f.txt'), before)
                self.assertEqual(repo.unmerged(), ['f.txt'])
                f = report['files'][0]
                self.assertEqual((f['outcome'], [i['reason'] for i in f['items']]), ('unresolved', [mr.REFUSALS[why]]))
                self.assertEqual((report['remaining'], report['trailers']), (['f.txt'], []))
                self.assertIn(mr.REFUSALS[why], mr.log_lines(report)[0])

    def test_add_add_refuses(self):
        repo = Repo(self, {'new.txt': (None, lines('p1', 'p2'), lines('c1', 'c2'))})
        before = repo.read('new.txt')
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertEqual(report['files'][0]['refused'], mr.REFUSALS['add-add'])
        self.assertEqual(report['files'][0]['outcome'], 'refused')
        self.assertEqual(repo.read('new.txt'), before)
        self.assertEqual(report['remaining'], ['new.txt'])

    def test_modify_delete_binary_and_symlink_refuse(self):
        repo = Repo(self, {'gone.txt': (lines('a', 'b'), None, lines('a', 'B')),
                           'bin.dat': (b'a\0b\n', b'a\0P\n', b'a\0C\n')})
        report = mr.resolve_conflicts(repo.top, ACT)
        why = {f['path']: f['refused'] for f in report['files']}
        self.assertIn('modify/delete', why['gone.txt'])
        self.assertEqual(why['bin.dat'], 'a binary file')
        self.assertEqual(sorted(report['remaining']), ['bin.dat', 'gone.txt'])

    def test_a_file_with_marker_like_lines_or_no_final_newline_refuses(self):
        marker = ('a\n<<<<<<< mine\nb\nc\n', 'a\n<<<<<<< mine\nB\nc\n', 'a\n<<<<<<< mine\nb\nC\n')
        nonl = ('a\nb\nc\nd', 'a\nB\nc\nd', 'a\nb\nC\nd')
        repo = Repo(self, {'m.txt': tuple(s.encode() for s in marker), 'n.txt': tuple(s.encode() for s in nonl)})
        before = {p: repo.read(p) for p in ('m.txt', 'n.txt')}
        report = mr.resolve_conflicts(repo.top, ACT)
        why = {f['path']: f['refused'] for f in report['files']}
        self.assertIn('look like conflict markers', why['m.txt'])
        self.assertIn('does not end with a newline', why['n.txt'])
        self.assertEqual({p: repo.read(p) for p in before}, before)

    def test_a_crlf_file_refuses_and_the_other_files_are_still_resolved(self):
        # git merge-file ends its marker lines with CRLF in such a file, so its regions cannot
        # be read. That is a refusal of the one file, not a failure of the whole evaluation.
        crlf = tuple(d.replace(b'\n', b'\r\n') for d in shape_files(DELETE_BELOW)['f.txt'])
        for modes in (ACT, {'splice': 'shadow'}):
            with self.subTest(modes=modes):
                repo = Repo(self, dict(shape_files(DELETE_BELOW, 'good.txt'), **{'win.txt': crlf}))
                before = repo.read('win.txt')
                self.assertIn(b'<<<<<<< ', before)
                report = mr.resolve_conflicts(repo.top, modes)
                by_path = {f['path']: f for f in report['files']}
                self.assertEqual(by_path['win.txt']['refused'], 'a version of the file has CRLF line endings')
                self.assertEqual(repo.read('win.txt'), before)
                self.assertIn('win.txt', report['remaining'])
                if modes is ACT:
                    self.assertEqual((report['resolved'], repo.unmerged()), (['good.txt'], ['win.txt']))
                else:
                    self.assertIn('shadow, nothing changes: would resolve good.txt (1 of 1 conflict)', mr.log_lines(report))
                self.assertIn('no rule applies to win.txt: a version of the file has CRLF line endings', mr.log_lines(report))

    def test_regions_that_cannot_be_read_refuse_the_one_file(self):
        repo = Repo(self, dict(shape_files(DELETE_BELOW, 'good.txt'), **shape_files(DELETE_BELOW, 'odd.txt')))
        real = mr.regions

        def regions(base, ours, theirs, calls=[]):
            calls.append(1)
            if len(calls) == 2:   # the second file
                raise mr.Error('git merge-file reports 1 conflict(s), 0 were read')
            return real(base, ours, theirs)
        mr.regions = regions
        self.addCleanup(setattr, mr, 'regions', real)
        before = repo.read('odd.txt')
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertEqual((report['resolved'], report['remaining']), (['good.txt'], ['odd.txt']))
        self.assertEqual(report['files'][1]['refused'], "git's merge of the three versions could not be read (git merge-file reports 1 conflict(s), 0 were read)")
        self.assertEqual(repo.read('odd.txt'), before)

    def test_a_partial_file_keeps_zdiff3_markers_for_the_rest(self):
        # two conflicts far apart: the first is adjacent edits, the second overlaps
        pad = [f'pad{i}' for i in range(8)]
        base = lines('a', 'b', 'c', *pad, 'x', 'y', 'z')
        parent = lines('a', 'B', 'c', *pad, 'x', 'common', 'P', 'z')
        child = lines('a', 'b', 'C', *pad, 'x', 'common', 'Q', 'z')
        repo = Repo(self, {'f.txt': (base, parent, child)})
        git_wrote = repo.read('f.txt')
        self.assertEqual(git_wrote.count(b'<<<<<<< '), 2)
        report = mr.resolve_conflicts(repo.top, ACT)
        f = report['files'][0]
        self.assertEqual((f['outcome'], f['regions'], f['n_write']), ('partial', 2, 1))
        now = repo.read('f.txt')
        self.assertEqual(now.count(b'<<<<<<< '), 1)
        self.assertTrue(now.startswith(lines('a', 'B', 'C', *pad)))
        # the second conflict is byte for byte what git wrote for it: zdiff3, with the line both
        # sides added moved out of the markers, the base between them, and git's labels
        tail = git_wrote[git_wrote.index(b'x\n'):]
        self.assertTrue(now.endswith(tail))
        self.assertIn(b'x\ncommon\n<<<<<<< HEAD\nP\n||||||| ', tail)
        self.assertIn(b'\ny\n=======\nQ\n>>>>>>> child\nz\n', tail)
        # still a conflict for git and for the PR
        self.assertEqual(repo.unmerged(), ['f.txt'])
        self.assertEqual(report['remaining'], ['f.txt'])
        self.assertEqual(report['trailers'], ['Merge-Up-Rule: splice files=1 regions=1'])
        self.assertIn('resolved 1 of 2 conflicts in f.txt; the rest keeps its markers (conflict 2: ' + mr.REFUSALS['overlap'] + ')',
                      mr.log_lines(report))

    def test_the_text_outside_the_regions_is_gits_own(self):
        # the regions written back with none resolved are git merge-file --zdiff3, byte for byte
        pad = [f'pad{i}' for i in range(8)]
        base = lines('a', 'b', 'c', *pad, 'x', 'y', 'z')
        ours = lines('a', 'B', 'c', *pad, 'x', 'common', 'P', 'z')
        theirs = lines('a', 'b', 'C', *pad, 'x', 'common', 'Q', 'z')
        names = (b'o', b'b', b't')
        plain = b''.join(b''.join(p[1] if p[0] == 'clean' else mr.marked(p, names)) for p in mr.regions(base, ours, theirs))
        self.assertEqual(plain, mr.merge_file(base, ours, theirs, '--zdiff3', '-L', 'o', '-L', 'b', '-L', 't')[1])
        # a region reader that loses a line is caught: the file is refused, not rewritten
        repo = Repo(self, {'f.txt': (base, ours, theirs)})
        before, real = repo.read('f.txt'), mr.regions

        def lossy(*a):
            parts = real(*a)
            parts[0] = ('clean', parts[0][1][1:])
            return parts
        mr.regions = lossy
        self.addCleanup(setattr, mr, 'regions', real)
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertIn('do not add up to git\'s own merge', report['files'][0]['refused'])
        self.assertEqual(repo.read('f.txt'), before)

    def test_a_file_git_merge_cut_differently_is_refused_whole(self):
        # The review's reproduction: git merge leaves two hunks, the first a true overlap (the
        # parent deleted lines the child changed). git merge-file cuts one region, in which
        # the edits look adjacent, so the splice used to write the file without markers.
        base, parent, child = 'b a a a b b _ c r b b', 'a a b _ c _ b b', 'b a a b b _ c r e b r'
        files = {'f.txt': tuple(lines(*v.split()).replace(b'_\n', b'\n').replace(b'r\n', b'return x\n') for v in (base, parent, child)),
                 'g.txt': shape_files(DELETE_BELOW)['f.txt']}
        for modes in (ACT, {'splice': 'shadow'}):
            repo = Repo(self, files)
            before = repo.read('f.txt')
            self.assertEqual(before.count(b'\n<<<<<<< ') + before.startswith(b'<<<<<<< '), 2)
            three = [repo_stage[1] for _, repo_stage in sorted(mr.stages(repo.top, 'f.txt').items())]
            parts = [p for p in mr.regions(*three) if p[0] == 'conflict']
            self.assertEqual(len(parts), 1)
            self.assertIsNotNone(mr.splice(*parts[0][1:])[0])   # the region alone would resolve
            report = mr.resolve_conflicts(repo.top, modes)
            f = [x for x in report['files'] if x['path'] == 'f.txt'][0]
            self.assertEqual((f['outcome'], f['would'], f['items']), ('refused', 'nothing', []))
            self.assertEqual(f['refused'], mr.REFUSALS['hunks'] + ' (2 hunks in the work tree, 1 region)')
            self.assertEqual(repo.read('f.txt'), before)
            self.assertIn('f.txt', repo.unmerged())
            self.assertIn(f'no rule applies to f.txt: {f["refused"]}', mr.log_lines(report))
            # the other file of the merge is still the rules' to resolve
            self.assertEqual(report['resolved'], ['g.txt'] if modes is ACT else [])

    def test_the_regions_must_be_the_work_tree_conflicts_byte_for_byte(self):
        # the same number of hunks on other lines is no better: here the work tree copy holds
        # one conflict, as the regions do, but not the one git merge-file cuts
        repo = Repo(self, shape_files(DELETE_BELOW))
        git_wrote = repo.read('f.txt')
        other = git_wrote.replace(b'<<<<<<< HEAD\n', b'<<<<<<< HEAD\nmoved\n')
        self.assertNotEqual(other, git_wrote)
        with open(os.path.join(repo.top, 'f.txt'), 'wb') as f:
            f.write(other)
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertEqual(report['files'][0]['refused'], mr.REFUSALS['hunks'] + ' (1 hunk in the work tree, 1 region, on other lines)')
        self.assertEqual((repo.read('f.txt'), repo.unmerged(), report['resolved']), (other, ['f.txt'], []))
        # a merge made with another conflict style is such a file too
        repo = Repo(self, shape_files(DELETE_BELOW))
        git(repo.top, 'checkout', '--conflict=merge', '--', 'f.txt')
        report = mr.resolve_conflicts(repo.top, ACT)
        self.assertTrue(report['files'][0]['refused'].startswith(mr.REFUSALS['hunks']), report['files'][0]['refused'])
        self.assertEqual(repo.unmerged(), ['f.txt'])

    def test_shadow_and_off_write_nothing(self):
        for mode in ('shadow', 'off'):
            with self.subTest(mode=mode):
                repo = Repo(self, shape_files(DELETE_BELOW))
                before = repo.read('f.txt')
                report = mr.resolve_conflicts(repo.top, {'splice': mode})
                self.assertEqual(repo.read('f.txt'), before)
                self.assertEqual(repo.unmerged(), ['f.txt'])
                self.assertEqual((report['resolved'], report['remaining'], report['trailers']), ([], ['f.txt'], []))
                self.assertEqual(mr.body_section(report), '')
                if mode == 'shadow':
                    self.assertEqual(mr.log_lines(report), ['shadow, nothing changes: would resolve f.txt (1 of 1 conflict)'])
                else:
                    self.assertEqual(report['files'][0]['items'][0]['reason'], 'no rule is on for it')

    def test_a_dry_run_evaluates_and_writes_nothing(self):
        repo = Repo(self, shape_files(DELETE_BELOW))
        before = repo.read('f.txt')
        report = mr.resolve_conflicts(repo.top, ACT, write=False)
        self.assertEqual(repo.read('f.txt'), before)
        self.assertEqual(repo.unmerged(), ['f.txt'])
        # the report says what a real run does
        self.assertEqual((report['resolved'], report['remaining']), (['f.txt'], []))
        self.assertEqual(mr.log_lines(report), ['would have resolved f.txt (1 of 1 conflict); no markers left'])

    def test_a_failure_after_the_writes_puts_every_file_back(self):
        # one.txt is resolved and staged, two.txt resolved in part; then something fails (the
        # caller's outputs, a later write). Both files are as git wrote them again, byte for
        # byte with the merge's own labels, and unmerged: the caller goes on without the rules.
        pad = [f'pad{i}' for i in range(8)]
        files = dict(shape_files(DELETE_BELOW, 'one.txt'),
                     **{'two.txt': (lines('a', 'b', 'c', *pad, 'x', 'y', 'z'), lines('a', 'B', 'c', *pad, 'x', 'common', 'P', 'z'),
                                    lines('a', 'b', 'C', *pad, 'x', 'common', 'Q', 'z'))})
        for how in ('after', 'add'):
            with self.subTest(how=how):
                repo = Repo(self, files)
                before = {p: repo.read(p) for p in ('one.txt', 'two.txt')}
                # the repo's own conflict style is not the merge's: git checkout --merge alone would not do
                git(repo.top, 'config', 'merge.conflictStyle', 'merge')
                self.assertIn(b'<<<<<<< HEAD\n', before['one.txt'])
                self.assertIn(b'||||||| ', before['one.txt'])
                self.assertIn(b'>>>>>>> child\n', before['one.txt'])
                seen = []

                def after(report):
                    seen.append((list(report['resolved']), repo.unmerged(), repo.read('one.txt') != before['one.txt']))
                    raise OSError(28, 'No space left on device')
                if how == 'after':
                    with self.assertRaises(OSError):
                        mr.resolve_conflicts(repo.top, ACT, after=after)
                    self.assertEqual(seen, [(['one.txt'], ['two.txt'], True)])   # the writes had happened
                else:
                    real = mr._git

                    def failing(git_dir, *args):
                        if args[0] == 'add':
                            raise mr.Error('git add: the index is locked')
                        return real(git_dir, *args)
                    mr._git = failing
                    self.addCleanup(setattr, mr, '_git', real)
                    with self.assertRaises(mr.Error):
                        mr.resolve_conflicts(repo.top, ACT)
                    mr._git = real
                self.assertEqual({p: repo.read(p) for p in before}, before)
                self.assertEqual(repo.unmerged(), ['one.txt', 'two.txt'])

    def test_after_sees_the_report_of_a_run_that_succeeds(self):
        repo = Repo(self, shape_files(DELETE_BELOW))
        seen = []
        report = mr.resolve_conflicts(repo.top, ACT, after=seen.append)
        self.assertIs(seen[0], report)
        self.assertEqual((report['resolved'], repo.unmerged(), report['trailers']), (['f.txt'], [], ['Merge-Up-Rule: splice files=1 regions=1']))

    def test_a_bad_mode_is_an_error(self):
        repo = Repo(self, shape_files(DELETE_BELOW))
        with self.assertRaises(mr.Error):
            mr.resolve_conflicts(repo.top, {'splice': 'on'})

    def test_only_the_paths_given_are_touched(self):
        files = dict(shape_files(DELETE_BELOW, 'one.txt'), **shape_files(DELETE_BELOW, 'two.txt'))
        repo = Repo(self, files)
        report = mr.resolve_conflicts(repo.top, ACT, paths=['one.txt'])
        self.assertEqual(repo.unmerged(), ['two.txt'])
        self.assertEqual([f['path'] for f in report['files']], ['one.txt'])


class NeverAutoTests(unittest.TestCase):
    """No rule writes a never_auto file. Where the splice has an answer it is a patch."""

    def never(self, path):
        return 'secret/**' if path.startswith('secret/') else None

    def test_a_never_auto_file_keeps_its_markers_and_gets_a_candidate(self):
        files = dict(shape_files(DELETE_BELOW, 'secret/s.txt'), **shape_files(DELETE_BELOW, 'open.txt'))
        repo = Repo(self, files)
        before = repo.read('secret/s.txt')
        report = mr.resolve_conflicts(repo.top, ACT, never_auto=self.never)
        self.assertEqual(repo.read('secret/s.txt'), before)
        self.assertEqual(repo.unmerged(), ['secret/s.txt'])
        self.assertEqual((report['resolved'], report['remaining']), (['open.txt'], ['secret/s.txt']))
        f = [x for x in report['files'] if x['path'] == 'secret/s.txt'][0]
        self.assertEqual((f['outcome'], f['never_auto'], f['candidate']), ('never_auto', 'secret/**', True))
        # only the file a rule wrote counts in the trailer
        self.assertEqual(report['trailers'], ['Merge-Up-Rule: splice files=1 regions=1'])
        # the candidate is a patch against the branch's copy that gives what the rule would have written
        self.assertEqual(list(report['candidates']), ['secret/s.txt'])
        git(repo.top, 'apply', '-', data=report['candidates']['secret/s.txt'].encode())
        self.assertEqual(repo.read('secret/s.txt'), repo.read('open.txt'))
        comment = mr.candidate_comment(report)
        self.assertIn('```diff\ndiff --git a/secret/s.txt b/secret/s.txt\n--- a/secret/s.txt\n+++ b/secret/s.txt\n@@ ', comment)
        self.assertIn('`secret/s.txt`: 1 of 1 conflict, no markers left after it', comment)
        self.assertIn('never_auto: secret/s.txt matches secret/**, so no rule writes it; the rules have an answer for 1 of 1 conflict, '
                      'kept as a candidate patch', mr.log_lines(report))
        body = mr.body_section(report)
        self.assertIn('- `secret/s.txt` (matches `secret/**`). What the rules would have written is in a comment on this PR', body)
        self.assertIn('| `open.txt` | splice | 1 of 1 conflict resolved, no markers left', body)

    def test_a_never_auto_file_the_rules_cannot_resolve_has_no_candidate(self):
        repo = Repo(self, shape_files(OVERLAP, 'secret/s.txt'))
        report = mr.resolve_conflicts(repo.top, ACT, never_auto=self.never)
        self.assertEqual(report['candidates'], {})
        self.assertEqual(mr.candidate_comment(report), '')
        self.assertIn('the rules have no answer either', mr.log_lines(report)[0])
        self.assertIn('- `secret/s.txt` (matches `secret/**`)\n', mr.body_section(report))

    def test_in_shadow_there_is_no_candidate_only_a_log_line(self):
        repo = Repo(self, shape_files(DELETE_BELOW, 'secret/s.txt'))
        report = mr.resolve_conflicts(repo.top, {'splice': 'shadow'}, never_auto=self.never)
        self.assertEqual(report['candidates'], {})
        self.assertEqual(mr.log_lines(report), ['never_auto: secret/s.txt matches secret/**, so no rule writes it; '
                                                'shadow: the rules would have an answer for 1 of 1 conflict'])

    def test_a_long_candidate_is_not_pasted(self):
        repo = Repo(self, shape_files(DELETE_BELOW, 'secret/s.txt'))
        report = mr.resolve_conflicts(repo.top, ACT, never_auto=self.never)
        self.assertIn('too long for a comment', mr.candidate_comment(report, limit=10))
        self.assertNotIn('```diff', mr.candidate_comment(report, limit=10))


class VerifyTests(unittest.TestCase):
    """A file the rules resolved in full must still pass the check its name asks for."""

    def test_verify_by_extension(self):
        self.assertIsNone(mr.verify('a.txt', b'anything {\n'))
        self.assertIsNone(mr.verify('a.json', b'{"a": 1}\n'))
        self.assertIn('not valid JSON', mr.verify('a.json', b'{"a": 1,\n'))
        self.assertIsNone(mr.verify('a.py', b'x = 1\n'))
        self.assertIn('does not compile as Python', mr.verify('a.py', b'def f(:\n'))
        self.assertIsNone(mr.verify('a.sh', b'if true; then echo; fi\n'))
        self.assertIn('bash -n fails', mr.verify('a.sh', b'if true; then\n'))
        self.assertEqual(mr.verify('a.txt', b'a\n>>>>>>> x\n'), 'a conflict marker line is left')
        self.assertEqual(mr.verify('a.txt', b'a\n=======\n'), 'a conflict marker line is left')
        self.assertIsNone(mr.verify('a.md', b'Title\n==========\n'))

    @unittest.skipUnless(os.path.exists(os.path.join(SCRIPTS, 'check_yaml.py')), 'this repo has no check_yaml.py')
    def test_yaml_goes_through_check_yaml_where_it_exists(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.assertIn('check_yaml.py could not run', mr.verify('a.yml', b'a: 1\n'))
            return
        self.assertIsNone(mr.verify('a.yml', b'a: 1\nb: 2\n'))
        self.assertIn('check_yaml.py fails', mr.verify('w.yaml', b'a: 1\na: 2\n'))
        self.assertIn('duplicate key', mr.verify('w.yaml', b'a: 1\na: 2\n'))

    def test_yaml_is_not_checked_without_check_yaml(self):
        real = mr.HERE
        mr.HERE = tempfile.mkdtemp(prefix='no-check-yaml-')
        self.addCleanup(shutil.rmtree, mr.HERE, True)
        self.addCleanup(setattr, mr, 'HERE', real)
        self.assertIsNone(mr.verify('a.yml', b'a: 1\na: 2\n'))

    def test_a_result_that_does_not_verify_is_refused_whole(self):
        # the two edits are adjacent, and together they are not valid JSON
        base = '{\n "a": 1,\n "b": 2\n}\n'
        parent = '{\n "a": 10,\n "b": 2\n}\n'
        child = '{\n "a": 1,\n "b": 2,\n}\n'
        repo = Repo(self, {'c.json': (base.encode(), parent.encode(), child.encode())})
        before = repo.read('c.json')
        self.assertIn(b'<<<<<<< ', before)
        report = mr.resolve_conflicts(repo.top, ACT)
        f = report['files'][0]
        self.assertEqual(f['outcome'], 'refused')
        self.assertIn('the result does not verify: not valid JSON', f['refused'])
        self.assertEqual(repo.read('c.json'), before)
        self.assertEqual(repo.unmerged(), ['c.json'])
        self.assertEqual(report['trailers'], [])
        # the same edits in a file with no check resolve
        repo = Repo(self, {'c.txt': (base.encode(), parent.encode(), child.encode())})
        self.assertEqual(mr.resolve_conflicts(repo.top, ACT)['resolved'], ['c.txt'])


PIN_FILE = 'conf/pins.inc'
P1, P2, P3 = 'a' * 40, 'b' * 40, 'c' * 40
PAIRS = [{'rev': 'APP_REV', 'branch': 'APP_BRANCH', 'repo': 'app', 'acts': True},
         {'rev': 'LIB_REV', 'branch': 'LIB_BRANCH', 'repo': 'lib', 'acts': False}]
PINS = {'pin_file': PIN_FILE, 'pairs': PAIRS}


def pin_text(branch, rev, note='', var='APP', lib=P1, extra=''):
    """A pin file: a header, the APP pair with an optional comment above its pin, the LIB pair far below."""
    out = ['# pins', '']
    if var == 'APP':
        out += ([f'APP_BRANCH ?= "{branch}"'] if branch else []) + ([f'# {note}'] if note else []) + [f'APP_REV ?= "{rev}"']
    else:
        out += ['APP_BRANCH ?= "main"', f'APP_REV ?= "{P1}"']
    out += [f'pad{i}' for i in range(8)]
    if var == 'LIB':
        out += [f'LIB_BRANCH ?= "{branch}"'] + ([f'# {note}'] if note else []) + [f'LIB_REV ?= "{rev}"']
    else:
        out += ['LIB_BRANCH ?= "main"', f'LIB_REV ?= "{lib}"']
    return ('\n'.join(out) + '\n' + extra).encode()


class Compare:
    """A compare API: answers by (repo, base, head), records every question."""

    def __init__(self, answers=None):
        self.answers, self.asked = answers or {}, []

    def __call__(self, repo, base, head):
        self.asked.append((repo, base, head))
        return self.answers.get((repo, base, head), ('not-found', 'gh: Not Found (HTTP 404)'))


class PinsTests(unittest.TestCase):
    """R-pins (plan 5.2) on a conflict in the pin file."""

    def run_rule(self, parent, child, answers=None, base=None, modes=ACT, pins=PINS):
        base = pin_text('main', P1) if base is None else base
        self.repo = Repo(self, {PIN_FILE: (base, parent, child)})
        self.before = self.repo.read(PIN_FILE)
        self.assertIn(b'<<<<<<< ', self.before)
        self.compare = Compare(answers)
        self.report = mr.resolve_conflicts(self.repo.top, modes, pins=pins, compare=self.compare)
        f = self.report['files'][0]
        return f, (f['pins'][0] if f['pins'] else None)

    def person(self, code, text):
        """The rule left the region to a person, for this reason, and nothing changed."""
        f, row = self.last
        self.assertEqual(row['code'], code)
        self.assertIn('left to a person: ', row['did'])
        self.assertIn(text, row['did'])
        self.assertEqual(f['outcome'], 'unresolved')
        self.assertEqual(self.repo.read(PIN_FILE), self.before)
        self.assertEqual(self.repo.unmerged(), [PIN_FILE])
        self.assertEqual(self.report['trailers'], [])

    def test_different_branches_keep_the_parent(self):
        parent, child = pin_text('2.x', P2, 'the parent moved to 2.x'), pin_text('main', P3, 'a fix on main')
        f, row = self.run_rule(parent, child)
        self.assertEqual((f['outcome'], row['code'], row['mode']), ('resolved', 'keep-parent', 'act'))
        self.assertEqual(self.repo.read(PIN_FILE), parent)
        self.assertEqual(self.repo.unmerged(), [])
        # no question to the API: the branches settle it
        self.assertEqual(self.compare.asked, [])
        self.assertEqual((row['parent_branch'], row['child_branch'], row['parent_pin'], row['child_pin']), ('2.x', 'main', P2, P3))
        self.assertEqual(self.report['trailers'], [f'Merge-Up-Rule: pins APP_REV=keep-parent parent={P2} child={P3}'])
        body = mr.body_section(self.report)
        self.assertIn('| Variable | Branch (parent / child) | Pin (parent / child) | Compare | What the rule did |', body)
        self.assertIn(f'| `APP_REV` | `2.x` / `main` | `{P2[:8]}` / `{P3[:8]}` | not needed | kept the parent\'s branch and pin: '
                      'the lines follow different branches of app |', body)

    def apart(self, branch, rev):
        """A pin file whose branch line and pin line are far enough apart to merge on their own."""
        return ('\n'.join(['# pins', '', f'APP_BRANCH ?= "{branch}"', '# why', '# this', '# pin', f'APP_REV ?= "{rev}"']
                          + [f'pad{i}' for i in range(8)] + ['LIB_BRANCH ?= "main"', f'LIB_REV ?= "{P1}"']) + '\n').encode()

    def test_a_branch_the_merge_already_moved_outside_the_conflict_goes_to_a_person(self):
        # The child moved to another branch and pinned it; the parent only bumped its pin. Git
        # merges the branch line on its own (the child's value) and conflicts on the pin alone.
        # Keeping the parent's side of that region would pair the child's branch with the
        # parent's pin, under a log line that says the parent's branch and pin were kept.
        f, row = self.last = self.run_rule(self.apart('main', P2), self.apart('2.x', P3), base=self.apart('main', P1))
        self.assertIn(b'APP_BRANCH ?= "2.x"\n', self.before)             # merged clean, outside the markers
        self.assertEqual(self.before.count(b'<<<<<<< '), 1)
        self.assertEqual((row['parent_branch'], row['child_branch']), ('main', '2.x'))
        self.person('outside', 'outside this conflict the merge already holds APP_BRANCH "2.x"')
        self.assertEqual(self.compare.asked, [])
        self.assertNotIn('kept the parent', ' '.join(mr.log_lines(self.report)))
        # the mirror: the conflict is on the branch line, and the child's pin merged outside it
        f, row = self.last = self.run_rule(self.apart('2.x', P1), self.apart('3.x', P3), base=self.apart('main', P1))
        self.assertIn(f'APP_REV ?= "{P3}"\n'.encode(), self.before)
        self.person('outside', f'outside this conflict the merge already holds APP_REV "{P3}"')
        # in shadow the line says the same, and still nothing changes
        f, row = self.run_rule(self.apart('main', P2), self.apart('2.x', P3), base=self.apart('main', P1), modes={'splice': 'shadow', 'pins': 'shadow'})
        self.assertEqual(row['code'], 'outside')
        self.assertEqual(self.repo.read(PIN_FILE), self.before)

    def test_a_pair_that_conflicts_in_two_regions_is_still_decided_whole(self):
        # both lines moved the branch and the pin, far apart: two regions, one answer for both
        parent, child = self.apart('2.x', P2), self.apart('3.x', P3)
        f, row = self.run_rule(parent, child, base=self.apart('main', P1))
        self.assertEqual(self.before.count(b'<<<<<<< '), 2)
        self.assertEqual((f['outcome'], [r['code'] for r in f['pins']]), ('resolved', ['keep-parent', 'keep-parent']))
        self.assertEqual(self.repo.read(PIN_FILE), parent)

    def test_same_branch_and_the_child_is_ahead_takes_the_child(self):
        parent, child = pin_text('main', P2, 'parent bump'), pin_text('main', P3, 'child bump')
        f, row = self.run_rule(parent, child, {('app', P2, P3): ('ahead', ''), ('app', P3, 'main'): ('ahead', '')})
        self.assertEqual((f['outcome'], row['code'], row['compare']), ('resolved', 'take-child', 'ahead'))
        self.assertEqual(self.repo.read(PIN_FILE), child)
        # two questions: is the parent's pin an ancestor of the child's, and is the child's on the branch
        self.assertEqual(self.compare.asked, [('app', P2, P3), ('app', P3, 'main')])
        self.assertEqual(self.report['trailers'], [f'Merge-Up-Rule: pins APP_REV=take-child parent={P2} child={P3}'])
        self.assertIn('took the child\'s pin', mr.log_lines(self.report)[0])

    def test_the_child_pin_at_the_branch_tip_counts_as_on_the_branch(self):
        f, row = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'),
                               {('app', P2, P3): ('ahead', ''), ('app', P3, 'main'): ('identical', '')})
        self.assertEqual((f['outcome'], row['code']), ('resolved', 'take-child'))

    def test_a_parent_pin_that_is_newer_is_never_rolled_back(self):
        parent, child = pin_text('main', P3, 'parent is ahead'), pin_text('main', P2, 'child bump')
        f, row = self.run_rule(parent, child, {('app', P3, P2): ('behind', '')})
        self.assertEqual((f['outcome'], row['code'], row['compare']), ('resolved', 'keep-parent-newer', 'behind'))
        self.assertEqual(self.repo.read(PIN_FILE), parent)
        self.assertIn('never rolls back', row['did'])
        self.assertEqual(self.compare.asked, [('app', P3, P2)])

    def test_diverged_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'), {('app', P2, P3): ('diverged', '')})
        self.person('diverged', 'the two pins have diverged')

    def test_a_404_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'))
        self.person('not-found', 'app does not have the two pins (HTTP 404)')

    def test_an_api_error_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'),
                                  {('app', P2, P3): ('error', 'gh: Bad Gateway (HTTP 502)')})
        self.person('api-error', 'the compare API failed (gh: Bad Gateway (HTTP 502))')

    def test_no_token_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'),
                                  {('app', P2, P3): ('no-token', 'no read-only token for the app repos')})
        self.person('no-token', 'no read-only token for the app repos')

    def test_the_outcomes_that_need_a_person_are_told_apart(self):
        codes = set()
        for answers in ({('app', P2, P3): ('diverged', '')}, {}, {('app', P2, P3): ('error', 'x')}, {('app', P2, P3): ('no-token', 'x')},
                        {('app', P2, P3): ('ahead', ''), ('app', P3, 'main'): ('diverged', '')}):
            f, row = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'), answers)
            codes.add((row['code'], row['did']))
        self.assertEqual(len(codes), 5)
        self.assertEqual(len({c for c, _ in codes}), 5)

    def test_a_child_pin_that_is_not_on_the_branch_goes_to_a_person(self):
        for status in ('behind', 'diverged'):
            self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'),
                                      {('app', P2, P3): ('ahead', ''), ('app', P3, 'main'): (status, '')})
            self.person('not-on-branch', 'the child\'s pin is not on the branch main of app')
        # the branch itself unknown: a 404, named as such
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', P3, 'c'), {('app', P2, P3): ('ahead', '')})
        self.person('not-found', 'app does not have the child\'s pin and the branch main (HTTP 404)')

    def test_a_missing_branch_variable_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('', P3, 'c'))
        self.person('missing-branch', 'the child line does not set APP_BRANCH exactly once')
        self.assertEqual(self.compare.asked, [])
        self.last = self.run_rule(pin_text('', P2, 'p'), pin_text('main', P3, 'c'), base=pin_text('', P1))
        self.person('missing-branch', 'the parent line does not set APP_BRANCH exactly once')

    def test_a_pin_that_is_not_a_full_commit_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'p'), pin_text('main', 'main', 'c'))
        self.person('missing-pin', 'the child line does not set APP_REV to one full commit')

    def test_anything_else_in_the_region_goes_to_a_person(self):
        # an unrelated assignment inside the conflict
        self.last = self.run_rule(pin_text('2.x', P2, 'p'), pin_text('main', P3).replace(b'APP_REV', b'OTHER = "1"\nAPP_REV'))
        self.person('other-lines', 'the conflict holds more than APP_BRANCH and APP_REV (OTHER)')
        # a line that is neither a comment nor an assignment
        self.last = self.run_rule(pin_text('2.x', P2, 'p'), pin_text('main', P3).replace(b'APP_REV', b'require foo.inc\nAPP_REV'))
        self.person('other-lines', 'lines that are neither a comment nor an assignment')

    def test_the_same_pin_on_both_lines_goes_to_a_person(self):
        self.last = self.run_rule(pin_text('main', P2, 'said one way'), pin_text('main', P2, 'said another way'))
        self.person('same-pin', 'both lines pin the same commit')

    def test_a_pair_that_only_logs_changes_nothing(self):
        # the LIB pair is not on the acting list: evaluated, logged, left to a person
        parent, child = pin_text('2.x', P2, 'p', var='LIB'), pin_text('main', P3, 'c', var='LIB')
        base = pin_text('main', P1, var='LIB')
        f, row = self.last = self.run_rule(parent, child, base=base)
        self.assertEqual((row['variable'], row['code'], row['mode'], row['acts']), ('LIB_REV', 'keep-parent', 'shadow', False))
        self.assertEqual(f['outcome'], 'unresolved')
        self.assertEqual(self.repo.read(PIN_FILE), self.before)
        self.assertEqual(self.report['trailers'], [])
        log = mr.log_lines(self.report)
        self.assertIn('pins rule: shadow, nothing changes: LIB_REV (parent 2.x at bbbbbbbb, child main at cccccccc): '
                      'kept the parent\'s branch and pin', log[0])
        self.assertEqual(log[1], 'shadow, nothing changes: would resolve conf/pins.inc (1 of 1 conflict)')
        # it is not in the PR's pins table, which lists what the rule did; the PR says why it was left
        body = mr.body_section(self.report)
        self.assertNotIn('LIB_REV` |', body)
        self.assertIn('- `conf/pins.inc`: conflict 1: the pins rule only logs for LIB_REV (it would have kept the parent\'s branch and pin)', body)

    def test_shadow_logs_and_changes_nothing(self):
        f, row = self.run_rule(pin_text('2.x', P2, 'p'), pin_text('main', P3, 'c'), modes={'splice': 'shadow', 'pins': 'shadow'})
        self.assertEqual((row['code'], row['mode'], f['outcome']), ('keep-parent', 'shadow', 'unresolved'))
        self.assertEqual(self.repo.read(PIN_FILE), self.before)
        self.assertIn('pins rule: shadow, nothing changes: APP_REV', mr.log_lines(self.report)[0])
        self.assertIn('shadow, nothing changes: would resolve conf/pins.inc', mr.log_lines(self.report)[1])

    def test_a_pin_region_is_never_spliced(self):
        # adjacent edits the splice would take: the parent adds a comment, the child moves the pin
        base = pin_text('main', P1)
        parent = base.replace(b'APP_REV', b'# a note\nAPP_REV')
        child = pin_text('main', P3)
        for modes in ({'splice': 'act', 'pins': 'off'}, {'splice': 'act', 'pins': 'shadow'}):
            with self.subTest(modes=modes):
                f, row = self.run_rule(parent, child, base=base, modes=modes)
                self.assertEqual(f['outcome'], 'unresolved')
                self.assertEqual(self.repo.read(PIN_FILE), self.before)
                self.assertNotIn('splice', [i['rule'] for i in f['items']])
        # the same edits in another file, or with no pin configuration, are a plain splice
        f, row = self.run_rule(parent, child, base=base, pins=None)
        self.assertEqual((f['outcome'], row), ('resolved', None))

    def test_another_region_of_the_pin_file_is_spliced(self):
        # the pin conflict is the rule's, an adjacent edit in the padding below is the splice's
        base = pin_text('main', P1)
        parent = pin_text('2.x', P2, 'p').replace(b'pad3\n', b'PAD3\n')
        child = pin_text('main', P3, 'c').replace(b'pad4\n', b'PAD4\n')
        f, row = self.run_rule(parent, child, base=base)
        self.assertEqual((f['outcome'], f['regions']), ('resolved', 2))
        self.assertEqual(sorted(i['rule'] for i in f['items']), ['pins', 'splice'])
        self.assertEqual(self.repo.read(PIN_FILE), parent.replace(b'pad4\n', b'PAD4\n'))
        self.assertEqual(len(self.report['trailers']), 2)

    def test_the_rule_is_inert_without_a_pin_configuration_and_on_other_files(self):
        parent, child = pin_text('2.x', P2, 'p'), pin_text('main', P3, 'c')
        f, row = self.run_rule(parent, child, pins=None)
        self.assertIsNone(row)
        self.assertEqual([i['rule'] for i in f['items']], ['splice'])
        repo = Repo(self, {'other.inc': (pin_text('main', P1), parent, child)})
        report = mr.resolve_conflicts(repo.top, ACT, pins=PINS, compare=Compare())
        self.assertEqual(report['files'][0]['pins'], [])

    def test_assignment_and_variable(self):
        self.assertEqual(mr.assignment(b'A_REV ?= "abc"\n'), ('A_REV', 'abc'))
        self.assertEqual(mr.assignment(b'A = "x"   \r\n'), ('A', 'x'))
        for line in (b'# A ?= "x"\n', b'A ?= x\n', b'A:append = " x"\n', b'A ?= "x" \\\n', b'\n'):
            self.assertIsNone(mr.assignment(line), line)
        self.assertEqual(mr.variable([b'A ?= "1"\n', b'B ?= "2"\n'], 'B'), '2')
        self.assertIsNone(mr.variable([b'A ?= "1"\n'], 'B'))
        self.assertIsNone(mr.variable([b'A ?= "1"\n', b'A ?= "2"\n'], 'A'))


REL_PAIRS = [dict(PAIRS[0], release='APP_RELEASE'), dict(PAIRS[1], release='LIB_RELEASE')]
REL_PINS = {'pin_file': PIN_FILE, 'pairs': REL_PAIRS}


def rel_text(branch, rev, release, gap=3, lib=('main', P1, '1.0.0'), lib_gap=3, extra=''):
    """A pin file laid out like the layer's: per pair its branch, a comment, its pin, `gap`
    comment lines, then its release value (None: no release line). The LIB pair far below."""
    def one(name, b, r, v, g):
        out = [f'{name}_BRANCH ?= "{b}"', f'# about {name}_REV', f'{name}_REV ?= "{r}"'] + [f'# the release at {name}_REV, {i}' for i in range(g)]
        return out + ([f'{name}_RELEASE ?= "{v}"'] if v is not None else [])
    out = ['# pins', ''] + one('APP', branch, rev, release, gap) + [f'pad{i}' for i in range(8)] + one('LIB', *lib, lib_gap) + ['', '# end']
    return ('\n'.join(out) + '\n' + extra).encode()


class ReleaseVariableTests(unittest.TestCase):
    """R-pins with a release variable: the pin, its branch variable and its release value
    (the release at the pin) are one unit, whichever side the rule takes and wherever git put
    the release line."""

    run_rule = PinsTests.run_rule
    person = PinsTests.person
    SAME = {('app', P2, P3): ('ahead', ''), ('app', P3, 'main'): ('ahead', '')}

    def rule(self, parent, child, answers=None, base=None, modes=ACT, pins=REL_PINS):
        base = rel_text('main', P1, '1.0.0') if base is None else base
        self.last = self.run_rule(parent, child, answers, base=base, modes=modes, pins=pins)
        return self.last

    def test_keep_parent_while_git_merged_the_child_release_clean_goes_to_a_person(self):
        # The parent moved to another app branch; the child bumped its pin and its release
        # value. Git conflicts on the pin and merges the child's release line on its own.
        # Keeping the parent's region would stamp the parent's pin with the child's release.
        f, row = self.rule(rel_text('2.x', P2, '1.0.0'), rel_text('main', P3, '1.1.0'))
        self.assertIn(b'APP_RELEASE ?= "1.1.0"\n', self.before)   # merged clean, outside the markers
        self.assertEqual(self.before.count(b'<<<<<<< '), 1)
        self.person('outside', 'outside this conflict the merge already holds APP_RELEASE "1.1.0", not the parent\'s "1.0.0"')
        self.assertEqual((row['release_variable'], row['parent_release'], row['child_release']), ('APP_RELEASE', '1.0.0', '1.1.0'))
        self.assertNotIn('kept the parent', ' '.join(mr.log_lines(self.report)))

    def test_take_child_while_the_release_line_stayed_the_parents_goes_to_a_person(self):
        # same branch, the child is ahead; the parent bumped its release value, the child did not
        f, row = self.rule(rel_text('main', P2, '1.1.0'), rel_text('main', P3, '1.0.0'), self.SAME)
        self.assertIn(b'APP_RELEASE ?= "1.1.0"\n', self.before)
        self.person('outside', 'outside this conflict the merge already holds APP_RELEASE "1.1.0", not the child\'s "1.0.0"')

    def test_the_release_line_in_the_pin_region_moves_with_the_pin(self):
        base = rel_text('main', P1, '1.0.0', gap=0)
        parent, child = rel_text('main', P2, '1.1.0', gap=0), rel_text('main', P3, '1.2.0', gap=0)
        f, row = self.rule(parent, child, self.SAME, base=base)
        self.assertEqual(self.before.count(b'<<<<<<< '), 1)
        self.assertEqual((f['outcome'], row['code']), ('resolved', 'take-child'))
        self.assertEqual(self.repo.read(PIN_FILE), child)
        self.assertIn('took the child\'s pin and APP_RELEASE "1.2.0": both lines follow main', row['did'])
        self.assertEqual(self.report['trailers'], [f'Merge-Up-Rule: pins APP_REV=take-child parent={P2} child={P3} APP_RELEASE=1.2.0'])
        self.assertIn(f'pins rule: APP_REV (parent main at {P2[:8]} with APP_RELEASE "1.1.0", child main at {P3[:8]} with APP_RELEASE "1.2.0"',
                      mr.log_lines(self.report)[0])
        # different branches: the parent's whole unit
        parent = rel_text('2.x', P2, '2.0.0', gap=0)
        f, row = self.rule(parent, child, base=base)
        self.assertEqual((f['outcome'], row['code']), ('resolved', 'keep-parent'))
        self.assertEqual(self.repo.read(PIN_FILE), parent)
        self.assertIn('kept the parent\'s branch, pin and APP_RELEASE "2.0.0": ', row['did'])

    def test_the_release_line_in_a_region_of_its_own_takes_the_same_side(self):
        parent, child = rel_text('2.x', P2, '2.0.0'), rel_text('main', P3, '1.1.0')
        f, row = self.rule(parent, child)
        self.assertEqual(self.before.count(b'<<<<<<< '), 2)
        self.assertEqual((f['outcome'], [(r['region'], r['code']) for r in f['pins']]), ('resolved', [(1, 'keep-parent'), (2, 'keep-parent')]))
        self.assertEqual([i['rule'] for i in f['items']], ['pins', 'pins'])   # never spliced
        self.assertEqual(self.repo.read(PIN_FILE), parent)
        # one trailer, one log line and one table row for the unit
        self.assertEqual(self.report['trailers'], [f'Merge-Up-Rule: pins APP_REV=keep-parent parent={P2} child={P3} APP_RELEASE=2.0.0'])
        self.assertEqual(len([l for l in mr.log_lines(self.report) if l.startswith('pins rule: ')]), 1)
        self.assertEqual(mr.body_section(self.report).count('| `APP_REV` |'), 1)
        # the same with the child taken: each question is asked once for the two regions
        parent, child = rel_text('main', P2, '1.1.0'), rel_text('main', P3, '1.2.0')
        f, row = self.rule(parent, child, self.SAME)
        self.assertEqual((f['outcome'], [r['code'] for r in f['pins']]), ('resolved', ['take-child', 'take-child']))
        self.assertEqual(self.repo.read(PIN_FILE), child)
        self.assertEqual(self.compare.asked, [('app', P2, P3), ('app', P3, 'main')])
        # something else in the release line's region: the whole unit goes to a person
        child = rel_text('main', P3, '1.1.0').replace(b'APP_RELEASE', b'OTHER ?= "1"\nAPP_RELEASE')
        f, row = self.rule(rel_text('2.x', P2, '2.0.0'), child)
        self.assertEqual([(r['region'], r['code']) for r in f['pins']], [(1, 'unit-split'), (2, 'other-lines')])
        self.assertIn('the conflict holds more than APP_BRANCH, APP_REV and APP_RELEASE (OTHER)', f['pins'][1]['did'])
        self.person('unit-split', 'APP_BRANCH, APP_REV and APP_RELEASE move as one, and conflict 2 of them goes to a person (other-lines)')

    def test_a_region_that_names_two_units_sends_both_units_to_a_person(self):
        # Both lines edit the lines between APP_RELEASE and LIB_BRANCH: git cuts one region
        # holding the APP release value and the LIB branch, and one each for the LIB pin and
        # release value. That region names two pairs, is refused and is keyed by APP, the first
        # pair. LIB's other two regions have an answer (different branches: the parent's), but
        # writing them would leave LIB_BRANCH inside markers next to the parent's LIB pin, so the
        # LIB unit goes to a person with it.
        pins = {'pin_file': PIN_FILE, 'pairs': [dict(p, acts=True) for p in REL_PAIRS]}

        def edited(text, lines):
            return b'\n'.join(l + b' (edited)' if n in lines else l for n, l in enumerate(text.split(b'\n'), 1))
        base = rel_text('main', P1, '1.0.0', lib=('main', P1, '1.0.0'))
        pad = [n for n, l in enumerate(base.split(b'\n'), 1) if l.startswith(b'pad')]
        parent = edited(rel_text('main', P1, '1.1.0', lib=('2.x', P2, '')), pad[1:])
        child = edited(rel_text('main', P1, '1.2.0', lib=('main', P3, '3.1.1')), pad[:2])
        f, row = self.rule(parent, child, base=base, pins=pins)
        self.assertEqual(self.before.count(b'<<<<<<< '), 3)
        self.assertEqual([(r['region'], r['variable'], r['code']) for r in f['pins']],
                         [(1, 'APP_REV', 'other-lines'), (2, 'LIB_REV', 'unit-split'), (3, 'LIB_REV', 'unit-split')])
        self.assertIn('the conflict holds more than APP_BRANCH, APP_REV and APP_RELEASE (LIB_BRANCH)', f['pins'][0]['did'])
        self.assertIn('LIB_BRANCH, LIB_REV and LIB_RELEASE move as one, and conflict 1 of them goes to a person (other-lines)',
                      f['pins'][1]['did'])
        self.assertEqual((f['outcome'], self.report['trailers']), ('unresolved', []))
        self.assertEqual(self.repo.read(PIN_FILE), self.before)
        self.assertEqual(self.repo.unmerged(), [PIN_FILE])

    def test_a_release_variable_missing_on_the_chosen_side_goes_to_a_person(self):
        # the parent never set it, the child added it (merged clean): keep-parent cannot carry it
        f, row = self.rule(rel_text('2.x', P2, None), rel_text('main', P3, '1.1.0'), base=rel_text('main', P1, None))
        self.person('missing-release', 'the parent line does not set APP_RELEASE exactly once (does not set it)')
        # a child from before the variable, taken over a parent that has it
        f, row = self.rule(rel_text('main', P2, '1.1.0'), rel_text('main', P3, None), self.SAME, base=rel_text('main', P1, None))
        self.person('missing-release', 'the child line does not set APP_RELEASE exactly once (does not set it)')

    def test_a_release_variable_set_twice_on_the_chosen_side_goes_to_a_person(self):
        child = rel_text('main', P3, '1.1.0', extra='APP_RELEASE = "9.9.9"\n')
        f, row = self.rule(rel_text('main', P2, '1.0.0'), child, self.SAME)
        self.assertEqual(self.before.count(b'<<<<<<< '), 1)
        self.person('missing-release', 'the child line does not set APP_RELEASE exactly once (sets it 2 times)')

    def test_a_pair_in_shadow_logs_the_release_value_it_would_set(self):
        # the LIB pair only logs (as the ds pair does); its release line sits in the pin's region
        base = rel_text('main', P1, '1.0.0', lib=('main', P1, '1.0.0'), lib_gap=0)
        parent = rel_text('main', P1, '1.0.0', lib=('2.x', P2, '2.0.0'), lib_gap=0)
        child = rel_text('main', P1, '1.0.0', lib=('main', P3, '1.1.0'), lib_gap=0)
        f, row = self.rule(parent, child, base=base)
        self.assertEqual((row['variable'], row['code'], row['mode'], f['outcome']), ('LIB_REV', 'keep-parent', 'shadow', 'unresolved'))
        self.assertEqual(self.repo.read(PIN_FILE), self.before)
        self.assertEqual(self.report['trailers'], [])
        log = mr.log_lines(self.report)
        self.assertEqual(log[0], f'pins rule: shadow, nothing changes: LIB_REV (parent 2.x at {P2[:8]} with LIB_RELEASE "2.0.0", '
                                 f'child main at {P3[:8]} with LIB_RELEASE "1.1.0"): kept the parent\'s branch, pin and LIB_RELEASE "2.0.0": '
                                 'the lines follow different branches of lib')
        self.assertEqual(f['items'][0]['reason'], 'the pins rule only logs for LIB_REV (it would have kept the parent\'s branch, pin and LIB_RELEASE "2.0.0")')

    def test_a_pair_with_no_release_variable_behaves_as_before(self):
        parent, child = pin_text('2.x', P2, 'p'), pin_text('main', P3, 'c')
        want = ('kept the parent\'s branch and pin: the lines follow different branches of app',
                [f'Merge-Up-Rule: pins APP_REV=keep-parent parent={P2} child={P3}'],
                f'pins rule: APP_REV (parent 2.x at {P2[:8]}, child main at {P3[:8]}): kept the parent\'s branch and pin')
        # the pair has none, or it has one and neither line sets it (a line from before it)
        for pins in (PINS, REL_PINS):
            with self.subTest(release=pins is REL_PINS):
                f, row = self.rule(parent, child, base=pin_text('main', P1), pins=pins)
                self.assertEqual((f['outcome'], row['code'], row['release_variable']), ('resolved', 'keep-parent', None))
                self.assertEqual(self.repo.read(PIN_FILE), parent)
                self.assertEqual((row['did'], self.report['trailers']), want[:2])
                self.assertTrue(mr.log_lines(self.report)[0].startswith(want[2]), mr.log_lines(self.report)[0])
        # a pair without one ignores a release-like line: it is not part of the unit
        f, row = self.rule(rel_text('2.x', P2, '1.0.0'), rel_text('main', P3, '1.1.0'), pins=PINS)
        self.assertEqual((f['outcome'], row['code']), ('resolved', 'keep-parent'))

    def test_unit(self):
        self.assertEqual(mr.unit(PAIRS[0]), ['APP_BRANCH', 'APP_REV'])
        self.assertEqual(mr.unit(REL_PAIRS[0]), ['APP_BRANCH', 'APP_REV', 'APP_RELEASE'])
        self.assertEqual(mr.assigned([b'A ?= "1"\n', b'B ?= "2"\n', b'A = "3"\n'], 'A'), ['1', '3'])


REAL_PINS = os.path.join(FIXTURES, 'ds-src-pins', 'vertex-main.inc')
REAL_PAIRS = {'pin_file': 'conf/distro/include/ds-src-pins.inc', 'pairs': [
    {'rev': 'DSREV', 'branch': 'DS_BRANCH', 'release': 'DSREV_RELEASE', 'repo': 'ds', 'acts': False},
    {'rev': 'SENSOR_PORTAL_REV', 'branch': 'SENSOR_PORTAL_BRANCH', 'release': 'SENSOR_PORTAL_RELEASE', 'repo': 'sensor-portal', 'acts': True}]}


@unittest.skipIf(not os.path.exists(REAL_PINS), 'no copy of the layer pin file in this repo (fixtures/merge-up/ds-src-pins/)')
class RealPinFileTests(unittest.TestCase):
    """The pins rule on the layer's own pin file (meta-dsensos vertex/main at e6b503e9,
    conf/distro/include/ds-src-pins.inc), where comment lines stand between each pin and its
    release value: DSREV and DSREV_RELEASE, SENSOR_PORTAL_REV and SENSOR_PORTAL_RELEASE."""

    D, E = 'd' * 40, 'e' * 40
    DS, PORTAL = '11d7208fad6d767b3a89ef87314c94874f53cc5b', '23d765c9b6e6888bbf6f2f899f0a12035be09d03'

    def setUp(self):
        with open(REAL_PINS, 'rb') as f:
            self.base = f.read()

    def edit(self, **values):
        out = self.base
        for name, value in values.items():
            old = mr.variable(mr.split_lines(out), name)
            self.assertIsNotNone(old, name)
            out = out.replace(f'{name} ?= "{old}"\n'.encode(), f'{name} ?= "{value}"\n'.encode())
        return out

    def run_real(self, parent, child, answers):
        path = REAL_PAIRS['pin_file']
        repo = Repo(self, {path: (self.base, parent, child)})
        before = repo.read(path)
        report = mr.resolve_conflicts(repo.top, ACT, pins=REAL_PAIRS, compare=Compare(answers))
        return report, report['files'][0], repo.read(path), before

    def test_the_reviewers_case_on_the_real_file(self):
        # the parent moves the portal to main and pins it, its release line untouched; the child
        # bumps 3.x.x to the next release, pin and SENSOR_PORTAL_RELEASE
        parent = self.edit(SENSOR_PORTAL_BRANCH='main', SENSOR_PORTAL_REV=self.D)
        child = self.edit(SENSOR_PORTAL_REV=self.E, SENSOR_PORTAL_RELEASE='3.1.1')
        report, f, now, before = self.run_real(parent, child, {})
        self.assertIn(b'SENSOR_PORTAL_RELEASE ?= "3.1.1"\n', before)   # git merged the child's value clean
        self.assertEqual(before.count(b'<<<<<<< '), 1)
        row = f['pins'][0]
        self.assertEqual((f['outcome'], row['variable'], row['code']), ('unresolved', 'SENSOR_PORTAL_REV', 'outside'))
        self.assertIn('outside this conflict the merge already holds SENSOR_PORTAL_RELEASE "3.1.1", not the parent\'s "3.1.0"', row['did'])
        self.assertEqual(now, before)

    def test_both_pairs_on_the_real_file(self):
        # ds: same branch, the child ahead, pin and release bumped on both lines (shadow: logged only)
        # sensor-portal: different branches, pin and release changed on both lines (acts: the parent's unit)
        parent = self.edit(DSREV=self.D, DSREV_RELEASE='', SENSOR_PORTAL_BRANCH='main', SENSOR_PORTAL_REV=self.D, SENSOR_PORTAL_RELEASE='')
        child = self.edit(DSREV=self.E, DSREV_RELEASE='1.2.0', SENSOR_PORTAL_REV=self.E, SENSOR_PORTAL_RELEASE='3.1.1')
        answers = {('ds', self.D, self.E): ('ahead', ''), ('ds', self.E, '1.x.x'): ('ahead', '')}
        report, f, now, before = self.run_real(parent, child, answers)
        rows = [(r['variable'], r['code'], r['mode']) for r in f['pins']]
        self.assertEqual(rows, [('DSREV', 'take-child', 'shadow')] * 2 + [('SENSOR_PORTAL_REV', 'keep-parent', 'act')] * 2)
        self.assertEqual(f['outcome'], 'partial')
        # the portal unit is the parent's, pin and release value; the ds regions keep their markers
        self.assertIn(f'SENSOR_PORTAL_REV ?= "{self.D}"\n'.encode(), now)
        self.assertIn(b'SENSOR_PORTAL_RELEASE ?= ""\n', now)
        self.assertNotIn(b'3.1.1', now)
        self.assertEqual(now.count(b'<<<<<<< '), 2)
        log = mr.log_lines(report)
        self.assertIn(f'pins rule: shadow, nothing changes: DSREV (parent 1.x.x at {self.D[:8]} with DSREV_RELEASE "", '
                      f'child 1.x.x at {self.E[:8]} with DSREV_RELEASE "1.2.0", compare ahead): took the child\'s pin and '
                      'DSREV_RELEASE "1.2.0": both lines follow 1.x.x', log[0])
        self.assertIn(f'pins rule: SENSOR_PORTAL_REV (parent main at {self.D[:8]} with SENSOR_PORTAL_RELEASE "", child 3.x.x at '
                      f'{self.E[:8]} with SENSOR_PORTAL_RELEASE "3.1.1"): kept the parent\'s branch, pin and SENSOR_PORTAL_RELEASE ""', log[1])
        self.assertEqual(report['trailers'], [f'Merge-Up-Rule: pins SENSOR_PORTAL_REV=keep-parent parent={self.D} child={self.E} SENSOR_PORTAL_RELEASE='])

    def test_a_region_that_names_two_units_sends_both_units_to_a_person(self):
        # Both lines edit the comments between DSREV_RELEASE and SENSOR_PORTAL_BRANCH, so git cuts
        # one region holding the ds release value and the portal branch, and two more for the
        # portal pin and release value. That region is refused (it names two units) and is keyed
        # by DSREV, the first pair; the portal unit must go to a person with it, or the result
        # would hold the parent's portal pin next to whichever portal branch the resolver keeps.
        def lines(text, edits):
            out = text.split(b'\n')
            for n, f in edits.items():
                out[n - 1] = f(out[n - 1])
            return b'\n'.join(out)
        parent = lines(self.edit(DSREV_RELEASE='1.2.0', SENSOR_PORTAL_BRANCH='main', SENSOR_PORTAL_REV=self.D,
                                 SENSOR_PORTAL_RELEASE=''),
                       {n: (lambda l: l + b' (rewritten on main)') for n in range(82, 104)})
        child = lines(self.edit(DSREV_RELEASE='1.1.1', SENSOR_PORTAL_REV=self.E, SENSOR_PORTAL_RELEASE='3.1.1'),
                      {80: lambda l: b'# (the release stamped at the 1.1.1 promote)',
                       81: lambda l: l + b' (child edit)', 82: lambda l: l + b' (child edit)'})
        report, f, now, before = self.run_real(parent, child, {})
        self.assertEqual(before.count(b'<<<<<<< '), 3)
        rows = [(r['region'], r['variable'], r['code'], r['mode']) for r in f['pins']]
        self.assertEqual(rows, [(1, 'DSREV', 'other-lines', 'shadow'),
                                (2, 'SENSOR_PORTAL_REV', 'unit-split', 'act'), (3, 'SENSOR_PORTAL_REV', 'unit-split', 'act')])
        self.assertIn('SENSOR_PORTAL_BRANCH, SENSOR_PORTAL_REV and SENSOR_PORTAL_RELEASE move as one, and conflict 1 of '
                      'them goes to a person (other-lines)', f['pins'][1]['did'])
        self.assertEqual((f['outcome'], report['trailers']), ('unresolved', []))
        self.assertEqual(now, before)


GH = '''#!/bin/sh
echo "$GH_TOKEN $*" >> "$GH_LOG"
case "$2" in
  repos/org/app/compare/aaa...bbb) echo ahead ;;
  repos/other/lib/compare/aaa...bbb) echo behind ;;
  repos/org/app/compare/aaa...gone) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  repos/org/app/compare/aaa...odd) echo surprising ;;
  *) echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1 ;;
esac
'''


class GhCompareTests(unittest.TestCase):
    """The compare API through gh, with the read-only token and no other."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gh-compare-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, 'gh'), 'w') as f:
            f.write(GH)
        os.chmod(os.path.join(self.tmp, 'gh'), 0o755)
        saved = {k: os.environ.get(k) for k in ('PATH', 'GH_LOG', 'GH_TOKEN')}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update(PATH=self.tmp + os.pathsep + os.environ['PATH'], GH_LOG=os.path.join(self.tmp, 'log'), GH_TOKEN='the-write-token')

    def log(self):
        with open(os.path.join(self.tmp, 'log')) as f:
            return f.read().splitlines()

    def test_statuses(self):
        compare = mr.gh_compare('org', 'read-token')
        self.assertEqual(compare('app', 'aaa', 'bbb'), ('ahead', ''))
        self.assertEqual(compare('other/lib', 'aaa', 'bbb'), ('behind', ''))
        self.assertEqual(compare('app', 'aaa', 'gone'), ('not-found', 'gh: Not Found (HTTP 404)'))
        self.assertEqual(compare('app', 'aaa', 'zzz'), ('error', 'gh: Bad Gateway (HTTP 502)'))
        self.assertEqual(compare('app', 'aaa', 'odd'), ('error', "unexpected answer 'surprising'"))
        # every call carried the read-only token, never the one in the environment
        self.assertEqual(len(self.log()), 5)
        self.assertTrue(all(l.startswith('read-token api repos/') for l in self.log()), self.log())
        self.assertIn('read-token api repos/org/app/compare/aaa...bbb --jq .status', self.log())

    def test_without_a_token_nothing_is_asked(self):
        self.assertEqual(mr.gh_compare('org', '')('app', 'aaa', 'bbb')[0], 'no-token')
        self.assertEqual(mr.gh_compare('org', None)('app', 'aaa', 'bbb')[0], 'no-token')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'log')))


class OutputTests(unittest.TestCase):
    """What a run prints and what goes to the PR."""

    def test_write_outputs(self):
        files = dict(shape_files(DELETE_BELOW, 'ok.txt'), **shape_files(OVERLAP, 'no.txt'))
        files.update(shape_files(DELETE_BELOW, 'secret/s.txt'))
        repo = Repo(self, files)
        report = mr.resolve_conflicts(repo.top, ACT, never_auto=lambda p: 'secret/**' if p.startswith('secret/') else None)
        out = os.path.join(repo.top, '.out')
        mr.write_outputs(report, out)
        self.assertEqual(sorted(os.listdir(out)), ['body.md', 'comment.md', 'log.txt', 'remaining.txt', 'report.json', 'trailers.txt'])

        def read(name):
            with open(os.path.join(out, name)) as f:
                return f.read()
        self.assertEqual(read('remaining.txt'), 'no.txt\nsecret/s.txt\n')
        self.assertEqual(read('trailers.txt'), 'Merge-Up-Rule: splice files=1 regions=1\n')
        self.assertEqual(json.loads(read('report.json'))['resolved'], ['ok.txt'])
        self.assertEqual(read('log.txt').splitlines(), mr.log_lines(report))
        body = read('body.md')
        self.assertIn('**Resolved by rule in this commit.**', body)
        self.assertIn('**On the `never_auto` list.**', body)
        self.assertIn('Why the rules left the rest to a person:\n\n- `no.txt`: conflict 1: ' + mr.REFUSALS['overlap'], body)
        self.assertIn('```diff', read('comment.md'))
        # a later report without a candidate removes the stale comment file
        report['candidates'] = {}
        mr.write_outputs(report, out)
        self.assertNotIn('comment.md', os.listdir(out))

    def test_nothing_for_the_body_when_no_rule_acts(self):
        repo = Repo(self, shape_files(OVERLAP))
        self.assertEqual(mr.body_section(mr.resolve_conflicts(repo.top, {'splice': 'shadow'})), '')
        repo = Repo(self, shape_files(OVERLAP))
        self.assertIn('Why the rules left the rest to a person', mr.body_section(mr.resolve_conflicts(repo.top, ACT)))


def fixture_cases():
    index = os.path.join(FIXTURES, 'rule-cases.json')
    if not os.path.exists(index):
        return None
    with open(index) as f:
        return json.load(f)


@unittest.skipIf(fixture_cases() is None, 'no real-conflict fixtures in this repo (fixtures/merge-up/rule-cases.json)')
class RealConflictTests(unittest.TestCase):
    """The real merge-up conflicts of plan section 9 (fixtures/merge-up/<case>/, written by
    build_rule_fixtures.py from history). The conflict is rebuilt by a real git merge of the three
    versions; the rules must do what rule.json records, and where a person resolved the same way
    the result is that person's file byte for byte."""

    def load(self, name):
        d = os.path.join(FIXTURES, name)
        with open(os.path.join(d, 'rule.json')) as f:
            doc = json.load(f)

        def blob(n, side):
            path = os.path.join(d, f'{n}.{side}')
            if not os.path.exists(path):
                return None
            with open(path, 'rb') as f:
                return f.read()
        return doc, blob

    def run_case(self, name, modes=ACT):
        doc, blob = self.load(name)
        repo = Repo(self, {f['path']: (blob(f['n'], 'base'), blob(f['n'], 'parent'), blob(f['n'], 'child')) for f in doc['files']})
        marked = {f['path']: repo.read(f['path']) for f in doc['files']}
        pins = {'pin_file': 'conf/distro/include/ds-src-pins.inc', 'pairs': [
            {'rev': 'DSREV', 'branch': 'DS_BRANCH', 'repo': 'ds', 'acts': False},
            {'rev': 'SENSOR_PORTAL_REV', 'branch': 'SENSOR_PORTAL_BRANCH', 'repo': 'sensor-portal', 'acts': True}]} if doc['pins'] else None
        compare = Compare()
        report = mr.resolve_conflicts(repo.top, modes, never_auto=lambda p: next((x for x in doc['never_auto_prefixes'] if p.startswith(x)), None),
                                      pins=pins, compare=compare)
        return doc, blob, repo, marked, report, compare

    def test_every_listed_case_is_present_and_holds_no_marker(self):
        for name in fixture_cases():
            doc, blob = self.load(name)
            self.assertTrue(doc['files'], name)
            for f in doc['files']:
                for side in ('base', 'parent', 'child', 'landed'):
                    data = blob(f['n'], side)
                    if data is None:
                        self.assertEqual((side, f.get('refused')), ('base', 'add-add'), (name, f['path']))
                    else:
                        self.assertFalse(any(mr.MARKER_LINE.match(l) for l in mr.split_lines(data)), (name, f['path'], side))

    def test_the_rules_do_what_was_measured(self):
        counts = {'resolved': 0, 'equal': 0}
        differ = []   # the refusal of each file whose hunk count is not its region count
        for name in fixture_cases():
            doc, blob, repo, marked, report, compare = self.run_case(name)
            for want in doc['files']:
                with self.subTest(case=name, path=want['path']):
                    f = [x for x in report['files'] if x['path'] == want['path']][0]
                    landed, now = blob(want['n'], 'landed'), repo.read(want['path'])
                    self.assertEqual(f['outcome'], want['outcome'])
                    self.assertEqual(f['regions'], want['regions'])
                    if want['outcome'] == 'resolved':
                        self.assertFalse(any(mr.MARKER_LINE.match(l) for l in mr.split_lines(now)))
                        self.assertEqual(now == landed, want['equals_landed'])
                        counts['resolved'] += 1
                        counts['equal'] += now == landed
                    else:
                        # nothing a rule does not resolve is rewritten
                        self.assertEqual(now, marked[want['path']])
                        self.assertIn(want['path'], report['remaining'])
                    if 'reasons' in want:
                        self.assertEqual([i['reason'] for i in f['items']], [mr.REFUSALS[k] for k in want['reasons']])
                    if 'refused' in want:
                        # the reason, which may end with its own detail in brackets
                        self.assertTrue(f['refused'] == mr.REFUSALS[want['refused']] or f['refused'].startswith(mr.REFUSALS[want['refused']] + ' ('),
                                        f['refused'])
                    if want['git_merge_hunks'] != want['regions']:
                        # git merge shows other conflicts than the rule reads: never resolved, in no part
                        self.assertEqual((f['outcome'], f['n_would']), ('refused', 0))
                        differ.append(want.get('refused'))
                    if 'candidate_equals_landed' in want:
                        git(repo.top, 'apply', '-', data=report['candidates'][want['path']].encode())
                        self.assertEqual(repo.read(want['path']) == landed, want['candidate_equals_landed'])
                    if 'pins' in want:
                        self.assertEqual((f['pins'][0]['code'], f['pins'][0]['variable']), (want['pins'], want['variable']))
            # the three pins cases are settled by the branches alone
            self.assertEqual(compare.asked, [], name)
        self.assertGreater(counts['resolved'], 0)
        if 'sensor-portal-297' in fixture_cases():
            # templates/sidebar.html: two hunks in the work tree, one region
            self.assertIn('hunks', differ)

    def test_the_two_files_a_person_also_edited_differ_only_outside_the_conflict(self):
        if 'meta-dsensos-182' not in fixture_cases():
            self.skipTest('no meta-dsensos-182 fixture')
        doc, blob, repo, marked, report, _ = self.run_case('meta-dsensos-182')
        for f in doc['files']:
            now, landed = mr.split_lines(repo.read(f['path'])), mr.split_lines(blob(f['n'], 'landed'))
            changed = [l for l in set(now) ^ set(landed)]
            # one comment line, reworded by hand after the merge
            self.assertTrue(changed and all(l.startswith(b'#') for l in changed), (f['path'], changed))

    def test_shadow_rewrites_none_of_them(self):
        for name in fixture_cases():
            doc, blob, repo, marked, report, _ = self.run_case(name, {'splice': 'shadow', 'pins': 'shadow'})
            # the paths, not the files: on a failure unittest would diff two dicts of whole
            # files line by line, which takes longer than the CI job is allowed to run
            self.assertEqual([p for p in sorted(marked) if repo.read(p) != marked[p]], [], name)
            self.assertEqual((report['resolved'], report['trailers'], report['candidates']), ([], [], {}), name)


if __name__ == '__main__':
    unittest.main()
