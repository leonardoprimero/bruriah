from __future__ import annotations

import pytest

from bruriah.models import SourceMetadata


def test_source_metadata_rejects_a_source_tier_outside_the_two_trusted_values() -> None:
    """`SourceMetadata.source` is typed `Literal["repository", "github"]`, but a `Literal` is a
    static-only guarantee. `index._build_premise_and_alternative_records` branches on
    `tier == "repository"` versus `else`, so an unknown value would silently be handled as the
    GitHub tier. The tier must fail closed at construction time instead."""
    with pytest.raises(ValueError, match="invalid_source_tier"):
        SourceMetadata(source="anything")  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["repository", "github"])
def test_source_metadata_accepts_the_two_trusted_tiers(source: str) -> None:
    assert SourceMetadata(source=source).source == source  # type: ignore[arg-type]
