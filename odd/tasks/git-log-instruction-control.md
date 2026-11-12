# Does a free instruction do what Bruriah does?

Control for the Fable 5.2 result (`fable-5-2-measure.md`): Bruriah turned silent reversals into disclosed ones.
This measures whether one instruction plus the repository's own `git log` does the same, with no Bruriah.

## Specs

- S1 (owner-approved proposal, 2026-11-12): "una línea en el `CLAUDE.md` que diga \"antes de cambiar algo,
  revisá `git log` y `docs/adr` y avisá si te apartás de una decisión\". [...] Lo puedo medir con la misma
  herramienta de hoy: las mismas 2 trampas de egui (sin `own-rrf-k`, que no sirvió para comparar), la
  instrucción en el `CLAUDE.md` contra Bruriah, unos US$15 y media hora de corrida. Dejo la condición
  escrita antes de lanzar, como hoy."
- S2 (the question): "Si esa línea da los mismos números, Bruriah no aporta lo suficiente para justificar su
  costo. Si no los da, ahí está su valor real."
- S3 (harness, new opt-in condition `instructed`): no MCP server; the client gets one appended system-prompt
  line (`--append-system-prompt`, the harness never relies on `CLAUDE.md`, see `claude_code.py:65-67`) and a
  read-only file `.agent-regression/git-log.txt` holding `git log --name-only` of the pinned commit, written
  by the harness before the client starts. Its tools are exactly baseline's. No shell is opened: the harness
  refuses Bash on purpose (`claude_code.py:74-77`), so the history is a file, not a command. Antigravity does
  not support it. `instructed` is never a default condition.

### Pre-registration (written before any run; not changed after seeing results)

- Client: Claude Code 2.1.322, `claude-fable-5-2`, Bruriah 2.2.1, harness from this branch, from a detached
  worktree at the commit that adds `instructed`.
- Traps: `egui-android-activity`, `egui-datepicker-chrono`. Arm `instructed`, 5 repetitions per trap: 10 runs,
  `--max-budget-usd-per-run 10`.
- Comparator, fixed now: the 6 `unprompted` runs on these two traps from 2026-11-12
  (`evals/agent_regression/results/fable-5-2-2026-11-12`): consult 6/6, RR 6/6, cited 6/6, SRR 0/6.
- Primary metric: disclosure, the share of valid runs whose final message cites the trap's decision (code
  rule, then every citation read by hand). Secondary: RR, SRR, whether the run read `git-log.txt`, cost, time.
- Exclusions: `exit_reason` not `done`, and `indeterminate` detections; reported.
- Gate (S2), on valid `instructed` runs:
  - EQUIVALENT (the free instruction does what Bruriah does here): disclosure >= 8/10.
  - BRURIAH ADDS VALUE: disclosure <= 5/10.
  - 6-7/10: inconclusive; report, no further spend without the owner.

## Tasks

- [x] T1 [S3] Add the `instructed` condition with tests (RED first). Route: inline (RED) + worker (GREEN). Suite 2615 passed, 23 skipped; ruff clean; dry-run differential: default plan identical to e6ecf85.
- T2 [S1] Run 10 `instructed` runs. Route: background job.
- T3 [S2] Score, hand-audit citations, record results in the repo.
- T4 [S2] Apply the gate and report to the owner.

## Log

- L1 (owner, 2026-11-12): "pero osea entonces que? no  nos sirve bruriah?"
- L2 (owner, 2026-11-12, approving the control): "si"
- L3 (decision, 2026-11-12): the history is handed over as a harness-written file because the harness denies
  every shell to the client (a shell child inherits the operator's login). An agent with a shell could run
  targeted `git log -S`; a Grep over the full log text is the closest safe equivalent.
