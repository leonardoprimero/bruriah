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


# --- The published leakage figures have to stay true of the published question set ---------------
# `evals/project-memory/README.md` states that ten of the twelve English questions hand over a term
# this corpus treats as distinctive, and uses that to bound how the recall numbers on the same page
# may be read. A figure in prose goes stale in silence while continuing to read as though somebody
# maintained it -- this repository shipped 0.5.0 with a test count that had been wrong since 0.4.1,
# and it was caught by noticing rather than by anything asserting it.
#
# So the claim is pinned. Not the exact mean, which moves whenever a commit is added to the corpus
# that changes an IDF, but the COUNT the README leans on, which is the number a reader acts on.
#
# And the CORPUS is pinned too, which this test did not do and had to learn. The corpus is derived
# from this repository's own history, so it grew under the measurement: three commits later the
# same computation returned nine, not because retrieval changed but because new commit messages
# moved the IDF of the terms one question leaned on, dropping it from 0.51 to 0.49. The page was
# never wrong -- it says which corpus it measured, right above the table -- the test was measuring
# a different one and blaming the page. A published figure can only be checked against the corpus
# it was published for; every commit after that is a different measurement wearing the same name.

_EVALS_RETRIEVAL = ROOT / "evals" / "retrieval"
if str(_EVALS_RETRIEVAL) not in sys.path:
    sys.path.insert(0, str(_EVALS_RETRIEVAL))

_PUBLISHED_ENGLISH_QUESTIONS_LEAKING_A_DISTINCTIVE_TERM = 10
_LEAKAGE_THRESHOLD = 0.50
# `evals/project-memory/README.md`, immediately above the table these figures come from: "147
# documents, the corpus as of `9591f91`". Both halves are asserted -- the revision, because it is
# what makes the number reproducible, and the count, because a revision that resolves to something
# other than the history the page describes would otherwise measure a stranger in silence.
_PUBLISHED_CORPUS_REVISION = "9591f91"
_PUBLISHED_CORPUS_DOCUMENTS = 147


def test_the_published_leakage_count_is_still_true() -> None:
    from leakage import TermStatistics, leakage  # noqa: PLC0415 -- path is set above

    toplevel = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)
    if toplevel.returncode != 0:
        pytest.skip("not a git checkout")
    repo = toplevel.stdout.strip()
    # A shallow clone -- the default of every CI checkout that has not asked otherwise -- simply
    # does not have this object, and cannot be made to produce the published corpus. That is an
    # absent measurement, not a failed one, so it skips and says which revision it wanted.
    if subprocess.run(["git", "cat-file", "-e", f"{_PUBLISHED_CORPUS_REVISION}^{{commit}}"],
                      cwd=repo, capture_output=True, timeout=60).returncode != 0:
        pytest.skip(f"{_PUBLISHED_CORPUS_REVISION} is not in this checkout (shallow clone?); the "
                    "published corpus cannot be derived, so the published figure cannot be checked")
    with tempfile.TemporaryDirectory() as workspace:
        built = subprocess.run(
            [sys.executable, str(GENERATOR), "--repo", repo, "--out", workspace,
             "--revision", _PUBLISHED_CORPUS_REVISION],
            capture_output=True, text=True, timeout=300)
        assert built.returncode == 0, built.stdout + built.stderr
        documents = {_SHA.sub(r"\1-", path.name): path.read_text(encoding="utf-8")
                     for path in Path(workspace).glob("*.md")}

    assert len(documents) == _PUBLISHED_CORPUS_DOCUMENTS, (
        f"README.md describes the corpus as of `{_PUBLISHED_CORPUS_REVISION}` as "
        f"{_PUBLISHED_CORPUS_DOCUMENTS} documents; that revision now yields {len(documents)}. The "
        "pin no longer resolves to the corpus the page describes -- a rewritten history, or the "
        "wrong sha -- so nothing below this line is measuring what the page published.")
    statistics = TermStatistics.over(documents.values())
    leaking = 0
    measured = 0
    for line in (EVALS / "decisions-en.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        target = case["ground_truth"]["must_include"][0]
        # The test above proves the ground truth exists at HEAD, which does not prove it existed at
        # the pin -- a question added later would name a document this older corpus never held.
        assert target in documents, f"{target} is not in the corpus as of {_PUBLISHED_CORPUS_REVISION}"
        measured += 1
        result = leakage(case["query"], documents[target], statistics)
        if result.peak is not None and result.peak >= _LEAKAGE_THRESHOLD:
            leaking += 1

    assert measured == 12, f"the English question set is no longer twelve questions but {measured}"
    assert leaking == _PUBLISHED_ENGLISH_QUESTIONS_LEAKING_A_DISTINCTIVE_TERM, (
        f"README.md says {_PUBLISHED_ENGLISH_QUESTIONS_LEAKING_A_DISTINCTIVE_TERM} of {measured} "
        f"English questions reach peak leakage {_LEAKAGE_THRESHOLD}; it is now {leaking}. Update "
        "the figure and the paragraph that reasons from it, in this commit."
    )
