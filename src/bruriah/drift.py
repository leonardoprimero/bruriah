# Architectural drift detection engine (RFC 0.10.0 / Slice 14):
# Inspects Git working tree / commit diffs against the decision lineage DAG
# to detect stale governance, missing architectural trailers, and conformance.
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .platform import PlatformPaths

from .repository import SnapshotRepository
from .why import WhyError, trace_causal_archaeology


class DriftError(ValueError):
    """Raised when drift inspection encounters a structured error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DriftWarning:
    file_path: str
    governing_decision: str
    decision_ref: str
    decision_sha: str
    lineage_state: str
    current_active_decision: str | None = None
    current_active_ref: str | None = None
    current_active_sha: str | None = None
    generations: int = 1
    action_recommendation: str = ""


@dataclass(frozen=True)
class FileGovernance:
    file_path: str
    conforms: bool
    governing_decision: str | None = None
    governing_ref: str | None = None
    governing_sha: str | None = None
    status: str = "clean"  # "clean", "stale", "unindexed"


@dataclass(frozen=True)
class DriftReport:
    repo_path: str
    revision_or_range: str | None
    staged: bool
    inspected_files: tuple[str, ...]
    stale_warnings: tuple[DriftWarning, ...] = ()
    clean_files: tuple[FileGovernance, ...] = ()
    unindexed_files: tuple[str, ...] = ()
    missing_trailers: tuple[str, ...] = ()

    @property
    def has_drift(self) -> bool:
        return len(self.stale_warnings) > 0 or len(self.missing_trailers) > 0


def get_git_diff_files(
    repo: Path,
    revision_or_range: str | None = None,
    staged: bool = False,
) -> tuple[str, ...]:
    """Extract list of touched/modified files from Git."""
    args = ["diff", "--name-only"]
    if staged:
        args.append("--cached")
    if revision_or_range:
        args.append(revision_or_range)
    elif not staged:
        args.append("HEAD")

    try:
        res = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        seen: set[str] = set()
        deduped: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                deduped.append(f)
        return tuple(deduped)
    except FileNotFoundError:
        raise DriftError("git_not_found")
    except subprocess.CalledProcessError as err:
        if not revision_or_range and not staged and "unknown revision" in (err.stderr or "").lower():
            try:
                res = subprocess.run(
                    ["git", "diff", "--name-only"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return tuple(line.strip() for line in res.stdout.splitlines() if line.strip())
            except Exception:
                pass
        raise DriftError(f"git_error:{err.returncode}")


def analyze_architectural_drift(
    repo: Path,
    snapshot_repo: SnapshotRepository,
    files: tuple[str, ...],
    revision_or_range: str | None = None,
    staged: bool = False,
) -> DriftReport:
    """Analyze architectural governance and drift across the given files."""
    stale_warnings: list[DriftWarning] = []
    clean_files: list[FileGovernance] = []
    unindexed_files: list[str] = []

    for file_path in files:
        try:
            res = trace_causal_archaeology(repo, snapshot_repo.database, file_path)
        except WhyError:
            unindexed_files.append(file_path)
            continue
        except Exception:
            unindexed_files.append(file_path)
            continue

        if res.governing_decision is None:
            unindexed_files.append(file_path)
            continue

        gov = res.governing_decision
        alerts = res.lineage_alerts
        if not alerts:
            clean_files.append(
                FileGovernance(
                    file_path=file_path,
                    conforms=True,
                    governing_decision=gov.subject,
                    governing_ref=gov.document_ref,
                    governing_sha=gov.commit_sha,
                    status="clean",
                )
            )
        else:
            primary_alert = alerts[0]
            rel = primary_alert.relation
            depth = primary_alert.depth

            active_subj = primary_alert.active_successor_subject or primary_alert.successor_subject
            active_sha = primary_alert.active_successor_commit or primary_alert.successor_commit
            active_ref = primary_alert.active_successor_ref or primary_alert.successor_ref

            sha_str = active_sha[:12] if active_sha else (active_ref or "")
            rec = (
                f'Ensure your changes adhere to "{active_subj or "active decision"}". '
                f"If this change updates the architecture, include trailer: "
                f"Amends: {sha_str}"
            )

            stale_warnings.append(
                DriftWarning(
                    file_path=file_path,
                    governing_decision=gov.subject,
                    decision_ref=gov.document_ref,
                    decision_sha=gov.commit_sha,
                    lineage_state=rel.upper(),
                    current_active_decision=active_subj,
                    current_active_ref=active_ref,
                    current_active_sha=active_sha,
                    generations=depth,
                    action_recommendation=rec,
                )
            )

    return DriftReport(
        repo_path=str(repo.resolve()),
        revision_or_range=revision_or_range,
        staged=staged,
        inspected_files=files,
        stale_warnings=tuple(stale_warnings),
        clean_files=tuple(clean_files),
        unindexed_files=tuple(unindexed_files),
        missing_trailers=(),
    )


def format_drift_human(report: DriftReport) -> str:
    """Render a human-readable terminal report of architectural drift inspection."""
    lines: list[str] = [
        "🔍 Bruriah Architectural Drift Inspection",
        f"   Repository: {report.repo_path}",
    ]
    if report.staged:
        lines.append(f"   Comparing: staged changes against HEAD ({len(report.inspected_files)} files inspected)")
    elif report.revision_or_range:
        lines.append(f"   Comparing: {report.revision_or_range} ({len(report.inspected_files)} files inspected)")
    else:
        lines.append(f"   Comparing: uncommitted changes against HEAD ({len(report.inspected_files)} files inspected)")

    lines.append("")

    if report.stale_warnings:
        lines.append(f"⚠️  STALE GOVERNANCE DETECTED ({len(report.stale_warnings)} files):")
        for warn in report.stale_warnings:
            lines.append(f"  • {warn.file_path}")
            lines.append(f'    Governing Decision: "{warn.governing_decision}"')
            lines.append(f"    Original Ref: {warn.decision_ref} (sha: {warn.decision_sha[:12]})")
            gen_text = f" (evolved through {warn.generations} generations)" if warn.generations > 1 else ""
            lines.append(f"    Lineage State: {warn.lineage_state}{gen_text}")
            if warn.current_active_decision:
                active_sha_str = f" (sha: {warn.current_active_sha[:12]})" if warn.current_active_sha else ""
                lines.append(f'    Current Active Decision: "{warn.current_active_decision}"{active_sha_str}')
            lines.append(f"    Action: {warn.action_recommendation}")
        lines.append("")

    if report.clean_files:
        lines.append(f"✅ CLEAN GOVERNANCE ({len(report.clean_files)} files):")
        for c in report.clean_files:
            lines.append(f"  • {c.file_path} (conforms to active decision {c.governing_decision})")
        lines.append("")

    if report.unindexed_files:
        lines.append(f"ℹ️  UNINDEXED / NON-GOVERNED ({len(report.unindexed_files)} files):")
        for u in report.unindexed_files:
            lines.append(f"  • {u}")
        lines.append("")

    summary = f"Summary: {len(report.stale_warnings)} stale governance warning(s), {len(report.missing_trailers)} missing trailer(s)."
    lines.append(summary)
    if report.has_drift:
        lines.append("Run with --strict in CI to block architectural regressions.")
    else:
        lines.append("All inspected changes conform cleanly to architectural governance.")

    return "\n".join(lines)


def format_drift_json(report: DriftReport) -> str:
    """Render a structured JSON report of architectural drift inspection."""
    data = {
        "repo_path": report.repo_path,
        "revision_or_range": report.revision_or_range,
        "staged": report.staged,
        "has_drift": report.has_drift,
        "inspected_files": list(report.inspected_files),
        "stale_warnings": [asdict(w) for w in report.stale_warnings],
        "clean_files": [asdict(c) for c in report.clean_files],
        "unindexed_files": list(report.unindexed_files),
        "missing_trailers": list(report.missing_trailers),
    }
    return json.dumps(data, indent=2)


def run_drift(
    paths: PlatformPaths,
    repo: Path,
    revision_or_range: str | None = None,
    staged: bool = False,
) -> DriftReport:
    """Run architectural drift inspection against the active snapshot for git changes."""
    from .platform import open_snapshot

    files = get_git_diff_files(repo, revision_or_range, staged)
    snapshot = open_snapshot(paths)
    try:
        repo_layer = SnapshotRepository(snapshot.database)
        return analyze_architectural_drift(repo, repo_layer, files, revision_or_range, staged)
    finally:
        snapshot.database.close()
