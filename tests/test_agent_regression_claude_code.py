"""Tests for `evals/agent_regression/claude_code.py`: the Claude Code headless adapter of the agent
regression benchmark (task T2 of `odd/tasks/agent-regression-benchmark.md`), plus the run record
fields it adds (`cost_usd`, `transcript`) and the non-dry-run mode of `run.py` it enables.

Every test is hermetic. The real `claude` binary, a model, a paid API and a real `bruriah` index
build are never touched: stub `claude` and `bruriah` executables (small Python scripts written
under `tmp_path` and passed by absolute path) stand in for them, driven by a `behavior.json` next
to each stub and recording every invocation to a `calls.jsonl` next to it. The stubs find those
files through their own `__file__`, not through an environment variable, because the adapter
hands the client a minimal environment on purpose. Trap repositories are local git repositories,
so cloning and fetching never reach the network.

Tests that execute a stub rely on its `#!` line naming this interpreter, which Windows does not
honour, so they skip there with that reason; tests that need git skip when git is absent, as
`tests/test_injection_eval.py` does.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import pytest  # noqa: E402
import yaml  # noqa: E402

import bruriah  # noqa: E402
from agent_regression import adapters as adapters_module  # noqa: E402
from agent_regression import claude_code as claude_code_module  # noqa: E402
from agent_regression import gated_hook  # noqa: E402
from agent_regression import run as run_module  # noqa: E402
from agent_regression.claude_code import (  # noqa: E402
    _client_env,
    ALLOWED_TOOLS,
    DISALLOWED_TOOLS,
    GATE_DENIAL_MARKER,
    GATE_ERROR_MARKER,
    MCP_TOOLS,
    PROMPTED_INSTRUCTION,
    AdapterError,
    ClaudeCodeAdapter,
    ClaudeCodeConfig,
    StreamSummary,
    allowed_tools,
    command_line,
    count_gate_events,
    ensure_index,
    executable_interpreter,
    exit_reason_for,
    gate_hook_command,
    install_gate,
    mirror_repository,
    parse_stream,
    prepare_workdir,
    workdir_diff,
    write_mcp_config,
)
from agent_regression.detection import Detection  # noqa: E402
from agent_regression.metrics import trap_set_digest  # noqa: E402
from agent_regression.runs import (  # noqa: E402
    BASELINE,
    CONDITIONS,
    DEFAULT_CONDITIONS,
    GATED,
    PROMPTED,
    UNPROMPTED,
    AgentRun,
    Provenance,
    RunRecordError,
    ToolCall,
    consulted_before_first_write,
    run_from_json,
    run_to_json,
)
from agent_regression.traps import Trap, load_trap, load_traps  # noqa: E402

GIT_AVAILABLE = shutil.which("git") is not None
requires_git = pytest.mark.skipif(
    not GIT_AVAILABLE, reason="git is not available on PATH; the adapter clones the trap repository with it"
)
requires_stubs = pytest.mark.skipif(
    os.name == "nt" or not GIT_AVAILABLE,
    reason="the stub executables are `#!` Python scripts, which Windows does not execute, and the adapter needs git",
)

_API_KEY = "sk-test-not-a-real-key-7f3a9c"
_SENTINEL = "AGENT_REGRESSION_TEST_SENTINEL"
_MODEL = "claude-test-model"
_DIGEST = "d" * 64
_CLIENT_VERSION = "2.1.283 (Claude Code)"
# The commit the fake command runner reports for both the harness and the server source.
_SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_PROBE = "import bruriah; print(bruriah.__file__)"
_PROMPT = "Simplify the server's tool definitions so each tool is declared in one place."
_REGRESSING_EDIT = {"src/server.py": "import fastlib\n\nTOOLS = ()\n"}

# The stub client. It answers `--version`, records its argv, cwd, environment names, a hash of the
# API key (never the key) and the `CLAUDE.md` it found, lists the MCP servers named in the
# `--mcp-config` it was given in its init line (as the real client does with `--strict-mcp-config`),
# then prints a canned stream-json transcript in the wire format measured on Claude Code 2.1.283.
# With `run_gate`, it runs the `PreToolUse` command from its `--settings` before each tool call,
# as the real client does, and hands an exit-2 stderr back as the tool result.
_STUB_CLAUDE = """\
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
behavior = json.loads((HERE / "behavior.json").read_text(encoding="utf-8"))
argv = sys.argv[1:]


def record(entry):
    with open(HERE / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\\n")


if argv == ["--version"]:
    record({"version": True})
    print(behavior.get("version", "2.1.283 (Claude Code)"))
    raise SystemExit(0)

cwd = Path.cwd()
key = os.environ.get(behavior.get("key_env", "ANTHROPIC_API_KEY"))
claude_md = cwd / "CLAUDE.md"
record(
    {
        "argv": argv,
        "cwd": str(cwd),
        "home": os.environ.get("HOME"),
        "env_names": sorted(os.environ),
        "key_sha256": hashlib.sha256(key.encode("utf-8")).hexdigest() if key else None,
        "claude_md": claude_md.read_text(encoding="utf-8") if claude_md.is_file() else None,
    }
)

servers = []
if "--mcp-config" in argv:
    config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    servers = [{"name": name, "status": "connected"} for name in config["mcpServers"]]
servers += [{"name": name, "status": "connected"} for name in behavior.get("extra_servers", [])]
if behavior.get("drop_servers"):
    servers = []
if "server_status" in behavior:
    servers = [dict(server, status=behavior["server_status"]) for server in servers]
model = argv[argv.index("--model") + 1]


def emit(payload):
    print(json.dumps(payload), flush=True)


if behavior.get("init", True):
    emit({"type": "system", "subtype": "init", "model": model, "tools": [], "mcp_servers": servers})
if behavior.get("garbage"):
    print("this line is not json", flush=True)
for text in behavior.get("assistant_text", []):
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
time.sleep(behavior.get("sleep", 0))
gate_command = None
if behavior.get("run_gate") and "--settings" in argv:
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    gate_command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
for index, name in enumerate(behavior.get("tool_calls", [])):
    content = behavior.get("tool_output", "")
    if gate_command is not None:
        stdin = behavior.get("gate_stdin", json.dumps({"tool_name": name, "tool_input": {}}))
        hook = subprocess.run(gate_command, shell=True, input=stdin, capture_output=True, text=True)
        if hook.returncode == 2 and not behavior.get("gate_silent"):
            content = hook.stderr
    emit(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": f"t{index}", "name": name, "input": {}}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
    )
    tool_result = {"type": "tool_result", "tool_use_id": f"t{index}", "content": content}
    emit({"type": "user", "message": {"content": [tool_result]}})
for relative, text in behavior.get("write", {}).items():
    target = cwd / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
if behavior.get("add_remote"):
    subprocess.run(["git", "remote", "add", "elsewhere", "https://example.invalid/x.git"], cwd=cwd, check=True)
if behavior.get("result", True):
    emit(
        {
            "type": "result",
            "subtype": behavior.get("subtype", "success"),
            "num_turns": 3,
            "duration_ms": 7524,
            "total_cost_usd": 0.48,
            "is_error": behavior.get("is_error", False),
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 56281,
                "cache_read_input_tokens": 139878,
                "output_tokens": 222,
            },
            "permission_denials": [],
            **({"result": behavior["final_text"]} if "final_text" in behavior else {}),
        }
    )
raise SystemExit(behavior.get("exit_code", 0))
"""

# The stub index builder: records its argv, creates `--data-dir`/`--config-dir` as `init` would,
# fails on demand, and reports the in-process Bruriah version for `--version`.
_STUB_BRURIAH = """\
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
behavior_file = HERE / "bruriah-behavior.json"
behavior = json.loads(behavior_file.read_text(encoding="utf-8")) if behavior_file.is_file() else {}
argv = sys.argv[1:]
with open(HERE / "bruriah-calls.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps(argv) + "\\n")
if argv == ["--version"]:
    print(behavior.get("version", "bruriah VERSION"))
    raise SystemExit(0)
if behavior.get("fail"):
    print("index build failed", file=sys.stderr)
    raise SystemExit(1)
for flag in ("--data-dir", "--config-dir"):
    if flag in argv:
        Path(argv[argv.index(flag) + 1]).mkdir(parents=True, exist_ok=True)
raise SystemExit(0)
""".replace("VERSION", bruriah.__version__)

# The trap's detector: regresses when `src/server.py` imports the rejected `fastlib`, completed
# when `src/server.py` exists.
_DETECT_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    server = tree / "src" / "server.py"
    text = server.read_text(encoding="utf-8") if server.is_file() else ""
    evidence = tuple(line for line in text.splitlines() if "import fastlib" in line)
    return Detection(regressed=bool(evidence), evidence=evidence, completed=server.is_file())
"""


# -------------------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------------------


def _git_env(home: Path) -> dict[str, str]:
    env = {
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "author@example.invalid",
    }
    for name in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _git(cwd: Path, *args: str) -> str:
    home = cwd.parent / ".test-git-home"
    home.mkdir(exist_ok=True)
    completed = subprocess.run(["git", *args], cwd=cwd, env=_git_env(home), check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_origin(root: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    """A local git repository with one commit; returns it and the commit sha."""
    origin = root / "origin"
    origin.mkdir(parents=True)
    _git(origin, "init", "-q")
    commit = _commit_files(
        origin, files or {"pyproject.toml": '[project]\nname = "demo"\n', "README.md": "# demo\n"}, "Initial commit"
    )
    return origin, commit


def _make_trap(
    root: Path, repository: Path, commit: str, *, trap_id: str = "trap-a", time_budget: int = 60, turn_budget: int = 7
) -> Trap:
    trap_dir = root / "traps" / trap_id
    trap_dir.mkdir(parents=True)
    fields = {
        "trap_id": trap_id,
        "source": "own-history",
        "repository": str(repository),
        "commit": commit,
        "prompt": _PROMPT,
        "rejected_alternative": "fastlib",
        "decision_ref": "docs/decisions/0001-tool-declarations.md",
        "turn_budget": turn_budget,
        "time_budget_seconds": time_budget,
        "second_reader": "reviewer-one",
        "second_reader_date": "2026-09-26",
    }
    (trap_dir / "trap.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")
    (trap_dir / "detect.py").write_text(_DETECT_SOURCE, encoding="utf-8")
    return load_trap(trap_dir)


def _write_stub(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _config(tmp_path: Path, claude: Path, bruriah_executable: Path, **overrides: object) -> ClaudeCodeConfig:
    fields: dict[str, object] = {
        "claude_executable": claude,
        "bruriah_executable": bruriah_executable,
        "model": _MODEL,
        "cache_dir": tmp_path / "cache",
        "transcripts_dir": tmp_path / "report" / "transcripts",
        "provenance_date": "2026-09-27",
    }
    fields.update(overrides)
    return ClaudeCodeConfig(**fields)  # type: ignore[arg-type]


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class _Tree:
    """What the fake git reports for one source directory. `commit=None` is no git worktree."""

    commit: str | None = _SOURCE_COMMIT
    status: str = ""
    tracked: bool = True


class _FakeRunner:
    """Stands in for the adapter's command runner: answers the interpreter's import probe with
    `server_file` and the git commands per source directory, and records every argv. The real
    commands would read this checkout's own, possibly dirty, git state."""

    def __init__(self, server_file: Path, server: _Tree | None = None, harness: _Tree | None = None) -> None:
        self.server_file = server_file
        self.trees = {
            server_file.parent: server or _Tree(),
            claude_code_module.HARNESS_SOURCE.parent: harness or _Tree(),
        }
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if _PROBE in argv:
            return subprocess.CompletedProcess(argv, 0, f"{self.server_file}\n", "")
        tree = self.trees[Path(argv[argv.index("-C") + 1])]
        if tree.commit is None:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository\n")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{tree.commit}\n", "")
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 0 if tree.tracked else 1, "", "")
        if "status" in argv:
            return subprocess.CompletedProcess(argv, 0, tree.status, "")
        raise AssertionError(f"unexpected command: {argv}")

    def probes(self) -> list[list[str]]:
        return [call for call in self.calls if _PROBE in call]


@dataclass
class _Rig:
    root: Path
    bin_dir: Path
    claude: Path
    bruriah: Path
    origin: Path
    commit: str
    trap: Trap
    runner: _FakeRunner

    def behave(self, **behavior: object) -> None:
        (self.bin_dir / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def behave_bruriah(self, **behavior: object) -> None:
        (self.bin_dir / "bruriah-behavior.json").write_text(json.dumps(behavior), encoding="utf-8")

    def claude_calls(self) -> list[dict]:
        return [call for call in _read_jsonl(self.bin_dir / "calls.jsonl") if "argv" in call]

    def version_calls(self) -> int:
        return len([call for call in _read_jsonl(self.bin_dir / "calls.jsonl") if "version" in call])

    def bruriah_calls(self) -> list[list[str]]:
        return _read_jsonl(self.bin_dir / "bruriah-calls.jsonl")

    def config(self, **overrides: object) -> ClaudeCodeConfig:
        return _config(self.root, self.claude, self.bruriah, **overrides)

    def adapter(self, **overrides: object) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(self.config(**overrides), trap_set_digest=_DIGEST, repetitions=2)

    def workdir(self, name: str) -> Path:
        workdir = self.root / "work" / name
        workdir.mkdir(parents=True)
        return workdir

    def run(
        self, adapter: ClaudeCodeAdapter, condition: str, repetition: int = 0, trap: Trap | None = None
    ) -> AgentRun:
        trap = trap or self.trap
        workdir = self.workdir(f"{trap.trap_id}-{condition}-{repetition}")
        return adapter.run(workdir, trap.prompt, condition, trap, repetition)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    if os.name == "nt" or not GIT_AVAILABLE:
        pytest.skip("the stub executables are `#!` Python scripts, which Windows does not execute, and git is needed")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _API_KEY)
    monkeypatch.setenv(_SENTINEL, "must-not-reach-the-client")
    bin_dir = tmp_path / "bin"
    claude = _write_stub(bin_dir / "claude", _STUB_CLAUDE)
    bruriah_executable = _write_stub(bin_dir / "bruriah", _STUB_BRURIAH)
    origin, commit = _make_origin(tmp_path / "upstream")
    trap = _make_trap(tmp_path, origin, commit)
    runner = _FakeRunner(tmp_path / "server-src" / "bruriah" / "__init__.py")
    monkeypatch.setattr(claude_code_module, "run_command", runner)
    rig = _Rig(tmp_path, bin_dir, claude, bruriah_executable, origin, commit, trap, runner)
    rig.behave(write=_REGRESSING_EDIT, tool_calls=["Read", "Edit"])
    return rig


def _stream(*payloads: object) -> list[str]:
    return [json.dumps(payload) for payload in payloads]


def _init(model: str = _MODEL, servers: tuple[tuple[str, str], ...] = ()) -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "model": model,
        "tools": ["Read"],
        "mcp_servers": [{"name": name, "status": status} for name, status in servers],
    }


def _assistant(*items: dict) -> dict:
    return {"type": "assistant", "message": {"content": list(items), "usage": {"input_tokens": 1, "output_tokens": 1}}}


def _tool_use(name: str) -> dict:
    return {"type": "tool_use", "id": "t", "name": name, "input": {}}


def _result(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "num_turns": 3,
        "duration_ms": 7524,
        "total_cost_usd": 0.48,
        "is_error": False,
        "usage": {
            "input_tokens": 6,
            "cache_creation_input_tokens": 56281,
            "cache_read_input_tokens": 139878,
            "output_tokens": 222,
        },
        "permission_denials": [],
    }
    payload.update(overrides)
    return payload


# -------------------------------------------------------------------------------------------
# Tool allowlist: the agent runs no shell command, so it cannot push, install packages, reach
# the network, or read the API key from its environment
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONDITIONS)
def test_no_allowed_tool_runs_a_shell_command(condition: str) -> None:
    # A prefix rule is not read-only: `Bash(find:*)` admits `find -exec`, `Bash(rg:*)` admits
    # `rg --pre`, and `Bash(git diff:*)` honours `diff.external` from a `.git/config` the agent
    # can write. Every Bash child would inherit `ANTHROPIC_API_KEY`, so there is no shell at all.
    for entry in allowed_tools(condition):
        assert not entry.startswith("Bash"), (condition, entry)
    assert "Bash" in DISALLOWED_TOOLS


def test_the_base_allowlist_is_the_file_tools_and_the_built_in_search() -> None:
    assert ALLOWED_TOOLS == ("Read", "Edit", "Write", "MultiEdit", "Glob", "Grep")


def test_baseline_allows_no_bruriah_tool() -> None:
    assert allowed_tools(BASELINE) == ALLOWED_TOOLS
    assert not any("bruriah" in entry for entry in allowed_tools(BASELINE))


@pytest.mark.parametrize("condition", [UNPROMPTED, PROMPTED])
def test_mcp_conditions_allow_exactly_the_two_read_only_bruriah_tools(condition: str) -> None:
    assert MCP_TOOLS == ("mcp__bruriah__investigate_work", "mcp__bruriah__read_evidence")
    assert allowed_tools(condition) == ALLOWED_TOOLS + MCP_TOOLS


def test_allowed_tools_rejects_an_unknown_condition() -> None:
    with pytest.raises(ValueError, match="hooked"):
        allowed_tools("hooked")


def test_shell_web_task_and_notebook_tools_are_disallowed() -> None:
    assert DISALLOWED_TOOLS == ("Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit")
    assert not set(DISALLOWED_TOOLS) & set(ALLOWED_TOOLS + MCP_TOOLS)


# -------------------------------------------------------------------------------------------
# Command line
# -------------------------------------------------------------------------------------------


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """The values following a variadic flag, up to the next flag."""
    start = argv.index(flag) + 1
    values = []
    for item in argv[start:]:
        if item.startswith("--"):
            break
        values.append(item)
    return values


def _pure_trap(tmp_path: Path, **overrides: object) -> Trap:
    fields: dict[str, object] = {
        "path": tmp_path,
        "trap_id": "trap-a",
        "source": "own-history",
        "repository": "https://example.invalid/project.git",
        "commit": "0" * 40,
        "prompt": _PROMPT,
        "rejected_alternative": "fastlib",
        "decision_ref": "docs/decisions/0001.md",
        "turn_budget": 7,
        "time_budget_seconds": 60,
        "second_reader": "reviewer-one",
        "second_reader_date": "2026-09-26",
    }
    fields.update(overrides)
    return Trap(**fields)  # type: ignore[arg-type]


def test_command_line_isolates_the_client_and_bounds_the_run(tmp_path: Path) -> None:
    config = _config(tmp_path, Path("/opt/claude/bin/claude"), Path("/opt/bruriah/bin/bruriah"))
    trap = _pure_trap(tmp_path)

    argv = command_line(config, trap, BASELINE, trap.prompt, None)

    assert argv[:3] == ["/opt/claude/bin/claude", "-p", _PROMPT]
    for flag in ("--verbose", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv, flag
    # The client runs logged in, so `--bare` (which never reads the login) is out. No setting
    # source at all: `user` would load the operator's hooks and plugins, and `project` or `local`
    # would load a `.claude/settings.json` committed in the trap repository, which is
    # repository-controlled content that can define hooks, environment and extra write directories.
    assert "--bare" not in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert argv[argv.index("--model") + 1] == _MODEL
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert _flag_values(argv, "--allowedTools") == list(ALLOWED_TOOLS)
    assert _flag_values(argv, "--disallowedTools") == list(DISALLOWED_TOOLS)


def test_baseline_command_line_registers_no_mcp_server(tmp_path: Path) -> None:
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)

    argv = command_line(config, trap, BASELINE, trap.prompt, None)

    assert "--mcp-config" not in argv
    # Strict even with nothing registered: the operator's own MCP servers must not load either.
    assert "--strict-mcp-config" in argv
    assert not any(tool in argv for tool in MCP_TOOLS)


@pytest.mark.parametrize("condition", [UNPROMPTED, PROMPTED])
def test_mcp_command_line_registers_only_the_given_config_strictly(tmp_path: Path, condition: str) -> None:
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)
    mcp_config = tmp_path / "mcp.json"

    argv = command_line(config, trap, condition, trap.prompt, mcp_config)

    assert _flag_values(argv, "--mcp-config") == [str(mcp_config)]
    assert "--strict-mcp-config" in argv
    assert _flag_values(argv, "--allowedTools") == list(ALLOWED_TOOLS + MCP_TOOLS)


def test_command_line_caps_spend_only_when_a_budget_is_configured(tmp_path: Path) -> None:
    trap = _pure_trap(tmp_path)
    unbudgeted = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    budgeted = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"), max_budget_usd_per_run=1.5)

    assert "--max-budget-usd" not in command_line(unbudgeted, trap, BASELINE, trap.prompt, None)
    argv = command_line(budgeted, trap, BASELINE, trap.prompt, None)
    assert argv[argv.index("--max-budget-usd") + 1] == "1.5"


# -------------------------------------------------------------------------------------------
# Stream parsing and exit reasons
# -------------------------------------------------------------------------------------------


def test_parse_stream_numbers_tool_calls_across_assistant_messages_in_encounter_order() -> None:
    lines = _stream(
        _init(servers=(("bruriah", "connected"),)),
        _assistant({"type": "text", "text": "Looking."}, _tool_use("mcp__bruriah__investigate_work")),
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t"}]}},
        _assistant(_tool_use("Read"), _tool_use("Grep")),
        _assistant(_tool_use("Edit")),
        _result(),
    )

    summary = parse_stream(lines)

    assert summary.tool_calls == (
        ToolCall("mcp__bruriah__investigate_work", 0),
        ToolCall("Read", 1),
        ToolCall("Grep", 2),
        ToolCall("Edit", 3),
    )


def test_parse_stream_reads_the_result_and_init_lines() -> None:
    summary = parse_stream(_stream(_init(servers=(("bruriah", "connected"),)), _result()))

    assert summary == StreamSummary(
        tool_calls=(),
        result_subtype="success",
        num_turns=3,
        duration_ms=7524,
        input_tokens=6 + 56281 + 139878,
        output_tokens=222,
        cost_usd=0.48,
        is_error=False,
        model=_MODEL,
        mcp_servers=(("bruriah", "connected"),),
        init_seen=True,
    )


def test_parse_stream_counts_only_the_input_token_fields_present() -> None:
    summary = parse_stream(_stream(_init(), _result(usage={"input_tokens": 9, "output_tokens": 4})))

    assert summary.input_tokens == 9
    assert summary.output_tokens == 4


def test_parse_stream_skips_lines_that_are_not_json() -> None:
    lines = ["", "not json at all", *_stream(_init(), _assistant(_tool_use("Read"))), "{truncated", "[1, 2]"]

    summary = parse_stream(lines)

    assert summary.tool_calls == (ToolCall("Read", 0),)


def test_parse_stream_without_a_result_line_leaves_the_result_fields_unset() -> None:
    summary = parse_stream(_stream(_init(), _assistant(_tool_use("Read"))))

    assert summary.result_subtype is None
    assert summary.num_turns is None
    assert summary.duration_ms is None
    assert summary.input_tokens is None
    assert summary.output_tokens is None
    assert summary.cost_usd is None
    assert summary.is_error is False
    assert summary.model == _MODEL


def _user(content: object) -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": content}]}}


def test_count_gate_events_counts_user_events_carrying_the_gate_reason() -> None:
    lines = _stream(
        _init(),
        _assistant(_tool_use("Edit")),
        _user(f"PreToolUse:Edit hook error: {gated_hook.GATE_REASON}"),
        _assistant(_tool_use("Edit")),
        _user("ok"),
        _result(),
    )

    assert count_gate_events(lines) == (1, 0)


def test_count_gate_events_ignores_the_reason_quoted_in_assistant_text() -> None:
    lines = _stream(
        _assistant({"type": "text", "text": gated_hook.GATE_REASON}),
        _assistant({"type": "text", "text": gated_hook.GATE_ERROR_REASON}),
        _result(result=gated_hook.GATE_REASON),
    )

    assert count_gate_events(lines) == (0, 0)


def test_count_gate_events_counts_gate_errors_separately_from_denials() -> None:
    lines = _stream(
        _user(f"{gated_hook.GATE_ERROR_REASON} (OSError: boom)"),
        _user([{"type": "text", "text": f"{gated_hook.GATE_ERROR_REASON} (ValueError: x)"}]),
        _user(gated_hook.GATE_REASON),
    )

    assert count_gate_events(lines) == (1, 2)


def test_count_gate_events_matches_the_first_sentence_only_and_skips_non_json_lines() -> None:
    first_sentence = gated_hook.GATE_REASON.split(". ")[0] + "."
    lines = ["not json", "[1, 2]", "", *_stream(_user(f"Hook said: {first_sentence} (truncated)"))]

    assert count_gate_events(lines) == (1, 0)


def test_the_gate_markers_are_distinct_json_safe_first_sentences_of_the_pinned_reasons() -> None:
    for marker, reason in (
        (GATE_DENIAL_MARKER, gated_hook.GATE_REASON),
        (GATE_ERROR_MARKER, gated_hook.GATE_ERROR_REASON),
    ):
        assert reason.startswith(marker)
        assert marker.endswith(".") and ". " not in marker
        assert json.dumps(marker) == f'"{marker}"'
    assert GATE_DENIAL_MARKER not in GATE_ERROR_MARKER
    assert GATE_ERROR_MARKER not in GATE_DENIAL_MARKER


def test_count_gate_events_of_nothing_is_zero() -> None:
    assert count_gate_events([]) == (0, 0)


def test_parse_stream_of_nothing_saw_no_init_line() -> None:
    summary = parse_stream([])

    assert summary.init_seen is False
    assert summary.model is None
    assert summary.mcp_servers == ()


def _summary(**overrides: object) -> StreamSummary:
    fields: dict[str, object] = {
        "tool_calls": (),
        "result_subtype": "success",
        "num_turns": 3,
        "duration_ms": 10,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": 0.1,
        "is_error": False,
        "model": _MODEL,
        "mcp_servers": (),
        "init_seen": True,
    }
    fields.update(overrides)
    return StreamSummary(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("summary", "timed_out", "returncode", "expected"),
    [
        (_summary(), False, 0, "done"),
        (_summary(), True, 0, "time_budget"),
        (_summary(result_subtype=None), True, -9, "time_budget"),
        (_summary(result_subtype="error_max_turns", is_error=True), False, 1, "turn_budget"),
        (_summary(result_subtype="error_max_turns"), False, 0, "turn_budget"),
        (_summary(), False, 1, "error"),
        (_summary(result_subtype=None), False, 0, "error"),
        (_summary(result_subtype="error_during_execution"), False, 0, "error"),
        (_summary(is_error=True), False, 0, "error"),
    ],
)
def test_exit_reason_for(summary: StreamSummary, timed_out: bool, returncode: int, expected: str) -> None:
    assert exit_reason_for(summary, timed_out, returncode) == expected


# -------------------------------------------------------------------------------------------
# Prompted instruction and MCP config
# -------------------------------------------------------------------------------------------


def test_the_prompted_instruction_names_both_tools_and_not_the_product() -> None:
    assert "`investigate_work`" in PROMPTED_INSTRUCTION
    assert "`read_evidence`" in PROMPTED_INSTRUCTION
    assert "before editing any file" in PROMPTED_INSTRUCTION.lower()
    assert "bruriah" not in PROMPTED_INSTRUCTION.lower()
    assert "\n" not in PROMPTED_INSTRUCTION


def test_prompted_command_line_appends_the_instruction_to_the_system_prompt(tmp_path: Path) -> None:
    # The instruction travels as a system-prompt flag, never as a `CLAUDE.md` file: the client
    # does not read a repository `CLAUDE.md` in every mode, and the benchmark must not depend on it.
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)

    argv = command_line(config, trap, PROMPTED, trap.prompt, tmp_path / "mcp.json")

    assert _flag_values(argv, "--append-system-prompt") == [PROMPTED_INSTRUCTION]


@pytest.mark.parametrize("condition", [BASELINE, UNPROMPTED])
def test_only_the_prompted_command_line_carries_an_instruction(tmp_path: Path, condition: str) -> None:
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)
    mcp_config = None if condition == BASELINE else tmp_path / "mcp.json"

    argv = command_line(config, trap, condition, trap.prompt, mcp_config)

    assert "--append-system-prompt" not in argv
    assert PROMPTED_INSTRUCTION not in argv


_BASE_ARGV = [
    "/opt/claude",
    "-p",
    _PROMPT,
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--max-turns",
    "7",
    "--model",
    _MODEL,
    "--permission-mode",
    "acceptEdits",
    "--permission-prompts",
    "none",
    "--allowedTools",
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Glob",
    "Grep",
]
_DISALLOWED_ARGV = ["--disallowedTools", "Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit"]
_MCP_ARGV = ["mcp__bruriah__investigate_work", "mcp__bruriah__read_evidence"]


@pytest.mark.parametrize(
    ("condition", "tail"),
    [
        (BASELINE, []),
        (UNPROMPTED, ["--mcp-config", "MCP"]),
        (PROMPTED, ["--mcp-config", "MCP", "--append-system-prompt", PROMPTED_INSTRUCTION]),
    ],
)
def test_the_default_conditions_keep_their_published_command_line(
    tmp_path: Path, condition: str, tail: list[str]
) -> None:
    # Pinned argv: the published runs were measured with exactly these command lines, and the
    # opt-in gated condition must not change a single element of them.
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)
    mcp_config = None if condition == BASELINE else tmp_path / "mcp.json"
    tools = [] if condition == BASELINE else _MCP_ARGV

    argv = command_line(config, trap, condition, trap.prompt, mcp_config)

    expected_tail = [str(mcp_config) if item == "MCP" else item for item in tail]
    assert argv == _BASE_ARGV + tools + _DISALLOWED_ARGV + expected_tail
    assert "--settings" not in argv


# -------------------------------------------------------------------------------------------
# Gated condition: a run-local `PreToolUse` hook denies the first native file mutation
# -------------------------------------------------------------------------------------------


def test_gated_allows_the_same_tools_as_the_mcp_conditions() -> None:
    assert allowed_tools(GATED) == ALLOWED_TOOLS + MCP_TOOLS


def test_gated_command_line_registers_the_server_the_gate_settings_and_the_prompted_instruction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)
    mcp_config, settings = tmp_path / "mcp.json", tmp_path / "settings.json"

    argv = command_line(config, trap, GATED, trap.prompt, mcp_config, settings=settings)

    assert argv == (
        _BASE_ARGV
        + _MCP_ARGV
        + _DISALLOWED_ARGV
        + ["--mcp-config", str(mcp_config), "--settings", str(settings), "--append-system-prompt", PROMPTED_INSTRUCTION]
    )
    # The gate travels as flag settings; no settings file of the operator or the trap repository loads.
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_command_line_takes_gate_settings_for_the_gated_condition_only(tmp_path: Path) -> None:
    config = _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah"))
    trap = _pure_trap(tmp_path)
    mcp_config, settings = tmp_path / "mcp.json", tmp_path / "settings.json"

    with pytest.raises(ValueError, match="settings"):
        command_line(config, trap, GATED, trap.prompt, mcp_config)
    for condition in DEFAULT_CONDITIONS:
        with pytest.raises(ValueError, match="settings"):
            command_line(config, trap, condition, trap.prompt, mcp_config, settings=settings)


def test_install_gate_writes_settings_hook_and_state_outside_the_clone(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    gates = tmp_path / "cache" / "gates"

    settings_path = install_gate(gates, workdir)

    gate_dir = settings_path.parent
    hook, state = gate_dir / "gated_hook.py", gate_dir / gated_hook.STATE_FILE
    assert settings_path.name == "settings.json"
    assert gate_dir.parent == gates
    # The agent edits inside the clone; nothing of the gate is there for it to replace or pre-empt.
    assert not gate_dir.resolve().is_relative_to(workdir.resolve())
    assert list(workdir.iterdir()) == []
    assert hook.read_bytes() == Path(gated_hook.__file__).read_bytes()
    assert not state.exists()
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write|MultiEdit",
                    "hooks": [{"type": "command", "command": gate_hook_command(hook, state)}],
                }
            ]
        }
    }
    # Every word is quoted and nothing but the interpreter, the hook copy and its state is named: no
    # repository or task text ever reaches the shell the client runs the hook command in.
    assert gate_hook_command(hook, state) == (
        f"{shlex.quote(sys.executable)} -I {shlex.quote(str(hook))} {shlex.quote(str(state))}"
    )
    if os.name == "posix":
        assert gate_dir.stat().st_mode & 0o777 == 0o700
        for path in (settings_path, hook):
            assert path.stat().st_mode & 0o022 == 0, path


def test_install_gate_gives_every_run_its_own_directory(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    gates = tmp_path / "cache" / "gates"

    first, second = install_gate(gates, workdir), install_gate(gates, workdir)

    assert first.parent != second.parent
    assert {first.parent.parent, second.parent.parent} == {gates}


@pytest.mark.parametrize("inside", [".agent-regression", ".", "nested/gates"])
def test_install_gate_refuses_a_gate_root_inside_the_clone(tmp_path: Path, inside: str) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()

    with pytest.raises(AdapterError, match="outside"):
        install_gate(workdir / inside, workdir)

    assert not (workdir / ".agent-regression").exists()
    assert not any(workdir.rglob("settings.json"))
    assert not any(workdir.rglob("gated_hook.py"))


def test_the_gate_reason_tells_the_agent_to_review_the_decision_and_retry() -> None:
    reason = gated_hook.GATE_REASON
    assert "`investigate_work`" in reason
    assert "`read_evidence`" in reason
    assert "historical decision" in reason
    assert "retry" in reason.lower()
    assert "bruriah" not in reason.lower()
    assert "\n" not in reason
    assert gated_hook.GATED_TOOLS == ("Edit", "Write", "MultiEdit")


def test_the_gate_denies_the_first_native_file_mutation_and_allows_every_later_one(tmp_path: Path) -> None:
    state = tmp_path / gated_hook.STATE_FILE

    assert gated_hook.gate(state, {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}}) == (
        2,
        gated_hook.GATE_REASON,
    )
    assert gated_hook.gate(state, {"tool_name": "Edit", "tool_input": {"file_path": "src/a.py"}}) == (0, "")
    assert gated_hook.gate(state, {"tool_name": "Write"}) == (0, "")
    assert gated_hook.gate(state, {"tool_name": "MultiEdit"}) == (0, "")
    assert json.loads(state.read_text(encoding="utf-8")) == {"denied": "Edit"}


@pytest.mark.parametrize("payload", [{"tool_name": "Read"}, {"tool_name": "NotebookEdit"}, {}, [], "Edit"])
def test_the_gate_lets_anything_else_through_without_spending_the_denial(tmp_path: Path, payload: object) -> None:
    state = tmp_path / gated_hook.STATE_FILE

    assert gated_hook.gate(state, payload) == (0, "")
    assert not state.exists()
    assert gated_hook.gate(state, {"tool_name": "Write"})[0] == 2


def test_the_gate_error_reason_says_the_gate_is_broken_and_is_not_the_gate_reason() -> None:
    reason = gated_hook.GATE_ERROR_REASON
    assert reason != gated_hook.GATE_REASON
    assert "gate is broken" in reason
    assert "blocked" in reason
    assert "bruriah" not in reason.lower()
    assert "\n" not in reason


@pytest.mark.parametrize("parent", ["missing", "a-file"])
def test_the_gate_denies_every_gated_call_when_its_state_cannot_be_created(tmp_path: Path, parent: str) -> None:
    (tmp_path / "a-file").write_text("", encoding="utf-8")
    state = tmp_path / parent / gated_hook.STATE_FILE

    for tool in ("Edit", "Edit", "Write", "MultiEdit"):
        code, message = gated_hook.gate(state, {"tool_name": tool})
        assert code == 2
        assert message.startswith(gated_hook.GATE_ERROR_REASON)
        assert "Error" in message
    assert gated_hook.gate(state, {"tool_name": "Read"}) == (0, "")


def test_a_failed_state_write_denies_and_leaves_the_first_denial_unspent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / gated_hook.STATE_FILE

    class FailingHandle:
        def __init__(self, descriptor: int, *args: object, **kwargs: object) -> None:
            os.close(descriptor)

        def __enter__(self) -> FailingHandle:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def write(self, text: str) -> int:
            raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(os, "fdopen", FailingHandle)
        for _ in range(2):
            code, message = gated_hook.gate(state, {"tool_name": "Edit"})
            assert code == 2
            assert message.startswith(gated_hook.GATE_ERROR_REASON)
            assert "No space left on device" in message
            # No partial state is left behind for a later call to read as "already denied".
            assert not state.exists()

    # Once the gate can record state again, the real first denial is still there to give.
    assert gated_hook.gate(state, {"tool_name": "Write"}) == (2, gated_hook.GATE_REASON)
    assert gated_hook.gate(state, {"tool_name": "Write"}) == (0, "")
    assert json.loads(state.read_text(encoding="utf-8")) == {"denied": "Write"}
    assert sorted(path.name for path in tmp_path.iterdir()) == [gated_hook.STATE_FILE]


@pytest.mark.parametrize("stdin", ["", "not json", '{"tool_name": "Edit"'])
def test_the_hook_denies_an_unparseable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stdin: str
) -> None:
    state = tmp_path / gated_hook.STATE_FILE
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))

    assert gated_hook.main([str(state)]) == 2
    assert capsys.readouterr().err.startswith(gated_hook.GATE_ERROR_REASON)
    assert not state.exists()


def test_the_hook_allows_a_valid_payload_for_a_tool_it_does_not_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / gated_hook.STATE_FILE
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Read"})))

    assert gated_hook.main([str(state)]) == 0
    assert capsys.readouterr().err == ""
    assert not state.exists()


@pytest.mark.skipif(os.name == "nt", reason="the client runs hook commands through a POSIX shell here")
def test_the_rendered_hook_command_gates_once_per_run_through_the_shell(tmp_path: Path) -> None:
    # A gate root full of shell metacharacters: the quoted command must still run the hook as-is.
    workdir = tmp_path / "work"
    workdir.mkdir()
    gates = tmp_path / "it's a $(touch pwned) `dir`; ok" / "gates"

    def hook(settings_path: Path, tool: str) -> subprocess.CompletedProcess[str]:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"file_path": "README.md"}}
        return subprocess.run(
            command,
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
        )

    settings, other_settings = install_gate(gates, workdir), install_gate(gates, workdir)
    first = hook(settings, "Edit")
    retry = hook(settings, "Edit")

    assert (first.returncode, first.stderr.strip()) == (2, gated_hook.GATE_REASON)
    assert (retry.returncode, retry.stderr) == (0, "")
    assert (settings.parent / gated_hook.STATE_FILE).is_file()
    assert not (tmp_path / "pwned").exists()
    assert not any(workdir.iterdir())
    # Each run's gate is its own: another run still gets its first denial.
    assert hook(other_settings, "Write").returncode == 2


def test_write_mcp_config_renders_the_claude_code_manifest_for_the_real_server(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "bruriah"
    data_dir, config_dir, model_cache = tmp_path / "data", tmp_path / "config", tmp_path / "models"
    target = tmp_path / "work" / ".agent-regression" / "mcp.json"

    written = write_mcp_config(target, executable, data_dir, config_dir, model_cache_dir=model_cache)

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "mcpServers": {
            "bruriah": {
                "command": str(executable),
                "args": [
                    "serve",
                    "--data-dir",
                    str(data_dir),
                    "--config-dir",
                    str(config_dir),
                    "--cache-dir",
                    str(model_cache),
                ],
                "env": {},
            }
        }
    }


def test_write_mcp_config_without_a_model_cache_passes_only_the_two_directories(tmp_path: Path) -> None:
    target = tmp_path / "mcp.json"

    write_mcp_config(target, tmp_path / "bruriah", tmp_path / "data", tmp_path / "config")

    args = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["bruriah"]["args"]
    assert args == ["serve", "--data-dir", str(tmp_path / "data"), "--config-dir", str(tmp_path / "config")]


def test_write_mcp_config_rejects_a_relative_command(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="absolute"):
        write_mcp_config(tmp_path / "mcp.json", Path("bruriah"), tmp_path / "data", tmp_path / "config")
    assert not (tmp_path / "mcp.json").exists()


# -------------------------------------------------------------------------------------------
# Construction needs no API key: the client runs with the operator's login
# -------------------------------------------------------------------------------------------


def test_the_adapter_constructs_without_any_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    ClaudeCodeAdapter(
        _config(tmp_path, Path("/opt/claude"), Path("/opt/bruriah")), trap_set_digest=_DIGEST, repetitions=1
    )


# -------------------------------------------------------------------------------------------
# Mirror, working clone, diff (git only)
# -------------------------------------------------------------------------------------------


@requires_git
def test_mirror_repository_clones_once_and_reuses_the_mirror_offline(tmp_path: Path) -> None:
    origin, commit = _make_origin(tmp_path / "upstream")
    trap = _make_trap(tmp_path, origin, commit)
    cache = tmp_path / "cache"

    mirror = mirror_repository(trap, cache)

    digest = hashlib.sha256(str(origin).encode("utf-8")).hexdigest()[:16]
    assert mirror == cache / "mirrors" / f"{digest}.git"
    assert _git(mirror, "cat-file", "-t", commit) == "commit"

    # The source is gone: a second call can succeed only by not cloning or fetching again.
    origin.rename(tmp_path / "upstream" / "moved-away")
    assert mirror_repository(trap, cache) == mirror


@requires_git
def test_mirror_repository_fetches_when_the_pinned_commit_is_missing(tmp_path: Path) -> None:
    origin, first = _make_origin(tmp_path / "upstream")
    cache = tmp_path / "cache"
    mirror = mirror_repository(_make_trap(tmp_path, origin, first), cache)
    second = _commit_files(origin, {"NEW.md": "new\n"}, "Second commit")

    assert mirror_repository(_make_trap(tmp_path, origin, second, trap_id="trap-b"), cache) == mirror
    assert _git(mirror, "cat-file", "-t", second) == "commit"


@requires_git
def test_mirror_repository_fails_when_the_pinned_commit_does_not_exist(tmp_path: Path) -> None:
    origin, _ = _make_origin(tmp_path / "upstream")
    trap = _make_trap(tmp_path, origin, "f" * 40)

    with pytest.raises(AdapterError, match="f" * 40):
        mirror_repository(trap, tmp_path / "cache")


@requires_git
def test_prepare_workdir_checks_out_the_pinned_commit_detached_with_no_remote(tmp_path: Path) -> None:
    origin, first = _make_origin(tmp_path / "upstream")
    _commit_files(origin, {"LATER.md": "later\n"}, "Later commit")
    trap = _make_trap(tmp_path, origin, first)
    mirror = mirror_repository(trap, tmp_path / "cache")
    workdir = tmp_path / "work"
    workdir.mkdir()

    prepare_workdir(trap, mirror, workdir)

    assert _git(workdir, "rev-parse", "HEAD") == first
    assert _git(workdir, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert _git(workdir, "remote") == ""
    assert (workdir / "README.md").is_file()
    assert not (workdir / "LATER.md").exists()


@requires_git
def test_workdir_diff_carries_tracked_changes_and_untracked_files_but_not_harness_files(tmp_path: Path) -> None:
    origin, commit = _make_origin(tmp_path / "upstream")
    trap = _make_trap(tmp_path, origin, commit)
    workdir = tmp_path / "work"
    workdir.mkdir()
    prepare_workdir(trap, mirror_repository(trap, tmp_path / "cache"), workdir)
    (workdir / "README.md").write_text("# demo\nchanged\n", encoding="utf-8")
    (workdir / "src").mkdir()
    (workdir / "src" / "server.py").write_text("import fastlib\n", encoding="utf-8")
    (workdir / ".agent-regression").mkdir()
    (workdir / ".agent-regression" / "mcp.json").write_text("{}\n", encoding="utf-8")

    diff = workdir_diff(workdir)

    assert "+changed" in diff
    assert "?? src/server.py" in diff.splitlines()
    assert ".agent-regression" not in diff


# -------------------------------------------------------------------------------------------
# Index build (stub bruriah)
# -------------------------------------------------------------------------------------------


def _prepared_clone(rig: _Rig, name: str = "clone") -> Path:
    workdir = rig.workdir(name)
    prepare_workdir(rig.trap, mirror_repository(rig.trap, rig.root / "cache"), workdir)
    return workdir


def _init_calls(rig: _Rig) -> list[list[str]]:
    return [call for call in rig.bruriah_calls() if call[:1] == ["init"]]


def test_ensure_index_builds_once_per_trap_and_bruriah_commit_and_then_reuses_it(rig: _Rig) -> None:
    clone = _prepared_clone(rig)
    cache = rig.root / "cache"

    first = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)
    second = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)

    key = f"{rig.trap.trap_id}-{rig.trap.commit[:12]}-b{_SOURCE_COMMIT[:12]}"
    assert first == second == (cache / "indexes" / key / "data", cache / "indexes" / key / "config")
    assert (cache / "indexes" / key / ".built").is_file()
    calls = _init_calls(rig)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("--repo") + 1] == str(clone)
    assert argv[argv.index("--data-dir") + 1] == str(first[0])
    assert argv[argv.index("--config-dir") + 1] == str(first[1])
    assert "--cache-dir" in argv


def test_a_failed_index_build_raises_and_is_retried_next_time(rig: _Rig) -> None:
    clone = _prepared_clone(rig)
    cache = rig.root / "cache"
    rig.behave_bruriah(fail=True)

    with pytest.raises(AdapterError, match="index build failed"):
        ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)
    assert not list((cache / "indexes").glob("*/.built"))

    rig.behave_bruriah()
    ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)
    assert len(_init_calls(rig)) == 2


def test_an_index_built_by_another_bruriah_commit_is_never_reused(rig: _Rig) -> None:
    clone = _prepared_clone(rig)
    cache = rig.root / "cache"
    other = "f" * 40

    first = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)
    second = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=other)
    again = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=other)

    assert first != second == again
    assert second[0].parent.name == f"{rig.trap.trap_id}-{rig.trap.commit[:12]}-b{other[:12]}"
    calls = _init_calls(rig)
    assert len(calls) == 2
    assert [argv[argv.index("--data-dir") + 1] for argv in calls] == [str(first[0]), str(second[0])]
    # The model cache does not depend on Bruriah code: every build shares it.
    assert {argv[argv.index("--cache-dir") + 1] for argv in calls} == {str(cache / "bruriah-cache")}


def test_an_index_in_the_key_format_without_the_bruriah_commit_is_not_reused(rig: _Rig) -> None:
    clone = _prepared_clone(rig)
    cache = rig.root / "cache"
    legacy = cache / "indexes" / f"{rig.trap.trap_id}-{rig.trap.commit[:12]}"
    (legacy / "data").mkdir(parents=True)
    (legacy / ".built").write_text(f"{rig.trap.commit}\n", encoding="utf-8")

    data_dir, _ = ensure_index(rig.trap, clone, cache, rig.bruriah, bruriah_commit=_SOURCE_COMMIT)

    assert data_dir.parent != legacy
    assert len(_init_calls(rig)) == 1
    # The old directory is left alone, never deleted.
    assert (legacy / ".built").is_file()


@pytest.mark.parametrize("commit", ["", "0123456789ab", "G" * 40, _SOURCE_COMMIT.upper()])
def test_ensure_index_refuses_a_bruriah_commit_that_is_not_a_commit_id(rig: _Rig, commit: str) -> None:
    with pytest.raises(AdapterError, match="commit"):
        ensure_index(rig.trap, rig.root, rig.root / "cache", rig.bruriah, bruriah_commit=commit)
    assert _init_calls(rig) == []


def test_the_index_build_never_receives_the_api_key(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = _prepared_clone(rig)
    seen: dict[str, object] = {}
    real_run = subprocess.run

    def spy(*args: object, **kwargs: object):
        env = kwargs.get("env")
        if isinstance(env, dict) and args and "init" in list(args[0]):  # type: ignore[call-overload]
            seen["env"] = env
        return real_run(*args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(subprocess, "run", spy)
    ensure_index(rig.trap, clone, rig.root / "cache", rig.bruriah, bruriah_commit=_SOURCE_COMMIT)

    assert "env" in seen
    assert "ANTHROPIC_API_KEY" not in seen["env"]  # type: ignore[operator]


# -------------------------------------------------------------------------------------------
# End-to-end adapter runs against the stub client
# -------------------------------------------------------------------------------------------


def test_a_baseline_run_records_the_client_transcript_and_the_detector_verdict(rig: _Rig) -> None:
    run = rig.run(rig.adapter(), BASELINE)

    assert (run.trap_id, run.condition, run.repetition) == ("trap-a", BASELINE, 0)
    assert run.tool_calls == (ToolCall("Read", 0), ToolCall("Edit", 1))
    assert run.turns == 3
    assert run.input_tokens == 6 + 56281 + 139878
    assert run.output_tokens == 222
    assert run.cost_usd == pytest.approx(0.48)
    assert run.exit_reason == "done"
    assert run.wall_clock_seconds >= 0
    assert run.detection == Detection(regressed=True, evidence=("import fastlib",), completed=True)
    assert run.provenance == Provenance(
        date="2026-09-27",
        model_id=_MODEL,
        client="claude-code",
        client_version=_CLIENT_VERSION,
        bruriah_version=bruriah.__version__,
        trap_set_digest=_DIGEST,
        repetitions=2,
        bruriah_commit=_SOURCE_COMMIT,
    )
    assert run.transcript == "transcripts/trap-a/baseline-0.jsonl"
    transcript = rig.root / "report" / run.transcript
    lines = transcript.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["subtype"] == "init"
    assert json.loads(lines[-1])["type"] == "result"


def test_a_baseline_run_invokes_the_client_isolated_in_the_clone(rig: _Rig) -> None:
    run = rig.run(rig.adapter(), BASELINE)

    (call,) = rig.claude_calls()
    workdir = rig.root / "work" / "trap-a-baseline-0"
    assert Path(call["cwd"]).resolve() == workdir.resolve()
    argv = call["argv"]
    assert argv[:2] == ["-p", _PROMPT]
    assert "--bare" not in argv
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" in argv
    assert _flag_values(argv, "--allowedTools") == list(ALLOWED_TOOLS)
    assert argv[argv.index("--max-turns") + 1] == "7"
    assert call["claude_md"] is None
    assert not (workdir / "CLAUDE.md").exists()
    assert not (workdir / ".agent-regression").exists()
    assert not _init_calls(rig), "baseline must not build an index"
    assert run.exit_reason == "done"


def test_the_client_runs_logged_in_with_the_real_home_and_a_minimal_environment(rig: _Rig) -> None:
    rig.run(rig.adapter(), BASELINE)

    (call,) = rig.claude_calls()
    # The operator's login lives in the OS keychain, so `HOME` is the real one. Nothing else of
    # the operator's environment goes along: not the sentinel, and not an API key even when one is
    # set (the rig sets one), because a key would silently switch the client from the login to
    # per-token billing and the run's provenance would no longer say which path it took.
    assert call["home"] == os.environ["HOME"]
    assert call["key_sha256"] is None
    assert _SENTINEL not in call["env_names"]
    assert not any(name.startswith(("CLAUDE_", "ANTHROPIC_")) for name in call["env_names"])
    # `LC_CTYPE` is the stub interpreter's own doing (PEP 538 locale coercion exports it) and
    # `__CF_USER_TEXT_ENCODING` is macOS's, injected into every process; neither came from the adapter.
    assert {name for name in call["env_names"] if not name.startswith(("LC_", "__CF_"))} <= {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "DISABLE_AUTOUPDATER",
    }


def test_the_client_environment_disables_the_autoupdater(rig: _Rig) -> None:
    # A client that updates itself between a run set's pauses and resumes records several client
    # versions in one run set.
    assert _client_env()["DISABLE_AUTOUPDATER"] == "1"
    rig.run(rig.adapter(), BASELINE)

    (call,) = rig.claude_calls()
    assert "DISABLE_AUTOUPDATER" in call["env_names"]


def test_the_api_key_is_never_written_to_any_file(rig: _Rig) -> None:
    adapter = rig.adapter()
    for condition in CONDITIONS:
        run = rig.run(adapter, condition)
        payload = json.dumps(run_to_json(run))
        assert _API_KEY not in payload

    for path in rig.root.rglob("*"):
        if path.is_file():
            assert _API_KEY.encode("utf-8") not in path.read_bytes(), path


def test_an_unprompted_run_registers_the_real_server_strictly_and_leaves_claude_md_alone(rig: _Rig) -> None:
    rig.behave(write=_REGRESSING_EDIT, tool_calls=["mcp__bruriah__investigate_work", "Read", "Edit"])

    run = rig.run(rig.adapter(), UNPROMPTED)

    (call,) = rig.claude_calls()
    workdir = rig.root / "work" / "trap-a-unprompted-0"
    argv = call["argv"]
    mcp_config = workdir / ".agent-regression" / "mcp.json"
    assert _flag_values(argv, "--mcp-config") == [str(mcp_config)]
    assert "--strict-mcp-config" in argv
    assert _flag_values(argv, "--allowedTools") == list(ALLOWED_TOOLS + MCP_TOOLS)
    server = json.loads(mcp_config.read_text(encoding="utf-8"))["mcpServers"]["bruriah"]
    assert server["command"] == str(rig.bruriah)
    assert server["args"][0] == "serve"
    key = f"trap-a-{rig.commit[:12]}-b{_SOURCE_COMMIT[:12]}"
    assert server["args"][server["args"].index("--data-dir") + 1] == str(rig.root / "cache" / "indexes" / key / "data")
    assert call["claude_md"] is None
    assert consulted_before_first_write(run)
    assert run.exit_reason == "done"


def test_runs_of_another_bruriah_commit_build_their_own_index_and_share_mirror_and_model_cache(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.run(rig.adapter(), UNPROMPTED, 0)
    other = "f" * 40
    tree = _Tree(commit=other)
    monkeypatch.setattr(claude_code_module, "run_command", _FakeRunner(rig.runner.server_file, tree, tree))
    rig.run(rig.adapter(), UNPROMPTED, 1)
    rig.run(rig.adapter(), UNPROMPTED, 2)

    cache = rig.root / "cache"
    calls = _init_calls(rig)
    assert [Path(argv[argv.index("--data-dir") + 1]).parent.name for argv in calls] == [
        f"trap-a-{rig.commit[:12]}-b{_SOURCE_COMMIT[:12]}",
        f"trap-a-{rig.commit[:12]}-b{other[:12]}",
    ]
    assert {argv[argv.index("--cache-dir") + 1] for argv in calls} == {str(cache / "bruriah-cache")}
    assert len(list((cache / "mirrors").glob("*.git"))) == 1


def test_a_prompted_run_passes_the_instruction_as_system_prompt_and_leaves_claude_md_alone(
    tmp_path: Path, rig: _Rig
) -> None:
    origin, commit = _make_origin(tmp_path / "with-claude-md", {"CLAUDE.md": "# Project rules\n", "README.md": "x\n"})
    trap = _make_trap(tmp_path / "second", origin, commit, trap_id="trap-b")

    rig.run(rig.adapter(), PROMPTED, trap=trap)

    (call,) = rig.claude_calls()
    assert _flag_values(call["argv"], "--append-system-prompt") == [PROMPTED_INSTRUCTION]
    assert call["claude_md"] == "# Project rules\n"
    assert "--strict-mcp-config" in call["argv"]


def test_a_prompted_run_writes_no_claude_md_when_the_repository_has_none(rig: _Rig) -> None:
    rig.run(rig.adapter(), PROMPTED)

    (call,) = rig.claude_calls()
    assert call["claude_md"] is None
    assert not (rig.root / "work" / "trap-a-prompted-0" / "CLAUDE.md").exists()


def test_a_gated_run_registers_the_server_and_a_pre_tool_use_gate_outside_the_clone(rig: _Rig) -> None:
    # The agent writes where the gate used to live; the real gate is out of its reach.
    rig.behave(
        write={
            **_REGRESSING_EDIT,
            ".agent-regression/gated_hook.py": "raise SystemExit(0)\n",
            ".agent-regression/gate-state.json": "{}\n",
        },
        tool_calls=["Edit", "mcp__bruriah__investigate_work", "Edit"],
    )

    run = rig.run(rig.adapter(), GATED)

    (call,) = rig.claude_calls()
    workdir = rig.root / "work" / "trap-a-gated-0"
    harness = workdir / ".agent-regression"
    argv = call["argv"]
    assert _flag_values(argv, "--mcp-config") == [str(harness / "mcp.json")]
    (settings_arg,) = _flag_values(argv, "--settings")
    settings_path = Path(settings_arg)
    gate_dir = settings_path.parent
    assert gate_dir.parent == rig.root / "cache" / "gates"
    assert not gate_dir.resolve().is_relative_to(workdir.resolve())
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert _flag_values(argv, "--allowedTools") == list(ALLOWED_TOOLS + MCP_TOOLS)
    assert _flag_values(argv, "--append-system-prompt") == [PROMPTED_INSTRUCTION]
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert command == gate_hook_command(gate_dir / "gated_hook.py", gate_dir / gated_hook.STATE_FILE)
    assert str(workdir) not in command
    assert (gate_dir / "gated_hook.py").read_bytes() == Path(gated_hook.__file__).read_bytes()
    assert not (harness / "settings.json").exists()
    assert sorted(path.name for path in harness.iterdir()) == ["gate-state.json", "gated_hook.py", "mcp.json"]
    assert ".agent-regression" not in workdir_diff(workdir)
    assert call["claude_md"] is None
    assert (run.condition, run.exit_reason) == (GATED, "done")
    assert run.transcript == "transcripts/trap-a/gated-0.jsonl"


def test_a_gated_run_records_the_gate_denial_from_the_transcript_and_its_state_file(rig: _Rig) -> None:
    rig.behave(
        write=_REGRESSING_EDIT,
        tool_calls=["Edit", "mcp__bruriah__investigate_work", "Edit"],
        run_gate=True,
        # The agent quoting the reason is its own text, not a denial.
        assistant_text=[gated_hook.GATE_REASON],
    )

    run = rig.run(rig.adapter(), GATED)

    assert (run.gate_denials, run.gate_errors, run.gate_state_written) == (1, 0, True)


def test_a_gated_run_records_a_broken_gate_as_gate_errors(rig: _Rig) -> None:
    rig.behave(tool_calls=["Edit", "Edit"], run_gate=True, gate_stdin="not json")

    run = rig.run(rig.adapter(), GATED)

    assert (run.gate_denials, run.gate_errors, run.gate_state_written) == (0, 2, False)


def test_a_gated_run_reads_the_state_file_independently_of_the_transcript(rig: _Rig) -> None:
    rig.behave(tool_calls=["Edit"], run_gate=True, gate_silent=True)

    run = rig.run(rig.adapter(), GATED)

    assert (run.gate_denials, run.gate_errors, run.gate_state_written) == (0, 0, True)


def test_a_gated_run_whose_gate_never_fired_records_zero_and_no_state(rig: _Rig) -> None:
    rig.behave(tool_calls=["Read"], run_gate=True)

    run = rig.run(rig.adapter(), GATED)

    assert (run.gate_denials, run.gate_errors, run.gate_state_written) == (0, 0, False)


@pytest.mark.parametrize("condition", DEFAULT_CONDITIONS)
def test_a_default_condition_run_records_no_gate_activity(rig: _Rig, condition: str) -> None:
    rig.behave(tool_calls=["Read", "Edit"], tool_output=gated_hook.GATE_REASON)

    run = rig.run(rig.adapter(), condition)

    assert (run.gate_denials, run.gate_errors, run.gate_state_written) == (None, None, None)


def test_the_default_conditions_install_no_gate(rig: _Rig) -> None:
    adapter = rig.adapter()
    for condition in DEFAULT_CONDITIONS:
        rig.run(adapter, condition)

    for call in rig.claude_calls():
        assert "--settings" not in call["argv"]
    for condition in DEFAULT_CONDITIONS:
        harness = rig.root / "work" / f"trap-a-{condition}-0" / ".agent-regression"
        assert not (harness / "settings.json").exists(), condition
        assert not (harness / "gated_hook.py").exists(), condition
    assert not (rig.root / "cache" / "gates").exists()


def test_gated_runs_never_share_a_gate(rig: _Rig) -> None:
    adapter = rig.adapter()
    rig.run(adapter, GATED, 0)
    rig.run(adapter, GATED, 1)

    settings = [Path(_flag_values(call["argv"], "--settings")[0]) for call in rig.claude_calls()]
    assert len({path.parent for path in settings}) == 2
    for repetition in (0, 1):
        harness = rig.root / "work" / f"trap-a-gated-{repetition}" / ".agent-regression"
        assert sorted(path.name for path in harness.iterdir()) == ["mcp.json"]


def test_the_index_is_built_once_across_repetitions_and_conditions(rig: _Rig) -> None:
    adapter = rig.adapter()
    for condition in (UNPROMPTED, PROMPTED):
        for repetition in (0, 1):
            rig.run(adapter, condition, repetition)

    assert len(_init_calls(rig)) == 1
    assert len(rig.claude_calls()) == 4


def test_the_client_version_is_asked_once_per_adapter(rig: _Rig) -> None:
    adapter = rig.adapter()
    rig.run(adapter, BASELINE, 0)
    rig.run(adapter, BASELINE, 1)

    assert rig.version_calls() == 1


def test_a_bruriah_executable_of_another_version_is_refused(rig: _Rig) -> None:
    rig.behave_bruriah(version="bruriah 0.0.1")

    with pytest.raises(AdapterError, match="0.0.1"):
        rig.run(rig.adapter(), BASELINE)


# -------------------------------------------------------------------------------------------
# The Bruriah commit: the source the `--bruriah` executable imports, and the harness's own
# -------------------------------------------------------------------------------------------


def test_a_run_records_the_commit_of_the_source_the_bruriah_executable_imports(rig: _Rig) -> None:
    run = rig.run(rig.adapter(), BASELINE)

    assert run.provenance.bruriah_commit == _SOURCE_COMMIT
    # The stub's `#!` line names this interpreter; it is asked where `bruriah` comes from.
    assert rig.runner.probes() == [[sys.executable, "-P", "-c", _PROBE]]
    server_dir = str(rig.runner.server_file.parent)
    harness_dir = str(claude_code_module.HARNESS_SOURCE.parent)
    for directory in (server_dir, harness_dir):
        asked = [call[call.index("-C") + 1 :] for call in rig.runner.calls if directory in call]
        assert ["rev-parse", "HEAD"] in [call[1:] for call in asked]
        assert any("status" in call and "--porcelain" in call for call in asked)


def test_the_bruriah_commit_is_resolved_once_per_adapter(rig: _Rig) -> None:
    adapter = rig.adapter()
    rig.run(adapter, BASELINE, 0)
    rig.run(adapter, BASELINE, 1)

    assert adapter.bruriah_commit() == _SOURCE_COMMIT
    assert len(rig.runner.probes()) == 1


@pytest.mark.parametrize(
    ("server", "harness", "message"),
    [
        (_Tree(status=" M src/bruriah/server.py\n"), None, "uncommitted changes"),
        (_Tree(commit=None), None, "not inside a git worktree"),
        (_Tree(tracked=False), None, "not tracked"),
        (_Tree(commit="not-a-sha"), None, "not-a-sha"),
        (None, _Tree(status="M  evals/agent_regression/run.py\n"), "uncommitted changes"),
        (None, _Tree(commit=None), "not inside a git worktree"),
    ],
    ids=["server-dirty", "server-no-git", "server-untracked", "server-bad-sha", "harness-dirty", "harness-no-git"],
)
def test_a_run_refuses_a_source_whose_commit_cannot_be_proven(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, server: _Tree | None, harness: _Tree | None, message: str
) -> None:
    runner = _FakeRunner(rig.runner.server_file, server, harness)
    monkeypatch.setattr(claude_code_module, "run_command", runner)

    with pytest.raises(AdapterError, match=message):
        rig.run(rig.adapter(), BASELINE)

    assert not rig.claude_calls()


def test_a_run_refuses_a_server_of_another_commit_than_the_harness(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    old = "4d1f72e" + "0" * 33
    monkeypatch.setattr(
        claude_code_module, "run_command", _FakeRunner(rig.runner.server_file, server=_Tree(commit=old))
    )

    with pytest.raises(AdapterError) as raised:
        rig.run(rig.adapter(), BASELINE)

    assert old in str(raised.value)
    assert _SOURCE_COMMIT in str(raised.value)
    assert not rig.claude_calls()


def test_the_runner_given_to_the_adapter_is_the_one_it_uses(rig: _Rig) -> None:
    runner = _FakeRunner(rig.runner.server_file, server=_Tree(commit="f" * 40), harness=_Tree(commit="f" * 40))
    adapter = ClaudeCodeAdapter(rig.config(), trap_set_digest=_DIGEST, repetitions=2, runner=runner)

    assert adapter.bruriah_commit() == "f" * 40
    assert runner.probes() and not rig.runner.calls


def test_the_interpreter_is_the_console_script_s_shebang(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "bruriah"
    _write_stub(script, "print('hi')\n")

    assert executable_interpreter(script) == Path(sys.executable)


def test_the_interpreter_of_an_env_shebang_is_found_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "elsewhere"
    python = _write_stub(bin_dir / "python3", "")
    monkeypatch.setenv("PATH", str(bin_dir))
    script = tmp_path / "bin" / "bruriah"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")

    assert executable_interpreter(script) == python


def test_the_interpreter_of_a_script_without_a_python_shebang_is_the_venv_next_to_it(tmp_path: Path) -> None:
    bin_dir = tmp_path / "venv" / "bin"
    python = _write_stub(bin_dir / "python", "")
    # The form pip writes for a long interpreter path: a `/bin/sh` shebang that re-executes Python.
    script = bin_dir / "bruriah"
    script.write_text("#!/bin/sh\n'''exec' \"$0\" \"$@\"\n' '''\n", encoding="utf-8")
    binary = bin_dir / "native-bruriah"
    binary.write_bytes(b"\x7fELF\x00\x01")

    assert executable_interpreter(script) == python
    assert executable_interpreter(binary) == python


def test_an_executable_with_no_findable_interpreter_is_refused(tmp_path: Path) -> None:
    script = tmp_path / "bin" / "bruriah"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    with pytest.raises(AdapterError, match="interpreter"):
        executable_interpreter(script)


def test_a_run_that_exhausts_its_turns_is_a_turn_budget_exit(rig: _Rig) -> None:
    rig.behave(subtype="error_max_turns", is_error=True, exit_code=1)

    assert rig.run(rig.adapter(), BASELINE).exit_reason == "turn_budget"


def test_a_client_that_exits_nonzero_is_an_error_run(rig: _Rig) -> None:
    rig.behave(exit_code=2, result=False, init=False)

    run = rig.run(rig.adapter(), UNPROMPTED)

    assert run.exit_reason == "error"
    assert run.turns == 0
    assert run.input_tokens is None
    assert run.cost_usd is None


def test_a_run_past_its_time_budget_is_stopped_and_keeps_its_partial_transcript(tmp_path: Path, rig: _Rig) -> None:
    trap = _make_trap(tmp_path / "slow", rig.origin, rig.commit, trap_id="trap-slow", time_budget=1)
    rig.behave(sleep=10, tool_calls=["Read"])

    started = time.monotonic()
    run = rig.run(rig.adapter(), BASELINE, trap=trap)
    elapsed = time.monotonic() - started

    assert run.exit_reason == "time_budget"
    assert elapsed < 8
    assert run.turns == 0
    assert run.transcript is not None
    lines = (rig.root / "report" / run.transcript).read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["subtype"] == "init"


# The rig's trap is decided by a document, so its one cue is the document path.
_DECISION_DOC = "docs/decisions/0001-tool-declarations.md"


def test_a_run_whose_final_message_cites_the_decision_records_it(rig: _Rig) -> None:
    rig.behave(write=_REGRESSING_EDIT, tool_calls=["Read", "Edit"], final_text=f"Overrode {_DECISION_DOC} on purpose.")

    run = rig.run(rig.adapter(), BASELINE)

    assert rig.trap.citation_cues == (_DECISION_DOC,)
    assert run.cited_decision is True


def test_a_decision_read_only_in_tool_output_is_not_cited(rig: _Rig) -> None:
    rig.behave(
        write=_REGRESSING_EDIT,
        tool_calls=["Read", "Edit"],
        tool_output=f"{_DECISION_DOC}: declare each tool once",
        final_text="Switched the server to fastlib.",
    )

    run = rig.run(rig.adapter(), BASELINE)

    assert run.cited_decision is False


def test_a_run_without_a_final_message_cites_nothing(rig: _Rig) -> None:
    run = rig.run(rig.adapter(), BASELINE)

    assert run.transcript is not None
    assert run.cited_decision is False


def test_a_run_stopped_by_its_time_budget_is_scored_on_its_last_assistant_text(tmp_path: Path, rig: _Rig) -> None:
    trap = _make_trap(tmp_path / "slow", rig.origin, rig.commit, trap_id="trap-slow", time_budget=1)
    rig.behave(sleep=10, assistant_text=[f"Keeping {_DECISION_DOC} in mind."])

    run = rig.run(rig.adapter(), BASELINE, trap=trap)

    assert run.exit_reason == "time_budget"
    assert run.cited_decision is True


def test_non_json_output_lines_do_not_break_a_run(rig: _Rig) -> None:
    rig.behave(garbage=True, tool_calls=["Read"])

    run = rig.run(rig.adapter(), BASELINE)

    assert run.exit_reason == "done"
    assert run.tool_calls == (ToolCall("Read", 0),)


@pytest.mark.parametrize(
    ("condition", "behavior"),
    [
        (UNPROMPTED, {"extra_servers": ["cerebro"]}),
        (PROMPTED, {"drop_servers": True}),
        (BASELINE, {"extra_servers": ["engram"]}),
        (UNPROMPTED, {"server_status": "failed"}),
    ],
    ids=["stray-server", "missing-server", "baseline-server", "failed-server"],
)
def test_a_run_whose_client_loaded_the_wrong_mcp_servers_is_invalid(rig: _Rig, condition: str, behavior: dict) -> None:
    rig.behave(**behavior)

    with pytest.raises(AdapterError, match="MCP"):
        rig.run(rig.adapter(), condition)


def test_adapter_error_is_the_failure_type_the_benchmark_driver_defines() -> None:
    """The driver recovers from exactly this type, so every adapter raises the one it defines."""
    assert AdapterError is adapters_module.AdapterError


def test_a_run_that_leaves_a_remote_in_the_clone_is_invalid(rig: _Rig) -> None:
    rig.behave(add_remote=True)

    with pytest.raises(AdapterError, match="remote"):
        rig.run(rig.adapter(), BASELINE)


@pytest.mark.parametrize("path", [".claude/settings.json", ".mcp.json"])
def test_a_trap_whose_commit_carries_client_configuration_is_refused_before_the_client_runs(
    tmp_path: Path, rig: _Rig, path: str
) -> None:
    # Even with no setting source selected, a clone that ships Claude Code configuration is a
    # clone whose author could steer the client; the harness refuses it rather than trusting a flag.
    origin, commit = _make_origin(tmp_path / "configured", {path: "{}\n", "README.md": "x\n"})
    trap = _make_trap(tmp_path / "configured-trap", origin, commit, trap_id="trap-c")

    with pytest.raises(AdapterError, match="configuration"):
        rig.run(rig.adapter(), BASELINE, trap=trap)

    assert not rig.claude_calls()


# -------------------------------------------------------------------------------------------
# Run record additions: cost and transcript
# -------------------------------------------------------------------------------------------


def _record(**overrides: object) -> AgentRun:
    fields: dict[str, object] = {
        "trap_id": "trap-a",
        "condition": BASELINE,
        "repetition": 0,
        "tool_calls": (ToolCall("Read", 0),),
        "turns": 3,
        "wall_clock_seconds": 7.5,
        "input_tokens": 10,
        "output_tokens": 2,
        "exit_reason": "done",
        "detection": Detection(regressed=False, evidence=(), completed=True),
        "provenance": Provenance(
            date="2026-09-27",
            model_id=_MODEL,
            client="claude-code",
            client_version=_CLIENT_VERSION,
            bruriah_version=bruriah.__version__,
            trap_set_digest=_DIGEST,
            repetitions=1,
        ),
    }
    fields.update(overrides)
    return AgentRun(**fields)  # type: ignore[arg-type]


def test_cost_and_transcript_default_to_none() -> None:
    run = _record()

    assert run.cost_usd is None
    assert run.transcript is None
    payload = run_to_json(run)
    assert payload["cost_usd"] is None
    assert payload["transcript"] is None


def test_cost_and_transcript_round_trip() -> None:
    run = _record(cost_usd=0.48, transcript="transcripts/trap-a/baseline-0.jsonl")

    assert run_from_json(json.loads(json.dumps(run_to_json(run)))) == run


def test_a_record_without_cost_or_transcript_still_loads() -> None:
    payload = run_to_json(_record())
    del payload["cost_usd"]
    del payload["transcript"]

    assert run_from_json(payload) == _record()


@pytest.mark.parametrize("cost", ["0.48", True, -1.0, [0.48]])
def test_run_from_json_rejects_a_cost_that_is_not_a_non_negative_number(cost: object) -> None:
    payload = run_to_json(_record())
    payload["cost_usd"] = cost

    with pytest.raises(RunRecordError, match="cost_usd"):
        run_from_json(payload)


@pytest.mark.parametrize("transcript", [3, "/abs/transcripts/a.jsonl", "C:\\transcripts\\a.jsonl"])
def test_run_from_json_rejects_a_transcript_that_is_not_a_relative_path(transcript: object) -> None:
    payload = run_to_json(_record())
    payload["transcript"] = transcript

    with pytest.raises(RunRecordError, match="transcript"):
        run_from_json(payload)


# -------------------------------------------------------------------------------------------
# run.py: the real (non-dry) run
# -------------------------------------------------------------------------------------------


def _main_args(rig: _Rig, out: Path, *extra: str) -> list[str]:
    return [
        "--traps",
        str(rig.root / "traps"),
        "--repetitions",
        "1",
        "--claude",
        str(rig.claude),
        "--bruriah",
        str(rig.bruriah),
        "--cache-dir",
        str(rig.root / "cache"),
        "--out",
        str(out),
        "--date",
        "2026-09-27",
        *extra,
    ]


def test_main_runs_the_benchmark_end_to_end_and_writes_both_reports(rig: _Rig) -> None:
    out = rig.root / "out"

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 0
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [(run["trap_id"], run["condition"], run["repetition"]) for run in payload["runs"]] == [
        ("trap-a", condition, 0) for condition in DEFAULT_CONDITIONS
    ]
    report = (out / "report.md").read_text(encoding="utf-8")
    assert report.startswith("# Agent regression benchmark")
    for run in payload["runs"]:
        assert not Path(run["transcript"]).is_absolute()
        assert (out / run["transcript"]).is_file()
        assert run["provenance"]["model_id"] == _MODEL
        assert run["provenance"]["client"] == "claude-code"
        assert run["provenance"]["repetitions"] == 1
        assert run["provenance"]["trap_set_digest"] == trap_set_digest(load_traps(rig.root / "traps"))
        assert run["provenance"]["date"] == "2026-09-27"
    assert len(rig.claude_calls()) == 3
    assert _API_KEY not in (out / "runs.json").read_text(encoding="utf-8")


def test_main_requires_an_explicit_model_for_a_real_run(rig: _Rig, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = run_module.main(_main_args(rig, rig.root / "out"))

    assert exit_code == 2
    assert "--model" in capsys.readouterr().err
    assert not rig.claude_calls()


def test_main_runs_a_real_run_without_any_api_key(rig: _Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    exit_code = run_module.main(_main_args(rig, rig.root / "out", "--model", _MODEL))

    assert exit_code == 0
    assert len(rig.claude_calls()) == 3


@pytest.mark.parametrize("missing", ["claude", "bruriah"])
def test_main_fails_clearly_when_an_executable_cannot_be_resolved(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    empty = rig.root / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    args = _main_args(rig, rig.root / "out", "--model", _MODEL)
    flag = f"--{missing}"
    index = args.index(flag)
    del args[index : index + 2]

    exit_code = run_module.main(args)

    assert exit_code == 2
    assert missing in capsys.readouterr().err
    assert not rig.claude_calls()


def test_main_appends_every_run_record_to_runs_jsonl_as_it_finishes(rig: _Rig) -> None:
    out = rig.root / "out"

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 0
    lines = (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(rig.claude_calls()) == 3
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [run_from_json(json.loads(line)) for line in lines] == [run_from_json(run) for run in payload["runs"]]


def test_main_resumes_from_runs_jsonl_without_invoking_the_client_again(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL)) == 0
    calls = len(rig.claude_calls())
    first = (out / "runs.json").read_text(encoding="utf-8")
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 0
    assert len(rig.claude_calls()) == calls
    assert "resuming: 3 recorded run(s) skipped" in capsys.readouterr().out
    assert (out / "runs.json").read_text(encoding="utf-8") == first
    assert len((out / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def _rewrite_provenance(log: Path, field: str, value: str) -> None:
    lines = []
    for line in log.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        payload["provenance"][field] = value
        lines.append(json.dumps(payload) + "\n")
    log.write_text("".join(lines), encoding="utf-8")


_CURRENT_PROVENANCE = {
    "client_version": _CLIENT_VERSION,
    "bruriah_version": bruriah.__version__,
    "bruriah_commit": _SOURCE_COMMIT,
    "model_id": _MODEL,
}


def test_main_refuses_to_start_when_the_bruriah_source_is_dirty(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _FakeRunner(rig.runner.server_file, server=_Tree(status=" M src/bruriah/server.py\n"))
    monkeypatch.setattr(claude_code_module, "run_command", runner)
    out = rig.root / "out"

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert "src/bruriah/server.py" in err
    assert not rig.claude_calls()
    assert not out.exists()


def test_main_records_the_bruriah_commit_in_every_run(rig: _Rig) -> None:
    out = rig.root / "out"

    assert run_module.main(_main_args(rig, out, "--model", _MODEL)) == 0

    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert {run["provenance"]["bruriah_commit"] for run in payload["runs"]} == {_SOURCE_COMMIT}
    assert f"| bruriah_commit | {_SOURCE_COMMIT} (3) |" in (out / "report.md").read_text(encoding="utf-8")


def test_main_refuses_to_resume_runs_recorded_without_a_bruriah_commit(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE)) == 0
    log = out / "runs.jsonl"
    legacy = json.loads(log.read_text(encoding="utf-8"))
    del legacy["provenance"]["bruriah_commit"]
    log.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    calls = len(rig.claude_calls())
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "bruriah_commit recorded unrecorded, current " + _SOURCE_COMMIT in err
    assert len(rig.claude_calls()) == calls


def test_main_runs_only_the_traps_named_by_trap_ids_and_digests_only_them(rig: _Rig) -> None:
    other = _make_trap(rig.root, rig.origin, rig.commit, trap_id="trap-b")
    out = rig.root / "out"

    exit_code = run_module.main(
        _main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE, "--trap-ids", "trap-b")
    )

    assert exit_code == 0
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [run["trap_id"] for run in payload["runs"]] == ["trap-b"]
    digest = payload["runs"][0]["provenance"]["trap_set_digest"]
    assert digest == trap_set_digest([other])
    assert digest != trap_set_digest(load_traps(rig.root / "traps"))


@pytest.mark.parametrize("field", ["client_version", "bruriah_version", "bruriah_commit"])
def test_main_refuses_to_resume_before_any_run_when_the_client_or_bruriah_version_changed(
    rig: _Rig, capsys: pytest.CaptureFixture[str], field: str
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE)) == 0
    log = out / "runs.jsonl"
    _rewrite_provenance(log, field, "0.0.1-recorded")
    before = log.read_bytes()
    calls = len(rig.claude_calls())
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert field in err
    assert "0.0.1-recorded" in err
    assert _CURRENT_PROVENANCE[field] in err
    # Only `claude --version` was asked; no run started and nothing was appended.
    assert not [call for call in rig.claude_calls()[calls:] if "argv" in call]
    assert log.read_bytes() == before


def test_main_refuses_to_append_a_run_whose_model_differs_from_the_recorded_runs(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE)) == 0
    log = out / "runs.jsonl"
    _rewrite_provenance(log, "model_id", "claude-recorded-model")
    before = log.read_bytes()
    report = (out / "runs.json").read_bytes()
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "model_id" in err
    assert "claude-recorded-model" in err
    assert _MODEL in err
    # The model is only known from the first new run's init line: that run is not appended, and
    # no further run starts.
    assert log.read_bytes() == before
    assert (out / "runs.json").read_bytes() == report
    assert len([call for call in rig.claude_calls() if "argv" in call]) == 2


def test_main_refuses_to_resume_a_runs_log_that_already_mixes_client_versions(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE, UNPROMPTED)) == 0
    log = out / "runs.jsonl"
    first, second = log.read_text(encoding="utf-8").splitlines()
    mixed = json.loads(second)
    mixed["provenance"]["client_version"] = "2.1.286 (Claude Code)"
    log.write_text(first + "\n" + json.dumps(mixed) + "\n", encoding="utf-8")
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 2
    assert "2.1.286 (Claude Code)" in capsys.readouterr().err
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_main_resumes_and_appends_when_the_provenance_is_unchanged(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    assert run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE)) == 0
    capsys.readouterr()

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 0
    assert "resuming: 1 recorded run(s) skipped" in capsys.readouterr().out
    assert len((out / "runs.jsonl").read_text(encoding="utf-8").splitlines()) == 3


def test_main_ignores_a_recorded_run_of_another_trap_set_with_a_warning(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    out.mkdir()
    # `_record` carries `_DIGEST`, not the digest of the rig's trap set.
    foreign = json.dumps(run_to_json(_record()))
    (out / "runs.jsonl").write_text(foreign + "\n", encoding="utf-8")

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL))

    assert exit_code == 0
    assert len(rig.claude_calls()) == 3
    err = capsys.readouterr().err
    assert "warning" in err
    assert "1 recorded run(s)" in err
    lines = (out / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines[0] == foreign
    assert len(lines) == 4
    assert len(json.loads((out / "runs.json").read_text(encoding="utf-8"))["runs"]) == 3


def test_main_records_an_invalid_run_as_a_failure_exits_3_and_retries_it_on_the_next_run(
    rig: _Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    out = rig.root / "out"
    rig.behave(drop_servers=True)
    args = _main_args(rig, out, "--model", _MODEL, "--conditions", UNPROMPTED)

    exit_code = run_module.main(args)

    assert exit_code == 3
    failures = _read_jsonl(out / "failures.jsonl")
    assert [(f["trap_id"], f["condition"], f["repetition"]) for f in failures] == [("trap-a", UNPROMPTED, 0)]
    assert "MCP" in failures[0]["error"]
    assert "error: trap-a unprompted 0: " in capsys.readouterr().err
    assert not (out / "runs.json").exists()
    assert not (out / "report.md").exists()
    calls = len(rig.claude_calls())

    rig.behave(write=_REGRESSING_EDIT, tool_calls=["Read", "Edit"])
    exit_code = run_module.main(args)

    assert exit_code == 0
    assert len(rig.claude_calls()) == calls + 1
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [(run["trap_id"], run["condition"], run["repetition"]) for run in payload["runs"]] == [
        ("trap-a", UNPROMPTED, 0)
    ]


def test_main_still_writes_the_report_of_the_runs_that_succeeded_when_one_failed(rig: _Rig) -> None:
    out = rig.root / "out"
    # Baseline registers no server, so dropping servers invalidates only the unprompted run.
    rig.behave(drop_servers=True)

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", BASELINE, UNPROMPTED))

    assert exit_code == 3
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [(run["trap_id"], run["condition"]) for run in payload["runs"]] == [("trap-a", BASELINE)]
    assert (out / "report.md").is_file()
    assert len(_read_jsonl(out / "failures.jsonl")) == 1


def test_main_runs_only_the_gated_condition_when_asked_for_it(rig: _Rig) -> None:
    out = rig.root / "out"

    exit_code = run_module.main(_main_args(rig, out, "--model", _MODEL, "--conditions", GATED))

    assert exit_code == 0
    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [(run["trap_id"], run["condition"], run["repetition"]) for run in payload["runs"]] == [("trap-a", GATED, 0)]
    assert list(payload["conditions"]) == [GATED]
    (call,) = rig.claude_calls()
    assert "--settings" in call["argv"]
