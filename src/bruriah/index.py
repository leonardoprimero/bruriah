from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .corpus import CorpusPolicy, parse_document
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
        return json.dumps(
            {
                "dimensions": self.embedding_dimensions,
                "fingerprint": json.loads(self.embedding_fingerprint),
                "model": self.embedding_model,
                "revision": self.embedding_revision,
            },
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
    vector BLOB NOT NULL, FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
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
    database = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
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
    source: sqlite3.Connection, document: object, dimensions: int
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
        "text, source_hash, metadata, vector FROM passages WHERE document_ref = ? ORDER BY ref",
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
    source: sqlite3.Connection, target: sqlite3.Connection, document: object, dimensions: int
) -> bool:
    stored = _stored_document(source, document, dimensions)
    if stored is None:
        return False
    row, passages = stored
    target.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", row)
    target.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", passages)
    return True


def _validate_candidate(
    database: sqlite3.Connection,
    config: BuildConfig,
    documents: list[object],
    manifest: list[tuple[str, str]],
    metadata: dict[str, str],
) -> None:
    counts = (
        database.execute("SELECT count(*) FROM manifest").fetchone()[0],
        database.execute("SELECT count(*) FROM documents").fetchone()[0],
        database.execute("SELECT count(*) FROM passages").fetchone()[0],
    )
    if (
        database.execute("PRAGMA integrity_check").fetchone() != ("ok",)
        or database.execute("PRAGMA foreign_key_check").fetchone() is not None
        or counts != (len(manifest), len(documents), sum(len(item.passages) for item in documents))
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


def _read_pointer(pointer: Path) -> dict[str, object]:
    return read_pointer(
        pointer, entry_keys=_POINTER_ENTRY_KEYS, name_key="database", error=IndexLifecycleError
    )


_write_pointer = write_pointer


def _entry(path: Path, metadata: dict[str, str]) -> dict[str, str]:
    return {"database": path.name, "build_id": metadata["build_id"]}


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
    database = sqlite3.connect(f"file:{location}?mode=ro&immutable=1", uri=True)
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
            prior = open_candidate(previous)
            if not _compatible(prior, config):
                prior.close()
                prior = None
        reused = 0
        passage_count = 0
        for document in documents:
            if prior and _reuse_document(prior, database, document, config.embedding_dimensions):
                reused += 1
                passage_count += len(document.passages)
                continue
            texts = [passage.text for passage in document.passages]
            vectors = embedder(texts)
            if len(vectors) != len(texts) or any(
                len(vector) != config.embedding_dimensions * 4 for vector in vectors
            ):
                raise ValueError("invalid_embedding_output")
            metadata = json.dumps(document.metadata.__dict__, sort_keys=True)
            database.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                (document.document_ref, document.relative_path, document.source_hash, metadata),
            )
            database.executemany(
                "INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        metadata,
                        vector,
                    )
                    for passage, vector in zip(document.passages, vectors, strict=True)
                ],
            )
            passage_count += len(document.passages)
        metadata = _metadata(config, manifest_hash, build_id)
        database.executemany("INSERT INTO index_meta VALUES (?, ?)", metadata.items())
        _validate_candidate(database, config, documents, manifest, metadata)
        database.commit()
        database.close()
        database = None
        os.replace(temporary, destination)
        return BuildResult(destination, build_id, manifest_hash, len(documents), passage_count, reused)
    except BaseException:
        if database is not None:
            database.close()
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if prior is not None:
            prior.close()
