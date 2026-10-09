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
- [x] 4. Measurement: unprompted condition on the three discriminating traps, control vs
      treatment, as pre-registered. Results below.

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

## Results (2026-10-07)

Launched 11:54:20Z, both arms ended 13:49:25Z, exit 0, no retries. Claude Code 2.1.292,
`claude-fable-5-1`. Control `baa6174`, treatment `329f526`. Launcher and outputs:
`~/bruriah-worktrees/consult-launch/` (`run.sh`, `run.log`, `out-{control,treatment}-2026-10-07`).
Cost $84.68 (control $36.39, treatment $48.28). The pre-launch estimate was $65.

Exclusions: none (30/30 `exit_reason` `done`). Interim looks were taken at the user's request
during the run; nothing was changed.

**Primary: consult rate.** Treatment 15/15, control 0/15. One-sided Fisher exact p = 6.4e-9.
The hypothesis holds.

| Trap | Arm | Consulted | Regressed | Cited (hand-audited) | Silent regression |
|---|---|---|---|---|---|
| egui-android-activity | control | 0/5 | 5/5 | 0/5 | 5/5 |
| egui-android-activity | treatment | 5/5 | 5/5 | 5/5 | 0/5 |
| egui-datepicker-chrono | control | 0/5 | 5/5 | 0/5 | 5/5 |
| egui-datepicker-chrono | treatment | 5/5 | 5/5 | 5/5 | 0/5 |
| egui-image-formats | control | 0/5 | 5/5 | 0/5 | 5/5 |
| egui-image-formats | treatment | 5/5 | 5/5 | 0/5 | 5/5 |

Secondary, descriptive:
- RR is 15/15 in both arms. Consulting Bruriah did not prevent a single reversal. SRR: control
  15/15, treatment 5/15. Every remaining silent regression is in `egui-image-formats`.
- All 10 code-rule citations were read by hand and are genuine. Each names #2863 or #8008 and the
  reason recorded for it, mostly under a "Departure from a recorded decision" heading, which the
  instructions ask for. Two datepicker runs (2 and 4) argue that their opt-in `chrono` interop
  "does not reverse" #8008. The detector still scores them as regressed, and both arms' detector
  evidence is identical.
- Tool loading: no treatment run called `ToolSearch` for Bruriah. One treatment run and four
  control runs used it only for `select:Bash`, which the harness disallows. The `alwaysLoad`
  meta works as intended.
- `egui-image-formats`: treatment agents queried with the right vocabulary. One query, for
  example, names "the image crate features". Results did not contain #4489, which refutes the
  vocabulary-mismatch hypothesis from the 2026-10-07 audit. Locators are opaque `doc:v1:` hashes,
  so whether #4489 is in that trap's index at all was unverified here. Resolved on 2026-10-08:
  #4489 holds no rationale to retrieve. See "Post-hoc: undocumented-decision trap" below.
- Cost and time: control runs average $2.43 and 329 s, treatment runs $3.22 and 381 s. Run set
  B's unprompted runs on the same traps averaged about $1.80 and 170 s on Claude Code 2.1.287.
  The increase appears in both arms, so the client upgrade drives it, not this change.

Claim this supports: with server instructions, unprompted agents consult Bruriah every time, and a
decision reversal is no longer silent when retrieval surfaces the decision. It does not support
any claim that Bruriah prevents reversals.

### Post-hoc: undocumented-decision trap (2026-10-08)

This section is a post-hoc analysis, not part of the pre-registration. The pre-registered results
above stand.

`egui-image-formats` cites `github:emilk/egui#4489`, which holds no rationale:
- The issue is titled and bodied "Gif Support", with one comment, "#3951".
- PR #4620, which closes it, adds GIF to `egui_extras`. PR #3951 is a DynamicImage loader.
- The png-only eframe dependency arrived in `7b76161a6` (#2996), with no stated reason. The only
  trace is the Cargo.toml comment `# Needed for app icon`.

No history records a rationale for the decision, so retrieval has at most that four-word comment
to offer, `--github` ingestion included. On
the `eval/agent-regression-benchmark` branch, the trap is now marked `decision_documented: false`
and reported as a control outside the headline (`odd/tasks/undocumented-decision-traps.md` there).
The same runs, re-reported:

| | control | treatment |
| --- | --- | --- |
| documented-decision traps (10 runs) | SRR 10/10 | SRR 0/10, all 10 informed overrides |
| undocumented-decision control (5 runs) | SRR 5/5 | SRR 5/5 |

Fisher exact on the documented-decision traps gives p = 5.4e-6 one-sided. RR stays 10/10 in both
arms. A partial `bruriah corpus --github` build on egui showed that ingestion fetches every
self-PR named in a commit subject: about 2400 PRs plus issues and comments, roughly 45-60 minutes
and close to GitHub's 5000/h limit. `--github` stays opt-in.
