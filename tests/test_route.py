from __future__ import annotations

import ast
import dataclasses
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from cerebro_router.classify import RequestClassification, classify
from cerebro_router.contracts import InvestigationRequest
from cerebro_router.lookup import LookupResult, SourceMatch, discover
from cerebro_router.packs import CapabilityPolicy, SourcePolicy, load_pack
from cerebro_router.registries import Registry
from cerebro_router.route import RouteDecision, RouteError, route

_SRC = Path(__file__).resolve().parents[1] / "src"
_ROUTE_MODULE = _SRC / "cerebro_router" / "route.py"
_DATA = _SRC / "cerebro_router" / "data"


def _classification(intent="investigate", domain="general", claim_type="factual", risk="low", jurisdiction="unknown"):
    return RequestClassification(intent=intent, domain=domain, claim_type=claim_type, risk=risk, jurisdiction=jurisdiction)


def _request(risk_class="low", jurisdiction=None, as_of=None):
    return InvestigationRequest(task="a task", risk_class=risk_class, jurisdiction=jurisdiction, as_of=as_of)


def _no_evidence() -> LookupResult:
    return LookupResult(domain_supported=False, sources=(), capabilities=())


def _source(source_id="src.one", jurisdiction_applicable=True, jurisdictions=("GLOBAL",)) -> SourceMatch:
    policy = SourcePolicy.model_validate({
        "source_id": source_id, "publisher": "Test Publisher", "authority": "official", "rationale": "test",
        "claim_types": ["documentation"], "jurisdictions": list(jurisdictions), "temporal_rules": "n/a",
        "citation_rules": "n/a", "reuse_rules": "n/a", "freshness_days": 30, "limitations": [],
        "biases": [], "conflicts": [], "exclusions": [],
    })
    return SourceMatch(source=policy, jurisdiction_applicable=jurisdiction_applicable)


def _capability(capability_id="cap.one") -> CapabilityPolicy:
    return CapabilityPolicy.model_validate({
        "capability_id": capability_id, "canonical_distribution": "https://example.test/pkg", "version": "1.0.0",
        "advisories": [], "integrity": "digest pinned", "permissions": [], "network_access": [],
        "data_access": [], "limitations": [],
    })


def _with_sources(*matches: SourceMatch) -> LookupResult:
    return LookupResult(domain_supported=True, sources=tuple(matches), capabilities=())


def _real_bundled_registry() -> Registry:
    roots = json.loads((_DATA / "trust-roots.json").read_text())
    pack = load_pack(_DATA / "research-policy.json", _DATA / "research-policy.manifest.json",
                     roots, today=date(2026, 7, 23))
    return Registry.from_packs([pack])


def test_caller_declared_regulated_is_never_swallowed_by_unknown_assessed_risk() -> None:
    # Binding constraint 2: a caller declaring risk_class="regulated" must have routing effect even
    # when the classifier could not assess (risk="unknown", which happens for every unsupported
    # domain). `unknown` is epistemic, not a severity above `regulated`; it must not absorb the
    # declaration and let RULE 3 (regulated-context gate) be skipped.
    unsupported = _classification(domain="unsupported", claim_type="capability_recommendation", risk="unknown")
    with_capability = LookupResult(domain_supported=False, sources=(), capabilities=(_capability(),))
    declared_regulated = route(unsupported, with_capability, _request(risk_class="regulated"))
    assert declared_regulated.effective_risk == "regulated"
    assert declared_regulated.outcome == "route_only"
    assert declared_regulated.reason == "regulated_domain_missing_context"
    # With the default declaration the epistemic-unknown stays unknown and does NOT get over-gated.
    declared_default = route(unsupported, with_capability, _request(risk_class="low"))
    assert declared_default.effective_risk == "unknown"
    assert declared_default.outcome == "proceed"


def test_out_of_domain_assessed_risk_fails_typed_not_bare_keyerror() -> None:
    # RequestClassification is a plain dataclass with no runtime Literal enforcement, so a bad
    # `risk` must raise a typed RouteError, never a bare KeyError from the rank lookup.
    bad = _classification(risk="critical")
    with pytest.raises(RouteError) as caught:
        route(bad, _no_evidence(), _request())
    assert caught.value.code == "invalid_risk_level"


def test_real_pipeline_medical_tool_request_declared_regulated_does_not_proceed() -> None:
    # Full classify -> discover -> route over the REAL signed bundled pack, not hand-built fixtures.
    # "what tool helps with medical diagnosis" classifies unsupported+capability_recommendation; the
    # only real capability surfaces (claim-type gated, per 6B-2), so evidence exists -- but a caller
    # declaring regulated must still be gated, not answered.
    registry = _real_bundled_registry()
    request = InvestigationRequest(task="what tool helps with medical diagnosis", risk_class="regulated")
    classification = classify(request)
    assert classification.domain == "unsupported" and classification.risk == "unknown"
    decision = route(classification, discover(classification, registry), request)
    assert decision.effective_risk == "regulated"
    assert decision.outcome != "proceed"


# --- Binding constraint 1: domain-agnostic abstention gate ---------------------------------

def test_binding_constraint_1_general_and_unsupported_abstain_identically_with_no_pack() -> None:
    general = route(_classification(domain="general", risk="low"), _no_evidence(), _request())
    unsupported = route(_classification(domain="unsupported", risk="unknown"), _no_evidence(), _request())
    assert general.outcome == unsupported.outcome == "abstained"
    assert general.reason == unsupported.reason == "no_approved_pack_or_local_evidence"
    assert general.gaps == unsupported.gaps


@pytest.mark.parametrize("domain,risk", [("law", "regulated"), ("accounting", "regulated"), ("cybersecurity", "high"),
                                          ("programming", "medium"), ("ux_design", "medium"), ("general", "low"), ("unsupported", "unknown")])
def test_binding_constraint_1_gate_ignores_domain_value_for_all_seven_domains(domain, risk) -> None:
    # The gate runs off `LookupResult` alone -- never off `classification.domain`.
    decision = route(_classification(domain=domain, risk=risk), _no_evidence(), _request())
    assert decision.outcome == "abstained"
    assert decision.reason == "no_approved_pack_or_local_evidence"


def test_capability_only_evidence_is_not_abstained_even_when_domain_unsupported() -> None:
    # Capabilities are not domain-gated (lookup.py rule 3); they still count as local evidence.
    lookup = LookupResult(domain_supported=False, sources=(), capabilities=(_capability(),))
    decision = route(_classification(domain="unsupported", claim_type="capability_recommendation"), lookup, _request())
    assert decision.outcome != "abstained"


# --- Binding constraint 2: caller's risk_class is a floor -----------------------------------

def test_binding_constraint_2_caller_declared_risk_elevates_assessed_low_risk() -> None:
    # Classifier assessed "low", caller declares "regulated" -- effective risk must elevate.
    decision = route(_classification(domain="general", risk="low"), _with_sources(_source()),
                      _request(risk_class="regulated", jurisdiction="US", as_of=date(2026, 1, 1)))
    assert decision.effective_risk == "regulated"


@pytest.mark.parametrize(("domain", "assessed_risk"), [("cybersecurity", "high"), ("unsupported", "unknown")])
def test_binding_constraint_2_floor_never_lowers_an_already_higher_assessed_risk(domain, assessed_risk) -> None:
    decision = route(_classification(domain=domain, risk=assessed_risk), _with_sources(_source()),
                      _request(risk_class="low"))
    assert decision.effective_risk == assessed_risk


def test_binding_constraint_2_elevated_floor_triggers_regulated_missing_context() -> None:
    # A caller-elevated risk must feed the SAME regulated-context rule as an assessed one.
    decision = route(_classification(domain="general", risk="low", jurisdiction="unknown"),
                      _with_sources(_source()), _request(risk_class="regulated"))
    assert decision.outcome == "route_only"
    assert decision.reason == "regulated_domain_missing_context"
    assert "missing_jurisdiction" in decision.gaps


# --- Rule 1: consequential action -------------------------------------------------------------

@pytest.mark.parametrize(("has_evidence", "expected_outcome"), [(True, "route_only"), (False, "abstained")])
def test_consequential_action_outcome_depends_only_on_evidence_never_proceeds(has_evidence, expected_outcome) -> None:
    lookup = _with_sources(_source()) if has_evidence else _no_evidence()
    decision = route(_classification(intent="consequential_action", domain="programming", risk="medium"),
                      lookup, _request())
    assert decision.outcome == expected_outcome
    assert decision.reason == "consequential_action_requires_host_action"
    assert decision.requires_escalation is True


def test_consequential_action_outranks_regulated_domain_missing_context() -> None:
    # Rule 1 wins over Rule 3 even for a regulated domain missing jurisdiction.
    decision = route(_classification(intent="consequential_action", domain="law", risk="regulated",
                                      jurisdiction="unknown"), _with_sources(_source()), _request())
    assert decision.reason == "consequential_action_requires_host_action"


# --- Rule 3: regulated domain missing context -------------------------------------------------

@pytest.mark.parametrize(
    ("jurisdiction", "as_of", "expected_gaps"),
    [
        ("unknown", date(2026, 1, 1), ("missing_jurisdiction",)),
        ("US", None, ("missing_effective_date",)),
        ("unknown", None, ("missing_jurisdiction", "missing_effective_date")),
    ],
)
def test_regulated_domain_missing_context_names_each_gap(jurisdiction, as_of, expected_gaps) -> None:
    decision = route(_classification(domain="law", risk="regulated", jurisdiction=jurisdiction),
                      _with_sources(_source()), _request(as_of=as_of))
    assert decision.outcome == "route_only"
    assert decision.gaps == expected_gaps


def test_regulated_domain_with_full_context_does_not_trigger_rule_3() -> None:
    decision = route(_classification(domain="law", risk="regulated", jurisdiction="US"),
                      _with_sources(_source(jurisdictions=("US",))),
                      _request(jurisdiction="US", as_of=date(2026, 1, 1)))
    assert decision.reason != "regulated_domain_missing_context"


# --- Rule 4: jurisdiction-sensitive sources present but inapplicable ------------------------

@pytest.mark.parametrize(
    ("applicable_flags", "expected_outcome", "expected_reason"),
    [
        ((False,), "route_only", "sources_not_jurisdiction_applicable"),
        ((False, True), "proceed", "domain_supported_with_applicable_evidence"),
    ],
)
def test_source_jurisdiction_applicability_gates_outcome(applicable_flags, expected_outcome, expected_reason) -> None:
    matches = tuple(_source(source_id=f"src.{i}", jurisdiction_applicable=flag) for i, flag in enumerate(applicable_flags))
    decision = route(_classification(domain="accounting", risk="regulated", jurisdiction="AR"),
                      _with_sources(*matches), _request(as_of=date(2026, 1, 1)))
    assert decision.outcome == expected_outcome
    assert decision.reason == expected_reason


# --- Rule 5: default proceed -----------------------------------------------------------------

def test_domain_supported_with_applicable_evidence_proceeds() -> None:
    decision = route(_classification(domain="programming", risk="medium"), _with_sources(_source()), _request())
    assert decision.outcome == "proceed"
    assert decision.reason == "domain_supported_with_applicable_evidence"
    assert decision.gaps == ()
    assert decision.requires_escalation is False


# --- Purity, determinism, typing --------------------------------------------------------------

def test_pure_function_same_inputs_same_result() -> None:
    classification, lookup, request = _classification(), _no_evidence(), _request()
    assert route(classification, lookup, request) == route(classification, lookup, request)


def test_determinism_across_hash_seeds() -> None:
    script = (
        "import sys; sys.path.insert(0, %r);"
        "from cerebro_router.classify import RequestClassification;"
        "from cerebro_router.contracts import InvestigationRequest;"
        "from cerebro_router.lookup import LookupResult;"
        "from cerebro_router.route import route;"
        "classification = RequestClassification(intent='investigate', domain='general', "
        "claim_type='factual', risk='low', jurisdiction='unknown');"
        "lookup = LookupResult(domain_supported=False, sources=(), capabilities=());"
        "request = InvestigationRequest(task='t');"
        "decision = route(classification, lookup, request);"
        "print((decision.outcome, decision.reason, decision.effective_risk, decision.gaps, "
        "decision.requires_escalation))" % (str(_SRC),)
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
    ("build_args", "expected_code"),
    [
        (lambda bad: (bad, _no_evidence(), _request()), "invalid_classification_type"),
        (lambda bad: (_classification(), bad, _request()), "invalid_lookup_type"),
        (lambda bad: (_classification(), _no_evidence(), bad), "invalid_request_type"),
    ],
)
def test_typed_error_on_invalid_argument_types(build_args, expected_code) -> None:
    for bad in (None, "not valid", 42, object()):
        with pytest.raises(RouteError) as excinfo:
            route(*build_args(bad))  # type: ignore[arg-type]
        assert excinfo.value.code == expected_code


def test_closed_output_is_frozen() -> None:
    decision = route(_classification(), _no_evidence(), _request())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.outcome = "proceed"  # type: ignore[misc]


def test_no_ranking_or_trust_signal_field_present() -> None:
    forbidden = {"score", "rank", "trust", "recommended", "confidence", "priority"}
    fields = {f.name for f in dataclasses.fields(RouteDecision)}
    assert fields.isdisjoint(forbidden)


def test_route_decision_never_echoes_request_or_classification_text() -> None:
    # `RouteDecision` has no open string field, so it cannot echo `request.task` as a conclusion.
    for field in dataclasses.fields(RouteDecision):
        assert field.type in ("RouteOutcome", "RouteReason", "RiskLevel", "tuple[Gap, ...]", "bool"), field.name


def test_module_performs_no_io_or_network_imports() -> None:
    tree = ast.parse(_ROUTE_MODULE.read_text())
    forbidden = {"sqlite3", "socket", "urllib", "http", "requests", "httpx", "os", "time", "random"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported.isdisjoint(forbidden)


# --- skills count as evidence (Unit 5) -----------------------------------------------------------


def _skill_only_lookup() -> LookupResult:
    """The case that matters: no supported domain, no sources, no capabilities -- a skill is the ONLY
    evidence present. Before Unit 5 this shape abstained."""
    from cerebro_router.lookup import SkillMatch
    from cerebro_router.skills import SkillPack

    from test_skills import _pack as _skill_pack

    pack = SkillPack.model_validate_json(json.dumps(_skill_pack()))
    return LookupResult(
        domain_supported=False, sources=(), capabilities=(),
        skills=(SkillMatch(skill=pack.skills[0], pack_id=pack.pack_id),),
    )


def _investigate() -> RequestClassification:
    return RequestClassification(intent="investigate", domain="programming", claim_type="factual",
                                 risk="low", jurisdiction="unknown")


def test_a_skill_alone_is_enough_evidence_to_proceed() -> None:
    decision = route(_investigate(), _skill_only_lookup(), InvestigationRequest(task="review this ui"))
    assert decision.outcome != "abstained"
    assert "no_approved_domain_pack" not in decision.gaps


def test_the_same_shape_without_the_skill_still_abstains() -> None:
    # MANDATORY negative control. One field differs from the test above. If this stays green, the
    # previous test proves nothing about skills.
    empty = LookupResult(domain_supported=False, sources=(), capabilities=(), skills=())
    decision = route(_investigate(), empty, InvestigationRequest(task="review this ui"))
    assert decision.outcome == "abstained"
    assert "no_approved_domain_pack" in decision.gaps


def test_skills_only_add_evidence_and_never_remove_it() -> None:
    # Additive means additive: adding skills to a lookup that already had evidence must not change
    # the outcome it already produced.
    from cerebro_router.lookup import SkillMatch
    from cerebro_router.skills import SkillPack

    from test_skills import _pack as _skill_pack

    pack = SkillPack.model_validate_json(json.dumps(_skill_pack()))
    supported = LookupResult(domain_supported=True, sources=(), capabilities=(), skills=())
    with_skills = dataclasses.replace(
        supported, skills=(SkillMatch(skill=pack.skills[0], pack_id=pack.pack_id),)
    )
    request = InvestigationRequest(task="review this ui")
    assert route(_investigate(), supported, request).outcome == route(_investigate(), with_skills, request).outcome
