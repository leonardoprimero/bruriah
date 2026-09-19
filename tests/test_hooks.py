from __future__ import annotations

import os
from pathlib import Path

import pytest

from bruriah.hooks import (
    HOOK_END_MARKER,
    HOOK_START_MARKER,
    HookError,
    install_hook,
    resolve_git_hooks_dir,
    uninstall_hook,
)


def test_resolve_git_hooks_dir_standard_and_worktree(tmp_path: Path) -> None:
    # Standard repository
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert resolve_git_hooks_dir(repo) == repo / ".git" / "hooks"

    # Git worktree / submodule with .git file
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    main_git = tmp_path / "main-repo" / ".git" / "worktrees" / "wt1"
    main_git.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {main_git}\n", encoding="utf-8")

    assert resolve_git_hooks_dir(worktree) == main_git / "hooks"

    # Non-git directory raises HookError
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(HookError) as exc:
        resolve_git_hooks_dir(plain)
    assert exc.value.code == "not_a_git_repository"


def test_install_hook_creates_new_hook_with_executable_permissions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    target, status = install_hook(repo)
    assert status == "created"
    assert target.is_file()

    content = target.read_text(encoding="utf-8")
    assert content.startswith("#!/bin/sh")
    assert HOOK_START_MARKER in content
    assert HOOK_END_MARKER in content
    assert "bruriah drift --staged --strict" in content

    if os.name == "posix":
        mode = target.stat().st_mode
        assert mode & 0o111 != 0  # Executable bit is set


def test_install_hook_appends_to_existing_hook_non_destructively(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    pre_commit = hooks_dir / "pre-commit"
    pre_commit.write_text("#!/bin/sh\n\necho 'running linting'\npytest\n", encoding="utf-8")

    target, status = install_hook(repo)
    assert status == "updated"
    content = target.read_text(encoding="utf-8")

    # Original contents preserved
    assert "echo 'running linting'" in content
    assert "pytest" in content
    # Bruriah block appended
    assert HOOK_START_MARKER in content
    assert HOOK_END_MARKER in content


def test_install_hook_idempotent_and_force_update(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    _, status1 = install_hook(repo)
    assert status1 == "created"

    # Second run without force is unchanged
    _, status2 = install_hook(repo, force=False)
    assert status2 == "unchanged"

    # Run with force updates
    _, status3 = install_hook(repo, force=True)
    assert status3 == "updated"


def test_uninstall_hook_removes_bruriah_and_preserves_other_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    pre_commit = hooks_dir / "pre-commit"

    # Hook with other commands
    install_hook(repo)
    pre_commit.write_text(
        "#!/bin/sh\n\necho 'custom lint'\n\n" + pre_commit.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    target, status = uninstall_hook(repo)
    assert status == "removed"
    assert target.is_file()
    remaining = target.read_text(encoding="utf-8")
    assert "echo 'custom lint'" in remaining
    assert HOOK_START_MARKER not in remaining


def test_uninstall_hook_deletes_file_if_only_bruriah_was_present(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    target, _ = install_hook(repo)
    assert target.is_file()

    _, status = uninstall_hook(repo)
    assert status == "removed"
    assert not target.exists()  # Cleaned up completely

    # Second uninstall reports not_found
    _, status_again = uninstall_hook(repo)
    assert status_again == "not_found"
