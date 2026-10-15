"""The client activation matrix in `docs/client-guidance.md`, and the README claim it scopes.

The server sends the same activation to every client: MCP `instructions` in its initialize result
and `_meta` `anthropic/alwaysLoad` on both tools. Whether a client puts that in front of its model,
and whether unprompted agents then consult Bruriah, has been measured for Claude Code only. The
matrix states those three facts per client, and these tests keep it from claiming more.
"""

from __future__ import annotations

from pathlib import Path
import re

from bruriah.clients import CLIENT_CAPABILITIES, ClientId
from bruriah.mcp_server import ALWAYS_LOAD_META, SERVER_INSTRUCTIONS

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE = ROOT / "docs" / "client-guidance.md"
README = ROOT / "README.md"
MATRIX_HEADING = "## Activation matrix"
COLUMNS = ["client", "emitted", "delivered", "consult measured"]
MEASURED_CLIENT = CLIENT_CAPABILITIES[ClientId.CLAUDE_CODE].display_name


def _matrix() -> list[dict[str, str]]:
    text = GUIDANCE.read_text(encoding="utf-8")
    _, heading, section = text.partition(MATRIX_HEADING)
    assert heading, f"{GUIDANCE.name} has no '{MATRIX_HEADING}' section"
    section = section.split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert len(rows) >= 2, "the activation matrix has no table"

    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    header = cells(rows[0])
    assert header == COLUMNS
    assert all(set(cell) <= set("-: ") for cell in cells(rows[1])), "second row must be the separator"
    return [dict(zip(header, cells(row), strict=True)) for row in rows[2:]]


def test_matrix_has_one_row_per_supported_client_in_order() -> None:
    assert [row["client"] for row in _matrix()] == [
        CLIENT_CAPABILITIES[client_id].display_name for client_id in ClientId
    ]


def test_every_client_is_sent_the_activation() -> None:
    # The emitted column is backed by the server itself: it sends both, unconditionally.
    assert SERVER_INSTRUCTIONS
    assert ALWAYS_LOAD_META == {"anthropic/alwaysLoad": True}
    assert {row["emitted"] for row in _matrix()} == {"yes"}


def test_only_claude_code_is_claimed_to_deliver_the_activation() -> None:
    delivered = {row["client"]: row["delivered"] for row in _matrix()}
    assert delivered.pop(MEASURED_CLIENT).startswith("yes")
    assert set(delivered.values()) == {"unknown"}


def test_only_claude_code_has_a_measured_consult_rate() -> None:
    measured = {row["client"]: row["consult measured"] for row in _matrix()}
    claude_code = measured.pop(MEASURED_CLIENT)
    assert "15/15" in claude_code
    assert "0/15" in claude_code
    assert "evals/agent_regression/README.md" in claude_code
    assert set(measured.values()) == {"not measured"}


def _prose_sentences(markdown: str) -> list[str]:
    """Sentences of `markdown`'s prose paragraphs; fenced code never joins a sentence."""
    prose = re.sub(r"^```.*?^```", "", markdown, flags=re.MULTILINE | re.DOTALL)
    return [
        sentence
        for paragraph in re.split(r"\n\s*\n", prose)
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(paragraph.split()))
    ]


def test_readme_scopes_the_no_instruction_claim_to_claude_code() -> None:
    sentences = [
        sentence
        for sentence in _prose_sentences(README.read_text(encoding="utf-8"))
        if "do not need to tell the agent to use it" in sentence.lower()
    ]
    assert sentences, "README.md no longer makes the no-instruction claim"
    assert all(MEASURED_CLIENT in sentence for sentence in sentences)
