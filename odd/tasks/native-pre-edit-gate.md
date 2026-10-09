# Native Pre-Edit Gate

## Goal
Add an opt-in `gated` benchmark condition using Claude Code's native `PreToolUse` hook, so a first file mutation is blocked with a decision-review message before the agent can edit. Preserve the published baseline/unprompted/prompted conditions and their comparability.

## Scope
- Add `gated` as an opt-in condition; do not include it in the runner's default condition set.
- Inject isolated per-run Claude settings, never mutate user/global Claude settings.
- Match native `Edit`, `Write`, and `MultiEdit` calls and deny the first mutation with an actionable reason; allow the retry so the run can complete.
- Keep the existing MCP tools and product server unchanged in this work unit; the gate is an integration experiment, not a claim that MCP can enforce native tool policy.
- Record gate activity in the transcript/tool-call evidence where the existing runner can observe it.
- Add deterministic tests for command construction, condition parsing/defaults, and gate state behavior.

## Acceptance Criteria
1. Existing default runner invocation still plans only baseline, unprompted, and prompted.
2. `--conditions gated` plans and runs only the new opt-in condition.
3. Gated runs still register Bruriah MCP and receive the prompted investigation instruction.
4. The isolated `PreToolUse` hook denies the first native file mutation and allows subsequent mutations for that run.
5. User/global Claude settings and the historical benchmark output are untouched.
6. Focused tests pass and a bounded smoke run compares gated behavior without rerunning the old benchmark.

## Work Units
- [x] Add opt-in gated condition and isolated hook configuration.
- [x] Add focused adapter/runner/hook tests.
- [x] Verify and run bounded gated smoke.
- [x] Commit the gated condition as an opt-in benchmark instrument.

## Verification Evidence
- TDD RED/GREEN: focused benchmark tests covered condition defaults, argv compatibility, hook state, shell-metacharacter safety, per-run isolation, and clone tampering; final focused suite passed with 358 tests.
- Ruff passed on the benchmark and focused test surfaces.
- Mypy was attempted but remains blocked by the pre-existing duplicate `detect` modules under trap directories.
- With `--repetitions 1`, default dry-run remains 36 invocations across baseline/unprompted/prompted; explicit `--conditions gated` plans 12 gated invocations (the default of 5 repetitions gives 180/60).
- Pre-commit re-verification (2026-10-02): `tests/test_agent_regression_claude_code.py` and `tests/test_agent_regression_eval.py` pass 259/259; Ruff clean; dry-run counts above confirmed; no paid runs.
- Hardened hook smoke: 3 gated `egui-android-activity` runs completed successfully at `/tmp/bruriah-egui-gated-smoke-long.FCleI8`, total cost USD 6.46056375. Each run had exactly one native first-Edit denial, then a successful retry; all three still detected `regressed=true` and `completed=true`.
- Conclusion: the native hook is operational and safely isolated, but a one-time denial does not enforce adherence to the historical decision. This is an integration experiment, not yet a product fix.
- Harness caveat: verifier reported an unexpected oversized-output spill at `/var/folders/yb/gfxxt4fs6d97wdd4thsrtlt00000gn/T/pi-bash-8f0627086a616289.log`; it was not read or cleaned. Repository status stayed unchanged.

## Decision
Keep `gated` as an opt-in benchmark instrument, not a product fix. It measures that a one-time native denial does not change adherence (heed 0/3), and serves as the comparison point for a future persistent-acknowledgement or host-authorization gate.

## Commit Evidence
- `eee486f` feat(evals): add opt-in gated condition with an isolated native pre-edit hook (7 files, +554/-14).
- Native review: lineage `review-361755826198b896`, committed-only range `bd46442..eee486f`, high tier, 4 lenses; approved and acknowledgement burned.

## Follow-ups (non-blocking review findings)
- `gated_hook.py:40-46` fails open silently when the state write fails; a broken gate is indistinguishable from an ignored one.
- `claude_code.py:638` does not record gate activity as structured run evidence; denials are only visible in transcripts.
- `claude_code.py:312-314` never cleans the per-run gate directories under the isolated cache.
- `tests/test_agent_regression_eval.py:1008-1018` does not pin the default report shape.
