# The Windows half of the activation primitives. Every choice here was MEASURED on Windows 11 /
# NTFS / Python 3.13 before it was written; the spikes are recorded in the commit message rather
# than reconstructed from documentation, because the documentation is what made the first two
# attempts wrong.
#
# What POSIX provides and what replaces it:
#
#   fcntl.flock(LOCK_EX)      -> LockFileEx(LOCKFILE_EXCLUSIVE_LOCK). Measured: a second handle
#                                requesting the same range with LOCKFILE_FAIL_IMMEDIATELY is
#                                refused while the first holds it, and succeeds after release.
#
#   os.open(O_RDONLY|         -> CreateFileW WITHOUT FILE_SHARE_DELETE, plus an explicit reparse
#           O_NOFOLLOW)          point rejection. Two different jobs, both required -- see
#                                `open_pinned`.
#
#   sqlite open of /dev/fd/N  -> ordinary open BY PATH, safe only because `open_pinned` is holding
#                                the name. Windows has no /dev/fd and no way to hand SQLite a
#                                handle, so the guarantee is relocated rather than reproduced:
#                                POSIX cannot pin a name and therefore must pass the descriptor;
#                                Windows can pin the name and therefore does not have to.
#
#   os.replace() for the      -> SetFileInformationByHandle(FileRenameInfoEx) with
#   pointer swap                 FILE_RENAME_FLAG_POSIX_SEMANTICS. This is the one that is NOT
#                                interchangeable: see `posix_replace`.
#
#   os.pread                  -> seek/read/restore, adequate here for a stated reason -- see `pread`.
#
# Deliberately ABSENT: any identity helper. `os.fstat` on Windows already reports a real file
# identity -- measured, `st_ino` is exactly the Win32 file index and agrees with `os.stat` on the
# same file -- so `pointer.identity` and `pointer.identity_matches` are already correct here and
# reimplementing them in ctypes would add a second source of truth for no gain.
from __future__ import annotations

import ctypes
import msvcrt
import os
import stat
from ctypes import wintypes
from pathlib import Path

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
_FileRenameInfoEx = 22
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_LOCK_RANGE = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE),
    ]


class _FILE_RENAME_INFO(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
        ("FileNameLength", wintypes.DWORD), ("FileName", wintypes.WCHAR * 1),
    ]


_NAME_OFFSET = _FILE_RENAME_INFO.FileName.offset


def _create(path: Path, access: int, share: int, flags: int = 0) -> int:
    handle = _kernel32.CreateFileW(str(path), access, share, None, OPEN_EXISTING, flags, None)
    if handle == _INVALID_HANDLE_VALUE:
        raise OSError(0, "CreateFileW failed", str(path), ctypes.get_last_error())
    return handle


def open_pinned(path: Path) -> int:
    """Open `path` read-only so that, for as long as the descriptor lives, the NAME cannot come to
    mean a different file. Returns an ordinary file descriptor.

    This carries the weight `O_RDONLY|O_NOFOLLOW` plus `/dev/fd` carry together on POSIX, and it
    splits into two independent requirements:

    Omitting FILE_SHARE_DELETE is what pins the name. Measured: while such a handle is open,
    `os.replace`, `os.rename` and `os.unlink` against that path all fail. That is why the caller may
    then let SQLite open the same path directly -- the path cannot have been swapped in between,
    which is the exact property `/dev/fd/N` buys on POSIX. It is also why this MUST NOT be relaxed
    to include FILE_SHARE_DELETE for symmetry with `open_shared`: the two functions want opposite
    things, deliberately.

    FILE_FLAG_OPEN_REPARSE_POINT is the no-follow half, and it is NOT equivalent on its own.
    `O_NOFOLLOW` FAILS on a symlink; this flag SUCCEEDS and hands back the link itself. Opening the
    link and then treating it as the target would be strictly worse than following it, so the
    reparse point is rejected explicitly right after the open."""
    handle = _create(
        path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, FILE_FLAG_OPEN_REPARSE_POINT
    )
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    try:
        if os.fstat(descriptor).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(0, "refusing to open a reparse point", str(path))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_shared(path: Path) -> int:
    """Open `path` read-only in a way that never blocks a concurrent publisher.

    The mirror image of `open_pinned`, for the pointer rather than for a generation. A reader that
    does not grant FILE_SHARE_DELETE denies deletion of what it is reading, and on Windows that
    stops the writer: measured, a pointer swap under such a reader fails with a sharing violation
    even when the writer uses POSIX rename semantics. Granting it restores exactly the POSIX
    arrangement -- the swap proceeds, and this descriptor goes on reading the complete generation it
    opened, because the replaced file stays alive until its last handle closes."""
    return msvcrt.open_osfhandle(
        _create(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE),
        os.O_RDONLY | os.O_BINARY,
    )


def posix_replace(source: Path, target: Path) -> None:
    """Rename `source` onto `target` with POSIX semantics, raising `OSError` if that is unavailable.

    `os.replace` cannot be used and no share mode rescues it. On Windows a file with a pending
    delete KEEPS ITS NAME until the last handle closes, so `MoveFileExW` cannot put anything at a
    name that someone still has open -- measured: it fails with ERROR_ACCESS_DENIED under a reader,
    under every share mode, while succeeding with no reader at all. POSIX `rename` detaches the name
    from the file immediately, and detaching is the whole basis of publishing a generation while
    readers are mid-read.

    `FILE_RENAME_FLAG_POSIX_SEMANTICS` (Windows 10 1607+, NTFS) asks for precisely that detachment.
    Measured against a share-delete reader: the swap succeeds, the reader's open descriptor keeps
    returning the OLD generation in full, and a fresh open sees the new one.

    There is deliberately NO fallback to `os.replace` when this is unsupported. Falling back would
    turn an unavailable guarantee into a silently weaker one on old Windows or non-NTFS volumes,
    and a promotion that quietly stops being atomic is the failure mode this whole module exists to
    avoid. Failing here is the honest outcome."""
    handle = _create(
        source, DELETE | SYNCHRONIZE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    )
    try:
        encoded = str(Path(target).absolute()).encode("utf-16-le")
        size = _NAME_OFFSET + len(encoded) + 2
        buffer = ctypes.create_string_buffer(size)
        info = ctypes.cast(buffer, ctypes.POINTER(_FILE_RENAME_INFO)).contents
        info.Flags = FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS
        info.RootDirectory = None
        info.FileNameLength = len(encoded)
        ctypes.memmove(ctypes.addressof(buffer) + _NAME_OFFSET, encoded, len(encoded))
        if not _kernel32.SetFileInformationByHandle(handle, _FileRenameInfoEx, buffer, size):
            raise OSError(
                0, "POSIX-semantics rename unavailable", str(target), ctypes.get_last_error()
            )
    finally:
        _kernel32.CloseHandle(handle)


def acquire_exclusive(descriptor: int) -> None:
    """Block until this descriptor holds the activation lock exclusively -- `flock(LOCK_EX)`.

    Omitting LOCKFILE_FAIL_IMMEDIATELY is what makes it block rather than fail, which is the
    behaviour `serialized` depends on: a contended promotion must WAIT for the other one, not error
    out. (`msvcrt.locking` cannot express this: it gives up after roughly ten seconds and raises,
    turning a slow holder into a failed promotion.)"""
    if not _kernel32.LockFileEx(
        msvcrt.get_osfhandle(descriptor), _LOCKFILE_EXCLUSIVE_LOCK, 0,
        _LOCK_RANGE, 0, ctypes.byref(_OVERLAPPED()),
    ):
        raise OSError(0, "LockFileEx failed", None, ctypes.get_last_error())


def release(descriptor: int) -> None:
    """Release the activation lock. Tolerates an unheld lock so the `finally` that calls this can
    never mask the exception that got it there -- POSIX `LOCK_UN` on an unheld lock is a no-op, and
    the caller should see the original failure, not this one."""
    try:
        _kernel32.UnlockFileEx(
            msvcrt.get_osfhandle(descriptor), 0, _LOCK_RANGE, 0, ctypes.byref(_OVERLAPPED())
        )
    except OSError:
        pass


def flush(path: Path) -> None:
    """Force `path`'s contents to stable storage -- the `fsync` a read-only descriptor cannot do.

    Windows refuses `fsync` on a handle without write access, and the descriptor held during
    promotion is read-only BY DESIGN: it is the pin, and reopening it writable would be the one
    thing the pin exists to prevent. So this takes a SEPARATE, short-lived handle purely to flush.
    It requests write ACCESS and writes nothing; the pin's share mode already permits it, and the
    file is not modified.

    The alternative was to let `durable` report False on every Windows promotion. That was rejected:
    the bytes reaching disk is exactly what `durable` claims, SQLite has already committed them, and
    a flag that is permanently False is a false alarm. A false alarm costs the same trust as a false
    assurance -- it just spends it in the other direction."""
    handle = _create(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE)
    try:
        if not _kernel32.FlushFileBuffers(handle):
            raise OSError(0, "FlushFileBuffers failed", str(path), ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(handle)


def pread(descriptor: int, length: int, offset: int) -> bytes:
    """Read at `offset` without permanently disturbing the descriptor's file position.

    `os.pread` does not exist on Windows. This restores the position afterwards rather than never
    moving it, so it is not atomic against a concurrent read on the SAME descriptor -- which is
    adequate here, and only because of what the one caller does next. `skillset._read_descriptor`
    reads a generation from a descriptor it alone owns, and the operations that follow are a flush
    (position-independent) and an identity re-check performed on the PATH, not on this descriptor's
    offset. If a future caller shares a descriptor across threads, this stops being sufficient and
    needs ReadFile with an explicit OVERLAPPED offset."""
    previous = os.lseek(descriptor, 0, os.SEEK_CUR)
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        return os.read(descriptor, length)
    finally:
        os.lseek(descriptor, previous, os.SEEK_SET)


__all__ = [
    "acquire_exclusive", "flush", "open_pinned", "open_shared", "posix_replace", "pread", "release",
]
