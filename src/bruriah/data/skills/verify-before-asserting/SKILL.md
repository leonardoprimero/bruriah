# Verify before asserting

Run the claim. Do not deduce it from the code, and do not recall it from the last time.

## Why

Reading code tells you what it appears to do. Running it tells you what it does. The gap between those two is where the expensive mistakes live, and it is widest exactly where you feel most certain — familiar code, an obvious default, a value you set yourself last week.

The cost is asymmetric. Checking takes seconds. A confident wrong claim propagates into decisions, documentation, and other people's mental models, and gets discovered much later by someone who trusted you.

## What to check before saying it

**Anything with a number.** Versions, limits, counts, sizes, timeouts. "Requires Python 3.12+" and `>=3.12,<3.13` are different claims, and the second one is what the installer enforces.

**Anything you are about to write in documentation.** Every command you put in a README should be executed first, with its flags, in a clean directory. Required arguments, working directories and defaults are exactly what a new user hits first and what an author is blindest to.

**Any example you did not run.** A configuration snippet that matches nobody's semantics is worse than none, because it fails silently and the reader assumes it worked.

**Any claim that something is impossible or unreachable.** These are usually true, and when they are not, they are severe.

## How to check

Prefer the smallest execution that would distinguish the two possibilities. A three-line script beats reading a module. A single command beats a paragraph of reasoning.

Check what the system *actually* returns, not what it should return. If a classifier is supposed to label a phrase one way, feed it the phrase and print the label. Assuming the answer builds fixtures on a false premise, and then the tests test the premise.

Prefer verifying against the real artifact over a stand-in. Signing the actual shipped file and loading it through the unmodified production path proves the signer and verifier agree. Signing a fixture proves only that a tool agrees with itself.

## Say what you checked

Report the evidence, not just the conclusion. "The lock pins `<3.13`" is checkable by the reader. "It needs 3.12" is a claim they have to trust.

When you did not check something, say that too, in the same sentence as the claim. An unverified statement labelled as such is useful. An unverified statement that reads as verified is a liability.

## Limits

Verification tells you what happened once, in one environment. It does not establish that behaviour is stable across platforms, versions, or inputs you did not try, and running something successfully proves nothing about whether it is *correct*.

It also does not scale to everything. Spend it on claims that would change a decision if wrong, and on anything that will be read by someone who cannot easily check it themselves.
