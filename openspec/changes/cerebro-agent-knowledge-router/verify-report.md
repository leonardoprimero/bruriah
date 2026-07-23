## Verification Report — Slice 5B

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 5B only — complete budgets, evidence/read provenance fields, closed safe YAML/JSON loading, plus the W2/W3 hardening carried over from the Slice 5A report
**Mode**: Standard (`strict_tdd: false`)
**Delivery**: interactive OpenSpec, ask-always, feature-branch-chain, `review_budget_lines: 400`
**Date**: 2026-07-23
**Run**: three verification passes — two independent fresh-context `sdd-verify` agents and continuous orchestrator re-execution of every gate with self-authored adversarial probes

### Verdict

**PASS** — no open coverage gaps; 1 WARNING / 1 SUGGESTION, neither blocking.

Slice 5B is functionally complete against its acceptance criteria. Three separate defects were found and closed during verification — each by a different pass, none by the pass that wrote the code.

### Verification Sequencing and What Each Pass Found

| Pass | Verdict | Found |
|---|---|---|
| Apply (initial) | — | Implemented budgets, partial evidence fields, ReadItem fields, YAML loading, W2, W3 |
| Orchestrator review | FAIL | `EvidenceRecord` under-implemented — 15 read as a total instead of a missing count |
| Independent verify #1 | **FAIL** | **CRITICAL**: raw `TypeError` escaped the typed-error contract |
| Orchestrator probes | FAIL | Duplicate mapping keys silently downgraded a security control |
| Independent verify #2 | **PASS** | Confirmed both fixes closed under ~30 self-authored probes |
| Orchestrator probes | FAIL | YAML-1.1 implicit typing broke declared JSON/YAML determinism |
| Independent verify #3 | **PASS** | Confirmed the JSON-core resolver hardening; 0 CRITICAL / 0 WARNING / 0 SUGGESTION |

Every fix received independent fresh-context review. Verify #3 was commissioned specifically because the resolver hardening landed after verify #2, so no change in this slice rests on a single pair of eyes.

Verify #3 enumerated the resulting resolver map directly: `_StrictYamlLoader.yaml_implicit_resolvers` is confirmed present in the class `__dict__` and not identical to `SafeLoader`'s, holding **25 entries across 14 first-char buckets versus SafeLoader's 54 across 30**. No YAML-1.1 resolver survived. It further confirmed that with the `''` bucket now empty, a bare `key:` yields `''` rather than `None`, and judged this safe: every field in `DomainPack`, `SourcePolicy`, and `CapabilityPolicy` is required and strictly typed, so `''` and `None` fail identically — verified against `regulated_domain`, `freshness_days`, `expires_at`, `provenance`, `domains`, and a bare list item in `jurisdictions`.

### Defects Found and Closed

**D1 — `EvidenceRecord` under-implemented.** Task 5B's phrase "the missing six `Budgets` ceilings, fifteen `EvidenceRecord` provenance fields, eight `ReadItem` evidence-state fields" uses *missing* counts throughout. The initial apply read six and eight as missing (correctly) but fifteen as a total, adding only seven fields. The decisive evidence was not arithmetic but internal incoherence: `ReadItem` received `citation_locator` and `provenance`, while `EvidenceRecord` — the canonical model those read items derive from — did not. A read item cannot return a citation locator its evidence record never stored. The nine absent spec-mandated fields were `publisher`, `citation_locator`, `published_at`, `updated_at`, `effective_at`, `language`, `reuse`, `provenance_chain`, `extraction_method`.

**D2 — raw `TypeError` escaped the typed-error contract (CRITICAL).** `yaml.safe_load` is safe against `!!python/*` tags but still resolves YAML-1.1 implicit scalars into non-JSON Python types: `!!binary` → `bytes`, unquoted `2026-07-23` → `date`, `2026-07-23 10:00:00` → `datetime`. `load_pack` then called `json.dumps(data)` inside `except (ValidationError, ValueError)`. `json.dumps` raises `TypeError`, which is **not** a `ValueError` subclass, so it escaped uncaught:

```text
!!binary             -> TypeError: Object of type bytes is not JSON serializable
maintainer: 2026-07-23        -> TypeError: Object of type date is not JSON serializable
maintainer: 2026-07-23 10:00  -> TypeError: Object of type datetime is not JSON serializable
```

An unquoted date-like scalar is a realistic pack-author typo, not an attack. Any Slice 6–8 caller written as `except PackError:` would have crashed unhandled.

**D3 — duplicate mapping keys silently downgraded a security control.** Both `yaml.safe_load` and `json.loads` resolve duplicate keys last-wins. A pack declaring `regulated_domain: true` and later `regulated_domain: false` loaded successfully, escaping the `unsigned_regulated_pack` guard — the guard whose entire purpose is preventing unsigned local packs from authorizing regulated conclusions:

```text
regulated: true  then false  -> LOADED   regulated_domain=False   [control bypassed]
regulated: false then true   -> PackError(unsigned_regulated_pack)
```

The reverse order closes, so a single-ordering probe would have returned a false pass. **This was never a YAML-only defect** — the JSON path carried it since Slice 5A; YAML inherited and widened it. Signed packs were always immune (the digest covers raw bytes, so any tampering yields `digest_mismatch`), and exploitation required `allow_unsigned_local=True`, so it was never live.

**D4 — YAML-1.1 implicit typing broke the declared JSON/YAML determinism.**

```text
freshness_days: 1:20   -> LOADED with freshness_days = 80   [silently wrong policy value]
jurisdictions: [NO]    -> malformed_pack                    [Norway unauthorable unquoted]
jurisdictions: [ON]    -> malformed_pack                    [Ontario likewise]
```

Sexagesimal resolution silently corrupted a freshness-window control value. The boolean cases failed closed, so there was no security bypass — but in a system whose premise is jurisdiction-sensitivity for law and accounting, legitimate `NO`/`ON` jurisdiction codes being unrepresentable without quoting is a real defect. Decisive against 5B's own acceptance criterion "valid JSON/YAML is deterministic": in JSON `"1:20"` is a string, in YAML it was 80. The two formats disagreed.

### Remediation

| Fix | Closes |
|---|---|
| `_encode_pack` → `json.dumps(data, allow_nan=False)` inside `except (TypeError, ValueError)` | D2, plus `.nan`/`.inf`, which previously only failed by accident downstream in pydantic |
| `_unique_pairs` as `json.loads(object_pairs_hook=...)` and `_StrictYamlLoader.construct_mapping` rejecting duplicates, both via a `_DuplicateKey` sentinel → typed `PackError("duplicate_key")` | D3, in both formats |
| `_StrictYamlLoader.yaml_implicit_resolvers` reset to the JSON/YAML-1.2-core set (null, bool, int, float) | D4 |
| `ReadItem.provenance` promoted to `provenance_chain: Annotated[list[ShortText], Field(max_length=10)]`, matching `EvidenceRecord` | lossy-fidelity WARNING from verify #1 |
| Nine missing provenance fields plus an `ordered_dates` validator on `EvidenceRecord` | D1 |
| Regression tests for `future_review`, `malformed_manifest`, `signature_required` | pre-existing 5A coverage gap raised by verify #1 |

**Reviewer note**: `packs.py` now calls `yaml.load(raw, Loader=_StrictYamlLoader)`. `_StrictYamlLoader` subclasses `yaml.SafeLoader`; this is the standard idiom for custom mapping construction and is safe. A naive `yaml.load(`/`Loader=` grep will flag it — it is not a finding. Independent verify #2 confirmed no `!!python/*` tag can be constructed through it.

### Final Field Contracts

| Model | Fields | Composition |
|---|---:|---|
| `Budgets` | 10 | 4 original + the 6 missing ceilings (candidates, pages, redirects, network requests, bytes, extracted characters) |
| `EvidenceRecord` | 24 | 8 original + **15 provenance** + `uncertainty` |
| `ReadItem` | 17 | 9 original + the 8 evidence-state fields |

`uncertainty` is not one of the fifteen. It is retained as a parallel to `conflict` per design.md's Evidence boundary ("conflicts are classified; uncertainty lists missing checks"). Verify #1 noted spec A's literal text assigns uncertainty reason to *claims* — see WARNING 2.

### Build and Test Execution

```text
$ <ext>/bin/python -m pytest tests -q
73 passed in 6.19s                      # 49 at the start of Slice 5A

$ <ext>/bin/python -m pip_audit
No known vulnerabilities found

$ UV_PROJECT_ENVIRONMENT=<ext> uv lock --check
Resolved 78 packages

$ shasum -a 256 uv.lock
4e40f608b3c1625ae6b69ea7e18c80ed3f0857bf8d3d766b259a91c876dc2f87   # unchanged

$ git diff --check                      # clean
$ AST parse over 14 source/test/legacy Python files   # all ok
```

`pyproject.toml` unmodified; no dependency added. `pyyaml>=6.0.3,<7` was already declared and locked, so YAML support cost no lock change.

### Adversarial Probe Evidence (orchestrator, post-remediation)

All probes executed against the current tree from the external locked interpreter.

| Probe | Result |
|---|---|
| `!!binary`, unquoted date, unquoted datetime, `.nan`, `.inf` | `malformed_pack` — typed, no bare exception |
| Duplicate `regulated_domain` both orders, YAML and JSON | `duplicate_key` in all four |
| Duplicate `version` (rollback attempt), JSON | `duplicate_key` |
| `freshness_days: 1:20` | `malformed_pack` (was silently 80) |
| `jurisdictions: [NO, ON]` | loads as `["NO", "ON"]` (was rejected) |
| `regulated_domain: yes` / `off` | `malformed_pack` — JSON-consistent, no YAML-1.1 booleans |
| `expires_at: 2027-07-23` unquoted | loads as `date(2027, 7, 23)` (was `TypeError`) |
| Anchors/aliases, non-mapping roots, oversize | `malformed_pack` / `pack_too_large` |
| Valid signed pack | **loads** — hardening did not break the happy path |
| Unsigned without opt-in | `signature_required` |
| JSON vs YAML equivalence | identical `DomainPack` |

Independent verify #2 separately ran roughly 30 further probes — nested duplicates inside `sources[]`/`capabilities[]`, flow vs block style, merge keys, unhashable keys, multi-document streams, `!!set`/`!!omap`/`!!pairs`, and `!!python/name:` — and reported every one failing closed with a typed error.

### Spec Compliance Matrix

| Requirement / scenario | Evidence | Result |
|---|---|---|
| A — Bounded Investigation: all ten declared ceilings | `Budgets` 10 fields, bounded, tested | ✅ COMPLIANT |
| A — Evidence Normalization: complete per-record provenance | `EvidenceRecord` 24 fields, independently re-derived from spec line 185 by two passes | ✅ COMPLIANT |
| A — Exact Bounded Evidence Reading: per-ref evidence state | `ReadItem` 17 fields incl. the 8 evidence-state fields | ✅ COMPLIANT |
| A — "Missing metadata MUST remain `unknown`" | defaults test; no silent inference | ✅ COMPLIANT |
| A — read items must not substitute evidence | `provenance_chain` shape now equals `EvidenceRecord`'s | ✅ COMPLIANT |
| K — Versioned Registries: invalid packs fail closed | 19 prior modes plus `duplicate_key`, all typed | ✅ COMPLIANT |
| K — packs are policy, never professional answers | unchanged from 5A; fixture re-read | ✅ COMPLIANT |
| D-Packs — closed safe YAML/JSON loading | `safe_load`-derived strict loader, alias ban, size ceiling, same signature path | ✅ COMPLIANT |
| 5B — "valid JSON/YAML is deterministic" | identical `DomainPack` from both formats after the JSON-core resolver fix | ✅ COMPLIANT |
| W2 — typed `PackError("invalid_version")` | `_VERSION_PATTERN`; probes on `abc`, `''`, `latest` | ✅ COMPLIANT |
| W3 — cross-pack identifier uniqueness, ordering preserved | `registries.py`; deterministic ordering test intact | ✅ COMPLIANT |
| 5A boundary not weakened | 5A fixture edit gives the second pack distinct ids; original assertions unchanged | ✅ COMPLIANT |
| Security surface unchanged | pattern sweep clean; only `max_network_requests` (a budget name) and the one strict-loader call site | ✅ COMPLIANT |

### Issues Found

**CRITICAL**: None outstanding. D2 was CRITICAL and is closed.

**WARNING**:

1. **`EvidenceRecord.uncertainty` placement is arguable.** Spec A assigns "uncertainty reason" to claims, not evidence records; `ClaimRecord` currently has no such field and does not yet separate source text, extracted claim, assessment, and recommendation. That separation is Slice 10's scope (`evidence.py`). Tracked so it is not silently dropped.

**SUGGESTION**:

1. The bundled trust root remains the test signer `cerebro-release-test` (carried from the 5A report). A real release key must replace it before cutover, and the Slice 12 gate should assert no test signer stays trusted.

### Review Boundary

| File | 5A frozen | Current |
|---|---:|---:|
| `contracts.py` | 101 | 140 |
| `packs.py` | 145 | 207 |
| `registries.py` | 26 | 32 |
| `test_contracts.py` | 44 | 145 |
| `test_packs.py` | 73 | 169 |
| **Total** | **389** | **693** |

Net **+304**. Method and limitation: these files are untracked, so gross additions-plus-deletions cannot be measured. Identified in-place replacements against the 5A baseline (field reordering in `EvidenceRecord`, `_version` body, the validate call site, imports, the `ReadItem` field swap, the `registries` loop, the 5A fixture) total roughly 20 deleted lines, giving an estimated gross of **~325 changed lines against a 400-line budget**.

That fits, but the margin is now roughly 19% and narrowed with each remediation round. **Any further work on this unit should be split into a new slice rather than absorbed here.**

### Preservation

Baseline verified before and after every phase of this work; every run returned `"status": "pass"` with `"errors": []`.

| Asset | Evidence | Result |
|---|---|---|
| Markdown corpus | 370 notes; manifest `8cbcb107…42745` | ✅ unchanged |
| Live DB | `03e9f3c5…3c10d`; 34,770,944 bytes; 6,574 chunks/FTS/vectors; 384 dims | ✅ unchanged |
| Golden retrieval | 7 bilingual queries match the approved baseline | ✅ unchanged |
| Legacy `cerebro.py` | `a05a8c25…` | ✅ unchanged |
| `reindex.sh` | `8beb3dc0…` | ✅ unchanged |
| `uv.lock` / `pyproject.toml` | `4e40f608…` / `59f5d4ec…` | ✅ unchanged |
| LaunchAgent plist | `2421b8a4…` | ✅ unchanged |
| Registrations | `cerebro`, `cerebro-grafo` intact; no `cerebro-next` | ✅ unchanged |
| Registered `.venv` | never synced, activated, or written | ✅ unchanged |
| `git status` | identical path set throughout | ✅ unchanged |

No reindex process was running during any measurement. `claude mcp list` was never invoked; registrations were read from configuration only.

### Version Control (resolved 2026-07-23)

The `cerebro-retrieval` module was previously untracked, so no changed-line claim across Slices 1–5B could be measured rather than estimated, and a security-critical module had no history. On user approval this was closed **on a branch, without touching `main`**:

```text
$ git checkout -b feature/cerebro-agent-knowledge-router
3e9e2f3 feat(cerebro-router): add reproducible baseline, corpus evidence model and atomic index
c4b38eb feat(cerebro-router): add closed contracts, registries and signed domain packs
6ccff6b docs(openspec): add SDD artifacts for cerebro-agent-knowledge-router
31 files changed, 5733 insertions(+)
```

`cerebro.db` and `.venv` are excluded by the pre-existing `.gitignore`; `cerebro.py` and `reindex.sh` were already tracked. The unrelated `.gitignore` modification and the untracked Higgsfield note were deliberately left out and remain dirty, as the mandatory controls require. Nothing was pushed and no PR was opened.

Slices 5A and 5B share one commit because both units evolved the same files and only their final state exists on disk; splitting them would require fabricating intermediate history. Their separate review boundaries remain recorded here. **From Slice 6 onward, per-slice diffs are measurable with `git diff --numstat` and must no longer be estimated.**

### Risks

- This PASS authorizes no Slice 6 work, no MCP surface, no network capability, no registration change, and no client cutover.
- Slice 5B has no budget headroom left (~325 of 400 estimated). Nothing further may be absorbed into this unit.

### Next Recommendation

Accept Slices 5A and 5B as verified. Proceed to Slice 6 (local retrieval and routing) as a **fresh review unit**, measuring its diff against commit `6ccff6b` rather than estimating it.

Per interactive mode: **stop here and await explicit user approval.**

---

## Verification Report — Slice 5A

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 5A only — closed contracts, deterministic registries, Ed25519-signed JSON packs, tests and fixtures
**Version**: N/A
**Mode**: Standard (`strict_tdd: false`)
**Delivery**: interactive OpenSpec, ask-always, feature-branch-chain, `review_budget_lines: 400`
**Date**: 2026-07-23
**Run**: dual independent verification — one fresh-context `sdd-verify` agent plus an independent orchestrator re-execution of every gate. No product code, test, spec, task, or configuration file was modified. No checkbox was marked.

### Verdict

**PASS** — with 3 WARNING and 2 SUGGESTION items, none blocking the 5A boundary.

Slice 5A implements exactly the closed-contract, deterministic-registry, and signed-pack trust boundary it claims. Every failure mode driven by *pack-controlled* input fails closed with a typed `PackError`. The bundled signed fixture carries research-routing policy only. The module set has no network, subprocess, credential, filesystem-mutation, or auto-update surface. The 392-line boundary is under the 400-line review budget, and the live Cerebro baseline is byte-identical before and after verification.

Both verification passes independently reached PASS and independently identified the same two primary warnings. The orchestrator pass found one additional warning (W3) that the agent pass did not report.

### Completeness

| Metric | Value |
|---|---:|
| In-scope tasks | 1 (`5A`) |
| Verified complete | 1 |
| Focused 5A tests | 17/17 passed |
| Complete locked suite | 49/49 passed |
| Slice 5A changed lines | 392 / 400 budget |
| Deferred to 5B (correctly out of scope) | 4 items |
| Later tasks out of scope | 8 (`5B`, `6.1`–`12.1`) |

### Scoping Decision — What 5B Legitimately Defers

Task `5B` explicitly owns the following. Their absence in 5A is **by design** and is not a defect:

| Deferred item | Current 5A state | Owner |
|---|---|---|
| Six additional `Budgets` ceilings | 4 of 10 ceilings present | 5B |
| Fifteen `EvidenceRecord` provenance fields | 8 fields present | 5B |
| Eight `ReadItem` evidence-state fields | 9 fields present | 5B |
| Closed safe YAML loading (design `D-Packs` says "closed YAML/JSON") | JSON only, via `model_validate_json` | 5B |

### Repository and Review Boundary

- Repository `the repository root`, branch `main`, HEAD `9f2f2c8`.
- Slice 5A files are untracked, so Git cannot derive a per-unit numstat. The boundary is therefore measured as total authored lines across the exact 8-file set:

```text
$ wc -l src/cerebro_router/{contracts,packs,registries}.py \
        src/cerebro_router/data/{research-policy.json,research-policy.manifest.json,trust-roots.json} \
        tests/{test_contracts,test_packs}.py
     101 src/cerebro_router/contracts.py
     145 src/cerebro_router/packs.py
      26 src/cerebro_router/registries.py
       1 src/cerebro_router/data/research-policy.json
       1 src/cerebro_router/data/research-policy.manifest.json
       1 src/cerebro_router/data/trust-roots.json
      44 tests/test_contracts.py
      73 tests/test_packs.py
     392 total
```

- **392 ≤ 400.** No size exception applies.
- The pre-existing tracked `.gitignore` edit and untracked `Cerebro-IA/a-private-note.md` remain present and untouched.
- No branch, commit, push, PR, client registration, runtime cutover, checkbox completion, or Slice 5B work was performed.

### Environment Discipline

The registered environment `cerebro-retrieval/.venv` was never synchronized, activated, or written. All execution used a separate external environment:

```text
$ python3.12 -m venv <ext>/cerebro-slice5a-verify-20260723
$ UV_PROJECT_ENVIRONMENT=<ext> uv sync --locked      # 78 resolved, 74 installed
$ UV_PROJECT_ENVIRONMENT=<ext> uv lock --check       # Resolved 78 packages
```

No `claude mcp list` was invoked; MCP registrations were read from configuration only. The LaunchAgent was neither modified nor unloaded, and no reindex process was running during verification.

### Build and Test Execution

**Complete locked suite**: ✅ 49 passed

```text
$ CEREBRO_SERVER_PYTHON=<ext>/bin/python <ext>/bin/python -m pytest tests -q
49 passed in 6.17s
```

**Focused Slice 5A**: ✅ 17 passed

```text
$ <ext>/bin/python -m pytest tests/test_contracts.py tests/test_packs.py -q
17 passed in 0.06s
```

**Dependency/security audit**: ✅ Passed

```text
$ <ext>/bin/python -m pip_audit
No known vulnerabilities found
```

**Static/configuration gates**: ✅ Passed

```text
$ git diff --check                       # clean
$ ast.parse over all 14 source/test/legacy Python files   # all ok
```

Slice 5A adds no dependency to the lock; `uv.lock` SHA-256 is unchanged.

**Coverage**: ➖ Not configured; Standard Mode declares no non-zero threshold. Every in-scope behavior has committed tests plus independent adversarial probes.

### Security Surface Evidence

Slice 5A modules import only the standard library, `pydantic`, and `cryptography`:

```text
contracts.py   -> datetime, typing, pydantic
packs.py       -> base64, hashlib, datetime, pathlib, typing,
                  cryptography.exceptions, cryptography...ed25519, pydantic
registries.py  -> dataclasses, .packs
```

A pattern sweep over the three modules for `requests|httpx|urllib|socket|subprocess|os.system|popen|eval(|exec(|__import__|getenv|environ|open(...,"w")|write_text|write_bytes|mkdir|unlink|rmtree|pickle|yaml.load` returned **zero matches**. There is no network, subprocess, ambient-credential, auto-update, filesystem-mutation, or professional-conclusion behavior in the boundary.

### Signed Fixture — Policy, Not Professional Truth

`research-policy.json` was read in full. It contains a single `software-research` pack declaring: publisher identity, contextual authority class with rationale, temporal rules, citation rules, reuse rules, freshness window, limitations, biases, conflicts, and explicit exclusions; plus one capability entry recording canonical distribution, version, advisories, integrity method, and empty permissions/network/data access.

It contains **no** legal, medical, accounting, security, or any other professional conclusion, and no substantive domain answer. Representative policy text: *"Not professional advice and not proof of runtime safety."* and *"Discovery only; this policy does not install or execute packages."*

Structurally, `ClosedModel(extra="forbid")` makes a conclusion field unrepresentable — there is no schema location where professional truth could be smuggled in.

### Fail-Closed Evidence

Verified against pack-controlled input. Every case raises a typed `PackError`:

| Attack / condition | Code | Result |
|---|---|---|
| Tampered pack body (digest drift) | `digest_mismatch` | ✅ closed |
| Unknown signer | `unknown_signer` | ✅ closed |
| Invalid Ed25519 signature | `invalid_signature` | ✅ closed |
| Manifest `pack_id`/`version` mismatch | `digest_mismatch` | ✅ closed |
| Missing required source/pack fields | `malformed_pack` | ✅ closed |
| `expires_at <= reviewed_at` | `malformed_pack` | ✅ closed |
| Source freshness exceeds pack freshness | `malformed_pack` | ✅ closed |
| Expired pack | `expired_pack` | ✅ closed |
| Stale pack (past freshness window) | `stale_pack` | ✅ closed |
| Future review date | `future_review` | ✅ closed |
| Version rollback / downgrade | `version_rollback` | ✅ closed |
| Router incompatibility | `incompatible_pack` | ✅ closed |
| Malformed JSON | `malformed_pack` | ✅ closed |
| Malformed manifest | `malformed_manifest` | ✅ closed |
| Oversize pack (>64 KiB, checked pre-parse) | `pack_too_large` | ✅ closed |
| Unsigned pack without opt-in | `signature_required` | ✅ closed |
| Unsigned regulated pack even with opt-in | `unsigned_regulated_pack` | ✅ closed |
| Unsupported domain | `unsupported_domain` | ✅ closed |
| Unsupported jurisdiction | `unsupported_jurisdiction` | ✅ closed |

Ordering is correct: signature and trust-root validation execute **before** temporal, compatibility, and scoping checks, and the oversize guard executes **before** parsing.

The signature payload `1\n{signer}\n{pack_id}\n{version}\n{digest}` binds schema tag, signer identity, pack identity, version, and content digest — blocking cross-signer replay, cross-pack replay, and content substitution.

### Contract Closure Evidence

- `ConfigDict(extra="forbid", strict=True)` on every model; `strict=True` rejects type coercion, so `{"budgets": {"max_evidence": "2"}}` is rejected rather than coerced.
- `test_public_contract_schemas_are_nested_closed` recurses the generated JSON Schema including `$defs`, so nested closure is genuinely covered despite `__all__` exporting only the 4 public models.
- Bounds are enforced: `task` 1–4,096; `refs` 1–10 and unique; `ranges` bound to declared refs; reversed ranges rejected (`range_reversed`); `candidate_material` ≤20; `host_capabilities` ≤32.
- `InvestigationResult` carries every field the spec requires: `schema_version`, `status`, `request_id`, `evidence`, `claims`, `conflicts`, `gaps`, `host_actions`, `warnings`, `degradation`, `budgets`, `next_cursor`.

### Spec Compliance Matrix

| Requirement / scenario | Covering evidence | Result |
|---|---|---|
| A — Portable Two-Tool Public Contract: strict closed schemas, unknown fields and invalid types/bounds rejected before work | `ClosedModel` + recursive `$defs` closure test + 5 rejection cases | ✅ COMPLIANT |
| A — `investigate_work` / `read_evidence` input field sets | `InvestigationRequest`, `ReadRequest` field-by-field match to spec line 111 | ✅ COMPLIANT |
| A — result field set (`schema_version`…`next_cursor`) | `InvestigationResult` / `ReadResult` | ✅ COMPLIANT |
| K — Versioned Source Registries and Domain Packs: declared schema version, identity, version, maintainer, review date, claim types, jurisdictions, languages, freshness, cadence, license, provenance, compatibility | `DomainPack` required fields + `coherent` validator | ✅ COMPLIANT |
| K — source entries record authority class + rationale, temporal/citation/reuse rules, limitations, bias/conflict, exclusions | `SourcePolicy` required fields | ✅ COMPLIANT |
| K — packs are research policy and source maps, not professional answers | Full fixture read + structural impossibility under `extra="forbid"` | ✅ COMPLIANT |
| K — incompatible, expired, unsigned-or-unverified, invalid packs fail closed | 19-case fail-closed table above | ✅ COMPLIANT |
| K — no authority silently promoted | Authority is a declared `Literal` on the pack; loader never rewrites it | ✅ COMPLIANT |
| D-Models — closed nested schemas, declared field sets | `contracts.py` | ✅ COMPLIANT |
| D-Packs — Ed25519 release-manifest signing; unsigned local packs need explicit opt-in and cannot authorize regulated conclusions | `load_pack` signature path + `unsigned_regulated_pack` | ✅ COMPLIANT |
| D-Packs — closed **YAML**/JSON validation | JSON only in 5A | ➖ DEFERRED to 5B (declared) |
| D — registries load capabilities/sources with no install, execution, authentication, or popularity-as-integrity | `registries.py` is pure sorting over validated models | ✅ COMPLIANT |
| Slice budget — ≤400 changed lines | 392 measured | ✅ COMPLIANT |

**Compliance summary**: 12/12 in-scope checks compliant; 1 correctly deferred.

### Issues Found

**CRITICAL**: None.

**WARNING**:

1. **`tasks.md` "Mandatory Controls" cites pre-incident hashes.** Line 41 requires corpus aggregate `fda45d6d…` and live DB `d59066f4…`. The authoritative post-rebaseline values in `cerebro-retrieval/recovery/legacy-baseline-v1.json` are `8cbcb107…` and `03e9f3c5…`. A future slice author following `tasks.md` literally would compute a **false FAIL** on a healthy system. The other four mandatory hashes (`cerebro.py`, `reindex.sh`, `uv.lock`, LaunchAgent plist) remain correct and were confirmed unchanged. Correcting `tasks.md` is an artifact edit requiring explicit approval and was deliberately not performed in this run.

2. **`packs.py::_version()` leaks a bare `ValueError` on malformed router-controlled versions.** Independently reproduced:

```text
router_version='abc'      -> ValueError: invalid literal for int() ...   [not a PackError]
router_version=''         -> ValueError: invalid literal for int() ...   [not a PackError]
router_version='1.0'      -> PackError(incompatible_pack)                [closed]
router_version='1.0.0.0'  -> PackError(incompatible_pack)                [closed]
minimum_versions={'research.minimal': 'latest'} -> ValueError            [not a PackError]
```

`router_version` and `minimum_versions` are router-controlled, not pack-controlled, so this is **not reachable from untrusted pack content** and is not currently exploitable. It is nonetheless a hole in the typed-error surface: `Version` is pattern-validated on pack fields but not on these parameters. Harden in 5B by validating both against the `Version` pattern and raising `PackError("invalid_version")`.

3. **`Registry.from_packs` does not detect duplicate `source_id` / `capability_id` across different packs.** Uniqueness is enforced only *within* a pack. Independently reproduced:

```text
source_ids in registry:   ['canonical-project-docs', 'canonical-project-docs'] -> duplicated: True
capability_ids:           ['python.schema-validation', 'python.schema-validation'] -> duplicated: True
```

Ordering remains deterministic, so the literal "deterministic registries" requirement holds. But Slice 6 performs *source and capability lookup*, and an ambiguous identifier there means a lookup could silently resolve to either pack's policy — including selecting a weaker authority or reuse rule. This warning was found by the orchestrator pass only; the independent agent pass did not report it. Cheap to close in 5B alongside the other registry work.

**SUGGESTION**:

1. **The "policy-only" assertion is tautological.** `assert not hasattr(pack, "answers")` in `test_bundled_pack_is_policy_only_and_registry_is_deterministic` cannot fail, because `extra="forbid"` already makes the attribute unrepresentable. The underlying property is genuinely enforced by schema closure, so this is a test-expressiveness issue, not a safety gap. Consider asserting the positive instead: that every `SourcePolicy` carries non-empty `limitations` and `exclusions`.

2. **The bundled trust root is a test signer.** `trust-roots.json` contains only `cerebro-release-test`. Appropriate for the 5A fixture boundary, but a real release key must replace it before any cutover, and the cutover gate in Slice 12 should assert that no test signer remains trusted.

### Preservation and Operational Evidence

`scripts/verify_legacy_baseline.py` was executed from the external environment before and after all verification commands. Both runs returned `"status": "pass"` with `"errors": []`.

| Asset | Evidence | Before | After |
|---|---|---|---|
| Markdown corpus | 370 notes; manifest `8cbcb107d12817e9a6f9d5122e32ef0d2043c138ac37c3868f1cde0923d42745` | ✅ | ✅ |
| Live DB | 34,770,944 bytes; SHA-256 `03e9f3c59baeab23ec2eb74dfbc3dae38f73774af09c4b227ea3bb662553c10d` | ✅ | ✅ |
| DB rows | 6,574 chunks / chunks_fts / vec_chunks; integrity `ok`; 384 dims | ✅ | ✅ |
| Golden retrieval | 7 bilingual queries match the approved baseline exactly | ✅ | ✅ |
| Model snapshot | `faf4aa4225822f3bc6376869cb1164e8e3feedd0`; ONNX `634d0f66…`; mean attention-mask pooling | ✅ | ✅ |
| Runtime | Python 3.12.13, MCP 1.28.1, FastEmbed 0.8.0, NumPy 2.5.1, ONNX Runtime 1.27.0 | ✅ | ✅ |
| Legacy `cerebro.py` | `a05a8c25c24c9cae0cddfe54bbf1682057f6c48973102ee629b60e8dd3e7e661` | ✅ | ✅ |
| `reindex.sh` | `8beb3dc04e19ab2c2114b0d1089840d8520446a8991b1ad7e246aed930a5ff95` | ✅ | ✅ |
| `uv.lock` | `4e40f608b3c1625ae6b69ea7e18c80ed3f0857bf8d3d766b259a91c876dc2f87` | ✅ | ✅ |
| LaunchAgent plist | `2421b8a42ca0c79c81498393a157e1d98a4de5d0f2fdcfc560f1bdb08f3efcd5` | ✅ | ✅ |
| Registrations | `cerebro` → registered `.venv/bin/python`; `cerebro-grafo` → graphify tool python; no `cerebro-next` | ✅ | ✅ |
| Registered `.venv` | never synced, activated, or written | ✅ | ✅ |
| `git status` | byte-identical to the pre-verification listing | ✅ | ✅ |
| Router residue | no `active.json`, candidate `*.sqlite3`, lock, or staging temp in the repository | ✅ | ✅ |

### Risks

- W3 (cross-pack identifier collision) becomes materially exploitable only once Slice 6 performs source/capability lookup. Closing it in 5B keeps it a design detail rather than a retrieval defect.
- W1 will actively mislead the next slice author. It is documentation drift, not a code defect, but it degrades the very safety control the incident recovery was built to protect.
- This PASS covers the Slice 5A boundary only. It authorizes no Slice 5B work, no MCP surface, no network capability, no registration change, and no client cutover.

### Next Recommendation

Accept Slice 5A as verified.

`5A`'s own acceptance text forbids checkbox completion within the slice, and SDD convention assigns checkbox marking to `sdd-apply`. **Task `5A` therefore remains unchecked; a subsequent apply run must mark it.**

Before Slice 5B, request explicit approval to correct the stale mandatory hashes in `tasks.md` (W1). Then implement 5B, folding W2 (`_version` hardening) and W3 (cross-pack identifier uniqueness) into that slice.

Per interactive mode: **stop here and await explicit user approval.**

---

## Previous Verification — Slice 4 (2026-07-23, superseded as current report, retained for history)

**Change**: cerebro-agent-knowledge-router  
**Verification boundary**: Current Slice 4 remediation only — canonical active snapshots and descriptor-bound transient ABA safety, plus current full regression suite  
**Version**: N/A  
**Mode**: Standard (`strict_tdd: false`)  
**Delivery**: interactive OpenSpec, ask-always, feature-branch-chain  
**Date**: 2026-07-23  
**Run**: fresh independent verification; no delegation and no product edits

### Verdict

**PASS**

The current remediation closes both residual Slice 4 defects. Fresh canonical-corruption probes rejected impossible lines, deleted passages, stale text, stale paths, and stale hashes as `invalid_active_target`. A deterministic transient swap-validate-restore probe proved that validation reads the original held vnode through `/dev/fd/<fd>` while the pathname temporarily names a different database; result, pointer, and subsequent snapshot all retained the original build identity. The current suite collected and passed 32 tests.

### Completeness

| Metric | Value |
|---|---:|
| In-scope residual requirements | 2 |
| Verified complete | 2 |
| Current complete suite | 32/32 passed |
| Later tasks out of scope | 4 (`5.1`–`8.1`) |

### Repository and Review Boundary

- Repository: `the repository root`, branch `main`, HEAD `9f2f2c869650fc12a705e592fe11d5a1dc27b1d6`.
- Current implementation files are untracked; Git therefore cannot derive their per-unit numstat. Current lengths are 674 lines for `index.py` and 530 lines for `test_index.py`.
- Cumulative apply-progress records the final corrective unit at **171 human-authored changed lines**: `index.py` +59/-59 and `test_index.py` +53. It remains independently below the 400-line review limit; no size exception applies.
- The pre-existing tracked `.gitignore` edit and untracked Higgsfield note remain present and untouched.
- No branch, commit, push, PR, client registration, runtime cutover, or task `5.1`+ work was performed.

### Build and Test Execution

**Fresh locked setup**: ✅ Passed

```text
$ python3.12 -m venv <fresh-env>
$ UV_PROJECT_ENVIRONMENT=<fresh-env> uv sync --locked
Resolved 78 packages; installed 74 packages
$ uv lock --check
Resolved 78 packages
```

No package build is configured (`tool.uv.package = false`); clean locked installation is the applicable build gate.

**Collection**: ✅ Passed

```text
$ <fresh>/bin/python -m pytest --collect-only -q tests/test_index.py
24 tests collected in 6.82s

$ <fresh>/bin/python -m pytest --collect-only -q tests
32 tests collected in 7.88s
```

**Focused canonical/ABA remediation**: ✅ 6 passed

```text
$ <fresh>/bin/python -m pytest tests/test_index.py \
    -k "noncanonical_passage_semantics or transient_aba" -q
6 passed, 18 deselected in 6.87s
```

**Complete locked suite**: ✅ 32 passed

```text
$ CEREBRO_SERVER_PYTHON=<fresh>/bin/python \
    <fresh>/bin/python -m pytest tests -q
32 passed in 14.84s
```

**Retained active legacy runtime**: ✅ 3 passed

```text
$ CEREBRO_SERVER_PYTHON=cerebro-retrieval/.venv/bin/python \
    <fresh>/bin/python -m pytest tests/test_legacy.py -q
3 passed in 1.33s
```

**Dependency/security audit**: ✅ Passed

```text
$ <fresh>/bin/python -m pip_audit
No known vulnerabilities found
```

All 74 locked distributions were audited. The remediation adds no dependency, network, shell, credential, registration, or client behavior.

**Static/configuration checks**: ✅ Passed

- `git diff --check`
- `sh -n cerebro-retrieval/reindex.sh`
- AST parse of the legacy runtime plus all source/test Python files (8 total)
- `plutil -lint ~/Library/LaunchAgents/com.leguillo.cerebro-reindex.plist`
- `uv tree --locked --package cerebro-router --depth 1`

**Coverage**: ➖ Not configured; Standard Mode has no non-zero coverage threshold. Both in-scope behaviors have passing committed tests and independent runtime probes.

### Fresh Adversarial Evidence

All candidates, pointers, mutations, and swaps were created under an external temporary directory and removed automatically.

| Probe | Result | Runtime evidence |
|---|---|---|
| Impossible passage line (`start_line=999`) | ✅ Rejected | `invalid_active_target`; pointer bytes unchanged |
| Deleted passage | ✅ Rejected | `invalid_active_target`; pointer bytes unchanged |
| Stale passage text | ✅ Rejected | `invalid_active_target`; pointer bytes unchanged |
| Stale passage relative path | ✅ Rejected | `invalid_active_target`; pointer bytes unchanged |
| Stale passage source hash | ✅ Rejected | `invalid_active_target`; pointer bytes unchanged |
| Transient ABA swap-validate-restore | ✅ Descriptor-bound | SQLite reported `/dev/fd/4`; pathname DB was swapped build while descriptor DB remained original; result, pointer, and snapshot all used original build ID |

### Canonical Snapshot Evidence

The active snapshot path now uses the same complete canonical contract as candidate validation:

1. `_validate_database` reloads policy, rediscovers the current corpus, reparses every eligible document, rebuilds the expected manifest and metadata, and invokes `_validate_candidate` (`index.py:306–319`).
2. `_validate_candidate` requires exact manifest/document/passage counts and calls `_stored_document` for every canonical document (`index.py:242–265`).
3. `_stored_document` compares document identity and metadata plus every passage ref, document mapping, relative path, heading path, inclusive lines, exact text, source hash, metadata, vector type, and vector dimensions (`index.py:182–227`). Missing or extra passages fail through exact counts and strict row comparison.
4. `_open_entry` validates the descriptor-backed database canonically before returning `ActiveSnapshot`; failures are normalized to `invalid_active_target` (`index.py:488–517`).
5. Permanent coverage exists in the five-axis parameterized regression (`tests/test_index.py:453–472`).

### Descriptor-Bound ABA Evidence

The previous race does **not** remain:

1. `_controlled_file` resolves/constrains the candidate, opens it with `O_NOFOLLOW`, and captures its descriptor identity (`index.py:422–440`).
2. `_open_descriptor` opens SQLite read-only and immutable through `/dev/fd/<held-fd>` rather than through the mutable pathname (`index.py:414–419`).
3. `promote_candidate` passes that descriptor-backed SQLite connection to `validate_candidate`, fsyncs the same held descriptor, checks pathname identity as an additional drift guard, and publishes metadata obtained from that connection (`index.py:520–555`).
4. During the independent deterministic probe, the candidate pathname was parked, the replacement was moved into its place, validation ran, and both moves were reversed. While swapped, a fresh pathname connection saw the replacement build ID, but the validation connection at `/dev/fd/4` still saw the original build ID. Promotion returned and published the original ID, and the next active snapshot opened successfully with that same ID.
5. Permanent regression coverage is at `tests/test_index.py:475–500`.

### Spec Compliance Matrix

| Requirement / scenario | Covering runtime evidence | Result |
|---|---|---|
| K — each active passage maps to canonical identity, exact lines, hash, and evidence | Five committed corruption cases plus independent five-axis probe | ✅ COMPLIANT |
| K — active snapshot is one complete compatible index | Canonical count/row comparison and current 32-test suite | ✅ COMPLIANT |
| K — candidate validation failure does not publish another database identity | Descriptor-backed deterministic ABA regression and independent instrumented probe | ✅ COMPLIANT |
| D — validate manifest, mappings, counts, and smoke queries before publication | `_validate_database` + `_validate_candidate` + `_representative_queries` through held descriptor | ✅ COMPLIANT |
| D — requests snapshot one pointer and open read-only | `_open_entry` uses descriptor-backed `mode=ro&immutable=1`, `query_only=ON` | ✅ COMPLIANT |
| K — reproducible runtime and prior regressions | Fresh lock, 32-test suite, retained legacy suite, audit, and static checks | ✅ COMPLIANT |

**Compliance summary**: 6/6 in-scope checks compliant.

### Correctness and Design Coherence

| Decision | Status | Evidence |
|---|---|---|
| Complete canonical evidence validation | ✅ Followed | Fresh corpus is parsed and compared exactly against all stored documents/passages |
| Descriptor-bound validation/publication identity | ✅ Followed | Validation and metadata reads use `/dev/fd/<held-fd>`; pathname ABA cannot redirect them |
| Persistent identity drift guard | ✅ Followed | Path identity is still compared after validation |
| Immutable read-only request snapshot | ✅ Followed | SQLite URI is read-only/immutable and `query_only` is enabled |
| Permanent regressions for both incidents | ✅ Followed | Five canonical cases and deterministic ABA test are committed in current test source |

### Issues Found

**CRITICAL**: None.

**WARNING**: None within the requested Darwin/APFS verification boundary.

**SUGGESTION**:

1. Keep the instrumented `/dev/fd` assertion as an optional platform qualification test if this lifecycle is later supported outside the current Darwin deployment; the behavioral ABA regression already protects the production contract.

### Preservation and Operational Evidence

Before and after all verification commands:

| Asset | Evidence | Result |
|---|---|---|
| Markdown corpus | 370 notes; path-inclusive aggregate `fda45d6d2f9bb06285c6a17c2d8f79ffdf65c31d6b21d4c1a01228f5f365643d` | ✅ unchanged |
| Live DB | 34,770,944 bytes; SHA-256 `d59066f4ae2822a2684c90696b23f5cd154e147fb79fd9fc1227fdd8ab2174ff` | ✅ unchanged |
| Legacy `cerebro.py` | SHA-256 `a05a8c25c24c9cae0cddfe54bbf1682057f6c48973102ee629b60e8dd3e7e661` | ✅ unchanged |
| `reindex.sh` | SHA-256 `8beb3dc04e19ab2c2114b0d1089840d8520446a8991b1ad7e246aed930a5ff95` | ✅ unchanged |
| `uv.lock` | SHA-256 `4e40f608b3c1625ae6b69ea7e18c80ed3f0857bf8d3d766b259a91c876dc2f87` | ✅ unchanged |
| LaunchAgent plist | SHA-256 `2421b8a42ca0c79c81498393a157e1d98a4de5d0f2fdcfc560f1bdb08f3efcd5` | ✅ unchanged / valid |

> **Note (added 2026-07-23, Slice 5A verification):** the corpus and live-DB hashes in this Slice 4 table predate the approved incident rebaseline. The authoritative current values are corpus `8cbcb107…` and DB `03e9f3c5…` per `cerebro-retrieval/recovery/legacy-baseline-v1.json`. The other four hashes remain current.

- No repository `active.json`, candidate `*.sqlite3`, activation lock, or staging temporary remains.
- `cerebro` and `cerebro-grafo` remain connected with their original commands; no `cerebro-next` registration exists.
- LaunchAgent retains its original command/watch paths, run count 54, and last exit code 0.
- Tasks `5.1`–`8.1` remain unchecked.

### Risks

- Descriptor-backed SQLite behavior is proven on the current Darwin/APFS host. A future cross-platform deployment must qualify its descriptor path and filesystem durability semantics separately.
- This PASS covers only the two requested final Slice 4 remediation requirements and current regressions; it does not authorize tasks `5.1`–`8.1` or client cutover.

### Next Recommendation

Accept the current Slice 4 remediation as verified. In interactive mode, stop here. Do not begin task `5.1`, register `cerebro-next`, or cut over clients without separate explicit authorization.
