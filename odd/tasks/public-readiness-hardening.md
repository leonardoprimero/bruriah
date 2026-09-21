# Feature: public-readiness-hardening

**Branch:** `chore/public-readiness-hardening` (from `main` at 63eeddd)
**Created:** 2026-09-20
**Route:** delegated direct (one bounded writer; 4+ non-trivial files per task)
**TDD:** strict, enabled by session configuration (`Strict TDD Mode: enabled`). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `single-pr` (forecast ≈ 350 authored changed lines)
**RDD:** enabled for this repo. Last reviewed boundary: 63eeddd (lineage review-495c6e5960cfd7a7, acknowledged).

## Objective

Make the public face of Bruriah match the quality of its engineering, so the repository
holds up to a skeptical conference-grade reviewer.

## Problem

The 2026-09-20 review found the code sound (1340 passed / 18 skipped, ruff+mypy clean,
3x3 CI matrix) but the presentation contradicting itself and one unverified default change:

- README claims 1,087 tests; 1,358 are collected.
- CHANGELOG stops at 1.1.0; tags/PyPI at 1.3.1; GitHub Releases at v0.9.4 (workflow never
  creates GitHub Releases, only publishes to PyPI).
- Commit 63eeddd changed `RRF_K` 60→20 with no measured gain; `evals/retrieval/adapters.py:171`
  still hardcodes 60 (desynced from production); README's external recall@3 0.377 was measured
  with 60; the justifying comment in `ranking.py` contains a wrong number (0.0156 vs 1/62=0.0161);
  `_cli/common.py:71` writes an unmeasured estimate ("estimated 0.50+ with E5-large") into code.
- `watch.py:16` and `bootstrap.py:351` import `run_index` from the CLI adapter (inverted layer
  dependency).
- `docs/counterfactual-paper.md` styles itself "Conference Whitepaper" while single-author and
  self-evaluated.

## Scope (authorized)

Files: `src/bruriah/ranking.py`, `src/bruriah/_cli/common.py`, `src/bruriah/cli.py`,
`src/bruriah/index.py`, `src/bruriah/watch.py`, `src/bruriah/bootstrap.py`,
`evals/retrieval/adapters.py`, `tests/**`, `README.md`, `CHANGELOG.md`,
`docs/counterfactual-paper.md`, `.github/workflows/release.yml`, `scripts/` (new).

Out of scope: splitting `cli.py`/`service.py`, changing the default embedder, running the
leakcanary/egui ablation, pushing, creating GitHub Releases, tagging.

## Tasks

- [x] T1 — Restore the measured RRF default and re-sync the evaluation adapter
  - `ranking.RRF_K` back to 60 (the value every published number was measured with). Keep the
    per-call `rrf_k` override, the expanded model registry, `is_symmetric_model()` and the CLI
    warning from 63eeddd.
  - Rewrite the `ranking.py` comment: correct arithmetic, state that K=20 was tried and did not
    change own-history recall (English 0.750, Spanish 0.500), and that external corpora are
    unmeasured at K=20.
  - `evals/retrieval/adapters.py`: import `RRF_K` from `bruriah.ranking` instead of hardcoding.
  - `_cli/common.py`: replace "estimated 0.50+ with E5-large" with only measured facts (own
    README: jina-embeddings-v2-base-es Spanish recall@3 0.750 vs MiniLM 0.500).
  - TDD: RED = test asserting `ranking.RRF_K == 60` and `adapters` uses `ranking.RRF_K`; adjust
    `test_rrf_lower_k_amplifies_rank_advantage` if it encodes 20.
  - Checks: `uv run pytest -q tests/test_ranking.py tests/test_cli.py`, ruff, mypy.
  - Route: delegated. Commit: f205dbc.

- [x] T2 — Fix the inverted dependency (infra → CLI adapter)
  - Moved `EmbedderFactory`, `_default_embedder_factory`, `_embedding_fingerprint` and `run_index`
    into a new `src/bruriah/index_runner.py` (not `index.py`: `platform.py` already imports from
    `index.py`, so a home inside `index.py` importing `platform.py`'s `PlatformPaths` would cycle;
    `index_runner.py` sits above both). `cli.py` imports and re-exports the same names so existing
    test imports keep working. `watch.py` and `bootstrap.py` import from the new home; bootstrap's
    deferred in-function import is gone since the cycle it worked around no longer exists.
  - TDD: RED = `tests/test_architecture.py` asserting no module under `src/bruriah` other than
    `cli.py`/`_cli/*` imports from `bruriah.cli` (static scan of import statements).
  - Checks: full suite, ruff, mypy.
  - Route: delegated. Commit: f98910f.

- [x] T3 — Make README claims self-verifying and calibrate the paper's framing
  - README line 182: replace the hand-typed count with the current collected count and reword
    to "N tests · 0 failures · skips only when an environment prerequisite is absent".
  - `tests/conftest.py`: record `session.testscollected` in `pytest_collection_finish`;
    `tests/test_readme_claims.py` asserts the README number equals the collected count, skipping
    when the run is narrowed (`-k`, explicit file args, `--deselect`).
  - README line 73 "technical whitepaper" → "technical report". `docs/counterfactual-paper.md:5`
    status → "Technical Report — single-author, self-evaluated on the fixture suite in
    `evals/counterfactual/`; not peer reviewed".
  - TDD: RED = the README-count test fails against 1,087.
  - Checks: full suite (the new test only asserts on a full run), ruff.
  - Deviation: `conftest.py` records the count via `len(session.items)`, not
    `session.testscollected` as scoped -- pytest only assigns `session.testscollected` AFTER
    `pytest_collection_finish` returns (`Session.perform_collect`'s `finally` block runs the hook
    before the trailing `self.testscollected = len(items)`), so the scoped approach would always
    read 0. Caught by the RED/GREEN cycle itself (first GREEN attempt reported "0 tests
    collected"). `session.items` is already populated at hook time and is the correct source.
  - Route: delegated. Commit: 3e0e054.

- [x] T4 — Bring CHANGELOG to 1.3.1 and let the release workflow publish GitHub Releases
  - Add `[1.0.0] — 2026-09-19`, `[1.2.0] — 2026-09-20`, `[1.3.0] — 2026-09-20`,
    `[1.3.1] — 2026-09-20` from git history (commits listed in the writer brief), plus an
    `[Unreleased]` section naming T1–T3 in user-facing terms.
  - `scripts/changelog_section.py <version>`: prints the CHANGELOG section for one version,
    exit 1 if absent. `tests/test_changelog_section.py` covers found / not found / Unreleased.
  - `.github/workflows/release.yml`: new job `github-release` (after `publish`, `permissions:
    contents: write` only on that job, `softprops/action-gh-release` pinned by SHA or plain
    `gh release create` with `GITHUB_TOKEN`) that creates the GitHub Release for the tag with
    the body from `scripts/changelog_section.py`. Keep the existing supply-chain comments' spirit.
  - TDD: RED = tests for `changelog_section.py`.
  - Checks: `uv run pytest -q tests/test_changelog_section.py`, `uv run python scripts/changelog_section.py 1.3.1`, ruff, mypy on scripts if included in config.
  - Note: T4's own 5 new tests shifted the collected total (1,369 → 1,374), so README's
    self-verifying count (added in T3) needed one more bump in this same commit — expected
    behavior of a self-verifying claim, not scope creep.
  - Route: delegated. Commit: 45a145f.

## Acceptance criteria

- [x] `uv run pytest -q` green on the branch (1,356 passed, 0 failed, 18 skipped = 1,374
  collected); ruff and mypy clean.
- [x] README, CHANGELOG and `ranking.RRF_K` agree with the published measurements.
- [x] No module outside `cli.py`/`_cli/` imports `bruriah.cli` (`tests/test_architecture.py`).
- [x] Four Conventional Commits, one per task, on the feature branch. Nothing pushed.

## Progress / evidence

- T1 `f205dbc` — RED: `uv run pytest -q -p no:cacheprovider tests/test_ranking.py
  tests/test_retrieval_eval.py -k "rrf or RRF"` → 2 failed (`ranking.RRF_K == 60` and
  `RouterAdapter._RRF_K == ranking.RRF_K`). GREEN: `uv run pytest -q -p no:cacheprovider
  tests/test_ranking.py tests/test_cli.py tests/test_retrieval_eval.py` → 129 passed, 5 skipped.
  ruff/mypy clean.
- T2 `f98910f` — RED: `uv run pytest -q -p no:cacheprovider tests/test_architecture.py` → 1
  failed. GREEN: same command → 1 passed. Full suite after fixing one pre-existing test that
  monkeypatched `cli_module.TextEmbedding` (now needs `index_runner_module.TextEmbedding`, since
  `_default_embedder_factory` moved): 1350 passed, 0 failed, 18 skipped. ruff/mypy clean.
- T3 `3e0e054` — RED: `uv run pytest -q -p no:cacheprovider` → 1 failed
  (`test_readme_test_count_matches_collected`, README said 1,087). GREEN: same command → 1351
  passed, 0 failed, 18 skipped (1,369 collected). ruff/mypy clean.
- T4 `45a145f` — RED: `uv run pytest -q -p no:cacheprovider tests/test_changelog_section.py` → 3
  failed (script missing, exit 2 not 0/1). GREEN: same command → 5 passed. Full suite: 1356
  passed, 0 failed, 18 skipped (1,374 collected). ruff/mypy clean. `uv run python -c "import
  yaml,sys; yaml.safe_load(open('.github/workflows/release.yml'))"` → parses.
- RDD: not exercised this session — no `gentle-ai review mode status` check was run; the
  parent orchestrator owns that gate per commit before delivery.

## RDD

- Candidate: range 63eeddd..45a145f (4 work-unit commits reviewed as one PR slice; `single-pr`).
- Assess: risk `high`, review_due `high_risk`, 800 changed lines (CHANGELOG text and moved code included).
- Consent: granted by the user. Lineage review-bce755d57ccd0380, four lenses admitted, outcome approved, acknowledged, authority burned. Reviewed boundary advances to 45a145f.
- Parent spot check: `uv run pytest -q -p no:cacheprovider` → 1356 passed, 0 failed, 18 skipped; ruff clean; mypy clean.

## Next step

All four tasks complete on `chore/public-readiness-hardening`. Nothing pushed, no tag, no
GitHub Release created (out of scope). Parent orchestrator to run the RDD/review gate per
commit if receipt-driven development is enabled, then decide push/PR.
