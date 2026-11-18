#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/abandon.sh (ENG-1935): two temp bare repos stand
# in for ds-tegra-distro and meta-dsensos, the lines are opened by the real
# create-branch.sh, gh and aws are the stubs in tests/stubs-tree. No network.
#   bash scripts-release/tests/test_abandon.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; fails=$((fails + 1)); fi; }
exists() { if [ -n "$1" ]; then echo yes; else echo no; fi; }

. "${here}/stubs-tree/fixture.sh"
tree_fixture_init
seed_vertex
MACHINE=jetson-orin-nx-a603-8gb
MAIN_TIP=$(head_of "${DISTRO_BARE}" vertex/main)
tag_distro vertex/v0.0.0 "${MAIN_TIP}"
open_line 0.0.0            # vertex/0.x.x in both repos, pending 0.1.0
release_exists() { [ -f "${STATE}/releases/${DISTRO_REPO//\//_}/rc_vertex_${1:0:7}.json" ] && echo yes || echo no; }

# run_abandon <args...> -> $rc, $log
run_abandon() {
  log="${tmp}/log"
  rc=0
  (cd "${DISTRO}" && "${scripts}/abandon.sh" "$@" > "${log}" 2>&1) || rc=$?
}

echo "# refusals that need no history"
run_abandon --line vertex/main --no-dry-run
check "${rc}" 1 "a product main exits 1"
contains "${log}" "::error::abandon: vertex/main is a product main: a product tree is retired by hand" "names the rule"
run_abandon --line dev/1.x.x --no-dry-run
check "${rc}" 1 "a non-line exits 1"
run_abandon --line vertex/2.x.x --no-dry-run
check "${rc}" 1 "a missing line exits 1"
contains "${log}" "origin has no branch vertex/2.x.x" "names the missing branch"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.x.x)")" yes "vertex/0.x.x untouched"

echo "# a fresh line: only the opening commit, which is bookkeeping, so nothing is unmerged"
export GITHUB_STEP_SUMMARY="${tmp}/summary.md"
: > "${GITHUB_STEP_SUMMARY}"
run_abandon --line vertex/0.x.x --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^unmerged commits:   0 not in vertex/main \(1 versions/ or base.yml bookkeeping commit\(s\) ignored\)" "the opening commit is ignored"
contains "${log}" "^== prune-rc plan \(product vertex, line vertex/0.x.x, dry_run=true\)" "prune dry run included"
contains "${log}" "^dry run: nothing deleted" "dry run says so"
lacks "${log}" "::warning::abandon: a real run would refuse" "no refusal warning"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.x.x)")" yes "ds-tegra-distro branch kept"
check "$(exists "$(head_of "${META_BARE}" vertex/0.x.x)")" yes "meta-dsensos branch kept"
contains "${GITHUB_STEP_SUMMARY}" "^## Abandon \`vertex/0.x.x\` \(dry_run=true, force=false\)" "step summary header"
contains "${GITHUB_STEP_SUMMARY}" "^### prune-rc: vertex, line vertex/0.x.x" "step summary has the prune table"
unset GITHUB_STEP_SUMMARY

echo "# work on the line: an unmerged commit, one promoted sha (tag) and one plain RC"
M3=$(meta_commit vertex/0.x.x "layer fix" "layer.txt=m3\n")
write_lock "${tmp}/lock" "${M3}"
X=$(distro_commit vertex/0.x.x "pin m3 and fix foo" "kas/include/base.lock.yml=$(cat "${tmp}/lock")" "foo.txt=line fix\n")
make_release "rc/vertex/${X:0:7}" "${X}" vertex/0.x.x "0.1.0-rc.4.github-${X:0:7}" "${MACHINE}" "20261021T100000Z-1-standard"
tag_distro vertex/v0.1.0 "${X}"
Y=$(distro_commit vertex/0.x.x "release: pending 0.2.0 [skip ci]" "versions/vertex=0.2.0\n")
Z=$(distro_commit vertex/0.x.x "another fix" "foo.txt=line fix 2\n")
make_release "rc/vertex/${Z:0:7}" "${Z}" vertex/0.x.x "0.2.0-rc.6.github-${Z:0:7}" "${MACHINE}" "20261021T110000Z-2-standard"
# an RC of another line must survive the prune
open_line 0.1.0            # vertex/0.1.x from vertex/v0.1.0 (a child of 0.x.x)
P=$(distro_commit vertex/0.1.x "patch fix" "foo.txt=patch\n")
make_release "rc/vertex/${P:0:7}" "${P}" vertex/0.1.x "0.1.1-rc.8.github-${P:0:7}" "${MACHINE}" "20261021T120000Z-3-standard"
W=$(distro_commit vertex/main "main work" "foo.txt=main\n")
make_release "rc/vertex/${W:0:7}" "${W}" vertex/main "0.0.1-rc.9.github-${W:0:7}" "${MACHINE}" "20261021T130000Z-4-standard"

run_abandon --line vertex/0.x.x --no-dry-run
check "${rc}" 1 "unmerged line without force exits 1"
contains "${log}" "^unmerged commits:   2 not in vertex/main \(2 versions/ or base.yml bookkeeping commit\(s\) ignored\)" "counts the real commits and ignores the opening and pending commits"
contains "${log}" "^                    ${X:0:7} pin m3 and fix foo" "lists the pin commit"
contains "${log}" "^                    ${Z:0:7} another fix" "lists the other commit"
contains "${log}" "^child lines:        vertex/0.1.x" "lists the child line"
contains "${log}" "::error::abandon: refusing: 2 commit\(s\) of vertex/0.x.x are not in vertex/main \(listed above\); child line\(s\) vertex/0.1.x still merge up into vertex/0.x.x" "refusal names both reasons"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.x.x)")" yes "branch kept"
check "$(exists "$(head_of "${META_BARE}" vertex/0.x.x)")" yes "meta branch kept"
check "$(release_exists "${Z}")" yes "RC kept"
lacks "${log}" "== prune-rc plan" "prune never ran"
run_abandon --line vertex/0.x.x --dry-run
check "${rc}" 0 "dry run of a refused line exits 0"
contains "${log}" "::warning::abandon: a real run would refuse: 2 commit\(s\)" "dry run warns"
contains "${log}" "^rc/vertex/${X:0:7}  published  line vertex/0.x.x  0.1.0-rc.4.github-${X:0:7}  1 build\(s\) .* reason: sha carries a vertex/v\* tag" "dry run lists the promoted sha as kept"
contains "${log}" "^rc/vertex/${Z:0:7}  published  line vertex/0.x.x  0.2.0-rc.6.github-${Z:0:7}  1 build\(s\) .* reason: no tag" "dry run lists the plain RC for deletion"

echo "# no App token: a real run stops before deleting anything"
run_abandon --line vertex/0.1.x --no-dry-run
check "${rc}" 1 "exits 1 (the patch line has an unmerged commit)"
TOKEN_KIND=github run_abandon --line vertex/0.1.x --force --no-dry-run
check "${rc}" 1 "exits 1"
contains "${log}" "::error::abandon: no App token" "names the problem"
contains "${log}" "git push origin --delete vertex/0.1.x" "names the manual delete"
contains "${log}" "prune-rc.sh --product vertex --line vertex/0.1.x --dry-run" "names the manual prune"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.1.x)")" yes "branch kept"
check "$(release_exists "${P}")" yes "RC kept"
TOKEN_KIND=github run_abandon --line vertex/0.1.x --dry-run
check "${rc}" 0 "a dry run works without the App"

echo "# force: the child line goes first (both branches, then its RC), in that order"
: > "${STATE}/log"; : > "${AWS_CALLS}"
run_abandon --line vertex/0.1.x --force --no-dry-run --report "${tmp}/patch.json"
check "${rc}" 0 "exits 0"
contains "${log}" "::warning::abandon: force: 1 commit\(s\) of vertex/0.1.x are not in vertex/0.x.x" "force warns"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.1.x)")" no "ds-tegra-distro branch deleted"
check "$(exists "$(head_of "${META_BARE}" vertex/0.1.x)")" no "meta-dsensos branch deleted"
check "$(grep -E '^gh (api -X DELETE|release delete)' "${STATE}/log" | tr '\n' ';')" \
  "gh api -X DELETE repos/ds/distro/git/refs/heads/vertex/0.1.x;gh api -X DELETE repos/ds/meta/git/refs/heads/vertex/0.1.x;gh release delete rc/vertex/${P:0:7} -R ds/distro --cleanup-tag --yes;" \
  "order: distro branch, meta branch, then the release"
check "$(grep -n -E 's3 rm' "${AWS_CALLS}" | wc -l)" 2 "the build prefix and the index were removed"
check "$(release_exists "${P}")" no "the line's RC is gone"
check "$([ -d "${BUCKET}/${MACHINE}/20261021T120000Z-3-standard" ] && echo yes || echo no)" no "its build prefix is gone"
check "$(release_exists "${Z}")" yes "the parent line's RC is untouched"
check "$(release_exists "${W}")" yes "main's RC is untouched"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.x.x)")" yes "vertex/0.x.x untouched"
check "$(exists "$(head_of "${META_BARE}" vertex/main)")" yes "meta vertex/main untouched"
contains "${log}" "^done: vertex/0.1.x abandoned" "done line"
check "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["selector"], [e["tag"][-7:] for e in d["delete"]], d["delete"][0]["result"])' "${tmp}/patch.json")" "line vertex/0.1.x ['${P:0:7}'] deleted" "report"

echo "# force on the minor line, with the meta-dsensos branch already gone: a notice, not an error"
git -C "${META_BARE}" update-ref -d refs/heads/vertex/0.x.x
export GITHUB_STEP_SUMMARY="${tmp}/summary2.md"
: > "${GITHUB_STEP_SUMMARY}"
run_abandon --line vertex/0.x.x --force --no-dry-run --report "${tmp}/minor.json"
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::abandon: meta-dsensos has no branch vertex/0.x.x; nothing to delete there" "missing meta branch is a notice"
check "$(exists "$(head_of "${DISTRO_BARE}" vertex/0.x.x)")" no "ds-tegra-distro branch deleted"
check "$(release_exists "${Z}")" no "the plain RC is gone"
check "$([ -d "${BUCKET}/rc/vertex/${Z:0:7}" ] && echo yes || echo no)" no "its index is gone"
check "$(release_exists "${X}")" yes "the promoted sha's RC is kept"
check "$([ -f "${BUCKET}/${MACHINE}/20261021T100000Z-1-standard/manifest.json" ] && echo yes || echo no)" yes "and its build"
check "$(tags_of "${DISTRO_BARE}" | tr '\n' ' ')" "$(printf '%s\n' "rc/vertex/${W:0:7}" "rc/vertex/${X:0:7}" vertex/v0.0.0 vertex/v0.1.0 | LC_ALL=C sort | tr '\n' ' ')" "tags: the promoted RC, main's RC and the product tags remain"
check "$(release_exists "${W}")" yes "main's RC is untouched"
check "$(heads_of "${DISTRO_BARE}" | tr '\n' ' ')" "trunk vertex/main " "only trunk and vertex/main remain"
contains "${GITHUB_STEP_SUMMARY}" "^- deleted \`vertex/0.x.x\` in ds-tegra-distro" "summary records the branch delete"
contains "${GITHUB_STEP_SUMMARY}" "^\| delete \| \`rc/vertex/${Z:0:7}\` \| \`vertex/0.x.x\` \| .* \| deleted \|$" "summary table has the result"
unset GITHUB_STEP_SUMMARY

echo
if [ "${fails}" = 0 ]; then echo "test_abandon: all passed"; else echo "test_abandon: ${fails} failure(s)"; exit 1; fi
