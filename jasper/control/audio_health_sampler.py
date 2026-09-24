# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one resident audio-health sampling loop.

:class:`AudioHealthSampler` runs the fast (per-tick) and slow (route/topology,
60 s) cadences, sampling the AirPlay collector inline plus cheap local
outputd/mux STATUS reads, then feeds everything into
:func:`~jasper.control.audio_health.compose_audio_health` to produce the
snapshot management surfaces read. Only this loop's thread is resident; the
AirPlay collector is sampled inline from it.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from ..camilla_config_contract import DEFAULT_CAMILLA_PORT
from ..output_hardware import load_state as load_output_hardware_state
from ..platform import wire
from ..platform.status_socket import (
    MUX_CONTROL_SOCKET_PATH,
    OUTPUTD_STATUS_SOCKET, STATUS_MAX_BYTES, read_status_socket_or_none,
)
from ..platform.uds import mux_socket_command
from ..source_intent import read_source_intents
from .airplay_health import AirPlayHealthSampler, SAMPLE_INTERVAL_SEC
from ._health_fields import MONITOR_ERRORS, mapping
from .audio_attribution import input_attribution
from .audio_health import (
    RESTART_WATCH_UNITS,
    compose_audio_health,
    health_prelude,
)
from .audio_health_events import (
    CounterBaselines,
    record_counter_events,
    record_raw_events,
)
from .audio_incident_view import present_incident
from . import transport_eligibility
from .audio_incidents import IncidentStore, IssueTracker, SessionRollup
from .audio_route_claim import read_route_claim
from .audio_signal_path import (
    fanin_selected_source,
    parked_signal,
    undeclared_hardware_signal,
)
from .audio_state_issues import _state_issues
from .audio_stream_card import fresh_dac_delay_ms
from ..output_topology_store import load_output_topology_snapshot

logger = logging.getLogger(__name__)

ROUTE_INTERVAL_SEC = 60.0
LOCAL_STATUS_TIMEOUT_SEC = 1.0


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
    except MONITOR_ERRORS:
        logger.debug("audio health mux STATUS probe failed", exc_info=True)
        return None


def _read_output_hardware() -> Any:
    """Read the reconciler-published output-hardware record, fail-soft.

    Same reader ``/state.audio.output_hardware``
    (:mod:`jasper.control.state_aggregate`) and the ``/sound/speaker/``
    hardware-adoption precondition use. ``MONITOR_ERRORS`` degrades to "no
    record" rather than taking a health tick down.
    """
    try:
        return load_output_hardware_state()
    except MONITOR_ERRORS:
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

        return load_output_topology_snapshot()
    except MONITOR_ERRORS:
        logger.debug("audio health output-topology probe failed", exc_info=True)
        return None


def _incident_context(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
    system: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture persisted incident evidence."""
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    source_input = (
        mapping(mapping(fanin.get("inputs")).get(active_source))
        if active_source is not None else {}
    )
    output = mapping(mapping(outputd).get("dac"))
    host = mapping(system)
    context: dict[str, Any] = {
        "clock_mode": mapping(fanin.get("host_clock")).get("ladder"),
        "input": {"rms_dbfs": source_input.get("rms_dbfs")},
        "output": {"snd_pcm_delay_ms": fresh_dac_delay_ms(output)},
        # Why the box could not keep up, frozen with the incident: SoC
        # throttling and memory stall pressure are the two host conditions
        # that starve the audio path without leaving a trace in it.
        "host": {
            "throttled_now": host.get("throttled_now"),
            "throttled_history": host.get("throttled_history"),
            "mem_psi_some_avg60": host.get("mem_psi_some_avg60"),
        },
    }
    attribution = input_attribution(airplay, active_source)
    if attribution is not None:
        context["attribution"] = attribution
    return context


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
        # `undeclared_hardware_signal` for why revision matters.
        self._output_topology_snapshot: Any = None
        self._transport_park: dict[str, Any] | None = None
        self._service_states: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._last_route_sample_at = 0.0
        self._counter_baselines = CounterBaselines()
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
                "active_source": mapping(snapshot.get("overall")).get(
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
            previous_stream = mapping(snapshot.get("current_stream"))
            source_id = previous_stream.get("source_id") or mapping(
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
            snapshot["current_incident"] = present_incident(
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
            except MONITOR_ERRORS:
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
        except MONITOR_ERRORS:
            logger.debug("audio health outputd probe failed", exc_info=True)
            outputd = None
        try:
            mux_status = self._mux_probe()
        except MONITOR_ERRORS:
            logger.debug("audio health mux STATUS probe failed", exc_info=True)
            mux_status = None
        try:
            output_hardware = self._output_hardware_probe()
        except MONITOR_ERRORS:
            logger.debug("audio health output-hardware probe failed", exc_info=True)
            output_hardware = None
        if self._service_probe is not None:
            try:
                service_states = self._service_probe()
            except MONITOR_ERRORS:
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
            except MONITOR_ERRORS:
                logger.debug("audio health route probe failed", exc_info=True)
                route = {"status": "unavailable", "low_latency_claim": False}
            self._route = route if isinstance(route, dict) else None
            try:
                self._output_topology_snapshot = self._output_topology_probe()
            except MONITOR_ERRORS:
                logger.debug("audio health output-topology probe failed", exc_info=True)
                # Keep the previously cached snapshot: a transient read failure
                # must not blank the declared side of the B1/B2 comparison.
            # ADR-0178's transport parks ride the SLOW cadence with the
            # topology read they classify; their own snapshot() is fail-soft,
            # so a bad read lands as status="unavailable" rather than raising.
            self._transport_park = transport_eligibility.snapshot()
            self._last_route_sample_at = now

        route_state = mapping(self._route)
        active_source, activity_unknown, signal_path, latency = health_prelude(
            airplay, outputd, mux_status, route_state,
        )
        selected_source = fanin_selected_source(airplay)
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
        # Computed once here and passed to _state_issues below, so the incident
        # rows and the overall headline cannot present a different verdict for
        # the same tick: the raw path.outputd_unavailable row must not
        # contradict the headline when the setup hint wins (#2812).
        undeclared_hardware = undeclared_hardware_signal(
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
            coherence_park=parked_signal(route_state),
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
            raw_points = record_raw_events(
                self._counter_baselines,
                airplay,
                active_source=active_source,
                now=now,
            )
            counter_points, clipping_issue, preserve_clipping = record_counter_events(
                self._counter_baselines,
                airplay,
                outputd,
                now,
                session_source_id=self._session.source_id,
                service_states=self._service_states,
                restart_watch_units=RESTART_WATCH_UNITS,
                context=context,
            )
            for candidate, when, count, point_context, observed_at in (
                *raw_points, *counter_points,
            ):
                self._issues.record_point(
                    candidate,
                    when,
                    count=count,
                    context=point_context,
                    observed_at=observed_at,
                )
                self._session.record_point(candidate, when, count=count)
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
        except MONITOR_ERRORS:
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
        cached = self._transport_park
        if cached is not None:
            return cached
        return transport_eligibility.snapshot()
