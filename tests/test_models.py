from __future__ import annotations

import pytest

from bruriah.models import SourceMetadata


def test_source_metadata_rejects_a_source_tier_outside_the_two_trusted_values() -> None:
    """premise-id-collision follow-up (review lineage review-c2885675bd293875,
    R3-tier-no-longer-normalized): `SourceMetadata.source` is typed
    `Literal["repository", "github"]`, but a `Literal` is a static-only guarantee -- nothing at
    runtime stopped a caller from constructing `SourceMetadata(source="anything")`, which
    `index._build_premise_and_alternative_records` would then have silently treated as neither
    trust tier acts on (falling through every `tier == "repository"`/`tier == "github"` branch).
    The tier must fail closed at construction time instead."""
    with pytest.raises(ValueError, match="invalid_source_tier"):
        SourceMetadata(source="anything")  # type: ignore[arg-type]
