# MCP protocol server (Slice 7B): exposes EXACTLY two tools -- `investigate_work` and
# `read_evidence` -- over the Model Context Protocol, wrapping the frozen
# `service.investigate`/`service.read` composition (Slice 7A/7A-2). This module owns ONLY
# protocol wiring: schema publication, authoritative request validation, structuredContent +
# canonical JSON text fallback, and typed tool errors.
#
# API choice: `mcp.server.lowlevel.Server`, not `FastMCP`'s decorator sugar. FastMCP derives a
# per-tool argument model from the Python function signature via `pydantic.create_model(...)`
# with no `extra="forbid"`, so an unknown top-level field is silently dropped before any
# handler code runs. We pass the frozen contracts.py models' own `model_json_schema()` verbatim
# as both `inputSchema` and `outputSchema`, and validate the RAW, unprocessed `arguments` dict
# ourselves by constructing the frozen model directly, so `extra="forbid"` and cross-field
# validators are what actually rejects invalid input, deterministically, before any work.
from __future__ import annotations

import json
from typing import Any, TypeVar

from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from .contracts import InvestigationRequest, InvestigationResult, ReadRequest, ReadResult
from .service import ServiceDeps, ServiceError, investigate, read

_RequestModel = TypeVar("_RequestModel", InvestigationRequest, ReadRequest)


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _error_result(code: str, message: str) -> CallToolResult:
    body = _canonical_json({"error": {"code": code, "message": message}})
    return CallToolResult(content=[TextContent(type="text", text=body)], isError=True)


def _success_result(result_model: InvestigationResult | ReadResult) -> CallToolResult:
    structured = result_model.model_dump(mode="json")
    return CallToolResult(
        content=[TextContent(type="text", text=_canonical_json(structured))],
        structuredContent=structured,
        isError=False,
    )


def _validate_as_json(model: type[_RequestModel], arguments: dict[str, Any]) -> _RequestModel:
    """Validate `arguments` in pydantic's JSON mode, which is the mode the wire actually used.

    Measured, python vs JSON mode differ on exactly one behavior: the ISO date string.
    `extra="forbid"`, strict scalar types (a bool is still not an int, a `"5"` is still not a
    `5`), range bounds, `Literal` members, unparseable dates, and every cross-field validator
    (`ReadRequest.valid_refs`, `ReadRange.ordered`) reject identically in both."""
    return model.model_validate_json(json.dumps(arguments))


def _invalid_request(model: type[_RequestModel], error: Exception) -> CallToolResult:
    """Refuse with a code a newer client can act on.

    A client built against a newer schema learns exactly which fields this server does not know
    and which it accepts, so it can drop them and retry instead of failing blind. Unknown fields
    are still refused: silently dropping one could change what the request means."""
    if isinstance(error, ValidationError):
        unknown = sorted(
            ".".join(str(part) for part in item["loc"]) for item in error.errors() if item["type"] == "extra_forbidden"
        )
        if unknown and len(unknown) == error.error_count():
            accepted = sorted(model.model_fields)
            message = _canonical_json({"unknown_fields": unknown, "accepted_fields": accepted})
            return _error_result("unsupported_fields", message)
    return _error_result("invalid_request", str(error))


def _handle_investigate(arguments: dict[str, Any], deps: ServiceDeps) -> CallToolResult:
    try:
        request = _validate_as_json(InvestigationRequest, arguments)
    except (ValidationError, TypeError, ValueError) as error:
        return _invalid_request(InvestigationRequest, error)
    try:
        result = investigate(request, deps)
    except ServiceError as error:
        return _error_result(error.code, f"investigate_work failed: {error.code}")
    return _success_result(result)


def _handle_read(arguments: dict[str, Any], deps: ServiceDeps) -> CallToolResult:
    try:
        request = _validate_as_json(ReadRequest, arguments)
    except (ValidationError, TypeError, ValueError) as error:
        return _invalid_request(ReadRequest, error)
    try:
        result = read(request, deps)
    except ServiceError as error:
        return _error_result(error.code, f"read_evidence failed: {error.code}")
    return _success_result(result)
