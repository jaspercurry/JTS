# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Arm the ring transport for a commissioning-load test (#2412 Wave 6).

WHY IT EXISTS. `build_driver_commission_load_preflight`'s
`commissioning_transport_armed` gate (Wave 3) reads two reconciler-owned files
FRESH — fan-in's coupling and outputd's ACTIVE-endpoint marker — and both
readers fail SAFE, so a test that declares neither reads `loopback` with the
marker false. Off the ring that costs nothing: the gate's ring branch is not
taken and both conjuncts stay `True`. On the ring it is the difference between
`status="loaded"` and `status="blocked"`.

**IT IS NOT AN INERT FIXTURE, AND THAT IS THE MEASURED FINDING.** #2412's design
asked whether an armed-transport fixture could be installed unconditionally in
the shared commissioning harnesses as a green-on-main no-op. Run on this tree,
the answer is **green but NOT a no-op**: `ring_active_endpoint_armed` is read by
`jasper.output_topology.resolve_output_layout` as well as by the gate, so arming
the marker also flips the fresh-emit chooser's answer for the whole box —

    marker false -> ('outputd_active_content_playback', 'outputd_active_lane')
    marker true  -> ('jts_ring_active_playback',        'outputd_active_lane')

— which would silently move every test funnelling through those harnesses off
the snd-aloop path it was written to cover. Green, because Waves 1-3 make ring
commissioning work; not a no-op, because the covered transport changes. So the
unconditional harness calls belong to **#2285-P2**, where the chooser answers
the ring anyway and the arming supplies only the missing liveness. This module
lands here, PROVEN by the ring-polarity tests that opt into it, so P2 adds three
call sites to a helper that already has a passing control rather than shipping
both at once.

WHAT IT DOES NOT DO. It does not make a graph coherent, protected, or audible —
Gate 1, the protection-while-audible evidence, and the ramp's seven checks all
still run untouched. It answers exactly the two liveness questions the gate
asks, and nothing else.
"""

from __future__ import annotations

from typing import Any

from jasper.fanin_coupling import COUPLING_SHM_RING


def arm_ring_transport(monkeypatch: Any) -> None:
    """Report fan-in coupled to Ring A and outputd armed on the ACTIVE ring.

    Patched at the two owning modules rather than at the reader, because the
    preflight imports both names INSIDE the call (`jasper.fanin_coupling` at the
    top of the gate, `jasper.fanin.coupling_reconcile` inside the ring branch),
    so the attribute is looked up per call and the patch is what the gate sees.
    The same shape the shipped ring-commissioning tests already use for the
    marker half.

    Deliberately NOT a pytest fixture: its call sites are plain helper functions
    rather than tests, and a fixture would have to be requested by every test
    that reaches them — which is the 59-edit outcome a shared helper exists to
    avoid.
    """
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.read_persisted_coupling",
        lambda path=None: COUPLING_SHM_RING,
    )
    monkeypatch.setattr(
        "jasper.fanin_coupling.ring_active_endpoint_armed",
        lambda env=None: True,
    )
