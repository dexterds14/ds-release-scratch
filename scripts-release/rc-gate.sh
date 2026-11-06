#!/usr/bin/env bash
set -euo pipefail

# The Action A gate (ENG-1931, ENG-2110): verify every build of one rc-build
# run from what the build job left in S3, and write release-manifest.json.
# With --kind release it is the release gate of promote (ENG-1934): the same
# checks on the release build, and a manifest that records both runs.
#
#   rc-gate.sh [--kind rc|release] [--rc-manifest <file>] <run_id> <sha> <product> <version> <matrix-json> <out>
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
#   --rc-manifest  with --kind release: the RC's release-manifest.json. Every
#                build's sdk (below) must then equal the RC build's for the
#                same flavor: the release is compiled with the toolchain its
#                release candidate was gated with.
#
# Environment: RELEASE_LINE (the pushed ref name) and PENDING_VERSION
# (versions/<product> at <sha>) are recorded in the manifest; GITHUB_SERVER_URL
# and GITHUB_REPOSITORY make run_url, and the repo name (GITHUB_REPOSITORY
# after the owner) is the commit_hashes entry the sha is checked against;
# DS_ARTIFACTS_BUCKET (lib.sh) defaults to ds-yocto-artifacts. --kind release
# also needs RC_RUN_ID, RC_RUN_URL and RC_TAG (the RC this release build
# repeats, from the RC's own release-manifest.json), recorded as rc_run next
# to release_run. For the tests: REPO_NAME overrides the repo name and
# RELEASE_ROOT the checkout whose release/config.json is read (default: the
# checkout this script lives in).
#
# What a build must produce comes from release/config.json (version.py
# config), the "artifact" object, by its kind:
#   debs           artifact.debs is the exact set of package names, deb_arch
#                  their architecture, deb_version_rule how their dpkg
#                  Version relates to the build version (recipe-pv: it
#                  matches ^1\.0\+git, the recipe's PV; deb-version: it equals
#                  version.py deb-version <version>, with or without the
#                  -r<N> package revision a Yocto build appends).
#                  version_probe, when set, names one deb, a path inside it
#                  (a text file or a binary) and a pattern with {version}.
#   install-trees  the build's manifest records one tarball as
#                  artifacts.install_tree {key, services} and lists it (and
#                  the info file) in artifacts.extra, with artifacts.debs
#                  empty. The tarball is downloaded, must hash to its recorded
#                  sha256, and its top level must be exactly one directory
#                  per artifact.services entry plus artifact.info_file; that
#                  file inside it must say ds_version=<version> and
#                  ds_commit=<sha>. version_probe, when set, names a path
#                  inside the tarball (read with tar -xzOf) and a pattern.
# The build job runs the same probe on its own output; the gate repeats it
# from S3. Either way the build manifest's buildparams.sdk (the SDK the
# services were compiled against: uri, sha256, machine, os_release), when
# present, is copied into the release manifest's build entry as "sdk".
#
# A matrix call of a reusable workflow keeps only one entry's outputs, so
# nothing per flavor is taken from the build job: each build is rediscovered
# in S3 by run id. For every flavor of the matrix:
#   1. machine and hardening from the flavor name (parse_flavor, the same
#      split as the build workflow's metadata step);
#   2. list s3://<bucket>/<machine>/ and keep the prefixes ending in
#      -<run_id>-<standard|hardened>/; among them the newest whose
#      manifest.json says buildparams.flavor == this flavor wins (a rerun of
#      one failed matrix job keeps the run id and gets a new timestamp);
#   3. manifest checks: buildparams.product == product,
#      buildparams.build_version == version, buildparams.flavor == the
#      flavor, commit_hashes[name == <repo name>].commit == sha; every
#      artifacts.debs[] and artifacts.extra[] entry has key, a 64-hex sha256
#      and integer bytes, with the sha256 that SHA256SUMS lists for the same
#      key (SHA256SUMS's manifest.json entry must match the downloaded
#      manifest too); then by kind: the debs' package set equals
#      artifact.debs exactly, every architecture is deb_arch and every
#      version follows deb_version_rule; or artifacts.debs is empty,
#      artifacts.install_tree names a tarball listed in extra whose services
#      equal artifact.services, and the info file is listed in extra too.
#      With --rc-manifest, buildparams.sdk equals the RC build's sdk;
#   4. every recorded object (each deb and extra file, manifest.json,
#      SHA256SUMS) exists (s3api head-object) with the recorded size;
#   5. by kind: the version probe, when configured, downloads that deb and
#      the file inside it must contain the pattern; or the tarball is
#      downloaded and checked (sha256, top level, the info file's ds_version
#      and ds_commit) and the probe, when configured, reads its path from it.
# Every miss is one ::error:: naming the flavor and the check; the script
# keeps going so one run shows every problem, then exits 1.
#
# release-manifest.json (schema 1), what publish-rc.sh, promote.sh, ship.sh
# and prune-rc.sh read:
#   {schema: 1, kind: "rc"|"release", product, version, pending, sha, line,
#    run_id, run_url, builds: [{flavor, machine, hardened, build_id,
#    s3_prefix, files: {debs: [{key, package, version, sha256, bytes}, ...],
#                       extra: [{key, sha256, bytes}, ...],
#                       manifest: {key, sha256, bytes},
#                       sha256sums: {key, sha256, bytes}},
#    sdk: {uri, sha256, machine, os_release}   (when the build recorded one)}]}
#   kind release adds rc_run: {run_id, run_url, tag} and
#   release_run: {run_id, run_url} (run_id/run_url stay the run that built).
#   files.debs is [] for an install-trees build; its tarball and info file
#   are in files.extra, so publish-rc, promote and ship handle both kinds
#   through the same file groups.
# build_id is the prefix basename (<UTC>-<run_id>-<attr>), s3_prefix is
# <machine>/<build_id>, keys are relative to s3_prefix.

. "$(dirname "$(readlink -f "$0")")/lib.sh"

KIND=rc
RC_MANIFEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --kind) KIND=$2; shift 2 ;;
    --rc-manifest) RC_MANIFEST=$2; shift 2 ;;
    --) shift; break ;;
    -*) die "rc-gate: unknown option $1" ;;
    *) break ;;
  esac
done
[ $# -eq 6 ] || die "usage: rc-gate.sh [--kind rc|release] [--rc-manifest <file>] <run_id> <sha> <product> <version> <matrix-json> <out>"
RUN_ID=$1 SHA=$2 PRODUCT=$3 RC_VERSION=$4 MATRIX_JSON=$5 OUT=$6
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${RUN_ID}"
REPO_NAME=${REPO_NAME:-${GITHUB_REPOSITORY:-}}
REPO_NAME=${REPO_NAME##*/}
RELEASE_ROOT=${RELEASE_ROOT:-$(dirname "${LIB_DIR}")}

case "${KIND}" in rc|release) ;; *) die "rc-gate: --kind must be rc or release, got '${KIND}'" ;; esac
[[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "rc-gate: sha '${SHA}' is not a full 40-hex commit"
[[ "${RUN_ID}" =~ ^[0-9]+$ ]] || die "rc-gate: run_id '${RUN_ID}' is not a number"
[ -n "${RELEASE_LINE:-}" ] || die "rc-gate: RELEASE_LINE is not set"
[ -n "${PENDING_VERSION:-}" ] || die "rc-gate: PENDING_VERSION is not set"
[ -n "${REPO_NAME}" ] || die "rc-gate: the repo name is unknown (set GITHUB_REPOSITORY or REPO_NAME)"
if [ "${KIND}" = release ]; then
  [[ "${RC_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "rc-gate: a release build's version must be plain X.Y.Z, got '${RC_VERSION}'"
  [ -n "${RC_RUN_ID:-}" ] && [ -n "${RC_RUN_URL:-}" ] && [ -n "${RC_TAG:-}" ] \
    || die "rc-gate: --kind release needs RC_RUN_ID, RC_RUN_URL and RC_TAG (the RC this release build repeats)"
else
  [ -z "${RC_MANIFEST}" ] || die "rc-gate: --rc-manifest goes with --kind release"
fi
if [ -n "${RC_MANIFEST}" ]; then
  [ -f "${RC_MANIFEST}" ] || die "rc-gate: --rc-manifest ${RC_MANIFEST} does not exist"
  [ "$(json_field "${RC_MANIFEST}" kind)" = rc ] || die "rc-gate: ${RC_MANIFEST} is not an rc manifest (kind != rc)"
fi

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

# The artifact rules, read once. An invalid config stops here (version.py says why).
CONFIG="${WORK}/config.json"
python3 "${LIB_DIR}/version.py" --root "${RELEASE_ROOT}" config > "${CONFIG}" \
  || die "rc-gate: ${RELEASE_ROOT}/release/config.json is invalid (see above)"
ARTIFACT_KIND=$(json_field "${CONFIG}" artifact.kind)
WANT_DEB_VERSION=""
INFO_FILE=""
case "${ARTIFACT_KIND}" in
  debs)
    if [ "$(json_field "${CONFIG}" artifact.deb_version_rule)" = deb-version ]; then
      WANT_DEB_VERSION=$(python3 "${LIB_DIR}/version.py" deb-version "${RC_VERSION}") \
        || die "rc-gate: version.py deb-version refuses '${RC_VERSION}'"
    fi
    command -v dpkg-deb >/dev/null 2>&1 || die "rc-gate: dpkg-deb is not installed (artifact kind debs reads the debs)"
    ;;
  install-trees)
    INFO_FILE=$(json_field "${CONFIG}" artifact.info_file)
    ;;
  *) die "rc-gate: unknown artifact kind '${ARTIFACT_KIND}'" ;;
esac
echo "rc-gate: artifact kind ${ARTIFACT_KIND}"

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

  # Manifest content, the artifact list and the rules of the artifact kind;
  # writes the manifest entry.
  entry="${WORK}/${flavor}.entry.json"
  if ! python3 -I - "${manifest}" "${sums}" "${CONFIG}" "${flavor}" "${PRODUCT}" "${SHA}" "${RC_VERSION}" \
       "${REPO_NAME}" "${WANT_DEB_VERSION}" "${FLAVOR_MACHINE}" "${FLAVOR_HARDENED}" "${build_id}" "${prefix}" "${entry}" \
       "${RC_MANIFEST}" <<'EOF'
import hashlib, json, re, sys
(mpath, spath, cpath, flavor, product, sha, rc_version, repo_name, want_deb_version,
 machine, hardened, build_id, prefix, out, rc_manifest) = sys.argv[1:16]
errors = []
with open(mpath) as f:
    m = json.load(f)
with open(cpath) as f:
    cfg = json.load(f)
art = cfg['artifact']
bp = m.get('buildparams') or {}
if bp.get('product') != product:
    errors.append(f"manifest buildparams.product is {bp.get('product')!r}, not {product!r}")
if bp.get('build_version') != rc_version:
    errors.append(f"manifest buildparams.build_version is {bp.get('build_version')!r}, not the plan's {rc_version!r}")
if bp.get('flavor') != flavor:
    errors.append(f"manifest buildparams.flavor is {bp.get('flavor')!r}, not {flavor!r}")
commit = next((c.get('commit') for c in m.get('commit_hashes') or [] if c.get('name') == repo_name), None)
if commit != sha:
    errors.append(f"manifest commit_hashes[{repo_name}].commit is {commit!r}, not {sha}")
sums = {}
with open(spath) as f:
    for line in f:
        parts = line.split()
        if len(parts) == 2:
            sums[parts[1]] = parts[0]
arts = m.get('artifacts') or {}

def checked(kind, i, a, fields):
    # One artifact entry: the named string fields, a 64-hex sha256, integer
    # bytes, and SHA256SUMS agreeing on the digest. None when incomplete.
    key, digest, size = a.get('key'), a.get('sha256'), a.get('bytes')
    strings_ok = all(isinstance(a.get(k), str) and a.get(k) for k in fields)
    if not strings_ok or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) \
       or not isinstance(size, int) or isinstance(size, bool):
        errors.append(f"manifest artifacts.{kind}[{i}] is missing or incomplete ({', '.join(fields)}, sha256, bytes)")
        return None
    if sums.get(key) != digest:
        errors.append(f'SHA256SUMS lists {sums.get(key)!r} for {key}, manifest artifacts.{kind}[{i}].sha256 is {digest}')
    return a

debs = []
for i, a in enumerate(arts.get('debs') or []):
    a = checked('debs', i, a or {}, ('key', 'package', 'version', 'architecture'))
    if a is not None:
        debs.append(a)
extra = []
for i, a in enumerate(arts.get('extra') or []):
    a = checked('extra', i, a or {}, ('key',))
    if a is not None:
        extra.append(a)
install_tree = None
if art['kind'] == 'debs':
    if not (arts.get('debs') or []):
        errors.append('manifest artifacts.debs is empty')
    # The package set, the architecture and the version rule.
    want = set(art['debs'])
    got = [d['package'] for d in debs]
    dup = sorted({p for p in got if got.count(p) > 1})
    if dup:
        errors.append(f"the build lists a package twice: {', '.join(dup)}")
    missing = sorted(want - set(got))
    if missing:
        errors.append(f"the build is missing packages release/config.json lists: {', '.join(missing)}")
    unlisted = sorted(set(got) - want)
    if unlisted:
        errors.append(f"the build produced packages release/config.json does not list: {', '.join(unlisted)}")
    rule = art['deb_version_rule']
    for d in debs:
        if d['architecture'] != art['deb_arch']:
            errors.append(f"{d['key']} ({d['package']}) is for architecture {d['architecture']!r}, not {art['deb_arch']!r}")
        if rule == 'recipe-pv':
            if not re.match(r'1\.0\+git', d['version']):
                errors.append(f"{d['key']} ({d['package']}) has version {d['version']!r}, which does not match ^1\\.0\\+git (deb_version_rule recipe-pv)")
        elif not re.fullmatch(re.escape(want_deb_version) + r'(-r[0-9]+)?', d['version']):
            errors.append(f"{d['key']} ({d['package']}) has version {d['version']!r}, not {want_deb_version!r} (deb_version_rule deb-version of {rc_version})")
else:
    # One install-tree tarball, recorded as artifacts.install_tree and listed
    # in extra (so it has a sha256 and a size); the info file listed in extra
    # too; no debs. The tarball itself is checked after download.
    if arts.get('debs'):
        errors.append(f"manifest artifacts.debs lists {len(arts['debs'])} deb(s); an install-trees build records none")
    it = arts.get('install_tree')
    if not isinstance(it, dict) or not isinstance(it.get('key'), str) or not it.get('key') \
       or not isinstance(it.get('services'), list):
        errors.append('manifest artifacts.install_tree {key, services} is missing (is the build using artifacts.py --kind install-trees?)')
    else:
        extra_keys = {a['key'] for a in extra}
        if it['key'] not in extra_keys:
            errors.append(f"manifest artifacts.install_tree.key {it['key']!r} is not listed in artifacts.extra")
        want = set(art['services'])
        got = set(it['services'])
        missing = sorted(want - got)
        if missing:
            errors.append(f"the install tree is missing services release/config.json lists: {', '.join(missing)}")
        unlisted = sorted(got - want)
        if unlisted:
            errors.append(f"the install tree has top-level directories release/config.json does not list: {', '.join(unlisted)}")
        if art['info_file'] not in extra_keys:
            errors.append(f"manifest artifacts.extra does not list the info file {art['info_file']}")
        if not errors:
            install_tree = {'key': it['key'], 'sha256': next(a['sha256'] for a in extra if a['key'] == it['key'])}

# The SDK the services were compiled against, carried into the release
# manifest; with the RC manifest given it must be the RC build's.
sdk = bp.get('sdk')
if sdk is not None and (not isinstance(sdk, dict) or not sdk.get('uri') or not sdk.get('sha256')):
    errors.append(f'manifest buildparams.sdk is incomplete (uri and sha256 at least): {sdk!r}')
    sdk = None
if rc_manifest:
    with open(rc_manifest) as f:
        rc_builds = [b for b in (json.load(f).get('builds') or []) if b.get('flavor') == flavor]
    if not rc_builds:
        errors.append(f"the RC's release-manifest.json has no build for flavor {flavor}")
    else:
        rc_sdk = rc_builds[0].get('sdk')
        if rc_sdk != sdk:
            errors.append(f"buildparams.sdk {json.dumps(sdk)} is not the RC build's SDK {json.dumps(rc_sdk)}; "
                          "a release is compiled with the SDK its release candidate was gated with; rerun the release candidate build, then promote again")

with open(mpath, 'rb') as f:
    mdata = f.read()
mdigest = hashlib.sha256(mdata).hexdigest()
if sums.get('manifest.json') != mdigest:
    errors.append(f"SHA256SUMS lists {sums.get('manifest.json')!r} for manifest.json, the downloaded one is {mdigest}")
with open(spath, 'rb') as f:
    sdata = f.read()
for e in errors:
    print(f'::error::rc-gate: {flavor}: {e}')
if errors:
    sys.exit(1)
files = {
    'debs': [{'key': d['key'], 'package': d['package'], 'version': d['version'],
              'sha256': d['sha256'], 'bytes': d['bytes']} for d in debs],
    'extra': [{'key': a['key'], 'sha256': a['sha256'], 'bytes': a['bytes']} for a in extra],
    'manifest': {'key': 'manifest.json', 'sha256': mdigest, 'bytes': len(mdata)},
    'sha256sums': {'key': 'SHA256SUMS', 'sha256': hashlib.sha256(sdata).hexdigest(), 'bytes': len(sdata)},
}
entry = {'flavor': flavor, 'machine': machine, 'hardened': hardened == 'true',
         'build_id': build_id, 's3_prefix': prefix, 'files': files}
if sdk is not None:
    entry['sdk'] = sdk
if install_tree is not None:
    entry['_install_tree'] = install_tree   # for the tarball checks below; dropped from the output
with open(out, 'w') as f:
    json.dump(entry, f)
EOF
  then
    failures=$((failures + 1))
    continue
  fi

  # The objects themselves: present with the recorded size.
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
    echo "   ok ${key} (${size} bytes)"
  done < <(python3 -I - "${entry}" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    files = json.load(f)['files']
for d in files['debs']:
    print(f"{d['package']}\t{d['key']}\t{d['bytes']}")
for a in files['extra']:
    print(f"extra\t{a['key']}\t{a['bytes']}")
for name in ('manifest', 'sha256sums'):
    print(f"{name}\t{files[name]['key']}\t{files[name]['bytes']}")
EOF
  )
  [ "${ok}" = 1 ] || continue

  # The version probe pattern, when one is configured.
  probe=$(json_field "${CONFIG}" version_probe)
  probe_path="" probe_raw="" probe_pattern=""
  if [ -n "${probe}" ]; then
    probe_path=$(json_field "${CONFIG}" version_probe.path)
    probe_raw=$(json_field "${CONFIG}" version_probe.pattern)
    probe_pattern=${probe_raw//\{version\}/${RC_VERSION}}
  fi

  # probe_miss <what> <file>: the failure line for a file that lacks the
  # pattern, quoting what it says instead when the pattern has a fixed lead-in.
  probe_miss() {
    local found="" lead=${probe_raw%%\{version\}*}
    if [ -n "${lead}" ]; then
      found=$(grep -aF -- "${lead}" "$2" | head -3 | tr -c '[:print:]\n' '?' | tr '\n' ' ' || true)
    fi
    fail "${flavor}" "$1 ${probe_path} does not contain '${probe_pattern}' (found: ${found:-nothing})"
  }

  if [ "${ARTIFACT_KIND}" = debs ]; then
    # The version probe: the named deb must carry the build version where
    # the config says it does.
    if [ -n "${probe}" ]; then
      probe_pkg=$(json_field "${CONFIG}" version_probe.deb)
      probe_key=$(python3 -I - "${entry}" "${probe_pkg}" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    print(next(d['key'] for d in json.load(f)['files']['debs'] if d['package'] == sys.argv[2]))
EOF
      )
      probe_deb="${WORK}/${flavor}.probe.deb"
      probe_file="${WORK}/${flavor}.probe.txt"
      if ! s3_get "${prefix}/${probe_key}" "${probe_deb}" 2>/dev/null; then
        fail "${flavor}" "${prefix}/${probe_key} (${probe_pkg}) could not be downloaded for the version probe"
        continue
      fi
      if ! dpkg-deb --fsys-tarfile "${probe_deb}" | tar -xO "${probe_path}" > "${probe_file}" 2>/dev/null; then
        fail "${flavor}" "${probe_key} (${probe_pkg}) has no ${probe_path} (version_probe)"
        continue
      fi
      if ! grep -qaF -- "${probe_pattern}" "${probe_file}"; then
        probe_miss "${probe_key} (${probe_pkg})" "${probe_file}"
        continue
      fi
      echo "   ok ${probe_key} ${probe_path} contains '${probe_pattern}'"
    fi
  else
    # The install tree itself: the tarball hashes to its recorded sha256, its
    # top level is exactly the configured services plus the info file, the
    # info file names this version and sha, and the probe path (when
    # configured) carries the version.
    tree_key=$(json_field "${entry}" _install_tree.key)
    tree_sha=$(json_field "${entry}" _install_tree.sha256)
    tarball="${WORK}/${flavor}.install-tree.tar.gz"
    if ! s3_get "${prefix}/${tree_key}" "${tarball}" 2>/dev/null; then
      fail "${flavor}" "${prefix}/${tree_key} (the install tree) could not be downloaded"
      continue
    fi
    got_sha=$(sha256sum "${tarball}" | cut -d' ' -f1)
    if [ "${got_sha}" != "${tree_sha}" ]; then
      fail "${flavor}" "${tree_key} hashes to ${got_sha}, the manifest says ${tree_sha}"
      continue
    fi
    if ! top=$(tar -tzf "${tarball}" 2>/dev/null | sed 's#^\./##' | awk -F/ '$1 != "" { print $1 }' | sort -u); then
      fail "${flavor}" "${tree_key} is not a readable tarball"
      continue
    fi
    want=$(python3 -I -c 'import json, sys; a = json.load(open(sys.argv[1]))["artifact"]; print("\n".join(sorted(a["services"] + [a["info_file"]])))' "${CONFIG}")
    if [ "${top}" != "${want}" ]; then
      fail "${flavor}" "${tree_key} top level is [$(echo "${top}" | paste -sd ' ')], release/config.json wants the services plus ${INFO_FILE}: [$(echo "${want}" | paste -sd ' ')]"
      continue
    fi
    info="${WORK}/${flavor}.info.txt"
    if ! { tar -xzOf "${tarball}" "./${INFO_FILE}" 2>/dev/null || tar -xzOf "${tarball}" "${INFO_FILE}" 2>/dev/null; } > "${info}" || [ ! -s "${info}" ]; then
      fail "${flavor}" "${tree_key} has no ${INFO_FILE} at its top level"
      continue
    fi
    info_version=$(sed -n 's/^ds_version=//p' "${info}" | head -1)
    info_commit=$(sed -n 's/^ds_commit=//p' "${info}" | head -1)
    if [ "${info_version}" != "${RC_VERSION}" ]; then
      fail "${flavor}" "${INFO_FILE} inside ${tree_key} says ds_version=${info_version:-<unset>}, not the plan's ${RC_VERSION}"
      continue
    fi
    if [ "${info_commit}" != "${SHA}" ]; then
      fail "${flavor}" "${INFO_FILE} inside ${tree_key} says ds_commit=${info_commit:-<unset>}, not ${SHA}"
      continue
    fi
    echo "   ok ${tree_key}: sha256, top level ($(echo "${want}" | wc -l) entries), ${INFO_FILE} ds_version and ds_commit"
    if [ -n "${probe}" ]; then
      probe_file="${WORK}/${flavor}.probe.txt"
      if ! tar -xzOf "${tarball}" "${probe_path}" > "${probe_file}" 2>/dev/null; then
        fail "${flavor}" "${tree_key} has no ${probe_path} (version_probe)"
        continue
      fi
      if ! grep -qaF -- "${probe_pattern}" "${probe_file}"; then
        probe_miss "${tree_key}" "${probe_file}"
        continue
      fi
      echo "   ok ${tree_key} ${probe_path} contains '${probe_pattern}'"
    fi
  fi
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
        b = json.load(f)
    b.pop('_install_tree', None)
    builds.append(b)
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
