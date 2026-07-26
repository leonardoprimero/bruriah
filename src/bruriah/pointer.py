from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

# One platform decision, made once, at import. Every primitive below then reads as a single named
# operation instead of an `os.name` check repeated at each atomicity-critical site -- which is what
# a half-port looks like, and what makes one forgotten branch silently weaker than its neighbours.
# `winfs` documents what was measured for each substitution; the divergences that matter to a
# READER of this file are called out where they bite.
_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover -- exercised on Windows CI, not on the POSIX suite
    from . import winfs

    fcntl = None
else:
    import fcntl

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


def _read_pointer_text(pointer: Path) -> str:
    """Read the pointer without ever becoming the reason a publisher cannot publish.

    On POSIX this is just a read: a concurrent `rename` over the path succeeds regardless of who is
    reading, and this reader keeps the generation it opened. Windows denies deletion of an open file
    by default, and that default silently inverts the relationship -- the READER blocks the WRITER,
    so a routine `doctor` or `serve` startup could make a promotion fail. Measured: a pointer swap
    under an ordinarily-opened reader fails with a sharing violation even when the writer asks for
    POSIX rename semantics. Opting into share-delete is what restores the POSIX arrangement."""
    if not _WINDOWS:
        return pointer.read_text(encoding="utf-8")
    descriptor = winfs.open_shared(pointer)  # pragma: no cover -- Windows-only
    try:
        chunks = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(descriptor)


def read_pointer(
    pointer: Path, *, entry_keys: frozenset[str], name_key: str, error: ErrorType
) -> dict[str, object]:
    """Parse and structurally validate a pointer file. Rejects symlinked pointers, unexpected
    shapes, and any entry whose `name_key` value is not a bare filename (path traversal)."""
    if pointer.is_symlink():
        raise error("invalid_active_pointer")
    try:
        value = json.loads(_read_pointer_text(pointer))
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
        # NOT interchangeable with `os.replace` on Windows, and this is the single most important
        # substitution in the port. A Windows file with a pending delete KEEPS ITS NAME until its
        # last handle closes, so `MoveFileExW` -- which is what `os.replace` calls -- cannot put a
        # new file at a name someone is still reading, under any share mode. POSIX `rename` detaches
        # the name immediately, and that detachment IS how a generation gets published while readers
        # are mid-read. `winfs.posix_replace` asks NTFS for exactly that semantic and fails loudly
        # where it is unavailable rather than degrading to a swap that is no longer atomic.
        if _WINDOWS:  # pragma: no cover -- Windows-only
            winfs.posix_replace(temporary, pointer)
        else:
            os.replace(temporary, pointer)
        # POSIX needs the parent directory fsync'd separately: the rename above changed a directory
        # ENTRY, and nothing guarantees that reaches disk just because the call returned. Windows
        # offers no per-directory flush at all -- `os.open` on a directory fails outright -- and it
        # does not need one: `winfs.posix_replace` only ever runs on NTFS, which journals metadata
        # operations, so the entry is durable once the call returns. (The precondition is enforced,
        # not assumed: on a volume that cannot do POSIX-semantics rename, `posix_replace` refuses
        # rather than returning.) Reporting False here on every Windows promotion would have been a
        # permanent false alarm about the one thing `durable` exists to tell the truth about.
        if not _WINDOWS:
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
        # `winfs.open_pinned` carries MORE than `O_NOFOLLOW` does: besides refusing a reparse point,
        # its share mode makes the name un-renameable and un-deletable for the descriptor's
        # lifetime. That surplus is what lets `index.py` hand SQLite a PATH on Windows instead of
        # the `/dev/fd` entry POSIX requires -- the path provably still names this file.
        descriptor = (
            winfs.open_pinned(path) if _WINDOWS  # pragma: no cover -- Windows-only
            else os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        )
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


def flush_validated(path: Path, descriptor: int) -> bool:
    """Force a validated candidate's bytes to disk before it is published, reporting whether that
    actually happened rather than assuming it did.

    The descriptor is open read-only and must stay that way: it is the same one `controlled_file`
    opened, and re-opening it for writing would reintroduce exactly the swap window that check
    exists to close. POSIX permits `fsync` on a read-only descriptor, so there it is one call.
    Windows refuses it -- a handle with no write access has no buffers of the caller's to flush --
    so `winfs.flush` takes a separate, short-lived handle for the flush alone, leaving the pin
    untouched. `path` is safe to reopen precisely BECAUSE the pin is held: it cannot have come to
    name a different file.

    Returning False instead of raising keeps a genuine failure visible: the caller folds it into the
    same `durable` flag a failed parent-directory fsync already produces, so reduced durability is
    REPORTED rather than crashed on or silently claimed."""
    try:
        if _WINDOWS:  # pragma: no cover -- Windows-only
            winfs.flush(path)
        else:
            os.fsync(descriptor)
        return True
    except OSError:
        return False


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
        if _WINDOWS:  # pragma: no cover -- Windows-only
            winfs.acquire_exclusive(descriptor)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if _WINDOWS:  # pragma: no cover -- Windows-only
            winfs.release(descriptor)
        else:
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
    "flush_validated",
    "identity",
    "identity_matches",
    "read_pointer",
    "serialized",
    "write_pointer",
]
