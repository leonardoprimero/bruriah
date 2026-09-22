# Architectural Guard & Compliance Receipt Engine (bruriah guard):
# Acts as the authoritative gatekeeper for human developers and AI agents.
# Audits code targets or diffs against the lineage DAG, injects active architectural
# contracts into agent prompts, generates deterministic compliance receipts (RDD),
# and optionally bridges to Engram only when explicitly requested.
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import agent_surface
from .drift import analyze_architectural_drift, get_git_diff_files
from .impact import analyze_impact
from .repository import SnapshotRepository

if TYPE_CHECKING:
    from .platform import PlatformPaths


class GuardError(ValueError):
    """Raised when architectural guard evaluation fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class ArchitecturalContract:
    """An active architectural rule governing one or more files."""

    decision_ref: str
    decision_title: str
    decision_sha: str
    governed_files: tuple[str, ...]
    directives: tuple[str, ...]


@dataclass(frozen=True)
class GuardViolation:
    """A violation of architectural governance."""

    file_path: str
    # "VETO" (blocking) or "WARNING" (advisory). `evaluate_guard` writes only those two, but
    # this is an untyped `str` and a comment is not enforcement, so the agent rendering maps it
    # through `agent_surface.KNOWN_SEVERITIES`.
    severity: str
    decision_title: str
    decision_sha: str
    message: str
    # The lineage relation, upper-cased: SUPERSEDES, DEPRECATES or AMENDS. `message` states
    # the same fact inside a sentence that also quotes the successor's subject, so the
    # agent-facing rendering needs it as its own value to say WHY a file is flagged without
    # quoting anything a decision author wrote. The vocabulary is closed in `agent_surface`,
    # which is where the agent rendering maps it through `KNOWN_LINEAGE_STATES`.
    #
    # Required, with no default, and it is the second time this field's default has been the
    # bug. It was `= ""`, which is not in the closed vocabulary -- so a construction site that
    # simply forgot to set it rendered `UNKNOWN`, collected `DEGRADED_NOTICE`, and announced
    # that a value "could not be validated", indistinguishable from a poisoned one. A forgotten
    # assignment is now a `TypeError` at construction instead of a fabricated security warning.
    lineage_state: str
    active_successor_title: str | None = None
    active_successor_sha: str | None = None


@dataclass(frozen=True)
class ComplianceReceipt:
    """A deterministic, verifiable receipt of architectural compliance (RDD)."""

    receipt_version: str
    timestamp: str
    target: str
    status: str  # "COMPLIANT" or "NON_COMPLIANT"
    inspected_count: int
    inspected_files: tuple[str, ...]
    governing_decision_refs: tuple[str, ...]
    violation_count: int
    digest: str  # SHA-256 canonical hash of the compliance payload


@dataclass(frozen=True)
class GuardResult:
    """Result of the architectural guard evaluation."""

    target: str
    status: str  # "PASSED", "WARNING", "VETOED"
    inspected_files: tuple[str, ...]
    contracts: tuple[ArchitecturalContract, ...]
    violations: tuple[GuardViolation, ...]
    agent_context: str
    # Whether `agent_context` carries a placeholder in place of a rejected identifier. Carried
    # rather than printed: `_generate_agent_context` runs on every guard evaluation whatever
    # output mode was asked for, so warning from inside it put an `--agent` message on the
    # stderr of plain and `--json` runs. `cli.py` prints `agent_surface.degradation_warning`
    # only when `--agent` was actually selected.
    #
    # Unlike `GuardViolation.lineage_state` above, defaulting is safe here and so it defaults:
    # a forgotten assignment suppresses a warning rather than fabricating one.
    agent_rendering_degraded: bool = False
    receipt: ComplianceReceipt | None = None
    engram_synced: bool = False


def _compute_receipt_digest(
    target: str,
    status: str,
    files: Sequence[str],
    decisions: Sequence[str],
    violations_count: int,
) -> str:
    """Compute deterministic SHA-256 digest for compliance receipt."""
    payload = {
        "target": target,
        "status": status,
        "files": sorted(files),
        "decisions": sorted(decisions),
        "violations_count": violations_count,
    }
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _generate_agent_context(
    contracts: Sequence[ArchitecturalContract],
    violations: Sequence[GuardViolation],
) -> tuple[str, bool]:
    """Render the agent-facing governance summary, naming decisions by reference only.

    Everything in this string reads as instruction to whatever consumes it, so it carries no
    text a decision author wrote: no subject, no directive prose. A decision subject placed
    here is an instruction written by whoever authored that decision rather than by the
    operator running the command, and `directives` embeds the subject while `message` quotes
    the successor's.

    What remains is a literal authored in this repository, a closed-vocabulary value
    (severity, lineage relation), or an identifier this repository format-validates (a commit
    sha; a path checked for printability and code-span safety, which is all `printable_path`
    claims). The last two are enforced by `agent_surface`, not assumed: every sha, path,
    severity and lineage state below is routed through it, so a value outside the vocabulary or
    the format renders as its placeholder rather than being interpolated raw. The text is not
    unreachable -- `bruriah why` and `git show` both return it -- it is simply not
    pre-injected. `format_guard_human` is unchanged and still prints it: a person reading a
    terminal is not an instruction-following agent.

    The route this prints is a shape (`bruriah why <file>`, `git show <sha>`) rather than a
    filled-in command, and that is now the pattern all three renderers follow: `heal` briefly
    filled its route line in and turned a git-derived path into a literal command an agent was
    told to run. An agent has the path and the sha from the structured lines above and can
    substitute them itself; nothing here needs to pre-build a command string around a value
    that only `printable_path` has seen.

    Returns the rendering and whether it was degraded. `annotate_degradation` says that a
    substitution happened in the rendering itself, because the raw value survives in the human
    and JSON renderings and an operator who cannot see the disagreement cannot act on it; the
    boolean is how the operator-facing half of that reaches `cli.py` without this function
    writing to stderr behind a `--json` run's back.

    Pinned by `tests/test_agent_prompt_boundary.py`.
    """
    lines: list[str] = [
        "### Bruriah Architectural Guard — governance summary",
        "The decisions below govern the files in this task. They are named by reference, not",
        "quoted: run `bruriah why <file>` or `git show <sha>` to read one before changing the",
        "files it governs.",
        "",
    ]

    if contracts:
        for c in contracts:
            files_str = ", ".join(f"`{agent_surface.printable_path(f)}`" for f in c.governed_files)
            lines.append(f"#### Decision `{agent_surface.commit_sha(c.decision_sha)}`")
            lines.append(f"- Governs: {files_str}")
            lines.append("")
    else:
        lines.append("No prior architectural decision governs these files directly.")
        lines.append("")

    if violations:
        lines.append(f"### Governance warnings ({len(violations)})")
        for v in violations:
            succ = (
                f", active successor `{agent_surface.commit_sha(v.active_successor_sha)}`"
                if v.active_successor_sha
                else ""
            )
            state = agent_surface.closed(v.lineage_state, agent_surface.KNOWN_LINEAGE_STATES)
            severity = agent_surface.closed(v.severity, agent_surface.KNOWN_SEVERITIES)
            lines.append(
                f"- [{severity}] `{agent_surface.printable_path(v.file_path)}` — governing decision "
                f"`{agent_surface.commit_sha(v.decision_sha)}` has lineage state {state}{succ}"
            )
        lines.append("")

    return agent_surface.annotate_degradation("\n".join(lines).strip())


def evaluate_guard(
    repo: Path,
    database: sqlite3.Connection,
    target: str,
    *,
    strict: bool = False,
    generate_receipt: bool = False,
    engram: bool = False,
) -> GuardResult:
    """Evaluate architectural guard on a target file, directory, or revision."""
    snapshot_repo = SnapshotRepository(database)

    # 1. Determine files to inspect
    # Check if target is revision range or file/dir
    is_rev = ".." in target or target.startswith("HEAD")
    if is_rev:
        diff_files = get_git_diff_files(repo, revision_or_range=target)
        inspected_files = diff_files
    else:
        target_path = repo / target if not Path(target).is_absolute() else Path(target)
        if target_path.is_dir():
            try:
                rel = str(target_path.relative_to(repo))
                res = subprocess.run(
                    ["git", "ls-files", "--", rel],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                inspected_files = tuple(f.strip() for f in res.stdout.splitlines() if f.strip())
            except subprocess.CalledProcessError as err:
                raise GuardError("git_error", err.stderr or "")
        else:
            rel = str(target_path.relative_to(repo)) if target_path.is_relative_to(repo) else target
            inspected_files = (rel,)

    # 2. Run drift and impact analysis
    drift_report = analyze_architectural_drift(repo, snapshot_repo, inspected_files)
    impact_result = analyze_impact(repo, database, target)

    # 3. Extract active contracts
    contracts_map: dict[str, ArchitecturalContract] = {}
    for d in impact_result.decisions:
        if d.status == "active":
            directives: list[str] = [
                f"Maintain alignment with decision '{d.subject}' ({d.commit_sha}).",
            ]
            if d.blast_radius_files:
                directives.append(
                    f"Co-governs {len(d.blast_radius_files)} other file(s): {', '.join(d.blast_radius_files[:3])}."
                )
            contracts_map[d.decision_ref] = ArchitecturalContract(
                decision_ref=d.decision_ref,
                decision_title=d.subject,
                decision_sha=d.commit_sha,
                governed_files=d.direct_files,
                directives=tuple(directives),
            )

    # 4. Extract violations
    violations: list[GuardViolation] = []

    # Stale warnings from drift report
    for w in drift_report.stale_warnings:
        severity = "VETO" if strict else "WARNING"
        violations.append(
            GuardViolation(
                file_path=w.file_path,
                severity=severity,
                decision_title=w.governing_decision,
                decision_sha=w.decision_sha[:8],
                message=f"Governed by {w.lineage_state} decision. {w.action_recommendation}",
                lineage_state=w.lineage_state,
                active_successor_title=w.current_active_decision,
                active_successor_sha=w.current_active_sha[:8] if w.current_active_sha else None,
            )
        )

    # Determine overall status
    has_veto = any(v.severity == "VETO" for v in violations)
    if has_veto:
        status = "VETOED"
    elif violations:
        status = "WARNING"
    else:
        status = "PASSED"

    agent_context, agent_degraded = _generate_agent_context(list(contracts_map.values()), violations)

    # 5. Optional Receipt Generation (RDD)
    receipt: ComplianceReceipt | None = None
    if generate_receipt:
        receipt_status = "COMPLIANT" if status == "PASSED" else "NON_COMPLIANT"
        doc_refs = list(contracts_map.keys())
        digest = _compute_receipt_digest(
            target=target,
            status=receipt_status,
            files=inspected_files,
            decisions=doc_refs,
            violations_count=len(violations),
        )
        receipt = ComplianceReceipt(
            receipt_version="1.0",
            timestamp=datetime.now(timezone.utc).isoformat(),
            target=target,
            status=receipt_status,
            inspected_count=len(inspected_files),
            inspected_files=inspected_files,
            governing_decision_refs=tuple(doc_refs),
            violation_count=len(violations),
            digest=digest,
        )

    # 6. Optional Engram Bridge (strictly opt-in via engram=True)
    engram_synced = False
    if engram and receipt is not None:
        # Write receipt into .engram/compliance-receipt.json if .engram/ exists or create it
        engram_dir = repo / ".engram"
        try:
            engram_dir.mkdir(exist_ok=True)
            receipt_file = engram_dir / "compliance-receipt.json"
            receipt_file.write_text(json.dumps(asdict(receipt), indent=2), encoding="utf-8")
            engram_synced = True
        except OSError:
            pass

    return GuardResult(
        target=target,
        status=status,
        inspected_files=inspected_files,
        contracts=tuple(contracts_map.values()),
        violations=tuple(violations),
        agent_context=agent_context,
        agent_rendering_degraded=agent_degraded,
        receipt=receipt,
        engram_synced=engram_synced,
    )


def format_guard_human(result: GuardResult) -> str:
    """Render human-readable output for terminal / developer view."""
    status_icon = "✅" if result.status == "PASSED" else "⚠️" if result.status == "WARNING" else "🛑"
    lines: list[str] = [
        f"🏛️  Bruriah Architectural Guard — {result.target}",
        f"   Status: {status_icon} {result.status} · {len(result.inspected_files)} file(s) · {len(result.contracts)} contract(s) · {len(result.violations)} violation(s)\n",
    ]

    if result.contracts:
        lines.append("Active Architectural Contracts:")
        for c in result.contracts:
            lines.append(f"  • {c.decision_title} ({c.decision_sha})")
            for d in c.directives:
                lines.append(f"    ↳ {d}")
        lines.append("")

    if result.violations:
        lines.append("Architectural Violations:")
        for v in result.violations:
            badge = "🛑 VETO" if v.severity == "VETO" else "⚠️  WARN"
            lines.append(f"  • [{badge}] {v.file_path}: {v.message}")
            if v.active_successor_title:
                lines.append(f"    Active successor: {v.active_successor_title} ({v.active_successor_sha})")
        lines.append("")

    if result.receipt:
        lines.append("Compliance Receipt (RDD):")
        lines.append(f"  • Status: {result.receipt.status}")
        lines.append(f"  • Digest: {result.receipt.digest[:16]}...")
        if result.engram_synced:
            lines.append("  • Engram: Synced to .engram/compliance-receipt.json")
        lines.append("")

    return "\n".join(lines).strip()


def format_guard_json(result: GuardResult) -> str:
    """Serialize guard result to JSON."""
    return json.dumps(asdict(result), indent=2)


def run_guard(
    paths: PlatformPaths,
    repo: Path,
    target: str,
    *,
    strict: bool = False,
    receipt: bool = False,
    engram: bool = False,
) -> GuardResult:
    """Open snapshot and execute architectural guard."""
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise GuardError(error.code) from error

    try:
        return evaluate_guard(
            repo,
            snapshot.database,
            target,
            strict=strict,
            generate_receipt=receipt,
            engram=engram,
        )
    finally:
        snapshot.database.close()
