# Feature: github-issue-pr-ingestion

**Branch:** `feat/github-issue-pr-ingestion` (from `chore/pin-own-history-eval` at 46eb621, which itself sits on `main` at fff2a71 / v1.4.0)
**Created:** 2026-09-21
**Route:** delegated direct (one bounded writer per task; every task touches 2+ non-trivial files)
**TDD:** strict, enabled by session configuration (`Strict TDD Mode: enabled`). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `single-pr` (same as the two previous features). Forecast ≈ 1,000 authored changed lines (recorded JSON fixtures and eval data files excluded); the size is explained by four independent surfaces (API client, link parsing, document builder, eval), not by padding.
**RDD:** enabled globally on user request (2026-09-21, after T2). First reviewed boundary: 46eb621. Lineage `review-1f67358a78d1c229` covers 46eb621..cb0b643 (T1+T2+T3, high risk, 26 files, 2,579 lines; consent granted by the user).

## Objective

Let `bruriah corpus` ingest the GitHub issues and pull requests a commit links to (`Fixes #N`,
`Closes #N`, `Resolves #N`, `(#N)` squash subjects) as corpus documents, so that (a) the issue's
vocabulary closes part of the retrieval gap measured on leakcanary and (b) pull requests closed
without merge and issues closed as `not_planned` become **rejected alternatives** the counterfactual
engine can detect. Network is opt-in and off by default; the two MCP tools are not touched.

## Problem and evidence

- Miss analysis with jina (2026-09-20): on leakcanary 40% of misses sit at rank 41+ or absent, a
  vocabulary gap between the issue's words and the closing commit's words. The issue text is the
  missing vocabulary.
- The counterfactual engine builds premises and alternatives only from front matter
  (`src/bruriah/index.py:780-838`, `src/bruriah/corpus.py:113-201`), provenance-agnostic. A
  GitHub-derived `.md` with `alternatives:` front matter is indexed like any commit document.
- `src/bruriah/github.py` only posts and dismisses PR reviews (`_github_api` at 80-126, stdlib
  `urllib`, no retry, no rate-limit handling, no read primitive). Tests mock `_github_api`; no
  recorded-response fixtures exist.
- `src/bruriah/gitcorpus.py` walks `git log --no-merges`, skips bodiless commits, parses decision
  trailers, honors `--revision`, and already captures touched file paths (`git show --name-only`,
  line 143) as prose.
- README promises: `README.md:147` "100% local-first. Stdio only, no telemetry";
  `README.md:237` "Off by default. Zero telemetry, zero analytics, zero outbound pings."
  Precedent for opt-in network: `InvestigationRequest.network_policy = "off"` (`contracts.py:53`).
- Eval hook: rows in `evals/project-memory/{leakcanary,egui}-issues.jsonl` carry
  `provenance.issue` and `provenance.commit`.

## Decisions taken (user, 2026-09-21)

- **Rejected alternatives come from GitHub state, not from PR templates**: a PR that
  cross-references the linked issue and was closed without merge, or a duplicate/related issue
  closed with `state_reason: not_planned`. The rejection reason is the closing comment (last
  comment before `closed_at`), truncated. Premises are read only from explicit `Premise:` lines in
  the issue/PR body using the same grammar as commit trailers; no NLP extraction.
- **Ingestion happens at corpus build time** (`bruriah corpus --github OWNER/REPO`), never inside
  `investigate_work` / `read_evidence`. `EvidenceRecord.kind` stays `"local"`; provenance goes in
  the document front matter and `publisher`.
- **Reproducibility and offline tests share one mechanism**: a JSON response cache directory
  (`--github-cache DIR`). The fetcher reads the cache before the network; with
  `--github-cache` and no token the build is fully offline. Tests point the fetcher at
  `tests/fixtures/github/` (recorded responses, hand-trimmed).

## Scope (authorized)

- `src/bruriah/github.py`: read primitives (`get_issue`, `get_issue_timeline`, `get_pull`,
  `get_issue_comments`), bounded retry with backoff on 5xx and on `403/429` with
  `X-RateLimit-Reset`, `If-None-Match`-free (keep it simple), response cache.
- `src/bruriah/gitcorpus.py` (or a sibling module): issue-link extraction from subject and body;
  document builder for issues/PRs; `--github`, `--github-cache`, `--github-token-env` on
  `bruriah corpus` (`src/bruriah/_cli/parser.py:176-198`), off by default.
- Tests: pure-function tests for link parsing; fetcher tests over recorded fixtures with the
  network path asserted never called; builder tests over fixtures; CLI test that `bruriah corpus`
  without `--github` performs zero GitHub calls.
- Docs: `README.md` (local-first section gains the opt-in sentence), `docs/cli-and-tools.md`,
  `CHANGELOG.md` `[Unreleased]`.
- Eval: `evals/retrieval/report_counterfactuals.py` (new, sibling of `report_reach.py`), paired
  recall@3 with and without GitHub documents on leakcanary and egui, numbers published in
  `evals/project-memory/README.md`.

Out of scope: reranker, RRF K, new embedding models, new subcommands, any change to
`contracts.py`, fetching inside MCP tools, GraphQL.

## Constraints

- Language contract: all artifacts in English, neutral register.
- Never delete comments, blank lines, or tests for size. ≈400 authored lines per task is a
  planning heuristic, not a cap.
- Conventional Commits, no AI attribution lines. Work-unit commits on the feature branch; push,
  PR, and merge are the user's decisions.
- A commit that links nothing produces exactly the document it produces today (byte-identical
  corpus when `--github` is absent).

## Tasks

- [x] **T1 — GitHub read primitives with cache and bounded retry.** (done 2026-09-21, commit `71fa4d7`)
  `src/bruriah/github.py`: `GitHubReader` (or module functions) for issue, timeline
  (`cross-referenced` events), pull, comments; `ResponseCache` keyed by endpoint path storing raw
  JSON; retry policy: up to 3 attempts, exponential backoff, honor `X-RateLimit-Reset` on
  403/429, never retry 404. Fixtures under `tests/fixtures/github/` for one issue with: one merged
  PR, one PR closed without merge (with a closing comment), one `not_planned` duplicate.
  Checks: new tests RED then GREEN; `tests/test_github.py` existing tests untouched and green;
  ruff, mypy.
- [x] **T2 — Issue-link extraction.** (done 2026-09-21, commit `7d666ae`) Pure function `linked_issues(subject, body) ->
  tuple[IssueLink, ...]` handling `Fixes|Closes|Resolves #N` (case-insensitive, with optional
  `-d`/`:`), `owner/repo#N`, bare `#N` in body, and the squash-merge `(#N)` subject suffix (which
  identifies the PR itself, not an issue). Deduplicated, ordered by first appearance.
  Checks: parametrized tests RED then GREEN; ruff, mypy.
- [x] **T3 — Issue/PR document builder and `bruriah corpus --github`.** (done 2026-09-21, commit `cb0b643`; native review corrections `e956e61`, `4c69c54`, `049a1e0`; approved and acknowledged) For each linked issue:
  one document `YYYY-MM-DD-issue-N-<slug>.md` (date = issue `closed_at` or `created_at`), front
  matter `issue: N`, `commit: <linking sha>` (anchors lineage), `github_url`, `alternatives:` from
  unmerged cross-referencing PRs and `not_planned` issues (name = title, disposition `rejected`,
  reason = closing comment excerpt, premises = explicit `Premise:` lines if any); body = issue
  title, body, and a "Linked decisions" section naming the closing commit(s). Skip-and-warn per
  issue on fetch failure; write a `github-manifest.json` in the corpus root listing fetched,
  skipped, and cache hits. CLI: `--github OWNER/REPO` (off by default), `--github-cache DIR`,
  `--github-token-env NAME` (default `GITHUB_TOKEN`, warn when unset, unauthenticated allowed).
  Docs: README local-first sentence, `docs/cli-and-tools.md`, CHANGELOG.
  Checks: builder tests over fixtures RED then GREEN; CLI test proving no GitHub call without
  `--github`; full suite; ruff, mypy.
- [x] **T3b — Make the network switch discoverable from `--github`.** (done 2026-09-21, commit `a203896`; reviewed `review-a1565adf35186fe3`, approved without findings, acknowledged; boundary advances to `a203896`) Found while running T4:
  `--github` honors the tool-wide `--network-enabled` switch (a subcommand-level flag, default
  off, also `NETWORK_ENABLED` env / `config.json`), which is the right design, but
  `docs/cli-and-tools.md` 13b never says so and the CLI's only signal is one
  `github_offline_cache_miss` warning per issue, hundreds of lines for a real repo. Fix: (a) when
  `--github` is given, network is off, and the cache misses at least once, print ONE stderr line
  up front naming `--network-enabled`; (b) collapse the per-issue offline warnings into a single
  count in that mode; (c) document `--network-enabled` next to `--github` in `docs/cli-and-tools.md`
  and in the README opt-in sentence. TDD; small.
- [x] **T3c — Deduplicate timeline cross-references before they become alternatives.** (done 2026-09-21, commit `4fb0c87`; reviewed `review-5d41ab06176e0d8c`, approved without findings, acknowledged; boundary advances to `4fb0c87`) Found by
  the egui index build: dedupe by source `(repo, number)` before fetching, strip names/reasons,
  suffix ` (#N)` when two distinct sources share a title; test that the document indexes through
  the real build path. Follow-up (not in this feature): `bruriah index` should surface a
  duplicate-alternative error by name instead of a raw `IntegrityError`.
- [x] **T4 — Measure on leakcanary and egui.** (done 2026-09-21: offline half `f8d2255`, publication `70b2dd7` reviewed `review-d17dd086e2e8e06b`, approved without findings, acknowledged; boundary advances to `70b2dd7`) (offline half done: `f8d2255` `evals/retrieval/report_counterfactuals.py` + tests + README section without numbers; reviewed and acknowledged as `review-525080f9c26af8c7`, boundary advances to `f8d2255`. Network half running: `~/bruriah-worktrees/ablation-data/fetch-github.sh` with `--network-enabled`, user-authorized `gh` token, cache under `ablation-data/github-cache/`.) Fetch linked issues for the pinned clones
  (`0f7dbab17e2a`, `5d3e958ecfd3`) with a token into a cache directory kept outside the repo;
  index twice (with and without GitHub documents, same model, K=60, no reranker); report paired
  recall@3 / recall@10 and McNemar via the existing harness, plus the new
  `report_counterfactuals.py`: for each question with `provenance.issue`, whether the index holds
  at least one alternative or premise traceable to that issue, and the totals of rejected
  alternatives and premises the engine now knows. Publish in `evals/project-memory/README.md`
  with the exact commands and cache size.
  Checks: numbers reproduced from the cache directory a second time; `tests/test_project_memory_eval.py`
  green.
- [ ] **T5 (deferred, not started) — Touched file paths as passage text.** Verify whether
  the prose "Files this decision touched" section already reaches the embedder as a passage; if
  not, a single paired measurement on leakcanary decides whether to keep or drop the change.

## Acceptance criteria

- `bruriah corpus` without `--github` produces a byte-identical corpus to 1.4.0 and performs
  zero network calls (asserted by test).
- With `--github` and a warm cache and no token, the build completes offline.
- At least one real leakcanary or egui question yields a rejected alternative detected by
  `investigate_work` that did not exist before, shown in the published eval.
- Full suite, ruff, and mypy green on every work-unit commit.

## Progress

- T1 done. Route: delegated direct (writer trigger: 2+ non-trivial files). Landed as a sibling
  module `src/bruriah/github_read.py` (379 lines) rather than inside `github.py`, so the write-side
  module has zero diff. Public API: `ResponseCache(root)`, `get_issue`, `get_pull`,
  `get_issue_timeline`, `get_issue_comments` (all with `cache`, `token=None`,
  `network_enabled=True`, injectable `transport`/`clock`/`sleep`), `GitHubNotFoundError`,
  `GitHubOfflineError`. Fixtures: `tests/fixtures/github/` (issue 41 completed, PR 50 merged,
  PR 47 closed unmerged with closing comment, issue 39 not_planned). README test count 1,411.
  RDD: assessment pending (switch was off at commit time; see the RDD line above).
- T2 done. Route: delegated direct. `src/bruriah/issue_links.py` (167 lines): `IssueLink(number,
  kind: closes|mentions|pull_request_self, repo, keyword)` and `linked_issues(subject, body)`.
  Matches GitHub's grammar: `Fixes #10, #12` closes only #10 and mentions #12 (same as GitHub);
  `(#N)` squash suffix is `pull_request_self`; code spans and `C#5`-style tokens excluded.
  README test count 1,450. RDD: assessment pending.
- T3 done. Route: delegated direct. `src/bruriah/github_corpus.py` (~430 lines: `build_documents`,
  `detect_repo_slug`, `GitHubCorpusResult`, `GitHubCorpusError`); `gitcorpus.py` gained an additive
  `walk_commits`/`WalkedCommit` and an extracted `_split_premise_value` (commit output
  byte-identical, pinned by the existing suites); `bruriah corpus --github [OWNER/REPO]`,
  `--github-cache DIR` (default `<platformdirs cache>/github`), `--github-token-env NAME`; front
  matter `commit`, `issue`, `github_url`, `source_url`, conditional `alternatives`/`premises`, written
  with `yaml.safe_dump` because real titles contain colons. Provenance carrier: the
  `YYYY-MM-DD-issue-N-<slug>.md` filename is what `EvidenceRecord.publisher` reads
  (`publisher` is hardcoded to `relative_path` in retrieval/service, which are off-limits), plus
  `source_url` into `SourceMetadata.provenance_urls`. Docs: README (opt-in sentences),
  `docs/cli-and-tools.md` section 13b, CHANGELOG `[Unreleased]`. Contracts/service/mcp_server
  untouched (verified by diff).
- Native review of 46eb621..cb0b643 (4 lenses): one CRITICAL candidate-caused finding, R3-001
  (reliability): `_collect_alternatives` resolved timeline `cross-referenced` events by number
  without checking the source repository, so a foreign-repo reference could fetch an unrelated
  same-numbered PR or drop the whole issue document on 404. Correction plan accepted (forecast 40
  lines); fix delegated as one bounded correction.
- R3-001 fixed in `e956e61` (`_source_repo_slug` via `repository.full_name` or `html_url`; foreign
  sources skipped and counted; fixtures issue-200). Because the candidate is a committed base-diff,
  the fix commit changed the target identity; the native flow required a maintainer-authorized
  recovery (`gentle-ai review recover --disposition scope_changed`), which the user authorized;
  successor lineage `review-1f67358a78d1c229-r3001`. Second four-lens round: two CRITICAL
  deterministic resilience findings, R4-001 (urlopen without timeout hangs the build) and R4-002
  (rate-limit sleep capped at 300 s retries into a still-closed window, and the builder does not
  remember the window is closed: ~8 h of sleeping for 50 uncached issues). Correction plan
  accepted (forecast 90 lines); fix delegated.
- R4-001/R4-002 fixed in `4c69c54` (30 s timeout mapped to `github_timeout`;
  `GitHubRateLimitedError(reset_at)` raised without sleeping when the reset is beyond the 300 s
  cap or after one full wait; the ledger remembers the closed window, skips remaining uncached
  issues with reason `rate_limited`, writes `rate_limited_until` into the manifest, one stderr
  line). Targeted validation then refused the correction: 360 changed lines against the frozen
  200-line budget. Tests were not cut. With the user's authorization the lineage
  `review-1f67358a78d1c229-r3001` was abandoned (`operator_disposition`) and a fresh review of
  46eb621..4c69c54 started as `review-de9bbfc688d6402c` (high, 28 files, 3,029 lines, consent
  granted). Third four-lens round: one CRITICAL deterministic finding, R4-001 (resilience):
  mid-body read failures (`IncompleteRead`, connection reset, SSL error) escape
  `_default_transport` as raw exceptions and abort the whole build before the manifest is written.
  Correction plan accepted (forecast 40 lines); fix delegated with a hard 120-line limit.
- R4-001 (mid-body) fixed in `049a1e0` (50 changed lines: `except (http.client.HTTPException,
  OSError)` after the timeout/URLError branches → `GitHubError("github_network_error")`).
  Targeted validation: `approved`. Exact acknowledgement executed; `gentle-ai.review-acknowledged/v1`
  returned for lineage `review-de9bbfc688d6402c`, target sha256:4c9d1b9e…. **Reviewed boundary
  advances to `049a1e0`.** Delivery (push, PR, merge) remains the user's decision.
- T4a done (`f8d2255`): `report_counterfactuals.py` reads only through `SnapshotRepository`
  (`scan_passages`, `get_alternatives`, `get_premises`); "traceable to issue N" = the row's
  `document_ref` names a document whose filename matches `YYYY-MM-DD-issue-N-<slug>.md` (the
  front-matter `issue:` key is not persisted into the index, so the filename is the only signal the
  index exposes). Usage: `python evals/retrieval/report_counterfactuals.py --corpus <name>
  --data-dir <index> --questions evals/project-memory/<name>-issues.jsonl --out <jsonl> [--json]`.
  Native review (4 lenses): approved with no findings; acknowledged. Reviewed boundary `f8d2255`.
- T4b in progress: first fetch attempt ran offline (every issue `github_offline_cache_miss`)
  because `--github` honors the tool-wide `--network-enabled` switch and the script did not pass
  it (that is the T3b finding). Re-run with the flag; ≈1 request/s.
- T4b leakcanary measured (see Verification evidence): 322 issue documents, 17 rejected
  alternatives from 16 issues, 10/153 questions traceable; raw recall@3 drops 0.373 → 0.242 because
  the question's own issue document ranks 1 in 147/153 (the question set is built from issue
  titles); commit-only recall@3 0.392 vs 0.373 (p=0.25, not significant); ingestion does not
  degrade commit ranking.
- T4b egui fetch done (43 min): 272 issue documents, 4 not found, 782 cross-repo references
  skipped, 66 documents with alternatives, 98 rejected alternatives in the corpus. **The index
  build failed with a raw `IntegrityError`** after 276 s: a timeline carries one
  `cross-referenced` event per mention, so the same unmerged PR was appended twice as an
  alternative and collided on the `alternatives (name, document_ref)` primary key
  (`index.py:181`); names also carried trailing whitespace. Fix delegated (T3c, below).
- T3b done (`a203896`): one up-front stderr line when `--github` runs with network off, offline
  cache-miss warnings collapsed into one count line, `--network-enabled` documented next to
  `--github`; `build_documents` signature unchanged (collapsing done at the CLI boundary by
  filtering the builder's stderr).
- T3c done (`4fb0c87`): dedupe by source `(repo, number)` before fetching, `_dedupe_alternative_names`
  strips/collapses whitespace and suffixes ` (#N)` on title collisions from distinct sources;
  integration test indexes a built document through the real `build_candidate` path. egui corpus
  regenerated offline from the cache (5 network calls, all for the 4 not-found retries; the T3b
  up-front line and collapsed summary showed up as designed), 272 issue docs, 66 with alternatives,
  98 rejected alternatives, 0 real duplicate names (verified by parsing the YAML; a per-line `rg`
  check gives false positives because `yaml.safe_dump` wraps long titles).
- T4b egui measured (see Verification evidence): raw recall@3 0.554 → 0.410 (own issue doc at
  rank 1 for 82/83); commit-only 0.554 → 0.554 (p=1.0); 98 rejected alternatives, 24/83 questions
  traceable. Combined with leakcanary: 115 rejected alternatives, 34/236 questions, 0 premises.
- T4c publication delegated: promote `paired_github.py` → `evals/retrieval/report_paired.py` and
  `issue_doc_rank.py` → `evals/retrieval/report_issue_document_rank.py` (TDD), copy the eight
  per-question/manifest artifacts into `evals/project-memory/`, write the "Issue ingestion,
  measured 2026-09-21" section, add the README section-4 row, CHANGELOG.
- T4c done (`70b2dd7`): `evals/retrieval/report_paired.py` and
  `evals/retrieval/report_issue_document_rank.py` with tests (7 + 10), eight artifacts
  `evals/project-memory/{leakcanary,egui}-github-ingestion-{ranks,issue-document-ranks,counterfactuals}.jsonl`
  + `-manifest.json`, section "Issue ingestion, measured 2026-09-21" at
  `evals/project-memory/README.md:284`, README section-4 row "Rejected alternatives from GitHub
  (236 questions)", CHANGELOG. The writer caught one wrong number in my notes: the egui index
  build took 316 s (from the log), not 277 s (that was the failed build); the README carries 316.
  Every number was reproduced from the files by the promoted scripts.

## Delivery

Branch `feat/github-issue-pr-ingestion`: 11 commits over `main@fff2a71` (the first, `46eb621`,
is the housekeeping commit from `chore/pin-own-history-eval`). Diffstat vs main: 51 files,
+6,221 / -78; authored lines excluding data artifacts and fixtures ≈ 4,593. That is far above the
≈400-line delivery budget; the `single-pr` strategy was chosen at creation and every commit was
reviewed and acknowledged individually under RDD (lineages `review-de9bbfc688d6402c`,
`review-525080f9c26af8c7`, `review-a1565adf35186fe3`, `review-5d41ab06176e0d8c`,
`review-d17dd086e2e8e06b`; two earlier lineages recovered/abandoned with the user's authorization).
Push, PR (single or chained), and merge remain the user's decisions. Full suite at HEAD: 1497
passed / 18 skipped; ruff and mypy clean.

Reproducibility pin kept outside the repo: `~/bruriah-worktrees/ablation-data/` (clones, the two
GitHub caches, 25 MB + 60 MB, corpora and indexes). Not deleted; the README reproduce commands
regenerate everything from GitHub, but the cache is the only way to rebuild today's exact corpus
offline.

## Verification evidence

- T1: RED `ModuleNotFoundError: No module named 'bruriah.github_read'` (tests written first);
  GREEN `tests/test_github_read.py` 27 passed, `tests/test_github.py` 19 passed unmodified;
  full suite 1393 passed / 18 skipped; ruff clean; mypy clean. Parent spot check re-ran both
  test files: 46 passed; `git diff 46eb621 71fa4d7 -- src/bruriah/github.py` empty.
- T2: RED `ModuleNotFoundError: No module named 'bruriah.issue_links'`; GREEN
  `tests/test_issue_links.py` 39 passed; full suite 1432 passed / 18 skipped; ruff clean; mypy
  clean. Parent spot check: 39 passed; live call on `Fix leak in Foo (#123)` + `Fixes #10, #12` +
  `owner/repo#7` + inline-code `#99` returned 123 self, 10 closes, 12 mentions, 7 cross-repo
  mention, and no 99.
- T3: RED `ModuleNotFoundError: No module named 'bruriah.github_corpus'`; GREEN
  `tests/test_github_corpus.py` 20 passed, 6 new CLI tests passed, `test_gitcorpus.py` +
  `test_git_corpus.py` 19 passed unmodified; full suite 1458 passed / 18 skipped; ruff clean; mypy
  clean. Parent spot check: 32 passed on the corpus/github CLI selection; `git diff cb0b643~1
  cb0b643 -- contracts.py service.py mcp_server.py` empty.
- R3-001 fix: RED `AssertionError: a foreign-repo timeline cross-reference must never be fetched`
  (3 tests); GREEN 3 passed; full suite 1461 passed / 18 skipped; ruff clean; mypy clean.
- R4-001/R4-002 fix: RED `ImportError: cannot import name 'GitHubRateLimitedError'`; GREEN 58
  passed across the two GitHub test files; full suite 1469 passed / 18 skipped; ruff clean; mypy
  clean.
- R4-001 (mid-body) fix: RED 3 failed (raw exceptions escaped at github_read.py:138); GREEN 3
  passed; full suite 1472 passed / 18 skipped; ruff clean; mypy clean; native targeted validation
  approved; acknowledged.
- T4a: RED `ModuleNotFoundError: No module named 'report_counterfactuals'`; GREEN 2 passed; full
  suite 1474 passed / 18 skipped; ruff (src tests evals) clean; mypy src clean (CI does not
  type-check evals). Parent spot check: 2 passed. Native review approved without findings.
- T3b: RED 2 of 3 new CLI tests failed before implementation; GREEN 9 github CLI tests passed;
  full suite 1477 passed / 18 skipped; ruff clean; mypy clean. Parent spot check: 9 passed;
  `git diff f8d2255 a203896 -- github_corpus.py github_read.py` empty. Native review approved
  without findings; acknowledged.
- T4b leakcanary (index `leakcanary-github-jina`, 1,206 docs, 186 s, jina-v2-base-es, K=60, no
  reranker, 153 questions, paired against the published `leakcanary-embedder-ablation-jina.jsonl`):
  raw commit recall@3 0.373 → 0.242, recall@10 0.471 → 0.399, MRR@10 0.330 → 0.144, 0 entered /
  20 left top-3, p<0.0001; own issue document at rank 1 for 147/153 and above the commit truth for
  150/153; commit truth ranked among commit documents only: recall@3 0.392, recall@10 0.458,
  3 entered / 0 left, p=0.25, 76/153 ranks identical. `report_counterfactuals.py`: alternatives
  17 (all GitHub-derived), premises 0, questions with issue document 150/153, questions with a
  traceable alternative 10/153.
- T3c: RED `tests/test_github_corpus.py:374: assert 2 == 1` (+2 more failures); GREEN 27 passed;
  full suite 1480 passed / 18 skipped; ruff clean; mypy clean. Parent spot check: 27 passed.
  Native review approved without findings; acknowledged.
- T4b egui (index `egui-github-jina`, 2,452 docs / 5,384 passages, 277 s, same setup, 83
  questions, paired against `egui-embedder-ablation-jina.jsonl`): raw recall@3 0.554 → 0.410,
  recall@10 0.723 → 0.687, MRR@10 0.473 → 0.243, 0 entered / 12 left, p=0.0005; own issue doc rank
  1 for 82/83, above truth for 82/83, missing 1; commit-only recall@3 0.554 → 0.554, recall@10
  0.723 → 0.735, 3 entered / 3 left, p=1.0, 53/83 identical. `report_counterfactuals.py`:
  alternatives 98 (GitHub), premises 0, issue doc 82/83, traceable 24/83. Fetch: 43 min online,
  cache 60 MB / 3,118 files; leakcanary cache 25 MB / 1,281 files.

## Next step

Feature complete on the branch. User decides push / PR shape / merge. Follow-ups outside this feature: (1) a held-out question set not derived from issue titles, to make a retrieval claim for issue ingestion; (2) `bruriah index` should raise a named duplicate-alternative error instead of a raw `IntegrityError`; (3) T5 touched-paths ablation. Previously planned: index leakcanary/egui twice (with and without the GitHub documents, jina, K=60, no reranker), run `report_reach.py` paired + McNemar and `report_counterfactuals.py`, publish in `evals/project-memory/README.md`; then T3b.
