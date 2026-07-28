#!/usr/bin/env python3
"""How deep you would have to look before the correct document appears, with no budget in the way.

    python evals/retrieval/report_reach.py --corpus leakcanary --data-dir /tmp/data-leakcanary \
        --questions evals/project-memory/leakcanary-issues.jsonl

WHY THIS CANNOT USE `search`. `Budgets.max_candidates` tops out at 200 passages, so `search` can
only ever report that the answer was NOT in what it returned. That reads like a retrieval failure
and has so far always been an ordering one -- on both foreign corpora the fused order ranks the
correct document every single time, at a median of 26 of 604 documents on leakcanary and 4 of 2120
on egui. A page that says "not in the pool" without this distinction invites work on the wrong
problem, which is why the figure is worth a script rather than an assumption.

It therefore walks the same legs `search` walks, through its private helpers, and reads the fused
order before truncation. Reaching into `_fuse`/`_bm25_ranks`/`_vector_ranks` is deliberate and is
the only way to see past a public ceiling: the alternative is inferring the shape of the ranking
from the part of it that fits in a budget, which is the inference this script exists to replace.
Being private, they can change -- and this file is the thing that should then fail loudly rather
than a paragraph in a README quietly going stale.

No reranker and no cross-encoder: this is the cheap half of the pipeline, minutes rather than the
hour `run_ablation.py` costs.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import Reach, reach, strip_build_sha  # noqa: E402

from bruriah import language  # noqa: E402
from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import (  # noqa: E402
    _CROSS_LINGUAL_LEXICAL_WEIGHT, _bm25_ranks, _corpus_language, _fuse, _scan_passages,
    _tokenize, _vector_ranks,
)

_CEILINGS = (3, 10, 40, 183, 300, 604, 2120)
_NO_DEADLINE = float("inf")


def _never_expires() -> float:
    return 0.0


def document_rank_of_answer(passages, by_ref, corpus_language, deps, query: str, truth: str) -> int | None:
    """1-indexed position of `truth` in the untruncated fused DOCUMENT order, or `None`."""
    lexical, _ = _bm25_ranks(passages, _tokenize(query), _NO_DEADLINE, _never_expires)
    vector, _ = _vector_ranks(passages, deps.embed_query(query), _NO_DEADLINE, _never_expires)

    # The same discount `search` applies, restated rather than imported: a reach figure measured
    # under a different ranking rule than the one that ships would not describe the shipped engine.
    weight = 1.0
    query_language = language.detect(query)
    if query_language is not None and corpus_language is not None and query_language != corpus_language:
        weight = _CROSS_LINGUAL_LEXICAL_WEIGHT

    seen: set[str] = set()
    for ref, _lexical_rank, _vector_rank in _fuse(lexical, vector, weight):
        name = strip_build_sha(by_ref[ref].relative_path.rsplit("/", 1)[-1])
        if name not in seen:
            seen.add(name)
            if name == truth:
                return len(seen)
    return None


def render(corpus: str, result: Reach) -> str:
    lines = [
        f"## {corpus}",
        "",
        f"{result.questions} questions over {result.total_documents} documents. "
        f"**Never ranked at all: {result.never_ranked}.** "
        f"Median rank of the correct document: **{result.median_rank}**.",
        "",
        "| look this deep | answers found | share |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| top {ceiling} | {found} / {result.questions} | **{share:.3f}** |"
        for ceiling, found, share in result.within
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None, help="per-question ranks, as JSONL")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    questions = [
        json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    deps = build_serve_deps(resolve_paths(cli_data_dir=args.data_dir, env={}))
    try:
        passages, _stopped = _scan_passages(deps.snapshot.database, _NO_DEADLINE, _never_expires)
        by_ref = {passage.ref: passage for passage in passages}
        corpus_language = _corpus_language(passages)
        rows = [
            {
                "id": case["id"],
                "document_rank": document_rank_of_answer(
                    passages, by_ref, corpus_language, deps, case["query"],
                    strip_build_sha(case["ground_truth"]["must_include"][0]),
                ),
            }
            for case in questions
        ]
    finally:
        deps.snapshot.database.close()

    result = reach([row["document_rank"] for row in rows], _CEILINGS,
                   len({passage.document_ref for passage in passages}))
    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    print(json.dumps(asdict(result), indent=2, sort_keys=True) if args.json else render(args.corpus, result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
