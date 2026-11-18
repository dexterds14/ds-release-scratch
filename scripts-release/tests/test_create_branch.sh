#!/usr/bin/env bash
set -euo pipefail
# Tests for scripts-release/create-branch.sh (ENG-1933, ENG-2110): one temp
# bare repo stands in for origin, gh is the stub in tests/stubs-tree. No
# network, no git identity (the opening commit must come out as the bot).
#   bash scripts-release/tests/test_create_branch.sh
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
scripts=$(dirname "${here}")
tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT
fails=0
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3 (got '$1', want '$2')"; fails=$((fails + 1)); fi; }
contains() { if grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (no match for '$2' in $1)"; sed 's/^/     | /' "$1"; fails=$((fails + 1)); fi; }
lacks() { if ! grep -q -E -- "$2" "$1"; then echo "ok   $3"; else echo "FAIL $3 (unexpected match for '$2' in $1)"; fails=$((fails + 1)); fi; }
exists() { if [ -n "$1" ]; then echo yes; else echo no; fi; }
BOT='ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>'

. "${here}/stubs-tree/fixture.sh"
tree_fixture_init
MAIN_TIP=$(head_of "${BARE}" main)
tag_origin v1.0.0 "${MAIN_TIP}"

# run_create <args...> -> $rc, $log
run_create() {
  log="${tmp}/log"
  rc=0
  (cd "${REPO}" && "${scripts}/create-branch.sh" "$@" > "${log}" 2>&1) || rc=$?
}
show() { git -C "${BARE}" show "$1:$2"; }
subject_of() { git -C "${BARE}" log -1 --format=%s "$1"; }
author_of() { git -C "${BARE}" log -1 --format='%an <%ae>' "$1"; }
files_of() { git -C "${BARE}" diff-tree --no-commit-id --name-only -r "$1" | LC_ALL=C sort | tr '\n' ' '; }
parent_of() { git -C "${BARE}" rev-parse "$1^"; }

echo "# refusals"
run_create
check "${rc}" 1 "no arguments exits 1"
contains "${log}" "::error::create-branch: --version is required" "names the missing argument"
run_create --seed-from main --no-dry-run
check "${rc}" 1 "--seed-from without --version exits 1"
run_create --version 1.2 --no-dry-run
check "${rc}" 1 "a version that is not N.M.P exits 1"
contains "${log}" "must be N.M.P" "names the shape"
run_create --version 1.2.3 --no-dry-run
check "${rc}" 1 "a missing tag exits 1"
contains "${log}" "::error::create-branch: tag v1.2.3 does not exist" "names the tag"
run_create --version 1.0.0 --kind major --no-dry-run
check "${rc}" 1 "an unknown kind exits 1"
contains "${log}" "--kind must be auto, minor or patch" "names the kinds"
run_create --version 1.0.0 --product other --no-dry-run
check "${rc}" 1 "an unconfigured product exits 1"
contains "${log}" "product 'other' is not in release/config.json \(ds\)" "names the configured product"
run_create --version 1.0.0 --bogus
check "${rc}" 2 "an unknown argument is a usage error"
check "$(heads_of "${BARE}" | tr '\n' ' ')" "main " "nothing was created"

echo "# dry run"
run_create --version 1.0.0 --dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "^== create-branch plan \(dry_run=true, token=app\)" "plan header"
contains "${log}" "^kind:               minor line \(--kind auto\)" "auto picks minor for N.0.0"
contains "${log}" "^product:            ds" "the configured product is implied"
contains "${log}" "^line:               1.x.x" "the line"
contains "${log}" "^from:               v1.0.0 \(${MAIN_TIP}\)" "the tag and its sha"
contains "${log}" "^parent line:        main" "the parent"
contains "${log}" "^versions/ds:   0.0.1 -> 1.1.0 \(minor line\)" "the pending version"
contains "${log}" "^\+1.1.0" "the opening commit diff"
contains "${log}" "^dry run: nothing created" "dry run says so"
check "$(heads_of "${BARE}" | tr '\n' ' ')" "main " "nothing was created"

echo "# line mode: v1.0.0 opens 1.x.x with one opening commit as the bot"
run_create --version 1.0.0 --no-dry-run
check "${rc}" 0 "exits 0"
TIP=$(head_of "${BARE}" 1.x.x)
check "$(exists "${TIP}")" yes "1.x.x exists"
check "$(parent_of "${TIP}")" "${MAIN_TIP}" "starts at the tag"
check "$(files_of "${TIP}")" "versions/ds " "the opening commit touches exactly versions/ds"
check "$(subject_of "${TIP}")" "release: open 1.x.x from v1.0.0 [skip ci]" "subject"
check "$(author_of "${TIP}")" "${BOT}" "author is the bot (no GIT_* in the environment)"
check "$(show "${TIP}" versions/ds)" 1.1.0 "pending 1.1.0"
check "$(show "${TIP}" "${ENV_FILE}")" "$(show "${MAIN_TIP}" "${ENV_FILE}")" "the bookkeeping file is untouched"
check "$(head_of "${BARE}" main)" "${MAIN_TIP}" "main untouched"
contains "${log}" "^created 1.x.x at ${TIP} \(parent ${MAIN_TIP}\)" "reports the new tip"
contains "${log}" "^done: 1.x.x open" "done line"
run_create --version 1.0.0 --no-dry-run
check "${rc}" 1 "the existing line is refused"
contains "${log}" "::error::create-branch: origin already has branch 1.x.x" "names the branch"
check "$(head_of "${BARE}" 1.x.x)" "${TIP}" "and left alone"

echo "# a patch line from a tag on the minor line: parent 1.x.x"
LINE_TIP=$(repo_commit 1.x.x "line fix" "foo.txt=line fix\n")
tag_origin v1.1.0 "${LINE_TIP}"
run_create --version 1.1.0 --no-dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "^kind:               patch line \(--kind auto\)" "auto picks patch for N.M.0"
contains "${log}" "^parent line:        1.x.x" "the minor line is the parent"
TIP=$(head_of "${BARE}" 1.1.x)
check "$(parent_of "${TIP}")" "${LINE_TIP}" "starts at the tag"
check "$(show "${TIP}" versions/ds)" 1.1.1 "pending 1.1.1"
check "$(subject_of "${TIP}")" "release: open 1.1.x from v1.1.0 [skip ci]" "subject"

echo "# any tag opens either line (ENG-1934): v0.0.1 with --kind minor and auto"
tag_origin v0.0.1 "${MAIN_TIP}"
run_create --version 0.0.1 --kind minor --no-dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "^kind:               minor line \(--kind minor\)" "forced minor"
check "$(show "$(head_of "${BARE}" 0.x.x)" versions/ds)" 0.1.0 "0.x.x pending 0.1.0"
run_create --version 0.0.1 --no-dry-run
check "${rc}" 0 "exits 0"
contains "${log}" "^kind:               patch line \(--kind auto\)" "auto is patch for P > 0"
TIP=$(head_of "${BARE}" 0.0.x)
check "$(show "${TIP}" versions/ds)" 0.0.2 "0.0.x pending 0.0.2"
check "$(subject_of "${TIP}")" "release: open 0.0.x from v0.0.1 [skip ci]" "subject"
run_create --version 0.0.1 --kind patch --no-dry-run
check "${rc}" 1 "the derived line existing is still refused"
contains "${log}" "already has branch 0.0.x" "names the branch"

echo "# --seed-from: the line opens at the ref when the tag is an ancestor of it"
LATER=$(repo_commit main "after the tag" "foo.txt=after the tag\n")
tag_origin v2.0.0 "${LATER}"
SEED=$(REPO_COMMIT_START="${LATER}" repo_commit seed "seed work" "foo.txt=seed branch\n")
run_create --version 2.0.0 --seed-from seed --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^from:               v2.0.0 \(${LATER:0:7}\), seeded from seed \(${SEED}\)" "the plan names both"
contains "${log}" "^dry run: nothing created" "dry run says so"
check "$(exists "$(head_of "${BARE}" 2.x.x)")" no "nothing created"
run_create --version 2.0.0 --seed-from seed --no-dry-run
check "${rc}" 0 "exits 0"
TIP=$(head_of "${BARE}" 2.x.x)
check "$(parent_of "${TIP}")" "${SEED}" "starts at the seed ref"
check "$(files_of "${TIP}")" "versions/ds " "the opening commit touches exactly versions/ds"
check "$(subject_of "${TIP}")" "release: open 2.x.x from v2.0.0 (seeded from seed) [skip ci]" "subject says seeded"
check "$(show "${TIP}" versions/ds)" 2.1.0 "pending 2.1.0"
check "$(show "${TIP}" foo.txt)" "seed branch" "carries the seed's tree"
run_create --version 2.0.0 --kind patch --seed-from "${LATER}" --no-dry-run
check "${rc}" 0 "a sha is accepted as the seed ref"
TIP=$(head_of "${BARE}" 2.0.x)
check "$(parent_of "${TIP}")" "${LATER}" "starts at that sha"
check "$(show "${TIP}" versions/ds)" 2.0.1 "pending 2.0.1"

echo "# --seed-from refusals"
FIRST=$(git -C "${BARE}" rev-list --max-parents=0 main)
OTHER=$(REPO_COMMIT_START="${FIRST}" repo_commit other "elsewhere" "foo.txt=elsewhere\n")
tag_origin v3.0.0 "$(head_of "${BARE}" main)"
run_create --version 3.0.0 --seed-from other --no-dry-run
check "${rc}" 1 "a ref the tag is not an ancestor of exits 1"
contains "${log}" "::error::create-branch: tag v3.0.0 \(.*\) is not an ancestor of --seed-from other \(${OTHER}\)" "names both"
check "$(exists "$(head_of "${BARE}" 3.x.x)")" no "nothing created"
run_create --version 3.0.0 --seed-from no-such-ref --no-dry-run
check "${rc}" 1 "an unknown ref exits 1"
contains "${log}" "seed ref 'no-such-ref' does not resolve to a commit on origin" "names the ref"
check "$(exists "$(head_of "${BARE}" 3.x.x)")" no "nothing created"
# A commit object only this checkout has: a valid sha, but on no origin ref.
LOCAL=$(tgit -C "${REPO}" commit-tree "$(head_of "${BARE}" main)^{tree}" -p "$(head_of "${BARE}" main)" -m "never pushed")
run_create --version 3.0.0 --seed-from "${LOCAL}" --no-dry-run
check "${rc}" 1 "a sha that no origin ref contains exits 1"
contains "${log}" "seed ref '${LOCAL}' does not resolve to a commit on origin" "names the sha"
check "$(exists "$(head_of "${BARE}" 3.x.x)")" no "nothing created"
check "$(head_of "${BARE}" other)" "${OTHER}" "the other branch is untouched"

echo "# no App token: a dry run works, a real run stops with the manual command"
TOKEN_KIND=github run_create --version 3.0.0 --dry-run
check "${rc}" 0 "dry run exits 0"
contains "${log}" "^dry run: nothing created" "dry run says so"
TOKEN_KIND=github run_create --version 3.0.0 --no-dry-run
check "${rc}" 1 "real run exits 1"
contains "${log}" "::error::create-branch: no App token \(vars.DS_RELEASE_APP_CLIENT_ID unset\)" "names the problem"
contains "${log}" "::error::  git fetch origin && git checkout -b 3.x.x .* && echo 3.1.0 > versions/ds && git add versions/ && git commit -m 'release: open 3.x.x from v3.0.0 \[skip ci\]' && git push origin 3.x.x" "names the manual command"
check "$(exists "$(head_of "${BARE}" 3.x.x)")" no "nothing created"

echo
if [ "${fails}" = 0 ]; then echo "test_create_branch: all passed"; else echo "test_create_branch: ${fails} failure(s)"; exit 1; fi
