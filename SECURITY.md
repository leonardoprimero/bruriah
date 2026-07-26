# Security

## Reporting a vulnerability

Use GitHub's [private vulnerability reporting](https://github.com/leonardoprimero/bruriah/security/advisories/new).
Please do not open a public issue for something exploitable.

I maintain this alone, so I will not promise a response time I cannot keep. I will acknowledge that
I read your report, and if I cannot fix something I will say so rather than leave it open forever.

## What Bruriah claims

The threat model assumes **your corpus may be hostile**. Someone who can write one file into an
indexed directory should not thereby control your agent.

- **Selection is deterministic and never reads corpus prose.** Which sources and skills apply is
  boolean set membership over a signed registry. No model, no scoring, no embedding sits in that
  decision. Corpus content cannot influence *what gets selected*, only what comes back as evidence.
- **`investigate_work` returns references, never corpus prose.** Locators, digests, provenance and
  an explicit `authority: unknown` / `not_assessed_by_retrieval`. There is nothing in that response
  to obey. Reproduce it: `uv run python demo/injection/run.py`.
- **Permission envelopes are default-deny, and "allow everything" is inexpressible.** No wildcard
  exists in the host or path grammars, so a broad grant cannot be written even by a correctly
  signed pack.
- **Human approval is bound to a content digest.** Edit approved content and the approval stops
  matching; activation fails rather than carrying it forward.
- **The MCP surface is two read-only tools.** Neither writes anything. All mutation — indexing,
  approval, activation, rollback — lives in a CLI a human runs.
- **Signatures establish authorship, not safety.** A valid Ed25519 signature says *who* published a
  pack. It is never evidence the content is correct or safe, and the code says so at the point
  where it verifies signatures.
- **Nothing leaves your machine.** No telemetry, no API keys, no network by default. Live research
  exists but is inert without an operator-defined allowlist that does not ship.

## What Bruriah does not claim

Stated plainly, because a security claim that quietly overreaches is worse than none.

- **It does not prevent prompt injection.** `read_evidence` returns the exact bytes you asked for,
  including hostile ones. Bruriah does not sanitise them and cannot stop a host that pastes them
  into a prompt. What it removes is the *automatic* path from "a document was retrieved" to "its
  prose is in the model's context" during selection.
- **It is not a sandbox.** It does not enforce your agent's filesystem or network permissions. An
  envelope is a **disclosure** of what a skill declares, not an enforcement of it. The host enforces.
- **Natural-language analysis is not a safety verdict** and is never presented as one. Whether
  prose persuades a model to do something harmful is not observable by inspection.
- **It does not verify that a skill body matches its digest.** Bruriah never reads skill bodies —
  that is the host's job, and the digest is what lets the host check.
- **It is not a legal, medical, financial or professional-security authority.** It abstains in
  domains it has no approved policy for, which is the honest behaviour, not a substitute for one.

## Supported versions

Only the latest release. This is 0.1.0 and there is no backport branch.

## Platform

macOS and Linux. Windows is unsupported and refuses to import rather than running with a weaker
guarantee: activation swaps a pointer under `flock` with `O_NOFOLLOW` and re-confirms the file's
identity afterwards, and a half-ported version of that would fail silently instead of loudly.
