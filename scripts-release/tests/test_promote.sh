#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/promote.sh (ENG-1934). Two bare repos in a temp
# dir stand in for ds-tegra-distro and meta-dsensos (lines are opened with
# the real create-branch.sh, children merged with the real merge-up.sh); a
# python stub gh on PATH answers run list from a green-sha list, keeps
# releases (with their assets) in a state dir, creates tags for
# `gh api -X POST .../git/refs` in the bare repo and logs every call; aws is
# the stub in tests/stubs (a bucket tree under FAKE_S3). No network, nothing
# touches this checkout.
#   bash scripts-release/tests/test_promote.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }

mkdir -p "${tmp}/bin"
cat > "${tmp}/bin/gh" <<'PYEOF'
#!/usr/bin/env python3
"""Stub gh for test_promote.sh: run list, release view/create/upload/edit/download, api git refs, pr list/create."""
import json, os, shutil, subprocess, sys
STATE = os.environ['GH_STUB_STATE']
REPOS = dict(kv.split('=', 1) for kv in os.environ['GH_STUB_REPOS'].split(','))
RELEASES = os.path.join(STATE, 'releases.json')
ASSETS = os.path.join(STATE, 'assets')
FLAGS = {'--draft', '--prerelease', '--clobber', '--merge', '--squash', '--rebase', '--delete-branch'}
VALUED = {'-R', '--repo', '--json', '--jq', '--pattern', '--dir', '--target', '--title', '--notes-file', '--workflow',
          '--commit', '--branch', '--status', '--limit', '--base', '--head', '--state', '--body-file', '-X', '-c'}


def parse(argv):
    pos, opts, fields = [], {}, {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '-f':
            k, _, v = argv[i + 1].partition('=')
            fields[k] = v
            i += 2
        elif a in FLAGS:
            opts[a] = True
            i += 1
        elif a.startswith('-') and '=' in a:
            k, _, v = a.partition('=')
            opts[k] = v
            i += 1
        elif a in VALUED:
            opts[a] = argv[i + 1]
            i += 2
        elif a.startswith('-'):
            opts[a] = True
            i += 1
        else:
            pos.append(a)
            i += 1
    return pos, opts, fields


def load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def save(path, doc):
    json.dump(doc, open(path, 'w'))


def main(argv):
    with open(os.path.join(STATE, 'log'), 'a') as f:
        f.write(' '.join(argv) + '\n')
    pos, opts, fields = parse(argv)
    repo = opts.get('-R') or opts.get('--repo')
    if pos[:2] == ['run', 'list']:
        green = set(open(os.environ['GH_STUB_GREEN']).read().split()) if os.environ.get('GH_STUB_GREEN') else set()
        sha = opts.get('--commit') or opts.get('-c')
        print(json.dumps([{'databaseId': 1234, 'number': 56}] if sha in green else []))
        return 0
    if pos[:1] == ['release']:
        sub, tag = pos[1], pos[2]
        rels = load(RELEASES, {})
        rel = rels.get(tag)
        if sub == 'view':
            if rel is None:
                print('release not found', file=sys.stderr)
                return 1
            doc = {'tagName': tag, 'isDraft': rel['draft'], 'isPrerelease': rel['prerelease'], 'name': rel.get('title', ''),
                   'targetCommitish': rel.get('target', '')}
            if opts.get('--jq'):
                v = doc[opts['--jq'].lstrip('.')]
                print(json.dumps(v) if isinstance(v, bool) else v)
            else:
                print(json.dumps({k: doc[k] for k in opts.get('--json', '').split(',') if k}))
            return 0
        if sub == 'download':
            if rel is None:
                print('release not found', file=sys.stderr)
                return 1
            pat = opts.get('--pattern')
            hits = [a for a in rel.get('assets', []) if not pat or a == pat]
            if not hits:
                print('no assets match the pattern', file=sys.stderr)
                return 1
            os.makedirs(opts.get('--dir', '.'), exist_ok=True)
            for a in hits:
                shutil.copy(os.path.join(ASSETS, tag.replace('/', '_'), a), os.path.join(opts.get('--dir', '.'), a))
            return 0
        if sub == 'create':
            if os.environ.get('GH_STUB_FAIL_CREATE') == '1':
                print('HTTP 422: Tag name creation is restricted by a ruleset', file=sys.stderr)
                return 1
            if rel is not None:
                print('release already exists', file=sys.stderr)
                return 1
            rels[tag] = {'draft': bool(opts.get('--draft')), 'prerelease': bool(opts.get('--prerelease')),
                         'title': opts.get('--title', ''), 'target': opts.get('--target', ''),
                         'notes': open(opts['--notes-file']).read() if opts.get('--notes-file') else '', 'assets': []}
            save(RELEASES, rels)
            return 0
        if sub == 'upload':
            if rel is None:
                print('release not found', file=sys.stderr)
                return 1
            d = os.path.join(ASSETS, tag.replace('/', '_'))
            os.makedirs(d, exist_ok=True)
            for p in pos[3:]:
                if not os.path.isfile(p):
                    print('asset %s does not exist' % p, file=sys.stderr)
                    return 1
                shutil.copy(p, os.path.join(d, os.path.basename(p)))
                if os.path.basename(p) not in rel['assets']:
                    rel['assets'].append(os.path.basename(p))
            save(RELEASES, rels)
            return 0
        if sub == 'edit':
            if rel is None:
                print('release not found', file=sys.stderr)
                return 1
            if opts.get('--draft') == 'false':
                rel['draft'] = False
            if opts.get('--prerelease') is True:
                rel['prerelease'] = True
            elif opts.get('--prerelease') == 'false':
                rel['prerelease'] = False
            save(RELEASES, rels)
            return 0
    if pos[:1] == ['api']:
        path = pos[1]
        parts = path.split('/')
        bare = REPOS[parts[1] + '/' + parts[2]]
        if opts.get('-X') == 'POST' and path.endswith('/git/refs'):
            if os.environ.get('GH_STUB_FAIL_TAG') == '1':
                print('HTTP 422: Tag name creation is restricted by a ruleset', file=sys.stderr)
                return 1
            ref, sha = fields['ref'], fields['sha']
            r = subprocess.run(['git', '-C', bare, 'show-ref', '--verify', '--quiet', ref])
            if r.returncode == 0:
                print('HTTP 422: Reference already exists', file=sys.stderr)
                return 1
            subprocess.check_call(['git', '-C', bare, 'update-ref', ref, sha])
            print(json.dumps({'ref': ref, 'object': {'sha': sha, 'type': 'commit'}}))
            return 0
    if pos[:2] == ['pr', 'list']:
        print(json.dumps([]) if not opts.get('--jq') else '')
        return 0
    if pos[:2] == ['pr', 'create']:
        print('https://github.com/%s/pull/1' % repo)
        return 0
    print('stub gh: unsupported: ' + ' '.join(argv), file=sys.stderr)
    return 1


sys.exit(main(sys.argv[1:]))
PYEOF
chmod +x "${tmp}/bin/gh"

export PATH="${tmp}/bin:${here}/stubs:${PATH}"
export FAKE_S3="${tmp}/s3" AWS_CALLS="${tmp}/aws.calls"
export DS_ARTIFACTS_BUCKET=ds-yocto-artifacts
export DISTRO_REPO=ds/distro META_REPO=ds/meta GITHUB_REPOSITORY=ds/distro
export GITHUB_SERVER_URL=https://github.com GITHUB_RUN_ID=777
export TOKEN=stub-token TOKEN_KIND=app GH_TOKEN=stub-token DRY_RUN=false
export GH_STUB_STATE="${tmp}/gh-state" GH_STUB_GREEN="${tmp}/gh-state/green"
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
MACHINE=jetson-orin-nx-a603-8gb
FLAVOR_STD=vertex_${MACHINE}_release
FLAVOR_HARD=vertex_${MACHINE}_release-hardened
FAKE_SHA=$(printf 'c%.0s' $(seq 64))
DISTRO_BARE="${tmp}/distro.git" META_BARE="${tmp}/meta.git" DISTRO="${tmp}/distro" META_WORK="${tmp}/meta-work"
export META_DSENSOS_URL="${META_BARE}" GH_STUB_REPOS="ds/distro=${DISTRO_BARE},ds/meta=${META_BARE}"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x

BASE_YML='# test base.yml
header:
  version: 23
repos:
  ds-tegra-distro:
    path: .
  meta-dsensos:
    url: git@github.com:DistributedSpectrum/meta-dsensos.git
    branch: trunk   # tracks the trunk
    path: repos/meta-dsensos
'
lock_yml() { printf 'header:\n  version: 14\noverrides:\n  repos:\n    meta-dsensos:\n      commit: %s\n' "$1"; }

# fixture: fresh bare origins (meta trunk m1,m2; distro trunk seeded with the release files), a
# distro clone the scripts run from, empty gh state and S3.
fixture() {
  rm -rf "${DISTRO_BARE}" "${META_BARE}" "${DISTRO}" "${META_WORK}" "${GH_STUB_STATE}" "${FAKE_S3}" "${tmp}/seed"
  mkdir -p "${GH_STUB_STATE}" "${BUCKET}"
  : > "${GH_STUB_GREEN}"; : > "${GH_STUB_STATE}/log"; : > "${AWS_CALLS}"
  git init -q --bare -b trunk "${META_BARE}"
  git init -q -b trunk "${META_WORK}"
  echo m1 > "${META_WORK}/layer.txt"; git -C "${META_WORK}" add -A; git -C "${META_WORK}" commit -q -m m1
  echo m2 > "${META_WORK}/layer.txt"; git -C "${META_WORK}" add -A; git -C "${META_WORK}" commit -q -m m2
  M2=$(git -C "${META_WORK}" rev-parse HEAD)
  git -C "${META_WORK}" remote add origin "${META_BARE}"; git -C "${META_WORK}" push -q origin trunk
  git init -q --bare -b trunk "${DISTRO_BARE}"
  local seed="${tmp}/seed"
  git init -q -b trunk "${seed}"
  mkdir -p "${seed}/kas/include" "${seed}/release" "${seed}/versions"
  printf '%s' "${BASE_YML}" > "${seed}/kas/include/base.yml"
  lock_yml "${M2}" > "${seed}/kas/include/base.lock.yml"
  echo '{"products": ["vertex", "aggregator"]}' > "${seed}/release/config.json"
  # vertex lists its standard flavor under "sdk" (ENG-2126): the promote build
  # alone builds the SDK, so --check must put sdk: true on that entry.
  printf '{"products": {"vertex": {"flavors": ["%s", "%s"], "sdk": ["%s"]}, "aggregator": {"flavors": ["aggregator_%s_release"]}}}\n' \
    "${FLAVOR_STD}" "${FLAVOR_HARD}" "${FLAVOR_STD}" "${MACHINE}" > "${seed}/release/matrix.json"
  echo 0.0.1 > "${seed}/versions/vertex"; echo 0.0.1 > "${seed}/versions/aggregator"; echo '# versions' > "${seed}/versions/README.md"
  echo trunk > "${seed}/foo.txt"
  git -C "${seed}" add -A; git -C "${seed}" commit -q -m seed
  SEED_SHA=$(git -C "${seed}" rev-parse HEAD)
  git -C "${seed}" remote add origin "${DISTRO_BARE}"; git -C "${seed}" push -q origin trunk
  git clone -q "${DISTRO_BARE}" "${DISTRO}"
}

head_of() { git -C "${DISTRO_BARE}" rev-parse --verify --quiet "refs/heads/$1" || echo none; }
tag_of() { git -C "${DISTRO_BARE}" rev-parse --verify --quiet "refs/tags/$1^{commit}" || echo none; }
show() { git -C "${DISTRO_BARE}" show "$1:$2"; }
files_in() { git -C "${DISTRO_BARE}" diff-tree --no-commit-id --name-only -r "$1" | paste -sd ' '; }
subject() { git -C "${DISTRO_BARE}" log -1 --format=%s "$1"; }
mark_green() { echo "$1" >> "${GH_STUB_GREEN}"; }
tag() { git -C "${DISTRO_BARE}" update-ref "refs/tags/$1" "$2"; }

# distro_commit <branch> <msg> <path=content>... -> sha (pushed)
distro_commit() {
  local branch=$1 msg=$2; shift 2
  git -C "${DISTRO}" fetch -q origin
  git -C "${DISTRO}" checkout -q -B "${branch}" "origin/${branch}"
  for kv in "$@"; do mkdir -p "$(dirname "${DISTRO}/${kv%%=*}")"; printf '%s\n' "${kv#*=}" > "${DISTRO}/${kv%%=*}"; done
  git -C "${DISTRO}" add -A; git -C "${DISTRO}" commit -q -m "${msg}"
  git -C "${DISTRO}" push -q origin "${branch}"
  git -C "${DISTRO}" rev-parse HEAD
}

seed_vertex() { (cd "${DISTRO}" && "${scripts}/create-branch.sh" --product vertex --seed-from trunk > "${tmp}/create.log" 2>&1) || { cat "${tmp}/create.log"; return 1; }; }
open_line() { (cd "${DISTRO}" && "${scripts}/create-branch.sh" --product vertex --version "$1" --kind "$2" > "${tmp}/create.log" 2>&1) || { cat "${tmp}/create.log"; return 1; }; }

# make_rc <sha> <pending> [<line>]: the rc/vertex/<sha7> tag at the sha, a published pre-release
# with a release-manifest.json asset (run 4242).
make_rc() {
  local sha=$1 pending=$2 line=${3:-vertex/main} tagname="rc/vertex/${1:0:7}"
  tag "${tagname}" "${sha}"
  python3 - "${GH_STUB_STATE}" "${tagname}" "${sha}" "${pending}" "${line}" "${FAKE_SHA}" "${MACHINE}" "${FLAVOR_STD}" <<'EOF'
import json, os, sys
state, tagname, sha, pending, line, fake, machine, flavor = sys.argv[1:9]
d = os.path.join(state, 'assets', tagname.replace('/', '_'))
os.makedirs(d, exist_ok=True)
files = {n: {'key': n, 'sha256': fake, 'bytes': 1} for n in ('image', 'deb_feed', 'package_list', 'manifest', 'sha256sums')}
doc = {'schema': 1, 'kind': 'rc', 'product': 'vertex', 'version': f'{pending}-rc.9.github-{sha[:7]}', 'pending': pending,
       'sha': sha, 'line': line, 'run_id': '4242', 'run_url': 'https://github.com/ds/distro/actions/runs/4242',
       'builds': [{'flavor': flavor, 'machine': machine, 'hardened': False, 'build_id': '20261021T110000Z-4242-standard',
                   's3_prefix': f'{machine}/20261021T110000Z-4242-standard', 'files': files}]}
json.dump(doc, open(os.path.join(d, 'release-manifest.json'), 'w'))
p = os.path.join(state, 'releases.json')
rels = json.load(open(p)) if os.path.exists(p) else {}
rels[tagname] = {'draft': False, 'prerelease': True, 'title': f'RC vertex {doc["version"]}', 'target': sha, 'notes': '',
                 'assets': ['release-manifest.json']}
json.dump(rels, open(p, 'w'))
EOF
}
# release_set <tag> <field> <json value> / release_del <tag> / release_get <tag> <field>
release_set() { python3 -c 'import json,sys; p=sys.argv[1]; r=json.load(open(p)); r[sys.argv[2]][sys.argv[3]]=json.loads(sys.argv[4]); json.dump(r, open(p,"w"))' "${GH_STUB_STATE}/releases.json" "$@"; }
release_del() { python3 -c 'import json,sys; p=sys.argv[1]; r=json.load(open(p)); r.pop(sys.argv[2], None); json.dump(r, open(p,"w"))' "${GH_STUB_STATE}/releases.json" "$@"; }
release_get() { python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); v=r.get(sys.argv[2], {}).get(sys.argv[3], "none"); print(json.dumps(v) if isinstance(v,(bool,list,dict)) else v)' "${GH_STUB_STATE}/releases.json" "$@"; }
release_exists() { python3 -c 'import json,sys,os; p=sys.argv[1]; r=json.load(open(p)) if os.path.exists(p) else {}; print("yes" if sys.argv[2] in r else "no")' "${GH_STUB_STATE}/releases.json" "$1"; }

# run_promote <args...> -> rc, log, out (GITHUB_OUTPUT file)
run_promote() {
  log="${tmp}/log"; out="${tmp}/out"
  : > "${out}"; rc=0
  (cd "${DISTRO}" && env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL -u GIT_COMMITTER_NAME -u GIT_COMMITTER_EMAIL \
    "${scripts}/promote.sh" "$@" --github-output "${out}") > "${log}" 2>&1 || rc=$?
}
outval() { sed -n "s/^$1=//p" "${out}" | tail -1; }
gh_count() { grep -c -E -- "$1" "${GH_STUB_STATE}/log" || true; }

# make_build <build_id> <flavor>: manifest.json and SHA256SUMS in a per-build prefix (what publish downloads)
make_build() {
  local d="${BUCKET}/${MACHINE}/$1"
  mkdir -p "${d}"
  printf '{"buildparams": {"flavor": "%s"}}\n' "$2" > "${d}/manifest.json"
  printf '%s  manifest.json\n' "${FAKE_SHA}" > "${d}/SHA256SUMS"
}
# release_manifest <out> <sha> <version> [<line>]: what rc-gate.sh --kind release writes, for two flavors of run 777
release_manifest() {
  python3 - "$1" "$2" "$3" "${4:-vertex/main}" "${FAKE_SHA}" "${MACHINE}" "${FLAVOR_STD}" "${FLAVOR_HARD}" <<'EOF'
import json, sys
out, sha, ver, line, fake, machine, std, hard = sys.argv[1:9]
def build(flavor, build_id, hardened):
    files = {'image': {'key': 'image-files/x.tar.gz', 'sha256': fake, 'bytes': 600000000},
             'deb_feed': {'key': 'deb/deb-feed.tar.gz', 'sha256': fake, 'bytes': 8},
             'package_list': {'key': 'deb/package-list.txt', 'sha256': fake, 'bytes': 12},
             'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 100},
             'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 300}}
    if not hardened:
        # the exported SDK the promote build made for the "sdk" flavor (ENG-2126)
        stem = f'sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-{machine}-toolchain-{ver}'
        files['sdk'] = {'key': f'{stem}.sh', 'sha256': fake, 'bytes': 2000000000}
        files['sdk_host_manifest'] = {'key': f'{stem}.host.manifest', 'sha256': fake, 'bytes': 10}
        files['sdk_target_manifest'] = {'key': f'{stem}.target.manifest', 'sha256': fake, 'bytes': 12}
    return {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id,
            's3_prefix': f'{machine}/{build_id}', 'files': files}
doc = {'schema': 1, 'kind': 'release', 'product': 'vertex', 'version': ver, 'pending': ver, 'sha': sha, 'line': line,
       'run_id': '777', 'run_url': 'https://github.com/ds/distro/actions/runs/777',
       'rc_run': {'run_id': '4242', 'run_url': 'https://github.com/ds/distro/actions/runs/4242', 'tag': f'rc/vertex/{sha[:7]}'},
       'release_run': {'run_id': '777', 'run_url': 'https://github.com/ds/distro/actions/runs/777'},
       'builds': [build(std, '20261022T100000Z-777-standard', False), build(hard, '20261022T100500Z-777-hardened', True)]}
json.dump(doc, open(out, 'w'), indent=2)
EOF
}

# =============================================================================
echo "# check: a green main tip with its RC"
fixture; seed_vertex
T=$(head_of vertex/main); mark_green "${T}"; make_rc "${T}" 0.0.1
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 0 "exits 0"
check "$(outval proceed)" true "proceed=true"
check "$(outval product)|$(outval line)|$(outval kind)|$(outval sha)" "vertex|vertex/main|main|${T}" "product, line, kind, sha"
check "$(outval version)|$(outval tag)|$(outval rc_tag)" "0.0.1|vertex/v0.0.1|rc/vertex/${T:0:7}" "version, tag, rc_tag"
check "$(outval bump)|$(outval next_version)" "major|1.0.0" "auto bump on main is major, next 1.0.0"
check "$(outval rc_run_id)|$(outval rc_run_url)" "4242|https://github.com/ds/distro/actions/runs/4242" "rc run from the RC manifest"
check "$(outval matrix)" "{\"include\":[{\"flavor\":\"${FLAVOR_STD}\",\"sdk\":true},{\"flavor\":\"${FLAVOR_HARD}\"}]}" "matrix is the product's release flavors, sdk: true on the \"sdk\" flavor only (the promote build builds the SDK)"
contains "${log}" "sdk:                ${FLAVOR_STD} \(promote builds it; RC builds never do\)" "the plan names the SDK flavor"
contains "${log}" "green sha:          ${T} \(run 56, id 1234\)" "names the green run"
contains "${log}" "children:           \(none\)" "no children"
contains "${log}" "bump:               major: versions/vertex 0.0.1 -> 1.0.0, commit \"release: vertex v0.0.1 promoted; pending 1.0.0 \[skip ci\]\"" "prints the bump commit"
contains "${log}" "release:            \"vertex v0.0.1 \(staging\)\"" "prints the release title"
check "$(gh_count '^release create|^release upload|^release edit|^api')" 0 "the check creates nothing"
run_promote --check --line vertex/main --no-dry-run --bump patch
check "${rc}|$(outval bump)|$(outval next_version)" "0|patch|0.0.2" "bump=patch on main: next 0.0.2 (the pilot's path)"
run_promote --check --line vertex/main --no-dry-run --bump minor
check "${rc}|$(outval next_version)" "0|0.1.0" "bump=minor on main: next 0.1.0"
run_promote --check --line vertex/main --no-dry-run --sha "${SEED_SHA}"
check "${rc}" 1 "a dispatch sha that is not the tip exits 1"
contains "${log}" "dispatched at ${SEED_SHA} but vertex/main is now at ${T}" "names both shas"
run_promote --check --line trunk --no-dry-run
check "${rc}" 1 "a non-line ref exits 1"
contains "${log}" "trunk is not a release line" "names the ref"
run_promote --check --line vertex/main --no-dry-run --bump release
check "${rc}" 1 "a bad bump level exits 1"

echo "# check: dry run creates nothing and sets proceed=false"
run_promote --check --line vertex/main --dry-run
check "${rc}|$(outval proceed)" "0|false" "exit 0, proceed=false"
contains "${log}" "dry run: nothing built, tagged, released or pushed" "dry-run notice"
check "$(outval tag)|$(outval sha)" "vertex/v0.0.1|${T}" "the plan outputs are still there"
check "$(gh_count '^release create|^release upload|^release edit|^api')" 0 "no gh create/upload/edit/api call"
check "$(grep -c 's3 cp' "${AWS_CALLS}" || true)" 0 "no S3 copy"
check "$(head_of vertex/main)|$(tag_of vertex/v0.0.1)" "${T}|none" "line and tags untouched"

echo "# check: refusals (red tip, missing or wrong RC release, RC manifest, existing tag)"
: > "${GH_STUB_GREEN}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "red tip exits 1"
contains "${log}" "::error::promote: the tip of vertex/main is not green: no successful release-rc-build.yml run on vertex/main for ${T}" "names the candidates"
mark_green "${T}"
release_del "rc/vertex/${T:0:7}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "missing RC release exits 1"
contains "${log}" "no pre-release rc/vertex/${T:0:7} for the green sha" "names the RC tag"
make_rc "${T}" 0.0.1
release_set "rc/vertex/${T:0:7}" draft true
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "draft RC exits 1"
contains "${log}" "is still a draft" "says draft"
release_set "rc/vertex/${T:0:7}" draft false
release_set "rc/vertex/${T:0:7}" prerelease false
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "non-pre-release RC exits 1"
contains "${log}" "is not a pre-release" "says so"
release_set "rc/vertex/${T:0:7}" prerelease true
tag "rc/vertex/${T:0:7}" "${SEED_SHA}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "RC tag at another sha exits 1"
contains "${log}" "tag rc/vertex/${T:0:7} points at '${SEED_SHA}', not the green sha ${T}" "names both"
tag "rc/vertex/${T:0:7}" "${T}"
make_rc "${T}" 0.0.5   # manifest disagrees with versions/vertex at the sha
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "RC manifest pending mismatch exits 1"
contains "${log}" "says pending 0.0.5, versions/vertex at ${T:0:7} says 0.0.1" "names both"
make_rc "${T}" 0.0.1
tag vertex/v0.0.1 "${T}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "existing tag at the same sha exits 1"
contains "${log}" "tag vertex/v0.0.1 already exists at ${T:0:7}: this sha was already promoted" "names the sha"
tag vertex/v0.0.1 "${SEED_SHA}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "existing tag at another sha exits 1"
contains "${log}" "tag vertex/v0.0.1 already exists at ${SEED_SHA:0:7}; versions/vertex on vertex/main \(0.0.1\) is a version that was already released" "names the sha"
git -C "${DISTRO_BARE}" update-ref -d refs/tags/vertex/v0.0.1
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 0 "back to green once the tag is gone"

echo "# check: a versions-only tip is refused (the release build runs at the tip, not at the older green sha)"
V=$(distro_commit vertex/main "release: pending moved [skip ci]" versions/aggregator=0.0.2)
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "a versions-only tip over a green sha exits 1"
contains "${log}" "::error::promote: the tip ${V:0:7} of vertex/main only touches versions/ and has no successful release-rc-build.yml run \(the last green sha is ${T:0:7}\); the release build would run at the tip, so re-run release-rc-build.yml on vertex/main" "names the tip, the green sha and the fix"
check "$(outval proceed)" "" "no proceed output"
tag vertex/v0.0.1 "${T}"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "the previous promote's bump commit as the tip exits 1"
contains "${log}" "::error::promote: nothing new to promote on vertex/main since vertex/v0.0.1 \(the tip ${V:0:7} only touches versions/; vertex/v0.0.1 is at ${T:0:7}\)" "says nothing new since the tag"
check "$(outval proceed)" "" "no proceed output"

echo "# check: children (bookkeeping-only child is merged; a child fix is not; merge_children merges and stops)"
fixture; seed_vertex
T=$(head_of vertex/main); mark_green "${T}"; make_rc "${T}" 0.0.1
tag vertex/v0.0.1 "${T}"
open_line 0.0.1 minor
L=$(head_of vertex/0.x.x)
check "$(show "${L}" versions/vertex)" 0.1.0 "vertex/0.x.x opened from v0.0.1 with pending 0.1.0 (relaxed line rule)"
git -C "${DISTRO_BARE}" update-ref -d refs/tags/vertex/v0.0.1
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 0 "a child with only its opening commit counts as merged"
contains "${log}" "child vertex/0.x.x: merged \(nothing but bookkeeping" "says so"
C=$(distro_commit vertex/0.x.x "child fix" foo.txt=child)
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "an unmerged child exits 1"
contains "${log}" "::error::promote: child line\(s\) not merged into vertex/main: vertex/0.x.x" "names the child"
contains "${log}" "child vertex/0.x.x: 1 commit\(s\) not in vertex/main:" "counts the commits"
contains "${log}" "    ${C:0:7} child fix" "lists the commit"
check "$(outval proceed)" "" "no proceed output"
run_promote --check --line vertex/main --no-dry-run --merge-children
check "${rc}" 1 "merge_children with a child that is not green exits 1"
contains "${log}" "no successful release-rc-build.yml run on vertex/0.x.x" "merge-up's green check refused"
contains "${log}" "merge-up of vertex/0.x.x into vertex/main failed" "promote names the child"
check "$(head_of vertex/main)" "${T}" "nothing merged"
mark_green "${C}"
run_promote --check --line vertex/main --dry-run --merge-children
check "${rc}|$(outval proceed)" "0|false" "merge_children dry run: exit 0, proceed=false"
contains "${log}" "dry run: would merge 1 child line\(s\) and stop" "says what it would do"
contains "${log}" "== merge-up plan \(dry_run=true" "ran merge-up's dry run"
check "$(head_of vertex/main)" "${T}" "nothing merged in the dry run"
run_promote --check --line vertex/main --no-dry-run --merge-children
check "${rc}" 1 "merge_children merges and exits 1 by design"
contains "${log}" "::error::promote: merged 1 child line\(s\) into vertex/main \(vertex/0.x.x\); the new tip needs its own Action A run \(release-rc-build.yml\), re-run promote when it is green" "the stop message"
M=$(head_of vertex/main)
check "$(git -C "${DISTRO_BARE}" rev-parse "${M}^2")|$(git -C "${DISTRO_BARE}" rev-parse "${M}^1")" "${C}|${T}" "vertex/main is now the merge of the child"
check "$(show "${M}" versions/vertex)|$(show "${M}" foo.txt)" "0.0.1|child" "target's versions/ kept, child's fix carried"
check "$(outval proceed)" "" "no proceed output after merging"
run_promote --check --line vertex/main --no-dry-run
check "${rc}" 1 "the new tip is not green yet"
contains "${log}" "not green" "says so"

echo "# check: version shape on minor and patch lines, bump levels that do not fit"
fixture; seed_vertex
T=$(head_of vertex/main); mark_green "${T}"; make_rc "${T}" 0.0.1
tag vertex/v0.0.1 "${T}"
open_line 0.0.1 minor
L=$(head_of vertex/0.x.x); mark_green "${L}"; make_rc "${L}" 0.1.0 vertex/0.x.x
run_promote --check --line vertex/0.x.x --no-dry-run
check "${rc}|$(outval kind)|$(outval version)|$(outval tag)|$(outval bump)|$(outval next_version)" "0|minor|0.1.0|vertex/v0.1.0|minor|0.2.0" "minor line: 0.1.0 -> tag, auto bump minor, next 0.2.0"
run_promote --check --line vertex/0.x.x --no-dry-run --bump patch
check "${rc}" 1 "bump=patch on a minor line exits 1"
contains "${log}" "bump patch after 0.1.0 leaves pending 0.1.1, which does not fit vertex/0.x.x \(0.M.0\); use bump=minor" "names the fix"
run_promote --check --line vertex/0.x.x --no-dry-run --bump major
check "${rc}" 1 "bump=major on a minor line exits 1"
B=$(distro_commit vertex/0.x.x "bad pending" versions/vertex=0.1.5 foo.txt=x)
mark_green "${B}"; make_rc "${B}" 0.1.5 vertex/0.x.x
run_promote --check --line vertex/0.x.x --no-dry-run
check "${rc}" 1 "pending 0.1.5 on a minor line exits 1"
contains "${log}" "pending version 0.1.5 does not fit vertex/0.x.x \(a minor line promotes 0.M.0\)" "names the shape"
open_line 0.0.1 patch
P=$(head_of vertex/0.0.x)
check "$(show "${P}" versions/vertex)" 0.0.2 "vertex/0.0.x opened from v0.0.1 with pending 0.0.2"
mark_green "${P}"; make_rc "${P}" 0.0.2 vertex/0.0.x
run_promote --check --line vertex/0.0.x --no-dry-run
check "${rc}|$(outval kind)|$(outval version)|$(outval bump)|$(outval next_version)" "0|patch|0.0.2|patch|0.0.3" "patch line: auto bump patch, next 0.0.3"
run_promote --check --line vertex/0.0.x --no-dry-run --bump minor
check "${rc}" 1 "bump=minor on a patch line exits 1"
contains "${log}" "leaves pending 0.1.0, which does not fit vertex/0.0.x \(0.0.P\)" "names the shape"

echo "# publish: tag, draft -> assets -> pre-release, S3 index, the bump commit, the staging request"
fixture; seed_vertex
T=$(head_of vertex/main)
make_build 20261022T100000Z-777-standard "${FLAVOR_STD}"; make_build 20261022T100500Z-777-hardened "${FLAVOR_HARD}"
M1="${tmp}/release-manifest.json"; release_manifest "${M1}" "${T}" 0.0.1
run_promote --publish --manifest "${M1}" --bump major
check "${rc}" 0 "exits 0"
check "$(tag_of vertex/v0.0.1)" "${T}" "tag vertex/v0.0.1 at the sha"
contains "${GH_STUB_STATE}/log" "^api -X POST repos/ds/distro/git/refs -f ref=refs/tags/vertex/v0.0.1 -f sha=${T}$" "tag created through the API"
check "$(release_get vertex/v0.0.1 draft)|$(release_get vertex/v0.0.1 prerelease)|$(release_get vertex/v0.0.1 title)|$(release_get vertex/v0.0.1 target)" \
  "false|true|vertex v0.0.1 (staging)|${T}" "release published as a pre-release, titled (staging), at the sha"
check "$(release_get vertex/v0.0.1 assets | python3 -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin))))')" \
  "release-manifest.json ${FLAVOR_HARD}.SHA256SUMS ${FLAVOR_HARD}.manifest.json ${FLAVOR_STD}.SHA256SUMS ${FLAVOR_STD}.manifest.json" "assets"
check "$(grep -E '^(api|release)' "${GH_STUB_STATE}/log" | cut -d' ' -f1-2 | tr '\n' ';')" \
  "api -X;release view;release create;release upload;release edit;" "order: tag, view, draft create, upload, publish"
contains "${GH_STUB_STATE}/log" "^release create vertex/v0.0.1 -R ds/distro --draft --prerelease --target ${T} --title vertex v0.0.1 \(staging\) --notes-file " "create is a draft pre-release"
contains "${GH_STUB_STATE}/log" "^release edit vertex/v0.0.1 -R ds/distro --draft=false --prerelease$" "published as a pre-release"
notes="${tmp}/notes.md"; release_get vertex/v0.0.1 notes > "${notes}"
contains "${notes}" "actions/runs/4242" "notes link the RC run"
contains "${notes}" "actions/runs/777" "notes link the release run"
contains "${notes}" "\`rc/vertex/${T:0:7}\`" "notes name the RC"
contains "${notes}" "\| \`${FLAVOR_STD}\` \| \`20261022T100000Z-777-standard\` \| \`s3://ds-yocto-artifacts/${MACHINE}/20261022T100000Z-777-standard/\` \| \`${FAKE_SHA}\` \|" "notes table row"
contains "${notes}" "^- SDK \(\`${FLAVOR_STD}\`\): \`s3://ds-yocto-artifacts/${MACHINE}/20261022T100000Z-777-standard/sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-${MACHINE}-toolchain-0.0.1.sh\` \(sha256 \`${FAKE_SHA}\`\)$" "notes list the promote build's SDK"
check "$(grep -c '^- SDK (' "${notes}")" 1 "one SDK line: the hardened build made none"
cmp -s "${M1}" "${BUCKET}/release/vertex/v0.0.1/release-manifest.json"; check $? 0 "S3 index is the release manifest"
N=$(head_of vertex/main)
check "$(git -C "${DISTRO_BARE}" rev-parse "${N}^")" "${T}" "one commit on top of the promoted sha"
check "$(files_in "${N}")" "versions/vertex" "the bump commit touches only versions/vertex"
check "$(show "${N}" versions/vertex)|$(show "${N}" versions/aggregator)" "1.0.0|0.0.1" "pending moved to 1.0.0, the other product untouched"
check "$(subject "${N}")" "release: vertex v0.0.1 promoted; pending 1.0.0 [skip ci]" "bump commit subject"
check "$(git -C "${DISTRO_BARE}" log -1 --format='%an <%ae>' "${N}")" "ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>" "bot identity"
check "$(outval tag)|$(outval next_version)|$(outval line_tip)" "vertex/v0.0.1|1.0.0|${N}" "outputs"
check "$(outval release_url)" "https://github.com/ds/distro/releases/tag/vertex/v0.0.1" "release url"
check "$(outval staging_request | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["product"], d["version"], d["sha"], d["line"], d["tag"], d["rc_run"]["run_id"], d["release_run"]["run_id"], len(d["builds"]),
      sorted(d["builds"][0]), d["builds"][1]["hardened"], d["builds"][1]["build_id"])')" \
  "vertex 0.0.1 ${T} vertex/main vertex/v0.0.1 4242 777 2 ['build_id', 'flavor', 'hardened', 'machine', 's3_prefix'] True 20261022T100500Z-777-hardened" "staging request"
contains "${log}" "::notice::promote: vertex/v0.0.1 published \(vertex v0.0.1 \(staging\)\)" "success notice"

echo "# publish: idempotent on a rerun (tag at the same sha, release published, already bumped)"
run_promote --publish --manifest "${M1}" --bump major
check "${rc}" 0 "exits 0"
contains "${log}" "tag vertex/v0.0.1 already points at ${T:0:7}" "tag reused"
contains "${log}" "release vertex/v0.0.1 is already published; not touching it" "release untouched"
contains "${log}" "versions/vertex on vertex/main is already 1.0.0" "bump skipped"
check "$(head_of vertex/main)" "${N}" "no second bump commit"
check "$(gh_count '^api -X POST')|$(gh_count '^release create')|$(gh_count '^release upload')" "1|1|1" "no second tag, create or upload"
check "$(grep -c 'release/vertex/v0.0.1/release-manifest.json' "${AWS_CALLS}" || true)" 2 "the S3 index copy is repeated (idempotent put)"

echo "# publish: refuses a tag that exists at another sha, a changed pending version, a non-release manifest"
M2="${tmp}/release-manifest-2.json"; release_manifest "${M2}" "${SEED_SHA}" 0.0.1
run_promote --publish --manifest "${M2}" --bump major
check "${rc}" 1 "tag elsewhere exits 1"
contains "${log}" "tag vertex/v0.0.1 already exists at ${T:0:7}, not at ${SEED_SHA:0:7}; refusing" "names both shas"
check "$(tag_of vertex/v0.0.1)|$(head_of vertex/main)" "${T}|${N}" "nothing changed"
Z=$(distro_commit vertex/main "someone else moved pending" versions/vertex=5.5.5)
M3="${tmp}/release-manifest-3.json"; release_manifest "${M3}" "${T}" 0.0.1
run_promote --publish --manifest "${M3}" --bump minor
check "${rc}" 1 "a pending version that is neither the promoted nor the next exits 1"
contains "${log}" "versions/vertex on vertex/main \(${Z:0:7}\) is '5.5.5', neither the promoted 0.0.1 nor the next 0.1.0" "names the values"
check "$(head_of vertex/main)" "${Z}" "line untouched"
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); m["kind"]="rc"; json.dump(m, open(sys.argv[2],"w"))' "${M1}" "${tmp}/rc.json"
run_promote --publish --manifest "${tmp}/rc.json" --bump major
check "${rc}" 1 "an rc manifest exits 1"
contains "${log}" "is not a release manifest" "says so"
run_promote --publish --manifest "${M1}" --bump auto
check "${rc}" 1 "bump=auto is not a resolved level for publish"
run_promote --publish --manifest "${M1}" --bump major --dry-run
check "${rc}" 0 "publish dry run exits 0"
contains "${log}" "dry run: publish does nothing" "and does nothing"

echo "# publish: resumes a draft left by an earlier run; refused tag on GITHUB_TOKEN names the human commands"
fixture; seed_vertex
T=$(head_of vertex/main)
make_build 20261022T100000Z-777-standard "${FLAVOR_STD}"; make_build 20261022T100500Z-777-hardened "${FLAVOR_HARD}"
M4="${tmp}/release-manifest-4.json"; release_manifest "${M4}" "${T}" 0.0.1
tag vertex/v0.0.1 "${T}"
python3 -c 'import json,sys; json.dump({"vertex/v0.0.1": {"draft": True, "prerelease": True, "title": "vertex v0.0.1 (staging)", "target": sys.argv[2], "notes": "", "assets": []}}, open(sys.argv[1], "w"))' "${GH_STUB_STATE}/releases.json" "${T}"
run_promote --publish --manifest "${M4}" --bump patch
check "${rc}" 0 "exits 0"
contains "${log}" "exists as a draft .* resuming" "resume notice"
check "$(gh_count '^release create')|$(gh_count '^release upload')|$(gh_count '^release edit')" "0|1|1" "no create, one upload, one publish"
check "$(release_get vertex/v0.0.1 draft)|$(show "$(head_of vertex/main)" versions/vertex)" "false|0.0.2" "draft ends published, bump=patch gives 0.0.2"
fixture; seed_vertex
T=$(head_of vertex/main)
make_build 20261022T100000Z-777-standard "${FLAVOR_STD}"; make_build 20261022T100500Z-777-hardened "${FLAVOR_HARD}"
M5="${tmp}/release-manifest-5.json"; release_manifest "${M5}" "${T}" 0.0.1
rc=0; (cd "${DISTRO}" && GH_STUB_FAIL_TAG=1 TOKEN_KIND=github "${scripts}/promote.sh" --publish --manifest "${M5}" --bump major) > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "refused tag exits 1"
contains "${log}" "::error::creating tag vertex/v0.0.1 was refused .*DS_RELEASE_APP_CLIENT_ID.*a human with bypass rights runs: git fetch origin && git tag vertex/v0.0.1 ${T} && git push origin refs/tags/vertex/v0.0.1" "hint names the App and the commands"
check "$(tag_of vertex/v0.0.1)|$(release_exists vertex/v0.0.1)|$(head_of vertex/main)" "none|no|${T}" "nothing created"
rc=0; (cd "${DISTRO}" && GH_STUB_FAIL_TAG=1 TOKEN_KIND=app "${scripts}/promote.sh" --publish --manifest "${M5}" --bump major) > "${log}" 2>&1 || rc=$?
check "${rc}|$(grep -c DS_RELEASE_APP_CLIENT_ID "${log}" || true)" "1|0" "with the App the refusal has no App hint"

echo
if [ "${fails}" = 0 ]; then echo "test_promote: all passed"; else echo "test_promote: ${fails} failure(s)"; exit 1; fi
