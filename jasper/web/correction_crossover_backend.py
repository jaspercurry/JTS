# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Correction-side active-crossover status payload and measurement-journey reset."""

from __future__ import annotations

import logging
from typing import Any

from jasper.active_speaker.applied_identity import applied_identity
from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state
from jasper.active_speaker.startup_load import load_commission_load_state
from jasper.active_speaker.commission_ramp import load_ramp_state
from jasper.active_speaker.setup_status import conductor_status
from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds
from jasper.active_speaker.safe_playback import load_safe_playback_state
from jasper.active_speaker.timing_status import timing_status_lines
from jasper.log_event import log_event
from jasper.output_topology_store import load_output_topology

from . import correction_capture

logger = logging.getLogger(__name__)

#: The slow blocks of the live run's first answer, keyed by (run, applied
#: record); see :func:`status_payload`.
_run_snapshot: dict[str, Any] | None = None


def reset_measurement_journey() -> dict[str, Any]:
    """Clear the active-crossover MEASUREMENT JOURNEY in place.

    The scoped sibling of the nuclear ``/sound/`` Advanced-setup reset
    (``jasper.active_speaker.reset.clear_active_speaker_setup_state``):
    restarts the guided capture flow — comparison set, driver captures,
    summed validation, and the compiled-but-not-loaded protected candidate —
    without losing driver research (``design_draft``) or disturbing whatever
    audio graph is currently applied/loaded (``baseline_profile``,
    ``startup_load``). See ``jasper.active_speaker.reset`` for the
    artifact-by-artifact rationale.

    Callers MUST stop any in-flight capture session before calling this —
    see ``_handle_crossover_reset`` in ``jasper.web.correction_setup``,
    which reuses the capture-cancel path first. This function only owns the
    durable journey files; it never touches CamillaDSP.
    """

    from jasper.active_speaker.reset import (
        active_speaker_measurement_journey_paths,
        active_speaker_setup_state_paths,
        clear_active_speaker_measurement_journey,
    )

    reset_result = clear_active_speaker_measurement_journey()
    # Report what actually happened, not the static intent: a file that failed
    # to unlink lands in ``errors`` and flips ``status`` to ``partial`` — the
    # UI must not paint that green. ``cleared`` are the files this call removed;
    # ``missing`` were already absent (also fine); ``errors`` are the honest
    # failures. ``kept`` is the by-design KEEP set (design_draft, baseline_profile,
    # startup_load), which this call never touches.
    cleared_ids = sorted(e["id"] for e in reset_result.get("cleared", []))
    missing_ids = sorted(e["id"] for e in reset_result.get("missing", []))
    error_ids = sorted(e["id"] for e in reset_result.get("errors", []))
    kept_ids = sorted(
        set(active_speaker_setup_state_paths())
        - set(active_speaker_measurement_journey_paths())
    )
    log_event(
        logger,
        "correction.crossover_reset",
        status=reset_result.get("status"),
        cleared=cleared_ids,
        missing=missing_ids,
        errors=error_ids,
        kept=kept_ids,
    )
    return {
        **reset_result,
        "cleared_ids": cleared_ids,
        "missing_ids": missing_ids,
        "error_ids": error_ids,
        "kept_ids": kept_ids,
    }


def status_payload() -> dict[str, Any]:
    """Return active-crossover targets and saved measurement evidence.

    While a capture holds the microphone, its reader thread shares this
    process (#5632 F1). So the slow blocks — the setup report with its two
    graph compiles, the banked-round timing and the receipt ledger — come
    from the run's first answer, and ``snapshot_at`` says when that was
    read. The run's own receipt and packet show in the first answer after
    the run ends, which is also when the graphs it loads are restored. An
    apply changes the applied record, which ends the reuse.
    """
    global _run_snapshot
    applied = load_applied_baseline_profile_state()
    identity = applied_identity(applied)
    run = correction_capture.live_run_id()
    held = _run_snapshot
    snapshot = held if held is not None and held["key"] == (run, identity) else None
    payload = conductor_status(setup=snapshot["setup"] if snapshot else None)
    payload["commission"] = {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
        "safe_playback": load_safe_playback_state(),
    }
    targets_raw = payload.get("targets")
    targets: dict[str, Any] = targets_raw if isinstance(targets_raw, dict) else {}
    driver_count = len(targets.get("drivers") or [])
    summed_count = len(targets.get("summed") or [])
    if payload["active"]:
        from jasper.active_speaker.design_draft import load_design_draft

        try:
            safety_topology = load_output_topology()
            safety_draft = load_design_draft(topology=safety_topology)
            payload["driver_safety_profile"] = safety_draft.get(
                "driver_safety_profile"
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            payload["driver_safety_profile"] = None
    payload["applied_profile"] = applied
    if snapshot is None:
        recent = latest_banked_rounds(identity, programs=("speaker",)) if identity is not None else {}
        payload["timing"] = timing_status_lines(applied, recent.get("speaker"))
    else:
        payload["timing"] = snapshot["timing"]
    # v2 session state (Wave 5a). Fail-soft: an unreadable v2 state must
    # never take down the whole status surface.
    try:
        from .correction_crossover_v2_status import crossover_v2_status_block

        v2_block = crossover_v2_status_block(controllability=snapshot["controllability"] if snapshot else False)
    except (OSError, RuntimeError, TypeError, ValueError):
        logger.warning("crossover v2 status block unavailable", exc_info=True)
        v2_block = None
    if v2_block is not None:
        payload["crossover_v2"] = v2_block
    payload["snapshot_at"] = snapshot["at"] if snapshot else None
    if snapshot is None:
        _run_snapshot = None if run is None else {
            "key": (run, identity), "at": payload.get("generated_at"), "setup": payload["setup"],
            "timing": payload["timing"], "controllability": (v2_block or {}).get("controllability"),
        }
    logger.debug(
        "crossover status active=%s drivers=%d summed=%d",
        payload["active"],
        driver_count,
        summed_count,
    )
    return payload
