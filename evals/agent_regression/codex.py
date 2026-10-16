"""The Codex CLI headless adapter: one `codex exec --json` run per trap, condition and repetition.

Measured on `codex-cli` 0.157.1 before writing this (`odd/tasks/heed-rule-and-codex.md`, L7):

- The client reads its configuration, sign-in, rules, skills, memories and MCP servers from
  `CODEX_HOME` (default `~/.codex`). Each run gets a fresh owner-only directory, outside the clone,
  used as both `HOME` and `CODEX_HOME`, holding only a copy of the operator's `auth.json` and the
  run's own `config.toml`. The whole directory is deleted when the run ends.
- The copy may be refreshed by the client, and a refresh may rotate the refresh token away from
  the operator's original. The copy is hashed before and after each run; a change stops the set
  (`AuthRotated`). The source is only ever read, and nothing is written back.
- `--ignore-rules` skips the execpolicy `.rules` files only. Project `AGENTS.md` files are switched
  off in the config (`project_doc_max_bytes = 0`); a clone carrying `.codex/` or `.agents/` is
  refused.
- Without the shell the agent cannot read the clone, so the shell stays. It runs in the client's
  sandbox under the `bench` permissions profile: reads of the minimal system set, `/tmp` and the
  clone, writes in the clone only, the operator's home and the run's own directory denied, and no
  network. In that sandbox `git` does not run and `rg` is not on the path.
- `features.multi_agent = false` is not honored (the subagent tools stay listed). Web search is
  disabled in the config.
- MCP tool calls and their results are in the stream itself: a `mcp_tool_call` item is recorded
  as `mcp__<server>__<tool>`. Bruriah is registered as a required stdio server in the config.
- The client has no flag to append to the system prompt that this adapter uses, and no pre-edit
  gate, so only `baseline` and `unprompted` are run; `prompted` and `gated` are refused.
- The client has no turn limit or timeout flag: the time budget is enforced by stopping the
  client's process group.
- `exec` waits on stdin unless it is closed, so stdin is the null device.

The wire format is `--json`: one JSON object per line, `thread.started` first, then `turn.started`,
`item.started` / `item.completed` for each item (`agent_message`, `reasoning`, `command_execution`,
`mcp_tool_call`, `file_change`, `web_search`, ...), and `turn.completed` with the turn's `usage`,
or `turn.failed` and `error` events.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import bruriah
from bruriah.clients import ClientError, LaunchManifest

from agent_regression.adapters import AdapterError
from agent_regression.antigravity import HOME_MARKER, _count, _object, _owner_alive
from agent_regression.citation import cites_decision
from agent_regression.claude_code import (
    CommandRunner,
    _check_no_remote,
    _kill,
    _tail,
    _windows_essentials,
    bruriah_source,
    ensure_index,
    executable_interpreter,
    mirror_repository,
    model_cache_dir,
    prepare_workdir,
    run_command,
    source_commit,
    workdir_diff,
)
from agent_regression.detection import validate_detection
from agent_regression.runs import BASELINE, CONDITIONS, UNPROMPTED, AgentRun, Provenance, ToolCall
from agent_regression.traps import Trap, load_detector

CLIENT = "codex"
SERVER_NAME = "bruriah"
SUPPORTED_CONDITIONS = (BASELINE, UNPROMPTED)

# Where the operator's login lives, and the files the run's `CODEX_HOME` holds.
AUTH_NAME = "auth.json"
CONFIG_NAME = "config.toml"
HOME_PREFIX = "codex-home-"
PERMISSION_PROFILE = "bench"

# Project configuration the client reads from a repository: config, rules and prompts under
# `.codex/`, skills under `.agents/`. A trap that can steer the client is no trap.
_CLIENT_CONFIGURATION = (".codex", ".agents")

# How a stream item is recorded. `file_change` and `apply_patch` are in `runs.WRITE_TOOLS`.
SHELL_TOOL = "shell"
_ITEM_NAMES = {"command_execution": SHELL_TOOL, "file_change": "file_change", "web_search": "web_search"}
# Item types that are the model's own output, not a tool call.
_NOT_TOOLS = frozenset({"agent_message", "reasoning", "todo_list", "error"})

# Denied in the `bench` profile beside the run's own directory: the operator's home by both
# spellings the sandbox resolves (`/Users` and its firmlink).
_DENIED_ROOTS = ("/Users", "/System/Volumes/Data/Users")
_READ_ROOTS = ("/tmp", "/private/tmp")

_CLIENT_ENV_VARS = ("PATH", "USER", "LOGNAME", "TMPDIR", "LANG")
_VERSION_TIMEOUT_SECONDS = 60
# The file this adapter runs from. Its commit must be the server's: a record states one commit.
HARNESS_SOURCE = Path(__file__).resolve()


class AuthRotated(RuntimeError):
    """Raised when the run's copy of `auth.json` changed during the run: the client refreshed it,
    which may have rotated the operator's refresh token. Not an `AdapterError`, so the driver stops
    the whole set instead of recording one failed run and starting the next."""


@dataclass(frozen=True)
class CodexConfig:
    codex_executable: Path
    bruriah_executable: Path
    model: str
    cache_dir: Path
    transcripts_dir: Path
    provenance_date: str
    # The operator's signed-in `auth.json`, copied into each run's `CODEX_HOME` and never written to.
    auth_source: Path = Path.home() / ".codex" / AUTH_NAME
    # Replaces every trap's time budget for this client when set; `None` keeps the trap's own.
    time_budget_seconds: int | None = None


@dataclass(frozen=True)
class StreamSummary:
    tool_calls: tuple[ToolCall, ...]
    thread_seen: bool
    turn_completed: bool
    failed: bool
    turns: int
    input_tokens: int | None
    output_tokens: int | None
    response: str | None


def _require_supported(condition: str) -> None:
    if condition not in CONDITIONS:
        raise AdapterError(f"unknown condition: {condition!r}")
    if condition not in SUPPORTED_CONDITIONS:
        raise AdapterError(
            f"condition {condition} cannot run on Codex CLI: this adapter appends no instruction to the system "
            f"prompt and has no pre-edit gate for it; supported: {', '.join(SUPPORTED_CONDITIONS)}"
        )


def toml_string(value: str) -> str:
    """`value` as a TOML basic string: quotes, backslashes and control characters escaped."""
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise AdapterError(f"{value!r} is not valid UTF-8 and cannot be written to the client config")
    escaped = []
    for char in value:
        code = ord(char)
        if char in ('"', "\\"):
            escaped.append("\\" + char)
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _bool(value: bool) -> str:
    return "true" if value else "false"


def config_toml(condition: str, home: Path, model: str, server: tuple[str, Sequence[str]] | None) -> str:
    """The run's `config.toml`: no approvals, web search, update check, history or project
    `AGENTS.md`; the `bench` permissions profile; and, for `unprompted` only, Bruriah as a required
    stdio MCP server (`server` is its command and arguments). The run's own directory is denied by
    its resolved path, which is the path the sandbox matches."""
    _require_supported(condition)
    if (server is None) != (condition == BASELINE):
        raise AdapterError(f"condition {condition} registers {'no' if condition == BASELINE else 'one'} MCP server")
    lines = [
        f"model = {toml_string(model)}",
        f"approval_policy = {toml_string('never')}",
        f"web_search = {toml_string('disabled')}",
        "check_for_update_on_startup = false",
        "project_doc_max_bytes = 0",
        f"default_permissions = {toml_string(PERMISSION_PROFILE)}",
        "",
        "[features]",
        "multi_agent = false",
        "apps = false",
        "",
        "[agents]",
        "enabled = false",
        "",
        "[history]",
        f"persistence = {toml_string('none')}",
        "",
        f"[permissions.{PERMISSION_PROFILE}.filesystem]",
        f"{toml_string(':minimal')} = {toml_string('read')}",
    ]
    lines += [f"{toml_string(path)} = {toml_string('deny')}" for path in (*_DENIED_ROOTS, str(home.resolve()))]
    lines += [f"{toml_string(path)} = {toml_string('read')}" for path in _READ_ROOTS]
    lines += [
        "",
        f"[permissions.{PERMISSION_PROFILE}.filesystem.{toml_string(':workspace_roots')}]",
        f"{toml_string('.')} = {toml_string('write')}",
        "",
        f"[permissions.{PERMISSION_PROFILE}.network]",
        f"enabled = {_bool(False)}",
    ]
    if server is not None:
        command, args = server
        lines += [
            "",
            f"[mcp_servers.{SERVER_NAME}]",
            f"command = {toml_string(command)}",
            f"args = [{', '.join(toml_string(arg) for arg in args)}]",
            "required = true",
            "startup_timeout_sec = 60",
            "tool_timeout_sec = 120",
            f"default_tools_approval_mode = {toml_string('approve')}",
        ]
    return "\n".join(lines) + "\n"


def sweep_leftover_homes(root: Path) -> list[Path]:
    """Remove run directories under `root` that this adapter made and whose process is gone: a run
    killed outright never reaches its own cleanup and leaves its auth copy behind. A directory
    without the marker, or whose owner still runs, is left alone. Returns what was removed."""
    removed = []
    for entry in sorted(root.glob(f"{HOME_PREFIX}*")):
        marker = entry / HOME_MARKER
        if entry.is_dir() and not entry.is_symlink() and marker.is_file() and not _owner_alive(marker):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry)
    return removed


def _item_name(item: dict[str, Any]) -> str | None:
    kind = item.get("type")
    if not isinstance(kind, str) or kind in _NOT_TOOLS:
        return None
    if kind == "mcp_tool_call":
        server, tool = item.get("server"), item.get("tool")
        if isinstance(server, str) and isinstance(tool, str):
            return f"mcp__{server}__{tool}"
    return _ITEM_NAMES.get(kind, kind)


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    """Summarize a `--json` transcript. Each tool item counts once, in the order it first appeared,
    and only when it completed. A line that is not a JSON object is skipped; no completed turn
    leaves the token counts unset."""
    first_seen: dict[str, int] = {}
    tools: dict[str, str] = {}
    messages: set[str] = set()
    response: str | None = None
    thread = completed = failed = False
    input_tokens: list[int] = []
    output_tokens: list[int] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread = True
        elif kind in ("item.started", "item.updated", "item.completed"):
            item = _object(event.get("item"))
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            first_seen.setdefault(item_id, len(first_seen))
            if kind != "item.completed":
                continue
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                messages.add(item_id)
                response = item["text"]
                continue
            name = _item_name(item)
            if name is not None and item_id not in tools:
                tools[item_id] = name
        elif kind == "turn.completed":
            completed = True
            usage = _object(event.get("usage"))
            used, produced = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
            if used is not None:
                input_tokens.append(used)
            if produced is not None:
                output_tokens.append(produced)
        elif kind in ("turn.failed", "error"):
            failed = True

    names = [name for _, name in sorted(tools.items(), key=lambda entry: first_seen[entry[0]])]
    return StreamSummary(
        tool_calls=tuple(ToolCall(name=name, ordinal=ordinal) for ordinal, name in enumerate(names)),
        thread_seen=thread,
        turn_completed=completed,
        failed=failed,
        # Each model output: a message or a tool call.
        turns=len(messages) + len(names),
        input_tokens=sum(input_tokens) if input_tokens else None,
        output_tokens=sum(output_tokens) if output_tokens else None,
        response=response,
    )


def exit_reason_for(summary: StreamSummary, timed_out: bool, returncode: int) -> str:
    if timed_out:
        return "time_budget"
    if summary.thread_seen and summary.turn_completed and not summary.failed and returncode == 0:
        return "done"
    return "error"


def _check_no_client_configuration(workdir: Path, trap: Trap) -> None:
    present = [name for name in _CLIENT_CONFIGURATION if (workdir / name).exists()]
    if present:
        raise AdapterError(
            f"trap {trap.trap_id}: the pinned commit carries Codex configuration ({', '.join(present)}); "
            "a repository that configures the client cannot be a trap"
        )


def _client_env(home: Path) -> dict[str, str]:
    """The run's own directory as `HOME` and `CODEX_HOME`, and the few variables the client needs
    to run. Nothing else: no `OPENAI_*` or `CODEX_*` key, no proxy, nothing of the operator's shell."""
    env = {name: os.environ[name] for name in _CLIENT_ENV_VARS if os.environ.get(name)}
    env.update(_windows_essentials())
    env["HOME"] = str(home)
    env["CODEX_HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    return env


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class CodexAdapter:
    """`AgentAdapter` for Codex CLI in `exec` mode, signed in with a copy of the operator's
    `auth.json` in a throwaway `CODEX_HOME`."""

    def __init__(
        self,
        config: CodexConfig,
        *,
        trap_set_digest: str,
        repetitions: int,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = config
        self._trap_set_digest = trap_set_digest
        self._repetitions = repetitions
        self._runner = runner if runner is not None else run_command
        self._client_version: str | None = None
        self._bruriah_checked = False
        self._bruriah_commit: str | None = None

    def client_version(self) -> str:
        """`codex --version`, asked once per adapter, under a throwaway `HOME` and `CODEX_HOME` of
        its own: the version needs no login, and the operator's `~/.codex` is never handed to the
        client."""
        if self._client_version is None:
            executable = self._config.codex_executable
            with tempfile.TemporaryDirectory(prefix="codex-version-home-") as home:
                try:
                    completed = subprocess.run(
                        [str(executable), "--version"],
                        env=_client_env(Path(home)),
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=_VERSION_TIMEOUT_SECONDS,
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise AdapterError(f"{executable} --version did not complete: {error}") from error
            version = completed.stdout.strip()
            if completed.returncode != 0 or not version:
                raise AdapterError(f"{executable} --version failed: {_tail(completed.stderr) or 'no output'}")
            self._client_version = version
        return self._client_version

    def _check_bruriah_version(self) -> None:
        if self._bruriah_checked:
            return
        try:
            completed = subprocess.run(
                [str(self._config.bruriah_executable), "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterError(f"{self._config.bruriah_executable} --version did not run: {error}") from error
        words = completed.stdout.split()
        reported = words[-1] if words else ""
        if completed.returncode != 0 or reported != bruriah.__version__:
            raise AdapterError(
                f"{self._config.bruriah_executable} reports version {reported or 'nothing'!r}, but this harness "
                f"records Bruriah {bruriah.__version__}; run it with the matching executable"
            )
        self._bruriah_checked = True

    def bruriah_commit(self) -> str:
        """The commit the `--bruriah` executable serves from, which must be this harness's own."""
        if self._bruriah_commit is None:
            executable = self._config.bruriah_executable
            source = bruriah_source(executable_interpreter(executable), self._runner)
            server = source_commit(source, self._runner, "the Bruriah source")
            harness = source_commit(HARNESS_SOURCE, self._runner, "the harness source")
            if server != harness:
                raise AdapterError(
                    f"{executable} serves Bruriah from commit {server} ({source}), but this harness runs from "
                    f"commit {harness}; run with the bruriah executable of this checkout"
                )
            self._bruriah_commit = server
        return self._bruriah_commit

    def _server(self, trap: Trap, workdir: Path, bruriah_commit: str) -> tuple[str, tuple[str, ...]]:
        """Bruriah's `serve` command line over the trap's index, refused when the command is not an
        absolute path or an argument carries shell metacharacters."""
        config = self._config
        data_dir, config_dir = ensure_index(
            trap, workdir, config.cache_dir, config.bruriah_executable, bruriah_commit=bruriah_commit
        )
        args = (
            "serve",
            "--data-dir",
            str(data_dir),
            "--config-dir",
            str(config_dir),
            "--cache-dir",
            str(model_cache_dir(config.cache_dir)),
        )
        try:
            LaunchManifest(command=str(config.bruriah_executable), args=args, server_name=SERVER_NAME)
        except ClientError as error:
            raise AdapterError(
                f"cannot register {config.bruriah_executable} as the MCP server: the command must be an absolute "
                f"path and no argument may carry shell metacharacters ({error.code})"
            ) from error
        return str(config.bruriah_executable), args

    def _make_home(self, workdir: Path) -> Path:
        """A fresh owner-only directory in the system temp dir, outside the clone, marked with this
        process's id, with the auth copy (owner read-write only). The config is written later,
        once the condition's server is known."""
        source = self._config.auth_source
        if not source.is_file():
            raise AdapterError(
                f"no Codex login at {source}; sign in with `codex login`, or pass the auth.json the signed-in "
                "client keeps"
            )
        root = Path(tempfile.gettempdir()).resolve()
        sweep_leftover_homes(root)
        home = Path(tempfile.mkdtemp(prefix=HOME_PREFIX, dir=root)).resolve()
        if home.is_relative_to(workdir.resolve()):
            shutil.rmtree(home, ignore_errors=True)
            raise AdapterError(f"the run's CODEX_HOME {home} must be outside the agent's workdir {workdir}")
        try:
            home.chmod(0o700)
            (home / HOME_MARKER).write_text(f"{os.getpid()}\n", encoding="utf-8")
            auth = home / AUTH_NAME
            shutil.copyfile(source, auth)
            auth.chmod(0o600)
        except BaseException as error:
            shutil.rmtree(home, ignore_errors=True)
            if isinstance(error, OSError):
                raise AdapterError(f"cannot prepare the run's HOME {home}: {error}") from error
            raise
        return home

    @staticmethod
    def _write_config(home: Path, condition: str, model: str, server: tuple[str, tuple[str, ...]] | None) -> None:
        try:
            (home / CONFIG_NAME).write_text(config_toml(condition, home, model, server), encoding="utf-8")
        except OSError as error:
            raise AdapterError(f"cannot prepare the run's HOME {home}: {error}") from error

    @staticmethod
    def _remove_home(home: Path) -> None:
        """Delete the run's directory, and fail the run when the auth copy in it survives."""
        shutil.rmtree(home, ignore_errors=True)
        if home.exists():
            raise AdapterError(
                f"the run's CODEX_HOME {home} still holds a copy of the sign-in and could not be removed; "
                "delete it by hand"
            )

    def _invoke(self, argv: list[str], workdir: Path, home: Path, budget: int) -> tuple[str, str, int, bool, float]:
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=workdir,
            env=_client_env(home),
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
        except BaseException:
            # An interrupt must not leave the client, and the server it spawned, running.
            _kill(process)
            raise
        elapsed = time.monotonic() - started
        return stdout or "", stderr or "", process.returncode, timed_out, elapsed

    def _write_transcript(self, trap: Trap, condition: str, repetition: int, stdout: str, stderr: str) -> str:
        name = f"{condition}-{repetition}"
        directory = self._config.transcripts_dir / trap.trap_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.jsonl").write_text(stdout, encoding="utf-8", newline="\n")
        if stderr.strip():
            (directory / f"{name}.stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
        return PurePosixPath(self._config.transcripts_dir.name, trap.trap_id, f"{name}.jsonl").as_posix()

    def _time_budget(self, trap: Trap) -> int:
        return self._config.time_budget_seconds or trap.time_budget_seconds

    def command_line(self, prompt: str, workdir: Path) -> list[str]:
        # No `--ignore-user-config`: the run's own `config.toml` in `CODEX_HOME` must load. And
        # `--strict-config`, so a key a later client stops knowing fails the run instead of being
        # silently ignored (measured on codex-cli 0.157.1: every generated key loads).
        return [
            str(self._config.codex_executable),
            "--strict-config",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            str(workdir.resolve()),
            "-m",
            self._config.model,
            prompt,
        ]

    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
        _require_supported(condition)
        config = self._config
        self._check_bruriah_version()
        bruriah_commit = self.bruriah_commit()

        mirror = mirror_repository(trap, config.cache_dir)
        prepare_workdir(trap, mirror, workdir)
        _check_no_client_configuration(workdir, trap)
        home = self._make_home(workdir)
        try:
            client_version = self.client_version()
            server = self._server(trap, workdir, bruriah_commit) if condition == UNPROMPTED else None
            self._write_config(home, condition, config.model, server)
            auth_before = _digest(home / AUTH_NAME)
            stdout, stderr, returncode, timed_out, elapsed = self._invoke(
                self.command_line(prompt, workdir), workdir, home, self._time_budget(trap)
            )
            auth_after = _digest(home / AUTH_NAME)
        except BaseException:
            # Best effort only: an error removing the home must not hide the one already raised.
            shutil.rmtree(home, ignore_errors=True)
            raise
        # The transcript is written first so a home that cannot be removed still keeps the paid run,
        # and the home is removed even when writing the transcript fails.
        try:
            transcript = self._write_transcript(trap, condition, repetition, stdout, stderr)
        finally:
            self._remove_home(home)
        if auth_after is None or auth_after != auth_before:
            raise AuthRotated(
                f"trap {trap.trap_id}, condition {condition}, repetition {repetition}: the run's copy of the Codex "
                "sign-in changed during the run, so the client refreshed it and may have rotated the operator's "
                "refresh token. The set is stopped; check `codex login status` (sign in again if needed) before "
                f"running more. The run's transcript is kept at {transcript}; its record is not written"
            )

        summary = parse_stream(stdout.splitlines())
        cited = cites_decision(summary.response, trap.citation_cues)
        _check_no_remote(workdir, trap)
        detection = validate_detection(load_detector(trap)(workdir, workdir_diff(workdir)))

        return AgentRun(
            trap_id=trap.trap_id,
            condition=condition,
            repetition=repetition,
            tool_calls=summary.tool_calls,
            turns=summary.turns,
            wall_clock_seconds=round(elapsed, 3),
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            exit_reason=exit_reason_for(summary, timed_out, returncode),
            detection=detection,
            provenance=Provenance(
                date=config.provenance_date,
                # The stream does not name the model; the one asked for is the record.
                model_id=config.model,
                client=CLIENT,
                client_version=client_version,
                bruriah_version=bruriah.__version__,
                trap_set_digest=self._trap_set_digest,
                repetitions=self._repetitions,
                bruriah_commit=bruriah_commit,
            ),
            cost_usd=None,
            transcript=transcript,
            cited_decision=cited,
        )
