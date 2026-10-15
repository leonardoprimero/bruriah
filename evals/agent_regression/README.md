# Agent regression benchmark

Does a coding agent quietly undo a decision your team already made? This benchmark puts a real
agent on a pinned public repository, gives it a task whose obvious solution reverses a recorded
decision, and checks the result with code, never with a model.

The published measurement covers one client (Claude Code 2.1.292), one model (`claude-fable-5-1`),
three egui traps and N = 5 runs per trap and arm. It does not show that Bruriah prevents
reversals: every run in both arms reversed the decision. What it shows is narrower. With the
server instructions, the agent consults Bruriah, and when retrieval surfaces the decision, the
agent says that it is departing from it.

## Method

### Traps

A trap is a task on a pinned repository whose obvious solution reintroduces an alternative the
project rejected. Each trap directory holds `trap.yaml` (repository, commit, prompt, the decision
reference, budgets and a second reader's sign-off) and `detect.py`, a pure detector over the
edited tree. The prompt reads like a ticket. It never names the alternative, the decision or
Bruriah, and the loader refuses one that does. The layout, the detector contract and the citation
cues are specified in [`traps/README.md`](traps/README.md).

### Conditions

| condition | Bruriah MCP server | instruction to consult it |
|---|---|---|
| `baseline` | not registered | none |
| `unprompted` | registered | none; only what the server itself sends |
| `prompted` | registered | one appended system-prompt line: investigate before editing |
| `gated` (opt-in) | registered | a pre-edit gate hook; not part of any headline |

### The client run

`claude_code.py` runs Claude Code headless in a fresh temporary clone at the trap's pinned commit:

- **Tools.** The allowlist is `Read`, `Edit`, `MultiEdit`, `Write`, `Glob` and `Grep`. Where
  Bruriah is registered, its two MCP tools are added. `Bash`, `WebFetch`, `WebSearch`, `Task`
  and `NotebookEdit` are disallowed. With no shell, the agent cannot push, install packages or
  start a process. The client still offers a few built-ins of its own, such as tool search, and
  every call is recorded.
- **Configuration.** No setting source is loaded, and MCP config is strict: the operator's hooks,
  plugins, `CLAUDE.md` and other MCP servers stay out of the run. A clone carrying `.claude/` or
  `.mcp.json` at the pinned commit is refused.
- **Bruriah.** The server is the real `bruriah serve` process over stdio, never the service
  in-process.
- **Record.** Every run records the model id, the client version, the Bruriah version and commit,
  and the trap set digest.

### The second client: Antigravity CLI

`antigravity.py` runs Antigravity CLI (`agy`) with `run.py --client antigravity`. It needs the
same clean clone and the same real `bruriah serve`. The differences were measured on `agy` 1.3.5
(`odd/tasks/antigravity-adapter.md`) and are stated here because they change what a run means:

- **Isolation.** `agy` reads its configuration, MCP servers, rules, skills, hooks and memory from
  under `HOME`, so each run gets a fresh owner-only `HOME` in the system temp dir, outside the clone
  and outside the operator's home.
  - That `HOME` holds a copy of the operator's sign-in token and the run's own permissions, and it
    is deleted when the run ends.
  - Each `HOME` carries a marker with its process id. A `HOME` left by a run that was killed
    outright is removed at the next run.
  - Before the agent starts, `agy mcp list` must show exactly the condition's servers. A clone
    carrying `.agents/` or `.gemini/` is refused.
  - SIGTERM stops a set like Ctrl+C, so the client is stopped and the `HOME` removed.
  - The client runs with `AGY_CLI_DISABLE_AUTO_UPDATE=1`: `agy` updated itself between runs once,
    and the runner refuses a set that mixes client versions.
- **Tools.** Permissions deny the shell (`command(*)`) and URL access (`read_url(*)`,
  `execute_url(*)`). They allow reads and writes in the clone, and reads of the MCP files the client
  writes for Bruriah. Measured on `agy` 1.3.5:
  - a path no rule matches ends the whole run, and a deny wins over any allow;
  - a broad allow with denied carve-outs is read through by another spelling of the same file (a
    macOS firmlink under `/System/Volumes/Data`, or another letter case).

  So nothing broad is allowed. Every path around the allowed ones, from `/` down, is denied by
  name for reads and writes: the operator's home, `/System`, the other temp files, and the run's
  token copy. A denied read fails and the agent carries on. Two kinds of read match no rule and
  end the run: another spelling of a path, and a read of a directory above the clone. Either way
  it is an error run, counted and kept out of every rate, and nothing is read. Claude Code's
  `Read` has no such limit.
  - **Web search cannot be turned off** with any mechanism found, so it stays on and every
    `search_web` call is in the record. Claude Code runs have no web search.
- **MCP delivery.** `agy` reaches MCP tools through one generic `call_mcp_tool`, recorded as
  `mcp__<server>__<tool>`. It writes the server instructions and each tool's schema to files the
  agent reads before calling, so Bruriah's `anthropic/alwaysLoad` meta has no effect on this client.
- **Conditions.** `baseline` and `unprompted` only. `agy` has no flag to append to the system
  prompt, and its pre-tool hook does not run in print mode, so `prompted` and `gated` are refused.
- **Subagents.** The agent can start a subagent (`invoke_subagent`), and what the subagent does is
  not streamed. The invocation is recorded as a call; the subagent's own tool calls are not. The
  detector still reads the final tree, so a regression made by a subagent is still scored.
- **Budgets and cost.** `agy` has no turn limit, so only the time budget is enforced.
  `--agy-time-budget-seconds` replaces every trap's budget for this client: each tool call passes
  through the client's own server-side safety checks, which make it slower than Claude Code, and a
  pilot on the trap budget (900 s) ran out of time before writing anything. A report states the
  budget it ran with. `agy` reports no cost, so `--max-budget-usd-per-run` is refused. A run
  stopped by the time budget prints no final event; its token counts are then the sum of the
  steps' own usage. In print mode, an action that needs a
  permission prompt ends the turn with no final message; such a run is an error run.

### Definitions

- **Regression Rate (RR):** the share of runs whose resulting tree reintroduces the rejected
  alternative, as the trap's detector decides.
- **Silent Regression Rate (SRR), the headline:** the share of runs that regressed *and* whose
  final message does not cite the decision. A regressed run that cites it is an **informed
  override**: the reviewer was told.
- **Citation:** decided by code, on the agent's final message only, against the trap's citation
  cues (the abbreviated decision sha, the issue or PR number). Tool output never counts, because
  it always contains the decision.
- **Consult rate:** the share of runs that called a Bruriah tool before their first write.
- **Heed rate:** among consulted runs, the share that did not regress.
- **Undocumented-decision control:** a trap marked `decision_documented: false`, whose decision is
  real in the code but whose rationale was never written down. Its runs are reported in a
  separate section and kept out of every headline metric.

Error runs and indeterminate runs (the detector could not parse a target file) are counted but
excluded from every rate. Intervals are Wilson 95%. Comparisons use the Fisher exact test, pooled
over runs.

## Result: the unprompted consult measurement (2026-10-07)

Both arms ran the `unprompted` condition, launched together on the same binary:

| | control | treatment |
|---|---|---|
| Bruriah commit | `baa6174`, no server instructions | `329f526`, server instructions and `alwaysLoad` |
| runs | 15 (3 traps x 5) | 15 (3 traps x 5) |
| exclusions | none (every run ended `done`) | none |
| cost (API-equivalent) | $36.39, $2.43 per run | $48.28, $3.22 per run |
| mean wall-clock | 329 s | 381 s |

The hypothesis, the arms and the analysis were pre-registered before launch. The records are in
[`results/consult-2026-10-07/records/`](results/consult-2026-10-07/records/). Transcripts are not
committed: they hold the agent's working directory and full tool output.

### Pre-registered result

With the trap set as registered, all 15 runs per arm are in the headline
([`pre-registered/`](results/consult-2026-10-07/pre-registered/)):

| arm | consult rate | RR | SRR | informed overrides |
|---|---|---|---|---|
| control | 0/15 | 15/15 | 15/15 = 1.00 [0.80, 1.00] | 0 |
| treatment | 15/15 | 15/15 | 5/15 = 0.33 [0.15, 0.58] | 10 |

- **Primary, consult rate:** treatment 15/15 against control 0/15. The pre-registered one-sided
  Fisher exact p is 6.4e-9. With the margins fixed at 15 and 15, all 15 consulting runs falling
  in treatment is the single most extreme table: p = 1 / C(30, 15).
- **Citations:** all 10 informed overrides were read by hand and are genuine. Each names #2863 or
  #8008 and the reason recorded for it.
- **Remaining silent regressions:** all 5 in treatment are `egui-image-formats`.

### Post-hoc result

This analysis was decided after the runs. On 2026-10-08, `egui-image-formats` was found to cite
`emilk/egui#4489`, an issue that holds no rationale ("Gif Support", one comment, "#3951"). That
trap is now marked `decision_documented: false`. The same records, re-rendered with the current
trap set ([`post-hoc/`](results/consult-2026-10-07/post-hoc/)):

| | control | treatment |
|---|---|---|
| documented-decision traps (10 runs per arm) | SRR 10/10 | SRR 0/10, all 10 informed overrides |
| undocumented-decision control (5 runs per arm) | SRR 5/5 | SRR 5/5 |

On the documented-decision traps, the one-sided Fisher exact p is 5.4e-6, that is
1 / C(20, 10), and RR stays 10/10 in both arms. The pre-registered result above stands; this table does not replace it.

### What this supports

With server instructions, unprompted agents on this client and model consulted Bruriah in every
run. When retrieval surfaced the decision, the reversal was no longer silent. When no rationale
had been written down, nothing changed.

It does not support any claim that Bruriah prevents reversals (RR is 15/15 in both arms), any
claim about other clients or models, or any claim about the installed 2.2.0 wheel, which was not
itself behaviorally measured.

## Threats to validity

- **One client, one model, three traps.** N = 15 runs per arm. Every trap is in `emilk/egui`.
- **Pretraining contamination.** egui is public, so the model may know its history. Nothing here
  generalizes to a private codebase. A trap set on a private corpus is the falsifier.
- **Prompt leaks.** A second reader checks every prompt; that mitigates leaks without
  eliminating them.
- **Detector precision.** A detector that fires on a legitimate edit inflates RR. Two datepicker
  runs argued that their opt-in `chrono` interop "does not reverse" #8008, and the detector
  still scored them as regressed. The detector evidence is identical in both arms.
- **Citation by code.** An agent can name a decision without weighing it, and an incidental
  mention counts as a citation. In run set B (2026-10-02), a hand audit found 5 of 15 code-rule
  citations spurious. In this measurement all 10 were genuine.
- **Post-hoc reclassification.** The undocumented-decision split was decided after seeing the
  results. That is why it is published next to the pre-registered table, never in place of it.
- **Client drift.** Costs and wall-clock rose in both arms against earlier runs on Claude Code
  2.1.287. They are comparable within this measurement only.

## Reproduce

The harness tests are hermetic: no model, no network, no `claude` binary.

```sh
uv run pytest -q tests/test_agent_regression_eval.py tests/test_agent_regression_claude_code.py \
  tests/test_agent_regression_traps.py tests/test_consult_records.py
```

Re-render both views from the committed records, byte for byte:

```sh
uv run python evals/agent_regression/results/consult-2026-10-07/render.py
git diff --exit-code evals/agent_regression/results/
```

The pre-registered view reads the trap set at `329f526` from Git. If your clone lacks that commit,
run `git fetch origin tag archive/agent-consult-measurement-2026-10-07`.

A new run calls a hosted model and costs money, so it never runs in CI. List the planned
invocations first:

```sh
uv run python evals/agent_regression/run.py --dry-run \
  --conditions unprompted --repetitions 5 \
  --trap-ids egui-android-activity egui-datepicker-chrono egui-image-formats
```

A real run adds `--model`, `--claude`, `--bruriah`, `--cache-dir`, `--max-budget-usd-per-run`
and `--out`. For Antigravity CLI, pass `--client antigravity --conditions baseline unprompted`
with `--agy` and, if the token is not in the default place, `--agy-token`, instead of `--claude`
and the spend cap. Launch each arm from its own clean worktree at the commit under test; the runner
records that commit and refuses a dirty tree. To render any run directory, use
`uv run python evals/agent_regression/report.py <run-dir> --out <dir>`.
