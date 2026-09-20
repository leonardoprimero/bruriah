from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.guard import GuardResult, GuardViolation
from bruriah.heal import (
    HealingResult,
    RemediationBlueprint,
    RemediationStep,
    _generate_agent_prompt,
    _synthesize_steps,
    evaluate_heal,
    format_heal_agent,
    format_heal_human,
    format_heal_json,
)


class TestHealingSynthesis:
    def test_synthesize_steps_with_bullets(self):
        body = """
        Decided to decouple database persistence.
        - Must never import sqlite3 directly in usecases.
        - Inject SnapshotRepository via constructor.
        - Keep domain entities pure.
        """
        steps, pattern, directives = _synthesize_steps(
            file_path="src/service.py",
            subject="Decouple Persistence",
            sha="abcdef123456",
            message="Direct SQLite import detected.",
            body=body,
        )

        assert len(steps) == 3
        assert steps[0].order == 1
        assert "Isolate Non-Compliant Code" in steps[0].action
        assert "src/service.py" in steps[0].detail
        assert steps[1].order == 2
        assert "Apply Canonical Architectural Pattern" in steps[1].action
        assert steps[2].order == 3
        assert "Verify Architectural Compliance" in steps[2].action

        assert "Must never import sqlite3 directly in usecases." in pattern
        assert len(directives) >= 3

    def test_generate_agent_prompt(self):
        bp = RemediationBlueprint(
            file_path="src/auth.py",
            decision_subject="OAuth2 Security",
            decision_sha="112233445566",
            violation_message="Plain token in cookie",
            canonical_pattern="Use HTTP-only encrypted session cookies",
            refactoring_steps=(
                RemediationStep(1, "Remove plain cookie", "Delete plain cookie setter."),
                RemediationStep(2, "Inject session manager", "Use SessionManager."),
            ),
            directives=("Use HTTP-only cookies",),
        )

        prompt = _generate_agent_prompt("src/auth.py", [bp])
        assert "# 🛠️ Bruriah Architectural Remediation Blueprint" in prompt
        assert "**Target**: `src/auth.py`" in prompt
        assert "Do NOT apply quick hacks" in prompt
        assert "OAuth2 Security (`11223344`)" in prompt
        assert "Plain token in cookie" in prompt
        assert "Use HTTP-only encrypted session cookies" in prompt
        assert "1. **Remove plain cookie**: Delete plain cookie setter." in prompt


class TestEvaluateHeal:
    @patch("bruriah.heal.evaluate_guard")
    def test_evaluate_heal_compliant(self, mock_guard, tmp_path: Path):
        mock_conn = MagicMock()
        mock_guard.return_value = GuardResult(
            target="src/main.py",
            status="PASSED",
            inspected_files=("src/main.py",),
            contracts=(),
            violations=(),
            agent_context="",
        )

        result = evaluate_heal(tmp_path, mock_conn, "src/main.py")
        assert result.status == "COMPLIANT"
        assert len(result.blueprints) == 0
        assert "No architectural violations detected" in result.agent_prompt

    @patch("bruriah.heal.evaluate_guard")
    @patch("bruriah.heal.find_decision_in_database")
    def test_evaluate_heal_with_violations(self, mock_find, mock_guard, tmp_path: Path):
        mock_conn = MagicMock()
        mock_guard.return_value = GuardResult(
            target="src/service.py",
            status="VETOED",
            inspected_files=("src/service.py",),
            contracts=(),
            violations=(
                GuardViolation(
                    file_path="src/service.py",
                    severity="VETO",
                    decision_title="Decoupled Persistence",
                    decision_sha="aabbccdd",
                    message="Direct database access detected.",
                ),
            ),
            agent_context="",
        )

        mock_find.return_value = MagicMock(
            subject="Decoupled Persistence",
            commit_sha="aabbccddeeff",
            body="- Must use SnapshotRepository.",
        )

        result = evaluate_heal(tmp_path, mock_conn, "src/service.py")
        assert result.status == "HEALABLE"
        assert len(result.blueprints) == 1
        bp = result.blueprints[0]
        assert bp.file_path == "src/service.py"
        assert bp.decision_subject == "Decoupled Persistence"
        assert bp.decision_sha == "aabbccddeeff"
        assert len(bp.refactoring_steps) == 3


class TestHealFormatting:
    def test_format_heal_human_and_json(self):
        bp = RemediationBlueprint(
            file_path="src/service.py",
            decision_subject="Clean Architecture",
            decision_sha="11223344",
            violation_message="Violation message",
            canonical_pattern="Use repository pattern",
            refactoring_steps=(
                RemediationStep(1, "Step 1", "Detail 1"),
            ),
            directives=("Directive 1",),
        )
        res = HealingResult(
            target="src/service.py",
            status="HEALABLE",
            inspected_files=("src/service.py",),
            blueprints=(bp,),
            agent_prompt="Agent prompt here",
        )

        human = format_heal_human(res)
        assert "🏛️  Bruriah Architectural Healing — src/service.py" in human
        assert "🔧 HEALABLE" in human
        assert "Blueprint #1: src/service.py" in human
        assert "Use repository pattern" in human

        agent = format_heal_agent(res)
        assert agent == "Agent prompt here"

        json_out = format_heal_json(res)
        data = json.loads(json_out)
        assert data["target"] == "src/service.py"
        assert data["status"] == "HEALABLE"
        assert len(data["blueprints"]) == 1


class TestHealCli:
    def test_heal_cli_parsing(self):
        from bruriah.cli import _build_cli_parser

        parser = _build_cli_parser()
        args = parser.parse_args(["heal", "src/auth.py", "--agent", "--json"])
        assert args.target == "src/auth.py"
        assert args.agent is True
        assert args.json is True

    def test_heal_cli_dispatch_human(self, capsys):
        from bruriah.cli import bruriah_main

        sample = HealingResult(
            target="src/auth.py",
            status="COMPLIANT",
            inspected_files=("src/auth.py",),
            blueprints=(),
            agent_prompt="Compliant",
        )
        with patch("bruriah.cli.run_heal", return_value=sample):
            code = bruriah_main(["heal", "src/auth.py"])
            assert code == 0
            captured = capsys.readouterr()
            assert "🏛️  Bruriah Architectural Healing — src/auth.py" in captured.out
            assert "✅ COMPLIANT" in captured.out

    def test_heal_cli_dispatch_agent(self, capsys):
        from bruriah.cli import bruriah_main

        sample = HealingResult(
            target="src/auth.py",
            status="HEALABLE",
            inspected_files=("src/auth.py",),
            blueprints=(),
            agent_prompt="HEAL_AGENT_PROMPT_ONLY",
        )
        with patch("bruriah.cli.run_heal", return_value=sample):
            code = bruriah_main(["heal", "src/auth.py", "--agent"])
            assert code == 0
            captured = capsys.readouterr()
            assert captured.out.strip() == "HEAL_AGENT_PROMPT_ONLY"

    def test_heal_cli_dispatch_json(self, capsys):
        from bruriah.cli import bruriah_main

        sample = HealingResult(
            target="src/auth.py",
            status="HEALABLE",
            inspected_files=("src/auth.py",),
            blueprints=(),
            agent_prompt="Prompt",
        )
        with patch("bruriah.cli.run_heal", return_value=sample):
            code = bruriah_main(["heal", "src/auth.py", "--json"])
            assert code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["target"] == "src/auth.py"

    def test_heal_cli_dispatch_error(self, capsys):
        from bruriah.cli import bruriah_main
        from bruriah.heal import HealError

        with patch("bruriah.cli.run_heal", side_effect=HealError("git_error", "fatal")):
            code = bruriah_main(["heal", "nonexistent.py"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: git_error" in captured.err

