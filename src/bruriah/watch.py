"""Continuous auto-indexing watcher for Bruriah.

Watches a Git repository for new commits, branch switches, or merges, and
automatically updates the corpus and index in the background using incremental
snapshot reuse.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import gitcorpus
from .cli import (
    EmbedderFactory,
    _default_embedder_factory,
    run_index,
)
from .corpus import CorpusPolicyError
from .index import BuildResult, IndexLifecycleError
from .platform import PlatformPaths, ensure_private_dirs


class WatchError(ValueError):
    """Typed failure for watcher operations."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def get_head_sha(repo: Path) -> str | None:
    """Resolve current HEAD commit SHA, or None if repo is empty or invalid."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except OSError:
        pass
    return None


def get_git_state_fingerprint(repo: Path) -> str:
    """Compute a lightweight fingerprint of the git repository's commit state.

    Combines current HEAD commit SHA with mtime of git ref pointers.
    """
    head_sha = get_head_sha(repo) or "empty"
    git_dir = repo / ".git"
    if git_dir.is_file():
        # Submodule or worktree: parse gitdir pointer
        try:
            content = git_dir.read_text(encoding="utf-8").strip()
            if content.startswith("gitdir:"):
                target = Path(content[len("gitdir:") :].strip())
                git_dir = target if target.is_absolute() else (repo / target).resolve()
        except OSError:
            pass

    mtimes: list[str] = [head_sha]
    for rel_path in ("HEAD", "refs/heads", "logs/HEAD"):
        p = git_dir / rel_path
        if p.exists():
            try:
                mtimes.append(f"{rel_path}:{p.stat().st_mtime_ns}")
            except OSError:
                pass
    return "|".join(mtimes)


class RepoWatcher:
    """Continuously monitors git repository state and triggers incremental index updates."""

    def __init__(
        self,
        repo: Path,
        paths: PlatformPaths,
        *,
        model_name: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        embedder_factory: EmbedderFactory = _default_embedder_factory,
        query_prefix: str | None = None,
        passage_prefix: str | None = None,
        interval: float = 2.0,
        policy_path: Path | None = None,
        on_sync: Callable[[BuildResult], None] | None = None,
    ):
        self.repo = repo.resolve()
        if not (self.repo / ".git").exists():
            raise WatchError("not_a_git_repository")

        self.paths = paths
        self.model_name = model_name
        self.embedder_factory = embedder_factory
        self.query_prefix = query_prefix
        self.passage_prefix = passage_prefix
        self.interval = max(0.1, interval)
        self.policy_path = policy_path or (self.paths.config_dir / "policy.yaml")
        self.on_sync = on_sync
        self.last_fingerprint: str | None = None

    def sync(self) -> BuildResult | None:
        """Execute one cycle of corpus generation and incremental index build."""
        ensure_private_dirs(self.paths)
        if not self.policy_path.exists():
            self.policy_path.write_text("version: 1\ninclude: ['**']\nexclude: ['private/**']\n", encoding="utf-8")

        corpus_root = self.paths.data_dir / "corpus"
        corpus_result = gitcorpus.build(self.repo, corpus_root)
        if not corpus_result.written:
            return None

        result = run_index(
            self.paths,
            corpus_root.resolve(),
            self.policy_path.resolve(),
            model_name=self.model_name,
            embedder_factory=self.embedder_factory,
            query_prefix=self.query_prefix,
            passage_prefix=self.passage_prefix,
        )
        if self.on_sync:
            self.on_sync(result)
        return result

    def watch(
        self,
        *,
        once: bool = False,
        max_iterations: int | None = None,
    ) -> int:
        """Run the watch loop. Exits on KeyboardInterrupt or max_iterations."""
        iteration = 0
        try:
            while max_iterations is None or iteration < max_iterations:
                iteration += 1
                current_fingerprint = get_git_state_fingerprint(self.repo)
                if current_fingerprint != self.last_fingerprint:
                    sha = get_head_sha(self.repo) or "unknown"
                    short_sha = sha[:8]
                    print(
                        f"[bruriah watch] Change detected at {short_sha}. Updating index...",
                        file=sys.stderr,
                    )
                    start_time = time.monotonic()
                    try:
                        result = self.sync()
                        elapsed = time.monotonic() - start_time
                        if result:
                            print(
                                f"[bruriah watch] Synced in {elapsed:.2f}s: "
                                f"{result.documents} docs ({result.reused_documents} reused), "
                                f"{result.passages} passages.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                "[bruriah watch] No explanatory commit decisions found to index.",
                                file=sys.stderr,
                            )
                    except (CorpusPolicyError, IndexLifecycleError, OSError, ValueError) as err:
                        print(
                            f"[bruriah watch] Indexing failed: {err}",
                            file=sys.stderr,
                        )
                    self.last_fingerprint = current_fingerprint

                if once:
                    break

                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\n[bruriah watch] Stopped.", file=sys.stderr)
            return 0

        return 0
