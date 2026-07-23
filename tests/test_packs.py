from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from cerebro_router.packs import PackError, load_pack
from cerebro_router.registries import Registry

DATA = Path(__file__).parents[1] / "src/cerebro_router/data"
def _load(tmp_path: Path, mutate=None, **kwargs):
    source = DATA / "research-policy.json"
    pack = json.loads(source.read_text())
    original = json.dumps(pack, sort_keys=True)
    manifest = json.loads((DATA / "research-policy.manifest.json").read_text())
    roots = json.loads((DATA / "trust-roots.json").read_text())
    if mutate:
        mutate(pack, manifest)
    pack_path = tmp_path / "pack.json"
    manifest_path = tmp_path / "manifest.json"
    pack_path.write_bytes(source.read_bytes() if json.dumps(pack, sort_keys=True) == original else json.dumps(pack, separators=(",", ":"), sort_keys=True).encode())
    manifest_path.write_text(json.dumps(manifest))
    kwargs.setdefault("today", date(2026, 7, 23))
    return load_pack(pack_path, manifest_path, roots, **kwargs)
def _code(tmp_path: Path, mutate, **kwargs) -> str:
    with pytest.raises(PackError) as caught:
        _load(tmp_path, mutate, **kwargs)
    return caught.value.code
def test_bundled_pack_is_policy_only_and_registry_is_deterministic(tmp_path: Path) -> None:
    pack = _load(tmp_path)
    assert not hasattr(pack, "answers")
    secondary = pack.model_copy(update={
        "pack_id": "research.secondary",
        "sources": [pack.sources[0].model_copy(update={"source_id": "secondary-project-docs"})],
        "capabilities": [pack.capabilities[0].model_copy(update={"capability_id": "python.secondary-validation"})],
    })
    first = Registry.from_packs([pack, secondary])
    second = Registry.from_packs(list(reversed(first.packs)))
    assert first.pack_ids == second.pack_ids == ("research.minimal", "research.secondary")
    assert first.sources[0].rationale
    assert first.capabilities[0].permissions == []
def test_registry_rejects_cross_pack_identifier_collisions(tmp_path: Path) -> None:
    pack = _load(tmp_path)
    duplicate = pack.model_copy(update={"pack_id": "research.duplicate"})
    with pytest.raises(PackError, match="duplicate_source_id"):
        Registry.from_packs([pack, duplicate])
@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda p, m: p.update({"maintainer": "tampered"}), "digest_mismatch"),
        (lambda p, m: m.update({"signer": "unknown"}), "unknown_signer"),
        (lambda p, m: p.update({"expires_at": "2026-01-01"}), "malformed_pack"),
        (lambda p, m: p.pop("license"), "malformed_pack"),
        (lambda p, m: p["sources"][0].pop("conflicts"), "malformed_pack"),
        (lambda p, m: p["sources"][0].pop("rationale"), "malformed_pack"),
        (lambda p, m: p["sources"][0].update({"freshness_days": 31}), "malformed_pack"),
    ],
)
def test_signed_pack_tampering_fails_closed(tmp_path: Path, mutate, code: str) -> None:
    assert _code(tmp_path, mutate) == code
def test_manifest_and_semantic_gates_have_stable_errors(tmp_path: Path) -> None:
    assert _code(tmp_path, lambda p, m: m.update({"signature": "AA=="})) == "invalid_signature"
    assert _code(tmp_path, None, minimum_versions={"research.minimal": "2.0.0"}) == "version_rollback"
    assert _code(tmp_path, None, router_version="1.0.0") == "incompatible_pack"
    assert _code(tmp_path, None, domain="law") == "unsupported_domain"
    assert _code(tmp_path, None, jurisdiction="AR") == "unsupported_jurisdiction"
def test_version_validation_rejects_malformed_router_and_minimum_versions(tmp_path: Path) -> None:
    assert _code(tmp_path, None, router_version="abc") == "invalid_version"
    assert _code(tmp_path, None, router_version="") == "invalid_version"
    assert _code(tmp_path, None, minimum_versions={"research.minimal": "latest"}) == "invalid_version"
def test_expiry_malformed_oversized_and_unsigned_regulated_fail_closed(tmp_path: Path) -> None:
    assert _code(tmp_path, None, today=date(2028, 1, 1)) == "expired_pack"
    assert _code(tmp_path, None, today=date(2026, 8, 23)) == "stale_pack"
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(PackError, match="malformed_pack"):
        load_pack(bad, None, {}, allow_unsigned_local=True)
    bad.write_bytes(b" " * 65_537)
    with pytest.raises(PackError, match="pack_too_large"):
        load_pack(bad, None, {}, allow_unsigned_local=True)
    unsigned = json.loads((DATA / "research-policy.json").read_text())
    unsigned["regulated_domain"] = True
    bad.write_text(json.dumps(unsigned))
    with pytest.raises(PackError, match="unsigned_regulated_pack"):
        load_pack(bad, None, {}, allow_unsigned_local=True)
def test_yaml_pack_loading_is_deterministic_against_json(tmp_path: Path) -> None:
    source_data = json.loads((DATA / "research-policy.json").read_text())
    json_path = tmp_path / "pack.json"
    json_path.write_text(json.dumps(source_data))
    yaml_path = tmp_path / "pack.yaml"
    yaml_path.write_text(yaml.safe_dump(source_data))
    kwargs = {"allow_unsigned_local": True, "today": date(2026, 7, 23)}
    json_pack = load_pack(json_path, None, {}, **kwargs)
    yaml_pack = load_pack(yaml_path, None, {}, **kwargs)
    assert yaml_pack == json_pack
def test_yaml_pack_rejects_aliases_unsafe_tags_and_non_mapping_root(tmp_path: Path) -> None:
    kwargs = {"allow_unsigned_local": True, "today": date(2026, 7, 23)}
    aliased = tmp_path / "aliased.yaml"
    aliased.write_text("anchor: &a [1, 2]\nalias: *a\n")
    with pytest.raises(PackError, match="malformed_pack"):
        load_pack(aliased, None, {}, **kwargs)
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("!!python/object/apply:builtins.print ['unsafe']\n")
    with pytest.raises(PackError, match="malformed_pack"):
        load_pack(unsafe, None, {}, **kwargs)
    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- one\n- two\n")
    with pytest.raises(PackError, match="malformed_pack"):
        load_pack(not_mapping, None, {}, **kwargs)
def test_duplicate_keys_cannot_silently_downgrade_controls(tmp_path: Path) -> None:
    kwargs = {"allow_unsigned_local": True, "today": date(2026, 7, 23)}
    source = json.loads((DATA / "research-policy.json").read_text())
    regulated = {key: value for key, value in source.items() if key != "regulated_domain"}
    downgrade = "regulated_domain: true\nregulated_domain: false\n"
    yaml_path = tmp_path / "dup.yaml"
    yaml_path.write_text(yaml.safe_dump(regulated) + downgrade)
    with pytest.raises(PackError, match="duplicate_key"):
        load_pack(yaml_path, None, {}, **kwargs)
    json_path = tmp_path / "dup.json"
    json_path.write_text(json.dumps(regulated)[:-1] + ',"regulated_domain":true,"regulated_domain":false}')
    with pytest.raises(PackError, match="duplicate_key"):
        load_pack(json_path, None, {}, **kwargs)
@pytest.mark.parametrize(
    "body",
    [
        "data: !!binary |\n  UGFuaWM=\n",
        "maintainer: 2026-07-23\n",
        "maintainer: 2026-07-23 10:00:00\n",
        "freshness_days: .nan\n",
    ],
)
def test_yaml_implicit_non_json_types_fail_closed(tmp_path: Path, body: str) -> None:
    path = tmp_path / "implicit.yaml"
    path.write_text(body)
    with pytest.raises(PackError, match="malformed_pack"):
        load_pack(path, None, {}, allow_unsigned_local=True, today=date(2026, 7, 23))
def _unsigned_yaml(tmp_path: Path, drop: set[str], extra: str):
    source = json.loads((DATA / "research-policy.json").read_text())
    body = yaml.safe_dump({key: value for key, value in source.items() if key not in drop}, sort_keys=True)
    path = tmp_path / "core.yaml"
    path.write_text(body + extra)
    return load_pack(path, None, {}, allow_unsigned_local=True, today=date(2026, 7, 23))
def test_yaml_resolves_only_json_core_types(tmp_path: Path) -> None:
    with pytest.raises(PackError, match="malformed_pack"):
        _unsigned_yaml(tmp_path, {"freshness_days"}, "freshness_days: 1:20\n")
    with pytest.raises(PackError, match="malformed_pack"):
        _unsigned_yaml(tmp_path, {"regulated_domain"}, "regulated_domain: yes\n")
    assert _unsigned_yaml(tmp_path, {"freshness_days"}, "freshness_days: 30\n").freshness_days == 30
    assert _unsigned_yaml(tmp_path, {"jurisdictions"}, "jurisdictions: [NO, ON]\n").jurisdictions == ["NO", "ON"]
    assert _unsigned_yaml(tmp_path, {"expires_at"}, "expires_at: 2027-07-23\n").expires_at == date(2027, 7, 23)
    assert _unsigned_yaml(tmp_path, {"regulated_domain"}, "regulated_domain: false\n").regulated_domain is False
def test_unsigned_future_review_and_malformed_manifest_fail_closed(tmp_path: Path) -> None:
    roots = json.loads((DATA / "trust-roots.json").read_text())
    source = DATA / "research-policy.json"
    with pytest.raises(PackError, match="signature_required"):
        load_pack(source, None, roots, today=date(2026, 7, 23))
    future = json.loads(source.read_text())
    future.update({"reviewed_at": "2027-01-01", "expires_at": "2028-01-01"})
    future_path = tmp_path / "future.json"
    future_path.write_text(json.dumps(future))
    with pytest.raises(PackError, match="future_review"):
        load_pack(future_path, None, {}, allow_unsigned_local=True, today=date(2026, 7, 23))
    broken = tmp_path / "manifest.json"
    broken.write_text("{")
    with pytest.raises(PackError, match="malformed_manifest"):
        load_pack(source, broken, roots, today=date(2026, 7, 23))
