"""Regression traps: the unit of the agent regression benchmark.

A trap is one directory holding `trap.yaml` (the pinned repository and commit, the task prompt,
the rejected alternative, the decision that rejected it, the budgets, and the second reader who
confirmed the prompt does not leak the answer) and `detect.py` (a pure, deterministic
`detect(tree, diff) -> Detection`). The loader refuses a trap whose prompt names the rejected
alternative or Bruriah: such a prompt measures instruction following, not memory.

Each trap also carries its citation cues: the identifiers that, in an agent's final message, mean
the agent cited the decision. Some are derived from `decision_ref`; `trap.yaml` may add more under
the optional `citation_cues` key, for identifiers the ref does not carry (the pull request that
merged a decision commit, a decision document path).
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

if TYPE_CHECKING:
    from agent_regression.detection import Detection

# The documented trap sources, in the order the task document lists them.
SOURCES = ("own-history", "leakcanary", "egui")

_STRING_KEYS = (
    "trap_id",
    "source",
    "repository",
    "commit",
    "prompt",
    "rejected_alternative",
    "decision_ref",
    "second_reader",
    "second_reader_date",
)
_BUDGET_KEYS = ("turn_budget", "time_budget_seconds")
_REQUIRED_KEYS = frozenset(_STRING_KEYS + _BUDGET_KEYS)
_OPTIONAL_KEYS = frozenset({"citation_cues"})

_COMMIT = re.compile(r"[0-9a-f]{40}")
# Trap ids become Markdown table cells and plan lines, so they stay plain.
_TRAP_ID = re.compile(r"[a-z0-9][a-z0-9._-]*")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_GITHUB_REF = re.compile(r"github:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)")
# A commit decision is cited by its sha abbreviated to git's default 7 characters. Matching
# accepts any prefix of the full sha at least this long, so the 7-character form is the only one
# a trap needs to store.
SHA_CUE_LENGTH = 7
# Shorter cues, and bare numbers without `#`, would match text that does not cite anything.
_MIN_CUE_LENGTH = 3


class TrapError(ValueError):
    """Raised when a trap directory is missing, malformed, or would not measure what it claims to."""


@dataclass(frozen=True)
class Trap:
    path: Path
    trap_id: str
    source: str
    repository: str
    commit: str
    prompt: str
    rejected_alternative: str
    decision_ref: str
    turn_budget: int
    time_budget_seconds: int
    second_reader: str
    second_reader_date: str
    # Derived cues first, then the manifest's, without duplicates. `load_trap` always fills it.
    citation_cues: tuple[str, ...] = ()


def _as_iso_date(manifest: Path, value: object) -> object:
    """Trap authors write `second_reader_date: 2026-09-26` unquoted, which YAML parses as a date;
    the trap carries the ISO string either way."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise TrapError(f"{manifest}: second_reader_date is not a valid date: {value!r}") from exc
        return value
    raise TrapError(f"{manifest}: second_reader_date must be an ISO date (YYYY-MM-DD), got {value!r}")


def derived_citation_cues(decision_ref: str) -> tuple[str, ...]:
    """A full commit sha gives its abbreviation; `github:owner/repo#N` gives `#N` and
    `owner/repo#N`. Any other ref, such as a decision document path, is cited as written."""
    if _COMMIT.fullmatch(decision_ref):
        return (decision_ref[:SHA_CUE_LENGTH],)
    github = _GITHUB_REF.fullmatch(decision_ref)
    if github:
        repository, number = github.groups()
        return (f"#{number}", f"{repository}#{number}")
    return (decision_ref,)


def _manifest_citation_cues(manifest: Path, value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TrapError(f"{manifest}: citation_cues must be a list of strings, got {value!r}")
    for cue in value:
        if not isinstance(cue, str) or not cue.strip():
            raise TrapError(f"{manifest}: citation_cues items must be non-empty strings, got {cue!r}")
        if len(cue.strip()) < _MIN_CUE_LENGTH:
            raise TrapError(f"{manifest}: citation_cues item {cue!r} is shorter than {_MIN_CUE_LENGTH} characters")
        if cue.strip().isdigit():
            raise TrapError(f"{manifest}: citation_cues item {cue!r} is a bare number; write it as '#{cue.strip()}'")
    return tuple(cue.strip() for cue in value)


def load_trap(path: Path) -> Trap:
    manifest = path / "trap.yaml"
    if not manifest.is_file():
        raise TrapError(f"{path}: trap.yaml not found")
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TrapError(f"{manifest}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise TrapError(f"{manifest}: must be a mapping of trap fields")

    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        raise TrapError(f"{manifest}: missing required key(s): {', '.join(missing)}")
    unknown = sorted(str(key) for key in data if key not in _REQUIRED_KEYS | _OPTIONAL_KEYS)
    if unknown:
        raise TrapError(f"{manifest}: unknown key(s): {', '.join(unknown)}")

    fields: dict[str, Any] = dict(data)
    manifest_cues = _manifest_citation_cues(manifest, fields.pop("citation_cues")) if "citation_cues" in fields else ()
    fields["second_reader_date"] = _as_iso_date(manifest, fields["second_reader_date"])
    for key in _STRING_KEYS:
        value = fields[key]
        if not isinstance(value, str) or not value.strip():
            raise TrapError(f"{manifest}: {key} must be a non-empty string, got {value!r}")
    for key in _BUDGET_KEYS:
        value = fields[key]
        # `bool` is an `int` subclass; `true` is not a budget.
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TrapError(f"{manifest}: {key} must be a positive integer, got {value!r}")

    if not _TRAP_ID.fullmatch(fields["trap_id"]):
        raise TrapError(f"{manifest}: trap_id must be lowercase letters, digits, '.', '_' or '-'")
    if fields["source"] not in SOURCES:
        raise TrapError(f"{manifest}: source must be one of {', '.join(SOURCES)}, got {fields['source']!r}")
    if not _COMMIT.fullmatch(fields["commit"]):
        raise TrapError(f"{manifest}: commit must be a full 40-character lowercase hex sha")

    prompt = fields["prompt"].lower()
    for leaked in (fields["rejected_alternative"], "bruriah"):
        if leaked.lower() in prompt:
            raise TrapError(
                f"{manifest}: prompt names {leaked!r}; a prompt that names it measures instruction following"
            )

    citation_cues = tuple(dict.fromkeys(derived_citation_cues(fields["decision_ref"]) + manifest_cues))
    if not citation_cues:
        raise TrapError(f"{manifest}: the decision has no citation cue")

    return Trap(path=path, **fields, citation_cues=citation_cues)


def load_traps(root: Path) -> tuple[Trap, ...]:
    """Every trap directly under `root`, sorted by trap id (not directory name) so the plan and
    the report order never depend on how directories were named."""
    if not root.is_dir():
        raise TrapError(f"traps directory does not exist: {root}")
    traps = [load_trap(child) for child in sorted(root.iterdir()) if (child / "trap.yaml").is_file()]
    seen: dict[str, Path] = {}
    for trap in traps:
        if trap.trap_id in seen:
            raise TrapError(f"duplicate trap_id {trap.trap_id!r} in {seen[trap.trap_id]} and {trap.path}")
        seen[trap.trap_id] = trap.path
    return tuple(sorted(traps, key=lambda trap: trap.trap_id))


def load_detector(trap: Trap) -> Callable[[Path, str], Detection]:
    """Import the trap's `detect.py` and return its `detect` function.

    Every trap ships a file named `detect.py`, so each is imported under a module name derived
    from its trap path: two traps' detectors never shadow each other in `sys.modules`. The module
    is registered there because dataclasses and similar machinery look their module up by name.
    """
    source = trap.path / "detect.py"
    if not source.is_file():
        raise TrapError(f"trap {trap.trap_id}: detect.py not found in {trap.path}")
    digest = hashlib.sha256(str(trap.path.resolve()).encode("utf-8")).hexdigest()[:16]
    module_name = f"agent_regression_detector_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        raise TrapError(f"trap {trap.trap_id}: cannot import detect.py from {trap.path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    detect = getattr(module, "detect", None)
    if not callable(detect):
        raise TrapError(f"trap {trap.trap_id}: detect.py defines no detect(tree, diff) function")
    return detect
