from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Generic atomic-pointer and activation-lock primitives, extracted verbatim from `index.py` so a
# second artifact kind can reuse the same guarantees instead of duplicating ~105 lines of
# atomicity-critical code. Behaviour is byte-identical to the original: same checks, in the same
# order, raising the same codes.
#
# Only three couplings here were ever specific to the corpus index -- the pointer entry key set,
# the entry key whose value names a sibling file (and is therefore traversal-checked), and the
# exception type. Those are now explicit parameters. `error` is the exception CLASS rather than a
# factory function because `controlled_file` both raises it and catches it, and a class serves both
# roles while a factory could only serve one.
#
# What deliberately did NOT move: `_open_descriptor` (SQLite-specific) and `_validated_entry`
# (`BuildConfig`-specific) stay in `index.py`. This module knows nothing about databases.

ErrorType = type[BaseException]


def read_pointer(
    pointer: Path, *, entry_keys: frozenset[str], name_key: str, error: ErrorType
) -> dict[str, object]:
    """Parse and structurally validate a pointer file. Rejects symlinked pointers, unexpected
    shapes, and any entry whose `name_key` value is not a bare filename (path traversal)."""
    if pointer.is_symlink():
        raise error("invalid_active_pointer")
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as cause:
        raise error("invalid_active_pointer") from cause
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "active", "retained"}
        or value["version"] != 1
        or not isinstance(value["active"], dict)
        or not isinstance(value["retained"], list)
    ):
        raise error("invalid_active_pointer")
    for entry in [value["active"], *value["retained"]]:
        if (
            not isinstance(entry, dict)
            or set(entry) != set(entry_keys)
            or not all(isinstance(item, str) and item for item in entry.values())
            or Path(entry[name_key]).name != entry[name_key]
        ):
            raise error("invalid_active_pointer")
    return value


def write_pointer(
    pointer: Path, active: dict[str, str], retained: list[dict[str, str]]
) -> bool:
    """Atomically replace the pointer. Returns False when the parent-directory fsync fails, so the
    caller can report reduced durability rather than treating a written pointer as lost."""
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{pointer.name}.", suffix=".tmp",
        dir=pointer.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump({"version": 1, "active": active, "retained": retained}, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, pointer)
        try:
            directory = os.open(pointer.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            return False
        return True
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def identity(file_descriptor: int) -> tuple[int, int, int, int]:
    value = os.fstat(file_descriptor)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def controlled_file(
    pointer: Path, name: str, *, error: ErrorType
) -> tuple[Path, int, tuple[int, int, int, int]]:
    """Open `name` beside `pointer` under symlink and containment control, returning its identity so
    the caller can detect a swap between validation and use."""
    path = pointer.parent / name
    descriptor = -1
    try:
        if path.is_symlink() or path.resolve(strict=True).parent != pointer.parent.resolve(strict=True):
            raise error("invalid_active_target")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        file_identity = identity(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise error("invalid_active_target")
        return path, descriptor, file_identity
    except error:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as cause:
        if descriptor >= 0:
            os.close(descriptor)
        raise error("invalid_active_target") from cause


def identity_matches(path: Path, expected: tuple[int, int, int, int]) -> bool:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) == expected


@contextmanager
def activation_lock(pointer: Path):
    descriptor = os.open(pointer.parent / f".{pointer.name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def serialized(pointer_position: int):
    """Serialize an operation on the pointer at `args[pointer_position]` under an exclusive lock."""
    def decorate(operation):
        def locked(*args, **kwargs):
            with activation_lock(args[pointer_position]):
                return operation(*args, **kwargs)
        return locked
    return decorate


__all__ = [
    "ErrorType",
    "activation_lock",
    "controlled_file",
    "identity",
    "identity_matches",
    "read_pointer",
    "serialized",
    "write_pointer",
]
