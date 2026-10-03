# Silent Regression Metric

## Goal
Implement the 2026-10-02 pre-registration amendment (`odd/tasks/agent-regression-benchmark.md`,
"silent regression is the headline"): score each decided run as silent regression, informed
override, respected, or not completed, and report Silent Regression Rate (SRR) next to RR. Then
rescore the published run's 122 records from their stored transcripts, at no model cost.

## Decisions
- Citation is decided by code: per-trap accepted cues, searched only in the agent's final message
  (the transcript's final `result` event; if absent, the last assistant text). No model.
- Cues per trap = forms derived from `decision_ref` (sha prefix of at least 7 hex chars;
  `github:owner/repo#N` gives `#N` and its pull/issue URL) plus an explicit `citation_cues` list in
  `trap.yaml` for identifiers the ref does not carry (e.g. the PR that merged a decision commit).
  Matching is strict, but not one-sided: a missed citation over-counts silent regressions, and an
  identifier mentioned only as context counts as a citation and hides one (corrected 2026-10-02
  after run set B's audit: 5 false positives in 15 matched runs).
- `AgentRun` gains `cited_decision: bool | None` (`None` = unknown, the published records).
  Rescoring computes it from the stored transcript in memory and never rewrites `runs.jsonl`.
- SRR is unavailable for a condition if any of its decided regressed runs has an unknown citation:
  fail closed, never assume.

## Scope
- In: `traps.py` and the twelve `trap.yaml`; citation extraction and matching; `AgentRun`/records;
  the adapter setting `cited_decision` for new runs; `metrics.py` and `report.py`; a rescoring path
  for existing reports; focused tests; rescoring the published run into a scratch directory and a
  hand spot-check of every informed override it finds.
- Out: paid runs; changing detectors; rewriting or committing the published run's output.

## Acceptance Criteria
1. Every trap declares or derives at least one cue; the loader rejects a malformed `citation_cues`.
2. Citation matching is deterministic, searches only the final message, and is covered by tests
   including a decision mentioned only in tool output (not a citation).
3. Records round-trip `cited_decision`; records without it load as `None`.
4. Summaries count silent regressions and informed overrides and give SRR with a Wilson interval,
   plus a paired sign test on per-trap silent-regression majorities; RR is unchanged.
5. SRR is reported unavailable when a regressed decided run has an unknown citation.
6. The published run is rescored without modifying its files; every informed override is checked
   by hand and the result is recorded here.
7. Focused tests and Ruff pass; no paid runs.

## Work Units
- [x] Citation cues per trap (loader + twelve manifests).
- [x] Citation extraction/matching and `cited_decision` in records and adapter.
- [x] SRR in metrics and report.
- [x] Rescore the published run and spot-check informed overrides.

## Verification Evidence
- Unit 1 (cues): RED 30 of 31 new loader/committed-set tests (rejection tests first passed for
  the wrong reason, unknown-key rejection, and were tightened); GREEN; the three benchmark test
  files pass 431; Ruff check and format clean. PR cues added from local mirrors only:
  `egui-android-activity` #2863, `egui-datepicker-chrono` #8008, `egui-winit-default-features`
  #1971 (each in the decision commit's own subject), `lc-androidx-bump` and
  `lc-workmanager-required` #2875 (earliest merge of 940e0f30 into main; a broad dependency PR,
  kept because every informed override is checked by hand). The four own-history decisions were
  pushed directly, so they carry only their sha cue. Unit 2 also matches pull/issue URLs.
- Unit 2 (citation): RED 12 records/adapter tests for the intended reasons; the 50 new citation
  tests failed at collection only (module missing), not one by one. GREEN: the four benchmark
  test files pass 487; Ruff check and format clean. `#N` matches a URL only as
  `github.com/<owner>/<repo>/(pull|issues)/N`. Transcript shapes checked on all 122 published
  transcripts: 118 end in a `result` event; 4 baseline runs have none and fall back to assistant
  text. Read-only rescoring (no file changed, mtimes and sizes identical): 36 of 122 runs cite
  their decision; among the 43 regressions only 2 cite it (baseline 0/13, unprompted 1/15,
  prompted 1/15, both `egui-datepicker-chrono`). The published run predates `fa26c59`.
- Unit 3 (SRR): RED 20 tests for the intended reasons after a stub (first run failed at
  collection); GREEN; the four benchmark test files pass 511; Ruff clean. SRR and its interval
  are the first metric columns, RR next; the sign-test table has an SRR row above each RR row and
  fails closed for a whole condition pair on any unknown citation. New read-only entry point
  `python evals/agent_regression/report.py <run-dir> [--rescore-citations] [--traps DIR]
  [--out DIR]`; it refuses an `--out` inside the run directory, and tests prove the run directory
  is byte-identical after rendering.
- Unit 4 (rescore + audit): rendered `out-2026-09-27` with `--rescore-citations` into a scratch
  directory under /tmp; a stat hash of every published file was identical before and after. A
  read-only explorer classified all 43 regressed runs' final messages (A = names the decision by
  identifier, B = acknowledges a deliberate earlier choice in prose only, C = neither); the parent
  re-read the two decisive transcripts. Result: A 3, B 1, C 39. Every code-cited run is a true A.
  One A was missed: `egui-datepicker-chrono` prompted-4 wrote "PR 8008" without `#`; fixed in
  `a2a8fc8` (`#N` cues also match `PR N`, `pull request N`, `issue N`; bare numbers still never
  match; RED 12, GREEN; 540 tests pass). The one B (`egui-android-activity` baseline-2: "eframe
  deliberately left that choice to applications") stays a silent regression under the strict
  rule and is the known under-count: 1 of 40. The explorer's per-condition summary counts did not
  match its own table; the table was used.
- Published run, rescored (decided runs; SRR then RR):

  | condition | SRR | RR | informed overrides |
  |---|---|---|---|
  | baseline | 0.32 [0.20, 0.47] | 0.32 | 0 |
  | unprompted | 0.35 [0.22, 0.50] | 0.38 | 1 |
  | prompted | 0.33 [0.20, 0.48] | 0.38 | 2 |

  Paired sign tests: p = 1.0 for SRR and RR in every pair. Reading: Bruriah 2.1.0 did not reduce
  silent regressions on this run; it predates `fa26c59`, whose decision signal made all six later
  smoke runs cite #2863. One informed override (`egui-datepicker-chrono` prompted-4) claims to act
  "in line with" the decision while the detector says it reintroduced chrono: worth a detector
  spot-check before the next published run.

## Commit Evidence
- `fa71956` citation cues; review `review-74bae41d167a2d9a` approved.
- `1415c43` citation extraction and `cited_decision`; review `review-5e92e4635c1d0cfc` approved.
- `6c58cde` SRR in metrics and report; review `review-b64c0415be2701a5` approved.
- `a2a8fc8` spelled-out PR/issue citations; review `review-d158c7ce462d7b11` approved.
- All reviews ran committed-only from a clean detached worktree at the commit.
