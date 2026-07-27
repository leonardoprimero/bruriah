# Slice 9B unit tests for the private atomic evidence cache. No network -- pure filesystem and
# `EvidenceRecord` round-trip logic, driven with an injected `now` so TTL expiry is deterministic.
# Slice 12D adds deletion-control tests: `prune_expired`, `cache_stats`, and the write-path
# self-bounding sweep -- see cache.py's module docstring for the full design rationale.
from __future__ import annotations

import dataclasses
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import requires_posix_permissions

from bruriah.cache import (
    CacheEntry, CacheStats, PruneSummary, build_cache_entry, cache_key, cache_stats, find_by_ref,
    prune_expired, read_cache, write_cache_atomic,
)
from bruriah.contracts import EvidenceRecord

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
    retrieved_at = overrides.pop("retrieved_at", _RETRIEVED_AT)
    return build_cache_entry(
        evidence, retrieved_at=retrieved_at, ttl=ttl, body=body,
        max_excerpt_chars=max_excerpt_chars, policy_version="1.0.0",
    )


def _path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{cache_key(url)}.json"


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


@requires_posix_permissions
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


# --- prune_expired: explicit deletion control (Slice 12D, closes 9B WARNING 2) -----------------


def test_prune_expired_keeps_a_live_entry(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/page", _entry(ttl=timedelta(hours=1)))
    summary = prune_expired(
        cache_dir, now=_RETRIEVED_AT + timedelta(minutes=30), ttl=timedelta(hours=1),
    )
    assert summary == PruneSummary(scanned=1, removed=0, corrupt=0, bytes_reclaimed=0)
    assert _path(cache_dir, "https://example.test:443/page").exists()


def test_prune_expired_removes_an_entry_past_its_ttl(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/page", _entry(ttl=timedelta(hours=1)))
    summary = prune_expired(cache_dir, now=_RETRIEVED_AT + timedelta(hours=2), ttl=timedelta(hours=1))
    assert summary.scanned == 1 and summary.removed == 1 and summary.corrupt == 0
    assert summary.bytes_reclaimed > 0
    assert not _path(cache_dir, "https://example.test:443/page").exists()


def test_prune_expired_removes_an_entry_with_an_implausible_future_expiry(tmp_path: Path) -> None:
    """Tamper/corruption ceiling: `expires_at` far beyond what `ttl` could ever legitimately grant
    is pruned even though `now` has not reached it yet -- the "other direction" of injectable
    `now` relative to a real deletion boundary."""
    cache_dir = tmp_path / "cache"
    tampered = dataclasses.replace(
        _entry(ttl=timedelta(hours=1)), expires_at=_RETRIEVED_AT + timedelta(days=365),
    )
    write_cache_atomic(cache_dir, "https://example.test:443/page", tampered)
    summary = prune_expired(cache_dir, now=_RETRIEVED_AT, ttl=timedelta(hours=1))
    assert summary.removed == 1
    assert not _path(cache_dir, "https://example.test:443/page").exists()


def test_prune_expired_is_typed_total_on_a_corrupt_entry(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    path = _path(cache_dir, "https://example.test:443/page")
    path.write_text("{not valid json", encoding="utf-8")
    summary = prune_expired(cache_dir, now=_RETRIEVED_AT, ttl=timedelta(hours=1))
    assert summary.scanned == 1 and summary.removed == 1 and summary.corrupt == 1
    assert not path.exists()


def test_prune_expired_missing_cache_dir_returns_zero_summary(tmp_path: Path) -> None:
    summary = prune_expired(tmp_path / "does-not-exist", now=_RETRIEVED_AT, ttl=timedelta(hours=1))
    assert summary == PruneSummary(scanned=0, removed=0, corrupt=0, bytes_reclaimed=0)


# --- write_cache_atomic: self-bounding write path (Slice 12D) -----------------------------------


def test_write_cache_atomic_self_prunes_expired_entries_on_a_later_write(tmp_path: Path) -> None:
    """No wall-clock, no new parameter: the frozen `research.py` call
    `write_cache_atomic(deps.cache_dir, canonical, entry)` still self-bounds, deriving `now` from
    the newly written entry's own `evidence.retrieved_at`."""
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/first", _entry(ttl=timedelta(hours=1)))
    first_path = _path(cache_dir, "https://example.test:443/first")
    assert first_path.exists()

    later = _RETRIEVED_AT + timedelta(hours=2)
    second = _entry(evidence=_evidence(retrieved_at=later), retrieved_at=later, ttl=timedelta(hours=1))
    write_cache_atomic(cache_dir, "https://example.test:443/second", second)
    second_path = _path(cache_dir, "https://example.test:443/second")

    assert not first_path.exists()  # now(T+2h, from `second`) > first.expires_at(T+1h)
    assert second_path.exists()
    assert {path.name for path in cache_dir.iterdir()} == {second_path.name}


def test_write_cache_atomic_self_prune_never_deletes_the_entry_just_written(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(
        cache_dir, "https://example.test:443/page", _entry(ttl=timedelta(seconds=0)),  # zero-ttl edge
    )
    assert _path(cache_dir, "https://example.test:443/page").exists()


def test_write_cache_atomic_self_prune_skipped_when_retrieved_at_is_none(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    entry = _entry(evidence=_evidence(retrieved_at=None))
    path = write_cache_atomic(cache_dir, "https://example.test:443/page", entry)
    assert path.exists()  # the write itself never fails when the self-prune proxy is unavailable


def test_self_prune_at_write_never_evicts_a_newer_live_entry_on_an_out_of_order_older_write(
    tmp_path: Path,
) -> None:
    """Out-of-order completion: the write that lands SECOND is not necessarily the one with the
    LATER `retrieved_at` (concurrent fetches can finish in either order). B completes first with a
    newer `retrieved_at` and a long TTL, so it is still live for hours. A completes second with an
    OLDER `retrieved_at` and a short TTL -- its self-prune sweep (`now` derived from A's own
    `retrieved_at`, see cache.py's module docstring "Slice 12D") must use the monotonic-safe plain-
    expiry direction (`A.retrieved_at > other.expires_at`), which never trips for B here
    (`A.retrieved_at` is far BEFORE `B.expires_at`). A tamper-ceiling-style direction instead
    compares another entry's `expires_at` against THIS write's own short-lived expiry window (as
    `prune_expired` legitimately does with an operator-supplied `now`/`ttl`); applied here it would
    flag B prunable purely because B's `expires_at` sits far beyond what A's own brief TTL could
    justify -- wrongly evicting a genuinely newer, still-live entry. This test fails under that
    unsafe direction and passes only under the real monotonic-safe one."""
    cache_dir = tmp_path / "cache"

    newer = _RETRIEVED_AT + timedelta(hours=2)
    b = _entry(evidence=_evidence(retrieved_at=newer), retrieved_at=newer, ttl=timedelta(hours=1))
    write_cache_atomic(cache_dir, "https://example.test:443/b", b)
    b_path = _path(cache_dir, "https://example.test:443/b")
    assert b_path.exists()

    a = _entry(retrieved_at=_RETRIEVED_AT, ttl=timedelta(minutes=1))  # older, out-of-order completion
    write_cache_atomic(cache_dir, "https://example.test:443/a", a)

    assert b_path.exists()  # A's self-prune (now=_RETRIEVED_AT) must never evict the newer, live B
    lookup = read_cache(cache_dir, "https://example.test:443/b", now=newer + timedelta(minutes=1))
    assert lookup.hit and not lookup.expired
    assert lookup.entry is not None
    assert lookup.entry.evidence == b.evidence


# --- cache_stats: read-only visibility for `doctor` (Slice 12D) ---------------------------------


def test_cache_stats_empty_or_missing_dir_returns_zero(tmp_path: Path) -> None:
    assert cache_stats(tmp_path / "does-not-exist", now=_RETRIEVED_AT) == CacheStats(
        entries=0, expired=0, total_bytes=0,
    )


def test_cache_stats_reports_entries_and_expired_without_mutating(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    write_cache_atomic(cache_dir, "https://example.test:443/live", _entry(ttl=timedelta(hours=1)))
    stats = cache_stats(cache_dir, now=_RETRIEVED_AT + timedelta(minutes=30))
    assert stats.entries == 1
    assert stats.expired == 0
    assert stats.total_bytes > 0
    assert _path(cache_dir, "https://example.test:443/live").exists()  # doctor never deletes

    stale = cache_stats(cache_dir, now=_RETRIEVED_AT + timedelta(hours=2))
    assert stale.entries == 1  # still on disk -- cache_stats only reports, never prunes
    assert stale.expired == 1
    assert _path(cache_dir, "https://example.test:443/live").exists()


# --- Finding an entry by the ref its evidence carries -------------------------------------------
# Entries are keyed by a hash of the canonical URL, which answers `research()`'s question ("do I
# already have this url") and not `read_evidence`'s ("give me the bytes behind this ref"). The key
# cannot be reversed: the ref digests the BODY and the key digests the URL, deliberately, so the
# same page fetched twice is one cache entry and two pieces of evidence.


def test_find_by_ref_returns_the_entry_whose_evidence_carries_that_ref(tmp_path: Path) -> None:
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    wanted = _entry(evidence=_evidence(ref="live:sha256:" + "a" * 32), retrieved_at=now)
    other = _entry(evidence=_evidence(ref="live:sha256:" + "b" * 32), retrieved_at=now)
    write_cache_atomic(tmp_path, "https://one.example:443/a", wanted)
    write_cache_atomic(tmp_path, "https://two.example:443/b", other)

    found = find_by_ref(tmp_path, "live:sha256:" + "a" * 32, now=now)
    assert found.hit and found.entry is not None
    assert found.entry.evidence.ref == "live:sha256:" + "a" * 32


def test_find_by_ref_reports_an_expired_entry_as_expired_and_withholds_it(tmp_path: Path) -> None:
    # "Had it, it aged out" is a different answer from "never had it", and neither is grounds for
    # handing back stale content -- the same split `read_cache` already makes.
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    write_cache_atomic(tmp_path, "https://one.example:443/a",
                       _entry(evidence=_evidence(ref="live:sha256:" + "c" * 32), retrieved_at=now))

    found = find_by_ref(tmp_path, "live:sha256:" + "c" * 32, now=now + timedelta(days=3))
    assert found.hit is False
    assert found.expired is True
    assert found.entry is None


def test_find_by_ref_misses_cleanly_on_an_unknown_ref_and_an_absent_directory(tmp_path: Path) -> None:
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert find_by_ref(tmp_path / "never-created", "live:sha256:" + "d" * 32, now=now).hit is False

    write_cache_atomic(tmp_path, "https://one.example:443/a",
                       _entry(evidence=_evidence(ref="live:sha256:" + "e" * 32), retrieved_at=now))
    miss = find_by_ref(tmp_path, "live:sha256:" + "f" * 32, now=now)
    assert miss.hit is False and miss.expired is False and miss.entry is None


def test_find_by_ref_skips_a_corrupt_entry_instead_of_raising(tmp_path: Path) -> None:
    # A corrupt file cannot answer for any ref, and a scan that dies on one bad file would make a
    # single unreadable entry break every live read. `prune_expired` is what removes it.
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "0000corrupt.json").write_text("{not json", encoding="utf-8")
    write_cache_atomic(tmp_path, "https://one.example:443/a",
                       _entry(evidence=_evidence(ref="live:sha256:" + "9" * 32), retrieved_at=now))

    found = find_by_ref(tmp_path, "live:sha256:" + "9" * 32, now=now)
    assert found.hit and found.entry is not None
