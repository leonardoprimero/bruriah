# Deterministic source and capability lookup (Slice 6B-2). Given a `RequestClassification`
# (6B-1) and an already-loaded `Registry` (5A/5B), returns matching sources and capabilities
# as a closed, read-only result. Pure: no I/O, no network, no install, no execution, no
# authentication, no retrieval -- design.md "Routing/retrieval": "registries.py loads
# capabilities/sources; no install, execution, authentication, or popularity-as-integrity."
# The route-or-abstain decision consuming this output is 6B-3, out of scope here.
#
# Matching rules (smallest defensible set for the "Method and tool discovery" scenario,
# no invented scoring/ranking):
#
# 1. Domain-gated sources: a source counts only if its owning pack declares the
#    classification's domain in `DomainPack.domains` -- the same membership test
#    `load_pack(..., domain=...)` already uses ("unsupported_domain" in packs.py). Structural
#    set membership between `Identifier` strings, not a synonym map. Requirement:
#    "Domain-Sensitive Outcomes and Unsupported-Domain Abstention".
#    IMPORTANT -- current state: `classify.Domain` and `DomainPack.domains` are independent
#    vocabularies with an EMPTY intersection today. The only shipped pack declares
#    `domains=["software-research"]`, which no classifier Domain value equals, so source
#    discovery is empty for ALL SEVEN domains (law, accounting, cybersecurity, programming,
#    ux_design, general, unsupported) -- not merely general/unsupported. That is spec-compliant
#    (no approved pack for the domain -> abstain, via 6B-3) and is pinned by
#    `test_real_bundled_pack_resolves_no_domain_sources_for_any_classifier_domain`. Reconciling
#    the two vocabularies belongs to future pack authoring (packs declaring classifier-domain
#    identifiers) or an explicit documented domain contract -- NEVER to a synonym map here.
#
# 2. Jurisdiction is disclosed, never silently collapsed: every domain-applicable source
#    returns a `jurisdiction_applicable` flag -- true for a "GLOBAL" declaration (the
#    bundled-pack convention for jurisdiction-agnostic sources) or an exact match to
#    `classification.jurisdiction`, false otherwise, including when the jurisdiction is
#    "unknown" (which cannot affirmatively match a jurisdiction-specific source). Requirement:
#    "Accounting authority is jurisdiction-sensitive" -- "preserves those refs as
#    non-applicable" rather than hiding them. Exact-string equality; jurisdiction codes are
#    identifiers, not prose, so no NFC/casefold applies (mirrors `classify.py`: jurisdiction
#    "read only from the request's dedicated structured field, never guessed").
#
# 3. Capabilities are claim-type gated, not domain-gated: `CapabilityPolicy` has no domain
#    field, and capability records (tools, libraries, MCP servers) are domain-agnostic
#    infrastructure -- the bundled `research.minimal` pack declares domain "software-research",
#    outside the classifier's closed `Domain` vocabulary, by design. All registry capabilities
#    return when `classification.claim_type == "capability_recommendation"` (6B-1's own
#    comment: capability discovery "is a distinct claim shape from an evidence lookup"); else
#    empty. Targets "Method and tool discovery" even for domain "general" (most tool/library
#    questions carry no domain keyword).
#
# 4. `authority` and source-level `claim_types` are disclosed but never filter a match:
#    `SourcePolicy.claim_types` is open pack-author vocabulary, while
#    `RequestClassification.claim_type` is the classifier's closed three-value enum --
#    conflating them would fabricate a correspondence. Filtering by `authority` would be a
#    trust judgment ("official" over "contextual"), which requirement 2 forbids in discovery.
#
# 5. No ranking: results preserve the registry's own order (`Registry.sources` /
#    `.capabilities` are already pack_id-then-id sorted); nothing here reorders or scores --
#    match is boolean set membership only.
#
# No free text is matched anywhere: every comparison is over closed Literal fields (`domain`,
# `claim_type`) or exact-equality identifier/code strings (`jurisdiction`, `source_id`,
# `capability_id`), never over `task`/`outcome` prose.
from __future__ import annotations

from dataclasses import dataclass

from .classify import RequestClassification
from .packs import CapabilityPolicy, SourcePolicy
from .registries import Registry

_GLOBAL_JURISDICTION = "GLOBAL"


class LookupError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SourceMatch:
    """A domain-applicable registry source paired with jurisdiction applicability. Sources
    with `jurisdiction_applicable=False` are still returned -- never hidden -- per "preserves
    those refs as non-applicable". `source` is the registry's own instance: never cloned."""

    source: SourcePolicy
    jurisdiction_applicable: bool


@dataclass(frozen=True)
class LookupResult:
    """Closed, read-only lookup outcome. `domain_supported=False` means no loaded pack
    declares the classification's domain -- the signal 6B-3's binding constraint needs to
    abstain identically for `general` and `unsupported` when no approved pack exists."""

    domain_supported: bool
    sources: tuple[SourceMatch, ...]
    capabilities: tuple[CapabilityPolicy, ...]


def _domain_applicable_pack_ids(registry: Registry, domain: str) -> frozenset[str]:
    return frozenset(pack.pack_id for pack in registry.packs if domain in pack.domains)


def _pack_id_by_source_id(registry: Registry) -> dict[str, str]:
    return {source.source_id: pack.pack_id for pack in registry.packs for source in pack.sources}


def _jurisdiction_applicable(source: SourcePolicy, jurisdiction: str) -> bool:
    if _GLOBAL_JURISDICTION in source.jurisdictions:
        return True
    if jurisdiction == "unknown":
        return False
    return jurisdiction in source.jurisdictions


def discover(classification: RequestClassification, registry: Registry) -> LookupResult:
    """Look up sources/capabilities `classification` may draw on. Pure and deterministic:
    same (classification, registry) always yields the same `LookupResult`."""
    if not isinstance(classification, RequestClassification):
        raise LookupError("invalid_classification_type")
    if not isinstance(registry, Registry):
        raise LookupError("invalid_registry_type")

    applicable_pack_ids = _domain_applicable_pack_ids(registry, classification.domain)
    domain_supported = bool(applicable_pack_ids)

    pack_id_by_source_id = _pack_id_by_source_id(registry)
    sources = tuple(
        SourceMatch(
            source=source,
            jurisdiction_applicable=_jurisdiction_applicable(source, classification.jurisdiction),
        )
        for source in registry.sources
        if pack_id_by_source_id.get(source.source_id) in applicable_pack_ids
    )

    capabilities: tuple[CapabilityPolicy, ...] = ()
    if classification.claim_type == "capability_recommendation":
        capabilities = registry.capabilities

    return LookupResult(domain_supported=domain_supported, sources=sources, capabilities=capabilities)


def resolve_source(source_id: object, registry: Registry) -> SourcePolicy | None:
    """Resolve `source_id` against the loaded registry. Not present -> `None`, never a
    fabricated or nearest-match record; the registry is the sole source of truth."""
    if not isinstance(source_id, str):
        raise LookupError("invalid_ref_type")
    if not isinstance(registry, Registry):
        raise LookupError("invalid_registry_type")
    for source in registry.sources:
        if source.source_id == source_id:
            return source
    return None


def resolve_capability(capability_id: object, registry: Registry) -> CapabilityPolicy | None:
    """Resolve `capability_id` against the loaded registry. Not present -> `None`, never a
    fabricated or nearest-match record."""
    if not isinstance(capability_id, str):
        raise LookupError("invalid_ref_type")
    if not isinstance(registry, Registry):
        raise LookupError("invalid_registry_type")
    for capability in registry.capabilities:
        if capability.capability_id == capability_id:
            return capability
    return None


__all__ = ["LookupError", "LookupResult", "SourceMatch", "discover", "resolve_capability", "resolve_source"]
