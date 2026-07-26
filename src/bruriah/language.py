"""Deterministic language identification, for one purpose: deciding whether the lexical retrieval
leg can be expected to work at all.

This is a function-word counter, not a classifier. That is deliberate. Retrieval already refuses to
put a model in the selection path, and adding one here to answer a question this narrow would trade
a property the project argues for against an accuracy it does not need. Counting `the`/`of`/`and`
against `que`/`por`/`del` is inspectable, has no weights to drift, and returns the same answer
forever for the same bytes.

It answers exactly one question -- "English, Spanish, or I cannot tell" -- and `None` is a real
answer that callers must handle, not a failure. When it cannot tell, nothing downstream changes.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Function words that are frequent in one language and absent from the other. Words that appear in
# both (`no`, `a`, `en`, `son`, `me`) are excluded on purpose: a marker that fires for either
# language is not evidence, and including it would only add noise to the margin test below.
_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "of", "and", "to", "that", "is", "was", "were", "for", "with", "as", "on", "by",
        "are", "this", "these", "those", "from", "which", "what", "why", "how", "when", "where",
        "did", "does", "do", "we", "our", "not", "but", "have", "has", "had", "be", "been", "it",
        "its", "they", "their", "there", "would", "should", "could", "than", "then", "into",
    }),
    "es": frozenset({
        "que", "por", "para", "con", "los", "las", "del", "una", "como", "donde", "cuando",
        "porque", "sobre", "este", "esta", "estos", "estas", "fue", "fueron", "hace", "desde",
        "hasta", "entre", "mas", "muy", "sin", "tambien", "cual", "quien", "nuestro", "nuestra",
        "el", "la", "lo", "al", "se", "su", "sus", "es", "un", "si", "ya", "pero", "todo",
    }),
}

_WORD = re.compile(r"[a-záéíóúüñ]+")

# A margin, not a plurality. Two markers deciding a language on a one-word lead is a coin flip
# dressed as a measurement, and the caller changes ranking behaviour on the answer.
_MINIMUM_HITS = 2
_MARGIN = 2


def _fold(text: str) -> str:
    """Lowercase and strip the accents Spanish carries and English does not.

    Folding costs the single strongest Spanish signal, which is the point: a corpus written by
    people who skip accents on a keyboard that fights them is the normal case, not the exception,
    and a detector that only works on carefully accented prose would be useless exactly there.
    """
    table = str.maketrans("áéíóúüñàèìòù", "aeiouunaeiou")
    return text.lower().translate(table)


def detect(text: str) -> str | None:
    """Return `"en"`, `"es"`, or `None` when the evidence does not separate them."""
    if not isinstance(text, str) or not text.strip():
        return None
    words = _WORD.findall(_fold(text))
    if not words:
        return None
    counts = {code: sum(1 for word in words if word in markers)
              for code, markers in _MARKERS.items()}
    (best, best_count), (_, runner_up) = sorted(counts.items(), key=lambda item: -item[1])
    if best_count < _MINIMUM_HITS or best_count - runner_up < _MARGIN:
        return None
    return best


def dominant(texts: Iterable[str]) -> str | None:
    """The language most of `texts` are written in, or `None` if no language holds a majority.

    A strict majority of the texts that could be identified at all -- an unidentifiable half does
    not get to veto a clear signal from the other half, and a corpus split evenly between two
    languages honestly has no dominant one.
    """
    votes: dict[str, int] = {}
    for text in texts:
        code = detect(text)
        if code is not None:
            votes[code] = votes.get(code, 0) + 1
    if not votes:
        return None
    total = sum(votes.values())
    leader, count = max(votes.items(), key=lambda item: (item[1], item[0]))
    return leader if count * 2 > total else None
