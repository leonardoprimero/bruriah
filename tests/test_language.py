"""The language signal that decides whether the lexical retrieval leg is trusted.

It changes ranking, so what matters is not raw accuracy but that it abstains rather than guesses:
a wrong confident answer discounts the leg that was about to find the document.
"""
from __future__ import annotations

import pytest

from bruriah.language import detect, dominant


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("why is the skill ceiling five", "en"),
        ("what did we decide about the skill permission envelope", "en"),
        ("por que el techo de skills es cinco", "es"),
        ("que decidimos sobre el permission envelope de las skills", "es"),
        # Unaccented Spanish is the normal case, not a degraded one: people type without accents.
        ("como se decidio la custodia de la llave de firma", "es"),
        ("cómo se decidió la custodia de la llave de firma", "es"),
    ],
)
def test_real_eval_questions_are_identified(text: str, expected: str) -> None:
    assert detect(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "sha256:deadbeef", "1 2 3", "kubectl apply -f deploy.yaml",
    "retrieval",                     # one content word, no function words at all
    "the que",                       # one marker each: a tie is not a decision
])
def test_it_abstains_rather_than_guessing(text: str) -> None:
    """`None` is the honest answer far more often than either language is.

    Abstaining leaves ranking exactly as it was. Guessing wrong discounts the leg that was about
    to work, so the failure modes are not symmetric and the threshold is set for that."""
    assert detect(text) is None


def test_a_one_word_lead_is_not_enough() -> None:
    # Two markers to one is the shape of an accident. Requiring a margin means the detector needs
    # to be told twice before it changes how results are ranked.
    assert detect("the of que") is None
    assert detect("the of and que") == "en"


def test_dominant_needs_a_real_majority_of_what_it_could_read() -> None:
    english = "the deployment of the service is described in the following section"
    spanish = "el despliegue del servicio se describe en la siguiente seccion con mas detalle"
    assert dominant([english, english, spanish]) == "en"
    assert dominant([english, spanish]) is None            # an even split has no dominant language
    assert dominant([]) is None
    assert dominant(["sha256:deadbeef", "1 2 3"]) is None  # nothing readable is not a verdict
    # An unreadable half must not veto a clear signal from the other half.
    assert dominant([english, english, "sha256:deadbeef", "42"]) == "en"


def test_the_verdict_is_stable_for_the_same_bytes() -> None:
    """No weights, no sampling, no randomness -- the property that lets ranking depend on it."""
    text = "por que la aprobacion se ata al digest y no a la version"
    assert len({detect(text) for _ in range(50)}) == 1
    assert detect(text) == "es"
