"""Detector for `own-fastmcp`: the MCP server moved onto FastMCP.

Fires on a FastMCP import or `FastMCP(` call in `src/`, or on a `fastmcp` runtime dependency.
Python is read through `ast`, so comments and docstrings that name FastMCP never fire.
A `pyproject.toml` that is present but does not parse makes the run indeterminate, unless `src/`
proves the regression; its note then follows that evidence. Unparseable Python is still searched
line by line.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from agent_regression.detection import Detection

TARGETS = frozenset({"src/bruriah/mcp_server.py"})

_FASTMCP_MODULES = ("fastmcp", "mcp.server.fastmcp")
_IMPORT_LINE = re.compile(r"^\s*(?:from|import)\s+(?:mcp\.server\.)?fastmcp\b")
_CALL = re.compile(r"\bFastMCP\s*\(")
_REQUIREMENT_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _is_fastmcp_module(name: str) -> bool:
    return any(name == module or name.startswith(module + ".") for module in _FASTMCP_MODULES)


def _python_hits(rel: str, text: str) -> list[str]:
    try:
        module = ast.parse(text)
    except SyntaxError:
        # A file the agent left unparseable is still read, line by line, skipping comment lines.
        hits = []
        for number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if _IMPORT_LINE.search(line) or _CALL.search(line):
                hits.append(f"{rel}: line {number} uses FastMCP")
        return hits
    hits = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            hits.extend(
                f"{rel}: line {node.lineno} imports {alias.name}"
                for alias in node.names
                if _is_fastmcp_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_fastmcp_module(node.module) or (
                node.module == "mcp.server" and any(alias.name == "fastmcp" for alias in node.names)
            ):
                hits.append(f"{rel}: line {node.lineno} imports from {node.module}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name == "FastMCP":
                hits.append(f"{rel}: line {node.lineno} calls FastMCP(...)")
    return hits


def _dependency_hits(tree: Path) -> list[str]:
    pyproject = tree / "pyproject.toml"
    if not pyproject.is_file():
        return []
    # A `TOMLDecodeError` or `UnicodeDecodeError` propagates: `detect` reports the file as unparseable.
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    hits = []
    for entry in data.get("project", {}).get("dependencies", []):
        match = _REQUIREMENT_NAME.match(str(entry))
        if match and re.sub(r"[-_.]+", "-", match.group(1)).lower() == "fastmcp":
            hits.append(f"pyproject.toml: runtime dependency {entry}")
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
    unparseable: list[str] = []
    try:
        evidence = _dependency_hits(tree)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        evidence = []
        unparseable.append(f"pyproject.toml: does not parse as TOML ({error})")
    src = tree / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            evidence.extend(_python_hits(path.relative_to(tree).as_posix(), text))
    completed = _completed(diff, TARGETS)
    if unparseable and not evidence:
        return Detection(regressed=False, evidence=tuple(unparseable), completed=completed, indeterminate=True)
    return Detection(regressed=bool(evidence), evidence=(*evidence, *unparseable), completed=completed)
