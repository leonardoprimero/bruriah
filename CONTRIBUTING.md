# Contributing

Thanks for looking. The most useful thing you can send me is not a pull request — it is a report of
where this broke on your repository, with the corpus shape that broke it.

## Setup

```bash
git clone https://github.com/leonardoprimero/bruriah && cd bruriah
uv sync
uv run python -m pytest tests -q -p no:randomly
```

Python 3.12 or 3.13, macOS or Linux. On Windows use WSL — the package refuses to import natively
and [says why](SECURITY.md#platform).

Some tests skip unless you have my private corpus. That is expected; `-rs` lists them. A fresh
clone runs the whole product suite green.

## What a change needs

**A falsifiability probe.** Break the invariant you just wrote, on purpose, and confirm the right
test fails. Then revert. Roughly one probe in three finds something here — one of them removed a
guard and *wrote a live private key into the package directory* instead of merely failing an
assertion, which is how I learned that guard was the only thing standing there. If your test cannot
fail, it is documentation with a green tick.

**A verified claim, not a plausible one.** Run the command, paste the real output. Numbers in a
commit message or a README should come from an execution, not from memory. The one time I skipped
this, the README shipped a measurement that had gone stale in the same commit that wrote it.

**A reason in the commit body.** This repository is its own test corpus: `bruriah corpus` turns
commits with explanatory bodies into the documents the eval scores against. A commit that records
*what* changed and not *why* is invisible to it, and to whoever inherits your decision.

## Things that will get pushed back

- **Widening the MCP surface.** It is exactly two read-only tools and that is a design decision, not
  an unfinished state. Mutation belongs in the CLI, where a human runs it.
- **Putting a model in the selection path.** Which sources apply is deterministic set membership. If
  corpus content could influence selection, the central property would be gone.
- **A skill payload that is not prose.** `payload` is `Literal["prose"]` on purpose.
- **Rewriting history to match the present.** Decision records under `openspec/` and the eval's
  ground truth name commits as they were actually written. A rename that "tidies" them falsifies the
  record — I did this to the eval once and every ground-truth entry then pointed at a document that
  can never exist.
- **A security claim that overreaches.** See [SECURITY.md](SECURITY.md). A narrower claim that
  survives inspection is worth more than a broad one that does not.

## Evaluations are welcome, especially unflattering ones

`evals/project-memory/` is twelve questions against this repository's own history. Twelve is a small
sample and one corpus in one pair of languages proves very little.

If you run it on **your** repository — better, with questions **you** wrote — that is more valuable
than a feature. Cross-lingual recall@3 sits at 58% against English's 83%, and that number came from
measuring rather than guessing; the next one should too.

## Licence

Apache 2.0. By contributing you agree your work ships under it.
