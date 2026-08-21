# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Level-solve refusal copy plus the logged screen-envelope entry point.

``/sound/`` owns topology, driver protection, output identity, and the safe
starting profile.  The crossover screen envelope owns the distinct microphone
journey:

    mic/calibration + per-driver level -> driver sweeps -> combined alignment
    -> atomic apply

The envelope itself is :mod:`jasper.active_speaker.crossover_envelope_v2` --
the only flow since W5b retired the legacy per-driver flow and the
``JASPER_CROSSOVER_FLOW`` selector. What is left here is the logged wrapper
around it and the single code -> household-copy mapping for a level-solve
refusal. Both perform no I/O and mutate no measurement state.

A ``build_crossover_envelope`` compatibility dispatcher and an
``_active_level_solve_refusal`` reader also lived here; the dispatcher's
callers now import v2 directly, and nothing in production ever read the
``solve_refusal`` field the reader projected, so both were deleted.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event

logger = logging.getLogger(__name__)


# Closed-loop level solver (W2.1/W2.2) refusal copy. ONE mapping owns
# code -> user copy, mirroring jasper.correction.level_match.describe_ramp_refusal
# (#1534) -- neither this function nor any caller hand-rolls its own
# sentence. Values are literal strings, not imports from
# jasper.audio_measurement.level_solver -- mirrors the existing
# room_too_noisy duplication so this module stays free of a solver import.
# Public (no leading underscore) since W2.4: the raise site itself
# (jasper.web.correction_crossover_backend.LevelSolveRefused) imports this to
# build str(exc) so an unmigrated catch site can never render the raw code --
# see that class's docstring for the hardware-run-20 leak this closed.
_LEVEL_SOLVE_REFUSAL_CODE_ROOM_TOO_NOISY = "room_too_noisy_for_safe_measurement"
_LEVEL_SOLVE_REFUSAL_CODE_MEASUREMENT_WINDOW_UNREACHABLE = (
    "measurement_window_unreachable"
)


def describe_level_solve_refusal(refusal: Mapping[str, Any]) -> str:
    """Homeowner copy for a level-solve refusal.

    Honest about what the offered action does: redoing the level check
    re-runs the full guided microphone/level sequence (today the room's
    ambient reading is a byproduct of that ramp, so re-measuring a quieter
    room requires re-locking -- see the branch comments at the call sites).
    The copy must not imply the saved levels survive the redo.
    """

    code = str(refusal.get("code") or "")
    if code == _LEVEL_SOLVE_REFUSAL_CODE_MEASUREMENT_WINDOW_UNREACHABLE:
        # W2.2: the target burned its bounded clip/SNR correction budget
        # (see CrossoverLevelLease.record_solve_correction) and rejected
        # again. Unlike room_too_noisy this is a mic-placement problem, not
        # a room-noise problem -- the level lock is unaffected, so the
        # remedy is repositioning and re-measuring, not redoing the level
        # check.
        return (
            "The microphone can't get a clean reading at this distance — "
            "it's picking up too much on loud passages and too little on "
            "quiet ones. Move the microphone close to the driver being measured "
            "(about 3 cm / just over an inch away), then measure again."
        )

    band = refusal.get("failing_band_hz")
    lo, hi = (
        (band[0], band[1])
        if isinstance(band, (list, tuple)) and len(band) == 2
        else (None, None)
    )
    band_text = (
        f"Room noise between {float(lo):.0f}–{float(hi):.0f} Hz"
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float))
        else "Room noise in this driver's measurement band"
    )
    remedy = (
        "Quiet the room or move the microphone closer, then redo the quick "
        "level check (about 2 minutes) to measure again."
    )
    if code != _LEVEL_SOLVE_REFUSAL_CODE_ROOM_TOO_NOISY:
        # An unrecognized future code still gets levers-naming copy rather
        # than a bare technical string.
        return (
            f"{band_text} could not be measured reliably at a safe level. "
            f"{remedy}"
        )
    return (
        f"{band_text} is too high to measure reliably at safe levels. "
        f"{remedy}"
    )


def build_crossover_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    """Serve the v2 session crossover envelope for ``status`` and log the serve."""

    from .crossover_envelope_v2 import build_crossover_envelope_v2

    envelope = build_crossover_envelope_v2(status)
    log_event(
        logger,
        "correction.crossover_envelope_serve",
        screen=envelope["screen"],
        active=envelope["active"],
        step_count=len(envelope["steps"]),
        nudge_count=len(envelope["nudges"]),
        action=(envelope.get("next_action") or {}).get("id"),
        alternate_action_count=len(envelope.get("alternate_actions") or []),
        applied=(envelope.get("applied") or {}).get("state"),
    )
    return envelope
