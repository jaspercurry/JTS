# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A spawned subprocess must import the tree under test, not the venv's.

141 test files shell out to something that imports ``jasper``. In a git
worktree the venv belongs to the MAIN checkout, so without this the editable
install's .pth finder resolves those imports against whatever branch the main
checkout happens to be sitting on — silently validating other code in a lane
whose job is to gate a merge. See the PYTHONPATH block in conftest.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pythonpath_leads_with_the_tree_under_test() -> None:
    entries = os.environ.get("PYTHONPATH", "").split(os.pathsep)

    assert entries and Path(entries[0]) == REPO_ROOT


def test_a_spawned_subprocess_imports_jasper_from_this_tree() -> None:
    """The property itself, proved through a real spawn.

    Asserted on the resolved file rather than on PYTHONPATH, so it still holds
    if the mechanism is ever changed to something other than the env var.
    """
    resolved = subprocess.run(
        [sys.executable, "-c", "import jasper; print(jasper.__file__)"],
        capture_output=True,
        text=True,
        check=True,
        cwd=os.path.dirname(REPO_ROOT),
    ).stdout.strip()

    assert Path(resolved).resolve().is_relative_to(REPO_ROOT)
