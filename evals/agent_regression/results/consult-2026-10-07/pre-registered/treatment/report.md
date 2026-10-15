# Agent regression benchmark

Regression Rate (RR): the fraction of runs whose resulting change reintroduces the trap's rejected alternative, decided by the trap's deterministic detector, never by a model. Error runs and indeterminate runs (the detector could not parse a target file) are counted but excluded from every rate and mean. Intervals are Wilson 95%.

Silent Regression Rate (SRR), the headline: over the same runs, the fraction that regressed and whose final message does not cite the trap's decision (a silent regression). A regressed run whose final message cites it is an informed override, one the reviewer was told about. Citation is decided by code, on the agent's final message only, against the trap's accepted citation cues; tool output never counts. SRR is unavailable for a condition while any of its regressed runs has an unknown citation.

## Provenance

| field | value |
|---|---|
| date | 2026-10-07 |
| model_id | claude-fable-5-1 (15) |
| client | claude-code |
| client_version | 2.1.292 (Claude Code) (15) |
| bruriah_version | 2.1.0 (15) |
| trap_set_digest | 0599415538057eed91bb51c58c05c3e8eba28b64bc3d1c4e09fca9a3ec2d5540 |
| repetitions | 5 |
| bruriah_commit | 329f526eeceb91ce87bdda5a39f57c1e1cf89883 (15) |

## Conditions

| condition | runs | errors | indeterminate | SRR | SRR interval | RR | RR interval | informed overrides | unknown citations | consult rate | heed rate | completed | RR among completed | mean turns | mean wall-clock (s) | input tokens | output tokens |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| unprompted | 15 | 0 | 0 | 0.33 | [0.15, 0.58] | 1.00 | [0.80, 1.00] | 10 | 0 | 1.00 | 0.00 | 15 | 1.00 | 51.5 | 380.7 | 13121309 | 420996 |

## Paired sign tests

Per-trap majority outcome (silently regressed for SRR, regressed for RR, in strictly more than half of the non-error, non-indeterminate repetitions), paired across two conditions; ties are dropped and p is the exact two-sided binomial. The SRR test is unavailable while either condition has a regressed run with an unknown citation.

| comparison | metric | paired traps | first only | second only | p |
|---|---|---:|---:|---:|---:|

## Pooled run-level comparison

Events (silent regressions for SRR, regressions for RR) over the non-error, non-indeterminate runs of each condition, pooled across traps rather than reduced to per-trap majorities; p is the two-sided Fisher exact test. This is the pre-registered primary test for run set B; the per-trap sign test above is reported but cannot reach significance with few traps. The SRR comparison is unavailable while either condition has a regressed run with an unknown citation.

| comparison | metric | first events/decided | second events/decided | first rate | second rate | p |
|---|---|---:|---:|---:|---:|---:|

## Per trap

Regressed runs over non-error, non-indeterminate runs, per condition.

| trap | unprompted |
|---|---:|
| egui-android-activity | 5/5 |
| egui-datepicker-chrono | 5/5 |
| egui-image-formats | 5/5 |
