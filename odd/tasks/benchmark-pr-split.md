# Split the agent-regression benchmark branch into PRs

Branch being split: `eval/agent-regression-benchmark` (57 commits since `1d36bc7`, 145 files,
about 19,500 changed lines). Strategy chosen by the user on 2026-10-08:
`chain_strategy=stacked-to-main`. Default branch: `main`.

## Constraints

- The ODD records cite about 30 commit hashes that exist only on this branch (pre-registrations,
  reviewed commits). Slices are rebuilt from the branch's final state, so those hashes would not
  reach `main`. An archive tag on the branch head keeps them resolvable on origin. The tag is
  created locally before any slice. Pushing it needs the user's go-ahead, like every push.
- Each slice branches from the previous slice, so a slice's diff against its parent shows only
  its own work unit. Each slice must pass the full test suite and `ruff check` on its own.
- No push, PR or merge without explicit approval per remote action.
- The only conflict with `main` is the README test-count line. Recompute it per slice.

## Slices (stacked, in merge order)

1. Product: surface historical decision commits (`fa26c59`, `src/bruriah` plus tests), about 290 lines.
2. Benchmark core: trap loader, detection, runs, metrics, citation, report and runner, with their
   tests, about 7,000 lines.
3. Claude Code adapter and gated pre-edit hook, with tests, about 3,400 lines.
4. All 12 traps, with `tests/test_agent_regression_traps.py` and `traps/README.md`, about 9,000
   lines (mostly fixture data).
5. ODD records, about 1,100 lines.

Amended on 2026-10-08: the original plan had three trap slices. The dependency map showed that
every test in `test_agent_regression_traps.py` needs all 12 traps (exact family counts, sorted
ids, and the README check at line 303). Splitting the traps would ship detectors without their
tests.

Every slice except 1 exceeds 400 lines. One slicing pass found no cohesive split below that
without cutting single modules. Tests and fixture data dominate the size.

## Tasks

- [x] 0. Create the local archive tag `archive/agent-regression-benchmark-2026-10-08` on the branch head.
- [x] 1. Build slice 1 locally, then verify it.
- [x] 2. Map the file dependencies for slices 2-5 (tests that load shipped traps, runner imports).
- [ ] 3. Build slices 2-5 locally, verifying each one.
- [ ] 4. Get approval, push the tag and the slice branches, and open PR 1 (later PRs as each parent merges).

## Evidence

- Task 0: tag `archive/agent-regression-benchmark-2026-10-08` on `8b17046` (local).
- Task 1: branch `feat/surface-decision-commits` (worktree `~/bruriah-worktrees/split-1`) from
  `origin/main` `4352a2a`:
  - cherry-picks `d17db2e` (from `fa26c59`) and `1fdb220` (from `bd46442`);
  - `style(tests)` formats `tests/test_context.py`;
  - `docs:` updates the README test count, 1,775 to 1,787.
  Checks: `ruff check` and `mypy src` pass, `ruff format --check` is clean, pytest gives 1764
  passed and 23 skipped. Known flake: `test_platform` auto-discovery passed on rerun.
- Task 2: no S2 test imports `claude_code` or `gated_hook`. `run.py` imports them lazily in
  `_run()`, and the S2 tests use `--dry-run`. `test_agent_regression_claude_code.py` needs no
  shipped traps.
