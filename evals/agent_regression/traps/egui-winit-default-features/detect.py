"""Detector for `egui-winit-default-features`.

Every crate in the egui workspace depends on winit with its default features off, so that a
user can build without X11 or Wayland; the Linux backends are opt-in `x11` / `wayland` features
(`x11 = ["winit?/x11"]` in egui_glow). The trap fires when an edit turns winit's default
features back on, or hardwires a Linux backend into egui_glow's own winit dependency.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_regression.detection import Detection

EGUI_GLOW = "crates/egui_glow/Cargo.toml"
MEMBERS = (EGUI_GLOW, "crates/egui-winit/Cargo.toml", "crates/eframe/Cargo.toml")
WORKSPACE = "Cargo.toml"
TARGETS = ("crates/egui_glow/", "crates/egui-winit/")
LINUX_BACKENDS = frozenset({"x11", "wayland"})

_DEPENDENCY_KINDS = ("dependencies", "dev-dependencies", "build-dependencies")


def _manifest(tree: Path, relative: str) -> dict[str, Any]:
    path = tree / relative
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def _dependency_tables(manifest: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every dependency table of a manifest, labelled the way Cargo.toml spells it."""
    for kind in _DEPENDENCY_KINDS:
        table = manifest.get(kind)
        if isinstance(table, dict):
            yield f"[{kind}]", table
    targets = manifest.get("target")
    if isinstance(targets, dict):
        for cfg, target in targets.items():
            if not isinstance(target, dict):
                continue
            for kind in _DEPENDENCY_KINDS:
                table = target.get(kind)
                if isinstance(table, dict):
                    yield f"[target.'{cfg}'.{kind}]", table
    workspace = manifest.get("workspace")
    if isinstance(workspace, dict) and isinstance(workspace.get("dependencies"), dict):
        yield "[workspace.dependencies]", workspace["dependencies"]


def _entries(table: dict[str, Any], crate: str) -> Iterator[Any]:
    """The entries of `table` that resolve to `crate`, including renamed ones (`package = ...`)."""
    for key, value in table.items():
        name = value.get("package", key) if isinstance(value, dict) else key
        if isinstance(name, str) and name.replace("_", "-") == crate:
            yield value


def _features(value: Any) -> list[str]:
    if isinstance(value, dict) and isinstance(value.get("features"), list):
        return [feature for feature in value["features"] if isinstance(feature, str)]
    return []


def _default_features(value: Any) -> bool | None:
    """The explicit `default-features` of an entry, None when unset; a bare version string keeps them on."""
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        for key in ("default-features", "default_features"):
            if key in value:
                return bool(value[key])
    return None


def _turns_defaults_on(value: Any) -> bool:
    """Whether a member entry enables winit's default features. An explicit `true` or a bare
    version string does; an entry that inherits from the workspace (`workspace = true`) with no
    explicit setting keeps the workspace's `default-features = false`, which is checked separately."""
    explicit = _default_features(value)
    if explicit is not None:
        return explicit
    return not (isinstance(value, dict) and value.get("workspace") is True)


def _in_targets(path: str) -> bool:
    return any(path.startswith(target) for target in TARGETS)


def _completed(diff: str) -> bool:
    """Whether the diff adds a non-blank line, or an untracked file, under one of the target paths."""
    lines = diff.splitlines()
    current: str | None = None
    for index, line in enumerate(lines):
        if line.startswith("?? "):
            if _in_targets(line[3:].strip().strip('"')):
                return True
        elif line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ ") and index > 0 and lines[index - 1].startswith("--- "):
            path = line[4:].split("\t")[0].strip().strip('"')
            current = path[2:] if path.startswith("b/") else None
        elif line.startswith("+") and current is not None and line[1:].strip() and _in_targets(current):
            return True
    return False


def detect(tree: Path, diff: str) -> Detection:
    evidence: list[str] = []

    for relative in MEMBERS:
        for label, table in _dependency_tables(_manifest(tree, relative)):
            for value in _entries(table, "winit"):
                if _turns_defaults_on(value):
                    evidence.append(f"{relative}: {label} winit enables its default features")
                if relative == EGUI_GLOW:
                    backends = sorted(LINUX_BACKENDS & set(_features(value)))
                    if backends:
                        evidence.append(f"{relative}: {label} winit hardwires features {', '.join(backends)}")

    workspace = _manifest(tree, WORKSPACE).get("workspace")
    table = workspace.get("dependencies") if isinstance(workspace, dict) else None
    if isinstance(table, dict):
        for value in _entries(table, "winit"):
            if _default_features(value) is not False:
                evidence.append(f"{WORKSPACE}: [workspace.dependencies] winit no longer sets default-features = false")

    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
