from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from conftest import requires_vault

# Every test here asserts about a resource that is not distributed with this project.
pytestmark = requires_vault
import yaml

from bruriah.corpus import (
    CorpusPolicy,
    CorpusPolicyError,
    RefAliases,
    parse_document,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VAULT_ROOT = REPOSITORY_ROOT / "Cerebro-IA"


def write_policy(path: Path) -> CorpusPolicy:
    path.write_text(
        """version: 1
include:
  - public/**
exclude:
  - '**/.*/**'
  - '**/private/**'
""",
        encoding="utf-8",
    )
    return CorpusPolicy.load(path)


def test_policy_confines_sources_and_rejects_private_and_symlinked_notes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside.md"
    public = root / "public"
    private = public / "private"
    public.mkdir(parents=True)
    private.mkdir()
    outside.write_text("outside secret", encoding="utf-8")
    (public / "allowed.md").write_text("# Allowed\n", encoding="utf-8")
    (private / "secret.md").write_text("private secret", encoding="utf-8")
    sensitive = public / "Sensitive" / "secret.md"
    sensitive.parent.mkdir()
    sensitive.write_text("sensitive secret", encoding="utf-8")
    escaped = public / "escaped.md"
    escaped.symlink_to(outside)
    policy = write_policy(tmp_path / "policy.yaml")

    assert [path.name for path in policy.discover(root)] == ["allowed.md"]
    assert policy.exclusion_reason(private / "secret.md", root) == "excluded_by_policy"
    assert policy.exclusion_reason(sensitive, root) == "excluded_by_policy"
    assert policy.exclusion_reason(escaped, root) == "outside_approved_root"
    with pytest.raises(CorpusPolicyError, match="outside_approved_root"):
        parse_document(escaped, root, policy)


def test_parser_preserves_raw_lines_metadata_and_stable_identity(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    note = root / "public" / "note.md"
    note.parent.mkdir(parents=True)
    raw = b"""---
status: verified
source: Oficial
url: https://example.com/source
verificado: 2026-07-17
---
# Title
First line.

## Details
Exact detail.
"""
    note.write_bytes(raw)
    policy = write_policy(tmp_path / "policy.yaml")

    first = parse_document(note, root, policy)
    second = parse_document(note, root, policy)

    assert note.read_bytes() == raw
    assert first == second
    assert first.source_hash == hashlib.sha256(raw).hexdigest()
    assert first.relative_path == "public/note.md"
    assert first.metadata.status == "verified"
    assert first.metadata.provenance == ("Oficial",)
    assert first.metadata.verification_date == "2026-07-17"
    assert first.metadata.provenance_urls == ("https://example.com/source",)
    assert [(item.start_line, item.end_line, item.text) for item in first.passages] == [
        (7, 9, "# Title\nFirst line.\n\n"),
        (10, 11, "## Details\nExact detail.\n"),
    ]
    assert first.document_ref.startswith("doc:v1:")
    assert all(item.ref.startswith("chunk:v1:") for item in first.passages)

    alias = note.with_name("alias.md")
    alias.symlink_to(note)
    assert parse_document(alias, root, policy) == first


def test_parser_keeps_preamble_and_normalizes_null_status(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    note = root / "public" / "preamble.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(b"---\r\nstatus: null\r\n---\r\nPreamble.\r\n# Heading\r\nBody.\r\n")
    document = parse_document(note, root, write_policy(tmp_path / "policy.yaml"))

    assert document.metadata.status == "unknown"
    assert [(item.start_line, item.end_line, item.text) for item in document.passages] == [
        (4, 4, "Preamble.\r\n"),
        (5, 6, "# Heading\r\nBody.\r\n"),
    ]


def test_duplicate_headings_renames_and_tombstones_are_explicit(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    note = root / "public" / "duplicate.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Same\nOne\n# Same\nTwo\n", encoding="utf-8")
    policy = write_policy(tmp_path / "policy.yaml")
    document = parse_document(note, root, policy)
    refs = [passage.ref for passage in document.passages]
    assert len(refs) == len(set(refs)) == 2

    aliases_path = tmp_path / "ref-aliases.json"
    aliases_path.write_text(
        '{"version":1,"aliases":{"chunk:v1:old":"chunk:v1:new"},'
        '"tombstones":["chunk:v1:gone"]}',
        encoding="utf-8",
    )
    aliases = RefAliases.load(aliases_path)
    assert aliases.resolve("chunk:v1:old") == "chunk:v1:new"
    assert aliases.resolve("chunk:v1:gone") is None
    assert aliases.resolve("chunk:v1:unknown") == "chunk:v1:unknown"
    with pytest.raises(TypeError):
        aliases.aliases["chunk:v1:old"] = "chunk:v1:mutated"


def test_real_corpus_rebuild_is_stable_and_non_destructive() -> None:
    policy = CorpusPolicy.load(REPOSITORY_ROOT / "cerebro-retrieval/corpus-policy.yaml")
    before = {
        path.relative_to(VAULT_ROOT): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VAULT_ROOT.rglob("*.md")
        if path.is_file()
    }
    paths = policy.discover(VAULT_ROOT)
    assert paths

    forward = {document.relative_path: document for document in (
        parse_document(path, VAULT_ROOT, policy) for path in paths
    )}
    reverse = {document.relative_path: document for document in (
        parse_document(path, VAULT_ROOT, policy) for path in reversed(paths)
    )}

    assert forward == reverse
    assert all(path.resolve().is_relative_to(VAULT_ROOT.resolve()) for path in paths)
    provenance_count = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        frontmatter = yaml.safe_load(text.split("---", 2)[1]) or {}
        source = frontmatter.get("source")
        if not source:
            continue
        values = source if isinstance(source, list) else [source]
        expected = tuple(dict.fromkeys(item for item in values if isinstance(item, str)))
        relative = path.resolve().relative_to(VAULT_ROOT.resolve()).as_posix()
        assert forward[relative].metadata.provenance == expected
        provenance_count += 1
    assert provenance_count
    assert {
        path.relative_to(VAULT_ROOT): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VAULT_ROOT.rglob("*.md")
        if path.is_file()
    } == before
