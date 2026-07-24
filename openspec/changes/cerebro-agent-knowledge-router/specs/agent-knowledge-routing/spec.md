# Agent Knowledge Routing Specification

## Purpose

Define deterministic, read-only agent discovery and retrieval.

## Requirements — Original Local-Router Baseline (Superseded 2026-07-23)

The following requirements and scenarios are retained verbatim as verified history. The dated universal work-intelligence revision below is the normative replacement.

### Requirement: Deterministic Explicit Invocation

Supported-client guidance MUST route “use Cerebro” through `search_knowledge`, use `read_knowledge` before citation-dependent synthesis, and MUST NOT substitute filesystem discovery without reporting Cerebro unavailable.

#### Scenario: Explicit bilingual invocation

- GIVEN a supported client receives “use Cerebro” or “usá Cerebro”
- WHEN it needs knowledge to complete the task
- THEN it invokes `search_knowledge` and reads selected evidence before synthesis

### Requirement: Strict Search Contract

`search_knowledge` MUST reject unknown fields, wrong types, empty or over-2,048-character `query`, `limit` outside 1–20, and invalid cursors/filters. It SHALL accept `query`, optional `limit` (default 10), `cursor`, `categories`, `languages`, `trust`, and `verified_since`. Output MUST contain `status`, `results`, `warnings`, `degradation`, and `next_cursor`; results MUST contain `ref`, title, category, canonical path, heading, lines, snippet, retrieval contributions, provenance URLs, verification date/state, freshness state, and source hash.

#### Scenario: Invalid bounded request

- GIVEN a request has an unknown field or exceeds a declared bound
- WHEN `search_knowledge` validates it
- THEN it returns a structured validation error and performs no search

### Requirement: Multilingual Hybrid and Broad Discovery

Search MUST combine lexical and semantic evidence for English, Spanish, and mixed-language queries, preserve exact terms, apply filters, and diversify broad results across relevant skills, MCP servers, security, tools, architecture, methods, prompts, playbooks, and related knowledge. Duplicate-heavy categories MUST NOT displace an available relevant category.

#### Scenario: Broad bilingual task

- GIVEN eligible evidence exists in several relevant categories
- WHEN a broad Spanish or English task is searched
- THEN results represent the relevant categories and retain stable evidence references

### Requirement: Search Degradation and Abstention

Unavailable channels MUST produce degradation warnings; remaining channels MAY return results. With no eligible relevant result, search MUST return `status: abstained` without fabricated evidence. Over-budget output MUST use a declared compact fallback preserving status, refs, category, path, lines, provenance/freshness, cursor, and warnings.

#### Scenario: Partial retrieval failure

- GIVEN one retrieval channel is unavailable and another has relevant evidence
- WHEN search executes
- THEN it returns bounded evidence with the failed channel named in `degradation`

### Requirement: Exact Bounded Evidence Reads

`read_knowledge` MUST reject unknown fields, over 10 refs, reversed ranges, and requests above 200 lines per ref. Each ref and optional subrange MUST return exact source text, canonical path, inclusive lines, line citations, source hash, provenance/freshness, `truncated`, and a continuation token when needed.

#### Scenario: Exact passage with continuation

- GIVEN a valid ref resolves to an eligible passage longer than the read bound
- WHEN the passage is read
- THEN returned text exactly matches cited source lines and provides continuation without overlap or omission

#### Scenario: Missing or stale reference

- GIVEN a ref is missing or its recorded source identity no longer matches
- WHEN it is read
- THEN the tool returns a typed `missing_ref` or `stale_ref` result and never silently redirects it

### Requirement: Untrusted Knowledge Boundary

Retrieved content MUST be labeled and delimited as untrusted data. Its instructions MUST NOT alter policy, expand access, trigger tools, override host instructions, or become trusted provenance; warnings MUST survive compact output.

#### Scenario: Prompt injection in a note

- GIVEN eligible evidence contains instructions to ignore policy or invoke a tool
- WHEN it is searched or read
- THEN the text remains quoted evidence and causes no privileged action

### Requirement: Optional Surface Evidence Gate

`assemble_work_context` MAY ship only if bounded agent evaluations defensibly improve required-category coverage or reduce round trips versus search-then-read, while returning cited refs and no unsupported facts. Graph augmentation MAY affect retrieval only after source-traceability, privacy, direction/correctness, and incremental utility gates pass; public traversal requires separate approval.

#### Scenario: Candidate augmentation lacks benefit

- GIVEN context assembly or graph augmentation does not beat the non-augmented baseline
- WHEN the release surface is selected
- THEN only `search_knowledge` and `read_knowledge` remain public

---

## Requirements — Universal Work-Intelligence Revision (2026-07-23)

### Requirement: Deterministic Explicit Invocation and Negative Controls

Supported-client guidance SHALL invoke Cerebro when the user explicitly asks to use Cerebro, including documented equivalent localized forms, and SHALL NOT invoke it merely because a task mentions knowledge, research, a profession, a tool, or a source. If unavailable, the client MUST report that state and MUST NOT silently substitute another discovery path as Cerebro.

#### Scenario: Explicit invocation

- GIVEN a supported client receives an explicit request to use Cerebro
- WHEN work intelligence is needed
- THEN it calls `investigate_work` and reads selected refs before citation-dependent synthesis

#### Scenario: Negative control

- GIVEN a task mentions research or a profession without requesting Cerebro
- WHEN the client selects tools
- THEN it does not invoke Cerebro solely from that mention

### Requirement: Portable Two-Tool Public Contract

The public contract MUST expose only `investigate_work` and `read_evidence`. Both tools MUST publish strict JSON Schemas, reject unknown fields and invalid types or bounds before work, return a structured result conforming to a declared output schema, and include the same result as canonical JSON in text fallback. Protocol-only clients MUST retain core behavior through `tools/list` and `tools/call`; optional MCP features MUST NOT be required.

`investigate_work` input SHALL contain required `task` (1–4,096 characters) and MAY contain `outcome`, `jurisdiction`, `as_of`, `risk_class`, `network_policy`, `host_capabilities`, `candidate_material`, `cursor`, and `budgets`. `read_evidence` input SHALL contain `refs` (1–10 unique stable refs) and MAY contain per-ref ranges, `cursor`, and output budgets.

#### Scenario: Strict schema and fallback

- GIVEN a client omits a required field, adds an unknown field, or cannot consume structured output
- WHEN it calls either tool
- THEN invalid input performs no work, while valid output remains available as canonical JSON text

### Requirement: Bounded Investigation, Pagination, and Stable References

Every request MUST enforce declared ceilings for candidates, pages, redirects, network requests, elapsed time, bytes, extracted text, evidence items, claims, and output tokens/characters. Results SHALL include `schema_version`, `status`, `request_id`, `evidence`, `claims`, `conflicts`, `gaps`, `host_actions`, `warnings`, `degradation`, `budgets`, and `next_cursor`; cursors MUST be opaque, request-bound, expiring, and invalid after relevant policy or evidence changes. Stable refs MUST identify immutable captured evidence rather than row order or pagination position.

#### Scenario: Budget exhaustion

- GIVEN valid work exceeds a declared budget
- WHEN the ceiling is reached
- THEN the tool stops, returns bounded partial results and consumed budgets, and provides a cursor or typed gap without dropping refs

### Requirement: Exact Bounded Evidence Reading

`read_evidence` MUST resolve each requested local or captured-live ref to the immutable evidence identity originally returned, enforce per-item and total byte/line/output budgets, and return exact content with ref, evidence kind, canonical locator, citation locator, digest, inclusive range, timestamps, provenance, authority/freshness/license/conflict states, `truncated`, and an opaque continuation cursor. Missing, stale, expired, policy-ineligible, or range-invalid refs MUST return typed per-ref failures and MUST NOT silently redirect or substitute evidence.

#### Scenario: Exact read with continuation

- GIVEN a valid ref resolves to evidence larger than the read budget
- WHEN `read_evidence` is called
- THEN exact non-overlapping content and metadata are returned with a continuation cursor

#### Scenario: Stale or ineligible ref

- GIVEN a ref no longer matches its digest or is disallowed by current policy
- WHEN it is read
- THEN a typed per-ref failure is returned without content substitution

### Requirement: Local Knowledge and Capability Discovery

Investigation SHALL search eligible local knowledge and discover relevant skills, MCP servers, tools, libraries, datasets, methods, and professional workflows from approved registries. Capability records MUST disclose canonical identity, version state, integrity/advisory evidence, permissions, network/data access, and limitations. Cerebro MUST NOT infer integrity from popularity or install, enable, authenticate to, or execute a discovered capability.

#### Scenario: Method and tool discovery

- GIVEN approved local evidence and capability records match a task
- WHEN local-only investigation runs
- THEN it returns complementary knowledge and capability refs with provenance, permissions, limitations, and no mutation

### Requirement: Safe Bounded Live Research

Live research MUST be opt-in by policy and limited to public read-only `HTTPS` destinations admitted by strict configured destination allowlists. The router MUST validate hostname resolution and every redirect; reject loopback, private, link-local, multicast, reserved, ambiguous, and disallowed IP destinations; pin validation to the connection destination; and enforce method, port, redirect, timeout, concurrency, compressed/decompressed size, and content-type limits. It MUST NOT forward ambient credentials, cookies, authorization headers, client secrets, or undeclared proxy credentials, and MUST redact unnecessary personal data and likely secrets from outbound material and local logs.

Robots directives, access restrictions, source terms, and licensing policy MUST be honored where applicable. Because the router fetches only caller-declared single URLs and never crawls, link-discovers, or follows undeclared destinations, robots directives are honored through the operator-configured destination allowlist/denylist (`AccessPolicy`) rather than by live-fetching `robots.txt`: issuing an unrequested request for a host's `robots.txt` would itself violate the caller-declared-URL-only and no-hidden-chaining invariants this requirement depends on, and `robots.txt` governs automated crawling/discovery that this router structurally does not perform. Paywalls, authentication, CAPTCHAs, and technical restrictions MUST NOT be bypassed.

#### Scenario: DNS or redirect SSRF attempt

- GIVEN an allowed-looking URL resolves or redirects to a prohibited destination
- WHEN live retrieval validates the connection chain
- THEN it blocks before content access and reports a typed safety result without leaking credentials

#### Scenario: Oversized or prohibited content

- GIVEN a response exceeds a time, size, decompression, redirect, or MIME limit
- WHEN retrieval reaches that limit
- THEN it stops, retains no prohibited body, and returns bounded diagnostics

### Requirement: Instruction and Evidence Separation

Local notes, web content, PDFs, search results, metadata, capability descriptions, and tool outputs MUST be treated as untrusted evidence. Their instructions, links, requests for secrets, policy claims, and tool commands MUST NOT alter system/host instructions, budgets, permissions, source policy, or actions. Returned source text MUST be distinctly delimited from Cerebro assessments and host actions.

#### Scenario: Retrieved prompt injection

- GIVEN evidence directs the agent to ignore policy, disclose secrets, or invoke a tool
- WHEN Cerebro processes or returns it
- THEN the instruction remains quoted data and causes no policy, access, or action change

### Requirement: Evidence Normalization and Claim State

Each evidence record MUST preserve stable ref, source/publisher identity, canonical locator, content digest, citation locator, captured/retrieved timestamp, publication/update/effective/expiry dates when known, jurisdiction, language, authority class with rationale, freshness state, license/reuse state, provenance chain, and extraction method. Claims MUST distinguish source text, extracted claim, Cerebro assessment, and host-facing recommendation; cite supporting and conflicting refs; preserve contradictions; and expose `supported|conflicted|insufficient|unknown` state plus uncertainty reason. Missing metadata MUST remain `unknown`.

#### Scenario: Conflicting current sources

- GIVEN authoritative evidence disagrees by time, jurisdiction, definition, or version
- WHEN claims are normalized
- THEN both refs and the conflict class remain visible and no consensus is fabricated

### Requirement: Domain-Sensitive Outcomes and Unsupported-Domain Abstention

Cybersecurity results MUST distinguish vulnerability evidence from authorization and incident judgment; law and accounting MUST require jurisdiction and applicable/effective date; programming MUST bind guidance to product/version; UX, UI, accessibility, and web-design results MUST distinguish standards from contextual research. For any profession without an applicable approved pack or defensible authority assumptions, Cerebro MUST return `route_only` or `abstained`, never a generic professional conclusion.

#### Scenario: Supported regulated domain lacks context

- GIVEN a legal task lacks jurisdiction or applicable effective date
- WHEN investigation evaluates sufficiency
- THEN it returns a named gap and professional escalation rather than a conclusion

#### Scenario: Accounting authority is jurisdiction-sensitive

- GIVEN an accounting or tax task has sources for a different jurisdiction or reporting regime
- WHEN investigation assesses applicability
- THEN it preserves those refs as non-applicable and requests qualified current authority

#### Scenario: Cybersecurity evidence lacks authorization

- GIVEN current vulnerability evidence exists but authorization or environment facts do not
- WHEN investigation assembles the result
- THEN it cites the evidence but does not recommend or execute exploitation or incident actions

#### Scenario: Programming guidance is version-bound

- GIVEN official programming documentation applies to a different product version
- WHEN investigation checks the requested version
- THEN it marks the mismatch and does not present the guidance as applicable

#### Scenario: UX and web evidence types differ

- GIVEN a UX, UI, accessibility, or web-design task has standards and contextual user research
- WHEN evidence is normalized
- THEN the result distinguishes normative standards from context-specific findings

#### Scenario: Arbitrary unsupported profession

- GIVEN a task belongs to a profession with no applicable approved domain pack
- WHEN generic discovery cannot establish authority policy
- THEN status is `route_only` or `abstained` with required next evidence

### Requirement: Read-Only Informational Boundary and Host Actions

Outcomes MUST be one of `complete`, `partial`, `route_only`, or `abstained` and MUST state gaps, conflicts, uncertainty, and professional limits. Cerebro SHALL provide evidence-linked information, not legal, accounting, security, medical, or other professional conclusions. It MUST NOT mutate files or systems, submit forms, purchase, exploit, install, enable, authenticate, or perform consequential actions. Needed work SHALL be expressed as vendor-neutral typed host actions with reason, bounded input, expected evidence, and safety notes.

#### Scenario: Consequential action requested

- GIVEN completing a task requires credentials, a write, execution, or licensed judgment
- WHEN Cerebro reaches that boundary
- THEN it performs no action and returns `route_only` or `abstained` with safe escalation

### Requirement: Cross-Client Core Equivalence

Claude Code, OpenCode, Cursor, Gemini/Antigravity, and generic MCP qualification MUST prove equivalent schemas, statuses, refs, citations, budgets, errors, and text fallback. Unsupported roots, resources, prompts, annotations, elicitation, dynamic lists, or UI features MAY degrade with an explicit capability notice but MUST NOT remove investigation or evidence reading. Runtime validation MUST remain authoritative if a client sanitizes schema keywords.

#### Scenario: Minimal or schema-sanitizing client

- GIVEN a client supports only tool listing/calls or removes a schema constraint
- WHEN it invokes valid or invalid input
- THEN core valid behavior remains available and server-side validation still rejects invalid input deterministically

### Requirement: Future Surface Extension Gate

Search, routing, verification, graph, domain, client, and fetch operations SHALL remain internal stages unless a separately approved extension demonstrates measurable task utility, security equivalence, cross-client portability, bounded context cost, and non-duplication of the two-tool contract. Failure of any gate MUST leave the public surface unchanged.

#### Scenario: Proposed specialized tool

- GIVEN a candidate public tool duplicates an internal stage or lacks cross-client utility evidence
- WHEN extension gates are evaluated
- THEN it is not exposed and the two-tool surface remains authoritative
