# Deployment policy

**Decided:** 2026-03-11 · **Commit:** `a1b2c3d4` · **Author:** Team

Every deploy to production goes through staging first. No exceptions.
Staging runs the full migration suite against a production-shaped dataset,
so a migration that will fail in production fails in staging instead.
