from __future__ import annotations

import struct
from bruriah import ranking


def test_ranked_orders_descending_with_ref_tiebreaker() -> None:
    scored = [
        (1.5, "doc_b"),
        (2.0, "doc_a"),
        (1.5, "doc_c"),
        (0.5, "doc_d"),
    ]
    ranks = ranking.ranked(scored)
    assert ranks == {
        "doc_a": 1,
        "doc_b": 2,  # 1.5 score, tie broken by "doc_b" < "doc_c"
        "doc_c": 3,
        "doc_d": 4,
    }


def test_floats_decoding() -> None:
    original = [1.0, -2.5, 3.14159]
    blob = struct.pack(f"{len(original)}f", *original)
    decoded = ranking.floats(blob)
    assert decoded is not None
    assert len(decoded) == 3
    assert abs(decoded[0] - 1.0) < 1e-5
    assert abs(decoded[1] - (-2.5)) < 1e-5
    assert abs(decoded[2] - 3.14159) < 1e-5

    # Invalid byte length for 32-bit floats
    assert ranking.floats(b"123") is None


def test_bm25_scores_from_tokens() -> None:
    tokenized = [
        ("clean", "architecture", "python"),
        ("spaghetti", "code", "everywhere"),
        ("clean", "code", "principles"),
    ]
    refs = ["doc1", "doc2", "doc3"]
    query = ("clean", "architecture")

    ranks, stopped = ranking.bm25_scores_from_tokens(
        tokenized=tokenized,
        refs=refs,
        query_tokens=query,
        deadline=10.0,
        clock=lambda: 0.0,
    )

    assert not stopped
    assert ranks is not None
    assert ranks["doc1"] == 1  # Matches both "clean" and "architecture"
    assert ranks["doc3"] == 2  # Matches only "clean"
    assert "doc2" not in ranks  # No matching terms


def test_bm25_scores_from_tokens_enforces_deadline() -> None:
    tokenized = [("clean", "code")] * 100
    refs = [f"doc{i}" for i in range(100)]
    query = ("clean",)

    ranks, stopped = ranking.bm25_scores_from_tokens(
        tokenized=tokenized,
        refs=refs,
        query_tokens=query,
        deadline=0.0,
        clock=lambda: 1.0,
    )

    assert stopped
    assert ranks == {}


def test_bm25_scores_from_postings() -> None:
    total_docs = 2
    avg_len = 3.0
    dfs = {"clean": 2, "architecture": 1}
    postings = [
        ("architecture", "doc1", 1, 3),
        ("clean", "doc1", 1, 3),
        ("clean", "doc2", 1, 3),
    ]

    ranks, stopped = ranking.bm25_scores_from_postings(
        total_documents=total_docs,
        average_length=avg_len,
        dfs=dfs,
        postings=postings,
        deadline=10.0,
        clock=lambda: 0.0,
    )

    assert not stopped
    assert ranks is not None
    assert ranks["doc1"] == 1
    assert ranks["doc2"] == 2


def test_vector_ranks_cosine_similarity() -> None:
    def make_vec(*values: float) -> bytes:
        return struct.pack(f"{len(values)}f", *values)

    query = make_vec(1.0, 0.0, 0.0)
    items = [
        ("doc_orthogonal", make_vec(0.0, 1.0, 0.0)),
        ("doc_exact", make_vec(2.0, 0.0, 0.0)),
        ("doc_close", make_vec(0.7, 0.7, 0.0)),
    ]

    ranks, stopped = ranking.vector_ranks(
        items=items,
        query_vector=query,
        deadline=10.0,
        clock=lambda: 0.0,
    )

    assert not stopped
    assert ranks is not None
    assert ranks["doc_exact"] == 1
    assert ranks["doc_close"] == 2
    assert ranks["doc_orthogonal"] == 3


def test_fuse_ranks_rrf() -> None:
    lexical = {"doc1": 1, "doc2": 2}
    vector = {"doc2": 1, "doc1": 2}

    # Equal weight: doc1 and doc2 tie on score (1/61 + 1/62), tie broken by ref ("doc1" < "doc2")
    fused = ranking.fuse_ranks(lexical, vector, lexical_weight=1.0)
    assert [ref for ref, _, _ in fused] == ["doc1", "doc2"]

    # Heavily discount lexical leg: doc2 wins because vector_rank 1 dominates
    fused_discounted = ranking.fuse_ranks(lexical, vector, lexical_weight=0.1)
    assert [ref for ref, _, _ in fused_discounted] == ["doc2", "doc1"]
