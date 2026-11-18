"""Tests for release_git.py, create-branch.sh and merge-up.sh (ENG-1933, ENG-1935).

Two bare repos in a temp dir stand in for ds-tegra-distro and meta-dsensos
("origin"); the scripts run from a clone of the distro bare repo with
META_DSENSOS_URL pointing at the meta bare repo. A stub `gh` on PATH answers
`run list` from a list of green shas, records `pr create`, serves `pr view`
from a per-PR review file and performs `pr merge` in the bare repo. No
network, nothing touches this checkout.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import version  # noqa: E402

CREATE = os.path.join(SCRIPTS, 'create-branch.sh')
MERGE_UP = os.path.join(SCRIPTS, 'merge-up.sh')
DISTRO_REPO = 'ds/distro'
META_REPO = 'ds/meta'

BASE_YML = textwrap.dedent('''\
    # test base.yml
    header:
      version: 23
    repos:
      ds-tegra-distro:
        path: .
      poky:
        url: https://git.yoctoproject.org/poky
        branch: scarthgap
        path: repos/poky
      meta-dsensos:
        url: git@github.com:DistributedSpectrum/meta-dsensos.git
        branch: ota-dev   # tracks the trunk
        path: repos/meta-dsensos
    local_conf_header:
      00-sanity: |
        CONF_VERSION = "2"
    ''')

LOCK_YML = textwrap.dedent('''\
    header:
      version: 14
    overrides:
      repos:
        poky:
          commit: 7117d115eab7351ecf21388ec720a3bb5f4a9b30
        meta-dsensos:
          commit: {pin}
    ''')

GH_STUB = r'''#!/usr/bin/env python3
"""Stub gh for the merge-up and create-branch tests."""
import json, os, re, subprocess, sys, tempfile
STATE = os.environ['GH_STUB_STATE']
REPOS = dict(kv.split('=', 1) for kv in os.environ['GH_STUB_REPOS'].split(','))
PRS = os.path.join(STATE, 'prs.json')
FLAGS = {'--merge', '--squash', '--rebase', '--draft', '--prerelease', '--delete-branch', '--paginate'}
BOT = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}

def parse(argv):
    pos, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in FLAGS:
            opts[a] = True; i += 1
        elif a.startswith('-'):
            opts[a] = argv[i + 1]; i += 2
        else:
            pos.append(a); i += 1
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
           'commits': [{'authors': [{'login': 'resolver'}]}]}
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

def main(argv):
    with open(os.path.join(STATE, 'log'), 'a') as f:
        f.write(' '.join(argv) + '\n')
    pos, opts = parse(argv)
    repo = opts.get('-R') or opts.get('--repo')
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
        if opts.get('--jq') == '.[0].url':
            print(hits[0]['url'] if hits else '')
        elif opts.get('--jq') == '.[] | "\\(.number) \\(.headRefName)"':
            for p in hits:
                print('%d %s' % (p['number'], p['head']))
        elif opts.get('--jq') == '.[] | "\\(.number) \\(.headRefName) \\(.headRefOid)"':
            for p in hits:
                print('%d %s %s' % (p['number'], p['head'], rev(p['repo'], 'refs/heads/' + p['head'])))
        else:
            print(json.dumps([view_doc(p) for p in hits]))
        return 0
    if pos[:2] == ['pr', 'view']:
        n = int(pos[2])
        hook = os.path.join(STATE, 'pr-view-hook')
        if os.path.exists(hook):
            # something that happens between two reads of a PR (a push, a moved base): run once
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
        pr.setdefault('comments', []).append(opts.get('--body', ''))
        save(prs)
        return 0
    if pos[:2] == ['pr', 'merge']:
        n = int(pos[2])
        prs = load()
        pr = [p for p in prs if p['repo'] == repo and p['number'] == n][0]
        if os.path.exists(os.path.join(STATE, 'fail-pr-merge')):
            print('GraphQL: Repository rule violations found (mergePullRequest)', file=sys.stderr)
            return 1
        if pr['state'] != 'OPEN':
            print('PR not open', file=sys.stderr)
            return 1
        head = rev(repo, 'refs/heads/' + pr['head'])
        if opts.get('--match-head-commit') and opts['--match-head-commit'] != head:
            print('Head branch was modified. Review and try the merge again.', file=sys.stderr)
            return 1
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, GIT_AUTHOR_NAME='gh', GIT_AUTHOR_EMAIL='gh@x', GIT_COMMITTER_NAME='gh', GIT_COMMITTER_EMAIL='gh@x')
        subprocess.check_call(['git', 'clone', '-q', REPOS[repo], tmp], env=env)
        subprocess.check_call(['git', '-C', tmp, 'checkout', '-q', pr['base']], env=env)
        subprocess.check_call(['git', '-C', tmp, 'merge', '-q', '--no-ff', '-m', 'Merge pull request #%d' % n, head], env=env)
        subprocess.check_call(['git', '-C', tmp, 'push', '-q', 'origin', pr['base']], env=env)
        # leave-open-after-merge: the base holds the merge, and GitHub still lists the PR as open
        if not os.path.exists(os.path.join(STATE, 'leave-open-after-merge')):
            pr['state'] = 'MERGED'; pr['merged_head'] = head
        save(prs)
        print('merged')
        return 0
    if pos[:1] == ['api'] and opts.get('-X') == 'POST' and pos[1].startswith('repos/') and '/commits/' in pos[1] and pos[1].endswith('/comments'):
        # a commit comment: repos/<owner>/<name>/commits/<sha>/comments -f body=...
        if os.path.exists(os.path.join(STATE, 'fail-commit-comment')):
            print('HTTP 403: Resource not accessible by integration', file=sys.stderr)
            return 1
        repo, sha = pos[1][len('repos/'):].split('/commits/')
        sha = rev(repo, sha[:-len('/comments')])
        path = os.path.join(STATE, 'commit-comments.json')
        comments = json.load(open(path)) if os.path.exists(path) else []
        comments.append({'repo': repo, 'sha': sha, 'body': opts['-f'].split('=', 1)[1]})
        json.dump(comments, open(path, 'w'))
        print('{}')
        return 0
    m = re.fullmatch(r'repos/(.+)/issues/(\d+)(/comments)?', pos[1]) if pos[:1] == ['api'] and len(pos) > 1 else None
    if m and '-X' not in opts:
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
    m = re.fullmatch(r'repos/(.+)/pulls/(\d+)/reviews', pos[1]) if pos[:1] == ['api'] and len(pos) > 1 else None
    if m and '-X' not in opts:
        # the REST reviews of a PR. pr-N-rest-reviews.json is the answer as it is; otherwise the
        # reviews of the pr view answer in REST shape: a person, on the PR's current head
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
            return 0
        doc = view_doc(match[0], consume=False)
        print(json.dumps([{'user': {'login': (rv.get('author') or {}).get('login'), 'type': 'User'}, 'state': rv.get('state'),
                           'commit_id': doc['headRefOid']} for rv in doc.get('reviews') or []]))
        return 0
    if pos[:1] == ['api'] and len(pos) > 1 and re.fullmatch(r'repos/.+/activity', pos[1]) and opts.get('-X') == 'GET':
        # the repository activity of a branch: activity.json as it is; unreadable without it
        path = os.path.join(STATE, 'activity.json')
        if not os.path.exists(path):
            print('HTTP 403: Resource not accessible by integration', file=sys.stderr)
            return 1
        print(open(path).read())
        return 0
    m = re.fullmatch(r'repos/(.+)/commits/([0-9a-f]{40})', pos[1]) if pos[:1] == ['api'] and len(pos) > 1 else None
    if m and '-X' not in opts:
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
    m = re.fullmatch(r'repos/(.+)/compare/(.+)', pos[1]) if pos[:1] == ['api'] and len(pos) > 1 else None
    if m and '-X' not in opts:
        # the compare API, for the pins rule. compare.json maps "<owner>/<repo> <base>...<head>" to a
        # status, or to "404" or "502"; a pair it does not name is a 404. The token in use is recorded.
        with open(os.path.join(STATE, 'compare-tokens'), 'a') as f:
            f.write(os.environ.get('GH_TOKEN', '') + '\n')
        path = os.path.join(STATE, 'compare.json')
        answer = (json.load(open(path)) if os.path.exists(path) else {}).get(m.group(1) + ' ' + m.group(2), '404')
        if answer == '404':
            print('gh: Not Found (HTTP 404)', file=sys.stderr)
            return 1
        if answer == '502':
            print('gh: Bad Gateway (HTTP 502)', file=sys.stderr)
            return 1
        print(answer)
        return 0
    print('stub gh: unsupported: ' + ' '.join(argv), file=sys.stderr)
    return 1

sys.exit(main(sys.argv[1:]))
'''


def git(cwd, *args, **kw):
    env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x')
    env.update(kw.pop('env', {}))
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True, env=env, **kw).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


def dead_error(what, branch, why, url=None, child='vertex/1.x.x', parent='vertex/main'):
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
    if what == 'meta-dsensos':
        # the layer branch is named after the layer's child tip, not after the distro green commit
        return (f'::error::merge-up: {what} {child} -> {parent}: {finding}. This run opens no pull request on {branch}, pushes nothing to it, '
                f'merges nothing from it and applies no merge-up rule to it. Two ways forward: the next commit on meta-dsensos {child} gets a '
                f'new branch and a new PR by itself, at the first merge-up run of {child} after it (the branch is named after the tip of '
                f'meta-dsensos {child}, so a new green commit on ds-tegra-distro {child} that leaves that tip where it is stops here again); {ways}')
    return (f'::error::merge-up: {what} {child} -> {parent}: {finding}. This run opens no pull request on {branch}, pushes nothing to it, '
            f'merges nothing from it and applies no merge-up rule to it. Two ways forward: the next green commit on {child} gets a new '
            f'branch and a new PR by itself; {ways}')


def commit_all(cwd, msg):
    git(cwd, 'add', '-A')
    git(cwd, 'commit', '-q', '-m', msg)
    return git(cwd, 'rev-parse', 'HEAD')


class Fixture:
    """Bare origins for both repos, a working clone of each, the stub gh and the script environment."""

    def __init__(self):
        self.top = tempfile.mkdtemp(prefix='release-git-')
        self.distro_bare = os.path.join(self.top, 'distro.git')
        self.meta_bare = os.path.join(self.top, 'meta.git')
        self.meta_work = os.path.join(self.top, 'meta-work')
        self.distro = os.path.join(self.top, 'distro')  # the checkout the scripts run in
        self.state = os.path.join(self.top, 'gh-state')
        self.binp = os.path.join(self.top, 'bin')
        os.makedirs(self.state)
        os.makedirs(self.binp)
        write(os.path.join(self.binp, 'gh'), GH_STUB)
        os.chmod(os.path.join(self.binp, 'gh'), 0o755)
        self.green = os.path.join(self.state, 'green')
        write(self.green, '')
        # meta-dsensos: ota-dev with two commits
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'ota-dev', self.meta_bare], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'ota-dev', self.meta_work], check=True)
        write(os.path.join(self.meta_work, 'layer.txt'), 'm1\n')
        commit_all(self.meta_work, 'm1')
        write(os.path.join(self.meta_work, 'layer.txt'), 'm2\n')
        self.m2 = commit_all(self.meta_work, 'm2')
        git(self.meta_work, 'remote', 'add', 'origin', self.meta_bare)
        git(self.meta_work, 'push', '-q', 'origin', 'ota-dev')
        # ds-tegra-distro: ota-dev pinning m2
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'ota-dev', self.distro_bare], check=True)
        seed = os.path.join(self.top, 'distro-seed')
        subprocess.run(['git', 'init', '-q', '-b', 'ota-dev', seed], check=True)
        write(os.path.join(seed, 'kas/include/base.yml'), BASE_YML)
        write(os.path.join(seed, 'kas/include/base.lock.yml'), LOCK_YML.format(pin=self.m2))
        write(os.path.join(seed, 'release/config.json'), json.dumps({'products': ['vertex', 'aggregator']}))
        write(os.path.join(seed, 'versions/vertex'), '0.0.1\n')
        write(os.path.join(seed, 'versions/aggregator'), '0.0.1\n')
        write(os.path.join(seed, 'versions/README.md'), '# versions\n')
        write(os.path.join(seed, 'foo.txt'), 'trunk\n')
        self.seed_sha = commit_all(seed, 'seed')
        git(seed, 'remote', 'add', 'origin', self.distro_bare)
        git(seed, 'push', '-q', 'origin', 'ota-dev')
        subprocess.run(['git', 'clone', '-q', self.distro_bare, self.distro], check=True)

    def close(self):
        shutil.rmtree(self.top, ignore_errors=True)

    def env(self, token_kind='app', dry_run='false'):
        # the merge-up switches of the caller's shell or CI job never reach a test
        inherited = {k: v for k, v in os.environ.items()
                     if k not in ('DS_MERGE_UP_AUTOMATION', 'MERGE_UP_SUMMARY', 'MERGE_UP_READ_TOKEN', 'DEFAULT_BRANCH')}
        return dict(inherited, PATH=self.binp + os.pathsep + os.environ['PATH'],
                    TOKEN='stub-token', TOKEN_KIND=token_kind, DRY_RUN=dry_run,
                    META_DSENSOS_URL=self.meta_bare, DISTRO_REPO=DISTRO_REPO, META_REPO=META_REPO,
                    GH_STUB_STATE=self.state, GH_STUB_GREEN=self.green,
                    GH_STUB_REPOS=f'{DISTRO_REPO}={self.distro_bare},{META_REPO}={self.meta_bare}',
                    GH_TOKEN='stub-token')

    def run(self, script, *args, token_kind='app', dry_run='false', extra_env=None):
        env = self.env(token_kind, dry_run)
        env.update(extra_env or {})
        return subprocess.run([script, *args], cwd=self.distro, capture_output=True, text=True, env=env)

    def scripts_copy(self, merge_up=None):
        """A copy of scripts-release/ and release/ inside the fixture, for a run under another
        release/config.json: merge_up maps "section.key" to a value in the merge_up block
        (the scripts read the config of their own checkout). Returns the copy's scripts-release/."""
        root = os.path.join(self.top, f'scripts-{len(os.listdir(self.top))}')
        shutil.copytree(SCRIPTS, os.path.join(root, 'scripts-release'),
                        ignore=shutil.ignore_patterns('tests', '__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(SCRIPTS), 'release'), os.path.join(root, 'release'))
        if merge_up:
            path = os.path.join(root, 'release', 'config.json')
            with open(path) as f:
                doc = json.load(f)
            for key, value in merge_up.items():
                section, name = key.split('.')
                doc['merge_up'][section][name] = value
            with open(path, 'w') as f:
                json.dump(doc, f, indent=2)
        return os.path.join(root, 'scripts-release')

    def gh_log(self):
        p = os.path.join(self.state, 'log')
        if not os.path.exists(p):
            return ''
        with open(p) as f:
            return f.read()

    # --- queries against the bare repos ---
    def heads(self, bare):
        out = git(bare, 'for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads/')
        return dict(line.split() for line in out.splitlines())

    def show(self, bare, ref, path):
        return git(bare, 'show', f'{ref}:{path}')

    def files_in_commit(self, bare, sha):
        return sorted(git(bare, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines())

    def subject(self, bare, sha):
        return git(bare, 'log', '-1', '--format=%s', sha)

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
        pr = prs[number - 1]
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
        prs[number - 1].update(state='OPEN', closed_by=None)
        write(os.path.join(self.state, 'prs.json'), json.dumps(prs))

    # --- fixture steps ---
    def seed_vertex(self):
        r = self.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev')
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def tag(self, name, ref):
        git(self.distro, 'fetch', '-q', 'origin')
        git(self.distro, 'tag', name, ref)
        git(self.distro, 'push', '-q', 'origin', name)

    def open_line(self, version):
        r = self.run(CREATE, '--product', 'vertex', '--version', version)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def meta_commit(self, branch, content, msg, path='layer.txt'):
        """A commit on a meta-dsensos branch, pushed; returns the sha."""
        git(self.meta_work, 'fetch', '-q', 'origin')
        git(self.meta_work, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.meta_work, path), content)
        sha = commit_all(self.meta_work, msg)
        git(self.meta_work, 'push', '-q', 'origin', branch)
        return sha

    def distro_commit(self, branch, files, msg):
        """A commit on a ds-tegra-distro branch (files: path -> content), pushed; returns the sha."""
        git(self.distro, 'fetch', '-q', 'origin')
        git(self.distro, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        for path, content in files.items():
            write(os.path.join(self.distro, path), content)
        sha = commit_all(self.distro, msg)
        git(self.distro, 'push', '-q', 'origin', branch)
        return sha


class UnitTests(unittest.TestCase):
    def test_lock_pin(self):
        self.assertEqual(release_git.lock_pin(LOCK_YML.format(pin='a' * 40)), 'a' * 40)
        with self.assertRaises(release_git.Error):
            release_git.lock_pin(LOCK_YML.format(pin='notasha'))
        with self.assertRaises(release_git.Error):
            release_git.lock_pin('header:\n  version: 14\noverrides:\n  repos:\n    poky:\n      commit: ' + 'b' * 40 + '\n')

    def test_base_branch_get_and_edit(self):
        self.assertEqual(release_git.base_branch_get(BASE_YML), 'ota-dev')
        new = release_git.base_branch_edit(BASE_YML, 'ota-dev', 'vertex/main')
        changed = [(a, b) for a, b in zip(BASE_YML.splitlines(), new.splitlines()) if a != b]
        self.assertEqual(changed, [('    branch: ota-dev   # tracks the trunk', '    branch: vertex/main')])
        self.assertEqual(release_git.base_branch_get(new), 'vertex/main')
        # poky's branch line is untouched and never a match
        self.assertIn('branch: scarthgap', new)
        with self.assertRaises(release_git.Error):
            release_git.base_branch_edit(BASE_YML, 'scarthgap', 'x')
        with self.assertRaises(release_git.Error):
            release_git.base_branch_edit(BASE_YML + BASE_YML, 'ota-dev', 'x')  # two stanzas
        with self.assertRaises(release_git.Error):
            release_git.base_branch_get(BASE_YML.replace('    path: repos/meta-dsensos\n', '    path: repos/meta-dsensos\n    branch: other\n'))
        # base_branch_set: whatever the value was
        self.assertEqual(release_git.base_branch_get(release_git.base_branch_set(BASE_YML, 'vertex/1.0.x')), 'vertex/1.0.x')

    def test_line_plan(self):
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

    def test_promote_plan(self):
        products = ['vertex', 'aggregator']
        plan = release_git.promote_plan
        self.assertEqual(plan('vertex/main', '0.0.1', products),
                         {'product': 'vertex', 'line': 'vertex/main', 'kind': 'main', 'version': '0.0.1',
                          'tag': 'vertex/v0.0.1', 'level': 'major', 'next_version': '1.0.0'})
        self.assertEqual(plan('refs/heads/vertex/main', '0.0.1', products, 'patch')['next_version'], '0.0.2')
        self.assertEqual(plan('vertex/main', '1.2.3', products, 'minor')['next_version'], '1.3.0')
        self.assertEqual(plan('vertex/1.x.x', '1.2.0', products)['level'], 'minor')
        self.assertEqual(plan('vertex/1.x.x', '1.2.0', products)['next_version'], '1.3.0')
        self.assertEqual(plan('vertex/1.2.x', '1.2.3', products), {'product': 'vertex', 'line': 'vertex/1.2.x', 'kind': 'patch',
                         'version': '1.2.3', 'tag': 'vertex/v1.2.3', 'level': 'patch', 'next_version': '1.2.4'})
        self.assertEqual(plan('aggregator/0.0.x', '0.0.2', products)['next_version'], '0.0.3')
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
                plan(line, pending, products, bump)

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

    def test_first_run(self):
        self.assertEqual(release_git.first_run([{'databaseId': 7, 'number': 3}]), '7 3')
        self.assertIsNone(release_git.first_run([]))

    def test_versions_only(self):
        d = tempfile.mkdtemp()
        try:
            subprocess.run(['git', 'init', '-q', '-b', 'main', d], check=True)
            write(os.path.join(d, 'versions/vertex'), '1.0.0\n')
            write(os.path.join(d, 'a.txt'), 'a\n')
            base = commit_all(d, 'base')
            write(os.path.join(d, 'versions/vertex'), '1.1.0\n')
            v1 = commit_all(d, 'bump')
            write(os.path.join(d, 'versions/vertex'), '1.2.0\n')
            v2 = commit_all(d, 'bump again')
            write(os.path.join(d, 'a.txt'), 'b\n')
            write(os.path.join(d, 'versions/vertex'), '1.3.0\n')
            mixed = commit_all(d, 'mixed')
            self.assertFalse(release_git.is_versions_only(d, base))
            self.assertTrue(release_git.is_versions_only(d, v1))
            self.assertFalse(release_git.is_versions_only(d, mixed))
            self.assertEqual(release_git.walk_versions_only(d, v2), [v2, v1, base])
            self.assertEqual(release_git.walk_versions_only(d, mixed), [mixed])
            # a merge commit is never versions-only
            git(d, 'checkout', '-q', '-b', 'side', base)
            write(os.path.join(d, 'versions/vertex'), '9.0.0\n')
            commit_all(d, 'side bump')
            git(d, 'checkout', '-q', 'main')
            git(d, 'merge', '-q', '--no-ff', '-m', 'merge', 'side', '-X', 'ours')
            self.assertFalse(release_git.is_versions_only(d, 'HEAD'))
            merge = git(d, 'rev-parse', 'HEAD')
            # bookkeeping: versions/ and kas/include/base.yml only (a line's opening commit); unmerged
            # lists everything else in base..head, oldest first, merges included
            git(d, 'checkout', '-q', '-b', 'line', base)
            write(os.path.join(d, 'kas/include/base.yml'), 'branch: line\n')
            write(os.path.join(d, 'versions/vertex'), '1.0.1\n')
            opening = commit_all(d, 'release: open line [skip ci]')
            self.assertTrue(release_git.is_bookkeeping(d, opening))
            self.assertFalse(release_git.is_versions_only(d, opening))
            self.assertTrue(release_git.is_bookkeeping(d, v1))
            self.assertFalse(release_git.is_bookkeeping(d, mixed))
            self.assertFalse(release_git.is_bookkeeping(d, merge))
            self.assertEqual(release_git.unmerged(d, 'main', 'line'), [])
            write(os.path.join(d, 'a.txt'), 'line fix\n')
            fix = commit_all(d, 'line fix')
            write(os.path.join(d, 'versions/vertex'), '1.0.2\n')
            commit_all(d, 'release: pending 1.0.2 [skip ci]')
            self.assertEqual(release_git.unmerged(d, 'main', 'line'), [(fix, 'line fix')])
            self.assertEqual([s for s, _ in release_git.unmerged(d, 'line', 'main')], [mixed, merge])  # v1, v2 are versions-only
        finally:
            shutil.rmtree(d, ignore_errors=True)


class CreateBranchTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_seed_dry_run_creates_nothing(self):
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing created', r.stdout)
        self.assertIn('-    branch: ota-dev', r.stdout)
        self.assertIn('+    branch: vertex/main', r.stdout)
        self.assertEqual(set(self.fx.heads(self.fx.distro_bare)), {'ota-dev'})
        self.assertEqual(set(self.fx.heads(self.fx.meta_bare)), {'ota-dev'})

    def test_seed_mode(self):
        r = self.fx.seed_vertex()
        meta = self.fx.heads(self.fx.meta_bare)
        distro = self.fx.heads(self.fx.distro_bare)
        self.assertEqual(meta['vertex/main'], self.fx.m2)
        tip = distro['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^'), self.fx.seed_sha)
        self.assertEqual(self.fx.files_in_commit(self.fx.distro_bare, tip), ['kas/include/base.yml'])
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), f'release: open vertex/main from {self.fx.seed_sha[:7]} [skip ci]')
        base = self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')
        self.assertEqual(release_git.base_branch_get(base), 'vertex/main')
        self.assertIn('branch: scarthgap', base)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.0.1')
        self.assertIn('meta-dsensos: created vertex/main', r.stdout)

    def test_seed_refusals(self):
        r = self.fx.run(CREATE, '--product', 'nope', '--seed-from', 'ota-dev')
        self.assertEqual(r.returncode, 1)
        self.assertIn("product 'nope' is not in release/config.json", r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'no-such-ref')
        self.assertEqual(r.returncode, 1)
        self.assertIn('does not resolve', r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev', '--version', '1.0.0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('mutually exclusive', r.stdout)
        # the line exists in meta-dsensos only
        git(self.fx.meta_work, 'push', '-q', 'origin', 'ota-dev:refs/heads/vertex/main')
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev')
        self.assertEqual(r.returncode, 1)
        self.assertIn('meta-dsensos already has branch vertex/main', r.stdout)
        self.assertNotIn('vertex/main', self.fx.heads(self.fx.distro_bare))
        git(self.fx.meta_work, 'push', '-q', 'origin', ':refs/heads/vertex/main')
        # and in ds-tegra-distro
        self.fx.seed_vertex()
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ds-tegra-distro already has branch vertex/main', r.stdout)

    def test_no_app_token(self):
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev', token_kind='github', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('checked=false', r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--seed-from', 'ota-dev', token_kind='github')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::create-branch: no App token', r.stdout)
        self.assertIn(f'git push origin {self.fx.m2}:refs/heads/vertex/main', r.stdout)
        self.assertIn('base-branch-edit kas/include/base.yml ota-dev vertex/main', r.stdout)
        self.assertEqual(set(self.fx.heads(self.fx.distro_bare)), {'ota-dev'})
        self.assertEqual(set(self.fx.heads(self.fx.meta_bare)), {'ota-dev'})

    def test_line_mode(self):
        self.fx.seed_vertex()
        main_tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.fx.tag('vertex/v1.0.0', main_tip)
        r = self.fx.open_line('1.0.0')
        self.assertIn('parent line:        vertex/main', r.stdout)
        distro = self.fx.heads(self.fx.distro_bare)
        meta = self.fx.heads(self.fx.meta_bare)
        self.assertEqual(meta['vertex/1.x.x'], self.fx.m2)
        tip = distro['vertex/1.x.x']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^'), main_tip)
        self.assertEqual(self.fx.files_in_commit(self.fx.distro_bare, tip), ['kas/include/base.yml', 'versions/vertex'])
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), 'release: open vertex/1.x.x from vertex/v1.0.0 [skip ci]')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '1.1.0')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/aggregator'), '0.0.1')
        self.assertEqual(release_git.base_branch_get(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')), 'vertex/1.x.x')
        # a patch line from a tag on the minor line, with a different pin
        m3 = self.fx.meta_commit('vertex/1.x.x', 'm3\n', 'm3')
        line_tip = self.fx.distro_commit('vertex/1.x.x', {'kas/include/base.lock.yml': LOCK_YML.format(pin=m3)}, 'pin m3')
        self.fx.tag('vertex/v1.1.0', line_tip)
        r = self.fx.open_line('1.1.0')
        self.assertIn('parent line:        vertex/1.x.x', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)['vertex/1.1.x'], m3)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/1.1.x']
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '1.1.1')
        self.assertEqual(release_git.base_branch_get(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')), 'vertex/1.1.x')
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), 'release: open vertex/1.1.x from vertex/v1.1.0 [skip ci]')

    def test_line_mode_rolls_back_meta_when_the_distro_push_fails(self):
        self.fx.seed_vertex()
        main_tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.fx.tag('vertex/v1.0.0', main_tip)
        # ds-tegra-distro refuses the new line (as GitHub refused an App token without
        # workflows: write on 2026-10-26); meta-dsensos was created first and must go again
        hook = os.path.join(self.fx.distro_bare, 'hooks', 'pre-receive')
        write(hook, '#!/bin/sh\nwhile read old new ref; do [ "$ref" = refs/heads/vertex/1.x.x ] && { echo refused >&2; exit 1; }; done\nexit 0\n')
        os.chmod(hook, 0o755)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '1.0.0', '--no-dry-run')
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('deleting meta-dsensos vertex/1.x.x again', r.stdout)
        self.assertNotIn('vertex/1.x.x', self.fx.heads(self.fx.meta_bare))
        self.assertNotIn('vertex/1.x.x', self.fx.heads(self.fx.distro_bare))
        os.remove(hook)
        # nothing half-made: a re-run succeeds
        r = self.fx.open_line('1.0.0')
        self.assertIn('vertex/1.x.x', self.fx.heads(self.fx.meta_bare))
        self.assertIn('vertex/1.x.x', self.fx.heads(self.fx.distro_bare))

    def test_line_mode_from_any_tag(self):
        """ENG-1934: the pilot promotes 0.0.1 on main with bump=patch and cuts both lines from it."""
        self.fx.seed_vertex()
        main_tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.fx.tag('vertex/v0.0.1', main_tip)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '0.0.1', '--kind', 'minor')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('kind:               minor line (--kind minor)', r.stdout)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/0.x.x']
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.1.0')
        self.assertEqual(self.fx.heads(self.fx.meta_bare)['vertex/0.x.x'], self.fx.m2)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '0.0.1')  # auto = patch for P > 0
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('kind:               patch line (--kind auto)', r.stdout)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/0.0.x']
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.0.2')
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), 'release: open vertex/0.0.x from vertex/v0.0.1 [skip ci]')
        self.assertEqual(release_git.base_branch_get(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')), 'vertex/0.0.x')
        # the derived line existing is still refused
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '0.0.1', '--kind', 'patch')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ds-tegra-distro already has branch vertex/0.0.x', r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '0.0.1', '--kind', 'major')
        self.assertEqual(r.returncode, 1)
        self.assertIn('--kind must be auto, minor or patch', r.stdout)

    def test_line_refusals(self):
        self.fx.seed_vertex()
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '1.2')
        self.assertEqual(r.returncode, 1)
        self.assertIn('must be N.M.P', r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '1.2.3')
        self.assertEqual(r.returncode, 1)
        self.assertIn('tag vertex/v1.2.3 does not exist', r.stdout)
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '1.0.0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('tag vertex/v1.0.0 does not exist', r.stdout)
        self.fx.tag('vertex/v1.0.0', self.fx.heads(self.fx.distro_bare)['vertex/main'])
        self.fx.open_line('1.0.0')
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '1.0.0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('ds-tegra-distro already has branch vertex/1.x.x', r.stdout)
        # the pin must exist in meta-dsensos
        self.fx.distro_commit('vertex/main', {'kas/include/base.lock.yml': LOCK_YML.format(pin='c' * 40)}, 'bad pin')
        self.fx.tag('vertex/v2.0.0', self.fx.heads(self.fx.distro_bare)['vertex/main'])
        r = self.fx.run(CREATE, '--product', 'vertex', '--version', '2.0.0')
        self.assertEqual(r.returncode, 1)
        self.assertIn('meta-dsensos has no commit', r.stdout)
        self.assertNotIn('vertex/2.x.x', self.fx.heads(self.fx.distro_bare))
        self.assertNotIn('vertex/2.x.x', self.fx.heads(self.fx.meta_bare))


class MergeUpTests(unittest.TestCase):
    """vertex/1.x.x (child) into vertex/main (parent), both seeded by create-branch."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.fx.seed_vertex()
        self.main_tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.fx.tag('vertex/v1.0.0', self.main_tip)
        self.fx.open_line('1.0.0')
        self.line_open = self.fx.heads(self.fx.distro_bare)['vertex/1.x.x']

    def child_work(self, trailing_versions=True):
        """A layer fix on meta-dsensos vertex/1.x.x, its pin bump plus a foo.txt change on the child,
        then (optionally) a versions-only commit. Marks the pre-trailing sha green."""
        self.m3 = self.fx.meta_commit('vertex/1.x.x', 'm3 fix\n', 'layer fix')
        self.green_sha = self.fx.distro_commit('vertex/1.x.x', {
            'kas/include/base.lock.yml': LOCK_YML.format(pin=self.m3),
            'foo.txt': 'child fix\n',
        }, 'pin m3 and fix foo')
        write(self.fx.green, self.green_sha + '\n')
        if trailing_versions:
            self.child_tip = self.fx.distro_commit('vertex/1.x.x', {'versions/vertex': '1.2.0\n'}, 'release: pending 1.2.0 [skip ci]')
        else:
            self.child_tip = self.green_sha
        return self.child_tip

    def test_merge_up(self):
        child_tip = self.child_work()
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 56 (id 1234)', r.stdout)
        self.assertIn('only touches versions/', r.stdout)
        # meta-dsensos: vertex/main now carries the layer fix through a merge commit
        meta_tip = self.fx.heads(self.fx.meta_bare)['vertex/main']
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', meta_tip + '^2'), self.m3)
        self.assertEqual(self.fx.subject(self.fx.meta_bare, meta_tip), 'Merge vertex/1.x.x into vertex/main')
        # ds-tegra-distro: two-parent commit, target's versions/ and base.yml, child's lockfile and foo.txt
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^1'), self.main_tip)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), 'Merge vertex/1.x.x into vertex/main')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.0.1')
        self.assertEqual(release_git.base_branch_get(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')), 'vertex/main')
        self.assertEqual(release_git.lock_pin(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.lock.yml')), self.m3)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'foo.txt'), 'child fix')
        self.assertIn('post-check ok', r.stdout)
        self.assertEqual(self.fx.prs(), [])
        # the child line itself is untouched
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/1.x.x'], child_tip)
        # a second run has nothing to do
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('already contains the pin', r.stdout)
        self.assertIn('nothing to merge', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], tip)

    def test_not_green(self):
        self.child_work()
        write(self.fx.green, '')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on vertex/1.x.x', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], self.main_tip)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)['vertex/main'], self.fx.m2)

    def test_called_from_the_rc_build_run(self):
        """workflow_run after the child tip's own green rc-build run: CALLER_SHA is the tip and
        CALLER_RUN that run, so the green check takes the caller's word and never asks gh
        (the lookup would find nothing: the green list is empty)."""
        child_tip = self.child_work(trailing_versions=False)
        write(self.fx.green, '')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 78 (id 4321)', r.stdout)
        self.assertIn('the calling run', r.stdout)
        self.assertNotIn('run list', self.fx.gh_log())
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', self.fx.heads(self.fx.meta_bare)['vertex/main'] + '^2'), self.m3)

    def test_caller_sha_without_a_run_or_off_the_tip_still_looks_up(self):
        child_tip = self.child_work(trailing_versions=False)
        write(self.fx.green, '')
        # a dispatch (CALLER_RUN empty) does the lookup even with CALLER_SHA set
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': ''})
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on vertex/1.x.x', r.stdout)
        self.assertIn('run list', self.fx.gh_log())
        # the child moved since the caller's sha: the lookup runs for the new tip
        later = self.fx.distro_commit('vertex/1.x.x', {'foo.txt': 'later\n'}, 'later fix')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 1)
        self.assertIn('no successful release-rc-build.yml run on vertex/1.x.x', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], self.main_tip)
        # and finds it once that tip is green
        write(self.fx.green, later + '\n')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', extra_env={'CALLER_SHA': child_tip, 'CALLER_RUN': '4321 78'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('run 56 (id 1234)', r.stdout)
        self.assertNotIn('the calling run', r.stdout)

    def test_parent_without_versions_dir(self):
        self.child_work()
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', 'vertex/main', 'origin/vertex/main')
        git(self.fx.distro, 'rm', '-q', '-r', 'versions')
        git(self.fx.distro, 'commit', '-q', '-m', 'drop versions')
        git(self.fx.distro, 'push', '-q', 'origin', 'vertex/main')
        before = (self.fx.heads(self.fx.distro_bare), self.fx.heads(self.fx.meta_bare))
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: vertex/main has no versions/ directory', r.stdout)
        self.assertNotIn('pathspec', r.stderr)
        self.assertEqual((self.fx.heads(self.fx.distro_bare), self.fx.heads(self.fx.meta_bare)), before)

    def test_dry_run_and_no_app(self):
        self.child_work()
        before = (self.fx.heads(self.fx.distro_bare), self.fx.heads(self.fx.meta_bare))
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: nothing pushed', r.stdout)
        self.assertIn('meta-dsensos:       merge vertex/1.x.x', r.stdout)
        self.assertIn('ds-tegra-distro:    merge vertex/1.x.x', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', dry_run='true', token_kind='github')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('not checked (no App token', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', token_kind='github')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: no App token', r.stdout)
        self.assertIn('git merge --no-ff --no-commit origin/vertex/1.x.x; python3 scripts-release/release_git.py merge-keep-target vertex/main', r.stdout)
        self.assertEqual((self.fx.heads(self.fx.distro_bare), self.fx.heads(self.fx.meta_bare)), before)
        self.assertEqual(self.fx.prs(), [])

    def test_refusals(self):
        r = self.fx.run(MERGE_UP, '--child', 'vertex/main')
        self.assertEqual(r.returncode, 1)
        self.assertIn('product main', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', 'dev/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not a release line', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--parent', 'aggregator/main')
        self.assertEqual(r.returncode, 1)
        self.assertIn('the parent line of vertex/1.x.x is vertex/main', r.stdout)

    def test_distro_conflict_and_finish(self):
        child_tip = self.child_work()
        # the parent moved foo.txt too, and its pending version must survive
        parent_tip = self.fx.distro_commit('vertex/main', {'foo.txt': 'main fix\n', 'versions/vertex': '2.0.0\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('conflicts in: foo.txt', r.stdout)
        self.assertIn('::error::merge-up: ds-tegra-distro vertex/1.x.x -> vertex/main conflicts (foo.txt)', r.stdout)
        # named after the child's green commit, not the versions-only tip on top (ENG-2233)
        self.assertNotEqual(self.green_sha, child_tip)
        branch = f'merge-up/vertex/1.x.x/{self.green_sha[:7]}'
        # the PR branch is the parent with the child merged in, the markers committed (ENG-2200)
        head = self.fx.heads(self.fx.distro_bare)[branch]
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', head + '^1'), parent_tip)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', head + '^2'), child_tip)
        self.assertEqual(self.fx.subject(self.fx.distro_bare, head), 'Merge vertex/1.x.x into vertex/main (conflicts to resolve)')
        marked = self.fx.show(self.fx.distro_bare, head, 'foo.txt')
        self.assertIn('<<<<<<< ', marked)
        self.assertIn('||||||| ', marked)   # zdiff3: the base is in the hunk
        self.assertIn('>>>>>>> ', marked)
        self.assertEqual(self.fx.show(self.fx.distro_bare, head, 'versions/vertex'), '2.0.0')   # kept from the parent, not a conflict
        self.assertEqual(release_git.lock_pin(self.fx.show(self.fx.distro_bare, head, 'kas/include/base.lock.yml')), self.m3)
        self.assertIn(f'pushed {branch}: {head[:7]}, vertex/main plus the merge of vertex/1.x.x with markers in foo.txt', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        self.assertEqual((prs[0]['repo'], prs[0]['base'], prs[0]['head']), (DISTRO_REPO, 'vertex/main', branch))
        self.assertEqual(prs[0]['title'], 'Merge-up vertex/1.x.x into vertex/main (conflict)')
        self.assertIn('- `foo.txt`', prs[0]['body'])
        self.assertIn('The conflict markers are committed in the files above', prs[0]['body'])
        self.assertIn('The bot refuses to finish while a marker remains', prs[0]['body'])
        self.assertIn(prs[0]['url'], r.stdout)
        # meta-dsensos was merged first and stays merged
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', 'vertex/main^2'), self.m3)
        # a re-run leaves the branch alone and reuses the PR
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[branch], head)
        self.assertEqual(len(self.fx.prs()), 1)
        # a dry run of the same conflict pushes nothing and opens nothing
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # the branch is already on origin (the run above pushed it): the dry run says it is reused
        self.assertIn(f'dry run: {branch} already exists on origin; a real run leaves it as it is and reuses its PR', r.stdout)
        # not approved yet: refused, not merged
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('not ready to merge', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        # approved by the pusher only: refused
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'resolver'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': 'resolver'}]}]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(review))
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('other than the last pusher', r.stderr)
        # approved by someone else but the markers are still in: refused
        review['reviews'].append({'author': {'login': 'alice'}, 'state': 'APPROVED'})
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(review))
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: PR ds/distro#1 still has conflict markers in: foo.txt', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        # a human resolves on the PR branch: edit the marked file, commit, push
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.fx.distro, 'foo.txt'), 'both fixes\n')
        git(self.fx.distro, 'add', '-A')
        git(self.fx.distro, 'commit', '-q', '-m', 'resolve')
        git(self.fx.distro, 'push', '-q', 'origin', branch)
        pr_head = git(self.fx.distro, 'rev-parse', 'HEAD')
        # resolved and approved: merged, post-check on the new tip
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('would gh pr merge 1', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), pr_head)
        git(self.fx.distro_bare, 'merge-base', '--is-ancestor', child_tip, tip)
        self.assertIn('via PR 1', r.stdout)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        # the versions/ the human left on the PR branch is what merged (the PR path is the human's call)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'foo.txt'), 'both fixes')

    def test_meta_conflict_stops_before_distro(self):
        child_tip = self.child_work(trailing_versions=False)
        m4 = self.fx.meta_commit('vertex/main', 'main layer change\n', 'main layer change')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::error::merge-up: meta-dsensos vertex/1.x.x -> vertex/main conflicts (layer.txt)', r.stdout)
        branch = f'merge-up/vertex/1.x.x/{self.m3[:7]}'
        head = self.fx.heads(self.fx.meta_bare)[branch]
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', head + '^1'), m4)
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', head + '^2'), self.m3)
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.meta_bare, head, 'layer.txt'))
        self.assertEqual(self.fx.heads(self.fx.meta_bare)['vertex/main'], m4)
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        self.assertEqual((prs[0]['repo'], prs[0]['base'], prs[0]['head']), (META_REPO, 'vertex/main', branch))
        self.assertIn('- `layer.txt`', prs[0]['body'])
        # ds-tegra-distro untouched
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], self.main_tip)
        self.assertNotIn(f'merge-up/vertex/1.x.x/{child_tip[:7]}', self.fx.heads(self.fx.distro_bare))
        # approved but the markers are still in: refused
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': 'resolver'}]}]}
        write(os.path.join(self.fx.state, 'pr-1-review.json'), json.dumps(review))
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('::error::merge-up: PR ds/meta#1 still has conflict markers in: layer.txt', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.meta_bare)['vertex/main'], m4)
        # resolve in meta-dsensos (edit the marked file), finish: the meta PR merges and the distro merge follows
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.fx.meta_work, 'layer.txt'), 'both\n')
        git(self.fx.meta_work, 'add', '-A')
        git(self.fx.meta_work, 'commit', '-q', '-m', 'resolve')
        git(self.fx.meta_work, 'push', '-q', 'origin', branch)
        pr_head = git(self.fx.meta_work, 'rev-parse', 'HEAD')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        meta_tip = self.fx.heads(self.fx.meta_bare)['vertex/main']
        self.assertEqual(git(self.fx.meta_bare, 'rev-parse', meta_tip + '^2'), pr_head)
        self.assertIn('already contains the pin', r.stdout)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)
        self.assertEqual(release_git.lock_pin(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.lock.yml')), self.m3)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.0.1')

    # --- ENG-2233: supersession, stale PRs, the marker check's scope -------------

    def conflict_pr(self):
        """child_work, a conflicting parent commit, a merge-up run: the conflict PR #1 is open."""
        child_tip = self.child_work()
        parent_tip = self.fx.distro_commit('vertex/main', {'foo.txt': 'main fix\n'}, 'main fix')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        return child_tip, parent_tip, f'merge-up/vertex/1.x.x/{self.green_sha[:7]}'

    def child_moves(self, content='child fix 2\n'):
        """A new green commit on the child that still conflicts with the parent's foo.txt."""
        tip = self.fx.distro_commit('vertex/1.x.x', {'foo.txt': content}, 'another child fix')
        write(self.fx.green, tip + '\n')
        return tip

    def approve(self, number, pusher='resolver'):
        review = {'reviewDecision': 'APPROVED', 'mergeStateStatus': 'CLEAN', 'statusCheckRollup': [],
                  'reviews': [{'author': {'login': 'alice'}, 'state': 'APPROVED'}],
                  'commits': [{'authors': [{'login': pusher}]}]}
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps(review))

    def resolve(self, branch, content='both fixes\n', merge_child=False):
        """A human edits foo.txt on the PR branch (optionally merging the child in first) and pushes."""
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        if merge_child:
            subprocess.run(['git', '-C', self.fx.distro, 'merge', '-q', '--no-edit', 'origin/vertex/1.x.x'],
                           capture_output=True, text=True,
                           env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@x', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@x'))
        write(os.path.join(self.fx.distro, 'foo.txt'), content)
        git(self.fx.distro, 'add', '-A')
        git(self.fx.distro, 'commit', '-q', '-m', 'resolve')
        git(self.fx.distro, 'push', '-q', 'origin', branch)
        return git(self.fx.distro, 'rev-parse', 'HEAD')

    def test_a_newer_conflict_pr_closes_the_older_one(self):
        _, _, old_branch = self.conflict_pr()
        old_head = self.fx.heads(self.fx.distro_bare)[old_branch]
        new_child = self.child_moves()
        new_branch = f'merge-up/vertex/1.x.x/{new_child[:7]}'
        # a dry run says what it would close and closes nothing
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'dry run: would close superseded PR {DISTRO_REPO}#1 ({old_branch}), replaced by the new conflict PR on {new_branch}', r.stdout)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        # the real run opens #2 and closes #1 with a comment naming #2; #1's branch stays
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        prs = {p['number']: p for p in self.fx.prs()}
        self.assertEqual((prs[2]['head'], prs[2]['state']), (new_branch, 'OPEN'))
        self.assertEqual(prs[1]['state'], 'CLOSED')
        self.assertIn(f'Superseded by {prs[2]["url"]}', prs[1]['close_comment'])
        self.assertIn(f'The branch `{old_branch}` is kept', prs[1]['close_comment'])
        self.assertIn(f'closed superseded PR {DISTRO_REPO}#1 ({old_branch}), replaced by {prs[2]["url"]}', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[old_branch], old_head)
        # a closed PR cannot be finished
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('is not an open merge-up/vertex/1.x.x/* PR', r.stdout)
        # a re-run keeps #2 (it is the current one) and has nothing else to close
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn('closed superseded', r.stdout)
        self.assertEqual(self.fx.prs()[1]['state'], 'OPEN')

    def test_a_clean_merge_closes_open_conflict_prs(self):
        child_tip, _, branch = self.conflict_pr()
        # the parent takes the child's text by hand, so the next merge is clean
        self.fx.distro_commit('vertex/main', {'foo.txt': 'child fix\n'}, 'take the child fix on main')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'Superseded by the clean merge {tip}', pr['close_comment'])

    def test_a_stale_pr_is_refused_before_it_merges(self):
        _, parent_tip, branch = self.conflict_pr()
        self.resolve(branch)
        self.approve(1)
        # the child moves on after the PR was opened, and nobody re-ran merge-up
        new_child = self.child_moves()
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR {DISTRO_REPO}#1 is stale: vertex/1.x.x is at {new_child[:7]} now and {branch} does not contain it', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        # the resolver merges the child into the PR branch: now it finishes
        pr_head = self.resolve(branch, content='all three fixes\n', merge_child=True)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), pr_head)
        git(self.fx.distro_bare, 'merge-base', '--is-ancestor', new_child, tip)

    def test_markers_outside_the_pr_do_not_block_the_finish(self):
        # a doc on the parent that shows a conflict, unrelated to the merge-up
        self.fx.distro_commit('vertex/main', {'docs/conflicts.md': 'An example:\n<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n'}, 'doc with a conflict example')
        _, _, branch = self.conflict_pr()
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('still has conflict markers in: foo.txt', r.stdout)   # the PR's own file, not the doc
        self.assertNotIn('docs/conflicts.md', r.stdout)
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('via PR 1', r.stdout)

    def test_meta_newer_conflict_pr_closes_the_older_one(self):
        # two meta conflict PRs for successive meta child tips: the newer one closes the older one
        self.child_work(trailing_versions=False)
        self.fx.meta_commit('vertex/main', 'main layer change\n', 'main layer change')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        old_branch = f'merge-up/vertex/1.x.x/{self.m3[:7]}'
        # the child moves in both repos (new layer fix, new pin), still conflicting
        m5 = self.fx.meta_commit('vertex/1.x.x', 'm5 fix\n', 'second layer fix')
        tip = self.fx.distro_commit('vertex/1.x.x', {'kas/include/base.lock.yml': LOCK_YML.format(pin=m5)}, 'pin m5')
        write(self.fx.green, tip + '\n')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        new_branch = f'merge-up/vertex/1.x.x/{m5[:7]}'
        prs = {p['number']: p for p in self.fx.prs()}
        self.assertEqual((prs[1]['head'], prs[1]['state']), (old_branch, 'CLOSED'))
        self.assertEqual((prs[2]['repo'], prs[2]['head'], prs[2]['state']), (META_REPO, new_branch, 'OPEN'))
        self.assertIn(f'Superseded by {prs[2]["url"]}', prs[1]['close_comment'])

    # --- ENG-2233 review fixes ------------------------------------------------------

    def inject_pr(self, number, head, sha, repo=DISTRO_REPO, base='vertex/main'):
        """An open PR in the stub on a branch pointing at <sha> (pushed to the bare repo)."""
        bare = self.fx.distro_bare if repo == DISTRO_REPO else self.fx.meta_bare
        git(bare, 'update-ref', f'refs/heads/{head}', sha)
        prs = self.fx.prs()
        prs.append({'number': number, 'repo': repo, 'base': base, 'head': head, 'title': 'x', 'body': '',
                    'state': 'OPEN', 'url': f'https://github.com/{repo}/pull/{number}'})
        write(os.path.join(self.fx.state, 'prs.json'), json.dumps(prs))

    def state_of(self, number):
        return [p['state'] for p in self.fx.prs() if p['number'] == number][0]

    def test_a_conflict_without_markers_goes_on_the_child_tip(self):
        # modify/delete: the child deletes foo.txt, the parent edits it. git leaves no markers.
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', 'vertex/1.x.x', 'origin/vertex/1.x.x')
        git(self.fx.distro, 'rm', '-q', 'foo.txt')
        git(self.fx.distro, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.distro, 'push', '-q', 'origin', 'vertex/1.x.x')
        child_tip = git(self.fx.distro, 'rev-parse', 'HEAD')
        write(self.fx.green, child_tip + '\n')
        parent_tip = self.fx.distro_commit('vertex/main', {'foo.txt': 'main fix\n'}, 'main fix')
        branch = f'merge-up/vertex/1.x.x/{child_tip[:7]}'
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('no text markers in: foo.txt', r.stdout)
        self.assertIn(f'dry run: would push {branch} at {child_tip[:7]} (the child, no markers committed)', r.stdout)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        # the branch is the child itself, which GitHub shows as conflicting: nothing looks resolved
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[branch], child_tip)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], parent_tip)
        body = self.fx.prs()[0]['body']
        self.assertIn('leave no text markers', body)
        self.assertIn('- `foo.txt`', body)
        self.assertIn('::error::merge-up: ds-tegra-distro vertex/1.x.x -> vertex/main conflicts (foo.txt)', r.stdout)

    def test_the_zdiff3_base_line_left_in_is_refused(self):
        _, _, branch = self.conflict_pr()
        # a "keep both" resolution that deletes only the <<<<<<< and >>>>>>> lines
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        path = os.path.join(self.fx.distro, 'foo.txt')
        with open(path) as f:
            kept = [l for l in f if not l.startswith(('<<<<<<< ', '>>>>>>> '))]
        self.assertTrue(any(l.startswith('||||||| ') for l in kept))
        write(path, ''.join(kept))
        git(self.fx.distro, 'commit', '-q', '-am', 'half resolve')
        git(self.fx.distro, 'push', '-q', 'origin', branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 1)
        self.assertIn('still has conflict markers in: foo.txt', r.stdout)
        self.assertIn('`|||||||`', self.fx.prs()[0]['body'])

    def test_a_versions_bump_neither_supersedes_nor_staleness(self):
        _, parent_tip, branch = self.conflict_pr()
        # promote pushes a versions-only bump to the child ([skip ci], no RC run of its own)
        bump = self.fx.distro_commit('vertex/1.x.x', {'versions/vertex': '1.3.0\n'}, 'release: pending 1.3.0 [skip ci]')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{branch} already exists on origin; left as it is', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        pr_head = self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), pr_head)
        self.assertIn(f'--match-head-commit {pr_head}', self.fx.gh_log())
        self.assertNotEqual(bump, pr_head)

    def test_an_open_pr_that_contains_the_child_is_reused(self):
        _, _, branch = self.conflict_pr()
        new_child = self.child_moves()
        # the resolver follows the stale message: merges the child into the old branch
        self.resolve(branch, content='all three fixes\n', merge_child=True)
        git(self.fx.distro_bare, 'merge-base', '--is-ancestor', new_child, f'refs/heads/{branch}')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'open conflict PR {DISTRO_REPO}#1 ({branch}) already contains vertex/1.x.x at {new_child[:7]}; reusing it', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        self.assertNotIn(f'merge-up/vertex/1.x.x/{new_child[:7]}', self.fx.heads(self.fx.distro_bare))

    def test_every_outcome_closes_older_prs_and_only_this_childs(self):
        _, _, branch = self.conflict_pr()
        base = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.inject_pr(10, 'merge-up/vertex/1.x.x/0000000', base)          # an older one for this child
        self.inject_pr(11, 'merge-up/vertex/1.0.x/abc1234', base)          # another child into the same parent
        self.inject_pr(12, 'merge-up/vertex/1.x.xx/abc1234', base)         # a name that only starts the same
        self.inject_pr(13, 'feature/merge-up/vertex/1.x.x/abc1234', base)  # not a merge-up branch
        # outcome: a finished PR
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state_of(10), 'CLOSED')
        self.assertIn('Superseded by PR #1, merged at', [p for p in self.fx.prs() if p['number'] == 10][0]['close_comment'])
        for n in (11, 12, 13):
            self.assertEqual(self.state_of(n), 'OPEN', n)
        # outcome: nothing to merge
        self.inject_pr(14, 'merge-up/vertex/1.x.x/1111111', base)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)
        self.assertEqual(self.state_of(14), 'CLOSED')
        for n in (11, 12, 13):
            self.assertEqual(self.state_of(n), 'OPEN', n)

    def test_supersession_sees_past_gh_default_page_of_30(self):
        _, _, branch = self.conflict_pr()
        base = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.inject_pr(5, 'merge-up/vertex/1.x.x/0000000', base)      # old, so last in gh's newest-first list
        for n in range(100, 135):                                     # 35 newer feature PRs into the parent
            self.inject_pr(n, f'feature/f{n}', base)
        self.resolve(branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.state_of(5), 'CLOSED')
        self.assertEqual(self.state_of(100), 'OPEN')

    def test_meta_clean_merge_closes_the_meta_conflict_pr(self):
        self.child_work(trailing_versions=False)
        self.fx.meta_commit('vertex/main', 'main layer change\n', 'main layer change')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('|||||||', self.fx.show(self.fx.meta_bare, f'merge-up/vertex/1.x.x/{self.m3[:7]}', 'layer.txt'))
        # the parent takes the child's layer text by hand: the next merge is clean
        self.fx.meta_commit('vertex/main', 'm3 fix\n', 'take the child layer fix')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual((pr['repo'], pr['state']), (META_REPO, 'CLOSED'))
        self.assertIn('Superseded by the clean merge', pr['close_comment'])

    def test_meta_dry_run_finish_reports_the_distro_step(self):
        self.child_work(trailing_versions=False)
        self.fx.meta_commit('vertex/main', 'main layer change\n', 'main layer change')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        branch = f'merge-up/vertex/1.x.x/{self.m3[:7]}'
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.fx.meta_work, 'layer.txt'), 'both\n')
        git(self.fx.meta_work, 'commit', '-q', '-am', 'resolve')
        git(self.fx.meta_work, 'push', '-q', 'origin', branch)
        self.approve(1)
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', '--pr', '1', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('would gh pr merge 1', r.stdout)
        self.assertIn('its trial merge is skipped', r.stdout)
        self.assertNotIn('would push merge-up', r.stdout)
        self.assertNotIn('would close superseded', r.stdout)
        self.assertIn('ds-tegra-distro:    merge vertex/1.x.x', r.stdout)   # the distro plan still runs
        self.assertEqual(self.state_of(1), 'OPEN')

    def test_lockfile_conflict_when_parent_moved_the_pin(self):
        self.child_work(trailing_versions=False)
        # the parent bumped the same pin to its own commit
        m4 = self.fx.meta_commit('vertex/main', 'main only\n', 'main pin target', path='other.txt')
        self.fx.distro_commit('vertex/main', {'kas/include/base.lock.yml': LOCK_YML.format(pin=m4)}, 'pin m4')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('conflicts (kas/include/base.lock.yml)', r.stdout)
        self.assertIn('- `kas/include/base.lock.yml`', self.fx.prs()[0]['body'])

    def test_versions_kept_and_base_yml_merges(self):
        self.child_work(trailing_versions=False)
        # the child adds a layer stanza (as ENG-1690 added meta-swupdate); the parent moved its
        # pending version. The parent keeps versions/ and its branch name, and gets the stanza.
        child_base = BASE_YML.replace('branch: ota-dev   # tracks the trunk', 'branch: vertex/1.x.x').replace(
            '  meta-dsensos:\n', '  meta-new:\n    url: https://example.invalid/meta-new\n    path: repos/meta-new\n  meta-dsensos:\n')
        self.fx.distro_commit('vertex/1.x.x', {'kas/include/base.yml': child_base}, 'child adds a layer')
        self.fx.distro_commit('vertex/main', {'versions/vertex': '3.0.0\n'}, 'parent pending version')
        # a README fix on the child must reach the parent: only the per-product files are the
        # target's, versions/README.md merges like any doc
        self.fx.distro_commit('vertex/1.x.x', {'versions/README.md': 'pending versions, fixed\n', 'versions/vertex': '1.1.9\n'}, 'child: README fix and its own pending version')
        child_tip = self.fx.heads(self.fx.distro_bare)['vertex/1.x.x']
        write(self.fx.green, child_tip + '\n')
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        base = self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.yml')
        self.assertEqual(release_git.base_branch_get(base), 'vertex/main')
        self.assertIn('  meta-new:\n    url: https://example.invalid/meta-new\n', base)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '3.0.0')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/README.md'), 'pending versions, fixed')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'foo.txt'), 'child fix')

    def test_base_yml_content_conflict_opens_a_pr(self):
        self.child_work(trailing_versions=False)
        # both sides rewrote the same comment line: a real conflict, not a branch-name one
        self.fx.distro_commit('vertex/1.x.x', {'kas/include/base.yml': BASE_YML.replace('# test base.yml', '# child note').replace('branch: ota-dev   # tracks the trunk', 'branch: vertex/1.x.x')}, 'child base.yml')
        self.fx.distro_commit('vertex/main', {'kas/include/base.yml': BASE_YML.replace('# test base.yml', '# parent note').replace('branch: ota-dev   # tracks the trunk', 'branch: vertex/main')}, 'parent base.yml')
        child_tip = self.fx.heads(self.fx.distro_bare)['vertex/1.x.x']
        write(self.fx.green, child_tip + '\n')
        main_before = self.fx.heads(self.fx.distro_bare)['vertex/main']
        r = self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('conflicts (kas/include/base.yml)', r.stdout)
        self.assertIn('- `kas/include/base.yml`', self.fx.prs()[0]['body'])
        self.assertEqual(self.fx.heads(self.fx.distro_bare)['vertex/main'], main_before)

class MergeUpSwitchTests(unittest.TestCase):
    """The switches block every merge-up run prints (ENG-2234): the variable, the
    active rules, the ref whose scripts are in use. No rule exists yet, so a run
    does the same whatever the variable says, unless it does not parse."""

    # the MergeUpTests fixture (child vertex/1.x.x, parent vertex/main), without its cases
    setUp = MergeUpTests.setUp
    child_work = MergeUpTests.child_work

    def scripts_sha(self):
        return git(os.path.dirname(SCRIPTS), 'rev-parse', '--short=7', 'HEAD')

    def run_up(self, extra_env=None, **kw):
        return self.fx.run(MERGE_UP, '--child', 'vertex/1.x.x', extra_env=extra_env or {}, **kw)

    def block(self, stdout):
        """The lines of the switches block, by label."""
        lines = stdout.splitlines()
        start = lines.index('== merge-up automation')
        out = {}
        for line in lines[start + 1:start + 6]:
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
        self.assertEqual(b['config'], 'finish.strict=false, lock_pin.verify_finish=false, lock_pin.exact=bot-pr, '
                                      'lock_pin.extra=bot-pr (release/config.json at that ref)')
        self.assertEqual(b['read token'], 'absent (read-only, ds and sensor-portal)')
        # the block comes first, before the plan
        self.assertLess(r.stdout.index('== merge-up automation'), r.stdout.index('== merge-up plan'))
        tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)

    def test_every_valid_value_merges_the_same(self):
        """shadow and a rule list change the printed lines and nothing else yet."""
        want = {
            'off': 'none (automation: off, no rule is evaluated)',
            'shadow': 'none acting; shadow (evaluate and log only): lock-pin, splice, pins',
            'pins, lock-pin': 'acting: lock-pin, pins; shadow (evaluate and log only): splice',
            'lock-pin,splice,pins': 'acting: lock-pin, splice, pins; shadow (evaluate and log only): none',
        }
        for value, rules in want.items():
            with self.subTest(value=value):
                self.setUp()  # a fresh fixture per value
                child_tip = self.child_work()
                r = self.run_up({'DS_MERGE_UP_AUTOMATION': value})
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                b = self.block(r.stdout)
                self.assertEqual(b['variable'], f'DS_MERGE_UP_AUTOMATION={value}')
                self.assertEqual(b['active rules'], rules)
                tip = self.fx.heads(self.fx.distro_bare)['vertex/main']
                self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^1'), self.main_tip)
                self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), child_tip)
                self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), 'Merge vertex/1.x.x into vertex/main')
                self.assertEqual(release_git.lock_pin(self.fx.show(self.fx.distro_bare, tip, 'kas/include/base.lock.yml')), self.m3)
                self.assertEqual(self.fx.subject(self.fx.meta_bare, self.fx.heads(self.fx.meta_bare)['vertex/main']),
                                 'Merge vertex/1.x.x into vertex/main')
                self.assertEqual(self.fx.prs(), [])
                self.assertIn('post-check ok', r.stdout)

    def test_a_value_that_does_not_parse_stops_before_anything(self):
        self.child_work()
        distro_before = self.fx.heads(self.fx.distro_bare)
        meta_before = self.fx.heads(self.fx.meta_bare)
        for value, token in (('on', "'on'"), ('splice,lockpin', "'lockpin'"), ('shadow,pins', "'shadow'"),
                             ('Shadow', "'Shadow'"), ('splice,', 'empty item')):
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
        self.assertEqual(self.fx.heads(self.fx.distro_bare), distro_before)
        self.assertEqual(self.fx.heads(self.fx.meta_bare), meta_before)
        self.assertEqual(self.fx.gh_log(), '')
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
        self.assertEqual(self.block(r.stdout)['active rules'],
                         'acting: splice; shadow (evaluate and log only): lock-pin, pins')

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
        r = self.run_up({'GITHUB_REF_NAME': 'from-the-runner', 'DEFAULT_BRANCH': 'vertex/elsewhere'}, dry_run='true')
        self.assertEqual(self.block(r.stdout)['scripts'],
                         f"{ref} at {sha} (NOT the default branch vertex/elsewhere: this run uses that ref's "
                         'scripts and release/config.json)')

    def test_the_read_token_is_reported_and_never_printed(self):
        self.child_work()
        r = self.run_up({'MERGE_UP_READ_TOKEN': 'ghs_read-only-secret'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.block(r.stdout)['read token'], 'present (read-only, ds and sensor-portal)')
        self.assertNotIn('ghs_read-only-secret', r.stdout + r.stderr)
        self.assertNotIn('ghs_read-only-secret', self.fx.gh_log())

    def test_the_block_goes_to_the_step_summary(self):
        self.child_work()
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.run_up({'MERGE_UP_SUMMARY': summary, 'DS_MERGE_UP_AUTOMATION': 'lock-pin'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(summary) as f:
            text = f.read()
        self.assertTrue(text.startswith('### Merge-up vertex/1.x.x\n\n```\n== merge-up automation\n'), text)
        self.assertIn('variable:           DS_MERGE_UP_AUTOMATION=lock-pin\n', text)
        self.assertIn('active rules:       acting: lock-pin; shadow (evaluate and log only): splice, pins\n', text)
        self.assertIn(f'at {self.scripts_sha()}', text)
        self.assertTrue(text.endswith('```\n'))
        # a refused value leaves the summary alone: the error annotation carries it
        os.remove(summary)
        r = self.run_up({'MERGE_UP_SUMMARY': summary, 'DS_MERGE_UP_AUTOMATION': 'nope'})
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(summary))


class MergeUpWorkflowTests(unittest.TestCase):
    """release-merge-up.yml hands the switches to merge-up.sh (ENG-2234). Text checks:
    the suite is standard library only, and check_yaml.py parses the file in pr-checks."""

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

    def test_the_read_token_is_separate_and_read_only(self):
        read = self.step('App token (read-only, ds and sensor-portal)')
        self.assertIn('id: app_read\n', read)
        self.assertIn('repositories: |\n            ds\n            sensor-portal\n', read)
        permissions = [line.strip() for line in read.splitlines() if line.strip().startswith('permission-')]
        self.assertEqual(permissions, ['permission-contents: read'])
        self.assertNotIn('meta-dsensos', read.split('with:')[1])
        self.assertIn('continue-on-error: true\n', read)
        merge = self.step('Merge up')
        self.assertIn('MERGE_UP_READ_TOKEN: ${{ steps.app_read.outputs.token }}\n', merge)
        # it is used under that one name and nothing else
        self.assertEqual(self.text.count('steps.app_read.'), 1)
        # the write token's repository list did not grow
        write_step = self.step('App token')
        self.assertIn('repositories: |\n            ${{ github.event.repository.name }}\n            meta-dsensos\n', write_step)
        self.assertNotIn('sensor-portal', write_step)


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
        self.assertEqual(auto, {'value': 'shadow', 'act': [], 'shadow': ['lock-pin', 'splice', 'pins']})
        for rule in release_git.MERGE_UP_RULES:
            self.assertEqual(release_git.rule_mode(auto, rule), 'shadow')

    def test_a_list_acts_and_the_rest_stays_in_shadow(self):
        cases = {
            'lock-pin': (['lock-pin'], ['splice', 'pins']),
            'splice': (['splice'], ['lock-pin', 'pins']),
            'pins,lock-pin': (['lock-pin', 'pins'], ['splice']),
            ' splice , pins ': (['splice', 'pins'], ['lock-pin']),
            'lock-pin,splice,pins': (['lock-pin', 'splice', 'pins'], []),
            'splice,splice': (['splice'], ['lock-pin', 'pins']),
        }
        for value, (act, shadow) in cases.items():
            with self.subTest(value=value):
                auto = release_git.automation(value)
                self.assertEqual((auto['act'], auto['shadow']), (act, shadow))
                for rule in release_git.MERGE_UP_RULES:
                    self.assertEqual(release_git.rule_mode(auto, rule), 'act' if rule in act else 'shadow')

    def test_anything_else_is_an_error_naming_the_token(self):
        cases = {
            'on': "'on' is not a rule",
            'true': "'true' is not a rule",
            'Shadow': "'Shadow' is not a rule",
            'OFF': "'OFF' is not a rule",
            'lock_pin': "'lock_pin' is not a rule",
            'lockpin,splice': "'lockpin' is not a rule",
            'splice,pin': "'pin' is not a rule",
            'splice pins': "'splice pins' is not a rule",
            'splice;pins': "'splice;pins' is not a rule",
            'shadow,splice': "'shadow' is not a rule (\"off\" and \"shadow\" stand alone",
            'splice,off': "'off' is not a rule (\"off\" and \"shadow\" stand alone",
            'splice,': 'empty item in the list',
            ',': 'empty item in the list',
            'lock-pin,,pins': 'empty item in the list',
        }
        for value, needle in cases.items():
            with self.subTest(value=value):
                with self.assertRaises(release_git.Error) as cm:
                    release_git.automation(value)
                self.assertIn(needle, str(cm.exception))
                self.assertIn('DS_MERGE_UP_AUTOMATION=', str(cm.exception))
                self.assertIn('lock-pin, splice, pins', str(cm.exception))

    def test_rule_mode_refuses_an_unknown_rule(self):
        with self.assertRaises(release_git.Error):
            release_git.rule_mode(release_git.automation('shadow'), 'lock_pin')

    def test_summary_lines(self):
        def lines(value):
            return release_git.automation_summary(release_git.automation(value))
        self.assertEqual(lines(None), ['variable:           DS_MERGE_UP_AUTOMATION is absent',
                                       'active rules:       none (automation: off, no rule is evaluated)'])
        self.assertEqual(lines('off'), ['variable:           DS_MERGE_UP_AUTOMATION=off',
                                        'active rules:       none (automation: off, no rule is evaluated)'])
        self.assertEqual(lines('shadow')[1],
                         'active rules:       none acting; shadow (evaluate and log only): lock-pin, splice, pins')
        self.assertEqual(lines('pins,lock-pin'), [
            'variable:           DS_MERGE_UP_AUTOMATION=pins,lock-pin',
            'active rules:       acting: lock-pin, pins; shadow (evaluate and log only): splice'])
        self.assertEqual(lines('lock-pin,splice,pins')[1],
                         'active rules:       acting: lock-pin, splice, pins; shadow (evaluate and log only): none')

    def cli(self, *args, env_value=None):
        env = {k: v for k, v in os.environ.items() if k != release_git.AUTOMATION_VAR}
        if env_value is not None:
            env[release_git.AUTOMATION_VAR] = env_value
        return subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), *args],
                              capture_output=True, text=True, env=env)

    def test_automation_cli(self):
        r = self.cli('automation')
        self.assertEqual((r.returncode, r.stdout.splitlines()[1]),
                         (0, 'active rules:       none (automation: off, no rule is evaluated)'))
        r = self.cli('automation', env_value='splice')
        self.assertEqual(r.returncode, 0)
        self.assertIn('acting: splice; shadow (evaluate and log only): lock-pin, pins', r.stdout)
        self.assertEqual(self.cli('automation', '--rule', 'splice', env_value='splice').stdout, 'act\n')
        self.assertEqual(self.cli('automation', '--rule', 'pins', env_value='splice').stdout, 'shadow\n')
        self.assertEqual(self.cli('automation', '--rule', 'pins').stdout, 'off\n')
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
        cfg = release_git.merge_up_config(self.root_with({'products': ['vertex']}))
        self.assertEqual(cfg, version.MERGE_UP_DEFAULTS)
        self.assertIs(cfg['finish']['strict'], False)
        self.assertIs(cfg['lock_pin']['verify_finish'], False)

    def test_merge_up_config_reads_the_block(self):
        root = self.root_with({'products': ['vertex'], 'merge_up': {
            'finish': {'strict': True, 'required_checks': ['pr-checks']}, 'lock_pin': {'exact': 'push'}}})
        cfg = release_git.merge_up_config(root)
        self.assertEqual(cfg['finish'], {'strict': True, 'required_checks': ['pr-checks']})
        self.assertEqual(cfg['lock_pin']['exact'], 'push')   # not the default (bot-pr): the key was read
        self.assertEqual(cfg['lock_pin']['extra'], 'bot-pr')
        self.assertEqual(cfg['lock_pin']['wait_minutes'], 15)
        r = self.cli('merge-up-config', '--root', root, '--key', 'finish.strict')
        self.assertEqual((r.returncode, r.stdout), (0, 'true\n'))
        r = self.cli('merge-up-config', '--root', root, '--key', 'lock_pin.exact')
        self.assertEqual((r.returncode, r.stdout), (0, 'push\n'))
        # the shipped config and a config without the block both say bot-pr
        r = self.cli('merge-up-config', '--key', 'lock_pin.exact')
        self.assertEqual((r.returncode, r.stdout), (0, 'bot-pr\n'))
        r = self.cli('merge-up-config', '--root', self.root_with({'products': ['vertex']}), '--key', 'lock_pin.exact')
        self.assertEqual((r.returncode, r.stdout), (0, 'bot-pr\n'))
        r = self.cli('merge-up-config', '--root', root, '--key', 'finish.nope')
        self.assertEqual(r.returncode, 1)
        self.assertIn("merge_up has no key 'finish.nope'", r.stderr)

    def test_merge_up_config_defaults_to_the_scripts_own_checkout(self):
        """Not the working directory: the config of the ref whose scripts run governs."""
        elsewhere = self.root_with({'products': ['vertex'], 'merge_up': {'finish': {'strict': True}}})
        r = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'merge-up-config'],
                           capture_output=True, text=True, cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), version.merge_up(os.path.dirname(SCRIPTS)))
        self.assertEqual(release_git.merge_up_config(), version.merge_up(os.path.dirname(SCRIPTS)))

    def test_merge_up_config_refuses_a_bad_block(self):
        root = self.root_with({'products': ['vertex'], 'merge_up': {'finish': {'strict': 'true'}}})
        with self.assertRaises(release_git.Error) as cm:
            release_git.merge_up_config(root)
        self.assertIn('merge_up.finish.strict must be true or false', str(cm.exception))
        r = self.cli('merge-up-config', '--root', root)
        self.assertEqual((r.returncode, r.stdout), (1, ''))
        with self.assertRaises(release_git.Error):
            release_git.merge_up_config(self.root_with({'products': ['vertex'], 'merge-up': {}}))

    def test_never_auto_returns_the_first_matching_pattern(self):
        patterns = ['.github/workflows/**', '*sudoers*', 'scripts-secboot/**']
        self.assertEqual(release_git.never_auto(patterns, '.github/workflows/sub/x.yml'), '.github/workflows/**')
        self.assertEqual(release_git.never_auto(patterns, 'recipes/x/files/ds-sudoers'), '*sudoers*')
        self.assertEqual(release_git.never_auto(patterns, 'scripts-secboot/a/b.sh'), 'scripts-secboot/**')
        self.assertIsNone(release_git.never_auto(patterns, 'kas/include/base.lock.yml'))
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
    tracked file in the tree it governs, so a path that is renamed away, or a
    pattern that is mistyped, fails here instead of silently protecting nothing.

    DISTRO_UNMATCHED and META_UNMATCHED name the patterns that are kept although
    nothing matches them today. The test fails both ways: a listed pattern that
    starts matching must leave the set, a pattern that stops matching must be
    fixed or entered here on purpose."""

    # No sudoers file is tracked in ds-tegra-distro (they live in meta-dsensos).
    # The pattern stays so that one added here later is covered from its first commit.
    DISTRO_UNMATCHED = {'*sudoers*'}
    META_UNMATCHED = set()

    def unmatched(self, patterns, files):
        return {p for p in patterns if not any(release_git.never_auto([p], f) for f in files)}

    def test_distro_patterns_match_tracked_files(self):
        root = os.path.dirname(SCRIPTS)
        files = tracked_files(root)
        if files is None:
            self.skipTest(f'{root} is not a git checkout')
        self.assertGreater(len(files), 50)
        patterns = release_git.merge_up_config(root)['never_auto']
        self.assertEqual(self.unmatched(patterns, files), self.DISTRO_UNMATCHED,
                         f'never_auto patterns with no tracked file among {len(files)} in {root}')

    def test_meta_dsensos_patterns_match_tracked_files(self):
        """Against a meta-dsensos checkout: $META_DSENSOS_TREE, else repos/meta-dsensos
        (where kas puts it; pr-checks checks the layers out before the tests run)."""
        root = os.path.dirname(SCRIPTS)
        tree = os.environ.get('META_DSENSOS_TREE') or os.path.join(root, 'repos/meta-dsensos')
        files = tracked_files(tree) if os.path.isdir(os.path.join(tree, 'recipes-core')) else None
        if files is None:
            if os.environ.get('META_DSENSOS_TREE'):
                self.fail(f'META_DSENSOS_TREE={tree} is not a meta-dsensos git checkout')
            self.skipTest(f'no meta-dsensos checkout at {tree} (run ./run-kas checkout, or set META_DSENSOS_TREE)')
        self.assertGreater(len(files), 50)
        patterns = release_git.merge_up_config(root)['meta_dsensos']['never_auto']
        self.assertEqual(self.unmatched(patterns, files), self.META_UNMATCHED,
                         f'meta_dsensos.never_auto patterns with no tracked file among {len(files)} in {tree}')


if __name__ == '__main__':
    unittest.main()
