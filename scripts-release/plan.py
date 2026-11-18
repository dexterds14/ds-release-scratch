#!/usr/bin/env python3
"""The rc-build plan (ENG-1931, ENG-2110): what one push to a release line builds.

    plan.py <ref> [--sha SHA] [--root ROOT] [--git-dir DIR] [--github-output FILE] [--json]

Classifies <ref> with lines.py (bare names in single-product mode:
main, N.x.x, N.M.x). For a ref that is not a release line the plan is
`skip=true` and nothing else: the workflow's branch filter `[0-9]*.x.x` is
only a glob, and the plan job is what keeps a near miss like 1foo.x.x from
building anything. For a line it prints, one `key=value` per line (the
GITHUB_OUTPUT format, also appended to --github-output when given):

    skip=false
    product=ds
    kind=main|minor|patch
    line=main
    rc_version=0.0.1-rc.<N>.github-<sha7>     (version.py rc <product> --sha SHA)
    pending=0.0.1                             (versions/<product> at the checkout)
    matrix={"include": [{"flavor": "..."}, ...]}   (strategy.matrix, from release/matrix.json)

--json prints the same as one JSON object instead. Stdlib only.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import lines  # noqa: E402
import version  # noqa: E402

KEYS = ('skip', 'product', 'kind', 'line', 'rc_version', 'pending', 'matrix')


def plan(ref, root, git_dir, sha='HEAD'):
    """The plan as a dict with the KEYS above (skip=True has only skip and line)."""
    cfg = version.config(root)
    parsed = lines.parse_line(ref, cfg['products'], cfg['product_prefix'])
    line = lines.strip_ref(ref)
    if parsed['kind'] == 'none':
        return {'skip': True, 'line': line}
    product = parsed['product']
    resolved = version.resolve_sha(git_dir, sha)
    rc = version.checked_string(version.rc_version(
        version.read_version(root, product), version.commit_count(git_dir, resolved), resolved))
    flavors = version.matrix(root)[product]
    return {
        'skip': False,
        'product': product,
        'kind': parsed['kind'],
        'line': line,
        'rc_version': rc,
        'pending': version.read_version(root, product),
        'matrix': {'include': [{'flavor': f} for f in flavors]},
    }


def output_lines(p):
    out = []
    for k in KEYS:
        if k not in p:
            continue
        v = p[k]
        if isinstance(v, bool):
            v = 'true' if v else 'false'
        elif isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(',', ':'))
        out.append(f'{k}={v}')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ref', help='the pushed branch (github.ref_name)')
    ap.add_argument('--sha', default='HEAD', help='the commit to build (default HEAD)')
    ap.add_argument('--root', default=SCRIPT_ROOT, help='repo root with release/ and versions/')
    ap.add_argument('--git-dir', help='repository for the commit count and sha (default: --root)')
    ap.add_argument('--github-output', help='append the key=value lines to this file too')
    ap.add_argument('--json', action='store_true', help='print one JSON object instead of key=value lines')
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    git_dir = os.path.abspath(args.git_dir) if args.git_dir else root
    try:
        p = plan(args.ref, root, git_dir, args.sha)
    except version.CheckError as e:
        print(f'plan.py: {e}', file=sys.stderr)
        return 1
    except Exception as e:  # subprocess.CalledProcessError from git
        print(f'plan.py: {e}', file=sys.stderr)
        return 1
    text = output_lines(p)
    if args.json:
        print(json.dumps(p))
    else:
        print('\n'.join(text))
    if args.github_output:
        with open(args.github_output, 'a') as f:
            f.write('\n'.join(text) + '\n')
    if p['skip']:
        print(f"plan.py: {p['line']} is not a release line; nothing to build", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
