# Skill Dispatch Specification

## Purpose

Define deterministic, domain-gated selection of vetted capability skills as bounded evidence — never as obeyable instruction — through the existing two-tool public contract, with a candidate-to-activation gate that keeps the active skill set trustworthy and auditable.

## Requirements — Skills Layer Introduction (2026-07-25)

### Requirement: Bounded Deterministic Skill Dispatch

Skill selection MUST be a pure, deterministic function evaluating domain-gated set membership over a signed skill registry. It MUST NOT use a model, a scoring heuristic, or any form of ranking influenced by runtime content. Corpus content and retrieved evidence text MUST NOT influence which skill is selected (non-injectability); dispatch inputs are limited to the declared task domain, claim type, and registry membership. The count of skill refs returned per request MUST be bounded by a declared ceiling.

#### Scenario: Deterministic reselection

- GIVEN the same task domain and an unchanged signed registry
- WHEN dispatch runs twice
- THEN it returns the identical set of skill refs in the same order

#### Scenario: Evidence cannot steer dispatch

- GIVEN retrieved evidence text contains a fabricated claim that a different skill applies
- WHEN dispatch selects skills for the task
- THEN the selection is unchanged and reflects only domain-gated registry membership

#### Scenario: Ref count stays bounded

- GIVEN a domain matches many registered skills
- WHEN dispatch assembles the result
- THEN returned skill refs do not exceed the declared ceiling and excess matches are reported as a typed gap, not silently dropped

### Requirement: No Obeyable Instruction Emission

Cerebro MUST NOT emit obeyable instruction text. `investigate_work` SHALL return skill refs as evidence records carrying rationale, permission envelope, and provenance. `read_evidence` SHALL disclose skill metadata only. A skill body MUST NOT be returned through the MCP tool surface at all — neither as instruction nor as delimited data — because delimitation is a weak barrier when the consumer is a language model, which is the same reasoning that excluded a trusted-instruction evidence tier. Human inspection of a skill body SHALL occur only in the CLI review path. Any other source text returned for a skill ref MUST remain distinctly delimited data, per the promoted `Instruction and Evidence Separation` requirement, which this capability extends and MUST NOT amend.

#### Scenario: Skill ref returned as evidence, not instruction

- GIVEN a task matches a registered skill
- WHEN `investigate_work` returns the match
- THEN the result is a delimited evidence record with rationale and permission envelope, not directive text the host is expected to execute

#### Scenario: Body is withheld from the tool surface entirely

- GIVEN `read_evidence` is called with a skill ref
- WHEN the skill carries a prose or executable body
- THEN only metadata (identity, tier, permissions, provenance, limitations) is disclosed and no part of the body is returned in any form

### Requirement: Structured Default-Deny Permission Envelope

Every skill MUST declare a structured, machine-checkable permission envelope covering filesystem, network, subprocess, and secrets access. The envelope MUST default-deny any dimension not explicitly declared. A skill MUST NOT be able to widen its own declared permissions at runtime. Prose-only skills (no executable payload) MUST NOT declare tool access, hooks, or network access.

#### Scenario: Undeclared permission is denied

- GIVEN a skill's envelope does not declare network access
- WHEN the envelope is evaluated for a network-requiring host action
- THEN network access is denied by default and the gap is reported

#### Scenario: Prose-only skill cannot escalate

- GIVEN a skill has no executable payload
- WHEN its declared envelope requests tool, hook, or network access
- THEN the skill fails schema validation and is rejected before activation

### Requirement: Skill Trust Tiers and Provenance

Every skill MUST declare exactly one provenance tier: T1 (first-party, Cerebro-authored, release-key signed), T2 (third-party registry, never trusted by default, candidate only), or T3 (local user pack, local-only, never redistributed). A valid signature MUST establish attribution only and MUST NOT be treated as evidence of safety.

#### Scenario: Signed skill is not auto-trusted for safety

- GIVEN a T2 skill carries a valid publisher signature
- WHEN the gate evaluates it
- THEN the signature is accepted as provenance evidence only and the skill still requires the full candidate gate before activation

### Requirement: Candidate Lifecycle and Approval Gate

Every skill MUST pass, in order: candidate ingestion, static analysis, sandbox verification (executable payload only), human approval bound to the exact content digest, signature, and activation. Approval MUST be invalidated by any change to the content digest. A failed gate at any stage MUST leave the currently active skill set unchanged and MUST NOT expose the rejected candidate through dispatch. Sandbox verification observes only executed behavior; natural-language static analysis of prose MUST NOT be relied upon as a safety guarantee. The permission envelope, not prose analysis, is the effective mitigation against malicious instruction text embedded in a skill body.

#### Scenario: Digest change invalidates approval

- GIVEN a skill was approved and signed at digest D1
- WHEN its content changes to digest D2
- THEN the prior approval no longer applies and the skill re-enters the candidate gate from static analysis

#### Scenario: Sandbox limit is disclosed, not assumed

- GIVEN a prose-only skill contains no executable payload
- WHEN the gate evaluates it
- THEN sandbox verification is not claimed to cover prose safety, and approval relies on the declared permission envelope plus human review

### Requirement: Currency, Advisories, and Demotion

An expired or stale skill (per declared `reviewed_at`/`expires_at`/`freshness_days`) MUST be demoted from trusted to candidate; it MUST NOT continue to be dispatched as trusted. Detected upstream drift MUST surface as an advisory attached to the affected skill ref, paired with a host action, and MUST NOT mutate the approved skill body.

#### Scenario: Expiry demotes without deletion

- GIVEN a trusted skill's `expires_at` has passed
- WHEN dispatch evaluates the registry
- THEN the skill is treated as a candidate, not dispatched as trusted, and remains available for re-approval

#### Scenario: Drift produces an advisory, not a silent update

- GIVEN an approved skill's upstream source has since changed
- WHEN drift is detected
- THEN an advisory and host action are attached to the skill ref while the approved body remains byte-identical

### Requirement: Host-Delegated Authoring on Gap Detection

Cerebro has no generative model in-process and MUST NOT author a skill body. On detecting a capability gap, Cerebro MUST emit a bounded drafting host action rather than generate content itself. Any draft the host returns MUST be ingested only as a candidate, subject to the full candidate lifecycle gate; it MUST NOT be activated directly.

#### Scenario: Gap triggers a host action, not generation

- GIVEN no registered skill covers a matched task domain
- WHEN Cerebro detects the gap
- THEN it returns a bounded drafting host action and performs no content generation itself

#### Scenario: Host-authored draft still passes the full gate

- GIVEN a host agent returns a drafted skill body in response to a drafting host action
- WHEN Cerebro ingests the draft
- THEN it enters as a candidate and must pass static analysis, sandbox verification, human approval, and signature before activation

### Requirement: Read-Only Boundary and CLI-Confined Mutation

The MCP tool surface MUST remain read-only. It MUST NOT install, enable, authenticate to, or execute a skill. All mutation — candidate ingestion, approval, signing, activation, rollback — MUST occur only through the human-invoked CLI.

#### Scenario: MCP call cannot activate a skill

- GIVEN a client calls `investigate_work` or `read_evidence`
- WHEN the request references a candidate skill
- THEN no activation, installation, or execution occurs as a side effect of the call

### Requirement: Host-Side Availability and Digest Divergence

Because Cerebro dispatches to a skill the host loads through its own trust path, dispatch MUST NOT assume the referenced skill is present or unmodified on the host. Every skill ref MUST carry the approved version and content digest. When a referenced skill is reported absent, or present at a version or digest other than the approved one, Cerebro MUST report a typed gap and a host action, and MUST NOT represent the divergent copy as approved. Approval is bound to a digest; a body Cerebro never verified MUST NOT inherit that approval.

#### Scenario: Referenced skill is not installed on the host

- GIVEN dispatch selects an approved skill the host has not installed
- WHEN the result is assembled
- THEN a typed gap and an install-oriented host action are returned and the skill is not presented as available

#### Scenario: Host copy diverges from the approved digest

- GIVEN a referenced skill is present on the host at a digest other than the approved one
- WHEN Cerebro evaluates the ref
- THEN the divergence is reported and the local copy is treated as unapproved, never as the approved skill

### Requirement: Non-Destructive Gate Failure and Two-Tool Backward Compatibility

A failed gate at any stage MUST leave the active skill set completely unchanged and MUST NOT partially apply a rejected candidate. Skill dispatch MUST ride the existing two-tool public contract (`investigate_work` and `read_evidence`) and MUST NOT introduce a third public tool or alter either tool's existing schema in a way that breaks previously valid requests.

#### Scenario: Non-destructive gate failure

- GIVEN a candidate skill fails static analysis or sandbox verification
- WHEN the gate rejects it
- THEN the active skill set remains byte-identical to its pre-evaluation state and no partial activation occurs

#### Scenario: Backward-compatible two-tool surface

- GIVEN a client built before skill-dispatch existed calls `investigate_work` or `read_evidence` with a previously valid request
- WHEN skill-dispatch is active in the deployment
- THEN the call succeeds with the same schema-conformant behavior as before, with skill refs appearing only as additional optional evidence entries
