#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/merge-up.sh (ENG-1935, ENG-2110): one temp bare
# repo stands in for origin, 1.x.x is opened by the real create-branch.sh,
# gh is the stub in tests/stubs-tree (run list from a green list, PRs kept
# in a state file, pr merge performed in the bare repo). No network, no git
# identity (the merge commit must come out as the bot).
#   bash scripts-release/tests/test_merge_up.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; fails=$((fails + 1)); fi; }
BOT='ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>'

. "${here}/stubs-tree/fixture.sh"
tree_fixture_init
MAIN_TIP=$(head_of "${BARE}" main)
tag_origin v1.0.0 "${MAIN_TIP}"
open_line 1.0.0            # 1.x.x, pending 1.1.0

# run_merge_up <args...> -> $rc, $log
run_merge_up() {
  log="${tmp}/log"
  rc=0
  (cd "${REPO}" && "${scripts}/merge-up.sh" "$@" > "${log}" 2>&1) || rc=$?
}
show() { git -C "${BARE}" show "$1:$2"; }
subject_of() { git -C "${BARE}" log -1 --format=%s "$1"; }
author_of() { git -C "${BARE}" log -1 --format='%an <%ae>' "$1"; }
nth_parent() { git -C "${BARE}" rev-parse "$1^$2"; }
pr_count() { python3 -I -c 'import json,os,sys; p=sys.argv[1]; print(len(json.load(open(p))) if os.path.exists(p) else 0)' "${STATE}/prs.json"; }
pr_field() { python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))[int(sys.argv[2])][sys.argv[3]])' "${STATE}/prs.json" "$1" "$2"; }

echo "# refusals"
run_merge_up
check "${rc}" 1 "no --child exits 1"
run_merge_up --child main
check "${rc}" 1 "main exits 1"
contains "${log}" "::error::merge-up: main is the main line" "names the rule"
run_merge_up --child dev/1.x.x
check "${rc}" 1 "a non-line exits 1"
contains "${log}" "dev/1.x.x is not a release line" "names the non-line"
run_merge_up --child 1.x.x --parent 2.x.x
check "${rc}" 1 "a wrong --parent exits 1"
contains "${log}" "the parent line of 1.x.x is main" "names the real parent"
run_merge_up --child 2.x.x
check "${rc}" 1 "a missing child exits 1"
contains "${log}" "origin has no branch 2.x.x" "names the branch"
run_merge_up --child 1.x.x --pr x
check "${rc}" 1 "a non-numeric --pr exits 1"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"

echo "# child work: a fix plus a bookkeeping-file change (green), then a versions-only commit"
GREEN=$(repo_commit 1.x.x "child fix and env" "foo.txt=child fix\n" "${ENV_FILE}=DS_TEGRA_DISTRO_REF=child\n")
CHILD_TIP=$(repo_commit 1.x.x "release: v1.1.0 promoted; pending 1.2.0 [skip ci]" "versions/ds=1.2.0\n")

echo "# not green: refused"
run_merge_up --child 1.x.x
check "${rc}" 1 "exits 1"
contains "${log}" "::error::merge-up: no successful release-rc-build.yml run on 1.x.x for ${CHILD_TIP} ${GREEN} \(the tip and the versions/-only commits before it\)" "names the candidates"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"

echo "# dry run and no App token"
mark_green "${GREEN}"
run_merge_up --child 1.x.x --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^== merge-up plan \(dry_run=true, token=app\)" "plan header"
contains "${log}" "^green run:          release-rc-build.yml run 56 \(id 1234\) on ${GREEN:0:7} \(tip ${CHILD_TIP:0:7} only touches versions/\)" "the green run behind the versions-only tip"
contains "${log}" "^merge:              merge 1.x.x \(${CHILD_TIP:0:7}\) into main \(${MAIN_TIP:0:7}\)" "the trial merge"
contains "${log}" "kept from the target: versions/ds, ${ENV_FILE}" "what the target keeps"
contains "${log}" "^dry run: nothing pushed" "dry run says so"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"
TOKEN_KIND=github run_merge_up --child 1.x.x --dry-run
check "${rc}" 0 "a dry run works without the App"
contains "${log}" "^dry run: nothing pushed" "dry run says so"
TOKEN_KIND=github run_merge_up --child 1.x.x
check "${rc}" 1 "a real run without the App exits 1"
contains "${log}" "::error::merge-up: no App token \(vars.DS_RELEASE_APP_CLIENT_ID unset\)" "names the problem"
contains "${log}" "git fetch origin && git checkout main && git merge --no-ff --no-commit origin/1.x.x; python3 scripts-release/release_git.py merge-keep-target && git commit -m 'Merge 1.x.x into main' && git push origin main" "names the manual merge"
contains "${log}" "::error::  gh workflow run release-rc-build.yml --ref main" "names the manual rc-build"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"
check "$(pr_count)" 0 "no PR opened"

echo "# the merge: the target keeps versions/ and the bookkeeping file, the child's foo.txt arrives"
run_merge_up --child 1.x.x
check "${rc}" 0 "exits 0"
contains "${log}" "run 56 \(id 1234\)" "the green run"
contains "${log}" "only touches versions/" "the versions-only tip is walked past"
TIP=$(head_of "${BARE}" main)
check "$(nth_parent "${TIP}" 1)" "${MAIN_TIP}" "first parent is the old main"
check "$(nth_parent "${TIP}" 2)" "${CHILD_TIP}" "second parent is the child tip"
check "$(subject_of "${TIP}")" "Merge 1.x.x into main" "subject"
check "$(author_of "${TIP}")" "${BOT}" "author is the bot (no GIT_* in the environment)"
check "$(show "${TIP}" versions/ds)" 0.0.1 "main keeps its pending version"
check "$(show "${TIP}" "${ENV_FILE}")" "DS_TEGRA_DISTRO_REF=main" "main keeps its bookkeeping file"
check "$(show "${TIP}" foo.txt)" "child fix" "the child's fix arrived"
contains "${log}" "^post-check ok: ${TIP} merges ${CHILD_TIP}" "post-check"
contains "${log}" "^done: 1.x.x merged into main at ${TIP}" "done line"
check "$(pr_count)" 0 "no PR opened"
check "$(head_of "${BARE}" 1.x.x)" "${CHILD_TIP}" "the child is untouched"
run_merge_up --child 1.x.x
check "${rc}" 0 "a second run exits 0"
contains "${log}" "main already contains 1.x.x \(${CHILD_TIP:0:7}\); nothing to merge" "nothing to merge"
check "$(head_of "${BARE}" main)" "${TIP}" "main untouched"

echo "# started by the child tip's own green rc-build run (workflow_run): CALLER_SHA and CALLER_RUN stand in for the lookup"
MAIN_TIP="${TIP}"
mark_green
CHILD_TIP=$(repo_commit 1.x.x "later fix" "foo.txt=later\n")
: > "${STATE}/log"
CALLER_SHA="${CHILD_TIP}" CALLER_RUN="4321 78" run_merge_up --child 1.x.x
check "${rc}" 0 "exits 0"
contains "${log}" "^green run:          release-rc-build.yml run 78 \(id 4321\) on ${CHILD_TIP:0:7} \(the calling run" "the caller's run"
lacks "${STATE}/log" "run list" "gh was never asked"
TIP=$(head_of "${BARE}" main)
check "$(nth_parent "${TIP}" 2)" "${CHILD_TIP}" "merged"
MAIN_TIP="${TIP}"
CHILD_TIP=$(repo_commit 1.x.x "even later fix" "foo.txt=even later\n")
: > "${STATE}/log"
CALLER_SHA="${CHILD_TIP}" CALLER_RUN="" run_merge_up --child 1.x.x
check "${rc}" 1 "a dispatch (CALLER_RUN empty) still looks up, and finds nothing"
contains "${STATE}/log" "run list" "gh was asked"
PREV="${CHILD_TIP}"
CHILD_TIP=$(repo_commit 1.x.x "moved on" "foo.txt=moved on\n")
CALLER_SHA="${PREV}" CALLER_RUN="4321 78" run_merge_up --child 1.x.x
check "${rc}" 1 "a child that moved since the caller's sha goes through the lookup"
contains "${log}" "no successful release-rc-build.yml run on 1.x.x" "and is refused while red"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"
mark_green "${CHILD_TIP}"
CALLER_SHA="${PREV}" CALLER_RUN="4321 78" run_merge_up --child 1.x.x
check "${rc}" 0 "and merges once the new tip is green"
contains "${log}" "run 56 \(id 1234\)" "from the lookup"
lacks "${log}" "the calling run" "not the caller"
MAIN_TIP=$(head_of "${BARE}" main)

echo "# a parent without versions/ is refused before anything is merged"
git -C "${REPO}" fetch -q origin
git -C "${REPO}" checkout -q -B main origin/main
git -C "${REPO}" rm -q -r versions
tgit -C "${REPO}" commit -q -m "drop versions"
git -C "${REPO}" push -q origin main
CHILD_TIP=$(repo_commit 1.x.x "fix after drop" "foo.txt=after drop\n")
mark_green "${CHILD_TIP}"
BEFORE=$(heads_of "${BARE}" | tr '\n' ' ')
run_merge_up --child 1.x.x
check "${rc}" 1 "exits 1"
contains "${log}" "::error::merge-up: main has no versions/ directory" "names the problem"
lacks "${log}" "pathspec" "no bare pathspec error"
check "$(heads_of "${BARE}" | tr '\n' ' ')" "${BEFORE}" "nothing pushed"
git -C "${REPO}" checkout -q -B main "${MAIN_TIP}"
git -C "${REPO}" push -q -f origin main

echo "# a conflict: main plus the merge of 1.x.x, markers committed, is pushed as merge-up/1.x.x/<sha7> and a PR opened; --pr finishes it"
PARENT_TIP=$(repo_commit main "main fix" "foo.txt=main fix\n" "versions/ds=2.0.0\n")
run_merge_up --child 1.x.x --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^merge:              merge 1.x.x \(${CHILD_TIP:0:7}\) into main conflicts in: foo.txt" "the dry run reports the conflict"
contains "${log}" "^dry run: would push merge-up/1.x.x/${CHILD_TIP:0:7} \(main plus the merge of 1.x.x, markers in the files above\) and open the conflict PR against main; nothing pushed" "and what the real run would do"
check "$(head_of "${BARE}" "merge-up/1.x.x/${CHILD_TIP:0:7}")" "" "nothing pushed"
check "$(pr_count)" 0 "no PR opened"
run_merge_up --child 1.x.x
check "${rc}" 1 "exits 1"
contains "${log}" "conflicts in: foo.txt" "names the file"
BRANCH="merge-up/1.x.x/${CHILD_TIP:0:7}"
HEAD=$(head_of "${BARE}" "${BRANCH}")
check "$(nth_parent "${HEAD}" 1)" "${PARENT_TIP}" "the conflict branch is main plus the merge"
check "$(nth_parent "${HEAD}" 2)" "${CHILD_TIP}" "with the child tip as second parent"
check "$(subject_of "${HEAD}")" "Merge 1.x.x into main (conflicts to resolve)" "subject"
check "$(author_of "${HEAD}")" "${BOT}" "author is the bot"
check "$(show "${HEAD}" foo.txt | grep -c -E '^(<<<<<<< |>>>>>>> )')" 2 "the markers are committed in foo.txt"
check "$(show "${HEAD}" versions/ds)" 2.0.0 "main keeps its pending version (not a conflict)"
check "$(show "${HEAD}" "${ENV_FILE}")" "DS_TEGRA_DISTRO_REF=main" "main keeps its bookkeeping file"
contains "${log}" "^pushed ${BRANCH}: ${HEAD:0:7}, main plus the merge of 1.x.x with markers in foo.txt" "says what it pushed"
check "$(head_of "${BARE}" main)" "${PARENT_TIP}" "main untouched"
check "$(pr_count)" 1 "one PR opened"
check "$(pr_field 0 repo)/$(pr_field 0 base)/$(pr_field 0 head)" "ds/repo/main/${BRANCH}" "against main from the conflict branch"
check "$(pr_field 0 title)" "Merge-up 1.x.x into main (conflict)" "PR title"
check "$(pr_field 0 body | grep -c -- '- `foo.txt`')" 1 "PR body lists the file"
check "$(pr_field 0 body | grep -c 'The conflict markers are committed in the files above')" 1 "PR body says the markers are in"
check "$(pr_field 0 body | grep -c 'The bot refuses to finish while a marker remains')" 1 "PR body says how it ends"
check "$(pr_field 0 body | grep -c 'meta-dsensos')" 0 "PR body names no other repository"
URL=$(pr_field 0 url)
contains "${log}" "::error::merge-up: 1.x.x -> main conflicts \(foo.txt\); resolve ${URL}, then dispatch release-merge-up.yml with child=1.x.x pr=1" "the error links the PR"
run_merge_up --child 1.x.x
check "${rc}" 1 "a re-run exits 1 again"
contains "${log}" "^${BRANCH} already exists on origin; left as it is" "leaves the branch alone"
check "$(head_of "${BARE}" "${BRANCH}")" "${HEAD}" "the branch is untouched"
check "$(pr_count)" 1 "and reuses the PR"
run_merge_up --child 1.x.x --pr 2
check "${rc}" 1 "an unknown PR number exits 1"
contains "${log}" "PR #2 does not exist in ds/repo" "names it"
run_merge_up --child 1.x.x --pr 1
check "${rc}" 1 "an unapproved PR is refused"
contains "${log}" "::error::merge-up: PR ds/repo#1 is not ready to merge" "names the PR"
contains "${log}" "release_git.py: pr-check: reviewDecision is None, not APPROVED" "with the reason"
check "$(head_of "${BARE}" main)" "${PARENT_TIP}" "main untouched"
cat > "${STATE}/pr-1-review.json" <<'EOF'
{"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "statusCheckRollup": [],
 "reviews": [{"author": {"login": "resolver"}, "state": "APPROVED"}],
 "commits": [{"authors": [{"login": "resolver"}]}]}
EOF
run_merge_up --child 1.x.x --pr 1
check "${rc}" 1 "approved by the pusher only is refused"
contains "${log}" "no APPROVED review by someone other than the last pusher \('resolver'\)" "with the reason"
cat > "${STATE}/pr-1-review.json" <<'EOF'
{"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "statusCheckRollup": [],
 "reviews": [{"author": {"login": "resolver"}, "state": "APPROVED"}, {"author": {"login": "alice"}, "state": "APPROVED"}],
 "commits": [{"authors": [{"login": "resolver"}]}]}
EOF
run_merge_up --child 1.x.x --pr 1
check "${rc}" 1 "approved but the markers still in is refused"
contains "${log}" "::error::merge-up: PR ds/repo#1 still has conflict markers in: foo.txt" "names the marked file"
check "$(head_of "${BARE}" main)" "${PARENT_TIP}" "main untouched"
# a human resolves on the PR branch: edit the marked file, commit, push (no merge of main needed)
git -C "${REPO}" fetch -q origin
git -C "${REPO}" checkout -q -B "${BRANCH}" "origin/${BRANCH}"
printf 'both fixes\n' > "${REPO}/foo.txt"
tgit -C "${REPO}" add -A
tgit -C "${REPO}" commit -q -m resolve
git -C "${REPO}" push -q origin "${BRANCH}"
PR_HEAD=$(git -C "${REPO}" rev-parse HEAD)
run_merge_up --child 1.x.x --pr 1 --dry-run
check "${rc}" 0 "dry run of the finish exits 0"
contains "${log}" "^finish PR:          ds/repo#1 \(head ${PR_HEAD}\)" "the plan names the PR"
contains "${log}" "^dry run: would gh pr merge 1 -R ds/repo --merge \(head ${PR_HEAD}\)" "and the merge it would do"
check "$(head_of "${BARE}" main)" "${PARENT_TIP}" "main untouched"
run_merge_up --child 1.x.x --pr 1
check "${rc}" 0 "the finish exits 0"
TIP=$(head_of "${BARE}" main)
check "$(nth_parent "${TIP}" 2)" "${PR_HEAD}" "the PR head is the second parent"
check "$(git -C "${BARE}" merge-base --is-ancestor "${CHILD_TIP}" "${TIP}" && echo yes)" yes "the child tip is an ancestor"
contains "${log}" "^merged PR ds/repo#1 \(head ${PR_HEAD}\)" "reports the merge"
contains "${log}" "^done: 1.x.x merged into main at ${TIP} \(via PR 1\)" "done line"
check "$(pr_field 0 state)" MERGED "the PR is merged"
check "$(show "${TIP}" foo.txt)" "both fixes" "what the human left on the PR branch is what merged"
run_merge_up --child 1.x.x --pr 1
check "${rc}" 1 "a merged PR cannot be finished again"
contains "${log}" "PR #1 is not an open merge-up/1.x.x/\* PR against main" "names the state"

echo "# versions/ and the bookkeeping file stay target-side even when both sides changed them; the README merges"
MAIN_TIP=$(repo_commit main "parent pending version and env" "versions/ds=3.0.0\n" "${ENV_FILE}=DS_TEGRA_DISTRO_REF=parent\n")
CHILD_TIP=$(repo_commit 1.x.x "child: README fix, its own pending version and env" "versions/README.md=pending versions, fixed\n" "versions/ds=1.1.9\n" "${ENV_FILE}=DS_TEGRA_DISTRO_REF=child2\n" "foo.txt=both fixes\n")
mark_green "${CHILD_TIP}"
run_merge_up --child 1.x.x
check "${rc}" 0 "exits 0"
TIP=$(head_of "${BARE}" main)
check "$(nth_parent "${TIP}" 2)" "${CHILD_TIP}" "merged"
check "$(show "${TIP}" versions/ds)" 3.0.0 "main keeps its pending version"
check "$(show "${TIP}" "${ENV_FILE}")" "DS_TEGRA_DISTRO_REF=parent" "main keeps its bookkeeping file"
check "$(show "${TIP}" versions/README.md)" "pending versions, fixed" "the README merged"
check "$(pr_count)" 1 "no new PR"
check "$(python3 "${scripts}/release_git.py" unmerged "${TIP}" "${CHILD_TIP}" --git-dir "${BARE}" --root "${REPO}" | wc -l)" 0 "nothing left unmerged"

echo
if [ "${fails}" = 0 ]; then echo "test_merge_up: all passed"; else echo "test_merge_up: ${fails} failure(s)"; exit 1; fi
