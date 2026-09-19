from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bruriah.cli import _build_cli_parser, bruriah_main
from bruriah.lens import (
    FileLensResult,
    LensError,
    LineLens,
    _group_contiguous_lines,
    _parse_blame_porcelain,
    compute_file_lens,
    format_lens_human,
    format_lens_json,
    run_lens,
)


class TestParseBlamePorcelain:
    def test_single_commit_multiple_lines(self):
        sample = (
            "1111111111111111111111111111111111111111 1 1 2\n"
            "author Alice\n"
            "author-mail <alice@example.com>\n"
            "author-time 1700000000\n"
            "author-tz +0000\n"
            "committer Alice\n"
            "summary Initial commit\n"
            "filename test.py\n"
            "\tline 1\n"
            "1111111111111111111111111111111111111111 2 2\n"
            "\tline 2\n"
        )
        lines = _parse_blame_porcelain(sample)
        assert len(lines) == 2
        assert lines[0] == (1, "1111111111111111111111111111111111111111", "Alice", "2023-11-14")
        assert lines[1] == (2, "1111111111111111111111111111111111111111", "Alice", "2023-11-14")

    def test_multiple_commits(self):
        sample = (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 1 1 1\n"
            "author Bob\n"
            "author-time 1700000000\n"
            "summary A\n"
            "filename test.py\n"
            "\tline 1\n"
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb 1 2 1\n"
            "author Charlie\n"
            "author-time 1700100000\n"
            "summary B\n"
            "filename test.py\n"
            "\tline 2\n"
        )
        lines = _parse_blame_porcelain(sample)
        assert len(lines) == 2
        assert lines[0][1] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert lines[0][2] == "Bob"
        assert lines[1][1] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        assert lines[1][2] == "Charlie"

    def test_empty_output(self):
        assert _parse_blame_porcelain("") == []


class TestGroupContiguousLines:
    def test_groups_contiguous_lines_same_commit(self):
        lines_info = [
            (1, "sha1", "Alice", "2026-01-01"),
            (2, "sha1", "Alice", "2026-01-01"),
            (3, "sha1", "Alice", "2026-01-01"),
            (4, "sha2", "Bob", "2026-01-02"),
        ]
        groups = _group_contiguous_lines(lines_info)
        assert len(groups) == 2
        assert groups[0] == (1, 3, "sha1", "Alice", "2026-01-01")
        assert groups[1] == (4, 4, "sha2", "Bob", "2026-01-02")

    def test_non_contiguous_same_commit(self):
        lines_info = [
            (1, "sha1", "Alice", "2026-01-01"),
            (2, "sha2", "Bob", "2026-01-02"),
            (3, "sha1", "Alice", "2026-01-01"),
        ]
        groups = _group_contiguous_lines(lines_info)
        assert len(groups) == 3
        assert groups[0] == (1, 1, "sha1", "Alice", "2026-01-01")
        assert groups[1] == (2, 2, "sha2", "Bob", "2026-01-02")
        assert groups[2] == (3, 3, "sha1", "Alice", "2026-01-01")

    def test_empty_list(self):
        assert _group_contiguous_lines([]) == []


class TestComputeFileLens:
    def test_compute_file_lens_with_git_and_sqlite(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

        test_file = repo / "auth.py"
        test_file.write_text("line1\nline2\nline3\n")
        subprocess.run(["git", "add", "auth.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial auth"], cwd=repo, check=True)
        commit1 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        # Update line 2
        test_file.write_text("line1\nline2_modified\nline3\n")
        subprocess.run(["git", "commit", "-am", "update line 2"], cwd=repo, check=True)
        commit2 = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        # SQLite database
        db = sqlite3.connect(":memory:")
        try:
            db.executescript("""
                CREATE TABLE documents (document_ref TEXT, relative_path TEXT, metadata TEXT);
                CREATE TABLE passages (ref TEXT, document_ref TEXT, text TEXT, start_line INT);
                CREATE TABLE lineage (successor_ref TEXT, relation TEXT, predecessor_ref TEXT);
            """)

            # Decision 1 for commit 1 (superseded)
            meta1 = json.dumps({"commit": commit1})
            db.execute("INSERT INTO documents VALUES ('doc:1', '001.md', ?)", (meta1,))
            db.execute("INSERT INTO passages VALUES ('p1', 'doc:1', '# Initial Auth Model', 1)")

            # Decision 2 for commit 2 (supersedes Decision 1)
            meta2 = json.dumps({"commit": commit2})
            db.execute("INSERT INTO documents VALUES ('doc:2', '002.md', ?)", (meta2,))
            db.execute("INSERT INTO passages VALUES ('p2', 'doc:2', '# Advanced Auth Model', 1)")

            db.execute("INSERT INTO lineage VALUES ('doc:2', 'supersedes', 'doc:1')")

            result = compute_file_lens(repo, db, "auth.py")
            assert result.file_path == "auth.py"
            assert result.total_lines == 3
            assert result.indexed_decisions_count == 3
            assert result.stale_decisions_count == 2  # Lines 1 and 3 are commit 1 (superseded)

            # Check lenses
            assert len(result.lenses) == 3
            l1 = result.lenses[0]
            assert l1.line_start == 1
            assert l1.line_end == 1
            assert l1.status == "supersedes"
            assert l1.successor_title == "Advanced Auth Model"

            l2 = result.lenses[1]
            assert l2.line_start == 2
            assert l2.line_end == 2
            assert l2.status == "active"
            assert l2.decision_title == "Advanced Auth Model"
        finally:
            db.close()

    def test_compute_file_lens_not_in_git(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

        db = sqlite3.connect(":memory:")
        try:
            with pytest.raises(LensError) as exc:
                compute_file_lens(repo, db, "nonexistent.py")
            assert exc.value.code == "file_not_in_git"
        finally:
            db.close()


class TestFormatters:
    def test_format_lens_json(self):
        res = FileLensResult(
            file_path="src/main.py",
            lenses=(
                LineLens(
                    line_start=1,
                    line_end=10,
                    commit_sha="abcdef12",
                    author="Alice",
                    date="2026-01-01",
                    decision_title="Core Architecture",
                    decision_ref="doc:1",
                    status="active",
                ),
            ),
            total_lines=10,
            indexed_decisions_count=1,
            stale_decisions_count=0,
        )
        output = format_lens_json(res)
        data = json.loads(output)
        assert data["file_path"] == "src/main.py"
        assert len(data["lenses"]) == 1
        assert data["lenses"][0]["status"] == "active"

    def test_format_lens_human(self):
        res = FileLensResult(
            file_path="src/main.py",
            lenses=(
                LineLens(
                    line_start=1,
                    line_end=5,
                    commit_sha="abcdef12",
                    author="Alice",
                    date="2026-01-01",
                    decision_title="Old Architecture",
                    decision_ref="doc:1",
                    status="supersedes",
                    alert_relation="supersedes",
                    successor_title="New Architecture",
                    successor_sha="12345678",
                ),
                LineLens(
                    line_start=6,
                    line_end=10,
                    commit_sha="12345678",
                    author="Bob",
                    date="2026-02-01",
                    status="unindexed",
                ),
            ),
            total_lines=10,
            indexed_decisions_count=1,
            stale_decisions_count=1,
        )
        output = format_lens_human(res)
        assert "Bruriah Inline Archaeology" in output
        assert "⚠️  Old Architecture" in output
        assert "SUPERSEDES by New Architecture" in output
        assert "⚪ 12345678 · Bob" in output


class TestLensCli:
    def test_lens_cli_parsing(self):
        parser = _build_cli_parser()
        args = parser.parse_args(["lens", "src/auth.py", "--json"])
        assert args.file == "src/auth.py"
        assert args.json is True

    def test_lens_cli_dispatch_success(self):
        sample = FileLensResult(
            file_path="test.py",
            lenses=(),
            total_lines=0,
            indexed_decisions_count=0,
            stale_decisions_count=0,
        )
        with patch("bruriah.cli.run_lens", return_value=sample):
            code = bruriah_main(["lens", "test.py", "--json"])
            assert code == 0

    def test_lens_cli_dispatch_error(self, capsys):
        with patch("bruriah.cli.run_lens", side_effect=LensError("file_not_in_git")):
            code = bruriah_main(["lens", "nonexistent.py"])
            assert code == 1
            captured = capsys.readouterr()
            assert "bruriah: error: file_not_in_git" in captured.err
