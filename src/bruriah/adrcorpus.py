"""Copy a repository's committed decision documents (ADRs) into the corpus `init --repo` indexes.

`gitcorpus` turns commit messages into documents, but many projects keep their weightiest
reasoning in ADR files instead, and a corpus built from commits alone never sees them. This reads
the common ADR folders AT A REVISION, through git's object store, so what gets indexed is exactly
what was committed: an uncommitted edit or an untracked draft in the working tree is not a decision
anyone has agreed to, and it never reaches the index.

The folders, relative to the repository root:

    docs/adr  docs/adrs  docs/decisions  docs/architecture/decisions  doc/adr  adr  adrs  decisions

Only `*.md` blobs are read; symlinks and submodules are skipped. Each document is written byte for
byte under `<out>/adr/<its path in the repository>`, a subfolder `gitcorpus` never writes (its
documents are flat files at the top of the corpus), so the two can never collide. That subfolder is
this module's alone: every run builds it afresh in a sibling staging folder and then replaces the
old one, so re-running neither duplicates a decision nor keeps one the repository deleted. Nothing
is pruned file by file: on a case-insensitive filesystem a case-only rename would make a pruner
delete the file it had just written.

Nothing is written to the repository being read.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .corpus import _frontmatter
from .gitcorpus import _require_commit

ADR_FOLDERS = (
    "docs/adr",
    "docs/adrs",
    "docs/decisions",
    "docs/architecture/decisions",
    "doc/adr",
    "adr",
    "adrs",
    "decisions",
)
SUBDIR = "adr"


@dataclass(frozen=True)
class AdrResult:
    written: int
    # The folders that yielded at least one document, in `ADR_FOLDERS` order.
    folders: tuple[str, ...]
    # Committed `*.md` files the index could not parse (not UTF-8, or broken frontmatter). Left out
    # rather than written: one such file would otherwise fail the whole index build.
    skipped: int
    # Committed `*.md` paths with a `.`, `..` or empty component. Git's plumbing accepts a tree
    # entry named `..`, so a crafted repository could otherwise make the copy climb out of the
    # corpus; such an entry is never written.
    unsafe: int = 0


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)
    except FileNotFoundError:
        raise SystemExit("error: git is not on PATH")
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", "replace").strip()
        raise SystemExit(f"error: git failed: {detail or error}")
    return result.stdout


def _parses(raw: bytes) -> bool:
    """The checks `corpus.parse_document` would fail on, made before the file is written."""
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        _frontmatter(text.splitlines(keepends=True))
    except (UnicodeDecodeError, ValueError, yaml.YAMLError):
        return False
    return True


def build(repo: Path, out: Path, *, revision: str = "HEAD") -> AdrResult:
    """Write every committed `*.md` under `ADR_FOLDERS` at `revision` into `out / SUBDIR`."""
    _require_commit(repo, revision)
    listing = _git_bytes(repo, "ls-tree", "-r", "-z", revision, "--", *ADR_FOLDERS)
    target_root = out / SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SUBDIR}-", dir=out))
    try:
        written, folders, skipped, unsafe = _write_tree(repo, listing, staging)
        if target_root.is_symlink():
            target_root.unlink()
        elif target_root.is_dir():
            shutil.rmtree(target_root)
        if written:
            staging.rename(target_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    ordered = tuple(name for name in ADR_FOLDERS if name in folders)
    return AdrResult(written=written, folders=ordered, skipped=skipped, unsafe=unsafe)


def _write_tree(repo: Path, listing: bytes, root: Path) -> tuple[int, list[str], int, int]:
    """Write every safe, parseable `*.md` blob in `listing` under `root`; return the counts."""
    resolved_root = root.resolve()
    written = 0
    folders: list[str] = []
    skipped = 0
    unsafe = 0
    for entry in listing.decode("utf-8", "surrogateescape").split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, kind, oid = meta.split()
        if kind != "blob" or mode not in ("100644", "100755") or not path.endswith(".md"):
            continue
        parts = path.split("/")
        target = root.joinpath(*parts)
        if any(part in ("", ".", "..") for part in parts) or not target.resolve().is_relative_to(resolved_root):
            unsafe += 1
            continue
        raw = _git_bytes(repo, "cat-file", "blob", oid)
        if not _parses(raw):
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        written += 1
        folder = next(name for name in ADR_FOLDERS if path.startswith(f"{name}/"))
        if folder not in folders:
            folders.append(folder)
    return written, folders, skipped, unsafe
