from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, model_validator

from . import __version__ as _ROUTER_VERSION
from .packs import (
    ClosedModel,
    Identifier,
    PackError,
    Text,
    Version,
    check_review_window,
    check_router_compatibility,
    check_version_floor,
    encode_pack,
    parse_pack_bytes,
    read_pack_bytes,
    verify_manifest_bytes,
)

# Skill records for the Skills Layer. A skill is a POINTER plus a policy: Bruriah decides WHICH
# skill applies and discloses its identity, provenance, and permission envelope, but the skill body
# never traverses the MCP tool surface -- the host loads it through its own trust path. That is why
# there is no body field here, only a locator and the digest the approval is bound to.
#
# These are siblings of `DomainPack`/`CapabilityPolicy`, not extensions of them: `DomainPack` is a
# closed model at `schema_version "1"` pinned by the existing suite, and `CapabilityPolicy` declares
# permissions as free text, which is descriptive rather than enforceable. Nothing in `packs.py`
# changes shape here; only its verification primitives are reused.
#
# v1 is PROSE-ONLY by approved scope revision: `payload` is `Literal["prose"]`, so an executable
# payload is not expressible and the sandbox stage is unreachable rather than faked.

Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Hostname = Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")]
# Character whitelist only. The structural rules (no absolute path, no `..` segment, no empty or
# dot segment) are enforced in `FilesystemAccess.safe_paths` rather than in the pattern, because
# pydantic v2 compiles `pattern` with a regex engine that has no lookahead support -- a pattern
# that appeared to forbid `..` would silently fail to compile or fail to match as intended.
RelPath = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._/-]+$")]
Tier = Literal["first_party", "third_party", "local"]
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})
class FilesystemAccess(ClosedModel):
    read_paths: list[RelPath] = []
    write_paths: list[RelPath] = []
    @model_validator(mode="after")
    def safe_paths(self) -> "FilesystemAccess":
        for value in [*self.read_paths, *self.write_paths]:
            if value.startswith("/") or value.endswith("/"):
                raise ValueError("unsafe_path")
            if any(segment in _FORBIDDEN_SEGMENTS for segment in value.split("/")):
                raise ValueError("unsafe_path")
        return self
class NetworkAccess(ClosedModel):
    # Exact hosts only. A wildcard is not expressible because `*` is outside `Hostname`'s character
    # class, so "allow everything" cannot be declared even by a signed pack.
    hosts: list[Hostname] = []
    schemes: list[Literal["https"]] = []
class SubprocessAccess(ClosedModel):
    # Basenames, never a shell string: `Identifier` admits no space, slash, pipe, or semicolon.
    programs: list[Identifier] = []
class PermissionEnvelope(ClosedModel):
    """Default-deny by ABSENCE: every dimension defaults to an empty collection and empty means
    deny. There is no "allow all" token in any dimension, so a broader grant cannot be written."""
    filesystem: FilesystemAccess = FilesystemAccess()
    network: NetworkAccess = NetworkAccess()
    subprocess: SubprocessAccess = SubprocessAccess()
    secrets: list[Identifier] = []  # named handles the host resolves; never secret values
    def grants_nothing(self) -> bool:
        return not (
            self.filesystem.read_paths
            or self.filesystem.write_paths
            or self.network.hosts
            or self.network.schemes
            or self.subprocess.programs
            or self.secrets
        )
class SkillPolicy(ClosedModel):
    skill_id: Identifier
    version: Version
    tier: Tier
    payload: Literal["prose"]
    summary: Text
    # Classifier-vocabulary domains. Skill surfacing is domain-gated in addition to claim-type
    # gated, which is what `CapabilityPolicy` cannot express (it has no domain field).
    domains: list[Identifier]
    # Where the HOST finds the body. Bruriah never reads or returns it; this is what an
    # `install_skill` host action points at and what divergence detection compares against.
    body_locator: Text
    body_digest: Digest
    permissions: PermissionEnvelope = PermissionEnvelope()
    provenance: Text
    license: Text
    advisories: list[Text] = []
    limitations: list[Text] = []
    @model_validator(mode="after")
    def prose_only_envelope(self) -> "SkillPolicy":
        """A prose skill is instructions for a model, so a sandbox cannot verify its behaviour. The
        only honest posture is to make the dangerous dimensions inexpressible: no network, no
        subprocess, no secrets, no filesystem WRITE. Read paths stay allowed because instructing an
        agent to consult a file grants nothing the host has not already granted."""
        if not self.domains:
            raise ValueError("empty_required_registry")
        envelope = self.permissions
        if envelope.network.hosts or envelope.network.schemes:
            raise ValueError("prose_skill_declares_network")
        if envelope.subprocess.programs:
            raise ValueError("prose_skill_declares_subprocess")
        if envelope.secrets:
            raise ValueError("prose_skill_declares_secrets")
        if envelope.filesystem.write_paths:
            raise ValueError("prose_skill_declares_filesystem_write")
        return self
class SkillPack(ClosedModel):
    schema_version: Literal["1"]
    pack_id: Identifier
    version: Version
    maintainer: Text
    min_router_version: Version
    max_router_version: Version
    # Currency is declared at pack level, matching `DomainPack`. Per-skill review windows would be
    # finer-grained for third-party skills whose upstreams move independently, but v1 keeps one
    # window per pack; demotion therefore happens at pack granularity.
    reviewed_at: date
    expires_at: date
    freshness_days: Annotated[int, Field(ge=1, le=3650)]
    license: Text
    provenance: Text
    skills: list[SkillPolicy]
    @model_validator(mode="after")
    def coherent(self) -> "SkillPack":
        if not self.skills:
            raise ValueError("empty_required_registry")
        if self.expires_at <= self.reviewed_at:
            raise ValueError("invalid_review_window")
        identifiers = [skill.skill_id for skill in self.skills]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("duplicate_skill_id")
        return self
def pack_currency(pack: "SkillPack", today: date) -> str:
    """`current`, `stale`, or `expired` for a loaded pack.

    The same three words `EvidenceRecord.freshness` already uses, so a demoted skill reports its
    state in the vocabulary the contract has always spoken rather than in a new one."""
    if pack.expires_at < today:
        return "expired"
    if pack.reviewed_at + timedelta(days=pack.freshness_days) < today:
        return "stale"
    return "current"


@dataclass(frozen=True)
class SkillSet:
    """Frozen, deterministically ordered set of loaded skill packs. Mirrors `registries.Registry`:
    same construction discipline, same collision codes, same sort order, so there is one way to
    build a trusted collection in this codebase rather than two."""
    packs: tuple[SkillPack, ...]
    skills: tuple[SkillPolicy, ...]
    @classmethod
    def from_packs(cls, packs: list[SkillPack]) -> "SkillSet":
        ordered = tuple(sorted(packs, key=lambda item: item.pack_id))
        if len({item.pack_id for item in ordered}) != len(ordered):
            raise PackError("duplicate_pack_id")
        seen: set[str] = set()
        for pack in ordered:
            for skill in pack.skills:
                if skill.skill_id in seen:
                    raise PackError("duplicate_skill_id")
                seen.add(skill.skill_id)
        skills = tuple(
            item for pack in ordered for item in sorted(pack.skills, key=lambda value: value.skill_id)
        )
        return cls(ordered, skills)
    @property
    def pack_ids(self) -> tuple[str, ...]:
        return tuple(item.pack_id for item in self.packs)
    @property
    def skill_ids(self) -> tuple[str, ...]:
        return tuple(item.skill_id for item in self.skills)
    def resolve(self, skill_id: object) -> SkillPolicy | None:
        """Exact match only. Absent means `None`, never a fabricated or nearest-match record -- the
        loaded set is the sole source of truth, exactly as `resolve_capability` behaves."""
        if not isinstance(skill_id, str):
            raise PackError("invalid_ref_type")
        for skill in self.skills:
            if skill.skill_id == skill_id:
                return skill
        return None
ManifestReader = Callable[[], bytes]
def load_skill_pack_bytes(
    raw: bytes,
    suffix: str,
    manifest: ManifestReader | None,
    trust_roots: dict[str, str],
    *,
    today: date | None = None,
    router_version: str = _ROUTER_VERSION,
    minimum_versions: dict[str, str] | None = None,
    allow_unsigned_local: bool = False,
    enforce_currency: bool = True,
) -> SkillPack:
    """Verify a skill pack already held in memory, failing closed on every gate.

    Check order deliberately mirrors `load_pack`, and every verification primitive is the SAME
    function `load_pack` calls -- there is one Ed25519/digest/hardened-parsing path in this codebase,
    not two that can drift apart. The order is: parse -> executable payload -> schema -> signature ->
    review window -> router compatibility -> version floor. The size ceiling belongs to whoever
    produced `raw`; `load_skill_pack` applies it through `read_pack_bytes`.

    `manifest` is a READER rather than bytes so an unreadable manifest is still discovered at the
    same point in the order as before -- after parsing and schema validation. Reading it eagerly here
    would silently promote `malformed_manifest` ahead of `malformed_pack` and `payload_unsupported`,
    which is exactly the kind of invisible precedence change the `packs.py` extraction took a
    dedicated test block to avoid.

    `payload_unsupported` is raised before schema validation so an unsupported capability reports
    itself instead of surfacing as a generic `malformed_pack`, which would read as a typo. There are
    no domain or jurisdiction gates here: skills are domain-gated at dispatch, not at load."""
    data = parse_pack_bytes(raw, suffix)
    reject_executable_payload(data)
    encoded = encode_pack(data)
    try:
        pack = SkillPack.model_validate_json(encoded)
    except (ValidationError, ValueError) as error:
        raise PackError("malformed_pack") from error
    if manifest is None:
        if not allow_unsigned_local:
            raise PackError("signature_required")
        # Unsigned is a LOCAL-ONLY affordance. A first- or third-party skill without a signature has
        # no provenance at all, so it must not ride in on the local-pack exception.
        if any(skill.tier != "local" for skill in pack.skills):
            raise PackError("unsigned_nonlocal_pack")
    else:
        try:
            manifest_raw = manifest()
        except OSError as error:
            raise PackError("malformed_manifest") from error
        verify_manifest_bytes(raw, manifest_raw, trust_roots, pack.pack_id, pack.version)
    now = today or date.today()
    # `enforce_currency=False` is for the SERVING path only, where an aged pack must demote its own
    # skills rather than take the server down with it -- an expiry date is a statement about how
    # stale the review is, and answering it by refusing to start is a far worse outcome than
    # answering it by refusing to dispatch. Activation keeps the gate: you do not put expired
    # content into service, you only keep serving content that expired while in service.
    if enforce_currency:
        check_review_window(pack.reviewed_at, pack.expires_at, pack.freshness_days, now)
    check_router_compatibility(pack.min_router_version, pack.max_router_version, router_version)
    check_version_floor(pack.pack_id, pack.version, minimum_versions)
    return pack
def load_skill_pack(
    pack_path: Path,
    manifest_path: Path | None,
    trust_roots: dict[str, str],
    *,
    today: date | None = None,
    router_version: str = _ROUTER_VERSION,
    minimum_versions: dict[str, str] | None = None,
    allow_unsigned_local: bool = False,
    enforce_currency: bool = True,
) -> SkillPack:
    """Load and verify a skill pack from disk. The ceiling-checked read is the only step that needs a
    path; `manifest_path.read_bytes` is passed unevaluated so the manifest is touched at the same
    point in the check order as before, never earlier."""
    raw = read_pack_bytes(pack_path)
    return load_skill_pack_bytes(
        raw,
        pack_path.suffix.lower(),
        None if manifest_path is None else manifest_path.read_bytes,
        trust_roots,
        today=today,
        router_version=router_version,
        minimum_versions=minimum_versions,
        allow_unsigned_local=allow_unsigned_local,
        enforce_currency=enforce_currency,
    )
def reject_executable_payload(data: dict) -> None:
    """A candidate declaring a non-prose payload is REFUSED with its own code, never silently
    dropped or coerced to prose. Closed-model validation alone would report `malformed_pack`, which
    would read as a schema typo rather than an unsupported capability."""
    for skill in data.get("skills", []) if isinstance(data, dict) else []:
        if isinstance(skill, dict) and skill.get("payload") not in (None, "prose"):
            raise PackError("payload_unsupported")
__all__ = [
    "Digest",
    "FilesystemAccess",
    "Hostname",
    "ManifestReader",
    "NetworkAccess",
    "PermissionEnvelope",
    "RelPath",
    "SkillPack",
    "SkillPolicy",
    "SkillSet",
    "SubprocessAccess",
    "Tier",
    "load_skill_pack",
    "pack_currency",
    "load_skill_pack_bytes",
    "reject_executable_payload",
]
