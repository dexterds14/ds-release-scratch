#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/ship.sh (ENG-1936, ENG-2110). S3 is the aws stub
# in tests/stubs (a bucket tree under FAKE_S3) behind a wrapper that adds the
# two shapes ship.sh needs and the shared stub lacks: a bucket-to-bucket
# `s3 cp` (with a knob that corrupts one copy) and `s3 rm` (one object or
# --recursive). gh
# is a stub of this test's own: the release is a JSON state file under
# FAKE_RELEASES, its release-manifest.json asset sits next to it, `release
# edit` rewrites the state, `api .../approvals` answers from FAKE_APPROVALS.
# Every call is logged so the order copy -> verify can be asserted. Names
# carry no product (product ds, product_prefix false): the release is
# v0.0.1 and the builds list debs and extra files. No network.
#   bash scripts-release/tests/test_ship.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
# ship.sh reads the product list from RELEASE_ROOT: a fixture config with the
# product ds, so the test is the same in every repo that carries the tooling.
mkdir -p "${tmp}/root/release"
echo '{"products": ["ds"], "product_prefix": false}' > "${tmp}/root/release/config.json"
echo '{"products": {"ds": {"flavors": ["ds_ds-arm64_release"]}}}' > "${tmp}/root/release/matrix.json"
export RELEASE_ROOT="${tmp}/root"
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
# status <cmd...>: 0 or 1 on stdout, so a failing assertion never trips set -e
status() { if "$@" >/dev/null 2>&1; then echo 0; else echo 1; fi; }
# copies_in: server-side or upload copies whose destination is the release bucket
copies_in() { grep -c -E '^aws s3 cp --only-show-errors [^ ]+ s3://ds-os-releases/' "${AWS_CALLS}" || true; }
rms() { grep -c -E '^aws s3 rm ' "${AWS_CALLS}" || true; }
# ordered <key>: the key's copy line precedes its stream-back, which precedes its last head-object
ordered() {
  local copy stream head
  copy=$(grep -n -E "^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/$1$" "${AWS_CALLS}" | cut -d: -f1 | head -1)
  stream=$(grep -n -E "^aws s3 cp --only-show-errors s3://ds-os-releases/$1 -$" "${AWS_CALLS}" | cut -d: -f1 | head -1)
  head=$(grep -n -E "^aws s3api head-object --bucket ds-os-releases --key $1 " "${AWS_CALLS}" | cut -d: -f1 | tail -1)
  echo "   (copy line ${copy:-?}, stream ${stream:-?}, head-object ${head:-?})"
  [ -n "${copy}" ] && [ -n "${stream}" ] && [ -n "${head}" ] && [ "${copy}" -lt "${stream}" ] && [ "${stream}" -lt "${head}" ]
}
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unwanted match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }

# ---- stubs -----------------------------------------------------------------------
mkdir -p "${tmp}/bin"
cat > "${tmp}/bin/aws" <<EOF
#!/usr/bin/env bash
# Wrapper: bucket-to-bucket cp and recursive rm here, everything else in the shared stub.
set -euo pipefail
root="\${FAKE_S3:?}"
case "\$1 \$2" in
  "s3 cp")
    args=(); for a in "\$@"; do case "\$a" in --only-show-errors|--quiet) ;; *) args+=("\$a") ;; esac; done
    if [[ "\${args[2]}" == s3://* && "\${args[3]}" == s3://* ]]; then
      [ -z "\${AWS_CALLS:-}" ] || echo "aws \$*" >> "\${AWS_CALLS}"
      src="\${root}/\${args[2]#s3://}" dst="\${root}/\${args[3]#s3://}"
      [ -f "\${src}" ] || { echo "fatal error: An error occurred (404) when calling the HeadObject operation: Key does not exist" >&2; exit 1; }
      mkdir -p "\$(dirname "\${dst}")"
      if [ -n "\${FAKE_AWS_CORRUPT:-}" ] && [[ "\${args[3]}" == *"\${FAKE_AWS_CORRUPT}"* ]]; then
        { cat "\${src}"; printf 'x'; } > "\${dst}"
      else
        cp "\${src}" "\${dst}"
      fi
      exit 0
    fi ;;
  "s3 rm")
    [ -z "\${AWS_CALLS:-}" ] || echo "aws \$*" >> "\${AWS_CALLS}"
    rec=0 url=""
    for a in "\$@"; do case "\$a" in --recursive) rec=1 ;; s3://*) url="\$a" ;; esac; done
    if [ "\${rec}" = 1 ]; then rm -rf "\${root}/\${url#s3://}"; else rm -f "\${root}/\${url#s3://}"; fi
    exit 0 ;;
esac
exec "${here}/stubs/aws" "\$@"
EOF
cat > "${tmp}/bin/gh" <<'EOF'
#!/usr/bin/env bash
# gh stub for ship.sh: releases are JSON state files FAKE_RELEASES/<tag with / as _>.json
# ({isDraft, isPrerelease, tagName, targetCommitish, body}); the manifest asset is
# <state>.asset.release-manifest.json. FAKE_GH_FAIL_EDIT=1 refuses the edit.
set -euo pipefail
[ -z "${GH_CALLS:-}" ] || echo "gh $*" >> "${GH_CALLS}"
store="${FAKE_RELEASES:?}"
if [ "$1" = api ]; then
  case "$2" in
    repos/*/actions/runs/*/approvals) [ -f "${FAKE_APPROVALS:-/nonexistent}" ] && cat "${FAKE_APPROVALS}" || echo '[]'; exit 0 ;;
    *) echo "gh stub: unsupported api $2" >&2; exit 1 ;;
  esac
fi
[ "$1" = release ] || { echo "gh stub: unsupported call: $*" >&2; exit 2; }
sub=$2 tag=$3
state="${store}/${tag//\//_}.json"
shift 3
case "${sub}" in
  view)
    [ -f "${state}" ] || { echo "release not found" >&2; exit 1; }
    fields=""
    while [ $# -gt 0 ]; do case "$1" in --json) fields="$2"; shift 2 ;; *) shift ;; esac; done
    python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1])); want = sys.argv[2].split(",")
print(json.dumps({k: d.get(k) for k in want}))' "${state}" "${fields}" ;;
  download)
    [ -f "${state}" ] || { echo "release not found" >&2; exit 1; }
    pattern="" dir="."
    while [ $# -gt 0 ]; do case "$1" in --pattern) pattern="$2"; shift 2 ;; --dir) dir="$2"; shift 2 ;; *) shift ;; esac; done
    [ "${pattern}" = release-manifest.json ] || { echo "gh stub: only release-manifest.json is served" >&2; exit 1; }
    asset="${state%.json}.asset.release-manifest.json"
    [ -f "${asset}" ] || { echo "no assets match the file pattern" >&2; exit 1; }
    mkdir -p "${dir}" && cp "${asset}" "${dir}/release-manifest.json" ;;
  edit)
    [ -f "${state}" ] || { echo "release not found" >&2; exit 1; }
    [ "${FAKE_GH_FAIL_EDIT:-0}" = 0 ] || { echo "HTTP 403: Resource not accessible by integration" >&2; exit 1; }
    pre="" notes="" title=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --title) title="$2"; shift 2 ;;
        --prerelease=false) pre=false; shift ;;
        --prerelease=true|--prerelease) pre=true; shift ;;
        --latest*) echo "gh stub: --latest must not be passed" >&2; exit 1 ;;
        --notes-file) notes="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    python3 -I -c '
import json, sys
p, pre, notes, title = sys.argv[1:5]
d = json.load(open(p))
if title: d["name"] = title
if pre: d["isPrerelease"] = pre == "true"
if notes: d["body"] = open(notes).read()
json.dump(d, open(p, "w"))' "${state}" "${pre}" "${notes}" "${title}" ;;
  *) echo "gh stub: unsupported call: $*" >&2; exit 2 ;;
esac
EOF
chmod +x "${tmp}/bin/aws" "${tmp}/bin/gh"

export PATH="${tmp}/bin:${here}/stubs:${PATH}"
export FAKE_S3="${tmp}/s3" FAKE_RELEASES="${tmp}/releases"
export GH_CALLS="${tmp}/gh.calls" AWS_CALLS="${tmp}/aws.calls"
export DS_ARTIFACTS_BUCKET=ds-yocto-artifacts RELEASE_S3_BUCKET=ds-os-releases
export GITHUB_REPOSITORY=ds/repo GITHUB_SERVER_URL=https://github.com
export GITHUB_RUN_ID=777 GITHUB_ACTOR=dexter GH_TOKEN=stub-token TOKEN_KIND=app
SRC="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
DST="${FAKE_S3}/${RELEASE_S3_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
TAG=v0.0.1
MACHINE=ds-arm64
FLAVOR_STD=ds_${MACHINE}_release
FLAVOR_HARD=ds_${MACHINE}_release-hardened
BUILD_STD=20261023T100000Z-555-standard
BUILD_HARD=20261023T110000Z-555-hardened
STATE="${FAKE_RELEASES}/v0.0.1.json"
ORIG_BODY=$'Release `0.0.1` (staging).\n\n- RC run: https://github.com/x/actions/runs/1\n'

# Two builds with real content (two debs, one extra file, the build manifest
# and its sums each); the manifest records the real digests.
seed_source() {
  rm -rf "${FAKE_S3}"
  local b d
  for b in "${MACHINE}/${BUILD_STD}" "${MACHINE}/${BUILD_HARD}"; do
    d="${SRC}/${b}"
    mkdir -p "${d}/deb" "${d}/extra"
    head -c 4096 /dev/urandom > "${d}/deb/ds-utils_1.0-1_arm64.deb"
    printf 'sdr deb %s' "${b}" > "${d}/deb/ds-sdr_1.0-1_arm64.deb"
    printf 'config %s' "${b}" > "${d}/extra/ds-config.tar.gz"
    printf '{"build": "%s"}\n' "${b}" > "${d}/manifest.json"
    printf 'sums %s\n' "${b}" > "${d}/SHA256SUMS"
  done
}
# write_manifest <out> <kind> <version>
write_manifest() {
  python3 -I - "$1" "$2" "$3" "${SRC}" "${SHA}" "${MACHINE}" "${FLAVOR_STD}" "${FLAVOR_HARD}" "${BUILD_STD}" "${BUILD_HARD}" <<'EOF'
import hashlib, json, os, sys
out, kind, version, src, sha, machine, std, hard, build_std, build_hard = sys.argv[1:11]
def entry(prefix, key, **more):
    p = os.path.join(src, prefix, key)
    data = open(p, 'rb').read()
    return dict({'key': key, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}, **more)
def build(flavor, build_id, hardened):
    prefix = f'{machine}/{build_id}'
    return {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id, 's3_prefix': prefix,
            'files': {'debs': [entry(prefix, 'deb/ds-utils_1.0-1_arm64.deb', package='ds-utils', version='1.0-1'),
                               entry(prefix, 'deb/ds-sdr_1.0-1_arm64.deb', package='ds-sdr', version='1.0-1')],
                      'extra': [entry(prefix, 'extra/ds-config.tar.gz')],
                      'manifest': entry(prefix, 'manifest.json'),
                      'sha256sums': entry(prefix, 'SHA256SUMS')}}
doc = {'schema': 1, 'kind': kind, 'product': 'ds', 'version': version, 'pending': '0.0.2', 'sha': sha,
       'line': 'main', 'run_id': '555', 'rc_run': '444', 'release_run': '555',
       'run_url': 'https://github.com/ds/repo/actions/runs/555',
       'builds': [build(std, build_std, False), build(hard, build_hard, True)]}
json.dump(doc, open(out, 'w'), indent=2)
EOF
}
# seed_release <isDraft> <isPrerelease> [kind]
seed_release() {
  rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"
  python3 -I -c '
import json, sys
json.dump({"isDraft": sys.argv[2] == "true", "isPrerelease": sys.argv[3] == "true", "tagName": "v0.0.1",
           "targetCommitish": sys.argv[4], "body": sys.argv[5]}, open(sys.argv[1], "w"))' "${STATE}" "$1" "$2" "${SHA}" "${ORIG_BODY}"
  write_manifest "${STATE%.json}.asset.release-manifest.json" "${3:-release}" 0.0.1
}
reset() { seed_source; seed_release false true; : > "${GH_CALLS}"; : > "${AWS_CALLS}"; }
# run_ship <--plan|--ship> [more args] -> $rc, $log
run_ship() {
  log="${tmp}/log"
  rc=0
  "${scripts}/ship.sh" "$@" --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
}
body() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["body"], end="")' "${STATE}"; }
pre() { python3 -I -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["isPrerelease"]).lower())' "${STATE}"; }
TOTAL=$(python3 -I -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(sum(f["bytes"] for b in m["builds"] for f in b["files"]["debs"] + b["files"]["extra"] + [b["files"]["manifest"], b["files"]["sha256sums"]]))' <(seed_source; seed_release false true; cat "${STATE%.json}.asset.release-manifest.json"))
DST_STD="release/ds/v0.0.1/${MACHINE}/standard"
DST_HARD="release/ds/v0.0.1/${MACHINE}/hardened"
DEB_A="${DST_STD}/ds-utils_1.0-1_arm64.deb"
DEB_H="${DST_HARD}/ds-utils_1.0-1_arm64.deb"

echo "# plan: every destination key with its size, the total, nothing copied or edited"
reset
run_ship --plan
sed 's/^/     | /' "${log}"
check "${rc}" 0 "exits 0"
for k in "${DEB_A}" "${DEB_H}" \
         "${DST_STD}/ds-sdr_1.0-1_arm64.deb" "${DST_STD}/ds-config.tar.gz" "${DST_STD}/manifest.json" "${DST_STD}/SHA256SUMS" \
         "${DST_HARD}/ds-sdr_1.0-1_arm64.deb" "${DST_HARD}/ds-config.tar.gz" "${DST_HARD}/manifest.json" "${DST_HARD}/SHA256SUMS"; do
  contains "${log}" " s3://ds-os-releases/${k}$" "lists ${k##*/v0.0.1/}"
done
contains "${log}" "^total +${TOTAL} +10 object\(s\) \+ release.json$" "total bytes over 10 objects"
contains "${log}" "\(deb:ds-utils\) +4096 +s3://ds-os-releases/${DEB_A}$" "deb row carries its package and size"
contains "${log}" "\(extra:ds-config.tar.gz\) +[0-9]+ +s3://ds-os-releases/${DST_STD}/ds-config.tar.gz$" "extra row names the file"
contains "${log}" "^plan: gh release edit ${TAG} --prerelease=false --title .v0.0.1. --notes-file" "names the gh edit with the bare title"
contains "${log}" "::notice::ship: plan only; nothing was copied or edited" "plan notice"
check "$(copies_in)$(rms)" 00 "no copy into the release bucket, no rm"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 0 "no release edit"
check "$(status test ! -d "${DST}")" 0 "release bucket untouched"
rc=0; "${scripts}/ship.sh" --plan --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "without --product the one configured product is used"
contains "${log}" "^== ship ds 0.0.1 \(plan\)" "and named in the header"

echo "# ship: copy, then verify each object by streaming, then release.json, then the edit"
reset
printf '[{"state":"approved","user":{"login":"eric"},"environments":[{"name":"production"}]},{"state":"rejected","user":{"login":"mallory"}}]' > "${tmp}/approvals.json"
FAKE_APPROVALS="${tmp}/approvals.json" run_ship --ship
check "${rc}" 0 "exits 0"
for k in "${DEB_A}" "${DEB_H}" "${DST_HARD}/SHA256SUMS"; do
  check "$(status ordered "${k}")" 0 "${k##*/v0.0.1/}: copied, then streamed back, then head-object"
done
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 10 "ten server-side copies"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-os-releases/.* -$' "${AWS_CALLS}")" 10 "ten stream-backs"
check "$(status cmp -s "${SRC}/${MACHINE}/${BUILD_STD}/deb/ds-utils_1.0-1_arm64.deb" "${DST}/${DEB_A}")" 0 "the standard build's deb landed byte for byte"
check "$(status cmp -s "${SRC}/${MACHINE}/${BUILD_HARD}/extra/ds-config.tar.gz" "${DST}/${DST_HARD}/ds-config.tar.gz")" 0 "the hardened build's extra file landed"
check "$(status test -f "${DST}/release/ds/v0.0.1/release.json")" 0 "release.json written"
check "$(python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["verification"], d["shipped_by"], d["shipped_run_url"], d["destination"]["bucket"], d["destination"]["prefix"],
      len(d["destination"]["keys"]), d["kind"], d["version"], d["sha"], len(d["builds"]), d["run_url"])' "${DST}/release/ds/v0.0.1/release.json")" \
  "ok dexter https://github.com/ds/repo/actions/runs/777 ds-os-releases release/ds/v0.0.1 10 release 0.0.1 ${SHA} 2 https://github.com/ds/repo/actions/runs/555" \
  "release.json: manifest kept plus the ship record"
contains "${DST}/release/ds/v0.0.1/release.json" "\"key\": \"${DEB_H}\"" "release.json lists the destination keys"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sorted({k["name"] for k in d["destination"]["keys"]}))' "${DST}/release/ds/v0.0.1/release.json")" \
  "['deb:ds-sdr', 'deb:ds-utils', 'extra:ds-config.tar.gz', 'manifest', 'sha256sums']" "release.json names each object by kind"
check "$(tail -1 "${AWS_CALLS}" | sed -E 's#aws s3 cp --only-show-errors [^ ]+ ##')" "s3://ds-os-releases/release/ds/v0.0.1/release.json" "release.json is the last aws call"
check "$(cut -d' ' -f1-3 "${GH_CALLS}" | tr '\n' ';')" "gh release view;gh release download;gh api repos/ds/repo/actions/runs/777/approvals;gh release edit;" "gh order: view, download, approvals, edit"
contains "${GH_CALLS}" "^gh release edit ${TAG} --repo ${GITHUB_REPOSITORY} --prerelease=false --title v0.0.1 --notes-file " "edit flips prerelease with a notes file"
lacks "${GH_CALLS}" "latest" "--latest is not passed"
check "$(pre)" false "release is a full release"
check "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("name", ""))' "${STATE}")" "v0.0.1" "the title drops (staging) and carries no product"
new_body=$(body)
check "${new_body:0:${#ORIG_BODY}}" "${ORIG_BODY}" "the previous body is kept verbatim at the top"
printf '%s' "${new_body}" > "${tmp}/body.md"
contains "${tmp}/body.md" "^## Shipped$" "evidence block header"
contains "${tmp}/body.md" "^- Approved by: eric$" "approver from the run's approvals (rejected ones left out)"
contains "${tmp}/body.md" "^- Ship run: https://github.com/ds/repo/actions/runs/777$" "ship run link"
contains "${tmp}/body.md" "^- Destination: \`s3://ds-os-releases/release/ds/v0.0.1/\`" "destination"
contains "${tmp}/body.md" "^\| \`${FLAVOR_HARD}\` \| \`s3://ds-os-releases/${DEB_H}\` \| \`[0-9a-f]{64}\` \| 4096 \|$" "checksum row"
contains "${tmp}/body.md" "^- Shipped at 20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z by \`dexter\`$" "timestamp and actor"
contains "${log}" "::notice::ship: ${TAG} is a full release; 10 object\(s\) and release.json under s3://ds-os-releases/release/ds/v0.0.1/" "success notice"

echo "# idempotent: a rerun after a complete ship copies and edits nothing"
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::ship: ${TAG} is already a full release and s3://ds-os-releases/release/ds/v0.0.1/release.json records verification ok \(shipped .* by dexter\); nothing to do" "says so"
check "$(copies_in)$(rms)" 00 "no copies, no rm"
check "$(grep -c -E '^gh release (edit|download)' "${GH_CALLS}")" 0 "no edit, no download"
run_ship --plan
check "${rc}" 0 "plan after a ship exits 0 too"

echo "# resume: objects present, release.json missing (died before the write): nothing copied, all verified, finished"
seed_release false true
rm "${DST}/release/ds/v0.0.1/release.json"
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 0 "no copy (every object exists with its size)"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-os-releases/.* -$' "${AWS_CALLS}")" 10 "every object streamed back again"
check "$(grep -c 'copy skipped' "${log}")" 10 "ten skips reported"
check "$(status test -f "${DST}/release/ds/v0.0.1/release.json")" 0 "release.json written"
check "$(pre)" false "release flipped"
check "$(grep -c '^## Shipped$' <(body))" 1 "one evidence block"

echo "# resume: release.json present but the release still a pre-release (died before the edit): verified again, edited once"
seed_release false true
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::ship: s3://ds-os-releases/release/ds/v0.0.1/release.json already records verification ok but ${TAG} is still a pre-release" "says why it continues"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 0 "no copy"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 1 "one edit"
check "$(pre)" false "release flipped"

echo "# a corrupted copy: this run's copies are removed, promote's release-manifest.json stays, release.json is not written, the release is not edited"
reset
mkdir -p "${DST}/release/ds/v0.0.1"
printf '{"kind": "release"}\n' > "${DST}/release/ds/v0.0.1/release-manifest.json"
FAKE_AWS_CORRUPT="${MACHINE}/hardened/ds-config.tar.gz" run_ship --ship
check "${rc}" 1 "exits 1"
contains "${log}" "::error::ship: ${FLAVOR_HARD} extra:ds-config.tar.gz: s3://ds-os-releases/${DST_HARD}/ds-config.tar.gz hashes to '[0-9a-f]{64}', the manifest says [0-9a-f]{64}" "names the object and both digests"
contains "${log}" "::error::ship: 1 object\(s\) did not verify; the copies were removed and ${TAG} stays a pre-release" "summary"
contains "${AWS_CALLS}" "^aws s3 rm --only-show-errors s3://ds-os-releases/${DEB_A}$" "each copied object is removed"
lacks "${AWS_CALLS}" "^aws s3 rm --recursive" "never a recursive rm"
check "$(status test ! -e "${DST}/${DEB_A}")$(status test ! -e "${DST}/${DST_HARD}/ds-config.tar.gz")" 00 "the copies are gone"
check "$(status test -f "${DST}/release/ds/v0.0.1/release-manifest.json")" 0 "promote's release-manifest.json stays"
check "$(status test ! -e "${DST}/release/ds/v0.0.1/release.json")" 0 "no release.json"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 0 "no edit"
check "$(pre)" true "still a pre-release"
check "$(body; echo x)" "${ORIG_BODY}x" "body unchanged"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 10 "every object was still attempted before the verdict"

echo "# a wrong size in the destination is recopied, not trusted"
reset
mkdir -p "${DST}/${DST_STD}"
printf 'short' > "${DST}/${DEB_A}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${AWS_CALLS}" "^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/${DEB_A}$" "the short object is copied again"
check "$(status cmp -s "${SRC}/${MACHINE}/${BUILD_STD}/deb/ds-utils_1.0-1_arm64.deb" "${DST}/${DEB_A}")" 0 "and is correct afterwards"

echo "# refusals: rc manifest, draft, a full release nobody shipped, wrong product/version, a build without its debs list, no bucket"
reset
seed_release false true rc
run_ship --ship
check "${rc}" 1 "rc manifest exits 1"
contains "${log}" "::error::ship: release-manifest.json is a 'rc' manifest, not a release manifest: only a promoted release ships \(this is the RC manifest; promote the sha first\)" "names the kind"
check "$(copies_in)$(rms)" 00 "nothing copied"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 0 "nothing edited"
run_ship --plan
check "${rc}" 1 "plan refuses the rc manifest too"

reset
seed_release true true
run_ship --ship
check "${rc}" 1 "draft exits 1"
contains "${log}" "::error::ship: release ${TAG} is a draft; promote did not finish publishing it" "names the draft"
check "$(grep -c -E '^gh release (download|edit)' "${GH_CALLS}")" 0 "no download, no edit"

reset
seed_release false false
run_ship --ship
check "${rc}" 1 "a full release without a verified ship exits 1"
contains "${log}" "::error::ship: release ${TAG} is not a pre-release and s3://ds-os-releases/release/ds/v0.0.1/release.json does not record a verified ship" "names the reason"
check "$(copies_in)$(rms)" 00 "nothing copied"

reset
rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"
run_ship --ship
check "${rc}" 1 "missing release exits 1"
contains "${log}" "::error::ship: release ${TAG} does not exist \(promote ds 0.0.1 first\)" "names the release"

reset
write_manifest "${STATE%.json}.asset.release-manifest.json" release 0.0.2
run_ship --ship
check "${rc}" 1 "manifest of another version exits 1"
contains "${log}" "::error::ship: release-manifest.json is version '0.0.2', not '0.0.1'" "names the version"

reset
python3 -I -c '
import json, sys
p = sys.argv[1]; m = json.load(open(p)); m["product"] = "portal"; json.dump(m, open(p, "w"))' "${STATE%.json}.asset.release-manifest.json"
run_ship --ship
check "${rc}" 1 "manifest of another product exits 1"
contains "${log}" "::error::ship: release-manifest.json is for product 'portal', not 'ds'" "names the product"

reset
python3 -I -c '
import json, sys
p = sys.argv[1]; m = json.load(open(p)); del m["builds"][1]["files"]["debs"]; json.dump(m, open(p, "w"))' "${STATE%.json}.asset.release-manifest.json"
run_ship --ship
check "${rc}" 1 "a build without its debs list exits 1"
contains "${log}" "::error::ship: ${FLAVOR_HARD}: files.debs is missing or not a list" "names the build and the list"
check "$(copies_in)$(rms)" 00 "nothing copied"

reset
rc=0; "${scripts}/ship.sh" --ship --product nosuch --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "unknown product exits 1"
contains "${log}" "::error::ship: product 'nosuch' is not in release/config.json" "names the config"
rc=0; "${scripts}/ship.sh" --ship --product ds --version 0.0.1-rc.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a suffixed version exits 1"
rc=0; "${scripts}/ship.sh" --ship --product ds --version 01.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a leading zero exits 1"
rc=0; RELEASE_S3_BUCKET='' "${scripts}/ship.sh" --plan --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "empty RELEASE_S3_BUCKET exits 1"
contains "${log}" "::error::ship: RELEASE_S3_BUCKET is empty" "names the variable"
rc=0; RELEASE_S3_BUCKET=ds-yocto-artifacts "${scripts}/ship.sh" --plan --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "the build bucket as the release bucket is fine"
contains "${log}" " s3://ds-yocto-artifacts/${DEB_A}$" "and ships under its release/ prefix"
rc=0; "${scripts}/ship.sh" --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 2 "no mode is a usage error"
rc=0; env -u GH_TOKEN -u GITHUB_TOKEN "${scripts}/ship.sh" --plan --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no token exits 1"
contains "${log}" "::error::GH_TOKEN is not set" "names the token"

echo "# edit refused on GITHUB_TOKEN: objects and release.json stay, the hint names the manual command"
reset
rc=0; FAKE_GH_FAIL_EDIT=1 TOKEN_KIND=github "${scripts}/ship.sh" --ship --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1"
contains "${log}" "::error::editing ${TAG} was refused, and this job ran on GITHUB_TOKEN .*ds-os-release-bot.*a human with bypass rights runs: gh release edit ${TAG} --prerelease=false --title .v0.0.1. --notes-file" "hint"
contains "${log}" "::error::ship: gh release edit ${TAG} failed; the objects and release.json are in place, rerun to finish" "the failure itself"
check "$(status test -f "${DST}/release/ds/v0.0.1/release.json")" 0 "release.json kept"
check "$(pre)" true "still a pre-release"
rc=0; FAKE_GH_FAIL_EDIT=1 TOKEN_KIND=app "${scripts}/ship.sh" --ship --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1 with the App too"
check "$(grep -c 'DS_RELEASE_APP_CLIENT_ID' "${log}")" 0 "no App hint when the App token was in use"

echo "# an install-trees release: files.debs [] and the tarball plus the info file in extra ship as any other objects"
TREE_BUILD=20261030T100000Z-555-standard
TARBALL=ds-sdk-build-jetson-orin-nx-a603-8gb-0123456.tar.gz
seed_trees() {
  rm -rf "${FAKE_S3}"
  local d="${SRC}/${MACHINE}/${TREE_BUILD}"
  mkdir -p "${d}"
  head -c 8192 /dev/urandom > "${d}/${TARBALL}"
  printf 'ds_commit=%s\nds_version=0.0.1\n' "${SHA}" > "${d}/sdk-build-info.txt"
  printf '{"build": "%s"}\n' "${TREE_BUILD}" > "${d}/manifest.json"
  printf 'sums %s\n' "${TREE_BUILD}" > "${d}/SHA256SUMS"
}
write_trees_manifest() {
  python3 -I - "$1" "${SRC}" "${SHA}" "${MACHINE}" "${FLAVOR_STD}" "${TREE_BUILD}" "${TARBALL}" <<'EOF'
import hashlib, json, os, sys
out, src, sha, machine, flavor, build_id, tarball = sys.argv[1:8]
prefix = f'{machine}/{build_id}'
def entry(key):
    data = open(os.path.join(src, prefix, key), 'rb').read()
    return {'key': key, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
doc = {'schema': 1, 'kind': 'release', 'product': 'ds', 'version': '0.0.1', 'pending': '0.0.2', 'sha': sha,
       'line': 'main', 'run_id': '555', 'run_url': 'https://github.com/ds/repo/actions/runs/555',
       'rc_run': {'run_id': '444', 'run_url': 'https://github.com/ds/repo/actions/runs/444', 'tag': 'rc/0123456'},
       'release_run': {'run_id': '555', 'run_url': 'https://github.com/ds/repo/actions/runs/555'},
       'builds': [{'flavor': flavor, 'machine': machine, 'hardened': False, 'build_id': build_id, 's3_prefix': prefix,
                   'files': {'debs': [], 'extra': [entry(tarball), entry('sdk-build-info.txt')],
                             'manifest': entry('manifest.json'), 'sha256sums': entry('SHA256SUMS')},
                   'sdk': {'uri': 's3://b/toolchain.sh', 'sha256': 'a' * 64, 'machine': 'jetson-orin-nx-a603-8gb', 'os_release': 'v2.0.0'}}]}
json.dump(doc, open(out, 'w'), indent=2)
EOF
}
seed_trees
rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"
python3 -I -c '
import json, sys
json.dump({"isDraft": False, "isPrerelease": True, "tagName": "v0.0.1", "targetCommitish": sys.argv[2], "body": sys.argv[3]}, open(sys.argv[1], "w"))' "${STATE}" "${SHA}" "${ORIG_BODY}"
write_trees_manifest "${STATE%.json}.asset.release-manifest.json"
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --plan
check "${rc}" 0 "plan exits 0"
contains "${log}" "\(extra:${TARBALL}\) +8192 +s3://ds-os-releases/${DST_STD}/${TARBALL}$" "the tarball is listed as an extra object"
contains "${log}" "\(extra:sdk-build-info.txt\) +[0-9]+ +s3://ds-os-releases/${DST_STD}/sdk-build-info.txt$" "the info file too"
lacks "${log}" "\(deb:" "no deb rows"
lacks "${log}" "Debs" "no Debs wording"
contains "${log}" "^total +[0-9]+ +4 object\(s\) \+ release.json$" "four objects: tarball, info file, manifest, sums"
run_ship --ship
check "${rc}" 0 "ship exits 0"
check "$(status cmp -s "${SRC}/${MACHINE}/${TREE_BUILD}/${TARBALL}" "${DST}/${DST_STD}/${TARBALL}")" 0 "the tarball landed byte for byte"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 4 "four server-side copies"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sorted({k["name"] for k in d["destination"]["keys"]}), d["builds"][0]["sdk"]["os_release"])' "${DST}/release/ds/v0.0.1/release.json")" \
  "['extra:ds-sdk-build-jetson-orin-nx-a603-8gb-0123456.tar.gz', 'extra:sdk-build-info.txt', 'manifest', 'sha256sums'] v2.0.0" "release.json names the objects and keeps the build's sdk"
check "$(pre)" false "release is a full release"

echo "# approvals unavailable: the block still lands, saying so"
reset
rc=0; env -u GITHUB_RUN_ID "${scripts}/ship.sh" --ship --product ds --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "exits 0 without a run id"
check "$(grep -c '^- Approved by: not recorded (no run context)$' <(body))" 1 "says the approvers are not recorded"
check "$(grep -c -E '^gh api' "${GH_CALLS}")" 0 "no approvals call without a run id"

echo
if [ "${fails}" = 0 ]; then echo "test_ship: all passed"; else echo "test_ship: ${fails} failure(s)"; exit 1; fi
