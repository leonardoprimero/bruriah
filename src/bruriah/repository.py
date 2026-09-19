# Snapshot repository layer: encapsulates all SQLite queries against the active
# snapshot database (passages, documents, lineage).
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any


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
class LineageRelation:
    successor_ref: str
    predecessor_target: str
    predecessor_ref: str
    relation: str


class SnapshotRepository:
    """Encapsulates data access and queries for an active SQLite snapshot."""

    def __init__(self, database: sqlite3.Connection):
        self._db = database

    @property
    def database(self) -> sqlite3.Connection:
        return self._db

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
