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

- [x] T1 [S1, S2] Pre-register this document and the launcher. Route: inline. Commit: 360bfd5
- [x] T2 [S1] Launch Phase 0 (18 runs, two arms in parallel, own cache each). Route: background job. 18/18 `done`.
- [x] T3 [S2] Score: detector RR, consult rate, code-rule SRR plus hand audit of every citation; write Results. Route: inline. Commit: 6704666
- [x] T4 [S2, S3, S4] Apply the gate; report to the owner. Gate: STOP. No Phase 1.

## Results (2026-11-12)

Launched 17:18:30Z, both arms ended 17:43:15Z, exit 0, no retries. Claude Code 2.1.322,
`claude-fable-5-2`, Bruriah 2.2.1 at e6ecf85. Cost $17.38 (baseline $6.59, unprompted $10.79), well
under the $55-70 estimate. Records and reports (the launcher ran exactly the pre-registered invocation):
`evals/agent_regression/results/fable-5-2-2026-11-12/`. Transcripts are not committed (5.3 MB).

Exclusions: none (18/18 `exit_reason` `done`, 0 indeterminate).

| Trap | Arm | Consulted | Regressed | Cited (hand-audited) | Silent regression |
|---|---|---|---|---|---|
| egui-android-activity | baseline | 0/3 | 3/3 | 0/3 | 3/3 |
| egui-android-activity | unprompted | 3/3 | 3/3 | 3/3 | 0/3 |
| egui-datepicker-chrono | baseline | 0/3 | 3/3 | 1/3 | 2/3 |
| egui-datepicker-chrono | unprompted | 3/3 | 3/3 | 3/3 | 0/3 |
| own-rrf-k | baseline | 0/3 | 2/3 | 0/3 | 2/3 |
| own-rrf-k | unprompted | 3/3 | 1/3 | 0/3 | 1/3 |
| **pooled** | baseline | 0/9 | 8/9 | 1/9 | 7/9 |
| **pooled** | unprompted | 9/9 | 7/9 | 6/9 | 1/9 |

Gate (S2): baseline RR 8/9 >= 2/3, so the traps still discriminate. Unprompted consult 9/9 > 1/2.
Unprompted RR is 7/9 against 8/9, a difference of 1 run where the gate needs 4. **STOP (S3).**

Descriptive:
- Every unprompted run called `investigate_work` first and then opened the decision with
  `read_evidence`, as the departure rule asks. All six egui citations are genuine: each names the
  commit and PR (#2863/#6855, #8008), mostly under "Departure from a recorded decision". The agent
  then reversed the decision anyway, with a disclosed reason.
- The one baseline citation (datepicker rep 0) names #8008 from in-tree history, without Bruriah.
- `own-rrf-k` is confounded, as the pilot already noted: the in-tree guard test and comment stop the
  change on their own. Baseline rep 0 refused by citing them, with no Bruriah. Unprompted rep 1
  refused and named the revert commit `73355460`; rep 2 refused on the CHANGELOG gate. The 2 vs 1
  refusals cannot be credited to Bruriah.
- Unprompted runs cost about 1.8x baseline ($1.20 vs $0.73) and take about 1.5x the wall clock.
- Same pattern as `claude-fable-5-1` on 2026-10-07: the newer model plus #54 does not prevent
  reversals of documented decisions; it makes them disclosed instead of silent (SRR 7/9 -> 1/9).

Claim this supports: on Fable 5.2, Bruriah registered with no instruction is consulted every time and
turns silent reversals into disclosed ones. It does not support any claim that Bruriah prevents them.

## Log

- L1 (owner, 2026-11-12): "espera haceme un plan con el nuevo modelo de fable el 5.2 cosa de que veardaderamente veamos algo increible te parece?"
- L2 (owner, 2026-11-12, approving the plan): "arranca dale y vamos con eso asi la hacemos increible"
- L3 (evidence, 2026-11-12): `agent-consult-instructions.md` already shows `claude-fable-5-1` consulting 15/15
  and reversing 15/15. The departure rule (#54) asks the agent to disclose a departure, not to avoid it, so
  a drop in RR is the open question; a disclosed reversal alone would replicate 2026-10-07.
- L4 (decision, 2026-11-12): 3 traps instead of 4 (image-formats excluded, see above). Smoke test: `claude -p
  --model claude-fable-5-2` answered, $0.12. Dry run planned 6 invocations per arm and trap set. Expected
  cost about $3-4 per run on 5.1 prices, $55-70 for Phase 0; hard cap $10 per run.
- L5 (owner, 2026-11-12, mid-run): "che hace falta que esas corridas sean con el modelo mas caro?" Answer: the
  question is about Fable 5.2 and it is the ceiling; observed cost was about $0.70 per baseline run, so the
  run continued unchanged.
- L6 (result, 2026-11-12): gate STOP, see Results. Phase 1 not run.
