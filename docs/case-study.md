# Real-World Case Study: Preventing Architectural Regressions in Coding Agents

This case study demonstrates how Bruriah prevents coding agents from reintroducing previously evaluated and discarded architectures.

---

## The Scenario

- **Codebase:** Mature multi-contributor repository (>3,000 commits).
- **Subsystem:** Model Context Protocol (MCP) server interface (`src/bruriah/mcp_server.py`).
- **The Actor:** An autonomous coding agent (Claude Code, Cursor, or Antigravity) tasked with modernizing and refactoring the server code.
- **The Task:** *"Refactor `mcp_server.py` to use FastMCP for cleaner decorator-based routing."*

---

## 1. What Happens Without Bruriah (The Standard Failure Mode)

1. The coding agent reads `src/bruriah/mcp_server.py`.
2. It sees raw `mcp.server.lowlevel.Server` handlers with explicit payload parsing.
3. The LLM's pretraining heavily associates MCP with `FastMCP`, which looks cleaner, reduces boilerplate, and is widely promoted in tutorials.
4. **The Regression:** The agent rewrites the server using `FastMCP`.
5. **The Invisible Bug:** In production, unknown input fields in JSON-RPC payloads are silently dropped because FastMCP disables `extra="forbid"`. Client requests with malformed or unvalidated fields pass through uninspected, bypassing server-side validation.

The agent didn't make a syntax error or a logic bug; it lacked **causal memory**. Nothing in the working tree told it that FastMCP had already been evaluated, tested, and rejected 18 months earlier.

---

## 2. What Happens With Bruriah

### Step 1: Pre-Flight Investigation

Before modifying `src/bruriah/mcp_server.py`, the agent calls `investigate_work` via the MCP protocol:

```jsonc
agent → investigate_work({
  "task": "refactor mcp server to use FastMCP",
  "code_target": "src/bruriah/mcp_server.py:42"
})
```

Alternatively, a human developer or pre-commit hook runs:

```bash
bruriah why src/bruriah/mcp_server.py:42
```

### Step 2: Causal Retrieval (Evidence, Not Prose)

Bruriah queries the SQLite lineage DAG and commit index. It discovers the governing architectural decision:

```text
Governing Architectural Decision:
  Decision: Migrate from FastMCP to lowlevel server
  Commit:   e8f3003bda26 (2026-07-23)
  Author:   Leonardo Caliva
  Files:    src/bruriah/mcp_server.py

  Why this was written:
  FastMCP derives its argument model without extra="forbid", so an unknown
  field is silently dropped before any handler runs — defeating authoritative
  server-side validation.
```

Through MCP, Bruriah returns a reference:

```jsonc
bruriah ← {
  "status": "complete",
  "evidence": [
    {
      "ref": "chunk:v1:6d4329329f9ab6ea67e3d34ec31da3567a07b51041f0787c800d6b1bd73fb1c4",
      "kind": "local",
      "publisher": "2026-07-23-e8f3003b-feat-cerebro-router-add-the-two-tool-mcp-protocol-server.md",
      "citation_locator": "2026-07-23-e8f3003b-feat-cerebro-router-add-the-two-tool-mcp-protocol-server.md#1-28",
      "digest": "sha256:5bfcda316ae7f376c75729c07c7f90d2d39af10b8072be11067cc791a29b290d",
      "authority": "primary",
      "authority_rationale": "Governing architectural decision for src/bruriah/mcp_server.py:42"
    }
  ]
}
```

### Step 3: Explicit Evidence Verification

The agent decides to inspect the evidence by calling `read_evidence`:

```jsonc
agent → read_evidence({
  "refs": ["chunk:v1:6d4329329f9ab6ea67e3d34ec31da3567a07b51041f0787c800d6b1bd73fb1c4"]
})
```

Bruriah returns the exact historical reasoning:

```
# feat(cerebro-router): add the two-tool MCP protocol server

Decided: 2026-07-23 · Commit: e8f3003bda26 · Author: Leonardo Caliva

built on mcp.server.lowlevel.Server, not FastMCP: FastMCP derives its argument
model without extra="forbid", so an unknown field is silently dropped before
any handler runs — defeating authoritative server-side validation. Proven
empirically against a real FastMCP counter-example during review.
```

### Step 4: The Outcome

The agent replies to the developer:

> *"I evaluated refactoring `mcp_server.py` to FastMCP. However, historical commit `e8f3003bda26` explicitly established that FastMCP silently drops unknown fields by omitting `extra='forbid'`, defeating our schema validation contract.*  
> *Instead of migrating to FastMCP, I will retain the low-level server architecture and refactor internal helper functions to improve readability without breaking validation guarantees."*

---

## 3. Why This Changes Team Velocity

| Without Bruriah | With Bruriah |
|---|---|
| Agents repeatedly propose rejected patterns. | Agents cite the specific commit that rejected the pattern. |
| Senior engineers spend PR review time re-explaining past decisions. | PR review bots (`bruriah review`) and pre-flight checks (`bruriah brief`) enforce invariants automatically. |
| Invariants only exist in the heads of developers who were there 2 years ago. | Invariants are queried directly from Git history and line-level causal DAGs. |
| Codebases suffer gradual architectural erosion ("drift"). | Architectural drift is caught in pre-commit and CI with zero extra infrastructure. |
