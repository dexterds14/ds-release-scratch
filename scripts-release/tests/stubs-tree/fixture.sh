#!/usr/bin/env bash
# shellcheck shell=bash
# Fixture for the create-branch, merge-up, abandon and prune-rc shell tests
# (ENG-1935, ENG-2110). Source it after `set -euo pipefail`; it needs `tmp`
# (a temp dir the caller removes) and `scripts` (the scripts-release dir).
#
# tree_fixture_init
#   One bare repo under $tmp stands in for origin ($BARE: trunk `main` with
#   the release-line files: versions/ds 0.0.1, release/config.json with
#   product ds and product_prefix false, release/matrix.json, the
#   bookkeeping file .github/yocto-ci.env, foo.txt). $REPO is a clone the
#   scripts run in. The stub gh and aws from this directory go first on
#   PATH, with GH_STUB_STATE, GH_STUB_REPOS, GH_STUB_GREEN, FAKE_S3,
#   FAKE_RELEASES, AWS_CALLS set, and the script environment (TOKEN,
#   TOKEN_KIND=app, DRY_RUN=false, GH_TOKEN, GITHUB_REPOSITORY) exported.
#   No GIT_* identity is exported: the fixture's own commits set one per
#   call (tgit) and the scripts must fall back to lib.sh's bot identity.
# open_line <N.M.P> [args]   create-branch.sh --version (the tag must exist)
# tag_origin <tag> <ref>     an annotated tag pushed to the bare repo
# repo_commit <branch> <msg> <path>=<content>...   a commit pushed to a branch (prints the sha;
#                            REPO_COMMIT_START=<ref> creates the branch from <ref>)
# head_of <bare> <branch>    the sha of a branch in a bare repo ("" when absent)
# heads_of <bare>            the branch names of a bare repo, one per line
# tags_of <bare>             the tag names of a bare repo, one per line
# mark_green <sha>...        the shas gh run list reports a successful rc-build for
# make_release <tag> <sha> <line> <version> <machine> <build_id>...
#   an rc pre-release of the repo in the stub (release-manifest.json asset
#   listing one build per build_id under <machine>/), the tag in the bare
#   repo, the S3 index copy rc/ds/<sha7>/ and a per-build prefix with a few
#   objects.
# gh_log / aws_log           the stub call logs

TREE_STUBS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OWN_REPO=ds/repo
PRODUCT=ds
ENV_FILE=.github/yocto-ci.env

tgit() {
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x git "$@"
}

# tmp and scripts are set by the test that sources this file.
# shellcheck disable=SC2154
tree_fixture_init() {
  BARE="${tmp}/origin.git"
  REPO="${tmp}/repo"
  STATE="${tmp}/gh-state"
  mkdir -p "${STATE}"
  export PATH="${TREE_STUBS}:${PATH}"
  export GH_STUB_STATE="${STATE}" GH_STUB_REPOS="${OWN_REPO}=${BARE}" GH_STUB_GREEN="${STATE}/green"
  export FAKE_S3="${tmp}/s3" AWS_CALLS="${tmp}/aws.calls" DS_ARTIFACTS_BUCKET=ds-yocto-artifacts
  export TOKEN=stub-token TOKEN_KIND=app DRY_RUN=false GH_TOKEN=stub-token
  export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY="${OWN_REPO}" GITHUB_RUN_ID=4242
  # The scripts read the product list from RELEASE_ROOT: the fixture clone,
  # so the suite is the same in every repo that carries the tooling.
  export RELEASE_ROOT="${REPO}"
  unset GITHUB_STEP_SUMMARY CALLER_SHA CALLER_RUN
  unset GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL
  BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
  mkdir -p "${BUCKET}"
  : > "${AWS_CALLS}"
  : > "${GH_STUB_GREEN}"

  git init -q --bare -b main "${BARE}"
  local seed="${tmp}/seed"
  git init -q -b main "${seed}"
  mkdir -p "${seed}/release" "${seed}/versions" "${seed}/.github"
  cat > "${seed}/release/config.json" <<EOF
{"products": ["${PRODUCT}"], "product_prefix": false, "bookkeeping_files": ["${ENV_FILE}"]}
EOF
  cat > "${seed}/release/matrix.json" <<EOF
{"products": {"${PRODUCT}": {"flavors": ["${PRODUCT}_ds-arm64_release"]}}}
EOF
  echo 0.0.1 > "${seed}/versions/${PRODUCT}"
  echo '# versions' > "${seed}/versions/README.md"
  echo 'DS_TEGRA_DISTRO_REF=main' > "${seed}/${ENV_FILE}"
  echo trunk > "${seed}/foo.txt"
  tgit -C "${seed}" add -A && tgit -C "${seed}" commit -q -m seed
  SEED_SHA=$(git -C "${seed}" rev-parse HEAD)
  export SEED_SHA   # for tests that want the seed commit
  git -C "${seed}" remote add origin "${BARE}"
  git -C "${seed}" push -q origin main
  git clone -q "${BARE}" "${REPO}"
}

# open_line <N.M.P> [create-branch args...]
# shellcheck disable=SC2154
open_line() {
  (cd "${REPO}" && "${scripts}/create-branch.sh" --version "$@" > "${tmp}/open.log" 2>&1) \
    || { cat "${tmp}/open.log"; echo "fixture: opening a line from $1 failed" >&2; return 1; }
}

tag_origin() {
  git -C "${REPO}" fetch -q origin
  tgit -C "${REPO}" tag -a -m "$1" "$1" "$2"
  git -C "${REPO}" push -q origin "refs/tags/$1"
}

# repo_commit <branch> <msg> <path>=<content>...  (content: \n for newlines;
# REPO_COMMIT_START=<ref> creates <branch> from <ref> instead of origin/<branch>)
repo_commit() {
  local branch=$1 msg=$2 kv
  shift 2
  git -C "${REPO}" fetch -q origin
  git -C "${REPO}" checkout -q -B "${branch}" "${REPO_COMMIT_START:-origin/${branch}}"
  for kv in "$@"; do
    mkdir -p "$(dirname "${REPO}/${kv%%=*}")"
    printf '%b' "${kv#*=}" > "${REPO}/${kv%%=*}"
  done
  tgit -C "${REPO}" add -A
  tgit -C "${REPO}" commit -q -m "${msg}"
  git -C "${REPO}" push -q origin "${branch}"
  git -C "${REPO}" rev-parse HEAD
}

head_of() { git -C "$1" rev-parse --verify --quiet "refs/heads/$2^{commit}" 2>/dev/null || true; }
heads_of() { git -C "$1" for-each-ref --format='%(refname:short)' refs/heads/ | LC_ALL=C sort; }
tags_of() { git -C "$1" for-each-ref --format='%(refname:short)' refs/tags/ | LC_ALL=C sort; }

mark_green() { printf '%s\n' "$@" > "${GH_STUB_GREEN}"; }

# make_release <tag> <sha> <line> <version> <machine> <build_id>...
make_release() {
  local tag=$1 sha=$2 line=$3 version=$4 machine=$5 sha7=${2:0:7}
  shift 5
  local rel="${STATE}/releases/${OWN_REPO//\//_}"
  local assets="${STATE}/assets/${tag//\//_}"
  mkdir -p "${rel}" "${assets}"
  local builds="" bid
  for bid in "$@"; do
    local prefix="${machine}/${bid}"
    mkdir -p "${BUCKET}/${prefix}/deb"
    printf 'deb bytes of %s' "${bid}" > "${BUCKET}/${prefix}/deb/${PRODUCT}-utils_1.0+git_arm64.deb"
    echo '{}' > "${BUCKET}/${prefix}/manifest.json"
    echo 'sums' > "${BUCKET}/${prefix}/SHA256SUMS"
    builds="${builds}{\"flavor\": \"${PRODUCT}_${machine}_release\", \"machine\": \"${machine}\", \"hardened\": false, \"build_id\": \"${bid}\", \"s3_prefix\": \"${prefix}\", \"files\": {}},"
  done
  cat > "${assets}/release-manifest.json" <<EOF
{"schema": 1, "kind": "rc", "product": "${PRODUCT}", "version": "${version}", "pending": "${version%%-*}", "sha": "${sha}",
 "line": "${line}", "run_id": "1", "run_url": "https://github.com/${OWN_REPO}/actions/runs/1",
 "builds": [${builds%,}]}
EOF
  cat > "${rel}/${tag//\//_}.json" <<EOF
{"tag_name": "${tag}", "draft": false, "assets": {"release-manifest.json": "${assets}/release-manifest.json"}}
EOF
  mkdir -p "${BUCKET}/rc/${PRODUCT}/${sha7}"
  cp "${assets}/release-manifest.json" "${BUCKET}/rc/${PRODUCT}/${sha7}/release-manifest.json"
  git -C "${BARE}" update-ref "refs/tags/${tag}" "${sha}"
}

gh_log() { cat "${STATE}/log" 2>/dev/null || true; }
aws_log() { cat "${AWS_CALLS}" 2>/dev/null || true; }
