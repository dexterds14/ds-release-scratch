#!/usr/bin/env bash
set -euo pipefail

# Abandon a release line and delete the builds it produced (ENG-1935,
# ENG-2110). Run from a checkout whose origin is the repo (fetch-depth 0).
#
#   abandon.sh --line <line> [--force] [--dry-run|--no-dry-run] [--report FILE]
#
# 1. The line must be a minor or patch line (lines.py parse); the main line
#    is never abandoned here.
# 2. Unmerged commits: everything in origin/<line> that is not in
#    origin/<parent> (lines.py parent), minus the line's bookkeeping commits
#    as release_git.py unmerged defines them (the one definition promote's
#    children check uses too: single-parent commits whose diff stays within
#    versions/ and the bookkeeping files release/config.json lists; merge-up
#    keeps the target's copies of those, so they never reach the parent and
#    are not lost). Any other unmerged commit, or a child line that still
#    merges up into this one, is listed and the run refuses unless --force.
# 3. Delete the branch (gh api -X DELETE .../git/refs/heads/<line> with the
#    App token, which has the ruleset bypass).
# 4. prune-rc.sh --product P --line <line> --no-dry-run: every rc
#    pre-release whose manifest names the line and whose sha carries no
#    release tag goes, with its tag, its S3 index and the per-build prefixes
#    it lists; promoted shas and the builds a release manifest references
#    stay. Its exit code is this script's.
#
# A dry run (the default) prints the plan and prune-rc's keep and delete
# lists, deletes nothing and exits 0 (a ::warning:: says when the real run
# would refuse). The plan goes to GITHUB_STEP_SUMMARY when set.
#
# Environment: TOKEN_KIND (app|github), DRY_RUN, GH_TOKEN, GITHUB_REPOSITORY
# (the repo gh acts on): as in merge-up.sh. Only the App token can delete
# the branch once the release-lines ruleset is active, so with
# TOKEN_KIND=github a real run stops before deleting anything and prints the
# commands a human runs.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"

LINE=""
FORCE=false
DRY_RUN="${DRY_RUN:-true}"
REPORT="abandon-prune-report.json"
TOKEN_KIND="${TOKEN_KIND:-github}"
OWN_REPO="${GITHUB_REPOSITORY:-}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,35p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --line) LINE="$2"; shift 2 ;;
    --force) FORCE=true; shift ;;
    --report) REPORT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::abandon: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::abandon: $*"
  exit 1
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

[ -n "$LINE" ] || die "--line is required"
[ -n "$OWN_REPO" ] || die "GITHUB_REPOSITORY is not set (the owner/name gh acts on)"
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac

# ---- 1. the line and its parent -------------------------------------------------

git fetch --quiet origin
PARSED=$(python3 "$SCRIPTS/lines.py" --root "$REPO" parse "$LINE")
KIND=$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["kind"])' "$PARSED")
case "$KIND" in
  minor|patch) ;;
  main) die "$LINE is the main line: it is never abandoned by this job" ;;
  *) die "$LINE is not a release line of a configured product" ;;
esac
PRODUCT=$(python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])["product"])' "$PARSED")
HEADS="$TMP/heads"
git ls-remote --heads origin > "$HEADS"
LINE_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$LINE^{commit}") || die "origin has no branch $LINE"
PARENT=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" parent "$LINE")
PARENT_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$PARENT^{commit}") || die "origin has no branch $PARENT (the parent line of $LINE)"
CHILDREN=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" children "$LINE" | python3 -I -c 'import json,sys; print(" ".join(json.load(sys.stdin)))')

# ---- 2. unmerged commits -----------------------------------------------------------

UNMERGED="$TMP/unmerged"
TOTAL=$(git rev-list --count "$PARENT_TIP..$LINE_TIP")
# release_git.py unmerged lists the non-bookkeeping commits (exit 1 when
# there are any, and also on an error, which it reports on stderr with no
# list), oldest first.
rc=0
python3 "$SCRIPTS/release_git.py" unmerged "$PARENT_TIP" "$LINE_TIP" --git-dir "$REPO" --root "$REPO" > "$UNMERGED" 2>"$TMP/unmerged.err" || rc=$?
if [ "$rc" -ne 0 ] && ! [ -s "$UNMERGED" ]; then
  die "release_git.py unmerged $PARENT_TIP..$LINE_TIP failed: $(cat "$TMP/unmerged.err")"
fi
sed -i 's/^\([0-9a-f]\{7\}\)[0-9a-f]* /\1 /' "$UNMERGED"
UNMERGED_COUNT=$(wc -l < "$UNMERGED")
IGNORED=$((TOTAL - UNMERGED_COUNT))

# ---- the plan ------------------------------------------------------------------------

echo "== abandon plan (dry_run=$DRY_RUN, force=$FORCE, token=$TOKEN_KIND)"
echo "line:               $LINE ($KIND) at $LINE_TIP"
echo "parent:             $PARENT at $PARENT_TIP"
echo "child lines:        ${CHILDREN:-(none)}"
echo "unmerged commits:   $UNMERGED_COUNT not in $PARENT ($IGNORED versions/ or bookkeeping-file commit(s) ignored)"
sed 's/^/                    /' "$UNMERGED"
echo "branch:             delete $LINE"
echo "builds:             prune-rc.sh --product $PRODUCT --line $LINE (keep and delete lists below)"

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## Abandon \`$LINE\` (dry_run=$DRY_RUN, force=$FORCE)"
    echo
    echo "- line: \`$LINE\` at \`$LINE_TIP\`, parent \`$PARENT\` at \`$PARENT_TIP\`"
    echo "- child lines: ${CHILDREN:-none}"
    echo "- unmerged commits not in \`$PARENT\`: $UNMERGED_COUNT ($IGNORED bookkeeping commit(s) ignored)"
    sed 's/^/    - `/; s/$/`/' "$UNMERGED"
    echo
  } >> "$GITHUB_STEP_SUMMARY"
fi

REFUSE=""
[ "$UNMERGED_COUNT" -eq 0 ] || REFUSE="$UNMERGED_COUNT commit(s) of $LINE are not in $PARENT (listed above)"
if [ -n "$CHILDREN" ]; then
  REFUSE="${REFUSE:+$REFUSE; }child line(s) $CHILDREN still merge up into $LINE (abandon them first, or they re-parent to $PARENT)"
fi

if [ "$DRY_RUN" = true ]; then
  if [ -n "$REFUSE" ] && [ "$FORCE" != true ]; then
    echo "::warning::abandon: a real run would refuse: $REFUSE; pass force to abandon anyway"
  fi
  echo "-- prune-rc dry run"
  "$SCRIPTS/prune-rc.sh" --product "$PRODUCT" --line "$LINE" --dry-run --report "$REPORT"
  echo "dry run: nothing deleted"
  exit 0
fi

if [ -n "$REFUSE" ] && [ "$FORCE" != true ]; then
  die "refusing: $REFUSE; pass force to abandon anyway"
fi
[ -z "$REFUSE" ] || echo "::warning::abandon: force: $REFUSE"

if [ "$TOKEN_KIND" != app ]; then
  echo "::error::abandon: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): GITHUB_TOKEN gets no ruleset bypass on the release lines. Nothing was deleted. A human runs, in this order:"
  echo "::error::  git push origin --delete $LINE"
  echo "::error::  GH_TOKEN=<token> scripts-release/prune-rc.sh --product $PRODUCT --line $LINE --dry-run   # then --no-dry-run"
  exit 1
fi

# ---- 3. the branch -------------------------------------------------------------------

gh api -X DELETE "repos/${OWN_REPO}/git/refs/heads/${LINE}" >/dev/null \
  || die "could not delete branch $LINE (gh api -X DELETE repos/${OWN_REPO}/git/refs/heads/${LINE})"
[ -z "$(git ls-remote --heads origin "refs/heads/$LINE")" ] || die "origin still has branch $LINE after the delete"
echo "deleted $LINE (was $LINE_TIP)"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "- deleted \`$LINE\` (was \`$LINE_TIP\`)" >> "$GITHUB_STEP_SUMMARY"
  echo >> "$GITHUB_STEP_SUMMARY"
fi

# ---- 4. the builds -------------------------------------------------------------------------

echo "-- prune-rc"
"$SCRIPTS/prune-rc.sh" --product "$PRODUCT" --line "$LINE" --no-dry-run --report "$REPORT"
echo "done: $LINE abandoned"
