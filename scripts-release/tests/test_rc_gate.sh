#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/rc-gate.sh (ENG-1931, ENG-2110). aws is the stub
# in tests/stubs (a fake bucket tree under FAKE_S3); no network, no real S3.
# Each build prefix is written by the real artifacts.py build-manifest over
# tiny debs made with dpkg-deb --build, so the gate reads exactly what the
# build job leaves. The deb rules come from the test's own release config
# (the ds shape: twelve packages, recipe-pv, the cot-xlate probe), so the
# same test runs in every repo that carries the tooling;
# (twelve ds-* packages, arm64, recipe-pv, the ds-utils version probe);
# variants of it go through RELEASE_ROOT.
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
export RELEASE_LINE=main PENDING_VERSION=0.0.1
export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY=DistributedSpectrum/ds
BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
SHA=0123456789abcdef0123456789abcdef01234567
OTHER_SHA=fedcba9876543210fedcba9876543210fedcba98
RUN=424242
RC=0.0.1-rc.12.github-0123456
MACHINE=ds-arm64
MATRIX='{"include":[{"flavor":"ds_ds-arm64_release"},{"flavor":"ds_ds-arm64_release-hardened"}]}'
PV=1.0+git0+abc1234
# The test's own release config of the debs kind (twelve packages, arm64,
# recipe-pv, the cot-xlate probe): the gate reads it through RELEASE_ROOT, so
# the test does not depend on the config of the repo it runs in. The
# install-trees kind has its own config further down.
BASE="${tmp}/base"
mkdir -p "${BASE}/release" "${BASE}/versions"
printf '0.0.1\n' > "${BASE}/versions/ds"
cat > "${BASE}/release/config.json" <<'JSON'
{"products": ["ds"], "product_prefix": false, "bookkeeping_files": [],
 "artifact": {"kind": "debs",
              "debs": ["ds-ais-publisher", "ds-announcer", "ds-controller", "ds-cot-xlate", "ds-gps-publisher", "ds-interpreter",
                       "ds-mqtt-xlate", "ds-planner", "ds-processing", "ds-sdr", "ds-services", "ds-utils"],
              "deb_arch": "arm64", "deb_version_rule": "recipe-pv"},
 "version_probe": {"deb": "ds-cot-xlate", "path": "./opt/ds/bin/cot-xlate", "pattern": "{version}"}}
JSON
cat > "${BASE}/release/matrix.json" <<'JSON'
{"products": {"ds": {"flavors": ["ds_ds-arm64_release", "ds_ds-arm64_release-hardened"]}}}
JSON
export RELEASE_ROOT="${BASE}"
mapfile -t DEBS < <(python3 "${scripts}/version.py" --root "${BASE}" config --key artifact | python3 -I -c 'import json, sys; print("\n".join(json.load(sys.stdin)["debs"]))')
check "${#DEBS[@]}" 12 "the test config lists twelve debs"
PROBE_FILE=./opt/ds/bin/cot-xlate
mkdir -p "${tmp}/extra"
printf 'build notes\n' > "${tmp}/extra/build-notes.txt"

# make_deb <dir> <package> <version> <arch> <probe version|"">: one tiny
# deb; ds-cot-xlate carries the fake binary the probe reads (a few non-text
# bytes around the version string, as the real one has) unless the probe
# version is empty.
make_deb() {
  local dir=$1 pkg=$2 ver=$3 arch=$4 header=$5
  # Its own statement: bash expands every word of one local before assigning any.
  local d="${tmp}/debbuild/${pkg}"
  rm -rf "${d}"
  mkdir -p "${d}/DEBIAN"
  printf 'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: test <test@example.com>\nDescription: tiny test package\n' \
    "${pkg}" "${ver}" "${arch}" > "${d}/DEBIAN/control"
  if [ "${pkg}" = ds-cot-xlate ] && [ -n "${header}" ]; then
    mkdir -p "${d}/opt/ds/bin"
    printf '\177ELF\0\0\0version=%s\0\0\n' "${header}" > "${d}/opt/ds/bin/cot-xlate"
  fi
  dpkg-deb -Zgzip -b "${d}" "${dir}/${pkg}_${ver}_${arch}.deb" > /dev/null
}

# make_build <build_id> <flavor> <sha> <build_version> [--no-manifest]
# One complete per-build prefix as the build job leaves it: the twelve debs
# under deb/, one extra file, manifest.json and SHA256SUMS, all written by
# artifacts.py build-manifest. Knobs (environment, unset = the good build):
#   DEB_VERSION       every deb's dpkg Version (default PV)
#   DEB_VERSION_OF    pkg=version for one package
#   DEB_ARCH_OF       pkg=arch for one package
#   DEB_SKIP          a package to leave out
#   DEB_ADD           a package to add
#   HEADER_VERSION    the version the fake cot-xlate binary carries (default the build version)
#   NO_HEADER=1       ds-cot-xlate without the binary
# --no-manifest stops before artifacts.py (an upload that died early).
make_build() {
  local build_id=$1 flavor=$2 sha=$3 ver=$4 nomanifest=${5:-}
  local d="${BUCKET}/${MACHINE}/${build_id}" product=${flavor%%_*} src="${tmp}/debs/${build_id}-${flavor}"
  local pkg pver parch header=${HEADER_VERSION:-${ver}} debs=()
  [ "${NO_HEADER:-0}" = 0 ] || header=""
  rm -rf "${src}"
  mkdir -p "${src}" "${d}/deb"
  for pkg in "${DEBS[@]}" ${DEB_ADD:-}; do
    [ "${pkg}" != "${DEB_SKIP:-}" ] || continue
    pver=${DEB_VERSION:-${PV}} parch=arm64
    case "${DEB_VERSION_OF:-}" in "${pkg}="*) pver=${DEB_VERSION_OF#*=} ;; esac
    case "${DEB_ARCH_OF:-}" in "${pkg}="*) parch=${DEB_ARCH_OF#*=} ;; esac
    make_deb "${src}" "${pkg}" "${pver}" "${parch}" "${header}"
  done
  mapfile -t debs < <(find "${src}" -name '*.deb' | sort)
  if [ -n "${nomanifest}" ]; then
    cp "${debs[@]}" "${d}/deb/"
    return 0
  fi
  python3 "${scripts}/artifacts.py" build-manifest --out "${d}" \
    --product "${product}" --flavor "${flavor}" --machine "${MACHINE}" \
    --sha "${sha}" --branch main --build-version "${ver}" --release-line main \
    --run-id "${build_id#*-}" --run-url "https://github.com/DistributedSpectrum/ds/actions/runs/${RUN}" \
    --build-datetime "${build_id%%-*}" --layer "layer-a=$(printf '1%.0s' $(seq 40))" \
    --extra "${tmp}/extra/build-notes.txt" "${debs[@]}" > /dev/null
}

# good_pair: the two flavors of the matrix, both complete and correct.
good_pair() {
  make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
  make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
}

# variant_root <name> <python expression over c>: a repo root whose
# the test's base config with one change, for RELEASE_ROOT.
variant_root() {
  local root="${tmp}/root-$1"
  mkdir -p "${root}/release"
  cp "${BASE}/release/matrix.json" "${root}/release/matrix.json"
  python3 -I - "${BASE}/release/config.json" "${root}/release/config.json" "$2" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    c = json.load(f)
exec(sys.argv[3])
with open(sys.argv[2], 'w') as f:
    json.dump(c, f)
EOF
  echo "${root}"
}

reset() { rm -rf "${FAKE_S3}"; mkdir -p "${BUCKET}/${MACHINE}"; }

# run_gate <out> -> exit code in $rc, log in $log
run_gate() {
  log="${tmp}/log"
  rc=0
  "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "$1" > "${log}" 2>&1 || rc=$?
}

echo "# positive: both flavors verified, newest complete prefix wins, other runs ignored"
reset
make_build "20261021T100000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "0.0.1-rc.11.github-0123456"   # older, stale: must not be picked
make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"                          # the newest complete one
make_build "20261021T120000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}" --no-manifest            # newer but died before manifest
make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
make_build "20261021T120000Z-999999-standard" ds_ds-arm64_release "${OTHER_SHA}" "0.0.1-rc.13.github-fedcba9"  # another run, newer
out="${tmp}/release-manifest.json"
run_gate "${out}"
check "${rc}" 0 "positive case exits 0"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
r = b["ds_ds-arm64_release"]
print(m["schema"], m["kind"], m["product"], m["version"], m["pending"], m["sha"], m["line"], m["run_id"], len(m["builds"]),
      r["build_id"], b["ds_ds-arm64_release-hardened"]["s3_prefix"], b["ds_ds-arm64_release-hardened"]["hardened"],
      sorted(r["files"]), len(r["files"]["debs"]), sorted(r["files"]["debs"][0]), sorted(d["package"] for d in r["files"]["debs"]) == sorted(json.load(open(sys.argv[2]))["artifact"]["debs"]),
      r["files"]["extra"], r["files"]["manifest"]["key"], r["files"]["sha256sums"]["key"], m["run_url"])
' "${out}" "${BASE}/release/config.json")" \
  "1 rc ds ${RC} 0.0.1 ${SHA} main ${RUN} 2 20261021T110000Z-${RUN}-standard ${MACHINE}/20261021T100000Z-${RUN}-hardened True ['debs', 'extra', 'manifest', 'sha256sums'] 12 ['bytes', 'key', 'package', 'sha256', 'version'] True [{'key': 'build-notes.txt', 'sha256': '$(sha256sum "${tmp}/extra/build-notes.txt" | cut -d' ' -f1)', 'bytes': 12}] manifest.json SHA256SUMS https://github.com/DistributedSpectrum/ds/actions/runs/${RUN}" \
  "release-manifest.json content"
[ -f "${out}" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
d = next(x for x in m["builds"][0]["files"]["debs"] if x["package"] == "ds-utils")
print(d["key"], d["version"], d["sha256"], d["bytes"])
' "${out}")" \
  "deb/ds-utils_${PV}_arm64.deb ${PV} $(sha256sum "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/deb/ds-utils_${PV}_arm64.deb" | cut -d' ' -f1) $(stat -c %s "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/deb/ds-utils_${PV}_arm64.deb")" \
  "a deb entry carries the object's real key, version, sha256 and size"
contains "${log}" "20261021T120000Z-${RUN}-standard: no manifest.json" "incomplete newer prefix is reported and skipped"
contains "${log}" "ok deb/ds-cot-xlate_${PV//+/\\+}_arm64.deb ${PROBE_FILE} contains '${RC}'" "the version probe is reported"
check "$(grep -c '::error::' "${log}")" 0 "no error annotations on success"
check "$(grep -c '   ok ' "${log}")" 32 "every object of both builds is checked (12 debs + extra + manifest + sums, plus the probe)"

echo "# negative: a missing object"
rm "${BUCKET}/${MACHINE}/20261021T100000Z-${RUN}-hardened/deb/ds-sdr_${PV}_arm64.deb"
run_gate "${tmp}/out2.json"
check "${rc}" 1 "missing deb exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: .*deb/ds-sdr_${PV//+/\\+}_arm64.deb \(ds-sdr\) does not exist" "names the flavor and the object"
check "$([ -f "${tmp}/out2.json" ] && echo written || echo absent)" absent "no manifest written on failure"

echo "# negative: wrong size"
reset
good_pair
printf 'x' >> "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/deb/ds-sdr_${PV}_arm64.deb"
run_gate "${tmp}/out2b.json"
check "${rc}" 1 "a deb of another size exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: .*deb/ds-sdr_${PV//+/\\+}_arm64.deb \(ds-sdr\) is [0-9]+ bytes, the manifest says [0-9]+" "names the sizes"
printf 'x' >> "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/build-notes.txt"
run_gate "${tmp}/out2c.json"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: .*/build-notes.txt \(extra\) is 13 bytes, the manifest says 12" "an extra file is checked the same way"

echo "# negative: wrong sha in the manifest"
reset
make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${OTHER_SHA}" "${RC}"
make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out3.json"
check "${rc}" 1 "wrong sha exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: manifest commit_hashes\[ds\].commit is '${OTHER_SHA}', not ${SHA}" "names the sha check against the repo name"
rc=0; REPO_NAME=other "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" '["ds_ds-arm64_release-hardened"]' "${tmp}/out3b.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a manifest without the repo's commit_hashes entry exits 1"
contains "${log}" "manifest commit_hashes\[other\].commit is None, not ${SHA}" "REPO_NAME overrides the name"

echo "# negative: wrong build_version"
reset
make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "0.0.1-rc.11.github-0123456"
make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out4.json"
check "${rc}" 1 "wrong build_version exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: manifest buildparams.build_version is '0.0.1-rc.11.github-0123456', not the plan's '${RC}'" "names the build_version check"

echo "# negative: the package set (missing, extra)"
reset
DEB_SKIP=ds-sdr make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
DEB_ADD=ds-extra make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out5.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: the build is missing packages release/config.json lists: ds-sdr" "names the missing package"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: the build produced packages release/config.json does not list: ds-extra" "names the extra package"
check "$(grep -c '::error::rc-gate: ds_' "${log}")" 2 "every flavor is checked before exiting"

echo "# negative: wrong architecture, wrong version under recipe-pv"
reset
DEB_ARCH_OF=ds-planner=amd64 make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
DEB_VERSION_OF=ds-sdr=2.0 make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out6.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: deb/ds-planner_${PV//+/\\+}_amd64.deb \(ds-planner\) is for architecture 'amd64', not 'arm64'" "names the architecture"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: deb/ds-sdr_2.0_arm64.deb \(ds-sdr\) has version '2.0', which does not match \^1\\\\.0\\\\\+git \(deb_version_rule recipe-pv\)" "names the version rule"

echo "# deb_version_rule deb-version: the dpkg Version must be version.py deb-version of the build version"
reset
root=$(variant_root deb-version 'c["artifact"]["deb_version_rule"] = "deb-version"')
DEB_VERSION=0.0.1~rc.12.github.0123456 good_pair
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out7.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "debs at the mapped version pass"
check "$(python3 -c 'import json, sys; m = json.load(open(sys.argv[1])); print(sorted({d["version"] for b in m["builds"] for d in b["files"]["debs"]}))' "${tmp}/out7.json")" \
  "['0.0.1~rc.12.github.0123456']" "the recorded deb version is the mapped one"
reset
DEB_VERSION=0.0.1~rc.12.github.0123456-r0 good_pair
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out7b.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "the -rN package revision a Yocto build appends is accepted too"
reset
DEB_VERSION=0.0.1~rc.12.github.0123456-rc1 good_pair
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out7c.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "any other suffix is refused"
reset
good_pair
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out8.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "recipe-pv debs fail under deb-version"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: deb/ds-sdr_${PV//+/\\+}_arm64.deb \(ds-sdr\) has version '${PV//+/\\+}', not '0.0.1~rc.12.github.0123456' \(deb_version_rule deb-version of ${RC}\)" "names both versions"
check "$(grep -c "has version '${PV}'" "${log}")" 24 "every deb of both builds is listed"

echo "# negative: the version probe"
reset
HEADER_VERSION=0.0.1-rc.11.github-0123456 make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
NO_HEADER=1 make_build "20261021T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_gate "${tmp}/out9.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: deb/ds-cot-xlate_${PV//+/\\+}_arm64.deb \(ds-cot-xlate\) ${PROBE_FILE} does not contain '${RC}' \(found: nothing\)" "names the pattern (a bare {version} pattern has no lead-in to quote)"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: deb/ds-cot-xlate_${PV//+/\\+}_arm64.deb \(ds-cot-xlate\) has no ${PROBE_FILE} \(version_probe\)" "names the missing file"
root=$(variant_root no-probe 'c["version_probe"] = None')
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out10.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "without a configured probe the same builds pass"
check "$(grep -c 'contains' "${log}")" 0 "and no probe runs"

echo "# negative: tampered sha256 (a deb line, then the manifest line)"
reset
good_pair
d="${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard"
sed -i "s|^[0-9a-f]\{64\}  deb/ds-sdr_|$(printf 'f%.0s' $(seq 64))  deb/ds-sdr_|" "${d}/SHA256SUMS"
run_gate "${tmp}/out11.json"
check "${rc}" 1 "a SHA256SUMS line that disagrees with the manifest exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: SHA256SUMS lists 'f{64}' for deb/ds-sdr_${PV//+/\\+}_arm64.deb, manifest artifacts.debs\[[0-9]+\].sha256 is [0-9a-f]{64}" "names the key and both digests"
reset
good_pair
python3 -I -c '
import json, sys
p = sys.argv[1]
m = json.load(open(p))
m["artifacts"]["debs"][0]["bytes"] += 1
json.dump(m, open(p, "w"), indent=2)' "${d}/manifest.json"
run_gate "${tmp}/out12.json"
check "${rc}" 1 "a manifest that SHA256SUMS does not vouch for exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: SHA256SUMS lists '[0-9a-f]{64}' for manifest.json, the downloaded one is [0-9a-f]{64}" "names the manifest line"

echo "# negative: no prefix for the run, and SHA256SUMS missing"
reset
make_build "20261021T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
rm "${BUCKET}/${MACHINE}/20261021T110000Z-${RUN}-standard/SHA256SUMS"
run_gate "${tmp}/out13.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: no build prefix s3://ds-yocto-artifacts/${MACHINE}/\*-${RUN}-hardened/" "names the missing prefix"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: ${MACHINE}/20261021T110000Z-${RUN}-standard/SHA256SUMS is missing" "names the missing SHA256SUMS"
check "$(grep -c '::error::rc-gate: ds_' "${log}")" 2 "every flavor is checked before exiting"

echo "# negative: a flavor of another product, and a bad matrix"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" '["portal_ds-arm64_release"]' "${tmp}/out14.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "foreign product flavor exits 1"
contains "${log}" "::error::rc-gate: portal_ds-arm64_release: not a ds flavor" "names the product check"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" '[]' "${tmp}/out15.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "empty matrix exits 1"
rc=0; "${scripts}/rc-gate.sh" "${RUN}" abc ds "${RC}" "${MATRIX}" "${tmp}/out16.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "short sha exits 1"
rc=0; env -u GITHUB_REPOSITORY "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/out17.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "no repo name exits 1"
contains "${log}" "the repo name is unknown" "names the fix"

echo "# --kind release: the same checks with the plain version, and a manifest naming both runs"
reset
make_build "20261022T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "0.0.1"
make_build "20261022T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "0.0.1"
rc=0; RC_RUN_ID=4242 RC_RUN_URL=https://github.com/DistributedSpectrum/ds/actions/runs/4242 RC_TAG=rc/0123456 \
  "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" ds 0.0.1 "${MATRIX}" "${tmp}/release.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "release kind exits 0"
check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["kind"], m["version"], m["pending"], m["run_id"], m["rc_run"]["run_id"], m["rc_run"]["tag"], m["rc_run"]["run_url"], m["release_run"]["run_id"], m["release_run"]["run_url"], len(m["builds"]), list(m)[-1])
' "${tmp}/release.json")" \
  "release 0.0.1 0.0.1 ${RUN} 4242 rc/0123456 https://github.com/DistributedSpectrum/ds/actions/runs/4242 ${RUN} https://github.com/DistributedSpectrum/ds/actions/runs/${RUN} 2 builds" \
  "release manifest: kind, version, rc_run, release_run"
contains "${log}" "contains '0.0.1'" "the probe looks for the plain version"
rc=0; "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" ds 0.0.1 "${MATRIX}" "${tmp}/release2.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "release kind without RC_RUN_ID/RC_RUN_URL/RC_TAG exits 1"
contains "${log}" "needs RC_RUN_ID, RC_RUN_URL and RC_TAG" "names the variables"
rc=0; RC_RUN_ID=1 RC_RUN_URL=u RC_TAG=t "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/release3.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "release kind with an rc version string exits 1"
contains "${log}" "must be plain X.Y.Z" "names the shape"
rc=0; RC_RUN_ID=1 RC_RUN_URL=u RC_TAG=t "${scripts}/rc-gate.sh" --kind release "${RUN}" "${SHA}" ds 0.0.2 "${MATRIX}" "${tmp}/release4.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "a build of another version fails the release gate"
contains "${log}" "manifest buildparams.build_version is '0.0.1', not the plan's '0.0.2'" "names the check"
contains "${log}" "nothing is tagged or released" "release wording in the summary"
rc=0; "${scripts}/rc-gate.sh" --kind nope "${RUN}" "${SHA}" ds 0.0.1 "${MATRIX}" "${tmp}/release5.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "an unknown kind exits 1"

echo "# install-trees: one tarball per build, a directory per service plus the info file; the probe reads from it"
# The install-trees config: the same twelve names as services, the info file
# sdk-build-info.txt, the probe path inside the tarball.
TREES_ROOT=$(variant_root install-trees 'c["artifact"] = {"kind": "install-trees", "services": c["artifact"]["debs"], "info_file": "sdk-build-info.txt"}; c["version_probe"] = {"path": "./ds-cot-xlate/opt/ds/bin/cot-xlate", "pattern": "{version}"}')
SDK_URI=s3://ds-yocto-artifacts/release/vertex/v2.0.0/jetson-orin-nx-a603-8gb/standard/toolchain-2.0.0.sh
SDK_SHA=$(printf 'a%.0s' $(seq 64))
# make_tree_build <build_id> <flavor> <sha> <build_version>: a per-build prefix
# the way the SDK build leaves it: the tarball (./<service>/... for every
# configured service plus ./sdk-build-info.txt, packed with tar like the
# driver does), the info file beside it, manifest.json and SHA256SUMS from
# artifacts.py build-manifest --kind install-trees with the SDK recorded.
# Knobs (environment, unset = the good build):
#   TREE_SKIP          a service directory left out of the tarball
#   TREE_ADD           an extra top-level directory
#   TREE_INFO_VERSION  ds_version= in the info file (default the build version)
#   TREE_INFO_COMMIT   ds_commit= in the info file (default the sha)
#   TREE_HEADER_VERSION the version the fake cot-xlate binary carries
#   TREE_NO_INFO=1     no sdk-build-info.txt inside the tarball
#   TREE_INFO_NOT_EXTRA=1  the info file not passed as --extra
#   TREE_SDK_SHA       buildparams.sdk.sha256 (default SDK_SHA); TREE_NO_SDK=1: no --sdk-* options
make_tree_build() {
  local build_id=$1 flavor=$2 sha=$3 ver=$4
  local d="${BUCKET}/${MACHINE}/${build_id}" product=${flavor%%_*} work="${tmp}/trees/${build_id}-${flavor}"
  local svc root="${work}/root" tarball="${work}/ds-sdk-build-jetson-orin-nx-a603-8gb-${sha:0:7}.tar.gz" args=()
  rm -rf "${work}"
  mkdir -p "${root}" "${d}"
  for svc in "${DEBS[@]}" ${TREE_ADD:-}; do
    [ "${svc}" != "${TREE_SKIP:-}" ] || continue
    mkdir -p "${root}/${svc}/opt/ds/bin"
    if [ "${svc}" = ds-cot-xlate ]; then
      printf '\177ELF\0\0\0version=%s\0\0\n' "${TREE_HEADER_VERSION:-${ver}}" > "${root}/${svc}/opt/ds/bin/cot-xlate"
    else
      printf 'binary %s\n' "${svc}" > "${root}/${svc}/opt/ds/bin/${svc#ds-}"
    fi
  done
  # the info file beside the tarball is the one inside it
  printf 'ds_commit=%s\nds_version=%s\nds_release_line=main\nmachine=jetson-orin-nx-a603-8gb\n' \
    "${TREE_INFO_COMMIT:-${sha}}" "${TREE_INFO_VERSION:-${ver}}" > "${work}/sdk-build-info.txt"
  [ "${TREE_NO_INFO:-0}" = 1 ] || cp "${work}/sdk-build-info.txt" "${root}/sdk-build-info.txt"
  tar -czf "${tarball}" -C "${root}" .
  [ "${TREE_INFO_NOT_EXTRA:-0}" = 1 ] || args+=(--extra "${work}/sdk-build-info.txt")
  if [ "${TREE_NO_SDK:-0}" = 0 ]; then
    args+=(--sdk-uri "${SDK_URI}" --sdk-sha256 "${TREE_SDK_SHA:-${SDK_SHA}}" --sdk-machine jetson-orin-nx-a603-8gb --sdk-os-release v2.0.0)
  fi
  python3 "${scripts}/artifacts.py" build-manifest --kind install-trees --out "${d}" \
    --product "${product}" --flavor "${flavor}" --machine "${MACHINE}" \
    --sha "${sha}" --branch main --build-version "${ver}" --release-line main \
    --run-id "${build_id#*-}" --run-url "https://github.com/DistributedSpectrum/ds/actions/runs/${RUN}" \
    --build-datetime "${build_id%%-*}" --layer "dsensos-sdk=${TREE_SDK_SHA:-${SDK_SHA}}" \
    "${args[@]}" "${tarball}" > /dev/null
}
mkdir -p "${tmp}/trees"
TARBALL_STD="ds-sdk-build-jetson-orin-nx-a603-8gb-0123456.tar.gz"
# rewrite_manifest <prefix dir> <python over m>: edit a build's manifest.json
# and re-sum its SHA256SUMS line (a manifest that lies, but consistently).
rewrite_manifest() {
  python3 -I - "$1/manifest.json" "$1/SHA256SUMS" "$2" <<'EOF'
import hashlib, json, sys
mpath, spath, code = sys.argv[1:4]
m = json.load(open(mpath))
exec(code)
with open(mpath, 'w') as f:
    json.dump(m, f, indent=2)
    f.write('\n')
digest = hashlib.sha256(open(mpath, 'rb').read()).hexdigest()
lines = [l for l in open(spath).read().splitlines() if not l.endswith('  manifest.json')]
lines.append(f'{digest}  manifest.json')
open(spath, 'w').write('\n'.join(lines) + '\n')
EOF
}
good_trees() {
  make_tree_build "20261030T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
  make_tree_build "20261030T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
}
# run_trees <out> [more rc-gate options]
run_trees() {
  local out=$1; shift
  log="${tmp}/log"
  rc=0
  RELEASE_ROOT="${TREES_ROOT}" "${scripts}/rc-gate.sh" "$@" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${out}" > "${log}" 2>&1 || rc=$?
}

reset
good_trees
check "$(tar -tzf "${BUCKET}/${MACHINE}/20261030T110000Z-${RUN}-standard/"*.tar.gz | sed 's#^\./##' | awk -F/ '$1 != "" { print $1 }' | sort -u | paste -sd ' ')" \
  "$(printf '%s\n' "${DEBS[@]}" sdk-build-info.txt | sort | paste -sd ' ')" "the fixture tarball's top level is the twelve services plus the info file"
run_trees "${tmp}/trees.json"
check "${rc}" 0 "a good install tree passes"
contains "${log}" "^rc-gate: artifact kind install-trees$" "the kind is announced"
[ -f "${tmp}/trees.json" ] && check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
b = {x["flavor"]: x for x in m["builds"]}
r = b["ds_ds-arm64_release"]
print(m["kind"], len(m["builds"]), sorted(r), r["files"]["debs"], [e["key"] for e in r["files"]["extra"]],
      r["sdk"], b["ds_ds-arm64_release-hardened"]["sdk"]["sha256"] == r["sdk"]["sha256"])
' "${tmp}/trees.json")" \
  "rc 2 ['build_id', 'files', 'flavor', 'hardened', 'machine', 's3_prefix', 'sdk'] [] ['ds-sdk-build-jetson-orin-nx-a603-8gb-0123456.tar.gz', 'sdk-build-info.txt'] {'uri': '${SDK_URI}', 'sha256': '${SDK_SHA}', 'machine': 'jetson-orin-nx-a603-8gb', 'os_release': 'v2.0.0'} True" \
  "release-manifest.json: files.debs [], the tarball and the info file in extra, the build's sdk copied in"
contains "${log}" "ok ${TARBALL_STD}: sha256, top level \(13 entries\), sdk-build-info.txt ds_version and ds_commit" "the tarball checks are reported"
contains "${log}" "ok ${TARBALL_STD} ./ds-cot-xlate/opt/ds/bin/cot-xlate contains '${RC}'" "the probe reads the binary out of the tarball"
check "$(grep -c '::error::' "${log}")" 0 "no error annotations"
check "$(grep -c '   ok ' "${log}")" 12 "every object of both builds is checked (tarball + info + manifest + sums, the tarball content, the probe)"

echo "# install-trees negatives: a missing service, an extra directory, a tarball that does not match its manifest"
reset
TREE_SKIP=ds-sdr make_tree_build "20261030T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
TREE_ADD=ds-extra make_tree_build "20261030T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_trees "${tmp}/trees2.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: the install tree is missing services release/config.json lists: ds-sdr" "names the missing service"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: the install tree has top-level directories release/config.json does not list: ds-extra" "names the extra directory"
check "$(grep -c '::error::rc-gate: ds_' "${log}")" 2 "every flavor is checked before exiting"
check "$([ -f "${tmp}/trees2.json" ] && echo written || echo absent)" absent "no manifest written on failure"
reset
TREE_SKIP=ds-sdr make_tree_build "20261030T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
make_tree_build "20261030T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
rewrite_manifest "${BUCKET}/${MACHINE}/20261030T110000Z-${RUN}-standard" 'm["artifacts"]["install_tree"]["services"] = sorted(m["artifacts"]["install_tree"]["services"] + ["ds-sdr"])'
run_trees "${tmp}/trees2b.json"
check "${rc}" 1 "a manifest whose services list does not match the tarball exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: ${TARBALL_STD} top level is \[ds-ais-publisher .* ds-services ds-utils sdk-build-info.txt\], release/config.json wants the services plus sdk-build-info.txt: \[ds-ais-publisher .* ds-sdr .*\]" "the downloaded tarball's top level is what counts"
reset
good_trees
printf 'x' >> "${BUCKET}/${MACHINE}/20261030T110000Z-${RUN}-standard/${TARBALL_STD}"
run_trees "${tmp}/trees2c.json"
check "${rc}" 1 "a tarball of another size exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: .*/${TARBALL_STD} \(extra\) is [0-9]+ bytes, the manifest says [0-9]+" "caught by the object size check"
reset
good_trees
d="${BUCKET}/${MACHINE}/20261030T110000Z-${RUN}-standard"
python3 -I -c '
import sys
p = sys.argv[1]
data = bytearray(open(p, "rb").read())
data[-1] ^= 0xff   # the gzip CRC: same size, other content
open(p, "wb").write(data)' "${d}/${TARBALL_STD}"
run_trees "${tmp}/trees2d.json"
check "${rc}" 1 "a tarball of the right size and other content exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: ${TARBALL_STD} hashes to [0-9a-f]{64}, the manifest says [0-9a-f]{64}" "the download is hashed against the manifest"

echo "# install-trees negatives: the info file (wrong ds_version, wrong ds_commit, missing inside, missing beside)"
reset
TREE_INFO_VERSION=0.0.1-rc.11.github-0123456 make_tree_build "20261030T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
TREE_INFO_COMMIT="${OTHER_SHA}" make_tree_build "20261030T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_trees "${tmp}/trees3.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: sdk-build-info.txt inside ${TARBALL_STD} says ds_version=0.0.1-rc.11.github-0123456, not the plan's ${RC}" "names the version the info file carries"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: sdk-build-info.txt inside ${TARBALL_STD} says ds_commit=${OTHER_SHA}, not ${SHA}" "names the commit the info file carries"
reset
TREE_NO_INFO=1 make_tree_build "20261030T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" "${RC}"
TREE_INFO_NOT_EXTRA=1 make_tree_build "20261030T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" "${RC}"
run_trees "${tmp}/trees3b.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: ${TARBALL_STD} top level is \[.*\], release/config.json wants the services plus sdk-build-info.txt" "an info file missing inside the tarball fails the top-level check"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: manifest artifacts.extra does not list the info file sdk-build-info.txt" "the info file must be beside the tarball too"

echo "# install-trees negatives: the probe, and a debs build under the install-trees config"
reset
TREE_HEADER_VERSION=0.0.1-rc.11.github-0123456 good_trees
run_trees "${tmp}/trees4.json"
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: ${TARBALL_STD} ./ds-cot-xlate/opt/ds/bin/cot-xlate does not contain '${RC}' \(found: nothing\)" "names the probe path inside the tarball"
check "$(grep -c "does not contain '${RC}'" "${log}")" 2 "both builds are probed"
root=$(variant_root trees-no-probe 'c["artifact"] = {"kind": "install-trees", "services": c["artifact"]["debs"], "info_file": "sdk-build-info.txt"}; c["version_probe"] = None')
rc=0; RELEASE_ROOT="${root}" "${scripts}/rc-gate.sh" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/trees4b.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 0 "without a configured probe the same builds pass (the tarball checks still run)"
check "$(grep -c 'contains' "${log}")" 0 "and no probe runs"
check "$(grep -c 'ds_version and ds_commit' "${log}")" 2 "the info file is still checked"
reset
good_pair
run_trees "${tmp}/trees5.json"
check "${rc}" 1 "a debs build fails the install-trees gate"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: manifest artifacts.debs lists 12 deb\(s\); an install-trees build records none" "names the debs"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release: manifest artifacts.install_tree \{key, services\} is missing" "names the missing install_tree"

echo "# --kind release with --rc-manifest: the release build's SDK must be the RC build's"
reset
make_tree_build "20261031T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" 0.0.1
make_tree_build "20261031T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" 0.0.1
release_trees() {
  log="${tmp}/log"
  rc=0
  RELEASE_ROOT="${TREES_ROOT}" RC_RUN_ID=4242 RC_RUN_URL=https://github.com/DistributedSpectrum/ds/actions/runs/4242 RC_TAG=rc/0123456 \
    "${scripts}/rc-gate.sh" --kind release "$@" "${RUN}" "${SHA}" ds 0.0.1 "${MATRIX}" "${tmp}/release-trees.json" > "${log}" 2>&1 || rc=$?
}
release_trees --rc-manifest "${tmp}/trees.json"
check "${rc}" 0 "the same SDK as the RC passes"
check "$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
print(m["kind"], m["version"], m["rc_run"]["tag"], [b["sdk"]["sha256"] == sys.argv[2] for b in m["builds"]])' "${tmp}/release-trees.json" "${SDK_SHA}")" \
  "release 0.0.1 rc/0123456 [True, True]" "the release manifest carries the SDK of each build"
release_trees
check "${rc}" 0 "without --rc-manifest the SDK is recorded but not compared"
reset
make_tree_build "20261031T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" 0.0.1
TREE_SDK_SHA=$(printf 'c%.0s' $(seq 64)) make_tree_build "20261031T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" 0.0.1
release_trees --rc-manifest "${tmp}/trees.json"
check "${rc}" 1 "another SDK on one flavor exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: buildparams.sdk \{\"uri\": \"${SDK_URI}\", \"sha256\": \"c{64}\".* is not the RC build's SDK \{\"uri\": \"${SDK_URI}\", \"sha256\": \"a{64}\"" "names both SDKs"
check "$(grep -c "is not the RC build's SDK" "${log}")" 1 "the flavor with the RC's SDK passes"
contains "${log}" "nothing is tagged or released" "release wording"
reset
make_tree_build "20261031T110000Z-${RUN}-standard" ds_ds-arm64_release "${SHA}" 0.0.1
make_tree_build "20261031T100000Z-${RUN}-hardened" ds_ds-arm64_release-hardened "${SHA}" 0.0.1
python3 -I -c '
import json, sys
m = json.load(open(sys.argv[1]))
for b in m["builds"]:
    b.pop("sdk", None)
json.dump(m, open(sys.argv[2], "w"))' "${tmp}/trees.json" "${tmp}/trees-nosdk.json"
release_trees --rc-manifest "${tmp}/trees-nosdk.json"
check "${rc}" 1 "an RC manifest without an SDK record fails the comparison"
contains "${log}" "is not the RC build's SDK null" "says the RC recorded none"
python3 -I -c '
import json, sys
m = json.load(open(sys.argv[1]))
m["builds"] = [b for b in m["builds"] if not b["hardened"]]
json.dump(m, open(sys.argv[2], "w"))' "${tmp}/trees.json" "${tmp}/trees-onebuild.json"
release_trees --rc-manifest "${tmp}/trees-onebuild.json"
check "${rc}" 1 "an RC manifest without the flavor exits 1"
contains "${log}" "::error::rc-gate: ds_ds-arm64_release-hardened: the RC's release-manifest.json has no build for flavor ds_ds-arm64_release-hardened" "names the flavor"
rc=0; RELEASE_ROOT="${TREES_ROOT}" "${scripts}/rc-gate.sh" --rc-manifest "${tmp}/trees.json" "${RUN}" "${SHA}" ds "${RC}" "${MATRIX}" "${tmp}/x.json" > "${log}" 2>&1 || rc=$?
check "${rc}" 1 "--rc-manifest with --kind rc exits 1"
contains "${log}" "goes with --kind release" "names the rule"
release_trees --rc-manifest "${tmp}/release-trees.json"
check "${rc}" 1 "a release manifest as --rc-manifest exits 1"
contains "${log}" "is not an rc manifest" "names the kind"
release_trees --rc-manifest "${tmp}/nonexistent.json"
check "${rc}" 1 "a missing --rc-manifest exits 1"

echo "# artifacts.py names the first commit_hashes entry after the repo, the name the gate looks up"
mkdir -p "${tmp}/name"
make_deb "${tmp}/name" ds-utils 1.0+git1 arm64 0.0.1
first_name() {
  rm -rf "${tmp}/name/out"
  # shellcheck disable=SC2086  # PRODUCT_REPO_ARGS is two words on purpose
  "$@" python3 "${scripts}/artifacts.py" build-manifest --out "${tmp}/name/out" --product portal --flavor portal_x_release \
    --machine x --sha "${SHA}" ${PRODUCT_REPO_ARGS:-} "${tmp}/name/ds-utils_1.0+git1_arm64.deb" \
    | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["commit_hashes"][0]["name"])'
}
check "$(first_name env GITHUB_REPOSITORY=org/sensor-portal)" sensor-portal "the basename of GITHUB_REPOSITORY, not the product"
check "$(first_name env -u GITHUB_REPOSITORY)" portal "the product when GITHUB_REPOSITORY is unset"
check "$(PRODUCT_REPO_ARGS="--product-repo other" first_name env GITHUB_REPOSITORY=org/sensor-portal)" other "--product-repo overrides both"

echo
if [ "${fails}" = 0 ]; then echo "test_rc_gate: all passed"; else echo "test_rc_gate: ${fails} failure(s)"; exit 1; fi
