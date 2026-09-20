from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.brief import (
    ArchitecturalBrief,
    BriefError,
    GoverningConstraint,
    SupersedeTemplate,
    _extract_directives,
    _generate_agent_context,
    _generate_supersede_instructions,
    format_brief_human,
    format_brief_json,
    generate_brief,
)
from bruriah.impact import DecisionImpact, ImpactAnalysis


class TestDirectivesAndSupersede:
    def test_extract_directives_with_bullets(self):
        body = """
        Context on the auth architecture.
        - Must never store plain JWT tokens in localStorage.
        - Avoid FastMCP due to schema dropping.
        * Require strict typing for all handlers.
        """
        directives = _extract_directives("Auth Architecture", "abcdef123456", body)
        assert len(directives) >= 3
        assert "Maintain alignment with 'Auth Architecture' (abcdef12)." in directives[0]
        assert "Must never store plain JWT tokens in localStorage." in directives
        assert "Avoid FastMCP due to schema dropping." in directives

    def test_extract_directives_with_keywords(self):
        body = "We decided this because we must ensure thread safety at all costs."
        directives = _extract_directives("Concurrency Policy", "12345678", body)
        assert any("must ensure thread safety" in d for d in directives)

    def test_supersede_template_markdown(self):
        template = SupersedeTemplate(
            target_sha="a1b2c3d4e5f6",
            target_subject="Old HTTP Client",
            changed_premise="httpx now supports HTTP/3 natively.",
            proposed_invariant="Adopt httpx with HTTP/3 support.",
            rationale="Reduces latency by 40% across remote endpoints.",
        )
        md = template.to_markdown()
        assert "#### Architectural Supersede Proposal" in md
        assert "Old HTTP Client (`a1b2c3d4`)" in md
        assert "httpx now supports HTTP/3 natively." in md
        assert "Adopt httpx with HTTP/3 support." in md
        assert "Reduces latency by 40%" in md

    def test_generate_supersede_instructions(self):
        constraint = GoverningConstraint(
            decision_ref="doc:1",
            commit_sha="112233445566",
            subject="Strict Hexagonal Boundary",
            author="Architect",
            date="2026-01-01",
            status="active",
            governed_files=("src/domain.py",),
            directives=("Maintain domain isolation.",),
        )
        instructions = _generate_supersede_instructions([constraint])
        assert "### Supersede Protocol Directive" in instructions
        assert "Strict Hexagonal Boundary" in instructions
        assert "DO NOT silently violate it" in instructions

    def test_generate_agent_context(self):
        constraint = GoverningConstraint(
            decision_ref="doc:1",
            commit_sha="112233445566",
            subject="Decouple Storage",
            author="Dev",
            date="2026-05-10",
            status="active",
            governed_files=("src/storage.py",),
            directives=("Never import sqlite3 directly in usecases.",),
            active_successor_title="New Repo Layer",
            active_successor_sha="99887766",
        )
        ctx = _generate_agent_context(
            intent="Refactor repository",
            targets=["src/storage.py"],
            risk_level="HIGH",
            constraints=[constraint],
            co_governed=["src/service.py"],
            supersede_instructions="Follow supersede protocol.",
        )
        assert "# Bruriah Pre-Flight Architectural Brief" in ctx
        assert "**Task Intent**: Refactor repository" in ctx
        assert "**Risk Level**: HIGH" in ctx
        assert "### [ACTIVE] Decouple Storage (`11223344`)" in ctx
        assert "Never import sqlite3 directly in usecases." in ctx
        assert "SUPERSEDED BY: New Repo Layer (`99887766`)" in ctx
        assert "Modifying targets may impact: `src/service.py`" in ctx


class TestBriefFormatting:
    def test_format_brief_human_and_json(self):
        brief = ArchitecturalBrief(
            task_intent="Migrate to Postgres",
            targets=("src/db.py",),
            risk_level="MEDIUM",
            constraints=(
                GoverningConstraint(
                    decision_ref="doc:db",
                    commit_sha="aabbccdd",
                    subject="Use SQLite for Zero-Setup",
                    author="Lead",
                    date="2026-02-01",
                    status="active",
                    governed_files=("src/db.py",),
                    directives=("Use SQLite.",),
                ),
            ),
            co_governed_files=("src/config.py",),
            recommendations=("Ensure backward compatibility.",),
            supersede_protocol_instructions="Supersede instructions here.",
            agent_context="Agent context here.",
        )

        human = format_brief_human(brief)
        assert "🏛️  Bruriah Architectural Brief — Pre-flight Dossier" in human
        assert "Migrate to Postgres" in human
        assert "🟡 MEDIUM" in human
        assert "Use SQLite for Zero-Setup" in human
        assert "Supersede Protocol:" in human

        json_out = format_brief_json(brief)
        data = json.loads(json_out)
        assert data["task_intent"] == "Migrate to Postgres"
        assert data["risk_level"] == "MEDIUM"
        assert len(data["constraints"]) == 1
        assert data["constraints"][0]["commit_sha"] == "aabbccdd"


class TestGenerateBrief:
    def test_missing_task_and_target(self, tmp_path: Path):
        mock_conn = MagicMock()
        with patch("bruriah.brief.get_git_diff_files", return_value=[]):
            with pytest.raises(BriefError) as exc:
                generate_brief(tmp_path, mock_conn, intent="", targets=[])
            assert "missing_task_or_target" in str(exc.value)

    @patch("bruriah.brief.analyze_impact")
    def test_generate_brief_with_targets(self, mock_impact, tmp_path: Path):
        mock_impact.return_value = ImpactAnalysis(
            target="src/service.py",
            target_type="file",
            inspected_files=("src/service.py",),
            decisions=(
                DecisionImpact(
                    decision_ref="doc:svc",
                    commit_sha="1234567890ab",
                    subject="Decouple Services",
                    author="Senior Architect",
                    date="2026-03-01",
                    status="active",
                    direct_files=("src/service.py",),
                    blast_radius_files=("src/api.py",),
                ),
            ),
            total_blast_radius_files=("src/api.py",),
            risk_level="HIGH",
            recommendations=("Check dependent APIs.",),
        )

        mock_conn = MagicMock()

        brief = generate_brief(
            repo=tmp_path,
            database=mock_conn,
            intent="Update service endpoints",
            targets=["src/service.py"],
        )

        assert brief.task_intent == "Update service endpoints"
        assert brief.targets == ("src/service.py",)
        assert brief.risk_level == "HIGH"
        assert len(brief.constraints) == 1
        assert brief.constraints[0].subject == "Decouple Services"
        assert brief.co_governed_files == ("src/api.py",)
        assert brief.constraints[0].supersede_template is not None


class TestBriefCli:
    def test_brief_cli_parsing(self):
        from bruriah.cli import _build_cli_parser

        parser = _build_cli_parser()
        args = parser.parse_args(["brief", "Refactor auth", "--targets", "src/auth.py", "--json", "--agent"])
        assert args.intent == "Refactor auth"
        assert args.targets == ["src/auth.py"]
        assert args.json is True
        assert args.agent is True

    def test_brief_cli_dispatch_human(self, capsys):
        from bruriah.cli import bruriah_main

        sample = ArchitecturalBrief(
            task_intent="Refactor auth",
            targets=("src/auth.py",),
            risk_level="LOW",
            constraints=(),
            co_governed_files=(),
            recommendations=(),
            supersede_protocol_instructions="Supersede instructions",
            agent_context="Agent context",
        )
        with patch("bruriah.cli.run_brief", return_value=sample):
            code = bruriah_main(["brief", "Refactor auth", "--targets", "src/auth.py"])
            assert code == 0
            captured = capsys.readouterr()
            assert "🏛️  Bruriah Architectural Brief — Pre-flight Dossier" in captured.out
            assert "Refactor auth" in captured.out

    def test_brief_cli_dispatch_agent_mode(self, capsys):
        from bruriah.cli import bruriah_main

        sample = ArchitecturalBrief(
            task_intent="Refactor auth",
            targets=("src/auth.py",),
            risk_level="LOW",
            constraints=(),
            co_governed_files=(),
            recommendations=(),
            supersede_protocol_instructions="",
            agent_context="BRIEF_AGENT_CONTEXT_ONLY",
        )
        with patch("bruriah.cli.run_brief", return_value=sample):
            code = bruriah_main(["brief", "Refactor auth", "--agent"])
            assert code == 0
            captured = capsys.readouterr()
            assert captured.out.strip() == "BRIEF_AGENT_CONTEXT_ONLY"

    def test_brief_cli_dispatch_json_mode(self, capsys):
        from bruriah.cli import bruriah_main

        sample = ArchitecturalBrief(
            task_intent="Refactor auth",
            targets=("src/auth.py",),
            risk_level="LOW",
            constraints=(),
            co_governed_files=(),
            recommendations=(),
            supersede_protocol_instructions="",
            agent_context="",
        )
        with patch("bruriah.cli.run_brief", return_value=sample):
            code = bruriah_main(["brief", "Refactor auth", "--json"])
            assert code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["task_intent"] == "Refactor auth"

    def test_brief_cli_dispatch_error(self, capsys):
        from bruriah.cli import bruriah_main

        with patch("bruriah.cli.run_brief", side_effect=BriefError("missing_task_or_target", "error detail")):
            code = bruriah_main(["brief"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: missing_task_or_target" in captured.err

