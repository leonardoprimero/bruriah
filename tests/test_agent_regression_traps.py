"""The committed trap set of the agent regression benchmark (`evals/agent_regression/traps/`).

These tests pin what T3 of `odd/tasks/agent-regression-benchmark.md` promised: a fixed set of
traps, each pinned to a public repository and commit, each backed by a decision document the
corpus at that commit carries, each with a deterministic detector proven in both directions on
committed fixtures, and each with a prompt that a second reader confirmed does not leak the
answer. They run offline: a detector is a pure function over a fixture tree and a diff text.
"""

from __future__ import annotations

import re
import shutil
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

# trap_id -> the identifiers that, in an agent's final message, cite the trap's decision: those
# derived from the decision ref, then those `trap.yaml` adds. Each added PR number was read from
# the cached mirror: the decision commit's squash subject for egui, the merge commit that first
# brought the decision commit into the pinned LeakCanary history for `#2875`. The own-history
# decisions were pushed to main before any pull request, so their sha is the only cue.
EXPECTED_CUES: dict[str, tuple[str, ...]] = {
    "own-fastmcp": ("395962e",),
    "own-lenient-schemas": ("395962e",),
    "own-rrf-k": ("7335546",),
    "own-ann-index": ("4dc37e8",),
    "lc-workmanager-required": ("940e0f3", "#2875"),
    "lc-androidx-bump": ("940e0f3", "#2875"),
    "lc-toast-removal": ("#844", "square/leakcanary#844"),
    "egui-image-formats": ("#4489", "emilk/egui#4489"),
    "egui-winit-default-features": ("be9f363", "#1971"),
    "egui-android-activity": ("89e4288", "#2863"),
    "egui-datepicker-chrono": ("a12d18d", "#8008"),
    "egui-wgpu-vulkan": ("#7342", "emilk/egui#7342"),
}

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
def test_each_trap_carries_its_pinned_citation_cues(traps: dict[str, Trap], trap_id: str) -> None:
    assert traps[trap_id].citation_cues == EXPECTED_CUES[trap_id]
    assert len(traps[trap_id].citation_cues) >= 1


def test_the_android_activity_trap_is_cited_by_the_pull_request_that_merged_its_decision(
    traps: dict[str, Trap],
) -> None:
    # 89e42884 is "Remove android-activity dependency + add activity features (#2863)".
    assert "#2863" in traps["egui-android-activity"].citation_cues


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


# A target file present but unparseable is neither clean nor regressed: the detector reports it
# as indeterminate, with the file named in its evidence. Each case corrupts one target file of a
# copy of the clean fixture, so the copy would otherwise be decided clean.
_UNPARSEABLE = {
    "egui-android-activity": [
        ("crates/eframe/Cargo.toml", b'[package\nname = "eframe"\n'),
        ("crates/egui-winit/Cargo.toml", b'[package]\nname = "egui-winit"\nversion = \n'),
        ("Cargo.toml", b'[workspace]\nmembers = ["\xff\xfe"]\n'),
    ],
    "own-lenient-schemas": [
        ("src/bruriah/contracts.py", b"class Base(BaseModel:\n    pass\n"),
        ("src/bruriah/mcp_server.py", b"def serve(:\n"),
        ("src/bruriah/contracts.py", b"x = 1\x00\n"),
    ],
    "egui-datepicker-chrono": [
        ("crates/egui_extras/Cargo.toml", b'[package\nname = "egui_extras"\n'),
        ("Cargo.toml", b'[workspace]\nmembers = ["\xff\xfe"]\n'),
    ],
    "egui-image-formats": [
        ("crates/eframe/Cargo.toml", b'[package]\nname = "eframe"\nversion = \n'),
        ("Cargo.toml", b"[workspace\n"),
    ],
    "egui-wgpu-vulkan": [
        ("crates/eframe/Cargo.toml", b"[features\n"),
        ("crates/egui-wgpu/Cargo.toml", b'[package]\nname = "\xff"\n'),
        ("Cargo.toml", b"[workspace]\nmembers = [\n"),
    ],
    "egui-winit-default-features": [
        ("crates/egui_glow/Cargo.toml", b"[dependencies\n"),
        ("crates/egui-winit/Cargo.toml", b'[package]\nname = "\xff"\n'),
        ("crates/eframe/Cargo.toml", b"[package]\nversion = \n"),
        ("Cargo.toml", b"[workspace\n"),
    ],
    "lc-androidx-bump": [
        ("gradle/libs.versions.toml", b'[versions\nworkManager = "2.7.0"\n'),
        ("gradle/libs.versions.toml", b'[versions]\nworkManager = "\xff"\n'),
    ],
    "own-ann-index": [
        ("pyproject.toml", b"[project\ndependencies = []\n"),
        ("pyproject.toml", b'[project]\nname = "\xff"\n'),
    ],
    "own-fastmcp": [
        ("pyproject.toml", b"[project\ndependencies = []\n"),
        ("pyproject.toml", b'[project]\nname = "\xff"\n'),
    ],
    "own-rrf-k": [
        ("src/bruriah/retrieval.py", b"def fused_order(:\n"),
        ("src/bruriah/retrieval.py", b"x = 1\x00\n"),
        # The module-level constant still reads 60, but `fuse_ranks` cannot be checked.
        ("src/bruriah/ranking.py", b"RRF_K: int = 60\n\ndef fuse_ranks(:\n"),
    ],
}


@pytest.mark.parametrize(
    ("trap_id", "relative", "content"),
    [(trap_id, relative, content) for trap_id, cases in _UNPARSEABLE.items() for relative, content in cases],
)
def test_a_detector_is_indeterminate_on_an_unparseable_target_file(
    traps: dict[str, Trap], tmp_path: Path, trap_id: str, relative: str, content: bytes
) -> None:
    trap = traps[trap_id]
    source = trap.path / "fixtures" / "clean"
    tree = tmp_path / "tree"
    shutil.copytree(source, tree)
    assert (tree / relative).is_file(), "corrupt a file the clean fixture carries, never add one"
    (tree / relative).write_bytes(content)
    diff = (tree / "diff.patch").read_text(encoding="utf-8")

    detection = validate_detection(load_detector(trap)(tree, diff))

    assert detection.indeterminate is True
    assert detection.regressed is False
    assert detection.completed is True, "completion is decided from the diff, as before"
    assert any(item.startswith(f"{relative}: does not parse") for item in detection.evidence), detection.evidence


# A regression proven in a file that parsed is a fact; another target being unparseable does not
# erase it. Each case copies a fixture, overwrites target files, and expects a regression whose
# evidence lists the proof first and the unparseable file after it.
_REGRESSED_DESPITE_UNPARSEABLE = [
    pytest.param(
        "egui-android-activity",
        "regressed",
        {"Cargo.toml": b"[workspace\n"},
        "crates/eframe/Cargo.toml: [features] default enables",
        "Cargo.toml",
        id="egui-workspace-unparseable",
    ),
    pytest.param(
        "egui-android-activity",
        "regressed",
        {"crates/egui-winit/Cargo.toml": b"[package]\nversion = \n"},
        "crates/eframe/Cargo.toml: [features] default enables",
        "crates/egui-winit/Cargo.toml",
        id="egui-winit-unparseable",
    ),
    pytest.param(
        "own-lenient-schemas",
        "regressed",
        {"src/bruriah/mcp_server.py": b"def serve(:\n"},
        "src/bruriah/contracts.py: line 12 sets extra='ignore'",
        "src/bruriah/mcp_server.py",
        id="lenient-mcp-server-unparseable",
    ),
    pytest.param(
        "own-lenient-schemas",
        "clean",
        {
            "src/bruriah/mcp_server.py": b'from pydantic import ConfigDict\n\nCONFIG = ConfigDict(extra="ignore")\n',
            "src/bruriah/contracts.py": b"class Base(BaseModel:\n",
        },
        "src/bruriah/mcp_server.py: line 3 sets extra='ignore'",
        "src/bruriah/contracts.py",
        id="lenient-contracts-unparseable",
    ),
    pytest.param(
        "egui-datepicker-chrono",
        "regressed",
        {"Cargo.toml": b"[workspace\n"},
        "crates/egui_extras/Cargo.toml: [dependencies] depends on chrono",
        "Cargo.toml",
        id="datepicker-workspace-unparseable",
    ),
    pytest.param(
        "egui-datepicker-chrono",
        "regressed",
        {"crates/egui_extras/Cargo.toml": b"[package\n"},
        "Cargo.toml: [workspace.dependencies] depends on chrono",
        "crates/egui_extras/Cargo.toml",
        id="datepicker-extras-unparseable",
    ),
    pytest.param(
        "egui-image-formats",
        "regressed",
        {"Cargo.toml": b"[workspace\n"},
        "crates/eframe/Cargo.toml: [target.",
        "Cargo.toml",
        id="image-formats-workspace-unparseable",
    ),
    pytest.param(
        "egui-wgpu-vulkan",
        "regressed",
        {"crates/egui-wgpu/Cargo.toml": b"[features\n"},
        "crates/eframe/Cargo.toml: [features] wgpu enables wgpu/vulkan",
        "crates/egui-wgpu/Cargo.toml",
        id="wgpu-vulkan-egui-wgpu-unparseable",
    ),
    pytest.param(
        "egui-winit-default-features",
        "regressed",
        {"Cargo.toml": b"[workspace\n"},
        "crates/egui_glow/Cargo.toml: [dependencies] winit hardwires features",
        "Cargo.toml",
        id="winit-defaults-workspace-unparseable",
    ),
    pytest.param(
        "own-ann-index",
        "regressed",
        {"pyproject.toml": b"[project\n"},
        "src/bruriah/index.py: line 7 imports sqlite_vec",
        "pyproject.toml",
        id="ann-index-pyproject-unparseable",
    ),
    pytest.param(
        "own-fastmcp",
        "regressed",
        {"pyproject.toml": b"[project\n"},
        "src/bruriah/mcp_server.py: line 11 imports from mcp.server.fastmcp",
        "pyproject.toml",
        id="fastmcp-pyproject-unparseable",
    ),
    pytest.param(
        "own-rrf-k",
        "regressed",
        {"src/bruriah/retrieval.py": b"def fused_order(:\n"},
        "src/bruriah/ranking.py: RRF_K = 20",
        "src/bruriah/retrieval.py",
        id="rrf-k-retrieval-unparseable",
    ),
    pytest.param(
        "own-rrf-k",
        "clean",
        {"src/bruriah/ranking.py": b"RRF_K: int = 20\n\ndef fuse_ranks(:\n"},
        "src/bruriah/ranking.py: RRF_K = 20",
        "src/bruriah/ranking.py",
        id="rrf-k-ranking-unparseable-constant-read",
    ),
]


@pytest.mark.parametrize(("trap_id", "direction", "overwrites", "proof", "broken"), _REGRESSED_DESPITE_UNPARSEABLE)
def test_a_proven_regression_survives_another_unparseable_target_file(
    traps: dict[str, Trap],
    tmp_path: Path,
    trap_id: str,
    direction: str,
    overwrites: dict[str, bytes],
    proof: str,
    broken: str,
) -> None:
    trap = traps[trap_id]
    tree = tmp_path / "tree"
    shutil.copytree(trap.path / "fixtures" / direction, tree)
    for relative, content in overwrites.items():
        assert (tree / relative).is_file(), "overwrite a file the fixture carries, never add one"
        (tree / relative).write_bytes(content)

    detection = validate_detection(load_detector(trap)(tree, (tree / "diff.patch").read_text(encoding="utf-8")))

    assert detection.regressed is True
    assert detection.indeterminate is False
    proofs = [index for index, item in enumerate(detection.evidence) if item.startswith(proof)]
    notes = [index for index, item in enumerate(detection.evidence) if item.startswith(f"{broken}: does not parse")]
    assert proofs and len(notes) == 1, detection.evidence
    assert max(proofs) < notes[0], "the regression evidence comes first, the unparseable note after it"
