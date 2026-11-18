#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/sync-product-releases.sh (ENG-1935): two temp
# bare repos stand in for ds-tegra-distro and meta-dsensos, the product mains
# are seeded by the real create-branch.sh, gh is the stub in tests/stubs-tree
# (PRs in a JSON file, merges performed in the bare repo). No network.
#   bash scripts-release/tests/test_sync.sh
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
(cd "${DISTRO}" && "${scripts}/create-branch.sh" --product aggregator --seed-from trunk > "${tmp}/seed-agg.log" 2>&1) || { cat "${tmp}/seed-agg.log"; exit 1; }
SYNC="${scripts}/sync-product-releases.sh"

# run_sync <args...> -> $rc, $log
run_sync() {
  log="${tmp}/log"
  rc=0
  (cd "${DISTRO}" && "${SYNC}" "$@" > "${log}" 2>&1) || rc=$?
}
pr_count() { python3 -c 'import json,os,sys; p=sys.argv[1]; print(len(json.load(open(p))) if os.path.exists(p) else 0)' "${STATE}/prs.json"; }
# pr_field <number> <field>
pr_field() { python3 -c 'import json,sys; print([p for p in json.load(open(sys.argv[1])) if p["number"]==int(sys.argv[2])][0][sys.argv[3]])' "${STATE}/prs.json" "$1" "$2"; }
pr_body() { pr_field "$1" body > "${tmp}/body-$1.md"; echo "${tmp}/body-$1.md"; }
show() { git -C "$1" show "$2:$3"; }
approve() {
  echo '{"reviewDecision": "APPROVED", "mergeStateStatus": "CLEAN", "statusCheckRollup": [{"name": "pr-checks", "conclusion": "SUCCESS"}], "reviews": [{"author": {"login": "alice"}, "state": "APPROVED"}], "commits": [{"authors": [{"login": "bob"}]}]}' > "${STATE}/pr-$1-review.json"
}
VMAIN0=$(head_of "${DISTRO_BARE}" vertex/main)
AMAIN0=$(head_of "${DISTRO_BARE}" aggregator/main)

echo "# nothing to sync: the mains already contain the trunk"
run_sync --from-ref trunk --no-dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "::notice::sync: vertex: vertex/main \(${VMAIN0}\) already contains trunk" "vertex reported"
contains "${log}" "::notice::sync: aggregator: aggregator/main" "aggregator reported"
check "$(pr_count)" 0 "no PR"

echo "# a trunk change that keeps the pin: one PR per product (ds-tegra-distro only)"
T1=$(distro_commit trunk "t1" "foo.txt=t1\n")
run_sync --from-ref trunk --products vertex --no-dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "^meta-dsensos:       vertex/main at ${M2} already contains the pin ${M2:0:7}; no meta-dsensos PR needed" "pin containment reported"
check "$(pr_count)" 1 "one PR"
check "$(pr_field 1 repo) $(pr_field 1 base) $(pr_field 1 head) $(pr_field 1 title)" "ds/distro vertex/main sync/vertex/${T1:0:7} Sync trunk ${T1:0:7} into vertex/main" "PR repo, base, head, title"
contains "$(pr_body 1)" "already contains the pin \`${M2}\`; no meta-dsensos PR is needed" "body says no meta-dsensos PR"
check "$(exists "$(head_of "${META_BARE}" "sync/vertex/${T1:0:7}")")" no "no meta-dsensos branch"
check "$(exists "$(head_of "${DISTRO_BARE}" "sync/aggregator/${T1:0:7}")")" no "aggregator not selected"
S1=$(head_of "${DISTRO_BARE}" "sync/vertex/${T1:0:7}")
check "$(git -C "${DISTRO_BARE}" rev-parse "${S1}^1") $(git -C "${DISTRO_BARE}" rev-parse "${S1}^2")" "${VMAIN0} ${T1}" "sync branch = main + merge of the trunk"
check "$(show "${DISTRO_BARE}" "${S1}" foo.txt)" t1 "carries the trunk change"
check "$(head_of "${DISTRO_BARE}" vertex/main)" "${VMAIN0}" "vertex/main itself untouched"

echo "# finish: refused until approved, then merged with a merge commit"
run_sync --finish 1 --no-dry-run
check "${rc}" 1 "unapproved exits 1"
contains "${log}" "not ready to merge" "names the self-check"
check "$(head_of "${DISTRO_BARE}" vertex/main)" "${VMAIN0}" "nothing merged"
approve 1
run_sync --finish 1 --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^dry run: would gh pr merge 1 -R ds/distro --merge \(head ${S1}\)" "dry run names the merge"
check "$(head_of "${DISTRO_BARE}" vertex/main)" "${VMAIN0}" "dry run merges nothing"
run_sync --finish 1 --no-dry-run
check "${rc}" 0 "exits 0"
VMAIN1=$(head_of "${DISTRO_BARE}" vertex/main)
check "$(git -C "${DISTRO_BARE}" rev-parse "${VMAIN1}^2")" "${S1}" "vertex/main tip is a merge commit of the PR head"
contains "${log}" "post-check ok: ${VMAIN1} merges ${S1}" "post-check"
contains "${log}" "::notice::sync: vertex/main is at ${VMAIN1}; the push starts release-rc-build.yml there" "says rc-build follows"
check "$(pr_field 1 state)" MERGED "PR merged"
run_sync --finish 1 --no-dry-run
check "${rc}" 1 "a merged PR cannot be finished again"
run_sync --finish 99 --no-dry-run
check "${rc}" 1 "an unknown PR exits 1"
contains "${log}" "not an open sync/<product>/<sha7> PR" "names the rule"

echo "# a trunk change that moves the pin and touches versions/ and base.yml: two PRs per product"
M3=$(meta_commit trunk "m3" "layer.txt=m3\n")
write_lock "${tmp}/lock" "${M3}"
# the trunk renames its meta-dsensos branch (as the cut-over will), adds a layer stanza (as
# ENG-1690 added meta-swupdate) and moves versions/vertex; vertex/main bumped its own pending
# version meanwhile (as a promote does)
BASE_TRUNK=$(show "${DISTRO_BARE}" trunk kas/include/base.yml | sed -e 's/branch: trunk   # tracks the trunk/branch: trunk2   # renamed/' -e 's/^  meta-dsensos:$/  meta-new:\n    url: https:\/\/example.invalid\/meta-new\n    path: repos\/meta-new\n  meta-dsensos:/')
T2=$(distro_commit trunk "t2" "kas/include/base.lock.yml=$(cat "${tmp}/lock")" "versions/vertex=9.9.9\n" "versions/README.md=# versions, fixed\n" "kas/include/base.yml=${BASE_TRUNK}" "foo.txt=t2\n")
distro_commit vertex/main "release: pending 0.0.2 [skip ci]" "versions/vertex=0.0.2\n" >/dev/null
VMAIN=$(head_of "${DISTRO_BARE}" vertex/main)
AMAIN=$(head_of "${DISTRO_BARE}" aggregator/main)
run_sync --from-ref trunk --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^from:               trunk at ${T2}" "from sha"
contains "${log}" "^meta-dsensos pin:   ${M3}" "pin"
contains "${log}" "^ds-tegra-distro:    vertex/main at ${VMAIN}; branch sync/vertex/${T2:0:7}$" "vertex branch name"
contains "${log}" "^                    merge base with ${T2:0:7}: ${T1}" "vertex merge base (the last sync)"
contains "${log}" "^                    trial merge: clean" "versions/ and the base.yml branch name do not count as conflicts"
contains "${log}" "^meta-dsensos:       vertex/main at ${M2}; branch sync/vertex/${T2:0:7}$" "meta branch name"
contains "${log}" "^                    merge base with ${M3:0:7}: ${M2}" "meta merge base"
contains "${log}" "^                    trial merge: clean$" "meta trial merge"
contains "${log}" "^ds-tegra-distro:    aggregator/main at ${AMAIN}; branch sync/aggregator/${T2:0:7}$" "aggregator too"
contains "${log}" "^dry run: nothing pushed, no PR opened" "dry run says so"
check "$(pr_count)" 1 "no new PR"
check "$(heads_of "${DISTRO_BARE}" | grep -c sync/)" 1 "no new distro branch (only the merged one from before)"
check "$(heads_of "${META_BARE}" | grep -c sync/ || true)" 0 "no meta branch"

export GITHUB_STEP_SUMMARY="${tmp}/summary.md"
: > "${GITHUB_STEP_SUMMARY}"
run_sync --from-ref trunk --no-dry-run
check "${rc}" 0 "exits 0"
check "$(pr_count)" 5 "four new PRs"
# vertex: meta PR 2, distro PR 3; aggregator: meta PR 4, distro PR 5
check "$(pr_field 2 repo) $(pr_field 2 base) $(pr_field 2 head) $(pr_field 2 title)" "ds/meta vertex/main sync/vertex/${T2:0:7} Sync trunk ${T2:0:7} into vertex/main" "meta PR"
check "$(pr_field 3 repo) $(pr_field 3 base) $(pr_field 3 head) $(pr_field 3 title)" "ds/distro vertex/main sync/vertex/${T2:0:7} Sync trunk ${T2:0:7} into vertex/main" "distro PR"
check "$(pr_field 4 repo) $(pr_field 5 repo)" "ds/meta ds/distro" "aggregator PRs in the same order"
contains "$(pr_body 2)" "Merge this PR first" "meta body: merges first"
contains "$(pr_body 2)" "pins meta-dsensos \`${M3}\`" "meta body names the pin"
contains "$(pr_body 3)" "Merge the meta-dsensos PR first:\*\* https://github.com/ds/meta/pull/2" "distro body links the meta PR"
contains "$(pr_body 3)" "finish=<this PR's number>" "distro body says how to merge"
MS=$(head_of "${META_BARE}" "sync/vertex/${T2:0:7}")
check "$(git -C "${META_BARE}" rev-parse "${MS}^1") $(git -C "${META_BARE}" rev-parse "${MS}^2")" "${M2} ${M3}" "meta sync branch = meta main + merge of the pin"
check "$(git -C "${META_BARE}" log -1 --format=%s "${MS}")" "Sync trunk ${T2:0:7} into vertex/main (meta-dsensos ${M3:0:7})" "meta merge subject"
DS=$(head_of "${DISTRO_BARE}" "sync/vertex/${T2:0:7}")
check "$(git -C "${DISTRO_BARE}" rev-parse "${DS}^1") $(git -C "${DISTRO_BARE}" rev-parse "${DS}^2")" "${VMAIN} ${T2}" "distro sync branch = main + merge of the trunk"
check "$(show "${DISTRO_BARE}" "${DS}" versions/vertex)" 0.0.2 "vertex/main keeps its pending version"
check "$(show "${DISTRO_BARE}" "${DS}" versions/README.md)" "# versions, fixed" "the trunk's versions/README.md fix reaches vertex/main"
check "$(show "${DISTRO_BARE}" "${DS}" kas/include/base.yml | grep -c 'trunk2')" 0 "vertex/main keeps its meta-dsensos branch name"
check "$(show "${DISTRO_BARE}" "${DS}" kas/include/base.yml | grep -c '^  meta-new:$')" 1 "the trunk's new layer stanza reaches vertex/main"
check "$(python3 "${scripts}/release_git.py" base-branch-get <(show "${DISTRO_BARE}" "${DS}" kas/include/base.yml))" vertex/main "and its meta-dsensos branch name"
check "$(python3 "${scripts}/release_git.py" lock-pin --file <(show "${DISTRO_BARE}" "${DS}" kas/include/base.lock.yml) x)" "${M3}" "the lockfile merged normally"
check "$(show "${DISTRO_BARE}" "${DS}" foo.txt)" t2 "and so did foo.txt"
AS=$(head_of "${DISTRO_BARE}" "sync/aggregator/${T2:0:7}")
check "$(show "${DISTRO_BARE}" "${AS}" versions/vertex) $(python3 "${scripts}/release_git.py" base-branch-get <(show "${DISTRO_BARE}" "${AS}" kas/include/base.yml))" "0.0.1 aggregator/main" "aggregator keeps its copies too"
check "$(head_of "${DISTRO_BARE}" vertex/main) $(head_of "${META_BARE}" vertex/main)" "${VMAIN} ${M2}" "the mains are untouched"
contains "${GITHUB_STEP_SUMMARY}" "^- \*\*vertex\*\*: meta-dsensos https://github.com/ds/meta/pull/2 \(merge first\), ds-tegra-distro https://github.com/ds/distro/pull/3$" "summary line"
unset GITHUB_STEP_SUMMARY
contains "${log}" "^done: 4 PR\(s\): https://github.com/ds/meta/pull/2 https://github.com/ds/distro/pull/3 https://github.com/ds/meta/pull/4 https://github.com/ds/distro/pull/5" "done line lists them"

echo "# a rerun reuses the branches and PRs"
run_sync --from-ref trunk --no-dry-run
check "${rc}" 0 "exits 0"
check "$(pr_count)" 5 "no new PR"
contains "${log}" "^meta-dsensos: sync/vertex/${T2:0:7} exists at ${MS}; not re-pushed" "meta branch reused"
contains "${log}" "^ds-tegra-distro: sync/vertex/${T2:0:7} exists at ${DS}; not re-pushed" "distro branch reused"
check "$(head_of "${DISTRO_BARE}" "sync/vertex/${T2:0:7}")" "${DS}" "branch unchanged"

echo "# finish the meta-dsensos PR, then the ds-tegra-distro one"
run_sync --finish 2 --no-dry-run
check "${rc}" 1 "unapproved meta PR refused"
approve 2
run_sync --finish 2 --no-dry-run
check "${rc}" 0 "exits 0"
MM=$(head_of "${META_BARE}" vertex/main)
check "$(git -C "${META_BARE}" rev-parse "${MM}^2")" "${MS}" "meta vertex/main merged the PR head"
check "$(git -C "${META_BARE}" merge-base --is-ancestor "${M3}" "${MM}" && echo yes || echo no)" yes "meta vertex/main now contains the pin"
contains "${log}" "::notice::sync: meta-dsensos vertex/main is at ${MM}; now finish the ds-tegra-distro PR: dispatch release-sync-product-releases.yml with finish=3" "names the next PR"
check "$(head_of "${DISTRO_BARE}" vertex/main)" "${VMAIN}" "ds-tegra-distro untouched"
approve 3
run_sync --finish 3 --no-dry-run
check "${rc}" 0 "exits 0"
VMAIN2=$(head_of "${DISTRO_BARE}" vertex/main)
check "$(git -C "${DISTRO_BARE}" rev-parse "${VMAIN2}^2")" "${DS}" "vertex/main merged the sync branch"
check "$(show "${DISTRO_BARE}" "${VMAIN2}" versions/vertex) $(show "${DISTRO_BARE}" "${VMAIN2}" foo.txt)" "0.0.2 t2" "pending version kept, trunk change carried"
check "$(git -C "${DISTRO_BARE}" merge-base --is-ancestor "${T2}" "${VMAIN2}" && echo yes || echo no)" yes "the trunk is an ancestor of vertex/main"

echo "# conflicts in both repos: PRs at the trunk commits, marked and listing the files"
distro_commit vertex/main "main only" "foo.txt=main only\n" >/dev/null
meta_commit vertex/main "main layer" "layer.txt=main layer\n" >/dev/null
M4=$(meta_commit trunk "m4" "layer.txt=m4\n")
write_lock "${tmp}/lock" "${M4}"
T3=$(distro_commit trunk "t3" "kas/include/base.lock.yml=$(cat "${tmp}/lock")" "foo.txt=t3\n")
run_sync --from-ref trunk --products vertex --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^                    trial merge: conflicts in: foo.txt$" "distro conflict predicted"
contains "${log}" "^                    trial merge: conflicts in: layer.txt$" "meta conflict predicted"
check "$(pr_count)" 5 "dry run opens nothing"
run_sync --from-ref trunk --products vertex --no-dry-run
check "${rc}" 0 "exits 0 (the PRs carry the conflict)"
check "$(pr_count)" 7 "two PRs"
check "$(pr_field 6 repo) $(pr_field 6 title)" "ds/meta Sync trunk ${T3:0:7} into vertex/main (conflict)" "meta PR marked"
check "$(pr_field 7 repo) $(pr_field 7 title)" "ds/distro Sync trunk ${T3:0:7} into vertex/main (conflict)" "distro PR marked"
check "$(head_of "${META_BARE}" "sync/vertex/${T3:0:7}")" "${M4}" "meta branch is the pin itself"
check "$(head_of "${DISTRO_BARE}" "sync/vertex/${T3:0:7}")" "${T3}" "distro branch is the trunk commit itself"
contains "$(pr_body 6)" "^- \`layer.txt\`$" "meta body lists the file"
contains "$(pr_body 7)" "^- \`foo.txt\`$" "distro body lists the file"
contains "$(pr_body 7)" "git checkout origin/vertex/main -- versions/<product>\` and set the meta-dsensos branch in \`kas/include/base.yml\` back to \`vertex/main\`" "distro body says how to restore the kept files"
contains "${log}" "pushing the trunk commit itself as sync/vertex/${T3:0:7}" "log says so"

echo "# products: a bad name, and the aggregator alone"
run_sync --from-ref trunk --products nope --no-dry-run
check "${rc}" 1 "unknown product exits 1"
contains "${log}" "product 'nope' is not in release/config.json" "names it"
run_sync --from-ref no-such-ref --no-dry-run
check "${rc}" 1 "unknown ref exits 1"
run_sync --no-dry-run
check "${rc}" 1 "no from_ref exits 1"

echo "# no App token"
T4=$(distro_commit trunk "t4" "foo.txt=t4\n")
TOKEN_KIND=github run_sync --from-ref trunk --products aggregator --dry-run
check "${rc}" 0 "dry run works"
contains "${log}" "^meta-dsensos:       not checked \(no App token" "meta half not checked"
contains "${log}" "^ds-tegra-distro:    aggregator/main at .*; branch sync/aggregator/${T4:0:7}" "distro half planned"
TOKEN_KIND=github run_sync --from-ref trunk --products aggregator --no-dry-run
check "${rc}" 1 "real run exits 1"
contains "${log}" "::error::sync: aggregator: no App token" "names the problem"
contains "${log}" "git checkout -b sync/aggregator/${T4:0:7} origin/aggregator/main && git merge --no-ff --no-commit ${T4}; python3 scripts-release/release_git.py merge-keep-target aggregator/main" "names the manual merge"
check "$(exists "$(head_of "${DISTRO_BARE}" "sync/aggregator/${T4:0:7}")")" no "nothing pushed"
check "$(pr_count)" 7 "no PR"
TOKEN_KIND=github run_sync --finish 5 --no-dry-run
check "${rc}" 1 "finish without the App exits 1"
contains "${log}" "not ready to merge|no App token" "and says why"

echo "# a product main as the trunk: vertex/main into aggregator/main"
VTIP=$(head_of "${DISTRO_BARE}" vertex/main)
run_sync --from-ref vertex/main --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "::notice::sync: vertex: vertex/main is the sync source; skipped" "the source product is skipped"
contains "${log}" "^ds-tegra-distro:    aggregator/main at .*; branch sync/aggregator/${VTIP:0:7}" "aggregator gets a sync from vertex/main"
lacks "${log}" "branch sync/vertex/" "no sync branch for vertex itself"

echo
if [ "${fails}" = 0 ]; then echo "test_sync: all passed"; else echo "test_sync: ${fails} failure(s)"; exit 1; fi
