#!/usr/bin/env python3
"""Git and GitHub helpers shared by create-branch.sh, merge-up.sh, abandon.sh and promote.sh (ENG-1933, ENG-1934, ENG-1935, ENG-2110).

    release_git.py merge-keep-target [--git-dir DIR] [--root ROOT]
        inside an uncommitted merge: the target keeps its pending-version files
        (versions/<product>, never versions/README.md, which merges like any doc)
        and its copy of every bookkeeping file (release/config.json
        `bookkeeping_files`) that exists in HEAD, via `git checkout HEAD --`, so
        those paths never conflict. The target is HEAD, the side checked out: run it
        with the parent checked out and the child merged in, never the other way round.
        What merge-up runs, and what a human runs by hand.
    release_git.py line-plan <product> <version> [--kind auto|minor|patch] [--root ROOT]
        JSON {line, kind, level, pending, tag} for the line cut from tag vN.M.P:
        minor -> N.x.x pending N.(M+1).0; patch -> N.M.x pending N.M.(P+1).
        auto (the default) is minor for N.0.0 and patch for anything else.
        With `product_prefix: true` the line is <product>/N.x.x and the tag <product>/vN.M.P
    release_git.py promote-plan <line> <pending> [--bump auto|major|minor|patch] [--root DIR]
        JSON {product, line, kind, version, tag, level, next_version} for promoting <pending>
        on <line>; exit 1 when the version does not fit the line or the bump would leave
        a pending version that does not fit it (auto: major on main, minor on N.x.x,
        patch on N.M.x)
    release_git.py unmerged <base> <head> [--git-dir DIR] [--root ROOT]
        the commits in <base>..<head> that are not line bookkeeping (versions/ and the
        bookkeeping files only), "<sha> <subject>" per line; exit 1 when there are any
    release_git.py versions-only <sha> [--git-dir DIR]
        exit 0 when the commit touches only versions/ (never for a merge commit)
    release_git.py walk-versions-only <sha> [--git-dir DIR] [--max N]
        <sha>, then its first parents while the commit is versions-only, one per line
    release_git.py first-run [FILE]
        from `gh run list --json databaseId,number` output: prints "<databaseId> <number>"
        of the first run, exit 1 when the list is empty
    release_git.py latest-pr FILE
        from `gh pr list --state all --json number,state,url`: "<number> <state>
        <url>" of the most recent PR (the highest number); nothing when it lists none
    release_git.py person-no --state STATE [--issue FILE] [--comments FILE] [--reviews FILE]
                             [--release-app LOGIN]
        why the last PR of a merge-up/<child>/<sha7> head makes that branch dead
        for merge-up.sh: with --state CLOSED, "<login> closed it without merging",
        unless the bot closed it as superseded (--issue, the REST issue: closed_by
        is of type Bot and its login is --release-app, the release App's login,
        GIT_BOT_NAME; without --release-app every close is a no; --comments, its
        issue comments: that account left a comment that starts "Superseded by ").
        With --state OPEN and --reviews (its REST reviews, ENG-2235): "<logins>
        requested changes" when the latest review of a person asks for changes,
        which stops a bot finish. Prints nothing when it is not a no
    release_git.py checks-state FILE --head SHA --required a,b
        from `gh pr view --json state,headRefOid,statusCheckRollup` (ENG-2235): one
        word, green (every required check succeeded on that head), red, pending,
        moved (the head is not SHA) or closed, then the reasons. Only the most
        recent run of each check counts (latest_runs), here and in pr-check: a run
        that a newer run of the same check superseded (a cancelled one included)
        is not read
    release_git.py check-sources --required a,b --sources FILE --head-ref BRANCH --head SHA
                                 --pr N --base BRANCH --repo OWNER/NAME
                                 [--check-runs FILE --actions-runs FILE] [--jobs-dir DIR]
                                 [--head-prs FILE --events FILE] [--release-app LOGIN --other-events DIR]
        where each required check of a bot-finished PR's head came from (ENG-2235),
        by auto_finish.check_sources: "green", "pending" or "red", then one line
        per reason. --sources: merge_up.finish.check_sources; the other files are
        the REST answers auto_finish.py ready reads; --pr, --base and --repo: the
        PR, its base and its repository, whose own runs alone vouch for a workflow
        source (auto_finish.this_prs_run); --head-prs and --events: every PR of
        the head branch and the PR's issue events; --release-app and
        --other-events: the release App's login and <number>.json, the issue
        events of each PR `auto_finish.py same-base-prs` names (another PR of the
        branch with another base, one into this base that the release App did
        not open or whose base changed, or a changed base of this PR, is red:
        auto_finish.head_branch_prs) (absent check runs, Actions runs, PRs or events: they could not be read,
        which is pending). red: a newest check run
        of a required name failed, or came from somewhere other than its source;
        pending: one is missing, still running or could not be read
    release_git.py siblings FILE --child C --skip BRANCH
        FILE: "<number> <state> <head> <url>" lines, the merge-up PRs of a parent
        (`gh pr list --state all`). For every merge-up/<child>/<sha7> head but
        BRANCH, the PR that speaks for it (latest_pr), as a line of the same
        shape, newest first: the PRs a new merge-up PR of <child> supersedes,
        whose people a bot finish asks first (ENG-2235)
    release_git.py rules-would-resolve REPORT --listed FILE
        exit 0 when the merge rules' report (resolve-conflicts --out DIR, its
        report.json) shows that they would resolve every file of FILE (one
        path per line) once they act: each is a report file the rules would
        resolve whole (not never_auto, not refused). Exit 1 otherwise (ENG-2235)
    release_git.py merge-answer BODY --stderr FILE --exit N
        the answer of `gh api -X PUT repos/<repo>/pulls/<n>/merge -f sha=... -f
        merge_method=merge` (stdout, stderr, exit status; ENG-2235): "merged <sha>",
        exit 0, or one line quoting GitHub's message ("GitHub refused the merge
        (HTTP 405: ...)" for 405, 409 and 422, "the merge call failed (...)"
        otherwise), exit 1
    release_git.py pr-check [FILE] [--reviews FILE]
        from `gh pr view --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits`:
        exit 0 when the PR is approved by someone other than the last pusher,
        mergeStateStatus is CLEAN and no check failed; else every reason, exit 1.
        Only the most recent run of each check counts (latest_runs): a run that a
        newer run of the same check superseded (a cancelled one included) is not read.
        --reviews (what merge-up.sh passes, ENG-2234): the PR's reviews from the
        REST API (`gh api --paginate repos/<repo>/pulls/<n>/reviews`), read instead
        of the view's own list. Only a review whose user.type is "User" counts, so
        an approval by github-actions, Copilot or any App never finishes a PR.
        Without it the view's reviews count, the known bot logins aside.
        --strict (merge_up.finish.strict; needs --reviews, and headRefOid in the
        view): an approval counts only when its commit_id is the PR head; the
        approver must not have pushed to the branch; and every check named in
        --required a,b must be reported on the head and successful. Who pushed:
        --activity FILE, the repository activity of the branch (`gh api
        --paginate repos/<repo>/activity?ref=refs/heads/<branch>`), bots aside.
        When that could not be read (--activity absent), --commits FILE instead:
        the REST commit objects of the commits above the bot's commit, of which
        every author and committer counts as a pusher; the output says so.
        In both modes the check runs of the workflow "Merge-up review trigger"
        (release-merge-up-review.yml, ENG-2235) are not read: that run does
        nothing, and a cancelled or queued one must not block a finish. An
        UNSTABLE mergeStateStatus is no reason when those runs are the only
        unfinished or unsuccessful ones
    release_git.py markers --base SHA --head SHA [--git-dir DIR]
        the files <head> changes against its merge base with <base> that still
        hold a conflict marker line (<<<<<<< , ||||||| or >>>>>>> at the start of
        a line), one per line, exit 1; exit 0 when there is none. The rule the
        pr-checks step on a merge-up/** head applies in bash and git, so a
        conflict PR cannot be merged from the GitHub UI with markers in it
        (ENG-2234); tests/test_finish.py checks that the two agree
    release_git.py bookkeeping --parent REF --head SHA [--git-dir DIR] [--root ROOT]
        the bookkeeping files a merge-up PR at <head> changes against its merge
        base with the parent line at REF, one per line, exit 1; exit 0 when there
        is none. The files are the set merge-keep-target keeps with the parent
        checked out: each versions/<product> of REF (never versions/README.md) and
        each `bookkeeping_files` entry REF has. A PR that changes one would carry
        the child line's pending version or SDK line into the parent, so the
        finish refuses it, and so does the pr-checks step on a merge-up/** head,
        in bash and git (ENG-2234); tests/test_finish.py checks that the two agree
    release_git.py branch-origin <head> --parent REF [--like SHA] [--bot-email E] [--git-dir DIR]
        where a merge-up/<child>/<sha7> branch at <head> comes from: its first
        commit above the parent line (the last one on the first-parent chain that
        REF does not contain). Prints "ours <sha>" when that commit is the bot's
        merge, else "foreign <sha>" and why on a second line. --like: the commit
        this run just made; the first commit must have the same tree, the same two
        parents and the same committer. --bot-email alone (a finish, which
        computes no merge): a two-parent commit committed under that address
    release_git.py resolve-conflicts --git-dir DIR --out DIR [--skip PATH]... [--check] [--root DIR]
        the merge-up rules of merge_rules.py (ENG-2234) on the unmerged paths of
        the uncommitted merge in --git-dir. This repo runs one rule, splice
        (adjacent edits), in the mode DS_MERGE_UP_AUTOMATION gives it. No rule
        writes a file on merge_up.never_auto; what the rule would have written
        there is kept as a patch for a PR comment. A rule that acts writes its
        result to the work tree and stages a file with no conflict left; --check
        (a dry run) evaluates and writes nothing. --skip names a path to leave
        alone. Prints one line per finding and writes to --out: report.json,
        log.txt, body.md (the PR body section), comment.md (the candidate
        patches, only when there is one), trailers.txt (Merge-Up-Rule lines for
        the commit) and remaining.txt (the paths still in conflict, skipped ones
        included). Exit 1 only when the evaluation itself failed, and then
        nothing was written
    release_git.py merge-up-config [--root DIR] [--key a.b]
        the "merge_up" block of release/config.json (ENG-2234), validated, with
        the defaults for anything absent; --key prints one value by dotted path
        (finish.strict), a string bare and anything else as JSON. The root
        defaults to the checkout this script is in: the ref a run was
        dispatched on, which is the default branch for every automatic run
    release_git.py automation [--value V] [--rule splice] [--action bot-finish] [--actions]
        DS_MERGE_UP_AUTOMATION (the environment variable, or --value) parsed:
        two lines for the run summary, or with --rule that rule's mode
        (off, shadow or act), with --action that action's mode, with --actions
        the summary line of the actions ("bot finish: ..."). Exit 1 naming the
        token that is neither a rule nor an action

--root (default: the checkout this script lives in) names the repo whose
release/config.json supplies the products, the prefix mode and the
bookkeeping files. Standard library only (runs on the CI runners and in
the tests).
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lines  # noqa: E402  (sibling module: parse_line)
import version  # noqa: E402  (sibling module: bump(), config() and SEMVER_CORE)

NUM = r'(0|[1-9][0-9]*)'
VERSIONS_DIR = 'versions/'
VERSIONS_README = 'versions/README.md'
LINE_KINDS = ('auto', 'minor', 'patch')
BUMP_LEVELS = ('auto',) + version.LEVELS
AUTO_LEVEL = {'main': 'major', 'minor': 'minor', 'patch': 'patch'}
FAILED_CONCLUSIONS = {'FAILURE', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'}
FAILED_STATES = {'FAILURE', 'ERROR'}
# The Actions variable that switches the merge-up rules (ENG-2234), and the
# rules it can name in this repo. Checks that refuse are switched in
# release/config.json. The OS distro repo has two more rules (OTHER_REPO_RULES):
# they have nothing to act on here, and naming one is an error, like any
# other token that is not a rule of this repo.
AUTOMATION_VAR = 'DS_MERGE_UP_AUTOMATION'
MERGE_UP_RULES = ('splice',)
OTHER_REPO_RULES = ('lock-pin', 'pins')
# Words of the same variable that are not rules (ENG-2235, 2c): bot-finish lets the bot merge a
# PR the merge rules resolved, with no approval, once its required checks succeed. A rule decides
# what a merge holds; an action decides who merges it. See action_mode.
MERGE_UP_ACTIONS = ('bot-finish',)
# The rules whose PR bot-finish merges (a "(resolved by rule)" PR): here the one rule, splice.
BOT_FINISH_RULES = ('splice',)
# The `name:` of .github/workflows/release-merge-up-review.yml (ENG-2235), a no-op that
# runs on every review of a merge-up PR; pr_check does not read its check runs.
REVIEW_TRIGGER_WORKFLOW = 'Merge-up review trigger'


class Error(Exception):
    pass


def git(git_dir, *args):
    r = subprocess.run(['git', '-C', git_dir, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise Error(f'git {" ".join(args)}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


def load_config(root):
    """release/config.json under root, as version.config gives it (an Error when invalid)."""
    try:
        return version.config(os.path.abspath(root))
    except version.CheckError as e:
        raise Error(str(e))


def tag_name(product, ver, prefix=True):
    """The release tag of a version: <product>/vX.Y.Z when prefixed, vX.Y.Z otherwise."""
    return f'{product}/v{ver}' if prefix else f'v{ver}'


# --- merge-up: what the target keeps ------------------------------------------

def keep_target_versions(git_dir):
    """Inside an uncommitted merge: restore the target's (HEAD's) pending-version files.

    Only the per-product files: versions/README.md is documentation and merges like any
    other file, so a README change reaches every line. A versions file that exists only on
    the incoming side is left as merged (the target has no copy to prefer). Returns the paths
    restored."""
    paths = [p for p in git(git_dir, 'ls-tree', '--name-only', 'HEAD', 'versions/').split()
             if p != VERSIONS_README]
    if paths:
        git(git_dir, 'checkout', 'HEAD', '--', *paths)
    return paths


def keep_target_bookkeeping(git_dir, bookkeeping_files):
    """Inside an uncommitted merge: restore the target's copy of each bookkeeping file it has.

    `git checkout HEAD -- <path>` writes HEAD's version and stages it, which also resolves a
    conflict on that path, so a bookkeeping file never conflicts. A file the target does not
    have is left as merged. Returns the paths restored."""
    kept = []
    for path in bookkeeping_files:
        if git(git_dir, 'ls-tree', '--name-only', 'HEAD', '--', path).strip() == path:
            git(git_dir, 'checkout', 'HEAD', '--', path)
            kept.append(path)
    return kept


def merge_keep_target(git_dir, bookkeeping_files=()):
    """keep_target_versions, then keep_target_bookkeeping. Returns every path restored."""
    return keep_target_versions(git_dir) + keep_target_bookkeeping(git_dir, bookkeeping_files)


# --- versions ----------------------------------------------------------------

def line_plan(product, ver, kind='auto', prefix=True):
    """Where a line cut from v<ver> lives and what it opens with.

    Any N.M.P tag can open either line (ENG-1934: e.g. both 1.x.x and 1.0.x
    can be cut from v1.0.0). kind 'auto' keeps the original derivation,
    N.0.0 -> the minor line, anything else -> the patch line; 'minor' and
    'patch' force one.
    """
    m = version.SEMVER_CORE.fullmatch(ver)
    if not m:
        raise Error(f'version {ver!r} must be N.M.P (the tag {tag_name(product, "N.M.P", prefix)} the line is cut from)')
    if kind not in LINE_KINDS:
        raise Error(f'kind must be one of {LINE_KINDS}, got {kind!r}')
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    if kind == 'auto':
        kind = 'minor' if (minor == '0' and patch == '0') else 'patch'
    tag = tag_name(product, ver, prefix)
    if kind == 'minor':
        return {'line': lines.line_name(product, f'{major}.x.x', prefix), 'kind': 'minor', 'level': 'minor',
                'pending': version.bump(ver, 'minor'), 'tag': tag}
    return {'line': lines.line_name(product, f'{major}.{minor}.x', prefix), 'kind': 'patch', 'level': 'patch',
            'pending': version.bump(ver, 'patch'), 'tag': tag}


def fits_line(parsed, ver):
    """True when a pending version has the shape the line can promote: anything on main,
    N.M.0 on N.x.x, N.M.P on N.M.x."""
    if not version.SEMVER_CORE.fullmatch(ver):
        return False
    kind, prefix = parsed['kind'], parsed['version_prefix']
    if kind == 'main':
        return True
    if not ver.startswith(prefix):
        return False
    rest = ver[len(prefix):]
    if kind == 'minor':
        return bool(re.fullmatch(NUM + r'\.0', rest))
    if kind == 'patch':
        return bool(re.fullmatch(NUM, rest))
    return False


def line_shape(parsed):
    kind, prefix = parsed['kind'], parsed['version_prefix']
    return {'main': 'X.Y.Z', 'minor': f'{prefix}M.0', 'patch': f'{prefix}P'}.get(kind, '?')


def promote_plan(line, pending, products, bump='auto', prefix=True):
    """What a promote of <pending> on <line> tags and leaves behind (ENG-1934)."""
    parsed = lines.parse_line(line, products, prefix)
    name = lines.strip_ref(line)
    if parsed['kind'] == 'none':
        raise Error(f'{name} is not a release line ({", ".join(products)})')
    if bump not in BUMP_LEVELS:
        raise Error(f'bump must be one of {BUMP_LEVELS}, got {bump!r}')
    if not version.SEMVER_CORE.fullmatch(pending):
        raise Error(f'pending version {pending!r} is not X.Y.Z')
    if not fits_line(parsed, pending):
        raise Error(f'pending version {pending} does not fit {name} (a {parsed["kind"]} line promotes {line_shape(parsed)})')
    level = AUTO_LEVEL[parsed['kind']] if bump == 'auto' else bump
    nxt = version.bump(pending, level)
    if not fits_line(parsed, nxt):
        raise Error(f'bump {level} after {pending} leaves pending {nxt}, which does not fit {name} '
                    f'({line_shape(parsed)}); use bump={AUTO_LEVEL[parsed["kind"]]} (auto) on a {parsed["kind"]} line')
    return {'product': parsed['product'], 'line': name, 'kind': parsed['kind'], 'version': pending,
            'tag': tag_name(parsed['product'], pending, prefix), 'level': level, 'next_version': nxt}


# --- commits -----------------------------------------------------------------

def is_versions_only(git_dir, sha):
    """True when the commit has one parent and its diff touches only versions/."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) for f in files)


def is_bookkeeping(git_dir, sha, bookkeeping_files=()):
    """True when the commit has one parent and touches only versions/ and the bookkeeping
    files (a line's opening commit or a promote's bump): never unmerged work."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) or f in bookkeeping_files for f in files)


def unmerged(git_dir, base, head, bookkeeping_files=()):
    """[(sha, subject)] of the commits in base..head that are not bookkeeping, oldest first."""
    out = []
    text = git(git_dir, 'rev-list', '--topo-order', '--reverse', '--format=%H %s', '--no-commit-header', f'{base}..{head}')
    for line in text.splitlines():
        sha, _, subject = line.partition(' ')
        if sha and not is_bookkeeping(git_dir, sha, bookkeeping_files):
            out.append((sha, subject))
    return out


def walk_versions_only(git_dir, sha, limit=5):
    cur = git(git_dir, 'rev-parse', '--verify', f'{sha}^{{commit}}').strip()
    out = [cur]
    while len(out) <= limit and is_versions_only(git_dir, cur):
        cur = git(git_dir, 'rev-parse', '--verify', f'{cur}^1').strip()
        out.append(cur)
    return out


# --- gh JSON -----------------------------------------------------------------

def first_run(doc):
    if not isinstance(doc, list) or not doc:
        return None
    r = doc[0]
    return f'{r.get("databaseId", "")} {r.get("number", "")}'.strip()


def is_bot_login(login):
    """GitHub Apps review as <name>[bot]; gh sometimes drops the suffix for github-actions."""
    return login.endswith('[bot]') or login in ('github-actions', 'copilot-pull-request-reviewer')


REVIEW_STATES = ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')


def read_json_stream(path):
    """Every JSON document in a file, lists flattened: what `gh api --paginate`
    prints for a list endpoint, whether as one array, one array per page or
    one object per line."""
    with open(path) as f:
        text = f.read()
    out, pos, dec = [], 0, json.JSONDecoder()
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return out
        doc, pos = dec.raw_decode(text, pos)
        out.extend(doc if isinstance(doc, list) else [doc])


def person_reviews(reviews):
    """({login: review}, [bot logins that approved]) from the REST reviews of a PR,
    oldest first: the last APPROVED, CHANGES_REQUESTED or DISMISSED review of
    every person. A review counts only when user.type is "User": github-actions,
    Copilot and every App report "Bot", whatever their login looks like."""
    latest, bots = {}, set()
    for rv in reviews:
        user = rv.get('user') or {}
        login, state = user.get('login'), rv.get('state')
        if not login or state not in REVIEW_STATES:
            continue
        if user.get('type') != 'User':
            if state == 'APPROVED':
                bots.add(login)
            continue
        latest[login] = rv
    return latest, sorted(bots)


def check_names(c):
    return c.get('name') or c.get('context') or ''


# gh prints a time GitHub left empty (a check run still queued or running) as Go's zero time.
ZERO_TIME = '0001-01-01T00:00:00Z'


def _when(c, key):
    v = c.get(key) or ''
    return '' if v == ZERO_TIME else v


def check_key(c):
    """Which check a statusCheckRollup entry is one run of: a commit status by its context,
    a check run by its workflow and name (two workflows may both have a job `build`)."""
    if c.get('__typename') == 'StatusContext' or ('context' in c and 'name' not in c):
        return ('status', c.get('context') or '')
    return ('check', c.get('workflowName') or '', c.get('name') or '')


def latest_runs(rollup):
    """The most recent run of each check in a `gh pr view --json statusCheckRollup` list, in the
    order the checks first appear.

    The rollup holds one check run per check suite, so a check that ran in two workflow runs
    is there twice. Measured on dexterds14/ds-release-scratch#25 (2026-11-18): reopening the
    PR started a second pr-checks run whose concurrency group cancelled the first run's
    pin-propagation-safety job; the rollup then listed that job CANCELLED next to the second
    run's SUCCESS, and GitHub read mergeStateStatus CLEAN. A rerun of a workflow run replaces
    its own entry (same run, new job), it does not add one.

    Most recent is by completedAt, GitHub's own measure: the check-runs REST API's default
    filter=latest "filters check runs by their completed_at timestamp". A run that has not
    completed is newer than every completed one, so a run still going is never hidden behind an
    older result (it reads as pending). Ties fall to startedAt (createdAt for a status), then
    to the later position in the list. The rollup order is not documented (#25 interleaves the
    two runs job by job), so position is only the last resort. A commit status keeps only its
    latest state per context on GitHub's side; it goes through the same rule."""
    best = {}   # key -> (rank, entry); dicts keep the order keys were first seen
    for i, c in enumerate(rollup or []):
        key = check_key(c)
        done = _when(c, 'completedAt')
        started = _when(c, 'startedAt') or _when(c, 'createdAt')
        rank = (0 if done or key[0] == 'status' else 1, done, started, i)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, c)
    return [c for _, c in best.values()]


REFUSALS = ('405', '409', '422')


def merge_answer(body, err, status):
    """(merged sha | None, why) from `gh api -X PUT repos/<repo>/pulls/<n>/merge`: its stdout
    <body>, its stderr <err> and its exit <status>.

    gh prints the response body on stdout whatever the status, and `gh: <message> (HTTP <code>)`
    on stderr, exit 1. Measured 2026-11-18 (dexterds14/ds-release-scratch#20, run 44417468799):
    {"message":"Repository rule violations found\\n\\nAt least 1 approving review is required by
    reviewers with write access.\\n\\n",...,"status":"405"}. 405, 409 (head moved: the sha guard)
    and 422 are GitHub refusing the merge; anything else is a call that failed. Either way the
    PR was not merged by this call, and <why> quotes GitHub's message."""
    doc = None
    try:
        doc = json.loads(body) if body.strip() else None
    except ValueError:
        doc = None
    doc = doc if isinstance(doc, dict) else {}
    if status == 0 and doc.get('merged') is True:
        return doc.get('sha') or '', ''
    message = '; '.join(l.strip() for l in str(doc.get('message') or '').splitlines() if l.strip())
    code = str(doc.get('status') or '')
    m = re.search(r'\(HTTP (\d{3})\)\s*$', err)
    if m and not code:
        code = m.group(1)
    if not message:
        lines = [l.strip() for l in err.splitlines() if l.strip()]
        message = re.sub(r'^gh: ', '', ' '.join(lines)) if lines else f'gh exited {status}'
        message = re.sub(r'\s*\(HTTP \d{3}\)$', '', message)
    if status == 0:
        return None, f'GitHub answered without merging it ({message})'
    if code in REFUSALS:
        return None, f'GitHub refused the merge (HTTP {code}: {message})'
    return None, f'the merge call failed ({"HTTP " + code + ": " if code else ""}{message})'


def checks_state(doc, head, required):
    """('green' | 'red' | 'pending' | 'moved' | 'closed', [reasons]) for a bot-finished PR,
    from `gh pr view --json state,headRefOid,statusCheckRollup` (ENG-2235).

    green only when the PR is open, its head is still <head> and every required check has
    reported success on it. A required check that is missing or still running is pending;
    one that failed is red. Only the most recent run of each check counts (latest_runs): an
    older run a newer one superseded, cancelled or not, says nothing about the head any more.
    Checks that are not required are not read. With no required check at all nothing could
    ever vouch for the commit, so that is red too."""
    if doc.get('state') not in (None, 'OPEN'):
        return 'closed', [f'the PR is {doc.get("state")}']
    if doc.get('headRefOid') != head:
        return 'moved', [f'the PR head is {doc.get("headRefOid")}, not the commit the bot pushed ({head})']
    if not required:
        return 'red', ['no required check is configured (merge_up.finish.required_checks), so nothing vouches for the commit']
    rollup = latest_runs(doc.get('statusCheckRollup'))
    red, pending = [], []
    for name in required:
        runs = [c for c in rollup if check_names(c) == name]
        if not runs:
            pending.append(f'{name}: not reported yet')
            continue
        failed = [c for c in runs if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES]
        if failed:
            red.append(f'{name}: {failed[0].get("conclusion") or failed[0].get("state")}')
        elif not all(c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS' for c in runs):
            pending.append(f'{name}: {runs[0].get("status") or runs[0].get("conclusion") or runs[0].get("state") or "running"}')
    if red:
        return 'red', red + pending
    if pending:
        return 'pending', pending
    return 'green', [f'{name}: SUCCESS' for name in required]


# The codes of auto_finish.check_sources that stop a bot finish like a red check: the newest run
# of a required name failed, or it came from somewhere other than its source. The others
# (missing, still running, unreadable) can still turn green: the bot waits for them. Another PR
# of the head branch into another base, or a change of the PR's base (auto_finish.head_branch_prs),
# does not go away by itself either (a closed PR stays listed): the bot stops at once.
SOURCE_RED = ('check-source', 'check-source-failed', 'check-source-other-pr', 'check-source-base-changed')


def sources_state(required, sources, head_ref, head, check_runs, actions_runs, jobs=None, pr=None, base=None, repo=None,
                  head_prs=None, events=None, release_app=None, other_events=None):
    """('green' | 'pending' | 'red', [reasons]): whether every required check of a bot-finished
    PR's head came from its source (merge_up check_sources), by auto_finish.check_sources, the
    function the auto-finish asks (ENG-2235). checks_state reads the check names only; a check
    run any workflow or token posts under a required name passes there, not here. green lists
    each name with its source. pr, base, repo: the PR's number, its base and the repository
    that holds it (owner/name): a workflow source counts only from this PR's own runs. head_prs,
    events: every PR of the head branch and the PR's issue events (None: could not be read, which
    is pending): a run of the branch is this PR's alone only while no other PR of the branch has
    another base, none into this base was opened by anyone but the release App (release_app) or
    had its base changed (other_events: its issue events, by str(number); one missing is pending),
    and the PR's base never changed (auto_finish.head_branch_prs)."""
    import auto_finish   # loaded here: it imports this module
    found = auto_finish.check_sources(list(required), sources, head_ref, head, check_runs, actions_runs, jobs,
                                      pr=pr, base=base, repo=repo, head_prs=head_prs, events=events,
                                      release_app=release_app, other_events=other_events)
    if not found:
        return 'green', [f'{n}: ' + (f'the App {sources[n]["app"]}' if 'app' in sources[n] else sources[n]['workflow'])
                         for n in required]
    red = [text for code, text in found if code in SOURCE_RED]
    rest = [text for code, text in found if code not in SOURCE_RED]
    return ('red', red + rest) if red else ('pending', rest)


# How merge-up.sh's close_superseded starts the comment it closes a PR with.
SUPERSEDED = 'Superseded by '


def latest_pr(prs):
    """(number, state, url) of the PR that speaks for a head, from a `gh pr list
    --state all --json number,state,url` answer, or None when it lists none: its
    open PR when it has one, whatever its number (a person may reopen an older PR
    after a newer one of the same head was closed), else the most recent one
    (highest number)."""
    if not prs:
        return None
    pr = max([p for p in prs if p['state'] == 'OPEN'] or prs, key=lambda p: p['number'])
    return pr['number'], pr['state'], pr.get('url') or ''


def person_no(state, issue=None, comments=None, reviews=None, release_app=None):
    """Why the most recent merge-up PR of a head is a person's no to it, or None.

    CLOSED (without a merge): a no, unless the bot closed it as superseded. That
    is read from three things together: `closed_by` of the REST issue is of type
    Bot, its login is release_app (the release App's login, GIT_BOT_NAME: any
    workflow's github-actions[bot] can close a PR and comment too, ENG-2235 2c
    review), and that same account left a comment that starts the way
    close_superseded writes it. Anything else, a closer GitHub does not name
    and a call that names no release_app included, is a no. OPEN: a no when the latest review of any person is
    CHANGES_REQUESTED (person_reviews: a later approval or a dismissal by the
    same person takes it back, and a bot's review never counts; ENG-2235). A
    merged PR is never a no."""
    if state == 'CLOSED':
        closer = (issue or {}).get('closed_by') or {}
        if closer.get('type') == 'Bot' and release_app and closer.get('login') == release_app and any(
                (c.get('user') or {}).get('login') == closer.get('login') and (c.get('body') or '').startswith(SUPERSEDED)
                for c in comments or []):
            return None
        return f'{closer.get("login") or "someone GitHub does not name"} closed it without merging'
    if state == 'OPEN':
        latest, _ = person_reviews(reviews or [])
        refused = sorted(login for login, rv in latest.items() if rv.get('state') == 'CHANGES_REQUESTED')
        if refused:
            return f'{", ".join(refused)} requested changes'
    return None


def merge_up_siblings(lines, child, skip):
    """[(number, state, head, url)]: for every merge-up/<child>/<sha7> head other than <skip> in
    "<number> <state> <head> <url>" lines (the merge-up PRs of a parent, every state), the PR
    that speaks for that head (latest_pr), newest first. A new merge-up PR of <child>
    supersedes them; bot-finish asks whether a person said no to one (ENG-2235)."""
    head_re = re.compile(re.escape(f'merge-up/{child}/') + r'[0-9a-f]{7}')
    heads = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 4 or not parts[0].isdigit() or parts[2] == skip or not head_re.fullmatch(parts[2]):
            continue
        heads.setdefault(parts[2], []).append({'number': int(parts[0]), 'state': parts[1], 'url': parts[3]})
    out = []
    for head, prs in heads.items():
        number, state, url = latest_pr(prs)
        out.append((number, state, head, url))
    return sorted(out, reverse=True)


def rules_would_resolve_all(report, listed):
    """Whether the merge rules would resolve every path of <listed> once they act: each is a file
    of the report (merge_rules.resolve_conflicts) whose would is "resolve", not never_auto and not
    refused. A path the rules never saw is not. An empty list is not either (ENG-2235)."""
    files = {f['path']: f for f in report.get('files') or []}
    if not listed:
        return False
    for path in listed:
        f = files.get(path)
        if not f or f.get('would') != 'resolve' or f.get('never_auto') or f.get('refused'):
            return False
    return True


PUSH_ACTIVITIES = ('push', 'force_push', 'branch_creation')


def push_actors(activity=None, commits=None):
    """(logins, how, unknown): the people who pushed to a PR branch.

    activity: the repository activity of the branch. Every actor of a push, a
    force push or the branch's creation, bots aside (the release App made the
    branch). When it is None the fallback is commits, the REST commit objects
    of the commits above the bot's commit: every author and committer with a
    login, bots aside. A commit whose author or committer GitHub maps to no
    account is returned in unknown, because nothing then shows who wrote it."""
    logins, unknown = set(), []
    if activity is not None:
        for a in activity:
            actor = a.get('actor') or {}
            if a.get('activity_type') in PUSH_ACTIVITIES and actor.get('login') and actor.get('type') != 'Bot':
                logins.add(actor['login'])
        return logins, 'the repository activity', unknown
    for c in commits or []:
        for role in ('author', 'committer'):
            user = c.get(role) or {}
            if user.get('login'):
                if user.get('type') != 'Bot':
                    logins.add(user['login'])
            else:
                ident = ((c.get('commit') or {}).get(role) or {})
                unknown.append(f'{(c.get("sha") or "?")[:7]} ({role} {ident.get("email") or ident.get("name") or "unknown"})')
    return logins, 'the authors and committers of the commits above the bot\'s commit', unknown


def required_checks(doc, required):
    """Reasons a required check is not green on the head (finish.strict): each
    name must be reported in the view's statusCheckRollup, and the most recent run
    of it (latest_runs, per workflow when two have a job of that name) must have
    succeeded. Failed runs are named by pr_check's own loop."""
    reasons = []
    rollup = latest_runs(doc.get('statusCheckRollup'))
    for name in required:
        runs = [c for c in rollup if check_names(c) == name]
        if not runs:
            reasons.append(f'required check {name}: not reported on the head')
            continue
        for c in runs:
            if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES:
                continue
            if not (c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS'):
                reasons.append(f'required check {name}: {c.get("conclusion") or c.get("state") or c.get("status") or "not finished"}, not SUCCESS')
                break
    return reasons


def without_review_trigger(doc):
    """The view with the check runs of the merge-up review trigger left out of its
    statusCheckRollup (ENG-2235). That workflow runs on every review of a merge-up PR and
    does nothing; a run of it that was cancelled, timed out or is still queued says nothing
    about the head, and must not keep a finish from merging."""
    rollup = doc.get('statusCheckRollup')
    if not rollup:
        return doc
    return dict(doc, statusCheckRollup=[c for c in rollup if c.get('workflowName') != REVIEW_TRIGGER_WORKFLOW])


def unstable_for_review_trigger(rollup):
    """True when the review trigger's own check runs are what make GitHub call a PR UNSTABLE:
    at least one of the most recent runs (latest_runs) is unfinished or not a success, and every
    such run is a check run of the merge-up review trigger (ENG-2235). A queued, running,
    cancelled or failed run of that no-op says nothing about the head. A commit status, or a
    check run of any other workflow, that is unfinished or not a success makes it False."""
    flagged = []
    for c in latest_runs(rollup):
        if c.get('conclusion') in ('SUCCESS', 'SKIPPED', 'NEUTRAL') or c.get('state') == 'SUCCESS':
            continue
        flagged.append(c)
    return bool(flagged) and all(check_key(c)[:2] == ('check', REVIEW_TRIGGER_WORKFLOW) for c in flagged)


def pr_check(doc, reviews=None, strict=False, required=(), activity=None, commits=None, notes=None):
    """Reasons the PR must not be merged by the bot (empty = ok).

    reviews None: the reviews of the view itself, where a bot can only be told
    by its login (is_bot_login). reviews a list: the REST reviews, where the
    account type says who is a person (what merge-up.sh passes).
    strict (merge_up.finish.strict, with the REST reviews): the approval must
    be on the PR head, the approver must not have pushed to the branch
    (push_actors) and every required check must be green on the head. notes,
    a list, receives what the strict checks went by, for the log. The check
    runs of the merge-up review trigger are not read, in either mode
    (without_review_trigger), and an UNSTABLE merge state that only they
    cause is no reason (unstable_for_review_trigger)."""
    trigger_unstable = (doc.get('mergeStateStatus') == 'UNSTABLE'
                        and unstable_for_review_trigger(doc.get('statusCheckRollup')))
    doc = without_review_trigger(doc)
    reasons = []
    if doc.get('reviewDecision') != 'APPROVED':
        reasons.append(f'reviewDecision is {doc.get("reviewDecision")!r}, not APPROVED')
    if doc.get('mergeStateStatus') != 'CLEAN' and not trigger_unstable:
        reasons.append(f'mergeStateStatus is {doc.get("mergeStateStatus")!r}, not CLEAN')
    pr_commits = doc.get('commits') or []
    pusher = None
    if pr_commits:
        authors = pr_commits[-1].get('authors') or []
        if authors:
            pusher = authors[0].get('login')
    if strict and reviews is None:
        raise Error('pr-check: strict needs the REST reviews (--reviews)')
    if reviews is None:
        latest = {}
        for rv in doc.get('reviews') or []:
            login = (rv.get('author') or {}).get('login')
            state = rv.get('state')
            # Bot reviews never count: the Claude review workflow posts as
            # github-actions, and an approval from it is not a person's.
            if login and not is_bot_login(login) and state in REVIEW_STATES:
                latest[login] = state  # gh lists reviews oldest first
        approvers = sorted(l for l, s in latest.items() if s == 'APPROVED' and l != pusher)
        if not approvers:
            reasons.append(f'no APPROVED review by someone other than the last pusher ({pusher!r})')
    else:
        latest, bots = person_reviews(reviews)
        approved = {l: rv for l, rv in latest.items() if rv.get('state') == 'APPROVED'}
        bot_note = f'; the approval by {", ".join(bots)} is a bot review and never counts' if bots else ''
        if not strict:
            approvers = sorted(l for l in approved if l != pusher)
            if not approvers:
                reasons.append(f'no APPROVED review by someone other than the last pusher ({pusher!r}){bot_note}')
        else:
            head = doc.get('headRefOid')
            if not head:
                raise Error('pr-check: strict needs headRefOid in the view')
            pushers, how, unknown = push_actors(activity, commits)
            if activity is None and commits is None:
                reasons.append('strict: neither the repository activity of the branch nor its commits could be read, '
                               'so nothing shows who pushed to it')
            if notes is not None:
                notes.append(f'push actors, from {how}: {", ".join(sorted(pushers)) or "nobody but the bot"}')
            for who in unknown:
                reasons.append(f'strict: commit {who} maps to no GitHub account, so nothing shows the approver did not write it')
            # One approval that counts is enough: strict binds the review that is counted,
            # not every review on the PR. An approval that does not count (stale, or by a
            # pusher) is a reason only when no other approval does; next to one that
            # counts it is a note in the log.
            approvers, not_counted = [], []
            for login in sorted(approved):
                on = approved[login].get('commit_id')
                if on != head:
                    not_counted.append(f'strict: {login} approved {(on or "an unknown commit")[:7]}, not the PR head {head[:7]}; '
                                       'an approval counts only for the commit it was given on')
                elif login in pushers:
                    not_counted.append(f'strict: {login} approved but also pushed to the branch ({how})')
                else:
                    approvers.append(login)
            if not approvers:
                reasons.extend(not_counted)
                reasons.append(f'strict: no APPROVED review on the PR head {head[:7]} by a person who did not push to the branch{bot_note}')
            elif notes is not None:
                notes.extend(f'not counted, {why[len("strict: "):]}' for why in not_counted)
            reasons.extend(required_checks(doc, required))
    # Only the most recent run of each check: a run a newer one superseded (a reopen or a
    # push cancels the older run's jobs) is not a failure of the head.
    for c in latest_runs(doc.get('statusCheckRollup')):
        name = c.get('name') or c.get('context') or c.get('workflowName') or '?'
        if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES:
            reasons.append(f'check {name}: {c.get("conclusion") or c.get("state")}')
    return reasons, pusher, approvers


MARKER_RE = r'^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )'


def marked_files(git_dir, base, head):
    """The files <head> changes against its merge base with <base> that hold a
    conflict marker line. zdiff3's base line counts: a resolver who deletes
    only the <<<<<<< and >>>>>>> lines leaves it behind. Only changed files
    are read, so a doc or a fixture on the base that shows a conflict does not
    count; deleted files are skipped. Git names files in bytes, which need not
    be UTF-8: the names come back with surrogateescape, as os.fsdecode gives
    them, and the markers command prints them as the bytes they were."""
    def names(*args):
        r = subprocess.run(['git', '-C', git_dir, *args], capture_output=True)
        if r.returncode not in (0, 1) or (r.returncode == 1 and args[0] != 'grep'):   # grep's 1: no file matched
            raise Error(f'git {args[0]}: {r.stderr.decode(errors="replace").strip()}')
        return [f.decode('utf-8', 'surrogateescape') for f in r.stdout.split(b'\0') if f]

    merge_base = git(git_dir, 'merge-base', base, head).strip()
    changed = names('diff', '-z', '--name-only', '--diff-filter=d', merge_base, head)
    marked = []
    for i in range(0, len(changed), 200):
        found = names('grep', '-l', '-z', '-E', MARKER_RE, head, '--', *(f':(literal){f}' for f in changed[i:i + 200]))
        marked += [f.split(':', 1)[1] for f in found]
    return marked


def bookkeeping_changes(git_dir, parent, head, bookkeeping_files=()):
    """The bookkeeping files a merge-up PR at <head> changes against its merge base with the
    parent line at <parent>, in git's order (ENG-2234). The files are what merge-keep-target
    keeps when it runs with the parent checked out: each versions/<product> entry of <parent>
    (versions/README.md aside, it merges like any doc) and each of bookkeeping_files that
    <parent> has. A change, a deletion or a mode change counts.

    Every commit the bot makes passes: merge-keep-target restored the parent's copies, and the
    merge base is the parent tip itself. A parent that bumped its own version after the PR
    opened passes too, before the branch merges it in (the merge base still holds the old
    copy, which the branch keeps) and after (the merge base is the new tip). What fails is a
    head that keeps the child's copy: a no-marker PR still on the child commit, or a
    resolution made the wrong way round (the parent merged into the child's branch). Merged
    as it is, it would carry the child line's pending version or SDK line into the parent."""
    merge_base = git(git_dir, 'merge-base', parent, head).strip()
    listed = git(git_dir, 'ls-tree', '-z', '--name-only', parent, '--', VERSIONS_DIR, *bookkeeping_files)
    paths = [p for p in listed.split('\0') if p and p != VERSIONS_README]
    if not paths:
        return []
    changed = git(git_dir, 'diff', '-z', '--no-renames', '--name-only', merge_base, head, '--',
                  *(f':(literal){p}' for p in paths))
    return [f for f in changed.split('\0') if f]


def branch_origin(git_dir, head, parent, like=None, bot_email=None):
    """('ours' | 'foreign', first commit, why) for a merge-up branch at <head>.

    The first commit above the parent is the last commit of head's first-parent
    chain that <parent> does not contain: the merge the bot pushed, with a
    resolver's commits (merges of the parent included) on top of it. With
    <like>, the commit this run made for the same merge, it is ours only when
    it has that commit's tree, parents and committer, whatever its dates. With
    only <bot_email> it is ours when it is a merge of two lines committed
    under that address, which is all a finish can know."""
    chain = git(git_dir, 'rev-list', '--first-parent', head, f'^{parent}').split()
    if not chain:
        return 'foreign', '', 'it holds no commit above the parent line'
    first = chain[-1]

    def facts(sha):
        parents, tree, email = git(git_dir, 'show', '-s', '--format=%P%n%T%n%ce', sha).splitlines()[:3]
        return parents.split(), tree, email

    parents, tree, email = facts(first)
    what = f'its first commit above the parent line, {first[:7]},'
    if len(parents) != 2:
        return 'foreign', first, f'{what} is not a merge of two lines'
    if like:
        want_parents, want_tree, want_email = facts(like)
        if email != want_email:
            return 'foreign', first, f'{what} was committed by {email}, not by the bot'
        if parents != want_parents:
            return 'foreign', first, (f'{what} merges {parents[1][:7]} onto {parents[0][:7]}, while this run merges '
                                      f'{want_parents[1][:7]} onto {want_parents[0][:7]} (a line moved since, or the bot did not make it)')
        if tree != want_tree:
            return 'foreign', first, f'{what} does not hold the tree this run computed for the same merge'
    elif bot_email is not None and email != bot_email:
        return 'foreign', first, f'{what} was committed by {email}, not by the bot'
    return 'ours', first, ''


def automation(value):
    """DS_MERGE_UP_AUTOMATION parsed into {'value', 'act', 'shadow'}: the rules
    that act and the rules that only evaluate and log, each in MERGE_UP_RULES
    order. A rule in neither list is not evaluated at all.

    - absent (None), empty or "off": nothing is evaluated.
    - "shadow": every rule evaluates and logs, none acts.
    - a comma list of rule names: those act, the others stay in shadow.
    The list may also name an action (MERGE_UP_ACTIONS, bot-finish). It is not a
    rule and changes nothing here: action_mode reads it from 'value'.
    Anything else raises Error naming the token. The words are exact (lower
    case); only space around the value and around a list item is ignored, so
    a mistyped switch stops the run instead of reading as off."""
    raw = '' if value is None else value.strip()
    if raw in ('', 'off'):
        return {'value': raw, 'act': [], 'shadow': []}
    if raw == 'shadow':
        return {'value': raw, 'act': [], 'shadow': list(MERGE_UP_RULES)}
    known = f'off, shadow, or a comma list of {", ".join(MERGE_UP_RULES)} and the action {", ".join(MERGE_UP_ACTIONS)}'
    tokens = [t.strip() for t in raw.split(',')]
    for t in tokens:
        if not t:
            raise Error(f'{AUTOMATION_VAR}={raw!r}: empty item in the list (expected {known})')
        if t not in MERGE_UP_RULES + MERGE_UP_ACTIONS:
            hint = ''
            if t in ('off', 'shadow') and len(tokens) > 1:
                hint = ' ("off" and "shadow" stand alone, not in a list)'
            elif t in OTHER_REPO_RULES:
                hint = ' of this repo (it is a rule of the OS distro repo)'
            raise Error(f'{AUTOMATION_VAR}={raw!r}: {t!r} is not a rule{hint} (expected {known})')
    return {'value': raw,
            'act': [r for r in MERGE_UP_RULES if r in tokens],
            'shadow': [r for r in MERGE_UP_RULES if r not in tokens]}


def rule_mode(auto, rule):
    """'act', 'shadow' or 'off' for one rule under a parsed automation value."""
    if rule not in MERGE_UP_RULES:
        raise Error(f'{rule!r} is not a merge-up rule ({", ".join(MERGE_UP_RULES)})')
    return 'act' if rule in auto['act'] else 'shadow' if rule in auto['shadow'] else 'off'


def action_mode(auto, action):
    """'act', 'shadow' or 'off' for one action (MERGE_UP_ACTIONS) under a parsed automation value:
    off when the value is absent, empty or "off"; shadow under "shadow" and under a list that does
    not name it (it logs what it would do); act when the list names it."""
    if action not in MERGE_UP_ACTIONS:
        raise Error(f'{action!r} is not a merge-up action ({", ".join(MERGE_UP_ACTIONS)})')
    raw = auto['value']
    if raw in ('', 'off'):
        return 'off'
    if raw == 'shadow':
        return 'shadow'
    return 'act' if action in [t.strip() for t in raw.split(',')] else 'shadow'


def action_summary(auto):
    """The run-summary line of the actions: the last line of merge-up.sh's switches block."""
    mode = action_mode(auto, 'bot-finish')
    if mode == 'off':
        text = 'off (a rule-resolved PR waits for one approval)'
    elif mode == 'shadow':
        text = 'shadow (log only: a rule-resolved PR waits for one approval)'
    elif any(r in auto['act'] for r in BOT_FINISH_RULES):
        text = 'acting (a rule-resolved PR is merged with no approval once its required checks succeed on the bot\'s commit)'
    else:
        text = 'acting, but splice does not act, so no rule-resolved PR is made and nothing is finished'
    return [f'bot finish:         {text}']


def automation_summary(auto):
    """The two run-summary lines: the variable as set, and what it switches on."""
    variable = f'{AUTOMATION_VAR}={auto["value"]}' if auto['value'] else f'{AUTOMATION_VAR} is absent'
    if auto['act']:
        rules = f'acting: {", ".join(auto["act"])}; shadow (evaluate and log only): {", ".join(auto["shadow"]) or "none"}'
    elif auto['shadow']:
        rules = f'none acting; shadow (evaluate and log only): {", ".join(auto["shadow"])}'
    else:
        rules = 'none (automation: off, no rule is evaluated)'
    return [f'variable:           {variable}', f'active rules:       {rules}']


def merge_up_config(root=None):
    """The "merge_up" block of release/config.json under root, validated, with
    the defaults for a missing key or a missing block (version.MERGE_UP_DEFAULTS).
    root defaults to the checkout this script is in, not the directory a
    script was started from: the config that governs a merge-up is the one on
    the ref whose scripts run (the default branch, for every automatic run and
    every finish dispatched as documented), never a child line's."""
    try:
        return version.merge_up(os.path.abspath(root) if root else os.path.dirname(HERE))
    except version.CheckError as e:
        raise Error(str(e))


def never_auto(patterns, path):
    """The first pattern of a never_auto list that matches the repo-relative
    path, or None. No rule may write to a file for which this is not None.
    The match is version.never_auto_regex's: glob_regex's patterns (** crosses
    directories), but a pattern without a slash matches any one path segment,
    a directory name as well as the file name, and case does not count."""
    for pattern in patterns:
        try:
            regex = version.never_auto_regex(pattern)
        except version.CheckError as e:
            raise Error(str(e))
        if regex.fullmatch(path):
            return pattern
    return None


# --- the merge rules (ENG-2234) --------------------------------------------------

def resolve_conflicts(git_dir, out_dir, skip=(), root=None, check=False, value=None):
    """merge_rules.resolve_conflicts under this repo's switch and never_auto
    list, its outputs written to out_dir. Only the splice runs here: the pins
    rule of merge_rules.py is off and gets no pin configuration. Returns the
    report, with the skipped paths that are unmerged added to 'remaining' and
    listed as 'skipped', and its log lines."""
    # Loaded here, not at the top: every release script runs this file, and
    # only this command needs the rules.
    import merge_rules
    patterns = merge_up_config(root)['never_auto']
    auto = automation(os.environ.get(AUTOMATION_VAR) if value is None else value)
    modes = {'splice': rule_mode(auto, 'splice'), 'pins': 'off'}
    try:
        unmerged_now = merge_rules.unmerged_paths(git_dir)
        skipped = [p for p in unmerged_now if p in skip]

        def outputs(report):
            report['skipped'] = skipped
            report['remaining'] = sorted(report['remaining'] + skipped)
            merge_rules.write_outputs(report, out_dir)

        # The outputs are written inside the rules' own undo: if they cannot be
        # written (the caller then reads "not evaluated" and goes on without the
        # rules), no file of the merge stays resolved behind the caller's back.
        report = merge_rules.resolve_conflicts(
            git_dir, modes, [p for p in unmerged_now if p not in skip],
            never_auto=lambda path: never_auto(patterns, path), write=not check, after=outputs)
    except merge_rules.Error as e:
        raise Error(str(e))
    except OSError as e:
        raise Error(f'the rules\' outputs could not be written ({e})')
    return report, merge_rules.log_lines(report)


def read_json_arg(path):
    if path and path != '-':
        with open(path) as f:
            return json.load(f)
    return json.load(sys.stdin)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    root_help = 'repo root with release/config.json (default: this checkout)'
    p = sub.add_parser('merge-keep-target')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('line-plan')
    p.add_argument('product')
    p.add_argument('version')
    p.add_argument('--kind', default='auto', choices=LINE_KINDS)
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('promote-plan')
    p.add_argument('line')
    p.add_argument('pending')
    p.add_argument('--bump', default='auto', choices=BUMP_LEVELS)
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('unmerged')
    p.add_argument('base')
    p.add_argument('head')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    for name in ('versions-only', 'walk-versions-only'):
        p = sub.add_parser(name)
        p.add_argument('sha')
        p.add_argument('--git-dir', default='.')
        if name == 'walk-versions-only':
            p.add_argument('--max', type=int, default=5)
    p = sub.add_parser('latest-pr', help='"<number> <state> <url>" of the open PR of a `gh pr list --state all --json number,state,url` answer, else of its most recent PR; nothing when it is empty')
    p.add_argument('file')
    p = sub.add_parser('person-no', help="why a merge-up PR is a person's no (closed by a person, or changes requested); prints nothing when it is not")
    p.add_argument('--state', required=True, help='the PR state: OPEN, CLOSED or MERGED')
    p.add_argument('--issue', help='CLOSED: the REST issue of the PR (closed_by)')
    p.add_argument('--comments', help='CLOSED: its REST issue comments')
    p.add_argument('--reviews', help='OPEN: its REST reviews')
    p.add_argument('--release-app', help="CLOSED: the release App's login (GIT_BOT_NAME); only its close is the bot's supersession")
    p = sub.add_parser('checks-state')
    p.add_argument('file')
    p.add_argument('--head', required=True)
    p.add_argument('--required', default='', help='comma list of check names')
    p = sub.add_parser('check-sources', help='whether every required check of a head came from its source: '
                                             'green, pending or red, then the reasons')
    p.add_argument('--required', default='', help='comma list of check names')
    p.add_argument('--sources', required=True, help="merge_up.finish.check_sources (JSON)")
    p.add_argument('--head-ref', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--pr', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--repo', required=True, help='owner/name of the repository that holds the PR')
    p.add_argument('--head-prs', help='every PR of the head branch, REST (absent: it could not be read)')
    p.add_argument('--events', help="the PR's REST issue events (absent: they could not be read)")
    p.add_argument('--release-app', help="the release App's login (GIT_BOT_NAME): only its same-base PRs of the branch are set aside")
    p.add_argument('--other-events', help='<number>.json: the REST issue events of each PR auto_finish.py same-base-prs names')
    p.add_argument('--check-runs')
    p.add_argument('--actions-runs')
    p.add_argument('--jobs-dir')
    p = sub.add_parser('siblings', help='the PR of every other merge-up/<child>/<sha7> head of a listing, newest first')
    p.add_argument('file')
    p.add_argument('--child', required=True)
    p.add_argument('--skip', default='')
    p = sub.add_parser('rules-would-resolve', help='exit 0 when the rules would resolve every listed file once they act')
    p.add_argument('report')
    p.add_argument('--listed', required=True, help='file: one conflicted path per line')
    p = sub.add_parser('merge-answer', help='whether `gh api -X PUT repos/<repo>/pulls/<n>/merge` merged: '
                                            '"merged <sha>", exit 0; else the reason, quoting GitHub, exit 1')
    p.add_argument('body', help="the call's stdout (the response body)")
    p.add_argument('--stderr', required=True, help="the call's stderr")
    p.add_argument('--exit', type=int, required=True, help="the call's exit status")
    p = sub.add_parser('first-run')
    p.add_argument('file', nargs='?')
    p = sub.add_parser('pr-check')
    p.add_argument('file', nargs='?')
    p.add_argument('--reviews', help='the REST reviews of the PR; only people count')
    p.add_argument('--strict', action='store_true', help='merge_up.finish.strict')
    p.add_argument('--required', default='', help='strict: comma list of check names that must be green on the head')
    p.add_argument('--activity', help='strict: the repository activity of the PR branch')
    p.add_argument('--commits', help='strict, when the activity could not be read: REST commit objects of the commits above the bot\'s commit')
    p = sub.add_parser('markers')
    p.add_argument('--base', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('bookkeeping')
    p.add_argument('--parent', required=True, help='a ref or commit of the parent line')
    p.add_argument('--head', required=True)
    p.add_argument('--git-dir', default='.')
    p.add_argument('--root', default=os.path.dirname(HERE), help=root_help)
    p = sub.add_parser('branch-origin')
    p.add_argument('head')
    p.add_argument('--parent', required=True, help='a ref or commit of the parent line')
    p.add_argument('--like', help='the commit this run made for the same merge')
    p.add_argument('--bot-email')
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('resolve-conflicts')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--out', required=True, help='directory for report.json, log.txt, body.md, comment.md, trailers.txt, remaining.txt')
    p.add_argument('--skip', action='append', default=[], help='a path to leave alone; it stays in remaining.txt')
    p.add_argument('--check', action='store_true', help='evaluate only, write nothing to the work tree')
    p.add_argument('--root', help='repo root with release/ (default: the checkout this script is in)')
    p = sub.add_parser('merge-up-config')
    p.add_argument('--root', help='repo root with release/config.json (default: the checkout this script is in)')
    p.add_argument('--key', help='dotted path inside the block, e.g. finish.strict')
    p = sub.add_parser('automation')
    p.add_argument('--value', help=f'parse this instead of ${AUTOMATION_VAR}')
    p.add_argument('--rule', choices=MERGE_UP_RULES, help='print off, shadow or act for this rule')
    p.add_argument('--action', choices=MERGE_UP_ACTIONS, help='print off, shadow or act for this action')
    p.add_argument('--actions', action='store_true', help='print the summary line of the actions')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'merge-keep-target':
            cfg = load_config(args.root)
            kept = merge_keep_target(args.git_dir, cfg['bookkeeping_files'])
            print(f'kept from the target: {", ".join(kept) or "nothing (no versions files, no bookkeeping files)"}')
        elif args.cmd == 'line-plan':
            if not version.PRODUCT_NAME.fullmatch(args.product):
                raise Error(f'product {args.product!r} must match {version.PRODUCT_NAME.pattern}')
            cfg = load_config(args.root)
            print(json.dumps(line_plan(args.product, args.version, args.kind, cfg['product_prefix'])))
        elif args.cmd == 'promote-plan':
            cfg = load_config(args.root)
            print(json.dumps(promote_plan(args.line, args.pending, cfg['products'], args.bump, cfg['product_prefix'])))
        elif args.cmd == 'unmerged':
            cfg = load_config(args.root)
            missing = unmerged(args.git_dir, args.base, args.head, cfg['bookkeeping_files'])
            for sha, subject in missing:
                print(f'{sha} {subject}')
            return 1 if missing else 0
        elif args.cmd == 'versions-only':
            return 0 if is_versions_only(args.git_dir, args.sha) else 1
        elif args.cmd == 'walk-versions-only':
            print('\n'.join(walk_versions_only(args.git_dir, args.sha, args.max)))
        elif args.cmd == 'latest-pr':
            found = latest_pr(read_json_arg(args.file))
            if found:
                print(*found)
        elif args.cmd == 'person-no':
            why = person_no(args.state, read_json_arg(args.issue) if args.issue else None,
                            read_json_stream(args.comments) if args.comments else None,
                            read_json_stream(args.reviews) if args.reviews else None, args.release_app)
            if why:
                print(why)
        elif args.cmd == 'checks-state':
            state, reasons = checks_state(read_json_arg(args.file), args.head, [n for n in args.required.split(',') if n])
            print(state)
            for r in reasons:
                print(r)
        elif args.cmd == 'check-sources':
            import auto_finish   # its readers of the REST answers
            state, reasons = sources_state(
                [n for n in args.required.split(',') if n], auto_finish._sources(args.sources), args.head_ref, args.head,
                auto_finish._check_runs(args.check_runs) if args.check_runs else None,
                auto_finish._runs(args.actions_runs) if args.actions_runs else None, auto_finish._jobs_dir(args.jobs_dir),
                pr=args.pr, base=args.base, repo=args.repo,
                head_prs=read_json_stream(args.head_prs) if args.head_prs else None,
                events=read_json_stream(args.events) if args.events else None,
                release_app=args.release_app, other_events=auto_finish._events_dir(args.other_events))
            print(state)
            for r in reasons:
                print(r)
        elif args.cmd == 'siblings':
            with open(args.file) as f:
                for number, state, head, url in merge_up_siblings(f.read().splitlines(), args.child, args.skip):
                    print(f'{number} {state} {head} {url}')
        elif args.cmd == 'rules-would-resolve':
            with open(args.listed) as f:
                listed = [l for l in f.read().split('\n') if l]
            return 0 if rules_would_resolve_all(read_json_arg(args.report), listed) else 1
        elif args.cmd == 'merge-answer':
            with open(args.body, errors='replace') as f:
                body = f.read()
            with open(args.stderr, errors='replace') as f:
                err = f.read()
            sha, why = merge_answer(body, err, args.exit)
            if sha is None:
                print(why)
                return 1
            print(f'merged {sha}')
        elif args.cmd == 'first-run':
            out = first_run(read_json_arg(args.file))
            if out is None:
                return 1
            print(out)
        elif args.cmd == 'pr-check':
            reviews = read_json_stream(args.reviews) if args.reviews else None
            doc, notes = read_json_arg(args.file), []
            required = [n for n in args.required.split(',') if n]
            reasons, pusher, approvers = pr_check(
                doc, reviews, strict=args.strict, required=required,
                activity=read_json_stream(args.activity) if args.activity else None,
                commits=read_json_stream(args.commits) if args.commits else None, notes=notes)
            for n in notes:
                print(f'strict finish:      {n}')
            for r in reasons:
                print(f'release_git.py: pr-check: {r}', file=sys.stderr)
            if reasons:
                return 1
            if args.strict:
                print(f'approved by {", ".join(approvers)} on the head {doc["headRefOid"][:7]}, none of whom pushed to the branch; mergeable, '
                      f'required checks green ({", ".join(required) or "none configured"})')
            else:
                print(f'approved by {", ".join(approvers)} (last pusher {pusher}), mergeable, checks green')
        elif args.cmd == 'markers':
            marked = marked_files(args.git_dir, args.base, args.head)
            # the names as git gave them, whatever the locale's encoding
            sys.stdout.flush()
            sys.stdout.buffer.write(b''.join(f.encode('utf-8', 'surrogateescape') + b'\n' for f in marked))
            sys.stdout.buffer.flush()
            if marked:
                print(f'release_git.py: markers: {len(marked)} file(s) changed by {args.head[:7]} still hold conflict markers', file=sys.stderr)
                return 1
        elif args.cmd == 'bookkeeping':
            cfg = load_config(args.root)
            changed = bookkeeping_changes(args.git_dir, args.parent, args.head, cfg['bookkeeping_files'])
            for f in changed:
                print(f)
            if changed:
                return 1
        elif args.cmd == 'branch-origin':
            state, first, why = branch_origin(args.git_dir, args.head, args.parent, args.like, args.bot_email)
            print(f'{state} {first}'.rstrip())
            if why:
                print(why)
        elif args.cmd == 'resolve-conflicts':
            _, log = resolve_conflicts(args.git_dir, args.out, skip=args.skip, root=args.root, check=args.check)
            for line in log:
                print(line)
        elif args.cmd == 'merge-up-config':
            value = merge_up_config(args.root)
            for part in (args.key.split('.') if args.key else ()):
                if not isinstance(value, dict) or part not in value:
                    raise Error(f'merge_up has no key {args.key!r}')
                value = value[part]
            print(value if isinstance(value, str) else json.dumps(value))
        elif args.cmd == 'automation':
            auto = automation(os.environ.get(AUTOMATION_VAR) if args.value is None else args.value)
            if args.rule:
                print(rule_mode(auto, args.rule))
            elif args.action:
                print(action_mode(auto, args.action))
            elif args.actions:
                print('\n'.join(action_summary(auto)))
            else:
                print('\n'.join(automation_summary(auto)))
        return 0
    except (Error, OSError, ValueError) as e:
        print(f'release_git.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
