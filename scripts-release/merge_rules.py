#!/usr/bin/env python3
"""The merge-up rules that resolve a conflict without a person (ENG-2234).

One file, byte for byte the same in the three repos that carry it: the OS
distro repo, sensor-portal and ds. It names no repository the other two may
not name (their pr-checks audit the release tooling for that). Standard
library and git only. release_git.py `resolve-conflicts` is the
command; merge-up.sh runs it inside an uncommitted merge whose conflicts git
has already written to the work tree with zdiff3 markers.

R-splice, adjacent edits. Git reports two edits that touch but do not overlap
as one conflict. The three versions of the file come from the index (stage 1
the base, 2 the parent line, 3 the child line) and are cut into regions with
`git merge-file --diff3`. Inside a region each side is a list of edits to the
base lines. The region resolves when the two sides' edits cover disjoint base
line ranges: both are applied, in base order. Text both sides wrote is kept
once, and the log and the PR body name the region ("same text on both sides,
kept once"): an edit that is the same on both sides is applied once, and an
insertion whose whole block equals the adjoining lines the other side's
replacement produced is dropped. The region is refused when

    overlap     both sides changed the same base lines
    add/add     the file has no base version
    same-point  both sides inserted at the same point
    inside      one side inserted inside the range the other replaced
    end, start  one side inserted right after, or right before, an edit of
                the other side that puts more lines than it replaces: that
                edit may have added lines at the same point, and nothing
                says which text comes first. An edit that only deletes
                lines, or replaces them with no more lines, is not refused
    twice       the result would hold a line more often than either side
                does: both sides added it, but not as one whole block that
                can be kept once (sameness in part is never deduplicated)
    absorbed    two insertions of one side, before and after an edit of the
                other side, both repeat that edit's text, and together they
                are longer than it: keeping the text once would drop lines
                that side wrote

A file is refused whole when it is binary, not a regular file on every side,
deleted on one side, holds lines of its own that look like markers, lacks a
final newline or has CRLF line endings in any version, or when git's merge of
its three versions cannot be read. A refused file never stops the others.

A file whose regions all resolve is verified (no marker line left, `bash -n`
for .sh, json.loads for .json, compile() for .py, check_yaml.py for YAML where
that script exists next to this one) and staged. In a file that resolves in
part the resolved regions are replaced and the others are written back with
zdiff3 markers, so the file stays in the conflict list. The text outside the
regions must equal git's own output: the regions, written back with none of
them resolved, must give `git merge-file --zdiff3` of the three versions byte
for byte, or the file is left exactly as git wrote it.

The regions are `git merge-file`'s (the myers diff, on every git version).
`git merge` aligns with the histogram diff and now and then cuts a file
differently: more hunks, fewer, or the same number on other lines. Such a
file is refused whole, because the regions would not be the conflicts the
work tree and the PR diff show: the regions, written back unresolved, must
also equal the work tree file byte for byte (which needs the merge to have
been made with merge.conflictStyle=zdiff3). The refusal names the two counts
when they differ. A file no rule resolves in any part is never rewritten.

R-pins, the app pins of a layer (the layer step of the OS distro repo only;
inert where the caller passes no pin configuration). A pin, its branch
variable and, where the caller names one, its release variable (the release
at the pin, X.Y.Z or empty) are one unit: a region of the pin file that names
any of them belongs to this rule and is never spliced. When the two lines
follow different app branches the parent keeps its own branch and pin. When
they follow the same branch the child's pin is taken if the compare API says
the parent's pin is an ancestor of it and it is on that branch; a parent pin
that is already newer stays (a pin never rolls back). Diverged pins, an
unknown commit, an API error, a missing token, a missing branch variable and
anything else in the region go to a person, each named. Whatever side the
answer takes, the result must hold that side's branch, pin and release value
together, wherever git put each line: in the region, in another region of the
same unit (which then takes the same side) or merged clean outside every
region. Else the unit goes to a person: the chosen side does not set the
release variable exactly once, git merged another line's value outside the
regions, or another region that names the unit has no answer (also one that
names a second pair and was refused for that). A file in which neither
line sets the release variable is decided on the branch and pin alone. A pair
the caller does not list as acting is evaluated and logged only.

never_auto: the caller passes a function that says whether a path is on the
list. No rule writes such a file. What the rules would have written is kept as
a patch (the candidate) for a PR comment.

Modes: each rule is 'act', 'shadow' (evaluate and log, write nothing) or 'off'.
"""
import collections
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = ('splice', 'pins')
MODES = ('off', 'shadow', 'act')
TRAILER = 'Merge-Up-Rule'
# What git writes with the default marker size. A file that holds such a line
# of its own cannot be told from a conflict, so no rule touches it.
MARKER_LINE = re.compile(rb'^(<{7} |\|{7} |={7}\r?\n?$|>{7} )')
LABELS = (b'HEAD', b'base', b'MERGE_HEAD')
ASSIGNMENT = re.compile(rb'^([A-Za-z_][A-Za-z0-9_]*)[ \t]*\??=[ \t]*"([^"\\]*)"[ \t]*$')
SHA = re.compile(r'[0-9a-f]{40}')

REFUSALS = {
    'overlap': 'both sides changed the same base lines',
    'add-add': 'add/add: the file has no base version, both lines added it',
    'same-point': 'both sides inserted at the same point',
    'inside': 'one side inserted inside the range the other side replaced',
    'end': "one side inserted right after an edit of the other side that adds lines",
    'start': "one side inserted right before an edit of the other side that adds lines",
    'twice': 'both sides added the same line, but not as one whole block: the result would hold it more often than either side',
    'absorbed': "one side wrote the other side's replacement text both before and after it, more lines than the replacement holds: keeping it once would lose lines",
}
# Refusals of a whole file that end with their own detail in brackets.
REFUSALS['hunks'] = 'git merge cut the conflicts of the file differently from git merge-file, so the regions are not the conflicts git shows'
SPLICED = 'adjacent edits, both applied in base order'
SAME = 'same text on both sides, kept once'


class Error(Exception):
    pass


def _run(args, cwd=None, data=None):
    return subprocess.run(args, cwd=cwd, input=data, capture_output=True)


def _git(git_dir, *args):
    r = _run(['git', '-C', git_dir, *args])
    if r.returncode != 0:
        raise Error(f'git {" ".join(args)}: {(r.stderr or r.stdout).decode("utf-8", "replace").strip()}')
    return r.stdout


def split_lines(data):
    """The lines of a blob, each with its newline. Only \\n ends a line, as for git."""
    return re.findall(rb'[^\n]*\n|[^\n]+', data)


# --- regions --------------------------------------------------------------------

def _marker_size(blobs):
    """A marker size no line of the three versions can be taken for."""
    longest = 0
    for data in blobs:
        for m in re.finditer(rb'(?m)^(<+|\|+|=+|>+)', data):
            longest = max(longest, len(m.group(1)))
    return max(7, longest + 1)


def merge_file(base, ours, theirs, *options):
    """(conflict count, output) of `git merge-file -p` on the three versions.
    Always the myers diff, whatever the git version or its configuration."""
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for name, data in (('ours', ours), ('base', base), ('theirs', theirs)):
            paths.append(os.path.join(tmp, name))
            with open(paths[-1], 'wb') as f:
                f.write(data)
        r = _run(['git', '-c', 'diff.algorithm=myers', 'merge-file', '-p', *options, *paths])
    if r.returncode < 0 or r.returncode > 127:
        raise Error(f'git merge-file failed: {r.stderr.decode("utf-8", "replace").strip()}')
    return r.returncode, r.stdout


def regions(base, ours, theirs):
    """The three versions cut the way git merges them: a list that alternates
    ('clean', lines) and ('conflict', ours lines, base lines, theirs lines),
    starting and ending with a clean entry. From `git merge-file --diff3`."""
    size = _marker_size((base, ours, theirs))
    count, output = merge_file(base, ours, theirs, '--diff3', f'--marker-size={size}', '-L', 'o', '-L', 'b', '-L', 't')
    marks = {b'<' * size + b' o\n': 'ours', b'|' * size + b' b\n': 'base', b'=' * size + b'\n': 'theirs', b'>' * size + b' t\n': 'clean'}
    order = {'ours': 'clean', 'base': 'ours', 'theirs': 'base', 'clean': 'theirs'}
    out, state, cur = [], 'clean', {'clean': []}
    for line in split_lines(output):
        nxt = marks.get(line)
        if nxt and order[nxt] == state:
            if nxt == 'ours':
                out.append(('clean', cur['clean']))
                cur = {'ours': [], 'base': [], 'theirs': []}
            elif nxt == 'clean':
                out.append(('conflict', cur['ours'], cur['base'], cur['theirs']))
                cur = {'clean': []}
            state = nxt
        else:
            cur[state].append(line)
    if state != 'clean':
        raise Error('git merge-file output ends inside a conflict')
    out.append(('clean', cur['clean']))
    if (len(out) - 1) // 2 != count:
        raise Error(f'git merge-file reports {count} conflict(s), {(len(out) - 1) // 2} were read')
    return out


def edits(base, side):
    """One side of a region as edits to the base lines: (start, end, new lines),
    a half-open base range. start == end is an insertion before base line start."""
    sm = difflib.SequenceMatcher(None, base, side, autojunk=False)
    return [(i1, i2, side[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != 'equal']


def clash(a, b):
    """Why two edits, one from each side, cannot both be applied: a key of REFUSALS, or None."""
    (a1, a2, ar), (b1, b2, br) = a, b
    a_ins, b_ins = a1 == a2, b1 == b2
    if a_ins and b_ins:
        return 'same-point' if a1 == b1 else None
    if a_ins or b_ins:
        (p, (r1, r2, repl)) = (a1, b) if a_ins else (b1, a)
        if r1 < p < r2:
            return 'inside'
        if len(repl) > r2 - r1:
            # the other edit adds lines: at its ends they would meet this insertion
            return 'end' if p == r2 else 'start' if p == r1 else None
        return None
    return 'overlap' if a1 < b2 and b1 < a2 else None


def same_text(ins, other):
    """Whether an insertion of one side repeats, whole, the text the other side's edit put
    right next to it: the inserted block equals the last lines of a replacement that ends
    where it is inserted, or the first lines of one that starts there."""
    (p, _, text), (r1, r2, repl) = ins, other
    if r1 == r2 or not text or len(repl) < len(text):
        return False
    return (p == r2 and repl[-len(text):] == text) or (p == r1 and repl[:len(text)] == text)


def splice(ours, base, theirs):
    """(lines, None, kept) when the two sides' edits can both be applied in base order,
    else (None, a key of REFUSALS, 0). kept counts the lines that both sides wrote and the
    result holds once: an edit that is the same on both sides is applied once, and an
    insertion that repeats, whole, the adjoining text of the other side's edit is dropped
    (see same_text). Nothing else is deduplicated: a result that holds a line more often
    than either side does is refused."""
    eo, et = edits(base, ours), edits(base, theirs)
    kept = 0
    for e in [e for e in eo if e in et]:
        # the same edit on both sides, applied once
        et.remove(e)
        kept += len(e[2])
    for mine, other in ((eo, et), (et, eo)):
        claimed = collections.Counter()
        for e in [e for e in mine if e[0] == e[1] and any(same_text(e, o) for o in other)]:
            mine.remove(e)
            kept += len(e[2])
            for o in other:
                if same_text(e, o):
                    claimed[o[:2]] += len(e[2])
        # One edit can stand for an insertion before it and one after it only while the two
        # do not claim the same lines of it. Base `x`, one side `L x L`, the other `L`:
        # dropping both insertions would leave one `L` where that side wrote two.
        if any(n > len(o[2]) for o in other for n in [claimed[o[:2]]]):
            return None, 'absorbed', 0
    for a in eo:
        for b in et:
            why = clash(a, b)
            if why:
                return None, why, 0
    out, pos = [], 0
    # an insertion at a point sorts before an edit that starts there
    for i1, i2, repl in sorted(eo + et, key=lambda e: (e[0], e[1])):
        out += base[pos:i1] + repl
        pos = i2
    out += base[pos:]
    have = (collections.Counter(ours), collections.Counter(theirs))
    if any(n > max(have[0][line], have[1][line]) for line, n in collections.Counter(out).items()):
        return None, 'twice', 0
    return out, None, kept


def splice_region(ours, base, theirs):
    """splice without the count: (lines, None), or (None, a key of REFUSALS)."""
    return splice(ours, base, theirs)[:2]


# --- the pins rule -------------------------------------------------------------

def assignment(line):
    """(variable, value) of a `VAR ?= "value"` or `VAR = "value"` line, else None."""
    m = ASSIGNMENT.match(line.rstrip(b'\r\n'))
    return (m.group(1).decode(), m.group(2).decode()) if m else None


def assigned(lines, name):
    """Every value a pin file gives a variable, in file order."""
    return [a[1] for a in map(assignment, lines) if a and a[0] == name]


def variable(lines, name):
    """The value a pin file gives a variable, or None when it is absent or set more than once."""
    values = assigned(lines, name)
    return values[0] if len(values) == 1 else None


def unit(pair):
    """The variables of a pair that move as one: its branch variable, its pin and, when the
    caller names one, its release variable."""
    return [v for v in (pair['branch'], pair['rev'], pair.get('release')) if v]


def _and(names):
    return names[0] if len(names) == 1 else f'{", ".join(names[:-1])} and {names[-1]}'


def gh_compare(owner, token):
    """compare(repo, base, head) through `gh api` with the read-only token:
    (status, detail), status one of ahead, behind, identical, diverged (the
    compare API's words: ahead means base is an ancestor of head), not-found
    (HTTP 404: a commit or branch the repo does not have), no-token, error."""
    def compare(repo, base, head):
        if not token:
            return 'no-token', 'no read-only token for the app repos'
        full = repo if '/' in repo else f'{owner}/{repo}'
        try:
            r = subprocess.run(['gh', 'api', f'repos/{full}/compare/{base}...{head}', '--jq', '.status'],
                               capture_output=True, text=True, env=dict(os.environ, GH_TOKEN=token))
        except OSError as e:
            return 'error', str(e)
        status = r.stdout.strip()
        if r.returncode == 0 and status in ('ahead', 'behind', 'identical', 'diverged'):
            return status, ''
        detail = (r.stderr.strip().splitlines() or [f'unexpected answer {status!r}'])[0]
        return ('not-found' if 'HTTP 404' in detail else 'error'), detail
    return compare


def pins_named(region, pairs):
    """(the pairs a region names by a variable of their unit, every variable it assigns, the
    count of its lines that are neither a comment, blank nor an assignment)."""
    _, ours, base, theirs = region
    named, other = set(), 0
    for line in ours + base + theirs:
        a = assignment(line)
        if a:
            named.add(a[0])
        elif line.strip() and not line.lstrip().startswith(b'#'):
            other += 1
    return [p for p in pairs if named & set(unit(p))], named, other


def pins_region(region, pairs, parent_lines, child_lines, compare):
    """R-pins on one region of the pin file. None when the region names no
    variable of a pair's unit (it is not this rule's). Else a row: variable,
    branch_variable, release_variable (None when the pair has none or neither
    line sets it), repo, acts, the two branches, pins and release values,
    compare, code, did, and lines (the region's text) when the rule has an
    answer, None when the region goes to a person."""
    _, ours, base, theirs = region
    mine, named, other = pins_named(region, pairs)
    if not mine:
        return None
    pair = mine[0]
    rel = pair.get('release')
    counts = {'parent': len(assigned(parent_lines, rel)), 'child': len(assigned(child_lines, rel))} if rel else {}
    # Neither line sets it (a line from before the variable): the file is decided on the branch
    # and pin alone, and the result has no release value either.
    active = rel if any(counts.values()) else None
    row = {'variable': pair['rev'], 'branch_variable': pair['branch'], 'release_variable': active, 'repo': pair['repo'],
           'acts': bool(pair.get('acts')),
           'parent_branch': variable(parent_lines, pair['branch']), 'child_branch': variable(child_lines, pair['branch']),
           'parent_pin': variable(parent_lines, pair['rev']), 'child_pin': variable(child_lines, pair['rev']),
           'parent_release': variable(parent_lines, rel) if active else None,
           'child_release': variable(child_lines, rel) if active else None,
           'compare': '', 'lines': None}
    out = _pins_decide(row, pair, named, other, len(mine), ours, theirs, compare)
    if out['lines'] is None or not active:
        return out
    whose = 'child' if out['code'] == 'take-child' else 'parent'
    if counts[whose] != 1:
        how = 'does not set it' if not counts[whose] else f'sets it {counts[whose]} times'
        return dict(out, lines=None, code='missing-release',
                    did=f'left to a person: the {whose} line does not set {rel} exactly once ({how}), so the release value '
                        f'cannot go with the {whose}\'s pin')
    return out


def _pins_decide(row, pair, named, other, n_pairs, ours, theirs, compare):
    """The answer of R-pins for one region, the release variable aside: see pins_region."""
    rel = row['release_variable']

    def person(code, did):
        return dict(row, code=code, did=did)

    def carried(whose, *what):
        """What a side gives the result: its branch and pin, and its release value."""
        return _and(list(what) + ([f'{rel} "{row[whose + "_release"]}"'] if rel and row[whose + '_release'] is not None else []))

    extra = sorted(named - set(unit(pair)))
    if n_pairs > 1 or extra or other:
        what = ', '.join(extra) or 'lines that are neither a comment nor an assignment'
        return person('other-lines', f'left to a person: the conflict holds more than {_and(unit(pair))} ({what})')
    for side, key in (('parent', 'parent_branch'), ('child', 'child_branch')):
        if row[key] is None:
            return person('missing-branch', f'left to a person: the {side} line does not set {pair["branch"]} exactly once')
    for side, key in (('parent', 'parent_pin'), ('child', 'child_pin')):
        if row[key] is None or not SHA.fullmatch(row[key]):
            return person('missing-pin', f'left to a person: the {side} line does not set {pair["rev"]} to one full commit')
    if row['parent_branch'] != row['child_branch']:
        return dict(row, code='keep-parent', lines=ours,
                    did=f'kept the parent\'s {carried("parent", "branch", "pin")}: the lines follow different branches of {pair["repo"]}')
    branch = row['parent_branch']
    if row['parent_pin'] == row['child_pin']:
        return person('same-pin', 'left to a person: both lines pin the same commit, so the conflict is in the lines around it')
    status, detail = compare(pair['repo'], row['parent_pin'], row['child_pin'])
    row['compare'] = status

    def api_person(status, detail, what):
        if status == 'diverged':
            return person('diverged', f'left to a person: {what} have diverged')
        if status == 'not-found':
            return person('not-found', f'left to a person: {pair["repo"]} does not have {what} (HTTP 404)')
        if status == 'no-token':
            return person('no-token', f'left to a person: {detail}, so the pins could not be compared')
        return person('api-error', f'left to a person: the compare API failed ({detail})')

    if status == 'behind':
        return dict(row, code='keep-parent-newer', lines=ours,
                    did=f'kept the parent\'s {carried("parent", "pin")}: it is already newer than the child\'s on {branch} (a pin never rolls back)')
    if status != 'ahead':
        return api_person(status, detail, 'the two pins')
    on_branch, detail = compare(pair['repo'], row['child_pin'], branch)
    if on_branch in ('ahead', 'identical'):
        return dict(row, code='take-child', lines=theirs,
                    did=f'took the child\'s {carried("child", "pin")}: both lines follow {branch}, the parent\'s pin is an ancestor of it and it is on {branch}')
    if on_branch in ('behind', 'diverged'):
        row['compare'] = f'ahead; not on {branch}'
        return person('not-on-branch', f'left to a person: the child\'s pin is not on the branch {branch} of {pair["repo"]}')
    row['compare'] = f'ahead; {on_branch}'
    return api_person(on_branch, detail, f'the child\'s pin and the branch {branch}')


# --- one file ---------------------------------------------------------------------

def stages(git_dir, path):
    """{stage: (mode, blob)} of an unmerged path, from the index."""
    out = {}
    for entry in _git(git_dir, 'ls-files', '-u', '-z', '--', path).split(b'\0'):
        if entry:
            meta, _ = entry.split(b'\t', 1)
            mode, sha, stage = meta.decode().split()
            out[int(stage)] = (mode, _git(git_dir, 'cat-file', 'blob', sha))
    return out


def labels(worktree):
    """The three marker labels of the conflict git wrote to the work tree, or LABELS."""
    out = list(LABELS)
    for n, start in enumerate((b'<<<<<<< ', b'||||||| ', b'>>>>>>> ')):
        m = re.search(rb'(?m)^' + re.escape(start) + rb'([^\r\n]*)\r?$', worktree)
        if m:
            out[n] = m.group(1)
    return tuple(out)


def _trim(ours, theirs):
    """(prefix, suffix) lengths zdiff3 moves out of a conflict: the lines both sides start
    with, then the lines both of the remainders end with."""
    pre = 0
    while pre < min(len(ours), len(theirs)) and ours[pre] == theirs[pre]:
        pre += 1
    suf = 0
    while suf < min(len(ours), len(theirs)) - pre and ours[-1 - suf] == theirs[-1 - suf]:
        suf += 1
    return pre, suf


def marked(region, names):
    """A region as git writes it with zdiff3: the lines both sides start and end with outside
    the markers, the base in full between them."""
    _, o, b, t = region
    pre, suf = _trim(o, t)
    return (o[:pre] + [b'<<<<<<< ' + names[0] + b'\n'] + o[pre:len(o) - suf] + [b'||||||| ' + names[1] + b'\n'] + b
            + [b'=======\n'] + t[pre:len(t) - suf] + [b'>>>>>>> ' + names[2] + b'\n'] + (o[len(o) - suf:] if suf else []))


def verify(path, data):
    """Why a file without markers is still not accepted, or None: a marker
    line, or the syntax check its name asks for."""
    for line in split_lines(data):
        if MARKER_LINE.match(line):
            return 'a conflict marker line is left'
    ext = os.path.splitext(path)[1].lower()
    if ext == '.json':
        try:
            json.loads(data.decode('utf-8'))
        except ValueError as e:
            return f'not valid JSON ({e})'
    elif ext == '.py':
        try:
            compile(data.decode('utf-8'), path, 'exec')
        except (SyntaxError, ValueError) as e:
            return f'does not compile as Python ({e})'
    elif ext == '.sh':
        r = _run(['bash', '-n'], data=data)
        if r.returncode != 0:
            return f'bash -n fails ({(r.stderr.decode("utf-8", "replace").strip().splitlines() or ["no message"])[0]})'
    elif ext in ('.yml', '.yaml'):
        checker = os.path.join(HERE, 'check_yaml.py')
        if os.path.exists(checker):
            with tempfile.TemporaryDirectory() as tmp:
                name = os.path.join(tmp, 'result' + ext)
                with open(name, 'wb') as f:
                    f.write(data)
                r = _run([sys.executable, checker, name])
            if r.returncode == 1:
                lines = [l for l in r.stdout.decode('utf-8', 'replace').splitlines() if l.startswith('::error')]
                return f'check_yaml.py fails ({(lines or ["no message"])[0].split(": ", 1)[-1]})'
            if r.returncode != 0:
                return f'check_yaml.py could not run ({(r.stderr.decode("utf-8", "replace").strip().splitlines() or ["exit %d" % r.returncode])[0]})'
    return None


def patch(path, old, new):
    """A patch that turns old into new, with a/<path> and b/<path> headers (git apply takes it)."""
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, 'a'), os.path.join(tmp, 'b')
        for name, data in ((a, old), (b, new)):
            with open(name, 'wb') as f:
                f.write(data)
        r = _run(['git', 'diff', '--no-index', '--no-color', a, b])
    if r.returncode not in (0, 1):
        raise Error(f'git diff failed: {r.stderr.decode("utf-8", "replace").strip()}')
    body = r.stdout[r.stdout.find(b'\n@@ ') + 1:] if b'\n@@ ' in r.stdout else b''
    return f'diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n'.encode() + body


def evaluate_file(git_dir, path, modes, never_auto=None, pins=None, compare=None):
    """What the rules make of one unmerged path. Read-only. Returns a dict:
    path, never_auto (the pattern or None), regions (count), refused (a reason
    for the whole file, or None), items (per region: n, rule, mode, resolved,
    reason), pins (rows), and the texts: write (what an acting rule puts in the
    work tree, or None), would (the same with shadow rules acting too), each
    with its count of resolved regions (n_write, n_would)."""
    res = {'path': path, 'never_auto': never_auto(path) if never_auto else None, 'regions': 0, 'refused': None,
           'items': [], 'pins': [], 'write': None, 'would': None, 'n_write': 0, 'n_would': 0, 'worktree': None}

    def refuse(why):
        res['refused'] = why
        return res

    st = stages(git_dir, path)
    if not st:
        return refuse('not an unmerged path')
    if any(mode not in ('100644', '100755') for mode, _ in st.values()):
        return refuse('not a regular file on every side (a symlink or a submodule pointer)')
    if 2 not in st or 3 not in st:
        return refuse('modify/delete: one line deleted or renamed the file')
    if 1 not in st:
        return refuse(REFUSALS['add-add'])
    base, ours, theirs = st[1][1], st[2][1], st[3][1]
    if any(b'\0' in d for d in (base, ours, theirs)):
        return refuse('a binary file')
    if any(MARKER_LINE.match(l) for d in (base, ours, theirs) for l in split_lines(d)):
        return refuse('the file holds lines of its own that look like conflict markers')
    if any(d and not d.endswith(b'\n') for d in (base, ours, theirs)):
        return refuse('a version of the file does not end with a newline')
    if any(b'\r\n' in d for d in (base, ours, theirs)):
        # git merge-file ends its marker lines with CRLF in such a file
        return refuse('a version of the file has CRLF line endings')
    try:
        with open(os.path.join(git_dir, path), 'rb') as f:
            wt = f.read()
    except OSError as e:
        return refuse(f'the work tree copy could not be read ({e.strerror})')
    res['worktree'] = wt
    names = labels(wt)
    # A merge-file output this code cannot read is a refusal of this file: the
    # other files of the merge are still evaluated.
    try:
        parts = regions(base, ours, theirs)
        conflicts = [p for p in parts if p[0] == 'conflict']
        res['regions'] = len(conflicts)
        if not conflicts:
            return refuse('git merge-file finds no conflict in the three versions')
        # The text outside the regions must be git's own output: the regions written back with
        # none resolved are git merge-file --zdiff3 of the three versions, byte for byte.
        plain = b''.join(b''.join(p[1] if p[0] == 'clean' else marked(p, names)) for p in parts)
        if plain != merge_file(base, ours, theirs, '--zdiff3', '-L', names[0], '-L', names[1], '-L', names[2])[1]:
            return refuse('the regions do not add up to git\'s own merge of the three versions')
        # And the regions must be the conflicts git merge itself wrote to the work tree. git
        # merge aligns with another diff and now and then cuts a file differently: it can show
        # two hunks, one of them a true overlap, where merge-file has one region that looks
        # like adjacent edits. A rule then would not be resolving what the PR diff shows.
        if plain != wt:
            hunks = sum(1 for l in split_lines(wt) if l.startswith(b'<<<<<<< '))
            return refuse(f'{REFUSALS["hunks"]} ({hunks} hunk{"s" if hunks != 1 else ""} in the work tree, '
                          f'{len(conflicts)} region{"s" if len(conflicts) != 1 else ""}'
                          f'{", on other lines" if hunks == len(conflicts) else ""})')
    except Error as e:
        res['regions'] = 0
        return refuse(f'git\'s merge of the three versions could not be read ({e})')
    pin_lines = (split_lines(ours), split_lines(theirs)) if pins and path == pins['pin_file'] else None
    if pin_lines and compare:
        # one answer per question: the regions of one unit ask the same ones
        asked, ask = {}, compare

        def compare(*q):
            if q not in asked:
                asked[q] = ask(*q)
            return asked[q]
    results = []   # per region: (lines or None, acts); None for a pins region until all are read
    rows = {}      # index in results -> the pins row of that region
    units = {}     # index in results -> the pins (rev) of every unit that region names
    for n, region in enumerate(conflicts, 1):
        _, o, b, t = region
        row = None
        if pin_lines and modes.get('pins', 'off') == 'off':
            if pins_named(region, pins['pairs'])[0]:
                # a pin is the pins rule's alone, also while that rule is off
                res['items'].append({'n': n, 'rule': None, 'mode': 'off', 'resolved': False,
                                     'reason': 'it names an app pin, which only the pins rule decides, and that rule is off'})
                results.append((None, False))
                continue
        elif pin_lines:
            row = pins_region(region, pins['pairs'], pin_lines[0], pin_lines[1], compare)
        if row is not None and row['lines'] is not None:
            # The rule read the branches, pins and release values from the two lines' own files,
            # but it only writes the regions. What git merged outside them stands: with the side
            # the rule takes in every conflict of the file, the unit must read as that side's
            # branch, pin and release value, or the result would pair one line's branch or
            # release value with the other's pin.
            side, whose = (3, 'child') if row['code'] == 'take-child' else (1, 'parent')
            whole = [l for p in parts for l in (p[1] if p[0] == 'clean' else p[side])]
            want = [(row['branch_variable'], row[f'{whose}_branch']), (row['variable'], row[f'{whose}_pin'])]
            if row['release_variable']:
                want.append((row['release_variable'], row[f'{whose}_release']))
            for var, value in want:
                got = variable(whole, var)
                if got != value:
                    held = f'{var} "{got}"' if got is not None else f'{var} unset or set twice'
                    row.update(lines=None, code='outside',
                               did=f'left to a person: outside this conflict the merge already holds {held}, not the '
                                   f'{whose}\'s "{value}", so this conflict cannot be decided on its own')
                    break
        if row is not None:
            units[len(results)] = {p['rev'] for p in pins_named(region, pins['pairs'])[0]}
            rows[len(results)] = dict(row, region=n)
            res['items'].append(None)
            results.append(None)
            continue
        mode = modes.get('splice', 'off')
        if mode == 'off':
            res['items'].append({'n': n, 'rule': None, 'mode': 'off', 'resolved': False, 'reason': 'no rule is on for it'})
            results.append((None, False))
            continue
        lines, why, kept = splice(o, b, t)
        res['items'].append({'n': n, 'rule': 'splice', 'mode': mode, 'resolved': lines is not None, 'same': kept,
                             'reason': REFUSALS[why] if why else f'{SPLICED}; {SAME}' if kept else SPLICED})
        results.append((lines, mode == 'act'))

    # A unit's regions are decided together: when one of them goes to a person, so do the
    # others, or the result would hold one line's pin next to the other line's release value
    # (or branch) from the region left with markers. A refused region counts for every unit it
    # names, not only the one its row is keyed by: one that holds two pairs' lines is refused
    # under the first pair, and the other pair's unit still has a line inside its markers.
    refused = [(units[k], r) for k, r in rows.items() if r['lines'] is None]
    for k, row in rows.items():
        left = [r for named, r in refused if row['variable'] in named]
        if row['lines'] is not None and left:
            pair = next(p for p in pins['pairs'] if p['rev'] == row['variable'])
            row.update(lines=None, code='unit-split',
                       did=f'left to a person: {_and(unit(pair))} move as one, and conflict {left[0]["region"]} of them '
                           f'goes to a person ({left[0]["code"]})')
    for k, row in rows.items():
        lines = row.pop('lines')
        mode = modes['pins'] if row['acts'] else 'shadow'
        res['pins'].append(dict(row, mode=mode))
        reason = row['did']
        if lines is not None and mode != 'act':
            reason = f'the pins rule only logs for {row["variable"]} (it would have {reason.split(":")[0]})'
        res['items'][k] = {'n': row['region'], 'rule': 'pins', 'mode': mode, 'resolved': lines is not None, 'reason': reason}
        results[k] = (lines, mode == 'act')

    def compose(acting_only):
        out, k, count = [], 0, 0
        for part in parts:
            if part[0] == 'clean':
                out += part[1]
                continue
            lines, acts = results[k]
            k += 1
            if lines is not None and (acts or not acting_only):
                out += lines
                count += 1
            else:
                out += marked(part, names)
        return b''.join(out), count

    would, n_would = compose(False)
    write, n_write = compose(True)
    if n_would == len(conflicts):
        problem = verify(path, would)
        if problem:
            for item in res['items']:
                item['resolved'] = False
            return refuse(f'the result does not verify: {problem}')
    res.update(n_would=n_would, n_write=n_write)
    if n_would:
        res['would'] = would
    if n_write:
        res['write'] = write
    return res


# --- all files --------------------------------------------------------------------

def unmerged_paths(git_dir):
    return [p.decode('utf-8', 'surrogateescape') for p in _git(git_dir, 'diff', '--name-only', '--diff-filter=U', '-z').split(b'\0') if p]


def _put_back(git_dir, path, data):
    """Undo a write of this module: the unmerged index entries again, and the work tree file
    byte for byte as git wrote it (checkout --merge alone writes its own labels and the
    repo's default conflict style, not the zdiff3 conflict the merge was made with)."""
    _run(['git', '-C', git_dir, 'checkout', '--merge', '--', path])
    with open(os.path.join(git_dir, path), 'wb') as f:
        f.write(data)


def resolve_conflicts(git_dir, modes, paths=None, never_auto=None, pins=None, compare=None, write=True, after=None):
    """Run the rules over the unmerged paths of an open merge (or over `paths`).

    Every path is evaluated first, and nothing is written unless every
    evaluation ran. Then, per file not on never_auto: the regions an acting
    rule resolved are written to the work tree, and a file with no conflict
    left is staged. write=False (a dry run) evaluates only. Returns the report:
    files (see evaluate_file, plus outcome), remaining (paths still in
    conflict), resolved (paths staged), candidates ({path: patch} for
    never_auto files), trailers.

    after: called with the report before it is returned (the caller's own
    outputs). The writes stand only when everything succeeded: if a write, the
    staging or `after` fails, every file written so far is put back as git left
    it, unmerged and byte for byte, and the failure is raised. A caller that
    then goes on without the rules finds the merge exactly as it was."""
    for rule in RULES:
        if modes.get(rule, 'off') not in MODES:
            raise Error(f'{rule}: mode must be one of {", ".join(MODES)}')
    files = [evaluate_file(git_dir, p, modes, never_auto, pins, compare) for p in (unmerged_paths(git_dir) if paths is None else paths)]
    report = {'modes': {r: modes.get(r, 'off') for r in RULES}, 'written': bool(write), 'files': [], 'remaining': [], 'resolved': [],
              'candidates': {}, 'trailers': []}
    done = []   # (path, the work tree copy before this module wrote it)
    try:
        for f in files:
            wt, text, would = f.pop('worktree'), f.pop('write'), f.pop('would')
            full = f['n_write'] == f['regions'] and f['regions'] > 0
            full_would = f['n_would'] == f['regions'] and f['regions'] > 0
            if f['refused']:
                f['outcome'] = 'refused'
            elif f['never_auto']:
                f['outcome'] = 'never_auto'
                if text is not None:
                    report['candidates'][f['path']] = patch(f['path'], wt, text).decode('utf-8', 'replace')
            elif text is None:
                f['outcome'] = 'unresolved'
            else:
                f['outcome'] = 'resolved' if full else 'partial'
                if write:
                    done.append((f['path'], wt))
                    with open(os.path.join(git_dir, f['path']), 'wb') as out:
                        out.write(text)
                    if full:
                        _git(git_dir, 'add', '--', f['path'])
            f['would'] = ('resolve' if full_would else 'partial' if would is not None else 'nothing') if not f['refused'] else 'nothing'
            f['candidate'] = f['path'] in report['candidates']
            (report['resolved'] if f['outcome'] == 'resolved' else report['remaining']).append(f['path'])
            report['files'].append(f)
        acted = [f for f in report['files'] if f['outcome'] in ('resolved', 'partial')]
        n_splice = sum(1 for f in acted for i in f['items'] if i['rule'] == 'splice' and i['mode'] == 'act' and i['resolved'])
        if n_splice:
            report['trailers'].append(f'{TRAILER}: splice files={sum(1 for f in acted if any(i["rule"] == "splice" and i["mode"] == "act" and i["resolved"] for i in f["items"]))} regions={n_splice}')
        for f in acted:
            for row in f['pins']:
                if row['mode'] == 'act' and row['code'] in ('keep-parent', 'keep-parent-newer', 'take-child'):
                    rel = row['release_variable']
                    line = (f'{TRAILER}: pins {row["variable"]}={row["code"]} parent={row["parent_pin"]} child={row["child_pin"]}'
                            + (f' {rel}={row["child_release" if row["code"] == "take-child" else "parent_release"]}' if rel else ''))
                    if line not in report['trailers']:   # one per unit, also when it conflicts in two regions
                        report['trailers'].append(line)
        if after:
            after(report)
    except Exception:
        # put back what git wrote for every file touched so far, then fail as a whole
        for path, data in done:
            _put_back(git_dir, path, data)
        raise
    return report


# --- what a run prints, and what a PR says ------------------------------------------

def _count(f, key='n_write'):
    return f'{f[key]} of {f["regions"]} conflict{"s" if f["regions"] != 1 else ""}'


def _same(f):
    """The regions an acting splice resolved by keeping text of both sides once, for a log line or a table cell."""
    return [f'conflict {i["n"]}: {SAME} ({i["same"]} line{"s" if i["same"] != 1 else ""})'
            for i in f['items'] if i.get('same') and i['resolved']]


def _left(f, sep='; '):
    return sep.join(f'conflict {i["n"]}: {i["reason"]}' for i in f['items'] if not (i['resolved'] and i['mode'] == 'act'))


def _quoted(value):
    """A release value for a log line: "3.1.0", "" (set, empty), or unset (absent or set twice)."""
    return 'unset' if value is None else f'"{value}"'


def log_lines(report):
    """One line per finding, for the run log and the run summary."""
    out = []
    dry = '' if report['written'] else 'would have '
    for f in report['files']:
        p = f['path']
        for row in f['pins']:
            shadow = '' if row['mode'] == 'act' else 'shadow, nothing changes: '
            sides = ', '.join(f'{who} {row[who + "_branch"]} at {(row[who + "_pin"] or "?")[:8]}'
                              + (f' with {row["release_variable"]} {_quoted(row[who + "_release"])}' if row['release_variable'] else '')
                              for who in ('parent', 'child'))
            line = f'pins rule: {shadow}{row["variable"]} ({sides}{", compare " + row["compare"] if row["compare"] else ""}): {row["did"]}'
            if line not in out:   # one per unit, also when it conflicts in two regions
                out.append(line)
        if f['refused']:
            out.append(f'no rule applies to {p}: {f["refused"]}')
            continue
        if f['never_auto']:
            cand = (f'; the rules have an answer for {_count(f)}, kept as a candidate patch' if f['candidate']
                    else f'; shadow: the rules would have an answer for {_count(f, "n_would")}' if f['n_would'] else '; the rules have no answer either')
            out.append(f'never_auto: {p} matches {f["never_auto"]}, so no rule writes it{cand}')
            continue
        splice = [i for i in f['items'] if i['rule'] == 'splice']
        if f['outcome'] == 'resolved':
            out.append(f'{dry}resolved {p} ({_count(f)}); no markers left' + ''.join(f'; {x}' for x in _same(f)))
        elif f['outcome'] == 'partial':
            out.append(f'{dry}resolved {_count(f)} in {p}; the rest keeps its markers ({_left(f)})' + ''.join(f'; {x}' for x in _same(f)))
        elif f['n_would']:
            verb = 'resolve' if f['would'] == 'resolve' else 'partly resolve'
            out.append(f'shadow, nothing changes: would {verb} {p} ({_count(f, "n_would")})'
                       + ('' if f['would'] == 'resolve' else f'; {_left(f)}') + ''.join(f'; {x}' for x in _same(f)))
        elif splice or f['items']:
            out.append(f'no rule resolves {p} ({_left(f)})')
    return out


def body_section(report):
    """The part of a PR body that says what the rules did, as markdown. Empty when no rule acted
    and nothing is worth telling the resolver."""
    acted = [f for f in report['files'] if f['outcome'] in ('resolved', 'partial')]
    pins = [row for f in report['files'] for row in f['pins'] if row['mode'] == 'act']
    # one row per unit and answer, also when the unit conflicts in two regions
    pins = [row for i, row in enumerate(pins) if not any((r['variable'], r['code'], r['did']) == (row['variable'], row['code'], row['did']) for r in pins[:i])]
    never = [f for f in report['files'] if f['outcome'] == 'never_auto']
    told = [f for f in report['files'] if f['outcome'] in ('refused', 'unresolved', 'partial')]
    if 'act' not in report['modes'].values() or not (acted or pins or never or told):
        return ''
    out = []
    if acted:
        out += ['**Resolved by rule in this commit.** Check these like any other change in the diff:', '',
                '| File | Rule | What the rule did |', '|---|---|---|']
        for f in acted:
            rules = sorted({i['rule'] for i in f['items'] if i['resolved'] and i['mode'] == 'act'})
            what = (f'{_count(f)} resolved, no markers left' if f['outcome'] == 'resolved'
                    else f'{_count(f)} resolved, the rest keeps its markers')
            if 'splice' in rules:
                what += '. Splice: the two lines edited neighbouring lines, both edits are applied in base order'
                what += ''.join(f'. {x[0].upper()}{x[1:]}' for x in _same(f) if f['outcome'] in ('resolved', 'partial'))
            out.append(f'| `{f["path"]}` | {", ".join(rules)} | {what} |')
        out.append('')
    if pins:
        out += ['The pins rule:', '', '| Variable | Branch (parent / child) | Pin (parent / child) | Compare | What the rule did |', '|---|---|---|---|---|']
        for row in pins:
            out.append(f'| `{row["variable"]}` | `{row["parent_branch"]}` / `{row["child_branch"]}` | '
                       f'`{(row["parent_pin"] or "?")[:8]}` / `{(row["child_pin"] or "?")[:8]}` | {row["compare"] or "not needed"} | {row["did"]} |')
        out.append('')
    if never:
        out += ['**On the `never_auto` list.** No rule writes these files. Each needs a resolver and a different approver:', '']
        for f in never:
            out.append(f'- `{f["path"]}` (matches `{f["never_auto"]}`)'
                       + ('. What the rules would have written is in a comment on this PR, as a patch to check and apply by hand' if f['candidate'] else ''))
        out.append('')
    left = [f for f in told if f['outcome'] != 'resolved']
    if left:
        out += ['Why the rules left the rest to a person:', '']
        for f in left:
            out.append(f'- `{f["path"]}`: {f["refused"] or _left(f, ". ")}')
        out.append('')
    return '\n'.join(out).rstrip('\n') + '\n'


def candidate_comment(report, limit=60000):
    """The PR comment that carries the candidate patches of the never_auto files, or ''."""
    if not report['candidates']:
        return ''
    out = ['The merge-up rules do not write files on the `never_auto` list. For the file(s) below they do have an answer. '
           'Treat it as a suggestion: read it, and apply it to this branch by hand if it is right '
           '(`git apply` takes each patch as it is).', '']
    for path, text in report['candidates'].items():
        f = [x for x in report['files'] if x['path'] == path][0]
        out += [f'`{path}`: {_count(f)}' + (', no markers left after it' if f['n_write'] == f['regions'] else ', markers remain for the rest'), '']
        if len(text) > limit:
            out += [f'(the patch is {len(text)} characters, too long for a comment; re-run the rule locally to get it)', '']
        else:
            out += ['```diff', text.rstrip('\n'), '```', '']
    return '\n'.join(out).rstrip('\n') + '\n'


def write_outputs(report, out_dir):
    """report.json, log.txt, body.md, comment.md (only with a candidate), trailers.txt, remaining.txt in out_dir."""
    os.makedirs(out_dir, exist_ok=True)
    texts = {'report.json': json.dumps(report, indent=1) + '\n', 'log.txt': ''.join(l + '\n' for l in log_lines(report)),
             'body.md': body_section(report), 'trailers.txt': ''.join(t + '\n' for t in report['trailers']),
             'remaining.txt': ''.join(p + '\n' for p in report['remaining']), 'comment.md': candidate_comment(report)}
    for name, text in texts.items():
        path = os.path.join(out_dir, name)
        if name == 'comment.md' and not text:
            if os.path.exists(path):
                os.remove(path)
            continue
        with open(path, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.write(text)
