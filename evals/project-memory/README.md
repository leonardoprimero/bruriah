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

## Reranking, measured 2026-07-27 — and how much of the loss is really ranking

The paragraph above says the ranking gap is the distance between recall@3 and recall@10. That
understates it by half, and the correction came from measuring the ceiling instead of inferring it.

Ask how often the correct document is **anywhere** in the pool `search` returns, rather than in its
top ten. Reproduce with no reranker and no new model — the pool is just `Budgets(max_candidates=200)`:

| | recall@3 | in the top 10 | in the top 40 documents | **anywhere in the pool** |
|---|---|---|---|---|
| leakcanary (604 docs) | 0.340 | 0.451 | 0.588 | **0.758** |
| egui (2119 docs) | 0.530 | 0.723 | 0.855 | **0.916** |

**Retrieval finds the right document three times in four, and nine times in ten. The ordering then
throws it away.** Everything on this page about a "ranking problem" was true and too modest.

### What a cross-encoder recovers

`search` takes an optional `rerank` callable, off unless an operator names one with `--reranker`.
It scores the top 40 **documents** and reorders them; the tail keeps retrieval's own order, so a
reranker can only reorder what was already found and never invent a result.

| | recall@3 | recall@10 | MRR@10 | ceiling at this depth |
|---|---|---|---|---|
| leakcanary, shipped ranking | 0.340 | 0.451 | 0.290 | — |
| leakcanary, `jina-reranker-v2` | **0.431** | **0.516** | **0.392** | 0.588 |
| egui, shipped ranking | **0.530** | **0.723** | 0.443 | — |
| egui, `jina-reranker-v2` | 0.494 | 0.675 | 0.445 | 0.855 |

**It is worth fourteen questions on leakcanary and costs three on egui**, and the second number is
the one to read first. On egui the reranker lowers recall@3 *and* recall@10 while leaving MRR flat:
it is not noise around zero, it is a small consistent loss, on the corpus where the shipped ranking
was already strongest.

### Depth amplifies the direction, it does not improve it

The obvious hypothesis is that egui was simply not reranked deeply enough. It was measured:

| | top 20 | top 40 *(shipped)* | no reranker |
|---|---|---|---|
| leakcanary | 0.412 | **0.431** | 0.340 |
| this repository, English | 0.917 | **1.000** | 0.833 |
| egui | 0.518 | **0.494** | 0.530 |

**Deeper is better where the reranker helps and worse where it hurts.** That is the opposite of a
depth problem — more candidates do not give a struggling reranker more to work with, they give it
more opportunities to move the right document down. An earlier draft of this page said depth helped
everywhere it was tried, on two corpora out of three. The third one is why that sentence is gone.

So the rule this eval supports is narrower than "reranking helps", and narrower than the tidy story
that it helps where retrieval is weak — the internal English set began at 0.833, the strongest
baseline measured, and still went to 1.000. Four question sets: three gained, one lost, and no
explanation available from four sets is worth stating as a mechanism. **Measure it on your own
corpus before turning it on**, which is a real instruction here because the flag makes that a
one-command experiment and no re-index.

### What reranking actually does, one question at a time, measured 2026-07-28

Everything above this line is a corpus average, and the paragraph above admits what that costs:
four question sets, three gained, one lost, and no mechanism worth stating. Averages cannot supply
one — four numbers are four points. So the same runs were scored per question and the ranks
committed, in [`leakcanary-ablation.jsonl`](leakcanary-ablation.jsonl) and
[`egui-ablation.jsonl`](egui-ablation.jsonl). 236 questions, the rank the correct document held with
the reranker off and with it on, produced by `evals/retrieval/run_ablation.py` against the default
MiniLM index and read by `report_ablation.py` with no model and no index at all.

**Read `vs null`, not `helped`/`hurt`.** Bucketing by the rank the shipped ranking gave a document
is a biased selection: one at rank 1 can only move down, one at rank 40 can only move up, and a
reranker scoring by coin flip reproduces exactly the shape a reader calls "it hurts where the
ranking was strong". `vs null` is positions better than a uniform shuffle of the same 40-document
head would have managed — the closed form is in `ablation.py`, and a test asserts a skill-free
reranker scores as skill-free.

Both corpora pooled:

| rank before | n | helped | hurt | same | mean rank after | shuffle gives | **vs null** | recall@3 |
|---|---|---|---|---|---|---|---|---|
| 1 | 50 | 0 | 11 | **39** | 2.84 | 20.50 | **+17.66** | 1.000 -> 0.920 |
| 2-3 | 26 | 18 | 4 | 4 | 2.00 | 20.50 | **+18.50** | 1.000 -> 0.846 |
| 4-10 | 37 | 25 | 11 | 1 | 4.65 | 20.50 | **+15.85** | 0.000 -> 0.568 |
| 11-40 | 41 | **32** | 9 | 0 | 11.44 | 20.50 | **+9.06** | 0.000 -> 0.366 |
| 41+ | 25 | 0 | 0 | 25 | 63.96 | 63.96 | 0.00 | 0.000 -> 0.000 |

**The stage is a trade, not a lift.** It leaves 39 of 50 already-correct top answers where they
were and costs 11 of them; it pulls 32 of 41 documents up out of ranks 11-40. Where the top is
nearly empty the trade is a large net gain, and where the top is already full there is more to lose
than to win. That is a mechanism, and it is what the four corpus averages were the shadow of.

It also disposes of the reading those averages invited. The reranker does not destroy strong
rankings: given 50 documents the shipped ranking had already placed first, it moved 11 — against a
null that would have moved essentially all of them. **`41+` is the row worth staring at.** Twenty-
five questions whose answer sat past the reranked head did not move by a single position, because
`_rerank_fused` reorders the head and carries the tail untouched. Reranking is not retrieval and
cannot become it.

**And it is not free even when it wins.** Across the 236, the answer set shrank from a mean of 181
documents to 88, and **twelve questions had their correct document returned by the shipped ranking
and not returned at all once reranked — none the other way.** The fused order ranks passages, so
its head is drawn from many documents at one passage each; `_rerank_fused` groups every passage of
a document together, so the same budget buys half as many distinct documents. At the shipped
20,000-character `max_extracted_chars` the count is fourteen rather than nine, so this is worse at
the default than at the ceiling these figures were measured at. The claim that reranking "changes
the order of the evidence and never its membership" is true of `_rerank_fused` and false of
`search`, which truncates afterwards.

**What this does NOT support**, stated because the shape of the pooled table invites it: that gain
is predictable from baseline strength. It is tempting — the four foreign-corpus configurations run
+0.164, +0.091, +0.036, -0.036 as the baseline rises from 0.261 to 0.530, which looks like a
ceiling near 0.45. The internal English set refutes it. That set begins at 0.833, the strongest
baseline on this page, and still reaches 1.000. Two of those four rows are also published figures
from a `jina-v2-base-es` index rather than measurements taken here. The mechanism above is measured
on 236 questions; the ceiling is a line drawn through four points with a counterexample already on
the page, and it is recorded here as a thing to test rather than a thing to believe.

### The unit matters more than the size of the model

The first attempt reranked **passages** and was nearly worthless. A passage here is roughly 250
characters of a commit body whose median length is 445 and 497 in these two corpora, so a
cross-encoder was being handed half a commit and asked whether it answered a bug report:

| what the cross-encoder was given | model | size | leakcanary recall@3 |
|---|---|---|---|
| passages | `ms-marco-MiniLM-L-6` | 0.08 GB | 0.359 *(recall@10 falls to 0.425)* |
| passages | `jina-reranker-v2` | 1.11 GB | 0.373 |
| **whole documents** | `ms-marco-MiniLM-L-6` | 0.08 GB | 0.373 |
| **whole documents** | `jina-reranker-v2` | 1.11 GB | **0.412** *(at top 20)* |

The 0.08 GB model reading whole documents matches the 1.11 GB model reading passages. Fourteen
times the download bought what changing the input unit bought for free. This is the same shape as
"the model matters more than the weighting" below, one level further in: the model matters more
than the weighting, and what you feed the model matters more than the model.

### An English-only reranker is not neutral on Spanish — it is destructive

The cheap model is English-only, and the tempting reading of the table above is that it is nearly
as good for a fraction of the size. On the twelve Spanish questions, against a 159-document corpus
of this repository's own history:

| | recall@3 | recall@10 | MRR@10 |
|---|---|---|---|
| shipped ranking | 0.500 | 0.917 | 0.454 |
| `ms-marco-MiniLM-L-6` (English-only, top 20) | 0.583 | **0.667** | 0.507 |
| `jina-reranker-v2` (multilingual) | **0.917** | 0.917 | **0.819** |

**A model that cannot read the query still returns confident scores, and reordering by them
destroyed a quarter of the recall@10 the engine already had.** That is the reason the flag takes a
model name and ships with no default: there is no safe default across languages, and a silent one
would have made this failure invisible.

The same stage takes the twelve **English** questions from 0.833 to **1.000** — all twelve — with
MRR@10 going 0.637 to 0.847. So Spanish does not reach parity: it lands one question short of an
English set that the reranker also improved. What closed is most of the gap, not the gap.

Two paragraphs further down this page say 58% is "the vector leg's own ceiling" and that the fusion
fix "does not make cross-lingual retrieval as good as same-language retrieval." Both were true of
the pipeline that existed when they were written. Neither survives this, and it is the second time
a number on this page called a ceiling turned out to be a property of one replaceable component.

Twelve questions, and one of them is eight points: read that 1.000 as "nothing left to find in a
twelve-question set", which is a statement about the set.

### How these numbers were produced, which is not uniform and should not be hidden

Two harnesses produced this section, and saying which did what is the difference between a
measurement and a number.

The **internal twelve-question rows** come from calling the shipped `search(..., rerank=...)`
directly, through the deps `bruriah serve` builds, with the shipped depth. Nothing was reordered
outside the product.

The **exploratory rows** — every reranker and depth that was tried and discarded, including the
passage-level table above — come from an offline harness that dumps the ranked pool once and
reorders it, because rerunning retrieval for every candidate would have taken hours. That harness
earns its place by reproducing every previously published figure on this page **exactly**:
leakcanary 0.340 / 0.451 / 0.662 (n=68) / 0.082 (n=85), egui 0.530 / 0.723 / 0.649 (n=57) /
0.269 (n=26), and 0.833 on the internal English set.

**Every reranked row in the result table above was then re-derived through the shipped `search()`**,
at the shipped depth, and the leakcanary figures agree with the offline harness digit for digit
(0.431 / 0.516 / 0.392, bands 0.824 and 0.118). Two independent paths, same numbers, so neither is
a private convention of one script.

That re-derivation is also what produced the egui loss. The offline exploration had egui at depth 20
reading 0.518, close enough to the 0.530 baseline to write off as one question; running the shipped
configuration turned it into 0.494 and three questions. The number that went into this page is the
one from the code that ships, and it is worse than the one exploration suggested.

### What this costs, and the caveats that do not go away

A cross-encoder pass per candidate document is by far the most expensive thing in a request, and
depth is the whole cost: 40 documents is 40 forward passes before a single result is returned.
`search` drops the stage entirely once `max_elapsed_ms` has passed rather than overrunning it, and
degrades to the shipped ranking — a measured 0.340, not nothing — on every failure a supplied model
can produce, disclosing which one in `degradation`.

**Twelve questions is still twelve questions.** The Spanish result is five questions moving, and
the English one is a single question plus an MRR gain. The foreign-corpus figures rest on 153 and
83 questions written by people who did not know the answer, which is why they lead. The internal
Spanish baseline reads 0.500 here rather than the 0.583 published further down because this corpus
has grown to 159 documents from 147 — one question out of twelve, which is exactly what the sample
size warning on this page has always meant.

**Not measured:** any language other than English and Spanish, and any reranker outside the two
named. The ceiling table says 0.758 is available on leakcanary and reranking the whole
200-candidate pool would be the way to chase it — but the depth table above is a reason to expect
less from that than the ceiling suggests, since on egui more depth made things worse rather than
better. Nobody has run it, and the latency is why it is not the default in any case.

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
