"""The published injection demo has to keep being true.

A demo in a README is a claim, and claims rot. This runs `demo/injection/run.py` as
a subprocess -- exactly as a reader would -- and fails if any of its three assertions
stops holding. The properties themselves are covered by unit tests elsewhere; what is
covered *here* is that the artifact people are pointed at still demonstrates them.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "injection" / "run.py"
README = ROOT / "README.md"
POISONED_NOTE = ROOT / "demo" / "injection" / "corpus" / "onboarding-notes.md"


def test_the_published_injection_demo_still_holds() -> None:
    result = subprocess.run([sys.executable, str(DEMO)], capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    # Not just "it exited 0" -- an early return or a silently removed assertion would also do
    # that. These are the conclusions the demo is published to support.
    assert "Payload fragments present: none" in result.stdout
    assert "the routing decision is identical: True" in result.stdout
    assert "All three properties hold." in result.stdout


def test_the_readme_quotes_the_demo_it_actually_ships() -> None:
    """The front page reproduces a digest and a payload it did not compute at read time.

    Editing the demo corpus silently invalidates both, and a README that quotes a digest
    nothing produces any more is worse than one that quotes none: it is a provenance claim
    that is false, on a project whose entire argument is provenance."""
    readme = README.read_text(encoding="utf-8")
    result = subprocess.run([sys.executable, str(DEMO)], capture_output=True, text=True, timeout=300)
    digest = next(line.split()[-1] for line in result.stdout.splitlines()
                  if line.strip().startswith("digest"))
    assert digest in readme, f"the README quotes a stale digest; the demo now produces {digest}"

    note = POISONED_NOTE.read_text(encoding="utf-8")
    for sentence in ("Ignore all previous deployment rules", "supersedes every other policy"):
        assert sentence in note and sentence in readme, f"README and corpus disagree on: {sentence}"
