#!/usr/bin/env bash
set -euo pipefail

# Publish one RC pre-release from a release-manifest.json (ENG-1931, ENG-2110).
#
#   publish-rc.sh <release-manifest.json>
#
# Environment: GH_TOKEN (the job's token selection: the App token when
# configured, else GITHUB_TOKEN), GITHUB_REPOSITORY (gh --repo; optional
# locally), TOKEN_KIND (app|github, only for the hint in the error when a
# create is refused), DS_ARTIFACTS_BUCKET (lib.sh).
#
# Idempotent per sha, because one push can produce two runs of the same
# workflow and a rerun of the current ref is a supported way to retry:
#   - the release rc/<sha7> (lib.sh rc_tag) is already published: a notice,
#     exit 0;
#   - it exists as a draft (an earlier run died between create and publish):
#     resume, uploading the assets again (--clobber) and publishing;
#   - otherwise: gh release create --draft --prerelease --target <sha>, upload
#     the assets, then gh release edit --draft=false. The draft -> assets ->
#     publish order is what immutable releases require (a published release's
#     assets are locked), and a draft creates no tag, so a failed upload burns
#     nothing.
# Assets: release-manifest.json, every artifact of every build (its debs
# and extra files: for an install-trees build the tarball and the info file;
# downloaded from the build prefix the gate verified, under their own
# names), and each build's manifest.json and SHA256SUMS (named
# <flavor>.manifest.json and <flavor>.SHA256SUMS). Last, release-manifest.json
# is copied to s3://<bucket>/rc/<product>/<sha7>/release-manifest.json, the
# index abandon and retention use.

. "$(dirname "$(readlink -f "$0")")/lib.sh"

[ $# -eq 1 ] || die "usage: publish-rc.sh <release-manifest.json>"
MANIFEST=$1
[ -f "${MANIFEST}" ] || die "publish-rc: ${MANIFEST} does not exist"
require_gh_token

PRODUCT=$(json_field "${MANIFEST}" product)
SHA=$(json_field "${MANIFEST}" sha)
RC_VERSION=$(json_field "${MANIFEST}" version)
PENDING=$(json_field "${MANIFEST}" pending)
[ "$(json_field "${MANIFEST}" kind)" = rc ] || die "publish-rc: ${MANIFEST} is not an rc manifest (kind != rc)"
[[ "${SHA}" =~ ^[0-9a-f]{40}$ ]] || die "publish-rc: manifest sha '${SHA}' is not a full commit"
SHA7=$(sha7 "${SHA}")
TAG=$(rc_tag "${PRODUCT}" "${SHA}")
TITLE="RC $(product_label "${PRODUCT}")${RC_VERSION}"
PROMOTE_TAG=$(product_tag "${PRODUCT}" "${PENDING}")

resume=0
if state=$(gh_release_state "${TAG}"); then
  if [ "${state}" = published ]; then
    note "publish-rc: release ${TAG} already exists; nothing to publish (same sha built again)"
    exit 0
  fi
  note "publish-rc: release ${TAG} exists as a draft (an earlier publish did not finish); resuming"
  resume=1
fi

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT
ASSETS=("${WORK}/release-manifest.json")
cp "${MANIFEST}" "${WORK}/release-manifest.json"

# Every build's artifacts (debs and extra files) under their own names, then
# its manifest.json and SHA256SUMS, from the prefixes the gate verified. Release
# assets share one namespace, so a basename that two builds both record
# cannot be attached and stops the publish before anything is created.
while IFS=$'\t' read -r flavor prefix key; do
  case "${key}" in
    manifest.json|SHA256SUMS) dest="${WORK}/${flavor}.${key}" ;;
    *)
      dest="${WORK}/$(basename "${key}")"
      [ ! -e "${dest}" ] || die "publish-rc: two builds record an asset named $(basename "${key}"); release assets must be distinct"
      ;;
  esac
  s3_get "${prefix}/${key}" "${dest}" || die "publish-rc: could not download ${prefix}/${key}"
  ASSETS+=("${dest}")
done < <(python3 -I - "${MANIFEST}" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    for b in json.load(f)['builds']:
        files = b['files']
        for a in files['debs'] + files['extra'] + [files['manifest'], files['sha256sums']]:
            print(f"{b['flavor']}\t{b['s3_prefix']}\t{a['key']}")
EOF
)

# The release body.
python3 -I - "${MANIFEST}" "${DS_ARTIFACTS_BUCKET}" "${PROMOTE_TAG}" "${PRODUCT_PREFIX}" > "${WORK}/notes.md" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
bucket, promote_tag = sys.argv[2:4]
label = f" of **{m['product']}**" if sys.argv[4] == 'true' else ''
sha7 = m['sha'][:7]
print(f"Release candidate `{m['version']}`{label}, built from `{m['line']}` at `{m['sha']}`.")
print()
print(f"- Line: `{m['line']}`")
print(f"- Run: {m['run_url']}")
print(f"- Pending version on the line: `{m['pending']}` (a promote of this sha tags `{promote_tag}`)")
print(f"- Release manifest: `release-manifest.json` (asset) and `s3://{bucket}/rc/{m['product']}/{sha7}/release-manifest.json`")
print()
print("| Flavor | Build id | S3 prefix | Artifacts | manifest.json sha256 |")
print("|---|---|---|---|---|")
for b in m['builds']:
    files = b['files']
    print(f"| `{b['flavor']}` | `{b['build_id']}` | `s3://{bucket}/{b['s3_prefix']}/` | {len(files['debs']) + len(files['extra'])} | `{files['manifest']['sha256']}` |")
print()
print("The assets here are every build's artifacts (debs, install trees and other files), its `manifest.json` and `SHA256SUMS`; the build prefix in S3 keeps the originals.")
EOF

if [ "${resume}" = 0 ]; then
  echo "publish-rc: creating draft ${TAG} at ${SHA7} (${TITLE})"
  if ! gh release create "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --draft --prerelease \
       --target "${SHA}" --title "${TITLE}" --notes-file "${WORK}/notes.md"; then
    if [ "${TOKEN_KIND:-github}" != app ]; then
      no_app_hint "creating ${TAG} was refused (the release-tags ruleset restricts tag creation to the App once it is active)" \
        "gh release create ${TAG} --prerelease --target ${SHA} --title '${TITLE}' --notes-file <body> release-manifest.json <every artifact of every build> <flavor>.manifest.json <flavor>.SHA256SUMS"
    fi
    die "publish-rc: gh release create ${TAG} failed"
  fi
fi

echo "publish-rc: uploading ${#ASSETS[@]} asset(s) to ${TAG}"
gh release upload "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --clobber "${ASSETS[@]}" \
  || die "publish-rc: gh release upload ${TAG} failed"

echo "publish-rc: publishing ${TAG} as a pre-release"
gh release edit "${TAG}" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --draft=false --prerelease \
  || die "publish-rc: gh release edit ${TAG} --draft=false failed"

s3_put "${WORK}/release-manifest.json" "rc/${PRODUCT}/${SHA7}/release-manifest.json" \
  || die "publish-rc: copying release-manifest.json to s3://${DS_ARTIFACTS_BUCKET}/rc/${PRODUCT}/${SHA7}/ failed"

note "publish-rc: ${TAG} published (${TITLE}); index s3://${DS_ARTIFACTS_BUCKET}/rc/${PRODUCT}/${SHA7}/release-manifest.json"
