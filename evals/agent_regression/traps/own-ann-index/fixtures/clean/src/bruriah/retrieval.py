# Snapshot-bound local retrieval: bounded BM25 + vector legs fused with RRF (Slice 6A).
# The candidate schema has no FTS5 table and the snapshot is opened read-only/immutable,
# so lexical scoring is a bounded pure-Python BM25 scan instead of SQLite FTS5.
from __future__ import annotations

import sqlite3
from collections.abc import Callable

from . import ranking

# Partitions scored per query. The vector leg reads the centroids plus the passages of this many
# partitions, so its cost follows the partition size rather than the corpus size.
_PROBE_PARTITIONS = 8


def _nearest_partitions(
    database: sqlite3.Connection, query_vector: bytes, deadline: float, clock: Callable[[], float]
) -> list[int]:
    centroids = [
        (str(partition), centroid)
        for partition, centroid in database.execute("SELECT partition, centroid FROM vector_partitions")
    ]
    ranks, _ = ranking.vector_ranks(items=centroids, query_vector=query_vector, deadline=deadline, clock=clock)
    ordered = sorted((ranks or {}).items(), key=lambda item: item[1])
    return [int(partition) for partition, _ in ordered[:_PROBE_PARTITIONS]]


def _scan_vectors(database: sqlite3.Connection, partitions: list[int]) -> list[tuple[str, bytes]]:
    placeholders = ", ".join("?" for _ in partitions)
    query = f"SELECT ref, vector FROM passages WHERE partition IN ({placeholders}) ORDER BY ref"
    return list(database.execute(query, partitions))


def _vector_ranks(
    database: sqlite3.Connection,
    query_vector: bytes,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, int] | None, bool]:
    partitions = _nearest_partitions(database, query_vector, deadline, clock)
    return ranking.vector_ranks(
        items=_scan_vectors(database, partitions),
        query_vector=query_vector,
        deadline=deadline,
        clock=clock,
    )
