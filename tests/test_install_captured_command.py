# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy/install.sh"


def _run_helper(command: str) -> subprocess.CompletedProcess[str]:
    script = f"""
source "{INSTALL}"
captured=""
if run_captured_command captured bash -c {command!r}; then
    status=0
else
    status=$?
fi
printf 'STATUS=%s\\nCAPTURED=%s\\n' "$status" "$captured"
"""
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_captured_command_replays_combined_output_and_returns_success():
    result = _run_helper("printf out; printf err >&2")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "outerr\nSTATUS=0\nCAPTURED=outerr\n"


def test_captured_command_replays_combined_output_and_returns_failure():
    result = _run_helper("printf bad; printf news >&2; exit 7")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "badnews\nSTATUS=1\nCAPTURED=badnews\n"
