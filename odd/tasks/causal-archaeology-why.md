# Feature: Causal Archaeology (`bruriah why`)

## Objective
Implement `bruriah why <file>[:line]` — the first architectural causality analyzer for codebases. Where `git blame` tells you WHO changed a line and WHEN, `bruriah why` connects code lines to the architectural decision record that decided WHY it was written that way, enriched with causal decision lineage and supersession alerts from Bruriah's SQLite DAG.

## Constraints & Invariants
- Deterministic and local: relies solely on local Git history and the active SQLite index snapshot. No generative models and no network.
- Graceful degradation: handles untracked files, commits without explanatory decision records (tracing backwards to the governing decision), and Windows path syntax (`C:\...:line`).
- Lineage integrity: surfaces `lineage` DAG supersessions (`Supersedes:`, `Deprecates:`, `Amends:`) so developers and agents are immediately alerted if a line's governing decision was invalidated.
- Terminal & Structured JSON: supports human-readable terminal output and `--json` for machine/agent consumption.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [ ] **task-1**: Core Causal Resolution Engine (`src/bruriah/why.py`)
  - Implement `parse_target(target)` parsing `<path>[:<line>]` with cross-platform path safety.
  - Implement `resolve_blame` and backwards commit tracing via `git`.
  - Implement decision and lineage DAG lookup against SQLite snapshot.
  - Add unit tests in `tests/test_why.py`.
- [ ] **task-2**: CLI Integration & Formatter (`src/bruriah/_cli/parser.py`, `src/bruriah/_cli/why.py`, `src/bruriah/cli.py`)
  - Add `why` subcommand to `build_cli_parser` with `--repo`, `--json`, `--read`, platform arguments.
  - Implement terminal viewer displaying decision subject, reasoning (WHY), author, date, and DAG lineage alerts.
  - Add CLI end-to-end tests in `tests/test_cli.py`.
- [ ] **task-3**: Full Suite Verification & Documentation
  - Run full test suite (`pytest tests`), `ruff check src tests`, `mypy src`.
  - Document `bruriah why` in `CHANGELOG.md` and `README.md`.
