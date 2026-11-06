#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/prune-rc.sh (ENG-1935, ENG-2110). gh and aws are
# the stubs in tests/stubs-tree (releases and tags backed by a temp bare
# repo, S3 a directory tree); names carry no product (product ds,
# product_prefix false): RC tags are rc/<sha7>, release tags v*. No network.
#   bash scripts-release/tests/test_prune_rc.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; fails=$((fails + 1)); fi; }

. "${here}/stubs-tree/fixture.sh"
tree_fixture_init
MACHINE=ds-arm64
RELEASES="${STATE}/releases/${OWN_REPO//\//_}"

# Five shas on main (the line a manifest names is what prune reads; the
# commits only have to exist for the tags).
A=$(repo_commit main "a" "foo.txt=a\n")
B=$(repo_commit main "b" "foo.txt=b\n")
C=$(repo_commit main "c" "foo.txt=c\n")
D=$(repo_commit main "d" "foo.txt=d\n")
E=$(repo_commit main "e" "foo.txt=e\n")
make_release "rc/${A:0:7}" "${A}" main "0.0.1-rc.3.github-${A:0:7}" "${MACHINE}" "20261021T100000Z-1-standard"
make_release "rc/${B:0:7}" "${B}" 0.x.x "0.1.0-rc.5.github-${B:0:7}" "${MACHINE}" "20261021T110000Z-2-standard" "20261021T110100Z-2-hardened"
make_release "rc/${C:0:7}" "${C}" 0.x.x "0.1.0-rc.6.github-${C:0:7}" "${MACHINE}" "20261021T120000Z-3-standard"
make_release "rc/${D:0:7}" "${D}" main "0.0.2-rc.9.github-${D:0:7}" "${MACHINE}" "20261021T130000Z-4-standard"
# E: a release with no manifest anywhere
mkdir -p "${RELEASES}"
echo "{\"tag_name\": \"rc/${E:0:7}\", \"draft\": true, \"assets\": {}}" > "${RELEASES}/rc_${E:0:7}.json"
# A is promoted (tag), C's build is referenced by a release manifest in S3
tag_origin v0.0.1 "${A}"
mkdir -p "${BUCKET}/release/ds/v0.1.0"
cat > "${BUCKET}/release/ds/v0.1.0/release-manifest.json" <<EOF
{"schema": 1, "kind": "release", "product": "ds", "version": "0.1.0",
 "builds": [{"build_id": "20261021T120000Z-3-standard", "s3_prefix": "${MACHINE}/20261021T120000Z-3-standard"}]}
EOF
release_exists() { [ -f "${RELEASES}/rc_${1:0:7}.json" ]; }

# run_prune <args...> -> $rc, $log (run from the clone: the tag check is git ls-remote origin)
run_prune() {
  log="${tmp}/log"
  rc=0
  (cd "${REPO}" && "${scripts}/prune-rc.sh" "$@" > "${log}" 2>&1) || rc=$?
}

echo "# dry run --line: keeps the referenced build, lists the other for deletion, removes nothing"
export GITHUB_STEP_SUMMARY="${tmp}/summary.md"
: > "${GITHUB_STEP_SUMMARY}"
run_prune --product ds --line 0.x.x --dry-run --report "${tmp}/dry.json"
check "${rc}" 0 "exits 0"
contains "${log}" "^== prune-rc plan \(product ds, line 0.x.x, dry_run=true\)" "plan header"
contains "${log}" "^rc/${C:0:7}  published  line 0.x.x  0.1.0-rc.6.github-${C:0:7}  1 build\(s\) .* reason: build referenced by release/ds/v0.1.0/release-manifest.json" "C kept by the release manifest"
contains "${log}" "^rc/${B:0:7}  published  line 0.x.x  0.1.0-rc.5.github-${B:0:7}  2 build\(s\) .* reason: no tag and no release manifest" "B listed for deletion"
contains "${log}" "s3://ds-yocto-artifacts/${MACHINE}/20261021T110100Z-2-hardened/  .* ds_${MACHINE}_release" "B's prefixes listed with sizes"
contains "${log}" "s3://ds-yocto-artifacts/rc/ds/${B:0:7}/  .* \(index\)" "B's index keeps the product segment in S3"
lacks "${log}" "rc/${A:0:7}" "A (another line) is not selected"
lacks "${log}" "rc/${D:0:7}" "D (another line) is not selected"
contains "${log}" "^rc/${E:0:7}  draft  no release-manifest.json" "E is reported as skipped"
contains "${log}" "^dry run: nothing deleted" "dry run says so"
check "$(grep -c -E '(s3 rm|release delete)' "${AWS_CALLS}" "${STATE}/log" | awk -F: '{ s += $2 } END { print s }')" 0 "no rm or delete call"
check "$([ -f "${BUCKET}/${MACHINE}/20261021T110000Z-2-standard/manifest.json" ] && echo yes || echo no)" yes "B's build still in S3"
check "$(release_exists "${B}" && echo yes || echo no)" yes "B's release still exists"
check "$(python3 -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d["schema"], d["kind"], d["product"], d["dry_run"], [e["tag"][-7:] for e in d["keep"]], [e["tag"][-7:] for e in d["delete"]], [e["tag"][-7:] for e in d["skipped"]], d["delete"][0]["bytes"] > 0, [b["s3_prefix"] for b in d["delete"][0]["builds"]])
' "${tmp}/dry.json")" "1 prune-rc ds True ['${C:0:7}'] ['${B:0:7}'] ['${E:0:7}'] True ['${MACHINE}/20261021T110000Z-2-standard', '${MACHINE}/20261021T110100Z-2-hardened']" "report content"
contains "${GITHUB_STEP_SUMMARY}" "^\| delete \| \`rc/${B:0:7}\` \| \`0.x.x\` \| \`0.1.0-rc.5.github-${B:0:7}\` \| 2 \| " "step summary table"
unset GITHUB_STEP_SUMMARY
run_prune --line 0.x.x --dry-run --report "${tmp}/dry2.json"
check "${rc}" 0 "without --product the one configured product is used"
contains "${log}" "^== prune-rc plan \(product ds, line 0.x.x, dry_run=true\)" "plan header names it"

echo "# real run --line: deletes B's prefixes, then its index, then the release and tag; C untouched"
: > "${AWS_CALLS}"; : > "${STATE}/log"
run_prune --product ds --line 0.x.x --no-dry-run --report "${tmp}/real.json"
check "${rc}" 0 "exits 0"
check "$(grep -E "(s3 rm|release delete)" "${AWS_CALLS}" "${STATE}/log" | sed 's/^[^:]*://' | sed -E 's/ --only-show-errors//' | tr '\n' ';')" \
  "aws s3 rm --recursive s3://ds-yocto-artifacts/${MACHINE}/20261021T110000Z-2-standard/;aws s3 rm --recursive s3://ds-yocto-artifacts/${MACHINE}/20261021T110100Z-2-hardened/;aws s3 rm --recursive s3://ds-yocto-artifacts/rc/ds/${B:0:7}/;gh release delete rc/${B:0:7} --repo ds/repo --cleanup-tag --yes;" \
  "deletion order: prefixes, index, release+tag (and nothing else)"
check "$([ -d "${BUCKET}/${MACHINE}/20261021T110000Z-2-standard" ] && echo yes || echo no)" no "B's standard prefix gone"
check "$([ -d "${BUCKET}/${MACHINE}/20261021T110100Z-2-hardened" ] && echo yes || echo no)" no "B's hardened prefix gone"
check "$([ -d "${BUCKET}/rc/ds/${B:0:7}" ] && echo yes || echo no)" no "B's index gone"
check "$(release_exists "${B}" && echo yes || echo no)" no "B's release gone"
check "$(tags_of "${BARE}" | tr '\n' ' ')" "$(printf '%s\n' "rc/${A:0:7}" "rc/${C:0:7}" "rc/${D:0:7}" v0.0.1 | LC_ALL=C sort | tr '\n' ' ')" "only B's rc tag was removed"
check "$([ -f "${BUCKET}/${MACHINE}/20261021T120000Z-3-standard/manifest.json" ] && echo yes || echo no)" yes "C's build kept"
check "$(release_exists "${C}" && echo yes || echo no)" yes "C's release kept"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["dry_run"], d["delete"][0]["result"], d["repo"])' "${tmp}/real.json")" "False deleted ds/repo" "report records the result and the repo"
contains "${log}" "^prune-rc: done" "done line"

echo "# --below: the promoted sha is kept by its tag; a failed prefix rm keeps that release and exits 1"
: > "${AWS_CALLS}"; : > "${STATE}/log"
GH_STUB_PAGES=2 FAKE_AWS_FAIL_RM="20261021T130000Z-4-standard" run_prune --product ds --below 0.1.0 --no-dry-run --report "${tmp}/below.json"
check "${rc}" 1 "exits 1"
contains "${log}" "^rc/${A:0:7}  published  line main  0.0.1-rc.3.github-${A:0:7}  1 build\(s\) .* reason: sha carries a v\* tag" "A kept by tag (read across two pages)"
contains "${log}" "^rc/${D:0:7}  published  line main  0.0.2-rc.9.github-${D:0:7}  1 build\(s\) .* reason: no tag" "D selected (0.0.2 < 0.1.0)"
lacks "${log}" "^rc/${C:0:7}" "C not selected (0.1.0 is not below 0.1.0)"
contains "${log}" "::error::prune-rc: aws s3 rm --recursive s3://ds-yocto-artifacts/${MACHINE}/20261021T130000Z-4-standard/ failed; keeping the index and release of rc/${D:0:7}" "failure names the prefix and what is kept"
contains "${log}" "::error::prune-rc: 1 release\(s\) could not be fully deleted" "final error"
check "$([ -d "${BUCKET}/rc/ds/${D:0:7}" ] && echo yes || echo no)" yes "D's index kept after the failed prefix rm"
check "$(release_exists "${D}" && echo yes || echo no)" yes "D's release kept"
check "$(grep -c 'release delete' "${STATE}/log")" 0 "no release delete after the failure"
check "$([ -f "${BUCKET}/${MACHINE}/20261021T100000Z-1-standard/manifest.json" ] && echo yes || echo no)" yes "A's build kept"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["delete"][0]["result"], d["keep"][0]["reason"])' "${tmp}/below.json")" "failed sha carries a v* tag" "report records the failure"
# the rerun finishes the job
run_prune --product ds --below 0.1.0 --no-dry-run --report "${tmp}/below2.json"
check "${rc}" 0 "rerun exits 0"
check "$(release_exists "${D}" && echo yes || echo no)" no "D's release gone on the rerun"
check "$([ -d "${BUCKET}/${MACHINE}/20261021T130000Z-4-standard" ] && echo yes || echo no)" no "D's prefix gone"
check "$(release_exists "${A}" && echo yes || echo no)" yes "A's release still kept"

echo "# --sha: selection by sha, keep reasons"
run_prune --product ds --sha "${C:0:7}" --sha "${A}" --dry-run --report "${tmp}/sha.json"
check "${rc}" 0 "exits 0"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(sorted(e["tag"][-7:] for e in d["keep"]), d["delete"])' "${tmp}/sha.json")" "$(python3 -I -c 'import sys; print(sorted([sys.argv[1], sys.argv[2]]), [])' "${A:0:7}" "${C:0:7}")" "both kept, nothing to delete"
contains "${log}" "^total to delete: 0 B in 0 release\(s\)" "zero total"

echo "# an unsafe prefix in a manifest skips that release instead of deleting"
F=$(repo_commit main "f" "foo.txt=f\n")
make_release "rc/${F:0:7}" "${F}" 0.x.x "0.1.0-rc.7.github-${F:0:7}" "${MACHINE}" "20261021T140000Z-5-standard"
sed -i "s#\"s3_prefix\": \"${MACHINE}/20261021T140000Z-5-standard\"#\"s3_prefix\": \"${MACHINE}\"#" "${STATE}/assets/rc_${F:0:7}/release-manifest.json"
: > "${STATE}/log"
run_prune --product ds --line 0.x.x --no-dry-run --report "${tmp}/unsafe.json"
check "${rc}" 0 "exits 0 (a skip is not a failure)"
contains "${log}" "^rc/${F:0:7}  published  unsafe build prefix in the manifest" "skipped with the reason"
check "$(release_exists "${F}" && echo yes || echo no)" yes "F's release kept"
check "$(grep -c 'release delete' "${STATE}/log")" 0 "nothing deleted"

echo "# a tag whose rest is not a 7-hex sha is skipped before any S3 path is built"
echo "{\"tag_name\": \"rc/0.1.0\", \"draft\": false, \"assets\": {}}" > "${RELEASES}/rc_0.1.0.json"
mkdir -p "${BUCKET}/rc/ds/0.1.0"
cp "${STATE}/assets/rc_${F:0:7}/release-manifest.json" "${BUCKET}/rc/ds/0.1.0/release-manifest.json"
# a product-prefixed RC tag of the other naming mode is not rc/<sha7> either
echo "{\"tag_name\": \"rc/ds/${F:0:7}\", \"draft\": false, \"assets\": {}}" > "${RELEASES}/rc_ds_${F:0:7}.json"
: > "${STATE}/log"
run_prune --product ds --line 0.x.x --no-dry-run --report "${tmp}/badtag.json"
check "${rc}" 0 "exits 0 (a skip is not a failure)"
contains "${log}" "^rc/0.1.0  published  tag name is not rc/<sha7> \(7 lowercase hex\); not touching it" "skipped with the reason"
contains "${log}" "^rc/ds/${F:0:7}  published  tag name is not rc/<sha7> \(7 lowercase hex\); not touching it" "a prefixed tag name is skipped too"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print([e["tag"] for e in d["skipped"] if e["tag"] == "rc/0.1.0"], [e["tag"] for e in d["delete"] if e["tag"] == "rc/0.1.0"])' "${tmp}/badtag.json")" "['rc/0.1.0'] []" "in the report as skipped, not deleted"
check "$([ -f "${BUCKET}/rc/ds/0.1.0/release-manifest.json" ] && echo yes || echo no)" yes "its index copy was never read as a manifest nor removed"
check "$(grep -c 'release delete' "${STATE}/log")" 0 "nothing deleted"
rm -f "${RELEASES}/rc_0.1.0.json" "${RELEASES}/rc_ds_${F:0:7}.json"; rm -rf "${BUCKET}/rc/ds/0.1.0"

echo "# arguments"
run_prune --product ds
check "${rc}" 1 "no selector exits 1"
contains "${log}" "exactly one of --line, --below or --sha" "names the selectors"
run_prune --product ds --line 0.x.x --below 1.0.0
check "${rc}" 1 "two selectors exit 1"
run_prune --product ds --line aggregator/main
check "${rc}" 1 "a name that is not a line exits 1"
contains "${log}" "--line aggregator/main is not a line of product ds" "names the line"
run_prune --product vertex --line 0.x.x
check "${rc}" 1 "a product that is not configured exits 1"
contains "${log}" "product 'vertex' is not in release/config.json" "names the config"
run_prune --product ds --below 1.0
check "${rc}" 1 "bad --below exits 1"
run_prune --product ds --sha xyz
check "${rc}" 1 "bad --sha exits 1"
rc=0; (cd "${REPO}" && env -u GH_TOKEN -u GITHUB_TOKEN "${scripts}/prune-rc.sh" --product ds --line 0.x.x > "${log}" 2>&1) || rc=$?
check "${rc}" 1 "no token exits 1"
rc=0; (cd "${REPO}" && env -u GITHUB_REPOSITORY "${scripts}/prune-rc.sh" --product ds --line 0.x.x > "${log}" 2>&1) || rc=$?
check "${rc}" 1 "no GITHUB_REPOSITORY exits 1"
contains "${log}" "GITHUB_REPOSITORY is not set" "names the variable"

echo
if [ "${fails}" = 0 ]; then echo "test_prune_rc: all passed"; else echo "test_prune_rc: ${fails} failure(s)"; exit 1; fi
