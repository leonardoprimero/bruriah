# Feature: MCP Causal Archaeology (`code_target` in `investigate_work`)

## Objective
Enable autonomous coding agents to perform causal archaeology through the Model Context Protocol without breaking the Two-Tool Public Contract (`Portable Two-Tool Public Contract`, `spec.md:253`). By adding an optional `code_target` field to `InvestigationRequest`, agents can investigate specific files and line numbers (e.g. `src/bruriah/mcp_server.py:42`) to retrieve the governing architectural decision, its exact Git commit provenance, and any DAG lineage supersessions/conflicts as primary evidence, and subsequently read the exact reasoning via `read_evidence`.

## Constraints & Invariants
- **Two-Tool Contract Preservation:** The MCP server public surface remains strictly two tools: `investigate_work` and `read_evidence`. No third tool is added.
- **Instruction and Evidence Separation:** Causal resolution emits structured `EvidenceRecord`, `ClaimRecord`, and `conflicts` envelopes without dumping raw unvetted markdown into the agent's prompt, preventing prompt injection attacks.
- **Lineage DAG Integration:** When a code target's governing decision is superseded, deprecated, or amended in the SQLite lineage DAG, the evidence is marked `freshness="stale"`, `conflict="declared"`, alerts are populated into `conflicts`, and a `ClaimRecord(state="conflicted")` is emitted.
- **Graceful Fallback:** If `code_target` points to an untracked file, uncommitted changes, or non-git environment, it records a typed degradation/warning (e.g. `code_target_unavailable:file_not_in_git`) without crashing the investigation.
- **Strict Typing & Backward Compatibility:** Existing requests without `code_target` remain byte-identical and 100% backward compatible. Strict validation with `extra="forbid"` on `InvestigationRequest`.

## Tasks
- [x] **task-1**: Extend `InvestigationRequest` and `ServiceDeps` (`src/bruriah/contracts.py`, `src/bruriah/service.py`)
  - Add `code_target: ShortText | None = None` to `InvestigationRequest`.
  - Add `repo: Path = Path(".")` to `ServiceDeps` with backwards-compatible default.
  - Wire `repo` through `build_serve_deps` and platform loader.
- [x] **task-2**: Implement Causal Resolution in `service.investigate` (`src/bruriah/service.py`)
  - Integrate `trace_causal_archaeology` from `why.py` when `request.code_target` is present.
  - Query SQLite passages for the governing decision document ref and materialize primary `EvidenceRecord`.
  - Populate lineage conflicts, claims, and uncertainty if the governing decision has DAG alerts.
  - Prepend causal evidence at rank #1 in the evidence catalog.
  - Gracefully catch `WhyError` and record degradation/warnings.
- [x] **task-3**: Unit, Contract, and MCP Integration Tests (`tests/test_service.py`, `tests/test_mcp_contract.py`)
  - Test `investigate` with valid `code_target` returning primary governing decision evidence.
  - Test `investigate` with superseded code target returning `stale` freshness and DAG conflicts.
  - Test `investigate` with invalid/untracked `code_target` degrading gracefully without failure.
  - Test MCP server `investigate_work` JSON-RPC tool invocation with `code_target` and subsequent `read_evidence`.
- [x] **task-4**: Full Suite Verification, Ruff, Mypy, and Documentation
  - Run full test suite (`pytest tests`), `ruff check src tests`, `mypy src`.
  - Update `README.md` and `CHANGELOG.md` documenting MCP causal archaeology.
