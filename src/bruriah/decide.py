# Architectural Decision Scribe (bruriah decide):
# Structures and formalizes architectural decisions at creation time, ensuring
# high-quality context, problem articulation, evaluated alternatives, established
# invariants, and validated lineage trailers (Supersedes, Amends, Deprecates).
from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from .why import find_decision_in_database

if TYPE_CHECKING:
    from .platform import PlatformPaths


class DecideError(ValueError):
    """Raised when architectural decision creation or validation fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Alternative:
    """An alternative option evaluated and rejected during decision making."""

    name: str
    tradeoff: str
    rejected_reason: str


@dataclass(frozen=True)
class DecisionRecord:
    """A formalized architectural decision ready for commit or ADR publication."""

    title: str
    problem: str
    solution: str
    invariants: tuple[str, ...]
    alternatives: tuple[Alternative, ...] = ()
    supersedes: tuple[str, ...] = ()
    amends: tuple[str, ...] = ()
    deprecates: tuple[str, ...] = ()

    def format_commit_message(self) -> str:
        """Format decision as a clean, standardized Git commit message."""
        lines: list[str] = [self.title.strip(), ""]

        # Problem / Context
        lines.append("## Context & Problem")
        lines.append(self.problem.strip())
        lines.append("")

        # Solution
        lines.append("## Decision & Solution")
        lines.append(self.solution.strip())
        lines.append("")

        # Invariants
        if self.invariants:
            lines.append("## Invariants Established")
            for inv in self.invariants:
                lines.append(f"- {inv.strip()}")
            lines.append("")

        # Alternatives Considered
        if self.alternatives:
            lines.append("## Alternatives Considered")
            for alt in self.alternatives:
                lines.append(f"- **{alt.name.strip()}**: {alt.tradeoff.strip()}")
                lines.append(f"  * Rejected because: {alt.rejected_reason.strip()}")
            lines.append("")

        # Lineage Git Trailers
        trailers: list[str] = []
        for sha in self.supersedes:
            trailers.append(f"Supersedes: {sha.strip()}")
        for sha in self.amends:
            trailers.append(f"Amends: {sha.strip()}")
        for sha in self.deprecates:
            trailers.append(f"Deprecates: {sha.strip()}")

        if trailers:
            lines.append("\n".join(trailers))

        return "\n".join(lines).strip() + "\n"

    def format_adr_markdown(self) -> str:
        """Format decision as an Architecture Decision Record (ADR) document."""
        lines: list[str] = [
            f"# ADR: {self.title.strip()}",
            "",
            "## Status",
            "Accepted",
            "",
            "## Context",
            self.problem.strip(),
            "",
            "## Decision",
            self.solution.strip(),
            "",
        ]

        if self.invariants:
            lines.append("## Invariants")
            for inv in self.invariants:
                lines.append(f"- {inv.strip()}")
            lines.append("")

        if self.alternatives:
            lines.append("## Considered Alternatives & Tradeoffs")
            for alt in self.alternatives:
                lines.append(f"### {alt.name.strip()}")
                lines.append(f"- **Tradeoff**: {alt.tradeoff.strip()}")
                lines.append(f"- **Reason Rejected**: {alt.rejected_reason.strip()}")
                lines.append("")

        if self.supersedes or self.amends or self.deprecates:
            lines.append("## Lineage Relations")
            for s in self.supersedes:
                lines.append(f"- Supersedes: `{s.strip()}`")
            for a in self.amends:
                lines.append(f"- Amends: `{a.strip()}`")
            for d in self.deprecates:
                lines.append(f"- Deprecates: `{d.strip()}`")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

    def to_json(self) -> str:
        """Serialize decision record to structured JSON."""
        return json.dumps(asdict(self), indent=2)


def validate_predecessor_sha(
    database: sqlite3.Connection,
    target_sha_or_ref: str,
) -> str:
    """Validate that predecessor exists in SQLite snapshot and return full commit SHA."""
    clean = target_sha_or_ref.strip().lower()

    # Search for decision in SQLite database
    info = find_decision_in_database(database, clean)
    if info is not None:
        return info.commit_sha

    # Check documents table directly
    doc_row = database.execute(
        "SELECT metadata FROM documents WHERE document_ref = ? OR metadata LIKE ?",
        (clean, f'%"{clean}%'),
    ).fetchone()

    if doc_row:
        try:
            meta = json.loads(doc_row[0])
            c = str(meta.get("commit", "")).lower()
            if c:
                return c
        except Exception:
            pass

    raise DecideError(
        "predecessor_not_found",
        f"Predecessor decision or commit '{target_sha_or_ref}' was not found in the active index.",
    )


def execute_git_commit(repo: Path, message: str) -> str:
    """Commit staged changes with the formatted architectural decision message."""
    # Check if there are staged changes
    diff_res = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo,
        capture_output=True,
    )
    if diff_res.returncode == 0:
        raise DecideError(
            "no_staged_changes",
            "No staged changes to commit. Run 'git add <files>' before creating an architectural commit.",
        )

    try:
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        # Extract new commit SHA
        rev_res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return rev_res.stdout.strip()
    except subprocess.CalledProcessError as err:
        raise DecideError("git_commit_failed", err.stderr or str(err))


def run_decide(
    paths: PlatformPaths,
    repo: Path,
    title: str,
    problem: str,
    solution: str,
    invariants: Sequence[str],
    alternatives: Sequence[Alternative] = (),
    supersedes: Sequence[str] = (),
    amends: Sequence[str] = (),
    deprecates: Sequence[str] = (),
    commit: bool = False,
) -> tuple[DecisionRecord, str | None]:
    """Validate predecessors and generate decision record (optionally committing)."""
    from .platform import PlatformError, open_snapshot

    valid_supersedes: list[str] = []
    valid_amends: list[str] = []
    valid_deprecates: list[str] = []

    # Open snapshot to validate predecessors if any are specified
    if supersedes or amends or deprecates:
        try:
            snapshot = open_snapshot(paths)
            try:
                for s in supersedes:
                    valid_supersedes.append(validate_predecessor_sha(snapshot.database, s))
                for a in amends:
                    valid_amends.append(validate_predecessor_sha(snapshot.database, a))
                for d in deprecates:
                    valid_deprecates.append(validate_predecessor_sha(snapshot.database, d))
            finally:
                snapshot.database.close()
        except PlatformError as error:
            raise DecideError(error.code) from error

    record = DecisionRecord(
        title=title,
        problem=problem,
        solution=solution,
        invariants=tuple(invariants),
        alternatives=tuple(alternatives),
        supersedes=tuple(valid_supersedes if valid_supersedes else supersedes),
        amends=tuple(valid_amends if valid_amends else amends),
        deprecates=tuple(valid_deprecates if valid_deprecates else deprecates),
    )

    new_commit_sha: str | None = None
    if commit:
        msg = record.format_commit_message()
        new_commit_sha = execute_git_commit(repo, msg)

    return record, new_commit_sha
