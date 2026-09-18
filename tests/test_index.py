from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import warnings
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import requires_vault

import bruriah.index as index_module
from bruriah.corpus import CorpusPolicy
import bruriah.cli as cli_module
from bruriah.cli import _embedding_fingerprint
from bruriah.index import (
    BuildConfig,
    IndexLifecycleError,
    build_candidate,
    open_candidate,
    promote_candidate,
    prune_generations,
    recover_active,
    rollback_active,
    snapshot_active,
)

FINGERPRINT = json.dumps(
    {
        "artifact": "model.onnx",
        "artifact_sha256": "a" * 64,
        "pooling": "mean",
        "runtime": "fastembed==0.8.0",
        "snapshot": "snapshot-a",
        "source": "example/model",
    },
    sort_keys=True,
    separators=(",", ":"),
)


def write_corpus(tmp_path: Path) -> tuple[Path, CorpusPolicy]:
    root = tmp_path / "vault"
    public = root / "public"
    public.mkdir(parents=True)
    (public / "one.md").write_text("# One\nFirst passage.\n", encoding="utf-8")
    (public / "two.md").write_text("# Two\nSecond passage.\n", encoding="utf-8")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    return root, CorpusPolicy.load(policy_path)


def config(root: Path, policy_path: Path) -> BuildConfig:
    return BuildConfig(
        root=root,
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/minilm",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
    )


def fake_embeddings(texts: list[str]) -> list[bytes]:
    return [hashlib.sha256(text.encode()).digest()[:12] for text in texts]


def test_candidate_declares_schema_metadata_manifest_and_model_identity(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"

    result = build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)

    assert result.documents == 2
    assert result.passages == 2
    assert result.reused_documents == 0
    with closing(open_candidate(candidate)) as database:
        metadata = dict(database.execute("SELECT key, value FROM index_meta"))
        manifest = database.execute(
            "SELECT relative_path, source_hash FROM manifest ORDER BY relative_path"
        ).fetchall()
        assert json.loads(metadata["embedding_identity"]) == {
            "dimensions": 3,
            "fingerprint": json.loads(FINGERPRINT),
            "model": "test/minilm",
            "revision": "snapshot-a",
        }
        assert metadata["embedding_fingerprint"] == hashlib.sha256(
            FINGERPRINT.encode()
        ).hexdigest()
        assert metadata["schema_version"] == "1"
        assert metadata["parser_version"] == "corpus-v2"
        assert metadata["ref_version"] == "v1"
        assert metadata["validation_state"] == "candidate"
        assert metadata["corpus_manifest_hash"] == result.manifest_hash
        assert manifest == [
            ("public/one.md", hashlib.sha256((root / "public/one.md").read_bytes()).hexdigest()),
            ("public/two.md", hashlib.sha256((root / "public/two.md").read_bytes()).hexdigest()),
        ]
        assert database.execute("PRAGMA query_only").fetchone() == (1,)


def test_every_passage_is_indexed_under_its_title_and_ancestry_and_stored_bare(
    tmp_path: Path,
) -> None:
    """`search_text` carries what the section does not contain; `text` stays the section itself.

    Both columns exist because the two jobs conflict: retrieval needs the headings a passage sits
    beneath, and `read_evidence` needs bytes it can slice by the offsets a caller already holds. The
    assertions below are that neither job borrowed from the other -- the ancestry never enters
    `text`, and the section's own heading is never repeated into `search_text`, where it already is."""
    root = tmp_path / "vault"
    (root / "public").mkdir(parents=True)
    (root / "public" / "guide.md").write_text(
        "Opening lines before any heading.\n\n"
        "# Installation guide\n\n## Prerequisites\n\n### Windows\n\nRun the installer.\n",
        encoding="utf-8",
    )
    (root / "public" / "untitled.md").write_text("Just a body, no heading at all.\n", encoding="utf-8")
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['public/**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)
    candidate = tmp_path / "candidate.sqlite3"

    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)

    with closing(open_candidate(candidate)) as database:
        stored = {
            (row[0], row[1]): (row[2], row[3])
            for row in database.execute(
                "SELECT relative_path, heading_path, text, search_text FROM passages"
            )
        }

    # An H3: the title and both ancestors, then the section, whose own heading is not repeated.
    text, search_text = stored[("public/guide.md", '["Installation guide", "Prerequisites", "Windows"]')]
    assert text == "### Windows\n\nRun the installer.\n"
    assert search_text == "Installation guide\nPrerequisites\n\n" + text

    # A preamble sits under no heading at all, so it gets the document's title and nothing else.
    text, search_text = stored[("public/guide.md", "[]")]
    assert text == "Opening lines before any heading.\n\n"
    assert search_text == "Installation guide\n\n" + text

    # A document with no H1 has no title to borrow; its file name is the only other thing that is
    # always present and always about the document.
    text, search_text = stored[("public/untitled.md", "[]")]
    assert search_text == "untitled\n\n" + text

    # The section's own heading is already the first line of `text`, and for an H1 it is also the
    # title -- prefixing either would only make the passage repeat itself.
    text, search_text = stored[("public/guide.md", '["Installation guide"]')]
    assert search_text == text


def test_a_snapshot_built_before_search_text_fails_typed_instead_of_at_query_time(
    tmp_path: Path,
) -> None:
    """An index built by an earlier version has no `search_text` column, and the build descriptor it
    left behind describes itself consistently -- so no version marker on its own stops `serve` from
    opening it. What stops it is that validation reads the columns the running code needs: the
    missing column surfaces as a typed `invalid_candidate` when the snapshot is opened, not as a
    bare `OperationalError` on the first query a user asks."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"
    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    with closing(sqlite3.connect(candidate)) as database, database:
        database.execute("ALTER TABLE passages DROP COLUMN search_text")

    with closing(open_candidate(candidate)) as database:
        with pytest.raises(IndexLifecycleError) as caught:
            index_module.validate_candidate(database, config(root, policy_path), policy)

    assert caught.value.code == "invalid_candidate"


def test_incremental_build_reuses_only_compatible_unchanged_documents(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    (root / "public/two.md").write_text("# Two\nChanged.\n", encoding="utf-8")

    result = build_candidate(
        config(root, policy_path), second, policy, fake_embeddings, previous=first
    )

    assert result.reused_documents == 1
    with closing(open_candidate(second)) as database:
        rows = database.execute(
            "SELECT relative_path, text, vector FROM passages ORDER BY relative_path"
        ).fetchall()
    assert rows[0][1:] == ("# One\nFirst passage.\n", fake_embeddings(["# One\nFirst passage.\n"])[0])
    assert rows[1][1] == "# Two\nChanged.\n"

    # `corpus-v1` is the real previous value, not an invented one: the parser now emits
    # `search_text` per passage, so a snapshot built before that has no column to reuse from and
    # every one of its rows would be a passage indexed without its heading ancestry. The version
    # marker is what makes that refusal automatic -- one full rebuild, then reuse resumes.
    incompatible = replace(config(root, policy_path), parser_version="corpus-v1")
    third = tmp_path / "third.sqlite3"
    assert build_candidate(
        incompatible, third, policy, fake_embeddings, previous=second
    ).reused_documents == 0


def test_the_active_pointer_resolves_to_a_reusable_previous_index(tmp_path: Path) -> None:
    """`active_database` is what turns a pointer into a `previous` for the next build. It answers
    with a path only when there is a file to reuse, and with `None` for every other outcome --
    reuse is an optimisation, and none of its failure modes may become a build that fails."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    candidate = tmp_path / "candidate-abc.sqlite3"

    assert index_module.active_database(pointer) is None  # no index has ever been built here

    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    promote_candidate(candidate, pointer, config(root, policy_path), policy)
    assert index_module.active_database(pointer) == candidate

    candidate.unlink()  # a prune, or anything else, took the file the pointer still names
    assert index_module.active_database(pointer) is None

    pointer.write_text("not json", encoding="utf-8")
    assert index_module.active_database(pointer) is None


def test_a_previous_index_that_cannot_be_opened_is_not_reused_rather_than_fatal(
    tmp_path: Path,
) -> None:
    """Builds hold no lock across the embedding phase, so the file named as `previous` can be
    unlinked or replaced while the build that wanted to reuse it is running. Both outcomes have to
    end in a complete build with nothing reused -- the corpus is the source of truth, and the point
    of reuse is to save time, never to be required."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    missing = tmp_path / "gone.sqlite3"
    not_a_database = tmp_path / "garbage.sqlite3"
    not_a_database.write_bytes(b"this is not a sqlite file")

    for previous, candidate in (
        (missing, tmp_path / "from-missing.sqlite3"),
        (not_a_database, tmp_path / "from-garbage.sqlite3"),
    ):
        result = build_candidate(
            config(root, policy_path), candidate, policy, fake_embeddings, previous=previous
        )
        assert result.reused_documents == 0
        assert result.documents == 2


def test_failed_build_removes_candidate_and_preserves_existing_assets(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"
    retained = tmp_path / "live.db"
    retained.write_bytes(b"retained-live-index")

    def fail(_: list[str]) -> list[bytes]:
        raise RuntimeError("embedding failed")

    with pytest.raises(RuntimeError, match="embedding failed"):
        build_candidate(config(root, policy_path), candidate, policy, fail)

    assert not candidate.exists()
    assert retained.read_bytes() == b"retained-live-index"
    assert not list(tmp_path.glob(".candidate.sqlite3.*.tmp"))


def test_deleted_documents_and_incompatible_embeddings_are_not_reused(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    first = tmp_path / "first.sqlite3"
    build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    (root / "public/two.md").unlink()

    for name, changes in (
        ("model", {"embedding_model": "other/model"}),
        (
            "revision",
            {
                "embedding_revision": "snapshot-b",
                "embedding_fingerprint": FINGERPRINT.replace("snapshot-a", "snapshot-b"),
            },
        ),
        ("dimensions", {"embedding_dimensions": 4}),
        (
            "fingerprint",
            {"embedding_fingerprint": FINGERPRINT.replace("a" * 64, "b" * 64)},
        ),
        ("pooling", {"embedding_fingerprint": FINGERPRINT.replace("mean", "cls")}),
    ):
        candidate = tmp_path / f"{name}.sqlite3"
        changed = replace(config(root, policy_path), **changes)
        embedder = lambda texts, size=changed.embedding_dimensions: [b"x" * (size * 4) for _ in texts]
        assert build_candidate(changed, candidate, policy, embedder, previous=first).reused_documents == 0
        with closing(open_candidate(candidate)) as database:
            assert database.execute("SELECT relative_path FROM documents").fetchall() == [
                ("public/one.md",)
            ]


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE passages SET vector = X'00' WHERE relative_path = 'public/one.md'",
        "UPDATE passages SET source_hash = 'wrong' WHERE relative_path = 'public/one.md'",
        "UPDATE passages SET start_line = 99 WHERE relative_path = 'public/one.md'",
        "DELETE FROM passages WHERE relative_path = 'public/one.md'",
    ],
)
def test_semantically_invalid_reused_rows_are_rebuilt(tmp_path: Path, corruption: str) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    with closing(sqlite3.connect(first)) as database, database:
        database.execute(corruption)
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    result = build_candidate(config(root, policy_path), second, policy, fake_embeddings, previous=first)

    assert result.reused_documents == 1
    with closing(open_candidate(second)) as database:
        assert database.execute(
            "SELECT count(*) FROM passages WHERE length(vector) != 12"
        ).fetchone() == (0,)
        assert database.execute("SELECT count(*) FROM passages").fetchone() == (2,)


def test_fastembed_fingerprint_binds_pooling_source_snapshot_and_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"model-weights")
    backend_type = type("PooledEmbedding", (), {})
    backend = backend_type()
    backend._model_dir = tmp_path
    backend.model_description = SimpleNamespace(
        model_file="model.onnx", sources=SimpleNamespace(hf="qdrant/model", url=None)
    )

    fingerprint = json.loads(_embedding_fingerprint(SimpleNamespace(model=backend)))

    assert fingerprint == {
        "artifact": "model.onnx",
        "artifact_sha256": hashlib.sha256(b"model-weights").hexdigest(),
        "pooling": "mean",
        "runtime": "fastembed==0.8.0",
        "snapshot": tmp_path.name,
        "source": "qdrant/model",
    }


def test_only_the_pooling_notice_is_swallowed_on_model_construction(
    tmp_path: Path, monkeypatch, recwarn
) -> None:
    """fastembed says, every time a model is built, that it now pools by mean instead of CLS. That
    lands mid-quickstart and reads like a fault, and the change it reports already invalidates an
    index through `embedding_fingerprint` rather than needing to be read. So it is filtered -- by
    message. Filtering the CATEGORY would take every future fastembed warning with it, and those
    have no such backstop."""
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"model-weights")
    backend_type = type("PooledEmbedding", (), {})
    backend = backend_type()
    backend._model_dir = tmp_path
    backend.model_description = SimpleNamespace(
        model_file="model.onnx", sources=SimpleNamespace(hf="qdrant/model", url=None)
    )

    def _noisy(model_name: str):
        warnings.warn(
            f"The model {model_name} now uses mean pooling instead of CLS embedding.", UserWarning
        )
        warnings.warn("a different fastembed problem, with no backstop", UserWarning)
        return SimpleNamespace(model=backend, embed=lambda texts: [], embedding_size=3)

    monkeypatch.setattr(cli_module, "TextEmbedding", _noisy)
    cli_module._default_embedder_factory("qdrant/model")

    raised = [str(item.message) for item in recwarn]
    assert not any("mean pooling" in message for message in raised), "filtered too little"
    assert any("no backstop" in message for message in raised), "filtered too much"


@requires_vault
def test_real_corpus_candidate_has_expected_counts(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    root = repository / "Cerebro-IA"
    policy_path = repository / "cerebro-retrieval/corpus-policy.yaml"
    policy = CorpusPolicy.load(policy_path)
    real_config = replace(config(root, policy_path), embedding_dimensions=1)

    result = build_candidate(
        real_config,
        tmp_path / "real.sqlite3",
        policy,
        lambda texts: [b"\0" * 4 for _ in texts],
    )

    assert (result.documents, result.passages) == (331, 9657)


def test_promotion_is_atomic_and_existing_snapshot_survives(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    first_result = build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    promote_candidate(first, pointer, config(root, policy_path), policy)
    old_snapshot = snapshot_active(pointer, config(root, policy_path))
    (root / "public/two.md").write_text("# Two\nChanged.\n", encoding="utf-8")
    second_result = build_candidate(config(root, policy_path), second, policy, fake_embeddings)

    promote_candidate(second, pointer, config(root, policy_path), policy)

    with old_snapshot:
        assert old_snapshot.build_id == first_result.build_id
        assert old_snapshot.database.execute("SELECT count(*) FROM passages").fetchone() == (2,)
    with snapshot_active(pointer, config(root, policy_path)) as current:
        assert current.build_id == second_result.build_id
        assert current.path == second
    assert first.exists() and second.exists()
    assert not list(tmp_path.glob(".active.json.*.tmp"))


def test_invalid_or_incompatible_promotion_keeps_last_known_good(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    active = tmp_path / "active.sqlite3"
    invalid = tmp_path / "invalid.sqlite3"
    incompatible = tmp_path / "incompatible.sqlite3"
    result = build_candidate(config(root, policy_path), active, policy, fake_embeddings)
    promote_candidate(active, pointer, config(root, policy_path), policy)
    original_pointer = pointer.read_bytes()
    old_snapshot = snapshot_active(pointer, config(root, policy_path))
    build_candidate(config(root, policy_path), invalid, policy, fake_embeddings)
    with closing(sqlite3.connect(invalid)) as database, database:
        database.execute(
            "UPDATE passages SET vector = X'00' WHERE ref = (SELECT ref FROM passages LIMIT 1)"
        )
    changed = replace(config(root, policy_path), parser_version="corpus-v1")
    build_candidate(changed, incompatible, policy, fake_embeddings)

    with pytest.raises(ValueError, match="invalid_candidate"):
        promote_candidate(invalid, pointer, config(root, policy_path), policy)
    with pytest.raises(ValueError, match="incompatible_candidate"):
        promote_candidate(incompatible, pointer, config(root, policy_path), policy)

    assert pointer.read_bytes() == original_pointer
    with old_snapshot:
        assert old_snapshot.database.execute("SELECT count(*) FROM passages").fetchone() == (2,)
    with snapshot_active(pointer, config(root, policy_path)) as snapshot:
        assert snapshot.build_id == result.build_id


def test_edited_policy_promotes_and_discards_the_unretainable_generation(tmp_path: Path) -> None:
    """An edited `policy.yaml` used to make the directory permanently unindexable: the outgoing
    index's `policy_hash` is the old policy's by construction, so validating it against the new
    config could only ever fail, and that failure vetoed a candidate that had already validated.
    Promotion now proceeds, the un-activatable generation is dropped instead of recorded as a
    rollback target that `rollback_active` would refuse, and the loss is reported."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    first_activation = promote_candidate(first, pointer, config(root, policy_path), policy)
    assert first_activation.retention_discarded is False

    policy_path.write_text(
        "version: 1\ninclude: ['public/**']\nexclude: ['drafts/**']\n", encoding="utf-8"
    )
    edited = CorpusPolicy.load(policy_path)
    second_result = build_candidate(config(root, policy_path), second, edited, fake_embeddings)

    activation = promote_candidate(second, pointer, config(root, policy_path), edited)

    assert activation.retention_discarded is True
    assert activation.build_id == second_result.build_id
    assert json.loads(pointer.read_text(encoding="utf-8"))["retained"] == []
    with snapshot_active(pointer, config(root, policy_path)) as snapshot:
        assert snapshot.build_id == second_result.build_id
    # The dropped generation is unreferenced, not deleted: nothing here removes a file it did not
    # create, and the bytes stay available to anyone who wants to inspect them.
    assert first.exists()
    with pytest.raises(ValueError, match="no_retained_index"):
        rollback_active(pointer, config(root, policy_path))


def test_prune_removes_only_generations_the_pointer_does_not_reference(tmp_path: Path) -> None:
    """Promotion leaves the outgoing index on disk, and a promotion under a changed policy now
    drops it from `retained` rather than keeping it, so unreferenced generations accumulate with
    nothing able to name them. `retain=1` here makes the third promotion strand the first."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first, second, third = (tmp_path / f"candidate-{'ab' * 16}.sqlite3",
                            tmp_path / f"candidate-{'cd' * 16}.sqlite3",
                            tmp_path / f"candidate-{'ef' * 16}.sqlite3")
    for candidate in (first, second, third):
        build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
        promote_candidate(candidate, pointer, config(root, policy_path), policy, retain=1)
    value = json.loads(pointer.read_text(encoding="utf-8"))
    assert value["active"]["database"] == third.name
    assert [entry["database"] for entry in value["retained"]] == [second.name]

    removed = prune_generations(pointer)

    assert [path.name for path in removed] == [first.name]
    assert not first.exists()
    assert second.exists() and third.exists(), "a referenced generation was removed"
    assert prune_generations(pointer) == (), "a second run has nothing left to do"


def test_prune_cannot_name_anything_it_did_not_create(tmp_path: Path) -> None:
    """The only unlink in the package. It matches the exact form `run_index` writes, so the
    pointer, the build descriptor, the lock and whatever an operator left in the data directory
    are not expressible as targets -- including files that merely look close."""
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    candidate = tmp_path / f"candidate-{'ab' * 16}.sqlite3"
    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    promote_candidate(candidate, pointer, config(root, policy_path), policy)
    bystanders = [
        tmp_path / "build-config.json",
        tmp_path / "candidate.sqlite3",
        tmp_path / "candidate-not-a-uuid.sqlite3",
        tmp_path / f"candidate-{'ab' * 16}.sqlite3.bak",
        tmp_path / f"CANDIDATE-{'ab' * 16}.sqlite3",
    ]
    for path in bystanders:
        path.write_text("not mine to delete", encoding="utf-8")

    assert prune_generations(pointer) == ()
    for path in bystanders:
        assert path.exists(), f"{path.name} was removed and never should have been"


def test_prune_refuses_when_there_is_no_pointer_to_read(tmp_path: Path) -> None:
    """With no pointer, every generation reads as unreferenced. Refusing is the only safe answer;
    deleting on the strength of a file that is not there is the opposite one."""
    stranded = tmp_path / f"candidate-{'ab' * 16}.sqlite3"
    stranded.write_text("would be lost", encoding="utf-8")
    with pytest.raises(ValueError, match="index_not_built"):
        prune_generations(tmp_path / "active.json")
    assert stranded.exists()


def test_rollback_and_recovery_restore_retained_index_without_rebuild(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    first_result = build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    promote_candidate(first, pointer, config(root, policy_path), policy)
    (root / "public/two.md").write_text("# Two\nChanged.\n", encoding="utf-8")
    second_result = build_candidate(config(root, policy_path), second, policy, fake_embeddings)
    promote_candidate(second, pointer, config(root, policy_path), policy)

    (root / "public/two.md").write_text("# Two\nSecond passage.\n", encoding="utf-8")
    rolled_back = rollback_active(pointer, config(root, policy_path))
    assert rolled_back.build_id == first_result.build_id
    with snapshot_active(pointer, config(root, policy_path)) as snapshot:
        assert snapshot.build_id == first_result.build_id
    (root / "public/two.md").write_text("# Two\nChanged.\n", encoding="utf-8")
    promote_candidate(second, pointer, config(root, policy_path), policy)
    with closing(sqlite3.connect(second)) as database, database:
        database.execute(
            "UPDATE passages SET source_hash = 'corrupt' "
            "WHERE ref = (SELECT ref FROM passages LIMIT 1)"
        )

    (root / "public/two.md").write_text("# Two\nSecond passage.\n", encoding="utf-8")
    recovered = recover_active(pointer, config(root, policy_path))

    assert recovered.build_id == first_result.build_id
    assert recovered.build_id != second_result.build_id
    with snapshot_active(pointer, config(root, policy_path)) as snapshot:
        assert snapshot.path == first


def test_active_target_rejects_escape_corruption_and_missing_file(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside.sqlite3"
    result = build_candidate(config(root, policy_path), outside, policy, fake_embeddings)
    pointer = store / "active.json"
    (store / "escape.sqlite3").symlink_to(outside)
    pointer.write_text(json.dumps({"version": 1, "active": {
        "database": "../outside.sqlite3", "build_id": result.build_id}, "retained": []}))
    with pytest.raises(IndexLifecycleError, match="invalid_active_pointer"):
        snapshot_active(pointer, config(root, policy_path))
    pointer.write_text(json.dumps({"version": 1, "active": {
        "database": "escape.sqlite3", "build_id": result.build_id}, "retained": []}))
    with pytest.raises(IndexLifecycleError) as escape:
        snapshot_active(pointer, config(root, policy_path))
    assert escape.value.code == "invalid_active_target"
    inside = store / "inside.sqlite3"
    build_candidate(config(root, policy_path), inside, policy, fake_embeddings)
    pointer.unlink()
    promote_candidate(inside, pointer, config(root, policy_path), policy)
    # `with sqlite3.connect(...)` is a TRANSACTION context manager, not a closing one -- it commits
    # and leaves the connection open. The `inside.unlink()` below then tried to delete a file this
    # test still had open, which POSIX permits and Windows refuses. Closing explicitly is what the
    # test always meant; the leak was simply invisible on one platform.
    database = sqlite3.connect(inside)
    try:
        database.execute("UPDATE passages SET vector = X'00' WHERE ref = (SELECT ref FROM passages LIMIT 1)")
        database.commit()
    finally:
        database.close()
    with pytest.raises(IndexLifecycleError) as corrupt:
        snapshot_active(pointer, config(root, policy_path))
    assert corrupt.value.code == "invalid_active_target"
    inside.unlink()
    with pytest.raises(IndexLifecycleError) as missing:
        snapshot_active(pointer, config(root, policy_path))
    assert missing.value.code == "invalid_active_target"


def test_rollback_rebinds_identity_and_malformed_recovery_scans_nothing(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    first_result = build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    build_candidate(config(root, policy_path), second, policy, fake_embeddings)
    promote_candidate(first, pointer, config(root, policy_path), policy)
    promote_candidate(second, pointer, config(root, policy_path), policy)
    value = json.loads(pointer.read_text())
    value["retained"][0]["build_id"] = "stale"
    pointer.write_text(json.dumps(value))
    rolled_back = rollback_active(pointer, config(root, policy_path))
    assert rolled_back.build_id == first_result.build_id
    assert json.loads(pointer.read_text())["active"]["build_id"] == first_result.build_id
    pointer.write_text("not-json")
    with pytest.raises(IndexLifecycleError) as recovery:
        recover_active(pointer, config(root, policy_path))
    assert recovery.value.code == "invalid_active_pointer"


def test_promotion_reports_directory_fsync_outcome_and_runs_smoke_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    build_candidate(config(root, policy_path), first, policy, fake_embeddings)
    second_result = build_candidate(config(root, policy_path), second, policy, fake_embeddings)
    promote_candidate(first, pointer, config(root, policy_path), policy)
    # The point is that a durability failure is REPORTED rather than crashed on, and that the
    # promotion still publishes. Only the failure can be injected on POSIX: Windows has no
    # per-directory flush to fail, because NTFS journals the rename itself -- so there the same
    # promotion is genuinely durable and must say so. Asserting `durable` in both directions keeps
    # this a test of the flag's honesty rather than of one platform's plumbing.
    if os.name == "posix":
        real_fsync = os.fsync
        def fail_directory_fsync(file_descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
                raise OSError("injected directory fsync failure")
            real_fsync(file_descriptor)
        monkeypatch.setattr(index_module.os, "fsync", fail_directory_fsync)
        outcome = promote_candidate(second, pointer, config(root, policy_path), policy)
        assert not outcome.durable
        monkeypatch.setattr(index_module.os, "fsync", real_fsync)
    else:
        outcome = promote_candidate(second, pointer, config(root, policy_path), policy)
        assert outcome.durable
    assert outcome.build_id == second_result.build_id
    with snapshot_active(pointer, config(root, policy_path)) as snapshot:
        assert snapshot.build_id == second_result.build_id
    original = pointer.read_bytes()
    fail_query = lambda _: (_ for _ in ()).throw(IndexLifecycleError("representative_query_failed"))
    monkeypatch.setattr(index_module, "_representative_queries", fail_query)
    with pytest.raises(IndexLifecycleError, match="representative_query_failed"):
        promote_candidate(first, pointer, config(root, policy_path), policy)
    assert pointer.read_bytes() == original


def test_promotion_rejects_candidate_path_swap_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    build_candidate(config(root, policy_path), replacement, policy, fake_embeddings)
    real_validate = index_module.validate_candidate
    swap_succeeded = False

    def swap_after_validation(path: Path, build: BuildConfig, corpus: CorpusPolicy):
        # The GUARANTEE is that a swap here never publishes the swapped file. There are two honest
        # ways to keep it and the platforms use different ones, so the test asserts the outcome
        # rather than the mechanism -- otherwise the stronger implementation fails the test written
        # for the weaker one. POSIX cannot stop the rename, so the code must DETECT it afterwards
        # via the identity re-check. Windows opens the candidate pinned, so the rename is REFUSED
        # by the OS and the attack never lands at all.
        nonlocal swap_succeeded
        metadata = real_validate(path, build, corpus)
        try:
            os.replace(replacement, candidate)
            swap_succeeded = True
        except OSError:
            swap_succeeded = False
        return metadata

    monkeypatch.setattr(index_module, "validate_candidate", swap_after_validation)
    pointer = tmp_path / "active.json"
    code = None
    try:
        promote_candidate(candidate, pointer, config(root, policy_path), policy)
        published = True
    except IndexLifecycleError as error:
        published, code = False, error.code

    if swap_succeeded:
        # The OS allowed the attack, so DETECTION is the only thing standing between it and a
        # published impostor. Nothing may be published.
        assert not published and code == "candidate_changed_during_validation"
        assert not pointer.exists()
    else:
        # The OS refused the attack, so there is nothing to detect and promotion is free to
        # succeed -- what it publishes is provably the file that was validated.
        assert published and pointer.exists()

    # Guard against the test quietly stopping to exercise anything: on POSIX the swap MUST be
    # possible, or this passed for the wrong reason.
    assert swap_succeeded or os.name != "posix"


@pytest.mark.parametrize("corruption", [
    "UPDATE passages SET start_line = 999 WHERE ref = (SELECT ref FROM passages LIMIT 1)",
    "DELETE FROM passages WHERE ref = (SELECT ref FROM passages LIMIT 1)",
    "UPDATE passages SET text = 'stale' WHERE ref = (SELECT ref FROM passages LIMIT 1)",
    "UPDATE passages SET relative_path = 'stale.md' WHERE ref = (SELECT ref FROM passages LIMIT 1)",
    "UPDATE passages SET source_hash = 'stale' WHERE ref = (SELECT ref FROM passages LIMIT 1)",
])
def test_snapshot_rejects_noncanonical_passage_semantics(
    tmp_path: Path, corruption: str
) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"
    pointer = tmp_path / "active.json"
    build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    promote_candidate(candidate, pointer, config(root, policy_path), policy)
    with closing(sqlite3.connect(candidate)) as database, database:
        database.execute(corruption)
    with pytest.raises(IndexLifecycleError, match="invalid_active_target"):
        snapshot_active(pointer, config(root, policy_path))


def test_transient_aba_swap_cannot_publish_other_database_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "candidate.sqlite3"
    replacement = tmp_path / "replacement.sqlite3"
    parked = tmp_path / "parked.sqlite3"
    original = build_candidate(config(root, policy_path), candidate, policy, fake_embeddings)
    swapped = build_candidate(config(root, policy_path), replacement, policy, fake_embeddings)
    real_validate = index_module.validate_candidate
    aba_succeeded = False

    def aba_validate(subject, build: BuildConfig, corpus: CorpusPolicy):
        # An A-B-A swap puts the name back before anyone looks, so a comparison of PATHS would see
        # nothing wrong -- which is why promotion pins identity, not the name. Whether the swap can
        # be staged at all is platform-dependent, so the assertion below is about what gets
        # PUBLISHED, which is the same on both: the identity that was validated, never the impostor.
        nonlocal aba_succeeded
        try:
            os.replace(candidate, parked)
            os.replace(replacement, candidate)
            aba_succeeded = True
        except OSError:
            return real_validate(subject, build, corpus)
        try:
            return real_validate(subject, build, corpus)
        finally:
            os.replace(candidate, replacement)
            os.replace(parked, candidate)

    monkeypatch.setattr(index_module, "validate_candidate", aba_validate)

    result = promote_candidate(candidate, tmp_path / "active.json", config(root, policy_path), policy)

    assert result.build_id == original.build_id != swapped.build_id
    with snapshot_active(tmp_path / "active.json", config(root, policy_path)) as snapshot:
        assert snapshot.build_id == original.build_id
    # Guard against passing for the wrong reason: on POSIX the swap MUST have been stageable, or
    # the identity pinning was never actually exercised.
    assert aba_succeeded or os.name != "posix"


def test_concurrent_readers_and_promoters_keep_complete_history(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    pointer = tmp_path / "active.json"
    candidates = [tmp_path / f"candidate-{number}.sqlite3" for number in range(3)]
    results = [build_candidate(config(root, policy_path), path, policy, fake_embeddings)
               for path in candidates]
    promote_candidate(candidates[0], pointer, config(root, policy_path), policy)
    barrier = threading.Barrier(9)
    def read_old() -> str:
        with snapshot_active(pointer, config(root, policy_path)) as snapshot:
            barrier.wait()
            for _ in range(50):
                assert snapshot.database.execute("SELECT count(*) FROM passages").fetchone() == (2,)
            return snapshot.build_id
    with ThreadPoolExecutor(max_workers=10) as pool:
        readers = [pool.submit(read_old) for _ in range(8)]
        barrier.wait()
        promote_candidate(candidates[1], pointer, config(root, policy_path), policy)
        assert {future.result() for future in readers} == {results[0].build_id}
        promotions = [
            pool.submit(promote_candidate, candidate, pointer, config(root, policy_path), policy)
            for candidate in candidates[1:]
        ]
        assert len({future.result().build_id for future in promotions}) == 2
    value = json.loads(pointer.read_text())
    observed = {value["active"]["build_id"], *(item["build_id"] for item in value["retained"])}
    assert observed == {result.build_id for result in results}


def test_lineage_table_records_supersessions_and_resolves_predecessors(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)

    doc1_content = (
        "---\ncommit: a1b2c3d4e5f6\n---\n"
        "# Initial Decision\n\n**Decided:** 2026-01-01 · **Commit:** `a1b2c3d4e5f6` · **Author:** Alice\n\n"
        "Original architecture reasoning.\n"
    )
    (root / "doc1.md").write_text(doc1_content, encoding="utf-8")

    doc2_content = (
        "---\ncommit: f6e5d4c3b2a1\nsupersedes:\n  - a1b2c3d4e5f6\ndeprecates:\n  - deadbeef0000\n---\n"
        "# Replacement Decision\n\n**Decided:** 2026-02-01 · **Commit:** `f6e5d4c3b2a1` · **Author:** Bob\n\n"
        "New architecture reasoning that replaces the initial one.\n"
    )
    (root / "doc2.md").write_text(doc2_content, encoding="utf-8")

    destination = tmp_path / "candidate.sqlite3"
    build_candidate(config(root, policy_path), destination, policy, fake_embeddings)

    with closing(sqlite3.connect(destination)) as db:
        rows = db.execute(
            "SELECT successor_ref, predecessor_target, predecessor_ref, relation FROM lineage ORDER BY relation DESC"
        ).fetchall()
        assert len(rows) == 2
        # supersedes row
        succ_ref, pred_target, pred_ref, rel = rows[0]
        assert rel == "supersedes"
        assert pred_target == "a1b2c3d4e5f6"
        assert pred_ref is not None and pred_ref.startswith("doc:v1:")
        assert succ_ref.startswith("doc:v1:")
        # deprecates row
        _, dep_target, dep_ref, dep_rel = rows[1]
        assert dep_rel == "deprecates"
        assert dep_target == "deadbeef0000"
        assert dep_ref is None  # not in corpus


def test_lineage_detects_and_rejects_cycles(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['**']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)

    (root / "doc1.md").write_text(
        "---\ncommit: 111111111111\nsupersedes:\n  - 222222222222\n---\n# Doc 1\n\nBody 1.\n",
        encoding="utf-8",
    )
    (root / "doc2.md").write_text(
        "---\ncommit: 222222222222\nsupersedes:\n  - 111111111111\n---\n# Doc 2\n\nBody 2.\n",
        encoding="utf-8",
    )

    destination = tmp_path / "cycle.sqlite3"
    with pytest.raises(IndexLifecycleError, match="lineage_cycle_detected"):
        build_candidate(config(root, policy_path), destination, policy, fake_embeddings)




def test_asymmetric_embedding_prefixes_in_identity_and_candidate_build(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    candidate = tmp_path / "asym.sqlite3"

    asym_config = BuildConfig(
        root=root,
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="intfloat/multilingual-e5-large",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1",
        query_prefix="query: ",
        passage_prefix="passage: ",
    )

    embedded_texts: list[str] = []

    def recording_embedder(texts: list[str]) -> list[bytes]:
        embedded_texts.extend(texts)
        return [hashlib.sha256(t.encode()).digest()[:12] for t in texts]

    result = build_candidate(asym_config, candidate, policy, recording_embedder)
    assert result.passages == 2
    assert len(embedded_texts) == 2
    assert all(t.startswith("passage: ") for t in embedded_texts)

    with closing(open_candidate(candidate)) as database:
        metadata = dict(database.execute("SELECT key, value FROM index_meta"))
        identity = json.loads(metadata["embedding_identity"])
        assert identity["query_prefix"] == "query: "
        assert identity["passage_prefix"] == "passage: "


def test_differing_embedding_prefixes_prevent_document_reuse(tmp_path: Path) -> None:
    root, policy = write_corpus(tmp_path)
    policy_path = tmp_path / "policy.yaml"
    first_candidate = tmp_path / "first.sqlite3"
    second_candidate = tmp_path / "second.sqlite3"

    cfg_first = BuildConfig(
        root=root, policy_path=policy_path, schema_version=1, parser_version="corpus-v2",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model="test/minilm",
        embedding_revision="snapshot-a", embedding_dimensions=3, embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1", passage_prefix="passage: ", query_prefix="query: ",
    )
    build_candidate(cfg_first, first_candidate, policy, fake_embeddings)

    cfg_second = BuildConfig(
        root=root, policy_path=policy_path, schema_version=1, parser_version="corpus-v2",
        service_version="0.1.0", mcp_range=">=1.28.1,<2", embedding_model="test/minilm",
        embedding_revision="snapshot-a", embedding_dimensions=3, embedding_fingerprint=FINGERPRINT,
        ranking_config="rrf-v1", passage_prefix="", query_prefix="",
    )
    result = build_candidate(cfg_second, second_candidate, policy, fake_embeddings, previous=first_candidate)
    assert result.reused_documents == 0
