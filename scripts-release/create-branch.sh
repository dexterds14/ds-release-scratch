#!/usr/bin/env bash
set -euo pipefail

# Open a release line (ENG-1933, ENG-2110). Run from a checkout whose origin
# is the repo (fetch-depth 0, credentials for pushing already configured).
#
#   create-branch.sh --version N.M.P [--kind auto|minor|patch] [--seed-from REF] [--dry-run|--no-dry-run]
#       Tag vN.M.P opens the minor line N.x.x (pending N.(M+1).0) or the
#       patch line N.M.x (pending N.M.(P+1)). --kind auto (the default)
#       picks minor for N.0.0 and patch for anything else; minor or patch
#       forces one (ENG-1934: the pilot cuts both 0.x.x and 0.0.x from
#       v0.0.1). The line starts at the tag, or with --seed-from at REF
#       (a branch, tag or sha on origin) when vN.M.P is an ancestor of it,
#       plus one opening commit, [skip ci], that writes versions/<product>
#       and nothing else, so no rc-build runs on an empty line.
#       With release/config.json `product_prefix: true` the names carry the
#       product (<product>/N.x.x, <product>/vN.M.P) and --product P picks it;
#       without the prefix the one configured product is implied.
#
# Refusals (before anything is created): the line exists on origin; the tag
# or the seed ref does not resolve; the tag is not an ancestor of the seed
# ref; a version that is not N.M.P; an opening commit that would touch
# anything but versions/<product>.
#
# Environment:
#   TOKEN_KIND          app | github. Only the App's pushes get the ruleset
#                       bypass on the release lines and start workflows, so
#                       a real run needs app; github gets a dry run and
#                       otherwise the commands a human runs.
#   DRY_RUN             true|false, overridden by --dry-run / --no-dry-run
#   GIT_BOT_NAME, GIT_BOT_EMAIL   commit identity (lib.sh: the release App's user)
#
# Output is the plan (branch name, shas, parent line, the versions/ diff); a
# dry run stops after it with exit 0.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
. "$SCRIPTS/lib.sh"

PRODUCT=""
VERSION=""
KIND=auto
SEED_FROM=""
DRY_RUN="${DRY_RUN:-true}"
TOKEN_KIND="${TOKEN_KIND:-github}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,34p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --product) PRODUCT="$2"; shift 2 ;;
    --version) VERSION="$2"; shift 2 ;;
    --kind) KIND="$2"; shift 2 ;;
    --seed-from) SEED_FROM="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::create-branch: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::create-branch: $*"
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

[ -n "$VERSION" ] || die "--version is required (the tag vN.M.P the line is cut from)"
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
case "$KIND" in auto|minor|patch) ;; *) die "--kind must be auto, minor or patch, got '$KIND'" ;; esac

# The product: named with --product when names carry the product, else the
# one configured product. Either way it must be in release/config.json.
PRODUCTS=$(python3 "$SCRIPTS/version.py" --root "$REPO" products) || die "release/config.json is invalid (see above)"
if [ -z "$PRODUCT" ]; then
  [ "$PRODUCT_PREFIX" = false ] || die "--product is required (release/config.json names $(echo "$PRODUCTS" | paste -sd ' '))"
  PRODUCT=$(echo "$PRODUCTS" | head -1)
elif ! echo "$PRODUCTS" | grep -qx -- "$PRODUCT"; then
  die "product '$PRODUCT' is not in release/config.json ($(echo "$PRODUCTS" | paste -sd ' '))"
fi

git fetch --quiet origin
git fetch --quiet --tags origin

# ---- what to create ---------------------------------------------------------

PLAN=$(python3 "$SCRIPTS/release_git.py" line-plan "$PRODUCT" "$VERSION" --kind "$KIND" --root "$REPO") \
  || die "version '$VERSION' must be N.M.P (the tag the line is cut from; --kind minor opens the N.x.x line, --kind patch the N.M.x line, auto picks minor for N.0.0)"
plan_field() { python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$PLAN" "$1"; }
LINE=$(plan_field line)
LEVEL=$(plan_field level)
PENDING=$(plan_field pending)
TAG=$(plan_field tag)
TAG_SHA=$(git rev-parse --verify --quiet "refs/tags/$TAG^{commit}") \
  || die "tag $TAG does not exist (git ls-remote --tags origin $TAG is empty)"
if [ -n "$SEED_FROM" ]; then
  # The seed ref is resolved on origin: a branch or tag by name (git
  # ls-remote), or a sha the fetch above brought in that some origin branch
  # or tag contains (a commit only this checkout has is not on origin).
  if SHA=$(git ls-remote --heads --tags origin "refs/heads/$SEED_FROM" "refs/tags/$SEED_FROM" | awk 'NR == 1 { print $1 }' | grep .); then
    SHA=$(git rev-parse --verify --quiet "$SHA^{commit}") || die "seed ref '$SEED_FROM' is on origin but its commit is not fetched"
  elif [[ "$SEED_FROM" =~ ^[0-9a-f]{7,40}$ ]] && SHA=$(git rev-parse --verify --quiet "$SEED_FROM^{commit}") \
      && [ -n "$(git for-each-ref --contains "$SHA" refs/remotes/origin/ refs/tags/)" ]; then
    :
  else
    die "seed ref '$SEED_FROM' does not resolve to a commit on origin"
  fi
  git merge-base --is-ancestor "$TAG_SHA" "$SHA" \
    || die "tag $TAG (${TAG_SHA:0:7}) is not an ancestor of --seed-from $SEED_FROM ($SHA); the line must contain the version it opens from"
  FROM_DESC="$TAG (${TAG_SHA:0:7}), seeded from $SEED_FROM ($SHA)"
  SUBJECT="release: open $LINE from $TAG (seeded from $SEED_FROM) [skip ci]"
else
  SHA="$TAG_SHA"
  FROM_DESC="$TAG ($SHA)"
  SUBJECT="release: open $LINE from $TAG [skip ci]"
fi
# Parent = nearest existing ancestor line of the same product (informational:
# merge-up resolves the parent again at run time).
HEADS="$TMP/heads"
git ls-remote --heads origin > "$HEADS"
PARENT=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" parent "$LINE")

# The line may not exist already.
if [ -n "$(git ls-remote --heads origin "refs/heads/$LINE")" ]; then
  die "origin already has branch $LINE"
fi

# ---- the opening commit, prepared in a temporary worktree -------------------

git worktree add --quiet --detach "$WT" "$SHA"
OLD_PENDING=$(cat "$WT/versions/$PRODUCT" 2>/dev/null || true)
mkdir -p "$WT/versions"
printf '%s\n' "$PENDING" > "$WT/versions/$PRODUCT"
git -C "$WT" add -A -- versions/
CHANGED=$(git -C "$WT" diff --cached --name-only)
EXPECT_CHANGED="versions/$PRODUCT"
[ "$CHANGED" = "$EXPECT_CHANGED" ] || die "the opening commit would touch [$(echo "$CHANGED" | paste -sd ' ')], expected [$EXPECT_CHANGED]"
DIFF=$(git -C "$WT" diff --cached)

# ---- the plan ----------------------------------------------------------------

echo "== create-branch plan (dry_run=$DRY_RUN, token=$TOKEN_KIND)"
echo "kind:               $LEVEL line (--kind $KIND)"
echo "product:            $PRODUCT"
echo "line:               $LINE"
echo "from:               $FROM_DESC"
echo "parent line:        $PARENT"
echo "branch:             create $LINE at $SHA + 1 commit \"$SUBJECT\""
echo "versions/$PRODUCT:   ${OLD_PENDING:-(absent)} -> $PENDING ($LEVEL line)"
echo "-- opening commit diff"
echo "$DIFF"
echo "--"

if [ "$DRY_RUN" = true ]; then
  echo "dry run: nothing created"
  exit 0
fi

if [ "$TOKEN_KIND" != app ]; then
  echo "::error::create-branch: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): a GITHUB_TOKEN push gets no ruleset bypass on the release lines and starts no workflow. Nothing was created. A human runs:"
  echo "::error::  git fetch origin && git checkout -b $LINE $SHA && echo $PENDING > versions/$PRODUCT && git add versions/ && git commit -m '$SUBJECT' && git push origin $LINE"
  exit 1
fi

# ---- create ------------------------------------------------------------------

git -C "$WT" commit --quiet -m "$SUBJECT"
NEW=$(git -C "$WT" rev-parse HEAD)
git -C "$WT" push --quiet origin "HEAD:refs/heads/$LINE" || die "pushing $LINE failed (see above); nothing was created"
echo "created $LINE at $NEW (parent $SHA)"
echo "done: $LINE open"
