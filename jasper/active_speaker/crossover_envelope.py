# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure screen envelope for sequential Layer-A acoustic commissioning.

``/sound/`` owns topology, driver protection, output identity, and the safe
starting profile.  This envelope owns the distinct microphone journey:

    mic/calibration + per-driver level -> driver sweeps -> combined alignment
    -> atomic apply

It reads the already-composed crossover status payload and returns one primary
action plus any explicit alternatives. It performs no I/O and mutates no
measurement state; the correction web host supplies relay/apply adapters for
the returned action descriptors.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ..log_event import log_event

logger = logging.getLogger(__name__)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _active_level_solve_refusal(
    status: Mapping[str, Any], target_id: str
) -> Mapping[str, Any] | None:
    """The closed-loop level solver's refusal (W2.1/W2.3) for ``target_id``.

    Sourced from ``CrossoverLevelLease.level_match_snapshot()``'s
    ``solve_refusal`` -- the most recent refusal from an actual PRE-FLIGHT
    solve attempt, cleared by a fresh ramp lock (stale solver inputs) or an
    explicit flow reset (a full level-match retune), never by set
    completion (W2.3).

    W2.3: a completed-but-insufficient finalization can itself exhaust the
    bounded correction budget (``record_solve_correction``'s
    ``"completed_insufficient"`` trigger) WITHOUT any fresh solve attempt
    ever running -- the lease only solves again when the next sweep is
    actually prepared. Without this, the exhausted target would keep
    rendering the generic completed-insufficient terminal instead of the
    placement-lever copy the household actually needs. So this also
    synthesizes the SAME typed refusal straight from
    ``level_match.solve_correction``'s ``exhausted`` flag the moment the
    budget runs out, reusing ``describe_level_solve_refusal``'s single
    code -> copy mapping (its ``measurement_window_unreachable`` branch
    reads only ``code``, so the minimal synthesized mapping is sufficient).
    The offered "Redo the quick level check" action is a genuine escape
    hatch, not a dead end: the between-set restart clears a REFUSED
    target's correction state for a fresh evaluation -- W2.4 (hardware run
    20) widened this from "exhausted only" to "any pre-flight refusal
    shown, at any write count," closing a dead loop where a
    room_too_noisy refusal below the exhausted threshold survived the
    restart and refused again identically with no audio played (see
    ``CrossoverLevelLease.invalidate_comparison_context``'s
    ``preserve_solve_corrections`` contract), so the refusal cannot latch
    across the restart.

    This function and the restart's own reader
    (``CrossoverLevelLease._target_refusal_pending``) are SEPARATE code
    paths over the same two stored facts: the lease stores
    ``_solve_refusal`` and the bounded write count; this function reads
    their snapshot projections (``solve_refusal`` /
    ``solve_correction.exhausted``), the restart reads them directly. The
    OR here must stay equivalent to that predicate -- their agreement
    across the representative states is pinned by
    ``test_refusal_pending_predicate_parity_with_envelope_rendering`` in
    tests/test_correction_crossover_backend_level_solve.py; if you change
    either side, that parity test is the contract to keep green.
    """

    refusal = _mapping(_mapping(status.get("level_match")).get("solve_refusal"))
    if refusal and str(refusal.get("target_id") or "") == target_id:
        return refusal
    correction = _mapping(
        _mapping(status.get("level_match")).get("solve_correction")
    ).get(target_id)
    if isinstance(correction, Mapping) and correction.get("exhausted") is True:
        return {
            "target_id": target_id,
            "code": _LEVEL_SOLVE_REFUSAL_CODE_MEASUREMENT_WINDOW_UNREACHABLE,
        }
    return None


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
            "quiet ones. Move the phone close to the driver being measured "
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


def build_crossover_envelope(status: Mapping[str, Any]) -> dict[str, Any]:
    """Serve the v2 conductor crossover envelope for ``status``.

    The legacy per-driver flow and the ``JASPER_CROSSOVER_FLOW`` selector
    that chose between it and v2 were retired in W5b — v2 is the only flow
    now. This thin dispatcher stays so callers that still import
    ``build_crossover_envelope`` keep serving the current envelope; a stale
    ``JASPER_CROSSOVER_FLOW=legacy`` on a machine no longer selects anything.
    """
    from .crossover_envelope_v2 import build_crossover_envelope_v2

    return build_crossover_envelope_v2(status)


def build_crossover_envelope_logged(status: Mapping[str, Any]) -> dict[str, Any]:
    envelope = build_crossover_envelope(status)
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
