# Design: Universal Cerebro Work-Intelligence Router

## Revision History

| Date | Decision |
|---|---|
| 2026-07-23 (original) | Side-by-side FastMCP replacement; immutable evidence/indexes; strict contracts; atomic activation; two tools; legacy rollback. |
| 2026-07-23 (revision) | Preserve verified Slices 1–4; expand Slices 5–12 to portable work intelligence. `search_knowledge`/`read_knowledge` become temporary adapters. |

## Architecture

`investigate_work` composes tested stages; `read_evidence` only resolves immutable refs.

```text
MCP adapter -> intent/domain router -> local retriever + capability/source registries
                                      -> research planner -> safe fetch adapters
                                      -> evidence normalizer/verifier -> context assembler
host plan <---------------- unavailable/unsafe work ----------------------|
```

| Boundary | Decision |
|---|---|
| Models | `contracts.py`: every Pydantic model uses `ConfigDict(extra="forbid", strict=True)`. `InvestigationRequest`: `task` 1–4096, optional `outcome`, `jurisdiction`, `as_of`, `risk_class`, `network_policy`, `host_capabilities`, bounded `candidate_material`, `cursor`, `budgets`. `InvestigationResult`: `schema_version`, `complete|partial|route_only|abstained`, request ID, evidence, claims, conflicts, gaps, host actions, warnings, degradation, consumed budgets, cursor. `ReadRequest`: 1–10 unique refs, optional inclusive ranges/cursor/budgets; `ReadResult` has typed per-ref results. All nested JSON Schemas are closed. FastMCP publishes `outputSchema`; one validated object is returned as `structuredContent` and canonical RFC-8785-style JSON text. Server validation remains authoritative when clients sanitize schemas. |
| Routing/retrieval | `routing.py` classifies intent, domain, claim type, risk, and jurisdiction; it never concludes. `retrieval.py` reuses the verified snapshot for BM25/vector/RRF and returns ranks, not confidence. `registries.py` loads capabilities/sources; no install, execution, authentication, or popularity-as-integrity. |
| Packs | `packs.py` validates closed YAML/JSON: schema/pack ID/version, maintainer, compatibility, review/expiry, claim types, jurisdictions/languages, update cadence, license/provenance/signature; sources include publisher, contextual authority+rationale, temporal/citation/reuse rules, freshness, limitations, bias/conflicts/exclusions; capabilities include canonical distribution, version/advisories, integrity, permissions/network/data access. Bundled packs are Ed25519-signed through the release manifest and updated only by pinned package or explicit import. Unknown/expired/incompatible/unverified packs fail closed; local unsigned packs require explicit opt-in and cannot authorize regulated conclusions. Generic discovery only routes or abstains. |
| Research | `research.py` plans bounded work. With an enabled provider and admitted URL, `fetch.py` performs HTTPS `GET/HEAD`; otherwise it returns vendor-neutral `web_search`, `fetch_public_url`, `inspect_capability`, `request_jurisdiction`, or `consult_professional` actions. No hidden chaining. |
| Network | Parse/canonicalize URL; allow configured hosts/ports; strip userinfo, cookies, authorization and ambient credentials. Resolve through the configured proxy policy, reject loopback/private/link-local/multicast/reserved/metadata ranges, connect to a validated IP with hostname/TLS verification, and repeat DNS/IP checks on every redirect. Enforce redirects, concurrency, connect/read/total time, compressed/decompressed bytes, text/PDF/declared API MIME, and extraction limits. Atomic private cache stores permitted minimum excerpts plus digest, redirect chain, policy/pack versions, license and TTL; prohibited bodies are discarded. Audit records IDs, destination class, decisions, counts and timings—never query/body/secret content. |
| Evidence | `evidence.py` stores source text only inside typed `UNTRUSTED_EVIDENCE` envelopes; parsers cannot emit actions. Claims separately record extracted text, assessment, supporting/conflicting refs and `supported|conflicted|insufficient|unknown`. Authority is pack-contextual; freshness is date-rule state; conflicts are classified; uncertainty lists missing checks. Retrieval scores affect ordering only and are never truth confidence. Law/accounting require jurisdiction/effective regime and professional escalation; security separates public vulnerability evidence from authorization/incident judgment; unsupported domains return route-only/abstained. |
| Portability/CLI | Build wheel/sdist and `cerebro-mcp {init,serve,index,doctor}`. `platformdirs` resolves config/data/cache/logs; precedence is CLI > env > config > private defaults (network off). `init` creates user-private configuration and client snippets; `doctor` is read-only. `platform.py` keeps current descriptor/no-follow activation on Darwin/Linux. Windows uses a private, uniquely named generation plus a native handle opened without delete/write sharing while SQLite validates by path; identity is checked before/after pointer publication. This prevents replacement without `/dev/fd`. Cross-platform writer lock, atomic-or-recoverable pointer, explicit durability state, retention and startup recovery share one interface. |
| Clients | One manifest renders Claude Code, OpenCode, Cursor, Gemini, Antigravity and generic stdio snippets. Adapters detect declared structured output and host capabilities; core requires only `tools/list`/`tools/call`, always emits text fallback, stderr-only diagnostics, absolute commands, and no shell quoting. |

## Files and Verification

Preserve `models/corpus/index`, source notes, legacy runtime/database/registration, and Slice 4’s Darwin PASS. Tests ship with each autonomous feature-chain unit:

| Slice | Exact boundary | Rollback |
|---|---|---|
| 5 | `contracts`, registries, signed pack loader/fixtures | Remove new data/modules |
| 6 | Local retrieval, intent/domain/source/capability routing | Disable router |
| 7 | Two MCP tools, local-only service, fallback | Unregister candidate |
| 8 | Wheel, directories, platform lifecycle, four CLI commands | Restore repository runtime |
| 9 | Planner, SSRF-safe fetch, cache/audit | Network stays disabled |
| 10 | Evidence normalization and claim verification ledger | Return raw cited evidence only |
| 11 | Context assembly, host actions, refusal/escalation | Route-only mode |
| 12 | Client templates, matrix evaluation, migration/cutover | Restore legacy launch/index |

Each slice is ≤400 changed lines including tests; split before apply otherwise.

Cutover requires clean install/doctor, protocol and OS/client matrix, exact citations, domain authority/jurisdiction/freshness/conflict tests, zero injection/SSRF/secret/action/unsupported-claim failures, utility/latency budgets, and rehearsed configuration/index rollback. Failure leaves legacy canonical. No graph, autonomous agents, remote transport, browser, extra public tools, auto-update, or speculative providers ship.
