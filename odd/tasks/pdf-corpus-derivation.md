# Feature: Ingest PDFs as an Auditable Corpus Derivation (`bruriah corpus --pdf`)

## Objective
Enable users and autonomous agents to index PDF documents (design specs, academic papers, standards, architecture RFCs) by extracting them into an auditable Markdown corpus on disk prior to indexing. This strictly preserves Bruriah's sacred byte-for-byte locator guarantee (`spec.md:253`, Issue #5) without modifying a single line of the indexer or introducing unverified probabilistic guessing.

## Constraints & Invariants
- **Zero Indexer Modification:** Not one line changes in `corpus.py`, `index.py`, or `retrieval.py`. The indexer only sees real on-disk Markdown files.
- **Byte-for-Byte Locator Contract:** Locators cite lines in physical derived Markdown files on disk (`corpus/<stem>-p<page:03d>.md`). `read_evidence` returns the exact on-disk bytes as always.
- **Declared Provenance:** Each derived Markdown document contains YAML frontmatter recording `source` (original PDF path), `page` (1-indexed page number), `extractor` (e.g. `pypdf==5.3.0`), `source_sha256` (SHA-256 digest of original PDF), and `status: active`.
- **Honest Text-Layer First:** Only pages with real extractable text are written. Purely blank pages or unextractable scanned images without text layers are skipped and honestly reported in examination stats (analogous to skipping commits with no explanatory body).
- **Optional Dependency:** `pypdf` is an optional extra (`bruriah[pdf]`). Core installations remain pure and lightweight with zero native C extensions. Missing dependency produces an explicit, actionable error message.
- **Backward-Compatible CLI Ergonomics:** `bruriah corpus --pdf PATH --out DIR` handles single files or directory trees of PDFs, while `bruriah corpus --repo PATH --out DIR` continues to handle Git repositories as before.

## Tasks
- [x] **task-1**: Optional dependency configuration & `src/bruriah/pdfcorpus.py` engine
  - Add optional dependency `pdf = ["pypdf>=5.0.0"]` and add `pypdf` to dev dependencies in `pyproject.toml`.
  - Implement `src/bruriah/pdfcorpus.py` with `build(source: Path, out: Path) -> PdfCorpusResult`.
  - Handle single PDF files and directory traversal (`*.pdf`).
  - Calculate source SHA-256, extract page text, and format frontmatter + Markdown body.
  - Fail with clear actionable message if `pypdf` is not installed.
- [x] **task-2**: CLI Integration & Coverage Reporting (`src/bruriah/cli.py`, `src/bruriah/_cli/parser.py`)
  - Add `--pdf` option to `bruriah corpus` CLI subparser.
  - Wire `_cmd_corpus` to branch cleanly between Git history derivation and PDF derivation.
  - Emit JSON execution metrics and human-readable coverage summary (`_report_pdf_coverage`).
- [x] **task-3**: Unit & Integration Tests (`tests/test_pdfcorpus.py`, `tests/test_cli.py`)
  - Test single PDF derivation, directory of PDFs derivation.
  - Test skipping of empty/whitespace pages and stats calculation.
  - Test provenance frontmatter completeness (`source`, `page`, `extractor`, `source_sha256`).
  - Test end-to-end: PDF derivation -> `bruriah index` -> `investigate_work` -> `read_evidence` verifying byte-for-byte exactness.
  - Test graceful error when `pypdf` is not installed or when file is invalid.
- [x] **task-4**: Documentation & Verification
  - Update `README.md` and `CHANGELOG.md` with PDF derivation workflow and design rationale.
  - Run full test suite, `ruff`, and `mypy`.
