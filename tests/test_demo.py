from __future__ import annotations

import io
import json
from array import array
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from bruriah.cli import bruriah_main
from bruriah.contracts import Budgets, InvestigationRequest, ReadItem, ReadRequest, ReadResult
from bruriah.corpus import CorpusPolicy
from bruriah.demo import _alternative_name, run_demo
from bruriah.index import BuildConfig, build_candidate, promote_candidate, snapshot_active
from bruriah.packs import load_pack
from bruriah.registries import Registry
from bruriah.service import InvestigateService, ServiceDeps, read

FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
_SRC = Path(__file__).resolve().parents[1] / "src"
_DATA = _SRC / "bruriah" / "data"


def _real_registry() -> Registry:
    roots = json.loads((_DATA / "trust-roots.json").read_text())
    pack = load_pack(
        _DATA / "research-policy.json", _DATA / "research-policy.manifest.json",
        roots, today=date(2026, 7, 23),
    )
    return Registry.from_packs([pack])


def _embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


def test_run_demo_non_interactive():
    buf = io.StringIO()
    exit_code = run_demo(interactive=False, use_color=False, stdout=buf)
    assert exit_code == 0
    output = buf.getvalue()
    assert "Step 1: Storing Initial Architectural Decision" in output
    assert "Step 2: Agent Proposes Discarded Architecture" in output
    assert "🛑 VERDICT: repeat_of_rejected_architecture" in output
    assert "Step 3: Premise Invalidation & Re-evaluation" in output
    assert "🔄 VERDICT: premise_changed_requires_reevaluation" in output
    assert "Demo completed successfully!" in output



def test_demo_cli_dispatch():
    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        code = bruriah_main(["demo", "--non-interactive", "--no-color"])
        assert code == 0
        output = mock_stdout.getvalue()
        assert "Step 1: Storing Initial Architectural Decision" in output
        assert "repeat_of_rejected_architecture" in output
        assert "premise_changed_requires_reevaluation" in output


def test_alternative_name_falls_back_to_the_ref_when_the_read_is_truncated(tmp_path: Path) -> None:
    """R3-demo-alt-name-truncated-json (T4.1): a `reason` long enough to exceed
    `read_evidence`'s default 20,000-char output budget makes the alternative's JSON disclosure
    come back truncated (`item.truncated=True`, `item.content` cut off mid-string, not valid
    JSON). `_alternative_name` must degrade to the raw ref rather than crash on
    `json.JSONDecodeError`.

    R3-demo-truncation-precondition-unasserted (T5): the fixture is only exercising the
    truncated-read fallback if the read for this ref actually comes back truncated -- asserted
    directly here before the fallback assertion, so a change that stopped truncating (a larger
    default budget, a shorter fixture reason) would fail this test for the right reason instead
    of silently no longer testing what it claims to."""
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)
    long_reason = "Too verbose to summarize in a short sentence. " * 1000
    (vault / "adr-01.md").write_text(
        f"""---
commit: a1b2c3d4e5f6
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: "{long_reason}"
---
# ADR 001: Reject FastMCP
We evaluated FastMCP and rejected it.
""",
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=tmp_path / "vault", policy_path=policy_path, schema_version=1, parser_version="corpus-v2",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model="test/minilm",
        embedding_revision="snapshot-a", embedding_dimensions=3, embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )
    candidate = tmp_path / "candidate.sqlite3"
    pointer = tmp_path / "active.json"
    build_candidate(config, candidate, policy, _embed)
    promote_candidate(candidate, pointer, config, policy)

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        assert result.counterfactual_assessment is not None
        ref = result.counterfactual_assessment.matched_alternative_ref

        precondition = read(ReadRequest(refs=[ref]), deps).items[0]
        assert precondition.status == "ok"
        assert precondition.truncated is True

        assert _alternative_name(ref, deps) == ref


def _ok_read_result(ref: str, content: str) -> ReadResult:
    item = ReadItem(ref=ref, status="ok", content=content, truncated=False, evidence_kind="alternative")
    return ReadResult(schema_version="1", request_id="stub-request", items=[item], warnings=[], budgets=Budgets())


def test_alternative_name_falls_back_to_the_ref_when_the_disclosure_is_not_json(monkeypatch) -> None:
    """T5: `_alternative_name` must degrade to the raw ref, not raise, when a complete
    (non-truncated) `ok` read's content is not valid JSON at all."""
    ref = "alt:v1:" + "a" * 64
    monkeypatch.setattr("bruriah.demo.read", lambda request, deps: _ok_read_result(ref, "not json at all"))

    assert _alternative_name(ref, deps=None) == ref  # type: ignore[arg-type]


def test_alternative_name_falls_back_to_the_ref_when_the_disclosure_is_not_an_object(monkeypatch) -> None:
    """T5: valid JSON that decodes to something other than an object (a list, here) has no
    `name` to read, so `_alternative_name` falls back to the raw ref rather than crash on
    `.get`."""
    ref = "alt:v1:" + "b" * 64
    monkeypatch.setattr("bruriah.demo.read", lambda request, deps: _ok_read_result(ref, "[1, 2, 3]"))

    assert _alternative_name(ref, deps=None) == ref  # type: ignore[arg-type]


def test_alternative_name_falls_back_to_the_ref_when_the_object_has_no_string_name(monkeypatch) -> None:
    """T5: a well-formed JSON object missing a string `name` field (either absent entirely or
    present with a non-string value) falls back to the raw ref rather than returning a non-string
    or `None`."""
    ref = "alt:v1:" + "c" * 64
    monkeypatch.setattr(
        "bruriah.demo.read", lambda request, deps: _ok_read_result(ref, json.dumps({"disposition": "rejected"}))
    )

    assert _alternative_name(ref, deps=None) == ref  # type: ignore[arg-type]
