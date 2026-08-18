"""End-to-end CLI tests: argparse -> cmd_* -> gh.* -> subprocess, with only
the subprocess boundary faked (see conftest.fake_cli). Exercises the same
call chains real `git`/`gh` invocations would produce, so these catch
integration bugs (e.g. a parsing regression) that isolated gh.py unit tests
can miss.
"""

from __future__ import annotations

import json

import pytest

from wazup import cli

_REPO_VIEW_CMD = ["gh", "repo", "view", "--json", "nameWithOwner,url,defaultBranchRef"]
_BRANCH_CMD = ["git", "rev-parse", "--abbrev-ref", "HEAD"]
_AUTH_STATUS_CMD = ["gh", "auth", "status", "--json", "hosts"]
_STATUS_CMD = ["git", "status", "--porcelain=v2", "--branch"]


def _run_cli(argv: list[str]):
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def _register_repo(fake_cli, name_with_owner="vivainio/wazup", default_branch="main"):
    fake_cli.set(_REPO_VIEW_CMD, stdout=json.dumps({
        "nameWithOwner": name_with_owner,
        "url": f"https://github.com/{name_with_owner}",
        "defaultBranchRef": {"name": default_branch},
    }))


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    # uv_lock_private_mirrors() reads "uv.lock" relative to cwd — run from an
    # empty directory so status tests don't pick up this repo's real lockfile.
    monkeypatch.chdir(tmp_path)


def test_status_clean_repo_no_pr_no_ci(fake_cli, capsys):
    _register_repo(fake_cli)
    fake_cli.set(_BRANCH_CMD, stdout="main")
    fake_cli.set(["git", "fetch"], stdout="")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    fake_cli.set(_STATUS_CMD, stdout="# branch.ab +0 -0\n")
    fake_cli.set(
        ["gh", "pr", "view", "main", "--json",
         "number,title,url,state,isDraft,reviewDecision,statusCheckRollup"],
        returncode=1, stderr="no pull requests found",
    )
    fake_cli.set(
        ["gh", "run", "list", "--branch", "main", "--limit", "10", "--json",
         "name,status,conclusion,url"],
        stdout="[]",
    )
    fake_cli.set(["git", "worktree", "list", "--porcelain"], stdout="")
    fake_cli.set(
        ["git", "for-each-ref", "refs/heads/", "--sort=-committerdate",
         "--format=%(refname:short)\t%(committerdate:relative)"],
        stdout="",
    )
    fake_cli.set(
        ["gh", "pr", "list", "--json", "number,url,state,isDraft,headRefName", "--limit", "100"],
        stdout="[]",
    )

    exit_code = _run_cli([])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "repo   vivainio/wazup" in out
    assert "branch main (default)" in out
    assert "local  pushed, clean" in out
    assert "pr     none" in out
    assert "ci     none" in out


def test_status_dirty_repo_with_open_pr_and_failing_check(fake_cli, capsys):
    _register_repo(fake_cli, default_branch="main")
    fake_cli.set(_BRANCH_CMD, stdout="feature-x")
    fake_cli.set(["git", "fetch"], stdout="")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    fake_cli.set(
        _STATUS_CMD,
        stdout="# branch.ab +1 -0\n1 M. N... 100644 100644 100644 h h path.py\n",
    )
    fake_cli.set(
        ["gh", "pr", "view", "feature-x", "--json",
         "number,title,url,state,isDraft,reviewDecision,statusCheckRollup"],
        stdout=json.dumps({
            "number": 7,
            "title": "Add feature",
            "url": "https://github.com/vivainio/wazup/pull/7",
            "state": "OPEN",
            "isDraft": False,
            "reviewDecision": "",
            "statusCheckRollup": [
                {"name": "build", "status": "COMPLETED", "conclusion": "FAILURE",
                 "detailsUrl": "https://github.com/vivainio/wazup/actions/runs/999/job/111"},
            ],
        }),
    )
    fake_cli.set(
        ["gh", "run", "view", "999", "--json", "jobs"],
        stdout=json.dumps({"jobs": [
            {"databaseId": 111, "name": "build", "conclusion": "failure",
             "steps": [{"name": "pytest", "conclusion": "failure"}]},
        ]}),
    )
    fake_cli.set(
        ["gh", "run", "list", "--limit", "20", "--json", "name,status,conclusion,url,createdAt"],
        stdout="[]",
    )
    fake_cli.set(
        ["git", "rev-parse", "--git-dir", "--git-common-dir"],
        stdout="/repo/.git\n/repo/.git\n",
    )

    exit_code = _run_cli([])

    assert exit_code == 0  # cmd_status itself always returns 0; failure is signalled in the output
    out = capsys.readouterr().out
    assert "local  1 unpushed, dirty" in out
    assert "M path.py" in out
    assert "pr     #7 Add feature  [open]" in out
    assert "build" in out
    assert "run `wazup --why` to see the failing log" in out
    assert "Everything is clean" not in out


def test_ci_command_no_pr_falls_back_to_branch_runs(fake_cli, capsys):
    fake_cli.set(_BRANCH_CMD, stdout="feature-x")
    fake_cli.set(
        ["gh", "pr", "view", "feature-x", "--json",
         "number,title,url,state,isDraft,reviewDecision,statusCheckRollup"],
        returncode=1, stderr="no pull requests found",
    )
    fake_cli.set(
        ["gh", "run", "list", "--branch", "feature-x", "--limit", "10", "--json",
         "name,status,conclusion,url"],
        stdout=json.dumps([
            {"name": "CI", "status": "COMPLETED", "conclusion": "SUCCESS",
             "url": "https://github.com/vivainio/wazup/actions/runs/1"},
        ]),
    )
    fake_cli.set(
        ["gh", "run", "list", "--limit", "20", "--json", "name,status,conclusion,url,createdAt"],
        stdout="[]",
    )

    exit_code = _run_cli(["ci"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "branch feature-x  (no open PR)" in out
    assert "CI" in out


def test_release_wrong_branch_errors(fake_cli, capsys):
    _register_repo(fake_cli, default_branch="main")
    fake_cli.set(_BRANCH_CMD, stdout="feature-x")

    exit_code = _run_cli(["release"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "current branch is 'feature-x'" in err
    assert "release target is 'main'" in err


def test_release_blocks_on_dirty_worktree(fake_cli, capsys):
    _register_repo(fake_cli, default_branch="main")
    fake_cli.set(_BRANCH_CMD, stdout="main")
    fake_cli.set(
        ["gh", "release", "list", "--limit", "5", "--json",
         "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout="[]",
    )
    fake_cli.set(["git", "fetch", "origin", "--prune", "--tags"], stdout="")
    fake_cli.set(
        _STATUS_CMD,
        stdout="# branch.ab +0 -0\n1 M. N... 100644 100644 100644 h h dirty.py\n",
    )

    exit_code = _run_cli(["release"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "dirty worktree" in err


def test_release_preflight_passes_when_ci_green(fake_cli, capsys):
    _register_repo(fake_cli, default_branch="main")
    fake_cli.set(_BRANCH_CMD, stdout="main")
    fake_cli.set(
        ["gh", "release", "list", "--limit", "5", "--json",
         "tagName,name,publishedAt,isDraft,isPrerelease"],
        stdout="[]",
    )
    fake_cli.set(["git", "fetch", "origin", "--prune", "--tags"], stdout="")
    fake_cli.set(_STATUS_CMD, stdout="# branch.ab +0 -0\n")
    fake_cli.set(["git", "rev-parse", "HEAD"], stdout="abc123")
    fake_cli.set(
        ["gh", "run", "list", "--commit", "abc123", "--limit", "100", "--json",
         "name,status,conclusion,url,headSha"],
        stdout=json.dumps([
            {"name": "CI", "status": "completed", "conclusion": "success",
             "url": "https://x/1", "headSha": "abc123"},
        ]),
    )
    fake_cli.set(
        ["gh", "run", "list", "--limit", "20", "--json", "name,status,conclusion,url,createdAt"],
        stdout="[]",
    )

    exit_code = _run_cli(["release"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "RELEASE PREFLIGHT PASS" in out


def test_rel_is_an_alias_for_release(fake_cli, capsys):
    _register_repo(fake_cli, default_branch="main")
    fake_cli.set(_BRANCH_CMD, stdout="feature-x")

    exit_code = _run_cli(["rel"])

    assert exit_code == 1
    assert "release target is 'main'" in capsys.readouterr().err


def test_my_command_lists_open_prs_in_current_repo(fake_cli, capsys):
    _register_repo(fake_cli)
    fake_cli.set(
        ["gh", "pr", "list", "--author", "@me", "--json",
         "number,title,url,state,isDraft,updatedAt"],
        stdout=json.dumps([
            {"number": 3, "title": "Fix bug", "url": "https://x/3", "state": "OPEN",
             "isDraft": False, "updatedAt": "2026-08-01T00:00:00Z"},
        ]),
    )

    exit_code = _run_cli(["my"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "your open PRs in vivainio/wazup" in out
    assert "#3 Fix bug" in out


def test_review_command_lists_prs_awaiting_review(fake_cli, capsys):
    fake_cli.set(
        _review_search_cmd(),
        stdout=json.dumps([
            {"number": 9, "title": "Needs eyes", "url": "https://x/9", "state": "OPEN",
             "isDraft": False, "updatedAt": "2026-08-10T00:00:00Z",
             "repository": {"nameWithOwner": "vivainio/other"}},
        ]),
    )

    exit_code = _run_cli(["review"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "PRs awaiting your review" in out
    assert "vivainio/other  #9 Needs eyes" in out


def test_status_hints_fixup_uv_lock_for_a_leaked_mirror(fake_cli, capsys, tmp_path):
    (tmp_path / "uv.lock").write_text(
        'source = { registry = "https://pypi-mirror.example.com/simple" }\n'
    )
    _register_repo(fake_cli)
    fake_cli.set(_BRANCH_CMD, stdout="main")
    fake_cli.set(["git", "fetch"], stdout="")
    fake_cli.set(_AUTH_STATUS_CMD, stdout=json.dumps({
        "hosts": {"github.com": [{"login": "vivainio", "active": True}]}
    }))
    fake_cli.set(_STATUS_CMD, stdout="# branch.ab +0 -0\n")
    fake_cli.set(
        ["gh", "pr", "view", "main", "--json",
         "number,title,url,state,isDraft,reviewDecision,statusCheckRollup"],
        returncode=1, stderr="no pull requests found",
    )
    fake_cli.set(
        ["gh", "run", "list", "--branch", "main", "--limit", "10", "--json",
         "name,status,conclusion,url"],
        stdout="[]",
    )
    fake_cli.set(["git", "worktree", "list", "--porcelain"], stdout="")
    fake_cli.set(
        ["git", "for-each-ref", "refs/heads/", "--sort=-committerdate",
         "--format=%(refname:short)\t%(committerdate:relative)"],
        stdout="",
    )
    fake_cli.set(
        ["gh", "pr", "list", "--json", "number,url,state,isDraft,headRefName", "--limit", "100"],
        stdout="[]",
    )

    exit_code = _run_cli([])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "uv.lock references a private index/mirror: pypi-mirror.example.com" in out
    assert "fix with `wazup fixup uv-lock`" in out


def test_fixup_uv_lock_rewrites_the_lockfile(capsys, tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'source = { registry = "https://pypi-mirror.example.com/simple" }\n'
        'sdist = { url = "https://pypi-mirror.example.com/packages/foo.tar.gz" }\n'
    )

    exit_code = _run_cli(["fixup", "uv-lock"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "rewrote uv.lock: pypi-mirror.example.com -> pypi.org / files.pythonhosted.org" in out
    text = lock.read_text()
    assert "pypi-mirror.example.com" not in text
    assert "https://pypi.org/simple" in text


def test_fixup_uv_lock_noop_when_already_public(capsys, tmp_path):
    (tmp_path / "uv.lock").write_text('source = { registry = "https://pypi.org/simple" }\n')

    exit_code = _run_cli(["fixup", "uv-lock"])

    assert exit_code == 0
    assert "nothing to fix" in capsys.readouterr().out


def _review_search_cmd() -> list[str]:
    from datetime import date, timedelta

    since = (date.today() - timedelta(days=7)).isoformat()
    return [
        "gh", "search", "prs", "--review-requested", "@me", "--state", "open",
        "--updated", f">={since}", "--sort", "updated", "--json",
        "number,title,url,state,isDraft,updatedAt,repository",
    ]
