from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from bruriah.why import (
    CausalResolution,
    CommitInfo,
    WhyError,
    check_lineage_alerts,
    find_decision_in_database,
    format_why_human,
    format_why_json,
    parse_target,
    resolve_commit_for_target,
    trace_causal_archaeology,
)


def test_parse_target_valid_and_edge_cases() -> None:
    # Standard posix file + line
    path, line = parse_target("src/bruriah/retrieval.py:158")
    assert path == "src/bruriah/retrieval.py"
    assert line == 158

    # Standard posix file without line
    path, line = parse_target("src/bruriah/retrieval.py")
    assert path == "src/bruriah/retrieval.py"
    assert line is None

    # Windows drive letter with line
    path, line = parse_target("C:\\project\\src\\main.py:42")
    assert path == "C:\\project\\src\\main.py"
    assert line == 42

    # Windows drive letter without line
    path, line = parse_target("C:\\project\\src\\main.py")
    assert path == "C:\\project\\src\\main.py"
    assert line is None

    # Invalid line number (zero or negative)
    with pytest.raises(WhyError) as caught:
        parse_target("file.py:0")
    assert caught.value.code == "invalid_line_number"

    # Empty target
    with pytest.raises(WhyError) as caught:
        parse_target("   ")
    assert caught.value.code == "empty_target"


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)


def _create_commit(repo: Path, filename: str, content: str, msg: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def test_resolve_commit_for_target(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    sha1 = _create_commit(repo, "code.py", "line 1\nline 2\nline 3\n", "feat: initial commit")
    sha2 = _create_commit(repo, "code.py", "line 1\nline 2 modified\nline 3\n", "fix: modify line 2")

    # Line 1 was touched by sha1
    c1 = resolve_commit_for_target(repo, "code.py", 1)
    assert c1.sha.startswith(sha1[:8])
    assert c1.author == "Test Author"
    assert c1.subject == "feat: initial commit"

    # Line 2 was touched by sha2
    c2 = resolve_commit_for_target(repo, "code.py", 2)
    assert c2.sha.startswith(sha2[:8])
    assert c2.subject == "fix: modify line 2"

    # File target resolves to latest commit on file (sha2)
    c_file = resolve_commit_for_target(repo, "code.py")
    assert c_file.sha == sha2

    # Untracked file
    with pytest.raises(WhyError) as caught:
        resolve_commit_for_target(repo, "nonexistent.py")
    assert caught.value.code == "file_not_in_git"

    # Line out of range
    with pytest.raises(WhyError) as caught:
        resolve_commit_for_target(repo, "code.py", 100)
    assert caught.value.code == "line_out_of_range"


def test_find_decision_and_lineage_alerts(tmp_path: Path) -> None:
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

        sha_initial = "1111111122223333444455556666777788889999"
        sha_successor = "aaaaaaaabbbbccccddddeeeeffff000011112222"

        meta_doc1 = json.dumps({"commit": sha_initial, "verification_date": "2026-01-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-initial", "initial.md", "hash1", meta_doc1))
        doc1_text = """# Initial Architecture Decision

**Decided:** 2026-01-01 · **Commit:** `111111112222` · **Author:** Senior Architect

We decided to use pure SQLite for all index tables to guarantee zero foreign dependencies.

## Files this decision touched
- `src/core/storage.py`
"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p1", "doc-initial", "initial.md", "[]", 1, 10, doc1_text, "h1", meta_doc1, doc1_text, b"vec"))

        meta_doc2 = json.dumps({"commit": sha_successor, "verification_date": "2026-06-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-successor", "successor.md", "hash2", meta_doc2))
        doc2_text = """# Modernized Storage Engine

**Decided:** 2026-06-01 · **Commit:** `aaaaaaaabbbb` · **Author:** Lead Engineer

Supersedes the initial storage decision with memory-mapped files.
"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p2", "doc-successor", "successor.md", "[]", 1, 5, doc2_text, "h2", meta_doc2, doc2_text, b"vec"))

        db.execute("INSERT INTO lineage VALUES (?, ?, ?, ?)",
                   ("doc-successor", sha_initial[:8], "doc-initial", "supersedes"))
        db.commit()

        # 1. Lookup initial decision
        decision = find_decision_in_database(db, sha_initial)
        assert decision is not None
        assert decision.subject == "Initial Architecture Decision"
        assert "pure SQLite" in decision.body
        assert decision.files == ("src/core/storage.py",)

        # 2. Check lineage alert
        alerts = check_lineage_alerts(db, decision.document_ref, sha_initial)
        assert len(alerts) == 1
        assert alerts[0].relation == "supersedes"
        assert alerts[0].successor_ref == "doc-successor"
        assert alerts[0].successor_commit == sha_successor
        assert alerts[0].successor_subject == "Modernized Storage Engine"

        # 3. Test formatting with alert
        res_with_alert = CausalResolution(
            target="src/core/storage.py:10",
            file_path="src/core/storage.py",
            line=10,
            line_commit=CommitInfo(sha=sha_initial, author="Senior Architect", date="2026-01-01", subject="Initial Architecture Decision"),
            governing_decision=decision,
            governing_commit=None,
            lineage_alerts=alerts,
        )
        human_alert = format_why_human(res_with_alert)
        assert "Lineage Alerts:" in human_alert
        assert "⚠️  SUPERSEDES by doc-successor (sha: aaaaaaaabbbb)" in human_alert
        assert '"Modernized Storage Engine"' in human_alert

        # 4. Test formatting without decision
        res_no_dec = CausalResolution(
            target="unindexed.py:1",
            file_path="unindexed.py",
            line=1,
            line_commit=CommitInfo(sha="1234567890ab", author="Dev", date="2026-01-01", subject="chore"),
            governing_decision=None,
            governing_commit=None,
            lineage_alerts=(),
        )
        human_no_dec = format_why_human(res_no_dec)
        assert "No governing architectural decision indexed" in human_no_dec


def test_check_lineage_alerts_transitive_multi_hop(tmp_path: Path) -> None:
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

        sha_a = "1111111122223333444455556666777788889999"
        sha_b = "bbbbbbbbccccddddeeeeffff0000111122223333"
        sha_c = "ccccccccddddeeeeffff00001111222233334444"

        meta_a = json.dumps({"commit": sha_a, "verification_date": "2026-01-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-a", "a.md", "hash_a", meta_a))
        text_a = "# Generation 1 Architecture\n\nInitial design.\n"
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p_a", "doc-a", "a.md", "[]", 1, 3, text_a, "ha", meta_a, text_a, b"v"))

        meta_b = json.dumps({"commit": sha_b, "verification_date": "2026-03-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-b", "b.md", "hash_b", meta_b))
        text_b = "# Generation 2 Architecture\n\nIntermediate rewrite.\n"
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p_b", "doc-b", "b.md", "[]", 1, 3, text_b, "hb", meta_b, text_b, b"v"))

        meta_c = json.dumps({"commit": sha_c, "verification_date": "2026-06-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-c", "c.md", "hash_c", meta_c))
        text_c = "# Generation 3 Cloud-Native\n\nActive modern architecture.\n"
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p_c", "doc-c", "c.md", "[]", 1, 3, text_c, "hc", meta_c, text_c, b"v"))

        # Link: doc-a -> doc-b -> doc-c
        db.execute("INSERT INTO lineage VALUES (?, ?, ?, ?)",
                   ("doc-b", sha_a[:8], "doc-a", "supersedes"))
        db.execute("INSERT INTO lineage VALUES (?, ?, ?, ?)",
                   ("doc-c", sha_b[:8], "doc-b", "supersedes"))
        db.commit()

        alerts = check_lineage_alerts(db, "doc-a", sha_a)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.relation == "supersedes"
        assert alert.successor_ref == "doc-b"
        assert alert.successor_commit == sha_b
        assert alert.successor_subject == "Generation 2 Architecture"
        assert alert.depth == 2
        assert alert.active_successor_ref == "doc-c"
        assert alert.active_successor_commit == sha_c
        assert alert.active_successor_subject == "Generation 3 Cloud-Native"
        assert len(alert.chain) == 2
        assert alert.chain[0].ref == "doc-b"
        assert alert.chain[1].ref == "doc-c"

        # Check formatting
        res = CausalResolution(
            target="src/core.py:1",
            file_path="src/core.py",
            line=1,
            line_commit=CommitInfo(sha=sha_a, author="Dev", date="2026-01-01", subject="feat: gen 1"),
            governing_decision=find_decision_in_database(db, sha_a),
            governing_commit=None,
            lineage_alerts=alerts,
        )
        human = format_why_human(res)
        assert "⚠️  SUPERSEDES by doc-b (sha: bbbbbbbbcccc)" in human
        assert "↳ subsequently evolved through 2 generations to [CURRENT ACTIVE]: doc-c (sha: ccccccccdddd)" in human
        assert '"Generation 3 Cloud-Native"' in human

        json_repr = json.loads(format_why_json(res))
        alert_json = json_repr["lineage_alerts"][0]
        assert alert_json["depth"] == 2
        assert alert_json["active_successor_ref"] == "doc-c"
        assert alert_json["active_successor_commit"] == sha_c
        assert alert_json["active_successor_subject"] == "Generation 3 Cloud-Native"
        assert len(alert_json["chain"]) == 2



def test_trace_causal_archaeology_e2e(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    sha_feat = _create_commit(repo, "storage.py", "def connect():\n    return sqlite3.connect('app.db')\n", "feat(storage): initial sqlite storage")
    sha_chore = _create_commit(repo, "storage.py", "def connect():\n    # chore formatting\n    return sqlite3.connect('app.db')\n", "style: format comment")

    # Set up index database with decision for sha_feat only (sha_chore was not indexed)
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
        meta_feat = json.dumps({"commit": sha_feat, "verification_date": "2026-03-01"})
        db.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", ("doc-feat", "feat.md", "hash1", meta_feat))
        feat_text = f"""# SQLite Storage Implementation

**Decided:** 2026-03-01 · **Commit:** `{sha_feat[:12]}` · **Author:** Test Author

Decided to use raw sqlite3 connection pooling.

## Files this decision touched
- `storage.py`
"""
        db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   ("p1", "doc-feat", "feat.md", "[]", 1, 10, feat_text, "h1", meta_feat, feat_text, b"vec"))
        db.commit()

        # Trace line 2 (# chore formatting)
        # The line was touched by sha_chore, but the governing decision traces back to sha_feat!
        res = trace_causal_archaeology(repo, db, "storage.py:2")
        assert res.target == "storage.py:2"
        assert res.line_commit.subject == "style: format comment"
        assert res.line_commit.sha == sha_chore
        assert res.governing_decision is not None
        assert res.governing_decision.subject == "SQLite Storage Implementation"
        assert res.governing_commit is not None
        assert res.governing_commit.sha == sha_feat
        assert "raw sqlite3 connection pooling" in res.governing_decision.body
        assert res.lineage_alerts == ()

        # Test formatters
        human = format_why_human(res)
        assert "Target: storage.py:2" in human
        assert "Line Commit:" in human
        assert "style: format comment" in human
        assert "Governing Architectural Decision:" in human
        assert "SQLite Storage Implementation" in human
        assert "Why this was written:" in human

        json_str = format_why_json(res)
        parsed = json.loads(json_str)
        assert parsed["target"] == "storage.py:2"
        assert parsed["line_commit"]["subject"] == "style: format comment"
        assert parsed["governing_decision"]["subject"] == "SQLite Storage Implementation"
        assert parsed["lineage_alerts"] == []
