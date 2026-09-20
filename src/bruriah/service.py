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
from pathlib import Path
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from .cache import find_by_ref
from .classify import classify
from .context import assemble_context, compact_to_budget
from .contracts import (
    AlternativeRecord, ClaimRecord, CounterfactualAssessment, EvidenceRecord, HostAction,
    InvestigationRequest, InvestigationResult, PermissionDisclosure, PremiseRecord,
    ReadItem, ReadRange, ReadRequest, ReadResult,
)
from .dispatch import DEFAULT_SKILL_CEILING, SkillDispatch, dispatch
from .index import ActiveSnapshot
from .lookup import LookupResult, SkillMatch, discover, resolve_capability
from .packs import CapabilityPolicy
from .skills import PermissionEnvelope, SkillSet
from .registries import Registry
from .repository import RepositoryError, SnapshotRepository
from .research import NetworkLedger, ResearchDeps, ResearchOutcome, research
from .retrieval import EmbedQuery, Rerank, SearchService, is_shortfall, to_evidence_records
from .route import route
from .why import WhyError, trace_causal_archaeology

_CAPABILITY_REF_PREFIX = "capability:"
_SKILL_REF_PREFIX = "skill:"
# The prefix `fetch.py` mints for captured live evidence (`live:sha256:<32 hex>`). It lives here
# as a constant, next to the two it joins, so the routing table in `read()` reads as one list of
# ref kinds rather than two named prefixes and a literal.
_LIVE_REF_PREFIX = "live:"


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
    # Resolved by the OPERATOR (`platform.resolve_paths`), never by the calling host -- see the
    # note on `PlatformPaths.skill_ceiling` for why this is not a `Budgets` field. Defaulting to
    # the same constant `dispatch` already defaulted to keeps every existing construction, and
    # every test that builds deps directly, byte-identical to before this was configurable.
    skill_ceiling: int = DEFAULT_SKILL_CEILING
    # Dormant unless supplied, the same discipline `research` and `skill_set` follow: a deps built
    # without it ranks exactly as it did before the reranking stage existed. Off by default is a
    # measured position and not caution -- the stage is worth 0.340 -> 0.431 recall@3 on one
    # foreign corpus and one question out of eighty-three on the other, while costing a 1.11 GB
    # download and a cross-encoder pass per candidate document. That is an operator's call.
    rerank: Rerank | None = None
    repo: Path = Path(".")



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


def _decode_cursor(token: str) -> dict[str, Any] | None:
    try:
        value = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _encode_investigate_cursor(request_id: str, build_id: str, offset: int) -> str:
    payload = _canonical_json({"build_id": build_id, "kind": "investigate", "offset": offset, "request_id": request_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_investigate_cursor(token: str, expected_request_id: str, expected_build_id: str) -> int:
    decoded = _decode_cursor(token)
    if (
        decoded is None
        or decoded.get("kind") != "investigate"
        or decoded.get("request_id") != expected_request_id
        or decoded.get("build_id") != expected_build_id
        or not isinstance(decoded.get("offset"), int)
        or decoded["offset"] < 1
    ):
        raise ServiceError("invalid_cursor")
    return decoded["offset"]



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


def _pack_currency_gaps(lookup: LookupResult) -> list[str]:
    """Name the domain packs whose review window has passed, whatever the request goes on to do.

    An expired pack no longer registers its domains (`lookup._domain_applicable_pack_ids`), so the
    request abstains with `no_approved_domain_pack` -- which, on its own, is indistinguishable from
    a domain this tool never covered. It is not the same situation at all, and only the operator
    can tell the difference matters: one is out of scope, the other is a review that lapsed and a
    pack that can be re-signed. The gap names the pack so the abstention says which.

    A stale pack still answers; the gap rides alongside the result rather than instead of it, which
    is the whole distinction between "the review is due" and "the review is void". Both are emitted
    on every outcome, because an aged pack is a fact about the installation and not about whether
    this particular request happened to reach retrieval."""
    return [
        *(f"pack_expired:{pack_id}" for pack_id in lookup.expired_pack_ids),
        *(f"pack_stale:{pack_id}" for pack_id in lookup.stale_pack_ids),
    ]


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
    text: str, requested_range: ReadRange | None, cursor_start: int | None, item_cap: int, remaining_total: int,
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
    # ONE ledger for the whole investigation, not one budget per URL. `_candidate_urls` already
    # caps how many URLs are attempted at `max_network_requests`, which bounded the COUNT of
    # fetches and nothing else: each of those fetches then received the full declared budget
    # again, so bytes and elapsed time multiplied by the number of candidates while every
    # individual call stayed honestly inside its limits. Measured before this line existed: a
    # request declaring `max_bytes=1_000_000` and `max_network_requests=5` produced 5 connections
    # and 2,500,000 bytes served.
    #
    # Built from `deps.research.clock` rather than this module's `deps.clock`, because the
    # deadline has to be measured against the same monotonic source `fetch.py` compares it to.
    ledger = NetworkLedger.for_request(request.budgets, deps.research.clock)
    return [research(request, url, deps.research, ledger) for url in _candidate_urls(request)]


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


def _apply_lineage(
    local_evidence: list[EvidenceRecord],
    repo: SnapshotRepository,
) -> tuple[list[EvidenceRecord], list[ClaimRecord], list[str]]:
    if not local_evidence:
        return local_evidence, [], []

    if not repo.has_lineage_table():
        return local_evidence, [], []

    ref_keys = [rec.ref for rec in local_evidence]
    ref_to_doc = repo.get_passages_meta_by_refs(ref_keys)
    if not ref_to_doc:
        return local_evidence, [], []

    doc_to_refs: dict[str, list[str]] = {}
    for ref, (doc_ref, _) in ref_to_doc.items():
        doc_to_refs.setdefault(doc_ref, []).append(ref)

    doc_refs = list(doc_to_refs)
    if not doc_refs:
        return local_evidence, [], []

    lineage_rows = repo.get_lineage_relations(doc_refs)
    if not lineage_rows:
        return local_evidence, [], []

    claims: list[ClaimRecord] = []
    conflicts: list[str] = []
    updated_evidence = list(local_evidence)
    additional_evidence: list[EvidenceRecord] = []
    existing_refs = {rec.ref for rec in local_evidence}
    ref_overrides: dict[str, dict[str, Any]] = {}

    for rel_item in lineage_rows:
        succ_ref = rel_item.successor_ref
        pred_target = rel_item.predecessor_target
        pred_ref = rel_item.predecessor_ref
        rel = rel_item.relation

        if pred_ref and pred_ref in doc_to_refs:
            succ_path = repo.get_document_path(succ_ref) or succ_ref

            for target_ref in doc_to_refs[pred_ref]:
                overrides = ref_overrides.setdefault(target_ref, {})
                if rel == "supersedes":
                    overrides["freshness"] = "stale"
                    overrides["conflict"] = "declared"
                    overrides.setdefault("uncertainty", []).append(f"superseded_by:{succ_path}")
                elif rel == "deprecates":
                    overrides["freshness"] = "stale"
                    overrides.setdefault("uncertainty", []).append(f"deprecated_by:{succ_path}")

            if rel == "supersedes":
                pred_path = ref_to_doc[doc_to_refs[pred_ref][0]][1]
                conflicts.append(f"Decision in {pred_path} was superseded by {succ_path}")

                succ_passages = repo.get_passages_by_document(succ_ref, limit=1)
                supporting_refs: list[str] = []
                for p in succ_passages:
                    supporting_refs.append(p.ref)
                    if p.ref not in existing_refs:
                        existing_refs.add(p.ref)
                        additional_evidence.append(
                            EvidenceRecord(
                                ref=p.ref,
                                kind="local",
                                publisher=p.relative_path,
                                locator=p.relative_path,
                                citation_locator=f"{p.relative_path}#{p.start_line}-{p.end_line}",
                                digest=f"sha256:{p.source_hash}",
                                extraction_method="markdown_section",
                                authority="unknown",
                                authority_rationale="not_assessed_by_retrieval",
                                freshness="current",
                                license="unknown",
                                conflict="none",
                            )
                        )
                claims.append(
                    ClaimRecord(
                        text=f"Decision in {pred_path} was superseded by {succ_path}",
                        state="conflicted",
                        supporting_refs=supporting_refs,
                        conflicting_refs=doc_to_refs[pred_ref],
                    )
                )

        if succ_ref in doc_to_refs and rel == "supersedes":
            for target_ref in doc_to_refs[succ_ref]:
                overrides = ref_overrides.setdefault(target_ref, {})
                overrides["freshness"] = "current"
                overrides["conflict"] = "none"

        if succ_ref in doc_to_refs and rel == "amends":
            for target_ref in doc_to_refs[succ_ref]:
                ref_overrides.setdefault(target_ref, {}).setdefault("provenance_chain", []).append(
                    f"amends:{pred_target}"
                )

    result_evidence: list[EvidenceRecord] = []
    for rec in updated_evidence:
        if rec.ref in ref_overrides:
            ov = ref_overrides[rec.ref]
            rec_dict = rec.model_dump()
            if "freshness" in ov:
                rec_dict["freshness"] = ov["freshness"]
            if "conflict" in ov:
                rec_dict["conflict"] = ov["conflict"]
            if "uncertainty" in ov:
                rec_dict["uncertainty"] = list(rec.uncertainty) + ov["uncertainty"]
            if "provenance_chain" in ov:
                rec_dict["provenance_chain"] = list(rec.provenance_chain) + ov["provenance_chain"]
            result_evidence.append(EvidenceRecord.model_validate(rec_dict))
        else:
            result_evidence.append(rec)

    result_evidence.extend(additional_evidence)
    return result_evidence, claims, conflicts


def _resolve_code_target_causality(
    code_target: str,
    repo_path: Path,
    snapshot_repo: SnapshotRepository,
) -> tuple[list[EvidenceRecord], list[ClaimRecord], list[str], list[str], list[str]]:
    """Trace the governing architectural decision for a code target in Git + SQLite."""
    evidence: list[EvidenceRecord] = []
    claims: list[ClaimRecord] = []
    conflicts: list[str] = []
    warnings: list[str] = []
    degradation: list[str] = []

    try:
        res = trace_causal_archaeology(repo_path, snapshot_repo.database, code_target)
    except WhyError as error:
        degradation.append(f"code_target_unavailable:{error.code}")
        return evidence, claims, conflicts, warnings, degradation
    except Exception:
        degradation.append("code_target_unavailable:unexpected_error")
        return evidence, claims, conflicts, warnings, degradation

    if res.governing_decision is None:
        warnings.append(f"code_target_no_decision:{res.line_commit.sha[:12]}")
        degradation.append("code_target_unindexed_decision")
        return evidence, claims, conflicts, warnings, degradation

    gov = res.governing_decision
    try:
        passages_rows = snapshot_repo.get_passages_by_document(gov.document_ref)
    except RepositoryError:
        degradation.append("code_target_passages_unreadable")
        return evidence, claims, conflicts, warnings, degradation

    if not passages_rows:
        warnings.append(f"code_target_passages_missing:{gov.document_ref}")
        return evidence, claims, conflicts, warnings, degradation

    p_first = passages_rows[0]
    p_ref = p_first.ref
    p_path = p_first.relative_path
    p_start = p_first.start_line
    p_end = p_first.end_line
    p_hash = p_first.source_hash

    alerts = res.lineage_alerts
    freshness: Literal["current", "stale", "expired", "unknown"] = "stale" if alerts else "current"
    conflict_state: Literal["none", "declared", "unknown"] = "declared" if alerts else "none"
    uncertainty: list[str] = [
        f"{a.relation}:{a.successor_commit[:12] if a.successor_commit else a.successor_ref}"
        for a in alerts
    ]

    rationale = (
        f"Governing architectural decision for {code_target} decided by {gov.author} "
        f"on {gov.date} (commit {gov.commit_sha[:12]})."
    )

    ev_record = EvidenceRecord(
        ref=p_ref,
        kind="local",
        publisher=p_path,
        locator=p_path,
        citation_locator=f"{p_path}#{p_start}-{p_end}",
        digest=f"sha256:{p_hash}",
        extraction_method="markdown_section",
        provenance_chain=[
            f"commit:{gov.commit_sha[:12]}",
            f"line_commit:{res.line_commit.sha[:12]}",
            f"author:{gov.author}",
            f"target:{code_target}",
        ][:10],
        authority="primary",
        authority_rationale=rationale,
        freshness=freshness,
        license="permitted",
        reuse="permitted",
        conflict=conflict_state,
        uncertainty=uncertainty[:10],
    )
    evidence.append(ev_record)

    conflicting_refs: list[str] = []
    if alerts:
        for alert in alerts:
            conf_msg = (
                f"Decision in {p_path} governing {code_target} has been {alert.relation} "
                f"by {alert.successor_commit[:8] if alert.successor_commit else alert.successor_ref}: "
                f"{alert.successor_subject or 'successor'}"
            )
            if alert.depth > 1 and alert.active_successor_ref:
                act_str = (
                    alert.active_successor_commit[:8]
                    if alert.active_successor_commit
                    else alert.active_successor_ref
                )
                conf_msg += f" (evolved to active leaf {act_str}: {alert.active_successor_subject or 'active'})"
            conflicts.append(conf_msg)

            try:
                target_succs = [alert.successor_ref]
                if alert.depth > 1 and alert.active_successor_ref and alert.active_successor_ref not in target_succs:
                    target_succs.append(alert.active_successor_ref)

                for idx, succ_doc_ref in enumerate(target_succs):
                    is_active_leaf = (idx == len(target_succs) - 1 and alert.depth > 1) or (alert.depth == 1)
                    succ_passages = snapshot_repo.get_passages_by_document(succ_doc_ref, limit=1)
                    for s in succ_passages:
                        conflicting_refs.append(s.ref)
                        evidence.append(
                            EvidenceRecord(
                                ref=s.ref,
                                kind="local",
                                publisher=s.relative_path,
                                locator=s.relative_path,
                                citation_locator=f"{s.relative_path}#{s.start_line}-{s.end_line}",
                                digest=f"sha256:{s.source_hash}",
                                extraction_method="markdown_section",
                                authority="primary",
                                authority_rationale=(
                                    f"Active successor decision for {code_target}"
                                    if is_active_leaf
                                    else f"Intermediate successor decision ({alert.relation}) for {code_target}"
                                ),
                                freshness="current" if is_active_leaf else "stale",
                                license="permitted",
                                conflict="none" if is_active_leaf else "declared",
                            )
                        )
            except RepositoryError:
                pass

        claims.append(
            ClaimRecord(
                text=f"Governing decision {gov.commit_sha[:8]} for {code_target} is {alerts[0].relation}",
                state="conflicted",
                supporting_refs=[p_ref],
                conflicting_refs=conflicting_refs,
            )
        )
    else:
        claims.append(
            ClaimRecord(
                text=f"Decision {gov.commit_sha[:8]} ({gov.subject}) governs {code_target}",
                state="supported",
                supporting_refs=[p_ref],
                conflicting_refs=[],
            )
        )

    return evidence, claims, conflicts, warnings, degradation


def _evaluate_counterfactual(
    request: InvestigationRequest,
    snapshot_repo: SnapshotRepository,
) -> tuple[
    CounterfactualAssessment | None,
    list[AlternativeRecord],
    list[PremiseRecord],
    list[EvidenceRecord],
    list[str],
]:
    """Evaluate candidate task/target against historical alternatives and premises."""
    if not snapshot_repo.has_counterfactual_tables():
        return None, [], [], [], []

    alt_rows = snapshot_repo.get_alternatives()
    if not alt_rows:
        return None, [], [], [], []

    premises_map = snapshot_repo.get_premises()
    task_text = request.task.lower()
    target_text = request.code_target.lower() if request.code_target else ""

    matched_alt = None
    task_words = re.findall(r"\w+", task_text)
    target_words = re.findall(r"\w+", target_text) if target_text else []
    all_words = task_words + target_words

    for alt in alt_rows:
        alt_name_lower = alt.name.lower()
        # 1. Exact substring
        if alt_name_lower in task_text or (target_text and alt_name_lower in target_text):
            matched_alt = alt
            break

        # 2. Word boundary regex
        try:
            pattern = rf"\b{re.escape(alt_name_lower)}\b"
            if re.search(pattern, task_text) or (target_text and re.search(pattern, target_text)):
                matched_alt = alt
                break
        except re.error:
            pass

        # 3. Token-set match: all distinct keywords (len >= 3) of the alternative appear in the text
        alt_tokens = [tok for tok in re.findall(r"\w+", alt_name_lower) if len(tok) >= 3]
        if len(alt_tokens) >= 2:
            if all(tok in task_text or any(w.startswith(tok[:4]) for w in all_words) for tok in alt_tokens):
                matched_alt = alt
                break

    if matched_alt is None:
        return None, [], [], [], []

    invalidated_premises = [
        premises_map[pid]
        for pid in matched_alt.premises
        if pid in premises_map and premises_map[pid].status == "invalidated"
    ]
    active_premises = [
        premises_map[pid]
        for pid in matched_alt.premises
        if pid in premises_map and premises_map[pid].status == "active"
    ]
    unassessed_premises = [
        pid
        for pid in matched_alt.premises
        if pid not in premises_map or premises_map[pid].status not in ("active", "invalidated")
    ]

    supporting_refs: list[str] = []
    cf_evidence: list[EvidenceRecord] = []

    try:
        doc_passages = snapshot_repo.get_passages_by_document(matched_alt.document_ref, limit=1)
        for p in doc_passages:
            supporting_refs.append(p.ref)
            cf_evidence.append(
                EvidenceRecord(
                    ref=p.ref,
                    kind="local",
                    publisher=p.relative_path,
                    locator=p.relative_path,
                    citation_locator=f"{p.relative_path}#{p.start_line}-{p.end_line}",
                    digest=f"sha256:{p.source_hash}",
                    extraction_method="markdown_section",
                    authority="primary",
                    authority_rationale=f"Evaluated alternative '{matched_alt.name}' ({matched_alt.disposition}).",
                    freshness="current",
                    license="permitted",
                    reuse="permitted",
                    conflict="none",
                )
            )
    except RepositoryError:
        pass

    cf_conflicts: list[str] = []
    if invalidated_premises:
        verdict: Literal[
            "repeat_of_rejected_architecture",
            "premise_changed_requires_reevaluation",
            "unassessed_premise",
        ] = "premise_changed_requires_reevaluation"
        inv_p = invalidated_premises[0]
        inv_by = f"commit {inv_p.invalidated_by[:12]}" if inv_p.invalidated_by else "subsequent decision"
        rationale = (
            f"Alternative '{matched_alt.name}' was evaluated and {matched_alt.disposition} "
            f"because: {matched_alt.reason}. However, premise '{inv_p.premise_id}' was "
            f"invalidated by {inv_by}. The decision requires reevaluation under current conditions."
        )
        cf_conflicts.append(
            f"Historical rejection of '{matched_alt.name}' questioned: premise '{inv_p.premise_id}' was invalidated by {inv_by}"
        )

        if inv_p.document_ref and inv_p.document_ref != matched_alt.document_ref:
            try:
                inv_passages = snapshot_repo.get_passages_by_document(inv_p.document_ref, limit=1)
                for p in inv_passages:
                    if p.ref not in supporting_refs:
                        supporting_refs.append(p.ref)
                        cf_evidence.append(
                            EvidenceRecord(
                                ref=p.ref,
                                kind="local",
                                publisher=p.relative_path,
                                locator=p.relative_path,
                                citation_locator=f"{p.relative_path}#{p.start_line}-{p.end_line}",
                                digest=f"sha256:{p.source_hash}",
                                extraction_method="markdown_section",
                                authority="primary",
                                authority_rationale=f"Invalidated premise '{inv_p.premise_id}'.",
                                freshness="current",
                                license="permitted",
                                reuse="permitted",
                                conflict="none",
                            )
                        )
            except RepositoryError:
                pass
    elif active_premises and not unassessed_premises:
        verdict = "repeat_of_rejected_architecture"
        rationale = (
            f"Alternative '{matched_alt.name}' was evaluated and {matched_alt.disposition} "
            f"because: {matched_alt.reason}. All supporting premises "
            f"({', '.join(matched_alt.premises)}) remain active."
        )
        cf_conflicts.append(
            f"Task matches rejected architecture '{matched_alt.name}' under active premises: {matched_alt.reason}"
        )
    else:
        verdict = "unassessed_premise"
        rationale = (
            f"Alternative '{matched_alt.name}' was evaluated and {matched_alt.disposition} "
            f"({matched_alt.reason}), but supporting premises have no current verification record."
        )

    disposition_val: Literal["rejected", "deferred", "superseded"] = (
        matched_alt.disposition  # type: ignore[assignment]
        if matched_alt.disposition in ("rejected", "deferred", "superseded")
        else "rejected"
    )

    assessment = CounterfactualAssessment(
        matched_alternative=matched_alt.name,
        decision_ref=matched_alt.document_ref,
        verdict=verdict,
        supporting_evidence=supporting_refs,
        rationale=rationale,
    )

    alt_records = [
        AlternativeRecord(
            name=matched_alt.name,
            disposition=disposition_val,
            reason=matched_alt.reason,
            premises=list(matched_alt.premises),
        )
    ]

    premise_records = []
    for pid in matched_alt.premises:
        p_row = premises_map.get(pid)
        if p_row:
            p_status: Literal["active", "invalidated", "uncertain"] = (
                p_row.status  # type: ignore[assignment]
                if p_row.status in ("active", "invalidated", "uncertain")
                else "uncertain"
            )
            premise_records.append(
                PremiseRecord(
                    id=p_row.premise_id,
                    statement=p_row.statement,
                    status=p_status,
                    invalidated_by=p_row.invalidated_by,
                    rationale=p_row.rationale,
                )
            )

    return assessment, alt_records, premise_records, cf_evidence, cf_conflicts


class InvestigateService:
    """Application use case for orchestrating full investigation requests.

    Orchestrates:
    - Request validation & cursor parsing
    - Task classification & capability/skill discovery
    - Routing (delegating non-proceed outcomes to assemble_context)
    - On proceed:
      - Skill dispatch & capability evidence collection
      - Causal archaeology (why.py)
      - SearchService execution & lineage application
      - Bounded live research
      - Evidence deduplication, pagination, and cursor generation
      - Budget-enforced response compaction
    """

    def __init__(self, deps: ServiceDeps) -> None:
        self._deps = _validate_deps(deps)
        self._snapshot_repo = SnapshotRepository(self._deps.snapshot.database)
        self._search_service = SearchService(
            self._snapshot_repo,
            embed_query=self._deps.embed_query,
            rerank=self._deps.rerank,
            clock=self._deps.clock,
        )

    @property
    def deps(self) -> ServiceDeps:
        return self._deps

    @property
    def snapshot_repo(self) -> SnapshotRepository:
        return self._snapshot_repo

    @property
    def search_service(self) -> SearchService:
        return self._search_service

    def investigate(self, request: InvestigationRequest) -> InvestigationResult:
        """Execute the investigation pipeline."""
        if not isinstance(request, InvestigationRequest):
            raise ServiceError("invalid_request_type")

        request_id = _content_hash(request)
        cursor_offset = 0
        if request.cursor is not None:
            cursor_offset = _decode_investigate_cursor(
                request.cursor, request_id, self._deps.snapshot.build_id
            )

        classification = classify(request)
        opted_in = request.host_skills is not None
        lookup = discover(classification, self._deps.registry, self._deps.skill_set if opted_in else None)
        decision = route(classification, lookup, request)
        pack_gaps = _pack_currency_gaps(lookup)

        if decision.outcome != "proceed":
            return assemble_context(request, decision, mode="full", extra_gaps=pack_gaps)

        return self._proceed(
            request, request_id, cursor_offset, classification, lookup, decision, pack_gaps, opted_in
        )

    def _proceed(
        self,
        request: InvestigationRequest,
        request_id: str,
        cursor_offset: int,
        classification: Any,
        lookup: Any,
        decision: Any,
        pack_gaps: list[str],
        opted_in: bool,
    ) -> InvestigationResult:
        capability_evidence = [_capability_evidence_record(capability) for capability in lookup.capabilities]
        skill_dispatch = (
            dispatch(lookup, request.host_skills or [], ceiling=self._deps.skill_ceiling)
            if opted_in else None
        )
        skill_evidence = [_skill_evidence_record(item) for item in skill_dispatch.skills] if skill_dispatch else []

        causal_evidence: list[EvidenceRecord] = []
        lineage_claims: list[ClaimRecord] = []
        lineage_conflicts: list[str] = []
        warnings: list[str] = []
        degradation: list[str] = []

        if request.code_target:
            c_ev, c_claims, c_conflicts, c_warn, c_deg = _resolve_code_target_causality(
                request.code_target, self._deps.repo, self._snapshot_repo
            )
            causal_evidence = c_ev
            lineage_claims = lineage_claims + c_claims
            lineage_conflicts = lineage_conflicts + c_conflicts
            warnings = warnings + c_warn
            degradation = degradation + c_deg

        prefix_evidence = skill_evidence + causal_evidence + capability_evidence
        cf_assessment, cf_alts, cf_premises, cf_evidence, cf_conflicts = _evaluate_counterfactual(
            request, self._snapshot_repo
        )
        lineage_conflicts = lineage_conflicts + cf_conflicts
        prefix_evidence = prefix_evidence + cf_evidence
        prefix_count = len(prefix_evidence)
        max_evidence = request.budgets.max_evidence

        if cursor_offset < prefix_count:
            page_prefix = prefix_evidence[cursor_offset : cursor_offset + max_evidence]
            remaining_slots = max_evidence - len(page_prefix)
            local_offset = 0
        else:
            page_prefix = []
            remaining_slots = max_evidence
            local_offset = cursor_offset - prefix_count

        outcome = self._search_service.search(
            request.task,
            request.budgets,
            offset=local_offset,
        )
        raw_local_evidence = to_evidence_records(outcome)
        local_evidence, search_claims, search_conflicts = _apply_lineage(raw_local_evidence, self._snapshot_repo)
        lineage_claims = lineage_claims + search_claims
        lineage_conflicts = lineage_conflicts + search_conflicts

        seen_prefix_refs = {rec.ref for rec in page_prefix}
        deduped_local = [rec for rec in local_evidence if rec.ref not in seen_prefix_refs]
        page_local = deduped_local[:remaining_slots]
        warnings = warnings + list(outcome.warnings)
        degradation = degradation + list(outcome.degradation)

        research_outcomes = _run_research(request, self._deps)
        host_actions, research_degradation, research_evidence = _fold_research(research_outcomes)
        degradation = degradation + research_degradation

        available_slots = max_evidence - len(page_prefix) - len(page_local)
        page_research = research_evidence[: max(0, available_slots)]
        evidence = page_prefix + page_local + page_research

        prefix_has_more = (cursor_offset + len(page_prefix) < prefix_count)
        local_has_more = (len(deduped_local) > len(page_local))
        search_has_more = outcome.truncated
        research_has_more = (len(research_evidence) > len(page_research))
        has_more = prefix_has_more or local_has_more or search_has_more or research_has_more

        if has_more:
            next_offset = cursor_offset + len(evidence)
            next_cursor = _encode_investigate_cursor(request_id, self._deps.snapshot.build_id, next_offset)
            truncated = True
            if len(evidence) >= max_evidence:
                degradation.append("max_evidence_exceeded")
        else:
            next_cursor = None
            truncated = False

        status: Literal["complete", "partial", "route_only", "abstained"] = (
            "partial"
            if truncated or research_degradation or any(is_shortfall(note) for note in degradation)
            else "complete"
        )
        extra_gaps = list(pack_gaps)
        if skill_dispatch is not None:
            skill_gaps, skill_actions = _skill_outcomes(skill_dispatch.skills)
            extra_gaps = list(skill_dispatch.gaps) + skill_gaps
            host_actions = host_actions + skill_actions
            drafting = _drafting_action(classification, skill_dispatch)
            if drafting is not None:
                extra_gaps.append(f"no_skill_for_domain:{classification.domain}")
                host_actions = host_actions + [drafting]

        result = InvestigationResult(
            schema_version="1", status=status, request_id=request_id, evidence=evidence,
            claims=lineage_claims, conflicts=lineage_conflicts, gaps=list(decision.gaps) + extra_gaps, host_actions=host_actions,
            warnings=warnings, degradation=degradation, budgets=request.budgets, next_cursor=next_cursor,
            alternatives=cf_alts, premises=cf_premises, counterfactual_assessment=cf_assessment,
        )
        return compact_to_budget(result, request.budgets.max_output_chars)


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
    return InvestigateService(deps).investigate(request)


def _read_one(
    repo: SnapshotRepository, ref: str, requested_range: ReadRange | None, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    content = repo.get_passage_content(ref)
    if content is None:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total

    windowed = _window_text(content.text, requested_range, cursor_start, item_cap, remaining_total)
    if windowed is None:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    window, start, actual_end, truncated = windowed
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=f"sha256:{content.source_hash}", truncated=truncated, next_cursor=next_cursor,
        evidence_kind="local", locator=content.relative_path,
        citation_locator=f"{content.relative_path}#{content.start_line}-{content.end_line}",
        authority="unknown", freshness="unknown", license="unknown", conflict="unknown",
    )
    return item, remaining_total - len(window)


def _read_capability_one(
    registry: Registry, ref: str, requested_range: ReadRange | None, cursor_start: int | None,
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
    skill_set: SkillSet | None, ref: str, requested_range: ReadRange | None, cursor_start: int | None,
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


def _read_live_one(
    research_deps: ResearchDeps | None, ref: str, requested_range: ReadRange | None, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    # Resolves a `live:sha256:<32 hex>` ref -- the refs `fetch.py` mints for captured live evidence
    # and `investigate()` hands back to the client. Until now `read()` had no branch for them, so
    # they fell through to the local passages table, found nothing, and returned `missing_ref`:
    # `investigate_work` returned refs that `read_evidence` could not read, which is precisely the
    # contract `read_evidence` exists to keep ("stable refs returned by investigate_work").
    #
    # Content is the CACHE EXCERPT, never a refetch. Two reasons, and the second is the important
    # one. Refetching would make a read reach the network -- `read_evidence` is documented
    # read-only and resolving "immutable refs", and a ref that re-fetches is not immutable. And
    # the excerpt is already the permitted minimum `build_cache_entry` computed under the reuse
    # rules: when reuse is anything but `permitted` it is capped at 280 characters. Serving
    # anything larger here would route around that cap through a different door.
    if research_deps is None:
        # No research deps means no cache was ever configured for this process, so there is
        # nowhere a live ref could resolve. Typed as missing rather than as an error: from the
        # client's side the ref genuinely is not here.
        return ReadItem(ref=ref, status="missing_ref"), remaining_total

    lookup = find_by_ref(research_deps.cache_dir, ref, now=research_deps.now())
    if lookup.expired:
        # `expired_ref` was declared in the contract and documented as structurally unreachable
        # from 7A's deps, because a single active snapshot has no history to age out. Cached live
        # evidence does, so this is the status becoming reachable rather than a new one appearing.
        # Content is withheld: expired material must never be presented as current.
        return ReadItem(ref=ref, status="expired_ref"), remaining_total
    if not lookup.hit or lookup.entry is None:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total

    entry = lookup.entry
    windowed = _window_text(entry.excerpt, requested_range, cursor_start, item_cap, remaining_total)
    if windowed is None:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    window, start, actual_end, truncated = windowed
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    evidence = entry.evidence
    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=evidence.digest, truncated=truncated, next_cursor=next_cursor,
        captured_at=evidence.retrieved_at,
        evidence_kind="captured_live", locator=evidence.locator,
        citation_locator=evidence.citation_locator,
        provenance_chain=list(evidence.provenance_chain),
        # Carried from the record `investigate()` already returned, never re-derived here: the two
        # tools must not be able to disagree about the authority of one piece of evidence.
        authority=evidence.authority, freshness=evidence.freshness,
        license=evidence.license, conflict=evidence.conflict,
    )
    return item, remaining_total - len(window)


class ReadService:
    """Application use case for resolving immutable evidence references across sources.

    Polymorphically resolves:
    - `skill:<id>@<version>` via SkillSet
    - `capability:<id>` via Registry
    - `live:sha256:<hash>` via ResearchDeps cache
    - `<passage_ref>` via SnapshotRepository
    """

    def __init__(
        self,
        repository: SnapshotRepository,
        registry: Registry,
        *,
        skill_set: SkillSet | None = None,
        research_deps: ResearchDeps | None = None,
    ) -> None:
        self._repo = repository
        self._registry = registry
        self._skill_set = skill_set
        self._research_deps = research_deps

    @property
    def repository(self) -> SnapshotRepository:
        return self._repo

    @property
    def registry(self) -> Registry:
        return self._registry

    def read(self, request: ReadRequest) -> ReadResult:
        """Resolve each `refs` entry to immutable evidence and return exact, budget-bounded content."""
        if not isinstance(request, ReadRequest):
            raise ServiceError("invalid_request_type")

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
            cursor_ref, cursor_start = str(decoded["ref"]), int(decoded["start"])

        ranges_by_ref = {item.ref: item for item in request.ranges}
        remaining = request.budgets.max_output_chars
        items: list[ReadItem] = []
        try:
            for ref in request.refs:
                rng = ranges_by_ref.get(ref)
                pos = cursor_start if ref == cursor_ref else None
                cap = request.budgets.max_extracted_chars
                if ref.startswith(_SKILL_REF_PREFIX):
                    item, remaining = self._read_skill(ref, rng, pos, cap, remaining, request_id)
                elif ref.startswith(_CAPABILITY_REF_PREFIX):
                    item, remaining = self._read_capability(ref, rng, pos, cap, remaining, request_id)
                elif ref.startswith(_LIVE_REF_PREFIX):
                    item, remaining = self._read_live(ref, rng, pos, cap, remaining, request_id)
                else:
                    item, remaining = self._read_passage(ref, rng, pos, cap, remaining, request_id)
                items.append(item)
        except (RepositoryError, sqlite3.DatabaseError) as error:
            raise ServiceError("snapshot_unreadable") from error

        warnings = ["output_budget_exhausted"] if remaining <= 0 and any(item.truncated for item in items) else []
        next_cursor = next((item.next_cursor for item in items if item.next_cursor), None)
        return ReadResult(
            schema_version="1", request_id=request_id, items=items,
            warnings=warnings, budgets=request.budgets, next_cursor=next_cursor,
        )

    def _read_passage(
        self,
        ref: str,
        requested_range: ReadRange | None,
        cursor_start: int | None,
        item_cap: int,
        remaining_total: int,
        request_id: str,
    ) -> tuple[ReadItem, int]:
        return _read_one(self._repo, ref, requested_range, cursor_start, item_cap, remaining_total, request_id)

    def _read_capability(
        self,
        ref: str,
        requested_range: ReadRange | None,
        cursor_start: int | None,
        item_cap: int,
        remaining_total: int,
        request_id: str,
    ) -> tuple[ReadItem, int]:
        return _read_capability_one(self._registry, ref, requested_range, cursor_start, item_cap, remaining_total, request_id)

    def _read_skill(
        self,
        ref: str,
        requested_range: ReadRange | None,
        cursor_start: int | None,
        item_cap: int,
        remaining_total: int,
        request_id: str,
    ) -> tuple[ReadItem, int]:
        return _read_skill_one(self._skill_set, ref, requested_range, cursor_start, item_cap, remaining_total, request_id)

    def _read_live(
        self,
        ref: str,
        requested_range: ReadRange | None,
        cursor_start: int | None,
        item_cap: int,
        remaining_total: int,
        request_id: str,
    ) -> tuple[ReadItem, int]:
        return _read_live_one(self._research_deps, ref, requested_range, cursor_start, item_cap, remaining_total, request_id)


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
    service = ReadService(
        repository=SnapshotRepository(deps.snapshot.database),
        registry=deps.registry,
        skill_set=deps.skill_set,
        research_deps=deps.research,
    )
    return service.read(request)


__all__ = ["InvestigateService", "ReadService", "ServiceDeps", "ServiceError", "investigate", "read"]
