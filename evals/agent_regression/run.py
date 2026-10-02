#!/usr/bin/env python3
"""Runner for the agent regression benchmark.

The benchmark run is not hermetic: it calls a hosted model, costs money, and is nondeterministic,
so it never runs in CI and never starts unattended. `--dry-run` lists every planned invocation
(traps x conditions x repetitions) without constructing an adapter, which is how the cost
estimate is made before anyone approves a paid run.

A real run drives the Claude Code headless adapter (`claude_code.py`) and writes `runs.json` (every
run record), `report.md`, and the raw client transcripts under `transcripts/` into `--out`. Each run
record is appended to `runs.jsonl` as soon as the run finishes, and a run the adapter could not
record is appended to `failures.jsonl` and the benchmark moves on (exit code 3). Running the same
command again skips every invocation `runs.jsonl` already holds for this trap set, so it retries
exactly the failed and unfinished ones.

Usage:
    uv run python evals/agent_regression/run.py --dry-run
    uv run python evals/agent_regression/run.py --dry-run --traps <dir> --repetitions 5
    uv run python evals/agent_regression/run.py --model <id> [--claude <path>] [--bruriah <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Collection, Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import platformdirs

# Executed as a script, only this file's own directory is on `sys.path`; the package is imported
# through its parent, `evals/`, like the test suite does.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from agent_regression.runs import CONDITIONS, DEFAULT_CONDITIONS, AgentRun, run_from_json, run_to_json  # noqa: E402
from agent_regression.traps import Trap, TrapError, load_traps  # noqa: E402

DEFAULT_REPETITIONS = 5
DEFAULT_TRAPS_DIR = _HERE / "traps"
DEFAULT_CACHE_DIR = Path(platformdirs.user_cache_dir("bruriah")) / "agent-regression"


def plan_invocations(traps: Iterable[Trap], conditions: Sequence[str], repetitions: int) -> list[tuple[str, str, int]]:
    """Every `(trap_id, condition, repetition)` to run: by trap id, then condition in the given
    order, then repetition from 0."""
    unknown = [condition for condition in conditions if condition not in CONDITIONS]
    if unknown:
        raise ValueError(f"unknown condition(s): {', '.join(unknown)}")
    if repetitions < 1:
        raise ValueError(f"repetitions must be at least 1, got {repetitions}")
    return [
        (trap.trap_id, condition, repetition)
        for trap in sorted(traps, key=lambda trap: trap.trap_id)
        for condition in conditions
        for repetition in range(repetitions)
    ]


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {text}")
    return value


def _iso_date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {text!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent regression benchmark runner.")
    parser.add_argument("--dry-run", action="store_true", help="list the planned invocations and exit")
    parser.add_argument("--traps", type=Path, default=DEFAULT_TRAPS_DIR, help="directory of trap directories")
    parser.add_argument("--repetitions", type=_positive_int, default=DEFAULT_REPETITIONS, help="runs per trap")
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=CONDITIONS,
        default=list(DEFAULT_CONDITIONS),
        help=f"conditions to run (default: {' '.join(DEFAULT_CONDITIONS)}; gated is opt-in)",
    )
    parser.add_argument("--out", type=Path, default=_HERE, help="directory the reports are written to")
    parser.add_argument("--claude", type=Path, default=None, help="the claude executable (default: found on PATH)")
    parser.add_argument("--bruriah", type=Path, default=None, help="the bruriah executable (default: found on PATH)")
    parser.add_argument("--model", default=None, help="model id; required for a real run, never defaulted")
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="repository mirrors, indexes and the model cache"
    )
    parser.add_argument(
        "--max-budget-usd-per-run", type=_positive_float, default=None, help="spend cap passed to every client run"
    )
    parser.add_argument(
        "--date", type=_iso_date, default=date.today().isoformat(), help="provenance date (default: today)"
    )
    return parser


def _executable(given: Path | None, name: str) -> Path | None:
    """The executable at `given`, or `name` on PATH, as an absolute path; `None` when neither
    resolves to an executable file. A module invocation (`python -m bruriah`) is not accepted: the
    MCP config needs one absolute command."""
    if given is None:
        found = shutil.which(name)
        return Path(found).absolute() if found else None
    return given.absolute() if given.is_file() and os.access(given, os.X_OK) else None


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append `payload` as one line and flush it to disk, so a crash leaves every earlier line whole."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _recorded_runs(
    path: Path, digest: str, plan: Collection[tuple[str, str, int]]
) -> dict[tuple[str, str, int], AgentRun]:
    """The runs `path` already holds for this trap set and plan, by invocation key. Records of
    another trap set stay in the file, are left out, and are counted in a warning."""
    if not path.is_file():
        return {}
    recorded = {}
    foreign = 0
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            run = run_from_json(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path}, line {number}: {exc}") from None
        key = (run.trap_id, run.condition, run.repetition)
        if run.provenance.trap_set_digest != digest:
            foreign += 1
        elif key in plan:
            recorded[key] = run
    if foreign:
        print(f"warning: {foreign} recorded run(s) in {path} are for another trap set; ignored", file=sys.stderr)
    return recorded


def _run(args: argparse.Namespace, traps: Sequence[Trap], conditions: Sequence[str]) -> int:
    # Imported here, not at module level: `adapters` imports `plan_invocations` from this module,
    # and a dry run never needs the adapter.
    from agent_regression.adapters import AdapterError, run_benchmark
    from agent_regression.claude_code import ClaudeCodeAdapter, ClaudeCodeConfig
    from agent_regression.metrics import summarize, trap_set_digest
    from agent_regression.report import render_json, render_markdown, write_report

    if not args.model:
        print("error: --model is required for a real run; the provenance must name the model", file=sys.stderr)
        return 2
    executables = {}
    for name, given in (("claude", args.claude), ("bruriah", args.bruriah)):
        resolved = _executable(given, name)
        if resolved is None:
            where = f"{given} is not an executable file" if given else f"no `{name}` executable on PATH"
            print(f"error: {where}; pass --{name} <path>", file=sys.stderr)
            return 2
        executables[name] = resolved

    out: Path = args.out
    config = ClaudeCodeConfig(
        claude_executable=executables["claude"],
        bruriah_executable=executables["bruriah"],
        model=args.model,
        cache_dir=args.cache_dir.absolute(),
        transcripts_dir=out.absolute() / "transcripts",
        provenance_date=args.date,
        max_budget_usd_per_run=args.max_budget_usd_per_run,
    )
    plan = plan_invocations(traps, conditions, args.repetitions)
    digest = trap_set_digest(traps)
    runs_log = out / "runs.jsonl"
    failures_log = out / "failures.jsonl"
    try:
        recorded = _recorded_runs(runs_log, digest, set(plan))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if recorded:
        print(f"resuming: {len(recorded)} recorded run(s) skipped")

    failed: list[tuple[str, str, int]] = []

    def record_failure(key: tuple[str, str, int], error: AdapterError) -> None:
        trap_id, condition, repetition = key
        failed.append(key)
        _append_jsonl(
            failures_log,
            {"trap_id": trap_id, "condition": condition, "repetition": repetition, "error": str(error)},
        )
        print(f"error: {trap_id} {condition} {repetition}: {error}", file=sys.stderr)

    try:
        adapter = ClaudeCodeAdapter(config, trap_set_digest=digest, repetitions=args.repetitions)
        out.mkdir(parents=True, exist_ok=True)
        new_runs = run_benchmark(
            traps,
            adapter,
            conditions,
            args.repetitions,
            skip=recorded.keys(),
            on_run=lambda run: _append_jsonl(runs_log, run_to_json(run)),
            on_failure=record_failure,
        )
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    by_key = recorded | {(run.trap_id, run.condition, run.repetition): run for run in new_runs}
    runs = [by_key[key] for key in plan if key in by_key]
    if failed:
        print(
            f"{len(failed)} run(s) failed, listed in {failures_log}; run the same command again to retry them",
            file=sys.stderr,
        )
    if not runs:
        print(f"no run recorded; nothing written to {out}")
        return 3 if failed else 0

    summaries = summarize(runs)
    write_report(out / "runs.json", render_json(runs, summaries))
    write_report(out / "report.md", render_markdown(runs, summaries))
    print(f"{len(runs)} runs recorded in {out / 'runs.json'}; report in {out / 'report.md'}")
    return 3 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Canonical order, duplicates dropped, whatever order the flags came in.
    conditions = tuple(condition for condition in CONDITIONS if condition in args.conditions)
    try:
        traps = load_traps(args.traps)
    except TrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    plan = plan_invocations(traps, conditions, args.repetitions)
    if not args.dry_run:
        return _run(args, traps, conditions)

    print(
        f"{len(plan)} planned invocations: {len(traps)} traps x {len(conditions)} conditions x "
        f"{args.repetitions} repetitions"
    )
    for trap_id, condition, repetition in plan:
        print(f"{trap_id} {condition} repetition {repetition}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
