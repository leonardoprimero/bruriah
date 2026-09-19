# RFC: Architectural Drift Detection (`bruriah drift` / `bruriah check`)

**Status:** Implemented  
**Author:** Senior Architecture & Engineering  
**Target Version:** 0.10.0  
**Context:** Next frontier capability following Transitive Lineage & Causal Archaeology

---

## 1. Problem Statement & Motivation

In modern software engineering:
- **Linters** (`ruff`, `eslint`) verify syntax, formatting, and coding conventions.
- **Typecheckers** (`mypy`, `tsc`) verify static type contracts.
- **Test Suites** (`pytest`, `jest`) verify functional regressions against current code.

**However, no automated gatekeeper checks whether new code adheres to the organization's Architectural Decisions.**

When human engineers or autonomous AI coding agents (Claude Code, Cursor, Copilot) modify a repository:
1. **Stale Architecture Violations:** They frequently modify code originally authored under an older architectural decision (e.g. `Decision A`), unaware that `Decision A` was superseded by `Decision B` or `Decision C` (`Cloud-Native Architecture`). Consequently, they reintroduce obsolete conventions, dead abstractions, or forbidden dependencies.
2. **Silent Architectural Modifications:** They refactor core architectural modules without documenting *why* or establishing an audit trail (omitting `Amends:` or `Supersedes:` trailers).
3. **Review Blindness:** PR reviewers cannot easily see the historical decision lineage governing every changed line in a large pull request diff.

Bruriah already possesses:
- The precomputed SQLite index of all historical decisions and the files they touched.
- The verified acyclic decision lineage DAG (`lineage` table).
- Causal archaeology (`trace_causal_archaeology` and transitive multi-hop resolution).

By creating **`bruriah drift`**, we elevate Bruriah from a passive retrieval system into an **active gatekeeper of architectural integrity** in CI pipelines and pre-commit hooks.

---

## 2. Architectural Design

```
                     ┌───────────────────────────┐
                     │ Git Working Tree / Commit │
                     │   (Diff / Staged Files)   │
                     └─────────────┬─────────────┘
                                   │
                                   ▼
                     ┌───────────────────────────┐
                     │     Change Extraction     │
                     │  (touched files & lines)  │
                     └─────────────┬─────────────┘
                                   │
                                   ▼
                     ┌───────────────────────────┐
                     │  Causal Governance Lookup │
                     │  (SQLite Documents/Index) │
                     └─────────────┬─────────────┘
                                   │
                                   ▼
                     ┌───────────────────────────┐
                     │   Transitive Lineage DAG  │
                     │   Freshness & Conflict    │
                     └─────────────┬─────────────┘
                                   │
                                   ▼
                     ┌───────────────────────────┐
                     │ Drift Analysis & Report   │
                     │  - Stale Governance       │
                     │  - Active Conformance     │
                     │  - Missing Audit Trailers │
                     └─────────────┬─────────────┘
                                   │
                 ┌─────────────────┴─────────────────┐
                 ▼                                   ▼
       Terminal / Human Report               JSON / CI Exit Code
     (Actionable developer tips)         (--strict -> exit 1 on drift)
```

### Core Checks Performed by `bruriah drift`:
1. **Stale Governance Warning:**
   - Detects when a modified file is governed by an architectural decision that has been `superseded` or `deprecated`.
   - Resolves the transitive successor DAG to find the `[CURRENT ACTIVE]` leaf decision.
   - Emits a direct warning pointing to the modern decision and its commit hash.
2. **Missing Architectural Trailer:**
   - When a commit modifies files touched by a major architectural decision, flags whether the commit message declares an architectural trailer (`Supersedes: <sha>`, `Amends: <sha>`, `Deprecates: <sha>`).
3. **Clean Conformance State:**
   - If touched files are governed by `current` active decisions, reports that the change conforms cleanly.

---

## 3. CLI Command Specification

```bash
bruriah drift [OPTIONS] [REVISION_OR_RANGE]
```

### Arguments & Flags:
- `REVISION_OR_RANGE`: Optional git revision or range (e.g. `HEAD~1`, `origin/main...HEAD`, `HEAD`). Defaults to inspecting uncommitted working tree changes against `HEAD`.
- `--staged`: Inspects git staged changes (index). Ideal for `.pre-commit-config.yaml`.
- `--strict`: Fails with exit code `1` if any stale governance or architectural drift is detected. Fails with `0` if clean.
- `--json`: Emits structured JSON output suitable for PR bots, GitHub Actions comments, or IDE plugins.
- `--repo PATH`: Path to git repository root (default `.`).
- `--data-dir PATH`, `--config-dir PATH`: Standard Bruriah data and configuration directory overrides.

---

## 4. User Experience: Example Terminal Output

```text
$ bruriah drift --staged

🔍 Bruriah Architectural Drift Inspection
   Repository: /Users/dev/project (branch: feat/new-storage)
   Comparing: staged changes against HEAD (2 files inspected)

⚠️  STALE GOVERNANCE DETECTED (1 file):
  • src/core/storage.py
    Governing Decision: "Pure SQLite Storage Architecture"
    Original Ref: doc-storage-initial (sha: 111111112222)
    Lineage State: SUPERSEDED (evolved through 2 generations)
    Current Active Decision: "Cloud-Native Storage Engine" (sha: ccccccccdddd)
    Action: Ensure your changes adhere to "Cloud-Native Storage Engine" (doc-storage-v3).
            If this change updates the architecture, include trailer:
            Amends: ccccccccdddd

✅ CLEAN GOVERNANCE (1 file):
  • src/api/router.py (conforms to active decision doc-api-v2)

Summary: 1 stale governance warning, 0 missing trailers.
Run with --strict in CI to block architectural regressions.
```

---

## 5. Implementation Plan

### Step 1: Engine Module (`src/bruriah/drift.py`)
- Define data structures: `DriftWarning`, `FileGovernance`, `DriftReport`.
- Implement `get_git_diff_files(repo: Path, revision_range: str | None, staged: bool) -> tuple[str, ...]`.
- Implement `analyze_architectural_drift(repo: Path, database: sqlite3.Connection, files: tuple[str, ...]) -> DriftReport`.
- Implement formatters: `format_drift_human(report: DriftReport) -> str` and `format_drift_json(report: DriftReport) -> str`.

### Step 2: CLI Integration (`src/bruriah/_cli/parser.py` & `src/bruriah/cli.py`)
- Add `drift` subparser with `--staged`, `--strict`, `--json`, `--repo`.
- Implement `_cmd_drift(args, ...)` handler with clean connection teardown.

### Step 3: Test Suite (`tests/test_drift.py`)
- Test clean repo diff (zero warnings).
- Test staged changes touching stale/superseded files.
- Test multi-hop transitive successor detection in drift reports.
- Test `--strict` exit code 1 vs 0.
- Test `--json` structure.

### Step 4: CI Workflow Recipe & Documentation
- Document `bruriah drift` in `README.md`.
- Provide a ready-to-copy GitHub Actions job snippet:
  ```yaml
  name: Architecture Conformance
  on: [pull_request]
  jobs:
    drift-check:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
          with: { fetch-depth: 0 }
        - run: bruriah drift origin/main...HEAD --strict
  ```
