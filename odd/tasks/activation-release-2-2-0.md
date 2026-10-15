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
| T2 | S6 | inline, test-first | `6029497` |
| T3 | S7 | inline | no change: report byte-identical (L11) |
| T4 | S8 | inline, test-first | `c23d8d4` |
| T5 | S9 | inline (docs) | `9db7f6c` |
| T6 | S10 | inline, test-first | slice 3 matrix commit |
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
- L10 Slice 1 review: native lineage `review-0f5b6d48616a496e` (base `20ab5c8`, commits `b21e026`,
  `6029497`), tier high, lenses resilience and reliability. APPROVED and acknowledged. Four
  non-blocking follow-ups:
  - the console-script assert at `tests/test_verify_activation.py:164-165`;
  - the timeout branch, which has no test (`tests/test_verify_activation.py:184-186`);
  - the exception-group diagnostics at `scripts/verify_activation.py:119-120`;
  - the exact equality of the meta at `scripts/verify_activation.py:68-69`.
- L11 (2026-10-15, owner: "continuemos dale") T3 evidence: `evals/injection/run.py` regenerated
  the report byte-identical, ASR 0/17. It scans the whole `model_dump(mode="json")`, `decisions`
  included. A probe showed `decisions` non-empty in 7 of the 17 cases (every git, github and
  code-target case). The field is `list[CommitSha]`, so it cannot carry a marker. No commit was
  needed.
- L12 T4: records copied with sha256 unchanged. They go to
  `evals/agent_regression/results/consult-2026-10-07/records/{control,treatment}/runs.jsonl`.
  `render.py` there renders two views:
  - `pre-registered/`: the trap set at `329f526`, read with `git archive`; that commit is
    reachable from the tag `archive/agent-consult-measurement-2026-10-07`, and CI checks out with
    `fetch-depth: 0`;
  - `post-hoc/`: the current trap set.

  The registered and current trap sets differ only in `egui-image-formats`
  `decision_documented: false`. RED: the 12 `tests/test_consult_records.py` tests failed, with no
  artifacts yet. GREEN: 12 passed. The pre-registered reports are byte-identical to the original
  2026-10-07 `out-*/report.md`. The post-hoc reports match the 2026-10-08 ones except for prose and
  the classification line added in `01ac1c2`; the numbers are identical:
  - pre-registered SRR: control 15/15, treatment 5/15;
  - post-hoc SRR: control 10/10, treatment 0/10, with 10 informed overrides; the control section
    is SRR 5/5 in both arms.

  Full suite: 2503 passed, 23 skipped, and 1 failure, the README count (2515 against 2527
  collected), which was then corrected to 2,527. Ruff check and format are clean.
- L13 T5: added `evals/agent_regression/README.md` with method, conditions, the client run,
  definitions, both result tables, the claim scope, threats and reproduce commands. Every number
  was recomputed from the committed records:
  - cost $36.39 and $48.28, mean wall-clock 329 s and 381 s;
  - consult 0/15 and 15/15;
  - one-sided Fisher p = 6.4e-9 for consult and 5.4e-6 for documented SRR.

  The `--dry-run` command quoted there lists the 15 invocations and exits 0. A row was added to the
  `README.md` evidence table, scoped to one client and model. `traps/README.md` now points at the
  committed renders. ODD `agent-regression-benchmark.md` T3-T6 are ticked.
- L14 Slice 2 verify and review:
  - Independent `gentle-ai-verify` returned S7, S8 and S9 PASS with no defects.
  - RDD assess on base `6029497` gave high risk (`process_boundary`: `render.py` shells out to
    `git archive`). The native START failed in preflight with `lens_context_budget_exceeded`, and
    no authority was created. The cause was the four generated `runs.json` renders, about 280 KB
    that repeat the records.
  - Owner decision (choice): "No commitear los runs.json (recomendado)".
  - `render.py` now renders in a scratch directory and keeps only each view's `report.md`. The
    tests read the views from the Markdown and require only Markdown. RED: 2 failed. GREEN: 13
    passed, and the four `report.md` stayed byte-identical.
  - The two local slice-2 commits were rebuilt (`0eff16f` and `c58bacd`, never pushed). README
    count is 2,528.
  - Full suite: 2505 passed, 23 skipped, 0 failed. Native review `review-e7c1188bb5c9c040`
    (base `6029497`, lenses resilience, readability and reliability) was APPROVED and acknowledged,
    and its authority burned. It raised nine non-blocking advisories. Taken in slice 3: the
    README tool allowlist omits `MultiEdit` and the MCP tools, and committed code does not
    reproduce the one-sided p values. Left as follow-ups:
    - `render.py` can write partially on a late failure;
    - the private-path scan reads every file as text;
    - the byte test is skipped without the registered commit;
    - the run-set-B prose generated by `report.py`;
    - the duplicated `REGISTERED_COMMIT`.
- L15 T6, branch `release/2.2.0-3-notes` from `9db7f6c`: `docs/client-guidance.md` gains
  `## Activation matrix` (client, emitted, delivered, consult measured), one row per `ClientId`.
  Only Claude Code is marked delivered and measured; every other client is "unknown" and "not
  measured". The README claim now reads "In Claude Code, you do not need to tell the agent to use
  it." and links the matrix.

  `tests/test_client_activation_matrix.py` has 5 tests. The first RED had 4 failures and 1 false
  pass: the sentence splitter merged the preceding code block, which says "(Claude Code)". The fix
  splits by paragraph and drops fences, and the RED then had 5 failures. GREEN: 66 passed, with
  `tests/test_clients.py`. README count is 2,533.
