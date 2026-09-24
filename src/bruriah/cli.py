from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import anyio
import mcp.server.stdio
import yaml
from fastembed import TextEmbedding

from . import __version__, agent_surface, clients, gitcorpus, github_corpus, pdfcorpus
from .github_read import ResponseCache as _GitHubResponseCache
from ._cli.common import (
    DEFAULT_EMBEDDING_MODEL,
    CliError,
    resolve_cli_paths as _resolve_paths,
    resolve_model_prefixes,
    is_symmetric_model,
)
from ._cli.doctor import cmd_doctor as _cmd_doctor, run_doctor
from ._cli.parser import build_cli_parser
from ._cli.skills import (
    cmd_skill_activate as _cmd_skill_activate,
    cmd_skill_analyze as _cmd_skill_analyze,
    cmd_skill_approve as _cmd_skill_approve,
    cmd_skill_ingest as _cmd_skill_ingest,
    cmd_skill_prune as _cmd_skill_prune,
    cmd_skill_rollback as _cmd_skill_rollback,
    cmd_skill_sign as _cmd_skill_sign,
    cmd_skill_status as _cmd_skill_status,
    run_skill_activate,
    run_skill_analyze,
    run_skill_approve,
    run_skill_ingest,
    run_skill_prune,
    run_skill_rollback,
    run_skill_sign,
    run_skill_status,
)
from .corpus import CorpusPolicy, CorpusPolicyError
from .index import BuildConfig, BuildResult, Embedder, IndexLifecycleError, build_candidate, prune_generations
from .index_runner import EmbedderFactory, _default_embedder_factory, _embedding_fingerprint, run_index
from .mcp_server import build_server
from .repository import SnapshotRepository, resolve_counterfactual_refs_for_humans
from .platform import (
    PlatformError,
    PlatformPaths,
    ensure_private_dirs,
    find_project_root,
    load_build_descriptor,
    load_deps,
    project_scoped_paths,
    resolve_paths,
)
from .aliases import AliasError, install_aliases, uninstall_aliases
from .hooks import HookError, install_hook, uninstall_hook
from .setup import SetupError, detect_installed_clients, setup_client
from .contracts import InvestigationRequest, ReadRequest
from .drift import DriftError, format_drift_human, format_drift_json, run_drift
from .github import GitHubError, detect_pr_context, post_review as gh_post_review
from .review import build_review, format_review_human, format_review_json, get_changed_lines
from .retrieval import Rerank
from .ui import UIError, run_ui
from .lens import LensError, format_lens_human, format_lens_json, run_lens
from .bootstrap import BootstrapError, run_bootstrap
from .impact import ImpactError, format_impact_human, format_impact_json, run_impact
from .guard import GuardError, format_guard_human, format_guard_json, run_guard
from .brief import BriefError, format_brief_human, format_brief_json, run_brief
from .decide import Alternative, DecideError, run_decide
from .heal import HealError, format_heal_agent, format_heal_human, format_heal_json, run_heal
from .service import ServiceDeps, investigate, read
from .why import WhyError, format_why_human, format_why_json, run_why

__all__ = [
    "CliError",
    "Embedder",
    "EmbedderFactory",
    "RerankerFactory",
    "bruriah_main",
    "build_serve_deps",
    "main",
    "resolve_model_prefixes",
    "resolve_paths",
    "run_bootstrap",
    "run_client_configs",
    "run_doctor",
    "run_drift",
    "run_index",
    "run_init",
    "run_skill_activate",
    "run_skill_analyze",
    "run_skill_approve",
    "run_skill_ingest",
    "run_skill_prune",
    "run_skill_rollback",
    "run_skill_sign",
    "run_skill_status",
    "run_why",
]

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
# `EmbedderFactory`, `_default_embedder_factory` and `_embedding_fingerprint` live in
# `.index_runner` (imported above) and are re-exported here so existing callers of
# `bruriah.cli.run_index`/`EmbedderFactory` keep working unchanged; see that module's docstring
# for why the runner cannot live inside `index.py` or `platform.py` themselves.
# Same shape as `EmbedderFactory`, for the same testing reason: the suite injects a fake so it
# never downloads or runs a real cross-encoder, and `--reranker` stays a documented operator
# choice rather than a silent default. Unlike the embedder there is NOTHING to fail closed
# against here -- a reranker touches no stored vector, so it cannot be mismatched with the
# snapshot and needs no fingerprint pinned in the build descriptor.
RerankerFactory = Callable[[str], Rerank]


def main() -> None:
    parser = argparse.ArgumentParser(prog="bruriah")
    parser.add_argument("root", type=Path)
    parser.add_argument("policy", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
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
            "corpus-v2",
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


def _default_reranker_factory(model_name: str) -> Rerank:
    """Real fastembed cross-encoder; tests inject a fake so the suite never loads real ONNX.

    Imported inside the function on purpose. `fastembed.rerank` pulls a second ONNX runtime graph
    into the process, and `doctor`, `index-prune` and every deps construction that never asked for
    a reranker must not pay for an import they will not use.
    """
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name=model_name)

    def rerank(query: str, documents: list[str]) -> list[float]:
        return [float(score) for score in model.rerank(query, documents)]

    return rerank


def _index_summary_line(result: BuildResult) -> str:
    """The one line `index` and `init --repo` both print, so the two cannot drift apart.

    The split is counted in DOCUMENTS because that is the unit reuse is decided in:
    `_stored_document` accepts or rejects a file's entire passage set, so a per-passage figure would
    be arithmetic the build never performs."""
    return (
        f"Index: {result.passages} passage(s) from {result.documents} document(s) "
        f"({result.reused_documents} reused, {result.documents - result.reused_documents} embedded)"
        f", build {result.build_id[:8]} is active"
    )


def run_init(paths: PlatformPaths) -> Path:
    """Create private dirs + a default `config.json` (idempotent); registers no client/legacy config."""
    ensure_private_dirs(paths)
    config_file = paths.config_dir / "config.json"
    if not config_file.exists():
        default = json.dumps({"network_enabled": False}, sort_keys=True) + "\n"
        config_file.write_text(default, encoding="utf-8", newline="\n")
    return config_file


# The front page's own default, byte for byte. Written only when no policy exists yet: an
# existing `policy.yaml` is the operator's and is never touched.
_DEFAULT_BOOTSTRAP_POLICY = "version: 1\ninclude: ['**']\nexclude: ['private/**']\n"


def _suggested_question(corpus_root: Path) -> str | None:
    """A first question this corpus is KNOWN to answer, so the first `ask` cannot come back empty.

    The newest document's name is `YYYY-MM-DD-sha8-<slug>`, and the slug is the commit subject the
    user themselves wrote -- asking "why" about it is guaranteed to have its answer in the index,
    which is the one property a suggested first question has to have."""
    documents = sorted(corpus_root.glob("*.md"))
    if not documents:
        return None
    words = documents[-1].stem[20:].replace("-", " ").strip()  # 'YYYY-MM-DD-sha8-' is 20 chars
    return f"why {words}" if words else None


def _run_init_repo_bootstrap(
    paths: PlatformPaths,
    repo: Path,
    *,
    limit: int | None = None,
    model_name: str,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
    query_prefix: str | None = None,
    passage_prefix: str | None = None,
) -> dict[str, Any]:
    """`init --repo`: from a cloned repository to an active index in one command.

    The measured first-run path was about ninety seconds of machine work and four commands of
    reading documentation first -- the machine was never the bottleneck, the ceremony was. This
    composes the exact steps the front page spells out (default policy, `corpus`, `index`) with
    no retrieval behaviour of its own: every step below is the documented command's own function,
    so the one-shot cannot drift from the spelled-out path.

    The corpus lands in `data_dir/corpus` and the policy in `config_dir/policy.yaml` -- inside
    directories this tool already owns and `ensure_private_dirs` already protects, so the
    bootstrap invents no new location. `index` is skipped when the history yielded nothing:
    an index of zero documents can only return nothing, and building it would dress that up as
    a completed setup."""
    if not (repo / ".git").exists():
        raise CliError("not_a_git_repository")
    ensure_private_dirs(paths)
    policy_path = paths.config_dir / "policy.yaml"
    if not policy_path.exists():
        policy_path.write_text(_DEFAULT_BOOTSTRAP_POLICY, encoding="utf-8", newline="\n")
    corpus_root = paths.data_dir / "corpus"
    corpus_result = gitcorpus.build(repo, corpus_root, limit)
    if corpus_result.written:
        if is_symmetric_model(model_name):
            print(
                f"Note: {model_name!r} is a symmetric-similarity model trained for paraphrase "
                "detection. For query-document retrieval (short question → long commit), "
                "asymmetric models like 'intfloat/multilingual-e5-large' with "
                "--query-prefix 'query: ' --passage-prefix 'passage: ' give higher recall@3. "
                "Measured gap: leakcanary recall@3 0.340 (MiniLM) vs 0.431+ (E5 + reranker). "
                "Rebuilding with a different model requires a full re-index.",
                file=sys.stderr,
            )
        index_result = run_index(
            paths,
            corpus_root.resolve(),
            policy_path.resolve(),
            model_name=model_name,
            embedder_factory=embedder_factory,
            query_prefix=query_prefix,
            passage_prefix=passage_prefix,
        )
    else:
        index_result = None
    return {
        "policy": policy_path,
        "corpus_root": corpus_root,
        "corpus": corpus_result,
        "index": index_result,
        "question": _suggested_question(corpus_root),
    }


def _build_launch_manifest(paths: PlatformPaths) -> clients.LaunchManifest:
    """The one manifest every rendered client config derives from (Slice 12B-2): invokes this
    module directly via `sys.executable` (absolute, always importable) rather than a `which
    bruriah` lookup -- no packaged console-script entry point exists until Slice 8B."""
    return clients.LaunchManifest(
        command=sys.executable,
        args=(
            "-m",
            "bruriah.cli",
            "serve",
            "--config-dir",
            str(paths.config_dir),
            "--data-dir",
            str(paths.data_dir),
            "--cache-dir",
            str(paths.cache_dir),
            "--log-dir",
            str(paths.log_dir),
        ),
        server_name="bruriah",
    )


def run_client_configs(
    paths: PlatformPaths,
    manifest: clients.LaunchManifest,
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
    paths: PlatformPaths,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
    reranker_model: str | None = None,
    reranker_factory: RerankerFactory = _default_reranker_factory,
    repo: Path | None = None,
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
    the vector leg off entirely.

    `reranker_model` is `None` unless the operator named one. It is NOT read from the build
    descriptor the way the embedder is, and the difference is not an oversight: passage vectors
    are baked into the snapshot, so the query embedder must match what built them or the vector
    leg searches the wrong space. A reranker reads text and writes nothing, so no snapshot can be
    wrong about it and any model can be swapped in or dropped without re-indexing."""
    try:
        descriptor = load_build_descriptor(paths)
        embed, fingerprint, dimensions = embedder_factory(descriptor.embedding_model)
        if fingerprint != descriptor.embedding_fingerprint or dimensions != descriptor.embedding_dimensions:
            raise CliError("embedding_model_mismatch")

        def embed_query(text: str) -> bytes:
            return embed([f"{descriptor.query_prefix}{text}"])[0]

        rerank = reranker_factory(reranker_model) if reranker_model else None
        return load_deps(paths, embed_query=embed_query, rerank=rerank, repo=repo)
    except PlatformError as error:
        raise CliError(error.code) from error


async def _serve_stdio(deps: ServiceDeps) -> None:
    server = build_server(deps)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _cmd_index_prune(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    try:
        removed = prune_generations(paths.data_dir / "active.json")
    except (IndexLifecycleError, OSError) as error:
        raise CliError(f"index_prune_failed:{getattr(error, 'code', type(error).__name__)}") from error
    print(json.dumps({"removed": sorted(path.name for path in removed)}, indent=2, sort_keys=True))
    print(f"Removed {len(removed)} unreferenced generation(s).", file=sys.stderr)
    return 0


def _cmd_init(
    args: argparse.Namespace,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    paths = _resolve_paths(args)
    repo_root = args.repo.resolve() if args.repo is not None else None
    if repo_root is not None and args.data_dir is None and args.config_dir is None:
        if getattr(args, "local", False):
            bruriah_dir = repo_root / ".bruriah"
            paths = PlatformPaths(
                config_dir=bruriah_dir / "config",
                data_dir=bruriah_dir / "data",
                cache_dir=paths.cache_dir,
                log_dir=paths.log_dir,
                network_enabled=paths.network_enabled,
                skill_ceiling=paths.skill_ceiling,
            )
        else:
            scoped = project_scoped_paths(repo_root)
            paths = PlatformPaths(
                config_dir=scoped.config_dir,
                data_dir=scoped.data_dir,
                cache_dir=scoped.cache_dir,
                log_dir=scoped.log_dir,
                network_enabled=paths.network_enabled,
                skill_ceiling=paths.skill_ceiling,
            )

    bootstrap: dict[str, Any] | None = None
    if args.repo is not None:
        assert repo_root is not None
        print(f"Reading the history of {args.repo}...", file=sys.stderr)
        bootstrap = _run_init_repo_bootstrap(
            paths,
            args.repo,
            limit=args.limit,
            model_name=args.model,
            embedder_factory=embedder_factory,
            query_prefix=getattr(args, "query_prefix", None),
            passage_prefix=getattr(args, "passage_prefix", None),
        )
        corpus_result = bootstrap["corpus"]
        print(
            f"Corpus: {corpus_result.written} decision document(s) from "
            f"{corpus_result.examined} non-merge commits -> {bootstrap['corpus_root']}",
            file=sys.stderr,
        )
        _report_corpus_coverage(corpus_result)
        if bootstrap["index"] is None:
            # The coverage explanation above already said why; the typed code makes the stop
            # scriptable. Config and client snippets are NOT written on this path: they would
            # point a client at an index that does not exist.
            raise CliError("corpus_has_no_reasoning")
        print(_index_summary_line(bootstrap["index"]), file=sys.stderr)

        # Write in-repo .bruriah/config.json pointer so subsequent commands auto-discover this project
        if args.config_dir is None:
            dot_bruriah = repo_root / ".bruriah"
            dot_bruriah.mkdir(parents=True, exist_ok=True)
            config_payload: dict[str, object] = {
                "data_dir": "data" if getattr(args, "local", False) else str(paths.data_dir),
                "config_dir": "config" if getattr(args, "local", False) else str(paths.config_dir),
            }
            (dot_bruriah / "config.json").write_text(
                json.dumps(config_payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
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
    if bootstrap is not None and bootstrap["question"]:
        # Same quoting discipline as `_cmd_ask`'s bridge line, for the same reason: a suggestion
        # that does not survive a paste is the design going undiscoverable one step from the end.
        # All four directories, not the two the bridge line carries: this run may have just
        # downloaded the model into a custom --cache-dir, and a paste that drops that flag
        # re-downloads it into the default one -- the exact re-download this command exists to end.
        directories = " ".join(
            f'--{name.replace("_", "-")} "{getattr(args, name)}"'
            for name in ("data_dir", "config_dir", "cache_dir", "log_dir")
            if getattr(args, name, None) is not None
        )
        command = f'bruriah ask "{bootstrap["question"]}"' + (f" {directories}" if directories else "")
        print(
            "\nThe index is active. Ask it something it is known to answer -- this question is "
            f"the newest decision\nin your own history:\n\n  {command}\n",
            file=sys.stderr,
        )
    return 0


def _report_corpus_coverage(result: gitcorpus.CorpusResult) -> None:
    """Shared by `corpus` and `init --repo`: the same history deserves the same honesty."""
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


def _report_pdf_coverage(result: pdfcorpus.PdfCorpusResult) -> None:
    """Honest coverage reporting for derived PDF corpora."""
    if result.written == 0:
        print(
            "No PDF page contained extractable text. The examined documents may be scanned images "
            "lacking a text layer, or empty -- retrieval quality cannot compensate for that.",
            file=sys.stderr,
        )
    elif result.skipped_empty_pages > 0:
        print(
            f"{result.written} of {result.examined} pages across {result.files_examined} PDF file(s) "
            f"contained extractable text; the other {result.skipped_empty_pages} page(s) were empty "
            "or image-only and were skipped.",
            file=sys.stderr,
        )


def _cmd_corpus(args: argparse.Namespace) -> int:
    """Step one of the documented workflow: derive a Markdown corpus from git history or PDFs."""
    if args.pdf is not None and args.repo is not None:
        raise CliError("cannot_specify_both_repo_and_pdf")

    if args.pdf is not None:
        result_pdf = pdfcorpus.build(args.pdf, args.out)
        print(
            json.dumps(
                {
                    "documents": result_pdf.documents,
                    "empty_pages_skipped": result_pdf.skipped_empty_pages,
                    "files_examined": result_pdf.files_examined,
                    "out": str(args.out),
                    "pages_examined": result_pdf.pages_examined,
                },
                indent=2,
                sort_keys=True,
            )
        )
        _report_pdf_coverage(result_pdf)
        return 0

    repo = args.repo or Path(".")
    if not (repo / ".git").exists():
        raise CliError("not_a_git_repository")
    result = gitcorpus.build(repo, args.out, args.limit, revision=args.revision)
    payload: dict[str, Any] = {
        "documents": result.written,
        "commits_examined": result.examined,
        "out": str(args.out),
        "revision": args.revision,
    }
    # `--github` is opt-in and off by default: with it absent (the common case, and every build
    # before this flag existed), nothing below this line ever runs -- no import side effect beyond
    # the module import above, no GitHub call, no extra file written. The git corpus above is
    # exactly what `gitcorpus.build` alone would have produced.
    if getattr(args, "github", None) is not None:
        github_payload = _cmd_corpus_github(args, repo)
        payload["github"] = github_payload
    print(json.dumps(payload, indent=2, sort_keys=True))
    _report_corpus_coverage(result)
    return 0


def _cmd_corpus_github(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    """The `--github` half of `bruriah corpus`: resolve OWNER/REPO, walk the same commits the git
    corpus above just walked, and ingest the issues/PRs they close. Split out of `_cmd_corpus`
    only to keep that function's normal (github-less) path short and easy to read top to bottom."""
    if args.github == "auto":
        try:
            repo_slug = github_corpus.detect_repo_slug(repo)
        except github_corpus.GitHubCorpusError as error:
            raise CliError(error.code) from error
    else:
        repo_slug = args.github
    if "/" not in repo_slug:
        raise CliError("invalid_repo_slug")

    paths = _resolve_paths(args)
    cache_dir = args.github_cache or (paths.cache_dir / "github")
    token = os.environ.get(args.github_token_env)
    if not token:
        print(
            f"warning: {args.github_token_env} is not set; unauthenticated GitHub requests are limited to 60/hour.",
            file=sys.stderr,
        )

    # Network is opt-in, same switch as everywhere else in this tool (`paths.network_enabled`,
    # `--network-enabled`, default False): with it off, `--github` still builds from a warm
    # `--github-cache`, and any uncached issue is skipped-and-warned rather than reaching out. That
    # design is right but was undiscoverable (T3b): say so up front, naming the flag that changes
    # it, before spending any time walking commits or hitting the cache.
    if not paths.network_enabled:
        print(
            f"Network is disabled (--network-enabled is off); `--github` will only use the warm "
            f"cache at {cache_dir}. Pass --network-enabled to let it fetch uncached issues from "
            "GitHub.",
            file=sys.stderr,
        )

    commits = gitcorpus.walk_commits(repo, args.limit, revision=args.revision)

    github_result = _build_github_documents_reporting(
        commits,
        args.out,
        repo=repo_slug,
        cache=_GitHubResponseCache(cache_dir),
        revision=args.revision,
        token=token,
        network_enabled=paths.network_enabled,
    )
    return {
        "repo": repo_slug,
        "documents": github_result.documents_written,
        "issues_fetched": github_result.issues_fetched,
        "issues_skipped": github_result.issues_skipped,
        "cross_repo_skipped": github_result.cross_repo_skipped,
        "cache_dir": str(cache_dir),
        "manifest": str(github_result.manifest_path),
    }


# One line per issue that misses the offline cache: exactly how `github_corpus.build_documents`
# formats it (`f"warning: skipping issue #{number} ({repo}): {error}"`, and `GitHubOfflineError`
# stringifies as `"github_offline_cache_miss: <path>"`). Matched, never re-derived, so a wording
# change in either place fails loudly here instead of silently stopping the collapse below.
_OFFLINE_CACHE_MISS_MARKER = ": github_offline_cache_miss:"


def _build_github_documents_reporting(
    commits: Sequence[gitcorpus.WalkedCommit],
    out: Path,
    *,
    repo: str,
    cache: _GitHubResponseCache,
    revision: str,
    token: str | None,
    network_enabled: bool,
) -> github_corpus.GitHubCorpusResult:
    """Call `github_corpus.build_documents` exactly as before -- its signature, behavior, and every
    warning it decides to print are untouched (T3's native review already approved them; T3b must
    not reopen that). This only post-processes the stderr TEXT that call already writes, at the CLI
    boundary: with the network switch off, hundreds of per-issue `github_offline_cache_miss` lines
    (T3b's finding) collapse into one count line, while every other warning -- rate limit, other
    `GitHubError`s -- passes through unchanged. With the switch on, nothing is captured at all."""
    if network_enabled:
        return github_corpus.build_documents(
            commits,
            out,
            repo=repo,
            cache=cache,
            revision=revision,
            token=token,
            network_enabled=network_enabled,
        )

    captured = io.StringIO()
    with contextlib.redirect_stderr(captured):
        result = github_corpus.build_documents(
            commits,
            out,
            repo=repo,
            cache=cache,
            revision=revision,
            token=token,
            network_enabled=network_enabled,
        )

    offline_misses = 0
    for line in captured.getvalue().splitlines():
        if _OFFLINE_CACHE_MISS_MARKER in line:
            offline_misses += 1
            continue
        print(line, file=sys.stderr)
    if offline_misses:
        print(
            f"warning: {offline_misses} issue(s) skipped ({repo}) due to a GitHub offline cache "
            "miss; pass --network-enabled to fetch them.",
            file=sys.stderr,
        )
    return result


def _cmd_index(
    args: argparse.Namespace,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
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
    if is_symmetric_model(args.model):
        print(
            f"Note: {args.model!r} is a symmetric-similarity model trained for paraphrase "
            "detection. For query-document retrieval (short question → long commit), "
            "asymmetric models like 'intfloat/multilingual-e5-large' with "
            "--query-prefix 'query: ' --passage-prefix 'passage: ' give higher recall@3. "
            "Measured gap: leakcanary recall@3 0.340 (MiniLM) vs 0.431+ (E5 + reranker). "
            "Rebuilding with a different model requires a full re-index.",
            file=sys.stderr,
        )
    try:
        result = run_index(
            paths,
            corpus_root,
            policy,
            model_name=args.model,
            embedder_factory=embedder_factory,
            query_prefix=getattr(args, "query_prefix", None),
            passage_prefix=getattr(args, "passage_prefix", None),
        )
    except (CorpusPolicyError, IndexLifecycleError, FileExistsError, ValueError, OSError, yaml.YAMLError) as error:
        # yaml.YAMLError (a malformed --policy that exists) is neither ValueError nor OSError.
        raise CliError(f"index_failed:{getattr(error, 'code', type(error).__name__)}") from error
    summary = {
        "build_id": result.build_id,
        "documents": result.documents,
        "passages": result.passages,
        # Reuse is the difference between a reindex that costs seconds and one that costs the whole
        # corpus again, and it is invisible from the outside -- the resulting snapshot is identical
        # either way. Reporting it is how the user learns that the second `index` was cheap, and how
        # they find out when it was not: a full re-embed after a model or parser change shows up
        # here as zero, on the run that took the time.
        "reused_documents": result.reused_documents,
    }
    print(json.dumps(summary, sort_keys=True))
    print(_index_summary_line(result), file=sys.stderr)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    raw_repo = getattr(args, "repo", None)
    repo = raw_repo.resolve() if raw_repo is not None else Path.cwd()
    deps = build_serve_deps(paths, repo=repo, reranker_model=getattr(args, "reranker", None))
    try:
        anyio.run(_serve_stdio, deps)
    except OSError as error:
        raise CliError("serve_io_error") from error
    finally:
        deps.snapshot.database.close()
    return 0


# T2 (investigate-boundary-v2): `EvidenceRecord.locator`/`citation_locator`/conflicts/claim text
# now carry the opaque `doc:v1:<sha256>` document_ref, never the corpus-relative file path (see
# `retrieval.build_local_evidence_record`). The human `ask` view is the one place that opacity is
# meant to be undone: a person reading a terminal wants the real path, and can already get it --
# `read_evidence`'s own `ReadItem.locator` keeps the real path unconditionally. This resolves the
# SAME thing locally, for every doc ref embedded anywhere in the human-readable text, so a person
# never has to run `--read` just to learn which file a conflict or claim is about. `--json` is
# untouched: it stays exactly the prose-free payload the MCP surface returns.
_DOC_REF_PATTERN = re.compile(r"doc:v1:[0-9a-f]{64}")


def _resolve_doc_refs_for_humans(text: str, repo: SnapshotRepository) -> str:
    def _resolve_doc(match: "re.Match[str]") -> str:
        path = repo.get_document_path(match.group(0))
        return path if path is not None else match.group(0)

    text = _DOC_REF_PATTERN.sub(_resolve_doc, text)
    # T3/T4 (investigate-boundary-v2): the counterfactual contract's opaque `alt:`/`premise:`
    # refs -- resolved back to a name/id for the human view exactly like `doc:v1:` resolves back
    # to a path, via the one shared resolver both this function and `demo.py` use (T4 closes
    # R2-ref-resolver-duplicated). `--json` and the MCP surface never call this function, so they
    # stay ref-only regardless.
    return resolve_counterfactual_refs_for_humans(text, repo)


def _cmd_ask(
    args: argparse.Namespace,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
    reranker_factory: RerankerFactory = _default_reranker_factory,
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
    deps = build_serve_deps(
        paths,
        embedder_factory=embedder_factory,
        reranker_model=getattr(args, "reranker", None),
        reranker_factory=reranker_factory,
        repo=getattr(args, "repo", Path(".")),
    )
    # The snapshot holds an open SQLite connection. `_cmd_serve` closes it in a finally and
    # `run_doctor` closes it before returning; this one never did, across five exits. On POSIX
    # that is a ResourceWarning nobody reads, and on Windows an unclosed connection is what
    # makes a generation undeletable -- the same shape that failed `index-prune` in CI.
    try:
        code_target = getattr(args, "code_target", None)
        result = investigate(
            InvestigationRequest(task=args.question, code_target=code_target, host_skills=[]),
            deps,
        )
        payload = result.model_dump(mode="json")
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        # Human view only, never `--json`: resolve every embedded document_ref back to the real
        # corpus-relative path, the same way `read_evidence` would on explicit request.
        repo = SnapshotRepository(deps.snapshot.database)
        local = [item for item in payload["evidence"] if item["kind"] == "local"]
        print(f"\n  status: {payload['status']} · {len(local)} references")
        for note in payload["degradation"]:
            print(f"  disclosed: {note}")
        for gap in payload["gaps"]:
            print(f"  gap: {gap}")
        for conflict in payload.get("conflicts", []):
            print(f"  conflict: {_resolve_doc_refs_for_humans(conflict, repo)}")
        for claim in payload.get("claims", []):
            if claim.get("state") == "conflicted":
                print(f"  claim (conflicted): {_resolve_doc_refs_for_humans(claim['text'], repo)}")
        if payload["status"] == "abstained":
            print(
                "\n  No approved policy covers this domain, so nothing is returned rather than the\n"
                "  nearest-looking passage. That is the designed answer, not a failure.\n"
            )
            return 0

        # When a reference was named, list only it: relisting eight results above the text you
        # asked for buries the thing you asked for.
        shown = [(n, item) for n, item in enumerate(local, start=1) if not args.read or n in args.read][
            : args.limit if not args.read else None
        ]
        for position, item in shown:
            print(f"\n  [{position}] {_resolve_doc_refs_for_humans(item['citation_locator'], repo)}")
            print(f"      authority: {item['authority']} ({item['authority_rationale']})")
            if item.get("freshness") and item["freshness"] != "unknown":
                print(f"      freshness: {item['freshness']}")
            if item.get("conflict") and item["conflict"] != "none":
                print(f"      conflict: {item['conflict']}")
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
        print(
            "\n  Nothing above is the document's text -- only references to it. That is the whole\n"
            f"  design: read one explicitly with\n\n    {command}\n"
        )
        return 0
    finally:
        deps.snapshot.database.close()


def _cmd_why(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        raise CliError("not_a_git_repository")

    try:
        res = run_why(paths, repo, args.target)
    except PlatformError as error:
        raise CliError(error.code) from error
    except WhyError as error:
        raise CliError(error.code) from error

    if args.json:
        print(format_why_json(res))
    else:
        print(format_why_human(res))
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        raise CliError("not_a_git_repository")

    try:
        report = run_drift(paths, repo, args.revision_or_range, args.staged)
    except PlatformError as error:
        raise CliError(error.code) from error
    except DriftError as error:
        raise CliError(error.code) from error

    if args.json:
        print(format_drift_json(report))
    else:
        print(format_drift_human(report))

    if args.strict and report.has_drift:
        return 1
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """Analyze PR changes and post architectural review comments.

    Combines drift detection with optional line-level causal archaeology to
    produce a structured review suitable for GitHub PRs.  When ``--post`` is
    given, posts the review directly to the PR via the GitHub API; otherwise
    prints it to stdout for local inspection or piping into other tools.
    """
    paths = _resolve_paths(args)
    repo = args.repo.resolve()
    if not (repo / ".git").exists():
        raise CliError("not_a_git_repository")

    # Auto-detect revision from PR context when not explicitly provided.
    revision = args.revision_or_range
    pr_context = detect_pr_context()
    if revision is None and pr_context:
        revision = f"origin/{pr_context.base_ref}...HEAD"

    if revision is None:
        raise CliError("no_revision_specified")

    # Run drift analysis (reuses the same engine as `bruriah drift`).
    try:
        report = run_drift(paths, repo, revision)
    except PlatformError as error:
        raise CliError(error.code) from error
    except DriftError as error:
        raise CliError(error.code) from error

    # Optionally enrich with line-level causal archaeology.
    changed_lines = None
    database = None
    skip_line_comments = getattr(args, "no_line_comments", False)

    if not skip_line_comments:
        changed_lines = get_changed_lines(repo, revision)
        try:
            from .platform import open_snapshot

            snapshot = open_snapshot(paths)
            database = snapshot.database
        except PlatformError:
            database = None

    try:
        review = build_review(
            report,
            repo,
            database,
            changed_lines,
            strict=args.strict,
            line_comments=not skip_line_comments,
        )
    finally:
        if database is not None:
            database.close()

    # Output: post to GitHub, print as JSON, or print human-readable.
    if args.post:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            raise CliError("github_token_missing")
        if pr_context is None:
            raise CliError("not_in_pr_context")

        try:
            url = gh_post_review(token, pr_context, review)
        except GitHubError as error:
            raise CliError(error.code) from error

        print(f"Review posted: {url}", file=sys.stderr)
    elif args.json:
        print(format_review_json(review))
    else:
        print(format_review_human(review))

    if args.strict and review.has_drift:
        return 1
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    try:
        run_ui(
            paths,
            port=getattr(args, "port", 0),
            open_browser=not getattr(args, "no_browser", False),
        )
    except UIError as error:
        raise CliError(error.code) from error
    return 0


def _cmd_lens(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")
    target_path = Path(args.file)
    rel_path = str(target_path.relative_to(repo)) if target_path.is_absolute() else str(target_path)
    try:
        result = run_lens(paths, repo, rel_path)
    except LensError as error:
        raise CliError(error.code) from error

    if args.json:
        print(format_lens_json(result))
    else:
        print(format_lens_human(result))
    return 0


def _cmd_bootstrap(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")
    out_dir = args.out_dir if args.out_dir.is_absolute() else repo / args.out_dir

    try:
        result = run_bootstrap(
            repo,
            out_dir,
            limit=args.limit,
            min_score=args.min_score,
            dry_run=args.dry_run,
            index_after=args.index,
            paths=paths,
        )
    except BootstrapError as error:
        raise CliError(error.code) from error

    prefix = "[dry-run] " if result.dry_run else ""
    print(f"🏛️  {prefix}Bruriah Bootstrap — Mined {result.total_commits_scanned} commits")
    print(f"   Found {result.candidates_found} candidate architectural decision(s).")
    if not result.dry_run:
        print(f"   Written {result.decisions_written} decision document(s) to: {result.out_dir}")
        if args.index:
            print("   ✅ Built and promoted initial index snapshot successfully.")
    else:
        print(f"   Destination: {result.out_dir} (files not written)")

    print()
    for d in result.decisions[:10]:
        supersedes_note = f" (supersedes {', '.join(d.inferred_supersedes)})" if d.inferred_supersedes else ""
        print(f"   • [{d.score:.2f}] {d.date} {d.sha[:8]} — {d.subject}{supersedes_note}")
        reasons_str = ", ".join(d.reasons[:3])
        print(f"     ↳ {reasons_str}")

    if len(result.decisions) > 10:
        print(f"   ... and {len(result.decisions) - 10} more.")

    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")

    try:
        result = run_impact(paths, repo, args.target)
    except ImpactError as error:
        raise CliError(error.code) from error

    if args.json:
        print(format_impact_json(result))
    else:
        print(format_impact_human(result))
    return 0


def _warn_if_agent_rendering_degraded(command: str, degraded: bool) -> None:
    """Tell the operator, once, that the `--agent` rendering they just printed was degraded.

    Here rather than in `agent_surface`, because this is the only layer that knows which output
    format was selected. The renderers run on every `guard`, `brief` and `heal` evaluation
    whatever the operator asked for, so warning from inside them put a line about `--agent` on
    the stderr of plain runs and `--json` runs that never passed it -- unexpected stderr on a
    successful exit, which a CI wrapper reads as noise at best and as failure at worst, carrying
    advice the operator had already taken.
    """
    if degraded:
        print(agent_surface.degradation_warning(command), file=sys.stderr)


def _cmd_guard(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")

    try:
        result = run_guard(
            paths,
            repo,
            args.target,
            strict=args.strict,
            receipt=args.receipt,
            engram=getattr(args, "engram", False),
        )
    except GuardError as error:
        raise CliError(error.code) from error

    if args.agent:
        print(result.agent_context)
        _warn_if_agent_rendering_degraded("guard", result.agent_rendering_degraded)
    elif args.json:
        print(format_guard_json(result))
    else:
        print(format_guard_human(result))

    if result.status == "VETOED" or (args.strict and result.status == "WARNING"):
        return 1
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")

    try:
        result = run_brief(
            paths,
            repo,
            intent=args.intent or "",
            targets=args.targets,
        )
    except BriefError as error:
        raise CliError(error.code) from error

    if args.agent:
        print(result.agent_context)
        _warn_if_agent_rendering_degraded("brief", result.agent_rendering_degraded)
    elif args.json:
        print(format_brief_json(result))
    else:
        print(format_brief_human(result))
    return 0


def _cmd_decide(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")

    title = args.title
    problem = args.problem
    solution = args.solution

    # If required fields are missing, check if interactive prompt is possible
    if not title or not problem or not solution:
        if sys.stdin.isatty():
            print("🏛️  Bruriah Architectural Decision Scribe\n")
            if not title:
                title = input("Decision title (e.g. feat(auth): adopt OAuth2): ").strip()
            if not problem:
                problem = input("Problem & Context: ").strip()
            if not solution:
                solution = input("Decision & Solution: ").strip()
        else:
            raise CliError(
                "missing_required_fields: --title, --problem, and --solution are required in non-interactive mode."
            )

    # Parse alternatives from Name:Tradeoff:Reason
    alternatives: list[Alternative] = []
    if args.alternative:
        for alt_str in args.alternative:
            parts = alt_str.split(":", 2)
            if len(parts) == 3:
                alternatives.append(Alternative(parts[0], parts[1], parts[2]))
            elif len(parts) == 2:
                alternatives.append(Alternative(parts[0], parts[1], "Suboptimal tradeoff"))
            else:
                alternatives.append(Alternative(parts[0], "Evaluated option", "Rejected"))

    try:
        record, new_sha = run_decide(
            paths=paths,
            repo=repo,
            title=title,
            problem=problem,
            solution=solution,
            invariants=args.invariants or [],
            alternatives=alternatives,
            supersedes=args.supersedes or [],
            amends=args.amends or [],
            deprecates=args.deprecates or [],
            commit=args.commit,
        )
    except DecideError as error:
        raise CliError(error.code) from error

    if args.json:
        print(record.to_json())
    elif args.adr:
        print(record.format_adr_markdown())
    elif args.commit:
        print(f"✅ Architectural commit created: {new_sha[:8] if new_sha else 'OK'}")
        print(record.format_commit_message())
    else:
        print(record.format_commit_message())

    return 0


def _cmd_heal(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    repo = find_project_root(args.repo.resolve())
    if repo is None:
        raise CliError("not_a_git_repository")

    try:
        result = run_heal(
            paths=paths,
            repo=repo,
            target=args.target,
        )
    except HealError as error:
        raise CliError(error.code) from error

    if args.agent:
        print(format_heal_agent(result))
        _warn_if_agent_rendering_degraded("heal", result.agent_rendering_degraded)
    elif args.json:
        print(format_heal_json(result))
    else:
        print(format_heal_human(result))

    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    paths = _resolve_paths(args)
    manifest = _build_launch_manifest(paths)
    repo_root = find_project_root(args.repo.resolve())

    scope = "auto"
    if getattr(args, "global_scope", False):
        scope = "global"
    elif getattr(args, "project", False):
        scope = "project"

    client_arg = getattr(args, "client", None)
    if client_arg is None or client_arg == "all":
        clients_to_configure = detect_installed_clients(repo_root)
        if not clients_to_configure:
            clients_to_configure = ["cursor", "claude"]
    else:
        clients_to_configure = [client_arg]

    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("[dry-run] MCP Client Configuration Preview:", file=sys.stderr)

    for client in clients_to_configure:
        try:
            res = setup_client(
                client,
                manifest,
                project_root=repo_root,
                scope=scope,
                dry_run=dry_run,
            )
            prefix = f"[{client}]"
            action_label = {
                "created": "Created and registered" if not dry_run else "Would create",
                "updated": "Updated" if not dry_run else "Would update",
                "unchanged": "Up to date",
            }.get(res.status, res.status)
            print(f"{prefix} {action_label}: {res.target_path}", file=sys.stderr)
            if dry_run:
                print(res.content)
        except SetupError as error:
            raise CliError(f"setup_failed:{error.code}") from error

    return 0


def _cmd_hook(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    action = getattr(args, "hook_action", None)
    try:
        if action == "install":
            target, status = install_hook(repo, force=getattr(args, "force", False))
            label = {
                "created": "Installed pre-commit hook to",
                "updated": "Updated pre-commit hook in",
                "unchanged": "Pre-commit hook is already up to date in",
            }.get(status, status)
            print(f"{label} {target}", file=sys.stderr)
        elif action == "uninstall":
            target, status = uninstall_hook(repo)
            label = {
                "removed": "Removed pre-commit hook from",
                "not_installed": "No Bruriah hook was installed in",
                "not_found": "No pre-commit hook found at",
            }.get(status, status)
            print(f"{label} {target}", file=sys.stderr)
        else:
            raise CliError("unknown_hook_action")
    except HookError as error:
        raise CliError(f"hook_failed:{error.code}") from error
    return 0


def _cmd_alias(args: argparse.Namespace) -> int:
    scope = "local" if getattr(args, "local", False) else "global"
    repo = args.repo.resolve() if getattr(args, "repo", None) else None
    action = getattr(args, "alias_action", None)
    try:
        if action == "install":
            install_results = install_aliases(scope=scope, repo=repo)
            for name, cmd, status in install_results:
                label = {
                    "created": "Configured",
                    "updated": "Updated",
                    "unchanged": "Already configured",
                }.get(status, status)
                print(f"git {name} -> {cmd} ({label}, {scope})", file=sys.stderr)
        elif action == "uninstall":
            uninstall_results = uninstall_aliases(scope=scope, repo=repo)
            for name, status in uninstall_results:
                label = {
                    "removed": "Removed",
                    "not_found": "Not configured",
                }.get(status, status)
                print(f"git {name} ({label}, {scope})", file=sys.stderr)
        else:
            raise CliError("unknown_alias_action")
    except AliasError as error:
        raise CliError(f"alias_failed:{error.code}") from error
    return 0


def _cmd_watch(
    args: argparse.Namespace,
    *,
    embedder_factory: EmbedderFactory = _default_embedder_factory,
) -> int:
    from . import watch

    paths = _resolve_paths(args)
    repo = args.repo.resolve() if args.repo is not None else Path(".").resolve()
    try:
        watcher = watch.RepoWatcher(
            repo,
            paths,
            model_name=args.model,
            embedder_factory=embedder_factory,
            query_prefix=getattr(args, "query_prefix", None),
            passage_prefix=getattr(args, "passage_prefix", None),
            interval=args.interval,
        )
        return watcher.watch(once=args.once)
    except watch.WatchError as error:
        raise CliError(error.code) from error


def _cmd_demo(args: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo(
        interactive=not getattr(args, "non_interactive", False),
        use_color=not getattr(args, "no_color", False),
    )


def _build_cli_parser() -> argparse.ArgumentParser:
    return build_cli_parser(
        version=__version__,
        handlers={
            "demo": _cmd_demo,
            "init": _cmd_init,
            "setup": _cmd_setup,
            "hook": _cmd_hook,
            "alias": _cmd_alias,
            "corpus": _cmd_corpus,
            "ask": _cmd_ask,
            "why": _cmd_why,
            "drift": _cmd_drift,
            "review": _cmd_review,
            "ui": _cmd_ui,
            "lens": _cmd_lens,
            "bootstrap": _cmd_bootstrap,
            "impact": _cmd_impact,
            "guard": _cmd_guard,
            "brief": _cmd_brief,
            "decide": _cmd_decide,
            "heal": _cmd_heal,
            "index": _cmd_index,
            "index-prune": _cmd_index_prune,
            "serve": _cmd_serve,
            "doctor": _cmd_doctor,
            "watch": _cmd_watch,
            "skill-ingest": _cmd_skill_ingest,
            "skill-analyze": _cmd_skill_analyze,
            "skill-approve": _cmd_skill_approve,
            "skill-sign": _cmd_skill_sign,
            "skill-activate": _cmd_skill_activate,
            "skill-rollback": _cmd_skill_rollback,
            "skill-status": _cmd_skill_status,
            "skill-prune": _cmd_skill_prune,
        },
    )


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
