# Client launch manifest and adapters (Slice 12B): ONE typed source of truth describing how to
# launch the `bruriah serve` server, from which every supported client's config snippet is
# rendered. See tasks.md task 12.1 and design.md "Clients": "One manifest renders Claude Code,
# OpenCode, Cursor, Gemini, Antigravity and generic stdio snippets. Adapters detect declared
# structured output and host capabilities; core requires only tools/list/tools/call, always
# emits text fallback, stderr-only diagnostics, absolute commands, and no shell quoting."
#
# Scope boundary (deliberate, see tasks.md 12.1 non-goals): this module only RENDERS strings from
# a manifest. It never writes files, never touches the network, never spawns a subprocess, and
# has zero side effects at import. Wiring a renderer into `cli.py`'s `init` command (so `init`
# writes all six client configs to disk) is proposed as a follow-up (12B-2) rather than done here
# -- every existing module, including `cli.py`, stays byte-frozen this slice.
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Callable

from . import __version__ as _ROUTER_VERSION

__all__ = [
    "CLIENT_CAPABILITIES",
    "ClientCapability",
    "ClientError",
    "ClientId",
    "LaunchManifest",
    "StructuredOutputSupport",
    "render",
    "render_all",
    "render_antigravity",
    "render_claude_code",
    "render_cursor",
    "render_generic_stdio",
    "render_gemini",
    "render_opencode",
]


class ClientError(ValueError):
    """Typed failure for malformed manifest input -- the one escape hatch every render entry
    point raises through; no `ValueError`/`KeyError`/etc. from this module is ever untyped."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# Classic shell metacharacters (`;`, `|`, `&`, `$`, backtick, `<`, `>`, parens, newlines, etc.)
# that would let a malicious command/arg/env value break out of a literal argv element if some
# downstream tool ever naively re-interpreted it via a shell. Every renderer in this module emits
# `command`/`args` as a literal JSON argv array -- exec()'d directly by the host, never handed to
# `/bin/sh` -- so word-splitting and shell-metacharacter expansion never happen on the intended
# path; rejecting these characters at construction time is defense in depth against a hypothetical
# downstream shell re-interpretation, not the primary safety mechanism.
#
# A plain space is deliberately NOT in this set. Because the transport is an argv array (not a
# shell string), a space inside an absolute command path is legitimate and common on real
# installs (e.g. macOS `/Users/x/Library/Application Support/cerebro/bruriah`) and never
# triggers word-splitting when passed as one argv element. Rejecting spaces would break valid
# installs for no injection-safety benefit, so this set stays scoped to characters that are
# genuinely dangerous if a config is ever mishandled by a shell, not every character a shell
# grammar happens to treat specially.
#
# `\` gets the same treatment as the space, for the same reason and only where the reason applies.
# On POSIX it is an escape character and never a path separator, so it stays rejected. On Windows
# it is the ONLY path separator: every absolute command (`C:\...\python.exe`) and every directory
# argument (`--data-dir C:\Users\...`) contains one, so keeping it in the set does not harden
# anything -- it rejects every valid Windows install, which is exactly what `bruriah init` did
# there: it could not emit a single client config. Dropping it costs no injection safety, because
# `cmd.exe` does not treat `\` specially and PowerShell escapes with a backtick, which stays in
# the set on every platform.
_SHELL_METACHARACTERS = frozenset(";&|$`()<>{}[]\n\r\t\\\"'*?~!#") - (
    frozenset("\\") if os.name == "nt" else frozenset()
)


def _reject_shell_metacharacters(value: str, *, code: str) -> None:
    if not value or any(character in _SHELL_METACHARACTERS for character in value):
        raise ClientError(code)


def _is_env_pair(entry: object) -> bool:
    """`True` only for a well-shaped `(name, value)` pair of strings -- used to type-validate
    every `env` entry BEFORE unpacking it, so a malformed entry (wrong length, non-tuple, non-str
    members) raises this module's typed `ClientError` instead of an untyped unpacking error."""
    return (
        isinstance(entry, tuple)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
    )


@dataclass(frozen=True)
class LaunchManifest:
    """The one canonical description of how to launch the server. Every renderer below consumes
    only this -- never a hand-authored per-client command -- so all six clients launch the
    identical `bruriah serve` process with the identical two-tool surface.

    `command` MUST be an absolute path (design.md "Clients": "absolute commands"); `args` and
    `env` are additional exec-argv/environment entries (e.g. `--config-dir`). `env` is a tuple of
    `(name, value)` pairs, not a dict, so the manifest stays hashable and its rendering
    deterministic regardless of dict-ordering behavior across callers.

    `router_version` defaults to the installed `bruriah.__version__` and gives every
    manifest version visibility (spec `K-Canonical Client Launch Manifest and Adapters`: "Adapters
    MUST use ... version visibility ..."): any caller holding a `LaunchManifest` can tell which
    router version the config it describes targets, without this module guessing or embedding an
    undocumented extra key inside a client's rendered JSON (see `docs/client-guidance.md`
    "Version visibility" for why it is surfaced at the manifest/API level and in that document,
    not inside the rendered snippets).
    """

    command: str
    args: tuple[str, ...] = ("serve",)
    env: tuple[tuple[str, str], ...] = ()
    server_name: str = "bruriah"
    router_version: str = _ROUTER_VERSION

    def __post_init__(self) -> None:
        # Type validation runs FIRST and exclusively raises this module's own typed `ClientError`
        # -- never an untyped `TypeError`/`AttributeError` escaping from `PurePath(...)` on a
        # `None` command, `.replace(...)` on a `None` server_name, or unpacking a malformed `env`
        # pair. No untyped exception may escape manifest construction.
        if not isinstance(self.command, str):
            raise ClientError("command_not_string")
        if not isinstance(self.args, tuple) or not all(isinstance(argument, str) for argument in self.args):
            raise ClientError("args_invalid_type")
        if not isinstance(self.env, tuple) or not all(_is_env_pair(entry) for entry in self.env):
            raise ClientError("env_invalid_type")
        if not isinstance(self.server_name, str):
            raise ClientError("server_name_not_string")
        if not isinstance(self.router_version, str):
            raise ClientError("router_version_not_string")

        if not PurePath(self.command).is_absolute():
            raise ClientError("command_not_absolute")
        _reject_shell_metacharacters(self.command, code="command_has_shell_metacharacters")
        for argument in self.args:
            _reject_shell_metacharacters(argument, code="arg_has_shell_metacharacters")
        for name, value in self.env:
            _reject_shell_metacharacters(name, code="env_name_has_shell_metacharacters")
            _reject_shell_metacharacters(value, code="env_value_has_shell_metacharacters")
        if not self.server_name or not self.server_name.replace("-", "").isalnum():
            raise ClientError("server_name_invalid")

    @property
    def env_dict(self) -> dict[str, str]:
        return dict(self.env)

    @property
    def full_argv(self) -> list[str]:
        """`command` followed by `args` as a single exec-argv list -- what OpenCode's config
        shape expects inline (see `render_opencode`), and what every renderer ultimately launches
        regardless of how its own JSON shape splits `command` from `args`."""
        return [self.command, *self.args]


class ClientId(str, Enum):
    CLAUDE_CODE = "claude-code"
    OPENCODE = "opencode"
    CURSOR = "cursor"
    GEMINI = "gemini"
    ANTIGRAVITY = "antigravity"
    GENERIC_STDIO = "generic-stdio"


class StructuredOutputSupport(str, Enum):
    """Whether a client is known to consume `structuredContent` as published, or is expected to
    sanitize/drop schema keywords (design.md: "Adapters detect declared structured output and
    host capabilities"). This never changes server behavior -- `mcp_server.py` (Slice 7B) always
    emits both `structuredContent` and the canonical JSON text fallback unconditionally, for
    every client, regardless of this annotation. It exists purely so guidance can tell an
    operator which clients rely on the text fallback today."""

    DETECTED = "detected"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class ClientCapability:
    """A static, non-behavioral annotation describing one supported client -- never a switch
    that changes what the server sends. `config_path_hint` is documentary (see
    `docs/client-guidance.md`); it is not consumed by any renderer."""

    client_id: ClientId
    display_name: str
    config_path_hint: str
    structured_output: StructuredOutputSupport
    note: str


CLIENT_CAPABILITIES: dict[ClientId, ClientCapability] = {
    ClientId.CLAUDE_CODE: ClientCapability(
        ClientId.CLAUDE_CODE, "Claude Code", ".mcp.json (project) or user scope via `claude mcp add`",
        StructuredOutputSupport.DETECTED,
        "Claude Code consumes structuredContent directly; text fallback is unused in practice "
        "but still returned and still valid.",
    ),
    ClientId.OPENCODE: ClientCapability(
        ClientId.OPENCODE, "OpenCode", "opencode.json (project) or ~/.config/opencode/opencode.json",
        StructuredOutputSupport.DEGRADED,
        "OpenCode's local MCP config combines executable and arguments into one `command` array "
        "and does not document structuredContent consumption; core behavior (tools/list, "
        "tools/call, text fallback) is unaffected either way.",
    ),
    ClientId.CURSOR: ClientCapability(
        ClientId.CURSOR, "Cursor", ".cursor/mcp.json (project) or ~/.cursor/mcp.json (global)",
        StructuredOutputSupport.DETECTED,
        "Cursor follows the same mcpServers shape as Claude Code and is expected to consume "
        "structuredContent; text fallback remains available regardless.",
    ),
    ClientId.GEMINI: ClientCapability(
        ClientId.GEMINI, "Gemini CLI", "~/.gemini/settings.json or .gemini/settings.json (project)",
        StructuredOutputSupport.DETECTED,
        "Gemini CLI adopted the mcpServers convention; core two-tool behavior is unaffected by "
        "any schema keyword it does not enforce client-side.",
    ),
    ClientId.ANTIGRAVITY: ClientCapability(
        ClientId.ANTIGRAVITY, "Antigravity", "~/.antigravity/mcp_config.json (best-effort -- verify locally)",
        StructuredOutputSupport.DEGRADED,
        "Antigravity's exact on-disk config path could not be verified from first principles at "
        "authoring time; this renders the standard mcpServers shape as a best-effort starting "
        "point. Confirm the real path and any schema differences against current Antigravity "
        "docs before relying on this snippet -- see docs/client-guidance.md.",
    ),
    ClientId.GENERIC_STDIO: ClientCapability(
        ClientId.GENERIC_STDIO, "Generic stdio MCP client", "host-specific -- consult that host's MCP documentation",
        StructuredOutputSupport.DEGRADED,
        "Assume only tools/list and tools/call (A-Cross-Client Core Equivalence's minimum); the "
        "canonical JSON text fallback is required reading for any host that does not surface "
        "structuredContent.",
    ),
}


def _canonical_json(payload: dict[str, object]) -> str:
    # Deterministic, byte-identical for the same input every call: sorted keys, stable indent,
    # no wall-clock, no set/dict-ordering dependence (payload is built from sorted/tuple sources).
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _mcp_servers_entry(manifest: LaunchManifest) -> dict[str, object]:
    return {"command": manifest.command, "args": list(manifest.args), "env": manifest.env_dict}


def _render_mcp_servers(manifest: LaunchManifest, *, top_key: str) -> str:
    """Shared shape for every client whose config nests `command`/`args`/`env` under a
    server-name key inside a top-level object -- Claude Code, Cursor, and Gemini CLI all publish
    this same `mcpServers`-style structure; only the top-level key name is a parameter here in
    case a client's documented key ever needs to diverge."""
    payload = {top_key: {manifest.server_name: _mcp_servers_entry(manifest)}}
    return _canonical_json(payload)


def render_claude_code(manifest: LaunchManifest) -> str:
    """`.mcp.json` (project) or the equivalent user-scope entry `claude mcp add` would create."""
    return _render_mcp_servers(manifest, top_key="mcpServers")


def render_cursor(manifest: LaunchManifest) -> str:
    """`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global); identical shape to Claude
    Code's `mcpServers` convention."""
    return _render_mcp_servers(manifest, top_key="mcpServers")


def render_gemini(manifest: LaunchManifest) -> str:
    """`~/.gemini/settings.json` (or project-scoped `.gemini/settings.json`); Gemini CLI reuses
    the `mcpServers` convention."""
    return _render_mcp_servers(manifest, top_key="mcpServers")


def render_antigravity(manifest: LaunchManifest) -> str:
    """Best-effort `mcpServers` shape -- see `CLIENT_CAPABILITIES[ClientId.ANTIGRAVITY].note` and
    `docs/client-guidance.md` for the documented uncertainty around Antigravity's exact config
    path and schema; never silently claimed as verified."""
    return _render_mcp_servers(manifest, top_key="mcpServers")


def render_opencode(manifest: LaunchManifest) -> str:
    """`opencode.json` (project) or `~/.config/opencode/opencode.json` (global). OpenCode's local
    MCP entries combine the executable and its arguments into one `command` array and use
    `environment` rather than `env`, so this renderer intentionally diverges in shape from the
    `mcpServers` renderers above while launching the exact same argv (`manifest.full_argv`)."""
    payload = {
        "mcp": {
            manifest.server_name: {
                "type": "local",
                "command": manifest.full_argv,
                "environment": manifest.env_dict,
            }
        }
    }
    return _canonical_json(payload)


def render_generic_stdio(manifest: LaunchManifest) -> str:
    """The canonical MCP stdio server descriptor with no vendor wrapper -- for any spec-compliant
    host not covered by a named renderer above. Any host that only implements `tools/list` and
    `tools/call` (the core the spec requires) can adapt this shape without loss of core
    functionality (A-Cross-Client Core Equivalence)."""
    return _canonical_json(_mcp_servers_entry(manifest))


RENDERERS: dict[ClientId, Callable[[LaunchManifest], str]] = {
    ClientId.CLAUDE_CODE: render_claude_code,
    ClientId.OPENCODE: render_opencode,
    ClientId.CURSOR: render_cursor,
    ClientId.GEMINI: render_gemini,
    ClientId.ANTIGRAVITY: render_antigravity,
    ClientId.GENERIC_STDIO: render_generic_stdio,
}


def render(client_id: ClientId, manifest: LaunchManifest) -> str:
    """The single public render entry point: dispatch to the named client's renderer. Raises the
    typed `ClientError` (never an untyped `KeyError`) for an unknown `client_id`."""
    renderer = RENDERERS.get(client_id)
    if renderer is None:
        raise ClientError("unknown_client")
    return renderer(manifest)


def render_all(manifest: LaunchManifest) -> dict[ClientId, str]:
    """Render every supported client's snippet from the same manifest, in `ClientId` enum
    declaration order -- the basis for `docs/client-guidance.md` and the cross-client-equivalence
    tests."""
    return {client_id: renderer(manifest) for client_id, renderer in RENDERERS.items()}
