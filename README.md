# Cerebro

**An MCP server that lets an agent consult your knowledge without letting your knowledge instruct the agent.**

Cerebro answers one question deterministically: *given this task, what do I actually know that bears on it, and how far can I be trusted about it?* It returns evidence with provenance, freshness, and stated limits — and abstains when it has no authority to answer.

---

## The problem

Point an agent at your notes and you get a retrieval engine: it finds passages that look similar and hands them over. Three things go wrong.

**Anything retrieved becomes an instruction.** A note that says "always deploy straight to prod" is text an agent may simply obey. Retrieval systems do not distinguish *this is evidence you should weigh* from *this is an order*. An attacker who can write one file into your corpus inherits your agent.

**Similarity is not authority.** A four-year-old draft and an official specification score the same if the words match. Nothing records that one supersedes the other, that one expired, or that one was never meant to be quoted.

**Silence is indistinguishable from ignorance.** Ask about a domain the system knows nothing about and you get the closest-looking passages anyway, presented with the same confidence as a real answer.

## What Cerebro does differently

**Selection is deterministic, not modelled.** Which sources apply to a request is boolean set membership over a signed registry — no scoring, no embedding, no model in the decision path. Corpus content cannot influence *what gets selected*, only what gets returned as evidence. That property is structural, not a policy someone remembers to enforce.

**Evidence is never instruction.** The contract separates the two, and the separation is a normative requirement, not a convention. Retrieved text is disclosed as untrusted evidence with its provenance attached.

**Abstention is a real outcome.** No approved policy pack for a domain means Cerebro says so, rather than answering from whatever happened to be nearby.

**Everything is read-only.** The MCP surface has exactly two tools and neither writes anything. All mutation — indexing, activation, approval — lives in a CLI a human runs.

**Claims carry state.** Evidence is normalized and each claim gets an explicit state: supported, stale, expired, unknown provenance. "I found something" and "I found something current and authoritative" are different answers.

## Why not just use skills?

Native agent skills are good at what they do, and Cerebro does not replace them. They solve *give the agent a capability*. Cerebro solves *give the agent access to a large body of knowledge it must reason about carefully* — where citation rules, expiry, jurisdiction, conflicting sources, and "I don't know" all matter, and where the corpus is big enough that dumping it into context is neither affordable nor safe.

If you have a handful of instructions you trust completely, use skills. If you have hundreds of documents of varying authority and age, and it matters whether the agent can tell them apart, that is the problem Cerebro is built for.

## The two tools

| Tool | Purpose |
|---|---|
| `investigate_work` | Classify a task, discover applicable sources, return bounded evidence refs with provenance and claim state, or abstain |
| `read_evidence` | Read an exact, bounded region behind a ref returned by `investigate_work` |

The surface is deliberately fixed at two tools. Adding a third requires passing an explicit gate in the specification.

## Status — read this before installing

Honest state, current as of 2026-07-25.

**Working and tested**
- Hybrid retrieval (BM25 + local vector search via `sqlite-vec`) over a local corpus
- The two-tool MCP contract, with structured output and typed failures
- Signed domain-policy packs with Ed25519 release manifests and fail-closed loading
- Deterministic source discovery, domain-gated, with explicit abstention
- Atomic index build / promote / rollback with retained generations
- 859 tests

- The **skills layer**, end to end. `investigate_work` returns skill refs with provenance and a declared permission envelope; the full lifecycle — `skill-ingest`, `skill-analyze`, `skill-approve`, `skill-sign`, `skill-activate`, `skill-rollback`, `skill-status`, `skill-prune` — runs from the terminal.
- **Six first-party skills ship and are active on install**: running falsifiability probes, verifying claims before asserting them, making invalid states inexpressible, refactoring without touching a test file, sweeping the clock forward to find time bombs, and reading your own interface as a stranger. They are signed with the release key, they grant nothing, and each states its own limits. Every one was used to build this project and found real defects while doing it — two expiry time bombs, a security hole in a control-character check, and a complete feature that was unreachable in production.

**Known limits, stated rather than buried**
- Six bundled skills is a starting point, not a library. The mechanism is finished; the content is deliberately small, and whether it grows well is the open question.
- The dispatch ceiling is five refs, and six skills ship, so one is reported as a gap on every programming request. The ceiling was NOT raised to fit the pack: measured, six refs cost about 6.6 KB, a third of the default output budget. The cut is alphabetical by `skill_id` — deterministic and non-injectable by design, but unrelated to relevance. Making the ceiling operator-configurable is the obvious next step and is not done.
- Skill dispatch is gated on the client sending `host_skills`. A client that does not opt in gets a byte-identical response to the pre-skills contract, which is intentional but does mean the layer is invisible until a client asks for it.
- Only macOS/POSIX is validated. `index.py` uses POSIX-only primitives and will not import on Windows.
- Live web research is present in the codebase but **deliberately inert**: it needs an operator-defined destination allowlist that does not ship by default. The absence is a safety posture, not an oversight.
- Retrieval quality is measured against a 61-query evaluation set, not against a public benchmark.
- Embeddings are local (`fastembed`, ONNX). There is no generative model anywhere in this project — Cerebro retrieves, classifies, and discloses. It does not write prose.

## Trust model

Cerebro assumes the corpus may be hostile.

- **Provenance is attribution, not safety.** A valid signature establishes *who* published a pack. It is never evidence that the content is correct or safe, and the code says so where it verifies signatures.
- **Default-deny by absence.** Permission envelopes default to empty collections, and "allow everything" is not expressible — no wildcard exists in the host or path grammars, so a broad grant cannot be written even by a correctly signed pack.
- **Human approval is bound to a content digest.** Editing approved content invalidates the approval rather than silently carrying it forward.
- **Natural-language analysis is not a safety guarantee** and is never presented as one. Whether prose persuades an agent to do something harmful is not observable by inspection. The real mitigations are the permission envelope and the host's own enforcement.

## Install

Requires **Python 3.12** on macOS or Linux. Not 3.13 — the project pins `>=3.12,<3.13`, because the embedding runtime is validated against exactly that version.

```bash
uv sync

# Write private config and print client snippets. Creates nothing else.
uv run cerebro-mcp init

# Build and promote an index over your corpus. Both flags are required:
# --corpus-root is the directory to index, --policy is the include/exclude ruleset.
uv run cerebro-mcp index --corpus-root ./notes --policy ./policy.yaml

# Read-only health check. Safe at any time; never mutates anything.
uv run cerebro-mcp doctor

# Run the two-tool MCP server over stdio.
uv run cerebro-mcp serve
```

A minimal `policy.yaml`:

```yaml
version: 1
include: ['**']
exclude: ['private/**']
```

Only Markdown files are ever considered, so `include` selects *which* of them to take rather than which file types. Note the sharp edge: `'**/*.md'` matches only files inside a subdirectory and silently skips everything at the top level — use `'**'` unless you specifically want to exclude the root.

Nothing is indexed that the policy does not explicitly include, and no directory is created that you did not ask for.

## Design notes

The `openspec/` directory carries the specifications, architecture decisions, and per-unit implementation records — including what was rejected and why. If you want to understand a decision rather than just read the code, start there.

## Licence

Apache License 2.0. See `LICENSE` and `NOTICE`.

Chosen over MIT for the explicit patent grant: a project whose entire argument is about trust
boundaries should not leave a patent question open. Chosen over AGPL because the goal is adoption —
a copyleft that reaches across a network would keep exactly the users this is built for from trying
it.
