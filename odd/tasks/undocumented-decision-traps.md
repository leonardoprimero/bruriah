# Undocumented-decision traps

Branch: `eval/agent-regression-benchmark` (worktree `~/bruriah-worktrees/eval-bench`); task 4 on a
new branch from `main`.

## Why

The 2026-10-07 consult measurement left 5/15 silent regressions in the treatment arm, all in
`egui-image-formats`. Checked on GitHub on 2026-10-08: the trap's `decision_ref`
(`github:emilk/egui#4489`) holds no rationale. The issue is titled and bodied "Gif Support", with
one comment, "#3951". PR #4620, which closes it, adds GIF to `egui_extras`, and PR #3951 is a
DynamicImage loader. Neither argues against enabling more image formats in eframe. The png-only
dependency arrived in `7b76161a6` (#2996) without a stated reason. The only trace is the
Cargo.toml comment `# Needed for app icon`. No retrieval, with or without `--github`, can surface
a decision that was never written down.

## Decision

- Keep the trap, and classify it as an undocumented-decision control.
- Headline metrics (SRR, RR, consult, pooled comparison) cover documented-decision traps only.
  Undocumented-decision traps are reported in a separate control section.
- The 2026-10-07 measurement was pre-registered, so its pre-registered result stays as published.
  The exclusion is a post-hoc analysis, labeled as such everywhere it appears.
- `--github` stays opt-in. On egui it needs about 2400 PR fetches plus issues and comments (about
  45-60 min, near the 5000/h limit).

## Tasks

- [x] 1. Trap schema: optional `decision_documented` key (bool, default true). Mark
      `egui-image-formats` false, with the evidence above as a manifest comment. Test-first.
- [x] 2. Metrics and report: headline excludes undocumented-decision traps. Separate control
      section in markdown and JSON. Test-first.
- [x] 3. Post-hoc re-report of the 2026-10-07 control and treatment runs. Document it in the
      traps README and this file, labeled post-hoc.
- [x] 4. On a new branch from `main`: post-hoc note in `README.md` and
      `odd/tasks/agent-consult-instructions.md`. No push without approval.

## Evidence

### Task 1: `457beb0`

`feat(evals): mark traps whose decision has no written rationale`. RED: 21 failed (unknown key,
no attribute). GREEN: 457 passed in the trap and eval test files. `trap_set_digest` hashes only
(trap_id, commit, prompt), so the new key leaves every stored digest unchanged.

### Task 2: `e42c4d7`, `a849693`

- `e42c4d7` `feat(evals): report undocumented-decision traps apart from the headline`.
  `report.split_undocumented` runs before `summarize`, the pairing and the pooled comparison, so
  `metrics.py` is unchanged. With no control runs, the output is byte-identical. RED: 6 failed.
  GREEN: 463 passed.
- `a849693` `fix(evals): split undocumented-decision runs in fresh run reports`. `run.py` still
  rendered every run into the headline. RED: 1 failed (end-to-end `run.main`). GREEN: 705 passed
  across the claude_code, eval, traps and citation test files. `ruff check` is clean.
- Full suite (worker, before `a849693`): 2462 passed, 23 skipped, 1-2 failed, all pre-existing and
  unrelated:
  - `test_readme_claims.py::test_readme_test_count_matches_collected`: the README count is stale on
    this branch.
  - `test_platform.py::test_resolve_paths_auto_discovery_precedence`: intermittent. It writes under
    the real `~/Library/Application Support/bruriah/projects`.

### Task 3: post-hoc re-report (2026-10-08)

This is a post-hoc analysis. The pre-registered result of the 2026-10-07 measurement stands as
published: SRR control 15/15, treatment 5/15. The runs are the same; only the classification
changed. Reports were regenerated with `report.py <run-dir> --out DIR` into
`~/bruriah-worktrees/consult-launch/posthoc-2026-10-08/{control,treatment}`. The original
`out-*-2026-10-07/report.md` files are untouched.

| | control | treatment |
| --- | --- | --- |
| pre-registered, 15 runs (3 traps) | SRR 15/15 | SRR 5/15 |
| post-hoc, documented-decision traps (10 runs, 2 traps) | SRR 10/10 | SRR 0/10 [0.00, 0.28] |
| post-hoc, undocumented-decision control (5 runs) | SRR 5/5 | SRR 5/5 |

- On documented-decision traps, Fisher exact for 10/10 vs 0/10 gives p = 5.4e-6 one-sided and
  1.1e-5 two-sided. Post-hoc, so it is not a confirmatory test.
- RR stays 1.00 in both arms. The agents still make the change, but in treatment they say so: on
  documented-decision traps, every regression is an informed override.
- On the control trap, neither arm cites anything, which is what we expect when no rationale
  exists to find.

### Task 4: `d6ee99d` on `docs/undocumented-decision-trap` (from `main` `e36695e`)

`docs: record the post-hoc re-report without the undocumented-decision trap`. The README sentence
that blamed retrieval now says the decision was never written down, and gives the post-hoc 10/10
vs 0/10 while stating that the pre-registered result stands.
`odd/tasks/agent-consult-instructions.md` gains a dated post-hoc section, and its "Next
diagnostic" bullet points to it. Not pushed.
