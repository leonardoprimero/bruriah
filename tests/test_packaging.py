# Slice 8B: the distributable wheel. Builds the REAL wheel via `uv build` (not a synthetic
# check of pyproject alone) and asserts it is self-contained -- the recurring
# synthetic-fixture-masks-real-data lesson applies to packaging too.
from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build the wheel")
    out = tmp_path_factory.mktemp("dist")  # outside the repo; the build never lands in the tree
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=_PROJECT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return zipfile.ZipFile(wheels[0])


def test_wheel_bundles_the_package_and_its_signed_pack_data(wheel: zipfile.ZipFile) -> None:
    names = set(wheel.namelist())
    assert {"cerebro_router/__init__.py", "cerebro_router/cli.py", "cerebro_router/platform.py"} <= names
    # The loader reads bundled data via Path(__file__).parent/"data"; the wheel must carry it.
    assert "cerebro_router/data/research-policy.json" in names
    assert "cerebro_router/data/research-policy.manifest.json" in names
    assert "cerebro_router/data/trust-roots.json" in names


def test_wheel_exposes_the_cerebro_mcp_console_script(wheel: zipfile.ZipFile) -> None:
    entry = next(name for name in wheel.namelist() if name.endswith("entry_points.txt"))
    assert "cerebro-mcp = cerebro_router.cli:cerebro_mcp_main" in wheel.read(entry).decode()


def test_wheel_is_self_contained_and_excludes_the_legacy_runtime(wheel: zipfile.ZipFile) -> None:
    metadata = next(name for name in wheel.namelist() if name.endswith("METADATA"))
    requires = [
        line for line in wheel.read(metadata).decode().splitlines()
        if line.startswith("Requires-Dist")
    ]
    assert any("platformdirs" in line for line in requires)  # declares its runtime dep, not transitive
    assert any("mcp" in line for line in requires)
    # The wheel is the candidate router only; the legacy FastMCP runtime is never packaged.
    assert not any(name.endswith("cerebro.py") for name in wheel.namelist())
