"""How far a query's best score stands out from the distribution that query itself produced.

WHY THIS LIVES IN `evals/` AND NOT IN `src/`. It was written to answer one question -- can a
retrieval engine that refuses to emit confidence scores still tell a question it can answer from
one it cannot? -- and the answer, measured, was no. See "Separation does not detect an
unanswerable question" in `evals/project-memory/README.md`. Shipping it would have added two
fields to `RetrievalOutcome` for a hypothesis that did not survive its own control, so it stayed
here, where `report_reach.py` already established that an eval may reach into the engine's private
helpers to see something the public surface does not expose.

WHAT IT IS NOT. Not a confidence score. A confidence says "this passage is 0.87 relevant" -- a
claim about one piece of evidence, which `RetrievalMatch` refuses to carry and which
`tests/test_retrieval.py`, `tests/test_lookup.py` and `tests/test_candidates.py` each forbid by
name. This is a property of the SEARCH, read off the spread of one query's own scores. That
distinction is what made it expressible at all; it is not what made it work.

No I/O, no clock, no randomness: pure computation over its arguments, like `metrics.py` beside it.
"""
from __future__ import annotations

from collections.abc import Sequence

from metrics import _median

# Below this many scores a spread is not a distribution and the statistic is noise, so it is
# reported as undefined rather than as a small number. A floor on whether it is computable at all,
# never a threshold on what it means.
MIN_SCORES = 8
MAD_TO_SIGMA = 1.4826  # Makes MAD a consistent estimator of sigma for a normal distribution.


def separation(scores: Sequence[float]) -> float | None:
    """Robust deviations between the best score and the median of the rest.

    Median and MAD rather than mean and standard deviation. A query with SEVERAL good answers
    inflates a standard deviation with exactly the scores that make it a good query, and would
    then read as less separated than a query with one lucky hit -- backwards. That much the
    statistic gets right, and `tests/test_retrieval_eval.py` pins it against the naive form.

    Scale-free: dividing by the spread of the same distribution cancels the units, so the number
    does not depend on whether the leg is BM25 or cosine, or on which embedding model produced the
    vectors. THAT PROPERTY IS REAL AND IT WAS NOT ENOUGH -- scale-invariance is not shape-
    invariance. A larger, denser corpus has a differently SHAPED cosine distribution, not a
    differently scaled one, and the measurement found the corpus moving this number further than
    the presence of an answer did. The full result is in `evals/project-memory/README.md`; it is
    recorded here too, because a scale-free statistic is exactly the kind of thing a later reader
    would otherwise reasonably assume transfers across corpora.

    `None` -- never 0.0 -- when the question cannot be asked: too few scores to describe a
    distribution, or a distribution with no spread at all. `metrics.recall_at_k` draws that line
    for the same reason: an undefined measurement averaged in as a zero is a fabricated
    observation.
    """
    if len(scores) < MIN_SCORES:
        return None
    values = sorted(scores, reverse=True)
    rest = values[1:]
    median = _median(rest)
    deviation = _median([abs(value - median) for value in rest])
    if deviation == 0.0:
        return None
    return (values[0] - median) / (MAD_TO_SIGMA * deviation)


__all__ = ["MAD_TO_SIGMA", "MIN_SCORES", "separation"]
