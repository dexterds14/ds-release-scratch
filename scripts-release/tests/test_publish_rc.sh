#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/publish-rc.sh (ENG-1931, ENG-2110). gh and aws
# are the stubs in tests/stubs: releases are markers under FAKE_RELEASES, S3
# is a directory tree under FAKE_S3, and every call is logged so the test can
# assert the draft -> upload -> publish order. No network.
#   bash scripts-release/tests/test_publish_rc.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }

export PATH="${here}/stubs:${PATH}"
export FAKE_S3="${tmp}/s3" FAKE_RELEASES="${tmp}/releases"
export GH_CALLS="${tmp}/gh.calls" AWS_CALLS="${tmp}/aws.calls"
export DS_ARTIFACTS_BUCKET=ds-yocto-artifacts
export GITHUB_REPOSITORY=DistributedSpectrum/ds
export GH_TOKEN=stub-token
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
RC=0.0.1-rc.12.github-0123456
TAG=rc/0123456
MARKER="${FAKE_RELEASES}/rc_0123456"
FAKE_SHA=$(printf 'b%.0s' $(seq 64))
MACHINE=ds-arm64
PV=1.0+git0+abc1234

# A release manifest as rc-gate.sh writes it: one build with two debs and an
# extra file, under the prefix the gate verified. (The hardened flavor is
# added only in the collision case below.)
MANIFEST="${tmp}/release-manifest.json"
python3 - "${MANIFEST}" "${SHA}" "${RC}" "${FAKE_SHA}" "${MACHINE}" "${PV}" <<'EOF'
import json, sys
out, sha, rc, fake, machine, pv = sys.argv[1:7]
def build(flavor, build_id, hardened):
    return {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id,
            's3_prefix': f'{machine}/{build_id}',
            'files': {'debs': [{'key': f'deb/ds-sdr_{pv}_arm64.deb', 'package': 'ds-sdr', 'version': pv, 'sha256': fake, 'bytes': 540},
                               {'key': f'deb/ds-utils_{pv}_arm64.deb', 'package': 'ds-utils', 'version': pv, 'sha256': fake, 'bytes': 670}],
                      'extra': [{'key': 'build-notes.txt', 'sha256': fake, 'bytes': 12}],
                      'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 100},
                      'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 300}}}
doc = {'schema': 1, 'kind': 'rc', 'product': 'ds', 'version': rc, 'pending': '0.0.1', 'sha': sha,
       'line': 'main', 'run_id': '424242',
       'run_url': 'https://github.com/DistributedSpectrum/ds/actions/runs/424242',
       'builds': [build('ds_ds-arm64_release', '20261021T110000Z-424242-standard', False)]}
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
EOF
seed_s3() {
  rm -rf "${FAKE_S3}"
  for b in 20261021T110000Z-424242-standard 20261021T100000Z-424242-hardened; do
    mkdir -p "${BUCKET}/${MACHINE}/${b}/deb"
    echo "{\"build\": \"${b}\"}" > "${BUCKET}/${MACHINE}/${b}/manifest.json"
    echo "${FAKE_SHA}  manifest.json" > "${BUCKET}/${MACHINE}/${b}/SHA256SUMS"
    printf 'sdr %s' "${b}" > "${BUCKET}/${MACHINE}/${b}/deb/ds-sdr_${PV}_arm64.deb"
    printf 'utils %s' "${b}" > "${BUCKET}/${MACHINE}/${b}/deb/ds-utils_${PV}_arm64.deb"
    printf 'build notes\n' > "${BUCKET}/${MACHINE}/${b}/build-notes.txt"
  done
}
reset() { seed_s3; rm -rf "${FAKE_RELEASES}"; mkdir -p "${FAKE_RELEASES}"; : > "${GH_CALLS}"; : > "${AWS_CALLS}"; }

# run_publish -> $rc, $log
run_publish() {
  log="${tmp}/log"
  rc=0
  "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
}

echo "# fresh publish: draft, upload, publish, then the S3 index"
reset
run_publish
check "${rc}" 0 "exits 0"
check "$(cat "${MARKER}")" published "release ends published"
check "$(cat "${MARKER}.title")" "RC ${RC}" "title carries no product (product_prefix false)"
check "$(cat "${MARKER}.target")" "${SHA}" "target sha"
check "$(LC_ALL=C sort "${MARKER}.assets" | tr '\n' ' ')" \
  "build-notes.txt ds-sdr_${PV}_arm64.deb ds-utils_${PV}_arm64.deb ds_ds-arm64_release.SHA256SUMS ds_ds-arm64_release.manifest.json release-manifest.json " \
  "assets: release-manifest, every deb and extra file, the flavor's manifest.json and SHA256SUMS"
check "$(cut -d' ' -f1-3 "${GH_CALLS}" | tr '\n' ';')" \
  "gh release view;gh release create;gh release upload;gh release edit;" "gh call order: view, create, upload, edit"
contains "${GH_CALLS}" "^gh release create ${TAG} --repo ${GITHUB_REPOSITORY} --draft --prerelease --target ${SHA} --title RC ${RC} --notes-file " "create is a draft pre-release at the sha"
contains "${GH_CALLS}" "^gh release upload ${TAG} --repo ${GITHUB_REPOSITORY} --clobber " "upload clobbers"
contains "${GH_CALLS}" "^gh release edit ${TAG} --repo ${GITHUB_REPOSITORY} --draft=false --prerelease$" "edit publishes as a pre-release"
# the deb assets are the build's objects, downloaded from the verified prefix
check "$(grep -c -E "s3 cp .* s3://ds-yocto-artifacts/${MACHINE}/20261021T110000Z-424242-standard/deb/" "${AWS_CALLS}")" 2 "both debs come from the build prefix"
# the S3 index is written after the publish
check "$(grep -n -E "s3 cp .* s3://ds-yocto-artifacts/rc/ds/0123456/release-manifest.json$" "${AWS_CALLS}" | cut -d: -f1)" \
  "$(wc -l < "${AWS_CALLS}")" "the S3 index copy is the last aws call"
cmp -s "${MANIFEST}" "${BUCKET}/rc/ds/0123456/release-manifest.json"; check $? 0 "S3 index is the release manifest"
contains "${MARKER}.notes" "^\| \`ds_ds-arm64_release\` \| \`20261021T110000Z-424242-standard\` \| \`s3://ds-yocto-artifacts/${MACHINE}/20261021T110000Z-424242-standard/\` \| 3 \| \`${FAKE_SHA}\` \|$" "notes table row: flavor, build id, prefix, artifact count (debs and extra files), manifest sha256"
contains "${MARKER}.notes" "actions/runs/424242" "notes link the run"
contains "${MARKER}.notes" "Pending version on the line: \`0.0.1\` \(a promote of this sha tags \`v0.0.1\`\)" "notes name the pending version and the bare tag"
contains "${log}" "::notice::publish-rc: ${TAG} published" "success notice"

echo "# an install-trees RC: files.debs [] and the tarball plus the info file in extra are the assets"
TARBALL=ds-sdk-build-jetson-orin-nx-a603-8gb-0123456.tar.gz
TREES_MANIFEST="${tmp}/trees-manifest.json"
python3 -I - "${TREES_MANIFEST}" "${SHA}" "${RC}" "${FAKE_SHA}" "${MACHINE}" "${TARBALL}" <<'EOF'
import json, sys
out, sha, rc, fake, machine, tarball = sys.argv[1:7]
build = {'flavor': 'ds_ds-arm64_release', 'machine': machine, 'hardened': False,
         'build_id': '20261030T110000Z-424242-standard', 's3_prefix': f'{machine}/20261030T110000Z-424242-standard',
         'files': {'debs': [],
                   'extra': [{'key': tarball, 'sha256': fake, 'bytes': 4096}, {'key': 'sdk-build-info.txt', 'sha256': fake, 'bytes': 60}],
                   'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 100},
                   'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 300}},
         'sdk': {'uri': 's3://b/toolchain.sh', 'sha256': 'a' * 64, 'machine': 'jetson-orin-nx-a603-8gb', 'os_release': 'v2.0.0'}}
doc = {'schema': 1, 'kind': 'rc', 'product': 'ds', 'version': rc, 'pending': '0.0.1', 'sha': sha,
       'line': 'main', 'run_id': '424242', 'run_url': 'https://github.com/DistributedSpectrum/ds/actions/runs/424242',
       'builds': [build]}
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
EOF
reset
d="${BUCKET}/${MACHINE}/20261030T110000Z-424242-standard"
mkdir -p "${d}"
head -c 4096 /dev/urandom > "${d}/${TARBALL}"
printf 'ds_commit=%s\nds_version=%s\n' "${SHA}" "${RC}" > "${d}/sdk-build-info.txt"
echo '{"build": "trees"}' > "${d}/manifest.json"
echo "${FAKE_SHA}  manifest.json" > "${d}/SHA256SUMS"
rc=0; "${scripts}/publish-rc.sh" "${TREES_MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "exits 0"
check "$(cat "${MARKER}")" published "release ends published"
check "$(LC_ALL=C sort "${MARKER}.assets" | tr '\n' ' ')" \
  "${TARBALL} ds_ds-arm64_release.SHA256SUMS ds_ds-arm64_release.manifest.json release-manifest.json sdk-build-info.txt " \
  "assets: release-manifest, the tarball, the info file, the flavor's manifest.json and SHA256SUMS"
contains "${MARKER}.notes" "^\| Flavor \| Build id \| S3 prefix \| Artifacts \| manifest.json sha256 \|$" "the notes table has an Artifacts column"
contains "${MARKER}.notes" "^\| \`ds_ds-arm64_release\` \| \`20261030T110000Z-424242-standard\` \| \`s3://ds-yocto-artifacts/${MACHINE}/20261030T110000Z-424242-standard/\` \| 2 \| \`${FAKE_SHA}\` \|$" "the row counts the tarball and the info file"
if grep -q 'Debs' "${MARKER}.notes"; then echo "FAIL notes carry Debs wording"; fails=$((fails + 1)); else echo "ok   no Debs wording in the notes"; fi
cmp -s "${TREES_MANIFEST}" "${BUCKET}/rc/ds/0123456/release-manifest.json"; check $? 0 "S3 index is the release manifest"

# back to the debs manifest for the cases below
reset
run_publish
check "${rc}" 0 "the debs manifest publishes again after a reset"

echo "# idempotent: the release already exists"
run_publish
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::publish-rc: release ${TAG} already exists; nothing to publish" "skip notice"
check "$(grep -c -E '^gh release (create|upload|edit)' "${GH_CALLS}")" 3 "no second create, upload or edit (counts from the first run only)"
check "$(grep -c 's3 cp' "${AWS_CALLS}")" 6 "no second S3 copy (counts from the first run only)"

echo "# resume: a draft left by an earlier run is finished, not recreated"
reset
echo draft > "${MARKER}"
run_publish
check "${rc}" 0 "exits 0"
contains "${log}" "exists as a draft .* resuming" "resume notice"
check "$(cut -d' ' -f1-3 "${GH_CALLS}" | tr '\n' ';')" "gh release view;gh release upload;gh release edit;" "no create on resume"
check "$(cat "${MARKER}")" published "draft ends published"

echo "# create refused on GITHUB_TOKEN: names what a human must do"
reset
rc=0; FAKE_GH_FAIL_CREATE=1 TOKEN_KIND=github "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1"
contains "${log}" "::error::creating ${TAG} was refused .*DS_RELEASE_APP_CLIENT_ID.*a human with bypass rights runs: gh release create ${TAG}" "hint names the App and the manual command"
contains "${log}" "::error::publish-rc: gh release create ${TAG} failed" "the failure itself"
check "$([ -f "${MARKER}" ] && echo created || echo absent)" absent "nothing created"
check "$(grep -c -E '^gh release (upload|edit)' "${GH_CALLS}")" 0 "no upload or edit after a failed create"
rc=0; FAKE_GH_FAIL_CREATE=1 TOKEN_KIND=app "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1 with the App too"
check "$(grep -c 'DS_RELEASE_APP_CLIENT_ID' "${log}")" 0 "no App hint when the App token was in use"

echo "# refuses to run without a token, with a non-rc manifest, or with a missing object"
reset
rc=0; env -u GH_TOKEN -u GITHUB_TOKEN "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no token exits 1"
contains "${log}" "::error::GH_TOKEN is not set" "names the token"
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); m["kind"]="release"; json.dump(m, open(sys.argv[2],"w"))' "${MANIFEST}" "${tmp}/release.json"
rc=0; "${scripts}/publish-rc.sh" "${tmp}/release.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "non-rc manifest exits 1"
check "$(wc -l < "${GH_CALLS}")" 0 "and never calls gh"
rm "${BUCKET}/${MACHINE}/20261021T110000Z-424242-standard/deb/ds-utils_${PV}_arm64.deb"
run_publish
check "${rc}" 1 "a deb missing from the build prefix exits 1"
contains "${log}" "::error::publish-rc: could not download ${MACHINE}/20261021T110000Z-424242-standard/deb/ds-utils_${PV//+/\\+}_arm64.deb" "names the object"
check "$(grep -c -E '^gh release (create|upload|edit)' "${GH_CALLS}")" 0 "before anything is created"

echo "# two builds recording the same asset name is refused before the create"
reset
python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = dict(m["builds"][0], flavor="ds_ds-arm64_release-hardened", hardened=True,
         build_id="20261021T100000Z-424242-hardened", s3_prefix="ds-arm64/20261021T100000Z-424242-hardened")
m["builds"].append(b)
json.dump(m, open(sys.argv[2], "w"))' "${MANIFEST}" "${tmp}/two.json"
rc=0; "${scripts}/publish-rc.sh" "${tmp}/two.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1"
contains "${log}" "::error::publish-rc: two builds record an asset named ds-sdr_${PV//+/\\+}_arm64.deb" "names the asset"
check "$(grep -c -E '^gh release (create|upload|edit)' "${GH_CALLS}")" 0 "nothing created"

echo
if [ "${fails}" = 0 ]; then echo "test_publish_rc: all passed"; else echo "test_publish_rc: ${fails} failure(s)"; exit 1; fi
