"""wazup: what's up with this repo, right now."""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from . import gh


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(text: str) -> str:
    return _color(text, "32")


def _red(text: str) -> str:
    return _color(text, "31")


def _yellow(text: str) -> str:
    return _color(text, "33")


def _dim(text: str) -> str:
    return _color(text, "2")


def _check_icon(conclusion: str | None, status: str) -> str:
    s = (status or "").upper()
    c = (conclusion or "").upper()
    if s in {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}:
        return _yellow("~")
    if c == "SUCCESS" or s == "SUCCESS":
        return _green("✓")
    if c in {"FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"} or s in {
        "FAILURE",
        "ERROR",
    }:
        return _red("✗")
    return _dim("?")


def _is_failed(c: gh.CheckRun) -> bool:
    conclusion = (c.conclusion or "").upper()
    status = (c.status or "").upper()
    return conclusion in {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE"} or status in {
        "FAILURE",
        "ERROR",
    }


def _fetch_failure_info(
    checks: list[gh.CheckRun], why: bool
) -> list[tuple[str | None, str | None]]:
    """(summary, log tail) per check, fetched concurrently — each is an
    independent `gh` call, so threads (I/O-bound, stdlib-only) turn N
    sequential round trips into ~1."""
    if not checks:
        return []

    def fetch(c: gh.CheckRun) -> tuple[str | None, str | None]:
        summary = gh.failed_steps_summary(c.details_url)
        tail = gh.failure_log_tail(c.details_url) if why else None
        return summary, tail

    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        return list(pool.map(fetch, checks))


def _print_failure_detail(c: gh.CheckRun, tail: str | None) -> None:
    if tail:
        print(_dim(f"       ── {c.name} (last lines before failure) ──"))
        for line in tail.splitlines():
            print(f"       {_dim('|')} {line}")
    elif c.details_url:
        print(f"       see: {c.details_url}")


def _print_why_hint(cmd: str) -> None:
    # spells out the exact command (not just "pass --why") since this is
    # parsed by AI agents as often as read by humans, and a vague hint gets
    # ignored in favor of the agent improvising raw `gh`/`git` commands
    print(f"       {_dim(f'run `{cmd} --why` to see the failing log')}")


def _print_checks(checks: list[gh.CheckRun], cmd: str, why: bool = False) -> None:
    if not checks:
        print("ci     no checks reported")
        return
    print("ci")
    failed = [c for c in checks if _is_failed(c)]
    info = iter(_fetch_failure_info(failed, why))

    for c in checks:
        print(f"       {_check_icon(c.conclusion, c.status)} {c.name}")
        if _is_failed(c):
            summary, tail = next(info)
            if summary:
                print(f"         {_dim(summary)}")
            if why:
                _print_failure_detail(c, tail)
    if failed and not why:
        _print_why_hint(cmd)


def _print_ci_fallback(branch: str, why: bool, cmd: str) -> bool:
    """CI status for a branch with no open PR, from its latest workflow
    runs — this is what `pr.checks` would show if there were a PR to attach
    to. Returns whether any runs were found."""
    runs = gh.latest_runs_for_branch(branch, limit=10)
    if not runs:
        return False

    checks = [gh.CheckRun(r.name, r.status, r.conclusion, r.url) for r in runs]
    latest = checks[0]
    earlier_failures = sum(1 for c in checks[1:] if _is_failed(c))

    print("ci")
    print(f"       {_check_icon(latest.conclusion, latest.status)} {latest.name}  {latest.details_url}")

    if not _is_failed(latest):
        if earlier_failures:
            note = f"fixed — {earlier_failures} of the last {len(checks)} runs had failed"
            print(f"         {_dim(note)}")
        return True

    summary, tail = _fetch_failure_info([latest], why)[0]
    if summary:
        print(f"         {_dim(summary)}")
    if why:
        _print_failure_detail(latest, tail)
    if earlier_failures:
        print(f"         {_dim(f'{earlier_failures + 1} of the last {len(checks)} runs failed')}")
    if not why:
        _print_why_hint(cmd)
    return True


def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _pr_suffix(prs: dict[str, gh.BranchPr], branch: str) -> str:
    pr = prs.get(branch)
    if not pr:
        return ""
    draft = " draft" if pr.is_draft else ""
    return f"  {_green(f'PR #{pr.number}{draft}')}"


def _unmerged_note(unmerged: int | None) -> str:
    if unmerged is None:
        return ""
    if unmerged == 0:
        return f"  {_dim('merged')}"
    return f"  {_yellow(f'{unmerged} unmerged')}"


def _print_recent_branches(current_branch: str) -> None:
    # called only when current_branch is the repo's default branch, so it
    # doubles as the ref worktrees are checked for unmerged commits against
    worktrees = gh.worktree_branches()
    all_branches = gh.local_branches_by_recency(exclude={current_branch, *worktrees})
    branches = [b for b in all_branches if not gh.is_orphaned_worktree_branch_name(b.name)][:8]
    prs = gh.open_prs_by_branch()

    if branches:
        print("recent branches")
        for b in branches:
            print(f"       {b.name}  ({b.relative_date}){_pr_suffix(prs, b.name)}")

    worktree_entries = gh.worktree_info(
        exclude_branch=current_branch, default_ref=f"origin/{current_branch}"
    )
    if worktree_entries:
        print("worktrees")
        for w in worktree_entries:
            path = _dim(_display_path(w.path))
            print(
                f"       {w.branch}  ({w.relative_date})  {path}"
                f"{_pr_suffix(prs, w.branch)}{_unmerged_note(w.unmerged)}"
            )


_MAX_LISTED_CHANGED_FILES = 10


def _print_local_status(status: gh.LocalStatus) -> None:
    if status.ahead is None:
        push_note = _dim("no upstream")
    else:
        notes = []
        if status.ahead > 0:
            notes.append(f"{status.ahead} unpushed")
        if status.behind:
            notes.append(f"{status.behind} behind")
        push_note = _yellow(", ".join(notes)) if notes else _green("pushed")

    tree_note = _red("dirty") if status.changed_files else _green("clean")
    untracked_note = (
        _dim(f"  (+{status.untracked_count} untracked)") if status.untracked_count else ""
    )
    print(f"local  {push_note}, {tree_note}{untracked_note}")

    if len(status.changed_files) > _MAX_LISTED_CHANGED_FILES:
        print(f"       {len(status.changed_files)} files changed")
    else:
        for f in status.changed_files:
            print(f"       {f.status} {f.path}")


def cmd_status(args: argparse.Namespace) -> int:
    fetch_thread = gh.start_background_fetch()
    try:
        repo = gh.repo_info()
        branch = gh.current_branch()
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"repo   {repo.name_with_owner}  ({repo.url})")
    print(f"branch {branch}" + (" (default)" if branch == repo.default_branch else ""))
    fetch_thread.join(timeout=5)  # cap the wait; a slow fetch just means stale ahead/behind
    _print_local_status(gh.local_status())

    pr = gh.pull_request_for_branch(branch)
    if pr is None:
        print("pr     none")
        try:
            found = _print_ci_fallback(branch, args.why, cmd="wazup")
        except gh.WazupError:
            found = False
        if not found:
            print("ci     none")
        if branch == repo.default_branch:
            _print_recent_branches(current_branch=branch)
        return 0

    state = pr.state.lower() + (" (draft)" if pr.is_draft else "")
    print(f"pr     #{pr.number} {pr.title}  [{state}]")
    print(f"       {pr.url}")
    if pr.review_decision:
        print(f"review {pr.review_decision.replace('_', ' ').lower()}")

    _print_checks(pr.checks, cmd="wazup", why=args.why)
    return 0


def cmd_ci(args: argparse.Namespace) -> int:
    try:
        branch = gh.current_branch()
        pr = gh.pull_request_for_branch(branch)
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if pr is not None:
        print(f"pr #{pr.number} {pr.title}")
        _print_checks(pr.checks, cmd="wazup ci", why=args.why)
        return 0

    print(f"branch {branch}  (no open PR)")
    try:
        found = _print_ci_fallback(branch, args.why, cmd="wazup ci")
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not found:
        print(f"no CI runs found for branch {branch}")
    return 0


def _print_pr_list(prs: list[gh.PullRequestSummary], show_repo: bool) -> None:
    if not prs:
        print("  none")
        return
    for p in prs:
        state = p.state.lower() + (" draft" if p.is_draft else "")
        prefix = f"{p.repo}  " if show_repo and p.repo else ""
        print(f"  {prefix}#{p.number} {p.title}  [{state}]  {p.updated_at}")
        print(f"      {p.url}")


def cmd_my(args: argparse.Namespace) -> int:
    try:
        repo = gh.current_repo()
        if repo is not None:
            print(f"your open PRs in {repo.name_with_owner}:")
            _print_pr_list(gh.my_open_prs_in_repo(), show_repo=False)
        else:
            since = (date.today() - timedelta(days=7)).isoformat()
            print(f"your PRs with activity since {since}:")
            _print_pr_list(gh.my_recent_prs(since), show_repo=True)
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    since = (date.today() - timedelta(days=7)).isoformat()
    print(f"PRs awaiting your review, updated since {since}:")
    try:
        _print_pr_list(gh.prs_awaiting_my_review(since), show_repo=True)
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _add_why_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-w",
        "--why",
        action="store_true",
        help="for failed checks, show the tail of the failing job's log",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wazup", description="what's up with this repo, right now"
    )
    parser.set_defaults(func=cmd_status)
    _add_why_flag(parser)
    sub = parser.add_subparsers(dest="command")

    p_ci = sub.add_parser("ci", help="show CI status for the current branch/PR")
    p_ci.set_defaults(func=cmd_ci)
    _add_why_flag(p_ci)

    p_my = sub.add_parser(
        "my", help="list your open PRs, or recent PR activity outside a repo"
    )
    p_my.set_defaults(func=cmd_my)

    p_review = sub.add_parser(
        "review", help="list PRs awaiting your review, updated this week"
    )
    p_review.set_defaults(func=cmd_review)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    notice = gh.ensure_gh_account_for_repo()
    if notice:
        print(_dim(notice), file=sys.stderr)

    sys.exit(args.func(args))
