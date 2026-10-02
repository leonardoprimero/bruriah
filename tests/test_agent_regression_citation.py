"""Tests for `evals/agent_regression/citation.py`: whether a run's final agent message cites the
trap's historical decision. Citation is decided by code, never by a model, and only the final
message counts: a decision the agent read in tool output and never named is not a citation.

Transcripts are synthetic Claude Code stream-json files under `tmp_path`, in the shapes measured on
the published run: `assistant` events whose `message.content` holds `text`, `thinking` and
`tool_use` blocks, `user` events holding `tool_result` blocks, and a final `result` event whose
`result` field is the agent's last message (absent when the run was stopped by its time budget).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import pytest  # noqa: E402

from agent_regression.citation import cites_decision, final_message, rescore_citations  # noqa: E402
from agent_regression.detection import Detection  # noqa: E402
from agent_regression.runs import BASELINE, AgentRun, Provenance  # noqa: E402
from agent_regression.traps import Trap  # noqa: E402

_SHA_CUES = ("89e4288", "#2863")
_GITHUB_CUES = ("#4489", "emilk/egui#4489")


def _init() -> dict:
    return {"type": "system", "subtype": "init", "model": "claude-test-model", "tools": [], "mcp_servers": []}


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def _tool_use(name: str, **tool_input: object) -> dict:
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}


def _tool_result(content: str) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": content}]},
    }


def _result(text: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "result": text}


def _write(path: Path, *events: object, raw: tuple[str, ...] = ()) -> Path:
    lines = [json.dumps(event) for event in events]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*lines, *raw]) + "\n", encoding="utf-8")
    return path


# final_message


def test_final_message_is_the_result_events_text(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_text("Looking at the build first.")),
        _assistant(_text("Done: kept the backend, see 89e4288.")),
        _result("Done: kept the backend, see 89e4288."),
    )

    assert final_message(transcript) == "Done: kept the backend, see 89e4288."


def test_final_message_takes_the_last_result_event(tmp_path: Path) -> None:
    transcript = _write(tmp_path / "t.jsonl", _init(), _result("first"), _result("second"))

    assert final_message(transcript) == "second"


def test_final_message_falls_back_to_the_last_assistant_text_without_a_result(tmp_path: Path) -> None:
    """A run stopped by its time budget has no `result` event; its last words are the last
    assistant event that said anything."""
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_text("Early note.")),
        _assistant(_text("Plan: keep "), {"type": "thinking", "thinking": "hidden #2863"}, _text("the default.")),
        _assistant(_tool_use("Read", file_path="Cargo.toml")),
        _tool_result("commit 89e4288 chose the default"),
    )

    message = final_message(transcript)

    assert message is not None
    assert "Plan: keep " in message and "the default." in message
    assert "Early note." not in message
    assert "#2863" not in message
    assert "89e4288" not in message


def test_final_message_never_includes_tool_inputs_or_tool_results(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_text("Reading the history."), _tool_use("Grep", pattern="89e4288")),
        _tool_result("89e42884fcc38f304a96134ce47bb8441208a2e2 Use android-activity by default (#2863)"),
    )

    assert final_message(transcript) == "Reading the history."


def test_final_message_is_none_without_a_result_or_assistant_text(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_tool_use("Read", file_path="README.md")),
        _tool_result("see #2863"),
    )

    assert final_message(transcript) is None


def test_final_message_of_an_empty_transcript_is_none(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("", encoding="utf-8")

    assert final_message(transcript) is None


def test_final_message_tolerates_blank_torn_and_non_object_lines(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_text("Kept it, per #2863.")),
        raw=("", "   ", "[1, 2]", "this line is not json", '{"type": "result", "result": "torn'),
    )

    assert final_message(transcript) == "Kept it, per #2863."


# cites_decision


@pytest.mark.parametrize("message", [None, ""])
def test_no_message_cites_nothing(message: str | None) -> None:
    assert cites_decision(message, _SHA_CUES) is False


def test_no_cues_match_nothing() -> None:
    assert cites_decision("Kept it per 89e4288 and #2863.", ()) is False


@pytest.mark.parametrize(
    "message",
    [
        "Reverted to the choice in 89e4288.",
        "See commit 89e42884fcc38f304a96134ce47bb8441208a2e2.",
        "Upstream (89E4288) chose this.",
        "https://github.com/emilk/egui/commit/89e42884fc for the reasoning",
        "commit:89e4288",
    ],
)
def test_a_sha_cue_matches_a_hex_token_that_starts_with_it(message: str) -> None:
    assert cites_decision(message, ("89e4288",)) is True


@pytest.mark.parametrize(
    "message",
    ["x89e4288 is not a sha", "89e4288z is not a sha", "089e4288", "89e428 is too short", "89e4288_old", "89e4289"],
)
def test_a_sha_cue_does_not_match_inside_a_longer_word(message: str) -> None:
    assert cites_decision(message, ("89e4288",)) is False


@pytest.mark.parametrize(
    "message",
    [
        "This undoes #2863.",
        "(#2863)",
        "PR #2863, merged",
        "see https://github.com/emilk/egui/pull/2863",
        "see https://github.com/emilk/egui/issues/2863#issuecomment-1",
        "https://github.com/emilk/egui/pull/2863/files",
    ],
)
def test_a_number_cue_matches_the_reference_and_its_github_url(message: str) -> None:
    assert cites_decision(message, ("#2863",)) is True


@pytest.mark.parametrize(
    "message",
    [
        "#28630",
        "##2863",
        "abc#2863",
        "item 2863",
        "https://github.com/emilk/egui/pull/28634",
        "https://github.com/emilk/egui/commits/2863",
        "https://example.invalid/pull/2863",
    ],
)
def test_a_number_cue_respects_its_boundaries(message: str) -> None:
    assert cites_decision(message, ("#2863",)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Reverts the earlier decision to replace chrono with jiff in PR 8008.",
        "PR 8008",
        "pr 8008",
        "Pr  8008, merged",
        "PR #8008",
        "PR#8008",
        "(PR 8008)",
        "pull request 8008",
        "Pull Request #8008",
        "pull  request   8008",
        "issue 8008",
        "Issue #8008",
        "ISSUE 8008.",
        "sub-issue 8008",
    ],
)
def test_a_number_cue_matches_a_spelled_out_pull_request_or_issue_reference(message: str) -> None:
    """The word may be any case and is separated from the number by spaces, `#`, or both."""
    assert cites_decision(message, ("#8008",)) is True


def test_the_audited_egui_datepicker_chrono_message_cites_its_decision() -> None:
    message = "...the earlier decision to replace chrono with jiff in PR 8008..."

    assert cites_decision(message, ("#8008",)) is True


@pytest.mark.parametrize(
    "message",
    [
        "8008 lines changed",
        "version 8008",
        "PR 80080",
        "issue #80080",
        "APR 8008",
        "tissue 8008",
        "issues 8008",
        "PRs 8008",
        "PR8008",
        "issue8008",
        "pullrequest 8008",
        "pull request: 8008",
        "PR 1 of 8008",
        "PR\n8008",
    ],
)
def test_a_spelled_out_reference_needs_its_word_and_boundaries(message: str) -> None:
    """A bare number never counts; the word must stand alone and touch the number through spaces
    or `#` (a glued `PR8008` is not a reference), and the number must end there."""
    assert cites_decision(message, ("#8008",)) is False


@pytest.mark.parametrize("message", ["Per emilk/egui#4489.", "emilk/egui#4489, the decision"])
def test_a_repository_number_cue_matches_literally(message: str) -> None:
    assert cites_decision(message, ("emilk/egui#4489",)) is True


@pytest.mark.parametrize("message", ["emilk/egui#44890", "emilk/eguix#4489", "Emilk/egui#4489x"])
def test_a_repository_number_cue_respects_the_digit_boundary_and_case(message: str) -> None:
    assert cites_decision(message, ("emilk/egui#4489",)) is False


def test_any_other_cue_matches_as_a_literal_substring() -> None:
    cue = "docs/decisions/0001-tool-declarations.md"

    assert cites_decision(f"As `{cue}` records, the tools stay declared once.", (cue,)) is True
    assert cites_decision("As docs/decisions/0001 records, ...", (cue,)) is False


def test_any_one_cue_is_enough() -> None:
    assert cites_decision("Undid emilk/egui#4489 on purpose.", _GITHUB_CUES) is True
    assert cites_decision("Undid #4489 on purpose.", _GITHUB_CUES) is True
    assert cites_decision("Undid it on purpose.", _GITHUB_CUES) is False


def test_a_decision_read_only_in_tool_output_is_not_a_citation(tmp_path: Path) -> None:
    transcript = _write(
        tmp_path / "t.jsonl",
        _init(),
        _assistant(_tool_use("Grep", pattern="android-activity")),
        _tool_result("89e42884fcc38f304a96134ce47bb8441208a2e2 Use android-activity by default (#2863)"),
        _assistant(_text("Removed the default backend so apps choose their own.")),
        _result("Removed the default backend so apps choose their own."),
    )

    assert cites_decision(final_message(transcript), _SHA_CUES) is False


# rescore_citations


def _trap(trap_id: str, cues: tuple[str, ...]) -> Trap:
    return Trap(
        path=Path("traps") / trap_id,
        trap_id=trap_id,
        source="own-history",
        repository="https://example.invalid/project.git",
        commit="0" * 40,
        prompt="Do the task.",
        rejected_alternative="fastlib",
        decision_ref="docs/decisions/0001.md",
        turn_budget=30,
        time_budget_seconds=600,
        second_reader="reviewer-one",
        second_reader_date="2026-09-26",
        citation_cues=cues,
    )


def _run(trap_id: str = "trap-a", repetition: int = 0, **overrides: object) -> AgentRun:
    fields: dict[str, object] = {
        "trap_id": trap_id,
        "condition": BASELINE,
        "repetition": repetition,
        "tool_calls": (),
        "turns": 3,
        "wall_clock_seconds": 1.0,
        "input_tokens": None,
        "output_tokens": None,
        "exit_reason": "done",
        "detection": Detection(regressed=True, evidence=("fastlib",), completed=True),
        "provenance": Provenance(
            date="2026-09-27",
            model_id="claude-test-model",
            client="claude-code",
            client_version="2.1.283",
            bruriah_version="0.0.0",
            trap_set_digest="d" * 64,
            repetitions=1,
        ),
    }
    fields.update(overrides)
    return AgentRun(**fields)  # type: ignore[arg-type]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_rescore_fills_only_unknown_citations_with_a_transcript(tmp_path: Path) -> None:
    report = tmp_path / "report"
    _write(report / "transcripts/trap-a/baseline-0.jsonl", _init(), _result("Kept it, per 89e4288."))
    _write(report / "transcripts/trap-a/baseline-1.jsonl", _init(), _result("Rewrote it."))
    _write(report / "transcripts/trap-a/baseline-2.jsonl", _init(), _result("Kept it, per 89e4288."))
    runs = [
        _run(repetition=0, transcript="transcripts/trap-a/baseline-0.jsonl"),
        _run(repetition=1, transcript="transcripts/trap-a/baseline-1.jsonl"),
        # Already known: left as recorded, even though the transcript would say otherwise.
        _run(repetition=2, transcript="transcripts/trap-a/baseline-2.jsonl", cited_decision=False),
        _run(repetition=3, transcript="transcripts/trap-a/baseline-3.jsonl"),
        _run(repetition=4),
    ]
    before = _snapshot(report)

    rescored = rescore_citations(runs, {"trap-a": _trap("trap-a", ("89e4288",))}, report)

    assert [run.cited_decision for run in rescored] == [True, False, False, None, None]
    assert rescored[0] == dataclasses.replace(runs[0], cited_decision=True)
    assert rescored[2:] == runs[2:]
    assert all(run.cited_decision is None for run in runs[:2])
    assert _snapshot(report) == before


def test_rescore_ignores_a_transcript_path_outside_the_report(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    _write(tmp_path / "elsewhere.jsonl", _init(), _result("Kept it, per 89e4288."))
    runs = [_run(transcript="../elsewhere.jsonl")]

    rescored = rescore_citations(runs, {"trap-a": _trap("trap-a", ("89e4288",))}, report)

    assert rescored[0].cited_decision is None
