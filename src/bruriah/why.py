# Causal archaeology engine (Slice 13A): resolves why a file or line of code was written
# by linking Git history to the precomputed decision index and lineage DAG.
# Deterministic, local, read-only, and generative-model-free.
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform import PlatformPaths


class WhyError(ValueError):
    """Raised when causal resolution fails for a structured, typed reason."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str
    date: str
    subject: str


@dataclass(frozen=True)
class DecisionInfo:
    document_ref: str
    commit_sha: str
    subject: str
    author: str
    date: str
    body: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class LineageHop:
    relation: str
    ref: str
    commit: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class LineageAlert:
    relation: str
    successor_ref: str
    successor_commit: str | None = None
    successor_subject: str | None = None
    depth: int = 1
    chain: tuple[LineageHop, ...] = ()
    active_successor_ref: str | None = None
    active_successor_commit: str | None = None
    active_successor_subject: str | None = None


@dataclass(frozen=True)
class CausalResolution:
    target: str
    file_path: str
    line: int | None
    line_commit: CommitInfo
    governing_decision: DecisionInfo | None = None
    governing_commit: CommitInfo | None = None
    lineage_alerts: tuple[LineageAlert, ...] = ()


def parse_target(target: str) -> tuple[str, int | None]:
    """Parse a target string like 'path/to/file.py:158' or 'path/to/file.py'.

    Protects Windows drive letters (e.g. 'C:\\file.py:10') by matching the colon
    and digit sequence only at the end of the string.
    """
    cleaned = target.strip()
    if not cleaned:
        raise WhyError("empty_target")
    match = re.search(r":(\d+)$", cleaned)
    if match:
        line_num = int(match.group(1))
        if line_num <= 0:
            raise WhyError("invalid_line_number")
        return cleaned[: match.start()], line_num
    return cleaned, None


def _run_git(repo: Path, *args: str) -> str:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout
    except FileNotFoundError:
        raise WhyError("git_not_found")
    except subprocess.CalledProcessError as err:
        stderr = (err.stderr or "").strip().lower()
        if "no such path" in stderr or "not in the working tree" in stderr or "unknown revision" in stderr:
            raise WhyError("file_not_in_git")
        if ("has only" in stderr and "lines" in stderr) or "fatal: file" in stderr and "has only" in stderr:
            raise WhyError("line_out_of_range")
        raise WhyError(f"git_error:{err.returncode}")


def resolve_commit_for_target(repo: Path, relative_path: str, line: int | None = None) -> CommitInfo:
    """Find the commit that last touched the given line or file."""
    if line is not None:
        raw_blame = _run_git(repo, "blame", "-L", f"{line},{line}", "--porcelain", "--", relative_path)
        lines = raw_blame.splitlines()
        if not lines:
            raise WhyError("file_not_in_git")
        sha = lines[0].split()[0]
        if set(sha) == {"0"}:
            raise WhyError("uncommitted_line")

        author = "unknown"
        subject = "unknown"
        date_str = "unknown"
        for line_str in lines[1:]:
            if line_str.startswith("author "):
                author = line_str[len("author ") :].strip()
            elif line_str.startswith("summary "):
                subject = line_str[len("summary ") :].strip()
            elif line_str.startswith("author-time "):
                try:
                    epoch = int(line_str[len("author-time ") :].strip())
                    date_str = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, OSError):
                    pass
        return CommitInfo(sha=sha, author=author, date=date_str, subject=subject)

    # Whole file target: get most recent commit touching file
    output = _run_git(repo, "log", "-n", "1", "--format=%H%x00%s%x00%an%x00%aI", "--", relative_path).strip()
    if not output:
        raise WhyError("file_not_in_git")
    parts = output.split("\x00")
    if len(parts) < 4:
        raise WhyError("file_not_in_git")
    sha, subject, author, raw_date = parts[0], parts[1], parts[2], parts[3]
    date_str = raw_date[:10] if len(raw_date) >= 10 else raw_date
    return CommitInfo(sha=sha, author=author, date=date_str, subject=subject)


def find_decision_in_database(database: sqlite3.Connection, commit_sha: str) -> DecisionInfo | None:
    """Find a decision document in SQLite that was built from the given commit SHA."""
    prefix_8 = commit_sha[:8].lower()
    full_sha = commit_sha.lower()

    # Search documents metadata
    rows = database.execute(
        "SELECT document_ref, relative_path, metadata FROM documents WHERE metadata LIKE ?",
        (f'%"{prefix_8}%',),
    ).fetchall()

    matched_ref: str | None = None
    matched_meta: dict[str, object] = {}
    for doc_ref, _rel_path, meta_raw in rows:
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            continue
        c = str(meta.get("commit", "")).lower()
        if c == full_sha or (len(prefix_8) >= 7 and c.startswith(prefix_8)):
            matched_ref = doc_ref
            matched_meta = meta
            break

    if matched_ref is None:
        return None

    # Fetch passages to reconstruct the subject, body, and touched files
    passages = database.execute(
        "SELECT text FROM passages WHERE document_ref = ? ORDER BY start_line",
        (matched_ref,),
    ).fetchall()
    full_text = "\n\n".join(row[0] for row in passages)

    # Extract subject (first heading)
    subject = "Decision"
    subject_match = re.search(r"^#\s+(.+)$", full_text, re.MULTILINE)
    if subject_match:
        subject = subject_match.group(1).strip()

    # Extract author and date
    date_str = str(matched_meta.get("verification_date", "unknown"))
    author = "unknown"
    meta_match = re.search(
        r"\*\*Decided:\*\*\s*([^\s·]+)\s*·\s*\*\*Commit:\*\*\s*`([^`]+)`\s*·\s*\*\*Author:\*\*\s*(.+)$",
        full_text,
        re.MULTILINE,
    )
    if meta_match:
        date_str = meta_match.group(1).strip()
        author = meta_match.group(3).strip()

    # Extract files touched
    files: list[str] = []
    files_section = re.search(r"## Files this decision touched\n((?:- `[^`]+`\n?)+)", full_text)
    if files_section:
        files = re.findall(r"- `([^`]+)`", files_section.group(1))

    # Extract reasoning body (text between author header and files section)
    body = full_text
    if meta_match and files_section:
        body = full_text[meta_match.end() : files_section.start()].strip()
    elif meta_match:
        body = full_text[meta_match.end() :].strip()
    elif files_section:
        body = full_text[: files_section.start()].strip()

    return DecisionInfo(
        document_ref=matched_ref,
        commit_sha=full_sha,
        subject=subject,
        author=author,
        date=date_str,
        body=body,
        files=tuple(files),
    )


def _doc_info(database: sqlite3.Connection, doc_ref: str) -> tuple[str | None, str | None]:
    succ_doc = database.execute("SELECT metadata FROM documents WHERE document_ref = ?", (doc_ref,)).fetchone()
    succ_sha: str | None = None
    succ_subj: str | None = None
    if succ_doc:
        try:
            smeta = json.loads(succ_doc[0])
            succ_sha = smeta.get("commit")
        except json.JSONDecodeError:
            pass
        first_passage = database.execute(
            "SELECT text FROM passages WHERE document_ref = ? ORDER BY start_line LIMIT 1",
            (doc_ref,),
        ).fetchone()
        if first_passage:
            s_match = re.search(r"^#\s+(.+)$", first_passage[0], re.MULTILINE)
            if s_match:
                succ_subj = s_match.group(1).strip()
    return succ_sha, succ_subj


def check_lineage_alerts(database: sqlite3.Connection, decision_ref: str, commit_sha: str) -> tuple[LineageAlert, ...]:
    """Check if the given decision has been superseded, deprecated, or amended in the lineage DAG,
    tracing multi-hop successor chains transitively to find the active leaf decision.
    """
    has_lineage = (
        database.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='lineage'").fetchone()[0] > 0
    )
    if not has_lineage:
        return ()

    prefix_8 = commit_sha[:8].lower()
    rows = database.execute(
        "SELECT successor_ref, relation, predecessor_target FROM lineage WHERE predecessor_ref = ? OR predecessor_target LIKE ?",
        (decision_ref, f"{prefix_8}%"),
    ).fetchall()

    alerts: list[LineageAlert] = []
    for successor_ref, relation, _target in rows:
        succ_sha, succ_subj = _doc_info(database, successor_ref)

        chain: list[LineageHop] = [LineageHop(relation=relation, ref=successor_ref, commit=succ_sha, subject=succ_subj)]
        visited = {decision_ref, successor_ref}
        current_ref = successor_ref
        current_sha = succ_sha
        current_subj = succ_subj

        for _ in range(50):
            p8 = f"{current_sha[:8].lower()}%" if current_sha else "---none---"
            next_rows = database.execute(
                "SELECT successor_ref, relation FROM lineage WHERE predecessor_ref = ? OR (predecessor_target != '' AND predecessor_target LIKE ?)",
                (current_ref, p8),
            ).fetchall()
            if not next_rows:
                break
            next_succ_ref, next_rel = next_rows[0]
            if next_succ_ref in visited:
                break
            visited.add(next_succ_ref)
            n_sha, n_subj = _doc_info(database, next_succ_ref)
            chain.append(LineageHop(relation=next_rel, ref=next_succ_ref, commit=n_sha, subject=n_subj))
            current_ref = next_succ_ref
            current_sha = n_sha
            current_subj = n_subj

        depth = len(chain)
        active_ref = current_ref if depth > 1 else None
        active_sha = current_sha if depth > 1 else None
        active_subj = current_subj if depth > 1 else None

        alerts.append(
            LineageAlert(
                relation=relation,
                successor_ref=successor_ref,
                successor_commit=succ_sha,
                successor_subject=succ_subj,
                depth=depth,
                chain=tuple(chain),
                active_successor_ref=active_ref,
                active_successor_commit=active_sha,
                active_successor_subject=active_subj,
            )
        )
    return tuple(alerts)


def trace_causal_archaeology(
    repo: Path,
    database: sqlite3.Connection,
    target: str,
) -> CausalResolution:
    """Trace why a file or line exists by combining git history with index decisions."""
    file_path, line = parse_target(target)
    line_commit = resolve_commit_for_target(repo, file_path, line)

    # 1. Check if the line's own commit is indexed
    decision = find_decision_in_database(database, line_commit.sha)
    governing_commit = line_commit if decision is not None else None

    # 2. If not, trace back through the git history of this line or file to find the governing decision
    if decision is None:
        commits_to_check: list[CommitInfo] = []
        if line is not None:
            try:
                log_output = _run_git(
                    repo, "log", "-L", f"{line},{line}:{file_path}", "--format=%H%x00%s%x00%an%x00%aI"
                )
                for line_text in log_output.splitlines():
                    if "\x00" in line_text:
                        parts = line_text.strip().split("\x00")
                        if len(parts) >= 4:
                            commits_to_check.append(
                                CommitInfo(sha=parts[0], subject=parts[1], author=parts[2], date=parts[3][:10])
                            )
            except WhyError:
                pass

        try:
            file_log = _run_git(repo, "log", "--format=%H%x00%s%x00%an%x00%aI", "--", file_path)
            for line_text in file_log.splitlines():
                if "\x00" in line_text:
                    parts = line_text.strip().split("\x00")
                    if len(parts) >= 4:
                        info = CommitInfo(sha=parts[0], subject=parts[1], author=parts[2], date=parts[3][:10])
                        if not any(c.sha == info.sha for c in commits_to_check):
                            commits_to_check.append(info)
        except WhyError:
            pass

        for c_info in commits_to_check:
            cand_decision = find_decision_in_database(database, c_info.sha)
            if cand_decision is not None:
                decision = cand_decision
                governing_commit = c_info
                break

    # 3. Check lineage alerts if a decision was located
    alerts: tuple[LineageAlert, ...] = ()
    if decision is not None:
        alerts = check_lineage_alerts(database, decision.document_ref, decision.commit_sha)

    return CausalResolution(
        target=target,
        file_path=file_path,
        line=line,
        line_commit=line_commit,
        governing_decision=decision,
        governing_commit=governing_commit,
        lineage_alerts=alerts,
    )


def format_why_json(res: CausalResolution) -> str:
    payload = {
        "target": res.target,
        "file_path": res.file_path,
        "line": res.line,
        "line_commit": {
            "sha": res.line_commit.sha,
            "author": res.line_commit.author,
            "date": res.line_commit.date,
            "subject": res.line_commit.subject,
        },
        "governing_commit": {
            "sha": res.governing_commit.sha,
            "author": res.governing_commit.author,
            "date": res.governing_commit.date,
            "subject": res.governing_commit.subject,
        }
        if res.governing_commit is not None
        else None,
        "governing_decision": {
            "document_ref": res.governing_decision.document_ref,
            "commit_sha": res.governing_decision.commit_sha,
            "subject": res.governing_decision.subject,
            "author": res.governing_decision.author,
            "date": res.governing_decision.date,
            "body": res.governing_decision.body,
            "files": list(res.governing_decision.files),
        }
        if res.governing_decision is not None
        else None,
        "lineage_alerts": [
            {
                "relation": a.relation,
                "successor_ref": a.successor_ref,
                "successor_commit": a.successor_commit,
                "successor_subject": a.successor_subject,
                "depth": a.depth,
                "active_successor_ref": a.active_successor_ref,
                "active_successor_commit": a.active_successor_commit,
                "active_successor_subject": a.active_successor_subject,
                "chain": [
                    {
                        "relation": h.relation,
                        "ref": h.ref,
                        "commit": h.commit,
                        "subject": h.subject,
                    }
                    for h in a.chain
                ],
            }
            for a in res.lineage_alerts
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def format_why_human(res: CausalResolution) -> str:
    lines: list[str] = [
        f"Target: {res.target}",
        "",
        "Line Commit:",
        f"  Commit:  {res.line_commit.sha[:12]} ({res.line_commit.date})",
        f"  Author:  {res.line_commit.author}",
        f"  Subject: {res.line_commit.subject}",
        "",
    ]

    if res.governing_decision is not None:
        lines.append("Governing Architectural Decision:")
        lines.append(f"  Decision: {res.governing_decision.subject}")
        lines.append(f"  Doc Ref:  {res.governing_decision.document_ref}")
        if res.governing_commit is not None and res.governing_commit.sha != res.line_commit.sha:
            lines.append(f"  Commit:   {res.governing_commit.sha[:12]} ({res.governing_commit.date})")
        lines.append(f"  Decided:  {res.governing_decision.date} by {res.governing_decision.author}")
        if res.governing_decision.files:
            lines.append(f"  Files:    {', '.join(res.governing_decision.files)}")
        lines.append("")
        lines.append("  Why this was written:")
        for body_line in res.governing_decision.body.splitlines():
            lines.append(f"    {body_line}")
        lines.append("")
    else:
        lines.append("Governing Architectural Decision:")
        lines.append("  No governing architectural decision indexed in Bruriah.")
        lines.append("  (Run `bruriah init --repo .` or `bruriah index` to index architectural decisions)")
        lines.append("")

    if res.lineage_alerts:
        lines.append("Lineage Alerts:")
        for alert in res.lineage_alerts:
            rel_upper = alert.relation.upper()
            succ_str = alert.successor_ref
            if alert.successor_commit:
                succ_str += f" (sha: {alert.successor_commit[:12]})"
            lines.append(f"  ⚠️  {rel_upper} by {succ_str}")
            if alert.successor_subject:
                lines.append(f'     "{alert.successor_subject}"')
            if alert.depth > 1 and alert.active_successor_ref:
                act_str = alert.active_successor_ref
                if alert.active_successor_commit:
                    act_str += f" (sha: {alert.active_successor_commit[:12]})"
                lines.append(
                    f"     ↳ subsequently evolved through {alert.depth} generations to [CURRENT ACTIVE]: {act_str}"
                )
                if alert.active_successor_subject:
                    lines.append(f'       "{alert.active_successor_subject}"')
        lines.append("")

    return "\n".join(lines).rstrip()


def run_why(
    paths: PlatformPaths,
    repo: Path,
    target: str,
) -> CausalResolution:
    """Run causal archaeology against the active snapshot for a git target."""
    from .platform import open_snapshot

    snapshot = open_snapshot(paths)
    try:
        return trace_causal_archaeology(repo, snapshot.database, target)
    finally:
        snapshot.database.close()
