## Verification Report — Slice 8A-2 (`cerebro-mcp` CLI)

**Change**: cerebro-agent-knowledge-router · **Boundary**: 8A-2 — `cli.py` (extended) + `test_cli.py`. Second of three units in Phase 8.
**Date**: 2026-07-24 · **Run**: apply → verify #1 (FAIL, 7th escape) → fix → confirm (FAIL, 8th escape) → **backstop** → confirm #2 (PASS). `size:exception` 419.

### Verdict

**PASS.** 246 tests. The `cerebro-mcp {init,serve,index,doctor}` CLI over `platform.py`'s loader — the candidate is now operable end to end.

### Two untyped escapes (7th and 8th of the change) → one class-closing backstop

- **7th**: a malformed-but-existing `--policy` YAML raises `yaml.YAMLError` (neither `ValueError` nor `OSError`), escaping `_cmd_index` as a bare traceback. Fixed by catching `yaml.YAMLError` (targeted).
- **8th**: `_cmd_init`'s `run_init → ensure_private_dirs → mkdir` (and `write_text`) raises a bare `OSError` (e.g. `NotADirectoryError`/`FileExistsError`) when a path segment is a file — `_cmd_init` had no guard and `cerebro_mcp_main` caught only `CliError`. Found independently by both the verifier and the orchestrator's parallel sweep.
- **The fix** (user chose it over an 8th per-command patch): a **backstop** — `cerebro_mcp_main` now catches `Exception` after `except CliError`, converting any unenumerated failure to a typed nonzero exit, never a bare traceback. `SystemExit` (argparse) and `KeyboardInterrupt` (serve shutdown) are `BaseException`s and correctly still propagate. Verify confirmed this is **structural class closure**: every `_cmd_*` path funnels through the one try, so no 9th untyped escape is possible via the CLI. An 11-case adversarial sweep (corpus-root a file, policy a dir, corrupt/mistyped config, malformed build-config/active.json, each dir as a file, unwritable parent) all exit typed. Both escapes have falsifiable regressions.

### Coverage (independently confirmed)

No regression to the Slice-3 `main`/`_embedding_fingerprint` (byte-identical; the `__main__` guard change breaks nothing — nothing invoked the old interface). `init` idempotent, registers nothing, real user dirs never created. `index` requires `--corpus-root`/`--policy`, calls `ensure_private_dirs` first (closing 8A-1's WARNING), builds only under the private data dir, never `cerebro.db`; embedder injectable. `serve` wiring never blocks a test, stdout stays the JSON-RPC channel. `doctor` read-only + a 7-day pack-staleness warning. Real-pipeline test drives init→index→doctor→serve-wiring over the mcp in-memory transport with a real index + fake embedder. Carried WARNINGs (`_cmd_serve` OSError-only catch; broad `_cmd_index` catch) and the SUGGESTION (`uuid4` candidate filename) judged acceptable across all three passes.

### Gates & Preservation

246 passed; `pip_audit` clean; `uv.lock` `4e40f608…` and `pyproject.toml` byte-identical (8A-2 touches no lock — that is 8B); frozen modules zero-diff; baseline `pass`/`errors: []` before and after; corpus/DB/registrations unchanged; real `~/…/cerebro-router` dirs never created. Boundary `cli.py` delta 243 + `test_cli.py` 176 = **419**, approved `size:exception`. Note: a transient `test_legacy` failure under `pytest-randomly` ordering (WAL churn on the live DB from an unrelated subprocess test) is pre-existing test-ordering flakiness, not a code defect; `-p no:randomly` is a clean 246/246 and the DB is byte-identical before/after.

### Next Recommendation

8A-2 verified. Proceed to **8B** (packaging: `package=true`, declare `platformdirs`/`mcp` as direct deps, regenerate `uv.lock`, **LOCK REBASELINE** — the first authorized change to the preservation baseline — build wheel/sdist, verify clean private-default install). 8B needs a user decision: it touches the preservation mechanism. Per interactive mode: **stop and await approval.**

---

## Verification Report — Slice 7B (MCP protocol server) — completes Phase 7

**Change**: cerebro-agent-knowledge-router · **Boundary**: 7B only — `mcp_server.py` + `test_mcp_contract.py`. Third of three units; on PASS completes Phase 7 (two-tool local MCP).
**Date**: 2026-07-23 · **Run**: apply, one orchestrator pre-verify probe (found + fixed a stage-error gap), then one independent pass — **PASS on the first independent attempt** (0 CRITICAL, 0 WARNING, 1 cosmetic SUGGESTION), the first slice in the change to do so.

### Verdict

**PASS.** 229 tests. `build_server(deps) -> Server` exposes exactly `investigate_work`/`read_evidence`. `size:exception` 406.

### Central Question — FastMCP vs lowlevel Server (verdict A: deviation correct)

design.md says "FastMCP publishes `outputSchema`", but the apply used `mcp.server.lowlevel.Server`. The independent pass PROVED the deviation is correct, not just asserted it: it built a real FastMCP tool and drove it through a real session — an unknown top-level field (`{"task": "hello", "unknown_field": "malicious"}`) was **silently dropped** (FastMCP's `ArgModelBase` has no `extra="forbid"`, so pydantic's default `extra="ignore"` applies to every `create_model`-derived argument model). That would defeat "server validation remains authoritative". The lowlevel Server with `validate_input=False` + direct pydantic construction of the raw arguments correctly rejects unknown/invalid/out-of-bounds/cross-field-invalid input (`isError=True`, `structuredContent=None`, no work), publishes `outputSchema` on both tools at the wire level (satisfying the design intent), and keeps the flat `{"task": ...}` contract instead of a nested `{"request": {...}}` wrapper.

### The stage-error fix (orchestrator, pre-verify)

The apply's handlers caught only `ServiceError`; `investigate()` documents that stage errors propagate unwrapped, and a broken/closed snapshot raises `RetrievalError("snapshot_unreadable")` — reachable — which would reach the client as the mcp SDK's generic exception format instead of the module's typed `{"error":{"code":...}}` envelope. Orchestrator widened the handlers to `except _STAGE_ERRORS` (ServiceError + the four composition stage errors, all `ValueError`-subclasses with `.code`) and added a falsifiable regression. The independent pass confirmed falsifiability (narrowing back → the test hard-crashes with an unhandled `ExceptionGroup`) and reachability (only `ServiceError`/`RetrievalError` are reachable in practice; the other three are safe defense-in-depth). This +6-line fix took the unit to 406, the approved `size:exception`.

### Coverage (all against the real in-memory `mcp` server + real deps)

Exactly two tools, deterministic across `PYTHONHASHSEED`; authoritative rejection of unknown field, missing required, wrong type, out-of-range `max_evidence`, and cross-field violations (`range_ref_missing`, `range_reversed`, `duplicate_ref`) — proving the pydantic path, not a declarative pre-check, is the gate; text fallback deserializes to `structuredContent` for both tools; malformed `tools/call` (nonexistent name → typed `unknown_tool`, `None`/non-dict arguments → clean protocol error) never crashes the session; stdout stays clean (verified, not superficial); injection inert through the protocol; `initialize` exercised every test; deps injected; the contract tests drive a real Server+client session with a real snapshot + real signed registry, never a mocked service.

### Gates & Preservation

229 passed; `pip_audit` clean; `uv.lock` `4e40f608…` unchanged; frozen Slices 1–7A-2 zero-diff vs `78506e6`; baseline `pass`/`errors: []` before and after; corpus/DB (`03e9f3c5…`), `cerebro.py`, `reindex.sh`, LaunchAgent, registrations all unchanged. Boundary `mcp_server.py` 164 + `test_mcp_contract.py` 242 = **406**, approved `size:exception`; genuinely reviewable.

**SUGGESTION** (cosmetic, addressed): the `tasks.md` 7A-2 line read "(TODO)" though the code had landed — corrected when marking 7.1 complete.

### Next Recommendation

**Phase 7 (7A + 7A-2 + 7B) is complete and independently verified — task 7.1 marked done.** Cerebro is now invocable by an MCP client through the two-tool contract, entirely as a local candidate; the legacy MCP runs untouched. Proceed to Slice 8 (packaging, `platformdirs` private directories, and the `cerebro-mcp {init,serve,index,doctor}` CLI that wires the real `ServiceDeps`). Per interactive mode: **stop and await approval.**

---

## Verification Report — Slice 7A (service composition)

**Change**: cerebro-agent-knowledge-router · **Boundary**: 7A only — `service.py` local composition + `test_service.py`. First of three units splitting Phase 7 (7A local composition, 7A-2 capability evidence, 7B MCP protocol).
**Date**: 2026-07-23 · **Run**: one independent pass (FAIL, 2 CRITICAL), remediation of one + re-scoping of the other, then an independent re-verification (PASS).

### Verdict

**PASS** within 7A's re-scoped boundary. 213 tests. `size:exception` 429.

### Sequencing and Defects

| Pass | Verdict | Found |
|---|---|---|
| Apply | — | `investigate()`/`read()` composition, deps injected, request_id content hash |
| Orchestrator probe | — | `investigate()` never surfaces `lookup.capabilities` → raised as central question |
| Independent verify #1 | **FAIL** | CRITICAL A: `max_evidence` unenforced (4th synthetic-fixture recurrence); CRITICAL B: capability evidence dropped (central question → verdict A) |
| Orchestrator + user | — | Fixed A in 7A; re-scoped B to a new unit **7A-2** (user chose the split) |
| Independent verify #2 | **PASS** | A closed and falsifiable; re-scoping honest; zero regression |

**CRITICAL A — `max_evidence` unenforced.** `retrieval.search` bounds by `max_candidates` (scan ceiling, default 50), not `max_evidence` (result-item ceiling, default 20), so `investigate()` could return up to 50 evidence items against a 20 budget. Invisible to the 2-note fixture — the 4th time a synthetic fixture hid a real-data defect. Fix: cap evidence to `request.budgets.max_evidence`, append `max_evidence_exceeded` degradation, set status `partial`. Regression test builds a 30-note real snapshot; independent re-verify confirmed falsifiable (revert → red) across `max_evidence ∈ {1,3,5,10,20,50}`.

**CRITICAL B — capability evidence dropped (verdict A, re-scoped to 7A-2).** Against the real bundled pack, the only path reaching `route()=="proceed"` is a `capability_recommendation` query, yet `investigate()` returns local retrieval evidence and discards the capability that justified proceeding — violating "Method and tool discovery". The frozen `EvidenceRecord(kind="capability")` exists precisely to carry it. Full disclosure also needs `read()` to resolve capability refs (permissions/limitations have no `EvidenceRecord` field). This is genuine composition work; with 7A at 400/400, the user chose to split it into unit **7A-2** rather than a larger single exception. Recorded in `tasks.md` as a known gap with its spec citation — not a hidden omission. 7A within its boundary (local investigation + reads + budget enforcement) is coherent; no other output is silently dropped.

### Coverage (re-verified, no regression)

request_id determinism (content hash, cursor excluded to fix a cursor-circularity bug the apply found); routing authority (abstained/route_only don't retrieve or fabricate); exact reads + working continuation cursor; forged cursor → `invalid_cursor`; `cursor_not_supported` on investigate (judged honest — `retrieval.search` has no offset, so a facade cursor would be a false promise); `max_output_chars`/`max_extracted_chars` enforced; typed-and-total (no 6th untyped escape from the added `len()`/slice); injection inertness end-to-end. Deps injected via `ServiceDeps`; the real-pipeline test discipline held (real snapshot + real registry).

### Gates & Preservation

213 passed; `pip_audit` clean; `uv.lock` `4e40f608…` unchanged; frozen Slices 1–6B zero-diff vs `5717f6d`; baseline `pass`/`errors: []` before and after; corpus/DB/registrations unchanged. Boundary `service.py` 218 + `test_service.py` 211 = **429**, approved `size:exception`.

### Next Recommendation

7A closes within its boundary. Proceed to **7A-2** (capability evidence in investigate + `resolve_capability` in read, with its own real-pipeline regression test), then 7B (MCP protocol). Per interactive mode: **stop and await approval.**

---

## Verification Report — Slice 6B-3 (route-or-abstain decision)

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 6B-3 only — the route-or-abstain decision (`route.py`, `tests/test_route.py`). Last of the three units splitting the routing phase; completes Phase 6.
**Mode**: Standard (`strict_tdd: false`) · **Delivery**: interactive OpenSpec, ask-always, feature-branch-chain
**Date**: 2026-07-23
**Run**: one independent fresh-context pass (FAIL, 3 CRITICAL), remediation, then an independent confirmation of the fixes

### Verdict

**PASS** (on the remediated tree; the fix's independent confirmation is the final gate). 198 tests. Pure, deterministic decision consuming classification + lookup. Approved `size:exception` at 458 lines. **Both binding constraints satisfied.**

### Verification Sequencing

| Pass | Verdict | Found |
|---|---|---|
| Apply | — | Implemented `route.py` (5-rule precedence: consequential > no-evidence > regulated-missing-context > jurisdiction-mismatch > proceed) |
| Orchestrator probe | — | A caller-declared `regulated` produced `proceed` when assessed risk was `unknown` → raised as central question |
| Independent verify #1 | **FAIL** | 3 CRITICAL: binding-constraint-2 hole (verdict A); 5th untyped escape (`KeyError`); no real-pipeline test (3rd occurrence of the pattern) |
| Orchestrator confirm | PASS | Rank-swap fix correct across all 20 risk pairs; no over-gating regression |

### Defects Found and Closed (3 CRITICAL)

**D1 — binding constraint 2 violated (the caller's `risk_class` floor had zero effect).** `_RISK_RANK` ranked `unknown` above `regulated`, and Rule 3 (the regulated-context gate) keys on `effective_risk == "regulated"` exactly. Since the classifier only produces `risk="unknown"` for `domain=="unsupported"`, a caller declaring `risk_class="regulated"` on any unsupported-domain request got `effective_risk="unknown"` → Rule 3 skipped → `proceed`. Declaring `regulated` was *less* protective than declaring `low`. Reproduced end-to-end through the real `classify → discover → route` pipeline: "what tool helps with medical diagnosis" + `risk_class="regulated"` → `proceed`.

Fix (candidate ii, not i): `_RISK_RANK = {"low":0,"medium":1,"high":2,"unknown":3,"regulated":4}` — `regulated` is the ceiling; `unknown` still outranks low/medium/high. `unknown` is an epistemic state, not a severity above a caller-declared `regulated`, so it must not absorb the declaration. Candidate (i) — firing Rule 3 on `unknown` — was rejected because it would over-gate benign default-declared unsupported-domain lookups. Only the `unknown`+`regulated` combination changes behavior; all 19 other pairs are provably unchanged.

**D2 — 5th untyped escape.** `_RISK_RANK[assessed]` was an unguarded dict lookup; `RequestClassification` is a plain dataclass with no runtime Literal enforcement, so `risk="critical"` raised a bare `KeyError` (unlike `request.risk_class`, which pydantic validates at construction). Fix: `_effective_risk` raises typed `RouteError("invalid_risk_level")` for any out-of-range level.

**D3 — synthetic-fixture-masks-real-data (3rd occurrence).** No test exercised `route()` against a `LookupResult` from the real `discover()` over the real bundled pack — the same pattern that hid a CRITICAL in 6A and 6B-2, and exactly what let D1 ship. Fix: `test_real_pipeline_medical_tool_request_declared_regulated_does_not_proceed` runs the full pipeline over the real signed pack; plus `test_caller_declared_regulated_is_never_swallowed_by_unknown_assessed_risk` and `test_out_of_domain_assessed_risk_fails_typed_not_bare_keyerror`.

### Binding Constraints — Both Satisfied

1. **Domain-agnostic abstention**: `has_evidence` never reads `classification.domain`; general and unsupported (and all seven domains) with no evidence reach an identical `abstained`/`no_approved_pack_or_local_evidence` outcome. Consequential-action abstains identically across domains too.
2. **`risk_class` is a floor**: `effective_risk = max(assessed, declared)` by rank, with `regulated` the ceiling. A caller declaring higher than the classifier assessed always wins; the D1 fix ensures a declared `regulated` is never swallowed by an epistemic `unknown`.

### Decision Vocabulary and Precedence

`RouteOutcome = proceed | route_only | abstained` (the latter two are exactly `InvestigationResult.status` values; `proceed` signals later stages may continue). Closed `RouteReason` (5) and `Gap` (4) literals; no free-text field. Precedence: (1) consequential action → route_only/abstained; (2) domain-agnostic no-evidence → abstained; (3) regulated + missing jurisdiction/date → route_only with named gaps; (4) sources present but none jurisdiction-applicable → route_only; (5) default → proceed. The three evidence-normalization scenarios (cyber authorization, programming version, UX standards) are documented as Slice 10 duties, not routing triggers — structurally unreachable here since the decision has no conclusion field.

### Issues Found

**CRITICAL**: None outstanding. All three are closed and their regression tests are falsifiable.

**WARNING**: None new.

**SUGGESTION** (carried, for a future unit): expose the caller's original declared `risk_class` on `RouteDecision` (not only the collapsed `effective_risk`) so a later stage can audit the floor was honored; and consider whether `Intent.unclear` should gate more conservatively than `investigate` (currently treated identically) — a note for the evidence/context assembler.

### Mechanical Gates

```text
$ <ext>/bin/python -m pytest tests -q          → 198 passed  (was 195; +3)
$ <ext>/bin/python -m pip_audit                → No known vulnerabilities found
$ UV_PROJECT_ENVIRONMENT=<ext> uv lock --check → Resolved 78 packages
uv.lock SHA-256 4e40f608… unchanged; pyproject.toml unmodified; no dependency added
git diff --check clean; AST parse clean
```

Frozen Slices 1–6B-2 (`classify.py`, `lookup.py`, `registries.py`, `packs.py`, `contracts.py`, `retrieval.py`, `index.py`, `corpus.py`, `models.py`, `pyproject.toml`, `uv.lock`) show zero diff versus `c7428bb`; only `route.py` and `test_route.py` changed. Determinism confirmed across `PYTHONHASHSEED`. Purity by AST: imports limited to `dataclasses`, `typing`, `.classify`, `.contracts`, `.lookup` — no I/O, clock, randomness, or network. Baseline `"status": "pass"`, `"errors": []` before and after; corpus, DB, legacy runtime, registrations unchanged.

### Review Boundary

`route.py` 157 + `test_route.py` 301 = **458 lines**, under a `size:exception` approved 2026-07-23. Third exception on the routing sub-slices (6B-2 435, 6B-3 458). The unit's real decision logic is ~90 lines; the overage is the adversarial test suite that this change's defect history demands — including the three regression tests that close the three CRITICALs. The 400 budget was kept unchanged; each exception is judged on merits. Tests confirmed uncompressed and reviewable.

### Next Recommendation

6B-3 is verified and committed on `feature/cerebro-agent-knowledge-router`. **All three routing units (6B-1, 6B-2, 6B-3) have landed and verified — task 6B is complete.** Proceed to Slice 7 (two-tool local MCP: `investigate_work`, `read_evidence`), which will compose the frozen classify → lookup → route → retrieve stages behind the public contract.

Per interactive mode: **stop here and await explicit user approval.**

---

## Verification Report — Slice 6B-2 (source/capability lookup)

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 6B-2 only — deterministic source/capability lookup (`lookup.py`, `tests/test_lookup.py`). Second of the three units splitting the routing phase.
**Mode**: Standard (`strict_tdd: false`) · **Delivery**: interactive OpenSpec, ask-always, feature-branch-chain
**Date**: 2026-07-23
**Run**: one independent fresh-context pass (FAIL, CRITICAL), remediation, then an independent confirmation of the fix

### Verdict

**PASS** (on the remediated tree; the fix's independent confirmation is the final gate). 163 tests. Pure, deterministic, read-only over the loaded `Registry`. Approved `size:exception` at 435 lines.

### Verification Sequencing

| Pass | Verdict | Found |
|---|---|---|
| Apply | — | Implemented `lookup.py` (`discover`, `resolve_source`, `resolve_capability`) |
| Orchestrator probe | — | classifier `Domain` enum and pack `domains` have empty intersection → raised as central question |
| Independent verify #1 | **FAIL** | CRITICAL: no test pins `discover()` against the real bundled pack; central question resolved (B) |
| Orchestrator confirm | PASS | Falsifiability shown: an aligned pack (`domains=["programming"]`) → `domain_supported=True` |

### The central question — resolved (B)

`lookup.py` gates source matching on exact set membership (`domain in pack.domains`). The classifier's closed `Domain` enum (law, accounting, cybersecurity, programming, ux_design, general, unsupported) and the open `DomainPack.domains` have an **empty intersection today** — the only shipped pack declares `domains=["software-research"]`. Verify #1 ruled the exact-match logic **correct**: it mirrors the frozen `load_pack(..., domain=...)` membership test; `DomainPack.domains` was deliberately an open field predating the classifier enum; and a synonym map inside `lookup.py` would fabricate a correspondence only a pack author may assert. Proven by bypassing the (unenforced) Literal at runtime: an aligned pack resolves sources correctly. The dead-on-arrival state is a **vocabulary-authoring gap, not a lookup defect**.

**Constraint recorded (in `tasks.md`)**: reconciliation must live in future pack authoring — packs declaring classifier-domain identifiers, or an explicit documented domain contract — never in `lookup.py`. Today, source discovery is empty for ALL seven domains against the real registry, which is spec-compliant ("no applicable approved pack → abstain") and is the precondition 6B-3's binding constraint relies on.

### Defect Found and Closed (CRITICAL)

**No test pinned `discover()` against the real bundled pack.** Every fixture in `test_lookup.py` was synthetic; the one domain-gating "works" test used `domains=("general",)`, a value the real pack can never hold. So the fact that source discovery is empty for all seven domains against production data was unverified, and an accidental edit to `research-policy.json`'s `domains` would change routing with no failing test — the identical pattern to 6A's `max_bytes` defect (a fixture too small to see production behavior).

Fix: `test_real_bundled_pack_resolves_no_domain_sources_for_any_classifier_domain` loads the real signed pack through `load_pack`, builds a `Registry`, and asserts `domain_supported is False` and `sources == ()` for all seven `Domain` values, plus that capabilities still surface for `capability_recommendation`. Orchestrator confirmed falsifiability: mutating the pack to `domains=["programming"]` yields `domain_supported=True, sources=1`, so the test pins real behavior rather than a tautology.

The misleading header comment (WARNING) was also corrected: rule 1 now states plainly that all seven domains — not just general/unsupported — are empty against the real pack, cites the pinning test, and locates reconciliation in pack authoring.

### Match Rules (with spec citations)

1. Sources are domain-gated by exact `Identifier` membership — mirrors `load_pack(..., domain=...)`. Cites "Domain-Sensitive Outcomes and Unsupported-Domain Abstention".
2. Jurisdiction is disclosed via `jurisdiction_applicable`, never silently collapsed: `GLOBAL` or exact match → true; mismatch and `unknown` → false. Cites "Accounting authority is jurisdiction-sensitive" ("preserves those refs as non-applicable").
3. Capabilities are claim-type gated (`capability_recommendation`), not domain-gated — `CapabilityPolicy` has no domain field, and most tool/library questions classify as `general`. Cites "Local Knowledge and Capability Discovery".
4. `authority` and source-level `claim_types` are disclosed but never filter — filtering by authority would be a forbidden trust judgment.
5. No ranking: the registry's own deterministic order is preserved; match is boolean set membership.

### Issues Found

**CRITICAL / WARNING**: None outstanding. The CRITICAL and the header-comment WARNING are closed.

**WARNING (carried, non-blocking, for a future unit)**: all-or-nothing capability return will surface unrelated capabilities once the registry holds more than one — a `CapabilityPolicy` has no domain/scope field. Structurally correct today; flag for whoever next touches that frozen contract or a downstream relevance stage.

**SUGGESTION**: none material.

### Mechanical Gates

```text
$ <ext>/bin/python -m pytest tests -q          → 163 passed  (was 162; +1)
$ <ext>/bin/python -m pip_audit                → No known vulnerabilities found
$ UV_PROJECT_ENVIRONMENT=<ext> uv lock --check → Resolved 78 packages
uv.lock SHA-256 4e40f608… unchanged; pyproject.toml unmodified; no dependency added
git diff --check clean; AST parse clean
```

Frozen Slices 1–6B-1 (`classify.py`, `registries.py`, `packs.py`, `contracts.py`, `retrieval.py`, `index.py`, `corpus.py`, `models.py`, `pyproject.toml`, `uv.lock`) show zero diff versus `f807b9a`; only `lookup.py` and `test_lookup.py` are new. Determinism confirmed across `PYTHONHASHSEED`. Purity confirmed by AST: imports limited to `.classify`, `.packs`, `.registries`, `dataclasses` — no I/O, clock, randomness, network, install, or execution. No fifth untyped-escape found. Baseline `"status": "pass"`, `"errors": []` before and after; corpus, DB, legacy runtime, registrations all unchanged.

### Review Boundary

`lookup.py` 166 + `test_lookup.py` 269 = **435 lines**, under a `size:exception` approved by the user on 2026-07-23. Unlike 6A's exception (compression had hidden defects), verify #1 confirmed the code is uncompressed and reviewable; the overage is the required real-pack pinning test that closes the CRITICAL plus honest documentation, not hidden complexity. Trimming adversarial coverage to hit 400 was rejected as the inverse anti-pattern.

### Next Recommendation

6B-2 is verified and committed on `feature/cerebro-agent-knowledge-router`. Proceed to 6B-3 (route-or-abstain), which MUST honor the binding constraint: a domain-agnostic abstention gate ("no approved pack / no local evidence"), run identically for `general` and `unsupported`; and treat the caller's `risk_class` as a floor. Given today's empty source discovery for all domains, `route_only`/`abstained` is the only currently-reachable source-backed outcome — 6B-3's tests must reflect that.

Per interactive mode: **stop here and await explicit user approval.**

---

## Verification Report — Slice 6B-1 (classification)

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 6B-1 only — deterministic request classification (`classify.py`, `tests/test_classify.py`). Slice 6B was split into 6B-1 classification / 6B-2 lookup / 6B-3 route-abstain to avoid the compression that hid defects in 6A.
**Mode**: Standard (`strict_tdd: false`) · **Delivery**: interactive OpenSpec, ask-always, feature-branch-chain
**Date**: 2026-07-23
**Run**: one independent fresh-context pass (FAIL, CRITICAL found), remediation, then an independent confirmation of the fix (PASS)

### Verdict

**PASS.** 132 tests. One CRITICAL and two comment-level defects found and closed. The classifier is pure, deterministic, typed, and total.

### Verification Sequencing

| Pass | Verdict | Found |
|---|---|---|
| Apply | — | Implemented `classify.py` (intent, domain, claim type, risk, jurisdiction) |
| Orchestrator probe | — | Six real regulated professions classify as `general`/`low`, bucketed with trivia — raised as the central question |
| Independent verify #1 | **FAIL** | CRITICAL: NFC/NFD Unicode gap; overclaimed rationale; undocumented domain priority; central question resolved (B) |
| Independent verify #2 | **PASS** | Confirmed the NFC fix; normalize/casefold order sound over all 140 keywords; 0 CRITICAL/WARNING |

### The central question — resolved (B)

The classifier detects `unsupported` via a fixed ~12-word list; unlisted professions (commercial pilot, funeral director, food-truck permits, peluquería) fall to `general`/`low`, identical to "capital of France". Verify #1 ruled this **acceptable, not a 6B-1 defect**: whether a domain is *supported* depends on approved-pack availability, which is a registry question owned by 6B-2/6B-3. The design's Routing/retrieval row is explicitly "classifies … never concludes"; abstention lives in the Packs and Evidence rows. The closed 7-value `Domain` literal cannot represent arbitrary professions by construction, and that is correct.

**Binding constraint recorded for 6B-3** (in `tasks.md`, to be checked at its verify — CRITICAL if violated): the abstention gate MUST be domain-agnostic ("no approved pack / no local evidence") and run identically for `general` and `unsupported`. If 6B-3 keys abstention off `domain == "unsupported"` alone, the unlisted regulated professions will be answered instead of abstained. Also recorded: 6B-3 should treat the caller's `risk_class` as a floor.

### Defect Found and Closed (CRITICAL)

**NFC/NFD Unicode normalization gap.** `_words()` tokenized and casefolded but never Unicode-normalized. The keyword literals are NFC (Python source is), but macOS text fields and APFS filenames emit NFD, where an accented letter is a base plus a combining mark. NFD input silently failed to match every accented keyword. Reproduced: 6 of 8 Spanish queries misclassified on byte form alone — `auditoría` lost `accounting`, `médico` lost the medical safety net, all dropping to `general`/`low`. This is a real production trigger on the maintainer's own platform.

Fix: `unicodedata.normalize("NFC", text)` in `_words()` before tokenizing, plus a parametrized regression test. Verify #2 exhaustively confirmed all 140 keyword literals are already NFC and round-trip from NFD input, and that the `normalize→casefold` order is sound for this lexicon (checked against ß, Turkish I, Greek final sigma, ligatures).

Two comment-only defects also closed: the `_UNSUPPORTED_PROFESSION_WORDS` rationale no longer claims to satisfy "arbitrary unsupported profession" (it catches only explicitly-named professions; the real backstop is downstream), and the first-match domain priority is now documented as a deterministic tie-break, not a confidence judgment.

### Value Sets (with spec citations)

- `Domain` = law, accounting, cybersecurity, programming, ux_design, general, unsupported — the five professional families from "Domain-Sensitive Outcomes and Unsupported-Domain Abstention".
- `Intent` = investigate, consequential_action, unclear — `consequential_action` from "Read-Only Informational Boundary and Host Actions".
- `ClaimType` = factual, capability_recommendation, professional_conclusion — from "Local Knowledge and Capability Discovery" and the Read-Only boundary.
- `RiskLevel` = low, medium, high, regulated, unknown — derived from domain, deliberately independent of the caller's `risk_class` (reconciliation deferred to 6B-3 as a floor).
- `jurisdiction` — read verbatim from the structured field, else `"unknown"`; never guessed from `task` text.

### Issues Found

**CRITICAL / WARNING**: None outstanding.

**SUGGESTION**: Verify #2 proposes NFKC over NFC — a strict superset that also folds fullwidth Latin (`ｃｏｄｅ` → `code`) and compatibility forms. The risk asymmetry favors it (a missed keyword silently degrades to `general`/`low`; a spurious match fails safe by raising risk). Not adopted in 6B-1: the module targets bilingual EN/ES, for which NFC is confirmed sufficient, and NFKC is robustness against CJK/fullwidth input outside this unit's stated scope. Tracked as a one-word future change should the input surface widen.

### Mechanical Gates

```text
$ <ext>/bin/python -m pytest tests -q          → 132 passed  (was 125; +7)
$ <ext>/bin/python -m pip_audit                → No known vulnerabilities found
$ UV_PROJECT_ENVIRONMENT=<ext> uv lock --check → Resolved 78 packages
uv.lock SHA-256 4e40f608… unchanged; pyproject.toml unmodified; no dependency added
git diff --check clean; AST parse of all 16 files clean
```

Frozen Slices 1–6A (`contracts.py`, `packs.py`, `registries.py`, `index.py`, `corpus.py`, `models.py`, `retrieval.py`, `pyproject.toml`, `uv.lock`) show zero diff versus `c0c2ef8`. Determinism confirmed across `PYTHONHASHSEED` values. Adversarial inputs — lone/repeated combining marks, NFD at the 4096 bound, mixed NFC/NFD, ZWJ, RTL — all yield a typed result or `ClassificationError`; no bare exception. Purity confirmed by AST: imports limited to `re`, `unicodedata`, `dataclasses`, `typing`, `.contracts` — no I/O, clock, randomness, network, or registry.

### Review Boundary

`classify.py` 179 + `test_classify.py` 194 = **373 lines**, within the 400 budget (no exception needed). Density is genuinely reviewable — 26 blank lines, max column 116. Splitting 6B into three units up front, rather than compressing, kept this unit under budget on the first try.

### Preservation

Baseline `"status": "pass"`, `"errors": []` before and after. Corpus 370 notes / `8cbcb107…`; DB `03e9f3c5…`, 34,770,944 bytes, 6,574 rows; `cerebro.py`, `reindex.sh`, `uv.lock`, LaunchAgent, both registrations, `.gitignore`/Higgsfield dirty state — all unchanged.

### Next Recommendation

6B-1 is verified and committed on `feature/cerebro-agent-knowledge-router`. Proceed to 6B-2 (source/capability lookup against the deterministic registries), then 6B-3 (route-or-abstain) — where the binding constraint above must be honored and checked. Mark the 6B checkbox complete only when all three units land and the combined unit verifies.

Per interactive mode: **stop here and await explicit user approval.**

---

## Verification Report — Slice 6A

**Change**: cerebro-agent-knowledge-router
**Verification boundary**: Slice 6A only — snapshot-bound local retrieval (`retrieval.py`, `tests/test_retrieval.py`, `evals/local-routing.jsonl`)
**Mode**: Standard (`strict_tdd: false`) · **Delivery**: interactive OpenSpec, ask-always, feature-branch-chain
**Date**: 2026-07-23
**Run**: two independent fresh-context passes plus continuous orchestrator re-execution and adversarial probing

### Verdict

**PASS.** 97 tests pass. Six defects were found and closed. Not one was found by the pass that wrote the code.

The final `scorable = passages[: len(tokenized)]` fix received its own independent confirmation (verify #3): a 22,230-case sweep found zero untyped escapes, the regression test was proven falsifiable (1,800 crashes against the reverted code), and the disputed reachability of D6 was settled by proof — the pre-fix bug was unreachable under the default `time.monotonic` and required a caller-supplied non-monotonic clock. No coverage gap remains.

### Verification Sequencing

| Pass | Verdict | Found |
|---|---|---|
| Apply | — | Implemented `retrieval.py`, tests, eval fixtures |
| Orchestrator probes | FAIL | `max_bytes` capped local retrieval at **14.6% of the corpus** |
| Orchestrator probes | FAIL | `max_elapsed_ms` guarded ~0.03% of the request cost |
| Independent verify #1 | **FAIL** | CRITICAL silent degradation; CRITICAL malformed `heading_path`; corrupt vector killing the whole leg; eval file never read |
| Independent verify #2 | **FAIL** | CRITICAL: untyped `ValueError` introduced by the deadline restructuring |
| Independent verify #3 | **PASS** | Confirmed the D6 fix; 22,230-case sweep clean; D6 reachability settled by proof |

### Defects Found and Closed

**D1 — retrieval saw 14.6% of the corpus.** `Budgets.max_bytes` — a *network transfer* ceiling per design.md's Network boundary — was applied to the local snapshot scan. Measured against the live corpus: 7,659,512 bytes across 6,574 passages against a 1,000,000-byte default, so the scan stopped at passage 963. Because the scan is `ORDER BY ref`, the same ~85% was invisible on every query: a fixed blind spot, not a sample. Independent verify #1 was asked to challenge the fix and confirmed it, including proving the new regression test goes red against the pre-fix logic.

**D2 — `max_extracted_chars` was declared and enforced nowhere.** A hard-coded `_SNIPPET_CHARS = 500` ignored it.

**D3 — `max_elapsed_ms` bounded almost nothing.** The deadline guarded only the row scan, measured at ~0.1 ms of a ~330 ms request; tokenization (113 ms), document frequency (94 ms) and cosine scoring (120 ms) ran unguarded. A caller asking for `max_elapsed_ms=1` still paid ~328 ms. After the fix, measured on the real corpus: **266 ms → 7.7 ms**.

**D4 — silent unreported degradation (CRITICAL).** The scoring functions returned `{}` when a leg ran but matched nothing, while `search()` only tested `is None`. Embedding-dimension drift therefore produced zero vector ranks with an empty `degradation` tuple — a dead leg, reported as nothing at all.

**D5 — malformed `heading_path` (CRITICAL).** Broader than first reported. `5` and `null` raised a bare `TypeError`; `"5"` silently became `("5",)` and `{"a": 1}` silently became `("a",)`. Since `heading_path` feeds the citation locator, the silent cases were provenance corruption — worse than the crash, because a crash is visible.

**D6 — untyped `ValueError` introduced by the D3 fix (CRITICAL).** `zip(passages, tokenized, lengths, strict=True)` assumed all three stayed the same length, but the deadline can truncate `tokenized` independently. `strict=` raises during the iterator advance, *before* the loop body's own deadline check runs.

Reachability, stated precisely: with the default `time.monotonic` this is **not** reachable — ten deadline values across a 4,000-passage corpus never triggered it, because once the deadline passes the scoring loop breaks at position 0. It requires a caller-supplied `clock` that reports expired and then unexpired. Independent verify #2's claim that it was reachable "on any corpus over ~65 passages" overstates it. But `clock` is a public documented parameter, a bare exception escaping violates the module's own typed-error contract regardless of route, and the correctness of the unfixed code rested on an implicit ordering invariant that any later edit could break.

### Remediation

| Fix | Closes |
|---|---|
| `max_bytes` removed from the local scan; budget mapping documented in the module header | D1 |
| Snippets bounded by `max_extracted_chars` with explicit `max_extracted_chars_exceeded` | D2 |
| `_expired()` re-checked every 64 items inside every scan and scoring loop | D3 |
| `_leg_state()` distinguishes `None` (`*_leg_unavailable`) from `{}` (`*_leg_no_matches`) | D4 |
| `_heading_path()` requires a JSON list of strings, raising typed `corrupt_snapshot_metadata` | D5 |
| `scorable = passages[: len(tokenized)]` | D6 |
| Broad `except` narrowed to the `embed_query` call; per-candidate `_floats()` returning `None` | one corrupt vector no longer kills the leg |
| `test_eval_fixtures_are_bilingual_and_reference_real_corpus_notes` | eval file had no reader |

**Budget mapping, now explicit**: local scan bounded by `max_elapsed_ms`; results by `max_candidates`; returned text by `max_extracted_chars`. `max_bytes` is the network ceiling and Slice 9's fetch layer owns it.

### Orchestrator Probe Evidence (post-remediation)

| Probe | Result |
|---|---|
| Embedding-dimension drift | `vector_leg_no_matches` reported |
| Lexical leg matching nothing | `lexical_leg_no_matches` reported |
| `heading_path` = `"5"`, `5`, `{"a":1}`, `null`, `[1,2]`, `[[]]` | typed `corrupt_snapshot_metadata`, all six |
| Valid `heading_path` | still parses to `("A", "B")` |
| One corrupt vector among three | only its own candidate lost; leg survives |
| `max_elapsed_ms=1` on 6,574 real passages | 266 ms → 7.7 ms, reported |
| Full budget on 6,574 real passages | all 6,574 scanned |
| 200 clock-expiry combinations × 4 corpus sizes × with/without embedder | **zero untyped escapes** |
| Determinism across `PYTHONHASHSEED` 0/1/42/2026 | identical ordering |

### Performance and Cost, Measured

Full-corpus BM25 on 6,574 real passages: 119 ms tokenize + 92 ms document frequency + 67 ms score = **279 ms**, against a 10,000 ms budget. The full-scan architecture is viable and the candidate index does **not** need an FTS5 table added — which would have meant reopening frozen Slice 3.

The candidate schema (`index_meta`, `manifest`, `documents`, `passages`) genuinely has no FTS5 and no vec0 table, unlike the legacy `cerebro.db`. Pure-Python BM25 was a forced and correct choice for a `mode=ro&immutable=1` snapshot with `query_only=ON`.

Memory is ~17.8 MB per call (7.7 MB text + 10.1 MB vectors) with no caching; independent verify #2 measured ~86 MB peak allocation at production scale. In-boundary for 6A, but a real load question for Slice 7 under concurrency.

### Issues Found

**CRITICAL**: None outstanding. D4, D5 and D6 were CRITICAL and are closed.

**WARNING**:

1. **No per-request caching.** Every call re-scans, re-tokenizes and re-parses the whole snapshot. Not a 6A acceptance failure; a real design question for Slice 7's MCP server under concurrency.
2. **The eval fixtures are still not executed at production scale.** The new test parses `evals/local-routing.jsonl` and confirms all nine referenced vault notes exist, closing the literal "never read" defect. But no harness runs those bilingual queries against the real 370-note corpus, so 6A's bilingual recall claim rests on synthetic 3-passage fixtures. The evaluation harness is Slice 12 scope; this is tracked, not resolved.

**SUGGESTION**:

1. Independent verify #2 judged the `size:exception` the wrong call, noting that `retrieval.py` bundles at least four concerns and that D6 lives inside its densest function. That judgment is recorded. The counter-evidence is that the exception was granted *because* compression had already hidden two CRITICALs at 383 lines. Slice 6B should be split by concern from the start rather than compressed to fit.

### Review Boundary

| File | Lines |
|---|---:|
| `retrieval.py` | 335 |
| `tests/test_retrieval.py` | 219 |
| `evals/local-routing.jsonl` | 9 |
| **Total** | **563** |

Against a 400-line budget with a `size:exception` approved by the user on 2026-07-23. The original estimate given when requesting the exception was ~450; the delivered unit is 563, **25% above that estimate** — the module grew more when spacing was restored (221 → 335) and the adversarial tests grew more than projected (153 → 219). Density is now 47 blank lines with a maximum column of 109, between `packs.py` (2 blanks) and `index.py` (75 blanks).

No tracked file was modified: `git diff --numstat d413d08 -- cerebro-retrieval/` is empty. The frozen Slice 1–5B modules are untouched.

### Preservation

Baseline verified before and after every phase; every run returned `"status": "pass"` with `"errors": []`.

| Asset | Evidence | Result |
|---|---|---|
| Corpus | 370 notes; manifest `8cbcb107…42745` | ✅ unchanged |
| Live DB | `03e9f3c5…3c10d`; 34,770,944 bytes; 6,574 chunk/FTS/vector rows | ✅ unchanged |
| `cerebro.py` / `reindex.sh` | `a05a8c25…` / `8beb3dc0…` | ✅ unchanged |
| `uv.lock` / `pyproject.toml` | `4e40f608…` / `59f5d4ec…`; no dependency added | ✅ unchanged |
| LaunchAgent plist | `2421b8a4…` | ✅ unchanged |
| Registrations, Graphify, `.gitignore` mod, Higgsfield note | as before | ✅ unchanged |

### Next Recommendation

6A is verified and committed on `feature/cerebro-agent-knowledge-router`; task 6A is marked complete. Proceed to Slice 6B (routing), split by concern up front, on the normal ≤400 budget.

Per interactive mode: **stop here and await explicit user approval.**

---

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
