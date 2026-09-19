# Architectural Blast Radius Engine (bruriah impact):
# Computes pre-flight architectural impact analysis before code changes are made.
# Evaluates governing decisions, co-governed files, lineage freshness, and risk level.
from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .why import (
    WhyError,
    check_lineage_alerts,
    find_decision_in_database,
    trace_causal_archaeology,
)

if TYPE_CHECKING:
    from .platform import PlatformPaths


class ImpactError(ValueError):
    """Raised when impact analysis fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class DecisionImpact:
    """Impact profile of a single governing architectural decision."""

    decision_ref: str
    commit_sha: str
    subject: str
    author: str
    date: str
    status: str  # "active", "superseded", "deprecated", "amended"
    direct_files: tuple[str, ...]
    blast_radius_files: tuple[str, ...]
    active_successor_title: str | None = None
    active_successor_sha: str | None = None


@dataclass(frozen=True)
class ImpactAnalysis:
    """Complete architectural blast radius analysis for a target."""

    target: str
    target_type: str  # "file", "directory", "revision"
    inspected_files: tuple[str, ...]
    decisions: tuple[DecisionImpact, ...]
    total_blast_radius_files: tuple[str, ...]
    risk_level: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    recommendations: tuple[str, ...]


def _get_git_files(repo: Path, target: str) -> tuple[str, str]:
    """Determine target type and list of files to inspect.

    Returns:
        (target_type, tuple of relative file paths)
    """
    # Check if target is a revision or revision range (e.g. HEAD~1, main..HEAD)
    is_rev = False
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--verify", target],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            is_rev = True
    except FileNotFoundError:
        raise ImpactError("git_not_found")

    if ".." in target or is_rev:
        # Revision or range: get diff files
        cmd = ["git", "diff", "--name-only", target] if ".." in target else ["git", "diff", "--name-only", f"{target}~1", target]
        try:
            diff_res = subprocess.run(
                cmd,
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            files = tuple(f.strip() for f in diff_res.stdout.splitlines() if f.strip())
            return "revision", files
        except subprocess.CalledProcessError as err:
            raise ImpactError("git_error", err.stderr or "")

    target_path = Path(target)
    full_path = repo / target_path if not target_path.is_absolute() else target_path
    rel_path = str(full_path.relative_to(repo)) if full_path.is_relative_to(repo) else str(target_path)

    if full_path.is_dir():
        # Directory: find all git-tracked files in directory
        try:
            ls_res = subprocess.run(
                ["git", "ls-files", "--", rel_path],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
            files = tuple(f.strip() for f in ls_res.stdout.splitlines() if f.strip())
            return "directory", files
        except subprocess.CalledProcessError as err:
            raise ImpactError("git_error", err.stderr or "")

    # Single file
    return "file", (rel_path,)


def analyze_impact(
    repo: Path,
    database: sqlite3.Connection,
    target: str,
) -> ImpactAnalysis:
    """Analyze the architectural blast radius of modifying a file, directory, or revision."""
    target_type, files_to_inspect = _get_git_files(repo, target)
    if not files_to_inspect:
        return ImpactAnalysis(
            target=target,
            target_type=target_type,
            inspected_files=(),
            decisions=(),
            total_blast_radius_files=(),
            risk_level="LOW",
            recommendations=("No git-tracked files found for the given target.",),
        )

    # Group inspected files by governing decision
    doc_to_direct_files: dict[str, list[str]] = {}
    doc_to_decision: dict[str, any] = {}

    for file_path in files_to_inspect:
        try:
            resolution = trace_causal_archaeology(repo, database, file_path)
            if resolution.governing_decision is not None:
                doc_ref = resolution.governing_decision.document_ref
                doc_to_direct_files.setdefault(doc_ref, []).append(file_path)
                doc_to_decision[doc_ref] = resolution.governing_decision
        except WhyError:
            continue

    decision_impacts: list[DecisionImpact] = []
    all_blast_radius_files: set[str] = set()
    inspected_set = set(files_to_inspect)

    has_superseded = False

    for doc_ref, direct_files in doc_to_direct_files.items():
        dec = doc_to_decision[doc_ref]
        alerts = check_lineage_alerts(database, dec.document_ref, dec.commit_sha)

        status = "active"
        succ_title = None
        succ_sha = None
        if alerts:
            first_alert = alerts[0]
            status = first_alert.relation  # "supersedes", "deprecates", "amends"
            succ_title = first_alert.active_successor_subject or first_alert.successor_subject
            succ_sha = first_alert.active_successor_commit or first_alert.successor_commit
            has_superseded = True

        # Blast radius: files co-governed by this decision that are NOT in the target files
        co_governed = [f for f in dec.files if f not in inspected_set]
        all_blast_radius_files.update(co_governed)

        decision_impacts.append(
            DecisionImpact(
                decision_ref=dec.document_ref,
                commit_sha=dec.commit_sha[:8],
                subject=dec.subject,
                author=dec.author,
                date=dec.date,
                status=status,
                direct_files=tuple(direct_files),
                blast_radius_files=tuple(co_governed),
                active_successor_title=succ_title,
                active_successor_sha=succ_sha[:8] if succ_sha else None,
            )
        )

    # Determine risk level
    total_blast_count = len(all_blast_radius_files)
    if has_superseded:
        risk_level = "CRITICAL"
    elif total_blast_count > 6:
        risk_level = "HIGH"
    elif total_blast_count > 0:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    # Formulate recommendations
    recs: list[str] = []
    if has_superseded:
        recs.append(
            "⚠️ Stale Architecture: The target is governed by an already SUPERSEDED or DEPRECATED decision. "
            "Aligning with the active decision is required before building new features."
        )
    if total_blast_count > 0:
        recs.append(
            f"Blast Radius Warning: Modifying this target affects {total_blast_count} co-governed file(s). "
            "Changes to the architectural contract will cause drift in these files."
        )
        recs.append(
            "Recommendation: If changing architectural contracts, declare 'Supersedes: <commit>' in your commit "
            "and update the co-governed files in the same pull request."
        )
    else:
        recs.append(
            "Isolated Change: No other files share this architectural governance. Risk of collateral drift is low."
        )

    return ImpactAnalysis(
        target=target,
        target_type=target_type,
        inspected_files=files_to_inspect,
        decisions=tuple(decision_impacts),
        total_blast_radius_files=tuple(sorted(all_blast_radius_files)),
        risk_level=risk_level,
        recommendations=tuple(recs),
    )


def format_impact_human(result: ImpactAnalysis) -> str:
    """Render human-readable terminal output for impact analysis."""
    lines: list[str] = [
        f"🏛️  Bruriah Architectural Blast Radius — {result.target} ({result.target_type})",
        f"   Risk Level: {result.risk_level} · {len(result.decisions)} governing decision(s) · {len(result.total_blast_radius_files)} co-governed file(s) at risk\n",
    ]

    if not result.decisions:
        lines.append("   ⚪ No governing architectural decisions found for this target.")
        return "\n".join(lines)

    lines.append("Governing Decisions & Blast Radius:")
    for d in result.decisions:
        if d.status == "active":
            status_badge = "✅ ACTIVE"
        else:
            rel = d.status.upper()
            succ = f" → {d.active_successor_title} ({d.active_successor_sha})" if d.active_successor_title else ""
            status_badge = f"⚠️  {rel}{succ}"

        lines.append(f"  • [{status_badge}] {d.subject} ({d.commit_sha})")
        lines.append(f"    Author: {d.author} · Date: {d.date}")
        lines.append(f"    Directly governs: {', '.join(d.direct_files)}")
        if d.blast_radius_files:
            lines.append("    Co-governed files (Blast Radius):")
            for bf in d.blast_radius_files:
                lines.append(f"      ⚠️  {bf}")
        else:
            lines.append("    Co-governed files: None (isolated)")
        lines.append("")

    lines.append("Recommendations:")
    for idx, rec in enumerate(result.recommendations, 1):
        lines.append(f"  {idx}. {rec}")

    return "\n".join(lines)


def format_impact_json(result: ImpactAnalysis) -> str:
    """Serialize impact analysis to JSON."""
    return json.dumps(asdict(result), indent=2)


def run_impact(
    paths: PlatformPaths,
    repo: Path,
    target: str,
) -> ImpactAnalysis:
    """Open snapshot and run impact analysis."""
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise ImpactError(error.code) from error

    try:
        return analyze_impact(repo, snapshot.database, target)
    finally:
        snapshot.database.close()
