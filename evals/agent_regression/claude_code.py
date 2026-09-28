"""The Claude Code headless adapter: one `claude -p` run per trap, condition and repetition.

Every invocation is isolated from the operator's own Claude Code setup, because a baseline that
inherits hooks, plugins and a dozen MCP servers (a predecessor of this product and a memory server
among them, on the machine the first run was planned on) is not a baseline. The client runs with
the operator's login (`--bare` would skip it, and a benchmark run has no API key), so isolation
comes from flags and the environment instead: `--setting-sources ""` loads no settings file at
all, neither the operator's (`user`: hooks, plugins, user-level `CLAUDE.md`) nor the trap
repository's (`project`, `local`: a committed `.claude/settings.json` can define hooks, session
environment such as the API base URL, and extra write directories) -- measured on 2.1.283 with a
`SessionStart` hook committed in a probe repository: it ran under `project` and not under `""`;
`--strict-mcp-config` in every condition admits only the servers this run registers;
`--disable-slash-commands` drops the operator's skills; the clone of the pinned commit is fresh,
has no remote, and is refused outright when it carries client configuration (`.claude/`,
`.mcp.json`); the agent has no shell tool; and the child environment is the five variables the
login needs and nothing else. A repository `CLAUDE.md` or `AGENTS.md` at the pinned commit is
tree content the agent may read, like any other file. The run is then checked after the fact:
the client's init line must list exactly the servers the condition registered, and the clone
must still have no remote, or the run is refused rather than recorded. One side effect remains:
the client creates an empty `~/.claude/projects/<workdir>/memory` directory per run.

The wire format is Claude Code's `--output-format stream-json --verbose`, measured on 2.1.283: one
JSON object per line, a `system`/`init` line first, `assistant` lines carrying `tool_use` items,
`user` lines carrying tool results, and a final `result` line with the turn count, cost and usage.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import bruriah
from bruriah.clients import ClientError, LaunchManifest, render_claude_code

from agent_regression.detection import validate_detection
from agent_regression.runs import BASELINE, CONDITIONS, PROMPTED, AgentRun, Provenance, ToolCall
from agent_regression.traps import Trap, load_detector

CLIENT = "claude-code"
SERVER_NAME = "bruriah"

# The one line the `prompted` condition appends to the client's system prompt. It travels as
# `--append-system-prompt`, never as a `CLAUDE.md` file: whether the client reads a repository
# `CLAUDE.md` depends on its mode (`--bare` skips it), and the benchmark must not depend on that.
# Pinned: changing it changes what `prompted` measures.
PROMPTED_INSTRUCTION = (
    "Before editing any file, call the `investigate_work` tool with the task you were given and read the "
    "evidence it returns with `read_evidence` before deciding how to proceed."
)

# Read, search and edit inside the clone through the built-in tools, and no shell. A scoped Bash
# rule is a prefix, not a read-only command: `Bash(find:*)` admits `find -exec`, `Bash(rg:*)`
# admits `rg --pre`, and `Bash(git diff:*)` honours `diff.external` from a `.git/config` the agent
# can write. Any shell child would inherit the operator's login and could push, install, or exfiltrate.
ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "MultiEdit", "Glob", "Grep")
MCP_TOOLS: tuple[str, ...] = (f"mcp__{SERVER_NAME}__investigate_work", f"mcp__{SERVER_NAME}__read_evidence")
DISALLOWED_TOOLS: tuple[str, ...] = ("Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit")

# The directory inside the clone that holds the harness's own files (the MCP config). It is
# listed in the clone's `.git/info/exclude`, so it never shows up in the diff the detector reads.
HARNESS_DIR = ".agent-regression"

# Never forwarded to the client: a key in the environment would silently switch it from the
# operator's login to per-token billing, and the provenance would not say which path a run took.
_CLIENT_API_KEY_ENV = "ANTHROPIC_API_KEY"
# What the logged-in client needs and nothing more (measured on macOS with Claude Code 2.1.283:
# the OAuth token lives in the OS keychain, which `HOME` and `PATH` alone cannot reach).
_CLIENT_ENV_VARS = ("HOME", "PATH", "USER", "LOGNAME", "TMPDIR")
# Kept out of the index build: it needs no key, and it must use the same model cache the server
# later reads, not one an operator variable points elsewhere.
_INDEX_ENV_DROPPED = frozenset({_CLIENT_API_KEY_ENV, "FASTEMBED_CACHE_PATH"})
_WINDOWS_ESSENTIAL_ENV_VARS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "agent-regression",
    "GIT_AUTHOR_EMAIL": "agent-regression@example.invalid",
    "GIT_COMMITTER_NAME": "agent-regression",
    "GIT_COMMITTER_EMAIL": "agent-regression@example.invalid",
}
_VERSION_TIMEOUT_SECONDS = 60
_ERROR_TAIL = 2000


class AdapterError(RuntimeError):
    """Raised when a run cannot be set up, or finished in a state that makes it invalid to record."""


@dataclass(frozen=True)
class ClaudeCodeConfig:
    claude_executable: Path
    bruriah_executable: Path
    model: str
    # Holds `mirrors/` (one bare mirror per trap repository), `indexes/` (one Bruriah index per
    # trap and commit) and the model cache the index build and the server share.
    cache_dir: Path
    transcripts_dir: Path
    provenance_date: str
    max_budget_usd_per_run: float | None = None


@dataclass(frozen=True)
class StreamSummary:
    tool_calls: tuple[ToolCall, ...]
    result_subtype: str | None
    num_turns: int | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    is_error: bool
    model: str | None
    mcp_servers: tuple[tuple[str, str], ...]
    # Whether the client printed its init line at all; without it the run never started.
    init_seen: bool = False


def allowed_tools(condition: str) -> tuple[str, ...]:
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition!r}")
    return ALLOWED_TOOLS if condition == BASELINE else ALLOWED_TOOLS + MCP_TOOLS


def _windows_essentials() -> dict[str, str]:
    return {name: os.environ[name] for name in _WINDOWS_ESSENTIAL_ENV_VARS if os.environ.get(name)}


def _client_env() -> dict[str, str]:
    """The operator's real `HOME` (the login is read from the OS keychain under it), the four
    variables that reach the keychain and the temp directory, and the Windows essentials. Nothing
    else: no `CLAUDE_*`, no `ANTHROPIC_*`, no proxy or locale of the operator's shell."""
    env = {name: os.environ[name] for name in _CLIENT_ENV_VARS if os.environ.get(name)}
    env.update(_windows_essentials())
    if os.name == "nt" and os.environ.get("USERPROFILE"):
        env["USERPROFILE"] = os.environ["USERPROFILE"]
    return env


def _git_env(home: Path) -> dict[str, str]:
    # The same hermetic git environment as `evals/injection/cases.py::_hermetic_git_env`: a
    # throwaway `HOME`, no system or global config, an explicit identity, and no credential prompt
    # that could hang.
    env = {"HOME": str(home), **_windows_essentials()}
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
    env.update(_GIT_IDENTITY)
    return env


def _tail(text: str) -> str:
    return text.strip()[-_ERROR_TAIL:]


def _git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    if shutil.which("git") is None:
        raise AdapterError("git is not available on PATH; the adapter clones every trap repository with it")
    with tempfile.TemporaryDirectory(prefix="agent-regression-git-home-") as home:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_git_env(Path(home)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )
    if check and completed.returncode != 0:
        raise AdapterError(f"git {' '.join(args)} failed: {_tail(completed.stderr)}")
    return completed


def _has_commit(repository: Path, commit: str) -> bool:
    return _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=repository, check=False).returncode == 0


def mirror_repository(trap: Trap, cache_dir: Path) -> Path:
    """A bare mirror of `trap.repository` under `cache_dir/mirrors/`, cloned once per repository and
    fetched again only when it lacks the pinned commit."""
    mirrors = cache_dir / "mirrors"
    mirror = mirrors / f"{hashlib.sha256(trap.repository.encode('utf-8')).hexdigest()[:16]}.git"
    if not mirror.is_dir():
        mirrors.mkdir(parents=True, exist_ok=True)
        # Cloned aside and moved into place, so an interrupted clone never passes for a mirror.
        with tempfile.TemporaryDirectory(prefix=".clone-", dir=mirrors) as staging:
            staged = Path(staging) / "mirror.git"
            _git("clone", "--quiet", "--mirror", trap.repository, str(staged))
            os.replace(staged, mirror)
    elif not _has_commit(mirror, trap.commit):
        _git("fetch", "--quiet", "origin", cwd=mirror)
    if not _has_commit(mirror, trap.commit):
        raise AdapterError(f"trap {trap.trap_id}: commit {trap.commit} is not in {trap.repository}")
    return mirror


def prepare_workdir(trap: Trap, mirror: Path, workdir: Path) -> None:
    """Clone the mirror into `workdir` at the pinned commit, detached, with no remote to push to."""
    _git("clone", "--quiet", "--no-checkout", str(mirror), str(workdir))
    _git("checkout", "--quiet", "--detach", trap.commit, cwd=workdir)
    _git("remote", "remove", "origin", cwd=workdir)
    exclude = workdir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"\n/{HARNESS_DIR}/\n")


def workdir_diff(workdir: Path) -> str:
    """The agent's change as the detector sees it: `git diff HEAD`, then one `?? <path>` line per
    untracked file. The agent can write `.git/config`, so the diff runs with external diff
    drivers, textconv and fsmonitor off: nothing it wrote can run here."""
    diff = _git("diff", "--no-ext-diff", "--no-textconv", "HEAD", cwd=workdir).stdout
    status = _git("-c", "core.fsmonitor=false", "status", "--porcelain", "--untracked-files=all", cwd=workdir).stdout
    untracked = [line for line in status.splitlines() if line.startswith("?? ")]
    return diff + "".join(f"{line}\n" for line in untracked)


def model_cache_dir(cache_dir: Path) -> Path:
    """Where the index build and the server keep the embedding model. Passed to both explicitly:
    the server runs with a redirected `HOME`, so its default cache would be empty on every run."""
    return cache_dir / "bruriah-cache"


def ensure_index(trap: Trap, clone: Path, cache_dir: Path, bruriah_executable: Path) -> tuple[Path, Path]:
    """Build the trap's Bruriah index once per `(trap_id, commit)` with `bruriah init --repo` on a
    clone of the pinned commit, and return its `(data_dir, config_dir)`."""
    root = cache_dir / "indexes" / f"{trap.trap_id}-{trap.commit[:12]}"
    data_dir, config_dir, marker = root / "data", root / "config", root / ".built"
    if marker.is_file():
        return data_dir, config_dir
    root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            str(bruriah_executable),
            "init",
            "--repo",
            str(clone),
            "--data-dir",
            str(data_dir),
            "--config-dir",
            str(config_dir),
            "--cache-dir",
            str(model_cache_dir(cache_dir)),
        ],
        env={name: value for name, value in os.environ.items() if name not in _INDEX_ENV_DROPPED},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AdapterError(
            f"trap {trap.trap_id}: the index build exited {completed.returncode}: {_tail(completed.stderr)}"
        )
    marker.write_text(f"{trap.commit}\n", encoding="utf-8")
    return data_dir, config_dir


def write_mcp_config(
    path: Path, bruriah_executable: Path, data_dir: Path, config_dir: Path, *, model_cache_dir: Path | None = None
) -> Path:
    """Register the real `bruriah serve` process, rendered from the same launch manifest `bruriah
    init` renders every client config from."""
    args = ["serve", "--data-dir", str(data_dir), "--config-dir", str(config_dir)]
    if model_cache_dir is not None:
        args += ["--cache-dir", str(model_cache_dir)]
    try:
        manifest = LaunchManifest(command=str(bruriah_executable), args=tuple(args), server_name=SERVER_NAME)
    except ClientError as error:
        raise AdapterError(
            f"cannot register {bruriah_executable} as the MCP server: the command must be an absolute path and no "
            f"argument may carry shell metacharacters ({error.code})"
        ) from error
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_claude_code(manifest), encoding="utf-8")
    return path


def command_line(
    config: ClaudeCodeConfig, trap: Trap, condition: str, prompt: str, mcp_config: Path | None
) -> list[str]:
    # `--allowedTools`, `--disallowedTools` and `--mcp-config` take several values each, so every
    # entry is its own argv element and the prompt comes first.
    argv = [
        str(config.claude_executable),
        "-p",
        prompt,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--max-turns",
        str(trap.turn_budget),
        "--model",
        config.model,
        "--permission-mode",
        "acceptEdits",
        "--permission-prompts",
        "none",
        "--allowedTools",
        *allowed_tools(condition),
        "--disallowedTools",
        *DISALLOWED_TOOLS,
    ]
    if mcp_config is not None:
        argv += ["--mcp-config", str(mcp_config)]
    if condition == PROMPTED:
        argv += ["--append-system-prompt", PROMPTED_INSTRUCTION]
    if config.max_budget_usd_per_run is not None:
        argv += ["--max-budget-usd", str(config.max_budget_usd_per_run)]
    return argv


def _count(value: object) -> int | None:
    # `bool` is an `int` subclass; `true` is not a count.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    """Summarize a stream-json transcript. A line that is not a JSON object is skipped; a missing
    result line leaves the result fields unset."""
    calls: list[ToolCall] = []
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and init is None:
            init = event
        elif kind == "assistant":
            content = _object(event.get("message")).get("content")
            for item in content if isinstance(content, list) else ():
                if isinstance(item, dict) and item.get("type") == "tool_use" and isinstance(item.get("name"), str):
                    calls.append(ToolCall(name=item["name"], ordinal=len(calls)))
        elif kind == "result":
            result = event

    input_tokens = output_tokens = cost = None
    subtype = None
    if result is not None:
        usage = _object(result.get("usage"))
        counted = [
            _count(usage.get(key)) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ]
        present = [count for count in counted if count is not None]
        input_tokens = sum(present) if present else None
        output_tokens = _count(usage.get("output_tokens"))
        raw_cost = result.get("total_cost_usd")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and raw_cost >= 0:
            cost = float(raw_cost)
        if isinstance(result.get("subtype"), str):
            subtype = result["subtype"]

    servers: list[tuple[str, str]] = []
    model = None
    if init is not None:
        model = init["model"] if isinstance(init.get("model"), str) else None
        raw_servers = init.get("mcp_servers")
        for server in raw_servers if isinstance(raw_servers, list) else ():
            if isinstance(server, dict) and isinstance(server.get("name"), str):
                status = server.get("status")
                servers.append((server["name"], status if isinstance(status, str) else ""))

    return StreamSummary(
        tool_calls=tuple(calls),
        result_subtype=subtype,
        num_turns=_count(result.get("num_turns")) if result else None,
        duration_ms=_count(result.get("duration_ms")) if result else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        is_error=bool(result and result.get("is_error") is True),
        model=model,
        mcp_servers=tuple(servers),
        init_seen=init is not None,
    )


def exit_reason_for(summary: StreamSummary, timed_out: bool, returncode: int) -> str:
    if timed_out:
        return "time_budget"
    # The client reports an exhausted turn budget as an error result and may exit nonzero for it.
    if summary.result_subtype == "error_max_turns":
        return "turn_budget"
    if summary.init_seen and summary.result_subtype == "success" and not summary.is_error and returncode == 0:
        return "done"
    return "error"


def _check_mcp_servers(summary: StreamSummary, condition: str, trap: Trap) -> None:
    """The client loaded exactly the servers the condition registered, and they connected. A run
    that picked up a stray server, or ran without the one it was given, is not a run of that
    condition. A client that never printed its init line is an error run, not an invalid one."""
    if not summary.init_seen:
        return
    names = sorted(name for name, _ in summary.mcp_servers)
    expected = [] if condition == BASELINE else [SERVER_NAME]
    if names != expected:
        raise AdapterError(
            f"trap {trap.trap_id}, condition {condition}: the client loaded MCP servers {names}, expected "
            f"{expected}; the run is not valid for this condition"
        )
    failed = [(name, status) for name, status in summary.mcp_servers if status != "connected"]
    if failed:
        raise AdapterError(
            f"trap {trap.trap_id}, condition {condition}: MCP server(s) did not connect: {failed}; the run is not "
            "valid for this condition"
        )


def _check_no_remote(workdir: Path, trap: Trap) -> None:
    remotes = _git("remote", cwd=workdir).stdout.split()
    if remotes:
        raise AdapterError(f"trap {trap.trap_id}: the run left a remote in the clone ({', '.join(remotes)})")


# Client configuration a repository can commit. No setting source is selected, so the client
# should ignore it; the clone is refused anyway, because a trap whose author can steer the client
# is not a trap, and a flag is a weaker guarantee than an absent file.
_CLIENT_CONFIGURATION = (".claude", ".mcp.json")


def _check_no_client_configuration(workdir: Path, trap: Trap) -> None:
    present = [name for name in _CLIENT_CONFIGURATION if (workdir / name).exists()]
    if present:
        raise AdapterError(
            f"trap {trap.trap_id}: the pinned commit carries Claude Code configuration ({', '.join(present)}); "
            "a repository that configures the client cannot be a trap"
        )


def _kill(process: subprocess.Popen[str]) -> None:
    # The client runs in its own session, so the MCP server it spawned dies with it.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()


class ClaudeCodeAdapter:
    """`AgentAdapter` for Claude Code in headless mode.

    The client runs with the operator's login, never with an API key: a key is not forwarded even
    when the operator's shell carries one, so every run bills the same way and the model line in
    the init event names what actually answered. A run that cannot authenticate ends as an error
    run with the client's own message in its stderr transcript.
    """

    def __init__(self, config: ClaudeCodeConfig, *, trap_set_digest: str, repetitions: int) -> None:
        self._config = config
        self._trap_set_digest = trap_set_digest
        self._repetitions = repetitions
        self._client_version: str | None = None
        self._bruriah_checked = False

    def client_version(self) -> str:
        """`claude --version`, asked once per adapter."""
        if self._client_version is None:
            completed = subprocess.run(
                [str(self._config.claude_executable), "--version"],
                env=_client_env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_SECONDS,
            )
            version = completed.stdout.strip()
            if completed.returncode != 0 or not version:
                raise AdapterError(
                    f"{self._config.claude_executable} --version failed: {_tail(completed.stderr) or 'no output'}"
                )
            self._client_version = version
        return self._client_version

    def _check_bruriah_version(self) -> None:
        """Every record states `bruriah.__version__`; the executable that serves the tools must be
        that version, or the provenance would describe a server that never ran."""
        if self._bruriah_checked:
            return
        completed = subprocess.run(
            [str(self._config.bruriah_executable), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
        words = completed.stdout.split()
        reported = words[-1] if words else ""
        if completed.returncode != 0 or reported != bruriah.__version__:
            raise AdapterError(
                f"{self._config.bruriah_executable} reports version {reported or 'nothing'!r}, but this harness "
                f"records Bruriah {bruriah.__version__}; run it with the matching executable"
            )
        self._bruriah_checked = True

    def _invoke(self, argv: list[str], workdir: Path, budget: int) -> tuple[str, str, int, bool, float]:
        """Run the client in `workdir` with the minimal logged-in environment. Past the time budget
        the client is killed and whatever it had printed is kept."""
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=workdir,
            env=_client_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=budget)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill(process)
            stdout, stderr = process.communicate()
            timed_out = True
        elapsed = time.monotonic() - started
        return stdout or "", stderr or "", process.returncode, timed_out, elapsed

    def _write_transcript(self, trap: Trap, condition: str, repetition: int, stdout: str, stderr: str) -> str:
        """Keep the raw client output and return its path relative to the report directory, which
        is the transcripts directory's parent."""
        name = f"{condition}-{repetition}"
        directory = self._config.transcripts_dir / trap.trap_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.jsonl").write_text(stdout, encoding="utf-8", newline="\n")
        if stderr.strip():
            (directory / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
        return PurePosixPath(self._config.transcripts_dir.name, trap.trap_id, f"{name}.jsonl").as_posix()

    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
        if condition not in CONDITIONS:
            raise AdapterError(f"unknown condition: {condition!r}")
        config = self._config
        client_version = self.client_version()
        self._check_bruriah_version()

        mirror = mirror_repository(trap, config.cache_dir)
        prepare_workdir(trap, mirror, workdir)
        _check_no_client_configuration(workdir, trap)
        mcp_config = None
        if condition != BASELINE:
            data_dir, config_dir = ensure_index(trap, workdir, config.cache_dir, config.bruriah_executable)
            mcp_config = write_mcp_config(
                workdir / HARNESS_DIR / "mcp.json",
                config.bruriah_executable,
                data_dir,
                config_dir,
                model_cache_dir=model_cache_dir(config.cache_dir),
            )

        argv = command_line(config, trap, condition, prompt, mcp_config)
        stdout, stderr, returncode, timed_out, elapsed = self._invoke(argv, workdir, trap.time_budget_seconds)
        transcript = self._write_transcript(trap, condition, repetition, stdout, stderr)
        summary = parse_stream(stdout.splitlines())

        _check_mcp_servers(summary, condition, trap)
        _check_no_remote(workdir, trap)
        detection = validate_detection(load_detector(trap)(workdir, workdir_diff(workdir)))

        return AgentRun(
            trap_id=trap.trap_id,
            condition=condition,
            repetition=repetition,
            tool_calls=summary.tool_calls,
            turns=summary.num_turns or 0,
            wall_clock_seconds=round(elapsed, 3),
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            exit_reason=exit_reason_for(summary, timed_out, returncode),
            detection=detection,
            provenance=Provenance(
                date=config.provenance_date,
                model_id=summary.model or config.model,
                client=CLIENT,
                client_version=client_version,
                bruriah_version=bruriah.__version__,
                trap_set_digest=self._trap_set_digest,
                repetitions=self._repetitions,
            ),
            cost_usd=summary.cost_usd,
            transcript=transcript,
        )
