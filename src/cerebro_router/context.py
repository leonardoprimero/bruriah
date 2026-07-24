# Slice 11: the bounded context assembler + vendor-neutral host actions, standalone (composition
# into `service.py`'s `investigate()` pipeline is Slice 12's cutover job, matching how 9B
# delivered `research.py` and 10 delivered `evidence.py` unwired). Composes over the frozen
# `contracts.InvestigationResult`/`HostAction`, `route.RouteDecision`, `evidence.ClaimAssessment`,
# and `research.ResearchOutcome` -- never mutates or amends any of them; every new typed structure
# this assembler needs lives here. design.md Architecture: "... -> evidence normalizer/verifier
# -> context assembler / host plan <---- unavailable/unsafe work ----|" and "Files and
# Verification" Slice 11: "Context assembly, host actions, refusal/escalation | Route-only mode."
#
# Binding guarantees this module enforces:
#   1. ROUTE-GATED CONCLUSIONS: `route_decision.outcome` (`route.py`, already-computed) decides
#      the ceiling. `abstained`/`route_only` NEVER carry evidence or claims -- only `gaps`,
#      escalation `host_actions`, and safety `warnings` -- so a regulated-context gap or an
#      unsupported profession can never be dressed up as a conclusion (spec "Domain-Sensitive
#      Outcomes..." / "Supported regulated domain lacks context", "Arbitrary unsupported
#      profession"). Only `outcome == "proceed"` may assemble evidence/claims.
#   2. FIVE VENDOR-NEUTRAL HOST ACTIONS, PROPOSED NEVER EXECUTED: `web_search`/`fetch_public_url`
#      are reused verbatim from `research.ResearchOutcome.host_actions` (`research.py` already
#      builds them without fetching); `inspect_capability`/`request_jurisdiction`/
#      `consult_professional` are synthesized here from `route_decision.gaps` and per-claim
#      `ClaimAssessment.uncertainty` reasons. This module never calls a search engine, never
#      executes a capability, and never performs a fetch of its own -- it only ever consumes an
#      already-computed `ResearchOutcome` (spec "Read-Only Informational Boundary and Host
#      Actions").
#   3. PROFESSIONAL ESCALATION, NEVER A FABRICATED CONCLUSION: `_GAP_HOST_ACTIONS` maps every
#      named `route.Gap` (and the matching `ClaimAssessment.uncertainty` reasons) to
#      `request_jurisdiction`/`consult_professional`; a `consequential_action` route reason always
#      adds a `consequential_action_refused_no_action_performed` warning plus an
#      `inspect_capability` host action -- the host is told to inspect/act itself, this tool never
#      does (spec "Consequential action requested").
#   4. OUTPUT-BUDGET COMPACTION NEVER DROPS REFS/CONFLICTS/WARNINGS: `compact_to_budget` only ever
#      removes evidence records NOT cited by any claim's `supporting_refs`/`conflicting_refs`
#      (`_protected_refs`), in a fixed deterministic order (reverse-sorted ref). `conflicts`,
#      `warnings`, `gaps`, `host_actions`, and every claim-cited evidence ref survive compaction
#      unconditionally -- retaining them is the entire point (spec "Bounded Investigation,
#      Pagination, and Stable References" / "Budget exhaustion": "stops, returns bounded partial
#      results ... without dropping refs").
#   5. ROLLBACK = FORCED ROUTE-ONLY MODE: `mode="route_only"` ignores `assessments`/
#      `evidence_pool`/`research_outcomes` entirely and assembles ONLY the routing decision (no
#      live research, no assembled conclusions) -- the safe fallback design.md names for Slice 11.
#      A genuinely `abstained` route decision stays `abstained` even under rollback (there is
#      nothing to "route to"); rollback only ever narrows `proceed` down to `route_only`, never
#      widens anything.
#   6. TOTAL AND TYPED: `assemble_context`'s public entry never raises -- a typed `ContextError` or
#      any unenumerated exception is caught and converted into a safe `abstained`
#      `InvestigationResult` with a named `warnings` reason (closes the untyped-escape defect
#      class this change has hit repeatedly in prior slices).
#   7. ZERO WRITES/INSTALLS/EXECUTION: this module performs no filesystem, network, or subprocess
#      I/O of its own -- it is a pure data-transformation layer over already-computed inputs
#      (`RouteDecision`, `ClaimAssessment`, `EvidenceRecord`, `ResearchOutcome`). No wall-clock
#      call: nothing here needs the current date or time (freshness/timing already happened in
#      `evidence.py`/`research.py`), so determinism never depends on an un-injected clock.
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Literal

from .contracts import (
    Budgets, ClaimRecord, EvidenceRecord, HostAction, InvestigationRequest, InvestigationResult,
)
from .evidence import ClaimAssessment, render_claim_record
from .research import ResearchOutcome
from .route import RouteDecision

AssemblyMode = Literal["full", "route_only"]
ContextStatus = Literal["complete", "partial", "route_only", "abstained"]

_FALLBACK_REQUEST_ID = f"sha256:{'0' * 64}"


class ContextError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# --- Gap -> vendor-neutral escalation host action (guarantee #3) -------------------------------
# Keys cover BOTH `route.Gap` values (`route_decision.gaps`) and the overlapping subset of
# `evidence.ClaimAssessment.uncertainty` reasons that name the same missing context at the
# individual-claim level; a reason with no escalation mapping (e.g. "evidence_disagrees",
# "authority_unknown") simply contributes no host action, never a KeyError.
_GAP_HOST_ACTIONS: dict[str, HostAction] = {
    "no_approved_domain_pack": HostAction(kind="consult_professional", reason="no_approved_domain_pack"),
    "missing_jurisdiction": HostAction(kind="request_jurisdiction", reason="missing_jurisdiction"),
    "missing_effective_date": HostAction(kind="request_jurisdiction", reason="missing_effective_date"),
    "sources_jurisdiction_mismatch": HostAction(kind="consult_professional", reason="sources_jurisdiction_mismatch"),
}
_CONSEQUENTIAL_ACTION_HOST_ACTION = HostAction(
    kind="inspect_capability", reason="consequential_action_requires_host_execution",
)


def _request_id(request: InvestigationRequest) -> str:
    """Deterministic request identity, mirroring `research.py`'s own private `_request_id`
    (duplicated on purpose: a frozen module's private helper is never imported across the module
    boundary -- every new helper this slice needs lives here, per the module docstring)."""
    canonical = json.dumps(
        request.model_dump(mode="json", exclude={"cursor"}), sort_keys=True, separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _escalation_host_actions(gaps: Sequence[str]) -> list[HostAction]:
    actions: list[HostAction] = []
    for gap in gaps:
        action = _GAP_HOST_ACTIONS.get(gap)
        if action is not None and action not in actions:
            actions.append(action)
    return actions


def _route_gated_result(
    request: InvestigationRequest, route_decision: RouteDecision, request_id: str, *, forced: bool,
) -> InvestigationResult:
    """`route_decision.outcome in ("route_only", "abstained")`, or rollback `mode="route_only"`
    forcing a `proceed` decision down to route-only (guarantees #1 and #5): assemble ONLY the
    routing decision. Zero evidence, zero claims -- ever."""
    status: Literal["route_only", "abstained"] = (
        "abstained" if route_decision.outcome == "abstained" else "route_only"
    )
    host_actions = _escalation_host_actions(route_decision.gaps)
    warnings: list[str] = []
    degradation: list[str] = ["rollback_route_only_mode"] if forced and route_decision.outcome == "proceed" else []
    if route_decision.reason == "consequential_action_requires_host_action":
        warnings.append("consequential_action_refused_no_action_performed")
        if _CONSEQUENTIAL_ACTION_HOST_ACTION not in host_actions:
            host_actions.append(_CONSEQUENTIAL_ACTION_HOST_ACTION)
    result = InvestigationResult(
        schema_version="1", status=status, request_id=request_id, evidence=[], claims=[],
        conflicts=[], gaps=list(route_decision.gaps), host_actions=host_actions, warnings=warnings,
        degradation=degradation, budgets=request.budgets, next_cursor=None,
    )
    return compact_to_budget(result, request.budgets.max_output_chars)


def _assembled_result(
    request: InvestigationRequest, request_id: str, assessments: Sequence[ClaimAssessment],
    evidence_pool: Sequence[EvidenceRecord], research_outcomes: Sequence[ResearchOutcome],
) -> InvestigationResult:
    """`route_decision.outcome == "proceed"` under `mode="full"` (guarantee #1's only reachable
    assembly path): render each `ClaimAssessment` to a frozen `ClaimRecord`, resolve every ref a
    claim cites against `evidence_pool` (typed failure if a ref cannot be resolved -- never a
    silently missing citation), fold in successfully retrieved live research evidence, and
    determine `complete`/`partial` from the ACTUAL assembled state -- never a caller-declared
    confidence."""
    evidence_by_ref: dict[str, EvidenceRecord] = {}
    for record in evidence_pool:
        if not isinstance(record, EvidenceRecord):
            raise ContextError("invalid_evidence_pool_item")
        evidence_by_ref.setdefault(record.ref, record)

    claims: list[ClaimRecord] = []
    conflicts: list[str] = []
    gaps: list[str] = []
    host_actions: list[HostAction] = []
    referenced_refs: list[str] = []

    for assessment in assessments:
        if not isinstance(assessment, ClaimAssessment):
            raise ContextError("invalid_assessment_type")
        claims.append(render_claim_record(assessment))
        for ref in (*assessment.supporting_refs, *assessment.conflicting_refs, *assessment.non_applicable_refs):
            if ref not in referenced_refs:
                referenced_refs.append(ref)
        if assessment.state == "conflicted":
            classes = ", ".join(assessment.conflict_classes) if assessment.conflict_classes else "unspecified"
            conflicts.append(f"Claim '{assessment.claim_text}' has conflicting evidence ({classes}).")
        for reason in assessment.uncertainty:
            if reason not in gaps:
                gaps.append(reason)
            action = _GAP_HOST_ACTIONS.get(reason)
            if action is not None and action not in host_actions:
                host_actions.append(action)

    evidence: list[EvidenceRecord] = []
    for ref in referenced_refs:
        record = evidence_by_ref.get(ref)
        if record is None:
            raise ContextError("evidence_ref_not_in_pool")
        evidence.append(record)

    degradation: list[str] = []
    seen_evidence_refs = {item.ref for item in evidence}
    for outcome in research_outcomes:
        if not isinstance(outcome, ResearchOutcome):
            raise ContextError("invalid_research_outcome_type")
        if outcome.status in ("cached", "fetched") and outcome.evidence is not None:
            if outcome.evidence.ref not in seen_evidence_refs:
                evidence.append(outcome.evidence)
                seen_evidence_refs.add(outcome.evidence.ref)
        else:
            # Unavailable/refused/degraded live research is a named gap in `degradation`, never a
            # silent drop -- the ALREADY-COMPUTED `HostAction`s `research.py` returned for exactly
            # this outcome are folded straight through (guarantee #2: reused, never re-derived).
            degradation.append(f"research_unavailable:{outcome.code}")
            for action in outcome.host_actions:
                if action not in host_actions:
                    host_actions.append(action)

    warnings: list[str] = []
    if "authority_unknown" in gaps:
        warnings.append("unverified_authority_evidence_present")

    if not evidence and not claims:
        if "no_evidence_available" not in gaps:
            gaps.append("no_evidence_available")

    all_supported = bool(assessments) and all(item.state == "supported" for item in assessments)
    status: Literal["complete", "partial"] = (
        "complete" if all_supported and evidence and not gaps and not degradation else "partial"
    )

    result = InvestigationResult(
        schema_version="1", status=status, request_id=request_id, evidence=evidence, claims=claims,
        conflicts=conflicts, gaps=gaps, host_actions=host_actions, warnings=warnings,
        degradation=degradation, budgets=request.budgets, next_cursor=None,
    )
    return compact_to_budget(result, request.budgets.max_output_chars)


def _protected_refs(result: InvestigationResult) -> frozenset[str]:
    refs: set[str] = set()
    for claim in result.claims:
        refs.update(claim.supporting_refs)
        refs.update(claim.conflicting_refs)
    return frozenset(refs)


def compact_to_budget(result: InvestigationResult, max_output_chars: int) -> InvestigationResult:
    """Deterministically shrink `result` toward `max_output_chars` (guarantee #4). Only evidence
    records NOT cited by any claim's `supporting_refs`/`conflicting_refs` may be dropped, in a
    fixed order (reverse-sorted `ref`, so repeated calls on identical input always drop the same
    items in the same order). `conflicts`, `warnings`, `gaps`, `host_actions`, and every
    claim-cited evidence ref are NEVER removed -- if the budget still cannot be met once every
    droppable item is gone, the result is returned as-is rather than sacrificing protected data."""
    if len(result.model_dump_json()) <= max_output_chars:
        return result

    protected = _protected_refs(result)
    kept = list(result.evidence)
    droppable_refs = sorted((item.ref for item in kept if item.ref not in protected), reverse=True)
    dropped = 0
    for ref in droppable_refs:
        kept = [item for item in kept if item.ref != ref]
        dropped += 1
        if len(result.model_copy(update={"evidence": kept}).model_dump_json()) <= max_output_chars:
            break

    if dropped == 0:
        return result

    compacted_status: ContextStatus = "partial" if result.status == "complete" else result.status
    return result.model_copy(update={
        "evidence": kept,
        "status": compacted_status,
        "degradation": [*result.degradation, "output_budget_compacted"],
        "next_cursor": f"ctx-compacted:{result.request_id}:{dropped}",
    })


def _fallback_result(request: object, code: str) -> InvestigationResult:
    """Guarantee #6's typed-total backstop payload: a safe, zero-conclusion `abstained` result
    that never fabricates a request identity or budget it cannot honestly derive."""
    request_id = _FALLBACK_REQUEST_ID
    budgets = Budgets()
    if isinstance(request, InvestigationRequest):
        try:
            request_id = _request_id(request)
            budgets = request.budgets
        except Exception:
            request_id, budgets = _FALLBACK_REQUEST_ID, Budgets()
    return InvestigationResult(
        schema_version="1", status="abstained", request_id=request_id, evidence=[], claims=[],
        conflicts=[], gaps=["assembly_failed"], host_actions=[], warnings=[code], degradation=[],
        budgets=budgets, next_cursor=None,
    )


def _assemble_context_inner(
    request: InvestigationRequest, route_decision: RouteDecision, assessments: Sequence[ClaimAssessment],
    evidence_pool: Sequence[EvidenceRecord], research_outcomes: Sequence[ResearchOutcome], *, mode: AssemblyMode,
) -> InvestigationResult:
    if not isinstance(request, InvestigationRequest):
        raise ContextError("invalid_request_type")
    if not isinstance(route_decision, RouteDecision):
        raise ContextError("invalid_route_decision_type")
    if not isinstance(assessments, (list, tuple)):
        raise ContextError("invalid_assessments_type")
    if not isinstance(evidence_pool, (list, tuple)):
        raise ContextError("invalid_evidence_pool_type")
    if not isinstance(research_outcomes, (list, tuple)):
        raise ContextError("invalid_research_outcomes_type")
    if mode not in ("full", "route_only"):
        raise ContextError("invalid_mode")

    request_id = _request_id(request)

    if mode == "route_only" or route_decision.outcome != "proceed":
        return _route_gated_result(request, route_decision, request_id, forced=mode == "route_only")

    return _assembled_result(request, request_id, assessments, evidence_pool, research_outcomes)


def assemble_context(
    request: InvestigationRequest,
    route_decision: RouteDecision,
    assessments: Sequence[ClaimAssessment] = (),
    evidence_pool: Sequence[EvidenceRecord] = (),
    research_outcomes: Sequence[ResearchOutcome] = (),
    *,
    mode: AssemblyMode = "full",
) -> InvestigationResult:
    """Total, typed context-assembly entry point (guarantee #6): never raises. Deterministic --
    identical arguments always yield an identical `InvestigationResult`. Reads only its already-
    computed arguments; performs no I/O, network, install, execution, or wall-clock access of its
    own (guarantee #7). A typed `ContextError` or any unenumerated exception is converted into a
    safe `abstained` result with a named `warnings` reason -- never a manufactured `complete`.
    """
    try:
        return _assemble_context_inner(request, route_decision, assessments, evidence_pool, research_outcomes, mode=mode)
    except ContextError as error:
        return _fallback_result(request, error.code)
    except Exception:  # Backstop (#6): no bare exception may ever escape `assemble_context()`.
        return _fallback_result(request, "internal_error")


__all__ = [
    "AssemblyMode", "ContextError", "ContextStatus", "assemble_context", "compact_to_budget",
]
