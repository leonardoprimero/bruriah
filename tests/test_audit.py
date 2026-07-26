# Slice 9B unit tests for the content-free redacted audit trail. No network.
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from bruriah.audit import AuditRecord, append_audit, read_audit_records

_TIMESTAMP = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _record(**overrides: object) -> AuditRecord:
    payload = dict(
        request_id="sha256:" + "a" * 64, destination_host="example.test",
        destination_class="public_https", decision="fetched", code="ok",
        bytes_transferred=128, elapsed_ms=42, timestamp=_TIMESTAMP,
    )
    payload.update(overrides)
    return AuditRecord(**payload)


def test_append_then_read_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "research.jsonl"
    append_audit(path, _record())
    records = read_audit_records(path)
    assert len(records) == 1
    assert records[0] == _record()


def test_append_is_append_only_never_truncates(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "research.jsonl"
    append_audit(path, _record(request_id="sha256:" + "1" * 64))
    append_audit(path, _record(request_id="sha256:" + "2" * 64))
    append_audit(path, _record(request_id="sha256:" + "3" * 64))
    records = read_audit_records(path)
    assert [record.request_id[-1] for record in records] == ["1", "2", "3"]


def test_audit_file_and_dir_are_0600_0700_on_posix(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "research.jsonl"
    append_audit(path, _record())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_missing_audit_file_reads_as_empty_list(tmp_path: Path) -> None:
    assert read_audit_records(tmp_path / "audit" / "research.jsonl") == []


def test_record_carries_only_the_closed_field_set(tmp_path: Path) -> None:
    """A structural guard against ever widening the audit record with a free-text field (e.g. a
    'notes' or 'raw_url' string) that could accidentally carry a query or secret."""
    path = tmp_path / "audit" / "research.jsonl"
    append_audit(path, _record())
    line = path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    assert set(payload) == {
        "request_id", "destination_host", "destination_class", "decision", "code",
        "bytes_transferred", "elapsed_ms", "timestamp",
    }
