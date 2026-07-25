# Slice 8A: platform private-dir resolution + the deps loader. The real-pipeline tests build a
# real candidate index through the frozen build/promote path, load real ServiceDeps, and answer
# real investigate_work/read_evidence over the mcp in-memory transport (constraint 1 discipline).
from __future__ import annotations

import json
import os
import stat
from array import array
from datetime import date
from pathlib import Path

import anyio
import pytest
from cerebro_router.corpus import CorpusPolicy
from cerebro_router.index import BuildConfig, build_candidate, promote_candidate
from cerebro_router.mcp_server import build_server
from cerebro_router.platform import (
    PlatformError, ensure_private_dirs, load_build_descriptor, load_deps, load_registry,
    open_snapshot, resolve_paths, write_build_descriptor,
)
from mcp.shared.memory import create_connected_server_and_client_session

# Pinned so the fail-closed freshness check is deterministic, not a wall-clock time bomb.
# Bumped from 2026-07-23 when the second bundled pack landed: `programming-policy` is
# reviewed 2026-07-25, and a `today` earlier than a pack's review date is `future_review`.
_TODAY = date(2026, 7, 25)

FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
_FILLER = "Unrelated filler sentence for padding purposes only. " * 6
_TASK = "Find a python schema validation library, apple pie baking recipe"


def _embed(texts: list[str]) -> list[bytes]:
    return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]


def _build_and_promote(tmp_path: Path, data_dir: Path) -> BuildConfig:
    root = tmp_path / "vault" / "public"
    root.mkdir(parents=True)
    (root / "en.md").write_text(
        f"# Apple\nAn apple pie baking recipe passage with real corpus text.\n{_FILLER}\n",
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    config = BuildConfig(
        root=tmp_path / "vault", policy_path=policy_path, schema_version=1, parser_version="corpus-v1",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model="test/minilm",
        embedding_revision="snapshot-a", embedding_dimensions=3, embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    build_candidate(config, data_dir / "candidate.sqlite3", policy, _embed)
    promote_candidate(data_dir / "candidate.sqlite3", data_dir / "active.json", config, policy)
    return config


def test_precedence_cli_over_env_over_config_file_over_default(tmp_path: Path) -> None:
    cli_dir, env_dir, file_dir = tmp_path / "cli", tmp_path / "env", tmp_path / "file"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"data_dir": str(file_dir)}), encoding="utf-8")

    assert resolve_paths(cli_config_dir=config_dir, env={}).data_dir == file_dir
    assert resolve_paths(
        cli_config_dir=config_dir, env={"CEREBRO_ROUTER_DATA_DIR": str(env_dir)}
    ).data_dir == env_dir
    assert resolve_paths(
        cli_config_dir=config_dir, cli_data_dir=cli_dir,
        env={"CEREBRO_ROUTER_DATA_DIR": str(env_dir)},
    ).data_dir == cli_dir


def test_default_paths_are_never_auto_created(tmp_path: Path) -> None:
    import platformdirs

    real_data_dir = Path(platformdirs.user_data_dir("cerebro-router"))
    existed_before = real_data_dir.exists()
    paths = resolve_paths(env={})
    assert paths.data_dir == real_data_dir
    assert real_data_dir.exists() == existed_before


def test_network_defaults_off_unless_explicitly_enabled(tmp_path: Path) -> None:
    assert resolve_paths(env={}).network_enabled is False
    assert resolve_paths(env={"CEREBRO_ROUTER_NETWORK_ENABLED": "true"}).network_enabled is True
    assert resolve_paths(cli_network_enabled=True, env={}).network_enabled is True


def test_ensure_private_dirs_creates_only_the_resolved_base(tmp_path: Path) -> None:
    base = tmp_path / "private"
    paths = resolve_paths(
        cli_config_dir=base / "config", cli_data_dir=base / "data",
        cli_cache_dir=base / "cache", cli_log_dir=base / "log", env={},
    )
    ensure_private_dirs(paths)
    assert paths.data_dir.is_dir() and paths.config_dir.is_dir()
    if os.name == "posix":
        assert stat.S_IMODE(paths.data_dir.stat().st_mode) == 0o700


def test_invalid_config_file_is_a_typed_platform_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for content in (b"not json", b'{"data_dir": "\xff\xfe"}'):  # invalid JSON, then non-UTF-8 bytes
        (config_dir / "config.json").write_bytes(content)
        with pytest.raises(PlatformError) as error:
            resolve_paths(cli_config_dir=config_dir, env={})
        assert error.value.code == "invalid_config"


def test_corrupt_build_descriptor_is_a_typed_platform_error(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    (data_dir / "build-config.json").write_bytes(b"\xff\xfe not utf8")  # non-UTF-8, not OSError
    with pytest.raises(PlatformError) as error:
        load_build_descriptor(paths)
    assert error.value.code == "corrupt_build_descriptor"


def test_open_snapshot_without_a_built_index_is_typed_and_read_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    with pytest.raises(PlatformError) as error:
        open_snapshot(paths)
    assert error.value.code == "index_not_built"
    assert not data_dir.exists()


def test_load_registry_freshness_is_date_injectable_not_wall_clock() -> None:
    # Injected date so freshness is deterministic, not a wall-clock time bomb after the window.
    assert load_registry(today=date(2026, 7, 25)).pack_ids == ("programming.minimal", "research.minimal")
    # Loading is all-or-nothing: the earliest-expiring bundled pack takes the whole registry down
    # rather than leaving a silently truncated one, which a caller could not distinguish from a
    # deliberately small registry.
    with pytest.raises(PlatformError) as expired:
        load_registry(today=date(2027, 7, 24))
    assert expired.value.code == "registry_load_failed:expired_pack"
    # A pack whose review is in the future is refused too, which is what keeps a mis-dated pack from
    # quietly loading.
    with pytest.raises(PlatformError) as future:
        load_registry(today=date(2026, 7, 24))
    assert future.value.code == "registry_load_failed:future_review"


def test_real_index_then_load_deps_produces_a_working_service_deps(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _build_and_promote(tmp_path, data_dir)
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    write_build_descriptor(paths, config)

    assert load_build_descriptor(paths) == config

    deps = load_deps(paths, today=_TODAY)
    try:
        assert deps.registry.pack_ids
        assert deps.snapshot.build_id
    finally:
        deps.snapshot.database.close()


def test_loader_never_touches_live_cerebro_db(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _build_and_promote(tmp_path, data_dir)
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    write_build_descriptor(paths, config)
    live_db = Path(__file__).resolve().parents[1] / "cerebro.db"
    hash_before = live_db.stat().st_mtime_ns
    load_deps(paths, today=_TODAY).snapshot.database.close()
    assert live_db.stat().st_mtime_ns == hash_before
    assert "cerebro.db" not in {path.name for path in data_dir.iterdir()}


def test_read_only_open_never_mutates_the_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _build_and_promote(tmp_path, data_dir)
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    write_build_descriptor(paths, config)
    pointer = data_dir / "active.json"
    before_bytes = pointer.read_bytes()
    before_names = sorted(path.name for path in data_dir.iterdir())
    load_deps(paths, today=_TODAY).snapshot.database.close()  # the read-only open 8A-2 `doctor` does

    assert pointer.read_bytes() == before_bytes
    assert sorted(path.name for path in data_dir.iterdir()) == before_names


def test_real_pipeline_build_server_answers_investigate_and_read_over_mcp(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config = _build_and_promote(tmp_path, data_dir)
    paths = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config", env={})
    write_build_descriptor(paths, config)
    deps = load_deps(paths, today=_TODAY)
    server = build_server(deps)

    async def _run() -> None:
        async with create_connected_server_and_client_session(server) as session:
            await session.initialize()
            investigated = await session.call_tool("investigate_work", {"task": _TASK})
            assert not investigated.isError
            evidence = investigated.structuredContent["evidence"]
            ref = next(item["ref"] for item in evidence if item["kind"] == "local")
            read = await session.call_tool("read_evidence", {"refs": [ref]})
            assert not read.isError
            assert read.structuredContent["items"][0]["status"] == "ok"

    try:
        anyio.run(_run)
    finally:
        deps.snapshot.database.close()


def test_the_bundled_registry_reconciles_exactly_one_classifier_domain() -> None:
    """The point of the second bundled pack: `programming` resolves, everything else abstains.

    Asserted as an exact set rather than "programming is True", so quietly widening a pack's
    `domains` shows up here as a failure instead of as silently broader routing."""
    import typing

    from cerebro_router.classify import Domain, RequestClassification
    from cerebro_router.lookup import discover

    registry = load_registry(today=_TODAY)
    supported = {
        domain
        for domain in typing.get_args(Domain)
        if discover(
            RequestClassification(intent="investigate", domain=domain, claim_type="factual",
                                  risk="low", jurisdiction="unknown"),
            registry,
        ).domain_supported
    }
    assert supported == {"programming"}


def test_the_retired_test_signer_is_no_longer_trusted() -> None:
    """`cerebro-release-test` was a signer whose private half nobody controls. Retiring it is only
    real if a pack bearing its name is now refused, so that is what gets asserted -- the absence of a
    key from a JSON file proves nothing on its own."""
    import json as _json

    from cerebro_router.packs import PackError, load_pack

    data = Path(__file__).parents[1] / "src/cerebro_router/data"
    roots = _json.loads((data / "trust-roots.json").read_text())
    assert "cerebro-release-test" not in roots
    assert set(roots) == {"cerebro-release"}

    forged = _json.loads((data / "research-policy.manifest.json").read_text())
    forged["signer"] = "cerebro-release-test"
    forged_path = Path(__file__).parent / "_forged.manifest.json"
    forged_path.write_text(_json.dumps(forged))
    try:
        with pytest.raises(PackError) as refused:
            load_pack(data / "research-policy.json", forged_path, roots, today=_TODAY)
        assert refused.value.code == "unknown_signer"
    finally:
        forged_path.unlink()


def test_both_bundled_packs_ship_with_a_verifiable_manifest() -> None:
    """Packaging assertion: every pack in the data directory has a manifest beside it that verifies
    through the unmodified load path. A pack that ships without one is dead weight the loader will
    refuse at startup."""
    from cerebro_router.packs import load_pack

    data = Path(__file__).parents[1] / "src/cerebro_router/data"
    roots = json.loads((data / "trust-roots.json").read_text())
    packs = sorted(item for item in data.glob("*.json") if not item.name.endswith(".manifest.json")
                   and item.name != "trust-roots.json")
    assert [item.stem for item in packs] == ["programming-policy", "research-policy"]
    for pack in packs:
        manifest = pack.with_suffix(".manifest.json")
        assert manifest.is_file(), pack.name
        assert load_pack(pack, manifest, roots, today=_TODAY).version == "1.0.0"
