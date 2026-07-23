# Tasks: Rebuild Cerebro as an Agent Knowledge Router

## Review Workload Forecast

| Field | Value |
|---|---|
| Estimated changed lines | 2,992–3,542 across 5A–12; each review unit ≤400 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | 5A → 5B → Slices 6–12; one child PR per review unit |
| Delivery strategy | ask-always (resolved 2026-07-23) |
| Chain strategy | feature-branch-chain (approved) |
| High-risk points | Signed-pack trust; SSRF/DNS rebinding; Windows identity/durability; schema-sanitizing clients; evidence-gated cutover |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: feature-branch-chain
400-line budget risk: High

PR #5A base = feature/tracker branch; PR #5B base = PR #5A branch; each later PR base = the immediately preceding slice branch. Retarget/rebase any polluted child diff. Each estimate counts code, tests, docs, and configuration; split the unit before apply if actual additions plus deletions would exceed 400. Decision status: feature-branch-chain approved; no size exception.

### Suggested Work Units

| Slice | Goal | Dependency | Estimate |
|---|---|---|---:|
| 5A | Existing closed contracts, deterministic registries, Ed25519 signed JSON packs, tests/fixtures; independently verify former 5.1 boundary | 1–4 | ~392 existing |
| 5B | Complete budgets, evidence/read fields, and closed safe YAML/JSON loading with tests | 5A | 275–375 |
| 6 | Local retrieval and routing | 5B | 325–400 |
| 7 | Two-tool local MCP | 6 | 325–400 |
| 8 | Packaging, platform lifecycle, CLI | 4, 7 | 350–400 |
| 9 | Safe fetch, cache, audit | 5, 8 | 350–400 |
| 10 | Evidence and claim verification | 6, 9 | 325–400 |
| 11 | Context, host actions, escalation | 7, 10 | 300–375 |
| 12 | Client adapters, evaluation, cutover | 8, 11 | 350–400 |

Mappings use exact revised requirement/scenario titles: `A` = agent-routing spec, `K` = lifecycle spec, and `D` = revised design boundary/decision.

## Mandatory Controls for Every New Slice

- Start with failing characterization/contract/adversarial tests where feasible, then implement only that slice. Run `uv lock --check`, focused tests, `uv run pytest -q`, `uv run python -m pip_audit`, AST parsing, and `git diff --check`; record unavailable OS/client cells as **NOT VALIDATED**, never PASS.
- Before and after, verify 370-note corpus manifest `8cbcb107d12817e9a6f9d5122e32ef0d2043c138ac37c3868f1cde0923d42745`; live DB `03e9f3c59baeab23ec2eb74dfbc3dae38f73774af09c4b227ea3bb662553c10d` (34,770,944 bytes; 6,574 chunk/FTS/vector rows; 384 dimensions); legacy runtime `a05a8c25c24c9cae0cddfe54bbf1682057f6c48973102ee629b60e8dd3e7e661`; `reindex.sh` `8beb3dc04e19ab2c2114b0d1089840d8520446a8991b1ad7e246aed930a5ff95`; `uv.lock` `4e40f608b3c1625ae6b69ea7e18c80ed3f0857bf8d3d766b259a91c876dc2f87`; LaunchAgent `2421b8a42ca0c79c81498393a157e1d98a4de5d0f2fdcfc560f1bdb08f3efcd5`. The corpus and DB values are the approved 2026-07-23 incident rebaseline; `cerebro-retrieval/recovery/legacy-baseline-v1.json` is authoritative and `scripts/verify_legacy_baseline.py` is the canonical check (it also validates the model snapshot, runtime versions, and seven bilingual golden queries). Superseded pre-incident values — corpus `fda45d6d…`, DB `d59066f4…` — MUST NOT be used. Lock changes require explicit slice scope and a newly recorded expected hash.
- Legacy command, environment, registration, index, Graphify, source notes, and unrelated dirty files remain active/untouched. No slice cuts over, installs/enables capabilities, writes source evidence, adds remote transport/browser/shell/autonomous agents, or exposes public tools beyond the approved two.

## Phase 1: Reproducible Safety Baseline

- [x] 1.1 Add `cerebro-retrieval/{pyproject.toml,uv.lock}` and `tests/test_legacy.py` with pytest, advisory, and clean-room checks; preserve legacy assets/dirty files. Verify legacy MCP plus source manifest; rollback removes new files only. [K-Reproducible Runtime, K-Pre-Cutover; D-Runtime]

## Phase 2: Corpus Evidence Model

- [x] 2.1 Add `corpus-policy.yaml`, `src/cerebro_router/{models,corpus}.py`, `ref-aliases.json`, and corpus tests for confinement, raw lines, stable refs, provenance, renames, and tombstones. Verify rebuild stability/private-symlink rejection; rollback leaves sources untouched. [K-Preservation, K-Policy, K-Identity; D-Ingestion/identity]

## Phase 3: Immutable Candidate Index

- [x] 3.1 Add `src/cerebro_router/{index,cli}.py` candidate schema, metadata, manifest, model identity, incremental reuse, and `tests/test_index.py`. Verify failures never touch live assets; rollback deletes candidate only. [K-Atomic Promotion, K-Compatibility; D-Index lifecycle, D-Compatibility]

## Phase 4: Atomic Activation and Recovery

- [x] 4.1 Extend `src/cerebro_router/index.py` with validation, atomic `active.json`, request snapshots, read-only opens, retention, recovery, and concurrency tests. Verify failed promotion/uninterrupted reads; rollback restores the prior pointer without rebuild. [K-Atomic Promotion, K-Compatibility; D-Index lifecycle]

## Superseded Remaining Plan — 2026-07-23

The unimplemented old Tasks 5.1–8.1—**Hybrid Retrieval and Goldens**, **Search MCP Contract**, **Exact Read and Trust Boundary**, and **Side-by-Side Adoption and Readiness**—are removed from the executable checklist. Their local-router intent is traceable in repository history and the dated exploration addendum; revised Slices 5–12 below replace them. No removed task was completed or authorized.

## Phase 5: Contracts, Registries, and Signed Packs (former 5.1)

- [x] 5A Independently verify the existing `contracts.py`, deterministic `registries.py`, Ed25519 signed-JSON `packs.py`, `tests/{test_contracts,test_packs}.py`, and fixtures as a closed review boundary. Acceptance/verification: all nested schemas reject extras; registry order is stable; unknown, expired, incompatible, tampered, and unauthorized unsigned packs fail closed; the existing 49 tests plus full suite, lock, advisory, AST, diff, and mandatory-hash checks pass. Estimate: ~392 existing changed lines.
  Preservation/rollback: begin from verified Slices 1–4, change no canonical/live/legacy/Graphify/registration assets, and remove only 5A modules/tests/fixtures if reverted. No retrieval, MCP, network, conclusions, checkbox completion, or cutover. [former 5.1; A-Portable Two-Tool Public Contract/Strict schema and fallback; K-Versioned Source Registries and Domain Packs; D-Models, D-Packs]
- [x] 5B Extend `contracts.py`, `registries.py`, and `packs.py` with the missing six `Budgets` ceilings, fifteen `EvidenceRecord` provenance fields, eight `ReadItem` evidence-state fields, and closed safe YAML/JSON loading; keep focused tests/fixtures with the behavior. Acceptance/verification: exact fields and bounds serialize into closed schemas; safe loaders reject unknown structure, invalid types, and unsafe YAML while valid JSON/YAML is deterministic; focused tests, 5A regression tests, full suite, lock, advisory, AST, diff, and mandatory-hash checks pass. Estimate: 275–375 changed lines.
  Preservation/rollback: preserve the independently reviewable 5A boundary and every mandatory asset/hash; revert only 5B additions/tests without weakening 5A. No retrieval, MCP, network, conclusions, checkbox completion, or cutover. [former 5.1; A-Portable Two-Tool Public Contract/Strict schema and fallback; A-Evidence Normalization and Claim State/Conflicting current sources; K-Versioned Source Registries and Domain Packs; D-Models, D-Packs]

## Phase 6: Local Retrieval and Routing

- [ ] 6.1 Tests first: add `evals/local-routing.jsonl` and `tests/test_routing.py`; implement `retrieval.py` and `routing.py` for snapshot BM25/vector/RRF ranks, intent/domain/claim/risk/jurisdiction classification, source/capability lookup, and generic route/abstain. Verify focused tests plus bilingual exact/broad, rank-not-confidence, fabricated/popular/tampered capability, wrong-jurisdiction, unsupported-domain, mutation, budget, and degradation cases; apply mandatory hashes. Rollback disables/removes router only. Non-goals: live fetch, verification conclusions, public tools. [A-Local Knowledge and Capability Discovery/Method and tool discovery; A-Domain-Sensitive Outcomes and Unsupported-Domain Abstention/Arbitrary unsupported profession; A-Bounded Investigation, Pagination, and Stable References/Budget exhaustion; D-Routing/retrieval]

## Phase 7: Two-Tool Local MCP

- [ ] 7.1 Tests first: add `tests/test_mcp_contract.py`; implement `service.py` and `mcp_server.py` exposing only `investigate_work` and `read_evidence`, authoritative validation, `outputSchema`, request-bound cursors/budgets, exact local reads, stderr diagnostics, and canonical JSON text fallback. Verify focused tests and real stdio `initialize`, `tools/list`, valid/invalid `tools/call`, fallback equality, injection inertness, timeout, shutdown, and legacy continuity; apply mandatory hashes. Rollback unregisters candidate/local service. Non-goals: network, client cutover, extra tools. [A-Portable Two-Tool Public Contract/Strict schema and fallback; A-Exact Bounded Evidence Reading/Exact read with continuation, Stale or ineligible ref; A-Instruction and Evidence Separation/Retrieved prompt injection; A-Future Surface Extension Gate/Proposed specialized tool; D-Models, D-Clients]

## Phase 8: Packaging, Platform Lifecycle, and CLI

- [ ] 8.1 Tests first: add `tests/test_platform.py` and OS qualification harness; update `pyproject.toml`/lock and implement `platform.py`, package data, and `cerebro-mcp {init,serve,index,doctor}` in `cli.py` using native private directories and CLI > env > config > defaults. Verify wheel/sdist clean install and real macOS, Linux, Windows writer/crash/race/path-substitution/recovery cells; POSIX keeps descriptor/no-follow, Windows uses held no-delete/no-write-sharing identity checks. Mark unrun platforms NOT VALIDATED; apply mandatory hashes except approved lock rebaseline. Rollback restores repository runtime. Non-goals: network-on defaults, repair, registration/cutover. [K-Portable Filesystem, Locking, and Promotion/Concurrent promotion and crash on each OS; K-Installable Package, Configuration, and Doctor/Clean private-default installation; K-Preserved Canonical Sources and Verified Foundations/Expanded lifecycle preserves history; D-Portability/CLI]

## Phase 9: Safe Fetch, Cache, and Audit

- [ ] 9.1 Tests first: add `tests/test_fetch.py`; implement `research.py`, `fetch.py`, and private atomic cache/audit storage for policy-opt-in HTTPS `GET/HEAD`, validated-IP TLS connections, redirect revalidation, hard budgets, permitted excerpts, TTL/deletion, licensing, and content-free redacted audit. Verify DNS rebinding, private/link-local/metadata redirects, userinfo/ambient-secret stripping, proxy policy, decompression/MIME/timeout/concurrency limits, prompt injection, unknown/prohibited reuse, and network-off behavior; apply mandatory hashes. Rollback disables network and removes cache generation. Non-goals: browser/login/paywall/search-provider speculation/private evidence sharing. [A-Safe Bounded Live Research/DNS or redirect SSRF attempt, Oversized or prohibited content; A-Instruction and Evidence Separation/Retrieved prompt injection; K-External Evidence and Cache Lifecycle/Cache expiry or prohibited reuse; D-Research, D-Network]

## Phase 10: Evidence and Claim Verification

- [ ] 10.1 Tests first: add `tests/test_evidence.py` and replay fixtures; implement `evidence.py` typed untrusted envelopes and claim ledger for source/extract/assessment separation, authority rationale, jurisdiction, effective/freshness state, corroboration, conflict classes, uncertainty, and `supported|conflicted|insufficient|unknown`. Verify two-jurisdiction law/accounting, security authorization, versioned software, UX standards/research, stale/superseded/conflicting/fabricated authority, unknown metadata, citation/digest integrity, and zero instruction execution; apply mandatory hashes. Rollback returns cited raw evidence without assessments. Non-goals: consensus invention, professional advice, context assembly. [A-Evidence Normalization and Claim State/Conflicting current sources; A-Domain-Sensitive Outcomes and Unsupported-Domain Abstention/all six domain scenarios; D-Evidence]

## Phase 11: Context, Host Actions, and Escalation

- [ ] 11.1 Tests first: add `tests/test_context.py`; implement bounded context assembly and vendor-neutral `web_search`, `fetch_public_url`, `inspect_capability`, `request_jurisdiction`, and `consult_professional` actions with `complete|partial|route_only|abstained`, named gaps/degradation, safety notes, and professional escalation. Verify unavailable network/host tools, missing jurisdiction/date/permission, unsafe/consequential requests, unsupported professions, output-budget compaction retaining refs/conflicts/warnings, and zero writes/installs/execution; apply mandatory hashes. Rollback forces route-only mode. Non-goals: hidden chaining, host-tool execution, unsupported conclusions. [A-Bounded Investigation, Pagination, and Stable References/Budget exhaustion; A-Read-Only Informational Boundary and Host Actions/Consequential action requested; A-Domain-Sensitive Outcomes and Unsupported-Domain Abstention/Supported regulated domain lacks context, Arbitrary unsupported profession; D-Research, D-Evidence]

## Phase 12: Client Adapters, Evaluation, Migration, and Cutover

- [ ] 12.1 Tests first: add canonical launch manifest, `src/cerebro_router/clients.py`, `tests/test_clients.py`, evaluation fixtures/harness, and `docs/{client-guidance,cutover}.md`; render Claude Code, OpenCode, Cursor, Gemini, Antigravity, and generic stdio configs, then evaluate invocation/negative controls, schemas/fallback/errors, domains, security zero-tolerance, utility/latency, package/OS cells, rollback, and preservation. Qualify real available clients/platforms and label all unavailable cells NOT VALIDATED; failed cells block only claims and canonical cutover. Rehearse side-by-side configuration-first rollback before any separately authorized switch; apply mandatory hashes. Rollback restores prior command/environment/registration/index without rebuild. Non-goals: silent/automatic cutover, legacy/Graphify retirement, remote transport, unsupported support claims. [A-Deterministic Explicit Invocation and Negative Controls/Explicit invocation, Negative control; A-Cross-Client Core Equivalence/Minimal or schema-sanitizing client; K-Canonical Client Launch Manifest and Adapters/Client feature degradation; K-Cross-Domain, Client, Platform, Security, and Utility Gates/Domain, client, platform, or security failure, Utility regression; K-Configuration-First Cutover and Rollback/Post-cutover rollback; D-Clients]
