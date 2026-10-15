#!/usr/bin/env python3
"""Check over the wire that an installed `bruriah serve` delivers the consult activation.

`python scripts/verify_activation.py --bruriah <exe> --data-dir <dir> --config-dir <dir>` spawns
`<exe> serve` over stdio with the `mcp` client, the way an agent client does, and checks:

- `initialize` carries server instructions, at most 2,048 characters (Claude Code truncates
  there), whose first paragraph is at most 512 characters and names both tools;
- `tools/list` lists exactly `investigate_work` and `read_evidence`, each with `_meta`
  `{"anthropic/alwaysLoad": true}` and an `outputSchema`;
- one `investigate_work` call returns `structuredContent` with `schema_version` "2" and a
  `decisions` list.

It prints `activation check passed` and exits 0, or names every failed check on stderr and exits
1. A server that exits, hangs or crashes before answering is a failure, never a pass.

This checks what the wheel emits, not what an agent does with it: the consult rate was measured
separately (`odd/tasks/agent-consult-instructions.md`). Runtime dependencies only (`mcp`,
`anyio`), so it runs in the clean environment the wheel was installed into.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, InitializeResult, ListToolsResult

INVESTIGATE_TOOL = "investigate_work"
READ_TOOL = "read_evidence"
EXPECTED_TOOLS = sorted([INVESTIGATE_TOOL, READ_TOOL])
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}
INSTRUCTIONS_LIMIT = 2048
LEAD_LIMIT = 512
PROBE_TASK = "why did this project choose its storage"


def check_initialize(result: InitializeResult) -> list[str]:
    instructions = result.instructions
    if not instructions:
        return ["initialize: no server instructions"]
    problems = []
    if len(instructions) > INSTRUCTIONS_LIMIT:
        problems.append(
            f"initialize: instructions are {len(instructions)} chars, over the {INSTRUCTIONS_LIMIT} Claude Code keeps"
        )
    lead = instructions.split("\n\n", 1)[0]
    if len(lead) > LEAD_LIMIT:
        problems.append(f"initialize: lead paragraph is {len(lead)} chars, over {LEAD_LIMIT}")
    for tool in (INVESTIGATE_TOOL, READ_TOOL):
        if tool not in lead:
            problems.append(f"initialize: lead paragraph does not name {tool}")
    return problems


def check_tools(result: ListToolsResult) -> list[str]:
    names = sorted(tool.name for tool in result.tools)
    if names != EXPECTED_TOOLS:
        return [f"tools/list: expected {EXPECTED_TOOLS}, got {names}"]
    problems = []
    for tool in sorted(result.tools, key=lambda tool: tool.name):
        # `meta` is the SDK attribute, `_meta` its wire alias; the client fills `meta` only from
        # the `_meta` key, so a match here already proves the key on the wire.
        if tool.meta != ALWAYS_LOAD_META:
            problems.append(f"tools/list: {tool.name} _meta is {tool.meta!r}, expected {ALWAYS_LOAD_META!r}")
        if not tool.outputSchema:
            problems.append(f"tools/list: {tool.name} has no outputSchema")
    return problems


def check_investigation(result: CallToolResult) -> list[str]:
    if result.isError:
        return ["investigate_work: the call returned an error"]
    structured: dict[str, Any] | None = result.structuredContent
    if structured is None:
        return ["investigate_work: no structuredContent"]
    problems = []
    if structured.get("schema_version") != "2":
        problems.append(f"investigate_work: schema_version is {structured.get('schema_version')!r}, expected '2'")
    if not isinstance(structured.get("decisions"), list):
        problems.append("investigate_work: structuredContent has no decisions list")
    return problems


async def verify(session: ClientSession) -> list[str]:
    """Run every check over one connected, not yet initialized session."""
    problems = check_initialize(await session.initialize())
    problems += check_tools(await session.list_tools())
    problems += check_investigation(await session.call_tool(INVESTIGATE_TOOL, {"task": PROBE_TASK}))
    return problems


async def verify_over_stdio(params: StdioServerParameters, timeout: float) -> list[str]:
    with anyio.fail_after(timeout):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            return await verify(session)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bruriah", required=True, help="the installed bruriah executable to spawn")
    parser.add_argument("--data-dir", required=True, help="data directory holding a built index")
    parser.add_argument("--config-dir", required=True, help="configuration directory used at init")
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds before a silent server fails")
    args = parser.parse_args(argv)

    params = StdioServerParameters(
        command=args.bruriah,
        args=["serve", "--data-dir", args.data_dir, "--config-dir", args.config_dir],
    )
    try:
        problems = anyio.run(verify_over_stdio, params, args.timeout)
    except TimeoutError:
        problems = [f"server did not answer within {args.timeout:g}s"]
    except Exception as error:  # an exited or crashed server surfaces here, often as a group
        problems = [f"server failed before answering: {error!r}"]

    if problems:
        print("activation check failed:", file=sys.stderr)
        for problem in problems:
            print(f"- {problem}", file=sys.stderr)
        return 1
    print("activation check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
