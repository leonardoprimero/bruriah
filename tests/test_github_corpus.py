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

from bruriah import github_corpus
from bruriah.corpus import CorpusPolicy, parse_document
from bruriah.github_corpus import (
    GitHubCorpusError,
    build_documents,
    detect_repo_slug,
)
from bruriah.github_read import ResponseCache, _RawResponse
from bruriah.gitcorpus import WalkedCommit, walk_commits
from bruriah.index import BuildConfig, build_candidate

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
        assert metadata.source == "github"
        assert metadata.github_issue == 41
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
        assert "bruriah_source: github" in frontmatter
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
# build_documents -- a timeline cross-reference from another repository is skipped and counted,
# never fetched (R3-001): a real GitHub timeline can cross-reference a fork or a downstream
# project, and that source's number means nothing in the corpus repo.
# ---------------------------------------------------------------------------


class TestTimelineCrossReferenceFromAnotherRepoIsSkipped:
    def _build(self, tmp_path: Path, *, transport: Any):
        cache = ResponseCache(tmp_path / "cache")
        cache.set("/repos/acme/widget/issues/200", _load("issue-200.json"))
        cache.set("/repos/acme/widget/issues/200/timeline", _load("issue-200-timeline.json"))
        commits = [_commit("h" * 40, "fix: note the fork reference", "Closes #200.")]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache,
            network_enabled=True, transport=transport,
        )
        return tmp_path / "out", result

    def test_neither_foreign_source_is_ever_fetched(self, tmp_path: Path) -> None:
        def _never_called(*_args: Any, **_kwargs: Any) -> _RawResponse:
            raise AssertionError("a foreign-repo timeline cross-reference must never be fetched")

        out, result = self._build(tmp_path, transport=_never_called)
        # Document still built -- a bug that fetches the wrong same-numbered PR/issue in the corpus
        # repo (or 404s trying) must not drop the whole issue document.
        assert result.documents_written == 1
        assert result.issues_skipped == 0

    def test_foreign_sources_never_appear_as_alternatives(self, tmp_path: Path) -> None:
        def _never_called(*_args: Any, **_kwargs: Any) -> _RawResponse:
            raise AssertionError("a foreign-repo timeline cross-reference must never be fetched")

        out, _ = self._build(tmp_path, transport=_never_called)
        policy = CorpusPolicy(include=("*.md",), exclude=())
        doc_path = next(out.glob("*issue-200*.md"))
        document = parse_document(doc_path, out, policy)
        assert document.metadata.alternatives == ()

    def test_cross_repo_skipped_counts_both_resolution_paths(self, tmp_path: Path) -> None:
        def _never_called(*_args: Any, **_kwargs: Any) -> _RawResponse:
            raise AssertionError("a foreign-repo timeline cross-reference must never be fetched")

        _, result = self._build(tmp_path, transport=_never_called)
        # #47 resolved via source.issue.repository.full_name, #999 via source.issue.html_url.
        assert result.cross_repo_skipped == 2
        manifest = json.loads((tmp_path / "out" / "github-manifest.json").read_text(encoding="utf-8"))
        assert manifest["counts"]["cross_repo_skipped"] == 2


# ---------------------------------------------------------------------------
# _collect_alternatives -- a timeline `cross-referenced` event fires once per *mention*, not once
# per PR/issue: the same PR can be cross-referenced more than once, and two distinct PRs can share
# a title. Both used to survive as duplicate `alternatives` frontmatter entries colliding on the
# `(name, document_ref)` primary key `index.py`'s `alternatives` table enforces -- see
# `TestDuplicateAlternativesStillIndex` for the reproduction through the real index build.
# ---------------------------------------------------------------------------


class TestCollectAlternativesDeduplicatesCrossReferences:
    def _ledger(self, tmp_path: Path, transport: Any) -> Any:
        cache = ResponseCache(tmp_path / "cache")
        return github_corpus._Ledger(
            cache=cache, owner="acme", repo="widget", token=None, network_enabled=True,
            transport=transport, clock=lambda: 0.0, sleep=lambda _seconds: None,
        )

    def test_same_pr_cross_referenced_twice_yields_one_alternative_and_one_fetch(
        self, tmp_path: Path
    ) -> None:
        fetch_counts: dict[str, int] = {}

        def transport(method: str, url: str, token: str | None) -> _RawResponse:
            fetch_counts[url] = fetch_counts.get(url, 0) + 1
            if url.endswith("/issues/41/timeline"):
                event = {
                    "event": "cross-referenced",
                    "source": {"issue": {
                        "number": 310,
                        "html_url": "https://github.com/acme/widget/pull/310",
                        "pull_request": {"merged_at": None},
                    }},
                }
                body: Any = [event, event]
            elif url.endswith("/pulls/310"):
                body = {
                    "title": "Fix: retry storm", "state": "closed", "merged_at": None,
                    "closed_at": "2026-08-01T00:00:00Z",
                }
            elif url.endswith("/issues/310/comments"):
                body = []
            else:
                raise AssertionError(f"unexpected fetch: {url}")
            return _RawResponse(status=200, headers={}, body=json.dumps(body).encode())

        ledger = self._ledger(tmp_path, transport)
        alternatives, cross_repo_skipped = github_corpus._collect_alternatives(41, ledger, "acme/widget")

        assert cross_repo_skipped == 0
        assert len(alternatives) == 1
        assert alternatives[0]["name"] == "Fix: retry storm"
        assert fetch_counts["https://api.github.com/repos/acme/widget/pulls/310"] == 1

    def test_two_distinct_prs_with_the_same_title_both_survive_disambiguated(
        self, tmp_path: Path
    ) -> None:
        def transport(method: str, url: str, token: str | None) -> _RawResponse:
            if url.endswith("/issues/41/timeline"):
                body: Any = [
                    {
                        "event": "cross-referenced",
                        "source": {"issue": {
                            "number": 320,
                            "html_url": "https://github.com/acme/widget/pull/320",
                            "pull_request": {"merged_at": None},
                        }},
                    },
                    {
                        "event": "cross-referenced",
                        "source": {"issue": {
                            "number": 321,
                            "html_url": "https://github.com/acme/widget/pull/321",
                            "pull_request": {"merged_at": None},
                        }},
                    },
                ]
            elif url.endswith("/pulls/320"):
                # Trailing whitespace a title should never have carried into the corpus.
                body = {
                    "title": "Fix: retry with backoff  ", "state": "closed", "merged_at": None,
                    "closed_at": "2026-08-01T00:00:00Z",
                }
            elif url.endswith("/pulls/321"):
                # Same title after normalization, spelled with a doubled internal space instead.
                body = {
                    "title": "Fix: retry  with backoff", "state": "closed", "merged_at": None,
                    "closed_at": "2026-08-02T00:00:00Z",
                }
            elif url.endswith("/comments"):
                body = []
            else:
                raise AssertionError(f"unexpected fetch: {url}")
            return _RawResponse(status=200, headers={}, body=json.dumps(body).encode())

        ledger = self._ledger(tmp_path, transport)
        alternatives, _ = github_corpus._collect_alternatives(41, ledger, "acme/widget")

        names = [alt["name"] for alt in alternatives]
        assert names == ["Fix: retry with backoff", "Fix: retry with backoff (#321)"]


# ---------------------------------------------------------------------------
# build_documents + index.build_candidate -- a document whose timeline cross-referenced the same
# PR twice used to carry two identical `alternatives` entries and die with a raw
# `sqlite3.IntegrityError` ~4.5 minutes into a real `bruriah index` build, because the
# `alternatives` table's primary key is `(name, document_ref)`.
# ---------------------------------------------------------------------------


class TestDuplicateAlternativesStillIndex:
    def test_document_with_a_duplicate_cross_reference_indexes_without_error(
        self, tmp_path: Path
    ) -> None:
        cache = ResponseCache(tmp_path / "cache")
        cache.set("/repos/acme/widget/issues/300", _load("issue-300.json"))
        cache.set("/repos/acme/widget/issues/300/timeline", _load("issue-300-timeline.json"))
        cache.set("/repos/acme/widget/pulls/47", _load("pull-47.json"))
        cache.set("/repos/acme/widget/issues/47/comments", _load("pull-47-comments.json"))
        out = tmp_path / "out"
        commits = [_commit("f" * 40, "fix: note", "Closes #300.")]
        result = build_documents(
            commits, out, repo="acme/widget", cache=cache, network_enabled=False,
        )
        assert result.documents_written == 1

        policy = CorpusPolicy(include=("*.md",), exclude=())
        doc_path = next(out.glob("*issue-300*.md"))
        document = parse_document(doc_path, out, policy)
        assert len(document.metadata.alternatives) == 1

        fingerprint = json.dumps({
            "artifact": "model.onnx", "artifact_sha256": "a" * 64, "pooling": "mean",
            "runtime": "fastembed==0.8.0", "snapshot": "snapshot-a", "source": "example/model",
        }, sort_keys=True)
        policy_path = tmp_path / "policy.yaml"
        policy_path.write_text("version: 1\ninclude: ['*.md']\nexclude: []\n", encoding="utf-8")
        config = BuildConfig(
            root=out, policy_path=policy_path, schema_version=1,
            parser_version="corpus-v2", service_version="0.1.0", mcp_range=">=1.28.1,<2",
            embedding_model="test/minilm", embedding_revision="snapshot-a",
            embedding_dimensions=3, embedding_fingerprint=fingerprint, ranking_config="rrf-v1",
        )

        def fake_embeddings(texts: list[str]) -> list[bytes]:
            import hashlib
            return [hashlib.sha256(text.encode()).digest()[:12] for text in texts]

        index_result = build_candidate(
            config, tmp_path / "candidate.sqlite3", policy, fake_embeddings,
        )
        assert index_result.documents == 1


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


class TestRateLimitWindowStopsFurtherFetches:
    """R4-002: once one fetch reports the hourly rate-limit window closed, the rest of the build
    must not attempt another network call -- cache hits are still served -- and the manifest must
    say so instead of silently truncating the corpus."""

    def test_uncached_issues_after_the_window_closes_are_skipped_without_a_second_call(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cache = ResponseCache(tmp_path / "cache")
        _seed(cache, "acme", "widget")  # issue #41 and its dependencies are all cache-warm

        calls = {"n": 0}

        def _transport(method: str, url: str, token: str | None) -> _RawResponse:
            calls["n"] += 1
            if calls["n"] > 1:
                raise AssertionError("no transport call after the rate limit window closes")
            return _RawResponse(
                status=403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4000"},
                body=b'{"message": "rate limited"}',
            )

        commits = [
            _commit("a" * 40, "fix: race condition", "Closes #41."),  # cache hit
            _commit("i" * 40, "chore: first uncached issue", "Fixes #900."),
            _commit("j" * 40, "chore: second uncached issue", "Fixes #901."),
        ]
        result = build_documents(
            commits, tmp_path / "out", repo="acme/widget", cache=cache,
            network_enabled=True, transport=_transport, clock=lambda: 0.0, sleep=lambda _s: None,
        )

        assert result.documents_written == 1  # #41, served entirely from cache
        assert result.issues_skipped == 2  # #900 and #901
        assert calls["n"] == 1  # only the first uncached issue ever reaches the transport

        manifest = json.loads((tmp_path / "out" / "github-manifest.json").read_text(encoding="utf-8"))
        assert manifest["counts"]["rate_limited_skipped"] == 2
        assert manifest["rate_limited_until"] == "1970-01-01T01:06:40Z"  # epoch 4000, UTC

        err = capsys.readouterr().err
        rate_limit_lines = [line for line in err.splitlines() if "rate limit" in line]
        assert len(rate_limit_lines) == 1
        assert "2" in rate_limit_lines[0]


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
