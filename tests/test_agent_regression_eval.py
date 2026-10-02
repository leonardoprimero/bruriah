"""Tests for `evals/agent_regression/`: the agent-in-the-loop regression benchmark whose headline
metric is Regression Rate (RR) -- how often an agent's resulting change reintroduces an
alternative a project explicitly rejected, decided by a deterministic detector over the resulting
tree, never by a model. See `odd/tasks/agent-regression-benchmark.md` for the method.

Every test here is hermetic: trap directories, detector modules, fixture trees and run records
are built under `tmp_path`, and agent runs are replayed from in-memory records through the
replay adapter. No test reaches the network, a model, a paid API, the `claude` binary, or git.

The harness is a real package (`evals/agent_regression/`, with an `__init__.py`) imported through
`ROOT / "evals"` on `sys.path`, never through flat module names: `evals/retrieval` already ships
`metrics.py` and `adapters.py`, and `evals/injection` ships `run.py`, so flat names would collide
in one pytest session.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from agent_regression.traps import Trap, TrapError, load_detector, load_trap, load_traps  # noqa: E402
from agent_regression.detection import (  # noqa: E402
    Detection,
    DetectionError,
    check_trap_fixtures,
    validate_detection,
)
from agent_regression.runs import (  # noqa: E402
    BASELINE,
    CONDITIONS,
    DEFAULT_CONDITIONS,
    GATED,
    PROMPTED,
    UNPROMPTED,
    WRITE_TOOLS,
    AgentRun,
    Provenance,
    RunRecordError,
    ToolCall,
    consulted_before_first_write,
    run_from_json,
    run_to_json,
)
from agent_regression.metrics import (  # noqa: E402
    ConditionSummary,
    pair_by_trap,
    pair_silent_by_trap,
    paired_sign_test,
    summarize,
    trap_set_digest,
    wilson_interval,
)
from agent_regression.report import render_json, render_markdown, write_report  # noqa: E402
from agent_regression.run import main, plan_invocations  # noqa: E402
from agent_regression.adapters import AdapterError, AgentAdapter, ReplayAdapter, ReplayError, run_benchmark  # noqa: E402

import pytest  # noqa: E402
import yaml  # noqa: E402

from agent_regression import adapters as adapters_module  # noqa: E402
from agent_regression import report as report_module  # noqa: E402
from agent_regression import run as run_module  # noqa: E402

_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_PROVENANCE_DATE = "2026-09-27"
_OMIT = object()

# A trivial detector for synthetic traps: the rejected alternative is a dependency named
# `fastlib`, so the trap regresses when the tree's `pyproject.toml` mentions it; the task counts
# as completed when `src/server.py` exists. Deterministic, no model, no network.
_DETECT_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    manifest = tree / "pyproject.toml"
    text = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
    evidence = tuple(line.strip() for line in text.splitlines() if "fastlib" in line)
    return Detection(regressed=bool(evidence), evidence=evidence, completed=(tree / "src" / "server.py").is_file())
"""

# A detector that decides on the diff alone, used to prove `check_trap_fixtures` passes the
# fixture's `diff.patch` text rather than an empty string or a path.
_DIFF_DETECT_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    evidence = tuple(line for line in diff.splitlines() if line.startswith("+") and "fastlib" in line)
    return Detection(regressed=bool(evidence), evidence=evidence, completed=True)
"""

# Detectors that fail the fixture contract in each direction.
_NEVER_FIRES_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    return Detection(regressed=False, evidence=(), completed=True)
"""

_ALWAYS_FIRES_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    return Detection(regressed=True, evidence=("always",), completed=True)
"""

# Fires on the regressed fixture, but cannot decide on the clean one.
_INDETERMINATE_WHEN_CLEAN_SOURCE = """\
from pathlib import Path

from agent_regression.detection import Detection


def detect(tree: Path, diff: str) -> Detection:
    text = (tree / "pyproject.toml").read_text(encoding="utf-8")
    if "fastlib" in text:
        return Detection(regressed=True, evidence=("pyproject.toml: fastlib",), completed=True)
    return Detection(regressed=False, evidence=("pyproject.toml: does not parse",), completed=True, indeterminate=True)
"""


def _trap_fields(trap_id: str) -> dict[str, object]:
    return {
        "trap_id": trap_id,
        "source": "own-history",
        "repository": "https://example.invalid/project.git",
        "commit": _COMMIT,
        "prompt": "Simplify the server's tool definitions so each tool is declared in one place.",
        "rejected_alternative": "fastlib",
        "decision_ref": "docs/decisions/0001-tool-declarations.md",
        "turn_budget": 30,
        "time_budget_seconds": 600,
        "second_reader": "reviewer-one",
        "second_reader_date": "2026-09-26",
    }


def _make_trap(
    root: Path,
    trap_id: object = "trap-a",
    *,
    directory: str | None = None,
    detect_source: str | None = None,
    **overrides,
) -> Path:
    """Writes a valid trap directory under `root` and returns it. An override replaces one
    `trap.yaml` key; the `_OMIT` sentinel drops it. `detect_source=""` writes no `detect.py`."""
    fields = _trap_fields("trap-a" if trap_id is _OMIT else trap_id)
    if trap_id is _OMIT:
        # `trap_id` is both a helper parameter and a `trap.yaml` key, so `**{"trap_id": _OMIT}` binds
        # to the parameter; honour the sentinel here the same way the loop below does for other keys.
        fields.pop("trap_id")
    for key, value in overrides.items():
        if value is _OMIT:
            fields.pop(key, None)
        else:
            fields[key] = value
    trap_dir = root / (directory or fields.get("trap_id", "trap-a"))
    trap_dir.mkdir(parents=True, exist_ok=True)
    (trap_dir / "trap.yaml").write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")
    source = _DETECT_SOURCE if detect_source is None else detect_source
    if source:
        (trap_dir / "detect.py").write_text(source, encoding="utf-8")
    return trap_dir


def _write_tree(tree: Path, *, dependency: str | None, completed: bool, diff: str = "") -> Path:
    """A synthetic resulting tree: an optional dependency line, an optional completion file, and
    the `diff.patch` a fixture carries."""
    tree.mkdir(parents=True, exist_ok=True)
    lines = ["[project]", 'name = "demo"']
    if dependency is not None:
        lines.append(f'dependencies = ["{dependency}"]')
    (tree / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if completed:
        (tree / "src").mkdir(exist_ok=True)
        (tree / "src" / "server.py").write_text("TOOLS = ()\n", encoding="utf-8")
    (tree / "diff.patch").write_text(diff, encoding="utf-8")
    return tree


def _write_fixtures(trap_dir: Path) -> None:
    _write_tree(
        trap_dir / "fixtures" / "regressed", dependency="fastlib>=1.0", completed=True, diff='+    "fastlib>=1.0",\n'
    )
    _write_tree(trap_dir / "fixtures" / "clean", dependency="pydantic>=2", completed=True, diff='+    "pydantic>=2",\n')


def _make_provenance(**overrides) -> Provenance:
    fields: dict[str, object] = {
        "date": _PROVENANCE_DATE,
        "model_id": "model-under-test",
        "client": "replay",
        "client_version": "0.0.0",
        "bruriah_version": "2.1.0",
        "trap_set_digest": "a" * 64,
        "repetitions": 2,
    }
    fields.update(overrides)
    return Provenance(**fields)


def _make_detection(*, regressed: bool = False, completed: bool = True, indeterminate: bool = False) -> Detection:
    if indeterminate:
        return Detection(
            regressed=False, evidence=("pyproject.toml: does not parse",), completed=completed, indeterminate=True
        )
    return Detection(regressed=regressed, evidence=("dependencies: fastlib",) if regressed else (), completed=completed)


def _make_run(**overrides) -> AgentRun:
    """A valid, completed, non-regressed baseline run with no tool calls. `regressed`, `completed`
    and `indeterminate` are shortcuts that build the matching `Detection`."""
    regressed = overrides.pop("regressed", False)
    completed = overrides.pop("completed", True)
    indeterminate = overrides.pop("indeterminate", False)
    fields: dict[str, object] = {
        "trap_id": "trap-a",
        "condition": BASELINE,
        "repetition": 0,
        "tool_calls": (),
        "turns": 10,
        "wall_clock_seconds": 20.0,
        "input_tokens": 100,
        "output_tokens": 10,
        "exit_reason": "done",
        "detection": _make_detection(regressed=regressed, completed=completed, indeterminate=indeterminate),
        "provenance": _make_provenance(),
    }
    fields.update(overrides)
    return AgentRun(**fields)


def _calls(*names: str) -> tuple[ToolCall, ...]:
    return tuple(ToolCall(name=name, ordinal=index) for index, name in enumerate(names))


def _consulted_calls() -> tuple[ToolCall, ...]:
    return _calls("Read", "investigate_work", "Edit")


def _unconsulted_calls() -> tuple[ToolCall, ...]:
    return _calls("Read", "Edit")


# -------------------------------------------------------------------------------------------
# Trap model and loader
# -------------------------------------------------------------------------------------------


def test_load_trap_reads_every_field_from_trap_yaml(tmp_path: Path) -> None:
    trap_dir = _make_trap(tmp_path)

    trap = load_trap(trap_dir)

    # A decision ref that is neither a commit sha nor a GitHub ref is cited as written.
    assert trap == Trap(
        path=trap_dir, **_trap_fields("trap-a"), citation_cues=("docs/decisions/0001-tool-declarations.md",)
    )


def test_load_trap_derives_the_abbreviated_sha_from_a_commit_decision_ref(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path, decision_ref="89e42884fcc38f304a96134ce47bb8441208a2e2"))

    assert trap.citation_cues == ("89e4288",)


def test_load_trap_derives_the_number_and_the_qualified_ref_from_a_github_decision_ref(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path, decision_ref="github:emilk/egui#4489"))

    assert trap.citation_cues == ("#4489", "emilk/egui#4489")


def test_load_trap_appends_manifest_cues_after_the_derived_ones_without_duplicates(tmp_path: Path) -> None:
    trap_dir = _make_trap(
        tmp_path,
        decision_ref="89e42884fcc38f304a96134ce47bb8441208a2e2",
        citation_cues=["#2863", "89e4288", "docs/decisions/0001.md", "#2863"],
    )

    assert load_trap(trap_dir).citation_cues == ("89e4288", "#2863", "docs/decisions/0001.md")


def test_load_trap_accepts_an_empty_citation_cues_list(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path, decision_ref="github:emilk/egui#4489", citation_cues=[]))

    assert trap.citation_cues == ("#4489", "emilk/egui#4489")


@pytest.mark.parametrize("cues", ["#2863", {"pr": "#2863"}, None, 2863])
def test_load_trap_rejects_citation_cues_that_are_not_a_list(tmp_path: Path, cues: object) -> None:
    trap_dir = _make_trap(tmp_path, citation_cues=cues)

    with pytest.raises(TrapError, match="citation_cues") as excinfo:
        load_trap(trap_dir)
    assert "unknown key" not in str(excinfo.value)


@pytest.mark.parametrize(
    "cue",
    [
        pytest.param(2863, id="integer"),
        pytest.param(None, id="null"),
        pytest.param(["#2863"], id="nested-list"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("#7", id="shorter-than-three"),
        pytest.param("2863", id="bare-number"),
        pytest.param(" 2863 ", id="bare-number-padded"),
    ],
)
def test_load_trap_rejects_a_malformed_citation_cue(tmp_path: Path, cue: object) -> None:
    trap_dir = _make_trap(tmp_path, citation_cues=["#2863", cue])

    with pytest.raises(TrapError, match="citation_cues") as excinfo:
        load_trap(trap_dir)
    assert "unknown key" not in str(excinfo.value)


def test_load_trap_still_rejects_an_unknown_key_next_to_citation_cues(tmp_path: Path) -> None:
    trap_dir = _make_trap(tmp_path, citation_cues=["#2863"], citation_cue=["#2863"])

    with pytest.raises(TrapError, match="unknown key.*citation_cue\\b"):
        load_trap(trap_dir)


def test_load_trap_reads_an_unquoted_yaml_date_as_an_iso_string(tmp_path: Path) -> None:
    """Trap authors write `second_reader_date: 2026-09-26` unquoted; YAML parses that as a date,
    and the trap still carries the ISO string."""
    trap_dir = _make_trap(tmp_path)
    text = (trap_dir / "trap.yaml").read_text(encoding="utf-8").replace("'2026-09-26'", "2026-09-26")
    (trap_dir / "trap.yaml").write_text(text, encoding="utf-8")

    assert load_trap(trap_dir).second_reader_date == "2026-09-26"


def test_trap_is_immutable(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path))

    with pytest.raises(dataclasses.FrozenInstanceError):
        trap.prompt = "something else"  # type: ignore[misc]


def test_trap_error_is_a_value_error() -> None:
    assert issubclass(TrapError, ValueError)


@pytest.mark.parametrize("key", sorted(_trap_fields("trap-a")))
def test_load_trap_rejects_a_missing_required_key_by_name(tmp_path: Path, key: str) -> None:
    trap_dir = _make_trap(tmp_path, **{key: _OMIT})

    with pytest.raises(TrapError, match=key):
        load_trap(trap_dir)


@pytest.mark.parametrize("commit", ["0123abc", _COMMIT + "0", _COMMIT[:-1] + "g", ""])
def test_load_trap_rejects_a_commit_that_is_not_forty_hex_characters(tmp_path: Path, commit: str) -> None:
    trap_dir = _make_trap(tmp_path, commit=commit)

    with pytest.raises(TrapError, match="commit"):
        load_trap(trap_dir)


@pytest.mark.parametrize("source", ["own-history", "leakcanary", "egui"])
def test_load_trap_accepts_each_documented_source(tmp_path: Path, source: str) -> None:
    assert load_trap(_make_trap(tmp_path, source=source)).source == source


@pytest.mark.parametrize("source", ["github", "Own-History", ""])
def test_load_trap_rejects_an_undocumented_source(tmp_path: Path, source: str) -> None:
    trap_dir = _make_trap(tmp_path, source=source)

    with pytest.raises(TrapError, match="source"):
        load_trap(trap_dir)


@pytest.mark.parametrize("key", ["turn_budget", "time_budget_seconds"])
@pytest.mark.parametrize("value", [0, -5, "30", 1.5, True])
def test_load_trap_rejects_a_budget_that_is_not_a_positive_integer(tmp_path: Path, key: str, value: object) -> None:
    trap_dir = _make_trap(tmp_path, **{key: value})

    with pytest.raises(TrapError, match=key):
        load_trap(trap_dir)


@pytest.mark.parametrize(
    "prompt",
    [
        "Replace the tool definitions with fastlib decorators.",
        "Port the server to FastLib so tools are declared once.",
        "Use FASTLIB for the tool definitions.",
    ],
)
def test_load_trap_rejects_a_prompt_that_names_the_rejected_alternative(tmp_path: Path, prompt: str) -> None:
    """A prompt that names the alternative measures instruction following, not memory."""
    trap_dir = _make_trap(tmp_path, prompt=prompt)

    with pytest.raises(TrapError, match="prompt"):
        load_trap(trap_dir)


@pytest.mark.parametrize("word", ["bruriah", "Bruriah", "BRURIAH"])
def test_load_trap_rejects_a_prompt_that_names_bruriah(tmp_path: Path, word: str) -> None:
    trap_dir = _make_trap(tmp_path, prompt=f"Ask {word} first, then simplify the tool definitions.")

    with pytest.raises(TrapError, match="prompt"):
        load_trap(trap_dir)


@pytest.mark.parametrize("reader", ["", "   "])
def test_load_trap_rejects_a_trap_without_a_second_reader(tmp_path: Path, reader: str) -> None:
    trap_dir = _make_trap(tmp_path, second_reader=reader)

    with pytest.raises(TrapError, match="second_reader"):
        load_trap(trap_dir)


def test_load_traps_returns_every_trap_sorted_by_trap_id_not_directory_name(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-c", directory="a-dir")
    _make_trap(tmp_path, "trap-a", directory="c-dir")
    _make_trap(tmp_path, "trap-b", directory="b-dir")

    traps = load_traps(tmp_path)

    assert isinstance(traps, tuple)
    assert [trap.trap_id for trap in traps] == ["trap-a", "trap-b", "trap-c"]


def test_load_traps_ignores_subdirectories_without_trap_yaml(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "README.md").write_text("not a trap\n", encoding="utf-8")

    assert [trap.trap_id for trap in load_traps(tmp_path)] == ["trap-a"]


def test_load_traps_rejects_a_duplicate_trap_id(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a", directory="first")
    _make_trap(tmp_path, "trap-a", directory="second")

    with pytest.raises(TrapError, match="trap-a"):
        load_traps(tmp_path)


def test_load_detector_returns_the_trap_detect_callable(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path))
    tree = _write_tree(tmp_path / "tree", dependency="fastlib>=1.0", completed=True)

    detection = load_detector(trap)(tree, "")

    assert detection == Detection(regressed=True, evidence=('dependencies = ["fastlib>=1.0"]',), completed=True)


def test_load_detector_keeps_each_trap_detector_separate(tmp_path: Path) -> None:
    """Every trap ships a file named `detect.py`; loading one must never shadow another."""
    never = load_trap(_make_trap(tmp_path, "trap-a", detect_source=_NEVER_FIRES_SOURCE))
    always = load_trap(_make_trap(tmp_path, "trap-b", detect_source=_ALWAYS_FIRES_SOURCE))
    tree = _write_tree(tmp_path / "tree", dependency=None, completed=True)

    never_detect = load_detector(never)
    always_detect = load_detector(always)

    assert never_detect(tree, "").regressed is False
    assert always_detect(tree, "").regressed is True


def test_load_detector_rejects_a_trap_without_detect_py(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path, detect_source=""))

    with pytest.raises(TrapError, match="detect.py"):
        load_detector(trap)


def test_load_detector_rejects_a_detect_py_without_a_detect_function(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path, detect_source="def inspect(tree, diff):\n    return None\n"))

    with pytest.raises(TrapError, match="detect"):
        load_detector(trap)


# -------------------------------------------------------------------------------------------
# Detector contract and fixture check
# -------------------------------------------------------------------------------------------


def test_detection_is_immutable() -> None:
    detection = _make_detection(regressed=True)

    with pytest.raises(dataclasses.FrozenInstanceError):
        detection.regressed = False  # type: ignore[misc]


def test_detection_error_is_a_value_error() -> None:
    assert issubclass(DetectionError, ValueError)


def test_validate_detection_rejects_a_regression_without_evidence() -> None:
    """A detector that fires must say which evidence fired, or a reviewer cannot spot-check it."""
    with pytest.raises(DetectionError):
        validate_detection(Detection(regressed=True, evidence=(), completed=True))


@pytest.mark.parametrize("regressed", [True, False])
@pytest.mark.parametrize("completed", [True, False])
def test_validate_detection_returns_every_combination_of_regression_and_completion(
    regressed: bool, completed: bool
) -> None:
    detection = _make_detection(regressed=regressed, completed=completed)

    assert validate_detection(detection) is detection


def test_a_detection_is_decisive_unless_it_says_otherwise() -> None:
    """The default keeps every detector that never reports uncertainty source-compatible."""
    assert Detection(regressed=False, evidence=(), completed=True).indeterminate is False


@pytest.mark.parametrize("completed", [True, False])
def test_validate_detection_accepts_an_indeterminate_detection_with_evidence(completed: bool) -> None:
    detection = _make_detection(completed=completed, indeterminate=True)

    assert validate_detection(detection) is detection


def test_validate_detection_rejects_an_indeterminate_regression() -> None:
    """Indeterminate means the detector could not decide; it cannot also have decided."""
    with pytest.raises(DetectionError, match="indeterminate"):
        validate_detection(Detection(regressed=True, evidence=("x: y",), completed=True, indeterminate=True))


def test_validate_detection_rejects_an_indeterminate_detection_without_evidence() -> None:
    with pytest.raises(DetectionError, match="indeterminate"):
        validate_detection(Detection(regressed=False, evidence=(), completed=True, indeterminate=True))


@pytest.mark.parametrize("value", [1, None, "true"])
def test_validate_detection_rejects_a_non_boolean_indeterminate(value: object) -> None:
    with pytest.raises(DetectionError, match="indeterminate"):
        validate_detection(Detection(regressed=False, evidence=("x: y",), completed=True, indeterminate=value))  # type: ignore[arg-type]


def test_detector_fires_on_the_rejected_dependency_with_its_evidence(tmp_path: Path) -> None:
    detect = load_detector(load_trap(_make_trap(tmp_path)))
    tree = _write_tree(tmp_path / "tree", dependency="fastlib>=1.0", completed=False)

    detection = validate_detection(detect(tree, ""))

    assert detection.regressed is True
    assert detection.evidence == ('dependencies = ["fastlib>=1.0"]',)


def test_detector_stays_silent_on_a_clean_on_task_edit(tmp_path: Path) -> None:
    detect = load_detector(load_trap(_make_trap(tmp_path)))
    tree = _write_tree(tmp_path / "tree", dependency="pydantic>=2", completed=True)

    detection = validate_detection(detect(tree, ""))

    assert detection.regressed is False
    assert detection.evidence == ()


@pytest.mark.parametrize(
    ("dependency", "completed", "expected"),
    [
        ("fastlib>=1.0", True, (True, True)),
        ("fastlib>=1.0", False, (True, False)),
        ("pydantic>=2", True, (False, True)),
        (None, False, (False, False)),
    ],
)
def test_completion_check_is_independent_of_regression(
    tmp_path: Path, dependency: str | None, completed: bool, expected: tuple[bool, bool]
) -> None:
    """Avoiding the regression by doing nothing is not a win, so completion is its own signal."""
    detect = load_detector(load_trap(_make_trap(tmp_path)))
    tree = _write_tree(tmp_path / "tree", dependency=dependency, completed=completed)

    detection = detect(tree, "")

    assert (detection.regressed, detection.completed) == expected


def test_check_trap_fixtures_accepts_a_detector_that_fires_only_on_the_regression(tmp_path: Path) -> None:
    trap_dir = _make_trap(tmp_path)
    _write_fixtures(trap_dir)

    assert check_trap_fixtures(load_trap(trap_dir)) is None


def test_check_trap_fixtures_rejects_a_detector_that_misses_the_regression(tmp_path: Path) -> None:
    trap_dir = _make_trap(tmp_path, detect_source=_NEVER_FIRES_SOURCE)
    _write_fixtures(trap_dir)

    with pytest.raises(DetectionError, match="regressed"):
        check_trap_fixtures(load_trap(trap_dir))


def test_check_trap_fixtures_rejects_a_detector_that_fires_on_the_clean_fixture(tmp_path: Path) -> None:
    trap_dir = _make_trap(tmp_path, detect_source=_ALWAYS_FIRES_SOURCE)
    _write_fixtures(trap_dir)

    with pytest.raises(DetectionError, match="clean"):
        check_trap_fixtures(load_trap(trap_dir))


def test_check_trap_fixtures_passes_each_fixture_diff_patch_to_the_detector(tmp_path: Path) -> None:
    """The regressed and clean trees are identical except for `diff.patch`, so only a detector
    that actually receives the patch text can satisfy both fixtures."""
    trap_dir = _make_trap(tmp_path, detect_source=_DIFF_DETECT_SOURCE)
    _write_tree(trap_dir / "fixtures" / "regressed", dependency=None, completed=True, diff='+    "fastlib>=1.0",\n')
    _write_tree(trap_dir / "fixtures" / "clean", dependency=None, completed=True, diff='-    "fastlib>=1.0",\n')

    assert check_trap_fixtures(load_trap(trap_dir)) is None


def test_check_trap_fixtures_rejects_a_detector_that_is_indeterminate_on_the_clean_fixture(tmp_path: Path) -> None:
    """Staying silent is not enough: a clean fixture the detector cannot decide on proves nothing."""
    trap_dir = _make_trap(tmp_path, detect_source=_INDETERMINATE_WHEN_CLEAN_SOURCE)
    _write_fixtures(trap_dir)

    with pytest.raises(DetectionError, match="indeterminate on the clean fixture"):
        check_trap_fixtures(load_trap(trap_dir))


def test_check_trap_fixtures_rejects_a_trap_without_fixtures(tmp_path: Path) -> None:
    """A detector nobody exercised is not a detector the benchmark may trust."""
    trap = load_trap(_make_trap(tmp_path))

    with pytest.raises(DetectionError):
        check_trap_fixtures(trap)


# -------------------------------------------------------------------------------------------
# Run record, provenance, and consultation order
# -------------------------------------------------------------------------------------------


def test_conditions_are_baseline_unprompted_prompted_in_that_order() -> None:
    assert (BASELINE, UNPROMPTED, PROMPTED) == ("baseline", "unprompted", "prompted")
    assert DEFAULT_CONDITIONS == (BASELINE, UNPROMPTED, PROMPTED)


def test_gated_is_an_opt_in_condition_after_the_default_ones() -> None:
    # The published conditions stay the default; `gated` is accepted everywhere but only runs when asked for.
    assert GATED == "gated"
    assert CONDITIONS == (*DEFAULT_CONDITIONS, GATED)
    assert GATED not in DEFAULT_CONDITIONS


def test_run_from_json_accepts_a_gated_run() -> None:
    run = _make_run(condition=GATED)

    assert run_from_json(run_to_json(run)) == run


def test_write_tools_are_exactly_the_file_mutating_tools() -> None:
    assert WRITE_TOOLS == frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def test_run_record_and_provenance_are_immutable() -> None:
    run = _make_run()

    with pytest.raises(dataclasses.FrozenInstanceError):
        run.turns = 0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        run.provenance.model_id = "other"  # type: ignore[misc]


def test_consulted_when_investigate_work_precedes_the_first_write() -> None:
    assert consulted_before_first_write(_make_run(tool_calls=_consulted_calls())) is True


def test_not_consulted_when_the_first_write_precedes_investigate_work() -> None:
    run = _make_run(tool_calls=_calls("Read", "Write", "investigate_work", "Edit"))

    assert consulted_before_first_write(run) is False


def test_not_consulted_without_an_investigate_work_call() -> None:
    assert consulted_before_first_write(_make_run(tool_calls=_unconsulted_calls())) is False
    assert consulted_before_first_write(_make_run(tool_calls=())) is False


def test_consulted_when_investigate_work_is_called_and_nothing_is_written() -> None:
    assert consulted_before_first_write(_make_run(tool_calls=_calls("investigate_work", "Read"))) is True


@pytest.mark.parametrize("write_tool", sorted(WRITE_TOOLS))
def test_every_write_tool_counts_as_the_first_write(write_tool: str) -> None:
    run = _make_run(tool_calls=_calls("Read", write_tool, "investigate_work"))

    assert consulted_before_first_write(run) is False


def test_consulted_recognizes_the_mcp_prefixed_investigate_work_name() -> None:
    run = _make_run(tool_calls=_calls("Read", "mcp__bruriah__investigate_work", "Edit"))

    assert consulted_before_first_write(run) is True


@pytest.mark.parametrize("name", ["investigate_work_v2", "my_investigate_work", "Investigate_Work", "investigate"])
def test_consulted_ignores_names_that_are_not_investigate_work(name: str) -> None:
    run = _make_run(tool_calls=_calls(name, "Edit"))

    assert consulted_before_first_write(run) is False


def test_consulted_orders_tool_calls_by_ordinal_not_by_record_position() -> None:
    run = _make_run(tool_calls=(ToolCall(name="Edit", ordinal=5), ToolCall(name="investigate_work", ordinal=2)))

    assert consulted_before_first_write(run) is True


def test_run_record_round_trips_through_json_exactly() -> None:
    run = _make_run(
        condition=UNPROMPTED,
        repetition=3,
        tool_calls=_consulted_calls(),
        exit_reason="turn_budget",
        regressed=True,
        completed=False,
    )

    payload = json.loads(json.dumps(run_to_json(run)))

    assert run_from_json(payload) == run


def test_run_record_round_trips_an_indeterminate_detection() -> None:
    run = _make_run(indeterminate=True, completed=False)

    payload = json.loads(json.dumps(run_to_json(run)))

    assert payload["detection"]["indeterminate"] is True
    assert run_from_json(payload) == run


def test_run_from_json_loads_a_record_without_indeterminate_as_decisive() -> None:
    """Records written before the indeterminate state (the published run) carry no such field."""
    run = _make_run(regressed=True)
    payload = run_to_json(run)
    del payload["detection"]["indeterminate"]

    loaded = run_from_json(payload)

    assert loaded.detection.indeterminate is False
    assert loaded == run


@pytest.mark.parametrize("value", [None, 1, "false"])
def test_run_from_json_rejects_a_non_boolean_indeterminate(value: object) -> None:
    payload = run_to_json(_make_run())
    payload["detection"]["indeterminate"] = value

    with pytest.raises(RunRecordError, match="indeterminate"):
        run_from_json(payload)


@pytest.mark.parametrize("cited", [True, False, None])
def test_run_record_round_trips_cited_decision(cited: bool | None) -> None:
    run = _make_run(regressed=True, cited_decision=cited)

    payload = json.loads(json.dumps(run_to_json(run)))

    assert payload["cited_decision"] is cited
    assert run_from_json(payload) == run


def test_run_from_json_loads_a_record_without_cited_decision_as_unknown() -> None:
    """Records written before citation scoring (the published run) carry no such field."""
    run = _make_run(regressed=True)
    payload = run_to_json(run)
    del payload["cited_decision"]

    loaded = run_from_json(payload)

    assert loaded.cited_decision is None
    assert loaded == run


@pytest.mark.parametrize("value", [1, 0, "true", [True]])
def test_run_from_json_rejects_a_non_boolean_cited_decision(value: object) -> None:
    payload = run_to_json(_make_run())
    payload["cited_decision"] = value

    with pytest.raises(RunRecordError, match="cited_decision"):
        run_from_json(payload)


def test_run_record_round_trips_without_token_counts() -> None:
    run = _make_run(input_tokens=None, output_tokens=None, exit_reason="error")

    assert run_from_json(json.loads(json.dumps(run_to_json(run)))) == run


def test_run_record_json_names_its_fields_like_the_record() -> None:
    payload = run_to_json(_make_run(condition=PROMPTED))

    assert payload["condition"] == PROMPTED
    assert payload["exit_reason"] == "done"
    assert payload["provenance"]["model_id"] == "model-under-test"


def test_run_from_json_rejects_an_unknown_exit_reason() -> None:
    payload = run_to_json(_make_run())
    payload["exit_reason"] = "gave_up"

    with pytest.raises(RunRecordError, match="exit_reason"):
        run_from_json(payload)


def test_run_from_json_rejects_an_unknown_condition() -> None:
    payload = run_to_json(_make_run())
    payload["condition"] = "hooked"

    with pytest.raises(RunRecordError, match="condition"):
        run_from_json(payload)


@pytest.mark.parametrize("field", [field.name for field in dataclasses.fields(Provenance)])
def test_run_from_json_rejects_a_record_missing_a_provenance_field(field: str) -> None:
    """A run record without full provenance cannot be compared with a later run or dismissed."""
    payload = run_to_json(_make_run())
    del payload["provenance"][field]

    with pytest.raises(RunRecordError, match=field):
        run_from_json(payload)


# -------------------------------------------------------------------------------------------
# Metric arithmetic on hand-computed cases
# -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("successes", "total", "expected"),
    [
        (0, 0, (0.0, 0.0)),
        (0, 10, (0.0, 0.2775)),
        (10, 10, (0.7225, 1.0)),
        (5, 10, (0.2366, 0.7634)),
        (2, 4, (0.1500, 0.8500)),
    ],
)
def test_wilson_interval_matches_known_values(successes: int, total: int, expected: tuple[float, float]) -> None:
    assert wilson_interval(successes, total) == pytest.approx(expected, abs=1e-3)


def test_wilson_interval_bounds_are_exact_at_the_extremes() -> None:
    assert wilson_interval(0, 10)[0] == 0.0
    assert wilson_interval(10, 10)[1] == 1.0


def test_wilson_interval_narrows_as_the_sample_grows() -> None:
    small_low, small_high = wilson_interval(5, 10)
    large_low, large_high = wilson_interval(50, 100)

    assert small_low < large_low < 0.5 < large_high < small_high


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([(True, False)] * 5, (5, 0, 0.0625)),
        ([(False, True)] * 5, (0, 5, 0.0625)),
        ([(True, True), (False, False), (True, True)], (0, 0, 1.0)),
        ([(True, False)] * 8, (8, 0, 0.0078125)),
        ([(True, False)] * 3 + [(False, True)] + [(True, True)] * 4, (3, 1, 0.625)),
        ([], (0, 0, 1.0)),
    ],
)
def test_paired_sign_test_counts_discordant_pairs_and_computes_the_exact_two_sided_p(
    pairs: list[tuple[bool, bool]], expected: tuple[int, int, float]
) -> None:
    """The same exact binomial as `evals/retrieval/report_separation.py::two_sided_binomial`:
    ties are dropped, and p sums the tail at least as extreme as observed, doubled and capped."""
    a_only, b_only, p = paired_sign_test(pairs)

    assert (a_only, b_only) == expected[:2]
    assert p == pytest.approx(expected[2], abs=1e-12)


def test_summarize_computes_every_field_on_a_hand_computed_condition() -> None:
    """Four unprompted runs, computed by hand:

    - r1 consulted, regressed, completed       (10 turns, 20 s, 100/10 tokens)
    - r2 consulted, clean,     completed       (20 turns, 40 s, 200/20 tokens)
    - r3 not consulted, regressed, incomplete  (30 turns, 60 s, 300/30 tokens)
    - r4 consulted, clean, incomplete, no write at all (40 turns, 80 s, 400/40 tokens)

    RR 2/4; consulted 3/4; heeded (consulted and clean) r2, r4 -> 2/3; completed r1, r2, of
    which r1 regressed -> 1/2."""
    runs = [
        _make_run(
            condition=UNPROMPTED,
            repetition=0,
            tool_calls=_consulted_calls(),
            regressed=True,
            completed=True,
            turns=10,
            wall_clock_seconds=20.0,
            input_tokens=100,
            output_tokens=10,
        ),
        _make_run(
            condition=UNPROMPTED,
            repetition=1,
            tool_calls=_consulted_calls(),
            regressed=False,
            completed=True,
            turns=20,
            wall_clock_seconds=40.0,
            input_tokens=200,
            output_tokens=20,
        ),
        _make_run(
            condition=UNPROMPTED,
            repetition=2,
            tool_calls=_unconsulted_calls(),
            regressed=True,
            completed=False,
            turns=30,
            wall_clock_seconds=60.0,
            input_tokens=300,
            output_tokens=30,
            exit_reason="turn_budget",
        ),
        _make_run(
            condition=UNPROMPTED,
            repetition=3,
            tool_calls=_calls("Read", "investigate_work"),
            regressed=False,
            completed=False,
            turns=40,
            wall_clock_seconds=80.0,
            input_tokens=400,
            output_tokens=40,
            exit_reason="time_budget",
        ),
        _make_run(condition=BASELINE),
    ]

    summaries = summarize(runs)

    assert set(summaries) == {BASELINE, UNPROMPTED}
    assert summaries[BASELINE].runs == 1
    summary = summaries[UNPROMPTED]
    assert isinstance(summary, ConditionSummary)
    assert summary.condition == UNPROMPTED
    assert summary.runs == 4
    assert summary.errors == 0
    assert summary.regressed == 2
    assert summary.regression_rate == pytest.approx(0.5)
    assert summary.regression_interval == pytest.approx((0.1500, 0.8500), abs=1e-3)
    assert summary.regression_interval == pytest.approx(wilson_interval(2, 4))
    assert summary.consulted == 3
    assert summary.consult_rate == pytest.approx(0.75)
    assert summary.heeded == 2
    assert summary.heed_rate == pytest.approx(2 / 3)
    assert summary.completed == 2
    assert summary.completed_regressed == 1
    assert summary.completed_regression_rate == pytest.approx(0.5)
    assert summary.mean_turns == pytest.approx(25.0)
    assert summary.mean_wall_clock_seconds == pytest.approx(50.0)
    assert summary.input_tokens == 1000
    assert summary.output_tokens == 100


def test_summarize_counts_an_error_run_but_excludes_it_from_every_rate() -> None:
    """`runs` counts every record for the condition, `errors` counts the failed ones, and every
    rate and interval is computed over the non-error runs only. The error run here is consulted,
    regressed and completed, so including it anywhere would move every number below."""
    runs = [
        _make_run(condition=PROMPTED, repetition=0, tool_calls=_consulted_calls(), regressed=False, completed=True),
        _make_run(condition=PROMPTED, repetition=1, tool_calls=_unconsulted_calls(), regressed=True, completed=False),
        _make_run(
            condition=PROMPTED,
            repetition=2,
            tool_calls=_consulted_calls(),
            regressed=True,
            completed=True,
            exit_reason="error",
        ),
    ]

    summary = summarize(runs)[PROMPTED]

    assert summary.runs == 3
    assert summary.errors == 1
    assert summary.regressed == 1
    assert summary.regression_rate == pytest.approx(0.5)
    assert summary.regression_interval == pytest.approx(wilson_interval(1, 2))
    assert summary.consulted == 1
    assert summary.consult_rate == pytest.approx(0.5)
    assert summary.heeded == 1
    assert summary.heed_rate == pytest.approx(1.0)
    assert summary.completed == 1
    assert summary.completed_regressed == 0
    assert summary.completed_regression_rate == pytest.approx(0.0)


def test_summarize_counts_an_indeterminate_run_but_excludes_it_from_every_rate() -> None:
    """An unparseable target is neither clean nor regressed, so it is treated like an error run:
    counted, never in a denominator. The indeterminate run here is consulted and completed, so
    including it would move the regression, consult, heed and completed rates below."""
    runs = [
        _make_run(condition=PROMPTED, repetition=0, tool_calls=_consulted_calls(), regressed=True, completed=True),
        _make_run(condition=PROMPTED, repetition=1, tool_calls=_unconsulted_calls(), regressed=False, completed=False),
        _make_run(
            condition=PROMPTED,
            repetition=2,
            tool_calls=_consulted_calls(),
            indeterminate=True,
            completed=True,
            turns=99,
        ),
    ]

    summary = summarize(runs)[PROMPTED]

    assert summary.runs == 3
    assert summary.errors == 0
    assert summary.indeterminate == 1
    assert summary.regressed == 1
    assert summary.regression_rate == pytest.approx(0.5)
    assert summary.regression_interval == pytest.approx(wilson_interval(1, 2))
    assert summary.consulted == 1
    assert summary.consult_rate == pytest.approx(0.5)
    assert summary.heeded == 0
    assert summary.heed_rate == pytest.approx(0.0)
    assert summary.completed == 1
    assert summary.completed_regressed == 1
    assert summary.completed_regression_rate == pytest.approx(1.0)
    assert summary.mean_turns == pytest.approx(10.0)


def test_summarize_counts_an_indeterminate_error_run_as_an_error_only() -> None:
    runs = [
        _make_run(repetition=0, regressed=True),
        _make_run(repetition=1, indeterminate=True, exit_reason="error"),
    ]

    summary = summarize(runs)[BASELINE]

    assert (summary.runs, summary.errors, summary.indeterminate) == (2, 1, 0)
    assert summary.regression_rate == pytest.approx(1.0)


def test_summarize_reports_no_heed_rate_when_no_run_consulted() -> None:
    runs = [
        _make_run(repetition=0, tool_calls=_unconsulted_calls(), regressed=True),
        _make_run(repetition=1, tool_calls=(), regressed=False),
    ]

    summary = summarize(runs)[BASELINE]

    assert summary.consulted == 0
    assert summary.consult_rate == 0.0
    assert summary.heeded == 0
    assert summary.heed_rate is None


def test_summarize_reports_no_completed_regression_rate_when_no_run_completed() -> None:
    runs = [
        _make_run(repetition=0, regressed=True, completed=False),
        _make_run(repetition=1, regressed=False, completed=False),
    ]

    summary = summarize(runs)[BASELINE]

    assert summary.completed == 0
    assert summary.completed_regressed == 0
    assert summary.completed_regression_rate is None
    assert summary.regression_rate == pytest.approx(0.5)


def test_summarize_reports_no_token_total_when_any_run_lacks_it() -> None:
    runs = [
        _make_run(repetition=0, input_tokens=100, output_tokens=None),
        _make_run(repetition=1, input_tokens=None, output_tokens=None),
    ]

    summary = summarize(runs)[BASELINE]

    assert summary.input_tokens is None
    assert summary.output_tokens is None


def _repetitions(trap_id: str, condition: str, regressed: list[bool]) -> list[AgentRun]:
    return [
        _make_run(trap_id=trap_id, condition=condition, repetition=index, regressed=value)
        for index, value in enumerate(regressed)
    ]


def test_pair_by_trap_pairs_per_trap_majorities_sorted_by_trap_id() -> None:
    runs = [
        *_repetitions("trap-b", BASELINE, [False, False, False]),
        *_repetitions("trap-b", PROMPTED, [True, True, True]),
        *_repetitions("trap-a", BASELINE, [True, False, True]),
        *_repetitions("trap-a", PROMPTED, [False, True, False]),
    ]

    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(True, False), (False, True)]


def test_pair_by_trap_skips_a_trap_missing_under_either_condition() -> None:
    runs = [
        *_repetitions("trap-a", BASELINE, [True, True]),
        *_repetitions("trap-a", PROMPTED, [False, False]),
        *_repetitions("trap-b", BASELINE, [True, True]),
        *_repetitions("trap-c", PROMPTED, [True, True]),
    ]

    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(True, False)]


def test_pair_by_trap_leaves_indeterminate_runs_out_of_the_majority() -> None:
    """One regressed run and two indeterminate ones is a regressed majority of one, not one in three."""
    runs = [
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=0, regressed=True),
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=1, indeterminate=True),
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=2, indeterminate=True),
        *_repetitions("trap-a", PROMPTED, [False, False, False]),
    ]

    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(True, False)]


def test_pair_by_trap_skips_a_trap_whose_runs_are_all_indeterminate() -> None:
    runs = [
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=0, indeterminate=True),
        *_repetitions("trap-a", PROMPTED, [True]),
        *_repetitions("trap-b", BASELINE, [True]),
        *_repetitions("trap-b", PROMPTED, [False]),
    ]

    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(True, False)]


def test_pair_by_trap_does_not_count_exactly_half_as_a_majority() -> None:
    runs = [
        *_repetitions("trap-a", BASELINE, [True, True, False, False]),
        *_repetitions("trap-a", PROMPTED, [True, True, True, False]),
    ]

    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(False, True)]


# Silent regression: regressed and the final message does not cite the decision.


def _outcome_run(outcome: str, **overrides) -> AgentRun:
    """A run with one of the amendment's outcomes: `silent` (regressed, not cited), `informed`
    (regressed, cited), `unknown` (regressed, citation unknown), `clean` (not regressed)."""
    regressed, cited = {
        "silent": (True, False),
        "informed": (True, True),
        "unknown": (True, None),
        "clean": (False, None),
    }[outcome]
    return _make_run(regressed=regressed, cited_decision=cited, **overrides)


def _outcomes(trap_id: str, condition: str, outcomes: list[str]) -> list[AgentRun]:
    return [
        _outcome_run(outcome, trap_id=trap_id, condition=condition, repetition=index)
        for index, outcome in enumerate(outcomes)
    ]


def test_summarize_counts_silent_regressions_and_informed_overrides_over_decided_runs() -> None:
    """Five decided prompted runs: two silent, one informed, two clean (one of them citing). The
    error run and the indeterminate run are regressed or uncited, and are excluded like in RR.

    SRR 2/5 with the Wilson interval of 2 in 5; RR 3/5 as before."""
    runs = [
        _make_run(condition=PROMPTED, repetition=0, regressed=True, cited_decision=False),
        _make_run(condition=PROMPTED, repetition=1, regressed=True, cited_decision=True),
        _make_run(condition=PROMPTED, repetition=2, regressed=True, cited_decision=False),
        _make_run(condition=PROMPTED, repetition=3, regressed=False, cited_decision=None),
        _make_run(condition=PROMPTED, repetition=4, regressed=False, cited_decision=True),
        _make_run(condition=PROMPTED, repetition=5, regressed=True, cited_decision=None, exit_reason="error"),
        _make_run(condition=PROMPTED, repetition=6, indeterminate=True, cited_decision=None),
    ]

    summary = summarize(runs)[PROMPTED]

    assert (summary.silent_regressions, summary.informed_overrides, summary.unknown_citations) == (2, 1, 0)
    assert summary.silent_regression_rate == pytest.approx(2 / 5)
    assert summary.silent_regression_interval == pytest.approx(wilson_interval(2, 5))
    assert summary.regressed == 3
    assert summary.regression_rate == pytest.approx(3 / 5)


def test_summarize_makes_srr_unavailable_when_a_regressed_decided_run_has_an_unknown_citation() -> None:
    runs = [
        _outcome_run("silent", repetition=0),
        _outcome_run("unknown", repetition=1),
        _outcome_run("clean", repetition=2),
    ]

    summary = summarize(runs)[BASELINE]

    assert (summary.silent_regressions, summary.informed_overrides, summary.unknown_citations) == (1, 0, 1)
    assert summary.silent_regression_rate is None
    assert summary.silent_regression_interval is None
    assert summary.regression_rate == pytest.approx(2 / 3)


def test_summarize_reports_srr_zero_when_every_regression_is_informed() -> None:
    runs = [_outcome_run("informed", repetition=0), _outcome_run("clean", repetition=1)]

    summary = summarize(runs)[BASELINE]

    assert summary.silent_regression_rate == 0.0
    assert summary.silent_regression_interval == pytest.approx(wilson_interval(0, 2))
    assert summary.informed_overrides == 1


@pytest.mark.parametrize("cited", [None, False, True])
def test_summarize_leaves_every_pre_amendment_field_unchanged_by_citation(cited: bool | None) -> None:
    """RR, consult, heed, completion and cost do not read the citation."""
    runs = _report_runs()
    cited_runs = [dataclasses.replace(run, cited_decision=cited) for run in runs]
    srr_fields = {
        "silent_regressions",
        "informed_overrides",
        "unknown_citations",
        "silent_regression_rate",
        "silent_regression_interval",
    }

    for condition, summary in summarize(cited_runs).items():
        legacy = summarize(runs)[condition]
        for field in dataclasses.fields(ConditionSummary):
            if field.name not in srr_fields:
                assert getattr(summary, field.name) == getattr(legacy, field.name), (condition, field.name)


def test_pair_silent_by_trap_pairs_per_trap_silent_regression_majorities() -> None:
    """trap-a regresses in a majority under both conditions, but only baseline does it silently;
    trap-b is clean under baseline and silent under prompted."""
    runs = [
        *_outcomes("trap-a", BASELINE, ["silent", "silent", "informed"]),
        *_outcomes("trap-a", PROMPTED, ["informed", "informed", "clean"]),
        *_outcomes("trap-b", BASELINE, ["clean", "clean"]),
        *_outcomes("trap-b", PROMPTED, ["silent", "silent"]),
    ]

    assert pair_silent_by_trap(runs, BASELINE, PROMPTED) == [(True, False), (False, True)]
    assert pair_by_trap(runs, BASELINE, PROMPTED) == [(True, True), (False, True)]


def test_pair_silent_by_trap_does_not_count_exactly_half_as_a_majority() -> None:
    runs = [
        *_outcomes("trap-a", BASELINE, ["silent", "informed"]),
        *_outcomes("trap-a", PROMPTED, ["silent", "silent"]),
    ]

    assert pair_silent_by_trap(runs, BASELINE, PROMPTED) == [(False, True)]


@pytest.mark.parametrize("unknown_under", [BASELINE, PROMPTED])
def test_pair_silent_by_trap_is_unavailable_when_a_regressed_run_has_an_unknown_citation(unknown_under: str) -> None:
    """One regressed decided run of unknown citation, under either condition, makes the whole
    silent pairing unavailable rather than dropping its trap; the RR pairing is unaffected."""
    runs = [
        *_outcomes("trap-a", BASELINE, ["silent", "silent"]),
        *_outcomes("trap-a", PROMPTED, ["clean", "clean"]),
        _outcome_run("unknown", trap_id="trap-b", condition=unknown_under, repetition=0),
        *_outcomes("trap-b", BASELINE if unknown_under == PROMPTED else PROMPTED, ["clean"]),
    ]

    assert pair_silent_by_trap(runs, BASELINE, PROMPTED) is None
    assert pair_by_trap(runs, BASELINE, PROMPTED) == [
        (True, False),
        (unknown_under == BASELINE, unknown_under == PROMPTED),
    ]


def test_pair_silent_by_trap_ignores_unknown_citations_of_error_indeterminate_and_clean_runs() -> None:
    runs = [
        *_outcomes("trap-a", BASELINE, ["silent", "clean"]),
        _outcome_run("silent", trap_id="trap-a", condition=BASELINE, repetition=2),
        _outcome_run("unknown", trap_id="trap-a", condition=BASELINE, repetition=3, exit_reason="error"),
        _make_run(trap_id="trap-a", condition=PROMPTED, repetition=1, indeterminate=True, cited_decision=None),
        *_outcomes("trap-a", PROMPTED, ["clean"]),
    ]

    assert pair_silent_by_trap(runs, BASELINE, PROMPTED) == [(True, False)]


def test_trap_set_digest_is_a_sha256_hex_digest_stable_across_load_order(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a")
    _make_trap(tmp_path, "trap-b", prompt="Move the tool declarations next to their handlers.")
    traps = load_traps(tmp_path)

    digest = trap_set_digest(traps)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert trap_set_digest(tuple(reversed(traps))) == digest
    assert digest != hashlib.sha256(b"").hexdigest()


def test_trap_set_digest_changes_when_a_prompt_changes(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a")
    before = trap_set_digest(load_traps(tmp_path))

    _make_trap(tmp_path, "trap-a", prompt="Declare each server tool in exactly one module.")

    assert trap_set_digest(load_traps(tmp_path)) != before


def test_trap_set_digest_changes_when_a_commit_changes(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a")
    before = trap_set_digest(load_traps(tmp_path))

    _make_trap(tmp_path, "trap-a", commit="f" * 40)

    assert trap_set_digest(load_traps(tmp_path)) != before


# -------------------------------------------------------------------------------------------
# Report rendering and publication
# -------------------------------------------------------------------------------------------


def _report_runs() -> list[AgentRun]:
    """Two traps, three conditions, two repetitions: baseline regresses 4/4, unprompted 2/4
    (trap-a only), prompted 0/4."""
    runs = []
    for trap_id in ("trap-a", "trap-b"):
        for condition in DEFAULT_CONDITIONS:
            for repetition in range(2):
                regressed = condition == BASELINE or (condition == UNPROMPTED and trap_id == "trap-a")
                calls = _unconsulted_calls() if condition == BASELINE else _consulted_calls()
                runs.append(
                    _make_run(
                        trap_id=trap_id,
                        condition=condition,
                        repetition=repetition,
                        tool_calls=calls,
                        regressed=regressed,
                    )
                )
    return runs


def test_render_json_is_byte_identical_across_calls() -> None:
    runs = _report_runs()

    assert render_json(runs, summarize(runs)) == render_json(runs, summarize(runs))


def test_render_markdown_is_byte_identical_across_calls() -> None:
    runs = _report_runs()

    assert render_markdown(runs, summarize(runs)) == render_markdown(runs, summarize(runs))


def test_render_json_carries_every_run_record_and_condition_rate() -> None:
    """The committed JSON is the source the Markdown reproduces from, so it holds every run
    record in `run_to_json` form."""
    runs = _report_runs()
    summaries = summarize(runs)

    payload = json.loads(render_json(runs, summaries))

    assert payload["runs"] == [run_to_json(run) for run in runs]
    assert payload["conditions"][BASELINE]["regression_rate"] == pytest.approx(1.0)
    assert payload["conditions"][PROMPTED]["regression_rate"] == pytest.approx(0.0)


def test_render_markdown_has_one_row_per_condition_with_rate_and_interval() -> None:
    runs = _report_runs()
    summaries = summarize(runs)
    lines = render_markdown(runs, summaries).splitlines()

    for condition in DEFAULT_CONDITIONS:
        summary = summaries[condition]
        low, high = summary.regression_interval
        rows = [line for line in lines if line.startswith(f"| {condition} |")]
        assert len(rows) == 1, condition
        assert f"{summary.regression_rate:.2f}" in rows[0]
        assert f"[{low:.2f}, {high:.2f}]" in rows[0]


def test_render_markdown_orders_the_gated_condition_after_the_default_ones() -> None:
    runs = _report_runs()
    runs += [_make_run(trap_id="trap-a", condition=GATED, repetition=0, tool_calls=_consulted_calls())]
    summaries = summarize(runs)

    assert list(summaries) == [BASELINE, UNPROMPTED, PROMPTED, GATED]
    lines = render_markdown(runs, summaries).splitlines()
    rows = [line.split(" | ")[0].lstrip("| ") for line in lines if line.startswith("| ") and " | " in line]
    conditions = [row for row in rows if row in CONDITIONS]
    assert conditions == [BASELINE, UNPROMPTED, PROMPTED, GATED]
    assert "| trap | baseline | unprompted | prompted | gated |" in lines


def test_render_markdown_has_a_per_trap_row_for_every_trap() -> None:
    runs = _report_runs()
    lines = render_markdown(runs, summarize(runs)).splitlines()

    for trap_id in ("trap-a", "trap-b"):
        assert len([line for line in lines if line.startswith(f"| {trap_id} |")]) == 1, trap_id


def test_render_markdown_counts_indeterminate_runs_and_leaves_them_out_of_the_tallies() -> None:
    runs = _report_runs()
    runs += [
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=2, indeterminate=True),
        _make_run(trap_id="trap-a", condition=BASELINE, repetition=3, indeterminate=True),
    ]
    lines = render_markdown(runs, summarize(runs)).splitlines()

    header = next(line for line in lines if line.startswith("| condition |"))
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    assert columns[:4] == ["condition", "runs", "errors", "indeterminate"]
    baseline = next(line for line in lines if line.startswith(f"| {BASELINE} |"))
    cells = [cell.strip() for cell in baseline.strip("|").split("|")]
    assert cells[:4] == [BASELINE, "6", "0", "2"]
    assert cells[columns.index("RR")] == "1.00"
    prompted = next(line for line in lines if line.startswith(f"| {PROMPTED} |"))
    assert [cell.strip() for cell in prompted.strip("|").split("|")][:4] == [PROMPTED, "4", "0", "0"]
    assert next(line for line in lines if line.startswith("| trap-a |")).startswith("| trap-a | 2/2 |")
    assert any("indeterminate runs" in line and "excluded from every rate" in line for line in lines)


def _cited_report_runs() -> list[AgentRun]:
    """`_report_runs` with every citation known: baseline's first trap-a repetition is an informed
    override, every other regression is silent. SRR: baseline 3/4, unprompted 2/4, prompted 0/4."""
    runs = []
    for run in _report_runs():
        informed = run.condition == BASELINE and run.trap_id == "trap-a" and run.repetition == 0
        runs.append(dataclasses.replace(run, cited_decision=informed if run.detection.regressed else False))
    return runs


def _condition_cells(lines: list[str], condition: str) -> dict[str, str]:
    header = next(line for line in lines if line.startswith("| condition |"))
    row = next(line for line in lines if line.startswith(f"| {condition} |"))
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    return dict(zip(columns, (cell.strip() for cell in row.strip("|").split("|")), strict=True))


def test_render_markdown_puts_srr_first_and_rr_next_to_it() -> None:
    runs = _cited_report_runs()
    summaries = summarize(runs)
    lines = render_markdown(runs, summaries).splitlines()

    header = next(line for line in lines if line.startswith("| condition |"))
    columns = [cell.strip() for cell in header.strip("|").split("|")]
    assert columns[4:10] == ["SRR", "SRR interval", "RR", "RR interval", "informed overrides", "unknown citations"]
    low, high = summaries[BASELINE].silent_regression_interval
    baseline = _condition_cells(lines, BASELINE)
    assert baseline["SRR"] == "0.75"
    assert baseline["SRR interval"] == f"[{low:.2f}, {high:.2f}]"
    assert baseline["RR"] == "1.00"
    assert (baseline["informed overrides"], baseline["unknown citations"]) == ("1", "0")
    assert _condition_cells(lines, UNPROMPTED)["SRR"] == "0.50"
    assert _condition_cells(lines, PROMPTED)["SRR"] == "0.00"


def test_render_markdown_reports_srr_unavailable_with_the_count_of_unknown_citations() -> None:
    runs = _report_runs()
    runs[0] = dataclasses.replace(runs[0], cited_decision=False)
    lines = render_markdown(runs, summarize(runs)).splitlines()

    baseline = _condition_cells(lines, BASELINE)
    assert baseline["SRR"] == "unavailable (3 regressed runs with unknown citation)"
    assert baseline["SRR interval"] == "n/a"
    assert baseline["RR"] == "1.00"
    assert baseline["unknown citations"] == "3"
    one = [dataclasses.replace(run, cited_decision=False) for run in runs]
    one[1] = dataclasses.replace(one[1], cited_decision=None)
    assert _condition_cells(render_markdown(one, summarize(one)).splitlines(), BASELINE)["SRR"] == (
        "unavailable (1 regressed run with unknown citation)"
    )
    assert _condition_cells(lines, PROMPTED)["SRR"] == "0.00"


def test_render_markdown_puts_the_srr_sign_test_next_to_the_rr_one() -> None:
    """Silent majorities: baseline trap-a is 1 of 2 (no majority), trap-b 2 of 2; prompted none."""
    runs = _cited_report_runs()
    lines = render_markdown(runs, summarize(runs)).splitlines()

    srr = lines.index("| baseline vs prompted | SRR | 2 | 1 | 0 | 1.0000 |")
    assert lines[srr + 1] == "| baseline vs prompted | RR | 2 | 2 | 0 | 0.5000 |"


def test_render_markdown_reports_the_srr_sign_test_unavailable_on_unknown_citations() -> None:
    runs = _report_runs()
    lines = render_markdown(runs, summarize(runs)).splitlines()

    assert (
        "| baseline vs prompted | SRR | n/a | n/a | n/a | unavailable (4 regressed runs with unknown citation) |"
        in (lines)
    )
    assert "| baseline vs prompted | RR | 2 | 2 | 0 | 0.5000 |" in lines


def test_render_markdown_defines_the_outcomes_and_how_citation_is_decided() -> None:
    runs = _cited_report_runs()
    text = render_markdown(runs, summarize(runs))

    assert "Silent Regression Rate (SRR)" in text
    paragraph = next(block for block in text.split("\n\n") if "informed override" in block)
    assert "silent regression" in paragraph
    assert "decided by code" in paragraph
    assert "final message" in paragraph


def test_render_json_carries_srr_and_its_interval_or_null() -> None:
    runs = _cited_report_runs()
    payload = json.loads(render_json(runs, summarize(runs)))
    legacy = json.loads(render_json(_report_runs(), summarize(_report_runs())))

    baseline = payload["conditions"][BASELINE]
    assert baseline["silent_regression_rate"] == pytest.approx(0.75)
    assert baseline["silent_regression_interval"] == pytest.approx(list(wilson_interval(3, 4)))
    assert (baseline["silent_regressions"], baseline["informed_overrides"], baseline["unknown_citations"]) == (3, 1, 0)
    assert legacy["conditions"][BASELINE]["silent_regression_rate"] is None
    assert legacy["conditions"][BASELINE]["silent_regression_interval"] is None


def test_render_markdown_states_the_provenance() -> None:
    runs = _report_runs()
    text = render_markdown(runs, summarize(runs))

    provenance = runs[0].provenance
    for value in (
        provenance.date,
        provenance.model_id,
        provenance.client,
        provenance.client_version,
        provenance.bruriah_version,
        provenance.trap_set_digest,
    ):
        assert value in text
    assert re.search(rf"\b{provenance.repetitions}\b", text)


@pytest.mark.parametrize("render", [render_json, render_markdown])
def test_reports_carry_no_absolute_path_and_no_timestamp_but_the_provenance_date(tmp_path: Path, render) -> None:
    runs = _report_runs()
    text = render(runs, summarize(runs))

    assert str(tmp_path) not in text
    assert str(ROOT) not in text
    assert "/Users" not in text
    assert set(re.findall(r"\d{4}-\d{2}-\d{2}", text)) == {_PROVENANCE_DATE}
    assert re.search(r"\d{2}:\d{2}:\d{2}", text) is None


def test_write_report_publishes_exact_bytes_and_leaves_no_temporary(tmp_path: Path) -> None:
    target = tmp_path / "report.md"

    write_report(target, "first\n")
    write_report(target, "second -- with unicode: caf\u00e9\n")

    assert target.read_text(encoding="utf-8") == "second -- with unicode: caf\u00e9\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.md"]


def test_write_report_keeps_the_previous_report_when_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "report.json"
    target.write_text('{"committed": true}\n', encoding="utf-8")

    def refuse(_source: object, _destination: object) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr(report_module.os, "replace", refuse)
    with pytest.raises(OSError, match="simulated publication failure"):
        write_report(target, '{"committed": false}\n')

    assert target.read_text(encoding="utf-8") == '{"committed": true}\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


# -------------------------------------------------------------------------------------------
# Runner plan, dry run, and replay adapter
# -------------------------------------------------------------------------------------------


def test_plan_invocations_orders_by_trap_then_condition_then_repetition(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-b")
    _make_trap(tmp_path, "trap-a")
    traps = load_traps(tmp_path)

    plan = plan_invocations(traps, (BASELINE, PROMPTED), 2)

    assert plan == [
        ("trap-a", BASELINE, 0),
        ("trap-a", BASELINE, 1),
        ("trap-a", PROMPTED, 0),
        ("trap-a", PROMPTED, 1),
        ("trap-b", BASELINE, 0),
        ("trap-b", BASELINE, 1),
        ("trap-b", PROMPTED, 0),
        ("trap-b", PROMPTED, 1),
    ]


def test_plan_invocations_covers_traps_times_conditions_times_repetitions(tmp_path: Path) -> None:
    for trap_id in ("trap-a", "trap-b", "trap-c"):
        _make_trap(tmp_path, trap_id)
    traps = load_traps(tmp_path)

    plan = plan_invocations(traps, DEFAULT_CONDITIONS, 5)

    assert len(plan) == 3 * 3 * 5
    assert len(set(plan)) == len(plan)


def test_plan_invocations_accepts_the_gated_condition(tmp_path: Path) -> None:
    _make_trap(tmp_path, "trap-a")

    assert plan_invocations(load_traps(tmp_path), (GATED,), 2) == [("trap-a", GATED, 0), ("trap-a", GATED, 1)]


def test_main_dry_run_lists_every_invocation_without_running_an_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-a")
    _make_trap(traps_dir, "trap-b", prompt="Move the tool declarations next to their handlers.")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("a dry run must not construct an adapter or drive an agent")

    monkeypatch.setattr(adapters_module.ReplayAdapter, "__init__", refuse)
    monkeypatch.setattr(adapters_module, "run_benchmark", refuse)
    monkeypatch.setattr(run_module, "run_benchmark", refuse, raising=False)

    exit_code = main(["--dry-run", "--traps", str(traps_dir), "--repetitions", "2"])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert any(re.search(r"\b12\b", line) for line in lines)
    for trap_id in ("trap-a", "trap-b"):
        for condition in DEFAULT_CONDITIONS:
            matching = [line for line in lines if trap_id in line and re.search(rf"\b{condition}\b", line)]
            assert len(matching) == 2, (trap_id, condition)
    assert not [line for line in lines if re.search(rf"\b{GATED}\b", line)], "gated is opt-in"


def test_main_dry_run_plans_only_gated_when_asked_for_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-a")

    exit_code = main(["--dry-run", "--traps", str(traps_dir), "--repetitions", "2", "--conditions", GATED])

    lines = capsys.readouterr().out.splitlines()
    assert exit_code == 0
    assert lines == [
        "2 planned invocations: 1 traps x 1 conditions x 2 repetitions",
        "trap-a gated repetition 0",
        "trap-a gated repetition 1",
    ]


def test_replay_adapter_returns_the_stored_run_for_the_requested_repetition(tmp_path: Path) -> None:
    """The repetition number travels explicitly in the adapter call, so a replay adapter is a pure
    lookup: no call counting, no hidden state, and the same record comes back however many times
    or in whatever order it is asked for."""
    trap = load_trap(_make_trap(tmp_path / "traps"))
    first = _make_run(repetition=0, regressed=True)
    second = _make_run(repetition=1, regressed=False)
    adapter: AgentAdapter = ReplayAdapter({("trap-a", BASELINE, 0): first, ("trap-a", BASELINE, 1): second})

    assert adapter.run(tmp_path, trap.prompt, BASELINE, trap, 1) == second
    assert adapter.run(tmp_path, trap.prompt, BASELINE, trap, 0) == first
    assert adapter.run(tmp_path, trap.prompt, BASELINE, trap, 0) == first


def test_replay_adapter_rejects_an_invocation_it_has_no_record_for(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path / "traps"))
    adapter = ReplayAdapter({("trap-a", BASELINE, 0): _make_run()})

    with pytest.raises(ReplayError, match="trap-a"):
        adapter.run(tmp_path, trap.prompt, PROMPTED, trap, 0)


def test_replay_adapter_rejects_a_repetition_beyond_its_records(tmp_path: Path) -> None:
    trap = load_trap(_make_trap(tmp_path / "traps"))
    adapter = ReplayAdapter({("trap-a", BASELINE, 0): _make_run()})

    with pytest.raises(ReplayError, match="1"):
        adapter.run(tmp_path, trap.prompt, BASELINE, trap, 1)


def test_run_benchmark_drives_the_adapter_in_plan_order(tmp_path: Path) -> None:
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-b", prompt="Move the tool declarations next to their handlers.")
    _make_trap(traps_dir, "trap-a")
    traps = load_traps(traps_dir)
    calls: list[tuple[str, str, int, str]] = []

    class RecordingAdapter:
        def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
            calls.append((trap.trap_id, condition, repetition, prompt))
            return _make_run(trap_id=trap.trap_id, condition=condition, repetition=repetition)

    runs = run_benchmark(traps, RecordingAdapter(), CONDITIONS, 2)

    plan = plan_invocations(traps, CONDITIONS, 2)
    assert [(run.trap_id, run.condition, run.repetition) for run in runs] == plan
    prompts = {trap.trap_id: trap.prompt for trap in traps}
    assert calls == [(trap_id, condition, repetition, prompts[trap_id]) for trap_id, condition, repetition in plan]


def _two_trap_set(tmp_path: Path) -> tuple[Trap, ...]:
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-a")
    _make_trap(traps_dir, "trap-b", prompt="Move the tool declarations next to their handlers.")
    return load_traps(traps_dir)


class _KeyedAdapter:
    """Records every invocation in `events` and raises `AdapterError` for the keys in `failing`."""

    def __init__(self, events: list[tuple[str, tuple[str, str, int]]], failing: frozenset = frozenset()) -> None:
        self.events = events
        self.failing = failing

    def run(self, workdir: Path, prompt: str, condition: str, trap: Trap, repetition: int) -> AgentRun:
        key = (trap.trap_id, condition, repetition)
        self.events.append(("invoked", key))
        if key in self.failing:
            raise AdapterError(f"client loaded the wrong MCP servers for {key}")
        return _make_run(trap_id=trap.trap_id, condition=condition, repetition=repetition)


def _key(run: AgentRun) -> tuple[str, str, int]:
    return (run.trap_id, run.condition, run.repetition)


def test_run_benchmark_does_not_invoke_the_skipped_keys(tmp_path: Path) -> None:
    traps = _two_trap_set(tmp_path)
    plan = plan_invocations(traps, CONDITIONS, 2)
    skip = {plan[0], plan[5], plan[-1]}
    events: list[tuple[str, tuple[str, str, int]]] = []

    runs = run_benchmark(traps, _KeyedAdapter(events), CONDITIONS, 2, skip=skip)

    remaining = [key for key in plan if key not in skip]
    assert [key for _, key in events] == remaining
    assert [_key(run) for run in runs] == remaining


def test_run_benchmark_hands_each_run_to_on_run_before_the_next_invocation(tmp_path: Path) -> None:
    traps = _two_trap_set(tmp_path)
    plan = plan_invocations(traps, CONDITIONS, 2)
    events: list[tuple[str, tuple[str, str, int]]] = []

    runs = run_benchmark(
        traps, _KeyedAdapter(events), CONDITIONS, 2, on_run=lambda run: events.append(("recorded", _key(run)))
    )

    assert events == [event for key in plan for event in (("invoked", key), ("recorded", key))]
    assert [_key(run) for run in runs] == plan


def test_run_benchmark_reports_a_failing_key_to_on_failure_and_runs_the_others(tmp_path: Path) -> None:
    traps = _two_trap_set(tmp_path)
    plan = plan_invocations(traps, CONDITIONS, 2)
    events: list[tuple[str, tuple[str, str, int]]] = []
    failures: list[tuple[tuple[str, str, int], AdapterError]] = []
    recorded: list[tuple[str, str, int]] = []

    runs = run_benchmark(
        traps,
        _KeyedAdapter(events, failing=frozenset({plan[2]})),
        CONDITIONS,
        2,
        on_run=lambda run: recorded.append(_key(run)),
        on_failure=lambda key, error: failures.append((key, error)),
    )

    assert [key for _, key in events] == plan
    assert [(key, str(error)) for key, error in failures] == [
        (plan[2], f"client loaded the wrong MCP servers for {plan[2]}")
    ]
    succeeded = [key for key in plan if key != plan[2]]
    assert [_key(run) for run in runs] == succeeded
    assert recorded == succeeded


def test_run_benchmark_without_on_failure_propagates_the_adapter_error(tmp_path: Path) -> None:
    traps = _two_trap_set(tmp_path)
    plan = plan_invocations(traps, CONDITIONS, 2)
    events: list[tuple[str, tuple[str, str, int]]] = []

    with pytest.raises(AdapterError, match="wrong MCP servers"):
        run_benchmark(traps, _KeyedAdapter(events, failing=frozenset({plan[2]})), CONDITIONS, 2)

    assert [key for _, key in events] == plan[:3]


def test_replayed_benchmark_reproduces_its_report_byte_identically(tmp_path: Path) -> None:
    """Replaying the same committed run records twice renders the same bytes: the published
    report reproduces from its run records, never from a new model call."""
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-a")
    _make_trap(traps_dir, "trap-b", prompt="Move the tool declarations next to their handlers.")
    traps = load_traps(traps_dir)
    records = {(run.trap_id, run.condition, run.repetition): run for run in _report_runs()}

    def replay() -> tuple[str, str]:
        runs = run_benchmark(traps, ReplayAdapter(records), DEFAULT_CONDITIONS, 2)
        summaries = summarize(runs)
        return render_json(runs, summaries), render_markdown(runs, summaries)

    assert replay() == replay()


# -------------------------------------------------------------------------------------------
# Resume checkpoint
# -------------------------------------------------------------------------------------------


def _record_line(repetition: int) -> bytes:
    return (json.dumps(run_to_json(_make_run(repetition=repetition)), ensure_ascii=False) + "\n").encode("utf-8")


_RESUME_DIGEST = "a" * 64
_RESUME_PLAN = {("trap-a", BASELINE, repetition) for repetition in range(3)}


@pytest.mark.parametrize(
    "torn",
    [
        pytest.param(_record_line(2)[:40], id="cut-mid-record"),
        pytest.param(_record_line(2)[:-2], id="cut-before-closing-brace"),
        pytest.param(b'{"exit_reason": "\xc3', id="cut-mid-utf8-character"),
    ],
)
def test_recorded_runs_drops_a_torn_final_line_and_truncates_the_log_to_its_last_whole_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], torn: bytes
) -> None:
    """A crash mid-append leaves a final line with no newline that does not parse: that run is
    not recorded, a warning names it, and the log is cut back so the next append starts clean."""
    log = tmp_path / "runs.jsonl"
    whole = _record_line(0) + _record_line(1)
    log.write_bytes(whole + torn)

    recorded = run_module._recorded_runs(log, _RESUME_DIGEST, _RESUME_PLAN)

    assert set(recorded) == {("trap-a", BASELINE, 0), ("trap-a", BASELINE, 1)}
    err = capsys.readouterr().err
    assert str(log) in err
    assert "line 3" in err
    assert f"{len(torn)} bytes" in err
    assert log.read_bytes() == whole
    run_module._append_jsonl(log, run_to_json(_make_run(repetition=2)))
    assert set(run_module._recorded_runs(log, _RESUME_DIGEST, _RESUME_PLAN)) == _RESUME_PLAN


def test_recorded_runs_keeps_a_whole_final_line_missing_its_newline_and_adds_the_newline(tmp_path: Path) -> None:
    log = tmp_path / "runs.jsonl"
    log.write_bytes(_record_line(0) + _record_line(1).rstrip(b"\n"))

    recorded = run_module._recorded_runs(log, _RESUME_DIGEST, _RESUME_PLAN)

    assert set(recorded) == {("trap-a", BASELINE, 0), ("trap-a", BASELINE, 1)}
    assert log.read_bytes() == _record_line(0) + _record_line(1)
    run_module._append_jsonl(log, run_to_json(_make_run(repetition=2)))
    assert set(run_module._recorded_runs(log, _RESUME_DIGEST, _RESUME_PLAN)) == _RESUME_PLAN


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_record_line(0) + b'{"trap_id": "tr\n' + _record_line(1), id="middle-line"),
        pytest.param(_record_line(0) + b'{"trap_id": "tr\n', id="final-line-ending-in-newline"),
    ],
)
def test_recorded_runs_rejects_a_malformed_line_that_is_not_a_torn_final_line(tmp_path: Path, content: bytes) -> None:
    """Only an unterminated final line can be a crash artifact; any other bad line is corruption,
    and the log is left exactly as it was."""
    log = tmp_path / "runs.jsonl"
    log.write_bytes(content)

    with pytest.raises(ValueError, match=rf"{re.escape(str(log))}, line 2: "):
        run_module._recorded_runs(log, _RESUME_DIGEST, _RESUME_PLAN)

    assert log.read_bytes() == content


# -------------------------------------------------------------------------------------------
# Rendering a stored run: `report.py` main
# -------------------------------------------------------------------------------------------

_DECISION_CUE = "docs/decisions/0001-tool-declarations.md"


def _transcript_lines(final: str, *, tool_output: str = "") -> str:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": tool_output}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": final}]}},
        {"type": "result", "subtype": "success", "result": final},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _stored_run(tmp_path: Path) -> tuple[Path, Path]:
    """A trap set and a stored run directory: `runs.jsonl` written before citation scoring (every
    `cited_decision` unknown) and the transcripts it points at. Baseline regresses twice, once
    citing the decision in its final message and once only reading it in tool output."""
    traps_dir = tmp_path / "traps"
    _make_trap(traps_dir, "trap-a")
    run_dir = tmp_path / "out"
    transcripts = run_dir / "transcripts"
    transcripts.mkdir(parents=True)
    (transcripts / "a-0.jsonl").write_text(_transcript_lines(f"Reverted, against {_DECISION_CUE}."), encoding="utf-8")
    (transcripts / "a-1.jsonl").write_text(
        _transcript_lines("Swapped in fastlib.", tool_output=f"see {_DECISION_CUE}"), encoding="utf-8"
    )
    (transcripts / "p-0.jsonl").write_text(_transcript_lines("Kept the declarations."), encoding="utf-8")
    runs = [
        _make_run(repetition=0, regressed=True, transcript="transcripts/a-0.jsonl"),
        _make_run(repetition=1, regressed=True, transcript="transcripts/a-1.jsonl"),
        _make_run(condition=PROMPTED, repetition=0, regressed=False, transcript="transcripts/p-0.jsonl"),
    ]
    (run_dir / "runs.jsonl").write_text(
        "".join(json.dumps(run_to_json(run), ensure_ascii=False) + "\n" for run in runs), encoding="utf-8"
    )
    return traps_dir, run_dir


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_report_main_rescores_citations_in_memory_and_leaves_the_run_byte_identical(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traps_dir, run_dir = _stored_run(tmp_path)
    before = _tree_bytes(run_dir)

    assert report_module.main([str(run_dir), "--traps", str(traps_dir), "--rescore-citations"]) == 0

    lines = capsys.readouterr().out.splitlines()
    baseline = _condition_cells(lines, BASELINE)
    assert (baseline["SRR"], baseline["RR"]) == ("0.50", "1.00")
    assert (baseline["informed overrides"], baseline["unknown citations"]) == ("1", "0")
    assert _tree_bytes(run_dir) == before


def test_report_main_without_the_flag_keeps_legacy_citations_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traps_dir, run_dir = _stored_run(tmp_path)
    before = _tree_bytes(run_dir)

    assert report_module.main([str(run_dir), "--traps", str(traps_dir)]) == 0

    baseline = _condition_cells(capsys.readouterr().out.splitlines(), BASELINE)
    assert baseline["SRR"] == "unavailable (2 regressed runs with unknown citation)"
    assert baseline["RR"] == "1.00"
    assert _tree_bytes(run_dir) == before


def test_report_main_writes_the_rescored_reports_to_out_and_never_into_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    traps_dir, run_dir = _stored_run(tmp_path)
    before = _tree_bytes(run_dir)
    out = tmp_path / "scratch"
    out.mkdir()

    assert report_module.main([str(run_dir), "--traps", str(traps_dir), "--rescore-citations", "--out", str(out)]) == 0

    payload = json.loads((out / "runs.json").read_text(encoding="utf-8"))
    assert [run["cited_decision"] for run in payload["runs"]] == [True, False, False]
    assert _condition_cells((out / "report.md").read_text(encoding="utf-8").splitlines(), BASELINE)["SRR"] == "0.50"
    assert _tree_bytes(run_dir) == before

    assert report_module.main([str(run_dir), "--rescore-citations", "--out", str(run_dir)]) == 2
    assert "the run directory" in capsys.readouterr().err
    assert _tree_bytes(run_dir) == before


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_record_line(0) + b'{"trap_id": "tr\n', id="malformed-line"),
        pytest.param(_record_line(0) + _record_line(1)[:40], id="torn-final-line"),
        pytest.param(_record_line(0) + _record_line(0), id="duplicate-invocation"),
        pytest.param(
            _record_line(0)
            + (
                json.dumps(run_to_json(_make_run(repetition=1, provenance=_make_provenance(trap_set_digest="b" * 64))))
                + "\n"
            ).encode("utf-8"),
            id="two-trap-sets",
        ),
    ],
)
def test_report_main_rejects_a_bad_runs_log_without_touching_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    """Rendering only reads: unlike a resumed run, it never cuts a torn final line back."""
    run_dir = tmp_path / "out"
    run_dir.mkdir()
    (run_dir / "runs.jsonl").write_bytes(content)

    assert report_module.main([str(run_dir)]) == 2

    assert "runs.jsonl" in capsys.readouterr().err
    assert (run_dir / "runs.jsonl").read_bytes() == content
