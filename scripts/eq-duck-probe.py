# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Trace CamillaDSP's main fader while EQ gestures are made on ``/eq/``.

Runs ON the Pi, inside the bounded diagnostic lane:

    bash scripts/pi-run-diagnostic.sh -- /opt/jasper/.venv/bin/python - \\
        < scripts/eq-duck-probe.py

Make every gesture under test on ``/eq/`` while it runs (default 60 s). It
prints one line per fader movement and a verdict: the deepest excursion from
the starting fader in dB. The EQ is silent only when that is 0.00 — a ducked
swap shows as a `camilla.GRAPH_SWAP_DUCK_DB` dip (see ADR-0211). Silent to
the room: it only reads.
"""

from __future__ import annotations

import os
import sys
import time

from camilladsp import CamillaClient

DURATION_S = float(os.environ.get("EQ_DUCK_PROBE_S", "60"))
PERIOD_S = 0.05  # a duck holds the fader down for >0.45 s; 20 Hz sees it many times

client = CamillaClient(
    os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
)
client.connect()

start = client.volume.main_volume()
last = start
deepest = 0.0
t0 = time.monotonic()
print(f"fader {start:+.2f} dB; watching for {DURATION_S:.0f} s", flush=True)
while time.monotonic() - t0 < DURATION_S:
    now = client.volume.main_volume()
    if now != last:
        print(f"t={time.monotonic() - t0:6.2f}s fader {now:+.2f} dB", flush=True)
        deepest = max(deepest, abs(now - start))
        last = now
    time.sleep(PERIOD_S)

print(f"deepest excursion {deepest:.2f} dB")
sys.exit(0 if deepest == 0.0 else 1)
