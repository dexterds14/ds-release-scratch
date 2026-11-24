"""Tests for release_git.py, lib.sh's naming helpers, create-branch.sh and merge-up.sh (ENG-1933, ENG-1935, ENG-2110).

One bare repo in a temp dir stands in for origin (trunk `main`, product ds,
`product_prefix: false`, `.github/yocto-ci.env` as the bookkeeping file);
the scripts run from a clone of it. A stub `gh` on PATH answers `run list`
from a list of green shas, records `pr create`, serves `pr view` from a
per-PR review file and performs `pr merge` in the bare repo. No network,
nothing touches this checkout, and no git identity is configured: the
fixture sets GIT_AUTHOR_* / GIT_COMMITTER_* for its own commits and the
scripts fall back to lib.sh's bot identity.
"""
import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import version  # noqa: E402

CREATE = os.path.join(SCRIPTS, 'create-branch.sh')
MERGE_UP = os.path.join(SCRIPTS, 'merge-up.sh')
REPO = 'ds/repo'
ENV_FILE = '.github/yocto-ci.env'
CONFIG = {'products': ['ds'], 'product_prefix': False, 'bookkeeping_files': [ENV_FILE]}
MATRIX = {'products': {'ds': {'flavors': ['ds_ds-arm64_release']}}}
PREFIXED = ['vertex', 'aggregator']

# How GitHub reads a gh api endpoint: one normaliser, used by the stub (pasted into it below) and by the checks
# on its log (api_parts, unallowed_writes, merge_calls, the static pins). Round 4 review, finding 2: GitHub
# answers repos/R/pulls/101/merge#x, https://API.GITHUB.COM/..., https://api.github.com:443/... and /graphql as the
# plain endpoint (read-only probes), so every check matches on what this returns, not on the spelling.
API_PATH_SRC = r'''
API_HOST = re.compile(r'https?://api\.github\.com(:443)?/', re.I)

def api_parts(path):
    """(path, query) of a gh api endpoint as GitHub reads it: without the https://api.github.com/ prefix (any
    case, with or without :443) or leading /, without a #fragment, and the query string apart."""
    m = API_HOST.match(path)
    if m:
        path = path[m.end():]
    path, _, query = path.lstrip('/').partition('#')[0].partition('?')
    return path, query
'''
exec(API_PATH_SRC)

GH_STUB = r'''#!/usr/bin/env python3
"""Stub gh for the merge-up and create-branch tests."""
import json, os, re, subprocess, sys, tempfile, time
STATE = os.environ['GH_STUB_STATE']
REPOS = dict(kv.split('=', 1) for kv in os.environ['GH_STUB_REPOS'].split(','))
PRS = os.path.join(STATE, 'prs.json')
FLAGS = {'--merge', '--squash', '--rebase', '--draft', '--prerelease', '--delete-branch', '--paginate'}
BOT = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}
#API_PATH#
# the flags of gh api that send fields, and so make it a POST when no method is named
FIELD_FLAGS = re.compile(r'(-[fF]|--field|--raw-field|--input)(=|$)|-[fF].')

def parse(argv):
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in FLAGS:
            opts[a] = True; i += 1
        elif a.startswith('--') and '=' in a:
            k, v = a.split('=', 1)
            opts[k] = v; i += 1
        elif a.startswith('-'):
            opts[a] = argv[i + 1]; i += 2
        else:
            pos.append(a); i += 1
    # gh api takes the method as -X or --method: one spelling for the handlers below
    if '--method' in opts:
        opts.setdefault('-X', opts['--method'])
    return pos, opts

def load():
    return json.load(open(PRS)) if os.path.exists(PRS) else []

def save(prs):
    json.dump(prs, open(PRS, 'w'))

def rev(repo, ref):
    return subprocess.check_output(['git', '-C', REPOS[repo], 'rev-parse', '--verify', ref + '^{commit}'], text=True).strip()

def view_doc(pr, consume=True):
    doc = {'number': pr['number'], 'url': pr['url'], 'state': pr['state'], 'headRefName': pr['head'], 'title': pr['title'],
           'baseRefName': pr['base'], 'headRefOid': rev(pr['repo'], 'refs/heads/' + pr['head']) if pr['state'] == 'OPEN' else pr.get('merged_head', ''),
           'reviewDecision': None, 'mergeStateStatus': 'BLOCKED', 'statusCheckRollup': [], 'reviews': [],
           'commits': [{'authors': [{'login': 'resolver'}]}], 'isDraft': bool(pr.get('draft')), 'body': pr.get('body', '')}
    override = os.path.join(STATE, 'pr-%d-review.json' % pr['number'])
    if os.path.exists(override):
        extra = json.load(open(override))
        if isinstance(extra, list):
            # a list is a sequence of answers: each view takes the next one, the last one stays
            if len(extra) > 1 and consume:
                json.dump(extra[1:], open(override, 'w'))
            extra = extra[0]
        doc.update(extra)
    return doc

# The sources of the shipped release/config.json (merge_up check_sources): what the stub's check runs
# of a head come from when the test names none (check-runs.json, head-runs.json, jobs-<id>.json).
SOURCES = (('pr-checks', 'workflow', '.github/workflows/pr-checks.yml'),)

def open_prs_at(repo, sha):
    """The open PRs of <repo> whose head branch is at <sha>."""
    out = []
    for p in load():
        if p['repo'] == repo and p['state'] == 'OPEN':
            try:
                if rev(repo, 'refs/heads/' + p['head']) == sha:
                    out.append(p)
            except subprocess.CalledProcessError:
                pass
    return out

def default_check_runs(repo, sha):
    """Every check of SOURCES, green on <sha> and from its source: a workflow's as job 3000+i of
    Actions run 2000+i (check suite 1000+i) for a pull request on the open PR branch at <sha>. As
    GitHub lists a run, its pull_requests are the open PRs of that branch, with their bases."""
    runs, actions = [], []
    prs = open_prs_at(repo, sha)
    for i, (name, kind, value) in enumerate(SOURCES):
        runs.append({'id': 3000 + i, 'name': name, 'head_sha': sha, 'status': 'completed', 'conclusion': 'success',
                     'started_at': '2026-11-20T11:30:00Z', 'completed_at': '2026-11-20T11:40:00Z',
                     'app': {'slug': 'github-actions' if kind == 'workflow' else value}, 'check_suite': {'id': 1000 + i}})
        if kind == 'workflow':
            actions.append({'id': 2000 + i, 'path': value, 'event': 'pull_request', 'head_branch': prs[0]['head'] if prs else None,
                            'head_sha': sha, 'status': 'completed', 'conclusion': 'success', 'check_suite_id': 1000 + i,
                            'head_repository': {'full_name': repo},
                            'pull_requests': [{'number': p['number'], 'head': {'ref': p['head'], 'sha': sha}, 'base': {'ref': p['base']}}
                                              for p in prs]})
    return runs, actions

def merge_pr(prs, pr, repo, n, head):
    """GitHub's merge of PR n at head into its base, in the bare repo; the merge commit's sha."""
    tmp = tempfile.mkdtemp()
    env = dict(os.environ, GIT_AUTHOR_NAME='gh', GIT_AUTHOR_EMAIL='gh@x', GIT_COMMITTER_NAME='gh', GIT_COMMITTER_EMAIL='gh@x')
    subprocess.check_call(['git', 'clone', '-q', REPOS[repo], tmp], env=env)
    subprocess.check_call(['git', '-C', tmp, 'checkout', '-q', pr['base']], env=env)
    subprocess.check_call(['git', '-C', tmp, 'merge', '-q', '--no-ff', '-m', 'Merge pull request #%d' % n, head], env=env)
    subprocess.check_call(['git', '-C', tmp, 'push', '-q', 'origin', pr['base']], env=env)
    sha = subprocess.check_output(['git', '-C', tmp, 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['rm', '-rf', tmp])
    # leave-open-after-merge: the base holds the merge, and GitHub still lists the PR as open
    if not os.path.exists(os.path.join(STATE, 'leave-open-after-merge')):
        pr['state'] = 'MERGED'; pr['merged_head'] = head
    save(prs)
    return sha

def main(argv):
    # the log: each call's argv as one JSON array per line (round 5 review, finding 1: joined by spaces, an argument
    # with a space in it could put an allowed endpoint in front of the real one). Fixture.gh_log renders it for people.
    with open(os.path.join(STATE, 'log'), 'a') as f:
        f.write(json.dumps(argv) + '\n')
    pos, opts = parse(argv)
    # -R/--repo, else the repo the workflow would name, else the one repo configured
    repo = opts.get('-R') or opts.get('--repo') or os.environ.get('GITHUB_REPOSITORY') or next(iter(REPOS))
    # a gh api call: the endpoint as GitHub reads it (api_parts) and its method, POST when fields are sent
    # with none named, as gh does
    api = pos[:1] == ['api'] and len(pos) > 1
    path, query = api_parts(pos[1]) if api else ('', '')
    method = (opts.get('-X') or ('POST' if any(FIELD_FLAGS.match(a) for a in argv) else 'GET')).upper() if api else None
    if pos[:2] == ['run', 'list']:
        green = set(open(os.environ['GH_STUB_GREEN']).read().split()) if os.environ.get('GH_STUB_GREEN') else set()
        sha = opts.get('--commit') or opts.get('-c')
        print(json.dumps([{'databaseId': 1234, 'number': 56}] if sha in green else []))
        return 0
    if pos[:2] == ['pr', 'create']:
        prs = load()
        if any(p['repo'] == repo and p['head'] == opts['--head'] and p['state'] == 'OPEN' for p in prs):
            print('a pull request for branch "%s" into branch "%s" already exists' % (opts['--head'], opts['--base']), file=sys.stderr)
            return 1
        n = len(prs) + 1
        pr = {'number': n, 'repo': repo, 'base': opts['--base'], 'head': opts['--head'], 'title': opts['--title'],
              'body': open(opts['--body-file']).read(), 'state': 'OPEN', 'url': 'https://github.com/%s/pull/%d' % (repo, n)}
        prs.append(pr); save(prs)
        print(pr['url'])
        return 0
    if pos[:2] == ['pr', 'list']:
        hits = [p for p in load() if p['repo'] == repo and p['base'] == opts.get('--base', p['base'])
                and p['head'] == opts.get('--head', p['head']) and opts.get('--state', 'open').upper() in (p['state'], 'ALL')]
        if os.path.exists(os.path.join(STATE, 'fail-pr-list-all')) and opts.get('--state') == 'all':
            print('HTTP 502: Bad Gateway', file=sys.stderr)
            return 1
        # like gh: newest first, 30 unless --limit says otherwise
        hits = sorted(hits, key=lambda p: -p['number'])[:int(opts.get('--limit', 30))]
        if 'isCrossRepository' in opts.get('--json', ''):
            # the auto-finish sweep's list (ENG-2235): read without taking a pr view answer.
            # fail-pr-list-open makes it fail.
            if os.path.exists(os.path.join(STATE, 'fail-pr-list-open')):
                print('HTTP 502: Bad Gateway', file=sys.stderr)
                return 1
            print(json.dumps([{'number': p['number'], 'headRefName': p['head'], 'baseRefName': p['base'],
                               'headRefOid': rev(p['repo'], 'refs/heads/' + p['head']), 'isCrossRepository': bool(p.get('cross')),
                               'isDraft': bool(p.get('draft'))} for p in hits]))
            return 0
        if opts.get('--jq') == '.[0].url':
            print(hits[0]['url'] if hits else '')
        elif opts.get('--jq') == '.[] | "\\(.number) \\(.headRefName)"':
            for p in hits:
                print('%d %s' % (p['number'], p['head']))
        elif opts.get('--jq') == '.[] | "\\(.number) \\(.headRefName) \\(.headRefOid)"':
            for p in hits:
                print('%d %s %s' % (p['number'], p['head'], rev(p['repo'], 'refs/heads/' + p['head'])))
        elif opts.get('--jq') == '.[] | "\\(.number) \\(.state) \\(.headRefName) \\(.url)"':
            # the merge-up PRs a bot finish asks about (ENG-2235): read without taking a pr view answer.
            # fail-pr-list-siblings makes it fail.
            if os.path.exists(os.path.join(STATE, 'fail-pr-list-siblings')):
                print('HTTP 502: Bad Gateway', file=sys.stderr)
                return 1
            for p in hits:
                print('%d %s %s %s' % (p['number'], p['state'], p['head'], p['url']))
        else:
            print(json.dumps([view_doc(p) for p in hits]))
        return 0
    if pos[:2] == ['pr', 'view']:
        n = int(pos[2])
        hook = os.path.join(STATE, 'pr-view-hook')
        if os.path.exists(hook):
            # something that happens between two reads of a PR (a push, a moved base, a review): run once
            os.rename(hook, hook + '.ran')
            subprocess.check_call(['sh', hook + '.ran'])
        match = [p for p in load() if p['repo'] == repo and p['number'] == n]
        if not match:
            print('no PR %d in %s' % (n, repo), file=sys.stderr)
            return 1
        doc = view_doc(match[0])
        fields = opts.get('--json', '').split(',') if opts.get('--json') else list(doc)
        doc = {k: doc.get(k) for k in fields}
        jq = opts.get('--jq')
        if jq:
            print(doc[jq.lstrip('.')])
        else:
            print(json.dumps(doc))
        return 0
    if pos[:2] == ['pr', 'close']:
        n = int(pos[2])
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        if pr['state'] != 'OPEN' or os.path.exists(os.path.join(STATE, 'fail-pr-close')):
            print('PR not open', file=sys.stderr)
            return 1
        pr['state'] = 'CLOSED'; pr['close_comment'] = opts.get('--comment', '')
        # the stub closes as the release App does: closed_by and the comment are the bot's
        pr['closed_by'] = dict(BOT)
        pr.setdefault('issue_comments', []).append({'user': dict(BOT), 'body': pr['close_comment']})
        save(prs)
        return 0
    if pos[:2] == ['pr', 'edit']:
        n = int(pos[2])
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        if os.path.exists(os.path.join(STATE, 'fail-pr-edit')):
            print('GraphQL: Resource not accessible by integration (updatePullRequest)', file=sys.stderr)
            return 1
        pr['title'] = opts.get('--title', pr['title'])
        save(prs)
        return 0
    if pos[:2] == ['pr', 'comment']:
        n = int(pos[2])
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        if os.path.exists(os.path.join(STATE, 'fail-pr-comment')):
            print('HTTP 502: Bad Gateway', file=sys.stderr)
            return 1
        pr.setdefault('comments', []).append(opts.get('--body', ''))
        # as GitHub lists it among the issue comments: by the release App, with a time
        pr.setdefault('issue_comments', []).append({'user': dict(BOT), 'body': opts.get('--body', ''),
                                                    'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())})
        save(prs)
        return 0
    if pos[:2] == ['pr', 'merge']:
        n = int(pos[2])
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        if pr['state'] != 'OPEN':
            print('PR not open', file=sys.stderr)
            return 1
        head = rev(repo, 'refs/heads/' + pr['head'])
        if opts.get('--match-head-commit') and opts['--match-head-commit'] != head:
            print('Head branch was modified. Review and try the merge again.', file=sys.stderr)
            return 1
        merge_pr(prs, pr, repo, n, head)
        hook = os.path.join(STATE, 'pr-merge-hook')
        if os.path.exists(hook):
            # something that happens right after the merge (a push to the base): run once
            os.rename(hook, hook + '.ran')
            subprocess.check_call(['sh', hook + '.ran'])
        print('merged')
        return 0
    if api and method == 'PUT' and re.fullmatch(r'repos/.+/pulls/\d+/merge', path):
        # the REST merge: gh api -X PUT repos/R/pulls/N/merge -f merge_method=merge -f sha=S (or --method PUT, any
        # spelling api_parts reads as that path, the fields in the query string: the same request on GitHub, so the
        # stub performs it too). Like gh,
        # the body goes to stdout whatever the status, and "gh: <message> (HTTP <code>)" to stderr.
        # It merges for the bot (a bypass actor of the ruleset), whatever mergeStateStatus says.
        # rest-merge-error.json, {"status", "message"}, is GitHub refusing it (405, 409, 422).
        fields = dict(kv.split('=', 1) for kv in query.split('&') if '=' in kv)
        fields.update(dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a in ('-f', '-F')))
        repo, n = re.fullmatch(r'repos/(.+)/pulls/(\d+)/merge', path).groups()
        n = int(n)
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        doc = 'https://docs.github.com/rest/pulls/pulls#merge-a-pull-request'
        def refuse(status, message):
            sys.stdout.write(json.dumps({'message': message, 'documentation_url': doc, 'status': status}))
            print('gh: %s (HTTP %s)' % (message, status), file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'rest-merge-error.json')
        if os.path.exists(path):
            err = json.load(open(path))
            return refuse(err['status'], err['message'])
        if pr['state'] != 'OPEN':
            return refuse('405', 'Pull Request is not mergeable')
        head = rev(repo, 'refs/heads/' + pr['head'])
        if fields.get('sha') and fields['sha'] != head:
            return refuse('409', 'Head branch was modified. Review and try the merge again.')
        hook = os.path.join(STATE, 'rest-merge-hook')
        if os.path.exists(hook):
            # something that happens right before GitHub merges (a push to the base): run once
            os.rename(hook, hook + '.ran')
            subprocess.check_call(['sh', hook + '.ran'])
            prs = load()
            pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        sha = merge_pr(prs, pr, repo, n, head)
        sys.stdout.write(json.dumps({'sha': sha, 'merged': True, 'message': 'Pull Request successfully merged'}))
        return 0
    m = re.fullmatch(r'repos/([^/]+/[^/]+)/pulls', path) if api and query else None
    if m and method == 'GET':
        # the pull requests of one head branch (2c re-review): GET repos/R/pulls?state=all&head=<owner>:<branch>, as
        # REST lists them, from the PRs the stub holds, then head-prs-extra.json (REST entries; another PR a test
        # adds without it taking part anywhere else). head-prs-extra-next.json replaces that file after a read: what a
        # person does between two reads. fail-head-prs makes it fail; fail-head-prs-next, every read after this one.
        if os.path.exists(os.path.join(STATE, 'fail-head-prs')):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        if os.path.exists(os.path.join(STATE, 'fail-head-prs-next')):
            os.replace(os.path.join(STATE, 'fail-head-prs-next'), os.path.join(STATE, 'fail-head-prs'))
        repo, query = m.group(1), dict(kv.split('=', 1) for kv in query.split('&') if '=' in kv)
        owner, _, branch = query.get('head', '').partition(':')
        if query.get('state') != 'all' or owner != repo.split('/')[0] or not branch:
            print('stub gh: unsupported pulls query: ' + pos[1], file=sys.stderr)
            return 1
        # the PRs the stub holds were opened by gh pr create, which runs as the release App; a PR a person opened
        # comes from head-prs-extra.json, with its user
        out = [{'number': p['number'], 'state': 'open' if p['state'] == 'OPEN' else 'closed', 'user': dict(p.get('user') or BOT),
                'head': {'ref': p['head'], 'repo': {'full_name': repo}}, 'base': {'ref': p['base']}}
               for p in load() if p['repo'] == repo and p['head'] == branch]
        path = os.path.join(STATE, 'head-prs-extra.json')
        extra = json.load(open(path)) if os.path.exists(path) else []
        out += [e for e in extra if (e.get('head') or {}).get('ref') == branch and ((e.get('head') or {}).get('repo') or {}).get('full_name') == repo]
        later = os.path.join(STATE, 'head-prs-extra-next.json')
        if os.path.exists(later):
            os.replace(later, path)
        print(json.dumps(sorted(out, key=lambda e: -e['number'])))
        return 0
    m = re.fullmatch(r'repos/(.+)/issues/(\d+)/events', path) if api else None
    if m and method == 'GET':
        # the issue events of a PR (2c re-review): issue-events.json maps "<owner>/<name>#<n>" to REST events
        # ({"event": "base_ref_changed", ...}); none by default. issue-events-next.json replaces that file after a
        # read: what a person does between two reads (round 4 review, finding 3). fail-issue-events makes it fail;
        # fail-issue-events-next, every read after this one; fail-issue-events-<n>, every read of PR n's, and
        # fail-issue-events-<n>-next, every read of PR n's after this one.
        if os.path.exists(os.path.join(STATE, 'fail-issue-events')) or os.path.exists(os.path.join(STATE, 'fail-issue-events-' + m.group(2))):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        if os.path.exists(os.path.join(STATE, 'fail-issue-events-next')):
            os.replace(os.path.join(STATE, 'fail-issue-events-next'), os.path.join(STATE, 'fail-issue-events'))
        if os.path.exists(os.path.join(STATE, 'fail-issue-events-%s-next' % m.group(2))):
            os.replace(os.path.join(STATE, 'fail-issue-events-%s-next' % m.group(2)), os.path.join(STATE, 'fail-issue-events-' + m.group(2)))
        events = os.path.join(STATE, 'issue-events.json')
        print(json.dumps((json.load(open(events)) if os.path.exists(events) else {}).get('%s#%s' % (m.group(1), m.group(2)), [])))
        later = os.path.join(STATE, 'issue-events-next.json')
        if os.path.exists(later):
            os.replace(later, events)
        return 0
    m = re.fullmatch(r'repos/(.+)/issues/(\d+)(/comments)?', path) if api else None
    if m and method == 'GET':
        # the REST issue of a PR (who closed it: the PR's closed_by, null when nothing recorded
        # it) and its issue comments (issue_comments: [{user: {login, type}, body}])
        if os.path.exists(os.path.join(STATE, 'fail-issues')):
            print('HTTP 502: Bad Gateway', file=sys.stderr)
            return 1
        match = [p for p in load() if p['repo'] == m.group(1) and p['number'] == int(m.group(2))]
        if not match:
            print('HTTP 404: Not Found', file=sys.stderr)
            return 1
        pr = match[0]
        print(json.dumps(pr.get('issue_comments', []) if m.group(3) else
                         {'number': pr['number'], 'state': 'open' if pr['state'] == 'OPEN' else 'closed', 'closed_by': pr.get('closed_by')}))
        return 0
    m = re.fullmatch(r'repos/(.+)/pulls/(\d+)/reviews', path) if api else None
    if m and method == 'GET':
        # the REST reviews of a PR. pr-N-rest-reviews.json is the answer as it is; otherwise the
        # reviews of the pr view answer in REST shape, on the PR's current head: a person, unless
        # the login is one gh reports for a bot
        repo, n = m.group(1), int(m.group(2))
        if os.path.exists(os.path.join(STATE, 'fail-reviews')):
            print('HTTP 502: Bad Gateway', file=sys.stderr)
            return 1
        match = [p for p in load() if p['repo'] == repo and p['number'] == n]
        if not match:
            print('HTTP 404: Not Found', file=sys.stderr)
            return 1
        exact = os.path.join(STATE, 'pr-%d-rest-reviews.json' % n)
        if os.path.exists(exact):
            print(open(exact).read())
            # pr-N-rest-reviews-next.json: what a person does after this read (a change-request, a
            # dismissal): every later read answers it
            later = os.path.join(STATE, 'pr-%d-rest-reviews-next.json' % n)
            if os.path.exists(later):
                os.replace(later, exact)
            return 0
        doc = view_doc(match[0], consume=False)
        kind = lambda login: 'Bot' if login and (login.endswith('[bot]') or login in ('github-actions', 'copilot-pull-request-reviewer')) else 'User'
        print(json.dumps([{'user': {'login': (rv.get('author') or {}).get('login'), 'type': kind((rv.get('author') or {}).get('login'))},
                           'state': rv.get('state'), 'commit_id': doc['headRefOid']} for rv in doc.get('reviews') or []]))
        return 0
    if api and re.fullmatch(r'repos/.+/activity', path) and method == 'GET':
        # the repository activity of a branch: activity.json as it is; unreadable without it
        path = os.path.join(STATE, 'activity.json')
        if not os.path.exists(path):
            print('HTTP 403: Resource not accessible by integration', file=sys.stderr)
            return 1
        print(open(path).read())
        return 0
    m = re.fullmatch(r'repos/(.+)/commits/([0-9a-f]{40})', path) if api else None
    if m and method == 'GET':
        # one commit; logins.json maps an email to [login, type], an email not in it has no account
        if os.path.exists(os.path.join(STATE, 'fail-commits')):
            print('HTTP 502: Bad Gateway', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'logins.json')
        logins = json.load(open(path)) if os.path.exists(path) else {}
        an, ae, cn, ce = subprocess.check_output(['git', '-C', REPOS[m.group(1)], 'show', '-s', '--format=%an%n%ae%n%cn%n%ce', m.group(2)], text=True).splitlines()[:4]
        user = lambda email: {'login': logins[email][0], 'type': logins[email][1]} if email in logins else None
        print(json.dumps({'sha': m.group(2), 'author': user(ae), 'committer': user(ce),
                          'commit': {'author': {'name': an, 'email': ae}, 'committer': {'name': cn, 'email': ce}}}))
        return 0
    m = re.fullmatch(r'repos/(.+)/collaborators/([^/]+)/permission', path) if api else None
    if m and method == 'GET':
        # a person's permission on a repo: permissions.json maps a login to it; a login it does not name is a 404
        path = os.path.join(STATE, 'permissions.json')
        perms = json.load(open(path)) if os.path.exists(path) else {}
        if m.group(2) not in perms:
            print('gh: Not Found (HTTP 404)', file=sys.stderr)
            return 1
        doc = {'permission': perms[m.group(2)], 'role_name': perms[m.group(2)], 'user': {'login': m.group(2)}}
        print(doc['permission'] if opts.get('--jq') == '.permission' else json.dumps(doc))
        return 0
    m = re.fullmatch(r'repos/(.+)/actions/workflows/([^/]+)/runs', path) if api else None
    if m and method == 'GET':
        # one workflow's runs, newest first: finish-runs.json, a list of REST run objects
        if os.path.exists(os.path.join(STATE, 'fail-finish-runs')):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'finish-runs.json')
        runs = json.load(open(path)) if os.path.exists(path) else []
        print(json.dumps({'total_count': len(runs), 'workflow_runs': runs}))
        return 0
    m = re.fullmatch(r'repos/(.+)/actions/runs', path) and re.fullmatch(r'status=([a-z_]+)(&.*)?', query) if api else None
    if m and method == 'GET':
        # the runs of the repo in one status: active-runs.json, a list of REST run objects
        if os.path.exists(os.path.join(STATE, 'fail-active-runs')):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'active-runs.json')
        runs = [r for r in (json.load(open(path)) if os.path.exists(path) else []) if r.get('status') == m.group(1)]
        print(json.dumps({'total_count': len(runs), 'workflow_runs': runs}))
        return 0
    m = re.fullmatch(r'repos/(.+)/commits/([0-9a-f]{40})/check-runs', path) if api else None
    if m and method == 'GET':
        # the check runs of a head (ENG-2235): check-runs.json, a list of REST check runs, those of that
        # sha; without it, default_check_runs. fail-check-runs makes it fail.
        if os.path.exists(os.path.join(STATE, 'fail-check-runs')):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'check-runs.json')
        runs = json.load(open(path)) if os.path.exists(path) else default_check_runs(m.group(1), m.group(2))[0]
        runs = [r for r in runs if r.get('head_sha') in (None, m.group(2))]
        print(json.dumps({'total_count': len(runs), 'check_runs': runs}))
        return 0
    m = re.fullmatch(r'repos/(.+)/actions/runs\?head_sha=([0-9a-f]{40})(&.*)?', path + '?' + query) if api else None
    if m and method == 'GET':
        # the Actions runs of a head (ENG-2235): head-runs.json, a list of REST workflow runs, those of
        # that sha; without it, default_check_runs. fail-head-runs makes it fail.
        if os.path.exists(os.path.join(STATE, 'fail-head-runs')):
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'head-runs.json')
        runs = json.load(open(path)) if os.path.exists(path) else default_check_runs(m.group(1), m.group(2))[1]
        runs = [r for r in runs if r.get('head_sha') in (None, m.group(2))]
        print(json.dumps({'total_count': len(runs), 'workflow_runs': runs}))
        return 0
    m = re.fullmatch(r'repos/(.+)/actions/runs/(\d+)/jobs', path) if api else None
    if m and method == 'GET':
        # the jobs of one run (ENG-2235): jobs-<id>.json, a list of REST job objects; unreadable without
        # it, but for a run of default_check_runs (2000-2099), whose job is check run 3000+i
        path = os.path.join(STATE, 'jobs-%s.json' % m.group(2))
        rid = int(m.group(2))
        if not os.path.exists(path) and 2000 <= rid < 2100 and not os.path.exists(os.path.join(STATE, 'head-runs.json')):
            print(json.dumps({'total_count': 1, 'jobs': [{'id': rid + 1000, 'run_id': rid, 'name': SOURCES[rid - 2000][0]}]}))
            return 0
        if not os.path.exists(path):
            print('gh: Not Found (HTTP 404)', file=sys.stderr)
            return 1
        jobs = json.load(open(path))
        print(json.dumps({'total_count': len(jobs), 'jobs': jobs}))
        return 0
    if pos[:2] == ['workflow', 'run']:
        # a dispatch: recorded in dispatches.json (the log has the whole command); fail-workflow-run refuses it
        if os.path.exists(os.path.join(STATE, 'fail-workflow-run')):
            print('could not create workflow dispatch event: HTTP 403: Resource not accessible by integration', file=sys.stderr)
            return 1
        path = os.path.join(STATE, 'dispatches.json')
        done = json.load(open(path)) if os.path.exists(path) else []
        fields = dict(argv[i + 1].split('=', 1) for i, a in enumerate(argv) if a == '-f')
        done.append({'workflow': pos[2], 'ref': opts.get('--ref'), 'fields': fields, 'repo': os.environ.get('GH_REPO', '')})
        json.dump(done, open(path, 'w'))
        print('https://github.com/%s/actions/runs/%d' % (os.environ.get('GH_REPO') or repo, 9000 + len(done)))
        return 0
    print('stub gh: unsupported: ' + ' '.join(argv), file=sys.stderr)
    return 1

sys.exit(main(sys.argv[1:]))
'''.replace('#API_PATH#', API_PATH_SRC)

REST_MERGE = '/merge -f merge_method='   # how the stub logs `gh api -X PUT repos/R/pulls/N/merge -f merge_method=...`
# A PR's merge endpoint, as api_parts reads it (2c re-review, should-fix 2; round 4 review, finding 2: GitHub
# answers every spelling api_parts takes apart the same, read-only probes of pulls/101 and pulls/200).
REST_MERGE_PATH = re.compile(r'repos/[^/]+/[^/]+/pulls/[^/]+/merge')
# The REST endpoint that merges a head into a base without a PR.
REST_MERGES_PATH = re.compile(r'repos/[^/]+/[^/]+/merges')
# The GraphQL mutations that merge a PR or a branch, or have GitHub merge a PR once it can.
GRAPHQL_MERGE = re.compile(r'\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|enqueuePullRequest)\b')

# The gh calls merge-up.sh makes that only read: every gh api GET (graphql without a mutation included), and these.
GH_READS = ('pr list', 'pr view', 'run list')
# An allowed write by a gh subcommand (round 5 review, finding 4): the command, the exact set of flags it is called
# with (each takes a value) and how many positional arguments it takes. A flag outside the set (--delete-branch on a
# close, --base on an edit) or a set flag left out is not that write.
GhWrite = collections.namedtuple('GhWrite', 'command flags args')
# An allowed gh api write: its method, the pattern of its api_parts path, the exact set of flags (as spelled) and the
# exact set of field names it sends.
ApiWrite = collections.namedtuple('ApiWrite', 'method path flags fields')
# Every write merge-up.sh makes that merges nothing (round 4 review, finding 2), derived from its gh calls and pinned
# to their flags (round 5 review, finding 4): the PRs it opens, retitles, closes and comments on. The only other writes
# it makes are its merges, so a path that must merge nothing makes these writes alone. A gh api write it may make goes
# in as an ApiWrite. unallowed_writes holds a log to this list;
# test_the_allowlist_is_every_write_merge_up_sh_makes_but_the_merges holds it to merge-up.sh.
ALLOWED_WRITES = (GhWrite('pr create', frozenset({'-R', '--base', '--head', '--title', '--body-file'}), 0),
                  GhWrite('pr edit', frozenset({'-R', '--title'}), 1),
                  GhWrite('pr close', frozenset({'-R', '--comment'}), 1),
                  GhWrite('pr comment', frozenset({'-R', '--body'}), 1))

# gh api's flags (gh api --help), by pflag's grammar: the short spellings, and the long flags that take no value
API_SHORT = {'X': '--method', 'H': '--header', 'f': '--raw-field', 'F': '--field', 'q': '--jq', 't': '--template',
             'p': '--preview', 'i': '--include', 'h': '--help'}
API_BOOL = {'--include', '--paginate', '--silent', '--slurp', '--verbose', '--help'}
API_VALUE = {'--method', '--header', '--raw-field', '--field', '--jq', '--template', '--preview', '--cache', '--hostname',
             '--input'}
# the flags that send a body, and so make gh api a POST when no method is named
API_BODY = {'--raw-field', '--field', '--input'}

ApiCall = collections.namedtuple('ApiCall', 'method path flags args')


def gh_calls(log):
    """The calls of a stub gh log (Fixture.gh_argv), each as its argv: one JSON array of strings per line (round 5
    review, finding 1). A line that is not one is an error: the log as people read it (Fixture.gh_log) joins the
    arguments by spaces and loses where one ends, so a check must never read it."""
    calls = []
    for line in log.split('\n'):
        if not line:
            continue
        argv = json.loads(line)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError('not a gh argv: %r' % line)
        calls.append(argv)
    return calls


def argv_flags(argv, short, value, boolean=frozenset()):
    """(flags, positional arguments) of argv by pflag's grammar, which gh uses: flags is [(spelling, long name, value
    or None)]. short maps a short letter to its long name; value and boolean are the long names that take a value and
    that take none. --name=v, --name v, -xv, -x=v, -x v and clusters such as -iXPUT; -- ends the flags; - alone is an
    argument. None when a flag is not known or its value is missing: gh would refuse the call, and a check cannot say
    what it does."""
    flags, args, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a == '--':
            args += argv[i:]
            break
        if a.startswith('--'):
            name, eq, v = a.partition('=')
            if name in boolean:
                flags.append((name, name, v if eq else None))
            elif name in value:
                if not eq:
                    if i >= len(argv):
                        return None
                    v, i = argv[i], i + 1
                flags.append((name, name, v))
            else:
                return None
        elif a.startswith('-') and len(a) > 1:
            s = a[1:]
            while s:
                name = short.get(s[0])
                if name is None:
                    return None
                if name in boolean and not (len(s) > 2 and s[1] == '='):
                    flags.append(('-' + s[0], name, None))
                    s = s[1:]
                    continue
                if len(s) > 2 and s[1] == '=':
                    v = s[2:]
                elif len(s) > 1:
                    v = s[1:]
                elif i < len(argv):
                    v, i = argv[i], i + 1
                else:
                    return None
                flags.append(('-' + s[0], name, v))
                s = ''
        else:
            args.append(a)
    return flags, args


def api_call(argv):
    """The ApiCall of a logged `gh api` call, its argv after `api`, read by gh's flag grammar (argv_flags), or None
    when gh would refuse it. method: the one -X/--method names (any spelling), upper-cased, or None when it names more
    than one; else POST when it sends a body (-f, -F, --raw-field, --field, --input), as gh does, and GET without.
    path: the endpoint, the single positional argument, through api_parts; None when there is not exactly one (round 5
    review, finding 1: a field's value is a value, never the endpoint). flags: [(spelling, long name, value)]."""
    parsed = argv_flags(argv, API_SHORT, API_VALUE, API_BOOL)
    if parsed is None:
        return None
    flags, args = parsed
    methods = {v.upper() for _, name, v in flags if name == '--method'}
    if methods:
        method = methods.pop() if len(methods) == 1 else None
    else:
        method = 'POST' if any(name in API_BODY for _, name, _ in flags) else 'GET'
    return ApiCall(method, api_parts(args[0])[0] if len(args) == 1 else None, flags, args)


def graphql_writes(call, argv):
    """Whether a GraphQL call can write, or its query cannot be read: an argument that holds "mutation" (any case), a
    body from a file or stdin (--input), or a typed field (-F/--field) whose value starts with @, which gh reads from
    that file or from stdin (round 5 review, finding 2)."""
    return (any('mutation' in a.lower() for a in argv) or any(name == '--input' for _, name, _ in call.flags)
            or any(name == '--field' and v.partition('=')[2].startswith('@') for _, name, v in call.flags))


def allowed_write(argv, allowed):
    """Whether a gh call is one of the writes in allowed, its flags and arguments as pinned."""
    if argv[:1] == ['api']:
        call = api_call(argv[1:])
        return bool(call and call.path is not None and any(
            isinstance(a, ApiWrite) and a.method == call.method and a.path.fullmatch(call.path)
            and {s for s, _, _ in call.flags} == a.flags
            and {v.partition('=')[0] for _, name, v in call.flags if name in API_BODY} == a.fields for a in allowed))
    for a in allowed:
        if isinstance(a, GhWrite) and argv[:2] == a.command.split():
            # every pinned flag takes a value; any other flag is not known here, so the call is not this write
            parsed = argv_flags(argv[2:], {f[1]: f for f in a.flags if not f.startswith('--')}, a.flags)
            if parsed and {name for _, name, _ in parsed[0]} == a.flags and len(parsed[1]) == a.args:
                return True
    return False


def unallowed_writes(log, allowed=ALLOWED_WRITES):
    """The calls of a stub gh log (Fixture.gh_argv) that write and are not in allowed (round 4 review, finding 2;
    round 5 review, findings 1, 2 and 4), each as its arguments joined by spaces: a gh subcommand that is neither a
    read (GH_READS) nor a write of allowed with its pinned flags and arguments (gh pr merge is never one); a gh api call
    gh would refuse, or with other than one endpoint, or whose method cannot be read; a GraphQL call (any spelling of
    the endpoint) that graphql_writes; and any other gh api call that is no GET (by its method, any spelling, or the
    POST its fields imply) unless allowed names its method, path, flags and fields. The tests of a path that must merge
    nothing require this to be empty, and the parent where it was: that closes the class, whatever the spelling or
    the endpoint of a merge."""
    out = []
    for argv in gh_calls(log):
        if argv[:1] != ['api']:
            if ' '.join(argv[:2]) not in GH_READS and not allowed_write(argv, allowed):
                out.append(argv)
            continue
        call = api_call(argv[1:])
        if call is None or call.path is None or call.method is None:
            out.append(argv)
        elif call.path.lower() == 'graphql':
            if graphql_writes(call, argv[1:]):
                out.append(argv)
        elif call.method != 'GET' and not allowed_write(argv, allowed):
            out.append(argv)
    return [' '.join(argv) for argv in out]


def merge_calls(log):
    """The merge calls in a stub gh log (Fixture.gh_argv), each as its arguments joined by spaces: `pr merge`
    (finish_pr), every gh api call on a PR's merge endpoint (the REST merge of the bot finish, whatever the method) or
    on repos/R/merges, and every GraphQL call that names a merge mutation; each endpoint as api_parts reads it, the
    normaliser the stub and unallowed_writes use. A gh api call gh would refuse, or with more than one positional
    argument, counts when any of its arguments is such an endpoint. For the tests that count merges; a test of a path
    that must merge nothing uses unallowed_writes."""
    out = []
    for argv in gh_calls(log):
        if argv[:2] == ['pr', 'merge']:
            out.append(argv)
        elif argv[:1] == ['api']:
            call = api_call(argv[1:])
            paths = [api_parts(a)[0] for a in (call.args if call else argv[1:])]
            if any(REST_MERGE_PATH.fullmatch(p) or REST_MERGES_PATH.fullmatch(p) for p in paths) or (
                    any(p.lower() == 'graphql' for p in paths) and any(GRAPHQL_MERGE.search(a) for a in argv)):
                out.append(argv)
    return [' '.join(argv) for argv in out]


TEST_IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'}


def git(cwd, *args, **kw):
    env = dict(os.environ, **TEST_IDENTITY)
    env.update(kw.pop('env', {}))
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True, env=env, **kw).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def dead_error(branch, why, url=None, child='1.x.x', parent='main'):
    """The ::error:: line of a merge-up run that found merge-up/<child>/<sha7> dead (a person closed
    its last PR, <url>; without <url> the PR list could not be read and <why> is the whole finding)."""
    if url:
        finding = f'a person closed {url} without merging ({why}), so {branch} is treated as closed'
        ways = (f'or a person reopens {url} (the branch then counts as open again) and resolves or finishes it as usual, '
                f'with a dispatch of release-merge-up.yml on the default branch with child={child} pr={url.rsplit("/", 1)[1]}')
    else:
        finding = f'{why}, so {branch} is treated as closed'
        ways = (f'or, if a person closed the last PR of {branch}, a person reopens it and resolves or finishes it as usual '
                f'(dispatch release-merge-up.yml on the default branch with child={child} pr=<its number>). '
                'If no person closed one, the next run that can read the pull requests goes on as normal')
    return (f'::error::merge-up: {child} -> {parent}: {finding}. This run opens no pull request on {branch}, pushes nothing to it, '
            f'merges nothing from it and applies no merge-up rule to it. Two ways forward: the next green commit on {child} gets a new '
            f'branch and a new PR by itself; {ways}')


def commit_all(cwd, msg):
    git(cwd, 'add', '-A')
    git(cwd, 'commit', '-q', '-m', msg)
    return git(cwd, 'rev-parse', 'HEAD')


def write_root(d, config=CONFIG, matrix=MATRIX, versions=('ds',)):
    """The release-line files of a repo root: release/, versions/, the bookkeeping file."""
    write(os.path.join(d, 'release/config.json'), json.dumps(config))
    write(os.path.join(d, 'release/matrix.json'), json.dumps(matrix))
    for p in versions:
        write(os.path.join(d, 'versions', p), '0.0.1\n')
    write(os.path.join(d, 'versions/README.md'), '# versions\n')
    write(os.path.join(d, ENV_FILE), 'DS_TEGRA_DISTRO_REF=main\n')


class Fixture:
    """A bare origin, the working clone the scripts run in, the stub gh and the script environment."""

    def __init__(self):
        self.top = tempfile.mkdtemp(prefix='release-git-')
        self.bare = os.path.join(self.top, 'origin.git')
        self.repo = os.path.join(self.top, 'repo')  # the checkout the scripts run in
        self.state = os.path.join(self.top, 'gh-state')
        self.binp = os.path.join(self.top, 'bin')
        os.makedirs(self.state)
        os.makedirs(self.binp)
        write(os.path.join(self.binp, 'gh'), GH_STUB)
        os.chmod(os.path.join(self.binp, 'gh'), 0o755)
        self.green = os.path.join(self.state, 'green')
        write(self.green, '')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', self.bare], check=True)
        seed = os.path.join(self.top, 'seed')
        subprocess.run(['git', 'init', '-q', '-b', 'main', seed], check=True)
        write_root(seed)
        write(os.path.join(seed, 'foo.txt'), 'trunk\n')
        self.seed_sha = commit_all(seed, 'seed')
        git(seed, 'remote', 'add', 'origin', self.bare)
        git(seed, 'push', '-q', 'origin', 'main')
        subprocess.run(['git', 'clone', '-q', self.bare, self.repo], check=True)

    def close(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def env(self, token_kind='app', dry_run='false'):
        # no git identity, and the merge-up switches of the caller's shell or CI job never reach a test
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')
               and k not in ('DS_MERGE_UP_AUTOMATION', 'MERGE_UP_SUMMARY', 'DEFAULT_BRANCH',
                             'DS_MERGE_UP_AUTO_FINISH', 'AUTO_FINISH_SUMMARY', 'AUTO_FINISH_TRIES', 'AUTO_FINISH_RETRY_SECONDS',
                             'HINT_PR', 'HINT_HEAD', 'GH_REPO', 'MERGE_UP_FINISH_WAIT_SECONDS', 'MERGE_UP_FINISH_POLL_SECONDS')}
        env.update(PATH=self.binp + os.pathsep + os.environ['PATH'],
                   TOKEN='stub-token', TOKEN_KIND=token_kind, DRY_RUN=dry_run,
                   GH_STUB_STATE=self.state, GH_STUB_GREEN=self.green, GH_STUB_REPOS=f'{REPO}={self.bare}',
                   GH_TOKEN='stub-token', GITHUB_REPOSITORY=REPO, GITHUB_SERVER_URL='https://github.com',
                   GITHUB_RUN_ID='4242')
        return env

    def run(self, script, *args, token_kind='app', dry_run='false', extra_env=None):
        env = self.env(token_kind, dry_run)
        env.update(extra_env or {})
        return subprocess.run([script, *args], cwd=self.repo, capture_output=True, text=True, env=env)

    def scripts_copy(self, merge_up=None):
        """A copy of scripts-release/ and release/ inside the fixture, for a run under another
        release/config.json: merge_up maps a dotted key of the merge_up block ("finish.strict",
        "never_auto") to a value (the scripts read the config of their own checkout). Returns
        the copy's scripts-release/."""
        root = os.path.join(self.top, f'scripts-{len(os.listdir(self.top))}')
        shutil.copytree(SCRIPTS, os.path.join(root, 'scripts-release'),
                        ignore=shutil.ignore_patterns('tests', '__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(SCRIPTS), 'release'), os.path.join(root, 'release'))
        if merge_up:
            path = os.path.join(root, 'release', 'config.json')
            with open(path) as f:
                doc = json.load(f)
            for key, value in merge_up.items():
                *sections, name = key.split('.')
                obj = doc['merge_up']
                for section in sections:
                    obj = obj[section]
                obj[name] = value
            with open(path, 'w') as f:
                json.dump(doc, f, indent=2)
        return os.path.join(root, 'scripts-release')

    def gh_argv(self, since=''):
        """The stub's log as it is: each call's argv as one JSON array per line, what gh_calls, unallowed_writes and
        merge_calls read (round 5 review, finding 1). since: a gh_log() taken earlier, whose calls are left out."""
        p = os.path.join(self.state, 'log')
        if not os.path.exists(p):
            return ''
        with open(p) as f:
            lines = [l + '\n' for l in f.read().split('\n') if l]
        n, left = 0, len(since)
        while left > 0:
            left -= len(' '.join(json.loads(lines[n])) + '\n')
            n += 1
        assert left == 0, 'since is not a gh_log() of this log'
        return ''.join(lines[n:])

    def gh_log(self):
        """The stub's log as people read it: each call's arguments joined by spaces, one call after another (an
        argument with a newline spans lines). It loses where an argument ends: a check on writes reads gh_argv."""
        return ''.join(' '.join(argv) + '\n' for argv in gh_calls(self.gh_argv()))

    # --- queries against the bare repo ---
    def heads(self):
        out = git(self.bare, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads/')
        return dict(line.split() for line in out.splitlines())

    def show(self, ref, path):
        return git(self.bare, 'show', f'{ref}:{path}')

    def files_in_commit(self, sha):
        return sorted(git(self.bare, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines())

    def subject(self, sha):
        return git(self.bare, 'log', '-1', '--format=%s', sha)

    def author(self, sha):
        return git(self.bare, 'log', '-1', '--format=%an <%ae>', sha)

    def prs(self):
        p = os.path.join(self.state, 'prs.json')
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return json.load(f)

    def close_pr(self, number, by=None, comment=None):
        """PR <number> closed without a merge. by: who GitHub names as the closer ({'login', 'type'});
        None when it names nobody. comment: an issue comment the closer leaves with it."""
        prs = self.prs()
        pr = [p for p in prs if p['number'] == number][0]
        pr.update(state='CLOSED', closed_by=by)
        if comment is not None:
            pr.setdefault('issue_comments', []).append({'user': by, 'body': comment})
        write(os.path.join(self.state, 'prs.json'), json.dumps(prs))

    def supersede_pr(self, number):
        """PR <number> closed the way merge-up.sh's close_superseded closes one: by the release App,
        with its comment. The only close that leaves merge-up/<child>/<sha7> alive."""
        self.close_pr(number, by={'login': 'ds-os-release-bot[bot]', 'type': 'Bot'},
                      comment='Superseded by the clean merge: the child moved on. The branch is kept.')

    def reopen_pr(self, number):
        """A person reopens PR <number>."""
        prs = self.prs()
        [p for p in prs if p['number'] == number][0].update(state='OPEN', closed_by=None)
        write(os.path.join(self.state, 'prs.json'), json.dumps(prs))

    # --- fixture steps ---
    def tag(self, name, ref):
        git(self.repo, 'fetch', '-q', 'origin')
        git(self.repo, 'tag', name, ref)
        git(self.repo, 'push', '-q', 'origin', name)

    def open_line(self, version, *args):
        r = self.run(CREATE, '--version', version, *args)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def commit(self, branch, files, msg, start=None):
        """A commit on a branch (files: path -> content), pushed; returns the sha. start creates the branch."""
        git(self.repo, 'fetch', '-q', 'origin')
        git(self.repo, 'checkout', '-q', '-B', branch, start or f'origin/{branch}')
        for path, content in files.items():
            write(os.path.join(self.repo, path), content)
        sha = commit_all(self.repo, msg)
        git(self.repo, 'push', '-q', 'origin', branch)
        return sha


class UnitTests(unittest.TestCase):
    def test_latest_runs(self):
        # The rollup gh printed for dexterds14/ds-release-scratch#25 on 2026-11-18, after a
        # person reran the cancelled job of the older run (gh run rerun 44421537899 --failed):
        # one entry per check per workflow run, interleaved job by job, and the rerun attempt
        # replaced the cancelled entry (same run, new job).
        def run(name, run_id, job, started, completed, conclusion='SUCCESS'):
            return {'__typename': 'CheckRun', 'name': name, 'workflowName': 'PR checks', 'status': 'COMPLETED',
                    'conclusion': conclusion, 'startedAt': started, 'completedAt': completed,
                    'detailsUrl': f'https://github.com/dexterds14/ds-release-scratch/actions/runs/{run_id}/job/{job}'}
        pr25 = [run('release-tooling', 44421537899, 135217796025, '2026-11-18T23:32:00Z', '2026-11-18T23:32:04Z'),
                run('release-tooling', 44421704642, 135216197819, '2026-11-18T23:33:26Z', '2026-11-18T23:33:36Z'),
                run('pin-propagation-safety', 44421537899, 135217793959, '2026-11-18T23:36:40Z', '2026-11-18T23:39:13Z'),
                run('pin-propagation-safety', 44421704642, 135216197868, '2026-11-18T23:33:26Z', '2026-11-18T23:35:59Z'),
                run('pr-checks', 44421537899, 135217797175, '2026-11-18T23:32:07Z', '2026-11-18T23:32:09Z'),
                run('pr-checks', 44421704642, 135216299209, '2026-11-18T23:33:39Z', '2026-11-18T23:33:43Z')]
        latest = release_git.latest_runs(pr25)
        self.assertEqual([(c['name'], c['detailsUrl'].rsplit('/', 1)[1]) for c in latest],
                         [('release-tooling', '135216197819'), ('pin-propagation-safety', '135217793959'), ('pr-checks', '135216299209')])
        # before that rerun: the cancelled job beside the newer green one
        before = pr25[:2] + [run('pin-propagation-safety', 44421537899, 135215502025, '2026-11-18T23:32:05Z', '2026-11-18T23:33:26Z', 'CANCELLED'),
                             pr25[3]] + pr25[4:]
        self.assertEqual([c['conclusion'] for c in release_git.latest_runs(before)], ['SUCCESS'] * 3)
        self.assertEqual(release_git.latest_runs(list(reversed(before))), list(reversed(release_git.latest_runs(before))))
        # a run not completed yet (gh prints the zero time) is newer than every completed one
        going = dict(pr25[3], status='IN_PROGRESS', conclusion='', completedAt='0001-01-01T00:00:00Z', startedAt='2026-11-18T23:30:00Z')
        self.assertIs(release_git.latest_runs([pr25[3], going])[0], going)
        self.assertIs(release_git.latest_runs([going, pr25[3]])[0], going)
        # two workflows with a job of the same name are two checks; a status is its own check
        other = dict(pr25[5], workflowName='Other', conclusion='FAILURE')
        status = {'__typename': 'StatusContext', 'context': 'pr-checks', 'state': 'SUCCESS', 'startedAt': '2026-11-18T23:00:00Z'}
        self.assertEqual(len(release_git.latest_runs([pr25[5], other, status])), 3)
        # without any time (the stub's shape) the later entry wins
        self.assertEqual(release_git.latest_runs([{'name': 'x', 'conclusion': 'SUCCESS'}, {'name': 'x', 'conclusion': 'CANCELLED'}]),
                         [{'name': 'x', 'conclusion': 'CANCELLED'}])
        self.assertEqual(release_git.latest_runs(None), [])

    def test_tag_name(self):
        self.assertEqual(release_git.tag_name('ds', '1.2.3', False), 'v1.2.3')
        self.assertEqual(release_git.tag_name('vertex', '1.2.3', True), 'vertex/v1.2.3')
        self.assertEqual(release_git.tag_name('vertex', '1.2.3'), 'vertex/v1.2.3')

    def test_line_plan_prefixed(self):
        self.assertEqual(release_git.line_plan('vertex', '1.0.0'),
                         {'line': 'vertex/1.x.x', 'kind': 'minor', 'level': 'minor', 'pending': '1.1.0', 'tag': 'vertex/v1.0.0'})
        self.assertEqual(release_git.line_plan('vertex', '1.2.0'),
                         {'line': 'vertex/1.2.x', 'kind': 'patch', 'level': 'patch', 'pending': '1.2.1', 'tag': 'vertex/v1.2.0'})
        self.assertEqual(release_git.line_plan('aggregator', '0.0.0')['line'], 'aggregator/0.x.x')
        # ENG-1934: any N.M.P opens either line; auto is patch for P > 0, --kind forces one
        self.assertEqual(release_git.line_plan('vertex', '1.2.3'),
                         {'line': 'vertex/1.2.x', 'kind': 'patch', 'level': 'patch', 'pending': '1.2.4', 'tag': 'vertex/v1.2.3'})
        self.assertEqual(release_git.line_plan('vertex', '0.0.1', 'minor'),
                         {'line': 'vertex/0.x.x', 'kind': 'minor', 'level': 'minor', 'pending': '0.1.0', 'tag': 'vertex/v0.0.1'})
        self.assertEqual(release_git.line_plan('vertex', '0.0.1', 'patch'),
                         {'line': 'vertex/0.0.x', 'kind': 'patch', 'level': 'patch', 'pending': '0.0.2', 'tag': 'vertex/v0.0.1'})
        self.assertEqual(release_git.line_plan('vertex', '1.2.0', 'minor')['pending'], '1.3.0')
        self.assertEqual(release_git.line_plan('vertex', '1.0.0', 'patch'),
                         {'line': 'vertex/1.0.x', 'kind': 'patch', 'level': 'patch', 'pending': '1.0.1', 'tag': 'vertex/v1.0.0'})
        for bad in ('1.0', 'v1.0.0', '01.0.0', '1.0.0-rc.1', '1.2.x'):
            with self.subTest(bad=bad), self.assertRaises(release_git.Error):
                release_git.line_plan('vertex', bad)
        with self.assertRaises(release_git.Error):
            release_git.line_plan('vertex', '1.0.0', 'major')

    def test_line_plan_bare(self):
        self.assertEqual(release_git.line_plan('ds', '1.0.0', prefix=False),
                         {'line': '1.x.x', 'kind': 'minor', 'level': 'minor', 'pending': '1.1.0', 'tag': 'v1.0.0'})
        self.assertEqual(release_git.line_plan('ds', '1.2.0', prefix=False),
                         {'line': '1.2.x', 'kind': 'patch', 'level': 'patch', 'pending': '1.2.1', 'tag': 'v1.2.0'})
        self.assertEqual(release_git.line_plan('ds', '0.0.1', 'minor', prefix=False),
                         {'line': '0.x.x', 'kind': 'minor', 'level': 'minor', 'pending': '0.1.0', 'tag': 'v0.0.1'})
        self.assertEqual(release_git.line_plan('ds', '0.0.1', prefix=False)['line'], '0.0.x')
        with self.assertRaises(release_git.Error) as cm:
            release_git.line_plan('ds', '1.0', prefix=False)
        self.assertIn('vN.M.P', str(cm.exception))

    def test_promote_plan_prefixed(self):
        plan = release_git.promote_plan
        self.assertEqual(plan('vertex/main', '0.0.1', PREFIXED),
                         {'product': 'vertex', 'line': 'vertex/main', 'kind': 'main', 'version': '0.0.1',
                          'tag': 'vertex/v0.0.1', 'level': 'major', 'next_version': '1.0.0'})
        self.assertEqual(plan('refs/heads/vertex/main', '0.0.1', PREFIXED, 'patch')['next_version'], '0.0.2')
        self.assertEqual(plan('vertex/main', '1.2.3', PREFIXED, 'minor')['next_version'], '1.3.0')
        self.assertEqual(plan('vertex/1.x.x', '1.2.0', PREFIXED)['level'], 'minor')
        self.assertEqual(plan('vertex/1.x.x', '1.2.0', PREFIXED)['next_version'], '1.3.0')
        self.assertEqual(plan('vertex/1.2.x', '1.2.3', PREFIXED), {'product': 'vertex', 'line': 'vertex/1.2.x', 'kind': 'patch',
                         'version': '1.2.3', 'tag': 'vertex/v1.2.3', 'level': 'patch', 'next_version': '1.2.4'})
        self.assertEqual(plan('aggregator/0.0.x', '0.0.2', PREFIXED)['next_version'], '0.0.3')
        bad = [('vertex/1.x.x', '1.2.3', 'auto'),   # a minor line promotes 1.M.0
               ('vertex/1.x.x', '2.0.0', 'auto'),   # wrong major
               ('vertex/1.2.x', '1.3.0', 'auto'),   # wrong minor
               ('vertex/1.x.x', '1.2.0', 'patch'),  # leaves 1.2.1
               ('vertex/1.x.x', '1.2.0', 'major'),  # leaves 2.0.0
               ('vertex/1.2.x', '1.2.3', 'minor'),  # leaves 1.3.0
               ('vertex/main', 'v1.0.0', 'auto'), ('vertex/main', '1.0.0', 'release'),
               ('dev/main', '1.0.0', 'auto'), ('vertex/feature', '1.0.0', 'auto'), ('nope/main', '1.0.0', 'auto')]
        for line, pending, bump in bad:
            with self.subTest(line=line, pending=pending, bump=bump), self.assertRaises(release_git.Error):
                plan(line, pending, PREFIXED, bump)

    def test_promote_plan_bare(self):
        plan = release_git.promote_plan
        self.assertEqual(plan('main', '0.0.1', ['ds'], prefix=False),
                         {'product': 'ds', 'line': 'main', 'kind': 'main', 'version': '0.0.1',
                          'tag': 'v0.0.1', 'level': 'major', 'next_version': '1.0.0'})
        self.assertEqual(plan('refs/heads/main', '0.0.1', ['ds'], 'patch', prefix=False)['next_version'], '0.0.2')
        self.assertEqual(plan('1.x.x', '1.2.0', ['ds'], prefix=False)['tag'], 'v1.2.0')
        self.assertEqual(plan('1.2.x', '1.2.3', ['ds'], prefix=False)['next_version'], '1.2.4')
        for line, pending, bump in (('1.x.x', '1.2.3', 'auto'), ('1.x.x', '1.2.0', 'patch'), ('dev/main', '1.0.0', 'auto'),
                                    ('ds/main', '1.0.0', 'auto'), ('1foo.x.x', '1.0.0', 'auto')):
            with self.subTest(line=line, pending=pending, bump=bump), self.assertRaises(release_git.Error):
                plan(line, pending, ['ds'], bump, prefix=False)

    def test_pr_check(self):
        ok = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN',
              'statusCheckRollup': [{'name': 'lint', 'conclusion': 'SUCCESS'}, {'context': 'ci', 'state': 'SUCCESS'}],
              'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
              'commits': [{'authors': [{'login': 'bob'}]}]}
        reasons, pusher, approvers = release_git.pr_check(ok)
        self.assertEqual((reasons, pusher, approvers), ([], 'bob', ['alice']))
        self_approved = dict(ok, reviews=[{'author': {'login': 'bob'}, 'state': 'APPROVED'}])
        self.assertTrue(any('other than the last pusher' in r for r in release_git.pr_check(self_approved)[0]))
        superseded = dict(ok, reviews=[{'author': {'login': 'alice'}, 'state': 'APPROVED'},
                                       {'author': {'login': 'alice'}, 'state': 'CHANGES_REQUESTED'}])
        self.assertTrue(release_git.pr_check(superseded)[0])
        failing = dict(ok, statusCheckRollup=[{'name': 'build', 'conclusion': 'FAILURE'}])
        self.assertTrue(any('check build: FAILURE' in r for r in release_git.pr_check(failing)[0]))
        dirty = dict(ok, mergeStateStatus='DIRTY')
        self.assertTrue(any('not CLEAN' in r for r in release_git.pr_check(dirty)[0]))
        self.assertTrue(release_git.pr_check(dict(ok, reviewDecision='REVIEW_REQUIRED'))[0])
        # the review workflow's own approval is not a person's
        for bot in ('github-actions', 'github-actions[bot]', 'copilot-pull-request-reviewer[bot]'):
            bot_only = dict(ok, reviews=[{'author': {'login': bot}, 'state': 'APPROVED'}])
            self.assertTrue(any('other than the last pusher' in r for r in release_git.pr_check(bot_only)[0]), bot)
        bot_and_alice = dict(ok, reviews=[{'author': {'login': 'github-actions'}, 'state': 'APPROVED'},
                                          {'author': {'login': 'alice'}, 'state': 'APPROVED'}])
        self.assertEqual(release_git.pr_check(bot_and_alice)[2], ['alice'])

    def test_latest_pr(self):
        self.assertIsNone(release_git.latest_pr([]))
        prs = [{'number': 3, 'state': 'CLOSED', 'url': 'u3'}, {'number': 12, 'state': 'OPEN', 'url': 'u12'}, {'number': 7, 'state': 'MERGED', 'url': 'u7'}]
        self.assertEqual(release_git.latest_pr(prs), (12, 'OPEN', 'u12'))
        # an open PR speaks for the head whatever its number: PR 3 reopened after PR 12 was closed
        prs = [{'number': 3, 'state': 'OPEN', 'url': 'u3'}, {'number': 12, 'state': 'CLOSED', 'url': 'u12'}, {'number': 7, 'state': 'MERGED', 'url': 'u7'}]
        self.assertEqual(release_git.latest_pr(prs), (3, 'OPEN', 'u3'))
        # none open: the most recent one
        prs[0]['state'] = 'CLOSED'
        self.assertEqual(release_git.latest_pr(prs), (12, 'CLOSED', 'u12'))

    def test_person_no(self):
        bot = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}

        def no(*args, **kw):
            return release_git.person_no(*args, release_app=bot['login'], **kw)
        alice = {'login': 'alice', 'type': 'User'}
        superseded = [{'user': bot, 'body': 'Superseded by https://x/pull/9: the child moved on'}]
        self.assertIsNone(no('CLOSED', {'closed_by': bot}, superseded))
        # 2c review, trust nit 4: only the release App's own close is its supersession. Another bot (a branch
        # workflow's github-actions[bot]) that closes and leaves the same comment is a no, and so is any
        # close when the caller names no release App
        actions = {'login': 'github-actions[bot]', 'type': 'Bot'}
        self.assertEqual(no('CLOSED', {'closed_by': actions}, [{'user': actions, 'body': 'Superseded by https://x/pull/9'}]),
                         'github-actions[bot] closed it without merging')
        self.assertEqual(release_git.person_no('CLOSED', {'closed_by': bot}, superseded),
                         'ds-os-release-bot[bot] closed it without merging')
        self.assertEqual(release_git.person_no('CLOSED', {'closed_by': bot}, superseded, release_app='other-app[bot]'),
                         'ds-os-release-bot[bot] closed it without merging')
        self.assertEqual(no('CLOSED', {'closed_by': alice}, superseded), 'alice closed it without merging')
        self.assertEqual(no('CLOSED', {'closed_by': bot}, []), 'ds-os-release-bot[bot] closed it without merging')
        other_bot = {'login': 'github-actions[bot]', 'type': 'Bot'}
        self.assertEqual(no('CLOSED', {'closed_by': other_bot}, superseded), 'github-actions[bot] closed it without merging')
        self.assertEqual(no('CLOSED', {'closed_by': None}, superseded), 'someone GitHub does not name closed it without merging')
        self.assertEqual(no('CLOSED'), 'someone GitHub does not name closed it without merging')
        self.assertIsNone(no('MERGED', {'closed_by': alice}, []))
        self.assertIsNone(no('OPEN', {'closed_by': None}, []))

    def test_person_no_reads_the_reviews_of_an_open_pr(self):
        # ENG-2235 (2c): the latest review of each person; a later approval or a dismissal takes a
        # change request back, a comment does not, and a bot's review never counts
        no = release_git.person_no
        alice, bob = {'login': 'alice', 'type': 'User'}, {'login': 'bob', 'type': 'User'}
        rv = lambda user, state: {'user': user, 'state': state}
        self.assertIsNone(no('OPEN', reviews=[]))
        self.assertEqual(no('OPEN', reviews=[rv(alice, 'APPROVED'), rv(alice, 'CHANGES_REQUESTED')]), 'alice requested changes')
        self.assertIsNone(no('OPEN', reviews=[rv(alice, 'CHANGES_REQUESTED'), rv(alice, 'APPROVED')]))
        self.assertIsNone(no('OPEN', reviews=[rv(alice, 'CHANGES_REQUESTED'), rv(alice, 'DISMISSED')]))
        self.assertEqual(no('OPEN', reviews=[rv(alice, 'CHANGES_REQUESTED'), rv(alice, 'COMMENTED')]), 'alice requested changes')
        self.assertIsNone(no('OPEN', reviews=[rv({'login': 'copilot', 'type': 'Bot'}, 'CHANGES_REQUESTED')]))
        self.assertEqual(no('OPEN', reviews=[rv(bob, 'CHANGES_REQUESTED'), rv(alice, 'CHANGES_REQUESTED')]), 'alice, bob requested changes')
        # reviews say nothing about a closed or a merged PR
        self.assertIsNone(no('MERGED', reviews=[rv(alice, 'CHANGES_REQUESTED')]))
        # the CLI: --reviews, paginated as gh prints it
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'reviews.json')
            write(path, json.dumps([rv(alice, 'APPROVED')]) + json.dumps([rv(bob, 'CHANGES_REQUESTED')]))
            cli = [sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'person-no', '--state', 'OPEN', '--reviews', path]
            r = subprocess.run(cli, capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, 'bob requested changes\n'))

    def test_checks_state(self):
        # ENG-2235 (2c): the wait of a bot finish
        head = 'a' * 40
        need = ['pr-checks', 'build']

        def state(rollup, **kw):
            doc = {'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': rollup}
            doc.update(kw)
            return release_git.checks_state(doc, head, need)

        ok = {'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
        ok2 = {'context': 'build', 'state': 'SUCCESS'}
        self.assertEqual(state([ok, ok2]), ('green', ['pr-checks: SUCCESS', 'build: SUCCESS']))
        self.assertEqual(state([])[0], 'pending')
        self.assertEqual(state([ok]), ('pending', ['build: not reported yet']))
        self.assertEqual(state([ok, {'name': 'build', 'status': 'IN_PROGRESS', 'conclusion': None}])[0], 'pending')
        self.assertEqual(state([ok, {'name': 'build', 'status': 'COMPLETED', 'conclusion': 'SKIPPED'}])[0], 'pending')
        self.assertEqual(state([ok, {'name': 'build', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}]), ('red', ['build: FAILURE']))
        self.assertEqual(state([{'context': 'pr-checks', 'state': 'ERROR'}])[0], 'red')
        # a rerun that is still red beside an older green one
        self.assertEqual(state([ok, ok2, {'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'CANCELLED'}])[0], 'red')
        # a check nobody requires does not count either way
        self.assertEqual(state([ok, ok2, {'name': 'lint', 'conclusion': 'FAILURE'}])[0], 'green')
        self.assertEqual(state([{'name': 'lint', 'conclusion': 'SUCCESS'}])[0], 'pending')
        # the head moved, or the PR is not open: never green, whatever the checks say
        self.assertEqual(state([ok, ok2], headRefOid='b' * 40)[0], 'moved')
        self.assertEqual(state([ok, ok2], state='MERGED'), ('closed', ['the PR is MERGED']))
        self.assertEqual(state([ok, ok2], state='CLOSED')[0], 'closed')
        # no required check configured: nothing vouches for the commit
        self.assertEqual(release_git.checks_state({'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': [ok]}, head, [])[0], 'red')

    def test_checks_state_reads_the_newest_run_only(self):
        head = 'a' * 40
        old = {'name': 'build', 'status': 'COMPLETED', 'conclusion': 'CANCELLED',
               'startedAt': '2026-11-18T23:32:05Z', 'completedAt': '2026-11-18T23:33:26Z'}
        new = dict(old, conclusion='SUCCESS', startedAt='2026-11-18T23:33:26Z', completedAt='2026-11-18T23:35:59Z')
        ok = {'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
        doc = lambda rollup: {'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': rollup}
        need = ['pr-checks', 'build']
        self.assertEqual(release_git.checks_state(doc([ok, old, new]), head, need)[0], 'green')
        self.assertEqual(release_git.checks_state(doc([ok, new, old]), head, need)[0], 'green')
        self.assertEqual(release_git.checks_state(doc([ok, dict(new, completedAt='2026-11-18T23:33:00Z'), old]), head, need),
                         ('red', ['build: CANCELLED']))
        running = dict(new, status='IN_PROGRESS', conclusion='', completedAt='0001-01-01T00:00:00Z')
        self.assertEqual(release_git.checks_state(doc([ok, old, running]), head, need)[0], 'pending')

    def test_checks_state_cli(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
            json.dump({'state': 'OPEN', 'headRefOid': 'a' * 40,
                       'statusCheckRollup': [{'name': 'pr-checks', 'conclusion': 'SUCCESS'}]}, f)
            f.flush()
            cli = [sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'checks-state', f.name, '--head', 'a' * 40, '--required']
            self.assertEqual(subprocess.run(cli + ['pr-checks'], capture_output=True, text=True).stdout.splitlines(),
                             ['green', 'pr-checks: SUCCESS'])
            self.assertEqual(subprocess.run(cli + ['pr-checks,build'], capture_output=True, text=True).stdout.splitlines(),
                             ['pending', 'build: not reported yet'])

    def test_merge_answer(self):
        # ENG-2235 (2c): what the REST merge of a bot finish answered
        body405 = ('{"message":"Repository rule violations found\\n\\nAt least 1 approving review is required by reviewers with write access.\\n\\n",'
                   '"documentation_url":"https://docs.github.com/rest/pulls/pulls#merge-a-pull-request","status":"405"}')
        err405 = 'gh: Repository rule violations found\n\nAt least 1 approving review is required by reviewers with write access.\n\n (HTTP 405)\n'
        self.assertEqual(release_git.merge_answer(body405, err405, 1),
                         (None, 'GitHub refused the merge (HTTP 405: Repository rule violations found; '
                                'At least 1 approving review is required by reviewers with write access.)'))
        self.assertEqual(release_git.merge_answer('{"message":"Head branch was modified. Review and try the merge again.","status":"409"}', '', 1),
                         (None, 'GitHub refused the merge (HTTP 409: Head branch was modified. Review and try the merge again.)'))
        self.assertEqual(release_git.merge_answer('', 'gh: Validation Failed (HTTP 422)\n', 1),
                         (None, 'GitHub refused the merge (HTTP 422: Validation Failed)'))
        self.assertEqual(release_git.merge_answer('', 'error connecting to api.github.com\n', 1),
                         (None, 'the merge call failed (error connecting to api.github.com)'))
        self.assertEqual(release_git.merge_answer('<html>', 'gh: Bad Gateway (HTTP 502)\n', 1),
                         (None, 'the merge call failed (HTTP 502: Bad Gateway)'))
        self.assertEqual(release_git.merge_answer('{"sha":"abc","merged":true,"message":"Pull Request successfully merged"}', '', 0), ('abc', ''))
        self.assertEqual(release_git.merge_answer('{"merged":false,"message":"no"}', '', 0), (None, 'GitHub answered without merging it (no)'))
        # the CLI: one line, exit 1 on a refusal; "merged <sha>", exit 0, on a merge
        with tempfile.TemporaryDirectory() as tmp:
            body, err = os.path.join(tmp, 'b'), os.path.join(tmp, 'e')
            write(body, body405)
            write(err, err405)
            cli = [sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'merge-answer', body, '--stderr', err, '--exit']
            r = subprocess.run(cli + ['1'], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.count('\n')), (1, 1))
            self.assertTrue(r.stdout.startswith('GitHub refused the merge (HTTP 405: '))
            write(body, '{"sha":"abc","merged":true}')
            write(err, '')
            r = subprocess.run(cli + ['0'], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (0, 'merged abc\n'))

    def test_first_run(self):
        self.assertEqual(release_git.first_run([{'databaseId': 7, 'number': 3}]), '7 3')
        self.assertIsNone(release_git.first_run([]))

    def test_versions_only_and_bookkeeping(self):
        d = tempfile.mkdtemp()
        try:
            subprocess.run(['git', 'init', '-q', '-b', 'main', d], check=True)
            write(os.path.join(d, 'versions/ds'), '1.0.0\n')
            write(os.path.join(d, ENV_FILE), 'A=1\n')
            write(os.path.join(d, 'a.txt'), 'a\n')
            base = commit_all(d, 'base')
            write(os.path.join(d, 'versions/ds'), '1.1.0\n')
            v1 = commit_all(d, 'bump')
            write(os.path.join(d, 'versions/ds'), '1.2.0\n')
            v2 = commit_all(d, 'bump again')
            write(os.path.join(d, 'a.txt'), 'b\n')
            write(os.path.join(d, 'versions/ds'), '1.3.0\n')
            mixed = commit_all(d, 'mixed')
            self.assertFalse(release_git.is_versions_only(d, base))
            self.assertTrue(release_git.is_versions_only(d, v1))
            self.assertFalse(release_git.is_versions_only(d, mixed))
            self.assertEqual(release_git.walk_versions_only(d, v2), [v2, v1, base])
            self.assertEqual(release_git.walk_versions_only(d, mixed), [mixed])
            # a merge commit is never versions-only
            git(d, 'checkout', '-q', '-b', 'side', base)
            write(os.path.join(d, 'versions/ds'), '9.0.0\n')
            commit_all(d, 'side bump')
            git(d, 'checkout', '-q', 'main')
            git(d, 'merge', '-q', '--no-ff', '-m', 'merge', 'side', '-X', 'ours')
            self.assertFalse(release_git.is_versions_only(d, 'HEAD'))
            merge = git(d, 'rev-parse', 'HEAD')
            # bookkeeping: versions/ and the configured bookkeeping files only (a line's opening
            # commit, a CI env pin); unmerged lists everything else in base..head, oldest first,
            # merges included
            git(d, 'checkout', '-q', '-b', 'line', base)
            write(os.path.join(d, ENV_FILE), 'A=line\n')
            write(os.path.join(d, 'versions/ds'), '1.0.1\n')
            opening = commit_all(d, 'release: open line [skip ci]')
            self.assertTrue(release_git.is_bookkeeping(d, opening, [ENV_FILE]))
            self.assertFalse(release_git.is_bookkeeping(d, opening))  # the env file counts only when configured
            self.assertFalse(release_git.is_versions_only(d, opening))
            self.assertTrue(release_git.is_bookkeeping(d, v1))
            self.assertFalse(release_git.is_bookkeeping(d, mixed, [ENV_FILE]))
            self.assertFalse(release_git.is_bookkeeping(d, merge, [ENV_FILE]))
            self.assertEqual(release_git.unmerged(d, 'main', 'line', [ENV_FILE]), [])
            self.assertEqual(release_git.unmerged(d, 'main', 'line'), [(opening, 'release: open line [skip ci]')])
            write(os.path.join(d, 'a.txt'), 'line fix\n')
            fix = commit_all(d, 'line fix')
            write(os.path.join(d, 'versions/ds'), '1.0.2\n')
            commit_all(d, 'release: pending 1.0.2 [skip ci]')
            self.assertEqual(release_git.unmerged(d, 'main', 'line', [ENV_FILE]), [(fix, 'line fix')])
            self.assertEqual([s for s, _ in release_git.unmerged(d, 'line', 'main', [ENV_FILE])], [mixed, merge])  # v1, v2 are versions-only
            # the CLI reads the bookkeeping files from --root
            root = os.path.join(d, 'root')
            write_root(root)
            r = subprocess.run([sys.executable, release_git.__file__, 'unmerged', 'main', 'line', '--git-dir', d, '--root', root],
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (1, f'{fix} line fix\n'), r.stderr)
            write(os.path.join(root, 'release/config.json'), json.dumps({'products': ['ds'], 'product_prefix': False}))
            r = subprocess.run([sys.executable, release_git.__file__, 'unmerged', 'main', 'line', '--git-dir', d, '--root', root],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertEqual([l.split()[0] for l in r.stdout.splitlines()], [opening, fix])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_merge_keep_target(self):
        """Inside an uncommitted merge the target keeps versions/<product> and every bookkeeping
        file it has, a conflict on either included; the README and everything else merge."""
        d = tempfile.mkdtemp()
        try:
            subprocess.run(['git', 'init', '-q', '-b', 'main', d], check=True)
            write_root(d)
            write(os.path.join(d, 'foo.txt'), 'trunk\n')
            base = commit_all(d, 'base')
            git(d, 'checkout', '-q', '-b', 'child', base)
            write(os.path.join(d, 'versions/ds'), '1.1.9\n')
            write(os.path.join(d, 'versions/README.md'), 'pending versions, fixed\n')
            write(os.path.join(d, ENV_FILE), 'DS_TEGRA_DISTRO_REF=child\n')
            write(os.path.join(d, 'foo.txt'), 'child fix\n')
            write(os.path.join(d, 'versions/portal'), '0.0.1\n')  # exists only on the child side
            commit_all(d, 'child work')
            git(d, 'checkout', '-q', 'main')
            write(os.path.join(d, 'versions/ds'), '3.0.0\n')
            write(os.path.join(d, ENV_FILE), 'DS_TEGRA_DISTRO_REF=parent\n')
            commit_all(d, 'parent work')
            r = subprocess.run(['git', '-C', d, 'merge', '--no-ff', '--no-commit', 'child'], capture_output=True, text=True,
                               env=dict(os.environ, **TEST_IDENTITY))
            self.assertNotEqual(r.returncode, 0)  # versions/ds and the env file conflict
            self.assertEqual(sorted(git(d, 'diff', '--name-only', '--diff-filter=U').split()), [ENV_FILE, 'versions/ds'])
            kept = release_git.merge_keep_target(d, [ENV_FILE, 'missing/file'])
            self.assertEqual(kept, ['versions/ds', ENV_FILE])
            self.assertEqual(git(d, 'diff', '--name-only', '--diff-filter=U'), '')
            self.assertEqual(git(d, 'show', ':versions/ds'), '3.0.0')
            self.assertEqual(git(d, 'show', f':{ENV_FILE}'), 'DS_TEGRA_DISTRO_REF=parent')
            self.assertEqual(git(d, 'show', ':versions/README.md'), 'pending versions, fixed')
            self.assertEqual(git(d, 'show', ':versions/portal'), '0.0.1')
            self.assertEqual(git(d, 'show', ':foo.txt'), 'child fix')
            commit_all(d, 'Merge child into main')
            self.assertEqual(git(d, 'rev-parse', 'HEAD^2'), git(d, 'rev-parse', 'child'))
            # the CLI, with the bookkeeping files from --root; no branch argument
            git(d, 'checkout', '-q', '-b', 'child2', base)
            write(os.path.join(d, ENV_FILE), 'DS_TEGRA_DISTRO_REF=child2\n')
            commit_all(d, 'child2 env')
            git(d, 'checkout', '-q', 'main')
            subprocess.run(['git', '-C', d, 'merge', '--no-ff', '--no-commit', 'child2'], capture_output=True, text=True,
                           env=dict(os.environ, **TEST_IDENTITY))
            root = os.path.join(d, 'cfg')
            write_root(root)
            r = subprocess.run([sys.executable, release_git.__file__, 'merge-keep-target', '--git-dir', d, '--root', root],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, f'kept from the target: versions/ds, versions/portal, {ENV_FILE}\n')
            self.assertEqual(git(d, 'show', f':{ENV_FILE}'), 'DS_TEGRA_DISTRO_REF=parent')
            r = subprocess.run([sys.executable, release_git.__file__, 'merge-keep-target', 'main', '--git-dir', d],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2)  # the branch argument is gone
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_cli_plans_read_the_prefix_from_root(self):
        d = tempfile.mkdtemp()
        try:
            write_root(d)
            r = subprocess.run([sys.executable, release_git.__file__, 'line-plan', 'ds', '1.0.0', '--root', d], capture_output=True, text=True)
            self.assertEqual(json.loads(r.stdout), {'line': '1.x.x', 'kind': 'minor', 'level': 'minor', 'pending': '1.1.0', 'tag': 'v1.0.0'})
            r = subprocess.run([sys.executable, release_git.__file__, 'promote-plan', 'main', '0.0.1', '--bump', 'patch', '--root', d],
                               capture_output=True, text=True)
            self.assertEqual(json.loads(r.stdout)['tag'], 'v0.0.1')
            r = subprocess.run([sys.executable, release_git.__file__, 'promote-plan', 'ds/main', '0.0.1', '--root', d], capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn('not a release line', r.stderr)
            write(os.path.join(d, 'release/config.json'), json.dumps({'products': PREFIXED}))
            r = subprocess.run([sys.executable, release_git.__file__, 'line-plan', 'vertex', '1.0.0', '--root', d], capture_output=True, text=True)
            self.assertEqual(json.loads(r.stdout)['tag'], 'vertex/v1.0.0')
            r = subprocess.run([sys.executable, release_git.__file__, 'promote-plan', 'vertex/main', '0.0.1', '--root', d], capture_output=True, text=True)
            self.assertEqual(json.loads(r.stdout)['tag'], 'vertex/v0.0.1')
        finally:
            shutil.rmtree(d, ignore_errors=True)


class LibShTests(unittest.TestCase):
    """lib.sh reads product_prefix once at source time from the release/config.json next to it
    (version.py's root), so a copy of lib.sh and version.py under a temp root exercises both modes."""

    def source(self, config, body):
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, 'scripts-release'))
            for name in ('lib.sh', 'version.py'):
                shutil.copy(os.path.join(SCRIPTS, name), os.path.join(d, 'scripts-release', name))
            write(os.path.join(d, 'release/config.json'), json.dumps(config))
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_BOT_')}
            r = subprocess.run(['bash', '-c', f'set -euo pipefail; . {d}/scripts-release/lib.sh; {body}'],
                               capture_output=True, text=True, env=env)
            return r.returncode, r.stdout, r.stderr
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_bare_names(self):
        rc, out, err = self.source(CONFIG, 'echo "$PRODUCT_PREFIX"; rc_tag ds 0123456789abcdef; product_tag ds 1.2.3; echo "[$(product_label ds)]"')
        self.assertEqual((rc, out.splitlines()), (0, ['false', 'rc/0123456', 'v1.2.3', '[]']), err)

    def test_prefixed_names(self):
        rc, out, err = self.source({'products': PREFIXED}, 'echo "$PRODUCT_PREFIX"; rc_tag vertex 0123456789abcdef; product_tag vertex 1.2.3; echo "[$(product_label vertex)]"')
        self.assertEqual((rc, out.splitlines()), (0, ['true', 'rc/vertex/0123456', 'vertex/v1.2.3', '[vertex ]']), err)

    def test_bot_identity_and_hint(self):
        rc, out, err = self.source(CONFIG, 'echo "$GIT_BOT_NAME"; echo "$GIT_BOT_EMAIL"; no_app_hint "x failed" "run this"')
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[:2], ['ds-os-release-bot[bot]', '346446406+ds-os-release-bot[bot]@users.noreply.github.com'])
        self.assertTrue(lines[2].startswith('::error::x failed, and this job ran on GITHUB_TOKEN'))
        self.assertIn('ds-os-release-bot App', lines[2])
        self.assertIn('docs/release-admin-setup.md', lines[2])
        self.assertTrue(lines[2].endswith('runs: run this'))
        # the workflow's values win
        r = subprocess.run(['bash', '-c', f'set -euo pipefail; . {SCRIPTS}/lib.sh; echo "$GIT_BOT_NAME"'],
                           capture_output=True, text=True, env=dict(os.environ, GIT_BOT_NAME='other[bot]'))
        self.assertEqual(r.stdout.strip(), 'other[bot]')

    def test_invalid_config_stops_the_script(self):
        rc, out, err = self.source({'products': PREFIXED, 'product_prefix': False}, 'echo reached')
        self.assertEqual(rc, 1)
        self.assertNotIn('reached', out)
        self.assertIn('::error::lib.sh: release/config.json is invalid', out)
        self.assertIn('exactly one product', err)


class CreateBranchTests(unittest.TestCase):
    """create-branch.sh in line mode: tag vN.M.P opens N.x.x or N.M.x with one opening commit."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.main_tip = self.fx.heads()['main']
        self.fx.tag('v1.0.0', self.main_tip)

    def test_dry_run_creates_nothing(self):
        r = self.fx.run(CREATE, '--version', '1.0.0', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing created', r.stdout)
        self.assertIn('+1.1.0', r.stdout)
        self.assertEqual(set(self.fx.heads()), {'main'})

    def test_line_mode(self):
        r = self.fx.open_line('1.0.0')
        self.assertIn('parent line:        main', r.stdout)
        heads = self.fx.heads()
        tip = heads['1.x.x']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^'), self.main_tip)
        self.assertEqual(self.fx.files_in_commit(tip), ['versions/ds'])
        self.assertEqual(self.fx.subject(tip), 'release: open 1.x.x from v1.0.0 [skip ci]')
        self.assertEqual(self.fx.author(tip), 'ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>')
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '1.1.0')
        self.assertEqual(self.fx.show(tip, ENV_FILE), self.fx.show(self.main_tip, ENV_FILE))
        self.assertEqual(heads['main'], self.main_tip)
        # a patch line from a tag on the minor line
        line_tip = self.fx.commit('1.x.x', {'foo.txt': 'line fix\n'}, 'line fix')
        self.fx.tag('v1.1.0', line_tip)
        r = self.fx.open_line('1.1.0')
        self.assertIn('parent line:        1.x.x', r.stdout)
        tip = self.fx.heads()['1.1.x']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^'), line_tip)
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '1.1.1')
        self.assertEqual(self.fx.subject(tip), 'release: open 1.1.x from v1.1.0 [skip ci]')

    def test_line_mode_from_any_tag(self):
        """ENG-1934: any N.M.P tag can open either line, so both lines can be cut from v0.0.1."""
        self.fx.tag('v0.0.1', self.main_tip)
        r = self.fx.run(CREATE, '--version', '0.0.1', '--kind', 'minor')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('kind:               minor line (--kind minor)', r.stdout)
        self.assertEqual(self.fx.show(self.fx.heads()['0.x.x'], 'versions/ds'), '0.1.0')
        r = self.fx.run(CREATE, '--version', '0.0.1')  # auto = patch for P > 0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('kind:               patch line (--kind auto)', r.stdout)
        tip = self.fx.heads()['0.0.x']
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '0.0.2')
        self.assertEqual(self.fx.subject(tip), 'release: open 0.0.x from v0.0.1 [skip ci]')
        # the derived line existing is still refused
        r = self.fx.run(CREATE, '--version', '0.0.1', '--kind', 'patch')
        self.assertEqual(r.returncode, 1)
        self.assertIn('already has branch 0.0.x', r.stdout)
        r = self.fx.run(CREATE, '--version', '0.0.1', '--kind', 'major')
        self.assertEqual(r.returncode, 1)
        self.assertIn('--kind must be auto, minor or patch', r.stdout)

    def test_seed_from(self):
        """--seed-from <ref>: the line opens at that ref instead of at the tag, provided the tag is an ancestor."""
        later = self.fx.commit('main', {'foo.txt': 'after the tag\n'}, 'after the tag')
        seed = self.fx.commit('seed', {'foo.txt': 'seed branch\n'}, 'seed work', start=later)
        r = self.fx.run(CREATE, '--version', '1.0.0', '--seed-from', 'seed', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing created', r.stdout)
        self.assertNotIn('1.x.x', self.fx.heads())
        r = self.fx.run(CREATE, '--version', '1.0.0', '--seed-from', 'seed')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['1.x.x']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^'), seed)
        self.assertEqual(self.fx.files_in_commit(tip), ['versions/ds'])
        subject = self.fx.subject(tip)
        self.assertTrue(subject.startswith('release: open 1.x.x from v1.0.0'), subject)
        self.assertIn('(seeded from seed)', subject)
        self.assertTrue(subject.endswith('[skip ci]'), subject)
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '1.1.0')
        self.assertEqual(self.fx.show(tip, 'foo.txt'), 'seed branch')
        # a sha works as the ref too
        r = self.fx.run(CREATE, '--version', '1.0.0', '--kind', 'patch', '--seed-from', later)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['1.0.x']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^'), later)
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '1.0.1')

    def test_seed_from_refusals(self):
        # v1.0.0 is at main's tip; a branch off the seed commit does not contain it
        first = git(self.fx.bare, 'rev-list', '--max-parents=0', 'main')
        self.fx.commit('main', {'foo.txt': 'tagged\n'}, 'tagged')
        self.fx.tag('v2.0.0', self.fx.heads()['main'])
        other = self.fx.commit('other', {'foo.txt': 'elsewhere\n'}, 'elsewhere', start=first)
        r = self.fx.run(CREATE, '--version', '2.0.0', '--seed-from', 'other')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('not an ancestor', r.stdout)
        self.assertNotIn('2.x.x', self.fx.heads())
        r = self.fx.run(CREATE, '--version', '2.0.0', '--seed-from', 'no-such-ref')
        self.assertEqual(r.returncode, 1)
        self.assertIn('does not resolve', r.stdout)
        self.assertNotIn('2.x.x', self.fx.heads())
        self.assertEqual(self.fx.heads()['other'], other)

    def test_refusals(self):
        r = self.fx.run(CREATE, '--version', '1.2')
        self.assertEqual(r.returncode, 1)
        self.assertIn('must be N.M.P', r.stdout)
        r = self.fx.run(CREATE, '--version', '1.2.3')
        self.assertEqual(r.returncode, 1)
        self.assertIn('tag v1.2.3 does not exist', r.stdout)
        r = self.fx.run(CREATE, '--seed-from', 'main')
        self.assertNotEqual(r.returncode, 0)
        r = self.fx.run(CREATE)
        self.assertNotEqual(r.returncode, 0)
        self.fx.open_line('1.0.0')
        r = self.fx.run(CREATE, '--version', '1.0.0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('already has branch 1.x.x', r.stdout)
        self.assertEqual(set(self.fx.heads()), {'main', '1.x.x'})

    def test_no_app_token(self):
        r = self.fx.run(CREATE, '--version', '1.0.0', token_kind='github', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing created', r.stdout)
        r = self.fx.run(CREATE, '--version', '1.0.0', token_kind='github')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::create-branch: no App token', r.stdout)
        self.assertIn('git checkout -b 1.x.x', r.stdout)
        self.assertEqual(set(self.fx.heads()), {'main'})


def argv_log(*calls):
    """A stub gh log of these calls, as the stub writes it: each a list of arguments, or a string read as a shell
    would (shlex.split)."""
    return ''.join(json.dumps(shlex.split(c) if isinstance(c, str) else c) + '\n' for c in calls)


def joined(call):
    """A call of argv_log as unallowed_writes and merge_calls give it: its arguments joined by spaces."""
    return ' '.join(shlex.split(call) if isinstance(call, str) else call)


def shell_gh_calls(path, command):
    """[argv] of every `gh <command>` merge-up.sh runs, its arguments as a shell would split them, up to the end of the
    command: for the pins of ALLOWED_WRITES. A variable stands for a value, o/r for one that names a repository
    (...REPO, $repo). Comments and echo lines (a command named for a person to run) are not calls."""
    with open(path) as f:
        lines = f.read().replace('\\\n', ' ').splitlines()
    lines = [re.sub(r'\$\{?\w+\}?', 'x', re.sub(r'\$\{?\w*(REPO|repo)\}?', 'o/r', l)) for l in lines]
    text = '\n'.join(l for l in lines if not l.lstrip().startswith('#') and not re.search(r'\becho\b', l))
    out = []
    for m in re.finditer(r'\bgh %s\b' % re.escape(command), text):
        lex = shlex.shlex(text[m.end():text.index('\n', m.end())], posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = []
        for w in lex:
            if w and w[0] in '();<>|&':
                if words and words[-1].isdigit():
                    words.pop()   # the 2 of 2>
                break
            words.append(w)
        out.append(command.split() + words)
    return out


class MergeCallsTests(unittest.TestCase):
    """merge_calls counts every spelling of a merge (2c review, SP/DS should-fix 1), and unallowed_writes finds every
    write outside ALLOWED_WRITES (round 4 review, finding 2), both on the stub's normaliser api_parts and on each
    call's argv as the stub logs it (round 5 review, finding 1)."""

    # Round 4 review, finding 2: the spellings GitHub answers as a merge (read-only probes), and other endpoints that
    # merge a head into a base.
    SPELLINGS = ['api -X PUT repos/o/r/pulls/1/merge# -f merge_method=merge -f sha=abc',
                 'api -X PUT repos/o/r/pulls/1/merge#x -f merge_method=merge',
                 'api -X PUT https://API.GITHUB.COM/repos/o/r/pulls/1/merge -f merge_method=merge',
                 'api -X PUT https://api.github.com:443/repos/o/r/pulls/1/merge -f merge_method=merge',
                 'api -X PUT HTTPS://Api.GitHub.com:443//repos/o/r/pulls/0101/merge?sha=abc#x',
                 """api /graphql -f 'query=mutation{mergePullRequest(input:{pullRequestId:"PR_1"}){clientMutationId}}'""",
                 """api graphql# -f 'query=mutation{enablePullRequestAutoMerge(input:{pullRequestId:"PR_1"}){clientMutationId}}'""",
                 """api https://API.github.com:443/graphql -f 'query=mutation{mergePullRequest(input:{pullRequestId:"PR_1"}){clientMutationId}}'""",
                 """api graphql?x=1 -f 'query=mutation{mergeBranch(input:{repositoryId:"R",base:"main",head:"h"}){clientMutationId}}'""",
                 """api graphql -f 'query=mutation{enqueuePullRequest(input:{pullRequestId:"PR_1"}){clientMutationId}}'"""]
    OTHER_WRITES = ['api repos/o/r/merges -f base=main -f head=merge-up/1.x.x/abc1234',
                    'api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc',
                    'api --method=patch repos/o/r/git/refs/heads/main -F sha=abc',
                    'api -XPOST repos/o/r/git/refs -f ref=refs/heads/x -f sha=abc',
                    'api -iXPUT repos/o/r/pulls/1/merge',
                    'api --input body.json repos/o/r/pulls/1/merge',
                    'api graphql --input query.json',
                    'api -X DELETE repos/o/r/git/refs/heads/merge-up/1.x.x/abc1234',
                    'api -X PUT repos/o/r/pulls/1/update-branch',
                    'api -X POST repos/o/r/pulls/1/reviews -f event=APPROVE',
                    'api -X POST repos/o/r/dispatches -f event_type=x',
                    'api -X POST repos/o/r/issues/1/comments -f body=x',
                    'api -X POST repos/o/r/commits/abc/comments -f body=x',
                    'pr merge 1 -R o/r --merge', 'pr merge 1 -R o/r --auto --merge', 'pr review 1 -R o/r --approve',
                    'pr ready 1 -R o/r', 'pr reopen 1 -R o/r', 'workflow run release-merge-up.yml -f child=x', 'api',
                    'api -X PUT {owner}/{repo}/pulls/1/merge']
    # every write merge-up.sh makes that merges nothing, as it makes them
    ALLOWED = ['pr create -R o/r --base main --head h --title t --body-file b', 'pr edit 1 -R o/r --title t',
               'pr close 1 -R o/r --comment c', "pr comment 1 -R o/r --body 'text with -X PUT repos/o/r/pulls/1/merge'"]

    def test_every_spelling_of_the_rest_merge(self):
        merges = ['pr merge 1 -R o/r --merge --match-head-commit abc',
                  'api -X PUT repos/o/r/pulls/1/merge -f merge_method=merge -f sha=abc',
                  'api --method PUT repos/o/r/pulls/1/merge -f merge_method=merge -f sha=abc',
                  'api --method=PUT repos/o/r/pulls/12/merge -f sha=0000',
                  'api repos/o/r/pulls/1/merge -X PUT -F merge_method=merge',
                  'api repos/o/r/pulls/1/merge',
                  'api -XPUT repos/o/r/pulls/1/merge',
                  # 2c re-review, should-fix 2: a leading /, the absolute URL, a query string, GraphQL
                  'api -X PUT /repos/o/r/pulls/1/merge -f merge_method=merge -f sha=abc',
                  'api -X PUT https://api.github.com/repos/o/r/pulls/1/merge -f merge_method=merge',
                  'api -X PUT repos/o/r/pulls/1/merge?merge_method=merge&sha=abc',
                  'api -X PUT /repos/o/r/pulls/1/merge?merge_method=merge',
                  """api graphql -f 'query=mutation{mergePullRequest(input:{pullRequestId:"PR_1"}){clientMutationId}}'""",
                  """api graphql -f 'query=mutation { enablePullRequestAutoMerge(input: {pullRequestId: "PR_1"}) { clientMutationId } }'""",
                  "api --paginate graphql -F id=PR_1 -f 'query=mutation($id: ID!) { mergePullRequest(input: {pullRequestId: $id}) { clientMutationId } }'",
                  'api repos/o/r/merges -f base=main -f head=h'] + self.SPELLINGS
        others = ['api --paginate repos/o/r/pulls/1/reviews', 'api repos/o/r/pulls/1/merged', 'api repos/o/r/pulls/1/merge_x',
                  'pr view 1 -R o/r --json state', 'api repos/o/r/issues/1/comments -f body=pulls/1/merge',
                  'api /repos/o/r/pulls/1/merged', 'api https://api.github.com/repos/o/r/pulls/1/merges',
                  """api graphql -f 'query={ repository(owner:"o", name:"r") { pullRequest(number: 1) { mergeable } } }'""",
                  'api repos/o/r/issues/1/comments -f body=mergePullRequest', 'api repos/o/r/pulls/1/merge/']
        self.assertEqual(merge_calls(argv_log(*merges, *others)), [joined(c) for c in merges])

    def test_every_write_outside_the_allowlist(self):
        writes = self.SPELLINGS + self.OTHER_WRITES + ['api -X PUT repos/o/r/pulls/1/merge -f merge_method=merge -f sha=abc',
                                                       """api graphql -f 'query=mutation{addComment(input:{subjectId:"PR_1",body:"x"}){clientMutationId}}'""",
                                                       'api graphql -f query=MUTATION{x}']
        # every write merge-up.sh makes that merges nothing, and reads in any spelling
        fine = self.ALLOWED + [
            'pr list -R o/r --state all', 'pr view 1 -R o/r', 'run list -R o/r --workflow w',
            'api --paginate repos/o/r/pulls/1/reviews', 'api --paginate -X GET repos/o/r/activity -f ref=refs/heads/x',
            "api --method GET -H 'Accept: application/vnd.github+json' repos/o/r/compare/a...b", 'api repos/o/r/pulls/1/merge',
            "api graphql -f 'query={ viewer { login } }'", 'api --paginate repos/o/r/pulls?state=all&head=o:x&per_page=100']
        self.assertEqual(unallowed_writes(argv_log(*writes, *fine)), [joined(c) for c in writes])
        # the allowlist is the argument: without it every write counts
        self.assertEqual(unallowed_writes(argv_log(*fine), allowed=()), [joined(c) for c in self.ALLOWED])
        # an argument with newlines stays one argument of its call, whatever its lines look like
        body = ['pr', 'comment', '1', '-R', 'o/r', '--body', 'stopped: <why>\n\nThe reasons:\n- no-approval: x\n'
                'api -X PUT repos/o/r/pulls/1/merge\n<!-- marker -->']
        self.assertEqual(unallowed_writes(argv_log(body)), [])
        spread = ['api', 'graphql', '-f', 'query=\nmutation {\n  mergePullRequest(input: {}) { clientMutationId }\n}']
        self.assertEqual(unallowed_writes(argv_log(body, spread)), [' '.join(spread)])
        self.assertEqual(merge_calls(argv_log(spread, body)), [' '.join(spread)])
        # the log as people read it is not a log the checks take: it loses where an argument ends
        for log in ('pr view 1 -R o/r\n', '["pr", 1]\n', '{"argv": []}\n'):
            with self.subTest(log=log):
                with self.assertRaises(ValueError):
                    unallowed_writes(log)

    def test_a_field_cannot_move_the_endpoint(self):
        # round 5 review, finding 1: a field whose value holds an allowed endpoint, in front of the real one. gh sends
        # the call to its one positional argument; a call with two is refused by gh, and is no allowed write here
        merges = ['api', '-X', 'POST', '-f', 'x=y repos/o/r/commits/abc/comments', '-f', 'base=main', '-f', 'head=h',
                  'repos/o/r/merges']
        approve = ['api', '-X', 'POST', '-f', 'x=y repos/o/r/commits/abc/comments', '-f', 'event=APPROVE',
                   'repos/o/r/pulls/1/reviews']
        two = ['api', '-X', 'POST', 'repos/o/r/commits/abc/comments', '-f', 'body=x', 'repos/o/r/merges']
        for call in (merges, approve, two):
            with self.subTest(call=call):
                self.assertEqual(unallowed_writes(argv_log(call)), [' '.join(call)])
        self.assertEqual(merge_calls(argv_log(merges, approve, two)), [' '.join(merges), ' '.join(two)])
        self.assertEqual(api_call(merges[1:])[:2], ('POST', 'repos/o/r/merges'))
        self.assertEqual(api_call(approve[1:])[:2], ('POST', 'repos/o/r/pulls/1/reviews'))
        self.assertEqual(api_call(two[1:])[:2], ('POST', None))

    def test_gh_api_flags_by_gh_grammar(self):
        # pflag's grammar, as gh reads it: (method, path) of each
        cases = {('-iXPUT', 'p'): ('PUT', 'p'), ('-X=put', 'p'): ('PUT', 'p'), ('-Xput', 'p'): ('PUT', 'p'),
                 ('--method=Put', 'p'): ('PUT', 'p'), ('p', '--method', 'put'): ('PUT', 'p'),
                 ('-fbody=x', 'p'): ('POST', 'p'), ('-F=a=b', 'p'): ('POST', 'p'), ('--raw-field', 'a=b', 'p'): ('POST', 'p'),
                 ('--input=-', 'p'): ('POST', 'p'), ('-H', 'X-A: -X PUT', 'p'): ('GET', 'p'), ('-i=true', 'p'): ('GET', 'p'),
                 ('--paginate=false', '-q', '.x y', 'p'): ('GET', 'p'), ('-X', 'GET', '--', '-p'): ('GET', '-p'),
                 ('-X', '-f', 'p'): ('-F', 'p'), ('-', '-X', 'PUT'): ('PUT', '-'),
                 ('-X', 'PUT', '-X', 'POST', 'p'): (None, 'p'), ('-X', 'PUT', '--method', 'put', 'p'): ('PUT', 'p'),
                 ('p', 'q'): ('GET', None), (): ('GET', None)}
        for argv, want in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(api_call(list(argv))[:2], want)
        # a flag gh does not know, or a flag without its value: gh refuses the call, and it is no allowed write
        for argv in (['--bogus', 'p'], ['-z', 'p'], ['-iz', 'p'], ['p', '-X'], ['p', '--jq']):
            with self.subTest(argv=argv):
                self.assertIsNone(api_call(argv))
                self.assertEqual(unallowed_writes(argv_log(['api'] + argv)), [' '.join(['api'] + argv)])

    def test_a_graphql_query_from_a_file(self):
        # round 5 review, finding 2: gh reads a typed field (-F, --field) whose value starts with @ from that file, or
        # from stdin for @-, and --input sends a whole body from one: the query is not in the log, so the call is no read
        writes = ['api graphql -F query=@/tmp/q.gql', 'api graphql --field query=@/tmp/q.gql', 'api graphql -Fquery=@/tmp/q.gql',
                  'api graphql --field=query=@-', 'api graphql -F=query=@q', 'api graphql -iFquery=@q',
                  'api /graphql -F query=@q', 'api GRAPHQL# -F query=@q', 'api https://API.github.com:443/graphql -F query=@q',
                  "api graphql -F variables=@v.json -f 'query={ viewer { login } }'",
                  'api graphql --input q.json', 'api graphql --input=-', 'api graphql -X GET --input q.json']
        fine = ["api graphql -f 'query=@not-a-file { viewer { login } }'",
                "api graphql -F owner=o -f 'query=query($owner: String!) { repositoryOwner(login: $owner) { login } }'",
                'api -X GET repos/o/r/pulls -F q=@x']
        self.assertEqual(unallowed_writes(argv_log(*writes, *fine)), [joined(c) for c in writes])

    def test_an_allowed_write_takes_only_its_flags(self):
        # round 5 review, finding 4: an allowed write is its command with the flags and arguments merge-up.sh gives it
        self.assertEqual(unallowed_writes(argv_log(*self.ALLOWED, 'pr close 1 -Ro/r --comment=c', 'pr edit 1 --title t -R o/r',
                                                   ['pr', 'comment', '1', '-R', 'o/r', '--body', '--delete-branch'])), [])
        writes = ['pr close 1 -R o/r --comment c --delete-branch', 'pr close 1 -R o/r -d --comment c',
                  'pr close 1 -dR o/r --comment c', 'pr close 1 --repo o/r --comment c', 'pr close 1 -R o/r',
                  'pr close 1 2 -R o/r --comment c', 'pr close -R o/r --comment c',
                  'pr edit 1 -R o/r --title t --base main', 'pr edit 1 -R o/r --base main',
                  'pr edit 1 -R o/r --title t --add-reviewer x', 'pr edit 1 -R o/r --title t --body b',
                  'pr comment 1 -R o/r --body x --edit-last', 'pr comment 1 -R o/r --body-file b',
                  'pr create -R o/r --base main --head h --title t --body-file b --draft',
                  'pr create -R o/r --base main --head h --fill', 'pr create 1 -R o/r --base main --head h --title t --body-file b',
                  'api -X POST repos/o/r/commits/abc/comments -f body=x -f event=APPROVE',
                  'api -X POST repos/o/r/commits/abc/comments -f body=x -H X-A:b',
                  'api --method POST repos/o/r/commits/abc/comments -f body=x',
                  'api -X POST repos/o/r/commits/abc/comments -F body=x',
                  'api -X POST repos/o/r/commits/abc/comments --input body']
        self.assertEqual(unallowed_writes(argv_log(*writes)), [joined(c) for c in writes])
        # the pins are the argument: a caller pins its own write
        dispatch = GhWrite('workflow run', frozenset({'--ref', '-f'}), 1)
        self.assertEqual(unallowed_writes(argv_log('workflow run w.yml --ref main -f a=1 -f b=2'), allowed=(dispatch,)), [])
        self.assertEqual(unallowed_writes(argv_log('workflow run w.yml --ref main -f a=1 -R o/r'), allowed=(dispatch,)),
                         ['workflow run w.yml --ref main -f a=1 -R o/r'])

    def test_the_allowlist_is_every_write_merge_up_sh_makes_but_the_merges(self):
        # derived from the code: every gh subcommand and gh api method merge-up.sh calls is a read, an allowed write
        # or one of its merges; a new write in it fails here until it is placed
        with open(MERGE_UP) as f:
            # the lines that run gh: not a comment, not a message that names a command for a person to run
            text = '\n'.join(l for l in f.read().splitlines() if not l.lstrip().startswith('#') and not re.search(r'\becho\b', l))
        subs = set(re.findall(r'(?m)\bgh (pr [a-z]+|run [a-z]+|workflow [a-z]+|release [a-z]+|repo [a-z]+|issue [a-z]+)\b', text))
        writes = {a.command: a for a in ALLOWED_WRITES if isinstance(a, GhWrite)}
        self.assertEqual(subs - set(GH_READS) - set(writes), {'pr merge'})
        methods = set(re.findall(r'\bgh api\b[^\n]*?(?:-X ?|--method[ =])([A-Za-z]+)', text))
        self.assertEqual(methods - {'GET'} - {a.method for a in ALLOWED_WRITES if isinstance(a, ApiWrite)}, {'PUT'})
        # round 5 review, finding 4: each pin is exactly the flags and arguments merge-up.sh gives that write, every time
        for command, pin in writes.items():
            calls = shell_gh_calls(MERGE_UP, command)
            with self.subTest(command=command):
                self.assertTrue(calls)
                self.assertEqual([c for c in calls if not allowed_write(c, (pin,))], [])
        # and merge-up.sh makes no gh api write but its merges: none is allowed, so a new one fails here until it is
        # pinned
        self.assertTrue(shell_gh_calls(MERGE_UP, 'api'))
        self.assertEqual([c for c in shell_gh_calls(MERGE_UP, 'api') if api_call(c[1:]).method not in ('GET', 'PUT')], [])

    def test_the_stub_takes_every_spelling_of_an_endpoint(self):
        # the stub reads the path as GitHub does (api_parts, pasted into it): the same endpoint whatever the prefix,
        # the case of the host, the port, the fragment or the query string
        self.assertIn(API_PATH_SRC, GH_STUB)
        for path in ('repos/o/r/pulls/1/merge', '/repos/o/r/pulls/1/merge', 'https://api.github.com/repos/o/r/pulls/1/merge',
                     'repos/o/r/pulls/1/merge?merge_method=merge&sha=abc', '/repos/o/r/pulls/1/merge?sha=abc',
                     'repos/o/r/pulls/1/merge#', 'repos/o/r/pulls/1/merge#x?y', 'https://API.GITHUB.COM/repos/o/r/pulls/1/merge',
                     'https://api.github.com:443/repos/o/r/pulls/1/merge', 'HTTPS://Api.GitHub.Com:443/repos/o/r/pulls/1/merge#x'):
            with self.subTest(path=path):
                self.assertEqual(api_parts(path)[0], 'repos/o/r/pulls/1/merge')
        self.assertEqual(api_parts('/repos/o/r/pulls?state=all&head=o:x#f'), ('repos/o/r/pulls', 'state=all&head=o:x'))
        for path in ('graphql', '/graphql', 'graphql#', 'graphql?x=1', 'https://API.github.com:443/graphql'):
            with self.subTest(path=path):
                self.assertEqual(api_parts(path)[0], 'graphql')


class MergeUpTests(unittest.TestCase):
    """1.x.x (child) into main (parent), the line opened by create-branch."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.main_tip = self.fx.heads()['main']
        self.fx.tag('v1.0.0', self.main_tip)
        self.fx.open_line('1.0.0')
        self.line_open = self.fx.heads()['1.x.x']

    def child_work(self, trailing_versions=True):
        """A foo.txt fix and a bookkeeping-file change on the child, then (optionally) a
        versions-only commit. Marks the pre-trailing sha green."""
        self.green_sha = self.fx.commit('1.x.x', {'foo.txt': 'child fix\n', ENV_FILE: 'DS_TEGRA_DISTRO_REF=child\n'}, 'child fix and env')
        write(self.fx.green, self.green_sha + '\n')
        if trailing_versions:
            self.child_tip = self.fx.commit('1.x.x', {'versions/ds': '1.2.0\n'}, 'release: pending 1.2.0 [skip ci]')
        else:
            self.child_tip = self.green_sha
        return self.child_tip

    def test_merge_up(self):
        child_tip = self.child_work()
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 56 (id 1234)', r.stdout)
        self.assertIn('only touches versions/', r.stdout)
        # two-parent commit: the target's versions/ and bookkeeping file, the child's foo.txt
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^1'), self.main_tip)
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)
        self.assertEqual(self.fx.subject(tip), 'Merge 1.x.x into main')
        self.assertEqual(self.fx.author(tip), 'ds-os-release-bot[bot] <346446406+ds-os-release-bot[bot]@users.noreply.github.com>')
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '0.0.1')
        self.assertEqual(self.fx.show(tip, ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')
        self.assertEqual(self.fx.show(tip, 'foo.txt'), 'child fix')
        self.assertIn('post-check ok', r.stdout)
        self.assertEqual(self.fx.prs(), [])
        # the child line itself is untouched
        self.assertEqual(self.fx.heads()['1.x.x'], child_tip)
        # a second run has nothing to do
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)
        self.assertEqual(self.fx.heads()['main'], tip)

    def test_not_green(self):
        self.child_work()
        write(self.fx.green, '')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on 1.x.x', r.stdout)
        self.assertEqual(self.fx.heads()['main'], self.main_tip)

    def test_called_from_the_rc_build_run(self):
        """workflow_run after the child tip's own green rc-build run: CALLER_SHA is the tip and
        CALLER_RUN that run, so the green check takes the caller's word and never asks gh
        (the lookup would find nothing: the green list is empty)."""
        child_tip = self.child_work(trailing_versions=False)
        write(self.fx.green, '')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 78 (id 4321)', r.stdout)
        self.assertIn('the calling run', r.stdout)
        self.assertNotIn('run list', self.fx.gh_log())
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)

    def test_caller_sha_without_a_run_or_off_the_tip_still_looks_up(self):
        child_tip = self.child_work(trailing_versions=False)
        write(self.fx.green, '')
        # a dispatch (CALLER_RUN empty) does the lookup even with CALLER_SHA set
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': ''})
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on 1.x.x', r.stdout)
        self.assertIn('run list', self.fx.gh_log())
        # the child moved since the caller's sha: the lookup runs for the new tip
        later = self.fx.commit('1.x.x', {'foo.txt': 'later\n'}, 'later fix')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on 1.x.x', r.stdout)
        self.assertEqual(self.fx.heads()['main'], self.main_tip)
        # and finds it once that tip is green
        write(self.fx.green, later + '\n')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 56 (id 1234)', r.stdout)
        self.assertNotIn('the calling run', r.stdout)

    def test_parent_without_versions_dir(self):
        self.child_work()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', 'main', 'origin/main')
        git(self.fx.repo, 'rm', '-q', '-r', 'versions')
        git(self.fx.repo, 'commit', '-q', '-m', 'drop versions')
        git(self.fx.repo, 'push', '-q', 'origin', 'main')
        before = self.fx.heads()
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: main has no versions/ directory', r.stdout)
        self.assertNotIn('pathspec', r.stderr)
        self.assertEqual(self.fx.heads(), before)

    def test_dry_run_and_no_app(self):
        self.child_work()
        before = self.fx.heads()
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing pushed', r.stdout)
        self.assertIn('merge 1.x.x', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', dry_run='true', token_kind='github')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing pushed', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', token_kind='github')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: no App token', r.stdout)
        self.assertIn('git merge --no-ff --no-commit origin/1.x.x; python3 scripts-release/release_git.py merge-keep-target', r.stdout)
        self.assertEqual(self.fx.heads(), before)
        self.assertEqual(self.fx.prs(), [])

    def test_refusals(self):
        r = self.fx.run(MERGE_UP, '--child', 'main')
        self.assertEqual(r.returncode, 1)
        self.assertIn('main', r.stdout)
        self.assertEqual(self.fx.heads()['main'], self.main_tip)
        r = self.fx.run(MERGE_UP, '--child', 'dev/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not a release line', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--parent', '2.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('the parent line of 1.x.x is main', r.stdout)

    def test_conflict_and_finish(self):
        child_tip = self.child_work()
        # the parent moved foo.txt too, and its pending version must survive
        parent_tip = self.fx.commit('main', {'foo.txt': 'main fix\n', 'versions/ds': '2.0.0\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('conflicts in: foo.txt', r.stdout)
        self.assertIn('::error::merge-up: 1.x.x -> main conflicts (foo.txt)', r.stdout)
        # named after the child's green commit, not the versions-only tip on top (ENG-2233)
        self.assertNotEqual(self.green_sha, child_tip)
        branch = f'merge-up/1.x.x/{self.green_sha[:7]}'
        # the PR branch is the parent with the child merged in, the markers committed (ENG-2200)
        head = self.fx.heads()[branch]
        self.assertEqual(git(self.fx.bare, 'rev-parse', head + '^1'), parent_tip)
        self.assertEqual(git(self.fx.bare, 'rev-parse', head + '^2'), child_tip)
        self.assertEqual(self.fx.subject(head), 'Merge 1.x.x into main (conflicts to resolve)')
        marked = self.fx.show(head, 'foo.txt')
        self.assertIn('<<<<<<< ', marked)
        self.assertIn('||||||| ', marked)   # zdiff3: the base is in the hunk (ENG-2233)
        self.assertIn('>>>>>>> ', marked)
        self.assertEqual(self.fx.show(head, 'versions/ds'), '2.0.0')              # kept from the parent, not a conflict
        self.assertEqual(self.fx.show(head, ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')  # the bookkeeping file too
        self.assertIn(f'pushed {branch}: {head[:7]}, main plus the merge of 1.x.x with markers in foo.txt', r.stdout)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        self.assertEqual((prs[0]['repo'], prs[0]['base'], prs[0]['head']), (REPO, 'main', branch))
        self.assertEqual(prs[0]['title'], 'Merge-up 1.x.x into main (conflict)')
        self.assertIn('- `foo.txt`', prs[0]['body'])
        self.assertIn('The conflict markers are committed in the files above', prs[0]['body'])
        self.assertIn('The bot refuses to finish while a marker remains', prs[0]['body'])
        self.assertIn(prs[0]['url'], r.stdout)
        # a re-run leaves the branch alone and reuses the PR
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual(self.fx.heads()[branch], head)
        self.assertEqual(len(self.fx.prs()), 1)
        # a dry run of the same conflict pushes nothing and opens nothing
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the branch is already on origin (the run above pushed it): the dry run says it is reused
        self.assertIn(f'dry run: {branch} already exists on origin; a real run leaves it as it is and reuses its PR', r.stdout)
        self.assertEqual(self.fx.heads()[branch], head)
        self.assertEqual(len(self.fx.prs()), 1)
        # not approved yet: refused, not merged
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        # approved by the pusher only: refused
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'resolver'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': 'resolver'}]}]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(review))
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('other than the last pusher', r.stderr)
        # approved by someone else but the markers are still in: refused
        review['reviews'].append({'author': {'login': 'alice'}, 'state': 'APPROVED'})
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(review))
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: PR ds/repo#1 still has conflict markers in: foo.txt', r.stdout)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        # a human resolves on the PR branch: edit the marked file, commit, push
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.fx.repo, 'foo.txt'), 'both fixes\n')
        git(self.fx.repo, 'add', '-A')
        git(self.fx.repo, 'commit', '-q', '-m', 'resolve')
        git(self.fx.repo, 'push', '-q', 'origin', branch)
        pr_head = git(self.fx.repo, 'rev-parse', 'HEAD')
        # resolved and approved: merged, post-check on the new tip
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('would gh pr merge 1', r.stdout)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), pr_head)
        git(self.fx.bare, 'merge-base', '--is-ancestor', child_tip, tip)
        self.assertIn('via PR 1', r.stdout)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        # what the human left on the PR branch is what merged (the PR path is the human's call)
        self.assertEqual(self.fx.show(tip, 'foo.txt'), 'both fixes')

    # --- ENG-2233: supersession, stale PRs, the marker check's scope -------------

    def conflict_pr(self):
        """child_work, a conflicting parent commit, a merge-up run: the conflict PR #1 is open."""
        child_tip = self.child_work()
        parent_tip = self.fx.commit('main', {'foo.txt': 'main fix\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        return child_tip, parent_tip, f'merge-up/1.x.x/{self.green_sha[:7]}'

    def child_moves(self, content='child fix 2\n'):
        """A new green commit on the child that still conflicts with the parent's foo.txt."""
        tip = self.fx.commit('1.x.x', {'foo.txt': content}, 'another child fix')
        write(self.fx.green, tip + '\n')
        return tip

    def approve(self, number, pusher='resolver'):
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': pusher}]}]}
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps(review))

    def resolve(self, branch, content='both fixes\n', merge_child=False):
        """A human edits foo.txt on the PR branch (optionally merging the child in first) and pushes."""
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        if merge_child:
            subprocess.run(['git', '-C', self.fx.repo, 'merge', '-q', '--no-edit', 'origin/1.x.x'],
                           capture_output=True, text=True,
                           env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x'))
        write(os.path.join(self.fx.repo, 'foo.txt'), content)
        git(self.fx.repo, 'add', '-A')
        git(self.fx.repo, 'commit', '-q', '-m', 'resolve')
        git(self.fx.repo, 'push', '-q', 'origin', branch)
        return git(self.fx.repo, 'rev-parse', 'HEAD')

    def test_a_newer_conflict_pr_closes_the_older_one(self):
        _, _, old_branch = self.conflict_pr()
        old_head = self.fx.heads()[old_branch]
        new_child = self.child_moves()
        new_branch = f'merge-up/1.x.x/{new_child[:7]}'
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'dry run: would close superseded PR {REPO}#1 ({old_branch}), replaced by the new conflict PR on {new_branch}', r.stdout)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        prs = {p['number']: p for p in self.fx.prs()}
        self.assertEqual((prs[2]['head'], prs[2]['state']), (new_branch, 'OPEN'))
        self.assertEqual(prs[1]['state'], 'CLOSED')
        self.assertIn(f'Superseded by {prs[2]["url"]}', prs[1]['close_comment'])
        self.assertIn(f'The branch `{old_branch}` is kept', prs[1]['close_comment'])
        self.assertEqual(self.fx.heads()[old_branch], old_head)
        # its writes are the open and the close as pinned: a close that also deletes the branch (--delete-branch,
        # which the stub would not act on) is not one (round 5 review, finding 4)
        self.assertEqual(unallowed_writes(self.fx.gh_argv()), [])
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('is not an open merge-up/1.x.x/* PR', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('closed superseded', r.stdout)
        self.assertEqual(self.fx.prs()[1]['state'], 'OPEN')

    def test_a_clean_merge_closes_open_conflict_prs(self):
        child_tip, _, _ = self.conflict_pr()
        self.fx.commit('main', {'foo.txt': 'child fix\n'}, 'take the child fix on main')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'Superseded by the clean merge {tip}', pr['close_comment'])

    def test_a_stale_pr_is_refused_before_it_merges(self):
        _, parent_tip, branch = self.conflict_pr()
        self.resolve(branch)
        self.approve(1)
        new_child = self.child_moves()
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR {REPO}#1 is stale: 1.x.x is at {new_child[:7]} now and {branch} does not contain it', r.stdout)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        pr_head = self.resolve(branch, content='all three fixes\n', merge_child=True)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), pr_head)
        git(self.fx.bare, 'merge-base', '--is-ancestor', new_child, tip)

    def test_markers_outside_the_pr_do_not_block_the_finish(self):
        self.fx.commit('main', {'docs/conflicts.md': 'An example:\n<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n'}, 'doc with a conflict example')
        _, _, branch = self.conflict_pr()
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('still has conflict markers in: foo.txt', r.stdout)
        self.assertNotIn('docs/conflicts.md', r.stdout)
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('via PR 1', r.stdout)

    # --- ENG-2233 review fixes ------------------------------------------------------

    def inject_pr(self, number, head, sha, base='main'):
        """An open PR in the stub on a branch pointing at <sha>."""
        git(self.fx.bare, 'update-ref', f'refs/heads/{head}', sha)
        prs = self.fx.prs()
        prs.append({'number': number, 'repo': REPO, 'base': base, 'head': head, 'title': 'x', 'body': '',
                    'state': 'OPEN', 'url': f'https://github.com/{REPO}/pull/{number}'})
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))

    def state_of(self, number):
        return [p['state'] for p in self.fx.prs() if p['number'] == number][0]

    def test_a_conflict_without_markers_goes_on_the_child_tip(self):
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', '1.x.x', 'origin/1.x.x')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        git(self.fx.repo, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.repo, 'push', '-q', 'origin', '1.x.x')
        child_tip = git(self.fx.repo, 'rev-parse', 'HEAD')
        write(self.fx.green, child_tip + '\n')
        parent_tip = self.fx.commit('main', {'foo.txt': 'main fix\n'}, 'main fix')
        branch = f'merge-up/1.x.x/{child_tip[:7]}'
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('no text markers in: foo.txt', r.stdout)
        self.assertIn(f'dry run: would push {branch} at {child_tip[:7]} (the child, no markers committed)', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads()[branch], child_tip)
        self.assertEqual(self.fx.heads()['main'], parent_tip)
        body = self.fx.prs()[0]['body']
        self.assertIn('Git cannot write conflict markers for the files marked above', body)
        self.assertIn('- `foo.txt`', body)

    def test_the_no_marker_body_names_a_resolution_that_keeps_the_parents_files(self):
        # F5 (2026-11-18): the no-marker body said "check out <head>, merge <base> into it". From
        # that side a plain merge takes the child's copy of a bookkeeping file only the child
        # changed (it merges clean), and merge-keep-target keeps the child's versions/ and
        # bookkeeping files (dexterds14/ds-release-scratch #8); finish_pr checks neither. The body
        # now names the merge-up's own direction; run its command as written, in a checkout.
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', '1.x.x', 'origin/1.x.x')
        git(self.fx.repo, 'rm', '-q', 'foo.txt')
        write(os.path.join(self.fx.repo, ENV_FILE), 'DS_TEGRA_DISTRO_REF=child\n')
        git(self.fx.repo, 'commit', '-q', '-am', 'drop foo, child env')
        git(self.fx.repo, 'push', '-q', 'origin', '1.x.x')
        child_tip = git(self.fx.repo, 'rev-parse', 'HEAD')
        write(self.fx.green, child_tip + '\n')
        parent_tip = self.fx.commit('main', {'foo.txt': 'main fix\n', 'versions/ds': '2.0.0\n'}, 'main fix')
        branch = f'merge-up/1.x.x/{child_tip[:7]}'
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        body = self.fx.prs()[0]['body']
        self.assertEqual(self.fx.heads()[branch], child_tip)
        self.assertNotEqual(self.fx.show(child_tip, 'versions/ds'), '2.0.0')
        # the path the old body named: the bookkeeping file merges clean to the child's copy
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', 'old-way', f'origin/{branch}')
        subprocess.run(['git', 'merge', '--no-ff', '--no-commit', 'origin/main'], cwd=self.fx.repo, capture_output=True)
        self.assertNotIn(ENV_FILE, git(self.fx.repo, 'diff', '--name-only', '--diff-filter=U'))
        with open(os.path.join(self.fx.repo, ENV_FILE)) as f:
            self.assertEqual(f.read(), 'DS_TEGRA_DISTRO_REF=child\n')
        git(self.fx.repo, 'merge', '--abort')
        # the body's procedure, its fenced command run as written in a checkout with scripts-release/
        shutil.copytree(SCRIPTS, os.path.join(self.fx.repo, 'scripts-release'),
                        ignore=shutil.ignore_patterns('tests', '__pycache__'))
        self.assertIn('Do not merge `main` into this branch instead', body)
        blocks = body.split('```\n')
        self.assertEqual(len(blocks), 3, body)
        cmd = blocks[1].strip()
        self.assertIn(f'git merge --no-ff --no-commit origin/{branch}; python3 scripts-release/release_git.py merge-keep-target', cmd)
        r = subprocess.run(['bash', '-c', cmd], cwd=self.fx.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        git(self.fx.repo, 'rm', '-q', 'foo.txt')  # decide the file the body lists
        subject = body.split("git commit -m '", 1)[1].split("'", 1)[0]
        self.assertEqual(subject, 'Merge 1.x.x into main')
        git(self.fx.repo, 'commit', '-q', '-m', subject)
        push = body.split('`git push origin ', 1)[1].split('`', 1)[0]
        self.assertEqual(push, f'HEAD:{branch}')
        git(self.fx.repo, 'push', '-q', 'origin', push)  # no --force: a fast-forward
        head = self.fx.heads()[branch]
        self.assertEqual(git(self.fx.bare, 'rev-parse', f'{head}^1', f'{head}^2').split(), [parent_tip, child_tip])
        self.assertEqual(self.fx.show(head, 'versions/ds'), '2.0.0')
        self.assertEqual(self.fx.show(head, ENV_FILE), 'DS_TEGRA_DISTRO_REF=main')
        self.assertEqual(git(self.fx.bare, 'diff', '--name-only', parent_tip, head, '--', 'versions/', ENV_FILE), '')

    def test_the_zdiff3_base_line_left_in_is_refused(self):
        _, _, branch = self.conflict_pr()
        git(self.fx.repo, 'fetch', '-q', 'origin')
        git(self.fx.repo, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        path = os.path.join(self.fx.repo, 'foo.txt')
        with open(path) as f:
            kept = [l for l in f if not l.startswith(('<<<<<<< ', '>>>>>>> '))]
        self.assertTrue(any(l.startswith('||||||| ') for l in kept))
        write(path, ''.join(kept))
        git(self.fx.repo, 'commit', '-q', '-am', 'half resolve')
        git(self.fx.repo, 'push', '-q', 'origin', branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('still has conflict markers in: foo.txt', r.stdout)
        self.assertIn('`|||||||`', self.fx.prs()[0]['body'])

    def test_a_versions_bump_neither_supersedes_nor_staleness(self):
        _, _, branch = self.conflict_pr()
        bump = self.fx.commit('1.x.x', {'versions/ds': '1.3.0\n'}, 'release: pending 1.3.0 [skip ci]')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        pr_head = self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), pr_head)
        self.assertIn(f'--match-head-commit {pr_head}', self.fx.gh_log())
        self.assertNotEqual(bump, pr_head)

    def test_an_open_pr_that_contains_the_child_is_reused(self):
        _, _, branch = self.conflict_pr()
        new_child = self.child_moves()
        self.resolve(branch, content='all three fixes\n', merge_child=True)
        git(self.fx.bare, 'merge-base', '--is-ancestor', new_child, f'refs/heads/{branch}')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'open conflict PR {REPO}#1 ({branch}) already contains 1.x.x at {new_child[:7]}; reusing it', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        self.assertNotIn(f'merge-up/1.x.x/{new_child[:7]}', self.fx.heads())

    def test_every_outcome_closes_older_prs_and_only_this_childs(self):
        _, _, branch = self.conflict_pr()
        base = self.fx.heads()['main']
        self.inject_pr(10, 'merge-up/1.x.x/0000000', base)
        self.inject_pr(11, 'merge-up/1.0.x/abc1234', base)
        self.inject_pr(12, 'merge-up/1.x.xx/abc1234', base)
        self.inject_pr(13, 'feature/merge-up/1.x.x/abc1234', base)
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state_of(10), 'CLOSED')
        for n in (11, 12, 13):
            self.assertEqual(self.state_of(n), 'OPEN', n)
        self.inject_pr(14, 'merge-up/1.x.x/1111111', base)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)
        self.assertEqual(self.state_of(14), 'CLOSED')
        for n in (11, 12, 13):
            self.assertEqual(self.state_of(n), 'OPEN', n)

    def test_supersession_sees_past_gh_default_page_of_30(self):
        _, _, branch = self.conflict_pr()
        base = self.fx.heads()['main']
        self.inject_pr(5, 'merge-up/1.x.x/0000000', base)
        for n in range(100, 135):
            self.inject_pr(n, f'feature/f{n}', base)
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', '1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state_of(5), 'CLOSED')
        self.assertEqual(self.state_of(100), 'OPEN')

    def test_versions_and_bookkeeping_kept_target_side(self):
        """The parent keeps versions/<product> and the bookkeeping file even when both sides changed
        them (a conflict there never opens a PR); the README and everything else merge."""
        self.child_work(trailing_versions=False)
        self.fx.commit('main', {'versions/ds': '3.0.0\n', ENV_FILE: 'DS_TEGRA_DISTRO_REF=parent\n'}, 'parent pending version and env')
        # a README fix on the child must reach the parent: only the per-product files are the
        # target's, versions/README.md merges like any doc
        child_tip = self.fx.commit('1.x.x', {'versions/README.md': 'pending versions, fixed\n', 'versions/ds': '1.1.9\n'},
                                   'child: README fix and its own pending version')
        write(self.fx.green, child_tip + '\n')
        r = self.fx.run(MERGE_UP, '--child', '1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)
        self.assertEqual(self.fx.show(tip, 'versions/ds'), '3.0.0')
        self.assertEqual(self.fx.show(tip, ENV_FILE), 'DS_TEGRA_DISTRO_REF=parent')
        self.assertEqual(self.fx.show(tip, 'versions/README.md'), 'pending versions, fixed')
        self.assertEqual(self.fx.show(tip, 'foo.txt'), 'child fix')
        self.assertEqual(self.fx.prs(), [])
        # the merge is bookkeeping-free from the child's point of view: nothing left unmerged
        self.assertEqual(release_git.unmerged(self.fx.bare, 'main', '1.x.x', [ENV_FILE]), [])


class MergeUpSwitchTests(unittest.TestCase):
    """The switches block every merge-up run prints (ENG-2234): the variable, the
    active rules, the ref whose scripts are in use. No rule exists yet, so a run
    does the same whatever the variable says, unless it does not parse."""

    # the MergeUpTests fixture (child 1.x.x, parent main), without its cases
    setUp = MergeUpTests.setUp
    child_work = MergeUpTests.child_work

    def scripts_sha(self):
        return git(os.path.dirname(SCRIPTS), 'rev-parse', '--short=7', 'HEAD')

    def run_up(self, extra_env=None, **kw):
        return self.fx.run(MERGE_UP, '--child', '1.x.x', extra_env=extra_env or {}, **kw)

    def block(self, stdout):
        """The lines of the switches block, by label."""
        lines = stdout.splitlines()
        start = lines.index('== merge-up automation')
        out = {}
        for line in lines[start + 1:start + 5]:
            label, _, value = line.partition(':')
            out[label] = value.strip()
        return out

    def test_variable_absent_prints_off_and_merges(self):
        child_tip = self.child_work()
        r = self.run_up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        b = self.block(r.stdout)
        self.assertEqual(b['variable'], 'DS_MERGE_UP_AUTOMATION is absent')
        self.assertEqual(b['active rules'], 'none (automation: off, no rule is evaluated)')
        self.assertTrue(b['scripts'].endswith(f'at {self.scripts_sha()}'), b['scripts'])
        self.assertEqual(b['config'], 'finish.strict=false (release/config.json at that ref)')
        # the block comes first, before the plan
        self.assertLess(r.stdout.index('== merge-up automation'), r.stdout.index('== merge-up plan'))
        tip = self.fx.heads()['main']
        self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)

    def test_every_valid_value_merges_the_same(self):
        """shadow and a rule list change the printed lines and nothing else yet."""
        want = {
            'off': 'none (automation: off, no rule is evaluated)',
            'shadow': 'none acting; shadow (evaluate and log only): splice',
            ' splice ': 'acting: splice; shadow (evaluate and log only): none',
        }
        for value, rules in want.items():
            with self.subTest(value=value):
                self.setUp()  # a fresh fixture per value
                child_tip = self.child_work()
                r = self.run_up({'DS_MERGE_UP_AUTOMATION': value})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                b = self.block(r.stdout)
                self.assertEqual(b['variable'], f'DS_MERGE_UP_AUTOMATION={value.strip()}')
                self.assertEqual(b['active rules'], rules)
                tip = self.fx.heads()['main']
                self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^1'), self.main_tip)
                self.assertEqual(git(self.fx.bare, 'rev-parse', tip + '^2'), child_tip)
                self.assertEqual(self.fx.subject(tip), 'Merge 1.x.x into main')
                self.assertEqual(self.fx.show(tip, 'foo.txt'), 'child fix')
                self.assertEqual(self.fx.prs(), [])
                self.assertIn('post-check ok', r.stdout)

    def test_a_value_that_does_not_parse_stops_before_anything(self):
        self.child_work()
        before = self.fx.heads()
        for value, token in (('on', "'on'"), ('splice,lockpin', "'lockpin'"), ('shadow,splice', "'shadow'"),
                             ('Shadow', "'Shadow'"), ('splice,', 'empty item'),
                             ('lock-pin,splice,pins', "'lock-pin' is not a rule of this repo")):
            with self.subTest(value=value):
                r = self.run_up({'DS_MERGE_UP_AUTOMATION': value})
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('::error::merge-up: DS_MERGE_UP_AUTOMATION=', r.stdout)
                self.assertIn(token, r.stdout)
                self.assertIn('nothing was read or pushed', r.stdout)
                self.assertNotIn('== merge-up plan', r.stdout)
                # a dry run refuses it too: the mistake shows before a real run meets it
                r = self.run_up({'DS_MERGE_UP_AUTOMATION': value}, dry_run='true')
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(), before)
        self.assertNotIn('run list', self.fx.gh_log())
        self.assertNotIn('pr ', self.fx.gh_log())
        self.assertEqual(self.fx.prs(), [])

    def test_printed_on_a_dry_run_and_on_a_run_that_refuses(self):
        self.child_work()
        r = self.run_up({'DS_MERGE_UP_AUTOMATION': 'shadow'}, dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.block(r.stdout)['variable'], 'DS_MERGE_UP_AUTOMATION=shadow')
        self.assertIn('dry run: nothing pushed', r.stdout)
        r = self.run_up({'DS_MERGE_UP_AUTOMATION': 'shadow'}, token_kind='github', dry_run='true')
        self.assertEqual(self.block(r.stdout)['variable'], 'DS_MERGE_UP_AUTOMATION=shadow')
        write(self.fx.green, '')
        r = self.run_up({'DS_MERGE_UP_AUTOMATION': 'splice'})
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run', r.stdout)
        self.assertEqual(self.block(r.stdout)['active rules'], 'acting: splice; shadow (evaluate and log only): none')

    def test_the_scripts_ref_line(self):
        """Which ref's scripts and config are in use, and whether that is the default branch."""
        self.child_work()
        root = os.path.dirname(SCRIPTS)
        ref = subprocess.run(['git', '-C', root, 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                             capture_output=True, text=True).stdout.strip()
        sha = self.scripts_sha()
        r = self.run_up({'GITHUB_REF_NAME': 'from-the-runner'}, dry_run='true')
        # the checkout's own branch wins; GITHUB_REF_NAME only names a detached checkout
        self.assertEqual(self.block(r.stdout)['scripts'], f'{ref or "from-the-runner"} at {sha}')
        ref = ref or 'from-the-runner'
        r = self.run_up({'GITHUB_REF_NAME': 'from-the-runner', 'DEFAULT_BRANCH': ref}, dry_run='true')
        self.assertEqual(self.block(r.stdout)['scripts'], f'{ref} at {sha} (the default branch)')
        r = self.run_up({'GITHUB_REF_NAME': 'from-the-runner', 'DEFAULT_BRANCH': 'elsewhere'}, dry_run='true')
        self.assertEqual(self.block(r.stdout)['scripts'],
                         f"{ref} at {sha} (NOT the default branch elsewhere: this run uses that ref's "
                         'scripts and release/config.json)')

    def test_the_block_ends_with_the_bot_finish_line(self):
        """bot-finish (ENG-2235, 2c) is said on the block's last line, in every run: after the config
        line (block() reads the four before it), and in the step summary too."""
        want = {
            None: 'off (a rule-resolved PR waits for one approval)',
            'off': 'off (a rule-resolved PR waits for one approval)',
            'shadow': 'shadow (log only: a rule-resolved PR waits for one approval)',
            'splice': 'shadow (log only: a rule-resolved PR waits for one approval)',
            'splice,bot-finish': "acting (a rule-resolved PR is merged with no approval once its required checks succeed on the bot's commit)",
            'bot-finish': 'acting, but splice does not act, so no rule-resolved PR is made and nothing is finished',
        }
        self.child_work()
        for value, text in want.items():
            with self.subTest(value=value):
                summary = os.path.join(self.fx.top, 'summary.md')
                env = {'MERGE_UP_SUMMARY': summary}
                if value is not None:
                    env['DS_MERGE_UP_AUTOMATION'] = value
                r = self.run_up(env, dry_run='true')
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                lines = r.stdout.splitlines()
                start = lines.index('== merge-up automation')
                self.assertTrue(lines[start + 4].startswith('config:'), lines[start:start + 6])
                self.assertEqual(lines[start + 5], f'bot finish:         {text}')
                self.assertEqual(r.stdout.count('bot finish:'), 1)
                with open(summary) as f:
                    self.assertIn(f'bot finish:         {text}\n```\n', f.read())
                os.remove(summary)

    def test_the_block_goes_to_the_step_summary(self):
        self.child_work()
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.run_up({'MERGE_UP_SUMMARY': summary, 'DS_MERGE_UP_AUTOMATION': 'splice'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(summary) as f:
            text = f.read()
        self.assertTrue(text.startswith('### Merge-up 1.x.x\n\n```\n== merge-up automation\n'), text)
        self.assertIn('variable:           DS_MERGE_UP_AUTOMATION=splice\n', text)
        self.assertIn('active rules:       acting: splice; shadow (evaluate and log only): none\n', text)
        self.assertIn(f'at {self.scripts_sha()}', text)
        self.assertTrue(text.endswith('```\n'))
        # a refused value leaves the summary alone: the error annotation carries it
        os.remove(summary)
        r = self.run_up({'MERGE_UP_SUMMARY': summary, 'DS_MERGE_UP_AUTOMATION': 'nope'})
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(summary))


class MergeUpWorkflowTests(unittest.TestCase):
    """release-merge-up.yml hands the switches to merge-up.sh (ENG-2234). Text checks:
    the suite is standard library only."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up.yml')) as f:
            self.text = f.read()

    def step(self, name):
        """The text of one step: from its `- name:` line to the blank line that ends it."""
        start = self.text.index(f'      - name: {name}\n')
        end = self.text.find('\n\n', start)
        return self.text[start:end if end != -1 else len(self.text)] + '\n'

    def test_the_variable_reaches_the_script(self):
        merge = self.step('Merge up')
        self.assertIn('DS_MERGE_UP_AUTOMATION: ${{ vars.DS_MERGE_UP_AUTOMATION }}\n', merge)
        self.assertIn('DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}\n', merge)
        self.assertIn('export MERGE_UP_SUMMARY="${GITHUB_STEP_SUMMARY}"\n', merge)
        # nowhere else: no job or step decides anything from the variable
        self.assertEqual(self.text.count('vars.DS_MERGE_UP_AUTOMATION'), 2)  # the header comment and the env line

    def test_one_token_and_only_this_repo(self):
        """No rule here reads another repository (the pins rule is ds-tegra-distro's), so the
        job mints one App token, scoped to this repo, as before."""
        self.assertEqual(self.text.count('actions/create-github-app-token@'), 1)
        self.assertIn('repositories: ${{ github.event.repository.name }}\n', self.step('App token'))
        self.assertNotIn('MERGE_UP_READ_TOKEN', self.text)

    def test_the_merge_job_outlives_the_wait(self):
        """Under bot-finish (ENG-2235, 2c) the merge job waits up to merge_up.finish.wait_minutes for
        the required checks of a rule-resolved PR: it must outlive the longest wait the config
        allows, with room for the merge itself."""
        import re
        merge = self.text[self.text.index('\n  merge:\n'):self.text.index('\n  merge-up-skipped:\n')]
        timeouts = re.findall(r'^    timeout-minutes: (\d+)$', merge, re.M)
        self.assertEqual(len(timeouts), 1, merge)
        timeout = int(timeouts[0])
        self.assertEqual(timeout, 90)
        self.assertGreaterEqual(timeout, release_git.merge_up_config()['finish']['wait_minutes'] + 30)
        self.assertGreaterEqual(timeout, version.FINISH_WAIT_MAX + 30)


class IsolationTests(unittest.TestCase):
    """What the first grep of pr-checks' isolation audit refuses, run here too: the release
    tooling (scripts-release/*.sh and *.py, the release workflows; comments count) names no
    other repository and dispatches into none. merge_rules.py is one blob shared with the OS
    distro repo, so a name added to it there would only fail in this repo's pr-checks."""

    def test_the_release_tooling_names_no_other_repository(self):
        import glob
        import re
        root = os.path.dirname(SCRIPTS)
        with open(os.path.join(root, '.github/workflows/pr-checks.yml')) as f:
            audit = f.read()
        m = re.search(r"if grep -rn -E '([^']+)' \.github/workflows/release-\*\.yml scripts-release/\*\.sh scripts-release/\*\.py; then", audit)
        self.assertIsNotNone(m, 'the isolation audit of pr-checks.yml changed shape; update this test with it')
        pattern = re.compile(m.group(1))
        files = (glob.glob(os.path.join(root, '.github/workflows/release-*.yml'))
                 + glob.glob(os.path.join(SCRIPTS, '*.sh')) + glob.glob(os.path.join(SCRIPTS, '*.py')))
        self.assertGreater(len(files), 15)
        self.assertIn(os.path.join(SCRIPTS, 'merge_rules.py'), files)
        hits = []
        for path in files:
            with open(path) as f:
                hits += [f'{os.path.relpath(path, root)}:{n}: {line.strip()}' for n, line in enumerate(f, 1) if pattern.search(line)]
        self.assertEqual(hits, [])


class AutomationTests(unittest.TestCase):
    """DS_MERGE_UP_AUTOMATION parsing, merge_up_config and the never_auto match (ENG-2234)."""

    def test_absent_and_off_evaluate_nothing(self):
        for value in (None, '', '   ', 'off', ' off\n'):
            with self.subTest(value=value):
                auto = release_git.automation(value)
                self.assertEqual((auto['act'], auto['shadow']), ([], []))
                for rule in release_git.MERGE_UP_RULES:
                    self.assertEqual(release_git.rule_mode(auto, rule), 'off')

    def test_shadow_evaluates_every_rule_and_none_acts(self):
        auto = release_git.automation('shadow')
        self.assertEqual(auto, {'value': 'shadow', 'act': [], 'shadow': ['splice']})
        self.assertEqual(release_git.rule_mode(auto, 'splice'), 'shadow')

    def test_a_list_acts(self):
        for value in ('splice', ' splice ', 'splice,splice', ' splice , splice\n'):
            with self.subTest(value=value):
                auto = release_git.automation(value)
                self.assertEqual((auto['act'], auto['shadow']), (['splice'], []))
                self.assertEqual(release_git.rule_mode(auto, 'splice'), 'act')

    def test_anything_else_is_an_error_naming_the_token(self):
        cases = {
            'on': "'on' is not a rule",
            'true': "'true' is not a rule",
            'Shadow': "'Shadow' is not a rule",
            'OFF': "'OFF' is not a rule",
            'Splice': "'Splice' is not a rule",
            'splice,pin': "'pin' is not a rule",
            'splice splice': "'splice splice' is not a rule",
            'splice;splice': "'splice;splice' is not a rule",
            'shadow,splice': "'shadow' is not a rule (\"off\" and \"shadow\" stand alone",
            'splice,off': "'off' is not a rule (\"off\" and \"shadow\" stand alone",
            'splice,': 'empty item in the list',
            ',': 'empty item in the list',
            'splice,,splice': 'empty item in the list',
            # the rules only the OS distro repo has: a value copied from there stops the run here
            'lock-pin': "'lock-pin' is not a rule of this repo (it is a rule of the OS distro repo)",
            'lock-pin,splice,pins': "'lock-pin' is not a rule of this repo (it is a rule of the OS distro repo)",
            'splice,pins': "'pins' is not a rule of this repo (it is a rule of the OS distro repo)",
        }
        for value, needle in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(release_git.Error) as cm:
                    release_git.automation(value)
                self.assertIn(needle, str(cm.exception))
                self.assertIn('DS_MERGE_UP_AUTOMATION=', str(cm.exception))
                self.assertIn('off, shadow, or a comma list of splice', str(cm.exception))

    def test_rule_mode_refuses_an_unknown_rule(self):
        for rule in ('lock-pin', 'pins', 'Splice'):
            with self.assertRaises(release_git.Error):
                release_git.rule_mode(release_git.automation('shadow'), rule)

    def test_summary_lines(self):
        def lines(value):
            return release_git.automation_summary(release_git.automation(value))
        self.assertEqual(lines(None), ['variable:           DS_MERGE_UP_AUTOMATION is absent',
                                       'active rules:       none (automation: off, no rule is evaluated)'])
        self.assertEqual(lines('off'), ['variable:           DS_MERGE_UP_AUTOMATION=off',
                                        'active rules:       none (automation: off, no rule is evaluated)'])
        self.assertEqual(lines('shadow'), ['variable:           DS_MERGE_UP_AUTOMATION=shadow',
                                           'active rules:       none acting; shadow (evaluate and log only): splice'])
        self.assertEqual(lines('splice'), ['variable:           DS_MERGE_UP_AUTOMATION=splice',
                                           'active rules:       acting: splice; shadow (evaluate and log only): none'])

    def cli(self, *args, env_value=None):
        env = {k: v for k, v in os.environ.items() if k != release_git.AUTOMATION_VAR}
        if env_value is not None:
            env[release_git.AUTOMATION_VAR] = env_value
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), *args],
                              capture_output=True, text=True, env=env)

    def test_bot_finish_is_an_action_not_a_rule(self):
        # ENG-2235 (2c): the word is accepted in the list, changes no rule, and has a mode of its own
        auto = release_git.automation('splice,bot-finish')
        self.assertEqual(auto, {'value': 'splice,bot-finish', 'act': ['splice'], 'shadow': []})
        self.assertEqual(release_git.automation('bot-finish'), {'value': 'bot-finish', 'act': [], 'shadow': ['splice']})
        self.assertEqual(release_git.MERGE_UP_RULES, ('splice',))
        self.assertEqual(release_git.OTHER_REPO_RULES, ('lock-pin', 'pins'))
        self.assertEqual(release_git.MERGE_UP_ACTIONS, ('bot-finish',))
        with self.assertRaises(release_git.Error):
            release_git.rule_mode(auto, 'bot-finish')
        with self.assertRaises(release_git.Error):
            release_git.action_mode(auto, 'splice')
        cases = {None: 'off', '': 'off', 'off': 'off', 'shadow': 'shadow', 'splice': 'shadow',
                 'splice,bot-finish': 'act', ' bot-finish , splice ': 'act', 'bot-finish': 'act'}
        for value, mode in cases.items():
            with self.subTest(value=value):
                self.assertEqual(release_git.action_mode(release_git.automation(value), 'bot-finish'), mode)
        # still exact words: a mistyped or capitalised word stops the run, like any other token, and
        # the OS distro repo's rules stay refused here with the word too
        for value, needle in (('Bot-Finish', "'Bot-Finish' is not a rule"), ('bot_finish', "'bot_finish' is not a rule"),
                              ('splice,botfinish', "'botfinish' is not a rule"), ('bot-finish,shadow', "'shadow' is not a rule"),
                              ('lock-pin,bot-finish', "'lock-pin' is not a rule of this repo")):
            with self.subTest(value=value):
                with self.assertRaises(release_git.Error) as cm:
                    release_git.automation(value)
                self.assertIn(needle, str(cm.exception))
                self.assertIn('off, shadow, or a comma list of splice and the action bot-finish', str(cm.exception))

    def test_action_summary_lines(self):
        def line(value):
            return release_git.action_summary(release_git.automation(value))
        self.assertEqual(line(None), ['bot finish:         off (a rule-resolved PR waits for one approval)'])
        self.assertEqual(line('shadow'), ['bot finish:         shadow (log only: a rule-resolved PR waits for one approval)'])
        self.assertEqual(line('splice'), ['bot finish:         shadow (log only: a rule-resolved PR waits for one approval)'])
        self.assertEqual(line('splice,bot-finish'), ["bot finish:         acting (a rule-resolved PR is merged with no approval once its "
                                                     "required checks succeed on the bot's commit)"])
        self.assertEqual(line('bot-finish'), ['bot finish:         acting, but splice does not act, so no rule-resolved PR is made '
                                              'and nothing is finished'])

    def test_automation_cli_actions(self):
        r = self.cli('automation', env_value='splice,bot-finish')
        self.assertEqual((r.returncode, len(r.stdout.splitlines())), (0, 2))   # plain automation: the same two lines
        self.assertEqual(self.cli('automation', '--action', 'bot-finish', env_value='splice,bot-finish').stdout, 'act\n')
        self.assertEqual(self.cli('automation', '--action', 'bot-finish', env_value='splice').stdout, 'shadow\n')
        self.assertEqual(self.cli('automation', '--action', 'bot-finish').stdout, 'off\n')
        self.assertEqual(self.cli('automation', '--actions', '--value', 'shadow').stdout,
                         'bot finish:         shadow (log only: a rule-resolved PR waits for one approval)\n')
        # bot-finish is not a rule, and no rule is an action
        self.assertEqual(self.cli('automation', '--rule', 'bot-finish').returncode, 2)
        self.assertEqual(self.cli('automation', '--action', 'splice').returncode, 2)

    def test_automation_cli(self):
        r = self.cli('automation')
        self.assertEqual((r.returncode, r.stdout.splitlines()[1]),
                         (0, 'active rules:       none (automation: off, no rule is evaluated)'))
        r = self.cli('automation', env_value='splice')
        self.assertEqual(r.returncode, 0)
        self.assertIn('acting: splice; shadow (evaluate and log only): none', r.stdout)
        self.assertEqual(self.cli('automation', '--rule', 'splice', env_value='splice').stdout, 'act\n')
        self.assertEqual(self.cli('automation', '--rule', 'splice', env_value='shadow').stdout, 'shadow\n')
        self.assertEqual(self.cli('automation', '--rule', 'splice').stdout, 'off\n')
        # --value wins over the environment
        self.assertEqual(self.cli('automation', '--value', 'off', '--rule', 'splice', env_value='splice').stdout, 'off\n')
        r = self.cli('automation', env_value='splice,lockpin')
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        self.assertIn("'lockpin' is not a rule", r.stderr)

    def root_with(self, doc):
        tmp = tempfile.mkdtemp(prefix='merge-up-config-')
        self.addCleanup(shutil.rmtree, tmp, True)
        write(os.path.join(tmp, 'release/config.json'), json.dumps(doc))
        return tmp

    def test_merge_up_config_defaults_without_the_block(self):
        cfg = release_git.merge_up_config(self.root_with({'products': ['ds']}))
        self.assertEqual(cfg, version.MERGE_UP_DEFAULTS)
        self.assertIs(cfg['finish']['strict'], False)

    def test_merge_up_config_reads_the_block(self):
        root = self.root_with({'products': ['ds'], 'merge_up': {
            'finish': {'strict': True, 'required_checks': ['pr-checks'],
                       'check_sources': {'pr-checks': {'workflow': '.github/workflows/pr-checks.yml'}}}}})
        cfg = release_git.merge_up_config(root)
        self.assertEqual(cfg['finish'], {'strict': True, 'required_checks': ['pr-checks'],
                                         'check_sources': {'pr-checks': {'workflow': '.github/workflows/pr-checks.yml'}},
                                         'wait_minutes': 15})
        self.assertEqual(cfg['never_auto'], ['.github/workflows/**'])
        r = self.cli('merge-up-config', '--root', root, '--key', 'finish.strict')
        self.assertEqual((r.returncode, r.stdout), (0, 'true\n'))
        r = self.cli('merge-up-config', '--root', root, '--key', 'finish.required_checks')
        self.assertEqual((r.returncode, r.stdout), (0, '["pr-checks"]\n'))
        r = self.cli('merge-up-config', '--root', root, '--key', 'finish.nope')
        self.assertEqual(r.returncode, 1)
        self.assertIn("merge_up has no key 'finish.nope'", r.stderr)

    def test_merge_up_config_defaults_to_the_scripts_own_checkout(self):
        """Not the working directory: the config of the ref whose scripts run governs."""
        elsewhere = self.root_with({'products': ['ds'], 'merge_up': {'finish': {'strict': True}}})
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'merge-up-config'],
                           capture_output=True, text=True, cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), version.merge_up(os.path.dirname(SCRIPTS)))
        self.assertEqual(release_git.merge_up_config(), version.merge_up(os.path.dirname(SCRIPTS)))

    def test_merge_up_config_refuses_a_bad_block(self):
        root = self.root_with({'products': ['ds'], 'merge_up': {'finish': {'strict': 'true'}}})
        with self.assertRaises(release_git.Error) as cm:
            release_git.merge_up_config(root)
        self.assertIn('merge_up.finish.strict must be true or false', str(cm.exception))
        r = self.cli('merge-up-config', '--root', root)
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        with self.assertRaises(release_git.Error):
            release_git.merge_up_config(self.root_with({'products': ['ds'], 'merge-up': {}}))

    def test_never_auto_returns_the_first_matching_pattern(self):
        patterns = ['.github/workflows/**', '*sudoers*', 'scripts-secboot/**']
        self.assertEqual(release_git.never_auto(patterns, '.github/workflows/sub/x.yml'), '.github/workflows/**')
        self.assertEqual(release_git.never_auto(patterns, 'recipes/x/files/ds-sudoers'), '*sudoers*')
        self.assertEqual(release_git.never_auto(patterns, 'ds-sensor-portal.sudoers'), '*sudoers*')
        self.assertEqual(release_git.never_auto(patterns, 'scripts-secboot/a/b.sh'), 'scripts-secboot/**')
        self.assertIsNone(release_git.never_auto(patterns, 'handlers/system.go'))
        self.assertIsNone(release_git.never_auto(patterns, 'docs/.github/workflows/x.yml'))
        self.assertIsNone(release_git.never_auto([], '.github/workflows/x.yml'))
        with self.assertRaises(release_git.Error):
            release_git.never_auto(['a/**b'], 'a/b')

    def test_never_auto_matches_a_directory_name_and_ignores_case(self):
        # the review's three paths: none has `sudoers` in a lower-case file name
        patterns = ['.github/workflows/**', '*sudoers*', '*keys*.sh', 'kas/include/security/**']
        for path in ('etc/sudoers.d/ds-portal', 'packaging/sudoers/portal', 'debian/ds-portal.SUDOERS',
                     'sudoers.d/a/b/c', 'a/b/Sudoers', 'ds.sudoers'):
            self.assertEqual(release_git.never_auto(patterns, path), '*sudoers*', path)
        self.assertEqual(release_git.never_auto(patterns, 'scripts/Fetch_Keys_all.SH'), '*keys*.sh')
        self.assertEqual(release_git.never_auto(patterns, 'old_keys_v1.sh/readme'), '*keys*.sh')
        # a pattern with a slash is still the whole path from the root, in any case
        self.assertEqual(release_git.never_auto(patterns, '.GitHub/Workflows/x.yml'), '.github/workflows/**')
        self.assertEqual(release_git.never_auto(patterns, 'KAS/include/Security/a/b.yml'), 'kas/include/security/**')
        self.assertIsNone(release_git.never_auto(patterns, 'docs/.github/workflows/x.yml'))
        self.assertIsNone(release_git.never_auto(patterns, 'x/kas/include/security/a.yml'))
        # a segment must match whole, and * does not cross a slash
        self.assertIsNone(release_git.never_auto(['sudoers'], 'etc/sudoers.d/x'))
        self.assertIsNone(release_git.never_auto(['*keys*.sh'], 'keys/run.sh'))
        self.assertIsNone(release_git.never_auto(patterns, 'etc/sudo/ers'))


def tracked_files(tree):
    """git ls-files of a checkout, or None when tree is not one."""
    r = subprocess.run(['git', '-C', tree, 'ls-files', '-z'], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split('\0') if p]


class NeverAutoTreeTests(unittest.TestCase):
    """Every never_auto pattern of this checkout's release/config.json matches a
    tracked file, so a path that is renamed away, or a pattern that is
    mistyped, fails here instead of silently protecting nothing.

    UNMATCHED names, per product of the config (this file is the same in
    every repo that carries the tooling), the patterns that are kept although
    nothing matches them today: the ds repo tracks no sudoers file, and keeps
    the pattern for the day one is added. The test fails both ways: a listed
    pattern that starts matching must leave the set, a pattern that stops
    matching must be fixed or entered here on purpose."""

    UNMATCHED = {'ds': {'*sudoers*'}}

    def test_patterns_match_tracked_files(self):
        root = os.path.dirname(SCRIPTS)
        files = tracked_files(root)
        if files is None:
            self.skipTest(f'{root} is not a git checkout')
        self.assertGreater(len(files), 50)
        patterns = release_git.merge_up_config(root)['never_auto']
        unmatched = {p for p in patterns if not any(release_git.never_auto([p], f) for f in files)}
        product = version.config(root)['products'][0]
        self.assertEqual(unmatched, self.UNMATCHED.get(product, set()),
                         f'never_auto patterns with no tracked file among {len(files)} in {root}')


if __name__ == '__main__':
    unittest.main()
