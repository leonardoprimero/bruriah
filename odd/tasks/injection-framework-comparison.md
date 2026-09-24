# Feature: injection-framework-comparison

**Branch:** `feat/injection-framework-comparison` (from `main` at 6a50823, release 2.0.1)
**Created:** 2026-09-24
**Route:** delegated direct (one bounded writer per task)
**TDD:** strict (global session configuration). Runner: `uv run pytest -q -p no:cacheprovider`
**Delivery strategy:** `ask-on-risk`
**RDD:** enabled for this repo. Last reviewed boundary: 6a50823 (2.0.1 on main).
**Release:** minor (new eval surface, no `src/` changes expected).

## Objective

Add LlamaIndex and LangChain comparison rows to `evals/injection`: the same 17 attacker
surfaces, the same markers, the same control-run provenance, run through each framework's
typical agent retrieval-as-tool pattern, so the report can state what each ARCHITECTURE
returns to the model -- not which framework is "better".

## Problem

The benchmark proves Bruriah's boundary holds (ASR 0/17) but proves nothing about why that
number is interesting. The claim that motivates the whole design -- "a retriever tool that
returns chunk text gives the attacker a direct channel into the model context" -- is currently
prose. Without measured rows for the two dominant frameworks, a reader cannot tell whether
0/17 is an achievement or a triviality.

## Why this is dangerous to do naively

A naive run gives roughly 17/17 vs 0/17. That number is TRUE and still reads as a strawman:
returning retrieved text to the model is what RAG is FOR -- it is the design, not a defect.
A comparison framed as "they are insecure" would damage the honesty-as-brand positioning this
repo has spent two releases building. Every design decision below exists to keep the
comparison architecturally honest.

## Design decisions

1. **Compare architectures, not quality.** Each framework's row measures its typical agent
   retrieval-as-tool pattern: a retriever tool whose serialized tool output returns retrieved
   text to the model. Bruriah's row measures `investigate_work` (opaque refs; text only via an
   explicit `read_evidence` call). The report never ranks retrieval quality, latency, or
   features. Retrieval quality is deliberately degenerate (fake embeddings, `k` covering the
   whole fixture corpus) so ranking luck cannot decide a leak.
2. **The measured channel is the serialized tool output** -- the string an agent framework
   would place into a tool message -- exactly parallel to `investigate_work`'s
   `model_dump(mode="json")`. Same normalized marker matching, same control-run provenance
   (poisoned vs control at the same channel), same executed-path invariant: a case only counts
   when the carrying document was actually retrieved in BOTH the poisoned and the control run;
   otherwise the run fails rather than reporting "held".
3. **Same fixtures, byte for byte.** Each `InjectionCase.build`/`build_control` already writes
   a corpus directory of text files. The framework adapters ingest that same directory. No
   second fixture set, no drift between rows. Cases whose Bruriah-side entry point has no
   framework equivalent (`code_target` on the three `markdown+git` cases) still apply: the
   poisoned surface lives in the corpus files, and the framework row queries with the same
   task string.
4. **Include each framework's built-in mitigations, if any, at the retriever-tool boundary**
   (checked against current docs with context7; re-verify at implementation time). Exploration
   finding 2026-09-24: neither framework sanitizes or fences retrieved content on this path by
   default -- LangChain's built-in middleware covers moderation/caching/emulation, not
   retrieved-content isolation; LlamaIndex's `RetrieverTool`/`QueryEngineTool` return node
   content directly. If a relevant mitigation exists or ships later, its configured row is
   added NEXT TO the default row, never instead of it.
5. **Bruriah's cost goes in the same table.** The agent needs two calls (`investigate_work`,
   then `read_evidence` per reference) to see content the frameworks return in one. The table
   has a "calls to reach content" column so the trade is visible, not buried in prose.
6. **Hermetic, like everything else in `evals/`.** LlamaIndex `MockEmbedding` and LangChain
   `DeterministicFakeEmbedding`, in-memory vector stores, no network, no model download, no
   LLM in the loop (the tool output is inspected directly; no agent executes).
7. **Optional dependency group, absent from CI.** A `frameworks-compare` dependency group
   (`llama-index-core`, `langchain-core` -- core packages only, pinned) that CI never
   installs. The framework runner and its tests skip cleanly (pytest `skipif` on import) when
   the group is absent. The core benchmark (`run.py`, `report.json`) stays exactly as
   dependency-free as it is today.
8. **Separate artifacts.** `evals/injection/frameworks/` with its own runner writing
   `report-frameworks.json` / `report-frameworks.md`. The existing `report.json`/`report.md`
   remain byte-identical products of the core benchmark alone, so their determinism guarantee
   never depends on optional packages.
9. **Wording, everywhere it appears** (report, `evals/injection/README.md`, root README):
   "returns retrieved text to the model by design", never "is insecure". The comparison table
   caption states explicitly that returning text is the intended behavior of a RAG retriever
   tool, and that the rows measure an architectural property, not a defect.

## Scope

- `evals/injection/frameworks/`: two adapters (LlamaIndex, LangChain), a runner, reports.
- A pytest module for the adapters and the comparison invariants, skipped without the group.
- `pyproject.toml`: the `frameworks-compare` dependency group, pinned.
- Docs: `evals/injection/README.md` comparison section; root README table update.

Out of scope: any `src/` change; agent-in-the-loop simulation; measuring `read_evidence` as a
leak (it is the explicit channel, on both sides of the comparison); more frameworks (Haystack
etc. -- the harness shape should admit them later, but no speculative abstraction now).

## Constraints

- One surface per case, control run per case, executed-path invariant -- inherited from the
  core benchmark, enforced with the same force in the framework runner.
- Deterministic: the framework runner run twice produces byte-identical reports (fake
  embeddings are seeded/constant; retrieval `k` covers the full fixture corpus).
- Public repository: same disclosure posture as the core benchmark -- surface categories and
  outcomes, no ready-to-use exploit recipe.
- CI stays green with and without the optional group installed.

## Exploration findings (2026-09-24, current docs via context7)

- **LangChain**: `create_retriever_tool(retriever, name, description, *, document_prompt=None,
  document_separator="\n\n", response_format="content")` in `langchain_core.tools.retriever`
  builds a `StructuredTool` whose content is the retrieved `Document.page_content` values
  joined by `document_separator` (via `format_document`). That joined string is the
  `ToolMessage` content -- the measured channel. `DeterministicFakeEmbedding` and
  `InMemoryVectorStore` live in `langchain_core`, so `langchain-core` alone suffices.
- **LlamaIndex**: `RetrieverTool`/`QueryEngineTool` in `llama_index.core.tools`; a retriever
  returns `NodeWithScore` objects whose `.text` is the chunk prose, and the tool's
  `ToolOutput.content` carries node content to the agent. `MockEmbedding` lives in
  `llama_index.core.embeddings`; `llama-index-core` alone suffices (the `llama-index` bundle
  would drag OpenAI packages -- do not use it).
- Neither framework's default retriever-tool path applies a mitigation to retrieved content
  (see design decision 4; re-verify against pinned versions during T1/T2).

## Tasks

- [ ] **T0 -- RED.** `tests/test_injection_frameworks.py`: specify the adapter contract
  (build index from a case's corpus dir, run task, return serialized tool output + retrieved
  document identities for the executed-proof), the control-run provenance, the executed-path
  invariant, and the report shape. Module-level `skipif` when the group is absent.
- [ ] **T1 -- LlamaIndex adapter.** `MockEmbedding`, in-memory index over each case's
  `corpus_dir`, `RetrieverTool`-equivalent output serialization, `k` = corpus size. Verify
  design decision 4 against the pinned version; record the result here.
- [ ] **T2 -- LangChain adapter.** `DeterministicFakeEmbedding` + `InMemoryVectorStore` +
  `create_retriever_tool` defaults, same invariants. Verify design decision 4 against the
  pinned version; record the result here.
- [ ] **T3 -- Runner and report.** `evals/injection/frameworks/run.py` over the same 17
  `CASES`; `report-frameworks.{json,md}` with the comparison table (per-surface outcome per
  row, plus the "calls to reach content" column). Byte-identical across runs.
- [ ] **T4 -- Docs.** `evals/injection/README.md` comparison section with the wording rules of
  design decision 9; root README table; CHANGELOG.

## Acceptance criteria

- `uv run --group frameworks-compare python evals/injection/frameworks/run.py` produces both
  reports offline, deterministically; running it twice is byte-identical.
- Every framework case reports `executed: true` and `control_executed: true` (the carrying
  document retrieved in both runs), or the run fails.
- Without the group installed: full suite passes, framework tests skip (not fail), core
  `report.json`/`report.md` untouched.
- The published table contains Bruriah's cost column and zero instances of "insecure" (or a
  synonym) applied to a framework.

## Applicable checks

`uv run pytest -q -p no:cacheprovider`, `uv run ruff check src tests evals scripts`,
`uv run mypy src`.

## Progress / evidence

- 2026-09-24: ODD doc created. API exploration done against current docs (context7); findings
  recorded above. Branch not yet created.
