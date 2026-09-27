"""Detector for `own-rrf-k`: the RRF constant moved off the measured 60.

Fires when `ranking.py`'s module-level `RRF_K` is not 60 or is gone, when any `src/` module
assigns `RRF_K`/`_RRF_K` another integer, when `fuse_ranks` defaults `rrf_k` to another integer,
or when a call to the fusion function (`fuse_ranks`, or its alias `_fuse`) passes one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agent_regression.detection import Detection

RANKING = "src/bruriah/ranking.py"
TARGETS = frozenset({RANKING})
MEASURED_K = 60

_CONSTANT = re.compile(r"^RRF_K\s*(?::[^=\n]*)?=\s*(\d+)", re.MULTILINE)
_CONSTANT_NAMES = frozenset({"RRF_K", "_RRF_K"})
_FUSION_NAMES = frozenset({"fuse_ranks", "_fuse"})
_K_KEYWORDS = frozenset({"rrf_k", "k"})
_K_POSITION = 3  # fuse_ranks(lexical_ranks, vector_ranks, lexical_weight, rrf_k)


def _other_int(node: ast.expr | None) -> int | None:
    """The integer literal in `node` when it is one other than the measured K."""
    if isinstance(node, ast.Constant) and type(node.value) is int and node.value != MEASURED_K:
        return node.value
    return None


def _constant_hits(tree: Path) -> list[str]:
    path = tree / RANKING
    if not path.is_file():
        return [f"{RANKING}: file removed"]
    values = _CONSTANT.findall(path.read_text(encoding="utf-8", errors="replace"))
    if not values:
        return [f"{RANKING}: module-level RRF_K constant removed"]
    return [f"{RANKING}: RRF_K = {value}" for value in values if int(value) != MEASURED_K]


def _python_hits(rel: str, text: str) -> list[str]:
    try:
        module = ast.parse(text)
    except SyntaxError:
        return []
    hits = []
    # `ranking.py`'s own constant is read by `_constant_hits`; other modules may shadow it.
    for node in module.body if rel != RANKING else []:
        if isinstance(node, ast.Assign):
            names, value = node.targets, _other_int(node.value)
        elif isinstance(node, ast.AnnAssign):
            names, value = [node.target], _other_int(node.value)
        else:
            continue
        if value is None:
            continue
        hits.extend(
            f"{rel}: line {node.lineno} assigns {name.id} = {value}"
            for name in names
            if isinstance(name, ast.Name) and name.id in _CONSTANT_NAMES
        )
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "fuse_ranks":
            args = node.args.posonlyargs + node.args.args
            defaults = dict(zip([arg.arg for arg in args][len(args) - len(node.args.defaults) :], node.args.defaults))
            defaults.update(
                (arg.arg, default) for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default
            )
            value = _other_int(defaults.get("rrf_k"))
            if value is not None:
                hits.append(f"{rel}: line {node.lineno} defaults rrf_k to {value}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            if name not in _FUSION_NAMES:
                continue
            passed = [keyword.value for keyword in node.keywords if keyword.arg in _K_KEYWORDS]
            if len(node.args) > _K_POSITION:
                passed.append(node.args[_K_POSITION])
            for argument in passed:
                value = _other_int(argument)
                if value is not None:
                    hits.append(f"{rel}: line {node.lineno} passes k={value} to {name}")
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
    evidence = _constant_hits(tree)
    src = tree / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            evidence.extend(_python_hits(path.relative_to(tree).as_posix(), text))
    return Detection(regressed=bool(evidence), evidence=tuple(evidence), completed=_completed(diff, TARGETS))
