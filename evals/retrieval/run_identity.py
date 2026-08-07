#!/usr/bin/env python3
"""Does turning the reranking stage on change WHICH documents come back, before any model speaks?

    python evals/retrieval/run_identity.py \
        --index leakcanary /tmp/idx-leakcanary --index egui /tmp/idx-egui

`_rerank_fused` promises it "changes the ORDER of the evidence and never its membership", and its
tie-break note adds that a reranker scoring two documents identically cannot reorder them. A
reranker returning one constant for every document is therefore a no-op by both claims, so any
difference measured here belongs to the STAGE and not to a model -- which is why this needs no
download, no cross-encoder and no GPU, and runs in a minute against an already-built index.

It found the promise false: grouping a document's passages together, then truncating at
`max_candidates` (which counts passages), spent the budget on long documents. See "The stage was
halving the result before any model spoke" in `evals/project-memory/README.md`.

MEASURED AT TWO BUDGETS ON PURPOSE. `run_ablation.py` raises max_candidates to 200 and
max_extracted_chars to 200,000, which is where the published reranking figures come from; a caller
who passes no Budgets gets 50 and 20,000. The gap between those two rows is how the defect
survived its own eval.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
for _path in (str(_HERE), str(ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ablation import strip_build_sha  # noqa: E402

from bruriah.cli import build_serve_deps  # noqa: E402
from bruriah.contracts import Budgets  # noqa: E402
from bruriah.platform import resolve_paths  # noqa: E402
from bruriah.retrieval import search  # noqa: E402

BUDGETS = {
    "defaults": Budgets(),
    "ceilings": Budgets(max_candidates=200, max_elapsed_ms=120_000, max_extracted_chars=200_000),
}


def identity_rerank(_query: str, documents: list[str]) -> list[float]:
    """One score for every document: a no-op by the stage's own contract."""
    return [0.0] * len(documents)


def documents_of(outcome) -> list[str]:
    seen: list[str] = []
    for match in outcome.matches:
        name = strip_build_sha(match.relative_path.rsplit("/", 1)[-1])
        if name not in seen:
            seen.append(name)
    return seen


def summarize(rows: list[dict]) -> str:
    lines = []
    for budget in BUDGETS:
        for corpus in sorted({row["corpus"] for row in rows}):
            subset = [r for r in rows if r["budgets"] == budget and r["corpus"] == corpus]
            if not subset:
                continue
            count = len(subset)
            changed = sum(1 for r in subset if r["dropped"] or r["added"])
            lost = sum(1 for r in subset if r["plain_rank"] and not r["staged_rank"])
            moved = sum(1 for r in subset
                        if r["plain_rank"] and r["staged_rank"] and r["plain_rank"] != r["staged_rank"])
            plain = sum(r["plain_documents"] for r in subset) / count
            staged = sum(r["staged_documents"] for r in subset) / count
            lines.append(
                f"{budget:<9} {corpus:<12} n={count:>3}  documents {plain:6.1f} -> {staged:6.1f}  "
                f"membership changed {changed:>3}/{count:<3}  answers dropped {lost:>2}  "
                f"document rank moved {moved:>2}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", nargs=2, action="append", metavar=("CORPUS", "DATA_DIR"),
                        required=True, help="corpus name and the data dir holding its snapshot")
    parser.add_argument("--out", type=Path, default=None, help="per-question rows, as JSONL")
    args = parser.parse_args(argv)

    rows: list[dict] = []
    for corpus, data_dir in args.index:
        deps = build_serve_deps(resolve_paths(cli_data_dir=Path(data_dir), env={}))
        cases = [
            json.loads(line)
            for line in (ROOT / "evals" / "project-memory" / f"{corpus}-issues.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        try:
            for label, budgets in BUDGETS.items():
                for case in cases:
                    truth = strip_build_sha(case["ground_truth"]["must_include"][0])
                    plain = search(deps.snapshot, case["query"], budgets,
                                   embed_query=deps.embed_query, clock=deps.clock)
                    staged = search(deps.snapshot, case["query"], budgets,
                                    embed_query=deps.embed_query, rerank=identity_rerank,
                                    clock=deps.clock)
                    # Without this the run would compare the shipped ranking against itself and
                    # report a confident "no effect" -- run_ablation.py's guard, same reason.
                    if not any(note.startswith("reranked:") for note in staged.degradation):
                        raise SystemExit(
                            f"{case['id']}: the stage did not run -- {staged.degradation}")
                    plain_documents, staged_documents = documents_of(plain), documents_of(staged)
                    rows.append({
                        "corpus": corpus, "id": case["id"], "budgets": label,
                        "plain_documents": len(plain_documents),
                        "staged_documents": len(staged_documents),
                        "dropped": sorted(set(plain_documents) - set(staged_documents)),
                        "added": sorted(set(staged_documents) - set(plain_documents)),
                        "plain_rank": plain_documents.index(truth) + 1 if truth in plain_documents else None,
                        "staged_rank": staged_documents.index(truth) + 1 if truth in staged_documents else None,
                    })
        finally:
            deps.snapshot.database.close()
        print(f"{corpus}: {len(cases)} questions x {len(BUDGETS)} budgets", flush=True)

    print()
    print(summarize(rows))
    if args.out:
        args.out.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
