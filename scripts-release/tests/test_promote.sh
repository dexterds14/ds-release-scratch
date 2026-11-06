#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/promote.sh (ENG-1934, ENG-2110). One bare repo in
# a temp dir stands in for origin (trunk main, product ds, product_prefix
# false); lines are opened with the real create-branch.sh, children are
# merged with the real merge-up.sh. A python stub gh on PATH answers run list from a
# green-sha list, keeps releases (with their assets) in a state dir, creates
# tags for `gh api -X POST .../git/refs` in the bare repo and logs every
# call; aws is the stub in tests/stubs (a bucket tree under FAKE_S3). No
# network, nothing touches this checkout, and no git identity is configured
# for the script runs: the bump commit must come out as lib.sh's bot.
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
    repo = opts.get('-R') or opts.get('--repo') or os.environ.get('GITHUB_REPOSITORY') or next(iter(REPOS))
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
export GITHUB_REPOSITORY=ds/repo
export GITHUB_SERVER_URL=https://github.com GITHUB_RUN_ID=777
export TOKEN=stub-token TOKEN_KIND=app GH_TOKEN=stub-token DRY_RUN=false
export GH_STUB_STATE="${tmp}/gh-state" GH_STUB_GREEN="${tmp}/gh-state/green"
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
MACHINE=ds-arm64
FLAVOR_STD=ds_${MACHINE}_release
FLAVOR_HARD=ds_${MACHINE}_release-hardened
FAKE_SHA=$(printf 'c%.0s' $(seq 64))
BOT='ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>'
BARE="${tmp}/origin.git" REPO="${tmp}/repo"
export GH_STUB_REPOS="${GITHUB_REPOSITORY}=${BARE}"
# The fixture's own commits carry this identity; run_promote strips it so the
# script falls back to lib.sh's bot.
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x

# fixture: a fresh bare origin (main seeded with the release-line files), a
# clone the scripts run from, empty gh state and S3.
fixture() {
  rm -rf "${BARE}" "${REPO}" "${GH_STUB_STATE}" "${FAKE_S3}" "${tmp}/seed"
  mkdir -p "${GH_STUB_STATE}" "${BUCKET}"
  : > "${GH_STUB_GREEN}"; : > "${GH_STUB_STATE}/log"; : > "${AWS_CALLS}"
  git init -q --bare -b main "${BARE}"
  local seed="${tmp}/seed"
  git init -q -b main "${seed}"
  mkdir -p "${seed}/release" "${seed}/versions" "${seed}/.github"
  echo '{"products": ["ds"], "product_prefix": false, "bookkeeping_files": [".github/yocto-ci.env"]}' > "${seed}/release/config.json"
  printf '{"products": {"ds": {"flavors": ["%s", "%s"]}}}\n' "${FLAVOR_STD}" "${FLAVOR_HARD}" > "${seed}/release/matrix.json"
  echo 0.0.1 > "${seed}/versions/ds"; echo '# versions' > "${seed}/versions/README.md"
  echo 'DS_TEGRA_DISTRO_REF=main' > "${seed}/.github/yocto-ci.env"
  echo trunk > "${seed}/foo.txt"
  git -C "${seed}" add -A; git -C "${seed}" commit -q -m seed
  SEED_SHA=$(git -C "${seed}" rev-parse HEAD)
  # A second commit, so main's tip and SEED_SHA are two different shas.
  echo main > "${seed}/foo.txt"
  git -C "${seed}" add -A; git -C "${seed}" commit -q -m "trunk work"
  git -C "${seed}" remote add origin "${BARE}"; git -C "${seed}" push -q origin main
  git clone -q "${BARE}" "${REPO}"
}

head_of() { git -C "${BARE}" rev-parse --verify --quiet "refs/heads/$1" || echo none; }
tag_of() { git -C "${BARE}" rev-parse --verify --quiet "refs/tags/$1^{commit}" || echo none; }
show() { git -C "${BARE}" show "$1:$2"; }
files_in() { git -C "${BARE}" diff-tree --no-commit-id --name-only -r "$1" | paste -sd ' '; }
subject() { git -C "${BARE}" log -1 --format=%s "$1"; }
mark_green() { echo "$1" >> "${GH_STUB_GREEN}"; }
tag() { git -C "${BARE}" update-ref "refs/tags/$1" "$2"; }

# commit_on <branch> <msg> <path=content>... -> sha (pushed)
commit_on() {
  local branch=$1 msg=$2; shift 2
  git -C "${REPO}" fetch -q origin
  git -C "${REPO}" checkout -q -B "${branch}" "origin/${branch}"
  for kv in "$@"; do mkdir -p "$(dirname "${REPO}/${kv%%=*}")"; printf '%s\n' "${kv#*=}" > "${REPO}/${kv%%=*}"; done
  git -C "${REPO}" add -A; git -C "${REPO}" commit -q -m "${msg}"
  git -C "${REPO}" push -q origin "${branch}"
  git -C "${REPO}" rev-parse HEAD
}

# open_line <version> <kind>: the real create-branch.sh opens the line the
# tag v<version> derives (the line at the tag plus its opening commit).
open_line() { (cd "${REPO}" && "${scripts}/create-branch.sh" --version "$1" --kind "$2" > "${tmp}/create.log" 2>&1) || { cat "${tmp}/create.log"; return 1; }; }

# make_rc <sha> <pending> [<line>]: the rc/<sha7> tag at the sha, a published
# pre-release with a release-manifest.json asset (run 4242).
make_rc() {
  local sha=$1 pending=$2 line=${3:-main} tagname="rc/${1:0:7}"
  tag "${tagname}" "${sha}"
  python3 -I - "${GH_STUB_STATE}" "${tagname}" "${sha}" "${pending}" "${line}" "${FAKE_SHA}" "${MACHINE}" "${FLAVOR_STD}" <<'EOF'
import json, os, sys
state, tagname, sha, pending, line, fake, machine, flavor = sys.argv[1:9]
d = os.path.join(state, 'assets', tagname.replace('/', '_'))
os.makedirs(d, exist_ok=True)
files = {'debs': [{'key': 'deb/ds-utils_1.0+git1_arm64.deb', 'package': 'ds-utils', 'version': '1.0+git1', 'sha256': fake, 'bytes': 1}],
         'extra': [{'key': 'extra/ds-config.tar.gz', 'sha256': fake, 'bytes': 1}],
         'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 1},
         'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 1}}
doc = {'schema': 1, 'kind': 'rc', 'product': 'ds', 'version': f'{pending}-rc.9.github-{sha[:7]}', 'pending': pending,
       'sha': sha, 'line': line, 'run_id': '4242', 'run_url': 'https://github.com/ds/repo/actions/runs/4242',
       'builds': [{'flavor': flavor, 'machine': machine, 'hardened': False, 'build_id': '20261021T110000Z-4242-standard',
                   's3_prefix': f'{machine}/20261021T110000Z-4242-standard', 'files': files}]}
json.dump(doc, open(os.path.join(d, 'release-manifest.json'), 'w'))
p = os.path.join(state, 'releases.json')
rels = json.load(open(p)) if os.path.exists(p) else {}
rels[tagname] = {'draft': False, 'prerelease': True, 'title': f'RC {doc["version"]}', 'target': sha, 'notes': '',
                 'assets': ['release-manifest.json']}
json.dump(rels, open(p, 'w'))
EOF
}
# release_set <tag> <field> <json value> / release_del <tag> / release_get <tag> <field>
release_set() { python3 -I -c 'import json,sys; p=sys.argv[1]; r=json.load(open(p)); r[sys.argv[2]][sys.argv[3]]=json.loads(sys.argv[4]); json.dump(r, open(p,"w"))' "${GH_STUB_STATE}/releases.json" "$@"; }
release_del() { python3 -I -c 'import json,sys; p=sys.argv[1]; r=json.load(open(p)); r.pop(sys.argv[2], None); json.dump(r, open(p,"w"))' "${GH_STUB_STATE}/releases.json" "$@"; }
release_get() { python3 -I -c 'import json,sys; r=json.load(open(sys.argv[1])); v=r.get(sys.argv[2], {}).get(sys.argv[3], "none"); print(json.dumps(v) if isinstance(v,(bool,list,dict)) else v)' "${GH_STUB_STATE}/releases.json" "$@"; }
release_exists() { python3 -I -c 'import json,sys,os; p=sys.argv[1]; r=json.load(open(p)) if os.path.exists(p) else {}; print("yes" if sys.argv[2] in r else "no")' "${GH_STUB_STATE}/releases.json" "$1"; }

# run_promote <args...> -> rc, log, out (GITHUB_OUTPUT file)
run_promote() {
  log="${tmp}/log"; out="${tmp}/out"
  : > "${out}"; rc=0
  (cd "${REPO}" && env -u GIT_AUTHOR_NAME -u GIT_AUTHOR_EMAIL -u GIT_COMMITTER_NAME -u GIT_COMMITTER_EMAIL \
    "${scripts}/promote.sh" "$@" --github-output "${out}") > "${log}" 2>&1 || rc=$?
}
outval() { sed -n "s/^$1=//p" "${out}" | tail -1; }
gh_count() { grep -c -E -- "$1" "${GH_STUB_STATE}/log" || true; }

# make_build <build_id> <flavor> <deb basename> <extra basename>: the objects
# of a per-build prefix that publish downloads as assets
make_build() {
  local d="${BUCKET}/${MACHINE}/$1"
  mkdir -p "${d}/deb" "${d}/extra"
  printf '{"buildparams": {"flavor": "%s"}}\n' "$2" > "${d}/manifest.json"
  printf '%s  manifest.json\n' "${FAKE_SHA}" > "${d}/SHA256SUMS"
  printf 'deb %s of %s\n' "$3" "$2" > "${d}/deb/$3"
  printf 'extra %s of %s\n' "$4" "$2" > "${d}/extra/$4"
}
# release_manifest <out> <sha> <version> [<line>] [<hardened deb basename>]: what rc-gate.sh
# --kind release writes, for two flavors of run 777 (rule 7's files shape)
release_manifest() {
  python3 -I - "$1" "$2" "$3" "${4:-main}" "${FAKE_SHA}" "${MACHINE}" "${FLAVOR_STD}" "${FLAVOR_HARD}" "${5:-ds-sdr_1.0+git1_arm64.deb}" <<'EOF'
import json, sys
out, sha, ver, line, fake, machine, std, hard, hard_deb = sys.argv[1:10]
def build(flavor, build_id, hardened, deb, extra):
    files = {'debs': [{'key': f'deb/{deb}', 'package': deb.split('_', 1)[0], 'version': '1.0+git1', 'sha256': fake, 'bytes': 8}],
             'extra': [{'key': f'extra/{extra}', 'sha256': fake, 'bytes': 12}],
             'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 100},
             'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 300}}
    return {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id,
            's3_prefix': f'{machine}/{build_id}', 'files': files}
doc = {'schema': 1, 'kind': 'release', 'product': 'ds', 'version': ver, 'pending': ver, 'sha': sha, 'line': line,
       'run_id': '777', 'run_url': 'https://github.com/ds/repo/actions/runs/777',
       'rc_run': {'run_id': '4242', 'run_url': 'https://github.com/ds/repo/actions/runs/4242', 'tag': f'rc/{sha[:7]}'},
       'release_run': {'run_id': '777', 'run_url': 'https://github.com/ds/repo/actions/runs/777'},
       'builds': [build(std, '20261022T100000Z-777-standard', False, 'ds-utils_1.0+git1_arm64.deb', 'ds-config.tar.gz'),
                  build(hard, '20261022T100500Z-777-hardened', True, hard_deb, 'ds-config-hardened.tar.gz')]}
json.dump(doc, open(out, 'w'), indent=2)
EOF
}
seed_builds() {
  make_build 20261022T100000Z-777-standard "${FLAVOR_STD}" ds-utils_1.0+git1_arm64.deb ds-config.tar.gz
  make_build 20261022T100500Z-777-hardened "${FLAVOR_HARD}" "${1:-ds-sdr_1.0+git1_arm64.deb}" ds-config-hardened.tar.gz
}

# =============================================================================
echo "# check: a green main tip with its RC"
fixture
T=$(head_of main); mark_green "${T}"; make_rc "${T}" 0.0.1
run_promote --check --line main --no-dry-run
check "${rc}" 0 "exits 0"
check "$(outval proceed)" true "proceed=true"
check "$(outval product)|$(outval line)|$(outval kind)|$(outval sha)" "ds|main|main|${T}" "product, line, kind, sha"
check "$(outval version)|$(outval tag)|$(outval rc_tag)" "0.0.1|v0.0.1|rc/${T:0:7}" "version, tag, rc_tag (bare names)"
check "$(outval bump)|$(outval next_version)" "major|1.0.0" "auto bump on main is major, next 1.0.0"
check "$(outval rc_run_id)|$(outval rc_run_url)" "4242|https://github.com/ds/repo/actions/runs/4242" "rc run from the RC manifest"
check "$(outval matrix)" "{\"include\":[{\"flavor\":\"${FLAVOR_STD}\"},{\"flavor\":\"${FLAVOR_HARD}\"}]}" "matrix is the product's release flavors"
contains "${log}" "green sha:          ${T} \(run 56, id 1234\)" "names the green run"
contains "${log}" "children:           \(none\)" "no children"
contains "${log}" "bump:               major: versions/ds 0.0.1 -> 1.0.0, commit \"release: v0.0.1 promoted; pending 1.0.0 \[skip ci\]\"" "prints the bump commit without a product"
contains "${log}" "release:            \"v0.0.1 \(staging\)\"" "prints the release title without a product"
contains "${GH_STUB_STATE}/log" "^run list --repo ds/repo --workflow release-rc-build.yml --commit ${T} --branch main" "gh calls name the repo from GITHUB_REPOSITORY"
check "$(gh_count '^release create|^release upload|^release edit|^api')" 0 "the check creates nothing"
run_promote --check --line main --no-dry-run --bump patch
check "${rc}|$(outval bump)|$(outval next_version)" "0|patch|0.0.2" "bump=patch on main: next 0.0.2 (the pilot's path)"
run_promote --check --line main --no-dry-run --bump minor
check "${rc}|$(outval next_version)" "0|0.1.0" "bump=minor on main: next 0.1.0"
run_promote --check --line main --no-dry-run --sha "${SEED_SHA}"
check "${rc}" 1 "a dispatch sha that is not the tip exits 1"
contains "${log}" "dispatched at ${SEED_SHA} but main is now at ${T}" "names both shas"
commit_on dev/main "not a line" bar.txt=x >/dev/null 2>&1 || git -C "${REPO}" push -q origin "HEAD:refs/heads/dev/main"
run_promote --check --line dev/main --no-dry-run
check "${rc}" 1 "a non-line ref exits 1"
contains "${log}" "dev/main is not a release line" "names the ref"
run_promote --check --line main --no-dry-run --bump release
check "${rc}" 1 "a bad bump level exits 1"

echo "# check: dry run creates nothing and sets proceed=false"
run_promote --check --line main --dry-run
check "${rc}|$(outval proceed)" "0|false" "exit 0, proceed=false"
contains "${log}" "dry run: nothing built, tagged, released or pushed" "dry-run notice"
check "$(outval tag)|$(outval sha)" "v0.0.1|${T}" "the plan outputs are still there"
check "$(gh_count '^release create|^release upload|^release edit|^api')" 0 "no gh create/upload/edit/api call"
check "$(grep -c 's3 cp' "${AWS_CALLS}" || true)" 0 "no S3 copy"
check "$(head_of main)|$(tag_of v0.0.1)" "${T}|none" "line and tags untouched"

echo "# check: refusals (red tip, missing or wrong RC release, RC manifest, existing tag)"
: > "${GH_STUB_GREEN}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "red tip exits 1"
contains "${log}" "::error::promote: the tip of main is not green: no successful release-rc-build.yml run on main for ${T}" "names the candidates"
mark_green "${T}"
release_del "rc/${T:0:7}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "missing RC release exits 1"
contains "${log}" "no pre-release rc/${T:0:7} for the green sha" "names the RC tag"
make_rc "${T}" 0.0.1
release_set "rc/${T:0:7}" draft true
run_promote --check --line main --no-dry-run
check "${rc}" 1 "draft RC exits 1"
contains "${log}" "is still a draft" "says draft"
release_set "rc/${T:0:7}" draft false
release_set "rc/${T:0:7}" prerelease false
run_promote --check --line main --no-dry-run
check "${rc}" 1 "non-pre-release RC exits 1"
contains "${log}" "is not a pre-release" "says so"
release_set "rc/${T:0:7}" prerelease true
tag "rc/${T:0:7}" "${SEED_SHA}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "RC tag at another sha exits 1"
contains "${log}" "tag rc/${T:0:7} points at '${SEED_SHA}', not the green sha ${T}" "names both"
tag "rc/${T:0:7}" "${T}"
make_rc "${T}" 0.0.5   # manifest disagrees with versions/ds at the sha
run_promote --check --line main --no-dry-run
check "${rc}" 1 "RC manifest pending mismatch exits 1"
contains "${log}" "says pending 0.0.5, versions/ds at ${T:0:7} says 0.0.1" "names both"
make_rc "${T}" 0.0.1
tag v0.0.1 "${T}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "existing tag at the same sha exits 1"
contains "${log}" "tag v0.0.1 already exists at ${T:0:7}: this sha was already promoted" "names the sha"
tag v0.0.1 "${SEED_SHA}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "existing tag at another sha exits 1"
contains "${log}" "tag v0.0.1 already exists at ${SEED_SHA:0:7}; versions/ds on main \(0.0.1\) is a version that was already released" "names the sha"
git -C "${BARE}" update-ref -d refs/tags/v0.0.1
run_promote --check --line main --no-dry-run
check "${rc}" 0 "back to green once the tag is gone"

echo "# check: a versions-only tip is refused (the release build runs at the tip, not at the older green sha)"
V=$(commit_on main "release: pending moved [skip ci]" versions/ds=0.0.2)
run_promote --check --line main --no-dry-run
check "${rc}" 1 "a versions-only tip over a green sha exits 1"
contains "${log}" "::error::promote: the tip ${V:0:7} of main only touches versions/ and has no successful release-rc-build.yml run \(the last green sha is ${T:0:7}\); the release build would run at the tip, so re-run release-rc-build.yml on main" "names the tip, the green sha and the fix"
check "$(outval proceed)" "" "no proceed output"
tag v0.0.1 "${T}"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "the previous promote's bump commit as the tip exits 1"
contains "${log}" "::error::promote: nothing new to promote on main since v0.0.1 \(the tip ${V:0:7} only touches versions/; v0.0.1 is at ${T:0:7}\)" "says nothing new since the tag"
check "$(outval proceed)" "" "no proceed output"

echo "# check: children (bookkeeping-only child is merged; a child fix is not; merge_children merges and stops)"
fixture
T=$(head_of main); mark_green "${T}"; make_rc "${T}" 0.0.1
tag v0.0.1 "${T}"
open_line 0.0.1 minor
L=$(head_of 0.x.x)
check "$(show "${L}" versions/ds)|$(files_in "${L}")" "0.1.0|versions/ds" "0.x.x opened at v0.0.1 with pending 0.1.0 by its opening commit"
git -C "${BARE}" update-ref -d refs/tags/v0.0.1
run_promote --check --line main --no-dry-run
check "${rc}" 0 "a child with only its opening commit counts as merged"
contains "${log}" "child 0.x.x: merged \(nothing but bookkeeping" "says so"
commit_on 0.x.x "ci env moved [skip ci]" .github/yocto-ci.env=DS_TEGRA_DISTRO_REF=0.x.x > /dev/null
run_promote --check --line main --no-dry-run
check "${rc}" 0 "a child commit touching only the bookkeeping file counts as merged"
C=$(commit_on 0.x.x "child fix" foo.txt=child)
run_promote --check --line main --no-dry-run
check "${rc}" 1 "an unmerged child exits 1"
contains "${log}" "::error::promote: child line\(s\) not merged into main: 0.x.x" "names the child"
contains "${log}" "child 0.x.x: 1 commit\(s\) not in main:" "counts the commits"
contains "${log}" "    ${C:0:7} child fix" "lists the commit"
check "$(outval proceed)" "" "no proceed output"
run_promote --check --line main --no-dry-run --merge-children
check "${rc}" 1 "merge_children with a child that is not green exits 1"
contains "${log}" "no successful release-rc-build.yml run on 0.x.x" "merge-up's green check refused"
contains "${log}" "merge-up of 0.x.x into main failed" "promote names the child"
check "$(head_of main)" "${T}" "nothing merged"
mark_green "${C}"
run_promote --check --line main --dry-run --merge-children
check "${rc}|$(outval proceed)" "0|false" "merge_children dry run: exit 0, proceed=false"
contains "${log}" "dry run: would merge 1 child line\(s\) and stop" "says what it would do"
contains "${log}" "dry run: nothing pushed" "ran merge-up's dry run"
check "$(head_of main)" "${T}" "nothing merged in the dry run"
run_promote --check --line main --no-dry-run --merge-children
check "${rc}" 1 "merge_children merges and exits 1 by design"
contains "${log}" "::error::promote: merged 1 child line\(s\) into main \(0.x.x\); the new tip needs its own Action A run \(release-rc-build.yml\), re-run promote when it is green" "the stop message"
M=$(head_of main)
check "$(git -C "${BARE}" rev-parse "${M}^2")|$(git -C "${BARE}" rev-parse "${M}^1")" "${C}|${T}" "main is now the merge of the child"
check "$(show "${M}" versions/ds)|$(show "${M}" .github/yocto-ci.env)|$(show "${M}" foo.txt)" "0.0.1|DS_TEGRA_DISTRO_REF=main|child" "target's versions/ and bookkeeping file kept, child's fix carried"
check "$(outval proceed)" "" "no proceed output after merging"
run_promote --check --line main --no-dry-run
check "${rc}" 1 "the new tip is not green yet"
contains "${log}" "not green" "says so"

echo "# check: version shape on minor and patch lines, bump levels that do not fit"
fixture
T=$(head_of main); mark_green "${T}"; make_rc "${T}" 0.0.1
tag v0.0.1 "${T}"
open_line 0.0.1 minor
L=$(head_of 0.x.x); mark_green "${L}"; make_rc "${L}" 0.1.0 0.x.x
run_promote --check --line 0.x.x --no-dry-run
check "${rc}|$(outval kind)|$(outval version)|$(outval tag)|$(outval bump)|$(outval next_version)" "0|minor|0.1.0|v0.1.0|minor|0.2.0" "minor line: 0.1.0 -> tag, auto bump minor, next 0.2.0"
run_promote --check --line 0.x.x --no-dry-run --bump patch
check "${rc}" 1 "bump=patch on a minor line exits 1"
contains "${log}" "bump patch after 0.1.0 leaves pending 0.1.1, which does not fit 0.x.x \(0.M.0\); use bump=minor" "names the fix"
run_promote --check --line 0.x.x --no-dry-run --bump major
check "${rc}" 1 "bump=major on a minor line exits 1"
B=$(commit_on 0.x.x "bad pending" versions/ds=0.1.5 foo.txt=x)
mark_green "${B}"; make_rc "${B}" 0.1.5 0.x.x
run_promote --check --line 0.x.x --no-dry-run
check "${rc}" 1 "pending 0.1.5 on a minor line exits 1"
contains "${log}" "pending version 0.1.5 does not fit 0.x.x \(a minor line promotes 0.M.0\)" "names the shape"
open_line 0.0.1 patch
P=$(head_of 0.0.x)
check "$(show "${P}" versions/ds)" 0.0.2 "0.0.x opened at v0.0.1 with pending 0.0.2"
mark_green "${P}"; make_rc "${P}" 0.0.2 0.0.x
run_promote --check --line 0.0.x --no-dry-run
check "${rc}|$(outval kind)|$(outval version)|$(outval bump)|$(outval next_version)" "0|patch|0.0.2|patch|0.0.3" "patch line: auto bump patch, next 0.0.3"
run_promote --check --line 0.0.x --no-dry-run --bump minor
check "${rc}" 1 "bump=minor on a patch line exits 1"
contains "${log}" "leaves pending 0.1.0, which does not fit 0.0.x \(0.0.P\)" "names the shape"

echo "# publish: tag, draft -> assets -> pre-release, S3 index, the bump commit, the staging request"
fixture
T=$(head_of main)
seed_builds
M1="${tmp}/release-manifest.json"; release_manifest "${M1}" "${T}" 0.0.1
run_promote --publish --manifest "${M1}" --bump major
check "${rc}" 0 "exits 0"
check "$(tag_of v0.0.1)" "${T}" "tag v0.0.1 at the sha"
contains "${GH_STUB_STATE}/log" "^api -X POST repos/ds/repo/git/refs -f ref=refs/tags/v0.0.1 -f sha=${T}$" "tag created through the API of GITHUB_REPOSITORY"
check "$(release_get v0.0.1 draft)|$(release_get v0.0.1 prerelease)|$(release_get v0.0.1 title)|$(release_get v0.0.1 target)" \
  "false|true|v0.0.1 (staging)|${T}" "release published as a pre-release, titled (staging) without a product, at the sha"
check "$(release_get v0.0.1 assets | python3 -I -c 'import json,sys; print(" ".join(sorted(json.load(sys.stdin))))')" \
  "ds-config-hardened.tar.gz ds-config.tar.gz ds-sdr_1.0+git1_arm64.deb ds-utils_1.0+git1_arm64.deb ${FLAVOR_HARD}.SHA256SUMS ${FLAVOR_HARD}.manifest.json ${FLAVOR_STD}.SHA256SUMS ${FLAVOR_STD}.manifest.json release-manifest.json" \
  "assets: the manifest, every deb and extra file, each flavor's manifest.json and SHA256SUMS"
check "$(cat "${GH_STUB_STATE}/assets/v0.0.1/ds-utils_1.0+git1_arm64.deb")" "deb ds-utils_1.0+git1_arm64.deb of ${FLAVOR_STD}" "the deb asset is the build's object"
check "$(grep -E '^(api|release)' "${GH_STUB_STATE}/log" | cut -d' ' -f1-2 | tr '\n' ';')" \
  "api -X;release view;release create;release upload;release edit;" "order: tag, view, draft create, upload, publish"
contains "${GH_STUB_STATE}/log" "^release create v0.0.1 --repo ds/repo --draft --prerelease --target ${T} --title v0.0.1 \(staging\) --notes-file " "create is a draft pre-release"
contains "${GH_STUB_STATE}/log" "^release edit v0.0.1 --repo ds/repo --draft=false --prerelease$" "published as a pre-release"
notes="${tmp}/notes.md"; release_get v0.0.1 notes > "${notes}"
contains "${notes}" "actions/runs/4242" "notes link the RC run"
contains "${notes}" "actions/runs/777" "notes link the release run"
contains "${notes}" "\`rc/${T:0:7}\`" "notes name the RC"
contains "${notes}" "\| \`${FLAVOR_STD}\` \| \`20261022T100000Z-777-standard\` \| \`s3://ds-yocto-artifacts/${MACHINE}/20261022T100000Z-777-standard/\` \| 2 \| none recorded \|" "notes table row counts the artifacts (debs and extra files) and names no SDK when the build recorded none"
lacks "${notes}" "\*\*ds\*\*" "notes name no product"
cmp -s "${M1}" "${BUCKET}/release/ds/v0.0.1/release-manifest.json"; check $? 0 "S3 index is the release manifest, under the product's prefix"
N=$(head_of main)
check "$(git -C "${BARE}" rev-parse "${N}^")" "${T}" "one commit on top of the promoted sha"
check "$(files_in "${N}")" "versions/ds" "the bump commit touches only versions/ds"
check "$(show "${N}" versions/ds)" "1.0.0" "pending moved to 1.0.0"
check "$(subject "${N}")" "release: v0.0.1 promoted; pending 1.0.0 [skip ci]" "bump commit subject"
check "$(git -C "${BARE}" log -1 --format='%an <%ae>' "${N}")" "${BOT}" "bot identity from lib.sh"
check "$(outval tag)|$(outval next_version)|$(outval line_tip)" "v0.0.1|1.0.0|${N}" "outputs"
check "$(outval release_url)" "https://github.com/ds/repo/releases/tag/v0.0.1" "release url"
check "$(outval staging_request | python3 -I -c '
import json, sys
d = json.load(sys.stdin)
print(d["product"], d["version"], d["sha"], d["line"], d["tag"], d["rc_run"]["run_id"], d["release_run"]["run_id"], len(d["builds"]),
      sorted(d["builds"][0]), d["builds"][1]["hardened"], d["builds"][1]["build_id"])')" \
  "ds 0.0.1 ${T} main v0.0.1 4242 777 2 ['build_id', 'flavor', 'hardened', 'machine', 's3_prefix'] True 20261022T100500Z-777-hardened" "staging request"
contains "${log}" "::notice::promote: v0.0.1 published \(v0.0.1 \(staging\)\)" "success notice"

echo "# publish: idempotent on a rerun (tag at the same sha, release published, already bumped)"
run_promote --publish --manifest "${M1}" --bump major
check "${rc}" 0 "exits 0"
contains "${log}" "tag v0.0.1 already points at ${T:0:7}" "tag reused"
contains "${log}" "release v0.0.1 is already published; not touching it" "release untouched"
contains "${log}" "versions/ds on main is already 1.0.0" "bump skipped"
check "$(head_of main)" "${N}" "no second bump commit"
check "$(gh_count '^api -X POST')|$(gh_count '^release create')|$(gh_count '^release upload')" "1|1|1" "no second tag, create or upload"
check "$(grep -c 'release/ds/v0.0.1/release-manifest.json' "${AWS_CALLS}" || true)" 2 "the S3 index copy is repeated (idempotent put)"

echo "# publish: refuses a tag that exists at another sha, a changed pending version, a non-release manifest, a repeated asset name"
M2="${tmp}/release-manifest-2.json"; release_manifest "${M2}" "${SEED_SHA}" 0.0.1
run_promote --publish --manifest "${M2}" --bump major
check "${rc}" 1 "tag elsewhere exits 1"
contains "${log}" "tag v0.0.1 already exists at ${T:0:7}, not at ${SEED_SHA:0:7}; refusing" "names both shas"
check "$(tag_of v0.0.1)|$(head_of main)" "${T}|${N}" "nothing changed"
Z=$(commit_on main "someone else moved pending" versions/ds=5.5.5)
M3="${tmp}/release-manifest-3.json"; release_manifest "${M3}" "${T}" 0.0.1
run_promote --publish --manifest "${M3}" --bump minor
check "${rc}" 1 "a pending version that is neither the promoted nor the next exits 1"
contains "${log}" "versions/ds on main \(${Z:0:7}\) is '5.5.5', neither the promoted 0.0.1 nor the next 0.1.0" "names the values"
check "$(head_of main)" "${Z}" "line untouched"
python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])); m["kind"]="rc"; json.dump(m, open(sys.argv[2],"w"))' "${M1}" "${tmp}/rc.json"
run_promote --publish --manifest "${tmp}/rc.json" --bump major
check "${rc}" 1 "an rc manifest exits 1"
contains "${log}" "is not a release manifest" "says so"
run_promote --publish --manifest "${M1}" --bump auto
check "${rc}" 1 "bump=auto is not a resolved level for publish"
run_promote --publish --manifest "${M1}" --bump major --dry-run
check "${rc}" 0 "publish dry run exits 0"
contains "${log}" "dry run: publish does nothing" "and does nothing"
rc=0; (cd "${REPO}" && env -u GITHUB_REPOSITORY "${scripts}/promote.sh" --publish --manifest "${M1}" --bump major) > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no GITHUB_REPOSITORY exits 1"
contains "${log}" "::error::promote: GITHUB_REPOSITORY is not set" "names the variable"
fixture
T=$(head_of main)
seed_builds ds-utils_1.0+git1_arm64.deb
M6="${tmp}/release-manifest-6.json"; release_manifest "${M6}" "${T}" 0.0.1 main ds-utils_1.0+git1_arm64.deb
run_promote --publish --manifest "${M6}" --bump major
check "${rc}" 1 "two builds attaching the same deb name exit 1"
contains "${log}" "::error::promote: asset name ds-utils_1.0\+git1_arm64.deb is used twice" "names the asset"
check "$(tag_of v0.0.1)|$(release_exists v0.0.1)|$(head_of main)" "${T}|no|${T}" "the tag stays for the rerun (its check is idempotent), no release, nothing bumped"

echo "# publish: resumes a draft left by an earlier run; refused tag on GITHUB_TOKEN names the human commands"
fixture
T=$(head_of main)
seed_builds
M4="${tmp}/release-manifest-4.json"; release_manifest "${M4}" "${T}" 0.0.1
tag v0.0.1 "${T}"
python3 -I -c 'import json,sys; json.dump({"v0.0.1": {"draft": True, "prerelease": True, "title": "v0.0.1 (staging)", "target": sys.argv[2], "notes": "", "assets": []}}, open(sys.argv[1], "w"))' "${GH_STUB_STATE}/releases.json" "${T}"
run_promote --publish --manifest "${M4}" --bump patch
check "${rc}" 0 "exits 0"
contains "${log}" "exists as a draft .* resuming" "resume notice"
check "$(gh_count '^release create')|$(gh_count '^release upload')|$(gh_count '^release edit')" "0|1|1" "no create, one upload, one publish"
check "$(release_get v0.0.1 draft)|$(show "$(head_of main)" versions/ds)" "false|0.0.2" "draft ends published, bump=patch gives 0.0.2"
fixture
T=$(head_of main)
seed_builds
M5="${tmp}/release-manifest-5.json"; release_manifest "${M5}" "${T}" 0.0.1
rc=0; (cd "${REPO}" && GH_STUB_FAIL_TAG=1 TOKEN_KIND=github "${scripts}/promote.sh" --publish --manifest "${M5}" --bump major) > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "refused tag exits 1"
contains "${log}" "::error::creating tag v0.0.1 was refused .*DS_RELEASE_APP_CLIENT_ID.*ds-os-release-bot.*a human with bypass rights runs: git fetch origin && git tag v0.0.1 ${T} && git push origin refs/tags/v0.0.1" "hint names the App and the commands"
check "$(tag_of v0.0.1)|$(release_exists v0.0.1)|$(head_of main)" "none|no|${T}" "nothing created"
rc=0; (cd "${REPO}" && GH_STUB_FAIL_TAG=1 TOKEN_KIND=app "${scripts}/promote.sh" --publish --manifest "${M5}" --bump major) > "${log}" 2>&1 || rc=$?
check "${rc}|$(grep -c DS_RELEASE_APP_CLIENT_ID "${log}" || true)" "1|0" "with the App the refusal has no App hint"

echo
if [ "${fails}" = 0 ]; then echo "test_promote: all passed"; else echo "test_promote: ${fails} failure(s)"; exit 1; fi
