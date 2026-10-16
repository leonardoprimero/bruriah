"""The committed records of the 2026-10-15 Antigravity measurement, their render and their audit.

The measurement ran 30 runs on Antigravity CLI (`agy` 1.3.6) with `gemini-3.8-flash-high`: three
egui traps, `baseline` and `unprompted`, five repetitions, all at Bruriah `08a81db`. Its records are
committed under `evals/agent_regression/results/antigravity-2026-10-15/records/` without
transcripts, with the rendered `report.md` and `audit.json`, the replay of what Bruriah returned to
each `unprompted` run. These tests pin the published numbers to the records, and the claims the
results README makes about the audit to `audit.json`.
Those three generated files arrive in the commit before the one that adds this module, so the
authored change can be reviewed on its own; every test here reads them from the tree.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "agent_regression" / "results" / "antigravity-2026-10-15"
TRAP_IDS = ("egui-android-activity", "egui-datepicker-chrono", "egui-image-formats")
DOCUMENTED = ("egui-android-activity", "egui-datepicker-chrono")
CONDITIONS = ("baseline", "unprompted")
BRURIAH_COMMIT = "08a81dba2bdd39f6f8fa026731775b62f5a215e3"
COMMITTED = {"README.md", "audit.py", "audit.json", "report.md", "records/runs.jsonl"}

# An absolute path on any platform, or a home-relative one: none may reach a committed record.
_PRIVATE_PATH = re.compile(r"(/Users/|/home/|/private/|/var/folders/|/tmp/|~/|[A-Za-z]:[\\/])")


def _records() -> list[dict]:
    path = RESULTS / "records" / "runs.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audit() -> list[dict]:
    return list(json.loads((RESULTS / "audit.json").read_text(encoding="utf-8"))["runs"])


def test_the_set_holds_five_runs_of_each_trap_and_condition() -> None:
    cells = Counter((record["trap_id"], record["condition"]) for record in _records())
    assert cells == {(trap_id, condition): 5 for trap_id in TRAP_IDS for condition in CONDITIONS}


def test_every_run_records_the_measured_provenance() -> None:
    for record in _records():
        provenance = record["provenance"]
        assert provenance["client"] == "antigravity"
        assert provenance["client_version"] == "1.3.6"
        assert provenance["model_id"] == "gemini-3.8-flash-high"
        assert provenance["bruriah_commit"] == BRURIAH_COMMIT
        assert provenance["bruriah_version"] == "2.2.0"
        assert provenance["date"] == "2026-10-15"
        # agy reports tokens, never dollars.
        assert record["cost_usd"] is None


def test_no_run_errored_and_every_run_searched_the_web() -> None:
    records = _records()
    assert [record for record in records if record["exit_reason"] == "error"] == []
    assert all(any(call["name"] == "search_web" for call in record["tool_calls"]) for record in records)
    assert sum(record["exit_reason"] == "time_budget" for record in records) == 12


def test_only_the_documented_files_are_committed() -> None:
    committed = {path.relative_to(RESULTS).as_posix() for path in RESULTS.rglob("*") if path.is_file()}
    assert committed - {name for name in committed if "__pycache__" in name} == COMMITTED


def test_no_committed_file_holds_a_private_path() -> None:
    for name in COMMITTED:
        text = (RESULTS / name).read_text(encoding="utf-8")
        assert not _PRIVATE_PATH.search(text), f"{name} holds a private path"


def test_the_report_reproduces_byte_for_byte(tmp_path: Path) -> None:
    records = tmp_path / "records"
    shutil.copytree(RESULTS / "records", records)
    rendered = tmp_path / "rendered"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "evals" / "agent_regression" / "report.py"), str(records), "--out", str(rendered)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert (rendered / "report.md").read_bytes() == (RESULTS / "report.md").read_bytes()


def test_the_report_states_the_null_result() -> None:
    report = (RESULTS / "report.md").read_text(encoding="utf-8")
    assert "| baseline vs unprompted | SRR | 9/10 | 9/10 | 0.90 | 0.90 | 1.0000 |" in report
    assert "| baseline vs unprompted | RR | 9/10 | 9/10 | 0.90 | 0.90 | 1.0000 |" in report


def test_the_audit_covers_exactly_the_unprompted_runs() -> None:
    audited = sorted((entry["trap_id"], entry["repetition"]) for entry in _audit())
    unprompted = sorted((r["trap_id"], r["repetition"]) for r in _records() if r["condition"] == "unprompted")
    assert audited == unprompted
    assert sum(bool(entry["consult_steps"]) for entry in _audit()) == 14


def test_bruriah_returned_the_decision_before_every_documented_run_wrote() -> None:
    documented = [entry for entry in _audit() if entry["trap_id"] in DOCUMENTED]
    assert len(documented) == 10
    for entry in documented:
        assert entry["decision_listed_step"] is not None
        assert entry["decision_read_step"] is not None
        assert entry["first_write_step"] is None or entry["decision_read_step"] < entry["first_write_step"]
    assert sum(entry["decision_listed_step"] == entry["consult_steps"][0] for entry in documented) == 9


def test_no_documented_final_message_names_the_decision() -> None:
    for entry in _audit():
        if entry["trap_id"] == "egui-android-activity":
            assert entry["final_message"] == {"present": True, "names_decision": False}
        if entry["trap_id"] == "egui-datepicker-chrono":
            # Every one hit the time budget, so none ended with a message to cite in.
            assert entry["exit_reason"] == "time_budget"
            assert entry["final_message"] == {"present": False, "names_decision": False}
