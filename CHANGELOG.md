# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
and the entries here name what changed for *you* rather than which files moved.

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
  several frames deep on a missing `fcntl`.
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

[0.1.0]: https://github.com/leonardoprimero/bruriah/releases/tag/v0.1.0
