# Surface Unstructured Decisions

## Goal
Expose relevant historical commit references from ordinary repository history as a first-class investigation signal, so the host agent is prompted to inspect a likely decision before editing even when the repository has no structured decision trailers.

## Scope
- Preserve existing structured `conflicts`, `alternatives`, `premises`, and counterfactual semantics.
- Carry valid commit metadata from local retrieval into the investigation result.
- Add a top-level `decisions` field containing deduplicated validated commit references in retrieval order.
- Add deterministic tests for propagation, validation, deduplication, pagination/context forwarding, and schema compatibility.
- Do not add an LLM extractor or change the existing security boundary in this work unit.
- Defer broad omission of `unknown` metadata until a separate compatibility-focused change.

## Acceptance Criteria
1. A locally retrieved passage with a valid commit reaches `InvestigationResult.decisions`.
2. Invalid commit metadata is never emitted as a decision reference.
3. Duplicate commits are emitted once, preserving first-seen order.
4. Existing structured conflict/counterfactual behavior remains unchanged.
5. Existing callers can deserialize results when `decisions` is absent; new results expose the field.
6. Focused tests pass and the final work-unit commit is recorded here.

## Work Units
- [x] Propagate commit metadata through retrieval and service output.
- [x] Add contract/retrieval/service/context tests.
- [x] Verify focused suite and inspect diff.

## Verification Evidence
- TDD RED: 12 focused tests initially failed for the missing `decisions`, repository lookup, and retrieval propagation behavior.
- TDD GREEN: focused feature suite passed after implementation.
- `.venv/bin/python -m pytest -q tests/test_contracts.py tests/test_repository.py tests/test_retrieval.py tests/test_context.py tests/test_service.py`: 207 passed, 1 skipped (private corpus absent at `tests/test_retrieval.py:356`).
- `.venv/bin/python -m pytest -q tests/test_mcp_contract.py tests/test_injection_eval.py`: 58 passed.
- `.venv/bin/ruff check` on all changed source/test files: all checks passed.
- `.venv/bin/mypy` on changed source modules: no issues in 5 source files.
- Native Gentle review: approved and acknowledged, reliability lens; no blocking findings or correction route. Informational follow-up: add a dedicated decision-pagination assertion in a later candidate.

## Commit Evidence
- Pending explicit user authorization to create the required Conventional Commit; repository safety policy forbids committing without that authorization.
