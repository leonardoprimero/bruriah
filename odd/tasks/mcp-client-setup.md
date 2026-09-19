# Feature: MCP Client Setup (`bruriah setup`)

## Objective
Eliminate JSON copy-paste friction by providing an automated, non-destructive MCP client installer (`bruriah setup [client]`). Seamlessly injects `bruriah` into Cursor, Claude Code, Claude Desktop, Gemini CLI, and OpenCode configs, preserving all existing MCP servers and formatting.

## Constraints & Invariants
- Non-destructive merging: never overwrite or clobber existing MCP servers in user configs. If `mcpServers` already contains other tools, add/update `bruriah` in-place.
- Atomic & safe writes: use atomic write replacement and create `.bak` backups before modifying existing user configuration files.
- Project vs Global scope: support `--project` (default when inside a repository) and `--global` (user-level config).
- Zero untyped escapes: invalid JSON in existing user configs or unsupported clients must raise typed `SetupError` / `CliError`.
- Deterministic dry-run: support `--dry-run` to display exactly what file and JSON diff would be written without touching disk.
- Cross-platform paths: correctly resolve config locations on macOS, Linux, and Windows (e.g. `~/Library/Application Support/Claude` vs `%APPDATA%\Claude`).
- Strict typing with Mypy, Ruff compliance, and 0 warnings.

## Tasks
- [x] **task-1**: Client Config Target Resolution & Merger (`src/bruriah/setup.py`)
  - Define client target paths (project vs global) for `cursor`, `claude`, `claude-desktop`, `gemini`, `opencode`.
  - Implement non-destructive JSON merging logic for `mcpServers` structure.
  - Implement atomic write with backup creation and `--dry-run` support.
  - Add unit tests in `tests/test_setup.py`.
- [x] **task-2**: CLI Command Integration (`src/bruriah/_cli/parser.py`, `src/bruriah/cli.py`)
  - Add `setup` subcommand to parser with client argument, `--global`, `--project`, `--dry-run`.
  - Wire `_cmd_setup` in `cli.py` with friendly progress reporting.
  - Add CLI integration tests in `tests/test_cli.py`.
- [x] **task-3**: Documentation & Verification
  - Update `README.md` showcasing `bruriah setup cursor` and `bruriah setup claude`.
  - Run full test suite (`pytest tests`), `mypy src`, `ruff check src tests`.
