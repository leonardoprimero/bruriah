from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


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
    # Trust tier for premise-conflict resolution (`index._build_premise_and_alternative_records`):
    # "repository" for anything discovered from the corpus tree or derived from git history,
    # "github" only for a document `github_corpus.build_documents` generated from an issue or pull
    # request -- text an attacker who can open one chooses, not text the repository owner wrote.
    # Set from the `bruriah_source` frontmatter key, which only `github_corpus._render_document`
    # writes; never inferred from a document's path or file name, both of which a repository
    # document is free to reuse.
    source: Literal["repository", "github"] = "repository"
    # The GitHub issue/PR number a `source: "github"` document was generated from, carried for the
    # deterministic "lowest number wins" tie-break between two GitHub documents that redeclare the
    # same premise id. `None` for a repository document, where it plays no role.
    github_issue: int | None = None

    def __post_init__(self) -> None:
        # `Literal["repository", "github"]` is a static-only guarantee: nothing stops a caller
        # (untyped code, a `**kwargs` construction, a future frontmatter-driven value) from
        # actually passing a third string at runtime. `index._build_premise_and_alternative_
        # records` branches on `tier == "repository"`/`else` (the GitHub-tier path), so a bogus
        # tier would silently fall into the GitHub, lower-trust branch -- the tier must fail
        # closed here instead, the same `BuildConfig.__post_init__` pattern this module's sibling
        # dataclass already uses.
        if self.source not in ("repository", "github"):
            raise ValueError("invalid_source_tier")


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
