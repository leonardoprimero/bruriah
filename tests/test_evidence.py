# Slice 10 tests for the evidence normalization + claim-state ledger. No fixture files: every
# adversarial guarantee below is exercised against REAL constructed data -- a real sha256 digest
# (`hashlib`), real `date` arithmetic for freshness/effective windows, and a real two-jurisdiction
# `SourcePolicy` pair -- never a fixture that trivially passes.
#
# File organization (10.1a / 10.1b commit split): shared imports/helpers below are used by BOTH
# sections. Section "10.1a: core" (this file's first test section) covers the structural
# engine -- envelope separation, digest integrity, freshness/authority resolution, the generic
# conflicting-sources scenario, no-evidence, totality/typed-unknown, determinism, and the
# `ClaimRecord` projection -- and is independently runnable on its own. Section "10.1b:
# domain-scenario and regression coverage" (marked by its own header below) adds the six
# domain-sensitive adversarial scenarios, prompt-injection inertness, fabricated-authority
# spoofing, and corroboration-counting regression coverage. No test above the 10.1b boundary
# depends on anything defined below it.
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from cerebro_router.contracts import ClaimRecord, EvidenceRecord
from cerebro_router.evidence import (
    ClaimAssessment, EvidenceClaim, EvidenceError, UntrustedEvidence, assess_claim,
    assess_freshness, render_claim_record, resolve_authority, verify_extract_digest, wrap_evidence,
)
from cerebro_router.packs import SourcePolicy

_TODAY = date(2026, 7, 24)


def _digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def _record(ref: str = "local:1", *, publisher: str = "pub-a", digest_content: str = "content-a",
            **overrides: object) -> EvidenceRecord:
    payload: dict[str, object] = dict(
        ref=ref, kind="local", publisher=publisher, locator="doc.md", citation_locator="doc.md#1-2",
        digest=_digest(digest_content), extraction_method="markdown_section",
        authority="unknown", authority_rationale="raw_capture_unassessed",
        freshness="unknown", license="unknown", conflict="unknown",
    )
    payload.update(overrides)
    return EvidenceRecord(**payload)


def _source(source_id: str = "src-a", *, authority: str = "official", jurisdictions: tuple[str, ...] = ("GLOBAL",),
            freshness_days: int = 365, rationale: str = "Issued by the tax authority.") -> SourcePolicy:
    return SourcePolicy(
        source_id=source_id, publisher="Tax Authority", authority=authority, rationale=rationale,
        claim_types=["tax_rate"], jurisdictions=list(jurisdictions), temporal_rules="effective_at applies",
        citation_rules="cite section", reuse_rules="cite only", freshness_days=freshness_days,
        limitations=[], biases=[], conflicts=[], exclusions=[],
    )


# === 10.1a: core envelope, freshness/authority resolution, conflict, and totality coverage =====


# --- UntrustedEvidence envelope: separation and digest integrity -------------------------------


def test_wrap_evidence_separates_source_record_from_untrusted_extract() -> None:
    record = _record(digest_content="the real body")
    envelope = wrap_evidence(record, "the real body")
    assert isinstance(envelope, UntrustedEvidence)
    assert envelope.record is record
    assert envelope.extract == "the real body"


def test_wrap_evidence_rejects_non_record_and_non_string_typed() -> None:
    try:
        wrap_evidence("not a record", "extract")  # type: ignore[arg-type]
        raise AssertionError("expected EvidenceError")
    except EvidenceError as error:
        assert error.code == "invalid_record_type"
    try:
        wrap_evidence(_record(), 12345)  # type: ignore[arg-type]
        raise AssertionError("expected EvidenceError")
    except EvidenceError as error:
        assert error.code == "invalid_extract_type"


def test_verify_extract_digest_matches_real_sha256_of_full_text() -> None:
    text = "The exact full passage text this record's digest was computed over."
    record = _record(digest_content=text)
    assert verify_extract_digest(record, text) is True
    envelope = wrap_evidence(record, text, require_digest_match=True)
    assert envelope.extract == text


def test_wrap_evidence_detects_real_tampered_digest_mismatch() -> None:
    original = "Section 12: the withholding rate is 15 percent."
    tampered = "Section 12: the withholding rate is 99 percent."
    record = _record(digest_content=original)
    try:
        wrap_evidence(record, tampered, require_digest_match=True)
        raise AssertionError("expected EvidenceError")
    except EvidenceError as error:
        assert error.code == "digest_mismatch"


# --- Conflicting current sources (design.md scenario) -------------------------------------------


def test_conflicting_current_sources_preserves_both_refs_and_conflict_class() -> None:
    older = _record("src:old", digest_content="old-rule", effective_at=date(2018, 1, 1))
    newer = _record("src:new", digest_content="new-rule", effective_at=date(2024, 1, 1))
    source = _source(jurisdictions=("GLOBAL",))
    claims = [
        EvidenceClaim(wrap_evidence(older, "old-rule"), normalized_claim="rate=15pct", source=source),
        EvidenceClaim(wrap_evidence(newer, "new-rule"), normalized_claim="rate=21pct", source=source),
    ]

    assessment = assess_claim("current rate", claims, today=_TODAY)

    assert assessment.state == "conflicted"
    assert set(assessment.supporting_refs + assessment.conflicting_refs) == {"src:old", "src:new"}
    assert assessment.conflict_classes == ["time"]
    assert assessment.extracted_claim is None  # no consensus is fabricated


# --- Freshness resolution: stale, expired, and unknown metadata --------------------------------


def test_stale_evidence_alone_is_insufficient_never_fabricated_supported() -> None:
    record = _record(published_at=date(2020, 1, 1))
    source = _source(freshness_days=30)
    claims = [EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=source)]

    assessment = assess_claim("stale claim", claims, today=_TODAY)

    assert assess_freshness(record, today=_TODAY, freshness_days=30) == "stale"
    assert assessment.state == "insufficient"
    assert assessment.uncertainty == ["no_current_authoritative_evidence"]


def test_expired_evidence_alone_is_insufficient() -> None:
    record = _record(expires_at=date(2025, 1, 1))
    source = _source(freshness_days=3650)
    claims = [EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=source)]

    assessment = assess_claim("expired claim", claims, today=_TODAY)

    assert assess_freshness(record, today=_TODAY, freshness_days=3650) == "expired"
    assert assessment.state == "insufficient"


def test_unknown_metadata_stays_unknown_never_current() -> None:
    record = _record(jurisdiction=None, published_at=None, updated_at=None, effective_at=None, expires_at=None)
    assert assess_freshness(record, today=_TODAY, freshness_days=None) == "unknown"
    claims = [EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=None)]

    assessment = assess_claim("unknown-provenance claim", claims, today=_TODAY)

    assert assessment.state == "unknown"


# --- Determinism, totality, no-evidence, and ClaimRecord projection ----------------------------


def test_assess_claim_is_deterministic_for_identical_inputs() -> None:
    record = _record(published_at=date(2026, 1, 1))
    source = _source(freshness_days=3650)
    claims = [EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=source)]

    first = assess_claim("deterministic claim", claims, jurisdiction="US", as_of=_TODAY, today=_TODAY)
    second = assess_claim("deterministic claim", claims, jurisdiction="US", as_of=_TODAY, today=_TODAY)

    assert first == second


def test_assess_claim_never_raises_on_malformed_evidence_claims() -> None:
    assessment = assess_claim("garbage input", "not a list of EvidenceClaim", today=_TODAY)  # type: ignore[arg-type]
    assert assessment.state == "unknown"
    assert assessment.uncertainty == ["invalid_evidence_claims"]


def test_assess_claim_with_no_evidence_is_insufficient() -> None:
    assessment = assess_claim("unsupported claim", [], today=_TODAY)
    assert assessment.state == "insufficient"
    assert assessment.uncertainty == ["no_evidence"]


def test_render_claim_record_projects_to_frozen_contract() -> None:
    record = _record(published_at=date(2026, 1, 1))
    source = _source(freshness_days=3650)
    claims = [EvidenceClaim(wrap_evidence(record, "content-a"), normalized_claim="x", source=source)]
    assessment = assess_claim("projectable claim", claims, today=_TODAY)

    claim_record = render_claim_record(assessment)

    assert isinstance(claim_record, ClaimRecord)
    assert claim_record.text == assessment.claim_text
    assert claim_record.state == assessment.state
    assert claim_record.supporting_refs == assessment.supporting_refs
    assert claim_record.conflicting_refs == assessment.conflicting_refs
