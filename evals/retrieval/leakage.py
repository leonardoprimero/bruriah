"""Measure how much of a question's vocabulary already appears in the document that is its own
ground truth.

WHY THIS EXISTS. A retrieval eval writes questions and records which document answers each one. If
the question was written by reading that document, the question is partly a paraphrase of its own
answer, and the engine is being credited for matching vocabulary it was handed. Every eval says it
avoided this. None of them can show it, because "I wrote the questions from an independent source"
is a promise about a process, and a reader has no way to check a process.

This turns the promise into a number. A stranger with the corpus and the question set can compute
it, disagree with it, and see which questions are suspect -- without trusting whoever wrote them.

Three uses, in increasing order of usefulness:
  1. Inspect a question set before publishing anything measured on it.
  2. Refuse a question whose peak leakage is above a threshold, so the set cannot quietly fill up
     with restatements of the answer.
  3. Report recall stratified by leakage band, so a headline average cannot hide behind the easy
     questions. This is the one that matters: a set can have an acceptable mean and still owe all
     of its recall to the questions that gave the answer away.

WEIGHTING IS IDF OVER THE CORPUS ITSELF, and there is no stopword list and no minimum token
length. That absence is deliberate and was measured, not assumed. A hand-written stoplist is a
tuning knob: it decides which words "do not count", and the mean leakage of this repository's own
English question set moves from 0.58 to 0.70 depending on which list you pick -- so whoever writes
the list writes the result. IDF does the same job from the corpus: a term appearing in every
document contributes nothing to either side of the ratio, and a term appearing in the answer and
almost nowhere else contributes a lot, which is the shape of a leak. Compared across three
tokenisation choices (stoplist + length floor, length floor only, neither), `peak` was identical
for 22 of this repository's 24 questions, and both exceptions came from the stoplist rather than
from the length floor.

TWO NUMBERS, AND THEY ARE NOT INTERCHANGEABLE.

  `share`  the IDF-weighted fraction of the question that also appears in its answer. The
           descriptive one. Its ABSOLUTE value depends on tokenisation, so compare questions
           within one corpus and one tokeniser -- never a share from here against a share from
           somebody else's harness.

  `peak`   the single most distinctive term the question handed over, as a fraction of the highest
           IDF this corpus can produce. The robust one, and the one to gate on: a common word can
           never be the maximum, so adding or removing common words from the token set cannot move
           it. `peak = 0` means the question shares no term with its answer that the corpus
           considers distinctive at all.

No I/O, no wall-clock, no randomness: every function is a pure computation over its arguments, the
same discipline `metrics.py` follows, so the same inputs always produce the same outputs.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

# Unicode-aware: `\w` minus underscore, so accented Spanish terms tokenise as one term rather than
# splitting at the accent. The question sets here are English and Spanish over an English corpus,
# and a tokeniser that mangled one of them would report that language as leaking less than it does.
_TERM = re.compile(r"[^\W_]+", re.UNICODE)


def terms(text: str) -> list[str]:
    """Lowercase tokens, in order, with nothing filtered out.

    No stoplist and no length floor on purpose -- see the module docstring. Order is preserved and
    duplicates are kept, so a caller that wants a set can build one and a caller that wants term
    frequencies still can.
    """
    return _TERM.findall(text.lower())


@dataclass(frozen=True)
class TermStatistics:
    """Document frequencies over one corpus, and the IDF derived from them.

    Built from the corpus the eval actually runs against, never from a general-purpose frequency
    table: "distinctive" only means anything relative to the documents being searched. A term that
    is rare in English and appears in half of THIS corpus is not distinctive here.
    """

    document_frequency: Mapping[str, int]
    documents: int

    @classmethod
    def over(cls, corpus: Iterable[str]) -> "TermStatistics":
        frequency: Counter[str] = Counter()
        count = 0
        for document in corpus:
            count += 1
            frequency.update(set(terms(document)))
        return cls(document_frequency=dict(frequency), documents=count)

    def idf(self, term: str) -> float:
        """Smoothed IDF. A term in every document scores 0 and therefore cannot leak; a term in no
        document scores the maximum, which is why `ceiling` below is the right normaliser."""
        return math.log((self.documents + 1) / (self.document_frequency.get(term, 0) + 1))

    @property
    def ceiling(self) -> float:
        """The IDF of a term this corpus has never seen -- the most distinctive a term can be
        here. Used to express `peak` as a fraction, so a figure from a 147-document corpus is
        comparable to one from a 4,000-document corpus."""
        return math.log(self.documents + 1)


@dataclass(frozen=True)
class Leakage:
    """What one question gave away about its own answer.

    Every field is `None` when the question has no terms at all, which is undefined rather than
    zero -- the same rule `metrics.recall_at_k` applies to a query with no ground truth. A question
    that has terms but shares none of them scores `0.0`, which is a real measurement.
    """

    share: float | None
    peak: float | None
    leaked: tuple[str, ...]


def leakage(question: str, answer: str, statistics: TermStatistics) -> Leakage:
    """Measure `question` against the document that is its ground truth.

    `leaked` is every question term present in the answer, ordered most distinctive first, so a
    reviewer reading a flagged question can see WHICH words gave it away instead of only that it
    scored badly. That list is the part a human acts on.
    """
    question_terms = terms(question)
    if not question_terms:
        return Leakage(share=None, peak=None, leaked=())

    answer_terms = set(terms(answer))
    shared = [term for term in dict.fromkeys(question_terms) if term in answer_terms]
    weight = sum(statistics.idf(term) for term in question_terms)

    share = (sum(statistics.idf(term) for term in question_terms if term in answer_terms) / weight
             if weight else 0.0)
    ceiling = statistics.ceiling
    peak = (max((statistics.idf(term) for term in shared), default=0.0) / ceiling) if ceiling else 0.0
    return Leakage(
        share=share,
        peak=peak,
        leaked=tuple(sorted(shared, key=statistics.idf, reverse=True)),
    )


@dataclass(frozen=True)
class Band:
    """Recall for one leakage band. `None` metrics mean the band had nothing to score, never zero."""

    label: str
    questions: int
    recall_at_3: float | None
    mrr_at_10: float | None


@dataclass(frozen=True)
class Scored:
    """One question's leakage next to how retrieval actually did on it."""

    question_id: str
    peak: float | None
    hit_at_3: bool
    reciprocal_rank: float


def _band(label: str, rows: Sequence[Scored]) -> Band:
    if not rows:
        return Band(label=label, questions=0, recall_at_3=None, mrr_at_10=None)
    return Band(
        label=label,
        questions=len(rows),
        recall_at_3=sum(1 for row in rows if row.hit_at_3) / len(rows),
        mrr_at_10=sum(row.reciprocal_rank for row in rows) / len(rows),
    )


def stratify(rows: Sequence[Scored], threshold: float) -> tuple[Band, Band]:
    """Split scored questions at `threshold` on `peak` and report recall for each side.

    The reason to publish both halves rather than one average: a question set can hold an
    acceptable mean while owing all of its recall to the questions that gave the answer away. If
    the low-leakage band scores far worse than the high-leakage band, the headline is a fact about
    the questions and not about the engine.

    Questions whose `peak` is `None` are excluded from both bands -- undefined leakage cannot be
    said to fall on either side of a threshold, and silently treating it as zero would move it into
    the band this measurement exists to protect.
    """
    scoreable = [row for row in rows if row.peak is not None]
    high = [row for row in scoreable if row.peak >= threshold]
    low = [row for row in scoreable if row.peak < threshold]
    return _band(f"peak >= {threshold:.2f}", high), _band(f"peak < {threshold:.2f}", low)


__all__ = ["Band", "Leakage", "Scored", "TermStatistics", "leakage", "stratify", "terms"]
