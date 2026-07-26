# Service composition layer (Slice 7A): pure composition of the frozen classify -> lookup ->
# route -> retrieval pipeline into the two public InvestigationResult/ReadResult shapes design.md
# assigns to `investigate_work`/`read_evidence`. This module owns NO protocol, network, or stdio
# wiring -- that is Slice 7B's `mcp_server.py`. Registry/snapshot access is injected via
# `ServiceDeps`, never loaded here, so composition stays testable against real or fixture deps
# alike -- design.md "Architecture": "investigate_work composes tested stages; read_evidence only
# resolves immutable refs."
#
# Slice 12A-1: the first authorized edit of this frozen module. `investigate()`'s non-`proceed`
# outcomes (`route_only`/`abstained`) now delegate to `context.assemble_context` (Slice 11,
# standalone until now) instead of hand-building the route-gated `InvestigationResult` inline --
# `assemble_context`'s `_route_gated_result` adds escalation `host_actions`
# (`_escalation_host_actions(decision.gaps)`), the consequential-action refusal warning plus
# `inspect_capability` action, and output-budget compaction, none of which the old manual
# construction had. The `proceed` path was untouched in 12A-1: `assemble_context` discards
# evidence not cited by a claim, which would destroy the capability + local retrieval catalog
# this module still builds directly -- wiring `proceed`'s CONCLUSIONS through the assembler
# remains a later sub-slice's job (claim formation is Slice 12A-3), not this one's.
#
# Slice 12A-2: the second authorized edit. `investigate()`'s `proceed` path now runs bounded live
# research (`research.py`, Slice 9B, standalone until now) over each `http`/`https` locator in
# `request.candidate_material` and folds the outcomes into the same combined evidence/host_actions/
# degradation catalog this module already builds -- mirroring the identical fold `context.py`'s
# `_assembled_result` (:184-201) already performs for assembled claims: fetched/cached evidence is
# appended (ref-deduplicated), everything else becomes a named `research_unavailable:<code>`
# degradation entry plus the ALREADY-COMPUTED vendor-neutral `host_actions` `research()` returned
# (reused, never re-derived). `ServiceDeps.research` defaults to `None`, so every existing
# construction -- including frozen `platform.load_deps` -- still type-checks and keeps research
# dormant: `_run_research` then returns `[]` and the `proceed` result is byte-identical to
# 12A-1's. `claims` stays `[]` here; forming claims from research evidence is Slice 12A-3, not
# this one.
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from .classify import classify
from .context import assemble_context
from .contracts import (
    EvidenceRecord, HostAction, InvestigationRequest, InvestigationResult, PermissionDisclosure,
    ReadItem, ReadRequest, ReadResult,
)
from .dispatch import SkillDispatch, dispatch
from .index import ActiveSnapshot
from .lookup import SkillMatch, discover, resolve_capability
from .packs import CapabilityPolicy
from .skills import PermissionEnvelope, SkillSet
from .registries import Registry
from .research import ResearchDeps, ResearchOutcome, research
from .retrieval import EmbedQuery, search, to_evidence_records
from .route import route

_CAPABILITY_REF_PREFIX = "capability:"
_SKILL_REF_PREFIX = "skill:"


class ServiceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ServiceDeps:
    """Everything `investigate`/`read` need but must never load themselves: the deterministic
    registry (5A/5B) and the currently active read-only snapshot (Slice 3/4). `embed_query`/
    `clock` mirror `retrieval.search`'s own injection points so tests and later slices control
    them identically. 7B wires the real registry/snapshot; tests wire real or fixture ones.
    `research` (Slice 12A-2) defaults to `None`, keeping live research dormant for every deps
    construction that does not explicitly opt in -- including frozen `platform.load_deps`, whose
    real `ResearchDeps` construction remains a later sub-slice."""

    registry: Registry
    snapshot: ActiveSnapshot
    embed_query: EmbedQuery | None = None
    clock: Callable[[], float] = time.monotonic
    research: ResearchDeps | None = None
    # Dormant unless supplied. A deps built without it behaves exactly as it did before the
    # skills layer existed, which is the same discipline `research` follows above.
    skill_set: SkillSet | None = None


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _content_hash(request: InvestigationRequest | ReadRequest) -> str:
    # Deterministic request identity: a content hash of the canonical request, never a random
    # UUID or timestamp -- the whole codebase forbids Date.now/random. `cursor` is excluded since
    # it is the caller's *position*, not the request's identity: a resumed call must hash to the
    # same request_id as the original, or cursor validation below becomes circular (a request_id
    # that includes its own cursor can never match once that cursor is set).
    canonical = _canonical_json(request.model_dump(mode="json", exclude={"cursor"}))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _encode_cursor(request_id: str, ref: str, start: int) -> str:
    payload = _canonical_json({"ref": ref, "request_id": request_id, "start": start})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str) -> dict[str, object] | None:
    try:
        value = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_deps(deps: object) -> ServiceDeps:
    if not isinstance(deps, ServiceDeps):
        raise ServiceError("invalid_deps_type")
    if not isinstance(deps.registry, Registry):
        raise ServiceError("invalid_registry_type")
    if not isinstance(deps.snapshot, ActiveSnapshot):
        raise ServiceError("invalid_snapshot_type")
    return deps


def _capability_digest(capability: CapabilityPolicy) -> str:
    # CapabilityPolicy.integrity is a prose instruction ("Verify the pinned distribution digest
    # against the release index."), not a literal sha256 -- EvidenceRecord/ReadItem require the
    # closed `sha256:<64 hex>` pattern, so this hashes the capability's own declared identity
    # fields instead. Deterministic: the same capability always yields the same digest.
    canonical = _canonical_json({
        "capability_id": capability.capability_id, "canonical_distribution": capability.canonical_distribution,
        "version": capability.version, "integrity": capability.integrity,
    })
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _capability_evidence_record(capability: CapabilityPolicy) -> EvidenceRecord:
    # Capability identity only: ref, canonical distribution, version, and digest. The frozen
    # EvidenceRecord has no field for permissions/limitations, so that disclosure happens on
    # read() (below) -- the investigate-emits / read-discloses split recorded for 7A-2 in tasks.md,
    # consistent with design.md "Architecture" ("read_evidence only resolves immutable refs").
    # authority/freshness/license/conflict are honestly "unknown" -- a capability record carries
    # registry-declared identity, never an authority assessment, and this module must never
    # fabricate one ("never concludes").
    return EvidenceRecord(
        ref=f"{_CAPABILITY_REF_PREFIX}{capability.capability_id}",
        kind="capability",
        publisher=capability.canonical_distribution,
        locator=capability.canonical_distribution,
        citation_locator=f"{capability.capability_id}@{capability.version}",
        digest=_capability_digest(capability),
        authority="unknown",
        authority_rationale="Capability identity only; permissions and limitations are disclosed on read.",
        freshness="unknown", license="unknown", conflict="unknown",
    )


def _skill_ref(match: SkillMatch) -> str:
    return f"{_SKILL_REF_PREFIX}{match.skill.skill_id}@{match.skill.version}"


def _disclose_envelope(envelope: PermissionEnvelope) -> PermissionDisclosure:
    """Flatten the signed pack's envelope into the public contract's shape.

    Disclosure, never enforcement: Bruriah does not execute skills. An envelope that grants nothing
    still serializes as six empty lists, because "grants nothing" is the strongest thing this layer
    can say about a skill and hiding it would waste the claim."""
    return PermissionDisclosure(
        filesystem_read=list(envelope.filesystem.read_paths),
        filesystem_write=list(envelope.filesystem.write_paths),
        network_hosts=list(envelope.network.hosts),
        network_schemes=list(envelope.network.schemes),
        programs=list(envelope.subprocess.programs),
        secrets=list(envelope.secrets),
    )


def _skill_evidence_record(entry: SkillDispatch) -> EvidenceRecord:
    """One record per dispatched skill: identity, provenance, and the declared envelope.

    The BODY is absent, and not by omission here -- `SkillPolicy` has no body field at all, so it is
    structurally impossible for a skill body to reach this surface. `locator` is where the HOST
    finds the body, which is a pointer an `install_skill` action needs, never the content itself.

    `digest` is the body digest human approval was bound to, so a host comparing its local copy is
    comparing against the approved bytes rather than against a version label. `authority` is
    honestly "unknown": provenance is attribution, never a safety or authority assessment, and this
    module must not fabricate one."""
    skill = entry.skill.skill
    return EvidenceRecord(
        ref=_skill_ref(entry.skill),
        kind="skill",
        publisher=skill.provenance,
        locator=skill.body_locator,
        citation_locator=f"{skill.skill_id}@{skill.version}",
        digest=skill.body_digest,
        provenance_chain=[
            f"tier:{skill.tier}", f"pack:{entry.skill.pack_id}", f"availability:{entry.availability}",
            f"currency:{entry.currency}", f"trusted:{str(entry.trusted).lower()}",
        ],
        authority="unknown",
        authority_rationale=skill.summary,
        # `freshness` speaks the contract's existing three words. A demoted skill says so HERE,
        # in the field a client already reads, rather than in a new channel nobody parses.
        freshness=entry.currency, license="unknown", conflict="unknown",
        envelope=_disclose_envelope(skill.permissions),
    )


def _drafting_action(classification, dispatched) -> HostAction | None:
    """Ask the HOST to draft a candidate when a supported domain has no trusted skill covering it.

    Bruriah generates nothing. It has no generative model at all -- the dependency list is embeddings
    and protocol -- so the only honest move on detecting a gap is to name the gap and hand the work
    to whoever can do it. The brief is bounded and carries NO proposed content: a suggested draft
    written here would be exactly the obeyable instruction text this whole design refuses to emit.

    Fires only when nothing TRUSTED remains, so a domain covered solely by demoted skills is treated
    as uncovered -- which it is."""
    if any(entry.trusted for entry in dispatched.skills):
        return None
    return HostAction(
        kind="draft_skill_candidate",
        reason=f"No vetted skill covers the {classification.domain} domain for this request. "
               "Draft one and submit it through `bruriah skill-ingest`; it enters as a "
               "candidate and still requires analysis and human approval before it is dispatched.",
        target=classification.domain,
    )


def _skill_outcomes(entries: tuple[SkillDispatch, ...]) -> tuple[list[str], list[HostAction]]:
    """Turn availability into gaps and host actions. Divergence is never reported as approved."""
    gaps: list[str] = []
    actions: list[HostAction] = []
    for entry in entries:
        ref = _skill_ref(entry.skill)
        if not entry.trusted:
            # Demotion is REPORTED, never applied by editing anything. The approved body and its
            # digest are untouched: an overdue review is a fact about the review, not about the
            # content, and the skill stays available for re-approval exactly as it was.
            gaps.append(f"skill_{entry.currency}:{ref}")
            actions.append(HostAction(
                kind="inspect_capability",
                reason=f"This skill's pack is {entry.currency}; it is disclosed as a candidate "
                       "rather than as vetted guidance until it is reviewed again.",
                target=ref,
            ))
        if entry.availability == "not_installed":
            gaps.append(f"skill_not_installed:{ref}")
            actions.append(HostAction(kind="install_skill", reason=entry.skill.skill.summary, target=ref))
        elif entry.availability == "digest_divergent":
            gaps.append(f"skill_digest_divergent:{ref}")
            actions.append(HostAction(
                kind="inspect_capability",
                reason="The installed copy does not match the approved digest and is unapproved.",
                target=ref,
            ))
    return gaps, actions


def _window_text(
    text: str, requested_range: object, cursor_start: int | None, item_cap: int, remaining_total: int,
) -> tuple[str, int, int, bool] | None:
    # Shared exact-window/truncation logic for both local passages and capability disclosure
    # text: 1-indexed start, budget-capped window, and honest truncation reporting. `None` signals
    # an out-of-range start (`invalid_range`), never a fabricated substitute.
    length = len(text)
    start = cursor_start if cursor_start is not None else (requested_range.start if requested_range else 1)
    if start < 1 or start > length:
        return None
    wanted_end = min(requested_range.end, length) if requested_range else length
    cap = max(min(item_cap, remaining_total), 0)
    window = text[start - 1: wanted_end][:cap]
    actual_end = start - 1 + len(window)
    return window, start, actual_end, actual_end < wanted_end


def _candidate_urls(request: InvestigationRequest) -> list[str]:
    # Only `http`/`https` locators are live-fetch targets -- other `candidate_material` locators
    # (e.g. a bare capability id or filesystem-style path) are not URLs `research.py`/`fetch.py`
    # can ever admit, so they are silently skipped here rather than passed through to fail later.
    # Order-preserving de-duplication, then truncated to the declared `max_network_requests`
    # ceiling -- the same budget `research()` itself would refuse against one at a time, checked
    # here too so this module never plans more research calls than the request actually allows.
    urls: list[str] = []
    for material in request.candidate_material:
        if urlsplit(material.locator).scheme not in ("http", "https"):
            continue
        if material.locator not in urls:
            urls.append(material.locator)
    return urls[: request.budgets.max_network_requests]


def _run_research(request: InvestigationRequest, deps: ServiceDeps) -> list[ResearchOutcome]:
    # `deps.research is None` (the default) keeps research dormant: no ResearchDeps means no
    # network policy has been provisioned for this deps construction, so this returns `[]` rather
    # than raising -- the invariant that keeps the `proceed` result byte-identical to 12A-1's
    # whenever research was never wired in. Sequential, in `_candidate_urls`' deterministic order:
    # `research()` is total and never raises (module docstring #7), so no try/except is needed
    # here, and there is no fan-out to make ordering ambiguous.
    if deps.research is None:
        return []
    return [research(request, url, deps.research) for url in _candidate_urls(request)]


def _fold_research(
    outcomes: list[ResearchOutcome],
) -> tuple[list[HostAction], list[str], list[EvidenceRecord]]:
    # Mirrors `context.py`'s `_assembled_result` research fold (:184-201) exactly: a `cached`/
    # `fetched` outcome's evidence is appended (ref-deduplicated against outcomes already folded);
    # every other outcome is a named `research_unavailable:<code>` degradation entry plus its
    # ALREADY-COMPUTED vendor-neutral `host_actions` (reused verbatim, never re-derived -- research.
    # py already built them without fetching). Deduplicated by `HostAction` equality, same as
    # `context.py`.
    host_actions: list[HostAction] = []
    degradation: list[str] = []
    evidence: list[EvidenceRecord] = []
    seen_refs: set[str] = set()
    for outcome in outcomes:
        if outcome.status in ("cached", "fetched") and outcome.evidence is not None:
            if outcome.evidence.ref not in seen_refs:
                evidence.append(outcome.evidence)
                seen_refs.add(outcome.evidence.ref)
        else:
            degradation.append(f"research_unavailable:{outcome.code}")
            for action in outcome.host_actions:
                if action not in host_actions:
                    host_actions.append(action)
    return host_actions, degradation, evidence


def investigate(request: InvestigationRequest, deps: ServiceDeps) -> InvestigationResult:
    """Compose classify -> discover -> route, then, only on `proceed`, retrieve over the
    snapshot AND run bounded live research (Slice 12A-2) over any `http`/`https` candidate-
    material locators. `route_only`/`abstained` delegate to `context.assemble_context` (Slice
    12A-1), which carries the route decision's gaps, escalation `host_actions`, and safety
    `warnings`, and never retrieves or fabricates evidence -- design.md "Routing/retrieval":
    "Generic discovery only routes or abstains." Deterministic: identical `request`/`deps` state
    always yields an identical result (`deps.research is None` -> research stays dormant ->
    result is byte-identical to a build with no research wired in at all). Errors raised by the
    frozen stages (`ClassificationError`/`LookupError`/`RouteError`/`RetrievalError`) are already
    typed `ValueError` subclasses with a `.code` and propagate unwrapped; `ServiceError` is
    reserved for this module's own request/deps validation.
    """
    if not isinstance(request, InvestigationRequest):
        raise ServiceError("invalid_request_type")
    deps = _validate_deps(deps)
    if request.cursor is not None:
        # retrieval.search (frozen Slice 6A) has no offset/exclusion parameter, so a facade
        # cursor here could never actually resume a truncated request -- returning one anyway
        # would be a false promise. `status=partial` plus `degradation` already signal
        # truncation honestly; real investigate() pagination needs a 6A extension, not a 7A
        # workaround, so a client-supplied cursor fails typed instead of being silently dropped.
        raise ServiceError("cursor_not_supported")

    request_id = _content_hash(request)
    classification = classify(request)
    # The opt-in gate, applied once and early: a request without `host_skills` never reaches the
    # skill set, so dispatch cannot emit a ref, a gap, an action, or a new enum member to a
    # client that did not ask for one.
    opted_in = request.host_skills is not None
    lookup = discover(classification, deps.registry, deps.skill_set if opted_in else None)
    decision = route(classification, lookup, request)

    if decision.outcome != "proceed":
        # `assemble_context`'s `_request_id` recomputes the identical content-hash formula this
        # module uses above (both: model_dump(mode="json", exclude={"cursor"}) -> canonical JSON
        # -> sha256:), so delegating here keeps `request_id` byte-identical to a manually built
        # result -- `mode="full"` still routes to the route-gated branch because `assemble_context`
        # gates on `decision.outcome`, not on `mode`, whenever it isn't already "proceed".
        return assemble_context(request, decision, mode="full")

    evidence, warnings, degradation, status, host_actions = [], [], [], decision.outcome, []
    extra_gaps: list[str] = []
    if decision.outcome == "proceed":
        # Capability evidence (Slice 7A-2): one EvidenceRecord per matched `lookup.capabilities`
        # entry -- "Method and tool discovery" requires capability refs alongside knowledge, not
        # local retrieval alone. `lookup` was already computed above for route()'s has_evidence
        # gate; nothing here re-queries the registry.
        capability_evidence = [_capability_evidence_record(capability) for capability in lookup.capabilities]
        # Skill refs lead the evidence list for the same reason capabilities do: they are the
        # smallest, most specific answer to "what procedure applies here". `dispatch` is only
        # reached under the opt-in gate, so `skill_evidence` is [] for every pre-skills client.
        skill_dispatch = dispatch(lookup, request.host_skills or []) if opted_in else None
        skill_evidence = [_skill_evidence_record(item) for item in skill_dispatch.skills] if skill_dispatch else []
        outcome = search(
            deps.snapshot, request.task, request.budgets,
            embed_query=deps.embed_query, clock=deps.clock,
        )
        local_evidence = to_evidence_records(outcome)
        warnings = list(outcome.warnings)
        degradation = list(outcome.degradation)
        truncated = outcome.truncated
        # Combine capabilities first, then local retrieval, in each side's own deterministic order
        # (registry pack_id-then-id order; retrieval's own ranking). Capabilities lead because they
        # most directly answer "which tool/library" -- the reason `route()` proceeded on a
        # capability_recommendation claim -- and are typically the smaller, most specific set;
        # local passages fill the remaining budget. This ordering is the truncation preference below.
        evidence = skill_evidence + capability_evidence + local_evidence
        # Bounded live research (Slice 12A-2): run only when `deps.research` was actually
        # provisioned (`_run_research` returns `[]` otherwise -- the byte-identical-to-12A-1
        # invariant), then fold outcomes the same way `context.py`'s `_assembled_result` folds
        # them for assembled claims: fetched/cached evidence appended, everything else a named
        # `research_unavailable:<code>` degradation entry plus its already-computed host actions.
        research_outcomes = _run_research(request, deps)
        host_actions, research_degradation, research_evidence = _fold_research(research_outcomes)
        evidence = evidence + research_evidence
        degradation = degradation + research_degradation
        # Enforce the declared `max_evidence` ceiling over the COMBINED set (capability + local +
        # research). retrieval.search bounds by `max_candidates` (a scan ceiling), not by
        # `max_evidence` (the result-item ceiling), so without this an investigation could return
        # more evidence items than the smaller declared budget -- "Bounded Investigation": every
        # request MUST enforce the declared evidence-items ceiling. Truncation is reported, never
        # silent.
        if len(evidence) > request.budgets.max_evidence:
            evidence = evidence[: request.budgets.max_evidence]
            degradation.append("max_evidence_exceeded")
            truncated = True
        status = "partial" if truncated or research_degradation else "complete"
        if skill_dispatch is not None:
            skill_gaps, skill_actions = _skill_outcomes(skill_dispatch.skills)
            extra_gaps = list(skill_dispatch.gaps) + skill_gaps
            host_actions = host_actions + skill_actions
            drafting = _drafting_action(classification, skill_dispatch)
            if drafting is not None:
                extra_gaps.append(f"no_skill_for_domain:{classification.domain}")
                host_actions = host_actions + [drafting]

    return InvestigationResult(
        schema_version="1", status=status, request_id=request_id, evidence=evidence,
        claims=[], conflicts=[], gaps=list(decision.gaps) + extra_gaps, host_actions=host_actions,
        warnings=warnings, degradation=degradation, budgets=request.budgets, next_cursor=None,
    )


def _read_one(
    database: sqlite3.Connection, ref: str, requested_range: object, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    row = database.execute(
        "SELECT relative_path, start_line, end_line, text, source_hash FROM passages WHERE ref = ?",
        (ref,),
    ).fetchone()
    if row is None:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total
    relative_path, start_line, end_line, text, source_hash = row

    windowed = _window_text(text, requested_range, cursor_start, item_cap, remaining_total)
    if windowed is None:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    window, start, actual_end, truncated = windowed
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=f"sha256:{source_hash}", truncated=truncated, next_cursor=next_cursor,
        evidence_kind="local", locator=relative_path,
        citation_locator=f"{relative_path}#{start_line}-{end_line}",
        authority="unknown", freshness="unknown", license="unknown", conflict="unknown",
    )
    return item, remaining_total - len(window)


def _read_capability_one(
    registry: Registry, ref: str, requested_range: object, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    # Resolves a `capability:<id>` ref via the frozen `resolve_capability` (6B-2) -- never a
    # fabricated or nearest-match record; not-found is a typed `missing_ref`, exactly like local
    # refs. Content is the capability's own registry-declared metadata as canonical JSON: disclosure
    # of what the pack states, never a recommendation to run the tool and never a conclusion.
    capability = resolve_capability(ref[len(_CAPABILITY_REF_PREFIX):], registry)
    if capability is None:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total
    disclosure = _canonical_json({
        "canonical_distribution": capability.canonical_distribution, "version": capability.version,
        "integrity": capability.integrity, "advisories": capability.advisories,
        "permissions": capability.permissions, "network_access": capability.network_access,
        "data_access": capability.data_access, "limitations": capability.limitations,
    })

    windowed = _window_text(disclosure, requested_range, cursor_start, item_cap, remaining_total)
    if windowed is None:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    window, start, actual_end, truncated = windowed
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=_capability_digest(capability), truncated=truncated, next_cursor=next_cursor,
        evidence_kind="capability", locator=capability.canonical_distribution,
        citation_locator=f"{capability.capability_id}@{capability.version}",
        authority="unknown", freshness="unknown", license="unknown", conflict="unknown",
    )
    return item, remaining_total - len(window)


def _read_skill_one(
    skill_set: SkillSet | None, ref: str, requested_range: object, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    # Resolves a `skill:<id>@<version>` ref against the ACTIVE set, mirroring `_read_capability_one`.
    # Content is the skill's declared METADATA as canonical JSON -- identity, provenance, envelope,
    # advisories, limitations. The body is not withheld here so much as absent by construction:
    # `SkillPolicy` has no body field, so there is no code path through either tool that could
    # return one. The version must match too: a ref pinned to a version the active set no longer
    # carries is `missing_ref`, never silently answered with a different version's metadata.
    identifier, _, version = ref[len(_SKILL_REF_PREFIX):].partition("@")
    skill = skill_set.resolve(identifier) if skill_set is not None else None
    if skill is None or skill.version != version:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total
    disclosure = _canonical_json({
        "skill_id": skill.skill_id, "version": skill.version, "tier": skill.tier,
        "payload": skill.payload, "summary": skill.summary, "domains": skill.domains,
        "body_locator": skill.body_locator, "body_digest": skill.body_digest,
        "permissions": _disclose_envelope(skill.permissions).model_dump(mode="json"),
        "provenance": skill.provenance, "license": skill.license,
        "advisories": skill.advisories, "limitations": skill.limitations,
    })

    windowed = _window_text(disclosure, requested_range, cursor_start, item_cap, remaining_total)
    if windowed is None:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    window, start, actual_end, truncated = windowed
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=skill.body_digest, truncated=truncated, next_cursor=next_cursor,
        evidence_kind="skill", locator=skill.body_locator,
        citation_locator=f"{skill.skill_id}@{skill.version}",
        authority="unknown", freshness="unknown", license="unknown", conflict="unknown",
    )
    return item, remaining_total - len(window)


def read(request: ReadRequest, deps: ServiceDeps) -> ReadResult:
    """Resolve each `refs` entry to immutable local or capability evidence and return exact,
    budget-bounded content. Missing or out-of-range refs get typed per-ref failures -- never
    another ref's content and never a fabricated substitute. `stale_ref`/`expired_ref`/
    `ineligible_ref` are structurally supported statuses but unreachable from 7A's deps: a single
    active snapshot has no history to compare a ref against, so that limitation is stated
    explicitly rather than silently folded into `missing_ref`. A `capability:<id>` ref (7A-2)
    resolves via `resolve_capability` (6B-2) and discloses the registry's declared
    canonical_distribution/version/integrity/advisories/permissions/network_access/data_access/
    limitations in `content` -- the frozen `EvidenceRecord`/`ReadItem` have no dedicated fields
    for permissions/limitations. `resolve_source` (6B-2) exists for source-kind evidence a later
    slice may surface; wiring it here now would resolve refs `investigate()` never emits.
    """
    if not isinstance(request, ReadRequest):
        raise ServiceError("invalid_request_type")
    deps = _validate_deps(deps)

    request_id = _content_hash(request)
    cursor_ref: str | None = None
    cursor_start: int | None = None
    if request.cursor is not None:
        decoded = _decode_cursor(request.cursor)
        if (
            not decoded or decoded.get("request_id") != request_id
            or decoded.get("ref") not in request.refs or not isinstance(decoded.get("start"), int)
        ):
            raise ServiceError("invalid_cursor")
        cursor_ref, cursor_start = decoded["ref"], decoded["start"]

    ranges_by_ref = {item.ref: item for item in request.ranges}
    remaining = request.budgets.max_output_chars
    items: list[ReadItem] = []
    try:
        for ref in request.refs:
            if ref.startswith(_SKILL_REF_PREFIX):
                reader, source = _read_skill_one, deps.skill_set
            elif ref.startswith(_CAPABILITY_REF_PREFIX):
                reader, source = _read_capability_one, deps.registry
            else:
                reader, source = _read_one, deps.snapshot.database
            item, remaining = reader(
                source, ref, ranges_by_ref.get(ref),
                cursor_start if ref == cursor_ref else None,
                request.budgets.max_extracted_chars, remaining, request_id,
            )
            items.append(item)
    except sqlite3.DatabaseError as error:
        raise ServiceError("snapshot_unreadable") from error

    warnings = ["output_budget_exhausted"] if remaining <= 0 and any(item.truncated for item in items) else []
    next_cursor = next((item.next_cursor for item in items if item.next_cursor), None)
    return ReadResult(
        schema_version="1", request_id=request_id, items=items,
        warnings=warnings, budgets=request.budgets, next_cursor=next_cursor,
    )


__all__ = ["ServiceDeps", "ServiceError", "investigate", "read"]
