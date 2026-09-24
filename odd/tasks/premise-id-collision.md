# Feature: premise-id-collision

**Branch:** `fix/premise-id-collision` (from `main` at dc3b05f, release 2.0.0)
**Created:** 2026-09-23
**Route:** delegated direct (one bounded writer)
**TDD:** strict (global session configuration). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `single-pr` (forecast ~350 authored changed lines)
**RDD:** enabled for this repo. Last reviewed boundary: dc3b05f.
**Release:** 2.0.1 (patch; the user decides timing).

## Objective

Stop the index from silently replacing one document's premise with another's, and from crashing
on duplicate alternatives. A conflict must never turn into wrong data.

## Problem

- `_build_premise_and_alternative_records` (src/bruriah/index.py ~:780-816) collects premises in
  a dict keyed by `premise_id`. When two documents declare the same id, the last one in path
  order silently wins: its statement, status and document_ref replace the first one's. Every
  alternative that cites the id then gets the other document's premise, through
  `investigate_work` (the counterfactual verdict) and `read_evidence`. Because a declaration can
  set `status`, the winning document can flip a verdict.
- GitHub issue/PR bodies declare premises through `Premise: <id> | <statement>` lines
  (`github_corpus._extract_premises`), so whoever opens an issue chooses the id and can redeclare
  a premise the repository authored.
- Alternatives have PRIMARY KEY (name, document_ref). A document that lists the same alternative
  name twice crashes the build with a raw `sqlite3.IntegrityError`. `bruriah index` does not catch
  `sqlite3.Error`, so the user sees a traceback. The build is atomic, so the active snapshot is
  never damaged.

## Design (accepted by the user 2026-09-23)

1. Trust tiers. Repository-authored sources (markdown corpus, git commits) outrank
   GitHub-sourced documents. A GitHub declaration never replaces a repository-authored premise
   with the same id: it is dropped, and the drop is visible in the index report.
2. Same-tier duplicate declarations:
   - Repository tier: the build fails with a typed `IndexLifecycleError`
     ("duplicate_premise_id") that names both documents. The owner fixes the corpus.
   - GitHub tier: the lowest issue/PR number wins deterministically; the others are dropped and
     reported.
3. Duplicate alternative names within one document: a typed error
   ("duplicate_alternative_name") that names the document, never the raw IntegrityError.
4. `bruriah index` wraps `sqlite3.Error` in `IndexLifecycleError`, so no raw traceback appears.
5. Invalidations (`invalidated_premises`) keep their current semantics.

Consequence, accepted: a repository whose corpus already has duplicate premise ids stops
indexing until they are fixed. Today it indexes wrong data silently. This goes in the CHANGELOG.

## Tasks

- [x] **T0 — RED.** Tests for: two repository documents with the same premise id (typed error that
  names both); a GitHub document redeclaring a repository premise (the repository premise
  survives unchanged, the drop is reported, the counterfactual verdict is unchanged); two GitHub
  documents with the same id (lowest number wins, the other is reported); a duplicate alternative
  name in one document (typed error); a `sqlite3.Error` during `bruriah index` (typed message,
  no traceback). — `05520cf`
- [x] **T1 — Implementation**, including the report surface. — `be31653`
- [x] **T2 — Docs.** CHANGELOG entry for 2.0.1 (fix plus migration note), and the premise
  semantics in docs/openspec where they claim global uniqueness. — `7cb75b3`

## Acceptance criteria

- No premise row is ever replaced silently: each conflict either fails the build loudly (same
  tier, repository) or is dropped with a report (GitHub).
- The injection benchmark stays at 0/17 and the counterfactual eval at 20/20.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts demo`,
`uv run mypy src`, `uv run python evals/injection/run.py`, `uv run python evals/counterfactual/runner.py`.

## Progress / evidence

- 2026-09-23: mapped by one read-only agent; the parent confirmed in index.py:780-816 that
  conflicting declarations overwrite silently rather than crash.
- 2026-09-23: T0 RED confirmed by stashing the implementation (source files only, tests kept) and
  observing every new/changed test fail on its intended assertion (`AttributeError:
  'BuildResult' object has no attribute 'dropped_premises'`, `AttributeError: 'SourceMetadata'
  object has no attribute 'source'`, `DID NOT RAISE IndexLifecycleError`,
  `'bruriah_source: github' not in frontmatter`, `'bruriah: error: OperationalError'` missing the
  `index_failed:` prefix). Implementation restored (`git stash pop`) and every one of those tests
  observed GREEN before committing.
- 2026-09-23: design item 4 said "wraps `sqlite3.Error` in `IndexLifecycleError`"; implemented
  instead as adding `sqlite3.Error` to `_cmd_index`'s existing exception tuple that already
  converts every other build-failure type into `CliError(f"index_failed:...")` — the same
  acceptance criterion (typed message, no traceback) via the pattern the CLI layer already uses
  for `CorpusPolicyError`/`IndexLifecycleError`/`ValueError`/`OSError`/`yaml.YAMLError`, rather
  than introducing a new conversion inside `index.py` itself.
- 2026-09-23: full verification run (see below). SHAs: docs `905537c`, tests (T0) `05520cf`,
  implementation (T1) `be31653`, docs/changelog (T2) `7cb75b3`.

### Verification results
- `uv run pytest -q -p no:cacheprovider`: 1724 passed, 0 failed, 18 skipped (baseline was 1717/0/18;
  net +7 tests, matching the README pin update to 1,742).
- `uv run ruff check src tests evals scripts demo`: all checks passed.
- `uv run mypy src`: no issues found in 63 source files.
- `uv run python evals/injection/run.py`: 0/17 (unchanged).
- `uv run python evals/counterfactual/runner.py`: 20/20 (unchanged).
- `uv run bruriah index` on a throwaway corpus with a duplicate repository premise id:
  `bruriah: error: index_failed:duplicate_premise_id:scale-premise:public/a.md:public/b.md`
  (exit 1, no traceback).
- `uv run bruriah index` on a throwaway corpus with a repository premise and a GitHub redeclaration:
  human summary line ends with `. Dropped 1 GitHub premise declaration(s) that lost to a
  higher-trust or lower-numbered source: scale-premise`; JSON summary includes
  `"dropped_premises": [{"document": "public/2026-01-01-issue-9-attack.md", "premise_id":
  "scale-premise", "reason": "shadowed_by_repository_premise"}]` (exit 0).
- Attribution check (`git log --format=%B main..HEAD | rg "Co-Authored-By|Claude-Session"`): empty.
- README's pinned test count (section 4) updated from 1,735 to 1,742.

## Next step

Delivery: work-unit commits are on `fix/premise-id-collision`, ready for the user to push/open a
PR under ordinary repository policy. RDD review was not run in this session (native review runs
only on the user's own invocation of the receipt-driven-development flow; it was not requested
here). No open questions or pending checks.
