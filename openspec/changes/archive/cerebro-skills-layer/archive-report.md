# Archive Report: Cerebro Skills Layer

**Archived:** 2026-07-25 · **Branch:** `feature/cerebro-skills-layer` · **Base:** `327b153`

## What shipped

Cerebro dispatches to a vetted skill instead of flooding an agent's context with every skill it owns. `investigate_work` returns skill refs carrying provenance and a declared permission envelope; the full lifecycle — ingest, analyse, approve, sign, activate, roll back, inspect, prune — runs from the terminal; three first-party skills ship signed and active on install.

| | |
|---|---|
| Units | 21 (planned as 15; split on measurement, never on scope) |
| Tests | 499 → **856** |
| Requirements promoted | 14 new, across 3 capabilities (`skill-dispatch` is new) |
| Licence | Apache-2.0 |

## The two decisions everything else follows from

**D1 — Cerebro is a pure dispatcher and never emits obeyable instruction text.** It returns a skill's identity, provenance, permission envelope and rationale; the body lives in the host's own skill directory and is loaded through the host's own trust path. This is enforced by the SCHEMA rather than by the service layer: `SkillPolicy` has no body field, so there is no code path through either tool that could return one.

**D2 — prose-only in v1.** `SkillPolicy.payload` is `Literal["prose"]`, making an executable payload inexpressible and the sandbox stage unreachable rather than faked. The repository has no isolation primitive, and a weak sandbox that trust later leans on is worse than none.

## The design principle that recurs

Where a thing must never happen, it was made **inexpressible** rather than checked. `"*"` is outside the hostname character class. `Identifier` admits no shell metacharacters. `schemes` is `Literal["https"]`. `AnalysisReport` has no field that could carry a verdict. `SkillPolicy` has no body field. In each case the reviewer has nothing left to verify, because there is no value to reject.

Where that was not possible, a check was used and **labelled as a check** — never dressed up as a structural guarantee.

## Divergence from spec

One, accepted and amended rather than implemented: claim-type gating for skill dispatch. The classifier's claim types describe the shape of an assertion; a skill is a procedure. Decided on reversibility — adding the field later is backward-compatible, removing it later invalidates every signed manifest — with a revisit criterion written into the amended requirement. See `verify-report.md`.

## Defects this change found in shipped code

Two production time bombs (one pre-existing, one introduced by this change and caught by it), a security hole in a control-character check that scanned bytes instead of parsed values, an untyped crash in `prune_skillset` on a fresh install, and a contract change that would have altered every pre-skills client's response silently. All fixed. Full detail in `verify-report.md`.

## Method note, recorded because it is the reusable part

Every unit ran a **falsifiability probe**: the invariant just written was broken on purpose and the suite re-run to confirm the correct tests failed, then reverted. Roughly one probe in three found something. Two are worth carrying forward: removing a guard once **wrote a live private key into the package data directory** rather than merely failing an assertion, and injecting a `body` field into a disclosure proved the no-leak test detects a leak rather than restating that a field does not exist. A probe that causes the harm is worth more than one that trips an assert.

Estimation calibrated from 2.1x to roughly 1.0x over twenty-one units by counting deletions and doubling test estimates.

## Deliberately not done

- Executable payloads and the sandbox stage (deferred with D2).
- Web-driven freshness for skill packs — needs an operator allowlist that does not ship by default.
- Skills for the other six classifier domains; only `programming` is reconciled.
- Windows validation. `index.py` is POSIX-only.

## State at archive

`verify_legacy_baseline.py` `status: pass, errors: []`. Pins `uv.lock 3c83d9eb` and `cerebro.db 03e9f3c5` unchanged. The legacy engine, its database and the `Cerebro-IA/` vault were never modified. `origin/main` untouched at `327b153`; all work is on the feature branch.
