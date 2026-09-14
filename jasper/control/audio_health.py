# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, normalized audio-health snapshot for management surfaces.

Composes the AirPlay collector -- which owns the expensive monitoring cadence
(fan-in STATUS, shairport/Camilla journals, MPRIS, Camilla status) -- with
cheap local outputd and mux STATUS reads plus a slow route-claim read.  Mux
owns the canonical per-source ``playing`` predicates; the dashboard does not
duplicate them.  Only :class:`AudioHealthSampler`'s thread is resident; the
AirPlay collector is sampled inline.

Continuity and timing are separate axes.  A USB host-clock ``l2_fallback``
keeps audio playing safely, so it degrades the latency axis but does not
claim the signal path failed.  ``l0_locked`` is live clocking state, not an
end-to-end latency number; ``current_stream.latency`` is where the summed
queues are reported.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from ..camilla_config_contract import DEFAULT_CAMILLA_PORT
from ..music_sources import Source
from ..platform.status_socket import (
    MUX_CONTROL_SOCKET_PATH,
    OUTPUTD_STATUS_SOCKET, STATUS_MAX_BYTES, read_status_socket_or_none,
)
from ..service_units import (
    FANIN_SERVICE,
    OUTPUTD_SERVICE,
)
from ..source_intent import read_source_intents
from .airplay_health import (
    CAMILLA_UNIT_FULL,
    AirPlayHealthSampler,
    SAMPLE_INTERVAL_SEC,
)
from ._health_fields import (
    _MONITOR_ERRORS,
    _as_int,
    _finite_number,
    _mapping,
    _nonnegative_counter,
)
from ._health_sources import _SOURCE_LABELS
from .audio_attribution import _input_attribution
from .audio_incident_view import (
    _incident_is_relevant,
    _incident_priority,
    _present_incident,
)
from .audio_incidents import IncidentStore, IssueTracker, SessionRollup, issue_row
from .audio_route_claim import read_route_claim
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
# Re-export only: this module has no internal use of PARKED_DETAIL, but
# tests/test_transport_eligibility.py still imports it from here. #4718's
# split tracker moves that import to audio_signal_path directly.
from .audio_signal_path import PARKED_DETAIL as PARKED_DETAIL
from .audio_source_cards import (
    _not_applicable_timing,
    _source_cards,
    _usb_timing,
)
from .audio_state_issues import _state_issues
from .audio_stream_card import _current_stream, _fresh_dac_delay_ms
from ..platform import wire
from ..platform.uds import mux_socket_command

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROUTE_INTERVAL_SEC = 60.0
LOCAL_STATUS_TIMEOUT_SEC = 1.0

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
# Public: `jasper.control.heal_supervisor` stands down when one is not active.
RESTART_WATCH_UNITS = {
    FANIN_SERVICE: "path.fanin",
    CAMILLA_UNIT_FULL: "path.camilla",
    OUTPUTD_SERVICE: "path.outputd",
}


def _read_local_status(
    socket_path: str = OUTPUTD_STATUS_SOCKET,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
    max_bytes: int = STATUS_MAX_BYTES,
) -> dict[str, Any] | None:
    """Read one local daemon STATUS response, byte/time bounded and fail-soft."""
    return read_status_socket_or_none(
        socket_path,
        timeout=timeout_sec,
        max_bytes=max_bytes,
        event="audio_health.local_status_unavailable",
    )


def _read_mux_status(
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    """Read mux's already-normalized source activity over its local UDS."""
    try:
        return asyncio.run(
            mux_socket_command(
                wire.STATUS,
                socket_path=socket_path,
                timeout=timeout_sec,
            )
        )
    except _MONITOR_ERRORS:
        logger.debug("audio health mux STATUS probe failed", exc_info=True)
        return None


def _read_output_hardware() -> Any:
    """Read the reconciler-published output-hardware record, fail-soft.

    Same reader ``/state.audio.output_hardware``
    (:mod:`jasper.control.state_aggregate`) and the ``/sound/speaker/``
    hardware-adoption precondition use. ``_MONITOR_ERRORS`` degrades to "no
    record" rather than taking a health tick down; a broken import is
    deliberately NOT in that set — it would fail identically on every call from
    process start, so it is a startup bug, not a per-tick condition.
    """
    try:
        from ..output_hardware import load_state

        return load_state()
    except _MONITOR_ERRORS:
        logger.debug("audio health output-hardware probe failed", exc_info=True)
        return None


def _read_output_topology() -> Any:
    """Read the DECLARED output topology's SNAPSHOT (topology + revision),
    fail-soft.

    The SNAPSHOT, not the bare ``load_output_topology`` (#2812 B2): on a
    missing file both readers fall back to ``new_topology_draft``, which
    auto-seeds ``hardware`` FROM the observed record whenever it has outputs,
    so an ``OutputTopology`` alone cannot distinguish "never declared" from
    "declared and already matches". ``snapshot.revision == "missing"`` survives
    that auto-seed and says nothing was ever persisted. Same reader
    ``/sound/speaker/`` uses (``jasper.web.sound_active_speaker._output_topology_payload``).
    """
    try:
        from ..output_topology import load_output_topology_snapshot

        return load_output_topology_snapshot()
    except _MONITOR_ERRORS:
        logger.debug("audio health output-topology probe failed", exc_info=True)
        return None


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
    """Capture only the evidence rendered on a persisted incident."""
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
    :class:`~jasper.output_topology.OutputTopologySnapshot` or ``None``
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
    mux = mux_status if mux_status is not None else _mapping(ap.get("mux_status"))
    active_source = _active_source(ap, mux)
    activity_unknown = _activity_truth_unknown(ap, mux)
    signal_path = _signal_path(ap, outputd, active_source)
    if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
        signal_path = _activity_unavailable_signal()
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
    inputs = _mapping(fanin.get("inputs"))
    host_clock = _mapping(fanin.get("host_clock")) or None
    if active_source == Source.USBSINK.value:
        latency = _usb_timing(
            route_state,
            host_clock,
            _mapping(inputs.get(Source.USBSINK.value)),
            active=True,
        )
    else:
        latency = _not_applicable_timing()
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
            f"{_SOURCE_LABELS.get(active_source, active_source)} · sound path healthy."
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
            "label": _SOURCE_LABELS.get(selected or "", "Audio activity"),
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


class AudioHealthSampler:
    """The one production audio-health loop, with bounded in-memory history."""

    def __init__(
        self,
        *,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
        route_interval_sec: float = ROUTE_INTERVAL_SEC,
        airplay_sampler: AirPlayHealthSampler | Any | None = None,
        outputd_probe: Callable[[], dict[str, Any] | None] | None = None,
        mux_probe: Callable[[], dict[str, Any] | None] | None = None,
        route_probe: Callable[[], dict[str, Any]] | None = None,
        service_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
        system_probe: Callable[[], Mapping[str, Any] | None] | None = None,
        output_hardware_probe: Callable[[], Any] | None = None,
        output_topology_probe: Callable[[], Any] | None = None,
        incident_store: IncidentStore | None = None,
        time_fn: Callable[[], float] = time.time,
        camilla_host: str = "127.0.0.1",
        camilla_port: int = DEFAULT_CAMILLA_PORT,
    ) -> None:
        self._sample_interval = sample_interval_sec
        self._route_interval = route_interval_sec
        self._time = time_fn
        self._airplay = airplay_sampler or AirPlayHealthSampler(
            camilla_host=camilla_host,
            camilla_port=camilla_port,
            time_fn=time_fn,
        )
        self._outputd_probe = outputd_probe or _read_local_status
        self._mux_probe = mux_probe or _read_mux_status
        self._route_probe = route_probe or read_route_claim
        self._service_probe = service_probe
        self._system_probe = system_probe
        self._output_hardware_probe = output_hardware_probe or _read_output_hardware
        self._output_topology_probe = output_topology_probe or _read_output_topology
        observation_gap = max(15.0, sample_interval_sec * 3.0)
        self._issues = IssueTracker(
            store=incident_store,
            max_observation_gap_sec=observation_gap,
        )
        self._session = SessionRollup(
            max_observation_gap_sec=observation_gap,
        )
        self._outputd: dict[str, Any] | None = None
        self._route: dict[str, Any] | None = None
        # Refreshed on the slow `_route_interval` cadence, not every fast tick:
        # declared topology changes only when a household saves a new layout. A
        # SNAPSHOT (topology + revision), not a bare topology -- see
        # `_undeclared_hardware_signal` for why revision matters.
        self._output_topology_snapshot: Any = None
        self._transport_park: dict[str, Any] | None = None
        self._service_states: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._last_route_sample_at = 0.0
        self._previous_input_xruns: dict[str, int] | None = None
        self._previous_usb_buffer_counts: tuple[int, int] | None = None
        self._previous_fanin_pings_skipped: int | None = None
        self._previous_outputd_xruns: dict[str, int] | None = None
        self._previous_service_restarts: dict[str, int | None] | None = None
        self._previous_outputd_clipped: int | None = None
        self._seen_raw_events: deque[tuple[Any, ...]] = deque(maxlen=40)
        self._seen_raw_event_set: set[tuple[Any, ...]] = set()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="jasper-audio-health-sampler",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stopped = True

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if snapshot is None:
            return None
        sampled_at = snapshot.get("sampled_at")
        stale_after = max(15.0, self._sample_interval * 3.0)
        if (
            isinstance(sampled_at, (int, float))
            and self._time() - float(sampled_at) > stale_after
        ):
            stale_since = float(sampled_at) + stale_after
            snapshot["overall"] = {
                "status": "unknown",
                "headline": "Audio monitor is stale",
                "detail": "The last health sample is no longer current.",
                "active_source": _mapping(snapshot.get("overall")).get(
                    "active_source",
                ),
                "since": stale_since,
            }
            snapshot["signal_path"] = {
                "status": "unknown",
                "headline": "Audio health unavailable",
                "detail": "The monitor has not completed a fresh sample.",
            }
            stale_issue = {
                "key": "monitor.sample_stale",
                "scope": "monitor",
                "source_id": None,
                "impact": "observability",
                "severity": "issue",
                "title": "Audio monitor is stale",
                "detail": "Current audio health cannot be confirmed.",
                "status": "ongoing",
                "started_at": stale_since,
                "last_seen_at": self._time(),
                "recovered_at": None,
                "count": 1,
                "first_occurrence_at": stale_since,
                "last_occurrence_at": stale_since,
            }
            issues = list(snapshot.get("issues") or [])
            issues.insert(0, stale_issue)
            snapshot["issues"] = issues
            previous_stream = _mapping(snapshot.get("current_stream"))
            source_id = previous_stream.get("source_id") or _mapping(
                snapshot.get("overall")
            ).get("active_source")
            snapshot["current_stream"] = {
                "source_id": source_id,
                "label": previous_stream.get("label") or "Audio",
                "started_at": stale_since,
                "signal": {
                    "summary": "Current stream details unavailable",
                    "detail": "The audio monitor has not completed a fresh sample.",
                    "details": [],
                },
            }
            snapshot["current_incident"] = _present_incident(
                stale_issue,
                self._time(),
                issues,
            )
        return snapshot

    def airplay_snapshot(self) -> dict[str, Any]:
        """Compatibility surface for the existing ``airplay_health`` payload."""
        return self._airplay.snapshot()

    def airplay_playing(self) -> bool | None:
        """shairport's MPRIS PlaybackStatus for `/state`, from the sample this
        object already holds. None when unknown or not yet sampled."""
        return self._airplay.airplay_streaming()

    def outputd_snapshot(self) -> dict[str, Any] | None:
        """Reuse the cached outputd observation in ``/system/snapshot``."""
        with self._lock:
            return copy.deepcopy(self._outputd)

    def _run(self) -> None:
        while not self._stopped:
            started = time.monotonic()
            try:
                self._tick()
            except _MONITOR_ERRORS:
                logger.exception("audio health sampler tick failed")
            elapsed = time.monotonic() - started
            # Floor bounds the loop rate when a tick overruns the interval,
            # so a slow tick under load can't collapse it to a tight spin.
            time.sleep(max(1.0, self._sample_interval - elapsed))

    def _tick(self) -> None:
        now = self._time()
        self._airplay.sample_once()
        airplay = self._airplay.snapshot()
        try:
            outputd = self._outputd_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health outputd probe failed", exc_info=True)
            outputd = None
        try:
            mux_status = self._mux_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health mux STATUS probe failed", exc_info=True)
            mux_status = None
        try:
            output_hardware = self._output_hardware_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health output-hardware probe failed", exc_info=True)
            output_hardware = None
        if mux_status is None and isinstance(airplay.get("mux_status"), Mapping):
            # Explicit fixture/injected observation seam; production AirPlay
            # snapshots do not carry mux state and therefore still fail closed.
            mux_status = dict(airplay["mux_status"])
        if self._service_probe is not None:
            try:
                service_states = self._service_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health service-state probe failed", exc_info=True)
            else:
                if isinstance(service_states, dict):
                    self._service_states = service_states
        if (
            self._route is None
            or now - self._last_route_sample_at >= self._route_interval
        ):
            try:
                route = self._route_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health route probe failed", exc_info=True)
                route = {"status": "unavailable", "low_latency_claim": False}
            self._route = route if isinstance(route, dict) else None
            try:
                self._output_topology_snapshot = self._output_topology_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health output-topology probe failed", exc_info=True)
                # Keep the previously cached snapshot: a transient read failure
                # must not blank the declared side of the B1/B2 comparison.
            # ADR-0178's transport parks ride the SLOW cadence with the
            # topology read they classify; their own snapshot() is fail-soft,
            # so a bad read lands as status="unavailable" rather than raising.
            # Imported here, not at module scope, so the name cannot shadow the
            # `transport_park` PARAMETER the composers below take.
            from . import transport_eligibility as transport_park_reader

            self._transport_park = transport_park_reader.snapshot()
            self._last_route_sample_at = now

        active_source = _active_source(airplay, mux_status)
        activity_unknown = _activity_truth_unknown(airplay, mux_status)
        selected_source = _selected_source(airplay)
        if activity_unknown:
            if (
                self._session.source_id is not None
                and selected_source != self._session.source_id
            ):
                self._session.reset(None, now)
        elif active_source != self._session.source_id:
            self._session.reset(active_source, now)
        context = _incident_context(
            airplay, outputd, active_source, self._read_system_pressure(),
        )
        try:
            intents = {
                source.value: enabled
                for source, enabled in read_source_intents().items()
            }
        except RuntimeError:
            logger.debug("audio health source-intent probe failed", exc_info=True)
            intents = None
        signal_path = _signal_path(airplay, outputd, active_source)
        if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
            signal_path = _activity_unavailable_signal()
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        inputs = _mapping(fanin.get("inputs"))
        host_clock = _mapping(fanin.get("host_clock")) or None
        if active_source == Source.USBSINK.value:
            latency = _usb_timing(
                _mapping(self._route),
                host_clock,
                _mapping(inputs.get(Source.USBSINK.value)),
                active=True,
            )
        else:
            latency = _not_applicable_timing()
        # Computed once here and passed to _state_issues below, so the incident
        # rows and the overall headline cannot present a different verdict for
        # the same tick: the raw path.outputd_unavailable row must not
        # contradict the headline when the setup hint wins (#2812).
        undeclared_hardware = _undeclared_hardware_signal(
            output_hardware, self._output_topology_snapshot
        )
        state_issues = _state_issues(
            airplay,
            outputd,
            signal_path,
            latency,
            active_source,
            self._service_states,
            intents,
            activity_unknown=activity_unknown,
            coherence_park=_parked_signal(_mapping(self._route)),
            undeclared_hardware=undeclared_hardware,
            transport_park=self._transport_park,
        )
        tracked_state_issues = [
            issue for issue in state_issues
            if not (
                issue.get("impact") == "availability"
                and issue.get("source_id") != active_source
            )
        ]
        with self._issues.batch(now):
            self._record_raw_events(
                airplay,
                active_source=active_source,
                now=now,
            )
            clipping_issue, preserve_clipping = self._record_counter_events(
                airplay,
                outputd,
                now,
                context=context,
            )
            if clipping_issue is not None:
                tracked_state_issues.append(clipping_issue)
            preserve_unseen_keys: set[str] = set()
            if preserve_clipping:
                preserve_unseen_keys.add("path.outputd_clipping")
            if (
                activity_unknown
                and self._session.source_id is not None
                and selected_source == self._session.source_id
            ):
                preserve_unseen_keys.update(
                    str(issue["key"])
                    for issue in self._issues.snapshot()
                    if issue.get("status") == "ongoing"
                    and issue.get("source_id") == self._session.source_id
                )
            self._issues.update(
                tracked_state_issues,
                now,
                context=context,
                preserve_unseen_keys=preserve_unseen_keys,
            )
        self._session.observe_state(
            tracked_state_issues,
            now,
            preserve_unseen_keys=preserve_unseen_keys,
        )
        with self._lock:
            previous_overall = (
                self._snapshot.get("overall")
                if isinstance(self._snapshot, dict)
                else None
            )
            self._outputd = copy.deepcopy(outputd)
            self._snapshot = compose_audio_health(
                airplay=airplay,
                outputd=outputd,
                route=self._route,
                issues=self._issues.snapshot(),
                sampled_at=now,
                previous_overall=previous_overall,
                service_states=self._service_states,
                source_intents=intents,
                session=self._session.snapshot(now),
                mux_status=mux_status,
                output_hardware=output_hardware,
                output_topology_snapshot=self._output_topology_snapshot,
                transport_park=self._transport_park,
            )

    def _read_system_pressure(self) -> Mapping[str, Any] | None:
        if self._system_probe is None:
            return None
        try:
            pressure = self._system_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health system-pressure probe failed", exc_info=True)
            return None
        return pressure if isinstance(pressure, Mapping) else None

    def transport_park_snapshot(self) -> dict[str, Any]:
        """The transport-park verdict THIS sampler last computed.

        ``/state`` reads it from here rather than calling
        ``transport_eligibility.snapshot()`` again: the incident rows and the
        signal-path headline in the same payload were built from this cached
        value, and a fresher read would let one response disagree with itself —
        the box parked in ``resilience`` and playing in ``audio_health``.

        Falls back to a fresh read only before the first slow tick.
        """
        from . import transport_eligibility as transport_park_reader

        cached = self._transport_park
        if cached is not None:
            return cached
        return transport_park_reader.snapshot()

    def _record_point(
        self,
        candidate: dict[str, Any],
        when: float,
        *,
        count: int,
        context: Mapping[str, Any] | None,
        observed_at: float | None = None,
    ) -> None:
        self._issues.record_point(
            candidate,
            when,
            count=count,
            context=context,
            observed_at=observed_at,
        )
        self._session.record_point(candidate, when, count=count)

    def _record_raw_events(
        self,
        airplay: Mapping[str, Any],
        *,
        active_source: str | None,
        now: float,
    ) -> None:
        for raw in airplay.get("events") or []:
            if not isinstance(raw, Mapping):
                continue
            fingerprint = (
                raw.get("ts"), raw.get("type"), raw.get("count"), raw.get("detail")
            )
            if fingerprint in self._seen_raw_event_set:
                continue
            if len(self._seen_raw_events) == self._seen_raw_events.maxlen:
                oldest = self._seen_raw_events.popleft()
                self._seen_raw_event_set.discard(oldest)
            self._seen_raw_events.append(fingerprint)
            self._seen_raw_event_set.add(fingerprint)
            event_type = str(raw.get("type") or "")
            if event_type == "camilla_short_read":
                # Documented inaudible recovered partials are technical evidence,
                # not a household issue. A playback underrun is surfaced below.
                continue
            if (
                event_type == "fanin_airplay_xrun"
                and active_source != Source.AIRPLAY.value
            ):
                continue
            if event_type == "camilla_playback_underrun":
                candidate = issue_row(
                    f"path.{event_type}",
                    scope="path",
                    impact="continuity",
                    severity="issue",
                    title=str(raw.get("title") or "Audio path recovered"),
                    detail=str(raw.get("detail") or "The shared path recovered."),
                )
            elif event_type.startswith("shairport_") or event_type == "fanin_airplay_xrun":
                impact = "sync" if event_type in {
                    "shairport_packet_drop",
                    "shairport_oos",
                    "shairport_sync_positive",
                    "shairport_sync_negative",
                    "shairport_offset_too_short",
                } else "continuity"
                candidate = issue_row(
                    f"airplay.{event_type}",
                    scope="source",
                    source_id=Source.AIRPLAY.value,
                    impact=impact,
                    severity=(
                        "issue" if raw.get("severity") == "issue" else "warn"
                    ),
                    title=str(raw.get("title") or "AirPlay recovered"),
                    detail=str(raw.get("detail") or "AirPlay recovered."),
                )
            else:
                continue
            event_time = _finite_number(raw.get("ts"))
            when = float(event_time) if event_time is not None else now
            self._record_point(
                candidate,
                when,
                count=_as_int(raw.get("count"), 1),
                context=None,
                observed_at=now,
            )

    def _record_counter_events(
        self,
        airplay: Mapping[str, Any],
        outputd: Mapping[str, Any] | None,
        now: float,
        *,
        context: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        watchdog = _mapping(fanin.get("watchdog"))
        pings_skipped = _as_int(watchdog.get("pings_skipped"))
        if self._previous_fanin_pings_skipped is not None:
            skipped_delta = pings_skipped - self._previous_fanin_pings_skipped
            if skipped_delta > 0:
                self._record_point(
                    issue_row(
                        "path.fanin_watchdog_recovered",
                        scope="path",
                        impact="continuity",
                        severity="issue",
                        title="Sound recovered after a brief pause",
                        # No number in the sentence: `skipped_delta` counts
                        # watchdog ticks missed — how LONG one stall lasted,
                        # not how many stalls there were. It rides the
                        # structured `count` field below instead.
                        detail=(
                            "Sound stopped moving through the speaker briefly "
                            "and resumed."
                        ),
                    ),
                    now,
                    count=skipped_delta,
                    context=context,
                )
        self._previous_fanin_pings_skipped = pings_skipped
        inputs = _mapping(fanin.get("inputs"))
        input_counts = {
            source_id: _as_int(_mapping(observation).get("xrun_count"))
            for source_id, observation in inputs.items()
            if isinstance(source_id, str)
            and bool(_mapping(observation).get("present"))
        }
        if self._previous_input_xruns is not None:
            for source_id, count in input_counts.items():
                if (
                    source_id == Source.AIRPLAY.value
                    or source_id != self._session.source_id
                ):
                    continue  # AirPlay has its own events; idle lanes are noise.
                previous = self._previous_input_xruns.get(source_id, count)
                delta = count - previous
                if delta > 0:
                    self._record_point(
                        issue_row(
                            f"{source_id}.input_xrun",
                            scope="source",
                            source_id=source_id,
                            impact="continuity",
                            severity="issue",
                            title=f"{_SOURCE_LABELS.get(source_id, source_id)} input recovered",
                            detail=f"The input recovered {delta} interruption(s).",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_input_xruns = input_counts

        usb_input = _mapping(inputs.get(Source.USBSINK.value))
        unlocks = _nonnegative_counter(
            _mapping(usb_input.get("resampler")).get("unlock_count")
        )
        stream_stops = _nonnegative_counter(
            _mapping(usb_input.get("direct")).get("stream_stops")
        )
        if unlocks is None or stream_stops is None:
            self._previous_usb_buffer_counts = None
        else:
            previous_usb = self._previous_usb_buffer_counts
            if previous_usb is not None:
                unlock_delta = unlocks - previous_usb[0]
                stop_delta = stream_stops - previous_usb[1]
                if (
                    unlock_delta > 0
                    and stop_delta >= 0
                    and self._session.source_id == Source.USBSINK.value
                ):
                    unexpected = max(0, unlock_delta - stop_delta)
                    if unexpected:
                        self._record_point(
                            issue_row(
                                "usbsink.latency_buffer_underfill",
                                scope="source",
                                source_id=Source.USBSINK.value,
                                impact="continuity",
                                severity="issue",
                                title="USB input buffer ran dry",
                                detail=(
                                    "USB audio arrived too late for the selected "
                                    "buffer. JTS refilled it and resumed playback."
                                ),
                            ),
                            now,
                            count=unexpected,
                            context=context,
                        )
            self._previous_usb_buffer_counts = (unlocks, stream_stops)

        # None, not 0, for a unit systemd could not be asked about: a probe
        # that failed and recovered would otherwise read as a restart burst.
        restarts = {
            unit: _nonnegative_counter(
                _mapping(self._service_states.get(unit)).get("n_restarts"),
            )
            for unit in RESTART_WATCH_UNITS
        }
        if self._previous_service_restarts is not None:
            for unit, stem in RESTART_WATCH_UNITS.items():
                previous_restarts = self._previous_service_restarts.get(unit)
                current_restarts = restarts[unit]
                if previous_restarts is None or current_restarts is None:
                    continue
                delta = current_restarts - previous_restarts
                if delta > 0:
                    self._record_point(
                        issue_row(
                            f"{stem}.restarted",
                            scope="path",
                            impact="continuity",
                            severity="issue",
                            title="Sound restarted itself",
                            detail=(
                                "Part of the speaker's sound handling restarted, "
                                "so playback was interrupted for a moment."
                            ),
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_service_restarts = restarts

        if outputd is None:
            self._previous_outputd_xruns = None
            self._previous_outputd_clipped = None
            return None, True
        outputd_map = _mapping(outputd)
        clipping_issue: dict[str, Any] | None = None
        clipped_samples = _nonnegative_counter(
            _mapping(outputd_map.get("mix")).get("clipped_samples"),
        )
        preserve_clipping = False
        if clipped_samples is None:
            self._previous_outputd_clipped = None
            preserve_clipping = True
        elif self._previous_outputd_clipped is None:
            self._previous_outputd_clipped = clipped_samples
            preserve_clipping = True
        else:
            clipped_delta = clipped_samples - self._previous_outputd_clipped
            if clipped_delta > 0:
                clipping_issue = issue_row(
                    "path.outputd_clipping",
                    scope="path",
                    impact="quality",
                    severity="issue",
                    title="Audio clipping detected",
                    detail=(
                        f"JTS observed {clipped_delta} clipped sample(s) "
                        "in the latest output interval."
                    ),
                )
            elif clipped_delta < 0:
                # A daemon restart/reset establishes a new baseline; it does
                # not prove that an already-observed episode recovered.
                preserve_clipping = True
            self._previous_outputd_clipped = clipped_samples
        outputd_counts = {
            "content": _as_int(_mapping(outputd_map.get("content")).get("xrun_count")),
            "dac": _as_int(_mapping(outputd_map.get("dac")).get("xrun_count")),
        }
        if self._previous_outputd_xruns is not None:
            for stage, count in outputd_counts.items():
                previous = self._previous_outputd_xruns.get(stage, count)
                delta = count - previous
                if delta > 0:
                    title = (
                        "Sound to the speaker recovered"
                        if stage == "dac" else "Music path recovered"
                    )
                    self._record_point(
                        issue_row(
                            f"path.outputd_{stage}_xrun",
                            scope="path",
                            impact="continuity",
                            severity="issue",
                            title=title,
                            detail=f"Sound was interrupted {delta} time(s) and resumed.",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_outputd_xruns = outputd_counts
        return clipping_issue, preserve_clipping
