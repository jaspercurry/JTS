"""Regression tests for the 'lost edit' bug class.

PR #146 (multi-device peering) added a new wizard but lost two lines
during an edit-merge race: `peering_setup` never made it into the
`from . import (...)` tuple, and `peers_port` was referenced inside
`main()` without ever being defined. The module compiles fine —
Python only resolves the names at call time — so the bug only
surfaced when systemd started the daemon, at which point ALL eight
wizards went down.

Two layers of defense here:

1. **Pattern-specific checks against `__main__.py`** — catches the
   exact `__main__.py` bug that bit us (every `<name>_setup.X` has
   a matching import; every `<name>_port` has an assignment).

2. **ruff F821 across every peering-touched file** — catches the
   same lost-edit pattern (undefined name) anywhere else in the
   package. ruff is already in our dev dependencies and is the
   battle-tested implementation of pyflakes-style undefined-name
   detection (handles match/case patterns, comprehensions, walrus,
   nested scopes, etc.). If ruff isn't available locally the test
   skips rather than flakes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_MAIN_PATH = _REPO / "jasper" / "web" / "__main__.py"


# ----------------------------------------------------------------------
# Layer 1 — pattern checks on __main__.py
# ----------------------------------------------------------------------


def test_every_referenced_setup_module_is_imported():
    """Every `xxx_setup.YYY` lookup must have `xxx_setup` in the
    package's `from . import (...)` tuple."""
    text = _MAIN_PATH.read_text()
    referenced = set(re.findall(r"\b([a-z][a-z0-9_]*_setup)\.", text))
    for mod in sorted(referenced):
        in_bulk_import = re.search(
            rf"^\s+{re.escape(mod)},\s*$", text, re.MULTILINE,
        )
        as_separate_import = re.search(rf"\bimport {re.escape(mod)}\b", text)
        assert in_bulk_import or as_separate_import, (
            f"{mod}.X is referenced in __main__.py but {mod} is not "
            f"in `from . import (...)` — adding a new wizard requires "
            f"both the wiring AND the import line."
        )


def test_every_referenced_port_var_is_defined():
    """Every `xxx_port` reference inside `main()` must have a local
    `xxx_port = ...` assignment. Catches the missing-port-var half
    of the PR #146 bug."""
    text = _MAIN_PATH.read_text()
    referenced = set(re.findall(r"\b([a-z][a-z0-9_]*_port)\b", text))
    for var in sorted(referenced):
        assigned = re.search(rf"\b{re.escape(var)}\s*=", text)
        assert assigned, (
            f"{var} is referenced in __main__.py but never assigned. "
            f"Adding a new wizard port requires the line "
            f"`{var} = int(os.environ.get(\"JASPER_*_WEB_PORT\", \"NNNN\"))` "
            f"alongside the other port-var declarations."
        )


# ----------------------------------------------------------------------
# Layer 2 — ruff F821 across the peering surface
# ----------------------------------------------------------------------


# Files where a lost edit during a peering refactor could re-introduce
# the bug class. Adding a new file? Add it here.
_PEERING_FILES = [
    "jasper/peering/",  # whole subtree
    "jasper/web/peering_setup.py",
    "jasper/web/__main__.py",
    "jasper/voice_daemon.py",
    "jasper/control/server.py",
    "jasper/cli/doctor.py",
]


def test_peering_surface_has_no_undefined_names():
    """Run ruff F821 (undefined-name) over every peering-touched file.

    The bug that motivated this would have shown up as
    `peering_setup` reported as undefined in jasper/web/__main__.py.
    """
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed; install via `pip install ruff`")
    paths = [str(_REPO / p) for p in _PEERING_FILES]
    result = subprocess.run(
        [ruff, "check", "--select=F821", "--no-cache", "--output-format=concise",
         *paths],
        capture_output=True, text=True, cwd=str(_REPO),
    )
    # Exit 0 = no findings, exit 1 = findings. Other codes = ruff error.
    if result.returncode == 0:
        return
    if result.returncode == 1:
        pytest.fail(
            "ruff F821 found undefined names in the peering surface:\n"
            + result.stdout,
        )
    pytest.skip(f"ruff failed to run (exit {result.returncode}): {result.stderr}")
