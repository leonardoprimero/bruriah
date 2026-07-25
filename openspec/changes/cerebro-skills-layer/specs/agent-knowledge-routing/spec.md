# Agent Knowledge Routing Specification

## Purpose

Delta for the `agent-knowledge-routing` capability: reconcile the classifier domain vocabulary with skill-pack domains so skill dispatch can fire, and preserve the two-tool public contract as skill refs are added as evidence. This delta MUST NOT restate or amend the promoted requirements in `openspec/specs/agent-knowledge-routing/spec.md`; it is additive and, at archive time, merges alongside them as a new dated revision section.

## Requirements — Skills Layer Addition (2026-07-25)

### Requirement: Domain Vocabulary Reconciliation and Domain-Gated Capability Surfacing

The classifier `Domain` vocabulary MUST be reconciled with `DomainPack.domains` so that at least one classifier domain resolves to `domain_supported=True` against a bundled pack, enabling skill dispatch to fire for supported domains. Skill surfacing MUST be domain-gated in addition to claim-type gated. Existing capability surfacing MUST retain its present claim-type gating: re-gating it by domain would break assertions that pin verified behavior, and the non-destructive rule takes precedence over the broader reading. This specification prescribes behavior only; the mechanism — extending an existing record type versus introducing a sibling record type and registry — is a design decision constrained by the rule that existing verified records and the tests pinning them MUST NOT be broken. This reconciliation MUST preserve the existing unsupported-domain abstention behavior: any classifier domain without an applicable approved pack MUST continue to resolve `domain_supported=False` and MUST continue to yield `route_only` or `abstained`, never a fabricated match. Verification MUST include falsifiable negative controls: a test fixture mutation (e.g., altering a pack's declared domain or a classifier domain value) MUST flip the test outcome, so the reconciliation is pinned against real behavior rather than a tautological assertion.

#### Scenario: Reconciled domain enables dispatch

- GIVEN a classifier domain now shares a member with a bundled pack's declared domains
- WHEN investigation evaluates domain support
- THEN `domain_supported=True` is returned and skill dispatch is eligible to fire for that domain

#### Scenario: Unsupported domain still abstains

- GIVEN a classifier domain has no applicable approved pack after reconciliation
- WHEN investigation evaluates domain support
- THEN `domain_supported=False` is returned and the outcome remains `route_only` or `abstained`

#### Scenario: Negative control pins real behavior

- GIVEN a test asserts a classifier domain resolves against a pack
- WHEN the pack's declared domain value is mutated to no longer match
- THEN the test fails, proving the assertion depends on genuine vocabulary alignment rather than an always-true condition

#### Scenario: Non-destructive reconciliation

- GIVEN the existing classifier domain enum and pack domain declarations
- WHEN vocabulary reconciliation is applied
- THEN no existing classifier domain value or pack domain declaration is deleted, only extended or aligned, and prior classification outputs for already-supported domains are unchanged

### Requirement: Two-Tool Surface Preservation under Skill Dispatch

Skill dispatch MUST ride the existing two public tools, `investigate_work` and `read_evidence`, expressed as additional evidence records and typed host actions. No third public tool MUST be introduced to expose skill dispatch, per the promoted `Future Surface Extension Gate` and `Portable Two-Tool Public Contract` requirements, which remain unchanged and in force.

#### Scenario: Skill refs surface through investigate_work

- GIVEN a task matches a registered skill
- WHEN `investigate_work` assembles its result
- THEN the skill ref appears within the existing `evidence`/`host_actions` result shape, with no new top-level public tool invoked

#### Scenario: Backward-compatible schema

- GIVEN a client validated against the pre-skills-layer `investigate_work` output schema
- WHEN skill dispatch adds skill refs to the response
- THEN the additions are optional fields within the existing schema and previously valid client parsing continues to succeed
