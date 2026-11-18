#!/usr/bin/env bash
# shellcheck shell=bash
# Fixture for the abandon, prune-rc and sync shell tests (ENG-1935). Source
# it after `set -euo pipefail`; it needs `tmp` (a temp dir the caller
# removes) and `scripts` (the scripts-release dir).
#
# tree_fixture_init
#   Two bare repos under $tmp stand in for ds-tegra-distro ($DISTRO_BARE,
#   trunk branch `trunk` with the release-line files, pinning meta-dsensos
#   $M2) and meta-dsensos ($META_BARE, `trunk` with two commits). $DISTRO is
#   a clone the scripts run in, $META_WORK a clone for making layer commits.
#   The stub gh and aws from this directory go first on PATH, with
#   GH_STUB_STATE, GH_STUB_REPOS, FAKE_S3, FAKE_RELEASES, AWS_CALLS set, and
#   the script environment (TOKEN, TOKEN_KIND=app, DRY_RUN=false,
#   META_DSENSOS_URL, DISTRO_REPO, META_REPO, GH_TOKEN) exported.
# seed_vertex                 create-branch.sh --seed-from trunk (vertex/main in both repos)
# open_line <N.0.0|N.M.0>     create-branch.sh --version (the tag must exist)
# tag_distro <tag> <ref>      an annotated tag pushed to the distro bare repo
# distro_commit <branch> <msg> <path>=<content>...   a commit pushed to a distro branch (prints the sha)
# meta_commit <branch> <msg> [<path>=<content>...]   the same in meta-dsensos
# head_of <bare> <branch>     the sha of a branch in a bare repo ("" when absent)
# heads_of <bare>             the branch names of a bare repo, one per line
# make_release <tag> <sha> <line> <version> <machine> <build_id>...
#   an rc pre-release of the distro repo in the stub (release-manifest.json
#   asset listing one build per build_id under <machine>/), the tag in the
#   bare repo, the S3 index copy and a per-build prefix with a few objects.
# gh_log / aws_log            the stub call logs

TREE_STUBS=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DISTRO_REPO=ds/distro
META_REPO=ds/meta

tgit() {
  GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@x GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@x git "$@"
}

tree_fixture_init() {
  DISTRO_BARE="${tmp}/distro.git"
  META_BARE="${tmp}/meta.git"
  META_WORK="${tmp}/meta-work"
  DISTRO="${tmp}/distro"
  STATE="${tmp}/gh-state"
  mkdir -p "${STATE}"
  export PATH="${TREE_STUBS}:${PATH}"
  export GH_STUB_STATE="${STATE}" GH_STUB_REPOS="${DISTRO_REPO}=${DISTRO_BARE},${META_REPO}=${META_BARE}"
  export FAKE_S3="${tmp}/s3" AWS_CALLS="${tmp}/aws.calls" DS_ARTIFACTS_BUCKET=ds-yocto-artifacts
  export TOKEN=stub-token TOKEN_KIND=app DRY_RUN=false GH_TOKEN=stub-token
  export META_DSENSOS_URL="${META_BARE}" DISTRO_REPO META_REPO
  export META_DSENSOS_HUMAN_URL="git@github.com:${META_REPO}.git"
  export GITHUB_SERVER_URL=https://github.com GITHUB_REPOSITORY="${DISTRO_REPO}" GITHUB_RUN_ID=4242
  unset GITHUB_STEP_SUMMARY
  BUCKET="${FAKE_S3}/${DS_ARTIFACTS_BUCKET}"
  mkdir -p "${BUCKET}"
  : > "${AWS_CALLS}"

  # meta-dsensos: trunk with two commits
  git init -q --bare -b trunk "${META_BARE}"
  git init -q -b trunk "${META_WORK}"
  echo m1 > "${META_WORK}/layer.txt"
  tgit -C "${META_WORK}" add -A && tgit -C "${META_WORK}" commit -q -m m1
  echo m2 > "${META_WORK}/layer.txt"
  tgit -C "${META_WORK}" add -A && tgit -C "${META_WORK}" commit -q -m m2
  M2=$(git -C "${META_WORK}" rev-parse HEAD)
  git -C "${META_WORK}" remote add origin "${META_BARE}"
  git -C "${META_WORK}" push -q origin trunk

  # ds-tegra-distro: trunk pinning m2
  git init -q --bare -b trunk "${DISTRO_BARE}"
  local seed="${tmp}/distro-seed"
  git init -q -b trunk "${seed}"
  mkdir -p "${seed}/kas/include" "${seed}/release" "${seed}/versions"
  cat > "${seed}/kas/include/base.yml" <<'EOF'
# test base.yml
header:
  version: 23
repos:
  ds-tegra-distro:
    path: .
  poky:
    url: https://git.yoctoproject.org/poky
    branch: scarthgap
    path: repos/poky
  meta-dsensos:
    url: git@github.com:DistributedSpectrum/meta-dsensos.git
    branch: trunk   # tracks the trunk
    path: repos/meta-dsensos
local_conf_header:
  00-sanity: |
    CONF_VERSION = "2"
EOF
  write_lock "${seed}/kas/include/base.lock.yml" "${M2}"
  echo '{"products": ["vertex", "aggregator"]}' > "${seed}/release/config.json"
  echo 0.0.1 > "${seed}/versions/vertex"
  echo 0.0.1 > "${seed}/versions/aggregator"
  echo '# versions' > "${seed}/versions/README.md"
  echo trunk > "${seed}/foo.txt"
  tgit -C "${seed}" add -A && tgit -C "${seed}" commit -q -m seed
  SEED_SHA=$(git -C "${seed}" rev-parse HEAD)
  git -C "${seed}" remote add origin "${DISTRO_BARE}"
  git -C "${seed}" push -q origin trunk
  git clone -q "${DISTRO_BARE}" "${DISTRO}"
}

write_lock() {
  cat > "$1" <<EOF
header:
  version: 14
overrides:
  repos:
    poky:
      commit: 7117d115eab7351ecf21388ec720a3bb5f4a9b30
    meta-dsensos:
      commit: $2
EOF
}

seed_vertex() {
  (cd "${DISTRO}" && "${scripts}/create-branch.sh" --product vertex --seed-from trunk > "${tmp}/seed.log" 2>&1) \
    || { cat "${tmp}/seed.log"; echo "fixture: seeding vertex/main failed" >&2; return 1; }
}

open_line() {
  (cd "${DISTRO}" && "${scripts}/create-branch.sh" --product vertex --version "$1" > "${tmp}/open.log" 2>&1) \
    || { cat "${tmp}/open.log"; echo "fixture: opening a line from $1 failed" >&2; return 1; }
}

tag_distro() {
  git -C "${DISTRO}" fetch -q origin
  tgit -C "${DISTRO}" tag -a -m "$1" "$1" "$2"
  git -C "${DISTRO}" push -q origin "refs/tags/$1"
}

# distro_commit <branch> <msg> <path>=<content>...  (content: \n for newlines)
distro_commit() {
  local branch=$1 msg=$2 kv
  shift 2
  git -C "${DISTRO}" fetch -q origin
  git -C "${DISTRO}" checkout -q -B "${branch}" "origin/${branch}"
  for kv in "$@"; do
    mkdir -p "$(dirname "${DISTRO}/${kv%%=*}")"
    printf '%b' "${kv#*=}" > "${DISTRO}/${kv%%=*}"
  done
  tgit -C "${DISTRO}" add -A
  tgit -C "${DISTRO}" commit -q -m "${msg}"
  git -C "${DISTRO}" push -q origin "${branch}"
  git -C "${DISTRO}" rev-parse HEAD
}

meta_commit() {
  local branch=$1 msg=$2 kv
  shift 2
  git -C "${META_WORK}" fetch -q origin
  git -C "${META_WORK}" checkout -q -B "${branch}" "origin/${branch}"
  for kv in "$@"; do
    printf '%b' "${kv#*=}" > "${META_WORK}/${kv%%=*}"
  done
  tgit -C "${META_WORK}" add -A
  tgit -C "${META_WORK}" commit -q --allow-empty -m "${msg}"
  git -C "${META_WORK}" push -q origin "${branch}"
  git -C "${META_WORK}" rev-parse HEAD
}

head_of() { git -C "$1" rev-parse --verify --quiet "refs/heads/$2^{commit}" 2>/dev/null || true; }
heads_of() { git -C "$1" for-each-ref --format='%(refname:short)' refs/heads/ | LC_ALL=C sort; }
tags_of() { git -C "$1" for-each-ref --format='%(refname:short)' refs/tags/ | LC_ALL=C sort; }

# make_release <tag> <sha> <line> <version> <machine> <build_id>...
make_release() {
  local tag=$1 sha=$2 line=$3 version=$4 machine=$5 sha7=${2:0:7}
  shift 5
  local rel="${STATE}/releases/${DISTRO_REPO//\//_}"
  local assets="${STATE}/assets/${tag//\//_}"
  mkdir -p "${rel}" "${assets}"
  local builds="" bid
  for bid in "$@"; do
    local prefix="${machine}/${bid}"
    mkdir -p "${BUCKET}/${prefix}/image-files" "${BUCKET}/${prefix}/deb"
    printf 'image bytes of %s' "${bid}" > "${BUCKET}/${prefix}/image-files/image.tar.gz"
    printf 'deb' > "${BUCKET}/${prefix}/deb/deb-feed.tar.gz"
    echo '{}' > "${BUCKET}/${prefix}/manifest.json"
    echo 'sums' > "${BUCKET}/${prefix}/SHA256SUMS"
    builds="${builds}{\"flavor\": \"vertex_${machine}_release\", \"machine\": \"${machine}\", \"hardened\": false, \"build_id\": \"${bid}\", \"s3_prefix\": \"${prefix}\", \"files\": {}},"
  done
  cat > "${assets}/release-manifest.json" <<EOF
{"schema": 1, "kind": "rc", "product": "vertex", "version": "${version}", "pending": "${version%%-*}", "sha": "${sha}",
 "line": "${line}", "run_id": "1", "run_url": "https://github.com/${DISTRO_REPO}/actions/runs/1",
 "builds": [${builds%,}]}
EOF
  cat > "${rel}/${tag//\//_}.json" <<EOF
{"tag_name": "${tag}", "draft": false, "assets": {"release-manifest.json": "${assets}/release-manifest.json"}}
EOF
  mkdir -p "${BUCKET}/rc/vertex/${sha7}"
  cp "${assets}/release-manifest.json" "${BUCKET}/rc/vertex/${sha7}/release-manifest.json"
  git -C "${DISTRO_BARE}" update-ref "refs/tags/${tag}" "${sha}"
}

gh_log() { cat "${STATE}/log" 2>/dev/null || true; }
aws_log() { cat "${AWS_CALLS}" 2>/dev/null || true; }
