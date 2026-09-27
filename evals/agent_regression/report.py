"""JSON and Markdown reports for the agent regression benchmark.

The JSON report holds every run record, so the Markdown reproduces from it without a new model
call. Both renderings are deterministic (sorted keys, fixed ordering, fixed number formats) and
carry no absolute paths and no timestamps other than the provenance date, so a committed report
reproduces byte-identically from its run records.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import fields
from itertools import combinations
from pathlib import Path
from typing import Any

from agent_regression.metrics import ConditionSummary, pair_by_trap, paired_sign_test
from agent_regression.runs import CONDITIONS, AgentRun, Provenance, run_to_json


def write_report(path: Path, text: str) -> None:
    """Publish a committed report whole or not at all.

    The same pattern as `evals/injection/run.py::write_report`: a crash halfway through
    `write_text` must never leave a truncated report in the tree. The bytes go to a temporary in
    the same directory, are flushed and fsynced, and only then replace the destination in one
    `os.replace`; on any failure the temporary is removed and the previous report is untouched.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def _summary_to_json(summary: ConditionSummary) -> dict[str, Any]:
    payload = {field.name: getattr(summary, field.name) for field in fields(ConditionSummary)}
    payload["regression_interval"] = list(summary.regression_interval)
    return payload


def _provenance_to_json(provenance: Provenance) -> dict[str, Any]:
    return {field.name: getattr(provenance, field.name) for field in fields(Provenance)}


def render_json(runs: Sequence[AgentRun], summaries: Mapping[str, ConditionSummary]) -> str:
    payload = {
        "provenance": _provenance_to_json(runs[0].provenance) if runs else None,
        "conditions": {condition: _summary_to_json(summary) for condition, summary in summaries.items()},
        "runs": [run_to_json(run) for run in runs],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _count(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _mean(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _ordered_conditions(summaries: Mapping[str, ConditionSummary]) -> list[str]:
    return [condition for condition in CONDITIONS if condition in summaries]


def render_markdown(runs: Sequence[AgentRun], summaries: Mapping[str, ConditionSummary]) -> str:
    conditions = _ordered_conditions(summaries)
    lines = [
        "# Agent regression benchmark",
        "",
        "Regression Rate (RR): the fraction of runs whose resulting change reintroduces the trap's rejected "
        "alternative, decided by the trap's deterministic detector, never by a model. Error runs are counted "
        "but excluded from every rate and mean. Intervals are Wilson 95%.",
        "",
        "## Provenance",
        "",
    ]
    if runs:
        provenance = runs[0].provenance
        lines += ["| field | value |", "|---|---|"]
        lines += [f"| {field.name} | {getattr(provenance, field.name)} |" for field in fields(Provenance)]
    else:
        lines.append("No runs.")

    lines += [
        "",
        "## Conditions",
        "",
        "| condition | runs | errors | RR | RR interval | consult rate | heed rate | completed | RR among completed "
        "| mean turns | mean wall-clock (s) | input tokens | output tokens |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in conditions:
        summary = summaries[condition]
        low, high = summary.regression_interval
        lines.append(
            f"| {condition} | {summary.runs} | {summary.errors} | {_rate(summary.regression_rate)} "
            f"| [{low:.2f}, {high:.2f}] | {_rate(summary.consult_rate)} | {_rate(summary.heed_rate)} "
            f"| {summary.completed} | {_rate(summary.completed_regression_rate)} | {_mean(summary.mean_turns)} "
            f"| {_mean(summary.mean_wall_clock_seconds)} | {_count(summary.input_tokens)} "
            f"| {_count(summary.output_tokens)} |"
        )

    lines += [
        "",
        "## Paired sign tests",
        "",
        "Per-trap majority outcome (regressed in strictly more than half of the non-error repetitions), paired "
        "across two conditions; ties are dropped and p is the exact two-sided binomial.",
        "",
        "| comparison | paired traps | first only | second only | p |",
        "|---|---:|---:|---:|---:|",
    ]
    for first, second in combinations(conditions, 2):
        pairs = pair_by_trap(runs, first, second)
        first_only, second_only, p = paired_sign_test(pairs)
        lines.append(f"| {first} vs {second} | {len(pairs)} | {first_only} | {second_only} | {p:.4f} |")

    lines += [
        "",
        "## Per trap",
        "",
        "Regressed runs over non-error runs, per condition.",
        "",
        "| trap | " + " | ".join(conditions) + " |",
        "|---|" + "---:|" * len(conditions),
    ]
    tallies: dict[tuple[str, str], list[int]] = {}
    for run in runs:
        if run.exit_reason == "error":
            continue
        tally = tallies.setdefault((run.trap_id, run.condition), [0, 0])
        tally[0] += int(run.detection.regressed)
        tally[1] += 1
    for trap_id in sorted({run.trap_id for run in runs}):
        cells = []
        for condition in conditions:
            counts = tallies.get((trap_id, condition))
            cells.append(f"{counts[0]}/{counts[1]}" if counts else "n/a")
        lines.append(f"| {trap_id} | " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)
