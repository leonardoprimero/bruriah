"""Detector for `own-ann-index`: vector retrieval moved onto an ANN library or sqlite-vec.

Fires on an ANN distribution in the runtime `[project].dependencies` of `pyproject.toml`, an
import of one in `src/`, or a `CREATE VIRTUAL TABLE ... USING vec0` in `src/`. Dependency groups
are not read: `sqlite-vec` legitimately sits in the dev group for a legacy benchmark.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from agent_regression.detection import Detection

TARGETS = frozenset({"src/bruriah/retrieval.py", "src/bruriah/index.py", "pyproject.toml"})

_ANN_DISTRIBUTIONS = frozenset(
    {"sqlite-vec", "hnswlib", "annoy", "usearch", "chromadb", "lancedb", "pynndescent", "voyager"}
)
_ANN_MODULES = frozenset(
    {"sqlite_vec", "faiss", "hnswlib", "annoy", "usearch", "chromadb", "lancedb", "pynndescent", "voyager"}
)
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_IMPORT_LINE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)")
_VEC0 = re.compile(r"CREATE\s+VIRTUAL\s+TABLE\b[^;]*?\bUSING\s+vec0\b", re.IGNORECASE)


def _is_ann_distribution(entry: str) -> bool:
    match = _REQUIREMENT_NAME.match(entry)
    if not match:
        return False
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    return name in _ANN_DISTRIBUTIONS or name == "faiss" or name.startswith("faiss-")


def _dependency_hits(tree: Path) -> list[str]:
    pyproject = tree / "pyproject.toml"
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return []
    return [
        f"pyproject.toml: runtime dependency {entry}"
        for entry in data.get("project", {}).get("dependencies", [])
        if _is_ann_distribution(str(entry))
    ]


def _imported_modules(text: str) -> list[tuple[int, str]]:
    """(line, top-level module) for every absolute import; comment lines never count."""
    try:
        module = ast.parse(text)
    except SyntaxError:
        found = []
        for number, line in enumerate(text.splitlines(), start=1):
            match = _IMPORT_LINE.match(line)
            if match:
                found.append((number, match.group(1)))
        return found
    found = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module.split(".")[0]))
    return found


def _python_hits(rel: str, text: str) -> list[str]:
    hits = [f"{rel}: line {line} imports {name}" for line, name in _imported_modules(text) if name in _ANN_MODULES]
    # Blank comment lines, keeping line numbers, so a comment that mentions vec0 never fires.
    code = "\n".join("" if line.lstrip().startswith("#") else line for line in text.splitlines())
    for match in _VEC0.finditer(code):
        line = code.count("\n", 0, match.start()) + 1
        hits.append(f"{rel}: line {line} creates a vec0 virtual table")
    return hits


def _completed(diff: str, targets: frozenset[str]) -> bool:
    """True iff the diff adds a non-blank line to a target path or lists one as untracked."""
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("?? "):
            if line[3:].strip() in targets:
                return True
        elif line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = line[4:].strip()
            current = path[2:] if path.startswith("b/") else None
        elif current in targets and line.startswith("+") and line[1:].strip():
            return True
    return False


def detect(tree: Path, diff: str) -> Detection:
    evidence = _dependency_hits(tree)
    src = tree / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            evidence.extend(_python_hits(path.relative_to(tree).as_posix(), text))
    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff, TARGETS))
