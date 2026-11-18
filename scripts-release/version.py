#!/usr/bin/env python3
"""Pending versions and version strings for the release lines (ENG-1930).

    version.py check [ROOT]                  validate release/config.json, versions/ and release/matrix.json
    version.py config [--key a.b]            release/config.json, validated, defaults filled in (JSON)
    version.py rc <product> [--sha SHA]      X.Y.Z-rc.<N>.github-<sha7>, N = git rev-list --count SHA
    version.py dev <product>                 X.Y.Z-dev.<N>.g<sha7>[.dirty]
    version.py bump <product> <major|minor|patch>
    version.py matrix <product>              the product's release flavors as a JSON list
    version.py matrix-include <product> [--sdk]   {"include": [{"flavor": ...}]} for strategy.matrix;
                                             --sdk adds "sdk": true on the product's "sdk" flavors (promote only)
    version.py products [--json]             the products in release/config.json

ROOT defaults to the repo this script lives in; --root overrides it, and
--git-dir (rc, dev) the repository that supplies the commit count and sha.
Every string this prints is lowercase and never contains '+': ds-ota orders
versions by strict SemVer and publish-swu.sh strips both.

Standard library only: it runs on the CI runners and in the tests with no
extra packages.
"""
import argparse
import json
import os
import re
import subprocess
import sys

SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = 'release/config.json'
MATRIX = 'release/matrix.json'
VERSIONS_DIR = 'versions'
PRODUCT_DIR = 'kas/include/product'
# A semver core, no leading zeros, nothing else (no v, no pre-release, no build metadata).
SEMVER_CORE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
# What the version overlay in CI accepts (yocto-build.yml) and what ds-ota can order.
VERSION_STRING = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-z.-]+)?')
PRODUCT_NAME = re.compile(r'[a-z][a-z0-9]*')
LEVELS = ('major', 'minor', 'patch')
# How R-lock lands (merge_up.lock_pin, ENG-2234): "exact" when the meta-dsensos
# tip builds exactly what the two locked pins build, "extra" when it holds more.
# exact: bot-pr (the default and the shipped value since 2026-11-18: the bot
# opens a PR, waits for the required checks and merges it, so every merge the
# rule makes has a PR as its record) or push (a direct push of the parent, with
# the trailer and a commit comment as the record).
LOCK_PIN_EXACT = ('push', 'bot-pr')
LOCK_PIN_EXTRA = ('bot-pr', 'pr')
# No merge-up rule ever writes to a workflow file (Dexter, 2026-11-04), so
# every never_auto list must carry this pattern, and it is the whole default.
WORKFLOWS_GLOB = '.github/workflows/**'
# The "merge_up" block of release/config.json with the default of every key:
# what `config` fills in for a key, or the whole block, that is absent. The
# shipped state is in release/config.json; these are the values a config
# without the block (an older line, a test fixture) gets, and they refuse
# nothing and automate nothing beyond phase 0.
MERGE_UP_DEFAULTS = {
    'finish': {'strict': False, 'required_checks': []},
    'lock_pin': {'verify_finish': False, 'exact': 'bot-pr', 'extra': 'bot-pr', 'wait_minutes': 15},
    'never_auto': [WORKFLOWS_GLOB],
    'meta_dsensos': {'never_auto': [WORKFLOWS_GLOB], 'required_checks': []},
}
# The longest lock_pin.wait_minutes. The bot-finished PR is merged after the
# wait with the App token minted when the job started, and an installation
# token lives 60 minutes: a longer wait would end with green checks and an
# expired token. Ten minutes are left for the checkout and the layer step.
LOCK_PIN_WAIT_MAX = 50
# Every key release/config.json may carry. Anything else is refused, so a
# misspelt switch cannot sit in the file looking as if it were on.
CONFIG_KEYS = ('products', 'merge_up')


class CheckError(Exception):
    """A violation, with the file it was found in."""


def read_json(root, rel):
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise CheckError(f'{rel}: missing')
    with open(path) as f:
        try:
            return json.load(f)
        except ValueError as e:
            raise CheckError(f'{rel}: not valid JSON ({e})')


def glob_regex(pattern):
    """A glob pattern as a compiled regex over a repo-relative path with '/'
    separators. The one implementation of the glob syntax (ENG-2234); the
    never_auto lists are matched with never_auto_regex, which is built on it
    and matches more:

    - A pattern without '/' is matched against the file name alone, at any
      depth: `*sudoers*` matches `a/b/50-ds-sudoers`.
    - A pattern with '/' is matched against the whole path from the repo root.
    - `*` is any run of characters except '/', `?` one character except '/'.
      Neither crosses a directory.
    - `**` is a whole path segment and crosses directories: zero or more
      directories in front of or between other segments (`a/**/b` matches
      `a/b` and `a/x/y/b`), and everything beneath as the last segment
      (`a/**` matches `a/b` and `a/x/y`, not `a` itself).
    - Everything else is literal and case-sensitive. There are no character
      classes, escapes or negations: `[`, `]`, `{`, `}`, `\\`, a leading `!`, a
      leading or trailing '/', an empty, `.` or `..` segment, `**` inside a
      longer segment, and a pattern made only of `*` are refused, so a
      pattern cannot mean something other than what it looks like.
    """
    if not isinstance(pattern, str) or not pattern:
        raise CheckError(f'glob {pattern!r}: must be a non-empty string')
    if pattern != pattern.strip() or any(c in pattern for c in '[]{}\\') or pattern.startswith('!'):
        raise CheckError(f'glob {pattern!r}: only *, ? and ** are special; no [ ] {{ }} \\, leading ! or surrounding space')
    segments = pattern.split('/')
    if any(s in ('', '.', '..') for s in segments):
        raise CheckError(f'glob {pattern!r}: no leading, trailing or doubled /, and no . or .. segment')
    if any('**' in s and s != '**' for s in segments):
        raise CheckError(f'glob {pattern!r}: ** must be a whole path segment (a/**/b, a/**)')
    if not pattern.strip('*/'):
        raise CheckError(f'glob {pattern!r}: matches everything; switch the automation off instead')

    def segment(s):
        return ''.join('[^/]*' if c == '*' else '[^/]' if c == '?' else re.escape(c) for c in s)

    if len(segments) == 1:
        return re.compile(r'(?:[^/]+/)*' + segment(pattern))
    out = []
    for i, s in enumerate(segments):
        last = i == len(segments) - 1
        if s == '**':
            out.append('.+' if last else '(?:[^/]+/)*')
        else:
            out.append(segment(s) + ('' if last else '/'))
    return re.compile(''.join(out))


def never_auto_regex(pattern):
    """A never_auto pattern as the regex the never_auto match uses. A deny
    list errs on the side of matching, so this is wider than glob_regex, whose
    syntax and refusals it shares:

    - A pattern without '/' matches when it matches any one path segment, a
      directory name or the file name: `*sudoers*` matches
      `etc/sudoers.d/ds-portal` and `packaging/sudoers/portal`, and with them
      everything beneath such a directory.
    - A pattern with '/' is matched against the whole path, as in glob_regex.
    - Either way upper and lower case are the same:
      `debian/ds-portal.SUDOERS` matches `*sudoers*`.
    - A trailing `**` takes everything beneath, a file name with a newline in
      it included (git allows one, and the paths arrive NUL-separated).
    """
    regex = glob_regex(pattern)
    source = regex.pattern
    if '/' not in pattern:
        source += r'(?:/[^/]+)*'
    # DOTALL: glob_regex writes a trailing ** as '.+', which stops at a newline without it
    return re.compile(source, re.IGNORECASE | re.DOTALL)


def _keys(obj, where, allowed):
    if not isinstance(obj, dict):
        raise CheckError(f'{CONFIG}: "{where}" must be an object')
    unknown = sorted(set(obj) - set(allowed))
    if unknown:
        raise CheckError(f'{CONFIG}: unknown key(s) {", ".join(unknown)} in "{where}" (known: {", ".join(allowed)})')


def _bool(obj, where, key):
    if not isinstance(obj[key], bool):
        raise CheckError(f'{CONFIG}: {where}.{key} must be true or false, got {obj[key]!r}')


def _names(obj, where, key):
    value = obj[key]
    if not isinstance(value, list) or not all(isinstance(x, str) and x and x == x.strip() for x in value):
        raise CheckError(f'{CONFIG}: {where}.{key} must be a list of non-empty strings')
    if len(set(value)) != len(value):
        raise CheckError(f'{CONFIG}: {where}.{key} has a duplicate')


def _globs(obj, where, key):
    _names(obj, where, key)
    for pattern in obj[key]:
        try:
            glob_regex(pattern)
        except CheckError as e:
            raise CheckError(f'{CONFIG}: {where}.{key}: {e}')
    if WORKFLOWS_GLOB not in obj[key]:
        raise CheckError(f'{CONFIG}: {where}.{key} must list {WORKFLOWS_GLOB} (no rule ever writes to a workflow file)')


def _merge_up(block):
    """The "merge_up" block (ENG-2234), validated, with every default filled in.
    Unknown keys are refused at every level, booleans must be JSON booleans
    (not "true", not 1) and the enums are closed."""
    where = 'merge_up'
    _keys(block, where, MERGE_UP_DEFAULTS)
    out = {}
    for name, default in MERGE_UP_DEFAULTS.items():
        if isinstance(default, dict):
            sub = block.get(name, {})
            _keys(sub, f'{where}.{name}', default)
            out[name] = dict({k: (list(v) if isinstance(v, list) else v) for k, v in default.items()}, **sub)
        else:
            out[name] = block.get(name, list(default))
    _bool(out['finish'], f'{where}.finish', 'strict')
    _names(out['finish'], f'{where}.finish', 'required_checks')
    lock = out['lock_pin']
    _bool(lock, f'{where}.lock_pin', 'verify_finish')
    for key, choices in (('exact', LOCK_PIN_EXACT), ('extra', LOCK_PIN_EXTRA)):
        if not isinstance(lock[key], str) or lock[key] not in choices:
            raise CheckError(f'{CONFIG}: {where}.lock_pin.{key} must be one of {", ".join(choices)}, got {lock[key]!r}')
    wait = lock['wait_minutes']
    if isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= LOCK_PIN_WAIT_MAX:
        raise CheckError(f'{CONFIG}: {where}.lock_pin.wait_minutes must be a whole number of minutes from 1 to {LOCK_PIN_WAIT_MAX} '
                         f'(the App token that merges after the wait lives 60 minutes), got {wait!r}')
    _globs(out, where, 'never_auto')
    _globs(out['meta_dsensos'], f'{where}.meta_dsensos', 'never_auto')
    _names(out['meta_dsensos'], f'{where}.meta_dsensos', 'required_checks')
    return out


def config(root):
    """release/config.json, validated, with every default filled in: the
    product list and the "merge_up" block. Unknown top-level keys are refused,
    as sensor-portal's and ds's version.py do."""
    doc = read_json(root, CONFIG)
    if not isinstance(doc, dict):
        raise CheckError(f'{CONFIG}: must be an object')
    unknown = sorted(set(doc) - set(CONFIG_KEYS))
    if unknown:
        raise CheckError(f'{CONFIG}: unknown key(s) {", ".join(unknown)} (known: {", ".join(CONFIG_KEYS)})')
    names = doc.get('products')
    if not isinstance(names, list) or not names:
        raise CheckError(f'{CONFIG}: "products" must be a non-empty list')
    for n in names:
        if not isinstance(n, str) or not PRODUCT_NAME.fullmatch(n):
            raise CheckError(f'{CONFIG}: product {n!r} must match {PRODUCT_NAME.pattern}')
    if len(set(names)) != len(names):
        raise CheckError(f'{CONFIG}: duplicate product')
    return {'products': list(names), 'merge_up': _merge_up(doc.get('merge_up', {}))}


def products(root):
    """The product list from release/config.json, validated (the whole file is:
    a bad "merge_up" block or an unknown key fails here too)."""
    return config(root)['products']


def merge_up(root):
    """The "merge_up" block of release/config.json, validated, defaults filled in."""
    return config(root)['merge_up']


def product_fragments(root):
    d = os.path.join(root, PRODUCT_DIR)
    if not os.path.isdir(d):
        raise CheckError(f'{PRODUCT_DIR}: missing')
    return sorted(f[:-len('.yml')] for f in os.listdir(d) if f.endswith('.yml'))


def versions_files(root):
    d = os.path.join(root, VERSIONS_DIR)
    if not os.path.isdir(d):
        raise CheckError(f'{VERSIONS_DIR}/: missing')
    return sorted(f for f in os.listdir(d) if f != 'README.md')


def read_version(root, product):
    """The pending version of a product: one semver core line in versions/<product>."""
    rel = f'{VERSIONS_DIR}/{product}'
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        raise CheckError(f'{rel}: missing')
    with open(path) as f:
        raw = f.read()
    if raw.endswith('\n'):
        raw = raw[:-1]
    if not SEMVER_CORE.fullmatch(raw):
        raise CheckError(f'{rel}: must be one X.Y.Z line (no v, no suffix), got {raw!r}')
    return raw


def write_version(root, product, version):
    with open(os.path.join(root, VERSIONS_DIR, product), 'w') as f:
        f.write(version + '\n')


def matrix(root):
    doc = read_json(root, MATRIX)
    prods = doc.get('products') if isinstance(doc, dict) else None
    if not isinstance(prods, dict):
        raise CheckError(f'{MATRIX}: "products" must be an object')
    out = {}
    for name, entry in prods.items():
        flavors = entry.get('flavors') if isinstance(entry, dict) else None
        if not isinstance(flavors, list) or not flavors or not all(isinstance(x, str) for x in flavors):
            raise CheckError(f'{MATRIX}: products.{name}.flavors must be a non-empty list of strings')
        if len(set(flavors)) != len(flavors):
            raise CheckError(f'{MATRIX}: products.{name}.flavors has a duplicate')
        out[name] = list(flavors)
    return out


def sdk_flavors(root):
    """The optional "sdk" list per product in release/matrix.json (ENG-2126): the
    release flavors whose PROMOTE build (the release build) also runs
    populate_sdk. RC builds never do (Dexter, 2026-10-30: the SDK is built only
    when a release is promoted; the one exception is a manual build.yml run with
    build_sdk=true). Absent or empty means no SDK. Each entry must be one of the
    product's flavors."""
    doc = read_json(root, MATRIX)
    prods = doc.get('products') if isinstance(doc, dict) else None
    if not isinstance(prods, dict):
        raise CheckError(f'{MATRIX}: "products" must be an object')
    flavors = matrix(root)
    out = {}
    for name, entry in prods.items():
        sdk = entry.get('sdk', []) if isinstance(entry, dict) else []
        if sdk is None:
            sdk = []
        if not isinstance(sdk, list) or not all(isinstance(x, str) for x in sdk):
            raise CheckError(f'{MATRIX}: products.{name}.sdk must be a list of flavor names (or absent)')
        if len(set(sdk)) != len(sdk):
            raise CheckError(f'{MATRIX}: products.{name}.sdk has a duplicate')
        for fl in sdk:
            if fl not in flavors[name]:
                raise CheckError(f'{MATRIX}: products.{name}.sdk lists {fl}, which is not in products.{name}.flavors')
        out[name] = list(sdk)
    return out


def matrix_include(root, product, sdk=False):
    """The strategy.matrix the build workflows take: {"include": [{"flavor": ...}, ...]}.
    With sdk=True (promote.sh --check, the release build) the entries the
    product's "sdk" list names carry "sdk": true, and no "sdk" key appears on
    the others. Without it (plan.py, the RC build) no entry carries the key, so
    an RC build never builds an SDK whatever the list says; the list is still
    validated."""
    m = matrix(root)
    if product not in m:
        raise CheckError(f'{MATRIX}: no product {product!r}')
    listed = sdk_flavors(root)[product]  # validated on both paths
    with_sdk = set(listed) if sdk else set()
    return {'include': [dict({'flavor': f}, **({'sdk': True} if f in with_sdk else {})) for f in m[product]]}


def committed_flavors(root):
    """kas/*.yml minus the personal and CI overlays (the ./run-kas list set)."""
    d = os.path.join(root, 'kas')
    names = (f[:-len('.yml')] for f in os.listdir(d) if f.endswith('.yml')) if os.path.isdir(d) else ()
    return sorted(n for n in names
                  if n not in ('local', 'ci-override', 'ci-strict', 'ci-version') and not n.endswith('.lock'))


def check(root):
    """Every violation found, as a list of messages (empty = ok)."""
    errors = []
    try:
        names = products(root)
    except CheckError as e:
        return [str(e)]
    frags = product_fragments(root)
    if sorted(names) != frags:
        errors.append(f'{CONFIG}: products {sorted(names)} != {PRODUCT_DIR}/*.yml stems {frags}')
    try:
        files = versions_files(root)
    except CheckError as e:
        return errors + [str(e)]
    for extra in sorted(set(files) - set(names)):
        errors.append(f'{VERSIONS_DIR}/{extra}: not a product in {CONFIG}')
    for p in names:
        try:
            read_version(root, p)
        except CheckError as e:
            errors.append(str(e))
    try:
        m = matrix(root)
        sdk_flavors(root)
    except CheckError as e:
        return errors + [str(e)]
    if sorted(m) != sorted(names):
        errors.append(f'{MATRIX}: products {sorted(m)} != {CONFIG} products {sorted(names)}')
    committed = committed_flavors(root)
    for p, flavors in sorted(m.items()):
        for fl in flavors:
            if fl.split('_', 1)[0] != p:
                errors.append(f'{MATRIX}: flavor {fl} is listed under product {p}')
            if fl not in committed:
                errors.append(f'{MATRIX}: flavor {fl} has no kas/{fl}.yml')
    return errors


def git(git_dir, *args):
    return subprocess.run(['git', '-C', git_dir, *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def resolve_sha(git_dir, sha):
    return git(git_dir, 'rev-parse', '--verify', f'{sha}^{{commit}}')


def commit_count(git_dir, sha):
    return int(git(git_dir, 'rev-list', '--count', sha))


def is_dirty(git_dir):
    """Any tracked file changed in the index or the worktree."""
    return bool(git(git_dir, 'status', '--porcelain', '--untracked-files=no'))


def rc_version(base, count, sha):
    return f'{base}-rc.{count}.github-{sha[:7]}'


def dev_version(base, count, sha, dirty):
    return f'{base}-dev.{count}.g{sha[:7]}' + ('.dirty' if dirty else '')


def bump(version, level):
    major, minor, patch = (int(x) for x in version.split('.'))
    if level == 'major':
        return f'{major + 1}.0.0'
    if level == 'minor':
        return f'{major}.{minor + 1}.0'
    if level == 'patch':
        return f'{major}.{minor}.{patch + 1}'
    raise ValueError(f'level must be one of {LEVELS}, got {level!r}')


def checked_string(s):
    """Refuse to print anything ds-ota or publish-swu.sh would have to sanitise."""
    if not VERSION_STRING.fullmatch(s) or s != s.lower() or '+' in s:
        raise CheckError(f'version string {s!r} is not lowercase X.Y.Z[-suffix]')
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default=SCRIPT_ROOT, help='repo root (default: this checkout)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('check')
    p.add_argument('check_root', nargs='?', metavar='ROOT', help='repo root to check (default: --root)')
    for name in ('rc', 'dev'):
        p = sub.add_parser(name)
        p.add_argument('product')
        p.add_argument('--sha', default='HEAD')
        p.add_argument('--git-dir', help='repository for the commit count and sha (default: --root)')
    p = sub.add_parser('bump')
    p.add_argument('product')
    p.add_argument('level', choices=LEVELS)
    p = sub.add_parser('matrix')
    p.add_argument('product')
    p = sub.add_parser('matrix-include')
    p.add_argument('product')
    p.add_argument('--sdk', action='store_true',
                   help='mark the product\'s "sdk" flavors with "sdk": true (the promote build; never the RC build)')
    p = sub.add_parser('products')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('config')
    p.add_argument('--key', help='print one value by dotted path, e.g. merge_up.finish.strict '
                                 '(a string bare, anything else as JSON)')
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    try:
        if args.cmd == 'check':
            root = os.path.abspath(args.check_root) if args.check_root else root
            errors = check(root)
            for e in errors:
                print(f'version.py: {e}', file=sys.stderr)
            if errors:
                return 1
            print(f'version.py: {root}: ok ({", ".join(products(root))})')
            return 0
        if args.cmd in ('rc', 'dev'):
            if args.product not in products(root):
                raise CheckError(f'{CONFIG}: no product {args.product!r}')
            git_dir = os.path.abspath(args.git_dir) if args.git_dir else root
            base = read_version(root, args.product)
            sha = resolve_sha(git_dir, args.sha)
            count = commit_count(git_dir, sha)
            if args.cmd == 'rc':
                out = rc_version(base, count, sha)
            else:
                out = dev_version(base, count, sha, is_dirty(git_dir))
            print(checked_string(out))
            return 0
        if args.cmd == 'bump':
            if args.product not in products(root):
                raise CheckError(f'{CONFIG}: no product {args.product!r}')
            new = bump(read_version(root, args.product), args.level)
            write_version(root, args.product, new)
            print(new)
            return 0
        if args.cmd == 'matrix':
            m = matrix(root)
            if args.product not in m:
                raise CheckError(f'{MATRIX}: no product {args.product!r}')
            print(json.dumps(m[args.product]))
            return 0
        if args.cmd == 'matrix-include':
            print(json.dumps(matrix_include(root, args.product, sdk=args.sdk), separators=(',', ':')))
            return 0
        if args.cmd == 'products':
            names = products(root)
            print(json.dumps(names) if args.json else '\n'.join(names))
            return 0
        if args.cmd == 'config':
            value = config(root)
            for part in (args.key.split('.') if args.key else ()):
                if not isinstance(value, dict) or part not in value:
                    raise CheckError(f'{CONFIG}: no key {args.key!r}')
                value = value[part]
            print(value if isinstance(value, str) else json.dumps(value, indent=None if args.key else 2))
            return 0
    except CheckError as e:
        print(f'version.py: {e}', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f'version.py: {" ".join(e.cmd)} failed: {e.stderr.strip()}', file=sys.stderr)
        return 1
    return 2


if __name__ == '__main__':
    sys.exit(main())
