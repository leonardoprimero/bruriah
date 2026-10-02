"""JSON and Markdown reports for the agent regression benchmark.

The JSON report holds every run record, so the Markdown reproduces from it without a new model
call. Both renderings are deterministic (sorted keys, fixed ordering, fixed number formats) and
carry no absolute paths and no timestamps other than the provenance date, so a committed report
reproduces byte-identically from its run records.

Run as a script, it renders the report of a stored run from its `runs.jsonl`, read-only:
`--rescore-citations` scores the citations a record predating citation scoring left unknown from
its stored transcript, in memory. Without `--out` the Markdown goes to stdout; with it, `runs.json`
and `report.md` go there, never into the run directory itself.

Usage:
    uv run python evals/agent_regression/report.py <run-dir> [--rescore-citations] [--traps <dir>] [--out <dir>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import fields
from itertools import combinations
from pathlib import Path
from typing import Any

# Executed as a script, only this file's own directory is on `sys.path`; the package is imported
# through its parent, `evals/`, like the test suite does.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from agent_regression.citation import rescore_citations  # noqa: E402
from agent_regression.metrics import (  # noqa: E402
    ConditionSummary,
    pair_by_trap,
    pair_silent_by_trap,
    paired_sign_test,
    summarize,
)
from agent_regression.runs import CONDITIONS, AgentRun, Provenance, run_from_json, run_to_json  # noqa: E402
from agent_regression.traps import TrapError, load_traps  # noqa: E402

DEFAULT_TRAPS_DIR = _HERE / "traps"


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
    interval = summary.silent_regression_interval
    payload["silent_regression_interval"] = None if interval is None else list(interval)
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


def _unavailable(unknown_citations: int) -> str:
    runs = "run" if unknown_citations == 1 else "runs"
    return f"unavailable ({unknown_citations} regressed {runs} with unknown citation)"


def _srr_cells(summary: ConditionSummary) -> str:
    if summary.silent_regression_rate is None or summary.silent_regression_interval is None:
        return f"{_unavailable(summary.unknown_citations)} | n/a"
    low, high = summary.silent_regression_interval
    return f"{_rate(summary.silent_regression_rate)} | [{low:.2f}, {high:.2f}]"


def _ordered_conditions(summaries: Mapping[str, ConditionSummary]) -> list[str]:
    return [condition for condition in CONDITIONS if condition in summaries]


def render_markdown(runs: Sequence[AgentRun], summaries: Mapping[str, ConditionSummary]) -> str:
    conditions = _ordered_conditions(summaries)
    lines = [
        "# Agent regression benchmark",
        "",
        "Regression Rate (RR): the fraction of runs whose resulting change reintroduces the trap's rejected "
        "alternative, decided by the trap's deterministic detector, never by a model. Error runs and "
        "indeterminate runs (the detector could not parse a target file) are counted but excluded from every rate "
        "and mean. Intervals are Wilson 95%.",
        "",
        "Silent Regression Rate (SRR), the headline: over the same runs, the fraction that regressed and whose "
        "final message does not cite the trap's decision (a silent regression). A regressed run whose final message "
        "cites it is an informed override, one the reviewer was told about. Citation is decided by code, on the "
        "agent's final message only, against the trap's accepted citation cues; tool output never counts. SRR is "
        "unavailable for a condition while any of its regressed runs has an unknown citation.",
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
        "| condition | runs | errors | indeterminate | SRR | SRR interval | RR | RR interval | informed overrides "
        "| unknown citations | consult rate | heed rate | completed | RR among completed | mean turns "
        "| mean wall-clock (s) | input tokens | output tokens |",
        "|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for condition in conditions:
        summary = summaries[condition]
        low, high = summary.regression_interval
        lines.append(
            f"| {condition} | {summary.runs} | {summary.errors} | {summary.indeterminate} | {_srr_cells(summary)} "
            f"| {_rate(summary.regression_rate)} | [{low:.2f}, {high:.2f}] "
            f"| {summary.informed_overrides} | {summary.unknown_citations} "
            f"| {_rate(summary.consult_rate)} | {_rate(summary.heed_rate)} "
            f"| {summary.completed} | {_rate(summary.completed_regression_rate)} | {_mean(summary.mean_turns)} "
            f"| {_mean(summary.mean_wall_clock_seconds)} | {_count(summary.input_tokens)} "
            f"| {_count(summary.output_tokens)} |"
        )

    gated = [condition for condition in conditions if summaries[condition].gate_runs is not None]
    if gated:
        lines += [
            "",
            "## Gate activity",
            "",
            "Denials and gate errors are counted from each run's transcript as `user` events (tool results the "
            "client returns, never the agent's own assistant text) containing the first sentence of the gate's "
            "pinned denial or broken-gate reason. A run whose gate state file exists but whose transcript shows "
            "no denial is inconsistent; every run with gate data counts, error and indeterminate runs included.",
            "",
            "| condition | runs with gate data | runs with a denial | runs with a gate error | inconsistent runs |",
            "|---|---:|---:|---:|---:|",
        ]
        for condition in gated:
            summary = summaries[condition]
            lines.append(
                f"| {condition} | {summary.gate_runs} | {summary.gate_denied_runs} | {summary.gate_error_runs} "
                f"| {summary.gate_inconsistent_runs} |"
            )

    lines += [
        "",
        "## Paired sign tests",
        "",
        "Per-trap majority outcome (silently regressed for SRR, regressed for RR, in strictly more than half of "
        "the non-error, non-indeterminate repetitions), paired across two conditions; ties are dropped and p is "
        "the exact two-sided binomial. The SRR test is unavailable while either condition has a regressed run "
        "with an unknown citation.",
        "",
        "| comparison | metric | paired traps | first only | second only | p |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for first, second in combinations(conditions, 2):
        silent_pairs = pair_silent_by_trap(runs, first, second)
        if silent_pairs is None:
            unknown = summaries[first].unknown_citations + summaries[second].unknown_citations
            lines.append(f"| {first} vs {second} | SRR | n/a | n/a | n/a | {_unavailable(unknown)} |")
        else:
            first_only, second_only, p = paired_sign_test(silent_pairs)
            lines.append(
                f"| {first} vs {second} | SRR | {len(silent_pairs)} | {first_only} | {second_only} | {p:.4f} |"
            )
        pairs = pair_by_trap(runs, first, second)
        first_only, second_only, p = paired_sign_test(pairs)
        lines.append(f"| {first} vs {second} | RR | {len(pairs)} | {first_only} | {second_only} | {p:.4f} |")

    lines += [
        "",
        "## Per trap",
        "",
        "Regressed runs over non-error, non-indeterminate runs, per condition.",
        "",
        "| trap | " + " | ".join(conditions) + " |",
        "|---|" + "---:|" * len(conditions),
    ]
    tallies: dict[tuple[str, str], list[int]] = {}
    for run in runs:
        if run.exit_reason == "error" or run.detection.indeterminate:
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


def load_runs(path: Path) -> list[AgentRun]:
    """The run records of a stored `runs.jsonl`, in file order, read-only. Any line that is not a
    whole record, a torn final line included, a repeated invocation, or records of more than one
    trap set raise `ValueError`: a resumed run repairs its log, a rendering never does."""
    runs: list[AgentRun] = []
    seen: set[tuple[str, str, int]] = set()
    for number, raw in enumerate(path.read_bytes().split(b"\n"), start=1):
        if not raw.strip():
            continue
        try:
            run = run_from_json(json.loads(raw.decode("utf-8")))
        except ValueError as exc:
            raise ValueError(f"{path}, line {number}: {exc}") from None
        key = (run.trap_id, run.condition, run.repetition)
        if key in seen:
            raise ValueError(f"{path}, line {number}: {' '.join(map(str, key))} is recorded twice")
        seen.add(key)
        runs.append(run)
    digests = {run.provenance.trap_set_digest for run in runs}
    if len(digests) > 1:
        raise ValueError(f"{path}: records of {len(digests)} trap sets; a report measures one")
    return runs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the agent regression report of a stored run.")
    parser.add_argument("run_dir", type=Path, help="the run's output directory, holding runs.jsonl and transcripts/")
    parser.add_argument(
        "--rescore-citations",
        action="store_true",
        help="score every unknown citation from its stored transcript, in memory; runs.jsonl is never rewritten",
    )
    parser.add_argument("--traps", type=Path, default=DEFAULT_TRAPS_DIR, help="trap set the citation cues come from")
    parser.add_argument(
        "--out", type=Path, default=None, help="write runs.json and report.md here (default: Markdown to stdout)"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_dir: Path = args.run_dir
    out: Path | None = args.out
    if out is not None and out.resolve().is_relative_to(run_dir.resolve()):
        print(f"error: --out {out} is inside the run directory; rendering never writes into the run", file=sys.stderr)
        return 2
    try:
        runs = load_runs(run_dir / "runs.jsonl")
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.rescore_citations:
        try:
            traps = load_traps(args.traps)
        except TrapError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        runs = rescore_citations(runs, {trap.trap_id: trap for trap in traps}, run_dir)

    summaries = summarize(runs)
    markdown = render_markdown(runs, summaries)
    if out is None:
        sys.stdout.write(markdown)
        return 0
    out.mkdir(parents=True, exist_ok=True)
    write_report(out / "runs.json", render_json(runs, summaries))
    write_report(out / "report.md", markdown)
    print(f"{len(runs)} runs rendered into {out / 'runs.json'} and {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
