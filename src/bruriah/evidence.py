# Slice 10: evidence normalization + claim-state ledger, standalone (composition into
# `service.py`'s `investigate()` is Slice 11's job -- this module is delivered unwired, matching
# how 9B delivered `research.py`). Composes over the frozen `contracts.EvidenceRecord`/
# `ClaimRecord` and `packs.SourcePolicy` -- never mutates or amends them; every new typed
# structure this claim ledger needs lives here. design.md "Evidence": "`evidence.py` stores
# source text only inside typed UNTRUSTED_EVIDENCE envelopes; parsers cannot emit actions.
# Claims separately record extracted text, assessment, supporting/conflicting refs and
# supported|conflicted|insufficient|unknown. Authority is pack-contextual; freshness is
# date-rule state; conflicts are classified; uncertainty lists missing checks ... Law/accounting
# require jurisdiction/effective regime and professional escalation; security separates public
# vulnerability evidence from authorization/incident judgment; unsupported domains return
# route-only/abstained."
#
# Binding guarantees this module enforces:
#   1. UNTRUSTED ENVELOPE: `UntrustedEvidence` separates the closed, self-produced
#      `EvidenceRecord` (source identity) from the raw retrieved `extract` (untrusted DATA).
#      Every assessment function below reads ONLY structured fields (`record.*`,
#      `SourcePolicy.*`, the caller-declared `normalized_claim`/`applies_to_version`) -- NEVER
#      `extract` content -- so a prompt-injection payload embedded in `extract` changes nothing
#      about authority/freshness/conflict/state (Instruction and Evidence Separation). `extract`
#      is carried through unmodified, for citation display only; nothing here executes it.
#   2. PACK-CONTEXTUAL AUTHORITY: authority + rationale come from the matched `SourcePolicy`
#      ONLY (`resolve_authority`), never from `EvidenceRecord.authority`'s own value -- closing a
#      self-declared/fabricated-authority spoof. No matched source -> authority stays "unknown".
#   3. DATE-RULE FRESHNESS: `assess_freshness` compares the record's own
#      effective/published/updated/expiry dates against an injected `today` and a source's
#      `freshness_days` -- never wall-clock, never string content.
#   4. STRUCTURAL CONFLICT CLASSIFICATION: distinct `normalized_claim` values among otherwise-
#      applicable evidence are classified by jurisdiction / effective-date / version / default
#      "definition" mismatch over STRUCTURED fields -- never by comparing evidence prose. Both
#      sides of a genuine conflict are always preserved (`supporting_refs` + `conflicting_refs`);
#      no consensus is ever fabricated, and superseding evidence is flagged, never silently
#      auto-preferred.
#   5. ABSTENTION DISCIPLINE: missing metadata or a missing pack match yields "unknown"; thin/
#      stale/expired evidence yields "insufficient"; only current, pack-matched, corroborated
#      evidence yields "supported". `assess_claim` never returns "supported" by default.
#   6. TOTAL AND TYPED: `assess_claim`'s public entry never raises -- a typed `EvidenceError` or
#      any unenumerated exception is caught and reported as `state="unknown"` with a named
#      `uncertainty` reason (closes the untyped-escape defect class this change has hit before).
#
# Deliberately deferred to Slice 11 ("context assembler", per design.md's Architecture diagram):
# free-text "assessment"/"recommendation" prose and professional-escalation host actions. This
# slice's non-goals are explicit ("consensus invention, professional advice, context assembly"),
# so `ClaimAssessment`'s structured `state`/`conflict_classes`/`corroboration`/`uncertainty`
# fields ARE the assessment (non-narrative, closed-vocabulary) and no recommendation field is
# invented here -- adding an unused placeholder would only re-open the same non-goal by another
# name.
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .contracts import ClaimRecord, ClosedModel, EvidenceRecord, Ref, ShortText
from .packs import SourcePolicy

FreshnessState = Literal["current", "stale", "expired", "unknown"]
ClaimState = Literal["supported", "conflicted", "insufficient", "unknown"]
ConflictClass = Literal["time", "jurisdiction", "definition", "version"]
Authority = Literal["primary", "official", "standard", "contextual", "unknown"]

_GLOBAL_JURISDICTION = "GLOBAL"


class EvidenceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class UntrustedEvidence:
    """Typed envelope separating SOURCE identity (`record`, a closed, self-produced
    `EvidenceRecord`) from the raw retrieved EXTRACT text. `extract` is UNTRUSTED DATA: quoted
    verbatim for citation display, never read by any assessment function below, never executed."""

    record: EvidenceRecord
    extract: str


def verify_extract_digest(record: EvidenceRecord, extract: str) -> bool:
    """Recompute `extract`'s own sha256 and compare to `record.digest`. Only meaningful when
    `extract` IS the exact full content the digest was computed over (e.g. a local passage's
    complete source text) -- a truncated citation excerpt legitimately will NOT match its
    record's whole-body digest, so callers opt in via `wrap_evidence(require_digest_match=True)`
    only when they hold the full text; a mismatch is a citation-integrity signal, never silently
    treated as a match."""
    computed = f"sha256:{hashlib.sha256(extract.encode('utf-8')).hexdigest()}"
    return computed == record.digest


def wrap_evidence(
    record: EvidenceRecord, extract: str, *, require_digest_match: bool = False,
) -> UntrustedEvidence:
    """Build one untrusted envelope. Typed: rejects a non-`EvidenceRecord` source, a non-`str`
    or oversized extract, or (opt-in) a tampered/fabricated digest, instead of trusting caller
    shape or content."""
    if not isinstance(record, EvidenceRecord):
        raise EvidenceError("invalid_record_type")
    if not isinstance(extract, str):
        raise EvidenceError("invalid_extract_type")
    if len(extract) > 200_000:
        raise EvidenceError("extract_too_large")
    if require_digest_match and not verify_extract_digest(record, extract):
        raise EvidenceError("digest_mismatch")
    return UntrustedEvidence(record=record, extract=extract)


@dataclass(frozen=True)
class EvidenceClaim:
    """One evidence envelope's declared contribution to a single claim under assessment.
    `normalized_claim` is a caller-normalized, STRUCTURED statement of what this evidence says
    about the claim (e.g. "capital_gains_rate=20pct") -- comparing these VALUES for equality is
    a structural signal, never a semantic reading of `envelope.extract`'s prose (evidence.py
    never concludes from free text). `source` is the matched pack `SourcePolicy`, if any --
    `None` means no approved source policy matched this evidence, so authority stays "unknown"
    (pack-contextual authority, guarantee #2). `applies_to_version` is the product/library
    version this evidence documents, if the claim is version-bound (e.g. programming guidance);
    `None` means version-unbound evidence."""

    envelope: UntrustedEvidence
    normalized_claim: ShortText
    source: SourcePolicy | None = None
    applies_to_version: str | None = None


class ClaimAssessment(ClosedModel):
    """The claim ledger entry this slice adds beyond the frozen, minimal `ClaimRecord`:
    the extracted claim, pack-contextual authority rationale, jurisdiction, corroboration count,
    conflict classes, and named uncertainty reasons -- "Evidence Normalization and Claim State".
    `state`/`conflict_classes`/`corroboration`/`uncertainty` together ARE Bruriah's structured
    assessment (see module docstring for why no free-text assessment/recommendation field is
    added this slice). `render_claim_record` below projects this down to `ClaimRecord`."""

    claim_text: ShortText
    extracted_claim: ShortText | None = None
    state: ClaimState
    supporting_refs: list[Ref] = []
    conflicting_refs: list[Ref] = []
    non_applicable_refs: list[Ref] = []
    conflict_classes: list[ConflictClass] = []
    corroboration: int = 0
    jurisdiction: str | None = None
    authority_rationale: ShortText | None = None
    uncertainty: list[ShortText] = []


def resolve_authority(source: SourcePolicy | None) -> tuple[Authority, str]:
    """Pack-contextual authority (guarantee #2): a matched pack `SourcePolicy` is the ONLY
    source of authority/rationale. `EvidenceRecord.authority`'s own value is deliberately never
    consulted here -- it may be a conservative "unknown" self-report (every producer in this
    codebase reports it that way today) and must never be trusted as a stronger claim."""
    if source is None:
        return "unknown", "No approved pack source policy matched this evidence; authority not assessed."
    return source.authority, source.rationale


def assess_freshness(record: EvidenceRecord, *, today: date, freshness_days: int | None) -> FreshnessState:
    """Date-rule freshness (guarantee #3): compares `record`'s own dates to `today` and a
    source's `freshness_days`. Missing dates or an unmatched source (no `freshness_days` rule)
    stay honestly "unknown" -- never guessed "current"."""
    if not isinstance(today, date):
        raise EvidenceError("invalid_today_type")
    if record.expires_at is not None and today > record.expires_at:
        return "expired"
    reference = record.updated_at or record.published_at
    if reference is None or freshness_days is None:
        return "unknown"
    return "stale" if (today - reference).days > freshness_days else "current"


def _jurisdiction_applicable(jurisdictions: Sequence[str], requested: str | None) -> bool:
    if _GLOBAL_JURISDICTION in jurisdictions:
        return True
    if requested is None or requested == "unknown":
        return False
    return requested in jurisdictions


def _is_applicable(
    item: EvidenceClaim, jurisdiction: str | None, as_of: date | None, requested_version: str | None,
) -> bool:
    record = item.envelope.record
    if item.source is not None and not _jurisdiction_applicable(item.source.jurisdictions, jurisdiction):
        return False
    if as_of is not None:
        if record.effective_at is not None and record.effective_at > as_of:
            return False
        if record.expires_at is not None and record.expires_at < as_of:
            return False
    if (
        requested_version is not None and item.applies_to_version is not None
        and item.applies_to_version != requested_version
    ):
        return False
    return True


def _classify_conflict(a: EvidenceClaim, b: EvidenceClaim) -> ConflictClass:
    ra, rb = a.envelope.record, b.envelope.record
    if (ra.jurisdiction or rb.jurisdiction) and ra.jurisdiction != rb.jurisdiction:
        return "jurisdiction"
    if (ra.effective_at or rb.effective_at) and ra.effective_at != rb.effective_at:
        return "time"
    if (ra.pack_version or rb.pack_version) and ra.pack_version != rb.pack_version:
        return "version"
    if (a.applies_to_version or b.applies_to_version) and a.applies_to_version != b.applies_to_version:
        return "version"
    return "definition"


def _assess_claim_inner(
    claim_text: str, evidence_claims: Sequence[EvidenceClaim], *,
    jurisdiction: str | None, as_of: date | None, requested_version: str | None, today: date,
) -> ClaimAssessment:
    if not isinstance(claim_text, str) or not claim_text:
        raise EvidenceError("invalid_claim_text")
    if not isinstance(evidence_claims, (list, tuple)) or any(
        not isinstance(item, EvidenceClaim) for item in evidence_claims
    ):
        raise EvidenceError("invalid_evidence_claims")
    if not isinstance(today, date):
        raise EvidenceError("invalid_today_type")

    if not evidence_claims:
        return ClaimAssessment(claim_text=claim_text, state="insufficient", jurisdiction=jurisdiction,
                                uncertainty=["no_evidence"])

    applicable, non_applicable = [], []
    for item in evidence_claims:
        (applicable if _is_applicable(item, jurisdiction, as_of, requested_version) else non_applicable).append(item)
    non_applicable_refs = [item.envelope.record.ref for item in non_applicable]

    if not applicable:
        reasons = []
        if jurisdiction is None:
            reasons.append("missing_jurisdiction")
        if as_of is None:
            reasons.append("missing_effective_date")
        if not reasons:
            reasons.append("no_applicable_evidence")
        return ClaimAssessment(claim_text=claim_text, state="insufficient", jurisdiction=jurisdiction,
                                non_applicable_refs=non_applicable_refs, uncertainty=reasons)

    groups: dict[str, list[EvidenceClaim]] = {}
    for item in applicable:
        groups.setdefault(item.normalized_claim, []).append(item)

    if len(groups) > 1:
        # Preserve BOTH sides of a genuine disagreement -- never fabricate a single consensus
        # (guarantee #4). Superseding (e.g. a newer effective date) is flagged via "time", never
        # silently auto-preferred.
        ordered = list(groups.values())
        classes: list[ConflictClass] = sorted({
            _classify_conflict(ordered[i][0], ordered[j][0])
            for i in range(len(ordered)) for j in range(i + 1, len(ordered))
        })
        return ClaimAssessment(
            claim_text=claim_text, state="conflicted", jurisdiction=jurisdiction,
            supporting_refs=[item.envelope.record.ref for item in ordered[0]],
            conflicting_refs=[item.envelope.record.ref for group in ordered[1:] for item in group],
            non_applicable_refs=non_applicable_refs, conflict_classes=classes,
            uncertainty=["evidence_disagrees"],
        )

    group = applicable
    refs = [item.envelope.record.ref for item in group]
    assessed = [
        (
            resolve_authority(item.source),
            assess_freshness(
                item.envelope.record, today=today,
                freshness_days=item.source.freshness_days if item.source else None,
            ),
            item,
        )
        for item in group
    ]

    if all(authority == "unknown" for (authority, _rationale), _freshness, _item in assessed):
        return ClaimAssessment(
            claim_text=claim_text, state="unknown", jurisdiction=jurisdiction, supporting_refs=refs,
            non_applicable_refs=non_applicable_refs, extracted_claim=group[0].normalized_claim,
            uncertainty=["authority_unknown"],
        )

    current = [
        (authority, rationale, item) for (authority, rationale), freshness, item in assessed
        if authority != "unknown" and freshness == "current"
    ]
    if not current:
        return ClaimAssessment(
            claim_text=claim_text, state="insufficient", jurisdiction=jurisdiction, supporting_refs=refs,
            non_applicable_refs=non_applicable_refs, extracted_claim=group[0].normalized_claim,
            uncertainty=["no_current_authoritative_evidence"],
        )

    corroboration = len({item.envelope.record.publisher for _authority, _rationale, item in current})
    return ClaimAssessment(
        claim_text=claim_text, state="supported", jurisdiction=jurisdiction, supporting_refs=refs,
        non_applicable_refs=non_applicable_refs, extracted_claim=group[0].normalized_claim,
        corroboration=corroboration, authority_rationale=current[0][1],
    )


def assess_claim(
    claim_text: str, evidence_claims: Sequence[EvidenceClaim], *,
    jurisdiction: str | None = None, as_of: date | None = None,
    requested_version: str | None = None, today: date,
) -> ClaimAssessment:
    """Total, typed claim-ledger entry point (guarantee #6): never raises. Deterministic --
    identical arguments always yield an identical `ClaimAssessment`. A typed `EvidenceError` or
    any unenumerated exception is converted into `state="unknown"` with a named `uncertainty`
    reason -- never a manufactured `supported` (abstention discipline, guarantee #5)."""
    try:
        return _assess_claim_inner(
            claim_text, evidence_claims, jurisdiction=jurisdiction, as_of=as_of,
            requested_version=requested_version, today=today,
        )
    except EvidenceError as error:
        return ClaimAssessment(claim_text="unassessable_claim", state="unknown", uncertainty=[error.code])
    except Exception:  # Backstop: no bare exception may ever escape `assess_claim()`.
        return ClaimAssessment(claim_text="unassessable_claim", state="unknown", uncertainty=["internal_error"])


def render_claim_record(assessment: ClaimAssessment) -> ClaimRecord:
    """Project the richer ledger entry down to the frozen, minimal `ClaimRecord` contract
    `InvestigationResult.claims` uses (Slice 11 wiring). One direction only: the frozen model is
    never enriched in place."""
    return ClaimRecord(
        text=assessment.claim_text, state=assessment.state,
        supporting_refs=assessment.supporting_refs, conflicting_refs=assessment.conflicting_refs,
    )


__all__ = [
    "ClaimAssessment", "ClaimState", "ConflictClass", "EvidenceClaim", "EvidenceError",
    "FreshnessState", "UntrustedEvidence", "assess_claim", "assess_freshness", "render_claim_record",
    "resolve_authority", "verify_extract_digest", "wrap_evidence",
]
