"""Render the records of the 2026-10-07 unprompted consult measurement, twice.

The measurement ran two arms on the same Claude Code binary, each of 15 runs (three egui traps,
five repetitions): control at `baa6174`, treatment at `329f526`. Their records are in
`records/<arm>/runs.jsonl`; transcripts are not committed, so nothing here rescores a citation.

- `pre-registered/<arm>/`: rendered with the trap set as registered, the trap manifests at
  `329f526`, extracted from Git. Every run is in the headline, as pre-registered.
- `post-hoc/<arm>/`: rendered with the current trap set. It marks `egui-image-formats` as an
  undocumented decision (2026-10-08, after the runs), so its 5 runs per arm move to the
  undocumented-decision control section and the headline holds 10.

Each view keeps only `report.md`. `report.py` also writes `runs.json`, which repeats every record
`records/` already holds, so it is rendered in a scratch directory and left there. Both renders
are deterministic: re-running this script reproduces them byte for byte. The registered commit is
reachable from the tag `archive/agent-consult-measurement-2026-10-07`.

Usage:
    uv run python evals/agent_regression/results/consult-2026-10-07/render.py [--out <dir>]
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import shutil
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
EVALS_DIR = HERE.parents[2]
REPO_ROOT = EVALS_DIR.parent
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from agent_regression import report  # noqa: E402

ARMS = ("control", "treatment")
REGISTERED_COMMIT = "329f526eeceb91ce87bdda5a39f57c1e1cf89883"
REGISTERED_TAG = "archive/agent-consult-measurement-2026-10-07"
TRAPS_PATH = "evals/agent_regression/traps"


class RegisteredTrapsUnavailable(LookupError):
    """The registered trap set could not be read from Git."""


def extract_registered_traps(commit: str, destination: Path) -> Path:
    """Extract the trap manifests at `commit` into `destination` and return the trap directory."""
    try:
        archive = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", commit, TRAPS_PATH],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RegisteredTrapsUnavailable(f"git could not run: {exc}") from None
    if archive.returncode != 0:
        detail = archive.stderr.decode("utf-8", errors="replace").strip()
        raise RegisteredTrapsUnavailable(f"git archive {commit} failed: {detail}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")
    return destination / TRAPS_PATH


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the 2026-10-07 consult records, pre-registered and post hoc.")
    parser.add_argument("--out", type=Path, default=HERE, help="directory the two renders go into (default: here)")
    parser.add_argument(
        "--registered-commit",
        default=REGISTERED_COMMIT,
        help="commit whose trap manifests are the registered trap set (default: %(default)s)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out: Path = args.out
    with tempfile.TemporaryDirectory(prefix="bruriah-registered-traps-") as tmp:
        # Fail before writing anything: a half-rendered pair of views would read as complete.
        try:
            registered_traps = extract_registered_traps(args.registered_commit, Path(tmp))
        except RegisteredTrapsUnavailable as exc:
            print(
                f"error: cannot read the registered trap set: {exc}. Fetch it with "
                f"`git fetch origin tag {REGISTERED_TAG}`.",
                file=sys.stderr,
            )
            return 2
        scratch = Path(tmp) / "renders"
        views = {"pre-registered": registered_traps, "post-hoc": report.DEFAULT_TRAPS_DIR}
        for view, traps in views.items():
            for arm in ARMS:
                rendered = scratch / view / arm
                # `report.main` names its scratch output on stdout; this script reports its own.
                with contextlib.redirect_stdout(io.StringIO()):
                    code = report.main([str(HERE / "records" / arm), "--traps", str(traps), "--out", str(rendered)])
                if code != 0:
                    return code
                destination = out / view / arm
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(rendered / "report.md", destination / "report.md")
                print(f"{view}/{arm}/report.md rendered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
