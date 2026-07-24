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
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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
    prior complete entry or the new complete entry -- never a partial write."""
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
    return final_path


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
    "CacheEntry", "CacheError", "CacheLookup", "ReuseState", "build_cache_entry", "cache_key",
    "read_cache", "write_cache_atomic",
]
