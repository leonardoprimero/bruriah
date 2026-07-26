"""Asking in one language about a corpus written in another.

BM25 cannot match across languages. Fused at equal weight with a multilingual vector leg it does
not merely fail to help: it contributes rank noise that pushes correct documents out of the top
three, measured at 58% recall@3 for the vector leg alone against 33% for the equal-weight fusion.
These tests pin the discount, the disclosure, and -- most importantly -- the cases where the
discount must NOT apply.
"""
from __future__ import annotations

from pathlib import Path

from bruriah.retrieval import _CROSS_LINGUAL_LEXICAL_WEIGHT, _fuse, search
from test_retrieval import _snapshot_for, embed_query

_PAD = ("The following section describes the behaviour of the service in detail, and it is "
        "written in the same language as the rest of the corpus around it. ") * 4
_PAD_ES = ("La siguiente seccion describe el comportamiento del servicio con detalle, y esta "
           "escrita en el mismo idioma que el resto del corpus que la rodea. ") * 4


def _english_corpus(tmp_path: Path):
    return _snapshot_for(tmp_path, {
        "apple.md": f"# Apple\nWhy we chose the apple pie recipe and what we rejected.\n{_PAD}\n",
        "rocket.md": f"# Rocket\nThe rocket launch decision and the reasoning behind it.\n{_PAD}\n",
    })


# --- the discount itself ---------------------------------------------------------------------


def test_a_spanish_question_against_an_english_corpus_discounts_the_lexical_leg(tmp_path: Path) -> None:
    with _english_corpus(tmp_path) as active:
        outcome = search(active, "por que elegimos la receta de manzana", embed_query=embed_query)
    assert any(item.startswith("lexical_leg_discounted:") for item in outcome.degradation)
    assert "lexical_leg_discounted:es_query_en_corpus" in outcome.degradation


def test_an_english_question_against_an_english_corpus_does_not(tmp_path: Path) -> None:
    with _english_corpus(tmp_path) as active:
        outcome = search(active, "why did we choose the apple pie recipe", embed_query=embed_query)
    assert not any(item.startswith("lexical_leg_discounted:") for item in outcome.degradation)


def test_an_unidentifiable_question_leaves_ranking_alone(tmp_path: Path) -> None:
    """Abstention has to be inert. A detector that guessed here would discount the lexical leg on
    a corpus-language query, which is the one case where that leg is the STRONGER of the two."""
    with _english_corpus(tmp_path) as active:
        outcome = search(active, "sha256:deadbeef rocket", embed_query=embed_query)
    assert not any(item.startswith("lexical_leg_discounted:") for item in outcome.degradation)


def test_a_corpus_with_no_dominant_language_never_discounts(tmp_path: Path) -> None:
    # Half and half: there is no "the corpus language" to mismatch against, so the honest move is
    # to change nothing rather than pick a side and re-rank everyone's results on it.
    with _snapshot_for(tmp_path, {
        "en.md": f"# Apple\nWhy we chose the apple pie recipe.\n{_PAD}\n",
        "es.md": f"# Manzana\nPor que elegimos la receta de tarta de manzana.\n{_PAD_ES}\n",
    }) as active:
        outcome = search(active, "por que elegimos la manzana", embed_query=embed_query)
    assert not any(item.startswith("lexical_leg_discounted:") for item in outcome.degradation)


# --- what the discount is and is not allowed to change ----------------------------------------


def test_the_discount_reweights_evidence_and_never_removes_it(tmp_path: Path) -> None:
    """Both legs still run and both ranks are still reported.

    This is the line the project draws everywhere else: change how much something counts, never
    what the caller is allowed to see. A discount that quietly dropped lexical-only matches would
    make results depend on the ranking rule in a way no caller could inspect.

    The Spanish query here shares an identifier with the corpus, deliberately: with no shared token
    at all the lexical leg returns nothing and the assertion below would pass vacuously."""
    with _snapshot_for(tmp_path, {
        "apple.md": f"# Apple\nWhy we chose the apple recipe, decided in promote_candidate.\n{_PAD}\n",
        "rocket.md": f"# Rocket\nThe rocket launch decision and the reasoning behind it.\n{_PAD}\n",
    }) as active:
        spanish = search(active, "por que elegimos promote_candidate para la receta",
                         embed_query=embed_query)
    assert "lexical_leg_discounted:es_query_en_corpus" in spanish.degradation
    assert any(item.lexical_rank is not None for item in spanish.matches)
    assert any(item.vector_rank is not None for item in spanish.matches)


def test_a_fully_cross_lingual_query_matches_nothing_lexically_and_says_so(tmp_path: Path) -> None:
    """Sharing no token, BM25 returns an empty leg -- which is the whole reason for the discount.

    Worth pinning as its own case: it is the difference between "the leg ran and disagreed" and
    "the leg had nothing to say", and both are reported, never conflated."""
    with _english_corpus(tmp_path) as active:
        outcome = search(active, "por que elegimos la receta de manzana", embed_query=embed_query)
    assert "lexical_leg_no_matches" in outcome.degradation
    assert all(item.lexical_rank is None for item in outcome.matches)
    assert outcome.matches, "the vector leg must still answer"


def test_the_discount_is_disclosed_rather_than_applied_silently(tmp_path: Path) -> None:
    # A caller comparing two result sets is entitled to know the ranking rule was not the same for
    # both. The disclosure names both languages, so the reason is legible without reading the code.
    with _english_corpus(tmp_path) as active:
        outcome = search(active, "por que elegimos la receta de manzana", embed_query=embed_query)
    disclosed = [item for item in outcome.degradation if item.startswith("lexical_leg_discounted:")]
    assert disclosed == ["lexical_leg_discounted:es_query_en_corpus"]


# --- the fusion arithmetic --------------------------------------------------------------------


def test_a_discounted_lexical_leg_cannot_outvote_the_vector_leg() -> None:
    """The property the weight is chosen for: a tiebreaker, not a voter.

    `far` is lexical rank 1 and vector rank 40; `near` is vector rank 1 and lexical rank 40. At
    equal weight these are symmetric and the tie breaks on ref. Discounted, the vector leg wins --
    which is the entire cross-lingual fix, expressed as arithmetic rather than as a benchmark."""
    lexical, vector = {"far": 1, "near": 40}, {"near": 1, "far": 40}
    assert [ref for ref, _, _ in _fuse(lexical, vector, 1.0)] == ["far", "near"]
    assert [ref for ref, _, _ in _fuse(lexical, vector, _CROSS_LINGUAL_LEXICAL_WEIGHT)] == \
        ["near", "far"]


def test_the_leg_still_separates_candidates_the_vector_leg_ranked_together() -> None:
    # Not zero, and this is what the non-zero buys: where the vector leg cannot distinguish two
    # documents, an exact lexical match -- an identifier, a filename -- still decides the order.
    lexical, vector = {"exact": 1}, {"exact": 7, "other": 7}
    assert [ref for ref, _, _ in _fuse(lexical, vector, _CROSS_LINGUAL_LEXICAL_WEIGHT)][0] == "exact"


def test_default_fusion_is_unchanged(tmp_path: Path) -> None:
    """Everything that is not cross-lingual must rank exactly as it did before this existed."""
    lexical, vector = {"a": 1, "b": 3}, {"b": 1, "a": 5}
    unweighted = [(ref, lex, vec) for ref, lex, vec in _fuse(lexical, vector)]
    explicit = [(ref, lex, vec) for ref, lex, vec in _fuse(lexical, vector, 1.0)]
    assert unweighted == explicit
