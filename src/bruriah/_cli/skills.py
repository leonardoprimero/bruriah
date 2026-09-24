from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .. import approvals, candidates, platform as platform_module, signing, skillset
from ..platform import PlatformPaths
from .common import CliError, resolve_cli_paths

_ANALYSIS_LIMITS = (
    "Structural checks only. An advisory means a person should look at this, never that it is "
    "dangerous, and the absence of advisories never means it is safe. Whether prose persuades an "
    "agent to act against you is not observable by inspection; the permission envelope and your own "
    "reading of the text are what protect you."
)


def _manifest_for(candidate: Path) -> Path | None:
    manifest = candidate.with_suffix(".manifest.json")
    return manifest if manifest.is_file() else None


def run_skill_ingest(paths: PlatformPaths, source: Path) -> dict[str, Any]:
    """Store a candidate pack privately, addressed by its own content digest."""
    try:
        record = candidates.ingest_candidate(source, paths.data_dir)
    except candidates.CandidateError as error:
        raise CliError(f"candidate_rejected:{error.code}") from error
    return {"path": str(record.path), "digest": record.digest, "pack_id": record.pack_id, "version": record.version}


def run_skill_analyze(candidate: Path) -> dict[str, Any]:
    """Report structural findings. The result carries its own limits so that copying the output
    copies the caveat with it -- a report that travels without its disclaimer becomes a clearance."""
    try:
        report = candidates.analyze_candidate(candidate)
    except candidates.CandidateError as error:
        raise CliError(f"candidate_rejected:{error.code}") from error
    return {
        "digest": report.digest,
        "pack_id": report.pack_id,
        "version": report.version,
        "skill_ids": list(report.skill_ids),
        "advisories": [
            {"id": item.identifier, "code": item.code, "skill_id": item.skill_id, "detail": item.detail}
            for item in report.advisories
        ],
        "analysis_limits": _ANALYSIS_LIMITS,
    }


def run_skill_approve(
    paths: PlatformPaths,
    candidate: Path,
    acknowledge: list[str],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Record human approval, bound to each skill's current body digest."""
    try:
        records = approvals.approve_candidate(
            candidate,
            paths.data_dir,
            acknowledge=acknowledge,
            today=today or date.today(),
        )
    except approvals.ApprovalError as error:
        raise CliError(f"approval_refused:{error.code}") from error
    return {"approved": [record.as_json() for record in records], "analysis_limits": _ANALYSIS_LIMITS}


def run_skill_sign(key: Path, signer: str, pack: Path, out: Path | None) -> dict[str, Any]:
    """Sign a pack through the same `signing` module the release script uses, so a manifest produced
    here and one produced there cannot drift apart."""
    try:
        manifest = signing.sign_pack(key, signer, pack, out)
    except signing.SigningError as error:
        raise CliError(f"signing_failed:{error.code}") from error
    return {
        "manifest": str(manifest),
        "signer": signer,
        "note": "A signature establishes who signed these bytes. It is not a claim that they are correct or safe.",
    }


def run_skill_activate(
    paths: PlatformPaths,
    candidates_: list[Path],
    *,
    allow_unsigned_local: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Compile the named approved candidates into one generation and promote it.

    Candidates are named EXPLICITLY rather than swept up from "everything approved". Approval says
    "I read this and accept it"; activation says "this goes into service now". A mode that activated
    everything approved would collapse the two and make approval an implicit activation, which is
    the exact door the review gate exists to close.

    Compile validates the whole set -- signatures, dates, envelope invariants, and an approval bound
    to each skill's current digest -- and writes nothing if any of it fails. Promotion then swaps the
    pointer atomically under a lock, so a reader mid-request keeps serving the previous generation."""
    if not candidates_:
        raise CliError("no_candidates_named")
    skills_dir = paths.data_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    sources = [
        skillset.SkillSource(path, _manifest_for(path), allow_unsigned_local=allow_unsigned_local)
        for path in candidates_
    ]
    try:
        destination = skillset.generation_path(skills_dir)
        skillset.compile_skillset(
            sources,
            destination,
            platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir),
            today=today,
        )
        result = skillset.promote_skillset(
            destination,
            skills_dir / "active.json",
            platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir),
            today=today,
        )
    except (skillset.SkillSetError, approvals.ApprovalError) as error:
        raise CliError(f"activation_refused:{error.code}") from error
    return {
        "active": str(result.path),
        "build_id": result.build_id,
        "durable": result.durable,
        "skills": list(result.skill_set.skill_ids),
    }


def run_skill_rollback(paths: PlatformPaths, *, today: date | None = None) -> dict[str, Any]:
    """Restore the most recently retained generation, revalidating it first."""
    try:
        result = skillset.rollback_skillset(
            paths.data_dir / "skills" / "active.json",
            platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir),
            today=today,
        )
    except (skillset.SkillSetError, approvals.ApprovalError) as error:
        raise CliError(f"rollback_refused:{error.code}") from error
    return {"active": str(result.path), "build_id": result.build_id, "skills": list(result.skill_set.skill_ids)}


def run_skill_status(paths: PlatformPaths, *, today: date | None = None) -> dict[str, Any]:
    """Report what is active and what is sitting on disk. NEVER raises on a broken pointer: status is
    the command an operator runs precisely when something is wrong."""
    pointer = paths.data_dir / "skills" / "active.json"
    state = skillset.open_skillset(
        pointer,
        platform_module.load_trust_roots(),
        approvals.load_approvals(paths.data_dir),
        today=today,
    )
    report: dict[str, Any] = {
        "active": state.build_id,
        "skills": list(state.skill_set.skill_ids) if state.skill_set else [],
        "warning": state.warning,
    }
    try:
        inventory = skillset.list_generations(pointer)
        report["retained"] = [item.name for item in inventory.retained]
        report["unreferenced"] = [item.name for item in inventory.unreferenced]
    except skillset.SkillSetError as error:
        report["retained"], report["unreferenced"] = [], []
        # A fresh install has no pointer and that is not a fault. Only report the inventory as
        # UNAVAILABLE when a pointer exists and could not be read -- conflating "nothing here yet"
        # with "something is broken" would send an operator looking for a problem that is not there.
        if pointer.exists() or pointer.is_symlink():
            report["inventory_unavailable"] = error.code
    return report


def run_skill_prune(paths: PlatformPaths) -> dict[str, Any]:
    """Delete only unreferenced generations. Refuses entirely without a readable pointer, because
    nothing can be known to be unreferenced without one."""
    try:
        removed = skillset.prune_skillset(paths.data_dir / "skills" / "active.json")
    except skillset.SkillSetError as error:
        raise CliError(f"prune_refused:{error.code}") from error
    return {"removed": [item.name for item in removed]}


def cmd_skill_activate(args: argparse.Namespace) -> int:
    result = run_skill_activate(
        resolve_cli_paths(args), args.candidate or [], allow_unsigned_local=args.allow_unsigned_local
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Activated {len(result['skills'])} skill(s) as {result['build_id'][:16]}", file=sys.stderr)
    if not result["durable"]:
        print("Warning: the directory fsync failed; the pointer is written but less durable.", file=sys.stderr)
    return 0


def cmd_skill_rollback(args: argparse.Namespace) -> int:
    result = run_skill_rollback(resolve_cli_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Rolled back to {result['build_id'][:16]}", file=sys.stderr)
    return 0


def cmd_skill_status(args: argparse.Namespace) -> int:
    result = run_skill_status(resolve_cli_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["warning"]:
        print(f"Skills layer disabled: {result['warning']}", file=sys.stderr)
    elif result["active"] is None:
        print("Skills layer inactive: nothing has been activated.", file=sys.stderr)
    return 0


def cmd_skill_prune(args: argparse.Namespace) -> int:
    result = run_skill_prune(resolve_cli_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Removed {len(result['removed'])} unreferenced generation(s).", file=sys.stderr)
    return 0


def cmd_skill_ingest(args: argparse.Namespace) -> int:
    result = run_skill_ingest(resolve_cli_paths(args), args.pack)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Stored candidate at {result['path']}", file=sys.stderr)
    return 0


def cmd_skill_analyze(args: argparse.Namespace) -> int:
    result = run_skill_analyze(args.candidate)
    print(json.dumps(result, indent=2, sort_keys=True))
    count = len(result["advisories"])
    print(f"{count} advisor{'y' if count == 1 else 'ies'} for review.", file=sys.stderr)
    print(_ANALYSIS_LIMITS, file=sys.stderr)
    return 0


def cmd_skill_approve(args: argparse.Namespace) -> int:
    result = run_skill_approve(resolve_cli_paths(args), args.candidate, args.acknowledge or [])
    print(json.dumps(result, indent=2, sort_keys=True))
    for record in result["approved"]:
        print(f"Approved {record['skill_id']} bound to {record['body_digest']}", file=sys.stderr)
    return 0


def cmd_skill_sign(args: argparse.Namespace) -> int:
    result = run_skill_sign(args.key, args.signer, args.pack, args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Wrote manifest to {result['manifest']}", file=sys.stderr)
    return 0
