#!/usr/bin/env bash
set -euo pipefail

# Delete the RC pre-releases of one product and the builds behind them
# (ENG-1935, ENG-2110). Shared by abandon.sh (the abandoned line's RCs), the
# go-live cleanup (--below 1.0.0 on main) and the later retention policy.
#
#   prune-rc.sh [--product P] (--line <line> | --below X.Y.Z | --sha <sha7|sha> ...)
#               [--dry-run|--no-dry-run] [--report FILE]
#
# --product is optional when names carry no product (product_prefix false):
# the one product configured is the product. Candidates: every RC release of
# the repo (gh api releases, paginated, drafts included) whose tag is
# rc/<sha7> (prefixed: rc/<P>/<sha7>) and whose release-manifest.json (the
# release asset, else the s3://<bucket>/rc/<P>/<sha7>/ index copy) says
# product == P and
#   --line   line == <line>
#   --below  the X.Y.Z part of version is lower than X.Y.Z
#   --sha    sha starts with one of the given shas
# A candidate is KEPT when its sha carries any release tag (v*, prefixed
# <P>/v*: git ls-remote --tags origin) or when a
# release/<P>/v*/release-manifest.json in S3 lists one of its build
# prefixes. Everything else is deleted, per candidate in this order: each
# per-build S3 prefix the manifest lists (aws s3 rm --recursive), the
# rc/<P>/<sha7>/ index, then the GitHub release together with its tag (gh
# release delete --cleanup-tag). A failed prefix deletion leaves that
# candidate's index and release in place (the pointer to what is left) and
# the script moves on; the exit code is 1 when anything failed. A per-build
# prefix must look like <machine>/<UTC>-<run_id>-<standard|hardened> or the
# candidate is skipped, so a damaged manifest cannot widen a delete.
#
# The keep and delete lists are printed with sizes (aws s3 ls --summarize),
# appended to GITHUB_STEP_SUMMARY when set, and written to the JSON report
# (default prune-rc-report.json in the current directory). A dry run (the
# default) does all of that and deletes nothing.
#
# Environment: GH_TOKEN; GITHUB_REPOSITORY (owner/name for gh, required);
# DS_ARTIFACTS_BUCKET (lib.sh, default ds-yocto-artifacts); DRY_RUN
# (true|false, overridden by the flags). Run it from a checkout whose origin
# is the repo: the tag check is git ls-remote there.
# Exit: 0 (nothing failed, or dry run), 1 (a deletion failed, or a bad
# argument), 2 (usage).

. "$(dirname "$(readlink -f "$0")")/lib.sh"
SCRIPTS=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
# The checkout whose release/config.json names the product (the tests point
# it at a fixture; default: the checkout this script lives in).
RELEASE_ROOT=${RELEASE_ROOT:-$(dirname "${SCRIPTS}")}

PRODUCT=""
LINE=""
BELOW=""
SHAS=()
DRY_RUN="${DRY_RUN:-true}"
REPORT="prune-rc-report.json"

usage() {
  sed -n '3,42p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --product) PRODUCT="$2"; shift 2 ;;
    --line) LINE="$2"; shift 2 ;;
    --below) BELOW="$2"; shift 2 ;;
    --sha) SHAS+=("$2"); shift 2 ;;
    --report) REPORT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --no-dry-run) DRY_RUN=false; shift ;;
    -h|--help) usage ;;
    *) echo "::error::prune-rc: unknown argument $1"; usage ;;
  esac
done

die() {
  echo "::error::prune-rc: $*"
  exit 1
}

if [ -z "$PRODUCT" ] && [ "$PRODUCT_PREFIX" = false ]; then
  # Names carry no product, so the one product configured is the product.
  PRODUCT=$(python3 "$SCRIPTS/version.py" --root "$RELEASE_ROOT" products | head -1)
fi
[ -n "$PRODUCT" ] || die "--product is required"
[[ "$PRODUCT" =~ ^[a-z][a-z0-9]*$ ]] || die "product '$PRODUCT' is not a product name"
python3 "$SCRIPTS/version.py" --root "$RELEASE_ROOT" products | grep -qx -- "$PRODUCT" \
  || die "product '$PRODUCT' is not in release/config.json ($(python3 "$SCRIPTS/version.py" --root "$RELEASE_ROOT" products | paste -sd ' '))"
selectors=0
[ -z "$LINE" ] || selectors=$((selectors + 1))
[ -z "$BELOW" ] || selectors=$((selectors + 1))
[ ${#SHAS[@]} -eq 0 ] || selectors=$((selectors + 1))
[ "$selectors" -eq 1 ] || die "exactly one of --line, --below or --sha (repeatable) is required"
if [ -n "$LINE" ]; then
  # The line's product comes from lines.py parse, never from the name.
  LINE_PRODUCT=$(python3 "$SCRIPTS/lines.py" --root "$RELEASE_ROOT" parse "$LINE" \
    | python3 -I -c 'import json,sys; d=json.load(sys.stdin); print(d["product"] if d["kind"] != "none" else "")')
  [ "$LINE_PRODUCT" = "$PRODUCT" ] || die "--line $LINE is not a line of product $PRODUCT"
  SELECTOR="line $LINE"
elif [ -n "$BELOW" ]; then
  [[ "$BELOW" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || die "--below '$BELOW' is not X.Y.Z"
  SELECTOR="versions below $BELOW"
else
  for s in "${SHAS[@]}"; do
    [[ "$s" =~ ^[0-9a-f]{7,40}$ ]] || die "--sha '$s' is not a 7 to 40 hex sha"
  done
  SELECTOR="sha ${SHAS[*]}"
fi
case "$DRY_RUN" in true|false) ;; *) die "DRY_RUN must be true or false, got '$DRY_RUN'" ;; esac
require_gh_token
[ -n "${GITHUB_REPOSITORY:-}" ] || die "GITHUB_REPOSITORY is not set (owner/name of this repo, for gh api)"
git rev-parse --show-toplevel >/dev/null 2>&1 || die "run from a checkout of the repo (the tag check is git ls-remote --tags origin)"

# The tag shapes of this naming mode: RC tags are <RC_TAG_PREFIX><sha7> and
# release tags match RELEASE_TAG_GLOB (rc/<sha7> and v*, or rc/<P>/<sha7>
# and <P>/v* when names carry the product).
RC_TAG_PREFIX=$(rc_tag "$PRODUCT" "0000000")
RC_TAG_PREFIX="${RC_TAG_PREFIX%0000000}"
RELEASE_TAG_GLOB=$(product_tag "$PRODUCT" '*')

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID:-local}"

# ---- S3 helpers (same shapes as lib.sh; the test stub implements them) ------

# s3_prefix_size <prefix>: total bytes under s3://<bucket>/<prefix>/ ("0" when
# the prefix is empty or missing; aws exits 1 on an empty listing).
s3_prefix_size() {
  local out
  out=$(aws s3 ls --recursive --summarize "s3://${DS_ARTIFACTS_BUCKET}/$1/" 2>/dev/null || true)
  awk '/Total Size:/ { print $3; found = 1 } END { if (!found) print 0 }' <<<"$out"
}

# s3_rm_prefix <prefix>: delete everything under s3://<bucket>/<prefix>/.
s3_rm_prefix() { aws s3 rm --recursive --only-show-errors "s3://${DS_ARTIFACTS_BUCKET}/$1/"; }

# ---- 1. the rc releases of the product --------------------------------------

gh api "repos/${GITHUB_REPOSITORY}/releases?per_page=100" --paginate > "$WORK/releases.json" \
  || die "gh api repos/${GITHUB_REPOSITORY}/releases failed"
python3 -I - "$WORK/releases.json" "$RC_TAG_PREFIX" > "$WORK/rc-releases.tsv" <<'EOF'
import json, sys
path, rc_prefix = sys.argv[1:3]
text = open(path).read()
dec = json.JSONDecoder()
pos, pages = 0, []
while True:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    if pos >= len(text):
        break
    page, pos = dec.raw_decode(text, pos)
    pages.append(page)
for page in pages:
    for r in page if isinstance(page, list) else []:
        tag = r.get('tag_name') or ''
        if tag.startswith(rc_prefix):
            assets = [a.get('name') for a in r.get('assets') or []]
            print(f"{tag}\t{'draft' if r.get('draft') else 'published'}\t{'yes' if 'release-manifest.json' in assets else 'no'}")
EOF
RC_COUNT=$(wc -l < "$WORK/rc-releases.tsv")

# ---- 2. what protects a sha: release tags and release manifests ---------------

git ls-remote --tags origin "refs/tags/${RELEASE_TAG_GLOB}" > "$WORK/tags.tsv" \
  || die "git ls-remote --tags origin failed"

mkdir -p "$WORK/release-manifests"
if listing=$(aws s3 ls "s3://${DS_ARTIFACTS_BUCKET}/release/${PRODUCT}/" 2>/dev/null); then
  :
else
  [ -z "${listing:-}" ] || die "aws s3 ls s3://${DS_ARTIFACTS_BUCKET}/release/${PRODUCT}/ failed"
  listing=""
fi
while read -r ver; do
  [ -n "$ver" ] || continue
  s3_get "release/${PRODUCT}/${ver}/release-manifest.json" "$WORK/release-manifests/${ver}.json" 2>/dev/null \
    || warn "prune-rc: s3://${DS_ARTIFACTS_BUCKET}/release/${PRODUCT}/${ver}/ has no release-manifest.json; nothing under it protects a build"
done < <(awk '$1 == "PRE" { sub(/\/$/, "", $2); print $2 }' <<<"$listing")

# ---- 3. each candidate's release-manifest.json --------------------------------

mkdir -p "$WORK/manifests"
: > "$WORK/skipped.tsv"
while IFS=$'\t' read -r tag state has_asset; do
  [ -n "$tag" ] || continue
  sha7="${tag#"$RC_TAG_PREFIX"}"
  # The sha7 is the only part of the tag name that reaches an S3 path or a
  # comparison, so it has to be exactly 7 hex characters before it is used.
  if ! [[ "$sha7" =~ ^[0-9a-f]{7}$ ]]; then
    printf '%s\t%s\ttag name is not %s<sha7> (7 lowercase hex); not touching it\n' "$tag" "$state" "$RC_TAG_PREFIX" >> "$WORK/skipped.tsv"
    continue
  fi
  dest="$WORK/manifests/${sha7}.json"
  got=""
  if [ "$has_asset" = yes ]; then
    dl="$WORK/dl-${sha7}"
    mkdir -p "$dl"
    if gh release download "$tag" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --pattern release-manifest.json --dir "$dl" --clobber >/dev/null 2>&1 \
       && [ -f "$dl/release-manifest.json" ]; then
      mv "$dl/release-manifest.json" "$dest"
      got=asset
    fi
  fi
  if [ -z "$got" ] && s3_get "rc/${PRODUCT}/${sha7}/release-manifest.json" "$dest" 2>/dev/null; then
    got=index
  fi
  if [ -z "$got" ]; then
    printf '%s\t%s\tno release-manifest.json (neither a release asset nor the S3 index); delete it by hand if it belongs to the selection\n' "$tag" "$state" >> "$WORK/skipped.tsv"
    continue
  fi
  printf '%s\t%s\t%s\n' "$tag" "$state" "$got" >> "$WORK/have.tsv"
done < "$WORK/rc-releases.tsv"
[ -f "$WORK/have.tsv" ] || : > "$WORK/have.tsv"

# ---- 4. classify --------------------------------------------------------------

python3 -I - "$WORK" "$PRODUCT" "$RC_TAG_PREFIX" "$RELEASE_TAG_GLOB" "$LINE" "$BELOW" "${SHAS[@]+"${SHAS[@]}"}" <<'EOF'
import json, os, re, sys
work, product, rc_prefix, tag_glob, line, below = sys.argv[1:7]
shas = sys.argv[7:]
PREFIX_RE = re.compile(r'[A-Za-z0-9._-]+/[0-9]{8}T[0-9]{6}Z-[0-9]+-(standard|hardened)')
tag_shas = set()
with open(os.path.join(work, 'tags.tsv')) as f:
    for raw in f:
        parts = raw.split()
        if len(parts) == 2:
            tag_shas.add(parts[0])
protected = {}  # s3_prefix or build_id -> release version
rdir = os.path.join(work, 'release-manifests')
for name in sorted(os.listdir(rdir)):
    try:
        with open(os.path.join(rdir, name)) as f:
            doc = json.load(f)
    except ValueError:
        continue
    for b in doc.get('builds') or []:
        for key in (b.get('s3_prefix'), b.get('build_id')):
            if key:
                protected[key] = name[:-len('.json')]
below_t = tuple(int(x) for x in below.split('.')) if below else None
keep, delete, skipped = [], [], []
with open(os.path.join(work, 'skipped.tsv')) as f:
    for raw in f:
        tag, state, why = raw.rstrip('\n').split('\t', 2)
        skipped.append({'tag': tag, 'state': state, 'reason': why})
with open(os.path.join(work, 'have.tsv')) as f:
    rows = [r.rstrip('\n').split('\t') for r in f if r.strip()]
for tag, state, source in rows:
    sha7 = tag[len(rc_prefix):]
    with open(os.path.join(work, 'manifests', f'{sha7}.json')) as f:
        try:
            m = json.load(f)
        except ValueError:
            skipped.append({'tag': tag, 'state': state, 'reason': f'release-manifest.json ({source}) is not valid JSON'})
            continue
    sha = m.get('sha') or ''
    version = m.get('version') or ''
    base = version.split('-', 1)[0]
    entry = {'tag': tag, 'state': state, 'manifest_source': source, 'sha': sha, 'line': m.get('line'),
             'version': version, 'run_id': m.get('run_id'), 'builds': [], 'index_prefix': f'rc/{product}/{sha7}'}
    if m.get('product') != product:
        skipped.append(dict(entry, reason=f"manifest product is {m.get('product')!r}, not {product!r}"))
        continue
    if not re.fullmatch(r'[0-9a-f]{40}', sha) or not sha.startswith(sha7):
        skipped.append(dict(entry, reason=f'manifest sha {sha!r} does not match the tag'))
        continue
    if line:
        selected = m.get('line') == line
    elif below_t is not None:
        mb = re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', base)
        selected = bool(mb) and tuple(int(x) for x in base.split('.')) < below_t
    else:
        selected = any(sha.startswith(s) for s in shas)
    if not selected:
        continue
    bad = []
    for b in m.get('builds') or []:
        p = b.get('s3_prefix') or ''
        if not PREFIX_RE.fullmatch(p):
            bad.append(p)
        entry['builds'].append({'flavor': b.get('flavor'), 'build_id': b.get('build_id'), 's3_prefix': p})
    if bad:
        skipped.append(dict(entry, reason=f'unsafe build prefix in the manifest: {bad}; not deleting anything of it'))
        continue
    if sha in tag_shas:
        keep.append(dict(entry, reason=f'sha carries a {tag_glob} tag'))
        continue
    refs = sorted({protected[k] for b in entry['builds'] for k in (b['s3_prefix'], b['build_id']) if k in protected})
    if refs:
        keep.append(dict(entry, reason=f"build referenced by release/{product}/{', '.join(refs)}/release-manifest.json"))
        continue
    delete.append(dict(entry, reason='no tag and no release manifest references its builds'))
with open(os.path.join(work, 'plan.json'), 'w') as f:
    json.dump({'keep': keep, 'delete': delete, 'skipped': skipped}, f)
EOF

# ---- 5. sizes -----------------------------------------------------------------

: > "$WORK/sizes.tsv"
while read -r prefix; do
  [ -n "$prefix" ] || continue
  printf '%s\t%s\n' "$prefix" "$(s3_prefix_size "$prefix")" >> "$WORK/sizes.tsv"
done < <(python3 -I - "$WORK/plan.json" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
seen = set()
for e in p['keep'] + p['delete']:
    for b in e['builds']:
        seen.add(b['s3_prefix'])
    seen.add(e['index_prefix'])
print('\n'.join(sorted(seen)))
EOF
)

# ---- 6. the plan ----------------------------------------------------------------

print_plan() {
  python3 -I - "$WORK/plan.json" "$WORK/sizes.tsv" "$DS_ARTIFACTS_BUCKET" "$PRODUCT" "$SELECTOR" "$DRY_RUN" "$RC_COUNT" "$1" <<'EOF'
import json, sys
plan_path, sizes_path, bucket, product, selector, dry_run, rc_count, mode = sys.argv[1:9]
p = json.load(open(plan_path))
sizes = {}
for raw in open(sizes_path):
    if '\t' in raw:
        k, v = raw.rstrip('\n').split('\t', 1)
        sizes[k] = int(v or 0)
def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024
def total(e):
    return sum(sizes.get(b['s3_prefix'], 0) for b in e['builds']) + sizes.get(e['index_prefix'], 0)
for e in p['keep'] + p['delete']:
    e['bytes'] = total(e)
    for b in e['builds']:
        b['bytes'] = sizes.get(b['s3_prefix'], 0)
json.dump(p, open(plan_path, 'w'))
if mode == 'text':
    print(f'== prune-rc plan (product {product}, {selector}, dry_run={dry_run})')
    print(f'rc releases of {product}: {rc_count}; selected: {len(p["keep"]) + len(p["delete"])} (keep {len(p["keep"])}, delete {len(p["delete"])}); skipped: {len(p["skipped"])}')
    for title, rows in (('keep', p['keep']), ('delete', p['delete'])):
        print(f'-- {title} ({len(rows)})')
        for e in rows:
            print(f"{e['tag']}  {e['state']}  line {e['line']}  {e['version']}  {len(e['builds'])} build(s) {human(e['bytes'])}  reason: {e['reason']}")
            for b in e['builds']:
                print(f"    s3://{bucket}/{b['s3_prefix']}/  {human(b['bytes'])}  {b['flavor']}")
            print(f"    s3://{bucket}/{e['index_prefix']}/  {human(sizes.get(e['index_prefix'], 0))}  (index)")
    print(f'-- skipped ({len(p["skipped"])})')
    for e in p['skipped']:
        print(f"{e['tag']}  {e.get('state', '')}  {e['reason']}")
    print(f"total to delete: {human(sum(e['bytes'] for e in p['delete']))} in {len(p['delete'])} release(s)")
else:
    print(f'### prune-rc: {product}, {selector} (dry_run={dry_run})')
    print()
    print('| Action | RC | Line | Version | Builds | Size | Reason | Result |')
    print('|---|---|---|---|---|---|---|---|')
    for title, rows in (('keep', p['keep']), ('delete', p['delete'])):
        for e in rows:
            print(f"| {title} | `{e['tag']}` | `{e['line']}` | `{e['version']}` | {len(e['builds'])} | {human(e['bytes'])} | {e['reason']} | {e.get('result', '')} |")
    for e in p['skipped']:
        print(f"| skipped | `{e['tag']}` | | | | | {e['reason']} | |")
    print()
EOF
}
print_plan text

# ---- 7. delete ---------------------------------------------------------------------

failed=0
if [ "$DRY_RUN" = true ]; then
  echo "dry run: nothing deleted"
else
  : > "$WORK/results.tsv"
  while IFS=$'\t' read -r tag index prefixes; do
    [ -n "$tag" ] || continue
    echo "== deleting $tag"
    ok=1
    for prefix in $prefixes; do
      if s3_rm_prefix "$prefix" </dev/null; then
        echo "   removed s3://${DS_ARTIFACTS_BUCKET}/${prefix}/"
      else
        echo "::error::prune-rc: aws s3 rm --recursive s3://${DS_ARTIFACTS_BUCKET}/${prefix}/ failed; keeping the index and release of $tag so the leftovers stay findable"
        ok=0
        break
      fi
    done
    if [ "$ok" = 1 ]; then
      if s3_rm_prefix "$index" </dev/null; then
        echo "   removed s3://${DS_ARTIFACTS_BUCKET}/${index}/"
      else
        echo "::error::prune-rc: aws s3 rm --recursive s3://${DS_ARTIFACTS_BUCKET}/${index}/ failed; keeping the release $tag"
        ok=0
      fi
    fi
    if [ "$ok" = 1 ]; then
      if gh release delete "$tag" "${GH_REPO_ARGS[@]+"${GH_REPO_ARGS[@]}"}" --cleanup-tag --yes </dev/null; then
        echo "   deleted release and tag $tag"
      else
        echo "::error::prune-rc: gh release delete $tag --cleanup-tag failed"
        ok=0
      fi
    fi
    if [ "$ok" = 1 ]; then
      printf '%s\tdeleted\n' "$tag" >> "$WORK/results.tsv"
    else
      printf '%s\tfailed\n' "$tag" >> "$WORK/results.tsv"
      failed=$((failed + 1))
    fi
  done < <(python3 -I - "$WORK/plan.json" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
for e in p['delete']:
    print('\t'.join([e['tag'], e['index_prefix'], ' '.join(b['s3_prefix'] for b in e['builds'])]))
EOF
  )
  python3 -I - "$WORK/plan.json" "$WORK/results.tsv" <<'EOF'
import json, sys
p = json.load(open(sys.argv[1]))
res = dict(r.rstrip('\n').split('\t', 1) for r in open(sys.argv[2]) if '\t' in r)
for e in p['delete']:
    e['result'] = res.get(e['tag'], 'not attempted')
json.dump(p, open(sys.argv[1], 'w'))
EOF
fi

# ---- 8. report and summary ------------------------------------------------------------

python3 -I - "$WORK/plan.json" "$REPORT" "$PRODUCT" "$SELECTOR" "$DRY_RUN" "$DS_ARTIFACTS_BUCKET" "$GITHUB_REPOSITORY" "$RUN_URL" <<'EOF'
import json, sys
plan_path, out, product, selector, dry_run, bucket, repo, run_url = sys.argv[1:9]
p = json.load(open(plan_path))
doc = {'schema': 1, 'kind': 'prune-rc', 'product': product, 'selector': selector, 'dry_run': dry_run == 'true',
       'bucket': bucket, 'repo': repo, 'run_url': run_url,
       'keep': p['keep'], 'delete': p['delete'], 'skipped': p['skipped']}
with open(out, 'w') as f:
    json.dump(doc, f, indent=2)
    f.write('\n')
EOF
echo "report: $REPORT"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  print_plan markdown >> "$GITHUB_STEP_SUMMARY"
fi

if [ "$failed" -gt 0 ]; then
  die "$failed release(s) could not be fully deleted (see the errors above and $REPORT)"
fi
[ "$DRY_RUN" = true ] || echo "prune-rc: done"
