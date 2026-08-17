"""Thin wrappers around the `git` and `gh` CLIs."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime

_RUN_JOB_URL_RE = re.compile(r"/actions/runs/(\d+)/job/(\d+)")
_RUN_URL_RE = re.compile(r"/actions/runs/(\d+)")
_FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
_LOG_LINE_RE = re.compile(r"^[^\t]*\t[^\t]*\t\S+Z\s?")
_REMOTE_OWNER_RE = re.compile(r"github\.com[:/]([^/]+)/")


def is_orphaned_worktree_branch_name(name: str) -> bool:
    """True for a `worktree/`-prefixed branch name (herdr's convention for
    throwaway worktree branches). Only meaningful for branches that aren't
    currently checked out in a live worktree (those are handled separately)
    — a match here means the worktree was removed but the branch it
    generated was left behind."""
    return name.startswith("worktree/")


class WazupError(Exception):
    """Raised when a required CLI is missing or a command fails."""


def _run(args: list[str], env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, check=False, env=env
        )
    except FileNotFoundError as exc:
        raise WazupError(f"`{args[0]}` not found on PATH") from exc
    if result.returncode != 0:
        raise WazupError(result.stderr.strip() or f"`{' '.join(args)}` failed")
    return result.stdout.strip()


def _run_json(args: list[str]):
    return json.loads(_run(args))


def current_branch() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])


@dataclass
class ChangedFile:
    status: str  # single-letter: M, A, D, R, C, U
    path: str


@dataclass
class LocalStatus:
    ahead: int | None
    behind: int | None
    changed_files: list[ChangedFile]
    untracked_count: int


def start_background_fetch() -> threading.Thread:
    """Kick off `git fetch` on its own thread so `local_status()` can report
    ahead/behind against origin without a stale remote-tracking ref. Runs
    concurrently with the gh API calls the caller is about to make, so its
    latency overlaps with work that's already happening rather than adding
    to it — join the returned thread before calling `local_status()`.
    Failure (offline, no fetch access, etc.) is a silent no-op, same as
    `ensure_gh_account_for_repo`: this is best-effort freshness, not a hard
    requirement. Runs with prompts disabled (GIT_TERMINAL_PROMPT=0,
    batch-mode SSH) so a repo with no cached credentials fails fast instead
    of blocking forever on a prompt nothing can answer — there's no TTY
    attached to a background thread we `join()` on before printing."""

    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh") + " -o BatchMode=yes",
    }

    def fetch() -> None:
        try:
            _run(["git", "fetch"], env=env)
        except WazupError:
            pass

    thread = threading.Thread(target=fetch, daemon=True)
    thread.start()
    return thread


def local_status() -> LocalStatus:
    """Unpushed/behind commit counts (None if no upstream), tracked changes
    (staged or unstaged), and a separate untracked-file count. Untracked
    files are kept out of `changed_files` since they're often noise (scratch
    files, build output) rather than something you forgot to commit."""
    try:
        data = _run(["git", "status", "--porcelain=v2", "--branch"])
    except WazupError:
        return LocalStatus(ahead=None, behind=None, changed_files=[], untracked_count=0)

    ahead = None
    behind = None
    changed: list[ChangedFile] = []
    untracked_count = 0
    for line in data.splitlines():
        if line.startswith("# branch.ab "):
            _, _, ahead_str, behind_str = line.split()
            ahead = int(ahead_str.lstrip("+"))
            behind = int(behind_str.lstrip("-"))
        elif line.startswith("1 "):
            xy, path = line.split(" ", 8)[1], line.split(" ", 8)[8]
            changed.append(ChangedFile(status=xy[0] if xy[0] != "." else xy[1], path=path))
        elif line.startswith("2 "):
            xy, rest = line.split(" ", 8)[1], line.split(" ", 8)[8]
            path = rest.split("\t", 1)[0]
            changed.append(ChangedFile(status=xy[0] if xy[0] != "." else xy[1], path=path))
        elif line.startswith("u "):
            path = line.split(" ", 10)[10]
            changed.append(ChangedFile(status="U", path=path))
        elif line.startswith("? "):
            untracked_count += 1

    return LocalStatus(
        ahead=ahead, behind=behind, changed_files=changed, untracked_count=untracked_count
    )


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


def _login_matches_owner(login: str, owner: str) -> bool:
    if login.lower() == owner.lower():
        return True
    # SSO-linked corporate identities are commonly "personalname_OrgName" —
    # match those against org-owned repos by the part after the underscore.
    if "_" in login:
        return login.rsplit("_", 1)[1].lower() == owner.lower()
    return False


def ensure_gh_account_for_repo() -> str | None:
    """If this repo's owner has a logged-in gh account that isn't active, switch to it.

    Matches either an exact login (personal repos) or a "name_OrgName"
    corporate SSO identity against the org (org repos). The switch is
    permanent (persists across sessions, like running `gh auth switch` by
    hand), not just for this invocation. Returns a human-readable notice
    if a switch happened, else None. Never raises: any failure (no
    remote, gh not installed, no matching account) is a silent no-op,
    since this is a best-effort preflight, not a hard requirement.
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
        (a for a in accounts if _login_matches_owner(a.get("login", ""), owner)), None
    )
    if match is None or match.get("active"):
        return None

    try:
        _run(["gh", "auth", "switch", "--hostname", "github.com", "--user", match["login"]])
    except WazupError:
        return None
    return f"switched active gh account to {match['login']} (owner of {owner}'s repos)"


@dataclass
class BranchInfo:
    name: str
    relative_date: str


def local_branches_by_recency(exclude: set[str] | None = None) -> list[BranchInfo]:
    """All local branches, most recently committed to first."""
    exclude = exclude or set()
    try:
        data = _run(
            [
                "git",
                "for-each-ref",
                "refs/heads/",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(committerdate:relative)",
            ]
        )
    except WazupError:
        return []

    branches = []
    for line in data.splitlines():
        name, _, relative_date = line.partition("\t")
        if name and name not in exclude:
            branches.append(BranchInfo(name=name, relative_date=relative_date))
    return branches


def worktree_branches() -> dict[str, str]:
    """Map local branch name -> worktree path, for branches checked out in a
    worktree (including the current one)."""
    try:
        data = _run(["git", "worktree", "list", "--porcelain"])
    except WazupError:
        return {}

    result: dict[str, str] = {}
    path: str | None = None
    for line in data.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/") and path is not None:
            result[line[len("branch refs/heads/") :]] = path
    return result


@dataclass
class WorktreeInfo:
    branch: str
    path: str
    relative_date: str
    unmerged: int | None
    dirty: bool


def worktree_info(exclude_branch: str, default_ref: str | None = None) -> list[WorktreeInfo]:
    """Other worktrees' branches, most recently committed to first. If
    `default_ref` is given (e.g. "origin/main"), each entry also reports how
    many commits it has that aren't reachable from that ref — 0 means it's
    fully merged and the worktree is safe to prune. None (rather than a
    count) means it couldn't be determined, e.g. `default_ref` doesn't exist
    locally yet."""
    entries: list[tuple[int, WorktreeInfo]] = []
    for branch, path in worktree_branches().items():
        if branch == exclude_branch:
            continue
        try:
            data = _run(["git", "log", "-1", "--format=%ct\t%cr", branch])
        except WazupError:
            continue
        timestamp, _, relative_date = data.partition("\t")

        unmerged = None
        if default_ref is not None:
            try:
                unmerged = int(_run(["git", "rev-list", "--count", f"{default_ref}..{branch}"]))
            except WazupError:
                pass

        try:
            dirty = bool(_run(["git", "-C", path, "status", "--porcelain"]))
        except WazupError:
            dirty = False

        entries.append(
            (
                int(timestamp),
                WorktreeInfo(
                    branch=branch,
                    path=path,
                    relative_date=relative_date,
                    unmerged=unmerged,
                    dirty=dirty,
                ),
            )
        )
    entries.sort(key=lambda e: e[0], reverse=True)
    return [w for _, w in entries]


@dataclass
class BranchPr:
    number: int
    url: str
    state: str
    is_draft: bool


def open_prs_by_branch() -> dict[str, BranchPr]:
    """One call to map every open PR's head branch to its PR, for cheap lookup."""
    try:
        data = _run_json(
            [
                "gh",
                "pr",
                "list",
                "--json",
                "number,url,state,isDraft,headRefName",
                "--limit",
                "100",
            ]
        )
    except WazupError:
        return {}
    return {
        p["headRefName"]: BranchPr(
            number=p["number"], url=p["url"], state=p["state"], is_draft=p["isDraft"]
        )
        for p in data
    }


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


_RUNNING_STATUSES = {"queued", "in_progress", "waiting", "pending", "requested"}


def active_runs(limit: int = 20) -> list[WorkflowRun]:
    """Currently running/queued workflow runs anywhere in the repo, not
    scoped to a branch — a release-triggered run's headBranch is the tag,
    not the branch whose push triggered CI, so `--branch` filtering misses
    it entirely."""
    data = _run_json(
        [
            "gh",
            "run",
            "list",
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
        if (r.get("status") or "").lower() in _RUNNING_STATUSES
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


def _job_log_tail(run_id: str, job_id: str, lines: int) -> str | None:
    try:
        raw = _run(["gh", "run", "view", run_id, "--job", job_id, "--log-failed"])
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


@functools.lru_cache(maxsize=None)
def _run_jobs(run_id: str) -> tuple[dict, ...]:
    """Cached: a PR's checks are usually jobs of the same workflow run, so
    without this, listing N failed checks from one run would re-fetch the
    identical job list N times."""
    try:
        return tuple(_run_json(["gh", "run", "view", run_id, "--json", "jobs"])["jobs"])
    except WazupError:
        return ()


def _find_failed_job(run_id: str) -> dict | None:
    return next(
        (j for j in _run_jobs(run_id) if (j.get("conclusion") or "").lower() in _FAILED_CONCLUSIONS),
        None,
    )


def failure_log_tail(details_url: str | None, lines: int = 25) -> str | None:
    """Best-effort tail of a failed GitHub Actions job's log, ending at the error.

    Accepts either a job-level detailsUrl (from a PR check) or a bare
    run-level URL (from `gh run list`, which has no job id) — for the
    latter, the failed job is resolved via `gh run view --json jobs`.
    """
    if not details_url:
        return None

    job_match = _RUN_JOB_URL_RE.search(details_url)
    if job_match:
        run_id, job_id = job_match.groups()
    else:
        run_match = _RUN_URL_RE.search(details_url)
        if not run_match:
            return None
        run_id = run_match.group(1)
        failed_job = _find_failed_job(run_id)
        if failed_job is None:
            return None
        job_id = str(failed_job["databaseId"])

    return _job_log_tail(run_id, job_id, lines)


def _duration(started: str | None, completed: str | None) -> str | None:
    if not started or not completed:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return f"{int((end - start).total_seconds())}s"


def _failed_step_summaries(job: dict) -> list[str]:
    summaries = []
    for s in job.get("steps") or []:
        if (s.get("conclusion") or "").lower() not in _FAILED_CONCLUSIONS:
            continue
        dur = _duration(s.get("startedAt"), s.get("completedAt"))
        summaries.append(f"{s['name']} ({dur})" if dur else s["name"])
    return summaries


def failed_steps_summary(details_url: str | None) -> str | None:
    """One-line summary of what failed — job/step metadata only, no log
    fetch, so it's cheap enough to show by default (not gated behind
    --why). Accepts a job-level or bare run-level Actions URL, same as
    failure_log_tail.

    Normally this names the failed step(s), each with how long it ran
    before failing. If no individual step is marked failed (the job died
    some other way - cancelled, infra failure), falls back to how far the
    job got: how many steps completed and how long it ran - still useful
    context, and still free of any extra fetch beyond the job/step list
    already retrieved.
    """
    if not details_url:
        return None

    job_match = _RUN_JOB_URL_RE.search(details_url)
    if job_match:
        run_id, job_id = job_match.groups()
        job = next(
            (j for j in _run_jobs(run_id) if str(j.get("databaseId")) == job_id), None
        )
    else:
        run_match = _RUN_URL_RE.search(details_url)
        if not run_match:
            return None
        job = _find_failed_job(run_match.group(1))

    if job is None:
        return None

    steps = _failed_step_summaries(job)
    if steps:
        return f"{job['name']}: {', '.join(steps)}"

    all_steps = job.get("steps") or []
    completed = sum(1 for s in all_steps if s.get("status") == "completed")
    dur = _duration(job.get("startedAt"), job.get("completedAt"))
    if not all_steps:
        return job["name"]
    progress = f"stopped after {completed}/{len(all_steps)} steps"
    return f"{job['name']} ({progress}, {dur})" if dur else f"{job['name']} ({progress})"


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
