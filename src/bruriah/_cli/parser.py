from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from pathlib import Path

CommandHandler = Callable[[argparse.Namespace], int]


def add_platform_arguments(parser: argparse.ArgumentParser) -> None:
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


def build_cli_parser(
    version: str,
    handlers: Mapping[str, CommandHandler] | None = None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bruriah")
    # `docs/client-guidance.md` told readers to run this to find out which router version a config
    # targets, and through 0.3.0 there was nothing to run: the flag it named did not exist. A tool
    # whose subject is provenance has to be able to state its own version when asked.
    #
    # `action="version"` prints and exits while the flag is being parsed, so it answers before
    # `required=True` on the subcommand can reject the call for naming no command. Pinned by
    # test_it_reports_its_own_version_without_a_subcommand.
    parser.add_argument("--version", action="version", version=f"bruriah {version}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        add_platform_arguments(sub)
        if handlers and name in handlers:
            sub.set_defaults(handler=handlers[name])
        return sub

    init_parser = add(
        "init",
        "Create private config and print client snippets; --repo bootstraps a repository end to end.",
    )
    init_parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="bootstrap this git repository in one command: default policy, corpus, "
        "index and client configs, ending with a first question the index can answer",
    )
    init_parser.add_argument("--limit", type=int, default=None, help="most recent N commits only")
    init_parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    init_parser.add_argument("--query-prefix", default=None, help="query prefix template for asymmetric embedding models")
    init_parser.add_argument("--passage-prefix", default=None, help="passage prefix template for asymmetric embedding models")
    init_parser.add_argument(
        "--local",
        action="store_true",
        help="store corpus and index locally inside .bruriah/ in the repository instead of user data directory",
    )
    setup_parser = add(
        "setup",
        "Non-destructively configure the bruriah MCP server into an editor or client (Cursor, Claude, Claude Desktop, Gemini, OpenCode).",
    )
    setup_parser.add_argument(
        "client",
        nargs="?",
        default=None,
        choices=["cursor", "claude", "claude-desktop", "gemini", "opencode", "all"],
        help="client to configure (default: auto-detect installed clients, or 'all')",
    )
    setup_parser.add_argument(
        "--project",
        action="store_true",
        help="force project-scoped configuration (e.g. .cursor/mcp.json or .mcp.json)",
    )
    setup_parser.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="force user-scoped configuration (e.g. ~/.cursor/mcp.json or Claude Desktop)",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the merged configuration and target path without writing to disk",
    )
    setup_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="git repository root for project-scoped configuration (default '.')",
    )
    corpus_parser = add("corpus", "Turn a git history's reasoning or PDF documents into a corpus.")
    corpus_parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="git repository to read (default '.' if --pdf not specified)",
    )
    corpus_parser.add_argument(
        "--pdf",
        type=Path,
        default=None,
        help="PDF file or directory of PDFs to derive corpus from",
    )
    corpus_parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    corpus_parser.add_argument(
        "--limit", type=int, default=None, help="most recent N commits only (git corpus)"
    )
    corpus_parser.add_argument(
        "--revision",
        default="HEAD",
        metavar="REV",
        help="derive the corpus as of this commit (default HEAD). Pin it to reproduce a "
        "published measurement: the history IS the corpus, so it grows under the number.",
    )
    ask = add("ask", "Run one investigation and show the evidence.")
    ask.add_argument("question", help="what you want to know about your own corpus")
    ask.add_argument(
        "--read",
        type=int,
        action="append",
        metavar="N",
        help="read reference N's exact text. Repeat to read several.",
    )
    ask.add_argument("--limit", type=int, default=8, help="references to list (default 8)")
    ask.add_argument("--json", action="store_true", help="the raw investigate_work result")
    ask.add_argument(
        "--code-target",
        "-c",
        default=None,
        metavar="TARGET",
        help="ground the investigation on a specific file and optional line (e.g. 'src/server.py:42')",
    )
    ask.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="git repository root for causal resolution (default '.')",
    )
    ask.add_argument(
        "--reranker",
        default=None,
        metavar="MODEL",
        help="rerank the top documents with a cross-encoder. Off by default; "
        "measured in evals/project-memory/README.md",
    )
    why_parser = add(
        "why",
        "Trace why a line or file was written back to the governing architectural decision.",
    )
    why_parser.add_argument(
        "target",
        help="target file and optional line number, e.g. 'src/core/storage.py:42'",
    )
    why_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="git repository to inspect (default '.')",
    )
    why_parser.add_argument(
        "--json",
        action="store_true",
        help="output structured causal resolution as JSON",
    )
    drift_parser = add(
        "drift",
        "Inspect Git changes against the decision lineage DAG to detect architectural regressions.",
    )
    drift_parser.add_argument(
        "revision_or_range",
        nargs="?",
        default=None,
        metavar="REVISION_OR_RANGE",
        help="git revision or range to inspect (e.g. 'HEAD~1', 'origin/main...HEAD'). "
        "Defaults to inspecting uncommitted working tree changes against HEAD.",
    )
    drift_parser.add_argument(
        "--staged",
        action="store_true",
        help="inspect staged git changes (ideal for pre-commit hooks)",
    )
    drift_parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with code 1 if architectural drift or stale governance is detected",
    )
    drift_parser.add_argument(
        "--json",
        action="store_true",
        help="output structured drift inspection report as JSON",
    )
    drift_parser.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="git repository to inspect (default '.')",
    )
    index_parser = add("index", "Build and promote a candidate index.")
    index_parser.add_argument("--corpus-root", type=Path, required=True)
    index_parser.add_argument("--policy", type=Path, required=True)
    index_parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    index_parser.add_argument("--query-prefix", default=None, help="query prefix template for asymmetric embedding models")
    index_parser.add_argument("--passage-prefix", default=None, help="passage prefix template for asymmetric embedding models")
    ingest = add("skill-ingest", "Store a candidate skill pack privately.")
    ingest.add_argument("--pack", type=Path, required=True)
    analyze = add("skill-analyze", "Report structural findings for review.")
    analyze.add_argument("--candidate", type=Path, required=True)
    approve = add("skill-approve", "Record approval, bound to the content digest.")
    approve.add_argument("--candidate", type=Path, required=True)
    approve.add_argument(
        "--acknowledge",
        action="append",
        metavar="SKILL_ID:CODE",
        help="Acknowledge one advisory. Repeat for each; all are required.",
    )
    sign = add("skill-sign", "Sign a pack with a release key held by path.")
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--signer", required=True)
    sign.add_argument("--pack", type=Path, required=True)
    sign.add_argument("--out", type=Path, default=None)
    activate = add("skill-activate", "Compile and activate named approved candidates.")
    activate.add_argument(
        "--candidate",
        type=Path,
        action="append",
        required=True,
        help="An approved candidate to activate. Repeat to activate several.",
    )
    activate.add_argument(
        "--allow-unsigned-local",
        action="store_true",
        help="Permit unsigned local (T3) packs. Local-tier skills only.",
    )
    add("skill-rollback", "Restore the previously retained skill set.")
    add("skill-status", "Show the active set and generations on disk.")
    add("index-prune", "Delete unreferenced index generations.")
    add("skill-prune", "Delete unreferenced skill-set generations.")
    serve = add("serve", "Run the two-tool MCP server over stdio.")
    serve.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        metavar="PATH",
        help="repository root for code causal archaeology (defaults to current working directory)",
    )
    serve.add_argument(
        "--reranker",
        default=None,
        metavar="MODEL",
        help="rerank the top documents with a cross-encoder. Off by default; "
        "measured in evals/project-memory/README.md",
    )
    add("doctor", "Read-only health check.")
    return parser
