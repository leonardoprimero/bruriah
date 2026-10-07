# Agent consult instructions

Branch: `feat/agent-consult-instructions` (from `main` at `1d36bc7`).

## Why

Run set B (Part 1, `eval/agent-regression-benchmark`) measured an unprompted consult rate of 0%.
The transcripts show why: Claude Code defers MCP tool definitions behind `ToolSearch` by default,
so at session start the model sees only the tool *names* and each server's `instructions`.
Bruriah sends no `instructions` (`src/bruriah/mcp_server.py`, `Server(SERVER_NAME)`), so the
"use it before answering..." guidance in the tool description never reached an unprompted agent.
Prompted runs reached the tools only because the instruction made them call
`ToolSearch("select:mcp__bruriah__investigate_work,...")`.

## Scope

- Static server `instructions`: when to consult Bruriah and that results are evidence, never
  instructions. The first 512 characters must stand alone (Codex guidance); the whole text stays
  under 2,048 characters (Claude Code truncation). Never built from corpus content.
- `_meta: {"anthropic/alwaysLoad": true}` on both tools so Claude Code loads their full
  descriptions upfront. Other clients ignore the key.

## Non-goals

- No third tool; the two-tool contract is unchanged.
- No writes into user repositories (`CLAUDE.md` / `AGENTS.md` snippet from `bruriah setup`) until
  the measurement shows whether the server-side levers suffice.
- The paid measurement run needs its own explicit go-ahead.

## Tasks

- [x] 1. Server `instructions` with contract tests (length bounds, standalone lead, static text).
- [x] 2. `anthropic/alwaysLoad` meta on both tools with contract tests.
- [x] 3. CHANGELOG `Unreleased` entry and README mention.
- [ ] 4. Measurement plan: unprompted condition on the three discriminating traps, against this
      branch's commit (paid; awaits go-ahead). Plan written below; run not launched.

## Evidence

(Recorded per task: commit identity, checks run.)

- Task 1: `9d740e2` feat(mcp): tell agents when to consult Bruriah through server instructions.
  Test-first: RED on import, then RED `assert None == SERVER_INSTRUCTIONS` with the constant
  unwired; GREEN `tests/test_mcp_contract.py` 20 passed; `test_cli.py` + `test_legacy.py` 117
  passed, 4 skipped (legacy `cerebro.db` absent); ruff check/format and mypy clean. Text is 708
  characters; `read_evidence` first appears at character 382. Native review
  `review-9f664732b5af9ff1` approved (reliability lens) and acknowledged; advisory suggestion
  R3-lead-paragraph-boundary-unasserted (the test does not pin where the lead paragraph ends).
- Task 2: `a26935e` feat(mcp): load both tool descriptions upfront in clients that defer tools.
  RED `assert None == {'anthropic/alwaysLoad': True}`; it stayed RED with `MCPTool(meta=...)`,
  because `Tool` does not populate fields by name and `meta=` lands silently in its extras (mypy
  also flagged it): the keyword must be `_meta=`. GREEN 138 passed, 4 skipped across
  `test_mcp_contract.py`, `test_cli.py`, `test_legacy.py`; ruff and mypy clean. Also pins the
  lead paragraph (410 characters, both tool names), closing task 1's advisory. Native review
  `review-d36fd4a4f0cda8d4` (scoped to this commit, base `9d740e2`) approved and acknowledged;
  advisories R3-copy-isolation-unasserted and R3-tautological-wire-dump.
- Task 3: `3bda107` docs: README setup section and CHANGELOG `Unreleased`; the README states the
  unprompted consult rate is not measured yet. Tests that read README/CHANGELOG: 211 passed,
  1 skipped (private corpus absent). Passive documentation: no native review.

## Measurement plan (task 4, not launched)

- The harness refuses to run unless the Bruriah server and the harness are the same commit
  (`evals/agent_regression/claude_code.py`, `bruriah_commit`). Measure from a dedicated worktree
  on a measurement branch that merges this branch into `eval/agent-regression-benchmark`
  (`git merge-tree` reports no conflicts).
- Confound: Claude Code is now 2.1.292; run set B ran on 2.1.287. Comparing against run set B's
  unprompted 0/15 would mix the client upgrade with this change. Recommended: a concurrent
  control on the same client version.
- Design: traps `egui-android-activity egui-datepicker-chrono egui-image-formats`, condition
  `unprompted`, N=5 each. Treatment: the merge commit. Control: `eval/agent-regression-benchmark`
  HEAD. 30 runs, about $65 at run set B's $2.16 per run.
- Primary: consult rate, treatment vs control, one-sided Fisher exact. Secondary: silent-
  regression rate (SRR) and regression rate (RR), with informed overrides hand-audited as in run
  set B. Pre-register this before launch.

## Pre-registration (approved 2026-10-07, before launch)

- Arms: control = `eval/agent-regression-benchmark` at `baa6174`; treatment = the merge of this
  branch into it on `measure/agent-consult-instructions`. Each arm runs from its own clean
  worktree with its own `.venv`, and its own copy-on-write clone of `run-set-b-cache`. Both arms
  launch together on the same Claude Code binary; model `claude-fable-5-1`;
  `--max-budget-usd-per-run 10`; condition `unprompted` only; traps `egui-android-activity
  egui-datepicker-chrono egui-image-formats`; `--repetitions 5`. 15 runs per arm.
- Primary hypothesis: the treatment consult rate (runs with at least one Bruriah tool call) is
  higher than the control's. Test: one-sided Fisher exact on 2x2 consulted/not consulted, alpha
  0.05. Expected control: 0/15 (run set B unprompted).
- Secondary, descriptive only (no claim from them at this N): SRR and RR per arm, with every
  informed override hand-audited as in run set B; whether treatment runs call `ToolSearch` for
  Bruriah or see the tools loaded upfront (from the transcripts' `system/init`).
- Exclusions: a recorded run with `exit_reason` `error` is reported and excluded from both
  numerator and denominator (`done`, `time_budget` and `turn_budget` count); if either arm loses
  more than 3 runs this way, the result is reported as inconclusive. Recorded runs are never
  rerun. Runs the harness fails to record (adapter failure, exit 3) are retried by rerunning the
  same command, up to 3 attempts 300 s apart, as in run set B.
- No change to the instructions text, the traps or the harness after seeing results; any such
  change needs a new pre-registered run set.
