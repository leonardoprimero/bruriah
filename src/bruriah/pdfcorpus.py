#!/usr/bin/env python3
"""Turn PDF documents into an auditable Markdown corpus Bruriah can index.

This lives in the package, not in scripts/, because it is step ONE of the documented
workflow for organizations with documents, specifications, architecture RFCs, and standards.

Issue #5 defines the architectural thesis:
A PDF has no lines. Extract its text and cite "line 12", and that line exists only in the
extractor's output -- not in the file the user has. If `read_evidence` returned what a parser
produced rather than real on-disk bytes, Bruriah's sacred byte-for-byte locator guarantee
would be broken.

Therefore, PDF ingestion is an auditable derivation step, exactly like `gitcorpus.py`:

    document.pdf ──extract──▶ corpus/document-p014.md ──index──▶ passages
                              (front-matter: source, page, extractor, source_sha256)

What this buys:
1. Zero indexer modification: the index never sees a PDF; it sees on-disk Markdown files.
2. The locator contract is preserved: citation locators point to lines in real on-disk files.
3. Byte-for-byte fidelity: `read_evidence` returns exact bytes of real on-disk files.
4. Full auditability: if an extractor mangles layout or tables, you can open the .md file.
5. Declared provenance: front-matter names the source PDF, page number, extractor version,
   and source PDF SHA-256 digest.

Pages with no text (cover graphics, blank separator pages, pure image scans without text layer)
are skipped and honestly reported in coverage statistics.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import pypdf
except ImportError:
    pypdf = None  # type: ignore[assignment]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower())[:60].strip("-") or "untitled"


@dataclass(frozen=True)
class PdfCorpusResult:
    written: int
    examined: int
    files_examined: int
    skipped_empty_pages: int

    @property
    def documents(self) -> int:
        return self.written

    @property
    def pages(self) -> int:
        return self.examined

    @property
    def pages_examined(self) -> int:
        return self.examined


def _extractor_version() -> str:
    if pypdf is None:
        return "pypdf"
    version = getattr(pypdf, "__version__", "unknown")
    return f"pypdf=={version}"


def build(source: Path, out: Path) -> PdfCorpusResult:
    """Extract text from one PDF file or a directory of PDFs into an auditable Markdown corpus.

    Each non-empty page becomes an individual Markdown document with YAML frontmatter
    recording provenance (source file path, page number, extractor version, source SHA-256).
    """
    if pypdf is None:
        raise SystemExit("error: PDF extraction requires pypdf. Install it with: pip install 'bruriah[pdf]'")

    if not source.exists():
        raise SystemExit(f"error: source path does not exist: {source}")

    if source.is_file():
        if source.suffix.lower() != ".pdf":
            raise SystemExit(f"error: source file is not a PDF: {source}")
        files: list[tuple[Path, str]] = [(source, source.name)]
    elif source.is_dir():
        pdf_paths = sorted(
            [p for p in source.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"],
            key=lambda p: p.as_posix(),
        )
        if not pdf_paths:
            raise SystemExit(f"error: no PDF files found in {source}")
        files = [(p, p.relative_to(source).as_posix()) for p in pdf_paths]
    else:
        raise SystemExit(f"error: source {source} is neither a file nor a directory")

    out.mkdir(parents=True, exist_ok=True)

    written = 0
    pages_examined = 0
    skipped_empty_pages = 0
    extractor = _extractor_version()

    for file_path, rel_name in files:
        try:
            file_bytes = file_path.read_bytes()
            source_sha256 = hashlib.sha256(file_bytes).hexdigest()
            reader = pypdf.PdfReader(file_path)
            num_pages = len(reader.pages)
        except Exception as err:
            raise SystemExit(f"error: failed to read PDF {file_path}: {err}")

        base_slug = _slug(Path(rel_name).with_suffix("").as_posix())
        title_base = Path(rel_name).stem

        for page_idx in range(num_pages):
            page_num = page_idx + 1
            pages_examined += 1
            page = reader.pages[page_idx]
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                text = ""

            if not text:
                skipped_empty_pages += 1
                continue

            frontmatter = (
                "---\n"
                f"source: {rel_name}\n"
                f"page: {page_num}\n"
                f"extractor: {extractor}\n"
                f"source_sha256: {source_sha256}\n"
                "status: active\n"
                "---\n\n"
            )
            document = frontmatter + f"# {title_base} - Page {page_num}\n\n" + text + "\n"
            doc_filename = f"{base_slug}-p{page_num:03d}.md"
            (out / doc_filename).write_text(document, encoding="utf-8", newline="\n")
            written += 1

    return PdfCorpusResult(
        written=written,
        examined=pages_examined,
        files_examined=len(files),
        skipped_empty_pages=skipped_empty_pages,
    )
