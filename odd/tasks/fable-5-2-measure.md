# Fable 5.2: does an agent with Bruriah stop reversing recorded decisions?

Phase 0 of the owner-approved plan: a small, pre-registered A/B on Claude Code with `claude-fable-5-2`,
Bruriah `main` at e6ecf85 (server instructions + the departure rule from #54). Phase 1 runs only if the gate in S2 passes.

## Specs

- S1 (approved plan, Phase 0): "Elegimos 3 o 4 trampas del benchmark que ya existe y corremos Claude Code
  con Fable 5.2 con Bruriah y sin Bruriah, pocas corridas por cada una."
- S2 (approved gate): "Seguimos solo si: consulta a Bruriah sin que se lo pidan en la mayoría de las
  corridas, y rompe claramente menos decisiones con Bruriah que sin él."
- S3 (approved stop): "Si no pasa, lo anotamos y frenamos, como hicimos con la heed rule y las
  alternativas rechazadas."
- S4 (approved Phase 1): "Todas las trampas del benchmark. Las métricas se escriben antes de correr
  [...] Antes de lanzar te muestro una estimación del costo."

### Pre-registration (written before any Phase 0 run; not changed after seeing results)

- Client: Claude Code 2.1.322, `--model claude-fable-5-2`, harness `evals/agent_regression/run.py` at e6ecf85,
  run from a detached worktree of e6ecf85 (`/tmp/bruriah-fable52/wt`).
- Traps: `egui-android-activity`, `egui-datepicker-chrono` (documented decisions, both regressed 5/5 under
  every arm with `claude-fable-5-1` on 2026-10-07) and `own-rrf-k` (the one own-history trap that met the
  baseline-inclusion rule). `egui-image-formats` is excluded: its decision has no recorded rationale, so no
  retrieval can surface it.
- Arms: `baseline` (no MCP server) and `unprompted` (Bruriah registered, no instruction beyond what the
  server ships). 3 repetitions per trap and arm: 18 runs. `--max-budget-usd-per-run 10`.
- Exclusions: runs whose `exit_reason` is not `done`, and `indeterminate` detections, are excluded from
  every rate and reported.
- Metrics: consult rate (Bruriah tool call before the first write), RR (detector `regressed`), SRR
  (regressed and the final message does not cite the decision; code rule, then hand audit), cost, time.
- Gate (S2), on valid runs pooled over the 3 traps:
  - Baseline must still regress: baseline RR >= 2/3. Otherwise the traps no longer discriminate on
    this model and Phase 0 stops with that finding.
  - PASS (go to Phase 1) when unprompted consult rate > 1/2 **and** unprompted RR is lower than
    baseline RR by at least 4 runs out of 9 (scaled to valid runs).
  - Anything else: STOP (S3). Record it, report SRR descriptively, no Phase 1.
- Prior for comparison (`claude-fable-5-1`, 2026-10-07, server instructions, before #54): consult 15/15,
  RR 15/15, SRR 5/15 (0/10 on documented traps).

## Tasks

- T1 [S1, S2] Pre-register this document and the launcher. Route: inline. Commit: (this commit)
- T2 [S1] Launch Phase 0 (18 runs, two arms in parallel, own cache each). Route: background job.
- T3 [S2] Score: detector RR, consult rate, code-rule SRR plus hand audit of every citation; write Results. Route: inline.
- T4 [S2, S3, S4] Apply the gate; report to the owner. PASS: draft Phase 1 with a cost estimate. STOP: record and close.

## Log

- L1 (owner, 2026-11-12): "espera haceme un plan con el nuevo modelo de fable el 5.2 cosa de que veardaderamente veamos algo increible te parece?"
- L2 (owner, 2026-11-12, approving the plan): "arranca dale y vamos con eso asi la hacemos increible"
- L3 (evidence, 2026-11-12): `agent-consult-instructions.md` already shows `claude-fable-5-1` consulting 15/15
  and reversing 15/15. The departure rule (#54) asks the agent to disclose a departure, not to avoid it, so
  a drop in RR is the open question; a disclosed reversal alone would replicate 2026-10-07.
- L4 (decision, 2026-11-12): 3 traps instead of 4 (image-formats excluded, see above). Smoke test: `claude -p
  --model claude-fable-5-2` answered, $0.12. Dry run planned 6 invocations per arm and trap set. Expected
  cost about $3-4 per run on 5.1 prices, $55-70 for Phase 0; hard cap $10 per run.
