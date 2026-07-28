# The ablation exists to answer one question -- does reranking hurt where the shipped ranking was
# already strong -- and the honest answer depends entirely on the null it is measured against.
# Bucketing by base rank is a biased selection: a document at rank 1 can only move down. So the
# tests that matter most here are not the parsing ones, they are the ones asserting that a reranker
# with no skill scores as no skill.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVALS_RETRIEVAL = Path(__file__).resolve().parents[1] / "evals" / "retrieval"
if str(EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(EVALS_RETRIEVAL))

from ablation import (  # noqa: E402
    QuestionOutcome,
    bucket_of,
    document_ranking,
    expected_rank_under_shuffle,
    rank_of,
    reach,
    strip_build_sha,
    summarize,
    summarize_bucket,
)

_DEPTH = 40


def _outcome(base: int | None, reranked: int | None, **overrides: object) -> QuestionOutcome:
    payload: dict = dict(
        id="q1", corpus="leakcanary", base_rank=base, reranked_rank=reranked,
        pool_documents=200, rerank_depth=_DEPTH,
    )
    payload.update(overrides)
    return QuestionOutcome(**payload)


# --------------------------------------------------------------------------------------
# Ground-truth name matching -- the failure that reads as a retrieval collapse
# --------------------------------------------------------------------------------------


def test_the_build_sha_comes_out_and_the_rest_of_the_name_survives() -> None:
    assert strip_build_sha("2015-05-10-c2d938fb-customizable-excludedref.md") == (
        "2015-05-10-customizable-excludedref.md")


def test_a_name_that_never_carried_a_sha_is_left_alone() -> None:
    # Recorded ground truth arrives in this shape. If stripping mangled it, every question would
    # score zero and the run would read as a retrieval collapse rather than a name mismatch --
    # the exact failure `evals/project-memory/README.md` warns about.
    recorded = "2015-05-10-customizable-excludedref.md"
    assert strip_build_sha(recorded) == recorded


def test_a_slug_that_merely_looks_hexadecimal_is_not_mistaken_for_a_sha() -> None:
    # `deadbeef` is eight hex characters and a plausible slug opener. The anchor on the date is
    # what keeps it: only the segment immediately after the date is a candidate.
    assert strip_build_sha("2015-05-10-deadbeef-fix.md") == "2015-05-10-fix.md"
    assert strip_build_sha("2015-05-10-abc123-deadbeef-fix.md") == "2015-05-10-abc123-deadbeef-fix.md"


# --------------------------------------------------------------------------------------
# Passage order -> document order
# --------------------------------------------------------------------------------------


def test_a_document_ranks_where_its_best_passage_ranked() -> None:
    # Three passages, two documents. The reader's experience of `b` is rank 2, not rank 3.
    ranking = document_ranking([
        "corpus/2015-05-10-aaaaaaaa-first.md",
        "corpus/2015-05-11-bbbbbbbb-second.md",
        "corpus/2015-05-10-aaaaaaaa-first.md",
    ])
    assert ranking == ["2015-05-10-first.md", "2015-05-11-second.md"]


def test_rank_is_one_indexed_and_absence_is_none_rather_than_infinity() -> None:
    ranking = ["2015-05-10-first.md", "2015-05-11-second.md"]
    assert rank_of(ranking, "2015-05-10-first.md") == 1
    assert rank_of(ranking, "2015-05-11-second.md") == 2
    # None, never a large number: a document outside the pool was never offered to the reranker,
    # so averaging it in as rank-anything would be inventing an observation.
    assert rank_of(ranking, "2015-05-12-absent.md") is None


# --------------------------------------------------------------------------------------
# The null -- the part the whole analysis rests on
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("base_rank", [1, 2, 20, 40])
def test_inside_the_head_the_null_forgets_where_the_document_started(base_rank: int) -> None:
    # A uniform shuffle of 40 documents lands any of them uniformly on 1..40, so the expectation is
    # 20.5 no matter where it began. This is the whole reason rank-1 questions LOOK harmed.
    assert expected_rank_under_shuffle(base_rank, _DEPTH) == 20.5


def test_past_the_head_the_null_is_the_identity_because_nothing_can_move() -> None:
    # `_rerank_fused` reorders the head and carries the tail untouched, so a document at 41 has an
    # expected rank of exactly 41 -- not 20.5. Getting this wrong would manufacture a finding.
    assert expected_rank_under_shuffle(41, _DEPTH) == 41.0
    assert expected_rank_under_shuffle(120, _DEPTH) == 120.0


def test_a_skill_free_reranker_does_not_beat_the_null() -> None:
    # The guard on the entire analysis. Ten questions that all started at rank 1 and were scattered
    # uniformly across the head average out to the null, so `beats_null` is 0 -- NOT a large
    # negative number that a reader would take as "reranking is harmful here".
    scattered = [_outcome(1, rank, id=f"q{rank}") for rank in range(1, 41, 4)]
    summary = summarize_bucket("1", scattered)
    assert summary.mean_expected_rank == 20.5
    assert summary.mean_reranked_rank == pytest.approx(19.0)
    assert summary.beats_null == pytest.approx(1.5)  # noise-sized, not evidence


def test_a_reranker_with_real_skill_beats_the_null_even_where_it_looks_harmful() -> None:
    # Every one of these questions was HURT in raw terms -- each started at 1 and ended lower. A
    # naive read calls that damage. Against the null they are all far better than chance, which is
    # the distinction this module exists to draw.
    skilled = [_outcome(1, rank, id=f"q{rank}") for rank in (2, 2, 3, 2, 3)]
    summary = summarize_bucket("1", skilled)
    assert summary.hurt == 5 and summary.helped == 0
    assert summary.beats_null == pytest.approx(20.5 - 2.4)


# --------------------------------------------------------------------------------------
# Counting
# --------------------------------------------------------------------------------------


def test_helped_hurt_and_unchanged_partition_the_scored_questions() -> None:
    summary = summarize_bucket("4-10", [
        _outcome(7, 2, id="up"), _outcome(7, 9, id="down"), _outcome(7, 7, id="still"),
    ])
    assert (summary.helped, summary.hurt, summary.unchanged, summary.n) == (1, 1, 1, 3)


def test_a_document_outside_the_pool_is_counted_apart_and_never_averaged_in() -> None:
    # It is a miss for the reader, so it drags overall recall down; it is not an observation about
    # reordering, so it must not enter any bucket or any mean rank.
    summary = summarize("leakcanary", [_outcome(1, 1, id="in"), _outcome(None, None, id="out")])
    assert summary.questions == 2
    assert summary.outside_pool == 1
    assert summary.recall_at_3_before == pytest.approx(0.5)
    assert sum(bucket.n for bucket in summary.buckets) == 1


def test_a_document_the_reranker_dropped_from_the_answer_is_counted_as_evicted() -> None:
    # Measured, not hypothetical: on `lc1124` the shipped ranking returned the correct document at
    # rank 15 of 48 and the reranked one did not return it at all. `search` truncates on
    # `max_extracted_chars` AFTER reranking, and `_rerank_fused` groups every passage of a document
    # together, so the same character budget bought 48 documents unranked and 25 reranked.
    summary = summarize("leakcanary", [
        _outcome(15, None, id="evicted", pool_documents=48, reranked_pool_documents=25),
        _outcome(2, 1, id="kept", pool_documents=48, reranked_pool_documents=25),
    ])
    assert summary.evicted == 1
    assert summary.rescued == 0
    assert summary.mean_pool_documents == 48.0
    assert summary.mean_reranked_pool_documents == 25.0
    # An evicted question is a miss for the reader and must move recall, but it is not an
    # observation about reordering, so it stays out of the buckets.
    assert summary.recall_at_3_after == pytest.approx(0.5)
    assert sum(bucket.n for bucket in summary.buckets) == 1


def test_overall_recall_moves_with_the_reranked_ranks() -> None:
    summary = summarize("egui", [
        _outcome(1, 8, id="a"), _outcome(9, 2, id="b"), _outcome(30, 30, id="c"),
    ])
    assert summary.recall_at_3_before == pytest.approx(1 / 3)
    assert summary.recall_at_3_after == pytest.approx(1 / 3)


def test_buckets_cover_every_reachable_rank() -> None:
    assert [bucket_of(rank) for rank in (1, 2, 3, 4, 10, 11, 40, 41, 200)] == [
        "1", "2-3", "2-3", "4-10", "4-10", "11-40", "11-40", "41+", "41+"]


# === reach: which problem this project actually has ==============================================


def test_a_document_never_ranked_counts_against_every_ceiling() -> None:
    # This is the distinction the whole metric exists to draw. `rank_of` treats `None` as "no
    # observation about reordering" and keeps it out of the averages; here `None` means a caller
    # who looked all the way down would still not have it, so it is a miss at every depth.
    result = reach([1, None, 500], ceilings=[3, 604], total_documents=604)
    assert result.never_ranked == 1
    assert result.within == ((3, 1, pytest.approx(1 / 3)), (604, 2, pytest.approx(2 / 3)))


def test_a_ceiling_deeper_than_the_corpus_is_not_reported() -> None:
    # Printing "top 2120" for a 604-document corpus would invite reading 1.000 as headroom that
    # exists rather than as a count of every document there is.
    assert [ceiling for ceiling, _found, _share in
            reach([1], ceilings=[3, 604, 2120], total_documents=604).within] == [3, 604]


def test_the_median_is_the_figure_that_says_ordering_rather_than_retrieval() -> None:
    # 26 of 604 is an ordering problem wearing a budget's clothes. The metric has to surface it,
    # because `search` truncates at 200 candidates and can only ever say "not in what I returned".
    assert reach([4, 26, 300], ceilings=[10], total_documents=604).median_rank == 26


def test_no_scoreable_question_is_a_none_median_not_a_zero() -> None:
    assert reach([None, None], ceilings=[3], total_documents=604).median_rank is None


# === run_ablation.py: resuming ===================================================================
# A question costs about twenty seconds. Losing a run's accumulated work to an interruption is what
# these guard against -- and it is not hypothetical: the first version of this runner held every
# result in memory until the end, and thirty minutes of measurement died with the process.

from run_ablation import already_scored  # noqa: E402

import json as _json  # noqa: E402
from dataclasses import asdict as _asdict  # noqa: E402


def test_nothing_is_resumed_from_a_run_that_never_started(tmp_path) -> None:
    assert already_scored(tmp_path / "absent.jsonl") == {}


def test_every_row_already_written_is_resumed_by_id(tmp_path) -> None:
    artifact = tmp_path / "partial.jsonl"
    written = [_outcome(3, 1, id="lc1"), _outcome(27, 2, id="lc2")]
    artifact.write_text(
        "\n".join(_json.dumps(_asdict(outcome)) for outcome in written) + "\n", encoding="utf-8")
    resumed = already_scored(artifact)
    assert set(resumed) == {"lc1", "lc2"}
    # Round-trips to the same record, so a resumed run and an uninterrupted one publish identical
    # numbers -- a resume that silently altered a rank would be worse than no resume at all.
    assert resumed["lc2"] == written[1]


def test_a_half_written_final_line_does_not_take_the_resume_down(tmp_path) -> None:
    # The process can die mid-append. The rows before the tear are still good measurements and a
    # resume that refused to read them would throw away exactly what this file exists to protect.
    artifact = tmp_path / "torn.jsonl"
    good = _json.dumps(_asdict(_outcome(3, 1, id="lc1")))
    artifact.write_text(good + "\n" + good[: len(good) // 2], encoding="utf-8")
    assert set(already_scored(artifact)) == {"lc1"}


# === report_ablation.py: the reporter ============================================================

from report_ablation import load as load_ablation, render  # noqa: E402


def test_the_reporter_reads_back_what_the_runner_wrote(tmp_path) -> None:
    artifact = tmp_path / "corpus-ablation.jsonl"
    artifact.write_text(_json.dumps(_asdict(_outcome(27, 2, id="lc2"))) + "\n", encoding="utf-8")
    assert load_ablation(artifact) == [_outcome(27, 2, id="lc2")]


def test_the_table_leads_with_the_null_comparison_not_the_raw_movement() -> None:
    # A reader who reads `moved` on a bucket selected by base rank reads regression to the mean as
    # a finding. The column that carries the claim has to be present and signed.
    rendered = "\n".join(render(summarize("egui", [_outcome(1, 2, id="a"), _outcome(30, 4, id="b")])))
    assert "vs null" in rendered
    assert "+18.50" in rendered  # bucket "1": expected 20.5, observed 2.0
