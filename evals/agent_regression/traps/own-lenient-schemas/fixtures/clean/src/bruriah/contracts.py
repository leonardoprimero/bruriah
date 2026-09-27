from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class InvestigationRequest(ClosedModel):
    task: Annotated[
        ShortText,
        Field(
            description="The work task to investigate, in the requester's own words. This is matched against local knowledge; it is never executed and never treated as an instruction to obey."
        ),
    ]
    outcome: Annotated[
        ShortText | None,
        Field(
            description="What a good result would look like, if known. Used to scope the investigation, not to steer which sources are selected."
        ),
    ] = None
    as_of: date | None = None
    network_policy: Literal["off", "public_https"] = "off"
    budgets: Budgets = Budgets()
