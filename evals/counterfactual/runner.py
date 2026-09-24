#!/usr/bin/env python3
"""Counterfactual Architectural Memory & Premise Tracking Evaluation Runner.

Evaluates how accurately Bruriah identifies rejected architectural patterns,
determines whether their underlying premises hold, and alerts agents before
they commit architectural regressions.

Usage:
    uv run python evals/counterfactual/runner.py
"""

from __future__ import annotations

import json
import tempfile
from array import array
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from bruriah.contracts import InvestigationRequest
from bruriah.corpus import CorpusPolicy
from bruriah.index import BuildConfig, build_candidate, promote_candidate, snapshot_active
from bruriah.platform import load_registry
from bruriah.registries import Registry
from bruriah.repository import SnapshotRepository
from bruriah.service import InvestigateService, ServiceDeps

FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"'
    + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_PATH = ROOT / "evals" / "counterfactual" / "scenarios.jsonl"
DATA_DIR = ROOT / "src" / "bruriah" / "data"


def _real_registry() -> Registry:
    return load_registry(today=date(2026, 7, 25))


def _embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


@dataclass
class ScenarioResult:
    scenario_id: str
    task: str
    category: str
    expected_verdict: str
    actual_verdict: str | None
    matched_alternative: str | None
    success: bool
    evidence_count: int


def run_benchmark() -> list[ScenarioResult]:
    if not SCENARIOS_PATH.exists():
        raise FileNotFoundError(f"Scenarios not found at {SCENARIOS_PATH}")

    scenarios: list[dict[str, Any]] = [
        json.loads(line) for line in SCENARIOS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

    results: list[ScenarioResult] = []

    for sc in scenarios:
        with tempfile.TemporaryDirectory() as tmp_dir_str:
            tmp_path = Path(tmp_dir_str)
            vault = tmp_path / "vault" / "public"
            vault.mkdir(parents=True)

            import yaml

            # Build document 1 with alternative & premises
            alt_data = {
                "name": sc["alternative"],
                "disposition": sc["disposition"],
                "reason": sc["reason"],
                "premises": [p["id"] for p in sc.get("premises", [])],
            }
            doc1_dict: dict[str, Any] = {
                "commit": "111122223333",
                "alternatives": [alt_data],
            }
            premises_data = sc.get("premises", [])
            if premises_data:
                doc1_dict["premises"] = [
                    {
                        "id": p["id"],
                        "statement": p["statement"],
                        "status": p.get("status", "active"),
                    }
                    for p in premises_data
                ]

            doc1_yaml = yaml.safe_dump(doc1_dict, sort_keys=False)
            doc1_content = f"---\n{doc1_yaml}---\n# ADR: Evaluation of {sc['alternative']}\n\n{sc['reason']}\n"
            (vault / "adr-01.md").write_text(doc1_content, encoding="utf-8")

            # If premise is invalidated by a subsequent commit/doc, add adr-02
            inv_premises = [p for p in premises_data if p.get("status") == "invalidated"]
            if inv_premises:
                doc2_dict: dict[str, Any] = {
                    "commit": inv_premises[0].get("invalidated_by", "444455556666"),
                    "invalidated_premises": [p["id"] for p in inv_premises],
                }
                doc2_yaml = yaml.safe_dump(doc2_dict, sort_keys=False)
                doc2_content = f"---\n{doc2_yaml}---\n# ADR: Invalidate premise\n\nPremise changed.\n"
                (vault / "adr-02.md").write_text(doc2_content, encoding="utf-8")

            policy_path = tmp_path / "policy.yaml"
            policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
            policy = CorpusPolicy.load(policy_path)
            config = BuildConfig(
                root=tmp_path / "vault",
                policy_path=policy_path,
                schema_version=1,
                parser_version="corpus-v2",
                service_version="0.1.0",
                mcp_range=">=1.28.1,<2",
                embedding_model="test/minilm",
                embedding_revision="snapshot-a",
                embedding_dimensions=3,
                embedding_fingerprint=FINGERPRINT,
                ranking_config="rrf-v1",
            )
            candidate = tmp_path / "candidate.sqlite3"
            pointer = tmp_path / "active.json"
            build_candidate(config, candidate, policy, _embed)
            promote_candidate(candidate, pointer, config, policy)

            with snapshot_active(pointer, config) as active:
                deps = ServiceDeps(registry=_real_registry(), snapshot=active)
                service = InvestigateService(deps)
                req = InvestigationRequest(task=sc["task"], code_target=sc.get("code_target"))
                inv_res = service.investigate(req)

                cf = inv_res.counterfactual_assessment
                actual_verdict = cf.verdict if cf else None
                # T3 (investigate-boundary-v2): `CounterfactualAssessment.matched_alternative`
                # was replaced by an opaque `matched_alternative_ref` -- resolved back to the
                # alternative's name locally (the same resolver `bruriah ask`'s human view and
                # `demo.py` use), so this report's output is unchanged.
                matched_alt = None
                if cf:
                    repo = SnapshotRepository(active.database)
                    row = repo.resolve_alternative_ref(cf.matched_alternative_ref)
                    matched_alt = row.name if row is not None else cf.matched_alternative_ref
                evidence_count = len(cf.supporting_evidence) if cf else 0
                success = actual_verdict == sc["expected_verdict"]

                results.append(
                    ScenarioResult(
                        scenario_id=sc["scenario_id"],
                        task=sc["task"],
                        category=sc["category"],
                        expected_verdict=sc["expected_verdict"],
                        actual_verdict=actual_verdict,
                        matched_alternative=matched_alt,
                        success=success,
                        evidence_count=evidence_count,
                    )
                )

    return results


def print_summary(results: list[ScenarioResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.success)
    accuracy = (passed / total) * 100.0 if total else 0.0

    print("\n# Counterfactual Architectural Memory Correctness Suite\n")
    print(
        f"**Total Fixture Scenarios:** {total} | **Passed:** {passed} | **Suite Status:** {'VERIFIED' if passed == total else 'FAILURES DETECTED'} ({accuracy:.1f}%)\n"
    )
    print("| Scenario ID | Category | Expected Verdict | Actual Verdict | Result |")
    print("|---|---|---|---|:---:|")
    for r in results:
        status_icon = "PASS" if r.success else "FAIL"
        print(f"| `{r.scenario_id}` | {r.category} | `{r.expected_verdict}` | `{r.actual_verdict}` | {status_icon} |")

    print(f"\n{passed}/{total} deterministic fixture scenarios verified as expected.")


if __name__ == "__main__":
    results = run_benchmark()
    print_summary(results)
