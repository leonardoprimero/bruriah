from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.cli import _build_cli_parser, bruriah_main
from bruriah.guard import (
    ArchitecturalContract,
    ComplianceReceipt,
    GuardError,
    GuardResult,
    GuardViolation,
    _compute_receipt_digest,
    _generate_agent_context,
    evaluate_guard,
    format_guard_human,
    format_guard_json,
    run_guard,
)


class TestReceiptDigestAndContext:
    def test_deterministic_digest(self):
        d1 = _compute_receipt_digest("src/auth.py", "COMPLIANT", ["src/auth.py"], ["doc:1"], 0)
        d2 = _compute_receipt_digest("src/auth.py", "COMPLIANT", ["src/auth.py"], ["doc:1"], 0)
        assert d1 == d2
        assert len(d1) == 64

        d3 = _compute_receipt_digest("src/auth.py", "NON_COMPLIANT", ["src/auth.py"], ["doc:1"], 1)
        assert d1 != d3

    def test_generate_agent_context(self):
        contract = ArchitecturalContract(
            decision_ref="doc:auth",
            decision_title="OAuth2 Security Architecture",
            decision_sha="11111111",
            governed_files=("src/auth.py", "src/tokens.py"),
            directives=("Maintain OAuth2 tokens without session cookies.",),
        )
        violation = GuardViolation(
            file_path="src/auth.py",
            severity="VETO",
            decision_title="OAuth2 Security Architecture",
            decision_sha="11111111",
            message="Drift detected.",
            active_successor_title="New Auth Model",
            active_successor_sha="22222222",
        )

        ctx = _generate_agent_context([contract], [violation])
        assert "Bruriah Architectural Guard — Active Governance Directives" in ctx
        assert "OAuth2 Security Architecture" in ctx
        assert "Maintain OAuth2 tokens without session cookies." in ctx
        assert "[VETO] `src/auth.py`" in ctx
        assert "Drift detected." in ctx
        assert "New Auth Model" in ctx


class TestEvaluateGuard:
    @pytest.fixture
    def guard_env(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Architect"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "arch@example.com"], cwd=repo, check=True)

        (repo / "auth.py").write_text("auth code")
        (repo / "tokens.py").write_text("token code")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "feat(auth): initial auth"], cwd=repo, check=True)
        commit1 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        db = sqlite3.connect(":memory:")
        db.executescript("""
            CREATE TABLE documents (document_ref TEXT, relative_path TEXT, metadata TEXT);
            CREATE TABLE passages (ref TEXT, document_ref TEXT, text TEXT, start_line INT);
            CREATE TABLE lineage (successor_ref TEXT, relation TEXT, predecessor_ref TEXT, predecessor_target TEXT);
        """)

        meta1 = json.dumps({"commit": commit1, "verification_date": "2026-01-01"})
        db.execute("INSERT INTO documents VALUES ('doc:auth', 'decisions/001.md', ?)", (meta1,))
        text1 = (
            "# Initial Auth Architecture\n\n"
            f"**Decided:** 2026-01-01 · **Commit:** `{commit1[:8]}` · **Author:** Architect\n\n"
            "Auth rules.\n\n"
            "## Files this decision touched\n"
            "- `auth.py`\n"
            "- `tokens.py`\n"
        )
        db.execute("INSERT INTO passages VALUES ('p1', 'doc:auth', ?, 1)", (text1,))

        yield repo, db, commit1

        db.close()

    def test_guard_passed_standalone(self, guard_env):
        repo, db, commit1 = guard_env
        # Clean inspection
        res = evaluate_guard(repo, db, "auth.py", strict=False, generate_receipt=False, engram=False)
        assert res.status == "PASSED"
        assert len(res.contracts) == 1
        assert res.contracts[0].decision_title == "Initial Auth Architecture"
        assert res.violations == ()
        assert res.receipt is None
        assert res.engram_synced is False

    def test_guard_receipt_generation_standalone(self, guard_env):
        repo, db, commit1 = guard_env
        res = evaluate_guard(repo, db, "auth.py", generate_receipt=True, engram=False)
        assert res.status == "PASSED"
        assert res.receipt is not None
        assert res.receipt.status == "COMPLIANT"
        assert len(res.receipt.digest) == 64
        # Ensured engram was NOT synced since engram=False
        assert res.engram_synced is False
        assert not (repo / ".engram").exists()

    def test_guard_optional_engram_sync(self, guard_env):
        repo, db, commit1 = guard_env
        res = evaluate_guard(repo, db, "auth.py", generate_receipt=True, engram=True)
        assert res.status == "PASSED"
        assert res.engram_synced is True
        receipt_file = repo / ".engram" / "compliance-receipt.json"
        assert receipt_file.exists()
        data = json.loads(receipt_file.read_text(encoding="utf-8"))
        assert data["status"] == "COMPLIANT"

    def test_guard_violation_and_strict_veto(self, guard_env):
        repo, db, commit1 = guard_env

        # Supersede doc:auth with doc:auth-v2
        meta2 = json.dumps({"commit": "22222222bbbbbbbb"})
        db.execute("INSERT INTO documents VALUES ('doc:auth-v2', 'decisions/002.md', ?)", (meta2,))
        text2 = "# Next Auth Architecture\n\n**Decided:** 2026-02-01 · **Commit:** `22222222` · **Author:** Alice\n"
        db.execute("INSERT INTO passages VALUES ('p2', 'doc:auth-v2', ?, 1)", (text2,))
        db.execute("INSERT INTO lineage VALUES ('doc:auth-v2', 'supersedes', 'doc:auth', ?)", (commit1[:8],))

        # Without strict -> WARNING
        res_warn = evaluate_guard(repo, db, "auth.py", strict=False)
        assert res_warn.status == "WARNING"
        assert len(res_warn.violations) == 1
        assert res_warn.violations[0].severity == "WARNING"

        # With strict -> VETOED
        res_veto = evaluate_guard(repo, db, "auth.py", strict=True, generate_receipt=True)
        assert res_veto.status == "VETOED"
        assert res_veto.violations[0].severity == "VETO"
        assert res_veto.receipt.status == "NON_COMPLIANT"


class TestFormatters:
    def test_format_guard_human(self):
        res = GuardResult(
            target="src/main.py",
            status="PASSED",
            inspected_files=("src/main.py",),
            contracts=(
                ArchitecturalContract(
                    decision_ref="doc:1",
                    decision_title="Main Core",
                    decision_sha="11111111",
                    governed_files=("src/main.py",),
                    directives=("Directive 1",),
                ),
            ),
            violations=(),
            agent_context="Context",
            receipt=ComplianceReceipt(
                receipt_version="1.0",
                timestamp="2026-09-19T00:00:00Z",
                target="src/main.py",
                status="COMPLIANT",
                inspected_count=1,
                inspected_files=("src/main.py",),
                governing_decision_refs=("doc:1",),
                violation_count=0,
                digest="abcdef123456",
            ),
            engram_synced=False,
        )
        out = format_guard_human(res)
        assert "Bruriah Architectural Guard" in out
        assert "Status: ✅ PASSED" in out
        assert "Main Core" in out
        assert "Compliance Receipt (RDD):" in out

    def test_format_guard_json(self):
        res = GuardResult(
            target="src/main.py",
            status="PASSED",
            inspected_files=("src/main.py",),
            contracts=(),
            violations=(),
            agent_context="",
        )
        out = format_guard_json(res)
        data = json.loads(out)
        assert data["target"] == "src/main.py"
        assert data["status"] == "PASSED"


class TestGuardCli:
    def test_guard_cli_parsing(self):
        parser = _build_cli_parser()
        args = parser.parse_args(["guard", "src/auth.py", "--strict", "--receipt", "--agent"])
        assert args.target == "src/auth.py"
        assert args.strict is True
        assert args.receipt is True
        assert args.agent is True
        assert args.engram is False  # Default is strictly False

    def test_guard_cli_dispatch_passed(self, capsys):
        sample = GuardResult(
            target="src/auth.py",
            status="PASSED",
            inspected_files=("src/auth.py",),
            contracts=(),
            violations=(),
            agent_context="Agent prompt context",
        )
        with patch("bruriah.cli.run_guard", return_value=sample):
            code = bruriah_main(["guard", "src/auth.py"])
            assert code == 0
            captured = capsys.readouterr()
            assert "Status: ✅ PASSED" in captured.out

    def test_guard_cli_dispatch_agent_mode(self, capsys):
        sample = GuardResult(
            target="src/auth.py",
            status="PASSED",
            inspected_files=("src/auth.py",),
            contracts=(),
            violations=(),
            agent_context="AGENT_CONTEXT_ONLY",
        )
        with patch("bruriah.cli.run_guard", return_value=sample):
            code = bruriah_main(["guard", "src/auth.py", "--agent"])
            assert code == 0
            captured = capsys.readouterr()
            assert captured.out.strip() == "AGENT_CONTEXT_ONLY"

    def test_guard_cli_dispatch_vetoed(self):
        sample = GuardResult(
            target="src/auth.py",
            status="VETOED",
            inspected_files=("src/auth.py",),
            contracts=(),
            violations=(
                GuardViolation(
                    file_path="src/auth.py",
                    severity="VETO",
                    decision_title="Old Auth",
                    decision_sha="11111111",
                    message="Drift",
                ),
            ),
            agent_context="",
        )
        with patch("bruriah.cli.run_guard", return_value=sample):
            code = bruriah_main(["guard", "src/auth.py"])
            assert code == 1

    def test_guard_cli_dispatch_error(self, capsys):
        with patch("bruriah.cli.run_guard", side_effect=GuardError("git_error", "fatal")):
            code = bruriah_main(["guard", "nonexistent.py"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: git_error" in captured.err
