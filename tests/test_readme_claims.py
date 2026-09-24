"""README.md's test-count claim must match what this run actually collected.

Section 4 ("Measured & Empirical Evidence") used to hand-type a count -- "1,087 passed" -- that
silently drifted from reality as the suite grew: by the time it was caught, 1,358 tests were being
collected. This test makes the drift impossible to miss by asserting the README number equals
`session.testscollected` from `conftest.py`'s `pytest_collection_finish` hook, whenever the run is
a full one (see that hook's docstring for what "full" means and its known detection gap).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
README = REPOSITORY_ROOT / "README.md"
_TEST_COUNT_PATTERN = re.compile(r"\*\*([\d,]+) tests\*\*")


def test_readme_test_count_matches_collected(request: pytest.FixtureRequest) -> None:
    if not getattr(request.config, "_bruriah_full_run", False):
        pytest.skip("narrowed run (explicit paths, -k, --deselect or --lf): count not comparable")

    collected = getattr(request.config, "_bruriah_collected", None)
    assert collected is not None, "pytest_collection_finish did not record a collected count"

    text = README.read_text(encoding="utf-8")
    match = _TEST_COUNT_PATTERN.search(text)
    assert match is not None, "README.md section 4 must state the test count as '**N tests**' so this check can find it"
    readme_count = int(match.group(1).replace(",", ""))
    assert readme_count == collected, (
        f"README.md test count is {readme_count} but {collected} tests are collected; update README.md section 4."
    )
