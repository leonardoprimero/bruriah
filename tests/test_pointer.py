from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from bruriah import pointer as pointer_module
from bruriah.pointer import (
    activation_lock,
    controlled_file,
    identity,
    identity_matches,
    read_pointer,
    serialized,
    write_pointer,
)

# `pointer.py` holds the atomicity-critical primitives extracted from `index.py`. `index.py`'s own
# suite proves the corpus-index wiring still behaves identically; these tests pin the primitives
# directly, including the three parameters that used to be hardcoded (entry key set, traversal-
# checked name key, error type), so a second artifact kind cannot reuse them with weaker checks.

ENTRY_KEYS = frozenset({"database", "build_id"})


class Boom(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _write(pointer: Path, payload: object) -> None:
    pointer.write_text(json.dumps(payload), encoding="utf-8")


def _valid(name: str = "snap.sqlite3") -> dict[str, object]:
    return {
        "version": 1,
        "active": {"database": name, "build_id": "b1"},
        "retained": [],
    }


def _read(pointer: Path, *, entry_keys: frozenset[str] = ENTRY_KEYS, name_key: str = "database"):
    return read_pointer(pointer, entry_keys=entry_keys, name_key=name_key, error=Boom)


def test_read_pointer_accepts_a_well_formed_pointer(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    _write(target, _valid())
    assert _read(target)["active"] == {"database": "snap.sqlite3", "build_id": "b1"}


def test_read_pointer_raises_the_caller_supplied_error_type(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    _write(target, {"version": 2, "active": {}, "retained": []})
    with pytest.raises(Boom) as error:
        _read(target)
    assert error.value.code == "invalid_active_pointer"


def test_read_pointer_rejects_a_symlinked_pointer(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    _write(real, _valid())
    link = tmp_path / "active.json"
    link.symlink_to(real)
    with pytest.raises(Boom):
        _read(link)


@pytest.mark.parametrize("name", ["../escape.sqlite3", "nested/snap.sqlite3", "/abs.sqlite3"])
def test_read_pointer_rejects_path_traversal_in_the_name_key(tmp_path: Path, name: str) -> None:
    # The name key must stay a bare filename; anything with a directory component escapes the
    # pointer's own directory, which is the whole point of the containment check.
    target = tmp_path / "active.json"
    _write(target, _valid(name))
    with pytest.raises(Boom):
        _read(target)


def test_read_pointer_rejects_unreadable_and_malformed_files(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    with pytest.raises(Boom):
        _read(missing)
    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(Boom):
        _read(malformed)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"version": 1, "active": {"database": "a", "build_id": "b"}},
        {"version": 1, "active": [], "retained": []},
        {"version": 1, "active": {"database": "a", "build_id": "b"}, "retained": {}},
        {"version": 1, "active": {"database": "a", "build_id": ""}, "retained": []},
        {"version": 1, "active": {"database": "a", "build_id": 7}, "retained": []},
    ],
)
def test_read_pointer_rejects_malformed_shapes(tmp_path: Path, payload: object) -> None:
    target = tmp_path / "active.json"
    _write(target, payload)
    with pytest.raises(Boom):
        _read(target)


def test_read_pointer_validates_retained_entries_not_only_active(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    _write(
        target,
        {
            "version": 1,
            "active": {"database": "ok.sqlite3", "build_id": "b1"},
            "retained": [{"database": "../escape.sqlite3", "build_id": "b0"}],
        },
    )
    with pytest.raises(Boom):
        _read(target)


def test_entry_keys_are_parameterized_not_hardcoded(tmp_path: Path) -> None:
    # A pointer written for a different artifact kind must validate under its OWN key set and be
    # rejected under the corpus index's -- proving the extraction really parameterized this.
    target = tmp_path / "active.json"
    _write(
        target,
        {"version": 1, "active": {"skillset": "s.json", "build_id": "b1"}, "retained": []},
    )
    skill_keys = frozenset({"skillset", "build_id"})
    assert _read(target, entry_keys=skill_keys, name_key="skillset")["active"]["skillset"] == "s.json"
    with pytest.raises(Boom):
        _read(target)


def test_write_pointer_is_atomic_deterministic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    active = {"database": "snap.sqlite3", "build_id": "b1"}
    assert write_pointer(target, active, [{"database": "old.sqlite3", "build_id": "b0"}]) is True
    raw = target.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert json.loads(raw) == {"version": 1, "active": active, "retained": [{"database": "old.sqlite3", "build_id": "b0"}]}
    # Deterministic serialization (sort_keys) so identical state produces identical bytes.
    write_pointer(target, active, [{"database": "old.sqlite3", "build_id": "b0"}])
    assert target.read_text(encoding="utf-8") == raw
    assert [path.name for path in tmp_path.iterdir()] == ["active.json"]


def test_write_pointer_replaces_an_existing_pointer_in_place(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    write_pointer(target, {"database": "first.sqlite3", "build_id": "b1"}, [])
    write_pointer(target, {"database": "second.sqlite3", "build_id": "b2"}, [])
    assert json.loads(target.read_text(encoding="utf-8"))["active"]["database"] == "second.sqlite3"
    assert not list(tmp_path.glob(".active.json.*.tmp"))


def test_write_pointer_reports_reduced_durability_without_losing_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The documented contract: a failed parent-directory fsync returns False rather than raising,
    # because the pointer IS written -- only its durability is uncertain.
    target = tmp_path / "active.json"
    calls: list[int] = []
    real_fsync = os.fsync

    def flaky(descriptor: int) -> None:
        calls.append(descriptor)
        if len(calls) == 1:
            real_fsync(descriptor)
            return
        raise OSError("no directory fsync here")

    monkeypatch.setattr(pointer_module.os, "fsync", flaky)
    assert write_pointer(target, {"database": "s.sqlite3", "build_id": "b1"}, []) is False
    assert json.loads(target.read_text(encoding="utf-8"))["active"]["build_id"] == "b1"


def test_write_pointer_cleans_up_and_propagates_on_serialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "active.json"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("serialization exploded")

    monkeypatch.setattr(pointer_module.json, "dump", boom)
    with pytest.raises(RuntimeError):
        write_pointer(target, {"database": "s.sqlite3", "build_id": "b1"}, [])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_controlled_file_opens_a_sibling_and_reports_its_identity(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    _write(target, _valid())
    sibling = tmp_path / "snap.sqlite3"
    sibling.write_bytes(b"payload")
    path, descriptor, file_identity = controlled_file(target, "snap.sqlite3", error=Boom)
    try:
        assert path == sibling
        assert file_identity == identity(descriptor)
        assert identity_matches(sibling, file_identity)
    finally:
        os.close(descriptor)


def test_controlled_file_rejects_a_symlinked_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real.sqlite3"
    real.write_bytes(b"payload")
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / "snap.sqlite3").symlink_to(real)
    with pytest.raises(Boom) as error:
        controlled_file(inner / "active.json", "snap.sqlite3", error=Boom)
    assert error.value.code == "invalid_active_target"


def test_controlled_file_rejects_a_non_regular_target(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    (tmp_path / "snap.sqlite3").mkdir()
    with pytest.raises(Boom) as error:
        controlled_file(target, "snap.sqlite3", error=Boom)
    assert error.value.code == "invalid_active_target"


def test_controlled_file_rejects_a_missing_target(tmp_path: Path) -> None:
    with pytest.raises(Boom):
        controlled_file(tmp_path / "active.json", "absent.sqlite3", error=Boom)


def test_identity_matches_detects_replacement_and_absence(tmp_path: Path) -> None:
    sibling = tmp_path / "snap.sqlite3"
    sibling.write_bytes(b"first")
    descriptor = os.open(sibling, os.O_RDONLY)
    try:
        before = identity(descriptor)
    finally:
        os.close(descriptor)
    assert identity_matches(sibling, before)
    sibling.unlink()
    assert identity_matches(sibling, before) is False
    sibling.write_bytes(b"second-and-longer")
    assert identity_matches(sibling, before) is False


def test_activation_lock_is_mutually_exclusive(tmp_path: Path) -> None:
    # Deterministic rather than sleep-based: a second open file description on the same lock file
    # must fail a non-blocking exclusive acquisition while the lock is held, and succeed after.
    target = tmp_path / "active.json"
    lock_path = tmp_path / ".active.json.lock"

    def try_acquire() -> bool:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(descriptor)

    with activation_lock(target):
        assert try_acquire() is False
    assert try_acquire() is True


def test_activation_lock_releases_when_the_body_raises(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    with pytest.raises(RuntimeError):
        with activation_lock(target):
            raise RuntimeError("body failed")
    # A leaked lock would deadlock every later activation, so re-entry must still succeed.
    with activation_lock(target):
        pass


def test_serialized_locks_the_pointer_at_the_declared_position(tmp_path: Path) -> None:
    target = tmp_path / "active.json"
    observed: list[bool] = []

    @serialized(1)
    def operation(_first: object, pointer: Path, *, extra: str) -> str:
        held = threading.Event()

        def probe() -> None:
            descriptor = os.open(pointer.parent / f".{pointer.name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                observed.append(True)
            except BlockingIOError:
                observed.append(False)
            finally:
                os.close(descriptor)
                held.set()

        worker = threading.Thread(target=probe)
        worker.start()
        held.wait(timeout=5)
        worker.join(timeout=5)
        return f"done-{extra}"

    assert operation("ignored", target, extra="x") == "done-x"
    assert observed == [False]
