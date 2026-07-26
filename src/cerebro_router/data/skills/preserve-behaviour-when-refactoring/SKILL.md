# Preserve behaviour when refactoring

If a refactor preserves behaviour, you should not need to touch a single test. Touching tests is the signal that you did something else.

## Why

"Refactor" and "change behaviour" get done in the same commit constantly, and the result is unreviewable: the diff is large, the tests moved, and nobody can tell which edits were mechanical and which were decisions. When something breaks two weeks later, the bisect lands on a commit that did four things.

The test suite is the only independent description of current behaviour you have. If you edit it in the same breath as the code it describes, you have removed the instrument you were using to measure.

## How

**State the invariant first.** "Behaviour is unchanged" is not a plan. "Every error code and, critically, every check ORDER stays identical" is — because the second one tells you what to look at.

**Move code, do not rewrite it.** A reviewer can diff two blocks and confirm they match. Nobody can confirm that a rewrite is equivalent by reading.

**Parameterise the couplings, do not generalise the design.** Extracting shared logic usually means two or three call-site-specific details need to become arguments. Name exactly those and stop. A refactor that also improves the abstraction is two changes.

**Run the suite with zero test files modified.** That is the proof. Not "tests pass" — tests passing after you edited them proves nothing at all.

**If a test must change, justify it in the commit.** Sometimes a test pinned an implementation detail rather than a behaviour, and the change is legitimate. Say which test, why the old assertion was wrong, and what replaced it. One justified edit is fine; five unexplained ones mean the refactor was a rewrite.

## The order of checks is behaviour too

Easy to miss, and it bites hard.

When two conditions fail at once, which error surfaces first is observable, and callers branch on it. Extracting a validation sequence can silently reorder those checks: the suite stays green because each condition has its own passing test, and nothing covers what happens when two fail together.

Before extracting, add a test over deliberately overlapping failures so it captures current behaviour as a baseline rather than the post-refactor behaviour. Write it **first**, or it will simply enshrine whatever you just did.

## The honest failure mode

Sometimes you write that precedence test and discover the existing suite already pinned the ordering. That means the risk you claimed was overstated. Say so — a refactor whose stated danger turned out to be smaller than advertised is a finding, not an embarrassment.

## Limits

Preserving behaviour is not the same as preserving quality: a faithful extraction of a bad design gives you a well-organised bad design.

And "zero tests modified" only proves as much as the suite covers. It is a strong signal precisely because it is cheap, not because it is complete — untested behaviour can be silently rewritten and nothing turns red.
