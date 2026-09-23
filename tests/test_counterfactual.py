from __future__ import annotations

import hashlib
import json
import subprocess
from array import array
from datetime import date
from pathlib import Path

import pytest

from bruriah.contracts import InvestigationRequest
from bruriah.corpus import CorpusPolicy, alternative_ref_for, parse_document, premise_ref_for
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


def _doc_ref(relative_path: str) -> str:
    """The `doc:v1:<sha256>` ref `corpus.parse_document` mints for `relative_path`, computed the
    identical way. T2 (investigate-boundary-v2): the counterfactual evidence records
    `_evaluate_counterfactual` builds carry this opaque ref, never the corpus-relative path."""
    return f"doc:v1:{hashlib.sha256(relative_path.encode('utf-8')).hexdigest()}"


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

        adr_ref = _doc_ref("public/adr-01.md")
        alt_ref = alternative_ref_for(adr_ref, "FastMCP")
        premise_ref = premise_ref_for("fastmcp-no-forbid")

        assert result.counterfactual_assessment is not None
        assert result.counterfactual_assessment.matched_alternative_ref == alt_ref
        assert result.counterfactual_assessment.decision_ref == adr_ref
        assert result.counterfactual_assessment.verdict == "repeat_of_rejected_architecture"
        assert len(result.counterfactual_assessment.supporting_evidence) > 0
        assert any(
            f"Task matches rejected architecture {alt_ref} under active premises" in c
            for c in result.conflicts
        )
        assert len(result.alternatives) == 1
        assert result.alternatives[0].ref == alt_ref
        assert result.alternatives[0].disposition == "rejected"
        assert result.alternatives[0].decision_ref == adr_ref
        assert result.alternatives[0].premise_refs == [premise_ref]
        assert len(result.premises) == 1
        assert result.premises[0].ref == premise_ref
        assert result.premises[0].status == "active"
        assert result.premises[0].decision_ref == adr_ref
        assert result.premises[0].invalidated_by is None
        assert result.premises[0].invalidated_in is None

        # T3 (investigate-boundary-v2): the alternative's name/reason and the premise's own id/
        # statement never reach the wire -- neither the closed models above nor the free-text
        # surfaces (rationale, conflicts) carry them.
        wire = result.model_dump_json()
        assert "FastMCP" not in wire
        assert "fastmcp-no-forbid" not in wire

        # T2 (investigate-boundary-v2): the counterfactual evidence record for the matched
        # alternative's own document carries the opaque document_ref and a closed
        # authority_rationale code -- never the file path or a sentence built from the
        # alternative's name/disposition.
        cf_evidence = next(e for e in result.evidence if e.locator == adr_ref)
        assert cf_evidence.authority_rationale == "counterfactual_alternative_evidence"
        assert "adr-01.md" not in cf_evidence.citation_locator
        assert "FastMCP" not in cf_evidence.authority_rationale


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

        adr_ref = _doc_ref("public/adr-01.md")
        invalidator_ref = _doc_ref("public/adr-02.md")
        alt_ref = alternative_ref_for(adr_ref, "FastMCP")
        premise_ref = premise_ref_for("fastmcp-no-forbid")

        assert result.counterfactual_assessment is not None
        assert result.counterfactual_assessment.matched_alternative_ref == alt_ref
        assert result.counterfactual_assessment.verdict == "premise_changed_requires_reevaluation"
        assert len(result.counterfactual_assessment.supporting_evidence) >= 1
        assert any(
            f"Historical rejection of {alt_ref} questioned: premise {premise_ref} was invalidated "
            "by f6e5d4c3b2a1" in c
            for c in result.conflicts
        )
        assert len(result.premises) == 1
        assert result.premises[0].ref == premise_ref
        assert result.premises[0].status == "invalidated"
        # `commit: f6e5d4c3b2a1` (adr-02.md's frontmatter) is a valid, if short, hex sha, so it
        # passes `agent_surface.commit_sha` validation unchanged.
        assert result.premises[0].invalidated_by == "f6e5d4c3b2a1"
        assert result.premises[0].invalidated_in == invalidator_ref

        wire = result.model_dump_json()
        assert "FastMCP" not in wire
        assert "fastmcp-no-forbid" not in wire

        # T2: the invalidating document's own evidence record carries its opaque document_ref
        # and the closed "counterfactual_invalidated_premise_evidence" code.
        invalidation_evidence = next(e for e in result.evidence if e.locator == invalidator_ref)
        assert invalidation_evidence.authority_rationale == "counterfactual_invalidated_premise_evidence"
        assert "adr-02.md" not in invalidation_evidence.citation_locator
        assert "fastmcp-no-forbid" not in invalidation_evidence.authority_rationale


def test_a_one_letter_alternative_name_does_not_match_an_unrelated_task(tmp_path: Path):
    """T3 (investigate-boundary-v2), the name-match floor: a name with no token of at least 3
    characters (here, a single letter) is not specific enough to identify a task. Before the
    floor, step 1's raw substring check ('a' in task_text) matched almost any English sentence,
    so this alternative fired a counterfactual assessment against a task that has nothing to do
    with it."""
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)
    (vault / "adr-a.md").write_text(
        """---
commit: a1b2c3d4e5f6
alternatives:
  - name: "A"
    disposition: rejected
    reason: Too vague a name to ever mean anything.
---
# ADR: reject A
Decision content.
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
        result = service.investigate(
            InvestigationRequest(task="migrate server to a completely different framework")
        )

        assert result.counterfactual_assessment is None
        assert result.alternatives == []


def test_a_one_letter_alternative_name_cannot_shadow_a_legitimate_alternative_that_sorts_after_it(
    tmp_path: Path,
):
    """The same floor, proven against the exact shadowing shape it closes: `alternatives`' PRIMARY
    KEY is `(name, document_ref)` and `get_alternatives()` has no ORDER BY, so rows come back in
    PK order -- "A" sorts before "Kubernetes" and is tried FIRST. Before the floor, "A" matched
    step 1's raw substring against virtually any task (it is a substring of "managed", "platform",
    ...) and `break`ed the loop, so the real, specific "Kubernetes" alternative -- which sorts
    after it -- was never even tried."""
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)
    (vault / "adr-a.md").write_text(
        """---
commit: a1b2c3d4e5f6
alternatives:
  - name: "A"
    disposition: rejected
    reason: Too vague a name to ever mean anything.
---
# ADR: reject A
Decision content.
""",
        encoding="utf-8",
    )
    (vault / "adr-kubernetes.md").write_text(
        """---
commit: b1c2d3e4f5a6
alternatives:
  - name: Kubernetes
    disposition: rejected
    reason: Operational overhead too high for our team size.
---
# ADR: reject Kubernetes
Decision content.
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
        result = service.investigate(
            InvestigationRequest(task="migrate server to Kubernetes framework")
        )

        assert result.counterfactual_assessment is not None
        kubernetes_ref = _doc_ref("public/adr-kubernetes.md")
        assert result.counterfactual_assessment.matched_alternative_ref == (
            alternative_ref_for(kubernetes_ref, "Kubernetes")
        )
        assert result.counterfactual_assessment.decision_ref == kubernetes_ref


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
