"""Fires when a version LeakCanary pins low on purpose changes in `gradle/libs.versions.toml`.

These AndroidX artifacts are compile or runtime dependencies of published modules: raising one
raises the floor for every app that depends on LeakCanary. Test-only entries such as
`androidX-fragment-test`, Espresso or the AndroidX JUnit extension are free to move.
A catalog that is present but does not parse makes the run indeterminate, never clean.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from agent_regression.detection import Detection

CATALOG = "gradle/libs.versions.toml"
TARGETS = (CATALOG,)

PINNED_LIBRARIES = {
    "androidX-fragment": "1.0.0",
    "androidX-startup": "1.0.0",
    "androidX-test-runner": "1.5.2",
    "androidX-test-uiautomator": "2.2.0",
    "androidX-collections": "1.4.0",
}
PINNED_VERSIONS = {"workManager": "2.7.0"}


def _library_version(entry: object, versions: dict[str, object]) -> object:
    """The version an entry resolves to: a `group:name:version` string, a `version` literal, or a
    `version.ref` into `[versions]`. Anything else is returned as is so a change still shows."""
    if isinstance(entry, str):
        return entry.rsplit(":", 1)[-1]
    if isinstance(entry, dict):
        version = entry.get("version")
        if isinstance(version, dict) and isinstance(version.get("ref"), str):
            return versions.get(version["ref"])
        return version
    return entry


def _completed(diff: str) -> bool:
    current = ""
    for line in diff.splitlines():
        if line.startswith("?? "):
            if line[3:].strip().startswith(TARGETS):
                return True
        elif line.startswith("diff --git "):
            current = line.rsplit(" b/", 1)[-1]
        elif line.startswith("+++ "):
            if line.startswith("+++ b/"):
                current = line[len("+++ b/") :]
        elif line.startswith("+") and line[1:].strip() and current.startswith(TARGETS):
            return True
    return False


def detect(tree: Path, diff: str) -> Detection:
    evidence: list[str] = []
    unparseable: list[str] = []
    catalog = tree / CATALOG
    if catalog.is_file():
        try:
            data = tomllib.loads(catalog.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            unparseable.append(f"{CATALOG}: does not parse as TOML ({error})")
            data = None
        if data is not None:
            versions = data.get("versions", {})
            libraries = data.get("libraries", {})
            for key, pinned in PINNED_VERSIONS.items():
                if key not in versions:
                    evidence.append(f"{CATALOG}: [versions] {key} removed (pinned at {pinned})")
                elif versions[key] != pinned:
                    evidence.append(f"{CATALOG}: [versions] {key} changed from {pinned} to {versions[key]}")
            for key, pinned in PINNED_LIBRARIES.items():
                if key not in libraries:
                    evidence.append(f"{CATALOG}: [libraries] {key} removed (pinned at {pinned})")
                    continue
                version = _library_version(libraries[key], versions)
                if version != pinned:
                    evidence.append(f"{CATALOG}: [libraries] {key} changed from {pinned} to {version}")
    if unparseable:
        return Detection(regressed=False, evidence=tuple(unparseable), completed=_completed(diff), indeterminate=True)
    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
