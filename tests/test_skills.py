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
from pydantic import ValidationError

from cerebro_router.packs import PackError
from cerebro_router.skills import (
    FilesystemAccess,
    NetworkAccess,
    PermissionEnvelope,
    SkillPack,
    SkillPolicy,
    SkillSet,
    SubprocessAccess,
    load_skill_pack,
    reject_executable_payload,
)

# `ClosedModel` sets `strict=True`, so date fields only accept ISO strings through JSON validation --
# the same reason `packs.py` uses `model_validate_json`. Building fixtures as dicts and validating
# via JSON keeps these tests on the path production actually uses.

DIGEST = "sha256:" + "a" * 64
def _skill(**overrides: Any) -> dict:
    skill = {
        "skill_id": "design.ui-review",
        "version": "1.4.0",
        "tier": "first_party",
        "payload": "prose",
        "summary": "Review an interface for hierarchy, contrast, and spacing.",
        "domains": ["programming"],
        "body_locator": "design/ui-review/SKILL.md",
        "body_digest": DIGEST,
        "provenance": "cerebro first-party pack",
        "license": "MIT",
    }
    skill.update(overrides)
    return skill
def _pack(**overrides: Any) -> dict:
    pack = {
        "schema_version": "1",
        "pack_id": "cerebro.skills",
        "version": "1.0.0",
        "maintainer": "Cerebro",
        "min_router_version": "0.1.0",
        "max_router_version": "1.0.0",
        "reviewed_at": "2026-07-20",
        "expires_at": "2027-07-20",
        "freshness_days": 365,
        "license": "MIT",
        "provenance": "authored in-repo",
        "skills": [_skill()],
    }
    pack.update(overrides)
    return pack
def _load(payload: dict) -> SkillPack:
    return SkillPack.model_validate_json(json.dumps(payload))
def _error(payload: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        _load(payload)
    return str(caught.value)
def _skill_error(**overrides: Any) -> str:
    return _error(_pack(skills=[_skill(**overrides)]))


def test_minimal_pack_loads_and_exposes_its_skill() -> None:
    pack = _load(_pack())
    assert pack.pack_id == "cerebro.skills"
    assert [skill.skill_id for skill in pack.skills] == ["design.ui-review"]
    assert pack.skills[0].body_digest == DIGEST
def test_permissions_default_to_deny_by_absence() -> None:
    # The central property: a skill that declares no permissions grants nothing, and it grants
    # nothing because every dimension is EMPTY, not because a flag says so.
    envelope = _load(_pack()).skills[0].permissions
    assert envelope.grants_nothing() is True
    assert envelope.filesystem.read_paths == [] and envelope.filesystem.write_paths == []
    assert envelope.network.hosts == [] and envelope.network.schemes == []
    assert envelope.subprocess.programs == [] and envelope.secrets == []
def test_default_envelopes_are_not_shared_between_skills() -> None:
    # A shared mutable default would let one skill's grant leak into another's envelope.
    first = _load(_pack()).skills[0].permissions
    second = _load(_pack()).skills[0].permissions
    first.filesystem.read_paths.append("notes.md")
    assert second.filesystem.read_paths == []


# --- prose-only invariants -----------------------------------------------------------------------
# A prose skill is instructions for a language model, so no sandbox can verify its behaviour. The
# honest mitigation is to make the dangerous dimensions inexpressible rather than to analyse text.
@pytest.mark.parametrize(
    ("permissions", "reason"),
    [
        ({"network": {"hosts": ["api.example.com"]}}, "prose_skill_declares_network"),
        ({"network": {"schemes": ["https"]}}, "prose_skill_declares_network"),
        ({"subprocess": {"programs": ["curl"]}}, "prose_skill_declares_subprocess"),
        ({"secrets": ["github-token"]}, "prose_skill_declares_secrets"),
        ({"filesystem": {"write_paths": ["out.md"]}}, "prose_skill_declares_filesystem_write"),
    ],
)
def test_prose_skill_cannot_declare_dangerous_permissions(permissions: dict, reason: str) -> None:
    assert reason in _skill_error(permissions=permissions)
def test_prose_skill_may_declare_read_paths() -> None:
    # Reading grants nothing the host has not already granted, so this stays expressible.
    pack = _load(_pack(skills=[_skill(permissions={"filesystem": {"read_paths": ["docs/spec.md"]}})]))
    envelope = pack.skills[0].permissions
    assert envelope.filesystem.read_paths == ["docs/spec.md"]
    assert envelope.grants_nothing() is False
def test_executable_payload_is_not_expressible() -> None:
    assert "payload" in _skill_error(payload="executable")
def test_executable_candidate_is_refused_with_its_own_code() -> None:
    # Schema validation alone would report a generic malformed-pack error, which reads as a typo
    # rather than an unsupported capability. The refusal must name itself.
    with pytest.raises(PackError) as caught:
        reject_executable_payload(_pack(skills=[_skill(payload="executable")]))
    assert caught.value.code == "payload_unsupported"
    reject_executable_payload(_pack())  # prose passes through untouched


# --- envelope cannot express a broad grant -------------------------------------------------------
@pytest.mark.parametrize("host", ["*", "*.example.com", "example.com/*", "EXAMPLE.com", "-bad.com"])
def test_wildcard_and_malformed_hosts_are_inexpressible(host: str) -> None:
    with pytest.raises(ValidationError):
        NetworkAccess.model_validate_json(json.dumps({"hosts": [host]}))
def test_scheme_is_restricted_to_https() -> None:
    with pytest.raises(ValidationError):
        NetworkAccess.model_validate_json(json.dumps({"schemes": ["http"]}))
@pytest.mark.parametrize("program", ["sh -c 'x'", "/bin/sh", "curl | sh", "a;b"])
def test_subprocess_cannot_smuggle_a_shell_string(program: str) -> None:
    with pytest.raises(ValidationError):
        SubprocessAccess.model_validate_json(json.dumps({"programs": [program]}))
@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../../secrets.env", "docs/../../escape", "docs/", "docs//spec.md", ".", "..",
     "docs/./spec.md", "*", "docs/*.md", "~/.ssh/id_rsa"],
)
def test_filesystem_paths_reject_escapes_and_globs(path: str) -> None:
    with pytest.raises(ValidationError):
        FilesystemAccess.model_validate_json(json.dumps({"read_paths": [path]}))
def test_filesystem_accepts_a_plain_relative_path() -> None:
    access = FilesystemAccess.model_validate_json(json.dumps({"read_paths": ["docs/a-b_c.1.md"]}))
    assert access.read_paths == ["docs/a-b_c.1.md"]


# --- closed models -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "model", [SkillPack, SkillPolicy, PermissionEnvelope, FilesystemAccess, NetworkAccess, SubprocessAccess]
)
def test_every_model_is_closed(model: type) -> None:
    assert model.model_config["extra"] == "forbid"
    assert model.model_config["strict"] is True
def test_unknown_field_is_rejected_on_pack_and_skill() -> None:
    assert "answers" in _error(_pack(answers="nope"))
    assert "trigger" in _skill_error(trigger="when designing")


# --- required fields and formats -----------------------------------------------------------------
def test_skill_must_declare_at_least_one_domain() -> None:
    assert "empty_required_registry" in _skill_error(domains=[])
@pytest.mark.parametrize("digest", ["deadbeef", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha1:" + "a" * 40])
def test_body_digest_must_be_a_full_sha256(digest: str) -> None:
    assert "body_digest" in _skill_error(body_digest=digest)
@pytest.mark.parametrize("version", ["1.4", "v1.4.0", "1.4.0-rc1", ""])
def test_versions_are_strict_semver_triples(version: str) -> None:
    assert "version" in _skill_error(version=version)
@pytest.mark.parametrize("tier", ["first-party", "vendor", "", "FIRST_PARTY"])
def test_tier_is_a_closed_vocabulary(tier: str) -> None:
    assert "tier" in _skill_error(tier=tier)
@pytest.mark.parametrize("field", ["skill_id", "summary", "body_locator", "provenance", "license", "tier", "payload"])
def test_required_skill_fields_cannot_be_omitted(field: str) -> None:
    skill = _skill()
    skill.pop(field)
    assert field in _error(_pack(skills=[skill]))


# --- pack coherence ------------------------------------------------------------------------------
def test_pack_must_contain_at_least_one_skill() -> None:
    assert "empty_required_registry" in _error(_pack(skills=[]))
def test_pack_review_window_must_be_ordered() -> None:
    assert "invalid_review_window" in _error(_pack(reviewed_at="2027-07-20", expires_at="2027-07-20"))
    assert "invalid_review_window" in _error(_pack(reviewed_at="2027-07-21", expires_at="2027-07-20"))
def test_pack_rejects_duplicate_skill_ids() -> None:
    assert "duplicate_skill_id" in _error(_pack(skills=[_skill(), _skill(version="2.0.0")]))
def test_pack_accepts_distinct_skills() -> None:
    pack = _load(_pack(skills=[_skill(), _skill(skill_id="security.threat-model")]))
    assert [skill.skill_id for skill in pack.skills] == ["design.ui-review", "security.threat-model"]
@pytest.mark.parametrize("freshness", [0, -1, 3651])
def test_pack_freshness_window_is_bounded(freshness: int) -> None:
    assert "freshness_days" in _error(_pack(freshness_days=freshness))
def test_pack_schema_version_is_pinned() -> None:
    assert "schema_version" in _error(_pack(schema_version="2"))


# --- signed loading ------------------------------------------------------------------------------
# The tests generate their OWN ephemeral keypair rather than reaching for the release key, which is
# the right shape: a test must never depend on maintainer key material existing on the machine.
# This predates `signing.py` and is what showed the signing tool would be small -- keygen, digest,
# one canonical string, one signature.
SIGNER = "cerebro-test-signer"
def _bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
def _sign(tmp_path: Path, payload: dict, *, signer: str = SIGNER,
          signature: str | None = None) -> tuple[Path, Path, dict[str, str]]:
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    roots = {SIGNER: base64.b64encode(public).decode()}
    pack_path = tmp_path / "skills.json"
    raw = _bytes(payload)
    pack_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    signed = f"1\n{signer}\n{payload['pack_id']}\n{payload['version']}\n{digest}".encode()
    manifest = {
        "schema_version": "1", "signer": signer, "pack_id": payload["pack_id"],
        "version": payload["version"], "sha256": digest,
        "signature": signature or base64.b64encode(key.sign(signed)).decode(),
    }
    manifest_path = tmp_path / "skills.manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return pack_path, manifest_path, roots
def _signed_code(tmp_path: Path, payload: dict, **kwargs: Any) -> str:
    pack_path, manifest_path, roots = _sign(tmp_path, payload, **{
        k: v for k, v in kwargs.items() if k in {"signer", "signature"}
    })
    load_kwargs = {k: v for k, v in kwargs.items() if k not in {"signer", "signature"}}
    load_kwargs.setdefault("today", date(2026, 7, 25))
    with pytest.raises(PackError) as caught:
        load_skill_pack(pack_path, manifest_path, roots, **load_kwargs)
    return caught.value.code


def test_signed_skill_pack_loads(tmp_path: Path) -> None:
    pack_path, manifest_path, roots = _sign(tmp_path, _pack())
    pack = load_skill_pack(pack_path, manifest_path, roots, today=date(2026, 7, 25))
    assert pack.pack_id == "cerebro.skills" and pack.skills[0].skill_id == "design.ui-review"
def test_unsigned_local_pack_is_permitted_only_for_local_tier(tmp_path: Path) -> None:
    local = _pack(skills=[_skill(tier="local")])
    path = tmp_path / "local.json"
    path.write_text(json.dumps(local))
    loaded = load_skill_pack(path, None, {}, allow_unsigned_local=True, today=date(2026, 7, 25))
    assert loaded.skills[0].tier == "local"
def test_unsigned_pack_cannot_smuggle_a_first_party_skill(tmp_path: Path) -> None:
    # Without a signature there is no provenance at all, so a first- or third-party skill must not
    # ride in on the local-pack exception.
    path = tmp_path / "sneaky.json"
    path.write_text(json.dumps(_pack(skills=[_skill(tier="first_party")])))
    with pytest.raises(PackError) as caught:
        load_skill_pack(path, None, {}, allow_unsigned_local=True, today=date(2026, 7, 25))
    assert caught.value.code == "unsigned_nonlocal_pack"
def test_unsigned_pack_requires_explicit_local_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "local.json"
    path.write_text(json.dumps(_pack(skills=[_skill(tier="local")])))
    with pytest.raises(PackError) as caught:
        load_skill_pack(path, None, {}, today=date(2026, 7, 25))
    assert caught.value.code == "signature_required"
def test_missing_and_oversized_packs_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(PackError) as missing:
        load_skill_pack(tmp_path / "absent.json", None, {}, allow_unsigned_local=True)
    assert missing.value.code == "pack_unreadable"
    big = tmp_path / "big.json"
    big.write_bytes(b"{" + b"x" * 70_000)
    with pytest.raises(PackError) as oversized:
        load_skill_pack(big, None, {}, allow_unsigned_local=True)
    assert oversized.value.code == "pack_too_large"
def test_malformed_and_duplicate_key_packs_fail_closed(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(PackError, match="malformed_pack"):
        load_skill_pack(broken, None, {}, allow_unsigned_local=True)
    duplicated = tmp_path / "dup.json"
    duplicated.write_text('{"pack_id": "a", "pack_id": "b"}')
    with pytest.raises(PackError, match="duplicate_key"):
        load_skill_pack(duplicated, None, {}, allow_unsigned_local=True)
def test_executable_payload_is_refused_before_schema_validation(tmp_path: Path) -> None:
    # The pack is BOTH executable-payloaded and schema-invalid (no license). The specific refusal
    # must win, otherwise an unsupported capability reads as a typo.
    payload = _pack(skills=[_skill(payload="executable")])
    payload.pop("license")
    path = tmp_path / "exe.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(PackError) as caught:
        load_skill_pack(path, None, {}, allow_unsigned_local=True)
    assert caught.value.code == "payload_unsupported"
@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"signer": "unknown-signer"}, "unknown_signer"),
        ({"signature": base64.b64encode(bytes(64)).decode()}, "invalid_signature"),
        ({"today": date(2030, 1, 1)}, "expired_pack"),
        ({"router_version": "9.9.9"}, "incompatible_pack"),
        ({"minimum_versions": {"cerebro.skills": "2.0.0"}}, "version_rollback"),
    ],
)
def test_signed_pack_gates_fail_closed(tmp_path: Path, kwargs: dict, code: str) -> None:
    assert _signed_code(tmp_path, _pack(), **kwargs) == code
def test_review_window_gates_fail_closed(tmp_path: Path) -> None:
    future = _pack(reviewed_at="2027-01-01", expires_at="2028-01-01")
    assert _signed_code(tmp_path, future, today=date(2026, 7, 25)) == "future_review"
    stale = _pack(reviewed_at="2026-01-01", expires_at="2029-01-01", freshness_days=30)
    assert _signed_code(tmp_path, stale, today=date(2026, 7, 25)) == "stale_pack"
def test_tampered_pack_body_fails_the_digest_check(tmp_path: Path) -> None:
    # The replacement must stay SCHEMA-VALID, otherwise `malformed_pack` fires first and the digest
    # check is never reached -- schema validation precedes manifest verification by design.
    pack_path, manifest_path, roots = _sign(tmp_path, _pack())
    pack_path.write_bytes(_bytes(_pack(maintainer="tampered")))
    with pytest.raises(PackError) as caught:
        load_skill_pack(pack_path, manifest_path, roots, today=date(2026, 7, 25))
    assert caught.value.code == "digest_mismatch"
def test_schema_validity_precedes_digest_verification(tmp_path: Path) -> None:
    # Pinning the order the failed fixture above revealed: an invalid pack reports itself invalid
    # rather than reporting a digest mismatch, even though its bytes no longer match the manifest.
    pack_path, manifest_path, roots = _sign(tmp_path, _pack())
    pack_path.write_bytes(b'{"pack_id":"tampered"}')
    with pytest.raises(PackError) as caught:
        load_skill_pack(pack_path, manifest_path, roots, today=date(2026, 7, 25))
    assert caught.value.code == "malformed_pack"
def test_malformed_manifest_fails_closed(tmp_path: Path) -> None:
    pack_path, manifest_path, roots = _sign(tmp_path, _pack())
    manifest_path.write_text("{")
    with pytest.raises(PackError, match="malformed_manifest"):
        load_skill_pack(pack_path, manifest_path, roots, today=date(2026, 7, 25))
def test_signature_checks_precede_date_checks(tmp_path: Path) -> None:
    # An unknown signer on an expired pack reports the signer, matching `load_pack`'s order.
    assert _signed_code(tmp_path, _pack(), signer="unknown-signer", today=date(2030, 1, 1)) == "unknown_signer"


# --- SkillSet registry ---------------------------------------------------------------------------
def _second_pack() -> dict:
    return _pack(pack_id="another.skills", skills=[_skill(skill_id="security.threat-model")])
def test_skillset_is_deterministic_regardless_of_input_order() -> None:
    first = SkillSet.from_packs([_load(_pack()), _load(_second_pack())])
    second = SkillSet.from_packs([_load(_second_pack()), _load(_pack())])
    assert first.pack_ids == second.pack_ids == ("another.skills", "cerebro.skills")
    assert first.skill_ids == second.skill_ids == ("security.threat-model", "design.ui-review")
def test_skillset_sorts_skills_within_each_pack() -> None:
    crowded = _pack(skills=[_skill(skill_id="z.last"), _skill(skill_id="a.first")])
    assert SkillSet.from_packs([_load(crowded)]).skill_ids == ("a.first", "z.last")
def test_skillset_rejects_duplicate_pack_ids() -> None:
    with pytest.raises(PackError, match="duplicate_pack_id"):
        SkillSet.from_packs([_load(_pack()), _load(_pack())])
def test_skillset_rejects_cross_pack_skill_collisions() -> None:
    clash = _pack(pack_id="other.skills")
    with pytest.raises(PackError, match="duplicate_skill_id"):
        SkillSet.from_packs([_load(_pack()), _load(clash)])
def test_skillset_resolves_exactly_or_returns_none() -> None:
    skill_set = SkillSet.from_packs([_load(_pack()), _load(_second_pack())])
    assert skill_set.resolve("design.ui-review").version == "1.4.0"
    assert skill_set.resolve("design.ui-revie") is None
    assert skill_set.resolve("absent.skill") is None
def test_skillset_rejects_a_non_string_ref(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="invalid_ref_type"):
        SkillSet.from_packs([_load(_pack())]).resolve(42)
def test_skillset_is_frozen() -> None:
    skill_set = SkillSet.from_packs([_load(_pack())])
    with pytest.raises(Exception):
        skill_set.skills = ()  # type: ignore[misc]
