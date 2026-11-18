#!/usr/bin/env bash
set -euo pipefail

# The Action A gate (ENG-1931): verify every build of one rc-build run from
# what yocto-build.yml left in S3, and write release-manifest.json. With
# --kind release it is the release gate of promote (ENG-1934): the same
# checks on the release build, and a manifest that records both runs.
#
#   rc-gate.sh [--kind rc|release] <run_id> <sha> <product> <version> <matrix-json> <out>
#
#   run_id       GITHUB_RUN_ID of the run that built (the build job's matrix
#                entries share it, and the per-build S3 prefix carries it)
#   sha          the 40-hex commit the run built
#   product      the line's product (release/config.json)
#   version      the version the run injected: X.Y.Z-rc.<N>.github-<sha7>
#                for an RC, X.Y.Z for a release build
#   matrix-json  the build matrix: {"include": [{"flavor": ...}, ...]} or a
#                JSON list of flavor names
#   out          where to write release-manifest.json
#
# Environment: RELEASE_LINE (the pushed ref name) and PENDING_VERSION
# (versions/<product> at <sha>) are recorded in the manifest; GITHUB_SERVER_URL
# and GITHUB_REPOSITORY make run_url; DS_ARTIFACTS_BUCKET (lib.sh) defaults to
# ds-yocto-artifacts; RC_GATE_IMAGE_MIN_BYTES overrides the tarball floor and
# RC_GATE_SDK_MIN_BYTES (1 GB) the SDK installer floor.
# --kind release also needs RC_RUN_ID, RC_RUN_URL and RC_TAG (the RC this
# release build repeats, from the RC's own release-manifest.json), recorded
# as rc_run next to release_run.
#
# A matrix call of a reusable workflow keeps only one entry's outputs, so
# nothing per flavor is taken from the build job: each build is rediscovered
# in S3 by run id. For every flavor of the matrix:
#   1. machine and hardening from the flavor name (parse_flavor, the same
#      split as yocto-build's metadata step);
#   2. list s3://<bucket>/<machine>/ and keep the prefixes ending in
#      -<run_id>-<standard|hardened>/; among them the newest whose
#      manifest.json says buildparams.flavor == this flavor wins (a rerun of
#      one failed matrix job keeps the run id and gets a new timestamp);
#   3. manifest checks: buildparams.product == product,
#      buildparams.build_version == rc_version,
#      commit_hashes[name == ds-tegra-distro].commit == sha, and
#      artifacts.image present with the sha256 that SHA256SUMS lists for the
#      same key, and artifacts.{deb_feed,package_list} checked the same way
#      when present: both or neither, since yocto-build makes the deb feed
#      only on -rw flavors (SHA256SUMS's manifest.json entry must match the
#      downloaded manifest too);
#   4. every object (image, manifest.json, SHA256SUMS, and the deb feed and
#      package list when listed) exists (s3api head-object) with the
#      recorded size, and the image tarball is at least
#      RC_GATE_IMAGE_MIN_BYTES (500 MB);
#   5. the exported SDK (ENG-2126), --kind release only, and only when the
#      build made one (the yocto-build call had build_sdk, which promote sets
#      for the product's "sdk" flavors): manifest artifacts.sdk,
#      sdk_host_manifest and sdk_target_manifest, all three or none, each
#      matching SHA256SUMS, their objects present with the recorded sizes and
#      the installer at least RC_GATE_SDK_MIN_BYTES (1 GB). Recorded as
#      files.sdk, files.sdk_host_manifest, files.sdk_target_manifest, so the
#      release manifest carries them and ship copies them. No SDK is never
#      an error. With --kind rc an SDK IS an error: an RC build never builds
#      one (the SDK is built at promote only).
# Every miss is one ::error:: naming the flavor and the check; the script
# keeps going so one run shows every problem, then exits 1.
#
# release-manifest.json (schema 1):
#   {schema: 1, kind: "rc"|"release", product, version, pending, sha, line,
#    run_id, run_url, builds: [{flavor, machine, hardened, build_id,
#    s3_prefix, files: {image, manifest, sha256sums, deb_feed + package_list
#    on -rw flavors, and swu + ota_entry when the build made an update
#    package, and (kind release only) sdk + sdk_host_manifest +
#    sdk_target_manifest when it made an SDK: {key, sha256, bytes}}]}
#   kind release adds rc_run: {run_id, run_url, tag} and
#   release_run: {run_id, run_url} (run_id/run_url stay the run that built).
# build_id is the prefix basename (<UTC>-<run_id>-<attr>), s3_prefix is
# <machine>/<build_id>, keys are relative to s3_prefix.

. "$(dirname "$(readlink -f "$0")")/lib.sh"

KIND=rc
while [ $# -gt 0 ]; do
  case "$1" in
    --kind) KIND=$2; shift 2 ;;
    --) shift; break ;;
    -*) die "rc-gate: unknown option $1" ;;
    *) break ;;
  esac
done
[ $# -eq 6 ] || die "usage: rc-gate.sh [--kind rc|release] <run_id> <sha> <product> <version> <matrix-json> <out>"
RUN_ID=$1 SHA=$2 PRODUCT=$3 RC_VERSION=$4 MATRIX_JSON=$5 OUT=$6
IMAGE_MIN_BYTES=${RC_GATE_IMAGE_MIN_BYTES:-524288000}
SDK_MIN_BYTES=${RC_GATE_SDK_MIN_BYTES:-1073741824}
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${RUN_ID}"

case "${KIND}" in rc|release) ;; *) die "rc-gate: --kind must be rc or release, got '${KIND}'" ;; esac
[[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "rc-gate: sha '${SHA}' is not a full 40-hex commit"
[[ "${RUN_ID}" =~ ^[0-9]+$ ]] || die "rc-gate: run_id '${RUN_ID}' is not a number"
[ -n "${RELEASE_LINE:-}" ] || die "rc-gate: RELEASE_LINE is not set"
[ -n "${PENDING_VERSION:-}" ] || die "rc-gate: PENDING_VERSION is not set"
if [ "${KIND}" = release ]; then
  [[ "${RC_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "rc-gate: a release build's version must be plain X.Y.Z, got '${RC_VERSION}'"
  [ -n "${RC_RUN_ID:-}" ] && [ -n "${RC_RUN_URL:-}" ] && [ -n "${RC_TAG:-}" ] \
    || die "rc-gate: --kind release needs RC_RUN_ID, RC_RUN_URL and RC_TAG (the RC this release build repeats)"
fi

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

mapfile -t FLAVORS < <(python3 -I - "${MATRIX_JSON}" <<'EOF'
import json, sys
d = json.loads(sys.argv[1])
if isinstance(d, dict):
    d = d.get('include', [])
for e in d:
    print(e['flavor'] if isinstance(e, dict) else e)
EOF
)
[ ${#FLAVORS[@]} -gt 0 ] || die "rc-gate: the matrix names no flavor"

failures=0
fail() {
  # fail <flavor> <check>
  echo "::error::rc-gate: $1: $2"
  failures=$((failures + 1))
}

ENTRIES=()
for flavor in "${FLAVORS[@]}"; do
  echo "== ${flavor}"
  parse_flavor "${flavor}"
  if [ "${FLAVOR_PRODUCT}" != "${PRODUCT}" ]; then
    fail "${flavor}" "not a ${PRODUCT} flavor"
    continue
  fi

  mapfile -t candidates < <(s3_run_prefixes "${FLAVOR_MACHINE}" "${RUN_ID}" "${FLAVOR_ATTR}")
  if [ ${#candidates[@]} -eq 0 ]; then
    fail "${flavor}" "no build prefix s3://${DS_ARTIFACTS_BUCKET}/${FLAVOR_MACHINE}/*-${RUN_ID}-${FLAVOR_ATTR}/ (did its build job upload?)"
    continue
  fi

  build_id=""
  manifest="${WORK}/${flavor}.manifest.json"
  for c in "${candidates[@]}"; do
    if ! s3_get "${FLAVOR_MACHINE}/${c}/manifest.json" "${manifest}" 2>/dev/null; then
      echo "   ${c}: no manifest.json (incomplete build), skipped"
      continue
    fi
    got=$(json_field "${manifest}" buildparams.flavor 2>/dev/null || echo '?')
    if [ "${got}" = "${flavor}" ]; then
      build_id=${c}
      break
    fi
    echo "   ${c}: built flavor '${got}', skipped"
  done
  if [ -z "${build_id}" ]; then
    fail "${flavor}" "none of the run's prefixes under ${FLAVOR_MACHINE}/ (${candidates[*]}) has a manifest.json for this flavor"
    continue
  fi
  prefix="${FLAVOR_MACHINE}/${build_id}"
  echo "   build ${prefix}"

  sums="${WORK}/${flavor}.SHA256SUMS"
  if ! s3_get "${prefix}/SHA256SUMS" "${sums}" 2>/dev/null; then
    fail "${flavor}" "${prefix}/SHA256SUMS is missing (the build did not finish its upload)"
    continue
  fi

  # Manifest content and the artifact list; writes the manifest entry.
  entry="${WORK}/${flavor}.entry.json"
  if ! python3 -I - "${manifest}" "${sums}" "${flavor}" "${PRODUCT}" "${SHA}" "${RC_VERSION}" \
       "${FLAVOR_MACHINE}" "${FLAVOR_HARDENED}" "${build_id}" "${prefix}" "${entry}" "${KIND}" <<'EOF'
import hashlib, json, os, re, sys
(mpath, spath, flavor, product, sha, rc_version,
 machine, hardened, build_id, prefix, out, kind) = sys.argv[1:13]
errors = []
with open(mpath) as f:
    m = json.load(f)
bp = m.get('buildparams') or {}
if bp.get('product') != product:
    errors.append(f"manifest buildparams.product is {bp.get('product')!r}, not {product!r}")
if bp.get('build_version') != rc_version:
    errors.append(f"manifest buildparams.build_version is {bp.get('build_version')!r}, not the plan's {rc_version!r}")
commit = next((c.get('commit') for c in m.get('commit_hashes') or [] if c.get('name') == 'ds-tegra-distro'), None)
if commit != sha:
    errors.append(f"manifest commit_hashes[ds-tegra-distro].commit is {commit!r}, not {sha}")
sums = {}
with open(spath) as f:
    for line in f:
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
files = {}
arts = m.get('artifacts') or {}
# The deb feed and its package list come only from -rw flavors (yocto-build
# packages them on ROOTFS_RW=true): both or neither.
feed = [n for n in ('deb_feed', 'package_list') if n in arts]
if feed and len(feed) != 2:
    errors.append(f'manifest artifacts lists {feed} without its pair; want both deb_feed and package_list, or neither')
for name in ['image'] + feed:
    a = arts.get(name) or {}
    key, digest, size = a.get('key'), a.get('sha256'), a.get('bytes')
    if not key or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or not isinstance(size, int):
        errors.append(f'manifest artifacts.{name} is missing or incomplete (key, sha256, bytes)')
        continue
    if sums.get(key) != digest:
        errors.append(f'SHA256SUMS lists {sums.get(key)!r} for {key}, manifest artifacts.{name}.sha256 is {digest}')
    files[name] = {'key': key, 'sha256': digest, 'bytes': size}
# The exported SDK (ENG-2126), when the build was called with build_sdk:
# manifest artifacts.sdk, sdk_host_manifest and sdk_target_manifest in the
# same shape, listed in SHA256SUMS under sdk/. Optional on the release kind:
# no artifacts.sdk means no SDK and nothing is required; a partial set, or
# sdk/ keys in SHA256SUMS without the manifest entries, is an error. On the
# rc kind any SDK is an error: an RC build never builds one (the SDK is the
# promote build's; release-rc-build.yml passes no build_sdk), so one here
# means the workflow and the plan disagree.
sdk_names = ('sdk', 'sdk_host_manifest', 'sdk_target_manifest')
sdk_sums = sorted(k for k in sums if k.startswith('sdk/'))
if kind == 'rc' and (any(n in arts for n in sdk_names) or sdk_sums):
    errors.append('the build carries an SDK (manifest artifacts.sdk* or sdk/ in SHA256SUMS), but an RC build never builds one: the SDK is built by the promote run only (ENG-2126); check release-rc-build.yml passes no build_sdk')
elif any(n in arts for n in sdk_names) or sdk_sums:
    for name in sdk_names:
        a = arts.get(name) or {}
        key, digest, size = a.get('key'), a.get('sha256'), a.get('bytes')
        if not key or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) or not isinstance(size, int):
            errors.append(f'manifest artifacts.{name} is missing or incomplete (key, sha256, bytes); an SDK build records all three of {", ".join(sdk_names)}')
            continue
        if not key.startswith('sdk/'):
            errors.append(f'manifest artifacts.{name}.key is {key!r}, not under sdk/')
        if sums.get(key) != digest:
            errors.append(f'SHA256SUMS lists {sums.get(key)!r} for {key}, manifest artifacts.{name}.sha256 is {digest}')
        files[name] = {'key': key, 'sha256': digest, 'bytes': size}
    listed = {files[n]['key'] for n in sdk_names if n in files}
    for k in sdk_sums:
        if k not in listed:
            errors.append(f'SHA256SUMS lists {k}, which manifest.json does not record under artifacts.sdk*')
with open(mpath, 'rb') as f:
    mdata = f.read()
mdigest = hashlib.sha256(mdata).hexdigest()
if sums.get('manifest.json') != mdigest:
    errors.append(f"SHA256SUMS lists {sums.get('manifest.json')!r} for manifest.json, the downloaded one is {mdigest}")
files['manifest'] = {'key': 'manifest.json', 'sha256': mdigest, 'bytes': len(mdata)}
with open(spath, 'rb') as f:
    sdata = f.read()
files['sha256sums'] = {'key': 'SHA256SUMS', 'sha256': hashlib.sha256(sdata).hexdigest(), 'bytes': len(sdata)}
# The update package: yocto-build's "Upload the update package to S3" step
# appends the .swu and ota/entry.json to SHA256SUMS when the build made one
# (every A/B build not built --with ota/none). Both or neither. Their sizes
# are not in manifest.json; the shell below reads them from S3.
swus = sorted(k for k in sums if k.startswith('ota/') and k.endswith('.swu'))
has_entry = 'ota/entry.json' in sums
if swus or has_entry:
    if len(swus) != 1 or not has_entry:
        errors.append(f'SHA256SUMS lists an incomplete update package (.swu: {swus}, ota/entry.json: {has_entry}); want exactly one .swu and ota/entry.json')
    else:
        files['swu'] = {'key': swus[0], 'sha256': sums[swus[0]], 'bytes': None}
        files['ota_entry'] = {'key': 'ota/entry.json', 'sha256': sums['ota/entry.json'], 'bytes': None}
for e in errors:
    print(f'::error::rc-gate: {flavor}: {e}')
if errors:
    sys.exit(1)
with open(out, 'w') as f:
    json.dump({'flavor': flavor, 'machine': machine, 'hardened': hardened == 'true',
               'build_id': build_id, 's3_prefix': prefix, 'files': files}, f)
EOF
  then
    failures=$((failures + 1))
    continue
  fi

  # The update package, when the build made one: its sizes from S3 (they are
  # not in manifest.json), and ota/entry.json must name the .swu and carry
  # the sha256 SHA256SUMS lists for it.
  swu_key=$(python3 -I -c 'import json, sys; f = json.load(open(sys.argv[1]))["files"]; print(f["swu"]["key"] if "swu" in f else "")' "${entry}")
  if [ -n "${swu_key}" ]; then
    swu_size=$(s3_object_size "${prefix}/${swu_key}") || swu_size=""
    oe_size=$(s3_object_size "${prefix}/ota/entry.json") || oe_size=""
    ota_json="${WORK}/${flavor}.entry-ota.json"
    if [ -z "${swu_size}" ] || [ -z "${oe_size}" ]; then
      fail "${flavor}" "${prefix}/${swu_key} or ${prefix}/ota/entry.json does not exist in s3://${DS_ARTIFACTS_BUCKET}"
      continue
    fi
    if ! s3_get "${prefix}/ota/entry.json" "${ota_json}" 2>/dev/null; then
      fail "${flavor}" "${prefix}/ota/entry.json could not be read"
      continue
    fi
    if ! python3 -I - "${entry}" "${ota_json}" "${swu_size}" "${oe_size}" "${flavor}" <<'EOF'
import json, os, sys
epath, opath, swu_size, oe_size, flavor = sys.argv[1:6]
with open(epath) as f:
    e = json.load(f)
with open(opath) as f:
    o = json.load(f)
swu = e['files']['swu']
bad = []
if o.get('name') != os.path.basename(swu['key']):
    bad.append(f"ota/entry.json names {o.get('name')!r}, the build uploaded {os.path.basename(swu['key'])!r}")
if o.get('sha256') != swu['sha256']:
    bad.append(f"ota/entry.json says sha256 {o.get('sha256')!r}, SHA256SUMS says {swu['sha256']}")
for b in bad:
    print(f'::error::rc-gate: {flavor}: {b}')
if bad:
    sys.exit(1)
e['files']['swu']['bytes'] = int(swu_size)
e['files']['ota_entry']['bytes'] = int(oe_size)
with open(epath, 'w') as f:
    json.dump(e, f)
EOF
    then
      failures=$((failures + 1))
      continue
    fi
  fi

  # The objects themselves: present, the recorded size, and a real image.
  ok=1
  while IFS=$'\t' read -r name key want; do
    if ! size=$(s3_object_size "${prefix}/${key}"); then
      fail "${flavor}" "${prefix}/${key} (${name}) does not exist in s3://${DS_ARTIFACTS_BUCKET}"
      ok=0
      continue
    fi
    if [ "${size}" != "${want}" ]; then
      fail "${flavor}" "${prefix}/${key} (${name}) is ${size} bytes, the manifest says ${want}"
      ok=0
      continue
    fi
    if [ "${name}" = image ] && [ "${size}" -lt "${IMAGE_MIN_BYTES}" ]; then
      fail "${flavor}" "${prefix}/${key} is ${size} bytes, below the ${IMAGE_MIN_BYTES} byte floor for a tegraflash tarball"
      ok=0
      continue
    fi
    if [ "${name}" = sdk ] && [ "${size}" -lt "${SDK_MIN_BYTES}" ]; then
      fail "${flavor}" "${prefix}/${key} is ${size} bytes, below the ${SDK_MIN_BYTES} byte floor for an SDK installer"
      ok=0
      continue
    fi
    echo "   ok ${key} (${size} bytes)"
  done < <(python3 -I - "${entry}" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    for name, a in json.load(f)['files'].items():
        print(f"{name}\t{a['key']}\t{a['bytes']}")
EOF
  )
  [ "${ok}" = 1 ] || continue
  ENTRIES+=("${entry}")
done

if [ "${failures}" -gt 0 ]; then
  case "${KIND}" in
    rc) outcome="no RC is published" ;;
    *) outcome="nothing is tagged or released" ;;
  esac
  echo "::error::rc-gate: ${failures} check(s) failed for run ${RUN_ID} at ${SHA}; ${outcome}"
  exit 1
fi

python3 -I - "${OUT}" "${KIND}" "${PRODUCT}" "${RC_VERSION}" "${PENDING_VERSION}" "${SHA}" "${RELEASE_LINE}" \
  "${RUN_ID}" "${RUN_URL}" "${RC_RUN_ID:-}" "${RC_RUN_URL:-}" "${RC_TAG:-}" "${ENTRIES[@]}" <<'EOF'
import json, sys
out, kind, product, rc_version, pending, sha, line, run_id, run_url, rc_run_id, rc_run_url, rc_tag = sys.argv[1:13]
builds = []
for path in sys.argv[13:]:
    with open(path) as f:
        builds.append(json.load(f))
doc = {
    'schema': 1,
    'kind': kind,
    'product': product,
    'version': rc_version,
    'pending': pending,
    'sha': sha,
    'line': line,
    'run_id': run_id,
    'run_url': run_url,
}
if kind == 'release':
    doc['rc_run'] = {'run_id': rc_run_id, 'run_url': rc_run_url, 'tag': rc_tag}
    doc['release_run'] = {'run_id': run_id, 'run_url': run_url}
doc['builds'] = builds
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')
EOF
echo "rc-gate: ${#ENTRIES[@]} build(s) verified for ${PRODUCT} ${RC_VERSION} (${KIND}) at $(sha7 "${SHA}"); wrote ${OUT}"
