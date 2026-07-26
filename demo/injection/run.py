#!/usr/bin/env python3
"""A poisoned corpus, and what a retrieval layer hands your agent.

Run it:

    uv run python demo/injection/run.py

It builds two indexes over `corpus/` -- one containing a note with an injection
payload, one without it -- and then measures three things. Every claim below is an
`assert` in this file, so if a future change breaks a property the demo fails
rather than continuing to advertise it.

The baseline is not a strawman of somebody else's product. It is the *same*
retrieval, over the *same* index, with the one difference that defines the common
pipeline shape: it puts passage TEXT into the model's context. The bytes are taken
from this project's own `read_evidence`, so the comparison cannot be rigged.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from bruriah import cli  # noqa: E402
from bruriah.contracts import InvestigationRequest, ReadRequest  # noqa: E402
from bruriah.service import investigate, read  # noqa: E402

TASK = "what is our deployment policy for production"
POISON_MARKERS = ("id_rsa", "Ignore all previous", "deploy directly to", "supersedes every other")
POISONED_NOTE = "onboarding-notes.md"


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n" + "-" * len(title))


def build(corpus: Path, root: Path):
    """Index `corpus` into a throwaway data directory and return live deps for it."""
    env = dict(os.environ, BRURIAH_DATA_DIR=str(root / "data"), BRURIAH_CONFIG_DIR=str(root / "cfg"))
    paths = cli.resolve_paths(env=env)
    stdout, sys.stdout = sys.stdout, io.StringIO()  # the build report is noise here
    try:
        code = cli.bruriah_main(["index", "--corpus-root", str(corpus),
                                 "--policy", str(HERE / "policy.yaml"),
                                 "--data-dir", str(paths.data_dir),
                                 "--config-dir", str(paths.config_dir)])
    finally:
        sys.stdout = stdout
    if code != 0:
        raise SystemExit(f"indexing {corpus} failed with exit code {code}")
    return cli.build_serve_deps(paths)


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="bruriah-injection-"))
    try:
        clean_corpus = workspace / "clean-corpus"
        shutil.copytree(HERE / "corpus", clean_corpus)
        (clean_corpus / POISONED_NOTE).unlink()

        poisoned = build(HERE / "corpus", workspace / "poisoned")
        clean = build(clean_corpus, workspace / "clean")

        result = investigate(InvestigationRequest(task=TASK, host_skills=[]), poisoned)
        payload = result.model_dump(mode="json")
        wire = json.dumps(payload)

        # --- what a text-returning pipeline would have handed over ------------------------
        rule("1. The baseline: a pipeline that returns passage TEXT")
        ref = next(e["ref"] for e in payload["evidence"] if e["locator"] == POISONED_NOTE)
        body = read(ReadRequest(refs=[ref]), poisoned).model_dump(mode="json")["items"][0]["content"]
        print("These bytes reach the model's context, indistinguishable from your own prompt:\n")
        for line in body.splitlines():
            if line.strip():
                print(f"  \033[31m{line}\033[0m" if any(m in line for m in POISON_MARKERS) else f"  {line}")
        assert any(marker in body for marker in POISON_MARKERS), "the demo corpus lost its payload"

        # --- what investigate_work returns instead ----------------------------------------
        rule("2. investigate_work returns references, never corpus prose")
        found = [marker for marker in POISON_MARKERS if marker in wire]
        print(f"Whole response: {len(wire)} bytes. Payload fragments present: {found or 'none'}")
        print("\nThe poisoned note is still FOUND. This is what is said about it:\n")
        record = next(e for e in payload["evidence"] if e["locator"] == POISONED_NOTE)
        for key in ("locator", "citation_locator", "digest", "authority", "authority_rationale"):
            print(f"  {key:22} {record[key]}")
        assert not found, f"corpus prose reached the response: {found}"

        # --- and the content could not steer the decision ---------------------------------
        rule("3. Corpus content cannot change what gets selected")
        after = investigate(InvestigationRequest(task=TASK, host_skills=[]), clean).model_dump(mode="json")

        def decisions(value: dict) -> dict:
            """Everything the router DECIDED, as opposed to what it retrieved."""
            return {key: value[key] for key in ("status", "host_actions", "gaps", "claims",
                                                "conflicts", "warnings")}

        print("The note claims to supersede every policy in the corpus. With it and without it,")
        print(f"the routing decision is identical: {decisions(payload) == decisions(after)}")
        assert decisions(payload) == decisions(after), "corpus content influenced selection"

        # "Identical" is only interesting if the comparison could have come out otherwise. A
        # decision function that returns the same thing for everything would pass the check above
        # while proving nothing at all, so the resolution is measured rather than assumed.
        elsewhere = investigate(InvestigationRequest(
            task="what are the employment law requirements for firing someone in Argentina",
            host_skills=[]), poisoned).model_dump(mode="json")
        print("\nThat is only worth something if the comparison has resolution, so: the same")
        print("comparison over a different QUESTION does come out different --")
        print(f"  this corpus, deployment question: status={payload['status']}, gaps={payload['gaps']}")
        print(f"  this corpus, employment law:      status={elsewhere['status']}, gaps={elsewhere['gaps']}")
        assert decisions(elsewhere) != decisions(payload), "the comparison has no resolution"
        print("\nSensitive to what you ask. Deaf to what your documents say about themselves.")

        # --- the part that is not a safety guarantee --------------------------------------
        rule("4. What this does NOT do")
        print("`read_evidence` returned the payload above, because that is its job: you asked")
        print("for the bytes of a document you had already been told the origin of. Bruriah does")
        print("not sanitise it and does not stop a host that decides to paste it into a prompt.")
        print("\nWhat it does is narrower and checkable: the step where your agent DECIDES what")
        print("is relevant never sees corpus prose, and no document -- however it is written --")
        print("can change that decision. Whether the text persuades a model afterwards is not")
        print("observable by inspection, and this project refuses to claim otherwise.")

        rule("Result")
        print("\033[32mAll three properties hold.\033[0m")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
