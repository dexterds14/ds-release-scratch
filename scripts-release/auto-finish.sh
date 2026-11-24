#!/usr/bin/env bash
set -euo pipefail

# The auto-finish sweep (ENG-2235): looks at every open merge-up PR of this
# repo and starts the finish of one that is ready. release-auto-finish.yml
# runs it on the default branch: when a review on a merge-up PR finishes the
# review trigger, when PR checks finish on a merge-up/** head, every hour, or
# by hand.
#
#   auto-finish.sh
#
# DS_MERGE_UP_AUTO_FINISH (the Actions variable) decides what it does:
# absent, empty or off: nothing is read and nothing is dispatched; shadow:
# every PR is read and the run logs "shadow, nothing changes: would dispatch
# ..."; on: the finish of a ready PR is dispatched,
#   gh workflow run release-merge-up.yml --ref <default> -f child=<child>
#     -f pr=<n> -f dry_run=false -f auto_finish=true
# and merge-up.sh --pr <n> --auto-finish checks the PR again and merges it.
# Any other value stops the run red before anything is read (auto_finish.py
# switch; case and surrounding spaces are ignored).
#
# The sweep only reads, apart from that one dispatch: it comments on no PR,
# merges nothing and pushes nothing. For each candidate (auto_finish.py
# candidates: an open same-repo PR, not a draft, head merge-up/<child>/<sha7>)
# it checks the lines as release-merge-up.yml's resolve job does (the child is
# a minor or patch line, and the PR's base is its parent line), reads the
# view, the REST reviews, the branch activity, the permission of every
# approver, the PR's comments, the recent finish runs, and the check runs,
# Actions runs and jobs of the head (a required check counts only from the
# source merge_up check_sources names for it), every PR of the head branch and
# the PR's events (no run counts while another PR of the branch has another
# base, or the same base but was not opened by the release App or was ever
# retargeted, whose events are read too, or the PR's base was ever changed),
# and asks auto_finish.py ready. A PR that is not ready is one line with its reason codes, and the
# run stays green. UNKNOWN, and UNSTABLE while only the review
# trigger runs, are read again (AUTO_FINISH_TRIES times,
# AUTO_FINISH_RETRY_SECONDS apart; 3 and 5).
#
# At most one dispatch per run, and none while the release-line group is
# busy: a queued or running release-merge-up, release-promote,
# release-abandon or release-create-branch run (on any ref). A PR whose
# auto-finish run is still queued or running, or whose last three
# auto-finish runs for this head and approval failed without a stop comment,
# is not ready.
#
# The run is red only for a bad switch value, a run on another ref than the
# default branch (DEFAULT_BRANCH unset included), a PR list that cannot be
# read, a config that cannot be read, or a dispatch that fails. A branch list
# of origin that cannot be read is one line, nothing dispatched, and the run
# stays green.
#
# Environment: GH_TOKEN (actions: write for the dispatch), GITHUB_REPOSITORY
# (this repo, which every read names with -R), GH_REPO (the repo the one
# dispatch goes to: `gh workflow run` carries no -R, so gh takes GH_REPO, or
# without it the checkout's git remote; release-auto-finish.yml sets it to
# this repo), DEFAULT_BRANCH and GITHUB_REF_NAME,
# DS_MERGE_UP_AUTO_FINISH, AUTO_FINISH_SUMMARY (the step summary), EVENT and
# the hints HINT_PR and HINT_HEAD (only which PR is looked at first;
# everything is read again).

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
# GIT_BOT_NAME: the release App's login, the only account whose stop comments
# count and whose pushes are set aside.
. "$SCRIPTS/lib.sh"

OWN_REPO="${GITHUB_REPOSITORY:-}"
EVENT="${EVENT:-${GITHUB_EVENT_NAME:-a manual run}}"
HINT_PR="${HINT_PR:-}"
HINT_HEAD="${HINT_HEAD:-}"
TRIES="${AUTO_FINISH_TRIES:-3}"
RETRY_SECONDS="${AUTO_FINISH_RETRY_SECONDS:-5}"
RUN_REF="${GITHUB_REF_NAME:-}"
# The workflows of the release-line concurrency group.
GROUP=(release-merge-up.yml release-promote.yml release-abandon.yml release-create-branch.yml)
FINISH_WORKFLOW=release-merge-up.yml

die() {
  echo "::error::auto-finish: $*"
  exit 1
}

# summary <line>: one more line for the run summary.
summary() {
  [ -z "${AUTO_FINISH_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$AUTO_FINISH_SUMMARY"
}

# say <line>: one line of the log, on stdout and in the run summary.
say() {
  echo "auto-finish: $*"
  summary "- $*"
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---- the switch and the ref, before anything is read ----------------------------

MODE=$(python3 "$SCRIPTS/auto_finish.py" switch 2>"$TMP/switch.err") \
  || die "$(sed 's/^auto_finish.py: //' "$TMP/switch.err"); nothing was read or dispatched. Set the variable to off, shadow or on, or delete it"
if [ "$MODE" = off ]; then
  echo "auto-finish: DS_MERGE_UP_AUTO_FINISH is off; nothing was read or dispatched"
  exit 0
fi
[ -n "$OWN_REPO" ] || die "GITHUB_REPOSITORY is not set (the owner/name gh acts on); nothing was read or dispatched"
[ -n "${DEFAULT_BRANCH:-}" ] || die "DEFAULT_BRANCH is not set, so this run cannot tell whether it runs on the default branch; nothing was read or dispatched"
[ "$RUN_REF" = "$DEFAULT_BRANCH" ] \
  || die "runs on the default branch ($DEFAULT_BRANCH) only, not on ${RUN_REF:-an unnamed ref}: the finish it starts runs the default branch's scripts, and so must the check that starts it. Nothing was read or dispatched"

SCRIPTS_SHA=$(git -C "$(dirname "$SCRIPTS")" rev-parse --short=7 HEAD 2>/dev/null) || SCRIPTS_SHA=unknown
case "$MODE" in
  shadow) MODE_TEXT="shadow (logs what it would finish, changes nothing)" ;;
  on) MODE_TEXT="on (dispatches the finish of a ready PR, one per run)" ;;
esac
TRIGGER="$EVENT"
[ -z "$HINT_PR" ] || TRIGGER="$TRIGGER, looking first at PR #$HINT_PR"
[ -z "$HINT_HEAD" ] || TRIGGER="$TRIGGER, looking first at $HINT_HEAD"
{
  echo "== merge-up auto-finish"
  echo "variable:           DS_MERGE_UP_AUTO_FINISH=${DS_MERGE_UP_AUTO_FINISH:-}"
  echo "mode:               $MODE_TEXT"
  echo "trigger:            $TRIGGER"
  echo "scripts:            $DEFAULT_BRANCH at $SCRIPTS_SHA (the default branch)"
} > "$TMP/block.txt"
cat "$TMP/block.txt"
if [ -n "${AUTO_FINISH_SUMMARY:-}" ]; then
  { echo "### Merge-up auto-finish"; echo; echo '```'; cat "$TMP/block.txt"; echo '```'; } >> "$AUTO_FINISH_SUMMARY"
fi

REQUIRED=$(python3 "$SCRIPTS/release_git.py" merge-up-config --key finish.required_checks | python3 -I -c 'import json,sys; print(",".join(json.load(sys.stdin)))') \
  || die "release/config.json: merge_up.finish.required_checks could not be read; nothing was dispatched"
# Where each required check must come from (merge_up check_sources): the auto-finish counts a
# required check only from its source.
python3 "$SCRIPTS/release_git.py" merge-up-config --key finish.check_sources > "$TMP/sources.json" 2>/dev/null \
  || die "release/config.json: merge_up.finish.check_sources could not be read; nothing was dispatched"
# The branches decide each PR's parent line. A list that fails is a read GitHub may answer at
# the next run: one line, nothing dispatched, and the run stays green.
if ! git ls-remote --heads origin > "$TMP/heads" 2>"$TMP/heads.err"; then
  say "the branches of origin could not be listed ($(head -1 "$TMP/heads.err")), so no PR's parent line can be checked; nothing was dispatched, and the next event or sweep looks again"
  exit 0
fi

# ---- the open pull requests ------------------------------------------------------

gh pr list -R "$OWN_REPO" --state open --limit 1000 --json number,headRefName,baseRefName,headRefOid,isCrossRepository,isDraft > "$TMP/prs.json" 2>"$TMP/prs.err" \
  || die "the open pull requests of $OWN_REPO could not be listed ($(head -1 "$TMP/prs.err")); nothing was dispatched"
# The hinted PR first, then by number.
python3 "$SCRIPTS/auto_finish.py" candidates "$TMP/prs.json" --hint-pr "$HINT_PR" --hint-head "$HINT_HEAD" > "$TMP/candidates"
grep '^candidate ' "$TMP/candidates" > "$TMP/order" || true
while read -r _ n why; do
  say "$OWN_REPO#$n: skipped: $why"
done < <(grep '^skipped ' "$TMP/candidates" || true)

# The finish runs (newest first, one page), read once: a finish of a PR still
# queued or running, and the failed ones, are reasons of ready.
RUNS_ARG=()
if gh api "repos/$OWN_REPO/actions/workflows/$FINISH_WORKFLOW/runs?event=workflow_dispatch&per_page=100" > "$TMP/finish-runs.json" 2>"$TMP/runs.err"; then
  RUNS_ARG=(--runs "$TMP/finish-runs.json")
else
  RUNS_UNREAD="the runs of $FINISH_WORKFLOW could not be read ($(head -1 "$TMP/runs.err")), so a finish already queued or running cannot be ruled out"
fi

# busy_group: whether a run of the release-line group is active; the reasons in $TMP/busy.
BUSY_READ=""
busy_group() {
  if [ -z "$BUSY_READ" ]; then
    BUSY_READ=1
    : > "$TMP/active-runs.json"
    local s
    for s in queued in_progress waiting requested pending; do
      if ! gh api --paginate "repos/$OWN_REPO/actions/runs?status=$s&per_page=100" >> "$TMP/active-runs.json" 2>"$TMP/active.err"; then
        echo "the active runs ($s) could not be read: $(head -1 "$TMP/active.err")" > "$TMP/busy"
        return 0
      fi
      echo >> "$TMP/active-runs.json"
    done
    local wf=()
    for s in "${GROUP[@]}"; do wf+=(--workflow "$s"); done
    python3 "$SCRIPTS/auto_finish.py" busy "$TMP/active-runs.json" "${wf[@]}" > "$TMP/busy" 2>&1 \
      || echo "the active runs could not be read: $(tail -1 "$TMP/busy")" > "$TMP/busy"
  fi
  [ -s "$TMP/busy" ]
}

# ---- each candidate --------------------------------------------------------------

LOOKED=0 READY=0 NOT_READY=0 SKIPPED=0 DISPATCHED="" BUSY=""
while read -r -u 3 _ n child base head branch; do
  label="$OWN_REPO#$n $child -> $base at ${head:0:7}"
  LOOKED=$((LOOKED + 1))
  # The lines, as release-merge-up.yml's resolve job checks them.
  kind=$(python3 "$SCRIPTS/lines.py" --root "$REPO" parse "$child" 2>/dev/null | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["kind"])' 2>/dev/null || echo none)
  case "$kind" in
    minor|patch) ;;
    *) say "$label: skipped: $child is not a release line of a configured product"; SKIPPED=$((SKIPPED + 1)); continue ;;
  esac
  want=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$TMP/heads" parent "$child" 2>/dev/null || true)
  if [ "$want" != "$base" ]; then
    say "$label: skipped: base $base is not the parent line of $child (${want:-none})"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi
  extra=()
  [ -z "${RUNS_UNREAD:-}" ] || extra+=(--extra "runs-unreadable: $RUNS_UNREAD")
  # Everything ready reads.
  if ! gh api --paginate "repos/$OWN_REPO/pulls/$n/reviews" > "$TMP/reviews.json" 2>"$TMP/read.err"; then
    say "$label: not looked at: its reviews could not be read ($(head -1 "$TMP/read.err"))"; NOT_READY=$((NOT_READY + 1)); continue
  fi
  activity=()
  if gh api --paginate -X GET "repos/$OWN_REPO/activity" -f "ref=refs/heads/$branch" > "$TMP/activity.json" 2>/dev/null; then
    activity=(--activity "$TMP/activity.json")
  fi
  : > "$TMP/permissions.txt"
  for login in $(python3 "$SCRIPTS/auto_finish.py" approvers --reviews "$TMP/reviews.json"); do
    [[ "$login" =~ ^[A-Za-z0-9-]+$ ]] || continue
    if perm=$(gh api "repos/$OWN_REPO/collaborators/$login/permission" --jq .permission 2>/dev/null); then
      printf '%s %s\n' "$login" "$perm" >> "$TMP/permissions.txt"
    fi
  done
  comments=()
  if gh api --paginate "repos/$OWN_REPO/issues/$n/comments" > "$TMP/comments.json" 2>/dev/null; then
    comments=(--comments "$TMP/comments.json")
  fi
  # Where each required check came from: the check runs, the Actions runs and the jobs of the
  # head (checks: read, actions: read). A read that fails leaves them out: not ready, no mark.
  sources=()
  rm -rf "$TMP/head-jobs"
  mkdir -p "$TMP/head-jobs"
  if gh api --paginate "repos/$OWN_REPO/commits/$head/check-runs?per_page=100" > "$TMP/check-runs.json" 2>/dev/null \
     && gh api --paginate "repos/$OWN_REPO/actions/runs?head_sha=$head&per_page=100" > "$TMP/head-runs.json" 2>/dev/null; then
    sources=(--check-runs "$TMP/check-runs.json" --actions-runs "$TMP/head-runs.json")
    for rid in $(python3 "$SCRIPTS/auto_finish.py" source-runs --required "$REQUIRED" --sources "$TMP/sources.json" --head-ref "$branch" \
                   --head "$head" --pr "$n" --base "$base" --repo "$OWN_REPO" "${sources[@]}" 2>/dev/null || true); do
      [[ "$rid" =~ ^[0-9]+$ ]] || continue
      gh api --paginate "repos/$OWN_REPO/actions/runs/$rid/jobs?filter=all&per_page=100" > "$TMP/head-jobs/$rid.json" 2>/dev/null \
        || rm -f "$TMP/head-jobs/$rid.json"
    done
  fi
  # GitHub lists every open PR of the head branch in each run of the branch, whichever PR's
  # event started it: another PR of the branch into another base, or a base that was ever
  # changed, leaves no run this PR's alone (auto_finish.py head_branch_prs). Every PR of the
  # branch and this PR's events; a read that fails leaves them out: not ready, no mark. Another
  # PR of the branch into the same base counts only when the release App opened it and its own
  # events hold no base change: those events are read too (usually none or one PR), and a read
  # that fails leaves that PR's file out, which is the same.
  rm -rf "$TMP/other-events"
  mkdir -p "$TMP/other-events"
  if gh api --paginate "repos/$OWN_REPO/pulls?state=all&head=${OWN_REPO%%/*}:$branch&per_page=100" > "$TMP/head-prs.json" 2>/dev/null; then
    sources+=(--head-prs "$TMP/head-prs.json")
    for m in $(python3 "$SCRIPTS/auto_finish.py" same-base-prs "$TMP/head-prs.json" --pr "$n" --base "$base" --head-ref "$branch" \
                 --release-app "$GIT_BOT_NAME" 2>/dev/null || true); do
      [[ "$m" =~ ^[0-9]+$ ]] || continue
      gh api --paginate "repos/$OWN_REPO/issues/$m/events?per_page=100" > "$TMP/other-events/$m.json" 2>/dev/null \
        || rm -f "$TMP/other-events/$m.json"
    done
  fi
  sources+=(--other-events "$TMP/other-events")
  if gh api --paginate "repos/$OWN_REPO/issues/$n/events?per_page=100" > "$TMP/events.json" 2>/dev/null; then
    sources+=(--events "$TMP/events.json")
  fi
  # A workflow's run vouches only when it is a run for this PR: its number, this base, this repo.
  sources+=(--sources "$TMP/sources.json" --head-ref "$branch" --base "$base" --jobs-dir "$TMP/head-jobs")
  status=""
  for try in $(seq 1 "$TRIES"); do
    if ! gh pr view "$n" -R "$OWN_REPO" --json number,state,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup,commits,headRefOid \
           > "$TMP/view.json" 2>"$TMP/read.err"; then
      status=unread
      break
    fi
    if ! python3 "$SCRIPTS/auto_finish.py" ready "$TMP/view.json" --reviews "$TMP/reviews.json" --required "$REQUIRED" \
           --release-app "$GIT_BOT_NAME" --permissions "$TMP/permissions.txt" --repo "$OWN_REPO" \
           "${sources[@]}" ${activity[@]+"${activity[@]}"} ${comments[@]+"${comments[@]}"} ${RUNS_ARG[@]+"${RUNS_ARG[@]}"} ${extra[@]+"${extra[@]}"} \
           > "$TMP/ready.txt" 2>"$TMP/read.err"; then
      status=unread
      break
    fi
    status=$(head -1 "$TMP/ready.txt")
    [ "$status" = retry ] && [ "$try" -lt "$TRIES" ] || break
    sleep "$RETRY_SECONDS"
  done
  if [ "$status" = unread ]; then
    say "$label: not looked at: $(grep -v '^auto_finish.py: note: ' "$TMP/read.err" | head -1)"
    NOT_READY=$((NOT_READY + 1))
    continue
  fi
  if [ "$status" != ready ]; then
    reasons=$(tail -n +2 "$TMP/ready.txt" | sed 's/^\([a-z-]*\): \(.*\)$/\1 (\2)/' | paste -sd ';' | sed 's/;/; /g')
    say "$label: not ready: $reasons"
    NOT_READY=$((NOT_READY + 1))
    continue
  fi
  READY=$((READY + 1))
  approvers=$(sed -n 's/^approvers: //p' "$TMP/ready.txt")
  say "$label: ready: approved by $approvers after the last push, checks green"
  if [ -n "$DISPATCHED" ]; then
    say "$OWN_REPO#$n: deferred: this run already started the finish of $DISPATCHED; the next event or sweep takes it"
    continue
  fi
  if busy_group; then
    say "$OWN_REPO#$n: deferred: the release-line group is busy ($(paste -sd ';' "$TMP/busy" | sed 's/;/; /g')); the next event or sweep takes it"
    BUSY=1
    continue
  fi
  if [ "$MODE" = shadow ]; then
    say "$OWN_REPO#$n: shadow, nothing changes: would dispatch $FINISH_WORKFLOW on $DEFAULT_BRANCH with child=$child pr=$n dry_run=false auto_finish=true"
  else
    out=$(gh workflow run "$FINISH_WORKFLOW" --ref "$DEFAULT_BRANCH" -f child="$child" -f pr="$n" -f dry_run=false -f auto_finish=true 2>&1) \
      || die "$OWN_REPO#$n: could not dispatch $FINISH_WORKFLOW on $DEFAULT_BRANCH with child=$child pr=$n auto_finish=true: $(echo "$out" | tail -1)"
    say "$OWN_REPO#$n: dispatched $FINISH_WORKFLOW on $DEFAULT_BRANCH with child=$child pr=$n dry_run=false auto_finish=true ($(echo "$out" | grep -o 'https://[^ ]*' | tail -1 || true))"
  fi
  DISPATCHED="$OWN_REPO#$n"
done 3< "$TMP/order"

if [ -z "$DISPATCHED" ]; then
  started=none
  [ -z "$BUSY" ] || started="none (the release-line group is busy)"
elif [ "$MODE" = shadow ]; then
  started="none (shadow; it would have started the finish of $DISPATCHED)"
else
  started="the finish of $DISPATCHED"
fi
say "$LOOKED open merge-up PR(s) looked at: $READY ready, $NOT_READY not ready, $SKIPPED skipped; started $started"
