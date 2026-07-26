from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceMetadata:
    provenance: tuple[str, ...] = ()
    provenance_urls: tuple[str, ...] = ()
    status: str = "unknown"
    verification_date: str = "unknown"


@dataclass(frozen=True)
class Passage:
    ref: str
    document_ref: str
    relative_path: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    text: str
    source_hash: str
    metadata: SourceMetadata


@dataclass(frozen=True)
class Document:
    document_ref: str
    relative_path: str
    source_hash: str
    metadata: SourceMetadata
    passages: tuple[Passage, ...]
