# MCP protocol server (Slice 7B): exposes EXACTLY two tools -- `investigate_work` and
# `read_evidence` -- over the Model Context Protocol, wrapping the frozen
# `service.investigate`/`service.read` composition (Slice 7A/7A-2).
#
# API choice: `mcp.server.lowlevel.Server`, not `FastMCP`'s decorator sugar. FastMCP derives a
# per-tool argument model from the Python function signature via `pydantic.create_model(...)`
# with no `extra="forbid"`, so an unknown top-level field is silently dropped before any
# handler code runs. Each tool below is still one function: its request and result models come
# from its own annotations, and `_tool` turns them into the published schemas.
from __future__ import annotations

import json
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
from mcp.server.lowlevel import Server
from mcp.types import CallToolResult, TextContent
from mcp.types import Tool as MCPTool
from pydantic import BaseModel, ValidationError

from .contracts import InvestigationRequest, InvestigationResult, ReadRequest, ReadResult
from .service import ServiceDeps, ServiceError, investigate, read

SERVER_NAME = "bruriah"
INVESTIGATE_TOOL = "investigate_work"
READ_TOOL = "read_evidence"


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    request: type[BaseModel]
    result: type[BaseModel]
    run: Callable[[Any, ServiceDeps], BaseModel]


def _tool(name: str, run: Callable[[Any, ServiceDeps], BaseModel]) -> _Tool:
    hints = typing.get_type_hints(run)
    return _Tool(name, (run.__doc__ or "").strip(), hints["request"], hints["return"], run)


def _investigate_work(request: InvestigationRequest, deps: ServiceDeps) -> InvestigationResult:
    """Ask what the user's own indexed knowledge says about a task."""
    return investigate(request, deps)


def _read_evidence(request: ReadRequest, deps: ServiceDeps) -> ReadResult:
    """Read exact, bounded evidence content for stable refs returned by investigate_work."""
    return read(request, deps)


_TOOLS = {tool.name: tool for tool in (_tool(INVESTIGATE_TOOL, _investigate_work), _tool(READ_TOOL, _read_evidence))}


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _error_result(code: str, message: str) -> CallToolResult:
    body = _canonical_json({"error": {"code": code, "message": message}})
    return CallToolResult(content=[TextContent(type="text", text=body)], isError=True)


def _handle(tool: _Tool, arguments: dict[str, Any], deps: ServiceDeps) -> CallToolResult:
    try:
        request = tool.request.model_validate_json(json.dumps(arguments))
    except (ValidationError, TypeError, ValueError) as error:
        return _error_result("invalid_request", str(error))
    try:
        structured = tool.run(request, deps).model_dump(mode="json")
    except ServiceError as error:
        return _error_result(error.code, f"{tool.name} failed: {error.code}")
    return CallToolResult(
        content=[TextContent(type="text", text=_canonical_json(structured))],
        structuredContent=structured,
        isError=False,
    )


def build_server(deps: ServiceDeps) -> Server:
    """Build the MCP protocol server bound to injected `deps`."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[MCPTool]:
        return [
            MCPTool(
                name=tool.name,
                description=tool.description,
                inputSchema=tool.request.model_json_schema(),
                outputSchema=tool.result.model_json_schema(),
            )
            for tool in _TOOLS.values()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        tool = _TOOLS.get(name)
        if tool is None:
            return _error_result("unknown_tool", f"Unknown tool: {name}")
        return await anyio.to_thread.run_sync(_handle, tool, arguments, deps)

    return server


__all__ = ["INVESTIGATE_TOOL", "READ_TOOL", "SERVER_NAME", "build_server"]
