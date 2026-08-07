#!/usr/bin/env python3
"""Arithmetic over `separation-paired.jsonl`. No index, no model, no network.

    python evals/retrieval/report_separation.py

THE SIGN TEST IS THE HEADLINE, NOT THE AUC. AUC compares two POPULATIONS and so re-admits every
difference between them -- which is how the unpaired version of this measurement read 0.856 on a
question set whose two halves had different authors. The sign test compares each question against
ITSELF and admits only the corpus. Under the null -- separation says nothing about whether the
answer is present -- the probability that `home` beats `foreign` for a given question is exactly
0.5, so the p-value is an exact binomial with no distributional assumption to argue about.

Report the per-corpus rows, always. Pooled, this measurement reads as a clean null; split, it is
two large opposite effects that happen to cancel. Those are not the same finding, and only one of
them is true.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from metrics import _median, _percentile  # noqa: E402

DEFAULT_ROWS = _HERE.parents[0] / "project-memory" / "separation-paired.jsonl"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def two_sided_binomial(wins: int, total: int) -> float:
    """Exact two-sided p under p=0.5, summing the tail at least as extreme as observed."""
    if total == 0:
        return 1.0
    extreme = max(wins, total - wins)
    tail = sum(math.comb(total, k) for k in range(extreme, total + 1))
    return min(1.0, 2 * tail / (2 ** total))


def auc(highs: list[float], lows: list[float]) -> float | None:
    """P(a random `home` scores above a random `foreign`); ties count a half."""
    if not highs or not lows:
        return None
    wins = sum(1.0 if h > l else 0.5 if h == l else 0.0 for h in highs for l in lows)
    return wins / (len(highs) * len(lows))


def pairs_for(rows: list[dict], field: str) -> list[tuple[float, float]]:
    by_question: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        if row[field] is not None:
            by_question.setdefault((row["origin"], row["id"]), {})[row["label"]] = row[field]
    return [
        (sides["home"], sides["foreign"])
        for sides in by_question.values() if "home" in sides and "foreign" in sides
    ]


def render(label: str, rows: list[dict], field: str) -> str:
    pairs = pairs_for(rows, field)
    if not pairs:
        return f"{label:<28} {field:<20} no complete pairs"
    home = [value for value, _ in pairs]
    foreign = [value for _, value in pairs]
    deltas = [h - f for h, f in pairs]
    wins = sum(1 for delta in deltas if delta > 0)
    total = sum(1 for delta in deltas if delta != 0)
    area = auc(home, foreign)
    return (
        f"{label:<28} {field:<20} pairs={len(pairs):>3}  "
        f"home={_median(home):6.2f} foreign={_median(foreign):6.2f} "
        f"delta={_median(deltas):+6.2f}  "
        f"home wins {wins:>3}/{total:<3} ({wins/total:5.1%})  "
        f"p={two_sided_binomial(wins, total):9.3g}  AUC={area:.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    path = Path(argv[0]) if argv else DEFAULT_ROWS
    rows = load(path)
    home = sum(1 for row in rows if row["label"] == "home")
    present_home = sum(1 for row in rows if row["label"] == "home" and row["answer_present"])
    present_foreign = sum(1 for row in rows if row["label"] == "foreign" and row["answer_present"])
    print(f"{len(rows)} rows.  answer present: {present_home}/{home} home, "
          f"{present_foreign}/{len(rows) - home} foreign"
          + ("  (the pairing is exact)" if present_home == home and not present_foreign else
             "  <-- THE PAIRING IS BROKEN; nothing below means what it says"))
    print()
    for field in ("vector_separation", "lexical_separation"):
        print(render("POOLED", rows, field))
        for origin in sorted({row["origin"] for row in rows}):
            subset = [row for row in rows if row["origin"] == origin]
            print(render(f"  {origin} questions", subset, field))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
