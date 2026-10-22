# Decision documents and mixed-language questions on a real repository

Branch: `feat/mixed-language-repos` from `main` at `22ddcd7`. A local trial on a real 20,783-commit
work repository (private, never committed) showed two gaps: `init --repo` skips the repository's
ADRs, and a Spanish question loses its exact-word leg on an English corpus full of Spanish terms.

## Specs

- S1. Owner request (L3): "si ataquemos las dos dale". The two gaps are the ones the trial found (L2).
- S2. `init --repo` also indexes the repository's decision documents. It looks in the common ADR
  locations, reads only committed content at `HEAD`, never writes inside the repository, and says
  how many decision documents it added. `--limit` keeps limiting commits only. A repository
  without ADR folders behaves and prints exactly as today.
- S3. The cross-lingual lexical discount (`3c8e7c7`, `retrieval.py:_compute_lexical_weight`) keeps
  its measured win: Spanish recall@3 58% and English unchanged on the eval that commit used. It
  stops firing when the question's own words do occur in the corpus, as in an English corpus that
  carries Spanish domain terms. It is still
  disclosed whenever it fires.
- S4. S3 is measured before it is built (spike, T2). The spike stops S3 if no gate improves the
  private trial set without lowering the public eval. A private question set may be used locally
  for measurement only, and none of its text is committed.

## Tasks

| ID | Specs | Route | Commit |
|---|---|---|---|
| T1 | S2 | worker, test-first: ADR folders in `init --repo`, help and README | feature commit (L6) |
| T2 | S3, S4 | worker spike: gate variants measured on the public eval and the private trial set | failed the S4 gate (L5) |
| T3 | S3 | the measured gate, test-first, with its disclosure and docs (only if T2 passes) | dropped (L5) |
| T4 | S2, S3 | re-index the trial repository, re-run the trial questions, PR | trial in L6, PR opened |

## Log

- L1 (2026-10-22, owner): "y no se mandale alguna pregunta vos pero asi bien dificil"
- L2 Trial result on the work repository (offline, 45.5 min, 8,820 documents, 17,855 passages):
  - Four hard paraphrased Spanish questions found 1 of 4. The other 3 were not in the top 20.
  - The same questions with the concrete technical or domain term hit rank 1-2.
  - Spanish queries carried `lexical_leg_discounted:es_query_en_corpus`.
  - `docs/adr/` (63 ADRs) was not indexed: `init --repo` builds its corpus from commits only
    (`cli.py:_run_init_repo_bootstrap`, `gitcorpus.build`).
- L3 (owner, 2026-10-22): "si ataquemos las dos dale".
- L4 The discount is a measured decision, not a bug (`3c8e7c7`): Spanish recall@3 33% -> 58%,
  recall@10 83% -> 92%, MRR@10 0.29 -> 0.50, English unchanged. The weight 0.1 was swept. Its
  premise was "BM25 cannot match across languages at all", which a code-mixed corpus breaks.
- L5 T2 spike result (2026-10-22). The S4 gate fails: S3 is dropped and the discount stays at 0.1.
  - The variants were fixed in advance and hash-pinned before any run: V0 current, V1 no discount,
    V2 vocabulary-overlap gate at 0.3/0.5/0.7, and V3 a weight sliding with vocabulary coverage.
  - Public eval (`bruriah corpus --revision '3c8e7c7^'`, MiniLM as when 3c8e7c7 measured), es
    recall@3/recall@10/MRR@10: V0 .667/.917/.517, V1 .500/.917/.424, best gate .667/.917/.504.
    English is identical in every variant. The digits differ from the published ones (58/92/.50)
    because the corpus generator changed since, but the direction holds.
  - Private set of 16 questions, built before the variants ran, local only and deleted: Spanish
    with domain words recall@3 .875 and MRR .750 under V0, against .750 and .408 with no discount.
    Fully paraphrased Spanish got 0 of 4 in the top 3 under every variant: that is the embedding
    model, not the fusion weight. English .5 in every variant.
  - Why the gate cannot work: Spanish questions find 73-100% of their words in this corpus (50-89%
    even paraphrased), so every threshold turns the discount off almost everywhere, and the
    discount was helping. The premise in L4 was wrong for this corpus.
- L6 T1 done (2026-10-22). `adrcorpus.build` copies committed `*.md` under 8 ADR folders from
  `HEAD` into `corpus/adr/`, and prunes the ones `HEAD` no longer has. `bruriah corpus` is
  unchanged (pinned by a test).
  - Parent review found a path escape: git's `mktree` accepts a tree entry named `..`, and the
    copy joined the raw path. The RED test wrote `escaped.md` outside the test's temp dir. Paths
    with an empty, `.` or `..` component, or that resolve outside `corpus/adr`, are now skipped
    and reported.
  - Trial on the private repository (scratch dir, offline, `--limit 30`, deleted afterwards): 63
    ADRs added in 117 s. A Spanish question about the outbox now returns the outbox ADR at rank 1,
    and an English one about Redis Streams returns that ADR at rank 1. The repository had 0 changes.
  - Full suite 2603 passed, 23 skipped. ruff, ruff format and mypy are clean.
- L7 Native review (risk, resilience, reliability) required one correction: on a case-insensitive
  filesystem a case-only ADR rename made the pruner delete the file it had just written while init
  still reported it (RED reproduced on macOS). `a309b6d` builds `corpus/adr` in a staging folder and
  swaps it in. Approved and acknowledged. Non-blocking follow-ups: case-colliding ADR paths, one
  `git cat-file` per blob, an OSError while writing an ADR aborts init.
