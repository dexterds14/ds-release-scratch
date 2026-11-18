#!/usr/bin/env python3
"""Mint an explicitly scoped finish token, including Variables:read.

create-github-app-token v3 lacks a Variables input. Do not inherit all App
permissions to work around that omission. No private key/JWT is logged.
"""
import base64
import json
import os
import re
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

PERMISSIONS = {'contents': 'write', 'pull_requests': 'write', 'checks': 'write', 'actions_variables': 'read'}


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=')


def request_token(jwt, installation, repo):
    request = urllib.request.Request(
        f'https://api.github.com/app/installations/{installation}/access_tokens',
        data=json.dumps({'repositories': [repo], 'permissions': PERMISSIONS}).encode(),
        headers={'Authorization': 'Bearer ' + jwt, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'User-Agent': 'ds-pin-propagation',
                 'X-GitHub-Api-Version': '2022-11-28'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        body = json.loads(error.read())
        # Only API validation messages are exposed. Never log JWTs, keys or
        # successful responses containing installation tokens.
        detail = {k: body[k] for k in ('message', 'errors') if k in body}
        raise RuntimeError(f'GitHub rejected the scoped token request ({error.code}): {detail}') from None


def mint():
    now = int(time.time())
    header = encode(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    payload = encode(json.dumps({'iat': now - 60, 'exp': now + 540,
                                 'iss': os.environ['APP_CLIENT_ID']}).encode())
    message = header + b'.' + payload
    with tempfile.TemporaryDirectory(prefix='pin-app-') as tmp:
        key = Path(tmp) / 'key.pem'
        key.touch(mode=0o600)
        key.write_text(os.environ['APP_PRIVATE_KEY'])
        signature = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(key)], input=message)
    jwt = (message + b'.' + encode(signature)).decode()
    repo = os.environ.get('APP_TOKEN_REPOSITORY', os.environ['GITHUB_REPOSITORY'].split('/')[1])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', repo):
        raise ValueError('expected one repository name within this App installation')
    installation = int(os.environ['APP_INSTALLATION_ID'])
    result = request_token(jwt, installation, repo)
    if result['permissions'] != dict(PERMISSIONS, metadata='read'):
        # API may omit implicit metadata from its permissions response.
        if result['permissions'] != PERMISSIONS:
            raise RuntimeError('token permissions differ from the requested minimum')
    if [r['name'] for r in result.get('repositories', [])] != [repo]:
        raise RuntimeError('token repository scope differs from the requested repository')
    token = result['token']
    print('::add-mask::' + token)
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('token=' + token + '\n')


if __name__ == '__main__':
    mint()
