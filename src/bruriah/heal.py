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

from . import agent_surface
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
    # The lineage relation, upper-cased: SUPERSEDES, DEPRECATES or AMENDS. Carried from the
    # guard violation so the agent-facing rendering can say WHY a file is flagged without
    # quoting `violation_message`, which states the same fact inside a sentence that also
    # quotes the successor decision's subject. The vocabulary is closed in `agent_surface`,
    # which is where the agent rendering maps it through `KNOWN_LINEAGE_STATES`.
    lineage_state: str = ""


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
        directives[1] if len(directives) > 1 else f"Follow architectural constraints established in commit {sha[:8]}."
    )

    # The actions are the exact literals in `agent_surface.KNOWN_REMEDIATION_ACTIONS`, which is
    # what lets the agent rendering print them rather than fall back to a generic label.
    # `tests/test_heal.py` asserts the two cannot drift apart.
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
    """Render the agent-facing remediation blueprint, naming decisions by reference only.

    Everything in this string reads as instruction to whatever consumes it -- it is a
    numbered refactoring recipe under a "do this" heading -- so it carries no text a
    decision author wrote: no subject, no canonical pattern, no directive prose, no
    violation message. This renderer was the sharpest case of the three. `_synthesize_steps`
    harvests `- ` and `* ` bullet lines out of a decision body into `directives`, promotes
    `directives[1]` to `canonical_pattern`, and step 2 then rendered that bullet as the
    instruction to restructure by. A line lifted from a document and printed as step 2 of a
    refactoring recipe is an instruction the operator running this command never issued.
    `violation_message` additionally quotes the successor decision's subject, since it comes
    from drift's `action_recommendation`.

    What remains is a literal authored in this repository, a closed-vocabulary value (lineage
    relation, remediation action), or an identifier this repository format-validates (a commit
    sha; a path checked for printability and code-span safety, which is all `printable_path`
    claims). The last two are enforced by `agent_surface`, not assumed: every sha, path,
    lineage state and step action below is routed through it -- including the `**Target**`
    header, which is the operator's own argument but is rendered in a code span like every
    other path here. The text is not unreachable -- the route below points the agent at
    `bruriah why` and `git show`, which both return it -- it is simply not pre-injected.
    `format_heal_human` and `format_heal_json` are unchanged and still carry all of it: a
    person reading a terminal is not an instruction-following agent.

    **When an identifier is rejected, the route is withheld rather than printed.** This
    renderer used to emit ``run `bruriah why <unprintable path>` or `git show UNKNOWN` `` --
    a literal command it invited an agent to run, naming a path that is not one and a revision
    git answers with `fatal: ambiguous argument`. Withholding the prose is only honest while
    the route to it works, so where it cannot the rendering says the decision could not be
    identified safely and points at the human rendering, which still has the raw value.
    `report_degradation` says the same thing once on stderr, so the substitution is visible to
    the operator rather than only to whatever reads the prompt.

    The recipe is rendered from `bp.refactoring_steps` rather than restated here, so a
    maintainer editing the step wording in `_synthesize_steps` sees the edit reach `--agent`
    instead of leaving two parallel recipes. Only `step.action` is rendered, and only when it
    is one this repository authored -- see the comments at the loop.

    Pinned by `tests/test_agent_prompt_boundary.py`.
    """
    if not blueprints:
        return "No architectural violations detected. The codebase complies with active governance."

    lines: list[str] = [
        "# 🛠️ Bruriah Architectural Remediation Blueprint",
        f"**Target**: `{agent_surface.printable_path(target)}`",
        "**Instruction for Agent**: Do NOT apply quick hacks, monkey-patches, or bypass interfaces.",
        "Refactor the code according to the canonical project patterns. The governing decisions",
        "below are named by reference, not quoted: read one before changing what it governs.\n",
    ]

    for i, bp in enumerate(blueprints, start=1):
        state = agent_surface.closed(bp.lineage_state, agent_surface.KNOWN_LINEAGE_STATES)
        sha = agent_surface.commit_sha(bp.decision_sha)
        path = agent_surface.printable_path(bp.file_path)
        lines.append(f"## Issue {i}: Violation in `{path}`")
        lines.append(f"- **Governing Decision**: `{sha}`")
        lines.append(f"- **Lineage State**: {state}")
        # The route is printed only when both halves of it are identifiers. `bruriah why` takes
        # the path and `git show` takes the sha, so a placeholder in either makes the printed
        # command fail for the agent told to run it -- and a command that cannot work is worse
        # than no command, because it reads as though the prose were reachable.
        if sha == agent_surface.UNKNOWN or path == agent_surface.UNPRINTABLE_PATH:
            lines.append(
                "- **No route printed**: this decision could not be identified safely, so no "
                "command to read it is given here. Run `bruriah heal` without `--agent` for the "
                "raw values."
            )
        else:
            lines.append(
                f"- **Read the governing decision before changing what it governs**: run `bruriah why {path}` "
                f"or `git show {sha}`."
            )
        lines.append("- **Actionable Refactoring Recipe**:")
        # `step.action` only, never `step.detail`. Every action `_synthesize_steps` builds is a
        # literal authored in this repository, but every detail interpolates `message` and
        # `canonical_pattern`: `message` comes from drift's `action_recommendation` and quotes
        # the successor decision's subject, and `canonical_pattern` is a bullet line harvested
        # out of a decision body. Rendering a detail here would put text a decision author
        # wrote into a numbered instruction the operator never issued, which is the defect this
        # renderer exists to avoid. `format_heal_human` still prints both.
        #
        # And the action is checked against `KNOWN_REMEDIATION_ACTIONS` rather than trusted,
        # because "every action is a repository literal" was a claim about one producer while
        # `RemediationStep.action` is an untyped free string on a public dataclass -- the same
        # comment-instead-of-enforcement pattern `agent_surface` exists to end. An unrecognised
        # action keeps its step NUMBER, which is this repository's own structure, and loses its
        # wording.
        for step in bp.refactoring_steps:
            action = agent_surface.authored(
                step.action,
                agent_surface.KNOWN_REMEDIATION_ACTIONS,
                agent_surface.UNRECOGNISED_ACTION,
            )
            lines.append(f"  {step.order}. **{action}**")
        lines.append("")

    # "the above directives" used to point at bullet lines harvested from decision bodies.
    # Those are gone from this rendering, so the sentence names what is actually above it.
    lines.append(
        "After refactoring, ensure that all unit tests pass and the code strictly adheres to the "
        "governing decisions named above."
    )
    return agent_surface.report_degradation("\n".join(lines), command="heal")


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
                lineage_state=v.lineage_state,
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
    """Return the agent-facing remediation summary.

    The old name for this was "prompt injection snippet", which is precisely what the
    rendering now refuses to be: it carries no decision prose, only structure — the violated
    path, the governing decision's sha, its lineage state, and a recipe written here. See
    `_generate_agent_prompt` for why.
    """
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
