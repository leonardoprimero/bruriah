"""Agent regression benchmark: how often a coding agent's change reintroduces an alternative a
project explicitly rejected, with and without Bruriah.

The headline metric is Regression Rate (RR), decided per run by each trap's deterministic
detector over the resulting tree, never by a model. Method, conditions, and threats to validity
are in `odd/tasks/agent-regression-benchmark.md`.

This is a real package imported through `evals/` on `sys.path` (`from agent_regression.metrics
import ...`), never through flat module names: `evals/retrieval` already ships `metrics.py` and
`adapters.py`, and `evals/injection` ships `run.py`, so flat names would collide in one pytest
session.
"""
