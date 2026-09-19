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
}


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
