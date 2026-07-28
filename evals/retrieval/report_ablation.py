#!/usr/bin/env python3
"""Report where reranking helped and where it hurt, from the committed per-question artifacts.

    python evals/retrieval/report_ablation.py \
        --ablation evals/project-memory/leakcanary-ablation.jsonl \
        --ablation evals/project-memory/egui-ablation.jsonl

This needs NO index, NO embedding model and NO cross-encoder: it is arithmetic over ranks that were
already measured. That is the entire point of committing `*-ablation.jsonl`. Producing those files
costs about twenty seconds a question and 236 questions of wall-clock; every question asked of them
afterwards costs milliseconds, and a run whose per-question ranks are discarded charges the full
price again for the next question about the same runs.

READ THE `vs null` COLUMN, NOT `moved`. Questions are bucketed by the rank the shipped ranking gave
the correct document, which is a biased selection: a document at rank 1 can only move down and one
at rank 40 can only move up. A reranker assigning scores at random would produce a large negative
`moved` in the first bucket and a large positive one in the last. `vs null` subtracts that
arithmetic -- it is positions better than a uniform shuffle of the same head would have managed --
and only it says anything about the model. See `ablation.py` for the closed form.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ablation import AblationSummary, QuestionOutcome, summarize  # noqa: E402


def load(path: Path) -> list[QuestionOutcome]:
    return [
        QuestionOutcome(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _fmt(value: float | None, places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def render(summary: AblationSummary) -> list[str]:
    lines = [
        f"## {summary.corpus}",
        "",
        f"{summary.questions} questions, {summary.outside_pool} with the correct document outside "
        f"the pool entirely (counted as a miss below, excluded from every bucket).",
        "",
        f"recall@3: **{_fmt(summary.recall_at_3_before, 3)}** -> **{_fmt(summary.recall_at_3_after, 3)}**",
        "",
        f"documents returned: **{_fmt(summary.mean_pool_documents, 1)}** -> "
        f"**{_fmt(summary.mean_reranked_pool_documents, 1)}** on average. "
        f"**{summary.evicted}** questions had their correct document returned by the shipped "
        f"ranking and dropped by the reranked one; **{summary.rescued}** the other way.",
        "",
        "| base rank | n | helped | hurt | same | mean rank after | shuffle would give | **vs null** | recall@3 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for bucket in summary.buckets:
        beats = bucket.beats_null
        lines.append(
            f"| {bucket.bucket} | {bucket.n} | {bucket.helped} | {bucket.hurt} | {bucket.unchanged} | "
            f"{_fmt(bucket.mean_reranked_rank)} | {_fmt(bucket.mean_expected_rank)} | "
            f"**{'+' if beats is not None and beats > 0 else ''}{_fmt(beats)}** | "
            f"{_fmt(bucket.recall_at_3_before, 3)} -> {_fmt(bucket.recall_at_3_after, 3)} |"
        )
    lines.append("")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ablation", type=Path, action="append", required=True,
                        help="a *-ablation.jsonl artifact; repeat for several corpora")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    summaries: list[AblationSummary] = []
    pooled: list[QuestionOutcome] = []
    for path in args.ablation:
        outcomes = load(path)
        pooled.extend(outcomes)
        summaries.append(summarize(outcomes[0].corpus if outcomes else path.stem, outcomes))
    # The pooled row is the one with power. Per corpus the buckets thin out to a handful of
    # questions each; across both there are 236, which is the whole reason a second corpus exists.
    summaries.append(summarize("both corpora pooled", pooled))

    if args.json:
        print(json.dumps([summary.__dict__ for summary in summaries], default=lambda o: o.__dict__,
                         indent=2, sort_keys=True))
        return 0

    lines: list[str] = []
    for summary in summaries:
        lines.extend(render(summary))
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
