# The external question set has to stay what this page says it is.
#
# `evals/project-memory/README.md` reasons from three facts about it: that it holds 154 questions,
# that every one came from an issue somebody other than the commit author filed, and that the 24
# excluded are published with the rule that excluded each. Those are the claims a reader uses to
# decide how much the 0.266 is worth, and this repository has already shipped one release whose
# front page carried a count that had been wrong for a version.
#
# What CANNOT be asserted here, and is worth saying rather than quietly omitting: that the ground
# truth still exists. The corpus is `square/leakcanary`'s history, which is not in this repository
# and which CI has no business cloning. `test_project_memory_eval.py` can check the internal set
# against a corpus it can rebuild; this one can only check that the set is internally coherent and
# still says what the prose claims.
from __future__ import annotations

import json
import re
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "evals" / "project-memory"
QUESTIONS = EVALS / "leakcanary-issues.jsonl"
EXCLUDED = EVALS / "leakcanary-excluded.jsonl"

_PUBLISHED_QUESTIONS = 153
_PUBLISHED_EXCLUSIONS = 24
_GROUND_TRUTH = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md$")


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_published_counts_are_still_true() -> None:
    questions, excluded = _load(QUESTIONS), _load(EXCLUDED)
    assert len(questions) == _PUBLISHED_QUESTIONS, (
        f"README.md says {_PUBLISHED_QUESTIONS} questions and the file holds {len(questions)}; "
        "update the figure and the paragraphs that reason from it, in this commit")
    assert len(excluded) == _PUBLISHED_EXCLUSIONS


def test_every_question_carries_the_provenance_its_independence_rests_on() -> None:
    # The whole claim of this set is that somebody else asked. A case without both names recorded
    # cannot be audited by a reader, which makes it indistinguishable from a self-authored one.
    for case in _load(QUESTIONS):
        provenance = case["provenance"]
        assert provenance["issue_author"] and provenance["commit_author"]
        assert provenance["issue_author"] != provenance["commit_author"], case["id"]
        assert provenance["issue"] and provenance["commit"]


def test_ground_truth_is_recorded_without_a_sha() -> None:
    # A sha does not survive a history rewrite, and the eval is meant to outlive one -- the same
    # convention the internal set follows, restated here because a new file is where it gets lost.
    for case in _load(QUESTIONS):
        names = case["ground_truth"]["must_include"]
        assert len(names) == 1
        assert _GROUND_TRUTH.match(names[0]), names[0]


def test_ids_are_unique_and_the_two_files_do_not_overlap() -> None:
    questions = {case["id"] for case in _load(QUESTIONS)}
    excluded = {case["id"] for case in _load(EXCLUDED)}
    assert len(questions) == _PUBLISHED_QUESTIONS  # no duplicate ids collapsing the set
    assert not (questions & excluded), "a question cannot be both scored and excluded"


def test_every_exclusion_names_the_rule_that_excluded_it() -> None:
    # Publishing the rejects without their reason would be a list nobody can check.
    allowed = {"too_short", "stack_trace", "release_note_only", "generic_no_subject"}
    for case in _load(EXCLUDED):
        assert case["excluded_by"], case["id"]
        assert set(case["excluded_by"]) <= allowed, case["excluded_by"]
