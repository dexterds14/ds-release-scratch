#!/usr/bin/env bash
set -euo pipefail

# Prologue of every release job (ENG-1930): print the versions of the tools
# the scripts use and fail if one is missing or too old.
#   gh >= 2.40   (gh run list --commit; the runners ship 2.45)
#   git >= 2.38  (merge-up and create-branch rely on current merge/push flags)
# jq, python3 and aws only have to exist.

MIN_GH=2.40
MIN_GIT=2.38
rc=0

fail() {
  echo "::error::check-tools: $*"
  rc=1
}

# at_least <have> <min>: version compare by sort -V
at_least() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

for tool in gh git jq python3 aws; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    fail "$tool is not installed"
  fi
done
[ $rc = 0 ] || exit $rc

GH_VERSION=$(gh --version | sed -n 's/^gh version \([0-9][0-9.]*\).*/\1/p' | head -1)
GIT_VERSION=$(git --version | sed -n 's/^git version \([0-9][0-9.]*\).*/\1/p')
echo "gh ${GH_VERSION}"
echo "git ${GIT_VERSION}"
echo "jq $(jq --version)"
echo "python3 $(python3 --version 2>&1 | awk '{print $2}')"
echo "aws $(aws --version 2>&1 | awk '{print $1}')"

at_least "${GH_VERSION}" "${MIN_GH}" || fail "gh ${GH_VERSION} is older than ${MIN_GH}"
at_least "${GIT_VERSION}" "${MIN_GIT}" || fail "git ${GIT_VERSION} is older than ${MIN_GIT}"
exit $rc
