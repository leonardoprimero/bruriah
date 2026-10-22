# Client Launch Guidance

This document is rendered from the same canonical launch manifest that
`src/bruriah/clients.py` uses to generate every client's config
snippet. There is exactly one source of truth for how the server is
launched -- an absolute `command`, `args`, and `env` -- and every client
below launches that identical process. No client redefines the public
`investigate_work`/`read_evidence` schemas or the router's safety policy
(spec `K-Canonical Client Launch Manifest and Adapters`).

The example manifest used throughout this document is:

```json
{
  "command": "/usr/local/bin/bruriah",
  "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
  "env": {}
}
```

Substitute the real absolute path to your installed `bruriah`
console script (from Slice 8B's packaging) and your private config
directory. `bruriah serve` is the only command any client config
below should ever launch.

Diagnostics for every client are stderr-only; nothing this server writes
ever lands on stdout, which is the JSON-RPC transport channel itself.

## Version visibility

Every launch manifest carries a `router_version` field, defaulting to the
installed `bruriah.__version__` at the moment the manifest is constructed.
Run `bruriah --version` to see what that is on your machine; this sentence
deliberately does not repeat the number, having claimed `0.1.0` for three
releases after it stopped being true. This gives version visibility (spec
`K-Canonical Client Launch Manifest and Adapters`: "Adapters MUST use
absolute executable semantics, explicit arguments/environment, version
visibility, stderr diagnostics, and no shell-specific quoting
dependency") -- any caller can tell which router version a given config
snippet was generated against.

`router_version` is intentionally NOT injected as an extra key into the
rendered JSON snippets below. A strict client-side config parser may
reject an unrecognized key inside `mcpServers`/`opencode.json` entries, so
adding an undocumented field there would trade a documentation problem for
a real compatibility risk. Version visibility is instead surfaced at the
manifest/API level (`LaunchManifest.router_version`) and in this document.

To check the installed router version directly:

```bash
python3 -c "import bruriah; print(bruriah.__version__)"
```

The dedicated `bruriah --version` flag used above is that user-facing
counterpart. An earlier revision of this paragraph tracked it as a
follow-up blocked on a `cli.py` freeze; the freeze ended, the flag
shipped, and `test_it_reports_its_own_version_without_a_subcommand`
pins it.

## Activation matrix

The server tells an agent when to consult Bruriah in two ways. Its initialize result carries MCP
`instructions`, and both tools carry `_meta` `anthropic/alwaysLoad`, so a client that honors it
loads their full descriptions at session start. The server sends both to every client. What a
client does with them is up to the client.

The rule for a change that departs from a recorded decision is in both places. The instructions
say to open a decision with `read_evidence` before contradicting it, and to say so when a change
departs from it. The `investigate_work` description says the same and asks for the decision's
commit in the final message. It is repeated there because a client can show the model the
description and not the instructions: Antigravity writes the instructions to a file, which Gemini
opened in 2 of 15 measured runs.

- **emitted**: the server sends the activation to this client. CI checks it against the built
  wheel over stdio (`scripts/verify_activation.py`).
- **delivered**: the client puts the instructions in front of its model and loads both tool
  descriptions up front.
- **consult measured**: a benchmark measured unprompted agents in this client calling Bruriah.

| client | emitted | delivered | consult measured |
|---|---|---|---|
| Claude Code | yes | yes, on Claude Code 2.1.292: no treatment run needed tool search to find Bruriah | 15/15 runs with the activation, 0/15 without, `claude-fable-5-1`, 2026-10-07 ([evals/agent_regression/README.md](../evals/agent_regression/README.md)) |
| OpenCode | yes | unknown | not measured |
| Cursor | yes | unknown | not measured |
| Gemini CLI | yes | unknown | not measured |
| Antigravity | yes | no, on agy 1.3.6: the instructions and both tool schemas are files the agent opens on demand, so `anthropic/alwaysLoad` has no effect | 14/15 unprompted runs called Bruriah, `gemini-3.8-flash-high`, 2026-10-15; the baseline had no Bruriah registered, and silent regressions did not drop: 9/10 in both conditions ([results](../evals/agent_regression/results/antigravity-2026-10-15/README.md)) |
| Generic stdio MCP client | yes | unknown | not measured |

"unknown" means nobody has checked; it is not a known failure. In those clients, an agent may
still need to be told to use Bruriah, for example with a line in the project's agent instructions.

## Claude Code

- **Config file**: `.mcp.json` at the project root, or the equivalent
  user-scope entry created by `claude mcp add --scope user`.
- **Structured output**: detected -- Claude Code consumes
  `structuredContent` directly. The canonical JSON text fallback is
  still returned and still valid if that ever changes.

```json
{
  "mcpServers": {
    "bruriah": {
      "command": "/usr/local/bin/bruriah",
      "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
      "env": {}
    }
  }
}
```

## OpenCode

- **Config file**: `opencode.json` at the project root, or
  `~/.config/opencode/opencode.json` for a global entry.
- **Structured output**: degraded (not documented as consumed) -- core
  behavior (`tools/list`, `tools/call`, text fallback) is unaffected.
- **Shape note**: OpenCode's local MCP entries combine the executable
  and its arguments into a single `command` array, and use the key
  `environment` rather than `env`. This is a real shape difference, not
  an inconsistency in the manifest -- the launched argv is identical to
  every other client's.

```json
{
  "mcp": {
    "bruriah": {
      "type": "local",
      "command": [
        "/usr/local/bin/bruriah",
        "serve",
        "--config-dir",
        "/home/u/.config/cerebro"
      ],
      "environment": {}
    }
  }
}
```

## Cursor

- **Config file**: `.cursor/mcp.json` (project) or `~/.cursor/mcp.json`
  (global).
- **Structured output**: detected -- Cursor follows the same
  `mcpServers` convention as Claude Code.

```json
{
  "mcpServers": {
    "bruriah": {
      "command": "/usr/local/bin/bruriah",
      "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
      "env": {}
    }
  }
}
```

## Gemini CLI

- **Config file**: `~/.gemini/settings.json`, or a project-scoped
  `.gemini/settings.json`.
- **Structured output**: detected -- Gemini CLI adopted the
  `mcpServers` convention.

```json
{
  "mcpServers": {
    "bruriah": {
      "command": "/usr/local/bin/bruriah",
      "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
      "env": {}
    }
  }
}
```

## Antigravity

- **Config file**: best-effort `~/.antigravity/mcp_config.json`.
- **Structured output**: degraded (unverified).
- **Confidence note**: Antigravity's exact on-disk config path and
  schema could not be verified from first principles at authoring
  time. The snippet below is the standard `mcpServers` shape rendered
  as a best-effort starting point, not a verified claim. Confirm the
  real path and any schema differences against current Antigravity
  documentation before relying on this snippet in production. This is
  a deliberate, documented gap per this slice's non-goals (never guess
  silently).

```json
{
  "mcpServers": {
    "bruriah": {
      "command": "/usr/local/bin/bruriah",
      "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
      "env": {}
    }
  }
}
```

## Generic stdio MCP client

- **Config file**: host-specific -- consult that host's own MCP
  documentation for where to place this.
- **Structured output**: degraded (assume the minimum) -- assume only
  `tools/list` and `tools/call`, the core the spec requires
  (`A-Cross-Client Core Equivalence`). The canonical JSON text fallback
  is required reading for any host that does not surface
  `structuredContent`.
- **Shape note**: no vendor wrapper -- this is the manifest itself,
  which any spec-compliant host can adapt into its own config schema.

```json
{
  "args": ["serve", "--config-dir", "/home/u/.config/cerebro"],
  "command": "/usr/local/bin/bruriah",
  "env": {}
}
```

## What never changes across clients

- The launched command and arguments are byte-identical across all six
  renderers (only the wrapping JSON key/shape differs).
- The server always exposes exactly two tools -- `investigate_work` and
  `read_evidence` -- with strict, closed input/output schemas.
- The server always returns both `structuredContent` and the canonical
  JSON text fallback, regardless of whether the client is known to
  consume the former. A client that sanitizes or drops schema keywords
  still works correctly via the text fallback.
- Diagnostics are stderr-only; stdout carries only the JSON-RPC
  protocol stream.
- No client config may redefine the request/response schemas or the
  router's safety policy (network-off by default, read-only, no
  install/execute/authenticate).

## Non-goals of this document

This guidance renders launch configuration only. It does not perform
cutover, does not modify any existing legacy MCP registration, and does
not claim any client/platform pair has passed the Phase 12 evaluation
gates (`K-Cross-Domain, Client, Platform, Security, and Utility Gates`).
Those matrix qualifications are separate, later work.
