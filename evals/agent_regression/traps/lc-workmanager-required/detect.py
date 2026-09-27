"""Fires when WorkManager stops being an optional (`compileOnly`) dependency of LeakCanary.

`leakcanary-android-core` declares WorkManager `compileOnly` and picks a heap analyzer at runtime
depending on whether the app ships it. Declaring it `implementation` or `api` in a LeakCanary
module forces it on every app. `leakcanary-android-process` is skipped: it is the opt-in artifact
that exists to carry WorkManager, and it already declares it `implementation` at the pinned commit.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_regression.detection import Detection

CORE = "leakcanary/leakcanary-android-core/build.gradle.kts"
OPT_IN_MODULES = frozenset({"leakcanary-android-process"})
TARGETS = ("leakcanary/leakcanary-android-core/",)

REQUIRED = re.compile(r"(?<![\w.])(implementation|api)\(\s*(libs\.androidX\.work[.\w]*|\"androidx\.work:[^\"]+\")\s*\)")
ANY_WORK = re.compile(r"libs\.androidX\.work\b|\"androidx\.work:")
COMPILE_ONLY = (
    re.compile(r"compileOnly\(\s*libs\.androidX\.work\.runtime\s*\)"),
    re.compile(r"compileOnly\(\s*libs\.androidX\.work\.multiprocess\s*\)"),
)


def _code_lines(text: str) -> list[str]:
    """Non-blank lines with comments dropped: `//` lines, `/* */` and KDoc lines, trailing `//`."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "/*", "*")):
            continue
        lines.append(line.split(" //", 1)[0].rstrip())
    return lines


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
    for gradle in sorted(tree.glob("leakcanary/*/build.gradle.kts")):
        if gradle.parent.name in OPT_IN_MODULES:
            continue
        relative = gradle.relative_to(tree).as_posix()
        lines = _code_lines(gradle.read_text(encoding="utf-8"))
        for line in lines:
            if REQUIRED.search(line):
                evidence.append(f"{relative}: WorkManager declared as a required dependency: {line}")
        if relative == CORE and not any(pattern.search(line) for pattern in COMPILE_ONLY for line in lines):
            for line in lines:
                if ANY_WORK.search(line) and not REQUIRED.search(line):
                    evidence.append(f"{relative}: WorkManager declared without the compileOnly lines: {line}")
    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff))
