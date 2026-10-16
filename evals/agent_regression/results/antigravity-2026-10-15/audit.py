"""Audit what Bruriah returned to each `unprompted` Antigravity run, by replaying its calls.

The `agy` stream records every MCP call's arguments but never its output, so a transcript alone
cannot tell a retrieval failure (Bruriah never returned the decision) from a heed failure (it did,
and the agent went ahead anyway). Bruriah is deterministic over a fixed index, so this script
replays each run's Bruriah calls, in their recorded order, against a copy of the index that run
used, on the same `bruriah` executable, and records when the decision first came back.

For every `unprompted` run it writes one entry with:

- `consult_steps` and `read_evidence_steps`: the transcript steps of each Bruriah call;
- `decision_listed_step`: the first `investigate_work` reply that lists the trap's decision commit;
- `decision_read_step`: the first `read_evidence` reply whose content comes from the trap's
  decision document, the text that states the decision and its rationale;
- `first_write_step`: the first write to the clone;
- `final_message`: whether the run ended with a final message, and whether that message names the
  decision by any of the trap's citation cues or its commit.

It reads transcripts, which are not committed, so it runs only where the run set was recorded.
Its output, `audit.json`, holds no transcript text and no private path.

Usage:
    uv run python evals/agent_regression/results/antigravity-2026-10-15/audit.py \\
        --runs <run set dir> --cache-dir <adapter cache> --bruriah <bruriah executable> [--out audit.json]
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from agent_regression import citation, traps  # noqa: E402
from agent_regression.runs import WRITE_TOOLS  # noqa: E402

SERVER = "bruriah"
INDEX_SUFFIX = "b08a81dba2bdd"
TRAPS_DIR = HERE.parents[1] / "traps"


def bruriah_calls(transcript: Path) -> Iterator[tuple[int, str, dict[str, Any]]]:
    """Each completed Bruriah call of `transcript`: its step, tool name and arguments."""
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            step = json.loads(line).get("step_update") or {}
        except json.JSONDecodeError:
            continue
        parameters = (step.get("tool_info") or {}).get("parameters") or {}
        if (
            step.get("tool_name") == "call_mcp_tool"
            and step.get("state") == "DONE"
            and parameters.get("ServerName") == SERVER
        ):
            yield step["step_index"], parameters["ToolName"], parameters.get("Arguments") or {}


def first_write_step(transcript: Path) -> int | None:
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            step = json.loads(line).get("step_update") or {}
        except json.JSONDecodeError:
            continue
        if step.get("tool_name") in WRITE_TOOLS and step.get("state") == "DONE":
            return int(step["step_index"])
    return None


class Server:
    """One `bruriah serve` over MCP stdio, on a throwaway copy of an index."""

    def __init__(self, bruriah: Path, index: Path, models: Path) -> None:
        self._work = Path(tempfile.mkdtemp(prefix="bruriah-audit-"))
        subprocess.run(["cp", "-c", "-R", str(index), str(self._work / "index")], check=True)
        self._process = subprocess.Popen(
            [
                str(bruriah),
                "serve",
                "--data-dir",
                str(self._work / "index" / "data"),
                "--config-dir",
                str(self._work / "index" / "config"),
                "--cache-dir",
                str(models),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._next_id = 0
        self._request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "audit", "version": "0"}},
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, message: dict[str, Any]) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._process.stdout is not None
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        while True:
            reply = json.loads(self._process.stdout.readline())
            if reply.get("id") == self._next_id:
                return dict(reply)

    def call(self, tool: str, arguments: dict[str, Any]) -> str:
        reply = self._request("tools/call", {"name": tool, "arguments": arguments})
        content = (reply.get("result") or {}).get("content") or []
        return " ".join(item.get("text", "") for item in content if isinstance(item, dict))

    def close(self) -> None:
        self._process.terminate()
        self._process.wait(10)
        shutil.rmtree(self._work)


def audit_run(transcript: Path, trap: traps.Trap, server: Server) -> dict[str, Any]:
    short_ref = trap.decision_ref[:8]
    consult_steps: list[int] = []
    read_steps: list[int] = []
    listed: int | None = None
    read: int | None = None
    for step, tool, arguments in bruriah_calls(transcript):
        reply = server.call(tool, arguments)
        if tool == "investigate_work":
            consult_steps.append(step)
            decisions = json.loads(reply).get("decisions", []) if reply.startswith("{") else []
            if listed is None and any(decision.startswith(short_ref) for decision in decisions):
                listed = step
        elif tool == "read_evidence":
            read_steps.append(step)
            # Decision documents are named `<date>-<short sha>-<slug>.md`; a read of one is a read of the rationale.
            if read is None and f"-{short_ref}-" in reply:
                read = step
    message = citation.final_message(transcript)
    cues = (*trap.citation_cues, trap.decision_ref[: traps.SHA_CUE_LENGTH])
    return {
        "consult_steps": consult_steps,
        "read_evidence_steps": read_steps,
        "decision_listed_step": listed,
        "decision_read_step": read,
        "first_write_step": first_write_step(transcript),
        "final_message": {
            "present": bool(message),
            "names_decision": bool(message) and any(cue.lower() in (message or "").lower() for cue in cues),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the Bruriah calls of the unprompted Antigravity runs.")
    parser.add_argument("--runs", type=Path, required=True, help="the run set directory, with its transcripts")
    parser.add_argument("--cache-dir", type=Path, required=True, help="the adapter cache the runs used")
    parser.add_argument("--bruriah", type=Path, required=True, help="the bruriah executable the runs used")
    parser.add_argument("--out", type=Path, default=HERE / "audit.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    trap_set = {trap.trap_id: trap for trap in traps.load_traps(TRAPS_DIR)}
    records = [
        json.loads(line) for line in (args.runs / "runs.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    entries = []
    for record in sorted(records, key=lambda r: (r["trap_id"], r["repetition"])):
        if record["condition"] != "unprompted":
            continue
        trap = trap_set[record["trap_id"]]
        (index,) = [
            d
            for d in (args.cache_dir / "indexes").iterdir()
            if d.name.startswith(trap.trap_id) and d.name.endswith(INDEX_SUFFIX)
        ]
        server = Server(args.bruriah, index, args.cache_dir / "models")
        try:
            entry = audit_run(args.runs / record["transcript"], trap, server)
        finally:
            server.close()
        entries.append(
            {"trap_id": trap.trap_id, "repetition": record["repetition"], "exit_reason": record["exit_reason"], **entry}
        )
    args.out.write_text(
        json.dumps({"bruriah_commit": INDEX_SUFFIX[1:], "runs": entries}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(entries)} runs audited into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
