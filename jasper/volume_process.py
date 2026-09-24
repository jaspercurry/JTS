# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-process registration of the canonical main_volume target and fader owner.

Every process that performs a CamillaDSP graph swap needs both;
`install_env_canonical_target_provider` is the one call that gives a process
both, and its docstring names the callers' pin. `jasper-voice` is the
exception: it already owns a long-lived `VolumeCoordinator` and registers
that coordinator's own reader and owner instead of building throwaway ones
here.
"""
from __future__ import annotations

from .volume_owner import VolumeOwner, install_volume_owner
from .volume_persistence import VolumePersistence, configured_path as volume_state_path


async def env_canonical_target_db() -> float:
    """Read current household intent through the active source coordinator."""
    from jasper import librespot_state  # lazy: import cost, the actuator graph loads only when a swap releases its duck
    from jasper.camilla import primary_controller  # lazy: test patch boundary (tests/test_volume_coordinator.py)
    from jasper.renderer import RendererClient  # lazy: import cost, the actuator graph loads only when a swap releases its duck
    from jasper.volume_coordinator import VolumeCoordinator  # lazy: import cost, the actuator graph loads only when a swap releases its duck

    coord = VolumeCoordinator(
        camilla=primary_controller(),
        persistence=VolumePersistence(volume_state_path()),
        backend=RendererClient(
            librespot_state_path=librespot_state.configured_path(),
        ),
    )
    coord.load_persisted_level()
    return await coord.get_camilla_target_db()


def install_env_canonical_target_provider() -> None:
    """Register this process's canonical main_volume target AND its fader owner.

    Both, from one call: a process with one but not the other would be
    half-arbitrated, and every existing call site gets the owner without an
    edit of its own — so the two cannot drift apart.

    Every process that performs a CamillaDSP graph swap needs one. A swap's
    duck release lands at ``min(canonical, current + own depth)``; with no
    canonical target it falls back to the entry snapshot, which an interleaved
    voice cue may already have ducked, stranding the fader tens of dB quiet
    inside the band `maybe_reconcile_camilla` refuses to heal. Every swap that
    ducks now uses the canonical target, with no exception.

    A process that already owns a long-lived coordinator registers that
    coordinator's own :meth:`VolumeCoordinator.get_camilla_target_db` instead
    (jasper-voice does), and registers that coordinator's ``volume_owner`` as
    the process owner rather than building a second. The rest call this: the
    coordinator is built per call rather than held, because a release happens
    once per graph swap and a socket-activated wizard has to stay light.

    **The owner is held, not per call** — unlike the target reader above. It
    carries the claim ledger, so a fresh one per call would be a fresh set of
    claims and no arbitration at all. Its controller is lazy (``_ensure``
    connects on first use), so holding one costs a wizard nothing until
    something actually claims the fader.

    Which processes call it is pinned by
    ``tests/test_canonical_target_registration.py``.
    """
    from jasper.camilla import primary_controller, set_canonical_target_db_provider  # lazy: test patch boundary (tests/test_volume_coordinator.py)

    set_canonical_target_db_provider(env_canonical_target_db)

    # Bound with best_effort=True: the owner's doors must report failure, not
    # raise it (``volume_latch.FADER_IO_ERRORS`` states that contract, and
    # ``CamillaUnavailable`` is deliberately not in it).
    fader = primary_controller()
    install_volume_owner(
        VolumeOwner(
            set_fader_db=lambda db: fader.set_volume_db(db, best_effort=True),
            get_fader_db=lambda: fader.get_volume_db(best_effort=True),
        )
    )
