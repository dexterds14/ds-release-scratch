#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/rc-gate.sh (ENG-1931). aws is the stub in
# tests/stubs (a fake bucket tree under FAKE_S3); no network, no real S3.
#   bash scripts-release/tests/test_rc_gate.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
# contains <file> <pattern> <name>
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }

export PATH="${here}/stubs:${PATH}"
export FAKE_S3="${tmp}/s3"
export DS_ARTIFACTS_BUCKET=ds-yocto-artifacts
export RELEASE_LINE=vertex/main PENDING_VERSION=0.0.1
export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=DistributedSpectrum/ds-tegra-distro
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
OTHER_SHA=fedcba9876543210fedcba9876543210fedcba98
RUN=424242
RC=0.0.1-rc.12.github-0123456
MACHINE=jetson-orin-nx-a603-8gb
MATRIX='{"include":[{"flavor":"vertex_jetson-orin-nx-a603-8gb_release"},{"flavor":"vertex_jetson-orin-nx-a603-8gb_release-hardened"}]}'
FAKE_SHA=$(printf 'a%.0s' $(seq 64))

# make_build <build_id> <flavor> <sha> <build_version> [image_bytes] [--no-manifest]
# One complete per-build prefix as yocto-build.yml leaves it: the three
# artifacts (the image is a sparse file of the given size; the gate never
# hashes it), manifest.json and SHA256SUMS (manifest.json's real digest).
# FEED=none leaves out the deb feed and package list, as a read-only flavor's
# build does; FEED=half lists the deb feed without its package list.
make_build() {
  local build_id=$1 flavor=$2 sha=$3 ver=$4 bytes=${5:-600000000} nomanifest=${6:-}
  local d="${BUCKET}/${MACHINE}/${build_id}" hardened=false product=${flavor%%_*}
  case "${flavor}" in *-hardened) hardened=true ;; esac
  mkdir -p "${d}/image-files" "${d}/deb"
  truncate -s "${bytes}" "${d}/image-files/ds-image-sato-${MACHINE}.rootfs.tegraflash.tar.gz"
  printf 'deb feed' > "${d}/deb/deb-feed.tar.gz"
  printf 'a.deb\nb.deb\n' > "${d}/deb/package-list.txt"
  [ -z "${nomanifest}" ] || return 0
  python3 - "${d}" "${flavor}" "${product}" "${hardened}" "${sha}" "${ver}" "${bytes}" "${FAKE_SHA}" "${MACHINE}" "${FEED:-full}" <<'EOF'
import hashlib, json, os, sys
d, flavor, product, hardened, sha, ver, nbytes, fake, machine, feed = sys.argv[1:11]
image_key = f'image-files/ds-image-sato-{machine}.rootfs.tegraflash.tar.gz'
m = {
    'job_id': '424242', 'build_datetime': '20261021T120000Z',
    'buildparams': {'hardened': hardened == 'true', 'machine': machine, 'flavor': flavor, 'product': product,
                    'build_type': 'release', 'modifiers': [], 'build_version': ver, 'release_line': 'vertex/main'},
    'commit_hashes': [{'name': 'meta-dsensos', 'commit': '1' * 40, 'tag': None},
                      {'name': 'ds-tegra-distro', 'commit': sha, 'branch': 'vertex/main', 'tag': None}],
    'layers': [], 'distro': 'dsensos', 'image': 'ds-image-sato', 'deb_count': 2,
    'artifacts': {
        'image': {'key': image_key, 'sha256': fake, 'bytes': int(nbytes)},
        'deb_feed': {'key': 'deb/deb-feed.tar.gz', 'sha256': fake, 'bytes': 8},
        'package_list': {'key': 'deb/package-list.txt', 'sha256': fake, 'bytes': 12},
    },
}
if feed == 'none':
    del m['artifacts']['deb_feed'], m['artifacts']['package_list']
elif feed == 'half':
    del m['artifacts']['package_list']
data = (json.dumps(m, indent=2) + '\n').encode()
with open(os.path.join(d, 'manifest.json'), 'wb') as f:
    f.write(data)
with open(os.path.join(d, 'SHA256SUMS'), 'w') as f:
    f.write(f'{fake}  {image_key}\n')
    if feed != 'none':
        f.write(f'{fake}  deb/deb-feed.tar.gz\n')
    if feed == 'full':
        f.write(f'{fake}  deb/package-list.txt\n')
    f.write(f'{hashlib.sha256(data).hexdigest()}  manifest.json\n')
EOF
}

# add_ota <build_id> [ok|badsha|noentry]: the update package yocto-build
# uploads for an A/B build (ota/<name>.swu, ota/entry.json) and the two
# lines its upload step appends to SHA256SUMS.
add_ota() {
  local d="${BUCKET}/${MACHINE}/$1" mode=${2:-ok} name="ds-ota-${MACHINE}-${RC}.swu" sum
  mkdir -p "${d}/ota"
  printf 'swu payload' > "${d}/ota/${name}"
  sum=$(printf 'b%.0s' $(seq 64))
  if [ "${mode}" = badsha ]; then
    printf '{"name": "%s", "sha256": "%s"}\n' "${name}" "$(printf 'c%.0s' $(seq 64))" > "${d}/ota/entry.json"
  else
    printf '{"name": "%s", "sha256": "%s"}\n' "${name}" "${sum}" > "${d}/ota/entry.json"
  fi
  printf '%s  ota/%s\n' "${sum}" "${name}" >> "${d}/SHA256SUMS"
  [ "${mode}" = noentry ] || printf '%s  ota/entry.json\n' "$(sha256sum "${d}/ota/entry.json" | cut -d' ' -f1)" >> "${d}/SHA256SUMS"
}

# add_sdk <build_id> [ok|small|nomanifest|extra]: the exported SDK yocto-build
# uploads with build_sdk (sdk/<installer>.sh and the two package manifests;
# only the promote build asks for it, ENG-2126),
# recorded in manifest.json artifacts.sdk* and listed in SHA256SUMS. The
# installer is a sparse file above the 1 GB floor (small: 1 MB); nomanifest
# leaves artifacts.sdk_target_manifest out of manifest.json; extra lists an
# sdk/ object in SHA256SUMS that the manifest does not record.
add_sdk() {
  local d="${BUCKET}/${MACHINE}/$1" mode=${2:-ok} bytes=2000000000
  local stem="dsensos-glibc-x86_64-ds-image-sato-armv8a-${MACHINE}-toolchain-0.0.1"
  [ "${mode}" != small ] || bytes=1048576
  mkdir -p "${d}/sdk"
  truncate -s "${bytes}" "${d}/sdk/${stem}.sh"
  printf 'host pkgs\n' > "${d}/sdk/${stem}.host.manifest"
  printf 'target pkgs\n' > "${d}/sdk/${stem}.target.manifest"
  python3 - "${d}" "${stem}" "${bytes}" "${mode}" <<'EOF2'
import hashlib, json, os, sys
d, stem, nbytes, mode = sys.argv[1:5]
with open(os.path.join(d, 'manifest.json')) as f:
    m = json.load(f)
fake = 'd' * 64
def entry(name, size):
    return {'key': f'sdk/{name}', 'sha256': hashlib.sha256(open(os.path.join(d, 'sdk', name), 'rb').read()).hexdigest() if size < 100000 else fake, 'bytes': size}
m['buildparams']['sdk'] = True
m['artifacts']['sdk'] = entry(f'{stem}.sh', int(nbytes))
m['artifacts']['sdk_host_manifest'] = entry(f'{stem}.host.manifest', 10)
m['artifacts']['sdk_target_manifest'] = entry(f'{stem}.target.manifest', 12)
if mode == 'nomanifest':
    del m['artifacts']['sdk_target_manifest']
data = (json.dumps(m, indent=2) + '\n').encode()
with open(os.path.join(d, 'manifest.json'), 'wb') as f:
    f.write(data)
lines = []
with open(os.path.join(d, 'SHA256SUMS')) as f:
    for line in f:
        if not line.endswith(' manifest.json\n'):
            lines.append(line)
lines.append(f'{hashlib.sha256(data).hexdigest()}  manifest.json\n')
for name in ('sdk', 'sdk_host_manifest', 'sdk_target_manifest'):
    a = m['artifacts'].get(name)
    if a:
        lines.append(f"{a['sha256']}  {a['key']}\n")
if mode == 'extra':
    lines.append(f'{fake}  sdk/{stem}.testdata.json\n')
with open(os.path.join(d, 'SHA256SUMS'), 'w') as f:
    f.writelines(lines)
EOF2
}

reset() { rm -rf "${FAKE_S3}"; mkdir -p "${BUCKET}/${MACHINE}"; }

# run_gate <out> -> exit code in $rc, log in $log
run_gate() {
  log="${tmp}/log"
  rc=0
  "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" vertex "${RC}" "${MATRIX}" "$1" > "${log}" 2>&1 || rc=$?
}

echo "# positive: both flavors verified, newest complete prefix wins, other runs ignored"
reset
make_build "20261021T100000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "0.0.1-rc.11.github-0123456"   # older, stale: must not be picked
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"                          # the newest complete one
make_build "20261021T120000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}" 600000000 --no-manifest   # newer but died before manifest
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
make_build "20261021T120000Z-999999-standard" vertex_jetson-orin-nx-a603-8gb_release "${OTHER_SHA}" "0.0.1-rc.13.github-fedcba9"  # another run, newer
out="${tmp}/release-manifest.json"
run_gate "${out}"
check "${rc}" 0 "positive case exits 0"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
print(m["schema"], m["kind"], m["product"], m["version"], m["pending"], m["sha"], m["line"], m["run_id"], len(m["builds"]),
      b["vertex_jetson-orin-nx-a603-8gb_release"]["build_id"], b["vertex_jetson-orin-nx-a603-8gb_release-hardened"]["s3_prefix"],
      b["vertex_jetson-orin-nx-a603-8gb_release-hardened"]["hardened"], sorted(b["vertex_jetson-orin-nx-a603-8gb_release"]["files"]),
      b["vertex_jetson-orin-nx-a603-8gb_release"]["files"]["image"]["bytes"], m["run_url"])
' "${out}")" \
  "1 rc vertex ${RC} 0.0.1 ${SHA} vertex/main ${RUN} 2 20261021T110000Z-${RUN}-standard ${MACHINE}/20261021T100000Z-${RUN}-hardened True ['deb_feed', 'image', 'manifest', 'package_list', 'sha256sums'] 600000000 https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/${RUN}" \
  "release-manifest.json content"
contains "${log}" "20261021T120000Z-${RUN}-standard: no manifest.json" "incomplete newer prefix is reported and skipped"
check "$(grep -c '::error::' "${log}")" 0 "no error annotations on success"

echo "# negative: a missing object"
rm "${BUCKET}/${MACHINE}/20261021T100000Z-${RUN}-hardened/deb/deb-feed.tar.gz"
run_gate "${tmp}/out2.json"
check "${rc}" 1 "missing deb feed exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release-hardened: .*deb/deb-feed.tar.gz \(deb_feed\) does not exist" "names the flavor and the object"
[ ! -f "${tmp}/out2.json" ]; check $? 0 "no manifest written on failure"

echo "# negative: wrong sha in the manifest"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${OTHER_SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out3.json"
check "${rc}" 1 "wrong sha exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: manifest commit_hashes\[ds-tegra-distro\].commit is '${OTHER_SHA}', not ${SHA}" "names the sha check"

echo "# negative: wrong build_version"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "0.0.1-rc.11.github-0123456"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out4.json"
check "${rc}" 1 "wrong build_version exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: manifest buildparams.build_version is '0.0.1-rc.11.github-0123456', not the plan's '${RC}'" "names the build_version check"

echo "# negative: small tarball"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}" 1048576
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out5.json"
check "${rc}" 1 "1 MB tarball exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: .*is 1048576 bytes, below the 524288000 byte floor" "names the floor"

echo "# negative: no prefix for the run, and SHA256SUMS missing"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
rm "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/SHA256SUMS"
run_gate "${tmp}/out6.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release-hardened: no build prefix s3://ds-yocto-artifacts/${MACHINE}/\*-${RUN}-hardened/" "names the missing prefix"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: ${MACHINE}/20261021T110000Z-${RUN}-standard/SHA256SUMS is missing" "names the missing SHA256SUMS"
check "$(grep -c '::error::rc-gate: vertex_' "${log}")" 2 "every flavor is checked before exiting"

echo "# negative: a flavor of another product, and a bad matrix"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" vertex "${RC}" '["aggregator_jetson-orin-nx-a603-8gb_release"]' "${tmp}/out7.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "foreign product flavor exits 1"
contains "${log}" "::error::rc-gate: aggregator_jetson-orin-nx-a603-8gb_release: not a vertex flavor" "names the product check"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" vertex "${RC}" '[]' "${tmp}/out8.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "empty matrix exits 1"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" abc vertex "${RC}" "${MATRIX}" "${tmp}/out9.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "short sha exits 1"

echo "# --kind release: the same checks with the plain version, and a manifest naming both runs"
reset
make_build "20261022T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "0.0.1"
make_build "20261022T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "0.0.1"
rc=0; RC_RUN_ID=4242 RC_RUN_URL=https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/4242 RC_TAG=rc/vertex/0123456 \
  "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" vertex 0.0.1 "${MATRIX}" "${tmp}/release.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "release kind exits 0"
check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["kind"], m["version"], m["pending"], m["run_id"], m["rc_run"]["run_id"], m["rc_run"]["tag"], m["rc_run"]["run_url"], m["release_run"]["run_id"], m["release_run"]["run_url"], len(m["builds"]), list(m)[-1])
' "${tmp}/release.json")" \
  "release 0.0.1 0.0.1 ${RUN} 4242 rc/vertex/0123456 https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/4242 ${RUN} https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/${RUN} 2 builds" \
  "release manifest: kind, version, rc_run, release_run"
rc=0; "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" vertex 0.0.1 "${MATRIX}" "${tmp}/release2.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "release kind without RC_RUN_ID/RC_RUN_URL/RC_TAG exits 1"
contains "${log}" "needs RC_RUN_ID, RC_RUN_URL and RC_TAG" "names the variables"
rc=0; RC_RUN_ID=1 RC_RUN_URL=u RC_TAG=t "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" vertex "${RC}" "${MATRIX}" "${tmp}/release3.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "release kind with an rc version string exits 1"
contains "${log}" "must be plain X.Y.Z" "names the shape"
rc=0; RC_RUN_ID=1 RC_RUN_URL=u RC_TAG=t "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" vertex 0.0.2 "${MATRIX}" "${tmp}/release4.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a build of another version fails the release gate"
contains "${log}" "manifest buildparams.build_version is '0.0.1', not the plan's '0.0.2'" "names the check"
contains "${log}" "nothing is tagged or released" "release wording in the summary"
rc=0; "${scripts}/rc-gate.sh" --kind nope "${RUN}" "${SHA}" vertex 0.0.1 "${MATRIX}" "${tmp}/release5.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "an unknown kind exits 1"

echo "# the update package: recorded with its sizes when the build made one"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
add_ota "20261021T110000Z-${RUN}-standard"
out="${tmp}/ota.json"
run_gate "${out}"
check "${rc}" 0 "a build with an update package passes"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
f = b["vertex_jetson-orin-nx-a603-8gb_release"]["files"]
print(f["swu"]["key"], f["swu"]["bytes"], f["ota_entry"]["key"], f["ota_entry"]["bytes"] > 0,
      sorted(b["vertex_jetson-orin-nx-a603-8gb_release-hardened"]["files"]))
' "${out}")" "ota/ds-ota-${MACHINE}-${RC}.swu 11 ota/entry.json True ['deb_feed', 'image', 'manifest', 'package_list', 'sha256sums']" \
  "swu and ota_entry recorded with sizes; a build without one has neither"

echo "# negative: ota/entry.json disagrees with SHA256SUMS about the .swu"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
add_ota "20261021T110000Z-${RUN}-standard" badsha
run_gate "${tmp}/ota2.json"
check "${rc}" 1 "a mismatched entry exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: ota/entry.json says sha256 'c{64}', SHA256SUMS says b{64}" "names both digests"

echo "# negative: a .swu without ota/entry.json in SHA256SUMS"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
add_ota "20261021T110000Z-${RUN}-standard" noentry
run_gate "${tmp}/ota3.json"
check "${rc}" 1 "an incomplete update package exits 1"
contains "${log}" "SHA256SUMS lists an incomplete update package" "says so"

echo "# a read-only flavor's build has no deb feed: recorded without one"
reset
FEED=none make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
rm -r "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/deb"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
out="${tmp}/nofeed.json"
run_gate "${out}"
check "${rc}" 0 "a build without a deb feed passes"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
print(sorted(b["vertex_jetson-orin-nx-a603-8gb_release"]["files"]), sorted(b["vertex_jetson-orin-nx-a603-8gb_release-hardened"]["files"]))
' "${out}")" "['image', 'manifest', 'sha256sums'] ['deb_feed', 'image', 'manifest', 'package_list', 'sha256sums']" \
  "no deb_feed or package_list for the build without a feed; the other keeps both"
check "$(grep -c '::error::' "${log}")" 0 "no error annotations"

echo "# negative: a deb feed without its package list"
reset
FEED=half make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/halffeed.json"
check "${rc}" 1 "half a deb feed exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: manifest artifacts lists \['deb_feed'\] without its pair" "names the missing pair"

# The exported SDK (ENG-2126) is the promote build's: only --kind release
# records one, and --kind rc refuses one (an RC build never builds it).
run_release_gate() {
  log="${tmp}/log"
  rc=0
  RC_RUN_ID=4242 RC_RUN_URL=https://github.com/DistributedSpectrum/ds-tegra-distro/actions/runs/4242 RC_TAG=rc/vertex/0123456 \
    "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" vertex 0.0.1 "${MATRIX}" "$1" > "${log}" 2>&1 || rc=$?
}

echo "# the exported SDK on the release kind: recorded with its sizes when the promote build made one, absent otherwise"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" 0.0.1
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" 0.0.1
add_sdk "20261021T110000Z-${RUN}-standard"
out="${tmp}/sdk.json"
run_release_gate "${out}"
check "${rc}" 0 "a release build with an SDK passes"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
f = b["vertex_jetson-orin-nx-a603-8gb_release"]["files"]
print(f["sdk"]["key"], f["sdk"]["bytes"], f["sdk_host_manifest"]["key"], f["sdk_host_manifest"]["bytes"], f["sdk_target_manifest"]["bytes"],
      sorted(b["vertex_jetson-orin-nx-a603-8gb_release-hardened"]["files"]))
' "${out}")" "sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-${MACHINE}-toolchain-0.0.1.sh 2000000000 sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-${MACHINE}-toolchain-0.0.1.host.manifest 10 12 ['deb_feed', 'image', 'manifest', 'package_list', 'sha256sums']" \
  "sdk, sdk_host_manifest and sdk_target_manifest recorded; a build without an SDK has none"
contains "${log}" "ok sdk/dsensos-glibc-x86_64-ds-image-sato-armv8a-${MACHINE}-toolchain-0.0.1.sh \(2000000000 bytes\)" "the installer object is checked"

echo "# negative: an SDK installer below the floor"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" 0.0.1
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" 0.0.1
add_sdk "20261021T110000Z-${RUN}-standard" small
run_release_gate "${tmp}/sdk2.json"
check "${rc}" 1 "a 1 MB installer exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: .*toolchain-0.0.1.sh is 1048576 bytes, below the 1073741824 byte floor for an SDK installer" "names the SDK floor"

echo "# negative: an SDK with one manifest entry missing, and an sdk/ object the manifest does not record"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" 0.0.1
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" 0.0.1
add_sdk "20261021T110000Z-${RUN}-standard" nomanifest
run_release_gate "${tmp}/sdk3.json"
check "${rc}" 1 "a partial SDK record exits 1"
contains "${log}" "manifest artifacts.sdk_target_manifest is missing or incomplete" "names the missing entry"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" 0.0.1
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" 0.0.1
add_sdk "20261021T110000Z-${RUN}-standard" extra
run_release_gate "${tmp}/sdk4.json"
check "${rc}" 1 "an unrecorded sdk/ object exits 1"
contains "${log}" "SHA256SUMS lists sdk/.*testdata.json, which manifest.json does not record" "names the object"

echo "# negative: an SDK on the rc kind (an RC build never builds one; the SDK is promote's)"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
add_sdk "20261021T110000Z-${RUN}-standard"
run_gate "${tmp}/sdk5.json"
check "${rc}" 1 "an RC build carrying an SDK exits 1"
contains "${log}" "::error::rc-gate: vertex_jetson-orin-nx-a603-8gb_release: the build carries an SDK .*an RC build never builds one: the SDK is built by the promote run only" "names the rule"
[ ! -f "${tmp}/sdk5.json" ]; check $? 0 "no release manifest is written"
reset
make_build "20261021T110000Z-${RUN}-standard" vertex_jetson-orin-nx-a603-8gb_release "${SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" vertex_jetson-orin-nx-a603-8gb_release-hardened "${SHA}" "${RC}"
add_sdk "20261021T110000Z-${RUN}-standard" extra
run_gate "${tmp}/sdk6.json"
check "${rc}" 1 "an RC build with only an sdk/ line in SHA256SUMS exits 1 too"
contains "${log}" "an RC build never builds one" "names the rule"

echo
if [ "${fails}" = 0 ]; then echo "test_rc_gate: all passed"; else echo "test_rc_gate: ${fails} failure(s)"; exit 1; fi
