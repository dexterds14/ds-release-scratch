#!/usr/bin/env python3
"""The merge-up census as a command (ENG-2234). Read-only.

    merge_up_audit.py --since 2026-11-02                 the four repos, as text
    merge_up_audit.py --since 2026-11-02 --repo ds --json
    merge_up_audit.py --since 2026-11-02 --record audit.json   also save what was read
    merge_up_audit.py --replay audit.json                the same report from that file, no network

For each of the four repos that run release lines (DistributedSpectrum/
ds-tegra-distro, meta-dsensos, sensor-portal, ds) it lists

  - every merge-up PR opened since the date: a PR whose head branch is
    merge-up/<child>/<sha7>. Before the merge rules that is always a conflict
    PR. With them it can also be a PR a rule resolved ("(resolved by rule)")
    or the lockfile rule's PR ("(lockfile pin set by rule)"); `kind` says
    which, from the title.
  - every commit with a `Merge-Up-Rule:` trailer on a release line since the
    date, and whether it reached the line in one of those PRs or as a direct
    push (R-lock outcome A).

Per PR:

  class     from the conflicted files the PR body lists: never-auto (a file on
            that repo's never_auto list), lock-only (only kas/include/
            base.lock.yml), pins (ds-src-pins.inc), lock+other, other, or
            unknown when the body lists no file. `tags` has every one that
            applies; `class` is the first in that order.
  rules     the Merge-Up-Rule trailers on the PR's own commits.
  resolver  the people who authored a commit above the child tip the branch
            is named after (the bot's own commits aside). Nobody means the
            branch is as the bot left it.
  approver  the people whose latest review is APPROVED. A bot's review is
            listed apart and never counts (plan 6.1).
  on head   whether a counted approval was given on the PR's final head
            commit. "no" is an approval that predates the last push.
  second    whether an approver is someone other than a resolver.
  merged    bot finish (merged by an App), UI (merged by a person, named),
            indirect (GitHub marked it merged because its head reached the
            parent some other way, usually inside a later merge-up), bot
            finish with the PR left closed (the merge commit "Merge pull
            request #N" is on the parent, GitHub shows the PR closed), closed
            with its head in the parent all the same (superseded: a later
            merge-up carried the child), closed and not merged, or open.

It only reads: every request is `gh api --method GET repos/...`, and a request
that fails ends the run with exit 2 and no report, so a count is never printed
over a hole. The numbers come with what was examined (PRs listed, lines and
commits scanned). The report is the data source for Part 2's entry criteria.

The never_auto lists: ds-tegra-distro's and meta-dsensos's come from this
checkout's release/config.json (--root), sensor-portal's and ds's are the two
patterns their own config ships. Standard library and gh only.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import release_git  # noqa: E402  (sibling module: never_auto, merge_up_config, the lockfile path)

OWNER = 'DistributedSpectrum'
REPOS = ('ds-tegra-distro', 'meta-dsensos', 'sensor-portal', 'ds')
# sensor-portal and ds keep their own list in their own release/config.json;
# these are the two patterns both ship (plan 5.4).
APP_NEVER_AUTO = ('.github/workflows/**', '*sudoers*')
LOCKFILE = release_git.LOCKFILE
PINS_FILE = 'conf/distro/include/ds-src-pins.inc'
TRAILER = release_git.LOCK_RULE_TRAILER
BRANCH_RE = re.compile(r'merge-up/(?P<child>.+)/(?P<sha7>[0-9a-f]{7,40})')
LINE_RE = re.compile(r'(?:[A-Za-z0-9._-]+/)?(?:main|(?:0|[1-9][0-9]*)\.x\.x|(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.x)')
BODY_FILE_RE = re.compile(r'^\s*[-*] `([^`]+)`\s*$', re.M)
TRAILER_RE = re.compile(r'^' + re.escape(TRAILER) + r':[ \t]*(\S+)[ \t]*(.*)$', re.M)
MERGE_PR_RE = re.compile(r'Merge pull request #(\d+) ')
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
PATH_RE = re.compile(r'repos/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._/?&=%:+-]+')
KINDS = (('(resolved by rule)', 'rule-resolved'), ('(lockfile pin set by rule)', 'lock-rule'))
CLASS_ORDER = ('unknown', 'never-auto', 'lock-only', 'pins', 'lock+other', 'other')
PER_PAGE = 100
MAX_PAGES = 50


class Error(Exception):
    pass


# --- reading ---------------------------------------------------------------

def gh_argv(path):
    """The one shape of request this command makes: a GET of a repos/ path."""
    if not PATH_RE.fullmatch(path):
        raise Error(f'refusing to request {path!r}: not a repos/<owner>/<repo>/... path')
    return ['gh', 'api', '--method', 'GET', '-H', 'Accept: application/vnd.github+json', path]


def gh_get(path):
    """The parsed answer of one GET, or None for a 404."""
    p = subprocess.run(gh_argv(path), capture_output=True, text=True)
    if p.returncode != 0:
        if 'HTTP 404' in p.stderr:
            return None
        raise Error(f'gh api GET {path} failed (exit {p.returncode}): {p.stderr.strip()[:300]}')
    try:
        return json.loads(p.stdout)
    except ValueError as e:
        raise Error(f'gh api GET {path}: the answer is not JSON ({e})')


def _who(user):
    """{'login', 'bot'} of a GitHub user object, None when there is none."""
    if not user:
        return None
    login = user.get('login') or ''
    return {'login': login, 'bot': user.get('type') == 'Bot' or login.endswith('[bot]')}


def _trailers(message):
    return [[m.group(1), m.group(2).strip()] for m in TRAILER_RE.finditer(message or '')]


def slim_pulls(page):
    """A page of the PR listing: how many PRs it held, the oldest creation time
    on it, and the merge-up PRs."""
    items = [{'number': p['number'], 'title': p.get('title') or '', 'state': p['state'], 'created_at': p['created_at'],
              'closed_at': p.get('closed_at'), 'merged_at': p.get('merged_at'), 'author': _who(p.get('user')),
              'head_ref': p['head']['ref'], 'base_ref': p['base']['ref'], 'body': p.get('body') or ''}
             for p in page if BRANCH_RE.fullmatch(p['head']['ref'])]
    return {'n': len(page), 'oldest': min((p['created_at'] for p in page), default=''), 'items': items}


def slim_pull(p):
    return {'number': p['number'], 'state': p['state'], 'merged': bool(p.get('merged')), 'merged_by': _who(p.get('merged_by')),
            'head_sha': p['head']['sha'], 'commits': p.get('commits'), 'changed_files': p.get('changed_files')}


def slim_reviews(page):
    return [{'user': _who(r.get('user')), 'state': r.get('state'), 'commit_id': r.get('commit_id'),
             'submitted_at': r.get('submitted_at')} for r in page]


def slim_commit(c):
    """A commit of a PR: what the resolver and the rules are read from."""
    msg = c['commit'].get('message') or ''
    return {'sha': c['sha'], 'parents': [p['sha'] for p in c.get('parents') or []],
            'author': _who(c.get('author')), 'author_name': (c['commit'].get('author') or {}).get('name') or '',
            'trailers': _trailers(msg)}


def slim_commits(page):
    return [slim_commit(c) for c in page]


def slim_line_commits(page):
    """A page of a line's history: how many commits it held, the ones with a
    Merge-Up-Rule trailer, and who made the merge commit of each PR
    ("Merge pull request #N ...") as {N: {'sha', 'author', 'author_name'}}."""
    rule, merges = [], {}
    for c in page:
        msg = c['commit'].get('message') or ''
        one = slim_commit(c)
        if one['trailers']:
            rule.append({'sha': one['sha'], 'author': one['author'], 'author_name': one['author_name'],
                         'date': (c['commit'].get('committer') or {}).get('date') or '',
                         'subject': msg.split('\n', 1)[0], 'trailers': one['trailers']})
        pr = MERGE_PR_RE.match(msg)
        if pr:
            merges.setdefault(pr.group(1), {'sha': one['sha'], 'author': one['author'], 'author_name': one['author_name']})
    return {'n': len(page), 'rule': rule, 'merges': merges}


def slim_branches(page):
    return [b['name'] for b in page]


def slim_compare(doc):
    return {'status': doc.get('status')}


class Reader:
    """Every GET goes through here: fetched (or replayed), slimmed to the fields
    the audit reads, and kept under its path so a run can be recorded."""

    def __init__(self, fetch=gh_get, replay=None):
        self.fetch = fetch
        self.replay = replay
        self.responses = {}

    def get(self, path, slim, missing_ok=False):
        if self.replay is not None:
            if path not in self.replay:
                raise Error(f'replay: {path} is not in the recording (the audit makes no request in replay mode)')
            data = self.replay[path]
        else:
            raw = self.fetch(path)
            data = None if raw is None else slim(raw)
        self.responses[path] = data
        if data is None and not missing_ok:
            raise Error(f'GET {path}: not found (404). The token cannot read it, or it does not exist')
        return data

    def pages(self, path, slim, stop=None):
        """Every page of a list endpoint, as the slimmed pages. stop(page)
        ends the walk early (the PR listing stops at the date)."""
        out = []
        sep = '&' if '?' in path else '?'
        for n in range(1, MAX_PAGES + 1):
            page = self.get(f'{path}{sep}per_page={PER_PAGE}&page={n}', slim)
            out.append(page)
            count = page['n'] if isinstance(page, dict) else len(page)
            if count < PER_PAGE or (stop and stop(page)):
                return out
        raise Error(f'GET {path}: more than {MAX_PAGES} pages; narrow --since')


# --- one PR ----------------------------------------------------------------

def never_auto_patterns(repo, root=None):
    """The never_auto list that governs a repo's merge-up conflicts."""
    name = repo.rsplit('/', 1)[-1]
    if name in ('ds-tegra-distro', 'meta-dsensos'):
        try:
            cfg = release_git.merge_up_config(root)
        except release_git.Error as e:
            raise Error(str(e))
        return tuple(cfg['never_auto'] if name == 'ds-tegra-distro' else cfg['meta_dsensos']['never_auto'])
    return APP_NEVER_AUTO


def body_files(body):
    """The conflicted files a merge-up PR body lists (one "- `path`" line each)."""
    seen = []
    for path in BODY_FILE_RE.findall(body or ''):
        if path not in seen:
            seen.append(path)
    return seen


def classify(files, patterns):
    """(class, tags): every tag that applies, and the first one in CLASS_ORDER."""
    if not files:
        return 'unknown', ['unknown']
    tags = set()
    try:
        if any(release_git.never_auto(patterns, f) for f in files):
            tags.add('never-auto')
    except release_git.Error as e:
        raise Error(str(e))
    if files == [LOCKFILE]:
        tags.add('lock-only')
    elif LOCKFILE in files:
        tags.add('lock+other')
    if PINS_FILE in files:
        tags.add('pins')
    if not tags or any(f not in (LOCKFILE, PINS_FILE) and not release_git.never_auto(patterns, f) for f in files):
        tags.add('other')
    if 'lock+other' in tags and tags == {'lock+other', 'other'}:
        tags.discard('other')
    ordered = [t for t in CLASS_ORDER if t in tags]
    return ordered[0], ordered


def kind_of(title):
    for suffix, kind in KINDS:
        if title.rstrip().endswith(suffix):
            return kind
    return 'conflict'


def commit_person(c):
    """Who authored a commit: the login, or the git author name when the commit
    is not linked to an account. None for a bot."""
    if c['author']:
        return None if c['author']['bot'] else c['author']['login']
    name = c['author_name']
    return None if name.endswith('[bot]') else (name or '?')


def above(commits, sha7):
    """The commits of a PR that descend from the child tip <sha7> (the tip
    aside), in list order. None when the tip is not among them."""
    by_sha = {c['sha']: c for c in commits}
    tips = [c['sha'] for c in commits if c['sha'].startswith(sha7)]
    if len(tips) != 1:
        return None
    tip, memo = tips[0], {}

    def descends(sha):
        stack, seen = [sha], set()
        while stack:
            cur = stack.pop()
            if cur == tip or memo.get(cur):
                memo[sha] = True
                return True
            if cur in seen or cur not in by_sha or memo.get(cur) is False:
                continue
            seen.add(cur)
            stack.extend(by_sha[cur]['parents'])
        memo[sha] = False
        return False

    return [c for c in commits if c['sha'] != tip and descends(c['sha'])]


def approvals(reviews):
    """(people, bots): each person's latest verdict when it is APPROVED, as
    {'login', 'commit_id'}; and the logins of bots that approved."""
    latest, bots = {}, []
    for r in sorted(reviews, key=lambda r: r.get('submitted_at') or ''):
        user = r['user']
        if not user or r['state'] not in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            continue
        if user['bot']:
            if r['state'] == 'APPROVED' and user['login'] not in bots:
                bots.append(user['login'])
            continue
        latest[user['login']] = r
    people = [{'login': login, 'commit_id': r['commit_id']} for login, r in latest.items() if r['state'] == 'APPROVED']
    return people, bots


def how_merged(listed, detail, merges, contained):
    """How the PR ended. merges: {PR number: the "Merge pull request #N" commit
    on a line}. contained(): whether the parent holds the PR head (asked only
    for a closed PR that nothing else explains)."""
    if detail['state'] == 'open':
        return 'open', None
    if detail['merged']:
        by = detail['merged_by']
        if not by:
            return 'indirect', None
        return ('bot finish', by['login']) if by['bot'] else ('UI', by['login'])
    commit = merges.get(listed['number'])
    if commit:
        who = commit['author']
        if who and who['bot']:
            return 'bot finish (PR left closed)', who['login']
        return 'UI (PR left closed)', commit_person(commit)
    state = contained()
    if state is None:
        return 'closed, head not found', None
    return ('closed, head reached the parent elsewhere' if state else 'closed, not merged'), None


def audit_pr(reader, repo, listed, patterns, merges):
    n = listed['number']
    m = BRANCH_RE.fullmatch(listed['head_ref'])
    detail = reader.get(f'repos/{repo}/pulls/{n}', slim_pull)
    reviews = [r for page in reader.pages(f'repos/{repo}/pulls/{n}/reviews', slim_reviews) for r in page]
    commits = [c for page in reader.pages(f'repos/{repo}/pulls/{n}/commits', slim_commits) for c in page]
    files = body_files(listed['body'])
    cls, tags = classify(files, patterns)
    notes = []
    if detail['commits'] is not None and detail['commits'] != len(commits):
        notes.append(f'the API lists {len(commits)} of {detail["commits"]} commits')
    top = above(commits, m.group('sha7'))
    if top is None:
        resolvers = None
        notes.append(f'the child tip {m.group("sha7")} is not among the listed commits: resolver unknown')
    else:
        resolvers = []
        for c in top:
            who = commit_person(c)
            if who and who not in resolvers:
                resolvers.append(who)
    people, bots = approvals(reviews)
    head = detail['head_sha']

    def contained():
        doc = reader.get(f'repos/{repo}/compare/{listed["base_ref"]}...{head}', slim_compare, missing_ok=True)
        return None if doc is None else doc['status'] in ('behind', 'identical')

    how, by = how_merged(listed, detail, merges, contained)
    approvers = [p['login'] for p in people]
    return {
        'repo': repo, 'number': n, 'title': listed['title'], 'kind': kind_of(listed['title']),
        'child': m.group('child'), 'parent': listed['base_ref'], 'created_at': listed['created_at'],
        'closed_at': listed['closed_at'], 'state': detail['state'], 'opened_by': (listed['author'] or {}).get('login'),
        'files': files, 'class': cls, 'tags': tags,
        'rules': sorted({t[0] for c in commits for t in c['trailers']}),
        'resolvers': resolvers, 'approvers': approvers, 'bot_approvals': bots,
        'approval_on_head': (any(p['commit_id'] == head for p in people) if people else None),
        'second_person': (None if not people or resolvers is None else any(a not in resolvers for a in approvers)),
        'merged_how': how, 'merged_by': by, 'head': head, 'notes': notes,
        '_shas': [c['sha'] for c in commits],
    }


# --- one repo --------------------------------------------------------------

def audit_repo(reader, repo, since, root=None):
    patterns = never_auto_patterns(repo, root)
    stamp = f'{since}T00:00:00Z'

    # the lines' history first: the rule commits, and the merge commits that say
    # how a PR GitHub shows closed was really merged
    branches = [b for page in reader.pages(f'repos/{repo}/branches', slim_branches) for b in page]
    lines = sorted(b for b in branches if LINE_RE.fullmatch(b))
    scanned, on_line, merges = 0, {}, {}
    for line in lines:
        for page in reader.pages(f'repos/{repo}/commits?sha={line}&since={stamp}', slim_line_commits):
            scanned += page['n']
            for c in page['rule']:
                on_line.setdefault(c['sha'], dict(c, lines=[]))['lines'].append(line)
            for number, c in page['merges'].items():
                merges.setdefault(int(number), c)

    listed_total, listed = 0, []
    pages = reader.pages(f'repos/{repo}/pulls?state=all&sort=created&direction=desc', slim_pulls,
                         stop=lambda page: page['oldest'] < stamp)
    for page in pages:
        listed_total += page['n']
        listed.extend(p for p in page['items'] if p['created_at'] >= stamp)
    prs = [audit_pr(reader, repo, p, patterns, merges) for p in listed]

    in_pr = {sha: pr['number'] for pr in prs for sha in pr['_shas']}
    rule_commits = []
    for sha, c in on_line.items():
        rule_commits.append({'repo': repo, 'sha': sha, 'date': c['date'], 'lines': c['lines'], 'subject': c['subject'],
                             'author': (c['author'] or {}).get('login') or c['author_name'],
                             'rules': [{'rule': t[0], 'detail': t[1]} for t in c['trailers']],
                             'pr': in_pr.get(sha), 'landed': 'in PR' if sha in in_pr else 'direct push'})
    rule_commits.sort(key=lambda c: c['date'], reverse=True)
    for pr in prs:
        del pr['_shas']
    return {'repo': repo, 'since': since, 'prs': prs, 'rule_commits': rule_commits, 'counts': counts(prs, rule_commits),
            'examined': {'prs_listed': listed_total, 'pr_pages': len(pages), 'lines': lines, 'line_commits': scanned}}


def counts(prs, rule_commits):
    def tally(key):
        out = {}
        for pr in prs:
            out[pr[key]] = out.get(pr[key], 0) + 1
        return dict(sorted(out.items()))
    return {
        'prs': len(prs), 'by_kind': tally('kind'), 'by_class': tally('class'), 'by_merge': tally('merged_how'),
        'resolved_by_a_person': sum(1 for p in prs if p['resolvers']),
        'approved_by_a_person': sum(1 for p in prs if p['approvers']),
        'approved_by_a_second_person': sum(1 for p in prs if p['second_person']),
        'approved_only_by_the_resolver': sum(1 for p in prs if p['second_person'] is False),
        'approval_not_on_head': sum(1 for p in prs if p['approval_on_head'] is False),
        'bot_approval_only': sum(1 for p in prs if p['bot_approvals'] and not p['approvers']),
        'rule_commits': len(rule_commits),
        'rule_commits_direct_push': sum(1 for c in rule_commits if c['landed'] == 'direct push'),
    }


def audit(reader, repos, since, root=None):
    return {'since': since, 'repos': [audit_repo(reader, repo, since, root) for repo in repos]}


# --- output ----------------------------------------------------------------

def _names(names, none='-'):
    if names is None:
        return '?'
    return ','.join(names) if names else none


def _yes(value):
    return {True: 'yes', False: 'no', None: '-'}[value]


def render(report):
    out = []
    for r in report['repos']:
        ex, c = r['examined'], r['counts']
        out.append(f'== {r["repo"]}: {c["prs"]} merge-up PR(s) opened since {r["since"]}, '
                   f'{c["rule_commits"]} {TRAILER} commit(s)')
        out.append(f'   examined: {ex["prs_listed"]} PR(s) listed on {ex["pr_pages"]} page(s); '
                   f'{ex["line_commits"]} commit(s) on {len(ex["lines"])} line(s) ({", ".join(ex["lines"]) or "none"})')
        for p in r['prs']:
            merged = p['merged_how'] + (f' by {p["merged_by"]}' if p['merged_by'] else '')
            out.append(f'  #{p["number"]} {p["created_at"][:10]} {p["child"]} -> {p["parent"]}  {p["kind"]}  class={p["class"]}'
                       + (f' ({",".join(p["tags"])})' if len(p['tags']) > 1 else '')
                       + (f'  rules={",".join(p["rules"])}' if p['rules'] else ''))
            out.append(f'       resolver={_names(p["resolvers"])}  approver={_names(p["approvers"])}'
                       + (f' (bot, not counted: {",".join(p["bot_approvals"])})' if p['bot_approvals'] else '')
                       + f'  on head={_yes(p["approval_on_head"])}  second person={_yes(p["second_person"])}  merged: {merged}')
            out.append(f'       files: {", ".join(p["files"]) or "none listed in the PR body"}')
            for note in p['notes']:
                out.append(f'       note: {note}')
        for k in r['rule_commits']:
            rules = '; '.join(f'{x["rule"]} {x["detail"]}'.strip() for x in k['rules'])
            via = f'in PR #{k["pr"]}' if k['pr'] else 'direct push'
            out.append(f'  {k["sha"][:7]} {k["date"][:10]} on {",".join(k["lines"])}  {via}  {rules}  | {k["subject"]}')
        out.append('   counts: ' + '; '.join(
            [f'kind {_tally(c["by_kind"])}', f'class {_tally(c["by_class"])}', f'merged {_tally(c["by_merge"])}']))
        out.append(f'   people: resolved by a person {c["resolved_by_a_person"]}; approved by a person {c["approved_by_a_person"]}; '
                   f'by a second person {c["approved_by_a_second_person"]}; only by the resolver {c["approved_only_by_the_resolver"]}; '
                   f'approval not on the head {c["approval_not_on_head"]}; bot approval only {c["bot_approval_only"]}')
        out.append('')
    total = sum(r['counts']['prs'] for r in report['repos'])
    out.append(f'total: {total} merge-up PR(s) since {report["since"]} ('
               + ', '.join(f'{r["repo"].rsplit("/", 1)[-1]} {r["counts"]["prs"]}' for r in report['repos']) + '); '
               + f'{sum(r["counts"]["rule_commits"] for r in report["repos"])} {TRAILER} commit(s)')
    return '\n'.join(out)


def _tally(d):
    return ', '.join(f'{k}={v}' for k, v in d.items()) or 'none'


# --- CLI -------------------------------------------------------------------

def repo_name(arg):
    """DistributedSpectrum/<one of the four>, from that or the bare name."""
    name = arg[len(OWNER) + 1:] if arg.startswith(OWNER + '/') else arg
    if name not in REPOS:
        raise Error(f'--repo {arg}: not one of {", ".join(f"{OWNER}/{r}" for r in REPOS)}')
    return f'{OWNER}/{name}'


def main(argv=None):
    ap = argparse.ArgumentParser(description='List merge-up PRs and Merge-Up-Rule commits since a date. Read-only.')
    ap.add_argument('--since', help='YYYY-MM-DD (UTC): PRs opened and line commits made on or after it')
    ap.add_argument('--repo', action='append', help='one of the four repos, name or owner/name; repeatable; default all four')
    ap.add_argument('--json', action='store_true', help='print the report as JSON')
    ap.add_argument('--record', metavar='FILE', help='also write what was read (slimmed) to FILE, for --replay')
    ap.add_argument('--replay', metavar='FILE', help='report from a --record file; makes no request')
    ap.add_argument('--root', help='the checkout whose release/config.json holds the never_auto lists (default: this one)')
    args = ap.parse_args(argv)
    try:
        replay = None
        since, repos = args.since, args.repo
        if args.replay:
            if args.record:
                raise Error('--replay and --record do not go together')
            with open(args.replay) as f:
                doc = json.load(f)
            replay = doc['responses']
            since = since or doc['since']
            repos = repos or doc['repos']
            if since != doc['since']:
                raise Error(f'--since {since}: the recording was made with --since {doc["since"]}')
        if not since or not DATE_RE.fullmatch(since):
            raise Error('--since YYYY-MM-DD is required')
        datetime.date.fromisoformat(since)
        repos = [repo_name(r) for r in (repos or REPOS)]
        reader = Reader(replay=replay)
        report = audit(reader, repos, since, args.root)
        if args.record:
            with open(args.record, 'w') as f:
                json.dump({'since': since, 'repos': repos,
                           'recorded_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                           'responses': reader.responses}, f, indent=1, sort_keys=True)
                f.write('\n')
    except (Error, OSError, ValueError, KeyError) as e:
        print(f'merge_up_audit: {e}', file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if args.json else render(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
