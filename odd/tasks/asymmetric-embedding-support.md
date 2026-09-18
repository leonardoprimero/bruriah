# Feature: Asymmetric Embedding Support

## Objective
Support asymmetric embedding models (e.g., E5, BGE, Nomic) requiring distinct query and passage prefix templates (`query: ` vs `passage: `) across `BuildConfig`, candidate indexing, snapshot build descriptors, and MCP query serving.

## Constraints & Invariants
- Immutable snapshots (`mode=ro&immutable=1`) must remain unmodified.
- Existing indexes and default symmetric models (e.g. `MiniLM`) must maintain 100% backward compatibility with empty prefixes `("", "")`.
- Changing prefixes must invalidate snapshot compatibility (`_compatible(...) == False`) preventing unsafe vector reuse across differing embedding spaces.
- Retrieval core (`retrieval.py`) remains pure: query prefixing is handled in `build_serve_deps` closure.
- Zero untyped escapes, strict typing with Mypy, and Ruff compliance.

## Tasks
- [x] **task-1**: Contract & Domain Model (`index.py`, `platform.py`)
  - Extend `BuildConfig` with `query_prefix: str = ""` and `passage_prefix: str = ""`.
  - Include non-empty prefixes in `BuildConfig.embedding_identity`.
  - Persist prefixes in `platform._BUILD_DESCRIPTOR_FIELDS` and read them in `load_build_descriptor`.
  - Apply `config.passage_prefix` in `build_candidate` passage embedding.
  - Add unit tests in `tests/test_index.py` and `tests/test_platform.py`.
- [x] **task-2**: CLI Prefix Resolution & Serving Query Prefix (`_cli/parser.py`, `cli.py`)
  - Implement `resolve_model_prefixes` with known defaults (E5, BGE) and CLI override support.
  - Add `--query-prefix` and `--passage-prefix` to `index` and `init` in `_cli/parser.py`.
  - Wire resolved prefixes through `_cmd_index`, `run_index`, `_cmd_init`, `run_init_repo`.
  - Apply `descriptor.query_prefix` inside `build_serve_deps.embed_query`.
  - Add unit tests in `tests/test_cli.py`.
- [x] **task-3**: Full Verification & Documentation
  - Run full suite: `ruff check src tests`, `mypy src`, `pytest tests`.
  - Update `CHANGELOG.md` and `README.md` documenting asymmetric embedding support.
