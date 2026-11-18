#!/usr/bin/env python3
"""Generate reviewable ruleset payloads; never changes GitHub settings."""
import argparse
import copy
import json
from pathlib import Path


def generate(current, app_id):
    if current['enforcement'] != 'active' or app_id <= 0:
        raise ValueError('need active current ruleset and a real dedicated App ID')
    if any(x.get('actor_id') == app_id for x in current['bypass_actors']):
        raise ValueError('dedicated App must not already bypass the existing ruleset')
    keys = ('name', 'target', 'enforcement', 'conditions', 'rules', 'bypass_actors')
    retained = {k: copy.deepcopy(current[k]) for k in keys}
    approvals = [r for r in retained['rules'] if r['type'] == 'pull_request']
    if len(approvals) != 1:
        raise ValueError('expected exactly one existing PR approval rule')
    retained['rules'] = [r for r in retained['rules'] if r['type'] != 'pull_request']
    base = {k: copy.deepcopy(current[k]) for k in ('target', 'enforcement', 'conditions', 'bypass_actors')}
    approval = dict(copy.deepcopy(base), name='release-lines-approval', rules=approvals)
    approval['bypass_actors'].append({'actor_id': app_id, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'})
    safety = dict(copy.deepcopy(base), name='release-lines-pin-safety', rules=[{
        'type': 'required_status_checks', 'parameters': {
            'strict_required_status_checks_policy': True, 'do_not_enforce_on_create': True,
            'required_status_checks': [{'context': 'pr-checks', 'integration_id': 15368},
                                       {'context': 'pin-propagation-safety', 'integration_id': app_id}]}}])
    heads = dict(target='branch', enforcement='active', name='pin-bot-owned-branches',
                 conditions={'ref_name': {'include': ['refs/heads/pin-bot/**'], 'exclude': []}},
                 rules=[{'type': t} for t in ('creation', 'update', 'deletion', 'non_fast_forward')],
                 bypass_actors=[{'actor_id': app_id, 'actor_type': 'Integration', 'bypass_mode': 'always'}])
    return {'existing-release-lines': retained, 'approval': approval, 'safety': safety, 'bot-heads': heads}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--current-json', required=True)
    parser.add_argument('--app-id', required=True, type=int)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    for name, payload in generate(json.loads(Path(args.current_json).read_text()), args.app_id).items():
        (output / (name + '.json')).write_text(json.dumps(payload, indent=2) + '\n')
