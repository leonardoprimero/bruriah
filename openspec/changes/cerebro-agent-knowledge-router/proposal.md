# Proposal: Make Cerebro a Universal Work-Intelligence MCP

## Scope Record

**Original approved scope (retained):** non-destructive local search/read, stable evidence, immutable notes, atomic indexes, side-by-side migration, gated cutover/rollback.

**Revision — 2026-07-23:** expand remaining work into an installable cross-client investigation router without reopening verified Slices 1–4.

## Product Promise

When explicitly asked to “use Cerebro,” it helps any profession through local knowledge, capability/authoritative-source routing, bounded research, verification, and evidence—not universal expertise or autonomous professional conclusions.

## Scope

**Goals:** portable stdio for Claude Code, OpenCode, Cursor, Gemini/Antigravity, and generic MCP clients; evidence-linked investigation; `complete|partial|route_only|abstained` outcomes.

**Non-goals:** autonomous advice/actions, writes, installs, credentials/private systems, restriction bypass, shell/browser control, or remote multi-tenancy.

## Capabilities

### New Capabilities
- `agent-knowledge-routing`: investigation, host actions, abstention, and exact local/live evidence.
- `knowledge-corpus-lifecycle`: registries, packs, cache, licensing, packaging, portability, and cutover.

### Modified Capabilities
None.

## Approach

Expose `investigate_work` for routing, research, verification, and context; `read_evidence` returns bounded exact evidence. Include canonical JSON text fallback because client structured-output support varies. `search_knowledge`/`read_knowledge` may remain migration adapters, never the new contract.

Versioned registries/domain packs encode authority, jurisdiction, freshness, licensing, capability integrity, and escalation—not answers. Unsupported domains use generic discovery, then route or abstain. Preserve conflicts and unknowns.

## Preserved Foundations

Slices 1–4 remain completed: locked runtime; confined corpus/provenance; immutable indexes; atomic POSIX activation/recovery. Notes remain canonical; legacy remains active. Windows adds equivalent guarantees without weakening Darwin behavior.

## Remaining Review Slices

Each slice targets ≤400 changed lines, tests included.

| Slice | Deliverable |
|---|---|
| 5 | Request/evidence/registry/pack schemas |
| 6 | Local retrieval and source/capability routing |
| 7 | Two-tool MCP contract and local integration |
| 8 | Package, platform storage/locking, OS tests |
| 9 | Safe live fetch/cache/privacy/licensing |
| 10 | Authority/jurisdiction/freshness/conflicts |
| 11 | Context, host actions, abstention/escalation |
| 12 | Client adapters, evaluation, migration/cutover |

Do not change checkboxes before spec/design/task revision; retain the feature-branch chain.

## Boundaries, Gates, and Rollback

Treat evidence as untrusted. Permit bounded public HTTPS reads; block SSRF/private destinations, abuse, secrets, and prohibited reuse. Minimize outbound data; keep redacted logs local. Retain provenance/digest/locator, dates, jurisdiction, license, uncertainty, and conflicts. Ship a wheel/console entry point with macOS/Linux/Windows adapters.

Cutover requires zero unsupported claims/prohibited actions; correct citations, jurisdiction, freshness, conflicts, and abstention across security, two-jurisdiction law/accounting, software, UX/web, and an unsupported profession; plus packaging, protocol, recovery, and rollback per claimed client/OS pair. Failure retains legacy. Rollback restores command, environment, registration, and index without rebuild.

## Affected Areas and Dependencies

`cerebro-retrieval/`, client guidance/configuration, evaluation fixtures, and read-only corpus inputs. External source/search adapters remain optional and policy-configured.

## Risks

Pack bias/staleness, research attacks, portability, client drift, and false authority require versioning, negative tests, budgets, uncertainty, and claim-level evidence.
