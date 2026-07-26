# Undiscoverable is unbuilt

A feature nobody can find out how to use does not exist, and your test suite will never tell you.

## Why

A suite verifies what the code does **when called correctly**. Every test constructs its inputs by hand, with full knowledge of the interface — which is exactly the knowledge a real caller does not have.

So a feature can be complete, correct, covered, and completely unreachable. The tests are green because the tests know things the user cannot know. This is a structural blind spot, not an oversight: no amount of additional test coverage closes it, because every test you add will also construct its inputs with insider knowledge.

The failure is silent, and it is worse the more optional the feature is. Anything gated behind a parameter a caller must know to send is dead until something teaches them to send it.

## How to check

**Read the interface as a stranger.** Not the code — the *published* interface. The API schema, the CLI `--help`, the tool description, the function signature and docstring. Ask: from this alone, would someone know this capability exists?

**Look for the fields you invented.** Any parameter that is not standard for your protocol or ecosystem is one a caller has no prior reason to send. Those are the highest risk, always. A field named plausibly, with a type and no explanation, teaches nothing.

**Check what a default does.** If omitting a parameter silently disables a feature, that default is a decision about whether the feature ships at all. Optional-and-undocumented is off.

**Ask what happens on partial knowledge.** Someone who knows the field exists but not what to put in it will send nothing, or an empty value. Make sure the empty value does something sensible — and say so, explicitly, in the description.

## Write the description for the caller, not the implementer

Say what it is *for*, not what it *is*. "Skills the calling agent has installed" describes the field. "Send an empty list if you don't know what you have — you'll be told which ones apply" tells someone what to do.

State the cost of omitting it. A caller weighing whether to bother needs to know what they lose, and "you receive no guidance at all" is a much stronger prompt than silence.

## Pin it with a test

Discoverability regresses silently: someone tidies a schema, drops a description, and every test stays green while the feature stops existing.

Assert that the description is present and mentions the things a caller needs — the empty-value case, the cost of omission. It looks like a trivial test. It is the only one guarding a whole feature's reachability.

## Limits

A good description is necessary and nowhere near sufficient. It does not make the feature discoverable if nothing surfaces the interface in the first place, and it cannot fix a capability that is genuinely hard to explain — that is usually a design problem wearing a documentation costume.

Nor does it prove anyone actually uses it. The only real evidence is watching someone who did not build it try.
