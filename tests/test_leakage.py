# Tests for `evals/retrieval/leakage.py` -- the measurement that says whether a retrieval question
# was written by reading its own answer.
#
# The properties worth pinning here are not "does the arithmetic run". They are the three claims
# the module's docstring makes and that a reader is being asked to trust: that a question restating
# its answer scores near the top, that one sharing only common words scores near zero, and that
# `peak` does not move when common words are added or removed -- which is what makes it safe to
# gate on. The last one is the reason the module ships without a stopword list, so it is asserted
# rather than described.
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

EVALS_RETRIEVAL = Path(__file__).resolve().parents[1] / "evals" / "retrieval"
if str(EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(EVALS_RETRIEVAL))

from leakage import Scored, TermStatistics, leakage, stratify, terms  # noqa: E402

# A corpus where one term is genuinely distinctive (`sqlitevec`, one document) and others are
# ordinary (`the`, every document). Written out rather than generated so the expected IDF ordering
# is legible from the fixture itself.
CORPUS = [
    "the pointer primitives were extracted so a second artifact kind could reuse the guarantees",
    "the approval record binds to the digest because a version label can be reissued",
    "the skill ceiling defaults to five and the cut is alphabetical",
    "the lexical leg is discounted when the query language and the corpus language differ",
    "the retrieval leg reads float blobs and never used sqlitevec",
]


@pytest.fixture
def statistics() -> TermStatistics:
    return TermStatistics.over(CORPUS)


def test_terms_keeps_everything_and_preserves_order() -> None:
    # No stoplist and no length floor: the module's central design decision, and the thing a future
    # reader is most likely to "helpfully" add back. Order and duplicates are preserved so a caller
    # can still compute term frequencies.
    assert terms("Why is the THE ceiling five?") == ["why", "is", "the", "the", "ceiling", "five"]


def test_terms_keeps_accented_words_whole() -> None:
    # A tokeniser that split at the accent would report Spanish questions as leaking less than they
    # do, which would flatter exactly the question set this measurement exists to be sceptical of.
    assert terms("por qué la promoción atómica") == ["por", "qué", "la", "promoción", "atómica"]


def test_a_term_in_every_document_cannot_leak(statistics: TermStatistics) -> None:
    # This is what removes the need for a stopword list: IDF already scores such a term at zero.
    assert statistics.idf("the") == pytest.approx(0.0, abs=1e-12)


def test_a_rarer_term_outranks_a_common_one(statistics: TermStatistics) -> None:
    assert statistics.idf("sqlitevec") > statistics.idf("the")


def test_restating_the_answer_scores_far_above_asking_from_its_consequence(
    statistics: TermStatistics,
) -> None:
    """The property the measurement exists to detect, as a paired comparison.

    Both questions have the SAME ground-truth document, so difficulty is held fixed and the only
    thing that varies is how much of the answer's vocabulary the question hands over. Asserting the
    gap rather than an absolute threshold is deliberate: absolute `share` depends on tokenisation
    and on corpus size, and a test that pinned it would be pinning an artefact of this fixture.
    """
    answer = CORPUS[0]
    restated = leakage("why were the pointer primitives extracted", answer, statistics)
    consequence = leakage("how can two kinds of thing share one crash safety promise", answer, statistics)

    assert restated.share is not None and consequence.share is not None
    assert restated.share > consequence.share + 0.4
    assert restated.peak is not None and consequence.peak is not None
    assert restated.peak > consequence.peak

    # A reviewer acting on a flagged question needs to see WHICH words gave it away.
    assert {"pointer", "primitives", "extracted"} <= set(restated.leaked)


def test_a_question_sharing_only_common_words_scores_near_zero(statistics: TermStatistics) -> None:
    result = leakage("the the the", CORPUS[0], statistics)
    assert result.share == pytest.approx(0.0)
    assert result.peak == pytest.approx(0.0)


def test_a_question_sharing_nothing_scores_zero_not_none(statistics: TermStatistics) -> None:
    # Zero is a measurement; None means the question could not be measured. Conflating them would
    # let an unmeasurable question pass as a perfectly clean one.
    result = leakage("kubernetes helm chart rollout", CORPUS[0], statistics)
    assert result.share == pytest.approx(0.0)
    assert result.peak == pytest.approx(0.0)
    assert result.leaked == ()


def test_an_empty_question_is_undefined_rather_than_clean(statistics: TermStatistics) -> None:
    result = leakage("   ", CORPUS[0], statistics)
    assert result.share is None and result.peak is None


def test_peak_is_unchanged_by_adding_common_words(statistics: TermStatistics) -> None:
    # The property that makes `peak` the number to gate on, and the reason no stopword list ships:
    # padding a question with words the corpus considers ordinary must not move it. `share` may
    # move -- it is a ratio over every term -- which is exactly why `share` is documented as
    # descriptive and `peak` as the gate.
    answer = CORPUS[1]
    bare = leakage("digest approval", answer, statistics)
    padded = leakage("so the a is it that of digest approval the the", answer, statistics)
    assert padded.peak == pytest.approx(bare.peak)


def test_peak_is_expressed_against_what_this_corpus_can_produce(statistics: TermStatistics) -> None:
    # Normalised by the ceiling so a figure from a small corpus is comparable to one from a large
    # corpus -- otherwise every number would silently mean something different per repository.
    assert statistics.ceiling == pytest.approx(math.log(len(CORPUS) + 1))
    result = leakage("sqlitevec", CORPUS[4], statistics)
    assert result.peak is not None and 0.0 < result.peak <= 1.0


def test_stratify_separates_the_bands_that_hide_behind_an_average() -> None:
    # The case the whole module exists for: a set whose mean recall looks respectable while every
    # success sits in the band that gave the answer away.
    rows = [
        Scored("h1", peak=0.9, hit_at_3=True, reciprocal_rank=1.0),
        Scored("h2", peak=0.8, hit_at_3=True, reciprocal_rank=1.0),
        Scored("l1", peak=0.1, hit_at_3=False, reciprocal_rank=0.0),
        Scored("l2", peak=0.0, hit_at_3=False, reciprocal_rank=0.2),
    ]
    high, low = stratify(rows, threshold=0.5)
    assert (high.questions, high.recall_at_3) == (2, 1.0)
    assert (low.questions, low.recall_at_3) == (2, 0.0)
    assert low.mrr_at_10 == pytest.approx(0.1)


def test_stratify_reports_an_empty_band_as_none_not_zero() -> None:
    rows = [Scored("h1", peak=0.9, hit_at_3=True, reciprocal_rank=1.0)]
    high, low = stratify(rows, threshold=0.5)
    assert high.questions == 1
    assert (low.questions, low.recall_at_3, low.mrr_at_10) == (0, None, None)


def test_stratify_excludes_unmeasurable_questions_from_both_bands() -> None:
    # Treating `None` as zero would file an unmeasurable question into the low-leakage band, which
    # is the band this measurement exists to keep honest.
    rows = [
        Scored("ok", peak=0.9, hit_at_3=True, reciprocal_rank=1.0),
        Scored("unmeasurable", peak=None, hit_at_3=True, reciprocal_rank=1.0),
    ]
    high, low = stratify(rows, threshold=0.5)
    assert high.questions == 1
    assert low.questions == 0


# === report_leakage.py: the runner ===============================================================
# The pure module above is well covered and the runner was not, which is backwards: the runner holds
# the two things that actually break. The sha-stripping rule is the trap this repository has already
# documented twice -- ground truth carries no sha because a sha does not survive a history rewrite,
# so a corpus filename must be stripped before it can match, and forgetting it reports every
# question as unmatched and looks like a corpus problem rather than a naming one. And the gate's
# exit code is the part a CI job would depend on, which is worth nothing if it cannot fail.

from report_leakage import canonical, main as report_main  # noqa: E402


def test_canonical_strips_the_generators_sha() -> None:
    assert canonical("2026-07-25-0af3d9c2-fix-make-skills-reach-the-server.md") == (
        "2026-07-25-fix-make-skills-reach-the-server.md"
    )


def test_canonical_leaves_an_already_stripped_name_alone() -> None:
    # Idempotent, because ground truth is recorded in the stripped form and both sides of the
    # comparison run through this.
    stripped = "2026-07-25-fix-make-skills-reach-the-server.md"
    assert canonical(stripped) == stripped


def test_canonical_does_not_mistake_a_slug_for_a_sha() -> None:
    # `deadbeef` is eight hex characters AND a plausible slug start. The rule only applies directly
    # after the date, which is where the generator puts it.
    assert canonical("2026-07-25-deadbeef-cafe-slug.md") == "2026-07-25-cafe-slug.md"
    assert canonical("not-dated-deadbeef-slug.md") == "not-dated-deadbeef-slug.md"


def _write_fixture(root: Path, *, leaky: bool) -> tuple[Path, Path]:
    """A two-document corpus and a one-question set, with the question either restating its answer
    or asking from a consequence. Written with a sha in the filename and referenced without one, so
    the stripping rule is exercised rather than bypassed."""
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "2026-07-25-0af3d9c2-extract-the-pointer-primitives.md").write_text(
        "the pointer primitives moved so a second artifact kind reuses the guarantees",
        encoding="utf-8",
    )
    (corpus / "2026-07-26-1b2c3d4e-discount-the-lexical-leg.md").write_text(
        "the lexical leg is discounted when the query language differs from the corpus",
        encoding="utf-8",
    )
    question = (
        "why were the pointer primitives moved" if leaky
        else "how do two kinds of thing share one promise"
    )
    questions = root / "questions.jsonl"
    questions.write_text(
        json.dumps({
            "id": "q01", "query": question, "language": "en", "type": "factual",
            "ground_truth": {
                "must_include": ["2026-07-25-extract-the-pointer-primitives.md"],
                "acceptable": [],
            },
        }) + "\n",
        encoding="utf-8",
    )
    return corpus, questions


def test_the_runner_matches_ground_truth_across_the_sha(tmp_path: Path, capsys) -> None:
    corpus, questions = _write_fixture(tmp_path, leaky=True)
    assert report_main(["--corpus", str(corpus), "--questions", str(questions), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["unmatched_ground_truth"] == []
    assert payload["summary"]["questions"] == 1
    assert payload["questions"][0]["peak"] > 0.0


def test_the_gate_fails_and_names_the_offending_questions(tmp_path: Path, capsys) -> None:
    corpus, questions = _write_fixture(tmp_path, leaky=True)
    assert report_main(["--corpus", str(corpus), "--questions", str(questions), "--gate", "0.1"]) == 1
    assert "q01" in capsys.readouterr().err


def test_the_gate_passes_a_question_that_does_not_reach_the_threshold(tmp_path: Path, capsys) -> None:
    # A gate that only ever fails is not a gate. Same corpus, same target, a question asked from
    # the consequence instead of restated.
    corpus, questions = _write_fixture(tmp_path, leaky=False)
    assert report_main(["--corpus", str(corpus), "--questions", str(questions), "--gate", "0.6"]) == 0
    assert "OK" in capsys.readouterr().out


def test_an_empty_corpus_is_a_typed_refusal_not_a_clean_report(tmp_path: Path, capsys) -> None:
    # Reporting "0 questions leak" over a corpus that failed to load would be the worst possible
    # answer: a green gate that measured nothing.
    empty = tmp_path / "empty"
    empty.mkdir()
    _, questions = _write_fixture(tmp_path, leaky=True)
    assert report_main(["--corpus", str(empty), "--questions", str(questions)]) == 2
    assert "no .md documents" in capsys.readouterr().err


def test_ground_truth_absent_from_the_corpus_is_reported_not_scored(tmp_path: Path, capsys) -> None:
    corpus, _ = _write_fixture(tmp_path, leaky=True)
    orphan = tmp_path / "orphan.jsonl"
    orphan.write_text(
        json.dumps({
            "id": "q99", "query": "why anything", "language": "en", "type": "factual",
            "ground_truth": {"must_include": ["2020-01-01-a-document-that-never-existed.md"],
                             "acceptable": []},
        }) + "\n",
        encoding="utf-8",
    )
    assert report_main(["--corpus", str(corpus), "--questions", str(orphan), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["unmatched_ground_truth"] == ["q99"]
    assert payload["summary"]["mean_peak"] is None  # nothing scoreable, not zero
