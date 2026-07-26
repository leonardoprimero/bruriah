#!/usr/bin/env python3
"""Runs the note-level retrieval-quality eval against BOTH live Cerebro
engines (legacy `cerebro.py`/`cerebro.db` and the new `bruriah` active
snapshot) and writes `<out-prefix>.md` + `<out-prefix>.json` (default
`report-v1.md`/`report-v1.json`).

Read-only, deterministic, reproducible: every DB is opened `mode=ro`, no
metric depends on wall-clock time, and the report is stamped with the legacy
DB's sha256 (asserted unchanged before/after the run) plus the new engine's
snapshot `build_id` so a future run against the same dataset and the same
indexes reproduces the same numbers.

Usage:
    python evals/retrieval/run_eval.py
    python evals/retrieval/run_eval.py --dataset dataset-v2.jsonl --out-prefix report-v3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from adapters import ROOT, LEGACY_DB_PATH, LegacyAdapter, RouterAdapter, SearchResult  # noqa: E402
from metrics import (  # noqa: E402
    abstention_score_separation,
    ambiguous_diversity,
    cross_lingual_recall_delta,
    exact_name_recall_at_3,
    mean_or_none,
    mrr_at_10,
    ndcg_at_10,
    recall_at_k,
)

DEFAULT_DATASET_PATH = _HERE / "dataset-v1.jsonl"
DEFAULT_OUT_PREFIX = "report-v1"
K = 10
ENGINES = ("legacy", "new")
POSITIVE_BASELINE_TYPES = {"factual", "procedural", "exact_name"}


@dataclass(frozen=True)
class QueryRecord:
    id: str
    query: str
    language: str
    type: str
    must_include: list[str]
    acceptable: list[str]
    note: str


@dataclass
class EngineQueryScore:
    ranked_notes: list[str]
    top1_note: str | None
    top1_score: float | None
    first_correct_rank: int | None
    recall_at_5: float | None
    recall_at_10: float | None
    mrr_at_10: float | None
    ndcg_at_10: float | None
    exact_name_recall_at_3: float | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dataset(path: Path) -> list[QueryRecord]:
    records: list[QueryRecord] = []
    with path.open(encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            ground_truth = payload["ground_truth"]
            records.append(
                QueryRecord(
                    id=payload["id"],
                    query=payload["query"],
                    language=payload["language"],
                    type=payload["type"],
                    must_include=list(ground_truth["must_include"]),
                    acceptable=list(ground_truth["acceptable"]),
                    note=payload.get("note", ""),
                )
            )
    return records


def score_engine(results: list[SearchResult], record: QueryRecord) -> EngineQueryScore:
    ranked_notes = [result.relative_path for result in results]
    top1 = results[0] if results else None
    first_correct_rank = next(
        (result.rank for result in results if result.relative_path in record.must_include), None
    )
    return EngineQueryScore(
        ranked_notes=ranked_notes,
        top1_note=top1.relative_path if top1 else None,
        top1_score=top1.score if top1 else None,
        first_correct_rank=first_correct_rank,
        recall_at_5=recall_at_k(ranked_notes, record.must_include, 5),
        recall_at_10=recall_at_k(ranked_notes, record.must_include, 10),
        mrr_at_10=mrr_at_10(ranked_notes, record.must_include),
        ndcg_at_10=ndcg_at_10(ranked_notes, record.must_include, record.acceptable),
        exact_name_recall_at_3=(
            exact_name_recall_at_3(ranked_notes, record.must_include) if record.type == "exact_name" else None
        ),
    )


def _aggregate(scores: list[EngineQueryScore]) -> dict[str, float | None]:
    return {
        "recall_at_5": mean_or_none([score.recall_at_5 for score in scores]),
        "recall_at_10": mean_or_none([score.recall_at_10 for score in scores]),
        "mrr_at_10": mean_or_none([score.mrr_at_10 for score in scores]),
        "ndcg_at_10": mean_or_none([score.ndcg_at_10 for score in scores]),
        "n": len(scores),
    }


@dataclass
class RunResult:
    per_query: list[dict[str, object]] = field(default_factory=list)
    aggregate_overall: dict[str, dict[str, object]] = field(default_factory=dict)
    aggregate_by_type: dict[str, dict[str, object]] = field(default_factory=dict)
    aggregate_by_language: dict[str, dict[str, object]] = field(default_factory=dict)
    exact_name_recall_at_3: dict[str, float | None] = field(default_factory=dict)
    cross_lingual_recall_delta: dict[str, float | None] = field(default_factory=dict)
    ambiguous_diversity: dict[str, dict[str, int]] = field(default_factory=dict)
    abstention_separation: dict[str, dict[str, object]] = field(default_factory=dict)


def run(dataset: list[QueryRecord], adapters: dict[str, object]) -> RunResult:
    per_engine_scores: dict[str, dict[str, EngineQueryScore]] = {engine: {} for engine in ENGINES}
    per_query_rows: list[dict[str, object]] = []

    for record in dataset:
        row: dict[str, object] = {"id": record.id, "query": record.query, "type": record.type, "language": record.language}
        for engine in ENGINES:
            results = adapters[engine].search(record.query, k=K)
            score = score_engine(results, record)
            per_engine_scores[engine][record.id] = score
            row[engine] = {
                "top1_note": score.top1_note,
                "top1_score": score.top1_score,
                "first_correct_rank": score.first_correct_rank,
                "recall_at_5": score.recall_at_5,
                "recall_at_10": score.recall_at_10,
                "mrr_at_10": score.mrr_at_10,
                "ndcg_at_10": score.ndcg_at_10,
            }
        per_query_rows.append(row)

    result = RunResult(per_query=per_query_rows)

    by_type: dict[str, list[QueryRecord]] = defaultdict(list)
    by_language: dict[str, list[QueryRecord]] = defaultdict(list)
    for record in dataset:
        by_type[record.type].append(record)
        by_language[record.language].append(record)

    for engine in ENGINES:
        all_scores = [per_engine_scores[engine][record.id] for record in dataset]
        result.aggregate_overall[engine] = _aggregate(all_scores)

        for query_type, records in by_type.items():
            key_scores = [per_engine_scores[engine][record.id] for record in records]
            result.aggregate_by_type.setdefault(query_type, {})[engine] = _aggregate(key_scores)

        for language, records in by_language.items():
            key_scores = [per_engine_scores[engine][record.id] for record in records]
            result.aggregate_by_language.setdefault(language, {})[engine] = _aggregate(key_scores)

        exact_name_scores = [
            per_engine_scores[engine][record.id].exact_name_recall_at_3
            for record in dataset
            if record.type == "exact_name"
        ]
        result.exact_name_recall_at_3[engine] = mean_or_none(exact_name_scores)

        cross_lingual_recalls = [
            per_engine_scores[engine][record.id].recall_at_10
            for record in dataset
            if record.type == "cross_lingual" and per_engine_scores[engine][record.id].recall_at_10 is not None
        ]
        baseline_recalls = [
            per_engine_scores[engine][record.id].recall_at_10
            for record in dataset
            if record.type in POSITIVE_BASELINE_TYPES and per_engine_scores[engine][record.id].recall_at_10 is not None
        ]
        result.cross_lingual_recall_delta[engine] = cross_lingual_recall_delta(cross_lingual_recalls, baseline_recalls)

        ambiguous_records = [record for record in dataset if record.type == "ambiguous"]
        diversity = {
            record.id: ambiguous_diversity(per_engine_scores[engine][record.id].ranked_notes, k=K)
            for record in ambiguous_records
        }
        result.ambiguous_diversity[engine] = diversity

        abstention_top1 = [
            per_engine_scores[engine][record.id].top1_score
            for record in dataset
            if record.type == "abstention" and per_engine_scores[engine][record.id].top1_score is not None
        ]
        positive_top1 = [
            per_engine_scores[engine][record.id].top1_score
            for record in dataset
            if record.type != "abstention" and per_engine_scores[engine][record.id].top1_score is not None
        ]
        separation = abstention_score_separation(abstention_top1, positive_top1)
        result.abstention_separation[engine] = asdict(separation)

    return result


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(
    result: RunResult, meta: dict[str, object], dataset: list[QueryRecord], out_prefix: str
) -> str:
    lines: list[str] = []
    lines.append(f"# Cerebro Retrieval Eval — NEW vs LEGACY ({out_prefix})")
    lines.append("")
    lines.append(
        "Note-level comparison (chunk/section results deduped to one entry per note, keeping "
        "each note's best rank). Ground truth is engine-independent, resolved by reading vault "
        "content. Both DBs opened strictly read-only; neither engine was mutated by this run."
    )
    lines.append("")
    lines.append("## Reproducibility stamp")
    lines.append("")
    for key, value in meta.items():
        lines.append(f"- **{key}**: `{value}`")
    lines.append("")

    lines.append("## Overall: NEW vs LEGACY")
    lines.append("")
    lines.append("| Metric | Legacy | New | Delta (new - legacy) |")
    lines.append("|---|---|---|---|")
    for metric in ("recall_at_5", "recall_at_10", "mrr_at_10", "ndcg_at_10"):
        legacy_value = result.aggregate_overall["legacy"][metric]
        new_value = result.aggregate_overall["new"][metric]
        delta = new_value - legacy_value if legacy_value is not None and new_value is not None else None
        lines.append(f"| {metric} | {_fmt(legacy_value)} | {_fmt(new_value)} | {_fmt(delta)} |")
    lines.append(f"| n queries | {result.aggregate_overall['legacy']['n']} | {result.aggregate_overall['new']['n']} | |")
    lines.append("")

    lines.append("## By query type")
    lines.append("")
    lines.append("| Type | n | Legacy recall@5 | New recall@5 | Legacy recall@10 | New recall@10 | Legacy MRR@10 | New MRR@10 | Legacy nDCG@10 | New nDCG@10 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for query_type in sorted(result.aggregate_by_type):
        legacy = result.aggregate_by_type[query_type]["legacy"]
        new = result.aggregate_by_type[query_type]["new"]
        lines.append(
            f"| {query_type} | {legacy['n']} | {_fmt(legacy['recall_at_5'])} | {_fmt(new['recall_at_5'])} | "
            f"{_fmt(legacy['recall_at_10'])} | {_fmt(new['recall_at_10'])} | {_fmt(legacy['mrr_at_10'])} | "
            f"{_fmt(new['mrr_at_10'])} | {_fmt(legacy['ndcg_at_10'])} | {_fmt(new['ndcg_at_10'])} |"
        )
    lines.append("")

    lines.append("## By language")
    lines.append("")
    lines.append("| Language | n | Legacy recall@5 | New recall@5 | Legacy recall@10 | New recall@10 | Legacy MRR@10 | New MRR@10 | Legacy nDCG@10 | New nDCG@10 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for language in sorted(result.aggregate_by_language):
        legacy = result.aggregate_by_language[language]["legacy"]
        new = result.aggregate_by_language[language]["new"]
        lines.append(
            f"| {language} | {legacy['n']} | {_fmt(legacy['recall_at_5'])} | {_fmt(new['recall_at_5'])} | "
            f"{_fmt(legacy['recall_at_10'])} | {_fmt(new['recall_at_10'])} | {_fmt(legacy['mrr_at_10'])} | "
            f"{_fmt(new['mrr_at_10'])} | {_fmt(legacy['ndcg_at_10'])} | {_fmt(new['ndcg_at_10'])} |"
        )
    lines.append("")

    lines.append("## Exact-name recall@3")
    lines.append("")
    lines.append("| Engine | recall@3 |")
    lines.append("|---|---|")
    for engine in ENGINES:
        lines.append(f"| {engine} | {_fmt(result.exact_name_recall_at_3[engine])} |")
    lines.append("")

    lines.append("## Cross-lingual recall@10 delta (cross_lingual − same-language baseline)")
    lines.append("")
    lines.append(
        f"Baseline = mean recall@10 over `{'`, `'.join(sorted(POSITIVE_BASELINE_TYPES))}` queries."
    )
    lines.append("")
    lines.append("| Engine | Delta |")
    lines.append("|---|---|")
    for engine in ENGINES:
        lines.append(f"| {engine} | {_fmt(result.cross_lingual_recall_delta[engine])} |")
    lines.append("")

    lines.append("## Ambiguous-query diversity (distinct notes in top-10)")
    lines.append("")
    lines.append("| Query | Legacy | New |")
    lines.append("|---|---|---|")
    for query_id in sorted(result.ambiguous_diversity["legacy"]):
        lines.append(
            f"| {query_id} | {result.ambiguous_diversity['legacy'][query_id]} | "
            f"{result.ambiguous_diversity['new'][query_id]} |"
        )
    lines.append("")

    lines.append("## Abstention score-separation")
    lines.append("")
    lines.append(
        "Top-1 score is a genuine RRF score for legacy; for new it is reconstructed from the "
        "public `lexical_rank`/`vector_rank` fields with the same RRF formula (`k=60`) since "
        "`EvidenceRecord` carries no numeric score by design. `below_p25_fraction` = fraction of "
        "abstention queries whose top-1 score falls below the 25th percentile of positive-query "
        "top-1 scores — higher is better separation."
    )
    lines.append("")
    lines.append("| Engine | Abstention median top-1 | Positive median top-1 | Positive p25 top-1 | Below-p25 fraction | n abstention | n positive |")
    lines.append("|---|---|---|---|---|---|---|")
    for engine in ENGINES:
        separation = result.abstention_separation[engine]
        lines.append(
            f"| {engine} | {_fmt(separation['abstention_median_top1'])} | {_fmt(separation['positive_median_top1'])} | "
            f"{_fmt(separation['positive_p25_top1'])} | {_fmt(separation['below_p25_fraction'])} | "
            f"{separation['abstention_count']} | {separation['positive_count']} |"
        )
    lines.append("")

    lines.append("## Per-query results")
    lines.append("")
    lines.append("| ID | Type | Lang | Query | Legacy first-correct-rank | Legacy top-1 note | New first-correct-rank | New top-1 note |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in result.per_query:
        legacy = row["legacy"]
        new = row["new"]
        query_text = row["query"].replace("|", "\\|")
        lines.append(
            f"| {row['id']} | {row['type']} | {row['language']} | {query_text} | "
            f"{_fmt(legacy['first_correct_rank'])} | {legacy['top1_note'] or 'n/a'} | "
            f"{_fmt(new['first_correct_rank'])} | {new['top1_note'] or 'n/a'} |"
        )
    lines.append("")

    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Path to the dataset JSONL file (default: {DEFAULT_DATASET_PATH.name})",
    )
    parser.add_argument(
        "--out-prefix",
        type=str,
        default=DEFAULT_OUT_PREFIX,
        help=(
            "Prefix for the two output report files, written next to this script "
            f"as '<out-prefix>.md' and '<out-prefix>.json' (default: {DEFAULT_OUT_PREFIX})"
        ),
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    dataset_path = args.dataset if args.dataset.is_absolute() else (_HERE / args.dataset)
    report_md_path = _HERE / f"{args.out_prefix}.md"
    report_json_path = _HERE / f"{args.out_prefix}.json"
    dataset_version = dataset_path.stem.removeprefix("dataset-") or dataset_path.stem

    dataset = load_dataset(dataset_path)

    legacy_hash_before = sha256_file(LEGACY_DB_PATH)

    legacy_adapter = LegacyAdapter()
    router_adapter = RouterAdapter()
    try:
        result = run(dataset, {"legacy": legacy_adapter, "new": router_adapter})
        new_build_id = router_adapter.build_id
    finally:
        router_adapter.close()

    legacy_hash_after = sha256_file(LEGACY_DB_PATH)
    if legacy_hash_before != legacy_hash_after:
        raise SystemExit(
            "READ-ONLY GUARD VIOLATED: legacy cerebro.db sha256 changed during the eval run "
            f"({legacy_hash_before} -> {legacy_hash_after})"
        )

    meta = {
        "dataset_version": dataset_version,
        "dataset_path": str(dataset_path.relative_to(ROOT)),
        "n_queries": len(dataset),
        "k": K,
        "legacy_db_sha256": legacy_hash_after,
        "new_snapshot_build_id": new_build_id,
    }

    report_json = {"meta": meta, "aggregate": {
        "overall": result.aggregate_overall,
        "by_type": result.aggregate_by_type,
        "by_language": result.aggregate_by_language,
        "exact_name_recall_at_3": result.exact_name_recall_at_3,
        "cross_lingual_recall_delta": result.cross_lingual_recall_delta,
        "ambiguous_diversity": result.ambiguous_diversity,
        "abstention_separation": result.abstention_separation,
    }, "per_query": result.per_query}

    report_json_path.write_text(json.dumps(report_json, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    report_md_path.write_text(render_markdown(result, meta, dataset, args.out_prefix), encoding="utf-8")

    print(f"Legacy db sha256 unchanged: {legacy_hash_before == legacy_hash_after}")
    print(f"Wrote {report_json_path}")
    print(f"Wrote {report_md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
