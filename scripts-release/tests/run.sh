#!/usr/bin/env bash
set -euo pipefail

# Runs every scripts-release test, discovered by glob: the python unittest
# modules test_*.py (stdlib only; the version tests make throwaway git repos
# with git init) and the shell tests test_*.sh (aws and gh stubbed from
# tests/stubs on PATH). No network.
#   scripts-release/tests/run.sh [-v]      (-v goes to unittest)
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
cd "$HERE"
# The merge-up switches a caller's shell or CI job may carry must not reach the
# tests: each test that wants one sets it itself (ENG-2234, ENG-2235).
unset DS_MERGE_UP_AUTOMATION MERGE_UP_SUMMARY DEFAULT_BRANCH
unset DS_MERGE_UP_AUTO_FINISH AUTO_FINISH_SUMMARY AUTO_FINISH_TRIES AUTO_FINISH_RETRY_SECONDS HINT_PR HINT_HEAD
# The bot finish's wait (ENG-2235, 2c): no test may inherit one; a test that waits sets its own.
unset MERGE_UP_FINISH_WAIT_SECONDS MERGE_UP_FINISH_POLL_SECONDS
# Some tests run git as a person would (a plain merge, the commands a conflict
# PR's body gives), and git 2.53 and later refuse even `git merge --no-commit`
# without a committer identity. A developer machine has one in ~/.gitconfig, a
# CI runner has none. Give the run a fallback identity at the global-config
# level only, so every identity a test or script sets (environment, -c, repo
# config) still wins (ENG-2234, found on ds #631's first CI run, 2026-11-23).
if ! git config --get user.email >/dev/null 2>&1; then
  fallback_gitconfig=$(mktemp)
  trap 'rm -f "$fallback_gitconfig"' EXIT
  printf '[user]\n\tname = release-tests\n\temail = release-tests@example.invalid\n' > "$fallback_gitconfig"
  export GIT_CONFIG_GLOBAL="$fallback_gitconfig"
fi
rc=0
python3 -m unittest discover -s "$HERE" -t "$HERE" -p 'test_*.py' "$@" || rc=1
for t in "$HERE"/test_*.sh; do
  [ -f "$t" ] || continue
  echo "== $(basename "$t")"
  bash "$t" || rc=1
done
if [ "$rc" = 0 ]; then echo "scripts-release tests: all passed"; else echo "scripts-release tests: FAILED"; fi
exit $rc
