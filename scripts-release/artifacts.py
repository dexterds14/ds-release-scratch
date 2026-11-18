#!/usr/bin/env python3
"""Build artifacts of an app repo (ENG-2110): the manifest a build leaves next to its files.

    artifacts.py build-manifest [--kind debs|install-trees] --out DIR --product P \
        --flavor F --machine M --sha SHA [--branch B] [--tag T] \
        [--build-version V] [--release-line L] [--run-id ID] [--run-url URL] \
        [--layer NAME=SHA ...] [--extra FILE ...] [--exclude=SUBSTR ...] \
        [--sdk-uri URI --sdk-sha256 HEX --sdk-machine MACHINE --sdk-os-release REL] \
        FILE...

(--exclude takes the = form: its values start with '-', e.g. --exclude=-dbg_.)

Two artifact kinds:

  debs (default)   FILE... are debs. Every deb (and every --extra file) is copied into
                   DIR, Package, Version and Architecture are read with dpkg-deb, and
                   artifacts.debs lists them. At least one deb must remain after
                   --exclude. dpkg-deb must be on PATH.
  install-trees    FILE is exactly one install-tree tarball (the sdk-build.sh output,
                   ds-sdk-build-<MACHINE>-<sha7>.tar.gz). artifacts.debs is [], the
                   tarball is recorded in artifacts.extra like any file and as
                   artifacts.install_tree: {key, services: [<its top-level directories,
                   sorted>]}, listed with tarfile and never extracted. Stdlib only.

Both write DIR/manifest.json and, last, DIR/SHA256SUMS listing every file in DIR including
manifest.json. DIR is what the build uploads as one S3 prefix (<machine>/<UTC>-<run_id>-
standard/) and as the run artifact, so the release gate can verify the build from the
manifest and the checksums alone.

manifest.json, schema 1 (the fields the OS build's manifest has, minus the image, plus
artifacts.*):

    {"schema": 1, "job_id": "...", "build_datetime": "20261029T120000Z",
     "buildparams": {"flavor": "ds_ds-arm64_release", "product": "ds",
                     "machine": "ds-arm64", "hardened": false, "build_type": "release",
                     "build_version": "", "release_line": "",
                     "sdk": {"uri": "s3://.../...toolchain-2.0.0.sh", "sha256": "<64 hex>",
                             "machine": "jetson-orin-nx-a603-8gb", "os_release": "v2.0.0"}},
     "commit_hashes": [{"name": "ds", "commit": "<40 hex>", "branch": "main", "tag": null},
                       {"name": "dsensos-sdk", "commit": "<sha256 of the installer>"},
                       {"name": "schemas", "commit": "<40 hex>"}],
     "run_url": "...",
     "artifacts": {"debs": [{"key": "deb/ds-utils_1.0+git0+abc_arm64.deb", "package": "ds-utils",
                             "version": "1.0+git0+abc", "architecture": "arm64",
                             "sha256": "<64 hex>", "bytes": 12345}, ...],
                   "extra": [{"key": "sdk-build-info.txt", "sha256": "...", "bytes": 0},
                             {"key": "ds-sdk-build-<MACHINE>-<sha7>.tar.gz", "sha256": "...", "bytes": 0}],
                   "install_tree": {"key": "ds-sdk-build-<MACHINE>-<sha7>.tar.gz",
                                    "services": ["ds-ais-publisher", "...", "ds-utils"]}}}

buildparams.sdk is present when any --sdk-* option is given (the SDK the services were
compiled against, so a build says which toolchain made it); artifacts.install_tree only for
--kind install-trees. Keys are relative to the prefix. The first commit_hashes entry is
named after the repo (the basename of GITHUB_REPOSITORY, or --product-repo), which is the
name the release gate looks up; the layers follow.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile

KINDS = ('debs', 'install-trees')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def deb_fields(path):
    out = subprocess.run(['dpkg-deb', '-f', path, 'Package', 'Version', 'Architecture'],
                         check=True, capture_output=True, text=True).stdout
    fields = {}
    for line in out.splitlines():
        k, _, v = line.partition(':')
        fields[k.strip().lower()] = v.strip()
    for k in ('package', 'version', 'architecture'):
        if not fields.get(k):
            raise SystemExit(f'artifacts.py: {path}: dpkg-deb reports no {k}')
    return fields


def tarball_top_level_dirs(path):
    """The top-level directory names of a tarball, sorted (listing only, nothing extracted)."""
    dirs = set()
    with tarfile.open(path, 'r:*') as tf:
        for m in tf:
            name = m.name
            while name.startswith('./'):
                name = name[2:]
            if not name or name == '.':
                continue
            first, sep, _rest = name.partition('/')
            if sep or m.isdir():
                dirs.add(first)
    return sorted(dirs)


def repo_name(args):
    """The name of the first commit_hashes entry: the repo the build was made
    from. rc-gate.sh looks it up as the basename of GITHUB_REPOSITORY, so
    that is the default; --product-repo overrides it, and the product stands
    in when neither is known (a build outside Actions)."""
    if args.product_repo:
        return args.product_repo
    gh_repo = os.environ.get('GITHUB_REPOSITORY', '')
    return gh_repo.rsplit('/', 1)[-1] or args.product


def copy_in(src, dst):
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    return {'key': os.path.basename(dst), 'sha256': sha256(dst), 'bytes': os.path.getsize(dst)}


def build_manifest(args):
    os.makedirs(args.out, exist_ok=True)
    debs = []
    extra = []
    install_tree = None
    if args.kind == 'debs':
        if not args.files:
            raise SystemExit('artifacts.py: --kind debs needs at least one deb')
        os.makedirs(os.path.join(args.out, 'deb'), exist_ok=True)
        seen = set()
        for src in sorted(args.files):
            name = os.path.basename(src)
            if any(x in name for x in args.exclude):
                continue
            if name in seen:
                raise SystemExit(f'artifacts.py: two debs named {name}')
            seen.add(name)
            dst = os.path.join(args.out, 'deb', name)
            shutil.copyfile(src, dst)
            f = deb_fields(dst)
            debs.append({'key': f'deb/{name}', 'package': f['package'], 'version': f['version'],
                         'architecture': f['architecture'], 'sha256': sha256(dst), 'bytes': os.path.getsize(dst)})
        if not debs:
            raise SystemExit('artifacts.py: no debs after --exclude; nothing to record')
    else:
        if len(args.files) != 1:
            raise SystemExit(f'artifacts.py: --kind install-trees wants exactly one tarball, got {len(args.files)}')
        src = args.files[0]
        if not tarfile.is_tarfile(src):
            raise SystemExit(f'artifacts.py: {src} is not a tarball')
        services = tarball_top_level_dirs(src)
        if not services:
            raise SystemExit(f'artifacts.py: {src} has no top-level directory; an install tree has one per service')
        rec = copy_in(src, os.path.join(args.out, os.path.basename(src)))
        extra.append(rec)
        install_tree = {'key': rec['key'], 'services': services}
    for src in args.extra:
        rec = copy_in(src, os.path.join(args.out, os.path.basename(src)))
        if any(e['key'] == rec['key'] for e in extra):
            raise SystemExit(f'artifacts.py: two files named {rec["key"]}')
        extra.append(rec)
    layers = []
    for item in args.layer:
        name, _, commit = item.partition('=')
        if not name or not commit:
            raise SystemExit(f'artifacts.py: --layer wants NAME=SHA, got {item!r}')
        layers.append({'name': name, 'commit': commit})
    buildparams = {
        'flavor': args.flavor,
        'product': args.product,
        'machine': args.machine,
        'hardened': False,
        'build_type': args.flavor.split('_')[-1] if '_' in args.flavor else '',
        'build_version': args.build_version,
        'release_line': args.release_line,
    }
    if args.sdk_uri or args.sdk_sha256 or args.sdk_machine or args.sdk_os_release:
        if not (args.sdk_uri and args.sdk_sha256):
            raise SystemExit('artifacts.py: --sdk-uri and --sdk-sha256 go together')
        buildparams['sdk'] = {'uri': args.sdk_uri, 'sha256': args.sdk_sha256,
                              'machine': args.sdk_machine, 'os_release': args.sdk_os_release}
    artifacts = {'debs': debs, 'extra': extra}
    if install_tree is not None:
        artifacts['install_tree'] = install_tree
    manifest = {
        'schema': 1,
        'job_id': args.run_id,
        'build_datetime': args.build_datetime or datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
        'buildparams': buildparams,
        'commit_hashes': [{'name': repo_name(args), 'commit': args.sha,
                           'branch': args.branch, 'tag': args.tag or None}] + layers,
        'run_url': args.run_url,
        'artifacts': artifacts,
    }
    mpath = os.path.join(args.out, 'manifest.json')
    with open(mpath, 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')
    # SHA256SUMS last: a reader that sees it knows the prefix is complete.
    lines = [f'{d["sha256"]}  {d["key"]}' for d in debs] + [f'{e["sha256"]}  {e["key"]}' for e in extra]
    lines.append(f'{sha256(mpath)}  manifest.json')
    with open(os.path.join(args.out, 'SHA256SUMS'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(json.dumps(manifest, indent=2))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build-manifest')
    b.add_argument('--kind', choices=KINDS, default='debs',
                   help='what FILE... are: debs (default) or one install-tree tarball')
    b.add_argument('--out', required=True)
    b.add_argument('--product', required=True)
    b.add_argument('--product-repo', default='',
                   help='name of the first commit_hashes entry (default: the basename of GITHUB_REPOSITORY, else the product)')
    b.add_argument('--flavor', required=True)
    b.add_argument('--machine', required=True)
    b.add_argument('--sha', required=True)
    b.add_argument('--branch', default='')
    b.add_argument('--tag', default='')
    b.add_argument('--build-version', default='')
    b.add_argument('--release-line', default='')
    b.add_argument('--run-id', default=os.environ.get('GITHUB_RUN_ID', ''))
    b.add_argument('--run-url', default='')
    b.add_argument('--build-datetime', default='')
    b.add_argument('--layer', action='append', default=[])
    b.add_argument('--extra', action='append', default=[])
    b.add_argument('--exclude', action='append', default=[])
    b.add_argument('--sdk-uri', default='', help='installer the services were built against (buildparams.sdk.uri)')
    b.add_argument('--sdk-sha256', default='', help='its sha256 (buildparams.sdk.sha256)')
    b.add_argument('--sdk-machine', default='', help='the Yocto MACHINE it targets (buildparams.sdk.machine)')
    b.add_argument('--sdk-os-release', default='', help='the OS release it came from, vX.Y.Z or pinned (buildparams.sdk.os_release)')
    b.add_argument('files', nargs='*', metavar='FILE')
    args = ap.parse_args(argv)
    if args.cmd == 'build-manifest':
        return build_manifest(args)
    return 2


if __name__ == '__main__':
    sys.exit(main())
