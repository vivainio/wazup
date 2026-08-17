"""Thin wrappers around the `git` and `gh` CLIs."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

_RUN_JOB_URL_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
_LOG_LINE_RE = re.compile(r"^[^\t]*\t[^\t]*\t\S+Z\s?")
_REMOTE_OWNER_RE = re.compile(r"github\.com[:/]([^/]+)/")


class WazupError(Exception):
    """Raised when a required CLI is missing or a command fails."""


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise WazupError(f"`{args[0]}` not found on PATH") from exc
    if result.returncode != 0:
        raise WazupError(result.stderr.strip() or f"`{' '.join(args)}` failed")
    return result.stdout.strip()


def _run_json(args: list[str]):
    return json.loads(_run(args))


def current_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])


def current_repo() -> RepoInfo | None:
    try:
        return repo_info()
    except WazupError:
        return None


def _remote_owner() -> str | None:
    try:
        url = _run(["git", "remote", "get-url", "origin"])
    except WazupError:
        return None
    match = _REMOTE_OWNER_RE.search(url)
    return match.group(1) if match else None


def ensure_gh_account_for_repo() -> str | None:
    """If this repo's owner has a logged-in gh account that isn't active, switch to it.

    The switch is permanent (persists across sessions, like running
    `gh auth switch` by hand), not just for this invocation. Returns a
    human-readable notice if a switch happened, else None. Never raises:
    any failure (no remote, gh not installed, no matching account) is a
    silent no-op, since this is a best-effort preflight, not a hard
    requirement.
    """
    owner = _remote_owner()
    if owner is None:
        return None

    try:
        data = _run_json(["gh", "auth", "status", "--json", "hosts"])
    except WazupError:
        return None

    accounts = data.get("hosts", {}).get("github.com", [])
    match = next(
        (a for a in accounts if a.get("login", "").lower() == owner.lower()), None
    )
    if match is None or match.get("active"):
        return None

    try:
        _run(["gh", "auth", "switch", "--hostname", "github.com", "--user", match["login"]])
    except WazupError:
        return None
    return f"switched active gh account to {match['login']} (owner of {owner}'s repos)"


@dataclass
class RepoInfo:
    name_with_owner: str
    url: str
    default_branch: str


def repo_info() -> RepoInfo:
    data = _run_json(
        ["gh", "repo", "view", "--json", "nameWithOwner,url,defaultBranchRef"]
    )
    return RepoInfo(
        name_with_owner=data["nameWithOwner"],
        url=data["url"],
        default_branch=(data.get("defaultBranchRef") or {}).get("name", "?"),
    )


@dataclass
class CheckRun:
    name: str
    status: str
    conclusion: str | None
    details_url: str | None = None


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    review_decision: str
    checks: list[CheckRun]


def pull_request_for_branch(branch: str) -> PullRequest | None:
    try:
        data = _run_json(
            [
                "gh",
                "pr",
                "view",
                branch,
                "--json",
                "number,title,url,state,isDraft,reviewDecision,statusCheckRollup",
            ]
        )
    except WazupError:
        return None

    checks = [
        CheckRun(
            name=c.get("name") or c.get("context", "?"),
            status=c.get("status", c.get("state", "?")),
            conclusion=c.get("conclusion"),
            details_url=c.get("detailsUrl") or c.get("targetUrl"),
        )
        for c in data.get("statusCheckRollup") or []
    ]

    return PullRequest(
        number=data["number"],
        title=data["title"],
        url=data["url"],
        state=data["state"],
        is_draft=data["isDraft"],
        review_decision=data.get("reviewDecision") or "",
        checks=checks,
    )


@dataclass
class WorkflowRun:
    name: str
    status: str
    conclusion: str | None
    url: str


def latest_runs_for_branch(branch: str, limit: int = 5) -> list[WorkflowRun]:
    data = _run_json(
        [
            "gh",
            "run",
            "list",
            "--branch",
            branch,
            "--limit",
            str(limit),
            "--json",
            "name,status,conclusion,url",
        ]
    )
    return [
        WorkflowRun(
            name=r["name"],
            status=r["status"],
            conclusion=r.get("conclusion"),
            url=r["url"],
        )
        for r in data
    ]


@dataclass
class PullRequestSummary:
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    updated_at: str
    repo: str | None = None


def my_open_prs_in_repo() -> list[PullRequestSummary]:
    data = _run_json(
        [
            "gh",
            "pr",
            "list",
            "--author",
            "@me",
            "--json",
            "number,title,url,state,isDraft,updatedAt",
        ]
    )
    return [
        PullRequestSummary(
            number=p["number"],
            title=p["title"],
            url=p["url"],
            state=p["state"],
            is_draft=p["isDraft"],
            updated_at=p["updatedAt"],
        )
        for p in data
    ]


def prs_awaiting_my_review(since: str) -> list[PullRequestSummary]:
    data = _run_json(
        [
            "gh",
            "search",
            "prs",
            "--review-requested",
            "@me",
            "--state",
            "open",
            "--updated",
            f">={since}",
            "--sort",
            "updated",
            "--json",
            "number,title,url,state,isDraft,updatedAt,repository",
        ]
    )
    return [
        PullRequestSummary(
            number=p["number"],
            title=p["title"],
            url=p["url"],
            state=p["state"],
            is_draft=p["isDraft"],
            updated_at=p["updatedAt"],
            repo=p.get("repository", {}).get("nameWithOwner"),
        )
        for p in data
    ]


def failure_log_tail(details_url: str | None, lines: int = 25) -> str | None:
    """Best-effort tail of a failed GitHub Actions job's log, ending at the error."""
    if not details_url:
        return None
    match = _RUN_JOB_URL_RE.search(details_url)
    if not match:
        return None
    run_id, job_id = match.groups()

    try:
        raw = _run(
            ["gh", "run", "view", run_id, "--job", job_id, "--log-failed"]
        )
    except WazupError:
        return None

    cleaned = [_LOG_LINE_RE.sub("", line) for line in raw.splitlines() if line.strip()]
    if not cleaned:
        return None

    error_idx = next(
        (i for i in range(len(cleaned) - 1, -1, -1) if "##[error]" in cleaned[i]),
        len(cleaned) - 1,
    )
    start = max(0, error_idx - lines + 1)
    return "\n".join(cleaned[start : error_idx + 1])


def my_recent_prs(since: str) -> list[PullRequestSummary]:
    data = _run_json(
        [
            "gh",
            "search",
            "prs",
            "--author",
            "@me",
            "--updated",
            f">={since}",
            "--sort",
            "updated",
            "--json",
            "number,title,url,state,isDraft,updatedAt,repository",
        ]
    )
    return [
        PullRequestSummary(
            number=p["number"],
            title=p["title"],
            url=p["url"],
            state=p["state"],
            is_draft=p["isDraft"],
            updated_at=p["updatedAt"],
            repo=p.get("repository", {}).get("nameWithOwner"),
        )
        for p in data
    ]
