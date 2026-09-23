# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The incident-row builder: signal-path and service-state facts, turned into
one ``/state``/``/system`` audio incident row per condition.

:func:`_state_issues` is what
:class:`~jasper.control.audio_health_sampler.AudioHealthSampler` calls every
fast tick, and what ADR-0178's parked-transport tests
(:mod:`tests.test_transport_eligibility`) call directly, one row per park
class. It only NAMES conditions already computed elsewhere — the signal-path
verdict, the transport-park snapshot, the setup hint — so two surfaces
reporting the same fact cannot drift apart (#2812).
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..music_sources import Source
from ..service_units import CAMILLA_SERVICE, unit_failed
from ._health_fields import _mapping
from ._health_sources import (
    SOURCE_OFF_DRIFT_DETAIL,
    SOURCE_UNAVAILABLE_DETAIL,
    _SOURCE_HEALTH_UNITS,
    _SOURCE_LABELS,
    _SOURCE_OFF_DRIFT_UNITS,
)
from .audio_incidents import issue_row
from .audio_signal_path import (
    ACTIVITY_UNKNOWN_DETAIL,
    PARKED_HEADLINE,
    PATH_UNREPORTED_DETAIL,
    PATH_UNREPORTED_TITLE,
    STOPPED_DSP_HEADLINE,
    _OUTPUT_ABSENT_DETAIL,
    _OUTPUT_ABSENT_TITLE,
    _camilla_stopped,
    _park_detail,
)


def _state_issues(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    signal_path: Mapping[str, Any],
    latency: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
    *,
    activity_unknown: bool = False,
    coherence_park: Mapping[str, Any] | None = None,
    undeclared_hardware: Mapping[str, Any] | None = None,
    transport_park: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    park_state = _mapping(transport_park)
    if coherence_park is not None and park_state.get("status") != "parked":
        issues.append(issue_row(
            "path.transport_parked",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(coherence_park.get("headline")),
            detail=str(coherence_park.get("detail")),
        ))
    # ADR-0178's named transport parks, one row per class so both surfaces see
    # EVERY tracked issue this box waits on rather than a first-match verdict.
    # Ahead of the live-daemon rows and OUTSIDE the warmup gate: a park is
    # structural, true at boot, and never cleared by a restart.
    if park_state.get("status") == "parked":
        for park in park_state.get("parks") or []:
            if not isinstance(park, Mapping):
                continue
            # The park CLASS rides the key; the row's detail is THIS class's
            # household sentence. The operator's raw detail and the remedy
            # command stay in doctor and `/system/snapshot`'s `transport_park`.
            park_class = str(park.get("park_class"))
            issues.append(issue_row(
                f"path.transport_park.{park_class}",
                scope="path",
                impact="continuity",
                severity="issue",
                title=PARKED_HEADLINE,
                detail=_park_detail([park]),
            ))
    warmup = bool(airplay.get("warmup_active"))
    current = _mapping(airplay.get("current"))
    fanin = current.get("fanin")
    if activity_unknown:
        issues.append(issue_row(
            "monitor.mux_status_unavailable",
            scope="monitor",
            impact="observability",
            severity="warn",
            title="Playback activity unavailable",
            detail=ACTIVITY_UNKNOWN_DETAIL,
        ))
    if not warmup and not isinstance(fanin, Mapping):
        issues.append(issue_row(
            "path.fanin_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title=PATH_UNREPORTED_TITLE,
            detail=PATH_UNREPORTED_DETAIL,
        ))
    if not warmup:
        camilla_stopped = _camilla_stopped(
            _mapping(service_states).get(CAMILLA_SERVICE)
        )
        if camilla_stopped is not None:
            issues.append(issue_row(
                "path.camilla_stopped",
                scope="path",
                impact="continuity",
                severity="issue",
                title=STOPPED_DSP_HEADLINE,
                detail=camilla_stopped[1],
            ))
    if not warmup and outputd is None:
        # When the setup hint fires for this exact condition the incident row
        # must say what the headline says, or the household sees a friendly
        # "finish setup" card next to a danger badge for the identical fact
        # (#2812). Both read the one `undeclared_hardware` value the sampler
        # passes in, so they cannot drift apart.
        if undeclared_hardware is not None:
            title = str(undeclared_hardware.get("headline"))
            detail = str(undeclared_hardware.get("detail"))
        else:
            title = _OUTPUT_ABSENT_TITLE
            detail = _OUTPUT_ABSENT_DETAIL
        issues.append(issue_row(
            "path.outputd_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title=title,
            detail=detail,
        ))
    # The rows below ARE their signal-path shape, so they carry its sentence
    # rather than a second copy of it: one writer per household sentence.
    path_code = signal_path.get("code")
    if path_code == "path_stalled":
        issues.append(issue_row(
            "path.fanin_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code == "output_deaf":
        issues.append(issue_row(
            "path.outputd_content_deaf",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code == "output_stalled":
        issues.append(issue_row(
            "path.outputd_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code == "output_backend_inactive":
        # Same alignment as path.outputd_unavailable above.
        if undeclared_hardware is not None:
            title = str(undeclared_hardware.get("headline"))
            detail = str(undeclared_hardware.get("detail"))
        else:
            title = str(signal_path.get("headline"))
            detail = str(signal_path.get("detail"))
        issues.append(issue_row(
            "path.outputd_backend_inactive",
            scope="path",
            impact="continuity",
            severity="issue",
            title=title,
            detail=detail,
        ))
    if path_code == "tts_queue_full":
        issues.append(issue_row(
            "path.tts_queue_full",
            scope="path",
            impact="continuity",
            severity="warn",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code in {"input_absent", "input_broken", "input_stalled"}:
        source_id = active_source
        issues.append(issue_row(
            f"{source_id or 'source'}.input_unavailable",
            scope="source",
            source_id=source_id,
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if active_source == Source.USBSINK.value:
        latency_runtime = _mapping(latency.get("runtime"))
        raw_mode = latency_runtime.get("raw_mode")
        if raw_mode == "l2_fallback" and latency_runtime.get("preset") != "high":
            issues.append(issue_row(
                "usbsink.latency_fallback",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB switched to stable latency fallback",
                detail="Playback continues safely with more buffering.",
            ))
        elif raw_mode == "l1_warn":
            issues.append(issue_row(
                "usbsink.clock_tracking_warn",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB clock tracking is under strain",
                detail="Playback is still in its low-delay mode.",
            ))
        if latency.get("status") == "unknown":
            issues.append(issue_row(
                "usbsink.latency_state_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB latency state unavailable",
                detail="JTS cannot check this computer's USB audio delay.",
            ))
        elif (
            _mapping(latency.get("runtime")).get("raw_mode")
            not in {"l0_locked", "l1_warn", "l2_fallback", "probing"}
        ):
            issues.append(issue_row(
                "usbsink.host_clock_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB low-latency clock mode unavailable",
                detail="Playback continues with standard buffering.",
            ))
    for source_id, health_units in _SOURCE_HEALTH_UNITS.items():
        desired = _mapping(source_intents).get(source_id)
        units = (
            _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
            if desired is False
            else health_units
        )
        for unit in units:
            unit_state = _mapping(service_states).get(unit)
            if desired is False:
                if _mapping(unit_state).get("active_state") == "active":
                    issues.append(issue_row(
                        f"{source_id}.service.{unit}.off_drift",
                        scope="source",
                        source_id=source_id,
                        impact="availability",
                        severity="issue",
                        title=(
                            f"{_SOURCE_LABELS.get(source_id, source_id)} "
                            "is running while Off"
                        ),
                        detail=SOURCE_OFF_DRIFT_DETAIL,
                    ))
                continue
            if not unit_failed(_mapping(unit_state)):
                continue
            issues.append(issue_row(
                f"{source_id}.service.{unit}",
                scope="source",
                source_id=source_id,
                impact="availability",
                severity="issue",
                title=f"{_SOURCE_LABELS.get(source_id, source_id)} is unavailable",
                detail=SOURCE_UNAVAILABLE_DETAIL,
            ))
    return issues
