# Feature: Lazy Passage Hydration for Retrieval

## Objective
Decouple the scoring phase from candidate materialization in `retrieval.search`. Instead of full-table scanning and parsing all columns (`text`, `search_text`, `heading_path` JSON) into heavy Python `_Passage` objects across the entire corpus on every query, perform fast projection of vector candidates during scoring and lazily hydrate full passage metadata only for the top fused candidates that survive ranking and budget limits.

## Constraints & Invariants
- 100% backward compatibility and exact mathematical equivalence: returned matches, scores, ranks, and degradation notes must remain byte/value-identical.
- Snapshots remain immutable (`mode=ro&immutable=1`).
- Fallback path for unindexed legacy snapshots (where `_has_lexical_index` is False) continues to use full scan for Python BM25.
- `candidates_scanned` and deadline budgets (`max_elapsed_ms`) continue to be accurately tracked and enforced during the scan.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Vector Catalog Scan & Precomputed Corpus Language
  - Implement lightweight `_scan_vectors(database, deadline, clock)` projecting only `(ref, vector)`.
  - Read `corpus_language` directly from `corpus_stats` when indexed, bypassing passage sampling.
  - Update `_vector_ranks` to operate over `(ref, vector)` sequences.
- [x] **task-2**: Lazy Passage Hydration for Fused Candidates
  - Implement `_hydrate_passages(database, refs)` to fetch full passage metadata (`text`, `heading_path`, line numbers) only for candidate `ref`s that need reranking or snippet extraction.
  - Wire lazy hydration into `search()`, hydrating at most `max_candidates` (plus rerank depth when reranker is active).
- [x] **task-3**: Full Suite Verification & Performance Regression Check
  - Verify full test suite passes with 0 failures (`pytest tests`).
  - Run linting and static typing (`ruff check src tests`, `mypy src`).
  - Update `CHANGELOG.md` and `README.md`.
