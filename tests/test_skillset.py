from __future__ import annotations

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cerebro_router import skillset
from cerebro_router.skillset import (
    MAX_SKILLSET_BYTES,
    SkillSetError,
    SkillSource,
    compile_skillset,
    generation_path,
    promote_skillset,
    read_active,
    validate_skillset,
    validate_skillset_bytes,
)
from test_skills import DIGEST, _pack, _skill

# Fixture builders come from `test_skills` rather than being copied, following the existing
# `test_service` -> `test_research` precedent, so a schema change breaks one place and not two.

TODAY = date(2026, 7, 25)
SIGNER = "cerebro-test-signer"
KEY = Ed25519PrivateKey.generate()
ROOTS = {SIGNER: base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()}
APPROVALS = {"design.ui-review": DIGEST}


def _bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _manifest(raw: bytes, payload: dict, *, signer: str = SIGNER, key: Ed25519PrivateKey = KEY) -> bytes:
    digest = hashlib.sha256(raw).hexdigest()
    signed = f"1\n{signer}\n{payload['pack_id']}\n{payload['version']}\n{digest}".encode()
    return json.dumps(
        {
            "schema_version": "1",
            "signer": signer,
            "pack_id": payload["pack_id"],
            "version": payload["version"],
            "sha256": digest,
            "signature": base64.b64encode(key.sign(signed)).decode(),
        }
    ).encode()


def _source(tmp_path: Path, payload: dict, *, signed: bool = True, **manifest_kwargs: Any) -> SkillSource:
    raw = _bytes(payload)
    pack_path = tmp_path / f"{payload['pack_id']}.json"
    pack_path.write_bytes(raw)
    if not signed:
        return SkillSource(pack_path, None, allow_unsigned_local=True)
    manifest_path = tmp_path / f"{payload['pack_id']}.manifest.json"
    manifest_path.write_bytes(_manifest(raw, payload, **manifest_kwargs))
    return SkillSource(pack_path, manifest_path)


def _second() -> dict:
    return _pack(pack_id="another.skills", skills=[_skill(skill_id="security.threat-model")])


SECOND_APPROVALS = {**APPROVALS, "security.threat-model": DIGEST}


def _compile(tmp_path: Path, *payloads: dict, approvals: dict | None = None, **kwargs: Any):
    destination = kwargs.pop("destination", tmp_path / "gen.json")
    sources = [_source(tmp_path, payload) for payload in payloads or (_pack(),)]
    # `is None`, never `or`: an EMPTY approvals map is a meaningful input (nothing is approved) and
    # must not silently fall back to the default fixture.
    return destination, compile_skillset(
        sources, destination, ROOTS, APPROVALS if approvals is None else approvals, today=TODAY, **kwargs
    )


def _code(callable_, *args: Any, **kwargs: Any) -> str:
    with pytest.raises(SkillSetError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


def _generation(tmp_path: Path, *payloads: dict, approvals: dict | None = None) -> bytes:
    destination, _ = _compile(tmp_path, *payloads, approvals=approvals)
    return destination.read_bytes()


def _validate(raw: bytes, *, approvals: dict | None = None, roots: dict | None = None, **kwargs: Any):
    kwargs.setdefault("today", TODAY)
    return validate_skillset_bytes(
        raw, ROOTS if roots is None else roots, APPROVALS if approvals is None else approvals, **kwargs
    )


def _tamper(raw: bytes, mutate) -> bytes:
    data = json.loads(raw)
    mutate(data)
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _embed(raw: bytes, index: int, payload: dict) -> bytes:
    """Replace one embedded pack's bytes without touching its manifest -- the tamper a digest check
    exists to catch."""
    return _tamper(raw, lambda data: data["packs"][index].__setitem__(
        "source", base64.b64encode(_bytes(payload)).decode()
    ))


# --- compile -------------------------------------------------------------------------------------


def test_compile_produces_a_generation_that_validates(tmp_path: Path) -> None:
    destination, result = _compile(tmp_path)
    assert destination.is_file()
    assert result.skill_set.skill_ids == ("design.ui-review",)
    assert result.build_id == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_build_id_is_the_digest_of_the_canonical_bytes(tmp_path: Path) -> None:
    destination, result = _compile(tmp_path, _pack(), _second(), approvals=SECOND_APPROVALS)
    assert len(result.build_id) == 64
    assert result.build_id == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_compile_is_deterministic_regardless_of_source_order(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    forward = compile_skillset(
        [_source(first, _pack()), _source(first, _second())],
        first / "gen.json", ROOTS, SECOND_APPROVALS, today=TODAY,
    )
    backward = compile_skillset(
        [_source(second, _second()), _source(second, _pack())],
        second / "gen.json", ROOTS, SECOND_APPROVALS, today=TODAY,
    )
    assert (first / "gen.json").read_bytes() == (second / "gen.json").read_bytes()
    assert forward.build_id == backward.build_id
    assert forward.skill_set.pack_ids == ("another.skills", "cerebro.skills")


def test_generation_is_self_contained_after_its_sources_are_gone(tmp_path: Path) -> None:
    # The lifecycle spec requires rollback to restore a retained generation WITHOUT rebuilding from
    # source packs. That is only true if the generation carries everything needed to re-verify.
    destination, before = _compile(tmp_path)
    for leftover in tmp_path.glob("cerebro.skills*"):
        leftover.unlink()
    after = validate_skillset(destination, ROOTS, APPROVALS, today=TODAY)
    assert after.build_id == before.build_id
    assert after.skill_set.skill_ids == before.skill_set.skill_ids


def test_compile_refuses_to_overwrite_an_existing_generation(tmp_path: Path) -> None:
    destination, _ = _compile(tmp_path)
    assert _code(_compile, tmp_path, destination=destination) == "generation_exists"


def test_failed_compile_writes_no_artifact(tmp_path: Path) -> None:
    destination = tmp_path / "gen.json"
    assert _code(_compile, tmp_path, destination=destination, approvals={}) == "skill_not_approved"
    assert not destination.exists()
    assert not list(tmp_path.glob(".gen.json*"))


def test_compile_rejects_an_invalid_source_before_writing(tmp_path: Path) -> None:
    # `expires_at` alone is not enough: moving it before `reviewed_at` trips the coherence validator
    # and reports `malformed_pack`, not the expiry gate this test is about.
    expired = _pack(reviewed_at="2025-01-01", expires_at="2026-01-01", freshness_days=3650)
    destination = tmp_path / "gen.json"
    assert _code(_compile, tmp_path, expired, destination=destination) == "expired_pack"
    assert not destination.exists()


def test_compile_reports_unreadable_and_oversized_sources(tmp_path: Path) -> None:
    missing = SkillSource(tmp_path / "absent.json", None, allow_unsigned_local=True)
    assert _code(compile_skillset, [missing], tmp_path / "a.json", ROOTS, APPROVALS) == "pack_unreadable"
    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b"x" * 70_000)
    oversized = SkillSource(big, None, allow_unsigned_local=True)
    assert _code(compile_skillset, [oversized], tmp_path / "b.json", ROOTS, APPROVALS) == "pack_too_large"


def test_compile_reports_an_unreadable_manifest(tmp_path: Path) -> None:
    source = _source(tmp_path, _pack())
    source.manifest_path.unlink()
    assert _code(compile_skillset, [source], tmp_path / "gen.json", ROOTS, APPROVALS) == "malformed_manifest"


def test_unsigned_local_pack_compiles_and_validates(tmp_path: Path) -> None:
    local = _pack(pack_id="local.skills", skills=[_skill(tier="local")])
    destination = tmp_path / "gen.json"
    compile_skillset([_source(tmp_path, local, signed=False)], destination, ROOTS, APPROVALS, today=TODAY)
    assert validate_skillset(destination, ROOTS, APPROVALS, today=TODAY).skill_set.pack_ids == ("local.skills",)


def test_unsigned_source_needs_explicit_local_opt_in(tmp_path: Path) -> None:
    local = _pack(pack_id="local.skills", skills=[_skill(tier="local")])
    path = tmp_path / "local.json"
    path.write_bytes(_bytes(local))
    source = SkillSource(path, None, allow_unsigned_local=False)
    assert _code(compile_skillset, [source], tmp_path / "gen.json", ROOTS, APPROVALS) == "signature_required"


# --- validation re-verifies rather than trusting the compile --------------------------------------


def test_tampered_embedded_pack_fails_the_digest_check(tmp_path: Path) -> None:
    # The replacement stays schema-valid, otherwise `malformed_pack` fires first and the digest check
    # is never reached -- schema validation precedes manifest verification by design.
    raw = _embed(_generation(tmp_path), 0, _pack(maintainer="tampered"))
    assert _code(_validate, raw) == "digest_mismatch"


def test_embedded_signature_is_re_verified_not_assumed(tmp_path: Path) -> None:
    # A falsifiability probe that neutered `verify_manifest_bytes` left every other test in this file
    # green, which meant the generation path's SIGNATURE check was not pinned at all: `digest_mismatch`
    # and `unknown_signer` both fire earlier and were masking it. The rogue manifest here names the
    # trusted signer and carries the correct digest, so only the Ed25519 check can reject it.
    payload = _pack()
    raw = _bytes(payload)
    pack_path = tmp_path / "cerebro.skills.json"
    pack_path.write_bytes(raw)
    manifest_path = tmp_path / "cerebro.skills.manifest.json"
    manifest_path.write_bytes(_manifest(raw, payload, key=Ed25519PrivateKey.generate()))
    source = SkillSource(pack_path, manifest_path)
    assert _code(compile_skillset, [source], tmp_path / "gen.json", ROOTS, APPROVALS, today=TODAY) == "invalid_signature"
    # And the same rogue manifest swapped into an already-built generation, which is the path a
    # rollback takes: compile can never produce this, so only re-verification at validate catches it.
    rogue = base64.b64encode(_manifest(raw, payload, key=Ed25519PrivateKey.generate())).decode()
    swapped = _tamper(_generation(tmp_path), lambda data: data["packs"][0].__setitem__("manifest", rogue))
    assert _code(_validate, swapped) == "invalid_signature"


def test_trust_roots_are_applied_at_validation_not_baked_in(tmp_path: Path) -> None:
    raw = _generation(tmp_path)
    assert _validate(raw).skill_set.pack_ids == ("cerebro.skills",)
    assert _code(_validate, raw, roots={"someone-else": ROOTS[SIGNER]}) == "unknown_signer"


def test_freshness_is_injected_never_wall_clock(tmp_path: Path) -> None:
    raw = _generation(tmp_path)
    assert _validate(raw, today=date(2026, 7, 25)).skill_set.skill_ids == ("design.ui-review",)
    assert _code(_validate, raw, today=date(2030, 1, 1)) == "expired_pack"
    assert _code(_validate, raw, today=date(2025, 1, 1)) == "future_review"


def test_router_compatibility_and_version_floor_are_rechecked(tmp_path: Path) -> None:
    raw = _generation(tmp_path)
    assert _code(_validate, raw, router_version="9.9.9") == "incompatible_pack"
    assert _code(_validate, raw, minimum_versions={"cerebro.skills": "2.0.0"}) == "version_rollback"


def test_envelope_invariant_survives_the_generation_round_trip(tmp_path: Path) -> None:
    # A prose skill declaring network access is rejected by the closed model, and a correctly SIGNED
    # pack cannot buy its way past that -- schema validation runs before signature verification.
    leaky = _pack(skills=[_skill(permissions={"network": {"hosts": ["example.com"], "schemes": ["https"]}})])
    source = _source(tmp_path, leaky)
    assert _code(compile_skillset, [source], tmp_path / "gen.json", ROOTS, APPROVALS, today=TODAY) == "malformed_pack"


def test_unsigned_entry_cannot_smuggle_a_first_party_skill(tmp_path: Path) -> None:
    # An unsigned entry inside a generation is gated only by the tier invariant, which is exactly why
    # there is no "this was allowed to be unsigned" flag to forge.
    local = _pack(pack_id="local.skills", skills=[_skill(tier="local")])
    destination = tmp_path / "gen.json"
    compile_skillset([_source(tmp_path, local, signed=False)], destination, ROOTS, APPROVALS, today=TODAY)
    promoted = _pack(pack_id="local.skills", skills=[_skill(tier="first_party")])
    assert _code(_validate, _embed(destination.read_bytes(), 0, promoted)) == "unsigned_nonlocal_pack"


def test_embedded_pack_cannot_exceed_the_single_pack_ceiling(tmp_path: Path) -> None:
    # The path loader enforces the ceiling inside `read_pack_bytes`; an embedded pack never goes
    # through it, so a generation would otherwise be a way past a limit the direct loader applies.
    raw = _tamper(
        _generation(tmp_path),
        lambda data: data["packs"][0].__setitem__(
            "source", base64.b64encode(b"{" + b"x" * 70_000).decode()
        ),
    )
    assert _code(_validate, raw) == "pack_too_large"


# --- approval binding ----------------------------------------------------------------------------


def test_every_skill_needs_an_approval_record(tmp_path: Path) -> None:
    raw = _generation(tmp_path)
    assert _code(_validate, raw, approvals={}) == "skill_not_approved"


def test_approval_is_bound_to_the_current_body_digest(tmp_path: Path) -> None:
    raw = _generation(tmp_path)
    stale = {"design.ui-review": "sha256:" + "b" * 64}
    assert _code(_validate, raw, approvals=stale) == "approval_digest_divergent"


def test_a_second_pack_needs_its_own_approval(tmp_path: Path) -> None:
    raw = _generation(tmp_path, _pack(), _second(), approvals=SECOND_APPROVALS)
    assert _validate(raw, approvals=SECOND_APPROVALS).skill_set.skill_ids == (
        "security.threat-model", "design.ui-review",
    )
    assert _code(_validate, raw, approvals=APPROVALS) == "skill_not_approved"


# --- generation container fails closed -------------------------------------------------------------


def test_unsorted_generation_is_rejected_rather_than_re_sorted(tmp_path: Path) -> None:
    raw = _generation(tmp_path, _pack(), _second(), approvals=SECOND_APPROVALS)
    reversed_packs = _tamper(raw, lambda data: data["packs"].reverse())
    assert _code(_validate, reversed_packs, approvals=SECOND_APPROVALS) == "unsorted_generation"


def test_duplicate_skill_across_embedded_packs_is_rejected(tmp_path: Path) -> None:
    # Caught at compile because compile validates the encoded set before writing, which is the whole
    # point of validating first: the colliding generation never reaches disk.
    clash = _pack(pack_id="a.skills")
    destination = tmp_path / "gen.json"
    assert _code(_compile, tmp_path, _pack(), clash, destination=destination) == "duplicate_skill_id"
    assert not destination.exists()


def test_unreadable_and_oversized_generations_fail_closed(tmp_path: Path) -> None:
    assert _code(validate_skillset, tmp_path / "absent.json", ROOTS, APPROVALS) == "skillset_unreadable"
    assert _code(_validate, b"{" + b"x" * MAX_SKILLSET_BYTES) == "skillset_too_large"


def test_malformed_and_duplicate_key_generations_fail_closed(tmp_path: Path) -> None:
    assert _code(_validate, b"{not json") == "malformed_pack"
    assert _code(_validate, b'{"schema_version": "1", "schema_version": "1"}') == "duplicate_key"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda data: data.__setitem__("schema_version", "2"), "malformed_skillset"),
        (lambda data: data.__setitem__("unexpected", True), "malformed_skillset"),
        (lambda data: data["packs"][0].__setitem__("unexpected", True), "malformed_skillset"),
        (lambda data: data["packs"][0].__setitem__("format", ".exe"), "malformed_skillset"),
        (lambda data: data["packs"][0].__setitem__("source", "not base64!"), "malformed_skillset"),
        (lambda data: data["packs"][0].__setitem__("source", "AAAAA"), "malformed_skillset"),
        (lambda data: data.__setitem__("packs", []), "empty_skillset"),
    ],
)
def test_generation_container_is_closed(tmp_path: Path, mutate, code: str) -> None:
    assert _code(_validate, _tamper(_generation(tmp_path), mutate)) == code


def test_generation_path_names_a_fresh_file(tmp_path: Path) -> None:
    first = generation_path(tmp_path)
    assert first.parent == tmp_path and first.name.startswith("skillset-") and first.suffix == ".json"
    assert first != generation_path(tmp_path)


# --- activation ------------------------------------------------------------------------------------
# Generations live directly beside the pointer, as promotion requires; their source packs go in a
# per-generation subdirectory so distinct generations can reuse one pack id.


def _built(tmp_path: Path, name: str, **overrides: Any) -> Path:
    workspace = tmp_path / f".src-{name}"
    workspace.mkdir()
    destination = tmp_path / f"skillset-{name}.json"
    compile_skillset(
        [_source(workspace, _pack(**overrides))], destination, ROOTS, APPROVALS, today=TODAY
    )
    return destination


def _promote(candidate: Path, pointer: Path, **kwargs: Any):
    kwargs.setdefault("today", TODAY)
    approvals = kwargs.pop("approvals", APPROVALS)
    return promote_skillset(candidate, pointer, ROOTS, approvals, **kwargs)


def _pointer_state(pointer: Path) -> tuple[str, list[str]]:
    value = json.loads(pointer.read_text())
    return value["active"]["build_id"], [item["build_id"] for item in value["retained"]]


def test_promotion_publishes_the_candidate(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    result = _promote(_built(tmp_path, "one"), pointer)
    assert result.durable and result.skill_set.skill_ids == ("design.ui-review",)
    path, build_id = read_active(pointer)
    assert path == result.path and build_id == result.build_id
    assert _pointer_state(pointer) == (result.build_id, [])


def test_promotion_demotes_the_outgoing_generation(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    first = _promote(_built(tmp_path, "one"), pointer)
    second = _promote(_built(tmp_path, "two", maintainer="Second"), pointer)
    assert _pointer_state(pointer) == (second.build_id, [first.build_id])


def test_retention_keeps_exactly_two_prior_generations(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    built = [_promote(_built(tmp_path, str(n), maintainer=f"M{n}"), pointer) for n in range(4)]
    active, retained = _pointer_state(pointer)
    assert active == built[3].build_id
    assert retained == [built[2].build_id, built[1].build_id]


def test_repromoting_the_same_generation_does_not_duplicate_it(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    first = _built(tmp_path, "one")
    second = _built(tmp_path, "two", maintainer="Second")
    _promote(first, pointer)
    _promote(second, pointer)
    again = _promote(first, pointer)
    active, retained = _pointer_state(pointer)
    assert active == again.build_id
    assert retained.count(again.build_id) == 0


def test_failed_validation_leaves_the_active_set_byte_identical(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    good = _built(tmp_path, "one")
    _promote(good, pointer)
    before_pointer = pointer.read_bytes()
    before_active = good.read_bytes()
    rejected = _built(tmp_path, "two", maintainer="Second")
    assert _code(_promote, rejected, pointer, approvals={}) == "skill_not_approved"
    assert pointer.read_bytes() == before_pointer
    assert good.read_bytes() == before_active
    assert read_active(pointer)[0] == good


def test_expiry_is_checked_at_promotion_with_the_injected_today(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    candidate = _built(tmp_path, "one")
    assert _code(_promote, candidate, pointer, today=date(2030, 1, 1)) == "expired_pack"
    assert not pointer.exists()
    assert _promote(candidate, pointer).durable


@pytest.mark.parametrize("retain", [0, -1])
def test_retention_below_one_is_refused(tmp_path: Path, retain: int) -> None:
    pointer = tmp_path / "active.json"
    assert _code(_promote, _built(tmp_path, "one"), pointer, retain=retain) == "invalid_activation_path"


def test_candidate_outside_the_pointer_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    candidate = _built(elsewhere, "one")
    assert _code(_promote, candidate, tmp_path / "active.json") == "invalid_activation_path"


def test_symlinked_candidate_is_refused(tmp_path: Path) -> None:
    real = _built(tmp_path, "one")
    link = tmp_path / "skillset-link.json"
    link.symlink_to(real)
    assert _code(_promote, link, tmp_path / "active.json") == "invalid_active_target"


def test_symlinked_pointer_is_refused(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    _promote(_built(tmp_path, "one"), pointer)
    real = tmp_path / "real.json"
    pointer.rename(real)
    pointer.symlink_to(real)
    assert _code(_promote, _built(tmp_path, "two", maintainer="Second"), pointer) == "invalid_active_pointer"


def test_traversal_in_the_pointer_is_refused(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    _promote(_built(tmp_path, "one"), pointer)
    pointer.write_text(json.dumps(
        {"version": 1, "active": {"skillset": "../escape.json", "build_id": "x"}, "retained": []}
    ))
    assert _code(_promote, _built(tmp_path, "two", maintainer="Second"), pointer) == "invalid_active_pointer"
    assert _code(read_active, pointer) == "invalid_active_pointer"


def test_a_candidate_swapped_during_validation_is_caught(tmp_path: Path, monkeypatch) -> None:
    pointer = tmp_path / "active.json"
    candidate = _built(tmp_path, "one")
    other = _built(tmp_path, "two", maintainer="Second").read_bytes()
    original = skillset.validate_skillset_bytes

    def swap(raw: bytes, *args: Any, **kwargs: Any):
        result = original(raw, *args, **kwargs)
        candidate.write_bytes(other)
        return result

    monkeypatch.setattr(skillset, "validate_skillset_bytes", swap)
    assert _code(_promote, candidate, pointer) == "candidate_changed_during_validation"
    assert not pointer.exists()


def test_concurrent_readers_observe_one_complete_generation(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    first = _promote(_built(tmp_path, "one"), pointer)
    second = _built(tmp_path, "two", maintainer="Second")
    barrier = threading.Barrier(5)

    def observe() -> set[str]:
        seen: set[str] = set()
        barrier.wait()
        for _ in range(60):
            path, build_id = read_active(pointer)
            # Every observation must be a COMPLETE generation: the file the pointer names validates,
            # and its bytes hash to the build id the pointer pinned. A torn write fails both.
            assert validate_skillset(path, ROOTS, APPROVALS, today=TODAY).build_id == build_id
            seen.add(build_id)
        return seen

    with ThreadPoolExecutor(max_workers=5) as pool:
        readers = [pool.submit(observe) for _ in range(4)]
        barrier.wait()
        promoted = _promote(second, pointer)
        observed = set().union(*(future.result() for future in readers))
    assert observed <= {first.build_id, promoted.build_id}


def test_concurrent_promotions_serialize_and_keep_full_history(tmp_path: Path) -> None:
    pointer = tmp_path / "active.json"
    candidates = [_built(tmp_path, str(n), maintainer=f"M{n}") for n in range(3)]
    first = _promote(candidates[0], pointer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        promotions = [pool.submit(_promote, candidate, pointer) for candidate in candidates[1:]]
        build_ids = {future.result().build_id for future in promotions}
    assert len(build_ids) == 2
    active, retained = _pointer_state(pointer)
    # flock serialized the two promoters, so neither update was lost: whichever ran second is active
    # and the other sits in retained alongside the original.
    assert active in build_ids
    assert set(retained) == (build_ids - {active}) | {first.build_id}
    assert len(retained) == 2
