"""Characterization tests for the legacy Cerebro runtime.

These tests intentionally exercise the existing runtime without changing its code,
database, client registration, or corpus.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_ROOT = REPOSITORY_ROOT / "cerebro-retrieval"
VAULT_ROOT = REPOSITORY_ROOT / "Cerebro-IA"
LEGACY_SCRIPT = RETRIEVAL_ROOT / "cerebro.py"
LEGACY_PYTHON = RETRIEVAL_ROOT / ".venv/bin/python"
SERVER_PYTHON = Path(os.environ.get("CEREBRO_SERVER_PYTHON", LEGACY_PYTHON))
LEGACY_DATABASE = RETRIEVAL_ROOT / "cerebro.db"


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_manifest() -> dict[str, str]:
    return {
        path.relative_to(VAULT_ROOT).as_posix(): hash_file(path)
        for path in sorted(VAULT_ROOT.rglob("*.md"))
        if path.is_file()
    }


@pytest.fixture(scope="session", autouse=True)
def preserve_source_notes() -> None:
    before = source_manifest()
    assert before, "The canonical vault must contain Markdown source notes"
    yield
    assert source_manifest() == before, "Legacy checks must not alter source notes"


@pytest.fixture(scope="module")
def legacy_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("legacy_cerebro", LEGACY_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_chunking_contract(legacy_module: ModuleType) -> None:
    note = """---
status: verified
---
# Title

Introductory legacy content.

## Retrieval
Legacy retrieval details.
"""

    assert legacy_module.chunk_note(note) == [
        ("", "# Title\n\nIntroductory legacy content."),
        ("Retrieval", "## Retrieval\nLegacy retrieval details."),
    ]


def test_legacy_fts_query_normalization(legacy_module: ModuleType) -> None:
    assert legacy_module.fts_match("Usá MCP + IA, go!") == '"usá" OR "mcp"'


def test_legacy_mcp_remains_queryable_without_mutating_database() -> None:
    assert SERVER_PYTHON.is_file(), "The selected server environment is required"
    assert LEGACY_DATABASE.is_file(), "The retained live database is required"
    database_hash = hash_file(LEGACY_DATABASE)

    async def characterize() -> None:
        parameters = StdioServerParameters(
            command=str(SERVER_PYTHON),
            args=[str(LEGACY_SCRIPT), "serve"],
        )
        async with asyncio.timeout(90):
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert [tool.name for tool in listed.tools] == ["buscar_en_cerebro"]
                    tool = listed.tools[0]
                    assert tool.inputSchema == {
                        "properties": {
                            "consulta": {"title": "Consulta", "type": "string"},
                            "k": {"default": 8, "title": "K", "type": "integer"},
                        },
                        "required": ["consulta"],
                        "title": "buscar_en_cerebroArguments",
                        "type": "object",
                    }
                    result = await session.call_tool(
                        "buscar_en_cerebro", {"consulta": "MCP", "k": 1}
                    )
                    assert not result.isError
                    assert result.content
                    assert "Cerebro — 1 resultados" in result.content[0].text

    asyncio.run(characterize())
    assert hash_file(LEGACY_DATABASE) == database_hash
