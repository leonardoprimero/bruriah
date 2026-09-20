# Architectural Healing & Pedagogical Remediation Engine (bruriah heal):
# Bridges detection and resolution by synthesizing actionable refactoring blueprints
# from historical architectural decisions, teaching AI agents and developers how
# to remediate violations using the project's canonical patterns.
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .drift import get_git_diff_files
from .guard import evaluate_guard
from .why import find_decision_in_database

if TYPE_CHECKING:
    from .platform import PlatformPaths


class HealError(ValueError):
    """Raised when architectural healing fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class RemediationStep:
    """A concrete, sequential step required to remediate an architectural violation."""

    order: int
    action: str
    detail: str


@dataclass(frozen=True)
class RemediationBlueprint:
    """A pedagogical blueprint guiding the refactoring of an architectural violation."""

    file_path: str
    decision_subject: str
    decision_sha: str
    violation_message: str
    canonical_pattern: str
    refactoring_steps: tuple[RemediationStep, ...]
    directives: tuple[str, ...]


@dataclass(frozen=True)
class HealingResult:
    """Complete architectural remediation outcome."""

    target: str
    status: str  # "COMPLIANT", "HEALABLE", "UNRESOLVED"
    inspected_files: tuple[str, ...]
    blueprints: tuple[RemediationBlueprint, ...]
    agent_prompt: str


def _synthesize_steps(
    file_path: str,
    subject: str,
    sha: str,
    message: str,
    body: str,
) -> tuple[tuple[RemediationStep, ...], str, tuple[str, ...]]:
    """Synthesize canonical patterns and sequential refactoring steps from decision body."""
    directives: list[str] = [f"Align implementation with '{subject}' ({sha[:8]})."]

    # Extract bullets from decision body as canonical directives
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) and len(stripped) > 5:
            directives.append(stripped[2:].strip())
        if len(directives) >= 4:
            break

    # Extract or summarize canonical pattern
    canonical_pattern = (
        directives[1]
        if len(directives) > 1
        else f"Follow architectural constraints established in commit {sha[:8]}."
    )

    steps = (
        RemediationStep(
            order=1,
            action="Isolate Non-Compliant Code",
            detail=f"Identify and decouple the offending logic in `{file_path}` causing: {message}",
        ),
        RemediationStep(
            order=2,
            action="Apply Canonical Architectural Pattern",
            detail=f"Restructure according to '{subject}': {canonical_pattern}",
        ),
        RemediationStep(
            order=3,
            action="Verify Architectural Compliance",
            detail=f"Run 'bruriah guard {file_path}' to confirm the violation is resolved.",
        ),
    )

    return steps, canonical_pattern, tuple(directives)


def _generate_agent_prompt(
    target: str,
    blueprints: Sequence[RemediationBlueprint],
) -> str:
    """Generate structured, pedagogical instructions for AI coding agents."""
    if not blueprints:
        return "No architectural violations detected. The codebase complies with active governance."

    lines: list[str] = [
        "# 🛠️ Bruriah Architectural Remediation Blueprint",
        f"**Target**: `{target}`",
        "**Instruction for Agent**: Do NOT apply quick hacks, monkey-patches, or bypass interfaces.",
        "Refactor the code according to the canonical project patterns below:\n",
    ]

    for i, bp in enumerate(blueprints, start=1):
        lines.append(f"## Issue {i}: Violation in `{bp.file_path}`")
        lines.append(f"- **Governing Decision**: {bp.decision_subject} (`{bp.decision_sha[:8]}`)")
        lines.append(f"- **Violation**: {bp.violation_message}")
        lines.append(f"- **Canonical Design Pattern**: {bp.canonical_pattern}")
        lines.append("- **Directives**:")
        for d in bp.directives:
            lines.append(f"  * {d}")
        lines.append("- **Actionable Refactoring Recipe**:")
        for step in bp.refactoring_steps:
            lines.append(f"  {step.order}. **{step.action}**: {step.detail}")
        lines.append("")

    lines.append(
        "After refactoring, ensure that all unit tests pass and code strictly adheres to the above directives."
    )
    return "\n".join(lines)


def evaluate_heal(
    repo: Path,
    database: sqlite3.Connection,
    target: str | None = None,
) -> HealingResult:
    """Evaluate architectural violations and generate pedagogical remediation blueprints."""
    resolved_target = target.strip() if target and target.strip() else ""

    if not resolved_target:
        diff_files = get_git_diff_files(repo)
        if diff_files:
            resolved_target = diff_files[0] if len(diff_files) == 1 else "."
        else:
            resolved_target = "."

    # 1. Run guard evaluation to detect violations
    guard_result = evaluate_guard(repo, database, resolved_target, strict=False)

    if guard_result.status == "PASSED":
        return HealingResult(
            target=resolved_target,
            status="COMPLIANT",
            inspected_files=guard_result.inspected_files,
            blueprints=(),
            agent_prompt="No architectural violations detected. Code is compliant.",
        )

    blueprints: list[RemediationBlueprint] = []

    for v in guard_result.violations:
        decision_body = ""
        decision_subject = v.decision_title
        decision_sha = v.decision_sha

        # Try to retrieve full decision details from SQLite
        info = find_decision_in_database(database, v.decision_sha)
        if info is not None:
            decision_body = info.body
            decision_subject = info.subject
            decision_sha = info.commit_sha

        steps, canonical_pattern, directives = _synthesize_steps(
            file_path=v.file_path,
            subject=decision_subject,
            sha=decision_sha,
            message=v.message,
            body=decision_body,
        )

        blueprints.append(
            RemediationBlueprint(
                file_path=v.file_path,
                decision_subject=decision_subject,
                decision_sha=decision_sha,
                violation_message=v.message,
                canonical_pattern=canonical_pattern,
                refactoring_steps=steps,
                directives=directives,
            )
        )

    agent_prompt = _generate_agent_prompt(resolved_target, blueprints)

    return HealingResult(
        target=resolved_target,
        status="HEALABLE" if blueprints else "UNRESOLVED",
        inspected_files=guard_result.inspected_files,
        blueprints=tuple(blueprints),
        agent_prompt=agent_prompt,
    )


def format_heal_human(result: HealingResult) -> str:
    """Render human-friendly remediation guide for terminal view."""
    lines: list[str] = [
        f"🏛️  Bruriah Architectural Healing — {result.target}",
        f"   Status: {'✅ COMPLIANT' if result.status == 'COMPLIANT' else '🔧 HEALABLE'} · {len(result.blueprints)} blueprint(s) generated\n",
    ]

    if result.status == "COMPLIANT":
        lines.append("No architectural violations detected. The codebase adheres to active governance.")
        return "\n".join(lines)

    for i, bp in enumerate(result.blueprints, start=1):
        lines.append(f"Blueprint #{i}: {bp.file_path}")
        lines.append(f"  • Issue: {bp.violation_message}")
        lines.append(f"  • Governed by: {bp.decision_subject} ({bp.decision_sha[:8]})")
        lines.append(f"  • Canonical Pattern: {bp.canonical_pattern}")
        lines.append("  • Refactoring Recipe:")
        for step in bp.refactoring_steps:
            lines.append(f"    {step.order}. {step.action}: {step.detail}")
        lines.append("")

    return "\n".join(lines)


def format_heal_agent(result: HealingResult) -> str:
    """Return prompt injection snippet for AI agents."""
    return result.agent_prompt


def format_heal_json(result: HealingResult) -> str:
    """Serialize healing result to structured JSON."""
    return json.dumps(asdict(result), indent=2)


def run_heal(
    paths: PlatformPaths,
    repo: Path,
    target: str | None = None,
) -> HealingResult:
    """Open snapshot and execute architectural healing."""
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise HealError(error.code) from error

    try:
        return evaluate_heal(repo, snapshot.database, target)
    finally:
        snapshot.database.close()
