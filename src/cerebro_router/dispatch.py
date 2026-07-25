from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .contracts import HostSkill
from .lookup import LookupResult, SkillMatch

# Pure, bounded skill dispatch: decide WHICH vetted skills apply to a request and what the host's
# copy of each one looks like. No I/O, no model, no corpus input, no scoring.
#
# The one property this module exists to protect: SELECTION IS NOT INFLUENCED BY ANYTHING UNTRUSTED.
# The candidate set comes from `LookupResult.skills`, which is closed-vocabulary domain membership
# over a signed skill set (see `lookup._domain_applicable_skills`). Ordering and the ceiling are
# applied BEFORE the host inventory is consulted, so a host that misreports what it has installed
# can change how availability is DESCRIBED but never which skills are selected, nor their order, nor
# which ones fall past the ceiling. That ordering of operations is the invariant; it is tested
# directly rather than left as a property of how the code happens to read.
#
# Domain gating is not re-implemented here. `lookup` already performs it, and a second membership
# test would be a second place for the two vocabularies to drift.

DEFAULT_SKILL_CEILING = 5
# Assumed, NOT measured -- stated plainly because the design records it as an open question and
# inheriting an unexamined constant silently is how assumptions become facts. It is deliberately not
# a `Budgets` field: `Budgets` is echoed back in `InvestigationResult`, so adding one would change
# the response every pre-skills client receives, which is precisely the compatibility guarantee the
# contract gate was built to hold. Skills are also bounded a second time downstream by the existing
# `max_evidence` budget, which the client does declare; this ceiling only stops skills from crowding
# every other kind of evidence out of that budget.

Availability = Literal["installed", "not_installed", "digest_divergent"]


@dataclass(frozen=True)
class SkillDispatch:
    """One selected skill plus what the HOST reports about its copy.

    `availability` is a claim about the host, never about the skill's validity: `digest_divergent`
    means the installed body does not match the digest the approval is bound to, so the local copy is
    unapproved -- not that the skill itself is bad."""

    skill: SkillMatch
    availability: Availability
    host_version: str | None = None


@dataclass(frozen=True)
class DispatchResult:
    skills: tuple[SkillDispatch, ...]
    gaps: tuple[str, ...] = ()


def dispatch(
    lookup: LookupResult,
    host_skills: Sequence[HostSkill],
    *,
    ceiling: int = DEFAULT_SKILL_CEILING,
) -> DispatchResult:
    """Order, bound, and annotate the skills a lookup already matched.

    Takes the `LookupResult` rather than re-deriving matches from a skill set: domain gating lives in
    `lookup` and duplicating it here would create a second place for the classifier and pack
    vocabularies to drift apart.

    Excess past `ceiling` is reported as a typed gap carrying the number dropped. Silently truncating
    would let a host believe it had seen every applicable skill, which is worse than returning fewer
    and saying so."""
    if ceiling < 0:
        raise ValueError("invalid_skill_ceiling")

    # Deterministic order and the ceiling FIRST, both computed only from the signed set.
    ordered = sorted(lookup.skills, key=lambda item: item.skill.skill_id)
    selected, dropped = ordered[:ceiling], ordered[ceiling:]

    # Only now is the untrusted host inventory consulted, and only to describe what it has.
    installed = _by_skill_id(host_skills)
    skills = tuple(
        SkillDispatch(
            skill=match,
            availability=_availability(match, installed.get(match.skill.skill_id)),
            host_version=getattr(installed.get(match.skill.skill_id), "version", None),
        )
        for match in selected
    )
    gaps = (f"skill_ceiling_exceeded:{len(dropped)}",) if dropped else ()
    return DispatchResult(skills=skills, gaps=gaps)


def _by_skill_id(host_skills: Sequence[HostSkill]) -> dict[str, HostSkill]:
    """First entry wins on a duplicate id. A host reporting the same skill twice is malformed input,
    and picking the first is deterministic; picking the "best" would let a host improve its own
    reported state by repeating itself."""
    seen: dict[str, HostSkill] = {}
    for entry in host_skills:
        seen.setdefault(entry.skill_id, entry)
    return seen


def _availability(match: SkillMatch, installed: HostSkill | None) -> Availability:
    """Compare the host's copy against the digest approval is bound to.

    Version is NOT part of this comparison. The digest is what human approval was granted against, so
    a matching digest under a different version string is still the approved bytes, and a matching
    version with a different digest is emphatically not. Trusting the version label instead would let
    a host claim approval by renaming."""
    if installed is None:
        return "not_installed"
    return "installed" if installed.digest == match.skill.body_digest else "digest_divergent"


__all__ = [
    "DEFAULT_SKILL_CEILING",
    "Availability",
    "DispatchResult",
    "SkillDispatch",
    "dispatch",
]
