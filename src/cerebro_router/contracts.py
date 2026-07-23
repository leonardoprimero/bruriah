from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
class InvestigationRequest(ClosedModel):
    task: ShortText
    outcome: ShortText | None = None
    jurisdiction: Annotated[str, Field(min_length=2, max_length=32)] | None = None
    as_of: date | None = None
    risk_class: Literal["low", "medium", "high", "regulated"] = "low"
    network_policy: Literal["off", "public_https"] = "off"
    host_capabilities: Annotated[list[ShortText], Field(max_length=32)] = []
    candidate_material: Annotated[list[CandidateMaterial], Field(max_length=20)] = []
    cursor: Annotated[str, Field(min_length=1, max_length=2048)] | None = None
    budgets: Budgets = Budgets()
class EvidenceRecord(ClosedModel):
    ref: Ref
    kind: Literal["local", "captured_live", "source", "capability"]
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
    @model_validator(mode="after")
    def ordered_dates(self) -> "EvidenceRecord":
        for earlier, later in (("published_at", "updated_at"), ("effective_at", "expires_at")):
            first, second = getattr(self, earlier), getattr(self, later)
            if first and second and second < first:
                raise ValueError("date_order_invalid")
        return self
class ClaimRecord(ClosedModel):
    text: ShortText
    state: Literal["supported", "conflicted", "insufficient", "unknown"]
    supporting_refs: list[Ref] = []
    conflicting_refs: list[Ref] = []
class HostAction(ClosedModel):
    kind: Literal["web_search", "fetch_public_url", "inspect_capability", "request_jurisdiction", "consult_professional"]
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
    evidence_kind: Literal["local", "captured_live", "source", "capability"] | None = None
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
    "InvestigationRequest", "InvestigationResult", "ReadRequest", "ReadResult"
]
