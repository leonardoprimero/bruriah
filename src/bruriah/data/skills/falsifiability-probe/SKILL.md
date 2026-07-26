# Falsifiability probe

Before you believe a test protects something, break the thing on purpose and watch the test fail.

## Why

A passing test proves the suite is green. It does not prove the test would notice if the behaviour disappeared. Tests that assert something already true by construction pass forever and protect nothing, and you cannot tell them apart from real ones by reading — both are green.

The only way to know a test is load-bearing is to remove what it guards and confirm it goes red.

## How

1. Write the code and the test. Get to green.
2. Pick the invariant the test claims to protect.
3. **Break exactly that**, in the smallest way that removes the property. Delete the check, invert the comparison, return early, empty the loop.
4. Run the suite.
5. Read which tests failed, and *how many*.
6. Revert.

Do this once per invariant that matters, not once per test.

## Reading the result

**Exactly the expected tests fail.** The invariant is genuinely pinned. This is the outcome you want.

**Nothing fails.** The test is tautological, or the code path is unreachable, or you broke something that was never live. All three are worth knowing. Fix the test, not the probe.

**More tests fail than expected.** The invariant is load-bearing in places you did not know about. Read the extra failures before assuming they are noise — they are a map of hidden coupling.

**A different test fails than expected.** Your mental model of the code is wrong. Stop and find out why before continuing.

## Choose the probe that would actually happen

The best probes are changes a competent person would make in good faith six months from now, not sabotage.

Sorting cached results first to save a lookup. Trusting a version label instead of a hash because it reads better in logs. Collapsing two error codes that "mean the same thing". Adding a `safe: true` field because a caller asked for one.

If your probe is something nobody would ever write, it tells you nothing about the future of the code.

## When a probe finds a real defect

That is the point, and it is common. Expect roughly one probe in three to find something — usually a gap in the test rather than a bug in the code.

The most valuable outcome is a probe that **causes the harm instead of failing an assertion**: removing a guard and finding the forbidden file actually written, or a leak actually present in the output. An assertion tells you a test noticed. Real damage tells you the guard was the only thing standing there.

## Limits

A probe verifies that a test detects a change. It says nothing about whether the invariant is the *right* one, whether the design is sound, or whether some other property matters more. It is a check on your tests, not on your thinking.

It also cannot prove absence. Passing every probe you thought of means the invariants you named are pinned. The ones you did not name are still unguarded.
