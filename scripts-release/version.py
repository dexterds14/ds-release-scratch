#!/usr/bin/env python3
"""Pending versions and version strings for the release lines (ENG-1930, ENG-2110).

    version.py check [ROOT]                  validate release/config.json, versions/ and release/matrix.json
    version.py config [--key KEY]            release/config.json with every default filled in, as JSON
    version.py rc <product> [--sha SHA]      X.Y.Z-rc.<N>.github-<sha7>, N = git rev-list --count SHA
    version.py dev <product>                 X.Y.Z-dev.<N>.g<sha7>[.dirty]
    version.py bump <product> <major|minor|patch>
    version.py deb-version <string>          the dpkg Version of a build version (X.Y.Z-rc.1 -> X.Y.Z~rc.1)
    version.py matrix <product>              the product's release flavors as a JSON list
    version.py products [--json]             the products in release/config.json

ROOT defaults to the repo this script lives in; --root overrides it, and
--git-dir (rc, dev) the repository that supplies the commit count and sha.
Every string this prints is lowercase and never contains '+': ds-ota orders
versions by strict SemVer and the deb version keeps that order
(dev < rc.N < rc.N+1 < release under dpkg --compare-versions).

release/config.json carries everything repo-specific: `products` (one, in
single-product mode), `product_prefix` (false: lines are main, N.x.x, N.M.x
and tags vX.Y.Z; true: <product>/main and <product>/vX.Y.Z), the
`bookkeeping_files` a merge-up keeps target-side next to versions/, the
`artifact` a build must produce and an optional `version_probe` (a file
inside the artifact that must carry the build version). `config` prints the
validated document with the defaults filled in, so every script reads one
shape.

`artifact` is an object with a `kind`, and the rest of its keys depend on it:

    {"kind": "debs", "debs": [<package names>], "deb_arch": "arm64",
     "deb_version_rule": "recipe-pv"|"deb-version"}
        the build produces exactly these debs (the default kind; with no
        `artifact` the config means "debs, none listed"). `deb_version_rule`
        says how their dpkg Version relates to the build version (recipe-pv:
        a Version matching ^1.0+git, the recipe's PV; deb-version: it equals
        `deb-version <version>`).
        `version_probe` is {"deb": <one of debs>, "path", "pattern"}.
    {"kind": "install-trees", "services": [<names>], "info_file": "<basename>"}
        the build produces one install-tree tarball whose top level is exactly
        one directory per service plus the info file (ds_version= and
        ds_commit= lines the gate checks). `version_probe` is {"path",
        "pattern"}: the path inside the tarball.

The two key sets never mix: a debs key under kind install-trees (or the other
way round) is refused, as are the old top-level debs/deb_arch/deb_version_rule
keys, which moved under `artifact`.

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
# A semver core, no leading zeros, nothing else (no v, no pre-release, no build metadata).
SEMVER_CORE = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
# What the build workflow accepts as build_version and what ds-ota can order.
VERSION_STRING = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-z.-]+)?')
PRODUCT_NAME = re.compile(r'[a-z][a-z0-9]*')
# A Debian source or binary package name (Policy 5.6.1), and a dpkg architecture.
DEB_NAME = re.compile(r'[a-z0-9][a-z0-9+.-]+')
DEB_ARCH = re.compile(r'[a-z0-9-]+')
# <product>_<machine>_<type>[-tags...][-hardened]: the split lib.sh parse_flavor makes.
FLAVOR_NAME = re.compile(r'([a-z][a-z0-9]*)_([a-z0-9-]+)_([a-z0-9-]+)')
LEVELS = ('major', 'minor', 'patch')
DEB_VERSION_RULES = ('recipe-pv', 'deb-version')
ARTIFACT_KINDS = ('debs', 'install-trees')
# The keys of release/config.json "artifact" per kind, with the defaults
# `config` fills in (None: required). deb_arch has no default once debs are
# listed: the gate must not guess it.
ARTIFACT_KEYS = {
    'debs': {'debs': [], 'deb_arch': '', 'deb_version_rule': 'recipe-pv'},
    'install-trees': {'services': None, 'info_file': None},
}
# The keys the old flat shape had; they moved under "artifact" (the error names it).
LEGACY_ARTIFACT_KEYS = ('debs', 'deb_arch', 'deb_version_rule')
# Every key release/config.json may carry, with the default `config` fills in.
CONFIG_DEFAULTS = {
    'product_prefix': True,
    'bookkeeping_files': [],
    'artifact': {'kind': 'debs'},
    'version_probe': None,
}
CONFIG_KEYS = ('products',) + tuple(CONFIG_DEFAULTS)


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


def _str_list(doc, key, what, pattern=None):
    """A list of distinct non-empty strings under key (each matching pattern when given)."""
    value = doc[key]
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise CheckError(f'{CONFIG}: "{key}" must be a list of {what}')
    if pattern is not None:
        for x in value:
            if not pattern.fullmatch(x):
                raise CheckError(f'{CONFIG}: {key} entry {x!r} must match {pattern.pattern}')
    if len(set(value)) != len(value):
        raise CheckError(f'{CONFIG}: duplicate in "{key}"')
    return list(value)


def _artifact(doc):
    """The "artifact" object, validated per kind, with its defaults filled in."""
    art = doc['artifact']
    if not isinstance(art, dict) or not isinstance(art.get('kind'), str):
        raise CheckError(f'{CONFIG}: "artifact" must be an object with a "kind" ({"|".join(ARTIFACT_KINDS)})')
    kind = art['kind']
    if kind not in ARTIFACT_KINDS:
        raise CheckError(f'{CONFIG}: artifact.kind must be one of {ARTIFACT_KINDS}, got {kind!r}')
    allowed = ARTIFACT_KEYS[kind]
    unknown = sorted(set(art) - {'kind'} - set(allowed))
    if unknown:
        other = sorted(k for k in unknown if any(k in ARTIFACT_KEYS[o] for o in ARTIFACT_KINDS if o != kind))
        if other:
            raise CheckError(f'{CONFIG}: artifact key(s) {", ".join(other)} belong to another kind than {kind!r}; '
                             f'the kinds do not mix (kind {kind} takes {", ".join(allowed)})')
        raise CheckError(f'{CONFIG}: unknown artifact key(s) {", ".join(unknown)} (kind {kind} takes {", ".join(allowed)})')
    missing = sorted(k for k, default in allowed.items() if default is None and k not in art)
    if missing:
        raise CheckError(f'{CONFIG}: artifact kind {kind} needs {", ".join(missing)}')
    art = {'kind': kind, **{k: v for k, v in allowed.items() if v is not None}, **art}
    if kind == 'debs':
        debs = _str_list(art, 'debs', 'deb package names', DEB_NAME)
        if not isinstance(art['deb_arch'], str) or (art['deb_arch'] and not DEB_ARCH.fullmatch(art['deb_arch'])):
            raise CheckError(f'{CONFIG}: artifact.deb_arch must be a dpkg architecture ({DEB_ARCH.pattern})')
        if debs and not art['deb_arch']:
            raise CheckError(f'{CONFIG}: artifact.deb_arch is required when debs are listed')
        if art['deb_version_rule'] not in DEB_VERSION_RULES:
            raise CheckError(f'{CONFIG}: artifact.deb_version_rule must be one of {DEB_VERSION_RULES}, '
                             f'got {art["deb_version_rule"]!r}')
    else:
        services = _str_list(art, 'services', 'service names (the top-level directories of the tarball)', DEB_NAME)
        if not services:
            raise CheckError(f'{CONFIG}: artifact.services must list at least one service')
        info = art['info_file']
        if not isinstance(info, str) or not info or '/' in info:
            raise CheckError(f'{CONFIG}: artifact.info_file must be a file name at the top level of the tarball (no /)')
        if info in services:
            raise CheckError(f'{CONFIG}: artifact.info_file {info!r} is also listed in services')
    return art


def _version_probe(doc, art):
    """The optional "version_probe", validated against the artifact kind."""
    probe = doc['version_probe']
    if probe is None:
        return None
    keys = ['deb', 'path', 'pattern'] if art['kind'] == 'debs' else ['path', 'pattern']
    if not isinstance(probe, dict) or sorted(probe) != keys:
        raise CheckError(f'{CONFIG}: "version_probe" must be an object with exactly {", ".join(keys)} '
                         f'(artifact kind {art["kind"]})')
    if not all(isinstance(probe[k], str) and probe[k] for k in keys):
        raise CheckError(f'{CONFIG}: version_probe {", ".join(keys)} must be non-empty strings')
    if art['kind'] == 'debs' and probe['deb'] not in art['debs']:
        raise CheckError(f'{CONFIG}: version_probe.deb {probe["deb"]!r} is not in artifact.debs')
    if '{version}' not in probe['pattern']:
        raise CheckError(f'{CONFIG}: version_probe.pattern must contain {{version}}')
    return dict(probe)


def config(root):
    """release/config.json, validated, with every default filled in."""
    doc = read_json(root, CONFIG)
    if not isinstance(doc, dict):
        raise CheckError(f'{CONFIG}: must be an object')
    legacy = sorted(set(doc) & set(LEGACY_ARTIFACT_KEYS))
    if legacy:
        raise CheckError(f'{CONFIG}: {", ".join(legacy)} moved under "artifact" ({{"kind": "debs", ...}}); '
                         f'top-level {", ".join(LEGACY_ARTIFACT_KEYS)} are no longer read')
    unknown = sorted(set(doc) - set(CONFIG_KEYS))
    if unknown:
        raise CheckError(f'{CONFIG}: unknown key(s) {", ".join(unknown)} (known: {", ".join(CONFIG_KEYS)})')
    doc = dict(CONFIG_DEFAULTS, **doc)
    names = doc.get('products')
    if not isinstance(names, list) or not names:
        raise CheckError(f'{CONFIG}: "products" must be a non-empty list')
    for n in names:
        if not isinstance(n, str) or not PRODUCT_NAME.fullmatch(n):
            raise CheckError(f'{CONFIG}: product {n!r} must match {PRODUCT_NAME.pattern}')
    if len(set(names)) != len(names):
        raise CheckError(f'{CONFIG}: duplicate product')
    if not isinstance(doc['product_prefix'], bool):
        raise CheckError(f'{CONFIG}: "product_prefix" must be true or false')
    if not doc['product_prefix'] and len(names) != 1:
        raise CheckError(f'{CONFIG}: "product_prefix": false needs exactly one product (bare line names '
                         f'belong to one product), got {len(names)}')
    files = _str_list(doc, 'bookkeeping_files', 'repo paths')
    for p in files:
        if p.startswith('/') or '..' in p.split('/') or p.endswith('/') or p.startswith(VERSIONS_DIR + '/'):
            raise CheckError(f'{CONFIG}: bookkeeping file {p!r} must be a repo-relative file path outside {VERSIONS_DIR}/')
    doc['artifact'] = _artifact(doc)
    doc['version_probe'] = _version_probe(doc, doc['artifact'])
    return doc


def products(root):
    """The product list from release/config.json, validated."""
    return config(root)['products']


def product_prefix(root):
    """True when lines and tags carry the product name (release/config.json product_prefix)."""
    return config(root)['product_prefix']


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


def check(root):
    """Every violation found, as a list of messages (empty = ok)."""
    errors = []
    try:
        names = products(root)
    except CheckError as e:
        return [str(e)]
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
    except CheckError as e:
        return errors + [str(e)]
    if sorted(m) != sorted(names):
        errors.append(f'{MATRIX}: products {sorted(m)} != {CONFIG} products {sorted(names)}')
    for p, flavors in sorted(m.items()):
        for fl in flavors:
            fm = FLAVOR_NAME.fullmatch(fl)
            if not fm:
                errors.append(f'{MATRIX}: flavor {fl} is not <product>_<machine>_<type>')
            elif fm.group(1) != p:
                errors.append(f'{MATRIX}: flavor {fl} is listed under product {p}')
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
    """Refuse to print anything ds-ota would have to sanitise."""
    if not VERSION_STRING.fullmatch(s) or s != s.lower() or '+' in s:
        raise CheckError(f'version string {s!r} is not lowercase X.Y.Z[-suffix]')
    return s


def deb_version(s):
    """The dpkg Version a build version maps to: X.Y.Z stays, X.Y.Z-<suffix> becomes
    X.Y.Z~<suffix with every - replaced by .>. The tilde sorts a pre-release below the
    release and dpkg orders the dotted suffix the way SemVer does."""
    checked_string(s)
    core, sep, suffix = s.partition('-')
    if not sep:
        return core
    return f'{core}~{suffix.replace("-", ".")}'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default=SCRIPT_ROOT, help='repo root (default: this checkout)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('check')
    p.add_argument('check_root', nargs='?', metavar='ROOT', help='repo root to check (default: --root)')
    p = sub.add_parser('config')
    p.add_argument('--key', choices=CONFIG_KEYS, help='print one value (a string bare, anything else as JSON)')
    for name in ('rc', 'dev'):
        p = sub.add_parser(name)
        p.add_argument('product')
        p.add_argument('--sha', default='HEAD')
        p.add_argument('--git-dir', help='repository for the commit count and sha (default: --root)')
    p = sub.add_parser('bump')
    p.add_argument('product')
    p.add_argument('level', choices=LEVELS)
    p = sub.add_parser('deb-version')
    p.add_argument('string', help='a build version (X.Y.Z or X.Y.Z-<suffix>)')
    p = sub.add_parser('matrix')
    p.add_argument('product')
    p = sub.add_parser('products')
    p.add_argument('--json', action='store_true')
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
        if args.cmd == 'config':
            doc = config(root)
            if args.key:
                value = doc[args.key]
                print(value if isinstance(value, str) else json.dumps(value))
            else:
                print(json.dumps(doc))
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
        if args.cmd == 'deb-version':
            print(deb_version(args.string))
            return 0
        if args.cmd == 'matrix':
            m = matrix(root)
            if args.product not in m:
                raise CheckError(f'{MATRIX}: no product {args.product!r}')
            print(json.dumps(m[args.product]))
            return 0
        if args.cmd == 'products':
            names = products(root)
            print(json.dumps(names) if args.json else '\n'.join(names))
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
