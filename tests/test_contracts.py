from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from cerebro_router.contracts import Budgets, EvidenceRecord, InvestigationRequest, ReadItem, ReadRequest
def _assert_closed(schema: dict) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
    for value in schema.values():
        if isinstance(value, dict):
            _assert_closed(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_closed(item)
def test_public_contract_schemas_are_nested_closed() -> None:
    from cerebro_router import contracts

    for name in contracts.__all__:
        _assert_closed(getattr(contracts, name).model_json_schema())
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"task": ""},
        {"task": "x" * 4097},
        {"task": "research", "unknown": True},
        {"task": "research", "budgets": {"max_evidence": "2"}},
    ],
)
def test_investigation_request_rejects_invalid_or_unbounded_input(payload: dict) -> None:
    with pytest.raises(ValidationError):
        InvestigationRequest.model_validate(payload)
def test_read_request_requires_unique_refs_and_valid_ranges() -> None:
    assert ReadRequest(refs=["evidence:one"]).refs == ["evidence:one"]
    for payload in (
        {"refs": ["evidence:one", "evidence:one"]},
        {"refs": ["evidence:one"], "ranges": [{"ref": "other", "start": 1, "end": 2}]},
        {"refs": ["evidence:one"], "ranges": [{"ref": "evidence:one", "start": 2, "end": 1}]},
        {"refs": ["x"] * 11},
    ):
        with pytest.raises(ValidationError):
            ReadRequest.model_validate(payload)
def test_budgets_declares_all_ten_ceilings_with_safe_defaults() -> None:
    budgets = Budgets()
    assert budgets.max_candidates == 50
    assert budgets.max_pages == 5
    assert budgets.max_redirects == 5
    assert budgets.max_network_requests == 10
    assert budgets.max_bytes == 1_000_000
    assert budgets.max_extracted_chars == 20_000
    with pytest.raises(ValidationError):
        Budgets(max_candidates=0)
    with pytest.raises(ValidationError):
        Budgets(max_redirects=-1)
BASE_EVIDENCE = {
    "ref": "evidence:one",
    "kind": "captured_live",
    "publisher": "Project publisher",
    "locator": "https://example.test/doc",
    "citation_locator": "https://example.test/doc#section-2",
    "digest": "sha256:" + "0" * 64,
    "authority": "official",
    "authority_rationale": "Canonical publisher documentation.",
    "freshness": "current",
    "license": "permitted",
    "conflict": "none",
}
PROVENANCE_FIELDS = {
    "publisher", "citation_locator", "extraction_method", "provenance_chain",
    "redirect_chain", "pack_version", "jurisdiction", "language", "retrieved_at",
    "published_at", "updated_at", "effective_at", "expires_at",
    "authority_rationale", "reuse",
}
def test_evidence_record_carries_fifteen_provenance_fields() -> None:
    assert len(PROVENANCE_FIELDS) == 15
    assert PROVENANCE_FIELDS <= set(EvidenceRecord.model_fields)
    record = EvidenceRecord(
        **BASE_EVIDENCE,
        extraction_method="html_text",
        provenance_chain=["pack:research.minimal", "source:canonical-project-docs"],
        redirect_chain=["https://example.test/redirect"],
        pack_version="1.0.0",
        jurisdiction="US",
        language="en",
        retrieved_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
        published_at=date(2026, 1, 10),
        updated_at=date(2026, 6, 1),
        effective_at=date(2026, 6, 15),
        expires_at=date(2027, 7, 23),
        reuse="restricted",
        uncertainty=["No independent corroboration found."],
    )
    assert record.citation_locator.endswith("#section-2")
    assert record.provenance_chain[0] == "pack:research.minimal"
    assert (record.published_at, record.updated_at) == (date(2026, 1, 10), date(2026, 6, 1))
    assert record.reuse == "restricted"
def test_evidence_record_leaves_missing_metadata_unknown() -> None:
    record = EvidenceRecord(**BASE_EVIDENCE)
    assert record.extraction_method == "unknown"
    assert record.reuse == "unknown"
    assert record.provenance_chain == record.redirect_chain == record.uncertainty == []
    for name in ("pack_version", "jurisdiction", "language", "retrieved_at",
                 "published_at", "updated_at", "effective_at", "expires_at"):
        assert getattr(record, name) is None
@pytest.mark.parametrize(
    "override",
    [
        {"publisher": None},
        {"citation_locator": None},
        {"language": "english"},
        {"reuse": "maybe"},
        {"extraction_method": "screenshot"},
        {"published_at": date(2026, 6, 1), "updated_at": date(2026, 1, 10)},
        {"effective_at": date(2027, 1, 1), "expires_at": date(2026, 1, 1)},
        {"provenance_chain": ["x"] * 11},
        {"unknown_provenance": "smuggled"},
    ],
)
def test_evidence_record_rejects_invalid_provenance(override: dict) -> None:
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({**BASE_EVIDENCE, **override})
def test_read_item_carries_eight_evidence_state_fields() -> None:
    item = ReadItem(
        ref="evidence:one",
        status="ok",
        evidence_kind="local",
        locator="notes/example.md",
        citation_locator="notes/example.md#L1-L4",
        provenance_chain=["pack:research.minimal", "source:canonical-project-docs"],
        authority="primary",
        freshness="current",
        license="permitted",
        conflict="none",
    )
    assert item.evidence_kind == "local"
    assert item.citation_locator == "notes/example.md#L1-L4"
    assert item.provenance_chain == ["pack:research.minimal", "source:canonical-project-docs"]
    evidence_shape = EvidenceRecord.model_fields["provenance_chain"].annotation
    assert ReadItem.model_fields["provenance_chain"].annotation == evidence_shape
    with pytest.raises(ValidationError):
        ReadItem(ref="evidence:one", status="ok", provenance_chain=["x"] * 11)
