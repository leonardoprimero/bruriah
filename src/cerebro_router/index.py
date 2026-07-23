from __future__ import annotations

import hashlib
import fcntl
import json
import os
import sqlite3
import stat
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .corpus import CorpusPolicy, parse_document

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


def _read_pointer(pointer: Path) -> dict[str, object]:
    if pointer.is_symlink():
        raise IndexLifecycleError("invalid_active_pointer")
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise IndexLifecycleError("invalid_active_pointer") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "active", "retained"}
        or value["version"] != 1
        or not isinstance(value["active"], dict)
        or not isinstance(value["retained"], list)
    ):
        raise IndexLifecycleError("invalid_active_pointer")
    for entry in [value["active"], *value["retained"]]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"database", "build_id"}
            or not all(isinstance(item, str) and item for item in entry.values())
            or Path(entry["database"]).name != entry["database"]
        ):
            raise IndexLifecycleError("invalid_active_pointer")
    return value


def _write_pointer(
    pointer: Path, active: dict[str, str], retained: list[dict[str, str]]
) -> bool:
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{pointer.name}.", suffix=".tmp",
        dir=pointer.parent, delete=False,
    )
    temporary = Path(handle.name)
    try:
        json.dump({"version": 1, "active": active, "retained": retained}, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(temporary, pointer)
        try:
            directory = os.open(pointer.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            return False
        return True
    except BaseException:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise


def _entry(path: Path, metadata: dict[str, str]) -> dict[str, str]:
    return {"database": path.name, "build_id": metadata["build_id"]}


def _identity(file_descriptor: int) -> tuple[int, int, int, int]:
    value = os.fstat(file_descriptor)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _open_descriptor(file_descriptor: int) -> sqlite3.Connection:
    database = sqlite3.connect(
        f"file:/dev/fd/{file_descriptor}?mode=ro&immutable=1", uri=True
    )
    database.execute("PRAGMA query_only = ON")
    return database


def _controlled_file(pointer: Path, name: str) -> tuple[Path, int, tuple[int, int, int, int]]:
    path = pointer.parent / name
    descriptor = -1
    try:
        if path.is_symlink() or path.resolve(strict=True).parent != pointer.parent.resolve(strict=True):
            raise IndexLifecycleError("invalid_active_target")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        identity = _identity(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise IndexLifecycleError("invalid_active_target")
        return path, descriptor, identity
    except IndexLifecycleError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise IndexLifecycleError("invalid_active_target") from error


def _identity_matches(path: Path, expected: tuple[int, int, int, int]) -> bool:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns) == expected


def _validated_entry(
    pointer: Path, entry: dict[str, str], config: BuildConfig, *, canonical: bool = True
) -> tuple[Path, dict[str, str]]:
    path, descriptor, identity = _controlled_file(pointer, entry["database"])
    try:
        with _open_descriptor(descriptor) as database:
            metadata = (
                _validate_stored(database, config)
                if canonical else _activation_metadata(database, config)
            )
        if not _identity_matches(path, identity):
            raise IndexLifecycleError("active_target_changed_during_validation")
        return path, metadata
    finally:
        os.close(descriptor)


@contextmanager
def _activation_lock(pointer: Path):
    descriptor = os.open(pointer.parent / f".{pointer.name}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _serialized(pointer_position: int):
    def decorate(operation):
        def locked(*args, **kwargs):
            with _activation_lock(args[pointer_position]):
                return operation(*args, **kwargs)
        return locked
    return decorate


def _open_entry(pointer: Path, entry: dict[str, str], config: BuildConfig) -> ActiveSnapshot:
    descriptor = -1
    database: sqlite3.Connection | None = None
    try:
        path, descriptor, identity = _controlled_file(pointer, entry["database"])
        database = _open_descriptor(descriptor)
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
        database = _open_descriptor(descriptor)
        metadata = validate_candidate(database, config, policy)
        if not _identity_matches(path, identity):
            raise IndexLifecycleError("candidate_changed_during_validation")
        os.fsync(descriptor)
        retained: list[dict[str, str]] = []
        if pointer.exists():
            current = _read_pointer(pointer)
            current_path, current_descriptor, current_identity = _controlled_file(
                pointer, current["active"]["database"]
            )
            try:
                with _open_descriptor(current_descriptor) as current_database:
                    current_metadata = _activation_metadata(current_database, config)
                if not _identity_matches(current_path, current_identity):
                    raise IndexLifecycleError("invalid_active_target")
            finally:
                os.close(current_descriptor)
            retained = [_entry(current_path, current_metadata), *current["retained"]]
        active = _entry(path, metadata)
        retained = [item for item in retained if item != active][:retain]
        durable = _write_pointer(pointer, active, retained)
        return ActivationResult(path, metadata["build_id"], durable)
    finally:
        if database is not None:
            database.close()
        os.close(descriptor)


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
