#!/usr/bin/env python3
"""Turn a repository's decision record into a corpus Bruriah can index.

This lives in the package, not in scripts/, because it is step ONE of the documented
workflow: a reader who installs the wheel has no scripts/ directory, and for a while the
front page opened by telling them to run a file they did not have.

A project's reasoning already exists: it is in the commit messages that explain WHY, written by the
person deciding at the moment of deciding. This reads that history and writes one Markdown document
per decision, so `bruriah index` can take it from there.

    bruriah corpus --repo . --out ./corpus
    bruriah index --corpus-root ./corpus --policy ./policy.yaml

Commits with no explanatory body are skipped: a subject line records what changed, never why, and
including them adds noise without reasoning. Merge commits are skipped for the same reason.

Nothing is written to the repository being read. This script only reads.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# git's own escapes, NOT literal control bytes: a process argument cannot contain a NUL, so
# building the format string with real \x00 raises ValueError before git is ever invoked. git
# expands these itself, and the output is then split on the real bytes.
_SEPARATOR_FMT, _RECORD_FMT = "%x00", "%x01"
_SEPARATOR, _RECORD = "\x00", "\x01"
_MAX_BODY = 16_000


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, check=True
        )
    except FileNotFoundError:
        raise SystemExit("error: git is not on PATH")
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"error: git failed: {error.stderr.strip() or error}")
    return result.stdout


def _require_commit(repo: Path, revision: str) -> None:
    """Fail on an unresolvable revision BEFORE any document is written, and say which one.

    `git log` on an unknown revision reports `fatal: ambiguous argument`, which names the string but
    not the thing the caller was trying to do; and a pinned revision fails for one mundane reason
    far more often than any other -- a shallow clone simply does not have the object. So the failure
    says the revision and says that, in this module's one error convention (`SystemExit`), rather
    than leaking a CalledProcessError to a caller that has no idea a subprocess was involved.

    `--quiet` silences only the unknown-revision complaint, so anything git still writes to stderr
    is a problem with the repository rather than with the revision -- that one is reported as it
    always was, because telling someone their sha is missing from a directory that is not a git
    checkout at all sends them looking in the wrong place."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        raise SystemExit("error: git is not on PATH")
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        if detail:
            raise SystemExit(f"error: git failed: {detail}")
        raise SystemExit(
            f"error: revision {revision!r} is not a commit in {repo.resolve()}. "
            "A shallow clone is the usual reason -- fetch the full history."
        )


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:60].strip("-") or "untitled"


@dataclass(frozen=True)
class CorpusResult:
    written: int
    # Non-merge commits read. Merges are excluded by `--no-merges` before anything is counted, so
    # they are not in the denominator: they are skipped for being merges, not for lacking
    # reasoning, and counting them would understate a history that is actually well written.
    examined: int


def build(
    repo: Path, out: Path, limit: int | None = None, *, revision: str = "HEAD"
) -> CorpusResult:
    """Write one document per commit that carries reasoning.

    Returns what was written AND what was read, because the gap between them is the single thing
    that decides whether any of this is worth installing -- and until it was returned, the caller
    could only see the numerator. A history that yields three documents from three commits and one
    that yields three from three hundred are the same number on the way out.

    `revision` names the point in history to derive from. It defaults to `HEAD`, which is what
    anyone building a corpus of their own project wants -- but a corpus derived from a moving HEAD
    cannot reproduce a published measurement, because this repository's own history IS the corpus
    the eval measures: every commit added after the number was published changes the IDF the number
    was computed against. Naming the revision is the whole of what makes such a figure checkable by
    a reader, who otherwise runs the documented command and gets a different corpus than the table
    below it describes, with nothing on the page to tell them why."""
    _require_commit(repo, revision)
    fmt = (
        _SEPARATOR_FMT.join([
            "%H",
            "%aI",
            "%an",
            "%s",
            "%(trailers:key=Supersedes,valueonly=true)",
            "%(trailers:key=Deprecates,valueonly=true)",
            "%(trailers:key=Amends,valueonly=true)",
            "%(trailers:key=Alternative-Rejected,valueonly=true)",
            "%(trailers:key=Rejection-Reason,valueonly=true)",
            "%(trailers:key=Premise,valueonly=true)",
            "%(trailers:key=Premise-Invalidated,valueonly=true)",
            "%b",
        ])
        + _RECORD_FMT
    )
    args = ["log", "--no-merges", f"--format={fmt}"]
    if limit:
        args.append(f"-{limit}")
    args.append(revision)
    out.mkdir(parents=True, exist_ok=True)

    written = examined = 0
    for entry in _git(repo, *args).split(_RECORD):
        parts = entry.strip("\n").split(_SEPARATOR)
        if len(parts) < 12:
            continue
        (
            sha, when, author, subject, raw_supersedes, raw_deprecates, raw_amends,
            raw_alt_rejected, raw_rejection_reason, raw_premise, raw_premise_inv, body,
        ) = (part.strip() for part in parts[:12])
        examined += 1
        if not body:
            continue  # a subject records what changed, never why
        files = _git(repo, "show", "--stat", "--format=", "--name-only", sha).split()

        def _clean_hashes(raw: str) -> list[str]:
            return [it.strip().lower() for it in raw.replace(",", " ").split() if it.strip()]

        supersedes = _clean_hashes(raw_supersedes)
        deprecates = _clean_hashes(raw_deprecates)
        amends = _clean_hashes(raw_amends)
        alt_rejected = [line.strip() for line in raw_alt_rejected.splitlines() if line.strip()]
        rejection_reasons = [line.strip() for line in raw_rejection_reason.splitlines() if line.strip()]
        raw_premises = [line.strip() for line in raw_premise.splitlines() if line.strip()]
        premise_invalidated = [line.strip() for line in raw_premise_inv.splitlines() if line.strip()]

        frontmatter_lines = ["---", f"commit: {sha}"]
        if supersedes:
            frontmatter_lines.append("supersedes:")
            frontmatter_lines.extend(f"  - {item}" for item in supersedes)
        if deprecates:
            frontmatter_lines.append("deprecates:")
            frontmatter_lines.extend(f"  - {item}" for item in deprecates)
        if amends:
            frontmatter_lines.append("amends:")
            frontmatter_lines.extend(f"  - {item}" for item in amends)
        if alt_rejected:
            frontmatter_lines.append("alternatives:")
            premise_ids = [p.split("|")[0].strip() for p in raw_premises]
            for idx, alt in enumerate(alt_rejected):
                reason = (
                    rejection_reasons[idx]
                    if idx < len(rejection_reasons)
                    else (rejection_reasons[0] if rejection_reasons else "")
                )
                frontmatter_lines.append(f"  - name: {alt}")
                frontmatter_lines.append("    disposition: rejected")
                if reason:
                    frontmatter_lines.append(f"    reason: {reason}")
                if premise_ids:
                    frontmatter_lines.append("    premises:")
                    for pid in premise_ids:
                        frontmatter_lines.append(f"      - {pid}")
        if raw_premises:
            frontmatter_lines.append("premises:")
            for p in raw_premises:
                parts_p = p.split("|", 1)
                pid = parts_p[0].strip()
                stmt = parts_p[1].strip() if len(parts_p) > 1 else pid
                frontmatter_lines.append(f"  - id: {pid}")
                frontmatter_lines.append(f"    statement: {stmt}")
                frontmatter_lines.append("    status: active")
        if premise_invalidated:
            frontmatter_lines.append("invalidated_premises:")
            for p_inv in premise_invalidated:
                frontmatter_lines.append(f"  - {p_inv}")
        frontmatter_lines.append("---")
        frontmatter = "\n".join(frontmatter_lines) + "\n\n"

        alt_section = ""
        if alt_rejected:
            alt_section = (
                "\n## Evaluated Alternatives\n"
                + "".join(
                    f"- **{alt}** (rejected): "
                    + (rejection_reasons[i] if i < len(rejection_reasons) else "")
                    + "\n"
                    for i, alt in enumerate(alt_rejected)
                )
                + "\n"
            )

        document = (
            frontmatter
            + f"# {subject}\n\n"
            f"**Decided:** {when[:10]} · **Commit:** `{sha[:12]}` · **Author:** {author}\n\n"
            f"{body[:_MAX_BODY]}\n"
            f"{alt_section}\n"
            "## Files this decision touched\n"
            + "".join(f"- `{item}`\n" for item in sorted(set(files))[:12])
        )
        # LF explicitly: these documents are hashed byte-for-byte by `parse_document`
        # (`source_hash`), so letting text mode rewrite the separators would make an index built
        # from a Windows-generated corpus disagree with one built from the same commits anywhere
        # else -- same history, different digests, for no reason a reader could ever see.
        (out / f"{when[:10]}-{sha[:8]}-{_slug(subject)}.md").write_text(
            document, encoding="utf-8", newline="\n"
        )
        written += 1
    return CorpusResult(written, examined)
