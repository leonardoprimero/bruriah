from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import language
from .corpus import CorpusPolicy, parse_document
from .models import Document
from .pointer import (
    activation_lock,
    controlled_file,
    flush_validated,
    identity_matches,
    read_pointer,
    serialized,
    write_pointer,
)

Embedder = Callable[[list[str]], list[bytes]]
REF_VERSION = "v1"


@dataclass(frozen=True)
class BuildConfig:
    root: Path
    policy_path: Path
    schema_version: int
    parser_version: str
    service_version: str
    mcp_range: str
    embedding_model: str
    embedding_revision: str
    embedding_dimensions: int
    embedding_fingerprint: str
    ranking_config: str
    query_prefix: str = ""
    passage_prefix: str = ""

    def __post_init__(self) -> None:
        try:
            fingerprint = json.loads(self.embedding_fingerprint)
        except (json.JSONDecodeError, TypeError) as error:
            raise ValueError("invalid_embedding_fingerprint") from error
        required = {
            "artifact",
            "artifact_sha256",
            "pooling",
            "runtime",
            "snapshot",
            "source",
        }
        if (
            not isinstance(fingerprint, dict)
            or set(fingerprint) != required
            or not all(isinstance(value, str) and value for value in fingerprint.values())
            or fingerprint["snapshot"] != self.embedding_revision
            or len(fingerprint["artifact_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint["artifact_sha256"])
            or self.embedding_dimensions < 1
        ):
            raise ValueError("invalid_embedding_fingerprint")

    @property
    def embedding_identity(self) -> str:
        identity: dict[str, object] = {
            "dimensions": self.embedding_dimensions,
            "fingerprint": json.loads(self.embedding_fingerprint),
            "model": self.embedding_model,
            "revision": self.embedding_revision,
        }
        if self.passage_prefix:
            identity["passage_prefix"] = self.passage_prefix
        if self.query_prefix:
            identity["query_prefix"] = self.query_prefix
        return json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class BuildResult:
    path: Path
    build_id: str
    manifest_hash: str
    documents: int
    passages: int
    reused_documents: int
    dropped_premises: tuple[DroppedPremise, ...] = ()


@dataclass(frozen=True)
class DroppedPremise:
    """A GitHub-sourced premise declaration `_build_premise_and_alternative_records` refused to
    let win, surfaced so the drop is visible in the index report instead of a silent loss."""

    premise_id: str
    document_ref: str
    relative_path: str
    reason: Literal[
        "shadowed_by_repository_premise",
        "shadowed_by_lower_github_issue",
        "github_invalidation_ignored",
    ]


@dataclass
class ActiveSnapshot:
    path: Path
    build_id: str
    database: sqlite3.Connection

    def __enter__(self) -> ActiveSnapshot:
        return self

    def __exit__(self, *_: object) -> None:
        self.database.close()


@dataclass(frozen=True)
class ActivationResult:
    path: Path
    build_id: str
    durable: bool = True
    # True when the outgoing index could not be kept as a rollback target. Reported rather than
    # silent, for the same reason a truncated read is: the caller loses a capability it had, and
    # finding that out at rollback time is finding out too late.
    retention_discarded: bool = False


class IndexLifecycleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE manifest (
    relative_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE documents (
    document_ref TEXT PRIMARY KEY, relative_path TEXT UNIQUE NOT NULL,
    source_hash TEXT NOT NULL, metadata TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE passages (
    ref TEXT PRIMARY KEY, document_ref TEXT NOT NULL, relative_path TEXT NOT NULL,
    heading_path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
    text TEXT NOT NULL, source_hash TEXT NOT NULL, metadata TEXT NOT NULL,
    -- `search_text` sits before `vector` on purpose: `_stored_document` compares `stored[:-1]`
    -- against the expected tuple and type-checks the last column as the blob, so `vector` has to
    -- stay last for a new column to be compared rather than skipped.
    search_text TEXT NOT NULL,
    vector BLOB NOT NULL, FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
CREATE TABLE lineage (
    successor_ref TEXT NOT NULL,
    predecessor_target TEXT NOT NULL,
    predecessor_ref TEXT,
    relation TEXT NOT NULL,
    PRIMARY KEY (successor_ref, predecessor_target, relation),
    FOREIGN KEY(successor_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
CREATE INDEX idx_lineage_pred ON lineage(predecessor_target);
CREATE INDEX idx_lineage_pred_ref ON lineage(predecessor_ref);
CREATE TABLE premises (
    premise_id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    status TEXT NOT NULL,
    invalidated_by TEXT,
    rationale TEXT,
    document_ref TEXT NOT NULL,
    invalidation_document_ref TEXT,
    FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
CREATE INDEX idx_premises_doc ON premises(document_ref);
CREATE TABLE alternatives (
    name TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    premises_json TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    PRIMARY KEY (name, document_ref),
    FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
CREATE INDEX idx_alternatives_name ON alternatives(name);
CREATE TABLE corpus_stats (key TEXT PRIMARY KEY, num_value REAL, str_value TEXT) WITHOUT ROWID;
CREATE TABLE term_df (term TEXT PRIMARY KEY, df INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE term_postings (
    term TEXT NOT NULL,
    ref TEXT NOT NULL,
    freq INTEGER NOT NULL,
    doc_length INTEGER NOT NULL,
    PRIMARY KEY (term, ref),
    FOREIGN KEY(ref) REFERENCES passages(ref)
) WITHOUT ROWID;
"""


def _hash_records(records: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for path, source_hash in records:
        digest.update(path.encode("utf-8") + b"\0" + source_hash.encode("ascii") + b"\n")
    return digest.hexdigest()


def _metadata(config: BuildConfig, manifest_hash: str, build_id: str) -> dict[str, str]:
    return {
        "schema_version": str(config.schema_version),
        "parser_version": config.parser_version,
        "ref_version": REF_VERSION,
        "service_version": config.service_version,
        "mcp_range": config.mcp_range,
        "embedding_identity": config.embedding_identity,
        "embedding_fingerprint": hashlib.sha256(
            config.embedding_fingerprint.encode()
        ).hexdigest(),
        "ranking_config": config.ranking_config,
        "policy_hash": hashlib.sha256(config.policy_path.read_bytes()).hexdigest(),
        "corpus_manifest_hash": manifest_hash,
        "build_id": build_id,
        "created_at": datetime.now(UTC).isoformat(),
        "validation_state": "candidate",
    }


def open_candidate(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, check_same_thread=False)
    database.execute("PRAGMA query_only = ON")
    return database


def _compatible(previous: sqlite3.Connection, config: BuildConfig) -> bool:
    try:
        metadata = dict(previous.execute("SELECT key, value FROM index_meta"))
    except sqlite3.DatabaseError:
        return False
    return all(
        metadata.get(key) == value
        for key, value in {
            "schema_version": str(config.schema_version),
            "parser_version": config.parser_version,
            "ref_version": REF_VERSION,
            "embedding_identity": config.embedding_identity,
            "embedding_fingerprint": hashlib.sha256(
                config.embedding_fingerprint.encode()
            ).hexdigest(),
        }.items()
    )


def _stored_document(
    source: sqlite3.Connection, document: Document, dimensions: int
) -> tuple[tuple[object, ...], list[tuple[object, ...]]] | None:
    metadata = json.dumps(document.metadata.__dict__, sort_keys=True)
    expected_document = (
        document.document_ref,
        document.relative_path,
        document.source_hash,
        metadata,
    )
    row = source.execute(
        "SELECT document_ref, relative_path, source_hash, metadata FROM documents "
        "WHERE relative_path = ?",
        (document.relative_path,),
    ).fetchone()
    passages = source.execute(
        "SELECT ref, document_ref, relative_path, heading_path, start_line, end_line, "
        "text, source_hash, metadata, search_text, vector FROM passages "
        "WHERE document_ref = ? ORDER BY ref",
        (document.document_ref,),
    ).fetchall()
    expected_passages = sorted(
        (
            passage.ref,
            passage.document_ref,
            passage.relative_path,
            json.dumps(passage.heading_path),
            passage.start_line,
            passage.end_line,
            passage.text,
            passage.source_hash,
            metadata,
            passage.search_text,
        )
        for passage in document.passages
    )
    if (
        row != expected_document
        or len(passages) != len(expected_passages)
        or any(
            stored[:-1] != expected
            or not isinstance(stored[-1], bytes)
            or len(stored[-1]) != dimensions * 4
            for stored, expected in zip(passages, expected_passages, strict=True)
        )
    ):
        return None
    return row, passages


def _reuse_document(
    source: sqlite3.Connection, target: sqlite3.Connection, document: Document, dimensions: int
) -> bool:
    stored = _stored_document(source, document, dimensions)
    if stored is None:
        return False
    row, passages = stored
    target.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", row)
    target.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", passages)
    return True


def _validate_candidate(
    database: sqlite3.Connection,
    config: BuildConfig,
    documents: Sequence[Document],
    manifest: list[tuple[str, str]],
    metadata: dict[str, str],
) -> None:
    counts = (
        database.execute("SELECT count(*) FROM manifest").fetchone()[0],
        database.execute("SELECT count(*) FROM documents").fetchone()[0],
        database.execute("SELECT count(*) FROM passages").fetchone()[0],
    )
    has_corpus_stats = (
        database.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='corpus_stats'"
        ).fetchone()[0]
        > 0
    )
    if (
        database.execute("PRAGMA integrity_check").fetchone() != ("ok",)
        or database.execute("PRAGMA foreign_key_check").fetchone() is not None
        or counts != (len(manifest), len(documents), sum(len(item.passages) for item in documents))
        or (
            has_corpus_stats
            and dict(database.execute("SELECT key, num_value FROM corpus_stats").fetchall()).get(
                "total_documents"
            )
            != float(counts[2])
        )
        or database.execute(
            "SELECT relative_path, source_hash FROM manifest ORDER BY relative_path"
        ).fetchall()
        != manifest
        or dict(database.execute("SELECT key, value FROM index_meta")) != metadata
        or any(_stored_document(database, document, config.embedding_dimensions) is None for document in documents)
    ):
        raise IndexLifecycleError("invalid_candidate")


def _activation_metadata(database: sqlite3.Connection, config: BuildConfig) -> dict[str, str]:
    try:
        metadata = dict(database.execute("SELECT key, value FROM index_meta"))
    except sqlite3.DatabaseError as error:
        raise IndexLifecycleError("invalid_candidate") from error
    expected = {
        "schema_version": str(config.schema_version),
        "parser_version": config.parser_version,
        "ref_version": REF_VERSION,
        "service_version": config.service_version,
        "mcp_range": config.mcp_range,
        "embedding_identity": config.embedding_identity,
        "embedding_fingerprint": hashlib.sha256(config.embedding_fingerprint.encode()).hexdigest(),
        "ranking_config": config.ranking_config,
        "policy_hash": hashlib.sha256(config.policy_path.read_bytes()).hexdigest(),
    }
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise IndexLifecycleError("incompatible_candidate")
    required = set(expected) | {
        "corpus_manifest_hash",
        "build_id",
        "created_at",
        "validation_state",
    }
    try:
        created = datetime.fromisoformat(metadata["created_at"])
    except (KeyError, ValueError) as error:
        raise IndexLifecycleError("invalid_candidate") from error
    if (
        set(metadata) != required
        or not metadata["build_id"]
        or created.tzinfo is None
        or metadata["validation_state"] != "candidate"
    ):
        raise IndexLifecycleError("invalid_candidate")
    return metadata


def _validate_database(
    database: sqlite3.Connection, config: BuildConfig, policy: CorpusPolicy
) -> dict[str, str]:
    try:
        metadata = _activation_metadata(database, config)
        documents = [parse_document(path, config.root, policy) for path in policy.discover(config.root)]
        manifest = sorted((item.relative_path, item.source_hash) for item in documents)
        expected = _metadata(config, _hash_records(manifest), metadata["build_id"])
        expected["created_at"] = metadata["created_at"]
        _validate_candidate(database, config, documents, manifest, expected)
        _representative_queries(database)
        return metadata
    except sqlite3.DatabaseError as error:
        raise IndexLifecycleError("invalid_candidate") from error


def _validate_stored(database: sqlite3.Connection, config: BuildConfig) -> dict[str, str]:
    return _validate_database(database, config, CorpusPolicy.load(config.policy_path))


def _representative_queries(database: sqlite3.Connection) -> None:
    samples = database.execute(
        "SELECT ref, document_ref, relative_path, source_hash FROM passages ORDER BY ref LIMIT 3"
    ).fetchall()
    if not samples:
        raise IndexLifecycleError("representative_query_failed")
    for sample in samples:
        row = database.execute(
            "SELECT p.ref, p.document_ref, p.relative_path, p.source_hash FROM passages p "
            "JOIN documents d ON d.document_ref = p.document_ref "
            "JOIN manifest m ON m.relative_path = p.relative_path WHERE p.ref = ?",
            (sample[0],),
        ).fetchone()
        if row != sample:
            raise IndexLifecycleError("representative_query_failed")


def validate_candidate(
    database: sqlite3.Connection, config: BuildConfig, policy: CorpusPolicy
) -> dict[str, str]:
    return _validate_database(database, config, policy)


# The corpus index's pointer entries name a sqlite file and its build id; `database` is the key
# whose value must stay a bare filename. Everything else about the pointer is generic -- see
# `pointer.py`.
_POINTER_ENTRY_KEYS = frozenset({"database", "build_id"})


def _read_pointer(pointer: Path) -> dict[str, Any]:
    return read_pointer(
        pointer, entry_keys=_POINTER_ENTRY_KEYS, name_key="database", error=IndexLifecycleError
    )


_write_pointer = write_pointer


def _entry(path: Path, metadata: dict[str, str]) -> dict[str, str]:
    return {"database": path.name, "build_id": metadata["build_id"]}


def active_database(pointer: Path) -> Path | None:
    """The generation the pointer currently calls active, for `build_candidate` to reuse rows from
    -- or `None` when there is nothing to reuse.

    Reuse is an optimisation, never a precondition, so every way of not finding a previous index is
    answered with `None` rather than an exception: a first build has no pointer at all, and
    `index-prune` may unlink the file this pointer names while a build is running, because builds
    deliberately hold no lock across the embedding phase (see `_prune_locked`). Whoever asks this
    question is about to spend minutes embedding a corpus; the one outcome it must never produce is
    a build that fails because the *previous* build went missing.

    `open_snapshot` is the wrong instrument here and was not used: it opens a connection and raises
    `index_not_built`, which is the right answer for a reader and a fatal one for a builder that
    only wanted to skip re-embedding. Compatibility is not decided here either -- `_compatible`
    already refuses a snapshot built under another model, parser or schema, and `_validate_candidate`
    re-verifies every row that survives that."""
    try:
        entry = _read_pointer(pointer)["active"]
    except (IndexLifecycleError, OSError):
        return None
    previous = pointer.parent / entry["database"]
    return previous if previous.is_file() else None


_WINDOWS = os.name == "nt"


def _open_descriptor(path: Path, file_descriptor: int) -> sqlite3.Connection:
    """Open the ALREADY-VALIDATED file behind `file_descriptor`, never a fresh interpretation of a
    name. Both platforms deliver that; they cannot deliver it the same way.

    POSIX cannot pin a name -- any process may rename over it at any moment -- so the only way to be
    sure SQLite opens what `_controlled_file` checked is to hand it the descriptor itself, through
    `/dev/fd`. The path is untrustworthy by construction, so it goes unused.

    Windows has no `/dev/fd` and no way to give SQLite an open handle, but it does not need one: the
    descriptor from `winfs.open_pinned` was opened WITHOUT share-delete, which makes this name
    un-renameable and un-deletable for as long as it is held. Measured: `os.replace`, `os.rename`
    and `os.unlink` against a pinned path all fail. So here the path is the trustworthy thing, and
    `file_descriptor` is what goes unused -- it is still required as an argument because it is the
    live pin, and passing it keeps the caller from closing it while this connection is open.

    Same guarantee, obtained from opposite ends: POSIX distrusts the name and passes the file;
    Windows holds the name and can therefore trust it."""
    if _WINDOWS:  # pragma: no cover -- Windows-only
        # `?` and `#` would terminate the path component of a SQLite URI. Nothing else needs
        # escaping: a drive letter and spaces are already accepted verbatim (measured).
        location = path.as_posix().replace("?", "%3f").replace("#", "%23")
    else:
        location = f"/dev/fd/{file_descriptor}"
    database = sqlite3.connect(f"file:{location}?mode=ro&immutable=1", uri=True, check_same_thread=False)
    database.execute("PRAGMA query_only = ON")
    return database


def _controlled_file(pointer: Path, name: str) -> tuple[Path, int, tuple[int, int, int, int]]:
    return controlled_file(pointer, name, error=IndexLifecycleError)


_identity_matches = identity_matches


def _validated_entry(
    pointer: Path, entry: dict[str, str], config: BuildConfig, *, canonical: bool = True
) -> tuple[Path, dict[str, str]]:
    path, descriptor, identity = _controlled_file(pointer, entry["database"])
    try:
        # `closing`, not the connection's own `with`: sqlite3.Connection.__exit__ commits or rolls
        # back and leaves the handle OPEN. On POSIX that is an invisible descriptor leak, because
        # unlink succeeds against an open file anyway. On Windows it is not invisible at all --
        # SQLite opens without share-delete, so a connection nobody closed makes the file
        # undeletable, and `index-prune` fails with WinError 32 against a generation this same
        # process finished reading. Every use here is read-only, so there is no transaction to
        # keep; closing is the whole intent.
        with closing(_open_descriptor(path, descriptor)) as database:
            metadata = (
                _validate_stored(database, config)
                if canonical else _activation_metadata(database, config)
            )
        if not _identity_matches(path, identity):
            raise IndexLifecycleError("active_target_changed_during_validation")
        return path, metadata
    finally:
        os.close(descriptor)


_activation_lock = activation_lock
_serialized = serialized


def _open_entry(pointer: Path, entry: dict[str, str], config: BuildConfig) -> ActiveSnapshot:
    descriptor = -1
    database: sqlite3.Connection | None = None
    try:
        path, descriptor, identity = _controlled_file(pointer, entry["database"])
        database = _open_descriptor(path, descriptor)
        metadata = _validate_stored(database, config)
        if metadata["build_id"] != entry["build_id"]:
            raise IndexLifecycleError("invalid_active_target")
        if not _identity_matches(path, identity):
            raise IndexLifecycleError("invalid_active_target")
        return ActiveSnapshot(path, metadata["build_id"], database)
    except IndexLifecycleError as error:
        if database is not None:
            database.close()
        if error.code == "invalid_active_target":
            raise
        raise IndexLifecycleError("invalid_active_target") from error
    except (OSError, sqlite3.DatabaseError) as error:
        if database is not None:
            database.close()
        raise IndexLifecycleError("invalid_active_target") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def snapshot_active(pointer: Path, config: BuildConfig) -> ActiveSnapshot:
    value = _read_pointer(pointer)
    return _open_entry(pointer, value["active"], config)


@_serialized(1)
def promote_candidate(
    candidate: Path, pointer: Path, config: BuildConfig, policy: CorpusPolicy, *, retain: int = 2
) -> ActivationResult:
    if retain < 1 or candidate.parent.resolve() != pointer.parent.resolve():
        raise IndexLifecycleError("invalid_activation_path")
    path, descriptor, identity = _controlled_file(pointer, candidate.name)
    database: sqlite3.Connection | None = None
    try:
        database = _open_descriptor(path, descriptor)
        metadata = validate_candidate(database, config, policy)
        if not _identity_matches(path, identity):
            raise IndexLifecycleError("candidate_changed_during_validation")
        flushed = flush_validated(path, descriptor)
        retained: list[dict[str, str]] = []
        retention_discarded = False
        if pointer.exists():
            current = _read_pointer(pointer)
            current_path, current_descriptor, current_identity = _controlled_file(
                pointer, current["active"]["database"]
            )
            try:
                # `closing` for the reason recorded at `_validated_entry`: the connection's own
                # `with` does not close it, and the outgoing generation is exactly the file
                # `index-prune` will be asked to remove next.
                with closing(_open_descriptor(current_path, current_descriptor)) as current_database:
                    try:
                        current_metadata = _activation_metadata(current_database, config)
                    except IndexLifecycleError:
                        # The outgoing index was built under a different config; an edited
                        # `policy.yaml`, or a second project sharing this directory, is the
                        # ordinary way to arrive here, and `policy_hash` can then never match by
                        # construction. It is opened for one purpose -- to become a rollback
                        # entry -- and `rollback_active` re-validates that entry against whatever
                        # config is in force when it runs. An entry that cannot be validated now
                        # is therefore a return path that would refuse to activate later, so
                        # retaining it would record a rollback that does not exist. Refusing the
                        # promotion instead lets a superseded generation veto a candidate that
                        # already validated on line 475, which leaves no way to index at all and
                        # no documented way out. Drop it, keep the invariant that every retained
                        # entry is activatable, and disclose the loss.
                        current_metadata = None
                if not _identity_matches(current_path, current_identity):
                    raise IndexLifecycleError("invalid_active_target")
            finally:
                os.close(current_descriptor)
            if current_metadata is None:
                # Anything older was built under a config at least as distant, so it cannot be
                # activatable either; keeping it would rebuild the same false promise one deep.
                retention_discarded = True
            else:
                retained = [_entry(current_path, current_metadata), *current["retained"]]
        active = _entry(path, metadata)
        retained = [item for item in retained if item != active][:retain]
        # `_write_pointer` first, deliberately: the pointer write must happen whatever the flush
        # reported, and only then does reduced durability fold into the same flag.
        durable = _write_pointer(pointer, active, retained) and flushed
        return ActivationResult(path, metadata["build_id"], durable, retention_discarded)
    finally:
        if database is not None:
            database.close()
        os.close(descriptor)


# Only ever the shape `run_index` itself writes: `candidate-{uuid4().hex}.sqlite3`. This is the
# one path in the package that unlinks a file, so it is spelled as an exact form rather than a
# glob -- `active.json`, `build-config.json`, the lock, and anything an operator put in the data
# directory cannot be named by it, whatever the pointer says.
_GENERATION_NAME = re.compile(r"candidate-[0-9a-f]{32}\.sqlite3")


def prune_generations(pointer: Path) -> tuple[Path, ...]:
    """Delete every index generation the pointer does not reference; return what was removed.

    Promotion leaves the outgoing index on disk on purpose -- nothing here removes a file it did
    not create -- and since a promotion under a changed policy now drops that generation instead
    of retaining it, they accumulate with no way to name them. This is that way.

    The directory and pointer checks happen BEFORE the lock, for the reason `prune_skillset`
    records: the lock file is created beside the pointer, so on an installation that never indexed
    anything the lock itself would raise a bare `FileNotFoundError` rather than a typed refusal a
    caller can act on. Refusing when the pointer is missing also matters more here than there --
    with nothing to read, every generation would look unreferenced.
    """
    if not pointer.parent.is_dir() or not pointer.exists():
        raise IndexLifecycleError("index_not_built")
    return _prune_locked(pointer)


@_serialized(0)
def _prune_locked(pointer: Path) -> tuple[Path, ...]:
    """Serialized on the pointer, so a promotion cannot retain a generation between the moment the
    inventory is taken and the moment it is unlinked.

    What the lock does NOT cover, stated rather than discovered: `build_candidate` writes its file
    outside any lock -- that is the embedding phase, and holding the activation lock across it
    would block every read for the length of a build. So a prune racing a build in the same data
    directory can remove that build's candidate before it is ever promoted. Mutation here is a
    command a human runs, `skill-prune` carries the identical exposure, and the failure is a
    rebuild rather than a loss: the corpus is the source, the index is derived.
    """
    value = _read_pointer(pointer)
    referenced = {value["active"]["database"], *(entry["database"] for entry in value["retained"])}
    removed: list[Path] = []
    for path in sorted(pointer.parent.iterdir()):
        if path.name in referenced or not _GENERATION_NAME.fullmatch(path.name):
            continue
        path.unlink()
        removed.append(path)
    return tuple(removed)


@_serialized(0)
def rollback_active(pointer: Path, config: BuildConfig) -> ActivationResult:
    value = _read_pointer(pointer)
    if not value["retained"]:
        raise IndexLifecycleError("no_retained_index")
    selected_path, metadata = _validated_entry(pointer, value["retained"][0], config)
    selected = _entry(selected_path, metadata)
    current_path, current_metadata = _validated_entry(
        pointer, value["active"], config, canonical=False
    )
    current = _entry(current_path, current_metadata)
    durable = _write_pointer(pointer, selected, [current, *value["retained"][1:]])
    return ActivationResult(selected_path, metadata["build_id"], durable)


@_serialized(0)
def recover_active(pointer: Path, config: BuildConfig) -> ActivationResult:
    value = _read_pointer(pointer)
    entries = [value["active"], *value["retained"]]
    valid: list[dict[str, str]] = []
    for entry in entries:
        try:
            path, metadata = _validated_entry(pointer, entry, config)
            valid.append(_entry(path, metadata))
        except IndexLifecycleError:
            continue
    if not valid:
        raise IndexLifecycleError("no_recoverable_index")
    durable = _write_pointer(pointer, valid[0], valid[1:3])
    path = pointer.parent / valid[0]["database"]
    return ActivationResult(path, valid[0]["build_id"], durable)


def _build_lineage_records(
    documents: Sequence[Document],
) -> list[tuple[str, str, str | None, str]]:
    lookup: dict[str, str] = {}
    for doc in documents:
        lookup[doc.document_ref] = doc.document_ref
        lookup[doc.relative_path] = doc.document_ref
        lookup[Path(doc.relative_path).name] = doc.document_ref
        lookup[Path(doc.relative_path).stem] = doc.document_ref
        if doc.metadata.commit:
            commit = doc.metadata.commit.lower()
            lookup[commit] = doc.document_ref
            if len(commit) >= 7:
                lookup[commit[:7]] = doc.document_ref
                lookup[commit[:8]] = doc.document_ref
                lookup[commit[:12]] = doc.document_ref

    records: list[tuple[str, str, str | None, str]] = []
    for doc in documents:
        for relation, targets in (
            ("supersedes", doc.metadata.supersedes),
            ("deprecates", doc.metadata.deprecates),
            ("amends", doc.metadata.amends),
        ):
            for target in targets:
                target_clean = target.lower()
                pred_ref = lookup.get(target_clean)
                if not pred_ref and len(target_clean) >= 7:
                    for k, ref in lookup.items():
                        if k.startswith(target_clean) or target_clean.startswith(k):
                            pred_ref = ref
                            break
                records.append((doc.document_ref, target_clean, pred_ref, relation))
    return records


def _detect_lineage_cycles(lineage_records: list[tuple[str, str, str | None, str]]) -> None:
    adj: dict[str, list[str]] = {}
    for succ, target, pred_ref, rel in lineage_records:
        if rel == "supersedes":
            pred = pred_ref or target
            adj.setdefault(succ, []).append(pred)

    visited: set[str] = set()
    visiting: set[str] = set()

    def dfs(node: str) -> None:
        if node in visiting:
            raise IndexLifecycleError("lineage_cycle_detected")
        if node in visited:
            return
        visiting.add(node)
        for neighbor in adj.get(node, []):
            dfs(neighbor)
        visiting.remove(node)
        visited.add(node)

    for start_node in list(adj):
        if start_node not in visited:
            dfs(start_node)


def _build_premise_and_alternative_records(
    documents: Sequence[Document],
) -> tuple[
    list[tuple[str, str, str, str | None, str | None, str, str | None]],
    list[tuple[str, str, str, str, str]],
    list[DroppedPremise],
]:
    # Trust tiers (accepted design, premise-id-collision): a repository-authored document (the
    # ordinary corpus tree, or a document `gitcorpus` derives from a commit) outranks a GitHub
    # document, because a GitHub issue/PR body is text whoever opened it chose, not text the
    # repository owner wrote (`SourceMetadata.source`, set only by `corpus._metadata` from the
    # `bruriah_source` marker `github_corpus._render_document` writes). A GitHub declaration for an
    # id a repository document already claims is dropped, never merged in -- see `DroppedPremise`
    # for how that drop is reported. Two repository documents declaring the same id is instead a
    # corpus authoring mistake with no safe automatic resolution, so it fails the build. The same
    # boundary applies to `invalidated_premises` below: a GitHub-tier document can never flip the
    # status of a premise a repository-authored document declared, defense in depth against the
    # same attacker who can open an issue.
    repo_premises: dict[str, dict[str, Any]] = {}
    repo_declaring_path: dict[str, str] = {}
    github_candidates: dict[str, list[dict[str, Any]]] = {}

    for doc in documents:
        tier = "github" if doc.metadata.source == "github" else "repository"
        for p in doc.metadata.premises:
            pid = p.get("id")
            if not pid:
                continue
            record = {
                "premise_id": pid,
                "statement": p.get("statement", ""),
                "status": p.get("status", "active"),
                "invalidated_by": p.get("invalidated_by"),
                "rationale": p.get("rationale"),
                "document_ref": doc.document_ref,
                "invalidation_document_ref": None,
                "relative_path": doc.relative_path,
            }
            if tier == "repository":
                if pid in repo_premises:
                    raise IndexLifecycleError(
                        f"duplicate_premise_id:{pid}:{repo_declaring_path[pid]}:{doc.relative_path}"
                    )
                repo_premises[pid] = record
                repo_declaring_path[pid] = doc.relative_path
            else:
                record["github_issue"] = doc.metadata.github_issue
                github_candidates.setdefault(pid, []).append(record)

    premises_map: dict[str, dict[str, Any]] = dict(repo_premises)
    dropped: list[DroppedPremise] = []

    for pid, candidates in github_candidates.items():
        # Deterministic "lowest issue number wins": documents are already in corpus path order
        # (`corpus.CorpusPolicy.discover`), which is the tie-break for two candidates that somehow
        # carry the same (or no) issue number -- never left to dict/insertion order alone.
        ranked = sorted(
            range(len(candidates)),
            key=lambda i: (
                candidates[i]["github_issue"]
                if candidates[i]["github_issue"] is not None
                else float("inf"),
                i,
            ),
        )
        winner = candidates[ranked[0]]
        for loser_index in ranked[1:]:
            loser = candidates[loser_index]
            dropped.append(DroppedPremise(
                premise_id=pid,
                document_ref=loser["document_ref"],
                relative_path=loser["relative_path"],
                reason="shadowed_by_lower_github_issue",
            ))
        if pid in premises_map:
            dropped.append(DroppedPremise(
                premise_id=pid,
                document_ref=winner["document_ref"],
                relative_path=winner["relative_path"],
                reason="shadowed_by_repository_premise",
            ))
        else:
            premises_map[pid] = winner

    for doc in documents:
        for inv_id in doc.metadata.invalidated_premises:
            inv_id_clean = inv_id.strip()
            # Same trust boundary as a premise declaration itself: a GitHub-tier document can
            # invalidate a GitHub-tier or synthesized premise, but never flip the status of a
            # premise a repository-authored document declared. `repo_premises` still names exactly
            # those ids, unaffected by any mutation a later legitimate invalidation makes below.
            if doc.metadata.source == "github" and inv_id_clean in repo_premises:
                dropped.append(DroppedPremise(
                    premise_id=inv_id_clean,
                    document_ref=doc.document_ref,
                    relative_path=doc.relative_path,
                    reason="github_invalidation_ignored",
                ))
                continue
            inv_by = doc.metadata.commit or doc.document_ref
            if inv_id_clean in premises_map:
                premises_map[inv_id_clean]["status"] = "invalidated"
                premises_map[inv_id_clean]["invalidated_by"] = inv_by
                premises_map[inv_id_clean]["invalidation_document_ref"] = doc.document_ref
            else:
                premises_map[inv_id_clean] = {
                    "premise_id": inv_id_clean,
                    "statement": f"Premise {inv_id_clean}",
                    "status": "invalidated",
                    "invalidated_by": inv_by,
                    "rationale": "Invalidated by subsequent decision",
                    "document_ref": doc.document_ref,
                    "invalidation_document_ref": doc.document_ref,
                }

    premise_rows = [
        (
            p["premise_id"],
            p["statement"],
            p["status"],
            p["invalidated_by"],
            p["rationale"],
            p["document_ref"],
            p.get("invalidation_document_ref"),
        )
        for p in premises_map.values()
    ]

    alt_rows = []
    for doc in documents:
        seen_names: set[str] = set()
        for alt in doc.metadata.alternatives:
            name = alt.get("name")
            if not name:
                continue
            if name in seen_names:
                raise IndexLifecycleError(f"duplicate_alternative_name:{name}:{doc.relative_path}")
            seen_names.add(name)
            disposition = alt.get("disposition", "rejected")
            reason = alt.get("reason", "")
            premises_list = alt.get("premises", [])
            alt_rows.append((
                name,
                disposition,
                reason,
                json.dumps(premises_list),
                doc.document_ref,
            ))

    return premise_rows, alt_rows, dropped


def _build_lexical_index(database: sqlite3.Connection) -> None:
    rows = database.execute(
        "SELECT ref, search_text FROM passages ORDER BY ref"
    ).fetchall()
    total_documents = len(rows)
    if total_documents == 0:
        database.execute(
            "INSERT INTO corpus_stats VALUES ('total_documents', 0.0, NULL)"
        )
        database.execute(
            "INSERT INTO corpus_stats VALUES ('average_length', 0.0, NULL)"
        )
        database.execute(
            "INSERT INTO corpus_stats VALUES ('corpus_language', NULL, NULL)"
        )
        return

    df: dict[str, int] = defaultdict(int)
    postings: list[tuple[str, str, int, int]] = []
    total_tokens = 0

    for ref, search_text in rows:
        tokens = language.tokenize(search_text)
        doc_length = len(tokens)
        total_tokens += doc_length
        if not tokens:
            continue
        counts = Counter(tokens)
        for term, freq in counts.items():
            df[term] += 1
            postings.append((term, ref, freq, doc_length))

    average_length = total_tokens / total_documents
    dominant_language = language.dominant(text for _, text in rows)

    database.execute(
        "INSERT INTO corpus_stats VALUES ('total_documents', ?, NULL)",
        (float(total_documents),),
    )
    database.execute(
        "INSERT INTO corpus_stats VALUES ('average_length', ?, NULL)",
        (average_length,),
    )
    database.execute(
        "INSERT INTO corpus_stats VALUES ('corpus_language', NULL, ?)",
        (dominant_language,),
    )
    if df:
        database.executemany(
            "INSERT INTO term_df VALUES (?, ?)",
            sorted(df.items()),
        )
    if postings:
        postings.sort(key=lambda item: (item[0], item[1]))
        database.executemany(
            "INSERT INTO term_postings VALUES (?, ?, ?, ?)",
            postings,
        )


def build_candidate(
    config: BuildConfig,
    destination: Path,
    policy: CorpusPolicy,
    embedder: Embedder,
    *,
    previous: Path | None = None,
) -> BuildResult:
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    )
    temporary = Path(handle.name)
    handle.close()
    database: sqlite3.Connection | None = None
    prior: sqlite3.Connection | None = None
    try:
        documents = [parse_document(path, config.root, policy) for path in policy.discover(config.root)]
        manifest = sorted((item.relative_path, item.source_hash) for item in documents)
        manifest_hash = _hash_records(manifest)
        build_id = str(uuid.uuid4())
        database = sqlite3.connect(temporary)
        database.executescript(SCHEMA)
        database.executemany("INSERT INTO manifest VALUES (?, ?)", manifest)
        if previous and previous.is_file():
            # It was a file a moment ago; an `index-prune` racing this build may have unlinked it
            # since, and builds hold no lock across the embedding phase precisely so that prune can
            # run. A source that cannot be opened is a source that is not reused -- never a build
            # that fails for want of the index it was going to replace.
            try:
                prior = open_candidate(previous)
            except sqlite3.Error:
                prior = None
            if prior is not None and not _compatible(prior, config):
                prior.close()
                prior = None
        reused = 0
        passage_count = 0
        for document in documents:
            if prior and _reuse_document(prior, database, document, config.embedding_dimensions):
                reused += 1
                passage_count += len(document.passages)
                continue
            texts = [
                f"{config.passage_prefix}{passage.search_text}"
                for passage in document.passages
            ]
            vectors = embedder(texts)
            if len(vectors) != len(texts) or any(
                len(vector) != config.embedding_dimensions * 4 for vector in vectors
            ):
                raise ValueError("invalid_embedding_output")
            doc_metadata = json.dumps(document.metadata.__dict__, sort_keys=True)
            database.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                (document.document_ref, document.relative_path, document.source_hash, doc_metadata),
            )
            database.executemany(
                "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        passage.ref,
                        passage.document_ref,
                        passage.relative_path,
                        json.dumps(passage.heading_path),
                        passage.start_line,
                        passage.end_line,
                        passage.text,
                        passage.source_hash,
                        doc_metadata,
                        passage.search_text,
                        vector,
                    )
                    for passage, vector in zip(document.passages, vectors, strict=True)
                ],
            )
            passage_count += len(document.passages)
        lineage_records = _build_lineage_records(documents)
        _detect_lineage_cycles(lineage_records)
        database.executemany("INSERT INTO lineage VALUES (?, ?, ?, ?)", lineage_records)
        premise_records, alternative_records, dropped_premises = _build_premise_and_alternative_records(documents)
        if premise_records:
            database.executemany("INSERT INTO premises VALUES (?, ?, ?, ?, ?, ?, ?)", premise_records)
        if alternative_records:
            database.executemany("INSERT INTO alternatives VALUES (?, ?, ?, ?, ?)", alternative_records)
        _build_lexical_index(database)
        index_meta = _metadata(config, manifest_hash, build_id)
        database.executemany("INSERT INTO index_meta VALUES (?, ?)", index_meta.items())
        _validate_candidate(database, config, documents, manifest, index_meta)
        database.commit()
        database.close()
        database = None
        os.replace(temporary, destination)
        return BuildResult(
            destination, build_id, manifest_hash, len(documents), passage_count, reused,
            tuple(dropped_premises),
        )
    except BaseException:
        if database is not None:
            database.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if prior is not None:
            prior.close()
