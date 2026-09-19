# Feature: Zero-Config Context Auto-Discovery

## Objective
Eliminate adoption and daily usage friction by introducing automatic project root and context discovery. Developers and agents inside any subdirectory of a repository should be able to run `bruriah why <target>`, `bruriah ask <question>`, `bruriah drift`, `bruriah doctor`, and `bruriah serve` without requiring `--data-dir`, `--config-dir`, or manual environment variables.

## Constraints & Invariants
- Precedence hierarchy:
  1. CLI arguments (`--data-dir`, `--config-dir`, etc.) - explicit operator overrides.
  2. Environment variables (`BRURIAH_DATA_DIR`, `BRURIAH_CONFIG_DIR`, etc.).
  3. In-repo project configuration (`.bruriah/config.json` or `.bruriah.json` discovered by walking up from CWD).
  4. Project-scoped user data store (`<user_data_dir>/projects/<project-id>/data` mapped to git repository root).
  5. Global user configuration (`<user_config_dir>/config.json`).
  6. Global platform defaults (`<user_data_dir>`).
- Collision immunity: different repositories with the same base folder name (e.g. `api`) must never collide. Project IDs are derived from repo name plus canonical path hash.
- Zero untyped escapes: all failures remain typed (`PlatformError`, `CliError`).
- 100% backwards compatibility: existing explicit-path workflows, scripts, and tests continue to work identically.
- Cross-platform support: POSIX and Windows path resolution without shell or filesystem quirks.
- Strict typing: `mypy src tests` passes with 0 errors; `ruff check src tests` clean.

## Tasks
- [x] **task-1**: Repository & Project Root Discovery (`src/bruriah/platform.py`)
  - Implement `find_project_root(start: Path | None = None) -> Path | None` walking up from `start` (defaulting to CWD) to locate `.bruriah` or `.git`.
  - Implement deterministic `project_id_for_repo(repo_root: Path) -> str` combining slugified directory name and canonical path hash.
  - Extend `resolve_paths` to discover in-repo `.bruriah/config.json` and project-scoped data directories when explicit CLI/env options are absent.
  - Add unit tests in `tests/test_platform.py` covering root discovery, precedence, and collision resistance.
- [x] **task-2**: CLI Auto-Discovery Integration & Scoped Init (`src/bruriah/_cli/common.py`, `src/bruriah/cli.py`, `src/bruriah/_cli/parser.py`)
  - Update `resolve_cli_paths` to pass repo/CWD context for discovery.
  - Update `bruriah init`: when `--repo` is specified (or inferred from CWD) without `--data-dir`, automatically initialize into the isolated project-scoped data directory and create/update `.bruriah/config.json` (or write local config).
  - Update `why`, `ask`, `drift`, `doctor`, `serve` to seamlessly resolve project context when inside a repository.
  - Add end-to-end CLI tests in `tests/test_cli.py`.
- [x] **task-3**: Documentation & Verification
  - Update `README.md` quickstart and examples to showcase zero-config usage (`bruriah why src/file.py:42`, `bruriah ask "why ..."` without `--data-dir`).
  - Run full test suite (`pytest tests`), `mypy src`, `ruff check src tests`.
