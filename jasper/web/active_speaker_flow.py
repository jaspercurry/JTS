# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active-speaker commissioning ↔ measurement mutual exclusion.

The active-crossover commission flow (the per-driver ramp + near-field
level-match capture) plays sweeps/tones through the production CamillaDSP graph.
Pair sync does the same and opens
``jasper.measurement_window.measurement_window``. Two running at once corrupt
each other's captures, so this module keeps them apart.

This is DEFENSE-IN-DEPTH, not the only thing standing between the flows. An
active speaker can't be measured as a bonded pair (the graph carrier defers
active×grouping, so sync doesn't apply to it), and every commissioning graph
carries the tweeter protective high-pass. So a collision here means a
corrupted, re-runnable measurement — not unsafe output.

The sync flow holds ``measurement_window`` for its whole session and excludes a
concurrent one atomically through the window's ``_window_active`` mutex. The
commission flow can't hold a window the same way: it spans many
``/active-speaker/*`` requests, each on its own per-request ``asyncio.run``
loop, with the ramp tone deliberately continuous *across* requests — there is
no persistent loop to own a held context manager. So it participates
COOPERATIVELY instead:

  * :func:`active_phase` derives the commission "phase" from the self-expiring
    safe-playback session; the sync start path consults it.
  * :func:`blocking_measurement_phase` is the reverse — ``commission-load``
    refuses to arm while sync is active.

These checks are advisory and NON-ATOMIC: a sub-second start-vs-start race
between commission-load and a sync start can slip both past their checks. The
cost of losing that race is one corrupted measurement someone re-runs — never
unsafe output, per the protections above — so a cooperative check is the right
weight rather than a heavier shared lock.

Self-healing: the phase is read from the safe-playback armed state, which
``load_safe_playback_state`` reports as ``expired`` past its TTL
(``safe_playback.DEFAULT_ARM_TTL_SEC``). An abandoned commission session
therefore releases the exclusion automatically — no stale flag can wedge the
other flow.
"""

from __future__ import annotations


def active_phase() -> str | None:
    """``"commissioning"`` while an active-speaker commission session is armed.

    Returns ``None`` otherwise — idle, or expired past the safe-playback TTL.
    ``load_safe_playback_state`` is itself fail-soft (a missing / corrupt state
    reads as ``idle``), so this advisory gate never wedges the other measurement
    flows: anything but a live ``armed`` status reads as not-commissioning.
    """
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    return (
        "commissioning"
        if load_safe_playback_state().get("status") == "armed"
        else None
    )


def blocking_measurement_phase() -> str | None:
    """The active sync phase, or ``None``.

    The reverse of the sync start path consulting :func:`active_phase`:
    ``commission-load`` calls this and refuses to arm a driver test while
    another measurement flow holds (or is about to hold) the measurement
    window. The lazy import avoids an import cycle (that module consults us
    back).
    """
    from .sync_flow import active_phase as _sync_phase

    sync = _sync_phase()
    return f"sync:{sync}" if sync is not None else None
