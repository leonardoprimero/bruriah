#!/usr/bin/env python3
"""Sweep the ranking rule itself: every way of turning two per-passage legs into ONE document order.

    python evals/retrieval/run_fusion_sweep.py --corpus leakcanary --data-dir /tmp/data-leakcanary \
        --questions evals/project-memory/leakcanary-issues.jsonl

WHY THIS EXISTS. `evals/project-memory/README.md` measured the embedder, the instruction prefix, the
reranker and its depth, and a corpus-noise filter. It never measured the fusion rule, which has
three knobs that no published number varied together: the lexical leg's weight (1.0, or 0.1
cross-lingually), the RRF constant (60, the literature default) and -- the one nobody named -- how
a document's rank is derived from its passages' ranks. Today it is the position of the document's
FIRST fused passage (`ablation.document_ranking`), which discards every other passage the same
document matched. A commit whose subject matched lexically and whose body matched semantically
gets credit for one of those, in one leg, at passage granularity.

`report_reach.py` established that the correct document is ranked every time and that the loss is
in the last stretch of ordering. This script asks whether that stretch is partly the fusion rule:
no new model, no reranker, no re-index -- the two legs are computed ONCE per question and every
condition below is arithmetic over the same cached ranks, so the sweep costs seconds after the
legs are in.

Conditions, all deterministic and tie-broken on the document name:
  first    passage-level RRF; document score = its best passage's score   (SHIPPED)
  sum      passage-level RRF; document score = sum over all its passages
  doc_rrf  each leg collapsed to a document rank (best passage wins) BEFORE fusing; RRF on documents

With `--title-leg`, five more conditions add a THIRD leg: the query embedded against each document's
H1 alone (the commit subject, for a git-derived corpus), ranked over documents and fused by RRF
with the shipped order at a weight of 0.5, 1.0 or 2.0, plus that leg on its own. The hypothesis it
tests is that an issue title resembles the subject of the commit that closed it more than it
resembles the passage the subject is diluted in. It costs one embedding per document, of a string
short enough that a 2,180-document corpus takes about a minute.

The `first` condition at weight 1.0 / k 60 is asserted to reproduce `_fuse`'s own document order on
every question, so the sweep is measured against the pipeline that ships and not a re-derivation.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import strip_build_sha  # noqa: E402
from report_paired import exact_mcnemar_p, mrr_at, recall_at  # noqa: E402

from bruriah import language  # noqa: E402
from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import (  # noqa: E402
    _CROSS_LINGUAL_LEXICAL_WEIGHT,
    _bm25_ranks,
    _corpus_language,
    _fuse,
    _scan_passages,
    _tokenize,
    _vector_ranks,
)

_NO_DEADLINE = float("inf")
_AGGREGATIONS = ("first", "sum", "doc_rrf")
_WEIGHTS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
_RRF_KS = (10, 20, 40, 60, 100)
_TITLE_WEIGHTS = (0.5, 1.0, 2.0)


def _never_expires() -> float:
    return 0.0


@dataclass(frozen=True)
class Legs:
    """Both legs for one question, as `search` computes them, kept so every condition is arithmetic."""

    id: str
    truth: str
    lexical: dict[str, int] | None
    vector: dict[str, int] | None
    weight: float  # the cross-lingual discount `search` would apply; 1.0 on same-language pairs
    title: dict[str, int] | None = None  # DOCUMENT ranks by H1 similarity; only with --title-leg


@dataclass(frozen=True)
class Condition:
    aggregation: str
    lexical_weight: float
    rrf_k: int
    title_weight: float = 0.0

    @property
    def label(self) -> str:
        base = f"{self.aggregation:8s} w={self.lexical_weight:<4} k={self.rrf_k:<3}"
        return f"{base} title={self.title_weight}" if self.title_weight else base


# The rule that ships: passage-level RRF at the literature constant, first-occurrence collapse.
SHIPPED = Condition("first", 1.0, 60)


def _rrf(rank: int | None, k: int, weight: float = 1.0) -> float:
    return weight / (k + rank) if rank is not None else 0.0


def document_order(legs: Legs, doc_of: dict[str, str], condition: Condition) -> list[str]:
    """Documents best first under `condition`, ties broken on the document name for determinism.

    The lexical weight is the condition's weight TIMES the discount `search` would already apply to
    this question (1.0 same-language, `_CROSS_LINGUAL_LEXICAL_WEIGHT` otherwise), so a sweep value
    of 1.0 reproduces the shipped rule on every question and the cross-lingual rule is not undone.

    A title leg, when the condition carries one, is fused at DOCUMENT level: the order the passage
    legs produce becomes a document rank, and RRF combines it with the H1 rank at the same `k`.
    """
    if condition.aggregation == "title":
        return [name for name, _ in sorted((legs.title or {}).items(), key=lambda item: (item[1], item[0]))]
    base = _passage_legs_order(legs, doc_of, condition)
    if not condition.title_weight:
        return base
    title = legs.title or {}
    base_rank = {name: position for position, name in enumerate(base, start=1)}
    score = {
        name: _rrf(base_rank.get(name), condition.rrf_k)
        + _rrf(title.get(name), condition.rrf_k, condition.title_weight)
        for name in set(base_rank) | set(title)
    }
    return [name for name, _ in sorted(score.items(), key=lambda item: (-item[1], item[0]))]


def _passage_legs_order(legs: Legs, doc_of: dict[str, str], condition: Condition) -> list[str]:
    lexical = legs.lexical or {}
    vector = legs.vector or {}
    weight = condition.lexical_weight * legs.weight
    k = condition.rrf_k
    score: dict[str, float] = {}

    if condition.aggregation == "doc_rrf":
        best_lex: dict[str, int] = {}
        best_vec: dict[str, int] = {}
        for ref, rank in lexical.items():
            name = doc_of[ref]
            best_lex[name] = min(rank, best_lex.get(name, rank))
        for ref, rank in vector.items():
            name = doc_of[ref]
            best_vec[name] = min(rank, best_vec.get(name, rank))
        # Re-rank at document level: dense ranks 1..n over documents, so the constant `k` means the
        # same thing it means at passage level and a document is not penalised for its passages.
        lex_doc_rank = {
            name: pos for pos, (name, _) in enumerate(sorted(best_lex.items(), key=lambda i: (i[1], i[0])), 1)
        }
        vec_doc_rank = {
            name: pos for pos, (name, _) in enumerate(sorted(best_vec.items(), key=lambda i: (i[1], i[0])), 1)
        }
        for name in set(lex_doc_rank) | set(vec_doc_rank):
            score[name] = _rrf(lex_doc_rank.get(name), k, weight) + _rrf(vec_doc_rank.get(name), k)
    elif condition.aggregation == "first":
        # Exactly `ranking.fuse_ranks` followed by first-occurrence collapse: passages sorted on
        # (-score, ref), so two passages of different documents that tie are ordered by ref, as the
        # engine orders them, and not by document name. That is what lets `main` assert equality
        # with `_fuse` on every question rather than on the ones without ties.
        fused = sorted(
            ((_rrf(lexical.get(ref), k, weight) + _rrf(vector.get(ref), k), ref) for ref in set(lexical) | set(vector)),
            key=lambda item: (-item[0], item[1]),
        )
        order: list[str] = []
        seen: set[str] = set()
        for _score, ref in fused:
            name = doc_of[ref]
            if name not in seen:
                seen.add(name)
                order.append(name)
        return order
    elif condition.aggregation == "sum":
        for ref in set(lexical) | set(vector):
            name = doc_of[ref]
            score[name] = score.get(name, 0.0) + _rrf(lexical.get(ref), k, weight) + _rrf(vector.get(ref), k)
    else:  # pragma: no cover -- guarded by _AGGREGATIONS
        raise ValueError(condition.aggregation)

    return [name for name, _ in sorted(score.items(), key=lambda item: (-item[1], item[0]))]


def rank_of(order: Sequence[str], truth: str) -> int | None:
    for position, name in enumerate(order, start=1):
        if name == truth:
            return position
    return None


def shipped_document_order(legs: Legs, by_ref) -> list[str]:
    """What `_fuse` ships, collapsed to documents by first occurrence -- the reference every
    condition is compared against, produced by the engine and not by this file."""
    seen: list[str] = []
    for ref, _lex, _vec in _fuse(legs.lexical, legs.vector, legs.weight):
        name = strip_build_sha(by_ref[ref].relative_path.rsplit("/", 1)[-1])
        if name not in seen:
            seen.append(name)
    return seen


def title_vectors(passages, doc_of: dict[str, str], embed_query) -> list[tuple[str, bytes]]:
    """One vector per document, of its H1 alone, embedded by the same model the index was built with.

    The H1 is `heading_path[0]` of any passage under it, which for a git-derived corpus is the commit
    subject (`gitcorpus.build`). A document without one contributes nothing to the leg."""
    titles: dict[str, str] = {}
    for passage in passages:
        name = doc_of[passage.ref]
        if name not in titles and passage.heading_path:
            titles[name] = passage.heading_path[0]
    return [(name, embed_query(title)) for name, title in sorted(titles.items())]


def compute_legs(passages, corpus_language, deps, case: dict, titles: list[tuple[str, bytes]] | None = None) -> Legs:
    query = case["query"]
    lexical, _ = _bm25_ranks(passages, _tokenize(query), _NO_DEADLINE, _never_expires)
    query_vector = deps.embed_query(query)
    vector, _ = _vector_ranks(passages, query_vector, _NO_DEADLINE, _never_expires)
    title: dict[str, int] | None = None
    if titles is not None:
        # `_vector_ranks` accepts (ref, vector) pairs, so the document name stands in for the ref
        # and the leg is ranked by exactly the cosine the vector leg uses.
        title, _ = _vector_ranks(titles, query_vector, _NO_DEADLINE, _never_expires)
    weight = 1.0
    query_language = language.detect(query)
    if query_language is not None and corpus_language is not None and query_language != corpus_language:
        weight = _CROSS_LINGUAL_LEXICAL_WEIGHT
    return Legs(case["id"], strip_build_sha(case["ground_truth"]["must_include"][0]), lexical, vector, weight, title)


@dataclass(frozen=True)
class Row:
    condition: Condition
    ranks: tuple[int | None, ...]
    recall3: float
    recall10: float
    mrr10: float
    entered: int
    left: int
    p: float


def evaluate(
    condition: Condition, all_legs: Sequence[Legs], doc_of: dict[str, str], shipped: Sequence[int | None]
) -> Row:
    ranks = tuple(rank_of(document_order(legs, doc_of, condition), legs.truth) for legs in all_legs)
    entered = sum(1 for s, c in zip(shipped, ranks, strict=True) if not (s and s <= 3) and (c and c <= 3))
    left = sum(1 for s, c in zip(shipped, ranks, strict=True) if (s and s <= 3) and not (c and c <= 3))
    return Row(
        condition,
        ranks,
        recall_at(list(ranks), 3),
        recall_at(list(ranks), 10),
        mrr_at(list(ranks), 10),
        entered,
        left,
        exact_mcnemar_p(entered, left),
    )


def render(corpus: str, rows: Sequence[Row], questions: int) -> str:
    lines = [
        f"## {corpus} -- fusion sweep, {questions} questions, {len(rows)} conditions",
        "",
        "Sorted by recall@3. `entered`/`left` are questions that moved into / out of the top 3 against",
        "the shipped rule (`first w=1.0 k=60`); `p` is the exact two-sided McNemar over those pairs.",
        "",
        "| condition | recall@3 | recall@10 | MRR@10 | entered | left | p |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (-r.recall3, -r.mrr10, r.condition.label)):
        shipped = row.condition == SHIPPED
        label = f"`{row.condition.label}`" + (" *(shipped)*" if shipped else "")
        lines.append(
            f"| {label} | {row.recall3:.3f} | {row.recall10:.3f} | {row.mrr10:.3f} | {row.entered} | {row.left} | {row.p:.4f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--legs-cache", type=Path, default=None, help="JSON cache of both legs per question")
    parser.add_argument("--out", type=Path, default=None, help="per-question ranks for every condition, JSONL")
    parser.add_argument("--top", type=int, default=None, help="print only the best N conditions plus shipped")
    parser.add_argument(
        "--title-leg",
        action="store_true",
        help="also embed every document's H1 and add the title-leg conditions (one embedding per document)",
    )
    args = parser.parse_args(argv)

    cases = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]

    deps = build_serve_deps(resolve_paths(cli_data_dir=args.data_dir, env={}))
    try:
        passages, _stopped = _scan_passages(deps.snapshot.database, _NO_DEADLINE, _never_expires)
        by_ref = {passage.ref: passage for passage in passages}
        doc_of = {p.ref: strip_build_sha(p.relative_path.rsplit("/", 1)[-1]) for p in passages}
        corpus_language = _corpus_language(passages)

        cached: dict[str, dict] = {}
        if args.legs_cache and args.legs_cache.exists():
            cached = json.loads(args.legs_cache.read_text(encoding="utf-8"))
        titles: list[tuple[str, bytes]] | None = None
        if args.title_leg and any(case["id"] not in cached or "title" not in cached[case["id"]] for case in cases):
            titles = title_vectors(passages, doc_of, deps.embed_query)
        all_legs: list[Legs] = []
        shipped_ranks: list[int | None] = []
        for case in cases:
            entry = cached.get(case["id"])
            if entry is not None and (not args.title_leg or "title" in entry):
                legs = Legs(
                    case["id"], entry["truth"], entry["lexical"], entry["vector"], entry["weight"], entry.get("title")
                )
            else:
                legs = compute_legs(passages, corpus_language, deps, case, titles)
                cached[case["id"]] = {
                    "truth": legs.truth,
                    "lexical": legs.lexical,
                    "vector": legs.vector,
                    "weight": legs.weight,
                    **({"title": legs.title} if legs.title is not None else {}),
                }
            all_legs.append(legs)
        # Written BEFORE the reproduction check below, so a failed check costs the fix and not the
        # minutes of embedding every question again.
        if args.legs_cache:
            args.legs_cache.write_text(json.dumps(cached, sort_keys=True), encoding="utf-8")
        for legs in all_legs:
            # The reference is the ENGINE's order. Asserting the reproduction here, on every
            # question, is what lets every other row claim to be measured against what ships.
            engine = shipped_document_order(legs, by_ref)
            mine = document_order(legs, doc_of, SHIPPED)
            if engine != mine:
                raise SystemExit(f"{legs.id}: reimplemented `first` disagrees with `_fuse`; refusing to publish")
            shipped_ranks.append(rank_of(engine, legs.truth))
    finally:
        deps.snapshot.database.close()

    conditions = [Condition(a, w, k) for a, w, k in itertools.product(_AGGREGATIONS, _WEIGHTS, _RRF_KS)]
    if args.title_leg:
        conditions.append(Condition("title", 1.0, 60))
        conditions.extend(Condition("first", 1.0, 60, title_weight) for title_weight in _TITLE_WEIGHTS)
        conditions.append(Condition("first", 0.5, 60, 1.0))
    rows = [evaluate(condition, all_legs, doc_of, shipped_ranks) for condition in conditions]

    if args.out:
        with args.out.open("w", encoding="utf-8") as handle:
            for row in rows:
                for legs, rank in zip(all_legs, row.ranks, strict=True):
                    handle.write(
                        json.dumps(
                            {
                                "id": legs.id,
                                "aggregation": row.condition.aggregation,
                                "lexical_weight": row.condition.lexical_weight,
                                "rrf_k": row.condition.rrf_k,
                                "title_weight": row.condition.title_weight,
                                "document_rank": rank,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )

    if args.top:
        ordered = sorted(rows, key=lambda r: (-r.recall3, -r.mrr10, r.condition.label))
        keep = ordered[: args.top]
        shipped_row = next(r for r in rows if r.condition == SHIPPED)
        if shipped_row not in keep:
            keep.append(shipped_row)
        rows = keep
    print(render(args.corpus, rows, len(cases)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
