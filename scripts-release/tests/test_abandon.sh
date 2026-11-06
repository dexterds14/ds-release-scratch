#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/abandon.sh (ENG-1935, ENG-2110): one temp bare
# repo stands in for origin, the lines are opened by the real
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
MACHINE=ds-arm64
MAIN_TIP=$(head_of "${BARE}" main)
tag_origin v0.0.0 "${MAIN_TIP}"
open_line 0.0.0            # 0.x.x, pending 0.1.0
release_exists() { [ -f "${STATE}/releases/${OWN_REPO//\//_}/rc_${1:0:7}.json" ] && echo yes || echo no; }

# run_abandon <args...> -> $rc, $log
run_abandon() {
  log="${tmp}/log"
  rc=0
  (cd "${REPO}" && "${scripts}/abandon.sh" "$@" > "${log}" 2>&1) || rc=$?
}

echo "# refusals that need no history"
run_abandon --line main --no-dry-run
check "${rc}" 1 "the main line exits 1"
contains "${log}" "::error::abandon: main is the main line: it is never abandoned by this job" "names the rule"
run_abandon --line dev/1.x.x --no-dry-run
check "${rc}" 1 "a non-line exits 1"
contains "${log}" "dev/1.x.x is not a release line" "names the non-line"
run_abandon --line 2.x.x --no-dry-run
check "${rc}" 1 "a missing line exits 1"
contains "${log}" "origin has no branch 2.x.x" "names the missing branch"
check "$(exists "$(head_of "${BARE}" 0.x.x)")" yes "0.x.x untouched"

echo "# a fresh line: only the opening commit, which is bookkeeping, so nothing is unmerged"
export GITHUB_STEP_SUMMARY="${tmp}/summary.md"
: > "${GITHUB_STEP_SUMMARY}"
run_abandon --line 0.x.x --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^unmerged commits:   0 not in main \(1 versions/ or bookkeeping-file commit\(s\) ignored\)" "the opening commit is ignored"
contains "${log}" "^== prune-rc plan \(product ds, line 0.x.x, dry_run=true\)" "prune dry run included"
contains "${log}" "^dry run: nothing deleted" "dry run says so"
lacks "${log}" "::warning::abandon: a real run would refuse" "no refusal warning"
check "$(exists "$(head_of "${BARE}" 0.x.x)")" yes "branch kept"
contains "${GITHUB_STEP_SUMMARY}" "^## Abandon \`0.x.x\` \(dry_run=true, force=false\)" "step summary header"
contains "${GITHUB_STEP_SUMMARY}" "^### prune-rc: ds, line 0.x.x" "step summary has the prune table"
unset GITHUB_STEP_SUMMARY

echo "# work on the line: unmerged commits, one promoted sha (tag) and one plain RC"
X=$(repo_commit 0.x.x "fix foo" "foo.txt=line fix\n")
make_release "rc/${X:0:7}" "${X}" 0.x.x "0.1.0-rc.4.github-${X:0:7}" "${MACHINE}" "20261021T100000Z-1-standard"
tag_origin v0.1.0 "${X}"
repo_commit 0.x.x "release: v0.1.0 promoted; pending 0.2.0 [skip ci]" "versions/ds=0.2.0\n" > /dev/null
# a change to the bookkeeping file is bookkeeping too (merge-up keeps the target's copy)
E=$(repo_commit 0.x.x "ci: pin the env" "${ENV_FILE}=DS_TEGRA_DISTRO_REF=0.x.x\n")
Z=$(repo_commit 0.x.x "another fix" "foo.txt=line fix 2\n")
make_release "rc/${Z:0:7}" "${Z}" 0.x.x "0.2.0-rc.6.github-${Z:0:7}" "${MACHINE}" "20261021T110000Z-2-standard"
# an RC of another line must survive the prune
open_line 0.1.0            # 0.1.x from v0.1.0 (a child of 0.x.x)
P=$(repo_commit 0.1.x "patch fix" "foo.txt=patch\n")
make_release "rc/${P:0:7}" "${P}" 0.1.x "0.1.1-rc.8.github-${P:0:7}" "${MACHINE}" "20261021T120000Z-3-standard"
W=$(repo_commit main "main work" "foo.txt=main\n")
make_release "rc/${W:0:7}" "${W}" main "0.0.1-rc.9.github-${W:0:7}" "${MACHINE}" "20261021T130000Z-4-standard"

run_abandon --line 0.x.x --no-dry-run
check "${rc}" 1 "unmerged line without force exits 1"
contains "${log}" "^unmerged commits:   2 not in main \(3 versions/ or bookkeeping-file commit\(s\) ignored\)" "counts the real commits and ignores the opening, pending and env commits"
contains "${log}" "^                    ${X:0:7} fix foo" "lists the first commit"
contains "${log}" "^                    ${Z:0:7} another fix" "lists the other commit"
lacks "${log}" "^                    ${E:0:7} " "the bookkeeping-file commit is not listed"
contains "${log}" "^child lines:        0.1.x" "lists the child line"
contains "${log}" "::error::abandon: refusing: 2 commit\(s\) of 0.x.x are not in main \(listed above\); child line\(s\) 0.1.x still merge up into 0.x.x" "refusal names both reasons"
check "$(exists "$(head_of "${BARE}" 0.x.x)")" yes "branch kept"
check "$(release_exists "${Z}")" yes "RC kept"
lacks "${log}" "== prune-rc plan" "prune never ran"
run_abandon --line 0.x.x --dry-run
check "${rc}" 0 "dry run of a refused line exits 0"
contains "${log}" "::warning::abandon: a real run would refuse: 2 commit\(s\)" "dry run warns"
contains "${log}" "^rc/${X:0:7}  published  line 0.x.x  0.1.0-rc.4.github-${X:0:7}  1 build\(s\) .* reason: sha carries a" "dry run lists the promoted sha as kept"
contains "${log}" "^rc/${Z:0:7}  published  line 0.x.x  0.2.0-rc.6.github-${Z:0:7}  1 build\(s\) .* reason: no tag" "dry run lists the plain RC for deletion"

echo "# no App token: a real run stops before deleting anything"
run_abandon --line 0.1.x --no-dry-run
check "${rc}" 1 "exits 1 (the patch line has an unmerged commit)"
TOKEN_KIND=github run_abandon --line 0.1.x --force --no-dry-run
check "${rc}" 1 "exits 1"
contains "${log}" "::error::abandon: no App token" "names the problem"
contains "${log}" "git push origin --delete 0.1.x" "names the manual delete"
contains "${log}" "prune-rc.sh --product ds --line 0.1.x --dry-run" "names the manual prune"
check "$(exists "$(head_of "${BARE}" 0.1.x)")" yes "branch kept"
check "$(release_exists "${P}")" yes "RC kept"
TOKEN_KIND=github run_abandon --line 0.1.x --dry-run
check "${rc}" 0 "a dry run works without the App"

echo "# force: the child line goes first (the branch, then its RC), in that order"
: > "${STATE}/log"; : > "${AWS_CALLS}"
run_abandon --line 0.1.x --force --no-dry-run --report "${tmp}/patch.json"
check "${rc}" 0 "exits 0"
contains "${log}" "::warning::abandon: force: 1 commit\(s\) of 0.1.x are not in 0.x.x" "force warns"
check "$(exists "$(head_of "${BARE}" 0.1.x)")" no "branch deleted"
check "$(grep -E '^gh (api -X DELETE|release delete)' "${STATE}/log" | sed 's/ -R / --repo /' | tr '\n' ';')" \
  "gh api -X DELETE repos/ds/repo/git/refs/heads/0.1.x;gh release delete rc/${P:0:7} --repo ds/repo --cleanup-tag --yes;" \
  "order: the branch, then the release"
check "$(grep -n -E 's3 rm' "${AWS_CALLS}" | wc -l)" 2 "the build prefix and the index were removed"
check "$(release_exists "${P}")" no "the line's RC is gone"
check "$([ -d "${BUCKET}/${MACHINE}/20261021T120000Z-3-standard" ] && echo yes || echo no)" no "its build prefix is gone"
check "$(release_exists "${Z}")" yes "the parent line's RC is untouched"
check "$(release_exists "${W}")" yes "main's RC is untouched"
check "$(exists "$(head_of "${BARE}" 0.x.x)")" yes "0.x.x untouched"
contains "${log}" "^done: 0.1.x abandoned" "done line"
check "$(python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["selector"], [e["tag"][-7:] for e in d["delete"]], d["delete"][0]["result"])' "${tmp}/patch.json")" "line 0.1.x ['${P:0:7}'] deleted" "report"

echo "# force on the minor line: the promoted sha and the other lines' RCs stay"
export GITHUB_STEP_SUMMARY="${tmp}/summary2.md"
: > "${GITHUB_STEP_SUMMARY}"
run_abandon --line 0.x.x --force --no-dry-run --report "${tmp}/minor.json"
check "${rc}" 0 "exits 0"
check "$(exists "$(head_of "${BARE}" 0.x.x)")" no "branch deleted"
check "$(release_exists "${Z}")" no "the plain RC is gone"
check "$([ -d "${BUCKET}/rc/ds/${Z:0:7}" ] && echo yes || echo no)" no "its index is gone"
check "$(release_exists "${X}")" yes "the promoted sha's RC is kept"
check "$([ -f "${BUCKET}/${MACHINE}/20261021T100000Z-1-standard/manifest.json" ] && echo yes || echo no)" yes "and its build"
check "$(tags_of "${BARE}" | tr '\n' ' ')" "$(printf '%s\n' "rc/${W:0:7}" "rc/${X:0:7}" v0.0.0 v0.1.0 | LC_ALL=C sort | tr '\n' ' ')" "tags: the promoted RC, main's RC and the release tags remain"
check "$(release_exists "${W}")" yes "main's RC is untouched"
check "$(heads_of "${BARE}" | tr '\n' ' ')" "main " "only main remains"
contains "${GITHUB_STEP_SUMMARY}" "^- deleted \`0.x.x\` \(was" "summary records the branch delete"
contains "${GITHUB_STEP_SUMMARY}" "^\| delete \| \`rc/${Z:0:7}\` \| \`0.x.x\` \| .* \| deleted \|$" "summary table has the result"
unset GITHUB_STEP_SUMMARY

echo
if [ "${fails}" = 0 ]; then echo "test_abandon: all passed"; else echo "test_abandon: ${fails} failure(s)"; exit 1; fi
