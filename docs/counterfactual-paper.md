# Counterfactual Architectural Memory: Preventing Architectural Amnesia in Autonomous Coding Agents

**Author:** Leonardo Primero  
**Project:** Bruriah (Evidence-Backed Project Memory for Coding Agents)  
**Status:** Technical Report & Conference Whitepaper  
**Reproducibility:** `evals/counterfactual/runner.py` (Deterministic Correctness Suite)

---

## Abstract

Autonomous coding agents increasingly perform multi-file refactorings, dependency updates, and architectural redesigns. However, contemporary agent memory architectures—predominantly naive Retrieval-Augmented Generation (RAG) over source code, or conversational memory buffers—suffer from **Architectural Amnesia**: they observe what code currently exists, but cannot retrieve *why* specific alternative architectures were previously evaluated and rejected, nor can they determine whether the empirical *premises* that justified those rejections remain valid. Consequently, agents systematically reintroduce discarded patterns, resurrect historical bugs, and bloat dependencies.

We present **Counterfactual Architectural Memory & Premise Tracking**, an evidence-backed framework integrated into Git lineage graphs and local SQLite indices. By formalizing architectural rejections and contextual constraints as a premise calculus ($P \implies \text{disposition}(A)$), Bruriah dynamically determines whether a proposed task or code target matches a rejected alternative and verifies the validity of its supporting premises (`active`, `invalidated`, `uncertain`). 

We evaluate this system against a 20-scenario deterministic correctness suite spanning schema integrity, dependency bloat, security, concurrency, and performance. In this fixture suite, Bruriah deterministically identifies rejected architectural alternatives and invalidation states according to its relational rules, with zero generative hallucination and zero expansion of its minimal two-tool Model Context Protocol (MCP) contract.

---

## 1. Introduction: The Amnesia Crisis in Coding Agents

When human software engineers join a mature engineering team, the most dangerous mistakes are rarely syntax or basic logic errors; they are **architectural regressions**:
- Reintroducing a library that was discarded after uncovering a subtle concurrency bug.
- Reverting an in-memory caching layer that caused distributed data divergence.
- Storing session tokens in browser `localStorage` despite a historical vulnerability assessment.

In human teams, senior architects preserve this institutional memory through Architecture Decision Records (ADRs) and design reviews. Autonomous coding agents, however, operate with **Architectural Amnesia**. When an agent is prompted with *"Migrate server to FastMCP for simpler tool definitions"*, standard retrieval searches the codebase:
1. It finds no active usages of `FastMCP`.
2. It assumes `FastMCP` is a viable, greenfield modernization choice.
3. It implements the migration—silently destroying strict schema validation invariants (`extra="forbid"`).

Existing agent memory frameworks (such as Mem0, Letta/MemGPT, and Pinecone-based RAG) attempt to solve context limits by storing more text or compressing conversation turns into vector embeddings. This approach fails to address architectural causality because **what was rejected is usually absent from the active codebase**. Traditional retrieval searches for presence, not counterfactual absence.

---

## 2. The Counterfactual Problem in Agent Memory

Software architecture is inherently **counterfactual**: every concrete architectural decision is an explicit rejection of alternative approaches under specific environmental constraints (premises).

```
                  ┌───────────────────────────────┐
                  │    Decision Time (Commit A)   │
                  │  Alternative X -> REJECTED    │
                  │  Premise P1: "Library lacks   │
                  │               extra=forbid"   │
                  └───────────────┬───────────────┘
                                  │
                                  ▼
                    Premise Invalidation Event?
                                 / \
                                /   \
                        YES    /     \    NO
                              ▼       ▼
               ┌─────────────────────┐ ┌─────────────────────┐
               │ Commit B:           │ │ Premise P1 remains  │
               │ Premise-Invalidated │ │ ACTIVE              │
               └──────────┬──────────┘ └──────────┬──────────┘
                          │                       │
                          ▼                       ▼
               ┌─────────────────────┐ ┌─────────────────────┐
               │ VERDICT:            │ │ VERDICT:            │
               │ Premise changed     │ │ Repeat of rejected  │
               │ Requires reeval     │ │ architecture        │
               │ (Adoption allowed)  │ │ (REGRESSION FLAGGED)│
               └─────────────────────┘ └─────────────────────┘
```

Formally, an architectural choice $C$ over alternative $A$ is parameterized by a set of premises $\{P_1, P_2, \dots, P_n\}$:

$$\bigwedge_{i=1}^{n} \text{Holds}(P_i) \implies \text{Rejected}(A)$$

If an autonomous agent proposes $A$:
1. If $\forall i, \text{Status}(P_i) = \text{active}$, the proposal is a **Repeat of a Rejected Architecture** (a verified regression).
2. If $\exists j, \text{Status}(P_j) = \text{invalidated}$, the historical rejection was valid when decided, but the underlying reason no longer holds. The proposal requires **Premise Reevaluation**, not dogmatic prohibition.
3. If $\{P_i\}$ is empty or unverified, the decision is an **Unassessed Premise** requiring human architectural review.

---

## 3. Formal Domain Models & Git Lineage Graph

Bruriah implements counterfactual tracking without proprietary database engines or complex distributed infrastructure. It extracts institutional memory directly from the developer's source of truth: **Git commit history** and **Markdown ADR frontmatter**.

### 3.1 Git Trailers
Developers and CI pipelines declare decisions using standard Git trailers:
```gitcommit
refactor(mcp): migrate server to strict closed pydantic models

Alternative-Rejected: FastMCP
Rejection-Reason: Drops unknown fields silently without extra="forbid"
Premise: fastmcp-no-forbid | FastMCP derives schemas without extra=forbid
```

When an upstream dependency evolves or environmental constraints change, a subsequent commit invalidates the premise:
```gitcommit
feat(mcp): prepare FastMCP re-evaluation

Premise-Invalidated: fastmcp-no-forbid
```

### 3.2 Relational SQLite Representation
During index generation (`bruriah index`), Bruriah builds a directed lineage graph and stores normalized premise relations:

```sql
CREATE TABLE premises (
    premise_id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    status TEXT NOT NULL,         -- 'active' | 'invalidated' | 'uncertain'
    invalidated_by TEXT,          -- Commit SHA or Document Ref
    rationale TEXT,
    document_ref TEXT NOT NULL,
    FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;

CREATE TABLE alternatives (
    name TEXT NOT NULL,
    disposition TEXT NOT NULL,    -- 'rejected' | 'deferred' | 'superseded'
    reason TEXT NOT NULL,
    premises_json TEXT NOT NULL,  -- JSON array of premise IDs
    document_ref TEXT NOT NULL,
    PRIMARY KEY (name, document_ref),
    FOREIGN KEY(document_ref) REFERENCES documents(document_ref)
) WITHOUT ROWID;
```

---

## 4. System Architecture: Two-Tool Contract Purity

A central design principle of Bruriah is **Contract Purity**:
> *"Never expand the MCP tool surface when domain intelligence can be returned as structured evidence inside existing tools."*

While competitors expose 20 to 50 granular MCP endpoints (e.g. `save_memory`, `search_graph`, `delete_fact`, `tag_concept`), Bruriah exposes strictly **two read-only MCP tools**:
1. `investigate_work(task, code_target)`: Resolves governing decisions, lineage alerts, and counterfactual assessments.
2. `read_evidence(refs, ranges)`: Slices byte-for-byte immutable evidence from local repository snapshots.

When `investigate_work` runs, it executes counterfactual evaluation in sub-millisecond time:
```json
{
  "counterfactual_assessment": {
    "matched_alternative": "FastMCP",
    "decision_ref": "public/2026-07-20-e8f3003b-refactor-mcp.md",
    "verdict": "repeat_of_rejected_architecture",
    "supporting_evidence": [
      "public/2026-07-20-e8f3003b-refactor-mcp.md#1-24"
    ],
    "rationale": "Alternative 'FastMCP' was evaluated and rejected because: Drops unknown fields silently without extra='forbid'. All supporting premises (fastmcp-no-forbid) remain active."
  },
  "conflicts": [
    "Task matches rejected architecture 'FastMCP' under active premises: Drops unknown fields silently without extra='forbid'"
  ],
  "alternatives": [
    {
      "name": "FastMCP",
      "disposition": "rejected",
      "reason": "Drops unknown fields silently without extra='forbid'",
      "premises": ["fastmcp-no-forbid"]
    }
  ],
  "premises": [
    {
      "id": "fastmcp-no-forbid",
      "statement": "FastMCP derives schemas without extra='forbid'",
      "status": "active",
      "invalidated_by": null
    }
  ]
}
```

---

## 5. Evaluation: 20-Scenario Correctness Suite

To verify the deterministic correctness of counterfactual memory, we developed a fixture test suite (`evals/counterfactual/scenarios.jsonl`) comprising 20 architectural regression challenges across five categories:
1. **Schema Integrity**: Enforcing closed contracts and strict serialization.
2. **Dependency Bloat**: Preventing unnecessary third-party daemons or heavy transitive wheels.
3. **Security & Privacy**: Enforcing zero-telemetry, offline sandboxing, and token protection.
4. **Concurrency & Storage**: Multi-process SQLite locks and database durability.
5. **Protocol Standards**: Adherence to MCP JSON-RPC standards over ad-hoc REST/GraphQL.

### 5.1 Correctness Suite Verification

| Scenario ID | Category | Expected Verdict | Bruriah Verdict | Verification |
|---|---|---|---|:---:|
| `fastmcp-no-forbid-active` | Schema Integrity | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `fastmcp-upstream-fixed` | Schema Integrity | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `watchdog-zero-dep-active` | Dependency Bloat | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `watchdog-cross-platform-invalidated` | Dependency Bloat | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `pinecone-vector-db-active` | Privacy & Offline | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `chromadb-embedded-active` | Dependency Bloat | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `generative-llm-summaries-active` | Evidence Integrity | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `jwt-localstorage-active` | Security | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `jwt-localstorage-invalidated` | Security | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `sqlite-in-memory-active` | Concurrency | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `sqlite-in-memory-invalidated` | Concurrency | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `celery-redis-active` | Simplicity | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `graphql-api-active` | Protocol Standard | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `duckdb-analytics-active` | Dependency Bloat | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `duckdb-analytics-invalidated` | Performance | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `pydantic-v1-active` | Typing Soundness | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `fastapi-server-active` | Security | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `fastapi-server-invalidated` | Security | `premise_changed_requires_reevaluation` | `premise_changed_requires_reevaluation` | **PASS** |
| `opentelemetry-collector-active` | Privacy & Offline | `repeat_of_rejected_architecture` | `repeat_of_rejected_architecture` | **PASS** |
| `unassessed-redis-cache` | Simplicity | `unassessed_premise` | `unassessed_premise` | **PASS** |

### 5.2 Structural Analysis & Limitations

Standard codebase RAG relies on lexical or embedding similarity against existing repository files. Because rejected architectural alternatives are deliberately absent from the main branch codebase, standard retrieval lacks any representation of discarded alternatives unless an ADR happens to match the embedding query. Even when an ADR text is retrieved, standard RAG cannot evaluate whether the premises justifying that rejection have since been invalidated by subsequent commits.

Bruriah addresses this by representing alternatives and premises as first-class relational entities linked to Git lineage.

#### Documented Limitations
1. **Name-Based Alternative Matching**: The current matching engine performs case-insensitive substring search on alternative names (e.g., matching `"FastMCP"` in `"Migrate server to FastMCP"`). Queries that describe an alternative without naming it (e.g., *"use an in-memory cache"*) require future integration with semantic or synonym mapping.
2. **Authoring Precondition**: Counterfactual tracking requires explicit authoring of rejected alternatives and premises in ADR frontmatter or Git commit trailers. Unrecorded historical discussions remain inaccessible.
3. **Advisory Semantics**: Bruriah provides structured assessments and conflict flags inside `investigate_work`. It does not forcefully terminate or block external LLM client execution unless wrapped by an external host-level gate.

---

## 6. Related Work

- **Conversational Memory Buffers (Mem0, Letta/MemGPT)**: Store chat history and user preferences as key-value pairs or conversational summaries. While effective for user personalization, they lack codebase grounding, fail to track causality across Git commits, and suffer from lossy compression hallucinations.
- **Source-Level RAG (Cursor, GitHub Copilot, Devin)**: Indexes existing code ASTs and text passages. Incapable of retrieving rejected architectures because the code for rejected alternatives was never merged into the primary branch.
- **Architecture Decision Records (ADRs)**: Standard practice in software engineering (Nygard et al.). Historically maintained as human-readable documents in `docs/adr/`. Bruriah bridges ADRs into machine-verifiable graphs for autonomous agents without requiring manual prompt injection.

---

## 7. Conclusion

Autonomous coding agents cannot be trusted with architectural leadership if they suffer from Architectural Amnesia. By anchoring memory in Git lineage and evaluating decisions as dynamic functions of their supporting premises, **Bruriah provides evidence-backed counterfactual memory for AI coding agents**.

By verifying deterministic correctness across 20 diverse architectural regression scenarios, Bruriah demonstrates that high-rigor memory does not require bloated vector infrastructure or chatty MCP interfaces—only formal discipline, immutable hashes, and respect for developer provenance.

