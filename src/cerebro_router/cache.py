# Slice 9B (unit 1 of 2): the private atomic evidence cache. Stores ONLY the permitted minimum
# for captured live evidence -- design.md "Network": "Atomic private cache stores permitted
# minimum excerpts plus digest, redirect chain, policy/pack versions, license and TTL; prohibited
# bodies are discarded." and the lifecycle spec's "External Evidence and Cache Lifecycle" /
# "Cache expiry or prohibited reuse": prohibited/unknown reuse MUST store only permitted
# metadata, citation, and minimum necessary excerpt; expired content MUST NOT be presented as
# current.
#
# `evidence: EvidenceRecord` is stored WHOLE -- it is already "permitted metadata" by contract
# (digest, locator, citation_locator, redirect_chain, license/reuse state, pack_version -- no
# body-text field exists on it at all), so persisting it fully is safe regardless of reuse state.
# `excerpt` is the ONLY body-derived text ever written to disk; `excerpt_only=True` (the default
# for every live fetch in this change, since `fetch.py` has no pack context and always reports
# license=reuse="unknown") means `excerpt` is a small, bounded citation excerpt, never the full
# body. `policy_version` records the research-policy pack version this cache decision was made
# under -- distinct from `evidence.pack_version`, which belongs to the evidence's own source pack.
#
# Cache key: sha256 of the CANONICAL request URL (deterministic and content-addressed -- never a
# random/UUID/timestamp name, matching the rest of the codebase's "no Date.now/random for
# identity" convention). Write path: write-temp-then-rename (`os.replace`, atomic on the same
# filesystem) with 0600 permissions applied BEFORE the rename makes the file visible under its
# final name, so no reader can ever observe a partially-written or over-permissioned file.
# `now` is injectable everywhere TTL is evaluated, so expiry is deterministic under test, never a
# wall-clock race.
#
# Slice 12D: deletion controls (closes 9B WARNING 2 -- "no cache deletion/retention"; the
# lifecycle spec's "External Evidence and Cache Lifecycle" requires the cache be "governed by TTL
# and deletion controls", not just TTL staleness detection). Two pieces, both typed-total and both
# free of wall-clock reads:
#   - `prune_expired` is the explicit deletion control: it removes an entry once it has genuinely
#     expired (`now > expires_at`, the same condition `read_cache` already treats as a miss) OR
#     its `expires_at` exceeds what any legitimate write under the given `ttl` could have produced
#     (`expires_at - now > ttl`) -- a tamper/corruption ceiling, since a freshly built live entry
#     can never claim more than `ttl` of remaining life. Corrupt/unreadable entries are always
#     removed (nothing about them is auditable or bound-checkable). `doctor` (cli.py) never calls
#     this -- it only calls the read-only `cache_stats` below, preserving "doctor is read-only".
#   - `write_cache_atomic` self-bounds by sweeping for already-expired entries once after every
#     successful write, deriving `now` ENTIRELY from the entry just written
#     (`entry.evidence.retrieved_at` stands in for "now"). This needs zero new parameters and zero
#     wall-clock, so the frozen `research.py` callsite (`write_cache_atomic(deps.cache_dir,
#     canonical, entry)`) gets self-bounding for free. Only the plain-expiry direction
#     (`retrieved_at > other.expires_at`) is used here -- NOT the tamper ceiling -- because
#     `retrieved_at` is always a real PAST timestamp of one fetch among possibly concurrent or
#     out-of-order writes; wall-clock time only advances, so if this write's own retrieved_at
#     already exceeds another entry's `expires_at`, the true current time exceeds it too, and the
#     removal can never be a false positive. The ceiling check requires an authoritative, operator-
#     supplied `now`/`ttl` pair (an out-of-order write's own `ttl` says nothing about whether
#     ANOTHER entry's expiry is plausible), so it is reserved for the explicit `prune_expired` call
#     below. If `retrieved_at` is absent (the field is optional on `EvidenceRecord`) or not
#     comparable, self-pruning is skipped for that write -- best-effort, never a hard failure of
#     the write itself.
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .contracts import EvidenceRecord

ReuseState = Literal["permitted", "restricted", "prohibited", "unknown"]
_ENTRY_FIELDS = frozenset({"evidence", "expires_at", "excerpt", "excerpt_only", "policy_version"})


class CacheError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CacheEntry:
    """One cached research result. See module docstring for the excerpt-only/reuse contract."""

    evidence: EvidenceRecord
    expires_at: datetime
    excerpt: str
    excerpt_only: bool
    policy_version: str


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    expired: bool
    entry: CacheEntry | None


@dataclass(frozen=True)
class PruneSummary:
    """Result of one `prune_expired` sweep. `corrupt` is a subset of `removed` (every corrupt
    entry found is also deleted); `bytes_reclaimed` counts freed disk space across all removals."""

    scanned: int
    removed: int
    corrupt: int
    bytes_reclaimed: int


@dataclass(frozen=True)
class CacheStats:
    """Read-only visibility for `doctor` -- see `cache_stats`. Never deletes anything."""

    entries: int
    expired: int
    total_bytes: int


def cache_key(canonical_url: str) -> str:
    """Deterministic, content-addressed cache filename stem -- the SAME canonical URL always
    yields the SAME key, so a repeated request for the original URL hits cache even if the
    fetched content ultimately resolved through redirects to a different final locator."""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Path, canonical_url: str) -> Path:
    return cache_dir / f"{cache_key(canonical_url)}.json"


def _encode(entry: CacheEntry) -> str:
    payload = {
        "evidence": json.loads(entry.evidence.model_dump_json()),
        "expires_at": entry.expires_at.isoformat(),
        "excerpt": entry.excerpt,
        "excerpt_only": entry.excerpt_only,
        "policy_version": entry.policy_version,
    }
    return json.dumps(payload, sort_keys=True)


def _decode(raw: str) -> CacheEntry:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CacheError("corrupt_cache_entry") from error
    if not isinstance(payload, dict) or set(payload) != _ENTRY_FIELDS:
        raise CacheError("corrupt_cache_entry")
    try:
        # `model_validate` (Python-object mode) rejects an ISO datetime STRING under this
        # project's `strict=True` `ClosedModel` config; re-serializing the nested dict and using
        # `model_validate_json` (JSON mode, where a datetime is necessarily a string) is the same
        # pattern `packs.py` uses for its own nested pydantic parsing.
        evidence = EvidenceRecord.model_validate_json(json.dumps(payload["evidence"]))
        return CacheEntry(
            evidence=evidence,
            expires_at=datetime.fromisoformat(payload["expires_at"]),
            excerpt=payload["excerpt"],
            excerpt_only=payload["excerpt_only"],
            policy_version=payload["policy_version"],
        )
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise CacheError("corrupt_cache_entry") from error


def write_cache_atomic(cache_dir: Path, canonical_url: str, entry: CacheEntry) -> Path:
    """Write-temp-then-rename: a temp file created in the SAME directory (so `os.replace` is an
    atomic rename on one filesystem, never a cross-device copy) is written, chmod'd 0600, then
    atomically renamed onto the final content-addressed path. A reader can only ever observe the
    prior complete entry or the new complete entry -- never a partial write. After the write lands,
    an opportunistic self-prune sweep runs (see `_self_prune_after_write`) so the cache is
    self-bounding without any new parameter or wall-clock read."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(cache_dir, 0o700)
    final_path = _cache_path(cache_dir, canonical_url)
    fd, tmp_name = tempfile.mkstemp(dir=cache_dir, prefix=".tmp-cache-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_encode(entry))
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, final_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    _self_prune_after_write(cache_dir, entry)
    return final_path


def _unlink_quietly(path: Path) -> bool:
    """Delete-if-possible, never raise: a concurrent deletion or transient OS error during a
    best-effort sweep must not abort the sweep or, for `prune_expired`, escape untyped."""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _sweep_prunable(cache_dir: Path, is_prunable: Callable[[CacheEntry], bool]) -> PruneSummary:
    """Shared typed-total core for both deletion paths: scan every `*.json` entry once, always
    remove a corrupt/unreadable one, and remove a decodable one iff `is_prunable(entry)`. No
    exception escapes a single bad or racing file."""
    if not cache_dir.is_dir():
        return PruneSummary(scanned=0, removed=0, corrupt=0, bytes_reclaimed=0)
    scanned = removed = corrupt = bytes_reclaimed = 0
    for path in sorted(cache_dir.glob("*.json")):
        scanned += 1
        try:
            size = path.stat().st_size
        except OSError:
            continue
        try:
            entry = _decode(path.read_text(encoding="utf-8"))
        except (CacheError, OSError):
            corrupt += 1
            if _unlink_quietly(path):
                removed += 1
                bytes_reclaimed += size
            continue
        if is_prunable(entry):
            if _unlink_quietly(path):
                removed += 1
                bytes_reclaimed += size
    return PruneSummary(scanned=scanned, removed=removed, corrupt=corrupt, bytes_reclaimed=bytes_reclaimed)


def _self_prune_after_write(cache_dir: Path, entry: CacheEntry) -> PruneSummary | None:
    """See module docstring "Slice 12D" for the monotonic-safety rationale. Returns `None` (skips
    pruning for this write) when the entry carries no usable timestamp; this is a best-effort
    maintenance step, never a reason to fail the write that just succeeded."""
    retrieved_at = entry.evidence.retrieved_at
    if retrieved_at is None:
        return None
    try:
        return _sweep_prunable(cache_dir, lambda other: retrieved_at > other.expires_at)
    except TypeError:
        return None


def prune_expired(cache_dir: Path, *, now: datetime, ttl: timedelta) -> PruneSummary:
    """Explicit deletion control -- see module docstring "Slice 12D". An entry is removed once it
    has genuinely expired (`now > expires_at`, the same condition `read_cache` already treats as a
    miss) or its `expires_at` is implausibly far in the future for the given `ttl`
    (`expires_at - now > ttl`, a tamper/corruption ceiling that a legitimately built live entry can
    never trip, since `build_cache_entry` never grants more than `ttl` of remaining life). Corrupt
    or unreadable entries are always removed. Typed-total throughout."""
    return _sweep_prunable(cache_dir, lambda entry: now > entry.expires_at or entry.expires_at - now > ttl)


def cache_stats(cache_dir: Path, *, now: datetime) -> CacheStats:
    """Read-only visibility for `doctor` (design.md: "`doctor` is read-only"). Scans without ever
    deleting, decoding into pruning decisions, or otherwise modifying a file -- `doctor` MUST NOT
    delete anything; a separate explicit action would be required to expose `prune_expired` via the
    CLI, and none is added here. A corrupt entry still occupies disk space, so it is counted in
    `entries`/`total_bytes` even though it can never be validated as expired or live."""
    if not cache_dir.is_dir():
        return CacheStats(entries=0, expired=0, total_bytes=0)
    entries = expired = total_bytes = 0
    for path in sorted(cache_dir.glob("*.json")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries += 1
        total_bytes += size
        try:
            entry = _decode(path.read_text(encoding="utf-8"))
        except CacheError:
            continue
        if now > entry.expires_at:
            expired += 1
    return CacheStats(entries=entries, expired=expired, total_bytes=total_bytes)


def read_cache(cache_dir: Path, canonical_url: str, *, now: datetime) -> CacheLookup:
    """Read the cache entry for `canonical_url`. A missing file, a corrupt file, and an expired
    entry are ALL a miss for serving purposes (`hit=False`) -- expired content is never returned
    as current. `expired=True` distinguishes "was cached but is stale" from "never cached" for
    diagnostics without ever handing the caller stale content."""
    path = _cache_path(cache_dir, canonical_url)
    if not path.is_file():
        return CacheLookup(hit=False, expired=False, entry=None)
    try:
        entry = _decode(path.read_text(encoding="utf-8"))
    except CacheError:
        return CacheLookup(hit=False, expired=False, entry=None)
    if now > entry.expires_at:
        return CacheLookup(hit=False, expired=True, entry=None)
    return CacheLookup(hit=True, expired=False, entry=entry)


def build_cache_entry(
    evidence: EvidenceRecord,
    *,
    retrieved_at: datetime,
    ttl: timedelta,
    body: bytes,
    max_excerpt_chars: int,
    policy_version: str,
) -> CacheEntry:
    """Build the entry that will actually be persisted. Only `evidence.reuse == "permitted"` may
    store a body-derived excerpt up to `max_excerpt_chars`; every other state (`restricted`,
    `prohibited`, or `unknown` -- the DEFAULT, since `fetch.py` has no pack context and always
    reports `license=reuse="unknown"`) is capped to a small, bounded citation excerpt only, never
    treated as complete content -- "Cache expiry or prohibited reuse": "retained material is
    limited to permitted fields."""
    text = body.decode("utf-8", errors="replace")
    excerpt_only = evidence.reuse != "permitted"
    limit = min(max_excerpt_chars, 280) if excerpt_only else max_excerpt_chars
    return CacheEntry(
        evidence=evidence,
        expires_at=retrieved_at + ttl,
        excerpt=text[:limit],
        excerpt_only=excerpt_only,
        policy_version=policy_version,
    )


__all__ = [
    "CacheEntry", "CacheError", "CacheLookup", "CacheStats", "PruneSummary", "ReuseState",
    "build_cache_entry", "cache_key", "cache_stats", "prune_expired", "read_cache",
    "write_cache_atomic",
]
