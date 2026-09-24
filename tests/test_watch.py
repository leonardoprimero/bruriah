from __future__ import annotations

import subprocess
from array import array
from pathlib import Path

import pytest

from bruriah.cli import Embedder, bruriah_main
from bruriah.platform import resolve_paths
from bruriah.watch import RepoWatcher, WatchError, get_git_state_fingerprint, get_head_sha


def _fake_embedder_factory(model_name: str) -> tuple[Embedder, str, int]:
    fingerprint = (
        '{"artifact":"model.onnx","artifact_sha256":"'
        + "a" * 64
        + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
    )

    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

    return embed, fingerprint, 3


def _setup_git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    return path


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_watch_fails_on_non_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "not_git"
    non_repo.mkdir()
    paths = resolve_paths(cli_data_dir=tmp_path / "data", cli_config_dir=tmp_path / "config", env={})

    with pytest.raises(WatchError) as exc_info:
        RepoWatcher(non_repo, paths)
    assert exc_info.value.code == "not_a_git_repository"


def test_watch_sync_and_reuse_across_commits(tmp_path: Path) -> None:
    repo = _setup_git_repo(tmp_path / "repo")
    paths = resolve_paths(cli_data_dir=tmp_path / "data", cli_config_dir=tmp_path / "config", env={})

    # Commit 1 with explanatory decision
    _commit(
        repo,
        "auth.py",
        "def auth(): pass\n",
        "feat(auth): use session tokens\n\nBecause cookies are vulnerable to cross-subdomain attacks.",
    )

    watcher = RepoWatcher(
        repo,
        paths,
        embedder_factory=_fake_embedder_factory,
        interval=0.1,
    )

    # Initial sync
    res1 = watcher.sync()
    assert res1 is not None
    assert res1.documents == 1
    assert res1.reused_documents == 0

    # Commit 2 with second decision
    _commit(
        repo,
        "db.py",
        "def db(): pass\n",
        "feat(db): adopt sqlite\n\nBecause local-first storage eliminates network dependencies.",
    )

    # Second sync reuses document 1
    res2 = watcher.sync()
    assert res2 is not None
    assert res2.documents == 2
    assert res2.reused_documents == 1


def test_watch_cli_once(tmp_path: Path) -> None:
    repo = _setup_git_repo(tmp_path / "repo")
    _commit(
        repo,
        "net.py",
        "def net(): pass\n",
        "feat(net): disable telemetry\n\nPrivacy-first design requires zero outbound traffic.",
    )

    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    # Run via CLI with --once
    code = bruriah_main(
        [
            "watch",
            "--repo",
            str(repo),
            "--data-dir",
            str(data_dir),
            "--config-dir",
            str(config_dir),
            "--once",
        ]
    )
    assert code == 0
    assert (data_dir / "active.json").exists()


def test_watch_fingerprint_changes_on_commit(tmp_path: Path) -> None:
    repo = _setup_git_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "1", "feat: initial commit\n\nExplanatory reasoning.")
    fp1 = get_git_state_fingerprint(repo)

    _commit(repo, "b.txt", "2", "feat: second commit\n\nExplanatory reasoning.")
    fp2 = get_git_state_fingerprint(repo)

    assert fp1 != fp2
