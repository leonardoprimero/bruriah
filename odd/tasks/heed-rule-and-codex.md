# Heed rule in the tool description, and a Codex CLI benchmark client

Branch: `feat/heed-rule-codex` from `main` at `d9b702f` (v2.2.1). The work puts the "say so when you
depart from a decision" rule where every client reads it, adds Codex CLI as a third benchmark
client, and measures both. Every run that spends model quota needs the owner's OK first.

## Specs

- S1. Owner goal (L1): "bueno vamos a la A che y podemos medir tambien con codex algun modelo
  chico? es decir yo tengo el plan de 20 nomas crees que alcance para hacer alguna medicion ?"
  Option A (L3) means the departure rule goes into the `investigate_work` tool description: open
  the decision with `read_evidence`, and if the change departs from it, say so in the final
  message, naming the decision.
- S2. The rule is static text, never corpus text. The investigate boundary stays opaque, and the
  injection benchmark must still measure ASR 0/17.
- S3. A Codex CLI adapter implements the existing `AgentAdapter` contract, following the
  Antigravity adapter.
  - Each run gets a throwaway, isolated home with a copy of the owner's ChatGPT sign-in, removed
    on every exit path.
  - The owner's config, `AGENTS.md`, rules, skills, memories and MCP servers never reach the
    measured agent.
  - Shell and network are denied as far as Codex allows. Anything that cannot be denied is
    disclosed.
  - The model is `gpt-6-luna` (L4: "algun modelo chico").
- S4. `run.py --client codex`. Without the selector, behavior and output stay exactly as today.
  Hermetic tests use a stub `codex`; no test calls a real model.
- S5. Measurements, each needing the owner's OK before it spends quota (L5):
  - `agy`, `gemini-3.8-flash-high`: 15 `unprompted` runs with the rule, compared against the
    2026-10-15 set. That set's `baseline` is reused, because the rule cannot change a run without
    Bruriah.
  - Codex, `gpt-6-luna`: one pilot run first to measure quota use, then 15 runs each of
    `baseline`, `unprompted` without the rule, and `unprompted` with the rule.
- S6. Claude Code is not re-measured. The results state that the rule's effect there is not
  measured.
- S7. Results publish with provenance and every difference from the earlier runs. Each table row
  changes only by what was measured.

## Tasks

| ID | Specs | Route | Commit |
|---|---|---|---|
| T1 | S1, S2 | inline, test-first: the departure rule in the `investigate_work` description | |
| T2 | S3 | inline probe: `codex exec` wire format, MCP delivery, sandbox, auth isolation | |
| T3 | S3, S4 | inline, test-first: Codex adapter with stub and `run.py --client codex` | |
| T4 | S5 | `agy` set with the rule (owner OK) | |
| T5 | S5 | Codex pilot, then the three Codex arms (owner OK each) | |
| T6 | S6, S7 | results, docs, PR | |

## Log

- L1 (2026-10-16, owner): "bueno vamos a la A che y podemos medir tambien con codex algun modelo
  chico? es decir yo tengo el plan de 20 nomas crees que alcance para hacer alguna medicion ?"
- L2 Diagnosis behind A, from the 2026-10-15 `agy` set:
  - Gemini read `mcp/bruriah/instructions.md` in only 2 of 15 `unprompted` runs, at steps 80 and
    98, both after consulting. It read the `investigate_work` schema file in 15 of 15.
  - The departure rule ("say so explicitly when your change departs from a recorded decision")
    lives only in `SERVER_INSTRUCTIONS`. The tool description does not carry it.
  - Unknown: whether `agy` also inlines the instructions in its system prompt.
- L3 (owner choice): "bueno vamos a la A". An inline decision summary in `investigate_work` was
  ruled out, because the investigate boundary is opaque by construction (schema v2: no corpus
  text), which is what keeps the injection benchmark at ASR 0/17.
- L4 Codex research (2026-10-16), from learn.chatgpt.com docs/models and docs/pricing:
  - The CLI models are `gpt-6-astra`, `gpt-6.1-sol` and `gpt-6-luna`.
  - On Plus, the estimates are 350 to 3,000 local messages per 5 hours with Luna and 15 to 160
    with Sol. They are "not fixed message limits", and weekly limits may also apply.
  - Locally: `codex-cli 0.157.1`, "Logged in using ChatGPT", and `codex exec` has `--json`, `-m`,
    `--ignore-user-config` and `-C`.
- L5 (owner, on the plan with the Codex adapter included): "si dale".
- L6 T1, test-first: 2 RED, then GREEN. The `investigate_work` description gains one paragraph:
  "If a returned decision covers what you are about to change, open it with read_evidence first.
  If your change then departs from it, say so in your final message and name that decision's
  commit."
  - `SERVER_INSTRUCTIONS` is untouched, so its measured-text pin still holds.
  - `evals/injection/run.py` re-ran byte-identical: ASR 0/17.
  - `docs/client-guidance.md` states where the rule lives. The README count is 2,618.
