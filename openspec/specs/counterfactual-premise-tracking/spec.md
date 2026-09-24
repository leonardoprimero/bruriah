# Counterfactual Architectural Memory & Premise Tracking Specification

## Purpose

Define deterministic, evidence-backed counterfactual memory and premise tracking for software architectural decisions.

Instead of treating past architectural decisions as permanent dogmatic prohibitions ("never use X"), Bruriah tracks the underlying **premises** that justified each decision. When an autonomous coding agent or human engineer attempts an approach that was previously evaluated and rejected, Bruriah determines whether the supporting premises remain active, have been invalidated by subsequent repository changes, or require human reevaluation.

---

## Core Domain Models

> As of 2.0.0, the fields below describe the domain model Bruriah parses from the corpus, not
> the wire shape `investigate_work` returns. `PremiseRecord`/`AlternativeRecord`/
> `CounterfactualAssessment` are opaque by construction on the wire (`ref`/
> `matched_alternative_ref`, as `alt:v1:`/`premise:v1:` hashes); `name`, `statement`, `reason`
> and free-text `rationale` are corpus-authored text and reach a caller only through an explicit
> `read_evidence` call on the ref, never inside `investigate_work`'s own response.

### 1. Premise Record (`PremiseRecord`)
A contextual constraint, dependency limitation, or environmental invariant that was assumed true at the moment of decision:

- `id`: Stable normalized slug (e.g., `fastmcp-no-forbid`, `infra-no-managed-db`).
- `statement`: Verbatim assertion of the condition at decision time.
- `status`: One of:
  - `active`: The condition is still assumed to hold; no counter-evidence or invalidation has been declared.
  - `invalidated`: Subsequent repository commits or ADRs explicitly declared that this premise no longer holds.
  - `uncertain`: The premise depends on external components whose currency has not been verified.
- `invalidated_by`: Optional commit SHA or document ref that invalidated the premise.
- `rationale`: Context explaining why the premise was established or changed.

### 2. Alternative Record (`AlternativeRecord`)
An architectural approach, library, or pattern evaluated alongside or in contrast to the chosen solution:

- `name`: Identifier or title of the alternative (e.g., `FastMCP`, `PostgreSQL`, `JWT in localStorage`).
- `disposition`: One of `rejected`, `deferred`, `superseded`.
- `reason`: Concrete justification for rejection.
- `premises`: Tuple of premise IDs that justified the disposition.

### 3. Counterfactual Risk (`CounterfactualAssessment`)
Structured evaluation returned during investigation when a task or code target matches a rejected alternative:

- `matched_alternative_ref`: Opaque reference (`alt:v1:<hash>`) to the matched alternative; the
  name is not on the wire, and resolves only via an explicit `read_evidence` call on the ref.
- `decision_ref`: Reference to the governing decision where it was evaluated.
- `verdict`: One of:
  - `repeat_of_rejected_architecture`: All supporting premises remain `active`. Reintroducing this pattern repeats a verified historical mistake.
  - `premise_changed_requires_reevaluation`: At least one supporting premise is `invalidated`. The original rejection was valid when made, but the underlying justification no longer applies.
  - `unassessed_premise`: Supporting premises have no verification record. Human architectural review is required.
- `supporting_evidence`: Tuple of immutable evidence references.

---

## Requirements

### Requirement: Structured Declaration via Git Trailers & Frontmatter
Bruriah MUST extract alternatives, rejection reasons, and premise declarations from standard Git commit trailers and Markdown YAML frontmatter:

- Trailers:
  - `Alternative-Rejected: <name>`
  - `Rejection-Reason: <reason>`
  - `Premise: <id> | <statement>`
  - `Premise-Invalidated: <id>`
- Frontmatter:
  ```yaml
  alternatives:
    - name: FastMCP
      disposition: rejected
      reason: Drops unknown fields silently without extra="forbid"
      premises:
        - fastmcp-no-forbid
  premises:
    - id: fastmcp-no-forbid
      statement: FastMCP derives schemas without extra="forbid"
      status: active
  ```

#### Scenario: Commit declares rejected alternative and premise
- GIVEN a commit message containing `Alternative-Rejected: FastMCP`, `Rejection-Reason: no extra forbid`, and `Premise: fastmcp-no-forbid | FastMCP lacks extra forbid`
- WHEN `bruriah corpus` parses the commit
- THEN it extracts an `AlternativeRecord` linked to `fastmcp-no-forbid` with `status: active`.

#### Scenario: Subsequent commit invalidates historical premise
- GIVEN a later commit containing `Premise-Invalidated: fastmcp-no-forbid`
- WHEN `bruriah index` builds the lineage and premise graph
- THEN `fastmcp-no-forbid` is recorded with `status: invalidated` and `invalidated_by: <commit-sha>`.

---

### Requirement: Premise Identifier Conflicts (2.0.1)
`premise_id` is the `premises` table's PRIMARY KEY and MUST stay globally unique across the whole
corpus, but two documents declaring the same id are not equally trustworthy: a repository-authored
document (the corpus tree, or a document `gitcorpus` derives from a commit) MUST always outrank a
document `github_corpus` generated from an issue or pull request, because that body's text is
chosen by whoever opened it, not by the repository owner.

- A GitHub-tier declaration for an id a repository-tier document already claims MUST be dropped,
  never merged into `premises_map`, and the drop MUST be reported (premise id and source document)
  in `bruriah index`'s summary line and its JSON output.
- Two repository-tier documents declaring the same id have no safe automatic resolution: `bruriah
  index` MUST fail with a typed `duplicate_premise_id` error naming both documents, rather than
  letting whichever one happens to parse last silently win.
- Two GitHub-tier documents declaring the same id MUST resolve deterministically: the lower
  issue/PR number wins, and the other is dropped and reported the same way.
- `invalidated_premises`/`Premise-Invalidated` is trust-tiered the same way a declaration is: a
  document MAY change the `status`/`invalidated_by` of a premise its own tier or a lower-trust tier
  claims, but a GitHub-tier document's `invalidated_premises` entry MUST NOT change a
  repository-tier premise's `status`/`invalidated_by`. That entry MUST instead be dropped and
  reported the same way as a losing declaration (reason `github_invalidation_ignored`). This is
  defense in depth: `github_corpus` does not currently emit any `invalidated_premises` (only
  `premises` declarations, from `Premise:` lines), but the boundary applies regardless of what a
  future or hand-authored GitHub-tier document declares.

#### Scenario: A GitHub issue redeclares a repository premise
- GIVEN a repository-authored ADR declaring `id: fastmcp-no-forbid` with `status: active`, and a
  GitHub issue body declaring `Premise: fastmcp-no-forbid | ...` with a different statement
- WHEN `bruriah index` builds the premise table
- THEN the repository declaration's statement and status are stored unchanged, the GitHub
  declaration is dropped and reported, and `investigate_work`'s counterfactual verdict for that
  premise is unaffected.

#### Scenario: Two repository documents declare the same premise id
- GIVEN two repository-authored documents each declaring `id: scale-premise`
- WHEN `bruriah index` builds the premise table
- THEN the build fails with a typed `duplicate_premise_id` error naming both documents, and no
  candidate index is promoted.

#### Scenario: A GitHub document tries to invalidate a repository premise
- GIVEN a repository-authored ADR declaring `id: scale-premise` with `status: active`, and a
  GitHub-tier document declaring `invalidated_premises: [scale-premise]`
- WHEN `bruriah index` builds the premise table
- THEN the repository declaration's `status`/`invalidated_by` are stored unchanged, the GitHub
  invalidation is dropped and reported with reason `github_invalidation_ignored`, and
  `investigate_work`'s counterfactual verdict for that premise is unaffected.

---

### Requirement: Counterfactual Assessment during Investigation
`investigate_work` MUST evaluate candidate matches against historical alternatives and active premises without expanding the two-tool public contract:

- If `task` or `code_target` matches a rejected alternative:
  - If all linked premises are `active`, `investigate_work` MUST return `counterfactual_risk: repeat_of_rejected_architecture`.
  - If any linked premise is `invalidated`, `investigate_work` MUST return `counterfactual_risk: premise_changed_requires_reevaluation`.
  - The assessment MUST cite the exact commit/line references for both the original rejection and the premise invalidation.

#### Scenario: Agent attempts previously rejected approach with active premises
- GIVEN a codebase where `FastMCP` was rejected under active premise `fastmcp-no-forbid`
- WHEN an agent calls `investigate_work(task="migrate server to FastMCP", code_target="src/bruriah/mcp_server.py")`
- THEN `investigate_work` returns `verdict: repeat_of_rejected_architecture` with evidence citing commit `e8f3003bda26`.

#### Scenario: Agent attempts previously rejected approach after premise changed
- GIVEN a codebase where premise `fastmcp-no-forbid` was subsequently invalidated by commit `f6e5d4c3`
- WHEN an agent calls `investigate_work(task="migrate server to FastMCP", code_target="src/bruriah/mcp_server.py")`
- THEN `investigate_work` returns `verdict: premise_changed_requires_reevaluation` citing both commits and noting that the historical rejection may no longer hold.

---

### Requirement: Zero New MCP Tools & Strict Boundaries
Premise tracking MUST NOT introduce additional MCP tools or endpoints. All counterfactual intelligence MUST be delivered within the existing `investigate_work` response envelope as structured, verifiable metadata.
