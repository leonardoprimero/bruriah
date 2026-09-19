from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = [
    "MANAGED_ALIASES",
    "AliasError",
    "get_git_alias",
    "install_aliases",
    "set_git_alias",
    "uninstall_aliases",
    "unset_git_alias",
]

MANAGED_ALIASES: dict[str, str] = {
    "why": "!bruriah why",
    "drift": "!bruriah drift",
}


class AliasError(ValueError):
    """Typed failure for git alias operations."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _scope_args(scope: str, repo: Path | None = None) -> tuple[list[str], Path | None]:
    if scope == "global":
        return ["--global"], None
    if scope == "local":
        if repo is None:
            raise AliasError("local_scope_requires_repo")
        if not (repo / ".git").exists():
            raise AliasError("not_a_git_repository")
        return ["--local"], repo.resolve()
    raise AliasError("invalid_scope")


def get_git_alias(name: str, *, scope: str = "global", repo: Path | None = None) -> str | None:
    """Retrieve the current value of a git alias in the given scope."""
    scope_flag, cwd = _scope_args(scope, repo)
    try:
        proc = subprocess.run(
            ["git", "config", *scope_flag, f"alias.{name}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        if proc.returncode == 1:
            return None
        raise AliasError(f"git_config_failed:{proc.stderr.strip()}")
    except OSError as error:
        raise AliasError(f"git_invocation_failed:{type(error).__name__}") from error


def set_git_alias(
    name: str,
    command: str,
    *,
    scope: str = "global",
    repo: Path | None = None,
) -> str:
    """Set a git alias, returning 'created', 'updated', or 'unchanged'."""
    existing = get_git_alias(name, scope=scope, repo=repo)
    if existing == command:
        return "unchanged"

    scope_flag, cwd = _scope_args(scope, repo)
    try:
        proc = subprocess.run(
            ["git", "config", *scope_flag, f"alias.{name}", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AliasError(f"git_config_failed:{proc.stderr.strip()}")
        return "created" if existing is None else "updated"
    except OSError as error:
        raise AliasError(f"git_invocation_failed:{type(error).__name__}") from error


def unset_git_alias(name: str, *, scope: str = "global", repo: Path | None = None) -> str:
    """Unset a git alias, returning 'removed' or 'not_found'."""
    existing = get_git_alias(name, scope=scope, repo=repo)
    if existing is None:
        return "not_found"

    scope_flag, cwd = _scope_args(scope, repo)
    try:
        proc = subprocess.run(
            ["git", "config", *scope_flag, "--unset", f"alias.{name}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise AliasError(f"git_config_failed:{proc.stderr.strip()}")
        return "removed"
    except OSError as error:
        raise AliasError(f"git_invocation_failed:{type(error).__name__}") from error


def install_aliases(
    *,
    scope: str = "global",
    repo: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Install all managed Bruriah git aliases."""
    results: list[tuple[str, str, str]] = []
    for name, command in MANAGED_ALIASES.items():
        status = set_git_alias(name, command, scope=scope, repo=repo)
        results.append((name, command, status))
    return results


def uninstall_aliases(
    *,
    scope: str = "global",
    repo: Path | None = None,
) -> list[tuple[str, str]]:
    """Uninstall all managed Bruriah git aliases."""
    results: list[tuple[str, str]] = []
    for name in MANAGED_ALIASES:
        status = unset_git_alias(name, scope=scope, repo=repo)
        results.append((name, status))
    return results
