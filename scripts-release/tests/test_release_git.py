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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402

CREATE = os.path.join(SCRIPTS, 'create-branch.sh')
MERGE_UP = os.path.join(SCRIPTS, 'merge-up.sh')
REPO = 'ds/repo'
ENV_FILE = '.github/yocto-ci.env'
CONFIG = {'products': ['ds'], 'product_prefix': False, 'bookkeeping_files': [ENV_FILE]}
MATRIX = {'products': {'ds': {'flavors': ['ds_ds-arm64_release']}}}
PREFIXED = ['vertex', 'aggregator']

GH_STUB = r'''#!/usr/bin/env python3
"""Stub gh for the merge-up and create-branch tests."""
import json, os, subprocess, sys, tempfile
STATE = os.environ['GH_STUB_STATE']
REPOS = dict(kv.split('=', 1) for kv in os.environ['GH_STUB_REPOS'].split(','))
PRS = os.path.join(STATE, 'prs.json')
FLAGS = {'--merge', '--squash', '--rebase', '--draft', '--prerelease', '--delete-branch'}

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

def view_doc(pr):
    doc = {'number': pr['number'], 'url': pr['url'], 'state': pr['state'], 'headRefName': pr['head'],
           'baseRefName': pr['base'], 'headRefOid': rev(pr['repo'], 'refs/heads/' + pr['head']) if pr['state'] == 'OPEN' else pr.get('merged_head', ''),
           'reviewDecision': None, 'mergeStateStatus': 'BLOCKED', 'statusCheckRollup': [], 'reviews': [],
           'commits': [{'authors': [{'login': 'resolver'}]}]}
    override = os.path.join(STATE, 'pr-%d-review.json' % pr['number'])
    if os.path.exists(override):
        doc.update(json.load(open(override)))
    return doc

def main(argv):
    with open(os.path.join(STATE, 'log'), 'a') as f:
        f.write(' '.join(argv) + '\n')
    pos, opts = parse(argv)
    # -R/--repo, else the repo the workflow would name, else the one repo configured
    repo = opts.get('-R') or opts.get('--repo') or os.environ.get('GITHUB_REPOSITORY') or next(iter(REPOS))
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
                and p['head'] == opts.get('--head', p['head']) and p['state'] == opts.get('--state', 'open').upper()]
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
        if pr['state'] != 'OPEN':
            print('PR not open', file=sys.stderr)
            return 1
        pr['state'] = 'CLOSED'; pr['close_comment'] = opts.get('--comment', '')
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
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, GIT_AUTHOR_NAME='gh', GIT_AUTHOR_EMAIL='gh@x', GIT_COMMITTER_NAME='gh', GIT_COMMITTER_EMAIL='gh@x')
        subprocess.check_call(['git', 'clone', '-q', REPOS[repo], tmp], env=env)
        subprocess.check_call(['git', '-C', tmp, 'checkout', '-q', pr['base']], env=env)
        subprocess.check_call(['git', '-C', tmp, 'merge', '-q', '--no-ff', '-m', 'Merge pull request #%d' % n, head], env=env)
        subprocess.check_call(['git', '-C', tmp, 'push', '-q', 'origin', pr['base']], env=env)
        pr['state'] = 'MERGED'; pr['merged_head'] = head
        save(prs)
        print('merged')
        return 0
    print('stub gh: unsupported: ' + ' '.join(argv), file=sys.stderr)
    return 1

sys.exit(main(sys.argv[1:]))
'''

TEST_IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@x'}


def git(cwd, *args, **kw):
    env = dict(os.environ, **TEST_IDENTITY)
    env.update(kw.pop('env', {}))
    return subprocess.run(['git', '-C', cwd, *args], check=True, capture_output=True, text=True, env=env, **kw).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)


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
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
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

    def gh_log(self):
        p = os.path.join(self.state, 'log')
        if not os.path.exists(p):
            return ''
        with open(p) as f:
            return f.read()

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
        """ENG-1934: the pilot promotes 0.0.1 on main with bump=patch and cuts both lines from it."""
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
        self.assertIn('leave no text markers', body)
        self.assertIn('- `foo.txt`', body)

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


if __name__ == '__main__':
    unittest.main()
