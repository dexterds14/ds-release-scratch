#!/usr/bin/env python3
"""Release line names (ENG-1930): <product>/main, <product>/N.x.x, <product>/N.M.x.

    lines.py parse <ref>                  {"product", "kind": main|minor|patch|none, "version_prefix"}
    lines.py parent <ref>                 the line a merge-up from <ref> targets
    lines.py children <ref>               the lines that merge up into <ref>, as a JSON list

parent and children need the branch list: `git ls-remote --heads` output (or
bare names, one per line) on stdin or in --heads-file. A patch line's parent
is its minor line when that branch exists, else the product's main; a minor
line's parent is main. Trees never cross: a product only sees lines whose
first segment is its own name, and the product must be in
release/config.json (--root, default this checkout).

Pure functions (parse_line, parent_of, children_of, parse_heads) take the
product list and the head names, so the tests need no network and no git.
"""
import argparse
import json
import os
import re
import sys

SCRIPT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NUM = r'(0|[1-9][0-9]*)'
MINOR_RE = re.compile(NUM + r'\.x\.x')
PATCH_RE = re.compile(NUM + r'\.' + NUM + r'\.x')
NONE = {'product': None, 'kind': 'none', 'version_prefix': None}


def strip_ref(ref):
    for prefix in ('refs/remotes/origin/', 'refs/heads/'):
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def parse_line(ref, products):
    """Classify a branch name. Anything that is not exactly a line of a configured product is kind none."""
    name = strip_ref(ref)
    product, sep, rest = name.partition('/')
    if not sep or product not in products or '/' in rest:
        return dict(NONE)
    if rest == 'main':
        return {'product': product, 'kind': 'main', 'version_prefix': ''}
    m = MINOR_RE.fullmatch(rest)
    if m:
        return {'product': product, 'kind': 'minor', 'version_prefix': f'{m.group(1)}.'}
    m = PATCH_RE.fullmatch(rest)
    if m:
        return {'product': product, 'kind': 'patch', 'version_prefix': f'{m.group(1)}.{m.group(2)}.'}
    return dict(NONE)


def parse_heads(text):
    """Branch names from `git ls-remote --heads` output or bare names, one per line."""
    heads = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name = line.split('\t', 1)[1] if '\t' in line else line.split()[-1]
        heads.add(strip_ref(name))
    return heads


def line_key(name):
    """Sort key: main first, then minor and patch lines by version."""
    rest = name.partition('/')[2]
    if rest == 'main':
        return (0,)
    return (1,) + tuple(int(x) for x in rest.split('.') if x != 'x')


def product_lines(product, heads, products):
    """Every line of one product among the heads, as {name: parsed}."""
    out = {}
    for h in heads:
        p = parse_line(h, products)
        if p['kind'] != 'none' and p['product'] == product:
            out[strip_ref(h)] = p
    return out


def parent_of(ref, heads, products):
    p = parse_line(ref, products)
    if p['kind'] == 'none':
        raise ValueError(f'{strip_ref(ref)} is not a release line')
    if p['kind'] == 'main':
        raise ValueError(f'{strip_ref(ref)} is a product main and has no parent line')
    product = p['product']
    if p['kind'] == 'minor':
        return f'{product}/main'
    minor = f'{product}/{p["version_prefix"].split(".")[0]}.x.x'
    if minor in product_lines(product, heads, products):
        return minor
    return f'{product}/main'


def children_of(ref, heads, products):
    """Lines whose parent_of is ref, in version order."""
    p = parse_line(ref, products)
    if p['kind'] == 'none':
        raise ValueError(f'{strip_ref(ref)} is not a release line')
    name = strip_ref(ref)
    lines = product_lines(p['product'], heads, products)
    kids = [n for n in lines if n != name and lines[n]['kind'] != 'main'
            and parent_of(n, heads, products) == name]
    return sorted(kids, key=line_key)


def load_products(root):
    sys.path.insert(0, os.path.join(root, 'scripts-release'))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import version  # noqa: E402  (sibling module)
    return version.products(root)


def read_heads(args):
    if args.heads_file:
        with open(args.heads_file) as f:
            return parse_heads(f.read())
    if sys.stdin.isatty():
        print('lines.py: no --heads-file and nothing on stdin; assuming no other lines exist', file=sys.stderr)
        return set()
    return parse_heads(sys.stdin.read())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default=SCRIPT_ROOT, help='repo root with release/config.json')
    ap.add_argument('--heads-file', help='git ls-remote --heads output (default: stdin)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('parse', 'parent', 'children'):
        sub.add_parser(name).add_argument('ref')
    args = ap.parse_args(argv)
    try:
        products = load_products(os.path.abspath(args.root))
        if args.cmd == 'parse':
            print(json.dumps(parse_line(args.ref, products)))
            return 0
        heads = read_heads(args)
        if args.cmd == 'parent':
            print(parent_of(args.ref, heads, products))
        else:
            print(json.dumps(children_of(args.ref, heads, products)))
        return 0
    except ValueError as e:
        print(f'lines.py: {e}', file=sys.stderr)
        return 1
    except Exception as e:  # version.CheckError from load_products
        print(f'lines.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
