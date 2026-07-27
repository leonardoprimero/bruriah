from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import uuid
import warnings
from array import array
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anyio
import mcp.server.stdio
import yaml
from fastembed import TextEmbedding

from . import __version__, cache, clients, gitcorpus
from .corpus import CorpusPolicy, CorpusPolicyError
from .index import (
    BuildConfig, BuildResult, Embedder, IndexLifecycleError, build_candidate, promote_candidate,
    prune_generations,
)
from .mcp_server import build_server
from . import approvals, candidates, platform as platform_module, signing, skillset
from .platform import (
    PlatformError, PlatformPaths, ensure_private_dirs, load_build_descriptor, load_deps,
    load_registry, open_snapshot, resolve_paths, write_build_descriptor,
)
from .contracts import InvestigationRequest, ReadRequest
from .service import ServiceDeps, investigate, read

# Slice 8A-2: `bruriah {init,serve,index,doctor}` over Slice 8A-1's `platform.py` loader.
# `_embedding_fingerprint`/`main` below stay unchanged (Slice-3 entry point, imported by name).
# Slice 12B-2: `init` now also renders and writes all six `clients.py` client configs (see
# `_build_launch_manifest`/`run_client_configs` below) under a private `clients/` subdir.
# Slice 12D: `doctor` now also reports READ-ONLY cache stats (entry count, expired count, total
# bytes) via `cache.cache_stats` -- design.md's "`doctor` is read-only" is preserved: `doctor`
# never calls `cache.prune_expired`; no mutating prune is exposed via this CLI at all.
# BUGFIX (vector-leg wiring): `build_serve_deps` previously called `platform.load_deps(paths)`
# with no `embed_query`, so `serve` always ran retrieval BM25-only (`vector_leg_unavailable`).
# It now builds a real query embedder via the SAME `EmbedderFactory` `_cmd_index` uses (so query
# vectors share the exact code path passage vectors were built with) using the model recorded in
# the active snapshot's own build descriptor, verifies that embedder's fingerprint/dimensions
# fail-closed against the descriptor before ever using it, and threads it into `load_deps`.
# `doctor`/`platform.load_deps` stay untouched: `embed_query` still defaults to `None` there.
EmbedderFactory = Callable[[str], tuple[Embedder, str, int]]


class CliError(ValueError):
    """Typed failure for every `bruriah` command, mirroring `PlatformError`/`ServiceError`."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _embedding_fingerprint(model: TextEmbedding) -> str:
    backend = model.model
    description = backend.model_description
    pooling = {
        "OnnxTextEmbedding": "cls-normalized",
        "PooledEmbedding": "mean",
        "PooledNormalizedEmbedding": "mean-normalized",
    }.get(type(backend).__name__)
    source = description.sources.hf or description.sources.url
    model_dir = Path(backend._model_dir).resolve()
    relative_artifact = Path(description.model_file)
    artifact = model_dir / relative_artifact
    if (
        not pooling
        or not source
        or relative_artifact.is_absolute()
        or ".." in relative_artifact.parts
        or not artifact.is_file()
    ):
        raise ValueError("unsupported_embedding_runtime")
    return json.dumps(
        {
            "artifact": description.model_file,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "pooling": pooling,
            "runtime": f"fastembed=={importlib.metadata.version('fastembed')}",
            "snapshot": model_dir.name,
            "source": source,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="bruriah")
    parser.add_argument("root", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    parser.add_argument("--model-revision")
    parser.add_argument("--dimensions", type=int)
    arguments = parser.parse_args()
    model = TextEmbedding(model_name=arguments.model)
    fingerprint = _embedding_fingerprint(model)
    actual_revision = json.loads(fingerprint)["snapshot"]
    if arguments.model_revision and arguments.model_revision != actual_revision:
        parser.error("--model-revision does not match the verified model snapshot")
    if arguments.dimensions and arguments.dimensions != model.embedding_size:
        parser.error("--dimensions does not match the loaded model")

    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", vector).tobytes() for vector in model.embed(texts)]

    policy = CorpusPolicy.load(arguments.policy)
    result = build_candidate(
        BuildConfig(
            arguments.root,
            arguments.policy,
            1,
            "corpus-v1",
            "0.1.0",
            ">=1.28.1,<2",
            arguments.model,
            actual_revision,
            model.embedding_size,
            fingerprint,
            "rrf-v1",
        ),
        arguments.candidate,
        policy,
        embed,
        previous=arguments.previous,
    )
    print(result.path)


def _default_embedder_factory(model_name: str) -> tuple[Embedder, str, int]:
    """Real fastembed model; tests inject a fake factory so the suite never loads real ONNX."""
    with warnings.catch_warnings():
        # fastembed announces on every construction that this model now pools by mean rather than
        # CLS, which lands in the middle of `index` and `ask --read` and reads, to someone running
        # the quickstart, like something went wrong. The change it reports is real and is already
        # handled harder than a printed line can manage: `_embedding_fingerprint` reads the
        # pooling off the backend class, REFUSES a runtime it cannot name, and writes the answer
        # into `embedding_fingerprint` -- which is part of what an index is validated against, so
        # a pooling change invalidates the index rather than scrolling past. That is pinned by
        # test_fastembed_fingerprint_binds_pooling_source_snapshot_and_artifact and by the pooling
        # case in the activation-metadata table.
        #
        # Matched by message, never by category: any OTHER warning fastembed raises has no such
        # backstop and must still reach the user.
        warnings.filterwarnings(
            "ignore", message=".*mean pooling instead of CLS.*", category=UserWarning
        )
        model = TextEmbedding(model_name=model_name)
    fingerprint = _embedding_fingerprint(model)

    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", vector).tobytes() for vector in model.embed(texts)]

    return embed, fingerprint, model.embedding_size


def run_index(
    paths: PlatformPaths, root: Path, policy_path: Path, *, model_name: str,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> BuildResult:
    """Build+promote a candidate into the private `data_dir` (never `cerebro.db`); runs
    `ensure_private_dirs` first, closing the carried Slice 8A-1 descriptor-on-missing-dir WARNING."""
    ensure_private_dirs(paths)
    policy = CorpusPolicy.load(policy_path)
    embed, fingerprint, dimensions = embedder_factory(model_name)
    revision = json.loads(fingerprint)["snapshot"]
    # `service_version` is NOT `bruriah.__version__` and must not be wired to it. It belongs to the
    # same family as `parser_version="corpus-v1"` and `ranking_config="rrf-v1"`: a symbolic marker of
    # the snapshot contract, carried into the `expected` metadata that `promote_candidate` validates
    # (`index.py`). Binding it to the package version would put every release into the index
    # identity, so a patch bump would refuse the user's existing snapshot and force a full re-embed
    # of their corpus. It moves when the snapshot contract moves, and it has not moved.
    config = BuildConfig(
        root=root, policy_path=policy_path, schema_version=1, parser_version="corpus-v1",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model=model_name,
        embedding_revision=revision, embedding_dimensions=dimensions,
        embedding_fingerprint=fingerprint, ranking_config="rrf-v1",
    )
    candidate_path = paths.data_dir / f"candidate-{uuid.uuid4().hex}.sqlite3"
    result = build_candidate(config, candidate_path, policy, embed)
    activation = promote_candidate(candidate_path, paths.data_dir / "active.json", config, policy)
    if activation.retention_discarded:
        print(
            "Note: the previous index in this data directory was built under a different "
            "configuration -- an edited policy, or another project -- so it was not kept as a "
            "rollback target. The new index is active. Give each project its own --data-dir to "
            "keep their generations separate.",
            file=sys.stderr,
        )
    write_build_descriptor(paths, config)
    return result


_FRESHNESS_WARNING_DAYS = 7


def run_doctor(
    paths: PlatformPaths, *, today: date | None = None, now: datetime | None = None,
) -> dict[str, object]:
    """Read-only: resolved dirs, registry load, snapshot open, cache stats. Never creates/writes
    anything -- including the cache: `cache.cache_stats` only reads (never calls
    `cache.prune_expired`), so a mutating prune stays out of `doctor` entirely (Slice 12D).
    Adds an early WARNING within `_FRESHNESS_WARNING_DAYS` of pack staleness; the hard
    fail-closed staleness/expiry check in `load_registry` itself is unchanged."""
    effective_today = today or date.today()
    effective_now = now or datetime.now(timezone.utc)
    report: dict[str, object] = {
        "config_dir": str(paths.config_dir), "data_dir": str(paths.data_dir),
        "dirs_exist": {
            "config": paths.config_dir.is_dir(), "data": paths.data_dir.is_dir(),
            "cache": paths.cache_dir.is_dir(), "log": paths.log_dir.is_dir(),
        },
        "network_enabled": paths.network_enabled,
        # Surfaced because six first-party skills ship and the default admits five: without this,
        # the operator sees `skill_ceiling_exceeded:1` in a response and has no way to learn what
        # the number is, let alone that it is theirs to change.
        "skill_ceiling": paths.skill_ceiling,
        # Every site that narrows permissions is already guarded by `os.name == "posix"`, so on
        # Windows those calls correctly do nothing rather than pretending -- `os.chmod` there only
        # toggles a read-only attribute and would be theatre. What was missing is that the user had
        # no way to LEARN this. In practice the data lives under the per-user profile directory,
        # whose inherited ACL already denies other standard users, so the protection is real; it is
        # just not the one the code asked for, and not one this process verified. Writing an
        # explicit DACL was considered and rejected: a hand-rolled ACL that looks restrictive while
        # inheriting something permissive is precisely the silently-weaker outcome this codebase
        # refuses everywhere else. Saying so out loud is the honest version.
        "owner_only_file_modes": os.name == "posix",
        "warnings": (
            [] if os.name == "posix" else
            ["this platform does not enforce owner-only file modes; private data is protected by "
             "the user profile directory's inherited permissions, which bruriah does not verify"]
        ),
    }
    try:
        registry = load_registry(effective_today)
        report["registry"] = {"status": "ok", "pack_ids": list(registry.pack_ids)}
        for pack in registry.packs:
            days_left = (pack.reviewed_at + timedelta(days=pack.freshness_days) - effective_today).days
            if days_left <= _FRESHNESS_WARNING_DAYS:
                report["warnings"].append(f"pack {pack.pack_id} goes stale in {days_left} day(s)")
    except PlatformError as error:
        report["registry"] = {"status": "error", "code": error.code}
    try:
        snapshot = open_snapshot(paths)
        snapshot.database.close()
        report["snapshot"] = {"status": "ok", "build_id": snapshot.build_id}
    except PlatformError as error:
        report["snapshot"] = {"status": "error", "code": error.code}
    stats = cache.cache_stats(paths.cache_dir, now=effective_now)
    report["cache"] = {
        "entries": stats.entries, "expired": stats.expired, "total_bytes": stats.total_bytes,
    }
    report["healthy"] = (
        report["registry"].get("status") == "ok" and report["snapshot"].get("status") == "ok"
    )
    return report


def run_init(paths: PlatformPaths) -> Path:
    """Create private dirs + a default `config.json` (idempotent); registers no client/legacy config."""
    ensure_private_dirs(paths)
    config_file = paths.config_dir / "config.json"
    if not config_file.exists():
        default = json.dumps({"network_enabled": False}, sort_keys=True) + "\n"
        config_file.write_text(default, encoding="utf-8", newline="\n")
    return config_file


def _build_launch_manifest(paths: PlatformPaths) -> clients.LaunchManifest:
    """The one manifest every rendered client config derives from (Slice 12B-2): invokes this
    module directly via `sys.executable` (absolute, always importable) rather than a `which
    bruriah` lookup -- no packaged console-script entry point exists until Slice 8B."""
    return clients.LaunchManifest(
        command=sys.executable,
        args=(
            "-m", "bruriah.cli", "serve",
            "--config-dir", str(paths.config_dir), "--data-dir", str(paths.data_dir),
            "--cache-dir", str(paths.cache_dir), "--log-dir", str(paths.log_dir),
        ),
        server_name="bruriah",
    )


def run_client_configs(
    paths: PlatformPaths, manifest: clients.LaunchManifest,
) -> dict[clients.ClientId, Path]:
    """Render and persist all six client configs under a private `clients/` subdir of
    `config_dir`; deterministic content and `ClientId` declaration order (`clients.render_all`).
    Private permissions (0700 dir / 0600 files) on POSIX, reusing `audit.py`'s
    write-then-chmod pattern; Windows ACL NOT VALIDATED on this Darwin host."""
    clients_dir = paths.config_dir / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)
    written: dict[clients.ClientId, Path] = {}
    for client_id, rendered in clients.render_all(manifest).items():
        target = clients_dir / f"{client_id.value}.json"
        target.write_text(rendered, encoding="utf-8", newline="\n")
        written[client_id] = target
    if os.name == "posix":
        os.chmod(clients_dir, 0o700)
        for target in written.values():
            os.chmod(target, 0o600)
    return written


def build_serve_deps(
    paths: PlatformPaths, *, embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> ServiceDeps:
    """Load real `ServiceDeps`, including a real query embedder; split from `_serve_stdio` so
    tests verify wiring, never the loop.

    The query embedder is built by REUSING the identical batch `Embedder` factory `_cmd_index`
    uses (`embed_query = lambda q: embedder([q])[0]`), so the query vector comes from the exact
    same code path as the passage vectors -- matching by construction, not by luck. The model
    is never a blind default: it is read from the ACTIVE SNAPSHOT's own recorded build
    descriptor (`load_build_descriptor`), i.e. whatever model the snapshot was actually built
    with. Before ever embedding a real query with it, the constructed embedder's real
    fingerprint/dimensions are compared fail-closed against the descriptor's recorded
    `embedding_fingerprint`/`embedding_dimensions`; ANY mismatch raises a typed
    `embedding_model_mismatch` `CliError` instead of silently poisoning the vector leg with a
    query embedding space that does not match the indexed passage vectors -- worse than leaving
    the vector leg off entirely."""
    try:
        descriptor = load_build_descriptor(paths)
        embed, fingerprint, dimensions = embedder_factory(descriptor.embedding_model)
        if (
            fingerprint != descriptor.embedding_fingerprint
            or dimensions != descriptor.embedding_dimensions
        ):
            raise CliError("embedding_model_mismatch")

        def embed_query(text: str) -> bytes:
            return embed([text])[0]

        return load_deps(paths, embed_query=embed_query)
    except PlatformError as error:
        raise CliError(error.code) from error


async def _serve_stdio(deps: ServiceDeps) -> None:
    server = build_server(deps)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# The skill lifecycle is CLI-confined by design: the MCP surface stays two read-only tools, and
# every mutation -- ingest, approve, sign -- happens here, run by a human who can see what they are
# agreeing to. `run_*` carries the behaviour and is directly testable; `_cmd_*` only formats.

_ANALYSIS_LIMITS = (
    "Structural checks only. An advisory means a person should look at this, never that it is "
    "dangerous, and the absence of advisories never means it is safe. Whether prose persuades an "
    "agent to act against you is not observable by inspection; the permission envelope and your own "
    "reading of the text are what protect you."
)


def run_skill_ingest(paths: PlatformPaths, source: Path) -> dict[str, object]:
    """Store a candidate pack privately, addressed by its own content digest."""
    try:
        record = candidates.ingest_candidate(source, paths.data_dir)
    except candidates.CandidateError as error:
        raise CliError(f"candidate_rejected:{error.code}") from error
    return {"path": str(record.path), "digest": record.digest,
            "pack_id": record.pack_id, "version": record.version}


def run_skill_analyze(candidate: Path) -> dict[str, object]:
    """Report structural findings. The result carries its own limits so that copying the output
    copies the caveat with it -- a report that travels without its disclaimer becomes a clearance."""
    try:
        report = candidates.analyze_candidate(candidate)
    except candidates.CandidateError as error:
        raise CliError(f"candidate_rejected:{error.code}") from error
    return {
        "digest": report.digest, "pack_id": report.pack_id, "version": report.version,
        "skill_ids": list(report.skill_ids),
        "advisories": [
            {"id": item.identifier, "code": item.code, "skill_id": item.skill_id,
             "detail": item.detail}
            for item in report.advisories
        ],
        "analysis_limits": _ANALYSIS_LIMITS,
    }


def run_skill_approve(
    paths: PlatformPaths, candidate: Path, acknowledge: list[str], *, today: date | None = None,
) -> dict[str, object]:
    """Record human approval, bound to each skill's current body digest."""
    try:
        records = approvals.approve_candidate(
            candidate, paths.data_dir, acknowledge=acknowledge, today=today or date.today(),
        )
    except approvals.ApprovalError as error:
        raise CliError(f"approval_refused:{error.code}") from error
    return {"approved": [record.as_json() for record in records],
            "analysis_limits": _ANALYSIS_LIMITS}


def run_skill_sign(key: Path, signer: str, pack: Path, out: Path | None) -> dict[str, object]:
    """Sign a pack through the same `signing` module the release script uses, so a manifest produced
    here and one produced there cannot drift apart."""
    try:
        manifest = signing.sign_pack(key, signer, pack, out)
    except signing.SigningError as error:
        raise CliError(f"signing_failed:{error.code}") from error
    return {"manifest": str(manifest), "signer": signer,
            "note": "A signature establishes who signed these bytes. It is not a claim that they "
                    "are correct or safe."}


def run_skill_activate(
    paths: PlatformPaths, candidates_: list[Path], *,
    allow_unsigned_local: bool = False, today: date | None = None,
) -> dict[str, object]:
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
            sources, destination, platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir), today=today,
        )
        result = skillset.promote_skillset(
            destination, skills_dir / "active.json", platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir), today=today,
        )
    except (skillset.SkillSetError, approvals.ApprovalError) as error:
        raise CliError(f"activation_refused:{error.code}") from error
    return {"active": str(result.path), "build_id": result.build_id, "durable": result.durable,
            "skills": list(result.skill_set.skill_ids)}


def run_skill_rollback(paths: PlatformPaths, *, today: date | None = None) -> dict[str, object]:
    """Restore the most recently retained generation, revalidating it first."""
    try:
        result = skillset.rollback_skillset(
            paths.data_dir / "skills" / "active.json", platform_module.load_trust_roots(),
            approvals.load_approvals(paths.data_dir), today=today,
        )
    except (skillset.SkillSetError, approvals.ApprovalError) as error:
        raise CliError(f"rollback_refused:{error.code}") from error
    return {"active": str(result.path), "build_id": result.build_id,
            "skills": list(result.skill_set.skill_ids)}


def run_skill_status(paths: PlatformPaths, *, today: date | None = None) -> dict[str, object]:
    """Report what is active and what is sitting on disk. NEVER raises on a broken pointer: status is
    the command an operator runs precisely when something is wrong."""
    pointer = paths.data_dir / "skills" / "active.json"
    state = skillset.open_skillset(
        pointer, platform_module.load_trust_roots(), approvals.load_approvals(paths.data_dir),
        today=today,
    )
    report: dict[str, object] = {
        "active": state.build_id, "skills": list(state.skill_set.skill_ids) if state.skill_set else [],
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


def run_skill_prune(paths: PlatformPaths) -> dict[str, object]:
    """Delete only unreferenced generations. Refuses entirely without a readable pointer, because
    nothing can be known to be unreferenced without one."""
    try:
        removed = skillset.prune_skillset(paths.data_dir / "skills" / "active.json")
    except skillset.SkillSetError as error:
        raise CliError(f"prune_refused:{error.code}") from error
    return {"removed": [item.name for item in removed]}


def _manifest_for(candidate: Path) -> Path | None:
    manifest = candidate.with_suffix(".manifest.json")
    return manifest if manifest.is_file() else None


def _cmd_skill_activate(args: argparse.Namespace) -> int:
    result = run_skill_activate(_resolve_paths(args), args.candidate or [],
                                allow_unsigned_local=args.allow_unsigned_local)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Activated {len(result['skills'])} skill(s) as {result['build_id'][:16]}", file=sys.stderr)
    if not result["durable"]:
        print("Warning: the directory fsync failed; the pointer is written but less durable.",
              file=sys.stderr)
    return 0


def _cmd_skill_rollback(args: argparse.Namespace) -> int:
    result = run_skill_rollback(_resolve_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Rolled back to {result['build_id'][:16]}", file=sys.stderr)
    return 0


def _cmd_skill_status(args: argparse.Namespace) -> int:
    result = run_skill_status(_resolve_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["warning"]:
        print(f"Skills layer disabled: {result['warning']}", file=sys.stderr)
    elif result["active"] is None:
        print("Skills layer inactive: nothing has been activated.", file=sys.stderr)
    return 0


def _cmd_skill_prune(args: argparse.Namespace) -> int:
    result = run_skill_prune(_resolve_paths(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Removed {len(result['removed'])} unreferenced generation(s).", file=sys.stderr)
    return 0


def _cmd_index_prune(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    try:
        removed = prune_generations(paths.data_dir / "active.json")
    except (IndexLifecycleError, OSError) as error:
        raise CliError(f"index_prune_failed:{getattr(error, 'code', type(error).__name__)}") from error
    print(json.dumps({"removed": sorted(path.name for path in removed)}, indent=2, sort_keys=True))
    print(f"Removed {len(removed)} unreferenced generation(s).", file=sys.stderr)
    return 0


def _cmd_skill_ingest(args: argparse.Namespace) -> int:
    result = run_skill_ingest(_resolve_paths(args), args.pack)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Stored candidate at {result['path']}", file=sys.stderr)
    return 0


def _cmd_skill_analyze(args: argparse.Namespace) -> int:
    result = run_skill_analyze(args.candidate)
    print(json.dumps(result, indent=2, sort_keys=True))
    count = len(result["advisories"])
    print(f"{count} advisor{'y' if count == 1 else 'ies'} for review.", file=sys.stderr)
    print(_ANALYSIS_LIMITS, file=sys.stderr)
    return 0


def _cmd_skill_approve(args: argparse.Namespace) -> int:
    result = run_skill_approve(_resolve_paths(args), args.candidate, args.acknowledge or [])
    print(json.dumps(result, indent=2, sort_keys=True))
    for record in result["approved"]:
        print(f"Approved {record['skill_id']} bound to {record['body_digest']}", file=sys.stderr)
    return 0


def _cmd_skill_sign(args: argparse.Namespace) -> int:
    result = run_skill_sign(args.key, args.signer, args.pack, args.out)
    print(json.dumps(result, indent=2, sort_keys=True))
    print(f"Wrote manifest to {result['manifest']}", file=sys.stderr)
    return 0


def _resolve_paths(args: argparse.Namespace) -> PlatformPaths:
    try:
        return resolve_paths(
            cli_config_dir=args.config_dir, cli_data_dir=args.data_dir,
            cli_cache_dir=args.cache_dir, cli_log_dir=args.log_dir,
            cli_network_enabled=args.network_enabled,
            cli_skill_ceiling=getattr(args, "skill_ceiling", None),
        )
    except PlatformError as error:
        raise CliError(error.code) from error


def _cmd_init(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    config_file = run_init(paths)
    print(f"Wrote private configuration to {config_file}", file=sys.stderr)
    try:
        manifest = _build_launch_manifest(paths)
        written = run_client_configs(paths, manifest)
    except clients.ClientError as error:
        raise CliError(f"client_manifest_invalid:{error.code}") from error
    print("Wrote client configs:", file=sys.stderr)
    for client_id, target in written.items():
        capability = clients.CLIENT_CAPABILITIES[client_id]
        print(f"  {capability.display_name}: {target}", file=sys.stderr)
        print(f"    -> expected location: {capability.config_path_hint}", file=sys.stderr)
    print("See docs/client-guidance.md for full per-client detail.", file=sys.stderr)
    print(clients.render_generic_stdio(manifest))
    return 0


def _cmd_corpus(args: argparse.Namespace) -> int:
    """Step one of the documented workflow, and for a while the only one you could not install.

    It lived in `scripts/git_corpus.py`, which no wheel ships, so the front page opened by telling
    a reader to run a file that `pip install bruriah` had never put on their disk."""
    if not (args.repo / ".git").exists():
        raise CliError("not_a_git_repository")
    result = gitcorpus.build(args.repo, args.out, args.limit)
    print(json.dumps(
        {"documents": result.written, "commits_examined": result.examined, "out": str(args.out)},
        indent=2, sort_keys=True,
    ))
    if result.written == 0:
        print(
            "No commit carried an explanatory body. This history records what changed but not why, "
            "so there is no reasoning to retrieve -- retrieval quality cannot compensate for that.",
            file=sys.stderr,
        )
    elif result.written < result.examined:
        # Stated whenever anything was dropped, at no particular threshold. Coverage is THE
        # determinant of whether this is worth installing, and picking a percentage below which
        # to speak would be a constant nobody measured deciding when the user gets to know. The
        # ratio is cheap to print and the reader can judge their own history.
        print(
            f"{result.written} of {result.examined} non-merge commits carried an explanatory body; "
            f"the other {result.examined - result.written} record what changed but not why and "
            "were skipped. That share is the ceiling on what any of this can retrieve.",
            file=sys.stderr,
        )
    return 0


def _cmd_index(
    args: argparse.Namespace, *, embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    paths = _resolve_paths(args)
    if not args.corpus_root.is_dir():
        raise CliError("corpus_root_not_found")
    if not args.policy.is_file():
        raise CliError("policy_not_found")
    # Resolve BEFORE building: these two paths are persisted in the build descriptor and read back
    # by `serve` and `doctor`, in a different process whose working directory nobody controls. An
    # MCP host launches the server from wherever it happens to be, so a relative `--policy` that
    # worked at index time names nothing at serve time, `_validate_stored` fails to re-read it, and
    # the user is told `snapshot_unreadable:invalid_active_target` -- an error about the snapshot,
    # which is intact, rather than about the path. Storing what the user typed only works while the
    # working directory never changes, and for a server that is never.
    corpus_root = args.corpus_root.resolve()
    policy = args.policy.resolve()
    try:
        result = run_index(
            paths, corpus_root, policy, model_name=args.model,
            embedder_factory=embedder_factory,
        )
    except (CorpusPolicyError, IndexLifecycleError, FileExistsError, ValueError, OSError, yaml.YAMLError) as error:
        # yaml.YAMLError (a malformed --policy that exists) is neither ValueError nor OSError.
        raise CliError(f"index_failed:{getattr(error, 'code', type(error).__name__)}") from error
    summary = {"build_id": result.build_id, "documents": result.documents, "passages": result.passages}
    print(json.dumps(summary, sort_keys=True))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    deps = build_serve_deps(paths)
    try:
        anyio.run(_serve_stdio, deps)
    except OSError as error:
        raise CliError("serve_io_error") from error
    finally:
        deps.snapshot.database.close()
    return 0


def _cmd_ask(
    args: argparse.Namespace, *, embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    """Run one investigation from the terminal and show what came back.

    This exists because until it did, there was no way to see the central capability work. You
    installed, you indexed, and then you had to wire up an entire MCP client before anything was
    observable -- which is exactly the failure `undiscoverable-is-unbuilt` describes, sitting in
    the middle of the product it shipped with.

    It is a VIEWER, not a third tool. It answers nothing: there is no generative model here, so
    what it prints is the evidence and the disclosure, in the same two steps an agent takes. The
    MCP surface is still exactly `investigate_work` and `read_evidence`.
    """
    paths = _resolve_paths(args)
    deps = build_serve_deps(paths, embedder_factory=embedder_factory)
    # The snapshot holds an open SQLite connection. `_cmd_serve` closes it in a finally and
    # `run_doctor` closes it before returning; this one never did, across five exits. On POSIX
    # that is a ResourceWarning nobody reads, and on Windows an unclosed connection is what
    # makes a generation undeletable -- the same shape that failed `index-prune` in CI.
    try:
        result = investigate(InvestigationRequest(task=args.question, host_skills=[]), deps)
        payload = result.model_dump(mode="json")
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        local = [item for item in payload["evidence"] if item["kind"] == "local"]
        print(f"\n  status: {payload['status']} · {len(local)} references")
        for note in payload["degradation"]:
            print(f"  disclosed: {note}")
        for gap in payload["gaps"]:
            print(f"  gap: {gap}")
        if payload["status"] == "abstained":
            print("\n  No approved policy covers this domain, so nothing is returned rather than the\n"
                  "  nearest-looking passage. That is the designed answer, not a failure.\n")
            return 0

        # When a reference was named, list only it: relisting eight results above the text you
        # asked for buries the thing you asked for.
        shown = [(n, item) for n, item in enumerate(local, start=1)
                 if not args.read or n in args.read][: args.limit if not args.read else None]
        for position, item in shown:
            print(f"\n  [{position}] {item['citation_locator']}")
            print(f"      authority: {item['authority']} ({item['authority_rationale']})")
            print(f"      {item['digest']}")
        if not local:
            print("\n  Nothing in this corpus bears on that question.\n")
            return 0

        if args.read:
            chosen = [local[index - 1]["ref"] for index in args.read if 1 <= index <= len(local)]
            if not chosen:
                raise CliError("no_such_reference")
            print()
            for item in read(ReadRequest(refs=chosen), deps).model_dump(mode="json")["items"]:
                # `start`/`end` are character offsets into the passage, not line numbers -- they are
                # what the output budget is spent in and what `next_cursor` resumes from. Labelling
                # them "lines" printed 1-567 for a fifteen-line document. The line span is the
                # locator's, and it is the one a person wants next to the text.
                where = item["citation_locator"] or f"{item['ref'][:24]}…"
                print(f"  ── {where} · chars {item['start']}-{item['end']} " + "─" * 16)
                for line in item["content"].splitlines():
                    print(f"  {line}")
                print()
            return 0

        # This line is the bridge to the second call, so the whole two-call shape is behind it and a
        # suggestion that does not survive a paste is the design going undiscoverable one step from
        # the end. It used to truncate the question to 34 characters and append an ellipsis, and it
        # never carried the directory flags, so pasting it asked either a different question or the
        # right one of the wrong index. Double quotes rather than shlex: they are the form that runs
        # on every shell this project supports, including cmd.exe.
        # The values are quoted, not interpolated bare. The default data directory on macOS is
        # `~/Library/Application Support/bruriah`, so an unquoted path splits at the space and the
        # paste fails on the platform's own default -- and on Windows the same quoting is what keeps
        # a backslash path from being read as escapes. Double quotes hold both, in every shell here.
        directories = " ".join(
            f'--{name.replace("_", "-")} "{getattr(args, name)}"'
            for name in ("data_dir", "config_dir")
            if getattr(args, name, None) is not None
        )
        command = f'bruriah ask "{args.question}" --read 1' + (f" {directories}" if directories else "")
        print("\n  Nothing above is the document's text -- only references to it. That is the whole\n"
              f"  design: read one explicitly with\n\n    {command}\n")
        return 0
    finally:
        deps.snapshot.database.close()


def _cmd_doctor(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    report = run_doctor(paths)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["healthy"] else 1


def _add_platform_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--network-enabled", action=argparse.BooleanOptionalAction, default=None)
    # Left untyped as `int` on purpose: `type=int` would let argparse reject a bad value with its
    # own usage message and exit code, bypassing this module's typed-error discipline. Validation
    # happens once, in `resolve_paths`, so `--skill-ceiling -1` and a config file saying the same
    # thing fail identically.
    parser.add_argument("--skill-ceiling", default=None)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bruriah")
    # `docs/client-guidance.md` told readers to run this to find out which router version a config
    # targets, and through 0.3.0 there was nothing to run: the flag it named did not exist. A tool
    # whose subject is provenance has to be able to state its own version when asked.
    #
    # `action="version"` prints and exits while the flag is being parsed, so it answers before
    # `required=True` on the subcommand can reject the call for naming no command. Pinned by
    # test_it_reports_its_own_version_without_a_subcommand.
    parser.add_argument("--version", action="version", version=f"bruriah {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, handler: Callable) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        _add_platform_arguments(sub)
        sub.set_defaults(handler=handler)
        return sub

    add("init", "Create private config and print client snippets.", _cmd_init)
    corpus_parser = add("corpus", "Turn a git history's reasoning into a corpus.", _cmd_corpus)
    corpus_parser.add_argument("--repo", type=Path, default=Path("."), help="repository to read")
    corpus_parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    corpus_parser.add_argument("--limit", type=int, default=None, help="most recent N commits only")
    ask = add("ask", "Run one investigation and show the evidence.", _cmd_ask)
    ask.add_argument("question", help="what you want to know about your own corpus")
    ask.add_argument("--read", type=int, action="append", metavar="N",
                     help="read reference N's exact text. Repeat to read several.")
    ask.add_argument("--limit", type=int, default=8, help="references to list (default 8)")
    ask.add_argument("--json", action="store_true", help="the raw investigate_work result")
    index_parser = add("index", "Build and promote a candidate index.", _cmd_index)
    index_parser.add_argument("--corpus-root", type=Path, required=True)
    index_parser.add_argument("--policy", type=Path, required=True)
    index_parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    ingest = add("skill-ingest", "Store a candidate skill pack privately.", _cmd_skill_ingest)
    ingest.add_argument("--pack", type=Path, required=True)
    analyze = add("skill-analyze", "Report structural findings for review.", _cmd_skill_analyze)
    analyze.add_argument("--candidate", type=Path, required=True)
    approve = add("skill-approve", "Record approval, bound to the content digest.", _cmd_skill_approve)
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument("--acknowledge", action="append", metavar="SKILL_ID:CODE",
                         help="Acknowledge one advisory. Repeat for each; all are required.")
    sign = add("skill-sign", "Sign a pack with a release key held by path.", _cmd_skill_sign)
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--signer", required=True)
    sign.add_argument("--pack", type=Path, required=True)
    sign.add_argument("--out", type=Path, default=None)
    activate = add("skill-activate", "Compile and activate named approved candidates.", _cmd_skill_activate)
    activate.add_argument("--candidate", type=Path, action="append", required=True,
                          help="An approved candidate to activate. Repeat to activate several.")
    activate.add_argument("--allow-unsigned-local", action="store_true",
                          help="Permit unsigned local (T3) packs. Local-tier skills only.")
    add("skill-rollback", "Restore the previously retained skill set.", _cmd_skill_rollback)
    add("skill-status", "Show the active set and generations on disk.", _cmd_skill_status)
    add("index-prune", "Delete unreferenced index generations.", _cmd_index_prune)
    add("skill-prune", "Delete unreferenced skill-set generations.", _cmd_skill_prune)
    add("serve", "Run the two-tool MCP server over stdio.", _cmd_serve)
    add("doctor", "Read-only health check.", _cmd_doctor)
    return parser


def bruriah_main(argv: list[str] | None = None) -> int:
    """Dispatch `bruriah {init,serve,index,doctor}`; every failure is one typed `CliError`."""
    args = _build_cli_parser().parse_args(argv)
    try:
        return args.handler(args)
    except CliError as error:
        print(f"bruriah: error: {error.code}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- CLI boundary: no bare traceback ever, closing the
        # untyped-escape class (KeyboardInterrupt is a BaseException and still propagates for shutdown).
        print(f"bruriah: error: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(bruriah_main())
