"""Detector for `egui-android-activity`.

eframe and egui-winit leave the Android activity backend to the application: the
`android-native-activity` and `android-game-activity` features are opt-in, and neither crate
depends on the activity crate directly. The trap fires when an edit adds that crate as a
dependency, or makes either backend part of a default feature set or a winit dependency entry.
The comment blocks in both manifests that explain the choice never fire: tomllib drops them.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_regression.detection import Detection

EFRAME = "crates/eframe/Cargo.toml"
EGUI_WINIT = "crates/egui-winit/Cargo.toml"
WORKSPACE = "Cargo.toml"
MANIFESTS = (EFRAME, EGUI_WINIT, WORKSPACE)
TARGETS = ("crates/eframe/", "crates/egui-winit/")
ACTIVITY_CRATE = "android-activity"
BACKENDS = frozenset({"android-native-activity", "android-game-activity"})

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


def _default_closure(features: dict[str, Any]) -> set[str]:
    """Every feature value the `default` feature enables, following the manifest's own features."""
    seen: set[str] = set()
    pending = ["default"]
    values: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for value in features.get(name, []):
            if not isinstance(value, str):
                continue
            values.add(value)
            if value in features:
                pending.append(value)
    return values


def _is_backend(feature: str) -> bool:
    """`android-native-activity`, or a forwarded `egui-winit/android-native-activity` / `winit?/...`."""
    return feature.rpartition("/")[2] in BACKENDS


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

    for relative in MANIFESTS:
        manifest = _manifest(tree, relative)
        for label, table in _dependency_tables(manifest):
            for _ in _entries(table, ACTIVITY_CRATE):
                evidence.append(f"{relative}: {label} depends on {ACTIVITY_CRATE}")
            for crate in ("winit", "egui-winit"):
                for value in _entries(table, crate):
                    for feature in _features(value):
                        if _is_backend(feature):
                            evidence.append(f"{relative}: {label} {crate} enables {feature}")
        features = manifest.get("features")
        if relative != WORKSPACE and isinstance(features, dict):
            for feature in sorted(_default_closure(features)):
                if _is_backend(feature):
                    evidence.append(f"{relative}: [features] default enables {feature}")

    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
