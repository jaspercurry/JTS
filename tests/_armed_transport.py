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

**HISTORY — why this module exists at all, and why it shipped separately.**
#2412's design asked whether the arming could be installed unconditionally in the
shared harnesses as a green-on-main no-op. Measured on #2412's own tree the
answer was **green but NOT a no-op**: the chooser still read the marker there, so
arming flipped it from `outputd_active_content_playback` to the ring and would
have silently moved every carried test off the snd-aloop path it was written to
cover — a coverage loss §3.4 rule 1 forbids. That is why the call sites were
routed to **#2285 P2** and why this module landed first, proven by the
ring-polarity tests that opt into it, rather than shipping helper and call sites
together with neither proved. **P2 has since landed and closed that objection**:
with the snd-aloop ACTIVE endpoint retired there is no second transport left to
lose coverage of, so the arming supplies only the missing liveness. The
historical asymmetry is preserved here because it is the reason for the module's
shape, not because it still describes the code.

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
