from __future__ import annotations

import base64
import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from bruriah import signing
from bruriah.packs import PackError, load_pack
from bruriah.signing import SigningError, generate_key, load_public, sign_pack
from bruriah.skills import load_skill_pack
from test_skills import _pack as _skill_pack

# The strongest available assertion about a signer is that its output verifies through the UNMODIFIED
# verification path, so these tests sign the real bundled `research-policy.json` and load it with the
# production `load_pack`. Signing an artificial fixture would only prove the tool agrees with itself.

DATA = Path(signing.__file__).resolve().parent / "data"
BUNDLED = DATA / "research-policy.json"
TODAY = date(2026, 7, 23)
SIGNER = "bruriah-release"


def _key(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "release-key.pem"
    return path, {SIGNER: generate_key(path)}


def _signed_copy(tmp_path: Path, source: Path = BUNDLED) -> tuple[Path, Path, dict[str, str]]:
    key_path, roots = _key(tmp_path)
    pack = tmp_path / source.name
    pack.write_bytes(source.read_bytes())
    return pack, sign_pack(key_path, SIGNER, pack), roots


def _code(callable_, *args, **kwargs) -> str:
    with pytest.raises(SigningError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


# --- key generation ------------------------------------------------------------------------------


def test_keygen_returns_only_the_public_half(tmp_path: Path) -> None:
    path = tmp_path / "release-key.pem"
    public = generate_key(path)
    assert len(base64.b64decode(public, validate=True)) == 32
    assert public not in path.read_text()
    assert b"PRIVATE KEY" in path.read_bytes()


def test_keygen_writes_an_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "release-key.pem"
    generate_key(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_keygen_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "config" / "cerebro" / "release-key.pem"
    assert len(base64.b64decode(generate_key(path), validate=True)) == 32


def test_keygen_never_overwrites_an_existing_key(tmp_path: Path) -> None:
    # Silently replacing a release key would be unrecoverable: every pack it signed becomes
    # unverifiable and the loss is invisible until someone tries to load one.
    path = tmp_path / "release-key.pem"
    generate_key(path)
    before = path.read_bytes()
    assert _code(generate_key, path) == "key_exists"
    assert path.read_bytes() == before


def test_keygen_refuses_to_write_inside_the_installed_package(tmp_path: Path) -> None:
    target = DATA / "should-never-exist.pem"
    assert _code(generate_key, target) == "key_inside_package"
    assert not target.exists()


def test_public_matches_what_generation_reported(tmp_path: Path) -> None:
    path = tmp_path / "release-key.pem"
    public = generate_key(path)
    assert load_public(path) == public


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        (lambda p: None, "key_unreadable"),
        (lambda p: p.write_text("not a key"), "malformed_key"),
    ],
)
def test_unusable_keys_fail_closed(tmp_path: Path, prepare, code: str) -> None:
    path = tmp_path / "key.pem"
    prepare(path)
    assert _code(load_public, path) == code


def test_a_non_ed25519_key_is_refused(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    path = tmp_path / "rsa.pem"
    path.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        )
    )
    assert _code(load_public, path) == "unsupported_key_type"


# --- signing verifies through the production path -------------------------------------------------


def test_a_signed_bundled_pack_loads_through_the_unmodified_path(tmp_path: Path) -> None:
    pack, manifest, roots = _signed_copy(tmp_path)
    loaded = load_pack(pack, manifest, roots, today=TODAY)
    assert loaded.pack_id == "research.minimal"


def test_a_tampered_byte_breaks_the_signature(tmp_path: Path) -> None:
    pack, manifest, roots = _signed_copy(tmp_path)
    data = json.loads(pack.read_text())
    data["maintainer"] = "tampered"
    pack.write_text(json.dumps(data))
    with pytest.raises(PackError) as caught:
        load_pack(pack, manifest, roots, today=TODAY)
    assert caught.value.code == "digest_mismatch"


def test_a_signer_absent_from_trust_roots_is_rejected(tmp_path: Path) -> None:
    pack, manifest, roots = _signed_copy(tmp_path)
    with pytest.raises(PackError) as caught:
        load_pack(pack, manifest, {"somebody-else": next(iter(roots.values()))}, today=TODAY)
    assert caught.value.code == "unknown_signer"


def test_another_key_cannot_impersonate_the_signer(tmp_path: Path) -> None:
    # Same signer id, same digest, different key. Only the Ed25519 check can catch this.
    pack, _, roots = _signed_copy(tmp_path)
    impostor = tmp_path / "impostor.pem"
    generate_key(impostor)
    forged = sign_pack(impostor, SIGNER, pack, tmp_path / "forged.manifest.json")
    with pytest.raises(PackError) as caught:
        load_pack(pack, forged, roots, today=TODAY)
    assert caught.value.code == "invalid_signature"


def test_signing_a_skill_pack_verifies_through_load_skill_pack(tmp_path: Path) -> None:
    key_path, roots = _key(tmp_path)
    pack = tmp_path / "skills.json"
    pack.write_text(json.dumps(_skill_pack()))
    manifest = sign_pack(key_path, SIGNER, pack)
    assert load_skill_pack(pack, manifest, roots, today=date(2026, 7, 25)).pack_id == "bruriah.skills"


# --- what signing refuses to do -------------------------------------------------------------------


def test_signing_does_not_rewrite_the_pack_it_signs(tmp_path: Path) -> None:
    # A signer that reformatted its input would invalidate its own signature.
    pack, _, _ = _signed_copy(tmp_path)
    assert hashlib.sha256(pack.read_bytes()).hexdigest() == hashlib.sha256(BUNDLED.read_bytes()).hexdigest()


def test_identity_comes_from_the_pack_not_from_arguments(tmp_path: Path) -> None:
    pack, manifest, _ = _signed_copy(tmp_path)
    recorded = json.loads(manifest.read_text())
    source = json.loads(pack.read_text())
    assert (recorded["pack_id"], recorded["version"]) == (source["pack_id"], source["version"])


def test_a_pack_without_an_identity_cannot_be_signed(tmp_path: Path) -> None:
    key_path, _ = _key(tmp_path)
    pack = tmp_path / "anonymous.json"
    pack.write_text(json.dumps({"schema_version": "1"}))
    assert _code(sign_pack, key_path, SIGNER, pack) == "pack_identity_missing"


def test_signing_never_writes_into_the_installed_package(tmp_path: Path) -> None:
    # The tool addresses the private key by path and must leave the bundled data directory exactly as
    # it found it -- no key material, no stray manifest.
    # Files only: `data/` now also holds the `skills/` body directory, and the point of this
    # snapshot is that no key material appears, not that the tree is flat.
    before = {item.name: item.read_bytes() for item in sorted(DATA.iterdir()) if item.is_file()}
    _signed_copy(tmp_path)
    generate_key(tmp_path / "another.pem")
    after = {item.name: item.read_bytes() for item in sorted(DATA.iterdir()) if item.is_file()}
    assert after == before
