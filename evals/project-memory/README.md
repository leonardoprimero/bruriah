# Project-memory retrieval eval

Twelve "why was this decided" questions about **this repository's own history**, each with the
commit that answers it as ground truth. The answers are known because the commits were written
deliberately: this repo is its own test case, the same way `verify_legacy_baseline.py` is.

## Reproduce

```bash
bruriah corpus --repo . --out /tmp/corpus
bruriah index --corpus-root /tmp/corpus --policy policy.yaml --data-dir /tmp/data
# then score search() against decisions-{es,en}.jsonl with evals/retrieval/metrics.py
```

*(That first line said `python scripts/git_corpus.py` until 0.5.0. The generator moved into the
package when the front page started telling readers to run a file the wheel does not ship.)*

**One rule the recipe needs and did not state:** the generator writes `date-sha8-slug.md`, and the
recorded ground truth carries **no sha** — deliberately, because a sha does not survive a history
rewrite and this eval is meant to outlive one. Strip `-[0-9a-f]{8}` after the date before matching.
Without that the scorer reports 0% and looks like a retrieval collapse rather than a name mismatch.

`tests/test_project_memory_eval.py` asserts every ground-truth document is still produced by this
repository's own history, so the numbers below stay reproducible from the published repository.

## How much these questions give away, measured 2026-07-27

Read this before any table below it. It is the strongest caveat on this page and it is a
measurement, not a disclaimer.

These questions were written by the person who wrote the commits that answer them. That is the only
way this eval could exist — the ground truth is known because the reasoning was written down
deliberately — and it is also the failure mode of every self-authored retrieval eval: if the
question is phrased in the answer's own vocabulary, retrieval is being credited for matching words
it was handed. Everyone says they avoided that. Nobody can show it, because "I wrote the questions
independently" is a claim about a process and a reader cannot check a process.

So it is measured instead. `evals/retrieval/leakage.py` weights each question term by its IDF over
the derived corpus and reports two numbers: `share`, the weighted fraction of the question that
also appears in its own answer, and `peak`, the single most distinctive term the question handed
over, as a fraction of the highest IDF this corpus can produce. There is no stopword list — that
would be a knob for deciding which words do not count, and picking a different list moves the mean
by 0.12. IDF does the same work from the corpus itself. Reproduce with no index and no model:

```bash
bruriah corpus --repo . --out /tmp/corpus
python evals/retrieval/report_leakage.py --corpus /tmp/corpus \
    --questions evals/project-memory/decisions-en.jsonl
```

147 documents, the corpus as of `9591f91`:

| | mean `share` | mean `peak` | questions with `peak` ≥ 0.50 |
|---|---|---|---|
| English | 0.577 | 0.589 | **10 of 12** |
| Spanish | 0.066 | 0.336 | 3 of 12 |

**Ten of the twelve English questions hand over a term this corpus treats as distinctive.** Three
are close to restatements of the commit's own subject line with `why did` in front:

| question | the commit subject it restates |
|---|---|
| *why did user activated skills not reach the server* | `make user-activated skills reach the server` |
| *why is approval bound to the digest not the version* | `add digest-bound approval records` |
| *why did we extract the pointer primitives* | `extract atomic pointer primitives to pointer.py` |

The Spanish set leaks far less, but not from better question design — it leaks less because it is in
another language. Which means the English/Spanish gap reported further down this page has **two**
explanations, not one: cross-lingual retrieval really is harder, and the English questions really do
give more away. These figures cannot separate those.

### Does leakage actually change the result? Paired, and yes — 3 questions

Comparing different questions confounds difficulty with leakage. Comparing two phrasings of one
question against one fixed target does not. The three restatements above, re-asked from the
consequence someone would actually notice:

| phrasing | `peak` | rank of the correct document |
|---|---|---|
| *why did user activated skills not reach the server* | 0.68 | **1** |
| *the tool confirmed something was enabled and it never took effect* | 0.03 | 6 |
| *why is approval bound to the digest not the version* | 0.54 | **1** |
| *what prevents content from being altered after a human signed off* | 0.07 | >10 |
| *why did we extract the pointer primitives* | 0.68 | **1** |
| *how can a second artifact kind get the same crash safety without duplicating it* | 0.56 | **1** |

recall@3 goes **3/3 → 1/3**, and the one that survived is the one whose rewrite still leaked. That
internal consistency is what makes three questions worth reporting at all.

**The honest objection, which is not answered here:** those rewrites are also *vaguer* than the
originals, and they were written by someone who had read the answers. The drop mixes leakage with
vagueness and this experiment cannot separate them. That is exactly why
[issue #4](https://github.com/leonardoprimero/bruriah/issues/4) asks for questions drawn from a
source independent of the corpus — a linked issue, a PR discussion, a release note. That is not
methodological decoration; it is the only thing that separates the two.

### Stratifying by leakage proves nothing at twelve questions, and here is the proof

The obvious next step is to split the questions into high- and low-leakage bands and compare recall.
Done on `peak` at a 0.50 threshold, and on `share` at the same threshold, over the same twelve
English questions and the same engine:

| split on | high-leakage band | low-leakage band |
|---|---|---|
| `peak` | recall@3 0.80 (n=10) | recall@3 **1.00** (n=2) |
| `share` | recall@3 0.90 (n=10) | recall@3 **0.50** (n=2) |

**Opposite conclusions from the same data.** The low band holds two questions either way, and which
two depends on which number you sort by, so the sign of the effect is decided by a choice that has
nothing to do with retrieval. `evals/retrieval/leakage.py` ships `stratify` because the comparison
is the right one to make; this table is why it is not made *here* yet.

That is what "twelve questions means one question is eight points" costs in practice, and it is the
reason the sample size caveat on this page is not boilerplate.

### What this does to the numbers below

It does not retract them. Recall@3 of 0.83 on English is a real measurement of a real engine over a
real corpus, and the relative comparisons on this page — between models, between fusion weights,
between languages — hold, because leakage is a property of the question set and the question set was
held fixed across all of them. A confound that is constant cannot explain a difference.

What it does mean is narrower and worth stating exactly: **0.83 cannot be separated from lexical
overlap with a question set that hands over a distinctive term in ten cases out of twelve.** It is
not an estimate of what this engine would do on a repository whose history somebody else wrote, and
it should not be read as one. Getting a number that can be read that way is
[issue #4](https://github.com/leonardoprimero/bruriah/issues/4), and it needs an external corpus and
a question set large enough that the table above stops flipping.

## Two repositories nobody here wrote, measured 2026-07-27

Everything else on this page is this project measuring itself with questions its own author wrote.
This section is not, and it is the least flattering thing here.

**The corpora.** `square/leakcanary` (604 documents from 1736 commits) and `emilk/egui` (2119 from
4366), both derived by the shipped `bruriah corpus` and indexed by the shipped `bruriah index`.
Chosen by a query rather than from memory: permissive licence, not archived, not a fork, 2000–30000
stars, a real language, at least five releases. That release filter replaced a first rule sorting on
stars alone, which returned content collections — `public-apis`, `TheAlgorithms` — whose commits are
"add X to list" and carry no reasoning by construction. Reasoning coverage across the first twelve
candidates in the rule's own order has a **median of 23%**. This repository is at 100%, which is not
a normal number and is worth knowing before reading anything below.

**The questions.** 236, and **not one written by anybody who knew the answer**: each is a GitHub
issue title that a commit's body says it closes, where the issue came from a different person,
before that commit existed. `evals/retrieval/pairs.py` decides that. The sets are
[`leakcanary-issues.jsonl`](leakcanary-issues.jsonl) (153) and [`egui-issues.jsonl`](egui-issues.jsonl)
(83); the 27 excluded by shape rules ship beside them with the rule that excluded each.

### The result

recall@3, both models, both corpora:

| | leakcanary (604 docs) | egui (2119 docs) |
|---|---|---|
| default `MiniLM` | 0.261 | 0.434 |
| `jina-v2-base-es` | **0.340** | **0.530** |

Against 0.83 on this repository. A foreign history is harder — and **"how well this does on someone
else's repository" is not one number.** The two differ by more than 2×, and egui scores higher while
carrying three and a half times more distractors. Restricting egui's ranked lists to a random
604-document subset, to match leakcanary's corpus size, widens it further: 0.627 and 0.711.

### Why they differ, which is not what the headline suggests

egui's questions **give away more**:

| | mean `share` | mean `peak` | `peak` ≥ 0.50 |
|---|---|---|---|
| this repository's own 12 | 0.577 | 0.589 | 83% |
| leakcanary's 153 | 0.250 | 0.440 | 44% |
| egui's 83 | 0.450 | 0.565 | **69%** |

And once that is held fixed, most of the gap disappears. Splitting each set by leakage and comparing
band against band, with jina:

| | `peak` ≥ 0.50 | `peak` < 0.50 |
|---|---|---|
| leakcanary | 0.662 (n=68) | 0.082 (n=85) |
| egui | 0.649 (n=57) | 0.269 (n=26) |

**The high-leakage bands are near-identical**, with egui carrying 3.5× the distractors. So the
headline difference is largely the composition of the question set rather than the engine's
behaviour — which means comparing corpora by headline recall misleads, and only a leakage figure
makes the honest comparison possible at all.

### What holds across both, and what does not

**The direction holds.** In both corpora and under both models, questions sharing no distinctive
term with their answer score far worse: 8.8× and 8.1× on leakcanary, 5.0× and 2.4× on egui. A better
embedding lifts both bands and does not remove the dependence, which rules out blaming the default
model.

**The magnitude does not.** leakcanary's low-leakage band reaches 0.059 with the default model;
egui's reaches 0.269 with jina, more than four times better. Stated from leakcanary alone, "without
lexical overlap the right document is found six times in a hundred" would have been a claim about
one repository dressed as a finding. It is why a second corpus came before anything else.

### Four controls, so this is a measurement and not an anecdote with decimals

Measured on leakcanary:

| control | effect on recall@3 |
|---|---|
| embedding model, default → jina | +0.08 |
| distractors, 604 → 147 documents | +0.15 *(upper bound — restricts the ranked list, does not re-index)* |
| ground-truth document length, shortest → longest quartile | +0.09 |
| passages per document | **none**: 2.00 here, 2.02 there |

Document length matters because these corpora's documents are far shorter than this repository's —
median 445 and 497 characters against 1600 — so there is less reasoning per commit to match against.
It explains less than it looks like it should. None of the four reaches the within-corpus leakage
split, which is measured on one corpus, one question set, and both models at once.

### Two things this is not

**Not comparable with the 0.83.** The question sets measure different tasks. This page's twelve ask
for a decision's rationale (*why is the skill ceiling five*); these ask about a symptom
(*ToastEventListener leak*). Retrieving a commit from a why-question and retrieving it from the bug
that provoked it are different jobs, and there is a case the symptom is the more faithful one —
nobody asks why a ceiling is five out of nowhere; they hit it, and then they ask. Separate tables on
purpose.

**Not a verdict on the engine.** recall@10 sits well above recall@3 in both corpora — 0.451 against
0.340, and 0.723 against 0.530 — so the right document is *found* considerably more often than it is
*ranked* well. That is a ranking problem, and the same shape as the fusion defect recorded further
down this page, where reciprocal-rank fusion averaged a correct vector answer against a lexical leg
that could not read the query's language. That defect had a number to beat. Now this one has two,
from different corpora, which is what makes it safe to work against without fitting to either.

One finding is positive: jina improves on both foreign corpora. The claim below that the model
matters more than the weighting was, until this, one repository's opinion of itself.

## The model matters more than the weighting, measured 2026-07-26

`--model` has always been a flag on `bruriah index`, and nothing said what it was worth. It is
worth a great deal — more than the fusion fix that preceded it — and the shipped default is not
the best choice for every corpus. 218 passages from this repository's history, twelve questions
per language, same corpus build for every row.

| model | size | dim | en recall@3 | en MRR@10 | es recall@3 | es MRR@10 |
|---|---|---|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` *(default)* | 0.22 GB | 384 | 0.833 | 0.705 | 0.583 | 0.463 |
| `jina-embeddings-v2-base-es` | 0.64 GB | 768 | **0.917** | **0.861** | **0.750** | **0.667** |
| `paraphrase-multilingual-mpnet-base-v2` | 1.0 GB | 768 | 0.500 | 0.480 | 0.250 | 0.219 |

Spanish recall@3 goes 58% → 75%, which the main README had called the vector leg's own ceiling.
It was not a ceiling; it was this model's ceiling.

**Read the per-question results before believing the averages.** Twelve questions means one
question is eight points, so an average can move on luck. In Spanish, `jina` wins w01, w04 and w07
and *loses* w03 — three independent gains and one real regression, not one lucky question. The
firmer signal is MRR@10 (0.463 → 0.667): it measures where the right document ranks rather than
whether it crossed a threshold, so it moves less on noise, and it improved even where both models
already hit.

**Do not read this as "change the default."** `jina-embeddings-v2-base-es` is a Spanish-English
*bilingual* model, and this corpus is 95% English queried in Spanish — precisely what it was built
for. A German or Japanese corpus would likely do worse on it than on the multilingual default,
which is why the default stays multilingual and this stays a documented choice instead of a silent
one. Pick with `bruriah index --model`, and re-index: the embedding identity is pinned in the
snapshot, so a mismatched query embedder fails closed rather than quietly searching the wrong space.

`intfloat/multilingual-e5-large` is deliberately **absent** from that table. It expects
`query:`/`passage:` prefixes and this pipeline embeds both identically, so running it here would
measure the pipeline's omission and report it as the model's quality. Giving the pipeline a
per-model contract would make that comparison possible — but note that the gain above needed no
such contract, so the contract is a prerequisite for *more* candidates, not for the improvement
itself.

## The obvious next model is worse, measured 2026-07-26

The README says closing the cross-lingual gap "needs a better multilingual signal, not better
weighting". That is a hypothesis about the embedding, and it is cheap to test: same corpus, same
questions, same scoring, one variable changed. So it was tested, on 216 passages from this
repository's history.

| model | size | dim | en recall@3 | en MRR@10 | es recall@3 | es recall@10 | es MRR@10 |
|---|---|---|---|---|---|---|---|
| `paraphrase-multilingual-MiniLM-L12-v2` *(shipped)* | 0.22 GB | 384 | **0.833** | **0.705** | **0.583** | **0.917** | **0.463** |
| `paraphrase-multilingual-mpnet-base-v2` | 1.0 GB | 768 | 0.500 | 0.480 | 0.250 | 0.333 | 0.219 |

Five times the download, half the recall, and Spanish recall@10 collapses from 92% to 33%. The
larger sibling from the same family, same training objective, same mean pooling — and both emit
comparably scaled vectors (‖v‖ 2.91 and 2.45), so this is not a normalisation artefact hiding a
good model.

**What this does and does not establish.** It does not say mpnet is a worse encoder; it says the
model is **not a drop-in variable in this pipeline**. Queries and passages are embedded
identically, with no prefix and no per-model normalisation, so any model whose contract expects
something else — `intfloat/multilingual-e5-large` wants `query:` / `passage:` prefixes, for
instance — will be measured unfairly by exactly this experiment. That is the reason e5 was *not*
run: a number produced by a knowingly misconfigured pipeline is worse than no number.

So the open work is not "find a better multilingual model". It is "give the pipeline the per-model
contract that would let a fair comparison happen at all" — and only then compare. Twelve questions
per language remains the sample size; treat the direction as established and the figures as
indicative, as everywhere else here.

Reproduce it with `evals/retrieval/metrics.py` and the recipe above, indexing once per model
against one corpus build.

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
