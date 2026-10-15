"""Tests for `scripts/verify_activation.py`, the CI check that the installed wheel delivers the
activation (server instructions, `alwaysLoad` tool metadata, the `decisions` field) over the wire.

The passing case drives the real server over in-memory streams through the same `verify` coroutine
the script runs against `bruriah serve` over stdio. Each failing case hands the pure checks a
response missing exactly one part of the activation, so every rule is shown to fail on its own.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import anyio
from bruriah.mcp_server import SERVER_INSTRUCTIONS, build_server
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.types import (
    CallToolResult,
    Implementation,
    InitializeResult,
    ListToolsResult,
    ServerCapabilities,
    TextContent,
    Tool,
)
from test_mcp_contract import _default_notes, _deps_for

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "verify_activation.py"

_spec = importlib.util.spec_from_file_location("verify_activation", SCRIPT)
assert _spec is not None and _spec.loader is not None
verify_activation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_activation)

_ALWAYS_LOAD = {"anthropic/alwaysLoad": True}
_SCHEMA = {"type": "object"}


def _initialize(instructions: str | None) -> InitializeResult:
    return InitializeResult(
        protocolVersion="2025-06-18",
        capabilities=ServerCapabilities(),
        serverInfo=Implementation(name="bruriah", version="0"),
        instructions=instructions,
    )


def _tool(name: str, *, meta: dict | None = _ALWAYS_LOAD, output_schema: dict | None = _SCHEMA) -> Tool:
    return Tool(name=name, inputSchema=_SCHEMA, outputSchema=output_schema, _meta=meta)


def _tools(*tools: Tool) -> ListToolsResult:
    return ListToolsResult(tools=list(tools))


def _both_tools() -> ListToolsResult:
    return _tools(_tool("investigate_work"), _tool("read_evidence"))


def _investigation(structured: dict | None, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text="{}")], structuredContent=structured, isError=is_error)


async def _verify_real_server(deps) -> list[str]:
    server = build_server(deps)
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server.run(*server_streams, server.create_initialization_options()))
            async with ClientSession(*client_streams) as session:
                problems = await verify_activation.verify(session)
            tg.cancel_scope.cancel()
    return problems


def test_the_real_server_delivers_the_whole_activation(tmp_path) -> None:
    with _deps_for(tmp_path, _default_notes()) as deps:
        assert anyio.run(_verify_real_server, deps) == []


def test_the_shipped_instructions_pass_the_initialize_check() -> None:
    assert verify_activation.check_initialize(_initialize(SERVER_INSTRUCTIONS)) == []


def test_missing_instructions_fail() -> None:
    assert verify_activation.check_initialize(_initialize(None)) == ["initialize: no server instructions"]
    assert verify_activation.check_initialize(_initialize("")) == ["initialize: no server instructions"]


def test_instructions_past_the_claude_code_truncation_limit_fail() -> None:
    text = "investigate_work read_evidence\n\n" + "x" * 2048
    assert verify_activation.check_initialize(_initialize(text)) == [
        f"initialize: instructions are {len(text)} chars, over the 2048 Claude Code keeps"
    ]


def test_a_lead_paragraph_that_does_not_stand_alone_fails() -> None:
    long_lead = "investigate_work read_evidence " + "x" * 500
    assert verify_activation.check_initialize(_initialize(long_lead)) == [
        f"initialize: lead paragraph is {len(long_lead)} chars, over 512"
    ]
    assert verify_activation.check_initialize(_initialize("Call investigate_work.\n\nThen read_evidence.")) == [
        "initialize: lead paragraph does not name read_evidence"
    ]


def test_both_always_loaded_tools_pass_the_tools_check() -> None:
    assert verify_activation.check_tools(_both_tools()) == []


def test_a_third_or_missing_tool_fails() -> None:
    three = _tools(_tool("investigate_work"), _tool("read_evidence"), _tool("delete_all"))
    assert verify_activation.check_tools(three) == [
        "tools/list: expected ['investigate_work', 'read_evidence'], got ['delete_all', 'investigate_work', 'read_evidence']"
    ]
    assert verify_activation.check_tools(_tools(_tool("investigate_work"))) == [
        "tools/list: expected ['investigate_work', 'read_evidence'], got ['investigate_work']"
    ]


def test_a_tool_without_always_load_fails() -> None:
    listed = _tools(_tool("investigate_work", meta=None), _tool("read_evidence"))
    assert verify_activation.check_tools(listed) == [
        "tools/list: investigate_work _meta is None, expected {'anthropic/alwaysLoad': True}"
    ]


def test_a_tool_without_an_output_schema_fails() -> None:
    listed = _tools(_tool("investigate_work"), _tool("read_evidence", output_schema=None))
    assert verify_activation.check_tools(listed) == ["tools/list: read_evidence has no outputSchema"]


def test_a_v2_investigation_with_decisions_passes() -> None:
    assert verify_activation.check_investigation(_investigation({"schema_version": "2", "decisions": []})) == []


def test_an_investigation_without_decisions_fails() -> None:
    assert verify_activation.check_investigation(_investigation({"schema_version": "2"})) == [
        "investigate_work: structuredContent has no decisions list"
    ]


def test_an_investigation_on_another_schema_fails() -> None:
    assert verify_activation.check_investigation(_investigation({"schema_version": "1", "decisions": []})) == [
        "investigate_work: schema_version is '1', expected '2'"
    ]


def test_an_errored_or_unstructured_investigation_fails() -> None:
    assert verify_activation.check_investigation(
        _investigation({"schema_version": "2", "decisions": []}, is_error=True)
    ) == ["investigate_work: the call returned an error"]
    assert verify_activation.check_investigation(_investigation(None)) == ["investigate_work: no structuredContent"]


def test_the_script_exits_nonzero_naming_the_failure_when_serve_cannot_start(tmp_path) -> None:
    # No index under an empty data dir: the real `bruriah serve` exits before the handshake, which
    # is the failure CI would hit. The check must fail loudly with a nonzero exit, not hang and not
    # report success.
    bruriah = shutil.which("bruriah", path=str(Path(sys.executable).parent))
    assert bruriah is not None, "the bruriah console script must sit next to the test interpreter"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--bruriah",
            bruriah,
            "--data-dir",
            str(tmp_path / "data"),
            "--config-dir",
            str(tmp_path / "config"),
            "--timeout",
            "20",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "activation check failed" in completed.stderr
