# Bruriah Architecture

Bruriah implements a **Clean / Hexagonal Architecture (Ports and Adapters)**, strictly decoupling pure domain algorithms, persistence gateways, application use cases, and protocol adapters.

```
┌─────────────────────────────────────────────────────────────┐
│                    Driving / Input Adapters                 │
│         mcp_server.py (MCP JSON-RPC)  |  cli.py (CLI)       │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                   Application Use Cases                     │
│   InvestigateService   │    ReadService    │  SearchService │
└──────────────┬───────────────┴──────┬───────────────┬───────┘
               │                      │               │
               ▼                      ▼               ▼
┌──────────────────────────────┐  ┌───────────────────────────┐
│         Domain Core          │  │    Persistence Adapters   │
│  ranking.py (BM25, RRF)      │  │  repository.py            │
│  contracts.py (Models)       │  │  (SnapshotRepository)     │
│  language.py (Tokenization)  │  │  cache.py (ResearchCache) │
└──────────────────────────────┘  └───────────────────────────┘
```

---

## Architectural Layers

| Layer | Primary Modules | Responsibility | Inward / Outward Dependencies |
|---|---|---|---|
| **Domain** | [`src/bruriah/ranking.py`](file:///Users/leguillo/bruriah/src/bruriah/ranking.py)<br>[`src/bruriah/contracts.py`](file:///Users/leguillo/bruriah/src/bruriah/contracts.py)<br>[`src/bruriah/language.py`](file:///Users/leguillo/bruriah/src/bruriah/language.py) | Pure business math and entities. BM25 calculation, vector ranking, RRF rank fusion, and validation models. | **Zero dependencies** on I/O, SQLite, network, or external protocols. |
| **Persistence (Gateways)** | [`src/bruriah/repository.py`](file:///Users/leguillo/bruriah/src/bruriah/repository.py)<br>[`src/bruriah/cache.py`](file:///Users/leguillo/bruriah/src/bruriah/cache.py) | Data access abstraction. Encapsulates SQLite queries against active snapshots, metadata parsing, and cache storage. | Implements repository interfaces; raises typed `RepositoryError` rather than leaking `sqlite3` exceptions. |
| **Application (Use Cases)** | [`src/bruriah/service.py`](file:///Users/leguillo/bruriah/src/bruriah/service.py)<br>[`src/bruriah/retrieval.py`](file:///Users/leguillo/bruriah/src/bruriah/retrieval.py) | Core application orchestration: `InvestigateService`, `ReadService`, and `SearchService`. | Orchestrates repositories and domain models via Dependency Injection (`ServiceDeps`). |
| **Driving (Adapters)** | [`src/bruriah/mcp_server.py`](file:///Users/leguillo/bruriah/src/bruriah/mcp_server.py)<br>[`src/bruriah/cli.py`](file:///Users/leguillo/bruriah/src/bruriah/cli.py) | Protocol and CLI interfaces. Serializes/deserializes wire formats, converts typed stage errors to error envelopes. | Calls application use cases (`investigate`, `read`). Contains no business logic or SQL. |

---

## Key Use Cases

### 1. Investigation (`InvestigateService`)
Orchestrates the investigation pipeline:
1. Validates request and decodes pagination cursor.
2. Classifies task domain (`classify.py`) and discovers matching capabilities/skills (`lookup.py`).
3. Evaluates routing policy (`route.py`). If non-proceed, delegates immediately to `assemble_context()`.
4. On `proceed`:
   - Gathers prefix evidence (skills and causal lineage via `why.py`).
   - Executes hybrid retrieval via `SearchService` and applies lineage relations.
   - Executes bounded live research if configured.
   - Paginates, detects truncation, and enforces output character budget (`compact_to_budget`).

### 2. Evidence Reading (`ReadService`)
Polymorphically resolves immutable evidence references across sources:
- `skill:<id>@<version>`: Resolved from `SkillSet` with permission disclosure.
- `capability:<id>`: Resolved from `Registry` with tool metadata disclosure.
- `live:sha256:<hash>`: Resolved from `ResearchDeps` cache without network re-fetching.
- `<passage_ref>`: Resolved from `SnapshotRepository` with exact char-window slicing.

### 3. Search & Ranking (`SearchService`)
Coordinates lexical and semantic retrieval:
- Chooses execution strategy (fast precomputed index vs. full passage scan).
- Calculates BM25 and vector similarity scores via pure `ranking.py`.
- Applies cross-lingual lexical discounting when query and corpus languages diverge.
- Fuses rankings with Reciprocal Rank Fusion (RRF) and applies optional cross-encoder reranking.
- Lazily hydrates top-ranked passages bounded by candidate and extraction limits.

---

## Testing Strategy

Each layer is verified independently:
- **Unit Tests (`tests/test_ranking.py`)**: Tests domain ranking math in-memory without database or I/O.
- **Repository Tests (`tests/test_repository.py`)**: Tests `SnapshotRepository` queries and typed error mapping in-memory.
- **Service Tests (`tests/test_service.py`, `tests/test_retrieval.py`)**: Tests `InvestigateService`, `ReadService`, and `SearchService` with injected dependencies.
- **Contract & Protocol Tests (`tests/test_mcp_contract.py`, `tests/test_cli.py`)**: Tests MCP protocol compliance and CLI command dispatch.
