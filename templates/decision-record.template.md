---
# Professional Decision Record (PDR) Template
# Generic format for tracking falsifiable decisions, rejected alternatives, and premise drift.

status: active # active | superseded | deprecated
premises:
  - id: <premise-unique-id>
    statement: "<Falsifiable condition or assumption required for this decision>"
    status: active # active | invalidated | uncertain

alternatives:
  - name: "<Alternative or Discarded Option Name>"
    disposition: rejected # rejected | deferred | superseded
    reason: "<Specific reason this alternative was not selected>"
    premises:
      - <premise-unique-id> # The premise(s) that conditioned this rejection

invalidated_premises: [] # List premise IDs invalidated by this specific record, if any
---

# [RECORD-ID]: [Decision Title]

## 1. Context & Problem Statement
Describe the current state, facts, or observations motivating this decision.

## 2. Decision Outcome
State the concrete choice made (e.g., treatment plan, legal strategy, architectural pattern).

## 3. Evaluated Alternatives & Trade-Offs
- **Selected**: Why this approach was chosen under current premises.
- **Rejected/Deferred**: Alternatives considered, why they were set aside, and what condition (`Viable-If`) would make them viable again.
