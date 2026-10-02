"""Metric arithmetic for the agent regression benchmark.

Per condition: Regression Rate (RR) with a Wilson 95% interval, consult rate (`investigate_work`
before the first write), heed rate (consulted runs that did not regress, separating "did not ask"
from "asked and ignored it"), RR among completed runs (avoiding the regression by doing nothing
is not a win), and cost. Between conditions: an exact paired sign test on per-trap majorities,
the same statistical treatment the fusion sweep used.

The headline is Silent Regression Rate (SRR): regressed runs whose final message does not cite
the trap's decision, over the same decided runs as RR, with the same interval and sign test. A
regressed run that cites it is an informed override. A regressed decided run whose citation is
unknown makes SRR, and its per-trap pairing, unavailable: an unknown is never assumed either way.

Error runs are counted (`runs`, `errors`) but excluded from every rate and mean: a run that
crashed says nothing about whether the agent regresses. Indeterminate runs (the detector could
not parse a target file) are counted (`indeterminate`) and excluded the same way, from the
summary rates and from the per-trap majorities; a run that is both counts as an error.

Gate activity (the `gated` condition's hook) is counted over every run that carries gate data,
error and indeterminate runs included: the hook ran whether or not the detector could decide. A run
whose hook state file exists but whose transcript shows no denial is counted as inconsistent,
never corrected.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from agent_regression.runs import CONDITIONS, AgentRun, consulted_before_first_write
from agent_regression.traps import Trap

# Two-sided 95% normal quantile.
_Z95 = 1.959963984540054


@dataclass(frozen=True)
class ConditionSummary:
    condition: str
    runs: int
    errors: int
    indeterminate: int
    silent_regressions: int
    informed_overrides: int
    unknown_citations: int
    # `None` when `unknown_citations` is positive.
    silent_regression_rate: float | None
    silent_regression_interval: tuple[float, float] | None
    regressed: int
    regression_rate: float | None
    regression_interval: tuple[float, float]
    consulted: int
    consult_rate: float | None
    heeded: int
    heed_rate: float | None
    completed: int
    completed_regressed: int
    completed_regression_rate: float | None
    mean_turns: float | None
    mean_wall_clock_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    # Gate activity: `None` when no run of the condition carries gate data.
    gate_runs: int | None
    gate_denied_runs: int | None
    gate_error_runs: int | None
    gate_inconsistent_runs: int | None


def wilson_interval(successes: int, total: int, z: float = _Z95) -> tuple[float, float]:
    """The Wilson score interval for `successes` out of `total`; (0.0, 0.0) when `total` is 0.

    The bound at an extreme is set exactly (0.0 with no successes, 1.0 with all successes)
    rather than left to floating-point cancellation.
    """
    if total < 0 or not 0 <= successes <= total:
        raise ValueError(f"successes must be within [0, total], got {successes} of {total}")
    if total == 0:
        return (0.0, 0.0)
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    half_width = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half_width)
    high = 1.0 if successes == total else min(1.0, centre + half_width)
    return (low, high)


def _two_sided_binomial(wins: int, total: int) -> float:
    # The same arithmetic as `evals/retrieval/report_separation.py::two_sided_binomial`,
    # reimplemented rather than imported: `evals/retrieval` is imported through flat module
    # names, which collide with other evals in one pytest session.
    if total == 0:
        return 1.0
    extreme = max(wins, total - wins)
    tail = sum(math.comb(total, k) for k in range(extreme, total + 1))
    return min(1.0, 2 * tail / (2**total))


def paired_sign_test(pairs: Iterable[tuple[bool, bool]]) -> tuple[int, int, float]:
    """Exact two-sided sign test over paired outcomes: (a only, b only, p). Ties are dropped."""
    a_only = 0
    b_only = 0
    for a, b in pairs:
        if a and not b:
            a_only += 1
        elif b and not a:
            b_only += 1
    return a_only, b_only, _two_sided_binomial(a_only, a_only + b_only)


def _rate(numerator: float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _token_total(values: list[int | None]) -> int | None:
    """A sum only when every run reported the value: a partial total would understate cost."""
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _gate_counts(runs: list[AgentRun]) -> tuple[int | None, int | None, int | None, int | None]:
    """(runs with gate data, with at least one denial, with at least one gate error, inconsistent)."""
    gated = [run for run in runs if run.gate_denials is not None]
    if not gated:
        return None, None, None, None
    return (
        len(gated),
        sum(1 for run in gated if run.gate_denials),
        sum(1 for run in gated if run.gate_errors),
        sum(1 for run in gated if run.gate_state_written and run.gate_denials == 0),
    )


def _summarize_condition(condition: str, runs: list[AgentRun]) -> ConditionSummary:
    non_error = [run for run in runs if run.exit_reason != "error"]
    valid = [run for run in non_error if not run.detection.indeterminate]
    regressed = [run for run in valid if run.detection.regressed]
    silent = [run for run in regressed if run.cited_decision is False]
    informed = [run for run in regressed if run.cited_decision is True]
    unknown = len(regressed) - len(silent) - len(informed)
    consulted = [run for run in valid if consulted_before_first_write(run)]
    heeded = [run for run in consulted if not run.detection.regressed]
    completed = [run for run in valid if run.detection.completed]
    completed_regressed = [run for run in completed if run.detection.regressed]
    gate_runs, gate_denied, gate_errored, gate_inconsistent = _gate_counts(runs)
    return ConditionSummary(
        condition=condition,
        runs=len(runs),
        errors=len(runs) - len(non_error),
        indeterminate=len(non_error) - len(valid),
        silent_regressions=len(silent),
        informed_overrides=len(informed),
        unknown_citations=unknown,
        silent_regression_rate=None if unknown else _rate(len(silent), len(valid)),
        silent_regression_interval=None if unknown else wilson_interval(len(silent), len(valid)),
        regressed=len(regressed),
        regression_rate=_rate(len(regressed), len(valid)),
        regression_interval=wilson_interval(len(regressed), len(valid)),
        consulted=len(consulted),
        consult_rate=_rate(len(consulted), len(valid)),
        heeded=len(heeded),
        heed_rate=_rate(len(heeded), len(consulted)),
        completed=len(completed),
        completed_regressed=len(completed_regressed),
        completed_regression_rate=_rate(len(completed_regressed), len(completed)),
        mean_turns=_rate(sum(run.turns for run in valid), len(valid)),
        mean_wall_clock_seconds=_rate(sum(run.wall_clock_seconds for run in valid), len(valid)),
        input_tokens=_token_total([run.input_tokens for run in valid]),
        output_tokens=_token_total([run.output_tokens for run in valid]),
        gate_runs=gate_runs,
        gate_denied_runs=gate_denied,
        gate_error_runs=gate_errored,
        gate_inconsistent_runs=gate_inconsistent,
    )


def summarize(runs: Iterable[AgentRun]) -> dict[str, ConditionSummary]:
    """One summary per condition present in `runs`, in the canonical condition order."""
    by_condition: dict[str, list[AgentRun]] = {}
    for run in runs:
        by_condition.setdefault(run.condition, []).append(run)
    unknown = sorted(set(by_condition) - set(CONDITIONS))
    if unknown:
        raise ValueError(f"unknown condition(s): {', '.join(unknown)}")
    return {
        condition: _summarize_condition(condition, by_condition[condition])
        for condition in CONDITIONS
        if condition in by_condition
    }


# A per-run outcome for the sign test: `None` when it cannot be decided.
Outcome = Callable[[AgentRun], bool | None]


def _regressed(run: AgentRun) -> bool:
    return run.detection.regressed


def _silently_regressed(run: AgentRun) -> bool | None:
    """Regressed without citing the decision; `None` when it regressed and the citation is unknown."""
    if not run.detection.regressed:
        return False
    return None if run.cited_decision is None else not run.cited_decision


def _majorities(runs: Iterable[AgentRun], condition: str, outcome: Outcome = _regressed) -> dict[str, bool | None]:
    """Per trap: `outcome` in strictly more than half of the non-error, decided repetitions;
    `None` when any of them has an undecided outcome."""
    counts: dict[str, list[int]] = {}
    unknown: set[str] = set()
    for run in runs:
        if run.condition != condition or run.exit_reason == "error" or run.detection.indeterminate:
            continue
        value = outcome(run)
        tally = counts.setdefault(run.trap_id, [0, 0])
        tally[0] += int(bool(value))
        tally[1] += 1
        if value is None:
            unknown.add(run.trap_id)
    return {trap_id: None if trap_id in unknown else 2 * hits > total for trap_id, (hits, total) in counts.items()}


def _pair(
    runs: Sequence[AgentRun], condition_a: str, condition_b: str, outcome: Outcome
) -> list[tuple[bool, bool]] | None:
    a = _majorities(runs, condition_a, outcome)
    b = _majorities(runs, condition_b, outcome)
    if None in a.values() or None in b.values():
        return None
    return [(bool(a[trap_id]), bool(b[trap_id])) for trap_id in sorted(a.keys() & b.keys())]


def pair_by_trap(runs: Sequence[AgentRun], condition_a: str, condition_b: str) -> list[tuple[bool, bool]]:
    """Per-trap majority outcomes paired across two conditions, sorted by trap id. A trap without
    a non-error, decided run under either condition has nothing to pair and is skipped."""
    pairs = _pair(runs, condition_a, condition_b, _regressed)
    assert pairs is not None, "a regression is always decided"
    return pairs


def pair_silent_by_trap(runs: Sequence[AgentRun], condition_a: str, condition_b: str) -> list[tuple[bool, bool]] | None:
    """`pair_by_trap` on silent-regression majorities, or `None` when a regressed decided run of
    either condition has an unknown citation: the pairing is unavailable, no trap is dropped."""
    return _pair(runs, condition_a, condition_b, _silently_regressed)


def trap_set_digest(traps: Iterable[Trap]) -> str:
    """sha256 over the canonical serialization of the sorted (trap_id, commit, prompt) triples,
    so a report states exactly which trap set it measured, independent of load order."""
    triples = sorted((trap.trap_id, trap.commit, trap.prompt) for trap in traps)
    canonical = json.dumps(triples, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
