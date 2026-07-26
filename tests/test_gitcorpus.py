"""Step one of the documented workflow, and the one that has to survive being installed.

`bruriah.gitcorpus` used to live in `scripts/git_corpus.py`. No wheel ships `scripts/`, so the
front page told anyone who ran `pip install bruriah` to execute a file they did not have. These
tests cover the behaviour AND the distribution: shipping is part of the contract here, not an
afterthought, because the failure was invisible to a suite that ran from a clone.
"""
from __future__ import annotations

import shutil
import sys
import subprocess
import zipfile
from pathlib import Path

import pytest

from bruriah import cli, gitcorpus

ROOT = Path(__file__).resolve().parents[1]


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-q", "-m", "feat: add the thing\n\nBecause the other way needed two round trips.")
    (repo / "b.txt").write_text("two")
    run("add", "-A")
    run("commit", "-q", "-m", "chore: tidy")  # no body: records what changed, never why
    return repo


def test_only_commits_that_explain_themselves_become_documents(tmp_path: Path) -> None:
    written = gitcorpus.build(_repo(tmp_path), tmp_path / "out")
    assert written == 1
    documents = list((tmp_path / "out").glob("*.md"))
    assert len(documents) == 1
    body = documents[0].read_text(encoding="utf-8")
    assert "Because the other way needed two round trips." in body
    assert "chore: tidy" not in body


def test_the_document_carries_the_provenance_that_makes_it_worth_retrieving(tmp_path: Path) -> None:
    gitcorpus.build(_repo(tmp_path), tmp_path / "out")
    body = next((tmp_path / "out").glob("*.md")).read_text(encoding="utf-8")
    # Without the date and the commit, this is an anonymous opinion rather than a decision record.
    assert "**Decided:**" in body and "**Commit:**" in body and "**Author:**" in body


def test_it_writes_nothing_to_the_repository_it_reads(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            capture_output=True, text=True).stdout
    gitcorpus.build(repo, tmp_path / "out")
    after = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                           capture_output=True, text=True).stdout
    assert before == after == ""


def test_the_subcommand_refuses_a_directory_that_is_not_a_repository(tmp_path: Path) -> None:
    code = cli.bruriah_main(["corpus", "--repo", str(tmp_path), "--out", str(tmp_path / "out"),
                             "--data-dir", str(tmp_path / "d"), "--config-dir", str(tmp_path / "c")])
    assert code != 0


def test_the_subcommand_produces_what_the_module_produces(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code = cli.bruriah_main(["corpus", "--repo", str(repo), "--out", str(tmp_path / "viacli"),
                             "--data-dir", str(tmp_path / "d"), "--config-dir", str(tmp_path / "c")])
    assert code == 0
    gitcorpus.build(repo, tmp_path / "direct")
    viacli = {path.name: path.read_text() for path in (tmp_path / "viacli").glob("*.md")}
    direct = {path.name: path.read_text() for path in (tmp_path / "direct").glob("*.md")}
    assert viacli == direct and viacli


def test_the_corpus_builder_is_importable_from_the_installed_package() -> None:
    """The actual regression: `scripts/` is not distributed, so step one has to live in the package.

    Importing it from `bruriah` is what a wheel gives you; a test that shelled out to
    `scripts/git_corpus.py` would keep passing from a clone while the published package stayed
    broken -- which is exactly what happened."""
    assert gitcorpus.build.__module__ == "bruriah.gitcorpus"
    assert "corpus" in cli._build_cli_parser().format_help()


@pytest.mark.skipif(not (ROOT / "pyproject.toml").is_file(), reason="not a source checkout")
def test_the_built_wheel_actually_contains_it(tmp_path: Path) -> None:
    """Built, not asserted about. The distribution is the thing that was wrong before."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    built = subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path)],
                           cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    wheel = next(Path(tmp_path).glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    assert "bruriah/gitcorpus.py" in names
    # The signed packs and skill bodies have to travel too, or an installed copy has no policy.
    assert any(name.endswith("trust-roots.json") for name in names)
    assert sum(name.endswith("SKILL.md") for name in names) == 6


def test_windows_is_refused_with_an_answer_rather_than_a_traceback() -> None:
    """The wheel is py3-none-any, so pip installs it on Windows and the first command exploded.

    A bare `ModuleNotFoundError: No module named 'fcntl'` from several frames deep reads as a
    broken package, not an unsupported platform. Verified by importing the module fresh with
    `fcntl` made invisible -- the way Windows presents it -- rather than by reading the source."""
    import importlib

    real_find = importlib.util.find_spec
    module = "bruriah"
    saved = {name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith(module)}
    try:
        importlib.util.find_spec = lambda name, *a, **k: (
            None if name == "fcntl" else real_find(name, *a, **k))
        with pytest.raises(ImportError) as raised:
            importlib.import_module(module)
    finally:
        importlib.util.find_spec = real_find
        sys.modules.update(saved)
    message = str(raised.value)
    assert "macOS or Linux" in message and "WSL" in message
    assert "open an issue" in message, "a refusal should say what would change it"
