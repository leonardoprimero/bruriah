from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from bruriah.cli import bruriah_main
from bruriah.demo import run_demo


def test_run_demo_non_interactive():
    buf = io.StringIO()
    exit_code = run_demo(interactive=False, use_color=False, stdout=buf)
    assert exit_code == 0
    output = buf.getvalue()
    assert "Step 1: Storing Initial Architectural Decision" in output
    assert "Step 2: Agent Proposes Discarded Architecture" in output
    assert "🛑 VERDICT: repeat_of_rejected_architecture" in output
    assert "Step 3: Premise Invalidation & Re-evaluation" in output
    assert "🔄 VERDICT: premise_changed_requires_reevaluation" in output
    assert "Demo completed successfully!" in output



def test_demo_cli_dispatch():
    with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        code = bruriah_main(["demo", "--non-interactive", "--no-color"])
        assert code == 0
        output = mock_stdout.getvalue()
        assert "Step 1: Storing Initial Architectural Decision" in output
        assert "repeat_of_rejected_architecture" in output
        assert "premise_changed_requires_reevaluation" in output
