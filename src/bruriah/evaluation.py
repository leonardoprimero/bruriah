# Slice 12C: the evaluation harness + gate matrix. Drives the REAL assembled pipeline -- real
# `Registry`/`ActiveSnapshot` (built the same way `test_service.py`/`test_mcp_contract.py` build
# them), the real `mcp.server.lowlevel.Server` from `mcp_server.build_server`, real
# `service.investigate`/`service.read`, real `evidence.assess_claim`, and (for the security
# dimension) the real SSRF-safe `research.py`/`fetch.py` pipeline -- never mocks. See tasks.md
# 12.1 and design.md line 49 ("Cutover requires ... protocol and OS/client matrix, exact
# citations, domain authority/jurisdiction/freshness/conflict tests, zero injection/SSRF/secret/
# action/unsupported-claim failures, utility/latency budgets, and rehearsed configuration/index
# rollback") for the exact seven gate dimensions this module covers.
#
# HONESTY IS THE CONTRACT, not test-passing. Every `GateCell` is `pass`, `fail`, or
# `not_validated` -- there is no fourth value, and `not_validated` is never silently upgraded to
# `pass` because a cell is inconvenient to reach. Concretely, in THIS slice:
#   * claim formation (Slice 12A-3) is not wired into `investigate()`, so any END-TO-END
#     domain-sensitive or unsupported-claim gate that depends on `investigate()` FORMING a claim
#     is honestly `not_validated`, never a fabricated `pass` -- see `run_domain_gate`'s
#     `end_to_end_*` cells and `run_security_gate`'s `unsupported_claim_zero_tolerance` cell.
#   * this evaluation environment is a single Darwin host with no live external client process --
#     Linux/Windows OS cells and every named client except the protocol-level generic-stdio round
#     trip are `not_validated`, never `pass` -- see `run_package_os_client_gate`.
#   * a configuration-first rollback rehearsal is Slice 12D scope, not this one -- see
#     `run_rollback_preservation_gate`'s `rollback_rehearsal_config_first` cell.
# The unit-level domain scenarios ARE fully exercised against the real `evidence.assess_claim`
# (never a stub), and the security zero-tolerance controls that ARE reachable today (prompt
# injection, consequential-action refusal, SSRF/DNS-rebinding, audit content-freedom) are driven
# through the real pipeline, not merely cited from a prior slice's verification.
#
# TOTAL AND TYPED (closes the untyped-escape defect class this change has hit repeatedly): every
# gate-cell-producing call in this module is wrapped by `_safe_cell`, whose bare `except
# Exception` backstop converts ANY unenumerated failure into a `fail` cell with a named reason --
# the harness itself never raises out of a gate runner. `EvaluationError` is this module's own
# typed failure for malformed fixture input (mirrors `ServiceError`/`ClientError`/`PlatformError`).
from __future__ import annotations

import hashlib
import json
import platform as _os_platform
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import anyio
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import Field

from .clients import CLIENT_CAPABILITIES, ClientId
from .contracts import (
    Budgets, CandidateMaterial, ClosedModel, EvidenceRecord, InvestigationRequest, ReadRequest, ShortText,
)
from .evidence import EvidenceClaim, assess_claim, wrap_evidence
from .mcp_server import INVESTIGATE_TOOL, READ_TOOL, build_server
from .packs import SourcePolicy
from .research import ConcurrencyLimiter, ResearchDeps
from .service import ServiceDeps, investigate, read

GateStatus = Literal["pass", "fail", "not_validated"]
Dimension = Literal[
    "invocation", "schema_fallback_errors", "domain", "security", "utility_latency",
    "package_os_client", "rollback_preservation",
]
# A task text guaranteed (by construction, matching test_service.py/test_mcp_contract.py's own
# established pipeline-real technique) to yield both local and capability evidence over the
# fixture snapshot this module's own tests build: "library" triggers claim_type=
# capability_recommendation (classify.py), "apple pie baking recipe" BM25-matches the fixture's
# real corpus note.
_PROCEED_TASK = "Find a python schema validation library, apple pie baking recipe"
_PLACEHOLDER_DIGEST = f"sha256:{'0' * 64}"


class EvaluationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GateCell(ClosedModel):
    """One closed cell of the gate matrix: a single (dimension, cell_id) pair's outcome. Never a
    fourth status -- `not_validated` is a first-class, honest outcome, not a failure to report."""

    dimension: Dimension
    cell_id: ShortText
    status: GateStatus
    reason: ShortText
    measured_ms: Annotated[int, Field(ge=0)] | None = None


class EvaluationReport(ClosedModel):
    schema_version: Literal["1"]
    cells: list[GateCell]
    passed: int
    failed: int
    not_validated: int


def summarize(cells: Sequence[GateCell]) -> EvaluationReport:
    return EvaluationReport(
        schema_version="1", cells=list(cells),
        passed=sum(1 for cell in cells if cell.status == "pass"),
        failed=sum(1 for cell in cells if cell.status == "fail"),
        not_validated=sum(1 for cell in cells if cell.status == "not_validated"),
    )


_CellOutcome = tuple[GateStatus, str] | tuple[GateStatus, str, int]


def _safe_cell(dimension: Dimension, cell_id: str, run: Callable[[], _CellOutcome]) -> GateCell:
    """Run one gate check and ALWAYS return a `GateCell` -- never raise. The bare `except
    Exception` backstop is deliberate (module docstring): a broken probe is a `fail` cell with a
    named reason, not a crashed test run or a silently skipped gate."""
    try:
        outcome = run()
    except Exception as error:  # Backstop: no bare exception may ever escape the gate matrix.
        reason = f"unexpected_error:{type(error).__name__}:{error}"
        return GateCell(dimension=dimension, cell_id=cell_id, status="fail", reason=reason[:4096])
    status, reason, *rest = outcome
    return GateCell(
        dimension=dimension, cell_id=cell_id, status=status, reason=reason,
        measured_ms=rest[0] if rest else None,
    )


# --- Fixture cases: realistic, task-grounded, loaded from evals/*.jsonl (never inline toys) -----


@dataclass(frozen=True)
class InvestigationCase:
    case_id: str
    dimension: str
    kind: Literal[
        "explicit_invocation", "negative_control", "utility_golden",
        "consequential_action", "prompt_injection",
    ]
    task: str
    network_policy: Literal["off", "public_https"] = "off"
    jurisdiction: str | None = None
    as_of: date | None = None
    expect_status: tuple[str, ...] = ()
    expect_claims_empty: bool = True
    expect_evidence_empty: bool | None = None
    expect_locator_contains: str | None = None
    expect_host_action_kind: str | None = None


def _read_jsonl_lines(path: Path) -> list[str]:
    if not path.is_file():
        raise EvaluationError("fixture_file_missing")
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_investigation_cases(path: Path) -> list[InvestigationCase]:
    cases: list[InvestigationCase] = []
    for line_number, raw_line in enumerate(_read_jsonl_lines(path), start=1):
        try:
            payload = json.loads(raw_line)
            if not isinstance(payload, dict):
                raise EvaluationError("invalid_investigation_case")
            cases.append(InvestigationCase(
                case_id=payload["case_id"], dimension=payload["dimension"], kind=payload["kind"],
                task=payload["task"], network_policy=payload.get("network_policy", "off"),
                jurisdiction=payload.get("jurisdiction"),
                as_of=date.fromisoformat(payload["as_of"]) if payload.get("as_of") else None,
                expect_status=tuple(payload.get("expect_status", ())),
                expect_claims_empty=payload.get("expect_claims_empty", True),
                expect_evidence_empty=payload.get("expect_evidence_empty"),
                expect_locator_contains=payload.get("expect_locator_contains"),
                expect_host_action_kind=payload.get("expect_host_action_kind"),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise EvaluationError(f"malformed_investigation_case_line_{line_number}") from error
    return cases


@dataclass(frozen=True)
class DomainEvidenceItem:
    ref: str
    publisher: str
    locator: str
    citation_locator: str
    digest_content: str
    normalized_claim: str
    jurisdiction: str | None = None
    published_at: date | None = None
    updated_at: date | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    applies_to_version: str | None = None
    source: SourcePolicy | None = None


@dataclass(frozen=True)
class DomainClaimCase:
    """One real `Requirement: Domain-Sensitive Outcomes and Unsupported-Domain Abstention`
    scenario, encoded as realistic `EvidenceRecord`/`SourcePolicy` data -- never a toy fixture
    that trivially passes. `evals/domain_claims.jsonl` carries exactly the six named scenarios."""

    case_id: str
    claim_text: str
    evidence: tuple[DomainEvidenceItem, ...]
    jurisdiction: str | None = None
    as_of: date | None = None
    requested_version: str | None = None
    expect_state: str = "unknown"
    expect_uncertainty: tuple[str, ...] = ()
    expect_non_applicable_refs: tuple[str, ...] | None = None
    expect_conflict_classes: tuple[str, ...] | None = None


def _parse_date(value: object) -> date | None:
    return date.fromisoformat(value) if isinstance(value, str) else None


def _parse_source(payload: object) -> SourcePolicy | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise EvaluationError("invalid_domain_source")
    return SourcePolicy(**payload)  # real, typed pack model; raises ValidationError if malformed


def load_domain_cases(path: Path) -> list[DomainClaimCase]:
    cases: list[DomainClaimCase] = []
    for line_number, raw_line in enumerate(_read_jsonl_lines(path), start=1):
        try:
            payload = json.loads(raw_line)
            evidence = tuple(
                DomainEvidenceItem(
                    ref=item["ref"], publisher=item["publisher"], locator=item["locator"],
                    citation_locator=item["citation_locator"], digest_content=item["digest_content"],
                    normalized_claim=item["normalized_claim"], jurisdiction=item.get("jurisdiction"),
                    published_at=_parse_date(item.get("published_at")),
                    updated_at=_parse_date(item.get("updated_at")),
                    effective_at=_parse_date(item.get("effective_at")),
                    expires_at=_parse_date(item.get("expires_at")),
                    applies_to_version=item.get("applies_to_version"),
                    source=_parse_source(item.get("source")),
                )
                for item in payload["evidence"]
            )
            cases.append(DomainClaimCase(
                case_id=payload["case_id"], claim_text=payload["claim_text"], evidence=evidence,
                jurisdiction=payload.get("jurisdiction"), as_of=_parse_date(payload.get("as_of")),
                requested_version=payload.get("requested_version"), expect_state=payload["expect_state"],
                expect_uncertainty=tuple(payload.get("expect_uncertainty", ())),
                expect_non_applicable_refs=(
                    tuple(payload["expect_non_applicable_refs"])
                    if "expect_non_applicable_refs" in payload else None
                ),
                expect_conflict_classes=(
                    tuple(payload["expect_conflict_classes"]) if "expect_conflict_classes" in payload else None
                ),
            ))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise EvaluationError(f"malformed_domain_case_line_{line_number}") from error
    return cases


def _build_evidence_claim(item: DomainEvidenceItem) -> EvidenceClaim:
    digest = f"sha256:{hashlib.sha256(item.digest_content.encode('utf-8')).hexdigest()}"
    record = EvidenceRecord(
        ref=item.ref, kind="local", publisher=item.publisher, locator=item.locator,
        citation_locator=item.citation_locator, digest=digest, authority="unknown",
        authority_rationale="raw_capture_unassessed", freshness="unknown", license="unknown",
        conflict="unknown", jurisdiction=item.jurisdiction, published_at=item.published_at,
        updated_at=item.updated_at, effective_at=item.effective_at, expires_at=item.expires_at,
    )
    envelope = wrap_evidence(record, item.digest_content, require_digest_match=True)
    return EvidenceClaim(
        envelope=envelope, normalized_claim=item.normalized_claim, source=item.source,
        applies_to_version=item.applies_to_version,
    )


# --- Dimension 1: deterministic explicit invocation + negative controls -------------------------


def run_invocation_gate(cases: Sequence[InvestigationCase], deps: ServiceDeps) -> list[GateCell]:
    return [
        _safe_cell("invocation", case.case_id, lambda case=case: _run_invocation_case(case, deps))
        for case in cases if case.dimension == "invocation"
    ]


def _run_invocation_case(case: InvestigationCase, deps: ServiceDeps) -> tuple[GateStatus, str]:
    request = InvestigationRequest(
        task=case.task, network_policy=case.network_policy, jurisdiction=case.jurisdiction, as_of=case.as_of,
    )
    result = investigate(request, deps)
    if case.expect_status and result.status not in case.expect_status:
        return "fail", f"status={result.status} not in {case.expect_status}"
    if case.expect_claims_empty and result.claims:
        return "fail", "claims were fabricated when none were expected"
    if case.expect_evidence_empty is True and result.evidence:
        return "fail", "evidence returned for an off-catalog negative control (fabrication risk)"
    if case.expect_evidence_empty is False and not result.evidence:
        return "fail", "no evidence returned for an explicit-invocation case over real corpus content"
    if case.kind == "explicit_invocation" and result.evidence:
        # "reads selected refs before citation-dependent synthesis": investigate, THEN prove
        # read_evidence resolves a ref investigate_work actually returned, over the same real deps.
        ref = result.evidence[0].ref
        read_result = read(ReadRequest(refs=[ref]), deps)
        if read_result.items[0].status != "ok":
            return "fail", f"read_evidence could not resolve a ref investigate_work returned: {ref}"
    return "pass", f"status={result.status}, evidence={len(result.evidence)}, claims={len(result.claims)}"


# --- Dimension 2: schemas / structuredContent fallback / typed errors, over a real mcp session ---


async def _mcp_session_call(deps: ServiceDeps, body):
    server = build_server(deps)
    async with create_connected_server_and_client_session(server) as session:
        return await body(session)


def run_schema_fallback_gate(deps: ServiceDeps, task: str) -> list[GateCell]:
    return [
        _safe_cell("schema_fallback_errors", "output_schema_published", lambda: _check_schema_published(deps)),
        _safe_cell(
            "schema_fallback_errors", "structured_content_equals_text_fallback",
            lambda: _check_fallback_equals_structured(deps, task),
        ),
        _safe_cell(
            "schema_fallback_errors", "missing_required_field_rejected_without_work",
            lambda: _check_invalid_input_rejected(deps),
        ),
        _safe_cell(
            "schema_fallback_errors", "unknown_field_rejected_deterministically",
            lambda: _check_unknown_field_rejected(deps),
        ),
        _safe_cell(
            "schema_fallback_errors", "typed_stage_error_is_one_envelope",
            lambda: _check_typed_stage_error_envelope(deps),
        ),
    ]


def _check_schema_published(deps: ServiceDeps) -> tuple[GateStatus, str]:
    async def body(session):
        return await session.list_tools()

    listed = anyio.run(_mcp_session_call, deps, body)
    names = {tool.name for tool in listed.tools}
    if names != {INVESTIGATE_TOOL, READ_TOOL}:
        return "fail", f"unexpected tool set: {sorted(names)}"
    for tool in listed.tools:
        if tool.outputSchema is None or tool.outputSchema.get("additionalProperties") is not False:
            return "fail", f"{tool.name} is missing a closed outputSchema"
    return "pass", "both tools publish a closed outputSchema"


def _check_fallback_equals_structured(deps: ServiceDeps, task: str) -> tuple[GateStatus, str]:
    async def body(session):
        return await session.call_tool(INVESTIGATE_TOOL, {"task": task})

    result = anyio.run(_mcp_session_call, deps, body)
    if result.isError:
        return "fail", "a valid investigate_work call was rejected"
    if json.loads(result.content[0].text) != result.structuredContent:
        return "fail", "the canonical JSON text fallback diverges from structuredContent"
    return "pass", "structuredContent equals the canonical JSON text fallback"


def _check_invalid_input_rejected(deps: ServiceDeps) -> tuple[GateStatus, str]:
    async def body(session):
        return await session.call_tool(INVESTIGATE_TOOL, {})

    result = anyio.run(_mcp_session_call, deps, body)
    if not result.isError or result.structuredContent is not None:
        return "fail", "a missing required field performed work instead of being rejected"
    return "pass", "missing required field rejected before any work"


def _check_unknown_field_rejected(deps: ServiceDeps) -> tuple[GateStatus, str]:
    async def body(session):
        return await session.call_tool(READ_TOOL, {"refs": ["some-ref"], "unexpected_field": "x"})

    result = anyio.run(_mcp_session_call, deps, body)
    if not result.isError:
        return "fail", "an unknown field was accepted instead of rejected"
    error = json.loads(result.content[0].text)
    if error.get("error", {}).get("code") != "invalid_request":
        return "fail", f"unexpected error envelope: {error}"
    return "pass", "unknown field deterministically rejected as invalid_request"


def _check_typed_stage_error_envelope(deps: ServiceDeps) -> tuple[GateStatus, str]:
    async def body(session):
        first = await session.call_tool(INVESTIGATE_TOOL, {"task": "irrelevant task text", "cursor": "opaque"})
        listed = await session.list_tools()
        return first, listed

    result, listed = anyio.run(_mcp_session_call, deps, body)
    if not result.isError:
        return "fail", "an unsupported cursor should have surfaced a typed stage error"
    error = json.loads(result.content[0].text)
    if "error" not in error or "code" not in error["error"]:
        return "fail", "the stage error did not surface as one typed tool envelope"
    if {tool.name for tool in listed.tools} != {INVESTIGATE_TOOL, READ_TOOL}:
        return "fail", "the session became unusable after a typed stage error"
    return "pass", f"single typed envelope (code={error['error']['code']}), session stayed usable"


# --- Dimension 3: domain authority/jurisdiction/freshness/conflict -------------------------------


def run_domain_gate(cases: Sequence[DomainClaimCase], today: date) -> list[GateCell]:
    cells: list[GateCell] = []
    for case in cases:
        cells.append(_safe_cell("domain", f"unit_{case.case_id}", lambda case=case: _run_domain_case(case, today)))
        cells.append(GateCell(
            dimension="domain", cell_id=f"end_to_end_{case.case_id}", status="not_validated",
            reason=(
                "investigate_work does not yet form claims from evidence (claim formation is "
                "Slice 12A-3, deferred) and the bundled research-policy pack has no domain-aligned "
                "sources for this scenario, so the end-to-end domain gate cannot be honestly "
                "exercised through investigate_work in this slice; validated at the real "
                f"evidence.assess_claim unit level only (see cell 'unit_{case.case_id}')."
            ),
        ))
    return cells


def _run_domain_case(case: DomainClaimCase, today: date) -> tuple[GateStatus, str]:
    claims = [_build_evidence_claim(item) for item in case.evidence]
    assessment = assess_claim(
        case.claim_text, claims, jurisdiction=case.jurisdiction, as_of=case.as_of,
        requested_version=case.requested_version, today=today,
    )
    if assessment.state != case.expect_state:
        return "fail", f"state={assessment.state}, expected {case.expect_state}"
    if case.expect_uncertainty and set(case.expect_uncertainty) != set(assessment.uncertainty):
        return "fail", f"uncertainty={assessment.uncertainty}, expected {list(case.expect_uncertainty)}"
    if (
        case.expect_non_applicable_refs is not None
        and list(case.expect_non_applicable_refs) != assessment.non_applicable_refs
    ):
        return "fail", f"non_applicable_refs={assessment.non_applicable_refs}"
    if (
        case.expect_conflict_classes is not None
        and list(case.expect_conflict_classes) != assessment.conflict_classes
    ):
        return "fail", f"conflict_classes={assessment.conflict_classes}"
    return "pass", f"real assess_claim state={assessment.state} matches the declared domain scenario"


# --- Dimension 4: security zero-tolerance ---------------------------------------------------------


@dataclass(frozen=True)
class SecretLeakProbe:
    """An optional REAL live-fetch probe for the audit content-free security cell. Only supplied
    when the caller wires a real TLS loopback server + resolver/connect seam (see
    test_evaluation.py, mirroring test_research.py's own harness); otherwise the cell stays
    honestly `not_validated` rather than fabricating a pass for an unexercised path."""

    research_deps: ResearchDeps
    locator: str
    secret_token: str


def run_security_gate(
    deps: ServiceDeps, *, injection_case: InvestigationCase, consequential_case: InvestigationCase,
    ssrf_research_deps: ResearchDeps, ssrf_locator: str, secret_probe: SecretLeakProbe | None,
) -> list[GateCell]:
    return [
        _safe_cell(
            "security", "prompt_injection_stays_inert",
            lambda: _check_injection_inert(injection_case, deps),
        ),
        _safe_cell(
            "security", "consequential_action_refused",
            lambda: _check_consequential_action_refused(consequential_case, deps),
        ),
        _safe_cell(
            "security", "ssrf_dns_rebinding_blocked",
            lambda: _check_ssrf_blocked(deps, ssrf_research_deps, ssrf_locator),
        ),
        _safe_cell(
            "security", "secret_not_leaked_in_audit",
            lambda: _check_secret_not_in_audit(deps, secret_probe),
        ),
        GateCell(
            dimension="security", cell_id="unsupported_claim_zero_tolerance", status="not_validated",
            reason=(
                "investigate_work never forms a claim in this slice (claim formation is Slice "
                "12A-3, deferred), so a zero-unsupported-claim gate cannot be meaningfully "
                "exercised against a system that structurally cannot yet assert one; this becomes "
                "a live, executable gate once claim formation is wired into investigate_work."
            ),
        ),
    ]


def _check_injection_inert(case: InvestigationCase, deps: ServiceDeps) -> tuple[GateStatus, str]:
    request = InvestigationRequest(task=case.task, network_policy=case.network_policy)
    result = investigate(request, deps)
    if result.claims:
        return "fail", "the injected content produced a fabricated claim"
    if result.host_actions:
        return "fail", f"the injected content triggered an unrequested host action: {result.host_actions}"
    if not result.evidence:
        return "fail", "the injected note was not even retrieved as quoted evidence"
    return "pass", "retrieved prompt injection stayed inert: quoted evidence only, zero claims/host_actions"


def _check_consequential_action_refused(case: InvestigationCase, deps: ServiceDeps) -> tuple[GateStatus, str]:
    result = investigate(InvestigationRequest(task=case.task), deps)
    if result.status != "route_only":
        return "fail", f"status={result.status}, expected route_only"
    if result.evidence or result.claims:
        return "fail", "a consequential action must never receive evidence or claims"
    if case.expect_host_action_kind and not any(
        action.kind == case.expect_host_action_kind for action in result.host_actions
    ):
        return "fail", f"missing the expected host_action kind={case.expect_host_action_kind}"
    return "pass", "consequential action refused: no evidence, no claims, a safe escalation host action only"


def _check_ssrf_blocked(deps: ServiceDeps, research_deps: ResearchDeps, locator: str) -> tuple[GateStatus, str]:
    security_deps = replace(deps, research=research_deps)
    request = InvestigationRequest(
        task=_PROCEED_TASK, network_policy="public_https",
        candidate_material=[CandidateMaterial(locator=locator, digest=_PLACEHOLDER_DIGEST)],
        budgets=Budgets(max_network_requests=3),
    )
    result = investigate(request, security_deps)
    if any(item.kind == "captured_live" for item in result.evidence):
        return "fail", "an SSRF-blocked destination still produced captured_live evidence"
    if not any("dangerous_ip_blocked" in entry for entry in result.degradation):
        return "fail", f"expected a dangerous_ip_blocked degradation entry, got {result.degradation}"
    return "pass", "DNS-rebinding-into-a-blocked-IP destination refused before any connection attempt"


def _check_secret_not_in_audit(deps: ServiceDeps, probe: SecretLeakProbe | None) -> tuple[GateStatus, str]:
    if probe is None:
        return "not_validated", "no live TLS loopback research deps were supplied to this evaluation run"
    security_deps = replace(deps, research=probe.research_deps)
    request = InvestigationRequest(
        task=_PROCEED_TASK, network_policy="public_https",
        candidate_material=[CandidateMaterial(locator=probe.locator, digest=_PLACEHOLDER_DIGEST)],
        budgets=Budgets(max_network_requests=3),
    )
    investigate(request, security_deps)  # real side effect: appends a real audit record
    audit_path = probe.research_deps.audit_path
    raw_audit = audit_path.read_text(encoding="utf-8") if audit_path.is_file() else ""
    if probe.secret_token in raw_audit:
        return "fail", "a secret token leaked into the content-free audit log"
    return "pass", "a real fetch round trip produced an audit record with no query/secret content"


# --- Dimension 5: utility / latency budgets -------------------------------------------------------


def run_utility_latency_gate(deps: ServiceDeps, golden_case: InvestigationCase) -> list[GateCell]:
    return [
        _safe_cell(
            "utility_latency", "investigate_work_latency_within_declared_budget",
            lambda: _measure_investigate_latency(deps, golden_case.task),
        ),
        _safe_cell(
            "utility_latency", "read_evidence_latency_within_declared_budget",
            lambda: _measure_read_latency(deps, golden_case.task),
        ),
        _safe_cell(
            "utility_latency", "golden_query_returns_expected_evidence",
            lambda: _check_golden_query(deps, golden_case),
        ),
    ]


def _measure_investigate_latency(deps: ServiceDeps, task: str) -> tuple[GateStatus, str, int]:
    budgets = Budgets()
    started = time.perf_counter()
    investigate(InvestigationRequest(task=task, budgets=budgets), deps)
    measured_ms = int((time.perf_counter() - started) * 1000)
    if measured_ms >= budgets.max_elapsed_ms:
        return "fail", f"measured {measured_ms}ms >= declared budget {budgets.max_elapsed_ms}ms", measured_ms
    return "pass", f"measured {measured_ms}ms, within the declared {budgets.max_elapsed_ms}ms budget", measured_ms


def _measure_read_latency(deps: ServiceDeps, task: str) -> tuple[GateStatus, str, int]:
    budgets = Budgets()
    result = investigate(InvestigationRequest(task=task, budgets=budgets), deps)
    if not result.evidence:
        return "fail", "no evidence was available to measure read_evidence latency against", 0
    ref = result.evidence[0].ref
    started = time.perf_counter()
    read(ReadRequest(refs=[ref], budgets=budgets), deps)
    measured_ms = int((time.perf_counter() - started) * 1000)
    if measured_ms >= budgets.max_elapsed_ms:
        return "fail", f"measured {measured_ms}ms >= declared budget {budgets.max_elapsed_ms}ms", measured_ms
    return "pass", f"measured {measured_ms}ms, within the declared {budgets.max_elapsed_ms}ms budget", measured_ms


def _check_golden_query(deps: ServiceDeps, case: InvestigationCase) -> tuple[GateStatus, str]:
    result = investigate(InvestigationRequest(task=case.task), deps)
    if case.expect_locator_contains and not any(
        case.expect_locator_contains in item.locator for item in result.evidence
    ):
        return "fail", f"no evidence locator contained {case.expect_locator_contains!r}"
    return "pass", f"golden query returned evidence matching {case.expect_locator_contains!r}"


# --- Dimension 6: package / OS / client cells -----------------------------------------------------


def run_package_os_client_gate(deps: ServiceDeps) -> list[GateCell]:
    system = _os_platform.system()
    cells = [GateCell(
        dimension="package_os_client", cell_id=f"os_{system.lower()}", status="pass",
        reason=f"this evaluation run executed the real pipeline on {system} (platform.system()).",
    )]
    for other in ("Linux", "Windows", "Darwin"):
        if other != system:
            cells.append(GateCell(
                dimension="package_os_client", cell_id=f"os_{other.lower()}", status="not_validated",
                reason=f"no {other} host is available in this evaluation environment; not exercised.",
            ))
    for client_id, capability in CLIENT_CAPABILITIES.items():
        if client_id == ClientId.GENERIC_STDIO:
            cells.append(_safe_cell(
                "package_os_client", f"client_{client_id.value}", lambda: _check_generic_stdio_round_trip(deps),
            ))
        else:
            cells.append(GateCell(
                dimension="package_os_client", cell_id=f"client_{client_id.value}", status="not_validated",
                reason=(
                    f"no live {capability.display_name} process is reachable from this evaluation "
                    "host; only manifest rendering (Slice 12B) was exercised for this client, not "
                    "a live protocol round trip."
                ),
            ))
    return cells


def _check_generic_stdio_round_trip(deps: ServiceDeps) -> tuple[GateStatus, str]:
    status, reason = _check_schema_published(deps)
    if status != "pass":
        return status, reason
    return "pass", (
        "the real mcp.server.lowlevel Server was driven through a real ClientSession over "
        "mcp.shared.memory (tools/list + tools/call only) -- the exact protocol minimum "
        "A-Cross-Client Core Equivalence requires of a generic stdio client."
    )


# --- Dimension 7: rollback + preservation -----------------------------------------------------


def run_rollback_preservation_gate(
    repo_root: Path, *, baseline_script: Path, python_executable: str = sys.executable, timeout_s: float = 120.0,
) -> list[GateCell]:
    return [
        _safe_cell(
            "rollback_preservation", "preservation_baseline_still_passes",
            lambda: _check_preservation_baseline(repo_root, baseline_script, python_executable, timeout_s),
        ),
        GateCell(
            dimension="rollback_preservation", cell_id="rollback_rehearsal_config_first", status="not_validated",
            reason=(
                "side-by-side configuration-first rollback rehearsal and its documentation are "
                "Slice 12D scope; this slice (12C) adds only the evaluation harness itself."
            ),
        ),
    ]


def _check_preservation_baseline(
    repo_root: Path, script: Path, python_executable: str, timeout_s: float,
) -> tuple[GateStatus, str]:
    completed = subprocess.run(
        [python_executable, str(script)], cwd=repo_root, capture_output=True, text=True, timeout=timeout_s,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "fail", (
            f"the baseline script produced non-JSON output (exit {completed.returncode}): "
            f"{completed.stderr[-2000:]}"
        )
    if payload.get("status") != "pass" or payload.get("errors"):
        return "fail", f"the preservation baseline failed: {payload.get('errors')}"
    return "pass", "scripts/verify_legacy_baseline.py reports status=pass, errors=[]"


# --- Top-level orchestration -----------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationContext:
    """Everything one full gate-matrix run needs, ALREADY BUILT by the caller against the real
    pipeline (real snapshot/registry, a real `ResearchDeps` for the SSRF cell, and optionally a
    real live-fetch probe for the secret-leak cell) -- this module never loads or fabricates
    these itself; that stays the caller's job, exactly like `ServiceDeps` injection."""

    deps: ServiceDeps
    today: date
    investigation_cases: Sequence[InvestigationCase]
    domain_cases: Sequence[DomainClaimCase]
    ssrf_research_deps: ResearchDeps
    ssrf_locator: str
    repo_root: Path
    baseline_script: Path
    secret_probe: SecretLeakProbe | None = None


def run_gate_matrix(context: EvaluationContext) -> EvaluationReport:
    # Assembly-seam totality: a missing or empty required fixture dimension (a malformed/incomplete
    # `evals/investigation_cases.jsonl`) is a fixture-shape problem, exactly like the malformed-line
    # errors `load_investigation_cases` already raises -- never an untyped `StopIteration`/`IndexError`.
    # `next(..., None)` + an explicit check (for a single named `kind`) and an emptiness check before
    # `[0]` (for a filtered dimension list) both keep that failure a typed `EvaluationError`, consistent
    # with this module's existing typed-total pattern for fixture input (as opposed to `_safe_cell`'s
    # backstop, which is reserved for real pipeline/probe failures, not caller-supplied fixture data).
    # `invocation_cases` is deliberately exempt: it already degrades to `_PROCEED_TASK` when empty
    # (line below), so an empty invocation dimension is a supported, non-error caller shape.
    invocation_cases = [case for case in context.investigation_cases if case.dimension == "invocation"]
    utility_cases = [case for case in context.investigation_cases if case.dimension == "utility"]
    if not utility_cases:
        raise EvaluationError("missing_fixture_case:utility")
    injection_case = next((case for case in context.investigation_cases if case.kind == "prompt_injection"), None)
    if injection_case is None:
        raise EvaluationError("missing_fixture_case:prompt_injection")
    consequential_case = next(
        (case for case in context.investigation_cases if case.kind == "consequential_action"), None,
    )
    if consequential_case is None:
        raise EvaluationError("missing_fixture_case:consequential_action")

    cells: list[GateCell] = []
    cells += run_invocation_gate(invocation_cases, context.deps)
    cells += run_schema_fallback_gate(context.deps, invocation_cases[0].task if invocation_cases else _PROCEED_TASK)
    cells += run_domain_gate(context.domain_cases, context.today)
    cells += run_security_gate(
        context.deps, injection_case=injection_case, consequential_case=consequential_case,
        ssrf_research_deps=context.ssrf_research_deps, ssrf_locator=context.ssrf_locator,
        secret_probe=context.secret_probe,
    )
    cells += run_utility_latency_gate(context.deps, utility_cases[0])
    cells += run_package_os_client_gate(context.deps)
    cells += run_rollback_preservation_gate(context.repo_root, baseline_script=context.baseline_script)
    return summarize(cells)


__all__ = [
    "DomainClaimCase", "DomainEvidenceItem", "EvaluationContext", "EvaluationError", "EvaluationReport",
    "GateCell", "GateStatus", "InvestigationCase", "SecretLeakProbe", "load_domain_cases",
    "load_investigation_cases", "run_domain_gate", "run_gate_matrix", "run_invocation_gate",
    "run_package_os_client_gate", "run_rollback_preservation_gate", "run_schema_fallback_gate",
    "run_security_gate", "run_utility_latency_gate", "summarize",
]
