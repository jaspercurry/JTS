# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mux activity truth and the live signal-path classifier.

A leaf the audio-health composer reads every fast tick: whether mux's
canonical per-source ``playing`` truth can even be trusted right now
(:func:`_active_source`, :func:`_activity_truth_unknown`), and — given that
verdict — the ordered precedence ladder that turns fan-in/outputd/ring
observations into one ``signal_path`` shape (:func:`_signal_path`). Ring
pressure and TTS-backlog derivations live here too: both feed only this
classifier.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..fanin.status import DIRECT_HEALTH_BROKEN
from ..fanin_coupling import RING_SLOT_FRAMES
from ..platform.status_socket import FANIN_STALE_MS, OUTPUTD_STALE_MS
from ._health_fields import (
    DIAGNOSTICS_REMEDY,
    RESTART_REMEDY,
    _as_int,
    _finite_number,
    _mapping,
)
from ._health_sources import _LABEL_TO_SOURCE, _SOURCE_LABELS

# `_signal_path`'s generic "outputd never started" and "fan-in is not
# reporting" sentences. Written once because `_state_issues` raises the
# matching `path.outputd_unavailable` / `path.fanin_unavailable` incidents from
# the same two facts and neither pair may drift.
_OUTPUT_ABSENT_TITLE = "The speaker's sound output is not running"
_OUTPUT_ABSENT_DETAIL = (
    f"Nothing will play until it comes back. {RESTART_REMEDY} "
    f"{DIAGNOSTICS_REMEDY}"
)
PATH_UNREPORTED_TITLE = "Sound status unavailable"
PATH_UNREPORTED_DETAIL = (
    "JTS cannot tell whether sound is reaching the speaker right now, so "
    f"music may be missing. {RESTART_REMEDY}"
)

# The closed vocabulary of signal-path shape codes — every `code` any
# signal-path producer emits (`_signal_path` and the overrides
# `compose_audio_health` layers on it). A new shape registers itself HERE, which
# is what makes `test_the_household_shapes_cover_every_signal_path_code` fail
# until it is added to the household-register sweep as well.
SIGNAL_PATH_CODES = frozenset({
    "activity_unknown",
    "camilla_not_installed",
    "camilla_stopped",
    "clean",
    "input_absent",
    "input_broken",
    "input_stalled",
    "output_absent",
    "output_backend_inactive",
    "output_deaf",
    "output_ring_stalled",
    "output_stalled",
    "path_pressured",
    "path_stalled",
    "path_unreported",
    "starting",
    "transport_parked",
    "transport_unservable",
    "tts_queue_full",
    "undeclared_hardware",
})


def _selected_source(airplay: Mapping[str, Any]) -> str | None:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    selected = fanin.get("selected_input")
    if not isinstance(selected, str):
        return None
    normalized = selected.strip().lower()
    if normalized in _LABEL_TO_SOURCE:
        normalized = _LABEL_TO_SOURCE[normalized]
    return normalized if normalized in _SOURCE_LABELS else None


def _source_playing(
    mux_status: Mapping[str, Any] | None,
    source_id: str | None,
) -> bool | None:
    """Project mux's canonical per-source activity without inventing fallback."""
    if source_id is None or not isinstance(mux_status, Mapping):
        return None
    source = _mapping(_mapping(mux_status.get("sources")).get(source_id))
    playing = source.get("playing")
    return playing if isinstance(playing, bool) else None


def _active_source(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> str | None:
    selected = _selected_source(airplay)
    return selected if _source_playing(mux_status, selected) is True else None


def _activity_truth_unknown(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> bool:
    """Whether mux cannot authoritatively classify the selected lane."""
    if not isinstance(mux_status, Mapping) or not isinstance(
        mux_status.get("sources"),
        Mapping,
    ):
        return True
    selected = _selected_source(airplay)
    return selected is not None and _source_playing(mux_status, selected) is None


ACTIVITY_UNKNOWN_DETAIL = "JTS cannot tell which source is playing right now."


def _activity_unavailable_signal() -> dict[str, str]:
    return {
        "code": "activity_unknown",
        "status": "unknown",
        "headline": "Playback activity unavailable",
        "detail": ACTIVITY_UNKNOWN_DETAIL,
    }


def _ring_pressure(fanin_output: Mapping[str, Any]) -> float | None:
    """Fraction of fan-in's ring publishes that had to wait for a free slot.

    `full_waits` ticks once per SLOT publish that waited, so its rate is read
    against the publish rate (sample_rate / RING_SLOT_FRAMES): jts4 measured
    162 waits/s against 375 publishes/s in lockstep (issue #4124).

    INFORMATIONAL ONLY. Ring A is a blocking handshake pinned near full by
    design (ADR-0205), so a saturated ring is the steady state, not a fault:
    this must never reach a verdict.

    None whenever any term is absent or the publish rate is underivable —
    absence must read as "not observed", never as "no pressure".
    """
    ring = _mapping(fanin_output.get("ring"))
    waits = _finite_number(ring.get("full_waits_per_sec"))
    rate = _as_int(fanin_output.get("sample_rate"))
    if waits is None or rate <= 0:
        return None
    return float(waits) * RING_SLOT_FRAMES / rate


def _ring_occupancy_ms(fanin_output: Mapping[str, Any]) -> float | None:
    """Fan-in's queued program depth, in ms.

    ``occupancy`` counts ring SLOTS, each ``RING_SLOT_FRAMES`` frames wide
    (rust/jasper-ring/src/layout.rs), not frames or ms.
    """
    ring = _mapping(fanin_output.get("ring"))
    slots = _finite_number(ring.get("occupancy"))
    rate = _as_int(fanin_output.get("sample_rate"))
    if slots is None or slots < 0 or rate <= 0:
        return None
    return float(slots) * RING_SLOT_FRAMES * 1000.0 / rate


def _tts_backlog_ratio(*lanes: Any) -> float:
    """Deepest ``pending/budget`` across every armed TTS lane; 0.0 if none is."""
    deepest = 0.0
    for lane_raw in lanes:
        lane = _mapping(lane_raw)
        if lane.get("enabled") is not True:
            continue
        budget_frames = _as_int(lane.get("budget_frames"))
        if budget_frames <= 0:
            continue
        deepest = max(deepest, _as_int(lane.get("pending_frames")) / budget_frames)
    return deepest


def _signal_path(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    current = _mapping(airplay.get("current"))
    fanin_raw = current.get("fanin")
    warmup = bool(airplay.get("warmup_active"))
    if not isinstance(fanin_raw, Mapping):
        if warmup:
            return {
                "code": "starting",
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Sound will be ready in a moment.",
            }
        return {
            "code": "path_unreported",
            "status": "unknown",
            "headline": PATH_UNREPORTED_TITLE,
            "detail": PATH_UNREPORTED_DETAIL,
        }
    if outputd is None:
        if warmup:
            return {
                "code": "starting",
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Sound will be ready in a moment.",
            }
        return {
            "code": "output_absent",
            "status": "issue",
            "headline": _OUTPUT_ABSENT_TITLE,
            "detail": _OUTPUT_ABSENT_DETAIL,
        }

    outputd_map = _mapping(outputd)
    backend = outputd_map.get("backend")
    if backend is not None and backend != "alsa":
        return {
            "code": "output_backend_inactive",
            "status": "issue",
            "headline": "The speaker is not connected to its sound hardware",
            "detail": (
                "Sound is being processed but has nowhere to go, so nothing "
                f"will play. {RESTART_REMEDY} {DIAGNOSTICS_REMEDY}"
            ),
        }
    outputd_watchdog = _mapping(outputd_map.get("watchdog"))
    outputd_progress_age = _as_int(
        outputd_watchdog.get("last_progress_age_ms"),
    )
    if outputd_watchdog and outputd_progress_age > OUTPUTD_STALE_MS:
        return {
            "code": "output_stalled",
            "status": "issue",
            "headline": "Sound has stopped reaching the speaker",
            "detail": (
                "Sound stopped moving out to the speaker a few seconds ago. "
                f"{RESTART_REMEDY}"
            ),
        }

    fanin = _mapping(fanin_raw)
    watchdog = _mapping(fanin.get("watchdog"))
    if _as_int(watchdog.get("last_progress_age_ms")) > FANIN_STALE_MS:
        return {
            "code": "path_stalled",
            "status": "issue",
            "headline": "Sound has stopped moving through the speaker",
            "detail": (
                "Sound from your sources stopped moving through the speaker a "
                f"few seconds ago. {RESTART_REMEDY}"
            ),
        }

    output = _mapping(fanin.get("output"))
    ring = _mapping(output.get("ring"))
    if ring.get("stall_active") is True:
        # ABOVE `output_deaf` for the same reason the fan-in watchdog is: a
        # ring the reader has stopped draining is what leaves outputd with
        # nothing to play, and the cause outranks its own symptom.
        return {
            "code": "output_ring_stalled",
            "status": "issue",
            "headline": "Sound is stuck inside the speaker",
            "detail": (
                "Sound from your sources is arriving but cannot move on to "
                f"the speaker's output. {RESTART_REMEDY}"
            ),
        }

    # outputd is writing periods, but what it writes is silence it did not
    # intend: a deaf chain leaves both watchdogs progressing and every xrun
    # count flat (#3458). The verdict is outputd's own — it owns the DAC
    # geometry its threshold is derived from.
    #
    # BELOW the fan-in watchdog deliberately: a stalled fan-in starves
    # CamillaDSP, which empties the ring, so outputd latches deaf at 2 s while
    # FANIN_STALE_MS only trips at 5 — the cause outranks its own symptom, the
    # same way `camilla_stopped` outranks this in `compose_audio_health`.
    #
    # Not during warmup: outputd primes and starts reading an empty ring before
    # CamillaDSP is producing (the gate `_stopped_dsp_signal` carries for the
    # same reason).
    if not warmup and _mapping(outputd_map.get("content")).get("deaf") is True:
        return {
            "code": "output_deaf",
            "status": "issue",
            "headline": "The speaker is playing silence",
            "detail": (
                "Sound is reaching the speaker's last step, but nothing is "
                f"arriving for it to play. {RESTART_REMEDY} "
                f"{DIAGNOSTICS_REMEDY}"
            ),
        }

    active = active_source
    inputs = _mapping(fanin.get("inputs"))
    active_input = _mapping(inputs.get(active)) if active else {}
    if active and active_input.get("present") is False:
        return {
            "code": "input_absent",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{_SOURCE_LABELS.get(active, 'The source')} is playing, but "
                "the speaker has no open connection for it. Play it again, or "
                "try another source."
            ),
        }
    if active_input.get("health") == DIRECT_HEALTH_BROKEN:
        return {
            "code": "input_broken",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{_SOURCE_LABELS.get(active or '', 'The source')} stopped "
                "sending sound to the speaker. Play it again, or try another "
                "source."
            ),
        }
    frames_per_sec = active_input.get("frames_per_sec")
    if (
        active
        and isinstance(frames_per_sec, (int, float))
        and not isinstance(frames_per_sec, bool)
        and frames_per_sec < 1000.0
    ):
        return {
            "code": "input_stalled",
            "status": "issue",
            "headline": "No sound is arriving from this source",
            "detail": (
                f"{_SOURCE_LABELS.get(active, active)} is selected, but no "
                "sound is coming from it. Play it again, or try another source."
            ),
        }
    # Losing periods, from either end: the ring dropped a period the reader
    # never took, or the active lane is xrunning. Both are rates, so neither
    # latches once the box recovers.
    ring_drops = _finite_number(ring.get("drops_per_sec"))
    input_xrun_rate = _finite_number(active_input.get("xruns_per_sec"))
    if (
        (ring_drops is not None and ring_drops > 0.0)
        or (input_xrun_rate is not None and input_xrun_rate > 0.0)
    ):
        return {
            "code": "path_pressured",
            "status": "warn",
            "headline": "Sound is only just keeping up",
            "detail": (
                "Music is playing, but the speaker is right at the edge of "
                f"keeping up with it, so it may skip. {RESTART_REMEDY}"
            ),
        }

    # Both TTS lanes can be armed at once — fan-in's socket has a non-optional
    # default, and outputd's arms on a passive bonded member — so the enabled
    # flag cannot pick between them. Report on whichever is deepest against its
    # own budget; an idle lane can never mask a backed-up one.
    if _tts_backlog_ratio(fanin.get("tts"), outputd_map.get("tts")) >= 1.0:
        return {
            "code": "tts_queue_full",
            "status": "warn",
            "headline": "Voice replies are delayed",
            "detail": (
                "JTS has more spoken replies waiting than it can play right "
                "now, so answers may arrive late. Music is unaffected."
            ),
        }
    return {
        "code": "clean",
        "status": "ok",
        "headline": "Sound path is healthy",
        "detail": "Everything between your sources and the speaker is responding.",
    }
