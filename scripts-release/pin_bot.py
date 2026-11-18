#!/usr/bin/env python3
"""Trusted Actions controller for SHA-only propagation; disabled by default.

Commands: prepare [--source-pr N], finish --pr N, safety --pr N.
Uses gh with an installation token; never executes code from candidate repos.
"""
import argparse
import base64
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

import pin_policy as p
import release_git

CHECK = 'pin-propagation-safety'
REVIEW_CHECK = 'pin-propagation-review-block'
PREFIX = 'pin-bot/'
BLOCKED = 'HUMAN REVIEW REQUIRED'
# GitHub computes a new PR's test merge asynchronously, usually within
# seconds. Poll that long in the same run; a later run handles the rest.
MERGEABLE_POLLS, MERGEABLE_DELAY = 12, 10
# A clone can fail transiently (seen 2026-11-09: one run refused, its rerun
# passed). finish would block a good PR on it, so retry before giving up.
# Three attempts at 120 s each keep two clones inside a 30-minute job.
CLONE_ATTEMPTS, CLONE_DELAY, CLONE_TIMEOUT = 3, 15, 120
FAILURES = (p.Review, KeyError, ValueError, subprocess.SubprocessError)
# A malformed distro lockfile or base.yml (release_git.Error) is line-level:
# prepare lets it stop the scan loudly rather than ledger every source on the
# line; finish blocks its PR on it, as on any evidence it cannot verify.


class API:
    def __init__(self, repo, app_id, slug):
        if not repo or not app_id or not slug:
            raise p.Review('repository, App ID and App slug must be configured')
        self.repo, self.app_id, self.login = repo, int(app_id), slug + '[bot]'
        self.meta = repo.split('/')[0] + '/meta-dsensos'
        self.ledger_branch = 'pin-bot/source-review'
        self.anchor_branch = 'pin-bot/review-anchor'

    def call(self, path, method='GET', data=None, pages=False):
        args = ['gh', 'api', '--method', method, path]
        if pages:
            args += ['--paginate', '--slurp']
        if data is not None:
            args += ['--input', '-']
        env = dict(os.environ)
        if not path.startswith(f'repos/{self.repo}/'):
            env['GH_TOKEN'] = os.environ.get('SOURCE_GH_TOKEN', env.get('GH_TOKEN', ''))
        r = subprocess.run(args, input=json.dumps(data) if data is not None else None,
                           text=True, capture_output=True, timeout=120, env=env)
        if r.returncode:
            raise p.Review('GitHub API request failed: ' + path + ': ' + r.stderr.strip())
        result = json.loads(r.stdout) if r.stdout.strip() else None
        return result

    def items(self, path, key=None):
        pages = self.call(path, pages=True)
        return [item for page in pages for item in (page[key] if key else page)]

    def pr(self, number, repo=None):
        return self.call(f'repos/{repo or self.repo}/pulls/{int(number)}')

    def branch_sha(self, branch):
        return self.call(f'repos/{self.repo}/git/ref/heads/{quote(branch, safe="")}')['object']['sha']

    def checks(self, head, name=None):
        query = '&check_name=' + quote(name, safe='') if name else ''
        return self.items(f'repos/{self.repo}/commits/{head}/check-runs?per_page=100&filter=all{query}', 'check_runs')

    def review_anchor(self):
        refs = self.call(f'repos/{self.repo}/git/matching-refs/heads/{self.anchor_branch}')
        exact = [r for r in refs if r['ref'] == 'refs/heads/' + self.anchor_branch]
        if len(exact) > 1:
            raise p.Review('ambiguous review anchor')
        return exact[0]['object']['sha'] if exact else None

    def ensure_review_anchor(self):
        anchor = self.review_anchor()
        if anchor:
            return anchor
        # Only the main writer initializes this orphan commit. Check records
        # on it never affect a business commit's status, and safety jobs can
        # persist markers with Checks:write and Contents:read thereafter.
        tree = self.call(f'repos/{self.repo}/git/trees', 'POST', {'tree': [{
            'path': 'review-anchor.json', 'mode': '100644', 'type': 'blob', 'content': '{"schema":1}\n'}]})
        commit = self.call(f'repos/{self.repo}/git/commits', 'POST', {
            'message': 'Anchor for permanent pin propagation review checks', 'tree': tree['sha'], 'parents': []})
        self.call(f'repos/{self.repo}/git/refs', 'POST', {
            'ref': 'refs/heads/' + self.anchor_branch, 'sha': commit['sha']})
        return commit['sha']

    def pr_review_blocked(self, number):
        anchor = self.review_anchor()
        name = f'{REVIEW_CHECK}/{number}'
        return bool(anchor) and any(c['name'] == name and c['app']['id'] == self.app_id and
                                   c.get('external_id') == f'pr-review/{number}' for c in self.checks(anchor, name))

    def pr_review(self, number, reason):
        if self.pr_review_blocked(number):
            return
        anchor = self.review_anchor()
        if not anchor:
            raise p.Review('review anchor is missing; main writer must initialize it')
        self.call(f'repos/{self.repo}/check-runs', 'POST', {
            'name': f'{REVIEW_CHECK}/{number}', 'head_sha': anchor, 'external_id': f'pr-review/{number}',
            'status': 'completed', 'conclusion': 'failure',
            'output': {'title': BLOCKED, 'summary': reason}})

    def record(self, pr):
        found = [x for x in self.checks(pr['head']['sha'])
                 if x['name'] == CHECK and x['app']['id'] == self.app_id
                 and x.get('external_id', '').startswith('pin-propagation/v1/')]
        if len(found) != 1:
            raise p.Review('missing or duplicate App provenance record')
        check = found[0]
        if check['output']['title'] == BLOCKED:
            raise p.Review('persistent human-review block; manual resolution required')
        return check, json.loads(check['output']['text'])

    def update(self, check, title, data, conclusion=None):
        body = {'output': {'title': title, 'summary': title, 'text': json.dumps(data)}}
        if conclusion:
            body.update(status='completed', conclusion=conclusion)
        else:
            body['status'] = 'in_progress'
        self.call(f'repos/{self.repo}/check-runs/{check["id"]}', 'PATCH', body)

    def block(self, pr, reason):
        # Any prior blocked record survives retries; never overwrite it.
        checks = [c for c in self.checks(pr['head']['sha'])
                  if c['name'] == CHECK and c['app']['id'] == self.app_id]
        if not checks:
            self.call(f'repos/{self.repo}/check-runs', 'POST', {
                'name': CHECK, 'head_sha': pr['head']['sha'], 'status': 'completed', 'conclusion': 'failure',
                'output': {'title': BLOCKED, 'summary': reason}})
        for check in checks:
            if check['output']['title'] != BLOCKED:
                data = {'reason': reason}
                try:
                    data.update(json.loads(check['output'].get('text') or '{}'))
                except ValueError:
                    pass
                self.update(check, BLOCKED, data, 'failure')
        self.flag(self.repo, pr['number'], reason)

    def flag(self, repo, number, reason, source=False):
        if source or repo != self.repo:
            # Persist pre-PR review blocks in the destination repository so
            # recovery scans cannot silently re-enable an ambiguous source.
            # This does not require source write or Issues permissions.
            self.source_review(repo, number, reason)
            message = f'Source {repo}#{number}: human review required: {reason}'
            print(message)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                    summary.write(message + '\n\n')
            return
        self.pr_review(number, reason)
        marker = '<!-- pin-propagation-human-review -->'
        comments = self.items(f'repos/{repo}/issues/{number}/comments?per_page=100')
        if any(c['user']['login'] == self.login and marker in c['body'] for c in comments):
            return
        self.call(f'repos/{repo}/issues/{number}/comments', 'POST', {
            'body': f'{marker}\n**Human review required. Automatic merging is blocked.**\n\n{reason}'})

    def flagged(self, repo, number, source=False):
        _, ledger = self.source_reviews()
        if source or repo != self.repo:
            return f'{repo}#{number}' in ledger['blocked']
        if f'pr:{repo}#{number}' in ledger['blocked']:
            return True
        if self.pr_review_blocked(number):
            return True
        return any(c['user']['login'] == self.login and
                   '<!-- pin-propagation-human-review -->' in c['body']
                   for c in self.items(f'repos/{repo}/issues/{number}/comments?per_page=100'))

    def source_reviews(self):
        ref = 'refs/heads/' + self.ledger_branch
        refs = self.call(f'repos/{self.repo}/git/matching-refs/heads/{self.ledger_branch}')
        exact = [r for r in refs if r['ref'] == ref]
        if not exact:
            return None, {'schema': 1, 'blocked': {}}
        if len(exact) != 1:
            raise p.Review('ambiguous source-review ledger')
        sha = exact[0]['object']['sha']
        content = self.call(f'repos/{self.repo}/contents/reviews.json?ref={sha}')
        ledger = json.loads(base64.b64decode(content['content']))
        if ledger.get('schema') != 1 or not isinstance(ledger.get('blocked'), dict):
            raise p.Review('invalid source-review ledger')
        return sha, ledger

    def source_review(self, repo, number, reason, prefix=''):
        parent, ledger = self.source_reviews()
        key = f'{prefix}{repo}#{number}'
        if key in ledger['blocked']:
            return
        ledger['blocked'][key] = {'reason': reason}
        tree_data = {'tree': [{'path': 'reviews.json', 'mode': '100644', 'type': 'blob',
                              'content': json.dumps(ledger, indent=2) + '\n'}]}
        if parent:
            tree_data['base_tree'] = self.call(f'repos/{self.repo}/git/commits/{parent}')['tree']['sha']
        tree = self.call(f'repos/{self.repo}/git/trees', 'POST', tree_data)
        commit = self.call(f'repos/{self.repo}/git/commits', 'POST', {
            'message': f'Require human review for {key}', 'tree': tree['sha'],
            'parents': [parent] if parent else []})
        if parent:
            self.call(f'repos/{self.repo}/git/refs/heads/{self.ledger_branch}', 'PATCH', {'sha': commit['sha']})
        else:
            self.call(f'repos/{self.repo}/git/refs', 'POST', {
                'ref': 'refs/heads/' + self.ledger_branch, 'sha': commit['sha']})


def clone(repo, directory):
    # No credential in a URL, file or log. gh reads the installation token
    # from GH_TOKEN. Only git object inspection follows; no candidate hooks.
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_CONFIG_COUNT='2',
               GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
               GIT_CONFIG_KEY_1='credential.helper', GIT_CONFIG_VALUE_1='!gh auth git-credential')
    env['GH_TOKEN'] = os.environ.get('SOURCE_GH_TOKEN', env.get('GH_TOKEN', ''))
    detail = ''
    for attempt in range(1, CLONE_ATTEMPTS + 1):
        try:
            r = subprocess.run(['git', 'clone', '--quiet', '--no-checkout',
                                f'https://github.com/{repo}.git', str(directory)], env=env,
                               capture_output=True, text=True, timeout=CLONE_TIMEOUT)
            if not r.returncode:
                return directory
            # git's own message; the token never appears in the URL or args.
            detail = (r.stderr or r.stdout or '').strip()[-500:] or f'exit {r.returncode}'
        except subprocess.TimeoutExpired:
            detail = f'timed out after {CLONE_TIMEOUT} s'
        print(f'git clone {repo}: attempt {attempt}/{CLONE_ATTEMPTS} failed: {detail}', file=sys.stderr)
        shutil.rmtree(directory, ignore_errors=True)
        if attempt < CLONE_ATTEMPTS:
            time.sleep(CLONE_DELAY)
    raise p.Review(f'could not fetch repository {repo}: {detail}')


def validate_source(api, source, distro, meta, config):
    if not source.get('merged_at') or not source.get('merge_commit_sha'):
        raise p.Review('source PR is not merged')
    if source['base']['repo']['full_name'] != api.meta:
        raise p.Review('unexpected source repository')
    source_line, candidate = source['base']['ref'], source['merge_commit_sha']
    line = p.destination_line(source_line, config)
    # A layer line with no distro line (e.g. a retired vertex/1.0.x) has
    # nowhere to propagate to: skip it, do not record it for human review.
    if subprocess.run(['git', '-C', str(distro), 'rev-parse', '--verify', '--quiet',
                       'refs/remotes/origin/' + line], capture_output=True).returncode:
        raise p.Ineligible(f'no ds-tegra-distro line {line}')
    base = p.git(distro, 'rev-parse', 'refs/remotes/origin/' + line)
    p.route(line, p.show(distro, base, 'kas/include/base.yml'), config, source_line)
    tip = p.git(meta, 'rev-parse', 'refs/remotes/origin/' + source_line)
    p.ancestor(meta, candidate, tip)
    old = p.lock_pin(p.show(distro, base, p.LOCKFILE))
    if old == candidate:
        return None
    # Ignore already incorporated source merges, including descendants.
    r = subprocess.run(['git', '-C', str(meta), 'merge-base', '--is-ancestor', candidate, old],
                       capture_output=True)
    if r.returncode == 0:
        return None
    changes = p.classify_range(meta, old, candidate, config)
    if not changes:
        raise p.Ineligible('no net source SHA changes')
    # Check each original PR commit too: squash/rebase merges must not hide
    # functional changes later reverted within the source PR.
    commits = api.items(f'repos/{api.meta}/pulls/{source["number"]}/commits?per_page=100')
    if not commits or len(commits) >= 250:
        raise p.Review('source PR commit list missing or possibly truncated')
    for commit in commits:
        sha = commit['sha']
        # PR heads from forks may not be fetched by the normal clone.
        if source['head']['repo'] is None or source['head']['repo']['full_name'] != api.meta:
            raise p.Review('fork/deleted source branch requires human review')
        parents = commit['parents']
        if not parents:
            raise p.Review('source PR contains a root commit')
        p.classify_range(meta, parents[0]['sha'], sha, config)
    text = p.show(meta, candidate, config['pin_file'])
    for name, change in changes.items():
        mapping = config['sources'][name]
        repo = mapping['repo']
        if '/' not in repo:
            repo = api.repo.split('/')[0] + '/' + repo
        branch = p.source_branch(text, mapping)
        comparison = api.call(f'repos/{repo}/compare/{change["new"]}...{quote(branch, safe="")}')
        if comparison['status'] not in ('ahead', 'identical'):
            raise p.Review(f'{name} is not on its configured branch {branch}')
        commit = api.call(f'repos/{repo}/commits/{change["new"]}')
        if commit['sha'] != change['new']:
            raise p.Review('source SHA did not resolve exactly')
    return dict(source_pr=source['number'], line=line, source_line=source_line, candidate=candidate,
                old=old, base=base, changes=changes)


def provenance(pr, data, api):
    if pr['user']['login'] != api.login or pr['head']['repo']['full_name'] != api.repo:
        raise p.Review('unexpected PR owner or repository')
    if pr['head']['ref'] != data['branch'] or pr['base']['ref'] != data['line']:
        raise p.Review('PR branch or destination changed')
    if pr['head']['sha'] != data['head']:
        raise p.Review('PR head changed outside the bot')
    if pr['base']['sha'] != data['base']:
        raise p.Review('destination base moved; new validation is required')
    # PR base.sha can remain at its creation snapshot after the target moves.
    # Use canonical Git refs, also covering lagging PR head metadata.
    if api.branch_sha(data['line']) != data['base']:
        raise p.Review('destination base moved; new validation is required')
    if api.branch_sha(data['branch']) != data['head']:
        raise p.Review('PR head changed outside the bot')
    if pr.get('draft') or pr.get('state') != 'open':
        raise p.Review('PR is not open and ready')
    if any(x.get('login', '').lower().startswith('copilot') for x in pr.get('assignees', [])):
        raise p.Review('Copilot assigned to propagation PR; unassign and review manually')


def verify_merge(api, data, current):
    """GitHub can regenerate a test merge by changing its timestamp only.

    The built SHA remains immutable evidence. A regenerated ref qualifies
    only if both ordered parents and the entire Git tree match exactly.
    """
    built = data.get('merge')
    if not p.SHA.fullmatch(built or '') or not p.SHA.fullmatch(current or ''):
        raise p.Review('missing verifiable merge commit')
    if built == current:
        return
    expected = [data['base'], data['head']]
    commits = [api.call(f'repos/{api.repo}/git/commits/{sha}') for sha in (built, current)]
    if any([parent['sha'] for parent in commit['parents']] != expected for commit in commits):
        raise p.Review('regenerated merge has different parents')
    if commits[0]['tree']['sha'] != commits[1]['tree']['sha']:
        raise p.Review('regenerated merge has different content')
    data['verified_merge'] = current


def wait_for_mergeability(api, number):
    pr = api.pr(number)
    for _ in range(MERGEABLE_POLLS):
        if pr.get('mergeable') is not None:
            break
        time.sleep(MERGEABLE_DELAY)
        pr = api.pr(number)
    return pr


def prepare(api, mode, number, output):
    if mode == 'disabled':
        return
    config = p.policy()
    sources = ([api.pr(number, api.meta)] if number else
               api.items(f'repos/{api.meta}/pulls?state=closed&sort=updated&direction=desc&per_page=100'))
    sources = sorted((s for s in sources if s.get('merged_at') and
                      p.parse_line(p.destination_line(s['base']['ref'], config), config['products'])['kind'] != 'none'),
                     key=lambda s: s['merged_at'])
    with tempfile.TemporaryDirectory(prefix='pin-bot-') as tmp:
        distro = clone(api.repo, Path(tmp) / 'distro')
        meta = clone(api.meta, Path(tmp) / 'meta')
        for source in sources:
            pr = None
            try:
                if api.flagged(api.meta, source['number'], source=True):
                    continue
                data = validate_source(api, source, distro, meta, config)
                if data is None:
                    continue
                data['branch'] = f'{PREFIX}{data["line"]}/{data["source_pr"]}'
                existing = api.items(f'repos/{api.repo}/pulls?state=all&head=' +
                                     quote(api.repo.split('/')[0] + ':' + data['branch'], safe='') + '&per_page=100')
                if existing:
                    if len(existing) != 1 or existing[0]['state'] != 'open':
                        continue  # A manually closed PR is never reopened.
                    pr = api.pr(existing[0]['number'])
                    check, stored = api.record(pr)
                    # A moving base is safe to restart only before any PR
                    # exists. Existing candidates stop for review, never rebase.
                    if stored['base'] != data['base'] or stored['candidate'] != data['candidate']:
                        raise p.Review('prepared candidate became stale')
                    data = stored
                    provenance(pr, data, api)
                    if api.flagged(api.repo, pr['number']):
                        continue
                elif mode == 'audit':
                    print(json.dumps(dict(result='eligible', **data)))
                    continue
                else:
                    api.ensure_review_anchor()
                    old_text = p.show(distro, data['base'], p.LOCKFILE)
                    blob = api.call(f'repos/{api.repo}/git/blobs', 'POST', {
                        'content': p.replace_lock(old_text, data['old'], data['candidate']), 'encoding': 'utf-8'})
                    base_commit = api.call(f'repos/{api.repo}/git/commits/{data["base"]}')
                    tree = api.call(f'repos/{api.repo}/git/trees', 'POST', {
                        'base_tree': base_commit['tree']['sha'], 'tree': [{'path': p.LOCKFILE, 'mode': '100644',
                                                          'type': 'blob', 'sha': blob['sha']}]})
                    commit = api.call(f'repos/{api.repo}/git/commits', 'POST', {
                        'message': f'kas: propagate meta-dsensos #{data["source_pr"]} pin bumps',
                        'tree': tree['sha'], 'parents': [data['base']]})
                    data['head'] = commit['sha']
                    # Create provenance BEFORE exposing a PR to other workflows.
                    check = api.call(f'repos/{api.repo}/check-runs', 'POST', {
                        'name': CHECK, 'head_sha': data['head'], 'status': 'in_progress',
                        'external_id': f'pin-propagation/v1/{data["source_pr"]}',
                        'output': {'title': 'Prepared; validation pending',
                                   'summary': 'Awaiting all four image builds', 'text': json.dumps(data)}})
                    api.call(f'repos/{api.repo}/git/refs', 'POST', {
                        'ref': 'refs/heads/' + data['branch'], 'sha': data['head']})
                    pr = api.call(f'repos/{api.repo}/pulls', 'POST', {
                        'title': f'kas: propagate meta-dsensos #{data["source_pr"]} ({data["line"]})',
                        'head': data['branch'], 'base': data['line'],
                        'body': f'Source: https://github.com/{api.meta}/pull/{data["source_pr"]}\n\n'
                                f'Layer: `{data["old"]}` → `{data["candidate"]}`\n\n'
                                'SHA-only policy passed. Four RC-flavor builds and safety checks are required. '
                                'Do not enable Watch and fix. Conflicts and external edits require human review.\n\n'
                                '```json\n' + json.dumps(data['changes'], indent=2) + '\n```'})
                if mode == 'audit':
                    continue
                # GitHub computes refs/pull/N/merge asynchronously. Poll briefly;
                # a later run retries while still UNKNOWN, never inferring clean.
                pr = wait_for_mergeability(api, pr['number'])
                if pr.get('mergeable') is None:
                    print(f'PR #{pr["number"]}: waiting for mergeability')
                    return
                if pr['mergeable'] is not True:
                    raise p.Review('merge conflict')
                provenance(pr, data, api)
                data['merge'] = pr['merge_commit_sha']
                if not p.SHA.fullmatch(data['merge'] or ''):
                    raise p.Review('missing proposed merge SHA')
                api.update(check, 'Building four RC flavors', data)
                matrix = json.loads((p.ROOT / 'release/matrix.json').read_text())['products']['vertex']['flavors']
                if len(matrix) != 4 or len(set(matrix)) != 4:
                    raise p.Review('Vertex RC matrix must contain exactly four distinct flavors')
                pending = p.show(distro, data['base'], 'versions/vertex').strip()
                if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', pending):
                    raise p.Review('invalid pending version')
                result = dict(pr=pr['number'], base=data['base'], head=data['head'],
                              merge=data['merge'], candidate=data['candidate'], line=data['line'],
                              pending=pending, version=f'{pending}-pin.{pr["number"]}.github-{data["head"][:7]}',
                              matrix=json.dumps({'include': [{'flavor': f} for f in matrix]}))
                if output:
                    with open(output, 'a') as f:
                        for key, value in result.items():
                            f.write(f'{key}={value}\n')
                print(json.dumps(result))
                return
            except p.Ineligible as e:
                print(f'Source #{source["number"]}: ineligible: {e}')
            except FAILURES as e:
                print(f'Source #{source["number"]}: needs-review: {e}')
                if mode != 'audit':
                    if pr:
                        api.block(pr, str(e))
                    api.flag(api.meta, source['number'], str(e), source=True)


def safety(api, number):
    pr = api.pr(number)
    if pr['head']['ref'].startswith(PREFIX) or pr['user']['login'] == api.login:
        try:
            check, data = api.record(pr)
            provenance(pr, data, api)
            if pr.get('mergeable') is False:
                raise p.Review('merge conflict')
            if api.flagged(api.repo, number):
                raise p.Review('persistent review block')
            # No success here: only finish may pass an automation candidate.
        except (p.Review, KeyError, ValueError) as e:
            api.block(pr, str(e))
            raise
    else:
        api.call(f'repos/{api.repo}/check-runs', 'POST', {
            'name': CHECK, 'head_sha': pr['head']['sha'], 'status': 'completed', 'conclusion': 'success',
            'output': {'title': 'Ordinary PR', 'summary': 'Not eligible for unattended pin-bot merging; normal review rules apply.'}})


def finish(api, number, mode, build_ok, merge_sha):
    pr = api.pr(number)
    try:
        check, data = api.record(pr)
        provenance(pr, data, api)
        if api.flagged(api.repo, number) or api.flagged(api.meta, data['source_pr'], source=True):
            raise p.Review('persistent review block')
        if not build_ok or data.get('merge') != merge_sha:
            raise p.Review('four-build/artifact gate failed or wrong merge SHA')
        if pr.get('mergeable') is not True:
            raise p.Review('merge result changed or cannot be verified')
        verify_merge(api, data, pr.get('merge_commit_sha'))
        commits = api.items(f'repos/{api.repo}/pulls/{number}/commits?per_page=100')
        if len(commits) != 1 or commits[0]['sha'] != data['head'] or [x['sha'] for x in commits[0]['parents']] != [data['base']]:
            raise p.Review('unexpected commit ancestry')
        files = api.items(f'repos/{api.repo}/pulls/{number}/files?per_page=100')
        with tempfile.TemporaryDirectory(prefix='pin-bot-finish-') as tmp:
            distro = clone(api.repo, Path(tmp) / 'distro')
            meta = clone(api.meta, Path(tmp) / 'meta')
            current = validate_source(api, api.pr(data['source_pr'], api.meta), distro, meta, p.policy())
            if current is None or any(current[k] != data[k] for k in ('base', 'candidate', 'old', 'changes', 'line', 'source_line')):
                raise p.Review('source or destination evidence changed')
            p.verify_pr_delta(p.show(distro, data['base'], p.LOCKFILE),
                              p.show(distro, data['head'], p.LOCKFILE), data['old'], data['candidate'],
                              [x['filename'] for x in files])
        # Actions associates pull_request check-runs with the PR HEAD, even
        # when checkout builds refs/pull/N/merge. GitHub's strict checks and
        # the unchanged-base/exact-merge assertions above bind that result
        # to this candidate. Looking up check-runs on merge_sha finds none.
        checks = api.checks(data['head'])
        host = [c for c in checks if c['name'] == 'pr-checks' and c['app']['id'] == 15368]
        if not host or max(host, key=lambda c: c['id'])['conclusion'] != 'success':
            raise p.Review('pr-checks did not pass on the candidate head with its unchanged base')
        api.update(check, 'All validation passed', data, 'success')
        if mode != 'merge':
            print('Validated; production merging is disabled')
            return
        # Re-read the kill switch, not the value captured hours ago by prepare.
        enabled = api.call(f'repos/{api.repo}/actions/variables/DS_PIN_BOT_MODE')['value']
        if enabled != 'merge':
            raise p.Review('merge kill switch is off')
        provenance(api.pr(number), data, api)
        result = api.call(f'repos/{api.repo}/pulls/{number}/merge', 'PUT', {
            'sha': data['head'], 'merge_method': 'merge'})
        if not result.get('merged'):
            raise p.Review('GitHub refused the merge')
        print(f'Merged #{number}')
    # A bot PR exists: evidence that no longer verifies, or a source that is
    # no longer a candidate, blocks it rather than ending the run unmarked.
    except FAILURES + (release_git.Error, p.Ineligible) as e:
        api.block(pr, str(e))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'safety', 'finish'])
    parser.add_argument('--source-pr', type=int)
    parser.add_argument('--pr', type=int)
    parser.add_argument('--output')
    parser.add_argument('--build-ok', action='store_true')
    parser.add_argument('--merge-sha', default='')
    args = parser.parse_args()
    mode = os.environ.get('DS_PIN_BOT_MODE', 'disabled')
    if mode not in ('disabled', 'audit', 'create', 'merge'):
        raise p.Review('invalid DS_PIN_BOT_MODE')
    if args.command == 'prepare' and mode == 'disabled':
        return
    api = API(os.environ.get('GITHUB_REPOSITORY'), os.environ.get('DS_PIN_BOT_APP_ID'),
              os.environ.get('DS_PIN_BOT_APP_SLUG'))
    if args.command == 'prepare':
        prepare(api, mode, args.source_pr, args.output)
    elif args.command == 'safety':
        safety(api, args.pr)
    else:
        finish(api, args.pr, mode, args.build_ok, args.merge_sha)


if __name__ == '__main__':
    try:
        main()
    except FAILURES + (release_git.Error, p.Ineligible) as error:
        print(f'pin-bot: {error}', file=sys.stderr)
        sys.exit(1)
