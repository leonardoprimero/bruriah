"""Read-only search adapters over the two live Cerebro engines, normalized to
a common NOTE-LEVEL result shape so `metrics.py` never has to know which
engine (or its chunking scheme) produced a ranking.

Both adapters:
  - open their database strictly read-only (`mode=ro`); neither ever writes,
    reindexes, or promotes anything.
  - return chunk/section-level hits first, then dedup to one entry per note
    (`relative_path`) via `dedup_to_notes`, keeping each note's best (lowest)
    chunk rank and that hit's score/via -- per the eval's note-level
    methodology, comparison never happens at chunk/section granularity.

LEGACY (`cerebro.py` + `cerebro.db`): loaded via `importlib`, exactly like
`scripts/verify_legacy_baseline.py` does, and queried through a fresh
`sqlite3.connect("file:...?mode=ro", uri=True)` connection per call so the
live database is never opened read-write. `legacy.search()` already returns
chunk hits ordered by descending RRF score (best first) with a genuine
numeric `score`.

NEW (`bruriah` active snapshot): `ServiceDeps` (embedding model +
open read-only snapshot) is loaded ONCE in `__init__` and reused across every
`search()` call -- rebuilding it per query would reload the ONNX model each
time. Queried via the exact public sequence the router ships:
`search(deps.snapshot, query, budgets, embed_query=deps.embed_query,
clock=deps.clock)` followed by `to_evidence_records(outcome)`.

FIXED (previously a confirmed bug): `bruriah.platform.load_deps` used
to never wire an `embed_query` callable into `ServiceDeps` (`embed_query`
defaulted to `None`), so the ACTUAL deployed `bruriah serve` process ran
retrieval with `degradation=("vector_leg_unavailable", ...)` on every query --
pure lexical BM25 over the snapshot, no semantic/vector leg at all. This
adapter now builds its deps via `cli.build_serve_deps`, the exact function
`serve` itself calls, which constructs a real query embedder (reusing the
same batch `Embedder` factory `index` builds passage vectors with, verified
fail-closed against the active snapshot's recorded embedding fingerprint/
dimensions) and threads it in. This adapter therefore measures the engine as
it is ACTUALLY deployed today, post-fix: hybrid BM25 + vector retrieval, not
the BM25-only behavior the bug used to produce.

Because `EvidenceRecord` (the router's public evidence contract) carries no
numeric score by design (`retrieval.py`: "this slice's conservative
'unknown' -- never inferred from rank"), the router adapter reconstructs a
comparable RRF score itself from the public `RetrievalMatch.lexical_rank`/
`vector_rank` fields, using the identical RRF formula and `k=60` constant
`retrieval.py` uses internally (also legacy's own default `rrf_k`), so
scores are on the same footing across both engines for the abstention
separation metric.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import sqlite_vec

ROOT = Path(__file__).resolve().parents[2]
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.contracts import Budgets  # noqa: E402
from bruriah.platform import PlatformPaths, resolve_paths  # noqa: E402
from bruriah.retrieval import search as router_search  # noqa: E402
from bruriah.retrieval import to_evidence_records  # noqa: E402
from bruriah.service import ServiceDeps  # noqa: E402

LEGACY_DB_PATH = ROOT / "cerebro.db"
LEGACY_MODULE_PATH = ROOT / "cerebro.py"
DEFAULT_RAW_POOL = 40


@dataclass(frozen=True)
class SearchResult:
    """One NOTE-level result: the best chunk hit for `relative_path`,
    collapsed from possibly many chunk/section hits for the same note."""

    relative_path: str
    score: float
    via: str
    rank: int


@dataclass(frozen=True)
class ChunkHit:
    """One raw chunk/section-level hit, before note-level dedup."""

    relative_path: str
    score: float
    via: str


def dedup_to_notes(chunk_hits: Sequence[ChunkHit], k: int) -> list[SearchResult]:
    """Collapse chunk-level hits to one entry per note.

    Keeps each note's best (lowest) chunk rank and that hit's score/via.
    Requires `chunk_hits` to already be ordered best-first (ascending chunk
    rank) -- the dedup keeps the FIRST occurrence of each `relative_path`,
    which is only correct under that precondition. Both adapters below
    satisfy it: `legacy.search()` returns results sorted by descending RRF
    score, and `outcome.matches` is already fused-sorted by `retrieval.py`.
    """
    seen: dict[str, ChunkHit] = {}
    order: list[str] = []
    for hit in chunk_hits:
        if hit.relative_path not in seen:
            seen[hit.relative_path] = hit
            order.append(hit.relative_path)
    return [
        SearchResult(relative_path=path, score=seen[path].score, via=seen[path].via, rank=rank)
        for rank, path in enumerate(order[:k], start=1)
    ]


def _load_legacy_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("legacy_cerebro_eval", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load legacy module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LegacyAdapter:
    """Queries the live legacy engine (`cerebro.py` + `cerebro.db`),
    strictly read-only. Each `search()` call opens and closes its own
    read-only connection -- legacy's `search()` is already cheap and stateless
    per call (unlike the router's embedding model, there is no expensive
    per-instance state worth caching beyond the loaded module and the
    fastembed model it lazily caches internally)."""

    def __init__(
        self,
        db_path: Path = LEGACY_DB_PATH,
        module_path: Path = LEGACY_MODULE_PATH,
        raw_pool: int = DEFAULT_RAW_POOL,
    ) -> None:
        self._db_path = db_path
        self._legacy = _load_legacy_module(module_path)
        self._raw_pool = raw_pool

    def search(self, query: str, k: int = 10) -> list[SearchResult]:
        database = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        database.enable_load_extension(True)
        sqlite_vec.load(database)
        database.enable_load_extension(False)
        try:
            hits = self._legacy.search(database, query, k=self._raw_pool)
        finally:
            database.close()
        chunk_hits = [ChunkHit(relative_path=hit["file"], score=hit["score"], via=hit["via"]) for hit in hits]
        return dedup_to_notes(chunk_hits, k)


class RouterAdapter:
    """Queries the new `bruriah` engine against its currently active
    snapshot, strictly read-only. Loads `ServiceDeps` (embedding-model
    metadata + open snapshot, INCLUDING a real query embedder) ONCE and
    reuses it across every `search()` call, matching the task's exact real
    usage pattern: `cli.build_serve_deps(resolve_paths())` -- the identical
    function `bruriah serve` itself calls -- then
    `search(deps.snapshot, query, budgets, embed_query=deps.embed_query,
    clock=deps.clock)`.
    """

    _RRF_K = 60  # Matches retrieval.py's internal `_RRF_K` and legacy's default `rrf_k`.

    def __init__(self, paths: PlatformPaths | None = None, raw_pool: int = DEFAULT_RAW_POOL) -> None:
        self._deps: ServiceDeps = build_serve_deps(paths if paths is not None else resolve_paths())
        self._raw_pool = raw_pool

    def search(self, query: str, k: int = 10) -> list[SearchResult]:
        outcome = router_search(
            self._deps.snapshot,
            query,
            Budgets(max_candidates=self._raw_pool),
            embed_query=self._deps.embed_query,
            clock=self._deps.clock,
        )
        # Round-trips every match through the router's real public evidence
        # contract (locator/citation_locator/ref) -- validates the adapter is
        # exercising the shape callers actually receive, even though
        # relative_path/via/score below are sourced from the richer
        # `outcome.matches` (same order, same length as `records`) because
        # `EvidenceRecord` itself carries no numeric score.
        records = to_evidence_records(outcome)
        assert len(records) == len(outcome.matches)
        chunk_hits = [
            ChunkHit(
                relative_path=match.relative_path,
                score=self._rrf_score(match.lexical_rank, match.vector_rank),
                via=self._via(match.lexical_rank, match.vector_rank),
            )
            for match in outcome.matches
        ]
        return dedup_to_notes(chunk_hits, k)

    @property
    def degradation_supported(self) -> bool:
        """True if this deps construction wired a vector-search callable.
        Now `True`: `build_serve_deps` (post-fix) always constructs and
        injects a real query embedder -- see the module docstring."""
        return self._deps.embed_query is not None

    @property
    def build_id(self) -> str:
        """The active snapshot's build id, for stamping reports."""
        return self._deps.snapshot.build_id

    @classmethod
    def _rrf_score(cls, lexical_rank: int | None, vector_rank: int | None) -> float:
        score = 0.0
        if lexical_rank is not None:
            score += 1.0 / (cls._RRF_K + lexical_rank)
        if vector_rank is not None:
            score += 1.0 / (cls._RRF_K + vector_rank)
        return score

    @staticmethod
    def _via(lexical_rank: int | None, vector_rank: int | None) -> str:
        if lexical_rank is not None and vector_rank is not None:
            return "ambos"
        if lexical_rank is not None:
            return "bm25"
        return "vector"

    def close(self) -> None:
        """Release the open read-only snapshot connection."""
        self._deps.snapshot.database.close()


__all__ = [
    "ChunkHit",
    "DEFAULT_RAW_POOL",
    "LEGACY_DB_PATH",
    "LEGACY_MODULE_PATH",
    "LegacyAdapter",
    "RouterAdapter",
    "SearchResult",
    "dedup_to_notes",
]
