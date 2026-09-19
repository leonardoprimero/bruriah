from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.bootstrap import (
    BootstrapError,
    BootstrapResult,
    MinedDecision,
    _extract_trailers,
    _slugify,
    format_decision_markdown,
    mine_git_history,
    run_bootstrap,
    score_commit,
    write_decisions,
)
from bruriah.cli import _build_cli_parser, bruriah_main


class TestScoreCommit:
    def test_high_architectural_score(self):
        subject = "refactor(auth): migrate to oauth2 and deprecate session cookies"
        body = (
            "We decided to migrate authentication to OAuth2 because session cookies "
            "do not support our multi-region distributed setup.\n"
            "This was chosen instead of JWT due to revocation trade-offs.\n"
            "Supersedes: 11111111"
        )
        files = ["src/auth/oauth.py", "src/auth/cookies.py", "src/core/security.py", "src/api/routes.py"]

        score, reasons = score_commit(subject, body, files)
        assert score >= 0.8
        assert "refactoring" in reasons
        assert "migration/deprecation" in reasons
        assert "explicit lineage trailer" in reasons
        assert "touches core architectural components" in reasons

    def test_low_chore_score(self):
        subject = "chore: bump version to 1.2.3"
        body = ""
        files = ["package.json"]

        score, reasons = score_commit(subject, body, files)
        assert score <= 0.2
        assert any("routine" in r or "bump" in r for r in reasons)

    def test_score_clamped(self):
        score, _ = score_commit("merge branch 'main'", "", [])
        assert score >= 0.0

        score_high, _ = score_commit(
            "feat!: redesign architecture and migrate protocol",
            "detailed reasoning\n" * 10 + "Supersedes: 12345678\nBecause of reasons",
            ["core/a.py", "arch/b.py", "api/c.py", "engine/d.py"],
        )
        assert score_high <= 1.0


class TestSlugifyAndTrailers:
    def test_slugify(self):
        assert _slugify("feat(auth): Add OAuth2 Support!") == "featauth-add-oauth2-support"
        assert _slugify("Simple Title") == "simple-title"

    def test_extract_trailers(self):
        body = "Some notes\nSupersedes: 1a2b3c4d\nother: 123"
        assert _extract_trailers(body) == ["1a2b3c4d"]

    def test_extract_trailers_none(self):
        assert _extract_trailers("No trailers here") == []


class TestFormatAndWrite:
    def test_format_decision_markdown(self):
        dec = MinedDecision(
            sha="abcdef1234567890",
            author="Alice",
            date="2026-03-15",
            subject="Migrate to Microservices",
            body="Decompose monolith into services.",
            files=("src/main.py", "src/service.py"),
            score=0.85,
            reasons=("architectural keyword",),
            inferred_supersedes=("11111111",),
        )
        md = format_decision_markdown(dec)
        assert "commit: abcdef1234567890" in md
        assert "supersedes:" in md
        assert "- 11111111" in md
        assert "# Migrate to Microservices" in md
        assert "**Decided:** 2026-03-15 · **Commit:** `abcdef12` · **Author:** Alice" in md
        assert "Decompose monolith into services." in md
        assert "- `src/main.py`" in md
        assert "- `src/service.py`" in md

    def test_write_decisions(self, tmp_path):
        out_dir = tmp_path / "decisions"
        dec = MinedDecision(
            sha="abcdef1234567890",
            author="Alice",
            date="2026-03-15",
            subject="Migrate to Microservices",
            body="Notes",
            files=(),
            score=0.85,
            reasons=(),
        )
        count = write_decisions([dec], out_dir)
        assert count == 1
        written_files = list(out_dir.glob("*.md"))
        assert len(written_files) == 1
        assert "2026-03-15-abcdef12" in written_files[0].name


class TestMineGitHistory:
    @pytest.fixture
    def git_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Architect"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "arch@example.com"], cwd=repo, check=True)

        # Commit 1: Initial monolithic architecture
        f1 = repo / "core.py"
        f1.write_text("initial code")
        subprocess.run(["git", "add", "core.py"], cwd=repo, check=True)
        msg1 = (
            "feat(core): initial architecture design\n\n"
            "We decided to use a monolithic structure because we need rapid prototyping.\n"
            "All components reside in core.py."
        )
        subprocess.run(["git", "commit", "-m", msg1], cwd=repo, check=True)

        # Commit 2: Routine chore
        f2 = repo / "lint.txt"
        f2.write_text("lint")
        subprocess.run(["git", "add", "lint.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "chore: fix formatting"], cwd=repo, check=True)

        # Commit 3: Architectural refactoring superseding commit 1
        f1.write_text("refactored code")
        subprocess.run(["git", "add", "core.py"], cwd=repo, check=True)
        msg3 = (
            "refactor(core): redesign and migrate monolith to modular architecture\n\n"
            "Because monolith no longer scales with the team size.\n"
            "We decompose core.py into services."
        )
        subprocess.run(["git", "commit", "-m", msg3], cwd=repo, check=True)

        return repo

    def test_mine_git_history_filters_and_infers(self, git_repo):
        total, candidates = mine_git_history(git_repo, min_score=0.4)
        assert total == 3
        # Should pick up Commit 1 and Commit 3, skipping Commit 2 (chore)
        assert len(candidates) == 2
        assert "initial architecture design" in candidates[0].subject
        assert "redesign and migrate" in candidates[1].subject

        # Commit 3 touches core.py and is a refactor, so it should infer supersedes on Commit 1
        assert len(candidates[1].inferred_supersedes) == 1
        assert candidates[1].inferred_supersedes[0] == candidates[0].sha[:8]

    def test_run_bootstrap_dry_run(self, git_repo, tmp_path):
        out = tmp_path / "decisions"
        res = run_bootstrap(git_repo, out, min_score=0.4, dry_run=True)
        assert res.total_commits_scanned == 3
        assert res.candidates_found == 2
        assert res.decisions_written == 0
        assert not out.exists()

    def test_run_bootstrap_writes_files(self, git_repo, tmp_path):
        out = tmp_path / "decisions"
        res = run_bootstrap(git_repo, out, min_score=0.4, dry_run=False)
        assert res.decisions_written == 2
        assert out.exists()
        files = list(out.glob("*.md"))
        assert len(files) == 2


class TestBootstrapCli:
    def test_bootstrap_cli_parsing(self):
        parser = _build_cli_parser()
        args = parser.parse_args(["bootstrap", "--out-dir", "my_decisions", "--limit", "20", "--dry-run"])
        assert args.out_dir == Path("my_decisions")
        assert args.limit == 20
        assert args.dry_run is True

    def test_bootstrap_cli_dispatch_success(self, capsys):
        sample = BootstrapResult(
            total_commits_scanned=10,
            candidates_found=2,
            decisions_written=2,
            out_dir=Path("decisions"),
            decisions=(
                MinedDecision(
                    sha="1111111122222222",
                    author="Alice",
                    date="2026-01-01",
                    subject="Initial Architecture",
                    body="Body",
                    files=("core.py",),
                    score=0.8,
                    reasons=("architectural keyword",),
                ),
            ),
            dry_run=False,
        )
        with patch("bruriah.cli.run_bootstrap", return_value=sample):
            code = bruriah_main(["bootstrap"])
            assert code == 0
            captured = capsys.readouterr()
            assert "Bruriah Bootstrap — Mined 10 commits" in captured.out
            assert "Found 2 candidate architectural decision(s)" in captured.out
            assert "Initial Architecture" in captured.out

    def test_bootstrap_cli_dispatch_error(self, capsys):
        with patch("bruriah.cli.run_bootstrap", side_effect=BootstrapError("git_error", "fatal")):
            code = bruriah_main(["bootstrap"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: git_error" in captured.err
