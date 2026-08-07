#!/usr/bin/env python3
"""Asks every question twice -- of the corpus that holds its answer, and of one that cannot.

    python evals/retrieval/run_separation.py \
        --index leakcanary /tmp/idx-leakcanary --index egui /tmp/idx-egui \
        --out evals/project-memory/separation-paired.jsonl

WHY PAIRED. An earlier run compared questions written by this repository's author against
questions written by strangers and found separation (AUC 0.856). That comparison cannot tell "has
an answer here" from "was written by someone who knows this corpus" -- the leakage confound that
already forced the retraction of 0.83 on this very page. Here the SAME question is asked of two
corpora. Same author, same words, same domain, same day; the only variable left is whether the
answer is present, so each question is its own control and the comparison can be a sign test.

WHY THE SCORING IS RESTATED HERE. `retrieval._bm25_ranks` and `_vector_ranks` compute the scores
this measurement needs and return only ranks, so there is nothing to read from outside. The two
loops below reproduce them, in the manner `report_reach.py` restates the cross-lingual discount
rather than importing it. A restatement that has drifted would measure a different engine, so the
run ASSERTS that its ranks equal the shipped helpers' ranks for every query, and aborts otherwise.
That guard is the reason these numbers can be quoted; without it they describe this file.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import strip_build_sha  # noqa: E402
from separation import separation  # noqa: E402

from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import (  # noqa: E402
    _BM25_B, _BM25_K1, _bm25_ranks, _ranked, _scan_passages, _tokenize, _vector_ranks,
)

_NO_DEADLINE = float("inf")
EVALS = ROOT / "evals" / "project-memory"


def _never_expires() -> float:
    return 0.0


def _bm25_scores(passages, query_tokens) -> list[tuple[float, str]]:
    tokenized = [_tokenize(passage.text) for passage in passages]
    lengths = [len(tokens) for tokens in tokenized]
    if not lengths:
        return []
    average_length = sum(lengths) / len(lengths)
    document_frequency: dict[str, int] = {}
    for tokens in tokenized:
        for term in set(tokens):
            document_frequency[term] = document_frequency.get(term, 0) + 1
    total_documents = len(tokenized)
    terms = set(query_tokens)
    scored: list[tuple[float, str]] = []
    for passage, tokens, length in zip(passages, tokenized, lengths, strict=True):
        if length == 0 or average_length == 0:
            continue
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        total = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            document_count = document_frequency.get(term, 0)
            idf = math.log(1 + (total_documents - document_count + 0.5) / (document_count + 0.5))
            denominator = frequency + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / average_length)
            total += idf * (frequency * (_BM25_K1 + 1)) / denominator
        if total > 0:
            scored.append((total, passage.ref))
    return scored


def _vector_scores(passages, query_vector: bytes) -> list[tuple[float, str]]:
    from array import array
    query = array("f")
    query.frombytes(query_vector)
    query_norm = math.sqrt(sum(value * value for value in query))
    if query_norm == 0:
        return []
    dimensions = len(query)
    scored: list[tuple[float, str]] = []
    for passage in passages:
        candidate = array("f")
        try:
            candidate.frombytes(passage.vector)
        except (ValueError, TypeError):
            continue
        if len(candidate) != dimensions:
            continue
        candidate_norm = math.sqrt(sum(value * value for value in candidate))
        if candidate_norm == 0:
            continue
        dot = sum(a * b for a, b in zip(query, candidate, strict=True))
        scored.append((dot / (query_norm * candidate_norm), passage.ref))
    return scored


def measure(passages, deps, query: str) -> tuple[float | None, float | None]:
    """Both legs' separation, with the restatement checked against the shipped helpers."""
    tokens = _tokenize(query)
    lexical_scored = _bm25_scores(passages, tokens)
    vector_scored = _vector_scores(passages, deps.embed_query(query))
    shipped_lexical, _ = _bm25_ranks(passages, tokens, _NO_DEADLINE, _never_expires)
    shipped_vector, _ = _vector_ranks(passages, deps.embed_query(query), _NO_DEADLINE, _never_expires)
    if _ranked(lexical_scored) != (shipped_lexical or {}):
        raise SystemExit(f"restated BM25 disagrees with retrieval._bm25_ranks on: {query!r}")
    if _ranked(vector_scored) != (shipped_vector or {}):
        raise SystemExit(f"restated cosine disagrees with retrieval._vector_ranks on: {query!r}")
    return separation([score for score, _ in lexical_scored]), separation(
        [score for score, _ in vector_scored]
    )


def load(name: str) -> list[dict]:
    path = EVALS / f"{name}-issues.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", nargs=2, action="append", metavar=("CORPUS", "DATA_DIR"),
                        required=True, help="corpus name and the data dir holding its snapshot")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    names = [name for name, _ in args.index]
    questions = {name: load(name) for name in names}
    rows = []
    for index_name, data_dir in args.index:
        deps = build_serve_deps(resolve_paths(cli_data_dir=Path(data_dir), env={}))
        try:
            passages, _stopped = _scan_passages(deps.snapshot.database, _NO_DEADLINE, _never_expires)
            present = {
                strip_build_sha(passage.relative_path.rsplit("/", 1)[-1]) for passage in passages
            }
            print(f"{index_name}: {len(passages)} passages, snapshot {deps.snapshot.build_id}", flush=True)
            for origin in names:
                for case in questions[origin]:
                    lexical, vector = measure(passages, deps, case["query"])
                    truth = strip_build_sha(case["ground_truth"]["must_include"][0])
                    rows.append({
                        "id": case["id"], "origin": origin, "index": index_name,
                        # `home` means this index holds the corpus the question was written about.
                        "label": "home" if origin == index_name else "foreign",
                        "lexical_separation": lexical, "vector_separation": vector,
                        "answer_present": truth in present,
                    })
                print(f"  {origin}: {len(questions[origin])} questions", flush=True)
        finally:
            deps.snapshot.database.close()

    args.out.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
    print(f"Wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
