from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

from .packs import parse_pack_bytes, read_pack_bytes

# Release signing: the private half of the trust root.
#
# This is the counterpart to `packs.verify_manifest_bytes`, and it exists as an importable module
# rather than a standalone script for one reason: `skill-sign` in the CLI and the release script must
# produce byte-identical manifests. Two implementations of the same canonical signing string is
# exactly how a verifier and a signer drift apart.
#
# What signing does NOT do, stated here because it is the most common misreading: it does not
# validate the pack. A signature establishes WHO signed a byte sequence and nothing else -- not that
# the content is well-formed, current, or safe. `verify_manifest` says the same thing from the other
# side. Whoever signs is asserting authorship, and the fail-closed load path is what decides whether
# the signed bytes are acceptable.
#
# The private key is ALWAYS addressed by path and is never copied, never written anywhere else, and
# never printed. `generate_key` refuses to write inside the installed package, which is where
# `data_dir` and the bundled trust roots live.

_SIGNING_SCHEMA = "1"


class SigningError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _package_directory() -> Path:
    return Path(__file__).resolve().parent


def generate_key(destination: Path) -> str:
    """Generate an Ed25519 release key at `destination` and return ONLY its public half, base64
    encoded, ready to paste into `trust-roots.json`.

    The private half is never returned, never logged, and never printed. Refusals are deliberate:
    an existing file is never overwritten (losing a release key silently would be unrecoverable, and
    `O_EXCL` makes the check race-free), and the destination may not live inside the installed
    package, because that is where `data_dir` and the bundled trust roots are.

    Limit, stated rather than implied: the key is written unencrypted, protected by `0600` and by
    living outside the repository. That is adequate for a single maintainer on an encrypted disk and
    inadequate on a shared machine. Adding a passphrase is a small change to this function and a
    larger one to every caller of `sign_pack`."""
    resolved = destination.resolve() if destination.parent.exists() else destination.absolute()
    if _package_directory() in resolved.parents:
        raise SigningError("key_inside_package")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise SigningError("key_exists") from error
    except OSError as error:
        raise SigningError("key_unwritable") from error
    try:
        os.write(descriptor, pem)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return _encode_public(key)


def load_public(key_path: Path) -> str:
    """Return the base64 public half of an existing release key, so `trust-roots.json` can be
    populated without the private key ever leaving the file it lives in."""
    return _encode_public(_load_private(key_path))


def sign_pack(
    key_path: Path, signer: str, pack_path: Path, manifest_path: Path | None = None
) -> Path:
    """Sign `pack_path` and write its release manifest, returning the manifest path.

    `pack_id` and `version` are read FROM THE PACK, never taken as arguments. A manifest whose
    identity disagrees with the pack it signs is rejected at load as `digest_mismatch`, so accepting
    them separately would only create a way to produce a manifest that can never verify.

    The digest covers the pack file's bytes exactly as they are on disk, because that is what
    `verify_manifest_bytes` recomputes. The pack is deliberately NOT reformatted or re-encoded here:
    a signer that rewrites what it signs would invalidate its own signature."""
    key = _load_private(key_path)
    raw = read_pack_bytes(pack_path)
    data = parse_pack_bytes(raw, pack_path.suffix.lower())
    pack_id, version = data.get("pack_id"), data.get("version")
    if not isinstance(pack_id, str) or not isinstance(version, str):
        raise SigningError("pack_identity_missing")
    digest = hashlib.sha256(raw).hexdigest()
    signed = f"{_SIGNING_SCHEMA}\n{signer}\n{pack_id}\n{version}\n{digest}".encode()
    manifest = {
        "schema_version": _SIGNING_SCHEMA,
        "signer": signer,
        "pack_id": pack_id,
        "version": version,
        "sha256": digest,
        "signature": base64.b64encode(key.sign(signed)).decode("ascii"),
    }
    destination = manifest_path or pack_path.with_suffix(".manifest.json")
    # `newline="\n"` is load-bearing, not tidiness: this manifest is later re-read and compared
    # against a digest. Text mode translates "\n" to the platform separator on write, so a manifest
    # produced on Windows would be CRLF, hash differently from the identical manifest produced
    # anywhere else, and fail its own verification. Machine-verified bytes get written explicitly.
    destination.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return destination


def _load_private(key_path: Path) -> Ed25519PrivateKey:
    try:
        key = load_pem_private_key(key_path.read_bytes(), password=None)
    except OSError as error:
        raise SigningError("key_unreadable") from error
    except (ValueError, TypeError) as error:
        raise SigningError("malformed_key") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("unsupported_key_type")
    return key


def _encode_public(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")


__all__ = [
    "SigningError",
    "generate_key",
    "load_public",
    "sign_pack",
]
