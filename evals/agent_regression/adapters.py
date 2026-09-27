"""The agent adapter interface, the replay adapter, and the benchmark driver.

The repetition number travels explicitly in every adapter call, so a replay adapter is a pure
lookup: no call counting, no hidden state, and the same record comes back however many times or
in whatever order it is asked for. A second client (Codex, Cursor) is a second adapter and
changes no harness code.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from agent_regression.run import plan_invocations
from agent_regression.runs import AgentRun, RunRecordError
from agent_regression.traps import Trap

InvocationKey = tuple[str, str, int]


class ReplayError(RuntimeError):
    """Raised when a replay is asked for an invocation it holds no record for, or holds a record
    filed under the wrong invocation."""


class AgentAdapter(Protocol):
    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun: ...


class ReplayAdapter:
    """Replays recorded runs keyed by `(trap_id, condition, repetition)`: the published report
    reproduces from its run records, never from a new model call."""

    def __init__(self, records: Mapping[InvocationKey, AgentRun]) -> None:
        for key, run in records.items():
            if (run.trap_id, run.condition, run.repetition) != key:
                raise ReplayError(
                    f"record filed under {key} is for trap {run.trap_id}, condition {run.condition}, "
                    f"repetition {run.repetition}"
                )
        self._records = dict(records)

    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
        try:
            return self._records[(trap.trap_id, condition, repetition)]
        except KeyError:
            raise ReplayError(
                f"no recorded run for trap {trap.trap_id}, condition {condition}, repetition {repetition}"
            ) from None


def run_benchmark(
    traps: Iterable[Trap], adapter: AgentAdapter, conditions: Sequence[str], repetitions: int
) -> list[AgentRun]:
    """Drive `adapter` through every planned invocation, in plan order, each in its own empty
    working directory, and return the runs in that order."""
    traps = tuple(traps)
    by_id = {trap.trap_id: trap for trap in traps}
    runs = []
    for trap_id, condition, repetition in plan_invocations(traps, conditions, repetitions):
        trap = by_id[trap_id]
        with tempfile.TemporaryDirectory(prefix="agent-regression-") as workdir:
            run = adapter.run(Path(workdir), trap.prompt, condition, trap, repetition)
        if (run.trap_id, run.condition, run.repetition) != (trap_id, condition, repetition):
            raise RunRecordError(
                f"adapter returned a run for trap {run.trap_id}, condition {run.condition}, repetition "
                f"{run.repetition} when asked for trap {trap_id}, condition {condition}, repetition {repetition}"
            )
        runs.append(run)
    return runs
