#!/usr/bin/env python3
"""Pool the per-question ranks `run_fusion_sweep.py --out` wrote for one or more corpora into one table.

    python evals/retrieval/report_fusion_sweep.py --ranks /tmp/sweep-leakcanary.jsonl /tmp/sweep-egui.jsonl

No model and no index: the run scripts already did that work, and the number the README publishes
is the COMBINED one, over 236 questions, which no single run can produce. `entered`/`left`/`p` are
against the shipped rule found in the same files, so the comparison is paired per question.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from report_paired import exact_mcnemar_p, mrr_at, recall_at  # noqa: E402
from run_fusion_sweep import SHIPPED, Condition, Row, render  # noqa: E402


def load(paths: list[Path]) -> dict[Condition, dict[str, int | None]]:
    ranks: dict[Condition, dict[str, int | None]] = defaultdict(dict)
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            condition = Condition(
                entry["aggregation"], entry["lexical_weight"], entry["rrf_k"], entry.get("title_weight", 0.0)
            )
            key = f"{path.name}:{entry['id']}"  # ids are per corpus; two corpora may reuse one
            ranks[condition][key] = entry["document_rank"]
    return ranks


def rows(ranks: dict[Condition, dict[str, int | None]]) -> list[Row]:
    shipped = ranks[SHIPPED]
    out: list[Row] = []
    for condition, by_question in ranks.items():
        keys = sorted(by_question)
        mine = [by_question[key] for key in keys]
        base = [shipped[key] for key in keys]
        entered = sum(1 for s, c in zip(base, mine, strict=True) if not (s and s <= 3) and (c and c <= 3))
        left = sum(1 for s, c in zip(base, mine, strict=True) if (s and s <= 3) and not (c and c <= 3))
        out.append(
            Row(
                condition,
                tuple(mine),
                recall_at(mine, 3),
                recall_at(mine, 10),
                mrr_at(mine, 10),
                entered,
                left,
                exact_mcnemar_p(entered, left),
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ranks", type=Path, nargs="+", required=True, help="one --out file per corpus")
    parser.add_argument("--corpus", default="combined")
    parser.add_argument("--top", type=int, default=None, help="print only the best N conditions plus shipped")
    parser.add_argument("--only", choices=["title"], default=None, help="print only the title-leg conditions")
    args = parser.parse_args(argv)

    ranks = load(args.ranks)
    if SHIPPED not in ranks:
        raise SystemExit(
            "the shipped condition (first w=1.0 k=60) is missing from the ranks; nothing to compare against"
        )
    table = rows(ranks)
    questions = len(ranks[SHIPPED])
    shipped_row = next(row for row in table if row.condition == SHIPPED)
    if args.only == "title":
        table = [row for row in table if row.condition.aggregation == "title" or row.condition.title_weight]
        table.insert(0, shipped_row)
    if args.top:
        table = sorted(table, key=lambda r: (-r.recall3, -r.mrr10, r.condition.label))[: args.top]
        if shipped_row not in table:
            table.append(shipped_row)
    print(render(args.corpus, table, questions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
