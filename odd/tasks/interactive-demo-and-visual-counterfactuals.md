# Feature: Interactive Demo Command & Visual Counterfactual DAG Explorer

## Objective
Provide an interactive terminal demonstration (`bruriah demo`) and upgrade the Visual DAG Explorer (`bruriah ui`) to display premise health, evaluated alternatives, and premise drift alerts directly in the D3 graph.

## Problem & Context
Users encountering Bruriah on Hacker News or technical conferences need an immediate, zero-setup way to observe counterfactual memory in action. Furthermore, technical leaders inspecting projects with `bruriah ui` currently see only lineage links without visual insight into which decisions suffer from premise drift or what alternatives were rejected.

## Scope & Constraints
- Keep 2-tool MCP server untouched and pristine.
- No new external dependencies (use standard library + existing D3.js in `ui.py`).
- Maintain 100% test coverage and pass all CI checks.
- Conventional commits only.

## Actionable Tasks
- [x] Task 1: Upgrade `src/bruriah/ui.py` to query `premises` and `alternatives` tables, mark premise drift on `DecisionNode`, and render interactive panels and drift glow in D3.
- [x] Task 2: Implement `src/bruriah/demo.py` and register `bruriah demo` in `src/bruriah/cli.py` to provide a 3-step interactive terminal walkthrough of counterfactual memory.
- [x] Task 3: Add automated tests in `tests/test_ui.py` and `tests/test_demo.py` to verify both features across platforms.

## Applicable Checks
- `uv run pytest tests/test_ui.py tests/test_demo.py`
- `uv run pytest tests/test_packaging.py tests/test_counterfactual.py`
- `bruriah demo --non-interactive` exits 0 with expected output.
