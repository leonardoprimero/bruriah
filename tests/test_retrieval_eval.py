"""Unit tests for the retrieval-quality eval harness under `evals/retrieval/`.

Covers:
  - `metrics.py`'s pure functions against synthetic ranked lists with
    hand-computed expected values.
  - `adapters.py`'s note-level dedup logic against synthetic chunk hits.
  - A read-only guard: hashing the live legacy `cerebro.db` before/after a
    real `LegacyAdapter.search()` call must show no change.

Any test that needs a real built index (the legacy `cerebro.db` or the
`bruriah` active snapshot) SKIPS gracefully when that index is absent,
so this suite passes on a fresh checkout with no indexes built.
"""
from __future__ import annotations

import hashlib
import math
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALS_RETRIEVAL = REPOSITORY_ROOT / "evals" / "retrieval"
LEGACY_DATABASE = REPOSITORY_ROOT / "cerebro.db"

if str(EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(EVALS_RETRIEVAL))

from adapters import ChunkHit, LegacyAdapter, RouterAdapter, SearchResult, dedup_to_notes  # noqa: E402
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
from separation import MAD_TO_SIGMA, separation  # noqa: E402


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# recall_at_k
# ---------------------------------------------------------------------------


def test_recall_at_k_all_found() -> None:
    ranked = ["a", "b", "c", "d", "e"]
    assert recall_at_k(ranked, ["c"], k=5) == 1.0


def test_recall_at_k_outside_k_window() -> None:
    ranked = ["a", "b", "c", "d", "e"]
    assert recall_at_k(ranked, ["c"], k=2) == 0.0


def test_recall_at_k_partial_match() -> None:
    ranked = ["a", "b", "c", "d", "e"]
    assert recall_at_k(ranked, ["c", "z"], k=5) == 0.5


def test_recall_at_k_empty_must_include_is_none() -> None:
    ranked = ["a", "b", "c"]
    assert recall_at_k(ranked, [], k=5) is None


# ---------------------------------------------------------------------------
# mrr_at_10
# ---------------------------------------------------------------------------


def test_mrr_at_10_first_rank() -> None:
    assert mrr_at_10(["a", "b", "c"], ["a"]) == 1.0


def test_mrr_at_10_second_rank() -> None:
    assert mrr_at_10(["x", "a", "y"], ["a"]) == pytest.approx(0.5)


def test_mrr_at_10_miss_within_top_10() -> None:
    ranked = [f"note{i}" for i in range(10)]
    assert mrr_at_10(ranked, ["not_present"]) == 0.0


def test_mrr_at_10_miss_only_found_after_rank_10() -> None:
    ranked = [f"note{i}" for i in range(10)] + ["target"]
    assert mrr_at_10(ranked, ["target"]) == 0.0


def test_mrr_at_10_empty_must_include_is_none() -> None:
    assert mrr_at_10(["a", "b"], []) is None


def test_mrr_at_10_picks_earliest_of_multiple_relevant_notes() -> None:
    assert mrr_at_10(["x", "a", "b"], ["b", "a"]) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# exact_name_recall_at_3
# ---------------------------------------------------------------------------


def test_exact_name_recall_at_3_hit_within_window() -> None:
    assert exact_name_recall_at_3(["a", "b", "c", "d"], ["c"]) == 1.0


def test_exact_name_recall_at_3_miss_outside_window() -> None:
    assert exact_name_recall_at_3(["a", "b", "c", "d"], ["d"]) == 0.0


# ---------------------------------------------------------------------------
# ndcg_at_10 -- expected values derived independently from the graded-nDCG
# definition (relevance 2/1/0, log2(rank+1) discount, IDCG from the ideal
# ordering) rather than by re-calling the function under test.
# ---------------------------------------------------------------------------


def test_ndcg_at_10_perfect_ranking_is_one() -> None:
    assert ndcg_at_10(["a", "b"], must_include=["a"], acceptable=[]) == pytest.approx(1.0)


def test_ndcg_at_10_relevant_note_below_rank_one() -> None:
    expected_dcg = 2 / math.log2(3)
    expected_idcg = 2 / math.log2(2)
    assert ndcg_at_10(["x", "a"], must_include=["a"], acceptable=[]) == pytest.approx(
        expected_dcg / expected_idcg
    )


def test_ndcg_at_10_graded_must_include_and_acceptable() -> None:
    # rel(a)=0, rel(b)=2 (must_include), rel(c)=1 (acceptable)
    expected_dcg = 0 / math.log2(2) + 2 / math.log2(3) + 1 / math.log2(4)
    # Ideal ordering: [2, 1] (one must_include note, one acceptable note)
    expected_idcg = 2 / math.log2(2) + 1 / math.log2(3)
    assert ndcg_at_10(["a", "b", "c"], must_include=["b"], acceptable=["c"]) == pytest.approx(
        expected_dcg / expected_idcg
    )


def test_ndcg_at_10_acceptable_notes_do_not_double_count_must_include() -> None:
    # "acceptable" containing the same note as "must_include" must not inflate IDCG.
    expected_dcg = 2 / math.log2(2)
    expected_idcg = 2 / math.log2(2)
    assert ndcg_at_10(["a"], must_include=["a"], acceptable=["a"]) == pytest.approx(
        expected_dcg / expected_idcg
    )


def test_ndcg_at_10_no_ground_truth_is_none() -> None:
    assert ndcg_at_10(["a", "b"], must_include=[], acceptable=[]) is None


def test_ndcg_at_10_no_hits_is_zero() -> None:
    assert ndcg_at_10(["x", "y"], must_include=["a"], acceptable=[]) == 0.0


# ---------------------------------------------------------------------------
# cross_lingual_recall_delta
# ---------------------------------------------------------------------------


def test_cross_lingual_recall_delta_regression() -> None:
    delta = cross_lingual_recall_delta([0.5, 0.7], [0.9, 1.0])
    assert delta == pytest.approx(0.6 - 0.95)


def test_cross_lingual_recall_delta_empty_is_none() -> None:
    assert cross_lingual_recall_delta([], [0.9]) is None
    assert cross_lingual_recall_delta([0.9], []) is None


# ---------------------------------------------------------------------------
# ambiguous_diversity
# ---------------------------------------------------------------------------


def test_ambiguous_diversity_counts_distinct_notes() -> None:
    assert ambiguous_diversity(["a", "b", "a", "c"], k=10) == 3


def test_ambiguous_diversity_respects_k_window() -> None:
    assert ambiguous_diversity(["a", "b", "a", "c"], k=3) == 2


# ---------------------------------------------------------------------------
# abstention_score_separation
# ---------------------------------------------------------------------------


def test_abstention_score_separation_hand_computed() -> None:
    abstention_scores = [0.1, 0.2, 0.9]
    positive_scores = [0.5, 0.6, 0.7, 0.8]
    result = abstention_score_separation(abstention_scores, positive_scores)
    # p25 of [0.5, 0.6, 0.7, 0.8] via linear interpolation: rank = 0.75 * 3 = 0.75
    # -> 0.5 + (0.6 - 0.5) * 0.75 = 0.575
    assert result.positive_p25_top1 == pytest.approx(0.575)
    assert result.abstention_median_top1 == pytest.approx(0.2)
    assert result.positive_median_top1 == pytest.approx(0.65)
    # Below p25 (0.575): 0.1 and 0.2 -> 2/3
    assert result.below_p25_fraction == pytest.approx(2 / 3)
    assert result.abstention_count == 3
    assert result.positive_count == 4


def test_abstention_score_separation_empty_side_is_none() -> None:
    result = abstention_score_separation([], [0.5])
    assert result.abstention_median_top1 is None
    assert result.below_p25_fraction is None
    assert result.abstention_count == 0
    assert result.positive_count == 1


# ---------------------------------------------------------------------------
# separation
# ---------------------------------------------------------------------------

# Median 1.0, MAD 0.1: values 0.8, 0.9, 1.0, 1.1, 1.2 repeated four times.
_NOISE = [1.0 + 0.1 * ((index % 5) - 2) for index in range(20)]


def test_separation_hand_computed() -> None:
    # rest = _NOISE, median 1.0, MAD 0.1 -> (5.0 - 1.0) / (1.4826 * 0.1)
    assert separation([5.0, *_NOISE]) == pytest.approx(4.0 / (MAD_TO_SIGMA * 0.1))


def test_separation_is_scale_free() -> None:
    base = separation([5.0, *_NOISE])
    assert separation([500.0, *[value * 100 for value in _NOISE]]) == pytest.approx(base)
    assert separation([5.5, *[value + 0.5 for value in _NOISE]]) == pytest.approx(base)


def test_separation_is_scale_free_but_not_shape_free() -> None:
    """The limit that ended this line of work, kept as a test so it cannot be re-assumed.

    Scale-invariance is what made a single threshold look like it could serve every embedding
    model. It cannot, because a corpus does not rescale a distribution -- it RESHAPES one. Here
    the same top score against equally-centred distributions of different width reads five times
    apart, and neither is a rescaling of the other. Measured on real corpora this dominated: the
    denser of two corpora separated more for every question, including questions whose answer it
    did not contain. See evals/project-memory/README.md.
    """
    tight = [1.0 + 0.1 * ((index % 5) - 2) for index in range(20)]  # MAD 0.1
    wide = [1.0 + 0.5 * ((index % 5) - 2) for index in range(20)]   # MAD 0.5, same median
    assert separation([5.0, *tight]) == pytest.approx(5 * separation([5.0, *wide]))


def test_separation_survives_a_query_with_several_good_answers() -> None:
    # WHY MEDIAN AND MAD. A query with three good answers would inflate a standard deviation with
    # exactly the scores that make it a good query, and then read as LESS separated than a lucky
    # single hit. Asserting the robust form holds is not enough, so the naive one is computed here
    # to show it moves where the robust one does not.
    def naive_z(values: list[float]) -> float:
        rest = values[1:]
        mean = sum(rest) / len(rest)
        spread = (sum((value - mean) ** 2 for value in rest) / len(rest)) ** 0.5
        return (values[0] - mean) / spread

    assert separation([5.0, *_NOISE]) == pytest.approx(separation([5.0, 4.8, 4.6, *_NOISE]))
    assert naive_z([5.0, 4.8, 4.6, *_NOISE]) < naive_z([5.0, *_NOISE]) / 2


def test_separation_tells_a_peak_from_a_plateau() -> None:
    peak, plateau = separation([5.0, *_NOISE]), separation([1.25, *_NOISE])
    assert peak is not None and plateau is not None and peak > plateau


def test_separation_is_undefined_rather_than_zero_when_it_cannot_be_asked() -> None:
    # `None`, never 0.0, for recall_at_k's reason: an undefined measurement averaged in as a zero
    # is a fabricated observation. A distribution with no spread at all is what a constant-vector
    # embedder produces, so this is a real path rather than a corner.
    assert separation([5.0, 1.0, 1.0]) is None
    assert separation([5.0, *[1.0] * 20]) is None
    assert separation([]) is None


# ---------------------------------------------------------------------------
# mean_or_none
# ---------------------------------------------------------------------------


def test_mean_or_none_skips_none_values() -> None:
    assert mean_or_none([1.0, None, 3.0]) == pytest.approx(2.0)


def test_mean_or_none_all_none_is_none() -> None:
    assert mean_or_none([None, None]) is None


# ---------------------------------------------------------------------------
# dedup_to_notes (adapters.py) -- note-level dedup
# ---------------------------------------------------------------------------


def test_dedup_to_notes_keeps_best_rank_and_drops_repeats() -> None:
    chunk_hits = [
        ChunkHit(relative_path="noteA.md", score=0.9, via="ambos"),
        ChunkHit(relative_path="noteB.md", score=0.8, via="bm25"),
        ChunkHit(relative_path="noteA.md", score=0.5, via="vector"),
        ChunkHit(relative_path="noteC.md", score=0.3, via="bm25"),
    ]
    result = dedup_to_notes(chunk_hits, k=10)
    assert result == [
        SearchResult(relative_path="noteA.md", score=0.9, via="ambos", rank=1),
        SearchResult(relative_path="noteB.md", score=0.8, via="bm25", rank=2),
        SearchResult(relative_path="noteC.md", score=0.3, via="bm25", rank=3),
    ]


def test_dedup_to_notes_truncates_to_k_notes() -> None:
    chunk_hits = [
        ChunkHit(relative_path="noteA.md", score=0.9, via="bm25"),
        ChunkHit(relative_path="noteB.md", score=0.8, via="bm25"),
        ChunkHit(relative_path="noteC.md", score=0.7, via="bm25"),
    ]
    result = dedup_to_notes(chunk_hits, k=2)
    assert [item.relative_path for item in result] == ["noteA.md", "noteB.md"]


def test_dedup_to_notes_empty_input() -> None:
    assert dedup_to_notes([], k=10) == []


# ---------------------------------------------------------------------------
# Read-only guard: real engines must never mutate their database on search.
# ---------------------------------------------------------------------------


def test_legacy_adapter_search_does_not_mutate_database() -> None:
    if not LEGACY_DATABASE.is_file():
        pytest.skip("legacy cerebro.db is not present in this checkout")
    before = _hash_file(LEGACY_DATABASE)
    adapter = LegacyAdapter()
    adapter.search("token bucket rate limiting", k=5)
    after = _hash_file(LEGACY_DATABASE)
    assert before == after


def test_legacy_adapter_returns_note_level_results() -> None:
    if not LEGACY_DATABASE.is_file():
        pytest.skip("legacy cerebro.db is not present in this checkout")
    adapter = LegacyAdapter()
    results = adapter.search("rate limiting token bucket", k=5)
    assert results, "expected at least one result from the live legacy index"
    paths = [result.relative_path for result in results]
    assert len(paths) == len(set(paths)), "note-level results must be deduped"
    ranks = [result.rank for result in results]
    assert ranks == sorted(ranks), "results must be rank-ordered"


def test_router_adapter_returns_note_level_results() -> None:
    try:
        adapter = RouterAdapter()
    except Exception:  # noqa: BLE001 -- any failure to load a real snapshot means "absent"
        pytest.skip("bruriah active snapshot is not present in this environment")
    try:
        results = adapter.search("rate limiting token bucket", k=5)
    finally:
        adapter.close()
    if not results:
        pytest.skip("router snapshot returned no results (empty/unbuilt index)")
    paths = [result.relative_path for result in results]
    assert len(paths) == len(set(paths)), "note-level results must be deduped"
    ranks = [result.rank for result in results]
    assert ranks == sorted(ranks), "results must be rank-ordered"


def test_router_adapter_wires_a_real_query_embedder_post_bugfix() -> None:
    """Bugfix regression: `RouterAdapter` now builds its deps via `cli.build_serve_deps` (the
    exact function `serve` calls), which always constructs and injects a real query embedder --
    `degradation_supported` must be `True`, never the pre-fix always-`False`."""
    try:
        adapter = RouterAdapter()
    except Exception:  # noqa: BLE001 -- any failure to load a real snapshot means "absent"
        pytest.skip("bruriah active snapshot is not present in this environment")
    try:
        assert adapter.degradation_supported is True
    finally:
        adapter.close()
