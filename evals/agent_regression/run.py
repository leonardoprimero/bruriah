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
exactly the failed and unfinished ones. A resume whose client, model, Bruriah version or Bruriah
commit differs from the runs already recorded for the trap set is refused (exit code 2) rather than
mixed into them. A real run whose Bruriah source is not one clean commit, the harness's own, does
not start (exit code 2). `--trap-ids` restricts the run to some traps; the trap set digest is then
the digest of those traps.

Usage:
    uv run python evals/agent_regression/run.py --dry-run
    uv run python evals/agent_regression/run.py --dry-run --traps <dir> --repetitions 5
    uv run python evals/agent_regression/run.py --dry-run --trap-ids <id> [<id> ...]
    uv run python evals/agent_regression/run.py --model <id> [--claude <path>] [--bruriah <path>]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
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
# What every run of one trap set in one runs log must share: a report over runs of different
# clients, models or servers would compare them as if they were one run set.
PINNED_PROVENANCE = ("client_version", "model_id", "bruriah_version", "bruriah_commit")
# How a refusal names a pinned value a recorded run does not carry (`None`).
_UNRECORDED = "unrecorded"


class ProvenanceMismatch(RuntimeError):
    """Raised when a new run would record a pinned provenance value the runs log does not hold."""


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


def select_traps(traps: Sequence[Trap], trap_ids: Iterable[str] | None) -> Sequence[Trap]:
    """The traps whose id is in `trap_ids`, or all of them when it is `None`. An id no trap has
    raises `ValueError` naming the known ids."""
    if trap_ids is None:
        return traps
    wanted = set(trap_ids)
    known = sorted(trap.trap_id for trap in traps)
    unknown = sorted(wanted.difference(known))
    if unknown:
        raise ValueError(f"unknown trap id(s): {', '.join(unknown)}; known: {', '.join(known)}")
    return [trap for trap in traps if trap.trap_id in wanted]


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
    parser.add_argument(
        "--trap-ids",
        nargs="+",
        metavar="ID",
        default=None,
        help="run only these traps of --traps; the trap set digest covers only them",
    )
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
    another trap set stay in the file, are left out, and are counted in a warning.

    A final line with no newline is what a crash mid-append leaves. When it does not parse it is
    dropped with a warning and the file is cut back to its last whole line; when it does, it is
    kept and its newline is added. Either way the next append starts on a line of its own. Any
    other malformed line is corruption and raises `ValueError`, leaving the file untouched."""
    if not path.is_file():
        return {}
    data = path.read_bytes()
    lines = data.split(b"\n")
    # Without a trailing newline the last element is the unterminated final line.
    unterminated = len(lines) if lines[-1].strip() else None
    recorded = {}
    foreign = 0
    for number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            run = run_from_json(json.loads(raw.decode("utf-8")))
        except ValueError as exc:
            if number != unterminated:
                raise ValueError(f"{path}, line {number}: {exc}") from None
            print(
                f"warning: {path}, line {number}: dropped a torn final line ({len(raw)} bytes) left by an "
                "interrupted append; that run is not recorded",
                file=sys.stderr,
            )
            with path.open("r+b") as handle:
                handle.truncate(len(data) - len(raw))
                os.fsync(handle.fileno())
            break
        key = (run.trap_id, run.condition, run.repetition)
        if run.provenance.trap_set_digest != digest:
            foreign += 1
        elif key in plan:
            recorded[key] = run
        if number == unterminated:
            with path.open("ab") as handle:
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
    if foreign:
        print(f"warning: {foreign} recorded run(s) in {path} are for another trap set; ignored", file=sys.stderr)
    return recorded


def _recorded_provenance(path: Path, digest: str) -> dict[str, set[str | None]]:
    """Every value of each pinned provenance field among the runs `path` holds for this trap set,
    whether or not they are in this plan. Read after `_recorded_runs`, which has already rejected
    or repaired every malformed line."""
    values: dict[str, set[str | None]] = {field: set() for field in PINNED_PROVENANCE}
    if not path.is_file():
        return values
    for raw in path.read_bytes().split(b"\n"):
        if not raw.strip():
            continue
        provenance = run_from_json(json.loads(raw.decode("utf-8"))).provenance
        if provenance.trap_set_digest == digest:
            for field in PINNED_PROVENANCE:
                values[field].add(getattr(provenance, field))
    return values


def _provenance_mismatch(recorded: Mapping[str, set[str | None]], current: Mapping[str, str]) -> str | None:
    """Each field of `current` whose value is not the one value every recorded run shares, with
    the recorded and current values; `None` when all match or nothing is recorded. A recorded run
    that does not carry the field (`None`) never matches."""
    differing = [
        f"{field} recorded {', '.join(sorted(_UNRECORDED if each is None else each for each in recorded[field]))}, "
        f"current {value}"
        for field, value in current.items()
        if recorded[field] and recorded[field] != {value}
    ]
    return "; ".join(differing) or None


def _run(args: argparse.Namespace, traps: Sequence[Trap], conditions: Sequence[str]) -> int:
    # Imported here, not at module level: `adapters` imports `plan_invocations` from this module,
    # and a dry run never needs the adapter.
    from agent_regression.adapters import AdapterError, run_benchmark
    from agent_regression.claude_code import ClaudeCodeAdapter, ClaudeCodeConfig
    from agent_regression.metrics import summarize, trap_set_digest
    from agent_regression.report import render_json, render_markdown, write_report

    import bruriah

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

    pinned = _recorded_provenance(runs_log, digest)

    def check_provenance(current: Mapping[str, str]) -> None:
        mismatch = _provenance_mismatch(pinned, current)
        if mismatch:
            raise ProvenanceMismatch(
                f"{runs_log} already holds runs of this trap set with another provenance ({mismatch}); "
                "refusing to mix them into one run set. Resume with the recorded client, model and Bruriah "
                "version and commit, or write to a new --out"
            )

    def record_run(run: AgentRun) -> None:
        current = {field: getattr(run.provenance, field) for field in PINNED_PROVENANCE}
        check_provenance(current)
        _append_jsonl(runs_log, run_to_json(run))
        for field, value in current.items():
            pinned[field].add(value)

    try:
        adapter = ClaudeCodeAdapter(config, trap_set_digest=digest, repetitions=args.repetitions)
        # A Bruriah source that is not one clean commit, the harness's own, refuses here rather than
        # failing every run. The client version and Bruriah version and commit are known before any
        # run, so a resume that changed them stops here and spends nothing. The model is only known
        # from a run's init line, so `record_run` checks it before that run's record is appended.
        bruriah_commit = adapter.bruriah_commit()
        if any(pinned.values()):
            check_provenance(
                {
                    "client_version": adapter.client_version(),
                    "bruriah_version": bruriah.__version__,
                    "bruriah_commit": bruriah_commit,
                }
            )
        out.mkdir(parents=True, exist_ok=True)
        new_runs = run_benchmark(
            traps,
            adapter,
            conditions,
            args.repetitions,
            skip=recorded.keys(),
            on_run=record_run,
            on_failure=record_failure,
        )
    except (AdapterError, ProvenanceMismatch) as exc:
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
        loaded = load_traps(args.traps)
        traps = select_traps(loaded, args.trap_ids)
    except (TrapError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    plan = plan_invocations(traps, conditions, args.repetitions)
    if args.trap_ids is not None:
        selected = ", ".join(sorted(trap.trap_id for trap in traps))
        print(f"restricted by --trap-ids to {len(traps)} of {len(loaded)} traps: {selected}")
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
