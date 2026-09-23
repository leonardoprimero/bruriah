# Feature: injection-eval

**Branch:** `feat/injection-eval` (from `main` at e121752)
**Created:** 2026-09-23
**Route:** delegated direct (one bounded writer per task)
**TDD:** strict (global session configuration). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `ask-on-risk` (forecast ~900 authored changed lines, mostly harness and tests)
**RDD:** enabled for this repo. Last reviewed boundary: e121752 (1.6.0 on main).
**Release:** groundwork for 2.0.0. This branch measures; it does not fix.
**Disclosure (user decision 2026-09-23):** this branch stays local, never pushed, until the
boundary fix lands on top of it. Benchmark, fix and before/after numbers ship together in 2.0.0.

## Objective

Turn prompt-injection resistance from a claim into a measured, reproducible number:
`evals/injection/`, a hermetic benchmark whose headline metric is Attack Success Rate (ASR),
defined as "an attacker-controlled marker reached the serialized tool response", which is
inspectable without any model in the loop.

## Problem

Bruriah's positioning now rests on its retrieval boundary, but the only evidence is
`demo/injection/run.py` (three markdown files, one payload) and throwaway probe scripts that
were never committed. The probes found the boundary does not hold on every author-controlled
surface. Without a committed benchmark, neither the defect nor its eventual fix can be shown.

## Why

- A fix without a measurement cannot prove it closed anything.
- An honest, inspectable ASR table, including where Bruriah does not win, is the evidence the
  new positioning needs.

## Scope

- `evals/injection/`: attack corpus builder, runner, report writer, README.
- A pytest module that runs the benchmark hermetically and pins its invariants.
- A published baseline report of the current code.

Out of scope: fixing any boundary. That is the next branch, and it breaks a contract.

## Constraints

- Hermetic and offline: fake embedder, no reranker, no network, no model download. Follow
  `evals/counterfactual/runner.py` (constant fake vector) instead of `cli.bruriah_main`.
- Every case must first prove its carrying code path executed, then assert on content.
  A path that never ran is not a path that held.
- Public repository: the report states which surfaces are measured and their outcome, but
  does not ship a ready-to-use exploit recipe beyond what the tests themselves encode.
- CI stays green: currently-leaking cases are recorded as expected failures, strict, so a fix
  flips them loudly.

## Tasks

- [x] **T0 — RED.** (a9866f4; RED observed: `ModuleNotFoundError: No module named 'cases'`) `tests/test_injection_eval.py`: the harness API does not exist yet; tests
  specify the case model (surface, carrier, marker), the executed-path check, and the ASR
  computation.
- [x] **T1 — Harness.** (acda898, corrected by bcc2c2f) `evals/injection/`: hermetic corpus builder per surface (markdown
  front-matter surfaces, file name, heading, body; git commit subject/body/author; GitHub
  closing comment), runner calling the service directly, JSON and Markdown report.
- [x] **T1.1 — Harden per review advisories.** (b3d36d4) Address the non-blocking findings of
  review lineage review-8dd33710d95f8269 (listed under Progress): attribute each leak to a
  corpus-derived field so a task echo can never count as a leak; hermetic git environment
  (no inherited identity or config); skip cleanly when git is missing; explicit errors instead
  of `assert` as runtime guards; accurate test names; drop the dead constant; avoid re-running
  the full benchmark per test.
- [x] **T1.2 — Second review advisories.** (7fb5582, reports refreshed in 1710f8f) Address the non-blocking findings of
  review lineage review-6ba2405fbc1642c2 (listed under Progress): pass Windows-essential env vars
  through the hermetic git env and use `os.devnull` for `GIT_CONFIG_GLOBAL`; enforce
  `control_executed` with the same force as `executed`; record `echo_fields` so a path excluded
  from `leak_fields` because it also hits in the control run is observable, not silent; plus
  readability fixes (named clean-baseline constants for the git and GitHub control builds, an
  updated `control_executed` docstring, and a retraction note on the T1.1 Progress bullet whose
  claim about `.alternatives[0].name` was later superseded).
- [x] **T2 — Baseline report.** (30eb15b) Commit the regenerated report. Per-case tests already
  assert the current outcome as a record of behavior, so the fixing branch flips them loudly;
  strict xfail markers were dropped as redundant.
- [x] **T3 — Framework baselines.** Deferred by user decision (2026-09-23): the benchmark
  measures Bruriah alone and stays light and offline; LlamaIndex/LangChain rows come in a later
  branch built on this harness.
- [ ] **T4 — Docs.** `evals/injection/README.md`, README link, CHANGELOG. Deferred to the 2.0.0
  release so the docs carry the before/after numbers.

## Acceptance criteria

- `uv run python evals/injection/run.py` produces the report offline, deterministically.
- Every case reports `executed: true`; the suite fails if any case's path did not run.
- The baseline report matches the known probe result for `investigate_work`.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts`,
`uv run mypy src`.

## Progress / evidence

- 2026-09-23: exploration done (one mapper). Probe scripts backed up outside `/tmp`.
- T0/T1 (delegated writer, trigger: 2+ non-trivial files): a9866f4 RED, acda898 GREEN.
  The parent found a false negative: `git-subject` reaches the response lowercased inside the
  generated file name, and the exact-case match missed it. Fixed TDD-first in bcc2c2f
  (normalized match: lowercase, strip non-alphanumerics).
- Checks at bcc2c2f: full suite 1667 passed / 0 failed / 18 skipped; ruff and mypy clean;
  the benchmark run twice gives byte-identical reports. Parent spot check: 15 passed; ASR 0.636.
- Measured: ASR 7/11. Leak: md file name, alternatives[].name, alternatives[].reason,
  premises[].id, premises[].statement, git commit subject, GitHub closing comment. Hold: md body,
  md heading, git commit body, git commit author. All 11 cases executed.
- RDD: assessed high (subprocess use). Consent granted by the user. Four-lens review lineage
  review-8dd33710d95f8269 approved and acknowledged (authority burned) on e121752..bcc2c2f.
  Reviewed boundary is now bcc2c2f.
  Advisory findings (non-blocking): R2-assert-as-runtime-guard, R2-dead-root-constant,
  R2-misleading-test-name, R3-assert-under-O, R3-git-env-override, R3-git-prereq-no-skip,
  R3-task-echo-confound, R4-git-env-inherits-identity, R4-git-prereq-no-skip,
  R4-repeated-full-benchmark-in-tests.
  Parent check of R3-task-echo-confound: md-alt-name's marker is found in corpus-derived fields
  (alternatives[0].name, counterfactual_assessment.matched_alternative, conflicts[0]), so the
  current result is not confounded. The guard still belongs in the harness (T1.1).
- 2026-09-23: T1.1 (b3d36d4) + T2 (30eb15b) via one bounded writer, TDD strict (RED observed for
  each behavior change: an ImportError at collection for the new names, then a real assertion
  failure for the git-env hermeticity spy, both before the corresponding fix). All ten advisory
  findings addressed: leak_fields attribution with a task-echo drop rule (md-alt-name stays
  leaked via `.evidence[0].authority_rationale`, `.conflicts[0]`, and
  `.counterfactual_assessment.rationale`, not via the task-echoing `.alternatives[0].name` --
  this specific claim was found unsound and retracted by the correction bullet below:
  `.alternatives[0].name` DOES leak, it is md-alt-name's own corpus-authored surface);
  hermetic git subprocess env (HOME redirected, GIT_CONFIG_NOSYSTEM=1,
  GIT_CONFIG_GLOBAL=/dev/null, explicit author/committer identity+dates, no `os.environ`
  merge); `GitUnavailableError` + `pytest.skip`/`skipif` for a missing `git`, reported as not
  executed rather than crashing opaquely; `_executed`'s `assert` replaced with `ValueError`;
  the misleadingly-named test renamed; the dead `ROOT` constant in `run.py` removed; a
  module-scoped `benchmark_results` fixture replaces four separate full-benchmark runs per test
  (the determinism test still runs it twice, as intended). Checks: full suite 1677 passed / 0
  failed / 18 skipped (README's pinned count moved 1,685 -> 1,695 with the new tests, updated in
  the same commit); ruff and mypy clean; `evals/injection/run.py` run twice is byte-identical.
  Baseline unchanged: ASR 7/11 (0.636), same leak/hold set as before T1.1. The report now
  states `leak_fields` per case; no absolute paths or timestamps in the report.
- 2026-09-23: correction to T1.1's leak-field attribution (a4f435b, reports refreshed in
  22d3c74), caught by the coordinator: the containment-based task-echo rule was unsound -- it
  dropped `.alternatives[0].name` from md-alt-name's leak_fields, turning that case's own
  corpus-authored surface into a false "held" result, because the field's text legitimately
  overlaps the task (the task must carry the same marker for the counterfactual match to fire)
  without being an echo of it. Replaced with a control run per case (`InjectionCase.build_control`):
  identical fixture and task, only the one attacker surface reverted to a clean value;
  `find_leak_fields` now attributes a leak by JSON-path provenance (hits in the poisoned run,
  absent at that path in the control run), never by string containment against the task.
  `.alternatives[0].name` and `.counterfactual_assessment.matched_alternative` now correctly
  leak. All 11 cases' control runs executed their carrying path (`control_executed` true
  throughout, none affected). ASR unchanged at 7/11, same leak/hold set.
- 2026-09-23: T1.2 (7fb5582, reports refreshed in 1710f8f), second native review. RDD: assessed
  high risk (subprocess use). Consent granted by the user. Review lineage
  review-6ba2405fbc1642c2 approved and acknowledged (authority burned) on bcc2c2f..b86c46d;
  reviewed boundary is now b86c46d. Nine non-blocking findings: R3-windows-minimal-git-env,
  R4-hermetic-env-drops-windows-essentials, R3-control-executed-not-enforced,
  R4-control-executed-not-enforced-in-runner, R3-path-subtraction-can-mask-coincident-leak,
  R2-001, R2-002, R2-003, R2-004. TDD strict (RED observed on the intended assertion each time:
  `KeyError: 'SYSTEMROOT'`/`'USERPROFILE'` for the Windows env vars before `_hermetic_git_env`
  passed them through; `Failed: DID NOT RAISE NotExecutedError` for `control_executed` before
  `_require_all_executed` enforced it). Fixed: `_hermetic_git_env` now passes through
  SYSTEMROOT/WINDIR/COMSPEC/PATHEXT/TEMP/TMP when present, sets USERPROFILE to the redirected
  home on Windows (`os.name == "nt"`), and uses `os.devnull` instead of a hard-coded `/dev/null`
  for `GIT_CONFIG_GLOBAL`; `_require_all_executed` enforces `control_executed` with the same
  force as `executed`, so a case whose control run did not execute fails the run rather than
  reaching the report as held or leaked; `find_echo_fields`/`CaseResult.echo_fields` record every
  path `find_leak_fields` excludes because it also hits in the control run, so an excluded path
  is observable, not silent (none of the current 11 cases have one). Readability: R2-001/R2-002
  replaced literal strings duplicated across the git/GitHub poisoned and control builds with
  named clean-baseline constants (`_GIT_CLEAN_SUBJECT`/`_BODY`/`_AUTHOR`,
  `_GH_CLEAN_CLOSING_COMMENT`); R2-003's `control_executed` field comment now describes the
  enforced invariant; R2-004's contradiction (an earlier Progress bullet claiming
  `.alternatives[0].name` does not leak, superseded by the task-echo correction) got a retraction
  note in place. Checks: full suite 1688 passed / 0 failed / 18 skipped (README's pinned count
  moved 1,697 -> 1,706); ruff and mypy clean; `evals/injection/run.py` run twice is
  byte-identical. Baseline unchanged: ASR 7/11 (0.636), same leak/hold set; echo_fields empty and
  control_executed true for all 11 cases.

- RDD third review (T1.2 range b86c46d..9d4a6ba): high risk, consent granted by the user,
  lineage review-91fb63dc5012067b approved and acknowledged (authority burned). Boundary now
  9d4a6ba. Advisory findings, all on test quality, recorded as follow-ups rather than another
  round: R2-001 (tests:505), R2-002 (tests:339-345), R2-003 (tests:472), R2-004 (run.py:206-213),
  R3-echo-fields-integration-test-tautological (tests:339-345), R3-partition-test-flat-only
  (tests:459-477). Parent spot check at 9d4a6ba: 36 passed.
- Windows is covered only by the monkeypatched env tests until CI runs on windows-latest.

## Next step

Explore and design the `investigate_work` boundary fix on top of this branch (2.0.0).
Nothing is pushed before that fix lands.
