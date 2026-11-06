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
#    its open PR reused. Else commit "Merge <child> into <parent>" and
#    push (3 attempts on non-fast-forward).
# 3. Post-check: the parent tip's second parent is the child tip (or the
#    finished PR's head, whose own second parent is the child tip) and the
#    child tip is an ancestor of the parent.
# --pr N finishes a conflict PR: it must be APPROVED by someone other than
#    its last pusher, mergeStateStatus CLEAN, no failed check, and no
#    conflict marker (<<<<<<<, ||||||| or >>>>>>>) left in the files it
#    changes, and its head must contain the child's last green commit (a stale
#    PR is refused before anything merges; a versions-only bump on top does
#    not count); then gh pr merge --merge --match-head-commit and the
#    post-check. A conflict that leaves no text marker (binary, modify/delete)
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
# rc-build run that started this merge-up, see 1. gh needs GH_TOKEN. Only an App token's push gets the
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
  sed -n '3,43p' "$0" | sed 's/^# \{0,1\}//' >&2
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
      echo "(a binary file, or a file one side deleted or renamed). This branch is therefore \`$CHILD\` itself, and GitHub shows the PR as conflicting with no hunks. Checks and reviews start once it is mergeable. To resolve: check out \`$head\`, merge \`$base\` into it, decide every file above (keep, delete or replace it as the merge needs), commit, push. Then get a review from someone other than the last pusher and dispatch release-merge-up.yml with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false."
    else
    echo "This branch is \`$base\` with \`$CHILD\` merged in. The conflict markers are committed in the files above, so the diff of this PR shows every hunk to decide. Everything else merged clean. The parent keeps its \`versions/\` files and its bookkeeping files."
    echo
    echo "To resolve: check out \`$head\`, edit the files above until no \`<<<<<<<\`, \`|||||||\` or \`>>>>>>>\` line remains, commit, push. Get a review from someone other than the last pusher, then dispatch release-merge-up.yml with child=\`$CHILD\` and pr=<this PR's number>, dry_run=false. The bot refuses to finish while a marker remains. If \`$base\` moves first, merge it into this branch again. If \`$CHILD\` moves first, its next merge-up run closes this PR and opens a fresh one (the branch stays, so a resolution started here can be merged into the new branch)."
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
  gh pr create -R "$repo" --base "$base" --head "$head" --title "$title" --body-file "$body"
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
  existing=$(containing_pr "$OWN_REPO" "$PARENT" "$GREEN_SHA" "$CONFLICT_BRANCH")
  if [ -n "$existing" ]; then
    git -C "$WT" merge --abort
    echo "merge:              open conflict PR $OWN_REPO#${existing%% *} (${existing#* }) already contains $CHILD at ${GREEN_SHA:0:7}; reusing it, no new branch"
    close_superseded "$OWN_REPO" "$PARENT" "${existing#* }" "PR #${existing%% *}"
    [ "$DRY_RUN" = false ] || exit 0
    echo "::error::merge-up: $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $OWN_REPO#${existing%% *}, then dispatch release-merge-up.yml with child=$CHILD pr=${existing%% *}"
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
  fi
  url=$(conflict_pr "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "$files" "${nontext:+$TMP/nontext}") || die "could not open the conflict PR in $OWN_REPO"
  close_superseded "$OWN_REPO" "$PARENT" "$CONFLICT_BRANCH" "$url"
  echo "::error::merge-up: $CHILD -> $PARENT conflicts ($(paste -sd ' ' "$files")); resolve $url, then dispatch release-merge-up.yml with child=$CHILD pr=${url##*/}"
  exit 1
}

# commit_conflicts <git dir> <branch> <conflict file list file>: inside the
# conflicted merge, stage everything as it stands (the unresolved files keep
# their markers), commit the merge, and push it as <branch> unless origin
# already has that branch (a re-run: a resolver may have pushed to it, and
# the open PR is reused). Prints what it did.
commit_conflicts() {
  local dir="$1" branch="$2" files="$3"
  git -C "$dir" add -A
  git -C "$dir" commit --quiet -m "$MERGE_SUBJECT (conflicts to resolve)" -m "Conflict markers left in: $(paste -sd ' ' "$files")"
  if git -C "$dir" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1; then
    echo "$branch already exists on origin; left as it is (resolution in progress?), reusing its PR"
  else
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    echo "pushed $branch: $(git -C "$dir" rev-parse --short HEAD), $PARENT plus the merge of $CHILD with markers in $(paste -sd ' ' "$files")"
  fi
}

# finish_pr <repo> <number>: the bot self-check, the merge, and the post-check on <parent>.
finish_pr() {
  local repo="$1" number="$2" head_sha head_ref marked base_sha
  local -a changed=()
  # One view: the approval, the checks and the head they belong to are read
  # together, and the merge below is pinned to that head (--match-head-commit),
  # so a push after this point makes GitHub refuse the merge instead of the
  # bot merging an unchecked commit with its ruleset bypass.
  gh pr view "$number" -R "$repo" --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits,headRefOid,headRefName > "$TMP/pr.json"
  if ! python3 "$SCRIPTS/release_git.py" pr-check "$TMP/pr.json"; then
    die "PR $repo#$number is not ready to merge (see the reasons above)"
  fi
  head_sha=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$TMP/pr.json")
  head_ref=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["headRefName"])' "$TMP/pr.json")
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
  finish_pr "$OWN_REPO" "$PR"
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: nothing pushed"
    exit 0
  fi
  git fetch --quiet origin
  NEW_TIP=$(git rev-parse "refs/remotes/origin/$PARENT")
  post_check "$REPO" "$NEW_TIP" "$PR_MERGED_HEAD" "$GREEN_SHA"
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
