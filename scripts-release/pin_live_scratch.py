#!/usr/bin/env python3
"""Synthetic live acceptance suite. Writes ONLY to the named org scratch repo.

This simulates build outcomes to exercise GitHub/PR control flow. Real four-
image build validation runs separately; this is not image-build evidence.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import pin_bot as b
import pin_policy as p

FIXTURE = Path(__file__).parent / 'tests/pin-live-fixture.json'
DEST = 'DistributedSpectrum/ds-scratch-test-repo'
SOURCE = DEST
LINE = 'vertex/9000.1.x'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['suite', 'kill', 'conflict', 'repair', 'events', 'scope'])
    args = parser.parse_args()
    f = json.loads(FIXTURE.read_text())
    if args.phase == 'events':
        f.update(source_line=f['post_source_line'], source_pr=f['post_source_pr'], candidate=f['post_candidate'])
    assert (f['destination'], f['source'], f['line']) == (DEST, SOURCE, LINE)
    api = b.API(DEST, os.environ['DS_PIN_BOT_APP_ID'], os.environ['DS_PIN_BOT_APP_SLUG'])
    api.meta = SOURCE
    api.ledger_branch = 'pin-bot/pin-validation-20261106/source-review'
    # A single scratch repository hosts two virtual histories. Filter only
    # the fixture's source branch when exercising a missed-event scan.
    original_items = api.items
    def items(path, key=None):
        result = original_items(path, key)
        if path.startswith(f'repos/{SOURCE}/pulls?state=closed&sort='):
            result = [pr for pr in result if pr['base']['ref'] == f['source_line']]
        return result
    api.items = items
    p.policy = lambda: dict(products=['vertex'], routes={f['source_line']: LINE},
                            pin_file='conf/distro/include/ds-src-pins.inc', sources={
                                'DSREV': {'repo': SOURCE, 'default_branch': f['code_line']},
                                'EXTERNAL_REV': {'repo': 'DistributedSpectrum/schemas', 'default_branch': 'main'}})
    report = dict(phase=args.phase, checks=[], fixture=f,
                  note='Synthetic control-flow test; not actual image-build evidence')

    def passed(name):
        report['checks'].append(name)
        print('PASS: ' + name, flush=True)

    def denied(name, call, expected=None):
        try:
            call()
        except p.Review as error:
            if expected:
                assert expected in str(error), (name, str(error))
            passed(name)
        else:
            raise AssertionError('Unexpectedly permitted: ' + name)

    def pulls():
        return api.items(f'repos/{DEST}/pulls?state=all&base={LINE}&per_page=100')

    def wait_pr(number):
        for _ in range(60):
            pr = api.pr(number)
            if pr.get('mergeable') is not None:
                return pr
            time.sleep(2)
        raise AssertionError('mergeability stayed unknown')

    def wait_head(number, expected):
        for _ in range(60):
            pr = api.pr(number)
            if pr['head']['sha'] == expected:
                time.sleep(1)
                if api.pr(number)['head']['sha'] == expected:
                    return
            time.sleep(2)
        raise AssertionError('PR metadata did not converge to expected head')

    def wait_host(number):
        for _ in range(100):
            pr = wait_pr(number)
            checks = [c for c in api.checks(pr['head']['sha'])
                      if c['name'] == 'pr-checks' and c['app']['id'] == 15368]
            if checks:
                current = max(checks, key=lambda c: c['id'])
                if current['status'] == 'completed':
                    assert current['conclusion'] == 'success', current
                    passed('Actions pr-checks passed for scratch PR #' + str(number))
                    return pr
            time.sleep(3)
        raise AssertionError('synthetic host workflow did not complete')

    def prepare(source_pr):
        output = Path('pin-scratch-prepare.txt')
        for _ in range(45):
            output.unlink(missing_ok=True)
            b.prepare(api, 'create', source_pr, str(output))
            if output.exists():
                values = dict(line.split('=', 1) for line in output.read_text().splitlines())
                return api.pr(int(values['pr']))
            time.sleep(2)
        raise AssertionError('candidate PR was not prepared')

    def clone_candidate(data, suffix):
        data = copy.deepcopy(data)
        parent = api.call(f'repos/{DEST}/git/commits/{data["head"]}')
        commit = api.call(f'repos/{DEST}/git/commits', 'POST', {
            'message': 'Synthetic isolated ' + suffix, 'tree': parent['tree']['sha'],
            'parents': [data['base']]})
        data.update(head=commit['sha'], branch=f'pin-bot/pin-validation-20261106/{suffix}')
        api.call(f'repos/{DEST}/check-runs', 'POST', {
            'name': b.CHECK, 'head_sha': data['head'], 'status': 'in_progress',
            'external_id': 'pin-propagation/v1/' + suffix,
            'output': {'title': 'Synthetic validation pending', 'summary': 'Scratch fixture only',
                       'text': json.dumps(data)}})
        api.call(f'repos/{DEST}/git/refs', 'POST', {'ref': 'refs/heads/' + data['branch'], 'sha': data['head']})
        pr = api.call(f'repos/{DEST}/pulls', 'POST', {
            'title': 'Synthetic ' + suffix, 'body': 'Synthetic isolated acceptance fixture only.',
            'head': data['branch'], 'base': LINE})
        pr = wait_pr(pr['number'])
        check, stored = api.record(pr)
        stored['merge'] = pr['merge_commit_sha']
        api.update(check, 'Synthetic validation pending', stored)
        return pr

    def find_phase_pr(suffix):
        found = [pr for pr in pulls() if pr['head']['ref'] == 'pin-bot/pin-validation-20261106/' + suffix]
        assert len(found) == 1
        return api.pr(found[0]['number'])

    try:
        rules = api.call(f'repos/{DEST}/rules/branches/{quote(LINE, safe="")}')
        required = [rule['parameters'] for rule in rules if rule['type'] == 'required_status_checks']
        assert any(params.get('strict_required_status_checks_policy') and
                   {'context': b.CHECK, 'integration_id': api.app_id} in params['required_status_checks']
                   for params in required), 'scratch safety rules are not installed; stop before writes'
        assert any({'context': 'pr-checks', 'integration_id': 15368} in params['required_status_checks']
                   for params in required), 'scratch host check is not mandatory'
        assert any(rule['type'] == 'pull_request' and
                   rule['parameters']['required_approving_review_count'] == 1 for rule in rules), \
            'scratch approval rule is not installed'
        if args.phase in ('suite', 'events'):
            assert api.call(f'repos/{DEST}/actions/variables/DS_PIN_BOT_MODE')['value'] == 'merge', \
                'scratch merge switch is disabled; stop before writes'
        if args.phase == 'suite':
            if f.get('ordinary_pr'):
                ordinary = api.pr(f['ordinary_pr'])
                assert ordinary['user']['login'] != api.login
                b.safety(api, ordinary['number'])
                checks = [c for c in api.checks(ordinary['head']['sha'])
                          if c['name'] == b.CHECK and c['app']['id'] == api.app_id]
                assert checks and max(checks, key=lambda c: c['id'])['conclusion'] == 'success'
                denied('ordinary PR lacks bot merge provenance', lambda: api.record(ordinary))
                passed('ordinary PR safety passes while normal review remains separate')
            existing = [x for x in pulls() if x['head']['ref'] == f"pin-bot/{LINE}/{f['source_pr']}"]
            if existing and existing[0].get('merged_at'):
                merged = api.pr(existing[0]['number'])
                assert merged['merged_by']['login'] == api.login
                passed('prior gated App merge remains applied; no review block was cleared')
            else:
                pr = prepare(f['source_pr'])
                first = pr['number']
                assert pr['user']['login'] == api.login
                passed('PR created by the dedicated App')
                assert prepare(f['source_pr'])['number'] == first
                passed('duplicate notification reuses PR')
                pr = wait_host(first)
                denied('mandatory pending safety check blocks direct merge', lambda: api.call(
                    f'repos/{DEST}/pulls/{first}/merge', 'PUT', {'sha': pr['head']['sha'], 'merge_method': 'merge'}))
                # An empty commit would still be a protected direct release push.
                base_commit = api.call(f'repos/{DEST}/git/commits/{pr["base"]["sha"]}')
                direct = api.call(f'repos/{DEST}/git/commits', 'POST', {
                    'message': 'Synthetic forbidden direct update', 'tree': base_commit['tree']['sha'],
                    'parents': [pr['base']['sha']]})
                denied('dedicated App cannot push directly to the protected test line', lambda: api.call(
                    f'repos/{DEST}/git/refs/heads/{LINE}', 'PATCH', {'sha': direct['sha']}))
                check, data = api.record(pr)
                b.finish(api, first, 'merge', True, data['merge'])
                assert api.pr(first)['merged'] is True
                passed('gated App PR merges without human approval in scratch only')
            count = len(pulls())
            b.prepare(api, 'create', f['source_pr'], None)
            assert len(pulls()) == count
            passed('already applied source merge creates no duplicate')

            existing = [x for x in pulls() if x['head']['ref'] == f"pin-bot/{LINE}/{f['second-pin']['pr']}"]
            if existing:
                pr = api.pr(existing[0]['number'])
                assert pr['state'] == 'open', 'do not reopen a closed acceptance fixture'
                records = []
                for commit in api.items(f"repos/{DEST}/pulls/{pr['number']}/commits?per_page=100"):
                    for check in api.checks(commit['sha']):
                        if (check['app']['id'] == api.app_id and check['name'] == b.CHECK
                            and check.get('external_id') == f"pin-propagation/v1/{f['second-pin']['pr']}"):
                            records.append(json.loads(check['output']['text']))
                assert len(records) == 1
                data = records[0]
                assert data['source_pr'] == f['second-pin']['pr'] and data['candidate'] == f['second-pin']['candidate']
            else:
                pr = prepare(f['second-pin']['pr'])
                _, data = api.record(pr)
            second = pr['number']
            original_head = data['head']
            if pr['head']['sha'] == original_head:
                base = api.call(f'repos/{DEST}/git/commits/{original_head}')
                tree = api.call(f'repos/{DEST}/git/trees', 'POST', {
                    'base_tree': base['tree']['sha'], 'tree': [{'path': 'UNEXPECTED-EDIT.txt', 'mode': '100644',
                                                              'type': 'blob', 'content': 'Synthetic external edit\n'}]})
                edit = api.call(f'repos/{DEST}/git/commits', 'POST', {
                    'message': 'Synthetic external edit fault injection', 'tree': tree['sha'], 'parents': [original_head]})
                api.call(f'repos/{DEST}/git/refs/heads/{data["branch"]}', 'PATCH', {'sha': edit['sha']})
                expected_head = edit['sha']
            else:
                expected_head = pr['head']['sha']
                injected = api.call(f'repos/{DEST}/git/commits/{expected_head}')
                assert [x['sha'] for x in injected['parents']] == [original_head]
            wait_head(second, expected_head)
            denied('unexpected branch edit blocks safety', lambda: b.safety(api, second))
            api.call(f'repos/{DEST}/git/refs/heads/{data["branch"]}', 'PATCH', {'sha': original_head, 'force': True})
            wait_head(second, original_head)
            denied('repair cannot clear persistent review block', lambda: b.safety(api, second))
            denied('repaired candidate cannot auto-merge', lambda: b.finish(api, second, 'merge', True, data['merge']))
            api.call(f'repos/{DEST}/pulls/{second}', 'PATCH', {'state': 'closed'})
            count = len(pulls())
            b.prepare(api, 'create', f['second-pin']['pr'], None)
            assert len(pulls()) == count and api.pr(second)['state'] == 'closed'
            passed('closed candidate is never reopened')
            for name in ('inaccessible-pin', 'functional-layer'):
                count = len(pulls())
                b.prepare(api, 'create', f[name]['pr'], None)
                assert len(pulls()) == count
                passed(name + ' produces no downstream PR')
            for suffix in ('kill-switch', 'conflict'):
                created = clone_candidate(data, suffix)
                report[suffix + '_pr'] = created['number']
            passed('independent kill-switch and conflict candidates prepared')
        elif args.phase == 'scope':
            denied('read-only Contents safety token cannot write Git trees', lambda: api.call(
                f'repos/{DEST}/git/trees', 'POST', {'tree': [{'path': 'scope-check.json', 'mode': '100644',
                    'type': 'blob', 'content': '{"synthetic":true}\n'}]}))
            api.pr_review(0, 'Synthetic minimal-token permanent-block proof')
            assert api.pr_review_blocked(0)
            passed('minimal safety token persists a permanent App-owned check marker')
            # Existing repaired/closed fixture remains blocked using the real
            # safety function, now without any Git write permission.
            old = find_phase_pr('conflict')
            denied('minimal safety token preserves the repaired candidate block',
                   lambda: b.safety(api, old['number']))
            assert api.pr_review_blocked(old['number'])
        elif args.phase == 'events':
            pr = prepare(None)
            passed('recovery scan finds the eligible source without a notification')
            assert prepare(f['source_pr'])['number'] == pr['number']
            passed('later notification reuses recovered PR')
            pr = wait_host(pr['number'])
            _, data = api.record(pr)
            b.finish(api, pr['number'], 'merge', True, data['merge'])
            merged = api.pr(pr['number'])
            assert merged['merged'] and merged['merged_by']['login'] == api.login
            report.update(postmerge_pr=pr['number'], postmerge_sha=merged['merge_commit_sha'])
            passed('App merge produced the commit used for downstream event receipts')
        elif args.phase == 'kill':
            pr = find_phase_pr('kill-switch')
            pr = wait_host(pr['number'])
            assert api.call(f'repos/{DEST}/actions/variables/DS_PIN_BOT_MODE')['value'] == 'disabled'
            check, data = api.record(pr)
            # Refresh synthetic build evidence to the currently unchanged base.
            data['merge'] = pr['merge_commit_sha']
            api.update(check, 'Synthetic builds passed', data)
            denied('live disabled kill switch prevents merge', lambda: b.finish(
                api, pr['number'], 'merge', True, data['merge']), expected='merge kill switch is off')
            assert api.pr(pr['number'])['merged'] is False
        elif args.phase == 'conflict':
            pr = find_phase_pr('conflict')
            pr = wait_pr(pr['number'])
            assert pr['mergeable'] is False, 'inject an actual scratch-base lock conflict first'
            denied('actual merge conflict blocks safety', lambda: b.safety(api, pr['number']))
            # Base may later be repaired by an admin; this block must persist.
            assert api.flagged(DEST, pr['number'])
            denied('conflict cannot be auto-merged', lambda: b.finish(
                api, pr['number'], 'merge', True, pr.get('merge_commit_sha') or ''))
            assert api.pr(pr['number'])['merged'] is False
        else:
            pr = find_phase_pr('conflict')
            pr = wait_pr(pr['number'])
            assert pr['mergeable'] is True, 'repair the synthetic base conflict first'
            assert api.flagged(DEST, pr['number'])
            denied('repaired conflict remains blocked for human review',
                   lambda: b.safety(api, pr['number']), expected='persistent human-review block')
            denied('repaired conflict still cannot auto-merge', lambda: b.finish(
                api, pr['number'], 'merge', True, pr['merge_commit_sha']), expected='persistent human-review block')
            assert api.pr(pr['number'])['merged'] is False
    finally:
        Path('pin-scratch-results.json').write_text(json.dumps(report, indent=2) + '\n')
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('## Synthetic scratch validation\n\n' + report['note'] + '\n\n')
            for name in report['checks']:
                summary.write('- PASS: ' + name + '\n')


if __name__ == '__main__':
    main()
