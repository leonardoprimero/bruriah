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

    # Equal weight: doc1 and doc2 tie on score (1/21 + 1/22), tie broken by ref ("doc1" < "doc2")
    fused = ranking.fuse_ranks(lexical, vector, lexical_weight=1.0)
    assert [ref for ref, _, _ in fused] == ["doc1", "doc2"]

    # Heavily discount lexical leg: doc2 wins because vector_rank 1 dominates
    fused_discounted = ranking.fuse_ranks(lexical, vector, lexical_weight=0.1)
    assert [ref for ref, _, _ in fused_discounted] == ["doc2", "doc1"]


def test_rrf_lower_k_amplifies_rank_advantage() -> None:
    """At K=20, rank 1 would receive a meaningfully larger RRF score than rank 3.

    The literature default of K=60 makes rank-1 and rank-2 nearly indistinguishable
    (0.0164 vs 0.0161). At K=20 the gap widens to 4.6% per leg, which would be enough to
    push the correct document into the top-3 window when the vector leg already places
    it at rank 1-2. This test asserts the arithmetic property with an explicit `rrf_k=20`
    argument to `fuse_ranks`, independent of the module default `ranking.RRF_K` (which
    stays 60 -- see `test_rrf_k_matches_published_measurements`).
    """
    demo_k = 20
    # Exercise the explicit per-call override through the public API, confirming it never
    # reads the module default: `doc1` (rank 1) must out-score `doc3` (rank 3) under `demo_k`.
    fused = ranking.fuse_ranks({"doc1": 1, "doc3": 3}, None, rrf_k=demo_k)
    ranked_refs = [ref for ref, _, _ in fused]
    assert ranked_refs == ["doc1", "doc3"]

    score_rank1 = 1.0 / (demo_k + 1)
    score_rank3 = 1.0 / (demo_k + 3)
    gap_fraction = (score_rank1 - score_rank3) / score_rank1
    # At K=20: (1/21 - 1/23) / (1/21) ≈ 8.7 %. Requires >5% so the test still passes
    # even if K is bumped modestly but still well below 60.
    assert gap_fraction > 0.05, (
        f"Expected rank-1 vs rank-3 gap > 5% of rank-1 score, got {gap_fraction:.1%} "
        f"(demo_k={demo_k}). Increase K tuning or relax the threshold."
    )


def test_rrf_k_matches_published_measurements() -> None:
    """`RRF_K` stays 60: every published recall/MRR number in README.md and
    evals/project-memory/README.md was measured with K=60.

    K=20 was tried on 2026-09-20 (commit 63eeddd) and did not change this project's own
    twelve-question own-history recall@3 (English 0.750, Spanish 0.500 either way). The
    external corpora (leakcanary, egui) were never measured at K=20, so the change had no
    supporting evidence outside a sample too small to move on its own. Restore 20 only
    after `evals/retrieval/run_ablation.py --rrf-k 20` shows a measured gain on those
    external corpora.
    """
    assert ranking.RRF_K == 60
