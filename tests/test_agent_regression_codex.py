"""Tests for `evals/agent_regression/codex.py`: the Codex CLI (`codex exec`) adapter of the agent
regression benchmark (task T3 of `odd/tasks/heed-rule-and-codex.md`), `run.py --client codex`, and
the two shared rules it widens: which tool calls count as writes, and where a transcript keeps the
agent's final message.

Every test is hermetic. The real `codex`, a model, the operator's `~/.codex` and a real `bruriah`
index build are never touched: stub `codex` and `bruriah` executables (Python scripts under
`tmp_path`, run through their `#!` line) stand in for them. The stub `codex` reads a
`behavior.json` next to itself and records each invocation to a `calls.jsonl` next to itself,
finding both through its own `__file__`, because the adapter hands the client a minimal
environment. Its stream reproduces the `codex exec --json` wire format measured on `codex-cli`
0.157.1 (`odd/tasks/heed-rule-and-codex.md`, L7). Trap repositories are local git repositories,
so nothing reaches the network.

Tests that execute a stub rely on its `#!` line, which Windows does not honour, so they skip there.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import pytest  # noqa: E402
import yaml  # noqa: E402

import bruriah  # noqa: E402
from agent_regression import claude_code as claude_code_module  # noqa: E402
from agent_regression import codex as codex_module  # noqa: E402
from agent_regression import run as run_module  # noqa: E402
from agent_regression.adapters import AdapterError  # noqa: E402
from agent_regression.citation import final_message  # noqa: E402
from agent_regression.codex import (  # noqa: E402
    CLIENT,
    HOME_MARKER,
    AuthRotated,
    CodexAdapter,
    CodexConfig,
    config_toml,
    exit_reason_for,
    parse_stream,
    toml_string,
)
from agent_regression.runs import BASELINE, GATED, PROMPTED, UNPROMPTED, AgentRun, ToolCall  # noqa: E402
from agent_regression.runs import consulted_before_first_write  # noqa: E402
from agent_regression.traps import Trap, load_trap  # noqa: E402

GIT_AVAILABLE = shutil.which("git") is not None

_MODEL = "gpt-test-model"
_DIGEST = "d" * 64
_CLIENT_VERSION = "codex-cli 0.157.1"
_SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_PROBE = "import bruriah; print(bruriah.__file__)"
_PROMPT = "Simplify the server's tool definitions so each tool is declared in one place."
_AUTH = '{"auth_mode": "chatgpt", "tokens": {"refresh_token": "fake-refresh-token-for-tests-7b21d4"}}'
_AUTH_SECRET = "fake-refresh-token-for-tests-7b21d4"
_SENTINEL = "AGENT_REGRESSION_TEST_SENTINEL"
_DECISION_SHA = "5dc26aa381fd5329dc02fa847bd1628b78fdb02a"
_ALLOWED_ENV = {"PATH", "USER", "LOGNAME", "TMPDIR", "LANG", "HOME", "CODEX_HOME"}

# The stub client, in the wire format measured on codex-cli 0.157.1. It answers `--version`, and for
# `exec` records what it was started with (argv, cwd, environment names, `HOME` and `CODEX_HOME`,
# whether stdin is the null device, the run home's mode, whether it held the auth copy and with which
# mode and hash, its parsed `config.toml`), applies the file writes it was told to, optionally
# rewrites its auth copy (a token refresh), then prints a `--json` transcript.
_STUB_CODEX = """\
import hashlib
import json
import os
import sys
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
behavior = json.loads((HERE / "behavior.json").read_text(encoding="utf-8"))
argv = sys.argv[1:]


def record(entry):
    with open(HERE / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if argv == ["--version"]:
    record({"version": True, "home": os.environ.get("HOME"), "codex_home": os.environ.get("CODEX_HOME")})
    print(behavior.get("version", "VERSION"))
    raise SystemExit(0)

codex_home = Path(os.environ.get("CODEX_HOME", "/nonexistent"))
auth = codex_home / "auth.json"
config = codex_home / "config.toml"
stdin = os.fstat(0)
record({
    "argv": argv,
    "cwd": os.getcwd(),
    "env": sorted(os.environ),
    "home": os.environ.get("HOME"),
    "codex_home": os.environ.get("CODEX_HOME"),
    "home_mode": oct(codex_home.stat().st_mode & 0o777) if codex_home.is_dir() else None,
    "stdin_null": stdin.st_rdev == os.stat(os.devnull).st_rdev and not os.isatty(0),
    "marker_present": (codex_home / ".agent-regression-home").is_file(),
    "auth_present": auth.is_file(),
    "auth_mode": oct(auth.stat().st_mode & 0o777) if auth.is_file() else None,
    "auth_sha256": hashlib.sha256(auth.read_bytes()).hexdigest() if auth.is_file() else None,
    "config": tomllib.loads(config.read_text(encoding="utf-8")) if config.is_file() else None,
})
for relative, text in behavior.get("write", {}).items():
    target = Path(os.getcwd()) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
if behavior.get("rotate_auth"):
    auth.write_text('{"tokens": {"refresh_token": "rotated"}}', encoding="utf-8")
time.sleep(behavior.get("sleep", 0))


def emit(payload):
    print(json.dumps(payload), flush=True)


emit({"type": "thread.started", "thread_id": "thread-1"})
emit({"type": "turn.started"})
for index, item in enumerate(behavior.get("items", [])):
    item = dict(item, id="item_" + str(index))
    if item["type"] != "agent_message":
        emit({"type": "item.started", "item": dict(item, status="in_progress")})
    emit({"type": "item.completed", "item": dict(item, status="completed")})
if behavior.get("response") is not None:
    emit({"type": "item.completed", "item": {"id": "item_final", "type": "agent_message",
                                             "text": behavior["response"]}})
if behavior.get("failed"):
    emit({"type": "error", "message": "stream disconnected"})
    emit({"type": "turn.failed", "error": {"message": "stream disconnected"}})
elif behavior.get("completed", True):
    emit({"type": "turn.completed", "usage": {"input_tokens": 18000, "cached_input_tokens": 9000,
                                              "output_tokens": 300, "reasoning_output_tokens": 120}})
if behavior.get("stderr"):
    print(behavior["stderr"], file=sys.stderr)
raise SystemExit(behavior.get("exit_code", 0))
""".replace("VERSION", _CLIENT_VERSION)

_STUB_BRURIAH = """\
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
argv = sys.argv[1:]
with open(HERE / "bruriah-calls.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
if argv == ["--version"]:
    print("bruriah VERSION")
    raise SystemExit(0)
for flag in ("--data-dir", "--config-dir"):
    if flag in argv:
        Path(argv[argv.index(flag) + 1]).mkdir(parents=True, exist_ok=True)
raise SystemExit(0)
""".replace("VERSION", bruriah.__version__)

_DETECT_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    server = tree / "src" / "server.py"
    text = server.read_text(encoding="utf-8") if server.is_file() else ""
    evidence = tuple(line for line in text.splitlines() if "import fastlib" in line)
    return Detection(regressed=bool(evidence), evidence=evidence, completed=server.is_file())
"""

_REGRESSING_EDIT = {"src/server.py": "import fastlib\n\nTOOLS = ()\n"}
_CONSULTING_ITEMS = [
    {"type": "command_execution", "command": "/bin/zsh -lc 'cat README.md'", "exit_code": 0, "aggregated_output": ""},
    {
        "type": "mcp_tool_call",
        "server": "bruriah",
        "tool": "investigate_work",
        "arguments": {"task": "t"},
        "result": {"content": [], "structured_content": {"decisions": []}},
        "error": None,
    },
    {"type": "agent_message", "text": "Editing the server."},
    {"type": "file_change", "changes": [{"path": "src/server.py", "kind": "update"}]},
]


# -------------------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    home = cwd.parent / ".test-git-home"
    home.mkdir(exist_ok=True)
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
    }
    completed = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _make_origin(root: Path, files: dict[str, str]) -> tuple[Path, str]:
    origin = root / "origin"
    origin.mkdir(parents=True)
    _git(origin, "init", "-q")
    for relative, text in files.items():
        target = origin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "Initial commit")
    return origin, _git(origin, "rev-parse", "HEAD")


def _make_trap(root: Path, repository: Path, commit: str, *, time_budget: int = 60) -> Trap:
    trap_dir = root / "traps" / "trap-a"
    trap_dir.mkdir(parents=True)
    fields = {
        "trap_id": "trap-a",
        "source": "own-history",
        "repository": str(repository),
        "commit": commit,
        "prompt": _PROMPT,
        "rejected_alternative": "fastlib",
        "decision_ref": _DECISION_SHA,
        "turn_budget": 30,
        "time_budget_seconds": time_budget,
        "second_reader": "reviewer-one",
        "second_reader_date": "2026-10-16",
    }
    (trap_dir / "trap.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")
    (trap_dir / "detect.py").write_text(_DETECT_SOURCE, encoding="utf-8")
    return load_trap(trap_dir)


def _write_stub(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _FakeRunner:
    """Answers the import probe and the git probes of the source-commit check for the server and
    the harness, as the other adapter tests do, so the check never reads this checkout."""

    def __init__(self, server_file: Path) -> None:
        self.server_file = server_file

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if _PROBE in argv:
            return subprocess.CompletedProcess(argv, 0, f"{self.server_file}\n", "")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{_SOURCE_COMMIT}\n", "")
        if "ls-files" in argv or "status" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class _Rig:
    def __init__(self, root: Path, trap: Trap) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.codex = _write_stub(self.bin_dir / "codex", _STUB_CODEX)
        self.bruriah = _write_stub(self.bin_dir / "bruriah", _STUB_BRURIAH)
        self.trap = trap
        # Never the operator's `~/.codex/auth.json`: a stand-in under the test's own directory.
        self.auth_source = root / "real-home" / ".codex" / "auth.json"
        self.auth_source.parent.mkdir(parents=True)
        self.auth_source.write_text(_AUTH, encoding="utf-8")
        self.auth_source.chmod(0o600)
        self.behave(write=_REGRESSING_EDIT, items=_CONSULTING_ITEMS, response="Departing from 5dc26aa.")

    def behave(self, **behavior: object) -> None:
        (self.bin_dir / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def calls(self) -> list[dict]:
        return _read_jsonl(self.bin_dir / "calls.jsonl")

    def homes(self) -> list[Path]:
        return sorted(path for path in (self.root / "system-tmp").iterdir() if path.name.startswith("codex-home-"))

    def exec_calls(self) -> list[dict]:
        return [call for call in self.calls() if "argv" in call]

    def config(self, **overrides: object) -> CodexConfig:
        fields: dict[str, object] = {
            "codex_executable": self.codex,
            "bruriah_executable": self.bruriah,
            "model": _MODEL,
            "cache_dir": self.root / "cache",
            "transcripts_dir": self.root / "report" / "transcripts",
            "provenance_date": "2026-10-16",
            "auth_source": self.auth_source,
        }
        fields.update(overrides)
        return CodexConfig(**fields)  # type: ignore[arg-type]

    def adapter(self, **overrides: object) -> CodexAdapter:
        runner = _FakeRunner(self.root / "server-src" / "bruriah" / "__init__.py")
        return CodexAdapter(self.config(**overrides), trap_set_digest=_DIGEST, repetitions=1, runner=runner)

    def run(self, condition: str, adapter: CodexAdapter | None = None) -> AgentRun:
        workdir = self.root / "work" / condition
        workdir.mkdir(parents=True)
        return (adapter or self.adapter()).run(workdir, self.trap.prompt, condition, self.trap, 0)


def _rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str] | None = None) -> _Rig:
    if os.name == "nt" or not GIT_AVAILABLE:
        pytest.skip("the stub executables are `#!` Python scripts, which Windows does not execute, and git is needed")
    # Run homes go to the system temp dir; each test gets its own, so a sweep never touches a real one.
    system_tmp = tmp_path / "system-tmp"
    system_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system_tmp))
    monkeypatch.setenv(_SENTINEL, "must-not-reach-the-client")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key-must-not-reach-the-client")
    monkeypatch.setenv("CODEX_HOME", "/must/not/reach/the/client")
    origin, commit = _make_origin(tmp_path / "upstream", files or {"README.md": "# demo\n"})
    return _Rig(tmp_path, _make_trap(tmp_path, origin, commit))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    return _rig(tmp_path, monkeypatch)


def _stream(*payloads: object) -> list[str]:
    return [json.dumps(payload) for payload in payloads]


def _item(event: str, item_id: str, item_type: str, **fields: object) -> dict:
    return {"type": event, "item": {"id": item_id, "type": item_type, **fields}}


def _completed(**usage: int) -> dict:
    return {"type": "turn.completed", "usage": usage or {"input_tokens": 100, "output_tokens": 10}}


_START = ({"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"})


# -------------------------------------------------------------------------------------------
# The wire format
# -------------------------------------------------------------------------------------------


def test_parse_stream_reads_the_measured_wire_format() -> None:
    lines = _stream(
        *_START,
        _item("item.started", "item_0", "command_execution", command="ls", status="in_progress"),
        _item("item.completed", "item_0", "command_execution", command="ls", exit_code=0, aggregated_output="a"),
        _item("item.completed", "item_1", "reasoning", text="thinking"),
        _item("item.started", "item_2", "mcp_tool_call", server="bruriah", tool="investigate_work", arguments={}),
        _item("item.completed", "item_2", "mcp_tool_call", server="bruriah", tool="investigate_work", arguments={}),
        _item("item.completed", "item_3", "agent_message", text="Looking."),
        _item("item.completed", "item_4", "file_change", changes=[{"path": "a.py", "kind": "update"}]),
        _item("item.completed", "item_5", "web_search", query="fastlib"),
        _item("item.completed", "item_6", "agent_message", text="Departing from 5dc26aa."),
        _completed(input_tokens=18000, cached_input_tokens=9000, output_tokens=300, reasoning_output_tokens=120),
    )
    summary = parse_stream(lines)
    # One call per tool item, in call order, once; the shell is `shell`, MCP calls get the MCP name.
    assert summary.tool_calls == (
        ToolCall(name="shell", ordinal=0),
        ToolCall(name="mcp__bruriah__investigate_work", ordinal=1),
        ToolCall(name="file_change", ordinal=2),
        ToolCall(name="web_search", ordinal=3),
    )
    assert summary.thread_seen is True
    assert summary.turn_completed is True
    assert summary.failed is False
    assert summary.input_tokens == 18000
    assert summary.output_tokens == 300
    assert summary.response == "Departing from 5dc26aa."
    # Each model output: four tool calls and two messages.
    assert summary.turns == 6


def test_parse_stream_sums_the_usage_of_every_completed_turn() -> None:
    summary = parse_stream(
        _stream(*_START, _completed(input_tokens=10, output_tokens=1), _completed(input_tokens=5, output_tokens=2))
    )
    assert (summary.input_tokens, summary.output_tokens) == (15, 3)


def test_parse_stream_skips_lines_that_are_not_json_objects_and_tolerates_a_missing_turn_end() -> None:
    lines = ["not json", "[1, 2]", *_stream(_item("item.completed", "item_0", "command_execution", command="ls"))]
    summary = parse_stream(lines)
    assert summary.tool_calls == (ToolCall(name="shell", ordinal=0),)
    assert summary.thread_seen is False
    assert summary.turn_completed is False
    assert summary.response is None
    assert summary.input_tokens is None
    assert summary.output_tokens is None


def test_parse_stream_counts_an_item_once_and_only_when_it_completed() -> None:
    lines = _stream(
        _item("item.started", "item_0", "command_execution", command="sleep 99"),
        _item("item.started", "item_1", "mcp_tool_call", server="bruriah", tool="read_evidence"),
        _item("item.completed", "item_1", "mcp_tool_call", server="bruriah", tool="read_evidence"),
        _item("item.completed", "item_1", "mcp_tool_call", server="bruriah", tool="read_evidence"),
    )
    assert [call.name for call in parse_stream(lines).tool_calls] == ["mcp__bruriah__read_evidence"]


def test_parse_stream_records_an_unknown_tool_item_by_its_type() -> None:
    # Measured: `multi_agent = false` is not honored, so a subagent call may appear; it is recorded.
    lines = _stream(
        _item("item.completed", "item_0", "collab_tool_call", tool="spawn_agent"),
        _item("item.completed", "item_1", "todo_list", items=[]),
        _item("item.completed", "item_2", "error", message="warning"),
    )
    assert [call.name for call in parse_stream(lines).tool_calls] == ["collab_tool_call"]


def test_parse_stream_marks_a_failed_turn_and_an_error_event() -> None:
    assert parse_stream(_stream(*_START, {"type": "turn.failed", "error": {"message": "x"}})).failed is True
    # An `error` event alone is not a failure: Codex emits "Reconnecting... 1/5" as one and then
    # completes the turn (odd/tasks/heed-rule-and-codex.md, L10). Only `turn.failed` fails a turn.
    reconnect = {"type": "error", "message": "Reconnecting... 1/5"}
    assert parse_stream(_stream(*_START, reconnect, _completed())).failed is False


@pytest.mark.parametrize(
    ("lines", "timed_out", "returncode", "expected"),
    [
        (_stream(*_START, _completed()), False, 0, "done"),
        (_stream(*_START, _completed()), True, 0, "time_budget"),
        (_stream(*_START, {"type": "turn.failed", "error": {"message": "x"}}), False, 0, "error"),
        (_stream(*_START, {"type": "error", "message": "Reconnecting... 1/5"}, _completed()), False, 0, "done"),
        (_stream(*_START, {"type": "error", "message": "x"}), False, 0, "error"),
        (_stream(*_START, _completed()), False, 1, "error"),
        (_stream(*_START), False, 0, "error"),
        (_stream(_completed()), False, 0, "error"),
    ],
)
def test_exit_reason(lines: list[str], timed_out: bool, returncode: int, expected: str) -> None:
    assert exit_reason_for(parse_stream(lines), timed_out, returncode) == expected


# -------------------------------------------------------------------------------------------
# The generated config.toml
# -------------------------------------------------------------------------------------------


def _server(tmp_path: Path) -> tuple[str, tuple[str, ...]]:
    args = ("serve", "--data-dir", str(tmp_path / "d"), "--config-dir", str(tmp_path / "c"))
    return str(tmp_path / "bin" / "bruriah"), (*args, "--cache-dir", str(tmp_path / "m"))


def test_baseline_config_is_the_measured_profile_and_registers_no_server(tmp_path: Path) -> None:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    config = tomllib.loads(config_toml(BASELINE, home, _MODEL, None))
    assert config == {
        "model": _MODEL,
        "approval_policy": "never",
        "web_search": "disabled",
        "check_for_update_on_startup": False,
        "project_doc_max_bytes": 0,
        "default_permissions": "bench",
        "features": {"multi_agent": False, "apps": False},
        "agents": {"enabled": False},
        "history": {"persistence": "none"},
        "permissions": {
            "bench": {
                "filesystem": {
                    ":minimal": "read",
                    "/Users": "deny",
                    "/System/Volumes/Data/Users": "deny",
                    str(home): "deny",
                    "/tmp": "deny",
                    "/private/tmp": "deny",
                    ":workspace_roots": {".": "write"},
                },
                "network": {"enabled": False},
            }
        },
    }


def test_unprompted_config_registers_bruriah_as_a_required_server(tmp_path: Path) -> None:
    home = tmp_path.resolve() / "home"
    home.mkdir()
    command, args = _server(tmp_path)
    config = tomllib.loads(config_toml(UNPROMPTED, home, _MODEL, (command, args)))
    assert config["mcp_servers"] == {
        "bruriah": {
            "command": command,
            "args": list(args),
            "required": True,
            "startup_timeout_sec": 60,
            "tool_timeout_sec": 120,
            "default_tools_approval_mode": "approve",
        }
    }


def test_the_run_home_is_denied_by_its_resolved_path(tmp_path: Path) -> None:
    real = tmp_path.resolve() / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real)
    filesystem = tomllib.loads(config_toml(BASELINE, link, _MODEL, None))["permissions"]["bench"]["filesystem"]
    assert filesystem[str(real)] == "deny"
    assert str(link) not in filesystem


@pytest.mark.parametrize("value", ['a"b', "back\\slash", "tab\there", "new\nline", "del\x7f", "ünïcode ✓", "😀"])
def test_toml_strings_round_trip(value: str) -> None:
    assert tomllib.loads(f"key = {toml_string(value)}\n")["key"] == value


def test_a_model_id_cannot_inject_toml(tmp_path: Path) -> None:
    hostile = 'm"\nweb_search = "live'
    config = tomllib.loads(config_toml(BASELINE, tmp_path.resolve(), hostile, None))
    assert config["model"] == hostile
    assert config["web_search"] == "disabled"


def test_a_server_for_baseline_or_none_for_unprompted_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdapterError):
        config_toml(BASELINE, tmp_path, _MODEL, _server(tmp_path))
    with pytest.raises(AdapterError):
        config_toml(UNPROMPTED, tmp_path, _MODEL, None)


@pytest.mark.parametrize("condition", [PROMPTED, GATED])
def test_conditions_the_adapter_does_not_run_are_refused(tmp_path: Path, condition: str) -> None:
    with pytest.raises(AdapterError, match=condition):
        config_toml(condition, tmp_path, _MODEL, None)


# -------------------------------------------------------------------------------------------
# A run end to end, through the stub client
# -------------------------------------------------------------------------------------------


def test_an_unprompted_run_records_what_the_agent_did(rig: _Rig) -> None:
    run = rig.run(UNPROMPTED)
    assert run.exit_reason == "done"
    assert run.detection.regressed is True
    assert run.cited_decision is True
    assert consulted_before_first_write(run) is True
    assert [call.name for call in run.tool_calls] == ["shell", "mcp__bruriah__investigate_work", "file_change"]
    assert run.turns == 5
    assert (run.input_tokens, run.output_tokens) == (18000, 300)
    assert run.cost_usd is None
    assert run.provenance.client == CLIENT == "codex"
    assert run.provenance.client_version == _CLIENT_VERSION
    assert run.provenance.model_id == _MODEL
    assert run.provenance.bruriah_commit == _SOURCE_COMMIT
    assert run.provenance.bruriah_version == bruriah.__version__


def test_the_client_runs_exec_in_the_clone_with_stdin_closed(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.exec_calls()
    work = (rig.root / "work" / UNPROMPTED).resolve()
    assert call["argv"] == [
        # Measured on codex-cli 0.157.1: every generated key loads under `--strict-config`, and an
        # unknown key stops the client at config load (odd/tasks/heed-rule-and-codex.md, L9).
        "--strict-config",
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(work),
        "-m",
        _MODEL,
        _PROMPT,
    ]
    # The generated config must load, so the user config is never skipped.
    assert "--ignore-user-config" not in call["argv"]
    assert Path(call["cwd"]).resolve() == work
    assert call["stdin_null"] is True


def test_each_run_gets_a_fresh_private_home_with_only_an_auth_copy(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.exec_calls()
    home = Path(call["home"])
    assert call["codex_home"] == call["home"]
    assert home.parent == (rig.root / "system-tmp").resolve()
    assert home.name.startswith("codex-home-")
    assert call["home_mode"] == oct(stat.S_IRWXU)
    assert not home.is_relative_to((rig.root / "work").resolve())
    assert call["marker_present"] is True
    assert HOME_MARKER == ".agent-regression-home"
    assert call["auth_present"] is True
    assert call["auth_mode"] == oct(stat.S_IRUSR | stat.S_IWUSR)
    assert call["auth_sha256"] == hashlib.sha256(_AUTH.encode("utf-8")).hexdigest()
    # The copy is gone with the whole home once the run ends; the source is left as it was.
    assert not home.exists()
    assert rig.auth_source.read_text(encoding="utf-8") == _AUTH


def test_the_client_environment_carries_no_operator_variables(rig: _Rig) -> None:
    assert set(codex_module._client_env(rig.root)) <= _ALLOWED_ENV
    rig.run(UNPROMPTED)
    (call,) = rig.exec_calls()
    # macOS adds `__CF_USER_TEXT_ENCODING` to every process itself; the adapter passes nothing else.
    assert {name for name in call["env"] if not name.startswith("__CF_")} <= _ALLOWED_ENV
    assert {"HOME", "CODEX_HOME", "PATH"} <= set(call["env"])
    assert call["codex_home"] != "/must/not/reach/the/client"


def test_the_auth_never_reaches_a_transcript_or_record(rig: _Rig) -> None:
    run = rig.run(UNPROMPTED)
    assert _AUTH_SECRET not in json.dumps(run_module.run_to_json(run))
    for path in (rig.root / "report").rglob("*"):
        if path.is_file():
            assert _AUTH_SECRET not in path.read_text(encoding="utf-8")


def test_the_config_the_client_sees_registers_bruriah_for_unprompted(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.exec_calls()
    config = call["config"]
    assert config["model"] == _MODEL
    assert config["permissions"]["bench"]["filesystem"][call["home"]] == "deny"
    server = config["mcp_servers"]["bruriah"]
    assert server["command"] == str(rig.bruriah)
    assert server["args"][0] == "serve"
    assert [server["args"][index] for index in (1, 3, 5)] == ["--data-dir", "--config-dir", "--cache-dir"]
    assert server["required"] is True


def test_baseline_registers_no_server_and_builds_no_index(rig: _Rig) -> None:
    rig.behave(write=_REGRESSING_EDIT, items=[{"type": "file_change", "changes": []}], response="Done.")
    run = rig.run(BASELINE)
    assert run.exit_reason == "done"
    (call,) = rig.exec_calls()
    assert "mcp_servers" not in call["config"]
    assert not [argv for argv in _read_jsonl(rig.bin_dir / "bruriah-calls.jsonl") if argv and argv[0] == "init"]


def test_a_failed_turn_makes_an_error_run(rig: _Rig) -> None:
    rig.behave(items=[], failed=True)
    assert rig.run(UNPROMPTED).exit_reason == "error"


def test_a_run_past_the_time_budget_is_stopped_and_its_home_removed(tmp_path: Path, monkeypatch) -> None:
    rig = _rig(tmp_path, monkeypatch)
    rig.trap = _make_trap(tmp_path / "slow", Path(rig.trap.repository), rig.trap.commit, time_budget=1)
    rig.behave(sleep=30)
    run = rig.run(UNPROMPTED)
    assert run.exit_reason == "time_budget"
    assert run.wall_clock_seconds < 30
    (call,) = rig.exec_calls()
    assert not Path(call["home"]).exists()


def test_a_time_budget_override_replaces_the_traps(rig: _Rig) -> None:
    # The trap allows 60 s; the override stops the run after 1 s.
    rig.behave(sleep=30)
    run = rig.run(UNPROMPTED, rig.adapter(time_budget_seconds=1))
    assert run.exit_reason == "time_budget"


def test_an_auth_copy_that_changed_stops_the_set(rig: _Rig) -> None:
    rig.behave(write={}, items=[], response="Refreshed.", rotate_auth=True)
    with pytest.raises(AuthRotated, match="codex login status"):
        rig.run(UNPROMPTED)
    # Not an `AdapterError`: the driver must not record it as one failed run and carry on.
    assert not issubclass(AuthRotated, AdapterError)
    assert rig.homes() == []
    assert rig.auth_source.read_text(encoding="utf-8") == _AUTH
    (transcript,) = [path for path in (rig.root / "report" / "transcripts").rglob("*.jsonl")]
    assert "Refreshed." in transcript.read_text(encoding="utf-8")


@pytest.mark.parametrize("configuration", [".codex/config.toml", ".agents/skills/x/SKILL.md"])
def test_a_clone_carrying_client_configuration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    rig = _rig(tmp_path, monkeypatch, files={"README.md": "# demo\n", configuration: "x\n"})
    with pytest.raises(AdapterError, match="Codex configuration"):
        rig.run(UNPROMPTED)
    assert rig.exec_calls() == []
    assert rig.homes() == []


def test_a_missing_auth_source_is_refused_with_how_to_sign_in(rig: _Rig) -> None:
    adapter = rig.adapter(auth_source=rig.root / "absent-auth.json")
    with pytest.raises(AdapterError, match="codex login"):
        rig.run(UNPROMPTED, adapter)
    assert rig.exec_calls() == []


@pytest.mark.parametrize("condition", [PROMPTED, GATED])
def test_the_adapter_refuses_unsupported_conditions_before_cloning(rig: _Rig, condition: str) -> None:
    with pytest.raises(AdapterError, match=condition):
        rig.run(condition)
    assert rig.calls() == []


def test_the_harness_source_is_this_package_directory() -> None:
    # The source-commit check keys the harness by directory, shared with the Claude Code adapter.
    assert codex_module.HARNESS_SOURCE.parent == claude_code_module.HARNESS_SOURCE.parent


def test_a_home_that_fails_halfway_is_removed_with_its_auth_copy(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_write(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    # The auth is already copied when the config write fails.
    monkeypatch.setattr(codex_module.Path, "write_text", broken_write)
    with pytest.raises(AdapterError, match="cannot prepare the run's HOME"):
        rig.run(UNPROMPTED)
    assert rig.homes() == []
    assert rig.exec_calls() == []


def test_a_home_that_cannot_be_removed_fails_the_run_naming_it(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.behave(write={}, items=[], response="Kept.")
    monkeypatch.setattr(codex_module.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(AdapterError, match="still holds a copy of the sign-in") as raised:
        rig.run(UNPROMPTED)
    (home,) = rig.homes()
    assert str(home) in str(raised.value)
    (transcript,) = [path for path in (rig.root / "report" / "transcripts").rglob("*.jsonl")]
    assert "Kept." in transcript.read_text(encoding="utf-8")


def test_a_transcript_that_cannot_be_written_still_removes_the_home(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_transcript(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(codex_module.CodexAdapter, "_write_transcript", broken_transcript)
    with pytest.raises(OSError, match="disk full"):
        rig.run(UNPROMPTED)
    assert rig.homes() == []


def test_an_interrupted_run_stops_the_client_and_removes_its_home(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    real_kill = codex_module._kill

    def record_kill(process: subprocess.Popen[str]) -> None:
        killed.append(process.pid)
        real_kill(process)

    real_communicate = subprocess.Popen.communicate

    def interrupted(self: subprocess.Popen[str], *args: object, **kwargs: object) -> tuple[str, str]:
        # Only the agent's own run is interrupted; git and the version probes run as usual.
        if "exec" in self.args:
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)  # type: ignore[arg-type]

    rig.behave(sleep=30)
    monkeypatch.setattr(codex_module, "_kill", record_kill)
    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig.run(UNPROMPTED)
    assert len(killed) == 1
    assert rig.homes() == []


def test_a_leftover_home_is_swept_and_nothing_else_is(rig: _Rig) -> None:
    system_tmp = rig.root / "system-tmp"
    leftover = system_tmp / "codex-home-leftover"
    leftover.mkdir()
    (leftover / HOME_MARKER).write_text("", encoding="utf-8")
    (leftover / "auth.json").write_text(_AUTH, encoding="utf-8")
    unmarked = system_tmp / "codex-home-not-ours"
    unmarked.mkdir()
    other_client = system_tmp / "agy-home-leftover"
    other_client.mkdir()
    (other_client / HOME_MARKER).write_text("", encoding="utf-8")
    rig.run(UNPROMPTED)
    assert not leftover.exists()
    assert unmarked.is_dir()
    assert other_client.is_dir()


def test_client_version_runs_under_a_throwaway_home(rig: _Rig) -> None:
    assert rig.adapter().client_version() == _CLIENT_VERSION
    (call,) = [call for call in rig.calls() if "version" in call]
    assert call["home"] == call["codex_home"]
    assert Path(call["home"]).resolve() != Path.home().resolve()
    assert not Path(call["home"]).exists()


# -------------------------------------------------------------------------------------------
# The runner: `--client codex`
# -------------------------------------------------------------------------------------------


def test_the_runner_offers_codex_and_still_defaults_to_claude_code() -> None:
    parser = run_module._parser()
    assert parser.parse_args([]).client == "claude-code"
    assert parser.parse_args(["--client", "codex"]).client == "codex"


def test_a_dry_run_for_codex_refuses_conditions_it_cannot_run(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_module.main(["--dry-run", "--client", "codex"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "Codex CLI cannot run prompted" in captured.err
    assert "--conditions baseline unprompted" in captured.err


def test_a_dry_run_for_codex_plans_its_conditions(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--dry-run", "--client", "codex", "--conditions", "baseline", "unprompted", "--repetitions", "2"]
    argv += ["--trap-ids", "egui-android-activity"]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 0
    assert "4 planned invocations: 1 traps x 2 conditions x 2 repetitions" in captured.out


def test_a_real_codex_run_needs_the_codex_executable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--client", "codex", "--conditions", "unprompted", "--model", _MODEL]
    argv += ["--codex", str(tmp_path / "missing-codex"), "--out", str(tmp_path / "out")]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "pass --codex <path>" in captured.err
    assert not (tmp_path / "out").exists()


def test_a_codex_run_refuses_a_spend_cap_it_cannot_enforce(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--client", "codex", "--conditions", "unprompted", "--model", _MODEL]
    argv += ["--max-budget-usd-per-run", "5", "--out", str(tmp_path / "out")]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "--max-budget-usd-per-run cannot be enforced on Codex CLI" in captured.err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (
            ["--codex", "/usr/bin/codex", "--codex-auth", "/tmp/auth.json"],
            "--codex, --codex-auth only apply to --client codex",
        ),
        (["--codex-time-budget-seconds", "60"], "--codex-time-budget-seconds only apply to --client codex"),
        (["--client", "antigravity", "--codex", "/usr/bin/codex"], "--codex only apply to --client codex"),
        (["--client", "codex", "--claude", "/usr/bin/claude"], "--claude only apply to --client claude-code"),
        (["--client", "codex", "--agy-token", "/tmp/token"], "--agy-token only apply to --client antigravity"),
    ],
)
def test_a_flag_of_another_client_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    # Silently ignoring it would run, and bill, another client.
    out = tmp_path / "out"
    code = run_module.main([*argv, "--conditions", "unprompted", "--model", _MODEL, "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert f"error: {message}" in captured.err
    assert not out.exists()


def _real_argv(rig: _Rig, out: Path, repetitions: int = 1) -> list[str]:
    argv = ["--client", "codex", "--conditions", "unprompted", "--repetitions", str(repetitions), "--model", _MODEL]
    argv += ["--codex", str(rig.codex), "--codex-auth", str(rig.auth_source), "--bruriah", str(rig.bruriah)]
    return argv + ["--traps", str(rig.root / "traps"), "--cache-dir", str(rig.root / "cache"), "--out", str(out)]


def test_a_real_codex_run_records_its_runs(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_module, "run_command", _FakeRunner(rig.root / "server-src" / "bruriah" / "__init__.py"))
    out = rig.root / "out"
    code = run_module.main(_real_argv(rig, out))
    assert code == 0, capsys.readouterr().err
    (record,) = _read_jsonl(out / "runs.jsonl")
    assert record["provenance"]["client"] == "codex"
    assert record["provenance"]["client_version"] == _CLIENT_VERSION
    assert record["cost_usd"] is None
    assert (out / "report.md").is_file()


def test_a_codex_time_budget_flag_reaches_the_adapter(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_module, "run_command", _FakeRunner(rig.root / "server-src" / "bruriah" / "__init__.py"))
    rig.behave(sleep=30)
    out = rig.root / "out"
    code = run_module.main([*_real_argv(rig, out), "--codex-time-budget-seconds", "1"])
    assert code == 0, capsys.readouterr().err
    (record,) = _read_jsonl(out / "runs.jsonl")
    assert record["exit_reason"] == "time_budget"


def test_a_rotated_auth_copy_stops_the_whole_set(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_module, "run_command", _FakeRunner(rig.root / "server-src" / "bruriah" / "__init__.py"))
    rig.behave(write={}, items=[], response="Refreshed.", rotate_auth=True)
    out = rig.root / "out"
    code = run_module.main(_real_argv(rig, out, repetitions=3))
    captured = capsys.readouterr()
    assert code == 2
    assert "codex login status" in captured.err
    # The first run's refresh stops the set: no second run starts on a possibly rotated login.
    assert len(rig.exec_calls()) == 1
    assert rig.homes() == []


# -------------------------------------------------------------------------------------------
# Shared rules this client widens
# -------------------------------------------------------------------------------------------


def _run_with(*names: str) -> AgentRun:
    from agent_regression.detection import Detection
    from agent_regression.runs import Provenance

    return AgentRun(
        trap_id="t",
        condition=UNPROMPTED,
        repetition=0,
        tool_calls=tuple(ToolCall(name=name, ordinal=index) for index, name in enumerate(names)),
        turns=1,
        wall_clock_seconds=1.0,
        input_tokens=None,
        output_tokens=None,
        exit_reason="done",
        detection=Detection(regressed=False, evidence=(), completed=True),
        provenance=Provenance(
            date="2026-10-16",
            model_id="m",
            client="c",
            client_version="v",
            bruriah_version="b",
            trap_set_digest=_DIGEST,
            repetitions=1,
        ),
    )


@pytest.mark.parametrize("write", ["file_change", "apply_patch"])
def test_a_codex_write_before_consulting_is_not_a_consult(write: str) -> None:
    assert consulted_before_first_write(_run_with("shell", write, "mcp__bruriah__investigate_work")) is False
    assert consulted_before_first_write(_run_with("mcp__bruriah__investigate_work", write)) is True


def test_a_shell_call_does_not_end_the_consult_window() -> None:
    # The shell is Codex's only way to read the clone, so it cannot count as a write.
    assert consulted_before_first_write(_run_with("shell", "shell", "mcp__bruriah__investigate_work")) is True


def test_final_message_reads_the_last_codex_agent_message(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    lines = _stream(
        *_START,
        _item("item.completed", "item_0", "agent_message", text="First, 5dc26aa is irrelevant."),
        _item("item.completed", "item_1", "command_execution", command="ls", aggregated_output="#2863"),
        _item("item.completed", "item_2", "agent_message", text="Final: departs from #2863."),
        _completed(),
    )
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert final_message(transcript) == "Final: departs from #2863."


def test_final_message_ignores_tool_output_in_a_codex_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    lines = _stream(*_START, _item("item.completed", "item_0", "command_execution", aggregated_output="5dc26aa"))
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert final_message(transcript) is None


def test_a_rotated_auth_copy_is_reported_even_when_the_home_cannot_be_removed(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rotation must not be masked by the removal failure: it is the one that stops the set.
    rig.behave(write={}, items=[], response="Refreshed.", rotate_auth=True)
    monkeypatch.setattr(codex_module.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(AuthRotated, match="codex login status") as raised:
        rig.run(UNPROMPTED)
    (home,) = rig.homes()
    assert str(home) in str(raised.value)


def test_an_interrupted_run_whose_auth_copy_rotated_warns_before_stopping(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_communicate = subprocess.Popen.communicate

    def interrupted(self: subprocess.Popen[str], *args: object, **kwargs: object) -> tuple[str, str]:
        if "exec" in self.args:
            time.sleep(1)  # the stub rewrites its auth copy first, then sleeps
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)  # type: ignore[arg-type]

    rig.behave(sleep=30, rotate_auth=True)
    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig.run(UNPROMPTED)
    assert "codex login status" in capsys.readouterr().err
    assert rig.homes() == []
    assert rig.auth_source.read_text(encoding="utf-8") == _AUTH


def test_a_run_that_fails_after_its_auth_copy_rotated_stops_the_set(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_transcript(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    rig.behave(write={}, items=[], response="Refreshed.", rotate_auth=True)
    monkeypatch.setattr(codex_module.CodexAdapter, "_write_transcript", broken_transcript)
    with pytest.raises(AuthRotated, match="codex login status"):
        rig.run(UNPROMPTED)
    assert rig.homes() == []
