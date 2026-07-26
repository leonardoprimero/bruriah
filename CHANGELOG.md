# Changelog

Notable changes, newest first. This project follows [semantic versioning](https://semver.org/),
and the entries here name what changed for *you* rather than which files moved.

## [Unreleased]

Everything here is on `main` and **not** in the published `0.1.0`. If you installed from PyPI, you
do not have it yet — including `bruriah ask`, which the README documents.

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

### Added earlier on `main`, still unreleased

- `bruriah ask` — query from the terminal, with `--read N` to pull the exact lines, before wiring
  up any MCP client.

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

[Unreleased]: https://github.com/leonardoprimero/bruriah/compare/main...HEAD
[0.1.0]: https://github.com/leonardoprimero/bruriah/releases/tag/v0.1.0
