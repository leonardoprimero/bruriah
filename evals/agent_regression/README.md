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
and `--out`. Launch each arm from its own clean worktree at the commit under test; the runner
records that commit and refuses a dirty tree. To render any run directory, use
`uv run python evals/agent_regression/report.py <run-dir> --out <dir>`.
