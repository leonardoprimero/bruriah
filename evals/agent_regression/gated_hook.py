"""The `gated` condition's Claude Code `PreToolUse` hook: deny the run's first native file mutation.

The adapter copies this file into a fresh owner-only directory per run, outside the clone the agent
writes, and registers it through per-run `--settings` with the path of its state file
(`gate-state.json`, in the same directory) as its one argument. The client sends the tool call as
one JSON object on stdin; the first `Edit`, `Write` or `MultiEdit` is denied with exit code 2,
whose stderr the client hands back to the agent as the reason, and every later call is allowed, so
the agent can retry and the run can complete. The state file is created exclusively, so two calls
racing for the first denial cannot both get it.

Standard library only: the client runs it as `python -I`, with no project on the path.
"""

from __future__ import annotations

import json
import os
import sys
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


def gate(state: Path, payload: object) -> tuple[int, str]:
    """`(exit code, stderr)` for one hook call: 2 and the reason for the first gated tool call,
    0 and nothing for anything else. A call that is not a gated tool never spends the denial."""
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    if tool not in GATED_TOOLS:
        return 0, ""
    try:
        descriptor = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return 0, ""
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"denied": tool}) + "\n")
    return 2, GATE_REASON


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: gated_hook.py <state file>", file=sys.stderr)
        return 1
    try:
        payload = json.loads(sys.stdin.read())
    except ValueError:
        payload = None
    code, message = gate(Path(argv[0]), payload)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
