# Feature: Architectural Pre-flight & Supersede Protocol (`bruriah brief`)

## Objective
Implement `bruriah brief <task_or_intent> [--targets <paths...>]` — a proactive architectural briefing engine for developers and AI agents. Before writing any code, `brief` synthesizes active architectural invariants, governing decisions, and blast-radius warnings for a task, and introduces the formal **Supersede Protocol** allowing agents and developers to propose deprecating or superseding past decisions when historical premises have changed.

## Constraints & Invariants
- **Deterministic and Local**: Operates 100% offline using the local SQLite index snapshot, Git corpus, and retrieval engine. Zero external LLM calls or telemetry.
- **Unified Dual Consumer (Dev & Agent)**: Produces human-friendly, high-clarity terminal output by default, and structured `--json` / MCP tool response for agent context injection.
- **Explicit Supersede Protocol**: Mandates that agents never silently violate historical invariants; if premises changed, the agent must output a structured supersede proposal (`target_sha`, `changed_premise`, `new_invariant`).
- **Clean / Hexagonal Architecture**: Core logic encapsulated in `src/bruriah/brief.py`, application orchestration in `src/bruriah/service.py`, and CLI presentation in `src/bruriah/_cli/brief.py`.
- **Strict Quality Gate**: 100% type-checked with `mypy`, formatted/linted with `ruff`, and verified with comprehensive unit and integration tests.

## Tasks
- [x] **task-1**: Domain Model & Core Briefing Engine (`src/bruriah/brief.py`)
  - Define dataclasses: `GoverningConstraint`, `SupersedeTemplate`, `ArchitecturalBrief`.
  - Implement `generate_brief(intent, targets, paths)` combining semantic search and blast-radius analysis.
  - Implement the formal `SupersedeProtocol` instructions and template generator.
  - Add comprehensive unit tests in `tests/test_brief.py`.
- [x] **task-2**: CLI Integration & Terminal Formatter (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Register `brief` subcommand in CLI parser with positional `intent`, `--targets`, `--json`, `--agent`, and repository path options.
  - Implement rich terminal formatter with ANSI colors, invariant cards, and supersede instructions.
  - Add CLI integration tests in `tests/test_brief.py`.
- [x] **task-3**: Agent Context Injection & Structured Output
  - Provide `--agent` and `--json` modes for direct agent prompt injection and automated pipelines.
  - Preserve the fundamental Slice 7B invariant of exactly two MCP tools (`investigate_work`, `read_evidence`).
- [x] **task-4**: Documentation & Full Verification
  - Update `README.md` and `CHANGELOG.md`.
  - Run full test suite (`pytest tests`), `mypy src`, and `ruff check src tests` (1,307 passed, 0 failures).
