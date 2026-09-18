# Feature: Deterministic Cursor-Based Pagination for Investigation

## Objective
Enable real, deterministic cursor-based pagination for `investigate_work` in `bruriah.service` and `bruriah.retrieval`, allowing agents to paginate through large evidence catalogs with snapshot isolation, eliminating the `cursor_not_supported` limitation.

## Constraints & Invariants
- Cursors are opaque, URL-safe base64 tokens cryptographically tied to `request_id` (content hash) and snapshot `build_id`.
- Forgery, cross-query reuse, or cross-snapshot cursor reuse must fail typed with `ServiceError("invalid_cursor")`.
- Slicing across pages must never drop evidence, duplicate records, or reorder rankings.
- Initial request with `cursor=None` maintains 100% backward-compatible output and behavior.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Extend `retrieval.search` with Offset Pagination
  - Add `offset: int = 0` to `search(...)`.
  - Slice `ordered` starting at `offset` while preserving global rank numbering and truncation tracking.
  - Add unit tests in `tests/test_retrieval.py`.
- [x] **task-2**: Implement Investigation Cursor Encoding & Paging in `service.py`
  - Implement `_encode_investigate_cursor` and decode validation matching `request_id` and `snapshot.build_id`.
  - Wire pagination logic into `investigate()`, producing `next_cursor` when `max_evidence` or candidates truncate.
  - Update `InvestigationRequest.cursor` description in `contracts.py`.
  - Add unit tests in `tests/test_service.py` for full pagination lifecycle, tampering, and snapshot binding.
- [x] **task-3**: Full Suite Verification & Documentation
  - Run full suite: `ruff check src tests`, `mypy src`, `pytest tests`.
  - Update `CHANGELOG.md` and `README.md`.
