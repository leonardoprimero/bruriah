"""Unit tests for `scripts/changelog_section.py`, the release workflow's notes extractor.

Uses a small temporary CHANGELOG passed via `--path` so these tests never depend on the real
CHANGELOG.md's current content.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "changelog_section.py"

_SAMPLE_CHANGELOG = """\
# Changelog

Notable changes, newest first.

## [Unreleased]

### Changed
- Something not yet released.

## [1.3.1] — 2026-09-20

### Fixed
- A bug that mattered.
- Another line in the same section.

## [1.3.0] — 2026-09-20

### Added
- A feature.
"""


@pytest.fixture
def sample_changelog(tmp_path: Path) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(_SAMPLE_CHANGELOG, encoding="utf-8")
    return path


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_prints_the_body_of_an_existing_version(sample_changelog: Path) -> None:
    result = _run("1.3.1", "--path", str(sample_changelog))
    assert result.returncode == 0
    assert "### Fixed" in result.stdout
    assert "A bug that mattered." in result.stdout
    assert "Another line in the same section." in result.stdout


def test_body_excludes_the_heading_line(sample_changelog: Path) -> None:
    result = _run("1.3.1", "--path", str(sample_changelog))
    assert "## [1.3.1]" not in result.stdout


def test_body_stops_before_the_next_section(sample_changelog: Path) -> None:
    result = _run("1.3.1", "--path", str(sample_changelog))
    assert "A feature." not in result.stdout
    assert "## [1.3.0]" not in result.stdout


def test_accepts_unreleased(sample_changelog: Path) -> None:
    result = _run("Unreleased", "--path", str(sample_changelog))
    assert result.returncode == 0
    assert "Something not yet released." in result.stdout


def test_unknown_version_exits_one_with_a_stderr_message(sample_changelog: Path) -> None:
    result = _run("9.9.9", "--path", str(sample_changelog))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "9.9.9" in result.stderr
