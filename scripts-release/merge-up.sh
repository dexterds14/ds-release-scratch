#!/usr/bin/env bash
set -euo pipefail

# Merge a release line into its parent line (ENG-1935, ENG-2110). Run from a
# checkout whose origin is the repo (fetch-depth 0, push credentials set).
#
#   merge-up.sh --child <line> [--parent <line>] [--pr N [--auto-finish]] [--dry-run|--no-dry-run]
#
# 1. The child tip must have a green release-rc-build run (gh run list
#    --commit <sha> --branch <child> --status success); a trailing commit
#    that touches only versions/ is walked past. Started by the tip's own
#    green rc-build run (release-merge-up.yml on workflow_run: CALLER_SHA is
#    the tip and CALLER_RUN that run) the tip counts as green without the
#    lookup.
# 2. git merge --no-ff --no-commit origin/<child> into <parent>, then
#    `release_git.py merge-keep-target`: the target keeps its pending-version
#    files (versions/README.md merges like any doc) and its copy of every
#    bookkeeping file release/config.json lists, so those paths never
#    conflict. Everything else merges normally. Any conflict: commit the
#    merge with its markers (versions/ and the bookkeeping files already
#    kept), push it as merge-up/<child>/<sha7>, open the conflict PR
#    against <parent>, ::error::, exit 1 (ENG-2200: the PR diff is then
#    parent-relative and the markers are the only lines to decide; the
#    resolver edits the files on that branch). A branch that already
#    exists on origin is left alone (a resolver may have pushed to it) and
#    its open PR reused. Unless its first commit above the parent is the
#    bot's merge with the tree this run computed, the run and the body of a
#    PR it opens say the branch was not produced by this run; no rule and no
#    bot finish applies. Else commit "Merge <child> into <parent>" and
#    push (3 attempts on non-fast-forward).
# 3. Post-check: the parent tip's second parent is the child tip (or the
#    finished PR's head, whose own second parent is the child tip) and the
#    child tip is an ancestor of the parent.
# --pr N finishes a conflict PR: it must be APPROVED by a person other than
#    its last pusher (the reviews are read through the REST API and only
#    user.type "User" counts: a bot's or an App's approval never finishes a
#    PR; with merge_up.finish.strict the approval must be on the PR head, by
#    someone who pushed nothing to the branch according to the repository
#    activity, and every check of merge_up.finish.required_checks must be
#    green on the head), mergeStateStatus CLEAN, no failed check, and no
#    conflict marker (<<<<<<<, ||||||| or >>>>>>>) left in the files it
#    changes, no change against its merge base with <parent> to a
#    versions/<product> file or a bookkeeping file of <parent> (a resolution
#    that kept the child's copy), and its head must contain the child's last
#    green commit (a stale
#    PR is refused before anything merges; a versions-only bump on top does
#    not count); then gh pr merge --merge --match-head-commit and the
#    post-check. A PR that GitHub still shows open after that merge is
#    closed with a comment naming the merge commit. A conflict that leaves no text marker (binary, modify/delete)
#    opens the PR on the child instead, and an open merge-up PR whose head
#    already contains the child is reused.
# --auto-finish (ENG-2235; with --pr only): the finish release-auto-finish.yml
#    started. The strict checks apply whatever merge_up.finish.strict says (the
#    config is still read once and printed as it is; the switches block gains a
#    last line), and the PR is checked by auto_finish.py ready, the function the
#    sweep asked: also no person's latest review requests changes, the approval
#    is newer than the newest push to the branch, the approver has write
#    access, the branch activity was read (no fallback to the commit authors),
#    no bot but the release App pushed, no stop comment for this head is newer
#    than the approval, and mergeStateStatus is CLEAN (UNKNOWN, and UNSTABLE
#    while only the review trigger runs, are read again). A PR already merged
#    or closed ends the run green before anything else is read. After the
#    merge the bot comments on the PR with the approver, the head and a hidden
#    `merge-up-auto-finish: merged` line. A refusal inside the finish of that
#    PR (approval, checks, markers, bookkeeping, stale) leaves one comment with
#    a `merge-up-auto-finish: stopped head=<sha>` line, and the sweep leaves
#    that head alone until a newer approval. A read that failed, a check still
#    running, and anything outside the finish leave no comment. Never --admin.
# Supersession (ENG-2233): every outcome (a new conflict PR, a clean merge, a
#    finished PR, nothing to merge) closes the other open merge-up/<child>/*
#    PRs against <parent>, with a comment naming what replaced them; their
#    branches stay. The merge uses zdiff3 markers.
#
# Environment: TOKEN_KIND (app|github), DRY_RUN, GIT_BOT_NAME, GIT_BOT_EMAIL:
# as in create-branch.sh; GITHUB_REPOSITORY (the repo gh acts on); CALLER_SHA
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
# The merge rules (ENG-2234; rule name splice in the variable): when a merge
#    still conflicts after merge-keep-target, `release_git.py
#    resolve-conflicts` runs merge_rules.py on the conflicted files. splice
#    resolves two edits that touch but do not overlap. No rule writes a file
#    on merge_up.never_auto. In shadow each finding is logged and nothing
#    changes. A rule that acts writes its result into the merge before it is
#    committed:
#    - some conflicts remain: the conflict PR as before, on the commit that
#      holds the rule's results and the markers of the rest. Its body lists
#      what the rule did and why the rest was left, and the commit carries a
#      `Merge-Up-Rule:` trailer. For a never_auto file the rule has an answer
#      for, that answer is posted as a PR comment with a patch.
#    - every conflict is resolved: never a direct push. The merge is committed
#      as "Merge <child> into <parent>" with the trailer, pushed as
#      merge-up/<child>/<sha7>, and opened as "Merge-up <child> into <parent>
#      (resolved by rule)", a PR without markers that needs one approval and a
#      pr=N dispatch. A later run reuses the PR only while the branch head
#      is still that commit itself (its tree, its two parents, the bot's
#      committer address and the bot's message). The bot merges such a PR on
#      its own only under bot-finish (below).
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
# bot-finish (ENG-2235, 2c; an action word of DS_MERGE_UP_AUTOMATION, not a
#    rule): with it in the list, and splice acting, the bot merges the
#    "(resolved by rule)" PR itself, with no approval (bot_finish): it waits
#    for the checks of merge_up.finish.required_checks on the bot's commit as
#    it is on origin, each from its source in merge_up.finish.check_sources, at
#    most merge_up.finish.wait_minutes, then merges through the REST merge
#    pinned to that commit, only while it is still the head and the parent has
#    not moved. Never --admin, never gh pr merge. Eligible only when that
#    required list is not empty and no person said no to a PR this one
#    supersedes (an open or bot-closed merge-up PR of the child whose child
#    commit the parent does not hold yet, with a person's changes-requested
#    review, or closed by a person); anything that cannot be read is a no. A
#    PR that is not eligible waits for one approval as before, and the run
#    says why. Stops, each red with the PR left open: a dead branch, a commit
#    on top, changes requested before or during the wait, a close or merge by
#    someone else, a moved head, a red check or one from another source, the
#    timeout, a moved parent, a refused merge. A reused PR the bot now merges
#    gets one comment that says so; its body is not edited. The title stays.
#    Absent from a list, or under shadow, the word logs what it would do and
#    nothing changes; without splice acting it is inert and says so. The
#    switches block ends with its line. Scripts older than this refuse the
#    word: remove it before a revert or a dispatch on an older ref.
# A dead branch (decided 2026-11-18, with or without the variable):
#    merge-up/<child>/<sha7> is dead when no PR with that head against
#    <parent> is open and the most recent one was closed without a merge by a
#    person, that is by anyone but the bot's own supersession (closed by a Bot
#    account that left the "Superseded by" comment). An open PR of the head
#    keeps the branch alive whatever its number. What cannot be read (the PR list, who
#    closed it) counts as dead, and the run says it could not be read. On a
#    dead branch a run opens no PR, pushes nothing to the branch, merges
#    nothing from it and applies no rule to it, whatever the PR would have
#    been (resolved by rule, a conflict PR, the PR on the child itself) and
#    however the run was started (workflow_run or a dispatch by hand). It
#    ends like a conflict left for people: ::error::, a summary line, exit 1
#    (a dry run prints the finding and what it would not do, exit 0). The two
#    ways forward: the next green commit on the child has a new <sha7>, so a
#    new branch and a new PR; or a person reopens the closed PR, and the
#    branch is not dead any more. The bot never reopens or comments on that
#    PR. An open merge-up PR on another branch that already contains the
#    child is still reused, a --pr finish of an open PR is untouched, and so
#    is a clean merge, which needs no branch.
# Only an App token's push gets the
# ruleset bypass and starts the parent's rc-build; with TOKEN_KIND=github a
# real run stops before any push and prints the commands a human runs. A dry
# run prints the plan (green run, the trial merge and its conflicts) and
# exits 0.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
. "$SCRIPTS/lib.sh"

CHILD=""
PARENT=""
PR=""
AUTO_FINISH=false
# Set while a refusal of the auto-finish's PR leaves a stop comment (die does it).
AUTO_STOP_REPO="" AUTO_STOP_PR="" AUTO_STOP_HEAD="" AUTO_STOP_REASONS=""
# What auto_ready asked auto_finish.py ready besides the view and the reviews (auto_recheck asks again).
AUTO_READY_ARGS=()
DRY_RUN="${DRY_RUN:-true}"
TOKEN_KIND="${TOKEN_KIND:-github}"
OWN_REPO="${GITHUB_REPOSITORY:-}"
CALLER_SHA="${CALLER_SHA:-}"
CALLER_RUN="${CALLER_RUN:-}"
RC_WORKFLOW="release-rc-build.yml"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${OWN_REPO}/actions/runs/${GITHUB_RUN_ID:-local}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,72p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --child) CHILD="$2"; shift 2 ;;
    --parent) PARENT="$2"; shift 2 ;;
    --pr) PR="$2"; shift 2 ;;
    --auto-finish) AUTO_FINISH=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::merge-up: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::merge-up: $*"
  [ -z "$AUTO_STOP_HEAD" ] || auto_stop "$*"
  exit 1
}

# die_unmarked <text>: die without the auto-finish's stop comment: a read that
# failed or a state GitHub settles by itself is no refusal of the head.
die_unmarked() {
  AUTO_STOP_HEAD=""
  die "$@"
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
[ -n "$OWN_REPO" ] || die "GITHUB_REPOSITORY is not set (the owner/name gh acts on)"
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
if [ -n "$PR" ] && ! [[ "$PR" =~ ^[0-9]+$ ]]; then
  die "--pr must be a PR number, got '$PR'"
fi
[ "$AUTO_FINISH" != true ] || [ -n "$PR" ] || die "--auto-finish is only for a finish: it needs --pr N"

# ---- the switches (ENG-2234) --------------------------------------------------

# Printed before anything else so every run says which rules it ran under,
# whatever it ends with. No rule exists yet: this only reports.
SCRIPTS_ROOT=$(dirname "$SCRIPTS")
AUTOMATION_LINES=$(python3 "$SCRIPTS/release_git.py" automation 2>"$TMP/automation.err") \
  || die "$(sed 's/^release_git.py: //' "$TMP/automation.err"); nothing was read or pushed. Set the variable to off, shadow or a rule list, or delete it"
# bot-finish (ENG-2235, 2c), an action word of the same variable, not a rule: off,
# shadow or act, and the block's last line. The value parsed above, so neither fails.
BOT_FINISH_MODE=$(python3 "$SCRIPTS/release_git.py" automation --action bot-finish)
BOT_FINISH_LINE=$(python3 "$SCRIPTS/release_git.py" automation --actions)
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
CONFIG_STRICT=$(python3 "$SCRIPTS/release_git.py" merge-up-config --key finish.strict 2>"$TMP/config.err") \
  || die "release/config.json of the scripts in use ($SCRIPTS_REF at $SCRIPTS_SHA): $(sed 's/^release_git.py: //' "$TMP/config.err" | tail -1); nothing was read or pushed"
# What finish_pr goes by: the config's value, and always strict for an
# auto-finish (ENG-2235), whatever the config says. CONFIG_STRICT stays what
# the config line printed.
FINISH_STRICT="$CONFIG_STRICT"
[ "$AUTO_FINISH" != true ] || FINISH_STRICT=true
# Where every finish instruction this run prints says to dispatch: the default
# branch, so the finish runs the current scripts and release/config.json and
# not those of the child line (ENG-2234).
ON_DEFAULT="on the default branch${DEFAULT_BRANCH:+ ($DEFAULT_BRANCH)}"
WHY_DEFAULT="Dispatch it $ON_DEFAULT, not on \`$CHILD\`: a run uses the scripts and the release/config.json of the ref it is dispatched on, and the finish checks live on the default branch."
{
  echo "== merge-up automation"
  echo "$AUTOMATION_LINES"
  echo "scripts:            $SCRIPTS_REF at $SCRIPTS_SHA$SCRIPTS_NOTE"
  echo "config:             finish.strict=$CONFIG_STRICT (release/config.json at that ref)"
  [ "$AUTO_FINISH" != true ] || echo "auto-finish:        yes (PR #$PR, from release-auto-finish.yml): the strict finish checks apply whatever finish.strict says, and the outcome is left on the PR"
  echo "$BOT_FINISH_LINE"
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

# ---- auto-finish: a PR that is gone ends the run green (ENG-2235) ---------------

# The sweep dispatched this finish a moment ago; a person may have merged or
# closed the PR since (or a second dispatch came after the first merged it).
# Nothing is left to do, and the run says so before it reads anything else.
if [ "$AUTO_FINISH" = true ] && gh pr view "$PR" -R "$OWN_REPO" --json state,headRefName,baseRefName > "$TMP/auto-gone.json" 2>/dev/null; then
  read -r state head base < <(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("state",""), d.get("headRefName",""), d.get("baseRefName",""))' "$TMP/auto-gone.json")
  if [[ "$head" == merge-up/$CHILD/* ]] && { [ -z "$PARENT" ] || [ "$base" = "$PARENT" ]; } && [ "$state" != OPEN ]; then
    echo "auto-finish:        PR $OWN_REPO#$PR is $state now; nothing to finish"
    [ -z "${MERGE_UP_SUMMARY:-}" ] || echo "- auto-finish: PR $OWN_REPO#$PR is $state now; nothing to finish" >> "$MERGE_UP_SUMMARY"
    exit 0
  fi
fi

# ---- the two lines -----------------------------------------------------------

git fetch --quiet origin
KIND=$(python3 "$SCRIPTS/lines.py" --root "$REPO" parse "$CHILD" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["kind"])')
case "$KIND" in
  minor|patch) ;;
  main) die "$CHILD is the main line: it has no parent line to merge into" ;;
  *) die "$CHILD is not a release line of a configured product" ;;
esac
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

# The merge keeps the parent's versions/, so the parent must have one (every
# line opened by create-branch does; restoring the target's versions files on a
# parent without it would fail with a bare pathspec error).
git cat-file -e "$PARENT_TIP:versions" 2>/dev/null || die "$PARENT has no versions/ directory at ${PARENT_TIP:0:7}; the merge keeps the target's versions/, so the line must have been opened by create-branch"

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
    if RUN=$(gh run list -R "$OWN_REPO" --workflow "$RC_WORKFLOW" --commit "$sha" --branch "$CHILD" \
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
# nor make an approved one stale (ENG-2233; 3.0.x hit it on 2026-11-04 with
# v3.0.1's bump on top of an approved PR).
CONFLICT_BRANCH="merge-up/$CHILD/${GREEN_SHA:0:7}"

# ---- helpers -------------------------------------------------------------------

# summary <line>: one more line for the run summary (the file the workflow names).
summary() {
  [ -z "${MERGE_UP_SUMMARY:-}" ] || printf '%s\n' "$*" >> "$MERGE_UP_SUMMARY"
}

# ---- the merge rules (ENG-2234) ------------------------------------------------

# off, shadow or act, from the variable parsed above.
SPLICE_MODE=$(python3 "$SCRIPTS/release_git.py" automation --rule splice)
RULES_BODY=""       # file: what the rules did, for the body of a PR this run opens
RULES_COMMENT=""    # file: the candidate patches of never_auto files, for a PR comment
RULES_TRAILERS=""   # the Merge-Up-Rule lines of the commit, one per line
RULES_ACTED=""      # set when a rule changed the merge that is open
RULES_ASKED=""      # set once branch_gate has looked for an open PR that holds the child
RULES_EXISTING=""   # what it found there ("<number> <head>" or nothing); rules_resolve and on_conflict do not ask twice

# rules_say <text>: one line of the rules' log, on stdout and in the run summary.
rules_say() {
  echo "merge rules:        $1"
  summary "- merge rules: $1"
}

# rules_resolve <conflict list file>: the merge rules on the merge that is open
# in $WT. Shadow: log lines only. Act: the rules' results are written into the
# merge and <conflict list file> is rewritten to the files still in conflict
# (in a dry run: the files that would be); the original list stays in
# <conflict list file>.all. Leaves everything as it was when the rules do not
# apply or could not be evaluated.
rules_resolve() {
  local files="$1" out="$TMP/rules" existing line
  local -a args=()
  RULES_BODY=""
  RULES_COMMENT=""
  RULES_TRAILERS=""
  RULES_ACTED=""
  cp "$files" "$files.all"
  [ "$SPLICE_MODE" != off ] || return 0
  if [ -n "$(nontext_conflicts "$WT" "$files")" ]; then
    rules_say "not applied: a conflict here leaves no text markers, so the PR goes on $CHILD itself and a rule's result would not be on it"
    return 0
  fi
  existing="$RULES_EXISTING"   # branch_gate asked a moment ago
  if [ -n "$existing" ]; then
    rules_say "not applied: open PR $OWN_REPO#${existing%% *} already contains $CHILD at ${GREEN_SHA:0:7} and is reused as it is"
    return 0
  fi
  [ "$DRY_RUN" = false ] || args+=(--check)
  rm -rf "$out"
  if ! python3 "$SCRIPTS/release_git.py" resolve-conflicts --git-dir "$WT" --out "$out" \
         ${args[@]+"${args[@]}"} > /dev/null 2>"$TMP/rules.err"; then
    rules_say "not evaluated ($(tail -1 "$TMP/rules.err" | sed 's/^release_git.py: //'))"
    return 0
  fi
  while IFS= read -r line; do
    rules_say "$line"
  done < "$out/log.txt"
  if [ "$SPLICE_MODE" != act ]; then
    [ "$BOT_FINISH_MODE" = off ] || rules_bot_would "$out/report.json" "$files.all"
    return 0
  fi
  [ ! -s "$out/body.md" ] || RULES_BODY="$out/body.md"
  [ ! -s "$out/comment.md" ] || RULES_COMMENT="$out/comment.md"
  RULES_TRAILERS=$(cat "$out/trailers.txt")
  [ -z "$RULES_TRAILERS" ] || RULES_ACTED=1
  cp "$out/remaining.txt" "$files"
}

# ---- bot-finish: the bot merges a rule-resolved PR (ENG-2235, 2c) ---------------

RULES_BOT=""        # what bot-finish does with this run's rule-resolved PR: "" (off), shadow or act
RULES_BOT_WHY=""    # why the bot would not merge it (empty: it would)
BOT_BRANCH=""       # its head branch, merge-up/<child>/<sha7> (rules_bot_plan sets it)
# The hidden line of a PR body or comment that says the bot merges the PR itself.
BOT_NOTE_MARK="<!-- merge-up-bot-finish -->"

# rules_bot_say <text>: one line of bot-finish's log, on stdout and in the run
# summary. Not a "merge rules:" line, those are the rules', and not the
# switches block's "bot finish:" line, which says what the variable means.
rules_bot_say() {
  echo "bot-finish:         $1"
  summary "- bot-finish: $1"
}

# bot_checks_md: the required checks, as `a` and `b`.
bot_checks_md() {
  cfg finish.required_checks | python3 -I -c 'import json,sys; print(" and ".join("`%s`" % n for n in json.load(sys.stdin)) or "(no check is configured)")'
}

# bot_local_why: why the bot would not merge a rule-resolved PR, from this
# run's own facts, or nothing. The required list must name a check, or nothing
# would vouch for the commit.
bot_local_why() {
  [ -n "$(cfg_names finish.required_checks)" ] || echo "merge_up.finish.required_checks is empty, so no check would vouch for the commit"
}

# bot_siblings_no <branch>: did a person say no to a PR this one supersedes?
# Those are the other merge-up/<child>/<sha7> PRs against the parent (the PR
# that speaks for each head, release_git.py siblings) whose child commit the
# parent does not hold yet: merged up since, a commit is no longer superseded.
# A no is a close by a person (person-no CLOSED: not the bot's own
# supersession) or a person's latest review asking for changes, open or
# closed: the bot's supersession closes the PR, the review stays. Without this
# a no lasted until the next child commit, whose PR the bot merges with the
# same rule result (2c review, trust 4). Prints why, or nothing. What cannot be
# read counts as a no.
bot_siblings_no() {
  local branch="$1" n state head url sha why
  if ! gh pr list -R "$OWN_REPO" --base "$PARENT" --state all --limit 1000 --json number,state,headRefName,url \
         --jq '.[] | "\(.number) \(.state) \(.headRefName) \(.url)"' > "$TMP/siblings.txt" 2>"$TMP/siblings.err" \
     || ! python3 "$SCRIPTS/release_git.py" siblings "$TMP/siblings.txt" --child "$CHILD" --skip "$branch" > "$TMP/siblings.latest" 2>>"$TMP/siblings.err"; then
    echo "the merge-up PRs of $CHILD in $OWN_REPO could not be read ($(tail -1 "$TMP/siblings.err")), so a person's no to a PR this one supersedes cannot be ruled out"
    return 0
  fi
  while read -r n state head url; do
    [ -n "$n" ] && [ "$state" != MERGED ] || continue
    if sha=$(git rev-parse --verify --quiet "${head##*/}^{commit}") \
       && git merge-base --is-ancestor "$sha" "$PARENT_TIP" 2>/dev/null; then
      continue
    fi
    why=""
    if [ "$state" = CLOSED ]; then
      if gh api "repos/$OWN_REPO/issues/$n" > "$TMP/sibling-issue.json" 2>"$TMP/sibling.err" \
         && gh api --paginate "repos/$OWN_REPO/issues/$n/comments" > "$TMP/sibling-comments.json" 2>>"$TMP/sibling.err" \
         && why=$(python3 "$SCRIPTS/release_git.py" person-no --state CLOSED --issue "$TMP/sibling-issue.json" --comments "$TMP/sibling-comments.json" --release-app "$GIT_BOT_NAME" 2>>"$TMP/sibling.err"); then
        :
      else
        why="who closed it could not be read ($(tail -1 "$TMP/sibling.err"))"
      fi
    fi
    if [ -z "$why" ]; then
      if gh api --paginate "repos/$OWN_REPO/pulls/$n/reviews" > "$TMP/sibling-reviews.json" 2>"$TMP/sibling.err" \
         && why=$(python3 "$SCRIPTS/release_git.py" person-no --state OPEN --reviews "$TMP/sibling-reviews.json" 2>>"$TMP/sibling.err"); then
        :
      else
        why="its reviews could not be read ($(tail -1 "$TMP/sibling.err"))"
      fi
    fi
    if [ -n "$why" ]; then
      echo "a person said no to $url, which this PR supersedes ($why)"
      return 0
    fi
  done < "$TMP/siblings.latest"
}

# rules_bot_plan <branch>: what bot-finish does with the rule-resolved PR this
# run is about to open or reuse on <branch>. Sets RULES_BOT (empty when the
# word is off: nothing is read or said), BOT_BRANCH and RULES_BOT_WHY. Read
# before close_superseded, which closes the open PRs this one supersedes.
rules_bot_plan() {
  local branch="$1"
  RULES_BOT=""
  RULES_BOT_WHY=""
  [ "$BOT_FINISH_MODE" != off ] || return 0
  RULES_BOT="$BOT_FINISH_MODE"
  BOT_BRANCH="$branch"
  RULES_BOT_WHY=$(bot_local_why)
  [ -n "$RULES_BOT_WHY" ] || RULES_BOT_WHY=$(bot_siblings_no "$branch")
}

# rules_bot_acts: the bot merges this run's rule-resolved PR.
rules_bot_acts() {
  [ "$RULES_BOT" = act ] && [ -z "$RULES_BOT_WHY" ]
}

# rules_bot_log <url> <head>: what bot-finish says of a rule-resolved PR it
# does not merge: in shadow what it would do, acting why it does not. Off: nothing.
rules_bot_log() {
  local url="$1" head="$2"
  case "$RULES_BOT" in
    shadow)
      if [ -z "$RULES_BOT_WHY" ]; then
        rules_bot_say "shadow, nothing changes: would merge $url with no approval once $(cfg_names finish.required_checks | sed 's/,/, /g') succeed on ${head:0:7} (a wait of at most $(cfg finish.wait_minutes) minutes); it waits for one approval"
      else
        rules_bot_say "shadow, nothing changes: would not merge $url: $RULES_BOT_WHY; it waits for one approval"
      fi ;;
    act) rules_bot_say "not applied to $url: $RULES_BOT_WHY; it waits for one approval" ;;
  esac
}

# rules_bot_dry: the same, for a dry run, which opens no PR.
rules_bot_dry() {
  local shadow=""
  [ -n "$RULES_BOT" ] || return 0
  [ "$RULES_BOT" = act ] || shadow="shadow, nothing changes: "
  if [ -z "$RULES_BOT_WHY" ]; then
    rules_bot_say "${shadow}dry run: would then merge that PR with no approval once $(cfg_names finish.required_checks | sed 's/,/, /g') succeed on its commit (a wait of at most $(cfg finish.wait_minutes) minutes)"
  else
    rules_bot_say "${shadow}dry run: would not merge that PR: $RULES_BOT_WHY; it would wait for one approval"
  fi
}

# rules_bot_body <branch> <head>: the paragraphs of a rule-resolved PR's body
# that the bot merges itself (in place of the one-approval instructions).
rules_bot_body() {
  local branch="$1" head="$2"
  echo "Read the diff of the files above. If a result is wrong, request changes on this PR or close it: either stops the bot. A commit pushed to \`$branch\` stops it too, and makes this an ordinary conflict PR that needs a resolver and an approver other than the last pusher. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one."
  echo
  echo "The bot merges this PR itself, with no approval (\`bot-finish\` is in DS_MERGE_UP_AUTOMATION), once $(bot_checks_md) succeed on \`${head:0:7}\`. It waits up to $(cfg finish.wait_minutes) minutes, and merges only while that commit is still the head and \`$PARENT\` has not moved. If a check fails or the wait runs out, the PR stays open: approve it and dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false, or leave it to the next merge-up run of \`$CHILD\`, which finishes it once the checks are green and the head is untouched. $WHY_DEFAULT"
}

# rules_bot_notice <url> <head>: a reused rule-resolved PR that the bot now
# merges itself (the word was added since it was opened) gets one comment that
# says so. Its body, which asks for one approval, is not edited (an edit starts
# the PR's edited workflows again). Once per PR: a body or an earlier comment
# that carries BOT_NOTE_MARK says it already. A failure is a warning.
rules_bot_notice() {
  local url="$1" head="$2" number="${1##*/}" body
  if body=$(gh pr view "$number" -R "$OWN_REPO" --json body --jq .body 2>/dev/null) && [[ "$body" == *"$BOT_NOTE_MARK"* ]]; then
    return 0
  fi
  if gh api --paginate "repos/$OWN_REPO/issues/$number/comments" > "$TMP/notice-comments.json" 2>/dev/null \
     && grep -qF "$BOT_NOTE_MARK" "$TMP/notice-comments.json"; then
    return 0
  fi
  body="\`bot-finish\` is in DS_MERGE_UP_AUTOMATION now, so release-merge-up merges this PR itself, with no approval, once $(bot_checks_md) succeed on \`${head:0:7}\`, its head, which is still the bot's own commit. It waits up to $(cfg finish.wait_minutes) minutes per run, and merges only while that commit is the head and \`$PARENT\` has not moved. The description above says this PR needs one approval: that no longer holds. To stop the bot, request changes on this PR or close it. A commit pushed to \`$BOT_BRANCH\` stops it too, and makes this an ordinary conflict PR. Run: $RUN_URL"
  if gh pr comment "$number" -R "$OWN_REPO" --body "$body"$'\n\n'"$BOT_NOTE_MARK" >/dev/null 2>"$TMP/notice.err"; then
    echo "commented on $url: the bot now merges it itself"
  else
    echo "::warning::merge-up: could not comment on $url that the bot now merges it ($(head -1 "$TMP/notice.err"))"
  fi
}

# rules_bot_would <report> <listed file>: bot-finish's line when splice does
# not act (the value is shadow, or names the word without splice): the rules
# would resolve every conflict of this merge, so with splice acting the run
# would make a rule-resolved PR. Local facts only.
rules_bot_would() {
  local report="$1" listed="$2" why
  python3 "$SCRIPTS/release_git.py" rules-would-resolve "$report" --listed "$listed" 2>/dev/null || return 0
  if [ "$BOT_FINISH_MODE" = act ]; then
    rules_bot_say "not applied: the rules would resolve every conflict here, but splice does not act, so there is no rule-resolved PR to finish"
    return 0
  fi
  why=$(bot_local_why)
  if [ -z "$why" ]; then
    rules_bot_say "shadow, nothing changes: the rules would resolve every conflict here, and the bot would merge the resulting PR with no approval once $(cfg_names finish.required_checks | sed 's/,/, /g') succeed (unless a person said no to it or to a PR it supersedes)"
  else
    rules_bot_say "shadow, nothing changes: the rules would resolve every conflict here; the bot would not merge the resulting PR: $why"
  fi
}

# bot_stop <url> <why>: a bot-finished PR the bot does not merge. It stays
# open, and the run ends red with the link. (A PR that is no longer open:
# bot_closed.)
bot_stop() {
  summary "- bot-finished PR $1 was not merged: $2"
  echo "::error::merge-up: bot-finished PR $1 was not merged: $2. The PR stays open. A person approves it and dispatches release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${1##*/}, or the next merge-up run of $CHILD finishes it once its required checks are green and its head is untouched"
  exit 1
}

# bot_closed <url> <why>: the bot-finished PR stopped being open while the bot
# waited for its checks (checks-state "closed"; <why> is "the PR is CLOSED" or
# "the PR is MERGED"). The run ends red, and the message is not bot_stop's:
# the PR does not stay open. A close is read again with branch_dead (the PR
# was open when this run first read it), so a person's close gets the
# dead-branch rule's finding and ways forward.
bot_closed() {
  local url="$1" why="$2" finding
  if [ "$why" = "the PR is MERGED" ]; then
    summary "- bot-finished PR $url was not merged by the bot: someone merged it during the wait"
    echo "::error::merge-up: bot-finished PR $url was not merged by the bot: $why, so someone merged it while the bot waited for its checks. The bot did not check that merge (no post-check, no comment); the next merge-up run of $CHILD starts from $PARENT as it is then"
    exit 1
  fi
  if branch_dead "$BOT_BRANCH"; then
    finding=$(dead_finding "$BOT_BRANCH")
    summary "- bot-finished PR $url was not merged: $finding"
    echo "::error::merge-up: bot-finished PR $url was not merged: $finding. No run opens a pull request on $BOT_BRANCH, pushes to it or merges from it while it is closed. $(dead_ways "$BOT_BRANCH")"
    exit 1
  fi
  summary "- bot-finished PR $url was not merged: $why, not by a person"
  echo "::error::merge-up: bot-finished PR $url was not merged: $why, and not by a person (the bot superseded it, or another PR on $BOT_BRANCH is open), so $BOT_BRANCH is not treated as closed. The next merge-up run of $CHILD goes on as normal"
  exit 1
}

# bot_changes_requested <url>: before the bot merges a bot-finished PR that is
# open: did a person request changes on it? The latest review of each person
# counts (a later approval or a dismissal by the same person takes it back),
# bots never do, and reviews that cannot be read count as a no. The bot then
# does not merge: the PR stays open for people and the run ends red with the
# link, however the run was started. (A PR a person closed is a dead branch:
# branch_dead.)
bot_changes_requested() {
  local url="$1" number="${1##*/}" why=""
  if gh api --paginate "repos/$OWN_REPO/pulls/$number/reviews" > "$TMP/head-reviews.json" 2>"$TMP/person-no.err" \
     && why=$(python3 "$SCRIPTS/release_git.py" person-no --state OPEN --reviews "$TMP/head-reviews.json" 2>>"$TMP/person-no.err"); then
    :
  else
    why="its reviews could not be read ($(tail -1 "$TMP/person-no.err"))"
  fi
  [ -n "$why" ] || return 0
  rules_bot_say "not applied: a person said no to bot-finished PR $url ($why). The bot does not merge it, and the PR stays open for people"
  echo "::error::merge-up: a person said no to bot-finished PR $url ($why), so the bot does not merge $BOT_BRANCH. The PR stays open. Once the review is dismissed or its author approves, the next merge-up run of $CHILD finishes it. Or approve the PR and dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=$number. A new commit on $CHILD gets a branch of its own and runs as normal"
  exit 1
}

# bot_sources <bot commit> <PR number>: once the view shows every required check green on
# the bot's commit, where did each come from? A check run of a required name
# can be posted by any workflow on any branch, or by any token through the
# checks API, so the name alone vouches for nothing (2a review, trust 2). The
# check runs and the Actions runs of the commit, and the jobs of the runs that
# claim to post one, go to release_git.py check-sources, which asks
# auto_finish.check_sources against merge_up.finish.check_sources. Sets state
# (green, pending or red) and reasons, as checks-state does. A read that fails
# is pending: the wait goes on, and its timeout stops the bot.
bot_sources() {
  local sha="$1" rid
  local -a reads=()
  # A workflow source counts only from this PR's own runs: its number, the base $PARENT, OWN_REPO.
  local -a this=(--pr "${2:?bot_sources needs the PR number}" --base "$PARENT" --repo "$OWN_REPO")
  rm -rf "$TMP/bot-jobs"
  mkdir -p "$TMP/bot-jobs"
  if ! cfg finish.check_sources > "$TMP/bot-sources.json" 2>"$TMP/bot-sources.err"; then
    state=pending
    reasons="merge_up.finish.check_sources could not be read ($(tail -1 "$TMP/bot-sources.err" | sed 's/^release_git.py: //'))"
    return 0
  fi
  if gh api --paginate "repos/$OWN_REPO/commits/$sha/check-runs?per_page=100" > "$TMP/bot-check-runs.json" 2>/dev/null \
     && gh api --paginate "repos/$OWN_REPO/actions/runs?head_sha=$sha&per_page=100" > "$TMP/bot-head-runs.json" 2>/dev/null; then
    reads=(--check-runs "$TMP/bot-check-runs.json" --actions-runs "$TMP/bot-head-runs.json")
    for rid in $(python3 "$SCRIPTS/auto_finish.py" source-runs --required "$required" --sources "$TMP/bot-sources.json" \
                   --head-ref "$BOT_BRANCH" --head "$sha" "${this[@]}" "${reads[@]}" 2>/dev/null || true); do
      [[ "$rid" =~ ^[0-9]+$ ]] || continue
      gh api --paginate "repos/$OWN_REPO/actions/runs/$rid/jobs?filter=all&per_page=100" > "$TMP/bot-jobs/$rid.json" 2>/dev/null \
        || rm -f "$TMP/bot-jobs/$rid.json"
    done
  fi
  # GitHub lists every open PR of the branch in each run of it, whichever PR's event started it:
  # another PR of the branch into another base (or into $PARENT, unless the release App opened it
  # and it was never retargeted), or a base that was ever changed, leaves no run this PR's alone
  # (auto_finish.head_branch_prs), a stop like a red check. Every PR of the branch
  # and this PR's events, read on every poll; a read that fails leaves them out, which is pending.
  # Another PR of the branch into $PARENT counts only when the release App opened it and its own
  # events hold no base change: those events are read too (auto_finish.py same-base-prs, usually
  # none or one PR); a read that fails leaves its file out, which is pending.
  rm -rf "$TMP/bot-other-events"
  mkdir -p "$TMP/bot-other-events"
  if gh api --paginate "repos/$OWN_REPO/pulls?state=all&head=${OWN_REPO%%/*}:$BOT_BRANCH&per_page=100" > "$TMP/bot-head-prs.json" 2>/dev/null; then
    reads+=(--head-prs "$TMP/bot-head-prs.json")
    for rid in $(python3 "$SCRIPTS/auto_finish.py" same-base-prs "$TMP/bot-head-prs.json" --pr "$2" --base "$PARENT" \
                   --head-ref "$BOT_BRANCH" --release-app "$GIT_BOT_NAME" 2>/dev/null || true); do
      [[ "$rid" =~ ^[0-9]+$ ]] || continue
      gh api --paginate "repos/$OWN_REPO/issues/$rid/events?per_page=100" > "$TMP/bot-other-events/$rid.json" 2>/dev/null \
        || rm -f "$TMP/bot-other-events/$rid.json"
    done
  fi
  reads+=(--release-app "$GIT_BOT_NAME" --other-events "$TMP/bot-other-events")
  if gh api --paginate "repos/$OWN_REPO/issues/$2/events?per_page=100" > "$TMP/bot-events.json" 2>/dev/null; then
    reads+=(--events "$TMP/bot-events.json")
  fi
  if python3 "$SCRIPTS/release_git.py" check-sources --required "$required" --sources "$TMP/bot-sources.json" \
       --head-ref "$BOT_BRANCH" --head "$sha" "${this[@]}" ${reads[@]+"${reads[@]}"} --jobs-dir "$TMP/bot-jobs" > "$TMP/bot-sources.state" 2>"$TMP/bot-sources.err"; then
    state=$(head -1 "$TMP/bot-sources.state")
    reasons=$(tail -n +2 "$TMP/bot-sources.state" | paste -sd ';' | sed 's/;/; /g')
  else
    state=pending
    reasons="where the required checks came from could not be read ($(tail -1 "$TMP/bot-sources.err" | sed 's/^release_git.py: //'))"
  fi
}

# bot_merged_onto: after the REST merge in bot_finish (its locals url and number),
# the merge commit's first parent is the parent tip GitHub merged onto. The ls-remote
# before the merge and the merge are not atomic, and sha= pins only the head: a push to
# $PARENT in between merges the checked head onto a parent no check ran on (2c review,
# nit 3). That cannot be undone here, only said: a warning that names both commits and
# a summary line. The run does not end red: the merge happened.
bot_merged_onto() {
  local merged first
  merged=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha") or "")' "$TMP/bot-merge.json" 2>/dev/null || true)
  first=$(git rev-parse --verify --quiet "${merged:-$NEW_TIP}^1" 2>/dev/null || true)
  [ "$first" != "$PARENT_TIP" ] || return 0
  echo "::warning::merge-up: PR $OWN_REPO#$number was merged onto ${first:-a commit that could not be read} of $PARENT, not onto $PARENT_TIP, the tip its required checks vouched for: $PARENT moved between the last check and the merge. The merge stands; check $PARENT"
  summary "- PR $url was merged onto ${first:0:7} of $PARENT, not onto ${PARENT_TIP:0:7}, the tip its required checks vouched for: $PARENT moved between the last check and the merge"
}

# bot_finish <url> <bot commit>: wait for the required checks on the bot's
# commit, each from its source, then merge the PR pinned to that commit. The
# App bypasses the approval ruleset, so what is read here is the only gate on
# this merge. Every stop ends the run (bot_stop, bot_closed,
# bot_changes_requested). After the merge it returns, with NEW_TIP the
# parent's new tip: the caller closes what the PR superseded and ends the run.
# The wait is merge_up.finish.wait_minutes (MERGE_UP_FINISH_WAIT_SECONDS and
# MERGE_UP_FINISH_POLL_SECONDS override it and the 30-second poll, for the tests).
bot_finish() {
  local url="$1" sha="$2" number="${1##*/}" required wait poll deadline state reasons now sourced="" rc=0 why
  required=$(cfg_names finish.required_checks)
  wait="${MERGE_UP_FINISH_WAIT_SECONDS:-$(( $(cfg finish.wait_minutes) * 60 ))}"
  poll="${MERGE_UP_FINISH_POLL_SECONDS:-30}"
  deadline=$(( SECONDS + wait ))
  echo "waiting for ${required:-no check} on ${sha:0:7} (at most ${wait}s)"
  while :; do
    state=unread
    reasons="the PR could not be read"
    if gh pr view "$number" -R "$OWN_REPO" --json state,headRefOid,statusCheckRollup > "$TMP/bot-pr.json" 2>/dev/null \
       && python3 "$SCRIPTS/release_git.py" checks-state "$TMP/bot-pr.json" --head "$sha" --required "$required" > "$TMP/bot-pr.state" 2>/dev/null; then
      state=$(head -1 "$TMP/bot-pr.state")
      reasons=$(tail -n +2 "$TMP/bot-pr.state" | paste -sd ';' | sed 's/;/; /g')
    fi
    # Green by name: green only when each came from its source too (merge_up.finish.check_sources).
    if [ "$state" = green ]; then
      bot_sources "$sha" "$number"
      case "$state" in
        green) sourced="$reasons"
               reasons=$(tail -n +2 "$TMP/bot-pr.state" | paste -sd ';' | sed 's/;/; /g') ;;
        red) bot_stop "$url" "a required check on ${sha:0:7} did not come from its source in merge_up.finish.check_sources ($reasons)" ;;
      esac
    fi
    case "$state" in
      green) break ;;
      red) bot_stop "$url" "a required check failed on ${sha:0:7} ($reasons)" ;;
      moved) bot_stop "$url" "$reasons" ;;
      closed) bot_closed "$url" "$reasons" ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || bot_stop "$url" "the required checks were not green on ${sha:0:7} after ${wait}s ($reasons)"
    sleep "$poll"
  done
  echo "required checks green on ${sha:0:7}: $reasons"
  echo "required checks from their sources on ${sha:0:7}: $sourced"
  # A review that asks for changes may have come in during the wait.
  bot_changes_requested "$url"
  # So may a person's no to a PR this one supersedes (reopened and refused, or closed by a
  # person): read again as rules_bot_plan read it before the wait (2c review, nit 2).
  why=$(bot_siblings_no "$BOT_BRANCH")
  [ -z "$why" ] || bot_stop "$url" "$why, read again after the wait"
  # The checks vouch for this commit on top of the parent it was made from.
  now=$(git ls-remote origin "refs/heads/$PARENT" | cut -f1)
  [ "$now" = "$PARENT_TIP" ] || bot_stop "$url" "$PARENT moved from ${PARENT_TIP:0:7} to ${now:0:7} while the checks ran, so the checked commit is no longer up to date with it"
  # The REST merge, pinned to the bot's commit by sha (a moved head is a 409). Not
  # `gh pr merge`: the PR has no approval, so GitHub reads mergeStateStatus BLOCKED
  # even for the ruleset's bypass actor, and gh then refuses on its own side ("the
  # base branch policy prohibits the merge") before any API call, so the bypass is
  # never tried (measured 2026-11-18, dexterds14/ds-release-scratch#23). The REST
  # call does use the bypass (#20). Never --admin.
  gh api -X PUT "repos/$OWN_REPO/pulls/$number/merge" -f merge_method=merge -f sha="$sha" \
    > "$TMP/bot-merge.json" 2> "$TMP/bot-merge.err" || rc=$?
  why=$(python3 "$SCRIPTS/release_git.py" merge-answer "$TMP/bot-merge.json" --stderr "$TMP/bot-merge.err" --exit "$rc") \
    || bot_stop "$url" "$why"
  echo "merged PR $OWN_REPO#$number (head $sha, no approval: the merge rules and the required checks)"
  git fetch --quiet origin
  NEW_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  bot_merged_onto
  post_check "$REPO" "$NEW_TIP" "$sha" "$CHILD_TIP"
  close_if_still_open "$OWN_REPO" "$number" "$NEW_TIP" "$BOT_BRANCH"
  gh pr comment "$number" -R "$OWN_REPO" --body "Merged by release-merge-up at $NEW_TIP with no approving review: the merge-up rules resolved every conflict of this merge in this commit (\`${sha:0:7}\`), and ${required//,/, } succeeded on it. Run: $RUN_URL" >/dev/null 2>"$TMP/bot-comment.err" \
    || echo "::warning::merge-up: could not comment on $url ($(head -1 "$TMP/bot-comment.err"))"
}

# rules_pr <conflict list file>: the rules resolved every conflict of the merge
# that is open in $WT. Never a direct push: the merge is committed without
# markers, pushed as the conflict branch and opened as a PR. That PR needs one
# approval, unless bot-finish acts and the PR is eligible (rules_bot_plan):
# then the bot waits for its required checks and merges it (bot_finish).
# Always ends the run.
rules_pr() {
  local files="$1" branch="$CONFLICT_BRANCH" sha url count remote head
  count=$(grep -c . "$files.all" || true)
  rules_bot_plan "$branch"
  if [ "$DRY_RUN" = true ]; then
    git -C "$WT" merge --abort
    echo "dry run: would push $branch ($PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers) and open the PR \"Merge-up $CHILD into $PARENT (resolved by rule)\" against $PARENT for one approval; nothing pushed"
    rules_bot_dry
    close_superseded "$OWN_REPO" "$PARENT" "$branch" "the PR on $branch"
    exit 0
  fi
  git -C "$WT" commit --quiet -m "$MERGE_SUBJECT" -m "$RULES_TRAILERS"
  sha=$(git -C "$WT" rev-parse HEAD)
  if git -C "$WT" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    branch_provenance "$WT" "$branch" "$sha"
    # The head itself must be the commit this run computes. A branch that only
    # starts with it holds commits a person pushed on top: that is a stop
    # signal, and nothing on such a branch is called resolved by rule.
    [ -n "$BRANCH_NOTE" ] || branch_head_check "$WT" "$branch" "$sha"
    if [ -n "$BRANCH_NOTE" ]; then
      # Someone else's branch, an earlier run's on an older parent, or this run's commit
      # with other commits on top: it is not the rules' result and nothing more, so it is
      # the conflict PR of the phase-0 path, with the label.
      url=$(conflict_pr "$OWN_REPO" "$PARENT" "$branch" "$files.all") || die "could not open the conflict PR in $OWN_REPO"
      rules_unclaim "$OWN_REPO" "$url"
      close_superseded "$OWN_REPO" "$PARENT" "$branch" "$url"
      echo "::error::merge-up: $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files.all")); the rules resolve it, but $branch already exists on origin with other content and is left as it is. Resolve $url, then dispatch release-merge-up.yml with child=$CHILD pr=${url##*/} $ON_DEFAULT"
      exit 1
    fi
    # The commit on origin, which branch_provenance fetched: $sha is this run's own
    # commit, the same tree, parents and message but a new date, so another sha.
    remote=$(git -C "$WT" rev-parse --verify --quiet FETCH_HEAD) || remote="$sha"
    echo "$branch is already on origin at ${remote:0:7}: the commit this run computes, untouched; left as it is, reusing its PR"
    # A bot finish merges the commit on origin, pinned by sha.
    head="$remote"
  else
    git -C "$WT" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: ${sha:0:7}, $PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers"
    head="$sha"
  fi
  url=$(gh pr list -R "$OWN_REPO" --base "$PARENT" --head "$branch" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -z "$url" ]; then
    {
      echo "release-merge-up merged \`$CHILD\` into \`$PARENT\`. Git reported conflicts in $count file(s), and the merge-up rules resolved every one, so this branch has no conflict markers. The rules are mechanical: this PR is the review of what they did."
      echo
      [ -z "$RULES_BODY" ] || cat "$RULES_BODY"
      echo
      if rules_bot_acts; then
        rules_bot_body "$branch" "$head"
      else
        echo "Read the diff of the files above. If a result is wrong, fix the file on \`$branch\`, push, and get a review from someone other than the last pusher. If \`$PARENT\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one."
        echo
        finish_choices "This PR needs one approval. Get it from someone other than the last pusher, then finish it one of two ways:"
      fi
      echo
      echo "Run: $RUN_URL"
      if rules_bot_acts; then
        echo
        echo "$BOT_NOTE_MARK"
      fi
    } > "$TMP/rules-pr-body.md"
    url=$(gh pr create -R "$OWN_REPO" --base "$PARENT" --head "$branch" \
            --title "Merge-up $CHILD into $PARENT (resolved by rule)" --body-file "$TMP/rules-pr-body.md") \
      || die "could not open the PR for $branch in $OWN_REPO"
  elif rules_bot_acts; then
    # A reused PR: did a person say no to it? (bot_finish asks again after the wait.)
    bot_changes_requested "$url"
    rules_bot_notice "$url" "$head"
  fi
  if rules_bot_acts; then
    rules_say "every conflict resolved by rule in $url ($count file(s)); the PR has no markers and the bot merges it once $(cfg_names finish.required_checks | sed 's/,/, /g') succeed"
  else
    rules_say "every conflict resolved by rule in $url ($count file(s)); the PR has no markers and waits for one approval"
  fi
  close_superseded "$OWN_REPO" "$PARENT" "$branch" "$url"
  if rules_bot_acts; then
    # Every stop ends the run red with the PR open. Never --admin, never gh pr merge.
    bot_finish "$url" "$head"
    close_superseded "$OWN_REPO" "$PARENT" "" "PR #${url##*/}, merged at $NEW_TIP"
    summary "- bot-finished PR $url merged at $NEW_TIP"
    echo "done: $CHILD merged into $PARENT at $NEW_TIP (bot-finished PR ${url##*/})"
    exit 0
  fi
  rules_bot_log "$url" "$head"
  echo "::error::merge-up: $CHILD -> $PARENT: every conflict was resolved by rule in $url, which has no markers and waits for one approval. Then dispatch release-merge-up.yml with child=$CHILD pr=${url##*/} $ON_DEFAULT"
  exit 1
}

# rules_comment <url>: on a conflict PR this run opened, what the rules would
# have written to the never_auto files, as a comment with a patch. A failure
# is a warning: the PR is open either way.
rules_comment() {
  local url="$1"
  [ -n "$RULES_COMMENT" ] && [ -f "$TMP/pr-created" ] || return 0
  if gh pr comment "${url##*/}" -R "$OWN_REPO" --body "$(cat "$RULES_COMMENT")" >/dev/null 2>"$TMP/rules-comment.err"; then
    echo "commented on $url with the rules' candidate for the never_auto file(s)"
  else
    echo "::warning::merge-up: could not comment on $url with the rules' candidate ($(head -1 "$TMP/rules-comment.err"))"
  fi
}

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
  # The merge rules: what they resolved is in this run's commit, not on that branch.
  RULES_BODY=""
  RULES_COMMENT=""
  BRANCH_NOTE="**This branch was not produced by this run.** \`$branch\` already existed on origin${remote:+ at \`${remote:0:7}\`}, and $why. The bot left it as it is, so the files listed above are what this run's merge conflicts in, not necessarily what the branch holds. No merge-up rule was applied to it, and the bot does not merge it on its own. Read the whole diff of this PR before you approve it."
}

# branch_head_check <git dir> <branch> <like>: after branch_provenance found
# that <branch> starts with the commit this run made (<like>): is its head
# that commit itself, with nothing on top? The same tree, the same parents, the
# bot's committer address and the exact message. If not, someone pushed to the
# branch after the bot: the PR body says so (BRANCH_NOTE), nothing on it is
# called resolved by rule, and it is an ordinary conflict PR that needs a
# resolver and a different approver.
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
  # The merge rules: what they staged is this run's commit, and the branch is no
  # longer that commit alone.
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

# finish_choices: the two ways to finish a resolved conflict PR, for a PR body.
# An optional argument replaces the lead-in sentence.
finish_choices() {
  echo "${1:-When it is resolved, get a review from someone other than the last pusher, then finish it one of two ways:}"
  echo
  echo "- Dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\`, pr=<this PR's number> and dry_run=false (recommended: it ignores bot approvals, refuses a stale PR and closes superseded ones)."
  echo "- Merge this PR from GitHub. The required checks still apply, but none of the checks above do."
}

# conflict_pr <repo> <base> <head> <conflict file list file> -> prints the PR url
# A fifth argument (a no-marker list file) means the branch is the child tip
# (see on_conflict), and the body says so.
conflict_pr() {
  local repo="$1" base="$2" head="$3" files="$4" nontext="${5:-}" title body url
  title="Merge-up ${CHILD} into ${PARENT} (conflict)"
  body="$TMP/pr-body.md"
  {
    echo "release-merge-up stopped: merging \`$CHILD\` into \`$PARENT\` conflicts in:"
    echo
    if [ -n "$nontext" ]; then
      awk 'NR == FNR { nt[$0] = 1; next } { print "- `" $0 "`" (($0 in nt) ? " (no text markers: a binary file, or a file one side deleted or renamed)" : "") }' "$nontext" "$files"
    else
      sed 's/^/- `/; s/$/`/' "$files"
    fi
    echo
    if [ -n "$nontext" ]; then
      echo "Git cannot write conflict markers for the files marked above, so the bot could not commit the merge for you. This PR is therefore on \`$CHILD\` itself, and GitHub shows it as conflicting with no hunks. Checks and reviews start once it is mergeable."
      echo
      # The procedure is the merge-up's own direction (the parent checked out,
      # merge-keep-target): merge-keep-target keeps HEAD's files, so run from
      # this branch it keeps the child's, and a plain merge of the parent into
      # it takes the child's copy of a file only the child changed (measured on
      # dexterds14/ds-release-scratch #8, 2026-11-18; .ai/release-lines.md
      # "Resolving a no-marker PR"). Wording approved by Dexter 2026-11-19.
      # Since 2026-11-20 finish_pr and pr-checks refuse such a head (ENG-2234).
      echo "To resolve it, merge this branch into \`$base\` from a checkout of this repo:"
      echo
      echo '```'
      echo "git fetch origin && git checkout -B resolve origin/$base && git merge --no-ff --no-commit origin/$head; python3 scripts-release/release_git.py merge-keep-target"
      echo '```'
      echo
      echo "The merge stops on the files listed above. That is expected. \`merge-keep-target\` keeps \`$base\`'s \`versions/<product>\` files and its bookkeeping files (\`bookkeeping_files\` in release/config.json). Decide each file listed above, commit with \`git commit -m '$MERGE_SUBJECT'\`, and push with \`git push origin HEAD:$head\`. The push is a fast-forward. It updates this PR: \`$base\` gets the merge when the PR is approved and finished as below."
      echo
      echo "Do not merge \`$base\` into this branch instead. That keeps \`$CHILD\`'s version and bookkeeping files, and the finish and \`pr-checks\` refuse a branch that carries them."
      echo
      finish_choices
    else
    echo "This branch is \`$base\` with \`$CHILD\` merged in. The conflict markers are committed in the files above, so the diff of this PR shows every hunk to decide. Everything else merged clean. The parent keeps its \`versions/\` files and its bookkeeping files."
    echo
    echo "To resolve it: check out \`$head\`, edit the files above until no \`<<<<<<<\`, \`|||||||\` or \`>>>>>>>\` line remains, commit and push. The bot refuses to finish while a marker remains. If \`$base\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one (the branch stays, so a resolution started here can be merged into the new branch)."
    echo
    finish_choices
    fi
    if [ -n "$BRANCH_NOTE" ]; then
      echo
      echo "$BRANCH_NOTE"
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

# close_superseded <repo> <base> <keep head> <replacement>: close every other
# open merge-up/<child>/* PR against <base> (ENG-2233). Each RC of the child
# that conflicts opens merge-up/<child>/<sha7> for its own tip, so without
# this the PRs pile up: on 2026-11-04 sensor-portal had four open for one
# sudoers conflict. A superseded PR can never be finished (finish_pr refuses
# a head without the current child tip), so it is closed with a comment
# naming <replacement> (a PR url or a commit). Branches are left alone: a
# resolver may have pushed work there. A dry run prints what it would close.
# A failed list or close is a warning, never fatal.
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

DEAD_URL=""         # the PR a person closed (empty when the PR list could not be read)
DEAD_WHY=""         # who closed it, or what could not be read

# branch_dead <branch>: is merge-up/<child>/<sha7> dead? It is when no PR with
# that head against the parent is open and the most recent one (highest
# number) was closed without a merge by a person: anyone but the bot's own
# supersession, which is recognised by three things together (closed by a Bot
# account that is the release App, GIT_BOT_NAME, and left close_superseded's
# comment; release_git.py person-no). No
# PR, an open one (a reopened one included, also an older one reopened after a
# newer one of the head was closed) or a merged one: not dead. What cannot be
# read (the PR list, who closed it) counts as dead, and DEAD_WHY says it could
# not be read.
# Returns 0 when dead. How the run was started does not matter.
branch_dead() {
  local branch="$1" latest="" number state url why=""
  DEAD_URL=""
  DEAD_WHY=""
  if ! gh pr list -R "$OWN_REPO" --base "$PARENT" --head "$branch" --state all --limit 100 --json number,state,url > "$TMP/head-prs.json" 2>"$TMP/dead.err" \
     || ! latest=$(python3 "$SCRIPTS/release_git.py" latest-pr "$TMP/head-prs.json" 2>>"$TMP/dead.err"); then
    DEAD_WHY="the pull requests of $branch in $OWN_REPO could not be read ($(tail -1 "$TMP/dead.err")), so nothing shows that no person closed one"
    return 0
  fi
  [ -n "$latest" ] || return 1
  read -r number state url <<<"$latest"
  [ "$state" = CLOSED ] || return 1
  if gh api "repos/$OWN_REPO/issues/$number" > "$TMP/head-issue.json" 2>"$TMP/dead.err" \
     && gh api --paginate "repos/$OWN_REPO/issues/$number/comments" > "$TMP/head-comments.json" 2>>"$TMP/dead.err" \
     && why=$(python3 "$SCRIPTS/release_git.py" person-no --state CLOSED --issue "$TMP/head-issue.json" --comments "$TMP/head-comments.json" --release-app "$GIT_BOT_NAME" 2>>"$TMP/dead.err"); then
    [ -n "$why" ] || return 1   # the bot's own supersession
  else
    why="who closed it could not be read ($(tail -1 "$TMP/dead.err"))"
  fi
  DEAD_URL="$url"
  DEAD_WHY="$why"
  return 0
}

# dead_finding <branch>, dead_ways <branch>: what branch_dead found, and the two
# ways forward, as dead_stop and a bot finish's stop (bot_closed) say them.
dead_finding() {
  if [ -n "$DEAD_URL" ]; then
    echo "a person closed $DEAD_URL without merging ($DEAD_WHY), so $1 is treated as closed"
  else
    echo "$DEAD_WHY, so $1 is treated as closed"
  fi
}
dead_ways() {
  if [ -n "$DEAD_URL" ]; then
    echo "Two ways forward: the next green commit on $CHILD gets a new branch and a new PR by itself; or a person reopens $DEAD_URL (the branch then counts as open again) and resolves or finishes it as usual, with a dispatch of release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${DEAD_URL##*/}"
  else
    echo "Two ways forward: the next green commit on $CHILD gets a new branch and a new PR by itself; or, if a person closed the last PR of $1, a person reopens it and resolves or finishes it as usual (dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=<its number>). If no person closed one, the next run that can read the pull requests goes on as normal"
  fi
}

# dead_stop <branch>: the end of a run on a dead branch. Nothing is opened,
# pushed, merged or resolved by rule there, and the closed PR is neither
# reopened nor commented on. Ends like a conflict left for people: ::error::,
# a summary line, exit 1. A dry run prints the same finding and what a real
# run would not do, and exits 0.
dead_stop() {
  local branch="$1" finding ways
  finding=$(dead_finding "$branch")
  ways=$(dead_ways "$branch")
  if [ "$DRY_RUN" = true ]; then
    echo "merge:              dry run: $finding. A real run would open no pull request on $branch, push nothing to it, merge nothing from it and apply no merge-up rule to it, and would end with an error. $ways"
    summary "- merge: dry run: $finding; a real run would do nothing on $branch"
    echo "dry run: nothing pushed"
    exit 0
  fi
  echo "merge:              $finding. No pull request is opened on $branch, nothing is pushed to it or merged from it, and no merge-up rule is applied"
  summary "- merge: $finding. Nothing was opened, pushed, merged or resolved by rule on $branch"
  echo "::error::merge-up: $CHILD -> $PARENT: $finding. This run opens no pull request on $branch, pushes nothing to it, merges nothing from it and applies no merge-up rule to it. $ways"
  exit 1
}

# branch_gate: a merge that conflicts needs merge-up/<child>/<sha7>, unless an
# open merge-up PR on another branch already contains the child (that PR is
# reused and the branch is not touched). Otherwise the run stops here when the
# branch is dead. Runs before any rule looks at the merge.
branch_gate() {
  RULES_EXISTING=$(containing_pr "$OWN_REPO" "$PARENT" "$GREEN_SHA" "$CONFLICT_BRANCH")
  RULES_ASKED=1
  [ -z "$RULES_EXISTING" ] || return 0
  ! branch_dead "$CONFLICT_BRANCH" || dead_stop "$CONFLICT_BRANCH"
}

# containing_pr <repo> <base> <child tip> <skip head>: "<number> <head>" of an
# open merge-up/<child>/* PR against <base> (other than <skip head>) whose head
# already contains <child tip>, or nothing. That is a resolver who followed the
# stale message and merged the newer child into an older conflict branch: that
# PR is current, so it is reused instead of being superseded (ENG-2233).
containing_pr() {
  local repo="$1" base="$2" tip="$3" skip="$4" n h oid
  gh pr list -R "$repo" --base "$base" --state open --limit 1000 --json number,headRefName,headRefOid \
    --jq '.[] | "\(.number) \(.headRefName) \(.headRefOid)"' > "$TMP/open-prs-oid" 2>/dev/null || return 0
  while read -r n h oid; do
    [ -n "$n" ] || continue
    [[ "$h" == merge-up/$CHILD/* ]] || continue
    [ "$h" != "$skip" ] || continue
    git cat-file -e "$oid^{commit}" 2>/dev/null || git fetch --quiet origin "refs/heads/$h" 2>/dev/null || continue
    git cat-file -e "$oid^{commit}" 2>/dev/null || continue
    if git merge-base --is-ancestor "$tip" "$oid"; then
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

# on_conflict <conflict list file>: the end of a merge that conflicted; the
# merge is still open in $WT. Reuses an open PR that already contains the
# child's green commit; else opens the conflict PR: the merge with its markers
# committed (ENG-2200), or, when a conflict leaves no text markers, the green
# commit itself (the pre-ENG-2200 shape, which GitHub shows as conflicting).
# Then supersession. Exits 0 on a dry run, else 1.
on_conflict() {
  local files="$1" existing nontext="" url
  if [ -n "$RULES_ASKED" ]; then
    existing="$RULES_EXISTING"   # branch_gate asked a moment ago
  else
    existing=$(containing_pr "$OWN_REPO" "$PARENT" "$GREEN_SHA" "$CONFLICT_BRANCH")
  fi
  if [ -n "$existing" ]; then
    git -C "$WT" merge --abort
    echo "merge:              open conflict PR $OWN_REPO#${existing%% *} (${existing#* }) already contains $CHILD at ${GREEN_SHA:0:7}; reusing it, no new branch"
    close_superseded "$OWN_REPO" "$PARENT" "${existing#* }" "PR #${existing%% *}"
    [ "$DRY_RUN" = false ] || exit 0
    echo "::error::merge-up: $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $OWN_REPO#${existing%% *}, then dispatch release-merge-up.yml with child=$CHILD pr=${existing%% *} $ON_DEFAULT"
    exit 1
  fi
  nontext=$(nontext_conflicts "$WT" "$files")
  if [ -n "$nontext" ]; then
    echo "$nontext" > "$TMP/nontext"
    git -C "$WT" merge --abort
    echo "merge:              no text markers in: $(paste -sd ' ' "$TMP/nontext") (binary, or deleted or renamed on one side); the conflict PR goes on $CHILD itself at ${GREEN_SHA:0:7}"
    if [ "$DRY_RUN" = true ]; then
      echo "dry run: would push $CONFLICT_BRANCH at ${GREEN_SHA:0:7} (the child, no markers committed) and open the conflict PR against $PARENT; nothing pushed"
      close_superseded "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "the new conflict PR on $CONFLICT_BRANCH"
      exit 0
    fi
    if git ls-remote --exit-code --heads origin "$CONFLICT_BRANCH" >/dev/null 2>&1; then
      echo "$CONFLICT_BRANCH already exists on origin; left as it is (resolution in progress?), reusing its PR"
      branch_provenance "$REPO" "$CONFLICT_BRANCH" "" "$GREEN_SHA"
    else
      git push --quiet origin "$GREEN_SHA:refs/heads/$CONFLICT_BRANCH"
      echo "pushed $CONFLICT_BRANCH at the child tip ${GREEN_SHA:0:7}"
    fi
  else
    if [ "$DRY_RUN" = true ]; then
      git -C "$WT" merge --abort
      if git ls-remote --exit-code --heads origin "$CONFLICT_BRANCH" >/dev/null 2>&1; then
        echo "dry run: $CONFLICT_BRANCH already exists on origin; a real run leaves it as it is and reuses its PR; nothing pushed"
      else
        echo "dry run: would push $CONFLICT_BRANCH ($PARENT plus the merge of $CHILD, markers in the files above) and open the conflict PR against $PARENT; nothing pushed"
      fi
      close_superseded "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "the new conflict PR on $CONFLICT_BRANCH"
      exit 0
    fi
    commit_conflicts "$WT" "$CONFLICT_BRANCH" "$files"
    # A branch this run did not produce holds none of the rules' results: every
    # file the merge conflicts in is still to decide there, not only the ones
    # the rules left (rules_resolve keeps the full list in <list>.all).
    if [ -n "$BRANCH_NOTE" ] && [ -f "$files.all" ]; then
      cp "$files.all" "$files"
    fi
  fi
  rm -f "$TMP/pr-created"
  url=$(conflict_pr "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "$files" "${nontext:+$TMP/nontext}") || die "could not open the conflict PR in $OWN_REPO"
  # A reused PR may be one an earlier run opened as "(resolved by rule)": on a
  # branch this run did not produce, or that holds commits it did not make,
  # that title goes, here as in rules_pr.
  if [ -n "$BRANCH_NOTE" ] && [ ! -e "$TMP/pr-created" ]; then
    rules_unclaim "$OWN_REPO" "$url"
  fi
  rules_comment "$url"
  close_superseded "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "$url"
  echo "::error::merge-up: $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $url, then dispatch release-merge-up.yml with child=$CHILD pr=${url##*/} $ON_DEFAULT"
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
  local -a msg=(-m "$MERGE_SUBJECT (conflicts to resolve)" -m "Conflict markers left in: $(paste -sd ' ' "$files")")
  git -C "$dir" add -A
  # the merge rules: one Merge-Up-Rule line per rule that wrote into this commit
  [ -z "$RULES_ACTED" ] || msg+=(-m "$RULES_TRAILERS")
  git -C "$dir" commit --quiet "${msg[@]}"
  if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    echo "$branch already exists on origin; left as it is (resolution in progress?), reusing its PR"
    branch_provenance "$dir" "$branch" "$(git -C "$dir" rev-parse HEAD)"
    # This run's commit carries a rule's result: a PR body may only say so of a
    # branch whose head is that commit itself (see branch_head_check).
    if [ -z "$BRANCH_NOTE" ] && [ -n "$RULES_ACTED" ]; then
      branch_head_check "$dir" "$branch" "$(git -C "$dir" rev-parse HEAD)"
    fi
  else
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: $(git -C "$dir" rev-parse --short HEAD), $PARENT plus the merge of $CHILD with markers in $(paste -sd ' ' "$files")"
  fi
}

# strict_commits <repo> <head ref> <head sha>: the fallback of finish.strict
# when the repository activity cannot be read. Prints the REST commit object
# of every commit on the PR branch that is neither on the parent nor on the
# child, the bot's own merge aside (the first commit above the parent, when
# the bot committed it): the commits people pushed. Returns 1 when the branch
# or a commit cannot be read.
strict_commits() {
  local repo="$1" head_ref="$2" head_sha="$3" origin first="" sha
  git fetch --quiet origin "refs/heads/$head_ref" 2>/dev/null || return 1
  git cat-file -e "$head_sha^{commit}" 2>/dev/null || return 1
  origin=$(python3 "$SCRIPTS/release_git.py" branch-origin "$head_sha" --git-dir "$REPO" \
             --parent "refs/remotes/origin/$PARENT" --bot-email "$GIT_COMMITTER_EMAIL" 2>/dev/null | head -1) || return 1
  [ "${origin%% *}" != ours ] || first="${origin#* }"
  for sha in $(git rev-list "$head_sha" "^refs/remotes/origin/$PARENT" "^refs/remotes/origin/$CHILD" "^$GREEN_SHA"); do
    [ "$sha" != "$first" ] || continue
    gh api "repos/$repo/commits/$sha" 2>/dev/null || return 1
    echo
  done
}

# finish_pr <repo> <number>: the bot self-check, the merge, and the post-check on <parent>.
finish_pr() {
  local repo="$1" number="$2" head_sha head_ref marked base_sha kept required fields
  local -a changed=() strict=()
  # One view: the approval, the checks and the head they belong to are read
  # together, and the merge below is pinned to that head (--match-head-commit),
  # so a push after this point makes GitHub refuse the merge instead of the
  # bot merging an unchecked commit with its ruleset bypass.
  fields=reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits,headRefOid,headRefName
  [ "$AUTO_FINISH" != true ] || fields="$fields,number,state,isDraft"
  gh pr view "$number" -R "$repo" --json "$fields" > "$TMP/pr.json"
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
  if [ "$FINISH_STRICT" = true ]; then
    required=$(cfg_names finish.required_checks)
    strict=(--strict --required "$required")
    if gh api --paginate -X GET "repos/$repo/activity" -f "ref=refs/heads/$head_ref" > "$TMP/pr-activity.json" 2>"$TMP/pr-activity.err"; then
      strict+=(--activity "$TMP/pr-activity.json")
    elif [ "$AUTO_FINISH" = true ]; then
      # A commit names its author and committer at will: an auto-finish never
      # takes them for who pushed.
      echo "auto-finish:        the repository activity of $head_ref could not be read ($(head -1 "$TMP/pr-activity.err")); an auto-finish never falls back to the commits"
    else
      echo "strict finish:      the repository activity of $head_ref could not be read ($(head -1 "$TMP/pr-activity.err")); falling back to the authors and committers of the commits above the bot's commit"
      if strict_commits "$repo" "$head_ref" "$head_sha" > "$TMP/pr-above.json"; then
        strict+=(--commits "$TMP/pr-above.json")
      fi
    fi
  fi
  if [ "$AUTO_FINISH" = true ]; then
    # From here a refusal of this PR leaves the stop comment (die), in a real run.
    [ "$DRY_RUN" = true ] || { AUTO_STOP_REPO="$repo" AUTO_STOP_PR="$number" AUTO_STOP_HEAD="$head_sha"; }
    auto_ready
  elif ! python3 "$SCRIPTS/release_git.py" pr-check "$TMP/pr.json" --reviews "$TMP/pr-reviews.json" ${strict[@]+"${strict[@]}"}; then
    die "PR $repo#$number is not ready to merge (see the reasons above)"
  fi
  # The conflict PR was opened with the markers committed: none may remain.
  # An approval alone does not prove the resolver edited the files, so the
  # PR head is fetched into this checkout and grepped.
  git fetch --quiet origin "refs/heads/$head_ref" || die_unmarked "could not fetch $head_ref from $repo"
  # A force-push between the view and the fetch would leave $head_sha unknown
  # here, and git grep on a missing object would pass the check silently.
  git cat-file -e "$head_sha^{commit}" 2>/dev/null \
    || die_unmarked "PR $repo#$number head $head_sha is not what origin/$head_ref holds; re-run"
  # Stale: the child moved on since the PR was opened. Merging it now would
  # land an old child tip and fail post_check after the merge (ENG-2233).
  git merge-base --is-ancestor "$GREEN_SHA" "$head_sha" \
    || die "PR $repo#$number is stale: $CHILD is at ${GREEN_SHA:0:7} now and $head_ref does not contain it. Merge $CHILD into $head_ref and push, or let the next merge-up run of $CHILD open a fresh PR (it closes this one)."
  # Only the files this PR changes against the parent are checked: a doc or a
  # test fixture on the parent that shows a conflict must not block every
  # merge-up. The set comes from git, not from the conflict commit's message.
  base_sha=$(git merge-base "refs/remotes/origin/$PARENT" "$head_sha") \
    || die "PR $repo#$number: $head_ref shares no history with $PARENT"
  mapfile -d '' changed < <(git diff -z --name-only --diff-filter=d "$base_sha" "$head_sha")
  marked=""
  if [ "${#changed[@]}" -gt 0 ]; then
    # zdiff3's base line too: a resolver who deletes only the <<<<<<< and
    # >>>>>>> lines leaves ||||||| , the old base text and ======= behind.
    marked=$(git grep -l -E '^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )' "$head_sha" -- "${changed[@]/#/:(literal)}" 2>/dev/null | sed "s/^$head_sha://" || true)
  fi
  [ -z "$marked" ] || die "PR $repo#$number still has conflict markers in: $(echo "$marked" | paste -sd ' ')"
  # A merge-up never carries the child line's bookkeeping into the parent
  # (ENG-2234). The bot's own commits keep the parent's copy
  # (merge-keep-target); a person who resolved a no-marker PR the wrong way
  # round (the parent merged into the child's branch) keeps the child's, and
  # nothing else would notice. Measured against the merge base, so a parent
  # that bumped its own version since passes, merged in or not.
  if ! kept=$(python3 "$SCRIPTS/release_git.py" bookkeeping --parent "refs/remotes/origin/$PARENT" --head "$head_sha" \
                --root "$REPO" 2>"$TMP/bookkeeping.err"); then
    [ -n "$kept" ] || die_unmarked "PR $repo#$number: its bookkeeping files could not be compared with $PARENT's ($(head -1 "$TMP/bookkeeping.err"))"
    kept=$(echo "$kept" | paste -sd ' ')
    die "PR $repo#$number would carry $CHILD's bookkeeping into $PARENT: $head_ref changes $kept against its merge base with $PARENT (${base_sha:0:7}), and a merge-up keeps the parent's copy. Restore it on $head_ref and push: git checkout origin/$PARENT -- $kept, then commit (if $PARENT moved since $head_ref last merged it, merge origin/$PARENT into $head_ref first)"
  fi
  # An auto-finish reads the PR and its reviews once more, right before the merge.
  [ "$AUTO_FINISH" != true ] || auto_recheck
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: would gh pr merge $number -R $repo --merge (head $head_sha)"
    return 0
  fi
  gh pr merge "$number" -R "$repo" --merge --match-head-commit "$head_sha"
  AUTO_STOP_HEAD=""
  echo "merged PR $repo#$number (head $head_sha)"
  PR_MERGED_HEAD="$head_sha"
  PR_MERGED_REF="$head_ref"
  [ "$AUTO_FINISH" != true ] || auto_merged_comment
}

# auto_ready: the auto-finish's check of the PR finish_pr is reading (its
# locals repo, number, head_sha, head_ref, fields, required and strict):
# auto_finish.py ready, the function the sweep asked, on the same inputs: the
# view, the REST reviews, the branch activity (none when it could not be read:
# no fallback), the permission of each approver, the PR's comments, and the
# check runs, Actions runs and jobs of the head (a required check counts only
# from its source in merge_up check_sources), and every PR of the head branch
# and the PR's events (no other PR of the branch into another base, none into
# the same base that the release App did not open or whose base changed, whose
# events are read too, no change of its base: auto_finish.py head_branch_prs). UNKNOWN, and UNSTABLE while only
# the review trigger runs, are read again (AUTO_FINISH_TRIES,
# AUTO_FINISH_RETRY_SECONDS). A refusal of this head and approval dies with
# the stop comment; a transient one dies without it.
auto_ready() {
  local try status login perm codes rid tries="${AUTO_FINISH_TRIES:-3}" pause="${AUTO_FINISH_RETRY_SECONDS:-5}"
  local -a activity=() sources=()
  # strict is (--strict --required <list> [--activity <file>]): the activity, when it was read
  if [ "${#strict[@]}" -gt 3 ]; then activity=("${strict[@]:3}"); fi
  : > "$TMP/pr-permissions.txt"
  for login in $(python3 "$SCRIPTS/auto_finish.py" approvers --reviews "$TMP/pr-reviews.json"); do
    [[ "$login" =~ ^[A-Za-z0-9-]+$ ]] || continue
    if perm=$(gh api "repos/$repo/collaborators/$login/permission" --jq .permission 2>/dev/null); then
      printf '%s %s\n' "$login" "$perm" >> "$TMP/pr-permissions.txt"
    fi
  done
  gh api --paginate "repos/$repo/issues/$number/comments" > "$TMP/pr-comments.json" 2>"$TMP/pr-comments.err" \
    || die_unmarked "PR $repo#$number: its comments could not be read ($(head -1 "$TMP/pr-comments.err")), so a stop comment of an earlier auto-finish cannot be ruled out"
  # Where each required check came from (merge_up check_sources): the check runs, the Actions
  # runs and the jobs of the head. A read that fails leaves them out: not ready, and no mark.
  python3 "$SCRIPTS/release_git.py" merge-up-config --key finish.check_sources > "$TMP/pr-sources.json" 2>"$TMP/pr-sources.err" \
    || die_unmarked "release/config.json: merge_up.finish.check_sources could not be read ($(tail -1 "$TMP/pr-sources.err"))"
  rm -rf "$TMP/pr-head-jobs"
  mkdir -p "$TMP/pr-head-jobs"
  if gh api --paginate "repos/$repo/commits/$head_sha/check-runs?per_page=100" > "$TMP/pr-check-runs.json" 2>"$TMP/pr-check-runs.err" \
     && gh api --paginate "repos/$repo/actions/runs?head_sha=$head_sha&per_page=100" > "$TMP/pr-head-runs.json" 2>"$TMP/pr-check-runs.err"; then
    sources=(--check-runs "$TMP/pr-check-runs.json" --actions-runs "$TMP/pr-head-runs.json")
    for rid in $(python3 "$SCRIPTS/auto_finish.py" source-runs --required "$required" --sources "$TMP/pr-sources.json" \
                   --head-ref "$head_ref" --head "$head_sha" --pr "$number" --base "$PARENT" --repo "$repo" \
                   "${sources[@]}" 2>/dev/null || true); do
      [[ "$rid" =~ ^[0-9]+$ ]] || continue
      gh api --paginate "repos/$repo/actions/runs/$rid/jobs?filter=all&per_page=100" > "$TMP/pr-head-jobs/$rid.json" 2>/dev/null \
        || rm -f "$TMP/pr-head-jobs/$rid.json"
    done
  else
    echo "auto-finish:        the check runs or the Actions runs of ${head_sha:0:7} could not be read ($(head -1 "$TMP/pr-check-runs.err"))"
  fi
  # GitHub lists every open PR of the head branch in each run of the branch, whichever PR's
  # event started it: another PR of the branch into another base, or a base that was ever
  # changed, leaves no run this PR's alone (auto_finish.py head_branch_prs). A read that
  # fails leaves it out: not ready, and no mark. auto_recheck reads both again.
  if gh api --paginate "repos/$repo/pulls?state=all&head=${repo%%/*}:$head_ref&per_page=100" > "$TMP/pr-head-prs.json" 2>"$TMP/pr-head-prs.err"; then
    sources+=(--head-prs "$TMP/pr-head-prs.json")
    auto_other_events
  else
    echo "auto-finish:        the pull requests of $head_ref could not be read ($(head -1 "$TMP/pr-head-prs.err"))"
  fi
  sources+=(--other-events "$TMP/pr-other-events")
  if gh api --paginate "repos/$repo/issues/$number/events?per_page=100" > "$TMP/pr-events.json" 2>"$TMP/pr-events.err"; then
    sources+=(--events "$TMP/pr-events.json")
  else
    echo "auto-finish:        the events of PR $repo#$number could not be read ($(head -1 "$TMP/pr-events.err"))"
  fi
  # A workflow's run vouches only when it is a run for this PR: its number, the base $PARENT, this repo.
  sources+=(--sources "$TMP/pr-sources.json" --head-ref "$head_ref" --base "$PARENT" --repo "$repo" --jobs-dir "$TMP/pr-head-jobs")
  # Everything but the view and the reviews, which auto_recheck reads again before the merge.
  AUTO_READY_ARGS=(--required "$required" --release-app "$GIT_BOT_NAME" --permissions "$TMP/pr-permissions.txt"
                   --comments "$TMP/pr-comments.json" "${sources[@]}" ${activity[@]+"${activity[@]}"})
  for try in $(seq 1 "$tries"); do
    if [ "$try" -gt 1 ]; then
      sleep "$pause"
      gh pr view "$number" -R "$repo" --json "$fields" > "$TMP/pr.json"
      [ "$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$TMP/pr.json")" = "$head_sha" ] \
        || die_unmarked "PR $repo#$number moved off $head_sha while it was read; the next sweep reads it again"
    fi
    python3 "$SCRIPTS/auto_finish.py" ready "$TMP/pr.json" --reviews "$TMP/pr-reviews.json" "${AUTO_READY_ARGS[@]}" \
        > "$TMP/auto-ready.txt" 2>"$TMP/auto-ready.err" \
      || die_unmarked "PR $repo#$number: auto_finish.py could not read it ($(grep -v '^auto_finish.py: note: ' "$TMP/auto-ready.err" | head -1))"
    status=$(head -1 "$TMP/auto-ready.txt")
    [ "$status" = retry ] || break
  done
  sed -n 's/^auto_finish.py: note: /auto-finish:        /p' "$TMP/auto-ready.err"
  if [ "$status" = ready ]; then
    AUTO_APPROVERS=$(sed -n 's/^approvers: //p' "$TMP/auto-ready.txt")
    echo "auto-finish:        approved by $AUTO_APPROVERS on the head ${head_sha:0:7} after the last push to the branch, none of whom pushed to it; mergeable, required checks green ($required)"
    return 0
  fi
  tail -n +2 "$TMP/auto-ready.txt" | sed 's/^/auto-finish:        not ready: /'
  codes=$(tail -n +2 "$TMP/auto-ready.txt" | cut -d: -f1)
  # shellcheck disable=SC2086  # codes are words without spaces
  if [ "$status" = retry ] || ! python3 "$SCRIPTS/auto_finish.py" stop-due $codes; then
    die_unmarked "PR $repo#$number is not ready to merge (see the reasons above); nothing marks this head, so the next sweep reads it again"
  fi
  # The stop comment lists the reasons itself: the run log is not where a PR reader looks.
  AUTO_STOP_REASONS=$(tail -n +2 "$TMP/auto-ready.txt")
  die "PR $repo#$number is not ready to merge ($(echo "$codes" | paste -sd ',' | sed 's/,/, /g'); the reasons are listed below)"
}

# auto_other_events [again]: for auto_ready (and auto_recheck, "again"), the
# issue events of each other PR of the head branch into $PARENT that the
# release App opened (auto_finish.py same-base-prs, from $TMP/pr-head-prs.json),
# into $TMP/pr-other-events/<number>.json: such a PR is no other source only
# while its base never changed (auto_finish.py head_branch_prs). Usually there
# is none or one. A read that fails leaves its file out, which is not ready and
# no mark; again, it stops the run unmarked.
auto_other_events() {
  local again="${1:-}" m
  rm -rf "$TMP/pr-other-events"
  mkdir -p "$TMP/pr-other-events"
  for m in $(python3 "$SCRIPTS/auto_finish.py" same-base-prs "$TMP/pr-head-prs.json" --pr "$number" --base "$PARENT" \
               --head-ref "$head_ref" --release-app "$GIT_BOT_NAME" 2>/dev/null || true); do
    [[ "$m" =~ ^[0-9]+$ ]] || continue
    gh api --paginate "repos/$repo/issues/$m/events?per_page=100" > "$TMP/pr-other-events/$m.json" 2>"$TMP/pr-other-events.err" && continue
    rm -f "$TMP/pr-other-events/$m.json"
    [ -z "$again" ] \
      || die_unmarked "PR $repo#$number: the events of $repo#$m, another PR of $head_ref into $PARENT, could not be read again before the merge ($(head -1 "$TMP/pr-other-events.err")); nothing was merged, and the next sweep reads it again"
    echo "auto-finish:        the events of PR $repo#$m, another PR of $head_ref into $PARENT, could not be read ($(head -1 "$TMP/pr-other-events.err"))"
  done
}

# auto_recheck: the auto-finish's last read before the merge (2a review): the
# view, the REST reviews, the PRs of the head branch and the PR's events again
# (and those of the release App's other PRs of the branch into $PARENT),
# asked of auto_finish.py ready with every other input auto_ready read
# (AUTO_READY_ARGS). Between the first read and the merge
# come the fetch, the marker and bookkeeping checks and the lock verifier, and
# --match-head-commit pins only the head: a change-request or a dismissed
# approval in that window would still be merged, with the App's ruleset bypass.
# A retry (UNKNOWN, or UNSTABLE while only the review trigger runs: a review in
# the window starts it) is read again like auto_ready does (AUTO_FINISH_TRIES,
# AUTO_FINISH_RETRY_SECONDS). Anything that is no longer ready (a new head
# included, and a retry that outlasts the tries) stops the run without a stop
# comment: nothing refused this head, the PR changed, and the next sweep reads
# it again.
auto_recheck() {
  local status now try tries="${AUTO_FINISH_TRIES:-3}" pause="${AUTO_FINISH_RETRY_SECONDS:-5}"
  for try in $(seq 1 "$tries"); do
    if [ "$try" -gt 1 ]; then
      echo "auto-finish:        read again before the merge: $(tail -n +2 "$TMP/auto-final.txt" | cut -d: -f1 | paste -sd ',' | sed 's/,/, /g'), which GitHub settles by itself; reading once more ($try of $tries)"
      sleep "$pause"
    fi
    gh pr view "$number" -R "$repo" --json "$fields" > "$TMP/pr-final.json" 2>"$TMP/pr-final.err" \
      || die_unmarked "PR $repo#$number could not be read again before the merge ($(head -1 "$TMP/pr-final.err")); nothing was merged, and the next sweep reads it again"
    gh api --paginate "repos/$repo/pulls/$number/reviews" > "$TMP/pr-reviews-final.json" 2>"$TMP/pr-reviews-final.err" \
      || die_unmarked "PR $repo#$number: its reviews could not be read again before the merge ($(head -1 "$TMP/pr-reviews-final.err")); nothing was merged, and the next sweep reads it again"
    # another PR of the head branch, or a change of this PR's base, since auto_ready read them
    gh api --paginate "repos/$repo/pulls?state=all&head=${repo%%/*}:$head_ref&per_page=100" > "$TMP/pr-head-prs.json" 2>"$TMP/pr-head-prs.err" \
      || die_unmarked "PR $repo#$number: the pull requests of $head_ref could not be read again before the merge ($(head -1 "$TMP/pr-head-prs.err")); nothing was merged, and the next sweep reads it again"
    gh api --paginate "repos/$repo/issues/$number/events?per_page=100" > "$TMP/pr-events.json" 2>"$TMP/pr-events.err" \
      || die_unmarked "PR $repo#$number: its events could not be read again before the merge ($(head -1 "$TMP/pr-events.err")); nothing was merged, and the next sweep reads it again"
    auto_other_events again
    now=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("headRefOid") or "")' "$TMP/pr-final.json")
    [ "$now" = "$head_sha" ] \
      || die_unmarked "PR $repo#$number moved off ${head_sha:0:7} (to ${now:0:7}) before the merge; nothing was merged, and the next sweep reads it again"
    python3 "$SCRIPTS/auto_finish.py" ready "$TMP/pr-final.json" --reviews "$TMP/pr-reviews-final.json" "${AUTO_READY_ARGS[@]}" \
        > "$TMP/auto-final.txt" 2>"$TMP/auto-final.err" \
      || die_unmarked "PR $repo#$number: auto_finish.py could not read it again before the merge ($(grep -v '^auto_finish.py: note: ' "$TMP/auto-final.err" | head -1))"
    status=$(head -1 "$TMP/auto-final.txt")
    [ "$status" = retry ] || break
  done
  if [ "$status" != ready ]; then
    tail -n +2 "$TMP/auto-final.txt" | sed 's/^/auto-finish:        changed before the merge: /'
    die_unmarked "PR $repo#$number changed before the merge ($(tail -n +2 "$TMP/auto-final.txt" | cut -d: -f1 | paste -sd ',' | sed 's/,/, /g')); nothing was merged and nothing marks this head, so the next sweep reads it again"
  fi
  AUTO_APPROVERS=$(sed -n 's/^approvers: //p' "$TMP/auto-final.txt")
  echo "auto-finish:        read again before the merge: still ready, approved by $AUTO_APPROVERS on ${head_sha:0:7}"
}

# auto_merged_comment: the record of a merge the auto-finish made, on the PR.
# The hidden last line marks it for whoever reads the PR later. A failure is a
# warning: the merge already happened.
auto_merged_comment() {
  local body
  body="Merged by release-merge-up, started by release-auto-finish.yml: ${AUTO_APPROVERS:-a person} approved ${PR_MERGED_HEAD:0:7} after the last push to the branch without pushing to it, and the required checks are green on it. Run: $RUN_URL"
  body="$body"$'\n\n'"$(python3 "$SCRIPTS/auto_finish.py" marker merged "$PR_MERGED_HEAD")"
  if gh pr comment "$number" -R "$repo" --body "$body" >/dev/null 2>"$TMP/auto-comment.err"; then
    echo "auto-finish:        commented on PR $repo#$number with the approver and the head"
  else
    echo "::warning::merge-up: the merged PR $repo#$number got no auto-finish comment ($(head -1 "$TMP/auto-comment.err"))"
  fi
}

# auto_stop <why>: the stop comment on the auto-finish's PR, from die. Its hidden
# last line keeps the sweep away from this head until a newer approval. Posted
# once; a failure is a warning (the run is ending red anyway). AUTO_STOP_REASONS,
# the "code: text" lines of auto_finish.py ready, are listed in it when set. The
# text pasted in (a reason can quote a file name or a check name) loses every
# "<!--", removed again until none is left (taking out "<!--" from "<!<!------"
# closes up another), so nothing in it opens an HTML comment; only the last
# line is read as a marker anyway.
auto_stop() {
  local why="${1%.}" body repo="$AUTO_STOP_REPO" number="$AUTO_STOP_PR" head="$AUTO_STOP_HEAD" reasons="${AUTO_STOP_REASONS:-}"
  AUTO_STOP_HEAD=""
  while [[ "$why" == *'<!--'* ]]; do why="${why//'<!--'/}"; done
  while [[ "$reasons" == *'<!--'* ]]; do reasons="${reasons//'<!--'/}"; done
  body="release-merge-up tried to finish this pull request automatically (release-auto-finish.yml) and stopped: $why. Nothing was merged. The auto-finish does not try ${head:0:7} again until a person approves it again or a new commit is pushed; a finish by hand works as before. Run: $RUN_URL"
  [ -z "$reasons" ] || body="$body"$'\n\n'"The reasons:"$'\n'"$(printf '%s\n' "$reasons" | sed 's/^/- /')"
  body="$body"$'\n\n'"$(python3 "$SCRIPTS/auto_finish.py" marker stopped "$head")"
  if gh pr comment "$number" -R "$repo" --body "$body" >/dev/null 2>"$TMP/auto-stop.err"; then
    echo "auto-finish:        left a stop comment on PR $repo#$number for ${head:0:7}"
  else
    echo "::warning::merge-up: could not leave the auto-finish stop comment on PR $repo#$number ($(head -1 "$TMP/auto-stop.err"))"
  fi
}

# close_if_still_open <repo> <number> <merge commit> <head branch>: GitHub sometimes keeps a
# PR open after the bot merged it (seen in the OS layer repo). The parent holds the
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

# ---- --pr: the open conflict PR ---------------------------------------------------

PR_HEAD=""
if [ -n "$PR" ]; then
  gh pr view "$PR" -R "$OWN_REPO" --json headRefName,baseRefName,state,headRefOid > "$TMP/pr-$$.json" 2>/dev/null \
    || die "PR #$PR does not exist in $OWN_REPO"
  pr_field() { python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get(sys.argv[2],""))' "$TMP/pr-$$.json" "$1"; }
  head=$(pr_field headRefName)
  base=$(pr_field baseRefName)
  state=$(pr_field state)
  if [ "$state" = OPEN ] && [ "$base" = "$PARENT" ] && [[ "$head" == merge-up/$CHILD/* ]]; then
    PR_HEAD=$(pr_field headRefOid)
  else
    die "PR #$PR is not an open merge-up/$CHILD/* PR against $PARENT in $OWN_REPO"
  fi
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
[ -z "$PR" ] || echo "finish PR:          $OWN_REPO#$PR (head $PR_HEAD)"

if [ "$DRY_RUN" = false ] && [ "$TOKEN_KIND" != app ]; then
  echo "::error::merge-up: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): a GITHUB_TOKEN push gets no ruleset bypass on $PARENT and starts no rc-build. Nothing was pushed. A human runs, in this order:"
  echo "::error::  git fetch origin && git checkout $PARENT && git merge --no-ff --no-commit origin/$CHILD; python3 scripts-release/release_git.py merge-keep-target && git commit -m '$MERGE_SUBJECT' && git push origin $PARENT"
  echo "::error::  gh workflow run $RC_WORKFLOW --ref $PARENT"
  exit 1
fi

# ---- 2. the merge ----------------------------------------------------------------

if [ -n "$PR" ]; then
  PR_MERGED_HEAD=""
  PR_MERGED_REF=""
  finish_pr "$OWN_REPO" "$PR"
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: nothing pushed"
    exit 0
  fi
  git fetch --quiet origin
  NEW_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  post_check "$REPO" "$NEW_TIP" "$PR_MERGED_HEAD" "$GREEN_SHA"
  close_if_still_open "$OWN_REPO" "$PR" "$NEW_TIP" "$PR_MERGED_REF"
  close_superseded "$OWN_REPO" "$PARENT" "" "PR #$PR, merged at $NEW_TIP"
  echo "done: $CHILD merged into $PARENT at $NEW_TIP (via PR $PR)"
  exit 0
fi

if git merge-base --is-ancestor "$CHILD_TIP" "$PARENT_TIP"; then
  echo "merge:              $PARENT already contains $CHILD ($SHA7); nothing to merge"
  close_superseded "$OWN_REPO" "$PARENT" "" "$PARENT at ${PARENT_TIP:0:7}, which already contains $CHILD"
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
    git -C "$WT" rev-parse --quiet --verify MERGE_HEAD >/dev/null || { cat "$TMP/merge.log"; die "git merge failed (see above)"; }
  fi
  # The target keeps its pending versions and its bookkeeping files
  # (release_git.py merge-keep-target); a conflict there is resolved by that.
  KEPT=$(python3 "$SCRIPTS/release_git.py" merge-keep-target --git-dir "$WT" --root "$REPO") \
    || { git -C "$WT" merge --abort; die "release_git.py merge-keep-target failed (see above)"; }
  CONFLICTS=$(git -C "$WT" diff --name-only --diff-filter=U)
  if [ -n "$CONFLICTS" ]; then
    echo "merge:              merge $CHILD ($SHA7) into $PARENT conflicts in: $(echo "$CONFLICTS" | paste -sd ' ')"
    echo "$CONFLICTS" > "$TMP/conflicts"
    # A dead branch (a PR a person closed) stops the run before any rule acts.
    branch_gate
    rules_resolve "$TMP/conflicts"
    [ -s "$TMP/conflicts" ] || rules_pr "$TMP/conflicts"
    on_conflict "$TMP/conflicts"
  fi
  echo "merge:              merge $CHILD ($SHA7) into $PARENT (${PARENT_TIP:0:7}): $(git -C "$WT" diff --cached --stat | tail -1)"
  echo "                    $KEPT (versions/README.md and everything else merged)"
  if [ "$DRY_RUN" = true ]; then
    git -C "$WT" merge --abort
    close_superseded "$OWN_REPO" "$PARENT" "" "the clean merge"
    echo "dry run: nothing pushed"
    exit 0
  fi
  git -C "$WT" commit --quiet -m "$MERGE_SUBJECT"
  if git -C "$WT" push --quiet origin "HEAD:refs/heads/$PARENT" 2>"$TMP/push.log"; then
    NEW_TIP=$(git -C "$WT" rev-parse HEAD)
    break
  fi
  [ $attempt -lt 3 ] || { cat "$TMP/push.log"; die "push to $PARENT failed 3 times"; }
  echo "push rejected (attempt $attempt), refetching"
  git fetch --quiet origin
  PARENT_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
done

# ---- 3. post-check -------------------------------------------------------------

git fetch --quiet origin
[ "$(git rev-parse "refs/remotes/origin/$PARENT")" = "$NEW_TIP" ] || echo "note: $PARENT moved again after the push"
post_check "$REPO" "$NEW_TIP" "$CHILD_TIP" "$CHILD_TIP"
close_superseded "$OWN_REPO" "$PARENT" "" "the clean merge $NEW_TIP"
echo "done: $CHILD merged into $PARENT at $NEW_TIP"
