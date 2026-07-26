# Find the time bomb

Some code is correct today and broken on a date nobody wrote down. Go looking for it on purpose, by moving the clock.

## Why

A test suite runs at `now`. Every assertion it makes is about the present, so a system that fails on a future date passes every test until the day it does not — and then it fails in production, at once, for everyone, with no deploy to blame.

These are not rare. Expiry windows, freshness thresholds, certificate lifetimes, hardcoded years, "temporary" version pins, review dates. They are usually written by someone careful, which is exactly why they exist: a thoughtless developer would not have added an expiry at all.

## How

**Inject the clock, always.** A function that calls `date.today()` internally cannot be tested against the future. One that takes `today` as a parameter can be swept across years in a loop. If you can only fix one thing about a codebase's relationship with time, make it this.

**Then actually sweep it.** Not one future date — a range that crosses every threshold you can find:

```
for day in ["today", "+31d", "+1y", "+1y+1d", "+3y"]:
    load_the_thing(today=day)
```

Print what happens at each. You are looking for the first date where something stops working, and whether that date was intended.

**Compare the two deadlines.** Any component with both an expiry and a freshness window has two clocks, and they rarely agree. A pack reviewed today with a one-year expiry and a thirty-day freshness window dies in thirty days, not a year — eleven months earlier than its author declared. Whichever deadline is checked first wins, and it is usually not the one people think about.

**Grep for the literals.** Years, ISO dates, and durations in seconds sitting in source. Each one is a decision somebody made without a test.

## What to do when you find one

Ask what the deadline is *for* before shortening or lengthening it.

An expiry on data usually means "this may be wrong after this date". The right response to that is almost never "refuse to start". Refusing to serve stale content is proportionate; refusing to boot because content is stale takes down everything else with it. Prefer degrading the affected part and saying so loudly.

And check the failure blast radius: whether the deadline stops one feature or the whole process depends on where it is enforced, and that is usually an accident rather than a decision.

## The uncomfortable part

You will plant them yourself. Fixing an expiry bomb in one component and then introducing an identical one in a component you add a week later is entirely normal — the reasoning that produced the first one is still the reasoning you have.

So make the sweep a habit at the point where content gains a lifetime, not a one-time cleanup. The second bomb is found the same way as the first, and only if you look again.

## Limits

Sweeping the clock finds deadlines that are *in the code*. It says nothing about external ones: a token that expires server-side, an API sunset, a dependency dropping support. Those need a calendar, not a test.

It also cannot tell you whether a deadline is correct — only where it is and what it takes down.
