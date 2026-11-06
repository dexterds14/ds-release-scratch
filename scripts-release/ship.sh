#!/usr/bin/env bash
set -euo pipefail

# Ship one promoted release (ENG-1936, ENG-2110): copy the release build's
# objects from the build bucket to the release bucket, verify every copy by
# content, and turn the staging pre-release vX.Y.Z into a full release.
#
#   ship.sh --plan|--ship [--product <product>] --version X.Y.Z
#
#   --plan   print what would be copied (every destination key with its size
#            from the manifest, the total) and the gh commands a real run
#            would issue; create and change nothing. The dry run of
#            release-ship.yml ends here.
#   --ship   do it (the job runs under the production environment).
#   --product  the product of release/config.json to ship. Optional when
#            names carry no product (product_prefix false): the one product
#            configured is used.
#
# Environment: RELEASE_S3_BUCKET (the destination bucket, required; the
# workflow takes it from vars.RELEASE_S3_BUCKET), DS_ARTIFACTS_BUCKET
# (lib.sh, the build bucket, default ds-yocto-artifacts), GH_TOKEN (the
# job's token selection), GITHUB_REPOSITORY (gh --repo; optional locally),
# TOKEN_KIND (app|github, only for the hint when an edit is refused),
# GITHUB_ACTOR, GITHUB_RUN_ID, GITHUB_SERVER_URL (recorded as shipped_by and
# the run link; the approver list comes from the run's approvals when both
# are set).
#
# Checks, in order (each miss is one ::error:: and exit 1):
#   1. product is in release/config.json, version is X.Y.Z, the release
#      bucket is set (it may be the build bucket: shipped releases live under
#      its release/ prefix, next to the release-manifest.json promote wrote);
#   2. the release vX.Y.Z exists (gh release view), is not a draft and is a
#      pre-release (a full release that this job did not ship is refused: it
#      was flipped by hand);
#   3. its release-manifest.json asset downloads, has schema 1, kind
#      "release" (an rc manifest is refused), the same product and version,
#      and every build lists manifest and sha256sums plus a debs list and an
#      extra list (the build's artifacts: debs, or an install-tree tarball
#      and its info file under extra with debs empty) whose entries each
#      have key, 64-hex sha256 and integer bytes, with distinct basenames
#      within one build.
# Then, per build and per file, in --ship mode:
#   4. destination s3://<RELEASE_S3_BUCKET>/release/<product>/v<version>/
#      <machine>/<standard|hardened>/<basename of key>; an object already there with
#      the recorded size is not copied again (a rerun), anything else is a
#      server-side `aws s3 cp` from s3://<DS_ARTIFACTS_BUCKET>/<s3_prefix>/
#      <key> (multipart is automatic);
#   5. the copy is streamed back (`aws s3 cp <dst> - | sha256sum`) and must
#      hash to the manifest's sha256, and head-object's ContentLength must be
#      the recorded bytes. ETags are not used: they are not a content hash
#      for multipart objects.
#   Any mismatch: every object this run's copy list names is removed (never
#   the whole prefix: promote's release-manifest.json lives there too), the
#   mismatches are listed, exit 1.
#   6. release/<product>/v<version>/release.json = the release manifest plus
#      shipped_at, shipped_by, shipped_run_url, destination {bucket, prefix,
#      keys} and verification: "ok" is written last;
#   7. gh release edit <tag> --prerelease=false --title "v<version>"
#      (promote titled it "... (staging)") --notes-file <previous body
#      plus an evidence block> (approvers, run, bucket, keys, checksums,
#      timestamp). --latest is not passed: marking the release Latest is a
#      later decision (.ai/release-lines.md).
# Idempotent: release.json already present with verification ok and the
# release already a full release: a notice, exit 0 (nothing copied). Objects
# present but no release.json (an earlier run died between verify and the
# write, or before the edit): the copies are skipped, everything is verified
# again, release.json is written and the release edited. The evidence block
# is appended once (a marker line is looked for in the body).

. "$(dirname "$(readlink -f "$0")")/lib.sh"
SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
# The checkout whose release/config.json names the product (the tests point
# it at a fixture; default: the checkout this script lives in).
RELEASE_ROOT=${RELEASE_ROOT:-$(dirname "${SCRIPTS}")}

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

MODE=""
PRODUCT=""
VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --plan) MODE=plan; shift ;;
    --ship) MODE=ship; shift ;;
    --product) PRODUCT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "::error::ship: unknown argument $1"; usage ;;
  esac
done
[ -n "${MODE}" ] || { echo "::error::ship: one of --plan or --ship is required"; usage; }
if [ -z "${PRODUCT}" ] && [ "${PRODUCT_PREFIX}" = false ]; then
  # Names carry no product, so the one product configured is the product.
  PRODUCT=$(python3 "${SCRIPTS}/version.py" --root "${RELEASE_ROOT}" products | head -1)
fi
[ -n "${PRODUCT}" ] || { echo "::error::ship: --product is required"; usage; }
[ -n "${VERSION}" ] || { echo "::error::ship: --version is required"; usage; }

# ---- 1. arguments and buckets -------------------------------------------------

[[ "${VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
  || die "ship: version '${VERSION}' is not X.Y.Z (a release version has no suffix)"
python3 "${SCRIPTS}/version.py" --root "${RELEASE_ROOT}" products | grep -qx -- "${PRODUCT}" \
  || die "ship: product '${PRODUCT}' is not in release/config.json ($(python3 "${SCRIPTS}/version.py" --root "${RELEASE_ROOT}" products | paste -sd ' '))"
DST_BUCKET="${RELEASE_S3_BUCKET:-}"
[ -n "${DST_BUCKET}" ] || die "ship: RELEASE_S3_BUCKET is empty: set the repo variable RELEASE_S3_BUCKET to the release bucket (docs/release-admin-setup.md)"
require_gh_token

TAG=$(product_tag "${PRODUCT}" "${VERSION}")
DST_PREFIX="release/${PRODUCT}/v${VERSION}"
# promote publishes "v<version> (staging)"; a shipped release drops the suffix
SHIPPED_TITLE="$(product_label "${PRODUCT}")v${VERSION}"
DST_URL="s3://${DST_BUCKET}/${DST_PREFIX}"
RELEASE_JSON_KEY="${DST_PREFIX}/release.json"
SHIP_RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-local}"
SHIPPED_BY="${GITHUB_ACTOR:-$(whoami)}"
SHIP_MARKER="<!-- ds-release-ship ${DST_URL}/ -->"

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

# obj_size <bucket> <key>: ContentLength on stdout, exit 1 when absent.
obj_size() {
  aws s3api head-object --bucket "$1" --key "$2" --query ContentLength --output text 2>/dev/null
}

# ---- 2. the release -----------------------------------------------------------

REL="${WORK}/release.json"
if ! gh release view "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" \
     --json isPrerelease,isDraft,tagName,targetCommitish,body > "${REL}" 2>"${WORK}/view.err"; then
  die "ship: release ${TAG} does not exist (promote ${PRODUCT} ${VERSION} first): $(tr '\n' ' ' < "${WORK}/view.err")"
fi
# json_field prints Python booleans (True/False); lower-case them.
IS_DRAFT=$(json_field "${REL}" isDraft | tr '[:upper:]' '[:lower:]')
IS_PRERELEASE=$(json_field "${REL}" isPrerelease | tr '[:upper:]' '[:lower:]')
TARGET=$(json_field "${REL}" targetCommitish)
[ "${IS_DRAFT}" != true ] || die "ship: release ${TAG} is a draft; promote did not finish publishing it"

# Already shipped? release.json in the destination with verification ok.
SHIPPED_BEFORE=0
if aws s3 cp --only-show-errors "s3://${DST_BUCKET}/${RELEASE_JSON_KEY}" "${WORK}/shipped.json" 2>/dev/null; then
  if [ "$(json_field "${WORK}/shipped.json" verification 2>/dev/null || true)" = ok ]; then
    SHIPPED_BEFORE=1
  fi
fi
if [ "${IS_PRERELEASE}" != true ]; then
  if [ "${SHIPPED_BEFORE}" = 1 ]; then
    note "ship: ${TAG} is already a full release and ${DST_URL}/release.json records verification ok (shipped $(json_field "${WORK}/shipped.json" shipped_at) by $(json_field "${WORK}/shipped.json" shipped_by)); nothing to do"
    exit 0
  fi
  die "ship: release ${TAG} is not a pre-release and ${DST_URL}/release.json does not record a verified ship: it was turned into a full release outside this job; ship only promotes staging pre-releases"
fi

# ---- 3. the release manifest -------------------------------------------------

gh release download "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --pattern release-manifest.json --dir "${WORK}/assets" \
  || die "ship: release ${TAG} has no release-manifest.json asset (or the download failed)"
MANIFEST="${WORK}/assets/release-manifest.json"
[ -f "${MANIFEST}" ] || die "ship: release ${TAG} has no release-manifest.json asset"

# Validate and write the copy list: one line per object,
#   flavor <TAB> name <TAB> src key <TAB> dst key <TAB> sha256 <TAB> bytes
# name is deb:<package>, extra:<basename>, manifest or sha256sums.
COPIES="${WORK}/copies.tsv"
if ! python3 -I - "${MANIFEST}" "${PRODUCT}" "${VERSION}" "${DST_PREFIX}" "${COPIES}" <<'EOF'
import json, os, re, sys
mpath, product, version, dst_prefix, out = sys.argv[1:6]
errors = []
with open(mpath) as f:
    m = json.load(f)
if m.get('schema') != 1:
    errors.append(f"release-manifest.json schema is {m.get('schema')!r}, not 1")
kind = m.get('kind')
if kind != 'release':
    errors.append(f"release-manifest.json is a {kind!r} manifest, not a release manifest: only a promoted release ships"
                  + (" (this is the RC manifest; promote the sha first)" if kind == 'rc' else ''))
if m.get('product') != product:
    errors.append(f"release-manifest.json is for product {m.get('product')!r}, not {product!r}")
if m.get('version') != version:
    errors.append(f"release-manifest.json is version {m.get('version')!r}, not {version!r}")
builds = m.get('builds') or []
if not builds:
    errors.append('release-manifest.json lists no builds')
rows = []
for b in builds:
    flavor = b.get('flavor') or '?'
    machine, prefix = b.get('machine'), b.get('s3_prefix')
    if not machine or not prefix or not isinstance(b.get('hardened'), bool):
        errors.append(f'{flavor}: build entry is missing machine, s3_prefix or hardened')
        continue
    attr = 'hardened' if b['hardened'] else 'standard'
    files = b.get('files') or {}
    seen = {}
    # Every artifact the gate recorded (debs and extra files), then the
    # build's own manifest.json and SHA256SUMS: each a {key, sha256, bytes} entry.
    entries = []
    for group in ('debs', 'extra'):
        items = files.get(group)
        if not isinstance(items, list):
            errors.append(f'{flavor}: files.{group} is missing or not a list')
            continue
        for i, a in enumerate(items):
            a = a if isinstance(a, dict) else {}
            base = os.path.basename(a.get('key') or '')
            label = a.get('package') if group == 'debs' and a.get('package') else base
            entries.append((f'{group}[{i}]', f'{group.rstrip("s")}:{label}', a))
    for name in ('manifest', 'sha256sums'):
        entries.append((name, name, files.get(name) or {}))
    for where, name, a in entries:
        key, digest, size = a.get('key'), a.get('sha256'), a.get('bytes')
        if not key or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest) \
                or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            errors.append(f'{flavor}: files.{where} is missing or incomplete (key, sha256, bytes)')
            continue
        base = os.path.basename(key)
        if base in seen:
            errors.append(f'{flavor}: files.{where} ({key}) and files.{seen[base]} share the basename {base}')
            continue
        seen[base] = where
        rows.append((flavor, name, f'{prefix}/{key}', f'{dst_prefix}/{machine}/{attr}/{base}', digest, str(size)))
for e in errors:
    print(f'::error::ship: {e}')
if errors:
    sys.exit(1)
with open(out, 'w') as f:
    for r in rows:
        f.write('\t'.join(r) + '\n')
EOF
then
  die "ship: release-manifest.json of ${TAG} is not shippable (see above)"
fi

TOTAL_BYTES=$(awk -F'\t' '{ s += $6 } END { printf "%d", s }' "${COPIES}")
N_OBJECTS=$(wc -l < "${COPIES}")
N_BUILDS=$(cut -f1 "${COPIES}" | sort -u | wc -l)

echo "== ship ${PRODUCT} ${VERSION} (${MODE})"
echo "release:      ${TAG} at ${TARGET} (pre-release, not a draft)"
echo "source:       s3://${DS_ARTIFACTS_BUCKET}/ (${N_BUILDS} build(s), ${N_OBJECTS} object(s))"
echo "destination:  ${DST_URL}/"
[ "${SHIPPED_BEFORE}" = 0 ] || note "ship: ${DST_URL}/release.json already records verification ok but ${TAG} is still a pre-release (an earlier run did not finish the release edit); copies are skipped where the object exists and everything is verified again"
echo
printf '%-60s  %14s  %s\n' "flavor" "bytes" "destination key"
while IFS=$'\t' read -r flavor name src dst digest bytes; do
  printf '%-60s  %14s  s3://%s/%s\n' "${flavor} (${name})" "${bytes}" "${DST_BUCKET}" "${dst}"
done < "${COPIES}"
printf '%-60s  %14s  %s\n' "total" "${TOTAL_BYTES}" "${N_OBJECTS} object(s) + release.json"
echo

if [ "${MODE}" = plan ]; then
  echo "plan: aws s3 cp --only-show-errors s3://${DS_ARTIFACTS_BUCKET}/<s3_prefix>/<key> s3://${DST_BUCKET}/<destination key>   (per object, server side, skipped when it exists with the recorded size)"
  echo "plan: aws s3 cp --only-show-errors s3://${DST_BUCKET}/<destination key> - | sha256sum   (per object, against the manifest's sha256; head-object ContentLength against bytes)"
  echo "plan: aws s3 cp release.json s3://${DST_BUCKET}/${RELEASE_JSON_KEY}"
  echo "plan: gh release edit ${TAG} --prerelease=false --title '${SHIPPED_TITLE}' --notes-file <previous body + evidence block>   (--latest not passed)"
  if [ "${TOKEN_KIND:-github}" != app ]; then
    echo "plan: the job has no App token (TOKEN_KIND=${TOKEN_KIND:-github}); GITHUB_TOKEN with contents: write can edit the release, and nothing here pushes a ref"
  fi
  note "ship: plan only; nothing was copied or edited (dispatch release-ship.yml with dry_run=false to ship)"
  exit 0
fi

# ---- 4 and 5. copy and verify ------------------------------------------------

MISMATCHES=()
while IFS=$'\t' read -r flavor name src dst digest bytes; do
  have=""
  if have=$(obj_size "${DST_BUCKET}" "${dst}") && [ "${have}" = "${bytes}" ]; then
    echo "   exists  s3://${DST_BUCKET}/${dst} (${bytes} bytes); copy skipped"
  else
    echo "   copy    s3://${DS_ARTIFACTS_BUCKET}/${src} -> s3://${DST_BUCKET}/${dst}"
    if ! aws s3 cp --only-show-errors "s3://${DS_ARTIFACTS_BUCKET}/${src}" "s3://${DST_BUCKET}/${dst}"; then
      MISMATCHES+=("${flavor} ${name}: copying s3://${DS_ARTIFACTS_BUCKET}/${src} to s3://${DST_BUCKET}/${dst} failed")
      continue
    fi
  fi
  got=""
  got=$(aws s3 cp --only-show-errors "s3://${DST_BUCKET}/${dst}" - | sha256sum | cut -d' ' -f1) || got=""
  size=$(obj_size "${DST_BUCKET}" "${dst}") || size=""
  if [ "${got}" != "${digest}" ]; then
    MISMATCHES+=("${flavor} ${name}: s3://${DST_BUCKET}/${dst} hashes to '${got}', the manifest says ${digest}")
  elif [ "${size}" != "${bytes}" ]; then
    MISMATCHES+=("${flavor} ${name}: s3://${DST_BUCKET}/${dst} is '${size}' bytes, the manifest says ${bytes}")
  else
    echo "   ok      sha256 ${digest} (${bytes} bytes)"
  fi
done < "${COPIES}"

if [ ${#MISMATCHES[@]} -gt 0 ]; then
  for m in "${MISMATCHES[@]}"; do
    echo "::error::ship: ${m}"
  done
  echo "ship: removing this run's copies under ${DST_URL}/ (promote's release-manifest.json stays)"
  while IFS=$'\t' read -r flavor name src dst digest bytes; do
    aws s3 rm --only-show-errors "s3://${DST_BUCKET}/${dst}" 2>/dev/null \
      || echo "::error::ship: removing s3://${DST_BUCKET}/${dst} failed; delete it by hand before the next attempt"
  done < "${COPIES}"
  die "ship: ${#MISMATCHES[@]} object(s) did not verify; the copies were removed and ${TAG} stays a pre-release"
fi
echo "ship: ${N_OBJECTS} object(s) verified by content under ${DST_URL}/"

# ---- 6. release.json -----------------------------------------------------------

SHIPPED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 -I - "${MANIFEST}" "${COPIES}" "${WORK}/release.out.json" "${SHIPPED_AT}" "${SHIPPED_BY}" "${SHIP_RUN_URL}" \
  "${DST_BUCKET}" "${DST_PREFIX}" <<'EOF'
import json, sys
mpath, copies, out, at, by, run_url, bucket, prefix = sys.argv[1:9]
with open(mpath) as f:
    doc = json.load(f)
keys = []
with open(copies) as f:
    for line in f:
        flavor, name, src, dst, digest, size = line.rstrip('\n').split('\t')
        keys.append({'flavor': flavor, 'name': name, 'key': dst, 'sha256': digest, 'bytes': int(size)})
doc['shipped_at'] = at
doc['shipped_by'] = by
doc['shipped_run_url'] = run_url
doc['destination'] = {'bucket': bucket, 'prefix': prefix, 'keys': keys}
doc['verification'] = 'ok'
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')
EOF
aws s3 cp --only-show-errors "${WORK}/release.out.json" "s3://${DST_BUCKET}/${RELEASE_JSON_KEY}" \
  || die "ship: writing s3://${DST_BUCKET}/${RELEASE_JSON_KEY} failed (the objects are verified; rerun to write it)"
echo "ship: wrote s3://${DST_BUCKET}/${RELEASE_JSON_KEY}"

# ---- 7. the release: full release plus the evidence block -----------------------

APPROVERS="not recorded (no run context)"
if [ -n "${GITHUB_RUN_ID:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  if approvals=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/approvals" 2>/dev/null); then
    APPROVERS=$(python3 -I -c '
import json, sys
d = json.loads(sys.argv[1])
names = sorted({(a.get("user") or {}).get("login", "?") for a in d if a.get("state") == "approved"})
print(", ".join(names) if names else "none recorded on the run (the environment had no required reviewer, or the approval was not readable)")
' "${approvals}" 2>/dev/null) || APPROVERS="not readable (approvals payload)"
  else
    APPROVERS="not readable (gh api .../actions/runs/${GITHUB_RUN_ID}/approvals failed; the token may lack actions: read)"
  fi
fi

NOTES="${WORK}/notes.md"
python3 -I - "${REL}" "${WORK}/release.out.json" "${NOTES}" "${SHIP_MARKER}" "${APPROVERS}" <<'EOF'
import json, sys
rel, shipped, out, marker, approvers = sys.argv[1:6]
with open(rel) as f:
    body = json.load(f).get('body') or ''
with open(shipped) as f:
    s = json.load(f)
bucket, prefix = s['destination']['bucket'], s['destination']['prefix']
lines = [body.rstrip('\n')]
if marker not in body:
    lines += ['', marker, '## Shipped', '',
              f"- Shipped at {s['shipped_at']} by `{s['shipped_by']}`",
              f"- Ship run: {s['shipped_run_url']}",
              f"- Approved by: {approvers}",
              f"- Destination: `s3://{bucket}/{prefix}/` (`release.json` there carries this record)",
              '', '| Flavor | Object | sha256 | Bytes |', '|---|---|---|---|']
    for k in s['destination']['keys']:
        lines.append(f"| `{k['flavor']}` | `s3://{bucket}/{k['key']}` | `{k['sha256']}` | {k['bytes']} |")
    lines.append('')
    lines.append('Every object was copied server side and read back in full; the sha256 above is of the copy.')
with open(out, 'w') as f:
    f.write('\n'.join(lines) + '\n')
EOF

echo "ship: gh release edit ${TAG} --prerelease=false --title '${SHIPPED_TITLE}' (notes: previous body plus the evidence block)"
if ! gh release edit "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --prerelease=false --title "${SHIPPED_TITLE}" --notes-file "${NOTES}"; then
  if [ "${TOKEN_KIND:-github}" != app ]; then
    no_app_hint "editing ${TAG} was refused" \
      "gh release edit ${TAG} --prerelease=false --title '${SHIPPED_TITLE}' --notes-file <the previous body plus the Shipped block; s3://${DST_BUCKET}/${RELEASE_JSON_KEY} has the data>"
  fi
  die "ship: gh release edit ${TAG} failed; the objects and release.json are in place, rerun to finish"
fi

note "ship: ${TAG} is a full release; ${N_OBJECTS} object(s) and release.json under ${DST_URL}/ (approved by: ${APPROVERS})"
