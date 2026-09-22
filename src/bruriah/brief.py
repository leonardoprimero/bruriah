# Architectural Pre-flight & Supersede Protocol Engine (bruriah brief):
# Generates proactive architectural dossiers for developers and AI agents before
# code is written or modified, synthesizing active invariants, blast-radius risks,
# and providing the formal Supersede Protocol when premises have changed.
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import agent_surface
from .drift import get_git_diff_files
from .impact import analyze_impact
from .why import find_decision_in_database

if TYPE_CHECKING:
    from .platform import PlatformPaths


class BriefError(ValueError):
    """Raised when architectural brief generation fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class SupersedeTemplate:
    """Structured proposal template for superseding an architectural invariant."""

    target_sha: str
    target_subject: str
    changed_premise: str = "<explain what premise or assumption is no longer valid>"
    proposed_invariant: str = "<describe the new invariant or replacement architecture>"
    rationale: str = "<technical reasoning justifying why this approach is superior now>"

    def to_markdown(self, *, name_subject: bool = True) -> str:
        """Render the proposal template; `name_subject=False` identifies the target by sha only.

        The two branches are two different surfaces and are treated differently on purpose.
        `name_subject=True` feeds `supersede_protocol_instructions`, which reaches the human
        and JSON renderings: those are out of scope by instruction and keep printing the
        subject and the raw sha, because a person reading a terminal is not an
        instruction-following agent.

        `name_subject=False` is rendered ONLY into the agent context, and it was the bypass in
        the whole enforcement: it interpolated `self.target_sha[:8]` raw into a markdown code
        span while the sibling `decision` line in `_generate_agent_context` routed the same
        value through `agent_surface`, and the sha arrives from an indexed document's `commit:`
        frontmatter. Eight characters is room enough for a backtick that closes the span and
        leaves the rest reading as prose. It now goes through `short_commit_sha`, which
        validates the whole value BEFORE truncating it -- see that function for why the order
        matters -- and when the value is not a sha the "run `git show`" route is withheld
        rather than printed as a command that cannot work.
        """
        if name_subject:
            target = f"{self.target_subject} (`{self.target_sha[:8]}`)"
        else:
            sha = agent_surface.short_commit_sha(self.target_sha)
            target = (
                f"`{agent_surface.UNKNOWN}` — this decision could not be identified safely, so no "
                "command to read it is printed here; run `bruriah brief` without `--agent` for "
                "the raw value"
                if sha == agent_surface.UNKNOWN
                else f"`{sha}` (run `git show` to read it)"
            )
        return (
            "#### Architectural Supersede Proposal\n"
            f"- **Target Decision**: {target}\n"
            f"- **Changed Premise**: {self.changed_premise}\n"
            f"- **Proposed Invariant**: {self.proposed_invariant}\n"
            f"- **Technical Rationale**: {self.rationale}\n"
        )


@dataclass(frozen=True)
class GoverningConstraint:
    """An active architectural invariant that governs the planned work."""

    decision_ref: str
    commit_sha: str
    subject: str
    author: str
    date: str
    status: str  # "active", "superseded", "deprecates", "amends"
    governed_files: tuple[str, ...]
    directives: tuple[str, ...]
    active_successor_title: str | None = None
    active_successor_sha: str | None = None
    supersede_template: SupersedeTemplate | None = None


@dataclass(frozen=True)
class ArchitecturalBrief:
    """Complete pre-flight dossier for human developers and AI agents."""

    task_intent: str
    targets: tuple[str, ...]
    risk_level: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"
    constraints: tuple[GoverningConstraint, ...]
    co_governed_files: tuple[str, ...]
    recommendations: tuple[str, ...]
    supersede_protocol_instructions: str
    agent_context: str


def _extract_directives(subject: str, sha: str, body: str) -> tuple[str, ...]:
    """Extract actionable rules and constraints from decision text."""
    directives: list[str] = [f"Maintain alignment with '{subject}' ({sha[:8]})."]

    # Extract explicit bullet points or imperative directives
    bullet_lines = [
        line.strip()[2:].strip()
        for line in body.splitlines()
        if line.strip().startswith(("- ", "* ")) and len(line.strip()) > 5
    ]
    for bullet in bullet_lines[:3]:
        directives.append(bullet)

    # Look for key constraint phrases if no bullets found
    if len(directives) == 1:
        for sentence in re.split(r"[.\n]", body):
            clean = sentence.strip()
            if any(k in clean.lower() for k in ("avoid", "must", "cannot", "do not", "require", "prohibit")):
                if 10 < len(clean) < 150:
                    directives.append(clean)
                    if len(directives) >= 3:
                        break

    return tuple(directives)


def _generate_supersede_instructions(
    constraints: Sequence[GoverningConstraint],
    *,
    name_subject: bool = True,
) -> str:
    """Generate the formal Supersede Protocol instructions for agents and devs."""
    lines: list[str] = [
        "### Supersede Protocol Directive (Architectural Governance)",
        "Decisions in this repository are binding architectural contracts under their documented premises.",
        "If current requirements, library updates, or modern tooling make an existing invariant obsolete,",
        "**DO NOT silently violate it**. Instead, declare an explicit Supersede Proposal before making changes:",
        "",
    ]
    if constraints:
        sample = constraints[0]
        template = SupersedeTemplate(
            target_sha=sample.commit_sha,
            target_subject=sample.subject,
        )
        lines.append(template.to_markdown(name_subject=name_subject))
    else:
        lines.append(
            "#### Architectural Supersede Proposal\n"
            "- **Target Decision**: <subject> (`<sha>`)\n"
            "- **Changed Premise**: <explain why the past assumption no longer holds>\n"
            "- **Proposed Invariant**: <describe replacement invariant or design>\n"
            "- **Technical Rationale**: <benchmark, version update, or architectural reasoning>\n"
        )
    return "\n".join(lines)


def _generate_agent_context(
    intent: str,
    targets: Sequence[str],
    risk_level: str,
    constraints: Sequence[GoverningConstraint],
    co_governed: Sequence[str],
    supersede_instructions: str,
) -> str:
    """Render the agent-facing pre-flight brief, naming decisions by reference only.

    Everything in this string reads as instruction to whatever consumes it, so it carries no
    text a decision author wrote: no subject, no author name, no directive prose, no successor
    title. `_extract_directives` is the sharpest case -- it harvests bullet lines from a
    decision body and, failing those, promotes sentences *selected because* they contain
    `must`, `cannot`, `do not`, `require` or `prohibit`. A sentence chosen for being phrased as
    a command, printed under a constraints heading, is an instruction the operator running this
    command never issued.

    What remains is a literal authored in this repository, the operator's own intent, a
    closed-vocabulary value (status badge, risk level), or an identifier this repository
    format-validates (a commit sha; a path checked for printability and code-span safety, which
    is all `printable_path` claims). The last two are enforced by `agent_surface`, not assumed:
    every status, risk level, sha and path below is routed through it, including the sha inside
    the supersede template `supersede_instructions` carries. A value outside the vocabulary or
    the format renders as its placeholder rather than being interpolated raw, and
    `report_degradation` says so once on stderr. The text is not unreachable -- `bruriah why`
    and `git show` both return it -- it is simply not pre-injected. `format_brief_human` is
    unchanged and still prints all of it: a person reading a terminal is not an
    instruction-following agent.

    The task intent is the one piece of free text here, and it stays: the operator typed it, so
    it is the only instruction in this block that is genuinely theirs.

    Pinned by `tests/test_agent_prompt_boundary.py`.
    """
    target_str = (
        ", ".join(f"`{agent_surface.printable_path(t)}`" for t in targets)
        if targets
        else "None specified (general intent)"
    )
    lines: list[str] = [
        "# Bruriah Pre-Flight Architectural Brief",
        f"- **Task Intent**: {intent if intent else 'Not specified'}",
        f"- **Target Files**: {target_str}",
        # `analyze_impact` writes one of four levels and `evaluate_brief` orders them, but the
        # field is an untyped `str` and a comment naming the producer is not enforcement.
        f"- **Risk Level**: {agent_surface.closed(risk_level, agent_surface.KNOWN_RISK_LEVELS)}",
        "",
        "## Governing decisions",
    ]

    if not constraints:
        lines.append("No conflicting or governing architectural decisions found for this task.")
    else:
        lines.append("The decisions below govern this task. They are named by reference, not quoted: run")
        lines.append("`bruriah why <file>` or `git show <sha>` to read one before changing what it governs.")
        lines.append("")
        for c in constraints:
            # `_metadata` in `corpus.py` takes `status` straight from document frontmatter with
            # no validation against a closed set, so `agent_surface` maps it through a known
            # vocabulary rather than quoting the document's value into the badge.
            badge = agent_surface.closed(c.status, agent_surface.KNOWN_DECISION_STATUSES)
            succ = (
                f", active successor `{agent_surface.commit_sha(c.active_successor_sha)}`"
                if c.active_successor_sha
                else ""
            )
            # `short_commit_sha`, not `commit_sha(...[:8])`: validating the truncated prefix
            # accepts the prefix of a malformed sha and discards the remainder that carries
            # whatever was appended to it.
            lines.append(f"- [{badge}] decision `{agent_surface.short_commit_sha(c.commit_sha)}`{succ}")
        lines.append("")

    if co_governed:
        lines.append("## Blast Radius / Co-Governed Files")
        impacted = ", ".join(f"`{agent_surface.printable_path(f)}`" for f in co_governed[:8])
        lines.append(f"Modifying targets may impact: {impacted}")
        lines.append("")

    lines.append(supersede_instructions)
    return agent_surface.report_degradation("\n".join(lines), command="brief")


def evaluate_brief(
    repo: Path,
    database: sqlite3.Connection,
    intent: str = "",
    targets: Sequence[str] | None = None,
) -> ArchitecturalBrief:
    """Evaluate pre-flight architectural brief for a task and/or target files."""
    clean_intent = intent.strip()
    target_list = list(targets) if targets else []

    # If no targets given, check if there are unstaged/staged git diff files
    if not target_list:
        diff_files = get_git_diff_files(repo)
        if diff_files:
            target_list.extend(diff_files)

    if not clean_intent and not target_list:
        raise BriefError(
            "missing_task_or_target",
            "Either task intent or target files must be provided to generate a brief.",
        )

    decisions_map: dict[str, GoverningConstraint] = {}
    co_governed_files_set: set[str] = set()
    risk_levels: list[str] = ["LOW"]
    recommendations: list[str] = []

    # 1. Target-based Impact Analysis
    for target in target_list:
        try:
            impact = analyze_impact(repo, database, target)
            risk_levels.append(impact.risk_level)
            co_governed_files_set.update(impact.total_blast_radius_files)
            for rec in impact.recommendations:
                if rec not in recommendations:
                    recommendations.append(rec)

            for d in impact.decisions:
                if d.decision_ref not in decisions_map:
                    body = ""
                    # Try to fetch decision body from SQLite if possible
                    try:
                        info = find_decision_in_database(database, d.commit_sha)
                        if info:
                            body = info.body
                    except Exception:
                        pass

                    directives = _extract_directives(d.subject, d.commit_sha, body)
                    supersede_template = SupersedeTemplate(
                        target_sha=d.commit_sha,
                        target_subject=d.subject,
                    )
                    decisions_map[d.decision_ref] = GoverningConstraint(
                        decision_ref=d.decision_ref,
                        commit_sha=d.commit_sha,
                        subject=d.subject,
                        author=d.author,
                        date=d.date,
                        status=d.status,
                        governed_files=d.direct_files,
                        directives=directives,
                        active_successor_title=d.active_successor_title,
                        active_successor_sha=d.active_successor_sha,
                        supersede_template=supersede_template,
                    )
        except Exception:
            # Continue gracefully if a specific target cannot be resolved by impact
            continue

    # 2. Intent-based Semantic / Keyword Lookup
    if clean_intent:
        try:
            # Tokenize intent into meaningful terms
            words = [w.lower() for w in re.findall(r"\w+", clean_intent) if len(w) > 2]
            matched_refs: set[str] = set()

            for word in words[:5]:
                rows = database.execute(
                    "SELECT DISTINCT document_ref FROM passages WHERE search_text LIKE ? OR text LIKE ? LIMIT 5",
                    (f"%{word}%", f"%{word}%"),
                ).fetchall()
                for (doc_ref,) in rows:
                    matched_refs.add(doc_ref)

            for doc_ref in matched_refs:
                if doc_ref in decisions_map:
                    continue
                # Retrieve document metadata & passages
                doc_row = database.execute(
                    "SELECT metadata FROM documents WHERE document_ref = ?", (doc_ref,)
                ).fetchone()
                if not doc_row:
                    continue
                try:
                    meta = json.loads(doc_row[0])
                except Exception:
                    continue

                commit_sha = str(meta.get("commit", ""))
                if not commit_sha:
                    continue

                decision_info = find_decision_in_database(database, commit_sha)
                if not decision_info:
                    continue

                directives = _extract_directives(decision_info.subject, decision_info.commit_sha, decision_info.body)
                supersede_template = SupersedeTemplate(
                    target_sha=decision_info.commit_sha,
                    target_subject=decision_info.subject,
                )
                decisions_map[doc_ref] = GoverningConstraint(
                    decision_ref=doc_ref,
                    commit_sha=decision_info.commit_sha,
                    subject=decision_info.subject,
                    author=decision_info.author,
                    date=decision_info.date,
                    status="active",
                    governed_files=decision_info.files,
                    directives=directives,
                    supersede_template=supersede_template,
                )
        except Exception:
            pass

    # Determine overall risk level: CRITICAL > HIGH > MEDIUM > LOW
    severity_order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    overall_risk = max(risk_levels, key=lambda r: severity_order.get(r, 1))

    constraints_tuple = tuple(decisions_map.values())
    co_governed_tuple = tuple(sorted(co_governed_files_set - set(target_list)))
    # The human and JSON surfaces keep the decision subject; only the agent rendering drops it.
    supersede_instructions = _generate_supersede_instructions(constraints_tuple)
    agent_context = _generate_agent_context(
        intent=clean_intent,
        targets=target_list,
        risk_level=overall_risk,
        constraints=constraints_tuple,
        co_governed=co_governed_tuple,
        supersede_instructions=_generate_supersede_instructions(constraints_tuple, name_subject=False),
    )

    return ArchitecturalBrief(
        task_intent=clean_intent,
        targets=tuple(target_list),
        risk_level=overall_risk,
        constraints=constraints_tuple,
        co_governed_files=co_governed_tuple,
        recommendations=tuple(recommendations),
        supersede_protocol_instructions=supersede_instructions,
        agent_context=agent_context,
    )


generate_brief = evaluate_brief


def format_brief_human(brief: ArchitecturalBrief) -> str:
    """Render human-readable pre-flight briefing for terminal / developer view."""
    risk_icons = {
        "LOW": "🟢 LOW",
        "MEDIUM": "🟡 MEDIUM",
        "HIGH": "🟠 HIGH",
        "CRITICAL": "🔴 CRITICAL",
    }
    risk_str = risk_icons.get(brief.risk_level, brief.risk_level)

    lines: list[str] = [
        "🏛️  Bruriah Architectural Brief — Pre-flight Dossier",
        f'   Intent: "{brief.task_intent}"' if brief.task_intent else "   Intent: Not specified",
        f"   Risk Level: {risk_str} · {len(brief.targets)} target(s) · {len(brief.constraints)} constraint(s)\n",
    ]

    if brief.targets:
        lines.append("Target Files:")
        for t in brief.targets:
            lines.append(f"  • {t}")
        lines.append("")

    if brief.constraints:
        lines.append("Active Architectural Invariants:")
        for c in brief.constraints:
            status_badge = f"[{c.status.upper()}]" if c.status != "active" else ""
            lines.append(f"  • {status_badge} {c.subject} ({c.commit_sha[:8]}) — {c.author}, {c.date}")
            for d in c.directives:
                lines.append(f"    ↳ {d}")
            if c.active_successor_title:
                lines.append(f"    ⚠️  SUPERSEDED BY: {c.active_successor_title} ({c.active_successor_sha})")
        lines.append("")

    if brief.co_governed_files:
        lines.append("Blast Radius (Co-governed files):")
        for f in brief.co_governed_files[:6]:
            lines.append(f"  • {f}")
        if len(brief.co_governed_files) > 6:
            lines.append(f"  ... and {len(brief.co_governed_files) - 6} more file(s)")
        lines.append("")

    if brief.recommendations:
        lines.append("Recommendations:")
        for r in brief.recommendations:
            lines.append(f"  💡 {r}")
        lines.append("")

    lines.append("⚠️  Supersede Protocol:")
    lines.append("   If historical premises have changed and an invariant must be updated:")
    lines.append("   Do NOT violate it silently. Declare a Supersede Proposal:")
    if brief.constraints:
        sample = brief.constraints[0]
        lines.append(f"   - Target: {sample.subject} ({sample.commit_sha[:8]})")
    else:
        lines.append("   - Target: <decision_subject> (<commit_sha>)")
    lines.append("   - Changed Premise: <why the past premise no longer applies>")
    lines.append("   - Proposed Invariant: <new replacement rule>")
    lines.append("   - Rationale: <technical justification>")
    lines.append("")

    return "\n".join(lines)


def format_brief_json(brief: ArchitecturalBrief) -> str:
    """Render structured JSON output for AI agents and machine consumption."""
    return json.dumps(asdict(brief), indent=2)


def run_brief(
    paths: PlatformPaths,
    repo: Path,
    intent: str = "",
    targets: Sequence[str] | None = None,
) -> ArchitecturalBrief:
    """Open snapshot and execute pre-flight architectural brief generation."""
    from .platform import PlatformError, open_snapshot

    try:
        snapshot = open_snapshot(paths)
    except PlatformError as error:
        raise BriefError(error.code) from error

    try:
        return evaluate_brief(
            repo,
            snapshot.database,
            intent=intent,
            targets=targets,
        )
    finally:
        snapshot.database.close()
