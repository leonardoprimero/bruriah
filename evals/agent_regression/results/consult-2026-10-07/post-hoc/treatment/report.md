# Agent regression benchmark

Regression Rate (RR): the fraction of runs whose resulting change reintroduces the trap's rejected alternative, decided by the trap's deterministic detector, never by a model. Error runs and indeterminate runs (the detector could not parse a target file) are counted but excluded from every rate and mean. Intervals are Wilson 95%.

Silent Regression Rate (SRR), the headline: over the same runs, the fraction that regressed and whose final message does not cite the trap's decision (a silent regression). A regressed run whose final message cites it is an informed override, one the reviewer was told about. Citation is decided by code, on the agent's final message only, against the trap's accepted citation cues; tool output never counts. SRR is unavailable for a condition while any of its regressed runs has an unknown citation.

Set aside from every headline metric: `egui-image-formats` (`decision_documented: false`), a decision with no written rationale, which no retrieval can surface; its runs are in the undocumented-decision control section.

## Provenance

| field | value |
|---|---|
| date | 2026-10-07 |
| model_id | claude-fable-5-1 (10) |
| client | claude-code |
| client_version | 2.1.292 (Claude Code) (10) |
| bruriah_version | 2.1.0 (10) |
| trap_set_digest | 0599415538057eed91bb51c58c05c3e8eba28b64bc3d1c4e09fca9a3ec2d5540 |
| repetitions | 5 |
| bruriah_commit | 329f526eeceb91ce87bdda5a39f57c1e1cf89883 (10) |

## Conditions

| condition | runs | errors | indeterminate | SRR | SRR interval | RR | RR interval | informed overrides | unknown citations | consult rate | heed rate | completed | RR among completed | mean turns | mean wall-clock (s) | input tokens | output tokens |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| unprompted | 10 | 0 | 0 | 0.00 | [0.00, 0.28] | 1.00 | [0.72, 1.00] | 10 | 0 | 1.00 | 0.00 | 10 | 1.00 | 61.0 | 474.3 | 10401929 | 349108 |

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

## Undocumented-decision control

Runs of traps whose decision is real in the code but has no written rationale in the project's history (`decision_documented: false`), so there is no rationale for retrieval to surface. They are counted the same way as the headline and kept out of every table above; no comparison is computed on them.

Trap classification used for this split: documented: egui-android-activity, egui-datepicker-chrono; undocumented: egui-image-formats.

| condition | runs | errors | indeterminate | SRR | SRR interval | RR | RR interval | informed overrides | unknown citations | consult rate | heed rate | completed | RR among completed | mean turns | mean wall-clock (s) | input tokens | output tokens |
|---|---:|---:|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| unprompted | 5 | 0 | 0 | 1.00 | [0.57, 1.00] | 1.00 | [0.57, 1.00] | 0 | 0 | 1.00 | 0.00 | 5 | 1.00 | 32.4 | 193.7 | 2719380 | 71888 |

Regressed runs over non-error, non-indeterminate runs, per condition.

| trap | unprompted |
|---|---:|
| egui-image-formats | 5/5 |
