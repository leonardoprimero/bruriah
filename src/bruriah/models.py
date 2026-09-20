from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceMetadata:
    provenance: tuple[str, ...] = ()
    provenance_urls: tuple[str, ...] = ()
    status: str = "unknown"
    verification_date: str = "unknown"
    supersedes: tuple[str, ...] = ()
    deprecates: tuple[str, ...] = ()
    amends: tuple[str, ...] = ()
    commit: str | None = None
    alternatives: tuple[dict[str, Any], ...] = ()
    premises: tuple[dict[str, Any], ...] = ()
    invalidated_premises: tuple[str, ...] = ()


@dataclass(frozen=True)
class Passage:
    ref: str
    document_ref: str
    relative_path: str
    heading_path: tuple[str, ...]
    start_line: int
    end_line: int
    text: str
    # The two are deliberately separate, and the separation is the whole point. `text` is the
    # section's exact bytes: it is what `read_evidence` slices by character offset and hands back
    # as quoted evidence, so anything prepended to it would shift every offset a caller holds.
    # `search_text` is the same section under its ancestry, and is what the retrieval stages read.
    # See `corpus._search_text` for why a passage needs the ancestry it does not contain.
    search_text: str
    source_hash: str
    metadata: SourceMetadata


@dataclass(frozen=True)
class Document:
    document_ref: str
    relative_path: str
    source_hash: str
    metadata: SourceMetadata
    passages: tuple[Passage, ...]
