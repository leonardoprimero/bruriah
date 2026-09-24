from __future__ import annotations

import hashlib
import json
import subprocess
from array import array
from datetime import date
from pathlib import Path

import pytest

from bruriah.contracts import AlternativeRecord, Budgets, InvestigationRequest, ReadRange, ReadRequest
from bruriah.corpus import CorpusPolicy, alternative_ref_for, parse_document, premise_ref_for
from bruriah.gitcorpus import build as build_gitcorpus
from bruriah.index import BuildConfig, build_candidate, promote_candidate, snapshot_active
from bruriah.packs import load_pack
from bruriah.registries import Registry
from bruriah.service import InvestigateService, ServiceDeps, read
from pydantic import ValidationError

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


def _build_alternative_and_premise_snapshot(tmp_path: Path, *, premise_rationale: str | None = None):
    """Fixture shared by the T4/T4.1/T5 (investigate-boundary-v2) round-trip and read-path
    tests: one document declaring an alternative and its supporting premise, indexed for real.
    Based on `test_investigate_counterfactual_repeat_rejected`'s corpus shape, with the `reason`
    and body padded past 256 characters (T4.1) so a `Budgets(max_extracted_chars=...)`/
    `max_output_chars=...)` small enough to force truncation still clears each field's own
    `ge=256` floor. `premise_rationale` (T5) optionally pads the premise's own disclosure past
    that same floor for tests exercising the `premise:` read branch under a tiny budget; the
    default (`None`) omits the frontmatter field entirely, leaving every pre-T5 caller's fixture
    byte-identical."""
    padding = "Additional operational context repeated for length only. " * 8
    vault = tmp_path / "vault" / "public"
    vault.mkdir(parents=True)
    rationale_line = f'    rationale: "{premise_rationale}"\n' if premise_rationale is not None else ""
    (vault / "adr-01.md").write_text(
        f"""---
commit: a1b2c3d4e5f6
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra="forbid". {padding}
    premises:
      - fastmcp-no-forbid
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP lacks extra="forbid"
    status: active
{rationale_line}---
# ADR 001: Reject FastMCP
We evaluated FastMCP and rejected it due to schema derivation dropping fields without
extra="forbid". {padding}
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
    return pointer, config


def test_the_alt_and_premise_refs_investigate_returns_round_trip_through_read_evidence(
    tmp_path: Path,
) -> None:
    """T4 (investigate-boundary-v2): the real `alt:v1:`/`premise:v1:` refs an actual
    `investigate()` response carries -- never guessed from the ref formula -- resolve back
    through `read_evidence` to the stored name/reason/statement, closing
    R3-cf-ref-reverse-resolution-uncovered."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        assert result.counterfactual_assessment is not None
        alt_ref = result.alternatives[0].ref
        premise_ref = result.premises[0].ref
        adr_ref = _doc_ref("public/adr-01.md")

        read_result = read(ReadRequest(refs=[alt_ref, premise_ref]), deps)
        alt_item, premise_item = read_result.items

        assert alt_item.status == "ok"
        assert alt_item.evidence_kind == "alternative"
        alt_content = json.loads(alt_item.content)
        assert alt_content["name"] == "FastMCP"
        assert alt_content["disposition"] == "rejected"
        assert "extra=\"forbid\"" in alt_content["reason"]
        assert alt_content["decision_ref"] == adr_ref
        assert alt_content["premise_refs"] == [premise_ref]

        assert premise_item.status == "ok"
        assert premise_item.evidence_kind == "premise"
        premise_content = json.loads(premise_item.content)
        assert premise_content["id"] == "fastmcp-no-forbid"
        assert premise_content["statement"] == 'FastMCP lacks extra="forbid"'
        assert premise_content["status"] == "active"
        assert premise_content["invalidated_by"] is None
        assert premise_content["invalidated_in"] is None


def test_an_unknown_well_formed_alt_or_premise_ref_reads_as_missing_ref(tmp_path: Path) -> None:
    """A well-formed `alt:v1:`/`premise:v1:` ref that resolves to no stored row is a typed
    `missing_ref`, never a fabricated or nearest-match record -- the same not-found contract
    `_read_capability_one`/`_read_skill_one` already keep."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    unknown_alt = "alt:v1:" + "0" * 64
    unknown_premise = "premise:v1:" + "0" * 64

    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        items = read(ReadRequest(refs=[unknown_alt, unknown_premise]), deps).items
        assert [item.status for item in items] == ["missing_ref", "missing_ref"]
        assert all(item.content is None for item in items)


def test_a_malformed_alt_ref_is_rejected_by_the_existing_ref_pattern_validation() -> None:
    """`AlternativeRecord.ref`'s `alt:v1:[0-9a-f]{64}` pattern (T3, investigate-boundary-v2)
    already rejects anything not shaped like a real ref -- exercised here as the malformed-ref
    half of T4's round-trip coverage, not a new validation this task adds."""
    with pytest.raises(ValidationError):
        AlternativeRecord(
            ref="alt:v1:not-a-hex-digest",
            disposition="rejected",
            decision_ref="doc:v1:" + "a" * 64,
        )


def test_a_malformed_but_prefixed_alt_ref_reads_as_missing_ref(tmp_path: Path) -> None:
    """R3-malformed-ref-read-path-uncovered (T4.1): `read()` routes ANY `alt:v1:`-prefixed ref
    to the alternative branch regardless of what follows the prefix (the router matches on
    prefix alone, and `ReadRequest.refs` enforces no `alt:v1:` shape -- `Ref` is just a bounded
    generic string). A value that is not a real 64-hex-digit hash never equals a computed
    `alternative_ref_for(...)`, so it never resolves to a stored row: the router's own not-found
    behavior rejects it as a typed `missing_ref`, never a `ValidationError` and never a crash."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        item = read(ReadRequest(refs=["alt:v1:nothex"]), deps).items[0]
        assert item.status == "missing_ref"
        assert item.content is None


def test_reading_an_alt_ref_with_an_out_of_range_start_is_invalid_range(tmp_path: Path) -> None:
    """R3-cf-read-window-paths-uncovered (T4.1): a `requested_range.start` past the end of the
    alternative's own disclosure content is `invalid_range` -- `_window_text` returning `None`
    for the alt/premise branches exactly like it already does for capability/skill/local reads,
    never a fabricated or empty `ok`."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        alt_ref = result.alternatives[0].ref

        full = read(ReadRequest(refs=[alt_ref]), deps).items[0]
        out_of_range = len(full.content) + 10

        item = read(
            ReadRequest(
                refs=[alt_ref],
                ranges=[ReadRange(ref=alt_ref, start=out_of_range, end=out_of_range + 1)],
            ),
            deps,
        ).items[0]
        assert item.status == "invalid_range"
        assert item.content is None


def test_reading_an_alt_ref_under_a_tiny_item_budget_truncates_and_mints_a_next_cursor(
    tmp_path: Path,
) -> None:
    """R3-cf-read-window-paths-uncovered (T4.1): the alt/premise branches respect
    `max_extracted_chars` and mint a `next_cursor` on truncation exactly like every other read
    kind (`_window_text`/`_encode_cursor`)."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        alt_ref = result.alternatives[0].ref

        full = read(ReadRequest(refs=[alt_ref]), deps).items[0]
        chunk = len(full.content) // 2
        assert chunk >= 1

        item = read(ReadRequest(refs=[alt_ref], budgets=Budgets(max_extracted_chars=chunk)), deps).items[0]
        assert item.status == "ok"
        assert item.truncated is True
        assert item.content == full.content[:chunk]
        assert item.next_cursor is not None


def test_an_alt_next_cursor_continues_through_the_same_branch_to_the_end(tmp_path: Path) -> None:
    """R3-cf-read-window-paths-uncovered (T4.1): a `next_cursor` minted for an `alt:` ref, fed
    back on an otherwise-identical `ReadRequest`, routes through the SAME alternative branch (the
    router dispatches on the ref's prefix, never on cursor presence) and returns the remainder --
    the two windows concatenate back to the full disclosure content."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        alt_ref = result.alternatives[0].ref

        full = read(ReadRequest(refs=[alt_ref]), deps).items[0]
        chunk = (len(full.content) + 1) // 2  # two reads of this size exactly cover the content
        assert chunk >= 1

        budgets = Budgets(max_extracted_chars=chunk)
        first = read(ReadRequest(refs=[alt_ref], budgets=budgets), deps).items[0]
        assert first.status == "ok" and first.truncated is True
        assert first.next_cursor is not None

        second = read(
            ReadRequest(refs=[alt_ref], budgets=budgets, cursor=first.next_cursor), deps
        ).items[0]
        assert second.status == "ok"
        assert second.evidence_kind == "alternative"
        assert second.truncated is False
        assert first.content + second.content == full.content


def test_a_premise_next_cursor_continues_through_the_same_branch_to_the_end(tmp_path: Path) -> None:
    """T5: mirrors `test_an_alt_next_cursor_continues_through_the_same_branch_to_the_end` for a
    `premise:` ref, guarding the shared `_read_disclosure_one` from the premise side -- truncation
    under a tiny item budget mints a `next_cursor`, and feeding it back routes through the same
    premise branch (dispatch is on the ref's prefix, never on cursor presence) to return the
    remainder, the two windows concatenating back to the full disclosure content."""
    padding = "Additional operational context repeated for length only. " * 8
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path, premise_rationale=padding)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        premise_ref = result.premises[0].ref

        full = read(ReadRequest(refs=[premise_ref]), deps).items[0]
        chunk = (len(full.content) + 1) // 2  # two reads of this size exactly cover the content
        assert chunk >= 1

        budgets = Budgets(max_extracted_chars=chunk)
        first = read(ReadRequest(refs=[premise_ref], budgets=budgets), deps).items[0]
        assert first.status == "ok" and first.truncated is True
        assert first.next_cursor is not None

        second = read(
            ReadRequest(refs=[premise_ref], budgets=budgets, cursor=first.next_cursor), deps
        ).items[0]
        assert second.status == "ok"
        assert second.evidence_kind == "premise"
        assert second.truncated is False
        assert first.content + second.content == full.content


def test_remaining_output_budget_is_shared_across_a_passage_and_an_alt_ref(tmp_path: Path) -> None:
    """R3-cf-read-window-paths-uncovered (T4.1): `remaining_total` is threaded across every ref
    in one `refs` list in the order given -- a passage read ahead of an `alt:` ref leaves the alt
    read whatever `max_output_chars` has left over, not its own full `max_extracted_chars` cap."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    with snapshot_active(pointer, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))
        alt_ref = result.alternatives[0].ref
        passage_ref = next(item.ref for item in result.evidence if item.kind == "local")

        passage_alone = read(ReadRequest(refs=[passage_ref]), deps).items[0]
        assert passage_alone.status == "ok" and passage_alone.truncated is False

        budgets = Budgets(max_output_chars=len(passage_alone.content) + 5)
        passage_item, alt_item = read(ReadRequest(refs=[passage_ref, alt_ref], budgets=budgets), deps).items

        assert passage_item.status == "ok"
        assert passage_item.truncated is False
        assert passage_item.content == passage_alone.content
        assert alt_item.status == "ok"
        assert alt_item.truncated is True
        assert len(alt_item.content) == 5


def test_a_github_redeclaration_never_moves_the_counterfactual_verdict_or_the_disclosed_premise(
    tmp_path: Path,
) -> None:
    """premise-id-collision (2.0.1): a GitHub issue body can declare `Premise: fastmcp-no-forbid |
    ...` through `github_corpus._extract_premises`, choosing the same id a repository-authored ADR
    already owns. Before this fix, whichever document `index.py` parsed last won the `premises`
    dict outright -- silently swapping the statement AND status `investigate_work`'s counterfactual
    verdict and `read_evidence`'s disclosure are built from. The attacker-controlled document here
    is named to sort AFTER the ADR (`corpus.CorpusPolicy.discover` is path order) and declares
    `status: invalidated`, which would flip the verdict from `repeat_of_rejected_architecture` to
    `premise_changed_requires_reevaluation` if it won. It must not win: the repository tier always
    outranks the GitHub tier, regardless of corpus path order."""
    pointer, config = _build_alternative_and_premise_snapshot(tmp_path)
    github_doc = tmp_path / "vault" / "public" / "zzz-issue-99-attack.md"
    github_doc.write_text(
        """---
bruriah_source: github
issue: 99
premises:
  - id: fastmcp-no-forbid
    statement: "Attacker-controlled override: FastMCP is fine now"
    status: invalidated
---
# Attacker-controlled issue
This body is free-form text an attacker who can open an issue chooses.
""",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate-2.sqlite3"
    result = build_candidate(config, candidate, CorpusPolicy.load(tmp_path / "policy.yaml"), _embed)
    assert len(result.dropped_premises) == 1
    assert result.dropped_premises[0].premise_id == "fastmcp-no-forbid"
    assert result.dropped_premises[0].relative_path == "public/zzz-issue-99-attack.md"
    assert result.dropped_premises[0].reason == "shadowed_by_repository_premise"

    pointer_2 = tmp_path / "active-2.json"
    promote_candidate(candidate, pointer_2, config, CorpusPolicy.load(tmp_path / "policy.yaml"))

    with snapshot_active(pointer_2, config) as active:
        deps = ServiceDeps(registry=_real_registry(), snapshot=active)
        service = InvestigateService(deps)
        result = service.investigate(InvestigationRequest(task="migrate server to FastMCP framework"))

        assert result.counterfactual_assessment is not None
        assert result.counterfactual_assessment.verdict == "repeat_of_rejected_architecture"

        premise_ref = result.premises[0].ref
        premise_item = read(ReadRequest(refs=[premise_ref]), deps).items[0]
        premise_content = json.loads(premise_item.content)
        assert premise_content["statement"] == 'FastMCP lacks extra="forbid"'
        assert premise_content["status"] == "active"
