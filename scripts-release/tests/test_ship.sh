#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/ship.sh (ENG-1936). S3 is the aws stub in
# tests/stubs (a bucket tree under FAKE_S3) behind a wrapper that adds the
# two shapes ship.sh needs and the shared stub lacks: a bucket-to-bucket
# `s3 cp` (with a knob that corrupts one copy) and `s3 rm` (one object or
# --recursive). gh
# is a stub of this test's own: the release is a JSON state file under
# FAKE_RELEASES, its release-manifest.json asset sits next to it, `release
# edit` rewrites the state, `api .../approvals` answers from FAKE_APPROVALS.
# Every call is logged so the order copy -> verify can be asserted. No network.
#   bash scripts-release/tests/test_ship.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
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
    python3 -c '
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
    python3 -c '
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
export GITHUB_REPOSITORY=DistributedSpectrum/ds-tegra-distro GITHUB_SERVER_URL=https://github.com
export GITHUB_RUN_ID=777 GITHUB_ACTOR=dexter GH_TOKEN=stub-token TOKEN_KIND=app
SRC="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
DST="${FAKE_S3}/${RELEASE_S3_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
TAG=vertex/v0.0.1
STATE="${FAKE_RELEASES}/vertex_v0.0.1.json"
ORIG_BODY=$'Release `0.0.1` of **vertex** (staging).\n\n- RC run: https://github.com/x/actions/runs/1\n'

# Two builds with real content; the manifest records the real digests.
seed_source() {
  rm -rf "${FAKE_S3}"
  local b d
  for b in "jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard" "jetson-orin-nx-jackson/20261023T110000Z-555-hardened"; do
    d="${SRC}/${b}"
    mkdir -p "${d}/image-files" "${d}/deb"
    head -c 4096 /dev/urandom > "${d}/image-files/ds-image-sato-${b%%/*}.rootfs.tegraflash.tar.gz"
    printf 'deb feed %s' "${b}" > "${d}/deb/deb-feed.tar.gz"
    printf 'a.deb\nb.deb\n' > "${d}/deb/package-list.txt"
    printf '{"build": "%s"}\n' "${b}" > "${d}/manifest.json"
    printf 'sums %s\n' "${b}" > "${d}/SHA256SUMS"
  done
  # The a603 build made an update package; the jackson one did not (--with ota/none).
  d="${SRC}/jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard/ota"
  mkdir -p "${d}"
  printf 'swu payload' > "${d}/ds-ota-jetson-orin-nx-a603-8gb-0.0.1.swu"
  printf '{"name": "ds-ota-jetson-orin-nx-a603-8gb-0.0.1.swu"}\n' > "${d}/entry.json"
}
# write_manifest <out> <kind> <version>
write_manifest() {
  python3 - "$1" "$2" "$3" "${SRC}" "${SHA}" <<'EOF'
import hashlib, json, os, sys
out, kind, version, src, sha = sys.argv[1:6]
def entry(prefix, key):
    p = os.path.join(src, prefix, key)
    data = open(p, 'rb').read()
    return {'key': key, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
def build(flavor, machine, build_id, hardened):
    prefix = f'{machine}/{build_id}'
    b = {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id, 's3_prefix': prefix,
            'files': {'image': entry(prefix, f'image-files/ds-image-sato-{machine}.rootfs.tegraflash.tar.gz'),
                      'deb_feed': entry(prefix, 'deb/deb-feed.tar.gz'),
                      'package_list': entry(prefix, 'deb/package-list.txt'),
                      'manifest': entry(prefix, 'manifest.json'),
                      'sha256sums': entry(prefix, 'SHA256SUMS')}}
    ota = os.path.join(src, prefix, 'ota')
    if os.path.isdir(ota):
        swu = [f for f in os.listdir(ota) if f.endswith('.swu')][0]
        b['files']['swu'] = entry(prefix, f'ota/{swu}')
        b['files']['ota_entry'] = entry(prefix, 'ota/entry.json')
    sdk = os.path.join(src, prefix, 'sdk')
    if os.path.isdir(sdk):
        stem = [f for f in os.listdir(sdk) if f.endswith('.sh')][0][:-3]
        b['files']['sdk'] = entry(prefix, f'sdk/{stem}.sh')
        b['files']['sdk_host_manifest'] = entry(prefix, f'sdk/{stem}.host.manifest')
        b['files']['sdk_target_manifest'] = entry(prefix, f'sdk/{stem}.target.manifest')
    return b
doc = {'schema': 1, 'kind': kind, 'product': 'vertex', 'version': version, 'pending': '0.0.2', 'sha': sha,
       'line': 'vertex/main', 'run_id': '555', 'rc_run': '444', 'release_run': '555',
       'run_url': 'https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/555',
       'builds': [build('vertex_jetson-orin-nx-a603-8gb_release', 'jetson-orin-nx-a603-8gb', '20261023T100000Z-555-standard', False),
                  build('vertex_jetson-orin-nx-jackson_release-hardened', 'jetson-orin-nx-jackson', '20261023T110000Z-555-hardened', True)]}
json.dump(doc, open(out, 'w'), indent=2)
EOF
}
# add_sdk_source: the exported SDK of the a603 build (ENG-2126), three
# objects under sdk/; write_manifest then records files.sdk*.
add_sdk_source() {
  local d="${SRC}/jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard/sdk"
  mkdir -p "${d}"
  head -c 8192 /dev/urandom > "${d}/dsensos-glibc-x86_64-ds-image-sato-armv8a-jetson-orin-nx-a603-8gb-toolchain-0.0.1.sh"
  printf 'host pkgs\n' > "${d}/dsensos-glibc-x86_64-ds-image-sato-armv8a-jetson-orin-nx-a603-8gb-toolchain-0.0.1.host.manifest"
  printf 'target pkgs\n' > "${d}/dsensos-glibc-x86_64-ds-image-sato-armv8a-jetson-orin-nx-a603-8gb-toolchain-0.0.1.target.manifest"
}
# seed_release <isDraft> <isPrerelease> [kind]
seed_release() {
  rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"
  python3 -c '
import json, sys
json.dump({"isDraft": sys.argv[2] == "true", "isPrerelease": sys.argv[3] == "true", "tagName": "vertex/v0.0.1",
           "targetCommitish": sys.argv[4], "body": sys.argv[5]}, open(sys.argv[1], "w"))' "${STATE}" "$1" "$2" "${SHA}" "${ORIG_BODY}"
  write_manifest "${STATE%.json}.asset.release-manifest.json" "${3:-release}" 0.0.1
}
reset() { seed_source; seed_release false true; : > "${GH_CALLS}"; : > "${AWS_CALLS}"; }
# run_ship <--plan|--ship> [more args] -> $rc, $log
run_ship() {
  log="${tmp}/log"
  rc=0
  "${scripts}/ship.sh" "$@" --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
}
body() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["body"], end="")' "${STATE}"; }
pre() { python3 -c 'import json,sys; print(str(json.load(open(sys.argv[1]))["isPrerelease"]).lower())' "${STATE}"; }
TOTAL=$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1])); print(sum(f["bytes"] for b in m["builds"] for f in b["files"].values()))' <(seed_source; seed_release false true; cat "${STATE%.json}.asset.release-manifest.json"))
IMG_A="release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/ds-image-sato-jetson-orin-nx-a603-8gb.rootfs.tegraflash.tar.gz"
IMG_J="release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/ds-image-sato-jetson-orin-nx-jackson.rootfs.tegraflash.tar.gz"

echo "# plan: every destination key with its size, the total, nothing copied or edited"
reset
run_ship --plan
sed 's/^/     | /' "${log}"
check "${rc}" 0 "exits 0"
for k in "${IMG_A}" "${IMG_J}" \
         release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/deb-feed.tar.gz \
         release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/package-list.txt \
         release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/manifest.json \
         release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/SHA256SUMS \
         release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/deb-feed.tar.gz \
         release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/package-list.txt \
         release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/manifest.json \
         release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/SHA256SUMS; do
  contains "${log}" " s3://ds-os-releases/${k}$" "lists ${k##*/v0.0.1/}"
done
for k in release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/ds-ota-jetson-orin-nx-a603-8gb-0.0.1.swu \
         release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/entry.json; do
  contains "${log}" " s3://ds-os-releases/${k}$" "lists the update package object ${k##*/}"
done
contains "${log}" "^total +${TOTAL} +12 object\(s\) \+ release.json$" "total bytes over 12 objects"
contains "${log}" "\(image\) +4096 +s3://ds-os-releases/${IMG_A}$" "image row carries its size"
contains "${log}" "^plan: gh release edit ${TAG} --prerelease=false --title .vertex v0.0.1. --notes-file" "names the gh edit"
contains "${log}" "::notice::ship: plan only; nothing was copied or edited" "plan notice"
check "$(copies_in)$(rms)" 00 "no copy into the release bucket, no rm"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 0 "no release edit"
check "$(status test ! -d "${DST}")" 0 "release bucket untouched"

echo "# ship: copy, then verify each object by streaming, then release.json, then the edit"
reset
printf '[{"state":"approved","user":{"login":"eric"},"environments":[{"name":"production"}]},{"state":"rejected","user":{"login":"mallory"}}]' > "${tmp}/approvals.json"
FAKE_APPROVALS="${tmp}/approvals.json" run_ship --ship
check "${rc}" 0 "exits 0"
for k in "${IMG_A}" "${IMG_J}" release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/SHA256SUMS; do
  check "$(status ordered "${k}")" 0 "${k##*/v0.0.1/}: copied, then streamed back, then head-object"
done
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 12 "twelve server-side copies"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-os-releases/.* -$' "${AWS_CALLS}")" 12 "twelve stream-backs"
check "$(status cmp -s "${SRC}/jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard/image-files/ds-image-sato-jetson-orin-nx-a603-8gb.rootfs.tegraflash.tar.gz" "${DST}/${IMG_A}")" 0 "the a603 image landed byte for byte"
check "$(status cmp -s "${SRC}/jetson-orin-nx-jackson/20261023T110000Z-555-hardened/deb/package-list.txt" "${DST}/release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/package-list.txt")" 0 "the jackson package list landed"
check "$(status test -f "${DST}/release/vertex/v0.0.1/release.json")" 0 "release.json written"
check "$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["verification"], d["shipped_by"], d["shipped_run_url"], d["destination"]["bucket"], d["destination"]["prefix"],
      len(d["destination"]["keys"]), d["kind"], d["version"], d["sha"], len(d["builds"]), d["run_url"])' "${DST}/release/vertex/v0.0.1/release.json")" \
  "ok dexter https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/777 ds-os-releases release/vertex/v0.0.1 12 release 0.0.1 ${SHA} 2 https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/555" \
  "release.json: manifest kept plus the ship record"
contains "${DST}/release/vertex/v0.0.1/release.json" "\"key\": \"${IMG_J}\"" "release.json lists the destination keys"
check "$(tail -1 "${AWS_CALLS}" | sed -E 's#aws s3 cp --only-show-errors [^ ]+ ##')" "s3://ds-os-releases/release/vertex/v0.0.1/release.json" "release.json is the last aws call"
check "$(cut -d' ' -f1-3 "${GH_CALLS}" | tr '\n' ';')" "gh release view;gh release download;gh api repos/DistributedSpectrum/ds-tegra-distro/actions/runs/777/approvals;gh release edit;" "gh order: view, download, approvals, edit"
contains "${GH_CALLS}" "^gh release edit ${TAG} --repo ${GITHUB_REPOSITORY} --prerelease=false --title vertex v0.0.1 --notes-file " "edit flips prerelease with a notes file"
lacks "${GH_CALLS}" "latest" "--latest is not passed"
check "$(pre)" false "release is a full release"
check "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("name", ""))' "${STATE}")" "vertex v0.0.1" "the title drops (staging)"
new_body=$(body)
check "${new_body:0:${#ORIG_BODY}}" "${ORIG_BODY}" "the previous body is kept verbatim at the top"
printf '%s' "${new_body}" > "${tmp}/body.md"
contains "${tmp}/body.md" "^## Shipped$" "evidence block header"
contains "${tmp}/body.md" "^- Approved by: eric$" "approver from the run's approvals (rejected ones left out)"
contains "${tmp}/body.md" "^- Ship run: https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/777$" "ship run link"
contains "${tmp}/body.md" "^- Destination: \`s3://ds-os-releases/release/vertex/v0.0.1/\`" "destination"
contains "${tmp}/body.md" "^\| \`vertex_jetson-orin-nx-jackson_release-hardened\` \| \`s3://ds-os-releases/${IMG_J}\` \| \`[0-9a-f]{64}\` \| 4096 \|$" "checksum row"
contains "${tmp}/body.md" "^- Shipped at 20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z by \`dexter\`$" "timestamp and actor"
contains "${log}" "::notice::ship: ${TAG} is a full release; 12 object\(s\) and release.json under s3://ds-os-releases/release/vertex/v0.0.1/" "success notice"

echo "# idempotent: a rerun after a complete ship copies and edits nothing"
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::ship: ${TAG} is already a full release and s3://ds-os-releases/release/vertex/v0.0.1/release.json records verification ok \(shipped .* by dexter\); nothing to do" "says so"
check "$(copies_in)$(rms)" 00 "no copies, no rm"
check "$(grep -c -E '^gh release (edit|download)' "${GH_CALLS}")" 0 "no edit, no download"
run_ship --plan
check "${rc}" 0 "plan after a ship exits 0 too"

echo "# resume: objects present, release.json missing (died before the write): nothing copied, all verified, finished"
seed_release false true
rm "${DST}/release/vertex/v0.0.1/release.json"
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 0 "no copy (every object exists with its size)"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-os-releases/.* -$' "${AWS_CALLS}")" 12 "every object streamed back again"
check "$(grep -c 'copy skipped' "${log}")" 12 "twelve skips reported"
check "$(status test -f "${DST}/release/vertex/v0.0.1/release.json")" 0 "release.json written"
check "$(pre)" false "release flipped"
check "$(grep -c '^## Shipped$' <(body))" 1 "one evidence block"

echo "# resume: release.json present but the release still a pre-release (died before the edit): verified again, edited once"
seed_release false true
: > "${GH_CALLS}"; : > "${AWS_CALLS}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::ship: s3://ds-os-releases/release/vertex/v0.0.1/release.json already records verification ok but ${TAG} is still a pre-release" "says why it continues"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 0 "no copy"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 1 "one edit"
check "$(pre)" false "release flipped"

echo "# a corrupted copy: this run's copies are removed, promote's release-manifest.json stays, release.json is not written, the release is not edited"
reset
mkdir -p "${DST}/release/vertex/v0.0.1"
printf '{"kind": "release"}\n' > "${DST}/release/vertex/v0.0.1/release-manifest.json"
FAKE_AWS_CORRUPT="jetson-orin-nx-jackson/hardened/deb-feed.tar.gz" run_ship --ship
check "${rc}" 1 "exits 1"
contains "${log}" "::error::ship: vertex_jetson-orin-nx-jackson_release-hardened deb_feed: s3://ds-os-releases/release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/deb-feed.tar.gz hashes to '[0-9a-f]{64}', the manifest says [0-9a-f]{64}" "names the object and both digests"
contains "${log}" "::error::ship: 1 object\(s\) did not verify; the copies were removed and ${TAG} stays a pre-release" "summary"
contains "${AWS_CALLS}" "^aws s3 rm --only-show-errors s3://ds-os-releases/${IMG_A}$" "each copied object is removed"
lacks "${AWS_CALLS}" "^aws s3 rm --recursive" "never a recursive rm"
check "$(status test ! -e "${DST}/${IMG_A}")$(status test ! -e "${DST}/release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/deb-feed.tar.gz")" 00 "the copies are gone"
check "$(status test -f "${DST}/release/vertex/v0.0.1/release-manifest.json")" 0 "promote's release-manifest.json stays"
check "$(status test ! -e "${DST}/release/vertex/v0.0.1/release.json")" 0 "no release.json"
check "$(grep -c -E '^gh release edit' "${GH_CALLS}")" 0 "no edit"
check "$(pre)" true "still a pre-release"
check "$(body; echo x)" "${ORIG_BODY}x" "body unchanged"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 12 "every object was still attempted before the verdict"

echo "# a wrong size in the destination is recopied, not trusted"
reset
mkdir -p "${DST}/release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard"
printf 'short' > "${DST}/${IMG_A}"
run_ship --ship
check "${rc}" 0 "exits 0"
contains "${AWS_CALLS}" "^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/${IMG_A}$" "the short object is copied again"
check "$(status cmp -s "${SRC}/jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard/image-files/ds-image-sato-jetson-orin-nx-a603-8gb.rootfs.tegraflash.tar.gz" "${DST}/${IMG_A}")" 0 "and is correct afterwards"

echo "# the exported SDK (ENG-2126): listed in the plan and copied with the release when the manifest records one"
seed_source; add_sdk_source; seed_release false true; : > "${GH_CALLS}"; : > "${AWS_CALLS}"
SDK_DST="release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/dsensos-glibc-x86_64-ds-image-sato-armv8a-jetson-orin-nx-a603-8gb-toolchain-0.0.1"
run_ship --plan
check "${rc}" 0 "plan exits 0"
for k in "${SDK_DST}.sh" "${SDK_DST}.host.manifest" "${SDK_DST}.target.manifest"; do
  contains "${log}" " s3://ds-os-releases/${k}$" "lists ${k##*/}"
done
contains "${log}" "\(sdk\) +8192 +s3://ds-os-releases/${SDK_DST}.sh$" "the installer row carries its size"
contains "${log}" "15 object\(s\) \+ release.json$" "fifteen objects with the SDK"
run_ship --ship
check "${rc}" 0 "ship exits 0"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 15 "fifteen server-side copies"
check "$(status ordered "${SDK_DST}.sh")" 0 "installer: copied, then streamed back, then head-object"
check "$(status cmp -s "${SRC}/jetson-orin-nx-a603-8gb/20261023T100000Z-555-standard/sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-jetson-orin-nx-a603-8gb-toolchain-0.0.1.sh" "${DST}/${SDK_DST}.sh")" 0 "the installer landed byte for byte"
check "$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(len(d["destination"]["keys"]), sorted(k["name"] for k in d["destination"]["keys"] if k["name"].startswith("sdk")))' "${DST}/release/vertex/v0.0.1/release.json")" \
  "15 ['sdk', 'sdk_host_manifest', 'sdk_target_manifest']" "release.json lists the three SDK keys"
contains "$(body > "${tmp}/body2.md"; echo "${tmp}/body2.md")" "\| \`vertex_jetson-orin-nx-a603-8gb_release\` \| \`s3://ds-os-releases/${SDK_DST}.sh\` \| \`[0-9a-f]{64}\` \| 8192 \|" "evidence row for the installer"

echo "# refusals: rc manifest, draft, a full release nobody shipped, wrong product/version, no bucket"
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
contains "${log}" "::error::ship: release ${TAG} is not a pre-release and s3://ds-os-releases/release/vertex/v0.0.1/release.json does not record a verified ship" "names the reason"
check "$(copies_in)$(rms)" 00 "nothing copied"

reset
rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"
run_ship --ship
check "${rc}" 1 "missing release exits 1"
contains "${log}" "::error::ship: release ${TAG} does not exist \(promote vertex 0.0.1 first\)" "names the release"

reset
write_manifest "${STATE%.json}.asset.release-manifest.json" release 0.0.2
run_ship --ship
check "${rc}" 1 "manifest of another version exits 1"
contains "${log}" "::error::ship: release-manifest.json is version '0.0.2', not '0.0.1'" "names the version"

reset
rc=0; "${scripts}/ship.sh" --ship --product aggregator --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "manifest of another product exits 1"
contains "${log}" "::error::ship: release aggregator/v0.0.1 does not exist" "the release is looked up by product"
rc=0; "${scripts}/ship.sh" --ship --product nosuch --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "unknown product exits 1"
contains "${log}" "::error::ship: product 'nosuch' is not in release/config.json" "names the config"
rc=0; "${scripts}/ship.sh" --ship --product vertex --version 0.0.1-rc.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a suffixed version exits 1"
rc=0; "${scripts}/ship.sh" --ship --product vertex --version 01.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a leading zero exits 1"
rc=0; RELEASE_S3_BUCKET= "${scripts}/ship.sh" --plan --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "empty RELEASE_S3_BUCKET exits 1"
contains "${log}" "::error::ship: RELEASE_S3_BUCKET is empty" "names the variable"
rc=0; RELEASE_S3_BUCKET=ds-yocto-artifacts "${scripts}/ship.sh" --plan --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "the build bucket as the release bucket is fine"
contains "${log}" " s3://ds-yocto-artifacts/${IMG_A}$" "and ships under its release/ prefix"
rc=0; "${scripts}/ship.sh" --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 2 "no mode is a usage error"
rc=0; env -u GH_TOKEN -u GITHUB_TOKEN "${scripts}/ship.sh" --plan --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no token exits 1"
contains "${log}" "::error::GH_TOKEN is not set" "names the token"

echo "# edit refused on GITHUB_TOKEN: objects and release.json stay, the hint names the manual command"
reset
rc=0; FAKE_GH_FAIL_EDIT=1 TOKEN_KIND=github "${scripts}/ship.sh" --ship --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1"
contains "${log}" "::error::editing ${TAG} was refused, and this job ran on GITHUB_TOKEN .*a human with bypass rights runs: gh release edit ${TAG} --prerelease=false --title .vertex v0.0.1. --notes-file" "hint"
contains "${log}" "::error::ship: gh release edit ${TAG} failed; the objects and release.json are in place, rerun to finish" "the failure itself"
check "$(status test -f "${DST}/release/vertex/v0.0.1/release.json")" 0 "release.json kept"
check "$(pre)" true "still a pre-release"
rc=0; FAKE_GH_FAIL_EDIT=1 TOKEN_KIND=app "${scripts}/ship.sh" --ship --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1 with the App too"
check "$(grep -c 'DS_RELEASE_APP_CLIENT_ID' "${log}")" 0 "no App hint when the App token was in use"

echo "# approvals unavailable: the block still lands, saying so"
reset
rc=0; env -u GITHUB_RUN_ID "${scripts}/ship.sh" --ship --product vertex --version 0.0.1 > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "exits 0 without a run id"
check "$(grep -c '^- Approved by: not recorded (no run context)$' <(body))" 1 "says the approvers are not recorded"
check "$(grep -c -E '^gh api' "${GH_CALLS}")" 0 "no approvals call without a run id"

echo "# a build without a deb feed (read-only flavor): shipped without one, nothing missing reported"
reset
J="jetson-orin-nx-jackson/20261023T110000Z-555-hardened"
rm -r "${SRC}/${J}/deb"
python3 - "${STATE%.json}.asset.release-manifest.json" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for b in d['builds']:
    if b['machine'] == 'jetson-orin-nx-jackson':
        del b['files']['deb_feed'], b['files']['package_list']
json.dump(d, open(p, 'w'), indent=2)
EOF
printf '[{"state":"approved","user":{"login":"eric"},"environments":[{"name":"production"}]}]' > "${tmp}/approvals.json"
FAKE_APPROVALS="${tmp}/approvals.json" run_ship --ship
check "${rc}" 0 "exits 0"
check "$(grep -c -E '^aws s3 cp --only-show-errors s3://ds-yocto-artifacts/.* s3://ds-os-releases/' "${AWS_CALLS}")" 10 "ten server-side copies (no feed for the jackson build)"
check "$(status test ! -e "${DST}/release/vertex/v0.0.1/jetson-orin-nx-jackson/hardened/deb-feed.tar.gz")$(status test -f "${DST}/release/vertex/v0.0.1/jetson-orin-nx-a603-8gb/standard/deb-feed.tar.gz")" 00 "jackson has no feed in the release, the a603 feed is there"
check "$(grep -c '::error::' "${log}")" 0 "no error annotations"

echo "# refusal: a build lists the deb feed without its package list"
reset
python3 - "${STATE%.json}.asset.release-manifest.json" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
del d['builds'][1]['files']['package_list']
json.dump(d, open(p, 'w'), indent=2)
EOF
run_ship --plan
check "${rc}" 1 "half a deb feed exits 1"
contains "${log}" "vertex_jetson-orin-nx-jackson_release-hardened: files lists deb_feed without its pair" "names the build and the missing pair"

echo
if [ "${fails}" = 0 ]; then echo "test_ship: all passed"; else echo "test_ship: ${fails} failure(s)"; exit 1; fi
