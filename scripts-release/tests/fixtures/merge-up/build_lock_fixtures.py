#!/usr/bin/env python3
"""Rebuilds the R-lock fixtures from real history (ENG-2234). Not run by the suite.

    build_lock_fixtures.py --distro <ds-tegra-distro clone> --meta <meta-dsensos clone> [--out DIR]

For each merge-up conflict PR below that included kas/include/base.lock.yml it
redoes the merge the bot attempted (the parent line as it was when the PR
merged, the child tip the PR was opened for) in a throwaway clone, runs
release_git.lock_rule against the real meta-dsensos history, and writes
fixtures/merge-up/<case>/:

    base.lock.yml, parent.lock.yml, child.lock.yml   the three sides of the conflict
    landed.lock.yml                                    what the PR's merge commit holds
    case.json                                          the shas, the measured checks, the outcome
                                                       per value of merge_up.lock_pin.exact

The tests build the conflict markers from the three sides at test time, so no
marker is committed here. The tree comparison (check 5) needs meta-dsensos
objects, which a fixture cannot carry: case.json records what was measured,
with the counts, and tests/replay_lock_history.sh measures it again.
Both clones are only read (git clone --shared into a temp dir).
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
import release_git  # noqa: E402

LOCK = release_git.LOCKFILE
# (PR, merge commit of the PR, child tip the PR was opened for, child line, parent line, the
# outcome plan 5.1 expects with merge_up.lock_pin.exact=push, the value the plan was written
# for: A is a verdict "exact" with no other conflict. Under the shipped exact=bot-pr
# (2026-11-18) the same verdict lands as B; case.json records both, as outcome_by_exact)
CASES = [
    (146, '5d806f6137c67180c56bd03597dc325414989854', '5891961a', 'vertex/1.2.x', 'vertex/1.x.x', 'A'),
    (153, '0570eef72ffed843fe951f51fd367caf60103101', '77add162', 'vertex/1.3.x', 'vertex/1.x.x', 'B'),
    (158, '4d6cb2e1a3e915e2bd822f72e35ccdbb0546ef2f', '8806e1ab', 'vertex/1.3.x', 'vertex/1.x.x', 'B'),
    (162, '00e1f85bf0da8090112e9d78a4fa0df7c42e23d1', '667f3cd9', 'vertex/1.x.x', 'vertex/main', 'A'),
    (170, '1a09939c65c91bdb457a3a7a804214ee180425f3', 'b8a66ae0', 'vertex/1.3.x', 'vertex/1.x.x', 'B'),
    (172, 'be66f300f895cd0efa8afa00f08e8d3749946500', '1a09939c', 'vertex/1.x.x', 'vertex/main', 'C'),
    (173, '7367ad9454cf2cecf46e0526a5b5e6c619e2aad6', 'ca5dde26', 'vertex/1.x.x', 'vertex/main', 'A'),
]


def g(repo, *args, ok=False):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    if r.returncode and not ok:
        raise SystemExit(f'git -C {repo} {" ".join(args)}: {r.stderr.strip()}')
    return r.stdout if not ok else (r.returncode, r.stdout)


def meta_tip_then(meta, line, when):
    """The tip of the meta-dsensos line at a point in time: the newest commit on its
    first-parent chain committed at or before <when>."""
    for row in g(meta, 'log', '--first-parent', '--format=%H %ct', f'origin/{line}').splitlines():
        sha, ct = row.split()
        if int(ct) <= when:
            return sha
    raise SystemExit(f'meta-dsensos {line} has no commit before {when}')


def build(distro, meta, out, case):
    pr, merge, child, child_line, parent_line, expect = case
    ours = g(distro, 'rev-parse', f'{merge}^1').strip()
    theirs = g(distro, 'rev-parse', child).strip()
    bases = g(distro, 'merge-base', '--all', ours, theirs).split()
    tip = meta_tip_then(meta, parent_line, int(g(distro, 'log', '-1', '--format=%ct', merge)))
    with tempfile.TemporaryDirectory() as tmp:
        wt = os.path.join(tmp, 'wt')
        subprocess.run(['git', 'clone', '-q', '--shared', '--no-checkout', distro, wt], check=True)
        g(wt, 'checkout', '-q', '--detach', ours)
        env = ['-c', 'user.name=fixture', '-c', 'user.email=fixture@invalid', '-c', 'merge.conflictStyle=zdiff3']
        subprocess.run(['git', '-C', wt, *env, 'merge', '--no-ff', '--no-commit', theirs], capture_output=True)
        raw = [f for f in g(wt, 'diff', '--name-only', '--diff-filter=U').splitlines() if f]
        base_conflict = [] if release_git.merge_keep_target(wt, parent_line) else [release_git.BASE_YML]
        left = sorted(set([f for f in g(wt, 'diff', '--name-only', '--diff-filter=U').splitlines() if f] + base_conflict))
        if LOCK not in left:
            raise SystemExit(f'#{pr}: {LOCK} is not conflicted')
        others = [f for f in left if f != LOCK]
        pins = {name: release_git.lock_pin_at(distro, rev) for name, rev in
                (('base', bases[0]), ('parent', ours), ('child', theirs), ('landed', merge))}
        res = release_git.lock_rule(wt, meta, tip, pins['parent'], pins['child'])
        # extra is spelled out as well, so no default decides what a fixture records
        by_exact = {exact: release_git.lock_outcome(res['verdict'], len(others), exact=exact, extra='bot-pr')
                    for exact in ('push', 'bot-pr')}
        outcome = by_exact['push']
        if not release_git.lock_merge(wt, tip):
            raise SystemExit(f'#{pr}: lock_merge refused')
        with open(os.path.join(wt, LOCK)) as f:
            rule_text = f.read()
    landed = g(distro, 'show', f'{merge}:{LOCK}')
    if outcome != expect:
        raise SystemExit(f'#{pr}: measured outcome {outcome} with lock_pin.exact=push, the plan expects {expect}: {json.dumps(res, indent=1)}')
    name = f'ds-tegra-distro-{pr}'
    os.makedirs(os.path.join(out, name), exist_ok=True)
    for fname, rev in (('base', bases[0]), ('parent', ours), ('child', theirs), ('landed', merge)):
        with open(os.path.join(out, name, f'{fname}.lock.yml'), 'w') as f:
            f.write(g(distro, 'show', f'{rev}:{LOCK}'))
    doc = {
        'pr': f'ds-tegra-distro #{pr}', 'child': child_line, 'parent': parent_line,
        'merge': merge, 'parent_tip': ours, 'child_tip': theirs, 'merge_bases': bases,
        'conflicts_before_keep_target': raw, 'other_conflicts': others,
        'pins': pins, 'meta_tip': tip,
        'measured': {'verdict': res['verdict'], 'checks': res['checks'], 'extra_commits': len(res['extra_commits'])},
        'outcome_by_exact': by_exact,
        'rule_equals_landed': rule_text == landed,
        'landed_is': 'the tip' if pins['landed'] == tip else 'the child pin' if pins['landed'] == pins['child'] else 'another commit',
    }
    with open(os.path.join(out, name, 'case.json'), 'w') as f:
        json.dump(doc, f, indent=1)
        f.write('\n')
    print(f'{name}: outcome {by_exact["bot-pr"]} as shipped, {outcome} with lock_pin.exact=push ({res["verdict"]}), {len(others)} other conflicted file(s), '
          f'{len(res["extra_commits"])} layer commit(s) in neither pin, rule result == landed lockfile: {rule_text == landed} '
          f'(landed pin is {doc["landed_is"]})')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--distro', required=True)
    ap.add_argument('--meta', required=True)
    ap.add_argument('--out', default=HERE)
    args = ap.parse_args()
    for case in CASES:
        build(os.path.abspath(args.distro), os.path.abspath(args.meta), args.out, case)
    print(f'=== {len(CASES)} cases written to {args.out}')


if __name__ == '__main__':
    main()
