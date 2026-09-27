from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

Embedder = Callable[[list[str]], list[bytes]]
REF_VERSION = "v1"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE documents (
    document_ref TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL,
    source_hash TEXT NOT NULL, metadata TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE passages (
    ref TEXT PRIMARY KEY, document_ref TEXT NOT NULL, relative_path TEXT NOT NULL,
    heading_path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    text TEXT NOT NULL, source_hash TEXT NOT NULL, metadata TEXT NOT NULL,
    -- `search_text` sits before `vector` on purpose: `_stored_document` compares `stored[:-1]`
    -- against the expected tuple and type-checks the last column as the blob, so `vector` has to
    -- stay last for a new column to be compared rather than skipped.
    search_text TEXT NOT NULL, partition INTEGER NOT NULL,
    vector BLOB NOT NULL, FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
CREATE INDEX idx_passages_partition ON passages(partition);
-- Coarse quantizer: one centroid per partition, built from the passage vectors at index time.
-- A query scores the centroids, then only the passages of the nearest few partitions.
CREATE TABLE vector_partitions (partition INTEGER PRIMARY KEY, centroid BLOB NOT NULL);
"""


def _create_candidate(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path)
    database.executescript(SCHEMA)
    return database


def _insert_passages(database: sqlite3.Connection, rows: Sequence[tuple[object, ...]]) -> None:
    database.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _insert_partitions(database: sqlite3.Connection, centroids: Sequence[bytes]) -> None:
    database.executemany("INSERT INTO vector_partitions VALUES (?, ?)", list(enumerate(centroids)))
