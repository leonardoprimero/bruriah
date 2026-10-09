from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

import sqlite_vec

Embedder = Callable[[list[str]], list[bytes]]
REF_VERSION = "v1"
EMBEDDING_DIMENSIONS = 384

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
    search_text TEXT NOT NULL,
    vector BLOB NOT NULL, FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
"""

# The nearest-neighbour table the vector leg queries instead of scanning every passage blob.
VECTOR_SCHEMA = f"""
CREATE VIRTUAL TABLE passage_vectors USING vec0(
    ref TEXT PRIMARY KEY,
    embedding float[{EMBEDDING_DIMENSIONS}] distance_metric=cosine
);
"""


def _create_candidate(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(path)
    database.enable_load_extension(True)
    sqlite_vec.load(database)
    database.enable_load_extension(False)
    database.executescript(SCHEMA)
    database.executescript(VECTOR_SCHEMA)
    return database


def _insert_passages(database: sqlite3.Connection, rows: Sequence[tuple[object, ...]]) -> None:
    database.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    database.executemany(
        "INSERT INTO passage_vectors (ref, embedding) VALUES (?, ?)", [(row[0], row[-1]) for row in rows]
    )
