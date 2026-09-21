"""Tests for `bruriah.github_corpus`: the issue/PR document builder behind `bruriah corpus
--github`, and `bruriah.gitcorpus.walk_commits`, the additive commit-walk it consumes.

Every scenario runs fully offline against `tests/fixtures/github/` (see that directory's
README.md for what each fixture represents): a `ResponseCache` is pre-seeded from the recorded
JSON, `network_enabled=False` proves the network path is never taken for a warm cache, and a
scripted `Transport` proves the not-found path without ever opening a socket.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from bruriah.corpus import CorpusPolicy, parse_document
from bruriah.github_corpus import (
    GitHubCorpusError,
    build_documents,
    detect_repo_slug,
)
from bruriah.github_read import ResponseCache, _RawResponse
from bruriah.gitcorpus import WalkedCommit, walk_commits

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _seed(cache: ResponseCache, owner: str, repo: str) -> None:
    """Pre-populate `cache` with every recorded fixture, keyed exactly as `github_read` keys them."""
    cache.set(f"/repos/{owner}/{repo}/issues/41", _load("issue-41.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/41/timeline", _load("issue-41-timeline.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/50", _load("pull-50.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/47", _load("pull-47.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/47/comments", _load("pull-47-comments.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/39", _load("issue-39.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/39/comments", _load("issue-39-comments.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/100", _load("issue-100.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/100/timeline", _load("issue-100-timeline.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/101", _load("pull-101.json"))
    cache.set(f"/repos/{owner}/{repo}/pulls/102", _load("pull-102.json"))
    cache.set(f"/repos/{owner}/{repo}/issues/102/comments", _load("pull-102-comments.json"))


def _commit(sha: str, subject: str, body: str = "", date: str = "2026-07-15T10:00:00-03:00") -> WalkedCommit:
    return WalkedCommit(sha=sha, date=date, subject=subject, body=body)


# ---------------------------------------------------------------------------
# gitcorpus.walk_commits
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-q", "-m", "feat: add the thing (#101)")  # bodiless squash-style subject
    (repo / "b.txt").write_text("two")
    run("add", "-A")
    run("commit", "-q", "-m", "fix: race condition\n\nCloses #41.")
    return repo


class TestWalkCommits:
    def test_includes_bodiless_commits_build_would_skip(self, tmp_path: Path) -> None:
        commits = walk_commits(_repo(tmp_path))
        assert len(commits) == 2
        subjects = {c.subject for c in commits}
        assert subjects == {"feat: add the thing (#101)", "fix: race condition"}
        bodiless = next(c for c in commits if c.subject == "feat: add the thing (#101)")
        assert bodiless.body == ""
        assert len(bodiless.sha) == 40

    def test_an_unresolvable_revision_still_fails_before_writing_anything(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            walk_commits(_repo(tmp_path), revision="not-a-real-revision")


# ---------------------------------------------------------------------------
# detect_repo_slug
# ---------------------------------------------------------------------------


class TestDetectRepoSlug:
    def _with_origin(self, tmp_path: Path, url: str) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True, capture_output=True)
        return repo

    def test_https_remote(self, tmp_path: Path) -> None:
        repo = self._with_origin(tmp_path, "https://github.com/acme/widget.git")
        assert detect_repo_slug(repo) == "acme/widget"

    def test_ssh_remote(self, tmp_path: Path) -> None:
        repo = self._with_origin(tmp_path, "git@github.com:acme/widget.git")
        assert detect_repo_slug(repo) == "acme/widget"

    def test_https_remote_without_dot_git_suffix(self, tmp_path: Path) -> None:
        repo = self._with_origin(tmp_path, "https://github.com/acme/widget")
        assert detect_repo_slug(repo) == "acme/widget"

    def test_unrecognized_remote_raises_a_clear_error(self, tmp_path: Path) -> None:
        repo = self._with_origin(tmp_path, "https://gitlab.com/acme/widget.git")
        with pytest.raises(GitHubCorpusError):
            detect_repo_slug(repo)

    def test_no_origin_remote_raises_a_clear_error(self, tmp_path: Path) -> None:
        repo = tmp_path / "bare"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
        with pytest.raises(GitHubCorpusError):
            detect_repo_slug(repo)


# ---------------------------------------------------------------------------
# build_documents -- issue #41 scenario (merged PR, unmerged+commented PR, a Premise: line)
# ---------------------------------------------------------------------------


class TestBuildDocumentsIssue41:
    def _build(self, tmp_path: Path):
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")
        out = tmp_path / "out"
        commits = [_commit("a" * 40, "fix: race condition", "Closes #41.")]
        result = build_documents(
            commits, out, repo="acme/widget", cache=cache, network_enabled=False,
        )
        return out, result

    def test_writes_one_document_for_the_linked_issue(self, tmp_path: Path) -> None:
        out, result = self._build(tmp_path)
        docs = list(out.glob("*.md"))
        assert len(docs) == 1
        assert result.documents_written == 1
        assert docs[0].name == "2026-06-10-issue-41-race-condition-when-two-builds-run-concurrently.md"

    def test_front_matter_round_trips_through_metadata(self, tmp_path: Path) -> None:
        out, _ = self._build(tmp_path)
        doc_path = next(out.glob("*.md"))
        policy = CorpusPolicy(include=("*.md",), exclude=())
        document = parse_document(doc_path, out, policy)
        metadata = document.metadata
        assert metadata.commit == "a" * 40
        assert len(metadata.alternatives) == 1
        alt = metadata.alternatives[0]
        assert alt["name"] == "Attempt: retry on lock contention instead of locking"
        assert alt["disposition"] == "rejected"
        assert "file-lock approach in #50" in alt["reason"]
        assert len(metadata.premises) == 1
        assert metadata.premises[0]["statement"] == "The corpus writer is not process-safe."
        assert metadata.provenance_urls == ("https://github.com/acme/widget/issues/41",)

    def test_body_names_the_linked_commit_and_the_rejected_alternative(self, tmp_path: Path) -> None:
        out, _ = self._build(tmp_path)
        text = next(out.glob("*.md")).read_text(encoding="utf-8")
        assert "# Race condition when two builds run concurrently" in text
        assert "## Rejected alternatives" in text
        assert "Attempt: retry on lock contention instead of locking" in text
        assert "## Linked decisions" in text
        assert f"`{'a' * 8}`" in text
        assert "fix: race condition" in text
        # The merged PR must never appear as a rejected alternative.
        assert "Fix race condition with a per-build file lock" not in text

    def test_raw_frontmatter_carries_exactly_the_documented_keys(self, tmp_path: Path) -> None:
        out, _ = self._build(tmp_path)
        text = next(out.glob("*.md")).read_text(encoding="utf-8")
        frontmatter = text.split("---\n", 2)[1]
        assert "commit:" in frontmatter
        assert "issue: 41" in frontmatter
        assert "github_url:" in frontmatter
        assert "source_url:" in frontmatter
        assert "alternatives:" in frontmatter
        assert "premises:" in frontmatter
        # Not part of the documented keyset for this feature.
        assert "supersedes" not in frontmatter
        assert "deprecates" not in frontmatter


# ---------------------------------------------------------------------------
# build_documents -- issue #100 scenario (no premises, empty-comment fallback, not_planned
# cross-referenced issue, squash-merge self-reference, two commits linking the same issue)
# ---------------------------------------------------------------------------


class TestBuildDocumentsIssue100:
    def _build(self, tmp_path: Path):
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")
        out = tmp_path / "out"
        commits = [
            _commit("b" * 40, "fix: corpus writer under load", "Closes #100."),
            # Squash-merge subject, empty body: resolves through PR #101's own body ("Fixes #100").
            _commit("c" * 40, "Add a per-build file lock (#101)", ""),
        ]
        result = build_documents(
            commits, out, repo="acme/widget", cache=cache, network_enabled=False,
        )
        return out, result

    def test_one_document_lists_both_linking_commits(self, tmp_path: Path) -> None:
        out, result = self._build(tmp_path)
        docs = list(out.glob("*issue-100*.md"))
        assert len(docs) == 1
        assert result.documents_written == 1
        text = docs[0].read_text(encoding="utf-8")
        assert f"`{'b' * 8}`" in text
        assert f"`{'c' * 8}`" in text

    def test_no_premises_when_the_issue_body_has_none(self, tmp_path: Path) -> None:
        out, _ = self._build(tmp_path)
        policy = CorpusPolicy(include=("*.md",), exclude=())
        doc_path = next(out.glob("*issue-100*.md"))
        document = parse_document(doc_path, out, policy)
        assert document.metadata.premises == ()

    def test_two_rejected_alternatives_pr_and_not_planned_issue(self, tmp_path: Path) -> None:
        out, _ = self._build(tmp_path)
        policy = CorpusPolicy(include=("*.md",), exclude=())
        doc_path = next(out.glob("*issue-100*.md"))
        document = parse_document(doc_path, out, policy)
        names = {alt["name"] for alt in document.metadata.alternatives}
        assert names == {
            "Attempt: optimistic concurrency instead of locking",
            "Duplicate: builds sometimes corrupt the corpus",
        }
        by_name = {alt["name"]: alt for alt in document.metadata.alternatives}
        # PR #102 has an empty comment list -> the fixed fallback phrase.
        assert by_name["Attempt: optimistic concurrency instead of locking"]["reason"] == (
            "closed without merge; no closing comment"
        )
        # Issue #39 has a real closing comment.
        assert "Confirmed duplicate of #41" in by_name["Duplicate: builds sometimes corrupt the corpus"]["reason"]
        # The merged PR #101 must never appear.
        assert "Add a per-build file lock" not in names


# ---------------------------------------------------------------------------
# build_documents -- cross-repo links are skipped and counted, never fetched
# ---------------------------------------------------------------------------


class TestCrossRepoLinksAreSkipped:
    def test_cross_repo_reference_is_skipped_and_counted_without_a_fetch(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path / "cache")

        def _never_called(*_args: Any, **_kwargs: Any) -> _RawResponse:
            raise AssertionError("a cross-repo link must never be fetched")

        commits = [_commit("d" * 40, "chore: note", "Fixes other/widget#77.")]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache,
            network_enabled=True, transport=_never_called,
        )
        assert result.documents_written == 0
        assert result.cross_repo_skipped == 1


# ---------------------------------------------------------------------------
# build_documents -- failure policy: skip-and-warn, never abort the whole build
# ---------------------------------------------------------------------------


class TestSkipAndWarnOnFailure:
    def test_not_found_issue_is_skipped_with_a_warning_and_the_build_continues(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")

        def _transport(method: str, url: str, token: str | None) -> _RawResponse:
            assert "999" in url
            return _RawResponse(status=404, headers={}, body=b"{}")

        commits = [
            _commit("e" * 40, "chore: reference a missing issue", "Fixes #999."),
            _commit("a" * 40, "fix: race condition", "Closes #41."),
        ]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache,
            network_enabled=True, transport=_transport,
        )
        assert result.documents_written == 1  # #41 still gets built
        assert result.issues_skipped == 1
        err = capsys.readouterr().err
        assert "999" in err

    def test_a_persistent_5xx_is_skipped_with_a_warning_not_raised(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")

        def _always_500(method: str, url: str, token: str | None) -> _RawResponse:
            return _RawResponse(status=500, headers={}, body=b"{}")

        commits = [
            _commit("g" * 40, "chore: reference a struggling endpoint", "Fixes #997."),
            _commit("a" * 40, "fix: race condition", "Closes #41."),
        ]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache,
            network_enabled=True, transport=_always_500,
            clock=lambda: 0.0, sleep=lambda _seconds: None,
        )
        assert result.documents_written == 1
        assert result.issues_skipped == 1
        assert "997" in capsys.readouterr().err

    def test_offline_cache_miss_is_skipped_with_a_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")
        commits = [
            _commit("f" * 40, "chore: reference an uncached issue", "Fixes #998."),
            _commit("a" * 40, "fix: race condition", "Closes #41."),
        ]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache, network_enabled=False,
        )
        assert result.documents_written == 1
        assert result.issues_skipped == 1
        assert "998" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_documents -- manifest
# ---------------------------------------------------------------------------


class TestManifest:
    def test_manifest_is_written_with_counts_and_deterministic_mapping(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")
        out = tmp_path / "out"
        commits = [
            _commit("b" * 40, "fix: corpus writer under load", "Closes #100."),
            _commit("a" * 40, "fix: race condition", "Closes #41."),
            _commit("d" * 40, "chore: note", "Fixes other/widget#77."),
        ]
        result = build_documents(
            commits, out, repo="acme/widget", cache=cache, network_enabled=False, revision="deadbeef",
        )
        manifest_path = out / "github-manifest.json"
        assert manifest_path == result.manifest_path
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["repo"] == "acme/widget"
        assert manifest["revision"] == "deadbeef"
        assert manifest["counts"]["commits_scanned"] == 3
        assert manifest["counts"]["cross_repo_skipped"] == 1
        assert manifest["counts"]["issues_fetched"] == 2
        assert manifest["documents"]["41"].endswith(".md")
        assert manifest["documents"]["100"].endswith(".md")
        # Deterministic: numbers sorted ascending regardless of commit input order.
        assert list(manifest["documents"].keys()) == ["41", "100"]


# ---------------------------------------------------------------------------
# build_documents -- a PR closes-target (not just a cross-referenced source) is still buildable
# ---------------------------------------------------------------------------


class TestClosesTargetThatIsItselfAPullRequest:
    def test_a_commit_that_closes_a_pull_request_number_does_not_crash(self, tmp_path: Path) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")
        # #50 is a merged PR (see pull-50.json); expose it through the issues endpoint too, the way
        # GitHub itself does (an issue payload for a PR carries a `pull_request` key).
        pr_as_issue = dict(_load("pull-50.json"))
        pr_as_issue["pull_request"] = {"url": "https://api.github.com/repos/acme/widget/pulls/50"}
        pr_as_issue.setdefault("state_reason", None)
        cache.set("/repos/acme/widget/issues/50", pr_as_issue)
        cache.set("/repos/acme/widget/issues/50/timeline", [])
        commits = [_commit("a" * 40, "chore: track merged PR", "Closes #50.")]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache, network_enabled=False,
        )
        assert result.documents_written == 1
