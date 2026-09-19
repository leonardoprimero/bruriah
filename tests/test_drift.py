from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from bruriah.drift import (
    DriftError,
    DriftReport,
    DriftWarning,
    FileGovernance,
    analyze_architectural_drift,
    format_drift_human,
    format_drift_json,
    get_git_diff_files,
)
from bruriah.repository import SnapshotRepository


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)


def _create_commit(repo: Path, filename: str, content: str, msg: str) -> str:
    file_path = repo / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_get_git_diff_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    sha1 = _create_commit(repo, "src/core/storage.py", "# initial", "feat: initial storage")
    sha2 = _create_commit(repo, "src/api/router.py", "# router", "feat: initial router")

    # 1. Modify uncommitted working tree
    (repo / "src/core/storage.py").write_text("# modified storage", encoding="utf-8")
    diff_uncommitted = get_git_diff_files(repo)
    assert "src/core/storage.py" in diff_uncommitted

    # 2. Stage the modification
    subprocess.run(["git", "add", "src/core/storage.py"], cwd=repo, check=True, capture_output=True)
    diff_staged = get_git_diff_files(repo, staged=True)
    assert diff_staged == ("src/core/storage.py",)

    # 3. Revision range
    diff_range = get_git_diff_files(repo, revision_or_range=f"{sha1}..{sha2}")
    assert diff_range == ("src/api/router.py",)


def test_analyze_architectural_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    sha_initial = _create_commit(repo, "src/core/storage.py", "# storage v1", "feat: pure sqlite storage")
    sha_active = _create_commit(repo, "src/api/router.py", "# router v1", "feat: api router")
    sha_succ = _create_commit(repo, "src/core/cloud.py", "# cloud storage", "feat: cloud native storage")

    # Modify storage.py and router.py in working tree
    (repo / "src/core/storage.py").write_text("# storage v2 edit", encoding="utf-8")
    (repo / "src/api/router.py").write_text("# router v2 edit", encoding="utf-8")
    (repo / "unindexed.txt").write_text("# unindexed", encoding="utf-8")

    db_path = tmp_path / "index.sqlite3"
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        db.execute("""
            CREATE TABLE documents (
                document_ref TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL,
                source_hash TEXT NOT NULL, metadata TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE passages (
                ref TEXT PRIMARY KEY, document_ref TEXT NOT NULL, relative_path TEXT NOT NULL,
                heading_path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                text TEXT NOT NULL, source_hash TEXT NOT NULL, metadata TEXT NOT NULL,
                search_text TEXT NOT NULL, vector BLOB NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE lineage (
                successor_ref TEXT NOT NULL, predecessor_target TEXT NOT NULL,
                predecessor_ref TEXT, relation TEXT NOT NULL,
                PRIMARY KEY (successor_ref, predecessor_target, relation)
            )
        """)

        meta_initial = json.dumps({"commit": sha_initial, "verification_date": "2026-01-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-storage-v1", "storage.md", "hash1", meta_initial))
        text_initial = """# Pure SQLite Storage Architecture\n\n**Decided:** 2026-01-01 · **Commit:** `111111112222` · **Author:** Senior Architect\n\nPure SQLite.\n"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p1", "doc-storage-v1", "storage.md", "[]", 1, 5, text_initial, "h1", meta_initial, text_initial, b"vec"))

        meta_succ = json.dumps({"commit": sha_succ, "verification_date": "2026-06-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-storage-v2", "cloud.md", "hash2", meta_succ))
        text_succ = """# Cloud-Native Storage Engine\n\n**Decided:** 2026-06-01 · **Commit:** `ccccccccdddd` · **Author:** Lead Architect\n\nCloud storage.\n"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p2", "doc-storage-v2", "cloud.md", "[]", 1, 5, text_succ, "h2", meta_succ, text_succ, b"vec"))

        meta_router = json.dumps({"commit": sha_active, "verification_date": "2026-03-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-api-v1", "router.md", "hash3", meta_router))
        text_router = """# API Routing Standards\n\n**Decided:** 2026-03-01 · **Commit:** `333333334444` · **Author:** API Team\n\nRouting.\n"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p3", "doc-api-v1", "router.md", "[]", 1, 5, text_router, "h3", meta_router, text_router, b"vec"))

        # Storage v1 is superseded by Storage v2
        db.execute("INSERT INTO lineage VALUES (?, ?, ?, ?)",
                   ("doc-storage-v2", sha_initial[:8], "doc-storage-v1", "supersedes"))
        db.commit()

        repo_layer = SnapshotRepository(db)
        files = ("src/core/storage.py", "src/api/router.py", "unindexed.txt")
        report = analyze_architectural_drift(repo, repo_layer, files)

        assert report.has_drift is True
        assert len(report.stale_warnings) == 1
        warn = report.stale_warnings[0]
        assert warn.file_path == "src/core/storage.py"
        assert warn.governing_decision == "Pure SQLite Storage Architecture"
        assert warn.lineage_state == "SUPERSEDES"
        assert warn.current_active_decision == "Cloud-Native Storage Engine"
        assert "Amends:" in warn.action_recommendation

        assert len(report.clean_files) == 1
        clean = report.clean_files[0]
        assert clean.file_path == "src/api/router.py"
        assert clean.conforms is True
        assert clean.governing_decision == "API Routing Standards"

        assert report.unindexed_files == ("unindexed.txt",)


def test_format_drift_human_and_json() -> None:
    report = DriftReport(
        repo_path="/workspace/repo",
        revision_or_range=None,
        staged=True,
        inspected_files=("src/core/storage.py", "src/api/router.py"),
        stale_warnings=(
            DriftWarning(
                file_path="src/core/storage.py",
                governing_decision="Pure SQLite Storage",
                decision_ref="doc-storage-v1",
                decision_sha="1111111122223333",
                lineage_state="SUPERSEDES",
                current_active_decision="Cloud Storage",
                current_active_ref="doc-storage-v2",
                current_active_sha="ccccccccdddd1111",
                generations=1,
                action_recommendation="Ensure your changes adhere to Cloud Storage.",
            ),
        ),
        clean_files=(
            FileGovernance(
                file_path="src/api/router.py",
                conforms=True,
                governing_decision="API Standards",
                governing_ref="doc-api-v1",
                governing_sha="3333333344445555",
                status="clean",
            ),
        ),
        unindexed_files=(),
        missing_trailers=(),
    )

    human = format_drift_human(report)
    assert "STALE GOVERNANCE DETECTED" in human
    assert "src/core/storage.py" in human
    assert "CLEAN GOVERNANCE" in human
    assert "src/api/router.py" in human
    assert "Run with --strict in CI" in human

    payload_json = format_drift_json(report)
    parsed = json.loads(payload_json)
    assert parsed["has_drift"] is True
    assert len(parsed["stale_warnings"]) == 1
    assert parsed["stale_warnings"][0]["file_path"] == "src/core/storage.py"
    assert len(parsed["clean_files"]) == 1


_FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)


def _fake_embedder_factory(model_name: str):
    from array import array
    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]
    return embed, _FINGERPRINT, 3


def test_drift_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from bruriah import cli
    from bruriah.platform import resolve_paths

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    sha_init = _create_commit(repo, "src/core/storage.py", "# v1", "feat: initial storage")

    root = tmp_path / "vault"
    root.mkdir()
    doc_content = f"""---
commit: {sha_init}
---

# SQLite Storage Architecture

**Decided:** 2026-03-01 · **Commit:** `{sha_init[:12]}` · **Author:** Test Author

Decided to use raw sqlite3 connection pooling.

## Files this decision touched
- `src/core/storage.py`
"""
    (root / "decisions.md").write_text(doc_content, encoding="utf-8")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")

    paths = resolve_paths(
        cli_config_dir=tmp_path / "config", cli_data_dir=tmp_path / "data",
        cli_cache_dir=tmp_path / "cache", cli_log_dir=tmp_path / "log", env={},
    )
    cli.run_init(paths)
    cli.run_index(
        paths, root, policy_path, model_name="test/minilm", embedder_factory=_fake_embedder_factory,
    )

    # Modify storage.py in working tree
    (repo / "src/core/storage.py").write_text("# uncommitted edit", encoding="utf-8")

    # 1. Run drift --strict on clean active decision -> should exit 0
    capsys.readouterr()  # flush
    code_clean = cli.bruriah_main([
        "drift",
        "--repo", str(repo),
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
        "--strict",
    ])
    out_clean = capsys.readouterr().out
    assert code_clean == 0
    assert "CLEAN GOVERNANCE" in out_clean
    assert "src/core/storage.py" in out_clean

    # 2. Run drift with --json
    code_json = cli.bruriah_main([
        "drift",
        "--repo", str(repo),
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
        "--json",
    ])
    out_json = capsys.readouterr().out
    assert code_json == 0
    parsed = json.loads(out_json)
    assert parsed["has_drift"] is False
    assert len(parsed["clean_files"]) == 1
    assert parsed["clean_files"][0]["file_path"] == "src/core/storage.py"

