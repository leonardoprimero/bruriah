"""Bruriah: a read-only, two-tool MCP knowledge router over your own corpus."""
from __future__ import annotations

import importlib.util

__version__ = "0.1.0"

# Bruriah needs POSIX file primitives and there is no partial mode without them. Activation swaps a
# pointer under `flock` with `O_NOFOLLOW` and re-confirms the file's identity afterwards, which is
# what makes promoting a snapshot atomic and unspoofable; Windows has different primitives with
# different semantics, and a half-ported version of that guarantee is worse than none, because it
# would fail silently instead of loudly.
#
# The wheel is `py3-none-any`, so pip installs it happily on Windows and the first command then
# died with a bare `ModuleNotFoundError: No module named 'fcntl'` raised several frames deep. That
# reads as a broken package rather than an unsupported platform. Detecting the capability rather
# than checking `sys.platform` keeps this honest about what is actually required.
if importlib.util.find_spec("fcntl") is None:  # pragma: no cover -- POSIX-only by construction
    raise ImportError(
        "Bruriah requires macOS or Linux: it depends on POSIX file locking (fcntl), which this "
        "platform does not provide.\n"
        "\n"
        "On Windows, run it under WSL -- that is a real Linux and needs no changes:\n"
        "    wsl --install          (once, then reopen your terminal)\n"
        "    pip install bruriah    (inside WSL)\n"
        "\n"
        "Native Windows support would mean reimplementing the atomic pointer swap that makes "
        "activation safe, and that is deliberately not done half-way. If you want it, open an "
        "issue saying so -- knowing someone is waiting is what would make it worth doing."
    )
