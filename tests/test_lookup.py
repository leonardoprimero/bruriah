from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
import typing
from datetime import date
from pathlib import Path

import pytest
from cerebro_router.classify import Domain, RequestClassification
from cerebro_router.lookup import (
    LookupError,
    LookupResult,
    SourceMatch,
    discover,
    resolve_capability,
    resolve_source,
)
from cerebro_router.packs import DomainPack, load_pack
from cerebro_router.registries import Registry

_SRC = Path(__file__).resolve().parents[1] / "src"
_LOOKUP_MODULE = _SRC / "cerebro_router" / "lookup.py"
_DATA = _SRC / "cerebro_router" / "data"


def _real_bundled_registry() -> Registry:
    roots = json.loads((_DATA / "trust-roots.json").read_text())
    pack = load_pack(_DATA / "research-policy.json", _DATA / "research-policy.manifest.json",
                     roots, today=date(2026, 7, 23))
    return Registry.from_packs([pack])


def test_real_bundled_pack_resolves_no_domain_sources_for_any_classifier_domain() -> None:
    # The RESEARCH pack declares domains=["software-research"], which no classifier Domain value
    # equals, so on its own it resolves no sources for ANY of the seven domains -- not just
    # general/unsupported. The bundled `programming.minimal` pack is what reconciles the two
    # vocabularies, and it is pinned separately so this negative stays meaningful in isolation. This is spec-compliant (no approved pack for the domain -> abstain,
    # via 6B-3) but must be PINNED against the real pack, not only a synthetic domains=("general",)
    # fixture: an accidental edit to research-policy.json's domains, or a classifier/pack
    # vocabulary drift, must fail a test rather than silently change routing. Reconciliation of the
    # two vocabularies belongs to future pack authoring, never to lookup.py.
    registry = _real_bundled_registry()
    for domain in typing.get_args(Domain):
        result = discover(RequestClassification(intent="investigate", domain=domain,
                          claim_type="factual", risk="low", jurisdiction="unknown"), registry)
        assert result.domain_supported is False, domain
        assert result.sources == (), domain
    # Capabilities are claim-type gated, not domain-gated, so they DO surface for the real pack.
    caps = discover(RequestClassification(intent="investigate", domain="programming",
                    claim_type="capability_recommendation", risk="medium", jurisdiction="unknown"), registry)
    assert len(caps.capabilities) == 1
    assert caps.capabilities[0].capability_id == "python.schema-validation"


def _source_data(source_id="src.one", jurisdictions=("GLOBAL",), authority="official", claim_types=("documentation",)):
    return {"source_id": source_id, "publisher": "Test Publisher", "authority": authority, "rationale": "test rationale",
            "claim_types": list(claim_types), "jurisdictions": list(jurisdictions), "temporal_rules": "n/a",
            "citation_rules": "n/a", "reuse_rules": "n/a", "freshness_days": 30, "limitations": ["test only"],
            "biases": [], "conflicts": [], "exclusions": []}


def _capability_data(capability_id="cap.one"):
    return {"capability_id": capability_id, "canonical_distribution": "https://example.test/pkg", "version": "1.0.0",
            "advisories": ["check advisory feed"], "integrity": "digest pinned", "permissions": ["read"],
            "network_access": [], "data_access": [], "limitations": ["discovery only"]}


def _pack_data(pack_id="pack.one", domains=("programming",), sources=None, capabilities=None):
    return {"schema_version": "1", "pack_id": pack_id, "version": "1.0.0", "maintainer": "test",
            "min_router_version": "0.1.0", "max_router_version": "9.9.9", "reviewed_at": "2026-01-01",
            "expires_at": "2027-01-01", "domains": list(domains), "regulated_domain": False,
            "claim_types": ["documentation"], "jurisdictions": ["GLOBAL"], "languages": ["en"], "freshness_days": 365,
            "update_cadence": "monthly", "license": "CC0-1.0", "provenance": "test fixture",
            "sources": sources if sources is not None else [_source_data()],
            "capabilities": capabilities if capabilities is not None else [_capability_data()]}


def _pack(**kwargs) -> DomainPack:
    return DomainPack.model_validate_json(json.dumps(_pack_data(**kwargs)))


def _registry(*packs: DomainPack) -> Registry:
    return Registry.from_packs(list(packs))


def _classification(domain="programming", jurisdiction="unknown", claim_type="factual", risk="medium"):
    return RequestClassification(intent="investigate", domain=domain, claim_type=claim_type, risk=risk, jurisdiction=jurisdiction)


def test_pure_function_same_inputs_same_result() -> None:
    registry = _registry(_pack())
    classification = _classification()
    assert discover(classification, registry) == discover(classification, registry)


def test_determinism_across_hash_seeds() -> None:
    pack_data = _pack_data(sources=[_source_data(jurisdictions=("AR",))], capabilities=[_capability_data()])
    script = (
        "import json, sys; sys.path.insert(0, %r);"
        "from cerebro_router.classify import RequestClassification;"
        "from cerebro_router.lookup import discover;"
        "from cerebro_router.packs import DomainPack;"
        "from cerebro_router.registries import Registry;"
        "pack = DomainPack.model_validate_json(json.dumps(%r));"
        "registry = Registry.from_packs([pack]);"
        "classification = RequestClassification(intent='investigate', domain='programming', "
        "claim_type='capability_recommendation', risk='medium', jurisdiction='AR');"
        "result = discover(classification, registry);"
        "print((result.domain_supported, "
        "[(m.source.source_id, m.jurisdiction_applicable) for m in result.sources], "
        "[c.capability_id for c in result.capabilities]))"
        % (str(_SRC), pack_data)
    )
    outputs = set()
    for seed in ("0", "1", "42", "2026"):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outputs.add(result.stdout)
    assert len(outputs) == 1


@pytest.mark.parametrize(
    ("classification_domain", "pack_domains", "expected_supported", "expected_ids"),
    [
        ("programming", ("programming",), True, ["src.one"]),
        ("programming", ("law",), False, []),
        ("general", ("law", "accounting"), False, []),
        ("unsupported", ("law", "accounting"), False, []),
        ("general", ("general",), True, ["src.one"]),  # structural, not a hardcoded exclusion
    ],
)
def test_domain_gating_is_structural_set_membership(classification_domain, pack_domains, expected_supported, expected_ids) -> None:
    # Binding constraint for 6B-3: `general`/`unsupported` naturally have no matching pack
    # unless one explicitly declares that identifier -- this is the signal 6B-3 depends on.
    result = discover(_classification(domain=classification_domain), _registry(_pack(domains=pack_domains)))
    assert result.domain_supported is expected_supported
    assert [match.source.source_id for match in result.sources] == expected_ids


def test_empty_registry_yields_explicit_empty_result() -> None:
    registry = _registry()
    result = discover(_classification(), registry)
    assert result == LookupResult(domain_supported=False, sources=(), capabilities=())


def test_jurisdiction_mismatch_is_visible_not_silently_hidden() -> None:
    sources = [_source_data(source_id="src.ar", jurisdictions=("AR",)), _source_data(source_id="src.us", jurisdictions=("US",))]
    result = discover(_classification(domain="programming", jurisdiction="AR"), _registry(_pack(sources=sources)))
    applicability = {match.source.source_id: match.jurisdiction_applicable for match in result.sources}
    assert applicability == {"src.ar": True, "src.us": False}


@pytest.mark.parametrize(
    ("jurisdictions", "expected"),
    [(("GLOBAL",), True), (("AR",), False)],  # GLOBAL always applies; "unknown" never affirmatively matches
)
def test_unknown_classification_jurisdiction_vs_global_and_specific_sources(jurisdictions, expected: bool) -> None:
    registry = _registry(_pack(sources=[_source_data(jurisdictions=jurisdictions)]))
    result = discover(_classification(domain="programming", jurisdiction="unknown"), registry)
    assert result.sources[0].jurisdiction_applicable is expected


def test_capabilities_returned_only_for_capability_recommendation_claim_type() -> None:
    registry = _registry(_pack())
    factual = discover(_classification(claim_type="factual"), registry)
    capability = discover(_classification(claim_type="capability_recommendation"), registry)
    assert factual.capabilities == ()
    assert [item.capability_id for item in capability.capabilities] == ["cap.one"]


def test_capabilities_are_not_domain_gated() -> None:
    # Design decision 3: a domain matching no pack must still surface capabilities.
    registry = _registry(_pack(domains=("law",), capabilities=[_capability_data()]))
    result = discover(_classification(domain="general", claim_type="capability_recommendation"), registry)
    assert result.domain_supported is False
    assert [item.capability_id for item in result.capabilities] == ["cap.one"]


def test_authority_is_disclosed_but_never_filters_a_match() -> None:
    sources = [_source_data(source_id="src.official", authority="official"), _source_data(source_id="src.unknown", authority="unknown")]
    result = discover(_classification(domain="programming"), _registry(_pack(sources=sources)))
    assert {match.source.source_id for match in result.sources} == {"src.official", "src.unknown"}


def test_result_preserves_registry_order_not_reordered() -> None:
    registry = _registry(
        _pack(pack_id="pack.b", domains=("programming",),
              sources=[_source_data(source_id="src.z")], capabilities=[_capability_data(capability_id="cap.z")]),
        _pack(pack_id="pack.a", domains=("programming",),
              sources=[_source_data(source_id="src.y")], capabilities=[_capability_data(capability_id="cap.y")]),
    )
    result = discover(_classification(domain="programming"), registry)
    assert [match.source.source_id for match in result.sources] == [s.source_id for s in registry.sources]


def test_resolvers_never_fabricate_and_never_clone() -> None:
    registry = _registry(_pack())
    assert resolve_source("does-not-exist", registry) is None
    assert resolve_capability("does-not-exist", registry) is None
    assert resolve_source("src.one", registry) is registry.sources[0]
    assert resolve_capability("cap.one", registry) is registry.capabilities[0]


def test_disclosed_capability_fields_are_preserved_verbatim() -> None:
    registry = _registry(_pack())
    result = discover(_classification(claim_type="capability_recommendation"), registry)
    capability = result.capabilities[0]
    assert (capability.integrity, capability.advisories, capability.permissions, capability.limitations) == (
        "digest pinned", ["check advisory feed"], ["read"], ["discovery only"],
    )


@pytest.mark.parametrize("bad_classification", [None, "not a classification", 42, object()])
def test_typed_error_on_invalid_classification_type(bad_classification: object) -> None:
    with pytest.raises(LookupError) as excinfo:
        discover(bad_classification, _registry())  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_classification_type"


@pytest.mark.parametrize("bad_registry", [None, "not a registry", 42, object()])
def test_typed_error_on_invalid_registry_type(bad_registry: object) -> None:
    with pytest.raises(LookupError) as excinfo:
        discover(_classification(), bad_registry)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_registry_type"


@pytest.mark.parametrize("resolver", [resolve_source, resolve_capability])
def test_typed_error_on_invalid_ref_and_registry_type(resolver) -> None:
    for bad_ref in (None, 42, object(), ["a"]):
        with pytest.raises(LookupError) as excinfo:
            resolver(bad_ref, _registry())  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_ref_type"
    with pytest.raises(LookupError) as excinfo:
        resolver("some-id", None)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_registry_type"


def test_closed_output_is_frozen() -> None:
    registry = _registry(_pack())
    result = discover(_classification(), registry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.domain_supported = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.sources[0].jurisdiction_applicable = True  # type: ignore[misc]


def test_no_ranking_or_trust_signal_field_present() -> None:
    # Requirement 2: rank/relevance must never leak in as a safety or trust signal field.
    forbidden = {"score", "rank", "trust", "recommended", "confidence", "priority"}
    fields = {f.name for f in dataclasses.fields(LookupResult)} | {f.name for f in dataclasses.fields(SourceMatch)}
    assert fields.isdisjoint(forbidden)


def test_module_performs_no_io_or_network_imports() -> None:
    tree = ast.parse(_LOOKUP_MODULE.read_text())
    forbidden = {"sqlite3", "socket", "urllib", "http", "requests", "httpx", "os"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden)
