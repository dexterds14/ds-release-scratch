#!/usr/bin/env python3
"""Rebuilds the merge-rule fixtures from real history (ENG-2234). Not run by the suite.

    build_rule_fixtures.py --distro DIR --meta DIR --portal DIR --ds DIR [--out DIR]

Each argument is a clone that holds the commits below; all are only read (git
clone --shared into a temp dir). For each merge-up conflict PR listed it redoes
the merge the bot attempted (the parent line as it was, the child tip), runs
merge_rules.py on it with every rule acting, checks the result against what
plan section 9 expects, and writes fixtures/merge-up/<case>/:

    <n>.base, <n>.parent, <n>.child    the three versions of conflicted file n (no .base: add/add)
    <n>.landed                         the file in the commit that resolved the PR
    rule.json                          the PR, the shas, per file the path and what the rules do

and fixtures/merge-up/rule-cases.json, the list of cases. The tests build the
conflict from the three versions with a real git merge, so no conflict marker
is committed here. The compare answers the pins cases need are not real API
answers: where the rule asks (same branch on both lines) the case names them.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, SCRIPTS)
import merge_rules  # noqa: E402

PIN_FILE = 'conf/distro/include/ds-src-pins.inc'
PINS = {'pin_file': PIN_FILE, 'pairs': [
    {'rev': 'DSREV', 'branch': 'DS_BRANCH', 'repo': 'ds', 'acts': False},
    {'rev': 'SENSOR_PORTAL_REV', 'branch': 'SENSOR_PORTAL_BRANCH', 'repo': 'sensor-portal', 'acts': True}]}
NEVER_AUTO = ('.github/workflows/', 'ds-sensor-portal.sudoers')
# case: (repo, PR, parent, child, the commit with the human result, kind, {path: expectation})
# expectation: outcome, then for a file the rules resolve whether the result is the human's file
# byte for byte, for a refusal the reason (a key of merge_rules.REFUSALS, per conflict),
# for a pins file the rule's code.
CASES = [
    ('meta-dsensos', 176, '92200c092', 'ad1d4d6b6', 'd1b9132af', 'splice', {
        'recipes-ds-app/ds-sdr/ds-sdr_git.bb': {'outcome': 'resolved', 'equals_landed': True}}),
    ('meta-dsensos', 182, '259724902', 'de3fdff23', 'b6d4cdbc2', 'splice', {
        'recipes-ds-app/ds-ais-publisher/ds-ais-publisher_git.bb': {'outcome': 'resolved', 'equals_landed': False},
        'recipes-ds-app/ds-gps-publisher/ds-gps-publisher_git.bb': {'outcome': 'resolved', 'equals_landed': False}}),
    ('sensor-portal', 328, '7eda79528', 'f044d3127', '98257237', 'splice', {
        '.ai/architecture.md': {'outcome': 'resolved', 'equals_landed': True}}),
    ('ds-tegra-distro', 137, 'cbee77901', 'f8f3a391b', 'f808ba3af', 'splice', {
        '.github/workflows/release-promote.yml': {'outcome': 'never_auto', 'candidate_equals_landed': True},
        '.github/workflows/release-rc-build.yml': {'outcome': 'never_auto', 'candidate_equals_landed': True}}),
    ('sensor-portal', 297, 'a3119fea8', '38a8b445e', '559fdd0c', 'refusal', {
        'ds-sensor-portal.sudoers': {'outcome': 'never_auto', 'reasons': ['overlap']},
        'templates/sidebar.html': {'outcome': 'refused', 'refused': 'hunks'}}),
    ('sensor-portal', 332, '7eda79528', '90d649474', '072c1b38', 'refusal', {
        '.ai/release-lines.md': {'outcome': 'unresolved', 'reasons': ['overlap', 'overlap']}}),
    ('ds', 616, '1db062927', 'ce4d628e4', '3991a6c69', 'refusal', {
        '.ai/release-lines.md': {'outcome': 'unresolved', 'reasons': ['overlap']}}),
    ('meta-dsensos', 185, 'ce479bed0', '7560fa0e8', 'dd65e1d5e', 'refusal', {
        '.ai/ds-app-class.md': {'outcome': 'refused', 'refused': 'add-add'}}),
    ('meta-dsensos', 177, 'ad1d4d6b6', '5d2706855', 'efc119133', 'pins', {
        PIN_FILE: {'outcome': 'resolved', 'pins': 'keep-parent', 'variable': 'SENSOR_PORTAL_REV', 'equals_landed': True}}),
    ('meta-dsensos', 157, '946507029', 'c2334b4ee', '1b4d625ee', 'pins', {
        PIN_FILE: {'outcome': 'unresolved', 'pins': 'keep-parent', 'variable': 'DSREV', 'equals_landed': False}}),
    ('meta-dsensos', 166, '946507029', '99db05476', 'c8fd36bb', 'pins', {
        PIN_FILE: {'outcome': 'unresolved', 'pins': 'keep-parent', 'variable': 'DSREV', 'equals_landed': False}}),
]


def g(repo, *args, ok=False):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True)
    if r.returncode and not ok:
        raise SystemExit(f'git -C {repo} {" ".join(args)}: {r.stderr.decode().strip()}')
    return r.stdout if not ok else (r.returncode, r.stdout)


def never_auto(path):
    return next((p for p in NEVER_AUTO if path.startswith(p)), None)


def no_compare(repo, base, head):
    raise SystemExit(f'the pins rule asked the compare API for {repo} {base}...{head}; add the answer to the case')


def build(repos, out, case):
    repo, pr, parent, child, landed, kind, expect = case
    src = repos[repo]
    parent, child, landed = (g(src, 'rev-parse', f'{x}^{{commit}}').decode().strip() for x in (parent, child, landed))
    bases = g(src, 'merge-base', '--all', parent, child).decode().split()
    name = f'{repo}-{pr}'
    with tempfile.TemporaryDirectory() as tmp:
        wt = os.path.join(tmp, 'wt')
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout', src, wt], check=True)
        g(wt, 'checkout', '-q', '--detach', parent)
        subprocess.run(['git', '-C', wt, '-c', 'user.name=fixture', '-c', 'user.email=fixture@invalid', '-c', 'merge.conflictStyle=zdiff3',
                        'merge', '--no-ff', '--no-commit', child], capture_output=True)
        conflicted = merge_rules.unmerged_paths(wt)
        missing = sorted(set(expect) - set(conflicted))
        if missing:
            raise SystemExit(f'{name}: not conflicted: {missing}')
        marked = {}
        for path in expect:
            with open(os.path.join(wt, path), 'rb') as f:
                marked[path] = f.read()
        stages = {path: merge_rules.stages(wt, path) for path in expect}
        report = merge_rules.resolve_conflicts(wt, {'splice': 'act', 'pins': 'act'}, list(expect), never_auto=never_auto,
                                               pins=PINS if repo == 'meta-dsensos' else None, compare=no_compare)
        files = []
        os.makedirs(os.path.join(out, name), exist_ok=True)
        for n, (path, want) in enumerate(expect.items(), 1):
            f = [x for x in report['files'] if x['path'] == path][0]
            human = g(src, 'show', f'{landed}:{path}')
            with open(os.path.join(wt, path), 'rb') as fh:
                result = fh.read()
            got = {'outcome': f['outcome']}
            if 'equals_landed' in want:
                got['equals_landed'] = result == human
            if 'candidate_equals_landed' in want:
                r = subprocess.run(['git', '-C', wt, 'apply', '-'], input=report['candidates'][path].encode(), capture_output=True)
                with open(os.path.join(wt, path), 'rb') as fh:
                    got['candidate_equals_landed'] = r.returncode == 0 and fh.read() == human
            if 'reasons' in want:
                got['reasons'] = [k for i in f['items'] for k, text in merge_rules.REFUSALS.items() if text == i['reason']]
            if 'refused' in want:
                # a reason may end with its own detail in brackets
                got['refused'] = next(k for k, text in merge_rules.REFUSALS.items() if f['refused'] == text or f['refused'].startswith(text + ' ('))
            if 'pins' in want:
                got['pins'], got['variable'] = f['pins'][0]['code'], f['pins'][0]['variable']
            if got != want:
                raise SystemExit(f'{name} {path}: measured {got}, expected {want}')
            for side, stage in (('base', 1), ('parent', 2), ('child', 3)):
                if stage in stages[path]:
                    with open(os.path.join(out, name, f'{n}.{side}'), 'wb') as fh:
                        fh.write(stages[path][stage][1])
            with open(os.path.join(out, name, f'{n}.landed'), 'wb') as fh:
                fh.write(human)
            files.append(dict({'n': n, 'path': path, 'regions': f['regions'],
                               'git_merge_hunks': marked[path].count(b'\n>>>>>>> ')}, **want))
            print(f'{name}: {path}: {got}')
    doc = {'pr': f'{repo} #{pr}', 'kind': kind, 'parent_tip': parent, 'child_tip': child, 'merge_bases': bases, 'landed': landed,
           'pins': repo == 'meta-dsensos', 'never_auto_prefixes': list(NEVER_AUTO), 'conflicted': conflicted, 'files': files}
    with open(os.path.join(out, name, 'rule.json'), 'w') as f:
        json.dump(doc, f, indent=1)
        f.write('\n')
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('distro', 'meta', 'portal', 'ds'):
        ap.add_argument(f'--{name}', required=True)
    ap.add_argument('--out', default=HERE)
    args = ap.parse_args()
    repos = {'ds-tegra-distro': args.distro, 'meta-dsensos': args.meta, 'sensor-portal': args.portal, 'ds': args.ds}
    names = [build({k: os.path.abspath(v) for k, v in repos.items()}, args.out, case) for case in CASES]
    with open(os.path.join(args.out, 'rule-cases.json'), 'w') as f:
        json.dump(sorted(names), f, indent=1)
        f.write('\n')
    print(f'=== {len(names)} cases written to {args.out}')


if __name__ == '__main__':
    main()
