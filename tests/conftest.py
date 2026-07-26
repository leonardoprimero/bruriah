"""Availability of the private resources some tests need.

A handful of tests verify this project against the author's own live corpus and the legacy engine it
replaced: that the real vault reindexes reproducibly, that the legacy database is never mutated,
that the recovery baseline still matches. They are genuinely valuable *here* and meaningless
anywhere else, because the resources they assert about are private and are not distributed.

Rather than delete them or let a fresh clone fail on them, they skip when the resource is absent.
A clone that has never seen the vault runs the whole product suite green; a checkout that has it
runs the migration guarantees too. `pytest -rs` lists what was skipped, so the difference is visible
rather than silent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_ROOT = Path(__file__).resolve().parents[1]

VAULT_ROOT = REPOSITORY_ROOT / "Cerebro-IA"
LEGACY_DATABASE = RETRIEVAL_ROOT / "cerebro.db"
CORPUS_POLICY = RETRIEVAL_ROOT / "corpus-policy.yaml"
BASELINE_SCRIPT = RETRIEVAL_ROOT / "scripts" / "verify_legacy_baseline.py"

requires_vault = pytest.mark.skipif(
    not VAULT_ROOT.is_dir() or not CORPUS_POLICY.is_file(),
    reason="the author's private corpus is not part of this checkout",
)
requires_legacy_database = pytest.mark.skipif(
    not LEGACY_DATABASE.is_file(),
    reason="the legacy cerebro.db is not part of this checkout",
)
requires_baseline_script = pytest.mark.skipif(
    not BASELINE_SCRIPT.is_file(),
    reason="the legacy recovery baseline is not part of this checkout",
)
