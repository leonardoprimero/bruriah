# Feature: Gatekeeper Hooks (pre-commit, GitHub Action & native hook CLI)

## Objective
Provide turnkey integration points for CI and local development workflows to enforce architectural lineage checking (`bruriah drift`) automatically. Developers can add Bruriah to `.pre-commit-config.yaml`, run `bruriah hook install` for zero-dependency local git hook management, or use the official `action.yml` GitHub Action in CI pipelines.

## Constraints & Invariants
- Zero untyped escapes: hook installation failures (e.g. not a git repo, unwritable hook directory) raise typed `HookError` / `CliError`.
- Safe hook merging: `bruriah hook install` must not clobber existing custom pre-commit hooks. If an existing hook is present, it appends the execution block with clear markers, or replaces safely.
- Executable permissions: hook script must be set to `0o755` on POSIX systems.
- 100% backwards compatibility: existing CLI commands, tests, and options remain unaffected.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Repository Ecosystem Files (`.pre-commit-hooks.yaml`, `action.yml`)
  - Create `.pre-commit-hooks.yaml` exporting `bruriah-drift` hook.
  - Create root `action.yml` composite action for GitHub Actions CI.
- [x] **task-2**: Native Hook Manager (`src/bruriah/hooks.py`)
  - Implement `install_hook(repo_root: Path, *, force: bool = False) -> tuple[Path, str]` (status: "created" | "updated" | "unchanged").
  - Implement `uninstall_hook(repo_root: Path) -> tuple[Path, str]` (status: "removed" | "not_found").
  - Add unit tests in `tests/test_hooks.py`.
- [x] **task-3**: CLI Integration (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Add `hook` subcommand with `install` and `uninstall` actions.
  - Wire `_cmd_hook` in `cli.py` with friendly terminal output.
  - Add CLI integration tests in `tests/test_cli.py`.
- [x] **task-4**: Documentation & Verification
  - Update `README.md` with pre-commit, GitHub Action, and `bruriah hook install` examples.
  - Run full test suite (`pytest tests`), `mypy src`, `ruff check src tests`.
