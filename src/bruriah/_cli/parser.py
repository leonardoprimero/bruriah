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
    corpus_parser = add("corpus", "Turn a git history's reasoning into a corpus.")
    corpus_parser.add_argument("--repo", type=Path, default=Path("."), help="repository to read")
    corpus_parser.add_argument("--out", type=Path, required=True, help="directory to write into")
    corpus_parser.add_argument("--limit", type=int, default=None, help="most recent N commits only")
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
        "--reranker",
        default=None,
        metavar="MODEL",
        help="rerank the top documents with a cross-encoder. Off by default; "
        "measured in evals/project-memory/README.md",
    )
    index_parser = add("index", "Build and promote a candidate index.")
    index_parser.add_argument("--corpus-root", type=Path, required=True)
    index_parser.add_argument("--policy", type=Path, required=True)
    index_parser.add_argument(
        "--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
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
        "--reranker",
        default=None,
        metavar="MODEL",
        help="rerank the top documents with a cross-encoder. Off by default; "
        "measured in evals/project-memory/README.md",
    )
    add("doctor", "Read-only health check.")
    return parser
