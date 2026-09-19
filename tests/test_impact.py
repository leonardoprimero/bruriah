from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.cli import _build_cli_parser, bruriah_main
from bruriah.impact import (
    DecisionImpact,
    ImpactAnalysis,
    ImpactError,
    _get_git_files,
    analyze_impact,
    format_impact_human,
    format_impact_json,
    run_impact,
)


class TestGetGitFiles:
    def test_single_file(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        f = repo / "main.py"
        f.write_text("print('hello')")
        target_type, files = _get_git_files(repo, "main.py")
        assert target_type == "file"
        assert files == ("main.py",)

    def test_directory(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        sub = repo / "pkg"
        sub.mkdir()
        (sub / "a.py").write_text("a")
        (sub / "b.py").write_text("b")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)

        target_type, files = _get_git_files(repo, "pkg")
        assert target_type == "directory"
        assert "pkg/a.py" in files
        assert "pkg/b.py" in files


class TestAnalyzeImpact:
    @pytest.fixture
    def test_env(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Architect"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "arch@example.com"], cwd=repo, check=True)

        # File 1: auth.py, File 2: tokens.py, File 3: session.py
        (repo / "auth.py").write_text("auth code")
        (repo / "tokens.py").write_text("tokens code")
        (repo / "session.py").write_text("session code")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        msg = (
            "feat(auth): unified token and session model\n\n"
            "We build a unified auth model across tokens and sessions."
        )
        subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True)
        commit1 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        # Database setup
        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE documents (document_ref TEXT, relative_path TEXT, metadata TEXT);
            CREATE TABLE passages (ref TEXT, document_ref TEXT, text TEXT, start_line INT);
            CREATE TABLE lineage (successor_ref TEXT, relation TEXT, predecessor_ref TEXT, predecessor_target TEXT);
        """)

        meta1 = json.dumps({"commit": commit1, "verification_date": "2026-02-01"})
        db.execute("INSERT INTO documents VALUES ('doc:auth', 'decisions/001-auth.md', ?)", (meta1,))
        text1 = (
            "# Unified Token and Session Model\n\n"
            f"**Decided:** 2026-02-01 · **Commit:** `{commit1[:8]}` · **Author:** Architect\n\n"
            "All authentication components are co-governed.\n\n"
            "## Files this decision touched\n"
            "- `auth.py`\n"
            "- `tokens.py`\n"
            "- `session.py`\n"
        )
        db.execute("INSERT INTO passages VALUES ('p1', 'doc:auth', ?, 1)", (text1,))

        yield repo, db, commit1

        db.close()

    def test_impact_analysis_finds_blast_radius(self, test_env):
        repo, db, commit1 = test_env
        res = analyze_impact(repo, db, "auth.py")

        assert res.target == "auth.py"
        assert res.target_type == "file"
        assert len(res.decisions) == 1

        dec = res.decisions[0]
        assert dec.subject == "Unified Token and Session Model"
        assert dec.status == "active"
        assert dec.direct_files == ("auth.py",)
        assert "tokens.py" in dec.blast_radius_files
        assert "session.py" in dec.blast_radius_files

        # Blast radius files across all decisions
        assert "tokens.py" in res.total_blast_radius_files
        assert "session.py" in res.total_blast_radius_files
        assert res.risk_level == "MEDIUM"
        assert any("2 co-governed file(s)" in r for r in res.recommendations)

    def test_impact_analysis_critical_when_superseded(self, test_env):
        repo, db, commit1 = test_env

        # Add successor decision doc:auth-v2 that supersedes doc:auth
        meta2 = json.dumps({"commit": "2222222233333333", "verification_date": "2026-03-01"})
        db.execute("INSERT INTO documents VALUES ('doc:auth-v2', 'decisions/002-auth.md', ?)", (meta2,))
        text2 = "# Modern OAuth2 Model\n\n**Decided:** 2026-03-01 · **Commit:** `22222222` · **Author:** Alice\n"
        db.execute("INSERT INTO passages VALUES ('p2', 'doc:auth-v2', ?, 1)", (text2,))
        db.execute("INSERT INTO lineage VALUES ('doc:auth-v2', 'supersedes', 'doc:auth', ?)", (commit1[:8],))

        res = analyze_impact(repo, db, "auth.py")
        assert res.risk_level == "CRITICAL"
        assert any("Stale Architecture" in r for r in res.recommendations)


class TestFormatters:
    def test_format_impact_json(self):
        res = ImpactAnalysis(
            target="src/core.py",
            target_type="file",
            inspected_files=("src/core.py",),
            decisions=(
                DecisionImpact(
                    decision_ref="doc:1",
                    commit_sha="11111111",
                    subject="Core Engine",
                    author="Bob",
                    date="2026-01-01",
                    status="active",
                    direct_files=("src/core.py",),
                    blast_radius_files=("src/engine.py",),
                ),
            ),
            total_blast_radius_files=("src/engine.py",),
            risk_level="MEDIUM",
            recommendations=("Be careful",),
        )
        out = format_impact_json(res)
        data = json.loads(out)
        assert data["target"] == "src/core.py"
        assert data["risk_level"] == "MEDIUM"
        assert data["total_blast_radius_files"] == ["src/engine.py"]

    def test_format_impact_human(self):
        res = ImpactAnalysis(
            target="src/core.py",
            target_type="file",
            inspected_files=("src/core.py",),
            decisions=(
                DecisionImpact(
                    decision_ref="doc:1",
                    commit_sha="11111111",
                    subject="Core Engine",
                    author="Bob",
                    date="2026-01-01",
                    status="active",
                    direct_files=("src/core.py",),
                    blast_radius_files=("src/engine.py",),
                ),
            ),
            total_blast_radius_files=("src/engine.py",),
            risk_level="MEDIUM",
            recommendations=("Test recommendation",),
        )
        out = format_impact_human(res)
        assert "Bruriah Architectural Blast Radius" in out
        assert "Risk Level: MEDIUM" in out
        assert "Core Engine" in out
        assert "src/engine.py" in out
        assert "Test recommendation" in out


class TestImpactCli:
    def test_impact_cli_parsing(self):
        parser = _build_cli_parser()
        args = parser.parse_args(["impact", "src/auth.py", "--json"])
        assert args.target == "src/auth.py"
        assert args.json is True

    def test_impact_cli_dispatch_success(self, capsys):
        sample = ImpactAnalysis(
            target="src/auth.py",
            target_type="file",
            inspected_files=("src/auth.py",),
            decisions=(),
            total_blast_radius_files=(),
            risk_level="LOW",
            recommendations=("Isolated change",),
        )
        with patch("bruriah.cli.run_impact", return_value=sample):
            code = bruriah_main(["impact", "src/auth.py"])
            assert code == 0
            captured = capsys.readouterr()
            assert "Bruriah Architectural Blast Radius — src/auth.py" in captured.out
            assert "Risk Level: LOW" in captured.out

    def test_impact_cli_dispatch_error(self, capsys):
        with patch("bruriah.cli.run_impact", side_effect=ImpactError("git_error", "fatal")):
            code = bruriah_main(["impact", "nonexistent.py"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: git_error" in captured.err
