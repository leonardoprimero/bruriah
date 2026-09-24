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
import subprocess
from pathlib import Path

import pytest

from bruriah import cli

from test_cli import _fake_embedder_factory

_PAD = (
    "The following section describes the behaviour of the service in detail, and it is "
    "written in the same language as the rest of the corpus around it. "
) * 4


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
    # Spaces on purpose: the default data directory on macOS is `~/Library/Application Support/
    # bruriah`, so a path without one is not the path most of this project's users actually have.
    argv = ["--data-dir", str(tmp_path / "data dir"), "--config-dir", str(tmp_path / "config dir")]
    parser = cli._build_cli_parser()
    # The model NAME is recorded in the build descriptor and re-checked when deps are loaded, so
    # it has to match what the fake factory claims or activation fails `embedding_model_mismatch`.
    index_args = parser.parse_args(
        ["index", "--corpus-root", str(corpus), "--policy", str(policy), "--model", "test/minilm", *argv]
    )
    assert cli._cmd_index(index_args, embedder_factory=_fake_embedder_factory) == 0
    return parser, argv


def _ask(indexed, *arguments: str) -> int:
    parser, argv = indexed
    return cli._cmd_ask(parser.parse_args(["ask", *arguments, *argv]), embedder_factory=_fake_embedder_factory)


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
        line.strip() for line in capsys.readouterr().out.splitlines() if line.strip().startswith("bruriah ask")
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


def test_index_prune_clears_what_reindexing_stranded(indexed, capsys) -> None:
    """End to end, through the parser a person actually types at. Re-indexing under a changed
    policy promotes and drops the old generation, which leaves it on disk referenced by nothing."""
    parser, argv = indexed
    data_dir = Path(argv[argv.index("--data-dir") + 1])
    corpus = next(path for path in data_dir.parent.iterdir() if path.name == "corpus")
    edited = data_dir.parent / "edited.yaml"
    edited.write_text("version: 1\ninclude: ['**']\nexclude: ['nothing/**']\n")
    assert (
        cli._cmd_index(
            parser.parse_args(
                ["index", "--corpus-root", str(corpus), "--policy", str(edited), "--model", "test/minilm", *argv]
            ),
            embedder_factory=_fake_embedder_factory,
        )
        == 0
    )
    stranded = sorted(data_dir.glob("candidate-*.sqlite3"))
    assert len(stranded) == 2, "the superseded generation should still be on disk"
    capsys.readouterr()

    assert cli.bruriah_main(["index-prune", *argv]) == 0

    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["removed"]) == 1
    assert "Removed 1 unreferenced generation(s)." in captured.err
    assert len(sorted(data_dir.glob("candidate-*.sqlite3"))) == 1


def test_a_reference_that_does_not_exist_fails_typed(indexed) -> None:
    with pytest.raises(cli.CliError):
        _ask(indexed, "apple", "--read", "99")


def test_resolve_doc_refs_for_humans_resolves_known_and_keeps_unknown_refs_raw(indexed) -> None:
    """R3-cli-human-ref-resolution-uncovered (T2 review, carried into T3): `_resolve_doc_refs_for_
    humans` previously had only INDIRECT coverage, through `_cmd_ask`'s full human-view pipeline.
    Exercises it directly against a real snapshot: a `doc:v1:` ref for an indexed document
    resolves to its corpus-relative path, and one that resolves to nothing is left exactly as it
    was, never dropped or replaced with a placeholder."""
    from bruriah.corpus import document_ref_for
    from bruriah.repository import SnapshotRepository

    parser, argv = indexed
    args = parser.parse_args(["ask", "why did we choose the apple recipe", *argv])
    paths = cli._resolve_paths(args)
    deps = cli.build_serve_deps(paths, embedder_factory=_fake_embedder_factory)
    try:
        repo = SnapshotRepository(deps.snapshot.database)
        known_ref = document_ref_for("apple.md")
        unknown_ref = "doc:v1:" + "0" * 64
        resolved = cli._resolve_doc_refs_for_humans(f"see {known_ref} and also {unknown_ref}", repo)
        assert "apple.md" in resolved
        assert unknown_ref in resolved
        assert known_ref not in resolved
    finally:
        deps.snapshot.database.close()


def test_json_mode_never_resolves_document_refs_to_paths(indexed, capsys) -> None:
    """The human-view resolution `_resolve_doc_refs_for_humans` performs is human-view ONLY:
    `--json` is the wire payload an MCP client would receive, and it must keep the opaque
    `doc:v1:` ref exactly as `investigate_work` returns it -- never a corpus-relative path a
    client never asked to have resolved."""
    assert _ask(indexed, "why did we choose the apple recipe", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    locators = [item["locator"] for item in payload["evidence"] if item["kind"] == "local"]
    assert locators and all(loc.startswith("doc:v1:") for loc in locators)
    assert "apple.md" not in json.dumps(payload)


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


def test_ask_with_code_target_and_repo(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "code_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Leonardo Caliva"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "leo@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "storage.py").write_text("def init_db():\n    return 'sqlite'\n", encoding="utf-8")
    subprocess.run(["git", "add", "storage.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: initial sqlite db"], cwd=repo, check=True, capture_output=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    sha2 = "99999999ccccddddeeeeffff0000111122223333"

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    doc1 = f"""---
commit: {sha1}
verification_date: 2026-01-01
---
# Storage Architecture

**Decided:** 2026-01-01 · **Commit:** `{sha1[:12]}` · **Author:** Leonardo Caliva

We chose SQLite for durability.

## Files this decision touched
- `storage.py`
"""
    doc2 = f"""---
commit: {sha2}
verification_date: 2026-05-01
supersedes:
  - {sha1}
---
# Modern DuckDB Storage

**Decided:** 2026-05-01 · **Commit:** `{sha2[:12]}` · **Author:** Leonardo Caliva

We migrated to DuckDB for OLAP.
"""
    (corpus / "dec1.md").write_text(doc1, encoding="utf-8")
    (corpus / "dec2.md").write_text(doc2, encoding="utf-8")

    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")
    argv = ["--data-dir", str(tmp_path / "data dir"), "--config-dir", str(tmp_path / "config dir")]
    parser = cli._build_cli_parser()
    index_args = parser.parse_args(
        ["index", "--corpus-root", str(corpus), "--policy", str(policy), "--model", "test/minilm", *argv]
    )
    assert cli._cmd_index(index_args, embedder_factory=_fake_embedder_factory) == 0

    ask_args = parser.parse_args(
        [
            "ask",
            "why did we use sqlite",
            "--code-target",
            "storage.py:1",
            "--repo",
            str(repo),
            *argv,
        ]
    )
    exit_code = cli._cmd_ask(ask_args, embedder_factory=_fake_embedder_factory)
    assert exit_code == 0
    out = capsys.readouterr().out

    assert "dec1.md" in out
    assert "dec2.md" in out
    assert "authority: primary" in out
    assert "freshness: stale" in out
    assert "conflict: declared" in out
    assert "freshness: current" in out
    assert "conflict:" in out
    assert "governing storage.py:1 has been supersedes" in out
