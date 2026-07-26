from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .packs import MAX_PACK_BYTES, PackError, parse_pack_bytes
from .skills import SkillPack, reject_executable_payload

# Candidate intake and static analysis: the first stage of the gate a skill passes before anyone can
# approve it.
#
# THE CENTRAL DESIGN DECISION, and the reason this module is shaped the way it is: `AnalysisReport`
# has NO field for "safe", "passed", "risk", or "severity". A verdict is not merely withheld here --
# it is INEXPRESSIBLE, the same discipline that makes "allow everything" unwritable in a permission
# envelope. Everything this module produces is either a hard structural failure (raised) or a
# reviewer ADVISORY (recorded, and requiring explicit acknowledgment at approval time).
#
# Why so firm: a skill is prose meant to persuade a model. Whether prose induces an agent to
# exfiltrate a secret is a semantic question about a system that has not run yet, and no amount of
# pattern matching answers it. A report that said "no findings: safe" would be worse than no report,
# because it would move the reviewer's attention away from the only thing that actually protects
# them -- the permission envelope and their own reading of the text. Pattern findings here mean
# "a human should look at this", never "this is dangerous", and never their opposite.

MAX_CANDIDATE_BYTES = MAX_PACK_BYTES


class CandidateError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Advisory:
    """A reason for a human to look closer. Deliberately carries no severity: ranking findings would
    imply a scale this module has no basis to place them on, and reviewers skip low-ranked items."""

    code: str
    detail: str
    # The skill this came from, as a field rather than something to be parsed back out of `detail`.
    # Acknowledgment at approval time is PER FINDING, so each one needs a stable identity, and
    # deriving that identity by splitting prose would break the first time a detail string changed.
    skill_id: str = ""

    @property
    def identifier(self) -> str:
        return f"{self.skill_id}:{self.code}"


@dataclass(frozen=True)
class AnalysisReport:
    """The result of static analysis. Note what is absent: no verdict, no score, no boolean.

    A caller that wants to know whether a candidate may be approved has to read the advisories and
    decide, which is the entire point."""

    digest: str
    pack_id: str
    version: str
    skill_ids: tuple[str, ...]
    advisories: tuple[Advisory, ...]


@dataclass(frozen=True)
class CandidateRecord:
    path: Path
    digest: str
    pack_id: str
    version: str


# Patterns are matched against the pack's own declared PROSE. They are signals for a reviewer, in
# the reviewer's own words, and each one names what to go look at rather than what to conclude.
_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("mentions_credential_path", "references a path where credentials are usually kept",
     re.compile(r"~/\.ssh|~/\.aws|~/\.gnupg|\.env\b|id_rsa|credentials\.json", re.IGNORECASE)),
    ("mentions_shell_profile", "references a shell profile, which persists across sessions",
     re.compile(r"\.bashrc|\.zshrc|\.bash_profile|\.profile\b", re.IGNORECASE)),
    ("mentions_network_tool", "names a tool that moves data off the machine",
     re.compile(r"\bcurl\b|\bwget\b|\bnc\b|\bscp\b|\brsync\b", re.IGNORECASE)),
    ("mentions_encoding_and_transfer", "mentions encoding together with transfer, a common shape "
     "for moving data out without it being readable in transit",
     re.compile(r"base64.{0,80}(upload|post|send|http)|\b(upload|post|send)\b.{0,80}base64",
                re.IGNORECASE | re.DOTALL)),
    ("instructs_the_reader_to_ignore_guidance", "contains override phrasing aimed at the agent "
     "rather than at the task",
     re.compile(r"ignore (all )?(previous|prior|above)|disregard .{0,20}instruction|"
                r"do not tell|without (telling|informing)", re.IGNORECASE)),
)


def ingest_candidate(source: Path, data_dir: Path) -> CandidateRecord:
    """Copy a candidate pack into the private candidate directory, addressed by its own digest.

    Content-addressed on purpose: ingesting the same bytes twice is idempotent and cannot produce two
    records that disagree, and the filename is derived from the content rather than from an
    attacker-chosen name. Ingest does NOT validate beyond what is needed to name the file -- analysis
    is a separate, explicit step, so a candidate that fails analysis still exists to be inspected."""
    raw = _read_candidate(source)
    digest = hashlib.sha256(raw).hexdigest()
    pack = _parse(raw, source.suffix.lower())
    directory = data_dir / "skills" / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}.json"
    if not destination.exists():
        _write_private(destination, raw)
    return CandidateRecord(destination, f"sha256:{digest}", pack.pack_id, pack.version)


def analyze_candidate(path: Path) -> AnalysisReport:
    """Run deterministic, structural analysis over a candidate.

    Structural failures RAISE: they mean the candidate is not a well-formed skill pack at all, and
    there is nothing for a reviewer to weigh. Everything survivable is an advisory. The distinction
    matters -- collapsing the two would either hide malformed input among advisories or present
    prose findings with the authority of a schema error."""
    raw = _read_candidate(path)
    _require_utf8(raw)
    pack = _parse(raw, path.suffix.lower())
    _reject_control_characters(pack)
    return AnalysisReport(
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        pack_id=pack.pack_id,
        version=pack.version,
        skill_ids=tuple(skill.skill_id for skill in pack.skills),
        advisories=_advisories(pack),
    )


def _read_candidate(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CandidateError("candidate_unreadable") from error
    if len(raw) > MAX_CANDIDATE_BYTES:
        raise CandidateError("candidate_too_large")
    return raw


def _require_utf8(raw: bytes) -> None:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CandidateError("candidate_not_utf8") from error


def _reject_control_characters(pack: SkillPack) -> None:
    """Refuse invisible control characters outside tab/newline/carriage return.

    Text that renders differently to a reviewer than it does to a model is the whole problem: a
    zero-width or bidirectional-override character lets approved-looking prose carry instructions the
    approver never saw. A hard failure, not an advisory, because no legitimate skill needs them.

    Checked on the PARSED VALUES, never on the raw bytes. JSON escapes such characters as ASCII
    `\\u202e`, so a byte-level scan sees seven harmless characters while the parsed string contains
    the real one -- the check would have looked thorough and caught nothing. Found by a test that
    built its fixture with `json.dumps`, which is exactly how a real candidate would arrive."""
    for value in _strings(pack.model_dump(mode="json")):
        for character in value:
            if character in "\t\n\r":
                continue
            if unicodedata.category(character) in {"Cc", "Cf"}:
                raise CandidateError("candidate_contains_control_characters")


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _strings(entry)]
    if isinstance(value, list):
        return [item for entry in value for item in _strings(entry)]
    return []


def _parse(raw: bytes, suffix: str) -> SkillPack:
    try:
        data = parse_pack_bytes(raw, suffix)
        reject_executable_payload(data)
        return SkillPack.model_validate_json(json.dumps(data))
    except PackError as error:
        raise CandidateError(error.code) from error
    except ValueError as error:
        # `malformed_pack`, not a candidate-specific code: this is the same failure `load_skill_pack`
        # reports for the same input, and a second name for one condition only makes an operator
        # learn two words for it.
        raise CandidateError("malformed_pack") from error


def _advisories(pack: SkillPack) -> tuple[Advisory, ...]:
    """Scan the pack's declared prose. Deterministic and order-stable: same pack, same advisories."""
    found: list[Advisory] = []
    for skill in pack.skills:
        haystack = "\n".join([skill.summary, skill.provenance, skill.body_locator,
                              *skill.advisories, *skill.limitations])
        for code, detail, pattern in _PATTERNS:
            if pattern.search(haystack):
                found.append(Advisory(code=code, detail=detail, skill_id=skill.skill_id))
    return tuple(found)


def _write_private(destination: Path, raw: bytes) -> None:
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


__all__ = [
    "MAX_CANDIDATE_BYTES",
    "Advisory",
    "AnalysisReport",
    "CandidateError",
    "CandidateRecord",
    "analyze_candidate",
    "ingest_candidate",
]
