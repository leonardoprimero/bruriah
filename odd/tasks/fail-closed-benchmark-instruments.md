# Fail-Closed Benchmark Instruments

## Goal
Make the agent-regression benchmark's instruments fail closed or report uncertainty, so a broken
gate, a torn checkpoint, or an unparseable target file can never be scored as an ordinary outcome.
Prerequisite for measuring any future persistent pre-edit gate.

## Origin
Advisory findings from the 2026-10-02 native reviews (`review-361755826198b896`,
`review-a17764baac1075f3`, `review-906ee8382bc7097c`, `review-ba2b79d55e2574e4`).

## Decisions
- Gate errors fail closed: deny with a distinct gate-error reason instead of letting the edit pass.
- Checkpoint: drop only a torn final line (no trailing newline) with a warning, truncating the file
  to its last whole line; corruption anywhere else still raises.
- Detection gains an explicit indeterminate state (user decision, 2026-10-02): a run whose target
  file cannot be parsed is recorded with evidence and excluded from regression and heed rates, like
  error runs. Unparseable is neither clean nor regressed.

## Scope
- In: `gated_hook.py`; `run.py` checkpoint loading; `detection.py`, `metrics.py`, `runs.py`,
  `report.py`; detectors `egui-android-activity` and `own-lenient-schemas`; focused tests.
- Out: structured recording of gate activity (`claude_code.py:638`); auditing the other ten
  detectors for the same pattern; missing-file semantics; the persistent gate design; paid runs.

## Acceptance Criteria
1. A gated hook that cannot create or write its state file, or receives an unparseable payload,
   exits 2 with a gate-error reason distinct from `GATE_REASON`.
2. Resuming from a runs log whose final line is torn drops only that line, warns, and leaves the
   file ending in a whole line; a malformed line elsewhere still raises.
3. `Detection` has `indeterminate`; it is mutually exclusive with `regressed` and requires evidence.
4. Records without the field (the published run) load as `indeterminate=False`.
5. Indeterminate runs are excluded from regression, heed and majority denominators and are counted
   per condition in the summary and report.
6. Both named detectors return indeterminate, with evidence, for an unparseable target file; their
   fixtures still pass in both directions.
7. Focused tests and Ruff pass; no paid runs.

## Work Units
- [x] Gate fails closed on state or payload errors.
- [ ] Checkpoint tolerates only a torn final line.
- [ ] Indeterminate detection state through contract, records, metrics, report and two detectors.

## Verification Evidence
- Unit 1 (gate): RED 7 new failure-path cases (missing `GATE_ERROR_REASON`; `FileNotFoundError`,
  `NotADirectoryError` and `ENOSPC` escaping `gate`; unparseable payloads exiting 0). GREEN
  `tests/test_agent_regression_claude_code.py` 119 passed; Ruff check and format clean. State is
  written to a private temporary file and hard-linked into place, so a failed write never leaves a
  partial state file and never spends the first denial. The hook is registered only for
  `Edit|Write|MultiEdit` (`claude_code.py:323`), so denying an unparseable payload cannot block
  read tools.

## Commit Evidence
_Pending._
