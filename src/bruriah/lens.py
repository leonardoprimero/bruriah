# Editor Lens & Inline Archaeology Engine:
# Computes bulk decision annotations for an entire file in a single fast pass.
# Designed specifically for editor integrations (VS Code CodeLens, Cursor, Neovim virtual text).
from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform import PlatformPaths


class LensError(ValueError):
    """Raised when lens computation fails."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LineLens:
    """Decision lineage annotation for a contiguous range of lines."""

    line_start: int
    line_end: int
    commit_sha: str
    author: str
    date: str
    decision_title: str | None = None
    decision_ref: str | None = None
    status: str = "unindexed"  # "active", "superseded", "deprecated", "amended", "unindexed"
    alert_relation: str | None = None
    successor_title: str | None = None
    successor_sha: str | None = None
    generations: int = 0


@dataclass(frozen=True)
class FileLensResult:
    """Complete inline archaeology annotations for a file."""

    file_path: str
    lenses: tuple[LineLens, ...]
    total_lines: int
    indexed_decisions_count: int
    stale_decisions_count: int


def _parse_blame_porcelain(blame_output: str) -> list[tuple[int, str, str, str]]:
    """Parse git blame --porcelain output into list of (line_num, sha, author, date)."""
    commit_authors: dict[str, str] = {}
    commit_dates: dict[str, str] = {}

    lines_data: list[tuple[int, str]] = []  # (line_num, sha)

    lines = blame_output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue

        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) == 40:
            sha = parts[0]
            final_line = int(parts[2])
            lines_data.append((final_line, sha))

            i += 1
            while i < len(lines):
                sub_line = lines[i]
                if sub_line.startswith("\t"):
                    i += 1
                    break
                if sub_line.startswith("author "):
                    commit_authors.setdefault(sha, sub_line[len("author "):].strip())
                elif sub_line.startswith("author-time "):
                    try:
                        epoch = int(sub_line[len("author-time "):].strip())
                        commit_dates.setdefault(
                            sha,
                            datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d"),
                        )
                    except (ValueError, OSError):
                        pass
                i += 1
        else:
            i += 1

    result: list[tuple[int, str, str, str]] = []
    for line_num, sha in lines_data:
        author = commit_authors.get(sha, "unknown")
        date_str = commit_dates.get(sha, "unknown")
        result.append((line_num, sha, author, date_str))

    return result


def _group_contiguous_lines(
    lines_info: list[tuple[int, str, str, str]],
) -> list[tuple[int, int, str, str, str]]:
    """Group contiguous lines with the same commit into (start, end, sha, author, date)."""
    if not lines_info:
        return []

    groups: list[tuple[int, int, str, str, str]] = []
    start_line, current_sha, author, date = lines_info[0]
    end_line = start_line

    for line_num, sha, auth, dt in lines_info[1:]:
        if sha == current_sha and line_num == end_line + 1:
            end_line = line_num
        else:
            groups.append((start_line, end_line, current_sha, author, date))
            start_line = line_num
            end_line = line_num
            current_sha = sha
            author = auth
            date = dt

    groups.append((start_line, end_line, current_sha, author, date))
    return groups


def compute_file_lens(
    repo: Path,
    database: sqlite3.Connection,
    relative_path: str,
) -> FileLensResult:
    """Compute inline archaeology annotations for an entire file in one fast pass."""
    try:
        res = subprocess.run(
            ["git", "blame", "--porcelain", "--", relative_path],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        blame_output = res.stdout
    except FileNotFoundError:
        raise LensError("git_not_found")
    except subprocess.CalledProcessError as err:
        stderr = (err.stderr or "").strip().lower()
        if (
            "no such path" in stderr
            or "not in the working tree" in stderr
            or "cannot stat" in stderr
            or "unknown revision" in stderr
            or "no such ref" in stderr
        ):
            raise LensError("file_not_in_git")
        raise LensError(f"git_error:{err.returncode}")

    parsed_lines = _parse_blame_porcelain(blame_output)
    if not parsed_lines:
        return FileLensResult(
            file_path=relative_path,
            lenses=(),
            total_lines=0,
            indexed_decisions_count=0,
            stale_decisions_count=0,
        )

    groups = _group_contiguous_lines(parsed_lines)
    unique_shas = {sha for _, _, sha, _, _ in groups if set(sha) != {"0"}}

    # Batch query documents table for all unique SHAs
    sha_to_doc: dict[str, tuple[str, str]] = {}  # sha -> (doc_ref, subject)
    try:
        rows = database.execute(
            "SELECT document_ref, metadata FROM documents"
        ).fetchall()
        for doc_ref, meta_json in rows:
            try:
                meta = json.loads(meta_json)
                c_sha = meta.get("commit")
                if c_sha:
                    c_sha_lower = c_sha.lower()
                    for u_sha in unique_shas:
                        if u_sha.startswith(c_sha_lower) or c_sha_lower.startswith(u_sha):
                            # Fetch subject from first passage
                            p_row = database.execute(
                                "SELECT text FROM passages WHERE document_ref = ? ORDER BY start_line LIMIT 1",
                                (doc_ref,),
                            ).fetchone()
                            subj = "(untitled)"
                            if p_row:
                                for line in p_row[0].splitlines():
                                    if line.strip().startswith("# "):
                                        subj = line.strip()[2:].strip()
                                        break
                            sha_to_doc[u_sha] = (doc_ref, subj)
            except (json.JSONDecodeError, TypeError):
                continue
    except sqlite3.DatabaseError:
        pass

    # Batch query lineage table for alerts
    doc_alerts: dict[str, tuple[str, str, str, int]] = {}  # doc_ref -> (relation, succ_title, succ_sha, gen)
    has_lineage = False
    try:
        row = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lineage'"
        ).fetchone()
        has_lineage = row is not None
    except sqlite3.DatabaseError:
        pass

    if has_lineage and sha_to_doc:
        doc_refs = [doc_ref for doc_ref, _ in sha_to_doc.values()]
        placeholders = ",".join("?" for _ in doc_refs)
        try:
            l_rows = database.execute(
                f"SELECT successor_ref, relation, predecessor_ref FROM lineage WHERE predecessor_ref IN ({placeholders})",
                doc_refs,
            ).fetchall()
            for succ_ref, relation, pred_ref in l_rows:
                # Get successor subject and sha
                s_meta_row = database.execute(
                    "SELECT metadata FROM documents WHERE document_ref = ?", (succ_ref,)
                ).fetchone()
                s_sha = ""
                if s_meta_row:
                    try:
                        s_meta = json.loads(s_meta_row[0])
                        s_sha = s_meta.get("commit", "")[:8]
                    except (json.JSONDecodeError, TypeError):
                        pass

                p_row = database.execute(
                    "SELECT text FROM passages WHERE document_ref = ? ORDER BY start_line LIMIT 1",
                    (succ_ref,),
                ).fetchone()
                s_subj = "(untitled)"
                if p_row:
                    for line in p_row[0].splitlines():
                        if line.strip().startswith("# "):
                            s_subj = line.strip()[2:].strip()
                            break

                doc_alerts[pred_ref] = (relation, s_subj, s_sha, 1)
        except sqlite3.DatabaseError:
            pass

    lenses: list[LineLens] = []
    indexed_count = 0
    stale_count = 0

    for start_line, end_line, sha, author, date in groups:
        if sha in sha_to_doc:
            doc_ref, title = sha_to_doc[sha]
            indexed_count += 1
            if doc_ref in doc_alerts:
                relation, succ_title, succ_sha, gen = doc_alerts[doc_ref]
                stale_count += 1
                status = relation  # "supersedes", "deprecates", "amends"
                lenses.append(LineLens(
                    line_start=start_line,
                    line_end=end_line,
                    commit_sha=sha[:8],
                    author=author,
                    date=date,
                    decision_title=title,
                    decision_ref=doc_ref,
                    status=status,
                    alert_relation=relation,
                    successor_title=succ_title,
                    successor_sha=succ_sha,
                    generations=gen,
                ))
            else:
                lenses.append(LineLens(
                    line_start=start_line,
                    line_end=end_line,
                    commit_sha=sha[:8],
                    author=author,
                    date=date,
                    decision_title=title,
                    decision_ref=doc_ref,
                    status="active",
                ))
        else:
            lenses.append(LineLens(
                line_start=start_line,
                line_end=end_line,
                commit_sha=sha[:8] if sha else "uncommitted",
                author=author,
                date=date,
                status="unindexed",
            ))

    total_lines = parsed_lines[-1][0] if parsed_lines else 0
    return FileLensResult(
        file_path=relative_path,
        lenses=tuple(lenses),
        total_lines=total_lines,
        indexed_decisions_count=indexed_count,
        stale_decisions_count=stale_count,
    )


def format_lens_json(result: FileLensResult) -> str:
    """Serialize FileLensResult to JSON."""
    return json.dumps(
        {
            "file_path": result.file_path,
            "total_lines": result.total_lines,
            "indexed_decisions_count": result.indexed_decisions_count,
            "stale_decisions_count": result.stale_decisions_count,
            "lenses": [asdict(lens) for lens in result.lenses],
        },
        indent=2,
    )


def format_lens_human(result: FileLensResult) -> str:
    """Format FileLensResult as human-readable terminal output."""
    lines: list[str] = [
        f"🏛️  Bruriah Inline Archaeology — {result.file_path}",
        f"   {result.total_lines} lines · {result.indexed_decisions_count} decision blocks · {result.stale_decisions_count} stale\n",
    ]
    for lens in result.lenses:
        span = f"L{lens.line_start}" if lens.line_start == lens.line_end else f"L{lens.line_start}-{lens.line_end}"
        if lens.status == "active":
            lines.append(f"  {span:12} ✅ {lens.decision_title} ({lens.commit_sha}) · {lens.author}")
        elif lens.status in ("supersedes", "deprecates", "amends"):
            rel = lens.status.upper()
            lines.append(
                f"  {span:12} ⚠️  {lens.decision_title} ({lens.commit_sha}) · {rel} by {lens.successor_title} ({lens.successor_sha})"
            )
        else:
            lines.append(f"  {span:12} ⚪ {lens.commit_sha} · {lens.author} ({lens.date})")

    return "\n".join(lines)


def run_lens(
    paths: PlatformPaths,
    repo: Path,
    relative_path: str,
) -> FileLensResult:
    """Open snapshot and compute file lens."""
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise LensError(error.code) from error

    try:
        return compute_file_lens(repo, snapshot.database, relative_path)
    finally:
        snapshot.database.close()
