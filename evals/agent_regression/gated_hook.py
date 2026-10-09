"""The `gated` condition's Claude Code `PreToolUse` hook: deny the run's first native file mutation.

The adapter copies this file into a fresh owner-only directory per run, outside the clone the agent
writes, and registers it through per-run `--settings` with the path of its state file
(`gate-state.json`, in the same directory) as its one argument. The client sends the tool call as
one JSON object on stdin; the first `Edit`, `Write` or `MultiEdit` is denied with exit code 2,
whose stderr the client hands back to the agent as the reason, and every later call is allowed, so
the agent can retry and the run can complete. The state file is written whole to a private
temporary file and hard-linked into place, which fails if it already exists: two calls racing for
the first denial cannot both get it, and no call ever sees a partial state file.

The gate fails closed. The client treats any exit code but 2 as "allow", so a hook that crashed
would let the edit through with no denial on record, indistinguishable from an agent that ignored
the gate. Instead, a payload that cannot be parsed, or a gated call whose state cannot be recorded,
is denied with `GATE_ERROR_REASON`, every time, and spends nothing: a failed write leaves no state
file behind, so once the gate can record state again the first denial is still there to give.

Standard library only: the client runs it as `python -I`, with no project on the path.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

GATED_TOOLS = ("Edit", "Write", "MultiEdit")
STATE_FILE = "gate-state.json"

# What the agent reads when its first edit is refused. Like the prompted instruction it names the
# tools, not the product. Pinned: changing it changes what `gated` measures.
GATE_REASON = (
    "This edit was blocked by a pre-edit gate, once, before any file in this repository changes. Review the "
    "historical decision behind this code first: call the `investigate_work` tool with the task you were given "
    "and read the evidence it returns with `read_evidence`. Then retry the edit if it is still the right change; "
    "the retry will be allowed."
)

# What the agent reads when the gate itself fails; the error follows it in parentheses. Pinned and
# distinct from `GATE_REASON`, so a transcript tells a broken gate from a working one.
GATE_ERROR_REASON = (
    "This edit was blocked because the pre-edit gate is broken: it could not read this tool call or record "
    "its state, so it cannot tell whether the edit may go ahead. Every edit is blocked until the gate works."
)


def gate(state: Path, payload: object) -> tuple[int, str]:
    """`(exit code, stderr)` for one hook call: 2 and the reason for the first gated tool call,
    0 and nothing for anything else. A call that is not a gated tool never spends the denial; a
    gated call whose state cannot be recorded is denied as a broken gate and spends nothing."""
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    if tool not in GATED_TOOLS:
        return 0, ""
    try:
        first = _claim(state, tool)
    except OSError as error:
        return 2, _broken(error)
    return (2, GATE_REASON) if first else (0, "")


def _claim(state: Path, tool: str) -> bool:
    """Record `tool` as the denied call in `state`; False if an earlier call already has. The
    temporary file is removed whatever happens, so a failed write never leaves `state` behind."""
    descriptor, temporary = tempfile.mkstemp(prefix=f"{state.name}.", suffix=".tmp", dir=state.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"denied": tool}) + "\n")
        os.link(temporary, state)
    except FileExistsError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
    return True


def _broken(error: Exception) -> str:
    return f"{GATE_ERROR_REASON} ({type(error).__name__}: {error})"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: gated_hook.py <state file>", file=sys.stderr)
        return 1
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, ValueError) as error:
        # The client broke its side of the contract: the tool is unknown, so it may be a gated one.
        code, message = 2, _broken(error)
    else:
        code, message = gate(Path(argv[0]), payload)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
