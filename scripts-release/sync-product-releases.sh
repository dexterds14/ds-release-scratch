#!/usr/bin/env bash
set -euo pipefail

# Open (or finish) the sync PRs that carry the development trunk into the
# product mains, in both repos (ENG-1935). Run from a ds-tegra-distro
# checkout whose origin is the repo (fetch-depth 0, push credentials set).
#
#   sync-product-releases.sh --from-ref <ref> [--products all|p1,p2] [--dry-run|--no-dry-run]
#   sync-product-releases.sh --finish <PR number> [--dry-run|--no-dry-run]
#
# Open mode, for every selected product P of release/config.json whose
# P/main exists in both repos and does not already contain <ref>:
#   1. <ref> resolves to a sha in ds-tegra-distro; its kas/include/base.lock.yml
#      pins the meta-dsensos commit the trunk is at (release_git.py lock-pin).
#      <ref> may be another product's main (vertex/main as the trunk); that
#      product is then skipped as the source.
#   2. meta-dsensos (skipped when P/main there already contains the pin): in
#      a temporary clone, sync/P/<sha7> = P/main + `git merge --no-ff <pin>`,
#      pushed, PR against P/main titled "Sync <ref> <sha7> into P/main". The
#      body says this PR merges first. A conflict aborts the merge and pushes
#      the pin itself as sync/P/<sha7> (a PR needs commits between base and
#      head, so the branch cannot sit at the P/main tip); the PR is titled
#      "(conflict)" and lists the files.
#   3. ds-tegra-distro: in a temporary worktree, sync/P/<sha7> = P/main +
#      `git merge --no-ff --no-commit <sha>` and `release_git.py
#      merge-keep-target P/main` (P/main keeps its versions/<product> files
#      and its meta-dsensos branch name; versions/README.md, the rest of
#      base.yml, the lockfile and everything else merge normally), pushed,
#      PR against P/main whose body links the meta-dsensos PR and says to
#      merge it first. The same conflict rule: the trunk sha becomes the
#      branch and the PR is marked.
#   An existing sync/P/<sha7> branch is reused as it is, and an open PR for
#   it is reused. The job never merges in open mode.
# Finish mode (--finish N): the open PR N must sit in one of the two repos
#   with head sync/<product>/<sha7> and base <product>/main. The merge-up
#   self-check (release_git.py pr-check: APPROVED by someone other than the
#   last pusher, mergeStateStatus CLEAN, no failed check), then
#   `gh pr merge N --merge` and a post-check that the new tip of
#   <product>/main is a merge commit whose second parent is the PR head.
# A dry run (the default) prints, per product, the two branch names, the
# merge bases and whether each merge would conflict (git merge-tree
# --write-tree), or the finish self-check, and creates nothing.
#
# Environment: TOKEN, TOKEN_KIND (app|github), META_DSENSOS_URL,
# META_DSENSOS_HUMAN_URL, DISTRO_REPO, META_REPO (owner/name for gh),
# DRY_RUN, GIT_BOT_NAME, GIT_BOT_EMAIL, GH_TOKEN: as in merge-up.sh. Only
# the App token can push to meta-dsensos and make the PR gate run on a
# GitHub-created PR, so with TOKEN_KIND=github a real run stops before any
# push and prints the commands a human runs.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
# GIT_BOT_NAME and GIT_BOT_EMAIL (the release App's commit identity); this
# script defines its own die() further down, which replaces lib.sh's.
. "$SCRIPTS/lib.sh"
cd "$REPO"

FROM_REF=""
PRODUCTS="all"
FINISH=""
DRY_RUN="${DRY_RUN:-true}"
TOKEN_KIND="${TOKEN_KIND:-github}"
DISTRO_REPO="${DISTRO_REPO:-DistributedSpectrum/ds-tegra-distro}"
META_REPO="${META_REPO:-DistributedSpectrum/meta-dsensos}"
META_URL="${META_DSENSOS_URL:-https://x-access-token:${TOKEN:-}@github.com/${META_REPO}.git}"
META_HUMAN_URL="${META_DSENSOS_HUMAN_URL:-git@github.com:${META_REPO}.git}"
RC_WORKFLOW="release-rc-build.yml"
SYNC_WORKFLOW="release-sync-product-releases.yml"
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-$DISTRO_REPO}/actions/runs/${GITHUB_RUN_ID:-local}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,48p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --from-ref) FROM_REF="$2"; shift 2 ;;
    --products) PRODUCTS="$2"; shift 2 ;;
    --finish) FINISH="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::sync: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::sync: $*"
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

case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
if [ -n "$FINISH" ] && ! [[ "$FINISH" =~ ^[0-9]+$ ]]; then
  die "--finish must be a PR number, got '$FINISH'"
fi
[ -n "$FINISH" ] || [ -n "$FROM_REF" ] || die "--from-ref is required (or --finish <PR>)"

git fetch --quiet origin
mapfile -t ALL_PRODUCTS < <(python3 "$SCRIPTS/version.py" --root "$REPO" products)

# ---- helpers ---------------------------------------------------------------------

# json_get <file> <key>
json_get() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2]); print("" if v is None else v)' "$1" "$2"; }

# trial_merge <git dir> <ours> <theirs>: conflicted paths on stdout (one per
# line); exit 0 clean, 1 conflicts. git merge-tree --write-tree (git >= 2.38).
trial_merge() {
  local out rc=0
  out=$(git -C "$1" merge-tree --write-tree --name-only "$2" "$3" 2>"$TMP/merge-tree.err") || rc=$?
  case "$rc" in
    0) return 0 ;;
    1) sed -n '2,/^$/p' <<<"$out" | sed '/^$/d'; return 1 ;;
    *) cat "$TMP/merge-tree.err" >&2; die "git merge-tree $2 $3 failed in $1" ;;
  esac
}

# open_pr <repo> <base> <head> <title> <body file>: prints the PR url; an open PR for the head is reused.
open_pr() {
  local repo="$1" base="$2" head="$3" title="$4" body="$5" url
  url=$(gh pr list -R "$repo" --base "$base" --head "$head" --state open --json url --jq '.[0].url' 2>/dev/null || true)
  if [ -n "$url" ]; then
    echo "$url"
    return 0
  fi
  gh pr create -R "$repo" --base "$base" --head "$head" --title "$title" --body-file "$body"
}

# post_check <git dir> <new tip> <expected second parent>
post_check() {
  local dir="$1" tip="$2" second="$3" got
  got=$(git -C "$dir" rev-parse --verify --quiet "$tip^2") || die "post-check: $tip has no second parent (not a merge commit: was the PR squashed?)"
  [ "$got" = "$second" ] || die "post-check: $tip^2 is $got, expected $second"
  echo "post-check ok: $tip merges $second"
}

# ---- finish mode -------------------------------------------------------------------

if [ -n "$FINISH" ]; then
  PR_REPO=""
  for repo in "$DISTRO_REPO" "$META_REPO"; do
    if gh pr view "$FINISH" -R "$repo" --json headRefName,baseRefName,state,headRefOid,url,title > "$TMP/pr-$$.json" 2>/dev/null; then
      head=$(json_get "$TMP/pr-$$.json" headRefName)
      base=$(json_get "$TMP/pr-$$.json" baseRefName)
      state=$(json_get "$TMP/pr-$$.json" state)
      if [ "$state" = OPEN ] && [[ "$head" =~ ^sync/([a-z][a-z0-9]*)/([0-9a-f]{7})$ ]]; then
        product="${BASH_REMATCH[1]}"
        if [ "$base" = "$product/main" ] && printf '%s\n' "${ALL_PRODUCTS[@]}" | grep -qx -- "$product"; then
          [ -z "$PR_REPO" ] || die "PR #$FINISH is an open sync PR in both $PR_REPO and $repo; pass the right one"
          PR_REPO="$repo"
          PR_HEAD_NAME="$head"
          PR_PRODUCT="$product"
          PR_HEAD=$(json_get "$TMP/pr-$$.json" headRefOid)
          PR_URL=$(json_get "$TMP/pr-$$.json" url)
          PR_TITLE=$(json_get "$TMP/pr-$$.json" title)
        fi
      fi
    fi
  done
  [ -n "$PR_REPO" ] || die "PR #$FINISH is not an open sync/<product>/<sha7> PR against <product>/main in $DISTRO_REPO or $META_REPO (with GITHUB_TOKEN only $DISTRO_REPO can be searched)"
  MAIN="$PR_PRODUCT/main"
  echo "== sync finish (dry_run=$DRY_RUN, token=$TOKEN_KIND)"
  echo "PR:                 $PR_REPO#$FINISH $PR_URL"
  echo "title:              $PR_TITLE"
  echo "head:               $PR_HEAD_NAME at $PR_HEAD"
  echo "base:               $MAIN"
  gh pr view "$FINISH" -R "$PR_REPO" --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits > "$TMP/pr.json"
  if ! python3 "$SCRIPTS/release_git.py" pr-check "$TMP/pr.json"; then
    die "PR $PR_REPO#$FINISH is not ready to merge (see the reasons above)"
  fi
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: would gh pr merge $FINISH -R $PR_REPO --merge (head $PR_HEAD); nothing merged"
    exit 0
  fi
  if [ "$TOKEN_KIND" != app ]; then
    echo "::error::sync: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): a GITHUB_TOKEN merge gets no ruleset bypass and starts no rc-build. Nothing was merged. A human with bypass rights runs:"
    echo "::error::  gh pr merge $FINISH -R $PR_REPO --merge   # a merge commit, never squash"
    [ "$PR_REPO" != "$DISTRO_REPO" ] || echo "::error::  gh workflow run $RC_WORKFLOW -R $DISTRO_REPO --ref $MAIN"
    exit 1
  fi
  gh pr merge "$FINISH" -R "$PR_REPO" --merge
  echo "merged PR $PR_REPO#$FINISH (head $PR_HEAD)"
  if [ "$PR_REPO" = "$DISTRO_REPO" ]; then
    git fetch --quiet origin
    NEW_TIP=$(git rev-parse "refs/remotes/origin/$MAIN")
    post_check "$REPO" "$NEW_TIP" "$PR_HEAD"
    echo "::notice::sync: $MAIN is at $NEW_TIP; the push starts $RC_WORKFLOW there"
  else
    META="$TMP/meta"
    git clone --quiet --no-tags --branch "$MAIN" --single-branch "$META_URL" "$META" || die "could not clone meta-dsensos $MAIN after the merge"
    NEW_TIP=$(git -C "$META" rev-parse HEAD)
    post_check "$META" "$NEW_TIP" "$PR_HEAD"
    NEXT=$(gh pr list -R "$DISTRO_REPO" --base "$MAIN" --head "$PR_HEAD_NAME" --state open --json number --jq '.[0].number' 2>/dev/null || true)
    if [ -n "$NEXT" ]; then
      echo "::notice::sync: meta-dsensos $MAIN is at $NEW_TIP; now finish the ds-tegra-distro PR: dispatch $SYNC_WORKFLOW with finish=$NEXT dry_run=false once it is approved and its checks pass"
    else
      echo "::notice::sync: meta-dsensos $MAIN is at $NEW_TIP; no open ds-tegra-distro PR on $PR_HEAD_NAME was found"
    fi
  fi
  echo "done: PR $PR_REPO#$FINISH merged into $MAIN"
  exit 0
fi

# ---- open mode: the trunk commit --------------------------------------------------------

if SHA=$(git rev-parse --verify --quiet "refs/remotes/origin/$FROM_REF^{commit}"); then
  :
elif SHA=$(git rev-parse --verify --quiet "refs/tags/$FROM_REF^{commit}"); then
  :
elif [[ "$FROM_REF" =~ ^[0-9a-f]{7,40}$ ]] && SHA=$(git rev-parse --verify --quiet "$FROM_REF^{commit}"); then
  :
else
  die "from_ref '$FROM_REF' does not resolve to a branch, tag or commit on origin"
fi
SHA7="${SHA:0:7}"
PIN=$(python3 "$SCRIPTS/release_git.py" lock-pin "$SHA" --git-dir "$REPO") || die "could not read the meta-dsensos pin from kas/include/base.lock.yml at $SHA"
PIN7="${PIN:0:7}"

if [ "$PRODUCTS" = all ]; then
  SELECTED=("${ALL_PRODUCTS[@]}")
else
  IFS=', ' read -r -a SELECTED <<<"$PRODUCTS"
  for p in "${SELECTED[@]}"; do
    printf '%s\n' "${ALL_PRODUCTS[@]}" | grep -qx -- "$p" || die "product '$p' is not in release/config.json (${ALL_PRODUCTS[*]})"
  done
fi
[ ${#SELECTED[@]} -gt 0 ] || die "no product selected"

HEADS="$TMP/heads"
git ls-remote --heads origin > "$HEADS"

META=""
if [ "$TOKEN_KIND" = app ]; then
  META="$TMP/meta"
  git clone --quiet --no-tags "$META_URL" "$META" || die "could not clone meta-dsensos ($META_REPO)"
  git -C "$META" cat-file -e "$PIN^{commit}" 2>/dev/null || die "meta-dsensos has no commit $PIN (the pin in base.lock.yml at $SHA7)"
fi

echo "== sync plan (dry_run=$DRY_RUN, token=$TOKEN_KIND)"
echo "from:               $FROM_REF at $SHA"
echo "meta-dsensos pin:   $PIN"
echo "products:           ${SELECTED[*]}"

NOAPP=0
OPENED=()
for P in "${SELECTED[@]}"; do
  MAIN="$P/main"
  BRANCH="sync/$P/$SHA7"
  TITLE="Sync $FROM_REF $SHA7 into $MAIN"
  echo "-- $P"
  # Syncing from a product main (vertex/main as the de facto trunk once
  # ota-dev is retired): that product is the source, not a target.
  if [ "$FROM_REF" = "$MAIN" ]; then
    echo "::notice::sync: $P: $MAIN is the sync source; skipped"
    continue
  fi
  if ! MAIN_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$MAIN^{commit}"); then
    echo "::notice::sync: $P: ds-tegra-distro has no $MAIN (not seeded); skipped"
    continue
  fi
  if git merge-base --is-ancestor "$SHA" "$MAIN_TIP"; then
    echo "::notice::sync: $P: $MAIN ($MAIN_TIP) already contains $FROM_REF ($SHA7); nothing to sync"
    continue
  fi
  git show "$MAIN_TIP:kas/include/base.yml" > "$TMP/main-base.yml" 2>/dev/null || die "$P: $MAIN has no kas/include/base.yml"
  MAIN_META=$(python3 "$SCRIPTS/release_git.py" base-branch-get "$TMP/main-base.yml") || die "$P: base.yml at $MAIN has no single meta-dsensos branch: line"
  [ "$MAIN_META" = "$MAIN" ] || die "$P: base.yml at $MAIN tracks meta-dsensos $MAIN_META, expected $MAIN (not opened by create-branch?)"
  git cat-file -e "$MAIN_TIP:versions" 2>/dev/null || die "$P: $MAIN has no versions/ directory; the merge keeps the target's versions/"
  DISTRO_BRANCH_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$BRANCH^{commit}" || true)
  DISTRO_BASE=$(git merge-base "$MAIN_TIP" "$SHA" || true)
  echo "ds-tegra-distro:    $MAIN at $MAIN_TIP; branch $BRANCH${DISTRO_BRANCH_TIP:+ (exists at $DISTRO_BRANCH_TIP, reused)}"
  echo "                    merge base with $SHA7: ${DISTRO_BASE:-(none)}"
  DISTRO_TRIAL=""
  if [ -z "$DISTRO_BRANCH_TIP" ]; then
    if trial_merge "$REPO" "$MAIN_TIP" "$SHA" > "$TMP/distro-trial"; then
      echo "                    trial merge: clean"
    else
      # versions/<product> files and base.yml do not count (the real run keeps or
      # normalises them); versions/README.md is a doc, so a conflict there does.
      DISTRO_TRIAL=$(awk '!($0 == "kas/include/base.yml" || ($0 ~ /^versions\/[^\/]+$/ && $0 != "versions/README.md"))' "$TMP/distro-trial" | paste -sd ' ' || true)
      if [ -n "$DISTRO_TRIAL" ]; then
        echo "                    trial merge: conflicts in: $DISTRO_TRIAL"
      else
        echo "                    trial merge: clean (only versions/ or base.yml differ, and $MAIN keeps its copies)"
      fi
    fi
  fi

  META_NEEDED=unknown
  META_MAIN_TIP=""
  META_BRANCH_TIP=""
  META_TRIAL=""
  if [ -z "$META" ]; then
    echo "meta-dsensos:       not checked (no App token; a dry run with GITHUB_TOKEN cannot read the layer repo)"
  else
    META_MAIN_TIP=$(git -C "$META" rev-parse --verify --quiet "refs/remotes/origin/$MAIN^{commit}" || true)
    if [ -z "$META_MAIN_TIP" ]; then
      echo "::notice::sync: $P: meta-dsensos has no $MAIN (not seeded); skipped"
      continue
    fi
    META_BRANCH_TIP=$(git -C "$META" rev-parse --verify --quiet "refs/remotes/origin/$BRANCH^{commit}" || true)
    if git -C "$META" merge-base --is-ancestor "$PIN" "$META_MAIN_TIP"; then
      META_NEEDED=false
      echo "meta-dsensos:       $MAIN at $META_MAIN_TIP already contains the pin $PIN7; no meta-dsensos PR needed"
    else
      META_NEEDED=true
      META_BASE=$(git -C "$META" merge-base "$META_MAIN_TIP" "$PIN" || true)
      echo "meta-dsensos:       $MAIN at $META_MAIN_TIP; branch $BRANCH${META_BRANCH_TIP:+ (exists at $META_BRANCH_TIP, reused)}"
      echo "                    merge base with $PIN7: ${META_BASE:-(none)}"
      if [ -z "$META_BRANCH_TIP" ]; then
        if trial_merge "$META" "$META_MAIN_TIP" "$PIN" > "$TMP/meta-trial"; then
          echo "                    trial merge: clean"
        else
          META_TRIAL=$(paste -sd ' ' < "$TMP/meta-trial")
          echo "                    trial merge: conflicts in: $META_TRIAL"
        fi
      fi
    fi
  fi

  if [ "$DRY_RUN" = true ]; then
    continue
  fi
  if [ "$TOKEN_KIND" != app ]; then
    NOAPP=1
    echo "::error::sync: $P: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): GITHUB_TOKEN cannot push to meta-dsensos and a PR it opens gets no pull_request checks. Nothing was pushed. A human runs, in this order:"
    echo "::error::  git clone $META_HUMAN_URL && cd meta-dsensos && git checkout -b $BRANCH origin/$MAIN && git merge --no-ff -m '$TITLE (meta-dsensos $PIN7)' $PIN && git push -u origin $BRANCH && gh pr create -R $META_REPO --base $MAIN --head $BRANCH --title '$TITLE' --body 'merge first; carries meta-dsensos $PIN'   # skip if origin/$MAIN already contains $PIN"
    echo "::error::  cd <ds-tegra-distro> && git fetch origin && git checkout -b $BRANCH origin/$MAIN && git merge --no-ff --no-commit $SHA; python3 scripts-release/release_git.py merge-keep-target $MAIN && git commit -m '$TITLE' && git push -u origin $BRANCH && gh pr create -R $DISTRO_REPO --base $MAIN --head $BRANCH --title '$TITLE' --body 'merge the meta-dsensos PR first'"
    continue
  fi

  # ---- meta-dsensos half ----
  META_PR_URL=""
  META_PR_NOTE="meta-dsensos $MAIN already contains the pin \`$PIN\`; no meta-dsensos PR is needed."
  if [ "$META_NEEDED" = true ]; then
    META_CONFLICTS=""
    if [ -n "$META_BRANCH_TIP" ]; then
      META_HEAD="$META_BRANCH_TIP"
      echo "meta-dsensos: $BRANCH exists at $META_HEAD; not re-pushed"
    else
      git -C "$META" checkout --quiet -B "$BRANCH" "refs/remotes/origin/$MAIN"
      if git -C "$META" merge --no-ff --no-commit "$PIN" > "$TMP/meta-merge.log" 2>&1; then
        :
      else
        git -C "$META" rev-parse --quiet --verify MERGE_HEAD >/dev/null || { cat "$TMP/meta-merge.log"; die "$P: meta-dsensos git merge failed (see above)"; }
        META_CONFLICTS=$(git -C "$META" diff --name-only --diff-filter=U)
      fi
      if [ -n "$META_CONFLICTS" ]; then
        git -C "$META" merge --abort
        META_HEAD="$PIN"
        echo "meta-dsensos: merge $PIN7 into $MAIN conflicts in: $(echo "$META_CONFLICTS" | paste -sd ' '); pushing the pin itself as $BRANCH"
      else
        git -C "$META" commit --quiet -m "$TITLE (meta-dsensos $PIN7)"
        META_HEAD=$(git -C "$META" rev-parse HEAD)
      fi
      git -C "$META" push --quiet origin "$META_HEAD:refs/heads/$BRANCH"
      echo "meta-dsensos: pushed $BRANCH at $META_HEAD"
    fi
    {
      echo "Sync of the development trunk into \`$MAIN\` (meta-dsensos half)."
      echo
      echo "- ds-tegra-distro \`$FROM_REF\` at \`$SHA\` pins meta-dsensos \`$PIN\`; this branch brings that commit into \`$MAIN\`."
      echo "- **Merge this PR first.** The ds-tegra-distro sync PR on \`$BRANCH\` pins \`$PIN\`, and its checks pass only once \`$MAIN\` here contains it (kas containment)."
      echo "- Merge with a merge commit, never squash: dispatch \`$SYNC_WORKFLOW\` in ds-tegra-distro with \`finish=<this PR's number>\` and \`dry_run=false\` once it is approved."
      if [ -n "$META_CONFLICTS" ]; then
        echo
        echo "Merging \`$PIN7\` into \`$MAIN\` conflicts in:"
        echo
        echo "$META_CONFLICTS" | sed 's/^/- `/; s/$/`/'
        echo
        echo "This branch is the trunk commit itself. Merge \`$MAIN\` into it, resolve the files, push, then get a review from someone other than the last pusher."
      fi
      echo
      echo "Run: $RUN_URL"
    } > "$TMP/meta-body.md"
    META_TITLE="$TITLE"
    [ -z "$META_CONFLICTS" ] || META_TITLE="$TITLE (conflict)"
    META_PR_URL=$(open_pr "$META_REPO" "$MAIN" "$BRANCH" "$META_TITLE" "$TMP/meta-body.md") || die "$P: could not open the meta-dsensos PR"
    echo "meta-dsensos: PR $META_PR_URL"
    META_PR_NOTE="**Merge the meta-dsensos PR first:** $META_PR_URL (it brings \`$PIN\` onto meta-dsensos \`$MAIN\`; until then this PR's checks fail kas containment)."
    OPENED+=("$META_PR_URL")
  fi

  # ---- ds-tegra-distro half ----
  DISTRO_CONFLICTS=""
  if [ -n "$DISTRO_BRANCH_TIP" ]; then
    DISTRO_HEAD="$DISTRO_BRANCH_TIP"
    echo "ds-tegra-distro: $BRANCH exists at $DISTRO_HEAD; not re-pushed"
  else
    if [ -d "$WT" ]; then
      git worktree remove --force "$WT" >/dev/null 2>&1 || true
    fi
    git worktree add --quiet --detach "$WT" "$MAIN_TIP"
    if git -C "$WT" merge --no-ff --no-commit "$SHA" > "$TMP/merge.log" 2>&1; then
      :
    else
      git -C "$WT" rev-parse --quiet --verify MERGE_HEAD >/dev/null || { cat "$TMP/merge.log"; die "$P: ds-tegra-distro git merge failed (see above)"; }
    fi
    # P/main keeps its versions/<product> files and its meta-dsensos branch
    # name; versions/README.md and the rest of base.yml merge
    # (release_git.py merge-keep-target), so a new layer stanza or a README
    # fix on the source reaches P/main.
    BASE_CONFLICT=""
    python3 "$SCRIPTS/release_git.py" merge-keep-target "$MAIN" --git-dir "$WT" > /dev/null 2>&1 || BASE_CONFLICT=kas/include/base.yml
    DISTRO_CONFLICTS=$(printf '%s\n%s\n' "$(git -C "$WT" diff --name-only --diff-filter=U)" "$BASE_CONFLICT" | grep -v '^$' | sort -u || true)
    if [ -n "$DISTRO_CONFLICTS" ]; then
      git -C "$WT" merge --abort
      DISTRO_HEAD="$SHA"
      echo "ds-tegra-distro: merge $SHA7 into $MAIN conflicts in: $(echo "$DISTRO_CONFLICTS" | paste -sd ' '); pushing the trunk commit itself as $BRANCH"
    else
      git -C "$WT" commit --quiet -m "$TITLE"
      DISTRO_HEAD=$(git -C "$WT" rev-parse HEAD)
      echo "ds-tegra-distro: merge $SHA7 into $MAIN: $(git -C "$WT" diff --stat HEAD^ HEAD | tail -1)"
      echo "                 kept from $MAIN: its versions/<product> files and the meta-dsensos branch in base.yml (versions/README.md and the rest of base.yml merged); lockfile pin after merge: $(python3 "$SCRIPTS/release_git.py" lock-pin --file "$WT/kas/include/base.lock.yml" HEAD)"
    fi
    git push --quiet origin "$DISTRO_HEAD:refs/heads/$BRANCH"
    echo "ds-tegra-distro: pushed $BRANCH at $DISTRO_HEAD"
  fi
  {
    echo "Sync of \`$FROM_REF\` (\`$SHA\`) into \`$MAIN\`."
    echo
    echo "- One merge commit; \`$MAIN\` keeps its \`versions/<product>\` files and its meta-dsensos branch name in \`kas/include/base.yml\`; \`versions/README.md\`, the rest of \`base.yml\`, the lockfile and everything else merge normally. meta-dsensos pin after the merge: \`$PIN\`."
    echo "- $META_PR_NOTE"
    echo "- Merge with a merge commit, never squash: dispatch \`$SYNC_WORKFLOW\` with \`finish=<this PR's number>\` and \`dry_run=false\` once it is approved and its checks pass. Merging starts \`$RC_WORKFLOW\` on \`$MAIN\`."
    if [ -n "$DISTRO_CONFLICTS" ]; then
      echo
      echo "Merging \`$SHA7\` into \`$MAIN\` conflicts in:"
      echo
      echo "$DISTRO_CONFLICTS" | sed 's/^/- `/; s/$/`/'
      echo
      echo "This branch is the trunk commit itself. Merge \`$MAIN\` into it, resolve the files, restore \`$MAIN\`'s \`versions/<product>\` files (not the README) with \`git checkout origin/$MAIN -- versions/<product>\` and set the meta-dsensos branch in \`kas/include/base.yml\` back to \`$MAIN\` (\`python3 scripts-release/release_git.py base-branch-edit kas/include/base.yml <branch> $MAIN\`), push, then get a review from someone other than the last pusher."
    fi
    echo
    echo "Run: $RUN_URL"
  } > "$TMP/distro-body.md"
  DISTRO_TITLE="$TITLE"
  [ -z "$DISTRO_CONFLICTS" ] || DISTRO_TITLE="$TITLE (conflict)"
  DISTRO_PR_URL=$(open_pr "$DISTRO_REPO" "$MAIN" "$BRANCH" "$DISTRO_TITLE" "$TMP/distro-body.md") || die "$P: could not open the ds-tegra-distro PR"
  echo "ds-tegra-distro: PR $DISTRO_PR_URL"
  OPENED+=("$DISTRO_PR_URL")
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    {
      echo "- **$P**: ${META_PR_URL:+meta-dsensos $META_PR_URL (merge first), }ds-tegra-distro $DISTRO_PR_URL${DISTRO_CONFLICTS:+ (conflict)}"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
done

if [ "$DRY_RUN" = true ]; then
  echo "dry run: nothing pushed, no PR opened"
  exit 0
fi
[ "$NOAPP" = 0 ] || exit 1
echo "done: ${#OPENED[@]} PR(s): ${OPENED[*]:-none}"
