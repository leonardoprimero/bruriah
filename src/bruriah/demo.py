"""Interactive terminal demonstration of Bruriah's Counterfactual Memory engine."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from array import array
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import InvestigationRequest
from .corpus import CorpusPolicy
from .index import BuildConfig, build_candidate, promote_candidate, snapshot_active
from .packs import DomainPack
from .registries import Registry
from .repository import SnapshotRepository
from .service import InvestigateService, ServiceDeps

_DEMO_ALT_REF_PATTERN = re.compile(r"alt:v1:[0-9a-f]{64}")
_DEMO_PREMISE_REF_PATTERN = re.compile(r"premise:v1:[0-9a-f]{64}")


def _resolve_counterfactual_refs(text: str, repo: SnapshotRepository) -> str:
    """Resolve `alt:v1:`/`premise:v1:` refs embedded in `CounterfactualAssessment` text back to
    a name/id for this human-facing demo (T3, investigate-boundary-v2) -- the same local-resolver
    pattern `cli._resolve_doc_refs_for_humans` uses for `bruriah ask`'s human view. Applies to the
    whole string, so it resolves a bare ref (`matched_alternative_ref`) just as well as a ref
    embedded inside the fixed-wording `rationale` template."""

    def _resolve_alt(match: "re.Match[str]") -> str:
        row = repo.resolve_alternative_ref(match.group(0))
        return row.name if row is not None else match.group(0)

    def _resolve_premise(match: "re.Match[str]") -> str:
        row = repo.resolve_premise_ref(match.group(0))
        return row.premise_id if row is not None else match.group(0)

    text = _DEMO_ALT_REF_PATTERN.sub(_resolve_alt, text)
    text = _DEMO_PREMISE_REF_PATTERN.sub(_resolve_premise, text)
    return text

_SRC = Path(__file__).resolve().parent
_DATA = _SRC / "data"

FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)


def _embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


def _format_bold(text: str, enabled: bool) -> str:
    return f"\033[1m{text}\033[0m" if enabled else text


def _format_green(text: str, enabled: bool) -> str:
    return f"\033[32m{text}\033[0m" if enabled else text


def _format_red(text: str, enabled: bool) -> str:
    return f"\033[31m{text}\033[0m" if enabled else text


def _format_yellow(text: str, enabled: bool) -> str:
    return f"\033[33m{text}\033[0m" if enabled else text


def _format_cyan(text: str, enabled: bool) -> str:
    return f"\033[36m{text}\033[0m" if enabled else text


def run_demo(
    *,
    interactive: bool = True,
    use_color: bool = True,
    stdout: Any = None,
) -> int:
    """Execute the step-by-step counterfactual memory demonstration."""
    out = stdout if stdout is not None else sys.stdout

    def _pause(prompt: str = "Press Enter to continue...") -> None:
        if interactive:
            try:
                input(f"\n{_format_cyan(prompt, use_color)} ")
            except (EOFError, KeyboardInterrupt):
                out.write("\n")
                return

    out.write("\n" + _format_bold("🏛️  Bruriah Counterfactual Memory & Invariant Engine Demo", use_color) + "\n")
    out.write("=" * 60 + "\n")
    out.write("This demo illustrates how Bruriah prevents coding agents from\n")
    out.write("repeating discarded architectural mistakes without adding MCP bloat.\n\n")

    # Load base pack
    raw_pack = json.loads((_DATA / "research-policy.json").read_text(encoding="utf-8"))
    raw_pack["domains"] = ["software-research", "programming", "general"]
    raw_pack["reviewed_at"] = date.fromisoformat(raw_pack["reviewed_at"])
    raw_pack["expires_at"] = date.fromisoformat(raw_pack["expires_at"])
    pack_obj = DomainPack.model_validate(raw_pack)
    reg = Registry.from_packs([pack_obj])

    with tempfile.TemporaryDirectory() as tmpdir:
        t = Path(tmpdir)
        vault = t / "vault"
        vault.mkdir()
        policy_path = t / "policy.yaml"
        policy_path.write_text("version: 1\ninclude: ['*.md', '**/*.md']\nexclude: []\n", encoding="utf-8")
        policy = CorpusPolicy.load(policy_path)

        cfg = BuildConfig(
            root=vault,
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

        # -------------------------------------------------------------------
        # Step 1: Initial Architectural Decision
        # -------------------------------------------------------------------
        out.write(_format_bold("Step 1: Storing Initial Architectural Decision (ADR-001)", use_color) + "\n")
        out.write("-" * 60 + "\n")
        out.write("Suppose your team evaluated FastMCP 6 months ago and rejected it because\n")
        out.write("it silently dropped unknown fields without extra='forbid'.\n\n")

        doc1_content = """---
status: active
premises:
  - id: fastmcp-no-forbid
    statement: FastMCP derives schemas without extra='forbid', silently dropping unknown fields
    status: active
alternatives:
  - name: FastMCP
    disposition: rejected
    reason: Drops unknown fields silently without extra='forbid'
    premises:
      - fastmcp-no-forbid
---
# ADR 001: Standardize on lowlevel.Server instead of FastMCP
We evaluated FastMCP and rejected it. Supporting premise: fastmcp-no-forbid.
"""
        (vault / "adr-001.md").write_text(doc1_content, encoding="utf-8")
        out.write(_format_cyan("Recorded in corpus:", use_color) + " adr-001.md\n")
        out.write("  • Premise: fastmcp-no-forbid (active)\n")
        out.write("  • Alternative: FastMCP (rejected)\n\n")

        c1 = t / "c1.sqlite3"
        p1 = t / "p1.json"
        build_candidate(cfg, c1, policy, _embed)
        promote_candidate(c1, p1, cfg, policy)

        _pause("Step 1 complete. Press Enter to simulate an agent task...")

        # -------------------------------------------------------------------
        # Step 2: Agent Proposes the Discarded Architecture
        # -------------------------------------------------------------------
        out.write("\n" + _format_bold("Step 2: Agent Proposes Discarded Architecture", use_color) + "\n")
        out.write("-" * 60 + "\n")
        task_prompt = "Migrate server to FastMCP framework"
        out.write(f"Agent receives task: {_format_yellow(repr(task_prompt), use_color)}\n")
        out.write("Calling investigate_work(task=...)\n\n")

        with snapshot_active(p1, cfg) as active1:
            s1 = InvestigateService(ServiceDeps(registry=reg, snapshot=active1))
            res1 = s1.investigate(InvestigationRequest(task=task_prompt))

            cf1 = res1.counterfactual_assessment
            if cf1:
                repo1 = SnapshotRepository(active1.database)
                out.write("  " + _format_red(f"🛑 VERDICT: {cf1.verdict}", use_color) + "\n")
                out.write(f"  Matched Alternative: {_resolve_counterfactual_refs(cf1.matched_alternative_ref, repo1)}\n")
                out.write(f"  Rationale: {_resolve_counterfactual_refs(cf1.rationale, repo1)}\n")
                out.write(f"  Conflicts: {[_resolve_counterfactual_refs(c, repo1) for c in res1.conflicts]}\n\n")
                out.write(_format_green("RESULT: Agent is warned and flagged with an architectural conflict!", use_color) + "\n")
            else:
                out.write("  No counterfactual assessment generated.\n")

        _pause("Step 2 complete. Press Enter to simulate premise invalidation...")

        # -------------------------------------------------------------------
        # Step 3: Premise Invalidation & Re-evaluation
        # -------------------------------------------------------------------
        out.write("\n" + _format_bold("Step 3: Premise Invalidation & Re-evaluation", use_color) + "\n")
        out.write("-" * 60 + "\n")
        out.write("Now, FastMCP releases v2.0 which officially adds extra='forbid'.\n")
        out.write("A new decision or update invalidates the premise 'fastmcp-no-forbid'.\n\n")

        doc2_content = """---
status: active
invalidated_premises:
  - fastmcp-no-forbid
---
# ADR 002: FastMCP Schema Invalidation
FastMCP v2.0 added strict extra='forbid' validation. Premise fastmcp-no-forbid is invalidated.
"""
        (vault / "adr-002.md").write_text(doc2_content, encoding="utf-8")
        out.write(_format_cyan("Recorded in corpus:", use_color) + " adr-002.md\n")
        out.write("  • Invalidated premise: fastmcp-no-forbid\n\n")

        c2 = t / "c2.sqlite3"
        p2 = t / "p2.json"
        build_candidate(cfg, c2, policy, _embed)
        promote_candidate(c2, p2, cfg, policy)

        out.write(f"Agent re-evaluates task: {_format_yellow(repr(task_prompt), use_color)}\n")
        out.write("Calling investigate_work(task=...)\n\n")

        with snapshot_active(p2, cfg) as active2:
            s2 = InvestigateService(ServiceDeps(registry=reg, snapshot=active2))
            res2 = s2.investigate(InvestigationRequest(task=task_prompt))

            cf2 = res2.counterfactual_assessment
            if cf2:
                repo2 = SnapshotRepository(active2.database)
                out.write("  " + _format_yellow(f"🔄 VERDICT: {cf2.verdict}", use_color) + "\n")
                out.write(f"  Matched Alternative: {_resolve_counterfactual_refs(cf2.matched_alternative_ref, repo2)}\n")
                out.write(f"  Rationale: {_resolve_counterfactual_refs(cf2.rationale, repo2)}\n")
                out.write(f"  Conflicts: {[_resolve_counterfactual_refs(c, repo2) for c in res2.conflicts]}\n\n")
                out.write(_format_green("RESULT: Bruriah tracks premise invalidation and signals that FastMCP now requires reevaluation!", use_color) + "\n")
            else:
                out.write("  No counterfactual assessment generated.\n")

    out.write("\n" + "=" * 60 + "\n")
    out.write(_format_bold("Demo completed successfully!", use_color) + "\n")
    out.write("To use Bruriah in your project:\n")
    out.write("  1. Initialize:   bruriah init --repo .\n")
    out.write("  2. Explore DAG:  bruriah ui\n")
    out.write("  3. Ask queries:  bruriah ask \"why did we avoid X\"\n\n")
    return 0
