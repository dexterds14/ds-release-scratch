#!/usr/bin/env bash
# shellcheck shell=bash
# Shared helpers for the scripts-release shell scripts (ENG-1931, ENG-2110).
# Source it after `set -euo pipefail`:
#
#   . "$(dirname "$(readlink -f "$0")")/lib.sh"
#
# Plain bash plus aws, gh and python3 (stdlib only): the tools
# check-tools.sh requires on the runners. The tests under tests/ put stub
# aws and gh binaries on PATH, so every aws and gh call goes through a helper
# below with a fixed argument shape, and nothing else in these scripts talks
# to the network.
#
# Annotations (::error::, ::notice::, ::warning::) go to stdout: the Actions
# runner turns them into annotations on the run, and locally they are just
# lines.
#
# Sourcing reads release/config.json once (version.py config, the repo this
# file lives in) for the naming mode: with `product_prefix: false` (the mode
# this repo uses) lines are main, N.x.x, N.M.x, the release tag is vX.Y.Z,
# the RC tag rc/<sha7> and titles carry no product; with `product_prefix:
# true` every name carries the product (<product>/main, <product>/vX.Y.Z,
# rc/<product>/<sha7>, "<product> " in titles). S3 keys keep the product
# segment either way, because the bucket is shared between products.

: "${DS_ARTIFACTS_BUCKET:=ds-yocto-artifacts}"

# The commit identity of every push the jobs make: the ds-os-release-bot
# App's user, whose noreply address carries its user id (gh api
# 'users/ds-os-release-bot[bot]' --jq .id). The scripts source these and
# export GIT_AUTHOR_* / GIT_COMMITTER_* from them.
: "${GIT_BOT_NAME:=ds-os-release-bot[bot]}"
: "${GIT_BOT_EMAIL:=346446406+ds-os-release-bot[bot]@users.noreply.github.com}"

die()  { echo "::error::$*"; exit 1; }
note() { echo "::notice::$*"; }
warn() { echo "::warning::$*"; }

# PRODUCT_PREFIX: true|false from release/config.json, read once here. An
# invalid config stops the script that sourced this file (version.py has
# printed why).
LIB_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
PRODUCT_PREFIX=$(python3 "${LIB_DIR}/version.py" config --key product_prefix) \
  || die "lib.sh: release/config.json is invalid (see above)"

# sha7 <sha>: the short form used in tags and version strings.
sha7() { printf '%s\n' "${1:0:7}"; }

# product_label <product>: "<product> " when names carry the product, ""
# otherwise. Used wherever a title or subject names the product.
product_label() {
  if [ "${PRODUCT_PREFIX}" = true ]; then printf '%s \n' "$1"; else printf '\n'; fi
}

# rc_tag <product> <sha>: the RC pre-release tag, rc/<sha7> (prefixed:
# rc/<product>/<sha7>).
rc_tag() {
  if [ "${PRODUCT_PREFIX}" = true ]; then printf 'rc/%s/%s\n' "$1" "$(sha7 "$2")"; else printf 'rc/%s\n' "$(sha7 "$2")"; fi
}

# product_tag <product> <version>: the release tag, vX.Y.Z (prefixed:
# <product>/vX.Y.Z).
product_tag() {
  if [ "${PRODUCT_PREFIX}" = true ]; then printf '%s/v%s\n' "$1" "$2"; else printf 'v%s\n' "$2"; fi
}

# parse_flavor <flavor>: the same split as the build workflow's metadata
# step, <product>_<machine>_<type>[-tags...][-hardened]. Sets
#   FLAVOR_PRODUCT, FLAVOR_MACHINE, FLAVOR_TYPE (the type token with its
#   tags, minus -hardened), FLAVOR_HARDENED (true|false) and FLAVOR_ATTR
#   (standard|hardened: the suffix of the build prefix in S3).
# shellcheck disable=SC2034  # the FLAVOR_* results are read by the sourcing script
parse_flavor() {
  local flavor=$1 rest=""
  FLAVOR_PRODUCT=""; FLAVOR_MACHINE=""
  IFS=_ read -r FLAVOR_PRODUCT FLAVOR_MACHINE rest <<<"${flavor}"
  if [ -z "${FLAVOR_PRODUCT}" ] || [ -z "${FLAVOR_MACHINE}" ] || [ -z "${rest}" ] || [[ "${rest}" == *_* ]]; then
    die "flavor '${flavor}' is not <product>_<machine>_<type>[-hardened]"
  fi
  case "${rest}" in
    *-hardened) FLAVOR_HARDENED=true;  FLAVOR_ATTR=hardened; FLAVOR_TYPE=${rest%-hardened} ;;
    *)          FLAVOR_HARDENED=false; FLAVOR_ATTR=standard; FLAVOR_TYPE=${rest} ;;
  esac
}

# json_field <file> <dotted.path>: one scalar from a JSON file (objects and
# lists come back as JSON). Dies when the path is missing.
json_field() {
  python3 -I - "$1" "$2" <<'EOF' || die "json_field: $2 missing in $1"
import json, sys
path, dotted = sys.argv[1:3]
with open(path) as f:
    d = json.load(f)
for part in dotted.split('.'):
    if isinstance(d, list):
        d = d[int(part)]
    elif isinstance(d, dict) and part in d:
        d = d[part]
    else:
        sys.exit(1)
print(json.dumps(d) if isinstance(d, (dict, list)) else ('' if d is None else d))
EOF
}

# ---- S3 (the runners' ambient flasher identity; scripts-ci/aws-preflight.sh
#      runs first in every job that uses these) ----------------------------

# s3_run_prefixes <machine> <run_id> <standard|hardened>: the build ids
# (prefix basenames <UTC>-<run_id>-<attr>) under <machine>/ that belong to
# one workflow run, newest first. The UTC stamp leads, so a reverse sort is
# chronological. An empty listing is not an error here: the caller says what
# it means for the flavor it is looking for.
s3_run_prefixes() {
  local machine=$1 run_id=$2 attr=$3 listing=""
  if ! listing=$(aws s3 ls "s3://${DS_ARTIFACTS_BUCKET}/${machine}/"); then
    # aws-cli v2 exits 1 on an empty listing as well as on a failure; a
    # failure has printed its reason on stderr.
    [ -z "${listing}" ] || die "aws s3 ls s3://${DS_ARTIFACTS_BUCKET}/${machine}/ failed"
  fi
  awk '$1 == "PRE" { sub(/\/$/, "", $2); print $2 }' <<<"${listing}" \
    | { grep -E -- "-${run_id}-${attr}\$" || true; } | sort -r
}

# s3_object_size <key>: the ContentLength of s3://<bucket>/<key> on stdout;
# nonzero exit (and no output) when the object does not exist.
s3_object_size() {
  aws s3api head-object --bucket "${DS_ARTIFACTS_BUCKET}" --key "$1" \
    --query ContentLength --output text 2>/dev/null
}

# s3_get <key> <dest>: download one object.
s3_get() { aws s3 cp --only-show-errors "s3://${DS_ARTIFACTS_BUCKET}/$1" "$2"; }

# s3_put <src> <key>: upload one file.
s3_put() { aws s3 cp --only-show-errors "$1" "s3://${DS_ARTIFACTS_BUCKET}/$2"; }

# ---- gh ------------------------------------------------------------------

# In Actions GITHUB_REPOSITORY names the repo; locally gh infers it from the
# checkout's remote. Use "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" so an
# empty array is safe under set -u.
GH_REPO_ARGS=()
[ -n "${GITHUB_REPOSITORY:-}" ] && GH_REPO_ARGS=(--repo "${GITHUB_REPOSITORY}")

# require_gh_token: gh authenticates with GH_TOKEN (what the workflow's token
# selection step exports: the App token when configured, else GITHUB_TOKEN).
require_gh_token() {
  [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] \
    || die "GH_TOKEN is not set: export the job's token (steps.app.outputs.token || github.token)"
}

# gh_release_state <tag>: prints "draft" or "published" and exits 0 when a
# release with that tag exists; exits 1 when there is none.
gh_release_state() {
  local is_draft=""
  is_draft=$(gh release view "$1" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --json isDraft --jq .isDraft 2>/dev/null) || return 1
  case "${is_draft}" in
    true) echo draft ;;
    *) echo published ;;
  esac
}

# no_app_hint <what failed> <manual command>: the error a job prints when it
# ran on GITHUB_TOKEN (TOKEN_KIND != app) and hit something only the App
# can do (a ruleset bypass, a push that must start a workflow). It names
# what a human must do instead.
no_app_hint() {
  echo "::error::$1, and this job ran on GITHUB_TOKEN (vars.DS_RELEASE_APP_CLIENT_ID is unset). Either configure the ds-os-release-bot App (DS_RELEASE_APP_CLIENT_ID and DS_RELEASE_APP_PRIVATE_KEY, see docs/release-admin-setup.md) and rerun, or a human with bypass rights runs: $2"
}
