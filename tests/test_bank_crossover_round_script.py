# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins scripts/bank-crossover-round.sh's non-empty-<dest-dir> refusal.

This is the ONE outcome the script can produce with no Pi, no SSH, and no
network reachable: refusing to bank into a directory that already has
something in it (exit 4). The check runs before the script's first
`remote()` call, so it is safe to exercise as a real subprocess here. The
rest of the contract (exit 0/3, both gated on a live Pi round-trip) is not
exercised by this file.

Exit 4 exists at all because exit 1 was ambiguous WHEN IT WAS ADDED: the
script then graded the round with the capture-integrity checker, which
returns 1 for "nothing to check yet", and a caller scripting a retry loop
that hits a failed bank and retries into the SAME destination needs to tell
"this destination is unusable" apart from "there was nothing to grade"
without parsing stderr. The capture-dump ring that grading read is gone and
the script no longer calls the checker, so 1 is now only bash's own failure
-- but 4 stays, because the retry-loop caller still needs the distinction
from a bank that failed for any other reason. The literal integer is what
is pinned here, not a symbol -- a caller's `$?` is an integer, and a future
renumbering that quietly moved this refusal back onto 1 (or onto 3, next to
the incomplete-bank exit) would not be caught by anything that only asserts
"not zero".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bank-crossover-round.sh"


def _run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # The script targets a speaker, so _lib.sh requires one to be named
    # (#3498) — nothing here reaches the network, so an unroutable name
    # keeps the refusal under test the dest-dir one.
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env if env is not None else {**os.environ, "PI_HOST": "jts9.invalid"},
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


def test_sourcing_the_shared_lib_survives_a_pre_set_pi_host(tmp_path):
    """scripts/_lib.sh is sourced INTO this script's own namespace.

    This script runs under `set -u`, so any name the library leaves unset
    out from under it is an unbound-variable exit 1 -- which callers used to
    read as the capture-integrity checker's benign "nothing to check"
    verdict, banking nothing and calling it success. That overload is gone
    (`run-crossover-round.py` now aborts on any non-zero bank rc), but the
    silent-source failure this pin catches is not: reaching the exit-4
    refusal above proves the source completed.
    """
    dest = tmp_path / "already-banked"
    dest.mkdir()
    (dest / "state.json").write_text("{}")

    proc = _run(str(dest), env={**os.environ, "PI_HOST": "jts9.local"})

    assert proc.returncode == 4


def test_non_empty_dest_dir_refusal_is_distinct_from_a_missing_argument():
    # Bash's own `${1:?…}` exit code for a missing required argument is 1
    # -- unchanged, and deliberately not moved onto 4 alongside the
    # non-empty-dest-dir refusal (a one-time invocation mistake is not the
    # retry-loop collision exit 4 exists to resolve). The two refusals
    # must stay numerically distinct from each other.
    missing_arg = _run()
    assert missing_arg.returncode == 1
