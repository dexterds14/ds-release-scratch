#!/usr/bin/env bash
set -euo pipefail

# Merge a release line into its parent line (ENG-1935, ENG-2110). Run from a
# checkout whose origin is the repo (fetch-depth 0, push credentials set).
#
#   merge-up.sh --child <line> [--parent <line>] [--pr N] [--dry-run|--no-dry-run]
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
#    changes, and its head must contain the child's last green commit (a stale
#    PR is refused before anything merges; a versions-only bump on top does
#    not count); then gh pr merge --merge --match-head-commit and the
#    post-check. A PR that GitHub still shows open after that merge is
#    closed with a comment naming the merge commit. A conflict that leaves no text marker (binary, modify/delete)
#    opens the PR on the child instead, and an open merge-up PR whose head
#    already contains the child is reused.
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
  sed -n '3,52p' "$0" | sed 's/^# \{0,1\}//' >&2
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
[ -n "$OWN_REPO" ] || die "GITHUB_REPOSITORY is not set (the owner/name gh acts on)"
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
CONFIG_STRICT=$(python3 "$SCRIPTS/release_git.py" merge-up-config --key finish.strict 2>"$TMP/config.err") \
  || die "release/config.json of the scripts in use ($SCRIPTS_REF at $SCRIPTS_SHA): $(sed 's/^release_git.py: //' "$TMP/config.err" | tail -1); nothing was read or pushed"
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
  [ "$SPLICE_MODE" = act ] || return 0
  [ ! -s "$out/body.md" ] || RULES_BODY="$out/body.md"
  [ ! -s "$out/comment.md" ] || RULES_COMMENT="$out/comment.md"
  RULES_TRAILERS=$(cat "$out/trailers.txt")
  [ -z "$RULES_TRAILERS" ] || RULES_ACTED=1
  cp "$out/remaining.txt" "$files"
}

# rules_pr <conflict list file>: the rules resolved every conflict of the merge
# that is open in $WT. Never a direct push: the merge is committed without
# markers, pushed as the conflict branch and opened as a PR that needs one
# approval. Always ends the run.
rules_pr() {
  local files="$1" branch="$CONFLICT_BRANCH" sha url count
  count=$(grep -c . "$files.all" || true)
  if [ "$DRY_RUN" = true ]; then
    git -C "$WT" merge --abort
    echo "dry run: would push $branch ($PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers) and open the PR \"Merge-up $CHILD into $PARENT (resolved by rule)\" against $PARENT for one approval; nothing pushed"
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
    echo "$branch is already on origin at ${sha:0:7}: the commit this run computes, untouched; left as it is, reusing its PR"
  else
    git -C "$WT" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: ${sha:0:7}, $PARENT plus the merge of $CHILD, all $count conflicted file(s) resolved by rule, no markers"
  fi
  url=$(gh pr list -R "$OWN_REPO" --base "$PARENT" --head "$branch" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -z "$url" ]; then
    {
      echo "release-merge-up merged \`$CHILD\` into \`$PARENT\`. Git reported conflicts in $count file(s), and the merge-up rules resolved every one, so this branch has no conflict markers. The rules are mechanical: this PR is the review of what they did."
      echo
      [ -z "$RULES_BODY" ] || cat "$RULES_BODY"
      echo
      echo "This PR needs one approval. Read the diff of the files above, then dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT If a result is wrong, fix the file on \`$branch\`, push, and get a review from someone other than the last pusher. If \`$PARENT\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one."
      echo
      echo "Run: $RUN_URL"
    } > "$TMP/rules-pr-body.md"
    url=$(gh pr create -R "$OWN_REPO" --base "$PARENT" --head "$branch" \
            --title "Merge-up $CHILD into $PARENT (resolved by rule)" --body-file "$TMP/rules-pr-body.md") \
      || die "could not open the PR for $branch in $OWN_REPO"
  fi
  rules_say "every conflict resolved by rule in $url ($count file(s)); the PR has no markers and waits for one approval"
  close_superseded "$OWN_REPO" "$PARENT" "$branch" "$url"
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
    sed 's/^/- `/; s/$/`/' "$files"
    echo
    if [ -n "$nontext" ]; then
      echo "Some of these conflicts leave no text markers, so they cannot be committed for you to edit:"
      echo
      sed 's/^/- `/; s/$/`/' "$nontext"
      echo
      echo "(a binary file, or a file one side deleted or renamed). This branch is therefore \`$CHILD\` itself, and GitHub shows the PR as conflicting with no hunks. Checks and reviews start once it is mergeable. To resolve: check out \`$head\`, merge \`$base\` into it, decide every file above (keep, delete or replace it as the merge needs), commit, push. Then get a review from someone other than the last pusher and dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT"
    else
    echo "This branch is \`$base\` with \`$CHILD\` merged in. The conflict markers are committed in the files above, so the diff of this PR shows every hunk to decide. Everything else merged clean. The parent keeps its \`versions/\` files and its bookkeeping files."
    echo
    echo "To resolve: check out \`$head\`, edit the files above until no \`<<<<<<<\`, \`|||||||\` or \`>>>>>>>\` line remains, commit, push. Get a review from someone other than the last pusher, then dispatch release-merge-up.yml $ON_DEFAULT with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. $WHY_DEFAULT The bot refuses to finish while a marker remains. If \`$base\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one (the branch stays, so a resolution started here can be merged into the new branch)."
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
# supersession, which is recognised by two things together (closed by a Bot
# account that left close_superseded's comment; release_git.py person-no). No
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
     && why=$(python3 "$SCRIPTS/release_git.py" person-no --state CLOSED --issue "$TMP/head-issue.json" --comments "$TMP/head-comments.json" 2>>"$TMP/dead.err"); then
    [ -n "$why" ] || return 1   # the bot's own supersession
  else
    why="who closed it could not be read ($(tail -1 "$TMP/dead.err"))"
  fi
  DEAD_URL="$url"
  DEAD_WHY="$why"
  return 0
}

# dead_stop <branch>: the end of a run on a dead branch. Nothing is opened,
# pushed, merged or resolved by rule there, and the closed PR is neither
# reopened nor commented on. Ends like a conflict left for people: ::error::,
# a summary line, exit 1. A dry run prints the same finding and what a real
# run would not do, and exits 0.
dead_stop() {
  local branch="$1" finding ways
  if [ -n "$DEAD_URL" ]; then
    finding="a person closed $DEAD_URL without merging ($DEAD_WHY), so $branch is treated as closed"
    ways="Two ways forward: the next green commit on $CHILD gets a new branch and a new PR by itself; or a person reopens $DEAD_URL (the branch then counts as open again) and resolves or finishes it as usual, with a dispatch of release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=${DEAD_URL##*/}"
  else
    finding="$DEAD_WHY, so $branch is treated as closed"
    ways="Two ways forward: the next green commit on $CHILD gets a new branch and a new PR by itself; or, if a person closed the last PR of $branch, a person reopens it and resolves or finishes it as usual (dispatch release-merge-up.yml $ON_DEFAULT with child=$CHILD pr=<its number>). If no person closed one, the next run that can read the pull requests goes on as normal"
  fi
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
  local repo="$1" number="$2" head_sha head_ref marked base_sha
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
    strict=(--strict --required "$(cfg_names finish.required_checks)")
    if gh api --paginate -X GET "repos/$repo/activity" -f "ref=refs/heads/$head_ref" > "$TMP/pr-activity.json" 2>"$TMP/pr-activity.err"; then
      strict+=(--activity "$TMP/pr-activity.json")
    else
      echo "strict finish:      the repository activity of $head_ref could not be read ($(head -1 "$TMP/pr-activity.err")); falling back to the authors and committers of the commits above the bot's commit"
      if strict_commits "$repo" "$head_ref" "$head_sha" > "$TMP/pr-above.json"; then
        strict+=(--commits "$TMP/pr-above.json")
      fi
    fi
  fi
  if ! python3 "$SCRIPTS/release_git.py" pr-check "$TMP/pr.json" --reviews "$TMP/pr-reviews.json" ${strict[@]+"${strict[@]}"}; then
    die "PR $repo#$number is not ready to merge (see the reasons above)"
  fi
  # The conflict PR was opened with the markers committed: none may remain.
  # An approval alone does not prove the resolver edited the files, so the
  # PR head is fetched into this checkout and grepped.
  git fetch --quiet origin "refs/heads/$head_ref" || die "could not fetch $head_ref from $repo"
  # A force-push between the view and the fetch would leave $head_sha unknown
  # here, and git grep on a missing object would pass the check silently.
  git cat-file -e "$head_sha^{commit}" 2>/dev/null \
    || die "PR $repo#$number head $head_sha is not what origin/$head_ref holds; re-run"
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
