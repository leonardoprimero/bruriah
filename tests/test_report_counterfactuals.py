"""Tests for `evals/retrieval/report_counterfactuals.py` (T4, offline half, of
`odd/tasks/github-issue-pr-ingestion.md`): whether the index holds rejected alternatives or
premises traceable back to the GitHub issue an evaluation question names, and how many of the
engine's alternatives/premises came from GitHub-derived documents versus commit-derived ones.

Runs fully offline. One GitHub-derived document is built the same way `test_github_corpus.py`'s
`TestBuildDocumentsIssue41` does, over the recorded fixtures in `tests/fixtures/github/` (issue
#41: one rejected alternative from the unmerged, commented PR #47, one premise from the issue
body's `Premise:` line -- see that file for what each fixture represents). One commit-derived
document is hand-written with its own rejected alternative and no premise, so the GitHub/commit
split in the totals is exercised on both sides of the comparison. Both are indexed with the fake
embedder `test_cli.py` uses, then read back through `report_counterfactuals.main` exactly as the
CLI would invoke it, never through the module's private helpers.
"""
from __future__ import annotations

import json
import sys
from array import array
from pathlib import Path

import pytest

from bruriah import cli
from bruriah.gitcorpus import WalkedCommit
from bruriah.github_corpus import build_documents
from bruriah.github_read import ResponseCache
from bruriah.platform import resolve_paths

_ROOT = Path(__file__).resolve().parents[1]
_EVALS_RETRIEVAL = _ROOT / "evals" / "retrieval"
if str(_EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(_EVALS_RETRIEVAL))

import report_counterfactuals  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "github"
_FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed_issue_41(cache: ResponseCache, owner: str, repo: str) -> None:
    """The subset of `test_github_corpus.py`'s `_seed` needed for issue #41: one merged PR (must
    never become a rejected alternative), one unmerged+commented PR (the alternative), and the
    issue body's own `Premise:` line."""
    cache.set(f"/repos/{owner}/{repo}/issues/41", _load("issue-41.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/41/timeline", _load("issue-41-timeline.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/50", _load("pull-50.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/47", _load("pull-47.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/47/comments", _load("pull-47-comments.json"))


def _fake_embedder_factory(model_name: str) -> tuple[object, str, int]:
    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

    return embed, _FINGERPRINT, 3


def _build_corpus(tmp_path: Path) -> Path:
    out = tmp_path / "corpus"
    cache = ResponseCache(tmp_path / "github-cache")
    _seed_issue_41(cache, "acme", "widget")
    commits = [
        WalkedCommit(
            sha="a" * 40, date="2026-07-15T10:00:00-03:00", subject="fix: race condition",
            body="Closes #41.",
        )
    ]
    build_documents(commits, out, repo="acme/widget", cache=cache, network_enabled=False)

    # A commit-derived document, indistinguishable from `github_corpus` output except by its
    # filename: its own rejected alternative and no premise, so both totals' GitHub/commit split
    # has a non-zero commit-side count to prove against.
    (out / "2024-01-01-deadbeef-unrelated-decision.md").write_text(
        "---\n"
        "commit: deadbeef\n"
        "alternatives:\n"
        "  - name: Retry with exponential backoff\n"
        "    disposition: rejected\n"
        "    reason: adds latency nobody asked for\n"
        "---\n\n"
        "# Unrelated decision\n\n"
        "A commit-derived decision with a rejected alternative of its own, real corpus text "
        "unrelated to any GitHub issue, long enough to be indexed as a real passage.\n",
        encoding="utf-8",
    )
    return out


def _index(tmp_path: Path, out: Path) -> Path:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['*.md']\nexclude: []\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    paths = resolve_paths(
        cli_config_dir=tmp_path / "config", cli_data_dir=data_dir, cli_cache_dir=tmp_path / "cache",
        cli_log_dir=tmp_path / "log", env={},
    )
    cli.run_index(paths, out, policy_path, model_name="test/minilm", embedder_factory=_fake_embedder_factory)
    return data_dir


def _questions_file(tmp_path: Path) -> Path:
    rows = [
        {
            "id": "issue-hit", "query": "race condition", "type": "factual",
            "provenance": {"commit": "a" * 12, "issue": 41},
            "ground_truth": {"must_include": ["x"], "acceptable": []},
        },
        {
            "id": "no-issue", "query": "unrelated decision", "type": "factual",
            "provenance": {"commit": "deadbeef" * 5},
            "ground_truth": {"must_include": ["x"], "acceptable": []},
        },
    ]
    path = tmp_path / "questions.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_reports_traceable_alternatives_and_premises_for_a_question_with_provenance_issue(
    tmp_path: Path,
) -> None:
    out = _build_corpus(tmp_path)
    data_dir = _index(tmp_path, out)
    questions_path = _questions_file(tmp_path)
    out_path = tmp_path / "rows.jsonl"

    exit_code = report_counterfactuals.main(
        [
            "--corpus", "test-corpus", "--data-dir", str(data_dir), "--questions", str(questions_path),
            "--out", str(out_path), "--json",
        ],
        embedder_factory=_fake_embedder_factory,
    )
    assert exit_code == 0

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    # Only the question carrying `provenance.issue` is reported per-question; "no-issue" is not.
    assert len(rows) == 1
    assert rows[0] == {
        "id": "issue-hit", "issue": 41, "has_issue_document": True,
        "alternatives_traceable": True, "premises_traceable": True,
    }


def test_totals_split_alternatives_and_premises_by_github_vs_commit_provenance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    out = _build_corpus(tmp_path)
    data_dir = _index(tmp_path, out)
    questions_path = _questions_file(tmp_path)

    exit_code = report_counterfactuals.main(
        [
            "--corpus", "test-corpus", "--data-dir", str(data_dir), "--questions", str(questions_path),
            "--json",
        ],
        embedder_factory=_fake_embedder_factory,
    )
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["questions_total"] == 2
    assert result["questions_with_issue"] == 1
    assert result["questions_with_issue_document"] == 1
    assert result["questions_with_alternative"] == 1
    assert result["questions_with_premise"] == 1
    assert result["alternatives_total"] == 2
    assert result["alternatives_github"] == 1
    assert result["alternatives_commit"] == 1
    assert result["premises_total"] == 1
    assert result["premises_github"] == 1
    assert result["premises_commit"] == 0
