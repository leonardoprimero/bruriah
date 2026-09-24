from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .. import cache
from ..platform import PlatformError, PlatformPaths, load_registry, open_snapshot
from .common import resolve_cli_paths

_FRESHNESS_WARNING_DAYS = 7
_EXPIRY_WARNING_DAYS = 90


def run_doctor(
    paths: PlatformPaths,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only: resolved dirs, registry load, snapshot open, cache stats. Never creates/writes
    anything -- including the cache: `cache.cache_stats` only reads (never calls
    `cache.prune_expired`), so a mutating prune stays out of `doctor` entirely (Slice 12D).
    Adds an early WARNING within `_FRESHNESS_WARNING_DAYS` of pack staleness; the hard
    fail-closed staleness/expiry check in `load_registry` itself is unchanged."""
    effective_today = today or date.today()
    effective_now = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "config_dir": str(paths.config_dir),
        "data_dir": str(paths.data_dir),
        "dirs_exist": {
            "config": paths.config_dir.is_dir(),
            "data": paths.data_dir.is_dir(),
            "cache": paths.cache_dir.is_dir(),
            "log": paths.log_dir.is_dir(),
        },
        "network_enabled": paths.network_enabled,
        # Surfaced because six first-party skills ship and the default admits five: without this,
        # the operator sees `skill_ceiling_exceeded:1` in a response and has no way to learn what
        # the number is, let alone that it is theirs to change.
        "skill_ceiling": paths.skill_ceiling,
        # Every site that narrows permissions is already guarded by `os.name == "posix"`, so on
        # Windows those calls correctly do nothing rather than pretending -- `os.chmod` there only
        # toggles a read-only attribute and would be theatre. What was missing is that the user had
        # no way to LEARN this. In practice the data lives under the per-user profile directory,
        # whose inherited ACL already denies other standard users, so the protection is real; it is
        # just not the one the code asked for, and not one this process verified. Writing an
        # explicit DACL was considered and rejected: a hand-rolled ACL that looks restrictive while
        # inheriting something permissive is precisely the silently-weaker outcome this codebase
        # refuses everywhere else. Saying so out loud is the honest version.
        "owner_only_file_modes": os.name == "posix",
        "warnings": (
            []
            if os.name == "posix"
            else [
                "this platform does not enforce owner-only file modes; private data is protected by "
                "the user profile directory's inherited permissions, which bruriah does not verify"
            ]
        ),
    }
    try:
        registry = load_registry(effective_today)
        report["registry"] = {
            "status": "ok",
            "pack_ids": list(registry.pack_ids),
            # Per pack, because the registry loading is no longer the same question as every pack
            # in it still being able to speak. Without this the operator sees `status: ok` on the
            # day a domain stopped being routed and has nothing to connect the two.
            "pack_currency": {pack_id: registry.currency_of(pack_id) for pack_id in registry.pack_ids},
        }
        for pack in registry.packs:
            days_left = (pack.reviewed_at + timedelta(days=pack.freshness_days) - effective_today).days
            if days_left <= _FRESHNESS_WARNING_DAYS:
                report["warnings"].append(f"pack {pack.pack_id} goes stale in {days_left} day(s)")
            expires_in = (pack.expires_at - effective_today).days
            if expires_in <= _EXPIRY_WARNING_DAYS:
                report["warnings"].append(
                    f"pack {pack.pack_id} expires in {expires_in} day(s), on {pack.expires_at}; "
                    f"after that, requests in {', '.join(pack.domains)} abstain with a "
                    f"pack_expired:{pack.pack_id} gap until re-signed packs ship -- upgrade "
                    "before then"
                )
    except PlatformError as error:
        report["registry"] = {"status": "error", "code": error.code}
    try:
        snapshot = open_snapshot(paths)
        snapshot.database.close()
        report["snapshot"] = {"status": "ok", "build_id": snapshot.build_id}
    except PlatformError as error:
        report["snapshot"] = {"status": "error", "code": error.code}
    stats = cache.cache_stats(paths.cache_dir, now=effective_now)
    report["cache"] = {
        "entries": stats.entries,
        "expired": stats.expired,
        "total_bytes": stats.total_bytes,
    }
    report["healthy"] = report["registry"].get("status") == "ok" and report["snapshot"].get("status") == "ok"
    return report


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = resolve_cli_paths(args)
    report = run_doctor(paths)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["healthy"] else 1
