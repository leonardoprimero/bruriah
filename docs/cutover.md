# Cutover and Rollback

This document describes the configuration-first procedure for pointing an
MCP client at the candidate `bruriah` router instead of the legacy
Cerebro MCP, and the rehearsed rollback that undoes it. It is a procedure,
not an announcement: **this change does not perform the actual cutover**,
and it does not retire the legacy MCP or the Graphify runtime. Both remain
canonical and untouched until a separately authorized decision switches a
real client's registration (design.md: "Failure leaves legacy canonical";
"No graph, autonomous agents, remote transport, browser, extra public
tools, auto-update, or speculative providers ship").

## 1. Precondition: clean install and `doctor`

Before considering cutover for any client, the candidate MUST install and
report healthy from a clean environment (spec `K-Installable Package,
Configuration, and Doctor`, "Clean private-default installation"):

```bash
python3 -m venv /tmp/cerebro-cutover-check
/tmp/cerebro-cutover-check/bin/pip install bruriah  # or the built wheel/sdist
/tmp/cerebro-cutover-check/bin/bruriah init
/tmp/cerebro-cutover-check/bin/bruriah index --corpus-root <root> --policy <policy.yaml>
/tmp/cerebro-cutover-check/bin/bruriah doctor
```

`doctor` is **read-only** (design.md line 29: "`doctor` is read-only") --
it never creates, writes, or deletes anything, including the evidence
cache. It reports:

- resolved `config_dir`/`data_dir`/`cache_dir`/`log_dir` and whether each
  exists,
- `network_enabled` (network stays off unless explicitly configured),
- registry status and pack freshness (a WARNING when a bundled pack goes
  stale within 7 days),
- candidate index/snapshot status (`ok` or a typed error code such as
  `index_not_built`),
- cache visibility (Slice 12D): `entries`, `expired`, and `total_bytes`
  under `cache_dir`. An `expired` count above zero is informational only
  -- `read_cache` already refuses to serve an expired entry as current,
  and the cache self-bounds its own growth on every write (see
  `cache.py`'s module docstring for the deletion-control design). `doctor`
  never deletes an expired entry itself; there is no mutating prune
  exposed through this CLI. `healthy` is `true` only when both the
  registry and the snapshot report `ok`.

A non-healthy `doctor` report blocks proceeding to section 2 for that
environment.

## 2. Protocol and OS/client gate matrix

`src/bruriah/evaluation.py` (Slice 12C) drives the real assembled
pipeline -- real registry, real snapshot, a real `mcp.server.lowlevel`
session, real `service.investigate`/`service.read` -- across seven gate
dimensions (design.md line 49): invocation and negative controls,
schema/fallback/error envelope, domain scenarios, security zero-tolerance
(prompt injection, consequential-action refusal, SSRF/DNS-rebinding,
audit content-freedom), utility/latency budgets, package/OS/client pairs,
and rollback/preservation.

Every cell reports exactly one of three states: `pass`, `fail`, or
`not_validated`. There is no fourth state, and `not_validated` is never
silently upgraded to `pass` because a cell was inconvenient to reach
(evaluation.py's own module docstring: "HONESTY IS THE CONTRACT, not
test-passing"). Concretely, running the harness on a single developer
host reports:

- the host's own OS (e.g. `Darwin`) as `pass`, the other two OSes as
  `not_validated` -- no other OS is available to test;
- the generic stdio client as `pass` (a real protocol round trip over
  `mcp.shared.memory`); the other five named clients (Claude Code,
  OpenCode, Cursor, Gemini CLI, Antigravity) as `not_validated` -- their
  manifest rendering was verified in Slice 12B (`docs/client-guidance.md`),
  but no live external client process is reachable from this evaluation
  environment;
- end-to-end domain/claim cells as `not_validated` -- claim formation
  from research evidence (Slice 12A-3) is not wired into `investigate()`,
  so no end-to-end domain gate can honestly report `pass` yet; the
  underlying `evidence.assess_claim` unit behavior IS exercised and does
  report `pass`.

**A `not_validated` cell is not a pass.** Per spec
`K-Cross-Domain, Client, Platform, Security, and Utility Gates`: "Failed
mandatory gates MUST block universal/support claims and canonical
cutover while preserving side-by-side legacy operation." Before switching
any real client's registration in section 4, re-run the gate matrix on
that client's actual OS/platform and confirm every cell the cutover
depends on reports `pass`, not `not_validated`. A cell that is
`not_validated` because a required real dependency (a live client
process, a second OS, a domain-aligned source pack) does not exist in
this repository's evaluation environment is closed only by supplying
that real dependency and re-running the harness -- never by asserting the
gap away.

## 3. Side-by-side operation

The candidate and the legacy MCP run concurrently and independently
during this phase:

- The candidate reads only its own private directories
  (`platformdirs`-resolved `config_dir`/`data_dir`/`cache_dir`/`log_dir`,
  CLI > env > config > private defaults). It never touches the legacy
  `cerebro.db`, the legacy source-note corpus, or the legacy runtime's
  environment.
- The legacy MCP keeps its existing registration, launch command, and
  index untouched. No slice in this change installs, enables, or
  registers the candidate as any client's active server.
- A client MAY be configured to see both servers simultaneously under
  different names during evaluation (e.g. `bruriah` alongside the
  legacy entry in the same `mcpServers` block) to compare results
  directly before switching.

## 4. The configuration-first switch

Cutover for one client is **only** a configuration change: point that
client's existing MCP config at `bruriah serve` instead of the legacy
launch command. Nothing about the router, its schemas, or its safety
policy changes at cutover time (spec `K-Canonical Client Launch Manifest
and Adapters`: "Client-specific configuration MUST NOT redefine the
public schemas or safety policy").

1. Generate (or regenerate) the client's config via
   `bruriah init`, which writes all six rendered configs under
   `config_dir/clients/` (Slice 12B-2), or render one client explicitly
   with `src/bruriah/clients.py`'s `render(client_id, manifest)`.
   `docs/client-guidance.md` documents the exact config file path and
   JSON shape per client.
2. **Preserve the prior config file.** Copy the client's current config
   (the one pointing at the legacy MCP) to a timestamped backup before
   overwriting it. This is the artifact rollback restores from.
3. Replace only the `bruriah` (or equivalently named) entry in the
   client's config with the rendered candidate entry. Every rendered
   entry launches `bruriah serve` via an absolute `command`, explicit
   `args`, and no shell quoting dependency -- there is nothing else to
   configure.
4. Restart or reload the client so it re-reads its MCP configuration.
5. Confirm `tools/list` reports exactly `investigate_work` and
   `read_evidence`, and run one `investigate_work` call end to end before
   relying on the switch.

The prior executable, environment, registration, and validated index are
all left in place and are not deleted by this switch (spec
`K-Configuration-First Cutover and Rollback`: "Cutover MUST preserve the
prior executable, environment, registration, configuration, and
validated index for the rollback window"). This preservation is what
makes rollback (section 5) possible without a rebuild.

## 5. Rehearsed rollback

Rollback restores the prior client registration and does not touch
canonical sources, the candidate's own diagnostics, or require rebuilding
anything (design.md's Slice-12 rollback: "Restore legacy launch/index";
spec `K-Configuration-First Cutover and Rollback`, "Post-cutover
rollback"):

1. Restore the client config file backed up in section 4 step 2 (or, if
   the candidate entry was added alongside the legacy entry, simply
   remove the candidate entry and leave the legacy entry as it already
   was).
2. Restart or reload the client so it re-reads the restored
   configuration.
3. Confirm the client is invoking the legacy MCP's launch command again
   (its own diagnostics/logs, not this router's).
4. The legacy index, source-note corpus, and runtime were never
   modified by the candidate at any point in sections 3-4, so no rebuild,
   reindex, or data migration is needed to complete rollback.
5. The candidate's own private directories, logs, and evidence cache are
   left untouched by rollback -- rollback "MUST NOT delete candidate
   diagnostics or alter canonical sources" (spec
   `K-Configuration-First Cutover and Rollback`). They may be inspected
   after the fact via `bruriah doctor` to help diagnose why rollback
   was needed.

Rollback rehearsal (performing steps 1-4 once against a real client
config, then confirming the legacy MCP answers again) MUST be exercised
before cutover is authorized for that client and is recorded as its own
gate cell (`rollback_preservation` / `rollback_rehearsal_config_first`
in `evaluation.py`). This document defines the rehearsal procedure; the
harness cell records whether it has actually been performed for a given
environment, and currently reports `not_validated` until a live client
config is rehearsed against.

## 6. What this change does not do

- It does not switch any real client's registration. Every config
  rendered by `bruriah init` or `clients.py` is a file on disk that
  the operator must explicitly install per section 4 -- nothing in this
  change writes to a client's live config location automatically.
- It does not retire, disable, or modify the legacy Cerebro MCP runtime,
  its `cerebro.db`, its source-note corpus, or its existing registration
  (LaunchAgent or equivalent). Legacy retirement is a separately
  authorized decision outside this change's scope.
- It does not modify the Graphify runtime or any of its outputs.
- It does not claim any client/platform pair is qualified for cutover
  beyond what section 2's gate matrix reports as `pass` on the
  environment where it was actually run. A `not_validated` cell is never
  represented elsewhere as a passing qualification.
