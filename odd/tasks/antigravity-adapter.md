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
- S4. Conditions `baseline` and `unprompted` (narrowed in L8). `prompted` and `gated` are refused
  with a clear error: `agy` has no system-prompt append flag, and its `PreToolUse` hook did not run
  in print mode.
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
| T2 | S2, S3, S4, S7 | inline, test-first: adapter with stub `agy` | adapter commit (L9) |
| T3 | S6, S7 | inline, test-first: client selector in `run.py` | same commit as T2 (L9) |
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
- L8 Two more probes, with paths resolved; both throwaway dirs were removed.
  - macOS `/tmp` resolves to `/private/tmp`, and permission rules match the resolved path. With
    resolved allow rules on the workdir (read, write) and on `$HOME/.gemini/antigravity-cli/mcp`
    (read), plus `mcp(bruriah/*)`, the run had no denial.
  - `agy` writes each server's instructions to `mcp/<server>/instructions.md` and each tool's
    schema to `mcp/<server>/<tool>.json`. The agent read `instructions.md`, quoted its first sentence
    exactly, read `investigate_work.json`, and called
    `call_mcp_tool {"ServerName": "bruriah", "ToolName": "investigate_work", "Arguments": {...}}`.
    It then named the returned decision commit in `result.response`. The instructions are
    delivered, but as a file the agent must open, not in the prompt.
  - Project configuration that `agy` reads: `.agents/` (hooks, skills, workflows) and `.gemini/`.
- L8 Design:
  - New module `evals/agent_regression/antigravity.py`. It reuses the mirror, clone, index, diff and
    source-commit helpers of `claude_code.py`.
  - Each run gets a fresh `0700` `HOME` with the token copy and a `settings.json` holding the
    permissions above, deny `command(*)`, `read_url(*)` and `execute_url(*)`, and for baseline
    `deny mcp(*)`. Bruriah is registered with `agy mcp add`. Before the run, `agy mcp list` must show
    exactly the condition's servers, and the whole `HOME` is deleted after the run.
  - `call_mcp_tool` is recorded as `mcp__<server>__<tool>`; other tools keep their native names, so
    `search_web` calls are countable. `runs.WRITE_TOOLS` gains `agy`'s file-writing tools.
    `citation.final_message` also reads `agy`'s `result.response`.
  - Turns are the agent-response steps. Tokens: input as reported, output plus thinking. Cost is
    not reported by `agy` and stays unset.
  - Exit reasons: `time_budget` on timeout; `done` only on SUCCESS with no auto-denied action; else
    `error`. A run cut short by an auto-denial is an error run, excluded from every rate and counted.
  - Limit stated in the report: `agy` has no turn limit flag, so only the time budget is enforced.
  - S4 narrowed: the 2.2.0 question for this client is whether unprompted agents consult Bruriah
    and stop reversing silently, which `baseline` against `unprompted` answers. `prompted` would
    need the instruction inside the task prompt, which is a different condition.
- L9 T2 and T3, test-first, with a stub `agy` that reproduces the measured wire format:
  - New: `evals/agent_regression/antigravity.py` and `tests/test_agent_regression_antigravity.py`.
  - RED, at first: collection failed because the module did not exist.
  - With the adapter written and the shared code untouched, exactly 4 tests failed: the three
    `agy` write tools and the `final_message` read of `result.response`. The Claude Code regression
    tests passed.
  - Shared changes: `runs.WRITE_TOOLS` gains `write_to_file`, `replace_file_content`,
    `multi_replace_file_content`, `sed_file` and `notebook_edit`. The pin
    `test_write_tools_are_exactly_the_file_mutating_tools` was updated on purpose.
    `citation.final_message` also reads `{"event": "result", "result": {"response": ...}}`.
  - T3 RED: 7 failed (selector, condition refusal, spend-cap refusal, missing `--agy`, end-to-end
    record, argument-free `client_version`).
  - `run.py` gains `--client {claude-code,antigravity}` (default claude-code), `--agy` and
    `--agy-token`. Unsupported conditions are refused at exit 2 even in a dry run, and so is
    `--max-budget-usd-per-run` for antigravity.
  - GREEN: 526 passed across the antigravity, eval and claude_code test modules. `ruff` and
    `mypy` are clean.
  - Docs: `evals/agent_regression/README.md` has a "second client" section that states every
    difference. The README test count is 2,586.
- L10 Verification and review of `35ed6fd`:
  - The independent `gentle-ai-verify` gave PASS on S2, S3, S4, S6 and S7, with one low defect: a
    partial `_make_home` left the dir behind.
  - Native review `review-0996d6d103a9b6dc` (base `24fae8b`, high tier, risk/resilience/
    reliability) confirmed one CRITICAL: R3-silent-client-flags. `--agy` and `--agy-token` were
    ignored without `--client antigravity`, and `--claude` under it, so the run billed the other
    client. It was fixed in `62ea163` (a mismatch exits 2, plus 2 tests); the validator confirmed
    the fix, and the review is APPROVED and acknowledged.
  - Host config: the refuter and validator roles pointed at the removed
    `antigravity/gemini-3.7-flash`. `gemini-3.8-flash` tried tool calls in the text-only relay.
    With owner OK, both roles now use `claude-bridge/claude-sonnet-5` in
    `~/.pi/gentle-ai/models.json` and in profile `planning`, with backups.
- L11 Hardening before any paid run (review advisories R1/R3/R4). Test-first: 4 RED, then GREEN
  (54 adapter tests).
  - A failure while preparing a run's `HOME` removes it with the token copy and is an
    `AdapterError`.
  - A `HOME` that cannot be removed fails the run, naming the path.
  - An interrupt kills the client and removes the `HOME`.
  - A missing `bruriah` executable is an `AdapterError`.
  - README count is 2,592.
  - Native review `review-915ba3e7e8b8c9e7` (high tier, risk/resilience/reliability) is APPROVED
    and acknowledged. Its advisory: all three lenses flagged that `_remove_home` runs before
    `_write_transcript`, so a `HOME` that cannot be removed discards a paid transcript.
  - Hardening committed in `f0846f2`; full suite 2569 passed, 23 skipped; ruff, format and mypy
    clean.
- L12 Advisory fix: the transcript is written before the `HOME` is removed, inside `try/finally`
  so a transcript write failure still removes the `HOME`. RED observed for the kept transcript;
  the removal test is a guard (green before and after). README count is 2,594.
  Full suite 2571 passed, 23 skipped; ruff, format and mypy clean.
- L13 (owner, 2026-10-15): "dale vamos con tu recomendacion y tenes mi ok". Recommendation
  accepted: model `gemini-3.8-flash-high`, one pilot run first to measure tokens and wall-clock,
  then the 30-invocation set (3 egui traps, `baseline` and `unprompted`, N = 5). The OK covers
  both. Runs launch from a dedicated worktree at the reviewed commit, left untouched until the set
  finishes.
- L14 Pilot (2026-10-15, `d943931`, `egui-android-activity`, `unprompted`, `gemini-3.8-flash-high`).
  - Ended `time_budget` at 900 s with no write. Unprompted consult happened: `investigate_work`
    was the 3rd call, and `read_evidence` ran 3 times. Isolation held.
  - Cause: model time summed ~246 s. Every tool call goes through the client's server-side
    safety monitors. At step 52 the agent called `invoke_subagent` (step type `subagent`), and
    nothing more streamed until the kill.
  - Instrument gaps found: the subagent invocation was not recorded, and tokens were unset
    without a result event (step usage sums to ~196k in and ~28k out).
- L15 (owner choice): "30 minutos y declararlo (recomendado)". Fixes, test-first (4 RED, then
  GREEN, 538 passed across the three benchmark test modules):
  - a `subagent` step is recorded as a call;
  - without a result, tokens are the sum of the steps' usage;
  - `--agy-time-budget-seconds` replaces the trap budget for agy runs and is refused for
    claude-code.

  README count is 2,598. Next: a second pilot at 1800 s, then the 30-invocation set.
