#!/usr/bin/env bash
set -euo pipefail

# Promote the green tip of a release line to a release (ENG-1934, ENG-2110):
# the check half runs first in release-promote.yml and decides what gets
# built, the publish half runs after the release build passed its gate. Run
# from a checkout whose origin is the repo (fetch-depth 0, push credentials
# set); the product comes from lines.py parse (the one product of
# release/config.json when names carry no product).
#
#   promote.sh --check [--line <ref>] [--sha <sha>] [--bump auto|major|minor|patch]
#              [--merge-children] [--dry-run|--no-dry-run] [--github-output FILE]
#     1. <ref> (default GITHUB_REF_NAME) is a release line of a configured
#        product (lines.py parse), and its origin tip is <sha> when given
#        (the sha the run was dispatched at: a line that moved since is
#        refused, re-run on the new tip).
#     2. The tip is green: it has a successful release-rc-build run on this
#        line (gh run list), and the pre-release rc/<sha7> exists, is
#        published, is a pre-release and its tag points at the tip. Its
#        release-manifest.json asset names the RC run. The walk past trailing
#        versions/-only commits (release_git.py walk-versions-only) only serves
#        the diagnosis: a tip that is the previous promote's bump commit is
#        refused as "nothing new to promote" (step 4), any other versions/-only
#        tip is refused because the release build runs at github.sha, the tip,
#        and a tag at the older green sha would not match it.
#     3. Every child line of this line (lines.py children) is merged: no commit
#        in <line>..<child> other than bookkeeping (versions/ and the
#        bookkeeping_files of release/config.json only: release_git.py
#        unmerged). Else fail listing them per child; with --merge-children
#        run merge-up.sh --child for each in version order and then stop with
#        exit 1 (the new tip needs its own rc-build run first).
#     4. The pending version (versions/<product> at the RC sha, which the RC
#        manifest must agree with) fits the line (release_git.py promote-plan:
#        any X.Y.Z on main, N.M.0 on N.x.x, N.M.P on N.M.x), no tag
#        v<pending> exists on origin, and the bump level (auto = major on
#        main, minor on N.x.x, patch on N.M.x) leaves a pending version that
#        fits the line too.
#     Prints the plan (tag, release title, build matrix, bump commit) and
#     writes the outputs below. A dry run stops with proceed=false, exit 0.
#
#   promote.sh --publish --manifest release-manifest.json --bump major|minor|patch
#              [--github-output FILE]
#     From the release manifest the gate wrote (kind release): create the tag
#     v<version> at the sha through the API (idempotent: exists at the same
#     sha -> continue, elsewhere -> fail); draft release on that tag titled
#     "v<version> (staging)", upload release-manifest.json, every artifact
#     of each build (its debs and extra files) and each flavor's manifest.json
#     and SHA256SUMS from S3, publish as a pre-release (a published release is left alone, a
#     draft is resumed); copy release-manifest.json to
#     s3://<bucket>/release/<product>/v<version>/; then the bump:
#     versions/<product> on the line becomes version.py bump(<version>,
#     <level>), committed as the bot "release: v<version> promoted; pending
#     <next> [skip ci]" and pushed (3 attempts on a rejected push: refetch,
#     re-apply the one-file change, retry). Already bumped -> skipped. Writes
#     staging_request.
#
# Outputs (GITHUB_OUTPUT when set, or --github-output), check:
#   proceed=true|false (false in a dry run), product, line, kind, sha (to
#   tag), version (the pending version promoted), tag, rc_tag, rc_run_id,
#   rc_run_url, bump (the resolved level), next_version, matrix
#   ({"include": [{"flavor": ...}]}), pending
# publish: tag, release_url, next_version, staging_request (one-line JSON:
#   {product, version, sha, line, tag, rc_run, release_run,
#    builds: [{flavor, machine, hardened, build_id, s3_prefix}]})
#
# Environment: TOKEN, TOKEN_KIND (app|github), GH_TOKEN, GITHUB_REPOSITORY
# (owner/name: gh --repo and the API path of the tag), DRY_RUN, GIT_BOT_NAME,
# GIT_BOT_EMAIL as in merge-up.sh (merge-children hands them to it);
# DS_ARTIFACTS_BUCKET (lib.sh); GITHUB_REF_NAME, GITHUB_SHA, GITHUB_OUTPUT.
# With TOKEN_KIND=github a refused tag creation or push ends with the
# commands a human with bypass rights runs instead (lib.sh no_app_hint).
# Exit codes: 0 ok (or dry run), 1 refused or failed, 2 usage.

SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
REPO=$(git rev-parse --show-toplevel)
cd "$REPO"

. "$SCRIPTS/lib.sh"

MODE=""
LINE="${GITHUB_REF_NAME:-}"
SHA=""
BUMP=auto
MERGE_CHILDREN=false
DRY_RUN="${DRY_RUN:-true}"
MANIFEST=""
OUTPUT="${GITHUB_OUTPUT:-}"
TOKEN_KIND="${TOKEN_KIND:-github}"
RC_WORKFLOW="release-rc-build.yml"
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$GIT_BOT_NAME}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$GIT_BOT_EMAIL}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
export GIT_TERMINAL_PROMPT=0

usage() {
  sed -n '3,65p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --publish) MODE=publish; shift ;;
    --line) LINE="$2"; shift 2 ;;
    --sha) SHA="$2"; shift 2 ;;
    --bump) BUMP="$2"; shift 2 ;;
    --merge-children) MERGE_CHILDREN=true; shift ;;
    --manifest) MANIFEST="$2"; shift 2 ;;
    --github-output) OUTPUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::promote: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::promote: $*"
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

[ -n "$MODE" ] || die "one of --check or --publish is required"
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
case "$TOKEN_KIND" in app|github) ;; *) die "TOKEN_KIND must be app or github, got '$TOKEN_KIND'" ;; esac
case "$BUMP" in auto|major|minor|patch) ;; *) die "--bump must be auto, major, minor or patch, got '$BUMP'" ;; esac

# out <key> <value>: an output line, to the file when there is one.
out() {
  echo "output: $1=$2"
  [ -z "$OUTPUT" ] || printf '%s=%s\n' "$1" "$2" >> "$OUTPUT"
}

jsonget() { python3 -I -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(json.dumps(v) if isinstance(v,(dict,list)) else v)' "$1" "$2"; }

# remote_tag_sha <tag>: the commit a tag on origin points at (peeled), empty
# when the tag does not exist.
remote_tag_sha() {
  local listing peeled plain
  listing=$(git ls-remote --tags origin "refs/tags/$1" "refs/tags/$1^{}")
  peeled=$(awk -v t="refs/tags/$1^{}" '$2 == t { print $1 }' <<<"$listing")
  plain=$(awk -v t="refs/tags/$1" '$2 == t { print $1 }' <<<"$listing")
  printf '%s\n' "${peeled:-$plain}"
}

# Branch refs only: every tag question goes to origin (remote_tag_sha), so a
# local tag can never shadow what the repo has.
git fetch --quiet --no-tags origin

# =============================================================================
# --check
# =============================================================================
if [ "$MODE" = check ]; then
  [ -n "$LINE" ] || die "--line (or GITHUB_REF_NAME) is required"
  PARSED=$(python3 "$SCRIPTS/lines.py" --root "$REPO" parse "$LINE")
  KIND=$(jsonget "$PARSED" kind)
  case "$KIND" in
    main|minor|patch) ;;
    *) die "$LINE is not a release line of a configured product (release/config.json)" ;;
  esac
  PRODUCT=$(jsonget "$PARSED" product)
  TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$LINE^{commit}") || die "origin has no branch $LINE"
  if [ -n "$SHA" ] && [ "$SHA" != "$TIP" ]; then
    die "this run was dispatched at $SHA but $LINE is now at $TIP; re-run promote on the current tip"
  fi

  # ---- 2. green tip -----------------------------------------------------------
  CANDIDATES=$(python3 "$SCRIPTS/release_git.py" walk-versions-only "$TIP" --git-dir "$REPO" --max 20)
  RC_SHA=""
  RC_RUN=""
  for sha in $CANDIDATES; do
    if RUN=$(gh run list "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --workflow "$RC_WORKFLOW" --commit "$sha" --branch "$LINE" \
               --status success --json databaseId,number --limit 10 | python3 "$SCRIPTS/release_git.py" first-run); then
      RC_SHA="$sha"
      RC_RUN="$RUN"
      break
    fi
  done
  [ -n "$RC_SHA" ] || die "the tip of $LINE is not green: no successful $RC_WORKFLOW run on $LINE for $(echo "$CANDIDATES" | paste -sd ' ') (the tip and the versions/-only commits before it)"
  RC_SHA7="${RC_SHA:0:7}"
  RC_TAG=$(rc_tag "$PRODUCT" "$RC_SHA")
  state=$(gh_release_state "$RC_TAG") \
    || die "no pre-release $RC_TAG for the green sha $RC_SHA7 (publish-rc did not run or failed; rerun $RC_WORKFLOW on $LINE)"
  [ "$state" = published ] || die "$RC_TAG is still a draft (publish-rc did not finish; rerun $RC_WORKFLOW on $LINE)"
  is_pre=$(gh release view "$RC_TAG" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --json isPrerelease --jq .isPrerelease 2>/dev/null || echo '?')
  [ "$is_pre" = true ] || die "$RC_TAG is not a pre-release (isPrerelease=$is_pre); an RC must be one"
  RC_TAG_SHA=$(remote_tag_sha "$RC_TAG")
  [ "$RC_TAG_SHA" = "$RC_SHA" ] || die "tag $RC_TAG points at '${RC_TAG_SHA:-nothing}', not the green sha $RC_SHA"
  mkdir -p "$TMP/rc"
  gh release download "$RC_TAG" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --pattern release-manifest.json --dir "$TMP/rc" --clobber >/dev/null \
    || die "could not download the release-manifest.json asset of $RC_TAG"
  RC_MANIFEST="$TMP/rc/release-manifest.json"
  [ "$(json_field "$RC_MANIFEST" kind)" = rc ] || die "$RC_TAG's release-manifest.json is not an rc manifest"
  [ "$(json_field "$RC_MANIFEST" sha)" = "$RC_SHA" ] || die "$RC_TAG's release-manifest.json is for sha $(json_field "$RC_MANIFEST" sha), not $RC_SHA"
  [ "$(json_field "$RC_MANIFEST" product)" = "$PRODUCT" ] || die "$RC_TAG's release-manifest.json is for product $(json_field "$RC_MANIFEST" product), not $PRODUCT"
  RC_RUN_ID=$(json_field "$RC_MANIFEST" run_id)
  RC_RUN_URL=$(json_field "$RC_MANIFEST" run_url)
  RC_VERSION=$(json_field "$RC_MANIFEST" version)
  RC_PENDING=$(json_field "$RC_MANIFEST" pending)

  # ---- 3. children merged --------------------------------------------------------
  HEADS="$TMP/heads"
  git ls-remote --heads origin > "$HEADS"
  mapfile -t CHILDREN < <(python3 "$SCRIPTS/lines.py" --root "$REPO" --heads-file "$HEADS" children "$LINE" \
    | python3 -I -c 'import json,sys; print("\n".join(json.load(sys.stdin)))')
  UNMERGED_CHILDREN=()
  UNMERGED_REPORT="$TMP/unmerged"
  : > "$UNMERGED_REPORT"
  for child in "${CHILDREN[@]}"; do
    [ -n "$child" ] || continue
    if missing=$(python3 "$SCRIPTS/release_git.py" unmerged "refs/remotes/origin/$LINE" "refs/remotes/origin/$child" --git-dir "$REPO" --root "$REPO"); then
      echo "child $child: merged (nothing but bookkeeping in $LINE..$child)" >> "$UNMERGED_REPORT"
    else
      UNMERGED_CHILDREN+=("$child")
      {
        echo "child $child: $(echo "$missing" | wc -l) commit(s) not in $LINE:"
        echo "$missing" | awk '{ printf "    %s %s\n", substr($1, 1, 7), substr($0, index($0, " ") + 1) }'
      } >> "$UNMERGED_REPORT"
    fi
  done

  # ---- 4. version and tag -------------------------------------------------------
  PENDING=$(git show "$RC_SHA:versions/$PRODUCT" 2>/dev/null | head -1) || true
  [ -n "$PENDING" ] || die "versions/$PRODUCT is missing at $RC_SHA7"
  [ "$RC_PENDING" = "$PENDING" ] || die "$RC_TAG's release-manifest.json says pending $RC_PENDING, versions/$PRODUCT at $RC_SHA7 says $PENDING"
  PLAN=$(python3 "$SCRIPTS/release_git.py" promote-plan --root "$REPO" "$LINE" "$PENDING" --bump "$BUMP" 2>"$TMP/plan.err") \
    || die "$(cat "$TMP/plan.err")"
  TAG=$(jsonget "$PLAN" tag)
  LEVEL=$(jsonget "$PLAN" level)
  NEXT=$(jsonget "$PLAN" next_version)
  EXISTING=$(remote_tag_sha "$TAG")
  if [ -n "$EXISTING" ]; then
    if [ "$EXISTING" = "$RC_SHA" ] && [ "$RC_SHA" != "$TIP" ]; then
      die "nothing new to promote on $LINE since $TAG (the tip ${TIP:0:7} only touches versions/; $TAG is at $RC_SHA7)"
    fi
    if [ "$EXISTING" = "$RC_SHA" ]; then
      die "tag $TAG already exists at $RC_SHA7: this sha was already promoted (its bump commit may be missing from $LINE; versions/$PRODUCT must move past $PENDING first)"
    fi
    die "tag $TAG already exists at ${EXISTING:0:7}; versions/$PRODUCT on $LINE ($PENDING) is a version that was already released"
  fi
  # The release build runs at github.sha, the tip (the build workflow checks
  # it out), so a tag at an older green sha would not be the sha that was built.
  if [ "$RC_SHA" != "$TIP" ]; then
    die "the tip ${TIP:0:7} of $LINE only touches versions/ and has no successful $RC_WORKFLOW run (the last green sha is $RC_SHA7); the release build would run at the tip, so re-run $RC_WORKFLOW on $LINE and promote when the tip itself is green"
  fi
  FLAVORS=$(python3 "$SCRIPTS/version.py" --root "$REPO" matrix "$PRODUCT") || die "release/matrix.json has no flavors for $PRODUCT"
  MATRIX=$(python3 -I -c 'import json,sys; print(json.dumps({"include": [{"flavor": f} for f in json.loads(sys.argv[1])]}, separators=(",", ":")))' "$FLAVORS")
  TITLE="$(product_label "$PRODUCT")v$PENDING (staging)"

  # ---- the plan ------------------------------------------------------------------
  echo "== promote plan (dry_run=$DRY_RUN, token=$TOKEN_KIND, bump=$BUMP)"
  echo "line:               $LINE ($KIND line of $PRODUCT) at $TIP"
  echo "green sha:          $RC_SHA (run ${RC_RUN#* }, id ${RC_RUN%% *})"
  echo "rc:                 $RC_TAG = $RC_VERSION, run $RC_RUN_ID ($RC_RUN_URL)"
  echo "children:           ${CHILDREN[*]:-(none)}"
  sed 's/^/                    /' "$UNMERGED_REPORT"
  echo "version:            $PENDING -> tag $TAG at $RC_SHA7"
  echo "release:            \"$TITLE\", pre-release, assets release-manifest.json + every artifact of every build + per-flavor manifest.json and SHA256SUMS; index s3://$DS_ARTIFACTS_BUCKET/release/$PRODUCT/v$PENDING/release-manifest.json"
  echo "build matrix:       $(echo "$FLAVORS" | python3 -I -c 'import json,sys; print(" ".join(json.load(sys.stdin)))') with build_version=$PENDING release_line=$LINE"
  echo "bump:               $LEVEL: versions/$PRODUCT $PENDING -> $NEXT, commit \"release: $(product_label "$PRODUCT")v$PENDING promoted; pending $NEXT [skip ci]\" on $LINE"

  if [ ${#UNMERGED_CHILDREN[@]} -gt 0 ]; then
    if [ "$MERGE_CHILDREN" != true ]; then
      die "child line(s) not merged into $LINE: ${UNMERGED_CHILDREN[*]} (listed above); run merge-up for each, or promote with merge_children=true"
    fi
    echo "== merge-children: ${#UNMERGED_CHILDREN[@]} child line(s) to merge into $LINE, in version order"
    for child in "${UNMERGED_CHILDREN[@]}"; do
      echo "-- merge-up.sh --child $child --parent $LINE"
      if [ "$DRY_RUN" = true ]; then
        "$SCRIPTS/merge-up.sh" --child "$child" --parent "$LINE" --dry-run || die "merge-up dry run of $child failed (see above)"
      else
        "$SCRIPTS/merge-up.sh" --child "$child" --parent "$LINE" --no-dry-run || die "merge-up of $child into $LINE failed (see above); the remaining children were not merged"
      fi
    done
    if [ "$DRY_RUN" = true ]; then
      echo "dry run: would merge ${#UNMERGED_CHILDREN[@]} child line(s) and stop; nothing merged, nothing created"
      out proceed false
      exit 0
    fi
    echo "::error::promote: merged ${#UNMERGED_CHILDREN[@]} child line(s) into $LINE (${UNMERGED_CHILDREN[*]}); the new tip needs its own Action A run ($RC_WORKFLOW), re-run promote when it is green"
    exit 1
  fi

  out product "$PRODUCT"
  out line "$LINE"
  out kind "$KIND"
  out sha "$RC_SHA"
  out version "$PENDING"
  out pending "$PENDING"
  out tag "$TAG"
  out rc_tag "$RC_TAG"
  out rc_run_id "$RC_RUN_ID"
  out rc_run_url "$RC_RUN_URL"
  out bump "$LEVEL"
  out next_version "$NEXT"
  out matrix "$MATRIX"
  if [ "$DRY_RUN" = true ]; then
    echo "dry run: nothing built, tagged, released or pushed"
    out proceed false
    exit 0
  fi
  out proceed true
  echo "check passed: building $PRODUCT $PENDING at $RC_SHA7"
  exit 0
fi

# =============================================================================
# --publish
# =============================================================================
[ -n "$MANIFEST" ] || die "--publish needs --manifest <release-manifest.json>"
[ -f "$MANIFEST" ] || die "$MANIFEST does not exist"
case "$BUMP" in major|minor|patch) ;; *) die "--publish needs the resolved bump level (--bump major|minor|patch, the check job's output)" ;; esac
require_gh_token
[ -n "${GITHUB_REPOSITORY:-}" ] || die "GITHUB_REPOSITORY is not set (owner/name of this repo: the tag is created through its API)"
[ "$(json_field "$MANIFEST" kind)" = release ] || die "$MANIFEST is not a release manifest (kind != release); the release gate writes it with rc-gate.sh --kind release"
PRODUCT=$(json_field "$MANIFEST" product)
VERSION=$(json_field "$MANIFEST" version)
SHA=$(json_field "$MANIFEST" sha)
LINE=$(json_field "$MANIFEST" line)
RC_RUN_URL=$(json_field "$MANIFEST" rc_run.run_url)
RELEASE_RUN_URL=$(json_field "$MANIFEST" release_run.run_url)
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "manifest sha '$SHA' is not a full commit"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "manifest version '$VERSION' is not X.Y.Z"
SHA7="${SHA:0:7}"
TAG=$(product_tag "$PRODUCT" "$VERSION")
TITLE="$(product_label "$PRODUCT")v$VERSION (staging)"
NEXT=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import version; print(version.bump(sys.argv[2], sys.argv[3]))' "$SCRIPTS" "$VERSION" "$BUMP")
BUMP_SUBJECT="release: $(product_label "$PRODUCT")v$VERSION promoted; pending $NEXT [skip ci]"
RELEASE_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/releases/tag/${TAG}"

echo "== promote publish (token=$TOKEN_KIND)"
echo "release:            $TAG at $SHA7 (\"$TITLE\") from $LINE"
echo "runs:               rc $RC_RUN_URL, release $RELEASE_RUN_URL"
echo "bump:               versions/$PRODUCT -> $NEXT ($BUMP) on $LINE"
if [ "$DRY_RUN" = true ]; then
  echo "dry run: publish does nothing"
  exit 0
fi

# ---- the tag ----------------------------------------------------------------------
EXISTING=$(remote_tag_sha "$TAG")
if [ -n "$EXISTING" ]; then
  [ "$EXISTING" = "$SHA" ] || die "tag $TAG already exists at ${EXISTING:0:7}, not at $SHA7; refusing to touch it"
  note "promote: tag $TAG already points at $SHA7 (an earlier publish got this far); continuing"
else
  echo "promote: creating tag $TAG at $SHA7"
  if ! gh api -X POST "repos/${GITHUB_REPOSITORY}/git/refs" -f "ref=refs/tags/$TAG" -f "sha=$SHA" > "$TMP/tag.json" 2>"$TMP/tag.err"; then
    cat "$TMP/tag.err"
    if [ "$TOKEN_KIND" != app ]; then
      no_app_hint "creating tag $TAG was refused (the release-tags ruleset restricts tag creation to the App once it is active)" \
        "git fetch origin && git tag $TAG $SHA && git push origin refs/tags/$TAG, then re-run this promote (the tag check is idempotent)"
    fi
    die "creating tag $TAG failed"
  fi
fi

# ---- the release --------------------------------------------------------------------
resume=0
skip_release=0
if state=$(gh_release_state "$TAG"); then
  if [ "$state" = published ]; then
    note "promote: release $TAG is already published; not touching it"
    skip_release=1
  else
    note "promote: release $TAG exists as a draft (an earlier publish did not finish); resuming"
    resume=1
  fi
fi

if [ "$skip_release" = 0 ]; then
  # The assets, the same set publish-rc.sh attaches: release-manifest.json,
  # every artifact (deb or extra file) under its own name, and each build's
  # manifest.json and SHA256SUMS as <flavor>.manifest.json and <flavor>.SHA256SUMS. Asset
  # names are flat on a release, so a name used twice is refused.
  mkdir -p "$TMP/assets"
  ASSETS=("$TMP/assets/release-manifest.json")
  cp "$MANIFEST" "$TMP/assets/release-manifest.json"
  while IFS=$'\t' read -r flavor prefix key name; do
    [ ! -e "$TMP/assets/$name" ] || die "asset name $name is used twice (${prefix}/${key} of $flavor); every attached file needs a distinct name"
    s3_get "${prefix}/${key}" "$TMP/assets/$name" || die "could not download ${prefix}/${key}"
    ASSETS+=("$TMP/assets/$name")
  done < <(python3 -I - "$MANIFEST" <<'EOF'
import json, os, sys
with open(sys.argv[1]) as f:
    for b in json.load(f)['builds']:
        files = b.get('files') or {}
        print(f"{b['flavor']}\t{b['s3_prefix']}\tmanifest.json\t{b['flavor']}.manifest.json")
        print(f"{b['flavor']}\t{b['s3_prefix']}\tSHA256SUMS\t{b['flavor']}.SHA256SUMS")
        for a in (files.get('debs') or []) + (files.get('extra') or []):
            print(f"{b['flavor']}\t{b['s3_prefix']}\t{a['key']}\t{os.path.basename(a['key'])}")
EOF
  )

  python3 -I - "$MANIFEST" "$DS_ARTIFACTS_BUCKET" "$PRODUCT_PREFIX" > "$TMP/notes.md" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
bucket = sys.argv[2]
label = f" of **{m['product']}**" if sys.argv[3] == 'true' else ''
rc = m.get('rc_run') or {}
rel = m.get('release_run') or {}
print(f"Release `{m['version']}`{label}, promoted from `{m['line']}` at `{m['sha']}`. "
      "Staging pre-release: it becomes the release when Ship flips it.")
print()
print(f"- Line: `{m['line']}`")
print(f"- RC: `{rc.get('tag', '?')}`, run {rc.get('run_url', '?')}")
print(f"- Release build: run {rel.get('run_url', m.get('run_url', '?'))} (the same sha rebuilt with `build_version = {m['version']}`)")
print(f"- Release manifest: `release-manifest.json` (asset) and `s3://{bucket}/release/{m['product']}/v{m['version']}/release-manifest.json`")
print()
print("| Flavor | Build id | S3 prefix | Artifacts | SDK |")
print("|---|---|---|---|---|")
for b in m['builds']:
    files = b.get('files') or {}
    sdk = b.get('sdk') or {}
    print(f"| `{b['flavor']}` | `{b['build_id']}` | `s3://{bucket}/{b['s3_prefix']}/` | "
          f"{len(files.get('debs') or []) + len(files.get('extra') or [])} | "
          f"{'`' + sdk['os_release'] + '`' if sdk.get('os_release') else 'none recorded'} |")
print()
# The toolchain each build was compiled with: the OS release it came from and the installer.
for b in m['builds']:
    sdk = b.get('sdk') or {}
    if sdk.get('uri'):
        print(f"- SDK of `{b['flavor']}`: OS release `{sdk.get('os_release') or '?'}`, installer `{sdk['uri']}` (sha256 `{sdk.get('sha256', '?')}`), the same the release candidate was gated with")
if any((b.get('sdk') or {}).get('uri') for b in m['builds']):
    print()
print("The assets here are every artifact of each build (debs, install trees and other files) plus its `manifest.json` and `SHA256SUMS`; the builds stay in S3.")
EOF

  if [ "$resume" = 0 ]; then
    echo "promote: creating draft release $TAG (\"$TITLE\")"
    gh release create "$TAG" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --draft --prerelease --target "$SHA" --title "$TITLE" --notes-file "$TMP/notes.md" \
      || die "gh release create $TAG failed"
  fi
  echo "promote: uploading ${#ASSETS[@]} asset(s) to $TAG"
  gh release upload "$TAG" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --clobber "${ASSETS[@]}" || die "gh release upload $TAG failed"
  echo "promote: publishing $TAG as a pre-release"
  gh release edit "$TAG" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --draft=false --prerelease || die "gh release edit $TAG --draft=false failed"
fi

s3_put "$MANIFEST" "release/$PRODUCT/v$VERSION/release-manifest.json" \
  || die "copying release-manifest.json to s3://$DS_ARTIFACTS_BUCKET/release/$PRODUCT/v$VERSION/ failed"
note "promote: $TAG published ($TITLE): $RELEASE_URL"

# ---- the bump ---------------------------------------------------------------------------
attempt=0
while :; do
  attempt=$((attempt + 1))
  git fetch --quiet origin
  LINE_TIP=$(git rev-parse --verify --quiet "refs/remotes/origin/$LINE^{commit}") || die "origin has no branch $LINE"
  if [ -d "$WT" ]; then
    git worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  git worktree add --quiet --detach "$WT" "$LINE_TIP"
  CURRENT=$(head -1 "$WT/versions/$PRODUCT" 2>/dev/null || true)
  if [ "$CURRENT" = "$NEXT" ]; then
    note "promote: versions/$PRODUCT on $LINE is already $NEXT (bumped earlier); nothing to push"
    NEW_TIP="$LINE_TIP"
    break
  fi
  [ "$CURRENT" = "$VERSION" ] || die "versions/$PRODUCT on $LINE (${LINE_TIP:0:7}) is '$CURRENT', neither the promoted $VERSION nor the next $NEXT; not touching it (someone changed the pending version meanwhile)"
  printf '%s\n' "$NEXT" > "$WT/versions/$PRODUCT"
  git -C "$WT" add -- "versions/$PRODUCT"
  [ "$(git -C "$WT" diff --cached --name-only)" = "versions/$PRODUCT" ] || die "the bump commit would touch more than versions/$PRODUCT"
  git -C "$WT" commit --quiet -m "$BUMP_SUBJECT"
  if git -C "$WT" push --quiet origin "HEAD:refs/heads/$LINE" 2>"$TMP/push.log"; then
    NEW_TIP=$(git -C "$WT" rev-parse HEAD)
    echo "promote: pushed $LINE at $NEW_TIP (\"$BUMP_SUBJECT\")"
    break
  fi
  cat "$TMP/push.log"
  if [ "$attempt" -ge 3 ]; then
    if [ "$TOKEN_KIND" != app ]; then
      no_app_hint "pushing the bump commit to $LINE was refused" \
        "git fetch origin && git checkout $LINE && echo $NEXT > versions/$PRODUCT && git add versions/$PRODUCT && git commit -m '$BUMP_SUBJECT' && git push origin $LINE"
    fi
    die "push to $LINE failed 3 times; the release $TAG is published, only the versions/$PRODUCT bump to $NEXT is missing"
  fi
  echo "promote: push to $LINE rejected (attempt $attempt), refetching"
done

# ---- the staging request -------------------------------------------------------------------
REQUEST=$(python3 -I - "$MANIFEST" "$TAG" <<'EOF'
import json, sys
with open(sys.argv[1]) as f:
    m = json.load(f)
doc = {'product': m['product'], 'version': m['version'], 'sha': m['sha'], 'line': m['line'], 'tag': sys.argv[2],
       'rc_run': m.get('rc_run'), 'release_run': m.get('release_run'),
       'builds': [{k: b[k] for k in ('flavor', 'machine', 'hardened', 'build_id', 's3_prefix')} for b in m['builds']]}
print(json.dumps(doc, separators=(',', ':')))
EOF
)
out tag "$TAG"
out release_url "$RELEASE_URL"
out next_version "$NEXT"
out line_tip "$NEW_TIP"
out staging_request "$REQUEST"
echo "done: $(product_label "$PRODUCT")v$VERSION promoted from $LINE at $SHA7; pending on $LINE is now $NEXT"
