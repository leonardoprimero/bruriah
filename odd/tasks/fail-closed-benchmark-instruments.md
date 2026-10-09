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
- [x] Checkpoint tolerates only a torn final line.
- [x] Indeterminate detection state through contract, records, metrics, report and two detectors.
- [x] Apply the indeterminate rule to the eight other detectors that fail open on a parse error
  (added 2026-10-02 after an audit of all twelve): `egui-datepicker-chrono`, `egui-image-formats`,
  `egui-wgpu-vulkan`, `egui-winit-default-features` (manifest -> `{}`); `lc-androidx-bump`
  (version catalog -> no evidence); `own-ann-index`, `own-fastmcp` (`pyproject.toml` -> `[]`);
  `own-rrf-k` (other modules -> `[]`). The deliberate line-by-line fallbacks for unparseable
  Python in `own-ann-index` and `own-fastmcp` stay: they still search. `lc-toast-removal` and
  `lc-workmanager-required` match text and parse nothing.

## Verification Evidence
- Unit 1 (gate): RED 7 new failure-path cases (missing `GATE_ERROR_REASON`; `FileNotFoundError`,
  `NotADirectoryError` and `ENOSPC` escaping `gate`; unparseable payloads exiting 0). GREEN
  `tests/test_agent_regression_claude_code.py` 119 passed; Ruff check and format clean. State is
  written to a private temporary file and hard-linked into place, so a failed write never leaves a
  partial state file and never spends the first denial. The hook is registered only for
  `Edit|Write|MultiEdit` (`claude_code.py:323`), so denying an unparseable payload cannot block
  read tools.
- Unit 2 (checkpoint): RED 4 of 6 new `_recorded_runs` cases (torn final line cut mid-record,
  before the closing brace, and mid-UTF-8 character raised; a whole final line missing its newline
  was not repaired); the two corruption cases passed before and after by design. GREEN
  `tests/test_agent_regression_eval.py` 154 passed; Ruff check clean. The log is now split on
  `\n` only, which also stops U+2028 in unescaped records from splitting a line. Formatted one
  pre-existing line from `eee486f` (`test_main_dry_run_plans_only_gated_when_asked_for_it`).
- Unit 3 (indeterminate): RED 25 new tests (contract, fixture check, records incl. legacy record
  without the field and non-bool rejection, summaries, pairing, report, both detectors on
  unparseable TOML/Python incl. invalid UTF-8 and a null byte). GREEN. Parent then changed one
  design choice: regression evidence from a file that parsed wins over another unparseable target
  (indeterminate only when no regression evidence exists); RED 4 cases, GREEN. Final: the three
  benchmark test files pass 401; Ruff check and format clean. Indeterminate runs are excluded
  from every rate and mean (consult rate, turns, tokens included), like error runs; a run that is
  both counts as an error. The report gains an `indeterminate` column, so re-rendering the
  published report changes its table shape though its records load unchanged.

- Unit 4 (eight detectors): RED 29 new cases (20 unparseable-only -> indeterminate, 9 unparseable
  plus a proven regression elsewhere -> regressed with both notes); detectors returned clean,
  regressed without the note, or crashed on invalid UTF-8 (`lc-androidx-bump`, the own-*
  `pyproject.toml`). GREEN; the four benchmark test files pass 569, including
  `check_trap_fixtures` both ways for all twelve traps; Ruff clean. `own-rrf-k` also covers an
  unparseable `ranking.py` (its `rrf_k` default is read through the AST). Parent check: in
  `egui-image-formats` and `egui-winit-default-features` an unparseable manifest is read as `{}`,
  which cannot invent evidence because every rule fires only on an entry present in a parsed
  table. The 2026-10-02 hand check of `egui-datepicker-chrono` prompted-4 found the detector
  correct: the agent added chrono as a second date library while calling it consistent with #8008.

## Commit Evidence
- `9b45fc8` fix(evals): fail the gated hook closed. Native review `review-52cb0e6fd58e86cd`
  (medium, reliability) approved and acknowledged; advisories at `gated_hook.py:67-71,87,90-94`
  and `tests/test_agent_regression_claude_code.py:977-986`.
- `db0bea5` fix(evals): resume from a torn runs log. Native review `review-3c41e035c5772ca8`
  (high, 4 lenses) approved and acknowledged. Advisory follow-up: an unterminated final line that
  is complete JSON but fails record validation is also truncated (`run.py:163-173`); the U+2028
  split claim is untested (`run.py:152`).
- `994e995` feat(evals): score an unparseable target file as indeterminate. Native review
  `review-fd961ab772e707b2` (medium, reliability) approved and acknowledged. Advisories: the
  caveat below (R3-001), `runs.py:209`, `own-lenient-schemas/detect.py:92`.
- `473eda4` fix(evals): score unparseable targets as indeterminate in the remaining detectors.
  Native review `review-c409536b990ce186` (medium, reliability) approved and acknowledged, from a
  clean worktree. Advisory: on Python before 3.12 a null byte raises `ValueError`, not
  `SyntaxError`, in `own-rrf-k/detect.py:115-118` (the project runs 3.14).

## Caveat
`trap_set_digest` hashes only `(trap_id, commit, prompt)`, not detector code. Changing a
detector does not invalidate the published run's 122 records, which keep their original verdicts;
resuming that run after unit 3 would mix detector semantics within one result set.
