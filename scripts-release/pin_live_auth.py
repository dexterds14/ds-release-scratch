#!/usr/bin/env python3
"""Read-only live API validation. Called by the isolated integration workflow."""
import base64
import json
import os
from pathlib import Path

from pin_bot import API
import pin_policy as p


def main():
    api = API(os.environ['GITHUB_REPOSITORY'], os.environ['DS_PIN_BOT_APP_ID'],
              os.environ['DS_PIN_BOT_APP_SLUG'])
    report = {'run_url': f'https://github.com/{api.repo}/actions/runs/{os.environ["GITHUB_RUN_ID"]}',
              'checks': [], 'failures': [], 'manual_review_sources': []}

    def check(name, test):
        try:
            detail = test()
            report['checks'].append({'name': name, 'result': 'pass', 'detail': detail})
            print('PASS: ' + name)
        except Exception as error:
            report['failures'].append({'name': name, 'reason': str(error)})
            print('FAIL: ' + name + ': ' + str(error))

    def mode():
        value = api.call(f'repos/{api.repo}/actions/variables/DS_PIN_BOT_MODE')['value']
        if value != 'disabled':
            raise ValueError('production merging must remain disabled')
        return value

    check('live kill switch read', mode)
    for repo in (api.repo, api.meta, api.repo.split('/')[0] + '/sensor-portal', api.repo.split('/')[0] + '/ds'):
        check('private repository read: ' + repo,
              lambda repo=repo: api.call(f'repos/{repo}')['full_name'])
    config = p.policy()
    content = api.call(f'repos/{api.meta}/contents/{config["pin_file"]}?ref=vertex/main')
    text = base64.b64decode(content['content']).decode()
    pins = p.assignments(text, config['sources'])
    for name, sha in pins.items():
        mapping = config['sources'][name]
        repo = mapping['repo']
        if '/' not in repo:
            repo = api.repo.split('/')[0] + '/' + repo
        if name in ('DSREV', 'SENSOR_PORTAL_REV'):
            check('source commit read: ' + name,
                  lambda repo=repo, sha=sha: api.call(f'repos/{repo}/commits/{sha}')['sha'])
        else:
            try:
                api.call(f'repos/{repo}/commits/{sha}')
                report['checks'].append({'name': 'source commit read: ' + name, 'result': 'pass'})
                print('PASS: source commit read: ' + name)
            except p.Review as error:
                # Extra source access is optional. The real classifier only
                # verifies changed pins, and propagates this exception into
                # needs-review rather than guessing or widening permissions.
                report['manual_review_sources'].append({'pin': name, 'reason': str(error)})
                print('PASS: unavailable source requires human review: ' + name)
    Path('pin-live-auth.json').write_text(json.dumps(report, indent=2) + '\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
        summary.write('## Live pin-bot authentication\n\n')
        for item in report['checks']:
            summary.write('- PASS: ' + item['name'] + '\n')
        for item in report['failures']:
            summary.write('- FAIL: ' + item['name'] + ': ' + item['reason'] + '\n')
        for item in report['manual_review_sources']:
            summary.write('- MANUAL REVIEW for updates to ' + item['pin'] + ' (source access unavailable)\n')
    if report['failures']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
