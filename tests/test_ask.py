"""`bruriah ask` -- the command that lets you see the product work before wiring a client.

Until it existed you installed, indexed, and then had to configure an entire MCP client before
anything was observable. That is the failure `undiscoverable-is-unbuilt` describes, sitting inside
the product that ships it.

What these tests protect is the boundary: `ask` is a VIEWER, and adding it must not have widened
the MCP surface or introduced a component that answers questions.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

from bruriah import cli

from test_cli import _fake_embedder_factory

_PAD = ("The following section describes the behaviour of the service in detail, and it is "
        "written in the same language as the rest of the corpus around it. ") * 4


@pytest.fixture
def indexed(tmp_path: Path):
    """A real snapshot, with the fake embedder every other CLI test uses.

    Injected rather than monkeypatched: `build_serve_deps` binds its default factory at definition
    time, so patching the module attribute silently does nothing and the suite quietly loads real
    ONNX. `_cmd_ask` takes the same `embedder_factory` keyword `_cmd_index` and `serve` take."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "apple.md").write_text(f"# Apple\nWhy we chose the apple recipe.\n{_PAD}\n")
    (corpus / "rocket.md").write_text(f"# Rocket\nThe rocket launch decision.\n{_PAD}\n")
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\ninclude: ['**']\nexclude: []\n")
    argv = ["--data-dir", str(tmp_path / "d"), "--config-dir", str(tmp_path / "c")]
    parser = cli._build_cli_parser()
    # The model NAME is recorded in the build descriptor and re-checked when deps are loaded, so
    # it has to match what the fake factory claims or activation fails `embedding_model_mismatch`.
    index_args = parser.parse_args(["index", "--corpus-root", str(corpus), "--policy", str(policy),
                                    "--model", "test/minilm", *argv])
    assert cli._cmd_index(index_args, embedder_factory=_fake_embedder_factory) == 0
    return parser, argv


def _ask(indexed, *arguments: str) -> int:
    parser, argv = indexed
    return cli._cmd_ask(parser.parse_args(["ask", *arguments, *argv]),
                        embedder_factory=_fake_embedder_factory)


def test_it_lists_references_and_not_one_word_of_the_documents(indexed, capsys) -> None:
    """The property the whole design rests on, made visible from a terminal."""
    assert _ask(indexed, "why did we choose the apple recipe") == 0
    out = capsys.readouterr().out
    assert "apple.md" in out
    assert "not_assessed_by_retrieval" in out
    assert "Why we chose the apple recipe" not in out, "document prose reached the listing"


def test_reading_a_reference_is_a_separate_explicit_step(indexed, capsys) -> None:
    _ask(indexed, "why did we choose the apple recipe")
    listing = capsys.readouterr().out
    _ask(indexed, "why did we choose the apple recipe", "--read", "1")
    read = capsys.readouterr().out
    assert "Why we chose the apple recipe" not in listing
    assert "Why we chose the apple recipe" in read


def test_the_suggested_read_command_survives_being_pasted(indexed, capsys) -> None:
    """The listing ends by telling you how to read one, and that line is the bridge to the second
    call -- the entire two-call shape is behind it. It truncated the question to 34 characters
    with an ellipsis and omitted the directory flags, so pasting it asked a different question, or
    the right question of whichever index the default directory happened to hold."""
    question = "why did we choose the apple recipe over every other candidate recipe we tried"
    _ask(indexed, question)
    suggestion = next(
        line.strip() for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("bruriah ask")
    )

    parser, argv = indexed
    # The real check: hand what we printed back to the parser that has to accept it.
    parsed = parser.parse_args(shlex.split(suggestion)[1:])
    assert parsed.question == question, "the suggestion asks a different question than the user did"
    assert parsed.read == [1]
    assert str(parsed.data_dir) == argv[argv.index("--data-dir") + 1], (
        "the suggestion would read from a different index than the listing came from"
    )


def test_the_read_banner_reports_a_character_span_and_names_the_unit(indexed, capsys) -> None:
    """`ReadItem.start`/`end` are character offsets into the passage -- they are what the output
    budget is spent in and what `next_cursor` resumes from. The banner labelled them "lines",
    which printed a span in the hundreds above a body of four. Nothing errors when a caller
    believes it, because every line number is also a valid offset, so the wrong unit just returns
    a window an order of magnitude short."""
    _ask(indexed, "why did we choose the apple recipe", "--read", "1")
    out = capsys.readouterr().out
    banner = next(line for line in out.splitlines() if line.lstrip().startswith("──"))

    assert "lines" not in banner, "a character offset is being called a line number"
    span = re.search(r"chars (\d+)-(\d+)", banner)
    assert span is not None, banner
    start, end = int(span.group(1)), int(span.group(2))
    body = [line for line in out.split(banner, 1)[1].splitlines() if line.strip()]
    assert end - start + 1 > len(body), (
        "the span is a character count, so it cannot also be the number of lines printed under it"
    )
    # The locator keeps carrying the line range, which is the one a person wants next to the text.
    assert re.search(r"\.md#\d+-\d+", banner), banner


def test_a_reference_that_does_not_exist_fails_typed(indexed) -> None:
    with pytest.raises(cli.CliError):
        _ask(indexed, "apple", "--read", "99")


def test_json_mode_is_the_investigation_result_itself(indexed, capsys) -> None:
    # So the terminal view can never drift from what an MCP client would receive: same object.
    assert _ask(indexed, "apple recipe", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) >= {"schema_version", "status", "evidence", "gaps", "degradation"}


def test_abstention_is_reported_as_the_designed_answer(indexed, capsys) -> None:
    _ask(indexed, "what are the employment law requirements for firing someone")
    out = capsys.readouterr().out
    assert "abstained" in out and "no_approved_domain_pack" in out
    assert "not a failure" in out


# --- the boundary this command must not cross -------------------------------------------------


def test_adding_it_did_not_widen_the_mcp_surface() -> None:
    """`ask` is a terminal viewer. The protocol surface is still exactly two read-only tools, and
    a convenience command is the easiest possible way to lose that without noticing."""
    import inspect

    from bruriah import mcp_server

    source = inspect.getsource(mcp_server)
    assert source.count('"investigate_work"') >= 1 and source.count('"read_evidence"') >= 1
    assert '"ask"' not in source and "'ask'" not in source


def test_it_answers_nothing_and_has_nothing_to_answer_with(indexed, capsys) -> None:
    """There is no generative model in this project, so `ask` prints evidence and disclosure only.

    A command named `ask` is exactly where someone would later be tempted to add a summariser.
    Pinned here so that would fail loudly rather than quietly become the product's behaviour."""
    _ask(indexed, "why did we choose the apple recipe")
    out = capsys.readouterr().out
    for invented in ("In summary", "The answer is", "Based on the evidence", "It appears that"):
        assert invented not in out
    assert "references" in out and "authority" in out
