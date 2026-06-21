# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioural pins for scripts/multiroom-spike.sh (lab harness).

The spike harness needs real Pis + snapcast to do anything useful, so
these tests only exercise the laptop-side argument layer. The pin that
matters: ``--reference-ethernet`` must actually be consumed. The flag
was documented (script header + multiroom-spike-measure.py both tell
the operator to pass it) but for a while was parsed into a variable
nothing read — an operator's Ethernet best-case run silently overwrote
the WiFi cells in the shared results dir. It now redirects RESULTS_DIR
to a ``-ethernet-reference`` suffix and logs the redirect.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent.parent / "scripts" / "multiroom-spike.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_no_action_prints_usage_and_exits_2():
    result = _run()
    assert result.returncode == 2
    assert "multiroom-spike.sh" in result.stderr
    # No reference run was requested; the redirect must not fire.
    assert "ethernet-reference" not in result.stderr


def test_reference_ethernet_flag_redirects_results_dir():
    """The flag's observable contract: results are rerouted to a
    dedicated ``<results>-ethernet-reference`` dir (announced on
    stderr) so the best-case Ethernet line can't clobber WiFi cells."""
    result = _run("--reference-ethernet")
    # Still no action given -> usage + exit 2, but the redirect runs
    # (and is announced) before the action check.
    assert result.returncode == 2
    assert "ethernet-reference" in result.stderr
    assert "REFERENCE-ETHERNET run" in result.stderr
