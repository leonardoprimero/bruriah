# Platform lifecycle (Slice 8A): resolves user-private config/data/cache/log dirs via
# `platformdirs` and assembles the real `ServiceDeps` the `cerebro-mcp` `serve`/`doctor` commands
# need. Owns no retrieval/routing/protocol logic; composes only frozen index/packs/registries/
# service primitives. Windows durability (descriptor/no-follow, share-mode identity) is NOT
# VALIDATED on this Darwin host -- index.py's activation is POSIX-only (fcntl, O_NOFOLLOW, /dev/fd).
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import platformdirs

from .index import ActiveSnapshot, BuildConfig, IndexLifecycleError, snapshot_active
from .packs import PackError, load_pack
from .registries import Registry
from .service import ServiceDeps

APP_NAME = "cerebro-router"
ENV_PREFIX = "CEREBRO_ROUTER_"
_BUNDLED_DATA = Path(__file__).resolve().parent / "data"
_CONFIG_KEYS = {"config_dir", "data_dir", "cache_dir", "log_dir", "network_enabled"}


class PlatformError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PlatformPaths:
    config_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    network_enabled: bool = False


def _private_defaults() -> PlatformPaths:
    return PlatformPaths(
        config_dir=Path(platformdirs.user_config_dir(APP_NAME)),
        data_dir=Path(platformdirs.user_data_dir(APP_NAME)),
        cache_dir=Path(platformdirs.user_cache_dir(APP_NAME)),
        log_dir=Path(platformdirs.user_log_dir(APP_NAME)),
    )


def _env_path(environment: dict[str, str], suffix: str) -> Path | None:
    value = environment.get(ENV_PREFIX + suffix)
    return Path(value).expanduser() if value else None


def _env_bool(environment: dict[str, str], suffix: str) -> bool | None:
    value = environment.get(ENV_PREFIX + suffix)
    return None if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _read_json(path: Path, missing_code: str, invalid_code: str) -> object:
    # UnicodeDecodeError is a ValueError, NOT an OSError, so a non-UTF-8 file must be caught here.
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise PlatformError(missing_code) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformError(invalid_code) from error


def _load_config_file(path: Path) -> dict[str, object]:
    value = _read_json(path, "config_unreadable", "invalid_config")
    if not isinstance(value, dict) or not set(value) <= _CONFIG_KEYS:
        raise PlatformError("invalid_config")
    return value


def resolve_paths(
    *,
    cli_config_dir: Path | None = None,
    cli_data_dir: Path | None = None,
    cli_cache_dir: Path | None = None,
    cli_log_dir: Path | None = None,
    cli_network_enabled: bool | None = None,
    env: dict[str, str] | None = None,
    config_path: Path | None = None,
) -> PlatformPaths:
    """Resolve private dirs and network policy. Precedence: CLI arg > env var > config file >
    private `platformdirs` default; network defaults off unless explicitly enabled at any level."""
    environment = os.environ if env is None else env
    defaults = _private_defaults()
    config_dir = cli_config_dir or _env_path(environment, "CONFIG_DIR") or defaults.config_dir

    file_values: dict[str, object] = {}
    file_path = config_path or _env_path(environment, "CONFIG_FILE") or (config_dir / "config.json")
    if file_path.is_file():
        file_values = _load_config_file(file_path)

    def resolve(cli_value: Path | None, suffix: str, key: str, default: Path) -> Path:
        if cli_value is not None:
            return cli_value
        env_value = _env_path(environment, suffix)
        if env_value is not None:
            return env_value
        return Path(str(file_values[key])) if key in file_values else default

    data_dir = resolve(cli_data_dir, "DATA_DIR", "data_dir", defaults.data_dir)
    cache_dir = resolve(cli_cache_dir, "CACHE_DIR", "cache_dir", defaults.cache_dir)
    log_dir = resolve(cli_log_dir, "LOG_DIR", "log_dir", defaults.log_dir)

    env_network = _env_bool(environment, "NETWORK_ENABLED")
    if cli_network_enabled is not None:
        network_enabled = cli_network_enabled
    elif env_network is not None:
        network_enabled = env_network
    else:
        network_enabled = bool(file_values.get("network_enabled", False))

    return PlatformPaths(config_dir, data_dir, cache_dir, log_dir, network_enabled)


def ensure_private_dirs(paths: PlatformPaths) -> None:
    """Create the dirs with user-private (0700) permissions on POSIX; Windows ACL NOT VALIDATED."""
    for directory in (paths.config_dir, paths.data_dir, paths.cache_dir, paths.log_dir):
        directory.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(directory, 0o700)


_BUILD_DESCRIPTOR_FIELDS = (
    "root", "policy_path", "schema_version", "parser_version", "service_version", "mcp_range",
    "embedding_model", "embedding_revision", "embedding_dimensions", "embedding_fingerprint",
    "ranking_config",
)


def write_build_descriptor(paths: PlatformPaths, config: BuildConfig) -> None:
    """Persist `index`'s `BuildConfig` so `serve`/`doctor` can validate the promoted snapshot
    against an identical config without re-loading an embedding model."""
    payload = {field: str(getattr(config, field)) if field in {"root", "policy_path"}
               else getattr(config, field) for field in _BUILD_DESCRIPTOR_FIELDS}
    (paths.data_dir / "build-config.json").write_text(
        json.dumps(payload, sort_keys=True), encoding="utf-8"
    )


def load_build_descriptor(paths: PlatformPaths) -> BuildConfig:
    payload = _read_json(paths.data_dir / "build-config.json", "index_not_built", "corrupt_build_descriptor")
    try:
        return BuildConfig(
            root=Path(payload["root"]), policy_path=Path(payload["policy_path"]),
            schema_version=payload["schema_version"], parser_version=payload["parser_version"],
            service_version=payload["service_version"], mcp_range=payload["mcp_range"],
            embedding_model=payload["embedding_model"], embedding_revision=payload["embedding_revision"],
            embedding_dimensions=payload["embedding_dimensions"],
            embedding_fingerprint=payload["embedding_fingerprint"], ranking_config=payload["ranking_config"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PlatformError("corrupt_build_descriptor") from error


def load_registry(today: date | None = None) -> Registry:
    """Load the bundled research-policy pack into a `Registry`. `today` is injectable (default:
    real date) so the fail-closed freshness/expiry check is deterministic in tests instead of a
    wall-clock time bomb; production uses the real date, surfacing a stale bundled pack as
    `registry_load_failed`."""
    try:
        roots = json.loads((_BUNDLED_DATA / "trust-roots.json").read_text(encoding="utf-8"))
        pack = load_pack(
            _BUNDLED_DATA / "research-policy.json", _BUNDLED_DATA / "research-policy.manifest.json", roots,
            today=today,
        )
    except PackError as error:
        raise PlatformError(f"registry_load_failed:{error.code}") from error
    return Registry.from_packs([pack])


def open_snapshot(paths: PlatformPaths) -> ActiveSnapshot:
    """Open the active candidate snapshot under `paths.data_dir` read-only; never promotes,
    builds, or mutates. Missing/uninitialized/incompatible state fails typed."""
    config = load_build_descriptor(paths)
    pointer = paths.data_dir / "active.json"
    if not pointer.is_file():
        raise PlatformError("index_not_built")
    try:
        return snapshot_active(pointer, config)
    except IndexLifecycleError as error:
        raise PlatformError(f"snapshot_unreadable:{error.code}") from error


def load_deps(
    paths: PlatformPaths,
    today: date | None = None,
    embed_query: Callable[[str], bytes] | None = None,
) -> ServiceDeps:
    """Assemble the real `ServiceDeps` `serve`/`doctor` need: the bundled registry and the
    active read-only snapshot under the resolved private data directory. `today` forwards to
    `load_registry` for a deterministic freshness check under test. `embed_query` (default
    `None`) threads straight into `ServiceDeps` -- this module never constructs one itself (no
    model load here), so `doctor` and every existing caller that omits it keep the exact same
    embedder-less, light-weight behavior as before. The real query embedder is `cli.py`'s
    `build_serve_deps`'s job to build and inject for `serve`."""
    return ServiceDeps(
        registry=load_registry(today), snapshot=open_snapshot(paths), embed_query=embed_query,
    )


__all__ = [
    "PlatformError", "PlatformPaths", "ensure_private_dirs", "load_build_descriptor",
    "load_deps", "load_registry", "open_snapshot", "resolve_paths", "write_build_descriptor",
]
