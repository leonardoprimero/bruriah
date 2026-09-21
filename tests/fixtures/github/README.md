# GitHub fixtures

These files are trimmed by hand, not recorded live traffic: each one mirrors the shape of a real
GitHub REST API response but keeps only the fields a document builder needs (`number`, `title`,
`body`, `state`, `state_reason`, `closed_at`, `created_at`, `html_url`, `user.login`, the
`pull_request` presence/`merged`/`merged_at` fields, and `comments` with `body`/`created_at`/
`user`). They exist so `tests/test_github_read.py` and later the issue/PR document builder can run
fully offline against a `ResponseCache` rooted at this directory, with no live GitHub call.

One scenario, one issue thread:

- `issue-41.json` — `GET /repos/{owner}/{repo}/issues/41`. Closed with `state_reason: completed`;
  its body carries an explicit `Premise:` line using the same grammar as commit trailers.
- `issue-41-timeline.json` — `GET /repos/{owner}/{repo}/issues/41/timeline`. One unrelated
  `labeled` event (timeline pages carry more than cross-references) plus two `cross-referenced`
  events, one from PR #50 and one from PR #47.
- `pull-50.json` — `GET /repos/{owner}/{repo}/pulls/50`. Merged: closes #41 for real.
- `pull-47.json` — `GET /repos/{owner}/{repo}/pulls/47`. Closed **without** being merged: a
  rejected alternative approach to #41.
- `pull-47-comments.json` — `GET /repos/{owner}/{repo}/issues/47/comments` (GitHub serves PR
  comments through the issues comments endpoint). Its last comment is the closing rationale.
- `issue-39.json` — `GET /repos/{owner}/{repo}/issues/39`. Closed with `state_reason: not_planned`,
  a duplicate referencing #41.

A second scenario, added for the document builder (`github_corpus.py`, T3), reusing `issue-39.json`
above as the cross-referenced not-planned duplicate rather than duplicating it:

- `issue-100.json` — `GET /repos/{owner}/{repo}/issues/100`. Closed with `state_reason: completed`;
  no `Premise:` line (issue #41 above already covers premise extraction), so this scenario exercises
  a document with alternatives but no premises.
- `issue-100-timeline.json` — one unrelated `labeled` event plus three `cross-referenced` events:
  PR #101 (merged, not a rejected alternative), PR #102 (closed unmerged, no closing comment -- the
  fixed-phrase fallback), and issue #39 (a plain issue cross-reference, no `pull_request` key --
  the `state_reason: not_planned` rejected-alternative path).
- `pull-101.json` — merged; its body ("Fixes #100") is also what the squash-merge `(#N)` subject
  suffix flow reads via `pull_request_self`, so this one PR fixture doubles as the "same issue
  linked by two different commits" scenario.
- `pull-102.json` / `pull-102-comments.json` — closed without merge; comments is an empty list, so
  the reason falls back to the fixed phrase rather than a trimmed comment.
- `issue-39-comments.json` — the closing comment for issue #39's `not_planned` closure, read through
  the same issues-comments endpoint used for PR comments.

The `owner/repo` and timestamps are fictional; the field shapes match GitHub's documented REST
responses as of API version `2022-11-28`.
