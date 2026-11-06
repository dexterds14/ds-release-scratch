#!/usr/bin/env bash
set -euo pipefail

# Runs every scripts-release test, discovered by glob: the python unittest
# modules test_*.py (stdlib only; the version tests make throwaway git repos
# with git init) and the shell tests test_*.sh (aws and gh stubbed from
# tests/stubs on PATH). No network.
#   scripts-release/tests/run.sh [-v]      (-v goes to unittest)
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
cd "$HERE"
rc=0
python3 -m unittest discover -s "$HERE" -t "$HERE" -p 'test_*.py' "$@" || rc=1
for t in "$HERE"/test_*.sh; do
  [ -f "$t" ] || continue
  echo "== $(basename "$t")"
  bash "$t" || rc=1
done
if [ "$rc" = 0 ]; then echo "scripts-release tests: all passed"; else echo "scripts-release tests: FAILED"; fi
exit $rc
