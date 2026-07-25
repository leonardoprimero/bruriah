# Tasks: Cerebro Skills Layer

Citation legend: `S-` = `specs/skill-dispatch/spec.md`, `A-` = `specs/agent-knowledge-routing/spec.md`, `K-` = `specs/knowledge-corpus-lifecycle/spec.md`, `D-` = `design.md`.

Line estimates are `impl + tests`. They are derived from measured sizes of the analogous existing modules (`index.py` 674, `packs.py` 207, `contracts.py` 152, `lookup.py` 166, `route.py` 157, `service.py` 425, `platform.py` 212, `cli.py` 416; `test_index.py` 530, `test_service.py` 472, `test_cli.py` 397, `test_lookup.py` 269, `test_platform.py` 209, `test_packs.py` 169, `test_contracts.py` 155), not from guesswork. Where an estimate is unavoidably speculative it is marked *(est.)*.

## Mandatory Controls

Apply to every unit without exception.

- Verification runs ONLY in an external environment: `UV_PROJECT_ENVIRONMENT=<path> uv sync --locked`. NEVER the registered `.venv` (`VIRTUAL_ENV` and `--python` alone are ignored by uv).
- `cerebro-retrieval/scripts/verify_legacy_baseline.py` MUST report pass before and after every unit.
- Pins MUST hold: `uv.lock` `3c83d9eb…`, legacy `cerebro.db` `03e9f3c5…`.
- Test command: `python -m pytest tests -q -p no:randomly`. Also run `uv lock --check` and `git diff --check`.
- NEVER modify `cerebro-retrieval/cerebro.py`, `cerebro-retrieval/cerebro.db`, the `Cerebro-IA/` vault, or the LaunchAgent. The legacy engine stays live and canonical.
- Tests and implementation ship in the SAME unit (`openspec/config.yaml` `rules.tasks`). `strict_tdd: false`, so tests-first is preferred but not enforced.
- No commits until Leo asks.

## Blocking Precondition — Signing Capability and Key Custody

- [x] **P.1 Resolve release-key custody before Units 6 and 11. RESOLVED 2026-07-25 by Leo.**

  **Custody**: the release key lives at `~/.config/cerebro/release-key.pem`, mode `0600`, outside the repository. Every tool addresses it BY PATH and never copies, prints, or writes it anywhere else. **Leo generates it himself** with `scripts/sign_pack.py keygen` and hands over only the public half — the private key never passes through an assistant's context or a session log. It is his root of trust; there is no reason for a model to have seen it.

  **`cerebro-release-test` is RETIRED.** The shipped product currently trusts a signer whose private half is unaccounted for, and the repository is headed for open source. Retiring it forces re-signing the bundled `research-policy` pack in the same unit, which is why both happen together in 6b.

  Stated limit, not softened: the key is written unencrypted, protected by `0600` and by living outside the repository. Adequate for a single maintainer on an encrypted disk, inadequate on a shared machine. Adding a passphrase is a small change to `generate_key` and a larger one to every caller of `sign_pack`.

  Original analysis follows.

- [ ] ~~P.1 (original)~~ Verified state: `src/cerebro_router/data/trust-roots.json` contains exactly one signer, `cerebro-release-test`, as a public key. There is **no private key, no signing script, and no test that constructs `Ed25519PrivateKey` or calls `.sign(...)` anywhere in the repository** — the only `.pem` files are `tests/fixtures/fetch/{testcert,testkey}.pem`, which belong to the TLS fetch tests. `packs.py` verifies signatures; nothing in-repo can produce one. The existing `research-policy.manifest.json` was therefore signed out of band.

  **Resolved 2026-07-25 — option (a) does not exist.** Traced the key's origin: `trust-roots.json` was added in commit `c4b38eb` ("add closed contracts, registries and signed domain packs"), the Slice 5A implementation commit. The signer is named `cerebro-release-test` and the private half was never persisted anywhere in the repository or in project memory. It was an ephemeral test key generated to sign the example pack during that slice. There is no existing release key to recover.

  Therefore the only path is: generate a genuine release keypair, add its public half to `trust-roots.json`, and build a signing tool (Unit 6). Two consequences to handle there, not before:
  - This modifies the ROOT OF TRUST and is security-relevant — whoever holds the private key can sign packs Cerebro will obey.
  - Private key custody is a decision for Leo. The key MUST NOT live in the repository. The signing tool takes it by path and never writes it into `data_dir`.
  - Decide whether `cerebro-release-test` stays a trusted signer at all, or is retired from `trust-roots.json` once a real key exists. Keeping a signer whose private half is unaccounted for is a standing risk; retiring it would invalidate the existing bundled `research-policy` pack, so that pack must be re-signed in the same unit.

  This is a decision for Leo, not an implementation choice. Do NOT work around it by weakening verification, by adding a permissive trust root, or by using `allow_unsigned_local` for what should be a signed first-party pack. [D-Architecture Decisions 1; K-Signed Skill Pack Schema and Fail-Closed Loading]

## Units

### Preparatory extractions (no new product behavior)

- [x] **1. `pointer.py` extraction from `index.py`.** DONE 2026-07-25. `size:exception` — see actuals below. Move the generic atomic-pointer and lock primitives to a new `pointer.py`: `_read_pointer` (index.py:349-372), `_write_pointer` (375-402), `_identity` (409-413), `_controlled_file` (422-442), `_identity_matches` (443-450), `_activation_lock` (469-478), `_serialized` (479-487). Parameterize the three index-specific couplings in `_read_pointer`: the entry key set (`{"database","build_id"}`, line 367), the traversal-checked name key (line 369), and the raised error type (`IndexLifecycleError`). Leave `_open_descriptor` (414-421) and `_validated_entry` (451-468) in `index.py` — they are SQLite/`BuildConfig`-specific. `index.py` imports the primitives and keeps its own error codes (`invalid_active_pointer`, `invalid_active_target`, `active_target_changed_during_validation`) unchanged.

  **Correction to design.md.** The design estimates "~250 moved lines" and warns the unit may not fit. Measured, the genuinely generic surface is **~105 lines**, rising to ~120 after parameterization. The unit fits one 400-line budget and does NOT need splitting.

  Files: `pointer.py` (new), `index.py`, `tests/test_pointer.py` (new).
  Estimate: 120 + 15 (index.py delta) + 150 tests = **~285**.
  **Actual: 593 changed lines** — `pointer.py` 158 new, `index.py` 22 insertions + 101 deletions = 123, `tests/test_pointer.py` 312 new. `index.py` 674 → 595.

  **`size:exception` with rationale.** 593 exceeds the 400-line budget. Net new *logic* is approximately zero: the 158 added and 101 deleted lines are the same primitives relocated verbatim, which a reviewer can confirm by diffing the moved blocks rather than re-reading them. The genuinely new content is the 312 test lines, itself under budget. Splitting further would mean shipping a module without its tests, which `rules.tasks` forbids.

  **Estimation calibration — apply to every remaining unit.** Two systematic errors: (a) I counted only insertions for `index.py` and ignored that deletions also consume review budget (15 estimated vs 123 actual); (b) test lines ran ~2x my estimate (150 vs 312). Remaining estimates in the forecast below should be read as roughly **2x low on tests** and as undercounting deletions in refactor units. Re-estimate before opening each unit.

  Verification (all passed): full suite green with **zero test files modified** — 499 → 526 passed (+27 new); `verify_legacy_baseline.py` `status: pass`, `errors: []` both before and after; `uv.lock` 3c83d9eb and `cerebro.db` 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean. Falsifiability probe run: deleting the traversal check made 4 new tests AND the pre-existing `test_index.py::test_active_target_rejects_escape_corruption_and_missing_file` fail, then was reverted — the tests pin real behavior, not a tautology.
  Rollback: revert; no on-disk format changed. [D-Architecture Decisions 4; K-Atomic Skill Set Activation, Retention, and Rollback]

- [x] **2. `packs.py` helper extraction + precedence test.** DONE 2026-07-25. Promote `_unique_pairs` (94-100), `_version` (88-91), `_StrictYamlLoader`/`_reject_yaml_aliases` (101-121), the pack-bytes parse path, and the manifest verification block to public helpers that `load_pack` then calls. Extraction-only: every `PackError` code and, critically, **check order** stays byte-identical.

  Known risk: the existing suite may not pin every failure-precedence ordering, so extraction could silently change which code surfaces first when two conditions fail together (e.g. `signature_required` vs `expired_pack`). Add a precedence test over deliberately overlapping-failure fixtures BEFORE refactoring, so it captures current behavior as the baseline rather than the post-refactor behavior.

  Files: `packs.py`, `tests/test_packs.py`.
  Estimate: 55 + 80 tests = **~135**. **Actual: 198** (packs.py 115 insertions + 37 deletions = 152; test_packs.py +46). 1.5x — better than Unit 1's 2.1x but still over. `packs.py` 207 → 239.

  Extracted to public names: `parse_version`, `MAX_PACK_BYTES`, `parse_pack_bytes`, `encode_pack`, `read_pack_bytes`, `verify_manifest`, `check_review_window`, `check_router_compatibility`, `check_version_floor`. `load_pack` now composes them in the identical order; the private `_StrictYamlLoader`, `_reject_yaml_aliases`, and `_unique_pairs` stay private because `parse_pack_bytes` is the only entry point a second loader needs. Removed an orphaned `_version` alias left by the rename.

  **Honest finding about this unit's own premise.** The precedence test was written before the refactor, as planned, and it does catch reordering — inverting `expired_pack`/`stale_pack` failed `test_precedence_expired_beats_stale` and `test_precedence_dates_beat_router_compatibility`. But the pre-existing `test_expiry_malformed_oversized_and_unsigned_regulated_fail_closed` ALSO caught it, so that particular ordering was already pinned and the stated risk was overstated for it. The precedence block's real added value is narrower than claimed: existing tests exercised each failure code individually, while this block pins which code wins when two conditions fail *together* — specifically schema-vs-manifest, unknown_signer-vs-digest_mismatch, digest-vs-signature, signature_required-vs-dates, router-vs-floor, floor-vs-domain, and domain-vs-jurisdiction, none of which had a combined-failure test before.

  Verification (all passed): 526 → **538 passed** (+12); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` resolved clean; `git diff --check` clean. No existing test assertion was modified — the precedence block is purely appended.
  Rollback: revert. [D-Architecture Decisions 1]

### Skill schema and storage

- [x] **3A. Skill schemas and permission envelope.** DONE 2026-07-25. New `skills.py` with `SkillPolicy`, `SkillPack`, and `PermissionEnvelope` plus `FilesystemAccess`/`NetworkAccess`/`SubprocessAccess`, all `ClosedModel` (`extra="forbid", strict=True`). Default-deny is represented as absence: every dimension defaults to an empty collection, empty means deny, and no wildcard or "all" token is expressible in the path/host patterns. `SkillPolicy.payload` is `Literal["prose"]`, making an executable payload inexpressible per the approved scope revision.

  Files: `skills.py` (new, 148), `tests/test_skills.py` (new, 191).
  Estimate: 130 + 170 tests = **~300**. **Actual: 339** — 1.13x, comfortably under the 400 budget and the closest estimate so far.

  Implementation notes worth carrying forward:
  - **No body field exists on `SkillPolicy`.** A skill record is a pointer (`body_locator`) plus the digest approval is bound to (`body_digest`), which is what structurally prevents the body from reaching the MCP surface. Enforcing decision D1 in the schema is stronger than enforcing it in the service layer.
  - **pydantic v2 `pattern` has no lookahead.** Its regex engine cannot express "no `..` segment", so `RelPath` is a character whitelist and the structural rules (no absolute path, no trailing slash, no empty/dot/dotdot segment) live in `FilesystemAccess.safe_paths`. A lookahead pattern would have silently failed rather than rejecting escapes.
  - **A broad grant is inexpressible, not merely forbidden.** `*` is outside `Hostname`'s character class, `Identifier` admits no space/slash/pipe/semicolon so a shell string cannot be smuggled into `subprocess.programs`, and `schemes` is `Literal["https"]`. "Allow everything" cannot be written even by a correctly signed pack.
  - `reject_executable_payload` raises its own `payload_unsupported` code rather than letting closed-model validation report a generic `malformed_pack`, which would read as a schema typo instead of an unsupported capability.
  - Currency is declared at pack level, matching `DomainPack`. Per-skill windows would suit third-party skills whose upstreams move independently; v1 accepts pack-granularity demotion. Revisit if T2 volume grows.

  Verification (all passed): 538 → **606 passed** (+68); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean; no existing file modified — both files are new.
  Falsifiability probe: disabling the network branch of `prose_only_envelope` failed exactly the two network cases and nothing else, then was reverted — the invariant gates, and the tests are not passing for an unrelated reason.
  Rollback: delete module. [S-Structured Default-Deny Permission Envelope; S-Candidate Lifecycle and Approval Gate; D-Architecture Decisions 2, 7]

- [x] **3B. Signed skill-pack loading and `SkillSet` registry.** DONE 2026-07-25. `load_skill_pack` reusing the Unit 2 helpers (Ed25519 manifest verification, digest pinning, hardened parsing, review-window/expiry/freshness/version checks), plus a frozen `SkillSet` built only from loaded packs, rejecting duplicate skill ids. Local unsigned T3 packs permitted only through the explicit `allow_unsigned_local` path.

  Files: `skills.py` (148 → 244), `tests/test_skills.py` (191 → 363). No fixture files needed — see below.
  Estimate: 110 + 190 tests = **~300**. **Actual: 268** (+96 impl, +172 tests) — 0.89x, the first unit UNDER estimate.

  Implementation notes:
  - `load_skill_pack` calls the SAME primitives `load_pack` calls (`read_pack_bytes`, `parse_pack_bytes`, `encode_pack`, `verify_manifest`, `check_review_window`, `check_router_compatibility`, `check_version_floor`). One Ed25519/digest/hardened-parsing path exists in this codebase, not two that can drift. Order mirrors `load_pack`: unreadable → oversized → parse → executable payload → schema → signature → review window → router compatibility → version floor. No domain or jurisdiction gate at load: skills are domain-gated at dispatch.
  - **New invariant `unsigned_nonlocal_pack`** — the skill analogue of `unsigned_regulated_pack`. Unsigned is a LOCAL-ONLY affordance; a first- or third-party skill has no provenance at all without a signature, so it must not ride in on the T3 exception. Falsifiability probe confirmed it is load-bearing.
  - `SkillSet` mirrors `registries.Registry` exactly — same construction discipline, same collision codes (`duplicate_pack_id`, `duplicate_skill_id`), same sort order, plus `resolve()` with exact-match-or-`None` semantics matching `resolve_capability` (never fuzzy, never fabricated).
  - **No signing fixtures were needed: the tests generate their own ephemeral Ed25519 keypair.** `cryptography` is already a dependency. This is worth carrying into Unit 6 — it demonstrates the signing tool is small (keygen, digest, one canonical string, one signature) and that P.1 never blocked *testing* the signed path, only *shipping* a bundled signed pack.

  **A wrong test fixture taught the real order.** My first `digest_mismatch` fixture replaced the pack with `{"pack_id":"tampered"}`, which is schema-invalid, so `malformed_pack` fired before the digest check ever ran. The code was right and the fixture was wrong. Fixed by tampering with a still-schema-valid pack, and the lesson was kept as its own test, `test_schema_validity_precedes_digest_verification`, so the ordering is now pinned rather than merely known.

  Verification (all passed): 606 → **630 passed** (+24); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean.
  Rollback: delete loader; schemas from 3A remain inert. [K-Signed Skill Pack Schema and Fail-Closed Loading; S-Skill Trust Tiers and Provenance]

**Unit 4A was re-estimated before opening and SPLIT**, as the forecast warned it might be. Measured, compile+validate+promote lands near 1,000 changed lines, not 330. The seam is natural: 4A.1 produces and verifies a generation with no pointer involved; 4A.2 makes one active. Each half is independently reviewable, independently testable, and has its own rollback.

- [x] **4A.1. Generation format, compile, and validate.** DONE 2026-07-25. `size:exception` — see actuals below. New `skillset.py`: the on-disk generation schema, `compile_skillset` (private tempfile then `os.replace`), and `validate_skillset` / `validate_skillset_bytes` (hardened re-parse, closed-model revalidation, digest recomputation, Ed25519 re-verification against injected trust roots, date re-checks with an injected `today`, unique ids, approval bound to the current body digest). Plus two additive extractions so the generation path reuses the existing crypto path rather than growing a second one: `packs.verify_manifest_bytes` and `skills.load_skill_pack_bytes`.

  **Design decision taken here, not in `design.md`: a generation is a SEALED BUNDLE of its source packs.** It embeds each pack's original bytes and its manifest verbatim, base64-encoded, and DERIVES the compiled records at validation time. The alternative — storing compiled records and pointing at source paths — cannot satisfy `K-Atomic Skill Set Activation`'s "rollback restores a retained generation *without rebuilding from source packs*", and would reduce `validate_skillset` to schema revalidation because digests and signatures cannot be recomputed without the original bytes. Deriving rather than storing the records means the file holds exactly one source of truth and the two can never disagree. Stated limit, deliberately not softened: **a generation is not a trust boundary.** It lives under `data_dir`, which anyone able to write there controls; validation defends against corruption and partial writes, exactly as `index.py` validates a candidate database. The security gates remain the pack signature, the envelope, and the approval record.

  Two further decisions worth carrying: (a) `GenerationPack` has **no** "was allowed to be unsigned" flag — a redundant flag could disagree with the manifest field beside it, so an absent manifest simply IS the unsigned case and `unsigned_nonlocal_pack` stays its only gate; (b) approvals are an **injected** `Mapping[skill_id, body_digest]` rather than embedded, so Unit 9A supplies storage without changing this invariant, and a body edit invalidates ACTIVATION rather than merely a record nobody re-reads.

  Files: `skillset.py` (new, 312), `tests/test_skillset.py` (new, 358), `packs.py` (239 → 251), `skills.py` (244 → 283).
  Estimate: 200 + 280 tests = **~480** (itself a re-estimate of the original ~330 for the whole of 4A). **Actual: ~750 changed lines** — `skillset.py` 312, `test_skillset.py` 358, extractions ~80. 1.6x.

  **`size:exception` with rationale.** 750 is 1.9x the budget, comparable to Unit 1's 593. Measured composition: `skillset.py` is 198 code lines with 66 lines of comment/docstring, so the reviewable *logic* is itself under budget; the 358 test lines are the falsifiable coverage `rules.tasks` requires in the same unit. Splitting further would mean either shipping the module without its tests, or cutting a single 198-line module in half so one unit compiles generations nobody validates. Neither reads better.

  **The extraction is behavior-preserving by construction, and the manifest is passed as a READER, not as bytes.** `load_skill_pack_bytes` takes `ManifestReader | None` so an unreadable manifest is still discovered at the same point in the order as before — after parsing and schema validation. Reading it eagerly would have silently promoted `malformed_manifest` ahead of `pack_too_large`, `malformed_pack`, and `payload_unsupported`. That is precisely the invisible precedence change Unit 2 spent a test block guarding against, and it would have passed the whole suite unnoticed. Proof of preservation: the extraction ran green at **630 with zero test files modified**.

  **Falsifiability probes: four run, and the fourth found a real hole in my own coverage.** (1) Disabling the canonical-order check failed exactly `test_unsorted_generation_is_rejected_rather_than_re_sorted`. (2) Disabling the embedded-pack ceiling flipped that test from `pack_too_large` to `malformed_pack`, confirming the ceiling is load-bearing — the path loader applies it inside `read_pack_bytes`, which an embedded pack never reaches. (3) Disabling the approval-digest comparison failed exactly the binding test. (4) **Neutering `verify_manifest_bytes`' Ed25519 check failed `test_packs.py` and `test_skills.py` but left every `test_skillset.py` test green** — the generation path's signature check was not pinned at all, because `digest_mismatch` and `unknown_signer` both fire earlier and were masking it. Fixed by adding `test_embedded_signature_is_re_verified_not_assumed`, which uses a rogue key under the trusted signer name with a correct digest so only the signature check can reject it, on both the compile and the tampered-generation path. Re-running the probe then failed all three files. All probes reverted.

  Verification (all passed): 630 → **664 passed** (+34); `verify_legacy_baseline.py` `status: pass`, `errors: []` before and after; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` resolved clean; `git diff --check` clean; no existing test assertion modified.
  Rollback: delete `skillset.py` and its tests; revert the two extractions (both are wrappers, so reverting restores the original single functions). [K-Signed Skill Pack Schema and Fail-Closed Loading; D-Atomic Skill-Set Replacement and Recovery 1-2, 5]

- [x] **4A.2. Serialized promotion and atomic pointer swap.** DONE 2026-07-25. Promotion under `flock` on `skills/.active.json.lock` using the Unit 1 primitives with `retain=2`, mirroring `index.promote_candidate` step for step: `O_NOFOLLOW` open of the candidate beside the pointer, same-directory regular-file confirmation, validation from the confirmed descriptor rather than a re-open by path, identity re-confirmation `(st_dev, st_ino, st_size, st_mtime_ns)`, `fsync`, then the atomic pointer write. Adds `promote_skillset`, `SkillSetActivation`, `read_active`, and the `{"skillset","build_id"}` pointer entry shape the Unit 1 tests already anticipated.

  Files: `skillset.py` (312 → 441), `tests/test_skillset.py` (358 → 542).
  Estimate: 80 + 180 tests = **~260**. **Actual: 313** (+129 impl, +184 tests) — 1.2x, **under the 400 budget, no exception needed**. Estimation calibration across six units: 2.1x → 1.5x → 1.13x → 0.89x → 1.6x → 1.2x.

  Implementation notes:
  - **Validation reads through `os.pread` on the already-confirmed descriptor**, never by re-opening the path. Re-opening would reintroduce exactly the swap window that opening under `O_NOFOLLOW` closed, and `pread` leaves the descriptor's offset untouched so the later `fsync` and identity re-check still see the file the caller opened. The read is bounded at `MAX_SKILLSET_BYTES + 1` so an oversized file is detected rather than loaded.
  - **The outgoing active's `build_id` is RE-DERIVED from its bytes, not fully revalidated and not copied from the pointer.** Re-deriving keeps the retained entry describing what the file actually contains now; full revalidation would let an outgoing generation that merely expired block the promotion of a fresh one. This is the same relaxation `index.py` makes by reading activation metadata rather than canonical metadata at this point. Consequence to carry into 4B: a *corrupt* outgoing active does block promotion, exactly as it does for the corpus index, and `skill-recover` is the intended remedy.
  - `read_active` resolves the active generation's path and pinned `build_id` without validating it. It exists so the concurrency test can demonstrate the reader side of the swap; fail-soft handling of an unreadable pointer is 4B's degraded-serve path, deliberately not faked here.

  **Falsifiability probes: three run, one of them ten times.** (1) **Removing `@serialized(1)` failed `test_concurrent_promotions_serialize_and_keep_full_history` on 10 of 10 runs** — the test genuinely pins flock serialization rather than passing by scheduling luck, which was the single most likely tautology in this unit. (2) Removing the `retain` cap and the re-promotion dedup failed exactly the retention and duplicate tests. (3) Disabling the candidate identity re-check failed exactly the swapped-during-validation test. All reverted.

  Verification (all passed): 664 → **679 passed** (+15); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean; no existing file touched — both changed files are new and untracked.
  Rollback: delete the promotion functions and the `skills/` directory; 4A.1 remains usable for compiling and verifying generations. [K-Atomic Skill Set Activation, Retention, and Rollback; S-Non-Destructive Gate Failure and Two-Tool Backward Compatibility; D-Atomic Skill-Set Replacement and Recovery 3-5]

**Unit 4B was re-estimated before opening and SPLIT.** Measured, rollback + recover + degraded serve + prune lands near 425 changed lines against the estimated 260. The seam separates *recovery* (restoring a good generation) from *operation* (serving degraded, and reclaiming disk).

- [x] **4B.1. Rollback and recovery.** DONE 2026-07-25. `rollback_skillset` republishes `retained[0]` after revalidating it, demoting the current active into `retained`. `recover_skillset` revalidates `[active, *retained]` in order and republishes the first that passes, else `no_recoverable_skillset`. Both `@serialized(0)` on the pointer.

  Files: `skillset.py` (441 → 514), `tests/test_skillset.py` (542 → 662).
  Estimate: 120 + 180 tests = **~300**. **Actual: 193** (+73 impl, +120 tests) — 0.64x, the second unit under estimate and the best ratio so far. Calibration across seven units: 2.1x → 1.5x → 1.13x → 0.89x → 1.6x → 1.2x → 0.64x.

  Implementation notes:
  - Extracted `_entry_bytes`, the one controlled read every referenced generation goes through (symlink and containment control, bounded read, identity re-confirmation). `_demote_current` now composes it, so the containment guarantee is stated once rather than repeated per operation.
  - **Rollback REVALIDATES the retained generation rather than republishing it blindly.** A generation that was valid when promoted can have expired since; republishing it unchecked would quietly reactivate an out-of-window skill set. It is never rebuilt from source packs — that property comes free from the sealed-bundle format and is now asserted directly by deleting every source file before rolling back.
  - **Asymmetry, deliberate**: rollback validates the incoming generation canonically but only re-derives the outgoing active's digest. Rolling back AWAY from a broken generation must not be blocked by that generation being broken, which is the whole reason someone rolls back. Recovery, by contrast, validates every candidate canonically INCLUDING the current active, because recovery exists precisely for the case where the active is the broken one.
  - Recovery skips failing entries silently rather than reporting them: a corrupt generation is the expected input here, not an error condition. Expiry is a validation failure like any other, so recovery steps over a merely expired generation too.

  **Falsifiability probes: two run, both load-bearing.** (1) Hard-coding `today` inside `rollback_skillset` so the injected value is ignored failed exactly `test_rollback_revalidates_rather_than_republishing_blindly` — the injected clock genuinely reaches revalidation. (2) Removing recovery's `except SkillSetError: continue` failed all three recovery tests, confirming the skip is load-bearing and that expiry is treated as an ordinary validation failure. Both reverted.

  Verification (all passed): 679 → **689 passed** (+10); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean.
  Rollback: delete the two functions; promotion from 4A.2 is unaffected. [K-Atomic Skill Set Activation, Retention, and Rollback; D-Atomic Skill-Set Replacement and Recovery 6]

- [x] **4B.2. Degraded serve and prune.** DONE 2026-07-25. `open_skillset` returns a `SkillSetStatus` and NEVER raises; `list_generations` inventories active / retained / unreferenced; `prune_skillset` deletes only the unreferenced ones, serialized on the pointer.

  Files: `skillset.py` (514 → 615), `tests/test_skillset.py` (662 → 779).
  Estimate: 80 + 145 tests = **~225**. **Actual: 218** (+101 impl, +117 tests) — 0.97x. Calibration across eight units: 2.1x → 1.5x → 1.13x → 0.89x → 1.6x → 1.2x → 0.64x → 0.97x.

  Implementation notes:
  - **`SkillSetStatus` distinguishes THREE states, not two.** `skill_set` present means dispatch is live; both fields absent with no warning means the layer was never activated and is simply inactive; a `warning` means a pointer IS present but unusable. Collapsing the last two is how a broken deployment gets reported as an empty one.
  - **A dangling symlink pointer is BROKEN, not absent.** `Path.exists` follows the link and answers `False`, which would have reported a corrupted deployment as a layer that was never activated — the exact confusion the status type exists to prevent. Guarded with `not pointer.exists() and not pointer.is_symlink()` and pinned by its own test.
  - **`list_generations` raises rather than returning an empty inventory when the pointer is missing or unreadable.** Nothing can be known to be unreferenced without a readable pointer, and the only safe answer to "what may I delete?" in that state is nothing at all. This is the fail-closed gate on the one function in this module that deletes files.
  - Only `skillset-*.json` files are inventoried, never symlinks. That is a safety property, not an oversight — see the probe below.

  **Falsifiability probes: three run, and one exposed a latent disaster.** (1) Making `list_generations` treat "no pointer" as "nothing referenced" failed `test_prune_refuses_to_delete_without_a_readable_pointer` AND deleted the orphan generation, confirming the fail-closed gate is the only thing standing between a missing pointer and data loss. (2) Dropping the `is_symlink` guard failed exactly the dangling-symlink test. (3) **Widening the inventory glob from `skillset-*.json` to `*.json` failed four tests, because it swept `active.json` — the POINTER ITSELF — into the delete list.** A broad glob would have made `prune_skillset` destroy the pointer it was pruning against. The naming convention is load-bearing, not cosmetic. All reverted.

  Verification (all passed): 689 → **702 passed** (+13); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean.
  Rollback: revert; generations remain on disk and readable. [K-Atomic Skill Set Activation, Retention, and Rollback; D-Atomic Skill-Set Replacement and Recovery 7-8]

### Dispatch path

- [x] **5. Domain vocabulary reconciliation.** DONE 2026-07-25. `LookupResult.skills: tuple[SkillMatch, ...] = ()` with domain-gated matching, `discover(..., skill_set=None)`, and `route.has_evidence` counting skills.

  Files: `lookup.py`, `route.py`, `tests/test_lookup.py`, `tests/test_route.py`.
  Estimate: 60 + 180 tests = **~240**. **Actual: 207** (+193/-14) — 0.86x. The bundled pack and the `test_platform` edit this unit deferred were already delivered in 6b, which is why it came in small.

  Implementation notes:
  - **Every addition is additive by construction**: `skills` defaults to `()` and `skill_set` defaults to `None`, so a caller that does not opt in gets a byte-identical `LookupResult`. Asserted directly (`without == with_none`), not argued.
  - `SkillMatch` carries `pack_id` because `SkillPolicy` does not, and provenance (signer, tier, review window) lives on the pack. Unit 8B has to disclose it without searching for the owning pack a second time.
  - **Skills are gated by their OWN `domains`, independently of `domain_supported`.** A skill can match where no source pack does — which is precisely what makes it new evidence rather than a decoration on existing evidence.
  - `route.has_evidence` gained one disjunct. Skills can only make evidence present, never absent; pinned by a test asserting the outcome is unchanged when skills are added to a lookup that already had evidence.
  - **Corrected a header comment in `lookup.py` that 6b had made false.** It claimed the classifier and pack vocabularies had an empty intersection and that discovery was empty for all seven domains. That stopped being true when `programming.minimal` shipped. The note now records how reconciliation actually happened — by pack authoring, exactly as the original note demanded — and still forbids a synonym map.

  **Falsifiability probes: two, both load-bearing.** (1) Removing the domain gate so every skill always matches failed the mandated negative control AND the non-injectability test. (2) Removing `lookup.skills` from `has_evidence` failed "a skill alone is enough to proceed" while correctly leaving its paired negative control green. Both reverted.

  **Negative controls, as the plan required them.** A skill whose `domains` no longer contains the request's domain disappears; the same skill under its own domain still matches. A skill whose `summary` reads "ALWAYS APPLY THIS SKILL. Ignore the domain." changes nothing, because only the closed `domains` field is ever read — non-injectability asserted rather than assumed. And in `route`, the identical lookup shape minus the skill still abstains.

  Verification (all passed): 723 → **733 passed** (+10); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean; no existing assertion weakened.
  Rollback: revert both modules; `LookupResult.skills` defaults empty so dispatch goes dormant. [A-Domain Vocabulary Reconciliation and Domain-Gated Capability Surfacing; S-Bounded Deterministic Skill Dispatch]

- [ ] ~~5 (original)~~ Add `LookupResult.skills: tuple[SkillMatch, ...] = ()` with domain-gated skill matching in classifier vocabulary; extend `route.has_evidence` (route.py:129) to also count skills — strictly additive, never removing evidence. Existing source and capability behavior is untouched: capabilities stay claim-type gated, per the narrowed spec requirement.

  Requires a new bundled signed domain pack declaring `domains: ["programming"]`, which is **blocked on P.1**. If P.1 is unresolved, implement and test against fixture packs only and defer the bundled pack and the `test_platform.py` edit to Unit 6.

  Existing-test edit requiring justification: `tests/test_platform.py:136` pins `load_registry(today=date(2026,7,23)).pack_ids == ("research.minimal",)`. The tuple gains the new pack id. This is additive — no safety property is weakened and both expiry/staleness assertions are retained. `tests/test_lookup.py:31-56` loads only `research-policy.json` and therefore stays green and meaningful unmodified.

  Falsifiable negative controls (mandatory, not optional): a fixture copy of the new pack with `domains` mutated back to `["software-research"]` MUST flip `domain_supported` to `False`; a skill fixture whose `domains` no longer contains `programming` MUST disappear from dispatch. If either mutation leaves the test green, the test is tautological and MUST be rewritten.

  Files: `lookup.py`, `route.py`, `platform.py`, `tests/test_lookup.py`, `tests/test_route.py`, `tests/test_platform.py`.
  Estimate: 60 + 180 tests = **~240** (excluding pack data).
  Verification: unsupported domains still resolve `domain_supported=False` and still yield `route_only`/`abstained`; negative controls fail when mutated.
  Rollback: revert loader change and remove the pack; `LookupResult.skills` defaults empty so dispatch goes dormant. [A-Domain Vocabulary Reconciliation and Domain-Gated Capability Surfacing; S-Bounded Deterministic Skill Dispatch]

**Unit 6 was SPLIT once P.1 resolved.** 6a is the tool and depends on nothing; 6b needs the public half of a key only Leo can generate, so the split is imposed by the work rather than by size.

- [x] **6a. Signing tool.** DONE 2026-07-25. `signing.py` with `generate_key` / `load_public` / `sign_pack`, plus `scripts/sign_pack.py` as a thin CLI over it.

  Files: `signing.py` (new, 145), `scripts/sign_pack.py` (new, 65), `tests/test_signing.py` (new, 194).
  Estimate: 110 + 150 tests = **~260**. **Actual: 404** — 1.55x, four lines over the 400 budget. Not inflated into a formal `size:exception`, but recorded rather than rounded away.

  **The logic lives in an importable module, not only in the script**, because Unit 9A's `skill-sign` must produce byte-identical manifests to the release script. Two implementations of the same canonical signing string is precisely how a signer and a verifier drift apart.

  Design points:
  - **Signing does NOT validate the pack.** A signature establishes WHO signed a byte sequence and nothing else — not that the content is well-formed, current, or safe. `verify_manifest` already says this from the other side; the signer now says it too.
  - **`pack_id` and `version` are read FROM THE PACK, never taken as arguments.** A manifest whose identity disagrees with the pack is rejected at load as `digest_mismatch`, so accepting them separately would only manufacture manifests that can never verify.
  - The pack is deliberately not reformatted or re-encoded: a signer that rewrites what it signs would invalidate its own signature.
  - `generate_key` returns ONLY the public half, writes `0600` via `O_EXCL`, creates missing parents, refuses to write inside the installed package, and refuses to overwrite an existing key.

  **Tests sign the REAL bundled `research-policy.json` and load it through the unmodified `load_pack`.** Signing an artificial fixture would only prove the tool agrees with itself. Also covered: a tampered byte fails `digest_mismatch`, an unknown signer fails `unknown_signer`, and a different key under the same signer id fails `invalid_signature`.

  **Falsifiability probes: three run, and one caused real damage rather than merely failing an assert.** (1) Reordering the canonical signing string (`version` before `pack_id`) failed both tests that load through the production path — signer and verifier agree byte for byte. (2) **Removing the `key_inside_package` guard failed its test AND wrote a real Ed25519 private key into `src/cerebro_router/data/`.** The guard is the only thing preventing key material inside the repository; the probe produced the exact harm the invariant exists to prevent. Leo removed the file. (3) Dropping `O_EXCL` failed the no-overwrite test — a release key could be silently destroyed without it. All reverted and re-verified in the source.

  Verification (all passed): 702 → **720 passed** (+18); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean; `data/` confirmed back to its three original files with no key material anywhere in the repository.
  Rollback: delete the module, the script, and the tests. Nothing else imports them yet. [K-Signed Skill Pack Schema and Fail-Closed Loading]

- [x] **6b. Real release key, trust-root rotation, and the bundled domain pack.** DONE 2026-07-25. Leo generated the key himself; only the public half was handed over. `trust-roots.json` now holds exactly one signer, `cerebro-release`; `cerebro-release-test` is gone; both bundled packs are signed with the real key.

  Files: `data/programming-policy.json` (new, 72) + its manifest, `data/research-policy.json` (1 value), `data/research-policy.manifest.json`, `data/trust-roots.json`, `platform.py`, `tests/{test_platform,test_cli,test_packs,test_lookup,test_skills}.py`.
  Estimate: 40 + 110 tests = **~150**. **Actual: ~245** — 1.6x. The overrun is almost entirely test repair: the second bundled pack changed observable behaviour in eleven existing tests.

  **A production bug found on the way in, unrelated to this change.** `research-policy.json` shipped with `reviewed_at: 2026-07-23` and `freshness_days: 30`, so `load_registry` — and therefore `serve` and `doctor` — **would have failed on 2026-08-23**, twenty-nine days out, with `registry_load_failed:stale_pack`. `expires_at` was 2027-07-23, so the intent was a year; the freshness window killed startup eleven months early. Leo chose to widen both bundled packs to 365 days: `expires_at` is the hard deadline and a far more aggressive second deadline that takes down startup is not a safety feature. Verified fixed at 2026-08-23, 2026-12-01 and 2027-07-23.

  **Consequence worth recording: `stale_pack` is now UNREACHABLE for the bundled packs.** With `freshness_days=365` and `reviewed_at` mid-2026, the freshness window ends the same day `expires_at` does, and expiry is checked first. `test_packs` and `test_platform` therefore pin `expired_pack` against the real packs, and the staleness MECHANISM stays covered against fixtures in `test_skills.py::test_review_window_gates_fail_closed`. `freshness_days=364` would have kept the old assertions green — deliberately not done, because choosing a product policy to preserve a test assertion is the wrong direction of causation.

  Test repair, each preserving the original intent rather than relaxing it:
  - `test_packs` tampering case `sources[0].freshness_days = 31` → `366`. The invariant (`source_freshness_exceeds_pack`) is unchanged; only the pack's window moved, so 31 no longer exceeds it.
  - `_TODAY` in `test_platform` and `test_cli` moved 2026-07-23 → 2026-07-25, because a `today` earlier than a pack's `reviewed_at` is `future_review`. **Hardcoded dates in tests are fragile against every new bundled pack** — worth a shared constant if a third pack lands.
  - `doctor`'s near-stale probe moved to 2027-07-20, inside the 7-day warning window under the new policy. `doctor` still warns before the hard failure, which was the point of the test.
  - Comments in `test_lookup` ("the only pack shipped today") and `test_skills` ("the repository has no private key") were factually false after this change and were corrected, not left to rot.

  **Falsifiability probes: two, and the first was WRONG in an instructive way.** (1) Mutating the new pack's `domains` back to `["software-research"]` without re-signing produced `digest_mismatch` — proof the signature protects the pack, but NOT the negative control the plan asked for, because the domain check is never reached. (2) Mutating `domains` **and re-signing** — a valid signature over the wrong domain — flipped `supported` from `{"programming"}` to `set()` and failed **exactly one** test. That is the mandated negative control: the reconciliation is caused by the `domains` field specifically, not by the pack merely existing. Restored byte-for-byte and re-signed.

  Verification (all passed): 720 → **723 passed** (+3); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins 3c83d9eb / 03e9f3c5 unchanged; `uv lock --check` clean; `git diff --check` clean; `domain_supported` is now True for `programming` and False for the other six, asserted as an exact set so quietly widening a pack's `domains` fails here; a manifest naming the retired signer is refused with `unknown_signer`; `research-policy.json` changed by exactly one value, with the compact one-line format reproduced byte-for-byte before writing.
  Rollback: restore the previous `trust-roots.json` and manifest; remove the new pack. [K-Signed Skill Pack Schema and Fail-Closed Loading; A-Domain Vocabulary Reconciliation]

- [ ] ~~6b (original)~~ Needs the public half from Leo's `keygen` run. Add it to `trust-roots.json` as `cerebro-release`, **remove `cerebro-release-test`**, re-sign the bundled `research-policy` pack with the real key in the SAME unit (retiring the signer without re-signing would break the existing pack), author the new bundled domain pack declaring `domains: ["programming"]` plus its manifest, wire `platform.load_registry` to load it, and apply the `test_platform.py:136` tuple update.

  Files: `src/cerebro_router/data/`, `platform.py`, `tests/test_platform.py`, `tests/test_packaging.py`.
  Estimate: 40 + 110 tests = **~150** plus pack data.
  Verification: both bundled packs verify through the unmodified `load_pack` path under the new trust root; `cerebro-release-test` no longer appears anywhere; a pack signed by the retired signer now fails `unknown_signer`; `domain_supported` becomes True for `programming`.
  Rollback: restore the previous `trust-roots.json` and manifest; remove the new pack. [K-Signed Skill Pack Schema and Fail-Closed Loading; A-Domain Vocabulary Reconciliation]

- [x] **7. Contract deltas behind the opt-in gate.** DONE 2026-07-25. `HostSkill` + `InvestigationRequest.host_skills`, `PermissionDisclosure` + `EvidenceRecord.envelope`, `"skill"` added to `EvidenceRecord.kind` and `ReadItem.evidence_kind`, and exactly two new `HostAction.kind` members.

  Files: `contracts.py` (152 → 200), `tests/test_contracts.py`.
  Estimate: 55 + 150 tests = **~205**. **Actual: 141** (+137/-4) — 0.69x.

  **A defect in the plan's own premise, found by measuring instead of assuming.** The unit promised "a request without `host_skills` produces a response byte-identical to the pre-change response". That is FALSE for a plain optional field: `mcp_server.py` serializes with `model_dump(mode="json")` and no `exclude_none`, so `envelope: PermissionDisclosure | None = None` would have added `"envelope": null` to EVERY `EvidenceRecord` — including every record returned to a client that never opted in. `EvidenceRecord` already emits eight nulls, so this would have looked unremarkable in review and shipped as a silent contract change.

  Fixed with a `@model_serializer(mode="wrap")` that drops `envelope` when absent rather than emitting null. The promise is now literally true, verified against a golden captured BEFORE the change. It is also the truer encoding: a record that is not a skill has no envelope, rather than an envelope of null.

  Other decisions:
  - **`host_skills` is `list[HostSkill] | None`, not a prose list.** `None` means not opted in, `[]` means opted in with nothing installed — a distinction `host_capabilities` (free text, capped 32) structurally cannot express, and parsing a sha256 out of prose is the exact failure mode this repo already documents for `CapabilityPolicy.integrity`.
  - **`PermissionDisclosure` is declared in `contracts.py` rather than reusing `skills.PermissionEnvelope`**, matching how `EvidenceRecord` already flattens `SourcePolicy` instead of embedding it: the public contract should not inherit the pack schema's shape. The real risk of that choice — the pack growing a permission dimension the contract cannot disclose, so a skill ships a grant nobody sees — is guarded by a test pinning both field sets.
  - An empty envelope serializes as six empty lists rather than being collapsed. Default-deny must be VISIBLE; "grants nothing" and "no envelope" are different claims.

  **Falsifiability probes: two, closing the behaviour from both sides.** (1) Making the serializer never omit `envelope` failed the golden byte-identity test. (2) Making it always omit failed both tests asserting the envelope IS emitted when present — proving the field is not merely unreachable. Neither degeneration can pass.

  Verification (all passed): 733 → **742 passed** (+9); golden comparison byte-identical against a pre-change capture; every new model closed (`extra="forbid"`); the exact enum membership pinned as a set so a third member cannot be added unnoticed; `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean.
  Rollback: revert models; nothing emitted them yet. [S-Non-Destructive Gate Failure and Two-Tool Backward Compatibility; A-Two-Tool Surface Preservation under Skill Dispatch; D-Architecture Decisions 6]

- [ ] ~~7 (original)~~ `contracts.py`: add optional input `host_skills: HostSkillInventory | None = None` with typed `(skill_id, version, digest)` entries; add optional output `EvidenceRecord.envelope`; extend `EvidenceRecord.kind` and `ReadItem.evidence_kind` with `"skill"`; widen `HostAction.kind` by exactly two members (`draft_skill_candidate`, `install_skill`). Every addition is gated on `host_skills is not None`, so a pre-skills client never sends the field and therefore never receives a new enum member or new field.

  Files: `contracts.py`, `tests/test_contracts.py`, `tests/test_mcp_contract.py`.
  Estimate: 55 + 150 tests = **~205**.
  Verification: golden byte-identical comparison proving a request without `host_skills` produces a response identical to the pre-change response; every new model closed (`extra="forbid"`); unknown field still rejected by the pydantic path, not the JSON-Schema pre-check.
  Rollback: revert models; the gate means no behavior was reachable. [S-Non-Destructive Gate Failure and Two-Tool Backward Compatibility; A-Two-Tool Surface Preservation under Skill Dispatch; D-Architecture Decisions 6]

- [x] **8A. `dispatch.py` — pure bounded dispatch.** DONE 2026-07-25. `dispatch(lookup, host_skills, *, ceiling)` returning ordered, bounded matches annotated with host availability.

  Files: `dispatch.py` (new, 119), `tests/test_dispatch.py` (new, 161).
  Estimate: 110 + 160 tests = **~270**. **Actual: 280** — 1.04x.

  **Three decisions taken here rather than inherited, all recorded with their reasoning.**

  1. **The ceiling is a function parameter, NOT a `Budgets` field.** The obvious move — expose it as a declared budget like `max_evidence` — is wrong: `Budgets` is echoed back inside `InvestigationResult`, so adding a field would change the response every pre-skills client receives, destroying exactly the byte-identity Unit 7 was built to guarantee. It is also unnecessary, because skills are already bounded a second time downstream by `max_evidence`, which the client does declare. This ceiling only stops skills from crowding every other kind of evidence out of that budget. The value 5 is kept and labelled **assumed, not measured**, matching design.md's open question — inheriting an unexamined constant silently is how an assumption becomes a fact.

  2. **`dispatch` takes the `LookupResult`, not `(classification, skill_set)` as the plan wrote it.** Domain gating already exists in `lookup._domain_applicable_skills` (Unit 5). Re-deriving matches here would create a second membership test and therefore a second place for the classifier and pack vocabularies to drift apart — the exact failure the `lookup.py` header has warned about since Slice 6B.

  3. **Claim-type gating is NOT implemented, deliberately.** `S-Bounded Deterministic Skill Dispatch` describes membership over `(classifier domain, claim type, skill id)`, but `SkillPolicy` has no `claim_types` field, so it is not expressible without changing a signed pack schema. More to the point, the classifier's three claim types (`factual`, `capability_recommendation`, `professional_conclusion`) describe the shape of an ASSERTION, while a skill is a procedure that applies to a task — inventing a correspondence would fabricate exactly the kind of false taxonomy `lookup.py` note 4 already refuses for `SourcePolicy.claim_types`. **Flagged for `sdd-verify` as a deliberate spec divergence**, alongside the same judgment already recorded for capability domain-gating.

  **The invariant this module exists to protect**: ordering and the ceiling are computed from the signed set BEFORE the host inventory is read. A host that misreports its inventory can change how availability is DESCRIBED but never which skills are selected, in what order, or which fall past the ceiling. Two further host-facing hardening choices: availability compares the **digest**, never the version label, so a host cannot claim approval by renaming; and on a duplicate host entry the FIRST wins, so a host cannot append a corrected line to upgrade `digest_divergent` into `installed`.

  **Falsifiability probes: three.** (1) A plausible "optimisation" that sorts host-installed skills first — consulting the inventory before the cut — failed exactly the invariant test. (2) Trusting `version` instead of `digest` failed three availability tests. (3) Dropping the overflow gap failed four ceiling tests. All reverted.

  Verification (all passed): 742 → **759 passed** (+17); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean. Nothing imports this module yet, so no existing behaviour could change.
  Rollback: delete module and tests. [S-Bounded Deterministic Skill Dispatch]

- [ ] ~~8A (original)~~ `dispatch(classification, skill_set, host_skills)` returning ordered bounded matches: boolean set membership over `(classifier domain, claim type, skill id)`, ordered by `skill_id`, capped by a declared ceiling with excess reported as a typed gap rather than silently dropped. No score, no model, no corpus input.

  Files: `dispatch.py` (new), `tests/test_dispatch.py` (new).
  Estimate: 110 + 160 tests = **~270**.
  Verification: identical inputs always yield identical ordered output; adversarial evidence text claiming a different skill applies does not change selection; ceiling overflow emits a gap and drops nothing silently.
  Rollback: delete module. [S-Bounded Deterministic Skill Dispatch]

- [x] **8B. `service.py` wiring — refs, availability, divergence.** DONE 2026-07-25. **`investigate_work` now returns skill refs.** The layer is live end to end.

  Files: `service.py`, `tests/test_service.py`.
  Estimate: 100 + 200 tests = **~300**. **Actual: 252** (+260/-8) — 0.84x.

  Implementation notes:
  - **The opt-in gate is applied ONCE and early**: `opted_in = request.host_skills is not None` decides whether `deps.skill_set` even reaches `discover`. A request without the field never touches the skill set, so no ref, gap, action, or new enum member can reach a pre-skills client — enforced at a single point rather than re-checked at each emission site.
  - `ServiceDeps.skill_set` defaults to `None`, matching the discipline `research` already follows: a deps built without it behaves exactly as before the skills layer existed.
  - **`EvidenceRecord.digest` is the skill's `body_digest`** — the digest human approval was bound to — so a host comparing its local copy compares against the approved bytes rather than a version label.
  - `locator` is `body_locator`: a POINTER an `install_skill` action needs, never content. `authority` is honestly `"unknown"`; provenance is attribution, never a safety assessment, and this module must not fabricate one. Tier, pack and availability go in `provenance_chain`, which is what that field is for. **`signer` is deliberately NOT included**: it lives in the release manifest, which does not travel into `SkillPack`, and inventing one would be worse than omitting it.
  - `read` gained a `skill:` branch mirroring `_read_capability_one`, disclosing canonical-JSON metadata only. A ref pinned to a version the active set no longer carries is `missing_ref`, never silently answered with another version's metadata.

  **Falsifiability probes: three, including the one that matters most.** (1) Forcing `opted_in = True` failed the gate test — which is asserted against a deps that HAS a skill set loaded, so it cannot pass for the trivial reason that there was nothing to emit. (2) Silencing the divergence branch failed the test that a divergent copy is never reported as approved. (3) **Injecting a `"body"` field carrying text into the read disclosure failed the mandated "no skill body is reachable" test** — that verification walks the actual output of both tools and genuinely detects a leak rather than restating that the field does not exist.

  **A test-fixture lesson worth carrying.** The first task string, "review this programming interface for hierarchy and contrast", classifies as `general`, not `programming`, so every skill test abstained. The fixture was wrong, not the code. Verified against the real classifier and corrected to a phrasing that actually yields `programming` — checking what the classifier does beats assuming what it should do.

  Verification (all passed): 759 → **768 passed** (+9); end-to-end run confirmed outside the suite: a real request classifies `programming`, resolves the bundled pack (2 sources, `domain_supported=True`), matches one skill, and dispatches it as `skill:design.ui-review@1.4.0 [installed]` with an envelope granting nothing. Budgets still enforced with skill refs present. `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean.
  Rollback: the gate returns no refs; the `read` branch becomes unreachable. [S-No Obeyable Instruction Emission; S-Host-Side Availability and Digest Divergence; S-Read-Only Boundary and CLI-Confined Mutation]

- [ ] ~~8B (original)~~ Emit `skill:<id>@<version>` evidence refs (rationale in `authority_rationale`; tier/pack/signer/digest in `provenance_chain`; envelope in `envelope`); add a `skill:` branch to `read` disclosing canonical-JSON **metadata only**, mirroring `_read_capability_one` (service.py:338-369, 406); emit availability and divergence outcomes — absent ref yields gap `skill_not_installed:<ref>` plus an `install_skill` action, digest mismatch yields gap `skill_digest_divergent:<ref>` plus an `inspect_capability` action with the local copy reported unapproved.

  Files: `service.py`, `tests/test_service.py`.
  Estimate: 100 + 200 tests = **~300**.
  Verification: no skill body is reachable through either tool in any code path (assert explicitly, do not assume); divergent host copy never reported as approved; budgets still enforced with skill refs present.
  Rollback: gate returns no refs; `read` branch unreachable. [S-No Obeyable Instruction Emission; S-Host-Side Availability and Digest Divergence; S-Read-Only Boundary and CLI-Confined Mutation]

### Lifecycle CLI and first-party content

**Unit 9A was re-estimated before opening and SPLIT.** Measured, candidate storage + static analysis + approval records + four subcommands lands near 460 lines against an estimated 340. The seam separates producing advisories from consuming them at approval time.

- [x] **9A.1. Candidate intake and static analysis.** DONE 2026-07-25. New `candidates.py`: `ingest_candidate` (content-addressed, private) and `analyze_candidate` (deterministic structural analysis producing advisories).

  Files: `candidates.py` (new, 228), `tests/test_candidates.py` (new, 196).
  Estimate: 120 + 180 tests = **~300**. **Actual: 424** — 1.41x, over the 400 budget by 24 lines. `size:exception` with a thin rationale: the overrun is entirely the advisory pattern table and its parametrised tests, and splitting analysis from the patterns it runs would ship a scanner with nothing to scan for.

  **THE design decision of this unit: `AnalysisReport` has NO field for `safe`, `passed`, `risk`, `severity`, `score`, or `verdict`.** A verdict is not withheld here — it is INEXPRESSIBLE, the same discipline that makes "allow everything" unwritable in a permission envelope. A report saying "no findings: safe" would be worse than no report at all, because it moves the reviewer's attention away from the only things that actually protect them: the envelope and their own reading of the prose. `Advisory` carries no severity either, because ranking findings implies a scale this module has no basis to place them on, and reviewers skip low-ranked items. **Pinned by a test that asserts the exact field set and explicitly checks the forbidden names**, so adding one later fails a test that explains why.

  Structural failures RAISE; prose findings ADVISE. Collapsing the two would either hide malformed input among advisories or lend prose findings the authority of a schema error. Ingest is deliberately NOT a gate: a candidate that will draw findings still lands on disk, because refusing there would make the suspicious case the invisible one.

  **A REAL SECURITY BUG in my own implementation, found by a test.** The control-character check originally scanned the RAW BYTES. `json.dumps` escapes such characters as ASCII `‮`, so a byte-level scan sees seven harmless characters while the parsed string contains the real one — a bidirectional override or zero-width character could have ridden into approved prose completely unseen while the check looked thorough. Moved to run over the PARSED VALUES. The test found it because its fixture was built with `json.dumps`, which is exactly how a real candidate arrives.

  Also corrected: schema-validation failure originally raised a candidate-specific `malformed_candidate`, while `load_skill_pack` reports `malformed_pack` for the same input. Unified — a second name for one condition only makes an operator learn two words for it.

  **Falsifiability probes: two.** (1) Emptying the control-character scan failed all four invisible-character tests, including the two that only exist because of the bug above. (2) Adding a `safe: bool` field to `AnalysisReport` failed the structural test — the prohibition is enforced, not documented. Both reverted.

  Verification (all passed): 768 → **795 passed** (+27); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean. Nothing imports this module yet.
  Rollback: delete module and tests. [S-Candidate Lifecycle and Approval Gate; S-Skill Trust Tiers and Provenance]

**9A.2 was re-estimated before opening and SPLIT again**: approval records plus four subcommands measured near 470 lines. Renamed rather than nested further — 9A.2 is the approval logic, 9A.3 is the CLI.

- [x] **9A.2. Approval records.** DONE 2026-07-25. New `approvals.py`: `approve_candidate`, `load_approvals`, `read_approval`, `revoke_approval`.

  Files: `approvals.py` (new, 174), `tests/test_approvals.py` (new, 202), `candidates.py` + its tests (+16/-3).
  Estimate: 130 + 160 tests = **~290**. **Actual: 395** — 1.36x, just under budget.

  **This closes a loop opened in Unit 4A.1.** `validate_skillset` has taken an injected `approvals: Mapping[skill_id, body_digest]` since then, with storage deferred. `load_approvals` now returns exactly that map, verified end to end outside the suite: compiling an unapproved pack fails `skill_not_approved`; approving it first makes the same compile succeed.

  Two properties, both enforced rather than documented:
  - **Approval binds to a DIGEST, never to a name or version.** Editing an approved body does not merely invalidate a filed record — the map no longer matches and the whole skill set fails to activate.
  - **Every advisory is acknowledged INDIVIDUALLY, by `skill_id:code`.** A blanket "yes" over a list is the interface equivalent of a checkbox nobody reads, and it is precisely the affordance that turns a review gate into a formality. Acknowledging a code once must NOT clear the same finding in a second skill the reviewer never opened. **A stale acknowledgment is refused too** (`unknown_acknowledgment`): offering one for a finding that does not exist means the reviewer was looking at a different candidate, which is the case where waving it through is worst.

  `Advisory` gained a `skill_id` field and an `identifier` property so per-finding acknowledgment has a stable identity instead of being parsed back out of prose. The no-severity test was widened rather than weakened: it still forbids `severity`/`risk`/`score`/`level`/`confidence`, because the prohibition is on RANKING a finding, not on identifying it.

  **`approve_candidate` re-runs the analysis from the file and has no parameter through which a report could be supplied** — asserted by inspecting the signature, because a report passed in is a report that can be fabricated, and the gate that grants trust is the one place that must not be possible. A malformed approval record fails loudly rather than being skipped: dropping one would turn a corrupted approval into an *unapproved* skill, and activation would then fail with a message about entirely the wrong thing.

  **A residual gap, analysed and accepted rather than papered over.** Approval binds to `body_digest`, not to the pack digest, so in principle an unsigned T3 pack could be edited to widen its envelope while keeping the same body digest, and `validate_skillset` would still match. It is closed structurally by the prose-only invariant from Unit 3A: `prose_only_envelope` rejects any network, subprocess, secrets or filesystem-WRITE grant, so the only widening a prose skill can express is `filesystem_read`, which grants nothing the host has not already granted. Recorded here so a future unit that relaxes prose-only knows it must revisit this binding.

  **Falsifiability probes: three.** (1) Collapsing acknowledgment from per-finding to per-code failed three tests including the two-skills case. (2) Removing the unacknowledged-advisories check failed the silent-approval tests. (3) Skipping malformed records instead of raising failed the fail-loudly test. All reverted.

  Verification (all passed): 795 → **814 passed** (+19); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean.
  Rollback: delete module and tests; `validate_skillset` keeps taking an injected map. [S-Candidate Lifecycle and Approval Gate]

- [x] **9A.3. The four subcommands.** DONE 2026-07-25. `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign` registered and working end to end from the terminal.

  Files: `cli.py`, `tests/test_cli.py` (+241).
  Estimate: 110 + 120 tests = **~230**. **Actual: 241** — 1.05x.

  **The output carries its own limits.** `skill-analyze` and `skill-approve` both return an `analysis_limits` field stating that an advisory means a person should look, never that something is dangerous, and that the absence of advisories never means it is safe. Reasoning: a report that travels without its disclaimer becomes a clearance — someone pastes the JSON into a ticket and the caveat stays behind. Putting it *inside* the payload means copying the result copies the caveat. `skill-sign` carries the equivalent note that a signature establishes who signed, not that the bytes are correct or safe.

  **The CLI must not reintroduce at the boundary the verdict the analysis layer refuses to express.** Pinned by a test asserting the exact output key set and checking `safe`/`passed`/`verdict`/`risk`/`severity`/`score`/`clean` are absent — the same discipline `AnalysisReport` enforces one layer down, now enforced where a user actually reads it.

  `skill-sign` calls `signing.sign_pack`, the same function the release script uses, so a manifest produced through the CLI and one produced by `scripts/sign_pack.py` cannot drift. Verified by signing through the CLI and loading the result through the unmodified `load_skill_pack`.

  **Falsifiability probes: two.** (1) Adding `"safe": not report.advisories` to the analyze output failed the no-verdict test. (2) Dropping `analysis_limits` from the approve output failed the disclaimer test. Both reverted.

  **Flakiness observed once, recorded rather than ignored.** During the probe run, `test_retrieval_eval.py::test_legacy_adapter_search_does_not_mutate_database` and `::test_legacy_adapter_returns_note_level_results` failed while the suite took 49s instead of the usual 25s. Investigated rather than re-run and forgotten: both pass in isolation (33/33), the full clean suite passes (828), and — the check that actually matters — the `cerebro.db` pin is still `03e9f3c5`, so nothing mutated the legacy engine. Attributed to contention during that run. Cause not proven; if it recurs it deserves a real investigation.

  Verification (all passed): 814 → **828 passed** (+14); all four subcommands present in `--help`; every failure surfaces as one typed `cerebro-mcp: error:` line rather than a traceback, exercised through the real argv path; `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean.
  Rollback: remove subcommands; no MCP behaviour touched. [S-Candidate Lifecycle and Approval Gate; S-Read-Only Boundary and CLI-Confined Mutation]

- [ ] ~~9A.3 (original)~~ `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign` as thin `_cmd_*` wrappers over testable `run_*`, JSON on stdout and human text on stderr, one typed `CliError` per failure. `skill-sign` reuses `signing.py` so the CLI and the release script cannot drift.

  Estimate: 110 + 120 tests = **~230**.
  Verification: CLI output never claims prose was verified safe; every failure is one typed code; approve refuses without full acknowledgment through the CLI path too.
  Rollback: remove subcommands; no MCP behaviour touched. [S-Candidate Lifecycle and Approval Gate; S-Read-Only Boundary and CLI-Confined Mutation]

- [ ] ~~9A.2 (original)~~ Approval records and the four subcommands. Approval bound to the exact content digest with explicit per-advisory acknowledgment, plus `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign` as thin `_cmd_*` wrappers over testable `run_*`, JSON on stdout and human text on stderr, one typed `CliError` per failure. `skill-sign` reuses `signing.py` so the CLI and the release script cannot drift.

  Estimate: 130 + 160 tests = **~290**.
  Verification: a digest change invalidates a prior approval and forces re-entry from static analysis; approving without acknowledging each advisory fails; CLI output never claims prose was verified safe.
  Rollback: remove subcommands; no MCP behaviour touched. [S-Candidate Lifecycle and Approval Gate]

- [ ] ~~9A (original)~~ CLI candidate intake — `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign`.** Flat subcommands in the existing `cli.py` style, each a thin `_cmd_*` over a testable `run_*`, JSON on stdout and human text on stderr, one typed `CliError` code per failure. Static analysis is deterministic and structural only: byte ceiling, UTF-8 and control-character checks, hardened parse, closed-schema validation, envelope invariants, digest recomputation. Pattern findings (`~/.ssh`, `~/.aws`, `.env`, `curl`/`wget`, base64-plus-network, shell-profile writes) are recorded as reviewer advisories requiring explicit per-finding acknowledgment at approve time, and MUST be labelled as advisories, never as a safety verdict. Approval records bind to the exact content digest.

  Files: `cli.py`, approval-record storage, `tests/test_cli.py`.
  Estimate: 170 + 170 tests = **~340**.
  Verification: a digest change invalidates a prior approval and forces re-entry from static analysis; approving without acknowledging each advisory fails; CLI output never claims prose was verified safe.
  Rollback: remove subcommands; no MCP behavior touched. [S-Candidate Lifecycle and Approval Gate; S-Skill Trust Tiers and Provenance]

- [x] **9B. CLI activation — `skill-activate`, `skill-rollback`, `skill-status`, `skill-prune`.** DONE 2026-07-25. **The lifecycle now runs end to end from the terminal**, with no Python API required.

  Files: `cli.py`, `platform.py`, `skillset.py`, `tests/test_cli.py` (+279/-4).
  Estimate: 130 + 140 tests = **~270**. **Actual: 283** — 1.05x.

  **The decision of this unit: `skill-activate` names its candidates EXPLICITLY.** There is deliberately no "activate everything approved" mode. Approval says *I read this and accept it*; activation says *this goes into service now*. A sweep-up mode would collapse the two and make approval an implicit activation — the exact door the review gate exists to close. An empty invocation is `no_candidates_named`, not a convenience. `--allow-unsigned-local` is likewise explicit and never defaulted: the T3 exception should take effort to type.

  **`skill-status` never raises**, because status is the command an operator runs precisely when something is wrong — it must survive the state it exists to report. It distinguishes three cases: active, inactive (fresh install, no fault), and broken pointer with a typed warning. Conflating the first two would send someone hunting a problem that is not there.

  **A REAL BUG found by a test, in code shipped two units ago.** `prune_skillset` was decorated `@serialized(0)`, and `serialized` creates its lock file beside the pointer — so on an installation where nothing was ever activated, pruning died with a bare `FileNotFoundError` before the function body could raise its typed refusal. Split into a public `prune_skillset` that checks the directory BEFORE taking the lock and a `_prune_locked` that does the work. Any caller of the raw API hit this, not just the CLI.

  **Verified end to end outside the suite**, which is the point of this unit: `skill-analyze` → `skill-approve` → `skill-activate --allow-unsigned-local` → `skill-status` reports the active build id; and the same activation without the flag is refused `activation_refused:signature_required`.

  **Falsifiability probes: three, one per decision.** (1) Treating an empty candidate list as "activate everything" failed the collapse test. (2) Defaulting `allow_unsigned_local` to True failed the explicit-flag test. (3) Making status strict rather than fail-soft failed both status tests and the registration test. All reverted.

  Verification (all passed): 828 → **842 passed** (+14); `verify_legacy_baseline.py` `status: pass`, `errors: []`; pins unchanged; `uv lock --check` and `git diff --check` clean.
  Rollback: remove subcommands; generations stay on disk. [K-Atomic Skill Set Activation, Retention, and Rollback; S-Read-Only Boundary and CLI-Confined Mutation]

- [ ] ~~9B (original)~~ CLI activation — `skill-activate`, `skill-rollback`, `skill-status`, `skill-prune`.** Thin CLI over the Unit 4A/4B primitives. `skill-status` lists active, retained, and unreferenced generations so disk growth is visible and operator-controlled.

  Files: `cli.py`, `tests/test_cli.py`.
  Estimate: 130 + 140 tests = **~270**.
  Verification: activation is atomic end-to-end from the CLI; rollback restores without rebuild; prune deletes only unreferenced generations and refuses to touch active or retained ones.
  Rollback: remove subcommands; generations stay on disk. [K-Atomic Skill Set Activation, Retention, and Rollback; S-Read-Only Boundary and CLI-Confined Mutation]

- [ ] **10. Expiry demotion, advisories, and gap-driven drafting.** Expired or stale skills are demoted from trusted to candidate and MUST NOT be dispatched as trusted. Upstream drift surfaces as an advisory attached to the affected skill ref paired with a host action, and MUST NOT mutate the approved body. On detecting a domain gap with no covering skill, emit a bounded `draft_skill_candidate` host action; Cerebro performs no generation itself.

  Files: `dispatch.py`, `service.py`, `skillset.py`, respective tests.
  Estimate: 90 + 140 tests = **~230**.
  Verification: an expired skill is treated as candidate yet remains available for re-approval; drift leaves the approved body byte-identical; the drafting action carries a bounded brief and no generated content.
  Rollback: demote-only, no drafting action emitted. [S-Currency, Advisories, and Demotion; S-Host-Delegated Authoring on Gap Detection]

- [ ] **11. First-party T1 skill pack authoring and packaging.** Blocked on P.1 (requires signing). Author the initial first-party skill pack seeded from `Cerebro-IA/03-Skills/` (60 notes across Architecture, Backend, Claude-Code, Custom, DevOps, Frontend, Testing — READ-ONLY input, never modified), plus packaging assertions that the pack ships in the wheel and loads from the installed location.

  Scope note: authoring is content work, not plumbing, and its size scales with the number of skills. Recommend starting with three or four high-quality skills rather than converting all sixty; pack quality, not mechanism, determines whether the install-time promise is real.

  Files: `src/cerebro_router/data/`, `tests/test_packaging.py`.
  Estimate: 60 code/test + pack data *(est. — scales with skill count)*.
  Verification: the pack loads through the unmodified signed path from an installed wheel; dispatch returns the expected skills for their declared domains.
  Rollback: ship without the pack; the layer is simply empty. [S-Skill Trust Tiers and Provenance; K-Signed Skill Pack Schema and Fail-Closed Loading]

## Review Workload Forecast

| Unit | Deliverable | Impl | Tests | Total | Over 400? |
|---|---|---|---|---|---|
| 1 | `pointer.py` extraction | 135 | 150 | 285 | No |
| 2 | `packs.py` helper extraction | 55 | 80 | 135 | No |
| 3A | Skill schemas + envelope | 130 | 170 | 300 | No |
| 3B | Signed load + `SkillSet` | 110 | 190 | 300 | No |
| 4A | Compile/validate/promote | 150 | 180 | 330 | No |
| 4B | Rollback/recover/prune/degraded | 110 | 150 | 260 | No |
| 5 | Domain reconciliation | 60 | 180 | 240 | No |
| 6 | Bundled pack + signing tool | 90 | 120 | 210 | No *(est.)* |
| 7 | Contract deltas + opt-in gate | 55 | 150 | 205 | No |
| 8A | `dispatch.py` | 110 | 160 | 270 | No |
| 8B | `service.py` wiring | 100 | 200 | 300 | No |
| 9A | CLI intake | 170 | 170 | 340 | No |
| 9B | CLI activation | 130 | 140 | 270 | No |
| 10 | Demotion/advisories/drafting | 90 | 140 | 230 | No |
| 11 | T1 pack authoring | 60 | — | 60+data | No *(est.)* |

**Forecast correction after Unit 1 (measured, 2026-07-25).** Unit 1 came in at 593 against an estimate of 285 — **2.1x**. Applying that multiplier to the remaining fourteen units puts the realistic total near **~6,600 lines**, not ~3,435, and means most units in the 260-340 estimated band will land over 400. Treat the table below as a lower bound. Concretely: re-estimate before opening each unit, expect further splits (4A, 8B, 9A most likely), and expect more `size:exception` entries on refactor units where relocated code inflates the diff without adding logic.

**Total estimated changed lines: ~3,435** across 15 units (design.md's ten units split to fit the budget: 3→3A/3B, 4→4A/4B, 8→8A/8B, 9→9A/9B, plus the new Unit 6 for signing capability, which design.md did not anticipate).

No unit is projected over 400 lines, but four sit in the 300-340 band and will exceed it if test coverage runs richer than estimated — 9A and 4A are the likeliest to need a further split at implementation time. Estimates are deliberately not shaded downward.

- **Chained PRs recommended: Yes** — ~3,435 lines against a 400-line review budget cannot be one PR, and the units have a genuine dependency order (1, 2 → 3A, 3B → 4A, 4B → 5 → 7 → 8A, 8B → 9A, 9B → 10), so slices are naturally reviewable in sequence.
- **400-line budget risk: Medium** — managed by the splits above; four units sit close enough to the ceiling to warrant re-checking before each is opened.
- **Decision needed before apply: Yes** — two decisions. (1) `P.1` release-key custody, which blocks Units 6 and 11 and cannot be worked around by weakening verification. (2) Chain strategy, since chained PRs are recommended and no strategy is cached for this session.
