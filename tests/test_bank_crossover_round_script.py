# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins scripts/bank-crossover-round.sh's non-empty-<dest-dir> refusal.

This is the ONE outcome the script can produce with no Pi, no SSH, and no
network reachable: refusing to bank into a directory that already has
something in it (exit 4). The check runs before the script's first
`remote()` call, so it is safe to exercise as a real subprocess here. The
rest of the contract (exit 0/1/2/3, all gated on a live Pi round-trip) is
not exercised by this file.

Exit 4 exists at all because exit 1 was ambiguous: the capture-integrity
checker also returns 1 for "nothing to check yet" (jasper.audio_measurement.
capture_integrity), and a caller scripting a retry loop that hits a failed
bank and retries into the SAME destination needs to tell "this destination
is unusable" apart from "there was nothing to grade" without parsing
stderr. The literal integer is what is pinned here, not a symbol -- a
caller's `$?` is an integer, and a future renumbering that quietly moved
this refusal back onto 1 (or onto 3, next to the incomplete-bank exit)
would not be caught by anything that only asserts "not zero".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bank-crossover-round.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_syntax_is_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=10)


def test_refuses_a_non_empty_dest_dir_with_the_literal_exit_code_four(tmp_path):
    dest = tmp_path / "already-banked"
    dest.mkdir()
    (dest / "state.json").write_text("{}")

    proc = _run(str(dest))

    # Decoupled from any symbol on purpose -- see module docstring. `4`,
    # not `!= 0`, is the actual promise.
    assert proc.returncode == 4
    assert "already exists and is not empty" in proc.stderr

    # A refused run must not touch what was already in the directory.
    assert (dest / "state.json").read_text() == "{}"


def test_non_empty_dest_dir_refusal_is_distinct_from_a_missing_argument():
    # Bash's own `${1:?…}` exit code for a missing required argument is 1
    # -- unchanged, and deliberately not moved onto 4 alongside the
    # non-empty-dest-dir refusal (a one-time invocation mistake is not the
    # retry-loop collision exit 4 exists to resolve). The two refusals
    # must stay numerically distinct from each other.
    missing_arg = _run()
    assert missing_arg.returncode == 1
