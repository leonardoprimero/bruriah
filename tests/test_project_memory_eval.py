"""The published eval has to stay reproducible from the published repository.

`evals/project-memory/` names, as ground truth, the corpus documents that answer twelve questions
about this repository's own history. Those names derive from commit SUBJECTS, which are historical
facts: a document named after `feat(cerebro-router): ...` is what that commit is called, forever.

The Cerebro -> Bruriah rename rewrote them anyway, and every one of the twelve then pointed at a
document that can never exist. Nothing failed. The eval has no runnable scorer, so a corrupted
ground truth is indistinguishable from a working one until somebody tries to reproduce the numbers
printed on the front page -- which is the one thing a reader of THIS project is most likely to do.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals" / "project-memory"
GENERATOR = ROOT / "scripts" / "git_corpus.py"

# Corpus files are `date-sha8-slug.md`; the recorded ground truth carries no sha, because a sha is
# not stable across a history rewrite and the eval is meant to outlive one. Documented here because
# it is the rule that makes the published recipe actually reproduce.
_SHA = re.compile(r"^(\d{4}-\d{2}-\d{2})-[0-9a-f]{8}-")


def _ground_truth() -> set[str]:
    wanted: set[str] = set()
    for name in ("decisions-en.jsonl", "decisions-es.jsonl"):
        for line in (EVALS / name).read_text(encoding="utf-8").splitlines():
            if line.strip():
                wanted.update(json.loads(line)["ground_truth"]["must_include"])
    return wanted


def test_every_ground_truth_document_exists_in_the_generated_corpus() -> None:
    # The generator needs the repository ROOT, and this package is not always at it: in the
    # standalone repo it is, in the monorepo it lives one directory down. Asking git rather than
    # assuming keeps the same assertion true in both layouts.
    toplevel = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
    if toplevel.returncode != 0:
        pytest.skip("not a git checkout")
    with tempfile.TemporaryDirectory() as workspace:
        built = subprocess.run([sys.executable, str(GENERATOR), "--repo", toplevel.stdout.strip(),
                                "--out", workspace], capture_output=True, text=True, timeout=300)
        assert built.returncode == 0, built.stdout + built.stderr
        produced = {_SHA.sub(r"\1-", path.name) for path in Path(workspace).glob("*.md")}
    missing = _ground_truth() - produced
    assert not missing, (
        "the eval names documents this repository's history does not produce, so the published "
        f"numbers cannot be reproduced: {sorted(missing)}")
