#!/usr/bin/env python3
"""Whether a merge-up pull request is ready for an automatic finish (ENG-2235).

One file, byte-identical in every repository that carries the merge-up
scripts, like merge_rules.py: it names no repository and no product. Whatever
differs between them (the release App's login, the required checks, the
workflows of the release-line group) comes in as an argument.

The sweep (auto-finish.sh, run by release-auto-finish.yml on the default
branch) asks ready() about every open merge-up PR and dispatches the finish of
a ready one; the finish itself (merge-up.sh --pr N --auto-finish) asks the same
function again before it merges. Nothing here reads GitHub: the callers pass
what they read.

    auto_finish.py switch [--value V]
        DS_MERGE_UP_AUTO_FINISH (the environment variable, or --value) parsed:
        off, shadow or on. Absent, empty and off are off. Case and surrounding
        white space are ignored, as an Actions `if:` comparison ignores case.
        Exit 1 naming the accepted values for anything else
    auto_finish.py candidates FILE [--hint-pr N] [--hint-head BRANCH]
        from `gh pr list --state open --json number,headRefName,baseRefName,
        headRefOid,isCrossRepository,isDraft`: "candidate <number> <child>
        <base> <head sha> <head branch>" for every open same-repository PR, not a draft,
        whose head is merge-up/<child>/<sha7>, the hinted one first, then by
        number; "skipped <number> <why>" for a merge-up head that is not one
    auto_finish.py approvers --reviews FILE
        the people whose latest review is an approval, one login per line:
        the logins whose permission the caller reads
    auto_finish.py ready VIEW --reviews FILE --required a,b --release-app LOGIN
                         [--activity FILE] [--permissions FILE] [--comments FILE]
                         [--runs FILE --repo NAME] [--extra 'code: text']...
                         [--sources FILE --head-ref BRANCH --base BRANCH --check-runs FILE
                          --actions-runs FILE --jobs-dir DIR --head-prs FILE --events FILE
                          --other-events DIR]
        VIEW: `gh pr view --json number,state,isDraft,reviewDecision,
        mergeStateStatus,statusCheckRollup,commits,headRefOid`. --reviews:
        `gh api --paginate repos/<repo>/pulls/<n>/reviews`. --activity: `gh api
        --paginate repos/<repo>/activity?ref=refs/heads/<head>` (absent: it
        could not be read). --permissions: "<login> <permission>" lines, the
        permission field of `gh api repos/<repo>/collaborators/<login>/permission`
        (a login it does not name could not be read). --comments: `gh api
        --paginate repos/<repo>/issues/<n>/comments`. --runs (the sweep only):
        `gh api repos/<repo>/actions/workflows/release-merge-up.yml/runs`, its
        workflow_runs; --repo: the repository that holds the PR, whose name and
        number a run-name must carry to be this PR's, and owner/name, the head
        repository of a run that vouches for a required check (see --sources).
        --sources: merge_up's check_sources for the repo (`release_git.py
        merge-up-config --key ...check_sources`); --head-ref: the PR's head
        branch; --base: its base, the parent line (a workflow run vouches for
        a required check only when it is a run for this PR: the view's number,
        this base, and --repo as its head repository); --check-runs: `gh
        api --paginate repos/<repo>/commits/<head>/check-runs`; --actions-runs:
        `gh api --paginate repos/<repo>/actions/runs?head_sha=<head>`; --jobs-dir:
        <run id>.json, `gh api --paginate repos/<repo>/actions/runs/<id>/jobs?
        filter=all`, for each run source-runs names (absent check runs or Actions
        runs: they could not be read; see check_sources); --head-prs: `gh api
        --paginate repos/<repo>/pulls?state=all&head=<owner>:<head branch>`, every
        PR of the head branch; --events: `gh api --paginate repos/<repo>/issues/<n>/events`
        (absent: they could not be read; see head_branch_prs); --other-events: <number>.json,
        the same events read for each number same-base-prs prints (one it lacks could not be
        read). --extra: a reason the caller found itself. Prints
        "ready", "not-ready" or "retry", then one "code: text" line per
        reason ("approvers: <logins>" when ready). Exit 0 for all three; exit
        1 only when the inputs cannot be read
    auto_finish.py same-base-prs FILE --pr N --base BRANCH --head-ref BRANCH --release-app LOGIN
        FILE: ready's --head-prs. The numbers of the other PRs of the head branch into
        BASE that the release App opened, one per line: whose issue events the caller
        reads into ready's --other-events
    auto_finish.py source-runs --required a,b --sources FILE --head-ref BRANCH --head SHA
                               --pr N --base BRANCH --repo OWNER/NAME
                               --check-runs FILE --actions-runs FILE
        the ids of the Actions runs whose jobs ready's --jobs-dir needs, one per line
    auto_finish.py busy FILE --workflow W... [--job W=NAME]... [--jobs-dir DIR]
        FILE: the workflow runs of this repository that are queued, in
        progress, waiting, requested or pending (`gh api
        repos/<repo>/actions/runs?status=<s>`, each status, concatenated).
        One line per active run of a workflow file named by --workflow, and
        per active run of a --job workflow whose job NAME is not completed
        (DIR/<run id>.json holds `gh api repos/<repo>/actions/runs/<id>/jobs`;
        a run whose jobs are missing there counts as busy). The group is
        busy when anything is listed. Exit 1 only when FILE cannot be read
    auto_finish.py job-runs FILE --job W=NAME...
        the ids of the active runs of a --job workflow, whose jobs the caller
        reads for busy
    auto_finish.py marker merged|stopped HEAD
        the hidden last line of the PR comment the finish leaves
    auto_finish.py stop-due CODE...
        exit 0 when a refusal with these reason codes leaves a stop comment,
        1 when it is transient or already marked

Standard library only.
"""
import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from release_git import (  # noqa: E402  (sibling module; these behave the same in every repo)
    Error, latest_runs, person_reviews, pr_check, read_json_stream,
)

VAR = 'DS_MERGE_UP_AUTO_FINISH'
MODES = ('off', 'shadow', 'on')
# The `name:` of .github/workflows/release-merge-up-review.yml: a run of it does nothing.
REVIEW_TRIGGER = 'Merge-up review trigger'
HEAD_RE = re.compile(r'merge-up/(?P<child>.+)/(?P<sha7>[0-9a-f]{7})')
FINISH_MARKER_RE = re.compile(r'<!-- merge-up-auto-finish: (merged|stopped) head=([0-9a-f]{40}) -->')
# What release-merge-up.yml's run-name says for a finish the sweep dispatched: the name of the
# repository that holds the PR, and its number (two repos can both have a PR of that number).
AUTO_RUN_RE = re.compile(r'finish ([A-Za-z0-9._-]+)#(\d+) \(auto-finish\)')
# The permissions of GET repos/<repo>/collaborators/<login>/permission that can merge.
WRITE = ('admin', 'maintain', 'write')
PUSH_ACTIVITIES = ('push', 'force_push', 'branch_creation')
ACTIVE = ('queued', 'in_progress', 'waiting', 'requested', 'pending')
# An auto-finish run that failed and left no stop comment is dispatched again, at most this
# many times for one head and approval.
MAX_FAILED_RUNS = 3
FAILED_RUN_CONCLUSIONS = ('failure', 'timed_out', 'startup_failure')
# A check that has finished and is not a failure, for the merge state.
FINE = ('SUCCESS', 'SKIPPED', 'NEUTRAL')
# Codes whose cause GitHub settles by itself within seconds: the caller reads the PR again.
RETRY_CODES = ('merge-state-unknown', 'merge-state-trigger')
# Codes after which a finish that refuses leaves a stop comment: the same head and the same
# approval would be refused again, and only a newer approval or a new head changes that.
# The others go away by themselves (a check still running, a read that failed) or hold
# until a person acts anyway (changes requested, a draft, a closed PR).
MARKING_CODES = ('no-approval', 'stale-approval', 'approver-pushed', 'approved-before-push',
                 'approver-cannot-write', 'bot-pushed', 'unknown-author', 'checks-failed')
# The marking codes that judge the approvals. While the permission of an approver could not be
# read (permission-unreadable), that approval may still count at the next read, so none of these
# marks the head.
APPROVAL_CODES = ('no-approval', 'stale-approval', 'approver-pushed', 'approved-before-push', 'approver-cannot-write')


def switch(value):
    """off, shadow or on for the variable's value (None when it is not set)."""
    v = (value or '').strip().lower()
    if v in ('', 'off'):
        return 'off'
    if v in MODES:
        return v
    raise Error(f'{VAR}={value!r}: not a mode; the accepted values are off, shadow and on '
                '(absent or empty is off; case and surrounding spaces are ignored)')


def candidates(prs, hint_pr=None, hint_head=None):
    """([(number, child, base, head sha, head branch)], [(number, why)]) from a `gh pr list` answer: the
    open merge-up PRs an auto-finish may look at, the hinted one first, the rest by number."""
    kept, skipped = [], []
    for p in prs:
        head = p.get('headRefName') or ''
        if not head.startswith('merge-up/'):
            continue
        n = p.get('number')
        m = HEAD_RE.fullmatch(head)
        if not m:
            skipped.append((n, f'head {head} is not merge-up/<line>/<sha7>'))
        elif p.get('isCrossRepository') is not False:
            skipped.append((n, f'head {head} is not a branch of this repository'))
        elif p.get('isDraft'):
            skipped.append((n, 'a draft'))
        elif p.get('state') not in (None, 'OPEN'):
            skipped.append((n, f'{p.get("state")}, not open'))
        else:
            hinted = str(n) == str(hint_pr or '') or (bool(hint_head) and head == hint_head)
            kept.append((not hinted, n, (n, m.group('child'), p.get('baseRefName') or '', p.get('headRefOid') or '', head)))
    return [c for _, _, c in sorted(kept)], skipped


def _time(value):
    """A GitHub timestamp (2026-11-23T20:54:26Z) as an aware datetime, or None."""
    if not value:
        return None
    try:
        t = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def without_trigger(view):
    """The view without the check runs of the review trigger, which say nothing about the head."""
    rollup = view.get('statusCheckRollup') or []
    return dict(view, statusCheckRollup=[c for c in rollup if c.get('workflowName') != REVIEW_TRIGGER])


def code_of(reason):
    """The reason code of one pr_check reason in strict mode."""
    if reason.startswith('reviewDecision is '):
        return 'review-decision'
    if reason.startswith('mergeStateStatus is '):
        return 'merge-state'
    if reason.startswith('strict: no APPROVED review on the PR head '):
        return 'no-approval'
    if reason.startswith('strict: neither the repository activity'):
        return 'activity-unreadable'
    if reason.startswith('strict: commit ') and 'maps to no GitHub account' in reason:
        return 'unknown-author'
    if reason.startswith('strict: ') and ' approved but also pushed to the branch' in reason:
        return 'approver-pushed'
    if reason.startswith('strict: ') and ', not the PR head ' in reason:
        return 'stale-approval'
    m = re.match(r'required check .+: (.+), not SUCCESS$', reason)
    if m:
        return 'checks-skipped' if m.group(1) in ('SKIPPED', 'NEUTRAL') else 'checks-pending'
    if reason.startswith('required check ') and reason.endswith(': not reported on the head'):
        return 'checks-missing'
    if reason.startswith('check ') and ': ' in reason:
        return 'checks-failed'
    return 'unrecognised'


def _trigger_only(rollup):
    """True when every check of the rollup that is not fine is a run of the review trigger that
    has not finished, or an unfinished run GitHub has not named a workflow for yet: the merge
    state GitHub reads as UNSTABLE then turns CLEAN within seconds. A trigger run that finished
    and is not a success (cancelled, failed) does not change by itself, so it is no retry."""
    for c in latest_runs(rollup):
        done = c.get('conclusion') in FINE or c.get('state') == 'SUCCESS'
        if done:
            continue
        unfinished = (c.get('status') or '').upper() not in ('', 'COMPLETED') and not c.get('conclusion')
        if c.get('workflowName') == REVIEW_TRIGGER and unfinished:
            continue
        if not c.get('workflowName') and unfinished and 'context' not in c:
            continue
        return False
    return True


def last_marker(body):
    """(kind, head) of the marker on the last line of a comment body, or None. Only the last line
    counts: the finish writes the marker there, and text above it (a reason that quotes a file
    name, for one) is not the finish's to vouch for."""
    lines = (body or '').rstrip().splitlines()
    m = FINISH_MARKER_RE.fullmatch(lines[-1].strip()) if lines else None
    return m.groups() if m else None


def stop_markers(comments, head, release_app):
    """The times of the stop comments the release App left for <head>."""
    out = []
    for c in comments or []:
        if (c.get('user') or {}).get('login') != release_app:
            continue
        if last_marker(c.get('body')) == ('stopped', head):
            out.append(_time(c.get('created_at')))
    return out


def _auto_pr(run):
    """(repository name, PR number) of an auto-finish run, from its run-name, or None."""
    m = AUTO_RUN_RE.search(run.get('display_title') or run.get('name') or '')
    return (m.group(1), int(m.group(2))) if m else None


def ready(view, reviews, required, release_app, activity=None, permissions=None, comments=None,
          runs=None, extra=(), repo=None, sources=None, head_ref=None, check_runs=None, actions_runs=None, jobs=None,
          base=None, head_prs=None, events=None, other_events=None):
    """(status, [(code, text)], counted approvers, notes) for one open merge-up PR.

    status is 'ready', 'not-ready' or 'retry' (every reason is one GitHub settles within
    seconds: the merge state is UNKNOWN, or UNSTABLE only for the review trigger's own run).
    A PR is ready when all of these hold:
      it is open, not a draft, and no person's latest review requests changes;
      pr_check passes with strict on, whatever the configuration says: the approval is on the
        head, by a person who did not push, every required check is green and none failed;
      every required check comes from its source (check_sources: sources, the PR's head branch
        head_ref, its number (the view's), its base and repo (owner/name), and the check runs,
        Actions runs and jobs of the head; None for a read that failed, which is
        check-runs-unreadable), and no other PR of the head branch, nor a change of this PR's base,
        leaves a run of the branch untied to this PR (head_branch_prs: head_prs, events, and
        other_events, the events of the release App's same-base PRs of the branch);
      the counted approval was submitted after the newest push, force push or branch creation
        of the branch, by anyone (GitHub carries an approval to a new head after a clean merge
        of the base or a force push of the same tree, and leaves its submitted_at);
      the approver has write, maintain or admin permission (one that cannot be read does not count,
        and is its own reason, permission-unreadable, which marks nothing);
      the branch activity was read: there is no fallback to the commit authors, which a
        commit can name at will;
      nobody but the release App pushed as a bot: a bot's push names no person, so nothing
        shows who made it;
      the required list is not empty;
      mergeStateStatus is CLEAN;
      no stop comment of the release App for this head is newer than the counted approval;
      the caller found no reason of its own (extra, 'code: text').
    runs (the sweep): the recent runs of the finish workflow. A finish of this PR still queued
    or running, or MAX_FAILED_RUNS that failed since the last push and the last approval, is
    a reason too. A run is this PR's when its run-name names repo (the name of the repository
    that holds the PR; an owner/name is cut to the name) and the PR's number; runs needs repo."""
    for key in ('state', 'isDraft', 'headRefOid', 'mergeStateStatus'):
        if key not in view:
            raise Error(f'ready: the view has no {key}')
    if runs is not None and not repo:
        raise Error('ready: the runs need the name of the repository that holds the PR (--repo)')
    reasons, notes = [], []
    state = view.get('state')
    if state != 'OPEN':
        code = 'merged' if state == 'MERGED' else 'closed'
        return 'not-ready', [(code, f'the PR is {state}')], [], notes
    head = view['headRefOid']
    if view.get('isDraft'):
        reasons.append(('draft', 'the PR is a draft'))
    latest, _ = person_reviews(reviews)
    refused = sorted(login for login, rv in latest.items() if rv.get('state') == 'CHANGES_REQUESTED')
    if refused:
        reasons.append(('changes-requested', f'{", ".join(refused)} requested changes'))
    if not required:
        reasons.append(('no-required-checks', 'no required check is configured for this repository, '
                                              'so nothing vouches for the head; an auto-finish never takes it'))

    # pr_check, strict whatever the configuration says. Its merge-state reason is replaced by
    # the reading below, which tells a state GitHub is still computing from a real one.
    pr_notes = []
    pr_reasons, _, approvers = pr_check(without_trigger(view), reviews, strict=True, required=list(required),
                                        activity=activity, commits=None, notes=pr_notes)
    notes.extend(pr_notes)
    for why in pr_reasons:
        code = code_of(why)
        if code != 'merge-state':
            reasons.append((code, why[len('strict: '):] if why.startswith('strict: ') else why))
    # The same required checks again, from the REST check runs: the rollup names a check, this
    # says where it came from.
    reasons.extend(check_sources(list(required), sources, head_ref, head, check_runs, actions_runs, jobs,
                                 pr=view.get('number'), base=base, repo=repo, head_prs=head_prs, events=events,
                                 release_app=release_app, other_events=other_events))

    pushes = [a for a in activity or [] if a.get('activity_type') in PUSH_ACTIVITIES]
    newest_push = None
    if activity is not None:
        others = sorted({(a.get('actor') or {}).get('login') or 'an actor GitHub does not name'
                         for a in pushes
                         if (a.get('actor') or {}).get('type') != 'User'
                         and (a.get('actor') or {}).get('login') != release_app})
        if others:
            reasons.append(('bot-pushed', f'{", ".join(others)} pushed to the branch, and only the release App '
                                          f'({release_app}) is set aside: a bot\'s push names no person'))
        times = [_time(a.get('timestamp')) for a in pushes]
        if not pushes or None in times:
            reasons.append(('activity-unreadable', 'the activity of the branch lists no push with a time, '
                                                   'so nothing shows when it was last pushed'))
        else:
            newest_push = max(times)

    # The approvals pr_check counted, held to the last push and to write access.
    perms = permissions or {}
    counted, not_counted = [], []
    for login in approvers:
        when = _time(latest[login].get('submitted_at'))
        if activity is not None and newest_push is not None and (when is None or when <= newest_push):
            not_counted.append(('approved-before-push',
                                f'{login} approved at {latest[login].get("submitted_at") or "an unknown time"}, '
                                f'not after the last push to the branch ({newest_push:%Y-%m-%dT%H:%M:%SZ})'))
        elif login not in perms:
            # a read that failed is no answer: nothing marks the head for it
            not_counted.append(('permission-unreadable', f'the permission of {login} could not be read'))
        elif perms[login] not in WRITE:
            not_counted.append(('approver-cannot-write', f'{login} has {perms[login]} permission, not write'))
        else:
            counted.append(login)
    if approvers and not counted:
        reasons.extend(not_counted)
        reasons.append(('no-approval', f'no approval on the head {head[:7]}, given after the last push '
                                       'by a person with write access'))
    else:
        notes.extend(f'not counted, {text}' for _, text in not_counted)

    newest_approval = max((_time(latest[l].get('submitted_at')) for l in counted), default=None)
    if comments is None:
        reasons.append(('comments-unreadable', 'the comments of the PR could not be read, so a stop comment cannot be ruled out'))
    else:
        stops = stop_markers(comments, head, release_app)
        if stops:
            last = max((t for t in stops if t), default=None)
            if not (newest_approval and last and newest_approval > last):
                reasons.append(('stopped', f'an auto-finish of {head[:7]} stopped before (the release App\'s comment'
                                           f'{" of " + format(last, "%Y-%m-%dT%H:%M:%SZ") if last else ""}), and no '
                                           'approval is newer than that'))

    status = view.get('mergeStateStatus')
    if status == 'UNKNOWN':
        reasons.append(('merge-state-unknown', 'mergeStateStatus is UNKNOWN: GitHub has not computed it yet'))
    elif status == 'UNSTABLE' and _trigger_only(view.get('statusCheckRollup')):
        reasons.append(('merge-state-trigger', 'mergeStateStatus is UNSTABLE while only the review trigger runs'))
    elif status != 'CLEAN':
        reasons.append(('merge-state', f'mergeStateStatus is {status}, not CLEAN'))

    if runs is not None:
        number = view.get('number')
        me = (repo.rsplit('/', 1)[-1], int(number)) if number is not None else None
        mine = [r for r in runs if me is not None and _auto_pr(r) == me]
        active = [r for r in mine if r.get('status') != 'completed']
        if active:
            reasons.append(('in-flight', f'an auto-finish of this PR is {active[0].get("status")} '
                                         f'({active[0].get("html_url") or active[0].get("id")})'))
        # Runs of this head and this approval: started after both. A run whose start cannot be
        # read counts, so the limit cannot be dodged by a missing time.
        since = max((t for t in (newest_push, newest_approval) if t), default=None)
        failed = [r for r in mine if r.get('status') == 'completed' and r.get('conclusion') in FAILED_RUN_CONCLUSIONS
                  and (since is None or _time(r.get('created_at')) is None or _time(r.get('created_at')) > since)]
        if len(failed) >= MAX_FAILED_RUNS:
            reasons.append(('retries-exhausted', f'{len(failed)} auto-finish runs of this head failed since the last push '
                                                 'and the last approval without a stop comment; a person finishes it, '
                                                 'pushes or approves again'))

    for item in extra:
        code, _, text = item.partition(':')
        reasons.append((code.strip(), text.strip()))

    if not reasons:
        return 'ready', [], counted, notes
    if all(code in RETRY_CODES for code, _ in reasons):
        return 'retry', reasons, counted, notes
    return 'not-ready', reasons, counted, notes


# The events of an Actions run that may post a required check from a workflow source: a run for
# the pull request itself.
SOURCE_EVENTS = ('pull_request', 'pull_request_target')


def _newest(check_runs):
    """The newest of a list of REST check runs: one that has not completed is newer than every
    completed one (it reads as pending); then completed_at, started_at, and the id GitHub gave."""
    def rank(c):
        done = c.get('status') == 'completed'
        return (0 if done else 1, (c.get('completed_at') or '') if done else '', c.get('started_at') or '', c.get('id') or 0)
    return max(check_runs, key=rank)


def _is_job(job, check_run_id):
    """Whether a REST job of an Actions run is the check run <check_run_id>: an Actions job and
    its check run share the id, and the job's check_run_url names it."""
    return job.get('id') == check_run_id or (job.get('check_run_url') or '').endswith(f'/check-runs/{check_run_id}')


def this_prs_run(run, workflow, head_ref, head_sha, pr, base, repo):
    """Whether an Actions run is a run of the workflow file <workflow> for this pull request: the
    file, the head (head_sha), the PR's own branch (head_branch is head_ref), a pull_request or
    pull_request_target event, the head repository repo (owner/name), and a pull_requests entry
    with this PR's number pr whose base is base (the parent line). A run that lists no pull
    request does not count, and neither does one when pr, base or repo is not known: nothing
    then ties it to this PR. (GitHub fills pull_requests when it is read, with the open PRs of the
    run's head branch, whichever PR's event started the run; a fork's run lists none. So the entry
    ties a run to this PR only while no other PR has the branch: head_branch_prs.)"""
    if (run.get('path') != workflow or run.get('head_sha') != head_sha or run.get('head_branch') != head_ref
            or run.get('event') not in SOURCE_EVENTS):
        return False
    if pr is None or not base or not repo or (run.get('head_repository') or {}).get('full_name') != repo:
        return False
    return any(str(p.get('number')) == str(pr) and ((p.get('base') or {}).get('ref')) == base
               for p in run.get('pull_requests') or [] if isinstance(p, dict))


def _ours(name, workflow, head_ref, head_sha, check_runs, actions_runs, pr, base, repo):
    """(this PR's runs of <workflow>, the check runs named <name> on the head in their check suites)."""
    mine = [r for r in actions_runs or [] if this_prs_run(r, workflow, head_ref, head_sha, pr, base, repo)]
    suites = {r.get('check_suite_id') for r in mine if r.get('check_suite_id') is not None}
    ours = [c for c in check_runs or [] if c.get('name') == name and c.get('head_sha') in (None, head_sha)
            and (c.get('check_suite') or {}).get('id') in suites]
    return mine, ours


def _branch_prs(head_ref, head_prs):
    """The entries of head_prs that are PRs of the branch head_ref (an entry whose head.ref names
    another branch is not of it)."""
    return [p for p in head_prs or [] if isinstance(p, dict) and (p.get('head') or {}).get('ref') in (None, head_ref)]


def _base_changes(events):
    return [e for e in events or [] if isinstance(e, dict) and e.get('event') == 'base_ref_changed']


def same_base_prs(pr, base, head_ref, head_prs, release_app):
    """The numbers of the other PRs of the branch head_ref into base that the release App opened:
    the PRs whose own issue events head_branch_prs needs (other_events) before it sets one aside.
    A same-base PR anyone else opened is refused without a read."""
    return [p.get('number') for p in _branch_prs(head_ref, head_prs)
            if str(p.get('number')) != str(pr) and (p.get('base') or {}).get('ref') == base
            and release_app and (p.get('user') or {}).get('login') == release_app]


def head_branch_prs(pr, base, head_ref, head_prs, events, release_app=None, other_events=None):
    """[(code, text)]: why no Actions run of the head branch head_ref can be tied to this PR (number
    pr, base base) alone; empty when one can.

    GitHub fills a run's pull_requests when the run is read, not from the event that started it:
    with every open PR whose head is the run's branch, each with the base it has now (2c
    re-review, measured: workflow_dispatch runs listed the open PR of their branch).
    So the pull_request run of another PR from the same branch lists this PR as well while both
    are open, and this PR alone once the other is closed; and a run started while a PR's base was
    elsewhere lists it with its base of now. this_prs_run cannot tell such a run from this PR's
    own, so a workflow source vouches for nothing while:
      check-source-other-pr: another PR of the branch, open or closed (a closed one can be
        reopened, and its runs stay on the head), has a base other than base; or has this base
        but was not opened by the release App (release_app, its login; None sets none aside), or
        its own issue events (other_events) hold a base_ref_changed: a PR opened into another
        base, retargeted onto this one and closed lists only this PR in its runs (round 4
        review, finding 1). Only an earlier bot PR of the branch into the same base, never
        retargeted (the bot never retargets), runs the same checks against the same base: no
        other source;
      check-source-base-changed: this PR's base was changed at any time (a base_ref_changed
        event), or the list gives it a base other than base;
      check-source-unreadable: the list or this PR's events could not be read (None), or the
        events of a same-base PR of the App's (same_base_prs) are not in other_events.
    head_prs: the REST pull requests of the branch (GET repos/<repo>/pulls?state=all&head=
    <owner>:<head_ref>, every page, with their user). events: this PR's REST issue events (GET
    repos/<repo>/issues/<pr>/events, every page). other_events: str(number) -> the same, for the
    numbers same_base_prs names (a number it lacks could not be read).
    None of these codes marks a head: a person finishes such a PR by hand."""
    if head_prs is None or events is None:
        return [('check-source-unreadable', f'the pull requests of {head_ref} or the events of #{pr} could not be read, so a run '
                                            'another PR of the branch started cannot be told from a run of this one')]
    out = []
    others, mine, unread = [], [], []
    other_events = {str(k): v for k, v in (other_events or {}).items()}
    for p in _branch_prs(head_ref, head_prs):
        n, into = p.get('number'), (p.get('base') or {}).get('ref')
        state = p.get('state') or 'state unknown'
        if str(n) == str(pr):
            if into != base:
                mine.append(f'#{n} ({state}) into {into}')
            continue
        if into != base:
            others.append(f'#{n} ({state}) into {into}')
            continue
        login = (p.get('user') or {}).get('login')
        if not release_app or login != release_app:
            others.append(f'#{n} ({state}) into {into}, opened by {login or "an account GitHub does not name"}, not the release App')
            continue
        theirs = other_events.get(str(n))
        if theirs is None:
            unread.append(f'#{n}')
            continue
        moved = _base_changes(theirs)
        if moved:
            others.append(f'#{n} ({state}) into {into}, whose base was changed '
                          f'({", ".join(e.get("created_at") or "a time GitHub does not give" for e in moved)})')
    if others:
        out.append(('check-source-other-pr', f'{"; ".join(others)} also comes from {head_ref}: GitHub lists this PR in the runs of '
                                             f'every PR of the branch, so no run vouches for #{pr} into {base} alone; '
                                             'a person finishes it'))
    changed = _base_changes(events)
    if mine or changed:
        out.append(('check-source-base-changed', f'the base of #{pr} was changed '
                                                 f'({", ".join(mine + [e.get("created_at") or "a time GitHub does not give" for e in changed])}): '
                                                 f'a run started while it was elsewhere lists it into {base} now, so no run vouches '
                                                 'for it; a person finishes it'))
    if unread:
        out.append(('check-source-unreadable', f'the events of {", ".join(unread)}, a PR of {head_ref} into {base} the release App '
                                               'opened, could not be read, so whether its base was ever changed is unknown'))
    return out


def check_sources(required, sources, head_ref, head_sha, check_runs, actions_runs, jobs=None, pr=None, base=None, repo=None,
                  head_prs=None, events=None, release_app=None, other_events=None):
    """[(code, text)] for each required check that no run from its configured source vouches for
    on the head; empty when every one does.

    sources: merge_up's check_sources, a required name -> {'workflow': <path of the workflow
    file>} or {'app': <GitHub App slug>}. check_runs: the REST check runs of the head (GET
    repos/<repo>/commits/<sha>/check-runs, every page). actions_runs: the REST workflow runs of
    the head (GET repos/<repo>/actions/runs?head_sha=<sha>). jobs: Actions run id -> its REST
    jobs (GET repos/<repo>/actions/runs/<id>/jobs?filter=all), for the runs source_runs names.
    None for check_runs or actions_runs: it could not be read. pr, base, repo: the PR's number,
    its base (the parent line) and the repository that holds it (owner/name). head_prs, events,
    release_app, other_events: the PRs of the head branch, this PR's issue events, the release
    App's login and the issue events of its same-base PRs of the branch, for head_branch_prs,
    whose reasons come first (None: could not be read).

      workflow: the runs that count are this PR's runs of that workflow file (this_prs_run: the
        head, the PR's own branch, a pull_request or pull_request_target event, the head
        repository repo, and a pull_requests entry with number pr and base base). The NEWEST
        check run of the name among those in the check suite of one of them decides: it must
        have completed with success and be a job of that run. A check run of any other workflow,
        of the right file run for another branch, another PR or another base (a newer success
        there cannot hide this PR's newer failure), and one any token posts through the checks
        API (GitHub can file it in the run's check suite, but it is no job of the run) do not count.
      app: the NEWEST check run of the name on the head must have completed with success, posted
        by the GitHub App with that slug.
    A commit status never counts: the check-runs API does not list one, whatever its context.
    Codes, none of which marks a head (a newer run from the right source can follow on the same
    head and approval, and a mark would let anyone who can post a check run force a re-approval):
      check-runs-unreadable: the check runs, the Actions runs or a run's jobs could not be read;
      check-source-missing: no check run of that name on the head, or (workflow) none from a run
        of this PR;
      check-source-pending: the newest run that counts has not completed;
      check-source-failed: it completed without success;
      check-source: it succeeded but not from its source, or the name has no source;
      check-source-other-pr, check-source-base-changed, check-source-unreadable: head_branch_prs."""
    if check_runs is None or actions_runs is None:
        return [('check-runs-unreadable', 'the check runs or the Actions runs of the head could not be read, so where '
                                          'each required check came from is unknown')]
    jobs = {str(k): v for k, v in (jobs or {}).items()}
    out = head_branch_prs(pr, base, head_ref, head_prs, events, release_app=release_app, other_events=other_events)
    for name in required:
        runs = [c for c in check_runs if c.get('name') == name and c.get('head_sha') in (None, head_sha)]
        if not runs:
            out.append(('check-source-missing', f'{name}: no check run of that name on the head {head_sha[:7]} '
                                                '(a commit status never counts)'))
            continue
        src = (sources or {}).get(name) or {}
        mine = []
        if 'workflow' in src:
            want = src['workflow']
            mine, ours = _ours(name, want, head_ref, head_sha, runs, actions_runs, pr, base, repo)
            if not ours:
                suite = (_newest(runs).get('check_suite') or {}).get('id')
                owners = [r for r in actions_runs if suite is not None and r.get('check_suite_id') == suite]
                seen = '; '.join(f'{r.get("path")} ({r.get("event")} on {r.get("head_branch")}, '
                                 f'PRs {", ".join(_pr_names(r)) or "none listed"})' for r in owners)
                out.append(('check-source-missing', f'{name}: no check run of that name comes from a {want} run for this pull '
                                                    f'request (#{pr} into {base} from {head_ref} in {repo}), and its newest '
                                                    f'comes from {seen or "no Actions run of the head"}'))
                continue
            runs = ours
        newest = _newest(runs)
        if newest.get('status') != 'completed':
            out.append(('check-source-pending', f'{name}: its newest check run is {newest.get("status") or "not completed"}'))
            continue
        if newest.get('conclusion') != 'success':
            out.append(('check-source-failed', f'{name}: its newest check run ended {newest.get("conclusion") or "without a conclusion"}'))
            continue
        if 'app' in src:
            slug = (newest.get('app') or {}).get('slug')
            if slug != src['app']:
                out.append(('check-source', f'{name}: its newest check run was posted by {slug or "an App GitHub does not name"}, '
                                            f'not by the App {src["app"]}'))
            continue
        if 'workflow' not in src:
            out.append(('check-source', f'{name}: no source is configured for it (check_sources)'))
            continue
        suite = (newest.get('check_suite') or {}).get('id')
        held = [r for r in mine if r.get('check_suite_id') == suite]
        read = [r for r in held if str(r.get('id')) in jobs]
        if not read:
            out.append(('check-runs-unreadable', f'{name}: the jobs of the {want} run {held[0].get("id")} could not be read'))
        elif not any(_is_job(j, newest.get('id')) for r in read for j in jobs[str(r.get('id'))]):
            out.append(('check-source', f'{name}: its newest check run is in the check suite of the {want} run {read[0].get("id")} '
                                        'but is no job of it (posted through the checks API)'))
    return out


def _pr_names(run):
    return [f'#{p.get("number")} into {(p.get("base") or {}).get("ref")}' for p in run.get('pull_requests') or [] if isinstance(p, dict)]


def source_runs(required, sources, head_ref, head_sha, check_runs, actions_runs, pr=None, base=None, repo=None):
    """The ids of the Actions runs whose jobs check_sources needs: for each required name with a
    workflow source, this PR's run of that file (this_prs_run) whose check suite holds the newest
    check run of the name among this PR's runs."""
    ids = []
    for name in required:
        want = ((sources or {}).get(name) or {}).get('workflow')
        if not want:
            continue
        mine, ours = _ours(name, want, head_ref, head_sha, check_runs, actions_runs, pr, base, repo)
        if not ours:
            continue
        suite = (_newest(ours).get('check_suite') or {}).get('id')
        for r in mine:
            if r.get('check_suite_id') == suite and r.get('id') not in ids:
                ids.append(r.get('id'))
    return ids


def stop_due(codes):
    """Whether a finish refused with these codes leaves a stop comment: one of them would refuse
    the same head and approval again, and no stop comment holds them already. An approver whose
    permission could not be read (permission-unreadable) may count at the next read, so then
    no code about the approvals marks."""
    marking = [c for c in codes if c in MARKING_CODES]
    if 'permission-unreadable' in codes:
        marking = [c for c in marking if c not in APPROVAL_CODES]
    return 'stopped' not in codes and bool(marking)


def _workflow_file(path):
    return (path or '').split('@', 1)[0].rsplit('/', 1)[-1]


def busy(runs, workflows, job_rules=None, jobs=None):
    """What keeps the release-line group busy: one text per active run of a workflow in
    <workflows>, and per active run of a workflow in <job_rules> (file -> job name) whose
    job of that name is not completed. jobs maps a run id to its jobs, or lacks it when they
    could not be read, and such a run counts as busy. Runs listed twice count once."""
    out, seen = [], set()
    for r in runs:
        rid = r.get('id')
        if rid in seen or r.get('status') == 'completed':
            continue
        seen.add(rid)
        wf = _workflow_file(r.get('path'))
        where = r.get('html_url') or f'id {rid}'
        if wf in workflows:
            out.append(f'{wf} run {where} is {r.get("status")}')
        elif job_rules and wf in job_rules:
            name = job_rules[wf]
            js = (jobs or {}).get(rid)
            if js is None:
                out.append(f'{wf} run {where} is {r.get("status")} and its jobs could not be read')
                continue
            for j in js:
                if j.get('name') == name and j.get('status') != 'completed':
                    out.append(f'{wf} run {where}: job {name} is {j.get("status")}')
                    break
    return out


def job_runs(runs, job_rules):
    """The ids of the active runs of a job_rules workflow."""
    ids = []
    for r in runs:
        if r.get('status') != 'completed' and _workflow_file(r.get('path')) in job_rules and r.get('id') not in ids:
            ids.append(r.get('id'))
    return ids


def marker(kind, head):
    if kind not in ('merged', 'stopped') or not re.fullmatch(r'[0-9a-f]{40}', head or ''):
        raise Error(f'marker: want merged|stopped and a 40-character sha, got {kind} {head}')
    return f'<!-- merge-up-auto-finish: {kind} head={head} -->'


def _permissions(path):
    out = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 2:
                out[parts[0]] = parts[1]
    return out


def _runs(path):
    """Workflow runs from REST answers: a list, or objects with a workflow_runs list."""
    out = []
    for doc in read_json_stream(path):
        out.extend((doc.get('workflow_runs') or []) if isinstance(doc, dict) and 'workflow_runs' in doc else [doc])
    return out


def _check_runs(path):
    """Check runs from REST answers: a list, or objects with a check_runs list (every page)."""
    out = []
    for doc in read_json_stream(path):
        out.extend((doc.get('check_runs') or []) if isinstance(doc, dict) and 'check_runs' in doc else [doc])
    return out


def _jobs_dir(path):
    """Actions run id -> its jobs, from <path>/<run id>.json (REST answers of
    repos/<repo>/actions/runs/<id>/jobs). A run without a file there could not be read."""
    out = {}
    for name in sorted(os.listdir(path)) if path and os.path.isdir(path) else []:
        if name.endswith('.json'):
            out[name[:-len('.json')]] = [j for doc in read_json_stream(os.path.join(path, name))
                                         for j in ((doc.get('jobs') or []) if isinstance(doc, dict) and 'jobs' in doc else [doc])]
    return out


def _events_dir(path):
    """PR number -> its REST issue events, from <path>/<number>.json (every page). A number
    without a file there could not be read."""
    out = {}
    for name in sorted(os.listdir(path)) if path and os.path.isdir(path) else []:
        if re.fullmatch(r'\d+\.json', name):
            out[name[:-len('.json')]] = read_json_stream(os.path.join(path, name))
    return out


def _sources(path):
    with open(path) as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise Error(f'{path}: the check sources must be a JSON object')
    return doc


def _job_rules(items):
    rules = {}
    for item in items or []:
        wf, _, name = item.partition('=')
        if not wf or not name:
            raise Error(f'--job wants WORKFLOW=JOB, got {item!r}')
        rules[wf] = name
    return rules


def main(argv=None):
    ap = argparse.ArgumentParser(prog='auto_finish.py')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('switch')
    p.add_argument('--value')
    p = sub.add_parser('candidates')
    p.add_argument('file')
    p.add_argument('--hint-pr', default='')
    p.add_argument('--hint-head', default='')
    p = sub.add_parser('approvers')
    p.add_argument('--reviews', required=True)
    p = sub.add_parser('ready')
    p.add_argument('view')
    p.add_argument('--reviews', required=True)
    p.add_argument('--required', default='')
    p.add_argument('--release-app', required=True)
    p.add_argument('--activity')
    p.add_argument('--permissions')
    p.add_argument('--comments')
    p.add_argument('--runs')
    p.add_argument('--repo', help='the repository that holds the PR (name or owner/name), for --runs')
    p.add_argument('--extra', action='append', default=[])
    p.add_argument('--sources')
    p.add_argument('--head-ref')
    p.add_argument('--base', help="the PR's base, the parent line (a workflow source's run must be a run for it)")
    p.add_argument('--check-runs')
    p.add_argument('--actions-runs')
    p.add_argument('--jobs-dir')
    p.add_argument('--head-prs', help='every PR of the head branch, REST (absent: it could not be read)')
    p.add_argument('--events', help="the PR's REST issue events (absent: they could not be read)")
    p.add_argument('--other-events', help='<number>.json: the REST issue events of each PR same-base-prs names')
    p = sub.add_parser('same-base-prs')
    p.add_argument('file')
    p.add_argument('--pr', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--head-ref', required=True)
    p.add_argument('--release-app', required=True)
    p = sub.add_parser('source-runs')
    p.add_argument('--required', default='')
    p.add_argument('--sources', required=True)
    p.add_argument('--head-ref', required=True)
    p.add_argument('--head', required=True)
    p.add_argument('--pr', required=True)
    p.add_argument('--base', required=True)
    p.add_argument('--repo', required=True, help='owner/name of the repository that holds the PR')
    p.add_argument('--check-runs', required=True)
    p.add_argument('--actions-runs', required=True)
    p = sub.add_parser('busy')
    p.add_argument('file')
    p.add_argument('--workflow', action='append', default=[])
    p.add_argument('--job', action='append', default=[])
    p.add_argument('--jobs-dir')
    p = sub.add_parser('job-runs')
    p.add_argument('file')
    p.add_argument('--job', action='append', default=[])
    p = sub.add_parser('marker')
    p.add_argument('kind')
    p.add_argument('head')
    p = sub.add_parser('stop-due')
    p.add_argument('codes', nargs='*')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'switch':
            print(switch(a.value if a.value is not None else os.environ.get(VAR)))
        elif a.cmd == 'candidates':
            kept, skipped = candidates(read_json_stream(a.file), a.hint_pr, a.hint_head)
            for n, child, base, head, branch in kept:
                print(f'candidate {n} {child} {base} {head} {branch}')
            for n, why in skipped:
                print(f'skipped {n} {why}')
        elif a.cmd == 'approvers':
            latest, _ = person_reviews(read_json_stream(a.reviews))
            for login in sorted(l for l, rv in latest.items() if rv.get('state') == 'APPROVED'):
                print(login)
        elif a.cmd == 'ready':
            with open(a.view) as f:
                view = json.load(f)
            status, reasons, counted, notes = ready(
                view, read_json_stream(a.reviews), [n for n in a.required.split(',') if n], a.release_app,
                activity=read_json_stream(a.activity) if a.activity else None,
                permissions=_permissions(a.permissions) if a.permissions else None,
                comments=read_json_stream(a.comments) if a.comments else None,
                runs=_runs(a.runs) if a.runs else None, extra=a.extra, repo=a.repo,
                sources=_sources(a.sources) if a.sources else None, head_ref=a.head_ref,
                check_runs=_check_runs(a.check_runs) if a.check_runs else None,
                actions_runs=_runs(a.actions_runs) if a.actions_runs else None, jobs=_jobs_dir(a.jobs_dir), base=a.base,
                head_prs=read_json_stream(a.head_prs) if a.head_prs else None,
                events=read_json_stream(a.events) if a.events else None, other_events=_events_dir(a.other_events))
            print(status)
            for code, text in reasons:
                print(f'{code}: {text}')
            if status == 'ready':
                print(f'approvers: {", ".join(counted)}')
            for note in notes:
                print(f'auto_finish.py: note: {note}', file=sys.stderr)
        elif a.cmd == 'same-base-prs':
            for n in same_base_prs(a.pr, a.base, a.head_ref, read_json_stream(a.file), a.release_app):
                print(n)
        elif a.cmd == 'source-runs':
            for rid in source_runs([n for n in a.required.split(',') if n], _sources(a.sources), a.head_ref, a.head,
                                   _check_runs(a.check_runs), _runs(a.actions_runs), pr=a.pr, base=a.base, repo=a.repo):
                print(rid)
        elif a.cmd == 'busy':
            rules = _job_rules(a.job)
            jobs = {}
            if a.jobs_dir:
                for rid in job_runs(_runs(a.file), rules):
                    path = os.path.join(a.jobs_dir, f'{rid}.json')
                    if os.path.exists(path):
                        jobs[rid] = [j for doc in read_json_stream(path) for j in ((doc.get('jobs') or []) if 'jobs' in doc else [doc])]
            out = busy(_runs(a.file), a.workflow, rules, jobs)
            for line in out:
                print(line)
        elif a.cmd == 'job-runs':
            for rid in job_runs(_runs(a.file), _job_rules(a.job)):
                print(rid)
        elif a.cmd == 'marker':
            print(marker(a.kind, a.head))
        elif a.cmd == 'stop-due':
            return 0 if stop_due(a.codes) else 1
    except (Error, OSError, ValueError) as e:
        print(f'auto_finish.py: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
