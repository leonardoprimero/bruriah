"""Tests for `evals/retrieval/report_paired.py` (T4, "promote the two ablation scripts", of
`odd/tasks/github-issue-pr-ingestion.md`): the paired baseline-vs-candidate comparison used to
measure whether `bruriah corpus --github` moves recall, published in
`evals/project-memory/README.md`'s "Issue ingestion, measured 2026-09-21" section.

Runs entirely against synthetic `{"id", "rank"}` / `{"id", "document_rank"}` rank files -- no
index, no corpus, no network -- because the arithmetic under test (recall, MRR, the exact McNemar
sign test, rank-bucket movement) does not depend on how a rank file was produced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_EVALS_RETRIEVAL = _ROOT / "evals" / "retrieval"
if str(_EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(_EVALS_RETRIEVAL))

import report_paired  # noqa: E402


def _write(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_load_ranks_accepts_either_rank_or_document_rank_key(tmp_path: Path) -> None:
    rank_shape = _write(tmp_path / "rank.jsonl", [{"id": "q1", "rank": 2}, {"id": "q2", "rank": None}])
    document_rank_shape = _write(
        tmp_path / "document_rank.jsonl",
        [{"id": "q1", "document_rank": 2}, {"id": "q2", "document_rank": None}],
    )

    assert report_paired.load_ranks(rank_shape) == {"q1": 2, "q2": None}
    assert report_paired.load_ranks(document_rank_shape) == {"q1": 2, "q2": None}


def test_recall_at_and_mrr_at_ignore_absent_ranks() -> None:
    ranks = [1, 5, None, 20]
    assert report_paired.recall_at(ranks, 3) == pytest.approx(0.25)
    assert report_paired.recall_at(ranks, 10) == pytest.approx(0.5)
    assert report_paired.mrr_at(ranks, 10) == pytest.approx((1.0 + 1.0 / 5.0) / 4.0)


def test_exact_mcnemar_p_is_one_when_no_discordant_pairs() -> None:
    assert report_paired.exact_mcnemar_p(0, 0) == 1.0


def test_exact_mcnemar_p_matches_a_hand_computed_value() -> None:
    # n=20, k=min(0, 20)=0: two-sided exact p = 2 * C(20,0) / 2**20 = 2 / 1048576.
    assert report_paired.exact_mcnemar_p(entered=0, left=20) == pytest.approx(2 / 2**20)
    # A symmetric split (n=8, entered=left=4) must not be reported significant: the exact test
    # cannot reject a null the data does not distinguish from.
    assert report_paired.exact_mcnemar_p(entered=4, left=4) == pytest.approx(1.0)


def test_compare_reports_recall_mrr_and_top3_movement(tmp_path: Path) -> None:
    # q1 leaves the top 3 (2 -> 5), q2 enters it (12 -> 1), q3 is unchanged (7 -> 7), q4 stays
    # outside (None -> None). One entered, one left: McNemar over a single discordant pair each way.
    baseline = {"q1": 2, "q2": 12, "q3": 7, "q4": None}
    candidate = {"q1": 5, "q2": 1, "q3": 7, "q4": None}

    result, rows = report_paired.compare(baseline, candidate)

    assert result.n == 4
    assert result.entered_top3 == 1
    assert result.left_top3 == 1
    assert result.mcnemar_p == pytest.approx(1.0)
    assert result.unchanged == 2  # q3 and q4
    assert result.baseline_recall_at_3 == pytest.approx(0.25)  # only q1
    assert result.candidate_recall_at_3 == pytest.approx(0.25)  # only q2
    assert result.baseline_absent == 1
    assert result.candidate_absent == 1

    by_id = {row["id"]: row for row in rows}
    assert by_id["q1"] == {
        "id": "q1",
        "baseline_rank": 2,
        "candidate_rank": 5,
        "baseline_bucket": "1-3",
        "candidate_bucket": "4-10",
    }
    assert by_id["q2"]["baseline_bucket"] == "11-40"
    assert by_id["q2"]["candidate_bucket"] == "1-3"
    assert by_id["q4"] == {
        "id": "q4",
        "baseline_rank": None,
        "candidate_rank": None,
        "baseline_bucket": "41+",
        "candidate_bucket": "41+",
    }


def test_compare_rejects_mismatched_question_sets() -> None:
    with pytest.raises(ValueError, match="same question ids"):
        report_paired.compare({"q1": 1, "q2": 2}, {"q1": 1, "q3": 2})


def test_main_prints_json_and_writes_movement_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline = _write(tmp_path / "baseline.jsonl", [{"id": "q1", "rank": None}, {"id": "q2", "rank": 1}])
    candidate = _write(
        tmp_path / "candidate.jsonl",
        [{"id": "q1", "document_rank": 2}, {"id": "q2", "document_rank": 1}],
    )
    out = tmp_path / "movement.jsonl"

    exit_code = report_paired.main(
        [
            "--corpus",
            "test-corpus",
            "--baseline",
            str(baseline),
            "--candidate",
            str(candidate),
            "--out",
            str(out),
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["n"] == 2
    assert result["entered_top3"] == 1  # q1: absent -> rank 2
    assert result["left_top3"] == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert {row["id"] for row in rows} == {"q1", "q2"}
