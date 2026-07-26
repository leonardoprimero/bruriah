# Design: Cerebro Skills Layer — Vetted Capability Dispatch

## Revision History

| Date | Decision |
|---|---|
| 2026-07-25 (original) | Sibling skill schema/registry beside `DomainPack`; signed-pack permission envelope; opt-in gated surface extension on the existing two tools; separate atomic skill-set pointer; prose-only skills in v1 with executable payloads deferred. |

## Architecture

Skill dispatch is a fourth deterministic stage beside the frozen `classify -> discover -> route` pipeline. It reads a signed skill set, never a skill body; bodies live only on the host and in the CLI review path.

```text
investigate_work -> classify -> discover(registry) --------> route
                                   |                          |
                                   +-> dispatch(skill set) ----+-> evidence(skill refs) + host_actions
CLI (human)  ingest -> analyze -> approve(digest) -> sign -> activate/rollback -> skills/active.json
```

| Boundary | Decision |
|---|---|
| Mutation | CLI only, mirroring `cli.py:379-398` (`init`/`index` already write; MCP stays read-only). No skill body is ever returned by `investigate_work` or `read_evidence`. |
| Determinism | Dispatch is boolean set membership over `(classifier domain, claim type, skill id)`, ordered by `skill_id`, capped by a declared ceiling. No score, no model, no corpus input — the non-injectability property of `lookup.py` is preserved by construction. |
| Enforcement limit | Cerebro **declares and discloses** the permission envelope; the host enforces it. Cerebro never executes a skill, so it cannot enforce anything at runtime. This is stated, not implied. |

## Architecture Decisions

| # | Decision | Choice, rejected alternatives, tradeoff |
|---|---|---|
| 1 | Schema placement | **Choice:** new `skills.py` with sibling `SkillPolicy`/`SkillPack` closed models and a separate frozen `SkillSet`, loaded by `load_skill_pack`. **Rejected:** extending `DomainPack.capabilities` — `DomainPack.schema_version` is `Literal["1"]` (`packs.py:46`), `Registry.from_packs` is frozen and pinned (`registries.py:6-29`), and a *required* field addition would reject the existing bundled pack at schema validation. Note the mechanism precisely: the manifest digest is taken over the unmodified file bytes (`packs.py:182`), so an unedited `research-policy.json` keeps a valid manifest — it is closed-model validation, not the digest, that breaks. The remaining option, an optional field with a permissive default, would make the envelope unenforceable by construction and is therefore worse than a sibling schema. **Reuse:** one additive extraction inside `packs.py` promotes `_parse_pack_bytes`, `_reject_yaml_aliases`/`_StrictYamlLoader`, `_version` and the manifest verification block to public helpers that `load_pack` then calls, keeping every `PackError` code and **check order** byte-identical. **Rejected:** importing the private `_`-names from a new module (fragile) or re-implementing Ed25519/digest/hardened parsing (two divergent security paths). **Tradeoff:** touching `packs.py` at all risks reordering observable failures (e.g. `signature_required` before `expired_pack`); mitigated by extraction-only diffs, the existing suite, and one new precedence test over overlapping-failure fixtures. |
| 2 | Permission envelope | **Choice:** structured closed sub-models inside the **signed pack** (see below), replacing free-text `permissions`/`network_access`/`data_access` (`packs.py:41-43`) for skills only; legacy `CapabilityPolicy` is untouched. Default-deny is represented as *absence*: every dimension defaults to an empty collection, empty means deny, and no wildcard or "all" token is expressible in the patterns. **Why a skill cannot widen itself:** the envelope is pack data covered by the pack digest, which the Ed25519 manifest signs and to which the approval record is bound; the skill body is not an input to envelope resolution — Cerebro never reads a body and never executes one. Editing an envelope changes the pack bytes, which breaks the signature (T1/T2) and invalidates approval (all tiers), returning the skill to candidate. **Rejected:** declaring permissions in skill front matter — that puts the security-relevant claim inside the artifact the host obeys, i.e. self-asserted authority. |
| 3 | Domain vocabulary | **Choice:** (a) ship a **new** bundled signed domain pack declaring `domains: ["programming"]` (classifier vocabulary, `classify.py:21`) so at least one domain resolves `domain_supported=True`; (b) give `SkillPolicy` its own `domains` in classifier vocabulary, so skills are domain-gated *and* claim-type gated; (c) leave `CapabilityPolicy` claim-type gated as today (`lookup.py:134`). **Rejected:** mutating `research-policy.json`'s `domains` (re-sign, digest churn, breaks `test_lookup.py:38`); a synonym map in `lookup.py` (explicitly forbidden by `lookup.py:24`); domain-gating legacy capabilities via owning pack — that breaks `test_lookup.py:52-56` and `test_service.py:107-128`, so the non-destructive rule wins over the broader reading of the delta requirement. **Preserved:** `test_lookup.py:31-56` loads only `research-policy.json`, so it stays green and meaningful unmodified. **Requires update:** `test_platform.py:136` pins `load_registry(...).pack_ids == ("research.minimal",)`; the tuple gains the new pack id — additive, no safety property weakened, and both expiry assertions stay. **Negative controls:** a fixture copy of the new pack with `domains` mutated back to `["software-research"]` MUST flip `domain_supported` to `False`, and a skill fixture whose `domains` no longer contains `programming` MUST disappear from dispatch. |
| 4 | Skill-set storage | **Choice:** a **separate** pointer `data_dir/skills/active.json` plus one immutable canonical-JSON generation file per activation, using the same mechanics as `index.py`. **Rejected:** reusing `data_dir/active.json` — `_read_pointer` fixes entry keys to `{"database","build_id"}` and every entry is opened as SQLite and revalidated against `BuildConfig`/`CorpusPolicy` (`index.py:349-372`, `451-465`); a skill set is not a corpus index, so reuse would require weakening corpus validation, which the lifecycle delta forbids. **Reuse:** the generic pointer/lock primitives move to `pointer.py` with a parameterized entry key (`_read_pointer`, `_write_pointer`, `_controlled_file`, `_identity`, `_identity_matches`, `_activation_lock`, `_serialized`); `index.py` imports them and keeps its codes (`invalid_active_pointer`, `invalid_active_target`, `active_target_changed_during_validation`). **Rejected:** duplicating ~90 lines of atomicity-critical code (divergence risk). |
| 5 | Host-side availability | **Choice:** one new **optional** input field `InvestigationRequest.host_skills: HostSkillInventory \| None = None`, whose entries are typed `(skill_id, version, digest)`. `None` means no opt-in: dispatch stays dormant and the result is byte-identical to today's. **Rejected:** `host_capabilities` (`contracts.py:32`) as the channel — it is `list[ShortText]` prose capped at 32 entries, and parsing sha256 digests out of prose is precisely the failure mode `service.py:120-129` documents for `CapabilityPolicy.integrity`; it also cannot distinguish "opted in with zero skills installed" from "did not opt in". **Divergence:** absent ref -> gap `skill_not_installed:<ref>` + `install_skill` action; digest mismatch -> gap `skill_digest_divergent:<ref>` + `inspect_capability` action, and the local copy is reported as unapproved. `InvestigationResult.gaps` is `list[ShortText]` (`contracts.py:98`), so typed gap names need no schema change. |
| 6 | `HostAction.kind` extension | **Analysis:** widening an **output** enum is not backward compatible for a client validating against a pinned pre-skills schema — a closed union or exhaustive switch fails on an unknown member — whereas an unknown optional property is usually ignored. `mcp_server.py:54-57` publishes schemas derived from the models, so live clients always see the current schema; only pinned copies are at risk. **Choice:** widen `HostAction.kind` by exactly two members (`draft_skill_candidate`, `install_skill`), reuse `inspect_capability` for divergence, and **gate emission on `host_skills is not None`**. A pre-skills client never sends the field, therefore never receives a new enum member, a new `kind` value, or the new evidence field — the backward-compatibility scenario holds by construction rather than by convention. Same gate covers `EvidenceRecord.kind`/`ReadItem.evidence_kind` gaining `"skill"` and `EvidenceRecord.envelope`. **Rejected:** overloading `inspect_capability` for drafting (the host cannot tell "inspect" from "draft", defeating a typed action). |
| 7 | Static analysis and sandbox | **Choice: prose-only skills in v1.** `SkillPolicy.payload` is `Literal["prose"]`, so an executable payload is not expressible and the sandbox stage is unreachable; a candidate carrying one is rejected `payload_unsupported` — refused, never silently passed. Static analysis is deterministic and structural only: byte ceiling, UTF-8 and control-character checks, hardened parse, closed-schema validation, envelope invariants (prose-only MUST declare no network/subprocess/secrets/filesystem-write), digest recomputation. Pattern findings (`~/.ssh`, `~/.aws`, `.env`, `curl`/`wget`, base64-plus-network, shell-profile writes) and a purpose-coherence summary are recorded as reviewer advisories requiring explicit per-finding acknowledgment at approve time; they are **not** a safety verdict. **Cost of deferral:** no bundled scripts, hooks, or tools in v1, so Slice 6 of the proposal drops out and skills carry instructions and checklists only. **Rationale:** the repository has no isolation primitive and exactly one `subprocess.run` in the tree (`evaluation.py:677`); building a sandbox here would be an unverifiable security claim, and only Darwin/POSIX activation is validated at all (`platform.py:4-5`). The strong gates remain the envelope and human approval. |
| 8 | CLI surface | **Choice:** flat subcommands in the existing style (`skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign`, `skill-activate`, `skill-rollback`, `skill-status`, `skill-prune`), each a thin `_cmd_*` over a testable `run_*`, JSON on stdout, human text on stderr, one typed `CliError` code per failure. **Rejected:** nested subparsers (none exist today). Private signing keys are passed by path and never written into `data_dir`. |

### Permission envelope (signed pack data)

```python
class FilesystemAccess(ClosedModel):
    read_paths: list[SafeRelPath] = []      # empty => deny
    write_paths: list[SafeRelPath] = []     # SafeRelPath: relative, no "..", no "*"
class NetworkAccess(ClosedModel):
    hosts: list[Hostname] = []              # exact hosts; no wildcard expressible
    schemes: list[Literal["https"]] = []
class SubprocessAccess(ClosedModel):
    programs: list[Identifier] = []         # basenames only; no shell string
class PermissionEnvelope(ClosedModel):
    filesystem: FilesystemAccess = FilesystemAccess()
    network: NetworkAccess = NetworkAccess()
    subprocess: SubprocessAccess = SubprocessAccess()
    secrets: list[Identifier] = []          # named handles, never values
```

## Atomic Skill-Set Replacement and Recovery

Required by `openspec/config.yaml` `rules.design`, mirroring `index.py:520-588`.

1. **Build.** `skill-activate` compiles every approved skill record into one deterministic canonical-JSON generation `skills/skillset-<uuid>.json`, written to a private tempfile in the same directory and `os.replace`d into place (`index.py:602-665` pattern).
2. **Validate.** `validate_skillset` re-parses under the hardened loader, revalidates each closed record, recomputes each digest, re-verifies signatures against the bundled trust roots, re-checks `reviewed_at`/`expires_at`/`freshness_days` with an injected `today`, asserts unique skill ids and envelope invariants, and asserts every record has an approval record bound to its current digest.
3. **Promote.** Under `flock` on `skills/.active.json.lock`, open the candidate with `O_NOFOLLOW`, confirm same-directory regular file, validate, re-confirm identity `(st_dev, st_ino, st_size, st_mtime_ns)`, `fsync`, then write the pointer to a tempfile, `fsync` file and parent directory, and `os.replace` it. Readers therefore observe exactly one complete generation; a reader mid-request keeps its open descriptor.
4. **Pointer.** `{"version":1,"active":{"skillset","build_id"},"retained":[...]}`; symlinked pointers rejected; any entry whose `Path(name).name != name` rejected (traversal); `retain=2`.
5. **Failure.** Any failure raises before the pointer write, so the previously active skill set stays byte-identical and fully queryable; no partial activation exists because the generation file is immutable and unreferenced until the swap.
6. **Rollback / recovery.** `skill-rollback` promotes `retained[0]` after revalidating it, demoting the current active into `retained` (`rollback_active` shape). `skill-recover` revalidates `[active, *retained]` in order and republishes the first that passes, else `no_recoverable_skillset`.
7. **Eviction.** Generations dropped past `retain=2` are **not** garbage-collected, deliberately matching `index.py` rather than diverging. `skill-status` lists unreferenced generations; deletion happens only via explicit `skill-prune`. Disk growth is therefore visible and operator-controlled.
8. **Degraded serve.** A missing pointer means the skills layer is simply inactive. An invalid or unreadable pointer disables dispatch with a typed `skillset_unreadable:<code>` warning and **never** fails `serve`, `doctor`, or corpus retrieval.

## Contract and File Changes

| File | Action | Change |
|---|---|---|
| `packs.py` | Modify | Additive extraction of parse/verify/version helpers to public names; `load_pack` behavior, codes, and order unchanged. |
| `pointer.py` | Create | Generic atomic pointer, identity, and `flock` serialization primitives extracted from `index.py`, entry key parameterized. |
| `index.py` | Modify | Import the extracted primitives; no behavior change. |
| `skills.py` | Create | `SkillPolicy`, `SkillPack`, `PermissionEnvelope` (+3 sub-models), `load_skill_pack`, `SkillSet` frozen registry, fail-closed codes reusing the `PackError` class. |
| `skillset.py` | Create | Compile, validate, promote, rollback, recover, prune over `skills/active.json`. |
| `dispatch.py` | Create | Pure `dispatch(classification, skill_set, host_skills)` -> ordered bounded matches, availability/divergence outcomes, ceiling gap. |
| `contracts.py` | Modify | `+ host_skills` (optional input), `+ envelope` (optional output), `EvidenceRecord.kind`/`ReadItem.evidence_kind` `+ "skill"`, `HostAction.kind` `+ draft_skill_candidate, install_skill`, new closed sub-models (all `ClosedModel`, per `test_contracts.py:11`). |
| `lookup.py` | Modify | `LookupResult.skills: tuple[SkillMatch, ...] = ()`; domain-gated skill matching. Existing source/capability behavior untouched. |
| `route.py` | Modify | `has_evidence` also counts `lookup.skills` (`route.py:129`) — strictly additive, never removes evidence. |
| `service.py` | Modify | Emit `skill:<id>@<version>` evidence refs (rationale in `authority_rationale`, tier/pack/signer/digest in `provenance_chain`, envelope in `envelope`), gaps and host actions; `read` gains a `skill:` prefix branch disclosing canonical-JSON metadata in `content`, exactly mirroring `_read_capability_one` (`service.py:338-369`, `service.py:406`). |
| `platform.py` | Modify | Load the second bundled domain pack into the registry; add `open_skillset` (fail-soft). |
| `cli.py` | Modify | Eight `skill-*` subcommands. |
| `data/` | Create | New signed domain pack + manifest; first-party T1 skill pack + manifest seeded from `Cerebro-IA/03-Skills/` (read-only input). |

## Implementation Sequencing (each unit ≤400 changed lines, tests included)

| Unit | Deliverable | Rollback |
|---|---|---|
| 1 | `pointer.py` extraction, `index.py` rewired, suite green unchanged | Revert; no data format changed |
| 2 | `packs.py` helper extraction + precedence test | Revert |
| 3 | `skills.py` schemas, envelope, prose-only invariant, signed load, fail-closed codes | Delete module |
| 4 | `skillset.py` activation/rollback/recovery/prune | Delete module + `skills/` dir |
| 5 | New bundled domain pack, `load_registry`, `LookupResult.skills`, `route.has_evidence`, negative controls, `test_platform.py:136` update | Remove pack; revert loader |
| 6 | `contracts.py` deltas + opt-in gate + byte-identical-legacy-response test | Revert models |
| 7 | `dispatch.py` + `service.py` wiring (refs, ceiling gap, availability/divergence) | Gate returns no refs |
| 8 | CLI lifecycle commands and approval records | Remove subcommands |
| 9 | First-party T1 pack authoring + packaging assertions | Ship without pack |
| 10 | Expiry/advisory demotion + `draft_skill_candidate` gap action | Demote-only, no drafting |

## Testing Strategy

| Layer | What | How |
|---|---|---|
| Unit | Fail-closed codes, envelope default-deny, prose-only rejection, dispatch determinism and ceiling, pointer traversal/symlink rejection | Pure functions + fixture packs, injected `today` |
| Integration | Promote/rollback/recover, non-destructive validation failure, concurrent promote under `flock`, degraded serve on unreadable pointer | Real temp dirs, real files |
| Contract | Opt-in gate produces byte-identical legacy responses; every new model closed; no body reachable through either tool | `model_json_schema()` + golden response comparison |
| Negative control | Mutated pack/skill domain flips the reconciliation assertions | Fixture mutation must fail the test |

Full suite runs from an external `UV_PROJECT_ENVIRONMENT` (`python -m pytest tests -q -p no:randomly`), never the registered `.venv`. `cerebro-retrieval/scripts/verify_legacy_baseline.py` passes before and after every unit.

## Risks and Regressions

| Risk | Handling |
|---|---|
| `packs.py` extraction reorders observable `PackError` precedence | Extraction-only diff, existing suite, new precedence test |
| `test_platform.py:136` must change | Additive tuple update, justified; expiry assertions retained |
| Broader reading of the routing delta would also re-gate legacy capabilities | Not done: it breaks four verified assertions. Flagged for `sdd-verify` as a deliberate spec-vs-test judgment |
| New bundled pack introduces a second expiry/freshness time bomb (`registry_load_failed`) | Long review window; `doctor` already warns 7 days ahead |
| Pinned-schema clients that opt in see new enum members | Opting in is itself a new client behavior; live clients read the published schema |
| Static analysis mistaken for a safety guarantee | Advisory-only, per-finding acknowledgment, stated in disclosure and CLI output |
| Skill sprawl re-inflates host context | Declared ref ceiling (default 5) plus excess reported as a gap |
| Unbounded disk growth from un-GC'd generations | Visible in `skill-status`; explicit `skill-prune` only |
| First-party pack quality, not mechanism, decides whether the install promise is real | Unit 9 is authoring work, not plumbing |

## Open Questions

- [ ] Ref ceiling value: 5 assumed. Not derived from measurement.
- [ ] Whether `skill-prune` belongs in v1 or ships after the first generation is evicted.
- [ ] Whether the new bundled domain pack should declare additional classifier domains (`cybersecurity`, `ux_design`) at once, or one domain at a time to keep abstention coverage obvious.
