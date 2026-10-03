"""Whether a run's final agent message cites the trap's historical decision.

Citation is decided by code, never by a model, and only the agent's final message counts: the
transcript's last `result` event, or, when the run was stopped before one, its last assistant text.
Tool inputs and tool results are never searched, so a decision the agent only read is not one it
cited.

The rule errs in both directions, so audit it by hand. A citation written in a form no cue covers
is missed and over-counts silent regressions. An identifier mentioned only as context ("the picker
moved to jiff in #8008, so I assumed NaiveDate") still matches, although the message does not say
the change departs from that decision; that hides a silent regression. Run set B's hand audit
found 5 such false positives among 15 matched runs, so every published informed override is
checked by hand and the audited rate is reported next to the code-rule rate.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from agent_regression.runs import AgentRun
from agent_regression.traps import SHA_CUE_LENGTH, Trap

_SHA_CUE = re.compile(rf"[0-9a-fA-F]{{{SHA_CUE_LENGTH},}}")
_NUMBER_CUE = re.compile(r"#([0-9]+)")
_REPOSITORY_NUMBER_CUE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[0-9]+")


def _assistant_text(event: dict) -> str | None:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else ()
    texts = [
        block["text"]
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    ]
    return "\n".join(texts) if any(text.strip() for text in texts) else None


def final_message(transcript: Path) -> str | None:
    """The agent's final message in a Claude Code stream-json transcript: the last `result`
    event's `result`, else the text blocks of the last assistant event that has any text, else
    `None`. A line that is not a JSON object is skipped."""
    result: str | None = None
    assistant: str | None = None
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "result" and isinstance(event.get("result"), str):
            result = event["result"]
        elif kind == "assistant":
            assistant = _assistant_text(event) or assistant
    return result if result is not None else assistant


def _cue_pattern(cue: str) -> str:
    if _SHA_CUE.fullmatch(cue):
        # A whole hex token that starts with the abbreviated sha.
        return rf"(?<![0-9A-Za-z_]){re.escape(cue)}[0-9a-fA-F]*(?![0-9A-Za-z_])"
    number = _NUMBER_CUE.fullmatch(cue)
    if number:
        digits = number.group(1)
        return (
            rf"(?<![\w#])#{digits}(?![0-9])"
            rf"|(?<!\w)(?i:PR|pull +request|issue)(?: +#?|#){digits}(?![0-9])"
            rf"|github\.com/[^/\s]+/[^/\s]+/(?:pull|issues)/{digits}(?![0-9])"
        )
    if _REPOSITORY_NUMBER_CUE.fullmatch(cue):
        return rf"{re.escape(cue)}(?![0-9])"
    return re.escape(cue)


def cites_decision(message: str | None, cues: Sequence[str]) -> bool:
    """Whether `message` names any of `cues`. A sha cue matches a hex token it starts
    (case-insensitive); `#N` matches the bare reference, its GitHub pull or issue URL, and a
    spelled-out `PR N`, `pull request N` or `issue N` (the word in any case, then spaces, `#` or
    both before the number; a bare or glued number such as `PR8008` does not count);
    `owner/repo#N` matches as written; any other cue, such as a document path, as a substring."""
    if not message:
        return False
    for cue in cues:
        flags = re.IGNORECASE if _SHA_CUE.fullmatch(cue) else 0
        if re.search(_cue_pattern(cue), message, flags):
            return True
    return False


def _transcript_path(run: AgentRun, report_dir: Path) -> Path | None:
    if run.transcript is None:
        return None
    root = report_dir.resolve()
    path = (report_dir / run.transcript).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def rescore_citations(runs: Iterable[AgentRun], traps_by_id: Mapping[str, Trap], report_dir: Path) -> list[AgentRun]:
    """Copies of `runs` with an unknown `cited_decision` computed from the stored transcript under
    `report_dir`. A run whose citation is known, or that has no transcript there or no trap, is
    returned unchanged. Nothing is written."""
    rescored = []
    for run in runs:
        trap = traps_by_id.get(run.trap_id)
        transcript = _transcript_path(run, report_dir)
        if run.cited_decision is None and trap is not None and transcript is not None:
            run = dataclasses.replace(run, cited_decision=cites_decision(final_message(transcript), trap.citation_cues))
        rescored.append(run)
    return rescored
