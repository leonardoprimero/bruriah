# Slice 11 tests for the bounded context assembler + vendor-neutral host actions. No mocks of
# `route.py`/`evidence.py`/`research.py`: every scenario drives the REAL `route()`, `assess_claim()`,
# and (for the live-research scenarios) the REAL `research()` against a local TLS loopback server
# (mirroring `test_research.py`'s own harness) -- never a fixture that trivially passes.
#
# File organization: shared builders first, then sections mirroring the acceptance list in
# tasks.md 11.1 -- proceed assembly (complete/partial/conflicted), professional escalation
# (route-gated abstain/route-only), consequential-action refusal, the five vendor-neutral
# `HostAction` kinds (two reused from `research.py`, three synthesized here), output-budget
# compaction, rollback route-only mode, typed-total argument handling, and the zero-I/O/
# no-wall-clock structural guarantees.
from __future__ import annotations

import ast
import builtins
import hashlib
import socket
import ssl
import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from bruriah.classify import RequestClassification
from bruriah.context import assemble_context, compact_to_budget
from bruriah.contracts import (
    Budgets, ClaimRecord, EvidenceRecord, InvestigationRequest, InvestigationResult,
)
from bruriah.evidence import EvidenceClaim, assess_claim, wrap_evidence
from bruriah.lookup import LookupResult, SourceMatch
from bruriah.packs import SourcePolicy
from bruriah.research import ConcurrencyLimiter, ResearchDeps, ResearchOutcome, research
from bruriah.route import RouteDecision, route

_TODAY = date(2026, 7, 24)
_SRC = Path(__file__).resolve().parents[1] / "src"
_CONTEXT_MODULE = _SRC / "bruriah" / "context.py"


# === Shared builders =============================================================================


def _digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _evidence_record(ref: str = "local:1", *, publisher: str = "pub-a", digest_content: str = "content-a",
                      **overrides: object) -> EvidenceRecord:
    payload: dict[str, object] = dict(
        ref=ref, kind="local", publisher=publisher, locator="doc.md", citation_locator="doc.md#1-2",
        digest=_digest(digest_content), extraction_method="markdown_section",
        authority="unknown", authority_rationale="raw_capture_unassessed",
        freshness="unknown", license="unknown", conflict="unknown",
    )
    payload.update(overrides)
    return EvidenceRecord(**payload)


def _source_policy(source_id: str = "src-a", *, authority: str = "official",
                    jurisdictions: tuple[str, ...] = ("GLOBAL",), freshness_days: int = 365,
                    rationale: str = "Issued by an approved authority.") -> SourcePolicy:
    return SourcePolicy(
        source_id=source_id, publisher="Approved Publisher", authority=authority, rationale=rationale,
        claim_types=["documentation"], jurisdictions=list(jurisdictions), temporal_rules="effective_at applies",
        citation_rules="cite section", reuse_rules="cite only", freshness_days=freshness_days,
        limitations=[], biases=[], conflicts=[], exclusions=[],
    )


def _request(**overrides: object) -> InvestigationRequest:
    payload: dict[str, object] = {"task": "Investigate a documented programming API behavior."}
    payload.update(overrides)
    return InvestigationRequest(**payload)


def _classification(**overrides: object) -> RequestClassification:
    payload: dict[str, object] = dict(
        intent="investigate", domain="programming", claim_type="factual", risk="medium", jurisdiction="unknown",
    )
    payload.update(overrides)
    return RequestClassification(**payload)


def _source_match(source_id: str = "src-a", *, jurisdiction_applicable: bool = True,
                   jurisdictions: tuple[str, ...] = ("GLOBAL",)) -> SourceMatch:
    return SourceMatch(source=_source_policy(source_id, jurisdictions=jurisdictions),
                        jurisdiction_applicable=jurisdiction_applicable)


def _lookup_with_sources(*matches: SourceMatch) -> LookupResult:
    return LookupResult(domain_supported=True, sources=tuple(matches), capabilities=())


def _no_evidence_lookup() -> LookupResult:
    return LookupResult(domain_supported=False, sources=(), capabilities=())


def _proceeding_decision() -> RouteDecision:
    decision = route(_classification(), _lookup_with_sources(_source_match()), _request())
    assert decision.outcome == "proceed"
    return decision


def _supported_claim_fixture() -> tuple[EvidenceRecord, "object"]:
    record = _evidence_record("core:claim", published_at=_TODAY)
    source = _source_policy(freshness_days=3650)
    claim = EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="feature=available", source=source)
    assessment = assess_claim("feature availability", [claim], today=_TODAY)
    assert assessment.state == "supported"
    return record, assessment


# --- Loopback HTTPS harness (mirrors test_research.py, used only by the live-research section) --

_FIXTURES = Path(__file__).parent / "fixtures" / "fetch"
_CERT = _FIXTURES / "testcert.pem"
_KEY = _FIXTURES / "testkey.pem"
_HOST = "cerebro-test.local"
_PUBLIC_IP = "93.184.216.34"  # example.com; never actually connected to.
Responder = Callable[[bytes], tuple[int, dict[str, str], bytes]]


class _LocalTlsServer:
    def __init__(self, responder: Responder) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(_CERT), str(_KEY))
        self._context = context
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._responder = responder
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                raw_conn, _addr = self._socket.accept()
            except socket.timeout:
                continue
            try:
                tls_conn = self._context.wrap_socket(raw_conn, server_side=True)
            except (ssl.SSLError, OSError):
                continue
            try:
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = tls_conn.recv(4096)
                    if not chunk:
                        break
                    request += chunk
                status, headers, body = self._responder(request)
                head = f"HTTP/1.1 {status} status\r\n".encode()
                for key, value in headers.items():
                    head += f"{key}: {value}\r\n".encode()
                if "Content-Length" not in headers:
                    head += f"Content-Length: {len(body)}\r\n".encode()
                tls_conn.sendall(head + b"\r\n" + body)
            except (ssl.SSLError, OSError):
                pass
            finally:
                tls_conn.close()

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._socket.close()


def _redirect_connect(server: "_LocalTlsServer") -> Callable[[str, int, float], socket.socket]:
    def _connect(_ip: str, _port: int, timeout: float) -> socket.socket:
        return socket.create_connection(("127.0.0.1", server.port), timeout=timeout)

    return _connect


def _trusting_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(_CERT))


def _url(server: "_LocalTlsServer", path: str = "/page") -> str:
    return f"https://{_HOST}:{server.port}{path}"


def _allowlist(server: "_LocalTlsServer") -> frozenset[str]:
    return frozenset({f"{_HOST}:{server.port}"})


def _fake_public_resolver(_host: str) -> list[str]:
    return [_PUBLIC_IP]


def _ok_responder(body: bytes = b"Real page content from the loopback research pipeline.") -> Responder:
    def _responder(_request: bytes) -> tuple[int, dict[str, str], bytes]:
        return 200, {"Content-Type": "text/plain"}, body

    return _responder


def _research_deps(tmp_path: Path, server: "_LocalTlsServer", **overrides: object) -> ResearchDeps:
    base: dict[str, object] = dict(
        allowlist=_allowlist(server), cache_dir=tmp_path / "cache", audit_path=tmp_path / "audit" / "research.jsonl",
        network_enabled=True, concurrency=ConcurrencyLimiter(4), resolver=_fake_public_resolver,
        connect=_redirect_connect(server), ssl_context=_trusting_ssl_context(),
    )
    base.update(overrides)
    return ResearchDeps(**base)


# === Proceed assembly: complete, partial, conflicted (real route + real evidence) ===============


def test_real_pipeline_all_supported_claim_assembles_complete_status() -> None:
    decision = _proceeding_decision()
    req = _request()
    record, assessment = _supported_claim_fixture()

    result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record])

    assert isinstance(result, InvestigationResult)
    assert result.status == "complete"
    assert result.request_id.startswith("sha256:")
    assert [item.ref for item in result.evidence] == ["core:claim"]
    assert result.claims[0].state == "supported"
    assert result.gaps == []
    assert result.host_actions == []
    assert result.conflicts == []
    assert result.next_cursor is None


def test_real_pipeline_conflicting_sources_produce_conflicts_and_partial_status() -> None:
    decision = _proceeding_decision()
    req = _request()
    older = _evidence_record("src:old", digest_content="old-rule", effective_at=date(2018, 1, 1))
    newer = _evidence_record("src:new", digest_content="new-rule", effective_at=date(2024, 1, 1))
    source = _source_policy(jurisdictions=("GLOBAL",))
    claims = [
        EvidenceClaim(wrap_evidence(older, "old-rule"), normalized_claim="rate=15pct", source=source),
        EvidenceClaim(wrap_evidence(newer, "new-rule"), normalized_claim="rate=21pct", source=source),
    ]
    assessment = assess_claim("current rate", claims, today=_TODAY)
    assert assessment.state == "conflicted"

    result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[older, newer])

    assert result.status == "partial"
    assert len(result.conflicts) == 1
    assert "conflicting evidence" in result.conflicts[0]
    assert {item.ref for item in result.evidence} == {"src:old", "src:new"}  # both refs preserved
    assert "evidence_disagrees" in result.gaps


def test_authority_unknown_claim_adds_a_real_safety_warning_and_keeps_the_ref() -> None:
    decision = _proceeding_decision()
    req = _request()
    record = _evidence_record("astrology:1", digest_content="astrology-chart")
    claim = EvidenceClaim(wrap_evidence(record, "astrology-chart"), normalized_claim="x", source=None)
    assessment = assess_claim("astrology reading", [claim], today=_TODAY)
    assert assessment.state == "unknown"

    result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record])

    assert "authority_unknown" in result.gaps
    assert "unverified_authority_evidence_present" in result.warnings
    assert result.status == "partial"
    assert [item.ref for item in result.evidence] == ["astrology:1"]  # never hidden, never dropped


def test_no_evidence_at_all_under_proceed_names_gap_and_stays_partial() -> None:
    decision = _proceeding_decision()
    result = assemble_context(_request(), decision)
    assert result.status == "partial"
    assert "no_evidence_available" in result.gaps
    assert result.evidence == [] and result.claims == []


# === Professional escalation: regulated-context gaps and unsupported professions (route-gated) ==


def test_regulated_domain_missing_context_names_gaps_and_escalates_request_jurisdiction() -> None:
    classification = RequestClassification(intent="investigate", domain="law", claim_type="factual",
                                            risk="regulated", jurisdiction="unknown")
    lookup = _lookup_with_sources(_source_match(jurisdictions=("US",)))
    req = _request()
    decision = route(classification, lookup, req)
    assert decision.outcome == "route_only"
    assert decision.reason == "regulated_domain_missing_context"

    result = assemble_context(req, decision)

    assert result.status == "route_only"
    assert result.evidence == [] and result.claims == []  # named gap, never a fabricated conclusion
    assert "missing_jurisdiction" in result.gaps
    assert "missing_effective_date" in result.gaps
    kinds = {action.kind for action in result.host_actions}
    assert kinds == {"request_jurisdiction"}


def test_arbitrary_unsupported_profession_abstains_with_consult_professional() -> None:
    classification = RequestClassification(intent="investigate", domain="unsupported", claim_type="factual",
                                            risk="unknown", jurisdiction="unknown")
    decision = route(classification, _no_evidence_lookup(), _request())
    assert decision.outcome == "abstained"

    result = assemble_context(_request(), decision)

    assert result.status == "abstained"
    assert result.evidence == [] and result.claims == []
    assert "no_approved_domain_pack" in result.gaps
    assert any(action.kind == "consult_professional" for action in result.host_actions)


def test_accounting_sources_not_jurisdiction_applicable_escalates_consult_professional() -> None:
    classification = RequestClassification(intent="investigate", domain="accounting", claim_type="factual",
                                            risk="regulated", jurisdiction="AR")
    lookup = _lookup_with_sources(_source_match(jurisdiction_applicable=False, jurisdictions=("US",)))
    req = _request(jurisdiction="AR", as_of=_TODAY)
    decision = route(classification, lookup, req)
    assert decision.outcome == "route_only"
    assert decision.reason == "sources_not_jurisdiction_applicable"

    result = assemble_context(req, decision)

    assert "sources_jurisdiction_mismatch" in result.gaps
    assert any(action.kind == "consult_professional" for action in result.host_actions)


# === Consequential action: never performed, always escalated ====================================


def test_consequential_action_requested_performs_no_action_and_escalates_inspect_capability() -> None:
    classification = RequestClassification(intent="consequential_action", domain="programming",
                                            claim_type="factual", risk="medium", jurisdiction="unknown")
    lookup = _lookup_with_sources(_source_match())
    req = _request()
    decision = route(classification, lookup, req)
    assert decision.outcome == "route_only"
    assert decision.reason == "consequential_action_requires_host_action"

    result = assemble_context(req, decision)

    assert result.status == "route_only"
    assert result.evidence == [] and result.claims == []
    assert "consequential_action_refused_no_action_performed" in result.warnings
    assert any(action.kind == "inspect_capability" for action in result.host_actions)


def test_consequential_action_with_no_evidence_abstains_and_still_escalates() -> None:
    classification = RequestClassification(intent="consequential_action", domain="unsupported",
                                            claim_type="factual", risk="unknown", jurisdiction="unknown")
    decision = route(classification, _no_evidence_lookup(), _request())
    assert decision.outcome == "abstained"

    result = assemble_context(_request(), decision)

    assert result.status == "abstained"
    assert "consequential_action_refused_no_action_performed" in result.warnings
    kinds = {action.kind for action in result.host_actions}
    assert kinds == {"consult_professional", "inspect_capability"}


# === Live research: real loopback fetch folds in; unavailable research degrades =================


def test_research_fetched_evidence_folds_into_proceed_assembly(tmp_path: Path) -> None:
    body = b"Real captured page content from the loopback research pipeline."
    server = _LocalTlsServer(_ok_responder(body))
    try:
        classification = _classification()
        lookup = _lookup_with_sources(_source_match())
        req = InvestigationRequest(task="Find the current documented default timeout value.",
                                    network_policy="public_https", budgets=Budgets(max_network_requests=5))
        decision = route(classification, lookup, req)
        assert decision.outcome == "proceed"

        deps = _research_deps(tmp_path, server)
        outcome = research(req, _url(server), deps)
        assert outcome.status == "fetched"
        assert outcome.evidence is not None

        result = assemble_context(req, decision, research_outcomes=[outcome])

        assert outcome.evidence.ref in {item.ref for item in result.evidence}
        assert result.status == "partial"  # research-only evidence, no assessed claim yet
        assert result.degradation == []
    finally:
        server.close()


def test_research_unavailable_outcomes_surface_web_search_and_fetch_public_url(tmp_path: Path) -> None:
    server = _LocalTlsServer(_ok_responder())
    try:
        classification = _classification()
        lookup = _lookup_with_sources(_source_match())
        req = InvestigationRequest(task="Investigate a documented programming API behavior.",
                                    network_policy="public_https", budgets=Budgets(max_network_requests=5))
        decision = route(classification, lookup, req)
        assert decision.outcome == "proceed"
        deps = _research_deps(tmp_path, server)

        no_url_outcome = research(req, None, deps)
        assert no_url_outcome.status == "not_warranted"
        assert no_url_outcome.host_actions[0].kind == "web_search"

        off_req = InvestigationRequest(task="Investigate a documented programming API behavior.",
                                        network_policy="off")
        off_outcome = research(off_req, _url(server), deps)
        assert off_outcome.status == "disabled"
        assert off_outcome.host_actions[0].kind == "fetch_public_url"

        result = assemble_context(req, decision, research_outcomes=[no_url_outcome, off_outcome])

        kinds = {action.kind for action in result.host_actions}
        assert {"web_search", "fetch_public_url"} <= kinds
        assert result.degradation == ["research_unavailable:no_candidate_url", "research_unavailable:network_disabled"]
        assert result.status == "partial"
    finally:
        server.close()


# === Output-budget compaction: refs, conflicts, and safety warnings are NEVER dropped ============


def test_compact_to_budget_is_noop_when_already_within_budget() -> None:
    record = _evidence_record("only:1")
    claim = ClaimRecord(text="claim text", state="supported", supporting_refs=["only:1"], conflicting_refs=[])
    result = InvestigationResult(
        schema_version="1", status="complete", request_id=f"sha256:{'a' * 64}", evidence=[record],
        claims=[claim], conflicts=[], gaps=[], host_actions=[], warnings=[], degradation=[],
        budgets=Budgets(), next_cursor=None,
    )
    compacted = compact_to_budget(result, max_output_chars=100_000)
    assert compacted == result


def test_compact_to_budget_drops_only_unprotected_evidence_keeps_refs_conflicts_warnings() -> None:
    protected = _evidence_record("keep:cited", digest_content="protected content referenced by a claim")
    padding = [
        _evidence_record(f"drop:{i}", digest_content=f"padding evidence body number {i} " * 20)
        for i in range(8)
    ]
    claim = ClaimRecord(text="claim referencing protected evidence", state="conflicted",
                         supporting_refs=["keep:cited"], conflicting_refs=[])
    full = InvestigationResult(
        schema_version="1", status="complete", request_id=f"sha256:{'b' * 64}",
        evidence=[protected, *padding], claims=[claim],
        conflicts=["Claim 'claim referencing protected evidence' has conflicting evidence (time)."],
        gaps=["evidence_disagrees"], host_actions=[], warnings=["unverified_authority_evidence_present"],
        degradation=[], budgets=Budgets(), next_cursor=None,
    )
    protected_only = full.model_copy(update={"evidence": [protected]})
    threshold = len(protected_only.model_dump_json())
    assert len(full.model_dump_json()) > threshold  # padding really does inflate the payload

    compacted = compact_to_budget(full, max_output_chars=threshold)

    refs = {item.ref for item in compacted.evidence}
    assert "keep:cited" in refs  # claim-cited ref is NEVER dropped
    assert refs <= {"keep:cited", *(f"drop:{i}" for i in range(8))}
    assert compacted.conflicts == full.conflicts  # conflicts NEVER dropped
    assert compacted.warnings == full.warnings  # safety warnings NEVER dropped
    assert compacted.status == "partial"  # downgraded from "complete"
    assert "output_budget_compacted" in compacted.degradation
    assert compacted.next_cursor is not None

    again = compact_to_budget(full, max_output_chars=threshold)
    assert again == compacted  # deterministic: identical input drops identical refs, same order


def test_compact_to_budget_never_drops_protected_evidence_even_if_still_over_budget() -> None:
    protected = _evidence_record("keep:only", digest_content="the only evidence, and it is claim-cited")
    claim = ClaimRecord(text="claim", state="supported", supporting_refs=["keep:only"], conflicting_refs=[])
    result = InvestigationResult(
        schema_version="1", status="complete", request_id=f"sha256:{'c' * 64}", evidence=[protected],
        claims=[claim], conflicts=[], gaps=[], host_actions=[], warnings=["a safety warning"],
        degradation=[], budgets=Budgets(), next_cursor=None,
    )
    compacted = compact_to_budget(result, max_output_chars=1)  # impossibly small budget
    assert compacted.evidence == [protected]  # never sacrificed
    assert compacted.warnings == ["a safety warning"]
    assert compacted == result  # nothing droppable -> unchanged, no fake compaction claimed


def test_assemble_context_end_to_end_forces_compaction_under_a_real_output_budget() -> None:
    decision = _proceeding_decision()
    small_budget_req = _request(budgets=Budgets(max_output_chars=256))
    record, assessment = _supported_claim_fixture()
    padding_outcomes = [
        ResearchOutcome(
            status="fetched", code="ok",
            evidence=_evidence_record(f"live:{i}", kind="captured_live",
                                       digest_content=f"live captured body padding {i} " * 15),
            excerpt=f"live captured body padding {i} " * 15,
        )
        for i in range(6)
    ]

    result = assemble_context(small_budget_req, decision, assessments=[assessment], evidence_pool=[record],
                               research_outcomes=padding_outcomes)

    refs = {item.ref for item in result.evidence}
    assert "core:claim" in refs  # the claim-cited ref survives real end-to-end compaction
    assert len(refs) < 1 + len(padding_outcomes)  # padding was genuinely dropped
    assert "output_budget_compacted" in result.degradation
    assert result.status == "partial"
    assert result.next_cursor is not None


# === Rollback = forced route-only mode ===========================================================


def test_rollback_mode_forces_route_only_even_when_route_decision_would_proceed() -> None:
    decision = _proceeding_decision()
    record, assessment = _supported_claim_fixture()

    result = assemble_context(_request(), decision, assessments=[assessment], evidence_pool=[record],
                               mode="route_only")

    assert result.status == "route_only"
    assert result.evidence == []  # rollback assembles ONLY routing -- no conclusions, ever
    assert result.claims == []
    assert "rollback_route_only_mode" in result.degradation


def test_rollback_mode_keeps_a_genuinely_abstained_decision_abstained() -> None:
    classification = RequestClassification(intent="investigate", domain="unsupported", claim_type="factual",
                                            risk="unknown", jurisdiction="unknown")
    decision = route(classification, _no_evidence_lookup(), _request())
    assert decision.outcome == "abstained"

    result = assemble_context(_request(), decision, mode="route_only")

    assert result.status == "abstained"  # rollback never WIDENS abstained into route_only


# === Regression: route-gated abstain/route-only/rollback NEVER leak evidence or claims, even when
# the caller supplies a full evidence_pool, real supported ClaimAssessments, and a fetched
# ResearchOutcome (guarantee #1's "never a fabricated conclusion" contract, explicitly re-checked
# against non-empty inputs so a future refactor cannot silently regress the route gate) ============


def test_abstained_route_decision_discards_full_evidence_and_claims_even_when_provided(tmp_path: Path) -> None:
    classification = RequestClassification(intent="investigate", domain="unsupported", claim_type="factual",
                                            risk="unknown", jurisdiction="unknown")
    req = InvestigationRequest(task="Investigate a documented programming API behavior.",
                                network_policy="public_https", budgets=Budgets(max_network_requests=5))
    decision = route(classification, _no_evidence_lookup(), req)
    assert decision.outcome == "abstained"

    record, assessment = _supported_claim_fixture()
    server = _LocalTlsServer(_ok_responder())
    try:
        outcome = research(req, _url(server), _research_deps(tmp_path, server))
        assert outcome.status == "fetched"

        result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record],
                                   research_outcomes=[outcome])
    finally:
        server.close()

    assert result.status == "abstained"
    assert result.evidence == []
    assert result.claims == []


def test_route_only_decision_discards_full_evidence_and_claims_even_when_provided(tmp_path: Path) -> None:
    classification = RequestClassification(intent="investigate", domain="law", claim_type="factual",
                                            risk="regulated", jurisdiction="unknown")
    lookup = _lookup_with_sources(_source_match(jurisdictions=("US",)))
    req = InvestigationRequest(task="Investigate a documented programming API behavior.",
                                network_policy="public_https", budgets=Budgets(max_network_requests=5))
    decision = route(classification, lookup, req)
    assert decision.outcome == "route_only"

    record, assessment = _supported_claim_fixture()
    server = _LocalTlsServer(_ok_responder())
    try:
        outcome = research(req, _url(server), _research_deps(tmp_path, server))
        assert outcome.status == "fetched"

        result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record],
                                   research_outcomes=[outcome])
    finally:
        server.close()

    assert result.status == "route_only"
    assert result.evidence == []
    assert result.claims == []


def test_rollback_route_only_discards_full_evidence_and_claims_even_when_provided(tmp_path: Path) -> None:
    classification = _classification()
    lookup = _lookup_with_sources(_source_match())
    req = InvestigationRequest(task="Find the current documented default timeout value.",
                                network_policy="public_https", budgets=Budgets(max_network_requests=5))
    decision = route(classification, lookup, req)
    assert decision.outcome == "proceed"

    record, assessment = _supported_claim_fixture()
    server = _LocalTlsServer(_ok_responder())
    try:
        outcome = research(req, _url(server), _research_deps(tmp_path, server))
        assert outcome.status == "fetched"

        result = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record],
                                   research_outcomes=[outcome], mode="route_only")
    finally:
        server.close()

    assert result.status == "route_only"
    assert result.evidence == []
    assert result.claims == []
    assert "rollback_route_only_mode" in result.degradation


# === Typed-total: bad argument types never raise, always yield a safe abstained result ==========


def test_typed_total_backstop_never_raises_on_bad_argument_types() -> None:
    decision = _proceeding_decision()
    req = _request()

    for bad_request in (None, "not a request", 42):
        result = assemble_context(bad_request, decision)  # type: ignore[arg-type]
        assert result.status == "abstained"
        assert result.warnings == ["invalid_request_type"]

    for bad_decision in (None, "not a decision", 42):
        result = assemble_context(req, bad_decision)  # type: ignore[arg-type]
        assert result.warnings == ["invalid_route_decision_type"]

    assert assemble_context(req, decision, assessments="not a sequence").warnings == ["invalid_assessments_type"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, evidence_pool="not a sequence").warnings == ["invalid_evidence_pool_type"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, research_outcomes="not a sequence").warnings == ["invalid_research_outcomes_type"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, mode="bogus").warnings == ["invalid_mode"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, assessments=["not an assessment"]).warnings == ["invalid_assessment_type"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, evidence_pool=["not a record"]).warnings == ["invalid_evidence_pool_item"]  # type: ignore[arg-type]
    assert assemble_context(req, decision, research_outcomes=["not an outcome"]).warnings == ["invalid_research_outcome_type"]  # type: ignore[arg-type]

    record = _evidence_record("real:1")
    claim = EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=None)
    orphan_assessment = assess_claim("claim needing a ref not in the pool", [claim], today=_TODAY)
    orphan_result = assemble_context(req, decision, assessments=[orphan_assessment], evidence_pool=[])
    assert orphan_result.warnings == ["evidence_ref_not_in_pool"]


def test_unexpected_exception_is_converted_to_typed_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import bruriah.context as context_module

    def _exploding_request_id(_request: InvestigationRequest) -> str:
        raise RuntimeError("unexpected failure unrelated to any typed ContextError code")

    monkeypatch.setattr(context_module, "_request_id", _exploding_request_id)
    result = assemble_context(_request(), _proceeding_decision())
    assert result.status == "abstained"
    assert result.warnings == ["internal_error"]


# === Determinism ==================================================================================


def test_assemble_context_is_deterministic_for_identical_inputs() -> None:
    decision = _proceeding_decision()
    req = _request()
    record, assessment = _supported_claim_fixture()

    first = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record])
    second = assemble_context(req, decision, assessments=[assessment], evidence_pool=[record])

    assert first == second


# === Zero writes/installs/execution and no un-injected wall clock ===============================


def test_zero_writes_or_filesystem_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("assemble_context must never touch the filesystem")

    monkeypatch.setattr(builtins, "open", _forbidden_open)

    decision = _proceeding_decision()
    record, assessment = _supported_claim_fixture()

    result = assemble_context(_request(), decision, assessments=[assessment], evidence_pool=[record])

    assert result.status == "complete"
    assert list(tmp_path.iterdir()) == []  # nothing was written anywhere


def test_module_performs_no_io_or_network_imports() -> None:
    tree = ast.parse(_CONTEXT_MODULE.read_text())
    forbidden = {"sqlite3", "socket", "urllib", "http", "requests", "httpx", "os", "subprocess"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden)


def test_module_never_calls_an_un_injected_wall_clock() -> None:
    source = _CONTEXT_MODULE.read_text()
    assert "date.today(" not in source
    assert "datetime.now(" not in source
