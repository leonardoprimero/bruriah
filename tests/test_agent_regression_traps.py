"""The committed trap set of the agent regression benchmark (`evals/agent_regression/traps/`).

These tests pin what T3 of `odd/tasks/agent-regression-benchmark.md` promised: a fixed set of
traps, each pinned to a public repository and commit, each backed by a decision document the
corpus at that commit carries, each with a deterministic detector proven in both directions on
committed fixtures, and each with a prompt that a second reader confirmed does not leak the
answer. They run offline: a detector is a pure function over a fixture tree and a diff text.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from agent_regression.detection import check_trap_fixtures, validate_detection  # noqa: E402
from agent_regression.traps import Trap, load_detector, load_traps  # noqa: E402

TRAPS_DIR = EVALS_DIR / "agent_regression" / "traps"

SECOND_READER = "leonardoprimero"
SECOND_READER_DATE = "2026-09-27"

BRURIAH = "https://github.com/leonardoprimero/bruriah.git"
BRURIAH_COMMIT = "1d36bc781bd830dcf277277967a5aab81e9dcdd9"
LEAKCANARY = "https://github.com/square/leakcanary"
LEAKCANARY_COMMIT = "0f7dbab17e2a9f6f310f7b8be214e029bfc502e3"
EGUI = "https://github.com/emilk/egui"
EGUI_COMMIT = "5d3e958ecfd3468a460c57094ebaeca6e3c4f325"

# trap_id -> (source, repository, commit, decision_ref, rejected_alternative). The decision ref
# is the commit (full sha) or the GitHub issue whose corpus document carries the rejection.
EXPECTED: dict[str, tuple[str, str, str, str, str]] = {
    "own-fastmcp": (
        "own-history",
        BRURIAH,
        BRURIAH_COMMIT,
        "395962e7c96fd6ce1d3fb8b26afb15162c5d5913",
        "FastMCP",
    ),
    "own-lenient-schemas": (
        "own-history",
        BRURIAH,
        BRURIAH_COMMIT,
        "395962e7c96fd6ce1d3fb8b26afb15162c5d5913",
        "argument models that ignore unknown fields",
    ),
    "own-rrf-k": (
        "own-history",
        BRURIAH,
        BRURIAH_COMMIT,
        "73355460095703f7f219b3461a8b2498065843e9",
        "a lower RRF_K without a measurement",
    ),
    "own-ann-index": (
        "own-history",
        BRURIAH,
        BRURIAH_COMMIT,
        "4dc37e8ab9c5d8e07ec72d87734346ea945e215a",
        "sqlite-vec",
    ),
    "lc-workmanager-required": (
        "leakcanary",
        LEAKCANARY,
        LEAKCANARY_COMMIT,
        "940e0f30e07c1ee2a709554369bc6e48f30e02ff",
        "WorkManager as a required dependency",
    ),
    "lc-androidx-bump": (
        "leakcanary",
        LEAKCANARY,
        LEAKCANARY_COMMIT,
        "940e0f30e07c1ee2a709554369bc6e48f30e02ff",
        "raising the AndroidX versions pinned on purpose",
    ),
    "lc-toast-removal": (
        "leakcanary",
        LEAKCANARY,
        LEAKCANARY_COMMIT,
        "github:square/leakcanary#844",
        "removing the toast",
    ),
    "egui-image-formats": (
        "egui",
        EGUI,
        EGUI_COMMIT,
        "github:emilk/egui#4489",
        "enabling the image crate's other formats in eframe",
    ),
    "egui-winit-default-features": (
        "egui",
        EGUI,
        EGUI_COMMIT,
        "be9f363c5373447b8e44036f10cae115a8e7b32c",
        "winit's default features",
    ),
    "egui-android-activity": (
        "egui",
        EGUI,
        EGUI_COMMIT,
        "89e42884fcc38f304a96134ce47bb8441208a2e2",
        "android-activity",
    ),
    "egui-datepicker-chrono": (
        "egui",
        EGUI,
        EGUI_COMMIT,
        "a12d18d9bdf79afcb669908d1c6119b1816f440c",
        "chrono",
    ),
    "egui-wgpu-vulkan": (
        "egui",
        EGUI,
        EGUI_COMMIT,
        "github:emilk/egui#7342",
        "hardcoding the Vulkan backend in eframe's wgpu features",
    ),
}
TRAP_IDS = sorted(EXPECTED)

# Traps whose rejection is also written in the working tree the agent edits, at the edit site: a
# comment in LeakCanary's version catalog, a comment block in eframe's Cargo.toml, and, for every
# own-history trap, this repository's own habit of carrying its decisions in code comments
# (`mcp_server.py`'s header, the block above `RRF_K`, the dependency notes in `pyproject.toml`).
# They stay in the set as a control for what an in-tree comment achieves on its own; the report
# stratifies them and the README of the trap set names them.
TREE_VISIBLE_RATIONALE = frozenset(
    {
        "own-fastmcp",
        "own-lenient-schemas",
        "own-rrf-k",
        "own-ann-index",
        "lc-androidx-bump",
        "egui-android-activity",
    }
)

# Words that would turn a task into an instruction about the project's past. The loader already
# refuses the alternative's name and "bruriah"; this is the second-reader rule, pinned.
_LEAKING_WORDS = ("rejected", "decision", "decided", "history", "previously", "alternative", "instead of")

# A detector is a pure function over a tree and a diff: no process, no network, no clock.
_IMPURE = re.compile(r"\b(subprocess|socket|urllib|requests|http\.client|os\.system|time\.time|datetime\.now)\b")


@pytest.fixture(scope="module")
def traps() -> dict[str, Trap]:
    return {trap.trap_id: trap for trap in load_traps(TRAPS_DIR)}


def test_the_committed_set_is_exactly_the_signed_off_traps(traps: dict[str, Trap]) -> None:
    assert sorted(traps) == TRAP_IDS
    assert 12 <= len(traps) <= 15


def test_every_source_is_represented_and_the_shortfall_is_the_declared_one(traps: dict[str, Trap]) -> None:
    per_source = {
        source: sorted(t.trap_id for t in traps.values() if t.source == source)
        for source in ("own-history", "leakcanary", "egui")
    }
    assert len(per_source["own-history"]) == 4
    assert len(per_source["egui"]) == 5
    # One below the four-per-source target, declared in the task document rather than padded.
    assert len(per_source["leakcanary"]) == 3


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_trap_pins_the_repository_commit_decision_and_alternative(traps: dict[str, Trap], trap_id: str) -> None:
    source, repository, commit, decision_ref, alternative = EXPECTED[trap_id]
    trap = traps[trap_id]
    assert trap.source == source
    assert trap.repository == repository
    assert trap.commit == commit
    assert trap.decision_ref == decision_ref
    assert trap.rejected_alternative == alternative
    assert trap.path == TRAPS_DIR / trap_id


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_prompt_was_read_by_the_second_reader_and_does_not_describe_the_past(
    traps: dict[str, Trap], trap_id: str
) -> None:
    trap = traps[trap_id]
    assert trap.second_reader == SECOND_READER
    assert trap.second_reader_date == SECOND_READER_DATE
    lowered = trap.prompt.lower()
    for word in _LEAKING_WORDS:
        assert word not in lowered, (trap_id, word)
    assert len(trap.prompt) <= 600, "a prompt is a ticket, not a design document"


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_trap_declares_a_bounded_budget(traps: dict[str, Trap], trap_id: str) -> None:
    trap = traps[trap_id]
    assert 10 <= trap.turn_budget <= 60
    assert 120 <= trap.time_budget_seconds <= 1800


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_detector_fires_on_its_regressed_fixture_and_not_on_its_clean_one(
    traps: dict[str, Trap], trap_id: str
) -> None:
    assert check_trap_fixtures(traps[trap_id]) is None


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_fired_detection_names_evidence_a_reviewer_can_open(traps: dict[str, Trap], trap_id: str) -> None:
    trap = traps[trap_id]
    tree = trap.path / "fixtures" / "regressed"
    detection = validate_detection(load_detector(trap)(tree, (tree / "diff.patch").read_text(encoding="utf-8")))
    assert detection.regressed
    assert detection.completed, "the regressed fixture is an on-task edit that also regresses"
    for item in detection.evidence:
        # `<relative path>: <what fired>`; the path exists in the fixture tree, so the spot-check
        # in a published run has somewhere to look.
        path, sep, what = item.partition(": ")
        assert sep and what, item
        assert (tree / path).is_file(), (trap_id, item)


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_a_clean_edit_completes_and_an_empty_diff_does_not(traps: dict[str, Trap], trap_id: str) -> None:
    trap = traps[trap_id]
    detect = load_detector(trap)
    tree = trap.path / "fixtures" / "clean"
    clean = validate_detection(detect(tree, (tree / "diff.patch").read_text(encoding="utf-8")))
    assert not clean.regressed
    assert clean.completed
    untouched = validate_detection(detect(tree, ""))
    assert not untouched.regressed
    assert not untouched.completed, "avoiding the regression by doing nothing is not a win"


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_fixtures_are_small_synthetic_trees_with_a_non_empty_diff(traps: dict[str, Trap], trap_id: str) -> None:
    for direction in ("regressed", "clean"):
        tree = traps[trap_id].path / "fixtures" / direction
        files = [path for path in tree.rglob("*") if path.is_file()]
        assert (tree / "diff.patch").read_text(encoding="utf-8").strip()
        assert not (tree / "CLAUDE.md").exists(), "the prompted condition never writes CLAUDE.md any more"
        assert 2 <= len(files) <= 12, (trap_id, direction, len(files))
        assert sum(path.stat().st_size for path in files) < 64 * 1024


@pytest.mark.parametrize("trap_id", TRAP_IDS)
def test_each_detector_is_pure(traps: dict[str, Trap], trap_id: str) -> None:
    source = (traps[trap_id].path / "detect.py").read_text(encoding="utf-8")
    assert not _IMPURE.search(source), trap_id
    assert "def detect(" in source


def test_the_tree_visible_control_traps_are_named_in_the_trap_set_readme() -> None:
    readme = (TRAPS_DIR / "README.md").read_text(encoding="utf-8")
    for trap_id in sorted(TREE_VISIBLE_RATIONALE):
        assert f"`{trap_id}`" in readme
