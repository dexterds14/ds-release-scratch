#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/publish-rc.sh (ENG-1931). gh and aws are the
# stubs in tests/stubs: releases are markers under FAKE_RELEASES, S3 is a
# directory tree under FAKE_S3, and every call is logged so the test can
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
export GITHUB_REPOSITORY=DistributedSpectrum/ds-tegra-distro
export GH_TOKEN=stub-token
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
RC=0.0.1-rc.12.github-0123456
TAG=rc/vertex/0123456
MARKER="${FAKE_RELEASES}/rc_vertex_0123456"
FAKE_SHA=$(printf 'b%.0s' $(seq 64))

# A release manifest as rc-gate.sh writes it, plus the two prefixes it names.
MANIFEST="${tmp}/release-manifest.json"
python3 - "${MANIFEST}" "${SHA}" "${RC}" "${FAKE_SHA}" <<'EOF'
import json, sys
out, sha, rc, fake = sys.argv[1:5]
def build(flavor, build_id, hardened):
    machine = 'jetson-orin-nx-a603-8gb'
    return {'flavor': flavor, 'machine': machine, 'hardened': hardened, 'build_id': build_id,
            's3_prefix': f'{machine}/{build_id}',
            'files': {'image': {'key': 'image-files/x.tar.gz', 'sha256': fake, 'bytes': 600000000},
                      'deb_feed': {'key': 'deb/deb-feed.tar.gz', 'sha256': fake, 'bytes': 8},
                      'package_list': {'key': 'deb/package-list.txt', 'sha256': fake, 'bytes': 12},
                      'manifest': {'key': 'manifest.json', 'sha256': fake, 'bytes': 100},
                      'sha256sums': {'key': 'SHA256SUMS', 'sha256': fake, 'bytes': 300}}}
doc = {'schema': 1, 'kind': 'rc', 'product': 'vertex', 'version': rc, 'pending': '0.0.1', 'sha': sha,
       'line': 'vertex/main', 'run_id': '424242',
       'run_url': 'https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/424242',
       'builds': [build('vertex_jetson-orin-nx-a603-8gb_release', '20261021T110000Z-424242-standard', False),
                  build('vertex_jetson-orin-nx-a603-8gb_release-hardened', '20261021T100000Z-424242-hardened', True)]}
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
EOF
seed_s3() {
  rm -rf "${FAKE_S3}"
  for b in 20261021T110000Z-424242-standard 20261021T100000Z-424242-hardened; do
    mkdir -p "${BUCKET}/jetson-orin-nx-a603-8gb/${b}"
    echo "{\"build\": \"${b}\"}" > "${BUCKET}/jetson-orin-nx-a603-8gb/${b}/manifest.json"
    echo "${FAKE_SHA}  manifest.json" > "${BUCKET}/jetson-orin-nx-a603-8gb/${b}/SHA256SUMS"
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
check "$(cat "${MARKER}.title")" "RC vertex ${RC}" "title"
check "$(cat "${MARKER}.target")" "${SHA}" "target sha"
check "$(LC_ALL=C sort "${MARKER}.assets" | tr '\n' ' ')" \
  "release-manifest.json vertex_jetson-orin-nx-a603-8gb_release-hardened.SHA256SUMS vertex_jetson-orin-nx-a603-8gb_release-hardened.manifest.json vertex_jetson-orin-nx-a603-8gb_release.SHA256SUMS vertex_jetson-orin-nx-a603-8gb_release.manifest.json " \
  "assets: release-manifest plus each flavor's manifest.json and SHA256SUMS"
check "$(cut -d' ' -f1-3 "${GH_CALLS}" | tr '\n' ';')" \
  "gh release view;gh release create;gh release upload;gh release edit;" "gh call order: view, create, upload, edit"
contains "${GH_CALLS}" "^gh release create ${TAG} --repo ${GITHUB_REPOSITORY} --draft --prerelease --target ${SHA} --title RC vertex ${RC} --notes-file " "create is a draft pre-release at the sha"
contains "${GH_CALLS}" "^gh release upload ${TAG} --repo ${GITHUB_REPOSITORY} --clobber " "upload clobbers"
contains "${GH_CALLS}" "^gh release edit ${TAG} --repo ${GITHUB_REPOSITORY} --draft=false --prerelease$" "edit publishes as a pre-release"
# the S3 index is written after the publish
check "$(grep -n -E "s3 cp .* s3://ds-yocto-artifacts/rc/vertex/0123456/release-manifest.json$" "${AWS_CALLS}" | cut -d: -f1)" \
  "$(wc -l < "${AWS_CALLS}")" "the S3 index copy is the last aws call"
cmp -s "${MANIFEST}" "${BUCKET}/rc/vertex/0123456/release-manifest.json"; check $? 0 "S3 index is the release manifest"
contains "${MARKER}.notes" "^\| \`vertex_jetson-orin-nx-a603-8gb_release\` \| \`20261021T110000Z-424242-standard\` \| \`s3://ds-yocto-artifacts/jetson-orin-nx-a603-8gb/20261021T110000Z-424242-standard/\` \| \`${FAKE_SHA}\` \|$" "notes table row"
contains "${MARKER}.notes" "actions/runs/424242" "notes link the run"
contains "${MARKER}.notes" "Pending version on the line: \`0.0.1\`" "notes name the pending version"
[ "$(grep -c -i 'sdk' "${MARKER}.notes" || true)" = 0 ]; check $? 0 "notes never mention an SDK: an RC build makes none (ENG-2126, promote only)"
contains "${log}" "::notice::publish-rc: ${TAG} published" "success notice"

echo "# idempotent: the release already exists"
run_publish
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::publish-rc: release ${TAG} already exists; nothing to publish" "skip notice"
check "$(grep -c -E '^gh release (create|upload|edit)' "${GH_CALLS}")" 3 "no second create, upload or edit (counts from the first run only)"
check "$(grep -c 's3 cp' "${AWS_CALLS}")" 5 "no second S3 copy (counts from the first run only)"

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
[ ! -f "${MARKER}" ]; check $? 0 "nothing created"
check "$(grep -c -E '^gh release (upload|edit)' "${GH_CALLS}")" 0 "no upload or edit after a failed create"
rc=0; FAKE_GH_FAIL_CREATE=1 TOKEN_KIND=app "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "exits 1 with the App too"
check "$(grep -c 'DS_RELEASE_APP_CLIENT_ID' "${log}")" 0 "no App hint when the App token was in use"

echo "# refuses to run without a token or with a non-rc manifest"
reset
rc=0; env -u GH_TOKEN -u GITHUB_TOKEN "${scripts}/publish-rc.sh" "${MANIFEST}" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no token exits 1"
contains "${log}" "::error::GH_TOKEN is not set" "names the token"
python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); m["kind"]="release"; json.dump(m, open(sys.argv[2],"w"))' "${MANIFEST}" "${tmp}/release.json"
rc=0; "${scripts}/publish-rc.sh" "${tmp}/release.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "non-rc manifest exits 1"
check "$(wc -l < "${GH_CALLS}")" 0 "and never calls gh"

echo
if [ "${fails}" = 0 ]; then echo "test_publish_rc: all passed"; else echo "test_publish_rc: ${fails} failure(s)"; exit 1; fi
