# Verification Report: Cerebro Skills Layer

**Date:** 2026-07-25 · **Verdict:** PASS with one accepted, amended divergence · **Suite:** 856 tests

## Method

Verification was performed **inline and by execution**, not by delegation and not by reading. Each hard invariant was exercised against the running system independently of the test suite, on the reasoning that a suite is evidence a test noticed something, while executing the system is evidence the system does it. Delegating this step was rejected deliberately: two sub-agents earlier in this change reported clean completion while carrying real defects, and self-validation against one's own implementation is precisely where that failure mode is most expensive.

## Requirements

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 1 | Signed Skill Pack Schema and Fail-Closed Loading | PASS | `load_skill_pack` reuses the same Ed25519/digest/hardened-parse primitives as `load_pack`; one crypto path, not two. Fail-closed matrix in `test_skills.py`. |
| 2 | Atomic Skill Set Activation, Retention, and Rollback | PASS | flock-serialised promotion, `retain=2`, rollback revalidates without rebuilding from source packs; concurrency test fails 10/10 without the lock. |
| 3 | Domain Vocabulary Reconciliation | PASS | Executed: supported domains == `{"programming"}` exactly; the other six abstain. |
| 4 | Two-Tool Surface Preservation | PASS | Executed: exactly `investigate_work` and `read_evidence`. |
| 5 | Bounded Deterministic Skill Dispatch | **PASS, amended** | Executed: identical inputs → identical output, ordered by `skill_id`, ceiling 5. Claim-type gating removed — see divergence below. |
| 6 | No Obeyable Instruction Emission | PASS | Executed: `SkillPolicy` has no body field; a body cannot reach either tool because none exists to reach it. Probe injecting a `body` field into the read disclosure failed the guard test. |
| 7 | Structured Default-Deny Permission Envelope | PASS | Executed: default envelope grants nothing; `"*"` as a host and a shell string in `subprocess.programs` are both **inexpressible**, not merely rejected. |
| 8 | Skill Trust Tiers and Provenance | PASS | Executed: three tiers; unsigned packs admit local-tier skills only (`unsigned_nonlocal_pack`). |
| 9 | Candidate Lifecycle and Approval Gate | PASS | Executed: `AnalysisReport` cannot express a verdict; `approve_candidate` has no parameter through which a report could be supplied. |
| 10 | Currency, Advisories, and Demotion | PASS | Executed: an aged pack reports `expired` **and the server still starts**; demotion leaves the approved body and digest byte-identical. |
| 11 | Host-Delegated Authoring on Gap Detection | PASS | Drafting fires only when nothing trusted remains; the brief carries no generated content. Cerebro has no generative model. |
| 12 | Read-Only Boundary and CLI-Confined Mutation | PASS | Executed: `service.py` contains zero write operations. All mutation lives in human-invoked CLI subcommands. |
| 13 | Host-Side Availability and Digest Divergence | PASS | Three availability states; comparison is on the DIGEST, never the version label; duplicate host entries cannot upgrade their own state. |
| 14 | Non-Destructive Gate Failure and Backward Compatibility | PASS | Executed: a pre-skills client receives no `envelope` field at all; golden byte-identical against a capture taken before the change. |

## Divergence: claim-type gating (accepted, spec amended)

Requirement 5 originally listed `claim type` as a dispatch input. It is **not implemented**, and the spec has been amended rather than the code.

The classifier's claim types describe the shape of an ASSERTION; a skill is a PROCEDURE. Every first-party skill applies under all three, so a gate they all satisfy filters nothing while implying precision it lacks — the same fabricated correspondence `lookup.py` already refuses for `SourcePolicy.claim_types`.

The decision was taken on **reversibility, not certainty**: the evidence is three skills of similar character. Adding the field later is backward-compatible; removing it later invalidates every signed manifest. A revisit criterion is written into the amended requirement (re-evaluate past twenty authored skills).

## Defects found and fixed during implementation

Recorded because they are the substance of the verification, not a footnote.

1. **A production time bomb, twice.** `research-policy` would have stopped `serve` on 2026-08-23 (29 days out) via `freshness_days: 30`. Fixed in 6b. Then **reintroduced by me** in Unit 11 via the bundled skill pack, which would have stopped `serve` on 2027-07-26. Fixed in Unit 10 by demoting rather than refusing to boot.
2. **A security hole in the control-character check.** It scanned raw bytes, but `json.dumps` escapes such characters as ASCII, so a bidirectional override could have entered approved prose unseen while the check looked thorough. Moved to the parsed values.
3. **`prune_skillset` crashed untyped** on a fresh install, because `serialized` creates its lock beside a pointer whose directory did not exist.
4. **A contract change that would have shipped silently.** A plain optional `envelope` field would have added `"envelope": null` to every record of every pre-skills client. Fixed with a serializer that omits it.
5. **A gap in my own test coverage**, found by a probe: the generation path's Ed25519 check was unpinned because `digest_mismatch` and `unknown_signer` fired earlier and masked it.

## Environmental hazard (not a code fault)

The LaunchAgent `com.leguillo.cerebro-reindex` runs every 1200 seconds and rewrites `cerebro.db`. When it lands mid-run, tests asserting the legacy database is unmutated fail and the suite slows from ~25s to ~50s. The rebuild is content-identical (pin `03e9f3c5` holds), so it is a RACE, not corruption. Anyone contributing on a machine with that agent loaded will hit it.

## Non-destructive verification

`verify_legacy_baseline.py` reported `status: pass, errors: []` before and after every unit. Pins `uv.lock 3c83d9eb` and `cerebro.db 03e9f3c5` unchanged throughout. `cerebro.py`, the legacy database, and the `Cerebro-IA/` vault were never modified. All verification ran in an external `UV_PROJECT_ENVIRONMENT`.
