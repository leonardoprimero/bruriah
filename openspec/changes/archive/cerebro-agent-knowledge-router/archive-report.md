# Archive Report — cerebro-agent-knowledge-router

**Archived:** 2026-07-25
**Status:** COMPLETE — all 14 task checkboxes `[x]`; delta specs promoted to `openspec/specs/`.

## Outcome

Rebuilt Cerebro as a non-destructive, universal, cross-client **work-intelligence MCP** exposing exactly two public tools — `investigate_work` and `read_evidence` — over `mcp.server.lowlevel.Server` (authoritative closed-model validation; never FastMCP). The legacy MCP, corpus, index, model, registration, and Graphify were preserved untouched throughout; the preservation baseline (`scripts/verify_legacy_baseline.py`, lock `3c83d9eb…`) passed before and after every slice.

## Delivered (Phases 1–12)

| Phase | Result |
|---|---|
| 1–4 | Preserved/verified: reproducible baseline, corpus evidence model, immutable candidate index, atomic activation & recovery |
| 5A/5B | Closed contracts, deterministic registries, Ed25519 signed packs; budgets/evidence/read fields; safe YAML/JSON loaders |
| 6A/6B | Local BM25+vector+RRF retrieval; intent/domain/claim/risk/jurisdiction routing + abstention |
| 7 | Two-tool local MCP (`service.py` composition + `mcp_server.py`) |
| 8 | Packaging (wheel/sdist, `uv_build`), `platform.py` private dirs, `cerebro-mcp {init,serve,index,doctor}` CLI; lock rebaselined |
| 9A/9B | SSRF-safe HTTPS fetch core; research planner + private atomic cache (0600, TTL) + content-free audit |
| 10 | Evidence normalization + claim-state ledger (`supported\|conflicted\|insufficient\|unknown`) |
| 11 | Bounded context assembler + five vendor-neutral host actions + escalation |
| 12A-1/2 | Wired context + opt-in live research into the live `service.investigate()` pipeline (first authorized core un-freeze) |
| 12B/12B-2 | Six client launch adapters; `init` writes all six private configs |
| 12C | Evaluation gate-matrix harness (honest pass/fail/not_validated) |
| 12D | Cache deletion controls (self-bounding) + `docs/cutover.md` |

**Final test suite: 459 passing.** stdlib-only runtime additions; no dependency added since the 8B lock rebaseline.

## Deliberate deferrals (safety-/contract-preserving, NOT gaps)

1. **Live claim formation (was 12A-3).** `SourcePolicy` declares no URL/host match key and the bundled `research.minimal` pack's `domains=["software-research"]` matches no `classify.Domain`, so `lookup.sources` is always empty — there is nothing to match fetched evidence against. `evidence.py` is built, verified, and ready; it activates when a domain-aligned pack + a source-identity match contract exist (future pack authoring).
2. **Network activation in `platform.load_deps`.** `ResearchDeps.allowlist` (permitted research hosts) is declared nowhere; shipping a default allowlist would be an egress hole. Network stays OFF by default; enabling it requires an operator-defined allowlist contract.
3. **Robots directives** are honored via the operator `AccessPolicy` allowlist/denylist, not by live-fetching `robots.txt` (which would be hidden chaining for a single-URL non-crawler). `spec.md` amended accordingly.

## Not validated in this environment

- OpenCode and Antigravity on-disk config shapes (no live network) — rendered best-effort, flagged in `docs/client-guidance.md`.
- Linux/Windows and live-client cells — honestly `not_validated` in the 12C matrix, never fabricated as pass.
- End-to-end smoke test in a real client — the operator's step.

## Verification discipline

Every slice: implement → orchestrator probe → independent adversarial verification → close WARNINGs with named regression tests → commit. The recurring untyped-exception-escape defect class (10 occurrences total) was closed at every boundary. No dishonest `pass` in the evaluator (confirmed by an independent standalone matrix run). The commit arc (d460518 → b1ec47f) is preserved in git history.

## Promoted specs

- `openspec/specs/agent-knowledge-routing/spec.md`
- `openspec/specs/knowledge-corpus-lifecycle/spec.md`
