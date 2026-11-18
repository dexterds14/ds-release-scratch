#!/usr/bin/env python3
"""Verify every gated image used the intended locked meta-dsensos SHA."""
import json
import subprocess
import sys


def verify(evidence, expected, read):
    builds = evidence['builds']
    if len(builds) != 4 or len({b['flavor'] for b in builds}) != 4:
        raise ValueError('expected four distinct RC flavor builds')
    for build in builds:
        manifest = read(build['s3_prefix'] + '/manifest.json')
        pins = [c['commit'] for c in manifest['commit_hashes'] if c['name'] == 'meta-dsensos']
        if pins != [expected]:
            raise ValueError(f'{build["flavor"]}: wrong or missing meta-dsensos pin')


if __name__ == '__main__':
    import os
    bucket = os.environ.get('DS_ARTIFACTS_BUCKET', 'ds-yocto-artifacts')
    verify(json.load(open(sys.argv[1])), sys.argv[2], lambda key: json.loads(
        subprocess.check_output(['aws', 's3', 'cp', f's3://{bucket}/{key}', '-', '--no-progress'])))
