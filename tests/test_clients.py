from __future__ import annotations

import json
import os
from pathlib import Path, PurePath

import pytest
import bruriah
from bruriah.clients import (
    _SHELL_METACHARACTERS,
    CLIENT_CAPABILITIES,
    ClientCapability,
    ClientError,
    ClientId,
    LaunchManifest,
    StructuredOutputSupport,
    render,
    render_all,
    render_antigravity,
    render_claude_code,
    render_cursor,
    render_generic_stdio,
    render_gemini,
    render_opencode,
)

# "Absolute" is spelled differently per platform and the manifest requires it: `PurePath` is
# `PureWindowsPath` on Windows, where a driveless "/usr/local/bin/bruriah" is ROOTED but not
# ABSOLUTE. Hardcoding POSIX paths made this whole module fail to even import on Windows, so the
# contract went unasserted there rather than being asserted and found wanting. Building the
# fixtures for the running platform means the same contract is checked everywhere.
_WINDOWS = os.name == "nt"
_PREFIX = "C:\\opt\\bruriah\\" if _WINDOWS else "/usr/local/bin/"
_COMMAND = f"{_PREFIX}bruriah"
_CONFIG_DIR = "C:\\Users\\u\\AppData\\Roaming\\cerebro" if _WINDOWS else "/home/u/.config/cerebro"
_MANIFEST = LaunchManifest(command=_COMMAND, args=("serve", "--config-dir", _CONFIG_DIR))
# A space in an absolute command path is legitimate and common on both platforms, and is deliberately
# NOT a rejected character -- see `_SHELL_METACHARACTERS`.
_SPACED_COMMAND = (
    "C:\\Program Files\\cerebro\\bruriah" if _WINDOWS
    else "/Users/x/Library/Application Support/cerebro/bruriah"
)

_MCP_SERVERS_RENDERERS = {
    ClientId.CLAUDE_CODE: render_claude_code,
    ClientId.CURSOR: render_cursor,
    ClientId.GEMINI: render_gemini,
    ClientId.ANTIGRAVITY: render_antigravity,
}


# --- LaunchManifest construction: negative controls -----------------------------------------


def test_manifest_rejects_relative_command() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command="bruriah")
    assert excinfo.value.code == "command_not_absolute"


@pytest.mark.parametrize("bad_command", [
    # Built on the platform prefix so the ABSOLUTE check passes and the metacharacter check
    # is what actually rejects these -- otherwise Windows would fail them for the wrong reason.
    f"{_PREFIX}bruriah; rm -rf /", f"{_PREFIX}$(whoami)", f"{_PREFIX}`id`",
])
def test_manifest_rejects_shell_metacharacters_in_command(bad_command: str) -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=bad_command)
    assert excinfo.value.code == "command_has_shell_metacharacters"


def test_manifest_rejects_shell_metacharacters_in_args() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, args=("serve", "&& curl evil.example"))
    assert excinfo.value.code == "arg_has_shell_metacharacters"


def test_manifest_rejects_shell_metacharacters_in_env() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, env=(("PATH", "/bin:$HOME"),))
    assert excinfo.value.code == "env_value_has_shell_metacharacters"


def test_manifest_rejects_empty_server_name() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, server_name="")
    assert excinfo.value.code == "server_name_invalid"


def test_manifest_is_frozen_and_hashable() -> None:
    with pytest.raises(AttributeError):
        _MANIFEST.command = "/other"  # type: ignore[misc]
    hash(_MANIFEST)  # must not raise


# --- LaunchManifest construction: type-validation negative controls (no untyped exception may --
# --- escape manifest construction; every malformed input raises the module's own ClientError) --


def test_manifest_rejects_none_command_with_typed_error_not_type_error() -> None:
    # Regression: LaunchManifest(command=None) used to reach `PurePath(None)` and raise an
    # untyped `TypeError`, contradicting this module's typed-total `ClientError` guarantee.
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=None)  # type: ignore[arg-type]
    assert excinfo.value.code == "command_not_string"


@pytest.mark.parametrize("bad_command", [None, 123, ["/usr/local/bin/bruriah"], 1.5])
def test_manifest_rejects_non_string_command_with_typed_error(bad_command: object) -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=bad_command)  # type: ignore[arg-type]
    assert excinfo.value.code == "command_not_string"


def test_manifest_rejects_non_tuple_args_with_typed_error() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, args=["serve"])  # type: ignore[arg-type]
    assert excinfo.value.code == "args_invalid_type"


def test_manifest_rejects_args_with_non_string_element_with_typed_error() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, args=("serve", 123))  # type: ignore[arg-type]
    assert excinfo.value.code == "args_invalid_type"


def test_manifest_rejects_non_tuple_env_with_typed_error() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, env={"PATH": "/bin"})  # type: ignore[arg-type]
    assert excinfo.value.code == "env_invalid_type"


@pytest.mark.parametrize(
    "bad_env",
    [
        (("PATH",),),  # wrong shape: not a 2-tuple
        (("PATH", "/bin", "extra"),),  # wrong shape: not a 2-tuple
        ((None, "/bin"),),  # non-str name
        (("PATH", None),),  # non-str value
        ("PATH=/bin",),  # not a tuple entry at all
    ],
)
def test_manifest_rejects_malformed_env_entry_with_typed_error(bad_env: object) -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, env=bad_env)  # type: ignore[arg-type]
    assert excinfo.value.code == "env_invalid_type"


def test_manifest_rejects_non_string_server_name_with_typed_error() -> None:
    # Regression: a None server_name used to reach `.replace(...)` and raise an untyped
    # `AttributeError` instead of this module's typed `ClientError`.
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, server_name=None)  # type: ignore[arg-type]
    assert excinfo.value.code == "server_name_not_string"


def test_manifest_rejects_non_string_router_version_with_typed_error() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_COMMAND, router_version=1)  # type: ignore[arg-type]
    assert excinfo.value.code == "router_version_not_string"


# --- Space in an absolute command path: legitimate (argv array transport, never a shell) ----


def test_manifest_allows_space_in_absolute_command_path() -> None:
    # A space is legitimate inside an absolute path (e.g. macOS "Application Support") and is
    # intentionally NOT treated as a shell metacharacter: the transport is a literal JSON argv
    # array, never a shell string, so word-splitting on the space never happens.
    manifest = LaunchManifest(command=_SPACED_COMMAND)
    assert manifest.command == _SPACED_COMMAND


def test_manifest_still_rejects_genuine_shell_injection_metacharacters_alongside_spaced_path() -> None:
    with pytest.raises(ClientError) as excinfo:
        LaunchManifest(command=_SPACED_COMMAND + "; rm -rf /")
    assert excinfo.value.code == "command_has_shell_metacharacters"


@pytest.mark.parametrize("client_id", list(ClientId))
def test_render_renders_spaced_absolute_command_path_as_single_argv_element(client_id: ClientId) -> None:
    manifest = LaunchManifest(command=_SPACED_COMMAND)
    rendered = render(client_id, manifest)
    parsed = json.loads(rendered)
    argv = _extract_argv(client_id, parsed)
    # The argv array transports the space-containing path as one literal element -- never
    # shell-split -- because every renderer emits a JSON argv array/object, not a shell string.
    assert argv[0] == _SPACED_COMMAND
    assert " " in argv[0]


# --- Version visibility (spec K-Canonical Client Launch Manifest and Adapters) --------------


def test_manifest_exposes_router_version_defaulting_to_package_version() -> None:
    manifest = LaunchManifest(command=_COMMAND)
    assert manifest.router_version == bruriah.__version__


def test_manifest_router_version_is_overridable_and_still_typed() -> None:
    manifest = LaunchManifest(command=_COMMAND, router_version="9.9.9")
    assert manifest.router_version == "9.9.9"


def test_docs_document_version_visibility() -> None:
    doc_path = Path(__file__).resolve().parent.parent / "docs" / "client-guidance.md"
    content = doc_path.read_text(encoding="utf-8")
    assert "Version visibility" in content
    assert "router_version" in content


# --- Deterministic explicit invocation: every renderer matches the manifest verbatim --------


@pytest.mark.parametrize("client_id", list(ClientId))
def test_render_invokes_exact_canonical_command_and_args(client_id: ClientId) -> None:
    rendered = render(client_id, _MANIFEST)
    parsed = json.loads(rendered)
    argv = _extract_argv(client_id, parsed)
    assert argv == list(_MANIFEST.full_argv)
    assert argv[0] == _COMMAND
    # Absoluteness is the property; a leading "/" was only ever how POSIX spells it.
    assert PurePath(argv[0]).is_absolute()


def test_render_is_deterministic_byte_identical_across_calls() -> None:
    for client_id in ClientId:
        first = render(client_id, _MANIFEST)
        second = render(client_id, _MANIFEST)
        assert first == second


def test_render_all_covers_every_client_id() -> None:
    rendered = render_all(_MANIFEST)
    assert set(rendered) == set(ClientId)
    for client_id, text in rendered.items():
        assert text == render(client_id, _MANIFEST)


def test_render_unknown_client_id_raises_typed_error() -> None:
    with pytest.raises(ClientError) as excinfo:
        render("not-a-real-client", _MANIFEST)  # type: ignore[arg-type]
    assert excinfo.value.code == "unknown_client"


# --- Negative control: no rendered config launches anything but the canonical server --------


@pytest.mark.parametrize("client_id", list(ClientId))
def test_render_never_launches_arbitrary_command(client_id: ClientId) -> None:
    rendered = render(client_id, _MANIFEST)
    parsed = json.loads(rendered)
    argv = _extract_argv(client_id, parsed)
    assert argv[0] == _MANIFEST.command
    assert argv[1:] == list(_MANIFEST.args)
    # Asserted against the module's OWN set rather than a copy of it. The copy that used to live
    # here had drifted: it still contained "\\", which is a shell escape on POSIX but the path
    # separator on Windows, so this negative control rejected every legitimate Windows command.
    # Importing the real set means the guarantee under test can never disagree with the guarantee
    # being enforced.
    for token in argv:
        assert not any(character in _SHELL_METACHARACTERS for character in token)


# --- Cross-client core equivalence: same manifest -> identical launched server --------------


def test_cross_client_core_equivalence_same_argv_across_all_six() -> None:
    rendered = render_all(_MANIFEST)
    argvs = {client_id: _extract_argv(client_id, json.loads(text)) for client_id, text in rendered.items()}
    unique_argvs = {tuple(argv) for argv in argvs.values()}
    assert unique_argvs == {tuple(_MANIFEST.full_argv)}


def test_cross_client_core_equivalence_same_env_across_all_six() -> None:
    manifest = LaunchManifest(command=_COMMAND, env=(("BRURIAH_NETWORK_ENABLED", "false"),))
    rendered = render_all(manifest)
    envs = {client_id: _extract_env(client_id, json.loads(text)) for client_id, text in rendered.items()}
    assert all(env == manifest.env_dict for env in envs.values())


def test_cross_client_equivalence_only_wrapping_format_differs() -> None:
    rendered = render_all(_MANIFEST)
    top_level_keys = {client_id: frozenset(json.loads(text)) for client_id, text in rendered.items()}
    # mcpServers-shaped clients share the exact same top-level key; OpenCode and generic diverge
    # by design (documented in the module and docs/client-guidance.md), never by accident.
    mcp_servers_keys = {top_level_keys[client_id] for client_id in _MCP_SERVERS_RENDERERS}
    assert mcp_servers_keys == {frozenset(["mcpServers"])}
    assert top_level_keys[ClientId.OPENCODE] == frozenset(["mcp"])
    assert top_level_keys[ClientId.GENERIC_STDIO] == frozenset(["command", "args", "env"])


# --- Rendered JSON is valid and round-trips --------------------------------------------------


@pytest.mark.parametrize("client_id", list(ClientId))
def test_rendered_output_is_valid_json_and_round_trips(client_id: ClientId) -> None:
    rendered = render(client_id, _MANIFEST)
    parsed = json.loads(rendered)
    reserialized = json.dumps(parsed, indent=2, sort_keys=True) + "\n"
    assert reserialized == rendered


# --- Structured-output degradation annotation -------------------------------------------------


def test_every_client_id_has_a_capability_entry() -> None:
    assert set(CLIENT_CAPABILITIES) == set(ClientId)
    for client_id, capability in CLIENT_CAPABILITIES.items():
        assert isinstance(capability, ClientCapability)
        assert capability.client_id == client_id
        assert capability.structured_output in StructuredOutputSupport
        assert capability.note


def test_capability_annotation_never_alters_rendered_output() -> None:
    # The capability descriptor is documentary only -- verifying it never leaks into or changes
    # the rendered JSON, which must depend on the manifest alone (design.md: never a behavior
    # change to the server).
    for client_id in ClientId:
        rendered_before = render(client_id, _MANIFEST)
        _ = CLIENT_CAPABILITIES[client_id].note
        rendered_after = render(client_id, _MANIFEST)
        assert rendered_before == rendered_after


def test_text_fallback_always_available_regardless_of_structured_output_support() -> None:
    degraded = [c for c, cap in CLIENT_CAPABILITIES.items() if cap.structured_output == StructuredOutputSupport.DEGRADED]
    detected = [c for c, cap in CLIENT_CAPABILITIES.items() if cap.structured_output == StructuredOutputSupport.DETECTED]
    assert degraded and detected  # both states are represented among the six clients
    for client_id in [*degraded, *detected]:
        # Every renderer produces valid JSON regardless of the client's structured-output
        # support -- there is no separate "fallback" render path because the server-side
        # fallback (mcp_server.py) is unconditional, not something the manifest renders.
        assert json.loads(render(client_id, _MANIFEST))


# --- helpers -----------------------------------------------------------------------------------


def _extract_argv(client_id: ClientId, parsed: dict) -> list[str]:
    if client_id == ClientId.OPENCODE:
        entry = parsed["mcp"][_MANIFEST.server_name]
        assert entry["type"] == "local"
        return list(entry["command"])
    if client_id == ClientId.GENERIC_STDIO:
        return [parsed["command"], *parsed["args"]]
    entry = parsed["mcpServers"][_MANIFEST.server_name]
    return [entry["command"], *entry["args"]]


def _extract_env(client_id: ClientId, parsed: dict) -> dict[str, str]:
    if client_id == ClientId.OPENCODE:
        return dict(parsed["mcp"][_MANIFEST.server_name]["environment"])
    if client_id == ClientId.GENERIC_STDIO:
        return dict(parsed["env"])
    return dict(parsed["mcpServers"][_MANIFEST.server_name]["env"])
