# Slice 9B (unit 1 of 2): the content-free redacted audit trail for `research.py`. Append-only
# JSON Lines; every record carries ONLY identifiers, a closed destination classification,
# decisions, counts, and timings -- NEVER the task/query text, the destination path or query
# string (either can encode a secret token or the caller's private task), response body, request
# or response headers, or any secret. design.md "Network": "Audit records IDs, destination
# class, decisions, counts and timings -- never query/body/secret content." Only the destination
# HOST is recorded, never the full URL.
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

AuditDecision = Literal[
    "disabled", "not_warranted", "cached", "fetched", "refused", "degraded", "error",
]
DestinationClass = Literal["public_https", "none"]
_RECORD_FIELDS = (
    "request_id", "destination_host", "destination_class", "decision", "code",
    "bytes_transferred", "elapsed_ms", "timestamp",
)


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    destination_host: str | None
    destination_class: DestinationClass
    decision: AuditDecision
    code: str
    bytes_transferred: int
    elapsed_ms: int
    timestamp: datetime


def _encode(record: AuditRecord) -> str:
    return json.dumps(
        {
            "request_id": record.request_id,
            "destination_host": record.destination_host,
            "destination_class": record.destination_class,
            "decision": record.decision,
            "code": record.code,
            "bytes_transferred": record.bytes_transferred,
            "elapsed_ms": record.elapsed_ms,
            "timestamp": record.timestamp.isoformat(),
        },
        sort_keys=True,
    )


def append_audit(audit_path: Path, record: AuditRecord) -> None:
    """Append one redacted line; never truncates or reorders prior entries -- an audit trail, not
    a cache. The directory/file are private (0700/0600) on POSIX after every write."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as handle:
        handle.write(_encode(record) + "\n")
    if os.name == "posix":
        os.chmod(audit_path.parent, 0o700)
        os.chmod(audit_path, 0o600)


def read_audit_records(audit_path: Path) -> list[AuditRecord]:
    """Diagnostic/test helper: parse every line back into an `AuditRecord`. Not used by
    `research.py` itself -- production audit access is append-only, write-side only."""
    if not audit_path.is_file():
        return []
    records: list[AuditRecord] = []
    for line in audit_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict) or set(payload) != set(_RECORD_FIELDS):
            raise ValueError("corrupt_audit_record")
        records.append(
            AuditRecord(
                request_id=payload["request_id"],
                destination_host=payload["destination_host"],
                destination_class=payload["destination_class"],
                decision=payload["decision"],
                code=payload["code"],
                bytes_transferred=payload["bytes_transferred"],
                elapsed_ms=payload["elapsed_ms"],
                timestamp=datetime.fromisoformat(payload["timestamp"]),
            )
        )
    return records


__all__ = ["AuditDecision", "AuditRecord", "DestinationClass", "append_audit", "read_audit_records"]
