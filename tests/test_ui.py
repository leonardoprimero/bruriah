from __future__ import annotations

import contextlib
import json
import sqlite3
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

from bruriah.cli import _build_cli_parser, bruriah_main
from bruriah.ui import (
    DAGData,
    DecisionNode,
    LineageEdge,
    UIError,
    _extract_author_date,
    _extract_files_from_text,
    _extract_metadata_field,
    _extract_subject,
    _UIHandler,
    build_dag_from_database,
    run_ui,
)


def _setup_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite database populated with documents, passages, and lineage."""
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE documents (
            document_ref TEXT PRIMARY KEY,
            relative_path TEXT UNIQUE NOT NULL,
            source_hash TEXT NOT NULL,
            metadata TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE passages (
            ref TEXT PRIMARY KEY,
            document_ref TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            heading_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            text TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            metadata TEXT NOT NULL,
            search_text TEXT NOT NULL,
            vector BLOB NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE lineage (
            successor_ref TEXT NOT NULL,
            predecessor_target TEXT NOT NULL,
            predecessor_ref TEXT,
            relation TEXT NOT NULL,
            PRIMARY KEY (successor_ref, predecessor_target, relation)
        ) WITHOUT ROWID;
    """)

    # Insert Document 1 (Older, superseded)
    meta1 = json.dumps({"commit": "11111111aaaa", "status": "superseded"})
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?)",
        ("doc:1", "decisions/001-auth.md", "hash1", meta1),
    )
    text1 = (
        "# JWT Authentication Architecture\n\n"
        "**Decided:** 2026-01-10 · **Commit:** `11111111` · **Author:** Alice\n\n"
        "We choose JWT for authentication across services.\n\n"
        "## Files this decision touched\n"
        "- `src/auth.py`\n"
        "- `src/tokens.py`\n"
    )
    conn.execute(
        "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("pass:1", "doc:1", "decisions/001-auth.md", "[]", 1, 15, text1, "hash1", meta1, "", b""),
    )

    # Insert Document 2 (Newer, active, supersedes Document 1)
    meta2 = json.dumps({"commit": "22222222bbbb", "status": "active"})
    conn.execute(
        "INSERT INTO documents VALUES (?, ?, ?, ?)",
        ("doc:2", "decisions/002-oauth2.md", "hash2", meta2),
    )
    text2 = (
        "# OAuth2 & Session Tokens Migration\n\n"
        "**Decided:** 2026-02-15 · **Commit:** `22222222` · **Author:** Bob\n\n"
        "Migrate from JWT to opaque session tokens via OAuth2.\n\n"
        "## Files this decision touched\n"
        "- `src/auth.py`\n"
    )
    conn.execute(
        "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("pass:2", "doc:2", "decisions/002-oauth2.md", "[]", 1, 15, text2, "hash2", meta2, "", b""),
    )

    # Lineage: doc:2 supersedes doc:1
    conn.execute(
        "INSERT INTO lineage VALUES (?, ?, ?, ?)",
        ("doc:2", "11111111", "doc:1", "supersedes"),
    )

    return conn


class TestExtractHelpers:
    def test_extract_subject_found(self):
        text = "Some intro\n# Architecture Overhaul\nDetails..."
        assert _extract_subject(text) == "Architecture Overhaul"

    def test_extract_subject_missing(self):
        assert _extract_subject("No headings here") == "(untitled)"

    def test_extract_metadata_field(self):
        raw = json.dumps({"commit": "abcdef123456", "author": "Alice"})
        assert _extract_metadata_field(raw, "commit") == "abcdef123456"
        assert _extract_metadata_field(raw, "nonexistent") == ""
        assert _extract_metadata_field("not valid json", "commit") == ""

    def test_extract_files_from_text(self):
        text = (
            "## Summary\nSomething\n\n"
            "## Files this decision touched\n"
            "- `src/core/auth.py`\n"
            "- `src/models.py`\n"
            "- plain_path.txt\n\n"
            "## Next Steps\nMore..."
        )
        files = _extract_files_from_text(text)
        assert files == ["src/core/auth.py", "src/models.py", "plain_path.txt"]

    def test_extract_author_date(self):
        text = "**Decided:** 2026-03-01 · **Commit:** `abc1234` · **Author:** Leonardo Caliva"
        author, date = _extract_author_date(text)
        assert author == "Leonardo Caliva"
        assert date == "2026-03-01"

    def test_extract_author_date_missing(self):
        author, date = _extract_author_date("No metadata here")
        assert author == ""
        assert date == ""


class TestBuildDAGFromDatabase:
    def test_build_dag_with_complete_data(self):
        conn = _setup_test_db()
        try:
            dag = build_dag_from_database(conn)
            assert len(dag.nodes) == 2
            assert len(dag.edges) == 1

            # Check edge
            edge = dag.edges[0]
            assert edge.source == "doc:1"
            assert edge.target == "doc:2"
            assert edge.relation == "supersedes"

            # Check nodes
            node_map = {n.id: n for n in dag.nodes}
            doc1 = node_map["doc:1"]
            assert doc1.label == "JWT Authentication Architecture"
            assert doc1.status == "superseded"
            assert doc1.commit == "11111111"
            assert doc1.author == "Alice"
            assert doc1.date == "2026-01-10"
            assert "src/auth.py" in doc1.files
            assert "src/tokens.py" in doc1.files

            doc2 = node_map["doc:2"]
            assert doc2.label == "OAuth2 & Session Tokens Migration"
            assert doc2.status == "active"
            assert doc2.commit == "22222222"
            assert doc2.author == "Bob"
        finally:
            conn.close()

    def test_build_dag_empty_database(self):
        conn = sqlite3.connect(":memory:")
        try:
            dag = build_dag_from_database(conn)
            assert dag.nodes == []
            assert dag.edges == []
        finally:
            conn.close()

    def test_build_dag_without_lineage_table(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                "CREATE TABLE documents (document_ref TEXT, relative_path TEXT, metadata TEXT)"
            )
            conn.execute("INSERT INTO documents VALUES ('doc:1', 'p1.md', '{}')")
            dag = build_dag_from_database(conn)
            assert len(dag.nodes) == 1
            assert dag.edges == []
            assert dag.nodes[0].status == "active"
        finally:
            conn.close()


class TestUIHandler:
    @pytest.fixture
    def test_server(self):
        from http.server import HTTPServer

        sample_json = json.dumps({
            "nodes": [{"id": "doc:1", "label": "Test Node", "status": "active"}],
            "edges": [],
        })
        handler = type("Handler", (_UIHandler,), {"dag_json": sample_json})
        server = HTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        yield port

        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    def test_get_root_returns_html(self, test_server):
        conn = HTTPConnection("127.0.0.1", test_server)
        try:
            conn.request("GET", "/")
            res = conn.getresponse()
            assert res.status == 200
            assert "text/html" in res.getheader("Content-Type")
            body = res.read().decode("utf-8")
            assert "Bruriah" in body
            assert "Test Node" in body
        finally:
            conn.close()

    def test_get_api_dag_returns_json(self, test_server):
        conn = HTTPConnection("127.0.0.1", test_server)
        try:
            conn.request("GET", "/api/dag")
            res = conn.getresponse()
            assert res.status == 200
            assert "application/json" in res.getheader("Content-Type")
            data = json.loads(res.read().decode("utf-8"))
            assert len(data["nodes"]) == 1
            assert data["nodes"][0]["id"] == "doc:1"
        finally:
            conn.close()

    def test_get_404_for_unknown_path(self, test_server):
        conn = HTTPConnection("127.0.0.1", test_server)
        try:
            conn.request("GET", "/unknown-route")
            res = conn.getresponse()
            assert res.status == 404
        finally:
            conn.close()


class TestRunUI:
    def test_run_ui_raises_on_unbuilt_snapshot(self, tmp_path):
        paths_mock = MagicMock()
        with patch("bruriah.platform.open_snapshot") as mock_open:
            from bruriah.platform import PlatformError

            mock_open.side_effect = PlatformError("index_not_built")
            with pytest.raises(UIError) as exc:
                run_ui(paths_mock, port=0, open_browser=False)
            assert exc.value.code == "index_not_built"


class TestUICli:
    def test_ui_cli_parsing(self):
        parser = _build_cli_parser()
        args = parser.parse_args(["ui", "--port", "8080", "--no-browser"])
        assert args.port == 8080
        assert args.no_browser is True

    def test_ui_cli_dispatch_success(self):
        with patch("bruriah.cli.run_ui") as mock_run:
            code = bruriah_main(["ui", "--port", "3000", "--no-browser"])
            assert code == 0
            mock_run.assert_called_once()
            _, kwargs = mock_run.call_args
            assert kwargs["port"] == 3000
            assert kwargs["open_browser"] is False

    def test_ui_cli_dispatch_error(self, capsys):
        with patch("bruriah.cli.run_ui", side_effect=UIError("snapshot_unreadable")):
            code = bruriah_main(["ui", "--no-browser"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: snapshot_unreadable" in captured.err

    def test_build_dag_with_premises_and_drift(self):
        conn = _setup_test_db()
        try:
            conn.executescript("""
                CREATE TABLE premises (
                    premise_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    invalidated_by TEXT,
                    rationale TEXT,
                    document_ref TEXT NOT NULL,
                    invalidation_document_ref TEXT
                ) WITHOUT ROWID;

                CREATE TABLE alternatives (
                    name TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    premises TEXT NOT NULL,
                    document_ref TEXT NOT NULL,
                    PRIMARY KEY (name, document_ref)
                ) WITHOUT ROWID;
            """)
            conn.execute(
                "INSERT INTO premises VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("single-tenant", "System runs single tenant", "invalidated", "doc:2", "Moved to multi-tenant", "doc:1", "doc:2"),
            )
            conn.execute(
                "INSERT INTO alternatives VALUES (?, ?, ?, ?, ?)",
                ("BasicAuth", "rejected", "Insecure over plain HTTP", json.dumps(["single-tenant"]), "doc:1"),
            )
            dag = build_dag_from_database(conn)
            node1 = next(n for n in dag.nodes if n.id == "doc:1")
            assert node1.has_drift is True
            assert len(node1.premises) == 1
            assert node1.premises[0]["id"] == "single-tenant"
            assert node1.premises[0]["status"] == "invalidated"
            assert len(node1.alternatives) == 1
            assert node1.alternatives[0]["name"] == "BasicAuth"
        finally:
            conn.close()

