# Knowledge Corpus Lifecycle Specification

## Purpose

Delta for the `knowledge-corpus-lifecycle` capability: extend the existing signed-registry and atomic-activation machinery to cover signed skill packs. This delta MUST NOT restate or amend the promoted requirements in `openspec/specs/knowledge-corpus-lifecycle/spec.md`; it is additive and, at archive time, merges alongside them as a new dated revision section.

## Requirements — Skills Layer Addition (2026-07-25)

### Requirement: Signed Skill Pack Schema and Fail-Closed Loading

Skill packs MUST use a closed schema (`SkillPolicy` and pack container) validated the same way as existing source registries and domain packs. Loading MUST reuse the existing Ed25519 release-manifest verification and sha256 digest pinning, and MUST reuse the existing hardened YAML/data parsing that rejects anchors, aliases, and implicit resolvers. Loading MUST fail closed on the same class of conditions already enforced for registries and domain packs: untrusted signer, integrity mismatch, incompatible version, expired review window, expiry, staleness, unsupported domain, and unsupported jurisdiction. Local unsigned user packs (T3) MUST be permitted only under an explicit local-only mode using the existing `allow_unsigned_local` flag, and MUST NOT be redistributed or promoted to a shared registry.

#### Scenario: Fail-closed on integrity mismatch

- GIVEN a skill pack's content digest does not match its declared sha256
- WHEN the pack is loaded
- THEN loading is rejected with a typed integrity error and no partial pack state is registered

#### Scenario: Unsigned pack requires explicit local-only mode

- GIVEN a skill pack has no valid signature
- WHEN it is loaded outside explicit local-only mode
- THEN loading fails closed with an unsigned-regulated-pack error, matching existing `packs.py` behavior

#### Scenario: Hardened parsing rejects unsafe constructs

- GIVEN a skill pack file contains YAML anchors, aliases, or an implicit resolver construct
- WHEN it is parsed
- THEN parsing is rejected before any schema validation proceeds

### Requirement: Atomic Skill Set Activation, Retention, and Rollback

Skill set activation MUST mirror the existing candidate-build-and-promote lifecycle: build the candidate skill set beside the active one, run `validate_candidate`-equivalent checks, serialize promotion under a write lock, and perform an atomic pointer swap (`os.replace`-equivalent) so readers always observe one complete, compatible skill set. At least two prior generations MUST be retained to support `rollback_active`/`recover_active`-equivalent operations. A failed validation or promotion MUST leave the previously active skill set fully queryable and unchanged.

Web-driven freshness for skill packs is explicitly OUT of scope for this delta. It requires an operator-defined destination allowlist contract and production `ResearchDeps` construction, both deliberately absent today; the absence of a default allowlist is a safety posture, not an omission, and MUST NOT be worked around by this change.

#### Scenario: Atomic promotion under concurrent reads

- GIVEN a reader is actively serving skill refs from the current active skill set
- WHEN a validated candidate skill set is promoted
- THEN the reader completes its current request against one consistent generation and subsequent requests observe the new generation only after the atomic swap completes

#### Scenario: Rollback restores prior skill set without rebuild

- GIVEN a newly promoted skill set is found defective
- WHEN rollback is invoked
- THEN a previously retained generation is restored as active without rebuilding from source packs

#### Scenario: Non-destructive validation failure

- GIVEN a candidate skill set fails validation
- WHEN promotion is attempted
- THEN no active pointer changes, the prior active skill set continues serving reads uninterrupted, and the failure is reported with actionable diagnostics

#### Scenario: Backward-compatible activation contract

- GIVEN existing corpus-lifecycle consumers rely on the current candidate/validate/promote/rollback contract
- WHEN skill packs are added as a new artifact type under this same lifecycle
- THEN the existing index promotion, retention, and rollback behavior for non-skill corpus artifacts is unchanged
