# Slice 8A-2: `cerebro-mcp {init,serve,index,doctor}` over the Slice 8A-1 `platform.py` loader.
# Mandatory test below: real init -> index -> serve-wiring -> doctor against a tiny TMP corpus
# with an injected fake embedder -- never the live cerebro.db, never mocked build/promote/load.
from __future__ import annotations

import json
import os
import stat
import sys
from array import array
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anyio
import platformdirs
import pytest
from cerebro_router import cache, cli, clients
from cerebro_router.contracts import EvidenceRecord
from cerebro_router.platform import resolve_paths
from mcp.shared.memory import create_connected_server_and_client_session

# Pinned so the doctor freshness check is deterministic, not a wall-clock time bomb.
_TODAY = date(2026, 7, 23)
_NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)  # pinned: cache-stats freshness too
_FINGERPRINT = (
    '{"artifact":"model.onnx","artifact_sha256":"' + "a" * 64
    + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
)
_FILLER = "Unrelated filler sentence for padding purposes only. " * 6
_TASK = "Find a python schema validation library, apple pie baking recipe"


def _fake_embedder_factory(model_name: str) -> tuple[cli.Embedder, str, int]:
    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

    return embed, _FINGERPRINT, 3


def _corpus(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    (root / "public").mkdir(parents=True)
    (root / "public" / "en.md").write_text(
        f"# Apple\nAn apple pie baking recipe passage with real corpus text.\n{_FILLER}\n",
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    return root, policy_path


def _paths(tmp_path: Path):
    return resolve_paths(
        cli_config_dir=tmp_path / "config", cli_data_dir=tmp_path / "data",
        cli_cache_dir=tmp_path / "cache", cli_log_dir=tmp_path / "log", env={},
    )


def _evidence_for_cache(**overrides: object) -> EvidenceRecord:
    """Minimal `EvidenceRecord` for driving `cache.write_cache_atomic` directly in doctor tests
    (Slice 12D) -- mirrors `test_cache.py`'s own `_evidence` helper."""
    payload = dict(
        ref="live:sha256:" + "a" * 32, kind="captured_live", publisher="example.test",
        locator="https://example.test:443/page", citation_locator="https://example.test:443/page",
        digest="sha256:" + "b" * 64, extraction_method="raw_lines", authority="unknown",
        authority_rationale="Live HTTP fetch.", freshness="unknown", license="unknown",
        reuse="unknown", conflict="unknown", retrieved_at=_NOW,
    )
    payload.update(overrides)
    return EvidenceRecord(**payload)


def _write_cache_entry(cache_dir: Path, url: str, *, retrieved_at: datetime) -> None:
    entry = cache.build_cache_entry(
        _evidence_for_cache(retrieved_at=retrieved_at), retrieved_at=retrieved_at,
        ttl=timedelta(hours=1), body=b"cached body content", max_excerpt_chars=1000,
        policy_version="1.0.0",
    )
    cache.write_cache_atomic(cache_dir, url, entry)


def test_end_to_end_init_index_serve_doctor(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root, policy_path = _corpus(tmp_path)
    paths = _paths(tmp_path)

    config_file = cli.run_init(paths)
    assert config_file == paths.config_dir / "config.json"
    if os.name == "posix":
        assert stat.S_IMODE(paths.data_dir.stat().st_mode) == 0o700

    result = cli.run_index(
        paths, root, policy_path, model_name="test/minilm", embedder_factory=_fake_embedder_factory,
    )
    assert result.documents == 1 and result.passages >= 1
    assert (paths.data_dir / "build-config.json").is_file()
    assert "cerebro.db" not in {path.name for path in paths.data_dir.iterdir()}

    report = cli.run_doctor(paths, today=_TODAY)
    assert report["healthy"] is True
    assert report["snapshot"]["build_id"] == result.build_id
    assert report["registry"]["pack_ids"] == ["research.minimal"]

    capsys.readouterr()  # discard init/index stdout before the serve-wiring stdout-clean check
    deps = cli.build_serve_deps(paths)
    server = cli.build_server(deps)
    try:
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
        anyio.run(_run)
    finally:
        deps.snapshot.database.close()
    assert capsys.readouterr().out == ""  # stdout is the JSON-RPC channel: no diagnostics

    live_db = Path(__file__).resolve().parents[1] / "cerebro.db"
    assert "cerebro.db" not in {path.name for path in paths.data_dir.iterdir()}
    assert live_db.exists()  # untouched (name check above already proves no write occurred)


def test_init_is_idempotent_and_registers_no_client(tmp_path: Path) -> None:
    real_config_dir = Path(platformdirs.user_config_dir("cerebro-router"))
    existed_before = real_config_dir.exists()
    paths = _paths(tmp_path)
    cli.run_init(paths)
    custom = json.dumps({"network_enabled": True}, sort_keys=True) + "\n"
    (paths.config_dir / "config.json").write_text(custom, encoding="utf-8")
    cli.run_init(paths)  # re-running init must not clobber an existing config
    assert (paths.config_dir / "config.json").read_text(encoding="utf-8") == custom
    assert real_config_dir.exists() == existed_before  # init touches only the resolved tmp dir


def _init_args(paths) -> list[str]:
    return [
        "init", "--config-dir", str(paths.config_dir), "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir), "--log-dir", str(paths.log_dir),
    ]


def test_init_writes_all_client_configs_with_private_perms_and_manifest_cross_check(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    args = cli._build_cli_parser().parse_args(_init_args(paths))
    assert cli._cmd_init(args) == 0

    manifest = cli._build_launch_manifest(paths)
    assert manifest.command == sys.executable
    assert manifest.full_argv[:3] == [sys.executable, "-m", "cerebro_router.cli"]

    clients_dir = paths.config_dir / "clients"
    expected = clients.render_all(manifest)
    assert {path.name for path in clients_dir.iterdir()} == {
        f"{client_id.value}.json" for client_id in clients.ClientId
    }
    for client_id, rendered in expected.items():
        target = clients_dir / f"{client_id.value}.json"
        assert target.read_text(encoding="utf-8") == rendered
        json.loads(rendered)  # every rendered client config is valid JSON

    generic = json.loads((clients_dir / f"{clients.ClientId.GENERIC_STDIO.value}.json").read_text())
    assert [generic["command"], *generic["args"]] == manifest.full_argv  # cross-checked argv

    config = json.loads((paths.config_dir / "config.json").read_text(encoding="utf-8"))
    assert config == {"network_enabled": False}

    if os.name == "posix":
        assert stat.S_IMODE(clients_dir.stat().st_mode) == 0o700
        for target in clients_dir.iterdir():
            assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_init_client_configs_are_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    args = cli._build_cli_parser().parse_args(_init_args(paths))
    assert cli._cmd_init(args) == 0
    clients_dir = paths.config_dir / "clients"
    before = {path.name: path.read_text(encoding="utf-8") for path in clients_dir.iterdir()}
    assert cli._cmd_init(args) == 0  # re-running init must not error or duplicate/clobber
    after = {path.name: path.read_text(encoding="utf-8") for path in clients_dir.iterdir()}
    assert before == after


def test_init_client_manifest_failure_is_typed_not_bare(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    bad_config_dir = tmp_path / "bad;name"  # a shell metacharacter lands in a rendered arg
    exit_code = cli.cerebro_mcp_main([
        "init", "--config-dir", str(bad_config_dir), "--data-dir", str(tmp_path / "data"),
        "--cache-dir", str(tmp_path / "cache"), "--log-dir", str(tmp_path / "log"),
    ])
    assert exit_code == 1
    assert "client_manifest_invalid" in capsys.readouterr().err


def test_doctor_is_read_only_and_reports_freshness(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    uninitialized = cli.run_doctor(paths, today=_TODAY, now=_NOW)
    assert uninitialized["healthy"] is False
    assert uninitialized["snapshot"] == {"status": "error", "code": "index_not_built"}
    assert uninitialized["cache"] == {"entries": 0, "expired": 0, "total_bytes": 0}

    near_stale = date(2026, 8, 20)  # bundled pack reviewed 2026-07-23, freshness_days=30
    warned = cli.run_doctor(paths, today=near_stale, now=_NOW)
    assert warned["registry"]["status"] == "ok"
    assert any("goes stale" in warning for warning in warned["warnings"])
    assert not paths.data_dir.exists()  # doctor never creates the dir it only inspects
    assert not paths.cache_dir.exists()  # doctor never creates the cache dir either


def test_doctor_reports_cache_stats_read_only_never_deletes_an_expired_entry(tmp_path: Path) -> None:
    """Slice 12D: `doctor` gains cache visibility but design.md's "`doctor` is read-only" holds --
    it never calls `cache.prune_expired`, so even a genuinely expired entry survives untouched."""
    paths = _paths(tmp_path)
    _write_cache_entry(paths.cache_dir, "https://example.test:443/live", retrieved_at=_NOW)
    expired_at = _NOW - timedelta(hours=5)  # ttl=1h -> expires_at = NOW-4h, already past `now=_NOW`
    _write_cache_entry(paths.cache_dir, "https://example.test:443/expired", retrieved_at=expired_at)

    before = {path.name for path in paths.cache_dir.iterdir()}
    report = cli.run_doctor(paths, today=_TODAY, now=_NOW)
    after = {path.name for path in paths.cache_dir.iterdir()}

    assert after == before  # doctor deleted nothing
    assert report["cache"]["entries"] == 2
    assert report["cache"]["expired"] == 1
    assert report["cache"]["total_bytes"] > 0


def test_cli_dispatch_typed_errors_no_bare_exception(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    root, policy_path = _corpus(tmp_path)

    missing_root = tmp_path / "does-not-exist"
    exit_code = cli.cerebro_mcp_main([
        "index", "--config-dir", str(config_dir), "--data-dir", str(data_dir),
        "--corpus-root", str(missing_root), "--policy", str(policy_path),
    ])
    assert exit_code == 1
    assert "corpus_root_not_found" in capsys.readouterr().err

    config_dir.mkdir()
    (config_dir / "config.json").write_bytes(b"not json")
    exit_code = cli.cerebro_mcp_main(["doctor", "--config-dir", str(config_dir)])
    assert exit_code == 1
    assert "invalid_config" in capsys.readouterr().err

    empty_config = tmp_path / "empty-config"
    exit_code = cli.cerebro_mcp_main([
        "serve", "--config-dir", str(empty_config), "--data-dir", str(tmp_path / "empty-data"),
    ])
    assert exit_code == 1  # never enters the blocking stdio loop on an uninitialized dir
    assert "index_not_built" in capsys.readouterr().err

    bad_policy = tmp_path / "bad.yaml"
    bad_policy.write_text("include: [unclosed\n:::", encoding="utf-8")  # exists but malformed YAML
    exit_code = cli.cerebro_mcp_main([
        "index", "--config-dir", str(tmp_path / "c2"), "--data-dir", str(tmp_path / "d2"),
        "--corpus-root", str(root), "--policy", str(bad_policy),
    ])
    assert exit_code == 1 and "index_failed" in capsys.readouterr().err  # typed, not a traceback

    # init's mkdir into a file-path (no per-site guard) must still exit typed via the backstop.
    file_seg = tmp_path / "afile"; file_seg.write_text("x", encoding="utf-8")
    exit_code = cli.cerebro_mcp_main(["init", "--config-dir", str(file_seg / "cfg"),
        "--data-dir", str(tmp_path / "d3"), "--cache-dir", str(tmp_path / "ca3"), "--log-dir", str(tmp_path / "l3")])
    assert exit_code == 1 and "cerebro-mcp: error:" in capsys.readouterr().err


def test_cli_index_dispatch_builds_only_under_private_data_dir(tmp_path: Path) -> None:
    root, policy_path = _corpus(tmp_path)
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    args = cli._build_cli_parser().parse_args([
        "index", "--config-dir", str(config_dir), "--data-dir", str(data_dir),
        "--corpus-root", str(root), "--policy", str(policy_path),
    ])
    exit_code = cli._cmd_index(args, embedder_factory=_fake_embedder_factory)
    assert exit_code == 0
    assert (data_dir / "active.json").is_file()
    assert all(path.parent == data_dir for path in data_dir.rglob("candidate-*.sqlite3"))
