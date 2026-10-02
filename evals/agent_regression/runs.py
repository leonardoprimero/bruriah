"""The run record: one agent invocation on one trap under one condition and repetition.

A record carries everything the metrics need (the tool calls in order, turns, wall-clock, token
usage when the client reports it, the exit reason, the detector's verdict) plus full provenance,
because a run without provenance can be neither compared with a later run nor dismissed. The
JSON form is what a published report is reproduced from, so `run_from_json` validates every field
rather than trusting the file.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from agent_regression.detection import Detection

BASELINE = "baseline"
UNPROMPTED = "unprompted"
PROMPTED = "prompted"
# Opt-in: `prompted` plus a run-local `PreToolUse` hook that denies the first native file mutation.
GATED = "gated"
# What a runner plans when no condition is named: the published conditions, never `gated`.
DEFAULT_CONDITIONS = (BASELINE, UNPROMPTED, PROMPTED)
# Every condition a run record, a plan or a report accepts, in canonical order.
CONDITIONS = (*DEFAULT_CONDITIONS, GATED)

EXIT_REASONS = ("done", "turn_budget", "time_budget", "error")

# The tools that mutate files. "Consulted" means `investigate_work` ran before the first of these.
WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_INVESTIGATE = "investigate_work"


class RunRecordError(ValueError):
    """Raised when a run record is incomplete or names a value the benchmark does not define."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    ordinal: int


@dataclass(frozen=True)
class Provenance:
    date: str
    model_id: str
    client: str
    client_version: str
    bruriah_version: str
    trap_set_digest: str
    repetitions: int


@dataclass(frozen=True)
class AgentRun:
    trap_id: str
    condition: str
    repetition: int
    tool_calls: tuple[ToolCall, ...]
    turns: int
    wall_clock_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    exit_reason: str
    detection: Detection
    provenance: Provenance
    # What the client reported the run cost, when it reports it.
    cost_usd: float | None = None
    # The raw client transcript, as a path relative to the report directory: a committed report
    # carries no absolute paths.
    transcript: str | None = None


def _is_investigate(name: str) -> bool:
    # MCP clients prefix server tools (`mcp__<server>__investigate_work`).
    return name == _INVESTIGATE or name.endswith(f"__{_INVESTIGATE}")


def consulted_before_first_write(run: AgentRun) -> bool:
    """Whether `investigate_work` was called before the first write tool call, in call order
    (`ordinal`), not record order. A run that consults and never writes counts as consulted."""
    for call in sorted(run.tool_calls, key=lambda call: call.ordinal):
        if _is_investigate(call.name):
            return True
        if call.name in WRITE_TOOLS:
            return False
    return False


def run_to_json(run: AgentRun) -> dict[str, Any]:
    return {
        "trap_id": run.trap_id,
        "condition": run.condition,
        "repetition": run.repetition,
        "tool_calls": [{"name": call.name, "ordinal": call.ordinal} for call in run.tool_calls],
        "turns": run.turns,
        "wall_clock_seconds": run.wall_clock_seconds,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "exit_reason": run.exit_reason,
        "detection": {
            "regressed": run.detection.regressed,
            "evidence": list(run.detection.evidence),
            "completed": run.detection.completed,
        },
        "provenance": {field.name: getattr(run.provenance, field.name) for field in fields(Provenance)},
        "cost_usd": run.cost_usd,
        "transcript": run.transcript,
    }


def _require(payload: object, key: str, where: str) -> Any:
    if not isinstance(payload, dict):
        raise RunRecordError(f"{where} must be an object")
    if key not in payload:
        raise RunRecordError(f"{where} is missing {key}")
    return payload[key]


def _string(payload: object, key: str, where: str) -> str:
    value = _require(payload, key, where)
    if not isinstance(value, str):
        raise RunRecordError(f"{where}.{key} must be a string, got {value!r}")
    return value


def _integer(payload: object, key: str, where: str, *, optional: bool = False) -> Any:
    value = _require(payload, key, where)
    if value is None and optional:
        return None
    # `bool` is an `int` subclass; `true` is not a count.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RunRecordError(f"{where}.{key} must be a non-negative integer, got {value!r}")
    return value


def _boolean(payload: object, key: str, where: str) -> bool:
    value = _require(payload, key, where)
    if not isinstance(value, bool):
        raise RunRecordError(f"{where}.{key} must be a boolean, got {value!r}")
    return value


def _list(payload: object, key: str, where: str) -> list[Any]:
    value = _require(payload, key, where)
    if not isinstance(value, list):
        raise RunRecordError(f"{where}.{key} must be a list, got {value!r}")
    return value


def _cost(payload: dict[str, Any]) -> float | None:
    # Optional: records written before the Claude Code adapter carry no cost.
    value = payload.get("cost_usd")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RunRecordError(f"run.cost_usd must be a non-negative number, got {value!r}")
    return float(value)


def _transcript(payload: dict[str, Any]) -> str | None:
    # Optional, like the cost. A path that is absolute on either platform would tie the report to
    # the machine that ran it.
    value = payload.get("transcript")
    if value is None:
        return None
    if not isinstance(value, str) or PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        raise RunRecordError(f"run.transcript must be a path relative to the report directory, got {value!r}")
    return value


def run_from_json(payload: dict[str, Any]) -> AgentRun:
    where = "run"
    condition = _string(payload, "condition", where)
    if condition not in CONDITIONS:
        raise RunRecordError(f"run.condition must be one of {', '.join(CONDITIONS)}, got {condition!r}")
    exit_reason = _string(payload, "exit_reason", where)
    if exit_reason not in EXIT_REASONS:
        raise RunRecordError(f"run.exit_reason must be one of {', '.join(EXIT_REASONS)}, got {exit_reason!r}")

    wall_clock = _require(payload, "wall_clock_seconds", where)
    if isinstance(wall_clock, bool) or not isinstance(wall_clock, (int, float)) or wall_clock < 0:
        raise RunRecordError(f"run.wall_clock_seconds must be a non-negative number, got {wall_clock!r}")

    tool_calls = tuple(
        ToolCall(name=_string(call, "name", "run.tool_calls[]"), ordinal=_integer(call, "ordinal", "run.tool_calls[]"))
        for call in _list(payload, "tool_calls", where)
    )

    raw_detection = _require(payload, "detection", where)
    evidence = _list(raw_detection, "evidence", "run.detection")
    if not all(isinstance(item, str) for item in evidence):
        raise RunRecordError("run.detection.evidence must be a list of strings")
    detection = Detection(
        regressed=_boolean(raw_detection, "regressed", "run.detection"),
        evidence=tuple(evidence),
        completed=_boolean(raw_detection, "completed", "run.detection"),
    )

    raw_provenance = _require(payload, "provenance", where)
    provenance = Provenance(
        date=_string(raw_provenance, "date", "run.provenance"),
        model_id=_string(raw_provenance, "model_id", "run.provenance"),
        client=_string(raw_provenance, "client", "run.provenance"),
        client_version=_string(raw_provenance, "client_version", "run.provenance"),
        bruriah_version=_string(raw_provenance, "bruriah_version", "run.provenance"),
        trap_set_digest=_string(raw_provenance, "trap_set_digest", "run.provenance"),
        repetitions=_integer(raw_provenance, "repetitions", "run.provenance"),
    )

    return AgentRun(
        trap_id=_string(payload, "trap_id", where),
        condition=condition,
        repetition=_integer(payload, "repetition", where),
        tool_calls=tool_calls,
        turns=_integer(payload, "turns", where),
        wall_clock_seconds=float(wall_clock),
        input_tokens=_integer(payload, "input_tokens", where, optional=True),
        output_tokens=_integer(payload, "output_tokens", where, optional=True),
        exit_reason=exit_reason,
        detection=detection,
        provenance=provenance,
        cost_usd=_cost(payload),
        transcript=_transcript(payload),
    )
