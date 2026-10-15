# Antigravity CLI adapter for the agent regression benchmark

Branch: `feat/antigravity-adapter` from `main` at `24fae8b` (v2.2.0). Adds a second client adapter so
the benchmark can measure Antigravity CLI (`agy`) with Gemini models, and fills the Antigravity row
of the activation matrix with only what is measured. Paid runs need the owner's OK first.

## Specs

- S1. Owner goal (L1, L2): measure on Gemini, not Claude: "que consuma de gemini". The client is
  Antigravity CLI: "yo uso agy ya no se usa gemini CLI".
- S2. An `agy` adapter implements the existing `AgentAdapter` contract, reusing the trap, clone,
  detector, citation, run-record and resume machinery unchanged.
- S3. Isolation:
  - Every run uses a fresh throwaway `HOME`. The owner's `GEMINI.md`, `system.md`, hooks, skills,
    memory and MCP servers never reach the measured agent.
  - Login uses a copy of the owner's OAuth token (owner choice, L4: "Copiar mi token en cada
    corrida"). The copy lives in a `0700` directory, is deleted after the run, and is never logged,
    committed, written back or placed in a transcript.
  - A run that loads any MCP server other than the one its condition registers is refused.
- S4. Conditions `baseline`, `unprompted` and `prompted`. `gated` is supported only if `agy` has a
  pre-tool hook equivalent; otherwise it is refused with a clear error.
- S5. Comparability: the Claude Code runs have no shell tool. If `agy` cannot run without a
  terminal, that is reported to the owner as a decision, never hidden. Decided (L7):
  - The shell is denied (`deny: command(*)`), and URL reads and executions are denied too.
  - `search_web` stays on as the client default. Every run records its `search_web` calls.
  - Fixed before measuring: the report states this difference from Claude Code and adds a
    sensitivity table that excludes runs which searched the web.
- S6. `run.py` gains a client selector. With no selector, behavior and output stay exactly as today
  (Claude Code).
- S7. Hermetic tests use a stub `agy` executable; no test calls a real model.
- S8. Before any paid run: a dry run listing the invocations, a quota estimate from a small probe,
  and the owner's OK.
- S9. Results publish with provenance (client `antigravity`, `agy` version, model id). The matrix
  row changes only by what was measured.

## Tasks

| ID | Specs | Route | Commit |
|---|---|---|---|
| T1 | S3, S5 | inline probe: headless wire format, tool controls, MCP and instructions delivery | T1 doc commit |
| T2 | S2, S3, S4, S7 | inline, test-first: adapter with stub `agy` | |
| T3 | S6, S7 | inline, test-first: client selector in `run.py` | |
| T4 | S8 | inline: dry run, estimate, owner OK | |
| T5 | S9 | measured run, report, matrix row, docs | |

## Log

- L1 (2026-10-15, owner): "bueno a ver vamos con con eso que consume credito lo podemos hacer con
  gemini? osea usando gemini?" then "que consuma de gemini".
- L2 (owner choice): "Adaptador para Gemini CLI y medirlo (recomendado)", then "espera yo uso agy
  pue" and "yo uso agy ya no se usa gemini CLI".
- L3 Exploration:
  - `agy` 1.3.5 has `--print`, `--output-format stream-json`, `--model`, `--mode` and `--sandbox`.
  - It has no strict-MCP flag and no tool allowlist flag.
  - The owner's real config holds 4 MCP servers, a 56 KB `GEMINI.md`, hooks, skills and memory.
  - With a throwaway `HOME`, `agy` loads no MCP server but is signed out.
  - Login lives in `~/.gemini/antigravity-cli/antigravity-oauth-token`.
- L4 (owner choice): "Copiar mi token en cada corrida". Probe: a copied token in a throwaway `HOME`
  authenticates and loads no MCP server. The copy was rewritten (refreshed) by `agy models`; the
  original was unchanged and the owner's real `agy` stayed signed in.
- L5 (owner): "a como tenga que ser el que sea en verdad mas profesional dale". Isolated `HOME`
  with the copied token, same binary and account.
- L6 T1 probes: six small `gemini-3.8-flash-low` calls in throwaway `HOME`s, all removed afterward;
  the owner's real `agy` stayed signed in.
  - Wire format: `--print --output-format stream-json` emits `{"event": "init"}` (model, cwd,
    `permission_mode`, 60 tools, no MCP server list). It then emits `step_update` events: tool steps
    carry `tool_name`, parameters, and an error with a message; agent responses carry per-step
    usage. A final `result` holds status, `num_turns`, total usage, `denied_actions` and
    `response`, the final message, which is filled when the run ends normally.
  - Permissions in `settings.json` take `allow`/`deny` over `command`, `read_file`, `write_file`,
    `read_url`, `execute_url` and `mcp`. `deny: command(*)` hard-blocks `run_command` ("Matches
    user-configured deny rule") and the agent continues.
  - In headless mode, any action that would need a prompt is auto-denied and ends the turn with an
    empty `response` ("no output produced"). Every legitimate path must be allowed explicitly.
  - MCP: tools reach the agent only through a generic `call_mcp_tool`. Before calling, the agent
    reads the schema from `$HOME/.gemini/antigravity-cli/mcp/<server>/<tool>.json`. Tool loading is
    deferred, so Bruriah's `anthropic/alwaysLoad` meta has no effect here. The CLI also builds a
    `<mcp_servers>` system prompt block, where server instructions would land.
  - Not blockable with what was found: `search_web` (web search) ran despite `deny:
    read_url(*)`. The `disabledTools` and `disabled_tools` settings had no effect (still 60 tools).
    A `PreToolUse` hook in `$HOME/.gemini/config/hooks.json` never ran in print mode.
  - S5 consequence: the shell can be removed like in Claude Code, but web search cannot, as far
    as found. That makes it an owner decision.
- L7 (owner choice): "Medir con la web prendida y declararlo (recomendado)".
