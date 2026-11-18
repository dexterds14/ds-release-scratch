#!/usr/bin/env bash
set -euo pipefail

# Merge a release line into its parent line in both repos (ENG-1935):
# meta-dsensos first, then ds-tegra-distro. Run from a ds-tegra-distro
# checkout whose origin is the repo (fetch-depth 0, push credentials set).
#
#   merge-up.sh --child <product>/<line> [--parent <product>/<line>] [--pr N] [--dry-run|--no-dry-run]
#
# 1. The child tip must have a green release-rc-build run (gh run list
#    --commit <sha> --branch <child> --status success); a trailing commit
#    that touches only versions/ is walked past. Started by the tip's own
#    green rc-build run (release-merge-up.yml on workflow_run: CALLER_SHA is
#    the tip and CALLER_RUN that run) the tip counts as green without the
#    lookup.
# 2. meta-dsensos: if the meta-dsensos commit the child's base.lock.yml pins
#    is not already in the parent's meta-dsensos line, merge meta-dsensos
#    <child> into <parent> (--no-ff) and push. On conflict: commit the merge
#    as it stands, conflict markers and all, push it as
#    merge-up/<child>/<sha7>, open a PR against <parent> in meta-dsensos
#    listing the files, ::error:: with the link, exit 1 (ENG-2200: the PR
#    diff is then the parent-relative diff, and the markers are the only
#    lines to decide; the resolver edits the files on that branch).
# 3. ds-tegra-distro: git merge --no-ff --no-commit origin/<child> into
#    <parent>, then `release_git.py merge-keep-target <parent>`: the target
#    keeps its pending-version files (versions/README.md merges like any
#    doc), and base.yml
#    merges three-way with the meta-dsensos branch of every side set to the
#    parent's first, so the parent keeps its branch name and every other
#    base.yml change (a new layer stanza) still arrives. Everything else, the
#    lockfile included, merges normally. Any conflict, base.yml included:
#    commit the merge with its markers (versions/ and the branch name already
#    kept), push it as merge-up/<child>/<sha7>, open the conflict PR against
#    <parent>, ::error::, exit 1. A branch that already exists on origin is
#    left alone (a resolver may have pushed to it) and its open PR reused.
#    Unless its first commit above the parent is the bot's merge with the
#    tree this run computed, the run and the body of a PR it opens say the
#    branch was not produced by this run; no rule and no bot finish applies.
#    Else commit "Merge <child> into <parent>" and push (3 attempts on
#    non-fast-forward).
# 4. Post-check: the parent tip's second parent is the child tip (or the
#    finished PR's head, whose own second parent is the child tip) and the
#    child tip is an ancestor of the parent.
# Supersession (ENG-2233): every outcome in a repo (a new conflict PR, a clean
#    merge, a finished PR, nothing to merge) closes the other open
#    merge-up/<child>/* PRs against <parent> there, with a comment naming what
#    replaced them; their branches stay. Both merges use zdiff3 markers.
# --pr N finishes a conflict PR (in either repo): it must be APPROVED by
#    a person other than its last pusher (the reviews are read through the
#    REST API and only user.type "User" counts: a bot's or an App's approval
#    never finishes a PR; with merge_up.finish.strict the approval must be
#    on the PR head, by someone who pushed nothing to the branch according to
#    the repository activity, and every check of
#    merge_up.finish.required_checks must be green on the head),
#    mergeStateStatus CLEAN, no failed
#    check, no conflict marker (<<<<<<<, ||||||| or >>>>>>>) left in the files
#    it changes, and its head must contain the child's last green commit (a
#    stale PR is refused before anything merges; a versions-only bump on top
#    does not count); then gh pr merge --merge --match-head-commit, the
#    post-check, and the remaining steps. A PR that GitHub still shows open
#    after that merge is closed with a comment naming the merge commit. A
#    conflict that leaves no text
#    marker (binary, modify/delete) opens the PR on the child tip instead, and
#    an open merge-up PR whose head already contains the child is reused.
#
# Environment: TOKEN, TOKEN_KIND (app|github), META_DSENSOS_URL,
# META_DSENSOS_HUMAN_URL, DISTRO_REPO, META_REPO (owner/name for gh),
# DRY_RUN, GIT_BOT_NAME, GIT_BOT_EMAIL: as in create-branch.sh; CALLER_SHA
# and CALLER_RUN ("<run id> <run number>", empty on a dispatch): the green
# rc-build run that started this merge-up, see 1. gh needs GH_TOKEN.
# DS_MERGE_UP_AUTOMATION (the Actions variable, ENG-2234): which merge-up rules
# evaluate and which act (release_git.py automation); a value that does not
# parse stops the run before anything is read or pushed. Every run prints the
# variable, the active rules, the ref whose scripts and release/config.json
# are in use and the config switches, and appends the same block to the file
# MERGE_UP_SUMMARY names (the workflow passes its step summary).
# DEFAULT_BRANCH, when set, says whether that ref is the default branch.
# Every finish instruction the run prints (the conflict PR body, the ::error::
# that names the PR) says to dispatch on the default branch, by name when
# DEFAULT_BRANCH is set; a --pr run on another ref warns.
# MERGE_UP_READ_TOKEN: the read-only App token for ds and sensor-portal that
# the pins rule will use; only its presence is reported here.
# R-lock, the lockfile pin rule (ENG-2234; rule name lock-pin in the variable):
#    when kas/include/base.lock.yml is still unmerged after merge-keep-target
#    (both lines moved the meta-dsensos pin), the parent's lock belongs on the
#    parent line's meta-dsensos tip, which step 2 produced or found and which
#    contains both pins. `release_git.py lock-rule` runs the checks read-only:
#    1 the tip is a real pushed commit (a dry-run tip is planned and never used
#    as a value), 2 both pins are ancestors of it, 3 nothing else in the
#    lockfile conflicts, 4 the result parses back to the tip, 5 the tip's tree
#    equals git merge-tree of the two pins outside the pin bot's
#    non_build_paths. In shadow the result is logged and nothing changes; a
#    dry run uses lock-merge --check and writes nothing; without an App token,
#    or when anything inside the evaluation fails, the line says "not
#    evaluated" and the run goes on as if the rule did not exist. Every run
#    with the rule on also checks that the parent's pin is on its layer
#    line's first-parent chain and warns, naming the commit, when it is not.
#    How a lockfile-only conflict lands is merge_up.lock_pin of
#    release/config.json. As shipped (exact=bot-pr, extra=bot-pr; Dexter,
#    2026-11-18) it is always outcome B, so every merge the rule makes has a
#    PR as its record. exact=push is the supported other value: outcome A.
#    Outcome A (the lockfile is the only conflict, checks 1 to 5 pass,
#    lock_pin.exact=push, not the shipped value): the pin is set to the tip,
#    the merge is committed as
#    "Merge <child> into <parent>" with the trailer `Merge-Up-Rule: lock-pin
#    tip=<sha> parent-pin=<sha> child-pin=<sha>` and pushed to the parent like
#    a clean merge; a commit comment on the new parent commit states the rule
#    facts and the run. A rejected push refetches the meta-dsensos parent as
#    well as the distro parent, re-reads the parent's pin and evaluates again;
#    a layer line that no longer descends from the old tip refuses (check 1),
#    and so does a refetch that fails (the tip is then unknown).
#    Outcome B (the lockfile is the only conflict and checks 1 to 4 pass:
#    check 5 passes too and lock_pin.exact=bot-pr, the shipped value, or
#    check 5 does not and lock_pin.extra=bot-pr): the same commit goes to
#    merge-up/<child>/<sha7> and a PR without markers, whose body lists every
#    layer commit in the tip that neither pin contains. The same run waits for
#    the checks of merge_up.finish.required_checks on that commit (at most
#    lock_pin.wait_minutes) and merges the PR with --match-head-commit, with
#    no approval, only while the head is still the bot's commit and the
#    parent has not moved. A red check, the timeout, a moved head or base, or
#    a refused merge leaves the PR open and ends the run red with the link; a
#    later run for the child finishes that PR once its checks are green and
#    its head is still the commit the run computes (its tree, its two parents,
#    the bot's committer address and the bot's message). A branch that holds
#    anything else is left to the phase-0 path. A person who requested
#    changes on the open PR stops the merge: no run merges it, the PR stays
#    open and the run ends red with the link (a PR a person closed: see "A
#    dead branch" below). lock_pin.extra=pr opens the
#    same PR and stops: it waits for one approval and a pr=N dispatch.
#    MERGE_UP_LOCK_WAIT_SECONDS and MERGE_UP_LOCK_POLL_SECONDS (default 30)
#    override the wait and the poll interval, for the tests.
#    Outcome C (other files conflict too, checks 1 to 4 pass): the lockfile is
#    set to the tip inside the conflict commit, which carries the trailer; the
#    PR body says so and lists only the files that still have markers.
#    Outcome D (a check from 1 to 4 fails): the lockfile keeps its markers as
#    without the rule, and the PR body names the failed check and the
#    candidate tip.
#    merge_up.lock_pin.verify_finish (release/config.json, independent of the
#    variable): --pr N on a ds-tegra-distro PR also checks the lockfile the
#    resolver left. Its pin must contain the parent's previous pin and the
#    child's pin and be contained in the meta-dsensos parent tip, so a
#    resolution that took the child's pin is refused before anything merges.
# The merge rules (ENG-2234; rule names splice and pins in the variable): when a
#    merge still conflicts (after merge-keep-target and R-lock in the distro
#    step), `release_git.py resolve-conflicts` runs merge_rules.py on the
#    conflicted files, in both steps. splice resolves two edits that touch but
#    do not overlap; pins, in the meta-dsensos step only, decides the app pins
#    of conf/distro/include/ds-src-pins.inc by the branch each line follows.
#    No rule writes a file on merge_up.never_auto (merge_up.meta_dsensos.
#    never_auto in the layer step), and the lockfile and base.yml are left to
#    their own rules. In shadow each finding is logged and nothing changes. A
#    rule that acts writes its result into the merge before it is committed:
#    - some conflicts remain: the conflict PR as before, on the commit that
#      holds the rules' results and the markers of the rest. Its body lists
#      what each rule did and why the rest was left, and the commit carries a
#      `Merge-Up-Rule:` trailer per rule. For a never_auto file the rules have
#      an answer for, that answer is posted as a PR comment with a patch.
#    - every conflict is resolved: never a direct push. The merge is committed
#      as "Merge <child> into <parent>" with the trailers, pushed as
#      merge-up/<child>/<sha7>, and opened as "Merge-up <child> into <parent>
#      (resolved by rule)", a PR without markers that needs one approval and a
#      pr=N dispatch. A later run reuses the PR only while the branch head
#      is still that commit itself (its tree, its two parents, the bot's
#      committer address and the bot's message). The bot never merges such a
#      PR on its own.
#    A commit pushed on top of a commit that holds a rule's result is a stop
#    signal: a later run calls nothing on that branch resolved by rule. The PR
#    is the ordinary conflict PR, its body says the branch holds commits the
#    run did not make, and an open "(resolved by rule)" PR is retitled with a
#    comment. It needs a resolver and an approver other than the last pusher.
#    The rules are skipped, with a line that says why, when a conflict leaves
#    no text markers (the PR goes on the child itself) or an open PR already
#    contains the child. A failed evaluation is logged as "not evaluated" and
#    the run goes on as without the rules. A dry run evaluates and writes
#    nothing.
# A dead branch (decided 2026-11-18, in both steps, with or without the
#    variable): merge-up/<child>/<sha7> is dead when no PR with that head
#    against <parent> is open and the most recent one was closed without a
#    merge by a person, that is by anyone but the bot's own supersession
#    (closed by a Bot account that left the "Superseded by" comment). An open
#    PR of the head keeps the branch alive whatever its number. What cannot be read (the PR list, who
#    closed it) counts as dead, and the run says it could not be read. On a
#    dead branch a run opens no PR, pushes nothing to the branch, merges
#    nothing from it and applies no rule to it, whatever the PR would have
#    been (bot-finished, resolved by rule, a conflict PR, the PR on the child
#    itself) and however the run was started (workflow_run or a dispatch by
#    hand). It ends like a conflict left for people: ::error::, a summary
#    line, exit 1 (a dry run prints the finding and what it would not do,
#    exit 0). The two ways forward: the next green commit on the child has a
#    new <sha7>, so a new branch and a new PR; or a person reopens the closed
#    PR, and the branch is not dead any more. In the meta-dsensos step <sha7>
#    is the tip of the layer's child line, so there the new branch comes with
#    the next commit on meta-dsensos <child>: a green commit on ds-tegra-distro
#    <child> that leaves the layer's child line where it is meets the same
#    dead branch, and the error says so. The bot never reopens or
#    comments on that PR. An open merge-up PR on another branch that already
#    contains the child is still reused, a --pr finish of an open PR is
#    untouched, and so is a merge that needs no branch (a clean merge, or
#    outcome A of the lockfile pin rule).
# Only an App token can push to meta-dsensos and start the parent's
# rc-build; with TOKEN_KIND=github a real run stops before any push and
# prints the commands a human runs. A dry run prints the plan (green run,
# pin containment, trial merges and their conflicts) and exits 0.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
# GIT_BOT_NAME and GIT_BOT_EMAIL (the release App's commit identity); this
# script defines its own die() further down, which replaces lib.sh's.
. "$SCRIPTS/lib.sh"
cd "$REPO"

CHILD=""
PARENT=""
PR=""
DRY_RUN="${DRY_RUN:-true}"
TOKEN_KIND="${TOKEN_KIND:-github}"
DISTRO_REPO="${DISTRO_REPO:-DistributedSpectrum/ds-tegra-distro}"
META_REPO="${META_REPO:-DistributedSpectrum/meta-dsensos}"
META_URL="${META_DSENSOS_URL:-https://x-access-token:${TOKEN:-}@github.com/${META_REPO}.git}"
META_HUMAN_URL="${META_DSENSOS_HUMAN_URL:-git@github.com:${META_REPO}.git}"
CALLER_SHA="${CALLER_SHA:-}"
CALLER_RUN="${CALLER_RUN:-}"
RC_WORKFLOW="release-rc-build.yml"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-$DISTRO_REPO}/actions/runs/${GITHUB_RUN_ID:-local}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,40p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --child) CHILD="$2"; shift 2 ;;
    --parent) PARENT="$2"; shift 2 ;;
    --pr) PR="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::merge-up: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::merge-up: $*"
  exit 1
}

TMP=$(mktemp -d)
WT="$TMP/wt"
cleanup() {
  if [ -d "$WT" ]; then
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

[ -n "$CHILD" ] || die "--child is required"
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
if [ -n "$PR" ] && ! [[ "$PR" =~ ^[0-9]+$ ]]; then
  die "--pr must be a PR number, got '$PR'"
fi

# ---- the switches (ENG-2234) --------------------------------------------------

# Printed before anything else so every run says which rules it ran under,
# whatever it ends with. No rule exists yet: this only reports.
SCRIPTS_ROOT=$(dirname "$SCRIPTS")
AUTOMATION_LINES=$(python3 "$SCRIPTS/release_git.py" automation 2>"$TMP/automation.err") \
  || die "$(sed 's/^release_git.py: //' "$TMP/automation.err"); nothing was read or pushed. Set the variable to off, shadow or a rule list, or delete it"
SCRIPTS_SHA=$(git -C "$SCRIPTS_ROOT" rev-parse --short=7 HEAD 2>/dev/null) || SCRIPTS_SHA=unknown
SCRIPTS_REF=$(git -C "$SCRIPTS_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null) || SCRIPTS_REF="${GITHUB_REF_NAME:-a detached checkout}"
SCRIPTS_NOTE=""
if [ -n "${DEFAULT_BRANCH:-}" ]; then
  if [ "$SCRIPTS_REF" = "$DEFAULT_BRANCH" ]; then
    SCRIPTS_NOTE=" (the default branch)"
  else
    SCRIPTS_NOTE=" (NOT the default branch $DEFAULT_BRANCH: this run uses that ref's scripts and release/config.json)"
  fi
fi
CONFIG_LINE=$(python3 - "$SCRIPTS" <<'PY' 2>"$TMP/config.err"
import sys
sys.path.insert(0, sys.argv[1])
import release_git
c = release_git.merge_up_config()
flag = lambda b: 'true' if b else 'false'
print(f"finish.strict={flag(c['finish']['strict'])}, lock_pin.verify_finish={flag(c['lock_pin']['verify_finish'])}, "
      f"lock_pin.exact={c['lock_pin']['exact']}, lock_pin.extra={c['lock_pin']['extra']}")
PY
) || die "release/config.json of the scripts in use ($SCRIPTS_REF at $SCRIPTS_SHA): $(tail -1 "$TMP/config.err"); nothing was read or pushed"
# finish.strict as the validated read above printed it; finish_pr uses this
# value and never reads the key a second time.
case "$CONFIG_LINE" in "finish.strict=true,"*) CONFIG_STRICT=true ;; *) CONFIG_STRICT=false ;; esac
# Where every finish instruction this run prints says to dispatch: the default
# branch, so the finish runs the current scripts and release/config.json and
# not those of the child line (ENG-2234).
ON_DEFAULT="on the default branch${DEFAULT_BRANCH:+ ($DEFAULT_BRANCH)}"
WHY_DEFAULT="Dispatch it $ON_DEFAULT, not on \`$CHILD\`: a run uses the scripts and the release/config.json of the ref it is dispatched on, and the finish checks live on the default branch."
if [ -n "${MERGE_UP_READ_TOKEN:-}" ]; then READ_TOKEN_NOTE="present"; else READ_TOKEN_NOTE="absent"; fi
{
  echo "== merge-up automation"
  echo "$AUTOMATION_LINES"
  echo "scripts:            $SCRIPTS_REF at $SCRIPTS_SHA$SCRIPTS_NOTE"
  echo "config:             $CONFIG_LINE (release/config.json at that ref)"
  echo "read token:         $READ_TOKEN_NOTE (read-only, ds and sensor-portal)"
} > "$TMP/automation.txt"
cat "$TMP/automation.txt"
if [ -n "${MERGE_UP_SUMMARY:-}" ]; then
  {
    echo "### Merge-up $CHILD"
    echo
    echo '```'
    cat "$TMP/automation.txt"
    echo '```'
  } >> "$MERGE_UP_SUMMARY"
fi

# cfg <dotted key>: one value of the merge_up block of the release/config.json
# in use (a string bare, anything else as JSON). cfg_names: a list as a,b.
cfg() {
  python3 "$SCRIPTS/release_git.py" merge-up-config --key "$1"
}
cfg_names() {
  cfg "$1" | python3 -I -c 'import json,sys; print(",".join(json.load(sys.stdin)))'
}

# ---- R-lock: the lockfile pin rule (ENG-2234) ----------------------------------

LOCK=kas/include/base.lock.yml
# off, shadow or act: the value was parsed above, so this cannot fail on it.
LOCK_MODE=$(python3 "$SCRIPTS/release_git.py" automation --rule lock-pin)
META=""                # the meta-dsensos clone, once an App token has read the layer repo
META_PARENT_TIP=""     # what origin/<parent> holds there after this script's own push or fetch
META_TIP_PLANNED=""    # dry run: a real run pushes a new tip, so the one above is not it yet
META_TIP_REWOUND=""    # a refetch found the parent layer line no longer descends from that tip
META_TIP_UNREAD=""     # a refetch of the parent layer line failed (why): its tip is unknown
LOCK_VERDICT=""        # set by lock_rule_eval: planned, exact, extra or refused
LOCK_WOULD=""          # the outcome the rule would take: A, B, P, C or D
LOCK_OUTCOME=""        # that outcome, once the rule acted on this merge
LOCK_TRAILER=""        # the Merge-Up-Rule trailer of a commit the rule resolved
LOCK_BODY_NOTE=""      # what the conflict PR body says about the lockfile (outcomes C and D)

# summary <line>: one more line for the run summary (the file the workflow names).
summary() {
  [ -z "${MERGE_UP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$MERGE_UP_SUMMARY"
}

# lock_say <text>: one line of the rule's log, on stdout and in the run summary.
lock_say() {
  echo "ds-tegra-distro:    lockfile pin rule: $1"
  summary "- lockfile pin rule: $1"
}

# lock_field <key>: one value of the last evaluation (a list as lines, a flag as true or false).
lock_field() {
  python3 -I -c '
import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
print("\n".join(map(str, v)) if isinstance(v, list) else str(v).lower() if isinstance(v, bool) else "" if v is None else v)' "$TMP/lock-rule.json" "$1"
}

# lock_checks: the evaluation's check lines, "check N ok|FAILED: text".
lock_checks() {
  python3 -I -c '
import json, sys
for c in json.load(open(sys.argv[1]))["checks"]:
    print("check %d %s: %s" % (c["n"], "ok" if c["ok"] else "FAILED", c["text"]))' "$TMP/lock-rule.json"
}

# lock_invariant_check: the parent's lock pin should be on the first-parent
# chain of its own meta-dsensos line. A warning when it is not, never a stop.
lock_invariant_check() {
  local out
  [ "$LOCK_MODE" != off ] || return 0
  if [ -z "$META" ]; then
    echo "lock invariant:     not evaluated (no App token, the layer repo was not read)"
  elif out=$(python3 "$SCRIPTS/release_git.py" lock-invariant --git-dir "$REPO" --meta-dir "$META" \
               --parent-tip "$PARENT_TIP" --meta-ref "refs/remotes/origin/$PARENT" 2>"$TMP/lock-invariant.err"); then
    echo "$out"
    case "$out" in ::warning::*) summary "- ${out#::warning::merge-up: }" ;; esac
  else
    echo "lock invariant:     not evaluated ($(tail -1 "$TMP/lock-invariant.err" | sed 's/^release_git.py: //'))"
  fi
}

# lock_rule_eval: R-lock on the merge that is open in $WT, read-only. Runs only
# when the rule is on and the lockfile is unmerged. Leaves LOCK_VERDICT empty
# when the rule does not apply or could not be evaluated (the reason is
# logged as "not evaluated"), so the caller goes on exactly as without it.
lock_rule_eval() {
  local parent_pin others
  local -a args=()
  LOCK_VERDICT=""
  LOCK_WOULD=""
  [ "$LOCK_MODE" != off ] || return 0
  [ -n "$(git -C "$WT" diff --name-only --diff-filter=U -- "$LOCK" 2>/dev/null)" ] || return 0
  if [ -z "$META" ]; then
    lock_say "not evaluated (no App token, the layer repo was not read)"
    return 0
  fi
  if ! parent_pin=$(python3 "$SCRIPTS/release_git.py" lock-pin "$PARENT_TIP" --git-dir "$REPO" 2>"$TMP/lock-rule.err"); then
    lock_say "not evaluated (the parent's lockfile at ${PARENT_TIP:0:7} could not be read: $(tail -1 "$TMP/lock-rule.err" | sed 's/^release_git.py: //'))"
    return 0
  fi
  others=$(printf '%s\n' "$CONFLICTS" | grep -v -x -F "$LOCK" | grep -c . || true)
  [ -z "$META_TIP_PLANNED" ] || args+=(--planned)
  [ -z "$META_TIP_REWOUND" ] || args+=(--rewound "$META_TIP_REWOUND")
  [ -z "$META_TIP_UNREAD" ] || args+=(--unread "$META_TIP_UNREAD")
  if ! python3 "$SCRIPTS/release_git.py" lock-rule "$META_PARENT_TIP" --git-dir "$WT" --meta-dir "$META" \
         --parent-pin "$parent_pin" --child-pin "$PIN" --others "$others" ${args[@]+"${args[@]}"} \
         > "$TMP/lock-rule.json" 2>"$TMP/lock-rule.err"; then
    lock_say "not evaluated ($(tail -1 "$TMP/lock-rule.err" | sed 's/^release_git.py: //'))"
    return 0
  fi
  if ! LOCK_VERDICT=$(lock_field verdict) || ! LOCK_WOULD=$(lock_field outcome) || [ -z "$LOCK_VERDICT" ]; then
    LOCK_VERDICT=""
    lock_say "not evaluated (the evaluation's result could not be read)"
  fi
  return 0
}

# lock_resolve: the one write of the rule. Sets the lockfile to the tip in the
# open merge and stages it, then recomputes CONFLICTS. Returns 1, with the
# merge as it was, when the write fails (logged as "not evaluated").
lock_resolve() {
  if ! python3 "$SCRIPTS/release_git.py" lock-merge "$META_PARENT_TIP" --git-dir "$WT" >/dev/null 2>"$TMP/lock-rule.err"; then
    lock_say "not evaluated (the lockfile could not be written: $(tail -1 "$TMP/lock-rule.err" | sed 's/^release_git.py: //'))"
    return 1
  fi
  LOCK_TRAILER=$(lock_field trailer)
  CONFLICTS=$(printf '%s\n%s\n' "$(git -C "$WT" diff --name-only --diff-filter=U)" "$BASE_CONFLICT" | grep -v '^$' | sort -u || true)
}

# lock_refetch_meta: after a rejected push the parent moved, and its lock may
# now name a newer layer commit. Refetch the meta-dsensos parent too. The new
# tip must descend from the one this run started with; a rewritten layer line
# makes the next evaluation refuse (check 1), and so does a refetch that
# fails, with a reason of its own: the tip is then unknown, not rewritten.
lock_refetch_meta() {
  local old="$META_PARENT_TIP" new
  [ "$LOCK_MODE" != off ] && [ -n "$META" ] && [ -z "$META_TIP_PLANNED" ] || return 0
  # Forced, so the fetch itself says nothing about ancestry: the test below is the guard.
  if ! git -C "$META" fetch --quiet origin "+refs/heads/$PARENT:refs/remotes/origin/$PARENT" 2>"$TMP/meta-refetch.err"; then
    META_TIP_UNREAD="$(grep -v '^$' "$TMP/meta-refetch.err" | tail -1)"
    META_TIP_UNREAD="${META_TIP_UNREAD:-git fetch failed}"
    echo "meta-dsensos: $PARENT could not be refetched ($META_TIP_UNREAD)"
    return 0
  fi
  META_TIP_UNREAD=""
  new=$(git -C "$META" rev-parse "refs/remotes/origin/$PARENT")
  [ "$new" != "$old" ] || return 0
  if git -C "$META" merge-base --is-ancestor "$old" "$new"; then
    META_PARENT_TIP="$new"
    echo "meta-dsensos: $PARENT moved to $new (it descends from $old)"
  else
    META_TIP_REWOUND="$new does not descend from $old"
    echo "meta-dsensos: $PARENT was rewritten: $META_TIP_REWOUND"
  fi
}

# lock_commit_comment <commit>: the record of an outcome A push, on the new
# parent commit. A failure is a warning: the merge already happened.
lock_commit_comment() {
  local sha="$1"
  {
    echo "release-merge-up merged \`$CHILD\` into \`$PARENT\` with the lockfile pin rule (direct push, no PR)."
    echo
    echo "Both lines had moved the meta-dsensos pin in \`$LOCK\`, and that was the only conflict. The lock of \`$PARENT\` is now the tip of meta-dsensos \`$PARENT\`, which contains both pins and builds the same as the plain merge of the two."
    echo
    echo "- tip: \`$(lock_field tip)\`"
    echo "- pin of \`$PARENT\` before: \`$(lock_field parent_pin)\`"
    echo "- pin of \`$CHILD\`: \`$(lock_field child_pin)\`"
    echo
    lock_checks | sed 's/^/- /'
    echo
    echo "Run: $RUN_URL"
  } > "$TMP/lock-comment.md"
  if gh api -X POST "repos/$DISTRO_REPO/commits/$sha/comments" -f body="$(cat "$TMP/lock-comment.md")" >/dev/null 2>"$TMP/lock-comment.err"; then
    echo "commented on $sha with the rule facts"
  else
    echo "::warning::merge-up: could not comment on $sha ($(head -1 "$TMP/lock-comment.err")); the commit's Merge-Up-Rule trailer is the record"
  fi
}

# lock_cfg <dotted key>: one value of the merge_up config of the scripts in use.
lock_cfg() {
  python3 "$SCRIPTS/release_git.py" merge-up-config --key "$1"
}

# lock_bot_stop <url> <why>: a bot-finished PR the bot does not merge. It stays
# open, and the run ends red with the link.
lock_bot_stop() {
  summary "- bot-finished PR $1 was not merged: $2"
  echo "::error::merge-up: bot-finished PR $1 was not merged: $2. The PR stays open. A person approves it and dispatches release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${1##*/}, or the next merge-up run of $CHILD finishes it once its required checks are green and its head is untouched"
  exit 1
}

# lock_changes_requested [<url>]: before the bot merges a bot-finished PR that
# is open: did a person request changes on it? Without <url> the PR is the
# open one of merge-up/<child>/<sha7> (branch_dead read it), when it has
# one. The latest review of each person counts (a later approval or a
# dismissal by the same person takes it back; a bot's review never counts),
# and reviews that cannot be read count as a no. The bot then does not merge:
# the PR stays open for people and the run ends red with the link, however
# the run was started. (A PR a person closed is a dead branch: branch_dead.)
lock_changes_requested() {
  local url="${1:-}" number state="" why=""
  if [ -z "$url" ]; then
    read -r number state url <<<"$BRANCH_LATEST" || true
    [ "$state" = OPEN ] || return 0
  fi
  number="${url##*/}"
  if gh api --paginate "repos/$DISTRO_REPO/pulls/$number/reviews" > "$TMP/head-reviews.json" 2>"$TMP/person-no.err" \
     && why=$(python3 "$SCRIPTS/release_git.py" person-no --state OPEN --reviews "$TMP/head-reviews.json" 2>>"$TMP/person-no.err"); then
    :
  else
    why="its reviews could not be read ($(tail -1 "$TMP/person-no.err"))"
  fi
  [ -n "$why" ] || return 0
  lock_say "not applied: a person said no to bot-finished PR $url ($why). The bot does not merge it, and the PR stays open for people"
  echo "::error::merge-up: a person said no to bot-finished PR $url ($why), so the bot does not merge $DISTRO_CONFLICT_BRANCH. The PR stays open. Once the review is dismissed or its author approves, the next merge-up run of $CHILD finishes it. Or approve the PR and dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=$number. A new commit on $CHILD gets a branch of its own and runs as normal"
  exit 1
}

# lock_pr_body <B|P> <bot commit>: the body of the PR without markers.
lock_pr_body() {
  local kind="$1" sha="$2" count
  count=$(lock_field extra_commits | grep -c . || true)
  echo "release-merge-up merged \`$CHILD\` into \`$PARENT\`. The only conflict was the meta-dsensos pin in \`$LOCK\`, which both lines had moved. The lockfile pin rule set it to the tip of meta-dsensos \`$PARENT\`, which contains both pins, so this branch has no conflict markers."
  echo
  echo "- tip (the new pin): \`$(lock_field tip)\`"
  echo "- pin of \`$PARENT\` before: \`$(lock_field parent_pin)\`"
  echo "- pin of \`$CHILD\`: \`$(lock_field child_pin)\`"
  echo
  if [ "$LOCK_VERDICT" = exact ]; then
    echo "The tip builds the same as the plain merge of the two pins. This is a PR and not a direct push because \`merge_up.lock_pin.exact\` is \`bot-pr\`."
  else
    echo "This is a PR and not a direct push because the tip holds more than the two pins: $(lock_field reason | head -1)."
  fi
  echo
  echo "Layer commits in the tip that neither pin contains ($count, merge commits included). Merging this PR puts them into the lock of \`$PARENT\`:"
  echo
  lock_field extra_commits | head -100 | sed 's/^\([0-9a-f]*\) \(.*\)$/- `\1` \2/'
  [ "$count" -le 100 ] || echo "- and $((count - 100)) more"
  echo
  echo "Checks:"
  echo
  lock_checks | sed 's/^/- /'
  echo
  if [ "$kind" = B ]; then
    echo "The bot merges this PR itself, with no approval, once $(lock_cfg finish.required_checks | python3 -I -c 'import json,sys; print(" and ".join("`%s`" % n for n in json.load(sys.stdin)) or "(no check is configured)")') succeed on \`${sha:0:7}\`. It waits up to $(lock_cfg lock_pin.wait_minutes) minutes, and merges only while that commit is still the head and \`$PARENT\` has not moved. If a check fails, the wait runs out or someone pushes here, the PR stays open: approve it and dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false, or leave it to the next merge-up run of \`$CHILD\`, which finishes it once the checks are green and the head is untouched. $WHY_DEFAULT To stop the bot, request changes on this PR or close it. With changes requested the PR stays open and no run merges it until the review is dismissed or its author approves. A PR closed without merging closes \`$DISTRO_CONFLICT_BRANCH\` for the bot: no run, automatic or dispatched by hand, opens a new PR on it or merges it. The next green commit on \`$CHILD\` gets a new branch and a new PR, and reopening this PR brings this branch back."
  else
    echo "\`merge_up.lock_pin.extra\` is \`pr\`, so the bot does not merge this PR. It needs one approval. Then dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT"
  fi
  echo
  echo "Run: $RUN_URL"
}

# lock_pr <B|P>: the lockfile is the only conflict and the rule set it, but
# the result goes through a PR. Commits the merge without markers, pushes it
# as merge-up/<child>/<sha7> and opens the PR; B then waits and merges it.
# Returns 0 without having done anything when the branch already exists on
# origin with other content (a resolver's work, or a phase-0 conflict
# commit): the lockfile conflict is put back and the phase-0 path takes over.
lock_pr() {
  local kind="$1" branch="$DISTRO_CONFLICT_BRANCH" tree remote sha url
  cp "$WT/$LOCK" "$TMP/lock-as-merged"   # the conflict as git wrote it, for the way back
  lock_resolve || return 0
  tree=$(git -C "$WT" write-tree)
  remote=$(git -C "$WT" ls-remote origin "refs/heads/$branch" | cut -f1)
  if [ -n "$remote" ]; then
    git -C "$WT" fetch --quiet origin "refs/heads/$branch"
    # The bot's own commit and nothing else: the tree and the two parents this run
    # computes, committed under the bot's address with the message the bot writes.
    # A commit someone else pushed there is never finished by the bot, whatever it holds.
    if [ "$(git -C "$WT" rev-parse "$remote^{tree}")" = "$tree" ] \
       && [ "$(git -C "$WT" rev-parse "$remote^1" "$remote^2" 2>/dev/null | paste -sd ' ')" = "$PARENT_TIP $CHILD_TIP" ] \
       && [ "$(git -C "$WT" show -s --format=%ce "$remote")" = "$GIT_COMMITTER_EMAIL" ] \
       && [ "$(git -C "$WT" show -s --format=%B "$remote")" = "$MERGE_SUBJECT"$'\n\n'"$LOCK_TRAILER" ]; then
      [ "$kind" != B ] || lock_changes_requested
      sha="$remote"
      git -C "$WT" merge --abort
      echo "$branch is already on origin at ${sha:0:7}: the commit this run computes, untouched; reusing it"
    else
      # Not the bot's commit for this merge. Put the conflict back as git wrote it:
      # checkout --conflict restores the unmerged index entries but writes its own
      # labels (ours, base, theirs), which would change the tree of the conflict
      # commit and make the bot's own phase-0 branch look foreign.
      git -C "$WT" checkout --conflict=zdiff3 -- "$LOCK"
      cp "$TMP/lock-as-merged" "$WT/$LOCK"
      LOCK_TRAILER=""
      CONFLICTS=$(printf '%s\n%s\n' "$(git -C "$WT" diff --name-only --diff-filter=U)" "$BASE_CONFLICT" | grep -v '^$' | sort -u || true)
      lock_say "not applied: $branch already exists on origin at ${remote:0:7} and is not the commit this run computes (someone pushed to it, the parent moved, or it is an earlier conflict branch). It is left as it is"
      return 0
    fi
  else
    git -C "$WT" commit --quiet -m "$MERGE_SUBJECT" -m "$LOCK_TRAILER"
    sha=$(git -C "$WT" rev-parse HEAD)
    git -C "$WT" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: ${sha:0:7}, $PARENT plus the merge of $CHILD, the lockfile pin set to the meta-dsensos $PARENT tip, no markers"
  fi
  LOCK_OUTCOME="$kind"
  url=$(gh pr list -R "$DISTRO_REPO" --base "$PARENT" --head "$branch" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -z "$url" ]; then
    lock_pr_body "$kind" "$sha" > "$TMP/lock-pr-body.md"
    url=$(gh pr create -R "$DISTRO_REPO" --base "$PARENT" --head "$branch" \
            --title "Merge-up $CHILD into $PARENT (lockfile pin set by rule)" --body-file "$TMP/lock-pr-body.md") \
      || die "could not open the PR for $branch in $DISTRO_REPO"
  fi
  lock_say "pin set to the meta-dsensos $PARENT tip $META_PARENT_TIP in $url ($(if [ "$kind" = B ]; then echo "outcome B, bot-finished PR: $(lock_why_pr)"; else echo "lock_pin.extra=pr, one approval: $(lock_field reason | head -1)"; fi))"
  close_superseded "$DISTRO_REPO" "$PARENT" "$branch" "$url"
  if [ "$kind" = P ]; then
    echo "::error::merge-up: $url has the lockfile pin set by rule and no markers; merge_up.lock_pin.extra is pr, so it waits for one approval. Then dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${url##*/}"
    exit 1
  fi
  lock_bot_finish "$url" "$sha"
}

# lock_bot_finish <url> <bot commit>: wait for the required checks on the
# bot's commit, then merge the PR pinned to that commit. The App bypasses the
# approval ruleset, so what is read here is the only gate on this merge.
lock_bot_finish() {
  local url="$1" sha="$2" number="${1##*/}" required wait poll deadline state reasons now
  required=$(lock_cfg finish.required_checks | python3 -I -c 'import json,sys; print(",".join(json.load(sys.stdin)))')
  wait="${MERGE_UP_LOCK_WAIT_SECONDS:-$(( $(lock_cfg lock_pin.wait_minutes) * 60 ))}"
  poll="${MERGE_UP_LOCK_POLL_SECONDS:-30}"
  deadline=$(( SECONDS + wait ))
  echo "waiting for ${required:-no check} on ${sha:0:7} (at most ${wait}s)"
  while :; do
    state=unread
    reasons="the PR could not be read"
    if gh pr view "$number" -R "$DISTRO_REPO" --json state,headRefOid,statusCheckRollup > "$TMP/bot-pr.json" 2>/dev/null \
       && python3 "$SCRIPTS/release_git.py" checks-state "$TMP/bot-pr.json" --head "$sha" --required "$required" > "$TMP/bot-pr.state" 2>/dev/null; then
      state=$(head -1 "$TMP/bot-pr.state")
      reasons=$(tail -n +2 "$TMP/bot-pr.state" | paste -sd ';' | sed 's/;/; /g')
    fi
    case "$state" in
      green) break ;;
      red) lock_bot_stop "$url" "a required check failed on ${sha:0:7} ($reasons)" ;;
      moved) lock_bot_stop "$url" "$reasons" ;;
      closed) lock_bot_stop "$url" "$reasons" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || lock_bot_stop "$url" "the required checks were not green on ${sha:0:7} after ${wait}s ($reasons)"
    sleep "$poll"
  done
  echo "required checks green on ${sha:0:7}: $reasons"
  # A review that asks for changes may have come in during the wait.
  lock_changes_requested "$url"
  # The checks vouch for this commit on top of the parent it was made from.
  now=$(git ls-remote origin "refs/heads/$PARENT" | cut -f1)
  [ "$now" = "$PARENT_TIP" ] || lock_bot_stop "$url" "$PARENT moved from ${PARENT_TIP:0:7} to ${now:0:7} while the checks ran, so the checked commit is no longer up to date with it"
  gh pr merge "$number" -R "$DISTRO_REPO" --merge --match-head-commit "$sha" > "$TMP/bot-merge.log" 2>&1 \
    || lock_bot_stop "$url" "GitHub refused the merge ($(tail -1 "$TMP/bot-merge.log"))"
  echo "merged PR $DISTRO_REPO#$number (head $sha, no approval: the lockfile pin rule and the required checks)"
  git fetch --quiet origin
  NEW_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  post_check "$REPO" "$NEW_TIP" "$sha" "$CHILD_TIP"
  close_if_still_open "$DISTRO_REPO" "$number" "$NEW_TIP" "$DISTRO_CONFLICT_BRANCH"
  gh pr comment "$number" -R "$DISTRO_REPO" --body "Merged by release-merge-up at $NEW_TIP with no approving review: the lockfile pin rule made this commit (\`${sha:0:7}\`), and ${required//,/, } succeeded on it. Run: $RUN_URL" >/dev/null 2>"$TMP/bot-comment.err" \
    || echo "::warning::merge-up: could not comment on $url ($(head -1 "$TMP/bot-comment.err"))"
  close_superseded "$DISTRO_REPO" "$PARENT" "" "PR #$number, merged at $NEW_TIP"
  summary "- bot-finished PR $url merged at $NEW_TIP"
  echo "done: $CHILD merged into $PARENT at $NEW_TIP (bot-finished PR $number)"
  exit 0
}

# lock_why_pr: why a lockfile-only conflict the rule resolved is a
# bot-finished PR and not a direct push, as one clause. Verdict exact: the
# tip holds nothing new and lock_pin.exact says bot-pr. Verdict extra: the
# tip holds more, and the reason is what check 5 found.
lock_why_pr() {
  if [ "$LOCK_VERDICT" = exact ]; then
    echo "checks 1 to 5 pass, $(lock_checks | sed -n 's/^check 5 ok: //p'); a PR and not a direct push because lock_pin.exact=bot-pr"
  else
    echo "checks 1 to 4 pass; $(lock_field reason | head -1)"
  fi
}

# lock_would: what the evaluated outcome means, for a log line.
lock_would() {
  local tip="$META_PARENT_TIP" extra
  extra=$(lock_field extra_commits | grep -c . || true)
  case "$LOCK_WOULD" in
    A) echo "set the pin to the meta-dsensos $PARENT tip $tip and push $PARENT directly (outcome A: checks 1 to 5 pass, $(lock_checks | sed -n 's/^check 5 ok: //p'))" ;;
    B) echo "set the pin to the meta-dsensos $PARENT tip $tip in a bot-finished PR on $DISTRO_CONFLICT_BRANCH (outcome B: $(lock_why_pr); $extra layer commit(s) in neither pin)" ;;
    P) echo "set the pin to the meta-dsensos $PARENT tip $tip in a PR on $DISTRO_CONFLICT_BRANCH that waits for one approval (lock_pin.extra=pr; checks 1 to 4 pass; $extra layer commit(s) in neither pin; $(lock_field reason | head -1))" ;;
    C) echo "pre-resolve the lockfile to the meta-dsensos $PARENT tip $tip inside the conflict PR (outcome C: checks 1 to 4 pass, other files conflict too)" ;;
    D) echo "leave the markers in the lockfile (outcome D, refused at check $(lock_field failed): $(lock_field reason | head -1); candidate tip $tip)" ;;
    *) echo "nothing (unknown outcome '$LOCK_WOULD')" ;;
  esac
}

# lock_rule_apply: what the evaluation leads to. Shadow: one log line, nothing
# else. A dry run: the same line, and for an outcome that needs no person the
# run ends here, because the phase-0 plan (a conflict PR) is not what a real
# run would do. A planned tip is never used as a value.
lock_rule_apply() {
  local shadow=""
  [ "$LOCK_MODE" = act ] || shadow="shadow, nothing changes: "
  if [ "$LOCK_VERDICT" = planned ]; then
    if [ "$(lock_field planned_ok)" != true ]; then
      lock_say "${shadow}dry run: would refuse and leave the markers in the lockfile ($(lock_field reason | head -1))"
      return 0
    fi
    lock_say "${shadow}dry run: would set the pin to the new meta-dsensos $PARENT tip (the merge a real run pushes; the parent's pin $(lock_field parent_pin | cut -c1-7) and the child's pin ${PIN:0:7} are both in it). How that lands is decided on the real tip: lock_pin.exact=$(lock_cfg lock_pin.exact) when it holds nothing new, lock_pin.extra=$(lock_cfg lock_pin.extra) when it holds more (bot-pr: a bot-finished PR; push: a direct push; pr: a PR that waits for one approval)"
    [ "$LOCK_MODE" = act ] || return 0
    [ -z "$(printf '%s\n' "$CONFLICTS" | grep -v -x -F "$LOCK" | grep . || true)" ] || return 0
    # Direct push (lock_pin.exact=push) or PR is decided on the real tip, so a dead branch only matters if it is a PR.
    if branch_dead "$DISTRO_REPO" "$DISTRO_CONFLICT_BRANCH"; then
      lock_say "dry run: $(dead_finding "$DISTRO_CONFLICT_BRANCH"). If the real tip needs a PR on that branch (a bot-finished PR, or one that waits for an approval), a real run opens none, pushes nothing to the branch and ends with an error"
    fi
    git -C "$WT" merge --abort
    echo "dry run: nothing pushed"
    exit 0
  fi
  if [ "$LOCK_MODE" != act ]; then
    lock_say "${shadow}would $(lock_would)"
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    lock_say "dry run: would $(lock_would)"
    case "$LOCK_WOULD" in
      A) git -C "$WT" merge --abort
         close_superseded "$DISTRO_REPO" "$PARENT" "" "the clean merge"
         echo "dry run: nothing pushed"
         exit 0 ;;
      B|P) git -C "$WT" merge --abort
         close_superseded "$DISTRO_REPO" "$PARENT" "$DISTRO_CONFLICT_BRANCH" "the PR on $DISTRO_CONFLICT_BRANCH"
         echo "dry run: nothing pushed"
         exit 0 ;;
    esac
    return 0
  fi
  case "$LOCK_WOULD" in
    A) lock_resolve || return 0
       LOCK_OUTCOME=A
       lock_say "pin set to the meta-dsensos $PARENT tip $META_PARENT_TIP; pushing $PARENT directly (outcome A: checks 1 to 5 pass, $(lock_checks | sed -n 's/^check 5 ok: //p'))" ;;
    B|P) lock_pr "$LOCK_WOULD" ;;
    C) lock_resolve || return 0
       LOCK_OUTCOME=C
       LOCK_BODY_NOTE="\`$LOCK\` conflicted too (both lines moved the meta-dsensos pin) and is already resolved in this commit by the lockfile pin rule. It pins the tip of meta-dsensos \`$PARENT\`, \`$META_PARENT_TIP\`, which contains the pin of \`$PARENT\` (\`$(lock_field parent_pin)\`) and the pin of \`$CHILD\` (\`$(lock_field child_pin)\`). It has no markers, so leave it as it is."
       lock_say "lockfile set to the meta-dsensos $PARENT tip $META_PARENT_TIP inside the conflict PR (outcome C: checks 1 to 4 pass, $(printf '%s\n' "$CONFLICTS" | grep -c . || true) other file(s) conflict)" ;;
    D) LOCK_OUTCOME=D
       LOCK_BODY_NOTE="The lockfile pin rule did not resolve \`$LOCK\`: check $(lock_field failed) failed ($(lock_field reason | head -1)). The candidate was the tip of meta-dsensos \`$PARENT\`, \`$META_PARENT_TIP\`. Resolve the lockfile by hand: the lock of \`$PARENT\` belongs on a commit of its own meta-dsensos line that contains both pins."
       lock_say "refused at check $(lock_field failed): $(lock_field reason | head -1); candidate tip $META_PARENT_TIP. The lockfile keeps its markers (outcome D)" ;;
    *) lock_say "not applied (unknown outcome '$LOCK_WOULD')" ;;
  esac
}

# ---- the merge rules: splice and pins (ENG-2234) --------------------------------

# off, shadow or act, from the variable parsed above.
SPLICE_MODE=$(python3 "$SCRIPTS/release_git.py" automation --rule splice)
PINS_MODE=$(python3 "$SCRIPTS/release_git.py" automation --rule pins)
RULES_BODY=""       # file: what the rules did, for the body of a PR this run opens
RULES_COMMENT=""    # file: the candidate patches of never_auto files, for a PR comment
RULES_TRAILERS=""   # the Merge-Up-Rule lines of the commit, one per line
RULES_ACTED=""      # set when a rule changed the merge that is open
RULES_ASKED=""      # "<repo> <branch>" once branch_gate has looked for an open PR that holds the child
RULES_EXISTING=""   # what it found there ("<number> <head>" or nothing); rules_resolve and on_conflict do not ask twice

# rules_say <what> <text>: one line of the rules' log, on stdout and in the run summary.
rules_say() {
  echo "$1: merge rules: $2"
  summary "- $1 merge rules: $2"
}

# rules_resolve <what> <repo> <git dir> <branch> <child tip> <conflict list file> [resolve-conflicts options]:
# the merge rules on the merge that is open in <git dir>. Shadow: log lines
# only. Act: the rules' results are written into the merge and <conflict list
# file> is rewritten to the files still in conflict (in a dry run: the files
# that would be); the original list stays in <conflict list file>.all. Leaves
# everything as it was when the rules do not apply or could not be evaluated.
rules_resolve() {
  local what="$1" repo="$2" dir="$3" branch="$4" tip="$5" files="$6" out="$TMP/rules-$1" existing line
  shift 6
  local -a args=("$@")
  RULES_BODY=""
  RULES_COMMENT=""
  RULES_TRAILERS=""
  RULES_ACTED=""
  cp "$files" "$files.all"
  [ "$SPLICE_MODE" != off ] || [ "$PINS_MODE" != off ] || return 0
  if [ -n "$(nontext_conflicts "$dir" "$files")" ]; then
    rules_say "$what" "not applied: a conflict here leaves no text markers, so the PR goes on $CHILD itself and a rule's result would not be on it"
    return 0
  fi
  if [ "$RULES_ASKED" != "$repo $branch" ]; then
    RULES_EXISTING=$(containing_pr "$repo" "$PARENT" "$dir" "$tip" "$branch")
    RULES_ASKED="$repo $branch"
  fi
  existing="$RULES_EXISTING"   # branch_gate asked a moment ago
  if [ -n "$existing" ]; then
    rules_say "$what" "not applied: open PR $repo#${existing%% *} already contains $CHILD at ${tip:0:7} and is reused as it is"
    return 0
  fi
  [ "$DRY_RUN" = false ] || args+=(--check)
  rm -rf "$out"
  if ! python3 "$SCRIPTS/release_git.py" resolve-conflicts --git-dir "$dir" --out "$out" --owner "${META_REPO%%/*}" \
         ${args[@]+"${args[@]}"} > /dev/null 2>"$TMP/rules.err"; then
    rules_say "$what" "not evaluated ($(tail -1 "$TMP/rules.err" | sed 's/^release_git.py: //'))"
    return 0
  fi
  while IFS= read -r line; do
    rules_say "$what" "$line"
  done < "$out/log.txt"
  [ "$SPLICE_MODE" = act ] || [ "$PINS_MODE" = act ] || return 0
  [ ! -s "$out/body.md" ] || RULES_BODY="$out/body.md"
  [ ! -s "$out/comment.md" ] || RULES_COMMENT="$out/comment.md"
  RULES_TRAILERS=$(cat "$out/trailers.txt")
  [ -z "$RULES_TRAILERS" ] || RULES_ACTED=1
  # The new list: what the rules report as still in conflict, plus every entry
  # of the old list they never saw. The rules read the unmerged paths of the
  # index, and the list can hold more (base.yml, when merge-keep-target's own
  # merge conflicts in a file git had staged clean): such an entry stays.
  python3 -I - "$out/report.json" "$files.all" > "$files" <<'PY' || { cp "$files.all" "$files"; rules_say "$what" "the rules' report could not be read back; every conflicted file stays in the list"; }
import json, sys
report = json.load(open(sys.argv[1]))
listed = [l for l in open(sys.argv[2]).read().split('\n') if l]
seen = set(report['resolved']) | set(report['remaining'])
print('\n'.join(sorted(set(report['remaining']) | {p for p in listed if p not in seen})), end='')
PY
  [ ! -s "$files" ] || echo >> "$files"
}

# rules_pr <repo> <git dir> <branch> <conflict list file> <what>: the rules
# resolved every conflict of the merge that is open in <git dir>. Never a direct
# push: the merge is committed without markers, pushed as <branch> and opened
# as a PR that needs one approval. Always ends the run.
rules_pr() {
  local repo="$1" dir="$2" branch="$3" files="$4" what="$5" sha url count
  local trailers="$RULES_TRAILERS"
  count=$(grep -c . "$files.all" || true)
  if [ "$DRY_RUN" = true ]; then
    git -C "$dir" merge --abort
    echo "dry run: would push $branch ($PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers) and open the PR \"Merge-up $CHILD into $PARENT (resolved by rule)\" against $PARENT for one approval; nothing pushed"
    close_superseded "$repo" "$PARENT" "$branch" "the PR on $branch"
    exit 0
  fi
  # R-lock (ds-tegra-distro): the lockfile it set is in this commit too. One trailer block.
  [ -z "$LOCK_TRAILER" ] || [ "$dir" != "$WT" ] || trailers="$LOCK_TRAILER"$'\n'"$trailers"
  git -C "$dir" commit --quiet -m "$MERGE_SUBJECT" -m "$trailers"
  sha=$(git -C "$dir" rev-parse HEAD)
  if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    branch_provenance "$dir" "$branch" "$sha"
    # The head itself must be the commit this run computes. A branch that only
    # starts with it holds commits a person pushed on top: that is a stop
    # signal, and nothing on such a branch is called resolved by rule.
    [ -n "$BRANCH_NOTE" ] || branch_head_check "$dir" "$branch" "$sha"
    if [ -n "$BRANCH_NOTE" ]; then
      # Someone else's branch, an earlier run's on an older parent, or this run's commit
      # with other commits on top: it is not the rules' result and nothing more, so it is
      # the conflict PR of the phase-0 path, with the label.
      url=$(conflict_pr "$repo" "$PARENT" "$branch" "$files.all") || die "could not open the conflict PR in $repo"
      rules_unclaim "$repo" "$url"
      close_superseded "$repo" "$PARENT" "$branch" "$url"
      echo "::error::merge-up: $what $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files.all")); the rules resolve it, but $branch already exists on origin with other content and is left as it is. Resolve $url, then dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${url##*/}"
      exit 1
    fi
    echo "$branch is already on origin at ${sha:0:7}: the commit this run computes, untouched; left as it is, reusing its PR"
  else
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: ${sha:0:7}, $PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers"
  fi
  url=$(gh pr list -R "$repo" --base "$PARENT" --head "$branch" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -z "$url" ]; then
    {
      echo "release-merge-up merged \`$CHILD\` into \`$PARENT\`. Git reported conflicts in $count file(s), and the merge-up rules resolved every one, so this branch has no conflict markers. The rules are mechanical: this PR is the review of what they did."
      echo
      [ -z "$RULES_BODY" ] || cat "$RULES_BODY"
      if [ -n "$LOCK_BODY_NOTE" ]; then
        echo
        echo "$LOCK_BODY_NOTE"
      fi
      echo
      echo "This PR needs one approval. Read the diff of the files above, then dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT If a result is wrong, fix the file on \`$branch\`, push, and get a review from someone other than the last pusher. If \`$PARENT\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one."
      echo
      echo "Run: $RUN_URL"
    } > "$TMP/rules-pr-body.md"
    url=$(gh pr create -R "$repo" --base "$PARENT" --head "$branch" \
            --title "Merge-up $CHILD into $PARENT (resolved by rule)" --body-file "$TMP/rules-pr-body.md") \
      || die "could not open the PR for $branch in $repo"
  fi
  rules_say "$what" "every conflict resolved by rule in $url ($count file(s)); the PR has no markers and waits for one approval"
  close_superseded "$repo" "$PARENT" "$branch" "$url"
  echo "::error::merge-up: $what $CHILD -> $PARENT: every conflict was resolved by rule in $url, which has no markers and waits for one approval. Then dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${url##*/}"
  exit 1
}

# ---- the two lines -----------------------------------------------------------

git fetch --quiet origin
KIND=$(python3 "$SCRIPTS/lines.py" --root "$REPO" parse "$CHILD" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])')
case "$KIND" in
  minor|patch) ;;
  main) die "$CHILD is a product main: it has no parent line to merge into" ;;
  *) die "$CHILD is not a release line of a configured product" ;;
esac
PRODUCT="${CHILD%%/*}"
HEADS="$TMP/heads"
git ls-remote --heads origin > "$HEADS"
WANT_PARENT=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" parent "$CHILD")
if [ -z "$PARENT" ]; then
  PARENT="$WANT_PARENT"
elif [ "$PARENT" != "$WANT_PARENT" ]; then
  die "--parent $PARENT, but the parent line of $CHILD is $WANT_PARENT"
fi
CHILD_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$CHILD^{commit}") || die "origin has no branch $CHILD"
PARENT_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$PARENT^{commit}") || die "origin has no branch $PARENT"
SHA7="${CHILD_TIP:0:7}"
MERGE_SUBJECT="Merge $CHILD into $PARENT"

# Both lines must track their own meta-dsensos line (create-branch wrote that).
git show "$CHILD_TIP:kas/include/base.yml" > "$TMP/child-base.yml"
git show "$PARENT_TIP:kas/include/base.yml" > "$TMP/parent-base.yml"
CHILD_META=$(python3 "$SCRIPTS/release_git.py" base-branch-get "$TMP/child-base.yml") || die "base.yml at $CHILD has no single meta-dsensos branch: line"
PARENT_META=$(python3 "$SCRIPTS/release_git.py" base-branch-get "$TMP/parent-base.yml") || die "base.yml at $PARENT has no single meta-dsensos branch: line"
[ "$CHILD_META" = "$CHILD" ] || die "base.yml at $CHILD tracks meta-dsensos $CHILD_META, expected $CHILD"
[ "$PARENT_META" = "$PARENT" ] || die "base.yml at $PARENT tracks meta-dsensos $PARENT_META, expected $PARENT"
PIN=$(python3 "$SCRIPTS/release_git.py" lock-pin "$CHILD_TIP" --git-dir "$REPO") || die "could not read the meta-dsensos pin at $CHILD_TIP"
# The merge keeps the parent's versions/, so the parent must have one (every
# line opened by create-branch does; restoring the target's versions files on a
# parent without it would fail with a bare pathspec error).
git cat-file -e "$PARENT_TIP:versions" 2>/dev/null || die "$PARENT has no versions/ directory at ${PARENT_TIP:0:7}; the merge keeps the target's versions/, so the line must have been opened by create-branch (or seeded with one)"

# ---- 1. green child tip ------------------------------------------------------

GREEN_SHA=""
GREEN_RUN=""
GREEN_NOTE=""
if [ -n "$CALLER_RUN" ] && [ "$CALLER_SHA" = "$CHILD_TIP" ]; then
  # Started by the green rc-build run of this very tip (workflow_run fires
  # only on success), so the tip is green without asking gh.
  GREEN_SHA="$CHILD_TIP"
  GREEN_RUN="$CALLER_RUN"
  GREEN_NOTE=" (the calling run: the green RC run that started this merge-up)"
else
  CANDIDATES=$(python3 "$SCRIPTS/release_git.py" walk-versions-only "$CHILD_TIP" --git-dir "$REPO")
  for sha in $CANDIDATES; do
    if RUN=$(gh run list -R "$DISTRO_REPO" --workflow "$RC_WORKFLOW" --commit "$sha" --branch "$CHILD" \
               --status success --json databaseId,number --limit 10 | python3 "$SCRIPTS/release_git.py" first-run); then
      GREEN_SHA="$sha"
      GREEN_RUN="$RUN"
      break
    fi
  done
  [ -n "$GREEN_SHA" ] || die "no successful $RC_WORKFLOW run on $CHILD for $(echo "$CANDIDATES" | paste -sd ' ') (the tip and the versions/-only commits before it)"
  [ "$GREEN_SHA" = "$CHILD_TIP" ] || GREEN_NOTE=" (tip $SHA7 only touches versions/)"
fi
# The conflict branch and the stale check use the green sha, not the tip: a
# promote bump on the child ([skip ci], versions/ only, no RC run of its own)
# changes nothing the merge keeps, so it must neither open a new conflict PR
# nor make an approved one stale (ENG-2233; sensor-portal 3.0.x hit it on
# 2026-11-04 with v3.0.1's bump on top of an approved PR).
DISTRO_CONFLICT_BRANCH="merge-up/$CHILD/${GREEN_SHA:0:7}"

# ---- helpers -------------------------------------------------------------------

# branch_provenance <git dir> <branch> <like> [<child tip>]: <branch> already
# exists on origin, so this run pushes nothing to it. Whether it is the bot's
# own work is read from the branch: with <like> (the conflict commit this run
# just made in <git dir>) its first commit above the parent must be that same
# merge, by tree, parents and committer; with an empty <like> (a conflict that
# leaves no markers, whose branch is the child itself) it must contain the
# child at <child tip>. Anything else was not produced by this run: the
# PR body says so (BRANCH_NOTE), and no rule and no bot finish applies to it.
BRANCH_NOTE=""
branch_provenance() {
  local dir="$1" branch="$2" like="$3" child_tip="${4:-}" remote="" out="" why=""
  BRANCH_NOTE=""
  if ! git -C "$dir" fetch --quiet origin "refs/heads/$branch" 2>/dev/null || ! remote=$(git -C "$dir" rev-parse --verify --quiet FETCH_HEAD); then
    why="it could not be fetched to look at"
  elif [ -n "$like" ]; then
    if ! out=$(python3 "$SCRIPTS/release_git.py" branch-origin "$remote" --git-dir "$dir" --parent "$like^1" --like "$like" 2>/dev/null); then
      why="its history could not be read"
    elif [ "$(head -1 <<<"$out" | cut -d' ' -f1)" != ours ]; then
      why=$(sed -n 2p <<<"$out")
    fi
  elif ! git -C "$dir" merge-base --is-ancestor "$child_tip" "$remote" 2>/dev/null; then
    why="it does not contain $CHILD at ${child_tip:0:7}"
  fi
  [ -n "$why" ] || return 0
  echo "$branch was not produced by this run: $why. No rule and no bot finish applies to it"
  summary "- $branch was not produced by this run: $why"
  BRANCH_NOTE="**This branch was not produced by this run.** \`$branch\` already existed on origin${remote:+ at \`${remote:0:7}\`}, and $why. The bot left it as it is, so the files listed above are what this run's merge conflicts in, not necessarily what the branch holds. No merge-up rule was applied to it, and the bot does not merge it on its own. Read the whole diff of this PR before you approve it."
  # R-lock (ds-tegra-distro): what the rule staged is in this run's commit, not on that branch.
  LOCK_OUTCOME=""
  LOCK_BODY_NOTE=""
  # The merge rules: the same holds for what they resolved.
  RULES_BODY=""
  RULES_COMMENT=""
}

# branch_head_check <git dir> <branch> <like>: after branch_provenance found
# that <branch> starts with the commit this run made (<like>): is its head
# that commit itself, with nothing on top? The same tree, the same parents, the
# bot's committer address and the exact message, as lock_pr asks of a branch
# it finishes. If not, someone pushed to the branch after the bot: the PR body
# says so (BRANCH_NOTE), nothing on it is called resolved by rule, and it is
# an ordinary conflict PR that needs a resolver and a different approver.
branch_head_check() {
  local dir="$1" branch="$2" like="$3" remote="" above="" why
  remote=$(git -C "$dir" rev-parse --verify --quiet FETCH_HEAD) || remote=""   # branch_provenance fetched it
  if [ -n "$remote" ] \
     && [ "$(git -C "$dir" rev-parse "$remote^{tree}")" = "$(git -C "$dir" rev-parse "$like^{tree}")" ] \
     && [ "$(git -C "$dir" rev-parse "$remote^@" 2>/dev/null | paste -sd ' ')" = "$(git -C "$dir" rev-parse "$like^@" | paste -sd ' ')" ] \
     && [ "$(git -C "$dir" show -s --format=%ce "$remote")" = "$GIT_COMMITTER_EMAIL" ] \
     && [ "$(git -C "$dir" show -s --format=%B "$remote")" = "$(git -C "$dir" show -s --format=%B "$like")" ]; then
    return 0
  fi
  [ -z "$remote" ] || above=$(git -C "$dir" rev-list --count "$remote" "^$like^1" "^$like^2" 2>/dev/null) || above=""
  if [ -n "$above" ] && [ "$above" -gt 1 ]; then
    why="it starts with the commit this run computes, and $((above - 1)) more commit(s) were pushed on top of it"
  else
    why="its head${remote:+ ${remote:0:7}} has the tree this run computes, but not the bot's committer address and message"
  fi
  echo "$branch holds commits this run did not make: $why. That is a stop signal: nothing on it is called resolved by rule, and no bot finish applies to it"
  summary "- $branch holds commits this run did not make: $why"
  BRANCH_NOTE="**This branch holds commits this run did not make.** \`$branch\` already existed on origin${remote:+ at \`${remote:0:7}\`}, and $why. The bot left it as it is, so the files listed above are what this run's merge conflicts in, not necessarily what the branch holds. Nothing on it counts as resolved by rule, and the bot does not merge it on its own: it needs a resolver and an approver other than the last pusher. Read the whole diff of this PR before you approve it."
  # R-lock (ds-tegra-distro) and the merge rules: what they staged is this run's
  # commit, and the branch is no longer that commit alone.
  LOCK_OUTCOME=""
  LOCK_BODY_NOTE=""
  RULES_BODY=""
  RULES_COMMENT=""
}

# rules_unclaim <repo> <url>: the PR that is reused on a branch with
# BRANCH_NOTE may be one an earlier run opened as "(resolved by rule)", before
# someone pushed to the branch. Its title must not go on saying so: it becomes
# the conflict title, with the note as a comment. A failure is a warning.
rules_unclaim() {
  local repo="$1" url="$2" number="${2##*/}" title
  title=$(gh pr view "$number" -R "$repo" --json title --jq .title 2>/dev/null) || return 0
  [[ "$title" == *"(resolved by rule)" ]] || return 0
  if gh pr edit "$number" -R "$repo" --title "Merge-up $CHILD into $PARENT (conflict)" >/dev/null 2>"$TMP/unclaim.err" \
     && gh pr comment "$number" -R "$repo" --body "$BRANCH_NOTE"$'\n\n'"This PR was opened as \"(resolved by rule)\". It is retitled, because that no longer describes its branch. Run: $RUN_URL" >/dev/null 2>"$TMP/unclaim.err"; then
    echo "$url was opened as \"(resolved by rule)\"; retitled as a conflict PR, with a comment that says why"
  else
    echo "::warning::merge-up: $url still says \"(resolved by rule)\" and could not be retitled or commented ($(head -1 "$TMP/unclaim.err")); its branch holds commits this run did not make"
  fi
}

# conflict_pr <repo> <base> <head> <conflict file list file> [<no-marker list file>]
# -> prints the PR url. With the fifth argument the branch is the child tip
# (see on_conflict), and the body says so.
conflict_pr() {
  local repo="$1" base="$2" head="$3" files="$4" nontext="${5:-}" title body url
  title="Merge-up ${CHILD} into ${PARENT} (conflict)"
  body="$TMP/pr-body.md"
  {
    echo "release-merge-up stopped: merging \`$CHILD\` into \`$PARENT\` conflicts in:"
    echo
    sed 's/^/- `/; s/$/`/' "$files"
    echo
    if [ -n "$nontext" ]; then
      echo "Some of these conflicts leave no text markers, so they cannot be committed for you to edit:"
      echo
      sed 's/^/- `/; s/$/`/' "$nontext"
      echo
      echo "(a binary file, or a file one side deleted or renamed). This branch is therefore \`$CHILD\` itself, and GitHub shows the PR as conflicting with no hunks; checks and reviews start once it is mergeable. To resolve: check out \`$head\`, merge \`$base\` into it, decide every file above (keep, delete or replace it as the merge needs), commit, push. Then get a review from someone other than the last pusher and dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT"
    else
    echo "This branch is \`$base\` with \`$CHILD\` merged in. The conflict markers are committed in the files above, so the diff of this PR shows every hunk to decide; everything else merged clean (the parent keeps its \`versions/\` files and its meta-dsensos branch name)."
    echo
    echo "To resolve: check out \`$head\`, edit the files above until no \`<<<<<<<\`, \`|||||||\` or \`>>>>>>>\` line remains, commit, push. Get a review from someone other than the last pusher, then dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT The bot refuses to finish while a marker remains. If \`$base\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one (the branch stays, so a resolution started here can be merged into the new branch)."
    fi
    if [ -n "$BRANCH_NOTE" ]; then
      echo
      echo "$BRANCH_NOTE"
    fi
    if [ -n "$LOCK_BODY_NOTE" ]; then
      echo
      echo "$LOCK_BODY_NOTE"
    fi
    if [ -n "$RULES_BODY" ]; then
      echo
      cat "$RULES_BODY"
    fi
    echo
    echo "Run: $RUN_URL"
  } > "$body"
  # A re-run after an unresolved conflict reuses the open PR.
  url=$(gh pr list -R "$repo" --base "$base" --head "$head" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -n "$url" ]; then
    echo "$url"
    return 0
  fi
  gh pr create -R "$repo" --base "$base" --head "$head" --title "$title" --body-file "$body" || return 1
  # this run opened the PR (conflict_pr runs in a subshell, so the caller reads a file)
  : > "$TMP/pr-created"
}

# rules_comment <repo> <url>: on a conflict PR this run opened, what the rules
# would have written to the never_auto files, as a comment with a patch. A
# failure is a warning: the PR is open either way.
rules_comment() {
  local repo="$1" url="$2"
  [ -n "$RULES_COMMENT" ] && [ -f "$TMP/pr-created" ] || return 0
  if gh pr comment "${url##*/}" -R "$repo" --body "$(cat "$RULES_COMMENT")" >/dev/null 2>"$TMP/rules-comment.err"; then
    echo "commented on $url with the rules' candidate for the never_auto file(s)"
  else
    echo "::warning::merge-up: could not comment on $url with the rules' candidate ($(head -1 "$TMP/rules-comment.err"))"
  fi
}

# close_superseded <repo> <base> <keep head> <replacement>: close every other
# open merge-up/<child>/* PR against <base> in <repo> (ENG-2233). Each RC of
# the child that conflicts opens merge-up/<child>/<sha7> for its own tip, so
# without this the PRs pile up: on 2026-11-04 sensor-portal had four open for
# one sudoers conflict and meta-dsensos three. A superseded PR can never be
# finished (finish_pr refuses a head without the current child tip), so it is
# closed with a comment naming <replacement> (a PR url or a commit). Branches
# are left alone: a resolver may have pushed work there. A dry run prints
# what it would close. A failed list or close is a warning, never fatal: the
# merge-up itself already happened.
close_superseded() {
  local repo="$1" base="$2" keep="$3" replacement="$4" n h
  if ! gh pr list -R "$repo" --base "$base" --state open --limit 1000 --json number,headRefName \
         --jq '.[] | "\(.number) \(.headRefName)"' > "$TMP/open-prs" 2>"$TMP/open-prs.err"; then
    echo "::warning::merge-up: could not list open PRs against $base in $repo ($(head -1 "$TMP/open-prs.err")); superseded merge-up PRs, if any, are left open"
    return 0
  fi
  while read -r n h; do
    [ -n "$n" ] || continue
    [[ "$h" == merge-up/$CHILD/* ]] || continue
    [ "$h" != "$keep" ] || continue
    if [ "$DRY_RUN" = true ]; then
      echo "dry run: would close superseded PR $repo#$n ($h), replaced by $replacement"
      continue
    fi
    if gh pr close "$n" -R "$repo" --comment "Superseded by $replacement: \`$CHILD\` moved on, and release-merge-up does not finish a PR whose head lacks the current \`$CHILD\` tip. The branch \`$h\` is kept. If you started a resolution here, merge it into the new branch to carry it over." >/dev/null 2>"$TMP/close.err"; then
      echo "closed superseded PR $repo#$n ($h), replaced by $replacement"
    else
      echo "::warning::merge-up: could not close superseded PR $repo#$n ($h): $(head -1 "$TMP/close.err")"
    fi
  done < "$TMP/open-prs"
}

# ---- a dead branch: a PR a person closed (decided 2026-11-18) -----------------

DEAD_ASKED=""       # "<repo> <branch>" once branch_dead has read that branch's PRs
DEAD=""             # set when that branch is dead
DEAD_URL=""         # the PR a person closed (empty when the PR list could not be read)
DEAD_WHY=""         # who closed it, or what could not be read
BRANCH_LATEST=""    # "<number> <state> <url>" of that branch's open PR, else of its most recent one, or nothing

# branch_dead <repo> <branch>: is merge-up/<child>/<sha7> dead? It is when no
# PR with that head against the parent is open and the most recent one (highest
# number) was closed without a merge by a person: anyone but the bot's own
# supersession, which is read as today (closed by a Bot account that left
# close_superseded's comment; release_git.py person-no). No PR, an open one (a
# reopened one included, also an older one reopened after a newer one of the
# head was closed) or a merged one: not dead. What cannot be read (the PR list, who closed it) counts
# as dead, and DEAD_WHY says it could not be read. Returns 0 when dead. One read
# per branch and run. How the run was started does not matter.
branch_dead() {
  local repo="$1" branch="$2" latest="" number state url why=""
  if [ "$DEAD_ASKED" = "$repo $branch" ]; then
    [ -n "$DEAD" ] && return 0
    return 1
  fi
  DEAD_ASKED="$repo $branch"
  DEAD=""
  DEAD_URL=""
  DEAD_WHY=""
  BRANCH_LATEST=""
  if ! gh pr list -R "$repo" --base "$PARENT" --head "$branch" --state all --limit 100 --json number,state,url > "$TMP/head-prs.json" 2>"$TMP/dead.err" \
     || ! latest=$(python3 "$SCRIPTS/release_git.py" latest-pr "$TMP/head-prs.json" 2>>"$TMP/dead.err"); then
    DEAD=1
    DEAD_WHY="the pull requests of $branch in $repo could not be read ($(tail -1 "$TMP/dead.err")), so nothing shows that no person closed one"
    return 0
  fi
  BRANCH_LATEST="$latest"
  [ -n "$latest" ] || return 1
  read -r number state url <<<"$latest"
  [ "$state" = CLOSED ] || return 1
  if gh api "repos/$repo/issues/$number" > "$TMP/head-issue.json" 2>"$TMP/dead.err" \
     && gh api --paginate "repos/$repo/issues/$number/comments" > "$TMP/head-comments.json" 2>>"$TMP/dead.err" \
     && why=$(python3 "$SCRIPTS/release_git.py" person-no --state CLOSED --issue "$TMP/head-issue.json" --comments "$TMP/head-comments.json" 2>>"$TMP/dead.err"); then
    [ -n "$why" ] || return 1   # the bot's own supersession
  else
    why="who closed it could not be read ($(tail -1 "$TMP/dead.err"))"
  fi
  DEAD=1
  DEAD_URL="$url"
  DEAD_WHY="$why"
  return 0
}

# dead_finding <branch>: what branch_dead found, as one clause.
dead_finding() {
  if [ -n "$DEAD_URL" ]; then
    echo "a person closed $DEAD_URL without merging ($DEAD_WHY), so $1 is treated as closed"
  else
    echo "$DEAD_WHY, so $1 is treated as closed"
  fi
}

# dead_stop <what> <repo> <branch>: the end of a run on a dead branch. Nothing
# is opened, pushed, merged or resolved by rule there, and the closed PR is
# neither reopened nor commented on. Ends like a conflict left for people:
# ::error::, a summary line, exit 1. A dry run prints the same finding and
# what a real run would not do, and exits 0.
dead_stop() {
  local what="$1" repo="$2" branch="$3" finding ways next
  finding=$(dead_finding "$branch")
  # The branch is named after the child tip of the repo it is in. In the layer
  # that is the tip of meta-dsensos <child>, which a new green commit on
  # ds-tegra-distro <child> does not move.
  if [ "$what" = meta-dsensos ]; then
    next="the next commit on meta-dsensos $CHILD gets a new branch and a new PR by itself, at the first merge-up run of $CHILD after it (the branch is named after the tip of meta-dsensos $CHILD, so a new green commit on ds-tegra-distro $CHILD that leaves that tip where it is stops here again)"
  else
    next="the next green commit on $CHILD gets a new branch and a new PR by itself"
  fi
  if [ -n "$DEAD_URL" ]; then
    ways="Two ways forward: $next; or a person reopens $DEAD_URL (the branch then counts as open again) and resolves or finishes it as usual, with a dispatch of release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${DEAD_URL##*/}"
  else
    ways="Two ways forward: $next; or, if a person closed the last PR of $branch, a person reopens it and resolves or finishes it as usual (dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=<its number>). If no person closed one, the next run that can read the pull requests goes on as normal"
  fi
  if [ "$DRY_RUN" = true ]; then
    echo "$what: dry run: $finding. A real run would open no pull request on $branch, push nothing to it, merge nothing from it and apply no merge-up rule to it, and would end with an error. $ways"
    summary "- $what: dry run: $finding; a real run would do nothing on $branch"
    echo "dry run: nothing pushed"
    exit 0
  fi
  echo "$what: $finding. No pull request is opened on $branch, nothing is pushed to it or merged from it, and no merge-up rule is applied"
  summary "- $what: $finding. Nothing was opened, pushed, merged or resolved by rule on $branch"
  echo "::error::merge-up: $what $CHILD -> $PARENT: $finding. This run opens no pull request on $branch, pushes nothing to it, merges nothing from it and applies no merge-up rule to it. $ways"
  exit 1
}

# branch_gate <what> <repo> <git dir> <branch> <child tip>: a merge that
# conflicts needs merge-up/<child>/<sha7>, unless an open merge-up PR on
# another branch already contains the child (that PR is reused and <branch> is
# not touched). Otherwise the run stops here when <branch> is dead. Runs before
# any rule looks at the merge; a second call for the same branch asks nothing.
branch_gate() {
  local what="$1" repo="$2" dir="$3" branch="$4" tip="$5"
  if [ "$RULES_ASKED" != "$repo $branch" ]; then
    RULES_EXISTING=$(containing_pr "$repo" "$PARENT" "$dir" "$tip" "$branch")
    RULES_ASKED="$repo $branch"
  fi
  [ -z "$RULES_EXISTING" ] || return 0
  ! branch_dead "$repo" "$branch" || dead_stop "$what" "$repo" "$branch"
}

# lock_branch_gate: branch_gate for the ds-tegra-distro merge, before the
# lockfile pin rule acts. A lockfile-only conflict the rule pushes directly
# (outcome A, lock_pin.exact=push) needs no branch and is not held up; a dry run on a planned tip
# cannot tell yet (lock_rule_apply says so). The rule's own PR (outcomes B and
# P) is always on merge-up/<child>/<sha7>, so nothing else can stand in for a
# dead branch there.
lock_branch_gate() {
  if [ "$LOCK_MODE" = act ] && [ -n "$LOCK_VERDICT" ] && [ "$CONFLICTS" = "$LOCK" ]; then
    case "$LOCK_VERDICT:$LOCK_WOULD" in
      planned:*|*:A) return 0 ;;
      *:B|*:P)
        ! branch_dead "$DISTRO_REPO" "$DISTRO_CONFLICT_BRANCH" || dead_stop ds-tegra-distro "$DISTRO_REPO" "$DISTRO_CONFLICT_BRANCH"
        return 0 ;;
    esac
  fi
  branch_gate ds-tegra-distro "$DISTRO_REPO" "$WT" "$DISTRO_CONFLICT_BRANCH" "$GREEN_SHA"
}

# containing_pr <repo> <base> <git dir> <child tip> <skip head>: "<number>
# <head>" of an open merge-up/<child>/* PR against <base> (other than <skip
# head>) whose head already contains <child tip>, or nothing. That is a
# resolver who followed the stale message and merged the newer child into an
# older conflict branch: that PR is current, so it is reused instead of being
# superseded by a fresh one (ENG-2233).
containing_pr() {
  local repo="$1" base="$2" dir="$3" tip="$4" skip="$5" n h oid
  gh pr list -R "$repo" --base "$base" --state open --limit 1000 --json number,headRefName,headRefOid \
    --jq '.[] | "\(.number) \(.headRefName) \(.headRefOid)"' > "$TMP/open-prs-oid" 2>/dev/null || return 0
  while read -r n h oid; do
    [ -n "$n" ] || continue
    [[ "$h" == merge-up/$CHILD/* ]] || continue
    [ "$h" != "$skip" ] || continue
    git -C "$dir" cat-file -e "$oid^{commit}" 2>/dev/null \
      || git -C "$dir" fetch --quiet origin "refs/heads/$h" 2>/dev/null || continue
    git -C "$dir" cat-file -e "$oid^{commit}" 2>/dev/null || continue
    if git -C "$dir" merge-base --is-ancestor "$tip" "$oid"; then
      echo "$n $h"
      return 0
    fi
  done < "$TMP/open-prs-oid"
}

# nontext_conflicts <git dir> <conflict list file>: the conflicted paths whose
# work-tree copy has no text conflict marker: a binary file, a modify/delete
# or rename/delete (one side's file is left as it is), a file left missing.
# Committing those with `git add -A` would make them look resolved, with
# nothing in the PR diff and nothing for finish_pr's marker check to find.
nontext_conflicts() {
  local dir="$1" files="$2" f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if [ ! -f "$dir/$f" ] || ! grep -qaE '^(<<<<<<< |>>>>>>> )' "$dir/$f"; then
      echo "$f"
    fi
  done < "$files"
}

# on_conflict <repo> <git dir> <branch> <child tip> <conflict list file> <what>:
# the end of a merge that conflicted; the merge is still open in <git dir>.
# Reuses an open PR that already contains <child tip>; else opens the conflict
# PR: the merge with its markers committed (ENG-2200), or, when a conflict
# leaves no text markers, <child tip> itself (the pre-ENG-2200 shape, which
# GitHub shows as conflicting). Then supersession. Exits 0 on a dry run, else 1.
on_conflict() {
  local repo="$1" dir="$2" branch="$3" tip="$4" files="$5" what="$6" existing nontext="" url
  if [ "$RULES_ASKED" = "$repo $branch" ]; then
    existing="$RULES_EXISTING"   # branch_gate asked a moment ago
  else
    existing=$(containing_pr "$repo" "$PARENT" "$dir" "$tip" "$branch")
  fi
  if [ -n "$existing" ]; then
    git -C "$dir" merge --abort
    echo "$what: open conflict PR $repo#${existing%% *} (${existing#* }) already contains $CHILD at ${tip:0:7}; reusing it, no new branch"
    close_superseded "$repo" "$PARENT" "${existing#* }" "PR #${existing%% *}"
    [ "$DRY_RUN" = false ] || exit 0
    echo "::error::merge-up: $what $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $repo#${existing%% *}, then dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${existing%% *}"
    exit 1
  fi
  nontext=$(nontext_conflicts "$dir" "$files")
  if [ -n "$nontext" ]; then
    echo "$nontext" > "$TMP/nontext"
    if [ "$LOCK_OUTCOME" = C ]; then
      # The branch below is the child itself, so the rule's lockfile is not on
      # it: the lockfile is a conflict to decide again, with the rule's answer.
      echo "$LOCK" >> "$files"
      sort -u -o "$files" "$files"
      LOCK_BODY_NOTE="\`$LOCK\` conflicts as well (both lines moved the meta-dsensos pin). The lockfile pin rule's answer is the tip of meta-dsensos \`$PARENT\`, \`$META_PARENT_TIP\`, which contains both pins: set the meta-dsensos commit to it when you merge."
    fi
    git -C "$dir" merge --abort
    echo "$what: no text markers in: $(paste -sd ' ' "$TMP/nontext") (binary, or deleted or renamed on one side); the conflict PR goes on $CHILD itself at ${tip:0:7}"
    if [ "$DRY_RUN" = true ]; then
      echo "dry run: would push $branch at ${tip:0:7} (the child, no markers committed) and open the conflict PR against $PARENT; nothing pushed"
      close_superseded "$repo" "$PARENT" "$branch" "the new conflict PR on $branch"
      exit 0
    fi
    if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
      echo "$branch already exists on origin; left as it is (resolution in progress?), reusing its PR"
      branch_provenance "$dir" "$branch" "" "$tip"
    else
      git -C "$dir" push --quiet origin "$tip:refs/heads/$branch"
      echo "pushed $branch at the child tip ${tip:0:7}"
    fi
  else
    if [ "$DRY_RUN" = true ]; then
      git -C "$dir" merge --abort
      if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
        echo "dry run: $branch already exists on origin; a real run leaves it as it is and reuses its PR; nothing pushed"
      else
        echo "dry run: would push $branch ($PARENT plus the merge of $CHILD, markers in the files above) and open the conflict PR against $PARENT; nothing pushed"
      fi
      close_superseded "$repo" "$PARENT" "$branch" "the new conflict PR on $branch"
      exit 0
    fi
    commit_conflicts "$dir" "$branch" "$files"
    # A branch this run did not produce holds none of the rules' results: every
    # file the merge conflicts in is still to decide there, not only the ones
    # the rules left (rules_resolve keeps the full list in <list>.all).
    if [ -n "$BRANCH_NOTE" ] && [ -f "$files.all" ]; then
      cp "$files.all" "$files"
    fi
  fi
  rm -f "$TMP/pr-created"
  url=$(conflict_pr "$repo" "$PARENT" "$branch" "$files" "${nontext:+$TMP/nontext}") || die "could not open the conflict PR in $repo"
  # A reused PR may be one an earlier run opened as "(resolved by rule)": on a
  # branch this run did not produce, or that holds commits it did not make,
  # that title goes, here as in rules_pr.
  if [ -n "$BRANCH_NOTE" ] && [ ! -e "$TMP/pr-created" ]; then
    rules_unclaim "$repo" "$url"
  fi
  rules_comment "$repo" "$url"
  close_superseded "$repo" "$PARENT" "$branch" "$url"
  echo "::error::merge-up: $what $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $url, then dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${url##*/}"
  exit 1
}

# commit_conflicts <git dir> <branch> <conflict file list file>: inside the
# conflicted merge, stage everything as it stands (the unresolved files keep
# their markers), commit the merge, and push it as <branch> unless origin
# already has that branch (a re-run: a resolver may have pushed to it, and
# the open PR is reused; branch_provenance then says whether that branch is
# this run's own merge, and when a rule wrote into this run's commit,
# branch_head_check whether it is that commit and nothing more). Prints what
# it did.
commit_conflicts() {
  local dir="$1" branch="$2" files="$3"
  local trailers=""
  local -a msg=(-m "$MERGE_SUBJECT (conflicts to resolve)" -m "Conflict markers left in: $(paste -sd ' ' "$files")")
  git -C "$dir" add -A
  if [ "$LOCK_OUTCOME" = C ] && [ "$dir" = "$WT" ]; then
    trailers="$LOCK_TRAILER"
  fi
  # the merge rules: one Merge-Up-Rule line per rule that wrote into this commit
  [ -z "$RULES_ACTED" ] || trailers="${trailers:+$trailers"$'\n'"}$RULES_TRAILERS"
  [ -z "$trailers" ] || msg+=(-m "$trailers")
  git -C "$dir" commit --quiet "${msg[@]}"
  if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    echo "$branch already exists on origin; left as it is (resolution in progress?), reusing its PR"
    branch_provenance "$dir" "$branch" "$(git -C "$dir" rev-parse HEAD)"
    # This run's commit carries a rule's result: a PR body may only say so of a
    # branch whose head is that commit itself (see branch_head_check).
    if [ -z "$BRANCH_NOTE" ] && [ -n "$trailers" ]; then
      branch_head_check "$dir" "$branch" "$(git -C "$dir" rev-parse HEAD)"
    fi
  else
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: $(git -C "$dir" rev-parse --short HEAD), $PARENT plus the merge of $CHILD with markers in $(paste -sd ' ' "$files")"
  fi
}

# strict_commits <repo> <git dir> <head ref> <head sha> <child tip>: the
# fallback of finish.strict when the repository activity cannot be read. Prints
# the REST commit object of every commit on the PR branch that is neither on
# the parent nor on the child, the bot's own merge aside (the first commit
# above the parent, when the bot committed it): the commits people pushed.
# Returns 1 when the branch or a commit cannot be read.
strict_commits() {
  local repo="$1" dir="$2" head_ref="$3" head_sha="$4" child_tip="$5" origin first="" sha
  git -C "$dir" fetch --quiet origin "refs/heads/$head_ref" 2>/dev/null || return 1
  git -C "$dir" cat-file -e "$head_sha^{commit}" 2>/dev/null || return 1
  origin=$(python3 "$SCRIPTS/release_git.py" branch-origin "$head_sha" --git-dir "$dir" \
             --parent "refs/remotes/origin/$PARENT" --bot-email "$GIT_COMMITTER_EMAIL" 2>/dev/null | head -1) || return 1
  [ "${origin%% *}" != ours ] || first="${origin#* }"
  for sha in $(git -C "$dir" rev-list "$head_sha" "^refs/remotes/origin/$PARENT" "^refs/remotes/origin/$CHILD" "^$child_tip"); do
    [ "$sha" != "$first" ] || continue
    gh api "repos/$repo/commits/$sha" 2>/dev/null || return 1
    echo
  done
}

# finish_pr <repo> <number> <git dir> <child tip in that repo>: the bot
# self-check, the merge, and the post-check on <parent>.
finish_pr() {
  local repo="$1" number="$2" dir="$3" child_tip="$4" head_sha head_ref marked base_sha want required
  local -a changed=() strict=()
  # One view: the approval, the checks and the head they belong to are read
  # together, and the merge below is pinned to that head (--match-head-commit),
  # so a push after this point makes GitHub refuse the merge instead of the
  # bot merging an unchecked commit with its ruleset bypass.
  gh pr view "$number" -R "$repo" --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits,headRefOid,headRefName > "$TMP/pr.json"
  # The reviews come from the REST API, which says who is a person: a review
  # by github-actions, Copilot or any App never counts toward a finish
  # (ENG-2234; the view's reviewDecision is an aggregate a bot can turn green).
  gh api --paginate "repos/$repo/pulls/$number/reviews" > "$TMP/pr-reviews.json" 2>"$TMP/pr-reviews.err" \
    || die "PR $repo#$number: its reviews could not be read ($(head -1 "$TMP/pr-reviews.err")), so nothing shows a person approved it"
  head_sha=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$TMP/pr.json")
  head_ref=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefName"])' "$TMP/pr.json")
  # merge_up.finish.strict: the approval is on this head, the approver pushed
  # nothing to the branch, and every required check is green on the head.
  # CONFIG_STRICT is the value read and printed at the top of the run, where a
  # failed read stops it. Reading it again here, inside a test, would let a
  # read that fails pass for "not strict".
  if [ "$CONFIG_STRICT" = true ]; then
    # ds-tegra-distro: the layer repo has its own list of required checks.
    if [ "$repo" = "$META_REPO" ]; then required=$(cfg_names meta_dsensos.required_checks); else required=$(cfg_names finish.required_checks); fi
    strict=(--strict --required "$required")
    if gh api --paginate -X GET "repos/$repo/activity" -f "ref=refs/heads/$head_ref" > "$TMP/pr-activity.json" 2>"$TMP/pr-activity.err"; then
      strict+=(--activity "$TMP/pr-activity.json")
    else
      echo "strict finish:      the repository activity of $head_ref could not be read ($(head -1 "$TMP/pr-activity.err")); falling back to the authors and committers of the commits above the bot's commit"
      if strict_commits "$repo" "$dir" "$head_ref" "$head_sha" "$child_tip" > "$TMP/pr-above.json"; then
        strict+=(--commits "$TMP/pr-above.json")
      fi
    fi
  fi
  if ! python3 "$SCRIPTS/release_git.py" pr-check "$TMP/pr.json" --reviews "$TMP/pr-reviews.json" ${strict[@]+"${strict[@]}"}; then
    die "PR $repo#$number is not ready to merge (see the reasons above)"
  fi
  # The conflict PR was opened with the markers committed: none may remain.
  git -C "$dir" fetch --quiet origin "refs/heads/$head_ref" || die "could not fetch $head_ref from $repo"
  # A force-push between the view and the fetch would leave $head_sha unknown
  # here, and git grep on a missing object would pass the check silently.
  git -C "$dir" cat-file -e "$head_sha^{commit}" 2>/dev/null \
    || die "PR $repo#$number head $head_sha is not what origin/$head_ref holds; re-run"
  # Stale: the child moved on since the PR was opened. Merging it now would
  # land an old child tip and fail post_check after the merge (ENG-2233).
  git -C "$dir" merge-base --is-ancestor "$child_tip" "$head_sha" \
    || die "PR $repo#$number is stale: $CHILD is at ${child_tip:0:7} now and $head_ref does not contain it. Merge $CHILD into $head_ref and push, or let the next merge-up run of $CHILD open a fresh PR (it closes this one)."
  # Only the files this PR changes against the parent are checked: a doc or a
  # test fixture on the parent that shows a conflict must not block every
  # merge-up. The set comes from git, not from the conflict commit's message,
  # so a hand-made branch cannot narrow it.
  base_sha=$(git -C "$dir" merge-base "refs/remotes/origin/$PARENT" "$head_sha") \
    || die "PR $repo#$number: $head_ref shares no history with $PARENT"
  mapfile -d '' changed < <(git -C "$dir" diff -z --name-only --diff-filter=d "$base_sha" "$head_sha")
  marked=""
  if [ "${#changed[@]}" -gt 0 ]; then
    # zdiff3's base line too: a resolver who deletes only the <<<<<<< and
    # >>>>>>> lines leaves ||||||| , the old base text and ======= behind.
    marked=$(git -C "$dir" grep -l -E '^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )' "$head_sha" -- "${changed[@]/#/:(literal)}" 2>/dev/null | sed "s/^$head_sha://" || true)
  fi
  [ -z "$marked" ] || die "PR $repo#$number still has conflict markers in: $(echo "$marked" | paste -sd ' ')"
  # merge_up.lock_pin.verify_finish: the lockfile a person left must hold what
  # both lines locked and sit on the parent's own layer line.
  if [ "$repo" = "$DISTRO_REPO" ] && [ "$(lock_cfg lock_pin.verify_finish)" = true ]; then
    if [ -z "$META" ]; then
      echo "lock verifier:      not checked (no App token, the layer repo was not read)"
    elif python3 "$SCRIPTS/release_git.py" lock-verify --git-dir "$dir" --meta-dir "$META" --head "$head_sha" \
           --parent-tip "$PARENT_TIP" --child-pin "$PIN" --tip "$META_PARENT_TIP" ${META_TIP_PLANNED:+--planned} 2>"$TMP/lock-verify.err"; then
      :
    else
      sed 's/^release_git.py: lock-verify: /::error::merge-up: /' "$TMP/lock-verify.err"
      if [ -n "$META_TIP_PLANNED" ]; then want="the tip of meta-dsensos $PARENT (the merge a real run pushes)"; else want="the tip of meta-dsensos $PARENT, $META_PARENT_TIP,"; fi
      die "PR $repo#$number: the lockfile resolution is refused (merge_up.lock_pin.verify_finish). Set the meta-dsensos commit in $LOCK to $want and push"
    fi
  fi
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: would gh pr merge $number -R $repo --merge (head $head_sha)"
    return 0
  fi
  gh pr merge "$number" -R "$repo" --merge --match-head-commit "$head_sha"
  echo "merged PR $repo#$number (head $head_sha)"
  PR_MERGED_HEAD="$head_sha"
  PR_MERGED_REF="$head_ref"
}

# close_if_still_open <repo> <number> <merge commit> <head branch>: GitHub sometimes keeps a
# PR open after the bot merged it (meta-dsensos #182). The parent holds the
# merge, post_check has confirmed it, so the PR is closed with a comment that
# names the merge commit. A failure is a warning: the merge already happened.
close_if_still_open() {
  local repo="$1" number="$2" commit="$3" head="$4" url
  url=$(gh pr list -R "$repo" --base "$PARENT" --head "$head" --state open --json url --jq '.[0].url' 2>/dev/null) || return 0
  [ -n "$url" ] && [ "${url##*/}" = "$number" ] || return 0
  if gh pr close "$number" -R "$repo" --comment "Merged by release-merge-up: \`$PARENT\` holds this PR's head in the merge commit $commit. GitHub still showed the PR as open after the merge, so the bot closed it. Nothing is left to do here. Run: $RUN_URL" >/dev/null 2>"$TMP/close-merged.err"; then
    echo "PR $repo#$number was still open after its merge; closed it with a comment naming the merge commit $commit"
  else
    echo "::warning::merge-up: PR $repo#$number is still open after its merge at $commit and could not be closed ($(head -1 "$TMP/close-merged.err")); close it by hand"
  fi
}

# post_check <git dir> <new parent tip> <expected second parent> <child tip>
post_check() {
  local dir="$1" tip="$2" second="$3" child="$4" got
  got=$(git -C "$dir" rev-parse --verify --quiet "$tip^2") || die "post-check: $tip has no second parent (not a merge commit)"
  [ "$got" = "$second" ] || die "post-check: $tip^2 is $got, expected $second"
  git -C "$dir" merge-base --is-ancestor "$child" "$tip" || die "post-check: $child is not an ancestor of $tip"
  echo "post-check ok: $tip merges $second"
}

# ---- --pr: which repo holds it -------------------------------------------------

PR_REPO=""
PR_HEAD=""
if [ -n "$PR" ]; then
  for repo in "$DISTRO_REPO" "$META_REPO"; do
    if gh pr view "$PR" -R "$repo" --json headRefName,baseRefName,state,headRefOid > "$TMP/pr-$$.json" 2>/dev/null; then
      head=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("headRefName",""))' "$TMP/pr-$$.json")
      base=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("baseRefName",""))' "$TMP/pr-$$.json")
      state=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("state",""))' "$TMP/pr-$$.json")
      oid=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("headRefOid",""))' "$TMP/pr-$$.json")
      if [ "$state" = OPEN ] && [ "$base" = "$PARENT" ] && [[ "$head" == merge-up/$CHILD/* ]]; then
        [ -z "$PR_REPO" ] || die "PR #$PR is an open merge-up PR in both $PR_REPO and $repo; pass the right one"
        PR_REPO="$repo"
        PR_HEAD="$oid"
      fi
    fi
  done
  [ -n "$PR_REPO" ] || die "PR #$PR is not an open merge-up/$CHILD/* PR against $PARENT in $DISTRO_REPO or $META_REPO"
fi

# A finish belongs on the default branch. On another ref it still runs (the
# documented way back to a line's older scripts), with a warning that says
# whose checks it applies.
if [ -n "$PR" ] && [ -n "${DEFAULT_BRANCH:-}" ] && [ "$SCRIPTS_REF" != "$DEFAULT_BRANCH" ]; then
  echo "::warning::merge-up: this finish runs the scripts and release/config.json of $SCRIPTS_REF, not of the default branch $DEFAULT_BRANCH. Dispatch finishes on $DEFAULT_BRANCH"
  summary "- this finish runs the scripts and release/config.json of $SCRIPTS_REF, not of the default branch $DEFAULT_BRANCH"
fi

# ---- the plan so far ----------------------------------------------------------

echo "== merge-up plan (dry_run=$DRY_RUN, token=$TOKEN_KIND)"
echo "child:              $CHILD at $CHILD_TIP"
echo "parent:             $PARENT at $PARENT_TIP"
echo "green run:          $RC_WORKFLOW run ${GREEN_RUN#* } (id ${GREEN_RUN%% *}) on ${GREEN_SHA:0:7}$GREEN_NOTE"
echo "child pin:          meta-dsensos $PIN"
[ -z "$PR" ] || echo "finish PR:          $PR_REPO#$PR (head $PR_HEAD)"

if [ "$DRY_RUN" = false ] && [ "$TOKEN_KIND" != app ]; then
  echo "::error::merge-up: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): GITHUB_TOKEN cannot push to meta-dsensos and its pushes start no rc-build. Nothing was pushed. A human runs, in this order:"
  echo "::error::  git clone $META_HUMAN_URL && cd meta-dsensos && git checkout $PARENT && git merge --no-ff -m '$MERGE_SUBJECT' origin/$CHILD && git push origin $PARENT   # skip if $PIN is already in origin/$PARENT"
  echo "::error::  cd <ds-tegra-distro> && git fetch origin && git checkout $PARENT && git merge --no-ff --no-commit origin/$CHILD; python3 scripts-release/release_git.py merge-keep-target $PARENT && git commit -m '$MERGE_SUBJECT' && git push origin $PARENT"
  echo "::error::  gh workflow run $RC_WORKFLOW -R $DISTRO_REPO --ref $PARENT"
  exit 1
fi

# ---- 2. meta-dsensos --------------------------------------------------------------

META_CHILD_TIP=""
if [ "$TOKEN_KIND" != app ]; then
  echo "meta-dsensos:       not checked (no App token; a dry run with GITHUB_TOKEN cannot read the layer repo)"
  lock_invariant_check
else
  git clone --quiet --no-tags --branch "$PARENT" --single-branch "$META_URL" "$TMP/meta" \
    || die "meta-dsensos has no branch $PARENT (or the token cannot read it)"
  META="$TMP/meta"
  git -C "$META" fetch --quiet origin "refs/heads/$CHILD:refs/remotes/origin/$CHILD" \
    || die "meta-dsensos has no branch $CHILD"
  META_CHILD_TIP=$(git -C "$META" rev-parse "refs/remotes/origin/$CHILD")
  META_PARENT_TIP=$(git -C "$META" rev-parse "refs/remotes/origin/$PARENT")
  git -C "$META" cat-file -e "$PIN^{commit}" 2>/dev/null || die "meta-dsensos has no commit $PIN (the child's lockfile pin)"
  git -C "$META" merge-base --is-ancestor "$PIN" "$META_CHILD_TIP" \
    || die "the child's pin $PIN is not on meta-dsensos $CHILD ($META_CHILD_TIP); a build of $CHILD could not have passed kas containment"
  lock_invariant_check

  if [ -n "$PR" ] && [ "$PR_REPO" = "$META_REPO" ]; then
    PR_MERGED_HEAD=""
    finish_pr "$META_REPO" "$PR" "$META" "$META_CHILD_TIP"
    [ "$DRY_RUN" = false ] || META_PR_DRY=1
    [ "$DRY_RUN" = false ] || META_TIP_PLANNED=1
    if [ "$DRY_RUN" = false ]; then
      git -C "$META" fetch --quiet origin "refs/heads/$PARENT:refs/remotes/origin/$PARENT"
      META_PARENT_TIP=$(git -C "$META" rev-parse "refs/remotes/origin/$PARENT")
      post_check "$META" "$META_PARENT_TIP" "$PR_MERGED_HEAD" "$META_CHILD_TIP"
      close_if_still_open "$META_REPO" "$PR" "$META_PARENT_TIP" "$PR_MERGED_REF"
    fi
  fi

  if [ -n "${META_PR_DRY:-}" ]; then
    # Dry run of a meta-dsensos finish: once PR $PR merges, the parent holds the
    # pin, so its trial merge would only repeat the conflict the PR resolves.
    echo "meta-dsensos:       dry run: once PR #$PR merges, $PARENT contains the pin; its trial merge is skipped"
  elif git -C "$META" merge-base --is-ancestor "$PIN" "$META_PARENT_TIP"; then
    echo "meta-dsensos:       $PARENT ($META_PARENT_TIP) already contains the pin; nothing to merge"
    close_superseded "$META_REPO" "$PARENT" "" "$PARENT at ${META_PARENT_TIP:0:7}, which already contains the pin"
  else
    attempt=0
    while :; do
      attempt=$((attempt + 1))
      git -C "$META" checkout --quiet -B "$PARENT" "refs/remotes/origin/$PARENT"
      if git -C "$META" -c merge.conflictStyle=zdiff3 merge --no-ff --no-commit "refs/remotes/origin/$CHILD" > "$TMP/meta-merge.log" 2>&1; then
        META_CONFLICTS=""
      else
        git -C "$META" rev-parse --quiet --verify MERGE_HEAD >/dev/null || { cat "$TMP/meta-merge.log"; die "meta-dsensos: git merge failed (see above)"; }
        META_CONFLICTS=$(git -C "$META" diff --name-only --diff-filter=U)
      fi
      if [ -n "$META_CONFLICTS" ]; then
        echo "meta-dsensos:       merge $CHILD ($META_CHILD_TIP) into $PARENT conflicts in: $(echo "$META_CONFLICTS" | paste -sd ' ')"
        META_BRANCH="merge-up/$CHILD/${META_CHILD_TIP:0:7}"
        echo "$META_CONFLICTS" > "$TMP/meta-conflicts"
        RULES_ASKED=""
        branch_gate meta-dsensos "$META_REPO" "$META" "$META_BRANCH" "$META_CHILD_TIP"
        rules_resolve meta-dsensos "$META_REPO" "$META" "$META_BRANCH" "$META_CHILD_TIP" "$TMP/meta-conflicts" --layer
        [ -s "$TMP/meta-conflicts" ] || rules_pr "$META_REPO" "$META" "$META_BRANCH" "$TMP/meta-conflicts" meta-dsensos
        on_conflict "$META_REPO" "$META" "$META_BRANCH" "$META_CHILD_TIP" "$TMP/meta-conflicts" meta-dsensos
      fi
      echo "meta-dsensos:       merge $CHILD ($META_CHILD_TIP) into $PARENT ($META_PARENT_TIP): $(git -C "$META" diff --cached --stat | tail -1)"
      if [ "$DRY_RUN" = true ]; then
        git -C "$META" merge --abort
        META_TIP_PLANNED=1
        close_superseded "$META_REPO" "$PARENT" "" "the clean merge"
        break
      fi
      git -C "$META" commit --quiet -m "$MERGE_SUBJECT"
      if git -C "$META" push --quiet origin "HEAD:refs/heads/$PARENT" 2>"$TMP/meta-push.log"; then
        META_PARENT_TIP=$(git -C "$META" rev-parse HEAD)
        post_check "$META" "$META_PARENT_TIP" "$META_CHILD_TIP" "$META_CHILD_TIP"
        echo "meta-dsensos: pushed $PARENT at $META_PARENT_TIP"
        close_superseded "$META_REPO" "$PARENT" "" "the clean merge $META_PARENT_TIP"
        break
      fi
      [ $attempt -lt 3 ] || { cat "$TMP/meta-push.log"; die "meta-dsensos: push to $PARENT failed 3 times"; }
      echo "meta-dsensos: push rejected (attempt $attempt), refetching"
      git -C "$META" fetch --quiet origin "refs/heads/$PARENT:refs/remotes/origin/$PARENT"
      META_PARENT_TIP=$(git -C "$META" rev-parse "refs/remotes/origin/$PARENT")
    done
  fi
fi

# ---- 3. ds-tegra-distro ---------------------------------------------------------

if [ -n "$PR" ] && [ "$PR_REPO" = "$DISTRO_REPO" ]; then
  PR_MERGED_HEAD=""
  finish_pr "$DISTRO_REPO" "$PR" "$REPO" "$GREEN_SHA"
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: nothing pushed"
    exit 0
  fi
  git fetch --quiet origin
  NEW_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  post_check "$REPO" "$NEW_TIP" "$PR_MERGED_HEAD" "$GREEN_SHA"
  close_if_still_open "$DISTRO_REPO" "$PR" "$NEW_TIP" "$PR_MERGED_REF"
  close_superseded "$DISTRO_REPO" "$PARENT" "" "PR #$PR, merged at $NEW_TIP"
  echo "done: $CHILD merged into $PARENT at $NEW_TIP (via PR $PR)"
  exit 0
fi

if git merge-base --is-ancestor "$CHILD_TIP" "$PARENT_TIP"; then
  echo "ds-tegra-distro:    $PARENT already contains $CHILD ($SHA7); nothing to merge"
  close_superseded "$DISTRO_REPO" "$PARENT" "" "$PARENT at ${PARENT_TIP:0:7}, which already contains $CHILD"
  [ "$DRY_RUN" = false ] || echo "dry run: nothing pushed"
  exit 0
fi

attempt=0
while :; do
  attempt=$((attempt + 1))
  if [ -d "$WT" ]; then
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  git worktree add --quiet --detach "$WT" "$PARENT_TIP"
  if git -C "$WT" -c merge.conflictStyle=zdiff3 merge --no-ff --no-commit "$CHILD_TIP" > "$TMP/merge.log" 2>&1; then
    :
  else
    git -C "$WT" rev-parse --quiet --verify MERGE_HEAD >/dev/null || { cat "$TMP/merge.log"; die "ds-tegra-distro: git merge failed (see above)"; }
  fi
  # The target keeps its pending versions and its meta-dsensos branch name;
  # the rest of base.yml merges (release_git.py base-merge).
  BASE_CONFLICT=""
  python3 "$SCRIPTS/release_git.py" merge-keep-target "$PARENT" --git-dir "$WT" > /dev/null 2>&1 || BASE_CONFLICT=kas/include/base.yml
  CONFLICTS=$(printf '%s\n%s\n' "$(git -C "$WT" diff --name-only --diff-filter=U)" "$BASE_CONFLICT" | grep -v '^$' | sort -u || true)
  # R-lock: the lockfile is still unmerged, so both lines moved the pin.
  LOCK_OUTCOME=""
  LOCK_TRAILER=""
  LOCK_BODY_NOTE=""
  RULES_ASKED=""
  lock_rule_eval
  # A dead branch (a PR a person closed) stops the run before any rule acts.
  [ -z "$CONFLICTS" ] || lock_branch_gate
  [ -z "$LOCK_VERDICT" ] || lock_rule_apply
  if [ -n "$CONFLICTS" ]; then
    echo "ds-tegra-distro:    merge $CHILD ($SHA7) into $PARENT conflicts in: $(echo "$CONFLICTS" | paste -sd ' ')"
    echo "$CONFLICTS" > "$TMP/conflicts"
    # Again, for a merge the lockfile pin rule was to push directly and could not.
    branch_gate ds-tegra-distro "$DISTRO_REPO" "$WT" "$DISTRO_CONFLICT_BRANCH" "$GREEN_SHA"
    # The lockfile and base.yml have rules of their own (R-lock, merge-keep-target).
    rules_resolve ds-tegra-distro "$DISTRO_REPO" "$WT" "$DISTRO_CONFLICT_BRANCH" "$GREEN_SHA" "$TMP/conflicts" --skip "$LOCK" --skip kas/include/base.yml
    [ -s "$TMP/conflicts" ] || rules_pr "$DISTRO_REPO" "$WT" "$DISTRO_CONFLICT_BRANCH" "$TMP/conflicts" ds-tegra-distro
    on_conflict "$DISTRO_REPO" "$WT" "$DISTRO_CONFLICT_BRANCH" "$GREEN_SHA" "$TMP/conflicts" ds-tegra-distro
  fi
  echo "ds-tegra-distro:    merge $CHILD ($SHA7) into $PARENT (${PARENT_TIP:0:7}): $(git -C "$WT" diff --cached --stat | tail -1)"
  echo "                    kept from $PARENT: its versions/<product> files and the meta-dsensos branch $PARENT_META in base.yml (versions/README.md and the rest of base.yml merged); lockfile pin after merge: $(python3 "$SCRIPTS/release_git.py" lock-pin --file "$WT/kas/include/base.lock.yml" HEAD)"
  if [ "$DRY_RUN" = true ]; then
    git -C "$WT" merge --abort
    close_superseded "$DISTRO_REPO" "$PARENT" "" "the clean merge"
    echo "dry run: nothing pushed"
    exit 0
  fi
  if [ -n "$LOCK_TRAILER" ]; then
    git -C "$WT" commit --quiet -m "$MERGE_SUBJECT" -m "$LOCK_TRAILER"
  else
    git -C "$WT" commit --quiet -m "$MERGE_SUBJECT"
  fi
  if git -C "$WT" push --quiet origin "HEAD:refs/heads/$PARENT" 2>"$TMP/push.log"; then
    NEW_TIP=$(git -C "$WT" rev-parse HEAD)
    break
  fi
  [ $attempt -lt 3 ] || { cat "$TMP/push.log"; die "ds-tegra-distro: push to $PARENT failed 3 times"; }
  echo "ds-tegra-distro: push rejected (attempt $attempt), refetching"
  git fetch --quiet origin
  PARENT_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  lock_refetch_meta
done

# ---- 4. post-check -------------------------------------------------------------

git fetch --quiet origin
[ "$(git rev-parse "refs/remotes/origin/$PARENT")" = "$NEW_TIP" ] || echo "note: $PARENT moved again after the push"
post_check "$REPO" "$NEW_TIP" "$CHILD_TIP" "$CHILD_TIP"
[ "$LOCK_OUTCOME" != A ] || lock_commit_comment "$NEW_TIP"
close_superseded "$DISTRO_REPO" "$PARENT" "" "the clean merge $NEW_TIP"
echo "done: $CHILD merged into $PARENT at $NEW_TIP"
