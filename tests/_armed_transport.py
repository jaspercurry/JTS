# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Arm the ring transport for a commissioning-load test (#2412 Wave 6).

WHY IT EXISTS. `build_driver_commission_load_preflight`'s
`commissioning_transport_armed` gate (Wave 3) reads two reconciler-owned files
FRESH — fan-in's coupling and outputd's ACTIVE-endpoint marker — and a test
that declares neither has no `fanin.env` to read and a false marker. Off the
ring that costs nothing: the gate's ring branch is not
taken and both conjuncts stay `True`. On the ring it is the difference between
`status="loaded"` and `status="blocked"`.

**WHAT IT NOW AFFECTS, MEASURED ON THIS TREE.** Arming the marker moves the load
gate's two liveness conjuncts and **nothing else**. It does NOT move the
fresh-emit chooser: `jasper.output_topology.resolve_output_layout` answers the
ACTIVE ring for both marker values —

    marker false -> ('jts_ring_active_playback', 'outputd_active_lane')
    marker true  -> ('jts_ring_active_playback', 'outputd_active_lane')

— because #2285 P2 deleted case 2's marker read. The only mention of
`ring_active_endpoint_armed` left in that module is a past-tense comment
recording the deletion; there is no call. So the unconditional harness calls are
safe here, and they are installed: `_load` arms by default, and
`test_active_speaker_cli.py`'s and `test_sound_setup_commission.py`'s shared
envs call this directly.

WHAT IT DOES NOT DO. It does not make a graph coherent, protected, or audible —
Gate 1, the protection-while-audible evidence, and the ramp's seven checks all
still run untouched. It answers exactly the endpoint liveness question the
gate asks, and nothing else.
"""

from __future__ import annotations

from typing import Any


def arm_ring_transport(monkeypatch: Any) -> None:
    """Report outputd armed on the ACTIVE ring.

    The fan-in half needs no patch: an absent ``fanin.env`` already reads as
    ring-fed. Patched at the owning module rather than at the reader, because
    the preflight imports the name INSIDE the call, so the attribute is looked
    up per call and the patch is what the gate sees. The
    same shape the shipped ring-commissioning tests already use for the marker
    half.

    Deliberately NOT a pytest fixture: its call sites are plain helper functions
    rather than tests, and a fixture would have to be requested by every test
    that reaches them — which is the 59-edit outcome a shared helper exists to
    avoid.
    """
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: True,
    )
