# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The stream card: what is playing right now, its live latency breakdown,
and the holding-together facts with no other home on the dashboard.

Continuity and timing are separate axes (see ``audio_health``'s module
docstring): a USB host-clock ``l2_fallback`` keeps audio playing safely, so it
degrades the latency axis but does not claim the signal path failed --
``current_stream.latency`` is where the summed queues are reported.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..fanin.latency_mode import PRESETS
from ..fanin_coupling import RING_SLOT_FRAMES
from ..music_sources import Source
from ..platform.status_socket import OUTPUTD_STALE_MS
from ._health_fields import as_int, detail_row, finite_number, mapping
from ._health_sources import SOURCE_LABELS
from .audio_source_cards import airplay_sync_timing


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
    ring = mapping(fanin_output.get("ring"))
    waits = finite_number(ring.get("full_waits_per_sec"))
    rate = as_int(fanin_output.get("sample_rate"))
    if waits is None or rate <= 0:
        return None
    return float(waits) * RING_SLOT_FRAMES / rate


def _ring_occupancy_ms(fanin_output: Mapping[str, Any]) -> float | None:
    """Fan-in's queued program depth, in ms.

    ``occupancy`` counts ring SLOTS, each ``RING_SLOT_FRAMES`` frames wide
    (rust/jasper-ring/src/layout.rs), not frames or ms.
    """
    ring = mapping(fanin_output.get("ring"))
    slots = finite_number(ring.get("occupancy"))
    rate = as_int(fanin_output.get("sample_rate"))
    if slots is None or slots < 0 or rate <= 0:
        return None
    return float(slots) * RING_SLOT_FRAMES * 1000.0 / rate


def fresh_dac_delay_ms(dac: Mapping[str, Any]) -> float | None:
    delay = finite_number(dac.get("snd_pcm_delay_ms"))
    age = finite_number(dac.get("snd_pcm_delay_sample_age_ms"))
    if (
        delay is None
        or age is None
        or float(delay) < 0.0
        or float(age) < 0.0
        or float(age) > OUTPUTD_STALE_MS
    ):
        return None
    return float(delay)


def _receiver_latency(
    active_source: str,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    output = mapping(fanin.get("output"))
    source_input = mapping(mapping(fanin.get("inputs")).get(active_source))
    resampler = mapping(source_input.get("resampler"))
    camilla = mapping(current.get("camilla"))
    dac = mapping(mapping(outputd).get("dac"))
    rate = (
        as_int(output.get("sample_rate"))
        or as_int(route.get("fixed_sample_rate"))
        or as_int(dac.get("sample_rate"))
    )
    components: list[tuple[str, float]] = []
    if rate > 0 and active_source == Source.USBSINK.value:
        fill = finite_number(resampler.get("fill_frames"))
        if fill is not None and float(fill) >= 0.0:
            components.append(("USB input queue", float(fill) * 1000.0 / rate))
    mixing_queue_ms = _ring_occupancy_ms(output)
    if mixing_queue_ms is not None:
        components.append(("Mixing queue", mixing_queue_ms))
    capture_rate = as_int(camilla.get("capture_rate")) or rate
    camilla_frames = finite_number(camilla.get("buffer_level"))
    if (
        capture_rate > 0
        and camilla_frames is not None
        and float(camilla_frames) >= 0.0
    ):
        components.append((
            "DSP queue",
            float(camilla_frames) * 1000.0 / capture_rate,
        ))
    dac_delay = fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        components.append(("DAC presentation queue", float(dac_delay)))

    runtime = mapping(timing.get("runtime"))
    phase = str(runtime.get("phase") or "")
    raw_mode = str(runtime.get("raw_mode") or "")
    preset = str(runtime.get("preset") or "")
    if phase == "fallback":
        mode_label = "stable fallback"
    elif phase == "checking":
        mode_label = "timing check in progress"
    elif phase == "clock_adjusting":
        mode_label = "clock adjusting"
    elif phase == "buffer_adjusting":
        mode_label = "latency adjusting"
    elif phase == "buffer_held":
        mode_label = "extra buffer in use"
    elif phase == "stable":
        label = PRESETS[preset].label.lower() if preset in PRESETS else "low"
        mode_label = f"{label} latency stable"
    else:
        mode_label = None
    details = [
        detail_row(label, f"{value:.1f} ms")
        for label, value in components
    ]
    estimate: dict[str, float] | None = None
    if components:
        total = sum(value for _label, value in components)
        lower = int(max(0.0, total) * 10.0) / 10.0
        estimate = {"lower_ms": lower}
        summary = f"{lower:g} ms"
    else:
        summary = "Live queue timing unavailable"
    if active_source == Source.USBSINK.value and mode_label:
        summary = f"{summary} · {mode_label}"
    return {
        "summary": summary,
        "detail": "",
        "details": details,
        "estimate": estimate,
        "mode": raw_mode or None,
    }


def _reliability(
    fanin_output: Mapping[str, Any],
    service_states: Mapping[str, Any] | None,
    restart_watch_units: Mapping[str, str],
) -> dict[str, Any]:
    """The holding-together facts with no other home on the stream card.

    NOT the interruption count: the session card owns that roll-up. Each row
    names its own scope — the queue pressure is live, the restarts are since
    startup.
    """
    details: list[dict[str, str]] = []
    pressure = _ring_pressure(fanin_output)
    if pressure is not None:
        details.append(detail_row(
            "Output queue pressure", f"{min(1.0, pressure) * 100:.0f}%",
        ))
    restarts = sum(
        as_int(mapping(mapping(service_states).get(unit)).get("n_restarts"))
        for unit in restart_watch_units
    )
    if restarts:
        details.append(detail_row("Sound restarts since startup", str(restarts)))
    return {"summary": "", "detail": "", "details": details}


def build_current_stream(
    *,
    active_source: str | None,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
    sampled_at: float,
    session: Mapping[str, Any] | None,
    restart_watch_units: Mapping[str, str],
    service_states: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    if active_source is None:
        return None
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    source_input = mapping(mapping(fanin.get("inputs")).get(active_source))
    resampler = mapping(source_input.get("resampler"))
    camilla = mapping(current.get("camilla"))
    dac = mapping(mapping(outputd).get("dac"))
    session_state = mapping(session)
    session_start = session_state.get("started_at") or sampled_at
    stream: dict[str, Any] = {
        "source_id": active_source,
        "label": SOURCE_LABELS.get(active_source, active_source),
        "started_at": session_start,
    }
    if resampler or camilla:
        stream["processing"] = {
            "summary": (
                "Adaptive resampling · shared DSP"
                if resampler else "Shared DSP path"
            ),
            "detail": "Configured processing route for this stream.",
            "details": [
                detail_row("DSP rate", f"{as_int(camilla.get('capture_rate')):,} Hz")
            ] if as_int(camilla.get("capture_rate")) else [],
        }
    if session_state:
        stream["session"] = dict(session_state)
    if active_source == Source.USBSINK.value:
        stream["latency"] = _receiver_latency(
            active_source,
            airplay,
            outputd,
            route,
            timing,
        )
    elif active_source == Source.AIRPLAY.value:
        airplay_timing = airplay_sync_timing(airplay, active=True)
        stream["latency"] = {
            "summary": airplay_timing["headline"],
            "detail": airplay_timing["detail"],
            "details": [],
        }
    if active_source == Source.USBSINK.value:
        rate = as_int(route.get("fixed_sample_rate"))
        if rate:
            stream["media"] = {
                "summary": f"{rate / 1000:g} kHz · Stereo PCM",
                "detail": "The format advertised by JTS to the connected USB host.",
                "details": [],
            }
    output_rate = as_int(dac.get("sample_rate"))
    output_details: list[dict[str, str]] = []
    dac_delay = fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        output_details.append(detail_row(
            "DAC queue",
            f"{dac_delay:.1f} ms",
        ))
    if outputd is not None and mapping(outputd).get("backend") == "alsa" and dac:
        stream["output"] = {
            "summary": (
                f"{output_rate / 1000:g} kHz final output"
                if output_rate else "Final output reporting"
            ),
            "detail": "Post-DSP audio at the physical output stage.",
            "details": output_details,
        }
    reliability = _reliability(
        mapping(fanin.get("output")), service_states, restart_watch_units,
    )
    if reliability["details"]:
        stream["reliability"] = reliability
    rms = finite_number(source_input.get("rms_dbfs"))
    if rms is not None:
        stream["signal"] = {
            "summary": f"{float(rms):.1f} dBFS recent signal level",
            "detail": "The most recent level measured on the source that is playing.",
            "details": [],
        }
    return stream
