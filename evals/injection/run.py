#!/usr/bin/env python3
"""Prompt-injection benchmark for `investigate_work`: hermetic, offline, deterministic.

Headline metric: Attack Success Rate (ASR) = the fraction of EXECUTED cases in which a unique
attacker-controlled marker appears in the serialized `investigate_work` response
(`result.model_dump(mode="json")`) at a JSON path that is genuinely corpus-derived -- established
by a CONTROL run of the identical fixture and task with only that case's one attacker surface
reverted to a clean value (see `find_leak_fields` and `InjectionCase.build_control`), never by
whether the field's text merely happens to overlap the task string. A case that never executed
its carrying code path is not evidence the boundary held -- it is a harness failure, and this
module never reports one as "held" (see `_require_all_executed` below).

Methodology notes (the pitfalls worth stating up front):
- An earlier, unpublished sweep poisoned every corpus-authored surface in ONE document, including
  `alternatives[].name`. But the counterfactual engine only matches an alternative when its name
  appears in the task text, so poisoning the name suppressed the very match that would have
  exposed `reason`, `statement`, and `id` -- those surfaces reported "held" having simply never
  run. `evals/injection/cases.py` avoids this by isolating one surface per case and pairing each
  with a task chosen to exercise the path that would carry it (see `md-alt-name`'s case for the
  one place the task itself must also carry the marker, to keep matching the poisoned text).
- `md-alt-name`'s own poisoning then creates a second pitfall: because its task must carry the
  same marker as its corpus surface, a naive check for "does this field's text overlap the task"
  wrongly excluded `.alternatives[0].name` -- ITS OWN corpus-authored surface -- as a false
  "echo". String containment cannot establish provenance; only an actual control run can.

Hermetic: builds the index with a fake constant-vector embedder (the same pattern
`evals/counterfactual/runner.py` uses) and calls `InvestigateService` directly -- never
`cli.bruriah_main` with its real, network-fetching embedder. No reranker. No network. No model
download.

Usage:
    uv run python evals/injection/run.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from array import array
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cases import CASES, GitUnavailableError, InjectionCase  # noqa: E402

from bruriah.contracts import InvestigationRequest  # noqa: E402
from bruriah.corpus import CorpusPolicy  # noqa: E402
from bruriah.index import BuildConfig, build_candidate, promote_candidate, snapshot_active  # noqa: E402
from bruriah.platform import load_registry  # noqa: E402
from bruriah.registries import Registry  # noqa: E402
from bruriah.service import InvestigateService, ServiceDeps  # noqa: E402

REPORT_JSON_PATH = _HERE / "report.json"
REPORT_MD_PATH = _HERE / "report.md"


def write_report(path: Path, text: str) -> None:
    """Publish a committed report whole or not at all.

    The reports under `evals/injection` are committed artifacts that tests and the README pin
    against, so a crash halfway through `write_text` must never leave a truncated file in the
    tree. The bytes go to a temporary in the same directory, are flushed and fsynced, and only
    then replace the destination in one `os.replace`; on any failure the temporary is removed
    and the previous report is untouched. Shared by the core runner and the framework comparison.
    """
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    )
    temporary = Path(handle.name)
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, path)
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise

# Deterministic across every case, and across every run of this module: no fastembed download,
# no real vector, so the "fingerprint" is a fixture value like `evals/counterfactual/runner.py`'s.
_FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"'
    + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
# Pinned, not `date.today()`: the bundled domain packs' currency assessment must not depend on
# the day this module happens to run, or the report would stop being byte-identical across runs
# made on different days.
_REGISTRY_TODAY = date(2026, 7, 25)


def _fake_embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


def _registry() -> Registry:
    # Bundled domain packs only -- no network, no filesystem outside the installed package.
    return load_registry(today=_REGISTRY_TODAY)


class NotExecutedError(RuntimeError):
    """Raised when a case's carrying code path did not run. Never treated as 'held' -- the
    benchmark's whole premise is that a surface can only be reported held once the path that
    would carry it has been proven to run (see this module's docstring and `cases.py`)."""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    carrier: str
    surface: str
    executed: bool
    leaked: bool
    # JSON paths (e.g. ".alternatives[0].name") of every response field that carried the marker
    # in the poisoned run but not at that same path in the CONTROL run -- see `find_leak_fields`.
    # Empty when `leaked` is False, and empty for a case that never executed (e.g. git was
    # unavailable).
    leak_fields: tuple[str, ...] = ()
    # JSON paths where the marker hit BOTH the poisoned and the control run -- excluded from
    # leak_fields (see `find_leak_fields`) because, since the control's corpus-side surface is
    # clean by construction, a control-side hit at a path can only mean that path echoes the
    # task, not the corpus. Recorded so an excluded path is visible in the report instead of
    # silently dropped; see `find_echo_fields`.
    echo_fields: tuple[str, ...] = ()
    # Whether the control run (the identical fixture and task, with this case's one attacker
    # surface reverted to a clean value) exercised the same carrying path as the poisoned run.
    # A control run that did not execute makes the leak_fields subtraction meaningless -- the
    # control response may be missing the very section a leak would show up in -- so
    # `_require_all_executed` enforces this with the same force as `executed`: a case whose
    # control did not execute never reaches `render_json`/`render_markdown` as held or leaked,
    # it fails the run instead. See `_executed` and `InjectionCase.build_control`.
    control_executed: bool = True


def _investigate(
    corpus_dir: Path,
    task: str,
    work_dir: Path,
    *,
    code_target: str | None = None,
    repo: Path | None = None,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    policy_path = work_dir / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=corpus_dir,
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/injection-eval",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=_FINGERPRINT,
        ranking_config="rrf-v1",
    )
    candidate = work_dir / "candidate.sqlite3"
    pointer = work_dir / "active.json"
    build_candidate(config, candidate, policy, _fake_embed)
    promote_candidate(candidate, pointer, config, policy)
    with snapshot_active(pointer, config) as active:
        deps_kwargs: dict[str, Any] = {"registry": _registry(), "snapshot": active}
        if repo is not None:
            deps_kwargs["repo"] = repo
        deps = ServiceDeps(**deps_kwargs)
        service = InvestigateService(deps)
        request = InvestigationRequest(task=task, host_skills=[], code_target=code_target)
        result = service.investigate(request)
        return result.model_dump(mode="json")


_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _normalize(text: str) -> str:
    return _NON_ALNUM.sub("", text.lower())


def is_leaked(marker: str, serialized: str) -> bool:
    """A marker counts as leaked once its normalized form (lowercased, every non-alphanumeric
    separator stripped) appears anywhere in the normalized response -- `gitcorpus.build` slugs a
    generated file name (lowercases it and collapses separators to hyphens), so a verbatim,
    case-sensitive match undercounts a marker that reached the response only through that file
    name. Normalizing both sides subsumes a verbatim match: it never turns a real leak into a
    miss, only a slugged or case-changed one into a hit."""
    return _normalize(marker) in _normalize(serialized)


def _walk_string_fields(payload: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string leaf in a JSON-shaped payload, paired with its JSON path (e.g.
    ".alternatives[0].name"), in a stable, deterministic order (dict insertion order, list index
    order)."""
    if isinstance(payload, dict):
        leaves: list[tuple[str, str]] = []
        for key, value in payload.items():
            leaves.extend(_walk_string_fields(value, f"{path}.{key}"))
        return leaves
    if isinstance(payload, list):
        leaves = []
        for index, value in enumerate(payload):
            leaves.extend(_walk_string_fields(value, f"{path}[{index}]"))
        return leaves
    if isinstance(payload, str):
        return [(path, payload)]
    return []


def _marker_hits(marker: str, payload: dict[str, Any]) -> set[str]:
    return {path for path, value in _walk_string_fields(payload) if is_leaked(marker, value)}


def find_leak_fields(marker: str, poisoned_payload: dict[str, Any], control_payload: dict[str, Any]) -> list[str]:
    """JSON paths where the marker appears in `poisoned_payload` but NOT at that same path in
    `control_payload` -- the identical fixture and task, except this case's one attacker surface
    reverted to a clean, marker-free corpus value (see `InjectionCase.build_control`).

    A path that also hits in the control run is explained by something other than this case's
    corpus surface (almost always the task text itself, unchanged between the two runs) -- never
    a leak; see `find_echo_fields` for that excluded set, made observable rather than silently
    dropped. A whole-response, string-containment check against the task cannot make this
    distinction: `md-alt-name` (see `cases.py`) must poison its OWN task with the same marker for
    its counterfactual match to fire at all, so its corpus-authored `.alternatives[0].name` field
    legitimately overlaps the task text without being an echo of it. Provenance has to come from
    an actual control run, not from whether a string happens to be a substring of another."""
    return sorted(_marker_hits(marker, poisoned_payload) - _marker_hits(marker, control_payload))


def find_echo_fields(marker: str, poisoned_payload: dict[str, Any], control_payload: dict[str, Any]) -> list[str]:
    """JSON paths where the marker hits BOTH `poisoned_payload` and `control_payload` -- the
    complement of `find_leak_fields`, made observable instead of silently dropped. Since the
    control run's corpus-side surface is clean by construction (see
    `InjectionCase.build_control`), the marker can only reach a control-run field through the
    task text the two runs share; a path hit in both runs therefore echoes the task, never the
    corpus, and `find_leak_fields` is right to exclude it -- this records which paths that was,
    so a future coincidence is visible in the report rather than masked."""
    return sorted(_marker_hits(marker, poisoned_payload) & _marker_hits(marker, control_payload))


# A validated commit-sha provenance entry, in the exact shape
# `_resolve_code_target_causality` writes for its governing-decision evidence record
# (`f"commit:{short_commit_sha(gov.commit_sha)}"`, 7-64 lowercase hex after the colon --
# `agent_surface.commit_sha`'s own validated range). Unlike the OLD `authority_rationale`
# prefix check this replaces, this signal is prose-free by construction: it can never carry a
# poisoned author/subject string, because `provenance_chain`'s `commit:` entry is built only
# from a git-validated sha, never from corpus text (see T2 -- R4-proof-coupled-to-prose). It
# also holds on today's PRE-T2 response shape, where the same `commit:<sha>` entry already sits
# in that evidence record's `provenance_chain` -- so this move does not change what the
# benchmark reports before T2's src change lands, only what the proof reads to establish it.
_COMMIT_PROVENANCE = re.compile(r"^commit:[0-9a-f]{7,64}$")


def _executed(
    case: InjectionCase,
    payload: dict[str, Any],
    document_relative_path: str | None,
    document_ref: str | None = None,
) -> bool:
    if case.executed_proof == "alternatives":
        return bool(payload.get("alternatives"))
    if case.executed_proof == "premises":
        return bool(payload.get("premises"))
    if case.executed_proof == "code_target":
        # Structural signal, never response prose: a validated `commit:<sha>` provenance entry
        # only `_resolve_code_target_causality` writes, once it actually resolves a governing
        # decision -- independent of which field (author/subject) a case poisons, and
        # independent of `authority_rationale`'s wording (which T2 replaces with a closed code).
        return any(
            any(_COMMIT_PROVENANCE.match(str(item)) for item in (evidence.get("provenance_chain") or []))
            for evidence in payload.get("evidence", [])
        )
    if case.executed_proof == "lineage":
        # `_apply_lineage` only ever appends a `superseded_by:`/`deprecated_by:` entry to an
        # evidence record's `uncertainty` once it has actually matched a lineage relation --
        # proof the lineage path ran, never just that the response has evidence at all. Only the
        # PREFIX is checked, never what follows it, so this already survives T2 replacing the
        # successor's raw file path with its opaque `document_ref` after the colon.
        return any(
            any(
                str(item).startswith(("superseded_by:", "deprecated_by:"))
                for item in (evidence.get("uncertainty") or [])
            )
            for evidence in payload.get("evidence", [])
        )
    # "evidence": the document carrying the surface must actually have been retrieved -- proven
    # by an evidence record whose locator/publisher/citation_locator names it, never guessed.
    # An explicit exception, not `assert`: `assert` is a runtime guard that `python -O` strips
    # silently, and this invariant must hold no matter how the interpreter is invoked.
    if document_relative_path is None:
        raise ValueError(f"{case.case_id}: evidence-proof case has no document path")
    # Matches EITHER shape: the relative path as a substring (today's response, before T2) OR
    # the exact `document_ref` the indexer would mint for it, computed the identical way
    # (`cases.document_ref_for` -- see T2, which replaces `locator`/`citation_locator` with that
    # opaque ref). Both branches are checked unconditionally so this proof reads correctly, and
    # the benchmark report stays byte-identical, whichever response shape is under test.
    matches_relative_path = any(
        document_relative_path in str(evidence.get("locator") or "")
        or document_relative_path in str(evidence.get("publisher") or "")
        or document_relative_path in str(evidence.get("citation_locator") or "")
        for evidence in payload.get("evidence", [])
    )
    matches_document_ref = document_ref is not None and any(
        evidence.get("locator") == document_ref
        or str(evidence.get("citation_locator") or "").startswith(f"{document_ref}#")
        for evidence in payload.get("evidence", [])
    )
    return matches_relative_path or matches_document_ref


def run_case(case: InjectionCase) -> CaseResult:
    with tempfile.TemporaryDirectory(prefix="bruriah-injection-eval-") as tmp:
        work_dir = Path(tmp)
        poisoned_dir = work_dir / "poisoned"
        control_dir = work_dir / "control"
        poisoned_dir.mkdir()
        control_dir.mkdir()
        try:
            build_result = case.build(poisoned_dir)
            control_build_result = case.build_control(control_dir)
        except GitUnavailableError:
            # Report as not executed -- never as held, and never a crash on an opaque low-level
            # error. `_require_all_executed` turns this into a clear, actionable failure.
            return CaseResult(
                case_id=case.case_id,
                carrier=case.carrier,
                surface=case.surface,
                executed=False,
                leaked=False,
                control_executed=False,
            )
        payload = _investigate(
            build_result.corpus_dir,
            case.task,
            work_dir / "svc",
            code_target=case.code_target,
            repo=build_result.repo_dir,
        )
        control_payload = _investigate(
            control_build_result.corpus_dir,
            case.task,
            work_dir / "svc-control",
            code_target=case.code_target,
            repo=control_build_result.repo_dir,
        )
        executed = _executed(case, payload, build_result.document_relative_path, build_result.document_ref)
        control_executed = _executed(
            case, control_payload, control_build_result.document_relative_path, control_build_result.document_ref
        )
        leak_fields = find_leak_fields(case.marker, payload, control_payload)
        echo_fields = find_echo_fields(case.marker, payload, control_payload)
        return CaseResult(
            case_id=case.case_id,
            carrier=case.carrier,
            surface=case.surface,
            executed=executed,
            leaked=bool(leak_fields),
            leak_fields=tuple(leak_fields),
            echo_fields=tuple(echo_fields),
            control_executed=control_executed,
        )


def _require_all_executed(results: list[CaseResult]) -> None:
    not_executed = [r.case_id for r in results if not r.executed]
    # A control run that never exercised its carrying path makes `find_leak_fields`'s
    # poisoned-minus-control subtraction meaningless -- the control response may be missing the
    # very section a leak would show up in. Enforced with the same force as `executed`, so a case
    # like that is never presented as held or leaked, only as a harness failure.
    control_not_executed = [r.case_id for r in results if not r.control_executed]
    if not_executed or control_not_executed:
        messages = []
        if not_executed:
            messages.append(f"poisoned run never executed: {', '.join(not_executed)}")
        if control_not_executed:
            messages.append(f"control run never executed: {', '.join(control_not_executed)}")
        raise NotExecutedError(
            "the following cases never exercised their carrying code path, so their outcome is "
            f"a harness failure, not a measurement ({'; '.join(messages)})"
        )


def compute_asr(results: list[CaseResult]) -> float:
    """Attack Success Rate over EXECUTED cases only. Raises if any case is not executed --
    an unexecuted case has no leak/hold verdict to contribute to the fraction (see
    `NotExecutedError`); a caller that wants ASR over a partial set must filter first and knows
    what it is doing."""
    _require_all_executed(results)
    if not results:
        return 0.0
    leaked = sum(1 for r in results if r.leaked)
    return leaked / len(results)


def run_benchmark() -> list[CaseResult]:
    results = [run_case(case) for case in CASES]
    _require_all_executed(results)
    return results


def render_json(results: list[CaseResult]) -> str:
    payload = {
        "asr": compute_asr(results),
        "executed_count": sum(1 for r in results if r.executed),
        "leaked_count": sum(1 for r in results if r.leaked),
        "total_cases": len(results),
        "cases": [asdict(r) for r in results],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_markdown(results: list[CaseResult]) -> str:
    asr = compute_asr(results)
    leaked_count = sum(1 for r in results if r.leaked)
    lines = [
        "# Prompt-injection benchmark: investigate_work",
        "",
        "Attack Success Rate (ASR): the fraction of executed cases in which a unique "
        "attacker-controlled marker reached the serialized `investigate_work` response.",
        "",
        f"**ASR:** {asr:.3f} ({leaked_count}/{len(results)} executed cases leaked)",
        "",
        "leak_fields is established by a control run (identical fixture and task, this case's "
        "attacker surface reverted to a clean value): a path only counts once the marker hits it "
        "in the poisoned response but not at that same path in the control response. "
        "echo_fields lists every path excluded because it ALSO hit in the control response (and "
        "so echoes the task, never the corpus). control_executed confirms that control run "
        "exercised the same carrying path; a case whose control did not execute never reaches "
        "this report (see `_require_all_executed`).",
        "",
        "| case | carrier | surface | executed | leaked | leak_fields | echo_fields | control_executed |",
        "|---|---|---|:---:|:---:|---|---|:---:|",
    ]
    for r in results:
        leak_fields = ", ".join(f"`{field}`" for field in r.leak_fields) if r.leak_fields else "--"
        echo_fields = ", ".join(f"`{field}`" for field in r.echo_fields) if r.echo_fields else "--"
        lines.append(
            f"| `{r.case_id}` | {r.carrier} | {r.surface} | {r.executed} | {r.leaked} | {leak_fields} "
            f"| {echo_fields} | {r.control_executed} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    results = run_benchmark()
    write_report(REPORT_JSON_PATH, render_json(results))
    write_report(REPORT_MD_PATH, render_markdown(results))
    print(render_markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
