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

- [ ] **P.1 Resolve release-key custody before Units 6 and 11.** Verified state: `src/cerebro_router/data/trust-roots.json` contains exactly one signer, `cerebro-release-test`, as a public key. There is **no private key, no signing script, and no test that constructs `Ed25519PrivateKey` or calls `.sign(...)` anywhere in the repository** — the only `.pem` files are `tests/fixtures/fetch/{testcert,testkey}.pem`, which belong to the TLS fetch tests. `packs.py` verifies signatures; nothing in-repo can produce one. The existing `research-policy.manifest.json` was therefore signed out of band.

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

- [ ] **4B.2. Degraded serve and prune.** A missing pointer means the layer is simply inactive; an invalid or unreadable pointer disables dispatch with a typed `skillset_unreadable:<code>` warning and MUST NEVER fail `serve`, `doctor`, or corpus retrieval. Evicted generations are deliberately NOT garbage-collected (matching `index.py` rather than diverging); they are listed and removed only by explicit prune.

  Files: `skillset.py`, `tests/test_skillset.py`.
  Estimate: 80 + 145 tests = **~225**.
  Verification: unreadable pointer degrades rather than failing serve; prune deletes only unreferenced generations and refuses to touch active or retained ones.
  Rollback: revert; generations remain on disk and readable. [K-Atomic Skill Set Activation, Retention, and Rollback; D-Atomic Skill-Set Replacement and Recovery 7-8]

### Dispatch path

- [ ] **5. Domain vocabulary reconciliation.** Add `LookupResult.skills: tuple[SkillMatch, ...] = ()` with domain-gated skill matching in classifier vocabulary; extend `route.has_evidence` (route.py:129) to also count skills — strictly additive, never removing evidence. Existing source and capability behavior is untouched: capabilities stay claim-type gated, per the narrowed spec requirement.

  Requires a new bundled signed domain pack declaring `domains: ["programming"]`, which is **blocked on P.1**. If P.1 is unresolved, implement and test against fixture packs only and defer the bundled pack and the `test_platform.py` edit to Unit 6.

  Existing-test edit requiring justification: `tests/test_platform.py:136` pins `load_registry(today=date(2026,7,23)).pack_ids == ("research.minimal",)`. The tuple gains the new pack id. This is additive — no safety property is weakened and both expiry/staleness assertions are retained. `tests/test_lookup.py:31-56` loads only `research-policy.json` and therefore stays green and meaningful unmodified.

  Falsifiable negative controls (mandatory, not optional): a fixture copy of the new pack with `domains` mutated back to `["software-research"]` MUST flip `domain_supported` to `False`; a skill fixture whose `domains` no longer contains `programming` MUST disappear from dispatch. If either mutation leaves the test green, the test is tautological and MUST be rewritten.

  Files: `lookup.py`, `route.py`, `platform.py`, `tests/test_lookup.py`, `tests/test_route.py`, `tests/test_platform.py`.
  Estimate: 60 + 180 tests = **~240** (excluding pack data).
  Verification: unsupported domains still resolve `domain_supported=False` and still yield `route_only`/`abstained`; negative controls fail when mutated.
  Rollback: revert loader change and remove the pack; `LookupResult.skills` defaults empty so dispatch goes dormant. [A-Domain Vocabulary Reconciliation and Domain-Gated Capability Surfacing; S-Bounded Deterministic Skill Dispatch]

- [ ] **6. Bundled domain pack and signing tool.** Blocked on P.1. Build the signing utility (out of the MCP surface, CLI or `scripts/`, private key by path and never written into `data_dir`), generate the new bundled domain pack plus its release manifest, wire `platform.load_registry` to load it, and apply the `test_platform.py:136` tuple update if it was deferred from Unit 5.

  Files: `scripts/` or `cli.py`, `src/cerebro_router/data/`, `platform.py`, `tests/test_platform.py`, `tests/test_cli.py`.
  Estimate: 90 + 120 tests = **~210** *(est. — depends on the P.1 outcome)*.
  Verification: the generated manifest verifies through the unmodified `load_pack` path; a tampered byte fails `digest_mismatch`; the signing tool never writes key material into `data_dir`.
  Rollback: remove pack and manifest; revert `load_registry`. [K-Signed Skill Pack Schema and Fail-Closed Loading]

- [ ] **7. Contract deltas behind the opt-in gate.** `contracts.py`: add optional input `host_skills: HostSkillInventory | None = None` with typed `(skill_id, version, digest)` entries; add optional output `EvidenceRecord.envelope`; extend `EvidenceRecord.kind` and `ReadItem.evidence_kind` with `"skill"`; widen `HostAction.kind` by exactly two members (`draft_skill_candidate`, `install_skill`). Every addition is gated on `host_skills is not None`, so a pre-skills client never sends the field and therefore never receives a new enum member or new field.

  Files: `contracts.py`, `tests/test_contracts.py`, `tests/test_mcp_contract.py`.
  Estimate: 55 + 150 tests = **~205**.
  Verification: golden byte-identical comparison proving a request without `host_skills` produces a response identical to the pre-change response; every new model closed (`extra="forbid"`); unknown field still rejected by the pydantic path, not the JSON-Schema pre-check.
  Rollback: revert models; the gate means no behavior was reachable. [S-Non-Destructive Gate Failure and Two-Tool Backward Compatibility; A-Two-Tool Surface Preservation under Skill Dispatch; D-Architecture Decisions 6]

- [ ] **8A. `dispatch.py` — pure bounded dispatch.** `dispatch(classification, skill_set, host_skills)` returning ordered bounded matches: boolean set membership over `(classifier domain, claim type, skill id)`, ordered by `skill_id`, capped by a declared ceiling with excess reported as a typed gap rather than silently dropped. No score, no model, no corpus input.

  Files: `dispatch.py` (new), `tests/test_dispatch.py` (new).
  Estimate: 110 + 160 tests = **~270**.
  Verification: identical inputs always yield identical ordered output; adversarial evidence text claiming a different skill applies does not change selection; ceiling overflow emits a gap and drops nothing silently.
  Rollback: delete module. [S-Bounded Deterministic Skill Dispatch]

- [ ] **8B. `service.py` wiring — refs, availability, divergence.** Emit `skill:<id>@<version>` evidence refs (rationale in `authority_rationale`; tier/pack/signer/digest in `provenance_chain`; envelope in `envelope`); add a `skill:` branch to `read` disclosing canonical-JSON **metadata only**, mirroring `_read_capability_one` (service.py:338-369, 406); emit availability and divergence outcomes — absent ref yields gap `skill_not_installed:<ref>` plus an `install_skill` action, digest mismatch yields gap `skill_digest_divergent:<ref>` plus an `inspect_capability` action with the local copy reported unapproved.

  Files: `service.py`, `tests/test_service.py`.
  Estimate: 100 + 200 tests = **~300**.
  Verification: no skill body is reachable through either tool in any code path (assert explicitly, do not assume); divergent host copy never reported as approved; budgets still enforced with skill refs present.
  Rollback: gate returns no refs; `read` branch unreachable. [S-No Obeyable Instruction Emission; S-Host-Side Availability and Digest Divergence; S-Read-Only Boundary and CLI-Confined Mutation]

### Lifecycle CLI and first-party content

- [ ] **9A. CLI candidate intake — `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign`.** Flat subcommands in the existing `cli.py` style, each a thin `_cmd_*` over a testable `run_*`, JSON on stdout and human text on stderr, one typed `CliError` code per failure. Static analysis is deterministic and structural only: byte ceiling, UTF-8 and control-character checks, hardened parse, closed-schema validation, envelope invariants, digest recomputation. Pattern findings (`~/.ssh`, `~/.aws`, `.env`, `curl`/`wget`, base64-plus-network, shell-profile writes) are recorded as reviewer advisories requiring explicit per-finding acknowledgment at approve time, and MUST be labelled as advisories, never as a safety verdict. Approval records bind to the exact content digest.

  Files: `cli.py`, approval-record storage, `tests/test_cli.py`.
  Estimate: 170 + 170 tests = **~340**.
  Verification: a digest change invalidates a prior approval and forces re-entry from static analysis; approving without acknowledging each advisory fails; CLI output never claims prose was verified safe.
  Rollback: remove subcommands; no MCP behavior touched. [S-Candidate Lifecycle and Approval Gate; S-Skill Trust Tiers and Provenance]

- [ ] **9B. CLI activation — `skill-activate`, `skill-rollback`, `skill-status`, `skill-prune`.** Thin CLI over the Unit 4A/4B primitives. `skill-status` lists active, retained, and unreferenced generations so disk growth is visible and operator-controlled.

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
