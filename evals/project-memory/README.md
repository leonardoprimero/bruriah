# Project-memory retrieval eval

Twelve "why was this decided" questions about **this repository's own history**, each with the
commit that answers it as ground truth. The answers are known because the commits were written
deliberately: this repo is its own test case, the same way `verify_legacy_baseline.py` is.

## Reproduce

```bash
python scripts/git_corpus.py --repo .. --out /tmp/corpus
bruriah index --corpus-root /tmp/corpus --policy policy.yaml --data-dir /tmp/data
# then score search() against decisions-{es,en}.jsonl with evals/retrieval/metrics.py
```

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

The failure mode is visible in the results: the single long Spanish commit becomes an attractor for
Spanish queries through lexical overlap alone, outranking the correct English document. That is a
BM25 effect the reciprocal-rank fusion does not correct for.

Improving this is the next piece of retrieval work, and it now has a number to beat rather than an
impression to argue about.
