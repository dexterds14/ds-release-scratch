"""merge-up.sh with R-lock, the lockfile pin rule (ENG-2234, plan 5.1).

The MergeUpTests fixture of test_release_git.py (two bare repos, the stub gh):
vertex/1.x.x merges up into vertex/main, and both lines moved the meta-dsensos
pin, so kas/include/base.lock.yml conflicts. Commit dates are fixed, so two
fixtures built the same way hold the same shas and a run with the rule can be
compared with a run without it, line for line.
"""
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)
import release_git  # noqa: E402
import test_release_git as t  # noqa: E402  (the module, not its classes: discovery would run them twice)

LOCK = release_git.LOCKFILE
MERGE_UP, DISTRO_REPO, META_REPO, LOCK_YML = t.MERGE_UP, t.DISTRO_REPO, t.META_REPO, t.LOCK_YML
git, write = t.git, t.write
CHILD, PARENT = 'vertex/1.x.x', 'vertex/main'
FIXED_DATE = '2026-01-01T00:00:00+00:00'
RULE = 'lockfile pin rule: '
# merge_up.lock_pin.exact as shipped is bot-pr (the owner's decision of 2026-11-18): a
# lockfile-only conflict whose tip holds nothing new goes through a bot-finished PR. push,
# the direct push of outcome A, stays a supported value, and every test of it sets it here.
PUSH = {'lock_pin.exact': 'push'}
SHIPPED = object()   # up(config=...): the class's CONFIG


class LockCase(unittest.TestCase):
    """The fixture and its helpers. No test of its own."""

    # the MergeUpTests fixture (child vertex/1.x.x, parent vertex/main), without its cases
    child_work = t.MergeUpTests.child_work
    # The merge_up config every run of the class uses: None is release/config.json as shipped,
    # a dict ("section.key": value) is a copy of the scripts under that config.
    CONFIG = None

    def setUp(self):
        # Fixed commit dates, and no waiting: a bot-finished PR polls for 15 minutes by default,
        # which a test that reaches that path by mistake must never do.
        fixed = dict(GIT_AUTHOR_DATE=FIXED_DATE, GIT_COMMITTER_DATE=FIXED_DATE,
                     MERGE_UP_LOCK_WAIT_SECONDS='0', MERGE_UP_LOCK_POLL_SECONDS='0')
        saved = {k: os.environ.get(k) for k in fixed}
        os.environ.update(fixed)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        self.new_fixture()

    def new_fixture(self):
        """A fresh fixture as the current one (for twin runs); the old one is cleaned up at the end."""
        t.MergeUpTests.setUp(self)
        return self.fx

    # --- shapes ---

    def both_moved(self):
        """The child pins m3 (a layer fix on its line), the parent pins m4 (its own layer work).
        A real run merges the layer first: the new tip then holds exactly m3 and m4."""
        self.child_work(trailing_versions=False)
        self.m4 = self.fx.meta_commit(PARENT, 'main only\n', 'main pin target', path='other.txt')
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=self.m4)}, 'pin m4')
        return self.parent_tip

    def meta_merged(self):
        """The layer merge already on meta-dsensos vertex/main (an earlier run pushed it): a real tip."""
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', PARENT, f'origin/{PARENT}')
        git(self.fx.meta_work, 'merge', '-q', '--no-ff', '-m', f'Merge {CHILD} into {PARENT}', f'origin/{CHILD}')
        git(self.fx.meta_work, 'push', '-q', 'origin', PARENT)
        return self.meta_tip()

    def tip_holds_more(self):
        """both_moved, plus layer work on meta-dsensos vertex/main that no distro line has locked."""
        self.both_moved()
        self.m5 = self.fx.meta_commit(PARENT, 'unlocked layer work\n', 'layer work nobody locked', path='recipes-x/x.bb')

    def others_conflict(self):
        """both_moved, and foo.txt conflicts too."""
        self.both_moved()
        self.parent_tip = self.fx.distro_commit(PARENT, {'foo.txt': 'main fix\n'}, 'main fix')

    def another_layer_conflicts(self):
        """both_moved, and both lines also moved the poky pin: the lockfile conflicts beyond meta-dsensos."""
        self.both_moved()
        self.child_tip = self.fx.distro_commit(CHILD, {LOCK: LOCK_YML.format(pin=self.m3).replace('7117d115', 'aaaaaaaa')}, 'child poky')
        write(self.fx.green, self.child_tip + '\n')
        self.green_sha = self.child_tip
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=self.m4).replace('7117d115', 'bbbbbbbb')}, 'parent poky')

    # --- queries ---

    def merge_up_sh(self, config=SHIPPED):
        """merge-up.sh under the shipped config, or of a copy of the scripts under another one
        (one copy per fixture and config)."""
        config = self.CONFIG if config is SHIPPED else config
        if not config:
            return MERGE_UP
        key = (self.fx.top, tuple(sorted(config.items())))
        copies = self.__dict__.setdefault('_copies', {})
        if key not in copies:
            copies[key] = os.path.join(self.fx.scripts_copy(dict(config)), 'merge-up.sh')
        return copies[key]

    def up(self, *args, auto='lock-pin', env=None, config=SHIPPED, **kw):
        extra = dict(env or {})
        if auto is not None:
            extra['DS_MERGE_UP_AUTOMATION'] = auto
        return self.fx.run(self.merge_up_sh(config), '--child', CHILD, *args, extra_env=extra, **kw)

    def meta_tip(self):
        return self.fx.heads(self.fx.meta_bare)[PARENT]

    def distro_tip(self):
        return self.fx.heads(self.fx.distro_bare)[PARENT]

    def pin(self, ref):
        return release_git.lock_pin(self.fx.show(self.fx.distro_bare, ref, LOCK))

    def lockfile(self, ref):
        return self.fx.show(self.fx.distro_bare, ref, LOCK) + '\n'

    def anc(self, bare, a, b):
        return subprocess.run(['git', '-C', bare, 'merge-base', '--is-ancestor', a, b]).returncode == 0

    def branch(self):
        return f'merge-up/{CHILD}/{self.green_sha[:7]}'

    def rule_lines(self, r):
        return [l.split(RULE, 1)[1] for l in r.stdout.splitlines() if RULE in l]

    def snapshot(self, fx=None):
        """Everything a run can change: both repos' refs, the PRs, and what gh was asked to do."""
        fx = fx or self.fx
        log = re.sub(r'/\S*/tmp\.\w+/', '<tmp>/', fx.gh_log())
        return {'distro': fx.heads(fx.distro_bare), 'meta': fx.heads(fx.meta_bare), 'prs': fx.prs(), 'gh': log,
                'comments': self.commit_comments(fx)}

    def commit_comments(self, fx=None):
        path = os.path.join((fx or self.fx).state, 'commit-comments.json')
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def plain(self, r):
        """A run's output without what the rule adds: its own lines, the invariant line, and the
        two lines of the switches block that print the variable."""
        keep = []
        for line in r.stdout.splitlines():
            if RULE in line or 'lock invariant' in line or ': merge rules: ' in line or line.startswith(('variable:', 'active rules:')):
                continue
            keep.append(line)
        return keep

    def twin(self, shape, auto, *args, **kw):
        """Run once with the variable absent and once with <auto> on two fixtures built the same
        way. Returns (run without, snapshot without, run with, snapshot with)."""
        shape()
        off = self.up(*args, auto=None, **kw)
        snap_off = self.snapshot()
        self.new_fixture()
        shape()
        on = self.up(*args, auto=auto, **kw)
        return off, snap_off, on, self.snapshot()


class LockShadowTests(LockCase):
    """The rule evaluates and logs; nothing a run does changes (shadow, a dry run, no token,
    an evaluation that fails)."""

    def test_the_variable_absent_evaluates_nothing(self):
        self.both_moved()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('lockfile pin rule', r.stdout)
        self.assertNotIn('lock invariant', r.stdout)
        self.assertIn(f'conflicts ({LOCK})', r.stdout)
        r = self.up(auto='off')
        self.assertNotIn('lockfile pin rule', r.stdout)
        self.assertNotIn('lock invariant', r.stdout)
        # another rule listed: lock-pin is in shadow
        r = self.up(auto='splice')
        self.assertIn('shadow, nothing changes: would set the pin', r.stdout)

    def test_shadow_logs_and_changes_nothing(self):
        # nothing new in the tip: B as shipped (lock_pin.exact=bot-pr), A with lock_pin.exact=push
        for shape, config, letter in ((self.both_moved, None, 'B'), (self.both_moved, PUSH, 'A'), (self.tip_holds_more, None, 'B'),
                                      (self.others_conflict, None, 'C'), (self.another_layer_conflicts, None, 'D')):
            with self.subTest(outcome=letter, config=config):
                self.new_fixture()
                off, snap_off, on, snap_on = self.twin(shape, 'shadow', config=config)
                self.assertEqual(on.returncode, 1, on.stdout + on.stderr)
                self.assertEqual(off.returncode, 1, off.stdout + off.stderr)
                self.assertEqual(snap_on, snap_off)
                self.assertEqual(self.plain(on), self.plain(off))
                self.assertEqual(off.stdout.count('lockfile pin rule'), 0)
                lines = self.rule_lines(on)
                self.assertEqual(len(lines), 1, lines)
                self.assertTrue(lines[0].startswith('shadow, nothing changes: would '), lines)
                self.assertIn(f'outcome {letter}', lines[0])
                tip = self.meta_tip()
                self.assertIn(tip, lines[0])
                # the conflict PR is the phase-0 one: markers in the lockfile, the parent untouched
                pr = self.fx.prs()[0]
                self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
                self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, pr['head'], LOCK))
                self.assertEqual(self.distro_tip(), self.parent_tip)
                self.assertNotIn('rule', pr['body'])

    def test_shadow_lines_say_why(self):
        self.tip_holds_more()
        r = self.up(auto='shadow')
        line = self.rule_lines(r)[0]
        self.assertIn(f'in a bot-finished PR on {self.branch()}', line)
        self.assertIn('2 layer commit(s) in neither pin', line)   # m5 and the layer merge commit
        self.assertIn('1 build path(s) differ', line)
        self.assertIn('recipes-x/x.bb', line)
        self.new_fixture()
        self.another_layer_conflicts()
        r = self.up(auto='shadow')
        line = self.rule_lines(r)[0]
        self.assertIn('refused at check 3', line)
        self.assertIn('conflicts beyond the meta-dsensos commit', line)
        self.assertIn(f'candidate tip {self.meta_tip()}', line)
        # nothing new in the tip. As shipped that is a bot-finished PR too, and the line says which key decided it
        self.new_fixture()
        self.both_moved()
        r = self.up(auto='shadow')
        tip = self.meta_tip()
        self.assertEqual(self.rule_lines(r), [
            f'shadow, nothing changes: would set the pin to the meta-dsensos {PARENT} tip {tip} in a bot-finished PR on {self.branch()} '
            "(outcome B: checks 1 to 5 pass, the tip's tree equals the plain merge of the two pins (ignoring .ai/, docs/, *.md); "
            'a PR and not a direct push because lock_pin.exact=bot-pr; 1 layer commit(s) in neither pin)'])
        # lock_pin.exact=push: the direct push
        self.new_fixture()
        self.both_moved()
        r = self.up(auto='shadow', config=PUSH)
        tip = self.meta_tip()
        self.assertEqual(self.rule_lines(r), [
            f'shadow, nothing changes: would set the pin to the meta-dsensos {PARENT} tip {tip} and push {PARENT} directly '
            "(outcome A: checks 1 to 5 pass, the tip's tree equals the plain merge of the two pins (ignoring .ai/, docs/, *.md))"])

    def test_the_shadow_line_reaches_the_run_summary(self):
        self.both_moved()
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.up(auto='shadow', env={'MERGE_UP_SUMMARY': summary})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        with open(summary) as f:
            text = f.read()
        self.assertIn('- lockfile pin rule: shadow, nothing changes: would set the pin', text)

    def test_a_dry_run_with_a_planned_tip(self):
        self.both_moved()
        before = self.snapshot()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = self.rule_lines(r)[0]
        self.assertIn(f'dry run: would set the pin to the new meta-dsensos {PARENT} tip', line)
        self.assertIn(f"the parent's pin {self.m4[:7]} and the child's pin {self.m3[:7]} are both in it", line)
        # the landing is decided on the real tip; the line names the two keys and their shipped values
        self.assertIn('How that lands is decided on the real tip: lock_pin.exact=bot-pr when it holds nothing new, '
                      'lock_pin.extra=bot-pr when it holds more', line)
        # the planned tip is never printed as a value: the only layer sha on the line is in the pins
        self.assertNotIn(self.meta_tip(), line)
        self.assertNotIn('would push merge-up', r.stdout)
        self.assertNotIn('open the conflict PR', r.stdout)
        self.assertIn('dry run: nothing pushed', r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta'], after['prs']), (before['distro'], before['meta'], before['prs']))
        # in shadow the dry run keeps the phase-0 plan and adds the line
        r = self.up(auto='shadow', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('shadow, nothing changes: dry run: would set the pin to the new meta-dsensos', self.rule_lines(r)[0])
        self.assertIn(f'dry run: would push {self.branch()}', r.stdout)

    def test_a_dry_run_with_a_planned_tip_that_would_refuse(self):
        self.another_layer_conflicts()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('dry run: would refuse and leave the markers in the lockfile (the lockfile conflicts beyond the meta-dsensos commit)', self.rule_lines(r)[0])
        self.assertIn(f'dry run: would push {self.branch()}', r.stdout)   # the phase-0 plan stands

    def test_a_dry_run_with_a_real_tip(self):
        # nothing new in the tip, as shipped: the dry run names the bot-finished PR, and opens none
        self.both_moved()
        tip = self.meta_merged()
        before = self.snapshot()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('already contains the pin', r.stdout)
        self.assertEqual(self.rule_lines(r), [
            f'dry run: would set the pin to the meta-dsensos {PARENT} tip {tip} in a bot-finished PR on {self.branch()} '
            "(outcome B: checks 1 to 5 pass, the tip's tree equals the plain merge of the two pins (ignoring .ai/, docs/, *.md); "
            'a PR and not a direct push because lock_pin.exact=bot-pr; 1 layer commit(s) in neither pin)'])
        self.assertNotIn('directly', r.stdout)
        self.assertNotIn('would push merge-up', r.stdout)
        self.assertIn('dry run: nothing pushed', r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta'], after['prs']), (before['distro'], before['meta'], before['prs']))
        self.assertNotIn('pr create', after['gh'])
        self.assertNotIn(self.branch(), after['distro'])
        self.assertEqual(git(self.fx.distro, 'status', '--porcelain'), '')
        self.assertEqual(git(self.fx.distro, 'worktree', 'list').count('\n'), 0)

    def test_a_dry_run_with_a_real_tip_and_exact_push(self):
        self.both_moved()
        tip = self.meta_merged()
        before = self.snapshot()
        r = self.up(dry_run='true', config=PUSH)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('already contains the pin', r.stdout)
        line = self.rule_lines(r)[0]
        self.assertIn(f'dry run: would set the pin to the meta-dsensos {PARENT} tip {tip} and push {PARENT} directly (outcome A', line)
        self.assertNotIn('would push merge-up', r.stdout)
        self.assertIn('dry run: nothing pushed', r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta'], after['prs']), (before['distro'], before['meta'], before['prs']))
        # the checkout the script ran in holds no leftover of the trial merge
        self.assertEqual(git(self.fx.distro, 'status', '--porcelain'), '')
        self.assertEqual(git(self.fx.distro, 'worktree', 'list').count('\n'), 0)

    def test_a_dry_run_that_would_open_the_bot_pr(self):
        self.tip_holds_more()
        self.meta_merged()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'dry run: would set the pin to the meta-dsensos {PARENT} tip {self.meta_tip()} in a bot-finished PR on {self.branch()} (outcome B', self.rule_lines(r)[0])
        self.assertIn('dry run: nothing pushed', r.stdout)
        self.assertEqual(self.fx.prs(), [])
        self.assertNotIn(self.branch(), self.fx.heads(self.fx.distro_bare))

    def test_no_app_token_is_not_evaluated(self):
        off, snap_off, on, snap_on = self.twin(self.both_moved, 'lock-pin', token_kind='github', dry_run='true')
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        self.assertEqual(self.rule_lines(on), ['not evaluated (no App token, the layer repo was not read)'])
        self.assertIn('lock invariant:     not evaluated (no App token', on.stdout)
        self.assertIn('would push merge-up', on.stdout)   # the phase-0 plan, unchanged
        self.assertEqual(snap_on, snap_off)
        self.assertEqual(self.plain(on), self.plain(off))

    def unreadable_parent_lock(self):
        self.child_work(trailing_versions=False)
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin='not-a-sha')}, 'a lockfile nothing can read')

    def test_an_unreadable_parent_lockfile_leaves_behaviour_unchanged(self):
        for auto in ('lock-pin', 'shadow'):
            with self.subTest(auto=auto):
                self.new_fixture()
                off, snap_off, on, snap_on = self.twin(self.unreadable_parent_lock, auto)
                self.assertEqual(off.returncode, 1, off.stdout + off.stderr)
                self.assertEqual(on.returncode, 1, on.stdout + on.stderr)
                self.assertEqual(snap_on, snap_off)
                self.assertEqual(self.plain(on), self.plain(off))
                self.assertEqual(len(self.rule_lines(on)), 1)
                self.assertIn(f"not evaluated (the parent's lockfile at {self.parent_tip[:7]} could not be read", self.rule_lines(on)[0])
                self.assertIn('lock invariant:     not evaluated (', on.stdout)
                self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, self.fx.prs()[0]['head'], LOCK))

    def test_an_evaluation_that_fails_leaves_behaviour_unchanged(self):
        # the pin bot's policy file is what check 5 reads: without it the evaluation cannot finish
        scripts = self.fx.scripts_copy()
        os.remove(os.path.join(os.path.dirname(scripts), 'release', 'pin-propagation.json'))
        self.both_moved()
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'lock-pin'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(len(self.rule_lines(r)), 1)
        self.assertIn('not evaluated (', self.rule_lines(r)[0])
        self.assertIn('pin-propagation.json', self.rule_lines(r)[0])
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, pr['head'], LOCK))
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_the_lock_invariant(self):
        # a clean merge-up carries the child's pin into the parent: m3 is the second parent of the
        # layer merge, contained in meta-dsensos vertex/main but not on its first-parent chain
        self.child_work(trailing_versions=False)
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        merged = self.distro_tip()
        self.assertEqual(self.pin(merged), self.m3)
        self.fx.distro_commit(CHILD, {'foo.txt': 'child fix 2\n'}, 'another child fix')
        write(self.fx.green, self.fx.heads(self.fx.distro_bare)[CHILD] + '\n')
        before = self.snapshot()
        r = self.up(auto='shadow', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"::warning::merge-up: lock invariant: the parent's lock pin {self.m3} is not on the first-parent chain of its meta-dsensos line "
                      f"(contained in the line through a merge); the lockfile was last moved by {merged[:7]} Merge {CHILD} into {PARENT}", r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta']), (before['distro'], before['meta']))
        # the same run without the variable says nothing
        r = self.up(auto=None, dry_run='true')
        self.assertNotIn('lock invariant', r.stdout)
        # a parent whose pin is on the chain
        self.new_fixture()
        self.both_moved()
        r = self.up(auto='shadow', dry_run='true')
        self.assertIn(f"lock invariant:     ok, the parent's lock pin {self.m4[:7]} is on the first-parent chain", r.stdout)
        self.assertNotIn('::warning::', r.stdout)


RETRY_HOOK = r"""#!/usr/bin/env python3
# pre-receive hook of the distro bare repo, for one test: reject the first push to the parent
# line and, like a writer that got there first, move the parent's lock (and the layer line it
# names) before the script retries.
import os, subprocess, sys, tempfile
MARK, META, MODE, PARENT, LOCK = %(mark)r, %(meta)r, %(mode)r, %(parent)r, %(lock)r
if os.path.exists(MARK) or not any(l.endswith(' refs/heads/' + PARENT) for l in sys.stdin.read().splitlines()):
    sys.exit(0)
open(MARK, 'w').close()
env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
env.update(GIT_AUTHOR_NAME='other', GIT_AUTHOR_EMAIL='o@x', GIT_COMMITTER_NAME='other', GIT_COMMITTER_EMAIL='o@x',
           GIT_AUTHOR_DATE=%(date)r, GIT_COMMITTER_DATE=%(date)r)
def g(repo, *a, **kw):
    return subprocess.run(['git', '-C', repo, *a], check=True, capture_output=True, text=True, env=dict(env, **kw.pop('env', {})), **kw).stdout.strip()
DISTRO = os.getcwd()
ref = 'refs/heads/' + PARENT
t1 = g(META, 'rev-parse', ref)
if MODE in ('descend', 'gone'):   # a new layer commit on top of the tip (the same tree: nothing new to build)
    t2 = g(META, 'commit-tree', '-p', t1, '-m', 'a concurrent layer commit', t1 + '^{tree}')
else:                      # the layer line rewritten: the new tip does not contain the old one
    t2 = g(META, 'commit-tree', '-p', t1 + '^1', '-p', t1 + '^2', '-m', 'the layer merge, redone', t1 + '^{tree}')
g(META, 'update-ref', ref, t2)
if MODE == 'gone':         # and then the layer line cannot be read: the refetch fails
    g(META, 'update-ref', '-d', ref)
old = g(DISTRO, 'rev-parse', ref)
text = g(DISTRO, 'show', old + ':' + LOCK) + '\n'
pin = [l.split()[-1] for l in text.splitlines() if 'commit:' in l][-1]
blob = g(DISTRO, 'hash-object', '-w', '--stdin', input=text.replace(pin, t2))
with tempfile.TemporaryDirectory() as tmp:
    index = {'GIT_INDEX_FILE': os.path.join(tmp, 'index')}
    g(DISTRO, 'read-tree', old, env=index)
    g(DISTRO, 'update-index', '--cacheinfo', '100644,%%s,%%s' %% (blob, LOCK), env=index)
    tree = g(DISTRO, 'write-tree', env=index)
g(DISTRO, 'update-ref', ref, g(DISTRO, 'commit-tree', '-p', old, '-m', 'pin the new layer tip', tree))
print('rejected by the test hook: the parent moved', file=sys.stderr)
sys.exit(1)
"""


class LockDirectPushTests(LockCase):
    """Outcome A: the lockfile is the only conflict, the tip builds what the two pins build, and
    merge_up.lock_pin.exact is push. That is not the shipped value (bot-pr since 2026-11-18;
    LockBotPrTests has the shipped path), so every run here is under a config that sets it."""

    CONFIG = PUSH

    def trailer(self, sha):
        return git(self.fx.distro_bare, 'log', '-1', '--format=%(trailers:key=Merge-Up-Rule,valueonly)', sha)

    def test_outcome_a_is_pushed_with_its_record(self):
        parent_tip = self.both_moved()
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs(), [])
        self.assertIn('lock_pin.exact=push, lock_pin.extra=bot-pr (release/config.json at that ref)', r.stdout)
        meta_tip, tip = self.meta_tip(), self.distro_tip()
        # the pin is the layer tip the same run pushed: both pins in it, neither of them
        self.assertEqual(self.pin(tip), meta_tip)
        self.assertTrue(self.anc(self.fx.meta_bare, self.m3, meta_tip) and self.anc(self.fx.meta_bare, self.m4, meta_tip))
        self.assertNotIn(meta_tip, (self.m3, self.m4))
        self.assertEqual(self.lockfile(tip), LOCK_YML.format(pin=meta_tip))
        # an ordinary merge commit of the two lines, plus the trailer
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^1'), parent_tip)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), self.child_tip)
        self.assertEqual(self.fx.subject(self.fx.distro_bare, tip), f'Merge {CHILD} into {PARENT}')
        self.assertEqual(self.trailer(tip), f'lock-pin tip={meta_tip} parent-pin={self.m4} child-pin={self.m3}')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'foo.txt'), 'child fix')
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'versions/vertex'), '0.0.1')
        self.assertEqual(git(self.fx.distro_bare, 'diff', '--name-only', parent_tip, tip).split(), ['foo.txt', LOCK])
        self.assertIn(f'pin set to the meta-dsensos {PARENT} tip {meta_tip}; pushing {PARENT} directly (outcome A', self.rule_lines(r)[0])
        self.assertIn('post-check ok', r.stdout)
        # the record: a comment on the new parent commit with the rule facts and the run
        comments = self.commit_comments()
        self.assertEqual([(c['repo'], c['sha']) for c in comments], [(DISTRO_REPO, tip)])
        body = comments[0]['body']
        for want in (f'- tip: `{meta_tip}`', f'- pin of `{PARENT}` before: `{self.m4}`', f'- pin of `{CHILD}`: `{self.m3}`',
                     '- check 1 ok', '- check 2 ok', '- check 3 ok', '- check 4 ok', '- check 5 ok', 'direct push, no PR', 'Run: https://'):
            self.assertIn(want, body)
        self.assertIn(f'commented on {tip}', r.stdout)
        # the merge left no branch behind, and a second run has nothing to do
        self.assertNotIn(self.branch(), self.fx.heads(self.fx.distro_bare))
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)
        self.assertEqual(self.distro_tip(), tip)
        self.assertEqual(len(self.commit_comments()), 1)

    def test_outcome_a_when_the_layer_line_already_holds_the_pin(self):
        # the ds-tegra-distro #173 shape: meta-dsensos vertex/main already contains the child's
        # pin and the parent locks that tip; nothing is merged in the layer, the pin stays the tip
        self.child_work(trailing_versions=False)
        meta_tip = self.meta_merged()
        parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=meta_tip)}, 'lock the own tip')
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('already contains the pin', r.stdout)
        tip = self.distro_tip()
        self.assertEqual(self.pin(tip), meta_tip)
        self.assertEqual(self.meta_tip(), meta_tip)
        self.assertEqual(self.trailer(tip), f'lock-pin tip={meta_tip} parent-pin={meta_tip} child-pin={self.m3}')
        self.assertEqual(self.fx.files_in_commit(self.fx.distro_bare, tip), [])   # combined diff: nothing beyond the two parents
        self.assertEqual(git(self.fx.distro_bare, 'diff', '--name-only', parent_tip, tip).split(), ['foo.txt'])
        self.assertEqual(self.fx.prs(), [])

    def test_outcome_a_closes_an_older_conflict_pr(self):
        # an earlier run (the rule off) opened the conflict PR; the child then moved on
        self.both_moved()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.child_tip = self.fx.distro_commit(CHILD, {'foo.txt': 'child fix 2\n'}, 'another child fix')
        write(self.fx.green, self.child_tip + '\n')
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'Superseded by the clean merge {self.distro_tip()}', pr['close_comment'])
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())

    def test_a_failed_commit_comment_is_a_warning(self):
        self.both_moved()
        write(os.path.join(self.fx.state, 'fail-commit-comment'), '')
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'::warning::merge-up: could not comment on {self.distro_tip()}', r.stdout)
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())
        self.assertIn('done: ', r.stdout)

    def install_hook(self, mode):
        hook = os.path.join(self.fx.distro_bare, 'hooks', 'pre-receive')
        write(hook, RETRY_HOOK % {'mark': os.path.join(self.fx.state, 'hook-fired'), 'meta': self.fx.meta_bare, 'mode': mode,
                                  'parent': PARENT, 'lock': LOCK, 'date': '2026-01-02T00:00:00+00:00'})
        os.chmod(hook, 0o755)

    def test_a_rejected_push_refetches_both_parents(self):
        self.both_moved()
        self.install_hook('descend')
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.fx.state, 'hook-fired')))
        self.assertIn('ds-tegra-distro: push rejected (attempt 1), refetching', r.stdout)
        meta_tip, tip = self.meta_tip(), self.distro_tip()
        self.assertIn(f'meta-dsensos: {PARENT} moved to {meta_tip} (it descends from ', r.stdout)
        # the second attempt re-read the parent's pin (the hook moved it) and used the refetched tip
        moved_parent = git(self.fx.distro_bare, 'rev-parse', tip + '^1')
        self.assertEqual(self.fx.subject(self.fx.distro_bare, moved_parent), 'pin the new layer tip')
        self.assertEqual(self.pin(moved_parent), meta_tip)
        self.assertEqual(self.pin(tip), meta_tip)
        self.assertEqual(self.trailer(tip), f'lock-pin tip={meta_tip} parent-pin={meta_tip} child-pin={self.m3}')
        self.assertEqual(self.fx.subject(self.fx.meta_bare, meta_tip), 'a concurrent layer commit')
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), self.child_tip)
        self.assertEqual(len(self.rule_lines(r)), 2)   # evaluated on both attempts
        self.assertEqual(self.fx.prs(), [])
        self.assertEqual([c['sha'] for c in self.commit_comments()], [tip])


    def test_with_the_variable_absent_a_rejected_push_refetches_no_layer_line(self):
        # Phase 0: the retry refetches the distro parent only. The layer line is the rule's business.
        self.child_work(trailing_versions=False)   # a clean merge, until the hook moves the parent's lock
        self.install_hook('descend')
        for auto, refetched in ((None, False), ('shadow', True)):
            with self.subTest(auto=auto):
                if os.path.exists(os.path.join(self.fx.state, 'hook-fired')):
                    self.new_fixture()
                    self.child_work(trailing_versions=False)
                    self.install_hook('descend')
                r = self.up(auto=auto)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn('ds-tegra-distro: push rejected (attempt 1), refetching', r.stdout)
                self.assertEqual(f'meta-dsensos: {PARENT} moved to ' in r.stdout, refetched, r.stdout)
                # the second attempt conflicts on the lockfile the hook moved: the conflict PR, markers and all
                self.assertEqual(self.fx.prs()[0]['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
                self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, self.branch(), LOCK))


GREEN = [{'name': 'pr-checks', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'},
         {'name': 'pin-propagation-safety', 'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]
NO_WAIT = {'MERGE_UP_LOCK_WAIT_SECONDS': '0', 'MERGE_UP_LOCK_POLL_SECONDS': '0'}   # what LockCase.setUp sets, spelled out


class LockBotPrTests(LockCase):
    """Outcome B: the lockfile is the only conflict, but the layer tip holds more than the two
    pins. A PR without markers that the bot checks and merges itself."""

    def up(self, *args, env=None, **kw):
        return LockCase.up(self, *args, env=dict(NO_WAIT, **(env or {})), **kw)

    def checks(self, rollup, number=1):
        write(os.path.join(self.fx.state, f'pr-{number}-review.json'), json.dumps({'statusCheckRollup': rollup}))

    def view_hook(self, script):
        write(os.path.join(self.fx.state, 'pr-view-hook'), script)

    def bot_commit(self):
        return self.fx.heads(self.fx.distro_bare)[self.branch()]

    def assert_open_and_untouched(self, r, parent_tip, why):
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'OPEN')
        self.assertEqual(self.distro_tip(), parent_tip)
        self.assertNotIn('pr merge', self.fx.gh_log())
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn(f'bot-finished PR {pr["url"]} was not merged: ', errors[0])
        self.assertIn(why, errors[0])
        self.assertIn('The PR stays open', errors[0])
        self.assertIn(f'child={CHILD} pr=1', errors[0])

    def test_outcome_b_opens_checks_and_merges_the_pr(self):
        self.tip_holds_more()
        parent_tip = self.parent_tip
        self.checks(GREEN)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        meta_tip = self.meta_tip()
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        pr = prs[0]
        self.assertEqual((pr['repo'], pr['base'], pr['head'], pr['state']), (DISTRO_REPO, PARENT, self.branch(), 'MERGED'))
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (lockfile pin set by rule)')
        # the PR head: the parent plus the merge of the child, the pin on the tip, no marker
        bot = self.bot_commit()
        self.assertEqual(pr['merged_head'], bot)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', bot + '^1', bot + '^2').split(), [parent_tip, self.child_tip])
        self.assertEqual(self.lockfile(bot), LOCK_YML.format(pin=meta_tip))
        grep = subprocess.run(['git', '-C', self.fx.distro_bare, 'grep', '-l', '-E', r'^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )', bot], capture_output=True, text=True)
        self.assertEqual((grep.returncode, grep.stdout), (1, ''))
        self.assertEqual(self.fx.subject(self.fx.distro_bare, bot), f'Merge {CHILD} into {PARENT}')
        self.assertEqual(git(self.fx.distro_bare, 'log', '-1', '--format=%(trailers:key=Merge-Up-Rule,valueonly)', bot),
                         f'lock-pin tip={meta_tip} parent-pin={self.m4} child-pin={self.m3}')
        # the body is the record: every layer commit in the tip that neither pin contains, and the checks
        body = pr['body']
        self.assertIn(f'- `{self.m5[:7]}` layer work nobody locked', body)
        self.assertIn(f'- `{meta_tip[:7]}` Merge {CHILD} into {PARENT}', body)
        self.assertIn('neither pin contains (2, merge commits included)', body)
        self.assertIn(f'- tip (the new pin): `{meta_tip}`', body)
        self.assertIn('1 build path(s) differ', body)
        self.assertIn('- check 4 ok', body)
        self.assertIn('- check 5 FAILED', body)
        self.assertIn('once `pr-checks` and `pin-propagation-safety` succeed on `%s`' % bot[:7], body)
        self.assertIn('up to 15 minutes', body)
        self.assertNotIn('<<<<<<<', body)
        # merged by the bot, pinned to its own commit, with no review at all
        self.assertIn(f'pr merge 1 -R {DISTRO_REPO} --merge --match-head-commit {bot}', self.fx.gh_log())
        tip = self.distro_tip()
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^1', tip + '^2').split(), [parent_tip, bot])
        self.assertEqual(self.pin(tip), meta_tip)
        self.assertIn('post-check ok', r.stdout)
        self.assertIn(f'done: {CHILD} merged into {PARENT} at {tip} (bot-finished PR 1)', r.stdout)
        self.assertEqual(len(pr['comments']), 1)
        self.assertIn('with no approving review', pr['comments'][0])
        self.assertIn('pr-checks, pin-propagation-safety succeeded', pr['comments'][0])
        self.assertEqual(self.commit_comments(), [])   # the PR is the record, not a commit comment
        # a second run has nothing to do
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)

    def test_outcome_b_waits_for_checks_that_are_still_running(self):
        self.tip_holds_more()
        running = [{'name': 'pr-checks', 'status': 'IN_PROGRESS', 'conclusion': None}]
        write(os.path.join(self.fx.state, 'pr-1-review.json'),
              json.dumps([{'statusCheckRollup': []}, {'statusCheckRollup': running}, {'statusCheckRollup': GREEN}]))
        r = self.up(env={'MERGE_UP_LOCK_WAIT_SECONDS': '60'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')
        self.assertEqual(self.fx.gh_log().count('pr view 1'), 3)
        self.assertIn('(at most 60s)', r.stdout)

    def test_outcome_b_with_a_red_check_stays_open(self):
        self.tip_holds_more()
        self.checks([GREEN[0], {'name': 'pin-propagation-safety', 'status': 'COMPLETED', 'conclusion': 'FAILURE'}])
        r = self.up()
        self.assert_open_and_untouched(r, self.parent_tip, 'a required check failed')
        self.assertIn('pin-propagation-safety: FAILURE', r.stdout)
        # the PR is in the new shape all the same: no marker to resolve, a person can approve and finish it
        self.assertEqual(self.pin(self.bot_commit()), self.meta_tip())

    def test_outcome_b_that_times_out_stays_open(self):
        self.tip_holds_more()
        self.checks([GREEN[0]])   # one required check never reports
        r = self.up()
        self.assert_open_and_untouched(r, self.parent_tip, 'the required checks were not green')
        self.assertIn('after 0s (pin-propagation-safety: not reported yet)', r.stdout)

    def test_outcome_b_with_a_moved_head_is_not_merged(self):
        self.tip_holds_more()
        self.checks(GREEN)
        # someone pushes to the PR branch after the bot opened it and before it reads the checks
        self.view_hook(f"""set -e
cd {self.fx.distro}
git fetch -q origin
git checkout -q -B {self.branch()} origin/{self.branch()}
echo extra > foo.txt
git -c user.name=mallory -c user.email=m@x commit -q -am 'a push the bot did not make'
git push -q origin {self.branch()}
""")
        r = self.up()
        pushed = self.bot_commit()
        self.assert_open_and_untouched(r, self.parent_tip, f'the PR head is {pushed}, not the commit the bot pushed')
        self.assertEqual(self.fx.subject(self.fx.distro_bare, pushed), 'a push the bot did not make')
        # a later run does not finish it either, green checks or not: the branch is not the bot's commit
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'not applied: {self.branch()} already exists on origin at {pushed[:7]} and is not the commit this run computes', r.stdout)
        self.assertIn(f'{self.branch()} already exists on origin; left as it is', r.stdout)
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertEqual(self.bot_commit(), pushed)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_outcome_b_with_a_moved_parent_is_not_merged(self):
        self.tip_holds_more()
        self.checks(GREEN)
        self.view_hook(f"""set -e
cd {self.fx.distro}
git fetch -q origin
git checkout -q -B {PARENT} origin/{PARENT}
echo doc > NOTES.txt
git add NOTES.txt
git -c user.name=dev -c user.email=d@x commit -q -m 'the parent moves while the checks run'
git push -q origin {PARENT}
""")
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'{PARENT} moved from {self.parent_tip[:7]} to {self.distro_tip()[:7]} while the checks ran', r.stdout)
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')

    def test_outcome_b_when_github_refuses_the_merge(self):
        self.tip_holds_more()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'fail-pr-merge'), '')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('was not merged: GitHub refused the merge (GraphQL: Repository rule violations found', r.stdout)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_the_bot_pr_says_to_dispatch_on_the_default_branch(self):
        self.tip_holds_more()
        self.checks([{'name': 'pr-checks', 'conclusion': 'FAILURE'}])
        r = self.up(env={'DEFAULT_BRANCH': PARENT})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'dispatch release-merge-up.yml on the default branch ({PARENT}) with child=`{CHILD}`', self.fx.prs()[0]['body'])
        self.assertIn(f'dispatches release-merge-up.yml on the default branch ({PARENT}) with child={CHILD} pr=1', r.stdout)

    def test_a_bot_merged_pr_left_open_is_closed(self):
        self.tip_holds_more()
        self.checks(GREEN)
        write(os.path.join(self.fx.state, 'leave-open-after-merge'), '')
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'CLOSED')
        self.assertIn(f'the merge commit {self.distro_tip()}', pr['close_comment'])
        self.assertNotIn('Superseded', pr['close_comment'])

    def test_a_later_run_finishes_an_open_bot_pr(self):
        self.tip_holds_more()
        r = self.up()   # no check has reported: the wait runs out
        self.assert_open_and_untouched(r, self.parent_tip, 'the required checks were not green')
        bot = self.bot_commit()
        log_before = self.fx.gh_log()
        # the checks go green later; the next merge-up run of the child finishes the same PR
        self.checks(GREEN)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'{self.branch()} is already on origin at {bot[:7]}: the commit this run computes, untouched; reusing it', r.stdout)
        self.assertEqual(self.bot_commit(), bot)   # never re-pushed
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED')])
        new_calls = self.fx.gh_log()[len(log_before):]
        self.assertNotIn('pr create', new_calls)
        self.assertIn(f'--match-head-commit {bot}', new_calls)
        tip = self.distro_tip()
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), bot)
        self.assertEqual(self.pin(tip), self.meta_tip())

    def test_an_open_bot_pr_on_an_older_parent_is_not_finished(self):
        # The parent moved after the bot opened the PR, by a commit that changes no file, so the
        # tree this run computes is the tree on the branch. The checks vouched for that commit
        # on the old parent: it is not the commit this run computes, and the bot does not merge it.
        self.tip_holds_more()
        r = self.up()   # no check has reported: the wait runs out
        self.assert_open_and_untouched(r, self.parent_tip, 'the required checks were not green')
        bot = self.bot_commit()
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', PARENT, f'origin/{PARENT}')
        git(self.fx.distro, 'commit', '-q', '--allow-empty', '-m', 'the parent moves, no file changes')
        git(self.fx.distro, 'push', '-q', 'origin', PARENT)
        moved = git(self.fx.distro, 'rev-parse', 'HEAD')
        git(self.fx.distro, 'checkout', '-q', '--detach')
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', moved + '^{tree}'), git(self.fx.distro_bare, 'rev-parse', self.parent_tip + '^{tree}'))
        self.checks(GREEN)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('untouched; reusing it', r.stdout)
        self.assertIn(f'not applied: {self.branch()} already exists on origin at {bot[:7]} and is not the commit this run computes', r.stdout)
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertEqual(self.distro_tip(), moved)
        self.assertEqual(self.bot_commit(), bot)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])

    def test_a_person_can_finish_an_open_bot_pr(self):
        self.tip_holds_more()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        bot = self.bot_commit()
        t.MergeUpTests.approve(self, 1, pusher='ds-os-release-bot')
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('via PR 1', r.stdout)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)

    def test_a_hand_resolved_layer_merge_takes_outcome_b(self):
        # the ds-tegra-distro #170 shape: the layer lines conflicted, a person resolved the layer PR,
        # and the tip holds that resolution. No commit count sees it; the tree comparison does.
        self.child_work(trailing_versions=False)
        m4 = self.fx.meta_commit(PARENT, 'main layer change\n', 'main layer change')
        parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=m4)}, 'pin m4')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        branch = f'merge-up/{CHILD}/{self.m3[:7]}'
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', branch, f'origin/{branch}')
        write(os.path.join(self.fx.meta_work, 'layer.txt'), 'both\n')
        git(self.fx.meta_work, 'commit', '-q', '-am', 'resolve')
        git(self.fx.meta_work, 'push', '-q', 'origin', branch)
        t.MergeUpTests.approve(self, 1)
        # the dry run of the finish: the layer tip does not exist yet
        r = self.up('--pr', '1', dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'dry run: would set the pin to the new meta-dsensos {PARENT} tip', self.rule_lines(r)[0])
        self.checks(GREEN, number=2)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        meta_tip = self.meta_tip()
        self.assertIn('outcome B, bot-finished PR', self.rule_lines(r)[0])
        self.assertIn('do not merge clean', self.rule_lines(r)[0])
        prs = {p['number']: p for p in self.fx.prs()}
        self.assertEqual((prs[1]['repo'], prs[1]['state']), (META_REPO, 'MERGED'))
        self.assertEqual((prs[2]['repo'], prs[2]['state']), (DISTRO_REPO, 'MERGED'))
        self.assertEqual(self.pin(self.distro_tip()), meta_tip)
        self.assertTrue(self.anc(self.fx.meta_bare, self.m3, meta_tip) and self.anc(self.fx.meta_bare, m4, meta_tip))
        self.assertEqual(git(self.fx.meta_bare, 'rev-list', '--count', '--no-merges', meta_tip, f'^{m4}', f'^{self.m3}'), '1')
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^1'), parent_tip)

    def log_pushes(self):
        """Record every ref the distro bare repo receives, in order: '<old> <new> <ref>' lines."""
        path = os.path.join(self.fx.state, 'distro-pushes')
        hook = os.path.join(self.fx.distro_bare, 'hooks', 'pre-receive')
        write(hook, f'#!/bin/sh\ncat >> {path}\n')
        os.chmod(hook, 0o755)
        return path

    def test_nothing_new_in_the_tip_goes_through_a_bot_pr_as_shipped(self):
        # The shipped config (lock_pin.exact=bot-pr, 2026-11-18): the lockfile is the only conflict
        # and the tip builds what the two pins build. No direct push: a PR, the checks, the merge.
        parent_tip = self.both_moved()
        self.checks(GREEN)
        pushes = self.log_pushes()
        seen = os.path.join(self.fx.state, 'parent-when-the-checks-were-read')
        self.view_hook(f'git -C {self.fx.distro_bare} rev-parse refs/heads/{PARENT} > {seen}\n')
        summary = os.path.join(self.fx.top, 'summary.md')
        r = self.up(env={'MERGE_UP_SUMMARY': summary})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('config:             finish.strict=false, lock_pin.verify_finish=false, lock_pin.exact=bot-pr, '
                      'lock_pin.extra=bot-pr (release/config.json at that ref)', r.stdout)
        meta_tip, tip, bot = self.meta_tip(), self.distro_tip(), self.bot_commit()
        # the PR, opened by the bot on merge-up/<child>/<sha7>, without markers
        prs = self.fx.prs()
        self.assertEqual(len(prs), 1)
        pr = prs[0]
        self.assertEqual((pr['repo'], pr['base'], pr['head'], pr['state']), (DISTRO_REPO, PARENT, self.branch(), 'MERGED'))
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (lockfile pin set by rule)')
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', bot + '^1', bot + '^2').split(), [parent_tip, self.child_tip])
        self.assertEqual(self.lockfile(bot), LOCK_YML.format(pin=meta_tip))
        self.assertEqual(self.fx.subject(self.fx.distro_bare, bot), f'Merge {CHILD} into {PARENT}')
        self.assertEqual(git(self.fx.distro_bare, 'log', '-1', '--format=%(trailers:key=Merge-Up-Rule,valueonly)', bot),
                         f'lock-pin tip={meta_tip} parent-pin={self.m4} child-pin={self.m3}')
        body = pr['body']
        self.assertIn('The tip builds the same as the plain merge of the two pins. This is a PR and not a direct push '
                      'because `merge_up.lock_pin.exact` is `bot-pr`.', body)
        for n in range(1, 6):
            self.assertIn(f'- check {n} ok', body)
        self.assertNotIn('FAILED', body)
        # the only layer commit neither pin holds is the layer merge of this run
        self.assertIn('neither pin contains (1, merge commits included)', body)
        self.assertIn(f'- `{meta_tip[:7]}` Merge {CHILD} into {PARENT}', body)
        self.assertIn('once `pr-checks` and `pin-propagation-safety` succeed on `%s`' % bot[:7], body)
        self.assertNotIn('<<<<<<<', body)
        # opened, then the checks read, then merged pinned to the bot's commit: in that order
        log = self.fx.gh_log()
        create, view, merge = log.index('pr create'), log.index('pr view 1'), log.index('pr merge 1')
        self.assertTrue(create < view < merge, log)
        self.assertIn(f'pr merge 1 -R {DISTRO_REPO} --merge --match-head-commit {bot}', log)
        self.assertEqual(pr['merged_head'], bot)
        self.assertIn(f'waiting for pr-checks,pin-propagation-safety on {bot[:7]}', r.stdout)
        self.assertIn(f'required checks green on {bot[:7]}', r.stdout)
        # nothing reached the parent before that merge: it still stood at its old tip while the
        # checks were read, and the repo received two pushes, the branch and then GitHub's merge
        with open(seen) as f:
            self.assertEqual(f.read().strip(), parent_tip)
        with open(pushes) as f:
            received = [line.split() for line in f.read().splitlines()]
        self.assertEqual([(old, ref) for old, new, ref in received],
                         [('0' * 40, f'refs/heads/{self.branch()}'), (parent_tip, f'refs/heads/{PARENT}')])
        self.assertEqual((received[0][1], received[1][1]), (bot, tip))
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^1', tip + '^2').split(), [parent_tip, bot])
        self.assertEqual(self.pin(tip), meta_tip)
        # the record is the PR: its comment, no commit comment, and the log says which path it was
        self.assertEqual(self.rule_lines(r), [
            f'pin set to the meta-dsensos {PARENT} tip {meta_tip} in {pr["url"]} (outcome B, bot-finished PR: checks 1 to 5 pass, '
            "the tip's tree equals the plain merge of the two pins (ignoring .ai/, docs/, *.md); "
            'a PR and not a direct push because lock_pin.exact=bot-pr)'])
        self.assertNotIn('directly', r.stdout)
        self.assertIn(f'merged PR {DISTRO_REPO}#1 (head {bot}, no approval: the lockfile pin rule and the required checks)', r.stdout)
        self.assertIn('post-check ok', r.stdout)
        self.assertIn(f'done: {CHILD} merged into {PARENT} at {tip} (bot-finished PR 1)', r.stdout)
        self.assertEqual(len(pr['comments']), 1)
        self.assertIn('with no approving review', pr['comments'][0])
        self.assertEqual(self.commit_comments(), [])
        with open(summary) as f:
            text = f.read()
        self.assertIn(f'- lockfile pin rule: pin set to the meta-dsensos {PARENT} tip {meta_tip} in {pr["url"]} (outcome B, bot-finished PR: checks 1 to 5 pass', text)
        self.assertIn(f'- bot-finished PR {pr["url"]} merged at {tip}', text)
        # a second run has nothing to do
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('nothing to merge', r.stdout)

    def test_nothing_new_in_the_tip_without_green_checks_is_not_merged_and_not_pushed(self):
        # the same shape with no check reported: the PR stays open and the parent is not touched
        parent_tip = self.both_moved()
        r = self.up()
        self.assert_open_and_untouched(r, parent_tip, 'the required checks were not green')
        self.assertEqual(self.commit_comments(), [])
        bot = self.bot_commit()
        # the next run of the child finishes that PR, once the checks are green
        self.checks(GREEN)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'{self.branch()} is already on origin at {bot[:7]}: the commit this run computes, untouched; reusing it', r.stdout)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED')])
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^1', self.distro_tip() + '^2').split(), [parent_tip, bot])

    def test_an_explicit_exact_bot_pr_is_the_shipped_path(self):
        self.both_moved()
        self.checks(GREEN)
        r = self.up(config={'lock_pin.exact': 'bot-pr'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('lock_pin.exact=bot-pr', r.stdout)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'MERGED')
        self.assertIn('because `merge_up.lock_pin.exact` is `bot-pr`', pr['body'])
        self.assertIn('- check 5 ok', pr['body'])
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())
        self.assertEqual(self.commit_comments(), [])

    def test_lock_pin_extra_pr_waits_for_one_approval(self):
        scripts = self.fx.scripts_copy({'lock_pin.extra': 'pr'})
        merge_up = os.path.join(scripts, 'merge-up.sh')
        self.tip_holds_more()
        self.checks(GREEN)
        r = self.fx.run(merge_up, '--child', CHILD, extra_env=dict(NO_WAIT, DS_MERGE_UP_AUTOMATION='lock-pin'))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        pr = self.fx.prs()[0]
        self.assertEqual(pr['state'], 'OPEN')
        self.assertNotIn('pr merge', self.fx.gh_log())
        self.assertNotIn('pr view', self.fx.gh_log())   # no wait either
        self.assertIn('so the bot does not merge this PR. It needs one approval', pr['body'])
        self.assertIn('it waits for one approval', r.stdout)
        self.assertEqual(self.pin(self.bot_commit()), self.meta_tip())
        self.assertEqual(self.distro_tip(), self.parent_tip)
        # one approval, then the ordinary finish
        t.MergeUpTests.approve(self, 1, pusher='ds-os-release-bot')
        r = self.fx.run(merge_up, '--child', CHILD, '--pr', '1', extra_env={'DS_MERGE_UP_AUTOMATION': 'lock-pin'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())

    def test_outcome_b_closes_an_older_conflict_pr(self):
        self.tip_holds_more()
        r = self.up(auto=None)   # the rule off: the phase-0 conflict PR
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.child_tip = self.green_sha = self.fx.distro_commit(CHILD, {'foo.txt': 'child fix 2\n'}, 'another child fix')
        write(self.fx.green, self.child_tip + '\n')
        self.checks(GREEN, number=2)
        r = self.up()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        prs = {p['number']: p for p in self.fx.prs()}
        self.assertEqual((prs[1]['state'], prs[2]['state']), ('CLOSED', 'MERGED'))
        self.assertIn(f'Superseded by {prs[2]["url"]}', prs[1]['close_comment'])

    def test_the_merge_job_outlives_the_wait(self):
        with open(os.path.join(os.path.dirname(SCRIPTS), '.github/workflows/release-merge-up.yml')) as f:
            text = f.read()
        merge = text[text.index('\n  merge:\n'):text.index('\n  merge-up-skipped:\n')]
        timeout = int(re.search(r'^    timeout-minutes: (\d+)$', merge, re.M).group(1))
        wait = release_git.merge_up_config()['lock_pin']['wait_minutes']
        self.assertGreaterEqual(timeout, wait + 30, 'the merge job must outlive lock_pin.wait_minutes with room for the merge itself')


ALICE = {'login': 'alice', 'type': 'User'}
BOT = {'login': 'ds-os-release-bot[bot]', 'type': 'Bot'}
# How a run was started no longer decides anything: every case that stops is run both ways.
AUTOMATIC, MANUAL = {'GITHUB_EVENT_NAME': 'workflow_run'}, {'GITHUB_EVENT_NAME': 'workflow_dispatch'}
PR1 = 'https://github.com/ds/distro/pull/1'
TOUCHES = ('pr create', 'pr merge', 'pr close', 'pr comment', 'pr edit')


class LockPersonCase(LockCase):
    """A bot-finished PR (outcome B) that waits, and what a person does to it. No test of its own."""

    # the helpers of the outcome B tests, without their cases
    checks, view_hook, bot_commit = LockBotPrTests.checks, LockBotPrTests.view_hook, LockBotPrTests.bot_commit
    assert_open_and_untouched = LockBotPrTests.assert_open_and_untouched

    def up(self, *args, **kw):
        self.mark = self.fx.gh_log()   # what gh was asked before this run
        return LockBotPrTests.up(self, *args, **kw)

    def waiting_pr(self):
        """Outcome B whose wait ran out: PR 1 open on the bot's commit. Every check is green from here on."""
        self.tip_holds_more()
        r = self.up(env=AUTOMATIC)
        self.assert_open_and_untouched(r, self.parent_tip, 'the required checks were not green')
        for number in (1, 2):
            self.checks(GREEN, number)
        return self.bot_commit()

    def calls(self, since):
        return self.fx.gh_log()[len(since):]

    def assert_dead(self, r, why, states, url=PR1, since=None):
        """Nothing opened, pushed, merged, reopened or commented; red, with one error that names
        the closed PR and the two ways forward."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], states)
        self.assertEqual(self.distro_tip(), self.parent_tip)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [t.dead_error('ds-tegra-distro', self.branch(), why, url)])
        self.assertEqual(self.rule_lines(r), [])   # no rule is applied to a dead branch, and none says it would be
        for call in TOUCHES:
            self.assertNotIn(call, self.calls(self.mark if since is None else since))


class LockDeadBranchTests(LockPersonCase):
    """A PR a person closed makes merge-up/<child>/<sha7> dead (the owner's decision of
    2026-11-18): no run opens a PR on it, pushes to it or merges it, whether it was started
    by workflow_run or dispatched by hand. Here for the lockfile pin rule's own PRs."""

    def test_a_bot_pr_a_person_closed_is_dead_for_every_run(self):
        bot = self.waiting_pr()
        self.assertIn('To stop the bot, request changes on this PR or close it.', self.fx.prs()[0]['body'])
        self.assertIn(f'A PR closed without merging closes `{self.branch()}` for the bot: no run, automatic or dispatched by hand, '
                      f'opens a new PR on it or merges it. The next green commit on `{CHILD}` gets a new branch and a new PR, '
                      'and reopening this PR brings this branch back.', self.fx.prs()[0]['body'])
        self.assertNotIn('starts the bot again', self.fx.prs()[0]['body'])
        self.fx.close_pr(1, by=ALICE)
        before = self.fx.prs()
        for env in (AUTOMATIC, MANUAL, {}):   # workflow_run, workflow_dispatch, and a run that names no event
            with self.subTest(env=env):
                log = self.fx.gh_log()
                summary = os.path.join(self.fx.top, 'summary.md')
                r = self.up(env=dict(env, MERGE_UP_SUMMARY=summary))
                self.assert_dead(r, 'alice closed it without merging', [(1, 'CLOSED')], since=log)
                self.assertEqual(self.bot_commit(), bot)
                self.assertEqual(self.fx.prs(), before)   # the closed PR is neither reopened nor commented on
                self.assertNotIn('dispatched by hand, which restarts the bot', r.stdout)
                with open(summary) as f:
                    self.assertIn(f'- ds-tegra-distro: a person closed {PR1} without merging (alice closed it without merging), '
                                  f'so {self.branch()} is treated as closed. Nothing was opened, pushed, merged or resolved by rule on {self.branch()}', f.read())

    def test_a_dry_run_says_what_it_would_not_do(self):
        self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        for env in (AUTOMATIC, MANUAL):
            before = self.snapshot()
            r = self.up(env=env, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn(f'ds-tegra-distro: dry run: a person closed {PR1} without merging (alice closed it without merging), so {self.branch()} is treated as closed. '
                          f'A real run would open no pull request on {self.branch()}, push nothing to it, merge nothing from it and apply no merge-up rule to it, '
                          f'and would end with an error. Two ways forward: the next green commit on {CHILD} gets a new branch and a new PR by itself; or a person reopens {PR1}', r.stdout)
            self.assertNotIn('would set the pin', r.stdout)
            self.assertNotIn('::error::', r.stdout)
            self.assertIn('dry run: nothing pushed', r.stdout)
            after = self.snapshot()
            self.assertEqual((after['distro'], after['meta'], after['prs']), (before['distro'], before['meta'], before['prs']))

    def test_a_closed_pr_whose_branch_was_deleted_is_not_pushed_again(self):
        self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        git(self.fx.distro_bare, 'update-ref', '-d', f'refs/heads/{self.branch()}')
        for env in (AUTOMATIC, MANUAL):
            self.assert_dead(self.up(env=env), 'alice closed it without merging', [(1, 'CLOSED')])
            self.assertNotIn(self.branch(), self.fx.heads(self.fx.distro_bare))

    def test_a_closer_github_does_not_name_counts_as_a_person(self):
        self.waiting_pr()
        self.fx.close_pr(1)
        self.assert_dead(self.up(env=AUTOMATIC), 'someone GitHub does not name closed it without merging', [(1, 'CLOSED')])

    def test_the_bots_own_supersession_is_not_a_person(self):
        bot = self.waiting_pr()
        self.fx.close_pr(1, by=ALICE, comment='why is this open?')
        self.fx.close_pr(1, by=BOT, comment=f'Superseded by the clean merge: `{CHILD}` moved on, and release-merge-up does not finish a PR whose head lacks the current tip.')
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'MERGED')])
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)
        self.assertNotIn('is treated as closed', r.stdout)

    def test_a_close_that_only_looks_like_a_supersession_is_a_persons(self):
        # a bot that closed it with no such comment, and a person whose comment borrows the words
        for by, comment_by, comment, who in ((BOT, BOT, 'Closing.', 'ds-os-release-bot[bot]'),
                                             (BOT, ALICE, 'Superseded by my own PR', 'ds-os-release-bot[bot]'),
                                             (ALICE, ALICE, 'Superseded by my own PR', 'alice')):
            with self.subTest(by=by, comment_by=comment_by):
                self.new_fixture()
                self.waiting_pr()
                self.fx.close_pr(1, by=comment_by, comment=comment)
                self.fx.close_pr(1, by=by)
                self.assert_dead(self.up(env=AUTOMATIC), f'{who} closed it without merging', [(1, 'CLOSED')])

    def test_a_reopened_pr_is_not_dead(self):
        bot = self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        self.assert_dead(self.up(env=AUTOMATIC), 'alice closed it without merging', [(1, 'CLOSED')])
        # a person reopens it: the branch counts as open again, and the next run finishes the same PR
        self.fx.reopen_pr(1)
        log = self.fx.gh_log()
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED')])
        self.assertNotIn('pr create', self.calls(log))
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)
        self.assertNotIn('is treated as closed', r.stdout)

    def test_an_older_bot_finished_pr_a_person_reopened_is_finished(self):
        # a person closed PR 1, opened PR 2 on the same head by hand and closed that too: dead, by PR 2
        bot = self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        t.MergeUpTests.inject_pr(self, 2, self.branch(), bot)
        self.fx.close_pr(2, by=ALICE)
        self.assert_dead(self.up(env=AUTOMATIC), 'alice closed it without merging', [(1, 'CLOSED'), (2, 'CLOSED')],
                         url=f'https://github.com/{DISTRO_REPO}/pull/2')
        # PR 1 reopened: the head has an open PR, whatever its number, and an automatic run finishes it
        self.fx.reopen_pr(1)
        log = self.fx.gh_log()
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED'), (2, 'CLOSED')])
        self.assertNotIn('pr create', self.calls(log))
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)
        self.assertNotIn('is treated as closed', r.stdout)

    def test_a_new_child_commit_gets_a_new_branch(self):
        old = self.waiting_pr()
        old_branch = self.branch()
        self.fx.close_pr(1, by=ALICE)
        self.assert_dead(self.up(env=AUTOMATIC), 'alice closed it without merging', [(1, 'CLOSED')])
        closed = self.fx.prs()[0]
        # the child moves: a new sha7, a new branch, no memory of the dead one
        self.green_sha = self.child_tip = self.fx.distro_commit(CHILD, {'child-only.txt': 'more child work\n'}, 'more child work')
        write(self.fx.green, self.green_sha + '\n')
        self.assertNotEqual(self.branch(), old_branch)
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state'], p['head']) for p in self.fx.prs()], [(1, 'CLOSED', old_branch), (2, 'MERGED', self.branch())])
        self.assertNotIn('is treated as closed', r.stdout)
        self.assertNotEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), old)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[old_branch], old)   # the dead branch is left as it is
        self.assertEqual(self.fx.prs()[0], closed)                              # and so is its PR

    def test_only_the_most_recent_pr_of_the_head_counts(self):
        # a person closed PR 1 and then opened PR 2 on the same head by hand: the head has an open
        # PR again, so it is not dead, and the bot finishes PR 2 as it would finish its own
        bot = self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        t.MergeUpTests.inject_pr(self, 2, self.branch(), bot)
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'CLOSED'), (2, 'MERGED')])

    def test_what_cannot_be_read_counts_as_dead(self):
        self.waiting_pr()
        for env in (AUTOMATIC, MANUAL):
            write(os.path.join(self.fx.state, 'fail-pr-list-all'), '')
            r = self.up(env=env)
            self.assert_dead(r, f'the pull requests of {self.branch()} in {DISTRO_REPO} could not be read (HTTP 502: Bad Gateway), so nothing shows that no person closed one',
                             [(1, 'OPEN')], url=None)
            os.remove(os.path.join(self.fx.state, 'fail-pr-list-all'))
            self.fx.supersede_pr(1)   # not dead, if only it could be read
            write(os.path.join(self.fx.state, 'fail-issues'), '')
            self.assert_dead(self.up(env=env), 'who closed it could not be read (HTTP 502: Bad Gateway)', [(1, 'CLOSED')])
            os.remove(os.path.join(self.fx.state, 'fail-issues'))
            self.fx.reopen_pr(1)
        # readable again: the run goes on as normal
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')

    def test_a_pr_that_waits_for_an_approval_is_dead_too(self):
        # lock_pin.extra=pr: the bot never merges that PR, and a person's close still closes the branch
        scripts = self.fx.scripts_copy({'lock_pin.extra': 'pr'})
        self.tip_holds_more()
        run = lambda env: self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD,
                                      extra_env=dict(NO_WAIT, DS_MERGE_UP_AUTOMATION='lock-pin', **env))
        self.assertEqual(run(AUTOMATIC).returncode, 1)
        self.assertIn('It needs one approval', self.fx.prs()[0]['body'])
        self.fx.close_pr(1, by=ALICE)
        for env in (AUTOMATIC, MANUAL):
            log = self.fx.gh_log()
            self.assert_dead(run(env), 'alice closed it without merging', [(1, 'CLOSED')], since=log)
        # reopened, it is finished with one approval as before
        self.fx.reopen_pr(1)
        t.MergeUpTests.approve(self, 1, pusher='ds-os-release-bot')
        r = self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, '--pr', '1', extra_env={'DS_MERGE_UP_AUTOMATION': 'lock-pin'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')

    def test_a_finish_of_a_closed_pr_is_refused_as_before(self):
        self.waiting_pr()
        self.fx.close_pr(1, by=ALICE)
        t.MergeUpTests.approve(self, 1, pusher='ds-os-release-bot')
        r = self.up('--pr', '1', env=MANUAL)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'PR #1 is not an open merge-up/{CHILD}/* PR against {PARENT}', r.stdout)
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_the_shipped_bot_pr_for_nothing_new_in_the_tip_is_dead_too(self):
        # As shipped (lock_pin.exact=bot-pr) a lockfile-only conflict with nothing new in the tip
        # is a bot-finished PR on merge-up/<child>/<sha7>. A person closes it: no new PR, no
        # merge, and no direct push of the parent either, however the next run was started.
        self.both_moved()
        r = self.up(env=AUTOMATIC)
        self.assert_open_and_untouched(r, self.parent_tip, 'the required checks were not green')
        self.assertIn('because `merge_up.lock_pin.exact` is `bot-pr`', self.fx.prs()[0]['body'])
        self.checks(GREEN)
        bot = self.bot_commit()
        self.fx.close_pr(1, by=ALICE)
        before = self.fx.prs()
        for env in (AUTOMATIC, MANUAL, {}):
            with self.subTest(env=env):
                log = self.fx.gh_log()
                r = self.up(env=env)
                self.assert_dead(r, 'alice closed it without merging', [(1, 'CLOSED')], since=log)   # the parent is where it was
                self.assertNotIn('directly', r.stdout)
                self.assertEqual(self.bot_commit(), bot)
                self.assertEqual(self.fx.prs(), before)
                self.assertEqual(self.commit_comments(), [])
        # a dry run says so and changes nothing
        snap = self.snapshot()
        r = self.up(env=AUTOMATIC, dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'ds-tegra-distro: dry run: a person closed {PR1} without merging (alice closed it without merging), so {self.branch()} is treated as closed.', r.stdout)
        self.assertNotIn('would set the pin', r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta'], after['prs']), (snap['distro'], snap['meta'], snap['prs']))
        # reopened, the next run finishes the same PR
        self.fx.reopen_pr(1)
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED')])
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)

    def test_a_closed_conflict_pr_holds_up_the_shipped_bot_pr(self):
        # An earlier run without the rule left a conflict PR on this sha's branch and a person
        # closed it. As shipped the rule's PR would live on that same branch, so the run stops:
        # the opposite of lock_pin.exact=push, which needs no branch (the next test).
        self.both_moved()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.fx.close_pr(1, by=ALICE)
        closed, head = self.fx.prs()[0], self.bot_commit()
        for env in (AUTOMATIC, MANUAL):
            r = self.up(env=env)
            self.assert_dead(r, 'alice closed it without merging', [(1, 'CLOSED')])
            self.assertNotIn('directly', r.stdout)
            self.assertEqual((self.fx.prs(), self.bot_commit()), ([closed], head))

    def test_a_direct_push_needs_no_branch_and_is_not_held_up(self):
        # Outcome A (lock_pin.exact=push, set here: not the shipped value) pushes the parent line
        # itself. An earlier run without the rule left a conflict
        # PR on this sha's branch and a person closed it: with the rule off the branch is dead,
        # and with the rule on there is no merge-up branch in the path at all.
        self.both_moved()
        r = self.up(auto=None, config=PUSH)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.fx.close_pr(1, by=ALICE)
        self.assert_dead(self.up(auto=None, config=PUSH), 'alice closed it without merging', [(1, 'CLOSED')])
        self.assert_dead(self.up(auto='shadow', config=PUSH), 'alice closed it without merging', [(1, 'CLOSED')])
        closed, head = self.fx.prs()[0], self.bot_commit()
        r = self.up(config=PUSH)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('pushing vertex/main directly (outcome A', r.stdout)
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())
        self.assertEqual((self.fx.prs(), self.bot_commit()), ([closed], head))

    def test_a_dry_run_on_a_planned_tip_names_the_dead_branch(self):
        # the layer merge is not pushed yet, so a dry run cannot tell a direct push from a PR
        self.both_moved()
        t.MergeUpTests.inject_pr(self, 1, self.branch(), self.parent_tip)
        self.fx.close_pr(1, by=ALICE)
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = self.rule_lines(r)
        self.assertIn(f'dry run: would set the pin to the new meta-dsensos {PARENT} tip', lines[0])
        self.assertEqual(lines[1], f'dry run: a person closed {PR1} without merging (alice closed it without merging), so {self.branch()} is treated as closed. '
                         'If the real tip needs a PR on that branch (a bot-finished PR, or one that waits for an approval), a real run opens none, '
                         'pushes nothing to the branch and ends with an error')


class LockChangesRequestedTests(LockPersonCase):
    """Changes requested by a person on an open bot-finished PR: the bot does not merge it and
    the PR stays for people, in every run (a run dispatched by hand is no exception any more)."""

    def reviews(self, *reviews, number=1):
        write(os.path.join(self.fx.state, f'pr-{number}-rest-reviews.json'),
              json.dumps([{'user': user, 'state': state, 'commit_id': self.bot_commit()} for user, state in reviews]))

    def assert_stopped(self, r, why, states):
        """Nothing merged, no PR opened, the PR still open, the run red with one error that names it."""
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], states)
        self.assertEqual(self.distro_tip(), self.parent_tip)
        errors = [l for l in r.stdout.splitlines() if l.startswith('::error::')]
        self.assertEqual(errors, [f'::error::merge-up: a person said no to bot-finished PR {PR1} ({why}), so the bot does not merge {self.branch()}. '
                                  f'The PR stays open. Once the review is dismissed or its author approves, the next merge-up run of {CHILD} finishes it. '
                                  f'Or approve the PR and dispatch release-merge-up.yml on the default branch with child={CHILD} pr=1. '
                                  f'A new commit on {CHILD} gets a branch of its own and runs as normal'])
        self.assertIn(f'not applied: a person said no to bot-finished PR {PR1} ({why}). The bot does not merge it, and the PR stays open for people',
                      '\n'.join(self.rule_lines(r)))

    def test_changes_requested_stops_the_bot_and_a_later_approval_lifts_it(self):
        bot = self.waiting_pr()
        self.reviews((ALICE, 'CHANGES_REQUESTED'))
        for env in (AUTOMATIC, MANUAL):   # a run dispatched by hand does not merge it either
            log = self.fx.gh_log()
            r = self.up(env=env)
            self.assert_stopped(r, 'alice requested changes', [(1, 'OPEN')])
            self.assertNotIn('pr merge', self.calls(log))
            self.assertNotIn('pr create', self.calls(log))
            self.assertNotIn('dispatched by hand, which restarts the bot', r.stdout)
        # a comment after it changes nothing; the same person approving later takes it back
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'COMMENTED'))
        self.assert_stopped(self.up(env=AUTOMATIC), 'alice requested changes', [(1, 'OPEN')])
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'COMMENTED'), (ALICE, 'APPROVED'))
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'MERGED')])
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), bot)

    def test_changes_requested_by_one_person_stands_whatever_others_say(self):
        self.waiting_pr()
        bob = {'login': 'bob', 'type': 'User'}
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (bob, 'APPROVED'))
        self.assert_stopped(self.up(env=AUTOMATIC), 'alice requested changes', [(1, 'OPEN')])
        # a dismissed review is taken back, and a bot's review is nobody's no
        self.reviews((ALICE, 'CHANGES_REQUESTED'), (ALICE, 'DISMISSED'), ({'login': 'copilot', 'type': 'Bot'}, 'CHANGES_REQUESTED'))
        r = self.up(env=AUTOMATIC)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')

    def test_changes_requested_during_the_wait_stops_the_merge(self):
        for env in (AUTOMATIC, MANUAL):
            self.new_fixture()
            self.tip_holds_more()
            self.checks(GREEN)
            reviews = os.path.join(self.fx.state, 'pr-1-rest-reviews.json')
            # the review lands after the bot opened the PR and before it reads the checks
            self.view_hook(f"""cat > {reviews} <<'JSON'
[{{"user": {json.dumps(ALICE)}, "state": "CHANGES_REQUESTED"}}]
JSON
""")
            r = self.up(env=env)
            self.assert_stopped(r, 'alice requested changes', [(1, 'OPEN')])
            self.assertIn('required checks green on', r.stdout)
            self.assertNotIn('pr merge', self.fx.gh_log())

    def test_reviews_that_cannot_be_read_stop_the_merge(self):
        self.waiting_pr()
        write(os.path.join(self.fx.state, 'fail-reviews'), '')
        for env in (AUTOMATIC, MANUAL):
            self.assert_stopped(self.up(env=env), 'its reviews could not be read (HTTP 502: Bad Gateway)', [(1, 'OPEN')])
        os.remove(os.path.join(self.fx.state, 'fail-reviews'))
        r = self.up(env=MANUAL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.fx.prs()[0]['state'], 'MERGED')


class PersonNoUnitTests(unittest.TestCase):
    """release_git.person_no and latest_pr."""

    def test_latest_pr(self):
        self.assertIsNone(release_git.latest_pr([]))
        prs = [{'number': 3, 'state': 'CLOSED', 'url': 'u3'}, {'number': 12, 'state': 'OPEN', 'url': 'u12'}, {'number': 7, 'state': 'MERGED', 'url': 'u7'}]
        self.assertEqual(release_git.latest_pr(prs), (12, 'OPEN', 'u12'))
        # an open PR speaks for the head whatever its number: PR 3 reopened after PR 12 was closed
        prs = [{'number': 3, 'state': 'OPEN', 'url': 'u3'}, {'number': 12, 'state': 'CLOSED', 'url': 'u12'}, {'number': 7, 'state': 'MERGED', 'url': 'u7'}]
        self.assertEqual(release_git.latest_pr(prs), (3, 'OPEN', 'u3'))
        # none open: the most recent one
        prs[0]['state'] = 'CLOSED'
        self.assertEqual(release_git.latest_pr(prs), (12, 'CLOSED', 'u12'))

    def test_person_no(self):
        no = release_git.person_no
        superseded = [{'user': BOT, 'body': 'Superseded by https://x/pull/9: the child moved on'}]
        self.assertIsNone(no('CLOSED', {'closed_by': BOT}, superseded))
        self.assertEqual(no('CLOSED', {'closed_by': ALICE}, superseded), 'alice closed it without merging')
        self.assertEqual(no('CLOSED', {'closed_by': BOT}, []), 'ds-os-release-bot[bot] closed it without merging')
        other_bot = {'login': 'github-actions[bot]', 'type': 'Bot'}
        self.assertEqual(no('CLOSED', {'closed_by': other_bot}, superseded), 'github-actions[bot] closed it without merging')
        self.assertEqual(no('CLOSED', {'closed_by': None}, superseded), 'someone GitHub does not name closed it without merging')
        self.assertEqual(no('CLOSED'), 'someone GitHub does not name closed it without merging')
        self.assertIsNone(no('MERGED', {'closed_by': ALICE}, []))
        rv = lambda user, state: {'user': user, 'state': state}
        self.assertIsNone(no('OPEN', reviews=[]))
        self.assertEqual(no('OPEN', reviews=[rv(ALICE, 'APPROVED'), rv(ALICE, 'CHANGES_REQUESTED')]), 'alice requested changes')
        self.assertIsNone(no('OPEN', reviews=[rv(ALICE, 'CHANGES_REQUESTED'), rv(ALICE, 'APPROVED')]))
        self.assertIsNone(no('OPEN', reviews=[rv(ALICE, 'CHANGES_REQUESTED'), rv(ALICE, 'DISMISSED')]))
        self.assertEqual(no('OPEN', reviews=[rv(ALICE, 'CHANGES_REQUESTED'), rv(ALICE, 'COMMENTED')]), 'alice requested changes')
        self.assertIsNone(no('OPEN', reviews=[rv({'login': 'copilot', 'type': 'Bot'}, 'CHANGES_REQUESTED')]))
        bob = {'login': 'bob', 'type': 'User'}
        self.assertEqual(no('OPEN', reviews=[rv(bob, 'CHANGES_REQUESTED'), rv(ALICE, 'CHANGES_REQUESTED')]), 'alice, bob requested changes')


class LockConflictPrTests(LockCase):
    """Outcomes C and D: a conflict PR still opens. C carries the lockfile already set to the
    tip; D keeps the lockfile's markers and says which check refused."""

    install_hook = LockDirectPushTests.install_hook

    def test_outcome_c_pre_resolves_the_lockfile_in_the_conflict_pr(self):
        self.others_conflict()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        meta_tip = self.meta_tip()
        pr = self.fx.prs()[0]
        self.assertEqual((pr['title'], pr['head'], pr['state']), (f'Merge-up {CHILD} into {PARENT} (conflict)', self.branch(), 'OPEN'))
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        # the lockfile is done, foo.txt is left to a person
        self.assertEqual(self.lockfile(head), LOCK_YML.format(pin=meta_tip))
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, head, 'foo.txt'))
        message = git(self.fx.distro_bare, 'log', '-1', '--format=%B', head)
        self.assertIn('Conflict markers left in: foo.txt\n', message)
        self.assertNotIn(LOCK, message.split('Merge-Up-Rule')[0])
        self.assertIn(f'Merge-Up-Rule: lock-pin tip={meta_tip} parent-pin={self.m4} child-pin={self.m3}', message)
        # the body lists only what still has markers, and says what was done to the lockfile
        self.assertIn('- `foo.txt`', pr['body'])
        self.assertNotIn(f'- `{LOCK}`', pr['body'])
        self.assertIn(f'`{LOCK}` conflicted too', pr['body'])
        self.assertIn(f'already resolved in this commit by the lockfile pin rule. It pins the tip of meta-dsensos `{PARENT}`, `{meta_tip}`', pr['body'])
        self.assertIn('conflicts in: foo.txt\n', r.stdout)
        self.assertIn(f'::error::merge-up: ds-tegra-distro {CHILD} -> {PARENT} conflicts (foo.txt)', r.stdout)
        self.assertIn(f'lockfile set to the meta-dsensos {PARENT} tip {meta_tip} inside the conflict PR (outcome C: checks 1 to 4 pass, 1 other file(s) conflict)', self.rule_lines(r)[0])
        self.assertEqual(self.distro_tip(), self.parent_tip)
        self.assertNotIn('pr merge', self.fx.gh_log())
        # a person resolves foo.txt only; the finish merges the rule's lockfile with it
        pr_head = t.MergeUpTests.resolve(self, self.branch())
        t.MergeUpTests.approve(self, 1)
        r = self.up('--pr', '1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = self.distro_tip()
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', tip + '^2'), pr_head)
        self.assertEqual(self.pin(tip), meta_tip)
        self.assertEqual(self.fx.show(self.fx.distro_bare, tip, 'foo.txt'), 'both fixes')

    def test_outcome_c_in_a_dry_run(self):
        self.others_conflict()
        self.meta_merged()
        before = self.snapshot()
        r = self.up(dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'dry run: would pre-resolve the lockfile to the meta-dsensos {PARENT} tip {self.meta_tip()} inside the conflict PR (outcome C', self.rule_lines(r)[0])
        self.assertIn(f'dry run: would push {self.branch()}', r.stdout)
        after = self.snapshot()
        self.assertEqual((after['distro'], after['meta'], after['prs']), (before['distro'], before['meta'], before['prs']))

    def test_outcome_c_when_another_conflict_has_no_markers(self):
        # the child deletes foo.txt, the parent edits it: no text marker, so the PR goes on the child
        # itself and the rule's lockfile cannot ride along. The body hands the answer to the resolver.
        self.child_work(trailing_versions=False)
        git(self.fx.distro, 'checkout', '-q', '-B', CHILD, f'origin/{CHILD}')
        git(self.fx.distro, 'rm', '-q', 'foo.txt')
        git(self.fx.distro, 'commit', '-q', '-m', 'drop foo')
        git(self.fx.distro, 'push', '-q', 'origin', CHILD)
        self.child_tip = self.green_sha = git(self.fx.distro, 'rev-parse', 'HEAD')
        write(self.fx.green, self.child_tip + '\n')
        self.m4 = self.fx.meta_commit(PARENT, 'main only\n', 'main pin target', path='other.txt')
        self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=self.m4), 'foo.txt': 'main fix\n'}, 'pin m4, fix foo')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], self.child_tip)
        body = self.fx.prs()[0]['body']
        self.assertIn('leave no text markers', body)
        self.assertIn(f'- `{LOCK}`', body)
        self.assertIn(f"The lockfile pin rule's answer is the tip of meta-dsensos `{PARENT}`, `{self.meta_tip()}`", body)
        self.assertNotIn('already resolved in this commit', body)

    def test_outcome_d_another_layers_pin(self):
        self.another_layer_conflicts()
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        meta_tip = self.meta_tip()
        pr = self.fx.prs()[0]
        self.assertEqual(pr['title'], f'Merge-up {CHILD} into {PARENT} (conflict)')
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        marked = self.fx.show(self.fx.distro_bare, head, LOCK)
        self.assertIn('<<<<<<< ', marked)
        self.assertIn('||||||| ', marked)
        self.assertNotIn('Merge-Up-Rule', git(self.fx.distro_bare, 'log', '-1', '--format=%B', head))
        self.assertIn(f'- `{LOCK}`', pr['body'])
        self.assertIn(f'The lockfile pin rule did not resolve `{LOCK}`: check 3 failed (the lockfile conflicts beyond the meta-dsensos commit', pr['body'])
        self.assertIn(f'The candidate was the tip of meta-dsensos `{PARENT}`, `{meta_tip}`', pr['body'])
        self.assertIn('refused at check 3', self.rule_lines(r)[0])
        self.assertIn(f'candidate tip {meta_tip}', self.rule_lines(r)[0])
        self.assertEqual(self.distro_tip(), self.parent_tip)
        self.assertEqual(self.commit_comments(), [])

    def test_outcome_d_a_parent_pin_the_tip_does_not_contain(self):
        # the layer line already holds the child's pin, so nothing is merged there; the parent
        # then locks a newer commit of the child's layer line, which its own line does not contain
        self.child_work(trailing_versions=False)
        tip = self.meta_merged()
        m6 = self.fx.meta_commit(CHILD, 'm6 fix\n', 'a later layer fix on the child line')
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=m6)}, 'lock a commit of the wrong layer line')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.meta_tip(), tip)
        body = self.fx.prs()[0]['body']
        self.assertIn(f"check 2 failed (the parent's pin {m6} is not contained in the tip {tip})", body)
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, self.branch(), LOCK))
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_outcome_d_a_parent_pin_the_layer_repo_does_not_know_on_these_lines(self):
        self.child_work(trailing_versions=False)
        # (a local clone copies every object, so the fixture uses a sha the layer repo never had;
        # on GitHub a single-branch clone also lacks commits that are only on other branches)
        stray = 'd' * 40
        self.parent_tip = self.fx.distro_commit(PARENT, {LOCK: LOCK_YML.format(pin=stray)}, 'lock a commit the layer lines do not have')
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"check 2 failed (the parent's pin {stray} is an unknown object in the meta-dsensos clone", self.fx.prs()[0]['body'])
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, self.branch(), LOCK))
        self.assertEqual(self.distro_tip(), self.parent_tip)

    def test_a_rewritten_layer_line_refuses_on_the_retry(self):
        self.both_moved()
        self.install_hook('rewrite')
        r = self.up(config=PUSH)   # the push retry is outcome A's: lock_pin.exact=push
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('ds-tegra-distro: push rejected (attempt 1), refetching', r.stdout)
        # the refetch succeeds; the ancestry test is what refuses, and it names both tips
        self.assertRegex(r.stdout, rf'meta-dsensos: {PARENT} was rewritten: {self.meta_tip()} does not descend from [0-9a-f]{{40}}\n')
        self.assertNotIn('could not be refetched', r.stdout)
        lines = self.rule_lines(r)
        self.assertIn('outcome A', lines[0])
        self.assertIn(f'refused at check 1: the meta-dsensos parent line was rewritten during this run ({self.meta_tip()} does not descend from ', lines[1])
        pr = self.fx.prs()[0]
        self.assertIn('check 1 failed', pr['body'])
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, pr['head'], LOCK))
        # the parent holds the concurrent commit and nothing of this run
        self.assertEqual(self.fx.subject(self.fx.distro_bare, self.distro_tip()), 'pin the new layer tip')
        self.assertEqual(self.commit_comments(), [])


    def test_a_refetch_that_fails_refuses_with_its_own_reason(self):
        # After a rejected push the layer line cannot be read (a network or auth error). The tip
        # is then unknown: the rule refuses, and says so, instead of calling the line rewritten.
        self.both_moved()
        self.install_hook('gone')
        r = self.up(config=PUSH)   # the push retry is outcome A's: lock_pin.exact=push
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('ds-tegra-distro: push rejected (attempt 1), refetching', r.stdout)
        self.assertIn(f'meta-dsensos: {PARENT} could not be refetched (', r.stdout)
        self.assertNotIn('rewritten', r.stdout)
        lines = self.rule_lines(r)
        self.assertIn('outcome A', lines[0])
        self.assertIn('refused at check 1: the meta-dsensos parent line could not be refetched during this run, so its tip is unknown (', lines[1])
        pr = self.fx.prs()[0]
        self.assertIn('check 1 failed (the meta-dsensos parent line could not be refetched', pr['body'])
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, pr['head'], LOCK))
        self.assertEqual(self.fx.subject(self.fx.distro_bare, self.distro_tip()), 'pin the new layer tip')
        self.assertEqual(self.commit_comments(), [])


class LockProvenanceTests(LockCase):
    """A merge-up branch that is already on origin and is not this run's commit: no outcome of
    the rule applies to it and the bot does not finish it (plan 6.1)."""

    up = LockBotPrTests.up

    def seed(self, files, message='Merge by hand', who=None):
        """Someone pushes merge-up/<child>/<sha7> first: the child merged into the parent by hand."""
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', self.branch(), f'origin/{PARENT}')
        subprocess.run(['git', '-C', self.fx.distro, 'merge', '-q', '--no-ff', '--no-commit', f'origin/{CHILD}'], capture_output=True)
        for path, text in files.items():
            write(os.path.join(self.fx.distro, path), text)
        git(self.fx.distro, 'checkout', f'origin/{PARENT}', '--', 'versions/vertex', 'kas/include/base.yml')
        git(self.fx.distro, 'add', '-A')
        ident = {} if who is None else dict(GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_COMMITTER_NAME=who[0], GIT_COMMITTER_EMAIL=who[1])
        subprocess.run(['git', '-C', self.fx.distro, 'commit', '-q', '-m', message], check=True, capture_output=True, env=dict(os.environ, **ident))
        git(self.fx.distro, 'push', '-q', 'origin', self.branch())
        git(self.fx.distro, 'checkout', '-q', '--detach')
        return git(self.fx.distro, 'rev-parse', self.branch())

    def test_a_seeded_commit_with_the_computed_tree_is_not_bot_finished(self):
        # The tree and the parents the run computes can be reproduced by anyone: the lockfile set
        # to the layer tip. A commit that is not the bot's (its committer, its message) is still
        # not merged without an approval, forged trailer or not.
        self.tip_holds_more()
        tip = self.meta_merged()
        parent_tip = self.distro_tip()
        bot = (os.environ.get('GIT_BOT_NAME', 'ds-os-release-bot'), self.bot_email())
        trailer = f'Merge-Up-Rule: lock-pin tip={tip} parent-pin={self.m4} child-pin={self.m3}'
        subject = f'Merge {CHILD} into {PARENT}'
        for message, who in ((f'mallory wrote this\n\n{trailer}', ('mallory', 'm@x')),    # not the bot's committer
                             (f'{subject}\n\n{trailer}', ('mallory', 'm@x')),             # the bot's message, not its committer
                             (f'{subject}\n\nsomething else\n\n{trailer}', bot)):         # the bot's address, not its message
            with self.subTest(message=message, who=who):
                self.new_fixture()
                self.tip_holds_more()
                self.assertEqual(self.meta_merged(), tip)   # fixed dates: the same shas in every fixture
                seeded = self.seed({LOCK: LOCK_YML.format(pin=tip)}, message=message, who=who)
                self.checks(GREEN)
                r = self.up()
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertNotIn('untouched; reusing it', r.stdout)
                self.assertIn(f'not applied: {self.branch()} already exists on origin at {seeded[:7]} and is not the commit this run computes', r.stdout)
                self.assertNotIn('pr merge', self.fx.gh_log())
                self.assertEqual(self.distro_tip(), parent_tip)
                self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], seeded)
                pr = self.fx.prs()[0]
                self.assertEqual((pr['title'], pr['state']), (f'Merge-up {CHILD} into {PARENT} (conflict)', 'OPEN'))
                self.assertNotIn('The bot merges this PR itself', pr['body'])

    checks = LockBotPrTests.checks

    def bot_email(self):
        r = subprocess.run(['bash', '-c', f'. {SCRIPTS}/lib.sh; echo "$GIT_BOT_EMAIL"'], capture_output=True, text=True, check=True)
        return r.stdout.strip()

    def test_a_foreign_branch_is_not_bot_finished(self):
        # outcome B would open a PR without markers and merge it once the checks are green
        self.tip_holds_more()
        parent_tip = self.distro_tip()
        seeded = self.seed({LOCK: LOCK_YML.format(pin=self.m3)})   # no markers, and the child's pin
        write(os.path.join(self.fx.state, 'pr-1-review.json'),
              json.dumps({'statusCheckRollup': [{'name': n, 'conclusion': 'SUCCESS'} for n in ('pr-checks', 'pin-propagation-safety')]}))
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'not applied: {self.branch()} already exists on origin at {seeded[:7]} and is not the commit this run computes', r.stdout)
        self.assertIn(f'{self.branch()} was not produced by this run: ', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], seeded)
        self.assertEqual(self.distro_tip(), parent_tip)
        self.assertNotIn('pr merge', self.fx.gh_log())
        pr = self.fx.prs()[0]
        self.assertEqual((pr['title'], pr['state']), (f'Merge-up {CHILD} into {PARENT} (conflict)', 'OPEN'))
        self.assertIn('**This branch was not produced by this run.**', pr['body'])
        self.assertNotIn('The bot merges this PR itself', pr['body'])
        # a later run does not finish it either, green checks or not
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.distro_tip(), parent_tip)
        self.assertNotIn('pr merge', self.fx.gh_log())

    def test_the_bots_own_conflict_branch_is_not_called_foreign(self):
        # A phase-0 run (the variable absent) pushed the conflict commit. A later run with the
        # rule on finds that branch, backs out of outcome B, and must recognise the commit as
        # the one this run computes: the conflict is put back byte for byte, labels included.
        self.tip_holds_more()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        head = self.fx.heads(self.fx.distro_bare)[self.branch()]
        marked = self.fx.show(self.fx.distro_bare, head, LOCK)
        self.checks(GREEN)
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'not applied: {self.branch()} already exists on origin at {head[:7]}', r.stdout)
        self.assertNotIn('was not produced by this run', r.stdout)
        self.assertIn(f'{self.branch()} already exists on origin; left as it is', r.stdout)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], head)
        self.assertEqual(self.fx.show(self.fx.distro_bare, head, LOCK), marked)
        self.assertEqual([(p['number'], p['state']) for p in self.fx.prs()], [(1, 'OPEN')])
        self.assertNotIn('pr merge', self.fx.gh_log())

    def test_a_foreign_branch_gets_no_pre_resolved_lockfile_note(self):
        # outcome C would say the lockfile is already resolved in the conflict commit
        self.others_conflict()
        seeded = self.seed({LOCK: LOCK_YML.format(pin=self.m3), 'foo.txt': 'by hand\n'})
        r = self.up()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.fx.heads(self.fx.distro_bare)[self.branch()], seeded)
        body = self.fx.prs()[0]['body']
        self.assertIn('**This branch was not produced by this run.**', body)
        self.assertNotIn('already resolved in this commit by the lockfile pin rule', body)
        self.assertIn('No merge-up rule was applied to it', body)


class LockVerifyFinishTests(LockCase):
    """merge_up.lock_pin.verify_finish: the finish of a ds-tegra-distro conflict PR also reads
    the lockfile the resolver left. A config switch, so it holds with the variable absent."""

    def conflict_pr(self):
        """both_moved with the rule off: the phase-0 conflict PR, markers in the lockfile."""
        self.both_moved()
        r = self.up(auto=None)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('<<<<<<< ', self.fx.show(self.fx.distro_bare, self.branch(), LOCK))

    def resolve_to(self, pin):
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', self.branch(), f'origin/{self.branch()}')
        write(os.path.join(self.fx.distro, LOCK), LOCK_YML.format(pin=pin))
        git(self.fx.distro, 'commit', '-q', '-am', f'resolve the lockfile to {pin[:7]}')
        git(self.fx.distro, 'push', '-q', 'origin', self.branch())
        t.MergeUpTests.approve(self, 1)
        return git(self.fx.distro, 'rev-parse', 'HEAD')

    def finish(self, scripts, **kw):
        return self.fx.run(os.path.join(scripts, 'merge-up.sh'), '--child', CHILD, '--pr', '1', **kw)

    def test_a_child_pin_resolution_is_refused(self):
        strict = self.fx.scripts_copy({'lock_pin.verify_finish': True})
        self.conflict_pr()
        self.resolve_to(self.m3)   # the child's pin: the parent would build without its own layer work (m4)
        for dry in ('true', 'false'):
            r = self.finish(strict, dry_run=dry)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn(f"::error::merge-up: the lockfile on the PR head pins {self.m3}, which does not contain the previous pin of the parent line {self.m4} "
                          "(the resolution took the child's pin)", r.stdout)
            self.assertIn('the lockfile resolution is refused (merge_up.lock_pin.verify_finish)', r.stdout)
            self.assertIn(self.meta_tip(), r.stdout.splitlines()[-1])   # what to set it to
        self.assertEqual(self.distro_tip(), self.parent_tip)
        self.assertEqual(self.fx.prs()[0]['state'], 'OPEN')
        self.assertNotIn('pr merge', self.fx.gh_log())
        # resolved to the layer tip: finished
        head = self.resolve_to(self.meta_tip())
        r = self.finish(strict)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f'lock verifier:      ok, the PR head pins {self.meta_tip()}', r.stdout)
        self.assertEqual(git(self.fx.distro_bare, 'rev-parse', self.distro_tip() + '^2'), head)

    def test_the_parent_pin_or_a_pin_off_the_line_is_refused(self):
        strict = self.fx.scripts_copy({'lock_pin.verify_finish': True})
        self.conflict_pr()
        self.resolve_to(self.m4)   # the parent's own pin: the child's layer fix is dropped
        r = self.finish(strict)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"pins {self.m4}, which does not contain the child's pin {self.m3}", r.stdout)
        # a commit that holds both pins but is not on the parent's layer line
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', 'feature', f'origin/{PARENT}')
        write(os.path.join(self.fx.meta_work, 'other.txt'), 'feature\n')
        feature = t.commit_all(self.fx.meta_work, 'a feature commit on top of the tip')
        git(self.fx.meta_work, 'push', '-q', 'origin', 'feature')
        self.resolve_to(feature)
        r = self.finish(strict)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f'pins {feature}, which is not contained in the meta-dsensos parent line at {self.meta_tip()}', r.stdout)
        self.assertEqual(self.distro_tip(), self.parent_tip)
        self.assertNotIn('pr merge', self.fx.gh_log())

    def test_a_dry_run_finish_does_not_hold_the_pin_to_a_tip_that_is_not_pushed_yet(self):
        # The child moved its layer pin after the conflict PR opened, and the resolver merged the
        # new child in. A real finish first merges the layer; a dry run does not, so the layer tip
        # it holds is not the one the pin will be checked against (planned). The dry run checks the
        # two containments and says nothing about the tip.
        strict = self.fx.scripts_copy({'lock_pin.verify_finish': True})
        self.conflict_pr()
        old_tip = self.meta_tip()
        m6 = self.fx.meta_commit(CHILD, 'm6 fix\n', 'a later layer fix on the child line', path='six.txt')
        self.child_tip = self.fx.distro_commit(CHILD, {LOCK: LOCK_YML.format(pin=m6)}, 'pin m6')
        write(self.fx.green, self.child_tip + '\n')
        # what the layer merge will hold, made by the resolver ahead of it
        git(self.fx.meta_work, 'fetch', '-q', 'origin')
        git(self.fx.meta_work, 'checkout', '-q', '-B', 'ahead', f'origin/{PARENT}')
        git(self.fx.meta_work, 'merge', '-q', '--no-ff', '-m', 'the layer merge, ahead of the bot', f'origin/{CHILD}')
        git(self.fx.meta_work, 'push', '-q', 'origin', 'ahead')
        ahead = git(self.fx.meta_work, 'rev-parse', 'HEAD')
        git(self.fx.distro, 'fetch', '-q', 'origin')
        git(self.fx.distro, 'checkout', '-q', '-B', self.branch(), f'origin/{self.branch()}')
        subprocess.run(['git', '-C', self.fx.distro, 'merge', '-q', '--no-commit', f'origin/{CHILD}'], capture_output=True)
        write(os.path.join(self.fx.distro, LOCK), LOCK_YML.format(pin=ahead))
        git(self.fx.distro, 'checkout', f'origin/{PARENT}', '--', 'versions/vertex', 'kas/include/base.yml')
        git(self.fx.distro, 'add', '-A')
        git(self.fx.distro, 'commit', '-q', '-m', 'merge the newer child, lock the layer merge')
        git(self.fx.distro, 'push', '-q', 'origin', self.branch())
        t.MergeUpTests.approve(self, 1)
        r = self.finish(strict, dry_run='true')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"lock verifier:      ok, the PR head pins {ahead}: it contains the parent's previous pin and the child's pin\n", r.stdout)
        self.assertNotIn(f'the meta-dsensos parent line at {old_tip}', r.stdout)
        self.assertEqual(self.meta_tip(), old_tip)   # the dry run merged no layer

    def test_shipped_off_the_finish_reads_no_lockfile(self):
        # verify_finish is false in release/config.json: phase-0 behaviour, the child's pin merges
        self.assertFalse(release_git.merge_up_config()['lock_pin']['verify_finish'])
        self.conflict_pr()
        self.resolve_to(self.m3)
        for auto in (None, 'lock-pin'):
            r = self.up('--pr', '1', auto=auto, dry_run='true')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn('lock verifier', r.stdout)
        r = self.up('--pr', '1', auto=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.pin(self.distro_tip()), self.m3)

    def test_without_an_app_token_a_dry_run_says_not_checked(self):
        strict = self.fx.scripts_copy({'lock_pin.verify_finish': True})
        self.conflict_pr()
        self.resolve_to(self.m3)
        r = self.finish(strict, dry_run='true', token_kind='github')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('lock verifier:      not checked (no App token', r.stdout)

    def test_a_bot_pr_and_a_pre_resolved_pr_pass_the_verifier(self):
        strict = self.fx.scripts_copy({'lock_pin.verify_finish': True})
        self.others_conflict()
        r = self.fx.run(os.path.join(strict, 'merge-up.sh'), '--child', CHILD, extra_env={'DS_MERGE_UP_AUTOMATION': 'lock-pin'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        t.MergeUpTests.resolve(self, self.branch())
        t.MergeUpTests.approve(self, 1)
        r = self.finish(strict)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('lock verifier:      ok', r.stdout)
        self.assertEqual(self.pin(self.distro_tip()), self.meta_tip())

    def test_lock_verify_unit(self):
        self.conflict_pr()
        head = self.resolve_to(self.meta_tip())
        git(self.fx.distro, 'fetch', '-q', 'origin')
        pin, reasons = release_git.lock_verify(self.fx.distro, self.fx.meta_bare, head, self.parent_tip, self.m3, self.meta_tip())
        self.assertEqual((pin, reasons), (self.meta_tip(), []))
        # unknown objects are refusals of their own
        pin, reasons = release_git.lock_verify(self.fx.distro, self.fx.meta_bare, head, self.parent_tip, 'd' * 40, self.meta_tip())
        self.assertEqual(len(reasons), 1)
        self.assertIn('is unknown', reasons[0])
        pin, reasons = release_git.lock_verify(self.fx.distro, self.fx.meta_bare, head, self.parent_tip, self.m3, 'd' * 40)
        self.assertIn('an unknown object for the meta-dsensos parent line', reasons[0])
        # planned: the tip is not a value, the two containments still hold
        self.assertEqual(release_git.lock_verify(self.fx.distro, self.fx.meta_bare, head, self.parent_tip, self.m3, 'd' * 40, planned=True)[1], [])


if __name__ == '__main__':
    unittest.main()
