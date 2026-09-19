# Feature: Git Alias Integration (`bruriah alias install`)

## Objective
Provide turnkey integration with Git by configuring `git why` and `git drift` as native git aliases (`git config alias.why '!bruriah why'` and `alias.drift '!bruriah drift'`). Developers can inspect architectural lineage directly through their everyday `git` command invocation.

## Constraints & Invariants
- Scope support: supports `--global` (default, user-level git config) and `--local` (repository-level git config).
- Safe status reporting: reports whether each alias was `created`, `updated`, or `unchanged`.
- Clean uninstallation: `bruriah alias uninstall` removes only the managed Bruriah aliases without touching any other user git aliases.
- Zero untyped escapes: failure to run git or missing repository in local mode raises typed `AliasError` / `CliError`.
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Git Alias Manager (`src/bruriah/aliases.py`)
  - Implement `install_aliases(scope="global", repo=None) -> list[tuple[str, str]]`.
  - Implement `uninstall_aliases(scope="global", repo=None) -> list[tuple[str, str]]`.
  - Add unit tests in `tests/test_aliases.py`.
- [x] **task-2**: CLI Integration (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Add `alias` subcommand with `install` and `uninstall` actions.
  - Wire `_cmd_alias` in `cli.py` with friendly terminal output.
  - Add CLI integration tests in `tests/test_cli.py`.
- [x] **task-3**: Documentation & Verification
  - Update `README.md` with `git why` and `git drift` examples.
  - Run full test suite (`pytest tests`), `mypy src`, `ruff check src tests`.
