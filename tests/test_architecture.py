"""Static import-graph guard: only `cli.py` and `_cli/*` may depend on the CLI adapter layer.

`bruriah.cli` is the CLI adapter: argument parsing, process exit codes, stdout/stderr
formatting, and `main()`. Every other module under `src/bruriah` is infrastructure or domain
logic that the adapter is supposed to sit on top of. A module outside `cli.py`/`_cli/*` importing
FROM `bruriah.cli` inverts that dependency -- the adapter becomes a hidden implementation detail
of the modules that are meant to be adapter-independent, and the CLI can no longer change without
risking breakage two layers down.

This walks the AST of every module under `src/bruriah` (never imports them, so it works even when
an offending import would itself fail at runtime) and fails if any module outside the CLI adapter
imports `bruriah.cli` (absolute) or `.cli` (package-relative from `src/bruriah`).
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bruriah"


def _imports_cli_adapter(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from .cli import ...` at the package root has level=1 and module="cli".
            if node.level == 1 and node.module == "cli":
                return True
            if node.module in ("bruriah.cli",):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name == "bruriah.cli" for alias in node.names):
                return True
    return False


def test_no_module_outside_cli_adapter_imports_bruriah_cli() -> None:
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        relative = path.relative_to(SRC_ROOT)
        if relative == Path("cli.py") or relative.parts[0] == "_cli":
            continue
        if _imports_cli_adapter(path):
            offenders.append(relative.as_posix())
    assert not offenders, (
        "module(s) outside cli.py/_cli/* import bruriah.cli, inverting the CLI-adapter "
        f"dependency: {offenders}. Move the shared logic these modules need out of cli.py "
        "instead of importing the adapter from infrastructure."
    )
