from __future__ import annotations

import sqlite3
import pytest

from bruriah.repository import (
    PassageRecord,
    PassageSummary,
    RepositoryError,
    SnapshotRepository,
    parse_heading_path,
)


@pytest.fixture
def memory_db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def test_parse_heading_path_valid():
    assert parse_heading_path('["Intro", "Section 1"]') == ("Intro", "Section 1")
    assert parse_heading_path("[]") == ()


@pytest.mark.parametrize("invalid", ['"string"', "123", "null", '{"key": "val"}', "[1, 2]", "[[]]"])
def test_parse_heading_path_invalid(invalid: str):
    with pytest.raises(RepositoryError) as exc_info:
        parse_heading_path(invalid)
    assert exc_info.value.code == "corrupt_snapshot_metadata"


def test_has_lexical_index(memory_db: sqlite3.Connection):
    repo = SnapshotRepository(memory_db)
    assert not repo.has_lexical_index()

    memory_db.execute("CREATE TABLE term_df (term TEXT, df INT)")
    assert not repo.has_lexical_index()

    memory_db.execute("CREATE TABLE corpus_stats (key TEXT, num_value REAL, str_value TEXT)")
    memory_db.execute("CREATE TABLE term_postings (term TEXT, ref TEXT, freq INT, doc_length INT)")
    assert repo.has_lexical_index()


def test_scan_and_hydrate_passages(memory_db: sqlite3.Connection):
    memory_db.execute(
        "CREATE TABLE passages (ref TEXT, document_ref TEXT, relative_path TEXT, heading_path TEXT, "
        "start_line INT, end_line INT, text TEXT, search_text TEXT, source_hash TEXT, vector BLOB)"
    )
    memory_db.execute(
        "INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("p1", "doc1", "a.md", '["H1"]', 1, 10, "hello world", "hello world", "hash1", b"\x00" * 12),
    )
    memory_db.execute(
        "INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("p2", "doc2", "b.md", '["H2"]', 1, 5, "another text", "another text", "hash2", b"\x01" * 12),
    )

    repo = SnapshotRepository(memory_db)
    passages, stopped = repo.scan_passages(deadline=100.0, clock=lambda: 0.0)
    assert not stopped
    assert len(passages) == 2
    assert passages[0].ref == "p1"
    assert passages[0].heading_path == ("H1",)

    hydrated = repo.hydrate_passages(["p2"])
    assert "p2" in hydrated
    assert hydrated["p2"].relative_path == "b.md"
    assert hydrated["p2"].heading_path == ("H2",)

    assert repo.hydrate_passages([]) == {}


def test_scan_vectors(memory_db: sqlite3.Connection):
    memory_db.execute(
        "CREATE TABLE passages (ref TEXT, document_ref TEXT, relative_path TEXT, heading_path TEXT, "
        "start_line INT, end_line INT, text TEXT, search_text TEXT, source_hash TEXT, vector BLOB)"
    )
    memory_db.execute(
        "INSERT INTO passages VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("p1", "doc1", "a.md", '["H1"]', 1, 10, "text1", "text1", "hash1", b"vector1"),
    )
    repo = SnapshotRepository(memory_db)
    vectors, stopped = repo.scan_vectors(deadline=100.0, clock=lambda: 0.0)
    assert not stopped
    assert len(vectors) == 1
    assert vectors[0] == ("p1", b"vector1")


def test_corpus_stats_and_terms(memory_db: sqlite3.Connection):
    memory_db.execute("CREATE TABLE corpus_stats (key TEXT, num_value REAL, str_value TEXT)")
    memory_db.execute("INSERT INTO corpus_stats VALUES ('total_documents', 10.0, NULL)")
    memory_db.execute("INSERT INTO corpus_stats VALUES ('average_length', 15.5, NULL)")
    memory_db.execute("INSERT INTO corpus_stats VALUES ('corpus_language', NULL, 'es')")

    memory_db.execute("CREATE TABLE term_df (term TEXT, df INT)")
    memory_db.execute("INSERT INTO term_df VALUES ('apple', 3)")
    memory_db.execute("INSERT INTO term_df VALUES ('banana', 5)")

    memory_db.execute("CREATE TABLE term_postings (term TEXT, ref TEXT, freq INT, doc_length INT)")
    memory_db.execute("INSERT INTO term_postings VALUES ('apple', 'p1', 2, 10)")
    memory_db.execute("INSERT INTO term_postings VALUES ('apple', 'p2', 1, 20)")

    repo = SnapshotRepository(memory_db)
    total_docs, avg_len = repo.get_corpus_stats()
    assert total_docs == 10
    assert avg_len == 15.5

    assert repo.detect_corpus_language() == "es"

    dfs = repo.get_term_dfs(["apple", "banana", "cherry"])
    assert dfs == {"apple": 3, "banana": 5}
    assert repo.get_term_dfs([]) == {}

    postings = list(repo.iter_term_postings(["apple"]))
    assert len(postings) == 2
    assert postings[0] == ("apple", "p1", 2, 10)
    assert postings[1] == ("apple", "p2", 1, 20)
    assert list(repo.iter_term_postings([])) == []


def test_document_and_lineage_queries(memory_db: sqlite3.Connection):
    memory_db.execute("CREATE TABLE documents (document_ref TEXT, relative_path TEXT)")
    memory_db.execute("INSERT INTO documents VALUES ('doc:1', 'guide/intro.md')")

    memory_db.execute(
        "CREATE TABLE lineage (successor_ref TEXT, predecessor_target TEXT, predecessor_ref TEXT, relation TEXT)"
    )
    memory_db.execute("INSERT INTO lineage VALUES ('doc:2', 'doc:1', 'doc:1', 'revises')")

    memory_db.execute(
        "CREATE TABLE passages (ref TEXT, document_ref TEXT, relative_path TEXT, heading_path TEXT, "
        "start_line INT, end_line INT, text TEXT, search_text TEXT, source_hash TEXT, vector BLOB)"
    )
    memory_db.execute(
        "INSERT INTO passages VALUES ('p2', 'doc:1', 'guide/intro.md', '[]', 20, 30, 't2', 't2', 'h2', x'')"
    )
    memory_db.execute(
        "INSERT INTO passages VALUES ('p1', 'doc:1', 'guide/intro.md', '[]', 1, 10, 't1', 't1', 'h1', x'')"
    )

    repo = SnapshotRepository(memory_db)
    assert repo.get_document_path("doc:1") == "guide/intro.md"
    assert repo.get_document_path("doc:unknown") is None

    lineage = repo.get_lineage_relations(["doc:1"])
    assert len(lineage) == 1
    assert lineage[0].relation == "revises"
    assert repo.get_lineage_relations([]) == []

    passages = repo.get_passages_by_document("doc:1")
    assert len(passages) == 2
    assert passages[0].ref == "p1"  # ordered by start_line
    assert passages[1].ref == "p2"

    limited = repo.get_passages_by_document("doc:1", limit=1)
    assert len(limited) == 1
    assert limited[0].ref == "p1"


def test_repository_error_handling(memory_db: sqlite3.Connection):
    # Missing tables should trigger RepositoryError
    repo = SnapshotRepository(memory_db)
    with pytest.raises(RepositoryError) as exc:
        repo.get_corpus_stats()
    assert exc.value.code == "snapshot_unreadable"

    with pytest.raises(RepositoryError) as exc:
        repo.get_term_dfs(["word"])
    assert exc.value.code == "snapshot_unreadable"

    with pytest.raises(RepositoryError) as exc:
        repo.scan_passages(deadline=100.0, clock=lambda: 0.0)
    assert exc.value.code == "snapshot_unreadable"

    with pytest.raises(RepositoryError) as exc:
        repo.scan_vectors(deadline=100.0, clock=lambda: 0.0)
    assert exc.value.code == "snapshot_unreadable"

    with pytest.raises(RepositoryError) as exc:
        repo.hydrate_passages(["p1"])
    assert exc.value.code == "snapshot_unreadable"
