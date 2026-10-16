# Antigravity CLI, `gemini-3.8-flash-high`, 2026-10-15

Bruriah's first measurement outside Claude Code: 30 runs on Antigravity CLI (`agy` 1.3.6), three egui
traps × `baseline` and `unprompted` × 5 repetitions, all at Bruriah `08a81db` (2.2.0).

**Result: no effect on regressions.** Unprompted agents consulted Bruriah in 14 of 15 runs, and
Bruriah returned the decision and its rationale in every documented-trap run. The agent read it,
then reintroduced the rejected alternative anyway. On the two documented traps, silent regressions
were 9/10 in both conditions (pooled Fisher exact p = 1.00). This is a heed failure, not a
retrieval failure.

- [`records/runs.jsonl`](records/runs.jsonl): the 30 run records. Transcripts are not committed.
- [`report.md`](report.md): the benchmark report rendered from those records with the current trap
  set. It reproduces byte for byte with
  `uv run python evals/agent_regression/report.py evals/agent_regression/results/antigravity-2026-10-15/records --out <dir>`.
- [`audit.json`](audit.json), written by [`audit.py`](audit.py): what Bruriah returned to each
  `unprompted` run (see [Audit](#audit)).

## Headline

Documented traps (`egui-android-activity`, `egui-datepicker-chrono`), 10 runs per condition:

| condition | SRR | RR | consult rate | completed | mean wall-clock |
|---|---:|---:|---:|---:|---:|
| `baseline` | 9/10 | 9/10 | 0/10 | 9 | 1158 s |
| `unprompted` | 9/10 | 9/10 | 10/10 | 9 | 1241 s |

No regressed run cited the decision, so every regression was silent. The one non-regressed run of each
condition is a datepicker run that hit the time budget before it changed the date type.

Per trap:

- `egui-android-activity`: 5/5 silent regressions in both conditions. Every run finished on its own.
- `egui-datepicker-chrono`: 4/5 in both conditions. All 5 `unprompted` runs hit the 1800 s budget
  after 13 to 16 Bruriah consults each. A run stopped by the budget has no final message, so it
  cannot cite anything: its silence is structural, not a choice.
- `egui-image-formats`, the undocumented-decision control (no written rationale exists to retrieve):
  4/5 in both conditions, consulted in 4/5 `unprompted` runs.

## Audit

The `agy` stream records each MCP call's arguments but never its output, so the transcripts cannot
show what Bruriah returned. Bruriah is deterministic over a fixed index, so `audit.py` replays every
Bruriah call of each `unprompted` run, in its recorded order, on the same executable and a copy of
the index that run used. The replay is a reconstruction, not a recording of what the agent received.

In each of the 10 documented-trap `unprompted` runs:

- an `investigate_work` reply listed the decision commit (`89e42884`, `a12d18d9`), the first reply
  in 9 of the 10 runs;
- a later `read_evidence` reply returned the decision document itself, with its title, pull request
  and rationale (for example `# Remove android-activity dependency + add activity features (#2863)`),
  at transcript steps 14 to 62;
- the first write to the clone came later, at steps 108 to 349, in every run that wrote;
- the five `egui-android-activity` runs ended with a final message, and none names the decision.

The agent's later queries show it took the decision in: it searched Bruriah for
`"PR 8008 replace chrono with jiff"` and for the decision document's file name. It then wrote the
rejected alternative and did not tell the reviewer.

## How this run differs from the Claude Code measurements

Read these before comparing it with [`consult-2026-10-07`](../consult-2026-10-07/):

- **Only `baseline` and `unprompted`.** `agy` has no system-prompt append flag, and its `PreToolUse`
  hook does not run in print mode, so `prompted` and `gated` cannot run.
- **The baseline has no Bruriah at all.** Its 0/10 consult rate is trivial. There is no "activation
  off, Bruriah registered" arm like Claude Code's control.
- **Web search was on in both conditions.** No setting turned it off. Every one of the 30 runs
  searched the web, so no web-free sensitivity table can be computed. `search_web` calls per 5 runs:

  | trap | `baseline` | `unprompted` |
  |---|---:|---:|
  | `egui-android-activity` | 81 | 76 |
  | `egui-datepicker-chrono` | 222 | 83 |
  | `egui-image-formats` | 129 | 97 |

- **The time budget was 1800 s**, not the trap manifests' 900 s. `agy` runs are slower, partly from
  server-side safety checks. 12 of the 30 runs hit it.
- **Activation reaches the agent as files.** `agy` writes the server's instructions and each tool's
  schema to files the agent opens on demand. Tool loading is deferred, so `anthropic/alwaysLoad`
  has no effect.
- **Subagent steps are not streamed.** A `subagent` step is recorded, but its inner tool calls are
  not visible.
- **Permissions.** Each run had a throwaway `HOME` with a copy of the operator's sign-in token. The
  shell, URL reads and URL executions were denied. Reads and writes were allowed only in the clone,
  plus reads of the run's MCP files, and every other path on the way from `/` was denied by name.
  A read of a path no rule matches (a different spelling, or a directory above the clone) ends the
  run in print mode. No run of this set ended that way.
- **No cost is recorded.** `agy` reports tokens, not dollars.

## Superseded runs

These runs are not reported:

- Pilots 1 to 3 (`d943931`, `0cf7c67`, `5568652`): instrument checks, one run each.
- A first 30-run set at `0cf7c67`, stopped at 13 runs. Its read permissions had a hole: a denied
  directory was readable through the macOS firmlink spelling and through a case variant.
  `5568652` replaced them with the sibling-deny rules above, and every run of this set started over
  from the fixed commit.

The full history, with every probe and decision, is in
[`odd/tasks/antigravity-adapter.md`](../../../../odd/tasks/antigravity-adapter.md).
