# Make it inexpressible

When something must never happen, prefer a design where it cannot be written down over a check that refuses it.

## Why

A check runs at one moment, in one code path, and only if someone remembered to call it. It can be bypassed by a new caller, disabled during debugging, or quietly reordered until it no longer guards anything.

A thing that cannot be represented has none of those failure modes. There is no path that skips it, because there is no value to skip.

The difference shows up in review. A reviewer reading a check has to decide whether it is correct, complete, and reached. A reviewer reading a type that admits only valid values has nothing left to verify.

## How

**Narrow the type until the bad value has no name.** If a field must be one of three states, make it those three states. A boolean plus a comment describing the third case is a bug waiting for a caller.

**Represent "deny" as absence, not as a token.** A permission list that starts empty and has no wildcard cannot express "allow everything", however it is filled in. A list containing `"*"` can, and now every consumer must remember what `"*"` means.

**Leave out the field that could carry the wrong claim.** A report that has no `safe` field cannot say something is safe. Reviewers then have to read the findings, which is the behaviour you wanted. Adding the field and always setting it to `false` is not the same thing — someone will set it to `true`.

**Make the dangerous variant unrepresentable rather than rejected.** If executable content is out of scope for this version, a `payload` field typed as the literal `"prose"` closes it permanently. A validator that rejects other payloads leaves the door in the wall.

## The test that keeps it closed

Assert the *absence* structurally: the exact field set, the exact enum members, the forbidden names checked by name. Then a future change that reintroduces the possibility fails a test that explains why it exists, instead of passing review because it looks harmless.

This is the rare case where testing for absence is worth more than testing for behaviour.

## When a check is the right tool

Not everything can be made inexpressible, and forcing it produces worse designs than the checks it replaces.

Use a check when the constraint is about relationships between valid values (this date before that one), when it depends on state the type cannot see (does this file exist), or when the type system's expressive limit would push the complexity somewhere worse. A regex engine without lookahead cannot express "no `..` segment"; a pattern that appears to and silently does not is far more dangerous than a plain loop.

Know which one you are relying on, and never let a check *look* like a structural guarantee.

## Limits

Inexpressibility protects against the values you anticipated. It does nothing about a design where the wrong thing is expressible for good reasons, and it can make legitimate future cases expensive — the cost is real, and it is paid by whoever needs the case you closed.

Choose it where the failure is severe and the legitimate case is genuinely absent, not everywhere it is technically possible.
