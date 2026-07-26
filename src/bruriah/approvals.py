from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .candidates import AnalysisReport, CandidateError, analyze_candidate

# Human approval: the second of the two STRONG axes in this project's trust model, the other being
# the permission envelope. A signature says who published a skill; only this says a person read it.
#
# Two properties this module exists to hold, both enforced rather than documented:
#
# 1. APPROVAL IS BOUND TO A DIGEST, never to a name or a version. `load_approvals` returns
#    `skill_id -> body_digest`, which `skillset.validate_skillset` re-checks at every activation.
#    Editing an approved body does not merely invalidate a record filed away somewhere; it makes the
#    whole skill set fail to activate.
#
# 2. EVERY ADVISORY MUST BE ACKNOWLEDGED INDIVIDUALLY. Not a count, not a blanket flag, not "I read
#    them all" -- each finding by its own identifier. A single "yes" over a list is the interface
#    equivalent of a checkbox nobody reads, and it is exactly the affordance that turns a review gate
#    into a formality.
#
# Approval RE-RUNS the analysis from the candidate file rather than accepting a report from the
# caller. A report passed in is a report that can be fabricated, and the one place that must not be
# possible is the gate that grants trust.

_APPROVALS_DIRNAME = "approvals"


class ApprovalError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ApprovalRecord:
    """One approved skill. Deliberately records what was acknowledged, not a conclusion about it:
    there is no `safe` here either, for the same reason `AnalysisReport` has none."""

    skill_id: str
    body_digest: str
    candidate_digest: str
    acknowledged: tuple[str, ...]
    approved_on: date

    def as_json(self) -> dict[str, object]:
        return {
            "skill_id": self.skill_id, "body_digest": self.body_digest,
            "candidate_digest": self.candidate_digest, "acknowledged": list(self.acknowledged),
            "approved_on": self.approved_on.isoformat(),
        }


def approve_candidate(
    candidate: Path, data_dir: Path, *, acknowledge: Iterable[str], today: date,
) -> tuple[ApprovalRecord, ...]:
    """Approve every skill in a candidate, binding each to its current body digest.

    Re-runs static analysis from the file itself. Refuses unless every advisory it produces is
    acknowledged by identifier, and refuses just as firmly if acknowledgments are offered for
    findings that do not exist -- a stale acknowledgment list means the reviewer approved a
    different candidate than the one in front of them, which is precisely the case where waving it
    through is worst.

    `today` is injected, never read from the clock, so an approval record is reproducible."""
    try:
        report = analyze_candidate(candidate)
    except CandidateError as error:
        raise ApprovalError(error.code) from error

    required = {item.identifier for item in report.advisories}
    offered = set(acknowledge)
    if required - offered:
        raise ApprovalError("unacknowledged_advisories")
    if offered - required:
        raise ApprovalError("unknown_acknowledgment")

    records = tuple(
        ApprovalRecord(
            skill_id=skill_id,
            body_digest=digest,
            candidate_digest=report.digest,
            acknowledged=tuple(sorted(identifier for identifier in required
                                      if identifier.startswith(f"{skill_id}:"))),
            approved_on=today,
        )
        for skill_id, digest in _body_digests(candidate).items()
    )
    directory = data_dir / "skills" / _APPROVALS_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    for record in records:
        _write_private(directory / f"{record.skill_id}.json", record)
    return records


def load_approvals(data_dir: Path) -> dict[str, str]:
    """Return `skill_id -> body_digest` for `skillset.validate_skillset`.

    An unreadable or malformed record is a hard failure, never a skipped entry. Silently dropping one
    would turn a corrupted approval into an unapproved skill, and an unapproved skill fails
    activation with a message about the wrong thing entirely."""
    directory = data_dir / "skills" / _APPROVALS_DIRNAME
    if not directory.is_dir():
        return {}
    approvals: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        record = read_approval(path)
        approvals[record.skill_id] = record.body_digest
    return approvals


def read_approval(path: Path) -> ApprovalRecord:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ApprovalRecord(
            skill_id=data["skill_id"], body_digest=data["body_digest"],
            candidate_digest=data["candidate_digest"],
            acknowledged=tuple(data["acknowledged"]),
            approved_on=date.fromisoformat(data["approved_on"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ApprovalError("malformed_approval") from error


def revoke_approval(data_dir: Path, skill_id: str) -> bool:
    """Withdraw an approval. Returns whether one existed. Revocation is a first-class operation: a
    reviewer who changes their mind must not have to hand-delete a file to act on it."""
    path = data_dir / "skills" / _APPROVALS_DIRNAME / f"{skill_id}.json"
    if not path.is_file():
        return False
    path.unlink()
    return True


def _body_digests(candidate: Path) -> dict[str, str]:
    from .candidates import _parse, _read_candidate  # local: internal reuse, not a public seam

    pack = _parse(_read_candidate(candidate), candidate.suffix.lower())
    return {skill.skill_id: skill.body_digest for skill in pack.skills}


def _write_private(destination: Path, record: ApprovalRecord) -> None:
    payload = (json.dumps(record.as_json(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        handle.write(payload)
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
    "ApprovalError",
    "ApprovalRecord",
    "approve_candidate",
    "load_approvals",
    "read_approval",
    "revoke_approval",
]
