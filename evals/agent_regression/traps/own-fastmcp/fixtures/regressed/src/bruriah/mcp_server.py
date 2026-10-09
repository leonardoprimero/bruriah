# MCP protocol server (Slice 7B): exposes EXACTLY two tools -- `investigate_work` and
# `read_evidence` -- over the Model Context Protocol, wrapping the frozen
# `service.investigate`/`service.read` composition. Each tool is one decorated function; the
# SDK derives its input schema from the signature.
from __future__ import annotations

import json
from typing import Any

import anyio.to_thread
from mcp.server.fastmcp import FastMCP

from .contracts import InvestigationRequest, InvestigationResult, ReadRequest, ReadResult
from .service import ServiceDeps, ServiceError, investigate, read

SERVER_NAME = "bruriah"
INVESTIGATE_TOOL = "investigate_work"
READ_TOOL = "read_evidence"


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def build_server(deps: ServiceDeps) -> FastMCP:
    """Build the MCP protocol server bound to injected `deps`."""
    server = FastMCP(SERVER_NAME)

    @server.tool(name=INVESTIGATE_TOOL, structured_output=True)
    async def investigate_work(request: InvestigationRequest) -> InvestigationResult:
        """Ask what the user's own indexed knowledge says about a task."""
        return await anyio.to_thread.run_sync(investigate, request, deps)

    @server.tool(name=READ_TOOL, structured_output=True)
    async def read_evidence(request: ReadRequest) -> ReadResult:
        """Read exact, bounded evidence content for stable refs returned by investigate_work."""
        return await anyio.to_thread.run_sync(read, request, deps)

    return server


__all__ = ["INVESTIGATE_TOOL", "READ_TOOL", "SERVER_NAME", "ServiceError", "build_server"]
