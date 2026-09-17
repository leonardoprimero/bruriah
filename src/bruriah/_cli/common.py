from __future__ import annotations

import argparse
import os

from ..platform import PlatformError, PlatformPaths, resolve_paths


class CliError(ValueError):
    """Typed failure for every `bruriah` command, mirroring `PlatformError`/`ServiceError`."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def resolve_cli_paths(args: argparse.Namespace) -> PlatformPaths:
    try:
        paths = resolve_paths(
            cli_config_dir=args.config_dir,
            cli_data_dir=args.data_dir,
            cli_cache_dir=args.cache_dir,
            cli_log_dir=args.log_dir,
            cli_network_enabled=args.network_enabled,
            cli_skill_ceiling=getattr(args, "skill_ceiling", None),
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
