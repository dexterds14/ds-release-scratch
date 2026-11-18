"""Tests for R-lock, the lockfile pin rule of merge-up (ENG-2234, plan 5.1).

LockUnitTests: release_git.py's pieces on throwaway repos (the pin edit, the
normalised three-way merge, the five checks, the outcome table, the check
wait, the first-parent invariant). The seven real lockfile conflicts are in
test_lock_fixtures.py, the merge-up.sh cases in test_lock_merge_up.py.
Standard library and git only, no network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import version  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)

LOCK = release_git.LOCKFILE
OTHER = '7117d115eab7351ecf21388ec720a3bb5f4a9b30'   # the poky commit in t.LOCK_YML
git, write, commit_all = t.git, t.write, t.commit_all


def lock(pin, poky=OTHER):
    return t.LOCK_YML.format(pin=pin).replace(OTHER, poky)


class Repos:
    """A meta-dsensos repo and a ds-tegra-distro repo with an open merge whose lockfile conflicts.

    meta: m0 on `main`; the parent line adds p1 (other.txt), the child line adds c1
    (layer.txt); `tip` merges the child line into the parent line. distro: the base pins m0,
    the parent pins p1, the child pins c1, and the parent has `git merge --no-commit child`
    open."""

    def __init__(self, parent_poky=OTHER, child_poky=OTHER):
        self.top = tempfile.mkdtemp(prefix='lock-rule-')
        self.meta = os.path.join(self.top, 'meta')
        self.distro = os.path.join(self.top, 'distro')
        subprocess.run(['git', 'init', '-q', '-b', 'main', self.meta], check=True)
        write(os.path.join(self.meta, 'layer.txt'), 'm0\n')
        write(os.path.join(self.meta, 'other.txt'), 'm0\n')
        self.m0 = commit_all(self.meta, 'm0')
        git(self.meta, 'checkout', '-q', '-b', 'child')
        write(os.path.join(self.meta, 'layer.txt'), 'child fix\n')
        self.c1 = commit_all(self.meta, 'child layer fix')
        git(self.meta, 'checkout', '-q', 'main')
        write(os.path.join(self.meta, 'other.txt'), 'parent work\n')
        self.p1 = commit_all(self.meta, 'parent layer work')
        git(self.meta, 'merge', '-q', '--no-ff', '-m', 'Merge child into main', 'child')
        self.tip = git(self.meta, 'rev-parse', 'HEAD')
        subprocess.run(['git', 'init', '-q', '-b', 'base', self.distro], check=True)
        write(os.path.join(self.distro, LOCK), lock(self.m0))
        write(os.path.join(self.distro, 'foo.txt'), 'base\n')
        commit_all(self.distro, 'base')
        git(self.distro, 'checkout', '-q', '-b', 'child')
        write(os.path.join(self.distro, LOCK), lock(self.c1, child_poky))
        self.child_tip = commit_all(self.distro, 'child pins c1')
        git(self.distro, 'checkout', '-q', '-b', 'parent', 'base')
        write(os.path.join(self.distro, LOCK), lock(self.p1, parent_poky))
        self.parent_tip = commit_all(self.distro, 'parent pins p1')
        self.merge()

    def merge(self):
        r = subprocess.run(['git', '-C', self.distro, '-c', 'user.name=t', '-c', 'user.email=t@x', '-c', 'merge.conflictStyle=zdiff3',
                            'merge', '--no-ff', '--no-commit', 'child'], capture_output=True, text=True)
        assert r.returncode == 1, r.stdout + r.stderr
        return r

    def meta_commit(self, path, content, msg):
        write(os.path.join(self.meta, path), content)
        return commit_all(self.meta, msg)

    def lock_text(self):
        with open(os.path.join(self.distro, LOCK)) as f:
            return f.read()

    def unmerged(self):
        return git(self.distro, 'diff', '--name-only', '--diff-filter=U').split()

    def rule(self, tip=None, **kw):
        return release_git.lock_rule(self.distro, self.meta, tip or self.tip, kw.pop('parent_pin', self.p1), kw.pop('child_pin', self.c1), **kw)

    def close(self):
        shutil.rmtree(self.top, ignore_errors=True)


class LockUnitTests(unittest.TestCase):
    def repos(self, **kw):
        r = Repos(**kw)
        self.addCleanup(r.close)
        return r

    # --- the pin edit ---

    def test_lock_pin_set_changes_one_line(self):
        a, b = 'a' * 40, 'b' * 40
        before = lock(a)
        after = release_git.lock_pin_set(before, b)
        self.assertEqual(release_git.lock_pin(after), b)
        changed = [(x, y) for x, y in zip(before.splitlines(), after.splitlines()) if x != y]
        self.assertEqual(changed, [(f'      commit: {a}', f'      commit: {b}')])
        self.assertEqual(len(before.splitlines()), len(after.splitlines()))
        # another layer that happens to pin the same sha is not touched
        same = lock(a, poky=a)
        self.assertEqual(release_git.lock_pin_set(same, b).count(a), 1)

    def test_lock_pin_set_refuses_what_it_cannot_edit(self):
        with self.assertRaises(release_git.Error):
            release_git.lock_pin_set(lock('a' * 40), 'not-a-sha')
        with self.assertRaises(release_git.Error):
            release_git.lock_pin_set(lock('a' * 40), 'B' * 40)
        with self.assertRaises(release_git.Error):   # no meta-dsensos stanza
            release_git.lock_pin_set('overrides:\n  repos:\n    poky:\n      commit: ' + 'a' * 40 + '\n', 'b' * 40)
        # a layout the one-line edit does not cover (a key between the name and the commit)
        odd = lock('a' * 40).replace('    meta-dsensos:\n', '    meta-dsensos:\n      note: x\n')
        with self.assertRaises(release_git.Error) as cm:
            release_git.lock_pin_set(odd, 'b' * 40)
        self.assertIn('unsupported layout', str(cm.exception))

    # --- lock_merge ---

    def test_lock_merge_changes_one_line(self):
        r = self.repos()
        self.assertEqual(r.unmerged(), [LOCK])
        self.assertIn('<<<<<<< ', r.lock_text())
        self.assertTrue(release_git.lock_merge(r.distro, r.tip))
        self.assertEqual(r.lock_text(), lock(r.tip))
        self.assertEqual(r.unmerged(), [])
        self.assertEqual(git(r.distro, 'diff', '--cached', '--name-only', 'HEAD'), LOCK)
        # against the parent's lockfile exactly one line differs
        diff = git(r.distro, 'diff', '--cached', '-U0', 'HEAD', '--', LOCK).splitlines()
        self.assertEqual([l for l in diff if l.startswith(('+ ', '- '))], [f'-      commit: {r.p1}', f'+      commit: {r.tip}'])

    def test_lock_merge_check_writes_nothing(self):
        r = self.repos()
        before = (r.lock_text(), git(r.distro, 'status', '--porcelain=v2'), git(r.distro, 'ls-files', '-s'))
        self.assertTrue(release_git.lock_merge(r.distro, r.tip, check=True))
        self.assertEqual((r.lock_text(), git(r.distro, 'status', '--porcelain=v2'), git(r.distro, 'ls-files', '-s')), before)
        # the CLI form the shell uses
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'lock-merge', r.tip, '--git-dir', r.distro, '--check'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('would merge', p.stdout)
        self.assertEqual((r.lock_text(), git(r.distro, 'status', '--porcelain=v2'), git(r.distro, 'ls-files', '-s')), before)

    def test_lock_merge_refuses_another_layers_conflict(self):
        r = self.repos(parent_poky='b' * 40, child_poky='a' * 40)
        before = (r.lock_text(), git(r.distro, 'ls-files', '-s'))
        self.assertFalse(release_git.lock_merge(r.distro, r.tip))
        self.assertEqual((r.lock_text(), git(r.distro, 'ls-files', '-s')), before)   # still git's markers, still unmerged
        self.assertEqual(r.unmerged(), [LOCK])
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'lock-merge', r.tip, '--git-dir', r.distro],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn('conflicts beyond the meta-dsensos commit', p.stderr)

    def test_lock_merge_carries_another_layers_one_sided_change(self):
        r = self.repos(child_poky='a' * 40)
        self.assertTrue(release_git.lock_merge(r.distro, r.tip))
        self.assertEqual(r.lock_text(), lock(r.tip, poky='a' * 40))

    def test_lock_merge_needs_a_sha_and_a_merge(self):
        r = self.repos()
        with self.assertRaises(release_git.Error):
            release_git.lock_merge(r.distro, 'main')
        git(r.distro, 'merge', '--abort')
        with self.assertRaises(release_git.Error):
            release_git.lock_merge(r.distro, r.tip)

    # --- the checks ---

    def test_is_ancestor_tells_not_contained_from_unknown(self):
        r = self.repos()
        self.assertEqual(release_git.is_ancestor(r.meta, r.c1, r.tip), 'yes')
        self.assertEqual(release_git.is_ancestor(r.meta, r.tip, r.c1), 'no')
        self.assertEqual(release_git.is_ancestor(r.meta, 'd' * 40, r.tip), 'unknown')

    def test_rule_exact(self):
        r = self.repos()
        res = r.rule()
        self.assertEqual(res['verdict'], 'exact', res)
        self.assertEqual([(c['n'], c['ok']) for c in res['checks']], [(1, True), (2, True), (2, True), (3, True), (4, True), (5, True)])
        self.assertIsNone(res['failed'])
        self.assertEqual(res['extra_commits'], [f'{r.tip[:7]} Merge child into main'])   # the merge commit itself
        self.assertEqual(release_git.lock_trailer(res), f'Merge-Up-Rule: lock-pin tip={r.tip} parent-pin={r.p1} child-pin={r.c1}')
        self.assertEqual(r.unmerged(), [LOCK])   # read-only

    def test_rule_exact_ignores_paths_no_build_reads(self):
        r = self.repos()
        write(os.path.join(r.meta, 'docs/notes.md'), 'notes\n')
        write(os.path.join(r.meta, '.ai/x.md'), 'x\n')
        write(os.path.join(r.meta, 'README.md'), 'readme\n')
        tip = commit_all(r.meta, 'docs only')
        res = r.rule(tip)
        self.assertEqual(res['verdict'], 'exact', res)
        self.assertIn('3 such path(s) differ', res['checks'][-1]['text'])
        self.assertEqual(len(res['extra_commits']), 2)

    def test_rule_extra_when_the_tip_holds_more(self):
        r = self.repos()
        tip = r.meta_commit('recipes-x/x.bb', 'new recipe\n', 'layer work no line locked')
        res = r.rule(tip)
        self.assertEqual(res['verdict'], 'extra', res)
        self.assertEqual((res['checks'][-1]['n'], res['checks'][-1]['ok']), (5, False))
        self.assertIn('1 build path(s) differ', res['reason'])
        self.assertIn('recipes-x/x.bb', res['reason'])
        self.assertEqual(res['extra_commits'], [f'{tip[:7]} layer work no line locked', f'{r.tip[:7]} Merge child into main'])
        self.assertIsNone(res['failed'])   # checks 1 to 4 passed: not a refusal
        # a markdown file a recipe ships is a build input (only top-level *.md is not)
        r2 = self.repos()
        tip2 = r2.meta_commit('recipes-x/files/notes.md', 'shipped\n', 'a shipped doc')
        self.assertEqual(r2.rule(tip2)['verdict'], 'extra')

    def test_rule_extra_when_the_layer_merge_was_resolved_by_hand(self):
        # both layer lines edit layer.txt: the tip's merge commit carries a resolution no commit count sees
        r = self.repos()
        git(r.meta, 'checkout', '-q', '-b', 'p2', r.p1)
        write(os.path.join(r.meta, 'layer.txt'), 'parent edit\n')
        p2 = commit_all(r.meta, 'parent edits layer.txt')
        subprocess.run(['git', '-C', r.meta, '-c', 'user.name=t', '-c', 'user.email=t@x', 'merge', '--no-ff', '--no-commit', r.c1], capture_output=True)
        write(os.path.join(r.meta, 'layer.txt'), 'resolved by hand\n')
        tip = commit_all(r.meta, 'Merge child (hand-resolved)')
        res = r.rule(tip, parent_pin=p2)
        self.assertEqual(res['verdict'], 'extra', res)
        self.assertIn('do not merge clean', res['reason'])
        self.assertIn('layer.txt', res['reason'])
        self.assertEqual(git(r.meta, 'rev-list', '--count', '--no-merges', tip, f'^{p2}', f'^{r.c1}'), '0')   # what a count would say

    def test_rule_refuses_a_pin_the_tip_does_not_contain(self):
        r = self.repos()
        # the parent's pin: the tip is the child line, which lacks p1
        res = r.rule(r.c1)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 2))
        self.assertIn(f"the parent's pin {r.p1} is not contained in the tip", res['reason'])
        # the child's pin
        res = r.rule(r.p1)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 2))
        self.assertIn(f"the child's pin {r.c1} is not contained in the tip", res['reason'])
        # an object the clone does not have is a different refusal
        res = r.rule(parent_pin='d' * 40)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 2))
        self.assertIn('unknown object', res['reason'])
        res = r.rule(child_pin='d' * 40)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 2))
        self.assertIn(f"the child's pin {'d' * 40} is an unknown object", res['reason'])

    def test_rule_refuses_a_tip_that_is_not_a_commit(self):
        r = self.repos()
        res = r.rule('d' * 40)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 1))
        res = r.rule(rewound='old1234 is not an ancestor of new5678')
        self.assertEqual((res['verdict'], res['failed']), ('refused', 1))
        self.assertIn('rewritten during this run', res['reason'])
        res = r.rule(unread='fatal: could not read from remote repository')
        self.assertEqual((res['verdict'], res['failed']), ('refused', 1))
        self.assertIn('could not be refetched during this run, so its tip is unknown (fatal: could not read', res['reason'])
        self.assertNotIn('rewritten', res['reason'])
        with self.assertRaises(release_git.Error):
            r.rule('main')

    def test_rule_refuses_another_layers_conflict(self):
        r = self.repos(parent_poky='b' * 40, child_poky='a' * 40)
        res = r.rule()
        self.assertEqual((res['verdict'], res['failed']), ('refused', 3))
        self.assertIn('conflicts beyond the meta-dsensos commit', res['reason'])
        self.assertEqual(release_git.lock_outcome(res['verdict'], 0), 'D')

    def test_rule_refuses_a_result_that_does_not_parse_back(self):
        r = self.repos(child_poky='a' * 40)
        merged = lock(r.tip, poky='a' * 40)
        real = release_git.lock_pin
        seen = []

        def reparse(text, layer=release_git.LAYER):
            # the merged text is parsed twice: once as the child's side with the pin set, then
            # as the merge result. The second parse is check 4.
            if text == merged:
                seen.append(text)
                if len(seen) == 2:
                    return 'e' * 40
            return real(text, layer)
        release_git.lock_pin = reparse
        try:
            res = r.rule()
        finally:
            release_git.lock_pin = real
        self.assertEqual(len(seen), 2)
        self.assertEqual((res['verdict'], res['failed']), ('refused', 4), res)

    def test_rule_planned_never_uses_the_tip_as_a_value(self):
        r = self.repos()
        # a dry run: the "tip" handed in is the parent line before the merge a real run pushes
        res = r.rule(r.p1, planned=True)
        self.assertEqual((res['verdict'], res['planned_ok']), ('planned', True), res)
        self.assertEqual(res['checks'], [])
        self.assertEqual(res['extra_commits'], [])
        self.assertNotIn('outcome', res)
        self.assertEqual(r.unmerged(), [LOCK])
        with self.assertRaises(release_git.Error):
            release_git.lock_outcome('planned', 0)
        # the parent's pin must still be in the parent line, and the rest must merge
        res = r.rule(r.m0, planned=True)
        self.assertEqual((res['verdict'], res['planned_ok']), ('planned', False))
        r2 = self.repos(parent_poky='b' * 40, child_poky='a' * 40)
        res = r2.rule(r2.p1, planned=True)
        self.assertEqual((res['verdict'], res['planned_ok']), ('planned', False))
        self.assertIn('conflicts beyond', res['reason'])

    def test_rule_cli(self):
        r = self.repos()
        base = [sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'lock-rule', r.tip, '--git-dir', r.distro, '--meta-dir', r.meta,
                '--parent-pin', r.p1, '--child-pin', r.c1]
        p = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        # the shipped config (lock_pin.exact=bot-pr): nothing new in the tip is still a bot-finished PR
        self.assertEqual((doc['verdict'], doc['outcome']), ('exact', 'B'))
        self.assertEqual(doc['trailer'], f'Merge-Up-Rule: lock-pin tip={r.tip} parent-pin={r.p1} child-pin={r.c1}')
        # a config that sets lock_pin.exact=push: the direct push, and the same trailer
        root = os.path.join(r.top, 'push-root')
        shutil.copytree(os.path.join(os.path.dirname(SCRIPTS), 'release'), os.path.join(root, 'release'))
        with open(os.path.join(root, 'release', 'config.json')) as f:
            config = json.load(f)
        config['merge_up']['lock_pin']['exact'] = 'push'
        with open(os.path.join(root, 'release', 'config.json'), 'w') as f:
            json.dump(config, f)
        p = subprocess.run(base + ['--root', root], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual((doc['verdict'], doc['outcome']), ('exact', 'A'))
        self.assertEqual(doc['trailer'], f'Merge-Up-Rule: lock-pin tip={r.tip} parent-pin={r.p1} child-pin={r.c1}')
        doc = json.loads(subprocess.run(base + ['--others', '2'], capture_output=True, text=True).stdout)
        self.assertEqual(doc['outcome'], 'C')
        doc = json.loads(subprocess.run(base + ['--planned'], capture_output=True, text=True).stdout)
        self.assertEqual(doc['verdict'], 'planned')
        self.assertNotIn('outcome', doc)
        # an evaluation that cannot run is exit 1, not a verdict
        p = subprocess.run(base[:3] + ['nope'] + base[4:], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn('not a 40-hex sha', p.stderr)
        p = subprocess.run(base[:4] + ['--git-dir', r.meta] + base[6:], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)   # no merge in progress there
        self.assertIn('release_git.py: ', p.stderr)

    def test_outcome_table(self):
        o = release_git.lock_outcome
        # no config given: the defaults (exact bot-pr since 2026-11-18, extra bot-pr), so a
        # lockfile-only conflict is a bot-finished PR whatever the tip holds
        self.assertEqual(o('exact', 0), 'B')
        self.assertEqual(o('extra', 0), 'B')
        self.assertEqual(o('exact', 0), o('exact', 0, **{k: version.MERGE_UP_DEFAULTS['lock_pin'][k] for k in ('exact', 'extra')}))
        # lock_pin.exact = push: the "nothing new" case is a direct push; the "tip holds more" case is not
        self.assertEqual(o('exact', 0, exact='push'), 'A')
        self.assertEqual(o('extra', 0, exact='push'), 'B')
        self.assertEqual(o('exact', 2, exact='push'), 'C')
        self.assertEqual(o('refused', 0, exact='push'), 'D')
        self.assertEqual(o('exact', 3), 'C')
        self.assertEqual(o('extra', 1), 'C')
        self.assertEqual(o('refused', 0), 'D')
        self.assertEqual(o('refused', 4), 'D')
        # lock_pin.exact = bot-pr: the "nothing new" case is a bot-finished PR too
        self.assertEqual(o('exact', 0, exact='bot-pr'), 'B')
        self.assertEqual(o('extra', 0, exact='bot-pr'), 'B')
        # lock_pin.extra = pr: the "tip holds more" case waits for one approval
        self.assertEqual(o('extra', 0, extra='pr'), 'P')
        self.assertEqual(o('exact', 0, extra='pr'), 'B')
        self.assertEqual(o('exact', 0, exact='push', extra='pr'), 'A')
        self.assertEqual(o('exact', 0, exact='bot-pr', extra='pr'), 'B')
        for bad in ('planned', '', 'yes'):
            with self.assertRaises(release_git.Error):
                o(bad, 0)
        with self.assertRaises(release_git.Error):
            o('exact', 0, exact='force')
        # the shipped config
        cfg = release_git.merge_up_config()['lock_pin']
        self.assertEqual((cfg['exact'], cfg['extra'], cfg['verify_finish']), ('bot-pr', 'bot-pr', False))

    # --- the wait for a bot-finished PR ---

    def test_checks_state(self):
        head = 'a' * 40
        need = ['pr-checks', 'pin-propagation-safety']

        def state(rollup, **kw):
            doc = {'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': rollup}
            doc.update(kw)
            return release_git.checks_state(doc, head, need)

        ok = {'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}
        ok2 = {'context': 'pin-propagation-safety', 'state': 'SUCCESS'}
        self.assertEqual(state([ok, ok2])[0], 'green')
        self.assertEqual(state([])[0], 'pending')
        self.assertEqual(state([ok]), ('pending', ['pin-propagation-safety: not reported yet']))
        self.assertEqual(state([ok, {'name': 'pin-propagation-safety', 'status': 'IN_PROGRESS', 'conclusion': None}])[0], 'pending')
        self.assertEqual(state([ok, {'name': 'pin-propagation-safety', 'status': 'COMPLETED', 'conclusion': 'SKIPPED'}])[0], 'pending')
        self.assertEqual(state([ok, {'name': 'pin-propagation-safety', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}])[0], 'red')
        self.assertEqual(state([{'context': 'pr-checks', 'state': 'ERROR'}])[0], 'red')
        # a rerun that is still red beside an older green one
        self.assertEqual(state([ok, ok2, {'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'CANCELLED'}])[0], 'red')
        # a check nobody requires does not count either way
        self.assertEqual(state([ok, ok2, {'name': 'lint', 'conclusion': 'FAILURE'}])[0], 'green')
        self.assertEqual(state([{'name': 'lint', 'conclusion': 'SUCCESS'}])[0], 'pending')
        # the head moved, or the PR is not open: never green, whatever the checks say
        self.assertEqual(state([ok, ok2], headRefOid='b' * 40)[0], 'moved')
        self.assertEqual(state([ok, ok2], state='MERGED')[0], 'closed')
        self.assertEqual(state([ok, ok2], state='CLOSED')[0], 'closed')
        # no required check configured: nothing vouches for the commit
        self.assertEqual(release_git.checks_state({'state': 'OPEN', 'headRefOid': head, 'statusCheckRollup': [ok]}, head, [])[0], 'red')

    def test_checks_state_cli(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json') as f:
            json.dump({'state': 'OPEN', 'headRefOid': 'a' * 40,
                       'statusCheckRollup': [{'name': 'pr-checks', 'conclusion': 'SUCCESS'}]}, f)
            f.flush()
            cli = [sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'checks-state', f.name, '--head', 'a' * 40, '--required']
            self.assertEqual(subprocess.run(cli + ['pr-checks'], capture_output=True, text=True).stdout.splitlines()[0], 'green')
            self.assertEqual(subprocess.run(cli + ['pr-checks,pin-propagation-safety'], capture_output=True, text=True).stdout.splitlines(),
                             ['pending', 'pin-propagation-safety: not reported yet'])

    # --- the start-of-run invariant ---

    def test_lock_invariant(self):
        r = self.repos()
        git(r.distro, 'merge', '--abort')
        ok, text = release_git.lock_invariant(r.distro, r.meta, r.parent_tip, 'main')
        self.assertTrue(ok, text)
        # a merge-up that carried the child's pin: contained in the line, not on its first-parent chain
        ok, text = release_git.lock_invariant(r.distro, r.meta, r.child_tip, 'main')
        self.assertFalse(ok)
        self.assertIn(f'the parent\'s lock pin {r.c1} is not on the first-parent chain', text)
        self.assertIn('contained in the line through a merge', text)
        self.assertIn(f'last moved by {r.child_tip[:7]} child pins c1', text)
        # a pin that is not in the line at all
        git(r.meta, 'checkout', '-q', '-b', 'feature', r.m0)
        stray = r.meta_commit('layer.txt', 'stray\n', 'stray')
        write(os.path.join(r.distro, LOCK), lock(stray))
        tip = commit_all(r.distro, 'pin a feature commit')
        ok, text = release_git.lock_invariant(r.distro, r.meta, tip, 'main')
        self.assertFalse(ok)
        self.assertIn('not contained in the line', text)
        self.assertIn('pin a feature commit', text)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'release_git.py'), 'lock-invariant', '--git-dir', r.distro, '--meta-dir', r.meta,
                            '--parent-tip', tip, '--meta-ref', 'main'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertTrue(p.stdout.startswith('::warning::merge-up: lock invariant: '), p.stdout)


if __name__ == '__main__':
    unittest.main()
