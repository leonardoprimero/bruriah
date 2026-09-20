# Slice 8A-2: `bruriah {init,serve,index,doctor}` over the Slice 8A-1 `platform.py` loader.
# Mandatory test below: real init -> index -> serve-wiring -> doctor against a tiny TMP corpus
# with an injected fake embedder -- never the live cerebro.db, never mocked build/promote/load.
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from array import array
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anyio
import platformdirs
import pytest

from conftest import requires_vault
from bruriah import __version__ as bruriah_version
from bruriah import cache, cli, clients, packs
from bruriah.contracts import EvidenceRecord
from bruriah.platform import load_deps, open_snapshot, resolve_paths
from bruriah.retrieval import search as router_search
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


@requires_vault
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
    assert report["registry"]["pack_ids"] == [
        "programming.minimal", "project.memory", "research.minimal"]

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
    real_config_dir = Path(platformdirs.user_config_dir("bruriah"))
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
    assert manifest.full_argv[:3] == [sys.executable, "-m", "bruriah.cli"]

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
    exit_code = cli.bruriah_main([
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


def test_doctor_warns_about_expiry_far_earlier_than_about_staleness(tmp_path: Path) -> None:
    """Seven days' notice, worded "goes stale", for a capability the user silently loses.

    A stale pack still answers and says so. An expired one stops registering its domains, so every
    request that used to be routed by it abstains -- on an installation nobody touched, on a date
    nobody chose, and only re-signed packs restore it. The bundled packs set `expires_at` to exactly
    `reviewed_at + freshness_days`, so the staleness warning fired the same week and looked like it
    had this covered.
    """
    paths = _paths(tmp_path)
    early = cli.run_doctor(paths, today=date(2027, 4, 25), now=_NOW)
    assert early["registry"]["status"] == "ok"
    expiry = [warning for warning in early["warnings"] if "expires in" in warning]
    assert expiry, "no expiry warning three months out"
    # Naming the consequence, and naming it accurately: the gap a caller will actually receive.
    assert "abstain" in expiry[0] and "pack_expired:research.minimal" in expiry[0]
    assert not any("goes stale" in warning for warning in early["warnings"]), (
        "staleness is a different, later, milder thing and must not fire this early"
    )
    # And it is a threshold, not a permanent banner: five days earlier is outside the window and
    # says nothing, so the warning still means "soon" when it appears.
    earlier = cli.run_doctor(paths, today=date(2027, 4, 20), now=_NOW)
    assert not [warning for warning in earlier["warnings"] if "expires in" in warning]


def test_doctor_reports_each_packs_currency_and_stays_healthy_past_expiry(tmp_path: Path) -> None:
    """`registry: ok` is no longer the same question as every pack in it still being able to speak.

    Past every bundled expiry the registry still loads, so a report that said only `ok` would show
    nothing at all on the day three domains stopped being routed. The per-pack currency is what
    connects the two."""
    paths = _paths(tmp_path)

    report = cli.run_doctor(paths, today=date(2027, 8, 1), now=_NOW)

    assert report["registry"]["status"] == "ok"
    assert report["registry"]["pack_currency"] == {
        "programming.minimal": "expired", "project.memory": "expired", "research.minimal": "expired",
    }
    assert cli.run_doctor(paths, today=_TODAY, now=_NOW)["registry"]["pack_currency"] == {
        "programming.minimal": "current", "project.memory": "current", "research.minimal": "current",
    }


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
    exit_code = cli.bruriah_main([
        "index", "--config-dir", str(config_dir), "--data-dir", str(data_dir),
        "--corpus-root", str(missing_root), "--policy", str(policy_path),
    ])
    assert exit_code == 1
    assert "corpus_root_not_found" in capsys.readouterr().err

    config_dir.mkdir()
    (config_dir / "config.json").write_bytes(b"not json")
    exit_code = cli.bruriah_main(["doctor", "--config-dir", str(config_dir)])
    assert exit_code == 1
    assert "invalid_config" in capsys.readouterr().err

    empty_config = tmp_path / "empty-config"
    exit_code = cli.bruriah_main([
        "serve", "--config-dir", str(empty_config), "--data-dir", str(tmp_path / "empty-data"),
    ])
    assert exit_code == 1  # never enters the blocking stdio loop on an uninitialized dir
    assert "index_not_built" in capsys.readouterr().err

    bad_policy = tmp_path / "bad.yaml"
    bad_policy.write_text("include: [unclosed\n:::", encoding="utf-8")  # exists but malformed YAML
    exit_code = cli.bruriah_main([
        "index", "--config-dir", str(tmp_path / "c2"), "--data-dir", str(tmp_path / "d2"),
        "--corpus-root", str(root), "--policy", str(bad_policy),
    ])
    assert exit_code == 1 and "index_failed" in capsys.readouterr().err  # typed, not a traceback

    # init's mkdir into a file-path (no per-site guard) must still exit typed via the backstop.
    file_seg = tmp_path / "afile"; file_seg.write_text("x", encoding="utf-8")
    exit_code = cli.bruriah_main(["init", "--config-dir", str(file_seg / "cfg"),
        "--data-dir", str(tmp_path / "d3"), "--cache-dir", str(tmp_path / "ca3"), "--log-dir", str(tmp_path / "l3")])
    assert exit_code == 1 and "bruriah: error:" in capsys.readouterr().err


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


def test_index_persists_absolute_paths_so_serve_survives_a_different_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indexing with RELATIVE `--corpus-root`/`--policy` must still produce a snapshot that opens
    from anywhere. The build descriptor is read back by `serve` in a separate process whose working
    directory is chosen by the MCP host, not by the user who built the index. When the relative
    string was persisted verbatim, `_validate_stored` could not re-read the policy from the new
    directory and the failure surfaced as `snapshot_unreadable:invalid_active_target` -- blaming an
    intact snapshot for a path that no longer resolved."""
    root, policy_path = _corpus(tmp_path)
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"

    monkeypatch.chdir(tmp_path)
    args = cli._build_cli_parser().parse_args([
        "index", "--config-dir", str(config_dir), "--data-dir", str(data_dir),
        "--corpus-root", str(root.relative_to(tmp_path)),
        "--policy", str(policy_path.relative_to(tmp_path)),
    ])
    assert not args.corpus_root.is_absolute() and not args.policy.is_absolute()
    assert cli._cmd_index(args, embedder_factory=_fake_embedder_factory) == 0

    descriptor = json.loads((data_dir / "build-config.json").read_text(encoding="utf-8"))
    assert Path(descriptor["root"]).is_absolute()
    assert Path(descriptor["policy_path"]).is_absolute()

    # The real assertion: open the promoted snapshot from somewhere the relative paths mean nothing.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    paths = resolve_paths(cli_config_dir=config_dir, cli_data_dir=data_dir)
    snapshot = open_snapshot(paths)
    try:
        assert snapshot.build_id
    finally:
        snapshot.database.close()


# ---------------------------------------------------------------------------
# Bugfix regression: `run_index` built every candidate from scratch. `build_candidate` has always
# accepted a `previous` to reuse rows from, but nothing ever handed it one, so a one-document edit
# re-embedded the entire corpus to arrive at byte-identical vectors. It now resolves the active
# pointer first.
# ---------------------------------------------------------------------------


def _counting_embedder_factory(
    *, fingerprint: str = _FINGERPRINT,
) -> tuple[cli.EmbedderFactory, list[str]]:
    """A fake embedder that records every passage it was asked to embed.

    The tally is the point. `reused_documents` is a number the build reports about itself, so a
    test that only reads it would still pass if reuse were counted but the embedding ran anyway --
    which is the entire cost this change exists to avoid. What proves work was skipped is that the
    embedder was never asked."""
    embedded: list[str] = []

    def factory(model_name: str) -> tuple[cli.Embedder, str, int]:
        def embed(texts: list[str]) -> list[bytes]:
            embedded.extend(texts)
            return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

        return embed, fingerprint, 3

    return factory, embedded


def _two_document_corpus(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    (root / "public").mkdir(parents=True)
    (root / "public" / "en.md").write_text(
        f"# Apple\nAn apple pie baking recipe passage with real corpus text.\n{_FILLER}\n",
        encoding="utf-8",
    )
    (root / "public" / "other.md").write_text(
        f"# Schema\nA python schema validation library passage with real corpus text.\n{_FILLER}\n",
        encoding="utf-8",
    )
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    return root, policy_path


def test_reindexing_an_unchanged_corpus_embeds_nothing(tmp_path: Path) -> None:
    """The second `index` over a corpus nobody touched must reuse every document and call the
    embedder zero times. Before this, it re-embedded the whole corpus every time."""
    root, policy_path = _two_document_corpus(tmp_path)
    paths = _paths(tmp_path)
    factory, embedded = _counting_embedder_factory()

    first = cli.run_index(paths, root, policy_path, model_name="test/minilm", embedder_factory=factory)
    assert first.documents == 2 and first.reused_documents == 0
    assert len(embedded) == first.passages

    embedded.clear()
    second = cli.run_index(paths, root, policy_path, model_name="test/minilm", embedder_factory=factory)

    assert second.documents == 2
    assert second.reused_documents == 2
    assert embedded == []


def test_editing_one_document_re_embeds_only_that_document(tmp_path: Path) -> None:
    """Reuse is decided per document, so an edit costs one document's passages -- not the corpus."""
    root, policy_path = _two_document_corpus(tmp_path)
    paths = _paths(tmp_path)
    factory, embedded = _counting_embedder_factory()

    cli.run_index(paths, root, policy_path, model_name="test/minilm", embedder_factory=factory)
    (root / "public" / "other.md").write_text(
        f"# Schema\nA rewritten python schema validation passage.\n{_FILLER}\n", encoding="utf-8",
    )
    embedded.clear()

    result = cli.run_index(paths, root, policy_path, model_name="test/minilm", embedder_factory=factory)

    assert result.documents == 2
    assert result.reused_documents == result.documents - 1
    assert embedded and all("rewritten" in text for text in embedded)


def test_a_different_embedding_model_reuses_nothing_through_the_cli(tmp_path: Path) -> None:
    """`_compatible` refuses a snapshot built under another embedding identity, and this is the
    path a user actually reaches it by: reindexing after changing the model. Reusing there would
    mix two vector spaces in one snapshot, which no later validation could detect."""
    root, policy_path = _two_document_corpus(tmp_path)
    paths = _paths(tmp_path)
    first_factory, _ = _counting_embedder_factory()
    cli.run_index(paths, root, policy_path, model_name="test/minilm", embedder_factory=first_factory)

    other_factory, embedded = _counting_embedder_factory(
        fingerprint=_FINGERPRINT.replace("snapshot-a", "snapshot-b")
    )
    result = cli.run_index(
        paths, root, policy_path, model_name="test/other", embedder_factory=other_factory,
    )

    assert result.reused_documents == 0
    assert len(embedded) == result.passages


def test_the_index_command_reports_how_much_it_reused(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Reuse is invisible in the result -- the snapshot is identical either way -- so the only way
    a user learns their reindex was cheap, or learns that a model change made it expensive, is if
    the command says so."""
    root, policy_path = _two_document_corpus(tmp_path)
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    argv = [
        "index", "--config-dir", str(config_dir), "--data-dir", str(data_dir),
        "--corpus-root", str(root), "--policy", str(policy_path),
    ]
    args = cli._build_cli_parser().parse_args(argv)

    assert cli._cmd_index(args, embedder_factory=_fake_embedder_factory) == 0
    assert json.loads(capsys.readouterr().out)["reused_documents"] == 0

    assert cli._cmd_index(args, embedder_factory=_fake_embedder_factory) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["reused_documents"] == 2
    assert "2 reused, 0 embedded" in captured.err


# ---------------------------------------------------------------------------
# Bugfix regression: `build_serve_deps` used to call `platform.load_deps(paths)` with no
# `embed_query`, so the deployed `bruriah serve` process always ran retrieval BM25-only
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


# --- skill lifecycle subcommands (Unit 9A.3) -----------------------------------------------------

from bruriah.cli import (  # noqa: E402
    run_skill_analyze, run_skill_approve, run_skill_ingest, run_skill_sign,
)
from test_skills import DIGEST as _SKILL_DIGEST  # noqa: E402
from test_skills import _pack as _skill_pack  # noqa: E402
from test_skills import _skill as _skill_entry  # noqa: E402

_FLAGGED = "Read the key at ~/.ssh/id_rsa before starting."


def _candidate_file(tmp_path: Path, payload: dict | None = None) -> Path:
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(payload if payload is not None else _skill_pack()))
    return path


def _cli_code(callable_, *args, **kwargs) -> str:
    with pytest.raises(cli.CliError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


def test_ingest_stores_a_candidate_and_reports_its_identity(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    result = run_skill_ingest(paths, _candidate_file(tmp_path))
    assert result["pack_id"] == "bruriah.skills" and result["version"] == "1.0.0"
    assert Path(result["path"]).is_file()
    assert result["digest"].startswith("sha256:")


def test_analyze_output_carries_its_own_limits(tmp_path: Path) -> None:
    """A report that travels without its disclaimer becomes a clearance. Copying the JSON must copy
    the caveat with it."""
    result = run_skill_analyze(_candidate_file(tmp_path))
    assert result["advisories"] == []
    assert "never that it is dangerous" in result["analysis_limits"]
    assert "never means it is safe" in result["analysis_limits"]


def test_analyze_output_has_no_verdict_key(tmp_path: Path) -> None:
    # The CLI must not reintroduce at the boundary the verdict the analysis layer refuses to express.
    result = run_skill_analyze(_candidate_file(tmp_path, _skill_pack(
        skills=[_skill_entry(summary=_FLAGGED)])))
    assert set(result) == {"digest", "pack_id", "version", "skill_ids", "advisories",
                           "analysis_limits"}
    assert set(result) & {"safe", "passed", "verdict", "risk", "severity", "score", "clean"} == set()
    assert result["advisories"][0]["id"] == "design.ui-review:mentions_credential_path"


def test_analyze_reports_findings_without_refusing(tmp_path: Path) -> None:
    result = run_skill_analyze(_candidate_file(tmp_path, _skill_pack(
        skills=[_skill_entry(summary=_FLAGGED)])))
    assert [item["code"] for item in result["advisories"]] == ["mentions_credential_path"]
    assert result["skill_ids"] == ["design.ui-review"]


def test_a_structurally_invalid_candidate_is_one_typed_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert _cli_code(run_skill_analyze, broken) == "candidate_rejected:malformed_pack"
    assert _cli_code(run_skill_ingest, _paths(tmp_path), broken) == "candidate_rejected:malformed_pack"


def test_approve_refuses_without_full_acknowledgment_through_the_cli(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    candidate = _candidate_file(tmp_path, _skill_pack(skills=[_skill_entry(summary=_FLAGGED)]))
    assert _cli_code(run_skill_approve, paths, candidate, []) == \
        "approval_refused:unacknowledged_advisories"


def test_approve_records_the_binding_when_every_finding_is_acknowledged(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    candidate = _candidate_file(tmp_path, _skill_pack(skills=[_skill_entry(summary=_FLAGGED)]))
    result = run_skill_approve(paths, candidate, ["design.ui-review:mentions_credential_path"],
                               today=date(2026, 7, 25))
    record = result["approved"][0]
    assert record["skill_id"] == "design.ui-review"
    assert record["body_digest"] == _SKILL_DIGEST
    assert record["approved_on"] == "2026-07-25"
    # Even the approval output restates what the analysis was not.
    assert "never means it is safe" in result["analysis_limits"]


def test_approve_feeds_the_map_the_skill_set_validates_against(tmp_path: Path) -> None:
    from bruriah.approvals import load_approvals

    paths = _paths(tmp_path)
    run_skill_approve(paths, _candidate_file(tmp_path), [], today=date(2026, 7, 25))
    assert load_approvals(paths.data_dir) == {"design.ui-review": _SKILL_DIGEST}


def test_sign_produces_a_manifest_that_verifies_through_the_real_loader(tmp_path: Path) -> None:
    from bruriah.signing import generate_key
    from bruriah.skills import load_skill_pack

    key = tmp_path / "release-key.pem"
    public = generate_key(key)
    pack = _candidate_file(tmp_path)
    result = run_skill_sign(key, "bruriah-release", pack, None)
    loaded = load_skill_pack(pack, Path(result["manifest"]), {"bruriah-release": public},
                             today=date(2026, 7, 25))
    assert loaded.pack_id == "bruriah.skills"
    assert "not a claim that they" in result["note"]


def test_signing_with_a_missing_key_is_one_typed_error(tmp_path: Path) -> None:
    assert _cli_code(run_skill_sign, tmp_path / "absent.pem", "s", _candidate_file(tmp_path), None) \
        == "signing_failed:key_unreadable"


@pytest.mark.parametrize(
    "argv",
    [
        ["skill-ingest", "--pack", "x.json"],
        ["skill-analyze", "--candidate", "x.json"],
        ["skill-approve", "--candidate", "x.json"],
        ["skill-sign", "--key", "k.pem", "--signer", "s", "--pack", "x.json"],
    ],
)
def test_every_subcommand_is_registered_and_fails_typed(tmp_path: Path, argv, capsys) -> None:
    # Exercised through the real argv path: the subcommand parses, and a missing file surfaces as one
    # typed line on stderr rather than a traceback.
    assert cli.bruriah_main([*argv, "--data-dir", str(tmp_path / "d"),
                                 "--config-dir", str(tmp_path / "c")]) == 1
    assert "bruriah: error:" in capsys.readouterr().err


# --- skill activation subcommands (Unit 9B) -------------------------------------------------------

from bruriah.cli import (  # noqa: E402
    run_skill_activate, run_skill_prune, run_skill_rollback, run_skill_status,
)

_T3 = dict(tier="local", domains=["programming"])


def _local_pack(tmp_path: Path, name: str, **overrides) -> Path:
    payload = _skill_pack(pack_id="local.skills", skills=[_skill_entry(**_T3, **overrides)])
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(payload))
    return path


def _approved(tmp_path: Path, paths, name: str = "pack", **overrides) -> Path:
    candidate = _local_pack(tmp_path, name, **overrides)
    run_skill_approve(paths, candidate, [], today=date(2026, 7, 25))
    return candidate


def _activate(paths, *candidates_: Path):
    return run_skill_activate(paths, list(candidates_), allow_unsigned_local=True,
                              today=date(2026, 7, 25))


def test_the_whole_lifecycle_runs_from_the_cli(tmp_path: Path) -> None:
    """Approve, activate, inspect -- without touching the Python API."""
    paths = _paths(tmp_path)
    result = _activate(paths, _approved(tmp_path, paths))
    assert result["skills"] == ["design.ui-review"]
    assert len(result["build_id"]) == 64
    status = run_skill_status(paths, today=date(2026, 7, 25))
    assert status["active"] == result["build_id"] and status["warning"] is None


def test_activation_refuses_an_unapproved_candidate(tmp_path: Path) -> None:
    # The gate holds through the CLI too: approval is not implied by naming a candidate.
    paths = _paths(tmp_path)
    assert _cli_code(run_skill_activate, paths, [_local_pack(tmp_path, "pack")],
                     allow_unsigned_local=True, today=date(2026, 7, 25)) == \
        "activation_refused:skill_not_approved"


def test_an_unsigned_pack_needs_the_explicit_flag(tmp_path: Path) -> None:
    # The T3 exception has to be typed out. Defaulting it on would make the unsigned path the easy
    # one, which is the opposite of what an exception should feel like.
    paths = _paths(tmp_path)
    candidate = _approved(tmp_path, paths)
    assert _cli_code(run_skill_activate, paths, [candidate], today=date(2026, 7, 25)) == \
        "activation_refused:signature_required"


def test_naming_no_candidate_is_refused_rather_than_activating_everything(tmp_path: Path) -> None:
    # Approval says "I read this"; activation says "this goes into service". An empty invocation must
    # not be read as "activate everything approved" -- that would collapse the two.
    assert _cli_code(run_skill_activate, _paths(tmp_path), []) == "no_candidates_named"


def test_rollback_restores_the_previous_generation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _activate(paths, _approved(tmp_path, paths, "one"))
    second = _activate(paths, _approved(tmp_path, paths, "two", summary="A second revision."))
    assert second["build_id"] != first["build_id"]
    restored = run_skill_rollback(paths, today=date(2026, 7, 25))
    assert restored["build_id"] == first["build_id"]


def test_rollback_without_history_is_one_typed_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _activate(paths, _approved(tmp_path, paths))
    assert _cli_code(run_skill_rollback, paths, today=date(2026, 7, 25)) == \
        "rollback_refused:no_retained_skillset"


def test_status_never_raises_on_a_broken_pointer(tmp_path: Path) -> None:
    """Status is the command an operator runs precisely when something is wrong, so it must survive
    exactly the state it exists to report."""
    paths = _paths(tmp_path)
    _activate(paths, _approved(tmp_path, paths))
    (paths.data_dir / "skills" / "active.json").write_text("{not json")
    status = run_skill_status(paths, today=date(2026, 7, 25))
    assert status["active"] is None
    assert status["warning"] == "skillset_unreadable:invalid_active_pointer"
    assert status["inventory_unavailable"] == "invalid_active_pointer"


def test_status_on_a_fresh_install_reports_inactive_not_broken(tmp_path: Path) -> None:
    status = run_skill_status(_paths(tmp_path), today=date(2026, 7, 25))
    assert status == {"active": None, "skills": [], "warning": None,
                      "retained": [], "unreferenced": []}


def test_prune_removes_only_unreferenced_generations(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    for index in range(4):
        _activate(paths, _approved(tmp_path, paths, f"p{index}", summary=f"Revision {index}."))
    status = run_skill_status(paths, today=date(2026, 7, 25))
    assert len(status["unreferenced"]) == 1
    removed = run_skill_prune(paths)
    assert removed["removed"] == status["unreferenced"]
    assert run_skill_status(paths, today=date(2026, 7, 25))["active"] is not None


def test_prune_refuses_without_a_readable_pointer(tmp_path: Path) -> None:
    assert _cli_code(run_skill_prune, _paths(tmp_path)) == "prune_refused:no_active_skillset"


@pytest.mark.parametrize(
    "argv",
    [["skill-activate", "--candidate", "x.json"], ["skill-rollback"],
     ["skill-status"], ["skill-prune"]],
)
def test_every_activation_subcommand_is_registered(tmp_path: Path, argv, capsys) -> None:
    code = cli.bruriah_main([*argv, "--data-dir", str(tmp_path / "d"),
                                 "--config-dir", str(tmp_path / "c")])
    # skill-status succeeds on a fresh install by design; the rest fail typed rather than traceback.
    assert code == (0 if argv[0] == "skill-status" else 1)
    if code == 1:
        assert "bruriah: error:" in capsys.readouterr().err


def test_it_reports_its_own_version_without_a_subcommand(capsys) -> None:
    """`--version` has to answer before `required=True` on the subcommand can reject the call.

    `docs/client-guidance.md` named this flag as the way to find out which router version a config
    targets while the flag did not exist, and the doc asserted `0.1.0` for three releases. Both the
    flag and the single source it reads from are pinned here: the number comes from
    `bruriah.__version__` rather than being restated, so a bump cannot make this test agree with a
    stale string.
    """
    with pytest.raises(SystemExit) as exit_info:
        cli.bruriah_main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"bruriah {bruriah_version}"


def test_pack_compatibility_is_checked_against_the_version_this_actually_is(tmp_path: Path) -> None:
    """The router-version default is the installed version, not the `0.1.0` it was hardcoded to.

    `check_router_compatibility` gates a pack on `min_router_version <= router <= max_router`. With
    the default frozen at `0.1.0`, a pack requiring the CURRENT version was rejected by a router
    that IS the current version -- the gate was not dormant, it was answering about a release the
    package had not been since its first. Asserting the default equals `__version__` would restate
    the assignment; this asserts the BEHAVIOUR the assignment exists for.
    """
    packs.check_router_compatibility(bruriah_version, bruriah_version, bruriah_version)
    with pytest.raises(packs.PackError):
        packs.check_router_compatibility("0.0.1", "0.0.2", bruriah_version)


# --- `--reranker`: opt-in, never loaded unless named ------------------------------------------


def test_build_serve_deps_leaves_the_reranker_dormant_unless_an_operator_names_one(
    tmp_path: Path,
) -> None:
    """The stage is off by default and that default must be structural, not documentation.

    A reranker is a second ~1 GB model download and a cross-encoder pass per candidate document.
    Every caller that never passed `--reranker` -- `doctor`, the MCP server, the eval adapters --
    must get deps that neither construct nor consult one."""
    paths = _index_with_fake_embedder(tmp_path)

    def never(model_name: str):
        raise AssertionError(f"a reranker was constructed without being asked for: {model_name}")

    deps = cli.build_serve_deps(
        paths, embedder_factory=_fake_embedder_factory, reranker_factory=never,
    )
    try:
        assert deps.rerank is None
    finally:
        deps.snapshot.database.close()


def test_build_serve_deps_threads_a_named_reranker_through_to_the_search_stage(
    tmp_path: Path,
) -> None:
    paths = _index_with_fake_embedder(tmp_path)
    built: list[str] = []

    def factory(model_name: str):
        built.append(model_name)
        return lambda query, documents: [0.0] * len(documents)

    deps = cli.build_serve_deps(
        paths, embedder_factory=_fake_embedder_factory,
        reranker_model="example/reranker", reranker_factory=factory,
    )
    try:
        assert built == ["example/reranker"]
        assert deps.rerank is not None
        outcome = router_search(
            deps.snapshot, _TASK, embed_query=deps.embed_query, rerank=deps.rerank,
        )
        assert any(note.startswith("reranked:") for note in outcome.degradation)
    finally:
        deps.snapshot.database.close()


def test_build_serve_deps_default_reranker_factory_is_the_real_fastembed_one() -> None:
    """Same guard the embedder factory carries: the production path must never silently hold a
    fake, or `--reranker` would be accepted and quietly do nothing."""
    import inspect

    default = inspect.signature(cli.build_serve_deps).parameters["reranker_factory"].default
    assert default is cli._default_reranker_factory


def test_the_reranker_is_not_read_from_the_build_descriptor_like_the_embedder_is() -> None:
    """The asymmetry is deliberate and worth pinning, because it looks like an omission.

    Passage vectors are baked into the snapshot, so a query embedder that does not match what
    built them searches the wrong space -- hence the fail-closed fingerprint check. A reranker
    reads text and writes nothing, so no snapshot can be wrong about it and none records it."""
    import dataclasses

    from bruriah.index import BuildConfig

    assert not any("rerank" in field.name for field in dataclasses.fields(BuildConfig)), (
        "a reranker was recorded in the build descriptor; then swapping one would need a re-index"
    )


# --- `init --repo`: the quickstart in one command -------------------------------------------
# The measured first-run path was ~90 seconds of machine work and four commands of reading
# documentation first. These pin the one-shot's composition, not new retrieval behaviour:
# every step it takes is a documented command's own function.


def _decision_repo(tmp_path: Path) -> Path:
    """Mirrors test_gitcorpus._repo: two non-merge commits, exactly one carrying reasoning."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-q", "-m", "feat: add the thing\n\nBecause the other way needed two round trips.")
    (repo / "b.txt").write_text("two")
    run("add", "-A")
    run("commit", "-q", "-m", "chore: tidy")  # no body: records what changed, never why
    return repo


def _init_repo_args(tmp_path: Path, repo: Path) -> "list[str]":
    return [
        "init", "--repo", str(repo),
        "--config-dir", str(tmp_path / "config"), "--data-dir", str(tmp_path / "data"),
        "--cache-dir", str(tmp_path / "cache"), "--log-dir", str(tmp_path / "log"),
    ]


def test_init_repo_bootstraps_policy_corpus_index_and_clients_in_one_command(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    repo = _decision_repo(tmp_path)
    args = cli._build_cli_parser().parse_args(_init_repo_args(tmp_path, repo))
    assert cli._cmd_init(args, embedder_factory=_fake_embedder_factory) == 0
    captured = capsys.readouterr()

    # Every artifact of the spelled-out quickstart, from one command, in the directories the
    # tool already owns -- no new location was invented.
    assert (tmp_path / "config" / "policy.yaml").read_text(
        encoding="utf-8") == cli._DEFAULT_BOOTSTRAP_POLICY
    assert len(list((tmp_path / "data" / "corpus").glob("*.md"))) == 1
    assert (tmp_path / "data" / "active.json").is_file()
    assert (tmp_path / "config" / "clients").is_dir()

    # The suggested first question is the newest decision's own subject, so the first `ask`
    # cannot come back empty -- and the paste carries ALL the directory flags this run used:
    # dropping --cache-dir would re-download the model this run just cached, and dropping
    # --data-dir would ask the right question of the wrong index.
    assert 'bruriah ask "why feat add the thing"' in captured.err
    assert f'--data-dir "{tmp_path / "data"}"' in captured.err
    assert f'--cache-dir "{tmp_path / "cache"}"' in captured.err
    assert f'--log-dir "{tmp_path / "log"}"' in captured.err
    # stdout stays what plain `init` prints: the parseable client snippet, nothing else.
    snippet = json.loads(captured.out)
    assert snippet["args"][:2] == ["-m", "bruriah.cli"]


def test_init_repo_refuses_a_directory_that_is_not_a_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    exit_code = cli.bruriah_main(_init_repo_args(tmp_path, plain))
    assert exit_code == 1
    assert "not_a_git_repository" in capsys.readouterr().err


def test_init_repo_with_a_history_that_never_explains_stops_before_indexing(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """An index of zero documents can only return nothing; building it would dress that up as a
    completed setup, and writing client configs would point a client at it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *args: subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (repo / "a.txt").write_text("one")
    run("add", "-A")
    run("commit", "-q", "-m", "chore: tidy")  # subject only: what changed, never why
    exit_code = cli.bruriah_main(_init_repo_args(tmp_path, repo))
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "corpus_has_no_reasoning" in captured.err
    assert "No commit carried an explanatory body" in captured.err
    assert not (tmp_path / "data" / "active.json").exists()
    assert not (tmp_path / "config" / "clients").exists()


def test_init_repo_never_touches_an_existing_policy(tmp_path: Path) -> None:
    repo = _decision_repo(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    theirs = "version: 1\ninclude: ['**']\nexclude: ['secrets/**']\n"
    (config_dir / "policy.yaml").write_text(theirs, encoding="utf-8", newline="\n")
    args = cli._build_cli_parser().parse_args(_init_repo_args(tmp_path, repo))
    assert cli._cmd_init(args, embedder_factory=_fake_embedder_factory) == 0
    assert (config_dir / "policy.yaml").read_text(encoding="utf-8") == theirs


def test_the_model_cache_is_pinned_under_the_private_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fastembed's own default cache is the OS temp directory, which macOS purges on its own
    schedule -- so "the model downloads once" held only until the OS decided otherwise. Every
    command resolves paths before any factory constructs a model, and the resolver pins the
    cache under the tool's private `cache_dir` (`cache.py` only ever touches top-level `*.json`
    there, so the subdirectory sits outside its deletion control)."""
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", "placeholder")  # registers the restore
    monkeypatch.delenv("FASTEMBED_CACHE_PATH")
    args = cli._build_cli_parser().parse_args([
        "doctor", "--config-dir", str(tmp_path / "config"), "--data-dir", str(tmp_path / "data"),
        "--cache-dir", str(tmp_path / "cache"), "--log-dir", str(tmp_path / "log"),
    ])
    cli._resolve_paths(args)
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(tmp_path / "cache" / "models")


def test_an_operator_pinned_model_cache_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FASTEMBED_CACHE_PATH is fastembed's documented knob; an operator who set it keeps it."""
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "operator-cache"))
    args = cli._build_cli_parser().parse_args([
        "doctor", "--config-dir", str(tmp_path / "config"), "--data-dir", str(tmp_path / "data"),
        "--cache-dir", str(tmp_path / "cache"), "--log-dir", str(tmp_path / "log"),
    ])
    cli._resolve_paths(args)
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(tmp_path / "operator-cache")


def test_resolve_model_prefixes_known_and_overrides() -> None:
    from bruriah._cli.common import resolve_model_prefixes

    # Known e5 models default to ("query: ", "passage: ")
    assert resolve_model_prefixes("intfloat/multilingual-e5-large") == ("query: ", "passage: ")
    assert resolve_model_prefixes("intfloat/e5-base-v2") == ("query: ", "passage: ")

    # Known BGE models default to query prompt
    q_p, p_p = resolve_model_prefixes("BAAI/bge-base-en")
    assert q_p == "Represent this sentence for searching relevant passages: "
    assert p_p == ""

    # Symmetric default
    assert resolve_model_prefixes("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") == ("", "")
    assert resolve_model_prefixes("custom/unknown-model") == ("", "")

    # Explicit overrides take precedence
    assert resolve_model_prefixes("intfloat/multilingual-e5-large", query_prefix="q: ") == ("q: ", "passage: ")
    assert resolve_model_prefixes("intfloat/multilingual-e5-large", passage_prefix="doc: ") == ("query: ", "doc: ")
    assert resolve_model_prefixes("custom/model", query_prefix="q: ", passage_prefix="p: ") == ("q: ", "p: ")


def test_build_serve_deps_applies_query_prefix(tmp_path: Path) -> None:
    root, policy_path = tmp_path / "root", tmp_path / "policy.yaml"
    root.mkdir()
    (root / "doc.md").write_text("# Title\nPassage text.\n", encoding="utf-8")
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")

    data_dir = tmp_path / "data"
    paths = cli.PlatformPaths(
        config_dir=tmp_path / "cfg", data_dir=data_dir, cache_dir=tmp_path / "cache", log_dir=tmp_path / "log",
    )
    cli.run_index(
        paths, root, policy_path, model_name="intfloat/multilingual-e5-large",
        embedder_factory=_fake_embedder_factory,
        query_prefix="query: ", passage_prefix="passage: ",
    )

    embedded_queries: list[str] = []

    def recording_embedder_factory(model_name: str):
        embed, fp, dim = _fake_embedder_factory(model_name)

        def recording_embed(texts: list[str]) -> list[bytes]:
            embedded_queries.extend(texts)
            return embed(texts)

        return recording_embed, fp, dim

    deps = cli.build_serve_deps(paths, embedder_factory=recording_embedder_factory)
    assert deps.embed_query is not None
    deps.embed_query("why fastmcp")
    assert embedded_queries == ["query: why fastmcp"]
    deps.snapshot.database.close()


def test_cli_parser_accepts_query_and_passage_prefix_flags(tmp_path: Path) -> None:
    parser = cli._build_cli_parser()
    args_index = parser.parse_args([
        "index", "--corpus-root", str(tmp_path), "--policy", str(tmp_path / "policy.yaml"),
        "--model", "intfloat/multilingual-e5-large",
        "--query-prefix", "ask: ", "--passage-prefix", "doc: ",
    ])
    assert args_index.query_prefix == "ask: "
    assert args_index.passage_prefix == "doc: "

    args_init = parser.parse_args([
        "init", "--repo", str(tmp_path),
        "--model", "intfloat/multilingual-e5-large",
        "--query-prefix", "q: ", "--passage-prefix", "p: ",
    ])
    assert args_init.query_prefix == "q: "
    assert args_init.passage_prefix == "p: "


def test_cli_parser_why_subcommand(tmp_path: Path) -> None:
    parser = cli._build_cli_parser()
    args = parser.parse_args(["why", "src/core/storage.py:42", "--repo", str(tmp_path), "--json"])
    assert args.command == "why"
    assert args.target == "src/core/storage.py:42"
    assert args.repo == tmp_path
    assert args.json is True


def test_cli_why_not_a_git_repository(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    paths = _paths(tmp_path)
    exit_code = cli.bruriah_main([
        "why", "file.py:1",
        "--repo", str(non_git),
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
    ])
    assert exit_code == 1
    assert "not_a_git_repository" in capsys.readouterr().err


def test_cli_why_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    # 1. Set up a real git repo with a commit
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Author"], cwd=repo, check=True)

    (repo / "storage.py").write_text("def connect():\n    return 'db'\n", encoding="utf-8")
    subprocess.run(["git", "add", "storage.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feat: initial storage"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # 2. Build corpus and index containing this decision
    root = tmp_path / "vault"
    root.mkdir()
    doc_content = f"""---
commit: {sha}
---

# SQLite Storage Implementation

**Decided:** 2026-03-01 · **Commit:** `{sha[:12]}` · **Author:** Test Author

Decided to use raw sqlite3 connection pooling.

## Files this decision touched
- `storage.py`
"""
    (root / "decisions.md").write_text(doc_content, encoding="utf-8")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")

    paths = _paths(tmp_path)
    cli.run_init(paths)
    cli.run_index(
        paths, root, policy_path, model_name="test/minilm", embedder_factory=_fake_embedder_factory,
    )

    # 3. Run why command human-readable
    capsys.readouterr()  # flush
    exit_code = cli.bruriah_main([
        "why", "storage.py:1",
        "--repo", str(repo),
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Target: storage.py:1" in captured.out
    assert "Line Commit:" in captured.out
    assert "feat: initial storage" in captured.out
    assert "Governing Architectural Decision:" in captured.out
    assert "SQLite Storage Implementation" in captured.out
    assert "Decided to use raw sqlite3 connection pooling" in captured.out

    # 4. Run why command with --json
    exit_code = cli.bruriah_main([
        "why", "storage.py:1",
        "--repo", str(repo),
        "--json",
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
    ])
    assert exit_code == 0
    captured_json = capsys.readouterr().out
    data = json.loads(captured_json)
    assert data["target"] == "storage.py:1"
    assert data["line"] == 1
    assert data["line_commit"]["subject"] == "feat: initial storage"
    assert data["governing_decision"]["subject"] == "SQLite Storage Implementation"

    # 5. Run with line out of range
    exit_code = cli.bruriah_main([
        "why", "storage.py:999",
        "--repo", str(repo),
        "--config-dir", str(paths.config_dir),
        "--data-dir", str(paths.data_dir),
        "--cache-dir", str(paths.cache_dir),
        "--log-dir", str(paths.log_dir),
    ])
    assert exit_code == 1
    assert "line_out_of_range" in capsys.readouterr().err


def test_cli_corpus_pdf_single_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from test_pdfcorpus import _make_pdf

    pdf_path = _make_pdf(
        tmp_path / "paper.pdf",
        ["Page 1 architectural reasoning.", "Page 2 benchmark evaluation."],
    )
    out_dir = tmp_path / "derived_corpus"

    exit_code = cli.bruriah_main(["corpus", "--pdf", str(pdf_path), "--out", str(out_dir)])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["documents"] == 2
    assert data["pages_examined"] == 2
    assert data["empty_pages_skipped"] == 0
    assert data["files_examined"] == 1
    assert data["out"] == str(out_dir)


def test_cli_corpus_pdf_coverage_reporting_on_skipped_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_pdfcorpus import _make_pdf

    pdf_path = _make_pdf(tmp_path / "mixed.pdf", ["Only page with text.", ""])
    out_dir = tmp_path / "out"

    exit_code = cli.bruriah_main(["corpus", "--pdf", str(pdf_path), "--out", str(out_dir)])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["documents"] == 1
    assert data["pages_examined"] == 2
    assert data["empty_pages_skipped"] == 1
    assert "1 of 2 pages across 1 PDF file(s) contained extractable text" in captured.err
    assert "were empty or image-only and were skipped" in captured.err


def test_cli_corpus_pdf_no_extractable_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_pdfcorpus import _make_pdf

    pdf_path = _make_pdf(tmp_path / "empty_doc.pdf", ["", "   "])
    out_dir = tmp_path / "out"

    exit_code = cli.bruriah_main(["corpus", "--pdf", str(pdf_path), "--out", str(out_dir)])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["documents"] == 0
    assert data["empty_pages_skipped"] == 2
    assert "No PDF page contained extractable text." in captured.err


def test_cli_corpus_cannot_specify_both_repo_and_pdf(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from test_pdfcorpus import _make_pdf

    pdf_path = _make_pdf(tmp_path / "doc.pdf", ["Some text"])
    out_dir = tmp_path / "out"

    exit_code = cli.bruriah_main([
        "corpus", "--repo", ".", "--pdf", str(pdf_path), "--out", str(out_dir)
    ])
    assert exit_code == 1
    assert "cannot_specify_both_repo_and_pdf" in capsys.readouterr().err


def test_init_repo_zero_config_scopes_to_user_data_and_writes_pointer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user_data"
    user_config = tmp_path / "user_config"
    monkeypatch.delenv("BRURIAH_DATA_DIR", raising=False)
    monkeypatch.delenv("BRURIAH_CONFIG_DIR", raising=False)
    monkeypatch.setattr("platformdirs.user_data_dir", lambda _: str(user_data))
    monkeypatch.setattr("platformdirs.user_config_dir", lambda _: str(user_config))

    repo = _decision_repo(tmp_path)
    args = cli._build_cli_parser().parse_args(["init", "--repo", str(repo)])
    assert cli._cmd_init(args, embedder_factory=_fake_embedder_factory) == 0

    config_file = repo / ".bruriah" / "config.json"
    assert config_file.is_file()
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    assert "data_dir" in cfg
    assert "config_dir" in cfg

    scoped_data = Path(cfg["data_dir"])
    assert (scoped_data / "active.json").is_file()

    ask_args = cli._build_cli_parser().parse_args(["ask", "why feat add the thing", "--repo", str(repo)])
    assert cli._cmd_ask(ask_args, embedder_factory=_fake_embedder_factory) == 0


def test_init_repo_local_stores_in_dot_bruriah(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _decision_repo(tmp_path)
    args = cli._build_cli_parser().parse_args(["init", "--repo", str(repo), "--local"])
    assert cli._cmd_init(args, embedder_factory=_fake_embedder_factory) == 0

    config_file = repo / ".bruriah" / "config.json"
    assert config_file.is_file()
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    assert cfg["data_dir"] == "data"
    assert cfg["config_dir"] == "config"

    assert (repo / ".bruriah" / "data" / "active.json").is_file()

    ask_args = cli._build_cli_parser().parse_args(["ask", "why feat add the thing", "--repo", str(repo)])
    assert cli._cmd_ask(ask_args, embedder_factory=_fake_embedder_factory) == 0


def test_cmd_setup_writes_client_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    exit_code = cli.bruriah_main(["setup", "cursor", "--repo", str(repo), "--data-dir", str(tmp_path / "data")])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[cursor] Created and registered" in captured.err

    cursor_mcp = repo / ".cursor" / "mcp.json"
    assert cursor_mcp.is_file()
    cfg = json.loads(cursor_mcp.read_text(encoding="utf-8"))
    assert "mcpServers" in cfg
    assert "bruriah" in cfg["mcpServers"]

    exit_code = cli.bruriah_main(["setup", "cursor", "--repo", str(repo), "--data-dir", str(tmp_path / "data")])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[cursor] Up to date" in captured.err


def test_cmd_setup_dry_run_prints_preview(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    exit_code = cli.bruriah_main(["setup", "cursor", "--repo", str(repo), "--dry-run"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "[dry-run] MCP Client Configuration Preview:" in captured.err
    assert "[cursor] Would create:" in captured.err
    assert not (repo / ".cursor" / "mcp.json").exists()


def test_cmd_hook_install_and_uninstall(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    # Install hook
    exit_code = cli.bruriah_main(["hook", "install", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Installed pre-commit hook to" in captured.err
    hook_file = repo / ".git" / "hooks" / "pre-commit"
    assert hook_file.is_file()

    # Install again: unchanged
    exit_code = cli.bruriah_main(["hook", "install", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Pre-commit hook is already up to date" in captured.err

    # Uninstall hook
    exit_code = cli.bruriah_main(["hook", "uninstall", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Removed pre-commit hook from" in captured.err
    assert not hook_file.exists()


def test_cmd_hook_refuses_non_git_repo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    exit_code = cli.bruriah_main(["hook", "install", "--repo", str(plain)])
    assert exit_code == 1
    assert "hook_failed:not_a_git_repository" in capsys.readouterr().err


def test_cmd_alias_install_and_uninstall_local(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = tmp_path / "my-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # Install local git aliases
    exit_code = cli.bruriah_main(["alias", "install", "--local", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "git why -> !bruriah why (Configured, local)" in captured.err
    assert "git drift -> !bruriah drift (Configured, local)" in captured.err

    # Install again: unchanged
    exit_code = cli.bruriah_main(["alias", "install", "--local", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "git why -> !bruriah why (Already configured, local)" in captured.err

    # Uninstall
    exit_code = cli.bruriah_main(["alias", "uninstall", "--local", "--repo", str(repo)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "git why (Removed, local)" in captured.err
    assert "git drift (Removed, local)" in captured.err


# --- is_symmetric_model and expanded model registry -------------------------------------------


def test_is_symmetric_model_true_for_minilm() -> None:
    """The default MiniLM model is symmetric-similarity-oriented and must be flagged."""
    from bruriah._cli.common import is_symmetric_model

    assert is_symmetric_model("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2") is True


def test_is_symmetric_model_false_for_e5_large() -> None:
    """E5-large is an asymmetric retrieval model and must not trigger the warning."""
    from bruriah._cli.common import is_symmetric_model

    assert is_symmetric_model("intfloat/multilingual-e5-large") is False


def test_is_symmetric_model_case_insensitive() -> None:
    """Pattern matching is lowercased so capitalisation variants don't escape detection."""
    from bruriah._cli.common import is_symmetric_model

    assert is_symmetric_model("sentence-transformers/all-MiniLM-L6-v2") is True
    assert is_symmetric_model("sentence-transformers/all-mpnet-base-v2") is True
    assert is_symmetric_model("sentence-transformers/distiluse-base-multilingual") is True


def test_known_model_prefixes_nomic_embed_text() -> None:
    """Nomic embed-text uses search_query/search_document task-type prefixes."""
    from bruriah._cli.common import resolve_model_prefixes

    assert resolve_model_prefixes("nomic-ai/nomic-embed-text-v1.5") == (
        "search_query: ", "search_document: "
    )
    assert resolve_model_prefixes("nomic-ai/nomic-embed-text-v1") == (
        "search_query: ", "search_document: "
    )


def test_known_model_prefixes_bge_m3_no_prefix() -> None:
    """BGE-M3 handles asymmetric retrieval internally and must not receive E5-style prefixes."""
    from bruriah._cli.common import resolve_model_prefixes

    assert resolve_model_prefixes("BAAI/bge-m3") == ("", "")


def test_known_model_prefixes_jina_v3_no_prefix() -> None:
    """Jina v3 uses no prefix and must not be caught by the e5 heuristic fallback."""
    from bruriah._cli.common import resolve_model_prefixes

    assert resolve_model_prefixes("jinaai/jina-embeddings-v3") == ("", "")


# --- default embedding model: one constant, complete bge/jina registry entries ------------------
# The 2026-09-20 embedder ablation (evals/project-memory/README.md) showed
# `jinaai/jina-embeddings-v2-base-es` improving recall@3 on both external corpora and on this
# project's own bilingual history, while the bge candidates regressed Spanish below the old
# default. It also exposed a registry gap: `KNOWN_MODEL_PREFIXES` listed `BAAI/bge-*-en` but not
# the `-v1.5` names actually used in the ablation, so bge ran without its instruction prefix
# unless one was passed explicitly.


def test_default_embedding_model_is_the_ablation_winner() -> None:
    from bruriah._cli.common import DEFAULT_EMBEDDING_MODEL

    assert DEFAULT_EMBEDDING_MODEL == "jinaai/jina-embeddings-v2-base-es"


def test_is_symmetric_model_false_for_the_new_default() -> None:
    """The new default is an asymmetric retrieval model, not a symmetric-similarity one, so it
    must never trigger `bruriah index`'s symmetric-model warning."""
    from bruriah._cli.common import DEFAULT_EMBEDDING_MODEL, is_symmetric_model

    assert is_symmetric_model(DEFAULT_EMBEDDING_MODEL) is False


def test_init_index_watch_parsers_share_the_default_embedding_model_constant() -> None:
    """`init`, `index` and `watch` must all read their `--model` default from the SAME
    `DEFAULT_EMBEDDING_MODEL` constant, so the three cannot silently drift apart again."""
    from bruriah._cli.common import DEFAULT_EMBEDDING_MODEL

    parser = cli._build_cli_parser()
    init_args = parser.parse_args(["init"])
    index_args = parser.parse_args(["index", "--corpus-root", "x", "--policy", "y"])
    watch_args = parser.parse_args(["watch"])
    assert init_args.model == DEFAULT_EMBEDDING_MODEL
    assert index_args.model == DEFAULT_EMBEDDING_MODEL
    assert watch_args.model == DEFAULT_EMBEDDING_MODEL


def test_resolve_model_prefixes_bge_v1_5_variants_get_the_instruction_prefix() -> None:
    """`BAAI/bge-*-en-v1.5` -- the actual model names the ablation indexed with -- must resolve
    the same instruction prefix as the unversioned `BAAI/bge-*-en` entries already did."""
    from bruriah._cli.common import resolve_model_prefixes

    prefix = "Represent this sentence for searching relevant passages: "
    assert resolve_model_prefixes("BAAI/bge-small-en-v1.5") == (prefix, "")
    assert resolve_model_prefixes("BAAI/bge-base-en-v1.5") == (prefix, "")
    assert resolve_model_prefixes("BAAI/bge-large-en-v1.5") == (prefix, "")


def test_resolve_model_prefixes_jina_v2_base_es_no_prefix() -> None:
    """The new default and its sibling v2 models use no prefix, same as jina v3 above -- and must
    not be caught by the `elif "e5" in model_name` fallback (they aren't, but the registry entry
    makes the "no prefix" choice explicit rather than an accident of that heuristic)."""
    from bruriah._cli.common import resolve_model_prefixes

    assert resolve_model_prefixes("jinaai/jina-embeddings-v2-base-es") == ("", "")
    assert resolve_model_prefixes("jinaai/jina-embeddings-v2-base-en") == ("", "")
    assert resolve_model_prefixes("jinaai/jina-embeddings-v2-small-en") == ("", "")
