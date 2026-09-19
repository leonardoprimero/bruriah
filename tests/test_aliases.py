from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bruriah.aliases import (
    AliasError,
    get_git_alias,
    install_aliases,
    set_git_alias,
    uninstall_aliases,
    unset_git_alias,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    return repo_dir


def test_get_set_unset_git_alias_local(repo: Path) -> None:
    # 1. Fresh alias
    assert get_git_alias("testalias", scope="local", repo=repo) is None

    # 2. Set alias
    status = set_git_alias("testalias", "status -s", scope="local", repo=repo)
    assert status == "created"
    assert get_git_alias("testalias", scope="local", repo=repo) == "status -s"

    # 3. Idempotent set
    status = set_git_alias("testalias", "status -s", scope="local", repo=repo)
    assert status == "unchanged"

    # 4. Update alias
    status = set_git_alias("testalias", "status --short", scope="local", repo=repo)
    assert status == "updated"
    assert get_git_alias("testalias", scope="local", repo=repo) == "status --short"

    # 5. Unset alias
    status = unset_git_alias("testalias", scope="local", repo=repo)
    assert status == "removed"
    assert get_git_alias("testalias", scope="local", repo=repo) is None

    # 6. Unset non-existent alias
    status = unset_git_alias("testalias", scope="local", repo=repo)
    assert status == "not_found"


def test_install_and_uninstall_aliases_local(repo: Path) -> None:
    installed = install_aliases(scope="local", repo=repo)
    assert len(installed) == 2
    assert all(status == "created" for _, _, status in installed)

    assert get_git_alias("why", scope="local", repo=repo) == "!bruriah why"
    assert get_git_alias("drift", scope="local", repo=repo) == "!bruriah drift"

    # Uninstall
    uninstalled = uninstall_aliases(scope="local", repo=repo)
    assert len(uninstalled) == 2
    assert all(status == "removed" for _, status in uninstalled)

    assert get_git_alias("why", scope="local", repo=repo) is None
    assert get_git_alias("drift", scope="local", repo=repo) is None


def test_local_scope_refuses_non_git_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(AliasError) as exc:
        install_aliases(scope="local", repo=plain)
    assert exc.value.code == "not_a_git_repository"

    with pytest.raises(AliasError) as exc:
        install_aliases(scope="local", repo=None)
    assert exc.value.code == "local_scope_requires_repo"
