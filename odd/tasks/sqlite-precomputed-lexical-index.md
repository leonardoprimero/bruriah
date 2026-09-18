# Feature: SQLite Precomputed Lexical Index

## Objective
Precompute corpus statistics, document frequencies, and inverted postings in SQLite during `bruriah index` to replace the linear full-corpus BM25 tokenization scan in Python with sub-millisecond B-tree queries, while maintaining 100% mathematical ranking equivalence and seamless fallback for legacy snapshots.

## Constraints & Invariants
- Snapshots remain immutable (`mode=ro&immutable=1`).
- Fallback to linear scan is preserved for minimal/raw snapshots without precomputed tables.
- Ranking outcome (scores and order) must be byte-for-byte / float-for-float identical to the existing BM25 formula.
- Deadline budget enforcement (`max_elapsed_ms`) remains active during postings iteration.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Tokenizer in `language.py` & Schema Extension in `index.py`
  - Relocate canonical `tokenize` to `language.py` to prevent circular dependencies.
  - Add `corpus_stats`, `term_df`, and `term_postings` tables to `SCHEMA` in `index.py`.
  - Precompute and populate stats, DFs, and inverted postings during `build_candidate`.
  - Add unit tests in `tests/test_index.py`.
- [x] **task-2**: Accelerated Indexed BM25 in `retrieval.py`
  - Implement `_bm25_indexed_ranks` querying `corpus_stats`, `term_df`, and `term_postings`.
  - Wire into `search(...)` with fast path when index tables exist and fallback when absent.
  - Add unit tests in `tests/test_retrieval.py` verifying exact equivalence with unindexed BM25.
- [x] **task-3**: Full Suite Verification & Documentation
  - Run full suite: `ruff check src tests`, `mypy src`, `pytest tests`.
  - Update `CHANGELOG.md` and `README.md`.
