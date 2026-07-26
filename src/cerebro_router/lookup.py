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
#    RECONCILED 2026-07-25 by pack authoring, exactly as this note originally required. The
#    `research.minimal` pack still declares `domains=["software-research"]`, which no classifier
#    Domain value equals; the bundled `programming.minimal` pack declares `domains=["programming"]`
#    and is what makes discovery resolve at all. Today `programming` is supported and the other six
#    (law, accounting, cybersecurity, ux_design, general, unsupported) still abstain, pinned as an
#    EXACT SET by `test_platform.py::test_the_bundled_registry_reconciles_exactly_one_classifier_domain`
#    so widening a pack's domains fails a test rather than silently broadening routing. Further
#    domains are added the same way -- by authoring a pack -- and NEVER by a synonym map here.
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
from .skills import SkillPack, SkillPolicy, SkillSet

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
class SkillMatch:
    """A skill whose declared domains include the classification's domain.

    Carries the owning PACK, not just its id: provenance and the review window both live there, and
    dispatch has to evaluate currency without searching for the pack a second time. `pack_id` stays
    available as a property so callers that only need the name read the same as before. Both `skill`
    and `pack` are the loaded set's own instances, never cloned."""

    skill: SkillPolicy
    pack: SkillPack

    @property
    def pack_id(self) -> str:
        return self.pack.pack_id


@dataclass(frozen=True)
class LookupResult:
    """Closed, read-only lookup outcome. `domain_supported=False` means no loaded pack
    declares the classification's domain -- the signal 6B-3's binding constraint needs to
    abstain identically for `general` and `unsupported` when no approved pack exists.

    `skills` defaults to empty so every existing caller keeps its exact current behaviour: a caller
    that passes no skill set is byte-identical to before this field existed."""

    domain_supported: bool
    sources: tuple[SourceMatch, ...]
    capabilities: tuple[CapabilityPolicy, ...]
    skills: tuple[SkillMatch, ...] = ()


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


def discover(
    classification: RequestClassification, registry: Registry, skill_set: SkillSet | None = None
) -> LookupResult:
    """Look up sources/capabilities `classification` may draw on. Pure and deterministic:
    same (classification, registry) always yields the same `LookupResult`."""
    if not isinstance(classification, RequestClassification):
        raise LookupError("invalid_classification_type")
    if not isinstance(registry, Registry):
        raise LookupError("invalid_registry_type")
    if skill_set is not None and not isinstance(skill_set, SkillSet):
        raise LookupError("invalid_skill_set_type")

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

    return LookupResult(
        domain_supported=domain_supported, sources=sources, capabilities=capabilities,
        skills=_domain_applicable_skills(skill_set, classification.domain),
    )


def _domain_applicable_skills(skill_set: SkillSet | None, domain: str) -> tuple[SkillMatch, ...]:
    """Match skills by exact membership of the classifier domain in the skill's own `domains`.

    The same closed-vocabulary membership test the sources above use -- no scoring, no similarity,
    no synonym map. Skill text is never an input to this decision, so corpus or pack prose cannot
    influence WHICH skill is selected. Order follows the loaded set's pack order, which
    `SkillSet.from_packs` already sorted, so the result is deterministic."""
    if skill_set is None:
        return ()
    return tuple(
        SkillMatch(skill=skill, pack=pack)
        for pack in skill_set.packs
        for skill in pack.skills
        if domain in skill.domains
    )


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


__all__ = ["LookupError", "LookupResult", "SkillMatch", "SourceMatch", "discover",
           "resolve_capability", "resolve_source"]
