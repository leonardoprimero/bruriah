# Feature: Architectural Healing & Pedagogical Remediation (`bruriah heal`)

## Objective
Implement `bruriah heal [target] [--agent] [--json]` — an architectural auto-remediation and pedagogical blueprint engine. Where `guard` and `drift` detect and block architectural violations, `heal` guides developers and AI agents through the exact refactoring steps required to achieve compliance, synthesizing canonical code patterns from historical decisions so agents learn the project's standards instead of hallucinating dirty patches.

## Constraints & Invariants
- **Deterministic and Local**: Operates 100% offline using the local Git history and SQLite index snapshot. No external LLM or telemetry.
- **Pedagogical Agent Injection (`--agent`)**: Produces structured, step-by-step refactoring blueprints tailored for coding agents (Claude, Cursor, Antigravity) containing the diagnostic, canonical pattern, and refactoring recipe.
- **Human-Friendly Terminal View**: Formats clear, educational terminal cards for developers showing what broke, why it was decided, and how to fix it cleanly.
- **Clean / Hexagonal Architecture**: Core domain models and synthesis in `src/bruriah/heal.py`, CLI integration in `src/bruriah/_cli/parser.py` and `src/bruriah/cli.py`.
- **Strict Quality Gate**: 100% type-checked with `mypy`, formatted/linted with `ruff`, and verified with comprehensive unit and integration tests.

## Tasks
- [x] **task-1**: Domain Model & Healing Engine (`src/bruriah/heal.py`)
  - Define `RemediationStep`, `RemediationBlueprint`, `HealingResult`.
  - Implement `generate_remediation_blueprint` linking violations to canonical decision patterns and actionable steps.
  - Implement `format_heal_human`, `format_heal_agent`, and `format_heal_json`.
  - Add comprehensive unit tests in `tests/test_heal.py`.
- [x] **task-2**: CLI Integration (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Register `heal` subcommand with `target`, `--agent`, `--json`, and repository options.
  - Implement `_cmd_heal` handler.
  - Add CLI integration tests in `tests/test_heal.py`.
- [x] **task-3**: Documentation & Full Verification
  - Update `README.md` and `CHANGELOG.md`.
  - Run full test suite (`pytest tests`), `mypy src`, and `ruff check src tests`.
