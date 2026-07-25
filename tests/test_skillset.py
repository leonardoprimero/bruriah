from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cerebro_router.skillset import (
    MAX_SKILLSET_BYTES,
    SkillSetError,
    SkillSource,
    compile_skillset,
    generation_path,
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
