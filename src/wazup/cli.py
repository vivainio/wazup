"""wazup: what's up with this repo, right now."""

from __future__ import annotations

import argparse
import os
import sys
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


def _print_failure_detail(c: gh.CheckRun) -> None:
    tail = gh.failure_log_tail(c.details_url)
    if tail:
        print(_dim(f"       ── {c.name} (last lines before failure) ──"))
        for line in tail.splitlines():
            print(f"       {_dim('|')} {line}")
    elif c.details_url:
        print(f"       see: {c.details_url}")


def _print_checks(checks: list[gh.CheckRun], why: bool = False) -> None:
    if not checks:
        print("ci     no checks reported")
        return
    print("ci")
    for c in checks:
        print(f"       {_check_icon(c.conclusion, c.status)} {c.name}")
        if why and _is_failed(c):
            _print_failure_detail(c)


def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _pr_suffix(prs: dict[str, gh.BranchPr], branch: str) -> str:
    pr = prs.get(branch)
    if not pr:
        return ""
    draft = " draft" if pr.is_draft else ""
    return f"  {_green(f'PR #{pr.number}{draft}')}"


def _print_recent_branches(current_branch: str) -> None:
    worktrees = gh.worktree_branches()
    branches = gh.recent_local_branches(limit=8, exclude={current_branch, *worktrees})
    prs = gh.open_prs_by_branch()

    if branches:
        print("recent branches")
        for b in branches:
            print(f"       {b.name}  ({b.relative_date}){_pr_suffix(prs, b.name)}")

    worktree_entries = gh.worktree_info(exclude_branch=current_branch)
    if worktree_entries:
        print("worktrees")
        for w in worktree_entries:
            path = _dim(_display_path(w.path))
            print(f"       {w.branch}  ({w.relative_date})  {path}{_pr_suffix(prs, w.branch)}")


_MAX_LISTED_CHANGED_FILES = 10


def _print_local_status(status: gh.LocalStatus) -> None:
    if status.ahead is None:
        push_note = _dim("no upstream")
    elif status.ahead > 0:
        push_note = _yellow(f"{status.ahead} unpushed")
    else:
        push_note = _green("pushed")

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
    try:
        repo = gh.repo_info()
        branch = gh.current_branch()
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"repo   {repo.name_with_owner}  ({repo.url})")
    print(f"branch {branch}" + (" (default)" if branch == repo.default_branch else ""))
    _print_local_status(gh.local_status())

    pr = gh.pull_request_for_branch(branch)
    if pr is None:
        print("pr     none")
        if branch == repo.default_branch:
            _print_recent_branches(current_branch=branch)
        return 0

    state = pr.state.lower() + (" (draft)" if pr.is_draft else "")
    print(f"pr     #{pr.number} {pr.title}  [{state}]")
    print(f"       {pr.url}")
    if pr.review_decision:
        print(f"review {pr.review_decision.replace('_', ' ').lower()}")

    _print_checks(pr.checks, why=args.why)
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
        _print_checks(pr.checks, why=args.why)
        return 0

    try:
        runs = gh.latest_runs_for_branch(branch)
    except gh.WazupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not runs:
        print(f"no CI runs found for branch {branch}")
        return 0

    print(f"branch {branch}  (no open PR, showing latest workflow runs)")
    for r in runs:
        check = gh.CheckRun(r.name, r.status, r.conclusion, r.url)
        print(f"  {_check_icon(check.conclusion, check.status)} {check.name}  {r.url}")
        if args.why and _is_failed(check):
            _print_failure_detail(check)
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
