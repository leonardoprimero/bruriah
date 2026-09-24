from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clients import LaunchManifest, _mcp_servers_entry
from .platform import find_project_root

__all__ = [
    "SUPPORTED_CLIENTS",
    "SetupError",
    "SetupResult",
    "detect_installed_clients",
    "merge_client_config",
    "resolve_client_config_path",
    "setup_client",
]

SUPPORTED_CLIENTS = ("cursor", "claude", "claude-desktop", "gemini", "opencode")


class SetupError(ValueError):
    """Typed failure for client setup operations."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SetupResult:
    client: str
    target_path: Path
    status: str  # "created" | "updated" | "unchanged"
    content: str


def _claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def resolve_client_config_path(
    client: str,
    *,
    project_root: Path | None = None,
    scope: str = "auto",
) -> Path:
    """Determine the destination config file path for a client."""
    normalized = client.lower().strip()
    if normalized not in SUPPORTED_CLIENTS:
        raise SetupError(f"unknown_client:{normalized}")

    if scope not in ("auto", "project", "global"):
        raise SetupError("invalid_scope")

    is_project = (scope == "project") or (scope == "auto" and project_root is not None)

    if normalized == "claude-desktop":
        # Claude Desktop is always a global application config
        return _claude_desktop_path()

    if normalized == "cursor":
        if is_project:
            if project_root is None:
                raise SetupError("project_root_not_found")
            return project_root / ".cursor" / "mcp.json"
        return Path.home() / ".cursor" / "mcp.json"

    if normalized == "claude":
        if is_project:
            if project_root is None:
                raise SetupError("project_root_not_found")
            return project_root / ".mcp.json"
        return Path.home() / ".claude.json"

    if normalized == "gemini":
        if is_project:
            if project_root is None:
                raise SetupError("project_root_not_found")
            return project_root / ".gemini" / "settings.json"
        return Path.home() / ".gemini" / "settings.json"

    if normalized == "opencode":
        if is_project:
            if project_root is None:
                raise SetupError("project_root_not_found")
            return project_root / "opencode.json"
        return Path.home() / ".config" / "opencode" / "opencode.json"

    raise SetupError(f"unknown_client:{normalized}")


def _merge_mcp_servers(
    existing: dict[str, Any],
    manifest: LaunchManifest,
    top_key: str = "mcpServers",
) -> tuple[dict[str, Any], str]:
    servers = existing.get(top_key)
    if servers is None:
        existing[top_key] = {manifest.server_name: _mcp_servers_entry(manifest)}
        return existing, "created"
    if not isinstance(servers, dict):
        raise SetupError(f"invalid_section:{top_key}")

    new_entry = _mcp_servers_entry(manifest)
    if servers.get(manifest.server_name) == new_entry:
        return existing, "unchanged"

    status = "updated" if manifest.server_name in servers else "created"
    servers[manifest.server_name] = new_entry
    return existing, status


def _merge_opencode(
    existing: dict[str, Any],
    manifest: LaunchManifest,
) -> tuple[dict[str, Any], str]:
    mcp_section = existing.get("mcp")
    if mcp_section is None:
        existing["mcp"] = {
            manifest.server_name: {
                "type": "local",
                "command": manifest.full_argv,
                "environment": manifest.env_dict,
            }
        }
        return existing, "created"
    if not isinstance(mcp_section, dict):
        raise SetupError("invalid_section:mcp")

    new_entry = {
        "type": "local",
        "command": manifest.full_argv,
        "environment": manifest.env_dict,
    }
    if mcp_section.get(manifest.server_name) == new_entry:
        return existing, "unchanged"

    status = "updated" if manifest.server_name in mcp_section else "created"
    mcp_section[manifest.server_name] = new_entry
    return existing, status


def merge_client_config(
    client: str,
    target_path: Path,
    manifest: LaunchManifest,
) -> tuple[dict[str, Any], str]:
    """Load existing JSON (if present), merge bruriah entry, and return updated dict and status."""
    normalized = client.lower().strip()
    existing: dict[str, Any] = {}
    is_new = not target_path.exists()

    if not is_new:
        try:
            text = target_path.read_text(encoding="utf-8")
            if text.strip():
                loaded = json.loads(text)
                if not isinstance(loaded, dict):
                    raise SetupError("config_not_a_json_object")
                existing = loaded
        except json.JSONDecodeError as error:
            raise SetupError("invalid_json_in_target_file") from error
        except OSError as error:
            raise SetupError(f"read_failed:{type(error).__name__}") from error

    if normalized in ("cursor", "claude", "claude-desktop", "gemini"):
        merged, status = _merge_mcp_servers(existing, manifest, top_key="mcpServers")
    elif normalized == "opencode":
        merged, status = _merge_opencode(existing, manifest)
    else:
        raise SetupError(f"unknown_client:{normalized}")

    final_status = "created" if is_new else status
    return merged, final_status


def _atomic_write_json(path: Path, payload: dict[str, Any], *, make_backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if make_backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_bytes(path.read_bytes())

    temp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    try:
        temp.write(content)
        temp.flush()
        os.fsync(temp.fileno())
        temp.close()
        os.replace(temp.name, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        if os.path.exists(temp.name):
            os.unlink(temp.name)
        raise


def detect_installed_clients(project_root: Path | None = None) -> list[str]:
    """Detect which clients are installed or configured in the environment."""
    detected: list[str] = []

    # Cursor
    if (Path.home() / ".cursor").is_dir() or (project_root and (project_root / ".cursor").is_dir()):
        detected.append("cursor")

    # Claude Desktop
    if _claude_desktop_path().parent.is_dir():
        detected.append("claude-desktop")

    # Claude Code
    if (Path.home() / ".claude").is_dir() or (project_root and (project_root / ".mcp.json").exists()):
        detected.append("claude")

    # Gemini CLI
    if (Path.home() / ".gemini").is_dir() or (project_root and (project_root / ".gemini").is_dir()):
        detected.append("gemini")

    # OpenCode
    if (Path.home() / ".config" / "opencode").is_dir() or (project_root and (project_root / "opencode.json").exists()):
        detected.append("opencode")

    return detected


def setup_client(
    client: str,
    manifest: LaunchManifest,
    *,
    project_root: Path | None = None,
    scope: str = "auto",
    dry_run: bool = False,
) -> SetupResult:
    """Configure bruriah MCP server non-destructively in the client's settings."""
    root = project_root if project_root is not None else find_project_root()
    target_path = resolve_client_config_path(client, project_root=root, scope=scope)
    merged, status = merge_client_config(client, target_path, manifest)
    content = json.dumps(merged, indent=2, sort_keys=True) + "\n"

    if not dry_run and status != "unchanged":
        try:
            _atomic_write_json(target_path, merged, make_backup=True)
        except OSError as error:
            raise SetupError(f"write_failed:{type(error).__name__}") from error

    return SetupResult(
        client=client,
        target_path=target_path,
        status=status,
        content=content,
    )
