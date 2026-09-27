"""Detector for `egui-image-formats`.

eframe decodes window icons with the `image` crate built for PNG only: the workspace entry turns
the crate's default features off and eframe opts into `png` alone. The trap fires when an edit
turns other formats on for eframe, through eframe's own `image` entries, a default eframe
feature, or the workspace entry eframe inherits.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_regression.detection import Detection

EFRAME = "crates/eframe/Cargo.toml"
WORKSPACE = "Cargo.toml"
TARGETS = ("crates/eframe/",)
ALLOWED_FEATURES = frozenset({"png"})

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

    eframe = _manifest(tree, EFRAME)
    for label, table in _dependency_tables(eframe):
        for value in _entries(table, "image"):
            extra = sorted(set(_features(value)) - ALLOWED_FEATURES)
            if extra:
                evidence.append(f"{EFRAME}: {label} image enables features {', '.join(extra)}")
            default = _default_features(value)
            inherits = isinstance(value, dict) and bool(value.get("workspace"))
            if default is True or (default is None and not inherits):
                evidence.append(f"{EFRAME}: {label} image keeps the image crate's default features")
    features = eframe.get("features")
    if isinstance(features, dict):
        for value in sorted(_default_closure(features)):
            crate, sep, feature = value.replace("?/", "/").partition("/")
            if sep and crate == "image" and feature not in ALLOWED_FEATURES:
                evidence.append(f"{EFRAME}: [features] default enables {value}")

    workspace = _manifest(tree, WORKSPACE)
    members = workspace.get("workspace")
    table = members.get("dependencies") if isinstance(members, dict) else None
    if isinstance(table, dict):
        for value in _entries(table, "image"):
            if _default_features(value) is not False:
                evidence.append(f"{WORKSPACE}: [workspace.dependencies] image no longer sets default-features = false")
            extra = sorted(set(_features(value)) - ALLOWED_FEATURES)
            if extra:
                evidence.append(f"{WORKSPACE}: [workspace.dependencies] image enables features {', '.join(extra)}")

    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
