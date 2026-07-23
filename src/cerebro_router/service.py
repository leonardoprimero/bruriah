# Service composition layer (Slice 7A): pure composition of the frozen classify -> lookup ->
# route -> retrieval pipeline into the two public InvestigationResult/ReadResult shapes design.md
# assigns to `investigate_work`/`read_evidence`. This module owns NO protocol, network, or stdio
# wiring -- that is Slice 7B's `mcp_server.py`. Registry/snapshot access is injected via
# `ServiceDeps`, never loaded here, so composition stays testable against real or fixture deps
# alike -- design.md "Architecture": "investigate_work composes tested stages; read_evidence only
# resolves immutable refs."
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from .classify import classify
from .contracts import InvestigationRequest, InvestigationResult, ReadItem, ReadRequest, ReadResult
from .index import ActiveSnapshot
from .lookup import discover
from .registries import Registry
from .retrieval import EmbedQuery, search, to_evidence_records
from .route import route


class ServiceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ServiceDeps:
    """Everything `investigate`/`read` need but must never load themselves: the deterministic
    registry (5A/5B) and the currently active read-only snapshot (Slice 3/4). `embed_query`/
    `clock` mirror `retrieval.search`'s own injection points so tests and later slices control
    them identically. 7B wires the real registry/snapshot; tests wire real or fixture ones."""

    registry: Registry
    snapshot: ActiveSnapshot
    embed_query: EmbedQuery | None = None
    clock: Callable[[], float] = time.monotonic


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _content_hash(request: InvestigationRequest | ReadRequest) -> str:
    # Deterministic request identity: a content hash of the canonical request, never a random
    # UUID or timestamp -- the whole codebase forbids Date.now/random. `cursor` is excluded since
    # it is the caller's *position*, not the request's identity: a resumed call must hash to the
    # same request_id as the original, or cursor validation below becomes circular (a request_id
    # that includes its own cursor can never match once that cursor is set).
    canonical = _canonical_json(request.model_dump(mode="json", exclude={"cursor"}))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _encode_cursor(request_id: str, ref: str, start: int) -> str:
    payload = _canonical_json({"ref": ref, "request_id": request_id, "start": start})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str) -> dict[str, object] | None:
    try:
        value = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_deps(deps: object) -> ServiceDeps:
    if not isinstance(deps, ServiceDeps):
        raise ServiceError("invalid_deps_type")
    if not isinstance(deps.registry, Registry):
        raise ServiceError("invalid_registry_type")
    if not isinstance(deps.snapshot, ActiveSnapshot):
        raise ServiceError("invalid_snapshot_type")
    return deps


def investigate(request: InvestigationRequest, deps: ServiceDeps) -> InvestigationResult:
    """Compose classify -> discover -> route, then, only on `proceed`, retrieve over the
    snapshot. `route_only`/`abstained` carry the route decision's gaps and never retrieve or
    fabricate evidence -- design.md "Routing/retrieval": "Generic discovery only routes or
    abstains." Deterministic: identical `request`/`deps` state always yields an identical result.
    Errors raised by the frozen stages (`ClassificationError`/`LookupError`/`RouteError`/
    `RetrievalError`) are already typed `ValueError` subclasses with a `.code` and propagate
    unwrapped; `ServiceError` is reserved for this module's own request/deps validation.
    """
    if not isinstance(request, InvestigationRequest):
        raise ServiceError("invalid_request_type")
    deps = _validate_deps(deps)
    if request.cursor is not None:
        # retrieval.search (frozen Slice 6A) has no offset/exclusion parameter, so a facade
        # cursor here could never actually resume a truncated request -- returning one anyway
        # would be a false promise. `status=partial` plus `degradation` already signal
        # truncation honestly; real investigate() pagination needs a 6A extension, not a 7A
        # workaround, so a client-supplied cursor fails typed instead of being silently dropped.
        raise ServiceError("cursor_not_supported")

    request_id = _content_hash(request)
    classification = classify(request)
    lookup = discover(classification, deps.registry)
    decision = route(classification, lookup, request)

    evidence, warnings, degradation, status = [], [], [], decision.outcome
    if decision.outcome == "proceed":
        outcome = search(
            deps.snapshot, request.task, request.budgets,
            embed_query=deps.embed_query, clock=deps.clock,
        )
        evidence = to_evidence_records(outcome)
        warnings = list(outcome.warnings)
        degradation = list(outcome.degradation)
        truncated = outcome.truncated
        # Enforce the declared `max_evidence` ceiling. retrieval.search bounds by `max_candidates`
        # (a scan ceiling), not by `max_evidence` (the result-item ceiling), so without this an
        # investigation could return up to `max_candidates` evidence items against a smaller
        # `max_evidence` budget -- "Bounded Investigation": every request MUST enforce the declared
        # evidence-items ceiling. Truncation is reported, never silent.
        if len(evidence) > request.budgets.max_evidence:
            evidence = evidence[: request.budgets.max_evidence]
            degradation.append("max_evidence_exceeded")
            truncated = True
        status = "partial" if truncated else "complete"

    return InvestigationResult(
        schema_version="1", status=status, request_id=request_id, evidence=evidence,
        claims=[], conflicts=[], gaps=list(decision.gaps), host_actions=[],
        warnings=warnings, degradation=degradation, budgets=request.budgets, next_cursor=None,
    )


def _read_one(
    database: sqlite3.Connection, ref: str, requested_range: object, cursor_start: int | None,
    item_cap: int, remaining_total: int, request_id: str,
) -> tuple[ReadItem, int]:
    row = database.execute(
        "SELECT relative_path, start_line, end_line, text, source_hash FROM passages WHERE ref = ?",
        (ref,),
    ).fetchone()
    if row is None:
        return ReadItem(ref=ref, status="missing_ref"), remaining_total
    relative_path, start_line, end_line, text, source_hash = row
    length = len(text)

    start = cursor_start if cursor_start is not None else (requested_range.start if requested_range else 1)
    if start < 1 or start > length:
        return ReadItem(ref=ref, status="invalid_range"), remaining_total
    wanted_end = min(requested_range.end, length) if requested_range else length

    cap = max(min(item_cap, remaining_total), 0)
    window = text[start - 1: wanted_end][:cap]
    actual_end = start - 1 + len(window)
    truncated = actual_end < wanted_end
    next_cursor = _encode_cursor(request_id, ref, actual_end + 1) if truncated else None

    item = ReadItem(
        ref=ref, status="ok", content=window, start=start, end=actual_end,
        digest=f"sha256:{source_hash}", truncated=truncated, next_cursor=next_cursor,
        evidence_kind="local", locator=relative_path,
        citation_locator=f"{relative_path}#{start_line}-{end_line}",
        authority="unknown", freshness="unknown", license="unknown", conflict="unknown",
    )
    return item, remaining_total - len(window)


def read(request: ReadRequest, deps: ServiceDeps) -> ReadResult:
    """Resolve each `refs` entry to immutable local evidence and return exact, budget-bounded
    content. Missing or out-of-range refs get typed per-ref failures -- never another ref's
    content and never a fabricated substitute. `stale_ref`/`expired_ref`/`ineligible_ref` are
    structurally supported statuses but unreachable from 7A's deps: a single active snapshot has
    no history to compare a ref against, so that limitation is stated explicitly rather than
    silently folded into `missing_ref`. Local refs only -- `resolve_source`/`resolve_capability`
    (6B-2) exist for source/capability-kind evidence a later slice may surface; wiring them here
    now would resolve refs `investigate()` never actually emits in 7A, i.e. untested surface.
    """
    if not isinstance(request, ReadRequest):
        raise ServiceError("invalid_request_type")
    deps = _validate_deps(deps)

    request_id = _content_hash(request)
    cursor_ref: str | None = None
    cursor_start: int | None = None
    if request.cursor is not None:
        decoded = _decode_cursor(request.cursor)
        if (
            not decoded or decoded.get("request_id") != request_id
            or decoded.get("ref") not in request.refs or not isinstance(decoded.get("start"), int)
        ):
            raise ServiceError("invalid_cursor")
        cursor_ref, cursor_start = decoded["ref"], decoded["start"]

    ranges_by_ref = {item.ref: item for item in request.ranges}
    remaining = request.budgets.max_output_chars
    items: list[ReadItem] = []
    try:
        for ref in request.refs:
            item, remaining = _read_one(
                deps.snapshot.database, ref, ranges_by_ref.get(ref),
                cursor_start if ref == cursor_ref else None,
                request.budgets.max_extracted_chars, remaining, request_id,
            )
            items.append(item)
    except sqlite3.DatabaseError as error:
        raise ServiceError("snapshot_unreadable") from error

    warnings = ["output_budget_exhausted"] if remaining <= 0 and any(item.truncated for item in items) else []
    next_cursor = next((item.next_cursor for item in items if item.next_cursor), None)
    return ReadResult(
        schema_version="1", request_id=request_id, items=items,
        warnings=warnings, budgets=request.budgets, next_cursor=next_cursor,
    )


__all__ = ["ServiceDeps", "ServiceError", "investigate", "read"]
