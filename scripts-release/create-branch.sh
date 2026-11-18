#!/usr/bin/env bash
set -euo pipefail

# Open a release line in both repos (ENG-1933): meta-dsensos first, then
# ds-tegra-distro. Run from a ds-tegra-distro checkout whose origin is the
# repo (fetch-depth 0, credentials for pushing already configured).
#
#   create-branch.sh --product P --seed-from REF [--dry-run|--no-dry-run]
#       Seed mode: P/main in both repos. meta-dsensos P/main at the commit
#       REF's kas/include/base.lock.yml pins; ds-tegra-distro P/main at REF
#       with one opening commit that only moves base.yml's meta-dsensos
#       branch: line to P/main (versions/ stays as seeded).
#   create-branch.sh --product P --version N.M.P [--kind auto|minor|patch] [--dry-run|--no-dry-run]
#       Line mode: tag P/vN.M.P opens the minor line P/N.x.x (pending
#       N.(M+1).0) or the patch line P/N.M.x (pending N.M.(P+1)). --kind auto
#       (the default) picks minor for N.0.0 and patch for anything else;
#       minor or patch forces one (ENG-1934: the pilot cuts both 0.x.x and
#       0.0.x from P/v0.0.1). meta-dsensos line at the commit the tag's
#       lockfile pins; ds-tegra-distro line at the tag with an opening commit
#       that writes versions/P and base.yml's branch: line.
#
# Refusals (before anything is created): product not in release/config.json;
# the line exists in either repo; the seed ref or the tag does not resolve;
# a version that is not N.M.P; the pin missing from meta-dsensos.
#
# Environment:
#   TOKEN               token the ds-tegra-distro checkout pushes with (and gh uses)
#   TOKEN_KIND          app | github. Only an App installation token can push to
#                       meta-dsensos, so a real run needs app; github gets a
#                       dry run and otherwise the commands a human runs.
#   META_REPO           owner/name of meta-dsensos (the workflow passes
#                       <repository_owner>/meta-dsensos)
#   META_DSENSOS_URL    clone/push URL of meta-dsensos (default: the https
#                       x-access-token URL with TOKEN for META_REPO; the tests
#                       pass a path)
#   META_DSENSOS_HUMAN_URL  what the human instructions name (default: the ssh
#                       URL of META_REPO)
#   DRY_RUN             true|false, overridden by --dry-run / --no-dry-run
#   GIT_BOT_NAME, GIT_BOT_EMAIL   commit identity (default from lib.sh: ds-os-release-bot[bot])
#
# Output is a plan for both repos (branch names, shas, the base.yml diff and
# the versions/ diff); a dry run stops after it with exit 0.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
# GIT_BOT_NAME and GIT_BOT_EMAIL (the release App's commit identity); this
# script defines its own die() further down, which replaces lib.sh's.
. "$SCRIPTS/lib.sh"
cd "$REPO"

PRODUCT=""
VERSION=""
KIND=auto
SEED_FROM=""
DRY_RUN="${DRY_RUN:-true}"
META_REPO="${META_REPO:-DistributedSpectrum/meta-dsensos}"
META_URL="${META_DSENSOS_URL:-https://x-access-token:${TOKEN:-}@github.com/${META_REPO}.git}"
META_HUMAN_URL="${META_DSENSOS_HUMAN_URL:-git@github.com:${META_REPO}.git}"
TOKEN_KIND="${TOKEN_KIND:-github}"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//' >&2
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

[ -n "$PRODUCT" ] || die "--product is required"
if [ -n "$VERSION" ] && [ -n "$SEED_FROM" ]; then
  die "--version and --seed-from are mutually exclusive"
fi
if [ -z "$VERSION" ] && [ -z "$SEED_FROM" ]; then
  die "one of --version (line mode) or --seed-from (seed mode) is required"
fi
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
case "$KIND" in auto|minor|patch) ;; *) die "--kind must be auto, minor or patch, got '$KIND'" ;; esac

# The product must be configured in this checkout.
if ! python3 "$SCRIPTS/version.py" --root "$REPO" products | grep -qx -- "$PRODUCT"; then
  die "product '$PRODUCT' is not in release/config.json ($(python3 "$SCRIPTS/version.py" --root "$REPO" products | paste -sd ' '))"
fi

git fetch --quiet origin
git fetch --quiet --tags origin

# ---- what to create ---------------------------------------------------------

if [ -n "$SEED_FROM" ]; then
  MODE=seed
  LINE="$PRODUCT/main"
  OLD_BRANCH=""  # read from the seed's base.yml below
  if SHA=$(git rev-parse --verify --quiet "refs/remotes/origin/$SEED_FROM^{commit}"); then
    :
  elif SHA=$(git rev-parse --verify --quiet "$SEED_FROM^{commit}"); then
    :
  else
    die "seed ref '$SEED_FROM' does not resolve to a commit on origin"
  fi
  FROM_DESC="$SEED_FROM ($SHA)"
  SUBJECT="release: open $LINE from ${SHA:0:7} [skip ci]"
  PENDING=""
  PARENT="(none: a product main has no parent line)"
else
  MODE=line
  PLAN=$(python3 "$SCRIPTS/release_git.py" line-plan "$PRODUCT" "$VERSION" --kind "$KIND") \
    || die "version '$VERSION' must be N.M.P (the tag $PRODUCT/vN.M.P; --kind minor opens $PRODUCT/N.x.x, --kind patch opens $PRODUCT/N.M.x, auto picks minor for N.0.0)"
  LINE=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["line"])' "$PLAN")
  LEVEL=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["level"])' "$PLAN")
  PENDING=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["pending"])' "$PLAN")
  TAG="$PRODUCT/v$VERSION"
  SHA=$(git rev-parse --verify --quiet "refs/tags/$TAG^{commit}") \
    || die "tag $TAG does not exist in ds-tegra-distro (git ls-remote --tags origin $TAG is empty)"
  FROM_DESC="$TAG ($SHA)"
  SUBJECT="release: open $LINE from $TAG [skip ci]"
  # Parent = nearest existing ancestor line of the same product (informational:
  # the line starts at the tag, merge-up resolves the parent again at run time).
  HEADS="$TMP/heads"
  git ls-remote --heads origin > "$HEADS"
  PARENT=$(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" parent "$LINE")
fi

# Neither repo may have the line already.
if [ -n "$(git ls-remote --heads origin "refs/heads/$LINE")" ]; then
  die "ds-tegra-distro already has branch $LINE"
fi

PIN=$(python3 "$SCRIPTS/release_git.py" lock-pin "$SHA" --git-dir "$REPO") \
  || die "could not read the meta-dsensos pin from kas/include/base.lock.yml at $SHA"

# meta-dsensos: only an App token can see the private layer repo from a
# ds-tegra-distro run; with GITHUB_TOKEN the checks are skipped and reported.
META_CHECKED=false
if [ "$TOKEN_KIND" = app ]; then
  if [ -n "$(git ls-remote --heads "$META_URL" "refs/heads/$LINE")" ]; then
    die "meta-dsensos already has branch $LINE"
  fi
  git clone --quiet --bare "$META_URL" "$TMP/meta.git"
  git -C "$TMP/meta.git" cat-file -e "$PIN^{commit}" 2>/dev/null \
    || die "meta-dsensos has no commit $PIN (the pin in base.lock.yml at $SHA)"
  META_CHECKED=true
fi

# ---- the opening commit, prepared in a temporary worktree -------------------

git worktree add --quiet --detach "$WT" "$SHA"
OLD_BRANCH=$(python3 "$SCRIPTS/release_git.py" base-branch-get "$WT/kas/include/base.yml") \
  || die "kas/include/base.yml at $SHA has no single meta-dsensos branch: line"
python3 "$SCRIPTS/release_git.py" base-branch-edit "$WT/kas/include/base.yml" "$OLD_BRANCH" "$LINE" >/dev/null \
  || die "could not rewrite the meta-dsensos branch: line in kas/include/base.yml at $SHA"
OLD_PENDING=""
if [ "$MODE" = line ]; then
  OLD_PENDING=$(cat "$WT/versions/$PRODUCT" 2>/dev/null || true)
  printf '%s\n' "$PENDING" > "$WT/versions/$PRODUCT"
fi
git -C "$WT" add -A -- kas/include/base.yml versions/
CHANGED=$(git -C "$WT" diff --cached --name-only)
case "$MODE" in
  seed) EXPECT_CHANGED="kas/include/base.yml" ;;
  line) EXPECT_CHANGED=$(printf 'kas/include/base.yml\nversions/%s' "$PRODUCT") ;;
esac
[ "$CHANGED" = "$EXPECT_CHANGED" ] || die "the opening commit would touch [$(echo "$CHANGED" | paste -sd ' ')], expected [$(echo "$EXPECT_CHANGED" | paste -sd ' ')]"
DIFF=$(git -C "$WT" diff --cached)

# ---- the plan ----------------------------------------------------------------

echo "== create-branch plan ($MODE mode, dry_run=$DRY_RUN, token=$TOKEN_KIND)"
[ "$MODE" = seed ] || echo "kind:               $LEVEL line (--kind $KIND)"
echo "product:            $PRODUCT"
echo "line:               $LINE"
echo "from:               $FROM_DESC"
echo "parent line:        $PARENT"
echo "meta-dsensos:       create $LINE at $PIN (the base.lock.yml pin at ${SHA:0:7}; checked=$META_CHECKED)"
echo "ds-tegra-distro:    create $LINE at $SHA + 1 commit \"$SUBJECT\""
if [ "$MODE" = line ]; then
  echo "versions/$PRODUCT:   ${OLD_PENDING:-(absent)} -> $PENDING ($LEVEL line)"
fi
echo "base.yml:           meta-dsensos branch $OLD_BRANCH -> $LINE"
echo "-- opening commit diff"
echo "$DIFF"
echo "--"

if [ "$DRY_RUN" = true ]; then
  echo "dry run: nothing created"
  exit 0
fi

if [ "$TOKEN_KIND" != app ]; then
  VERSIONS_CMD=""
  if [ "$MODE" = line ]; then
    VERSIONS_CMD=" && echo $PENDING > versions/$PRODUCT"
  fi
  echo "::error::create-branch: no App token (vars.DS_RELEASE_APP_CLIENT_ID unset): GITHUB_TOKEN cannot push to meta-dsensos. Nothing was created. A human runs, in this order:"
  echo "::error::  git clone $META_HUMAN_URL && cd meta-dsensos && git push origin $PIN:refs/heads/$LINE"
  echo "::error::  cd <ds-tegra-distro> && git fetch origin && git checkout -b $LINE $SHA && python3 scripts-release/release_git.py base-branch-edit kas/include/base.yml $OLD_BRANCH $LINE$VERSIONS_CMD && git add kas/include/base.yml versions/ && git commit -m '$SUBJECT' && git push origin $LINE"
  exit 1
fi

# ---- create, meta-dsensos first ---------------------------------------------

git -C "$TMP/meta.git" push --quiet origin "$PIN:refs/heads/$LINE"
echo "meta-dsensos: created $LINE at $PIN"

git -C "$WT" commit --quiet -m "$SUBJECT"
NEW=$(git -C "$WT" rev-parse HEAD)
if ! git -C "$WT" push --quiet origin "HEAD:refs/heads/$LINE"; then
  # Leave nothing half-made: a re-run refuses a line that exists in either
  # repo, so the meta-dsensos branch created above goes again.
  echo "::error::create-branch: pushing $LINE to ds-tegra-distro failed (see above); deleting meta-dsensos $LINE again"
  git -C "$TMP/meta.git" push --quiet origin ":refs/heads/$LINE" \
    || echo "::error::create-branch: deleting meta-dsensos $LINE failed; delete it by hand before a re-run"
  die "create-branch: $LINE was not created"
fi
echo "ds-tegra-distro: created $LINE at $NEW (parent $SHA)"
echo "done: $LINE open in both repos"
