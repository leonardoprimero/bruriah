#!/usr/bin/env python3
"""Paired per-question comparison of two `document_rank`/`rank` files: whether a candidate corpus
moves the correct document's rank for the SAME questions the baseline was scored on, not just
whether the two averages differ.

    python evals/retrieval/report_paired.py --corpus leakcanary \
        --baseline evals/project-memory/leakcanary-embedder-ablation-jina.jsonl \
        --candidate /tmp/ranks-leakcanary-github-jina.jsonl

WHY PAIRED, NOT TWO INDEPENDENT AVERAGES. Two recall@3 figures on the same 153 questions can move
for reasons that have nothing to do with the change under test -- sampling noise in which
questions happen to sit near the top-3 boundary. A paired comparison asks the narrower question a
single average cannot: for each question, did adding the candidate's documents to the corpus move
IT, specifically, across the boundary a reader cares about (rank <= 3)? McNemar's exact test scores
exactly that: of the questions whose top-3 membership changed, how many entered versus left,
against the null that a real effect is equally likely to move a question either way. This is the
same statistic `evals/project-memory/README.md`'s 2026-09-20 embedder ablation used, restated here
as a reusable script instead of a one-off measurement.

WHY BOTH `rank` AND `document_rank`. `report_reach.py` (this script's sibling) writes
`{"id", "document_rank"}` rows via its own `--out`; the embedder-ablation baselines already
published in `evals/project-memory/` were written by a different script and carry `{"id", "rank"}`
instead. Rather than require one shape or a conversion step before every comparison, `load_ranks`
accepts either key, so this script scores a baseline/candidate pair regardless of which report
produced either side.

Reads two already-computed rank files; it does not build or query an index itself, and performs no
network access.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# The rank buckets a reader actually distinguishes: whether the answer was on the first screen
# (1-3), still reachable with one more scroll (4-10), technically present but not really found
# (11-40), or effectively absent (41+ or `None`).
_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("1-3", 1, 3),
    ("4-10", 4, 10),
    ("11-40", 11, 40),
    ("41+", 41, 1 << 30),
)


def load_ranks(path: Path) -> dict[str, int | None]:
    """`{"id", "rank"}` or `{"id", "document_rank"}` rows -> `{id: rank}`. Whichever key is absent
    is treated as absent from the *other* report, never as an error: the two report scripts this
    module compares were written independently and happen to have chosen different key names for
    the same quantity."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["id"]: row.get("rank", row.get("document_rank")) for row in rows}


def recall_at(ranks: list[int | None], k: int) -> float:
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def mrr_at(ranks: list[int | None], k: int) -> float:
    return sum(1.0 / rank for rank in ranks if rank is not None and rank <= k) / len(ranks)


def exact_mcnemar_p(entered: int, left: int) -> float:
    """Two-sided exact McNemar (sign test over the discordant top-3 pairs). `entered` is the count
    of questions that moved INTO the top 3; `left` is the count that moved OUT. Exact rather than
    the chi-squared approximation because the discordant counts here are small (single digits to
    low tens), where the approximation is unreliable."""
    n = entered + left
    if n == 0:
        return 1.0
    k = min(entered, left)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def _bucket(rank: int | None) -> str:
    if rank is None:
        return "41+"
    for name, low, high in _BUCKETS:
        if low <= rank <= high:
            return name
    return "41+"  # pragma: no cover -- `_BUCKETS` already covers every positive int


@dataclass(frozen=True)
class PairedComparison:
    """One corpus's paired baseline-vs-candidate comparison, computed once over the whole
    question set."""

    n: int
    baseline_recall_at_3: float
    baseline_recall_at_10: float
    baseline_mrr_at_10: float
    baseline_absent: int
    candidate_recall_at_3: float
    candidate_recall_at_10: float
    candidate_mrr_at_10: float
    candidate_absent: int
    entered_top3: int
    left_top3: int
    mcnemar_p: float
    unchanged: int


def compare(
    baseline: dict[str, int | None], candidate: dict[str, int | None],
) -> tuple[PairedComparison, list[dict[str, Any]]]:
    """The aggregate comparison, plus one movement row per question, in `baseline`'s order.

    Both files must score the identical question set -- a baseline missing a question the
    candidate answers (or vice versa) would silently drop it from every recall figure below,
    which is a worse failure than refusing to compare mismatched files."""
    if baseline.keys() != candidate.keys():
        only_baseline = sorted(baseline.keys() - candidate.keys())
        only_candidate = sorted(candidate.keys() - baseline.keys())
        raise ValueError(
            "baseline and candidate must score the same question ids; "
            f"only in baseline: {only_baseline}, only in candidate: {only_candidate}"
        )

    ids = list(baseline)
    base_ranks = [baseline[i] for i in ids]
    cand_ranks = [candidate[i] for i in ids]

    entered = sum(
        1 for b, c in zip(base_ranks, cand_ranks)
        if (b is None or b > 3) and (c is not None and c <= 3)
    )
    left = sum(
        1 for b, c in zip(base_ranks, cand_ranks)
        if (b is not None and b <= 3) and (c is None or c > 3)
    )
    unchanged = sum(1 for b, c in zip(base_ranks, cand_ranks) if b == c)

    result = PairedComparison(
        n=len(ids),
        baseline_recall_at_3=recall_at(base_ranks, 3),
        baseline_recall_at_10=recall_at(base_ranks, 10),
        baseline_mrr_at_10=mrr_at(base_ranks, 10),
        baseline_absent=sum(1 for rank in base_ranks if rank is None),
        candidate_recall_at_3=recall_at(cand_ranks, 3),
        candidate_recall_at_10=recall_at(cand_ranks, 10),
        candidate_mrr_at_10=mrr_at(cand_ranks, 10),
        candidate_absent=sum(1 for rank in cand_ranks if rank is None),
        entered_top3=entered,
        left_top3=left,
        mcnemar_p=exact_mcnemar_p(entered, left),
        unchanged=unchanged,
    )
    rows = [
        {
            "id": question_id, "baseline_rank": b, "candidate_rank": c,
            "baseline_bucket": _bucket(b), "candidate_bucket": _bucket(c),
        }
        for question_id, b, c in zip(ids, base_ranks, cand_ranks)
    ]
    return result, rows


def render(corpus: str, result: PairedComparison) -> str:
    lines = [
        f"## {corpus}",
        "",
        f"n={result.n}",
        f"baseline    recall@3={result.baseline_recall_at_3:.3f} "
        f"recall@10={result.baseline_recall_at_10:.3f} MRR@10={result.baseline_mrr_at_10:.3f} "
        f"absent={result.baseline_absent}",
        f"candidate   recall@3={result.candidate_recall_at_3:.3f} "
        f"recall@10={result.candidate_recall_at_10:.3f} MRR@10={result.candidate_mrr_at_10:.3f} "
        f"absent={result.candidate_absent}",
        "",
        f"top-3 discordant: entered={result.entered_top3} left={result.left_top3} "
        f"p={result.mcnemar_p:.4f}",
        f"unchanged rank: {result.unchanged}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="per-question movement, as JSONL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    baseline = load_ranks(args.baseline)
    candidate = load_ranks(args.candidate)
    result, rows = compare(baseline, candidate)

    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8",
        )
    print(json.dumps(asdict(result), indent=2, sort_keys=True) if args.json else render(args.corpus, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
