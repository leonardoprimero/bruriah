from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from git_corpus import build, main  # noqa: E402

# This script is exercised end to end against a REAL repository rather than a mocked `git`, because
# the defect it shipped with was invisible to any mock: the format string was built with literal NUL
# bytes, and a process argument cannot contain one, so it raised before git was ever invoked. A test
# that stubbed subprocess would have passed.


def _repo(tmp_path: Path, commits: list[tuple[str, str]]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Tester")
    for index, (subject, body) in enumerate(commits):
        (repo / f"f{index}.txt").write_text(str(index))
        run("git", "add", "-A")
        message = f"{subject}\n\n{body}" if body else subject
        run("git", "commit", "-q", "-m", message)
    return repo


def test_a_real_repository_yields_one_document_per_reasoned_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [
        ("add the thing", "Because the other approach could not express the constraint."),
        ("fix typo", ""),
        ("remove the thing", "It turned out the constraint was wrong."),
    ])
    written = build(repo, tmp_path / "out")
    assert written == 2, "commits without a body carry no reasoning and are skipped"
    documents = sorted(p.name for p in (tmp_path / "out").glob("*.md"))
    assert len(documents) == 2
    from datetime import date
    for name in documents:  # documents lead with the decision date, so they sort chronologically
        assert date.fromisoformat(name[:10])


def test_the_document_carries_the_reasoning_and_its_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [("choose postgres", "Because we need transactional DDL.")])
    build(repo, tmp_path / "out")
    text = next((tmp_path / "out").glob("*.md")).read_text()
    assert "choose postgres" in text
    assert "Because we need transactional DDL." in text
    assert "**Decided:**" in text and "**Commit:**" in text
    assert "Files this decision touched" in text


def test_a_history_with_no_reasoning_says_so_rather_than_failing(tmp_path: Path, capsys) -> None:
    # A repo of "wip" and "fix" messages has nothing to retrieve. Saying that plainly is more useful
    # than writing zero files and exiting silently.
    repo = _repo(tmp_path, [("wip", ""), ("fix", "")])
    assert main(["--repo", str(repo), "--out", str(tmp_path / "out")]) == 0
    assert "no reasoning to retrieve" in capsys.readouterr().err


def test_a_non_repository_is_refused(tmp_path: Path, capsys) -> None:
    assert main(["--repo", str(tmp_path), "--out", str(tmp_path / "out")]) == 1
    assert "not a git repository" in capsys.readouterr().err


def test_the_script_never_writes_into_the_repository_it_reads(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [("a decision", "with a reason")])
    before = {p.name for p in repo.iterdir()}
    build(repo, tmp_path / "out")
    assert {p.name for p in repo.iterdir()} == before
