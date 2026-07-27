"""Bruriah: a read-only, two-tool MCP knowledge router over your own corpus."""
from __future__ import annotations

import importlib.util
import os

__version__ = "0.5.0"

# Bruriah needs a platform that can promote a snapshot ATOMICALLY: take an exclusive lock, open a
# validated file in a way that cannot be swapped underneath it, and publish a pointer while readers
# are mid-read. Two families can do that, by different means, and `winfs.py` records which call
# replaces which and what was measured to confirm it. Anything else cannot, and there is no partial
# mode -- a half-ported activation is worse than none, because it fails silently instead of loudly.
#
# This check still asks about the CAPABILITY rather than about `sys.platform`, which is what kept it
# honest when Windows was unsupported and is what keeps it honest now that Windows is supported: the
# question was never "which OS is this", it was "can this OS keep the promise".
#
# One capability is NOT verified here, deliberately. Windows atomic publication needs POSIX-semantics
# rename (Windows 10 1607+ on NTFS); probing for it at import would mean writing files as a side
# effect of `import bruriah`. It is instead enforced at the only place it matters -- `winfs`'s
# `posix_replace`, which fails loudly rather than degrading to a swap that is no longer atomic. A
# FAT32 or exFAT data directory therefore installs fine and refuses at promotion, with an error that
# names the reason.
#
# The wheel is `py3-none-any`, so pip installs it happily anywhere, and before this check existed the
# first command died with a bare `ModuleNotFoundError: No module named 'fcntl'` several frames deep.
# That reads as a broken package rather than an unsupported platform.
if (
    importlib.util.find_spec("fcntl") is None and os.name != "nt"
):  # pragma: no cover -- only reachable on a platform that is neither
    raise ImportError(
        "Bruriah requires Linux, macOS or Windows: promoting a snapshot atomically needs either "
        "POSIX file locking (fcntl) or the Win32 file primitives, and this platform provides "
        "neither.\n"
        "\n"
        "If you are on something else that can express an exclusive lock, a no-follow open and a "
        "rename that detaches a name while readers hold it, open an issue saying which -- those "
        "three are the whole requirement, and `src/bruriah/winfs.py` shows what a second "
        "implementation of them looks like."
    )
