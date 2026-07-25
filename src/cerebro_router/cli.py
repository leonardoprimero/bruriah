from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import sys
import uuid
from array import array
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import anyio
import mcp.server.stdio
import yaml
from fastembed import TextEmbedding

from . import clients
from .corpus import CorpusPolicy, CorpusPolicyError
from .index import BuildConfig, BuildResult, Embedder, IndexLifecycleError, build_candidate, promote_candidate
from .mcp_server import build_server
from .platform import (
    PlatformError, PlatformPaths, ensure_private_dirs, load_deps, load_registry, open_snapshot,
    resolve_paths, write_build_descriptor,
)
from .service import ServiceDeps

# Slice 8A-2: `cerebro-mcp {init,serve,index,doctor}` over Slice 8A-1's `platform.py` loader.
# `_embedding_fingerprint`/`main` below stay unchanged (Slice-3 entry point, imported by name).
# Slice 12B-2: `init` now also renders and writes all six `clients.py` client configs (see
# `_build_launch_manifest`/`run_client_configs` below) under a private `clients/` subdir.
EmbedderFactory = Callable[[str], tuple[Embedder, str, int]]


class CliError(ValueError):
    """Typed failure for every `cerebro-mcp` command, mirroring `PlatformError`/`ServiceError`."""

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
    parser = argparse.ArgumentParser(prog="cerebro-router")
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
    config = BuildConfig(
        root=root, policy_path=policy_path, schema_version=1, parser_version="corpus-v1",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model=model_name,
        embedding_revision=revision, embedding_dimensions=dimensions,
        embedding_fingerprint=fingerprint, ranking_config="rrf-v1",
    )
    candidate_path = paths.data_dir / f"candidate-{uuid.uuid4().hex}.sqlite3"
    result = build_candidate(config, candidate_path, policy, embed)
    promote_candidate(candidate_path, paths.data_dir / "active.json", config, policy)
    write_build_descriptor(paths, config)
    return result


_FRESHNESS_WARNING_DAYS = 7


def run_doctor(paths: PlatformPaths, *, today: date | None = None) -> dict[str, object]:
    """Read-only: resolved dirs, registry load, snapshot open. Never creates/writes anything.
    Adds an early WARNING within `_FRESHNESS_WARNING_DAYS` of pack staleness; the hard
    fail-closed staleness/expiry check in `load_registry` itself is unchanged."""
    effective_today = today or date.today()
    report: dict[str, object] = {
        "config_dir": str(paths.config_dir), "data_dir": str(paths.data_dir),
        "dirs_exist": {
            "config": paths.config_dir.is_dir(), "data": paths.data_dir.is_dir(),
            "cache": paths.cache_dir.is_dir(), "log": paths.log_dir.is_dir(),
        },
        "network_enabled": paths.network_enabled, "warnings": [],
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
        config_file.write_text(default, encoding="utf-8")
    return config_file


def _build_launch_manifest(paths: PlatformPaths) -> clients.LaunchManifest:
    """The one manifest every rendered client config derives from (Slice 12B-2): invokes this
    module directly via `sys.executable` (absolute, always importable) rather than a `which
    cerebro-mcp` lookup -- no packaged console-script entry point exists until Slice 8B."""
    return clients.LaunchManifest(
        command=sys.executable,
        args=(
            "-m", "cerebro_router.cli", "serve",
            "--config-dir", str(paths.config_dir), "--data-dir", str(paths.data_dir),
            "--cache-dir", str(paths.cache_dir), "--log-dir", str(paths.log_dir),
        ),
        server_name="cerebro-router",
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
        target.write_text(rendered, encoding="utf-8")
        written[client_id] = target
    if os.name == "posix":
        os.chmod(clients_dir, 0o700)
        for target in written.values():
            os.chmod(target, 0o600)
    return written


def build_serve_deps(paths: PlatformPaths) -> ServiceDeps:
    """Load real `ServiceDeps`; split from `_serve_stdio` so tests verify wiring, never the loop."""
    try:
        return load_deps(paths)
    except PlatformError as error:
        raise CliError(error.code) from error


async def _serve_stdio(deps: ServiceDeps) -> None:
    server = build_server(deps)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _resolve_paths(args: argparse.Namespace) -> PlatformPaths:
    try:
        return resolve_paths(
            cli_config_dir=args.config_dir, cli_data_dir=args.data_dir,
            cli_cache_dir=args.cache_dir, cli_log_dir=args.log_dir,
            cli_network_enabled=args.network_enabled,
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


def _cmd_index(
    args: argparse.Namespace, *, embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    paths = _resolve_paths(args)
    if not args.corpus_root.is_dir():
        raise CliError("corpus_root_not_found")
    if not args.policy.is_file():
        raise CliError("policy_not_found")
    try:
        result = run_index(
            paths, args.corpus_root, args.policy, model_name=args.model,
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


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cerebro-mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str, handler: Callable) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        _add_platform_arguments(sub)
        sub.set_defaults(handler=handler)
        return sub

    add("init", "Create private config and print client snippets.", _cmd_init)
    index_parser = add("index", "Build and promote a candidate index.", _cmd_index)
    index_parser.add_argument("--corpus-root", type=Path, required=True)
    index_parser.add_argument("--policy", type=Path, required=True)
    index_parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    add("serve", "Run the two-tool MCP server over stdio.", _cmd_serve)
    add("doctor", "Read-only health check.", _cmd_doctor)
    return parser


def cerebro_mcp_main(argv: list[str] | None = None) -> int:
    """Dispatch `cerebro-mcp {init,serve,index,doctor}`; every failure is one typed `CliError`."""
    args = _build_cli_parser().parse_args(argv)
    try:
        return args.handler(args)
    except CliError as error:
        print(f"cerebro-mcp: error: {error.code}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- CLI boundary: no bare traceback ever, closing the
        # untyped-escape class (KeyboardInterrupt is a BaseException and still propagates for shutdown).
        print(f"cerebro-mcp: error: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cerebro_mcp_main())
