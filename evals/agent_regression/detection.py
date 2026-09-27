"""The detector contract: what a trap's `detect.py` returns, and the fixture check that decides
whether the benchmark may trust it.

The headline number is decided here, by code, never by a model. A detector that fires must say
which evidence fired (an import, a dependency line, a file, a symbol) so every fired detection in
a published run can be spot-checked by hand. Completion is a separate signal because avoiding the
regression by doing nothing is not a win.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_regression.traps import Trap, load_detector

# Each fixture direction and whether the detector must fire on it.
_FIXTURES = (("regressed", True), ("clean", False))


class DetectionError(ValueError):
    """Raised when a detection breaks the contract or a detector fails its own fixtures."""


@dataclass(frozen=True)
class Detection:
    regressed: bool
    evidence: tuple[str, ...]
    completed: bool


def validate_detection(detection: object) -> Detection:
    """Return `detection` unchanged when it honours the contract; raise `DetectionError` otherwise."""
    if not isinstance(detection, Detection):
        raise DetectionError(f"a detector must return a Detection, got {type(detection).__name__}")
    if not isinstance(detection.regressed, bool) or not isinstance(detection.completed, bool):
        raise DetectionError("Detection.regressed and Detection.completed must be booleans")
    if not isinstance(detection.evidence, tuple) or not all(isinstance(item, str) for item in detection.evidence):
        raise DetectionError("Detection.evidence must be a tuple of strings")
    if detection.regressed and not detection.evidence:
        raise DetectionError("a detection that fires must name the evidence that fired")
    return detection


def check_trap_fixtures(trap: Trap) -> None:
    """Run the trap's detector on its `fixtures/regressed/` and `fixtures/clean/` trees.

    Each fixture directory is passed as the tree, with the text of its `diff.patch` as the diff.
    The detector must fire on the regressed fixture and stay silent on the clean one; a detector
    nobody exercised in both directions is not one the benchmark may trust.
    """
    detect = load_detector(trap)
    for direction, should_fire in _FIXTURES:
        tree = trap.path / "fixtures" / direction
        if not tree.is_dir():
            raise DetectionError(f"trap {trap.trap_id}: the {direction} fixture directory is missing")
        patch = tree / "diff.patch"
        if not patch.is_file():
            raise DetectionError(f"trap {trap.trap_id}: the {direction} fixture has no diff.patch")
        detection = validate_detection(detect(tree, patch.read_text(encoding="utf-8")))
        if detection.regressed is not should_fire:
            verb = "did not fire on" if should_fire else "fired on"
            raise DetectionError(f"trap {trap.trap_id}: the detector {verb} the {direction} fixture")
