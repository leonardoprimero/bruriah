# Feature: agent-regression-benchmark

**Branch:** `eval/agent-regression-benchmark` (from `main` at 1d36bc7)
**Created:** 2026-09-27
**Route:** delegated direct (one bounded writer per task)
**TDD:** strict. Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** chained PRs, each under ~400 authored changed lines (harness, trap set,
report tooling, docs are natural cut points)
**RDD:** enabled for this repo. Last reviewed boundary: 1d36bc7 (2.1.0 on main).
**Release:** groundwork for 2.2.0. This branch measures; it does not change the product.
**Disclosure:** public from the first PR. The number ships whatever it is, including a null
result. A benchmark that only publishes when it flatters the product is marketing.

## Objective

Turn the central claim of this project -- *a coding agent with Bruriah reintroduces rejected
architectures less often than the same agent without it* -- from a case study into a measured,
reproducible number: `evals/agent_regression/`, a benchmark whose headline metric is
**Regression Rate (RR)**, defined as "the agent's resulting change reintroduces the rejected
alternative", decided by a deterministic detector over the resulting tree, never by a model.

## Problem

Every published measurement on this repository is structural or retrieval-side:
`evals/injection/` measures what `investigate_work` serializes, `evals/project-memory/` measures
where the right document ranks, `evals/counterfactual/` measures whether the engine raises the
right verdict for a hand-written scenario. None of them puts an agent in the loop. The README's
positioning ("Coding agents frequently ... reintroduce architectures that your team explicitly
rejected") and `docs/case-study.md` describe the failure mode; nothing measures how often it
happens, or whether Bruriah changes it. That is the first question anyone evaluating this project
asks, and today the honest answer is "not measured".

## Why

- A retrieval that ranks the right decision third is worthless if the agent never asks, or asks
  and ignores it. Only an end-to-end run can tell those apart.
- The result is the evidence the positioning needs, in both directions: if the unprompted
  condition shows no effect, the product needs a client-side change (a hook, a `CLAUDE.md`
  line, a pre-edit trigger) more than it needs another retrieval knob.
- The same harness later measures every client integration (Cursor, Codex, hooks) against the
  same traps, so a "supported" client means "measured on the traps", not "has a config snippet".

## Design

### The unit: a regression trap

A trap is one directory under `evals/agent_regression/traps/<id>/` holding:

- `trap.yaml`: the pinned repository and commit, the task prompt, the rejected alternative's
  name, the reference decision document (path or commit) that rejected it, and a turn/time
  budget.
- `detect.py`: a pure function `detect(tree: Path, diff: str) -> Detection` returning whether the
  rejected alternative was reintroduced and *which evidence* fired (an import, a dependency line,
  a file, a symbol). Deterministic, no model, no network. Each detector ships with two fixture
  tests: it fires on a synthetic regression and stays silent on a clean, on-task edit.

The task prompt is written to *tempt* the rejected alternative without naming the rejection or
the decision, the way a real ticket would ("simplify the MCP server's tool definitions", not
"do not use FastMCP"). A prompt that names the alternative measures instruction following, not
memory. A reviewer other than the author reads every prompt before it is committed and confirms it
does not leak the answer; the reviewer's identity and date go in `trap.yaml`.

### Trap sources, in order

1. **This repository's own history.** The rejections in `evals/counterfactual/scenarios.jsonl`
   are real decisions with real commits (FastMCP, watchdog, sqlite-vec as an ANN index, and
   whatever else the `Alternative-Rejected` trailers on `main` carry). Corpus: `bruriah corpus`
   on the pinned commit, as the README quickstart does.
2. **`square/leakcanary` and `emilk/egui`** at the shas pinned in `evals/project-memory`, using
   the rejected alternatives `bruriah corpus --github` recovered (115 across both, per
   `report_counterfactuals.py`, measured 2026-09-21). Corpus from the warm `--github-cache` so
   the build is offline and reproducible.

Target: 12 to 15 traps for the first published run, at least 4 per source. Fewer, well-detected
traps beat more traps with a detector that guesses.

### Conditions

| condition | MCP server registered | client instructed to investigate | what a difference measures |
|---|---|---|---|
| `baseline` | no | no | how often the agent regresses on its own |
| `unprompted` | yes | no | whether the tool is used and heeded without being told to |
| `prompted` | yes | one `CLAUDE.md` line: investigate the task before editing | the ceiling: what the tool is worth when used |

`unprompted` vs `prompted` is the question that decides where the next product work goes, so both
run from the first publication. A fourth condition with `bruriah brief` output pasted into the
prompt (no MCP round trip) is the obvious follow-up and is out of scope here.

### Agent adapters

One adapter interface, `AgentAdapter.run(workdir, prompt, condition, trap, repetition) -> AgentRun`,
where the repetition number travels explicitly so a replay adapter is a pure lookup, and where
`AgentRun` records the transcript, the tool calls in order, turn count, wall-clock, token usage
when the client reports it, and the exit reason (done, turn budget, time budget, error).

First adapter: **Claude Code headless** (`claude -p`, JSON output, tool allowlist restricted to
read/edit/write/grep/glob and a `bash` allowlist that excludes `git push`, package installs
and network). Model id, `claude --version`, and the Bruriah version go into every run record.
Codex is installed locally and gets the second adapter in a later branch; the interface is
designed so a second adapter changes no harness code.

### Metrics

- **RR (headline):** per condition, traps regressed / traps run, over N repetitions per trap
  (N = 5 for the first publication; sampling at temperature is the source of variance and one
  run per trap is an anecdote). Reported per trap, per source, and pooled, with a Wilson 95%
  interval and a paired sign test between conditions, the same statistical treatment the fusion
  sweep used (`evals/project-memory/README.md`, 2026-09-24).
- **Consult rate:** runs where `investigate_work` was called *before the first write tool call*.
- **Heed rate:** among consulted runs, runs that did not regress. Separates "did not ask" from
  "asked and ignored it".
- **Cost:** turns, wall-clock, tokens where reported, per condition. The trade is stated next
  to the benefit, as the injection README states the two-call cost.
- **Task completion:** a run that avoids the regression by doing nothing is not a win. Each trap
  declares a minimal completion check (a file exists, a test passes, a symbol is present),
  deterministic like the detector, and the report shows RR *among completed runs* alongside RR
  over all runs.

### What is hermetic and what is not

The harness's own tests are hermetic: a fake adapter replays recorded transcripts and synthetic
trees, so `pytest` covers the trap loader, detector contract, run record schema, metric
arithmetic, and report rendering without a model, a network, or a paid API. CI runs exactly that.

The benchmark run itself is not hermetic and never runs in CI: it calls a hosted model, costs
money, and is nondeterministic. It runs by hand, and the committed report carries its provenance
(date, model id, client version, Bruriah version, trap set digest, N) so a future run can be
compared or dismissed. Running it requires the user's explicit go-ahead on the cost estimate
first (T4), never an unattended kick-off.

### Threats to validity, stated in the report

- **Pretraining contamination.** `leakcanary` and `egui` are public and the model may know their
  history. This repository's history is public too. The report states this and does not claim the
  result generalizes to a private codebase; a trap set on a private corpus is the falsifier
  someone else can run, as CONTRIBUTING.md already asks for retrieval.
- **Prompt design leaks.** Mitigated by the second-reader rule above, not eliminated.
- **One client, one model.** The first publication is one adapter and one pinned model. The
  report says so in its first paragraph.
- **Detector precision.** A detector that fires on a legitimate non-regressing edit inflates RR.
  Every fired detection in the published run is spot-checked by hand and the spot-check count is
  in the report.

## Scope

- `evals/agent_regression/`: trap schema and loader, detector contract, adapter interface, Claude
  Code adapter, runner, metric computation, JSON and Markdown report writers, README.
- `tests/test_agent_regression_eval.py`: hermetic tests via a fake adapter.
- 12 to 15 committed traps with detectors and fixture tests.
- One published run, with provenance, in the README and linked from the top-level README.

Out of scope: any product change (no new tool, no prompt change in `bruriah setup`, no hook).
If the result argues for one, that is the next ODD, with this benchmark as its before/after.
Also out of scope: a Codex or Cursor adapter, an LLM-judge column, a private-corpus trap set.

## Constraints

- The headline number is decided by code, never by a model. An LLM-judge column, if ever added,
  is secondary and labeled as such.
- No trap prompt names the rejected alternative, the decision, or Bruriah.
- The agent runs in a fresh temporary clone at the pinned sha, with a tool allowlist that cannot
  push, install packages, or reach the network beyond the model API. Verified by a test that
  inspects the allowlist the adapter passes, and by a post-run check that the clone's remotes
  and lockfiles are untouched.
- The MCP surface stays two read-only tools. The benchmark drives them through the real
  `bruriah serve` process, not through the service in-process, because the client's behavior
  with the real transport is the thing being measured.
- Committed reports are written atomically (`fix/atomic-report-write`, PR #31 precedent) and
  contain no absolute paths or timestamps other than the provenance date.
- No number goes into a README or commit body without a pasted execution.

## Tasks

- [x] **T0 -- RED.** (25237d6) `tests/test_agent_regression_eval.py`: the harness API does not exist yet;
  tests specify the trap model and loader, the detector contract (fires on regression fixture,
  silent on clean fixture, completion check independent of detection), the `AgentRun` record and
  its provenance fields, the metric arithmetic (RR, consult rate, heed rate, Wilson interval,
  paired sign test) on hand-computed cases, and byte-identical report rendering.
- [x] **T1 -- Harness.** (GREEN commit on this branch) `evals/agent_regression/`: trap loader, detector protocol, adapter
  interface, fake replay adapter, runner, metrics, report writers. GREEN for T0.
- [ ] **T2 -- Claude Code adapter.** Headless invocation, tool allowlist, MCP registration per
  condition (writes the client config into the temporary clone, never into the user's), run
  record capture from the JSON output, budget enforcement. Tested with a stub `claude`
  executable on `PATH` that emits recorded JSON; the real binary is never invoked under pytest.
- [ ] **T3 -- Trap set.** 12 to 15 traps across the three sources, each with `trap.yaml`,
  `detect.py`, a regression fixture, a clean fixture, a completion check, and a second-reader
  sign-off recorded in `trap.yaml`. This is the largest and most valuable task; it is allowed
  to be its own PR.
- [ ] **T4 -- Cost estimate and go-ahead.** Dry-run the runner to count planned invocations
  (traps x conditions x N), estimate tokens from one pilot trap, and stop for the user's
  decision before any paid run. Record the approved budget here.
- [ ] **T5 -- Published run.** Execute, spot-check every fired detection by hand, commit the
  report with provenance. Publish the number as measured.
- [ ] **T6 -- Docs.** `evals/agent_regression/README.md` (method, conditions, threats to
  validity, reproduce block), a row in the top-level README's measured-evidence table, and a
  CHANGELOG entry. The README row states the one-client one-model scope in the same cell as the
  number.

## Acceptance criteria

- `uv run pytest -q -p no:cacheprovider tests/test_agent_regression_eval.py` passes offline with
  no model, no network, and no `claude` binary present.
- `uv run python -m agent_regression.run --dry-run` (with `evals/` on the path) or `uv run python evals/agent_regression/run.py --dry-run` lists every planned invocation and
  exits without calling an agent.
- Every committed trap's detector has both fixtures and both pass; every trap has a second-reader
  sign-off.
- The published report states RR, consult rate, heed rate, completion-conditioned RR, and cost
  per condition, with intervals, provenance, and the spot-check count, and reproduces from the
  committed run records byte-identically.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts`,
`uv run ruff format --check src tests evals scripts demo`, `uv run mypy src`.

## Progress / evidence

- 2026-09-27: T1 GREEN by one bounded writer: eight modules, 937 lines. Checks: full suite 1891
  passed / 18 skipped, the four sys.path-importing eval test modules green in one session, ruff
  check and format clean, mypy clean on src and on the package. `run.py` refuses to run without
  `--dry-run` until T2. Choices settled: `load_trap` rejects unknown keys (T3 must add any new
  field to `traps.py`); `AgentRun` has no transcript field yet (T2 adds it with the real client).
- 2026-09-27: T0 RED written by one bounded writer (80 tests, `tests/test_agent_regression_eval.py`).
  The writer found a flat-module-name collision with `evals/retrieval/metrics.py`,
  `evals/retrieval/adapters.py` and `evals/injection/run.py` (all imported through `sys.path` by
  their test modules in the same pytest session; commit bea29eb hit the same collision before).
  Decision: the harness is a real package, `evals/agent_regression/` with `__init__.py`, imported
  through `ROOT / "evals"`; no flat names. The parent also moved the repetition number into the
  adapter signature (the writer's draft had the replay adapter counting calls to infer it, which
  is hidden state). Settled by the tests: repetitions number from 0; `ConditionSummary.runs`
  counts error runs and every rate uses non-error runs as its denominator.
- 2026-09-27: task document written. Repository audit that motivated it: every existing eval is
  structural or retrieval-side; the agent-in-the-loop claim is unmeasured. Claude Code 2.1.283
  and Codex are installed locally; Claude Code is the first adapter.

## Next step

T0: write the RED tests for the trap model, detector contract, run record, metrics, and report
rendering. No harness code until a failing test names it.
