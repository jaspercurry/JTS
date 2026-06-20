"""Active-speaker commissioning ↔ measurement mutual exclusion.

The active-crossover commission flow (the per-driver ramp + near-field
level-match capture) plays sweeps/tones through the production CamillaDSP graph.
Room correction, pair balance, and pair sync do the same and open
``correction.coordinator.measurement_window``. Running two of these at once
corrupts both captures — and a correction sweep started mid active-speaker
commissioning could drive a bare tweeter. They must be mutually exclusive.

The other three flows hold ``measurement_window`` for their whole session and
exclude each other through ``correction._reserve_start_slot`` consulting each
other's ``active_phase()`` (with the window's ``_window_active`` mutex as the
backstop). The commission flow can't hold a window the same way: it spans many
``/active-speaker/*`` requests, each served on its own per-request
``asyncio.run`` loop, with the ramp tone deliberately continuous *across*
requests — there is no persistent loop to own a held context manager. So it
participates COOPERATIVELY instead:

  * :func:`active_phase` derives the commission "phase" from the self-expiring
    safe-playback session; the three measurement start paths consult it.
  * :func:`blocking_measurement_phase` is the reverse — ``commission-load``
    refuses to arm while any of the three is active.

Same guarantee (never two measurement flows at once), no held window or
persistent loop.

Self-healing: the phase is read from the safe-playback armed state, which
``load_safe_playback_state`` reports as ``expired`` past its TTL
(``safe_playback.DEFAULT_ARM_TTL_SEC``). An abandoned commission session
therefore releases the exclusion automatically — no stale flag can wedge the
other flows.
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
    """The first active correction / balance / sync phase, or ``None``.

    The reverse of the three start paths consulting :func:`active_phase`:
    ``commission-load`` calls this and refuses to arm a driver test while another
    measurement flow holds (or is about to hold) the measurement window. Lazy
    imports avoid an import cycle (those modules consult us back).
    """
    from .balance_flow import active_phase as _balance_phase
    from .correction_setup import active_correction_phase
    from .sync_flow import active_phase as _sync_phase

    balance = _balance_phase()
    if balance is not None:
        return f"balance:{balance}"
    sync = _sync_phase()
    if sync is not None:
        return f"sync:{sync}"
    correction = active_correction_phase()
    if correction is not None:
        return f"correction:{correction}"
    return None
