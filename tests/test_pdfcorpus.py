"""Tests for PDF corpus derivation (Issue #5).

Verifies that:
1. PDF derivation converts PDF pages into auditable Markdown documents on disk with
   declared provenance frontmatter (source, page, extractor, source_sha256).
2. Blank or unextractable pages are honestly skipped and reported in coverage statistics.
3. Recursive directories of PDFs are processed deterministically with collision-free filenames.
4. Missing dependencies or invalid files produce clear, actionable SystemExit failures.
5. End-to-end: derived PDF Markdown passes through the standard indexer without a single line
   of indexer modification, preserving the byte-for-byte locator contract.
"""

from __future__ import annotations

import hashlib
from array import array
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
import yaml

import contextlib
import sqlite3

from bruriah import index, pdfcorpus
from bruriah.corpus import CorpusPolicy, parse_document


def _make_pdf(path: Path, pages: list[str]) -> Path:
    """Generate a minimal valid PDF with specified page text streams."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=300, height=300)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 250 Td ({text}) Tj ET".encode("latin1"))
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            page[NameObject("/Contents")] = stream
            page[NameObject("/Resources")] = resources
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        writer.write(f)
    return path


def _fake_embedder_factory(model_name: str):
    def embed(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

    fingerprint = '{"runtime":"fastembed==0.8.0","model":"test"}'
    return embed, fingerprint, 3


def test_pdfcorpus_single_file_extracts_pages_with_provenance(tmp_path: Path) -> None:
    pdf_path = _make_pdf(
        tmp_path / "storage_spec.pdf",
        [
            "Architecture Decision: Use Event Sourcing for auditability.",
            "Storage Engine: PostgreSQL JSONB with write-ahead log.",
        ],
    )
    out_dir = tmp_path / "derived"

    result = pdfcorpus.build(pdf_path, out_dir)

    assert (result.written, result.examined, result.files_examined, result.skipped_empty_pages) == (2, 2, 1, 0)
    assert result.documents == 2
    assert result.pages == 2

    files = sorted(out_dir.glob("*.md"))
    assert len(files) == 2
    assert files[0].name == "storage-spec-p001.md"
    assert files[1].name == "storage-spec-p002.md"

    # Verify frontmatter on page 1
    content_p1 = files[0].read_text(encoding="utf-8")
    assert content_p1.startswith("---\n")
    parts = content_p1.split("---\n")
    frontmatter = yaml.safe_load(parts[1])
    assert frontmatter["source"] == "storage_spec.pdf"
    assert frontmatter["page"] == 1
    assert frontmatter["extractor"].startswith("pypdf==")
    assert frontmatter["source_sha256"] == hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    assert frontmatter["status"] == "active"

    # Verify body
    assert "# storage_spec - Page 1" in parts[2]
    assert "Architecture Decision: Use Event Sourcing for auditability." in parts[2]

    # Verify page 2
    content_p2 = files[1].read_text(encoding="utf-8")
    parts_p2 = content_p2.split("---\n")
    fm_p2 = yaml.safe_load(parts_p2[1])
    assert fm_p2["page"] == 2
    assert "Storage Engine: PostgreSQL JSONB with write-ahead log." in parts_p2[2]


def test_pdfcorpus_skips_blank_or_image_only_pages(tmp_path: Path) -> None:
    pdf_path = _make_pdf(
        tmp_path / "mixed.pdf",
        [
            "Valid page 1 content.",
            "",  # blank separator page
            "Valid page 3 content.",
        ],
    )
    out_dir = tmp_path / "out"

    result = pdfcorpus.build(pdf_path, out_dir)

    assert (result.written, result.examined, result.files_examined, result.skipped_empty_pages) == (2, 3, 1, 1)
    written_files = sorted(out_dir.glob("*.md"))
    assert len(written_files) == 2
    assert written_files[0].name == "mixed-p001.md"
    assert written_files[1].name == "mixed-p003.md"


def test_pdfcorpus_directory_recursive_derivation(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    _make_pdf(docs_dir / "rfc1.pdf", ["RFC 1 Content."])
    _make_pdf(docs_dir / "sub" / "rfc2.pdf", ["RFC 2 Content."])
    # Unrelated non-pdf file should be ignored
    (docs_dir / "notes.txt").write_text("not a pdf", encoding="utf-8")

    out_dir = tmp_path / "corpus"
    result = pdfcorpus.build(docs_dir, out_dir)

    assert result.files_examined == 2
    assert result.written == 2
    names = {p.name for p in out_dir.glob("*.md")}
    assert names == {"rfc1-p001.md", "sub-rfc2-p001.md"}


def test_pdfcorpus_error_handling(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"

    # Non-existent source
    with pytest.raises(SystemExit) as err:
        pdfcorpus.build(tmp_path / "missing.pdf", out_dir)
    assert "source path does not exist" in str(err.value)

    # Non-PDF single file
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("hello", encoding="utf-8")
    with pytest.raises(SystemExit) as err:
        pdfcorpus.build(txt_file, out_dir)
    assert "is not a PDF" in str(err.value)

    # Empty directory with no PDFs
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(SystemExit) as err:
        pdfcorpus.build(empty_dir, out_dir)
    assert "no PDF files found in" in str(err.value)

    # Corrupted PDF
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"not a valid pdf binary structure")
    with pytest.raises(SystemExit) as err:
        pdfcorpus.build(bad_pdf, out_dir)
    assert "failed to read PDF" in str(err.value)


def test_pdfcorpus_missing_pypdf_dependency(monkeypatch, tmp_path: Path) -> None:
    pdf_path = _make_pdf(tmp_path / "doc.pdf", ["Content"])
    monkeypatch.setattr(pdfcorpus, "pypdf", None)

    with pytest.raises(SystemExit) as err:
        pdfcorpus.build(pdf_path, tmp_path / "out")
    assert "pip install 'bruriah[pdf]'" in str(err.value)


def test_pdfcorpus_end_to_end_indexing_and_byte_guarantee(tmp_path: Path) -> None:
    """Proves Issue #5's thesis: PDF derivation leaves the indexer untouched and preserves
    exact byte-for-byte citation locators via `read_evidence`."""
    pdf_path = _make_pdf(
        tmp_path / "security_standard.pdf",
        [
            "Authentication Policy: All API requests require signed Ed25519 headers.\n"
            "Replay attacks must be rejected via monotonic nonces.",
        ],
    )
    corpus_root = tmp_path / "corpus"
    result = pdfcorpus.build(pdf_path, corpus_root)
    assert result.written == 1

    # Write standard policy
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("version: 1\ninclude: ['*.md', '**/*.md']\nexclude: []\n", encoding="utf-8")
    policy = CorpusPolicy.load(policy_path)

    # 1. Zero indexer modification: standard parse_document
    discovered = policy.discover(corpus_root)
    assert len(discovered) == 1
    documents = [parse_document(path, corpus_root, policy) for path in discovered]
    doc = documents[0]
    assert doc.metadata.provenance == ("security_standard.pdf",)
    assert doc.metadata.status == "active"
    assert len(doc.passages) >= 1

    # 2. Build candidate index
    candidate_db = tmp_path / "candidate.db"
    build_cfg = index.BuildConfig(
        root=corpus_root,
        policy_path=policy_path,
        schema_version=1,
        parser_version="corpus-v2",
        service_version="0.1.0",
        mcp_range=">=1.28.1,<2",
        embedding_model="test/minilm",
        embedding_revision="snapshot-a",
        embedding_dimensions=3,
        embedding_fingerprint=(
            '{"artifact":"model.onnx","artifact_sha256":"'
            + "a" * 64
            + '","pooling":"mean","runtime":"fastembed==0.8.0","snapshot":"snapshot-a","source":"example/model"}'
        ),
        ranking_config="rrf-v1",
    )

    def fake_embeddings(texts: list[str]) -> list[bytes]:
        return [array("f", (1.0, 0.0, 0.0)).tobytes() for _ in texts]

    build_res = index.build_candidate(build_cfg, candidate_db, policy, fake_embeddings)
    assert build_res.documents == 1

    # 3. Read exact bytes using locator citation
    passage = doc.passages[0]
    derived_file = corpus_root / "security-standard-p001.md"
    assert derived_file.exists()

    with contextlib.closing(sqlite3.connect(candidate_db)) as conn:
        row = conn.execute(
            "SELECT relative_path, start_line, end_line, text, source_hash FROM passages WHERE ref = ?",
            (passage.ref,),
        ).fetchone()
        assert row is not None
        rel_path, start_line, end_line, text, source_hash = row
        assert rel_path == derived_file.name
        assert start_line == passage.start_line
        assert end_line == passage.end_line
        assert "Authentication Policy: All API requests require signed Ed25519 headers." in text
        # Verify text is exact slice of lines from derived_file
        file_lines = derived_file.read_text(encoding="utf-8").splitlines(keepends=True)
        expected_slice = "".join(file_lines[start_line - 1 : end_line])
        assert text == expected_slice
        assert source_hash == hashlib.sha256(derived_file.read_bytes()).hexdigest()
