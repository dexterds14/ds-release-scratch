"""The seven real lockfile conflicts as R-lock fixtures (ENG-2234, plan 5.1 and section 9).

fixtures/merge-up/ds-tegra-distro-<PR>/ holds the three sides of each conflict,
the lockfile that landed and what build_lock_fixtures.py measured on real
history. No conflict marker is committed: the markers are built here by a real
git merge of the three sides. Standard library and git only, no network.
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
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)

LOCK = release_git.LOCKFILE
FIXTURES = os.path.join(HERE, 'fixtures', 'merge-up')
git, write, commit_all = t.git, t.write, t.commit_all

# The verdict measured on real history, and the outcome it leads to per value of
# merge_up.lock_pin.exact (extra is bot-pr in both). The three "exact" cases are a direct
# push (A) only with exact=push; as shipped (exact=bot-pr, 2026-11-18) they are a bot-finished
# PR (B) like the three whose tip holds more.
VERDICT = {'146': 'exact', '162': 'exact', '173': 'exact', '153': 'extra', '158': 'extra', '170': 'extra', '172': 'extra'}
EXPECTED = {'146': {'push': 'A', 'bot-pr': 'B'}, '162': {'push': 'A', 'bot-pr': 'B'}, '173': {'push': 'A', 'bot-pr': 'B'},
            '153': {'push': 'B', 'bot-pr': 'B'}, '158': {'push': 'B', 'bot-pr': 'B'}, '170': {'push': 'B', 'bot-pr': 'B'},
            '172': {'push': 'C', 'bot-pr': 'C'}}


class LockFixtureTests(unittest.TestCase):
    """The seven real lockfile conflicts (fixtures/merge-up/ds-tegra-distro-<PR>/, written by
    build_lock_fixtures.py from history). The markers are built here by a real git merge of the
    three sides; the rule's lockfile must be the one that landed wherever a person resolved to
    the tip, and the tip instead of the child's pin in the two PRs that needed a second fix."""

    def cases(self):
        out = {}
        for name in sorted(os.listdir(FIXTURES)):
            path = os.path.join(FIXTURES, name, 'case.json')
            if os.path.exists(path):
                with open(path) as f:
                    out[name.rsplit('-', 1)[1]] = (os.path.join(FIXTURES, name), json.load(f))
        return out

    def side(self, d, name):
        with open(os.path.join(d, f'{name}.lock.yml')) as f:
            return f.read()

    def conflict(self, d):
        """A repo with the three sides committed and the merge open: the conflict as git wrote it."""
        top = tempfile.mkdtemp(prefix='lock-fixture-')
        self.addCleanup(shutil.rmtree, top, True)
        subprocess.run(['git', 'init', '-q', '-b', 'base', top], check=True)
        write(os.path.join(top, LOCK), self.side(d, 'base'))
        commit_all(top, 'base')
        git(top, 'checkout', '-q', '-b', 'child')
        write(os.path.join(top, LOCK), self.side(d, 'child'))
        commit_all(top, 'child')
        git(top, 'checkout', '-q', '-b', 'parent', 'base')
        write(os.path.join(top, LOCK), self.side(d, 'parent'))
        commit_all(top, 'parent')
        r = subprocess.run(['git', '-C', top, '-c', 'user.name=t', '-c', 'user.email=t@x', '-c', 'merge.conflictStyle=zdiff3',
                            'merge', '--no-ff', '--no-commit', 'child'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        return top

    def test_every_expected_case_is_present(self):
        cases = self.cases()
        self.assertEqual(sorted(cases), sorted(EXPECTED))
        for pr, (d, doc) in cases.items():
            self.assertEqual(doc['pr'], f'ds-tegra-distro #{pr}')
            for name in ('base', 'parent', 'child', 'landed'):
                self.assertEqual(release_git.lock_pin(self.side(d, name)), doc['pins'][name], (pr, name))
            self.assertNotIn('<<<<<<<', ''.join(self.side(d, n) for n in ('base', 'parent', 'child', 'landed')))

    def test_the_rule_gives_the_lockfile_a_person_resolved_to(self):
        for pr, (d, doc) in self.cases().items():
            with self.subTest(pr=pr):
                top = self.conflict(d)
                with open(os.path.join(top, LOCK)) as f:
                    marked = f.read()
                self.assertIn('<<<<<<< ', marked)
                self.assertIn(doc['pins']['parent'], marked)
                self.assertIn(doc['pins']['child'], marked)
                self.assertTrue(release_git.lock_merge(top, doc['meta_tip']))
                with open(os.path.join(top, LOCK)) as f:
                    result = f.read()
                landed = self.side(d, 'landed')
                self.assertEqual(release_git.lock_pin(result), doc['meta_tip'])
                if pr in ('153', '158'):
                    # merged with the child's pin; each needed a second PR. The rule gives the tip,
                    # and nothing else differs from what landed.
                    self.assertEqual(doc['pins']['landed'], doc['pins']['child'])
                    self.assertNotEqual(result, landed)
                    self.assertEqual(result, release_git.lock_pin_set(landed, doc['meta_tip']))
                else:
                    self.assertEqual(result, landed)
                self.assertEqual(result == landed, doc['rule_equals_landed'])

    def test_the_outcome_per_case(self):
        for pr, (d, doc) in self.cases().items():
            with self.subTest(pr=pr):
                m = doc['measured']
                self.assertEqual(m['verdict'], VERDICT[pr])
                others = len(doc['other_conflicts'])
                for exact, letter in EXPECTED[pr].items():
                    self.assertEqual(release_git.lock_outcome(m['verdict'], others, exact=exact, extra='bot-pr'), letter, exact)
                self.assertEqual(doc['outcome_by_exact'], EXPECTED[pr])
                self.assertNotIn('outcome', doc)   # an outcome is never recorded without the setting it holds for
                # the shipped config, and a config without the block, land where exact=bot-pr does
                shipped = release_git.merge_up_config()['lock_pin']
                self.assertEqual(release_git.lock_outcome(m['verdict'], others, shipped['exact'], shipped['extra']), EXPECTED[pr]['bot-pr'])
                self.assertEqual(release_git.lock_outcome(m['verdict'], others), EXPECTED[pr]['bot-pr'])
                oks = {(c['n'], c['ok']) for c in m['checks']}
                self.assertTrue({(1, True), (2, True), (3, True), (4, True)} <= oks, oks)   # none of these was a refusal
                self.assertEqual((5, True) in oks, m['verdict'] == 'exact')
        # #170: a commit count says "nothing new"; the tree comparison says the layer merge was resolved by hand
        doc = self.cases()['170'][1]
        self.assertIn('do not merge clean', doc['measured']['checks'][-1]['text'])
        self.assertEqual(doc['other_conflicts'], [])
        # #172: other files conflicted too
        self.assertGreater(len(self.cases()['172'][1]['other_conflicts']), 0)

    def test_measured_again_on_real_layer_history(self):
        """Checks 1, 2 and 5 against a meta-dsensos clone that has the objects ($META_DSENSOS_TREE
        or repos/meta-dsensos); skipped without one. tests/replay_lock_history.sh does the same
        through merge-up.sh."""
        tree = os.environ.get('META_DSENSOS_TREE') or os.path.join(os.path.dirname(SCRIPTS), 'repos', 'meta-dsensos')
        cases = self.cases()
        shas = sorted({s for _, doc in cases.values() for s in (doc['meta_tip'], doc['pins']['parent'], doc['pins']['child'])})
        if not os.path.isdir(tree) or any(subprocess.run(['git', '-C', tree, 'cat-file', '-e', s + '^{commit}'], capture_output=True).returncode
                                          for s in shas):
            self.skipTest(f'no meta-dsensos clone with the {len(shas)} fixture commits (set META_DSENSOS_TREE)')
        for pr, (d, doc) in cases.items():
            with self.subTest(pr=pr):
                for who in ('parent', 'child'):
                    self.assertEqual(release_git.is_ancestor(tree, doc['pins'][who], doc['meta_tip']), 'yes')
                equal, text = release_git.lock_tree_check(tree, doc['pins']['parent'], doc['pins']['child'], doc['meta_tip'])
                self.assertEqual('exact' if equal else 'extra', doc['measured']['verdict'], text)
                self.assertEqual(len(release_git.lock_extra_commits(tree, doc['meta_tip'], doc['pins']['parent'], doc['pins']['child'])),
                                 doc['measured']['extra_commits'])


if __name__ == '__main__':
    unittest.main()
