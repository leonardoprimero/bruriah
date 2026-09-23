# Feature: investigate-boundary-v2

**Branch:** `feat/investigate-boundary-v2` (on top of `feat/injection-eval` at bdfdbaa)
**Created:** 2026-09-23
**Route:** delegated direct (one bounded writer per task)
**TDD:** strict (global session configuration). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `ask-on-risk`. Forecast ~1,500 authored changed lines, over budget. Slicing
is decided at delivery time, because nothing is pushed before the fix lands (see Disclosure).
**RDD:** enabled for this repo. Last reviewed boundary: aca5c9e (later commits passive).
**Release:** 2.0.0. Contract break: `InvestigationResult.schema_version` "1" → "2".
**Disclosure:** local only, never pushed, until this branch closes every measured channel.
Benchmark, fix and before/after numbers ship together.

## Objective

Make `investigate_work` hold its documented promise that corpus prose never enters the model
context. Every author-controlled value leaves the response and becomes an opaque ref that the
caller dereferences explicitly through `read_evidence`.

## Problem

`evals/injection/` measures 7/11 attacker-controlled surfaces reaching the serialized response.
The design mapping also found unmeasured channels on the `code_target` and lineage paths, and in
`premises[].rationale` / `invalidated_by`, so the real exposure is larger than 7/11.

## Design (option B, accepted by the user 2026-09-23)

- Evidence: `locator` = `document_ref` (`doc:v1:<hash>`), `citation_locator` =
  `<document_ref>#L<start>-<end>`, `publisher` a fixed literal. One helper builds every
  `EvidenceRecord` from a passage, so all five producers go through it.
- `authority_rationale`: a closed set of codes, enforced by the schema.
- Alternatives and premises become opaque refs (`alt:v1:<hash>`, `premise:v1:<hash>`) with
  closed fields only (disposition, status, premise refs, decision_ref, evidence_refs).
- Rationales, conflicts and claim text: fixed wording built from structure (verdict literal,
  counts, refs, validated shas), following `src/bruriah/agent_surface.py` from 1.6.0. Commit
  subjects and author names are dropped from the lineage and code-target outputs.
- New `read_evidence` kinds `alt:` and `premise:` return the stored name, reason and statement
  on explicit request, typed as `evidence_kind` "alternative" / "premise".
- Name matching stays internal, with a minimum-length threshold against over-broad names.
- The human CLI view keeps names by resolving refs locally. `--json` and MCP stay prose-free.

Rejected: filtering the text (a slug is printable and still an instruction); a separate
`untrusted_text` field (the prose would still enter context, so the promise stays false).

## Tasks

- [ ] **T0 — RED: widen the benchmark.** Add cases for the `code_target` path (commit author,
  subject, successor subject), the lineage path (file paths), and `premises[].rationale` /
  `invalidated_by`. Record the real baseline.
- [ ] **T1 — Version and packs.** 2.0.0; `max_router_version` → 2.x in the four packs in
  `src/bruriah/data/`, re-signed with `scripts/sign_pack.py`; the version-pinned tests.
- [ ] **T2 — Opaque evidence locators.** One `EvidenceRecord` builder, closed
  `authority_rationale`, lineage and code-target text built from structure, CLI human view
  resolving refs locally.
- [ ] **T3 — Counterfactual contract v2.** New alternative, premise and assessment shapes,
  fixed-wording rationale and conflicts, `schema_version` "2", the name-match threshold.
- [ ] **T4 — `alt:` / `premise:` reads.** Repository lookups by ref, the new `evidence_kind`
  values, `src/bruriah/demo.py` dereferencing through them.
- [ ] **T5 — Proof and docs.** The benchmark at ASR 0 with its report; `demo/injection/run.py`,
  the README section, docs, `evals/counterfactual/runner.py`, the CHANGELOG 2.0.0 entry,
  `evals/injection/README.md` with the before/after numbers.

## Acceptance criteria

- `uv run python evals/injection/run.py`: every case executed, ASR 0.
- The README claim at the investigation boundary is true as written and pinned by the benchmark.
- The full suite, ruff and mypy are clean on all three OSes when CI finally runs.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts`,
`uv run mypy src`, `uv run python evals/injection/run.py` (twice, byte-compared).

## Progress / evidence

- 2026-09-23: design mapped by one read-only agent (Engram topic
  `bruriah/2.0.0-boundary-design`); option B accepted by the user.

## Next step

T0 via one bounded writer.
