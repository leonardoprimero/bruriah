# Feature: Architectural Decision Scribe (`bruriah decide`)

## Objective
Implement `bruriah decide` — an interactive and structured architectural decision engine for developers and AI agents. It captures architectural decisions at the moment of creation, formalizing the problem, alternatives evaluated, technical tradeoffs, and invariants, while automatically resolving and attaching validated lineage trailers (`Supersedes:`, `Amends:`) against the SQLite DAG to ensure project memory is always authored with Senior Architect quality.

## Constraints & Invariants
- **Deterministic and Local**: Operates 100% offline using the local Git history and SQLite index snapshot. No external LLM or telemetry.
- **Dual Mode (Interactive & Non-Interactive)**:
  - Interactive mode: prompts the developer step-by-step in the terminal (title, problem, alternatives, invariants, lineage relations).
  - Non-interactive / Agent mode: flags or `--json` inputs for AI agents and automated workflows (`--title`, `--problem`, `--invariants`, `--supersedes`, `--alternatives`).
- **Validated Lineage Trailers**: If a predecessor commit or decision is specified (`--supersedes` or `--amends`), `decide` validates against the SQLite snapshot that the target exists and warns if it is already superseded.
- **Git Commit & ADR Output**: Can output a formatted commit message, generate an ADR document, or directly commit staged changes via `--commit`.
- **Clean / Hexagonal Architecture**: Pure domain models in `src/bruriah/decide.py`, CLI integration in `src/bruriah/_cli/parser.py` and `src/bruriah/cli.py`.
- **Strict Quality Gate**: 100% type-checked with `mypy`, formatted/linted with `ruff`, and verified with comprehensive unit and integration tests.

## Tasks
- [x] **task-1**: Domain Model & Decision Scribe Engine (`src/bruriah/decide.py`)
  - Define `DecisionRecord`, `AlternativeOption`, `DecideRequest`.
  - Implement commit message and ADR markdown formatters with standardized trailers.
  - Implement lineage predecessor validation against SQLite snapshot.
  - Add comprehensive unit tests in `tests/test_decide.py`.
- [x] **task-2**: CLI Integration & Interactive Prompt (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Register `decide` subcommand with `--title`, `--problem`, `--invariants`, `--supersedes`, `--amends`, `--commit`, `--json`.
  - Implement interactive terminal prompt when run without required arguments.
  - Add CLI integration tests in `tests/test_decide.py`.
- [x] **task-3**: Documentation & Full Verification
  - Update `README.md` and `CHANGELOG.md`.
  - Run full test suite (`pytest tests`), `mypy src`, and `ruff check src tests`.
