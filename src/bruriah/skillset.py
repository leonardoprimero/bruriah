from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from .packs import MAX_PACK_BYTES, ClosedModel, PackError, encode_pack, parse_pack_bytes
from .pointer import controlled_file, identity_matches, read_pointer, serialized, write_pointer
from .skills import SkillPack, SkillSet, load_skill_pack_bytes

# Compiled skill-set generations: the immutable on-disk artifact dispatch will read from.
#
# A generation is a SEALED BUNDLE of its source packs, not a rewrite of them. It embeds each pack's
# original bytes and its release manifest verbatim, so `validate_skillset` recomputes every digest and
# re-verifies every Ed25519 signature from the generation alone. That is what makes the lifecycle
# spec's "rollback restores a retained generation WITHOUT rebuilding from source packs" literally
# true: a retained generation stays independently verifiable long after its source files moved or
# changed. The compiled records are DERIVED at validation time rather than stored beside the bytes,
# so the file holds exactly one source of truth and the two can never disagree.
#
# What a generation is NOT: a trust boundary. It lives under `data_dir`, which anyone able to write
# there controls, so validation here defends against corruption, truncation, and partial writes --
# precisely what `index.py` validates a candidate database for. The security gates remain the pack
# signature, the default-deny envelope, and the human approval record, all re-checked below.

MAX_SKILLSET_BYTES = 1_048_576
# Sixteen packs at `MAX_PACK_BYTES` plus base64's 4/3 inflation and manifest overhead leaves room for
# roughly eleven embedded packs. A set that outgrows this is a packaging decision to make explicitly,
# never a ceiling to raise quietly.

Base64Blob = Annotated[
    str, Field(min_length=4, max_length=MAX_SKILLSET_BYTES, pattern=r"^[A-Za-z0-9+/]+={0,2}$")
]


class SkillSetError(ValueError):
    """The single boundary error for the skill-set lifecycle.

    A pack-level failure keeps its exact `PackError` code (`digest_mismatch`, `expired_pack`,
    `unsigned_nonlocal_pack`, ...) rather than being flattened into one generic code, because those
    codes ARE the actionable diagnostic the lifecycle spec asks for. The original exception is always
    chained. Codes owned here do not collide with pack codes."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class GenerationPack(ClosedModel):
    """One source pack, byte-for-byte, plus the manifest that signs it.

    There is deliberately no "this entry was allowed to be unsigned" flag. A redundant flag could
    disagree with the manifest field beside it, so an absent manifest simply IS the unsigned case,
    and the tier invariant (`unsigned_nonlocal_pack`) remains its only gate: an unsigned entry may
    carry local skills and nothing else."""

    format: Literal[".json", ".yaml", ".yml"]
    source: Base64Blob
    manifest: Base64Blob | None = None


class Generation(ClosedModel):
    schema_version: Literal["1"]
    packs: list[GenerationPack]


@dataclass(frozen=True)
class SkillSource:
    """One input to a compile: a pack file and, unless it is a local T3 pack, its release manifest."""

    pack_path: Path
    manifest_path: Path | None = None
    allow_unsigned_local: bool = False


@dataclass(frozen=True)
class ValidatedSkillSet:
    """`build_id` is the sha256 of the generation's canonical bytes, so identical content always
    yields an identical id and the pointer entry pins exactly which bytes were activated."""

    build_id: str
    skill_set: SkillSet


def generation_path(directory: Path) -> Path:
    """Name a fresh generation. Kept out of `compile_skillset` so compiles stay deterministic and
    testable; the CLI supplies the name, mirroring `cli.py`'s candidate naming."""
    return directory / f"skillset-{uuid.uuid4().hex}.json"


def _canonical(packs: list[GenerationPack]) -> bytes:
    return json.dumps(
        {"schema_version": "1", "packs": [item.model_dump(mode="json") for item in packs]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _entry(source: SkillSource, trust_roots: dict[str, str], gates: dict) -> tuple[str, GenerationPack]:
    """Verify one source through the full fail-closed path and capture its bytes verbatim.

    Reading the manifest eagerly is correct HERE, unlike in `load_skill_pack`: compile is not the
    order-sensitive API, and reading each file exactly once removes a window in which the pack could
    change between verification and capture."""
    try:
        raw = source.pack_path.read_bytes()
    except OSError as error:
        raise SkillSetError("pack_unreadable") from error
    if len(raw) > MAX_PACK_BYTES:
        raise SkillSetError("pack_too_large")
    manifest_raw: bytes | None = None
    if source.manifest_path is not None:
        try:
            manifest_raw = source.manifest_path.read_bytes()
        except OSError as error:
            raise SkillSetError("malformed_manifest") from error
    try:
        pack = load_skill_pack_bytes(
            raw,
            source.pack_path.suffix.lower(),
            None if manifest_raw is None else _reader(manifest_raw),
            trust_roots,
            allow_unsigned_local=source.allow_unsigned_local,
            **gates,
        )
    except PackError as error:
        raise SkillSetError(error.code) from error
    return pack.pack_id, GenerationPack(
        format=source.pack_path.suffix.lower(),
        source=base64.b64encode(raw).decode("ascii"),
        manifest=None if manifest_raw is None else base64.b64encode(manifest_raw).decode("ascii"),
    )


def _reader(data: bytes):
    return lambda: data


def compile_skillset(
    sources: Iterable[SkillSource],
    destination: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> ValidatedSkillSet:
    """Compile approved skill packs into one immutable generation at `destination`.

    Every source is verified through the full fail-closed path before it is embedded, and the encoded
    result is then re-validated AS A WHOLE before anything touches disk. A failed compile therefore
    leaves no artifact at all, and a generation that exists is one that has already validated once."""
    if destination.exists():
        raise SkillSetError("generation_exists")
    gates = {"today": today, "router_version": router_version, "minimum_versions": minimum_versions}
    entries = [_entry(source, trust_roots, gates) for source in sources]
    raw = _canonical([pack for _, pack in sorted(entries, key=lambda item: item[0])])
    result = validate_skillset_bytes(raw, trust_roots, approvals, **gates)
    _write_generation(destination, raw)
    return result


def validate_skillset_bytes(
    raw: bytes,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> ValidatedSkillSet:
    """Re-verify a generation from its bytes alone, then derive the skill set it compiles to.

    Nothing is taken on trust from the previous compile: every embedded pack is parsed with the same
    hardened loader, re-validated against the closed schema, its digest recomputed, its signature
    re-checked against the bundled trust roots, and its dates re-checked against an injected `today`
    so freshness never depends on the wall clock."""
    if len(raw) > MAX_SKILLSET_BYTES:
        raise SkillSetError("skillset_too_large")
    try:
        data = parse_pack_bytes(raw, ".json")
        encoded = encode_pack(data)
    except PackError as error:
        raise SkillSetError(error.code) from error
    try:
        generation = Generation.model_validate_json(encoded)
    except (ValidationError, ValueError) as error:
        raise SkillSetError("malformed_skillset") from error
    if not generation.packs:
        raise SkillSetError("empty_skillset")
    gates = {"today": today, "router_version": router_version, "minimum_versions": minimum_versions}
    packs = [_load_entry(entry, trust_roots, gates) for entry in generation.packs]
    identifiers = [pack.pack_id for pack in packs]
    if identifiers != sorted(identifiers):
        # Canonical order is what makes `build_id` an identity for the SET rather than for one
        # particular input ordering. Reject rather than re-sort: a generation whose bytes are not
        # canonical did not come from `compile_skillset`.
        raise SkillSetError("unsorted_generation")
    try:
        skill_set = SkillSet.from_packs(packs)
    except PackError as error:
        raise SkillSetError(error.code) from error
    _check_approvals(skill_set, approvals)
    return ValidatedSkillSet(hashlib.sha256(raw).hexdigest(), skill_set)


def validate_skillset(
    path: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> ValidatedSkillSet:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise SkillSetError("skillset_unreadable") from error
    return validate_skillset_bytes(
        raw,
        trust_roots,
        approvals,
        today=today,
        router_version=router_version,
        minimum_versions=minimum_versions,
    )


def _load_entry(entry: GenerationPack, trust_roots: dict[str, str], gates: dict) -> SkillPack:
    try:
        source = base64.b64decode(entry.source, validate=True)
        manifest = None if entry.manifest is None else base64.b64decode(entry.manifest, validate=True)
    except ValueError as error:
        raise SkillSetError("malformed_skillset") from error
    # The size ceiling travels with the path API through `read_pack_bytes`, so an embedded pack has
    # to be measured explicitly here or a generation would be a way to smuggle an oversized pack past
    # a limit the direct loader enforces.
    if len(source) > MAX_PACK_BYTES:
        raise SkillSetError("pack_too_large")
    try:
        return load_skill_pack_bytes(
            source,
            entry.format,
            None if manifest is None else _reader(manifest),
            trust_roots,
            allow_unsigned_local=entry.manifest is None,
            **gates,
        )
    except PackError as error:
        raise SkillSetError(error.code) from error


def _check_approvals(skill_set: SkillSet, approvals: Mapping[str, str]) -> None:
    """Every skill in an activated set MUST carry a human approval bound to the exact body digest its
    record declares.

    Approval is one of the two strong trust axes; the other is the envelope. A signature says who
    published a skill, never that a human read it. Re-checking the binding at activation, and not
    only when approval is granted, is what makes editing a body invalidate ACTIVATION rather than
    merely invalidating a record nobody consults again."""
    for skill in skill_set.skills:
        approved = approvals.get(skill.skill_id)
        if approved is None:
            raise SkillSetError("skill_not_approved")
        if approved != skill.body_digest:
            raise SkillSetError("approval_digest_divergent")


def _write_generation(destination: Path, raw: bytes) -> None:
    """Private tempfile in the destination directory, fsync'd, then `os.replace`, mirroring
    `index.py`'s candidate build: a generation is never observable half-written, and an interrupted
    compile leaves nothing behind."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, destination)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


# --- activation ----------------------------------------------------------------------------------
# The pointer entries name a generation file and the digest of its bytes. `skillset` is the key whose
# value must stay a bare filename; everything else about the pointer is generic and lives in
# `pointer.py`, shared verbatim with the corpus index rather than reimplemented.

_POINTER_ENTRY_KEYS = frozenset({"skillset", "build_id"})


@dataclass(frozen=True)
class SkillSetActivation:
    """Mirrors `index.ActivationResult` and adds the derived set, so a caller that just promoted does
    not have to validate the same bytes a second time to learn what it activated."""

    path: Path
    build_id: str
    durable: bool
    skill_set: SkillSet


def _read_pointer(pointer: Path) -> dict[str, object]:
    return read_pointer(
        pointer, entry_keys=_POINTER_ENTRY_KEYS, name_key="skillset", error=SkillSetError
    )


def _controlled_file(pointer: Path, name: str) -> tuple[Path, int, tuple[int, int, int, int]]:
    return controlled_file(pointer, name, error=SkillSetError)


def _pointer_entry(path: Path, build_id: str) -> dict[str, str]:
    return {"skillset": path.name, "build_id": build_id}


def _read_descriptor(descriptor: int) -> bytes:
    """Read the generation from the descriptor already confirmed by `controlled_file`, not by
    re-opening the path -- re-opening would reintroduce exactly the swap window that opening under
    `O_NOFOLLOW` closed. `pread` leaves the descriptor's offset untouched, so the later `fsync` and
    identity re-check still see the file the caller opened. The read is bounded so an oversized file
    is detected rather than loaded."""
    return os.pread(descriptor, MAX_SKILLSET_BYTES + 1, 0)


@serialized(1)
def promote_skillset(
    candidate: Path,
    pointer: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
    retain: int = 2,
) -> SkillSetActivation:
    """Validate a candidate generation and swap the active pointer to it, under an exclusive lock.

    Structure mirrors `index.promote_candidate` step for step: confirm the candidate is a regular
    file in the pointer's own directory, validate the bytes behind that confirmed descriptor,
    re-confirm the file's identity so a swap during validation is caught, `fsync`, then write the
    pointer atomically. Every failure raises BEFORE the pointer write, so the previously active
    generation stays byte-identical and fully readable; there is no partial activation because the
    generation file is immutable and unreferenced until the swap.

    `pointer` must be passed positionally -- `serialized(1)` locks `args[1]`."""
    if retain < 1 or candidate.parent.resolve() != pointer.parent.resolve():
        raise SkillSetError("invalid_activation_path")
    path, descriptor, file_identity = _controlled_file(pointer, candidate.name)
    try:
        result = validate_skillset_bytes(
            _read_descriptor(descriptor),
            trust_roots,
            approvals,
            today=today,
            router_version=router_version,
            minimum_versions=minimum_versions,
        )
        if not identity_matches(path, file_identity):
            raise SkillSetError("candidate_changed_during_validation")
        os.fsync(descriptor)
        retained: list[dict[str, str]] = []
        if pointer.exists():
            retained = _demote_current(pointer)
        active = _pointer_entry(path, result.build_id)
        retained = [item for item in retained if item != active][:retain]
        durable = write_pointer(pointer, active, retained)
        return SkillSetActivation(path, result.build_id, durable, result.skill_set)
    finally:
        os.close(descriptor)


def _entry_bytes(pointer: Path, entry: dict) -> tuple[Path, bytes]:
    """Open a pointer entry under symlink and containment control and read it, confirming the file did
    not change between the open and the read. Every path that touches a referenced generation goes
    through here, so the containment guarantee is stated once rather than repeated per operation."""
    path, descriptor, file_identity = _controlled_file(pointer, entry["skillset"])
    try:
        raw = _read_descriptor(descriptor)
        if len(raw) > MAX_SKILLSET_BYTES:
            raise SkillSetError("skillset_too_large")
        if not identity_matches(path, file_identity):
            raise SkillSetError("active_target_changed_during_validation")
        return path, raw
    finally:
        os.close(descriptor)


def _demote_current(pointer: Path) -> list[dict[str, str]]:
    """Move the outgoing active generation to the head of `retained`.

    Its `build_id` is RE-DERIVED from its bytes rather than copied from the pointer or recomputed by
    full validation. Re-deriving keeps the retained entry describing what the file actually contains
    now; full validation would let an outgoing generation that merely went stale or expired block the
    promotion of a fresh one, which is the same relaxation `index.py` makes by reading activation
    metadata instead of canonical metadata here."""
    current = _read_pointer(pointer)
    current_path, raw = _entry_bytes(pointer, current["active"])
    return [_pointer_entry(current_path, hashlib.sha256(raw).hexdigest()), *current["retained"]]


@serialized(0)
def rollback_skillset(
    pointer: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> SkillSetActivation:
    """Restore the most recently retained generation, demoting the current active into `retained`.

    The retained generation is REVALIDATED before it is republished but is never rebuilt from source
    packs -- it is self-contained, which is the property the sealed-bundle format exists to provide.
    Revalidating matters: a generation that was valid when it was promoted can have expired since, and
    republishing it unchecked would quietly reactivate an out-of-window skill set.

    The outgoing active only has its digest re-derived, matching `promote_skillset`: rolling back
    away from a broken generation must not be blocked by that generation being broken."""
    gates = {"today": today, "router_version": router_version, "minimum_versions": minimum_versions}
    value = _read_pointer(pointer)
    if not value["retained"]:
        raise SkillSetError("no_retained_skillset")
    selected_path, raw = _entry_bytes(pointer, value["retained"][0])
    result = validate_skillset_bytes(raw, trust_roots, approvals, **gates)
    current_path, current_raw = _entry_bytes(pointer, value["active"])
    current = _pointer_entry(current_path, hashlib.sha256(current_raw).hexdigest())
    selected = _pointer_entry(selected_path, result.build_id)
    durable = write_pointer(pointer, selected, [current, *value["retained"][1:]])
    return SkillSetActivation(selected_path, result.build_id, durable, result.skill_set)


@serialized(0)
def recover_skillset(
    pointer: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> SkillSetActivation:
    """Republish the first generation in `[active, *retained]` that still validates.

    Unlike rollback, every candidate is validated CANONICALLY, including the current active: recovery
    exists precisely for the case where the active generation is the broken one. Entries that fail are
    skipped rather than reported, because a corrupt generation is the expected input here, not an
    error; if none survives, `no_recoverable_skillset` says so rather than leaving a pointer that
    names something unreadable."""
    gates = {"today": today, "router_version": router_version, "minimum_versions": minimum_versions}
    value = _read_pointer(pointer)
    survivors: list[tuple[Path, ValidatedSkillSet]] = []
    for entry in [value["active"], *value["retained"]]:
        try:
            path, raw = _entry_bytes(pointer, entry)
            survivors.append((path, validate_skillset_bytes(raw, trust_roots, approvals, **gates)))
        except SkillSetError:
            continue
    if not survivors:
        raise SkillSetError("no_recoverable_skillset")
    path, result = survivors[0]
    retained = [_pointer_entry(item, value.build_id) for item, value in survivors[1:3]]
    durable = write_pointer(pointer, _pointer_entry(path, result.build_id), retained)
    return SkillSetActivation(path, result.build_id, durable, result.skill_set)


def read_active(pointer: Path) -> tuple[Path, str]:
    """Resolve the active generation's path and pinned `build_id` without validating it. The reader
    half of the swap: it exists so a concurrent reader can be shown to observe exactly one complete
    generation. Fail-soft handling of an unreadable pointer belongs to the degraded-serve path."""
    value = _read_pointer(pointer)
    entry = value["active"]
    path, descriptor, _ = _controlled_file(pointer, entry["skillset"])
    os.close(descriptor)
    return path, entry["build_id"]


@dataclass(frozen=True)
class SkillSetStatus:
    """The result of trying to open the active skill set. Three distinguishable states, because
    conflating them is how a broken deployment gets reported as an empty one: `skill_set` present
    means dispatch is live; both fields absent with no warning means the layer was never activated
    and is simply inactive; a `warning` means a pointer IS present but unusable."""

    skill_set: SkillSet | None
    build_id: str | None
    warning: str | None


@dataclass(frozen=True)
class GenerationInventory:
    active: Path | None
    retained: tuple[Path, ...]
    unreferenced: tuple[Path, ...]


def open_skillset(
    pointer: Path,
    trust_roots: dict[str, str],
    approvals: Mapping[str, str],
    *,
    today: date | None = None,
    router_version: str = "0.1.0",
    minimum_versions: dict[str, str] | None = None,
) -> SkillSetStatus:
    """Open the active skill set for serving. NEVER raises.

    Skill dispatch is an addition to Bruriah, not a precondition for it, so a broken skills layer
    must degrade to "no skills" and must never take down `serve`, `doctor`, or corpus retrieval. The
    failure is reported as a typed `skillset_unreadable:<code>` warning that keeps the underlying
    cause visible rather than swallowing it.

    A dangling symlink is treated as a BROKEN pointer, not an absent one: `Path.exists` follows the
    link and would answer False, which would report a corrupted deployment as a layer that was simply
    never activated -- the one confusion this function exists to prevent."""
    if not pointer.exists() and not pointer.is_symlink():
        return SkillSetStatus(None, None, None)
    try:
        value = _read_pointer(pointer)
        path, raw = _entry_bytes(pointer, value["active"])
        result = validate_skillset_bytes(
            raw,
            trust_roots,
            approvals,
            today=today,
            router_version=router_version,
            minimum_versions=minimum_versions,
        )
    except SkillSetError as error:
        return SkillSetStatus(None, None, f"skillset_unreadable:{error.code}")
    except OSError:
        return SkillSetStatus(None, None, "skillset_unreadable:pointer_unreadable")
    return SkillSetStatus(result.skill_set, result.build_id, None)


def list_generations(pointer: Path) -> GenerationInventory:
    """Inventory the generations on disk, split into active, retained, and unreferenced.

    Evicted generations are deliberately NOT garbage collected, matching `index.py` rather than
    diverging from it, so disk growth stays visible and operator-controlled instead of silent.

    Only files matching the `skillset-*.json` naming convention are considered, and never symlinks.
    That is a safety property, not an oversight: an inventory that swept every file in `data_dir`
    would hand `prune_skillset` a delete list containing things it never created.

    Raises rather than returning an empty inventory when the pointer is missing or unreadable. A
    caller cannot know what is referenced without a readable pointer, and the only safe answer to
    "what may I delete?" in that state is nothing at all."""
    if not pointer.exists() and not pointer.is_symlink():
        raise SkillSetError("no_active_skillset")
    value = _read_pointer(pointer)
    active = pointer.parent / value["active"]["skillset"]
    retained = tuple(pointer.parent / item["skillset"] for item in value["retained"])
    referenced = {active.name, *(item.name for item in retained)}
    unreferenced = tuple(sorted(
        item for item in pointer.parent.glob("skillset-*.json")
        if item.is_file() and not item.is_symlink() and item.name not in referenced
    ))
    return GenerationInventory(active, retained, unreferenced)


def prune_skillset(pointer: Path) -> tuple[Path, ...]:
    """Delete every unreferenced generation and return what was removed.

    The directory check happens BEFORE the lock: `serialized` creates its lock file beside the
    pointer, so on an installation where nothing was ever activated the lock itself would fail with a
    bare `FileNotFoundError` instead of the typed refusal a caller can act on."""
    if not pointer.parent.is_dir():
        raise SkillSetError("no_active_skillset")
    return _prune_locked(pointer)


@serialized(0)
def _prune_locked(pointer: Path) -> tuple[Path, ...]:
    """Serialized on the pointer so a promotion cannot retain a generation between the moment the
    inventory is taken and the moment the file is unlinked."""
    unreferenced = list_generations(pointer).unreferenced
    for path in unreferenced:
        path.unlink()
    return unreferenced


__all__ = [
    "MAX_SKILLSET_BYTES",
    "GenerationInventory",
    "SkillSetStatus",
    "Generation",
    "GenerationPack",
    "SkillSetActivation",
    "SkillSetError",
    "SkillSource",
    "ValidatedSkillSet",
    "compile_skillset",
    "generation_path",
    "list_generations",
    "open_skillset",
    "promote_skillset",
    "prune_skillset",
    "read_active",
    "recover_skillset",
    "rollback_skillset",
    "validate_skillset",
    "validate_skillset_bytes",
]
