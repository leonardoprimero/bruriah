#!/usr/bin/env python3
"""Build and verify the versioned legacy Cerebro recovery baseline."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import tomllib

import sqlite_vec


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
VAULT = REPOSITORY / "Cerebro-IA"
LIVE_DB = ROOT / "cerebro.db"
LEGACY = ROOT / "cerebro.py"
LOCK = ROOT / "uv.lock"
REGISTERED_ENV = ROOT / ".venv"
DEFAULT_BASELINE = ROOT / "recovery/legacy-baseline-v1.json"
MODEL_CACHE_NAME = "models--qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def corpus_state() -> dict[str, object]:
    files = []
    paths = sorted(
        (path for path in VAULT.rglob("*.md") if path.is_file()),
        key=lambda path: path.relative_to(VAULT).as_posix().encode(),
    )
    for path in paths:
        files.append(
            {
                "path": path.relative_to(VAULT).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return {"count": len(files), "manifest_sha256": hashlib.sha256(canonical(files)).hexdigest()}


def open_database(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.enable_load_extension(True)
    sqlite_vec.load(database)
    database.enable_load_extension(False)
    return database


def database_state(path: Path) -> dict[str, object]:
    database = open_database(path)
    try:
        schema = [
            row[0]
            for row in database.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            )
        ]
        rows = database.execute(
            "SELECT id,file,heading,text FROM chunks ORDER BY id"
        ).fetchall()
        vectors = database.execute(
            "SELECT rowid,embedding FROM vec_chunks ORDER BY rowid"
        ).fetchall()
        vector_bytes = b"".join(struct.pack(">Q", rowid) + bytes(blob) for rowid, blob in vectors)
        return {
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
            "integrity": database.execute("PRAGMA integrity_check").fetchone()[0],
            "schema_sha256": hashlib.sha256(canonical(schema)).hexdigest(),
            "counts": {
                table: database.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("chunks", "chunks_fts", "vec_chunks")
            },
            "source_rows_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
            "vector_rows_sha256": hashlib.sha256(vector_bytes).hexdigest(),
            "vector_dimensions": sorted({len(blob) // 4 for _, blob in vectors}),
        }
    finally:
        database.close()


def lock_state() -> dict[str, object]:
    packages = tomllib.loads(LOCK.read_text())["package"]
    return {
        "sha256": sha256_file(LOCK),
        "package_count": len(packages),
        "package_metadata_sha256": hashlib.sha256(canonical(packages)).hexdigest(),
    }


def model_state() -> dict[str, object]:
    cache = Path(
        os.environ.get(
            "FASTEMBED_CACHE_PATH", Path(tempfile.gettempdir()) / "fastembed_cache"
        )
    )
    root = cache / MODEL_CACHE_NAME
    snapshot = (root / "refs/main").read_text().strip()
    snapshot_root = root / "snapshots" / snapshot
    files = [
        {
            "path": path.relative_to(snapshot_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(snapshot_root.rglob("*"))
        if path.is_file()
    ]
    return {
        "id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "artifact_repo": "qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q",
        "snapshot": snapshot,
        "pooling": "mean attention-mask pooling (FastEmbed 0.8.0 PooledEmbedding)",
        "dimensions": 384,
        "files_manifest_sha256": hashlib.sha256(canonical(files)).hexdigest(),
        "onnx_sha256": sha256_file(snapshot_root / "model_optimized.onnx"),
    }


def runtime_state(names: list[str]) -> dict[str, object]:
    return {
        "python": ".".join(map(str, sys.version_info[:3])),
        "sqlite": sqlite3.sqlite_version,
        "packages": {name: importlib.metadata.version(name) for name in names},
    }


def load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_cerebro_baseline", LEGACY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def golden_state(database_path: Path, queries: list[str]) -> dict[str, list[str]]:
    legacy = load_legacy()
    legacy.DB = str(database_path)
    database = legacy.connect()
    try:
        return {
            query: [f"{hit['file']}#{hit['heading']}" for hit in legacy.search(database, query, k=3)]
            for query in queries
        }
    finally:
        database.close()


def build_candidate(path: Path) -> None:
    if REGISTERED_ENV.resolve() == Path(sys.prefix).resolve():
        raise SystemExit("candidate builds must not use the registered .venv")
    if path.exists() or ROOT.resolve() in path.resolve().parents:
        raise SystemExit("candidate path must be new and outside the live directory")
    legacy = load_legacy()
    legacy.DB = str(path)
    legacy.do_index()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--database", type=Path, default=LIVE_DB)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--build-candidate", action="store_true")
    args = parser.parse_args()
    if args.build_candidate:
        if not args.candidate:
            parser.error("--build-candidate requires --candidate")
        build_candidate(args.candidate)
    expected = json.loads(args.baseline.read_text())
    actual = {
        "corpus": corpus_state(),
        "database": database_state(args.database),
        "lock": lock_state(),
        "model": model_state(),
        "runtime": runtime_state(list(expected["runtime"]["packages"])),
        "goldens": golden_state(args.database, list(expected["goldens"])),
    }
    errors = [key for key in actual if actual[key] != expected[key]]
    if args.candidate:
        candidate = database_state(args.candidate)
        if candidate != actual["database"]:
            errors.append("candidate")
    print(json.dumps({"status": "pass" if not errors else "fail", "errors": errors, **actual}, ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
