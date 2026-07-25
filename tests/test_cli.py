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
from cerebro_router.platform import load_deps, resolve_paths
from cerebro_router.retrieval import search as router_search
from mcp.shared.memory import create_connected_server_and_client_session

# Pinned so the doctor freshness check is deterministic, not a wall-clock time bomb.
# Bumped from 2026-07-23 when the second bundled pack landed: `programming-policy` is
# reviewed 2026-07-25, and a `today` earlier than a pack's review date is `future_review`.
_TODAY = date(2026, 7, 25)
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
    assert report["registry"]["pack_ids"] == ["programming.minimal", "research.minimal"]

    capsys.readouterr()  # discard init/index stdout before the serve-wiring stdout-clean check
    deps = cli.build_serve_deps(paths, embedder_factory=_fake_embedder_factory)
    assert deps.embed_query is not None  # bugfix: serve now wires a real query embedder
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

    # Both bundled packs are reviewed mid-2026 with freshness_days=365, so the 7-day warning
    # window opens in July 2027. doctor still warns BEFORE the hard fail, which is the point.
    near_stale = date(2027, 7, 20)
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


# ---------------------------------------------------------------------------
# Bugfix regression: `build_serve_deps` used to call `platform.load_deps(paths)` with no
# `embed_query`, so the deployed `cerebro-mcp serve` process always ran retrieval BM25-only
# (`vector_leg_unavailable`). It now builds a real query embedder from the active snapshot's own
# recorded model, fail-closed verified, and threads it into `load_deps`.
# ---------------------------------------------------------------------------


def _index_with_fake_embedder(tmp_path: Path):
    root, policy_path = _corpus(tmp_path)
    paths = _paths(tmp_path)
    cli.run_index(
        paths, root, policy_path, model_name="test/minilm", embedder_factory=_fake_embedder_factory,
    )
    return paths


def test_build_serve_deps_wires_a_real_query_embedder_and_activates_the_vector_leg(
    tmp_path: Path,
) -> None:
    """`build_serve_deps` reuses the SAME `EmbedderFactory` `index` used (query vectors come from
    the identical code path as passage vectors -- matching by construction), so `search()` over
    the resulting deps must run BOTH legs, never degrading `vector_leg_unavailable`."""
    paths = _index_with_fake_embedder(tmp_path)

    deps = cli.build_serve_deps(paths, embedder_factory=_fake_embedder_factory)
    try:
        assert deps.embed_query is not None
        query_vector = deps.embed_query("apple pie baking recipe")
        assert isinstance(query_vector, bytes)
        assert len(query_vector) == 3 * 4  # 3 dims, float32 little-endian

        outcome = router_search(deps.snapshot, _TASK, embed_query=deps.embed_query)
        assert "vector_leg_unavailable" not in outcome.degradation
    finally:
        deps.snapshot.database.close()


def test_build_serve_deps_default_embedder_factory_is_the_real_fastembed_one() -> None:
    """`build_serve_deps` must default to the real `_default_embedder_factory` (never silently
    swap in a fake), so `serve`'s production path always builds a genuine query embedder."""
    import inspect

    default = inspect.signature(cli.build_serve_deps).parameters["embedder_factory"].default
    assert default is cli._default_embedder_factory


def test_build_serve_deps_fingerprint_mismatch_is_a_typed_fail_closed_error(tmp_path: Path) -> None:
    """A constructed embedder whose fingerprint does NOT match what the active snapshot was
    actually built with must raise a typed `embedding_model_mismatch` CliError -- never silently
    embed queries in the wrong vector space (worse than no vector leg at all)."""
    paths = _index_with_fake_embedder(tmp_path)
    wrong_fingerprint = _FINGERPRINT.replace("snapshot-a", "snapshot-b")

    def _wrong_model_embedder_factory(model_name: str) -> tuple[cli.Embedder, str, int]:
        def embed(texts: list[str]) -> list[bytes]:
            return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

        return embed, wrong_fingerprint, 3

    with pytest.raises(cli.CliError) as error:
        cli.build_serve_deps(paths, embedder_factory=_wrong_model_embedder_factory)
    assert error.value.code == "embedding_model_mismatch"


def test_build_serve_deps_dimension_mismatch_is_a_typed_fail_closed_error(tmp_path: Path) -> None:
    """A constructed embedder with the RIGHT fingerprint but the WRONG dimension count (e.g. a
    caller-supplied `EmbedderFactory` bug) must still fail closed instead of poisoning the
    vector leg with mismatched-length vectors."""
    paths = _index_with_fake_embedder(tmp_path)

    def _wrong_dimensions_embedder_factory(model_name: str) -> tuple[cli.Embedder, str, int]:
        def embed(texts: list[str]) -> list[bytes]:
            return [array("f", (1.0, 0.0, 0.0, 0.0)).tobytes() for _ in texts]

        return embed, _FINGERPRINT, 4  # index built with dimensions=3

    with pytest.raises(cli.CliError) as error:
        cli.build_serve_deps(paths, embedder_factory=_wrong_dimensions_embedder_factory)
    assert error.value.code == "embedding_model_mismatch"


def test_load_deps_default_embed_query_stays_none_doctor_path_light(tmp_path: Path) -> None:
    """`platform.load_deps`'s own default (no `embed_query` argument) must stay `None` -- doctor
    (and any caller that doesn't opt in) never triggers a model load."""
    paths = _index_with_fake_embedder(tmp_path)
    deps = load_deps(paths, today=_TODAY)
    try:
        assert deps.embed_query is None
    finally:
        deps.snapshot.database.close()


def test_load_deps_threads_an_explicit_embed_query_into_service_deps(tmp_path: Path) -> None:
    """`platform.load_deps` must thread a caller-supplied `embed_query` straight into
    `ServiceDeps` unchanged -- the exact callable identity, not a wrapped/copied one."""
    paths = _index_with_fake_embedder(tmp_path)

    def _embed_query(text: str) -> bytes:
        return array("f", (1.0, 0.0, 0.0)).tobytes()

    deps = load_deps(paths, today=_TODAY, embed_query=_embed_query)
    try:
        assert deps.embed_query is _embed_query
        outcome = router_search(deps.snapshot, _TASK, embed_query=deps.embed_query)
        assert "vector_leg_unavailable" not in outcome.degradation
    finally:
        deps.snapshot.database.close()
