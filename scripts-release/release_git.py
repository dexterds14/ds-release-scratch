#!/usr/bin/env python3
"""Git and GitHub helpers shared by create-branch.sh, merge-up.sh, abandon.sh and promote.sh (ENG-1933, ENG-1934, ENG-1935, ENG-2110).

    release_git.py merge-keep-target [--git-dir DIR] [--root ROOT]
        inside an uncommitted merge: the target keeps its pending-version files
        (versions/<product>, never versions/README.md, which merges like any doc)
        and its copy of every bookkeeping file (release/config.json
        `bookkeeping_files`) that exists in HEAD, via `git checkout HEAD --`, so
        those paths never conflict. What merge-up runs, and what a human runs by hand.
    release_git.py line-plan <product> <version> [--kind auto|minor|patch] [--root ROOT]
        JSON {line, kind, level, pending, tag} for the line cut from tag vN.M.P:
        minor -> N.x.x pending N.(M+1).0; patch -> N.M.x pending N.M.(P+1).
        auto (the default) is minor for N.0.0 and patch for anything else.
        With `product_prefix: true` the line is <product>/N.x.x and the tag <product>/vN.M.P
    release_git.py promote-plan <line> <pending> [--bump auto|major|minor|patch] [--root DIR]
        JSON {product, line, kind, version, tag, level, next_version} for promoting <pending>
        on <line>; exit 1 when the version does not fit the line or the bump would leave
        a pending version that does not fit it (auto: major on main, minor on N.x.x,
        patch on N.M.x)
    release_git.py unmerged <base> <head> [--git-dir DIR] [--root ROOT]
        the commits in <base>..<head> that are not line bookkeeping (versions/ and the
        bookkeeping files only), "<sha> <subject>" per line; exit 1 when there are any
    release_git.py versions-only <sha> [--git-dir DIR]
        exit 0 when the commit touches only versions/ (never for a merge commit)
    release_git.py walk-versions-only <sha> [--git-dir DIR] [--max N]
        <sha>, then its first parents while the commit is versions-only, one per line
    release_git.py first-run [FILE]
        from `gh run list --json databaseId,number` output: prints "<databaseId> <number>"
        of the first run, exit 1 when the list is empty
    release_git.py pr-check [FILE]
        from `gh pr view --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits`:
        exit 0 when the PR is approved by someone other than the last pusher,
        mergeStateStatus is CLEAN and no check failed; else every reason, exit 1

--root (default: the checkout this script lives in) names the repo whose
release/config.json supplies the products, the prefix mode and the
bookkeeping files. Standard library only (runs on the CI runners and in
the tests).
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lines  # noqa: E402  (sibling module: parse_line)
import version  # noqa: E402  (sibling module: bump(), config() and SEMVER_CORE)

NUM = r'(0|[1-9][0-9]*)'
VERSIONS_DIR = 'versions/'
VERSIONS_README = 'versions/README.md'
LINE_KINDS = ('auto', 'minor', 'patch')
BUMP_LEVELS = ('auto',) + version.LEVELS
AUTO_LEVEL = {'main': 'major', 'minor': 'minor', 'patch': 'patch'}
FAILED_CONCLUSIONS = {'FAILURE', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'}
FAILED_STATES = {'FAILURE', 'ERROR'}


class Error(Exception):
    pass


def git(git_dir, *args):
    r = subprocess.run(['git', '-C', git_dir, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise Error(f'git {" ".join(args)}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


def load_config(root):
    """release/config.json under root, as version.config gives it (an Error when invalid)."""
    try:
        return version.config(os.path.abspath(root))
    except version.CheckError as e:
        raise Error(str(e))


def tag_name(product, ver, prefix=True):
    """The release tag of a version: <product>/vX.Y.Z when prefixed, vX.Y.Z otherwise."""
    return f'{product}/v{ver}' if prefix else f'v{ver}'


# --- merge-up: what the target keeps ------------------------------------------

def keep_target_versions(git_dir):
    """Inside an uncommitted merge: restore the target's (HEAD's) pending-version files.

    Only the per-product files: versions/README.md is documentation and merges like any
    other file, so a README change reaches every line. A versions file that exists only on
    the incoming side is left as merged (the target has no copy to prefer). Returns the paths
    restored."""
    paths = [p for p in git(git_dir, 'ls-tree', '--name-only', 'HEAD', 'versions/').split()
             if p != VERSIONS_README]
    if paths:
        git(git_dir, 'checkout', 'HEAD', '--', *paths)
    return paths


def keep_target_bookkeeping(git_dir, bookkeeping_files):
    """Inside an uncommitted merge: restore the target's copy of each bookkeeping file it has.

    `git checkout HEAD -- <path>` writes HEAD's version and stages it, which also resolves a
    conflict on that path, so a bookkeeping file never conflicts. A file the target does not
    have is left as merged. Returns the paths restored."""
    kept = []
    for path in bookkeeping_files:
        if git(git_dir, 'ls-tree', '--name-only', 'HEAD', '--', path).strip() == path:
            git(git_dir, 'checkout', 'HEAD', '--', path)
            kept.append(path)
    return kept


def merge_keep_target(git_dir, bookkeeping_files=()):
    """keep_target_versions, then keep_target_bookkeeping. Returns every path restored."""
    return keep_target_versions(git_dir) + keep_target_bookkeeping(git_dir, bookkeeping_files)


# --- versions ----------------------------------------------------------------

def line_plan(product, ver, kind='auto', prefix=True):
    """Where a line cut from v<ver> lives and what it opens with.

    Any N.M.P tag can open either line (ENG-1934: the pilot promotes 0.0.1 on
    main with bump=patch and then cuts both 0.x.x and 0.0.x from it). kind
    'auto' keeps the original derivation, N.0.0 -> the minor line, anything
    else -> the patch line; 'minor' and 'patch' force one.
    """
    m = version.SEMVER_CORE.fullmatch(ver)
    if not m:
        raise Error(f'version {ver!r} must be N.M.P (the tag {tag_name(product, "N.M.P", prefix)} the line is cut from)')
    if kind not in LINE_KINDS:
        raise Error(f'kind must be one of {LINE_KINDS}, got {kind!r}')
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    if kind == 'auto':
        kind = 'minor' if (minor == '0' and patch == '0') else 'patch'
    tag = tag_name(product, ver, prefix)
    if kind == 'minor':
        return {'line': lines.line_name(product, f'{major}.x.x', prefix), 'kind': 'minor', 'level': 'minor',
                'pending': version.bump(ver, 'minor'), 'tag': tag}
    return {'line': lines.line_name(product, f'{major}.{minor}.x', prefix), 'kind': 'patch', 'level': 'patch',
            'pending': version.bump(ver, 'patch'), 'tag': tag}


def fits_line(parsed, ver):
    """True when a pending version has the shape the line can promote: anything on main,
    N.M.0 on N.x.x, N.M.P on N.M.x."""
    if not version.SEMVER_CORE.fullmatch(ver):
        return False
    kind, prefix = parsed['kind'], parsed['version_prefix']
    if kind == 'main':
        return True
    if not ver.startswith(prefix):
        return False
    rest = ver[len(prefix):]
    if kind == 'minor':
        return bool(re.fullmatch(NUM + r'\.0', rest))
    if kind == 'patch':
        return bool(re.fullmatch(NUM, rest))
    return False


def line_shape(parsed):
    kind, prefix = parsed['kind'], parsed['version_prefix']
    return {'main': 'X.Y.Z', 'minor': f'{prefix}M.0', 'patch': f'{prefix}P'}.get(kind, '?')


def promote_plan(line, pending, products, bump='auto', prefix=True):
    """What a promote of <pending> on <line> tags and leaves behind (ENG-1934)."""
    parsed = lines.parse_line(line, products, prefix)
    name = lines.strip_ref(line)
    if parsed['kind'] == 'none':
        raise Error(f'{name} is not a release line ({", ".join(products)})')
    if bump not in BUMP_LEVELS:
        raise Error(f'bump must be one of {BUMP_LEVELS}, got {bump!r}')
    if not version.SEMVER_CORE.fullmatch(pending):
        raise Error(f'pending version {pending!r} is not X.Y.Z')
    if not fits_line(parsed, pending):
        raise Error(f'pending version {pending} does not fit {name} (a {parsed["kind"]} line promotes {line_shape(parsed)})')
    level = AUTO_LEVEL[parsed['kind']] if bump == 'auto' else bump
    nxt = version.bump(pending, level)
    if not fits_line(parsed, nxt):
        raise Error(f'bump {level} after {pending} leaves pending {nxt}, which does not fit {name} '
                    f'({line_shape(parsed)}); use bump={AUTO_LEVEL[parsed["kind"]]} (auto) on a {parsed["kind"]} line')
    return {'product': parsed['product'], 'line': name, 'kind': parsed['kind'], 'version': pending,
            'tag': tag_name(parsed['product'], pending, prefix), 'level': level, 'next_version': nxt}


# --- commits -----------------------------------------------------------------

def is_versions_only(git_dir, sha):
    """True when the commit has one parent and its diff touches only versions/."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) for f in files)


def is_bookkeeping(git_dir, sha, bookkeeping_files=()):
    """True when the commit has one parent and touches only versions/ and the bookkeeping
    files (a line's opening commit or a promote's bump): never unmerged work."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) or f in bookkeeping_files for f in files)


def unmerged(git_dir, base, head, bookkeeping_files=()):
    """[(sha, subject)] of the commits in base..head that are not bookkeeping, oldest first."""
    out = []
    text = git(git_dir, 'rev-list', '--topo-order', '--reverse', '--format=%H %s', '--no-commit-header', f'{base}..{head}')
    for line in text.splitlines():
        sha, _, subject = line.partition(' ')
        if sha and not is_bookkeeping(git_dir, sha, bookkeeping_files):
            out.append((sha, subject))
    return out


def walk_versions_only(git_dir, sha, limit=5):
    cur = git(git_dir, 'rev-parse', '--verify', f'{sha}^{{commit}}').strip()
    out = [cur]
    while len(out) <= limit and is_versions_only(git_dir, cur):
        cur = git(git_dir, 'rev-parse', '--verify', f'{cur}^1').strip()
        out.append(cur)
    return out


# --- gh JSON -----------------------------------------------------------------

def first_run(doc):
    if not isinstance(doc, list) or not doc:
        return None
    r = doc[0]
    return f'{r.get("databaseId", "")} {r.get("number", "")}'.strip()


def pr_check(doc):
    """Reasons the PR must not be merged by the bot (empty = ok)."""
    reasons = []
    if doc.get('reviewDecision') != 'APPROVED':
        reasons.append(f'reviewDecision is {doc.get("reviewDecision")!r}, not APPROVED')
    if doc.get('mergeStateStatus') != 'CLEAN':
        reasons.append(f'mergeStateStatus is {doc.get("mergeStateStatus")!r}, not CLEAN')
    commits = doc.get('commits') or []
    pusher = None
    if commits:
        authors = commits[-1].get('authors') or []
        if authors:
            pusher = authors[0].get('login')
    latest = {}
    for rv in doc.get('reviews') or []:
        login = (rv.get('author') or {}).get('login')
        state = rv.get('state')
        if login and state in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[login] = state  # gh lists reviews oldest first
    approvers = sorted(l for l, s in latest.items() if s == 'APPROVED' and l != pusher)
    if not approvers:
        reasons.append(f'no APPROVED review by someone other than the last pusher ({pusher!r})')
    for c in doc.get('statusCheckRollup') or []:
        name = c.get('name') or c.get('context') or c.get('workflowName') or '?'
        if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES:
            reasons.append(f'check {name}: {c.get("conclusion") or c.get("state")}')
    return reasons, pusher, approvers


def read_json_arg(path):
    if path and path != '-':
        with open(path) as f:
            return json.load(f)
    return json.load(sys.stdin)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    root_help = 'repo root with release/config.json (default: this checkout)'
    p = sub.add_parser('merge-keep-target')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('line-plan')
    p.add_argument('product')
    p.add_argument('version')
    p.add_argument('--kind', default='auto', choices=LINE_KINDS)
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('promote-plan')
    p.add_argument('line')
    p.add_argument('pending')
    p.add_argument('--bump', default='auto', choices=BUMP_LEVELS)
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('unmerged')
    p.add_argument('base')
    p.add_argument('head')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    for name in ('versions-only', 'walk-versions-only'):
        p = sub.add_parser(name)
        p.add_argument('sha')
        p.add_argument('--git-dir', default='.')
        if name == 'walk-versions-only':
            p.add_argument('--max', type=int, default=5)
    p = sub.add_parser('first-run')
    p.add_argument('file', nargs='?')
    p = sub.add_parser('pr-check')
    p.add_argument('file', nargs='?')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'merge-keep-target':
            cfg = load_config(args.root)
            kept = merge_keep_target(args.git_dir, cfg['bookkeeping_files'])
            print(f'kept from the target: {", ".join(kept) or "nothing (no versions files, no bookkeeping files)"}')
        elif args.cmd == 'line-plan':
            if not version.PRODUCT_NAME.fullmatch(args.product):
                raise Error(f'product {args.product!r} must match {version.PRODUCT_NAME.pattern}')
            cfg = load_config(args.root)
            print(json.dumps(line_plan(args.product, args.version, args.kind, cfg['product_prefix'])))
        elif args.cmd == 'promote-plan':
            cfg = load_config(args.root)
            print(json.dumps(promote_plan(args.line, args.pending, cfg['products'], args.bump, cfg['product_prefix'])))
        elif args.cmd == 'unmerged':
            cfg = load_config(args.root)
            missing = unmerged(args.git_dir, args.base, args.head, cfg['bookkeeping_files'])
            for sha, subject in missing:
                print(f'{sha} {subject}')
            return 1 if missing else 0
        elif args.cmd == 'versions-only':
            return 0 if is_versions_only(args.git_dir, args.sha) else 1
        elif args.cmd == 'walk-versions-only':
            print('\n'.join(walk_versions_only(args.git_dir, args.sha, args.max)))
        elif args.cmd == 'first-run':
            out = first_run(read_json_arg(args.file))
            if out is None:
                return 1
            print(out)
        elif args.cmd == 'pr-check':
            reasons, pusher, approvers = pr_check(read_json_arg(args.file))
            for r in reasons:
                print(f'release_git.py: pr-check: {r}', file=sys.stderr)
            if reasons:
                return 1
            print(f'approved by {", ".join(approvers)} (last pusher {pusher}), mergeable, checks green')
        return 0
    except (Error, OSError, ValueError) as e:
        print(f'release_git.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
