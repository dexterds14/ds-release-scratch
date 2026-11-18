#!/usr/bin/env python3
"""Git and GitHub helpers shared by create-branch.sh, merge-up.sh and promote.sh (ENG-1933, ENG-1934, ENG-1935).

    release_git.py lock-pin <sha> [--git-dir DIR] [--file PATH]
        the meta-dsensos commit kas/include/base.lock.yml pins at <sha>
        (git show <sha>:kas/include/base.lock.yml), or in --file
    release_git.py base-branch-get <file>
        the meta-dsensos `branch:` value in a kas/include/base.yml
    release_git.py base-branch-edit <file> <old> <new>
        replace that one line in place; fails unless exactly one line matches
    release_git.py merge-keep-target <branch> [--git-dir DIR]
        inside an uncommitted merge: the target keeps its pending-version files
        (versions/<product>, never versions/README.md, which merges like any doc)
        and base-merge does base.yml. Exit 1 when base.yml has a real conflict.
        What merge-up and sync run, and what a human runs by hand.
    release_git.py base-merge <branch> [--git-dir DIR]
        inside an uncommitted merge (MERGE_HEAD set): three-way merge of
        kas/include/base.yml with the meta-dsensos `branch:` of all three sides
        set to <branch> first, so only real content differences can conflict.
        Clean: the result is written and staged, exit 0. Conflict: the file is
        left with conflict markers and unmerged, exit 1.
    release_git.py line-plan <product> <version> [--kind auto|minor|patch]
        JSON {line, kind, level, pending, tag} for the line cut from tag <product>/vN.M.P:
        minor -> <product>/N.x.x pending N.(M+1).0; patch -> <product>/N.M.x pending N.M.(P+1).
        auto (the default) is minor for N.0.0 and patch for anything else
    release_git.py promote-plan <line> <pending> [--bump auto|major|minor|patch] [--root DIR]
        JSON {product, line, kind, version, tag, level, next_version} for promoting <pending>
        on <line>; exit 1 when the version does not fit the line or the bump would leave
        a pending version that does not fit it (auto: major on main, minor on N.x.x,
        patch on N.M.x)
    release_git.py unmerged <base> <head> [--git-dir DIR]
        the commits in <base>..<head> that are not line bookkeeping (versions/ and
        kas/include/base.yml only), "<sha> <subject>" per line; exit 1 when there are any
    release_git.py versions-only <sha> [--git-dir DIR]
        exit 0 when the commit touches only versions/ (never for a merge commit)
    release_git.py walk-versions-only <sha> [--git-dir DIR] [--max N]
        <sha>, then its first parents while the commit is versions-only, one per line
    release_git.py first-run [FILE]
        from `gh run list --json databaseId,number` output: prints "<databaseId> <number>"
        of the first run, exit 1 when the list is empty
    release_git.py pr-check [FILE] [--reviews FILE]
        from `gh pr view --json reviewDecision,mergeStateStatus,statusCheckRollup,reviews,commits`:
        exit 0 when the PR is approved by someone other than the last pusher,
        mergeStateStatus is CLEAN and no check failed; else every reason, exit 1.
        --reviews (what merge-up.sh passes, ENG-2234): the PR's reviews from the
        REST API (`gh api --paginate repos/<repo>/pulls/<n>/reviews`), read instead
        of the view's own list. Only a review whose user.type is "User" counts, so
        an approval by github-actions, Copilot or any App never finishes a PR.
        Without it (sync-product-releases.sh) the view's reviews count as before.
        --strict (merge_up.finish.strict; needs --reviews, and headRefOid in the
        view): an approval counts only when its commit_id is the PR head; the
        approver must not have pushed to the branch; and every check named in
        --required a,b must be reported on the head and successful. Who pushed:
        --activity FILE, the repository activity of the branch (`gh api
        --paginate repos/<repo>/activity?ref=refs/heads/<branch>`), bots aside.
        When that could not be read (--activity absent), --commits FILE instead:
        the REST commit objects of the commits above the bot's commit, of which
        every author and committer counts as a pusher; the output says so
    release_git.py lock-merge <tip> [--git-dir DIR] [--check]
        inside an uncommitted merge: three-way merge of kas/include/base.lock.yml
        with the meta-dsensos commit of base, ours and theirs all set to <tip>
        first (ENG-2234), so only another line of the lockfile can conflict.
        Clean: the result (pin == <tip>) is written and staged, exit 0.
        Conflict: nothing is touched, exit 1. --check writes nothing either way
    release_git.py lock-rule <tip> --git-dir DIR --meta-dir DIR --parent-pin SHA --child-pin SHA
                             [--others N] [--planned] [--rewound TEXT] [--unread TEXT] [--root DIR]
        R-lock, the lockfile pin rule (ENG-2234), evaluated inside an uncommitted
        ds-tegra-distro merge whose base.lock.yml is unmerged. <tip> is the parent
        line's meta-dsensos tip, --meta-dir a clone that holds it. Read-only. Prints
        JSON: the five checks, the verdict (exact, extra, refused or planned), the
        layer commits in the tip that neither pin contains, and the outcome (A direct
        push, B bot-finished PR, P a PR for one approval, C pre-resolved inside a
        conflict PR, D refused) for --others other conflicted files under the
        lock_pin config (as shipped a lockfile-only conflict is always B; A needs
        lock_pin.exact=push). Exit 1 only when the evaluation itself failed
    release_git.py lock-invariant --git-dir DIR --meta-dir DIR --parent-tip SHA --meta-ref REF
        whether the parent line's lock pin is on the first-parent chain of its
        meta-dsensos line; when not, a ::warning:: naming the commit that moved it
    release_git.py lock-verify --git-dir DIR --meta-dir DIR --head SHA --parent-tip SHA --child-pin SHA --tip SHA [--planned]
        merge_up.lock_pin.verify_finish: the lockfile a person left on a conflict
        PR head. Its pin must contain the parent line's previous pin and the
        child's pin, and be contained in the meta-dsensos parent tip (not checked
        with --planned: a dry run whose tip does not exist yet). Exit 1 with the
        reasons; a resolution that took the child's pin is refused
    release_git.py checks-state FILE --head SHA --required a,b
        from `gh pr view --json state,headRefOid,statusCheckRollup`: one word, green
        (every required check succeeded on that head), red, pending, moved (the head
        is not SHA) or closed, then the reasons
    release_git.py markers --base SHA --head SHA [--git-dir DIR]
        the files <head> changes against its merge base with <base> that still
        hold a conflict marker line (<<<<<<< , ||||||| or >>>>>>> at the start of
        a line), one per line, exit 1; exit 0 when there is none. What pr-checks
        runs on a merge-up/** head, so a conflict PR cannot be merged from the
        GitHub UI with markers in it (ENG-2234)
    release_git.py branch-origin <head> --parent REF [--like SHA] [--bot-email E] [--git-dir DIR]
        where a merge-up/<child>/<sha7> branch at <head> comes from: its first
        commit above the parent line (the last one on the first-parent chain that
        REF does not contain). Prints "ours <sha>" when that commit is the bot's
        merge, else "foreign <sha>" and why on a second line. --like: the commit
        this run just made; the first commit must have the same tree, the same two
        parents and the same committer. --bot-email alone (a finish, which
        computes no merge): a two-parent commit committed under that address
    release_git.py resolve-conflicts --git-dir DIR --out DIR [--layer] [--owner ORG] [--skip PATH]... [--check] [--root DIR]
        the merge-up rules of merge_rules.py (ENG-2234) on the unmerged paths of
        the uncommitted merge in --git-dir. Each rule runs in the mode
        DS_MERGE_UP_AUTOMATION gives it: splice (adjacent edits) everywhere, pins
        (the app pins of release/pin-propagation.json) only with --layer, the
        meta-dsensos step, which also reads merge_up.meta_dsensos.never_auto
        instead of merge_up.never_auto. No rule writes a never_auto file; what
        the rules would have written there is kept as a patch for a PR comment.
        A rule that acts writes its result to the work tree and stages a file
        with no conflict left; --check (a dry run) evaluates and writes nothing.
        --skip names a path another rule owns (the lockfile, base.yml). The pins
        rule compares commits through `gh api` with MERGE_UP_READ_TOKEN, in
        --owner's repos. Prints one line per finding and writes to --out:
        report.json, log.txt, body.md (the PR body section), comment.md (the
        candidate patches, only when there is one), trailers.txt (Merge-Up-Rule
        lines for the commit) and remaining.txt (the paths still in conflict,
        skipped ones included). Exit 1 only when the evaluation itself failed,
        and then nothing was written
    release_git.py merge-up-config [--root DIR] [--key a.b]
        the "merge_up" block of release/config.json (ENG-2234), validated, with
        the defaults for anything absent; --key prints one value by dotted path
        (finish.strict), a string bare and anything else as JSON. The root
        defaults to the checkout this script is in: the ref a run was
        dispatched on, which is the default branch for every automatic run
    release_git.py automation [--value V] [--rule lock-pin|splice|pins]
        DS_MERGE_UP_AUTOMATION (the environment variable, or --value) parsed:
        two lines for the run summary, or with --rule that rule's mode
        (off, shadow or act). Exit 1 naming the token that is not a rule

Standard library only (runs on the CI runners and in the tests). The YAML
reader covers the lockfile's shape (nested mappings of scalars) and nothing
more; base.yml is edited as text so the file keeps its comments and order.
"""
import argparse
import json
import os
import re
import subprocess
import tempfile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lines  # noqa: E402  (sibling module: parse_line)
import version  # noqa: E402  (sibling module: bump() and SEMVER_CORE)

LOCKFILE = 'kas/include/base.lock.yml'
LAYER = 'meta-dsensos'
SHA_RE = re.compile(r'[0-9a-f]{40}')
NUM = r'(0|[1-9][0-9]*)'
VERSIONS_DIR = 'versions/'
# Files a line's own bookkeeping commits touch (the opening commit of a line
# and the bump after a promote); such commits never count as unmerged work.
BOOKKEEPING_FILES = ('kas/include/base.yml',)
LINE_KINDS = ('auto', 'minor', 'patch')
BUMP_LEVELS = ('auto',) + version.LEVELS
AUTO_LEVEL = {'main': 'major', 'minor': 'minor', 'patch': 'patch'}
FAILED_CONCLUSIONS = {'FAILURE', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'STALE'}
FAILED_STATES = {'FAILURE', 'ERROR'}
# The Actions variable that switches the merge-up rules (ENG-2234), and the
# rules it can name. Checks that refuse are switched in release/config.json.
AUTOMATION_VAR = 'DS_MERGE_UP_AUTOMATION'
MERGE_UP_RULES = ('lock-pin', 'splice', 'pins')


class Error(Exception):
    pass


def git(git_dir, *args):
    r = subprocess.run(['git', '-C', git_dir, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise Error(f'git {" ".join(args)}: {r.stderr.strip() or r.stdout.strip()}')
    return r.stdout


# --- lockfile -----------------------------------------------------------------

def parse_simple_yaml(text):
    """Nested mappings of scalars, indentation based, comments stripped. Enough for base.lock.yml."""
    root = {}
    stack = [(-1, root)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split('#', 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(' '))
        key, sep, value = line.strip().partition(':')
        if not sep or not key or key.startswith('-'):
            raise Error(f'{LOCKFILE} line {lineno}: not a key: value line: {raw!r}')
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise Error(f'{LOCKFILE} line {lineno}: bad indentation')
        parent = stack[-1][1]
        value = value.strip()
        if value:
            parent[key] = value.strip('\'"')
        else:
            child = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def lock_pin(text, layer=LAYER):
    doc = parse_simple_yaml(text)
    try:
        commit = doc['overrides']['repos'][layer]['commit']
    except (KeyError, TypeError):
        raise Error(f'{LOCKFILE}: no overrides.repos.{layer}.commit')
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise Error(f'{LOCKFILE}: {layer} commit {commit!r} is not a 40-hex sha')
    return commit


def lock_pin_at(git_dir, sha, path=LOCKFILE):
    return lock_pin(git(git_dir, 'show', f'{sha}:{path}'))


def lock_pin_set(text, new, layer=LAYER):
    """The lockfile text with the layer's commit set to <new>: exactly one line changes."""
    old = lock_pin(text, layer)
    if not SHA_RE.fullmatch(new):
        raise Error(f'{LOCKFILE}: {new!r} is not a 40-hex sha')
    pattern = r'(?m)^([ ]+' + re.escape(layer) + r':\n[ ]+commit: )' + re.escape(old) + r'[ \t]*$'
    result, count = re.subn(pattern, lambda m: m.group(1) + new, text)
    if count != 1 or lock_pin(result, layer) != new:
        raise Error(f'{LOCKFILE}: unsupported layout, cannot set the {layer} commit')
    return result


class LockResultError(Error):
    """The merged lockfile does not parse back to the pin it was given (R-lock check 4)."""


def lock_merge(git_dir, tip, check=False):
    """Three-way merge of base.lock.yml inside an uncommitted merge, the meta-dsensos commit
    of base, ours and theirs all set to <tip> first (as base_merge does for the branch name).

    True when the rest of the file merges clean: the result, whose pin is <tip>, is written
    and staged unless check is set. False when another line of the lockfile conflicts (a
    second layer whose pin both lines moved): nothing is written, the work tree copy stays as
    git left it. With more than one merge base every base must give the same clean result."""
    bases = git(git_dir, 'merge-base', '--all', 'HEAD', 'MERGE_HEAD').split()
    if not bases:
        raise Error(f'{LOCKFILE}: HEAD and MERGE_HEAD share no history')
    ours = lock_pin_set(git(git_dir, 'show', f'HEAD:{LOCKFILE}'), tip)
    theirs = lock_pin_set(git(git_dir, 'show', f'MERGE_HEAD:{LOCKFILE}'), tip)
    results = set()
    with tempfile.TemporaryDirectory() as tmp:
        paths = {name: os.path.join(tmp, name) for name in ('ours', 'base', 'theirs')}
        for name, text in (('ours', ours), ('theirs', theirs)):
            with open(paths[name], 'w') as f:
                f.write(text)
        for base in bases:
            with open(paths['base'], 'w') as f:
                f.write(lock_pin_set(git(git_dir, 'show', f'{base}:{LOCKFILE}'), tip))
            r = subprocess.run(['git', 'merge-file', '-p', paths['ours'], paths['base'], paths['theirs']],
                               capture_output=True, text=True)
            if r.returncode < 0 or r.returncode > 127:
                raise Error(f'git merge-file failed: {r.stderr.strip()}')
            if r.returncode != 0:
                return False
            results.add(r.stdout)
    if len(results) != 1:
        return False
    merged = results.pop()
    try:
        pin = lock_pin(merged)
    except Error as e:
        raise LockResultError(f'the merged lockfile does not parse: {e}')
    if pin != tip:
        raise LockResultError(f'{LOCKFILE}: the merged pin is {pin}, not {tip}')
    if not check:
        with open(os.path.join(git_dir, LOCKFILE), 'w') as f:
            f.write(merged)
        git(git_dir, 'add', LOCKFILE)
    return True


# --- R-lock: the lockfile pin rule (ENG-2234) ---------------------------------

LOCK_RULE_TRAILER = 'Merge-Up-Rule'
LOCK_LANDINGS = {'push': 'A', 'bot-pr': 'B', 'pr': 'P'}


def is_ancestor(git_dir, old, new):
    """'yes', 'no' or 'unknown' for `git merge-base --is-ancestor`. Exit 1 means the commit
    is not contained; any other failure (128: an object the clone does not have) is
    'unknown'. The two are different refusals, and neither may read as 'yes'."""
    r = subprocess.run(['git', '-C', git_dir, 'merge-base', '--is-ancestor', old, new], capture_output=True, text=True)
    return 'yes' if r.returncode == 0 else 'no' if r.returncode == 1 else 'unknown'


def _non_build_policy(root=None):
    """(non_build(name), the patterns): the pin bot's definition of a layer path no build
    reads (release/pin-propagation.json `non_build_paths`, pin_policy.non_build), so R-lock
    and the pin bot agree on what "builds the same" means."""
    import pin_policy  # here, not at the top: pin_policy imports this module
    path = os.path.join(os.path.abspath(root) if root else os.path.dirname(HERE), 'release', 'pin-propagation.json')
    with open(path) as f:
        config = json.load(f)
    return (lambda name: pin_policy.non_build(name, config)), list(config.get('non_build_paths', []))


def lock_tree_check(meta_dir, parent_pin, child_pin, tip, root=None):
    """R-lock check 5: does the tip build exactly what the two locked pins build together?

    `git merge-tree --write-tree <parent pin> <child pin>` must be clean, and its tree must
    equal the tip's tree once the paths no build reads are left out. Returns (equal, text).
    A merge-tree that conflicts means the tip holds a hand-resolved layer merge, which a
    commit count cannot see (ds-tegra-distro #170)."""
    r = subprocess.run(['git', '-C', meta_dir, 'merge-tree', '--write-tree', '--name-only', '--no-messages', parent_pin, child_pin],
                       capture_output=True, text=True)
    out = r.stdout.splitlines()
    if r.returncode == 1:
        files = [f for f in out[1:] if f]
        return False, f'the two pins do not merge clean ({len(files)} conflicted file(s): {", ".join(files[:5])}{", ..." if len(files) > 5 else ""}), so the tip holds a hand-resolved layer merge'
    if r.returncode != 0 or not out or not SHA_RE.fullmatch(out[0]):
        raise Error(f'git merge-tree {parent_pin} {child_pin}: {r.stderr.strip() or r.stdout.strip() or "no tree"}')
    non_build, patterns = _non_build_policy(root)
    names = [n for n in git(meta_dir, 'diff', '--name-only', '--no-renames', '-z', out[0], f'{tip}^{{tree}}').split('\0') if n]
    differ = [n for n in names if not non_build(n)]
    skipped = len(names) - len(differ)
    ignoring = f'ignoring {", ".join(patterns) or "nothing"}' + (f'; {skipped} such path(s) differ' if skipped else '')
    if differ:
        return False, f'{len(differ)} build path(s) differ from the plain merge of the two pins ({", ".join(differ[:5])}{", ..." if len(differ) > 5 else ""}; {ignoring})'
    return True, f'the tip\'s tree equals the plain merge of the two pins ({ignoring})'


def lock_extra_commits(meta_dir, tip, parent_pin, child_pin):
    """'<sha7> <subject>' of every commit in the tip that neither pin contains, merge commits included."""
    out = git(meta_dir, 'rev-list', '--format=%h %s', '--no-commit-header', '--abbrev=7', tip, f'^{parent_pin}', f'^{child_pin}')
    return [line for line in out.splitlines() if line]


def lock_outcome(verdict, others, exact=None, extra=None):
    """How R-lock lands (plan 5.1): A direct push, B bot-finished PR, P a PR without markers
    that waits for one approval, C the lockfile pre-resolved inside a conflict PR, D refused
    (markers in the lockfile, as without the rule). `others` is the number of other
    conflicted files; exact and extra are merge_up.lock_pin.exact and .extra, and one that
    is not given is the default of that key (version.MERGE_UP_DEFAULTS: bot-pr for both, so
    a lockfile-only conflict is B unless the config says exact=push)."""
    defaults = version.MERGE_UP_DEFAULTS['lock_pin']
    exact = defaults['exact'] if exact is None else exact
    extra = defaults['extra'] if extra is None else extra
    if verdict == 'refused':
        return 'D'
    if verdict not in ('exact', 'extra'):
        raise Error(f'no outcome for the verdict {verdict!r}')
    if others:
        return 'C'
    how = exact if verdict == 'exact' else extra
    if how not in LOCK_LANDINGS:
        raise Error(f'merge_up.lock_pin: {how!r} is not one of {", ".join(LOCK_LANDINGS)}')
    return LOCK_LANDINGS[how]


def lock_rule(git_dir, meta_dir, tip, parent_pin, child_pin, planned=False, rewound='', root=None, unread=''):
    """R-lock evaluated, read-only: the checks of plan 5.1 in order, stopping at the first
    that fails. git_dir holds the uncommitted distro merge, meta_dir a meta-dsensos clone.

    verdict: 'exact' (checks 1 to 5 pass: the tip builds what the two locked pins build),
    'extra' (1 to 4 pass, the tip holds more), 'refused' (one of 1 to 4 failed: `failed` and
    `reason` say which), or 'planned' (a dry run whose tip does not exist yet: the tip is
    never used as a value, `planned_ok` says whether the rest of the lockfile merges).
    Raises Error when a check could not be run at all; the caller logs "not evaluated"."""
    for name, sha in (('tip', tip), ('parent pin', parent_pin), ('child pin', child_pin)):
        if not SHA_RE.fullmatch(sha or ''):
            raise Error(f'the {name} {sha!r} is not a 40-hex sha')
    res = {'tip': tip, 'parent_pin': parent_pin, 'child_pin': child_pin, 'checks': [], 'extra_commits': [],
           'verdict': 'refused', 'failed': None, 'reason': ''}

    def check(n, ok, text):
        res['checks'].append({'n': n, 'ok': ok, 'text': text})
        if not ok and res['failed'] is None:
            res['failed'], res['reason'] = n, text
        return ok

    if planned:
        # The new tip is the merge a real run pushes; its sha does not exist yet, so nothing
        # below may use it as a value. What can be said: the parent's pin is in the parent
        # line as it stands, the child's pin is in the child line the real run merges (the
        # caller checked that), and the rest of the lockfile merges.
        contained = is_ancestor(meta_dir, parent_pin, tip)
        merges = lock_merge(git_dir, tip, check=True)
        res.update(verdict='planned', planned_ok=contained == 'yes' and merges)
        if contained != 'yes':
            res['reason'] = f'the parent\'s pin {parent_pin} is {"not in" if contained == "no" else "an unknown object for"} the meta-dsensos parent line at {tip}'
        elif not merges:
            res['reason'] = 'the lockfile conflicts beyond the meta-dsensos commit'
        return res
    # 1. the tip is a real, pushed commit on the parent's layer line
    if rewound:
        check(1, False, f'the meta-dsensos parent line was rewritten during this run ({rewound})')
        return res
    if unread:
        check(1, False, f'the meta-dsensos parent line could not be refetched during this run, so its tip is unknown ({unread})')
        return res
    real = subprocess.run(['git', '-C', meta_dir, 'cat-file', '-e', f'{tip}^{{commit}}'], capture_output=True).returncode == 0
    if not check(1, real, f'the tip {tip} is {"a commit of" if real else "not a commit in"} the meta-dsensos clone'):
        return res
    # 2. nothing either line locked is dropped
    for who, pin in (('parent', parent_pin), ('child', child_pin)):
        state = is_ancestor(meta_dir, pin, tip)
        text = {'yes': f'the {who}\'s pin {pin} is contained in the tip',
                'no': f'the {who}\'s pin {pin} is not contained in the tip {tip}',
                'unknown': f'the {who}\'s pin {pin} is an unknown object in the meta-dsensos clone (it is on neither line)'}[state]
        if not check(2, state == 'yes', text):
            return res
    # 3. only the meta-dsensos line of the lockfile conflicts; 4. the result parses back to the tip
    try:
        merges = lock_merge(git_dir, tip, check=True)
    except LockResultError as e:
        check(3, True, 'the rest of the lockfile merges clean')
        check(4, False, str(e))
        return res
    if not check(3, merges, 'the rest of the lockfile merges clean' if merges else 'the lockfile conflicts beyond the meta-dsensos commit (another layer\'s pin moved on both lines)'):
        return res
    check(4, True, f'the merged lockfile pins {tip}')
    # 5. the tip builds exactly what the two locked pins build together
    equal, text = lock_tree_check(meta_dir, parent_pin, child_pin, tip, root)
    res['checks'].append({'n': 5, 'ok': equal, 'text': text})
    res['extra_commits'] = lock_extra_commits(meta_dir, tip, parent_pin, child_pin)
    res['verdict'] = 'exact' if equal else 'extra'
    if not equal:
        res['reason'] = text
    return res


def lock_trailer(res):
    return f'{LOCK_RULE_TRAILER}: lock-pin tip={res["tip"]} parent-pin={res["parent_pin"]} child-pin={res["child_pin"]}'


def lock_invariant(git_dir, meta_dir, parent_tip, meta_ref):
    """(ok, text): is the parent line's lock pin on the first-parent chain of its
    meta-dsensos line? A pin elsewhere (a child's pin a merge-up carried, a feature commit)
    means the parent is not building its own layer line; the text names the distro commit
    that last moved the lockfile."""
    pin = lock_pin_at(git_dir, parent_tip)
    chain = set(git(meta_dir, 'rev-list', '--first-parent', meta_ref).split())
    if pin in chain:
        return True, f'the parent\'s lock pin {pin[:7]} is on the first-parent chain of its meta-dsensos line'
    moved = git(git_dir, 'log', '-1', '--first-parent', '--format=%h %s', parent_tip, '--', LOCKFILE).strip()
    where = {'yes': 'contained in the line through a merge', 'no': 'not contained in the line',
             'unknown': 'an object the line does not have'}[is_ancestor(meta_dir, pin, meta_ref)]
    return False, (f'the parent\'s lock pin {pin} is not on the first-parent chain of its meta-dsensos line ({where}); '
                   f'the lockfile was last moved by {moved or "an unknown commit"}')


def lock_verify(git_dir, meta_dir, head, parent_tip, child_pin, tip, planned=False):
    """(the pin on the PR head, reasons it must not be finished) for a person's resolution
    of a ds-tegra-distro conflict PR (merge_up.lock_pin.verify_finish).

    The parent's lock belongs on a commit of its own layer line that holds everything
    either line locked: the head's pin must contain the parent line's previous pin and the
    child's pin, and be contained in the meta-dsensos parent tip. Taking the child's pin
    fails the first test whenever the parent had layer work of its own, which is the
    resolution that left ds-tegra-distro #153 and #158 building without it."""
    head_pin = lock_pin_at(git_dir, head)
    parent_pin = lock_pin_at(git_dir, parent_tip)
    reasons = []
    for who, pin in (('the previous pin of the parent line', parent_pin), ('the child\'s pin', child_pin)):
        state = is_ancestor(meta_dir, pin, head_pin)
        if state == 'no':
            took = ' (the resolution took the child\'s pin)' if head_pin == child_pin and pin == parent_pin else ''
            reasons.append(f'the lockfile on the PR head pins {head_pin}, which does not contain {who} {pin}{took}')
        elif state == 'unknown':
            reasons.append(f'the lockfile on the PR head pins {head_pin}; whether it contains {who} {pin} is unknown '
                           f'(one of the two is not a commit on the meta-dsensos parent or child line)')
    if not planned:
        state = is_ancestor(meta_dir, head_pin, tip)
        if state != 'yes':
            reasons.append(f'the lockfile on the PR head pins {head_pin}, which is '
                           f'{"not contained in" if state == "no" else "an unknown object for"} the meta-dsensos parent line at {tip}')
    return head_pin, reasons


def check_names(c):
    return c.get('name') or c.get('context') or ''


def checks_state(doc, head, required):
    """('green' | 'red' | 'pending' | 'moved' | 'closed', [reasons]) for a bot-finished PR,
    from `gh pr view --json state,headRefOid,statusCheckRollup`.

    green only when the PR is open, its head is still <head> and every required check has
    reported success on it. A required check that is missing or still running is pending;
    one that failed is red. Checks that are not required are not read. With no required
    check at all nothing could ever vouch for the commit, so that is red too."""
    if doc.get('state') not in (None, 'OPEN'):
        return 'closed', [f'the PR is {doc.get("state")}']
    if doc.get('headRefOid') != head:
        return 'moved', [f'the PR head is {doc.get("headRefOid")}, not the commit the bot pushed ({head})']
    if not required:
        return 'red', ['no required check is configured (merge_up.finish.required_checks), so nothing vouches for the commit']
    rollup = doc.get('statusCheckRollup') or []
    red, pending = [], []
    for name in required:
        runs = [c for c in rollup if check_names(c) == name]
        if not runs:
            pending.append(f'{name}: not reported yet')
            continue
        failed = [c for c in runs if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES]
        if failed:
            red.append(f'{name}: {failed[0].get("conclusion") or failed[0].get("state")}')
        elif not all(c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS' for c in runs):
            pending.append(f'{name}: {runs[0].get("status") or runs[0].get("conclusion") or runs[0].get("state") or "running"}')
    if red:
        return 'red', red + pending
    if pending:
        return 'pending', pending
    return 'green', [f'{name}: SUCCESS' for name in required]


# --- base.yml ----------------------------------------------------------------

def _branch_lines(text, layer=LAYER):
    """(stanza count, [(index, indent, value)]) of the `branch:` lines inside the layer's repo stanza."""
    lines = text.split('\n')
    stanzas = 0
    found = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.split('#', 1)[0].strip() == f'{layer}:':
            stanzas += 1
            indent = len(line) - len(line.lstrip(' '))
            i += 1
            while i < len(lines):
                cur = lines[i]
                body = cur.split('#', 1)[0]
                if body.strip() and (len(cur) - len(cur.lstrip(' '))) <= indent:
                    break
                m = re.fullmatch(r'(\s+)branch:\s*(\S+)\s*', body)
                if m:
                    found.append((i, m.group(1), m.group(2)))
                i += 1
            continue
        i += 1
    return stanzas, found


def base_branch_get(text, layer=LAYER):
    stanzas, found = _branch_lines(text, layer)
    if stanzas != 1:
        raise Error(f'base.yml: expected one {layer}: stanza, found {stanzas}')
    if len(found) != 1:
        raise Error(f'base.yml: expected one branch: line under {layer}:, found {len(found)}')
    return found[0][2]


def base_branch_edit(text, old, new, layer=LAYER):
    """The text with the layer's `branch: <old>` line replaced by `branch: <new>`; exactly one match."""
    stanzas, found = _branch_lines(text, layer)
    if stanzas != 1:
        raise Error(f'base.yml: expected one {layer}: stanza, found {stanzas}')
    matches = [f for f in found if f[2] == old]
    if len(matches) != 1:
        have = ', '.join(f[2] for f in found) or 'none'
        raise Error(f'base.yml: expected exactly one `branch: {old}` line under {layer}:, found {len(matches)} (branch lines: {have})')
    idx, indent, _ = matches[0]
    lines = text.split('\n')
    lines[idx] = f'{indent}branch: {new}'
    return '\n'.join(lines)


def base_branch_set(text, new, layer=LAYER):
    """The text with the layer's single `branch:` line set to <new>, whatever it was."""
    return base_branch_edit(text, base_branch_get(text, layer), new, layer)


BASE_YML = 'kas/include/base.yml'


def base_merge(git_dir, branch):
    """Three-way merge of base.yml inside an uncommitted merge, branch names normalised.

    The target keeps its meta-dsensos branch name; every other change (a new repo stanza, a
    comment, a layer's url) merges like any other file. Returns True when clean (result
    written and staged), False on a real conflict (markers written, the path left unmerged)."""
    base = git(git_dir, 'merge-base', 'HEAD', 'MERGE_HEAD').strip()
    sides = {}
    for name, rev in (('base', base), ('ours', 'HEAD'), ('theirs', 'MERGE_HEAD')):
        sides[name] = base_branch_set(git(git_dir, 'show', f'{rev}:{BASE_YML}'), branch)
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, text in sides.items():
            paths[name] = os.path.join(tmp, name)
            with open(paths[name], 'w') as f:
                f.write(text)
        r = subprocess.run(['git', 'merge-file', '-p', '--zdiff3', '-L', 'HEAD', '-L', 'merge-base', '-L', 'MERGE_HEAD',
                            paths['ours'], paths['base'], paths['theirs']],
                           capture_output=True, text=True)
    if r.returncode < 0 or r.returncode > 127:
        raise Error(f'git merge-file failed: {r.stderr.strip()}')
    with open(os.path.join(git_dir, BASE_YML), 'w') as f:
        f.write(r.stdout)
    if r.returncode == 0:
        git(git_dir, 'add', BASE_YML)
        return True
    return False


VERSIONS_README = 'versions/README.md'


def keep_target_versions(git_dir):
    """Inside an uncommitted merge: restore the target's (HEAD's) pending-version files.

    Only the per-product files: versions/README.md is documentation and merges like any
    other file, so a README change reaches every line. A versions file that exists only on
    the incoming side is left as merged (the target has no copy to prefer). Returns the paths
    restored."""
    paths = [p for p in git(git_dir, 'ls-tree', '--name-only', 'HEAD', 'versions/').split()
             if p != VERSIONS_README]
    if paths:
        git(git_dir, 'checkout', 'HEAD', '--', *paths)
    return paths


def merge_keep_target(git_dir, branch):
    """keep_target_versions, then base_merge. True when clean, False on a base.yml conflict."""
    keep_target_versions(git_dir)
    return base_merge(git_dir, branch)


# --- versions ----------------------------------------------------------------

def line_plan(product, ver, kind='auto'):
    """Where a line cut from <product>/v<ver> lives and what it opens with.

    Any N.M.P tag can open either line (ENG-1934: the pilot promotes 0.0.1 on
    main with bump=patch and then cuts both 0.x.x and 0.0.x from it). kind
    'auto' keeps the original derivation, N.0.0 -> the minor line, anything
    else -> the patch line; 'minor' and 'patch' force one.
    """
    m = version.SEMVER_CORE.fullmatch(ver)
    if not m:
        raise Error(f'version {ver!r} must be N.M.P (the tag {product}/vN.M.P the line is cut from)')
    if kind not in LINE_KINDS:
        raise Error(f'kind must be one of {LINE_KINDS}, got {kind!r}')
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    if kind == 'auto':
        kind = 'minor' if (minor == '0' and patch == '0') else 'patch'
    if kind == 'minor':
        return {'line': f'{product}/{major}.x.x', 'kind': 'minor', 'level': 'minor',
                'pending': version.bump(ver, 'minor'), 'tag': f'{product}/v{ver}'}
    return {'line': f'{product}/{major}.{minor}.x', 'kind': 'patch', 'level': 'patch',
            'pending': version.bump(ver, 'patch'), 'tag': f'{product}/v{ver}'}


def fits_line(parsed, ver):
    """True when a pending version has the shape the line can promote: anything on main,
    N.M.0 on N.x.x, N.M.P on N.M.x."""
    if not version.SEMVER_CORE.fullmatch(ver):
        return False
    kind, prefix = parsed['kind'], parsed['version_prefix']
    if kind == 'main':
        return True
    if not ver.startswith(prefix):
        return False
    rest = ver[len(prefix):]
    if kind == 'minor':
        return bool(re.fullmatch(NUM + r'\.0', rest))
    if kind == 'patch':
        return bool(re.fullmatch(NUM, rest))
    return False


def line_shape(parsed):
    kind, prefix = parsed['kind'], parsed['version_prefix']
    return {'main': 'X.Y.Z', 'minor': f'{prefix}M.0', 'patch': f'{prefix}P'}.get(kind, '?')


def promote_plan(line, pending, products, bump='auto'):
    """What a promote of <pending> on <line> tags and leaves behind (ENG-1934)."""
    parsed = lines.parse_line(line, products)
    name = lines.strip_ref(line)
    if parsed['kind'] == 'none':
        raise Error(f'{name} is not a release line of a configured product ({", ".join(products)})')
    if bump not in BUMP_LEVELS:
        raise Error(f'bump must be one of {BUMP_LEVELS}, got {bump!r}')
    if not version.SEMVER_CORE.fullmatch(pending):
        raise Error(f'pending version {pending!r} is not X.Y.Z')
    if not fits_line(parsed, pending):
        raise Error(f'pending version {pending} does not fit {name} (a {parsed["kind"]} line promotes {line_shape(parsed)})')
    level = AUTO_LEVEL[parsed['kind']] if bump == 'auto' else bump
    nxt = version.bump(pending, level)
    if not fits_line(parsed, nxt):
        raise Error(f'bump {level} after {pending} leaves pending {nxt}, which does not fit {name} '
                    f'({line_shape(parsed)}); use bump={AUTO_LEVEL[parsed["kind"]]} (auto) on a {parsed["kind"]} line')
    return {'product': parsed['product'], 'line': name, 'kind': parsed['kind'], 'version': pending,
            'tag': f'{parsed["product"]}/v{pending}', 'level': level, 'next_version': nxt}


# --- commits -----------------------------------------------------------------

def is_versions_only(git_dir, sha):
    """True when the commit has one parent and its diff touches only versions/."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) for f in files)


def is_bookkeeping(git_dir, sha):
    """True when the commit has one parent and touches only versions/ and kas/include/base.yml
    (a line's opening commit or a promote's bump): never unmerged work."""
    parents = git(git_dir, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
    if len(parents) != 1:
        return False
    files = [f for f in git(git_dir, 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines() if f]
    return bool(files) and all(f.startswith(VERSIONS_DIR) or f in BOOKKEEPING_FILES for f in files)


def unmerged(git_dir, base, head):
    """[(sha, subject)] of the commits in base..head that are not bookkeeping, oldest first."""
    out = []
    text = git(git_dir, 'rev-list', '--topo-order', '--reverse', '--format=%H %s', '--no-commit-header', f'{base}..{head}')
    for line in text.splitlines():
        sha, _, subject = line.partition(' ')
        if sha and not is_bookkeeping(git_dir, sha):
            out.append((sha, subject))
    return out


def walk_versions_only(git_dir, sha, limit=5):
    cur = git(git_dir, 'rev-parse', '--verify', f'{sha}^{{commit}}').strip()
    out = [cur]
    while len(out) <= limit and is_versions_only(git_dir, cur):
        cur = git(git_dir, 'rev-parse', '--verify', f'{cur}^1').strip()
        out.append(cur)
    return out


# --- gh JSON -----------------------------------------------------------------

def first_run(doc):
    if not isinstance(doc, list) or not doc:
        return None
    r = doc[0]
    return f'{r.get("databaseId", "")} {r.get("number", "")}'.strip()


REVIEW_STATES = ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')


def read_json_stream(path):
    """Every JSON document in a file, lists flattened: what `gh api --paginate`
    prints for a list endpoint, whether as one array, one array per page or
    one object per line."""
    with open(path) as f:
        text = f.read()
    out, pos, dec = [], 0, json.JSONDecoder()
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            return out
        doc, pos = dec.raw_decode(text, pos)
        out.extend(doc if isinstance(doc, list) else [doc])


def person_reviews(reviews):
    """({login: review}, [bot logins that approved]) from the REST reviews of a PR,
    oldest first: the last APPROVED, CHANGES_REQUESTED or DISMISSED review of
    every person. A review counts only when user.type is "User": github-actions,
    Copilot and every App report "Bot", whatever their login looks like."""
    latest, bots = {}, set()
    for rv in reviews:
        user = rv.get('user') or {}
        login, state = user.get('login'), rv.get('state')
        if not login or state not in REVIEW_STATES:
            continue
        if user.get('type') != 'User':
            if state == 'APPROVED':
                bots.add(login)
            continue
        latest[login] = rv
    return latest, sorted(bots)


# How merge-up.sh's close_superseded starts the comment it closes a PR with.
SUPERSEDED = 'Superseded by '


def latest_pr(prs):
    """(number, state, url) of the PR that speaks for a head, from a `gh pr list
    --state all --json number,state,url` answer, or None when it lists none: its
    open PR when it has one, whatever its number (a person may reopen an older PR
    after a newer one of the same head was closed), else the most recent one
    (highest number)."""
    if not prs:
        return None
    pr = max([p for p in prs if p['state'] == 'OPEN'] or prs, key=lambda p: p['number'])
    return pr['number'], pr['state'], pr.get('url') or ''


def person_no(state, issue=None, comments=None, reviews=None):
    """Why the most recent merge-up PR of a head is a person's no to it, or None.

    CLOSED (without a merge): a no, unless the bot closed it as superseded. That
    is read from two things together: `closed_by` of the REST issue is of type
    Bot, and that same account left a comment that starts the way
    close_superseded writes it. Anything else, a closer GitHub does not name
    included, is a no. OPEN: a no when the latest review of any person is
    CHANGES_REQUESTED (person_reviews: a later approval or a dismissal by the
    same person takes it back, and a bot's review never counts). A merged PR is
    never a no."""
    if state == 'CLOSED':
        closer = (issue or {}).get('closed_by') or {}
        if closer.get('type') == 'Bot' and any(
                (c.get('user') or {}).get('login') == closer.get('login') and (c.get('body') or '').startswith(SUPERSEDED)
                for c in comments or []):
            return None
        return f'{closer.get("login") or "someone GitHub does not name"} closed it without merging'
    if state == 'OPEN':
        latest, _ = person_reviews(reviews or [])
        refused = sorted(login for login, rv in latest.items() if rv.get('state') == 'CHANGES_REQUESTED')
        if refused:
            return f'{", ".join(refused)} requested changes'
    return None


PUSH_ACTIVITIES = ('push', 'force_push', 'branch_creation')


def push_actors(activity=None, commits=None):
    """(logins, how, unknown): the people who pushed to a PR branch.

    activity: the repository activity of the branch. Every actor of a push, a
    force push or the branch's creation, bots aside (the release App made the
    branch). When it is None the fallback is commits, the REST commit objects
    of the commits above the bot's commit: every author and committer with a
    login, bots aside. A commit whose author or committer GitHub maps to no
    account is returned in unknown, because nothing then shows who wrote it."""
    logins, unknown = set(), []
    if activity is not None:
        for a in activity:
            actor = a.get('actor') or {}
            if a.get('activity_type') in PUSH_ACTIVITIES and actor.get('login') and actor.get('type') != 'Bot':
                logins.add(actor['login'])
        return logins, 'the repository activity', unknown
    for c in commits or []:
        for role in ('author', 'committer'):
            user = c.get(role) or {}
            if user.get('login'):
                if user.get('type') != 'Bot':
                    logins.add(user['login'])
            else:
                ident = ((c.get('commit') or {}).get(role) or {})
                unknown.append(f'{(c.get("sha") or "?")[:7]} ({role} {ident.get("email") or ident.get("name") or "unknown"})')
    return logins, 'the authors and committers of the commits above the bot\'s commit', unknown


def required_checks(doc, required):
    """Reasons a required check is not green on the head (finish.strict): each
    name must be reported in the view's statusCheckRollup, and every run of it
    must have succeeded. Failed runs are named by pr_check's own loop."""
    reasons = []
    rollup = doc.get('statusCheckRollup') or []
    for name in required:
        runs = [c for c in rollup if check_names(c) == name]
        if not runs:
            reasons.append(f'required check {name}: not reported on the head')
            continue
        for c in runs:
            if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES:
                continue
            if not (c.get('conclusion') == 'SUCCESS' or c.get('state') == 'SUCCESS'):
                reasons.append(f'required check {name}: {c.get("conclusion") or c.get("state") or c.get("status") or "not finished"}, not SUCCESS')
                break
    return reasons


def pr_check(doc, reviews=None, strict=False, required=(), activity=None, commits=None, notes=None):
    """Reasons the PR must not be merged by the bot (empty = ok).

    reviews None: the reviews of the view itself, every author counted (sync
    finishes). reviews a list: the REST reviews, and only people count.
    strict (merge_up.finish.strict, with the REST reviews): the approval must
    be on the PR head, the approver must not have pushed to the branch
    (push_actors) and every required check must be green on the head. notes,
    a list, receives what the strict checks went by, for the log."""
    reasons = []
    if doc.get('reviewDecision') != 'APPROVED':
        reasons.append(f'reviewDecision is {doc.get("reviewDecision")!r}, not APPROVED')
    if doc.get('mergeStateStatus') != 'CLEAN':
        reasons.append(f'mergeStateStatus is {doc.get("mergeStateStatus")!r}, not CLEAN')
    pr_commits = doc.get('commits') or []
    pusher = None
    if pr_commits:
        authors = pr_commits[-1].get('authors') or []
        if authors:
            pusher = authors[0].get('login')
    if strict and reviews is None:
        raise Error('pr-check: strict needs the REST reviews (--reviews)')
    if reviews is None:
        latest = {}
        for rv in doc.get('reviews') or []:
            login = (rv.get('author') or {}).get('login')
            state = rv.get('state')
            if login and state in REVIEW_STATES:
                latest[login] = state  # gh lists reviews oldest first
        approvers = sorted(l for l, s in latest.items() if s == 'APPROVED' and l != pusher)
        if not approvers:
            reasons.append(f'no APPROVED review by someone other than the last pusher ({pusher!r})')
    else:
        latest, bots = person_reviews(reviews)
        approved = {l: rv for l, rv in latest.items() if rv.get('state') == 'APPROVED'}
        bot_note = f'; the approval by {", ".join(bots)} is a bot review and never counts' if bots else ''
        if not strict:
            approvers = sorted(l for l in approved if l != pusher)
            if not approvers:
                reasons.append(f'no APPROVED review by a person other than the last pusher ({pusher!r}){bot_note}')
        else:
            head = doc.get('headRefOid')
            if not head:
                raise Error('pr-check: strict needs headRefOid in the view')
            pushers, how, unknown = push_actors(activity, commits)
            if activity is None and commits is None:
                reasons.append('strict: neither the repository activity of the branch nor its commits could be read, '
                               'so nothing shows who pushed to it')
            if notes is not None:
                notes.append(f'push actors, from {how}: {", ".join(sorted(pushers)) or "nobody but the bot"}')
            for who in unknown:
                reasons.append(f'strict: commit {who} maps to no GitHub account, so nothing shows the approver did not write it')
            # One approval that counts is enough: strict binds the review that is counted,
            # not every review on the PR. An approval that does not count (stale, or by a
            # pusher) is a reason only when no other approval does; next to one that
            # counts it is a note in the log.
            approvers, not_counted = [], []
            for login in sorted(approved):
                on = approved[login].get('commit_id')
                if on != head:
                    not_counted.append(f'strict: {login} approved {(on or "an unknown commit")[:7]}, not the PR head {head[:7]}; '
                                       'an approval counts only for the commit it was given on')
                elif login in pushers:
                    not_counted.append(f'strict: {login} approved but also pushed to the branch ({how})')
                else:
                    approvers.append(login)
            if not approvers:
                reasons.extend(not_counted)
                reasons.append(f'strict: no APPROVED review on the PR head {head[:7]} by a person who did not push to the branch{bot_note}')
            elif notes is not None:
                notes.extend(f'not counted, {why[len("strict: "):]}' for why in not_counted)
            reasons.extend(required_checks(doc, required))
    for c in doc.get('statusCheckRollup') or []:
        name = c.get('name') or c.get('context') or c.get('workflowName') or '?'
        if c.get('conclusion') in FAILED_CONCLUSIONS or c.get('state') in FAILED_STATES:
            reasons.append(f'check {name}: {c.get("conclusion") or c.get("state")}')
    return reasons, pusher, approvers


MARKER_RE = r'^(<<<<<<< |\|\|\|\|\|\|\| |>>>>>>> )'


def marked_files(git_dir, base, head):
    """The files <head> changes against its merge base with <base> that hold a
    conflict marker line. zdiff3's base line counts: a resolver who deletes
    only the <<<<<<< and >>>>>>> lines leaves it behind. Only changed files
    are read, so a doc or a fixture on the base that shows a conflict does not
    count; deleted files are skipped."""
    merge_base = git(git_dir, 'merge-base', base, head).strip()
    changed = [f for f in git(git_dir, 'diff', '-z', '--name-only', '--diff-filter=d', merge_base, head).split('\0') if f]
    marked = []
    for i in range(0, len(changed), 200):
        r = subprocess.run(['git', '-C', git_dir, 'grep', '-l', '-z', '-E', MARKER_RE, head, '--',
                            *(f':(literal){f}' for f in changed[i:i + 200])], capture_output=True, text=True)
        if r.returncode not in (0, 1):   # 1: no file matched
            raise Error(f'git grep: {r.stderr.strip()}')
        marked += [f.split(':', 1)[1] for f in r.stdout.split('\0') if f]
    return marked


def branch_origin(git_dir, head, parent, like=None, bot_email=None):
    """('ours' | 'foreign', first commit, why) for a merge-up branch at <head>.

    The first commit above the parent is the last commit of head's first-parent
    chain that <parent> does not contain: the merge the bot pushed, with a
    resolver's commits (merges of the parent included) on top of it. With
    <like>, the commit this run made for the same merge, it is ours only when
    it has that commit's tree, parents and committer, whatever its dates. With
    only <bot_email> it is ours when it is a merge of two lines committed
    under that address, which is all a finish can know."""
    chain = git(git_dir, 'rev-list', '--first-parent', head, f'^{parent}').split()
    if not chain:
        return 'foreign', '', 'it holds no commit above the parent line'
    first = chain[-1]

    def facts(sha):
        parents, tree, email = git(git_dir, 'show', '-s', '--format=%P%n%T%n%ce', sha).splitlines()[:3]
        return parents.split(), tree, email

    parents, tree, email = facts(first)
    what = f'its first commit above the parent line, {first[:7]},'
    if len(parents) != 2:
        return 'foreign', first, f'{what} is not a merge of two lines'
    if like:
        want_parents, want_tree, want_email = facts(like)
        if email != want_email:
            return 'foreign', first, f'{what} was committed by {email}, not by the bot'
        if parents != want_parents:
            return 'foreign', first, (f'{what} merges {parents[1][:7]} onto {parents[0][:7]}, while this run merges '
                                      f'{want_parents[1][:7]} onto {want_parents[0][:7]} (a line moved since, or the bot did not make it)')
        if tree != want_tree:
            return 'foreign', first, f'{what} does not hold the tree this run computed for the same merge'
    elif bot_email is not None and email != bot_email:
        return 'foreign', first, f'{what} was committed by {email}, not by the bot'
    return 'ours', first, ''


def automation(value):
    """DS_MERGE_UP_AUTOMATION parsed into {'value', 'act', 'shadow'}: the rules
    that act and the rules that only evaluate and log, each in MERGE_UP_RULES
    order. A rule in neither list is not evaluated at all.

    - absent (None), empty or "off": nothing is evaluated.
    - "shadow": every rule evaluates and logs, none acts.
    - a comma list of rule names: those act, the others stay in shadow.
    Anything else raises Error naming the token. The words are exact (lower
    case); only space around the value and around a list item is ignored, so
    a mistyped switch stops the run instead of reading as off."""
    raw = '' if value is None else value.strip()
    if raw in ('', 'off'):
        return {'value': raw, 'act': [], 'shadow': []}
    if raw == 'shadow':
        return {'value': raw, 'act': [], 'shadow': list(MERGE_UP_RULES)}
    known = f'off, shadow, or a comma list of {", ".join(MERGE_UP_RULES)}'
    tokens = [t.strip() for t in raw.split(',')]
    for t in tokens:
        if not t:
            raise Error(f'{AUTOMATION_VAR}={raw!r}: empty item in the list (expected {known})')
        if t not in MERGE_UP_RULES:
            hint = ' ("off" and "shadow" stand alone, not in a list)' if t in ('off', 'shadow') and len(tokens) > 1 else ''
            raise Error(f'{AUTOMATION_VAR}={raw!r}: {t!r} is not a rule{hint} (expected {known})')
    return {'value': raw,
            'act': [r for r in MERGE_UP_RULES if r in tokens],
            'shadow': [r for r in MERGE_UP_RULES if r not in tokens]}


def rule_mode(auto, rule):
    """'act', 'shadow' or 'off' for one rule under a parsed automation value."""
    if rule not in MERGE_UP_RULES:
        raise Error(f'{rule!r} is not a merge-up rule ({", ".join(MERGE_UP_RULES)})')
    return 'act' if rule in auto['act'] else 'shadow' if rule in auto['shadow'] else 'off'


def automation_summary(auto):
    """The two run-summary lines: the variable as set, and what it switches on."""
    variable = f'{AUTOMATION_VAR}={auto["value"]}' if auto['value'] else f'{AUTOMATION_VAR} is absent'
    if auto['act']:
        rules = f'acting: {", ".join(auto["act"])}; shadow (evaluate and log only): {", ".join(auto["shadow"]) or "none"}'
    elif auto['shadow']:
        rules = f'none acting; shadow (evaluate and log only): {", ".join(auto["shadow"])}'
    else:
        rules = 'none (automation: off, no rule is evaluated)'
    return [f'variable:           {variable}', f'active rules:       {rules}']


def merge_up_config(root=None):
    """The "merge_up" block of release/config.json under root, validated, with
    the defaults for a missing key or a missing block (version.MERGE_UP_DEFAULTS).
    root defaults to the checkout this script is in, not the directory a
    script was started from: the config that governs a merge-up is the one on
    the ref whose scripts run (the default branch, for every automatic run and
    every finish dispatched as documented), never a child line's."""
    try:
        return version.merge_up(os.path.abspath(root) if root else os.path.dirname(HERE))
    except version.CheckError as e:
        raise Error(str(e))


def never_auto(patterns, path):
    """The first pattern of a never_auto list that matches the repo-relative
    path, or None. No rule may write to a file for which this is not None.
    The match is version.never_auto_regex's: glob_regex's patterns (** crosses
    directories), but a pattern without a slash matches any one path segment,
    a directory name as well as the file name, and case does not count."""
    for pattern in patterns:
        try:
            regex = version.never_auto_regex(pattern)
        except version.CheckError as e:
            raise Error(str(e))
        if regex.fullmatch(path):
            return pattern
    return None


# --- the merge rules: splice and pins (ENG-2234) -------------------------------

PINS_CONFIG = 'release/pin-propagation.json'
# The pins the rule acts on when it is switched on. Every other pin that has a
# branch variable is evaluated and logged only (plan 5.2: the ds pair stays in
# shadow until the rule is confirmed for it).
PINS_ACT = ('SENSOR_PORTAL_REV',)


def pins_config(root=None):
    """What the pins rule works on: {'pin_file', 'pairs'} from the pin bot's
    release/pin-propagation.json, one pair per source that has a
    branch_variable ({'rev', 'branch', 'release', 'repo', 'acts'}). release is
    the source's release_variable (the release at the pin, which moves with
    it), or None. None when the file does not exist, which leaves the rule
    inert."""
    path = os.path.join(os.path.abspath(root) if root else os.path.dirname(HERE), PINS_CONFIG)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            doc = json.load(f)
        pairs = [{'rev': name, 'branch': src['branch_variable'], 'release': src.get('release_variable'), 'repo': src['repo'],
                  'acts': name in PINS_ACT}
                 for name, src in doc['sources'].items() if src.get('branch_variable')]
        for pair in pairs:
            if pair['release'] is not None and not (isinstance(pair['release'], str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', pair['release'])
                                                    and pair['release'] not in (pair['rev'], pair['branch'])):
                raise ValueError(f'{pair["rev"]}: release_variable must be a variable name of its own, not {pair["release"]!r}')
        return {'pin_file': doc['pin_file'], 'pairs': pairs}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise Error(f'{PINS_CONFIG}: cannot be read as the pin configuration ({e!r})')


def resolve_conflicts(git_dir, out_dir, layer=False, skip=(), owner='', root=None, check=False, value=None, token=None):
    """merge_rules.resolve_conflicts under this repo's switches and lists, its
    outputs written to out_dir. layer: the open merge is meta-dsensos (its own
    never_auto list, and the pins rule). Returns the report, with the skipped
    paths that are unmerged added to 'remaining' and listed as 'skipped'."""
    # Loaded here, not at the top: every release script runs this file, and
    # only this command needs the rules.
    import merge_rules
    cfg = merge_up_config(root)
    patterns = cfg['meta_dsensos']['never_auto'] if layer else cfg['never_auto']
    auto = automation(os.environ.get(AUTOMATION_VAR) if value is None else value)
    modes = {'splice': rule_mode(auto, 'splice'), 'pins': rule_mode(auto, 'pins') if layer else 'off'}
    pins = pins_config(root) if layer else None
    if pins is None:
        modes['pins'] = 'off'
    try:
        unmerged_now = merge_rules.unmerged_paths(git_dir)
        skipped = [p for p in unmerged_now if p in skip]

        def outputs(report):
            report['skipped'] = skipped
            report['remaining'] = sorted(report['remaining'] + skipped)
            merge_rules.write_outputs(report, out_dir)

        # The outputs are written inside the rules' own undo: if they cannot be
        # written (the caller then reads "not evaluated" and goes on without the
        # rules), no file of the merge stays resolved behind the caller's back.
        return merge_rules.resolve_conflicts(
            git_dir, modes, [p for p in unmerged_now if p not in skip],
            never_auto=lambda path: never_auto(patterns, path), pins=pins,
            compare=merge_rules.gh_compare(owner, os.environ.get('MERGE_UP_READ_TOKEN') if token is None else token),
            write=not check, after=outputs)
    except merge_rules.Error as e:
        raise Error(str(e))
    except OSError as e:
        raise Error(f'the rules\' outputs could not be written ({e})')


def read_json_arg(path):
    if path and path != '-':
        with open(path) as f:
            return json.load(f)
    return json.load(sys.stdin)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('lock-pin')
    p.add_argument('sha')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--file', help='read this lockfile instead of git show')
    p = sub.add_parser('base-branch-get')
    p.add_argument('file')
    p = sub.add_parser('base-branch-edit')
    p.add_argument('file')
    p.add_argument('old')
    p.add_argument('new')
    p = sub.add_parser('merge-keep-target')
    p.add_argument('branch')
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('base-merge')
    p.add_argument('branch')
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('lock-merge')
    p.add_argument('tip')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--check', action='store_true', help='report only: write and stage nothing')
    p = sub.add_parser('lock-rule')
    p.add_argument('tip')
    p.add_argument('--git-dir', required=True)
    p.add_argument('--meta-dir', required=True)
    p.add_argument('--parent-pin', required=True)
    p.add_argument('--child-pin', required=True)
    p.add_argument('--others', type=int, default=0, help='how many other files conflict in the merge')
    p.add_argument('--planned', action='store_true', help='a dry run: the tip is the merge a real run pushes, not a commit yet')
    p.add_argument('--rewound', default='', help='the parent layer line did not descend from the tip this run started with')
    p.add_argument('--unread', default='', help='the parent layer line could not be refetched after a rejected push (why)')
    p.add_argument('--root', help='repo root with release/ (default: the checkout this script is in)')
    p = sub.add_parser('lock-invariant')
    p.add_argument('--git-dir', required=True)
    p.add_argument('--meta-dir', required=True)
    p.add_argument('--parent-tip', required=True)
    p.add_argument('--meta-ref', required=True)
    p = sub.add_parser('lock-verify')
    p.add_argument('--git-dir', required=True)
    p.add_argument('--meta-dir', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--parent-tip', required=True)
    p.add_argument('--child-pin', required=True)
    p.add_argument('--tip', required=True)
    p.add_argument('--planned', action='store_true')
    p = sub.add_parser('checks-state')
    p.add_argument('file')
    p.add_argument('--head', required=True)
    p.add_argument('--required', default='', help='comma list of check names')
    p = sub.add_parser('latest-pr', help='"<number> <state> <url>" of the open PR of a `gh pr list --state all --json number,state,url` answer, else of its most recent PR; nothing when it is empty')
    p.add_argument('file')
    p = sub.add_parser('person-no', help="why a merge-up PR is a person's no (closed by a person, or changes requested); prints nothing when it is not")
    p.add_argument('--state', required=True, help='the PR state: OPEN, CLOSED or MERGED')
    p.add_argument('--issue', help='CLOSED: the REST issue of the PR (closed_by)')
    p.add_argument('--comments', help='CLOSED: its REST issue comments')
    p.add_argument('--reviews', help='OPEN: its REST reviews')
    p = sub.add_parser('line-plan')
    p.add_argument('product')
    p.add_argument('version')
    p.add_argument('--kind', default='auto', choices=LINE_KINDS)
    p = sub.add_parser('promote-plan')
    p.add_argument('line')
    p.add_argument('pending')
    p.add_argument('--bump', default='auto', choices=BUMP_LEVELS)
    p.add_argument('--root', default=os.path.dirname(HERE), help='repo root with release/config.json')
    p = sub.add_parser('unmerged')
    p.add_argument('base')
    p.add_argument('head')
    p.add_argument('--git-dir', default='.')
    for name in ('versions-only', 'walk-versions-only'):
        p = sub.add_parser(name)
        p.add_argument('sha')
        p.add_argument('--git-dir', default='.')
        if name == 'walk-versions-only':
            p.add_argument('--max', type=int, default=5)
    p = sub.add_parser('first-run')
    p.add_argument('file', nargs='?')
    p = sub.add_parser('pr-check')
    p.add_argument('file', nargs='?')
    p.add_argument('--reviews', help='the REST reviews of the PR; only people count')
    p.add_argument('--strict', action='store_true', help='merge_up.finish.strict')
    p.add_argument('--required', default='', help='strict: comma list of check names that must be green on the head')
    p.add_argument('--activity', help='strict: the repository activity of the PR branch')
    p.add_argument('--commits', help='strict, when the activity could not be read: REST commit objects of the commits above the bot\'s commit')
    p = sub.add_parser('markers')
    p.add_argument('--base', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('branch-origin')
    p.add_argument('head')
    p.add_argument('--parent', required=True, help='a ref or commit of the parent line')
    p.add_argument('--like', help='the commit this run made for the same merge')
    p.add_argument('--bot-email')
    p.add_argument('--git-dir', default='.')
    p = sub.add_parser('resolve-conflicts')
    p.add_argument('--git-dir', default='.')
    p.add_argument('--out', required=True, help='directory for report.json, log.txt, body.md, comment.md, trailers.txt, remaining.txt')
    p.add_argument('--layer', action='store_true', help='the merge is meta-dsensos: its never_auto list, and the pins rule')
    p.add_argument('--owner', default='', help='the organisation of the app repos the pins rule compares in')
    p.add_argument('--skip', action='append', default=[], help='a path another rule owns; left alone and kept in remaining.txt')
    p.add_argument('--check', action='store_true', help='evaluate only, write nothing to the work tree')
    p.add_argument('--root', help='repo root with release/ (default: the checkout this script is in)')
    p = sub.add_parser('merge-up-config')
    p.add_argument('--root', help='repo root with release/config.json (default: the checkout this script is in)')
    p.add_argument('--key', help='dotted path inside the block, e.g. finish.strict')
    p = sub.add_parser('automation')
    p.add_argument('--value', help=f'parse this instead of ${AUTOMATION_VAR}')
    p.add_argument('--rule', choices=MERGE_UP_RULES, help='print off, shadow or act for this rule')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'lock-pin':
            if args.file:
                with open(args.file) as f:
                    print(lock_pin(f.read()))
            else:
                print(lock_pin_at(args.git_dir, args.sha))
        elif args.cmd == 'base-branch-get':
            with open(args.file) as f:
                print(base_branch_get(f.read()))
        elif args.cmd == 'base-branch-edit':
            with open(args.file) as f:
                text = f.read()
            new = base_branch_edit(text, args.old, args.new)
            with open(args.file, 'w') as f:
                f.write(new)
            print(f'{args.file}: {LAYER} branch {args.old} -> {args.new}')
        elif args.cmd == 'merge-keep-target':
            kept = keep_target_versions(args.git_dir)
            print(f'kept from the target: {", ".join(kept) or "no versions files"}')
            if not base_merge(args.git_dir, args.branch):
                print(f'{BASE_YML}: conflicts after setting the {LAYER} branch to {args.branch} on all sides', file=sys.stderr)
                return 1
            print(f'{BASE_YML}: merged, {LAYER} branch {args.branch}')
        elif args.cmd == 'base-merge':
            if not base_merge(args.git_dir, args.branch):
                print(f'{BASE_YML}: conflicts after setting the {LAYER} branch to {args.branch} on all sides', file=sys.stderr)
                return 1
            print(f'{BASE_YML}: merged, {LAYER} branch {args.branch}')
        elif args.cmd == 'lock-merge':
            if not lock_merge(args.git_dir, args.tip, check=args.check):
                print(f'{LOCKFILE}: conflicts beyond the {LAYER} commit', file=sys.stderr)
                return 1
            print(f'{LOCKFILE}: {"would merge" if args.check else "merged"}, {LAYER} commit {args.tip}')
        elif args.cmd == 'lock-rule':
            res = lock_rule(args.git_dir, args.meta_dir, args.tip, args.parent_pin, args.child_pin,
                            planned=args.planned, rewound=args.rewound, root=args.root, unread=args.unread)
            if res['verdict'] != 'planned':
                cfg = merge_up_config(args.root)['lock_pin']
                res['outcome'] = lock_outcome(res['verdict'], args.others, cfg['exact'], cfg['extra'])
                res['trailer'] = lock_trailer(res)
            print(json.dumps(res, indent=1))
        elif args.cmd == 'lock-invariant':
            ok, text = lock_invariant(args.git_dir, args.meta_dir, args.parent_tip, args.meta_ref)
            print(f'lock invariant:     ok, {text}' if ok else f'::warning::merge-up: lock invariant: {text}')
        elif args.cmd == 'lock-verify':
            head_pin, reasons = lock_verify(args.git_dir, args.meta_dir, args.head, args.parent_tip, args.child_pin, args.tip, args.planned)
            for r in reasons:
                print(f'release_git.py: lock-verify: {r}', file=sys.stderr)
            if reasons:
                return 1
            print(f'lock verifier:      ok, the PR head pins {head_pin}: it contains the parent\'s previous pin and the child\'s pin'
                  + ('' if args.planned else f' and is contained in the meta-dsensos parent tip {args.tip}'))
        elif args.cmd == 'checks-state':
            state, reasons = checks_state(read_json_arg(args.file), args.head, [n for n in args.required.split(',') if n])
            print(state)
            for r in reasons:
                print(r)
        elif args.cmd == 'latest-pr':
            found = latest_pr(read_json_arg(args.file))
            if found:
                print(*found)
        elif args.cmd == 'person-no':
            why = person_no(args.state, read_json_arg(args.issue) if args.issue else None,
                            read_json_stream(args.comments) if args.comments else None,
                            read_json_stream(args.reviews) if args.reviews else None)
            if why:
                print(why)
        elif args.cmd == 'line-plan':
            if not version.PRODUCT_NAME.fullmatch(args.product):
                raise Error(f'product {args.product!r} must match {version.PRODUCT_NAME.pattern}')
            print(json.dumps(line_plan(args.product, args.version, args.kind)))
        elif args.cmd == 'promote-plan':
            try:
                products = version.products(os.path.abspath(args.root))
            except version.CheckError as e:
                raise Error(str(e))
            print(json.dumps(promote_plan(args.line, args.pending, products, args.bump)))
        elif args.cmd == 'unmerged':
            missing = unmerged(args.git_dir, args.base, args.head)
            for sha, subject in missing:
                print(f'{sha} {subject}')
            return 1 if missing else 0
        elif args.cmd == 'versions-only':
            return 0 if is_versions_only(args.git_dir, args.sha) else 1
        elif args.cmd == 'walk-versions-only':
            print('\n'.join(walk_versions_only(args.git_dir, args.sha, args.max)))
        elif args.cmd == 'first-run':
            out = first_run(read_json_arg(args.file))
            if out is None:
                return 1
            print(out)
        elif args.cmd == 'pr-check':
            reviews = read_json_stream(args.reviews) if args.reviews else None
            doc, notes = read_json_arg(args.file), []
            required = [n for n in args.required.split(',') if n]
            reasons, pusher, approvers = pr_check(
                doc, reviews, strict=args.strict, required=required,
                activity=read_json_stream(args.activity) if args.activity else None,
                commits=read_json_stream(args.commits) if args.commits else None, notes=notes)
            for n in notes:
                print(f'strict finish:      {n}')
            for r in reasons:
                print(f'release_git.py: pr-check: {r}', file=sys.stderr)
            if reasons:
                return 1
            if args.strict:
                print(f'approved by {", ".join(approvers)} on the head {doc["headRefOid"][:7]}, none of whom pushed to the branch; mergeable, '
                      f'required checks green ({", ".join(required) or "none configured"})')
            else:
                print(f'approved by {", ".join(approvers)} (last pusher {pusher}), mergeable, checks green')
        elif args.cmd == 'markers':
            marked = marked_files(args.git_dir, args.base, args.head)
            for f in marked:
                print(f)
            if marked:
                print(f'release_git.py: markers: {len(marked)} file(s) changed by {args.head[:7]} still hold conflict markers', file=sys.stderr)
                return 1
        elif args.cmd == 'branch-origin':
            state, first, why = branch_origin(args.git_dir, args.head, args.parent, args.like, args.bot_email)
            print(f'{state} {first}'.rstrip())
            if why:
                print(why)
        elif args.cmd == 'resolve-conflicts':
            report = resolve_conflicts(args.git_dir, args.out, layer=args.layer, skip=args.skip, owner=args.owner,
                                       root=args.root, check=args.check)
            import merge_rules   # resolve_conflicts has loaded it
            for line in merge_rules.log_lines(report):
                print(line)
        elif args.cmd == 'merge-up-config':
            value = merge_up_config(args.root)
            for part in (args.key.split('.') if args.key else ()):
                if not isinstance(value, dict) or part not in value:
                    raise Error(f'merge_up has no key {args.key!r}')
                value = value[part]
            print(value if isinstance(value, str) else json.dumps(value))
        elif args.cmd == 'automation':
            auto = automation(os.environ.get(AUTOMATION_VAR) if args.value is None else args.value)
            print(rule_mode(auto, args.rule) if args.rule else '\n'.join(automation_summary(auto)))
        return 0
    except (Error, OSError, ValueError) as e:
        print(f'release_git.py: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
