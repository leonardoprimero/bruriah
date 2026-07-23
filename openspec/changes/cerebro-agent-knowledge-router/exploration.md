## Exploration: Cerebro Agent Knowledge Router

### Current State

Cerebro is a valuable, actively maintained Obsidian knowledge corpus, but its agent-facing delivery layer is not yet a dependable knowledge router.

- `cerebro-retrieval/cerebro.py` is a single procedural Python service with one Spanish MCP tool, `buscar_en_cerebro`. It builds a local SQLite database, combines FTS5 BM25 and multilingual FastEmbed candidates with reciprocal-rank fusion, and returns plain-text results.
- Search results contain only a relative file, heading, opaque RRF score, retrieval channel, and a 280-character snippet. The service has no exact read/fetch operation, stable evidence identifier, source line range, citation URL, verification date, trust state, category contract, cursor, or explicit abstention/degradation signal.
- YAML frontmatter is deliberately stripped before chunking. Consequently, useful `type`, `tags`, `category`, `status`, `source`/`url`, `created`, `verificado`, and relationship metadata present in source notes cannot influence filtering, ranking, or result trust.
- Index row IDs are rebuild-order-dependent. Chunks split long sections by character offsets without preserving line ranges, and exact source evidence cannot be recovered through MCP.
- Paths, model, dimensions, and exclusions are hardcoded. Query length and `k` are unbounded. FTS exceptions and file-read failures are silently ignored. `iter_files()` does not resolve paths or prove root containment, so a Markdown symlink can escape the vault root.
- Reindexing removes the live database before the replacement is complete. A failed model download, parse, embedding, or write can therefore leave the active server without its prior index. The shell wrapper reduces duplicate writers but uses predictable `/tmp` paths and does not provide reader-safe cutover or rollback.
- There is no committed dependency manifest or lock, test runner, test suite, CI, linting, typing, retrieval evaluation set, or MCP client contract test. The active environment includes vulnerable `mcp 1.27.2` (fixed in 1.28.1) and vulnerable transitive versions. The known MCP WebSocket issue is not directly reachable through this stdio deployment, but the environment remains unreproducible and must be upgraded and locked.
- The Claude Cerebro skill has a strong trigger description and category map, but its workflow explicitly uses Grep, Glob, and direct reads. It therefore bypasses the MCP when the user explicitly asks to use Cerebro.
- A separately registered Graphify MCP exposes seven graph tools plus three unrelated GitHub PR tools. Its generated graph is dated 2026-06-19, includes `.obsidian` and `Archive` inputs, and is not the source of truth. The graph JSON declares `directed: false`; the installed server forcibly loads it as directed and uses successor/predecessor traversal, so stored orientation is not trustworthy. Public graph traversal should not be retained merely for compatibility.
- Current retrieval exclusions omit `Archive` but otherwise admit Markdown under personal and diary areas. Preservation and retrieval eligibility are different concerns: every source file must remain untouched, while default agent search needs an explicit, privacy-aware corpus policy.
- The authoritative assets are the source notes and their metadata. `cerebro.db` and `graphify-cerebro/graphify-out/` are generated artifacts and must remain replaceable derivatives.

### Affected Areas

- `cerebro-retrieval/cerebro.py` — current indexer, retrieval engine, formatter, CLI, and active MCP entry point; its observable behavior needs characterization before replacement.
- `cerebro-retrieval/reindex.sh` — destructive rebuild orchestration, hardcoded runtime paths, predictable temporary files, and missing atomic cutover/rollback.
- `cerebro-retrieval/` dependency metadata — a committed, hash-locked environment and supported Python/MCP versions are required.
- `Cerebro-IA/**/*.md` — immutable canonical corpus inputs whose content, frontmatter, citations, URLs, verification dates, and source line positions must be preserved. Knowledge gaps may be corrected only as separately verified corpus work, never as an indexing side effect.
- `~/.claude/skills/cerebro-ia/SKILL.md` — trigger and workflow must route explicit Cerebro requests through the MCP search/read sequence rather than filesystem tools.
- MCP client registration (currently user-level) — migration needs a side-by-side candidate registration, controlled cutover, and one-step rollback without disabling the active server prematurely.
- `graphify-cerebro/graphify-out/` and installed `graphify.serve` registration — stale generated inputs and overlapping tools require quarantine/consolidation; graph data may assist ranking internally only after correctness and benchmark gates.
- `openspec/` and a future test/evaluation area — contracts, fixtures, characterization tests, retrieval judgments, migration checks, and evidence-integrity tests are new first-class assets.

### Scope Boundaries

**In scope**

- A small, read-only, agent-oriented MCP surface centered on `search_knowledge` and `read_knowledge`.
- Evaluation of `assemble_work_context` as a bounded convenience tool; it is public only if it measurably improves broad-task completeness without duplicating the two-step surface.
- Metadata-preserving ingestion, explicit corpus policy, stable evidence IDs, exact bounded reads, line ranges, provenance/freshness/trust fields, multilingual hybrid retrieval, category diversification, and actionable error/degradation reporting.
- Characterization tests, unit/integration tests, MCP stdio contract tests, retrieval/security evaluations, dependency upgrade/locking, and operational observability on stderr.
- Side-by-side build, shadow evaluation, atomic index promotion, active-server continuity, backup retention, cutover, and rollback.
- Internal graph-derived expansion or relationship signals only when they remain traceable to source notes and beat a non-graph baseline.
- Updating the Cerebro skill and registration instructions so explicit “use Cerebro” requests deterministically invoke the MCP and read selected evidence.

**Out of scope**

- Destructive corpus cleanup, bulk note rewriting, frontmatter normalization in place, or replacing source notes with a database or graph.
- Agent-write, web-crawl, package-install, shell-execution, or automatic note-update MCP tools.
- Treating generated summaries, embeddings, inferred graph edges, or model output as authoritative evidence.
- Publishing graph traversal merely because a stale server already exposes it.
- Retaining Graphify's unrelated PR-management tools in the Cerebro product surface.
- Updating factual note content without a separate source-verification workflow and review unit.

### Approaches

1. **Parallel replacement with progressive evidence disclosure** — build a new versioned service and index beside the active implementation. Search returns compact, metadata-rich references; read returns bounded exact source evidence; optional context assembly composes the same primitives.
   - Pros: Preserves availability; enables characterization and shadow comparison; gives clean trust, security, and rollback boundaries; keeps the universal surface small; allows graph value to be tested rather than assumed.
   - Cons: Temporary duplication of runtime, index, registration, and migration logic; requires explicit compatibility and promotion criteria.
   - Effort: High

2. **Incremental in-place evolution of `cerebro.py`** — add metadata, read, validation, tests, and atomic rebuilding around the current file and database.
   - Pros: Lower initial code volume; reuses proven BM25/vector/RRF behavior and current registration.
   - Cons: Harder to isolate regressions; live and candidate schemas become coupled; rollback remains fragile during transition; the procedural file will accumulate unrelated responsibilities; encourages premature compatibility with weak IDs and output contracts.
   - Effort: Medium

3. **Graph-first unified MCP** — rebuild Graphify and make graph search/traversal the primary public interface, with lexical/vector retrieval as support.
   - Pros: Potentially useful for multi-hop discovery and global synthesis; existing generated assets demonstrate relationship extraction.
   - Cons: Current graph is stale, noisy, undirected, and loaded with fabricated direction; inferred edges can obscure source evidence; a broad tool surface reduces deterministic invocation; graph-only retrieval is weaker for exact evidence and arbitrary keyword/tool discovery; rebuild cost is high.
   - Effort: High

### Recommendation

Use **Approach 1: a parallel replacement with progressive evidence disclosure**.

The target should be one read-only MCP server with two mandatory tools:

1. `search_knowledge` accepts a bounded task/query plus optional category, trust/freshness, and pagination filters. It returns a compact structured list of stable evidence references with title, category, path, heading, line range, snippet, retrieval contributions, source URLs, verification date/status, and explicit warnings. Broad queries should diversify across relevant categories such as skills, MCP servers, security, tools, architecture/methods, prompts, and playbooks rather than returning eight near-duplicate chunks.
2. `read_knowledge` accepts stable evidence IDs and bounded ranges, validates canonical root confinement, then returns exact source text with frontmatter/provenance and line ranges. Retrieved note text must be clearly delimited as untrusted reference data, never instructions to the host agent.

Evaluate `assemble_work_context` behind an experiment gate. It may query/decompose internally, select complementary evidence, and return a bounded context kit, but it should be published only if agent-level evaluations show better task coverage or fewer tool round trips than search followed by read. It must return selected evidence IDs and cannot synthesize unsupported facts.

The replacement should separate five responsibilities even if implementation stays compact: corpus policy/parser, immutable evidence model, index builder/promoter, retrieval/ranking, and MCP adapter. The graph is an optional internal retriever or expansion signal behind the same evidence model. Source text always wins over graph-derived data.

Stable identity should be derived from canonical relative source identity plus a persisted section/chunk identity manifest, not SQLite row order. Reads must map every result back to immutable source line ranges. Renames need an explicit alias/migration map rather than silently changing citations.

### Migration and Rollback Strategy

1. Record a source manifest (relative path, size, hash) and characterize current CLI/MCP behavior without modifying the active database.
2. Add a reproducible locked environment with `mcp >= 1.28.1` and patched transitive dependencies; verify stdio startup and protocol output before introducing replacement behavior.
3. Build the candidate service and a versioned candidate index in a new location. Never delete or write the current `cerebro.db` during candidate builds.
4. Generate indexes as temporary, validated artifacts. Check schema, corpus manifest, source confinement, row/vector counts, evidence mappings, and representative queries; then atomically rename/promote only the candidate artifact.
5. Register the candidate under a temporary MCP name (for example, `cerebro-next`) while `cerebro` remains active. Run contract, retrieval, security, and explicit-invocation evaluations against both.
6. Update the Cerebro skill to call the candidate in a controlled test scope. Verify search → evidence selection → read → context-kit behavior across broad and exact tasks.
7. Cut over the canonical registration only after gates pass. Retain the previous registration command, executable environment, database, and a timestamped index backup for an agreed rollback window.
8. Rollback is configuration-first: restore the old `cerebro` command/arguments and previous index without rebuilding. Retire the stale Graphify registration only after the replacement proves equivalent or better discovery and rollback has been rehearsed.

### Measurable Success Criteria

- **Preservation:** 100% of pre-migration source files remain present and byte-identical unless separately approved corpus updates are listed; generated indexes never replace source notes.
- **Corpus policy:** 100% of indexed files resolve under approved roots; no symlink escape is accepted; personal, diary, archive, configuration, and generated areas have explicit tested inclusion/exclusion rules.
- **Evidence integrity:** 100% of evaluated hits and reads resolve to the correct relative path and exact source line range; frontmatter, citations, verification dates, and URLs round-trip without loss.
- **Retrieval quality:** On a reviewed multilingual benchmark spanning all product categories, target Recall@10 >= 0.90, MRR@10 >= 0.75, and >= 0.85 top-5 task success. Record the current engine baseline and require no category to regress materially.
- **Context-kit completeness:** For broad benchmark tasks, >= 0.90 of required relevant categories are represented when evidence exists, while redundant same-note chunks remain bounded.
- **Agent invocation:** In supported-client prompt tests, explicit “use Cerebro” instructions invoke Cerebro in 100% of cases; selected evidence is subsequently read before factual synthesis in 100% of citation-required cases.
- **Safety:** Query, result count, pagination, range, and output budgets are enforced; malicious note instructions remain delimited data; malformed input and unavailable lexical/vector/graph components produce explicit structured errors or warnings, never silent success.
- **Availability:** A failed build or validation leaves the prior active server and index queryable. Atomic promotion and configuration rollback are exercised in an integration test.
- **Operations:** Warm local p95 search latency is <= 1.5 seconds, p95 bounded reads are <= 100 ms, and response sizes stay within declared budgets on the reference machine; exact thresholds may be tightened after baseline measurement.
- **Reproducibility:** A clean environment installs from the committed lock, starts over stdio, rebuilds a candidate index, and passes all contract/evaluation checks without relying on the existing `.venv`.
- **Surface discipline:** Public graph traversal or `assemble_work_context` ships only with benchmark evidence of incremental value; otherwise the public surface remains two tools.

### Review-Size Slicing

The complete change will exceed the 400-line review budget. Chained PRs are recommended, and the `ask-always` strategy requires a user decision before apply. Each slice should include its own tests, verification, and rollback boundary:

1. **Reproducible safety baseline** — dependency lock/upgrade, test runner, current-behavior characterization, and stdio smoke tests.
2. **Corpus and evidence model** — explicit corpus policy, safe path resolution, frontmatter parser, stable IDs, line mappings, and preservation tests.
3. **Non-destructive index lifecycle** — candidate schema/build, validation, atomic promotion, failure recovery, and migration fixtures.
4. **Search contract** — bounded hybrid retrieval, category/trust metadata, structured results, diversification, degradation reporting, and retrieval tests.
5. **Read contract and trust boundary** — exact bounded evidence fetch, untrusted-data envelope, citation integrity, and adversarial tests.
6. **Context-kit experiment and evaluations** — broad task decomposition/assembly behind a gate, benchmark harness, baseline comparison, and keep/remove decision.
7. **Adoption and cutover** — Cerebro skill routing, side-by-side registration, rollback rehearsal, operational documentation, and stale Graphify retirement decision.

Slices should be adjusted during task planning if forecast additions plus deletions exceed 400 lines; tests stay with the behavior they verify rather than being deferred to a final testing PR.

### Risks

- Stable IDs are difficult if headings or files are renamed; a persisted alias strategy is required before promising durable external citations.
- “Preserve all content” can conflict with privacy-aware retrieval. The proposal must distinguish immutable preservation from default indexing eligibility and explicitly decide treatment of personal/diary/archive areas.
- Broad category routing can over-diversify and suppress the best exact result; exact-query and broad-task evaluation sets need separate scoring.
- Embedding/model changes may improve some languages while regressing others. Lock model identity and compare against the current multilingual baseline.
- Frontmatter quality is heterogeneous; missing metadata must be represented as unknown, not inferred as verified.
- Source notes can contain prompt injection or unsafe procedures. Metadata and delimiters reduce risk but do not make retrieved content trusted.
- Side-by-side registration can confuse clients or skills if tool names/descriptions overlap. Candidate naming and test-scoped routing must be explicit.
- The stale graph contains useful relationships mixed with excluded/noisy material. Reuse without a clean source manifest could reintroduce privacy and provenance defects.
- Dependency upgrades can alter FastMCP behavior. Pinning alone is insufficient; stdio contract tests and release-note review are required.

### Ready for Proposal

Yes. The proposal should commit to the parallel, non-destructive replacement; the two-tool progressive-disclosure baseline; source notes as immutable ground truth; explicit corpus/privacy policy; benchmark-gated graph/context assembly; dependency and test bootstrap; side-by-side verification; rollback; and chained review slices under the 400-line budget.

---

## Scope Expansion Addendum: Universal Work-Intelligence MCP

**Recorded:** 2026-07-23, after independent acceptance of slices 1–4 and before task 5.1.  
**History rule:** The original exploration above is retained unchanged. This addendum expands the product boundary; it does not invalidate completed implementation or verification history.

### Executive Decision

Cerebro should become an installable, cross-client **work-intelligence router**, not a universal answer database and not an autonomous general-purpose agent. Its differentiator is an evidence workflow: understand a work request, locate relevant local knowledge and capabilities, route toward current authoritative sources, perform only bounded safe research, verify the resulting evidence, and return an actionable context kit to the host AI.

The smallest recommended public surface remains **two read-only tools**, but their responsibility must expand:

1. `investigate_work` — route and investigate a bounded work request across local knowledge, source registries, capability registries, optional safe live retrieval, verification, and context assembly. It returns evidence references, claim/conflict state, uncertainty, and typed actions for the host when Cerebro cannot perform a step safely.
2. `read_evidence` — return exact bounded evidence for local or captured-live references, with immutable provenance, jurisdiction, temporal state, licensing constraints, and untrusted-content delimiters.

`search_knowledge` remains useful as an internal retrieval primitive, but no longer expresses the full product. `read_knowledge` becomes the local-evidence specialization of `read_evidence`. A separate tool for every domain, source, capability type, fetch mode, or verification step would increase context cost and create inconsistent client behavior without adding protocol power.

### Preserved Foundations

The clarified scope does **not** reopen slices 1–4:

- **Slice 1:** locked dependencies, clean installation checks, pytest, advisory audit, and legacy characterization remain the reproducible baseline.
- **Slice 2:** corpus policy, exact source lines, stable references, source hashes, provenance fields, aliases, tombstones, and root confinement remain the local evidence model.
- **Slice 3:** immutable candidate indexes, compatibility metadata, verified embedding identity, safe incremental reuse, and semantic validation remain valid local retrieval infrastructure.
- **Slice 4:** atomic pointer publication, retained indexes, recovery, canonical snapshots, and descriptor-bound POSIX validation remain valid. Portability requires an equivalent Windows strategy, not removal of the proven Darwin behavior.
- Source notes remain canonical and immutable. Legacy Cerebro remains active until expanded cross-domain and cross-client gates pass.
- Read-only MCP behavior, strict bounds, side-by-side adoption, configuration-first rollback, and feature-branch chained delivery remain hard constraints.

### Product Responsibility Layers

These concerns must be distinct in the architecture and evidence model even if one public tool composes them:

| Layer | Responsibility | Authority boundary |
|---|---|---|
| Local curated knowledge | Retrieve approved notes, methods, prior evaluations, and known source/capability pointers. | Useful orientation; never assumed current or authoritative solely because it is local. |
| Capability discovery | Identify relevant skills, MCP servers, tools, libraries, datasets, and human methods; record canonical source, version, permissions, and security state. | Recommend and explain; never auto-install, enable, authenticate, or execute. |
| Authoritative-source routing | Select source classes and publishers appropriate to claim type, jurisdiction, date, and risk. | Domain-pack policy ranks evidence contextually; there is no universal authority score. |
| Live research/fetching | Search or fetch current public material under explicit network policy and hard resource limits. | Only safe read operations; fetched content is untrusted and does not grant new access. |
| Verification | Check identity, authority, publication/effective dates, jurisdiction, freshness, provenance chain, corroboration, contradiction, and uncertainty. | Preserve conflicts; do not manufacture consensus or convert metadata into truth. |
| Context assembly | Build a bounded task kit of claims, evidence, gaps, conflicts, cautions, and next actions. | Every factual claim remains linked to evidence; no unsupported professional conclusion. |

### Tool-Surface Alternatives

1. **Two compositional tools (recommended)** — `investigate_work` plus `read_evidence`.
   - Pros: Lowest common denominator across clients; compact tool descriptions; one stable evidence envelope; supports local-only, direct-live, and host-assisted flows.
   - Cons: `investigate_work` needs a carefully bounded request/response schema and internal stages must remain independently testable.
   - Effort: High
2. **Three public tools** — route, investigate, then read.
   - Pros: More explicit stage control and easier partial retries.
   - Cons: More host orchestration, more invocation failures, and ambiguous boundaries between route and investigate.
   - Effort: High
3. **Many domain/source tools or autonomous MCP composition**.
   - Pros: Individual operations look simple.
   - Cons: Tool-list/context explosion; client-specific tool naming; unsafe hidden action chains; no portable way for one MCP server to discover or invoke every host's other MCP tools.
   - Effort: Very high; reject

The MCP interoperability floor is `tools/list` and `tools/call` over stdio, JSON Schema inputs, structured output plus canonical JSON text fallback, and deterministic error envelopes. Resources, prompts, roots, elicitation, dynamic tool lists, UI extensions, and annotation enforcement vary by host and must be progressive enhancements only. Runtime validation remains mandatory because Gemini-family clients may sanitize schema keywords such as `additionalProperties`.

### Safe Research Versus Host-Routed Work

`investigate_work` should accept the task, desired outcome, optional jurisdiction and `as_of`, risk class, network policy, host capability declaration, and bounded candidate material. It should report one of four execution states: `complete`, `partial`, `route_only`, or `abstained`.

**Cerebro may perform directly:**

- Local corpus, source-registry, domain-pack, and capability-registry lookup.
- Public HTTPS `GET`/`HEAD` retrieval from pack-approved sources and explicitly supplied public URLs.
- Bounded text/PDF/API response extraction, hashing, citation capture, temporal and jurisdiction checks, contradiction analysis, and context assembly.
- Public source discovery through explicitly configured read-only search adapters.

**Cerebro must route to the host or a human:**

- Browser interaction, JavaScript-only pages, CAPTCHAs, logins, paywalls, credential use, OAuth consent, or access to private systems.
- Shell execution, package installation, enabling another MCP/skill, exploit execution, form submission, purchases, writes, or any consequential action.
- Research requiring a host-only web search/browser/tool that the request has not declared available.
- Legal, accounting, or security conclusions when jurisdiction, effective date, authority, or evidence is insufficient; regulated/high-impact decisions require professional review.

Host routing must use vendor-neutral typed actions such as `web_search`, `fetch_public_url`, `inspect_capability`, `request_jurisdiction`, or `consult_professional`, with reason, bounded query/URL, expected evidence, and safety notes. Client-specific skills/rules translate those actions to available host tools. If the host cannot execute an action, Cerebro degrades explicitly rather than pretending research occurred.

### Source Registries and Domain Packs

A domain pack is a **research policy and source map**, not a cache of professional answers. Packs should be versioned, reviewable data with:

- Pack identity, maintainer, schema/version, supported claim types, jurisdictions, languages, update cadence, license, and disclaimer.
- Source records containing canonical publisher identity, authority tier per claim type, URL/discovery templates, accepted formats, temporal semantics, citation rules, reuse/license constraints, and known limitations.
- Capability records for skills, MCPs, tools, datasets, and methods: canonical distribution source, package/repository identity, version/advisory state, required permissions, network/data access, and integrity evidence.
- Verification rules for freshness windows, effective-versus-publication dates, supersession, corroboration, contradiction, and mandatory escalation.
- Explicit exclusions and demotions for mirrors, scraped copies, anonymous summaries, SEO content, unverifiable packages, and sources whose terms forbid the required use.

Initial reference packs should cover security, law, accounting, software engineering, and UX/web standards only as test fixtures and maintained source maps. Unknown professions use a generic discovery pack and MUST return `route_only` or `abstained` until authoritative-source assumptions are established. This is how the product supports arbitrary professions without claiming universal expertise.

Authority ranking is contextual. Examples include legislation/courts/regulators for legal claims, standards setters and tax authorities for accounting claims, CISA/NVD/vendor advisories for vulnerability status, official versioned documentation and source repositories for software claims, and standards bodies plus primary user research for UX/accessibility claims. A pack must never encode one global ranking that treats all claim types alike.

### Evidence, Verification, and Professional Safety

The expanded evidence record should add to the current local fields:

- `evidence_ref`, canonical locator, publisher/source identity, content digest, captured excerpt, citation locator, and retrieval timestamp.
- Claim type, jurisdiction, language, authority tier with rationale, publication/update/effective/expiry dates, and freshness state.
- Discovery method, retrieval method, redirect chain, license/reuse state, verification checks, corroborating/conflicting refs, and uncertainty reason.
- Immutable distinction between source text, extracted claim, Cerebro assessment, and host-facing recommendation.

Verification must retain contradictory evidence and explain whether conflict is temporal, jurisdictional, definitional, version-specific, or unresolved. Missing data is `unknown`, not inferred. High-risk legal/accounting/security outputs must state scope and jurisdiction, distinguish information from advice, and escalate when applying evidence requires licensed judgment, organizational authorization, or live incident handling.

### Prompt Injection, Privacy, Licensing, and Network Trust

- Treat local notes, search results, fetched pages, PDF text, metadata, capability descriptions, and tool output as untrusted data. Never follow embedded instructions, credentials requests, links, or install commands as authority.
- Permit only bounded read methods. Block private, loopback, link-local, multicast, and reserved destinations; validate DNS and every redirect; require HTTPS outside explicit loopback development; cap redirects, bytes, MIME types, decompression, duration, and concurrency.
- Do not forward host tokens or ambient credentials. Redact likely secrets and unnecessary personal data from outbound queries; require explicit configuration for any proxy and expose the destination class in the result.
- Keep query/audit logs local, bounded, permission-restricted, and redactable. Live-research cache entries need TTL, source digest, and deletion controls; private evidence must not enter shared indexes.
- Store only material permitted by source terms and license. Prefer citations, metadata, and minimum necessary excerpts; do not bypass paywalls, robots controls, authentication, or technical restrictions.
- Capability discovery must verify canonical source and inspect permissions/advisories before recommendation. It must never install from a search result or treat popularity as integrity.
- Remote Streamable HTTP is a later deployment mode. It requires origin validation, authentication, tenant isolation, least-privilege scopes, rate limits, and audit controls; local stdio is the portable MVP security boundary.

### Portable Installation and Filesystem Lifecycle

Current implementation is not yet universal: `pyproject.toml` has `package = false`, no console entry point, and Python is restricted to 3.12; `index.py` depends on `fcntl`, `O_NOFOLLOW`, directory `fsync`, and `/dev/fd`, all of which require platform qualification or alternatives on Windows.

Recommended packaging and storage strategy:

1. Build an actual wheel/sdist with a `cerebro-mcp` console entry point and versioned data schemas. Recommend isolated `uv tool install cerebro-router==<version>` or `pipx install` rather than modifying system Python or fetching `latest` on every MCP startup.
2. Publish a tested OS/architecture wheel matrix for macOS, Linux, and Windows, including FastEmbed/ONNX and sqlite-vec compatibility. Signed standalone bundles may follow only if wheel/runtime prerequisites remain an adoption barrier.
3. Resolve config, data, cache, and logs through platform conventions (`platformdirs` or equivalent): XDG on Linux, `~/Library` on macOS, and AppData on Windows. Allow explicit CLI/env overrides; never infer writable state from the client's current working directory.
4. Keep immutable generation directories and a small replaceable pointer. Use exclusive cross-platform writer locking (for example, `portalocker`, noting POSIX locks are advisory), process-local read snapshots, per-generation hashes, startup recovery, and retention/grace periods.
5. Preserve descriptor-bound `O_NOFOLLOW` hardening on POSIX. On Windows, use a native handle/share-mode backend or an equivalently tested generation-immutability and identity contract. Directory-fsync or replace limitations must produce an explicit durability state and recovery test, not a false durability claim.
6. State the local threat model: application-generated state lives in a user-only directory and resists crashes/races/path substitution; compromise by another process with the same user write authority is not solved by advisory locking alone.

There is no single portable client configuration file. Generate client examples from one canonical launch manifest and test each adapter:

| Client | Local stdio configuration strategy |
|---|---|
| Claude Code | User/local `claude mcp add ... -- cerebro-mcp ...` or project `.mcp.json`; project configs require user trust. |
| OpenCode | `opencode.json(c)` `mcp.<name>` with `type: local` and a command array. |
| Cursor | Global `~/.cursor/mcp.json` or project `.cursor/mcp.json`, with command/args and explicit environment interpolation. |
| Gemini CLI | Legacy/enterprise `~/.gemini/settings.json` or `.gemini/settings.json` `mcpServers`; retain compatibility tests while supported. |
| Antigravity CLI/IDE | Current consumer target; `.agents/mcp_config.json` or global profile. Test its dedicated schema and Gemini migration path rather than assuming 1:1 parity. |
| Other MCP clients | Document the canonical executable, arguments, environment, stdio purity, protocol range, and JSON fallback. |

Client adapters must not depend on shell quoting tricks or repository-relative paths. Diagnostics go only to stderr. Every example must pin or expose the installed version and declare corpus/state paths without embedding secrets.

### Required Changes to Existing SDD Artifacts

| Artifact | Remains valid | Must change before task 5.1 |
|---|---|---|
| Proposal | Non-destructive replacement, read-only safety, immutable sources, rollback, evaluation-first cutover. | Intent must become work-intelligence investigation; crawling/live research is no longer categorically out of scope; add capability/source routing, packs, packaging, portability, and host action protocol. |
| Agent-routing spec | Explicit invocation, strict bounds, multilingual local retrieval, exact evidence, degradation, injection boundary. | Replace mandatory `search_knowledge` flow with the two-tool investigation flow; specify host capabilities/actions, live evidence, verification, jurisdiction, conflicts, abstention, and professional escalation. |
| Corpus-lifecycle spec | Preservation, policy, stable identity, atomic promotion, compatibility, reproducibility, cutover gates. | Add external evidence/cache lifecycle, pack provenance/version/license, network trust, platform storage/durability, and OS/client compatibility gates. |
| Design | Modular boundaries, immutable index, structured/text compatibility, source-first trust, side-by-side rollout. | Revise surface and data flow; add registry/pack, safe fetch, claim verification, host routing, context assembly, packaging, and cross-platform lifecycle decisions. |
| Tasks 1–4 | Completed and independently verified. | Preserve checkboxes, history, and verification boundaries exactly. |
| Tasks 5–8 | Goals remain useful but are too local and combine unrelated risk. | Replace, after proposal/spec/design revision, with new dependency-ordered slices below. Do not start current task 5.1. |
| Verify report | Slice 4 evidence remains authoritative for its stated Darwin boundary. | Do not edit. Future portability verification is additive and cannot rewrite this PASS. |

### Replacement Slice Forecast

The expanded remaining work is approximately **2,650–3,350 changed lines**. Keep every implementation/review unit at or below 400 changed lines, tests included:

| New slice | Goal | Dependency | Forecast |
|---|---|---|---:|
| 5 | Work request, claim/evidence, source/capability registry, and domain-pack schemas with fixtures and negative packs | 1–4 | 325–400 |
| 6 | Local retrieval plus source/capability routing and domain-neutral ranking evaluations | 5 | 325–400 |
| 7 | `investigate_work`/`read_evidence` MCP contracts, canonical text fallback, budgets, and local-only integration | 6 | 325–400 |
| 8 | Installable package, platform directories, portable locking/publication adapters, and OS matrix tests | 4, 7 | 350–400 |
| 9 | Safe live discovery/fetch adapters, SSRF/redirect/content limits, cache lifecycle, privacy, and licensing controls | 5, 8 | 350–400 |
| 10 | Authority, jurisdiction, freshness, contradiction, uncertainty, and claim-ledger verification | 6, 9 | 325–400 |
| 11 | Bounded context assembly, typed host actions, degradation, abstention, and professional escalation | 7, 10 | 300–375 |
| 12 | Claude/OpenCode/Cursor/Gemini-Antigravity adapters, cross-client/domain evaluation, adoption, rollback, and cutover gates | 8, 11 | 350–400 |

The review-budget risk remains **High** and chained PRs remain required. The approved `feature-branch-chain` strategy remains appropriate. Estimates must be refreshed during the tasks phase after proposal/spec/design deltas are approved; any slice forecast over 400 lines must split before apply rather than claim a size exception by default.

### Cross-Domain and Cross-Client Evaluation

Use replayable fixtures plus controlled live qualification. The minimum matrix spans security, law in at least two jurisdictions, accounting/tax in at least two jurisdictions, versioned software engineering, UX/accessibility/web standards, and an intentionally unsupported profession.

Measure:

- Authoritative-source Recall@5 and primary-source Precision@5 by claim type.
- Correct jurisdiction and effective-date selection; freshness and supersession classification.
- Citation locator/hash correctness and unsupported-claim rate (target: zero in assembled factual claims).
- Contradiction recall and correct preservation of unresolved conflicts.
- Capability-source integrity, permission/advisory disclosure, and zero automatic installs/executions.
- Abstention/escalation precision and recall for missing jurisdiction, stale authority, unsafe action, and insufficient evidence.
- Prompt-injection action rate, private-network fetch rate, secret leakage, and prohibited-content retention (all target: zero).
- Required-category coverage, evidence redundancy, token/output budgets, latency, and direct-live versus host-assisted degradation.
- Initialization, tools/list, schema acceptance, tool calls, structured output, text fallback, errors, timeouts, and shutdown across every supported client/OS pair.

Negative controls must include a fabricated authority, SEO content outranking an official source, wrong jurisdiction, superseded rules, conflicting official sources, malicious fetched instructions, private/link-local redirects, oversized/decompression payloads, unknown license, tampered skill/MCP package, unavailable network, host without search/fetch, and a task for which no defensible authority is known.

Explicit behavior:

- `complete`: sufficient current evidence for the bounded informational output; still identify professional limits.
- `partial`: useful evidence exists but named gaps/conflicts remain; do not fill them by inference.
- `route_only`: provide safe, specific host/human research actions and required evidence.
- `abstained`: provide no conclusion; state the missing jurisdiction, authority, permission, freshness, or safety condition and the escalation path.

### Expanded Risks

- A single orchestration tool can become opaque or unbounded; internal stage traces, budgets, and typed outcomes are mandatory.
- Domain packs can silently encode bias, stale law, or false authority. Versioning, maintainer review, negative tests, and jurisdiction-specific gates are product requirements.
- Live fetching creates SSRF, privacy, licensing, prompt-injection, and supply-chain attack surfaces absent from the local-only design.
- Cross-client support can regress as hosts sanitize schemas, rename tools, alter trust prompts, or change configuration formats. Compatibility must be tested, not inferred from protocol compliance.
- Python native/model dependencies may limit OS/architecture installation. Packaging qualification must precede the “universal” claim.
- The proven POSIX descriptor strategy cannot simply be copied to Windows. Equivalent identity and recovery guarantees require a platform abstraction and adversarial tests.
- Context assembly may sound authoritative even when evidence is weak. Claim-level citations, visible uncertainty, and abstention must survive compact output.
- “Arbitrary professions” can invite unsupported expertise. Generic discovery plus explicit abstention is a feature, not a failure.

### Recommendation and Readiness

Adopt the two-tool compositional model, domain packs as research policy rather than expertise, a hybrid direct-live/host-routed execution contract, and an evidence claim ledger with explicit abstention. Preserve slices 1–4 and legacy operation. Reopen proposal, specifications, design, and tasks in that order before any task 5.1 implementation.

**Ready for revised proposal:** Yes.  
**Ready for current task 5.1:** No; its local retrieval scope is now an incomplete dependency of the expanded product.

### Research References Consulted

- MCP tools and structured-content compatibility: <https://modelcontextprotocol.io/specification/2025-06-18/server/tools>
- MCP stdio/Streamable HTTP and network requirements: <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- MCP security guidance (local servers, SSRF, OAuth, scope minimization): <https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices>
- Claude Code MCP configuration and trust: <https://docs.anthropic.com/en/docs/claude-code/mcp>
- OpenCode local/remote MCP configuration: <https://opencode.ai/docs/mcp-servers/>
- Cursor MCP transports and configuration: <https://cursor.com/docs/context/mcp>
- Gemini MCP configuration and schema processing: <https://geminicli.com/docs/tools/mcp-server/>
- Antigravity migration and MCP configuration changes: <https://antigravity.google/docs/cli/gcli-migration>
- Isolated Python tool installation: <https://docs.astral.sh/uv/guides/tools/>
- Cross-platform application directories: <https://platformdirs.readthedocs.io/en/latest/>
- Cross-platform locking constraints: <https://portalocker.readthedocs.io/en/latest/>
