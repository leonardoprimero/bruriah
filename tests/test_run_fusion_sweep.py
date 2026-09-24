# The sweep compares ranking rules that differ only in how passage evidence becomes a document
# order, so the tests here pin the one property each rule is defined by: `first` is the shipped
# first-occurrence collapse, `sum` lets a document's second passage count, and `doc_rrf` credits a
# document whose lexical and vector matches land in DIFFERENT passages.
from __future__ import annotations

import sys
from pathlib import Path

EVALS_RETRIEVAL = Path(__file__).resolve().parents[1] / "evals" / "retrieval"
if str(EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(EVALS_RETRIEVAL))

from run_fusion_sweep import Condition, Legs, document_order, evaluate, rank_of  # noqa: E402

# Two documents. A has one passage (a1) and B has two (b1, b2).
_DOC_OF = {"a1": "A.md", "b1": "B.md", "b2": "B.md"}


def _legs(lexical: dict[str, int], vector: dict[str, int], truth: str = "B.md") -> Legs:
    return Legs("q", truth, lexical, vector, 1.0)


def test_first_is_the_best_passage_and_ignores_the_rest() -> None:
    # a1 ranks first in both legs; b1 and b2 are second and third in both. Under `first`, B is
    # scored by b1 alone, so A stays ahead however many passages B matched.
    legs = _legs({"a1": 1, "b1": 2, "b2": 3}, {"a1": 1, "b1": 2, "b2": 3})
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60)) == ["A.md", "B.md"]


def test_sum_lets_a_second_matching_passage_overtake() -> None:
    # Same legs as above: 2/(60+1) for A against 2/(60+2) + 2/(60+3) for B under `sum`.
    legs = _legs({"a1": 1, "b1": 2, "b2": 3}, {"a1": 1, "b1": 2, "b2": 3})
    assert document_order(legs, _DOC_OF, Condition("sum", 1.0, 60)) == ["B.md", "A.md"]


def test_doc_rrf_credits_matches_that_land_in_different_passages() -> None:
    # B's lexical best is b1 and its vector best is b2. Passage-level fusion splits that credit
    # across two passages (each gets half), so A -- second in both legs on ONE passage -- wins under
    # `first`. Collapsing each leg to documents before fusing gives B rank 1 in both legs.
    legs = _legs({"b1": 1, "a1": 2}, {"b2": 1, "a1": 2})
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60)) == ["A.md", "B.md"]
    assert document_order(legs, _DOC_OF, Condition("doc_rrf", 1.0, 60)) == ["B.md", "A.md"]


def test_lexical_weight_scales_only_the_lexical_leg() -> None:
    # A wins lexically, B wins on vectors, symmetric ranks. At weight 1.0 they tie and the name
    # breaks it; at weight 0.5 the vector leg decides and B wins.
    legs = _legs({"a1": 1, "b1": 2}, {"b1": 1, "a1": 2})
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60)) == ["A.md", "B.md"]
    assert document_order(legs, _DOC_OF, Condition("first", 0.5, 60)) == ["B.md", "A.md"]


def test_a_missing_leg_is_zero_not_an_error() -> None:
    legs = Legs("q", "B.md", None, {"b1": 1, "a1": 2}, 1.0)
    assert document_order(legs, _DOC_OF, Condition("doc_rrf", 1.0, 60)) == ["B.md", "A.md"]


def test_title_only_orders_by_the_title_leg_alone() -> None:
    legs = Legs("q", "B.md", {"a1": 1, "b1": 2}, {"a1": 1, "b1": 2}, 1.0, title={"B.md": 1, "A.md": 2})
    assert document_order(legs, _DOC_OF, Condition("title", 1.0, 60)) == ["B.md", "A.md"]


def test_title_leg_fuses_at_document_level_and_a_zero_weight_is_the_base_order() -> None:
    # Passage legs put A first; the title leg puts B first. At equal weight they tie on RRF and the
    # name breaks it; at weight 2.0 the title leg decides. Weight 0.0 must be the base order exactly.
    legs = Legs("q", "B.md", {"a1": 1, "b1": 2}, {"a1": 1, "b1": 2}, 1.0, title={"B.md": 1, "A.md": 2})
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60, 0.0)) == ["A.md", "B.md"]
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60, 1.0)) == ["A.md", "B.md"]
    assert document_order(legs, _DOC_OF, Condition("first", 1.0, 60, 2.0)) == ["B.md", "A.md"]


def test_condition_label_names_the_title_weight_only_when_set() -> None:
    assert Condition("first", 1.0, 60).label == Condition("first", 1.0, 60, 0.0).label
    assert Condition("first", 1.0, 60, 0.5).label.endswith("title=0.5")


def test_rank_of_is_one_indexed_and_none_when_absent() -> None:
    assert rank_of(["A.md", "B.md"], "B.md") == 2
    assert rank_of(["A.md"], "B.md") is None


def test_evaluate_counts_movement_into_and_out_of_the_top_three() -> None:
    # Two questions. Under the shipped ranks the first is a hit (rank 1) and the second a miss
    # (rank 5). The condition inverts both, so exactly one entered and one left.
    hit_then_miss = [
        Legs("q1", "A.md", {"a1": 1, "b1": 2}, {"a1": 1, "b1": 2}, 1.0),
        Legs("q2", "B.md", {"a1": 1, "b1": 2}, {"a1": 1, "b1": 2}, 1.0),
    ]
    # `first` puts A first for both questions: q1 hit, q2 rank 2 (still a hit). Use shipped ranks
    # that disagree on purpose to exercise the bookkeeping rather than the ranking.
    row = evaluate(Condition("first", 1.0, 60), hit_then_miss, _DOC_OF, shipped=[5, 1])
    assert row.ranks == (1, 2)
    assert (row.entered, row.left) == (1, 0)
    assert row.recall3 == 1.0
