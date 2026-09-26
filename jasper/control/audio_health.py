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
from .audio_incident_view import (
    incident_is_relevant,
    incident_priority,
    present_incident,
)
from .audio_signal_path import (
    activity_truth_unknown,
    activity_unavailable_signal,
    classify_signal_path,
    fanin_selected_source,
    parked_signal,
    resolve_active_source,
    stopped_dsp_signal,
    transport_park_signal,
    undeclared_hardware_signal,
)
from .audio_source_cards import (
    build_source_cards,
    not_applicable_timing,
    usb_timing,
)
from .audio_stream_card import build_current_stream

SCHEMA_VERSION = 1

# Signal-path codes that name a CONSEQUENCE rather than a cause, so a
# cause-naming detector may displace them (:func:`_yields_to_a_named_cause`).
# `output_deaf` is what a stopped DSP, a live coherence contradiction and a
# parked transport ALL look like from the DAC end: the lane is armed, nothing
# produces for it, so outputd zero-fills. `output_ring_stalled` is the same
# three seen from the other end — fan-in's ring stalls on `no_reader` when
# CamillaDSP is gone, so a named cause must still displace it.
_SYMPTOM_ONLY_CODES = frozenset({"output_deaf", "output_ring_stalled"})

# The two `classify_signal_path` codes that mean "outputd is not delivering
# audio, for a reason `classify_signal_path` cannot see": outputd never started
# at all (its missing-declaration `ExecCondition` kept the unit down, so its
# control socket never answers) or it is up but self-reports a non-ALSA backend
# (`action=park_until_active_graph` keeps sockets alive on a `fake` backend
# without opening ALSA). `undeclared_hardware_signal` refines only these two;
# every other concrete `classify_signal_path` issue is left untouched.
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
    :func:`_named_cause_path`: they claim only the ground where the path
    looks clean, plus :data:`_SYMPTOM_ONLY_CODES`. A concrete live fan-in /
    outputd failure still wins.
    """
    return (
        signal_path.get("status") != "issue"
        or signal_path.get("code") in _SYMPTOM_ONLY_CODES
    )


def health_prelude(
    ap: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    mux: Mapping[str, Any] | None,
    route_state: Mapping[str, Any],
) -> tuple[str | None, bool, dict[str, Any], dict[str, Any]]:
    """The read-and-classify steps :func:`compose_audio_health` and the
    sampler's ``_tick`` both need before their two paths diverge: this
    composer layers its cause-naming overrides onto the returned
    ``signal_path``, while the sampler passes this bare version straight to
    :func:`~jasper.control.audio_state_issues.state_issues` alongside those
    same overrides as separate arguments.

    ``mux`` and ``route_state`` are the already-resolved observations --
    each caller keeps its own fallback for producing them.
    """
    active_source = resolve_active_source(ap, mux)
    activity_unknown = activity_truth_unknown(ap, mux)
    signal_path = classify_signal_path(ap, outputd, active_source)
    if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
        signal_path = activity_unavailable_signal()
    fanin = _mapping(_mapping(ap.get("current")).get("fanin"))
    if active_source == Source.USBSINK.value:
        latency = usb_timing(
            route_state,
            _mapping(fanin.get("host_clock")) or None,
            _mapping(_mapping(fanin.get("inputs")).get(Source.USBSINK.value)),
            active=True,
        )
    else:
        latency = not_applicable_timing()
    return active_source, activity_unknown, signal_path, latency


def _named_cause_path(
    signal_path: dict[str, Any],
    ap: Mapping[str, Any],
    route_state: Mapping[str, Any],
    service_states: Mapping[str, Any] | None,
    transport_park: Mapping[str, Any] | None,
    output_hardware: Any,
    output_topology_snapshot: Any,
) -> dict[str, Any]:
    """``signal_path`` with the cause-naming detectors layered onto it."""
    # Rank order: a stopped DSP is fixed NOW by starting it, a live coherence
    # contradiction by changing the layout, a transport park only by rebuilding
    # the topology on the ring. Each outranks ok / warn / idle / unknown: the
    # box cannot emit audio at all, and absence of evidence should not hide that.
    for cause in (
        stopped_dsp_signal(ap, service_states),
        parked_signal(route_state),
        transport_park_signal(transport_park),
    ):
        if cause is not None and _yields_to_a_named_cause(signal_path):
            signal_path = cause
    undeclared_hardware = undeclared_hardware_signal(
        output_hardware, output_topology_snapshot
    )
    # Checked by CODE, not the `_yields_to_a_named_cause` guard above:
    # `classify_signal_path`'s outputd-absent/non-ALSA branch is already
    # "issue" status, so this refines its generic wording rather than
    # outranking a different concrete issue. Runs last, so `stopped_dsp`
    # and `parked` keep priority.
    if (
        undeclared_hardware is not None
        and signal_path.get("code") in _UNDECLARED_OUTPUT_CODES
    ):
        return undeclared_hardware
    return signal_path


def _verdict(
    signal_path: Mapping[str, Any],
    source_cards: list[dict[str, Any]],
    active_source: str | None,
    latency: Mapping[str, Any],
) -> tuple[str, str, str]:
    """The card's ``(status, headline, detail)``: the first rule that holds."""
    unavailable_sources = [
        str(source.get("label") or source.get("id")) for source in source_cards
        if source.get("status") == "issue" and source.get("id") == active_source
    ]
    path_status = str(signal_path.get("status") or "unknown")
    headline = str(signal_path.get("headline"))
    detail = str(signal_path.get("detail"))
    if path_status in {"issue", "unknown"}:
        return path_status, headline, detail
    if unavailable_sources:
        unavailable = ", ".join(unavailable_sources)
        return "warn", "A playback source needs attention", f"Unavailable: {unavailable}."
    if path_status == "warn":
        if active_source:
            return "warn", "Audio is playing", headline
        return "warn", headline, detail
    if path_status == "idle":
        return "idle", headline, detail
    if active_source is None:
        return "idle", "Audio is ready", "No source is playing."
    if latency.get("status") in {"warn", "unknown"}:
        return "warn", "Audio is playing", str(latency.get("headline"))
    label = SOURCE_LABELS.get(active_source, active_source)
    return "ok", "Audio is playing", f"{label} · sound path healthy."


def _overall(
    verdict: tuple[str, str, str],
    active_source: str | None,
    previous_overall: Mapping[str, Any] | None,
    sampled_at: float,
) -> dict[str, Any]:
    """The card's verdict, dated from when this same verdict began."""
    status, headline, detail = verdict
    previous = _mapping(previous_overall)
    same_overall = (
        previous.get("status") == status
        and previous.get("headline") == headline
        and previous.get("active_source") == active_source
    )
    return {
        "status": status,
        "headline": headline,
        "detail": detail,
        "active_source": active_source,
        "since": previous.get("since") if same_overall else sampled_at,
    }


def _incident_views(
    issues: list[dict[str, Any]], active_source: str | None, sampled_at: float,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The one incident the card leads with, and up to five others."""
    priority = lambda issue: incident_priority(issue, active_source)
    ongoing_issues = [
        issue for issue in issues
        if issue.get("status") == "ongoing"
        and incident_is_relevant(issue, active_source)
    ]
    ongoing = max(ongoing_issues, key=priority, default=None)
    current_incident = (
        None if ongoing is None else present_incident(ongoing, sampled_at, issues)
    )
    secondary_ongoing = sorted(
        (issue for issue in ongoing_issues if issue is not ongoing),
        key=priority,
        reverse=True,
    )
    recovered = [issue for issue in issues if issue.get("status") == "recovered"]
    recent_incidents = [
        present_incident(issue, sampled_at, issues)
        for issue in (*secondary_ongoing, *recovered)
    ][:5]
    return current_incident, recent_incidents


def _activity_unknown_stream(
    ap: Mapping[str, Any], session: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The stream card while mux cannot say what is playing."""
    selected = fanin_selected_source(ap)
    stream: dict[str, Any] = {
        "source_id": selected,
        "label": SOURCE_LABELS.get(selected or "", "Audio activity"),
        "signal": {
            "summary": "Playback state unavailable",
            "detail": "Waiting for a fresh reading of what is playing.",
            "details": [],
        },
    }
    if session is not None:
        stream["session"] = dict(session)
    return stream


def _technical(
    ap: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route_state: Mapping[str, Any],
) -> dict[str, Any]:
    """The daemons' own readings behind the household card."""
    current = _mapping(ap.get("current"))
    fanin = _mapping(current.get("fanin"))
    return {
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
    }


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
    snapshot, not the bare topology; see :func:`undeclared_hardware_signal`.

    ``transport_park`` is ``jasper.control.transport_eligibility.snapshot()`` (or
    ``None`` before the first slow-cadence read), passed in rather than read
    here: the incident rows and this headline must be the SAME tick's verdict,
    and it is a file read that belongs on the slow cadence.
    """
    ap = _mapping(airplay)
    route_state = _mapping(route)
    active_source, activity_unknown, signal_path, latency = health_prelude(
        ap, outputd, mux_status, route_state,
    )
    signal_path = _named_cause_path(
        signal_path, ap, route_state, service_states, transport_park,
        output_hardware, output_topology_snapshot,
    )
    source_cards = build_source_cards(
        ap, signal_path, route_state, active_source, service_states, source_intents,
    )
    overall = _overall(
        _verdict(signal_path, source_cards, active_source, latency),
        active_source, previous_overall, sampled_at,
    )
    current_incident, recent_incidents = _incident_views(issues, active_source, sampled_at)
    current_stream = (
        _activity_unknown_stream(ap, session) if activity_unknown
        else build_current_stream(
            active_source=active_source, airplay=ap, outputd=outputd,
            route=route_state, timing=latency, sampled_at=sampled_at,
            session=session, restart_watch_units=RESTART_WATCH_UNITS,
            service_states=service_states,
        )
    )
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
        "technical": _technical(ap, outputd, route_state),
    }
