# Knowledge Corpus Lifecycle Specification

## Purpose

Define lifecycle gates.

## Requirements — Original Local-Lifecycle Baseline (Superseded 2026-07-23)

The following requirements and scenarios are retained verbatim as verified history. The dated universal work-intelligence revision below is the normative replacement.

### Requirement: Canonical Corpus Preservation

Source files MUST remain canonical and byte-identical unless changed by a separately approved corpus workflow. The router MUST NOT autonomously create, rewrite, normalize, move, or delete them; indexes, embeddings, summaries, and graphs MUST remain replaceable, non-authoritative derivatives.

#### Scenario: Full lifecycle preservation

- GIVEN a pre-build manifest of all source files and hashes
- WHEN build, promotion, cutover, and rollback are exercised
- THEN every unapproved source path and byte hash remains unchanged

### Requirement: Resolved Root and Eligibility Policy

Every source MUST resolve within an approved root; symlink escapes MUST be rejected. Preservation MUST be independent from retrieval eligibility. Personal, diary, archive, configuration, hidden, generated, and sensitive areas MUST have explicit inclusion, exclusion, and filter rules; excluded content MUST NOT enter indexes, outputs, metadata leakage, or graph signals.

#### Scenario: Escaping or private source

- GIVEN a Markdown path resolves outside an approved root or policy excludes it
- WHEN candidate ingestion runs
- THEN it is excluded with a non-content-bearing reason and cannot be retrieved

### Requirement: Stable Identity, Provenance, and Freshness

Each passage MUST map to canonical relative identity, exact lines, source hash, and a ref independent of row order. Frontmatter provenance, URLs, status, and verification dates MUST round-trip without inferred trust; absent fields MUST be `unknown`. Renames or identity changes MUST yield aliases or stale refs, never silent reassignment.

#### Scenario: Rebuild stability

- GIVEN unchanged eligible sources are rebuilt in a different enumeration order
- WHEN corresponding passages are compared
- THEN refs, line mappings, source hashes, provenance, and freshness states are identical

### Requirement: Atomic Validated Index Promotion

Candidates MUST be built beside the active index and validated for schema, compatibility, corpus manifest, root confinement, evidence mappings, counts, and representative queries before atomic promotion. Readers MUST use one complete compatible index throughout. Failure MUST leave the prior service and index queryable with actionable diagnostics.

#### Scenario: Candidate validation fails

- GIVEN an active compatible index serves reads
- WHEN candidate validation fails
- THEN no active pointer changes and uninterrupted reads continue on the prior index

### Requirement: Compatibility, Recovery, and Rollback

Every index MUST declare format version, service/protocol compatibility, model identity and dimensions, corpus manifest, build identity, and creation time. Incompatible indexes MUST be rejected. Recovery MUST select a validated compatible index; configuration rollback MUST restore the prior command, environment, registration, and timestamped index without rebuilding.

#### Scenario: Incompatible promotion and rollback

- GIVEN a candidate has incompatible metadata and a prior release is retained
- WHEN promotion is attempted and rollback is requested
- THEN activation is refused and the prior registered release resumes without index construction

### Requirement: Reproducible Runtime and MCP Contract

Runtime/transitive dependencies, model identities, Python support, and MCP expectations MUST be committed and locked to patched versions. A clean environment MUST install, build, and start reproducibly over stdio, keep diagnostics outside protocol stdout, and pass supported-client initialization, discovery, schema, error, and shutdown tests.

#### Scenario: Clean-room protocol run

- GIVEN no existing virtual environment or generated index
- WHEN the locked setup and MCP contract suite run
- THEN installation, candidate build, stdio lifecycle, and tool contracts succeed without undeclared dependencies

### Requirement: Pre-Cutover Evaluation Gates

Cutover MUST require: legacy characterization; byte preservation and 100% root confinement; English/Spanish/mixed retrieval; exact citation/hash, injection, privacy, failed-build, uninterrupted-read, promotion, recovery, and rollback tests; invocation and follow-up reads at 100%; Recall@10 ≥0.90, MRR@10 ≥0.75, top-5 task success ≥0.85 without material category regression; warm p95 search ≤1.5s and read p95 ≤100ms on the reference machine. Failure MUST block cutover while the active router remains available.

#### Scenario: Gate failure blocks cutover

- GIVEN any characterization, retrieval, citation, security, reliability, compatibility, invocation, utility, or latency gate fails
- WHEN cutover eligibility is evaluated
- THEN canonical registration remains unchanged and the failed evidence is reported

---

## Requirements — Universal Work-Intelligence Revision (2026-07-23)

### Requirement: Preserved Canonical Sources and Verified Foundations

Source notes MUST remain canonical, immutable except through a separately approved corpus workflow, and independent from retrieval eligibility. Verified Slices 1–4 contracts for confined corpus policy, exact lines, stable refs, immutable compatible generations, validated candidate builds, atomic POSIX activation, retained indexes, recovery, and legacy continuity SHALL remain satisfied; added platform behavior MUST NOT weaken them.

#### Scenario: Expanded lifecycle preserves history

- GIVEN verified local generations and a byte-hash source manifest
- WHEN registry, research, packaging, cutover, and rollback flows run
- THEN sources and prior guarantees remain intact and legacy stays available until cutover

### Requirement: Versioned Source Registries and Domain Packs

Every source registry and domain pack MUST declare schema version, identity, version, maintainer, review date, supported claim types, jurisdictions, languages, freshness windows, update cadence, license, provenance, and compatibility. Source entries MUST record contextual authority class and rationale, temporal semantics, citation/reuse rules, known limitations, bias/conflict metadata, and explicit exclusions. Packs MUST be research policy and source maps, not professional answers; incompatible, expired, unsigned-or-unverified where signatures are required, or invalid packs MUST fail closed.

#### Scenario: Biased, stale, or conflicting pack metadata

- GIVEN a pack omits required provenance or declares bias, conflict, staleness, or incompatible version
- WHEN it is loaded
- THEN the condition is explicit, invalid use is blocked, and no authority is silently promoted

### Requirement: External Evidence and Cache Lifecycle

Captured live evidence MUST retain source digest, locator, redirect chain, retrieval time, policy/pack versions, license/reuse state, and expiry. Cache writes MUST be atomic, bounded, locally permission-restricted, partitioned from private evidence, and governed by TTL and deletion controls. Expired content MAY remain for audit only when licensed and MUST NOT be presented as current. Prohibited or unknown reuse MUST store only permitted metadata, citation, and minimum necessary excerpt.

#### Scenario: Cache expiry or prohibited reuse

- GIVEN captured evidence expires or its terms prohibit body retention
- WHEN it is requested or maintained
- THEN current use is blocked or marked stale and retained material is limited to permitted fields

### Requirement: Portable Filesystem, Locking, and Promotion

macOS, Linux, and Windows adapters MUST resolve config, data, cache, and logs through native user directories unless explicitly overridden, never from client working directory. Application state SHALL default to user-private permissions. Each platform MUST provide exclusive writer locking, immutable generation identity, complete-reader snapshots, atomic-or-recoverable pointer publication, startup recovery, retention, and crash/race/path-substitution tests. POSIX descriptor-bound no-follow validation SHALL remain; Windows MUST provide equivalent tested identity/share-mode guarantees. Unsupported durability semantics MUST be reported, never overstated.

#### Scenario: Concurrent promotion and crash on each OS

- GIVEN readers, competing writers, and a fault during publication on a supported platform
- WHEN recovery starts
- THEN readers observe one complete compatible generation and recovery selects a validated generation without source loss

### Requirement: Installable Package, Configuration, and Doctor

Each release MUST provide a versioned installable package and stable `cerebro-mcp` entry point, declare supported Python/OS/architecture and protocol ranges, lock patched dependencies, and start with protocol stdout free of diagnostics. Install guidance MUST use isolated pinned installation. Configuration MUST have a documented precedence order, strict validation, explicit corpus/network overrides, no embedded secrets, and private local/network-disabled defaults. A read-only `doctor` command MUST report package, dependency/advisory, schema, directories/permissions, corpus, index/recovery, network-policy, and client-launch readiness without exposing secrets or modifying state unless a separate explicit repair operation is approved.

#### Scenario: Clean private-default installation

- GIVEN a supported clean machine with no repository checkout or prior environment
- WHEN a pinned package is installed, configured, and checked
- THEN the entry point and doctor work from native directories with network disabled and no secret disclosure

### Requirement: Canonical Client Launch Manifest and Adapters

One canonical launch manifest MUST generate or validate guidance for Claude Code, OpenCode, Cursor, Gemini CLI, Antigravity, and generic stdio MCP clients. Adapters MUST use absolute executable semantics, explicit arguments/environment, version visibility, stderr diagnostics, and no shell-specific quoting dependency. Client-specific configuration MUST NOT redefine the public schemas or safety policy.

#### Scenario: Client feature degradation

- GIVEN a supported client lacks an optional MCP feature or uses a distinct configuration schema
- WHEN its adapter is installed and qualified
- THEN initialization, tool calls, fallback, errors, timeout, shutdown, and rollback retain core functionality

### Requirement: Cross-Domain, Client, Platform, Security, and Utility Gates

Cutover MUST pass replayable and controlled-live evaluations for cybersecurity; law and accounting in at least two jurisdictions each; versioned programming; UX/UI/accessibility/web design; and an unsupported profession, across every claimed client/platform pair. Gates MUST cover deterministic invocation and negative controls; schema/fallback/pagination; authority-source recall and primary-source precision; jurisdiction/effective-date/freshness/supersession; citation/hash correctness; contradiction recall; abstention/escalation; package/capability integrity; recovery/rollback; latency and budgets; and task utility against the legacy baseline.

Security gates MUST record zero unsupported factual claims, prohibited autonomous actions, prompt-injection actions, private-network fetches, ambient-secret leaks, restriction bypasses, and prohibited-content retention. Failed mandatory gates MUST block universal/support claims and canonical cutover while preserving side-by-side legacy operation.

#### Scenario: Domain, client, platform, or security failure

- GIVEN any mandatory matrix cell or zero-tolerance control fails
- WHEN release eligibility is evaluated
- THEN the affected support claim and canonical cutover are blocked with replayable evidence

#### Scenario: Utility regression

- GIVEN the candidate is safe but materially worse than legacy or declared task-utility thresholds
- WHEN comparative evaluation completes
- THEN legacy remains canonical and the candidate is not promoted

### Requirement: Configuration-First Cutover and Rollback

Cutover MUST preserve the prior executable, environment, registration, configuration, and validated index for the rollback window. Promotion SHALL be side-by-side and reversible without rebuild. Rollback MUST restore the complete prior launch contract and MUST NOT delete candidate diagnostics or alter canonical sources.

#### Scenario: Post-cutover rollback

- GIVEN canonical registration was switched and a regression is detected
- WHEN rollback is invoked
- THEN the prior client registration and compatible index resume without rebuild or source mutation
