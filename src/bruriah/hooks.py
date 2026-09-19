from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "HOOK_END_MARKER",
    "HOOK_PAYLOAD",
    "HOOK_START_MARKER",
    "HookError",
    "install_hook",
    "resolve_git_hooks_dir",
    "uninstall_hook",
]

HOOK_START_MARKER = "# >>> bruriah drift hook >>>"
HOOK_END_MARKER = "# <<< bruriah drift hook <<<"

HOOK_PAYLOAD = f"""{HOOK_START_MARKER}
# Managed by Bruriah (https://github.com/leonardoprimero/bruriah)
if command -v bruriah >/dev/null 2>&1; then
    bruriah drift --staged --strict
elif command -v uvx >/dev/null 2>&1; then
    uvx bruriah drift --staged --strict
fi
{HOOK_END_MARKER}"""


class HookError(ValueError):
    """Typed failure for git hook operations."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def resolve_git_hooks_dir(repo_root: Path) -> Path:
    """Locate the git hooks directory, supporting standard repos, submodules, and worktrees."""
    git_entry = repo_root / ".git"
    if not git_entry.exists():
        raise HookError("not_a_git_repository")

    if git_entry.is_dir():
        return git_entry / "hooks"

    # Git worktree or submodule: .git is a file containing "gitdir: <path>"
    try:
        content = git_entry.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            gitdir_path = Path(content[len("gitdir:") :].strip())
            if not gitdir_path.is_absolute():
                gitdir_path = (repo_root / gitdir_path).resolve()
            return gitdir_path / "hooks"
    except OSError as error:
        raise HookError(f"gitdir_read_failed:{type(error).__name__}") from error

    raise HookError("not_a_git_repository")


def install_hook(repo_root: Path, *, force: bool = False) -> tuple[Path, str]:
    """Install bruriah drift as a pre-commit hook in the target git repository.

    Returns (pre_commit_path, status) where status is 'created', 'updated', or 'unchanged'.
    Non-destructively preserves existing pre-commit hook scripts.
    """
    hooks_dir = resolve_git_hooks_dir(repo_root)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "pre-commit"

    if target.is_file():
        try:
            content = target.read_text(encoding="utf-8")
        except OSError as error:
            raise HookError(f"read_failed:{type(error).__name__}") from error

        if HOOK_START_MARKER in content:
            if not force:
                return target, "unchanged"
            # Replace existing bruriah block
            start_idx = content.find(HOOK_START_MARKER)
            end_idx = content.find(HOOK_END_MARKER)
            if end_idx != -1:
                end_idx += len(HOOK_END_MARKER)
                new_content = content[:start_idx] + HOOK_PAYLOAD + content[end_idx:]
            else:
                new_content = content[:start_idx] + HOOK_PAYLOAD
            target.write_text(new_content.strip() + "\n", encoding="utf-8")
            status = "updated"
        else:
            # Append non-destructively to existing hook
            new_content = content.rstrip() + "\n\n" + HOOK_PAYLOAD + "\n"
            target.write_text(new_content, encoding="utf-8")
            status = "updated"
    else:
        # Create new standalone hook script
        script = f"#!/bin/sh\n\n{HOOK_PAYLOAD}\n"
        target.write_text(script, encoding="utf-8")
        status = "created"

    if os.name == "posix":
        try:
            target.chmod(0o755)
        except OSError as error:
            raise HookError(f"chmod_failed:{type(error).__name__}") from error

    return target, status


def uninstall_hook(repo_root: Path) -> tuple[Path, str]:
    """Remove the bruriah pre-commit hook from the target repository.

    Returns (pre_commit_path, status) where status is 'removed', 'not_installed', or 'not_found'.
    If the pre-commit script contains other commands, only the bruriah block is stripped.
    """
    try:
        hooks_dir = resolve_git_hooks_dir(repo_root)
    except HookError as error:
        if error.code == "not_a_git_repository":
            raise
        return repo_root / ".git" / "hooks" / "pre-commit", "not_found"

    target = hooks_dir / "pre-commit"
    if not target.is_file():
        return target, "not_found"

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as error:
        raise HookError(f"read_failed:{type(error).__name__}") from error

    if HOOK_START_MARKER not in content:
        return target, "not_installed"

    start_idx = content.find(HOOK_START_MARKER)
    end_idx = content.find(HOOK_END_MARKER)
    if end_idx != -1:
        end_idx += len(HOOK_END_MARKER)
        remaining = (content[:start_idx] + content[end_idx:]).strip()
    else:
        remaining = content[:start_idx].strip()

    # If nothing remains or only shebang remains, delete the file
    lines = [line.strip() for line in remaining.splitlines() if line.strip()]
    if not lines or (len(lines) == 1 and lines[0].startswith("#!")):
        try:
            target.unlink()
        except OSError as error:
            raise HookError(f"unlink_failed:{type(error).__name__}") from error
        return target, "removed"

    # Otherwise write back the preserved content
    try:
        target.write_text(remaining + "\n", encoding="utf-8")
    except OSError as error:
        raise HookError(f"write_failed:{type(error).__name__}") from error

    return target, "removed"
