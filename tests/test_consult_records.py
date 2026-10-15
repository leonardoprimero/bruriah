"""The committed records of the 2026-10-07 unprompted consult measurement and their two renders.

The measurement ran two arms of 15 runs each (three egui traps, five repetitions): control at
`baa6174`, treatment at `329f526`. Its records are committed under
`evals/agent_regression/results/consult-2026-10-07/records/`, without transcripts, and rendered
twice by that directory's `render.py`:

- `pre-registered/`: with the trap set as registered (the trap manifests at `329f526`), every run
  in the headline, 15 per arm;
- `post-hoc/`: with the current trap set, which marks `egui-image-formats` as an
  undocumented decision, 10 headline runs per arm plus a control section of 5.

Each view commits only its `report.md`: the JSON report repeats every record, which the records
already hold. These tests pin both renders to the records byte for byte, so a change to the
renderer, the trap manifests or the records cannot silently change a published number.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "agent_regression" / "results" / "consult-2026-10-07"
RENDER = RESULTS / "render.py"
ARMS = ("control", "treatment")
VIEWS = ("pre-registered", "post-hoc")
TRAP_IDS = ["egui-android-activity", "egui-datepicker-chrono", "egui-image-formats"]
UNDOCUMENTED = "egui-image-formats"
ARM_COMMITS = {
    "control": "baa6174d567981d7dee7b673dc1e9615c033ee87",
    "treatment": "329f526eeceb91ce87bdda5a39f57c1e1cf89883",
}
TRAP_SET_DIGEST = "0599415538057eed91bb51c58c05c3e8eba28b64bc3d1c4e09fca9a3ec2d5540"
REGISTERED_COMMIT = ARM_COMMITS["treatment"]

# An absolute path on any platform, or a home-relative one: none may reach a committed record.
_PRIVATE_PATH = re.compile(r"(/Users/|/home/|/private/|/var/folders/|/tmp/|~/|[A-Za-z]:[\\/])")


def _records(arm: str) -> list[dict]:
    path = RESULTS / "records" / arm / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_CONTROL_HEADING = "## Undocumented-decision control"
_CONDITION_ROW = re.compile(r"^\| unprompted \| (\d+) \|", re.MULTILINE)


def _report(view: str, arm: str) -> tuple[str, str | None]:
    """The rendered report's headline part and its undocumented-decision control part, if any."""
    text = (RESULTS / view / arm / "report.md").read_text(encoding="utf-8")
    headline, heading, control = text.partition(_CONTROL_HEADING)
    return headline, (control if heading else None)


def _condition_runs(section: str) -> int:
    match = _CONDITION_ROW.search(section)
    assert match is not None, "no unprompted row in the conditions table"
    return int(match.group(1))


def _registered_commit_available() -> bool:
    if shutil.which("git") is None:
        return False
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{REGISTERED_COMMIT}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.parametrize("arm", ARMS)
def test_each_arm_holds_fifteen_runs_of_the_three_traps(arm: str) -> None:
    records = _records(arm)
    assert len(records) == 15
    assert sorted({record["trap_id"] for record in records}) == TRAP_IDS
    assert sorted((record["trap_id"], record["repetition"]) for record in records) == [
        (trap_id, repetition) for trap_id in TRAP_IDS for repetition in range(5)
    ]
    assert {record["condition"] for record in records} == {"unprompted"}
    assert {record["exit_reason"] for record in records} == {"done"}


@pytest.mark.parametrize("arm", ARMS)
def test_each_arm_records_the_measured_provenance(arm: str) -> None:
    provenances = {json.dumps(record["provenance"], sort_keys=True) for record in _records(arm)}
    assert len(provenances) == 1
    provenance = json.loads(provenances.pop())
    assert provenance == {
        "bruriah_commit": ARM_COMMITS[arm],
        "bruriah_version": "2.1.0",
        "client": "claude-code",
        "client_version": "2.1.292 (Claude Code)",
        "date": "2026-10-07",
        "model_id": "claude-fable-5-1",
        "repetitions": 5,
        "trap_set_digest": TRAP_SET_DIGEST,
    }


def test_no_committed_file_holds_a_private_path() -> None:
    files = sorted(path for path in RESULTS.rglob("*") if path.is_file())
    assert files, f"nothing committed under {RESULTS}"
    leaks = {
        str(path.relative_to(RESULTS)): match.group(0)
        for path in files
        if (match := _PRIVATE_PATH.search(path.read_text(encoding="utf-8")))
    }
    assert leaks == {}


def test_transcripts_are_not_committed() -> None:
    assert not [path for path in RESULTS.rglob("*") if "transcripts" in path.parts]
    jsonl = sorted(str(path.relative_to(RESULTS).as_posix()) for path in RESULTS.rglob("*.jsonl"))
    assert jsonl == ["records/control/runs.jsonl", "records/treatment/runs.jsonl"]


@pytest.mark.parametrize("arm", ARMS)
def test_pre_registered_render_keeps_every_run_in_the_headline(arm: str) -> None:
    headline, control = _report("pre-registered", arm)
    assert control is None
    assert _condition_runs(headline) == 15
    for trap_id in TRAP_IDS:
        assert f"| {trap_id} | 5/5 |" in headline


@pytest.mark.parametrize("arm", ARMS)
def test_post_hoc_render_sets_the_undocumented_trap_apart(arm: str) -> None:
    headline, control = _report("post-hoc", arm)
    assert control is not None
    assert _condition_runs(headline) == 10
    assert f"| {UNDOCUMENTED} |" not in headline
    assert _condition_runs(control) == 5
    assert f"| {UNDOCUMENTED} | 5/5 |" in control
    assert (
        "Trap classification used for this split: documented: egui-android-activity, "
        "egui-datepicker-chrono; undocumented: egui-image-formats."
    ) in control


def test_only_the_markdown_reports_are_committed() -> None:
    for view in VIEWS:
        files = sorted(path.relative_to(RESULTS / view).as_posix() for path in (RESULTS / view).rglob("*"))
        assert files == ["control", "control/report.md", "treatment", "treatment/report.md"]


@pytest.mark.skipif(
    not _registered_commit_available(),
    reason=(
        f"the registered trap set lives at {REGISTERED_COMMIT}; fetch the tag "
        "archive/agent-consult-measurement-2026-10-07 to check the pre-registered render"
    ),
)
def test_both_renders_reproduce_byte_for_byte(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(RENDER), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""

    def rendered(root: Path) -> dict[str, bytes]:
        # `write_report` writes in text mode, so Windows writes CRLF; the checkout is LF
        # (`.gitattributes`). Line endings are the only difference allowed.
        return {
            path.relative_to(root).as_posix(): path.read_bytes().replace(b"\r\n", b"\n")
            for view in VIEWS
            for path in sorted((root / view).rglob("*"))
            if path.is_file()
        }

    committed = rendered(RESULTS)
    assert sorted(committed) == sorted(f"{view}/{arm}/report.md" for view in VIEWS for arm in ARMS)
    assert rendered(tmp_path) == committed
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(VIEWS)


def test_render_refuses_a_missing_registered_commit(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(RENDER), "--out", str(tmp_path), "--registered-commit", "0" * 40],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "archive/agent-consult-measurement-2026-10-07" in result.stderr
    assert list(tmp_path.iterdir()) == []
