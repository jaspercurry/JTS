# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared pieces for driving scripts/test-fast in a scratch repo and
recording what it selects, rather than letting it actually run pytest.

Used by test_build_and_ci_contracts.py and test_test_lane_tool_resolution.py
so the argv-recording stand-in and its env wiring live in exactly one place.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

TRUE_BIN = shutil.which("true") or "/usr/bin/true"

# The stand-in's own `SystemExit(5 if ...)`: pytest itself exits 5 for
# "no tests collected", which is what a genuine `--last-failed` call with
# nothing cached would report, and scripts/test-fast's own last-failed
# handling depends on seeing that real pytest exit code.
RECORDING_PYTEST_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "with open(os.environ['PYTEST_CALLS'], 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    "raise SystemExit(5 if '--last-failed' in sys.argv else 0)\n"
)


def write_recording_pytest(path: Path) -> Path:
    """Writes the argv-recording pytest stand-in to `path`, made executable."""

    path.write_text(RECORDING_PYTEST_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path


def lane_env(pytest_path: Path, calls_path: Path) -> dict[str, str]:
    """Env for a scripts/test-fast run using the recording pytest above.

    RUFF points at the real `true` binary rather than a hand-rolled stand-in
    script -- one fewer file to write per caller, and it always exists.
    TEST_BASE is a ref that can never resolve, so the lane's own base_ref
    diff is a no-op and only the scratch repo's working-tree/untracked state
    (which callers control directly) decides what counts as "changed".
    """

    return {
        **os.environ,
        "PYTEST": str(pytest_path),
        "PYTEST_CALLS": str(calls_path),
        "RUFF": TRUE_BIN,
        "TEST_BASE": "missing-base",
    }
