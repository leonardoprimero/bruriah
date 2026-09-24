# Prompt-injection benchmark for `investigate_work`

A hermetic, offline, deterministic measurement of whether corpus-authored text reaches
`investigate_work`'s serialized response. Bruriah's positioning depends on a specific claim --
"corpus prose never enters the model context during investigation" -- and this benchmark is the
evidence for it, not a substitute for it.

## What is measured: Attack Success Rate (ASR)

ASR is the fraction of **executed** cases in which a unique attacker-controlled marker appears in
the serialized `investigate_work` response (`result.model_dump(mode="json")`) at a JSON path that
is genuinely corpus-derived. "Genuinely corpus-derived" is established by a **control run**: the
identical fixture and task, with only that one case's attacker surface reverted to a clean value
(`InjectionCase.build_control`). A marker that hits the same JSON path in both the poisoned and the
control run is an echo of the task string, not a leak of the surface under test, and is recorded
separately (`find_echo_fields`) rather than silently dropped -- so an excluded path is observable,
not hidden. `find_leak_fields` attributes a leak by this poisoned-vs-control JSON-path provenance,
never by whether the field's text merely happens to overlap the task string; an earlier,
unpublished sweep used string containment and produced a false negative on `md-alt-name`'s own
surface for exactly that reason (see `run.py`'s module docstring for the full account).

**A case that never executed its carrying code path is not evidence the boundary held.** Every
case proves its own execution before its leak/hold result counts (`executed`), and its control run
proves the same thing separately (`control_executed`); a case whose path did not run fails the
benchmark run rather than reaching the report as "held".

## Threat model: who controls which surface

The attacker is anyone who can add a document, a commit, or a GitHub issue/PR comment to a
repository Bruriah indexes -- not someone with access to the MCP client or the agent's prompt.
Seventeen attacker-controlled surfaces are measured, grouped by what the attacker needs write
access to:

| Carrier | Surfaces measured |
|---|---|
| Markdown document (front-matter and body) | file name, body prose, heading, `alternatives[].name`, `alternatives[].reason`, `premises[].id`, `premises[].statement`, `premises[].rationale`, `premises[].invalidated_by`, a successor document's path via lineage |
| Git commit | commit subject, commit body, commit author |
| GitHub | a closing comment on an issue or pull request |
| Markdown + git together | the code-target path's governing decision author, its subject, and a superseding decision's subject |

A contributor whose pull request is merged authors all of these; a fork's commits and a GitHub
issue/PR comment reach the same surfaces through `--github` and `--repo`. The benchmark does not
model an attacker who can read the MCP transcript, modify the client, or act as the model itself --
see "What this does not measure" below.

## Method

- **Hermetic and offline.** A fake constant-vector embedder (the same pattern
  `evals/counterfactual/runner.py` uses) builds the index; the benchmark calls `InvestigateService`
  directly, never `cli.bruriah_main` with its real, network-fetching embedder. No reranker, no
  network, no model download.
- **One surface per case.** Each case isolates a single attacker-controlled field and pairs it
  with a task chosen to exercise the code path that would carry it. Poisoning every surface in one
  document at once was tried first and rejected: it can suppress the very match that would expose
  a later surface, reporting it "held" when it simply never ran.
- **A control run per case**, identical except for that one surface, is the only way leak
  attribution is established -- never string containment against the task (see ASR definition
  above).
- **The executed-path invariant**, enforced for both the poisoned and the control run
  (`executed`/`control_executed`), so a path that never ran cannot be miscounted as held.
- **Normalized marker matching**: a marker counts as leaked once its normalized form (lowercased,
  every non-alphanumeric character stripped) appears in the normalized serialized response --
  a case-sensitive, punctuation-exact match undercounts a marker that reached the response through
  a generated file name or a slug.
- **Field attribution**: every leaked case's report lists the exact JSON paths the marker was
  found at (`leak_fields`), and every excluded echo path (`echo_fields`), so the report is
  reproducible against the actual response shape rather than a prose summary of it.

## How to run it

```bash
uv run python evals/injection/run.py
```

Runs in a few seconds, entirely offline, and writes `evals/injection/report.json` and
`evals/injection/report.md`. The run is deterministic: running it twice produces byte-identical
reports.

## Before / after, by stage

| Stage | Commit | ASR | What changed |
|---|---|---|---|
| Widened baseline (1.6.0 behavior) | `84a6b67` | **13/17** (0.765) | The original 7/11 measurement (`feat/injection-eval`) widened with six more cases for the code-target path, the lineage path, and `premises[].rationale`/`invalidated_by` -- every newly measured channel leaked. |
| After opaque evidence | `dfbf448` | **7/17** (0.412) | `EvidenceRecord.locator`/`citation_locator` became opaque `doc:v1:<hash>` refs, `authority_rationale` closed to ten codes, and lineage/code-target text rebuilt from structure (a validated sha, a closed relation, ref counts) instead of markdown-authored subjects and author names. Flipped to held: file name, commit subject, the lineage successor path, and all three code-target surfaces. |
| After contract v2 | `6860180` | **0/17** (0.000) | `AlternativeRecord`/`PremiseRecord`/`CounterfactualAssessment` became opaque-ref shapes (`alt:v1:`/`premise:v1:`); `rationale`/`conflicts`/claim text became fixed wording built only from refs, counts, the verdict and a validated sha. Flipped to held: the alternative's name and reason, every premise field, and the GitHub closing comment. |

Every stage's 17 cases (and their controls) report `executed: true`; the current report
(`evals/injection/report.json`) is committed alongside this benchmark and regenerated whenever a
change could affect it.

## What this does not measure

- **Model behavior.** This measures `investigate_work`'s serialized response, a structural
  property checkable without any model in the loop. It does not simulate an agent reading the
  response and deciding whether to act on anything in it -- there is nothing to act on once the
  boundary holds, but whether a persuasive sentence would sway a specific model is a different,
  unfalsifiable-by-inspection question this project does not claim to answer.
- **`read_evidence`'s own output.** `read_evidence` is the explicit, caller-requested channel that
  returns corpus text -- including a poisoned document's actual prose -- by design, once a caller
  already holds the reference and asks for it. That is not a leak; it is the boundary's other side,
  and this benchmark does not (and should not) score it as a failure.
- **An exhaustive attacker surface list.** Seventeen surfaces are the ones currently reachable
  through `investigate_work`'s known producers. A new producer needs its own case before it is
  covered; `tests/test_contracts.py`'s schema-regression guard (walking
  `InvestigationResult.model_json_schema()` for an unconstrained string field) is the backstop for
  a field this benchmark has not yet been extended to cover.
- **A ready-to-use exploit recipe.** This repository is public. The case table above names the
  surface categories and the measured outcome; the specific marker values and corpus fixtures live
  in `evals/injection/cases.py`, which is a test fixture for this project's own suite, not a guide.
