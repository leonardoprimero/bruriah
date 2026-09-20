# Bruriah CLI & Architectural Governance Tools

This guide covers Bruriah's command-line tools, architectural governance workflows, editor extensions, and developer integrations.

---

## 1. Decision Lineage: Supersedes, Deprecates, Amends

When an architecture decision replaces, deprecates, or amends an earlier one, declare it in the Git commit message using standard Git trailers:

```gitcommit
feat(server): migrate from FastMCP to lowlevel server

Because FastMCP disables extra="forbid" schema validation.

Supersedes: e8f3003bda26
Deprecates: deadbeef1234
Amends: feedface5678
```

Or in Markdown document YAML frontmatter:

```yaml
---
commit: f6e5d4c3b2a1
supersedes:
  - e8f3003bda26
deprecates:
  - deadbeef0000
amends:
  - feedface5678
---
```

When `bruriah corpus` parses your repository, it extracts these trailers into document metadata. `bruriah index` records directed edges in a SQLite lineage DAG and verifies acyclicity via DFS (aborting with `IndexLifecycleError("lineage_cycle_detected")` if a loop is found).

At query time, `investigate_work`, `bruriah ask`, and `bruriah why` resolve superseded decisions transitively: when an architectural decision has evolved across multiple generations (e.g. A superseded by B, and B subsequently superseded by C), Bruriah traces the full DAG chain to locate the current active leaf decision. Older evidence is marked `stale` with `conflict: declared`, active successors are marked `current` with `conflict: none`, and explicit `ClaimRecord` entries with `state: "conflicted"` are emitted.

---

## 2. Causal Archaeology: `bruriah why`

`git blame` tells you who touched a line and when. It cannot tell you the architectural reasoning that governed it, or whether that reasoning was later superseded.

`bruriah why <file>[:line]` performs **causal archaeology** across your codebase:
1. Resolves the commit touching the line or file.
2. Traverses git history to link the change to the governing architectural decision indexed in Bruriah.
3. Queries the decision lineage DAG to alert you immediately if the governing decision was superseded, deprecated, or amended — tracing multi-hop successor chains to find the active leaf.

```bash
bruriah why src/bruriah/mcp_server.py:42 --data-dir "$B/data"
```

Output:

```text
Target: src/bruriah/mcp_server.py:42

Line Commit:
  Commit:  a1b2c3d4 (2026-07-25)
  Author:  Jane Doe
  Subject: style(mcp): reformat server init

Governing Architectural Decision:
  Decision: Migrate from FastMCP to lowlevel server
  Doc Ref:  doc-server-decision
  Commit:   e8f3003bda26 (2026-07-23)
  Decided:  2026-07-23 by Lead Engineer
  Files:    src/bruriah/mcp_server.py, src/bruriah/service.py

  Why this was written:
  FastMCP derives its argument model without extra="forbid", so an unknown
  field is silently dropped before any handler runs — defeating authoritative
  server-side validation.

Lineage Alerts:
  ⚠️  SUPERSEDED by doc-server-v2 (sha: f6e5d4c3b2a1)
     "Modernized Async MCP Protocol"
     ↳ subsequently evolved through 2 generations to [CURRENT ACTIVE]: doc-server-v3 (sha: 0123456789ab)
       "Cloud-Native Distributed MCP Architecture"
```

Pass `--json` to integrate causal archaeology directly into editor hover providers (VS Code, Neovim), CLI pipelines, or PR review bots.

---

## 3. In-Editor Inline Archaeology (VS Code, Cursor & Neovim)

Causal archaeology belongs where developers think and write code — not just in the terminal.

### VS Code & Cursor Extension (`editors/vscode`)

Provides native **CodeLens** and **Hover** annotations directly above your code:

```text
│ 🏛️ Decisión: Migración a Async MCP (e8f3003b) · ⚠️ SUPERSEDED por f6e5d4c3 · [Ver por qué se decidió]
```

- **CodeLens**: Displays the governing architectural decision, commit SHA, and drift alerts above functions and code blocks.
- **Hover**: Hover over any line to inspect the author, date, drift status, and original reasoning text.
- **Side Panel Webview**: Click `[Ver por qué se decidió]` to open a split panel with full decision notes, context, and multi-generation DAG lineage.
- **Commands**:
  - `Bruriah: Explain Why This Line Exists` (`bruriah.whyLine`)
  - `Bruriah: Open Visual DAG Explorer` (`bruriah.openUI`)

### Neovim Plugin (`editors/neovim`)

Native Lua plugin providing virtual text and floating windows:

- **Virtual Text**: Line-by-line annotations showing governing decisions in comments (`Comment`) or drift alerts (`DiagnosticWarn`).
- **Commands**:
  - `:BruriahWhy`: Opens a floating window with syntax-highlighted decision reasoning.
  - `:BruriahLens`: Refreshes inline virtual text for the current buffer.
  - `:BruriahUI`: Launches the Visual DAG Explorer in the background.

### Bulk File Archaeology: `bruriah lens`

To keep editor extensions instantaneous (<50ms for thousands of lines), `bruriah lens` performs bulk archaeology in a single fast pass:

```bash
bruriah lens src/bruriah/cli.py           # Human-readable line ranges
bruriah lens src/bruriah/cli.py --json    # Structured JSON for editor plugins
```

---

## 4. Visual DAG Explorer: `bruriah ui`

Visualize your project's entire architectural decision graph in an interactive, local web dashboard:

```bash
bruriah ui
bruriah ui --port 8080 --no-browser
```

- **Force-Directed Lineage Graph**: Powered by embedded D3.js and Python's stdlib `http.server` (zero extra dependencies).
- **Status Color-Coding**: Active (🟢 green), Superseded (🔴 red), Deprecated (🟡 yellow), and Amended (🔵 blue).
- **Interactive Detail Panel**: Click any decision node to view its source file, commit metadata, touched files, and reasoning excerpt.
- **Connected-Node Highlighting**: Focus on a decision to highlight its upstream and downstream lineage chain.
- **Real-Time Search**: Instant filtering across decision titles, touched files, and authors.
- **Timeline Scrubber**: Interactive slider to inspect how the architecture evolved over time.
- **REST API**: `GET /api/dag` for programmatic access to the graph data.

---

## 5. PR Review Bot: `bruriah review` & GitHub Actions

Turn Bruriah into a silent, senior architect reviewing your pull requests:

```bash
bruriah review origin/main...HEAD
bruriah review origin/main...HEAD --json
bruriah review origin/main...HEAD --post --strict
```

- **Surgical Inline Comments**: Posts comments directly on the exact lines touching code governed by superseded or deprecated decisions.
- **Multi-Generation Tracing**: Explains how many generations of decisions the code skipped and points to the current active decision.
- **Review Summary**: Generates a GitHub PR review summary with a clean metrics breakdown (inspected files, drift warnings, clean governance, unindexed files).
- **GitHub Actions Integration**:
  Add Bruriah to `.github/workflows/bruriah.yml`:

```yaml
name: Architectural Review

on:
  pull_request:
    branches: [ main ]

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Bruriah Architectural Review
        uses: leonardoprimero/bruriah@main
        with:
          command: review
          strict: false
          github-token: ${{ secrets.GITHUB_TOKEN }}
```

When drift is detected, Bruriah posts an inline review comment explaining the architectural deviation and recommending corrective actions. In `--strict` mode, it requests changes (`REQUEST_CHANGES`) to block merges until architectural alignment is restored.

---

## 6. Cold-Start Bootstrap: `bruriah bootstrap`

Adopting an architectural decision tool on an existing codebase with years of history shouldn't require writing dozens of ADRs by hand.

`bruriah bootstrap` mines your repository's existing Git history to automatically identify architectural turning points, extract them into structured markdown decisions, infer `supersedes` relations, and build an initial index snapshot in seconds:

```bash
bruriah bootstrap                       # Extract decisions to ./decisions
bruriah bootstrap --dry-run             # Preview candidates without writing files
bruriah bootstrap --limit 30            # Extract top 30 architectural decisions
bruriah bootstrap --min-score 0.6       # Filter by architectural relevance score
bruriah bootstrap --index               # Extract AND build the initial Bruriah snapshot
```

### How Git Mining Works:
1. **Architectural Scoring**: Evaluates each non-merge commit using heuristics (explanatory body length, keywords like *refactor*, *breaking change*, *architecture*, *redesign*, *migrate*, and cross-cutting file touches) while filtering out routine chores and version bumps.
2. **Lineage Inference**: Detects when subsequent refactoring commits touch the same architectural domain and automatically infers `supersedes` trailers.
3. **Instant Onboarding**: Automatically writes standard Markdown decisions into `decisions/` and optionally builds the initial index, making `bruriah ask`, `bruriah why`, `bruriah ui`, and editor extensions work immediately on day 1.

---

## 7. Architectural Blast Radius: `bruriah impact`

Architectural drift is easiest to prevent *before* touching code. `bruriah impact` performs pre-flight blast radius analysis on any file, directory, or revision range:

```bash
bruriah impact src/bruriah/auth.py              # Single file blast radius
bruriah impact src/bruriah/core/                # Directory / module blast radius
bruriah impact HEAD~1..HEAD                     # Revision range blast radius
bruriah impact src/bruriah/auth.py --json       # Structured JSON for CI or pre-commit hooks
```

Output:

```text
🏛️  Bruriah Architectural Blast Radius — src/bruriah/auth.py (file)
   Risk Level: MEDIUM · 1 governing decision(s) · 2 co-governed file(s) at risk

Governing Decisions & Blast Radius:
  • [✅ ACTIVE] Unified Token and Session Model (22222222)
    Author: Architect · Date: 2026-02-01
    Directly governs: src/bruriah/auth.py
    Co-governed files (Blast Radius):
      ⚠️  src/bruriah/tokens.py
      ⚠️  src/bruriah/session.py

Recommendations:
  1. Blast Radius Warning: Modifying this target affects 2 co-governed file(s). Changes to the architectural contract will cause drift in these files.
  2. Recommendation: If changing architectural contracts, declare 'Supersedes: <commit>' in your commit and update the co-governed files in the same pull request.
```

- **Pre-Flight Risk Assessment**: Classifies risk (`LOW`, `MEDIUM`, `HIGH`, `CRITICAL`) based on the number of co-governed files and whether the governing decision is already superseded.
- **Co-Governance Mapping**: Discovers all other files that share the same architectural decision so you can update them together in the same PR.
- **Actionable Recommendations**: Tells you exactly which trailers to declare to maintain lineage integrity.

---

## 8. Architectural Pre-Flight & Supersede Protocol: `bruriah brief`

Before writing a single line of code, developers and AI agents need to know: *what architectural invariants and historical decisions govern this task?* 

`bruriah brief` provides proactive pre-flight dossiers. Instead of catching broken rules after hundreds of lines are written, it synthesizes active invariants, blast-radius risks, and introduces the formal **Supersede Protocol**:

```bash
bruriah brief "migrate auth to OAuth2" --targets src/auth.py  # Terminal pre-flight dossier
bruriah brief "migrate auth to OAuth2" --agent               # Context prompt for AI agent injection
bruriah brief "migrate auth to OAuth2" --json                # Structured JSON for pipelines
```

Terminal Output:

```text
🏛️  Bruriah Architectural Brief — Pre-flight Dossier
   Intent: "migrate auth to OAuth2"
   Risk Level: 🟢 LOW · 1 target(s) · 1 constraint(s)

Target Files:
  • src/auth.py

Active Architectural Invariants:
  • Unified Token and Session Model (22222222) — Leonardo Caliva, 2026-07-23
    ↳ Maintain alignment with 'Unified Token and Session Model' (22222222).
    ↳ Must never store plain tokens in cookies.

Blast Radius (Co-governed files):
  • src/tokens.py
  • src/session.py

⚠️  Supersede Protocol:
   If historical premises have changed and an invariant must be updated:
   Do NOT violate it silently. Declare a Supersede Proposal:
   - Target: Unified Token and Session Model (22222222)
   - Changed Premise: <why the past premise no longer applies>
   - Proposed Invariant: <new replacement rule>
   - Rationale: <technical justification>
```

### The Supersede Protocol
Decisions in Bruriah are not dogmas; they are solutions chosen under specific premises. When a library update, runtime upgrade, or new requirement invalidates an old decision, **the agent or developer does not silently break it**. The protocol instructs the agent to explicitly propose an architectural supersede, detailing what premise changed and why, keeping the human lead firmly in control.

---

## 9. Architectural Decision Scribe: `bruriah decide`

A project's memory is only as good as the reasoning recorded in its commits. If engineers or AI agents commit with superficial messages like *"update auth"*, the lineage graph degrades. 

`bruriah decide` captures architectural choices at the moment of creation, formalizing the problem, alternatives evaluated, technical tradeoffs, established invariants, and automatically validating lineage trailers against the SQLite DAG:

```bash
bruriah decide                                      # Interactive terminal interview
bruriah decide --title "feat(auth): adopt OAuth2" \
               --problem "Session cookies vulnerable to CSRF across subdomains" \
               --solution "Migrate to OAuth2 authorization code with PKCE" \
               --invariants "Tokens must not exceed 15m TTL" \
               --alternative "JWT in localStorage:Zero server state:Vulnerable to XSS" \
               --supersedes e8f3003bda26 \
               --commit                             # Directly commit staged changes
```

### Key Capabilities:
1. **Interactive Senior Architect Interview**: When run without arguments in a terminal, prompts the developer through the core architectural dimensions (Context, Solution, Invariants, Lineage).
2. **Validated Predecessor Trailers**: Validates `--supersedes`, `--amends`, and `--deprecates` against the local SQLite index snapshot to ensure the referenced decision exists.
3. **Commit & ADR Publishing**: Emits standard Git commit messages with valid Git trailers or publishes ADR markdown (`--adr`).

---

## 10. Architectural Guard & Compliance Receipts: `bruriah guard`

Whether code is written by human engineers or AI agents (Claude Code, Cursor, Gentle-AI, Antigravity), `bruriah guard` acts as the authoritative gatekeeper enforcing architectural governance:

```bash
bruriah guard src/bruriah/auth.py               # Inspect compliance of target
bruriah guard origin/main...HEAD --strict       # Block on any drift (exit 1)
bruriah guard src/bruriah/auth.py --agent       # Emit prompt context for AI agents
bruriah guard origin/main...HEAD --receipt      # Generate cryptographic compliance receipt (RDD)
bruriah guard origin/main...HEAD --receipt --engram  # Optionally sync receipt to .engram/ memory
```

Output:

```text
🏛️  Bruriah Architectural Guard — src/bruriah/auth.py
   Status: ✅ PASSED · 1 file(s) · 1 contract(s) · 0 violation(s)

Active Architectural Contracts:
  • Unified Token and Session Model (22222222)
    ↳ Maintain alignment with decision 'Unified Token and Session Model' (22222222).
    ↳ Co-governs 2 other file(s): src/bruriah/tokens.py, src/bruriah/session.py.

Compliance Receipt (RDD):
  • Status: COMPLIANT
  • Digest: 3a7f89b1c0d4e5f6...
```

### Key Capabilities:
1. **AI Agent Context Injection (`--agent`)**: Outputs a clean, structured Markdown prompt listing active directives and prohibitions for the files under modification, grounding coding agents in architectural truth *before* they write code.
2. **Deterministic Compliance Receipts (RDD)**: Emits a verifiable, SHA-256 hashed `receipt` linking inspected files, governing decision SHAs, and timestamp to prove that changes comply with project architecture.
3. **Optional Engram Bridge (`--engram`)**: When explicitly passed, syncs the compliance receipt into `.engram/compliance-receipt.json` for teams using Gentle-AI/Engram workflows (disabled by default; 100% standalone otherwise).

---

## 11. Architectural Auto-Healing & Remediation: `bruriah heal`

When an architectural violation or drift occurs, blocking the build (`guard --strict`) is only half the battle. If developers or AI agents do not understand *how* to solve the violation properly, agents hallucinate ad-hoc hacks and developers get frustrated.

`bruriah heal` analyzes violations against historical decision documents, extracts the canonical implementation pattern, and synthesizes step-by-step refactoring recipes:

```bash
bruriah heal src/bruriah/auth.py              # Human-readable remediation guide
bruriah heal src/bruriah/auth.py --agent      # Pedagogical prompt injection for AI agents
bruriah heal origin/main...HEAD --json        # Structured JSON for automated CI/CD
```

### Key Capabilities:
1. **Pedagogical Agent Prompting (`--agent`)**: Instead of a dry failure message, injects the exact canonical pattern and step-by-step recipe into the LLM context so the agent refactors the code cleanly on its next turn.
2. **Canonical Pattern Synthesis**: Extracts the authoritative design pattern directly from the commit that established the rule, ensuring consistency across generations of developers.
3. **Graceful Verification**: Generates sequential steps that culminate in running `bruriah guard` to confirm that compliance is restored.

---

## 12. Causal Archaeology for Coding Agents (MCP)

Coding agents (Claude Code, Cursor, Antigravity) can perform causal archaeology directly through the MCP protocol without breaking the Two-Tool Public Contract, using the optional `code_target` parameter in `investigate_work`:

```jsonc
// 1. Agent investigates a specific line of code before refactoring
agent → investigate_work({
  "task": "refactor mcp protocol handler",
  "code_target": "src/bruriah/mcp_server.py:42"
})

// 2. Bruriah returns the governing architectural decision as primary evidence with DAG alerts
bruriah ← {
  "status": "complete",
  "evidence": [
    {
      "ref": "chunk:v1:6d43293...",
      "kind": "local",
      "authority": "primary",
      "authority_rationale": "Governing architectural decision for src/bruriah/mcp_server.py:42...",
      "freshness": "stale",
      "conflict": "declared"
    }
  ],
  "conflicts": [
    "Decision in 2026-07-23-e8f3003b.md governing src/bruriah/mcp_server.py:42 has been superseded by..."
  ],
  "claims": [
    {
      "text": "Governing decision e8f3003b for src/bruriah/mcp_server.py:42 is supersedes",
      "state": "conflicted"
    }
  ]
}

// 3. Agent reads the exact unmodified reasoning bytes before touching the code
agent → read_evidence({"refs": ["chunk:v1:6d43293..."]})
```

---

## 13. Deriving a Corpus from PDFs (`bruriah corpus --pdf`)

Bruriah indexes Markdown. Organizations with design documents, architecture RFCs, academic papers, and client standards frequently ask for PDF indexing.

Pretending a PDF has lines breaks Bruriah's locator guarantee: a cited line would exist only in extractor memory rather than in the original file on disk. Parsing PDFs at query time would destroy byte-for-byte citation integrity.

Instead, PDF ingestion is an auditable derivation step, exactly like git history derivation:

```bash
# Extract Markdown documents from a PDF or directory of PDFs (requires pip install 'bruriah[pdf]')
bruriah corpus --pdf ./docs/standards/ --out "$B/corpus"
bruriah index  --data-dir "$B/data" --corpus-root "$B/corpus" --policy "$B/policy.yaml"
```

What this achieves:
1. **Zero Indexer Modifications:** Not one line changes in the indexer or retriever. It sees inspectable Markdown files on disk.
2. **Byte-for-Byte Locator Contract:** Citation locators point to lines in physical derived Markdown files (`corpus/<stem>-p014.md`), which `read_evidence` returns byte-for-byte.
3. **Auditable Provenance:** Every derived document has YAML frontmatter naming `source`, `page`, `extractor` (versioned), `source_sha256`, and `status: active`.
4. **Honest Coverage:** Text-layer pages are extracted; blank separator pages and unextractable image scans are skipped and reported honestly in examination metrics.

---

## 14. Architectural Drift Detection: `bruriah drift`

Codebases naturally drift away from architectural intent. Developers or autonomous coding agents touch files governed by architectural decisions without realizing those decisions were superseded, or introduce regressions against active lineage constraints.

`bruriah drift [REVISION_OR_RANGE]` analyzes git diffs against Bruriah's decision lineage DAG to detect architectural drift before it is merged:

```bash
# Check working tree (staged and unstaged changes)
bruriah drift --data-dir "$B/data"

# Check only staged changes before committing (pre-commit hook)
bruriah drift --staged --data-dir "$B/data"

# Check a commit range or pull request branch in CI
bruriah drift origin/main..HEAD --strict --data-dir "$B/data"
```

Output:

```text
Inspecting 3 changed file(s)...

Found 2 architectural drift issue(s):

⚠️  DRIFT: src/bruriah/mcp_server.py
   Modified lines are governed by an outdated decision:
   Decision: Migrate from FastMCP to lowlevel server (e8f3003b)
   Status:   SUPERSEDED by f6e5d4c3b2a1
             ↳ subsequently evolved through 2 generations to [CURRENT ACTIVE]: 0123456789ab
   Action:   Review active successor decision before modifying this component.

ℹ️  GOVERNED: src/bruriah/service.py
   Governed by active decision: Extracted SnapshotRepository (38f9304a)
   Status:   CURRENT (no conflicts declared)

Exit status: 1 (drift detected in strict mode)
```

### Enforcing Architectural Governance

#### 1. Official GitHub Action
Add Bruriah as a zero-setup pull request check in GitHub Actions:

```yaml
- name: Check Architectural Drift
  uses: leonardoprimero/bruriah@main
  with:
    strict: true
```

#### 2. Pre-commit Framework
Add Bruriah to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/leonardoprimero/bruriah
    rev: v1.0.0
    hooks:
      - id: bruriah-drift
```

#### 3. Native Git Hook
If you don't use the Python pre-commit framework, install the native Git hook directly:

```bash
bruriah hook install        # installs pre-commit hook into .git/hooks/pre-commit
bruriah hook uninstall      # cleanly removes the hook
```

#### 4. Native Git Aliases (`git why` & `git drift`)
Make causal archaeology and drift detection feel like native Git subcommands:

```bash
bruriah alias install       # registers 'git why' and 'git drift' globally (or --local)
bruriah alias uninstall     # cleanly unregisters aliases

# Now use them anywhere in your workflow:
git why src/bruriah/mcp_server.py:42
git drift --staged
```

Pass `--json` to `bruriah drift` to integrate structured diagnostics into PR review bots or dashboards.
