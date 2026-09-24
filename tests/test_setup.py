from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bruriah.clients import LaunchManifest
from bruriah.setup import (
    SetupError,
    detect_installed_clients,
    merge_client_config,
    resolve_client_config_path,
    setup_client,
)

_WINDOWS = os.name == "nt"
_CMD = "C:\\opt\\bruriah\\bruriah" if _WINDOWS else "/opt/homebrew/bin/bruriah"


@pytest.fixture
def manifest() -> LaunchManifest:
    return LaunchManifest(
        command=_CMD,
        args=("serve", "--data-dir", "/tmp/bruriah/data"),
        server_name="bruriah",
    )


def test_resolve_client_config_path_project_and_global(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    # Cursor
    assert resolve_client_config_path("cursor", project_root=repo, scope="project") == repo / ".cursor" / "mcp.json"
    assert (
        resolve_client_config_path("cursor", project_root=repo, scope="global") == Path.home() / ".cursor" / "mcp.json"
    )

    # Claude Code
    assert resolve_client_config_path("claude", project_root=repo, scope="project") == repo / ".mcp.json"
    assert resolve_client_config_path("claude", project_root=repo, scope="global") == Path.home() / ".claude.json"

    # Claude Desktop (always global)
    assert "Claude" in str(resolve_client_config_path("claude-desktop", project_root=repo))

    # Gemini
    assert (
        resolve_client_config_path("gemini", project_root=repo, scope="project") == repo / ".gemini" / "settings.json"
    )

    # OpenCode
    assert resolve_client_config_path("opencode", project_root=repo, scope="project") == repo / "opencode.json"

    # Unknown client
    with pytest.raises(SetupError) as exc:
        resolve_client_config_path("unknown", project_root=repo)
    assert "unknown_client" in exc.value.code


def test_merge_mcp_servers_creates_and_updates_without_clobbering_existing_tools(
    tmp_path: Path, manifest: LaunchManifest
) -> None:
    config_file = tmp_path / "mcp.json"

    # 1. Fresh file
    merged, status = merge_client_config("cursor", config_file, manifest)
    assert status == "created"
    assert "mcpServers" in merged
    assert "bruriah" in merged["mcpServers"]
    assert merged["mcpServers"]["bruriah"]["command"] == _CMD

    # Write initial config with another server
    initial = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            }
        }
    }
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    # 2. Merge into existing without clobbering "filesystem"
    merged, status = merge_client_config("cursor", config_file, manifest)
    assert status == "created"  # "bruriah" is newly created in existing file
    assert "filesystem" in merged["mcpServers"]
    assert "bruriah" in merged["mcpServers"]

    # 3. Merging identical manifest reports "unchanged"
    config_file.write_text(json.dumps(merged), encoding="utf-8")
    merged_again, status_again = merge_client_config("cursor", config_file, manifest)
    assert status_again == "unchanged"

    # 4. Updating manifest reports "updated"
    updated_manifest = LaunchManifest(
        command=_CMD,
        args=("serve", "--data-dir", "/new/path"),
        server_name="bruriah",
    )
    merged_updated, status_updated = merge_client_config("cursor", config_file, updated_manifest)
    assert status_updated == "updated"
    assert merged_updated["mcpServers"]["bruriah"]["args"] == ["serve", "--data-dir", "/new/path"]
    assert "filesystem" in merged_updated["mcpServers"]  # preserved!


def test_merge_opencode_creates_and_preserves_existing(tmp_path: Path, manifest: LaunchManifest) -> None:
    config_file = tmp_path / "opencode.json"
    initial = {"mcp": {"existing_tool": {"type": "local", "command": ["node", "server.js"]}}}
    config_file.write_text(json.dumps(initial), encoding="utf-8")

    merged, status = merge_client_config("opencode", config_file, manifest)
    assert status == "created"
    assert "existing_tool" in merged["mcp"]
    assert "bruriah" in merged["mcp"]
    assert merged["mcp"]["bruriah"]["command"] == manifest.full_argv


def test_setup_client_dry_run_does_not_modify_disk(tmp_path: Path, manifest: LaunchManifest) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / ".cursor" / "mcp.json"

    res = setup_client("cursor", manifest, project_root=repo, scope="project", dry_run=True)
    assert res.status == "created"
    assert not target.exists()  # dry run: file must not be created


def test_setup_client_creates_backup_on_modification(tmp_path: Path, manifest: LaunchManifest) -> None:
    repo = tmp_path / "repo"
    target = repo / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"mcpServers": {"old": {"command": "old"}}}), encoding="utf-8")

    res = setup_client("cursor", manifest, project_root=repo, scope="project", dry_run=False)
    assert res.status == "created"
    assert target.exists()

    # Backup file was created
    backup = target.with_suffix(".json.bak")
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == {"mcpServers": {"old": {"command": "old"}}}


def test_setup_client_rejects_malformed_json_typed(tmp_path: Path, manifest: LaunchManifest) -> None:
    repo = tmp_path / "repo"
    target = repo / ".cursor" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text("invalid { json", encoding="utf-8")

    with pytest.raises(SetupError) as exc:
        setup_client("cursor", manifest, project_root=repo, scope="project")
    assert exc.value.code == "invalid_json_in_target_file"


def test_detect_installed_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    (home / ".cursor").mkdir()
    (home / ".gemini").mkdir()

    detected = detect_installed_clients()
    assert "cursor" in detected
    assert "gemini" in detected
