"""Thin wrappers around the `git` and `gh` CLIs."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


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
