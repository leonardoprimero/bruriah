from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.decide import (
    Alternative,
    DecideError,
    DecisionRecord,
    execute_git_commit,
    run_decide,
    validate_predecessor_sha,
)


class TestDecisionRecordFormatting:
    def test_format_commit_message(self):
        record = DecisionRecord(
            title="feat(auth): adopt OAuth2 authorization code flow",
            problem="Session cookies were prone to CSRF attacks across subdomains.",
            solution="Migrate to OAuth2 authorization code with PKCE and short-lived tokens.",
            invariants=(
                "Access tokens must never exceed 15 minutes TTL.",
                "Refresh tokens must be stored in secure HTTP-only cookies.",
            ),
            alternatives=(
                Alternative(
                    name="JWT in localStorage",
                    tradeoff="Zero server state but vulnerable to XSS token theft.",
                    rejected_reason="Security policy forbids unencrypted credential storage.",
                ),
            ),
            supersedes=("e8f3003bda26",),
            amends=(),
            deprecates=(),
        )

        msg = record.format_commit_message()
        assert msg.startswith("feat(auth): adopt OAuth2 authorization code flow\n\n")
        assert "## Context & Problem" in msg
        assert "Session cookies were prone to CSRF attacks" in msg
        assert "## Decision & Solution" in msg
        assert "Migrate to OAuth2 authorization code with PKCE" in msg
        assert "## Invariants Established" in msg
        assert "- Access tokens must never exceed 15 minutes TTL." in msg
        assert "## Alternatives Considered" in msg
        assert "**JWT in localStorage**" in msg
        assert "Rejected because: Security policy forbids" in msg
        assert "Supersedes: e8f3003bda26" in msg

    def test_format_adr_markdown(self):
        record = DecisionRecord(
            title="Adopt Hexagonal Architecture",
            problem="Domain logic was tightly coupled to SQLite and fastembed.",
            solution="Introduce port interfaces and separate domain algorithms into pure modules.",
            invariants=("Domain models must have zero external dependencies.",),
            alternatives=(),
            supersedes=(),
            amends=("11223344",),
            deprecates=(),
        )

        adr = record.format_adr_markdown()
        assert "# ADR: Adopt Hexagonal Architecture" in adr
        assert "## Status\nAccepted" in adr
        assert "## Context\nDomain logic was tightly coupled" in adr
        assert "## Decision\nIntroduce port interfaces" in adr
        assert "- Domain models must have zero external dependencies." in adr
        assert "## Lineage Relations\n- Amends: `11223344`" in adr


class TestPredecessorValidation:
    def test_validate_predecessor_found_in_why(self):
        mock_db = MagicMock()
        with patch("bruriah.decide.find_decision_in_database") as mock_find:
            mock_find.return_value = MagicMock(commit_sha="abcdef123456")
            sha = validate_predecessor_sha(mock_db, "abcdef12")
            assert sha == "abcdef123456"

    def test_validate_predecessor_not_found(self):
        mock_db = MagicMock()
        with patch("bruriah.decide.find_decision_in_database", return_value=None):
            mock_db.execute.return_value.fetchone.return_value = None
            with pytest.raises(DecideError) as exc:
                validate_predecessor_sha(mock_db, "nonexistent")
            assert "predecessor_not_found" in str(exc.value)


class TestGitCommitExecution:
    def test_execute_git_commit_no_staged_changes(self, tmp_path: Path):
        with patch("subprocess.run") as mock_run:
            # git diff --cached --quiet returns 0 when no changes
            mock_run.return_value.returncode = 0
            with pytest.raises(DecideError) as exc:
                execute_git_commit(tmp_path, "test message")
            assert "no_staged_changes" in str(exc.value)


class TestDecideCli:
    def test_decide_cli_parsing(self):
        from bruriah.cli import _build_cli_parser

        parser = _build_cli_parser()
        args = parser.parse_args(
            [
                "decide",
                "--title",
                "feat: adopt OAuth2",
                "--problem",
                "session vulnerability",
                "--solution",
                "use OAuth2",
                "--invariants",
                "inv1",
                "inv2",
                "--alternative",
                "OptionA:TradeoffA:WhyA",
                "--supersedes",
                "11223344",
                "--commit",
                "--adr",
                "--json",
            ]
        )
        assert args.title == "feat: adopt OAuth2"
        assert args.problem == "session vulnerability"
        assert args.solution == "use OAuth2"
        assert args.invariants == ["inv1", "inv2"]
        assert args.alternative == ["OptionA:TradeoffA:WhyA"]
        assert args.supersedes == ["11223344"]
        assert args.commit is True
        assert args.adr is True
        assert args.json is True

    def test_decide_cli_dispatch_formatted_message(self, capsys):
        from bruriah.cli import bruriah_main

        sample_record = DecisionRecord(
            title="feat: title",
            problem="prob",
            solution="sol",
            invariants=("inv1",),
        )
        with patch("bruriah.cli.run_decide", return_value=(sample_record, None)):
            code = bruriah_main(
                [
                    "decide",
                    "--title",
                    "feat: title",
                    "--problem",
                    "prob",
                    "--solution",
                    "sol",
                ]
            )
            assert code == 0
            captured = capsys.readouterr()
            assert "feat: title" in captured.out
            assert "## Context & Problem" in captured.out
            assert "## Decision & Solution" in captured.out

    def test_decide_cli_dispatch_adr_mode(self, capsys):
        from bruriah.cli import bruriah_main

        sample_record = DecisionRecord(
            title="feat: title",
            problem="prob",
            solution="sol",
            invariants=("inv1",),
        )
        with patch("bruriah.cli.run_decide", return_value=(sample_record, None)):
            code = bruriah_main(
                [
                    "decide",
                    "--title",
                    "feat: title",
                    "--problem",
                    "prob",
                    "--solution",
                    "sol",
                    "--adr",
                ]
            )
            assert code == 0
            captured = capsys.readouterr()
            assert "# ADR: feat: title" in captured.out
            assert "## Status\nAccepted" in captured.out

    def test_decide_cli_dispatch_json_mode(self, capsys):
        from bruriah.cli import bruriah_main

        sample_record = DecisionRecord(
            title="feat: title",
            problem="prob",
            solution="sol",
            invariants=("inv1",),
        )
        with patch("bruriah.cli.run_decide", return_value=(sample_record, None)):
            code = bruriah_main(
                [
                    "decide",
                    "--title",
                    "feat: title",
                    "--problem",
                    "prob",
                    "--solution",
                    "sol",
                    "--json",
                ]
            )
            assert code == 0
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["title"] == "feat: title"

    def test_decide_cli_missing_fields_non_interactive(self, capsys):
        from bruriah.cli import bruriah_main

        with patch("sys.stdin.isatty", return_value=False):
            code = bruriah_main(["decide"])
            assert code == 1
            captured = capsys.readouterr()
            assert "missing_required_fields" in captured.err
