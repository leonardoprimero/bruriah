# Why we chose PostgreSQL over MongoDB

**Decided:** 2025-11-02 · **Commit:** `f9e8d7c6` · **Author:** Team

We need transactional guarantees across three tables written in one request.
A document store makes that a two-phase problem we would have to solve ourselves,
and we would solve it worse than a database that already has it.
