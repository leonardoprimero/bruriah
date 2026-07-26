# Project-memory retrieval eval

Twelve "why was this decided" questions about **this repository's own history**, each with the
commit that answers it as ground truth. The answers are known because the commits were written
deliberately: this repo is its own test case, the same way `verify_legacy_baseline.py` is.

## Reproduce

```bash
python scripts/git_corpus.py --repo . --out /tmp/corpus
bruriah index --corpus-root /tmp/corpus --policy policy.yaml --data-dir /tmp/data
# then score search() against decisions-{es,en}.jsonl with evals/retrieval/metrics.py
```

**One rule the recipe needs and did not state:** the generator writes `date-sha8-slug.md`, and the
recorded ground truth carries **no sha** — deliberately, because a sha does not survive a history
rewrite and this eval is meant to outlive one. Strip `-[0-9a-f]{8}` after the date before matching.
Without that the scorer reports 0% and looks like a retrieval collapse rather than a name mismatch.

`tests/test_project_memory_eval.py` asserts every ground-truth document is still produced by this
repository's own history, so the numbers below stay reproducible from the published repository.

## Baseline, measured 2026-07-25

76 commits carrying an explanatory body, 152 passages.

| question language | recall@3 | recall@10 | MRR@10 |
|---|---|---|---|
| English (matches the corpus) | **75%** | 92% | 0.73 |
| Spanish (corpus is 95% English) | **25%** | 92% | 0.29 |

**What this measures and what it does not.** Recall@10 is identical across both, so the right
document is retrieved either way: the gap is entirely in RANKING. Cross-lingual retrieval costs two
thirds of the top-3 precision, which matters because a Spanish-speaking developer writing English
commits is the common case, not an edge one.

## Diagnosis, measured 2026-07-26

`RetrievalMatch` carries `lexical_rank` and `vector_rank` alongside the fused `rank`, so each leg
can be scored separately from a single pass. Over 84 documents / 168 passages:

| question language | BM25 alone | vectors alone | fused (shipped) |
|---|---|---|---|
| English | **83%** | 58% | **83%** |
| Spanish | 17% | **58%** | 33% |

**For Spanish the fusion is worse than the vector leg on its own — 58% down to 33%.** The embedding
model is already multilingual and finds the right document; reciprocal-rank fusion then averages
that answer against a lexical leg that cannot cross languages at all, and the correct document
falls out of the top 3. It is visible case by case:

| Spanish question | BM25 | vectors | fused |
|---|---|---|---|
| como se decidio la custodia de la llave de firma | — | **2** | 9 |
| por que la capa de skills era invisible para el agente | 27 | **2** | 7 |
| que decidimos sobre el permission envelope de las skills | 16 | **2** | 5 |
| por que extrajimos las primitivas de puntero | — | **4** | 10 |

So the fix is a weighting problem, not a model problem: the lexical leg has to lose influence when
the query language does not match the corpus. That is a small change with a number to beat, which
is a different situation from "improve cross-lingual retrieval".

Note that English loses nothing from BM25 — there it is the *strong* leg, 83% against the vector
leg's 58%. Any fix has to keep that, which rules out simply dropping the lexical leg.

## Result, measured 2026-07-26

`retrieval._fuse` now scales the lexical leg's contribution, and `search` discounts it to **0.1**
when the query language and the corpus language are identified and differ. Same 84 documents:

| | recall@3 | recall@10 | MRR@10 |
|---|---|---|---|
| English, before | 83% | 92% | 0.80 |
| English, after | 83% | 92% | 0.80 |
| Spanish, before | 33% | 83% | 0.29 |
| **Spanish, after** | **58%** | **92%** | **0.50** |

Spanish recall@3 closes to the vector leg's ceiling, `recall@10` reaches parity with English, and
MRR@10 nearly doubles. English is untouched — not "within noise", identical, because the discount
only fires on a mismatch and there is none.

**The weight was chosen, not fitted.** The sweep reads 1.0 → 33%, 0.5 → 50%, 0.25 → 50%,
0.1 → 58%, 0 → 58%. The gap between 0.25 and 0.1 is a single question out of twelve — noise at
this sample size, and not the reason for the choice. 0.1 was picked because of what it does
arithmetically: a lexical rank-1 hit then contributes 0.1/61 against a vector rank-1 hit's 1/61,
so the leg can only separate candidates the vector leg already ranked together. A tiebreaker, not
a voter. That is the role the measurement says it should have when it cannot read the language it
is being asked in.

**What is still not solved.** 58% is the vector leg's own ceiling — this recovers what fusion was
destroying, it does not make cross-lingual retrieval as good as same-language retrieval. Twelve
questions is a small sample and this is one corpus in two languages; treat the direction as
established and the exact figures as indicative. The language signal is a function-word counter
(`bruriah/language.py`), deliberately not a model, and it abstains often — on abstention nothing
changes, which is the safe direction but also means short or identifier-heavy questions get no
help at all.
