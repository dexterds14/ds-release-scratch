#!/usr/bin/env python3
"""Git and GitHub helpers shared by create-branch.sh, merge-up.sh, abandon.sh and promote.sh (ENG-1933, ENG-1934, ENG-1935, ENG-2110).

    release_git.py merge-keep-target [--git-dir DIR] [--root ROOT]
        inside an uncommitted merge: the target keeps its pending-version files
        (versions/<product>, never versions/README.md, which merges like any doc)
        and its copy of every bookkeeping file (release/config.json
        `bookkeeping_files`) that exists in HEAD, via `git checkout HEAD --`, so
        those paths never conflict. What merge-up runs, and what a human runs by hand.
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
    release_git.py person-no --state STATE [--issue FILE] [--comments FILE]
        why the last PR of a merge-up/<child>/<sha7> head makes that branch dead
        for merge-up.sh: with --state CLOSED, "<login> closed it without merging",
        unless the bot closed it as superseded (--issue, the REST issue: closed_by
        is of type Bot; --comments, its issue comments: that account left a
        comment that starts "Superseded by "). Prints nothing when it is not a no
    release_git.py pr-check [FILE] [--reviews FILE]
        from `gh pr view --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits`:
        exit 0 when the PR is approved by someone other than the last pusher,
        mergeStateStatus is CLEAN and no check failed; else every reason, exit 1.
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
        every author and committer counts as a pusher; the output says so
    release_git.py markers --base SHA --head SHA [--git-dir DIR]
        the files <head> changes against its merge base with <base> that still
        hold a conflict marker line (<<<<<<< , ||||||| or >>>>>>> at the start of
        a line), one per line, exit 1; exit 0 when there is none. What pr-checks
        runs on a merge-up/** head, so a conflict PR cannot be merged from the
        GitHub UI with markers in it (ENG-2234)
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
    release_git.py automation [--value V] [--rule splice]
        DS_MERGE_UP_AUTOMATION (the environment variable, or --value) parsed:
        two lines for the run summary, or with --rule that rule's mode
        (off, shadow or act). Exit 1 naming the token that is not a rule

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


def person_no(state, issue=None, comments=None):
    """Why the most recent merge-up PR of a head is a person's no to it, or None.

    CLOSED (without a merge): a no, unless the bot closed it as superseded. That
    is read from two things together: `closed_by` of the REST issue is of type
    Bot, and that same account left a comment that starts the way
    close_superseded writes it. Anything else, a closer GitHub does not name
    included, is a no. An open or a merged PR is never a no."""
    if state == 'CLOSED':
        closer = (issue or {}).get('closed_by') or {}
        if closer.get('type') == 'Bot' and any(
                (c.get('user') or {}).get('login') == closer.get('login') and (c.get('body') or '').startswith(SUPERSEDED)
                for c in comments or []):
            return None
        return f'{closer.get("login") or "someone GitHub does not name"} closed it without merging'
    return None


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
    name must be reported in the view's statusCheckRollup, and every run of it
    must have succeeded. Failed runs are named by pr_check's own loop."""
    reasons = []
    rollup = doc.get('statusCheckRollup') or []
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


def pr_check(doc, reviews=None, strict=False, required=(), activity=None, commits=None, notes=None):
    """Reasons the PR must not be merged by the bot (empty = ok).

    reviews None: the reviews of the view itself, where a bot can only be told
    by its login (is_bot_login). reviews a list: the REST reviews, where the
    account type says who is a person (what merge-up.sh passes).
    strict (merge_up.finish.strict, with the REST reviews): the approval must
    be on the PR head, the approver must not have pushed to the branch
    (push_actors) and every required check must be green on the head. notes,
    a list, receives what the strict checks went by, for the log."""
    reasons = []
    if doc.get('reviewDecision') != 'APPROVED':
        reasons.append(f'reviewDecision is {doc.get("reviewDecision")!r}, not APPROVED')
    if doc.get('mergeStateStatus') != 'CLEAN':
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
    for c in doc.get('statusCheckRollup') or []:
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
    count; deleted files are skipped."""
    merge_base = git(git_dir, 'merge-base', base, head).strip()
    changed = [f for f in git(git_dir, 'diff', '-z', '--name-only', '--diff-filter=d', merge_base, head).split('\0') if f]
    marked = []
    for i in range(0, len(changed), 200):
        r = subprocess.run(['git', '-C', git_dir, 'grep', '-l', '-z', '-E', MARKER_RE, head, '--',
                            *(f':(literal){f}' for f in changed[i:i + 200])], capture_output=True, text=True)
        if r.returncode not in (0, 1):   # 1: no file matched
            raise Error(f'git grep: {r.stderr.strip()}')
        marked += [f.split(':', 1)[1] for f in r.stdout.split('\0') if f]
    return marked


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
    Anything else raises Error naming the token. The words are exact (lower
    case); only space around the value and around a list item is ignored, so
    a mistyped switch stops the run instead of reading as off."""
    raw = '' if value is None else value.strip()
    if raw in ('', 'off'):
        return {'value': raw, 'act': [], 'shadow': []}
    if raw == 'shadow':
        return {'value': raw, 'act': [], 'shadow': list(MERGE_UP_RULES)}
    known = f'off, shadow, or a comma list of {", ".join(MERGE_UP_RULES)}'
    tokens = [t.strip() for t in raw.split(',')]
    for t in tokens:
        if not t:
            raise Error(f'{AUTOMATION_VAR}={raw!r}: empty item in the list (expected {known})')
        if t not in MERGE_UP_RULES:
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
    p = sub.add_parser('person-no', help="why a merge-up PR is a person's no (closed by a person); prints nothing when it is not")
    p.add_argument('--state', required=True, help='the PR state: OPEN, CLOSED or MERGED')
    p.add_argument('--issue', help='CLOSED: the REST issue of the PR (closed_by)')
    p.add_argument('--comments', help='CLOSED: its REST issue comments')
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
                            read_json_stream(args.comments) if args.comments else None)
            if why:
                print(why)
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
            for f in marked:
                print(f)
            if marked:
                print(f'release_git.py: markers: {len(marked)} file(s) changed by {args.head[:7]} still hold conflict markers', file=sys.stderr)
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
            print(rule_mode(auto, args.rule) if args.rule else '\n'.join(automation_summary(auto)))
        return 0
    except (Error, OSError, ValueError) as e:
        print(f'release_git.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
