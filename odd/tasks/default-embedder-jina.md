# Feature: default-embedder-jina

**Branch:** `feat/default-embedder-jina` (from `main` at 044a660)
**Created:** 2026-09-20
**Route:** delegated direct (one bounded writer)
**TDD:** strict, enabled by session configuration. Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `single-pr`. Forecast ≈ 450 authored changed lines (data files excluded).
**RDD:** enabled. Last reviewed boundary: 044a660 (PR #25, lineage review-bce755d57ccd0380).

## Objective

Ship the embedding model the 2026-09-20 ablation showed to be better in both languages, publish
the ablation with its negative results, and fix the registry gap the ablation exposed.

## Problem and evidence

Paired ablation on leakcanary (884 docs) and egui (2,180 docs), 236 questions, RRF_K=60, no
reranker, raw data in `/Users/leguillo/bruriah-worktrees/ablation-data/RESULTS.md`:

| model | combined recall@3 | McNemar p vs default | egui index time |
|---|---|---|---|
| paraphrase-multilingual-MiniLM-L12-v2 (default) | 0.373 | — | 33 s |
| jinaai/jina-embeddings-v2-base-es | 0.436 | 0.0081 | 242 s |
| BAAI/bge-small-en-v1.5 (+prefix) | 0.441 (0.449) | 0.0037 (0.0009) | 48 s |
| BAAI/bge-base-en-v1.5 (+prefix) | 0.453 (0.475) | 0.0003 | 141 s |

Own history (204 docs at HEAD, 12 questions per language): Spanish recall@3 default 0.500,
jina 0.750, bge-small 0.333, bge-base 0.417 (prefixed 0.417). English 0.750 → 0.750–0.833.
The user chose jina-v2-base-es: the only candidate that improves both languages.

Negative results to publish: removing bot/release/duplicate documents from the ranking recovers
0 of 148 baseline misses (no corpus noise filter will be built); the README's egui figure of
1,878 documents is not reproducible (the pinned clone yields 2,180 with the generator at both
HEAD and the commit that published the number, `e7f2d81`).

Registry gap: `KNOWN_MODEL_PREFIXES` lists `BAAI/bge-*-en` but not the `-v1.5` names, so bge
v1.5 ran without its instruction prefix unless passed explicitly.

## Scope (authorized)

`src/bruriah/_cli/common.py`, `src/bruriah/_cli/parser.py`, `src/bruriah/setup.py` and any
other module that names the default model, `tests/**`, `README.md`, `docs/*.md`,
`evals/project-memory/README.md` and new data files under `evals/project-memory/`,
`CHANGELOG.md`, `pyproject.toml` and `src/bruriah/__init__.py` (version 1.4.0).

Out of scope: changing RRF_K, reranker, corpus generation, or any MCP surface; pushing; tagging.

## Tasks

- [x] T1 — Registry: one default constant and complete bge/jina entries
  - `DEFAULT_EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-base-es"` in `_cli/common.py`; every
    argparse `--model` default (parser.py lines ~58, ~587, ~664) and any other hardcoded
    occurrence reads it. Add `jinaai/jina-embeddings-v2-base-es` (and `-base-en`, `-small-en`)
    to `KNOWN_MODEL_PREFIXES` with `("", "")`; add `BAAI/bge-small-en-v1.5`, `bge-base-en-v1.5`,
    `bge-large-en-v1.5` with the bge instruction prefix.
  - TDD RED: tests asserting the three parsers share the constant, that
    `resolve_model_prefixes("BAAI/bge-small-en-v1.5")` returns the instruction prefix, and that
    `is_symmetric_model(DEFAULT_EMBEDDING_MODEL)` is False.
  - Route: delegated. Commit: pending.

- [x] T2 — Behaviour under the new default, measured
  - Verify (with a test) that querying an index built with model X uses X from the snapshot
    metadata (`embedding_model` in `platform.py`/`index.py`), so existing user indexes keep
    working after the default changes; add a test if none pins it.
  - Measure on the scratch clone `/private/tmp/claude-501/-Users-leguillo-bruriah/547b6230-fce8-4c9e-bb15-4662e2a34d77/scratchpad/ttw/repo`
    (pallets/itsdangerous, 677 commits): `bruriah init --repo .` wall clock with the jina model
    cached, and the on-disk size of the cached model. Record both in the feature document and
    use them in T3's README wording. Do not claim numbers that were not observed.
  - Route: delegated. Commit: pending (may fold into T1 if no code changes are needed beyond tests).

- [x] T3 — Publish the ablation and update every published number
  - Copy the `ranks-*.jsonl` and `misses-*.jsonl` files from ablation-data into
    `evals/project-memory/` using the file's naming convention (e.g.
    `leakcanary-embedder-ablation-jina.jsonl`); keep them small and newline-terminated.
  - New section in `evals/project-memory/README.md`, in that file's voice, titled
    "The embedder was the bottleneck, measured 2026-09-20": method (paired, same corpus, same
    questions, K=60, no reranker), the table above, McNemar counts, the prefix follow-up
    (prefix effect not significant: p=0.73 / 0.23), own-history EN/ES, the negative noise-filter
    result, the egui 1,878 vs 2,180 disclosure, the own-history corpus now being 204 documents at
    HEAD (recommend pinning with `--revision`), index times and sizes, versions and machine.
  - Top-level `README.md` section 4: headline numbers become the jina numbers with the exact
    values from RESULTS.md (combined recall@3 0.436, recall@10 and MRR@10 from the file; own
    history EN and ES). Keep the old default's numbers visible as the "before" so the change is
    auditable. Rename "Quickstart in 60 Seconds" to plain "Quickstart" and state the measured
    init time and model download size from T2. Update `_cli/common.py:71` comment wording.
  - `docs/cli-and-tools.md` / `docs/architecture.md`: any mention of the default model.
  - Route: delegated. Commit: pending.

- [x] T4 — Release 1.4.0
  - `CHANGELOG.md`: move Unreleased into `## [1.4.0] — 2026-09-20` with: default embedder change
    and why (numbers), registry fix, existing indexes unaffected, first-run download size and
    slower indexing stated plainly, the published ablation. `pyproject.toml` and
    `src/bruriah/__init__.py` to 1.4.0 (a test for version sync exists or must be added).
  - Route: delegated. Commit: pending.

## Acceptance criteria

- Full suite green; ruff and mypy clean; `tests/test_readme_claims.py` still passes (update the
  count if T1–T4 add tests).
- Every number in README.md section 4 is traceable to a file under `evals/project-memory/`.
- `bruriah --help`, `bruriah index --help` show the new default.
- Four Conventional Commits on the branch. Nothing pushed, nothing tagged.

## Progress / evidence

- T1 committed `afae47c`. `DEFAULT_EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-base-es"` in
  `_cli/common.py`, routed through parser.py (init/index/watch), cli.py `main()`, watch.py,
  bootstrap.py. Added `jinaai/jina-embeddings-v2-*` and `BAAI/bge-*-en-v1.5` to
  `KNOWN_MODEL_PREFIXES`. RED: `uv run pytest -q -p no:cacheprovider tests/test_cli.py -k
  "default_embedding_model or resolve_model_prefixes_bge_v1_5 or ..."` -> 4 failed, 1 passed.
  GREEN: same command -> 7 passed. Full `tests/test_cli.py`: 89 passed, 1 skipped. ruff/mypy clean.
- T2 committed `5456459`. No production code change needed: `build_serve_deps` (cli.py) already
  reads the query embedder's model from `load_build_descriptor(paths).embedding_model`, never
  from a CLI default. Added
  `test_build_serve_deps_queries_with_the_snapshots_own_model_not_the_current_default`. Verified
  it is a real regression detector by temporarily forcing `embedder_factory(DEFAULT_EMBEDDING_MODEL)`
  instead of `embedder_factory(descriptor.embedding_model)` in cli.py, confirming the test failed
  (RED), then reverting (GREEN, diff empty). Full `tests/test_cli.py`: 90 passed, 1 skipped.
  Measured on the scratch pallets/itsdangerous clone (677 commits), `~/Library/Caches/bruriah`
  already had the model cached from the earlier ablation work (614M,
  `models--jinaai--jina-embeddings-v2-base-es`), so neither run observed a cold download:
  `bruriah init` 18.22s then 18.38s wall clock (`/usr/bin/time -p`), `bruriah ask` 1.35s wall
  clock for one query.
- T3 committed `601ac91`. Copied 10 files (baseline/jina ranks for leakcanary/egui/own-history-
  en/own-history-es, plus baseline misses for leakcanary/egui) into `evals/project-memory/` as
  `<corpus>-embedder-ablation-<baseline|jina>[-misses].jsonl`; all newline-terminated, all links
  resolve. Added "The embedder was the bottleneck, measured 2026-09-20" to
  `evals/project-memory/README.md` (method, corpora, registry gap, result table, prefix
  follow-up, own-history table with the "do not read this as change the default" callback,
  204-document/no-`--revision` disclosure, 0/148 noise-filter negative result, 1,878-vs-2,180
  disclosure, index cost, Reproduce). Updated README.md section 4 (jina numbers with old-default
  "before", corrected 884/2,180 doc counts, own-history EN+ES) and renamed "Quickstart in 60
  Seconds" to "Quickstart" with the T2 numbers. Updated the `_cli/common.py:71` comment (was
  citing a stale 0.583/0.750 pair from an earlier, smaller own-history measurement; now cites the
  2026-09-20 204-document measurement and no longer calls MiniLM "the shipped default").
  `docs/cli-and-tools.md`/`docs/architecture.md`: no default-model mentions found (`rg -n
  'paraphrase-multilingual|MiniLM' docs/*.md` -> no match), nothing to change. `uv run pytest -q
  -p no:cacheprovider tests/test_cli.py tests/test_project_memory_eval.py` -> 92 passed, 1
  skipped.
- T4 committed `ecf3748`. `CHANGELOG.md`: `## [Unreleased]` -> `## [1.4.0] — 2026-09-20` with a
  new "Default embedding model is now `jina-embeddings-v2-base-es`" entry (numbers, existing
  indexes unaffected, download/indexing cost stated plainly, registry fix) ahead of the existing
  Unreleased entries. `pyproject.toml` and `src/bruriah/__init__.py` bumped to 1.4.0 (`uv.lock`
  follows). `test_the_version_the_package_reports_is_the_version_it_is_built_as` (pre-existing)
  passes. While running the mandated verification (`bruriah index --help | rg jina`), found it
  did NOT match: `--model`'s argparse default was never rendered in `--help` (no `help=` text on
  any of the three declarations). Fixed with a RED/GREEN test
  (`test_help_text_shows_the_default_embedding_model`, parametrized over init/index/watch) added
  to `--model`'s help text on all three; folded into this commit since it was needed to satisfy
  the acceptance criterion "`bruriah index --help` show the new default" and T1 was already
  committed (no non-HEAD amends). README.md test count bumped 1,374 -> 1,383 to track.

**Final verification** (all commands run from `/Users/leguillo/bruriah`, branch
`feat/default-embedder-jina`, HEAD `ecf3748`):
- `uv run pytest -q -p no:cacheprovider` -> **1365 passed, 0 failed, 18 skipped** (1383 collected,
  matches README.md).
- `uv run ruff check src tests evals scripts` -> `[]` (clean).
- `uv run mypy src` -> `Success: no issues found`.
- `uv run bruriah index --help | rg jina` -> matches (`--model MODEL  embedding model name
  (default: jinaai/jina-embeddings-v2-base-es)`, wrapped by argparse).
- `uv run python scripts/changelog_section.py 1.4.0 | head -5` -> prints the new entry's opening
  lines.
- Four Conventional Commits on the branch: `afae47c` (T1), `5456459` (T2), `601ac91` (T3),
  `ecf3748` (T4). Nothing pushed, nothing tagged.

## RDD

- The whole range 044a660..HEAD (22 files, 1089 lines) was refused with `lens_context_budget_exceeded`
  (no authority created). Split T3 into a data-only commit and a docs commit (same final tree), and
  reviewed four candidates at detached checkouts:
  - C1 afae47c+5456459 (146 lines, high): four lenses, approved, acknowledged (review-d310ffe81657db02).
  - C2 07dcf5e data (668 lines, medium, slice_budget_reached): one lens, approved, acknowledged
    (review-88d4413d2bd0c4c1).
  - C3 506206c docs (219 lines, medium, assessed under_budget; user granted anyway): reliability lens
    raised R3-001 (the report said the bge registry gap was "fixed in the same change" while the fix
    lives in afae47c). Corrected the sentence (4 lines), amended HEAD → 6b839f9, targeted validator
    approved, acknowledged (review-8285257a9b244d88).
  - C4 e9c34c5 release (68 lines, high): four lenses, approved, acknowledged (review-cc617b3f13ccc5d8).
- Final branch: afae47c, 5456459, 07dcf5e, 6b839f9, e9c34c5. Tree differs from the writer's ecf3748
  only by the README correction. Parent spot check on the writer's tree: 1365 passed, 0 failed,
  18 skipped; ruff and mypy clean.

## Next step

Delegate to one writer; parent verifies; RDD assess; PR; CI; merge; tag v1.4.0 (publishes PyPI
and the GitHub Release through the workflow).
