# Slice 9B unit tests for the private atomic evidence cache. No network -- pure filesystem and
# `EvidenceRecord` round-trip logic, driven with an injected `now` so TTL expiry is deterministic.
from __future__ import annotations

import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cerebro_router.cache import (
    CacheEntry, build_cache_entry, cache_key, read_cache, write_cache_atomic,
)
from cerebro_router.contracts import EvidenceRecord

_RETRIEVED_AT = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _evidence(**overrides: object) -> EvidenceRecord:
    payload = dict(
        ref="live:sha256:" + "a" * 32, kind="captured_live", publisher="example.test",
        locator="https://example.test:443/page", citation_locator="https://example.test:443/page",
        digest="sha256:" + "b" * 64, extraction_method="raw_lines", authority="unknown",
        authority_rationale="Live HTTP fetch.", freshness="unknown", license="unknown",
        reuse="unknown", conflict="unknown", retrieved_at=_RETRIEVED_AT,
    )
    payload.update(overrides)
    return EvidenceRecord(**payload)


def _entry(**overrides: object) -> CacheEntry:
    evidence = overrides.pop("evidence", _evidence())
    body = overrides.pop("body", b"Full body content well beyond the 280-char excerpt cap. " * 10)
    ttl = overrides.pop("ttl", timedelta(hours=1))
    max_excerpt_chars = overrides.pop("max_excerpt_chars", 20_000)
    return build_cache_entry(
        evidence, retrieved_at=_RETRIEVED_AT, ttl=ttl, body=body,
        max_excerpt_chars=max_excerpt_chars, policy_version="1.0.0",
    )


# --- Atomic write, permissions, round-trip ------------------------------------------------------


def test_write_then_read_round_trips_evidence_and_excerpt(tmp_path: Path) -> None:
    entry = _entry()
    write_cache_atomic(tmp_path / "cache", "https://example.test:443/page", entry)
    lookup = read_cache(tmp_path / "cache", "https://example.test:443/page", now=_RETRIEVED_AT)
    assert lookup.hit
    assert lookup.entry is not None
    assert lookup.entry.evidence == entry.evidence
    assert lookup.entry.excerpt == entry.excerpt
    assert lookup.entry.policy_version == "1.0.0"


def test_cache_file_written_with_0600_permissions(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/page", _entry())
    path = cache_dir / f"{cache_key('https://example.test:443/page')}.json"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(os.stat(cache_dir).st_mode)
    assert dir_mode == 0o700


def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/page", _entry())
    names = {path.name for path in cache_dir.iterdir()}
    assert all(not name.startswith(".tmp-cache-") for name in names)


def test_deterministic_cache_key_same_url_same_key() -> None:
    assert cache_key("https://example.test:443/page") == cache_key("https://example.test:443/page")
    assert cache_key("https://example.test:443/page") != cache_key("https://example.test:443/other")


# --- TTL ------------------------------------------------------------------------------------------


def test_read_before_ttl_expiry_is_a_hit(tmp_path: Path) -> None:
    entry = _entry(ttl=timedelta(hours=1))
    write_cache_atomic(tmp_path / "cache", "https://example.test:443/page", entry)
    almost_expired = _RETRIEVED_AT + timedelta(minutes=59)
    lookup = read_cache(tmp_path / "cache", "https://example.test:443/page", now=almost_expired)
    assert lookup.hit and not lookup.expired


def test_read_after_ttl_expiry_is_a_miss_never_served_as_current(tmp_path: Path) -> None:
    entry = _entry(ttl=timedelta(hours=1))
    write_cache_atomic(tmp_path / "cache", "https://example.test:443/page", entry)
    after_expiry = _RETRIEVED_AT + timedelta(hours=1, seconds=1)
    lookup = read_cache(tmp_path / "cache", "https://example.test:443/page", now=after_expiry)
    assert not lookup.hit
    assert lookup.expired
    assert lookup.entry is None  # expired content is never handed back as current


def test_missing_entry_is_a_plain_miss_not_expired(tmp_path: Path) -> None:
    lookup = read_cache(tmp_path / "cache", "https://never-cached.test:443/", now=_RETRIEVED_AT)
    assert not lookup.hit and not lookup.expired and lookup.entry is None


def test_corrupt_cache_file_is_treated_as_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{cache_key('https://example.test:443/page')}.json").write_text(
        "{not valid json", encoding="utf-8",
    )
    lookup = read_cache(cache_dir, "https://example.test:443/page", now=_RETRIEVED_AT)
    assert not lookup.hit and not lookup.expired and lookup.entry is None


def test_cache_entry_missing_required_field_is_treated_as_a_miss(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / f"{cache_key('https://example.test:443/page')}.json").write_text(
        '{"evidence": {}, "excerpt": "x"}', encoding="utf-8",
    )
    lookup = read_cache(cache_dir, "https://example.test:443/page", now=_RETRIEVED_AT)
    assert not lookup.hit and lookup.entry is None


# --- Reuse-gated excerpt bounding --------------------------------------------------------------


def test_unknown_reuse_stores_bounded_excerpt_only_never_full_body(tmp_path: Path) -> None:
    body = b"X" * 5000
    entry = _entry(evidence=_evidence(reuse="unknown"), body=body, max_excerpt_chars=20_000)
    assert entry.excerpt_only
    assert len(entry.excerpt) <= 280
    assert entry.excerpt != body.decode()

    write_cache_atomic(tmp_path / "cache", "https://example.test:443/page", entry)
    raw = (tmp_path / "cache" / f"{cache_key('https://example.test:443/page')}.json").read_text()
    assert "X" * 281 not in raw  # the on-disk file itself never carries the full body


def test_prohibited_reuse_stores_bounded_excerpt_only(tmp_path: Path) -> None:
    entry = _entry(evidence=_evidence(reuse="prohibited"), body=b"secret body " * 100)
    assert entry.excerpt_only
    assert len(entry.excerpt) <= 280


def test_permitted_reuse_stores_full_body_up_to_the_excerpt_cap(tmp_path: Path) -> None:
    body = b"Permitted content." * 5
    entry = _entry(evidence=_evidence(reuse="permitted"), body=body, max_excerpt_chars=20_000)
    assert not entry.excerpt_only
    assert entry.excerpt == body.decode()


def test_permitted_reuse_still_bounded_by_max_excerpt_chars(tmp_path: Path) -> None:
    body = b"A" * 1000
    entry = _entry(evidence=_evidence(reuse="permitted"), body=body, max_excerpt_chars=50)
    assert not entry.excerpt_only
    assert len(entry.excerpt) == 50


def test_restricted_reuse_stores_bounded_excerpt_only() -> None:
    entry = _entry(evidence=_evidence(reuse="restricted"))
    assert entry.excerpt_only
    assert len(entry.excerpt) <= 280
