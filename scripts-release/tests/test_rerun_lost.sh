#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/rerun-lost-jobs.sh (ENG-2141): gh is the stub in
# tests/stubs answering from fixtures trimmed from ds-tegra-distro run
# 40913705305 attempt 1 (jackson_release lost to Spot, a603 hardened cancelled
# by a person). Every other case is that attempt with one job edited. No
# network.
#   bash scripts-release/tests/test_rerun_lost.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
fixtures="${here}/fixtures/rerun-lost"
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -F -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -F -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; fails=$((fails + 1)); fi; }

REPO=DistributedSpectrum/ds-tegra-distro
RUN=40913705305
HARDENED=123589522129   # a603-8gb release-hardened: cancelled by a person
JACKSON=123589522209    # jackson release: runner lost

# api_dir <name>: a fresh FAKE_API_DIR holding the real attempt 1; prints it.
api_dir() {
  local d="${tmp}/$1"
  mkdir -p "${d}"
  cp "${fixtures}/run.json" "${d}/repos_${REPO//\//_}_actions_runs_${RUN}.json"
  cp "${fixtures}/attempt1-jobs.json" "${d}/repos_${REPO//\//_}_actions_runs_${RUN}_attempts_1_jobs.json"
  for j in ${HARDENED} ${JACKSON}; do
    cp "${fixtures}/annotations-${j}.json" "${d}/repos_${REPO//\//_}_check-runs_${j}_annotations.json"
  done
  echo "${d}"
}
# edit_job <dir> <job id> <jq expression over the job>
edit_job() {
  local f="$1/repos_${REPO//\//_}_actions_runs_${RUN}_attempts_1_jobs.json"
  jq --argjson id "$2" "(.jobs[] | select(.id == \$id)) |= ($3)" "${f}" > "${f}.new" && mv "${f}.new" "${f}"
}
# set_annotations <dir> <job id> <json array>
set_annotations() { echo "$3" > "$1/repos_${REPO//\//_}_check-runs_$2_annotations.json"; }
# set_run <dir> <jq expression over the run>
set_run() {
  local f="$1/repos_${REPO//\//_}_actions_runs_${RUN}.json"
  jq "$2" "${f}" > "${f}.new" && mv "${f}.new" "${f}"
}

# run_it <dir> <args...> -> $rc, $log, $calls
run_it() {
  local d=$1; shift
  log="${tmp}/log"; calls="${tmp}/calls"; : > "${calls}"
  rc=0
  PATH="${here}/stubs:${PATH}" FAKE_API_DIR="${d}" GH_CALLS="${calls}" GITHUB_REPOSITORY="${REPO}" \
    "${scripts}/rerun-lost-jobs.sh" "$@" > "${log}" 2>&1 || rc=$?
}
posted() { grep -c -- "-X POST repos/${REPO}/actions/runs/${RUN}/rerun-failed-jobs" "${calls}" || true; }

HARDENED_OK='.status = "completed" | .conclusion = "success" | .steps |= map(.status = "completed" | .conclusion = "success")'
HARDENED_FAILED_BUILD='.status = "completed" | .conclusion = "failure" | .steps |= map(.status = "completed" | .conclusion = (if .name == "Build Yocto" then "failure" else "success" end))'

echo "# the real attempt 1: one lost runner, one human cancel"
d=$(api_dir real); run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::rerun-lost-jobs: build (vertex_jetson-orin-nx-jackson_release) / build: LOST_RUNNER" "jackson is LOST_RUNNER"
contains "${log}" "::notice::rerun-lost-jobs: build (vertex_jetson-orin-nx-a603-8gb_release-hardened) / build: HUMAN_CANCEL" "the hardened build is HUMAN_CANCEL"
lacks "${log}" "rc-gate" "a cancelled dependent with no steps is ignored"
contains "${log}" "not rerunning: 1 job(s) failed for a reason a rerun does not fix" "the human cancel blocks the rerun"
check "$(posted)" 0 "no POST"

echo "# lost runner only: rerun"
d=$(api_dir lost); edit_job "${d}" ${HARDENED} "${HARDENED_OK}"; run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
check "$(posted)" 1 "one POST to rerun-failed-jobs"
contains "${log}" "rerunning the failed jobs of https://github.com/${REPO}/actions/runs/${RUN} as attempt 2 (1 lost to Spot, 0 killed by the watchdog)" "names the next attempt and the counts"

echo "# lost runner only, --dry-run: no POST"
run_it "${d}" --dry-run ${RUN}
check "${rc}" 0 "exits 0"
check "$(posted)" 0 "no POST"
contains "${log}" "dry run: would POST repos/${REPO}/actions/runs/${RUN}/rerun-failed-jobs" "says what it would do"

echo "# --attempt picks the attempt to classify"
run_it "${d}" --dry-run --attempt 1 ${RUN}
contains "${log}" "run ${RUN} attempt 1: failure" "attempt 1"
run_it "${d}" --dry-run --attempt 2 ${RUN}
check "${rc}" 1 "attempt 2 has no fixture: the API error is an error"
contains "${log}" "HTTP 404" "and is printed"

echo "# a watchdog kill is rerun too"
d=$(api_dir watchdog); edit_job "${d}" ${HARDENED} "${HARDENED_FAILED_BUILD}"
set_annotations "${d}" ${HARDENED} '[{"title":"bitbake-watchdog","annotation_level":"failure","message":"Build Yocto hung: no bitbake events for 1200s and no active task; stuck phase: \"Checking sstate mirror object availability...\" (ENG-2141, killed by scripts-ci/bitbake-watchdog.sh)"}]'
run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "release-hardened) / build: WATCHDOG" "the hardened build is WATCHDOG"
check "$(posted)" 1 "one POST"
contains "${log}" "(1 lost to Spot, 1 killed by the watchdog)" "counts both"

echo "# RERUN_CLASSES narrows what is rerun"
RERUN_CLASSES="LOST_RUNNER" run_it "${d}" ${RUN}
check "$(posted)" 0 "no POST when WATCHDOG is not in RERUN_CLASSES"
contains "${log}" "not rerunning: 1 job(s) failed for a reason a rerun does not fix" "and says why"

echo "# a real build failure blocks the rerun"
d=$(api_dir buildfail); edit_job "${d}" ${HARDENED} "${HARDENED_FAILED_BUILD}"
set_annotations "${d}" ${HARDENED} '[{"title":"","annotation_level":"failure","message":"Process completed with exit code 1."}]'
run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "release-hardened) / build: BUILD_FAILURE" "the hardened build is BUILD_FAILURE"
check "$(posted)" 0 "no POST"

echo "# the attempt cap"
d=$(api_dir cap); edit_job "${d}" ${HARDENED} "${HARDENED_OK}"; set_run "${d}" '.run_attempt = 3'
run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "not rerunning: attempt 3 reached the cap of 3" "stops at the cap"
check "$(posted)" 0 "no POST"
MAX_ATTEMPTS=5 run_it "${d}" ${RUN}
check "$(posted)" 0 "attempt 3 of 5 still needs the attempt-3 jobs fixture, which is absent: API error, no POST"
check "${rc}" 1 "exits 1 on the API error"

echo "# a green run is never rerun"
d=$(api_dir green); edit_job "${d}" ${HARDENED} "${HARDENED_OK}"; set_run "${d}" '.conclusion = "success"'
run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "not rerunning: the run succeeded" "says so"
check "$(posted)" 0 "no POST"
lacks "${log}" "LOST_RUNNER" "and does not classify"
run_it "${d}" --dry-run --attempt 1 ${RUN}
contains "${log}" "jackson_release) / build: LOST_RUNNER" "a dry run still classifies an old attempt of a green run"
contains "${log}" "dry run: would not rerun: the run succeeded (1 lost to Spot, 0 killed by the watchdog)" "and says why it would not rerun"
check "$(posted)" 0 "no POST"

echo "# the POST is refused because the run is already running again"
d=$(api_dir running); edit_job "${d}" ${HARDENED} "${HARDENED_OK}"
FAKE_GH_POST_FAIL="HTTP 403: This workflow is already running (https://docs.github.com/rest/actions/workflow-runs#re-run-failed-jobs-from-a-workflow-run)" run_it "${d}" ${RUN}
check "${rc}" 0 "exits 0"
contains "${log}" "not rerunning: the run is already running again" "treated as a notice"

echo "# any other POST failure is an error"
FAKE_GH_POST_FAIL="HTTP 500: boom" run_it "${d}" ${RUN}
check "${rc}" 1 "exits 1"
contains "${log}" "::error::rerun-lost-jobs: rerun-failed-jobs failed" "with an error annotation"

if [ "${fails}" = 0 ]; then echo "test_rerun_lost: all passed"; else echo "test_rerun_lost: ${fails} FAILED"; exit 1; fi
