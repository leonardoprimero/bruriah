# Feature: Transitive Lineage Resolution and CLI Ask Code-Target Grounding

## Objective
Elevate Bruriah's decision lineage and causal archaeology capabilities with two key architectural enhancements:
1. **Transitive Lineage Resolution:** Extend `why.py` and `service.py` so that decision lineage traversal is not limited to 1-hop successors. When an architectural decision has evolved through multiple generations (e.g. Decision A superseded by B, and B subsequently superseded by C), Bruriah traces the full transitive DAG chain to identify the active leaf successor and alerts the developer or agent with the complete evolutionary path.
2. **CLI Ask Code-Target Grounding:** Expose `--code-target` (and `-c`) and `--repo` in the `bruriah ask` CLI command, allowing terminal users to ground questions in specific files and lines just as autonomous agents do over MCP, with full rendering of primary governing decisions, freshness, and DAG conflict disclosures.

## Constraints & Invariants
- **DAG Acyclicity & Bound Safety:** The index DAG is verified acyclic at build time (`_detect_lineage_cycles`), but traversal maintains an explicit visited set and bounded recursion depth to guarantee termination.
- **Contract Backward Compatibility:** Existing `CausalResolution` and `LineageAlert` fields remain unchanged. New properties (`depth`, `chain`, `active_successor_ref`, `active_successor_subject`) default cleanly without breaking callers or JSON schemas.
- **Two-Tool MCP Invariant:** MCP public contract remains strictly `investigate_work` and `read_evidence`.
- **Honest Disclosure:** Output explicitly distinguishes intermediate superseded decisions from current active leaf decisions.

## Tasks
- [x] **task-1**: Transitive Lineage DAG Engine (`src/bruriah/why.py`, `src/bruriah/service.py`)
  - Update `LineageAlert` with `depth`, `chain`, and active successor fields.
  - Implement transitive DAG traversal in `check_lineage_alerts` in `why.py`.
  - Enhance terminal rendering in `format_resolution_text` to show multi-hop successor paths and highlight the current active decision.
  - Ensure `_apply_lineage` in `service.py` handles transitive chains cleanly.
- [x] **task-2**: CLI `bruriah ask` Grounding (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Add `--code-target` / `-c` and `--repo` arguments to `ask` subparser.
  - Pass `repo` to `build_serve_deps` and `code_target` to `InvestigationRequest`.
  - Render `freshness`, `conflict`, `conflicts`, and `claims` in `_cmd_ask` terminal output.
- [x] **task-3**: Unit, CLI, and Service Tests (`tests/test_why.py`, `tests/test_cli.py`, `tests/test_service.py`, `tests/test_ask.py`)
  - Test multi-hop transitive DAG traversal (A -> B -> C) in `test_why.py`.
  - Test `bruriah ask --code-target` in `test_ask.py` with both human-readable and `--json` output.
  - Test transitive lineage behavior in `test_service.py`.
- [x] **task-4**: Documentation & Full Verification
  - Update `README.md` and `CHANGELOG.md`.
  - Run full test suite, `ruff check src tests evals`, and `mypy src`.
