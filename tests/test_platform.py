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

from conftest import requires_legacy_database
from bruriah.corpus import CorpusPolicy
from bruriah.index import BuildConfig, build_candidate, promote_candidate
from bruriah.mcp_server import build_server
from bruriah.dispatch import DEFAULT_SKILL_CEILING
from bruriah.platform import (
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
        cli_config_dir=config_dir, env={"BRURIAH_DATA_DIR": str(env_dir)}
    ).data_dir == env_dir
    assert resolve_paths(
        cli_config_dir=config_dir, cli_data_dir=cli_dir,
        env={"BRURIAH_DATA_DIR": str(env_dir)},
    ).data_dir == cli_dir


def test_default_paths_are_never_auto_created(tmp_path: Path) -> None:
    import platformdirs

    real_data_dir = Path(platformdirs.user_data_dir("bruriah"))
    existed_before = real_data_dir.exists()
    paths = resolve_paths(env={})
    assert paths.data_dir == real_data_dir
    assert real_data_dir.exists() == existed_before


def test_network_defaults_off_unless_explicitly_enabled(tmp_path: Path) -> None:
    assert resolve_paths(env={}).network_enabled is False
    assert resolve_paths(env={"BRURIAH_NETWORK_ENABLED": "true"}).network_enabled is True
    assert resolve_paths(cli_network_enabled=True, env={}).network_enabled is True


def test_skill_ceiling_follows_the_same_precedence_as_every_other_setting(tmp_path: Path) -> None:
    """Six first-party skills ship and the default admits five, so the operator needs a way to
    raise it. It resolves like everything else -- CLI over env over config file over default --
    rather than growing its own private mechanism."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"skill_ceiling": 6}), encoding="utf-8")

    assert resolve_paths(env={}).skill_ceiling == DEFAULT_SKILL_CEILING
    assert resolve_paths(cli_config_dir=config_dir, env={}).skill_ceiling == 6
    assert resolve_paths(
        cli_config_dir=config_dir, env={"BRURIAH_SKILL_CEILING": "7"}
    ).skill_ceiling == 7
    assert resolve_paths(
        cli_config_dir=config_dir, cli_skill_ceiling=9, env={"BRURIAH_SKILL_CEILING": "7"}
    ).skill_ceiling == 9
    # Zero is legal and means something: dispatch nothing, and report everything dropped as a gap.
    assert resolve_paths(cli_skill_ceiling=0, env={}).skill_ceiling == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5, "6", None])
def test_an_invalid_skill_ceiling_is_refused_identically_wherever_it_came_from(
    tmp_path: Path, bad: object
) -> None:
    """`True` is the one worth naming: `isinstance(True, int)` holds in Python, so a config saying
    `{"skill_ceiling": true}` would sail through a plain int check and silently mean 1. A setting
    that quietly becomes a different number is worse than one that refuses."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({"skill_ceiling": bad}), encoding="utf-8")
    with pytest.raises(PlatformError) as from_file:
        resolve_paths(cli_config_dir=config_dir, env={})
    assert from_file.value.code == "invalid_config"

    if isinstance(bad, int):  # `True` included: the CLI can hand a bool straight through
        with pytest.raises(PlatformError) as from_cli:
            resolve_paths(cli_skill_ceiling=bad, env={})
        assert from_cli.value.code == "invalid_config"

    if isinstance(bad, int) and not isinstance(bad, bool):
        # The environment carries strings, and `str(True)` is not a number at all -- only a real
        # integer round-trips through it, so only that case belongs here.
        with pytest.raises(PlatformError) as from_env:
            resolve_paths(env={"BRURIAH_SKILL_CEILING": str(bad)})
        assert from_env.value.code == "invalid_config"


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
    assert load_registry(today=date(2026, 7, 25)).pack_ids == (
        "programming.minimal", "project.memory", "research.minimal")
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
        # The operator's ceiling has to REACH the thing that applies it. Parsing it correctly and
        # then dropping it on the floor would leave the setting looking configurable and behaving
        # like a constant, which is worse than not offering it.
        assert deps.skill_ceiling == paths.skill_ceiling == DEFAULT_SKILL_CEILING
    finally:
        deps.snapshot.database.close()

    raised = resolve_paths(cli_data_dir=data_dir, cli_config_dir=tmp_path / "config",
                           cli_skill_ceiling=6, env={})
    deps = load_deps(raised, today=_TODAY)
    try:
        assert deps.skill_ceiling == 6
    finally:
        deps.snapshot.database.close()


@requires_legacy_database
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


def test_the_bundled_registry_resolves_only_the_domains_it_should() -> None:
    """`programming` and `general` resolve; every professional domain still abstains.

    Asserted as an exact set rather than a spot check, so quietly widening a pack's `domains` shows
    up here as a failure instead of as silently broader routing.

    The split is deliberate. Abstention exists to stop Bruriah answering about an EXTERNAL body of
    knowledge it holds no policy for, which is why law, accounting, cybersecurity and ux_design must
    keep abstaining -- that is where a confident wrong answer does real damage. `general` is the
    classifier's "no professional speciality applies" bucket; refusing to read a project's own
    decision record there protects nobody and merely hides the user's own corpus from them."""
    import typing

    from bruriah.classify import Domain, RequestClassification
    from bruriah.lookup import discover

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
    assert supported == {"programming", "general"}
    # The property that must never quietly change: the professional domains still refuse.
    assert supported & {"law", "accounting", "cybersecurity", "ux_design", "unsupported"} == set()


def test_the_retired_test_signer_is_no_longer_trusted() -> None:
    """`bruriah-release-test` was a signer whose private half nobody controls. Retiring it is only
    real if a pack bearing its name is now refused, so that is what gets asserted -- the absence of a
    key from a JSON file proves nothing on its own."""
    import json as _json

    from bruriah.packs import PackError, load_pack

    data = Path(__file__).parents[1] / "src/bruriah/data"
    roots = _json.loads((data / "trust-roots.json").read_text())
    assert "bruriah-release-test" not in roots
    assert set(roots) == {"bruriah-release"}

    forged = _json.loads((data / "research-policy.manifest.json").read_text())
    forged["signer"] = "bruriah-release-test"
    forged_path = Path(__file__).parent / "_forged.manifest.json"
    forged_path.write_text(_json.dumps(forged))
    try:
        with pytest.raises(PackError) as refused:
            load_pack(data / "research-policy.json", forged_path, roots, today=_TODAY)
        assert refused.value.code == "unknown_signer"
    finally:
        forged_path.unlink()


def test_every_bundled_pack_ships_with_a_verifiable_manifest() -> None:
    """Packaging assertion: every pack in the data directory has a manifest beside it that verifies
    through the unmodified load path for ITS OWN kind. A pack that ships without one is dead weight
    the loader refuses at startup."""
    from bruriah.packs import load_pack
    from bruriah.skills import load_skill_pack

    data = Path(__file__).parents[1] / "src/bruriah/data"
    roots = json.loads((data / "trust-roots.json").read_text())
    packs = sorted(item for item in data.glob("*.json")
                   if not item.name.endswith(".manifest.json") and item.name != "trust-roots.json")
    # Three bundled packs now: two domain policies and the first-party skill pack. Asserted as an
    # exact list so a pack added without a manifest fails here rather than at a user's first startup.
    assert [item.stem for item in packs] == [
        "practices-pack", "programming-policy", "project-memory-policy", "research-policy"]
    for pack in packs:
        manifest = pack.with_suffix(".manifest.json")
        assert manifest.is_file(), pack.name
        # A skill pack is not a domain pack; loading each through the other's path would either fail
        # or, worse, appear to succeed against the wrong schema.
        loader = load_skill_pack if pack.stem == "practices-pack" else load_pack
        # The skill pack versions independently of the domain packs: it gained three skills.
        expected = "1.1.0" if pack.stem == "practices-pack" else "1.0.0"
        assert loader(pack, manifest, roots, today=_TODAY).version == expected


# --- the user's own skills have to reach serve (or the CLI lifecycle is ceremony) ------------------


def _local_pack(tmp_path: Path, skill_id: str = "leo.deploy") -> Path:
    import hashlib

    body = tmp_path / "body.md"
    body.write_text("# Deploy\nNever on a Friday.\n")
    pack = {
        "schema_version": "1", "pack_id": f"{skill_id}.pack", "version": "1.0.0",
        "maintainer": "Leo", "min_router_version": "0.1.0", "max_router_version": "0.9.9",
        "reviewed_at": "2026-07-25", "expires_at": "2027-07-25", "freshness_days": 365,
        "license": "private", "provenance": "internal conventions",
        "skills": [{
            "skill_id": skill_id, "version": "1.0.0", "tier": "local", "payload": "prose",
            "summary": "How deploys work on this project, written by the person who runs them.",
            "domains": ["programming"], "body_locator": "body.md",
            "body_digest": "sha256:" + hashlib.sha256(body.read_bytes()).hexdigest(),
            "provenance": "internal", "license": "private", "advisories": [],
            "limitations": ["Specific to this project."],
        }],
    }
    path = tmp_path / f"{skill_id}.json"
    path.write_text(json.dumps(pack))
    return path


def _activate_local(tmp_path: Path, paths, skill_id: str = "leo.deploy") -> None:
    from bruriah import cli

    candidate = _local_pack(tmp_path, skill_id)
    cli.run_skill_approve(paths, candidate, [], today=_TODAY)
    cli.run_skill_activate(paths, [candidate], allow_unsigned_local=True, today=_TODAY)


def test_a_users_own_activated_skill_reaches_serve(tmp_path: Path) -> None:
    """The whole CLI lifecycle exists so this is true.

    Regression test for a real defect: `load_deps` loaded only the bundled pack while a docstring
    claimed it merged the user's set. Everything the CLI did -- ingest, analyse, approve, activate --
    ended in a file the server never read, and no test caught it because every dispatch test built
    its SkillSet by hand."""
    from bruriah.platform import load_active_skills

    paths = resolve_paths(cli_data_dir=tmp_path / "data", cli_config_dir=tmp_path / "cfg", env={})
    before = load_active_skills(paths, today=_TODAY)
    _activate_local(tmp_path, paths)
    after = load_active_skills(paths, today=_TODAY)

    assert "leo.deploy" not in before.skill_ids
    assert "leo.deploy" in after.skill_ids
    assert len(after.skill_ids) == len(before.skill_ids) + 1
    assert set(before.skill_ids) < set(after.skill_ids), "bundled skills must survive the merge"


def test_a_broken_user_pointer_leaves_the_shipped_skills_working(tmp_path: Path) -> None:
    # Fail-soft on the user's set, strict on the bundled one: a local mistake must not disarm the
    # skills that shipped signed.
    from bruriah.platform import load_active_skills, load_bundled_skills

    paths = resolve_paths(cli_data_dir=tmp_path / "data", cli_config_dir=tmp_path / "cfg", env={})
    _activate_local(tmp_path, paths)
    (paths.data_dir / "skills" / "active.json").write_text("{not json")
    assert load_active_skills(paths, today=_TODAY).skill_ids == \
        load_bundled_skills(today=_TODAY).skill_ids


def test_a_local_skill_cannot_silently_shadow_a_first_party_one(tmp_path: Path) -> None:
    # Shadowing is exactly how a trusted name gets quietly replaced, so a collision raises rather
    # than letting the local copy win.
    from bruriah.platform import load_active_skills

    paths = resolve_paths(cli_data_dir=tmp_path / "data", cli_config_dir=tmp_path / "cfg", env={})
    _activate_local(tmp_path, paths, skill_id="bruriah.falsifiability-probe")
    with pytest.raises(PlatformError) as error:
        load_active_skills(paths, today=_TODAY)
    assert error.value.code == "skills_collision:duplicate_skill_id"
