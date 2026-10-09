# Snapshot-bound local retrieval: bounded BM25 + vector legs fused with RRF (Slice 6A).
# The candidate schema has no FTS5 table and the snapshot is opened read-only/immutable,
# so lexical scoring is a bounded pure-Python BM25 scan instead of SQLite FTS5.
from __future__ import annotations

import sqlite3
from collections.abc import Callable

import sqlite_vec

from . import ranking

# Candidates the vec0 index returns per query; everything past it is never ranked.
_KNN_CANDIDATES = 200


def _vector_ranks(
    database: sqlite3.Connection,
    query_vector: bytes,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, int] | None, bool]:
    database.enable_load_extension(True)
    sqlite_vec.load(database)
    database.enable_load_extension(False)
    rows = database.execute(
        "SELECT ref, distance FROM passage_vectors WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (query_vector, _KNN_CANDIDATES),
    )
    return ranking.ranked([(-distance, ref) for ref, distance in rows]), clock() >= deadline
