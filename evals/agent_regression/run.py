#!/usr/bin/env python3
"""Runner for the agent regression benchmark.

The benchmark run is not hermetic: it calls a hosted model, costs money, and is nondeterministic,
so it never runs in CI and never starts unattended. `--dry-run` lists every planned invocation
(traps x conditions x repetitions) without constructing an adapter, which is how the cost
estimate is made before anyone approves a paid run.

Usage:
    uv run python evals/agent_regression/run.py --dry-run
    uv run python evals/agent_regression/run.py --dry-run --traps <dir> --repetitions 5
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

# Executed as a script, only this file's own directory is on `sys.path`; the package is imported
# through its parent, `evals/`, like the test suite does.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from agent_regression.runs import CONDITIONS  # noqa: E402
from agent_regression.traps import Trap, TrapError, load_traps  # noqa: E402

DEFAULT_REPETITIONS = 5
DEFAULT_TRAPS_DIR = _HERE / "traps"


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent regression benchmark runner.")
    parser.add_argument("--dry-run", action="store_true", help="list the planned invocations and exit")
    parser.add_argument("--traps", type=Path, default=DEFAULT_TRAPS_DIR, help="directory of trap directories")
    parser.add_argument("--repetitions", type=_positive_int, default=DEFAULT_REPETITIONS, help="runs per trap")
    parser.add_argument(
        "--conditions", nargs="+", choices=CONDITIONS, default=list(CONDITIONS), help="conditions to run"
    )
    parser.add_argument("--out", type=Path, default=_HERE, help="directory the reports are written to")
    return parser


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
        print(
            "error: no agent adapter is available yet; the Claude Code adapter arrives in T2 "
            "(odd/tasks/agent-regression-benchmark.md). Use --dry-run to list the planned invocations.",
            file=sys.stderr,
        )
        return 2

    print(
        f"{len(plan)} planned invocations: {len(traps)} traps x {len(conditions)} conditions x "
        f"{args.repetitions} repetitions"
    )
    for trap_id, condition, repetition in plan:
        print(f"{trap_id} {condition} repetition {repetition}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
