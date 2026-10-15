# Activation release 2.2.0

Branch: `release/2.2.0-activation` from `main` at `20ab5c8`. Ships the unreleased server
instructions, `anthropic/alwaysLoad` and `InvestigationResult.decisions` with a wheel wire check and
reproducible benchmark records. Plan of record: multi-model planning chain of 2026-10-15
(Gemini 3.8 Flash, Codex gpt-6-sol, Claude Opus 5.5); tag, push, PR and publish are the owner's.

## Specs

- S1. Owner goal (L1): "planear cual es el proximo paso que tiene que dar bruriah para convertirse
  en una herramienta inigualable para todos, individuos devs, empresas, para todos".
- S2. Owner decision (L3, L4): the next step is only the v2.2.0 activation release; teams,
  companies and other clients wait ("si dale arranca").
- S3. Release notes: `[Unreleased]` becomes `[2.2.0]`, and add the `decisions` field and the
  measurement paragraph, scoped to what was measured: Claude Code 2.1.292, `claude-fable-5-1`,
  three egui traps, N=5, at `329f526`. They must not claim that Bruriah prevents reversals, that it
  works in other clients, that the wheel itself was behaviorally measured, or that evidence
  "can never instruct".
- S4. Version 2.2.0 in `pyproject.toml`, `src/bruriah/__init__.py` and `uv.lock`.
- S5. A contract test pins the measured `SERVER_INSTRUCTIONS` text by its hash, its length and its
  lead length. The pre-tag diff `329f526..HEAD` must not touch the two constants.
- S6. `scripts/verify_activation.py` spawns the installed `bruriah serve` over stdio and asserts:
  - the instructions are present, at most 2048 chars, with a lead of at most 512 chars that names
    both tools;
  - there are exactly two tools, each with the `alwaysLoad` meta and an `outputSchema`;
  - one `investigate_work` call returns `schema_version "2"` and a `decisions` list.

  It runs in the CI `package` job against the clean wheel.
- S7. The injection report is regenerated with `decisions` present and still shows ASR 0/17.
- S8. The 2026-10-07 consult `runs.jsonl` records are committed. They are rendered twice:
  - pre-registered, with the traps as registered: 15 runs per arm;
  - post-hoc, with the current traps: headline 10 per arm plus a control section of 5 per arm.

  The renders are byte-identical when re-run. No absolute private path appears in any record, and
  transcripts are not committed.
- S9. `evals/agent_regression/README.md` covers method, definitions, threats, both result tables and
  how to reproduce. ODD `agent-regression-benchmark.md` T3–T6 are ticked.
- S10. The client activation matrix in `docs/client-guidance.md` has three columns: emitted, delivered
  and consult measured. The `README.md` "You do not need to tell the agent to use it" claim is scoped
  to Claude Code. A test parses the matrix.

## Tasks

| ID | Specs | Route | Commit |
|---|---|---|---|
| T1 | S5 | inline, test-first | `b21e026` |
| T2 | S6 | inline, test-first | |
| T3 | S7 | inline | |
| T4 | S8 | inline, test-first | |
| T5 | S9 | inline (docs) | |
| T6 | S10 | inline, test-first | |
| T7 | S3, S4 | inline (notes + bump last, after content is final) | |

Delivery forecast: about 700 authored changed lines (generated reports and records excluded).
Delivery: `chain_strategy=feature-branch-chain` (owner, L5). Tracker branch `release/2.2.0-activation`;
slices stacked on it: `release/2.2.0-1-wire-check` (T1, T2), `release/2.2.0-2-benchmark-records`
(T3, T4, T5), `release/2.2.0-3-notes` (T6, T7).

## Log

- L1 (2026-10-15, owner): "vamos con bruriah pone a codex a gemini a ver muy bien esto a planear cual
  es el proximo paso que tiene que dar bruriah para convertirse en una herramienta inigualable para
  todos, individuos devs, empresas, para todos eh dale. no uses astra nada mas, y usa el mejor modelo
  de gemini, y usa opus 5.5 tambien para la etapa del plan dale asi la llevamos a lo mejor"
- L2: Planning chain produced `/tmp/bruriah-plan/plan-final.md`; 21 critique findings, all accepted.
- L3 (owner): "y que ya incluya digo yo o no?" — recommendation: keep 2.2.0 narrow; M2 (Codex CLI
  adapter) starts right after the release, without the adoption gate.
- L4 (owner): "si dale arranca". Defaults applied: version 2.2.0; records only, no transcripts;
  post-release replication (T9 of the plan) left to the owner.
- L6 T1 evidence: text identical at `329f526` and `HEAD` (sha256 `d3401a48…`, 708/421 chars).
  A one-character mutation of `SERVER_INSTRUCTIONS` makes the pin test fail; the real text passes it
  (`tests/test_mcp_contract.py`, 22 passed). RDD assess: medium, deferred to the slice.
- L7 T2 RED: the 14 `tests/test_verify_activation.py` tests failed at collection because the
  script did not exist; GREEN came after `scripts/verify_activation.py` was written (14 passed).
- L8 T2 evidence:
  - The real-index end-to-end run passed: `init` then `verify_activation.py` gave "activation
    check passed" and the repository stayed clean.
  - A local simulation of the CI `package` job passed: wheel built, clean venv, the step extracted
    from `ci.yml`, isolated `HOME`, `doctor` healthy, activation check passed.
  - The full suite gave 2492 passed and 23 skipped, 0 failures (2515 collected, matching the README).
- L9 (owner): "bien por hoy cerremos sesion seguimos en otra dale". The session stopped after T2.
  Next: RDD assess for T2. Then close slice 1 with a native review (base `20ab5c8e38fe7f14a96e67d3c940035fe30d201b`,
  committedOnly) and start T3 on `release/2.2.0-2-benchmark-records`, branched from the slice 1 tip.
  Precheck already done for T4: the 2026-10-07 records have 15 rows per arm, every row has
  `cited_decision`, and no private path appears.
- L5 (owner): delivery "Cadena de PRs sobre una rama de feature"; read access granted to
  `/Users/leguillo/bruriah-worktrees/consult-launch/` for the 2026-10-07 records.
