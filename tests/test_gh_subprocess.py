"""gh.py functions that shell out to git/gh, exercised via the fake_cli
subprocess.run stand-in (see conftest.py) — real command lines in, real
parsing logic tested, no actual git/gh binary involved."""

from __future__ import annotations

import json
import subprocess

import pytest

from wazup import gh


def test_run_raises_wazup_error_when_binary_missing(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(gh.WazupError, match="not found on PATH"):
        gh.current_branch()


def test_run_raises_wazup_error_on_nonzero_exit(fake_cli):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], returncode=128, stderr="fatal: not a git repository")
    with pytest.raises(gh.WazupError, match="not a git repository"):
        gh.current_branch()


def test_current_branch(fake_cli):
    fake_cli.set(["git", "rev-parse", "--abbrev-ref", "HEAD"], stdout="main\n")
    assert gh.current_branch() == "main"


def test_current_commit_sha(fake_cli):
    fake_cli.set(["git", "rev-parse", "HEAD"], stdout="abc123\n")
    assert gh.current_commit_sha() == "abc123"


def test_is_linked_worktree_true(fake_cli):
    fake_cli.set(
        ["git", "rev-parse", "--git-dir", "--git-common-dir"],
        stdout="/repo/.git/worktrees/foo\n/repo/.git\n",
    )
    assert gh.is_linked_worktree() is True


def test_is_linked_worktree_false_for_main_checkout(fake_cli):
    fake_cli.set(
        ["git", "rev-parse", "--git-dir", "--git-common-dir"],
        stdout="/repo/.git\n/repo/.git\n",
    )
    assert gh.is_linked_worktree() is False


def test_is_linked_worktree_false_when_not_a_repo(fake_cli):
    fake_cli.fail(["git", "rev-parse", "--git-dir", "--git-common-dir"])
    assert gh.is_linked_worktree() is False


def test_commits_ahead_of_missing_ref_returns_none(fake_cli):
    fake_cli.fail(["git", "rev-list", "--count", "origin/main..HEAD"])
    assert gh.commits_ahead_of("origin/main") is None


def test_commits_ahead_of(fake_cli):
    fake_cli.set(["git", "rev-list", "--count", "origin/main..HEAD"], stdout="3\n")
    assert gh.commits_ahead_of("origin/main") == 3


# -- local_status -----------------------------------------------------------

_STATUS_CMD = ["git", "status", "--porcelain=v2", "--branch"]


def test_local_status_clean(fake_cli):
    fake_cli.set(_STATUS_CMD, stdout="# branch.oid abc\n# branch.head main\n# branch.ab +0 -0\n")
    status = gh.local_status()
    assert status.ahead == 0
    assert status.behind == 0
    assert status.changed_files == []
    assert status.untracked_count == 0


def test_local_status_ahead_behind(fake_cli):
    fake_cli.set(_STATUS_CMD, stdout="# branch.ab +2 -1\n")
    status = gh.local_status()
    assert status.ahead == 2
    assert status.behind == 1


def test_local_status_no_upstream(fake_cli):
    fake_cli.fail(_STATUS_CMD)
    status = gh.local_status()
    assert status.ahead is None
    assert status.behind is None


def test_local_status_ordinary_changed_file(fake_cli):
    line = "1 A. N... 000000 100644 100644 0000000000000000000000000000000000000000 e8814d5 c.txt"
    fake_cli.set(_STATUS_CMD, stdout=f"# branch.ab +0 -0\n{line}\n")
    status = gh.local_status()
    assert status.changed_files == [gh.ChangedFile(status="A", path="c.txt")]


def test_local_status_renamed_file_path_excludes_score(fake_cli):
    # regression test: the R<score> field sits between the hash and the
    # path\torigPath pair — a fixed split width used to leak "R100 " into
    # the parsed path.
    line = (
        "2 RM N... 100644 100644 100644 "
        "ce013625030ba8dba906f756967f9e9ca394464a ce013625030ba8dba906f756967f9e9ca394464a "
        "R100 b.txt\ta.txt"
    )
    fake_cli.set(_STATUS_CMD, stdout=f"# branch.ab +0 -0\n{line}\n")
    status = gh.local_status()
    assert status.changed_files == [gh.ChangedFile(status="R", path="b.txt")]


def test_local_status_unmerged_file(fake_cli):
    line = (
        "u UU N... 100644 100644 100644 100644 "
        "h1 h2 h3 conflict.py"
    )
    fake_cli.set(_STATUS_CMD, stdout=f"# branch.ab +0 -0\n{line}\n")
    status = gh.local_status()
    assert status.changed_files == [gh.ChangedFile(status="U", path="conflict.py")]


def test_local_status_untracked_files(fake_cli):
    fake_cli.set(_STATUS_CMD, stdout="# branch.ab +0 -0\n? a.txt\n? b.txt\n")
    status = gh.local_status()
    assert status.untracked_count == 2
    assert status.changed_files == []


# -- branches / worktrees ----------------------------------------------------


def test_local_branches_by_recency_excludes_given_names(fake_cli):
    fake_cli.set(
        ["git", "for-each-ref", "refs/heads/", "--sort=-committerdate",
         "--format=%(refname:short)\t%(committerdate:relative)"],
        stdout="main\t2 days ago\nfeature-x\t1 hour ago\n",
    )
    branches = gh.local_branches_by_recency(exclude={"main"})
    assert [b.name for b in branches] == ["feature-x"]


def test_worktree_branches(fake_cli):
    fake_cli.set(
        ["git", "worktree", "list", "--porcelain"],
        stdout=(
            "worktree /repo\nbranch refs/heads/main\n\n"
            "worktree /repo-wt\nbranch refs/heads/feature-x\n\n"
        ),
    )
    assert gh.worktree_branches() == {"main": "/repo", "feature-x": "/repo-wt"}


def test_worktree_info_reports_unmerged_and_dirty(fake_cli):
    fake_cli.set(
        ["git", "worktree", "list", "--porcelain"],
        stdout="worktree /repo\nbranch refs/heads/main\n\nworktree /repo-wt\nbranch refs/heads/feature-x\n\n",
    )
    fake_cli.set(["git", "log", "-1", "--format=%ct\t%cr", "feature-x"], stdout="1700000000\t3 days ago")
    fake_cli.set(["git", "rev-list", "--count", "origin/main..feature-x"], stdout="2")
    fake_cli.set(["git", "-C", "/repo-wt", "status", "--porcelain"], stdout="M dirty.py")

    entries = gh.worktree_info(exclude_branch="main", default_ref="origin/main")
    assert len(entries) == 1
    assert entries[0].branch == "feature-x"
    assert entries[0].unmerged == 2
    assert entries[0].dirty is True


def test_worktree_info_excludes_given_branch(fake_cli):
    fake_cli.set(
        ["git", "worktree", "list", "--porcelain"],
        stdout="worktree /repo\nbranch refs/heads/main\n\n",
    )
    assert gh.worktree_info(exclude_branch="main") == []


# -- gh API wrappers ----------------------------------------------------------


def test_repo_info(fake_cli):
    fake_cli.set(
        ["gh", "repo", "view", "--json", "nameWithOwner,url,defaultBranchRef"],
        stdout=json.dumps({
            "nameWithOwner": "vivainio/wazup",
            "url": "https://github.com/vivainio/wazup",
            "defaultBranchRef": {"name": "main"},
        }),
    )
    info = gh.repo_info()
    assert info.name_with_owner == "vivainio/wazup"
    assert info.default_branch == "main"


def test_current_repo_none_outside_a_repo(fake_cli):
    fake_cli.fail(["gh", "repo", "view", "--json", "nameWithOwner,url,defaultBranchRef"])
    assert gh.current_repo() is None


def test_open_prs_by_branch(fake_cli):
    fake_cli.set(
        ["gh", "pr", "list", "--json", "number,url,state,isDraft,headRefName", "--limit", "100"],
        stdout=json.dumps([
            {"number": 5, "url": "https://x/5", "state": "OPEN", "isDraft": False, "headRefName": "feature-x"},
        ]),
    )
    prs = gh.open_prs_by_branch()
    assert prs["feature-x"].number == 5


def test_pull_request_for_branch_none(fake_cli):
    fake_cli.fail([
        "gh", "pr", "view", "feature-x", "--json",
        "number,title,url,state,isDraft,reviewDecision,statusCheckRollup",
    ])
    assert gh.pull_request_for_branch("feature-x") is None


def test_pull_request_for_branch_found_with_checks(fake_cli):
    fake_cli.set(
        ["gh", "pr", "view", "feature-x", "--json",
         "number,title,url,state,isDraft,reviewDecision,statusCheckRollup"],
        stdout=json.dumps({
            "number": 5,
            "title": "Add thing",
            "url": "https://x/5",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS", "detailsUrl": "https://x/run/1"},
            ],
        }),
    )
    pr = gh.pull_request_for_branch("feature-x")
    assert pr is not None
    assert pr.number == 5
    assert pr.checks[0].name == "build"
    assert pr.checks[0].conclusion == "SUCCESS"


def test_runs_for_commit_filters_by_head_sha(fake_cli):
    fake_cli.set(
        ["gh", "run", "list", "--commit", "abc123", "--limit", "100", "--json",
         "name,status,conclusion,url,headSha"],
        stdout=json.dumps([
            {"name": "CI", "status": "completed", "conclusion": "success", "url": "https://x/1", "headSha": "abc123"},
            {"name": "Stale", "status": "completed", "conclusion": "success", "url": "https://x/2", "headSha": "other"},
        ]),
    )
    runs = gh.runs_for_commit("abc123")
    assert [r.name for r in runs] == ["CI"]


def test_recent_releases(fake_cli):
    fake_cli.set(
        ["gh", "release", "list", "--limit", "5", "--json",
         "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout=json.dumps([
            {"tagName": "v1.0.0", "name": "v1.0.0", "publishedAt": "2026-01-01T00:00:00Z",
             "isDraft": False, "isPrerelease": False},
        ]),
    )
    releases = gh.recent_releases()
    assert releases[0].tag_name == "v1.0.0"


def test_recent_other_runs_excludes_old_completed_runs(fake_cli):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    old = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    fake_cli.set(
        ["gh", "run", "list", "--limit", "20", "--json", "name,status,conclusion,url,createdAt"],
        stdout=json.dumps([
            {"name": "recent-pass", "status": "completed", "conclusion": "success", "url": "https://x/1", "createdAt": recent},
            {"name": "old-pass", "status": "completed", "conclusion": "success", "url": "https://x/2", "createdAt": old},
            {"name": "still-running", "status": "in_progress", "conclusion": None, "url": "https://x/3", "createdAt": old},
        ]),
    )
    runs = gh.recent_other_runs()
    assert {r.name for r in runs} == {"recent-pass", "still-running"}


# -- gh account switching ----------------------------------------------------

_AUTH_STATUS_CMD = ["gh", "auth", "status", "--json", "hosts"]


def test_ensure_gh_account_for_repo_no_remote(fake_cli):
    fake_cli.fail(["git", "remote", "get-url", "origin"])
    assert gh.ensure_gh_account_for_repo() is None


def test_ensure_gh_account_for_repo_already_active_is_noop(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:vivainio/wazup.git")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    assert gh.ensure_gh_account_for_repo() is None


def test_ensure_gh_account_for_repo_switches_when_mismatched(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:vivainio/wazup.git")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [
            {"login": "work-account", "active": True},
            {"login": "vivainio", "active": False},
        ]}
    }))
    fake_cli.set(["gh", "auth", "switch", "--hostname", "github.com", "--user", "vivainio"], stdout="")
    notice = gh.ensure_gh_account_for_repo()
    assert notice == "switched active gh account to vivainio (owner of vivainio's repos)"


def test_ensure_gh_account_for_repo_no_matching_login(fake_cli):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout="git@github.com:someorg/repo.git")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    assert gh.ensure_gh_account_for_repo() is None


def test_active_account_is_personal(fake_cli):
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    assert gh.active_account_is_personal() is True


def test_active_account_is_personal_false_for_sso_identity(fake_cli):
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "ville_Corp", "active": True}]}
    }))
    assert gh.active_account_is_personal() is False


def test_active_account_is_personal_none_when_not_authed(fake_cli):
    fake_cli.fail(_AUTH_STATUS_CMD)
    assert gh.active_account_is_personal() is None


# -- remote-owner parsing (via ensure_gh_account_for_repo) -------------------


@pytest.mark.parametrize(
    "url,expected_owner",
    [
        ("git@github.com:vivainio/wazup.git", "vivainio"),
        ("https://github.com/vivainio/wazup.git", "vivainio"),
        ("https://github.com/vivainio/wazup", "vivainio"),
        ("git@github-personal:vivainio/wazup.git", "vivainio"),
    ],
)
def test_remote_owner_parsed_from_various_url_forms(fake_cli, url, expected_owner):
    fake_cli.set(["git", "remote", "get-url", "origin"], stdout=url)
    assert gh._remote_owner() == expected_owner


# -- failure log / step summaries -------------------------------------------


def test_failed_steps_summary_names_failed_step(fake_cli):
    fake_cli.set(
        ["gh", "run", "view", "999", "--json", "jobs"],
        stdout=json.dumps({"jobs": [
            {
                "databaseId": 111,
                "name": "build",
                "conclusion": "failure",
                "startedAt": "2026-01-01T00:00:00Z",
                "completedAt": "2026-01-01T00:00:10Z",
                "steps": [
                    {"name": "Run tests", "conclusion": "failure",
                     "startedAt": "2026-01-01T00:00:01Z", "completedAt": "2026-01-01T00:00:09Z"},
                ],
            },
        ]}),
    )
    summary = gh.failed_steps_summary("https://github.com/o/r/actions/runs/999/job/111")
    assert summary == "build: Run tests (8s)"


def test_failure_log_tail_ends_at_error_marker(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    log_lines = [
        "2026-01-01T00:00:01Z\tsetup",
        "2026-01-01T00:00:02Z\trunning tests",
        "2026-01-01T00:00:03Z\t##[error]assertion failed",
        "2026-01-01T00:00:04Z\tcleanup (after error, should be excluded)",
    ]
    fake_cli.set(
        ["gh", "run", "view", "999", "--job", "111", "--log-failed"],
        stdout="\n".join(log_lines),
    )
    tail = gh.failure_log_tail("https://github.com/o/r/actions/runs/999/job/111", lines=25)
    assert tail is not None
    assert "##[error]assertion failed" in tail
    assert "cleanup" not in tail


def test_failure_log_tail_is_cached_to_disk(fake_cli, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    fake_cli.set(
        ["gh", "run", "view", "999", "--job", "111", "--log-failed"],
        stdout="2026-01-01T00:00:01Z\t##[error]boom",
    )
    first = gh.failure_log_tail("https://github.com/o/r/actions/runs/999/job/111")
    # second call must not re-invoke `gh run view` — only one response was
    # registered above, so a second subprocess call would raise.
    second = gh.failure_log_tail("https://github.com/o/r/actions/runs/999/job/111")
    assert first == second


def test_failure_log_tail_resolves_failed_job_from_run_level_url(fake_cli):
    fake_cli.set(
        ["gh", "run", "view", "999", "--json", "jobs"],
        stdout=json.dumps({"jobs": [
            {"databaseId": 111, "name": "build", "conclusion": "success"},
            {"databaseId": 112, "name": "test", "conclusion": "failure"},
        ]}),
    )
    fake_cli.set(
        ["gh", "run", "view", "999", "--job", "112", "--log-failed"],
        stdout="2026-01-01T00:00:01Z\t##[error]failed here",
    )
    tail = gh.failure_log_tail("https://github.com/o/r/actions/runs/999")
    assert tail is not None
    assert "failed here" in tail


def test_failure_log_tail_none_without_url():
    assert gh.failure_log_tail(None) is None
