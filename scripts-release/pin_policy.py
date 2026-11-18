#!/usr/bin/env python3
"""Fail-closed, read-only pin classifier. Never evaluates BitBake or candidate code."""
import fnmatch
import json
from pathlib import Path
import re
import subprocess

from release_git import base_branch_get, lock_pin, LOCKFILE
from lines import parse_line

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r'[0-9a-f]{40}')
ASSIGN = re.compile(r'([A-Z][A-Z0-9_]*) \?= "([0-9a-f]{40})"\n?')


class Review(Exception):
    """Evidence is missing, ambiguous, or no longer current."""


class Ineligible(Exception):
    """A known non-pin change; do not create a downstream PR."""


def git(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
    if p.returncode:
        raise Review(p.stderr.strip() or 'git failed')
    return p.stdout.strip()


def show(root, ref, path):
    # Preserve final newlines: they are part of the policy, too.
    return subprocess.check_output(['git', '-C', str(root), 'show', f'{ref}:{path}']).decode()


def policy():
    return json.loads((ROOT / 'release/pin-propagation.json').read_text())


def ancestor(root, old, new):
    if not SHA.fullmatch(old) or not SHA.fullmatch(new):
        raise Review('expected full commit SHAs')
    git(root, 'merge-base', '--is-ancestor', old, new)


def assignments(text, names):
    result = {}
    for line in text.splitlines(keepends=True):
        m = ASSIGN.fullmatch(line)
        if m and m[1] in names:
            if m[1] in result:
                raise Review(f'duplicate pin {m[1]}')
            result[m[1]] = m[2]
    return result


def pin_changes(before, after, allowed):
    """Only exact existing assignments and comments directly above a changed pin.

    Compare executable lines byte-for-byte after replacing approved SHA values.
    Comments with a preceding continuation are never treated as inert comments.
    """
    old, new = assignments(before, allowed), assignments(after, allowed)
    if old.keys() != new.keys():
        raise Ineligible('pin added, deleted, or assignment syntax changed')
    changes = {k: {'old': old[k], 'new': new[k]} for k in old if old[k] != new[k]}

    def normalized(text):
        lines = text.splitlines(keepends=True)
        # Only the contiguous comment block immediately before a changed pin
        # is eligible for removal. Blank lines and other comments stay exact.
        removable = set()
        for i, line in enumerate(lines):
            m = ASSIGN.fullmatch(line)
            if m and m[1] in changes:
                j = i - 1
                while j >= 0 and lines[j].startswith('#'):
                    removable.add(j)
                    j -= 1
        out = []
        for i, line in enumerate(lines):
            if i in removable:
                if i and lines[i - 1].rstrip('\n').endswith('\\'):
                    raise Ineligible('comment or pin occurs in a continuation')
                continue
            m = ASSIGN.fullmatch(line)
            if m and m[1] in changes:
                if i and lines[i - 1].rstrip('\n').endswith('\\'):
                    raise Ineligible('pin occurs in a continuation')
                line = line.replace(m[2], '<PIN>')
            out.append(line)
        return ''.join(out)

    if normalized(before) != normalized(after):
        raise Ineligible('content other than approved SHA assignments/adjacent comments changed')
    return changes


def non_build(name, config):
    """A layer path no build reads (policy `non_build_paths`): a directory prefix
    ending in '/' at the layer root, or a pattern for top-level files only. A
    nested file such as recipes-x/files/notes.md is a build input: recipes ship
    files from their own directories."""
    for pattern in config.get('non_build_paths', []):
        if pattern.endswith('/'):
            if name.startswith(pattern):
                return True
        elif '/' not in name and fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def classify_range(root, old, new, config):
    ancestor(root, old, new)
    if old == new:
        return {}
    path, allowed = config['pin_file'], config['sources']

    def edge(a, b):
        raw = git(root, 'diff', '--raw', '--no-abbrev', a, b, '--')
        for line in raw.splitlines():
            meta, name = line.split('\t', 1)
            # Docs and AI notes cannot change an image: skip them, so a doc
            # commit does not pause the line. A rename lists both paths;
            # both must be non-build.
            if all(non_build(n, config) for n in name.split('\t')):
                continue
            parts = meta.split()
            if name != path or parts[0] != ':100644' or parts[1] != '100644' or parts[4] != 'M':
                raise Ineligible(f'non-pin file or mode change: {name}')
        return pin_changes(show(root, a, path), show(root, b, path), allowed)

    # Inspect ALL introduced commits, including second-parent work and edits
    # later reverted. Merge commits are compared to their first parent.
    for commit in git(root, 'rev-list', f'{old}..{new}').splitlines():
        parents = git(root, 'rev-list', '--parents', '-n', '1', commit).split()[1:]
        if not parents:
            raise Review('unexpected root commit')
        edge(parents[0], commit)
    return edge(old, new)


def source_branch(text, mapping):
    name = mapping.get('branch_variable')
    if not name:
        return mapping['default_branch']
    matches = re.findall(r'^' + re.escape(name) + r' \?= "([^"\n]+)"$', text, re.M)
    # Reject unfamiliar overrides instead of silently falling back.
    mentions = re.findall(r'^' + re.escape(name) + r'\b.*$', text, re.M)
    if len(matches) > 1 or len(mentions) != len(matches):
        raise Review(f'ambiguous branch assignment: {name}')
    branch = matches[0] if matches else mapping['default_branch']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch) or '..' in branch:
        raise Review('unsupported source branch syntax')
    return branch


def destination_line(source_line, config):
    """Explicit trusted branch mappings; production defaults to identical lines."""
    line = config.get('routes', {}).get(source_line, source_line)
    if not isinstance(line, str):
        raise Review('invalid configured destination line')
    return line


def route(line, base_text, config, source_line=None):
    if parse_line(line, config['products'])['kind'] == 'none':
        raise Review('unsupported product or release line')
    if base_branch_get(base_text) != (source_line or line):
        raise Review('source/destination layer branch mismatch')


def replace_lock(text, expected, candidate):
    if lock_pin(text) != expected or not SHA.fullmatch(candidate):
        raise Review('unexpected lock pin')
    # Restrict replacement to the unambiguous layer stanza, not matching SHAs
    # in other layers or comments.
    pattern = r'(?m)^(    meta-dsensos:\n      commit: )' + re.escape(expected) + r'$'
    result, count = re.subn(pattern, lambda m: m[1] + candidate, text)
    if count != 1 or lock_pin(result) != candidate:
        raise Review('unsupported lockfile layout')
    return result


def verify_pr_delta(before, after, old, candidate, files):
    if files != [LOCKFILE] or after != replace_lock(before, old, candidate):
        raise Review('PR is not exactly the intended layer pin replacement')
