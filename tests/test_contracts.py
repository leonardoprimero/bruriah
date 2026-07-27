from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from bruriah.contracts import Budgets, EvidenceRecord, InvestigationRequest, ReadItem, ReadRequest
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
    from bruriah import contracts

    for name in contracts.__all__:
        _assert_closed(getattr(contracts, name).model_json_schema())
def test_investigation_cursor_is_documented_as_reserved_not_supported() -> None:
    # Contract honesty: `investigate_work` carries a `cursor` field but the runtime rejects any
    # non-null value with `cursor_not_supported` (retrieval has no offset yet), so the PUBLISHED
    # schema MUST say so -- a client must never infer an investigation-pagination capability that
    # does not exist. `read_evidence`'s cursor, by contrast, IS supported.
    cursor = str(InvestigationRequest.model_json_schema()["properties"]["cursor"]).lower()
    assert "not currently supported" in cursor
    assert "cursor_not_supported" in cursor
    read_cursor = str(ReadRequest.model_json_schema()["properties"]["cursor"]).lower()
    assert "cursor_not_supported" not in read_cursor
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


# --- skills-layer contract deltas behind the opt-in gate (Unit 7) --------------------------------

import json  # noqa: E402

from bruriah.contracts import HostAction, HostSkill, PermissionDisclosure  # noqa: E402

_EVIDENCE_FIELDS = dict(
    ref="local:a#1", kind="local", publisher="p", locator="l", citation_locator="c",
    digest="sha256:" + "a" * 64, authority="official", authority_rationale="r",
    freshness="current", license="permitted", conflict="none",
)


def test_a_record_without_an_envelope_serializes_exactly_as_before() -> None:
    """The compatibility guarantee, stated as bytes rather than as an argument.

    Responses go out through `model_dump(mode="json")` with no `exclude_none`, so a plain optional
    field would add `"envelope": null` to every record a pre-skills client receives. It must be
    absent, not null."""
    dumped = EvidenceRecord(**_EVIDENCE_FIELDS).model_dump(mode="json")
    assert "envelope" not in dumped
    assert json.loads(EvidenceRecord(**_EVIDENCE_FIELDS).model_dump_json()) == dumped


def test_an_envelope_is_emitted_when_it_is_actually_present() -> None:
    # The negative control for the test above: if `envelope` were dropped unconditionally, the field
    # would be unreachable and the omission would be a bug rather than a gate.
    record = EvidenceRecord(**{**_EVIDENCE_FIELDS, "kind": "skill",
                              "envelope": PermissionDisclosure(filesystem_read=["docs/"])})
    dumped = record.model_dump(mode="json")
    assert dumped["envelope"]["filesystem_read"] == ["docs/"]
    assert dumped["envelope"]["network_hosts"] == []


def test_an_empty_envelope_still_serializes_as_a_grant_of_nothing() -> None:
    # Default-deny must be VISIBLE. An envelope granting nothing is not the same as no envelope, and
    # collapsing the two would hide the strongest thing Bruriah can say about a skill.
    dumped = EvidenceRecord(**{**_EVIDENCE_FIELDS, "kind": "skill",
                              "envelope": PermissionDisclosure()}).model_dump(mode="json")
    assert dumped["envelope"] == {
        "filesystem_read": [], "filesystem_write": [], "network_hosts": [],
        "network_schemes": [], "programs": [], "secrets": [],
    }


def test_host_skills_distinguishes_not_opted_in_from_nothing_installed() -> None:
    # The reason this field is typed and nullable rather than a prose list: these two states drive
    # different behaviour, and `host_capabilities` cannot tell them apart.
    assert InvestigationRequest(task="t").host_skills is None
    assert InvestigationRequest(task="t", host_skills=[]).host_skills == []


def test_a_host_skill_entry_is_strictly_typed() -> None:
    entry = HostSkill(skill_id="design.ui-review", version="1.4.0", digest="sha256:" + "b" * 64)
    assert entry.skill_id == "design.ui-review"
    for bad in ({"skill_id": "Design UI"}, {"version": "1.4"}, {"digest": "b" * 64}):
        with pytest.raises(ValidationError):
            HostSkill(**{**{"skill_id": "a.b", "version": "1.0.0",
                            "digest": "sha256:" + "b" * 64}, **bad})


@pytest.mark.parametrize("model", [HostSkill, PermissionDisclosure])
def test_the_new_models_are_closed(model) -> None:
    base = ({"skill_id": "a.b", "version": "1.0.0", "digest": "sha256:" + "b" * 64}
            if model is HostSkill else {})
    with pytest.raises(ValidationError) as error:
        model(**base, unexpected="x")
    assert "unexpected" in str(error.value)


def test_the_new_enum_members_are_the_only_ones_added() -> None:
    # Widening an OUTPUT enum is the one change a pinned client cannot ignore, so the exact members
    # are pinned here rather than left to review.
    import typing
    assert set(typing.get_args(EvidenceRecord.model_fields["kind"].annotation)) == {
        "local", "captured_live", "source", "capability", "skill"}
    assert set(typing.get_args(HostAction.model_fields["kind"].annotation)) == {
        "web_search", "fetch_public_url", "inspect_capability", "request_jurisdiction",
        "consult_professional", "draft_skill_candidate", "install_skill"}


def test_the_disclosure_covers_every_dimension_the_signed_envelope_can_express() -> None:
    """Guards the one real risk of not reusing `skills.PermissionEnvelope`: that the pack grows a
    permission dimension the public contract cannot disclose, and a skill ships a grant nobody sees."""
    from bruriah.skills import PermissionEnvelope

    envelope = set(PermissionEnvelope.model_fields)
    disclosed = set(PermissionDisclosure.model_fields)
    assert envelope == {"filesystem", "network", "subprocess", "secrets"}
    assert disclosed == {"filesystem_read", "filesystem_write", "network_hosts",
                         "network_schemes", "programs", "secrets"}


# --- the layer has to be discoverable, or it does not exist ---------------------------------------


def test_an_agent_is_told_how_to_opt_into_skills() -> None:
    """Without this the entire skills layer is invisible in practice.

    `host_skills` is a field this server invented; no MCP client sends it spontaneously. If the
    schema does not explain what to put there and what omitting it costs, no agent ever opts in and
    every skill, envelope and provenance chain built behind that gate is dead code in production.
    This test exists because that was the actual state until it was found by inspection."""
    schema = InvestigationRequest.model_json_schema()
    description = schema["properties"]["host_skills"].get("description", "")
    assert description, "no description: no agent will ever send the field"
    assert "empty list" in description, "an agent must learn that [] still opts in"
    assert "OMITTING" in description or "Omitting" in description, "and what omitting it costs"


def test_the_task_field_says_it_is_not_an_instruction() -> None:
    # The separation of evidence from instruction is a normative requirement; the contract should
    # say so where an agent actually reads it, not only in a spec file.
    description = InvestigationRequest.model_json_schema()["properties"]["task"].get("description", "")
    assert "never treated as an instruction" in description


def test_the_read_range_states_which_unit_its_offsets_are_in() -> None:
    # `ReadRequest.model_json_schema()` is handed to the host verbatim as `read_evidence`'s
    # `inputSchema`, so this is the only place a caller can learn the unit. Getting it wrong is
    # silent by construction: every line number is also a valid character offset, so a client that
    # assumes lines receives a much shorter window and no error saying why.
    properties = ReadRequest.model_json_schema()["$defs"]["ReadRange"]["properties"]
    for field in ("start", "end"):
        assert "character offset" in properties[field].get("description", ""), field
    returned = ReadItem.model_json_schema()["properties"]
    for field in ("start", "end"):
        assert "character offset" in returned[field].get("description", ""), field


def test_descriptions_do_not_change_the_response_shape() -> None:
    # Schema metadata is not response data: the byte-identity guarantee for pre-skills clients must
    # survive documenting the contract.
    dumped = EvidenceRecord(**_EVIDENCE_FIELDS).model_dump(mode="json")
    assert "envelope" not in dumped
    assert InvestigationRequest(task="t").host_skills is None
