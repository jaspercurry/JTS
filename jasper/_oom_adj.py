# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single source of truth for OOMScoreAdjust values per critical
daemon, shared between Python (jasper.cli.doctor) and bash
(deploy/install.sh's migrate_memory_resilience step).

EDITING HERE CHANGES:
  - jasper-doctor's drift check (uses EXPECTED)
  - install.sh's live-write step (uses INSTALL_LIVE_WRITE)
  - The .service unit files MUST be updated separately to match —
    these constants don't write the unit files. systemctl reads
    OOMScoreAdjust= from the unit file at process start.

Don't add operator-tuning knobs here. These values are weighted
priorities calibrated for JTS's specific daemon set. Forks of the
project should rename the units and re-weight; they shouldn't tune
JTS's numbers per-deployment.

See docs/HANDOFF-resilience.md "Memory-pressure resilience (Stage 1)"
for the rationale on each value.
"""
from __future__ import annotations


# All daemons whose OOMScoreAdjust we verify in jasper-doctor.
# Includes ssh so drift in the recovery-path bias surfaces in
# jasper-doctor. Keep it killable: SSH-launched diagnostics inherit
# this value, so -1000 would make arbitrary remote work immortal.
EXPECTED: dict[str, int] = {
    "jasper-outputd": -950,     # final DAC owner; silence if killed
    "jasper-camilla": -900,     # silence = worst UX
    "jasper-fanin": -800,       # renderer audio convergence point
    "jasper-aec-bridge": -700,  # real-time mic processing
    "jasper-control": -600,     # recovery surface (HTTP dashboard)
    "jasper-usbsink": -600,     # optional USB-audio intent/liveness owner
    "jasper-voice": -500,       # largest blast radius (LLM session)
    "jasper-camilla-crossover": -500,  # reconciler-gated active crossover
    "nginx": -450,              # management front door; recoverable
    "jasper-mux": -300,         # transient-graceful (latest-source-wins)
    "jasper-input": -300,       # direct USB still works without bridge
    "jasper-wiim-remote-mic": -300,  # optional accessory; normal mic remains
    "jasper-snapclient": -300,  # grouping reconcile can restart
    "jasper-snapserver": -300,  # grouping reconcile can restart
    "ssh": -250,                # recovery path; moderately protected
    "jasper-usbsink-volume": 100,  # optional observer; prefer killing first
}


# Values install.sh actively live-writes to /proc/PID/oom_score_adj
# during deploy. This includes ssh now that JTS owns a drop-in for the
# recovery-path bias: live-writing the sshd listener makes future SSH
# sessions inherit the killable -250 value without restarting sshd.
INSTALL_LIVE_WRITE: dict[str, int] = dict(EXPECTED)
