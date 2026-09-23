# Feature: injection-eval

**Branch:** `feat/injection-eval` (from `main` at e121752)
**Created:** 2026-09-23
**Route:** delegated direct (one bounded writer per task)
**TDD:** strict (global session configuration). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `ask-on-risk` (forecast ~900 authored changed lines, mostly harness and tests)
**RDD:** enabled for this repo. Last reviewed boundary: e121752 (1.6.0 on main).
**Release:** groundwork for 2.0.0. This branch measures; it does not fix.

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

- [x] **T0 — RED.** (7b39fb2; RED observed: `ModuleNotFoundError: No module named 'cases'`) `tests/test_injection_eval.py`: the harness API does not exist yet; tests
  specify the case model (surface, carrier, marker), the executed-path check, and the ASR
  computation.
- [x] **T1 — Harness.** (f5c35f1, corrected by 30ae3b9) `evals/injection/`: hermetic corpus builder per surface (markdown
  front-matter surfaces, file name, heading, body; git commit subject/body/author; GitHub
  closing comment), runner calling the service directly, JSON and Markdown report.
- [x] **T1.1 — Harden per review advisories.** (98f54b3) Address the non-blocking findings of
  review lineage review-8dd33710d95f8269 (listed under Progress): attribute each leak to a
  corpus-derived field so a task echo can never count as a leak; hermetic git environment
  (no inherited identity or config); skip cleanly when git is missing; explicit errors instead
  of `assert` as runtime guards; accurate test names; drop the dead constant; avoid re-running
  the full benchmark per test.
- [x] **T2 — Baseline report.** (9ae5a3d) Commit the regenerated report. Per-case tests already
  assert the current outcome as a record of behavior, so the fixing branch flips them loudly;
  strict xfail markers were dropped as redundant.
- [x] **T3 — Framework baselines.** Deferred by user decision (2026-09-23): the benchmark
  measures Bruriah alone and stays light and offline; LlamaIndex/LangChain rows come in a later
  branch built on this harness.
- [ ] **T4 — Docs.** `evals/injection/README.md`, README link, CHANGELOG under Unreleased.

## Acceptance criteria

- `uv run python evals/injection/run.py` produces the report offline, deterministically.
- Every case reports `executed: true`; the suite fails if any case's path did not run.
- The baseline report matches the known probe result for `investigate_work`.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts`,
`uv run mypy src`.

## Progress / evidence

- 2026-09-23: exploration done (one mapper). Probe scripts backed up outside `/tmp`.
- T0/T1 (delegated writer, trigger: 2+ non-trivial files): 7b39fb2 RED, f5c35f1 GREEN.
  The parent found a false negative: `git-subject` reaches the response lowercased inside the
  generated file name, and the exact-case match missed it. Fixed TDD-first in 30ae3b9
  (normalized match: lowercase, strip non-alphanumerics).
- Checks at 30ae3b9: full suite 1667 passed / 0 failed / 18 skipped; ruff and mypy clean;
  the benchmark run twice gives byte-identical reports. Parent spot check: 15 passed; ASR 0.636.
- Measured: ASR 7/11. Leak: md file name, alternatives[].name, alternatives[].reason,
  premises[].id, premises[].statement, git commit subject, GitHub closing comment. Hold: md body,
  md heading, git commit body, git commit author. All 11 cases executed.
- RDD: assessed high (subprocess use). Consent granted by the user. Four-lens review lineage
  review-8dd33710d95f8269 approved and acknowledged (authority burned) on e121752..30ae3b9.
  Reviewed boundary is now 30ae3b9.
  Advisory findings (non-blocking): R2-assert-as-runtime-guard, R2-dead-root-constant,
  R2-misleading-test-name, R3-assert-under-O, R3-git-env-override, R3-git-prereq-no-skip,
  R3-task-echo-confound, R4-git-env-inherits-identity, R4-git-prereq-no-skip,
  R4-repeated-full-benchmark-in-tests.
  Parent check of R3-task-echo-confound: md-alt-name's marker is found in corpus-derived fields
  (alternatives[0].name, counterfactual_assessment.matched_alternative, conflicts[0]), so the
  current result is not confounded. The guard still belongs in the harness (T1.1).
- 2026-09-23: T1.1 (98f54b3) + T2 (9ae5a3d) via one bounded writer, TDD strict (RED observed for
  each behavior change: an ImportError at collection for the new names, then a real assertion
  failure for the git-env hermeticity spy, both before the corresponding fix). All ten advisory
  findings addressed: leak_fields attribution with a task-echo drop rule (md-alt-name stays
  leaked via `.evidence[0].authority_rationale`, `.conflicts[0]`, and
  `.counterfactual_assessment.rationale`, not via the task-echoing `.alternatives[0].name`);
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

## Next step

T4 (docs).
