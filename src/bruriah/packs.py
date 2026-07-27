from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# The version a pack's `min_router_version`/`max_router_version` window is checked against, which
# has to be the version this code actually IS. It was a hardcoded "0.1.0" through 0.3.0, which made
# `check_router_compatibility` compare against a version the package had not been since its first
# release: a pack declaring `min_router_version: "0.4.0"` was rejected by a 0.4.0 router. The gate
# was not dormant, it was answering the wrong question.
#
# NOT to be confused with `BuildConfig.service_version`, which looks like a package version and is
# not one -- see the note at its assignment in `cli.py` before touching it.
from . import __version__ as _ROUTER_VERSION


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
Text = Annotated[str, Field(min_length=1, max_length=2048)]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")]
Version = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
class SourcePolicy(ClosedModel):
    source_id: Identifier
    publisher: Text
    authority: Literal["primary", "official", "standard", "contextual", "unknown"]
    rationale: Text
    claim_types: list[Identifier]
    jurisdictions: list[Text]
    temporal_rules: Text
    citation_rules: Text
    reuse_rules: Text
    freshness_days: Annotated[int, Field(ge=1, le=3650)]
    limitations: list[Text]
    biases: list[Text]
    conflicts: list[Text]
    exclusions: list[Text]
class CapabilityPolicy(ClosedModel):
    capability_id: Identifier
    canonical_distribution: Text
    version: Version
    advisories: list[Text]
    integrity: Text
    permissions: list[Text]
    network_access: list[Text]
    data_access: list[Text]
    limitations: list[Text]
class DomainPack(ClosedModel):
    schema_version: Literal["1"]
    pack_id: Identifier
    version: Version
    maintainer: Text
    min_router_version: Version
    max_router_version: Version
    reviewed_at: date
    expires_at: date
    domains: list[Identifier]
    regulated_domain: bool
    claim_types: list[Identifier]
    jurisdictions: list[Text]
    languages: list[Text]
    freshness_days: Annotated[int, Field(ge=1, le=3650)]
    update_cadence: Text
    license: Text
    provenance: Text
    sources: list[SourcePolicy]
    capabilities: list[CapabilityPolicy]
    @model_validator(mode="after")
    def coherent(self) -> "DomainPack":
        collections = (self.domains, self.claim_types, self.jurisdictions, self.languages, self.sources)
        if any(not values for values in collections):
            raise ValueError("empty_required_registry")
        if self.expires_at <= self.reviewed_at:
            raise ValueError("invalid_review_window")
        for source in self.sources:
            if source.freshness_days > self.freshness_days:
                raise ValueError("source_freshness_exceeds_pack")
        return self
class ReleaseManifest(ClosedModel):
    schema_version: Literal["1"]
    signer: Identifier
    pack_id: Identifier
    version: Version
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    signature: Text
class PackError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
MAX_PACK_BYTES = 65_536
def parse_version(value: str) -> tuple[int, int, int]:
    if not _VERSION_PATTERN.match(value):
        raise PackError("invalid_version")
    return tuple(map(int, value.split(".")))  # type: ignore[return-value]
class _DuplicateKey(Exception):
    pass
def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result
class _StrictYamlLoader(yaml.SafeLoader):
    yaml_implicit_resolvers: dict = {}
    def construct_mapping(self, node, deep: bool = False) -> dict:
        keys = [self.construct_object(key, deep=True) for key, _ in node.value]
        if len(set(keys)) != len(keys):
            raise _DuplicateKey(node.start_mark)
        return super().construct_mapping(node, deep=deep)
for _tag, _pattern, _first in (
    ("tag:yaml.org,2002:null", r"^null$", "n"),
    ("tag:yaml.org,2002:bool", r"^(?:true|false)$", "tf"),
    ("tag:yaml.org,2002:int", r"^-?(?:0|[1-9][0-9]*)$", "-0123456789"),
    ("tag:yaml.org,2002:float", r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$", "-0123456789"),
):
    yaml.add_implicit_resolver(_tag, re.compile(_pattern), list(_first), Loader=_StrictYamlLoader)
def _reject_yaml_aliases(raw: bytes) -> None:
    try:
        tokens = list(yaml.scan(raw.decode("utf-8")))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise PackError("malformed_pack") from error
    if any(isinstance(token, (yaml.AnchorToken, yaml.AliasToken)) for token in tokens):
        raise PackError("malformed_pack")
def parse_pack_bytes(raw: bytes, suffix: str) -> dict:
    if suffix in (".yaml", ".yml"):
        _reject_yaml_aliases(raw)
        try:
            data = yaml.load(raw, Loader=_StrictYamlLoader)
        except _DuplicateKey as error:
            raise PackError("duplicate_key") from error
        except (yaml.YAMLError, TypeError) as error:
            raise PackError("malformed_pack") from error
    else:
        try:
            data = json.loads(raw, object_pairs_hook=_unique_pairs)
        except _DuplicateKey as error:
            raise PackError("duplicate_key") from error
        except ValueError as error:
            raise PackError("malformed_pack") from error
    if not isinstance(data, dict):
        raise PackError("malformed_pack")
    return data
def encode_pack(data: dict) -> str:
    try:
        return json.dumps(data, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PackError("malformed_pack") from error
# Helpers shared with any second pack kind (skill packs). Extraction only: every code and, more
# importantly, every CHECK ORDER below is identical to the inline version it replaced. The order is
# observable -- callers branch on which code surfaces when two conditions fail together -- and is
# pinned by the failure-precedence block in `tests/test_packs.py`, written before this refactor.
def read_pack_bytes(pack_path: Path) -> bytes:
    """Read a pack under the shared size ceiling: unreadable before oversized, both before parsing."""
    try:
        raw = pack_path.read_bytes()
    except OSError as error:
        raise PackError("pack_unreadable") from error
    if len(raw) > MAX_PACK_BYTES:
        raise PackError("pack_too_large")
    return raw
def verify_manifest_bytes(
    raw: bytes, manifest_raw: bytes, trust_roots: dict[str, str], pack_id: str, version: str
) -> ReleaseManifest:
    """Verify a release manifest already held in memory, so a caller that has the manifest bytes
    without a file behind them (a compiled skill-set generation embeds them) verifies through this
    exact path instead of growing a second one.

    Fixed order: malformed_manifest -> unknown_signer -> digest_mismatch -> invalid_signature. Note
    that a valid signature establishes only WHO signed; it is never evidence that the signed content
    is safe."""
    try:
        manifest = ReleaseManifest.model_validate_json(manifest_raw)
    except (ValidationError, ValueError) as error:
        raise PackError("malformed_manifest") from error
    if manifest.signer not in trust_roots:
        raise PackError("unknown_signer")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest.sha256 or (manifest.pack_id, manifest.version) != (pack_id, version):
        raise PackError("digest_mismatch")
    signed = f"1\n{manifest.signer}\n{manifest.pack_id}\n{manifest.version}\n{digest}".encode()
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(trust_roots[manifest.signer], validate=True))
        key.verify(base64.b64decode(manifest.signature, validate=True), signed)
    except (ValueError, InvalidSignature) as error:
        raise PackError("invalid_signature") from error
    return manifest
def verify_manifest(
    raw: bytes, manifest_path: Path, trust_roots: dict[str, str], pack_id: str, version: str
) -> ReleaseManifest:
    """Read a release manifest from disk and verify it. An unreadable manifest is `malformed_manifest`
    exactly as it was before the split, so the observable order is unchanged."""
    try:
        manifest_raw = manifest_path.read_bytes()
    except OSError as error:
        raise PackError("malformed_manifest") from error
    return verify_manifest_bytes(raw, manifest_raw, trust_roots, pack_id, version)
def check_review_window(reviewed_at: date, expires_at: date, freshness_days: int, now: date) -> None:
    """Fixed order: future_review -> expired_pack -> stale_pack."""
    if reviewed_at > now:
        raise PackError("future_review")
    if expires_at < now:
        raise PackError("expired_pack")
    if reviewed_at + timedelta(days=freshness_days) < now:
        raise PackError("stale_pack")
def check_router_compatibility(
    min_router_version: str, max_router_version: str, router_version: str
) -> None:
    if not (
        parse_version(min_router_version)
        <= parse_version(router_version)
        <= parse_version(max_router_version)
    ):
        raise PackError("incompatible_pack")
def check_version_floor(pack_id: str, version: str, minimum_versions: dict[str, str] | None) -> None:
    minimum = (minimum_versions or {}).get(pack_id)
    if minimum and parse_version(version) < parse_version(minimum):
        raise PackError("version_rollback")
def load_pack(
    pack_path: Path,
    manifest_path: Path | None,
    trust_roots: dict[str, str],
    *,
    today: date | None = None,
    router_version: str = _ROUTER_VERSION,
    minimum_versions: dict[str, str] | None = None,
    domain: str | None = None,
    jurisdiction: str | None = None,
    allow_unsigned_local: bool = False,
) -> DomainPack:
    raw = read_pack_bytes(pack_path)
    data = parse_pack_bytes(raw, pack_path.suffix.lower())
    encoded = encode_pack(data)
    try:
        pack = DomainPack.model_validate_json(encoded)
    except (ValidationError, ValueError) as error:
        raise PackError("malformed_pack") from error
    if manifest_path is None:
        if not allow_unsigned_local:
            raise PackError("signature_required")
        if pack.regulated_domain:
            raise PackError("unsigned_regulated_pack")
    else:
        verify_manifest(raw, manifest_path, trust_roots, pack.pack_id, pack.version)
    now = today or date.today()
    check_review_window(pack.reviewed_at, pack.expires_at, pack.freshness_days, now)
    check_router_compatibility(pack.min_router_version, pack.max_router_version, router_version)
    check_version_floor(pack.pack_id, pack.version, minimum_versions)
    if domain and domain not in pack.domains:
        raise PackError("unsupported_domain")
    if jurisdiction and jurisdiction not in pack.jurisdictions:
        raise PackError("unsupported_jurisdiction")
    return pack
