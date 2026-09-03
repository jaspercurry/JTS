# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""SSOT for the room-correction band ceiling and the gated spec's lower edge.

**This module is the single source of truth for the room layer's upper band
edge.** Before it existed the "350 Hz cap" was ten independent float literals
spread across the PEQ designer, the strategy table, the session config, the
acceptance ladder, the confidence report, the advisor gate, the repeatability
evidence, the acoustic-quality re-clamp, and the shared deviation metric —
with no shared constant, no cross-import, and no test pinning the relationship
to the speaker layer's spec edge (issue #1787). Moving the boundary meant
finding and editing all ten by hand, and two of them
(:func:`jasper.correction.acoustic_quality.repeatability_from_arrays`'s
``min(350, peq_f_high)`` re-clamp and
:func:`jasper.audio_measurement.analysis.deviation_metrics`) would have
silently capped a raised ceiling rather than failing loudly.

Why this module lives in ``audio_measurement`` and not ``correction``:
``correction`` and ``active_speaker`` import **each other** (for example
``active_speaker.linearization_fit`` imports ``correction.peq``, while
``correction.runtime_safety`` imports ``active_speaker.runtime_contract``), so
neither one is "below" the other and homing a shared constant in either would
be an arbitrary pick that some consumer has to reach sideways for.
``audio_measurement`` is different, and this is the precise property the
placement rests on: **it is imported by both and imports neither** — it has
zero imports of ``jasper.correction`` and zero of ``jasper.active_speaker``.
That makes it the one package all three consumers (including
``audio_measurement.analysis`` itself, which owns one of the routed sites) can
read the boundary from without any new cross-package edge.
``tests/test_correction_boundary_ssot.py`` pins that invariant, because the
placement argument is only as good as it stays true.

The two edges, and the relation between them
--------------------------------------------
Two independently-derived frequencies meet in the 250–350 Hz region, and the
whole point of this module is that they are held side by side with their
relation written down once:

* :data:`GATED_SPEC_LOWER_EDGE_HZ` — the **speaker** layer's spec floor. Below
  it the gated (anechoic-ish, quasi-free-field) measurement of this specific
  commissioned speaker has no graded authority, because the gate window that
  rejects room reflections also destroys low-frequency resolution.
  :data:`jasper.active_speaker.flat_spec.SPEC_BANDS` routes its first band's
  lower edge here rather than re-declaring it.
* :data:`ROOM_BOUNDARY_DEFAULT_HZ` — the **room** layer's ceiling: the highest
  frequency at which room correction places filters. Above it the response is
  dominated by the speaker's own direct sound and by
  position-dependent interference that spatial averaging cannot resolve, so
  correcting there fights the speaker instead of the room.

The relation, stated once: **the room ceiling may never be clamped below the
gated spec's lower edge.** Below that edge the gated layer has no measured
authority to hand off *to*, so a room ceiling underneath it would leave a band
that neither layer owns. That is why :data:`ROOM_BOUNDARY_MIN_HZ` is *defined
as* :data:`GATED_SPEC_LOWER_EDGE_HZ` rather than coincidentally equal to it —
moving the spec edge drags the clamp floor with it, and the contract test in
``tests/test_correction_boundary_ssot.py`` fails if the two ever drift apart.
Between the spec edge and the room ceiling the two layers deliberately overlap:
the gated layer owns absolute tonal balance there, the room layer cuts modal
peaks (see ``docs/room-correction-regime-plan.md`` D3).

The spec edge itself stays revisable — ``flat_spec`` records 250-vs-300 as
S0-contingent — but it can only move *through* this module.

**A session can grade higher than this edge, and since #2551 it says so.**
:data:`GATED_SPEC_LOWER_EDGE_HZ` is the TABLE's edge, not a promise that a
given capture has authority there: ``evaluate_flat_spec`` intersects it with
the session's own trusted floor (``2.5/T`` for the reflection-free window
that capture achieved) and publishes the result per band as ``graded_lo_hz``.
This module's relation is unaffected — the clamp only ever raises an edge, so
a room ceiling at or above 250 Hz can still never sit below the *table's*
floor — but the two layers' deliberate overlap can narrow, and on a room whose
gate tops out at 7 ms it closes: the trusted floor is 357.14 Hz against a
350 Hz room ceiling, so 350-357 Hz is owned by neither layer. That gap is
disclosed, not introduced: the gate never had authority at 350 Hz in such a
room, and grading there was the thing #2551 stopped. Whether the room ceiling
should follow the trusted floor is a room-layer policy question, deliberately
not answered here.

What is NOT owned here
----------------------
* **Lower band edges.** The room band's *low* edge (20 Hz for design, 50 Hz for
  the deviation/repeatability readouts) is a microphone-physics and
  design-floor concern, not a layer seam: 50 Hz exists because the iPhone
  built-in mic's own high-pass filter dominates below it. Those stay where they
  are used.
* **The SNR band tables** (:data:`jasper.correction.acoustic_quality.SNR_BANDS_HZ`
  and :data:`jasper.audio_measurement.snr_policy.CROSSOVER_SNR_BANDS_HZ`).
  Their 350 Hz edge looks like this boundary and is deliberately NOT routed
  here — see the trap note on those tables. They are capture-quality
  vocabulary, shared with the gated instrument, and must stay static and
  comparable across sessions and instruments even after the room boundary
  becomes per-room.

Roadmap: RC1 (this module) only makes the boundary a single value. RC2 adds a
Schroeder-frequency estimator and RC3 a per-room transition resolved here;
every consumer already routed here picks that up with no further edit. See
``docs/room-correction-regime-plan.md``.
"""
from __future__ import annotations

# The gated speaker spec's lower edge, in Hz. Owned here so the room layer's
# clamp floor and the speaker layer's spec floor cannot drift apart;
# jasper.active_speaker.flat_spec consumes this rather than re-declaring 250.
#
# This is the TABLE's nominal edge. What a session actually grades is this
# value intersected with that capture's trusted floor (2.5/T) — see the
# module docstring's "A session can grade higher than this edge" note.
GATED_SPEC_LOWER_EDGE_HZ: float = 250.0

# The room-correction ceiling, Hz, when no per-room estimate is available: the
# shipped Toole-aligned modal/transition boundary.
ROOM_BOUNDARY_DEFAULT_HZ: float = 350.0

# Clamp bounds for a per-room ceiling estimate. The floor is the gated spec's
# lower edge BY DEFINITION (see the module docstring); the ceiling is the widest
# room-correction band the project admits, and is also the `assertive`
# strategy's band.
ROOM_BOUNDARY_MIN_HZ: float = GATED_SPEC_LOWER_EDGE_HZ
ROOM_BOUNDARY_MAX_HZ: float = 500.0
