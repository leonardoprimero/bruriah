# Deterministic route-or-abstain decision (Slice 6B-3). Consumes a `RequestClassification`
# (6B-1) and a `LookupResult` (6B-2) -- plus the caller's own `InvestigationRequest`, for its
# declared `risk_class` and `as_of` -- and decides whether investigation may PROCEED, is
# `route_only`, or `abstained`. Pure: no I/O, no network, no retrieval, no evidence
# verification, no InvestigationResult assembly, and it never concludes anything -- design.md
# "Routing/retrieval": "classifies intent, domain, claim type, risk, and jurisdiction; it never
# concludes" and "Generic discovery only routes or abstains."
#
# Decision vocabulary and precedence (smallest defensible rule set; tasks.md names the four
# triggers this rule set must order: consequential action, no-pack, missing-jurisdiction,
# regulated-without-context):
#
# RULE 1 -- Consequential action boundary (highest precedence, domain-agnostic). "Read-Only
#    Informational Boundary and Host Actions" / "Consequential action requested": credentials, a
#    write, execution, or licensed judgment must never receive a normal answer.
#    `intent == "consequential_action"` always yields at most `route_only` (evidence exists to
#    point at) or `abstained` (nothing does); never downgraded by anything below, never `proceed`.
#
# RULE 2 -- Domain-agnostic no-evidence gate (BINDING CONSTRAINT from 6B-1 verification, recorded
#    in tasks.md). `has_evidence = lookup.domain_supported or bool(lookup.sources) or
#    bool(lookup.capabilities)` inspects only 6B-2's structural output, never
#    `classification.domain`, so it runs IDENTICALLY for `"general"`, `"unsupported"`, or any
#    named family. False -> `abstained`. "Domain-Sensitive Outcomes..." / "Arbitrary unsupported
#    profession": "no applicable approved domain pack ... status is route_only or abstained."
#    `lookup.domain_supported` is False for every domain today (see lookup.py), so this is the
#    only reachable abstention path unless a `capability_recommendation` also matched
#    (capabilities are not domain-gated, per lookup.py rule 3, and count as evidence here).
#
# RULE 3 -- Regulated domain missing context (only reached once RULE 2 confirms evidence exists).
#    `effective_risk == "regulated"` and either `classification.jurisdiction == "unknown"` or
#    `request.as_of is None` -> `route_only`, naming each missing item as its own gap.
#    "Domain-Sensitive Outcomes..." / "Supported regulated domain lacks context": "a legal task
#    lacks jurisdiction or applicable effective date ... returns a named gap and professional
#    escalation." Merged into one rule/outcome, not two, since the scenario treats both as one
#    requirement; each gap keeps its own closed name for precision.
#
# RULE 4 -- Jurisdiction-sensitive sources present but inapplicable. Domain-applicable sources
#    exist (`lookup.sources` non-empty) but NONE is `jurisdiction_applicable` -> `route_only`.
#    "Domain-Sensitive Outcomes..." / "Accounting authority is jurisdiction-sensitive":
#    "preserves those refs as non-applicable" -- never silently answer with the wrong jurisdiction.
#
# RULE 5 (default) -- `proceed`: evidence exists, no consequential action, no missing regulated
#    context, no jurisdiction mismatch. "Proceed" is not an `InvestigationResult.status` value --
#    it signals a later stage (Slices 10-11) may continue toward `complete`/`partial`;
#    `route_only`/`abstained` here ARE the exact `InvestigationResult.status` values a later
#    stage may use unchanged.
#
# Not routing decisions (satisfied elsewhere): "Cybersecurity evidence lacks authorization" (don't
# recommend/execute exploitation), "Programming guidance is version-bound" (mark mismatch), and
# "UX and web evidence types differ" (distinguish standards from research) are evidence-
# NORMALIZATION duties for the future evidence assembler (Slice 10) -- this module structurally
# cannot violate them: it has no free-text or conclusion field to begin with.
#
# Effective risk (BINDING CONSTRAINT: caller's `risk_class` is a FLOOR, never silently lowered).
# `effective_risk = classification.risk` unless `request.risk_class` ranks higher, in which case
# the declared value wins. Rank: low < medium < high < unknown < regulated. `regulated` is the
# ceiling: `unknown` is an EPISTEMIC state ("the classifier could not assess"), assessed only for
# `domain=="unsupported"`, and MUST NOT absorb a caller's specific, more-informed `regulated`
# declaration -- otherwise declaring `regulated` on an unknown-risk request would have zero
# routing effect (RULE 3 keys on `regulated` exactly), the opposite of a floor. `unknown` still
# outranks low/medium/high, so "could not assess" stays more cautious than any assessed non-
# regulated level.
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .classify import RequestClassification, RiskLevel
from .contracts import InvestigationRequest
from .lookup import LookupResult

RouteOutcome = Literal["proceed", "route_only", "abstained"]
RouteReason = Literal[
    "consequential_action_requires_host_action",
    "no_approved_pack_or_local_evidence",
    "regulated_domain_missing_context",
    "sources_not_jurisdiction_applicable",
    "domain_supported_with_applicable_evidence",
]
Gap = Literal[
    "no_approved_domain_pack",
    "missing_jurisdiction",
    "missing_effective_date",
    "sources_jurisdiction_mismatch",
]

_RISK_RANK: dict[RiskLevel, int] = {"low": 0, "medium": 1, "high": 2, "unknown": 3, "regulated": 4}


class RouteError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RouteDecision:
    """Closed, read-only routing decision. Every field is a fixed Literal/bool/tuple-of-Literal
    value; there is no open text field, so nothing from the request or classification can be
    echoed back as a conclusion."""

    outcome: RouteOutcome
    reason: RouteReason
    effective_risk: RiskLevel
    gaps: tuple[Gap, ...]
    requires_escalation: bool


def _effective_risk(assessed: RiskLevel, declared: Literal["low", "medium", "high", "regulated"]) -> RiskLevel:
    # `classification.risk` comes from a plain (runtime-unvalidated) dataclass, so an out-of-range
    # value must fail typed here rather than as a bare KeyError from the rank lookup.
    if assessed not in _RISK_RANK or declared not in _RISK_RANK:
        raise RouteError("invalid_risk_level")
    return assessed if _RISK_RANK[assessed] >= _RISK_RANK[declared] else declared


def route(classification: RequestClassification, lookup: LookupResult, request: InvestigationRequest) -> RouteDecision:
    """Decide whether `classification`/`lookup` may proceed to investigation, is route-only, or
    must abstain. Pure and deterministic: same inputs always yield the same `RouteDecision`.
    """
    if not isinstance(classification, RequestClassification):
        raise RouteError("invalid_classification_type")
    if not isinstance(lookup, LookupResult):
        raise RouteError("invalid_lookup_type")
    if not isinstance(request, InvestigationRequest):
        raise RouteError("invalid_request_type")

    effective_risk = _effective_risk(classification.risk, request.risk_class)
    has_evidence = lookup.domain_supported or bool(lookup.sources) or bool(lookup.capabilities)

    if classification.intent == "consequential_action":
        outcome: RouteOutcome = "route_only" if has_evidence else "abstained"
        gaps: tuple[Gap, ...] = () if has_evidence else ("no_approved_domain_pack",)
        return RouteDecision(outcome, "consequential_action_requires_host_action", effective_risk, gaps, True)

    if not has_evidence:
        return RouteDecision("abstained", "no_approved_pack_or_local_evidence", effective_risk,
                              ("no_approved_domain_pack",), True)

    if effective_risk == "regulated":
        context_gaps: list[Gap] = []
        if classification.jurisdiction == "unknown":
            context_gaps.append("missing_jurisdiction")
        if request.as_of is None:
            context_gaps.append("missing_effective_date")
        if context_gaps:
            return RouteDecision("route_only", "regulated_domain_missing_context", effective_risk,
                                  tuple(context_gaps), True)

    if lookup.sources and all(not match.jurisdiction_applicable for match in lookup.sources):
        return RouteDecision("route_only", "sources_not_jurisdiction_applicable", effective_risk,
                              ("sources_jurisdiction_mismatch",), True)

    return RouteDecision("proceed", "domain_supported_with_applicable_evidence", effective_risk, (), False)


__all__ = ["Gap", "RouteDecision", "RouteError", "RouteOutcome", "RouteReason", "route"]
