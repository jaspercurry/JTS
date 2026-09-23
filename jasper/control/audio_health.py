# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, normalized audio-health snapshot for management surfaces.

Composes the AirPlay collector -- which owns the expensive monitoring cadence
(fan-in STATUS, shairport/Camilla journals, MPRIS, Camilla status) -- with
cheap local outputd and mux STATUS reads plus a slow route-claim read.  Mux
owns the canonical per-source ``playing`` predicates; the dashboard does not
duplicate them.  Only
:class:`~jasper.control.audio_health_sampler.AudioHealthSampler`'s thread is
resident; the AirPlay collector is sampled inline.

Continuity and timing are separate axes.  A USB host-clock ``l2_fallback``
keeps audio playing safely, so it degrades the latency axis but does not
claim the signal path failed.  ``l0_locked`` is live clocking state, not an
end-to-end latency number; ``current_stream.latency`` is where the summed
queues are reported.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from ..music_sources import Source
from ..service_units import (
    CAMILLA_SERVICE,
    FANIN_SERVICE,
    OUTPUTD_SERVICE,
)
from ._health_fields import mapping as _mapping
from ._health_sources import SOURCE_LABELS
from .audio_attribution import _input_attribution
from .audio_incident_view import (
    _incident_is_relevant,
    _incident_priority,
    _present_incident,
)
from .audio_signal_path import (
    _active_source,
    _activity_truth_unknown,
    _activity_unavailable_signal,
    _parked_signal,
    _selected_source,
    _signal_path,
    _stopped_dsp_signal,
    _transport_park_signal,
    _undeclared_hardware_signal,
)
from .audio_source_cards import (
    _not_applicable_timing,
    _source_cards,
    _usb_timing,
)
from .audio_stream_card import _current_stream, _fresh_dac_delay_ms

SCHEMA_VERSION = 1

# Signal-path codes that name a CONSEQUENCE rather than a cause, so a
# cause-naming detector may displace them (:func:`_yields_to_a_named_cause`).
# `output_deaf` is what a stopped DSP, a live coherence contradiction and a
# parked transport ALL look like from the DAC end: the lane is armed, nothing
# produces for it, so outputd zero-fills. `output_ring_stalled` is the same
# three seen from the other end — fan-in's ring stalls on `no_reader` when
# CamillaDSP is gone, so a named cause must still displace it.
_SYMPTOM_ONLY_CODES = frozenset({"output_deaf", "output_ring_stalled"})

# The two `_signal_path` codes that mean "outputd is not delivering audio, for
# a reason `_signal_path` cannot see": outputd never started at all (its
# missing-declaration `ExecCondition` kept the unit down, so its control socket
# never answers) or it is up but self-reports a non-ALSA backend
# (`action=park_until_active_graph` keeps sockets alive on a `fake` backend
# without opening ALSA). `_undeclared_hardware_signal` refines only these two;
# every other concrete `_signal_path` issue is left untouched.
_UNDECLARED_OUTPUT_CODES = frozenset({"output_absent", "output_backend_inactive"})

# The shared-path units whose restart interrupts every source, and the incident
# key stem each one reports under (the stems `_likely_area` already classifies).
RESTART_WATCH_UNITS = {
    FANIN_SERVICE: "path.fanin",
    CAMILLA_SERVICE: "path.camilla",
    OUTPUTD_SERVICE: "path.outputd",
}


def _yields_to_a_named_cause(signal_path: Mapping[str, Any]) -> bool:
    """True when a cause-naming detector may replace this signal path.

    Shared by the three "the box cannot emit at all" detectors in
    :func:`compose_audio_health`: they claim only the ground where the path
    looks clean, plus :data:`_SYMPTOM_ONLY_CODES`. A concrete live fan-in /
    outputd failure still wins.
    """
    return (
        signal_path.get("status") != "issue"
        or signal_path.get("code") in _SYMPTOM_ONLY_CODES
    )


def _incident_context(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
    system: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture persisted incident evidence."""
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = (
        _mapping(_mapping(fanin.get("inputs")).get(active_source))
        if active_source is not None else {}
    )
    output = _mapping(_mapping(outputd).get("dac"))
    host = _mapping(system)
    context: dict[str, Any] = {
        "clock_mode": _mapping(fanin.get("host_clock")).get("ladder"),
        "input": {"rms_dbfs": source_input.get("rms_dbfs")},
        "output": {"snd_pcm_delay_ms": _fresh_dac_delay_ms(output)},
        # Why the box could not keep up, frozen with the incident: SoC
        # throttling and memory stall pressure are the two host conditions
        # that starve the audio path without leaving a trace in it.
        "host": {
            "throttled_now": host.get("throttled_now"),
            "throttled_history": host.get("throttled_history"),
            "mem_psi_some_avg60": host.get("mem_psi_some_avg60"),
        },
    }
    attribution = _input_attribution(airplay, active_source)
    if attribution is not None:
        context["attribution"] = attribution
    return context


def _health_prelude(
    ap: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    mux: Mapping[str, Any] | None,
    route_state: Mapping[str, Any],
) -> tuple[str | None, bool, dict[str, Any], dict[str, Any]]:
    """The read-and-classify steps :func:`compose_audio_health` and the
    sampler's ``_tick`` both need before their two paths diverge: this
    composer layers its cause-naming overrides onto the returned
    ``signal_path``, while the sampler passes this bare version straight to
    :func:`~jasper.control.audio_state_issues._state_issues` alongside those
    same overrides as separate arguments.

    ``mux`` and ``route_state`` are the already-resolved observations --
    each caller keeps its own fallback for producing them.
    """
    active_source = _active_source(ap, mux)
    activity_unknown = _activity_truth_unknown(ap, mux)
    signal_path = _signal_path(ap, outputd, active_source)
    if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
        signal_path = _activity_unavailable_signal()
    fanin = _mapping(_mapping(ap.get("current")).get("fanin"))
    if active_source == Source.USBSINK.value:
        latency = _usb_timing(
            route_state,
            _mapping(fanin.get("host_clock")) or None,
            _mapping(_mapping(fanin.get("inputs")).get(Source.USBSINK.value)),
            active=True,
        )
    else:
        latency = _not_applicable_timing()
    return active_source, activity_unknown, signal_path, latency


def compose_audio_health(
    *,
    airplay: Mapping[str, Any] | None,
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    issues: list[dict[str, Any]],
    sampled_at: float,
    previous_overall: Mapping[str, Any] | None = None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
    session: Mapping[str, Any] | None = None,
    mux_status: Mapping[str, Any] | None = None,
    output_hardware: Any = None,
    output_topology_snapshot: Any = None,
    transport_park: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the public, presentation-ready audio-health contract.

    ``output_hardware`` is an
    :class:`~jasper.output_hardware.OutputHardwareState` or ``None``, and
    ``output_topology_snapshot`` is a
    :class:`~jasper.output_topology_store.OutputTopologySnapshot` or ``None``
    (before the sampler's first slow-cadence read) — deliberately the
    snapshot, not the bare topology; see :func:`_undeclared_hardware_signal`.
    Both typed loosely because this module imports those layers lazily (same
    convention as ``topology`` in
    :func:`~jasper.control.audio_route_claim._transport_state`).

    ``transport_park`` is ``jasper.control.transport_eligibility.snapshot()`` (or
    ``None`` before the first slow-cadence read), passed in rather than read
    here: the incident rows and this headline must be the SAME tick's verdict,
    and it is a file read that belongs on the slow cadence.
    """
    ap = _mapping(airplay)
    route_state = _mapping(route)
    mux = mux_status
    active_source, activity_unknown, signal_path, latency = _health_prelude(
        ap, outputd, mux, route_state,
    )
    stopped_dsp = _stopped_dsp_signal(ap, service_states)
    if stopped_dsp is not None and _yields_to_a_named_cause(signal_path):
        # Ahead of both parked states: a daemon that is not running is
        # happening NOW and is fixed by starting it, while parked is persistent
        # and fixed by changing the layout.
        signal_path = stopped_dsp
    parked = _parked_signal(route_state)
    if parked is not None and _yields_to_a_named_cause(signal_path):
        # A verified structural fault outranks ok / warn / idle / unknown: the
        # box cannot emit audio at all, and absence of evidence should not hide
        # that.
        signal_path = parked
    transport_parked = _transport_park_signal(transport_park)
    if transport_parked is not None and _yields_to_a_named_cause(signal_path):
        # Last and most structural of the three: a live coherence contradiction
        # or a stopped daemon names something an operator can act on THIS boot,
        # while a transport park is cleared only by rebuilding the topology on
        # the ring.
        signal_path = transport_parked
    undeclared_hardware = _undeclared_hardware_signal(
        output_hardware, output_topology_snapshot
    )
    if (
        undeclared_hardware is not None
        and signal_path.get("code") in _UNDECLARED_OUTPUT_CODES
    ):
        # Checked by CODE, not the `_yields_to_a_named_cause` guard above:
        # `_signal_path`'s outputd-absent/non-ALSA branch is already "issue"
        # status, so this refines its generic wording rather than outranking a
        # different concrete issue. Runs last, so `stopped_dsp` and `parked`
        # keep priority.
        signal_path = undeclared_hardware
    current = _mapping(ap.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_cards = _source_cards(
        ap,
        signal_path,
        route_state,
        active_source,
        service_states,
        source_intents,
    )
    unavailable_sources = [
        str(source.get("label") or source.get("id"))
        for source in source_cards
        if source.get("status") == "issue"
        and source.get("id") == active_source
    ]

    path_status = str(signal_path.get("status") or "unknown")
    if path_status in {"issue", "unknown"}:
        overall_status = path_status
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif unavailable_sources:
        overall_status = "warn"
        headline = "A playback source needs attention"
        detail = f"Unavailable: {', '.join(unavailable_sources)}."
    elif path_status == "warn":
        overall_status = "warn"
        headline = "Audio is playing" if active_source else str(signal_path.get("headline"))
        detail = str(signal_path.get("headline") if active_source else signal_path.get("detail"))
    elif path_status == "idle":
        overall_status = "idle"
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif active_source is None:
        overall_status = "idle"
        headline = "Audio is ready"
        detail = "No source is playing."
    elif latency.get("status") in {"warn", "unknown"}:
        overall_status = "warn"
        headline = "Audio is playing"
        detail = str(latency.get("headline"))
    else:
        overall_status = "ok"
        headline = "Audio is playing"
        detail = (
            f"{SOURCE_LABELS.get(active_source, active_source)} · sound path healthy."
        )

    previous = _mapping(previous_overall)
    same_overall = (
        previous.get("status") == overall_status
        and previous.get("headline") == headline
        and previous.get("active_source") == active_source
    )
    since = previous.get("since") if same_overall else sampled_at
    overall = {
        "status": overall_status,
        "headline": headline,
        "detail": detail,
        "active_source": active_source,
        "since": since,
    }
    ongoing_issues = [
        issue for issue in issues
        if issue.get("status") == "ongoing"
        and _incident_is_relevant(issue, active_source)
    ]
    ongoing = max(
        ongoing_issues,
        key=lambda issue: _incident_priority(issue, active_source),
        default=None,
    )
    current_incident = (
        _present_incident(ongoing, sampled_at, issues)
        if ongoing is not None else None
    )
    secondary_ongoing = sorted(
        (issue for issue in ongoing_issues if issue is not ongoing),
        key=lambda issue: _incident_priority(issue, active_source),
        reverse=True,
    )
    recovered = [
        issue for issue in issues if issue.get("status") == "recovered"
    ]
    recent_incidents = [
        _present_incident(issue, sampled_at, issues)
        for issue in (*secondary_ongoing, *recovered)
    ][:5]
    current_stream = _current_stream(
        active_source=active_source,
        airplay=ap,
        outputd=outputd,
        route=route_state,
        timing=latency,
        sampled_at=sampled_at,
        session=session,
        restart_watch_units=RESTART_WATCH_UNITS,
        service_states=service_states,
    )
    if activity_unknown:
        selected = _selected_source(ap)
        current_stream = {
            "source_id": selected,
            "label": SOURCE_LABELS.get(selected or "", "Audio activity"),
            "signal": {
                "summary": "Playback state unavailable",
                "detail": "Waiting for a fresh reading of what is playing.",
                "details": [],
            },
        }
        if session is not None:
            current_stream["session"] = dict(session)
    return {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": sampled_at,
        "overall": overall,
        "signal_path": signal_path,
        "latency": latency,
        "sources": source_cards,
        "issues": copy.deepcopy(issues),
        "current_stream": current_stream,
        "current_incident": current_incident,
        "recent_incidents": recent_incidents,
        "technical": {
            "sampler": {
                "last_sample_at": ap.get("last_sample_at"),
                "warmup_active": bool(ap.get("warmup_active")),
                "suppressed_reason": ap.get("suppressed_reason"),
            },
            "fanin": {
                "available": bool(fanin.get("available")),
                "input_buffer_frames": fanin.get("input_buffer_frames"),
                "inputs": copy.deepcopy(fanin.get("inputs")),
                "host_clock": copy.deepcopy(fanin.get("host_clock")),
                "watchdog": copy.deepcopy(fanin.get("watchdog")),
                "output": copy.deepcopy(fanin.get("output")),
                "tts": copy.deepcopy(fanin.get("tts")),
            },
            "outputd": {
                "available": outputd is not None,
                "mix": copy.deepcopy(_mapping(outputd).get("mix")),
                "content": copy.deepcopy(_mapping(outputd).get("content")),
                "dac": copy.deepcopy(_mapping(outputd).get("dac")),
                "tts": copy.deepcopy(_mapping(outputd).get("tts")),
            },
            "route": {
                "route_id": route_state.get("route_id"),
                "route_config_hash": route_state.get("route_config_hash"),
            },
            "airplay": {
                "status": ap.get("status"),
                "reason": ap.get("reason"),
                "mpris": copy.deepcopy(current.get("mpris")),
                "camilla": copy.deepcopy(current.get("camilla")),
                "summary_5m": copy.deepcopy(ap.get("summary_5m")),
                "summary_30m": copy.deepcopy(ap.get("summary_30m")),
                "storm": copy.deepcopy(ap.get("storm")),
            },
            "link": copy.deepcopy(current.get("link")),
        },
    }
