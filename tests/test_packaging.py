# Slice 8B: the distributable wheel. Builds the REAL wheel via `uv build` (not a synthetic
# check of pyproject alone) and asserts it is self-contained -- the recurring
# synthetic-fixture-masks-real-data lesson applies to packaging too.
from __future__ import annotations

import json
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


# --- the first-party skill pack (Unit 11) ---------------------------------------------------------

_SKILL_BODIES = (
    "falsifiability-probe/SKILL.md",
    "verify-before-asserting/SKILL.md",
    "make-it-inexpressible/SKILL.md",
)


def test_wheel_bundles_the_skill_pack_and_every_body(wheel: zipfile.ZipFile) -> None:
    """A pack whose bodies did not ship is a dispatcher pointing at nothing."""
    names = set(wheel.namelist())
    assert "cerebro_router/data/practices-pack.json" in names
    assert "cerebro_router/data/practices-pack.manifest.json" in names
    for body in _SKILL_BODIES:
        assert f"cerebro_router/data/skills/{body}" in names, body


def test_every_declared_digest_matches_the_body_that_shipped(wheel: zipfile.ZipFile) -> None:
    """The invariant that makes the digest meaningful rather than decorative.

    Checked against the WHEEL's bytes, not the working tree's: a body edited after the pack was
    signed would still look consistent on disk while shipping broken."""
    import hashlib

    pack = json.loads(wheel.read("cerebro_router/data/practices-pack.json"))
    for skill in pack["skills"]:
        shipped = wheel.read(f"cerebro_router/data/skills/{skill['body_locator']}")
        assert skill["body_digest"] == "sha256:" + hashlib.sha256(shipped).hexdigest(), \
            skill["skill_id"]


def test_the_bundled_pack_loads_from_the_installed_location() -> None:
    from datetime import date

    from cerebro_router.platform import load_bundled_skills

    skills = load_bundled_skills(today=date(2026, 7, 25))
    assert skills.skill_ids == (
        "cerebro.falsifiability-probe", "cerebro.make-it-inexpressible",
        "cerebro.verify-before-asserting",
    )
    assert all(skill.tier == "first_party" for skill in skills.skills)


def test_every_bundled_skill_grants_nothing() -> None:
    # First-party status is not a reason to grant more. Default-deny is visible here or it is not
    # real anywhere.
    from datetime import date

    from cerebro_router.platform import load_bundled_skills

    for skill in load_bundled_skills(today=date(2026, 7, 25)).skills:
        assert skill.permissions.grants_nothing(), skill.skill_id
        assert skill.payload == "prose"


def test_every_bundled_skill_states_its_limits() -> None:
    # A skill that claims no limits is either trivial or dishonest. This is content quality asserted
    # as a test, because content is what decides whether the pack is worth installing.
    from datetime import date

    from cerebro_router.platform import load_bundled_skills

    for skill in load_bundled_skills(today=date(2026, 7, 25)).skills:
        assert skill.limitations, skill.skill_id
        assert len(skill.summary) > 40, skill.skill_id


def test_the_bundled_skills_dispatch_for_their_declared_domain() -> None:
    """End to end: a real programming request reaches the shipped skills."""
    from datetime import date

    from cerebro_router.classify import RequestClassification
    from cerebro_router.contracts import HostSkill
    from cerebro_router.dispatch import dispatch
    from cerebro_router.lookup import discover
    from cerebro_router.platform import load_bundled_skills, load_registry

    today = date(2026, 7, 25)
    skills = load_bundled_skills(today=today)
    lookup = discover(
        RequestClassification(intent="investigate", domain="programming", claim_type="factual",
                              risk="low", jurisdiction="unknown"),
        load_registry(today=today), skills,
    )
    assert len(lookup.skills) == 3
    installed = [HostSkill(skill_id=match.skill.skill_id, version=match.skill.version,
                           digest=match.skill.body_digest) for match in lookup.skills]
    result = dispatch(lookup, installed)
    assert [item.availability for item in result.skills] == ["installed"] * 3
    assert result.gaps == ()


def test_a_non_programming_request_gets_no_bundled_skills() -> None:
    # The negative control: dispatch is domain-gated, not "always send everything we ship".
    from datetime import date

    from cerebro_router.classify import RequestClassification
    from cerebro_router.lookup import discover
    from cerebro_router.platform import load_bundled_skills, load_registry

    today = date(2026, 7, 25)
    lookup = discover(
        RequestClassification(intent="investigate", domain="law", claim_type="factual",
                              risk="low", jurisdiction="unknown"),
        load_registry(today=today), load_bundled_skills(today=today),
    )
    assert lookup.skills == ()
