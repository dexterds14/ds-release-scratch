#!/usr/bin/env python3
"""History replay of R-lock, the lockfile pin rule (ENG-2234, plan section 9). Opt-in: it
needs clones of the real repos, so tests/run.sh does not run it.

    replay_lock_history.py --distro <ds-tegra-distro clone> --meta <meta-dsensos clone> [--keep] [PR ...]

For each real lockfile conflict under fixtures/merge-up/ it builds two local bare mirrors
rewound to just before the ds-tegra-distro PR: the distro parent line at the commit the PR
merged into, the child line at the tip the PR was opened for, and the meta-dsensos parent
line at the tip it had then (the layer step had already run). It then runs the real
merge-up.sh of this checkout against them with the stub gh and
DS_MERGE_UP_AUTOMATION=lock-pin, and compares blob shas. Each case runs once per value of
merge_up.lock_pin.exact that case.json records an outcome for (outcome_by_exact): under the
shipped release/config.json (exact=bot-pr since 2026-11-18), and under a copy of the scripts
whose config says exact=push. The second run is skipped where it cannot differ (the tip
holds more, or other files conflict).

    ds-tegra-distro #146, #162, #173   the tip holds nothing new.
                                       exact=bot-pr (shipped), outcome B: the lockfile on the parent after the bot merged its PR
                                       exact=push, outcome A: the lockfile pushed to the parent line
    ds-tegra-distro #170               outcome B: the lockfile on the parent after the bot merged its PR
    ds-tegra-distro #172               outcome C: the lockfile on the conflict PR branch
        must equal the lockfile of the PR's merge commit in history, byte for byte.
    ds-tegra-distro #153, #158         outcome B: these merged with the child's pin and each needed
        a second PR; the replayed lockfile must be the landed one with the pin on the tip.

The mirrors borrow the clones' objects through alternates and are only read from there:
nothing is written to either clone, nothing is fetched or pushed over the network. Exit 0
when every case matches.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the stub gh)

LOCK = release_git.LOCKFILE
FIXTURES = os.path.join(HERE, 'fixtures', 'merge-up')
DISTRO_REPO, META_REPO = 'ds/distro', 'ds/meta'
GREEN = [{'name': n, 'status': 'COMPLETED', 'conclusion': 'SUCCESS'} for n in release_git.merge_up_config()['finish']['required_checks']]


def g(repo, *args, ok=False, **kw):
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True, **kw)
    if r.returncode and not ok:
        raise SystemExit(f'git -C {repo} {" ".join(args)}: {r.stderr.strip()}')
    return r.stdout.strip()


def mirror(path, source, refs):
    """A bare repo at <path> holding only <refs> (name -> sha), reading <source>'s objects."""
    subprocess.run(['git', 'init', '-q', '--bare', '-b', next(iter(refs)), path], check=True)
    objects = g(source, 'rev-parse', '--git-common-dir')
    objects = os.path.join(objects if os.path.isabs(objects) else os.path.join(source, objects), 'objects')
    with open(os.path.join(path, 'objects', 'info', 'alternates'), 'w') as f:
        f.write(objects + '\n')
    for name, sha in refs.items():
        g(path, 'update-ref', f'refs/heads/{name}', sha)


def meta_line_then(meta, line, when, fallback):
    """The meta-dsensos line's tip at <when> (first-parent chain), or <fallback> when the
    line no longer exists on origin (an abandoned patch line)."""
    if subprocess.run(['git', '-C', meta, 'rev-parse', '-q', '--verify', f'refs/remotes/origin/{line}'], capture_output=True).returncode:
        return fallback, 'the child pin (the line is gone from origin)'
    for row in g(meta, 'log', '--first-parent', '--format=%H %ct', f'origin/{line}').splitlines():
        sha, ct = row.split()
        if int(ct) <= when:
            return sha, 'the line tip at that time'
    return fallback, 'the child pin (no commit that old on the line)'


def scripts_under(top, exact):
    """merge-up.sh of this checkout when its release/config.json already says lock_pin.exact
    = <exact>, else of a copy under <top> whose config does (the scripts read the config of
    their own checkout)."""
    if release_git.merge_up_config()['lock_pin']['exact'] == exact:
        return os.path.join(SCRIPTS, 'merge-up.sh'), 'release/config.json as shipped'
    root = os.path.join(top, f'scripts-exact-{exact}')
    shutil.copytree(SCRIPTS, os.path.join(root, 'scripts-release'), ignore=shutil.ignore_patterns('tests', '__pycache__'))
    shutil.copytree(os.path.join(os.path.dirname(SCRIPTS), 'release'), os.path.join(root, 'release'))
    path = os.path.join(root, 'release', 'config.json')
    with open(path) as f:
        config = json.load(f)
    config['merge_up']['lock_pin']['exact'] = exact
    with open(path, 'w') as f:
        json.dump(config, f, indent=2)
    return os.path.join(root, 'scripts-release', 'merge-up.sh'), 'a copy of the scripts with that value'


def replay(distro, meta, case_dir, keep):
    """One result per value of lock_pin.exact the case is replayed under."""
    with open(os.path.join(case_dir, 'case.json')) as f:
        by_exact = json.load(f)['outcome_by_exact']
    shipped = release_git.merge_up_config()['lock_pin']['exact']
    values = [shipped] + [e for e in by_exact if e != shipped and by_exact[e] != by_exact[shipped]]
    return [replay_one(distro, meta, case_dir, keep, exact) for exact in values]


def replay_one(distro, meta, case_dir, keep, exact):
    with open(os.path.join(case_dir, 'case.json')) as f:
        doc = json.load(f)
    child, parent, pins, tip = doc['child'], doc['parent'], doc['pins'], doc['meta_tip']
    top = tempfile.mkdtemp(prefix='lock-replay-')
    try:
        distro_bare, meta_bare = os.path.join(top, 'distro.git'), os.path.join(top, 'meta.git')
        when = int(g(distro, 'log', '-1', '--format=%ct', doc['merge']))
        meta_child, how = meta_line_then(meta, child, when, pins['child'])
        if release_git.is_ancestor(meta, pins['child'], meta_child) != 'yes' or release_git.is_ancestor(meta, meta_child, tip) != 'yes':
            meta_child, how = pins['child'], 'the child pin (the line tip then is not what the layer step had merged)'
        mirror(distro_bare, distro, {parent: doc['parent_tip'], child: doc['child_tip']})
        mirror(meta_bare, meta, {parent: tip, child: meta_child})
        work = os.path.join(top, 'distro')
        subprocess.run(['git', 'clone', '-q', '--branch', parent, distro_bare, work], check=True)
        state, binp = os.path.join(top, 'gh-state'), os.path.join(top, 'bin')
        os.makedirs(state)
        os.makedirs(binp)
        t.write(os.path.join(binp, 'gh'), t.GH_STUB)
        os.chmod(os.path.join(binp, 'gh'), 0o755)
        t.write(os.path.join(state, 'green'), doc['child_tip'] + '\n')
        t.write(os.path.join(state, 'pr-1-review.json'), json.dumps({'statusCheckRollup': GREEN}))
        env = {k: v for k, v in os.environ.items() if k not in ('MERGE_UP_SUMMARY', 'MERGE_UP_READ_TOKEN', 'DEFAULT_BRANCH')}
        env.update(PATH=binp + os.pathsep + os.environ['PATH'], TOKEN='stub-token', TOKEN_KIND='app', DRY_RUN='false',
                   META_DSENSOS_URL=meta_bare, DISTRO_REPO=DISTRO_REPO, META_REPO=META_REPO, GH_TOKEN='stub-token',
                   GH_STUB_STATE=state, GH_STUB_GREEN=os.path.join(state, 'green'),
                   GH_STUB_REPOS=f'{DISTRO_REPO}={distro_bare},{META_REPO}={meta_bare}',
                   DS_MERGE_UP_AUTOMATION='lock-pin', MERGE_UP_LOCK_WAIT_SECONDS='0', MERGE_UP_LOCK_POLL_SECONDS='0')
        merge_up, which = scripts_under(top, exact)
        r = subprocess.run([merge_up, '--child', child, '--parent', parent],
                           cwd=work, capture_output=True, text=True, env=env)
        rule = [l.split('lockfile pin rule: ', 1)[1] for l in r.stdout.splitlines() if 'lockfile pin rule: ' in l]
        outcome = doc['outcome_by_exact'][exact]
        prs = []
        if os.path.exists(os.path.join(state, 'prs.json')):
            with open(os.path.join(state, 'prs.json')) as f:
                prs = [(p['title'], p['state']) for p in json.load(f) if p['repo'] == DISTRO_REPO]
        # A is the only outcome without a PR; B is one PR, opened and merged by the bot
        landing = {'A': prs == [], 'B': [s for _, s in prs] == ['MERGED'], 'C': [s for _, s in prs] == ['OPEN']}[outcome]
        # where the rule's lockfile is after the run
        where = f'merge-up/{child}/{doc["child_tip"][:7]}' if outcome == 'C' else parent
        got = g(distro_bare, 'rev-parse', '-q', '--verify', f'refs/heads/{where}:{LOCK}', ok=True)
        landed = g(distro, 'rev-parse', f'{doc["merge"]}:{LOCK}')
        if doc['rule_equals_landed']:
            want, what = landed, 'the landed lockfile'
        else:
            text = release_git.lock_pin_set(g(distro, 'show', f'{doc["merge"]}:{LOCK}') + '\n', tip)
            want = g(distro, 'hash-object', '--stdin', input=text)
            what = 'the landed lockfile with the pin on the tip (it landed with the child pin)'
        new_tip = g(distro_bare, 'rev-parse', f'refs/heads/{parent}')
        expect_rc = 1 if outcome == 'C' else 0
        moved = new_tip != doc['parent_tip']
        ok = (got == want and r.returncode == expect_rc and moved == (outcome != 'C') and landing
              and f'lock_pin.exact={exact},' in r.stdout
              and any(f'outcome {outcome}' in l for l in rule) and g(meta_bare, 'rev-parse', f'refs/heads/{parent}') == tip)
        print(f'{doc["pr"]}: {child} -> {parent}, lock_pin.exact={exact} ({which}), verdict {doc["measured"]["verdict"]}, expected outcome {outcome}')
        print(f'    rewound: distro {parent}={doc["parent_tip"][:8]} {child}={doc["child_tip"][:8]}; '
              f'meta-dsensos {parent}={tip[:8]} {child}={meta_child[:8]} ({how})')
        print(f'    merge-up.sh exit {r.returncode} (expected {expect_rc}); {parent} {"moved to " + new_tip[:8] if moved else "did not move"}')
        print(f'    PRs in {DISTRO_REPO}: {"; ".join(f"{title} [{st}]" for title, st in prs) or "none"}')
        for line in rule:
            print(f'    rule: {line[:150]}')
        print(f'    lockfile blob on {where}: {got or "(none)"}')
        print(f'    {what}: {want}')
        print(f'    {"MATCH" if ok else "MISMATCH"}')
        if not ok:
            print(r.stdout[-3000:])
            print(r.stderr[-1500:])
        return ok
    finally:
        if keep:
            print(f'    kept {top}')
        else:
            shutil.rmtree(top, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--distro', required=True, help='a ds-tegra-distro clone that has the history')
    ap.add_argument('--meta', required=True, help='a meta-dsensos clone that has the history')
    ap.add_argument('--keep', action='store_true', help='keep the temporary mirrors')
    ap.add_argument('prs', nargs='*', help='PR numbers to replay (default: every fixture)')
    args = ap.parse_args()
    names = sorted(n for n in os.listdir(FIXTURES) if os.path.exists(os.path.join(FIXTURES, n, 'case.json')))
    if args.prs:
        names = [n for n in names if n.rsplit('-', 1)[1] in args.prs]
    results = [ok for n in names for ok in replay(os.path.abspath(args.distro), os.path.abspath(args.meta), os.path.join(FIXTURES, n), args.keep)]
    print(f'=== {len(names)} case(s), {len(results)} replay(s), {sum(results)} match, {len(results) - sum(results)} mismatch')
    return 0 if results and all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
