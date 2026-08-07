# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
and the entries here name what changed for *you* rather than which files moved.

## [Unreleased]

### Security: `cryptography` moves to 50

`PYSEC-2026-3552` affects `cryptography` 49, and the fix is 50.0.0 — outside the `<50` ceiling this
project declared, so the advisory could not be resolved by re-locking alone. The ceiling is now
`<51` and the lock is regenerated.

Nothing in this package had to change for it. Bruriah uses six symbols from that library —
`Ed25519PrivateKey`, `Ed25519PublicKey`, `InvalidSignature`, `Encoding`, `PrivateFormat.PKCS8` and
`PublicFormat.Raw` — all of them stable API that the major release does not touch. The full suite
passes unchanged.

### Corrected: `supported` is not a state this tool can return

The front page advertised responses as carrying `supported` / `stale` / `expired` / `unknown`.
Three of those are real and one is not. `stale`, `expired` and `unknown` are values of
`EvidenceRecord.freshness`, whose fourth value is **`current`** — the list omitted the good one and
substituted `supported`, which belongs to `ClaimRecord.state`, a different field on a different
record.

And no response carries it, because `claims` is always `[]`. The same page said an empty `claims`
list was "the design holding, not a feature that has not landed". Claim formation is not wired into
`investigate()`: the code that builds claims is only reached on the paths that do not retrieve, so
the list is empty for every corpus in every configuration, research or not. The evaluation harness
in this repository already said so where it refuses to score those cells; the README did not.

Both statements now match the code, and the `freshness` list reads `current` / `stale` / `expired`
/ `unknown`.

### Fixed: nothing warned you that the bundled packs stop this tool on a fixed date

Every bundled pack carries `expires_at`, and `load_registry` is fail-closed and all-or-nothing —
one expired pack fails the whole registry, and `load_deps` builds `serve` and `doctor` on top of
it. Verified by injecting the date: on **2027-07-23** everything loads, and on **2027-07-24**
`load_registry` raises `registry_load_failed:expired_pack`. An installation nobody touched stops
serving, and every test of it still passes, because every test in this repository pins `today` to a
2026 literal — including the one asserting the expiry fires in 2027. The suite was arranged to
confirm the deadline rather than to raise the alarm before it.

Two warnings now exist where there were none worth the name:

- **`bruriah doctor` warns 90 days out**, and says what happens: *"expires in N day(s), on DATE;
  after that bruriah stops serving until it ships re-signed packs"*. There was a warning before,
  but it gave seven days and was worded "goes stale" — a stale pack still works, an expired one
  does not, and the two only looked equivalent because these packs set `expires_at` to exactly
  `reviewed_at + freshness_days`.
- **The test suite fails 120 days out**, so the maintainer finds out from CI months before any
  user finds out from a broken install.

Note what this does *not* do: the packs are signed, so a later `expires_at` needs a new signature.
Nothing here extends any deadline — it makes the existing one impossible to walk into unaware.

### Corrected: reranking does not lose on `emilk/egui` — that was a bug, and it is fixed

The reranking write-up said this stage made things *worse* on one of the two foreign corpora, and
that reranking deeper made it worse still. Both of those came from a run taken before `d767aab`,
the commit that stopped the cross-encoder reading the first 4,000 characters of a document instead
of the passage that actually matched — which on egui meant reading a pull-request template. The
configuration that claim rested on was never re-run after the fix.

It has been now, on a fresh clone:

| egui, `jina-v2-base-es` | top 20 | top 40 *(shipped)* | no reranker |
|---|---|---|---|
| before the fix *(previously published)* | 0.518 | **0.494** | 0.530 |
| after the fix | **0.566** | **0.542** | 0.518 |

Both depths lost to the baseline before; both beat it now. **No corpus measured here is hurt by
reranking any more.** What survives is milder: depth 20 suits egui and depth 40 suits leakcanary,
so the best depth still depends on your corpus — which is why 40 remains a default rather than a
tuned value, and why the stage remains opt-in.

`evals/retrieval/run_ablation.py` grew a `--depth` flag. The depth table could previously only be
produced by editing a module constant between runs, which is why it named no command and why the
claim could not be checked when it was questioned.

### Fixed: a request that ran out of time came back labelled `complete`

`status` was derived from one flag, `truncated`, which retrieval sets only when a result hits
`max_candidates` or `max_extracted_chars`. Everything else it reports — a corpus scan stopped by
`max_elapsed_ms`, a vector leg that was unavailable or raised, a reranker that failed — went into
`degradation` and left `truncated` alone. So the response said `complete` in exactly the cases
where the client had a prefix of the picture rather than the picture.

`status` now reads `degradation` too. Not all of it: entries that disclose a rule this engine
*applied* — `reranked:N_documents`, `lexical_leg_discounted:...` — are not shortfalls, and marking
those `partial` would turn the field into "something is disclosed here" instead of "you got less
than this engine can give". The split is fail-closed (`retrieval.is_shortfall`): anything not
explicitly named a disclosure counts as a shortfall, so a degradation added later over-reports
rather than repeating this.

**Expect more `partial` than before**, and expect it to be accurate. In particular a request with
no embedder configured is `partial`, because half the ranking is not running.

Relatedly, the reranking stage now checks the clock on the way out as well as in. It cannot bound
the call — the reranker is one opaque invocation supplied by you — but forty cross-encoder passes
used to run after the only deadline check had already passed, and `search` returned late reporting
nothing at all.

### Fixed: `--reranker` was halving how many documents you got back

Turning the reranking stage on grouped every document's passages together in the result. Because
`max_candidates` counts passages, not documents, a few long documents could spend the whole budget
— so the stage returned **fewer distinct documents than not using it at all**.

Measured with a reranker that returns the same score for every document, which by the stage's own
contract is a no-op, so this is the stage and not any model: it changed the returned set on 100% of
the 236 foreign-corpus questions, took distinct documents at the default budget from 48.4 to 24.3
on `square/leakcanary` and from 46.1 to 17.8 on `emilk/egui`, and dropped the recorded answer out
of the result entirely for 21 of them.

Passages now interleave across documents instead of grouping. The documents come back in exactly
the order the reranker put them in — a document's first passage sits where it always did — so
**no published recall figure changes**, and that was verified rather than assumed: across those
236 questions at two budgets, the document-level rank of every answer moved for zero of them. All
21 dropped answers return, and distinct documents at the default budget go to 49.5 and 50.0, above
the no-reranker baseline, because one passage per document is the most breadth a passage budget can
buy.

If you were relying on a document's passages arriving as one contiguous run, they no longer do.
That grouping was never measured to be worth anything; this is what it cost.

### Corrected: this tool does not abstain when your corpus has no answer

The `investigate_work` description promised it abstained "rather than answering from whatever
looked closest", and the front-page comparison table said it "abstains, and says which domain it
lacks". Both overstated what the code does, and both now say what it does instead.

There *is* an abstention and it is real, but its gate is **registration, not relevance**: it fires
when no approved pack covers the request's domain, it decides from the request text and the signed
registry, and it runs before retrieval. Once it proceeds, ranking returns its best candidates
whether or not any of them answers the question. There is no relevance floor. The abstention also
never named the domain it lacked — `Gap` is a closed set of four strings and none of them carries
one.

Adding that floor was attempted and refused by measurement, which is the more useful half of this
entry. RRF discards score magnitude by construction, so there is nowhere in the fused ranking to
put a threshold; what was measured instead was *separation* — how far a query's best score stands
out from the spread of its own scores, which is a property of the search and not a confidence
attached to any passage. It looked strong (AUC 0.856) until the same questions were asked of a
corpus that could not answer them: pooled, the vector leg then read 117/236, p=0.948. A coin. Split
by corpus it was two large opposite effects that cancelled, one of them saying the *wrong* corpus
separates more.

Nothing from that work ships in the engine. It lives in `evals/retrieval/separation.py` with its
472 per-question results committed beside it, written up under "Separation does not detect an
unanswerable question" in `evals/project-memory/README.md`. If you were relying on the old promise,
the practical change is: treat a returned ref as a place to look, never as a claim that the answer
is in it.

### Optional cross-encoder reranking — `--reranker`

`bruriah ask` and `bruriah serve` take `--reranker MODEL`. It reorders the top 40 documents a
search returns, using a cross-encoder that reads the query and each document together instead of
comparing two vectors that were embedded without knowing about each other. It is **off unless you
name a model**, and it needs no re-index: unlike `--model`, a reranker touches no stored vector, so
it can be added, swapped or dropped against an existing snapshot.

What it changes, measured on the two foreign corpora and this repository's own history:

| | recall@3 before | after |
|---|---|---|
| `square/leakcanary`, 153 independent-source questions | 0.340 | **0.431** |
| `emilk/egui`, 83 independent-source questions | 0.518 | **0.542** *(re-measured, see below)* |
| this repository, 12 English questions | 0.833 | **1.000** |
| this repository, 12 Spanish questions | 0.500 | **0.917** |

Three things are worth knowing before you turn it on.

**It is not free, and how much it helps depends on your corpus.** Roughly 7 seconds per query and a
second ~1 GB model download. The egui figures above were rewritten while preparing this release:
they originally read 0.530 → 0.494 and were quoted as this stage making things *worse* on one of
the two foreign corpora. That measurement predated the pull-request-template fix, and re-running it
afterwards reversed the sign — see "reranking does not lose on `emilk/egui`" below. No corpus
measured here is hurt by reranking. Depth still is corpus-dependent, though: 20 suits egui and 40
suits leakcanary, so measure it on your own before turning it on. The flag needs no re-index, so
that is one command.

**An English-only reranker is dangerous on a non-English query.** `ms-marco-MiniLM-L-6` scores
confidently in a language it cannot read, and reordering by those scores cut Spanish recall@10 from
0.917 to 0.667 — worse than not reranking at all. There is no safe default across languages, which
is why there is no default.

**Every failure degrades to the shipped ranking rather than to an error**, and says which failure
it was in `degradation`: a model that raises, returns the wrong number of scores, returns something
that is not a number, or arrives after `max_elapsed_ms` has passed. Reranking also never changes
*which* evidence is returned — only its order — so `lexical_rank` and `vector_rank` keep describing
the list you actually received.

### Two claims on the front page were wrong and are corrected

The README said 58% Spanish recall@3 was "the vector leg's own ceiling" and that the cross-lingual
fix "does not make cross-lingual retrieval as good as same-language retrieval". Both were true of
the pipeline that existed when they were written; neither survives the measurement above.

The same eval also corrects something larger. The correct document is somewhere in the returned
pool **76%** of the time on leakcanary and **92%** on egui, against recall@3 of 0.340 and 0.530 —
so recall@10 was understating the ranking headroom by about half, and everything this project has
said about "a ranking problem" was right and too modest.

## [0.5.0] — 2026-07-27

The live research path, which 0.4.1 deliberately left alone. Four defects, all confirmed by
running code against a loopback TLS server, none of them reachable through `bruriah serve` today —
`ServiceDeps.research` is still `None` everywhere the installed package builds it. They are fixed
now rather than when the path is wired, because "we will remember when we turn it on" is not a
control.

### A cache hit no longer outlives the policy that allowed it

`research()` read the cache before checking the allowlist and the access policy, on the reasoning
that a hit costs no network. True, and beside the point: those lists do not exist to save
bandwidth, they declare which destinations this installation may surface at all. Revoking a host
stopped nothing — the entry kept being served, in full, for the rest of its 24-hour TTL. Same for
a path the access policy denies. Both checks are local, so they now run first at no cost.

`policy_version` was recorded on every cache write from the beginning and never read back. So a
cached answer also outlived the *pack* that authorised it: re-signing a research policy with
different source rules changed nothing about what was already on disk. An entry written under a
different policy version is now a miss and is refetched under the current rules. Neither case
deletes anything — TTL and `prune_expired` still own removal, and a policy change is not grounds
for destroying an audit-relevant artifact.

### `read_evidence` can read the refs `investigate_work` hands out

`fetch.py` mints `live:sha256:<32 hex>` for captured live evidence and `investigate_work` returns
it. `read_evidence` had no branch for that prefix, so those refs fell through to the local passage
table, found nothing, and came back `missing_ref` — the two tools disagreeing about refs one of
them had just issued.

They resolve from the cache now, by scanning for the entry whose evidence carries the ref. The
content served is the stored excerpt, never a refetch: `read_evidence` is documented read-only and
resolving *immutable* refs, and a ref that reaches the network on read is not immutable. The
excerpt is also already the permitted minimum computed under the reuse rules — capped at 280
characters whenever reuse is anything but `permitted` — so serving anything larger would route
around that cap through a different door.

One consequence worth naming: `expired_ref` becomes reachable. It was declared in the contract and
documented as structurally impossible from a single active snapshot, which has no history to age
out. Cached live evidence does. An aged-out ref returns `expired_ref` with no content, because
"had it, it aged out" is a different answer from "never had it" and neither justifies presenting
stale material as current.

### Refusal is the cheap path again

A redirect, a non-2xx and a rejected Content-Type each discarded their body with `response.read()`
and no argument — reading to EOF into memory with no ceiling at all. So `max_bytes` bounded the
one response shape that was accepted and none of the three that were refused: an allowlisted host
could spend this process's memory precisely by being refused. Measured: 3,000,000 bytes read on
each of the three paths against a declared `max_bytes` of 1,000,000. They now read at most one
chunk, which is politeness rather than necessity — the connection is closed immediately after, and
`Connection: close` is already in the outbound headers.

While measuring that: the accepted path overran too. The byte ceiling was checked *after* each
64 KB chunk landed, so `max_bytes` really meant "max_bytes plus up to one chunk" — a declared
1,000,000 read 1,062,144. Reads are now sized to at most one byte past the ceiling, which is all
the evidence an overrun needs.

### The network budget belongs to the investigation, not to each URL

`Budgets` is declared once per request, and `fetch.py` enforced it faithfully — for the single
hop-chain of a single call. Nothing pooled it. Each candidate URL received the same
`request.budgets` again, so a request declaring `max_bytes=1_000_000` and
`max_network_requests=5` made five connections and pulled 2,500,000 bytes, with every individual
fetch honestly inside its budget the whole time. `max_elapsed_ms` multiplied the same way. What
the host declared as the cost of an investigation was really the cost of one URL inside it.

A `NetworkLedger` now pools requests, bytes and wall-clock across every research call of one
investigation, and is charged for what each call actually spent — including calls refused
mid-flight, since billing only successes would make failure the cheap way to drain a budget. A
cache hit costs nothing, having made no request and read no socket. Candidates the pool cannot
fund come back as `research_unavailable:network_budget_exhausted` rather than being silently
dropped.

`fetch()` reports `requests_made`/`bytes_read` on every return path to make this possible,
including its error paths — a call refused after three hops used to report the same nothing as one
rejected on its scheme.

### The README stopped overstating one thing

"The step that chose *which* document to surface never saw a word of any document" was true of
routing and false of ranking, which is BM25 and a vector leg reading your corpus text, because
ordering passages by relevance cannot be done without reading them. The comparison table said
"selection never reads corpus prose" without saying which selection. Both now name the step. The
protection being described is unchanged and is stated more precisely: corpus content decides which
passages rank highest, it does not decide which sources are admissible, and it never reaches the
agent as prose.

Two other audit findings were checked and not acted on, for reasons recorded in the pull request:
`README:223` already explains why `claims` is empty for a local corpus, and
`docs/client-guidance.md` already declines to claim any client/platform pair has passed evaluation.

## [0.4.1] — 2026-07-27

### The date format the tool advertises is now a date format the tool accepts

`investigate_work` publishes its `inputSchema` straight from the frozen request model, and that
schema describes `as_of` as `{"format": "date", "type": "string"}` — which is the only way a
JSON-RPC client *can* send a date, since JSON has no date type. The server then rejected exactly
that. The request models set `strict=True`, and the tool boundary validated the already-parsed
arguments in pydantic's Python-object mode, where a `date` field accepts only a `datetime.date`
instance. A client that read the published schema and did what it said got `invalid_request`.

Validation now runs in JSON mode, which is the mode the payload was written in. Nothing was
loosened: `extra="forbid"`, strict scalars (a bool is still not an int, `"5"` is still not `5`),
range bounds, `Literal` members, unparseable dates and every cross-field validator reject exactly
as before — there is a test that walks each of those. `cache.py` already used this pattern for the
same reason; the boundary where it mattered most was the one place that never adopted it.

### `max_output_chars` binds the path that actually returns evidence

The declared output budget was enforced only where `context.assemble_context` produced the result,
which is the routed-or-abstained path. The proceeding path — the one that carries retrieved
evidence, and therefore the largest response this tool produces — built its result inline and
returned it unchecked. A request declaring 256 characters received 2581, labelled `complete`.
`max_evidence` did not cover for it: that ceiling bounds how many records come back, not how big
they are.

Compaction also has to tell the truth when it cannot win. Two ways it did not. It measured the
evidence copy and then returned something ~110 characters larger, because the
`output_budget_compacted` marker and the `ctx-compacted:<digest>:<n>` cursor are appended *after*
the stop decision — so a response could stop at "now it fits" and cross back over on the way out.
And when nothing was droppable it returned silently, still over budget, still `complete`. It now
measures the payload it actually returns, and a budget it cannot meet is reported as
`output_budget_unmet` with the status downgraded.

Worth knowing if you set this field: `Budgets` allows `max_output_chars` as low as 256, but an
entirely empty result already serializes to 470 characters — `request_id` is a 71-character digest
and `budgets` echoes all ten of its fields. Anything you declare below ~470 is unreachable by
construction. You will be told rather than quietly overrun.

### Every GitHub Action is pinned to a commit SHA

`@v4` and `@release/v1` are mutable pointers, not versions: they can be moved between one run and
the next. The sharpest instance was `pypa/gh-action-pypi-publish@release/v1` — a *branch* ref on
the only job holding `id-token: write`. Removing the stored API token in 0.4.0 bought less than it
looked like while the code minting the short-lived one was still mutable. Every third-party action
now names a 40-character SHA with its version in a trailing comment, and a new CI job fails the
build if a step is ever added without one, so this does not decay the next time someone adds a
step.

None of this changes any behaviour you would see at the CLI, and no index or snapshot is affected.

## [0.4.0] — 2026-07-27

### It can state its own version, and the compatibility gate now asks about the real one

`bruriah --version` exists. `docs/client-guidance.md` had been telling readers to run it to find
out which router version a config targets, while the flag did not exist and the doc asserted
`0.1.0` — through three releases in which that stopped being true. The doc and `SECURITY.md` now
name no number at all: a version written into prose goes stale silently while reading as though
somebody maintained it.

The same hardcoded `0.1.0` was doing damage where nobody could see it. `check_router_compatibility`
gates a pack on `min_router_version <= router <= max_router`, and every loader defaulted the middle
term to `0.1.0`. A pack requiring the current version would have been **rejected by a router that
is that version**. The gate was not dormant, it was answering about a release this package had not
been since its first. It now defaults to the installed `__version__`, the way `clients.py` already
did.

One thing that looks like the same bug is not, and is deliberately unchanged: `service_version` in
`BuildConfig` stays `"0.1.0"`. It belongs with `parser_version="corpus-v1"` and
`ranking_config="rrf-v1"` — a marker of the snapshot contract, carried into the metadata
`promote_candidate` validates. Wiring it to the package version would put every release into the
index identity, so a patch bump would refuse your existing snapshot and re-embed your whole corpus.
It moves when the snapshot contract moves. There is now a comment at the assignment saying so.

### The install contract says what the code actually imports

`cryptography` and `anyio` are imported by name in `src/bruriah/` and were never declared. They
arrived as transitive passengers of `mcp`: an install that worked by luck and would have broken the
day a dependency dropped them. Both are declared now.

`sqlite-vec` went the other way — a runtime dependency with no runtime caller. Nothing under `src/`
imports it: the vector leg reads float blobs out of SQLite and scores them in Python. Its one
importer is the eval harness, which opens the legacy `cerebro.db` to measure this router against
its predecessor — a benchmark, not the product. It is now a dev dependency, so installing Bruriah
no longer builds a compiled extension nothing it ships will call. If an ANN index ever lands, it
arrives with the code that uses it.

### CI runs the security tool it already declared

`pip-audit` sat in the dev dependency group from the beginning and no job ever ran it — the cost of
declaring a security tool with none of the signal. It audits the locked resolution, which is what
CI tests and what `uv sync --locked` reproduces, in its own job so an advisory published overnight
does not withhold the test results for the change under review.

### Everything merged since 0.3.0 and never released

0.3.0 is what PyPI has been serving while `main` accumulated eighteen commits of fixes nobody could
install. They are in this release: `ask` no longer leaks the snapshot it opens, and the suite fails
on a connection that outlives its test; `index` closes the connections that made a generation
undeletable and no longer lets a superseded generation veto a valid candidate; `index-prune` lands
as the counterpart to dropping a generation; the read command the CLI suggests can be pasted, and
the character offset it printed is no longer called a line number; the fastembed pooling notice is
silenced, and only that one. `bruriah corpus` now reports **what share of your history carried no
reasoning** — a repository yielding three documents from three commits and one yielding three from
three hundred used to print the same number. CI runs the quickstart the README states rather than a
copy that had already drifted, and publication goes through Trusted Publishing.

## [0.3.0] — 2026-07-26

### The skill-dispatch ceiling is operator-configurable

Six first-party skills ship and the ceiling admitted five, so every install reported
`skill_ceiling_exceeded:1` and had no way to do anything about it — or even to learn what the
number was. It now resolves like every other setting: `--skill-ceiling`, then
`BRURIAH_SKILL_CEILING`, then `skill_ceiling` in `config.json`, then the unchanged default of five.
`bruriah doctor` prints it.

It is deliberately **not** a `Budgets` field, and that is the whole design of the change. Budgets
are declared by the calling host and echoed back in every response; putting the ceiling there would
both alter the response every pre-skills client receives and hand the number to the least trusted
party in the exchange. `dispatch` orders and truncates *before* it consults the host inventory
precisely so a host cannot influence which skills are selected — a host-declared ceiling would have
returned through the front door what that ordering exists to keep out. The operator can set it,
because the operator runs the CLI and owns the config file.

Zero is legal and means something: dispatch nothing, and still report everything dropped as a gap.
`true` is not, even though `isinstance(True, int)` holds in Python and would otherwise have made
`{"skill_ceiling": true}` silently mean 1.

The default is unchanged at five. It was not raised to fit the pack: a constant nobody measured
should not quietly become a bigger constant nobody measured. The alphabetical cut is also
unchanged, and remains unrelated to relevance — raising the ceiling avoids the cut rather than
improving it.

A bad ceiling also fails the same way now wherever it came from. The flag carried `type=int`, so
`--skill-ceiling abc` exited 2 with argparse's usage message while `--skill-ceiling -1` exited 1
with a typed `invalid_config` — two formats and two exit codes for one class of mistake.

### Which embedding model to pick, measured

`bruriah index --model` has always existed and nothing said what choosing differently was worth.
It is worth more than the fusion fix that preceded it: `jinaai/jina-embeddings-v2-base-es` takes
Spanish recall@3 from 58% to **75%** and English from 83% to **92%**, improving both at once. The
README had called 58% the vector leg's own ceiling; it was that model's ceiling.

The default does **not** change, and the [eval note](https://github.com/leonardoprimero/bruriah/blob/main/evals/project-memory/)
says why: that model is Spanish-English bilingual and this corpus is 95% English queried in
Spanish, exactly what it was built for. A German or Japanese corpus would likely be worse off.
Bigger is not the axis either — the larger sibling of the default scored worse in both languages
for five times the download. Per-question results are published alongside the averages, because
twelve questions means one question is eight points.

### Documentation

- The README now says **when** state is declared rather than that it exists, so `"claims": []` and
  `authority: "unknown"` on your own git history read as the design holding rather than as work
  left undone. A signed pack declares authority; your commits are covered by none, and retrieval
  deciding otherwise is exactly what the project exists to prevent.

## [0.2.0] — 2026-07-26

The release that makes the published package match its own front page. `0.1.0` shipped without
`bruriah ask` while the README documented it, so the first command a reader tried did not exist.
That is fixed by publishing rather than by editing the README, because the README was right.

### Native Windows support

Windows now runs the same guarantees, not a reduced set. `import bruriah` no longer refuses, and
the capability gate it refuses on was rewritten to ask what it always claimed to ask: not *"which
OS is this"* but *"can this OS keep the promise"*.

- Activation is implemented against the Win32 primitives in `winfs.py` — `LockFileEx` for the
  exclusive lock, `CreateFileW` without `FILE_SHARE_DELETE` plus an explicit reparse-point
  rejection for the no-follow open, and `SetFileInformationByHandle` with
  `FILE_RENAME_FLAG_POSIX_SEMANTICS` for the pointer swap.
- The swap has **no fallback to `os.replace`**. A Windows file with a pending delete keeps its name
  until its last handle closes, so `MoveFileExW` cannot publish under a reader at all. On a volume
  that cannot provide POSIX rename semantics, promotion refuses rather than quietly becoming
  non-atomic.
- SQLite opens the validated snapshot by path rather than through `/dev/fd`, which is safe only
  because the pin holds the name for the descriptor's lifetime. The guarantee is relocated, not
  weakened: POSIX distrusts the name and passes the file, Windows holds the name and can trust it.
- **The one guarantee that does not survive:** owner-only file modes. `os.chmod` on Windows only
  toggles a read-only attribute, so five tests skip with a stated reason instead of passing and
  claiming a protection nobody applied. `bruriah doctor` now reports `owner_only_file_modes` and
  warns, so it is visible from the tool rather than only from the changelog.

### Python 3.14

The `<3.14` ceiling was never justified by a dependency, and it locked out Ubuntu 26.04 LTS, which
ships 3.14 and carries no older interpreter. The suite result is now identical on 3.12, 3.13 and
3.14.

### Fixed

- **Digest-verified packs survived a Windows checkout as corrupt.** The repository had no
  `.gitattributes`, so `core.autocrlf=true` rewrote the signed packs to CRLF and the registry
  failed closed with `registry_load_failed:digest_mismatch` on a clean clone, before any code ran.
- **`bruriah serve` broke if you moved.** `index` persisted the path strings it was handed, so a
  relative `--policy` recorded a path that only resolved from the directory you built in. An MCP
  host launches the server from wherever it likes; the failure surfaced as
  `snapshot_unreadable:invalid_active_target`, blaming an intact snapshot for a path.
- **Line endings could change bytes that are hashed.** Corpus documents are hashed byte-for-byte
  and release manifests are verified by digest, but six writes inherited the platform separator and
  the parser kept whatever separators it found — so a CRLF working tree indexed `\r` into every
  token boundary, and a manifest written on Windows would have failed its own verification.
- **`bruriah init` could not emit a single client config on Windows.** `\` was rejected as a shell
  metacharacter, and every absolute Windows command contains one.
- `promote` no longer reports `durable: false` on every Windows promotion. There is no
  per-directory flush there and NTFS journals the rename, so the flag now means what it says on
  both platforms — a permanently-false durability flag is a false alarm, which costs the same trust
  as a false assurance.

### Added

- `bruriah ask` — query from the terminal, with `--read N` to pull the exact lines, before wiring
  up any MCP client. Present on `main` since before `0.1.0` and documented in the README the whole
  time; this is the release that actually ships it.

### If you already have an index

Nothing forces a rebuild: `parser_version`, `service_version` and the snapshot schema are
unchanged, so an existing index keeps validating. One exception worth knowing — if you built one
from a working tree checked out with CRLF line endings, its passages contain a literal `backslash-r` and its
retrieval is subtly worse. Re-run `bruriah index` and it will be correct; there is no way for the
tool to detect that from the outside, which is why it is written here.

## [0.1.0] — 2026-07-26

First release. Published as [`bruriah`](https://pypi.org/project/bruriah/) on PyPI.

### The two-tool MCP surface

- `investigate_work` returns evidence **references** — locator, digest, provenance, authority,
  freshness, claim state — and never corpus prose.
- `read_evidence` resolves chosen references into exact, bounded, unmodified text.
- Neither writes anything. All mutation lives in the CLI, where a human runs it.
- Built on `mcp.server.lowlevel.Server` rather than FastMCP: FastMCP derives its argument model
  without `extra="forbid"`, so an unknown field is dropped before any handler runs, defeating
  authoritative server-side validation.

### Retrieval

- Hybrid BM25 + local vectors (`sqlite-vec`) over a read-only snapshot, fused with reciprocal rank.
- **Language-aware fusion.** Asked in a language the corpus is not written in, the lexical leg is
  discounted to 0.1 and the discount is disclosed in `degradation`. Spanish recall@3 33% → 58%,
  recall@10 83% → 92%, MRR@10 0.29 → 0.50, with English unchanged at 83% / 92% / 0.80.
- Explicit abstention when no approved policy covers a domain.
- `bruriah corpus` turns a git history's explanatory commits into an indexable corpus.

### Skills

- Six signed first-party skills, active on install, dispatched only when they apply.
- Full lifecycle from the terminal: ingest, analyse, approve, sign, activate, rollback, prune.
- Approval is bound to a content digest; editing approved content fails activation rather than
  carrying the old approval forward.
- Permission envelopes are default-deny, and "allow everything" is inexpressible — no wildcard
  exists in either grammar.
- Activation is an atomic pointer swap under `flock` with `O_NOFOLLOW` and identity
  re-confirmation, retaining two generations for rollback.

### Packs

- Ed25519-signed policy packs with release manifests, digest pinning and fail-closed loading.
- Hardened YAML/JSON parsing: no aliases, no unsafe tags, duplicate keys rejected so a control
  cannot be silently downgraded.

### Platform

- Python 3.12 and 3.13, macOS and Linux.
- Windows is unsupported and raises a legible `ImportError` pointing at WSL, rather than dying
  several frames deep on a missing `fcntl`. *(Superseded — see Unreleased. Left as written: a
  changelog edited retroactively to match the present is no longer a record.)*
- No generative model anywhere in the package. No telemetry. No network by default.

### Known limitations

- Cross-lingual retrieval trails same-language: 58% against 83% at recall@3. That 58% is the vector
  leg's own ceiling, so closing the rest needs a better multilingual signal, not better weighting.
- The eval is twelve questions on one corpus. Treat the direction as established and the figures as
  indicative.
- The lexical leg is a linear scan rather than FTS5; see the scale table in the README for where
  that starts to cost.
- Six bundled skills is a starting point, not a library.
- Live web research ships inert: it needs an operator-defined allowlist that is not distributed.

[0.3.0]: https://github.com/leonardoprimero/bruriah/releases/tag/v0.3.0
[0.2.0]: https://github.com/leonardoprimero/bruriah/releases/tag/v0.2.0
<!-- 0.1.0 was published to PyPI without a git tag, so it links to the artifact that actually
     exists rather than to a release page that never did. -->
[0.1.0]: https://pypi.org/project/bruriah/0.1.0/
