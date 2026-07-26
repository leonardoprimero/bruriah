from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator
class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
ShortText = Annotated[str, Field(min_length=1, max_length=4096)]
Ref = Annotated[str, Field(min_length=1, max_length=256)]
class Budgets(ClosedModel):
    max_evidence: Annotated[int, Field(ge=1, le=100)] = 20
    max_claims: Annotated[int, Field(ge=1, le=100)] = 20
    max_output_chars: Annotated[int, Field(ge=256, le=100_000)] = 20_000
    max_elapsed_ms: Annotated[int, Field(ge=1, le=120_000)] = 10_000
    max_candidates: Annotated[int, Field(ge=1, le=200)] = 50
    max_pages: Annotated[int, Field(ge=1, le=50)] = 5
    max_redirects: Annotated[int, Field(ge=0, le=10)] = 5
    max_network_requests: Annotated[int, Field(ge=0, le=50)] = 10
    max_bytes: Annotated[int, Field(ge=1024, le=10_000_000)] = 1_000_000
    max_extracted_chars: Annotated[int, Field(ge=256, le=200_000)] = 20_000
class CandidateMaterial(ClosedModel):
    locator: ShortText
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
class HostSkill(ClosedModel):
    """One skill the HOST reports as installed. Typed rather than prose: `host_capabilities` is a
    free-text list, and parsing a sha256 out of prose is exactly the failure mode this project
    already documents for `CapabilityPolicy.integrity`. Typing it also lets an empty list mean
    "opted in with nothing installed", which prose cannot distinguish from "did not opt in"."""
    skill_id: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
class PermissionDisclosure(ClosedModel):
    """What a skill's SIGNED PACK declares it needs, flattened for disclosure.

    Disclosure, never enforcement: Cerebro does not execute skills and cannot enforce anything at
    runtime. The host enforces. Declared here rather than reusing `skills.PermissionEnvelope` so the
    public contract does not inherit the pack schema's shape -- the same reason `EvidenceRecord`
    flattens `SourcePolicy` instead of embedding it. A test pins that this covers every dimension the
    envelope can express, so the two cannot silently drift apart."""
    filesystem_read: Annotated[list[ShortText], Field(max_length=32)] = []
    filesystem_write: Annotated[list[ShortText], Field(max_length=32)] = []
    network_hosts: Annotated[list[ShortText], Field(max_length=32)] = []
    network_schemes: Annotated[list[ShortText], Field(max_length=8)] = []
    programs: Annotated[list[ShortText], Field(max_length=32)] = []
    secrets: Annotated[list[ShortText], Field(max_length=32)] = []
class InvestigationRequest(ClosedModel):
    task: Annotated[ShortText, Field(description="The work task to investigate, in the requester's own words. This is matched against local knowledge; it is never executed and never treated as an instruction to obey.")]
    outcome: Annotated[ShortText | None, Field(description="What a good result would look like, if known. Used to scope the investigation, not to steer which sources are selected.")] = None
    jurisdiction: Annotated[str, Field(min_length=2, max_length=32)] | None = None
    as_of: date | None = None
    risk_class: Annotated[Literal["low", "medium", "high", "regulated"], Field(description="How costly a wrong answer would be. `regulated` tightens the outcome: more is withheld and more is disclosed as uncertain.")] = "low"
    network_policy: Literal["off", "public_https"] = "off"
    host_capabilities: Annotated[list[ShortText], Field(max_length=32)] = []
    candidate_material: Annotated[list[CandidateMaterial], Field(max_length=20)] = []
    cursor: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2048,
            description=(
                "Reserved for future investigation pagination; NOT currently supported. Any "
                "non-null value is rejected with a typed `cursor_not_supported` error rather than "
                "silently ignored. (Evidence-read pagination IS supported via `read_evidence`'s "
                "own cursor.)"
            ),
        ),
    ] | None = None
    host_skills: list[HostSkill] | None = Field(
        default=None,
        max_length=64,
        description=(
            "Skills the CALLING AGENT currently has installed, so this server can tell you which "
            "vetted skills apply to this task and whether your copy matches the approved one. "
            "Send an empty list if you have none or do not know what you have: an empty list "
            "still opts in, and you will be told which skills apply and asked to install them. "
            "OMITTING this field opts out entirely and you receive no skill guidance at all."
        ),
    )
    budgets: Budgets = Budgets()
class EvidenceRecord(ClosedModel):
    ref: Ref
    kind: Literal["local", "captured_live", "source", "capability", "skill"]
    publisher: ShortText
    locator: ShortText
    citation_locator: ShortText
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    extraction_method: Literal[
        "raw_lines", "markdown_section", "html_text", "pdf_text", "api_json", "unknown"
    ] = "unknown"
    provenance_chain: Annotated[list[ShortText], Field(max_length=10)] = []
    redirect_chain: Annotated[list[ShortText], Field(max_length=10)] = []
    pack_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")] | None = None
    jurisdiction: Annotated[str, Field(min_length=2, max_length=32)] | None = None
    language: Annotated[str, Field(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] | None = None
    retrieved_at: datetime | None = None
    published_at: date | None = None
    updated_at: date | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    authority: Literal["primary", "official", "standard", "contextual", "unknown"]
    authority_rationale: ShortText
    freshness: Literal["current", "stale", "expired", "unknown"]
    license: Literal["permitted", "restricted", "prohibited", "unknown"]
    reuse: Literal["permitted", "restricted", "prohibited", "unknown"] = "unknown"
    conflict: Literal["none", "declared", "unknown"]
    uncertainty: Annotated[list[ShortText], Field(max_length=10)] = []
    envelope: PermissionDisclosure | None = None
    @model_validator(mode="after")
    def ordered_dates(self) -> "EvidenceRecord":
        for earlier, later in (("published_at", "updated_at"), ("effective_at", "expires_at")):
            first, second = getattr(self, earlier), getattr(self, later)
            if first and second and second < first:
                raise ValueError("date_order_invalid")
        return self
    @model_serializer(mode="wrap")
    def _omit_absent_envelope(self, handler):
        """Drop `envelope` entirely when absent instead of emitting `null`.

        Responses are serialized with `model_dump(mode="json")` and no `exclude_none`, so a plain
        optional field would add `"envelope": null` to EVERY record -- including those returned to a
        client that never opted into skills. Omitting it keeps the pre-skills response byte-identical,
        which is the compatibility guarantee this gate exists to provide, and is also the truer
        encoding: a record that is not a skill has no envelope, rather than an envelope of null."""
        data = handler(self)
        if data.get("envelope", "absent") is None:
            data.pop("envelope")
        return data
class ClaimRecord(ClosedModel):
    text: ShortText
    state: Literal["supported", "conflicted", "insufficient", "unknown"]
    supporting_refs: list[Ref] = []
    conflicting_refs: list[Ref] = []
class HostAction(ClosedModel):
    kind: Literal["web_search", "fetch_public_url", "inspect_capability", "request_jurisdiction",
                 "consult_professional", "draft_skill_candidate", "install_skill"]
    reason: ShortText
    target: ShortText | None = None
class InvestigationResult(ClosedModel):
    schema_version: Literal["1"]
    status: Literal["complete", "partial", "route_only", "abstained"]
    request_id: Ref
    evidence: list[EvidenceRecord]
    claims: list[ClaimRecord]
    conflicts: list[ShortText]
    gaps: list[ShortText]
    host_actions: list[HostAction]
    warnings: list[ShortText]
    degradation: list[ShortText]
    budgets: Budgets
    next_cursor: str | None = None
class ReadRange(ClosedModel):
    ref: Ref
    start: Annotated[int, Field(ge=1)]
    end: Annotated[int, Field(ge=1)]
    @model_validator(mode="after")
    def ordered(self) -> "ReadRange":
        if self.end < self.start:
            raise ValueError("range_reversed")
        return self
class ReadRequest(ClosedModel):
    refs: Annotated[list[Ref], Field(min_length=1, max_length=10)]
    ranges: Annotated[list[ReadRange], Field(max_length=10)] = []
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    budgets: Budgets = Budgets()
    @model_validator(mode="after")
    def valid_refs(self) -> "ReadRequest":
        if len(set(self.refs)) != len(self.refs):
            raise ValueError("duplicate_ref")
        if any(item.ref not in self.refs for item in self.ranges):
            raise ValueError("range_ref_missing")
        return self
class ReadItem(ClosedModel):
    ref: Ref
    status: Literal["ok", "missing_ref", "stale_ref", "expired_ref", "ineligible_ref", "invalid_range"]
    content: str | None = None
    start: int | None = None
    end: int | None = None
    digest: str | None = None
    truncated: bool = False
    next_cursor: str | None = None
    captured_at: datetime | None = None
    evidence_kind: Literal["local", "captured_live", "source", "capability", "skill"] | None = None
    locator: ShortText | None = None
    citation_locator: ShortText | None = None
    provenance_chain: Annotated[list[ShortText], Field(max_length=10)] = []
    authority: Literal["primary", "official", "standard", "contextual", "unknown"] | None = None
    freshness: Literal["current", "stale", "expired", "unknown"] | None = None
    license: Literal["permitted", "restricted", "prohibited", "unknown"] | None = None
    conflict: Literal["none", "declared", "unknown"] | None = None
class ReadResult(ClosedModel):
    schema_version: Literal["1"]
    request_id: Ref
    items: list[ReadItem]
    warnings: list[ShortText]
    budgets: Budgets
    next_cursor: str | None = None
__all__ = [
    "PermissionDisclosure",
    "HostSkill",
    "InvestigationRequest", "InvestigationResult", "ReadRequest", "ReadResult"
]
