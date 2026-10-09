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
- [x] 3. Build slices 2-5 locally, verifying each one.
- [x] 4. Get approval, push the tag and the slice branches, and open PR 1 (later PRs as each parent merges).

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
- Task 3 (worker, worktree `~/bruriah-worktrees/split-2`). Files were extracted with
  `git archive SRC <paths> | tar -x` and blob-checked against `SRC:<path>`.
  - S2 `feat/benchmark-core`: `cafbac9` + count `3d29459`, 12 files, +5239/-1. 2137 passed.
  - S3 `feat/benchmark-claude-code-adapter`: `6c6e848` + count `0b1f97e`, 4 files, +3351/-1.
    2306 passed.
  - S4 `feat/benchmark-traps`: `ac1ab0d` + count `644e39a`, 115 files, +9659/-1. 2469 passed.
  - S5 `docs/benchmark-odd-records`: `4a4cdc8`, 7 files, +991. 2469 passed.
  ruff check and format are clean on every slice, and each has 23 skips (missing environment
  prerequisites). The `test_platform` flake failed once on S2, S3 and S4, and passed on rerun
  each time.
- Invariant, corrected: the plan compared against SRC, which lacks `main`'s later PRs #35-#37.
  Against `origin/main`, the stack changes exactly SRC's 146 files, and every blob equals SRC's
  except `README.md` (count line, 1,775 to 2,492) and `tests/test_context.py` (slice 1 format).
- Task 4 (approved 2026-10-08): pushed tag `archive/agent-regression-benchmark-2026-10-08`
  (-> `8b17046`) and the 5 branches. Opened stacked PRs:
  - #38 `feat/surface-decision-commits` -> `main`
  - #39 `feat/benchmark-core` -> #38's branch
  - #40 `feat/benchmark-claude-code-adapter` -> #39's branch
  - #41 `feat/benchmark-traps` -> #40's branch
  - #42 `docs/benchmark-odd-records` -> #41's branch
  The bodies first numbered the chain #1-#5, which GitHub autolinks to old issues; they were
  edited to the real numbers. CI (`pull_request`, no branch filter) runs on every PR. No native
  re-review of the slices: they are byte-identical rebuilds of commits already reviewed on the
  source branch, as recorded in the ODD records.
- Remaining: merge in order, retargeting each next PR to `main` after its parent merges. Then
  retire `eval/agent-regression-benchmark` and the split worktrees (the tag keeps the history).
- CI found 6 Windows-only failures in #40 (and so in #41), all test-side POSIX assumptions:
  - five command-line tests compared argv against POSIX path strings;
  - one tests `#!/usr/bin/env` lookup, which is POSIX-only.
  Fix `0d4b962` on `feat/benchmark-claude-code-adapter` builds the expected paths with
  `str(Path(...))` and skips the env-shebang test on Windows. It was merged forward into #41 and
  #42 (`3dc1fc6`, `46d35d4`), without a force-push, and explained in a #40 comment. The source
  branch had never run CI, so nothing had exercised Windows before. #38 and #39 were 15/15 green.
- Merged #38 as `358ddfc` (merge commit), then retargeted #39 to `main`. Its diff is exactly its 12 slice files.
- Merged #39 as `1a309eb`, then retargeted #40 to `main`: 4 files, CI 15/15, including the Windows fix.
- Merged #40 as `7db9d63`, then retargeted #41 to `main`.
- Merged #41 as `fdd7e32` (115 files; `gh --json files` caps at 100, so use `changedFiles`). Retargeted #42 to `main`.
- Before merging #42, this record was refreshed into it from the source branch's latest version.
