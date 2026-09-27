"""Detector for `egui-datepicker-chrono`.

egui_extras' `DatePickerButton` works on `jiff::civil::Date`, and the crate carries no second
date library. The trap fires when an edit adds that second library as a dependency of
egui_extras or of the workspace, or wires it into a feature with `dep:chrono`.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_regression.detection import Detection

EXTRAS = "crates/egui_extras/Cargo.toml"
WORKSPACE = "Cargo.toml"
TARGETS = ("crates/egui_extras/",)
CRATE = "chrono"

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

    for relative in (EXTRAS, WORKSPACE):
        manifest = _manifest(tree, relative)
        for label, table in _dependency_tables(manifest):
            for _ in _entries(table, CRATE):
                evidence.append(f"{relative}: {label} depends on {CRATE}")
        features = manifest.get("features")
        if isinstance(features, dict):
            for name, values in features.items():
                if isinstance(values, list) and f"dep:{CRATE}" in values:
                    evidence.append(f"{relative}: [features] {name} enables dep:{CRATE}")

    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
