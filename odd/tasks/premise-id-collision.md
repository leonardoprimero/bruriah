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

**Amended 2026-09-24** (native review `review-aa1bc7b855f40f4c`, R2-invalidation-tier-gap /
R3-invalidation-not-trust-tiered): item 5 above was incomplete. `invalidated_premises` is now
trust-tiered the same way a declaration is -- a GitHub-tier document can never change the
`status`/`invalidated_by` of a repository-tier premise. See "Review follow-ups" below.

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

## Review follow-ups

Native review lineage `review-aa1bc7b855f40f4c` approved the branch (as it stood through
`1343a8e`) and was acknowledged. Four findings came back for this feature; addressed here, TDD
throughout (RED confirmed by stashing the implementation, keeping the new/changed tests, before
restoring it):

1. **R2-invalidation-tier-gap / R3-invalidation-not-trust-tiered** -- the spec said
   `invalidated_premises` applies "regardless of trust tier", but nothing enforced that boundary
   for the trust tiers this feature introduced: a GitHub-tier `invalidated_premises` entry could
   still flip a repository-authored premise's status. Closed as defense in depth (`github_corpus`
   emits no invalidations today, only `premises` declarations): a GitHub-tier invalidation naming
   a repository-tier premise is now dropped and reported with a new reason,
   `github_invalidation_ignored`. Test: `test_a_github_invalidation_never_changes_a_repository_
   premise` (`tests/test_index.py`). Spec and CHANGELOG updated to describe the complete boundary.
   — `71d3599` (fix), `088314b` (docs)
2. **R3-summary-line-omits-document** / **R2-summary-duplicate-ids** -- the human summary line
   named only the dropped premise id, never its source document, and a premise dropped more than
   once collapsed into one undifferentiated id. `_index_summary_line` now groups by premise id and
   names each document/reason pair. — `847460b`
3. **R3-cli-drop-report-untested** -- added a CLI-level test
   (`test_the_index_report_names_every_dropped_document_and_groups_by_premise_id`,
   `tests/test_cli.py`) driving one mixed corpus through `_cmd_index` and asserting both the JSON
   `dropped_premises` field names and the exact grouped, per-document human summary text. — `847460b`
4. **R2-source-untyped-tier** -- `SourceMetadata.source` is now
   `Literal["repository", "github"]`; `index.py`'s redundant re-normalizing ternary
   (`"github" if doc.metadata.source == "github" else "repository"`) is gone in favor of reading
   the field directly. No behavior change. — `520e9f4`

**Left as-is by instruction:** R4-legacy-github-docs-hard-fail -- already documented in the
CHANGELOG's 2.0.1 migration note (GitHub documents generated by 2.0.0 or earlier carry no
`bruriah_source` marker and must be regenerated with `bruriah corpus --github`).

### Follow-up verification results
- `uv run pytest -q -p no:cacheprovider`: 1726 passed, 0 failed, 18 skipped (net +2 tests over the
  first round's 1724/0/18; README pin bumped 1,742 -> 1,744).
- `uv run ruff check src tests evals scripts demo`: all checks passed.
- `uv run mypy src`: no issues found in 63 source files.
- `uv run python evals/injection/run.py`: 0/17 (unchanged).
- `uv run python evals/counterfactual/runner.py`: 20/20 (unchanged).
- Attribution check (`git log --format=%B main..HEAD | rg "Co-Authored-By|Claude-Session"`): empty.
- `uv run bruriah index` on the mixed-case corpus from finding 3's test (a repository-tier
  redeclaration loss, a GitHub-vs-GitHub issue-number loss, and an ignored GitHub invalidation, all
  in one build) -- exact human summary line:
  `Index: 5 passage(s) from 5 document(s) (0 reused, 5 embedded), build <id> is active. Dropped 3
  GitHub premise declaration(s): scale-premise (public/g1-issue-9-redeclare.md:
  shadowed_by_repository_premise, public/g4-issue-50-invalidate.md: github_invalidation_ignored);
  other-premise (public/g2-issue-30-other-a.md: shadowed_by_lower_github_issue)`
  and the JSON `dropped_premises` array names each document alongside its premise id and reason,
  in the same order.

## Review follow-ups, round 2

Native review lineage `review-c2885675bd293875` approved the branch (as it stood through
`5b72470`) and was acknowledged. Four more findings came back; addressed here, TDD throughout.

1. **R3-invalidation-allowed-paths-untested** -- round 1 only proved the BLOCKED invalidation path
   (GitHub-tier targeting a repository premise). Added two tests proving the allowed paths still
   work: a repository-tier invalidation still flips a repository premise
   (`test_a_repository_invalidation_still_flips_a_repository_premise`), and a GitHub-tier
   invalidation still flips a GitHub-tier premise
   (`test_a_github_invalidation_still_flips_a_github_tier_premise`), both asserting
   `status`/`invalidated_by`/`invalidation_document_ref`. **Coverage, not RED: both pass on the
   already-committed code.** — `acebe46`
2. **R3-tier-no-longer-normalized** -- `Literal["repository", "github"]` is static-only;
   `SourceMetadata(source="anything")` was accepted at runtime and would have silently fallen into
   `index.py`'s GitHub (lower-trust) branch. Added `SourceMetadata.__post_init__`, the same
   pattern `BuildConfig.__post_init__` already uses, raising `ValueError("invalid_source_tier")`
   for anything outside the two trusted values. RED:
   `tests/test_models.py::test_source_metadata_rejects_a_source_tier_outside_the_two_trusted_values`
   failed with `DID NOT RAISE ValueError` before the guard. — `8ed08c1`
3. **R2/R3/R4 summary label** + **R2-dropped-comment-misstates-invalidation-rule** -- the human
   summary headline called every drop a "declaration" even when it was an ignored invalidation;
   relabeled to "entry"/"entries" (grammatically singular for exactly one drop). Updated the
   pinned text in the existing mixed-corpus CLI test and added a direct unit test of the singular
   case against `_index_summary_line`. Also corrected the JSON `dropped_premises` comment, which
   claimed every drop "lost to a higher-trust or lower-issue-number one" -- true only for a losing
   declaration, not for an invalidation, which is dropped only when it targets a repository-tier
   premise. — `3027733`
4. **Release prep** -- `chore(release): 2.0.1` bumps `pyproject.toml`/`__init__.py`/`uv.lock`
   (bruriah's own entry only), sets the CHANGELOG heading date to 2026-09-24, describes this
   round's fixes in the 2.0.1 entry, and bumps the README test-count pin. — `ca84b42`

### Round-2 verification results
- `uv run pytest -q -p no:cacheprovider`: 1730 passed, 0 failed, 18 skipped (net +4 tests over
  round 1's 1726/0/18; README pin bumped 1,744 -> 1,748).
- `uv run ruff check src tests evals scripts demo`: all checks passed.
- `uv run mypy src`: no issues found in 63 source files.
- `uv run bruriah --version`: `bruriah 2.0.1`.
- `uv run python scripts/changelog_section.py 2.0.1`: prints the `## [2.0.1]` section body.
- `uv run python evals/injection/run.py`: 0/17 (unchanged).
- `uv run python evals/counterfactual/runner.py`: 20/20 (unchanged).
- `git diff uv.lock` against the pre-bump state: exactly one line, bruriah's own
  `version = "2.0.0"` -> `version = "2.0.1"`; no other package moved.
- Attribution check (`git log --format=%B main..HEAD | rg "Co-Authored-By|Claude-Session"`): empty.
- Final human summary label, singular and plural: `Dropped 1 GitHub premise entry: ...` /
  `Dropped 3 GitHub premise entries: ...` (both pinned in tests).

## Next step

Delivery: PR, CI, merge, tag `v2.0.1` (the user authorizes the tag). Native review has approved
and acknowledged this branch through both `review-aa1bc7b855f40f4c` and `review-c2885675bd293875`;
commits landing after each acknowledgement (this round's follow-ups, and the release-prep commit)
were not themselves put through a fresh review cycle in this session. No open questions or
pending checks.
