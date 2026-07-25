# Proposal: Cerebro Skills Layer — Vetted Capability Dispatch

## Product Promise

Installing Cerebro installs *capabilities*. Given a task, Cerebro names the one or few skills that actually apply — instead of flooding the host with every skill's frontmatter. It never emits obeyable instructions, never installs or executes a skill, and nothing generated or fetched becomes a trusted skill without passing a gate.

## Decision Record

**D1 — Pure Dispatcher (approved 2026-07-25).** Cerebro MUST NOT emit obeyable instruction text. `investigate_work` returns skill refs with rationale, permission envelope, and provenance; `read_evidence` discloses skill *metadata*, never a body to be obeyed. The trusted skill body lives in the host's own skill directory and is loaded through the host's own trust path.

*Rejected — trusted-instruction evidence tier:* would require amending the promoted normative requirement `Instruction and Evidence Separation` (`openspec/specs/agent-knowledge-routing/spec.md:173`) and would construct exactly the channel an attacker wants — a path by which text becomes obeyed instruction. It is also unenforceable: the host, not Cerebro, decides what to obey.

*Rejected — tiered hybrid:* same amendment, attack surface proportional to tier.

*Consequences:* (a) the public surface stays exactly two tools, so `Future Surface Extension Gate` (`spec.md:253`) needs no evaluation; (b) all mutation — ingesting, approving, activating a skill — lives in the human-invoked CLI, following the established precedent that `cerebro-mcp init`/`index` write to disk (`cli.py:221`, `cli.py:248-252`) while the MCP tool surface stays read-only.

## Scope

**Goals:** deterministic domain-gated skill dispatch through the existing two-tool surface; a machine-checkable default-deny permission envelope; a candidate→approval→signature→activation gate mirroring the immutable-index lifecycle; per-user adaptation via local packs; first-party skill packs shipped signed.

**Non-goals:** Cerebro authoring skills itself (no generative model exists in-process); executing, installing, enabling, or authenticating a skill; a third public tool; machine learning that adapts without human approval; web-driven freshness and executable skill payloads (both deferred — see Deferred Work); making the repository public; any modification to the live legacy engine (`cerebro.py`, `cerebro.db`), the `Cerebro-IA/` vault, or the LaunchAgent reindex.

## Verified Preconditions

These were verified against source at HEAD `327b153`, not inferred. Each blocks the product promise and is therefore in scope.

1. **No generative model exists in-process.** `pyproject.toml:6-17` declares `fastembed==0.8.0` (local ONNX embeddings), `mcp`, `sqlite-vec`; no LLM/completion dependency or call site exists anywhere under `src/cerebro_router/`. Cerebro therefore cannot author a skill. Authoring becomes: Cerebro detects the gap → emits a bounded drafting `HostAction` → the host's agent drafts → Cerebro ingests the draft as a **candidate** → the gate runs. This keeps Cerebro deterministic and auditable rather than judge and party.
2. **Domain vocabulary is disjoint, so dispatch cannot currently fire.** `classify.py` `Domain` values share no member with `DomainPack.domains`; the only bundled pack declares `domains=["software-research"]`, so `lookup.discover()` returns `domain_supported=False` for every classifier domain — pinned by `test_real_bundled_pack_resolves_no_domain_sources_for_any_classifier_domain`. Additionally `CapabilityPolicy` has no domain field, so capabilities are claim-type gated (`lookup.py:134`), not domain gated: today either all surface or none do. Reconciling this vocabulary is a prerequisite for skill dispatch, and must preserve existing abstention behavior.
3. **Live research is dormant and unallowlisted by design.** `platform.load_deps` never constructs `ResearchDeps` (`service.py:80`), and no SSRF allowlist is bundled anywhere (`fetch.py`). The archive report records this as deliberate: a default allowlist would be an egress hole. Web freshness therefore requires an operator-defined allowlist contract first and is deferred out of this change.

## Preserved Foundations

The Skills Layer extends, and does not reopen, the archived router. Reused as-is: Ed25519 release-manifest verification, sha256 digest pinning, hardened YAML loading (anchors/aliases and implicit resolvers rejected), and the seventeen fail-closed `PackError` codes covering trust, integrity, version compatibility, review window, expiry, freshness, domain, and jurisdiction (`packs.py`). Reused as the activation template: candidate build → `validate_candidate` → flock-serialized `promote_candidate` → atomic `os.replace` pointer swap → `retain=2` with `rollback_active`/`recover_active` (`index.py`). Reused for per-user packs: the existing `allow_unsigned_local` flag with `signature_required`/`unsigned_regulated_pack` fail-closed behavior (`packs.py:156`).

Dispatch selection remains pure and deterministic — boolean set membership over a signed registry, no model, no scoring (`lookup.py`) — so *which* skill is dispatched is not influenceable by corpus content. This non-injectability is a security property to be preserved, not incidental.

## Skill Provenance Tiers

| Tier | Source | Trust | Gate |
|---|---|---|---|
| T1 | First-party, Cerebro-authored, release-key signed, shipped in the wheel | Only tier trusted by default on install — still default-deny permissions | Authorship review + signature |
| T2 | Third-party open-source registries | Never trusted; candidate only | Provenance (repo + pinned commit digest + license) + full gate |
| T3 | Local user packs (lawyer ≠ programmer ≠ designer) | Local only, never redistributed | Human approval is the sole gate; unsigned via `allow_unsigned_local` |

T1 seed material: the 60 curated `.md` notes under `Cerebro-IA/03-Skills/` (Architecture, Backend, Claude-Code, Custom, DevOps, Frontend, Testing), already indexed. Authoring high-quality first-party skills is substantive work, not a side effect of this change.

T2 default is **reference with pinned digest**, not vendoring: vendoring transfers redistribution and licensing obligations and adds staleness. Vendor only what has been reviewed and re-signed.

## Trust Model

Trust is multidimensional; a signature answers only *who*.

| Axis | Mechanism | Actual strength |
|---|---|---|
| Provenance | Ed25519 signature, publisher, pinned commit digest | Attribution — **not** safety |
| Integrity | sha256 digest pinning | Strong, cheap |
| Permission envelope | Structured default-deny: filesystem, network, subprocess, secrets | **Strong** |
| Human approval | Approval record bound to the digest; any edit invalidates it | **Strong** |
| Currency | `reviewed_at`/`expires_at`/`freshness_days` → **demotion to candidate** | Already exists, already fails closed |

The gate is: candidate → static analysis → sandbox (executable payload only) → human approval → signature → activation.

**Stated limit.** A sandbox verifies only what *executes*. Whether prose will induce a host agent to exfiltrate secrets is a semantic property, not an observable behavior, so natural-language analysis MUST NOT be treated as a safety guarantee. Design consequences: prose and executable payload are structurally separated; prose-only skills MUST NOT declare tools, hooks, or network access; a skill MUST NOT be able to widen its own permissions. The effective mitigation against malicious prose is the host's permission envelope, not text analysis. Prose-only skills are therefore a low tier with cheap approval; skills carrying executable payload are a high tier requiring static analysis, sandbox verification, and expensive approval.

**Freshness never rewrites trust.** Upstream drift produces an *advisory* attached to a skill ref (approved version, upstream version, digest divergence) surfaced as a gap plus `HostAction`. Expiry demotes a trusted skill to candidate. Neither path mutates an approved skill body.

## Capabilities

### New Capabilities
- `skill-dispatch`: domain-gated deterministic selection, skill refs as evidence, permission-envelope disclosure, gap detection and drafting host actions, bounded ref counts.

### Modified Capabilities
- `knowledge-corpus-lifecycle`: skill pack schema, candidate ingestion, approval records, signature, atomic activation and rollback.
- `agent-knowledge-routing`: domain vocabulary reconciliation and domain-gated capability surfacing (must not weaken existing abstention).

## Review Slices

Each slice targets ≤400 changed lines with tests in the same unit, per `openspec/config.yaml`.

| Slice | Deliverable |
|---|---|
| 1 | `SkillPolicy`/skill-pack closed schemas; signed-load path reusing `packs.py` primitives; fail-closed codes |
| 2 | Structured default-deny permission envelope replacing free-text `permissions`/`network_access`/`data_access` |
| 3 | Domain vocabulary reconciliation; domain-gated capability/skill surfacing preserving abstention |
| 4 | Skill registry and deterministic dispatch; skill refs as evidence; metadata-only disclosure through `read_evidence` |
| 5 | CLI lifecycle: candidate ingest, static analysis, approval bound to digest, signature, atomic activation, rollback |
| 6 | Prose-only payload invariant and structural static analysis with advisory-only findings |
| 7 | First-party pack authoring seeded from `03-Skills/`; packaging |
| 8 | Advisory/expiry demotion; gap-detection drafting host action |

This table is indicative. The approved `design.md` supersedes it with a ten-unit sequencing that adds two preparatory extraction units (`pointer.py`, `packs.py` helpers) before any skill code lands.

## Scope Revision — 2026-07-25

**Prose-only skills in v1; executable payloads deferred.** `SkillPolicy.payload` is `Literal["prose"]`, so an executable payload is inexpressible and a candidate carrying one is rejected `payload_unsupported` — refused, never silently passed. Consequently the sandbox stage is unreachable and the original sandbox slice drops out.

Rationale: the repository contains no isolation primitive and exactly one `subprocess.run` in the entire tree (`evaluation.py:677`, in the evaluation harness), and only Darwin/POSIX activation is validated. Building a sandbox here would be an unverifiable security claim, and a weak sandwich that trust later leans on is worse than none — it invites approving executables while believing they are contained. Making executable payloads inexpressible filters more strictly, not less: nothing executable can enter at all.

Cost, stated plainly: v1 skills carry instructions and checklists only — no bundled scripts, hooks, or tools. The strong gates remain the signed-pack permission envelope and human approval bound to the digest; structural static analysis yields reviewer advisories, never a safety verdict.

## Deferred Work

**Web-driven freshness.** Prerequisite: an operator-defined SSRF allowlist contract and production `ResearchDeps` construction, both deliberately absent today. Absence of a default allowlist is a safety posture, not an omission.

**Executable skill payloads and sandbox verification.** Prerequisite: an isolation primitive the repository does not currently have, plus cross-platform validation. Deferred by the 2026-07-25 scope revision above.

## Boundaries, Gates, and Rollback

The MCP tool surface remains read-only and exactly two tools. Skill bodies never traverse Cerebro as obeyable instruction. All mutation is human-invoked CLI. Activation is atomic with retained rollback; a failed gate leaves the active skill set unchanged. The preservation baseline `scripts/verify_legacy_baseline.py` MUST pass before and after every slice; `uv.lock` stays pinned `3c83d9eb…`, legacy `cerebro.db` stays pinned `03e9f3c5…`. Verification runs in an external `UV_PROJECT_ENVIRONMENT`, never the registered `.venv`.

## Affected Areas and Dependencies

`cerebro-retrieval/src/cerebro_router/{packs,registries,lookup,classify,contracts,service,cli,platform}.py`, new skill-layer modules, `tests/`, and new signed first-party pack data. Read-only inputs: `Cerebro-IA/03-Skills/`. No change to the legacy engine, the vault, the LaunchAgent, or repository visibility.

## Risks

Natural-language static analysis is weak and must not be presented as a guarantee. Domain vocabulary reconciliation risks regressing verified abstention tests and requires falsifiable negative controls. Third-party redistribution carries licensing obligations. Skill sprawl can re-inflate host context, so dispatch must cap returned refs — the dispatcher's value is bounded output, and an unbounded dispatcher is the failure mode it exists to prevent. First-party pack quality, not mechanism, determines whether the install-time promise is real.
