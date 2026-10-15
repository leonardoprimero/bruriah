"""Tests for `evals/agent_regression/antigravity.py`: the Antigravity CLI (`agy`) adapter of the agent
regression benchmark (task T2 of `odd/tasks/antigravity-adapter.md`), and the two shared rules it
widens: which tool calls count as writes, and where a transcript keeps the agent's final message.

Every test is hermetic. The real `agy`, a model and a real `bruriah` index build are never touched:
stub `agy` and `bruriah` executables (Python scripts under `tmp_path`, run through their `#!` line)
stand in for them. The stub `agy` reads a `behavior.json` next to itself and records each
invocation to a `calls.jsonl` next to itself, finding both through its own `__file__`, because the
adapter hands the client a minimal environment. Its stream reproduces the wire format measured on
`agy` 1.3.5 (`odd/tasks/antigravity-adapter.md`, L6 and L8). Trap repositories are local git
repositories, so nothing reaches the network.

Tests that execute a stub rely on its `#!` line, which Windows does not honour, so they skip there.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import pytest  # noqa: E402
import yaml  # noqa: E402

import bruriah  # noqa: E402
from agent_regression import antigravity as antigravity_module  # noqa: E402
from agent_regression import claude_code as claude_code_module  # noqa: E402
from agent_regression import run as run_module  # noqa: E402
from agent_regression.adapters import AdapterError  # noqa: E402
from agent_regression.antigravity import (  # noqa: E402
    CLIENT,
    DENIED_PERMISSIONS,
    HOME_MARKER,
    TOKEN_RELATIVE,
    AntigravityAdapter,
    AntigravityConfig,
    exit_reason_for,
    parse_stream,
    settings_for,
)
from agent_regression.citation import final_message  # noqa: E402
from agent_regression.runs import BASELINE, GATED, PROMPTED, UNPROMPTED, AgentRun, ToolCall  # noqa: E402
from agent_regression.runs import consulted_before_first_write  # noqa: E402
from agent_regression.traps import Trap, load_trap  # noqa: E402

GIT_AVAILABLE = shutil.which("git") is not None

_MODEL = "gemini-test-model"
_DIGEST = "d" * 64
_CLIENT_VERSION = "1.3.5"
_SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_PROBE = "import bruriah; print(bruriah.__file__)"
_PROMPT = "Simplify the server's tool definitions so each tool is declared in one place."
_TOKEN = "fake-oauth-token-for-tests-3c9e1a"
_SENTINEL = "AGENT_REGRESSION_TEST_SENTINEL"
_DECISION_SHA = "5dc26aa381fd5329dc02fa847bd1628b78fdb02a"

# The stub client, in the wire format measured on agy 1.3.5. It answers `--version`, keeps
# `mcp add` registrations in its `HOME`'s `.gemini/config/mcp_config.json` and lists them for
# `mcp list`, and for `--print` records what it was started with (argv, cwd, environment names,
# the `HOME` it saw, whether that `HOME` held the token copy and with which mode, its settings),
# applies the file writes it was told to, then prints a stream-json transcript.
_STUB_AGY = """\
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
behavior = json.loads((HERE / "behavior.json").read_text(encoding="utf-8"))
argv = sys.argv[1:]
home = Path(os.environ.get("HOME", "/nonexistent"))
registry = home / ".gemini" / "config" / "mcp_config.json"


def record(entry):
    with open(HERE / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if argv == ["--version"]:
    record({"version": True})
    print(behavior.get("version", "VERSION"))
    raise SystemExit(0)

if argv[:2] == ["mcp", "add"]:
    record({"mcp_add": argv[2:], "home": str(home)})
    servers = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else {}
    servers[argv[2]] = argv[3:]
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(servers), encoding="utf-8")
    print('Added MCP server "' + argv[2] + '" (stdio)')
    raise SystemExit(0)

if argv[:2] == ["mcp", "list"]:
    servers = json.loads(registry.read_text(encoding="utf-8")) if registry.is_file() else {}
    for name in behavior.get("stray_servers", []):
        servers[name] = ["/usr/bin/stray"]
    record({"mcp_list": sorted(servers), "home": str(home)})
    if not servers:
        print("No MCP servers configured.")
    else:
        print("NAME  TYPE  STATUS  COMMAND/URL")
        for name, command in sorted(servers.items()):
            print(name + "  stdio  enabled  " + " ".join(command))
    raise SystemExit(0)

token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
settings_file = home / ".gemini" / "antigravity-cli" / "settings.json"
record({
    "argv": argv,
    "cwd": os.getcwd(),
    "env": sorted(os.environ),
    "home": str(home),
    "token_present": token.is_file(),
    "marker_present": (home / ".agent-regression-home").is_file(),
    "token_mode": oct(token.stat().st_mode & 0o777) if token.is_file() else None,
    "token_sha256": hashlib.sha256(token.read_bytes()).hexdigest() if token.is_file() else None,
    "settings": json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.is_file() else None,
})
for relative, text in behavior.get("write", {}).items():
    target = Path(os.getcwd()) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
time.sleep(behavior.get("sleep", 0))


def emit(payload):
    print(json.dumps(payload), flush=True)


conversation = "conv-1"
if behavior.get("init", True):
    emit({"conversation_id": conversation, "event": "init", "init": {
        "model": behavior.get("model", "MODEL"), "cwd": os.getcwd(), "permission_mode": "accept_edits",
        "tools": ["view_file", "write_to_file", "call_mcp_tool", "search_web", "run_command"]}})
index = 0
emit({"conversation_id": conversation, "event": "step_update", "step_update": {
    "conversation_id": conversation, "step_index": index, "state": "DONE", "step_type": "user_input"}})
for step in behavior.get("steps", []):
    index += 1
    emit({"conversation_id": conversation, "event": "step_update", "step_update": {
        "conversation_id": conversation, "step_index": index, "state": "DONE", "step_type": "agent_response",
        "usage": {"input_tokens": 100, "output_tokens": 5, "thinking_tokens": 2, "cache_read_tokens": 0}}})
    index += 1
    info = {"name": step["tool"], "parameters": step.get("parameters", {})}
    emit({"conversation_id": conversation, "event": "step_update", "step_update": {
        "conversation_id": conversation, "step_index": index, "state": "ACTIVE", "step_type": "tool",
        "tool_name": step["tool"], "tool_info": info}})
    final = dict(info)
    if step.get("error"):
        final["error"] = {"type": "TOOL_ERROR", "message": step["error"]}
    emit({"conversation_id": conversation, "event": "step_update", "step_update": {
        "conversation_id": conversation, "step_index": index, "state": "ERROR" if step.get("error") else "DONE",
        "step_type": "tool", "tool_name": step["tool"], "tool_info": final}})
if behavior.get("result", True):
    result = {"conversation_id": conversation, "status": behavior.get("status", "SUCCESS"),
              "response": behavior.get("response", ""), "duration_seconds": 1.5, "num_turns": 1,
              "usage": {"input_tokens": 36351, "output_tokens": 215, "thinking_tokens": 40,
                        "cache_read_tokens": 0, "total_tokens": 36606}}
    if behavior.get("denied"):
        result["denied_actions"] = [{"action": "read_file", "display_name": "ViewFile"}]
    emit({"conversation_id": conversation, "event": "result", "result": result})
if behavior.get("stderr"):
    print(behavior["stderr"], file=sys.stderr)
raise SystemExit(behavior.get("exit_code", 0))
""".replace("VERSION", _CLIENT_VERSION).replace("MODEL", _MODEL)

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
_CONSULTING_STEPS = [
    {"tool": "view_file", "parameters": {"AbsolutePath": "/x/mcp/bruriah/instructions.md"}},
    {
        "tool": "call_mcp_tool",
        "parameters": {"ServerName": "bruriah", "ToolName": "investigate_work", "Arguments": {"task": "t"}},
    },
    {"tool": "search_web", "parameters": {"query": "fastlib"}},
    {"tool": "write_to_file", "parameters": {"TargetFile": "/x/src/server.py"}},
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
        "second_reader_date": "2026-10-15",
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
    the harness, as the Claude Code adapter tests do, so the check never reads this checkout."""

    def __init__(self, server_file: Path) -> None:
        self.server_file = server_file

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if _PROBE in argv:
            return subprocess.CompletedProcess(argv, 0, f"{self.server_file}\n", "")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{_SOURCE_COMMIT}\n", "")
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command: {argv}")


class _Rig:
    def __init__(self, root: Path, trap: Trap, *, files: dict[str, str] | None = None) -> None:
        self.root = root
        self.bin_dir = root / "bin"
        self.agy = _write_stub(self.bin_dir / "agy", _STUB_AGY)
        self.bruriah = _write_stub(self.bin_dir / "bruriah", _STUB_BRURIAH)
        self.trap = trap
        self.token_source = root / "real-home" / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        self.token_source.parent.mkdir(parents=True)
        self.token_source.write_text(_TOKEN, encoding="utf-8")
        self.token_source.chmod(0o600)
        self.behave(write=_REGRESSING_EDIT, steps=_CONSULTING_STEPS, response="Departing from 5dc26aa.")

    def behave(self, **behavior: object) -> None:
        (self.bin_dir / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def calls(self) -> list[dict]:
        return _read_jsonl(self.bin_dir / "calls.jsonl")

    def homes(self) -> list[Path]:
        return sorted(path for path in (self.root / "system-tmp").iterdir() if path.name.startswith("agy-home-"))

    def print_calls(self) -> list[dict]:
        return [call for call in self.calls() if "argv" in call]

    def config(self, **overrides: object) -> AntigravityConfig:
        fields: dict[str, object] = {
            "agy_executable": self.agy,
            "bruriah_executable": self.bruriah,
            "model": _MODEL,
            "cache_dir": self.root / "cache",
            "transcripts_dir": self.root / "report" / "transcripts",
            "provenance_date": "2026-10-15",
            "token_source": self.token_source,
        }
        fields.update(overrides)
        return AntigravityConfig(**fields)  # type: ignore[arg-type]

    def adapter(self, **overrides: object) -> AntigravityAdapter:
        runner = _FakeRunner(self.root / "server-src" / "bruriah" / "__init__.py")
        return AntigravityAdapter(self.config(**overrides), trap_set_digest=_DIGEST, repetitions=1, runner=runner)

    def run(self, condition: str, adapter: AntigravityAdapter | None = None) -> AgentRun:
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
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-must-not-reach-the-client")
    origin, commit = _make_origin(tmp_path / "upstream", files or {"README.md": "# demo\n"})
    return _Rig(tmp_path, _make_trap(tmp_path, origin, commit))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    return _rig(tmp_path, monkeypatch)


def _stream(*payloads: object) -> list[str]:
    return [json.dumps(payload) for payload in payloads]


def _tool(index: int, name: str, state: str = "DONE", **parameters: object) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "state": state,
            "step_type": "tool",
            "tool_name": name,
            "tool_info": {"name": name, "parameters": parameters},
        },
    }


def _response(index: int) -> dict:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": index,
            "state": "DONE",
            "step_type": "agent_response",
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    }


def _result(**overrides: object) -> dict:
    result: dict[str, object] = {
        "status": "SUCCESS",
        "response": "Done.",
        "num_turns": 1,
        "usage": {"input_tokens": 36351, "output_tokens": 215, "thinking_tokens": 40, "cache_read_tokens": 0},
    }
    result.update(overrides)
    return {"event": "result", "result": result}


# -------------------------------------------------------------------------------------------
# The wire format
# -------------------------------------------------------------------------------------------


def test_parse_stream_reads_the_measured_wire_format() -> None:
    lines = _stream(
        {"event": "init", "init": {"model": "gemini-3.8-flash-low", "tools": ["view_file"]}},
        _response(1),
        _tool(2, "view_file", state="ACTIVE"),
        _tool(2, "view_file"),
        _response(3),
        _tool(4, "call_mcp_tool", ServerName="bruriah", ToolName="investigate_work", Arguments={"task": "t"}),
        _tool(5, "run_command", state="ERROR", CommandLine="echo hi"),
        _tool(6, "search_web", query="x"),
        _result(response="Departing from 5dc26aa."),
    )
    summary = parse_stream(lines)
    # One call per tool step, in step order, on its final state only; MCP calls get the MCP name.
    assert summary.tool_calls == (
        ToolCall(name="view_file", ordinal=0),
        ToolCall(name="mcp__bruriah__investigate_work", ordinal=1),
        ToolCall(name="run_command", ordinal=2),
        ToolCall(name="search_web", ordinal=3),
    )
    assert summary.model == "gemini-3.8-flash-low"
    assert summary.init_seen is True
    assert summary.turns == 2
    assert summary.input_tokens == 36351
    assert summary.output_tokens == 255
    assert summary.status == "SUCCESS"
    assert summary.denied_actions == 0
    assert summary.response == "Departing from 5dc26aa."


def test_parse_stream_skips_lines_that_are_not_json_objects_and_tolerates_a_missing_result() -> None:
    summary = parse_stream(["not json", "[1, 2]", *_stream(_tool(1, "view_file"))])
    assert summary.tool_calls == (ToolCall(name="view_file", ordinal=0),)
    assert summary.init_seen is False
    assert summary.status is None
    assert summary.response is None
    assert summary.input_tokens is None


def test_parse_stream_records_a_subagent_invocation_as_a_call() -> None:
    # Measured in the pilot: a subagent step has its own step type, and what it does is not streamed.
    subagent = {
        "event": "step_update",
        "step_update": {"step_index": 3, "state": "DONE", "step_type": "subagent", "tool_name": "invoke_subagent"},
    }
    summary = parse_stream(_stream(_tool(1, "view_file"), subagent, _tool(5, "write_to_file")))
    assert [call.name for call in summary.tool_calls] == ["view_file", "invoke_subagent", "write_to_file"]


def test_parse_stream_sums_step_usage_when_the_run_ends_without_a_result() -> None:
    # A run stopped by the time budget prints no result event; its steps still report usage.
    def response(index: int, input_tokens: int, output_tokens: int, thinking: int) -> dict:
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "thinking_tokens": thinking}
        step = {"step_index": index, "state": "DONE", "step_type": "agent_response", "usage": usage}
        return {"event": "step_update", "step_update": step}

    active = {
        "event": "step_update",
        "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response"},
    }
    summary = parse_stream(_stream(response(1, 100, 5, 2), active, response(3, 40, 1, 0)))
    assert summary.input_tokens == 140
    assert summary.output_tokens == 8


def test_a_time_budget_override_bounds_the_client(rig: _Rig) -> None:
    rig.run(UNPROMPTED, rig.adapter(time_budget_seconds=1800))
    (call,) = rig.print_calls()
    assert call["argv"][call["argv"].index("--print-timeout") + 1] == "1800s"


def test_the_agy_time_budget_flag_is_refused_for_claude_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    argv = ["--agy-time-budget-seconds", "1800", "--conditions", "unprompted", "--model", _MODEL, "--out", str(out)]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "error: --agy-time-budget-seconds only apply to --client antigravity" in captured.err
    assert not out.exists()


def test_parse_stream_counts_auto_denied_actions() -> None:
    summary = parse_stream(
        _stream({"event": "init", "init": {"model": "m"}}, _result(response="", denied_actions=[{"action": "x"}]))
    )
    assert summary.denied_actions == 1


@pytest.mark.parametrize(
    ("lines", "timed_out", "returncode", "expected"),
    [
        (_stream({"event": "init", "init": {"model": "m"}}, _result()), False, 0, "done"),
        (_stream({"event": "init", "init": {"model": "m"}}, _result()), True, 0, "time_budget"),
        # A turn the client cut short by auto-denying a permission is not a completed run.
        (
            _stream({"event": "init", "init": {"model": "m"}}, _result(denied_actions=[{"action": "x"}])),
            False,
            0,
            "error",
        ),
        (_stream({"event": "init", "init": {"model": "m"}}, _result(status="ERROR")), False, 0, "error"),
        (_stream({"event": "init", "init": {"model": "m"}}, _result()), False, 1, "error"),
        (_stream({"event": "init", "init": {"model": "m"}}), False, 0, "error"),
        (_stream(_result()), False, 0, "error"),
    ],
)
def test_exit_reason(lines: list[str], timed_out: bool, returncode: int, expected: str) -> None:
    assert exit_reason_for(parse_stream(lines), timed_out, returncode) == expected


# -------------------------------------------------------------------------------------------
# Permissions: no shell and no URL access in any condition; MCP only where Bruriah is registered
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("condition", [BASELINE, UNPROMPTED])
def test_every_condition_denies_the_shell_and_url_access(tmp_path: Path, condition: str) -> None:
    settings = settings_for(condition, tmp_path / "work", tmp_path / "home")
    for rule in ("command(*)", "read_url(*)", "execute_url(*)"):
        assert rule in settings["permissions"]["deny"]
    assert DENIED_PERMISSIONS == ("command(*)", "read_url(*)", "execute_url(*)")


def test_baseline_denies_every_mcp_tool(tmp_path: Path) -> None:
    settings = settings_for(BASELINE, tmp_path / "work", tmp_path / "home")
    assert "mcp(*)" in settings["permissions"]["deny"]
    assert not [rule for rule in settings["permissions"]["allow"] if rule.startswith("mcp(")]


def test_unprompted_allows_bruriah_and_the_files_the_client_writes_for_it(tmp_path: Path) -> None:
    work, home = tmp_path / "work", tmp_path / "home"
    work.mkdir()
    home.mkdir()
    settings = settings_for(UNPROMPTED, work, home)
    assert settings["permissions"]["allow"] == ["read_file(/)", f"write_file({work.resolve()})", "mcp(bruriah/*)"]
    assert "mcp(*)" not in settings["permissions"]["deny"]


@pytest.mark.parametrize("condition", [PROMPTED, GATED])
def test_conditions_the_client_cannot_express_are_refused(tmp_path: Path, condition: str) -> None:
    with pytest.raises(AdapterError, match=condition):
        settings_for(condition, tmp_path / "work", tmp_path / "home")


# -------------------------------------------------------------------------------------------
# A run end to end, through the stub client
# -------------------------------------------------------------------------------------------


def test_an_unprompted_run_records_what_the_agent_did(rig: _Rig) -> None:
    run = rig.run(UNPROMPTED)
    assert run.exit_reason == "done"
    assert run.detection.regressed is True
    assert run.cited_decision is True
    assert consulted_before_first_write(run) is True
    assert [call.name for call in run.tool_calls] == [
        "view_file",
        "mcp__bruriah__investigate_work",
        "search_web",
        "write_to_file",
    ]
    assert run.turns == 4
    assert run.cost_usd is None
    assert run.provenance.client == CLIENT == "antigravity"
    assert run.provenance.client_version == _CLIENT_VERSION
    assert run.provenance.model_id == _MODEL
    assert run.provenance.bruriah_commit == _SOURCE_COMMIT
    assert run.provenance.bruriah_version == bruriah.__version__


def test_the_client_runs_headless_in_the_clone_with_the_time_budget(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.print_calls()
    assert call["argv"] == [
        "--print",
        _PROMPT,
        "--model",
        _MODEL,
        "--output-format",
        "stream-json",
        "--mode",
        "accept-edits",
        "--disable-slash-commands",
        "--print-timeout",
        f"{rig.trap.time_budget_seconds}s",
    ]
    assert Path(call["cwd"]).resolve() == (rig.root / "work" / UNPROMPTED).resolve()


def test_each_run_gets_a_fresh_home_with_only_a_private_token_copy(rig: _Rig) -> None:
    adapter = rig.adapter()
    rig.run(UNPROMPTED, adapter)
    (call,) = rig.print_calls()
    home = Path(call["home"])
    assert home.resolve() != Path.home().resolve()
    assert not home.resolve().is_relative_to((rig.root / "work").resolve())
    assert call["token_present"] is True
    assert call["token_mode"] == oct(stat.S_IRUSR | stat.S_IWUSR)
    # The copy is gone with the whole home once the run ends; the source is left as it was.
    assert not home.exists()
    assert rig.token_source.read_text(encoding="utf-8") == _TOKEN


def test_the_client_environment_carries_no_operator_variables(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.print_calls()
    assert _SENTINEL not in call["env"]
    assert "GEMINI_API_KEY" not in call["env"]
    assert "HOME" in call["env"]


def test_the_token_never_reaches_a_transcript(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    for path in (rig.root / "report").rglob("*"):
        if path.is_file():
            assert _TOKEN not in path.read_text(encoding="utf-8")


def test_the_settings_the_client_sees_are_the_conditions(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.print_calls()
    home = Path(call["home"])
    assert call["settings"] == settings_for(UNPROMPTED, rig.root / "work" / UNPROMPTED, home)


def test_unprompted_registers_bruriah_only(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (add,) = [call for call in rig.calls() if "mcp_add" in call]
    assert add["mcp_add"][:3] == ["bruriah", str(rig.bruriah), "serve"]
    assert [call["mcp_list"] for call in rig.calls() if "mcp_list" in call] == [["bruriah"]]


def test_baseline_registers_no_server_and_builds_no_index(rig: _Rig) -> None:
    rig.behave(write=_REGRESSING_EDIT, steps=[{"tool": "write_to_file"}], response="Done.")
    run = rig.run(BASELINE)
    assert run.exit_reason == "done"
    assert not [call for call in rig.calls() if "mcp_add" in call]
    assert [call["mcp_list"] for call in rig.calls() if "mcp_list" in call] == [[]]
    assert not [argv for argv in _read_jsonl(rig.bin_dir / "bruriah-calls.jsonl") if argv and argv[0] == "init"]


def test_a_stray_mcp_server_refuses_the_run_before_the_agent_starts(rig: _Rig) -> None:
    rig.behave(stray_servers=["engram"])
    with pytest.raises(AdapterError, match="engram"):
        rig.run(UNPROMPTED)
    assert rig.print_calls() == []


def test_an_auto_denied_action_makes_an_error_run(rig: _Rig) -> None:
    rig.behave(write={}, steps=[], response="", denied=True)
    assert rig.run(UNPROMPTED).exit_reason == "error"


def test_a_run_past_the_time_budget_is_stopped_and_its_home_removed(tmp_path: Path, monkeypatch) -> None:
    rig = _rig(tmp_path, monkeypatch)
    origin = Path(rig.trap.repository)
    rig.trap = _make_trap(tmp_path / "slow", origin, rig.trap.commit, time_budget=1)
    rig.behave(sleep=30)
    run = rig.run(UNPROMPTED)
    assert run.exit_reason == "time_budget"
    (call,) = rig.print_calls()
    assert not Path(call["home"]).exists()


@pytest.mark.parametrize("configuration", [".agents/hooks.json", ".gemini/settings.json"])
def test_a_clone_carrying_client_configuration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configuration: str
) -> None:
    rig = _rig(tmp_path, monkeypatch, files={"README.md": "# demo\n", configuration: "{}\n"})
    with pytest.raises(AdapterError, match="Antigravity configuration"):
        rig.run(UNPROMPTED)
    assert rig.print_calls() == []


def test_a_missing_token_source_is_refused_with_how_to_sign_in(rig: _Rig) -> None:
    adapter = rig.adapter(token_source=rig.root / "absent-token")
    with pytest.raises(AdapterError, match="sign in"):
        rig.run(UNPROMPTED, adapter)
    assert rig.print_calls() == []


@pytest.mark.parametrize("condition", [PROMPTED, GATED])
def test_the_adapter_refuses_unsupported_conditions_before_cloning(rig: _Rig, condition: str) -> None:
    with pytest.raises(AdapterError, match=condition):
        rig.run(condition)
    assert rig.calls() == []


def test_the_harness_source_is_this_package_directory() -> None:
    # The source-commit check keys the harness by directory, shared with the Claude Code adapter.
    assert antigravity_module.HARNESS_SOURCE.parent == claude_code_module.HARNESS_SOURCE.parent


def test_a_home_that_fails_halfway_is_removed_with_its_token_copy(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_write(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    # The token is already copied when the settings write fails.
    monkeypatch.setattr(antigravity_module.Path, "write_text", broken_write)
    with pytest.raises(AdapterError, match="cannot prepare the run's HOME"):
        rig.run(UNPROMPTED)
    assert rig.homes() == []
    assert rig.print_calls() == []


def test_a_home_that_cannot_be_removed_fails_the_run_naming_it(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(antigravity_module.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(AdapterError, match="still holds a copy of the sign-in token") as raised:
        rig.run(UNPROMPTED)
    (home,) = rig.homes()
    assert str(home) in str(raised.value)


def test_a_home_that_cannot_be_removed_keeps_the_paid_transcript(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    rig.behave(write={}, steps=[], response="Kept.")
    monkeypatch.setattr(antigravity_module.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(AdapterError, match="still holds a copy of the sign-in token"):
        rig.run(UNPROMPTED)
    (transcript,) = [path for path in (rig.root / "report" / "transcripts").rglob("*") if path.is_file()]
    assert "Kept." in transcript.read_text(encoding="utf-8")


def test_a_transcript_that_cannot_be_written_still_removes_the_home(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_transcript(*args: object, **kwargs: object) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(antigravity_module.AntigravityAdapter, "_write_transcript", broken_transcript)
    with pytest.raises(OSError, match="disk full"):
        rig.run(UNPROMPTED)
    assert rig.homes() == []


def test_an_interrupted_run_stops_the_client_and_removes_its_home(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    real_kill = antigravity_module._kill

    def record_kill(process: subprocess.Popen[str]) -> None:
        killed.append(process.pid)
        real_kill(process)

    real_communicate = subprocess.Popen.communicate

    def interrupted(self: subprocess.Popen[str], *args: object, **kwargs: object) -> tuple[str, str]:
        # Only the agent's own run is interrupted; git and the version probes run as usual.
        if "--print" in self.args:
            raise KeyboardInterrupt
        return real_communicate(self, *args, **kwargs)  # type: ignore[arg-type]

    rig.behave(sleep=30)
    monkeypatch.setattr(antigravity_module, "_kill", record_kill)
    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig.run(UNPROMPTED)
    assert len(killed) == 1
    assert rig.homes() == []


def test_a_missing_bruriah_executable_is_an_adapter_error(rig: _Rig) -> None:
    adapter = rig.adapter(bruriah_executable=rig.root / "no-such-bruriah")
    with pytest.raises(AdapterError, match="no-such-bruriah"):
        rig.run(UNPROMPTED, adapter)


@pytest.mark.parametrize("condition", [BASELINE, UNPROMPTED])
def test_every_condition_reads_everywhere_but_the_operators_home_and_the_token_copy(
    tmp_path: Path, condition: str
) -> None:
    # Measured: in print mode a path no rule matches ends the run, and a deny wins over any allow.
    work, home, operator = tmp_path / "work", tmp_path / "home", tmp_path / "operator"
    settings = settings_for(condition, work, home, operator)
    assert "read_file(/)" in settings["permissions"]["allow"]
    assert f"read_file({operator.resolve()})" in settings["permissions"]["deny"]
    assert f"read_file({home.resolve() / TOKEN_RELATIVE})" in settings["permissions"]["deny"]


def test_a_clone_inside_the_operators_home_is_refused(rig: _Rig) -> None:
    # The deny on the operator's home would hide the clone from the agent.
    with pytest.raises(AdapterError, match="operator's home"):
        rig.run(UNPROMPTED, rig.adapter(operator_home=rig.root))
    assert rig.print_calls() == []
    assert rig.homes() == []


def test_run_homes_live_in_the_system_temp_dir_and_carry_the_marker(rig: _Rig) -> None:
    rig.run(UNPROMPTED)
    (call,) = rig.print_calls()
    assert Path(call["home"]).parent == (rig.root / "system-tmp").resolve()
    assert call["marker_present"] is True
    assert HOME_MARKER == ".agent-regression-home"


def test_a_leftover_home_is_swept_and_nothing_else_is(rig: _Rig) -> None:
    system_tmp = rig.root / "system-tmp"
    leftover = system_tmp / "agy-home-leftover"
    (leftover / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (leftover / ".agent-regression-home").write_text("", encoding="utf-8")
    (leftover / TOKEN_RELATIVE).write_text(_TOKEN, encoding="utf-8")
    unmarked = system_tmp / "agy-home-not-ours"
    unmarked.mkdir()
    rig.run(UNPROMPTED)
    assert not leftover.exists()
    assert unmarked.is_dir()


def test_another_live_run_home_refuses_the_run(rig: _Rig) -> None:
    # Reads are allowed outside the operator's home, so another live run's token copy would be readable.
    live = rig.root / "system-tmp" / "agy-home-live"
    live.mkdir()
    (live / ".agent-regression-home").write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(AdapterError, match="another Antigravity run"):
        rig.run(UNPROMPTED)
    assert rig.print_calls() == []
    assert live.is_dir()


def test_sigterm_is_handled_as_an_interrupt() -> None:
    previous = run_module.interrupt_on_sigterm()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)


def test_client_version_needs_no_home_of_the_callers(rig: _Rig) -> None:
    assert rig.adapter().client_version() == _CLIENT_VERSION
    (call,) = [call for call in rig.calls() if "version" in call]
    assert call == {"version": True}


# -------------------------------------------------------------------------------------------
# The runner: `--client` picks the adapter; Claude Code stays the default
# -------------------------------------------------------------------------------------------


def test_the_runner_defaults_to_claude_code() -> None:
    assert run_module._parser().parse_args([]).client == "claude-code"


def test_a_dry_run_for_antigravity_refuses_conditions_it_cannot_run(capsys: pytest.CaptureFixture[str]) -> None:
    code = run_module.main(["--dry-run", "--client", "antigravity"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "prompted" in captured.err
    assert "--conditions baseline unprompted" in captured.err


def test_a_dry_run_for_antigravity_plans_its_conditions(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--dry-run", "--client", "antigravity", "--conditions", "baseline", "unprompted", "--repetitions", "2"]
    argv += ["--trap-ids", "egui-android-activity"]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 0
    assert "4 planned invocations: 1 traps x 2 conditions x 2 repetitions" in captured.out


def test_a_real_antigravity_run_needs_the_agy_executable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["--client", "antigravity", "--conditions", "unprompted", "--model", _MODEL]
    argv += ["--agy", str(tmp_path / "missing-agy"), "--out", str(tmp_path / "out")]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "pass --agy <path>" in captured.err
    assert not (tmp_path / "out").exists()


def test_an_antigravity_run_refuses_a_spend_cap_it_cannot_enforce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--client", "antigravity", "--conditions", "unprompted", "--model", _MODEL]
    argv += ["--max-budget-usd-per-run", "5", "--out", str(tmp_path / "out")]
    code = run_module.main(argv)
    captured = capsys.readouterr()
    assert code == 2
    assert "--max-budget-usd-per-run" in captured.err
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("argv", "flags"),
    [
        (["--agy", "/usr/bin/agy", "--agy-token", "/tmp/token"], "--agy, --agy-token"),
        (["--client", "antigravity", "--claude", "/usr/bin/claude"], "--claude"),
    ],
)
def test_a_flag_of_the_other_client_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str], flags: str
) -> None:
    # Silently ignoring it would run, and bill, the other client.
    out = tmp_path / "out"
    code = run_module.main([*argv, "--conditions", "unprompted", "--model", _MODEL, "--out", str(out)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert f"error: {flags} only apply to" in captured.err
    assert not out.exists()


def test_a_real_antigravity_run_records_its_runs(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _FakeRunner(rig.root / "server-src" / "bruriah" / "__init__.py")
    monkeypatch.setattr(antigravity_module, "run_command", runner)
    out = rig.root / "out"
    argv = ["--client", "antigravity", "--conditions", "unprompted", "--repetitions", "1", "--model", _MODEL]
    argv += ["--agy", str(rig.agy), "--agy-token", str(rig.token_source), "--bruriah", str(rig.bruriah)]
    argv += ["--traps", str(rig.root / "traps"), "--cache-dir", str(rig.root / "cache"), "--out", str(out)]
    code = run_module.main(argv)
    assert code == 0, capsys.readouterr().err
    (record,) = _read_jsonl(out / "runs.jsonl")
    assert record["provenance"]["client"] == "antigravity"
    assert record["provenance"]["client_version"] == _CLIENT_VERSION
    assert (out / "report.md").is_file()


# -------------------------------------------------------------------------------------------
# Shared rules this client widens, and their previous behavior
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
            date="2026-10-15",
            model_id="m",
            client="c",
            client_version="v",
            bruriah_version="b",
            trap_set_digest=_DIGEST,
            repetitions=1,
        ),
    )


@pytest.mark.parametrize(
    "write", ["write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit"]
)
def test_an_antigravity_write_before_consulting_is_not_a_consult(write: str) -> None:
    assert consulted_before_first_write(_run_with("view_file", write, "mcp__bruriah__investigate_work")) is False
    assert consulted_before_first_write(_run_with("mcp__bruriah__investigate_work", write)) is True


def test_claude_code_writes_still_end_the_consult_window() -> None:
    assert consulted_before_first_write(_run_with("Read", "Edit", "mcp__bruriah__investigate_work")) is False
    assert consulted_before_first_write(_run_with("mcp__bruriah__investigate_work", "Write")) is True


def test_final_message_reads_the_antigravity_result(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(_stream(_tool(1, "view_file"), _result(response="Cites 5dc26aa."))) + "\n")
    assert final_message(transcript) == "Cites 5dc26aa."


def test_final_message_still_reads_a_claude_code_result(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    events = [{"type": "result", "subtype": "success", "result": "Claude says #2863."}]
    transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    assert final_message(transcript) == "Claude says #2863."


def test_a_real_run_restores_the_sigterm_handler_it_replaced(tmp_path: Path) -> None:
    before = signal.getsignal(signal.SIGTERM)
    argv = ["--client", "antigravity", "--conditions", "unprompted", "--model", _MODEL]
    argv += ["--agy", str(tmp_path / "missing-agy"), "--out", str(tmp_path / "out")]
    assert run_module.main(argv) == 2
    assert signal.getsignal(signal.SIGTERM) is before
