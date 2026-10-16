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
| T1 | S1, S2 | inline, test-first: the departure rule in the `investigate_work` description | `1abf6bb` |
| T2 | S3 | inline probe: `codex exec` wire format, MCP delivery, sandbox, auth isolation | T2 doc commit |
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
- L7 T2 probes (2026-10-16): three tiny `gpt-6-luna` calls plus model-free `codex sandbox` checks.
  Each ran in a throwaway `HOME` and `CODEX_HOME` with a copy of `auth.json`, and the probe directory
  was removed afterwards. The copied `auth.json` hash never changed, and the owner's `codex` stayed
  "Logged in using ChatGPT".
  - Wire format, `codex exec --json`:
    - `thread.started`, `turn.started`, `item.completed`, and `turn.completed` with `usage`
      {`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`};
    - item types `agent_message`, `command_execution` {`command`, `exit_code`,
      `aggregated_output`} and `mcp_tool_call` {`server`, `tool`, `arguments`, `result`
      {`content`, `structured_content`}, `error`, `status`}.
    - Unlike `agy`, the MCP output is in the stream, so no replay is needed.
  - stdin must be closed (`< /dev/null`), or exec waits on "Reading additional input from stdin".
  - Without the shell, Codex cannot read the clone. With `features.shell_tool = false` it listed
    no file-read tool and said it "can't read `hello.txt`". The shell has to stay, so the runs
    are not comparable on tools with the Claude Code and `agy` runs; this is disclosed.
  - `features.multi_agent = false` was not honored: `collaboration.spawn_agent` etc. were still
    listed. `agents.enabled = false` is set as well. `image_gen__imagegen` stays listed.
  - The permissions profile (`default_permissions`, `[permissions.<name>.filesystem]`) is
    enforced by the macOS sandbox on the resolved file. Under `codex sandbox`, these were all
    denied, "Operation not permitted":
    - `/Users/<owner>/.zshrc`, also via `/System/Volumes/Data/Users/...`, `/users/LEGUILLO/...`
      and `/USERS/...`;
    - `ls /Users/<owner>`;
    - the copied `auth.json`, via its `/tmp`, `/private/tmp` and firmlink spellings;
    - writes to `/tmp`.
    Allowed: reading and writing the clone and reading `/etc/hosts`. Network failed with "Could
    not resolve host". The firmlink and case-variant hole of `agy` (L18 of the Antigravity doc)
    does not exist here.
  - In the sandbox `git` does not run (the `/usr/bin/git` shim needs the denied CommandLineTools),
    and `rg` is not on the path. `grep`, `find`, `sed` and `cat` are. Keeping `git` out is
    intended: it would let a `baseline` run read the decision from history, which no Claude Code
    or `agy` run could.
  - The model sees its permissions. Asked to read denied paths, it declined without trying.
  - MCP: a `[mcp_servers.bruriah]` stdio entry with `required = true` worked. Codex quoted the new
    departure rule from the `investigate_work` description verbatim, and the call returned 41
    `decisions` for the android task.
  - Token use per tiny call: about 18k to 42k input tokens, about 50% cached.
  - Auth risk: `last_refresh` was 2026-10-09 and the access token had about 79 h left. A copy that
    refreshes could rotate the refresh token away from the owner's original, so the adapter
    compares the copy's hash after each run and stops the set if it changed.
- L8 (owner choice): "Las dos condiciones en 1.4.0 (recomendado)". Before T4 launched, `agy` reported
  1.4.0, but the 2026-10-15 set ran on 1.3.6. Rule against that set would mix the rule with a
  client change, so T4 became an A/B on one client version:
  - arm `norule`: Bruriah at `main` `d9b702f`, worktree `~/bruriah-worktrees/agy-norule`;
  - arm `rule`: Bruriah at `1abf6bb`, worktree `~/bruriah-worktrees/agy-run`.
  - Each arm is `unprompted`, three egui traps x 5, `gemini-3.8-flash-high`, 1800 s.
  - The arms alternate by repetition round (norule r, rule r, ...), so time of day does not line
    up with an arm. A round that records any error or failure stops the launcher.
  - Launcher: `agy-launch/run-rule-ab.sh`. Outputs: `out-ab-{norule,rule}-2026-10-16`. Launched
    as job-33. Both arms' dry runs planned 3 invocations per round.
  - The 2026-10-15 `baseline` is not reused: it ran on 1.3.6.
- L9 T3 Codex adapter, written by a worker test-first and reviewed by the parent.
  - The worker started from a `NotImplementedError` skeleton: 66 RED, then GREEN.
  - The parent's model-free checks against the real codex-cli 0.157.1:
    - The adapter's generated config loads under `--strict-config`. It reached authentication
      and failed with a 401 in a home without `auth.json`. A control key `bogus_key_xyz` failed
      at load with "unknown configuration field".
    - `codex debug prompt-input`: a marker in a clone's `AGENTS.md` is absent from the
      model-visible prompt with `project_doc_max_bytes = 0` and present with 65536.
  - Added, test-first (1 RED, then GREEN): the client now runs with `--strict-config`.
  - Mirrors: egui (`5d3e958`) and bruriah carry no `AGENTS.md`. leakcanary (`0f7dbab`) has
    `AGENTS.md` and `CLAUDE.md` at the root and in `shark/shark-explorer/`, which the config
    switches off.
  - The worker's design calls, accepted:
    - `AuthRotated` stops the set with exit 2. The rotated run's transcript is kept and its
      record is not written.
    - `shell` is not a write tool, because it is also Codex's only way to read. `file_change`
      and `apply_patch` are.
    - `.agents/` is refused alongside `.codex/`.
    - `model_id` is the requested model.
