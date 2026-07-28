#!/usr/bin/env python3
"""Runs every question twice through the SHIPPED `search()` -- once without a reranker, once with
one -- and writes the rank the correct document held under each, one line per question.

WHY THIS EXISTS. `evals/project-memory/README.md` publishes one recall figure per corpus per
condition. Those averages cannot answer when reranking helps, because two corpora are two points.
Worse, every previous run computed per-question ranks, printed an average, and threw the ranks
away -- so each new question about the same runs cost another full re-measurement. This writes the
per-question ranks to disk so the next question is arithmetic.

THE GUARD THAT MATTERS. `search` drops the reranking stage and degrades to the shipped ranking
whenever the elapsed budget is gone, disclosing it in `degradation` rather than failing. The
default budget is 10s and a cross-encoder pass over 40 documents does not fit in it, so a naive run
here would compare the shipped ranking against ITSELF and report a confident "no effect". Every
reranked search is therefore asserted to carry a `reranked:` marker, and the run aborts rather than
publishing a number it did not measure.

Usage:
    python evals/retrieval/run_ablation.py \
        --corpus leakcanary --data-dir /tmp/data-leakcanary \
        --questions evals/project-memory/leakcanary-issues.jsonl \
        --reranker jinaai/jina-reranker-v2-base-multilingual \
        --out evals/project-memory/leakcanary-ablation.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import QuestionOutcome, document_ranking, rank_of, summarize  # noqa: E402

from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.contracts import Budgets  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import _RERANK_DEPTH, search  # noqa: E402

# 200 is the ceiling `Budgets` allows and the pool the published ceiling table was measured at. It
# changes no recall@3 -- `max_candidates` truncates the tail, never the head -- and buys the deep
# ranks the bucket analysis needs. 120_000ms is likewise the ceiling: the point is to measure the
# reranker, not to measure the timeout.
_BUDGETS = Budgets(max_candidates=200, max_elapsed_ms=120_000)


def load_questions(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _ranking(deps, query: str, rerank) -> tuple[list[str], tuple[str, ...]]:
    outcome = search(
        deps.snapshot, query, _BUDGETS,
        embed_query=deps.embed_query, rerank=rerank, clock=deps.clock,
    )
    return document_ranking([match.relative_path for match in outcome.matches]), outcome.degradation


def already_scored(path: Path) -> dict[str, QuestionOutcome]:
    """Rows a previous run of this exact command already wrote.

    Every row is appended the moment it is computed and this reads them back, so an interrupted run
    resumes instead of restarting. That is not a convenience: at ~20 seconds a question, a run that
    keeps its results only in memory converts any interruption into a total loss, which is the
    failure this whole artifact exists to stop happening to future questions.
    """
    if not path.exists():
        return {}
    scored: dict[str, QuestionOutcome] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # A process killed mid-append leaves a partial final line. Every line before it is a
            # real measurement and refusing to read them would discard exactly what appending was
            # for; the torn question simply gets measured again.
            print(f"  ignoring a torn line in {path.name}; its question will be rescored", flush=True)
            continue
        scored[payload["id"]] = QuestionOutcome(**payload)
    return scored


def run(corpus: str, questions: list[dict], deps, out: Path) -> list[QuestionOutcome]:
    resumed = already_scored(out)
    if resumed:
        print(f"resuming: {len(resumed)} of {len(questions)} already scored", flush=True)
    outcomes: list[QuestionOutcome] = []
    started = time.monotonic()
    for position, case in enumerate(questions, start=1):
        if case["id"] in resumed:
            outcomes.append(resumed[case["id"]])
            continue
        query = case["query"]
        ground_truth = case["ground_truth"]["must_include"][0]

        base_order, _base_degradation = _ranking(deps, query, None)
        reranked_order, reranked_degradation = _ranking(deps, query, deps.rerank)

        # The whole run is worthless if this is not true, so it is checked per question rather than
        # sampled: a budget that silently ate the stage would look exactly like a reranker with no
        # effect, and the second is a finding while the first is a bug.
        if not any(marker.startswith("reranked:") for marker in reranked_degradation):
            raise SystemExit(
                f"{case['id']}: the reranking stage did not run -- degradation={reranked_degradation}. "
                "Nothing below this point would be a measurement of the reranker."
            )

        outcome = QuestionOutcome(
            id=case["id"], corpus=corpus,
            base_rank=rank_of(base_order, ground_truth),
            reranked_rank=rank_of(reranked_order, ground_truth),
            pool_documents=len(base_order),
            rerank_depth=_RERANK_DEPTH,
        )
        outcomes.append(outcome)
        with out.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(asdict(outcome), sort_keys=True) + "\n")
        if position % 10 == 0 or position == len(questions):
            elapsed = time.monotonic() - started
            print(f"  {position}/{len(questions)}  {elapsed:.0f}s elapsed", flush=True)
    return outcomes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="name recorded on every row")
    parser.add_argument("--data-dir", type=Path, required=True, help="data dir holding the snapshot")
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--reranker", required=True, help="cross-encoder model name")
    parser.add_argument("--out", type=Path, required=True, help="per-question JSONL to write")
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    questions = load_questions(args.questions)
    paths = resolve_paths(cli_data_dir=args.data_dir, env={})
    deps = build_serve_deps(paths, reranker_model=args.reranker)
    if deps.rerank is None:
        raise SystemExit("no reranker was constructed; there is nothing to ablate")

    print(f"{args.corpus}: {len(questions)} questions, snapshot {deps.snapshot.build_id}", flush=True)
    try:
        outcomes = run(args.corpus, questions, deps, args.out)
    finally:
        deps.snapshot.database.close()

    # Rewritten in question order once complete. The appended file is already correct and complete;
    # this only makes the committed artifact's order independent of how many times it was resumed.
    args.out.write_text(
        "\n".join(json.dumps(asdict(outcome), sort_keys=True) for outcome in outcomes) + "\n",
        encoding="utf-8",
    )
    summary = summarize(args.corpus, outcomes)
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
