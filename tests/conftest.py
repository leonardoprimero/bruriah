"""Availability of the private resources some tests need.

A handful of tests verify this project against the author's own live corpus and the legacy engine it
replaced: that the real vault reindexes reproducibly, that the legacy database is never mutated,
that the recovery baseline still matches. They are genuinely valuable *here* and meaningless
anywhere else, because the resources they assert about are private and are not distributed.

Rather than delete them or let a fresh clone fail on them, they skip when the resource is absent.
A clone that has never seen the vault runs the whole product suite green; a checkout that has it
runs the migration guarantees too. `pytest -rs` lists what was skipped, so the difference is visible
rather than silent.
"""
from __future__ import annotations

import os
import sqlite3
import traceback
from pathlib import Path

import pytest

# Owner-only file modes are a POSIX guarantee that Windows does not express: `os.chmod` there only
# toggles a read-only attribute, so every site that narrows permissions is already guarded by
# `os.name == "posix"` and correctly does nothing. These tests assert the guarantee itself, so on
# Windows there is nothing to assert -- but passing them silently would claim a protection that was
# never applied. Skipping says so, `pytest -rs` lists it, and `bruriah doctor` reports the same fact
# to users at runtime. What actually protects the data there is the user profile directory's
# inherited ACL, which is real but is not something this process verifies.
requires_posix_permissions = pytest.mark.skipif(
    os.name != "posix",
    reason="owner-only file modes are a POSIX guarantee; see `doctor`'s owner_only_file_modes",
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_ROOT = Path(__file__).resolve().parents[1]

VAULT_ROOT = REPOSITORY_ROOT / "Cerebro-IA"
LEGACY_DATABASE = RETRIEVAL_ROOT / "cerebro.db"
CORPUS_POLICY = RETRIEVAL_ROOT / "corpus-policy.yaml"
BASELINE_SCRIPT = RETRIEVAL_ROOT / "scripts" / "verify_legacy_baseline.py"
BASELINE_RECORD = RETRIEVAL_ROOT / "recovery" / "legacy-baseline-v1.json"
LEGACY_ENGINE = RETRIEVAL_ROOT / "cerebro.py"

requires_vault = pytest.mark.skipif(
    not VAULT_ROOT.is_dir() or not CORPUS_POLICY.is_file(),
    reason="the author's private corpus is not part of this checkout",
)
requires_legacy_database = pytest.mark.skipif(
    not LEGACY_DATABASE.is_file(),
    reason="the legacy cerebro.db is not part of this checkout",
)
# The script alone is not enough to run: it imports the legacy engine, opens the legacy database
# and compares against a recorded baseline, and only the script is distributed. Guarding on the
# script's presence made a fresh clone FAIL rather than skip -- the guard has to name every
# resource the work actually touches, not the one that happens to be easiest to test for.
requires_baseline_script = pytest.mark.skipif(
    not (BASELINE_SCRIPT.is_file() and BASELINE_RECORD.is_file()
         and LEGACY_ENGINE.is_file() and LEGACY_DATABASE.is_file()),
    reason="the legacy recovery baseline is not part of this checkout",
)


# An unclosed SQLite connection is not cosmetic in this package. On POSIX it is a descriptor nobody
# notices, because unlink succeeds against an open file. On Windows the same connection makes the
# file undeletable, so a leak in `promote_candidate` surfaced as `index-prune` failing WinError 32
# on three CI jobs and as nothing at all on two platforms and every local run. Thirteen more were
# in the tests themselves, and the noise they made is what a real one hid in.
#
# `filterwarnings` cannot catch this and was tried: the ResourceWarning is raised by the garbage
# collector, outside the context those filters apply in, so neither `error::ResourceWarning` nor
# `error::pytest.PytestUnraisableExceptionWarning` fails a test that deliberately leaks one.
#
# So the connection is tracked instead of the warning. Closure is recorded from `close()` itself,
# which means a connection the collector happened to reclaim still counts as leaked -- that is the
# intent, not an approximation. Relying on collection IS the defect: it is what makes the failure
# depend on when the collector runs, which is why this one reached CI as a Windows-only mystery.
@pytest.fixture(autouse=True)
def no_connection_outlives_its_test(monkeypatch):
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []
    closed: set[int] = set()
    origins: dict[int, str] = {}

    def tracking_connect(*args, **kwargs):
        base = kwargs.pop("factory", sqlite3.Connection)
        # Subclassed per call so a caller's own factory is honoured rather than replaced.
        tracked = type("TrackedConnection", (base,), {
            "close": lambda self: (closed.add(id(self)), super(tracked, self).close())[1]
        })
        connection = real_connect(*args, factory=tracked, **kwargs)
        opened.append(connection)
        # [-1] is this wrapper and [-2] is whoever called `sqlite3.connect`. Taking the oldest of
        # three frames instead named `_pytest/python.py`, which is true and useless.
        caller = traceback.extract_stack(limit=2)[0]
        origins[id(connection)] = f"{caller.filename}:{caller.lineno}"
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    yield
    leaked = [connection for connection in opened if id(connection) not in closed]
    where = "\n".join(f"  opened at {origins[id(item)]}" for item in leaked)
    for connection in leaked:
        connection.close()   # do not leave the next test to inherit this one's handles
    assert not leaked, (
        f"{len(leaked)} SQLite connection(s) were never closed:\n{where}\n"
        "Use `contextlib.closing`; a bare `with connection:` commits or rolls back and leaves the "
        "handle open, which is undetectable on POSIX and fatal to unlink on Windows."
    )
