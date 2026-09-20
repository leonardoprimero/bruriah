from __future__ import annotations

import json
import subprocess
from array import array
from datetime import date
from pathlib import Path

import pytest

from bruriah.contracts import InvestigationRequest
from bruriah.corpus import CorpusPolicy, parse_document
from bruriah.gitcorpus import build as build_gitcorpus
from bruriah.index import BuildConfig, build_candidate, promote_candidate, snapshot_active
from bruriah.packs import load_pack
from bruriah.registries import Registry
from bruriah.service import InvestigateService, ServiceDeps

FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
_SRC = Path(__file__).resolve().parents[1] / "src"
_DATA = _SRC / "bruriah" / "data"


def _real_registry() -> Registry:
    roots = json.loads((_DATA / "trust-roots.json").read_text())
    pack = load_pack(
        _DATA / "research-policy.json",
        _DATA / "research-policy.manifest.json",
        roots,
        today=date(2026, 7, 23),
    )
    return Registry.from_packs([pack])


def _embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


def test_corpus_frontmatter_parsing(tmp_path: Path):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['*.md', '**/*.md']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)

    doc_file = tmp_path / "adr.md"
    doc_file.write_text(
        """---
commit: e8f3003bda26
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra="forbid"
    premises:
      - fastmcp-no-forbid
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP derives schemas without extra="forbid"
    status: active
invalidated_premises:
  - legacy-premise
---
# ADR 001: MCP Framework
Decision body text.
""",
        encoding="utf-8",
    )

    doc = parse_document(doc_file, tmp_path, policy)
    assert len(doc.metadata.alternatives) == 1
    assert doc.metadata.alternatives[0]["name"] == "FastMCP"
    assert doc.metadata.alternatives[0]["disposition"] == "rejected"
    assert doc.metadata.alternatives[0]["premises"] == ["fastmcp-no-forbid"]

    assert len(doc.metadata.premises) == 1
    assert doc.metadata.premises[0]["id"] == "fastmcp-no-forbid"
    assert doc.metadata.premises[0]["status"] == "active"

    assert doc.metadata.invalidated_premises == ("legacy-premise",)


def test_gitcorpus_trailers_parsing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True)

    test_file = repo / "main.py"
    test_file.write_text("print('hello')\n")
    subprocess.run(["git", "add", "main.py"], cwd=repo, check=True)

    msg1 = (
        "refactor: use pydantic closed models\n\n"
        "We chose custom closed models over FastMCP.\n\n"
        "Alternative-Rejected: FastMCP\n"
        "Rejection-Reason: Drops unknown fields silently\n"
        "Premise: fastmcp-no-forbid | FastMCP lacks extra forbid\n"
    )
    subprocess.run(["git", "commit", "-m", msg1], cwd=repo, check=True)

    out = tmp_path / "corpus"
    result = build_gitcorpus(repo, out)
    assert result.written == 1

    md_files = list(out.glob("*.md"))
    assert len(md_files) == 1
    content = md_files[0].read_text(encoding="utf-8")
    assert "alternatives:" in content
    assert "name: FastMCP" in content
    assert "fastmcp-no-forbid" in content
    assert "Evaluated Alternatives" in content


def test_investigate_counterfactual_repeat_rejected(tmp_path: Path):
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)

    (vault / "adr-01.md").write_text(
        """---
commit: a1b2c3d4e5f6
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra="forbid"
    premises:
      - fastmcp-no-forbid
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP lacks extra="forbid"
    status: active
---
# ADR 001: Reject FastMCP
We evaluated FastMCP and rejected it due to schema derivation dropping fields without extra="forbid".
""",
        encoding="utf-8",
    )

    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=tmp_path / "vault",
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/minilm",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )
    candidate = tmp_path / "candidate.sqlite3"
    pointer = tmp_path / "active.json"
    build_candidate(config, candidate, policy, _embed)
    promote_candidate(candidate, pointer, config, policy)

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(
            InvestigationRequest(task="migrate server to FastMCP framework")
        )

        assert result.counterfactual_assessment is not None
        assert result.counterfactual_assessment.matched_alternative == "FastMCP"
        assert result.counterfactual_assessment.verdict == "repeat_of_rejected_architecture"
        assert len(result.counterfactual_assessment.supporting_evidence) > 0
        assert any(
            "Task matches rejected architecture 'FastMCP' under active premises" in c
            for c in result.conflicts
        )
        assert len(result.alternatives) == 1
        assert result.alternatives[0].name == "FastMCP"
        assert len(result.premises) == 1
        assert result.premises[0].id == "fastmcp-no-forbid"
        assert result.premises[0].status == "active"


def test_investigate_counterfactual_premise_changed(tmp_path: Path):
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)

    (vault / "adr-01.md").write_text(
        """---
commit: a1b2c3d4e5f6
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra="forbid"
    premises:
      - fastmcp-no-forbid
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP lacks extra="forbid"
    status: active
---
# ADR 001: Reject FastMCP
We evaluated FastMCP and rejected it.
""",
        encoding="utf-8",
    )

    (vault / "adr-02.md").write_text(
        """---
commit: f6e5d4c3b2a1
invalidated_premises:
  - fastmcp-no-forbid
---
# ADR 002: Upstream FastMCP adds extra forbid support
Upstream released FastMCP 2.0 with strict schema forbid support.
""",
        encoding="utf-8",
    )

    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=tmp_path / "vault",
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/minilm",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )
    candidate = tmp_path / "candidate.sqlite3"
    pointer = tmp_path / "active.json"
    build_candidate(config, candidate, policy, _embed)
    promote_candidate(candidate, pointer, config, policy)

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(
            InvestigationRequest(task="migrate server to FastMCP framework")
        )

        assert result.counterfactual_assessment is not None
        assert result.counterfactual_assessment.matched_alternative == "FastMCP"
        assert result.counterfactual_assessment.verdict == "premise_changed_requires_reevaluation"
        assert len(result.counterfactual_assessment.supporting_evidence) >= 1
        assert any(
            "Historical rejection of 'FastMCP' questioned: premise 'fastmcp-no-forbid' was invalidated" in c
            for c in result.conflicts
        )
        assert len(result.premises) == 1
        assert result.premises[0].id == "fastmcp-no-forbid"
        assert result.premises[0].status == "invalidated"
        assert result.premises[0].invalidated_by == "f6e5d4c3b2a1"


def test_investigate_counterfactual_unmatched(tmp_path: Path):
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)

    (vault / "adr-01.md").write_text(
        """---
commit: a1b2c3d4e5f6
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra="forbid"
    premises:
      - fastmcp-no-forbid
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP lacks extra="forbid"
    status: active
---
# ADR 001: Reject FastMCP
Decision content.
""",
        encoding="utf-8",
    )

    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=tmp_path / "vault",
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/minilm",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )
    candidate = tmp_path / "candidate.sqlite3"
    pointer = tmp_path / "active.json"
    build_candidate(config, candidate, policy, _embed)
    promote_candidate(candidate, pointer, config, policy)

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(
            InvestigationRequest(task="unrelated task about database backups")
        )

        assert result.counterfactual_assessment is None
        assert result.alternatives == []
        assert result.premises == []
