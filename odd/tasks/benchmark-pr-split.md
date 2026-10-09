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
4. Own-history traps (4), about 2,800 lines.
5. Leakcanary traps (3), about 1,500 lines.
6. Egui traps (5), about 4,700 lines.
7. ODD records and benchmark docs, about 1,100 lines.

Every slice except 1 exceeds 400 lines. One slicing pass found no cohesive split below that
without cutting single modules. Tests and fixture data dominate the size.

## Tasks

- [ ] 0. Create the local archive tag `archive/agent-regression-benchmark-2026-10-08` on the branch head.
- [ ] 1. Build slice 1 locally, then verify it.
- [ ] 2. Map the file dependencies for slices 2-7 (tests that load shipped traps, runner imports).
- [ ] 3. Build slices 2-7 locally, verifying each one.
- [ ] 4. Get approval, push the tag and the slice branches, and open PR 1 (later PRs as each parent merges).

## Evidence
