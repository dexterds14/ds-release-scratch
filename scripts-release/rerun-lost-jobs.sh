#!/usr/bin/env bash
set -euo pipefail
# rerun-lost-jobs.sh [--dry-run] [--attempt N] <run_id>  (ENG-2141)
#
# Classifies every failed job of one attempt of a workflow run and reruns
# the run's failed jobs (POST .../rerun-failed-jobs: passed jobs keep their
# results, failed jobs and their dependents run again as the next attempt)
# when, and only when, every failure is one the retry can fix:
#
#   LOST_RUNNER    the runner went away mid-job (a reclaimed Spot node): an
#                  annotation says "lost communication with the server", or a
#                  step is still in_progress and no step failed
#   WATCHDOG       Build Yocto was killed by scripts-ci/bitbake-watchdog.sh
#                  (annotation title bitbake-watchdog)
#   HUMAN_CANCEL   a person cancelled the run ("The run was canceled by @...")
#   BUILD_FAILURE  anything else: a step failed
#
# Jobs whose conclusion is neither failure nor cancelled, and cancelled
# dependents that never started (no steps), are ignored. The rerun happens
# when at least one job is in RERUN_CLASSES (default "LOST_RUNNER WATCHDOG")
# and no job is outside it, the run did not succeed, and the attempt is below
# MAX_ATTEMPTS (default 3). Everything it decides is printed as ::notice::
# lines. A correct no-op exits 0; only an API error exits 1.
#
# Called by release-rerun-lost.yml (workflow_run on the default branch, and
# workflow_dispatch for testing). Needs gh with actions:write and checks:read.
. "$(dirname "$(readlink -f "$0")")/lib.sh"

REPO=${GITHUB_REPOSITORY:-DistributedSpectrum/ds-tegra-distro}
MAX_ATTEMPTS=${MAX_ATTEMPTS:-3}
RERUN_CLASSES=${RERUN_CLASSES:-"LOST_RUNNER WATCHDOG"}

DRY_RUN=0
ATTEMPT=""
RUN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --attempt) ATTEMPT=$2; shift 2 ;;
    -h|--help) sed -n '3,25p' "$0"; exit 0 ;;
    -*) die "rerun-lost-jobs: unknown option $1" ;;
    *) [ -z "${RUN_ID}" ] || die "rerun-lost-jobs: one run id only"; RUN_ID=$1; shift ;;
  esac
done
[ -n "${RUN_ID}" ] || { echo "usage: rerun-lost-jobs.sh [--dry-run] [--attempt N] <run_id>" >&2; exit 4; }

run=$(gh api "repos/${REPO}/actions/runs/${RUN_ID}")
[ -n "${ATTEMPT}" ] || ATTEMPT=$(jq -r '.run_attempt' <<<"${run}")
conclusion=$(jq -r '.conclusion // "in_progress"' <<<"${run}")
url=$(jq -r '.html_url' <<<"${run}")
echo "run ${RUN_ID} attempt ${ATTEMPT}: ${conclusion} (${url})"

# A dry run classifies the attempt whatever the run's state, so an old attempt
# can be inspected after the run went green; a real run stops here.
stop=""
if [ "${conclusion}" = success ]; then
  stop="the run succeeded"
elif [ "${ATTEMPT}" -ge "${MAX_ATTEMPTS}" ]; then
  stop="attempt ${ATTEMPT} reached the cap of ${MAX_ATTEMPTS}"
fi
if [ -n "${stop}" ] && [ "${DRY_RUN}" = 0 ]; then
  note "rerun-lost-jobs: not rerunning: ${stop}"
  exit 0
fi

jobs=$(gh api --paginate "repos/${REPO}/actions/runs/${RUN_ID}/attempts/${ATTEMPT}/jobs" -q '.jobs[]' | jq -s '.')
n_lost=0; n_watchdog=0; n_other=0; n_seen=0
while IFS=$'\t' read -r job_id job_name job_conclusion n_steps n_failed n_inprog; do
  [ -n "${job_id}" ] || continue
  case "${job_conclusion}" in failure|cancelled) ;; *) continue ;; esac
  [ "${n_steps}" -gt 0 ] || continue   # a dependent that never started
  n_seen=$((n_seen + 1))
  ann=$(gh api "repos/${REPO}/check-runs/${job_id}/annotations" | jq -s 'flatten')
  class=BUILD_FAILURE
  if jq -e '.[] | select(.message | test("lost communication with the server"))' <<<"${ann}" >/dev/null; then
    class=LOST_RUNNER
  elif jq -e '.[] | select(.title == "bitbake-watchdog")' <<<"${ann}" >/dev/null; then
    class=WATCHDOG
  elif jq -e '.[] | select(.message | startswith("The run was canceled by"))' <<<"${ann}" >/dev/null; then
    class=HUMAN_CANCEL
  elif [ "${n_inprog}" -gt 0 ] && [ "${n_failed}" = 0 ]; then
    class=LOST_RUNNER
  fi
  note "rerun-lost-jobs: ${job_name}: ${class}"
  case " ${RERUN_CLASSES} " in
    *" ${class} "*)
      case "${class}" in LOST_RUNNER) n_lost=$((n_lost + 1)) ;; *) n_watchdog=$((n_watchdog + 1)) ;; esac ;;
    *) n_other=$((n_other + 1)) ;;
  esac
done < <(jq -r '.[] | [.id, .name, (.conclusion // ""), (.steps | length),
                        ([.steps[] | select(.conclusion == "failure")] | length),
                        ([.steps[] | select(.status == "in_progress")] | length)] | @tsv' <<<"${jobs}")

if [ "${n_seen}" = 0 ]; then
  note "rerun-lost-jobs: not rerunning: no failed job ran any step"
  exit 0
fi
if [ "${n_other}" -gt 0 ]; then
  note "rerun-lost-jobs: not rerunning: ${n_other} job(s) failed for a reason a rerun does not fix"
  exit 0
fi
if [ $((n_lost + n_watchdog)) = 0 ]; then
  note "rerun-lost-jobs: not rerunning: nothing to rerun"
  exit 0
fi

summary="${n_lost} lost to Spot, ${n_watchdog} killed by the watchdog"
if [ "${DRY_RUN}" = 1 ]; then
  if [ -n "${stop}" ]; then
    note "rerun-lost-jobs: dry run: would not rerun: ${stop} (${summary})"
  else
    note "rerun-lost-jobs: dry run: would POST repos/${REPO}/actions/runs/${RUN_ID}/rerun-failed-jobs (${summary})"
  fi
  exit 0
fi
err=$(mktemp); trap 'rm -f "${err}"' EXIT
if gh api -X POST "repos/${REPO}/actions/runs/${RUN_ID}/rerun-failed-jobs" >/dev/null 2>"${err}"; then
  note "rerun-lost-jobs: rerunning the failed jobs of ${url} as attempt $((ATTEMPT + 1)) (${summary})"
elif grep -q -i 'already running' "${err}"; then
  note "rerun-lost-jobs: not rerunning: the run is already running again (someone else reran it)"
else
  cat "${err}" >&2
  die "rerun-lost-jobs: rerun-failed-jobs failed for ${url}"
fi
