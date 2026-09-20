# Git History Mining & Cold-Start Bootstrap Engine:
# Automatically analyzes existing Git history in any repository to identify
# architectural decisions, extract them into structured markdown files, infer
# lineage relations, and build an initial Bruriah snapshot in seconds.
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ._cli.common import DEFAULT_EMBEDDING_MODEL
from .index_runner import run_index

if TYPE_CHECKING:
    from .platform import PlatformPaths


class BootstrapError(ValueError):
    """Raised when bootstrap fails."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class MinedDecision:
    """An architectural decision extracted from Git history."""

    sha: str
    author: str
    date: str
    subject: str
    body: str
    files: tuple[str, ...]
    score: float
    reasons: tuple[str, ...]
    inferred_supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class BootstrapResult:
    """Result of the bootstrap operation."""

    total_commits_scanned: int
    candidates_found: int
    decisions_written: int
    out_dir: Path
    decisions: tuple[MinedDecision, ...]
    dry_run: bool


# Keywords that signal architectural significance in commit messages
_ARCH_SUBJECT_PATTERNS: list[tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"^feat!|BREAKING\s*CHANGE", re.IGNORECASE), 0.4, "breaking change"),
    (re.compile(r"^refactor", re.IGNORECASE), 0.3, "refactoring"),
    (re.compile(r"\b(architectur\w*|redesign\w*)\b", re.IGNORECASE), 0.35, "architectural keyword"),
    (re.compile(r"\b(migrat\w*|replac\w*|deprecat\w*)\b", re.IGNORECASE), 0.3, "migration/deprecation"),
    (re.compile(r"\b(protocol|schema|engine|core|pipeline)\b", re.IGNORECASE), 0.25, "core subsystem"),
]

# Keywords that penalize commit score (routine / non-architectural)
_ROUTINE_SUBJECT_PATTERNS: list[tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"^(chore|style|docs|typo|lint|ci|build)(\([^)]*\))?:", re.IGNORECASE), -0.35, "routine chore/style/docs"),
    (re.compile(r"^bump\s+version|release\s+v?\d", re.IGNORECASE), -0.4, "version bump"),
    (re.compile(r"^(merge\s+branch|merge\s+pull\s+request)", re.IGNORECASE), -0.5, "merge commit"),
]

_ARCH_BODY_PATTERNS: list[tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"\b(because|rationale|decided|trade-?off|instead\s+of)\b", re.IGNORECASE), 0.25, "reasoning keyword"),
    (re.compile(r"\b(supersedes|deprecates|amends):\s*[a-f0-9]+", re.IGNORECASE), 0.4, "explicit lineage trailer"),
    (re.compile(r"\b(why\s+this\s+was\s+written|motivation)\b", re.IGNORECASE), 0.3, "motivation header"),
]


def score_commit(
    subject: str,
    body: str,
    files: Sequence[str],
) -> tuple[float, tuple[str, ...]]:
    """Score a commit based on architectural heuristics.

    Returns:
        (score between 0.0 and 1.0, list of reasons)
    """
    score = 0.1  # base score
    reasons: list[str] = []

    # 1. Subject analysis
    for pattern, weight, reason in _ARCH_SUBJECT_PATTERNS:
        if pattern.search(subject):
            score += weight
            reasons.append(reason)

    for pattern, weight, reason in _ROUTINE_SUBJECT_PATTERNS:
        if pattern.search(subject):
            score += weight
            reasons.append(reason)

    # 2. Body analysis
    body_lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(body_lines) >= 5:
        score += 0.25
        reasons.append(f"detailed body ({len(body_lines)} lines)")
    elif len(body_lines) >= 2:
        score += 0.15
        reasons.append("multi-line body")

    for pattern, weight, reason in _ARCH_BODY_PATTERNS:
        if pattern.search(body):
            score += weight
            reasons.append(reason)

    # 3. File impact analysis
    if len(files) >= 4:
        score += 0.15
        reasons.append(f"cross-cutting change ({len(files)} files)")

    # Core architecture directory check
    core_dirs = ("core/", "arch/", "api/", "models/", "engine/", "db/", "service/")
    if any(any(f.startswith(d) or f"/{d}" in f for d in core_dirs) for f in files):
        score += 0.15
        reasons.append("touches core architectural components")

    # Clamp to [0.0, 1.0]
    final_score = max(0.0, min(1.0, score))
    return final_score, tuple(reasons)


def _slugify(text: str) -> str:
    """Create a URL/filename-friendly slug from text."""
    clean = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[-\s]+", "-", clean).strip("-")[:40]


def _extract_trailers(body: str) -> list[str]:
    """Extract Supersedes trailers if already present in commit body."""
    trailers: list[str] = []
    for line in body.splitlines():
        match = re.match(r"^\s*supersedes:\s*([a-f0-9]+)", line, re.IGNORECASE)
        if match:
            trailers.append(match.group(1).lower())
    return trailers


def mine_git_history(
    repo: Path,
    *,
    limit: int = 50,
    min_score: float = 0.5,
) -> tuple[int, tuple[MinedDecision, ...]]:
    """Scan git log, score commits, and extract candidate architectural decisions.

    Returns:
        (total_commits_scanned, tuple of MinedDecision sorted chronologically)
    """
    cmd = [
        "git",
        "log",
        "--reverse",
        "--format=COMMIT_DELIM%n%H%n%an%n%aI%n%s%nBODY_START%n%B%nBODY_END",
        "--name-only",
        "--no-merges",
    ]

    try:
        res = subprocess.run(
            cmd,
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise BootstrapError("git_not_found")
    except subprocess.CalledProcessError as err:
        raise BootstrapError("git_error", err.stderr or "")

    raw_commits = res.stdout.split("COMMIT_DELIM\n")
    candidates: list[MinedDecision] = []
    total_scanned = 0

    for chunk in raw_commits:
        if not chunk.strip():
            continue
        total_scanned += 1

        lines = chunk.splitlines()
        if len(lines) < 5:
            continue

        sha = lines[0].strip()
        author = lines[1].strip()
        date_iso = lines[2].strip()[:10]
        subject = lines[3].strip()

        # Extract body between BODY_START and BODY_END
        body_lines: list[str] = []
        file_lines: list[str] = []
        in_body = False
        in_files = False

        for line in lines[4:]:
            if line == "BODY_START":
                in_body = True
                continue
            if line == "BODY_END":
                in_body = False
                in_files = True
                continue
            if in_body:
                body_lines.append(line)
            elif in_files:
                if line.strip():
                    file_lines.append(line.strip())

        body_text = "\n".join(body_lines).strip()
        score, reasons = score_commit(subject, body_text, file_lines)

        if score >= min_score:
            trailers = _extract_trailers(body_text)
            candidates.append(
                MinedDecision(
                    sha=sha,
                    author=author,
                    date=date_iso,
                    subject=subject,
                    body=body_text,
                    files=tuple(file_lines),
                    score=score,
                    reasons=reasons,
                    inferred_supersedes=tuple(trailers),
                )
            )

    # Candidates are already chronologically ordered by git log --reverse

    # Infer supersedes relations for decisions touching the same files with refactor/migrate
    enriched: list[MinedDecision] = []
    for idx, cand in enumerate(candidates):
        inferred = list(cand.inferred_supersedes)
        if not inferred and any("refactor" in r or "migration" in r for r in cand.reasons):
            # Look back at earlier candidates touching the same files
            cand_files = set(cand.files)
            for prev in reversed(candidates[:idx]):
                prev_files = set(prev.files)
                if cand_files and prev_files and len(cand_files & prev_files) >= 1:
                    # Found an earlier architectural predecessor touching the same domain
                    inferred.append(prev.sha[:8])
                    break

        enriched.append(
            MinedDecision(
                sha=cand.sha,
                author=cand.author,
                date=cand.date,
                subject=cand.subject,
                body=cand.body,
                files=cand.files,
                score=cand.score,
                reasons=cand.reasons,
                inferred_supersedes=tuple(inferred),
            )
        )

    # Apply limit
    if len(enriched) > limit:
        # Keep the highest scoring decisions while preserving chronological order
        top_shas = {c.sha for c in sorted(enriched, key=lambda c: c.score, reverse=True)[:limit]}
        enriched = [c for c in enriched if c.sha in top_shas]

    return total_scanned, tuple(enriched)


def format_decision_markdown(decision: MinedDecision) -> str:
    """Format a MinedDecision into a standard Bruriah decision Markdown document."""
    frontmatter_lines: list[str] = ["---", f"commit: {decision.sha}"]
    if decision.inferred_supersedes:
        frontmatter_lines.append("supersedes:")
        for s in decision.inferred_supersedes:
            frontmatter_lines.append(f"  - {s}")
    frontmatter_lines.append("---\n")

    frontmatter = "\n".join(frontmatter_lines)

    files_list = "\n".join(f"- `{f}`" for f in decision.files) if decision.files else "- *(no files recorded)*"

    body_section = decision.body if decision.body else "*(No explanatory commit body provided)*"

    doc = f"""{frontmatter}# {decision.subject}

**Decided:** {decision.date} · **Commit:** `{decision.sha[:8]}` · **Author:** {decision.author}

{body_section}

## Files this decision touched
{files_list}
"""
    return doc


def write_decisions(
    decisions: Sequence[MinedDecision],
    out_dir: Path,
) -> int:
    """Write mined decisions as markdown files into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0

    for dec in decisions:
        slug = _slugify(dec.subject)
        filename = f"{dec.date}-{dec.sha[:8]}-{slug}.md" if slug else f"{dec.date}-{dec.sha[:8]}.md"
        target = out_dir / filename
        content = format_decision_markdown(dec)
        target.write_text(content, encoding="utf-8")
        written += 1

    return written


def run_bootstrap(
    repo: Path,
    out_dir: Path,
    *,
    limit: int = 50,
    min_score: float = 0.5,
    dry_run: bool = False,
    index_after: bool = False,
    paths: PlatformPaths | None = None,
) -> BootstrapResult:
    """Mine Git history, write decision documents, and optionally build the index."""
    total_scanned, candidates = mine_git_history(repo, limit=limit, min_score=min_score)

    decisions_written = 0
    if not dry_run and candidates:
        decisions_written = write_decisions(candidates, out_dir)

    if index_after and not dry_run and paths is not None:
        # Create a default policy.yaml in out_dir's parent or out_dir if needed
        policy_path = out_dir.parent / "policy.yaml"
        if not policy_path.exists():
            policy_content = (
                "version: 1\n"
                "include:\n"
                "  - '**/*.md'\n"
                "exclude:\n"
                "  - '**/archive/**'\n"
            )
            policy_path.write_text(policy_content, encoding="utf-8")

        run_index(
            paths,
            out_dir,
            policy_path,
            model_name=DEFAULT_EMBEDDING_MODEL,
        )

    return BootstrapResult(
        total_commits_scanned=total_scanned,
        candidates_found=len(candidates),
        decisions_written=decisions_written,
        out_dir=out_dir,
        decisions=candidates,
        dry_run=dry_run,
    )
