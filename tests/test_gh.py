"""Pure-logic tests: no subprocess involved."""

from __future__ import annotations

from wazup import gh


def test_is_orphaned_worktree_branch_name():
    assert gh.is_orphaned_worktree_branch_name("worktree/lucky-cloud-724c")
    assert not gh.is_orphaned_worktree_branch_name("main")
    assert not gh.is_orphaned_worktree_branch_name("feature/worktree-thing")


def test_login_matches_owner_exact():
    assert gh._login_matches_owner("vivainio", "vivainio")
    assert gh._login_matches_owner("Vivainio", "vivainio")
    assert not gh._login_matches_owner("someoneelse", "vivainio")


def test_login_matches_owner_sso_suffix():
    assert gh._login_matches_owner("ville_Basware", "Basware")
    assert not gh._login_matches_owner("ville_Basware", "OtherOrg")


def test_uv_lock_private_mirrors_flags_non_public_hosts(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        '[[package]]\nname = "foo"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "bar"\n'
        'source = { registry = "https://pkgs.internal.example.com/simple" }\n\n'
        '[[package.wheels]]\n'
        'url = "https://files.pythonhosted.org/packages/bar.whl"\n'
    )
    assert gh.uv_lock_private_mirrors(str(lock)) == ["pkgs.internal.example.com"]


def test_uv_lock_private_mirrors_missing_file(tmp_path):
    assert gh.uv_lock_private_mirrors(str(tmp_path / "nope.lock")) == []


def test_uv_lock_private_mirrors_all_public(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text('source = { registry = "https://pypi.org/simple" }\n')
    assert gh.uv_lock_private_mirrors(str(lock)) == []
