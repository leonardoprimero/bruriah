# Gate Activity Evidence

## Goal
Record what the opt-in `gated` condition's pre-edit hook actually did in every run, as structured
data instead of something only a transcript reader can see: how many gated calls it denied
normally, how many it denied as a broken gate, and whether its state file agrees. Origin: advisory
`R3-gate-activity-not-recorded` (`claude_code.py:638`, review `review-361755826198b896`).

## Decisions
- Counted from the transcript, shape-agnostic: `user` events (the client's tool results; the agent
  never writes them) whose serialized JSON contains the first sentence of `GATE_REASON` count as
  denials, of `GATE_ERROR_REASON` as gate errors. Both texts are pinned in `gated_hook.py`.
- Cross-check with the hook's state file: if `gate-state.json` exists after the run but no denial
  was counted, the run's gate evidence is marked inconsistent, never silently corrected.
- Non-gated runs carry `None` for every gate field. Legacy records load as `None`.
- The report shows gate activity only for conditions that have gate data.

## Scope
- In: `claude_code.py` (count after the run, read the state file), `runs.py` (fields,
  round-trip), `metrics.py`/`report.py` (per-condition gate counts), focused tests.
- Out: paid runs; changing the hook's behaviour; the persistent-gate design.

## Acceptance Criteria
1. A gated run records `gate_denials`, `gate_errors` (ints) and `gate_state_written` (bool); non-
   gated runs record `None` for all three; records without them load as `None`.
2. Counting ignores the agent's own text: a reason quoted in an assistant message is not counted.
3. State file present with zero counted denials is reported as inconsistent.
4. The report, for a condition with gate data, shows runs with at least one denial, runs with at
   least one gate error, and inconsistent runs.
5. Focused tests and Ruff pass; no paid runs.

## Work Units
- [x] Gate activity counted per run, recorded, and reported.

## Verification Evidence
- RED: 16 eval tests failed for the intended reasons; the adapter tests failed at collection
  (`count_gate_events` missing). GREEN: the four benchmark test files pass 599; Ruff clean.
- The test stub runs the real `PreToolUse` command from the generated `--settings`, so the adapter
  tests exercise the actual hook: denial -> (1 denial, 0 errors, state written); broken gate ->
  (0, 2, not written); denial missing from the transcript -> (0, 0, written) = inconsistent; gate
  never fired -> (0, 0, not written). Reasons quoted in assistant text or the result line are not
  counted; non-gated conditions stay `None` even with the reason in tool output.
- Gate counts include error and indeterminate runs, because the hook ran either way.
- Unverified until a paid run: that the real client returns the hook's stderr inside a `user`
  tool-result event. If it does not, the state-file cross-check reports the runs as inconsistent
  rather than hiding it. A `user` event carrying several denied results counts once.

## Commit Evidence
- `2a866c9` feat(evals): record what the gated hook did in every run. Native review
  `review-14082c9b05c1b9e7` (medium, reliability) approved and acknowledged, from a clean
  worktree. Advisory follow-ups: a tool output that itself contains a pinned reason (e.g. the
  agent reading `gated_hook.py`) would count as a denial (`claude_code.py:485-489`; the hook lives
  outside the agent's clone, so this needs the agent to read a copy elsewhere); counts are events,
  not calls (`runs.py:78-83`); a partial gate triple is accepted on load (`runs.py:204-215`).
