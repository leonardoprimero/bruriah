from __future__ import annotations

import argparse
import os
from pathlib import Path

from ..platform import PlatformError, PlatformPaths, resolve_paths


class CliError(ValueError):
    """Typed failure for every `bruriah` command, mirroring `PlatformError`/`ServiceError`."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def resolve_cli_paths(args: argparse.Namespace, *, cwd: Path | None = None) -> PlatformPaths:
    effective_cwd = cwd or getattr(args, "repo", None) or Path.cwd()
    try:
        paths = resolve_paths(
            cli_config_dir=args.config_dir,
            cli_data_dir=args.data_dir,
            cli_cache_dir=args.cache_dir,
            cli_log_dir=args.log_dir,
            cli_network_enabled=args.network_enabled,
            cli_skill_ceiling=getattr(args, "skill_ceiling", None),
            cwd=effective_cwd,
        )
    except PlatformError as error:
        raise CliError(error.code) from error
    # fastembed's own default model cache is `tempfile.gettempdir()/fastembed_cache`, and macOS
    # purges the temp tree on its own schedule -- so "the model downloads once" held only until
    # the OS decided otherwise, and the re-download then happened silently on whatever network was
    # present. Pinned under this tool's private `cache_dir` instead, where its other caches
    # already live (`cache.py` only ever touches top-level `*.json` there, so a subdirectory is
    # outside its deletion control by construction). `setdefault`, not assignment:
    # FASTEMBED_CACHE_PATH is fastembed's documented operator knob, and an operator who set it
    # keeps it. Set here because every command funnels through this resolver before any factory
    # constructs a model, which lets both factories keep the one-argument shape every injected
    # test fake shares.
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(paths.cache_dir / "models"))
    return paths


# The default embedding model for every `bruriah` command that builds or queries an index
# (`init`, `index`, `watch`). Chosen by the 2026-09-20 embedder ablation
# (evals/project-memory/README.md, "The embedder was the bottleneck") as the only candidate that
# improved recall@3 on both external corpora AND this project's own bilingual history without
# regressing either language -- the bge candidates scored higher on English-only corpora but cost
# Spanish recall below the old default. Read this constant, never hardcode the model name: three
# argparse defaults and two module-level call sites all point here so they cannot drift apart.
DEFAULT_EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-base-es"

KNOWN_MODEL_PREFIXES: dict[str, tuple[str, str]] = {
    "intfloat/multilingual-e5-large": ("query: ", "passage: "),
    "intfloat/multilingual-e5-base": ("query: ", "passage: "),
    "intfloat/multilingual-e5-small": ("query: ", "passage: "),
    "intfloat/e5-large-v2": ("query: ", "passage: "),
    "intfloat/e5-base-v2": ("query: ", "passage: "),
    "intfloat/e5-small-v2": ("query: ", "passage: "),
    "BAAI/bge-base-en": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-small-en": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-large-en": ("Represent this sentence for searching relevant passages: ", ""),
    # The `-v1.5` names are the actual current bge release names (and what the 2026-09-20 ablation
    # indexed with); without these entries they fell through to the unprefixed default, so bge ran
    # without its recommended asymmetric instruction prefix unless one was passed explicitly.
    "BAAI/bge-small-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-base-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    "BAAI/bge-large-en-v1.5": ("Represent this sentence for searching relevant passages: ", ""),
    # BGE-M3 is language-agnostic and uses no prefix -- it handles asymmetric retrieval
    # internally via multi-functionality training. Listed explicitly so it is not mistakenly
    # given E5 prefixes by the `elif "e5" in model_name` fallback.
    "BAAI/bge-m3": ("", ""),
    # Nomic embed-text v1.5 uses task-type prefixes. search_query/search_document for retrieval.
    "nomic-ai/nomic-embed-text-v1.5": ("search_query: ", "search_document: "),
    "nomic-ai/nomic-embed-text-v1": ("search_query: ", "search_document: "),
    # Jina v3 uses no prefix but is included so it is not caught by the e5 heuristic.
    "jinaai/jina-embeddings-v3": ("", ""),
    # Jina v2 uses no prefix either. `-base-es` is DEFAULT_EMBEDDING_MODEL above; the sibling
    # English-only v2 models are listed for the same reason v3 is -- explicit, not an accident of
    # the e5 fallback.
    "jinaai/jina-embeddings-v2-base-es": ("", ""),
    "jinaai/jina-embeddings-v2-base-en": ("", ""),
    "jinaai/jina-embeddings-v2-small-en": ("", ""),
}

# Model name substrings that indicate a symmetric-similarity model -- trained for paraphrase
# detection or sentence similarity, not for asymmetric query-document retrieval.
# `bruriah index` emits a warning when the chosen model matches any of these patterns,
# because the recall gap between symmetric and asymmetric models on this task is large
# (measured on this project's own history at 209 documents, pinned as of v1.4.0 (`fff2a71`):
# DEFAULT_EMBEDDING_MODEL jina-embeddings-v2-base-es Spanish recall@3 0.750 vs the old default
# paraphrase-multilingual-MiniLM-L12-v2's 0.500 -- one of the patterns below; see
# "The embedder was the bottleneck, measured 2026-09-20" in evals/project-memory/README.md).
_SYMMETRIC_MODEL_PATTERNS: frozenset[str] = frozenset({
    "paraphrase-",
    "all-minilm",
    "all-mpnet",
    "distiluse",
    "msmarco-distilbert",  # fine-tuned on MS MARCO, but symmetric
})


def is_symmetric_model(model_name: str) -> bool:
    """Return True if the model name matches a known symmetric-similarity pattern.

    Symmetric models are trained for paraphrase detection or semantic similarity,
    not for asymmetric query-document retrieval. They tend to underperform on recall@3
    when a short query must match a long architectural decision document.
    """
    lower = model_name.lower()
    return any(pattern in lower for pattern in _SYMMETRIC_MODEL_PATTERNS)


def resolve_model_prefixes(
    model_name: str,
    query_prefix: str | None = None,
    passage_prefix: str | None = None,
) -> tuple[str, str]:
    default_q, default_p = ("", "")
    if model_name in KNOWN_MODEL_PREFIXES:
        default_q, default_p = KNOWN_MODEL_PREFIXES[model_name]
    elif "e5" in model_name.lower():
        default_q, default_p = ("query: ", "passage: ")

    final_q = query_prefix if query_prefix is not None else default_q
    final_p = passage_prefix if passage_prefix is not None else default_p
    return final_q, final_p
