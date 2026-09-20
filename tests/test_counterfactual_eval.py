from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
EVALS_DIR = ROOT / "evals" / "counterfactual"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from runner import run_benchmark  # noqa: E402


def test_counterfactual_benchmark_all_scenarios_pass():
    results = run_benchmark()
    assert len(results) == 20, f"Expected 20 scenarios, got {len(results)}"
    failed = [r for r in results if not r.success]
    assert not failed, f"Scenarios failed: {[(f.scenario_id, f.expected_verdict, f.actual_verdict) for f in failed]}"
