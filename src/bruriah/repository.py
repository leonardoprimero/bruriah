# Snapshot repository layer: encapsulates all SQLite queries against the active
# snapshot database (passages, documents, lineage).
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from . import language, ranking
from .corpus import alternative_ref_for, premise_ref_for


class RepositoryError(Exception):
    """Raised when repository data access fails."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PassageSummary:
    ref: str
    relative_path: str
    start_line: int
    end_line: int
    source_hash: str
    document_ref: str = ""


@dataclass(frozen=True)
class PassageContent:
    relative_path: str
    start_line: int
    end_line: int
    text: str
    source_hash: str


@dataclass(frozen=True)
class PassageRecord:
    ref: str
    document_ref: str
    relative_path: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    text: str
    search_text: str
    source_hash: str
    vector: bytes


@dataclass(frozen=True)
class LineageRelation:
    successor_ref: str
    predecessor_target: str
    predecessor_ref: str
    relation: str


@dataclass(frozen=True)
class AlternativeRow:
    name: str
    disposition: str
    reason: str
    premises: tuple[str, ...]
    document_ref: str


@dataclass(frozen=True)
class PremiseRow:
    premise_id: str
    statement: str
    status: str
    invalidated_by: str | None
    rationale: str | None
    document_ref: str
    invalidation_document_ref: str | None = None


def parse_heading_path(raw: str) -> tuple[str, ...]:
    """Parse JSON heading path array from database into a tuple of strings."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RepositoryError("corrupt_snapshot_metadata") from error
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise RepositoryError("corrupt_snapshot_metadata")
    return tuple(parsed)


class SnapshotRepository:
    """Encapsulates data access and queries for an active SQLite snapshot."""

    def __init__(self, database: sqlite3.Connection):
        self._db = database

    @property
    def database(self) -> sqlite3.Connection:
        return self._db

    def has_lexical_index(self) -> bool:
        """Check whether precomputed lexical index tables exist."""
        try:
            row = self._db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('corpus_stats', 'term_df', 'term_postings')"
            ).fetchone()
            return bool(row and row[0] == 3)
        except sqlite3.Error:
            return False

    def scan_passages(
        self,
        deadline: float,
        clock: Callable[[], float],
        is_expired: Callable[[int, float, Callable[[], float]], bool] = ranking.default_expired,
    ) -> tuple[list[PassageRecord], bool]:
        """Scan all passages ordered by ref, bounded by deadline."""
        try:
            rows = self._db.execute(
                "SELECT ref, document_ref, relative_path, heading_path, start_line, end_line, "
                "text, search_text, source_hash, vector FROM passages ORDER BY ref"
            )
            passages: list[PassageRecord] = []
            stopped = False
            for position, row in enumerate(rows):
                if is_expired(position, deadline, clock):
                    stopped = True
                    break
                (
                    ref, document_ref, relative_path, heading_json, start_line, end_line, text,
                    search_text, source_hash, vector,
                ) = row
                passages.append(
                    PassageRecord(
                        ref=ref,
                        document_ref=document_ref,
                        relative_path=relative_path,
                        heading_path=parse_heading_path(heading_json),
                        start_line=start_line,
                        end_line=end_line,
                        text=text,
                        search_text=search_text,
                        source_hash=source_hash,
                        vector=vector,
                    )
                )
            return passages, stopped
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def scan_vectors(
        self,
        deadline: float,
        clock: Callable[[], float],
        is_expired: Callable[[int, float, Callable[[], float]], bool] = ranking.default_expired,
    ) -> tuple[list[tuple[str, bytes]], bool]:
        """Scan candidate vectors (ref, vector) ordered by ref, bounded by deadline."""
        try:
            rows = self._db.execute("SELECT ref, vector FROM passages ORDER BY ref")
            vectors: list[tuple[str, bytes]] = []
            stopped = False
            for position, (ref, vector) in enumerate(rows):
                if is_expired(position, deadline, clock):
                    stopped = True
                    break
                vectors.append((ref, vector))
            return vectors, stopped
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def hydrate_passages(self, refs: Sequence[str]) -> dict[str, PassageRecord]:
        """Hydrate full PassageRecord for specific refs."""
        if not refs:
            return {}
        placeholders = ", ".join("?" for _ in refs)
        try:
            rows = self._db.execute(
                f"SELECT ref, document_ref, relative_path, heading_path, start_line, end_line, "
                f"text, search_text, source_hash, vector FROM passages WHERE ref IN ({placeholders})",
                list(refs),
            )
            hydrated: dict[str, PassageRecord] = {}
            for row in rows:
                (
                    ref, document_ref, relative_path, heading_json, start_line, end_line, text,
                    search_text, source_hash, vector,
                ) = row
                hydrated[ref] = PassageRecord(
                    ref=ref,
                    document_ref=document_ref,
                    relative_path=relative_path,
                    heading_path=parse_heading_path(heading_json),
                    start_line=start_line,
                    end_line=end_line,
                    text=text,
                    search_text=search_text,
                    source_hash=source_hash,
                    vector=vector,
                )
            return hydrated
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def get_corpus_stats(self) -> tuple[int, float]:
        """Fetch total_documents and average_length from corpus_stats."""
        try:
            stats_rows = self._db.execute("SELECT key, num_value FROM corpus_stats").fetchall()
            stats = {key: num_val for key, num_val in stats_rows}
            total_documents = int(stats.get("total_documents", 0.0))
            average_length = float(stats.get("average_length", 0.0))
            return total_documents, average_length
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def get_term_dfs(self, terms: Iterable[str]) -> dict[str, int]:
        """Fetch document frequencies for terms."""
        if not terms:
            return {}
        unique_terms = sorted(set(terms))
        placeholders = ", ".join("?" for _ in unique_terms)
        try:
            rows = self._db.execute(
                f"SELECT term, df FROM term_df WHERE term IN ({placeholders})",
                unique_terms,
            ).fetchall()
            return dict(rows)
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def iter_term_postings(self, terms: Iterable[str]) -> Iterable[tuple[str, str, int, int]]:
        """Fetch postings (term, ref, freq, doc_length) ordered by term, ref."""
        if not terms:
            return []
        query_terms = sorted(set(terms))
        placeholders = ", ".join("?" for _ in query_terms)
        try:
            return self._db.execute(
                f"SELECT term, ref, freq, doc_length FROM term_postings WHERE term IN ({placeholders}) ORDER BY term, ref",
                query_terms,
            )
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def detect_corpus_language(self, passages: Sequence[PassageRecord] | None = None) -> str | None:
        """Detect dominant language of the corpus from precomputed stats or sample passages."""
        try:
            if self.has_lexical_index():
                row = self._db.execute(
                    "SELECT str_value FROM corpus_stats WHERE key = 'corpus_language'"
                ).fetchone()
                if row is not None and row[0] is not None:
                    return row[0]
            if passages is not None:
                return language.dominant(
                    passage.search_text[:400]
                    for passage in passages[:64]
                )
            sample_rows = self._db.execute(
                "SELECT search_text FROM passages ORDER BY ref LIMIT 64"
            ).fetchall()
            return language.dominant(text[:400] for text, in sample_rows)
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error

    def has_lineage_table(self) -> bool:
        try:
            row = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lineage'"
            ).fetchone()
            return row is not None
        except sqlite3.DatabaseError:
            return False

    def get_passage_content(self, ref: str) -> PassageContent | None:
        """Fetch passage content by ref. Raises RepositoryError if database is unreadable."""
        try:
            row = self._db.execute(
                "SELECT relative_path, start_line, end_line, text, source_hash FROM passages WHERE ref = ?",
                (ref,),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise RepositoryError("snapshot_unreadable") from error
        if row is None:
            return None
        return PassageContent(
            relative_path=row[0],
            start_line=row[1],
            end_line=row[2],
            text=row[3],
            source_hash=row[4],
        )

    def get_passages_meta_by_refs(self, refs: list[str]) -> dict[str, tuple[str, str]]:
        """Map passage refs to (document_ref, relative_path)."""
        if not refs:
            return {}
        placeholders = ",".join("?" for _ in refs)
        try:
            rows = self._db.execute(
                f"SELECT ref, document_ref, relative_path FROM passages WHERE ref IN ({placeholders})",
                refs,
            ).fetchall()
            return {r[0]: (r[1], r[2]) for r in rows}
        except sqlite3.DatabaseError:
            return {}

    def get_lineage_relations(self, doc_refs: list[str]) -> list[LineageRelation]:
        """Fetch lineage relations where any of the document refs is predecessor or successor."""
        if not doc_refs:
            return []
        placeholders = ",".join("?" for _ in doc_refs)
        try:
            rows = self._db.execute(
                f"SELECT successor_ref, predecessor_target, predecessor_ref, relation FROM lineage "
                f"WHERE predecessor_ref IN ({placeholders}) OR successor_ref IN ({placeholders})",
                [*doc_refs, *doc_refs],
            ).fetchall()
            return [
                LineageRelation(
                    successor_ref=r[0],
                    predecessor_target=r[1],
                    predecessor_ref=r[2],
                    relation=r[3],
                )
                for r in rows
            ]
        except sqlite3.DatabaseError:
            return []

    def get_document_path(self, doc_ref: str) -> str | None:
        """Fetch document relative_path by document_ref."""
        try:
            row = self._db.execute(
                "SELECT relative_path FROM documents WHERE document_ref = ?", (doc_ref,)
            ).fetchone()
            return row[0] if row else None
        except sqlite3.DatabaseError:
            return None

    def get_passages_by_document(
        self, doc_ref: str, limit: int | None = None
    ) -> list[PassageSummary]:
        """Fetch passages belonging to a document ordered by start_line."""
        query = (
            "SELECT ref, relative_path, start_line, end_line, source_hash "
            "FROM passages WHERE document_ref = ? ORDER BY start_line"
        )
        params: list[Any] = [doc_ref]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        try:
            rows = self._db.execute(query, params).fetchall()
            return [
                PassageSummary(
                    ref=r[0],
                    relative_path=r[1],
                    start_line=r[2],
                    end_line=r[3],
                    source_hash=r[4],
                    document_ref=doc_ref,
                )
                for r in rows
            ]
        except sqlite3.DatabaseError as error:
            raise RepositoryError("passages_unreadable") from error

    def has_counterfactual_tables(self) -> bool:
        try:
            row = self._db.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('alternatives', 'premises')"
            ).fetchone()
            return bool(row and row[0] == 2)
        except sqlite3.DatabaseError:
            return False

    def get_alternatives(self) -> list[AlternativeRow]:
        if not self.has_counterfactual_tables():
            return []
        try:
            rows = self._db.execute(
                "SELECT name, disposition, reason, premises_json, document_ref FROM alternatives"
            ).fetchall()
            result: list[AlternativeRow] = []
            for r in rows:
                try:
                    p_list = json.loads(r[3])
                    if not isinstance(p_list, list):
                        p_list = []
                except Exception:
                    p_list = []
                result.append(
                    AlternativeRow(
                        name=r[0],
                        disposition=r[1],
                        reason=r[2],
                        premises=tuple(str(p) for p in p_list),
                        document_ref=r[4],
                    )
                )
            return result
        except sqlite3.DatabaseError as error:
            raise RepositoryError("alternatives_unreadable") from error

    def get_premises(self) -> dict[str, PremiseRow]:
        if not self.has_counterfactual_tables():
            return {}
        try:
            rows = self._db.execute(
                "SELECT premise_id, statement, status, invalidated_by, rationale, document_ref, invalidation_document_ref FROM premises"
            ).fetchall()
            return {
                r[0]: PremiseRow(
                    premise_id=r[0],
                    statement=r[1],
                    status=r[2],
                    invalidated_by=r[3],
                    rationale=r[4],
                    document_ref=r[5],
                    invalidation_document_ref=r[6],
                )
                for r in rows
            }
        except sqlite3.DatabaseError as error:
            raise RepositoryError("premises_unreadable") from error

    def resolve_alternative_ref(self, ref: str) -> AlternativeRow | None:
        """Resolve an opaque `alt:v1:<hash>` ref back to its stored row, for a human view or an
        explicit `read_evidence` request (T3/T4, investigate-boundary-v2) -- never carried by the
        wire contract itself. The ref is RECOMPUTED over each row with `alternative_ref_for`
        (the same formula `_evaluate_counterfactual` used to mint it), not stored: no index
        migration, no new column. `None` for a ref that does not resolve, exactly like
        `get_document_path`."""
        for row in self.get_alternatives():
            if alternative_ref_for(row.document_ref, row.name) == ref:
                return row
        return None

    def resolve_premise_ref(self, ref: str) -> PremiseRow | None:
        """Resolve an opaque `premise:v1:<hash>` ref back to its stored row -- the premise
        analogue of `resolve_alternative_ref`. `premise_id` is the table's own PRIMARY KEY, so
        the ref is unique across every document without a document_ref component."""
        for row in self.get_premises().values():
            if premise_ref_for(row.premise_id) == ref:
                return row
        return None
