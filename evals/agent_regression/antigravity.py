"""The Antigravity CLI (`agy`) headless adapter: one `agy --print` run per trap, condition and repetition.

Measured on `agy` 1.3.5 before writing this (`odd/tasks/antigravity-adapter.md`, L3-L8):

- The client reads its configuration, MCP servers, rules, skills, hooks and memory from under
  `HOME`. Isolation therefore comes from a fresh owner-only `HOME` per run, outside the clone,
  holding only a copy of the operator's OAuth token and the run's own `settings.json`; the whole
  directory is deleted when the run ends. The operator's own `HOME` is never read by the client.
- The token copy may be refreshed in place by the client; the source token is only ever read.
- Permissions are rules in `settings.json`. `deny` hard-blocks a call and the agent continues;
  any call neither allowed nor denied is auto-denied in print mode and ends the turn with no
  final message, so every legitimate path is allowed explicitly. Rules match resolved paths
  (macOS `/tmp` is `/private/tmp`), so every path is resolved.
- There is no shell (`command(*)`) and no URL access (`read_url(*)`, `execute_url(*)`) in any
  condition. Web search cannot be denied by any mechanism found; it stays on, as the client's
  default, and every `search_web` call is in the record (owner decision, L7).
- MCP tools reach the agent through one generic `call_mcp_tool`. The client writes each server's
  instructions to `mcp/<server>/instructions.md` and each tool's schema to `mcp/<server>/<tool>.json`
  under `HOME`, and the agent reads them before calling: tool loading is deferred and Bruriah's
  `anthropic/alwaysLoad` meta has no effect here. A `call_mcp_tool` step is recorded as
  `mcp__<server>__<tool>`, the name the consult metric recognizes.
- The client has no flag to append to the system prompt, and its `PreToolUse` hook did not run in
  print mode, so only `baseline` and `unprompted` are run; `prompted` and `gated` are refused.
- The client has no turn limit flag: only the time budget is enforced.

The wire format is `--output-format stream-json`: one JSON object per line, an `init` event
first, `step_update` events for user input, agent responses (with per-step usage) and tool steps
(each emitted when active and again in its final state), and a final `result` event with the
status, total usage, any auto-denied actions and the final `response`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import bruriah
from bruriah.clients import ClientError, LaunchManifest

from agent_regression.adapters import AdapterError
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

CLIENT = "antigravity"
SERVER_NAME = "bruriah"
SUPPORTED_CONDITIONS = (BASELINE, UNPROMPTED)

# Denied in every condition: no shell, and no URL fetch or execution.
DENIED_PERMISSIONS: tuple[str, ...] = ("command(*)", "read_url(*)", "execute_url(*)")

# Where the operator's login lives, and where the run's `HOME` must hold its copy.
TOKEN_RELATIVE = Path(".gemini") / "antigravity-cli" / "antigravity-oauth-token"
_SETTINGS_RELATIVE = Path(".gemini") / "antigravity-cli" / "settings.json"
_MCP_FILES_RELATIVE = Path(".gemini") / "antigravity-cli" / "mcp"

# Project configuration the client reads from a repository: hooks, skills and workflows under
# `.agents/`, settings and MCP servers under `.gemini/`. A trap that can steer the client is no trap.
_CLIENT_CONFIGURATION = (".agents", ".gemini")

# The client's file-writing tools, as `runs.WRITE_TOOLS` lists them for the consult window.
_MCP_CALL = "call_mcp_tool"
_CLIENT_ENV_VARS = ("PATH", "USER", "LOGNAME", "TMPDIR", "LANG")
_VERSION_TIMEOUT_SECONDS = 60
# The file this adapter runs from. Its commit must be the server's: a record states one commit.
HARNESS_SOURCE = Path(__file__).resolve()


@dataclass(frozen=True)
class AntigravityConfig:
    agy_executable: Path
    bruriah_executable: Path
    model: str
    cache_dir: Path
    transcripts_dir: Path
    provenance_date: str
    # The operator's signed-in token, copied into each run's `HOME` and never written to.
    token_source: Path = Path.home() / TOKEN_RELATIVE


@dataclass(frozen=True)
class StreamSummary:
    tool_calls: tuple[ToolCall, ...]
    model: str | None
    init_seen: bool
    turns: int
    input_tokens: int | None
    output_tokens: int | None
    status: str | None
    denied_actions: int
    response: str | None


def _require_supported(condition: str) -> None:
    if condition not in CONDITIONS:
        raise AdapterError(f"unknown condition: {condition!r}")
    if condition not in SUPPORTED_CONDITIONS:
        raise AdapterError(
            f"condition {condition} cannot run on Antigravity CLI: it has no flag to append to the system prompt "
            f"and its PreToolUse hook does not run in print mode; supported: {', '.join(SUPPORTED_CONDITIONS)}"
        )


def settings_for(condition: str, workdir: Path, home: Path) -> dict[str, Any]:
    """The run's `settings.json`: read and write inside the clone, no shell or URL access, and the
    MCP tools of Bruriah only where it is registered, with read access to the instruction and
    schema files the client writes for it under `home`."""
    _require_supported(condition)
    work = workdir.resolve()
    allow = [f"read_file({work})", f"write_file({work})"]
    deny = list(DENIED_PERMISSIONS)
    if condition == BASELINE:
        deny.append("mcp(*)")
    else:
        allow += [f"read_file({home.resolve() / _MCP_FILES_RELATIVE})", f"mcp({SERVER_NAME}/*)"]
    return {"permissions": {"allow": allow, "deny": deny}}


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _tool_name(step: dict[str, Any]) -> str | None:
    name = step.get("tool_name")
    if not isinstance(name, str):
        return None
    if name == _MCP_CALL:
        parameters = _object(_object(step.get("tool_info")).get("parameters"))
        server, tool = parameters.get("ServerName"), parameters.get("ToolName")
        if isinstance(server, str) and isinstance(tool, str):
            return f"mcp__{server}__{tool}"
    return name


def parse_stream(lines: Iterable[str]) -> StreamSummary:
    """Summarize a stream-json transcript. Each tool step counts once, on its final state, in step
    order. A line that is not a JSON object is skipped; a missing result leaves its fields unset."""
    tools: dict[int, str] = {}
    responses: set[int] = set()
    init: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("event")
        if kind == "init" and init is None:
            init = _object(event.get("init"))
        elif kind == "step_update":
            step = _object(event.get("step_update"))
            index = _count(step.get("step_index"))
            if index is None:
                continue
            if step.get("step_type") == "agent_response":
                responses.add(index)
            elif step.get("step_type") == "tool" and step.get("state") in ("DONE", "ERROR"):
                name = _tool_name(step)
                if name is not None:
                    tools[index] = name
        elif kind == "result":
            result = _object(event.get("result"))

    input_tokens = output_tokens = None
    status = response = None
    denied = 0
    if result is not None:
        usage = _object(result.get("usage"))
        input_tokens = _count(usage.get("input_tokens"))
        produced = [_count(usage.get(key)) for key in ("output_tokens", "thinking_tokens")]
        present = [count for count in produced if count is not None]
        output_tokens = sum(present) if present else None
        status = result["status"] if isinstance(result.get("status"), str) else None
        response = result["response"] if isinstance(result.get("response"), str) else None
        actions = result.get("denied_actions")
        denied = len(actions) if isinstance(actions, list) else 0

    calls = tuple(ToolCall(name=name, ordinal=ordinal) for ordinal, (_, name) in enumerate(sorted(tools.items())))
    model = init.get("model") if init is not None and isinstance(init.get("model"), str) else None
    return StreamSummary(
        tool_calls=calls,
        model=model,
        init_seen=init is not None,
        turns=len(responses),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        status=status,
        denied_actions=denied,
        response=response,
    )


def exit_reason_for(summary: StreamSummary, timed_out: bool, returncode: int) -> str:
    if timed_out:
        return "time_budget"
    # An action auto-denied in print mode ends the turn early: the agent did not finish its task.
    if summary.init_seen and summary.status == "SUCCESS" and summary.denied_actions == 0 and returncode == 0:
        return "done"
    return "error"


def _check_no_client_configuration(workdir: Path, trap: Trap) -> None:
    present = [name for name in _CLIENT_CONFIGURATION if (workdir / name).exists()]
    if present:
        raise AdapterError(
            f"trap {trap.trap_id}: the pinned commit carries Antigravity configuration ({', '.join(present)}); "
            "a repository that configures the client cannot be a trap"
        )


def _client_env(home: Path) -> dict[str, str]:
    """The run's own `HOME` and the few variables the client needs to run. Nothing else: no
    `GEMINI_*` or `GOOGLE_*` key, no proxy, nothing of the operator's shell."""
    env = {name: os.environ[name] for name in _CLIENT_ENV_VARS if os.environ.get(name)}
    env.update(_windows_essentials())
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    return env


def _registered_servers(listing: str) -> list[str]:
    """Server names in `agy mcp list` output: one table row per server after a header row."""
    lines = [line for line in listing.splitlines() if line.strip()]
    if not lines or lines[0].startswith("No MCP servers"):
        return []
    return sorted(line.split()[0] for line in lines[1:])


class AntigravityAdapter:
    """`AgentAdapter` for Antigravity CLI in print mode, signed in with a copy of the operator's
    token in a throwaway `HOME`."""

    def __init__(
        self,
        config: AntigravityConfig,
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

    def _agy(self, home: Path, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [str(self._config.agy_executable), *args],
                env=_client_env(home),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AdapterError(f"agy {' '.join(args[:2])} did not complete: {error}") from error

    def client_version(self) -> str:
        """`agy --version`, asked once per adapter, under a throwaway `HOME` of its own: the version
        needs no login, and the operator's `HOME` is never handed to the client."""
        if self._client_version is None:
            with tempfile.TemporaryDirectory(prefix="agy-version-home-") as home:
                completed = self._agy(Path(home), "--version")
            version = completed.stdout.strip()
            if completed.returncode != 0 or not version:
                raise AdapterError(
                    f"{self._config.agy_executable} --version failed: {_tail(completed.stderr) or 'no output'}"
                )
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

    def _make_home(self, condition: str, workdir: Path) -> Path:
        """A fresh owner-only `HOME` under the cache, outside the clone, with the token copy and
        the condition's settings."""
        source = self._config.token_source
        if not source.is_file():
            raise AdapterError(
                f"no Antigravity login at {source}; sign in by running `agy` once, or pass the token file the "
                "signed-in client keeps"
            )
        root = self._config.cache_dir / "homes"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = Path(tempfile.mkdtemp(prefix="agy-home-", dir=root)).resolve()
        if home.is_relative_to(workdir.resolve()):
            shutil.rmtree(home, ignore_errors=True)
            raise AdapterError(f"the run's HOME {home} must be outside the agent's workdir {workdir}")
        # Every step from here can leave a token copy behind, so any failure removes the home.
        try:
            token = home / TOKEN_RELATIVE
            token.parent.mkdir(mode=0o700, parents=True)
            shutil.copyfile(source, token)
            token.chmod(0o600)
            settings = home / _SETTINGS_RELATIVE
            settings.write_text(json.dumps(settings_for(condition, workdir, home), indent=2) + "\n", encoding="utf-8")
        except BaseException as error:
            shutil.rmtree(home, ignore_errors=True)
            if isinstance(error, OSError):
                raise AdapterError(f"cannot prepare the run's HOME {home}: {error}") from error
            raise
        return home

    @staticmethod
    def _remove_home(home: Path) -> None:
        """Delete the run's `HOME`, and fail the run when the token copy in it survives."""
        shutil.rmtree(home, ignore_errors=True)
        if home.exists():
            raise AdapterError(
                f"the run's HOME {home} still holds a copy of the sign-in token and could not be removed; "
                "delete it by hand"
            )

    def _register_bruriah(self, home: Path, trap: Trap, workdir: Path, bruriah_commit: str) -> None:
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
        completed = self._agy(home, "mcp", "add", SERVER_NAME, str(config.bruriah_executable), *args)
        if completed.returncode != 0:
            raise AdapterError(f"agy mcp add {SERVER_NAME} failed: {_tail(completed.stderr) or 'no output'}")

    def _check_mcp_servers(self, home: Path, condition: str, trap: Trap) -> None:
        """The run's `HOME` registers exactly the condition's servers. Checked before the agent
        starts, because the client's own init event does not list them."""
        completed = self._agy(home, "mcp", "list")
        if completed.returncode != 0:
            raise AdapterError(f"agy mcp list failed: {_tail(completed.stderr) or 'no output'}")
        names = _registered_servers(completed.stdout)
        expected = [] if condition == BASELINE else [SERVER_NAME]
        if names != expected:
            raise AdapterError(
                f"trap {trap.trap_id}, condition {condition}: the client would load MCP servers {names}, expected "
                f"{expected}; the run is not valid for this condition"
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

    def command_line(self, prompt: str, trap: Trap) -> list[str]:
        return [
            str(self._config.agy_executable),
            "--print",
            prompt,
            "--model",
            self._config.model,
            "--output-format",
            "stream-json",
            "--mode",
            "accept-edits",
            "--disable-slash-commands",
            "--print-timeout",
            f"{trap.time_budget_seconds}s",
        ]

    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
        _require_supported(condition)
        config = self._config
        self._check_bruriah_version()
        bruriah_commit = self.bruriah_commit()

        mirror = mirror_repository(trap, config.cache_dir)
        prepare_workdir(trap, mirror, workdir)
        _check_no_client_configuration(workdir, trap)
        home = self._make_home(condition, workdir)
        try:
            client_version = self.client_version()
            if condition != BASELINE:
                self._register_bruriah(home, trap, workdir, bruriah_commit)
            self._check_mcp_servers(home, condition, trap)
            stdout, stderr, returncode, timed_out, elapsed = self._invoke(
                self.command_line(prompt, trap), workdir, home, trap.time_budget_seconds
            )
        except BaseException:
            # Best effort only: an error removing the home must not hide the one already raised.
            shutil.rmtree(home, ignore_errors=True)
            raise
        self._remove_home(home)

        transcript = self._write_transcript(trap, condition, repetition, stdout, stderr)
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
                model_id=summary.model or config.model,
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
