# A poisoned corpus

```bash
uv run python demo/injection/run.py
```

Takes about three seconds. Builds two throwaway indexes over `corpus/` — three
short notes, one of which contains an injection payload — and measures what a
retrieval layer actually hands your agent.

## Why this demo exists

In an MCP setup the injection does not arrive in the user's message. It arrives
in a **tool result**. Something your agent retrieved becomes part of its context,
and if that something contains instructions, a vulnerable agent may follow them.
The [MCPTox benchmark](https://arxiv.org/pdf/2508.14925) measured attack success
rates above 60% against real MCP servers. In April 2026 researchers hijacked
Claude Code, Gemini CLI and GitHub Copilot through text in pull request titles.

Almost every published defence is perimeter work: allowlists, gateways, proxies
that sanitise, a human in the loop. They inspect the payload and hope to catch it.

This demo shows a different position — one where the step that decides what is
relevant never sees the payload at all.

## What it measures

**1. The baseline.** The bytes a pipeline that returns passage *text* would place
in the model's context, indistinguishable from your own prompt. Those bytes come
from this project's own `read_evidence`, so the comparison cannot be rigged in
its favour.

**2. `investigate_work` returns references, never prose.** The poisoned note is
still *found* — hiding it would be a different and worse failure. What comes back
is a locator, a digest, and `authority: "unknown"` with
`authority_rationale: "not_assessed_by_retrieval"`. There is nothing in the
response to obey.

**3. Corpus content cannot change what gets selected.** The note claims to
supersede every policy in the corpus. The routing decision is identical with the
note and without it. And because "identical" would be worthless if the decision
were constant, the demo also proves the comparison *has resolution*: the same
comparison over a different question does come out different.

Each of the three is an `assert`. Break the property and the demo fails instead
of continuing to advertise it.

## What it does not show

`read_evidence` returns the payload, because that is its job: you asked for the
bytes of a document whose origin you had already been told. Bruriah does not
sanitise it, and does not stop a host that decides to paste it into a prompt.

So this is not "prompt injection solved". The honest claim is narrower and it is
checkable: **the decision step never sees corpus prose, and no document — however
it is written — can change that decision.** Whether text persuades a model
afterwards is not observable by inspection, and this project will not pretend
otherwise.

A narrower claim that survives inspection is worth more than a broad one that
does not.
