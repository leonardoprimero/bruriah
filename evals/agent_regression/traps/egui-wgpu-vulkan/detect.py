"""Detector for `egui-wgpu-vulkan`.

The workspace depends on wgpu with its default features off, and eframe's `wgpu` feature
forwards to `egui-wgpu/default`, which leaves backend selection to wgpu's defaults and to the
application. The trap fires when an edit pins the Vulkan or DX12 backend: in the features of a
`wgpu` dependency entry, or in a feature list of eframe or egui-wgpu. eframe's doc comment that
shows `features = ["dx12", "metal", "webgl"]` never fires: tomllib drops comments.
A manifest that is present but does not parse makes the run indeterminate, never clean, unless a
manifest that did parse proves the regression; its note then follows that evidence.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_regression.detection import Detection

EFRAME = "crates/eframe/Cargo.toml"
EGUI_WGPU = "crates/egui-wgpu/Cargo.toml"
WORKSPACE = "Cargo.toml"
TARGETS = ("crates/eframe/", "crates/egui-wgpu/")
BACKENDS = frozenset({"vulkan", "dx12"})
FEATURE_VALUES = frozenset({"vulkan", "dx12", "wgpu/vulkan", "wgpu/dx12"})

_DEPENDENCY_KINDS = ("dependencies", "dev-dependencies", "build-dependencies")


def _manifest(tree: Path, relative: str) -> dict[str, Any]:
    """The parsed manifest, `{}` when absent; raises `TOMLDecodeError` or `UnicodeDecodeError`."""
    path = tree / relative
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


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
    unparseable: list[str] = []

    for relative in (EFRAME, EGUI_WGPU, WORKSPACE):
        try:
            manifest = _manifest(tree, relative)
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            unparseable.append(f"{relative}: does not parse as TOML ({error})")
            continue
        for label, table in _dependency_tables(manifest):
            for value in _entries(table, "wgpu"):
                backends = sorted(BACKENDS & set(_features(value)))
                if backends:
                    evidence.append(f"{relative}: {label} wgpu enables features {', '.join(backends)}")
        features = manifest.get("features")
        if relative != WORKSPACE and isinstance(features, dict):
            for name, values in features.items():
                if not isinstance(values, list):
                    continue
                for value in values:
                    if isinstance(value, str) and value.replace("?/", "/") in FEATURE_VALUES:
                        evidence.append(f"{relative}: [features] {name} enables {value}")

    if unparseable and not evidence:
        return Detection(regressed=False, evidence=tuple(unparseable), completed=_completed(diff), indeterminate=True)
    return Detection(regressed=bool(evidence), evidence=(*evidence, *unparseable), completed=_completed(diff))
