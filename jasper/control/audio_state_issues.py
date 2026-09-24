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
from ._health_fields import mapping
from ._health_sources import (
    SOURCE_HEALTH_UNITS,
    SOURCE_LABELS,
    SOURCE_OFF_DRIFT_DETAIL,
    SOURCE_OFF_DRIFT_UNITS,
    SOURCE_UNAVAILABLE_DETAIL,
)
from .audio_incidents import issue_row
from .audio_signal_path import (
    ACTIVITY_UNKNOWN_DETAIL,
    OUTPUT_ABSENT_DETAIL,
    OUTPUT_ABSENT_TITLE,
    PARKED_HEADLINE,
    PATH_UNREPORTED_DETAIL,
    PATH_UNREPORTED_TITLE,
    STOPPED_DSP_HEADLINE,
    _park_detail,
    camilla_stopped_verdict,
)

# Signal-path codes whose incident row IS that path shape, so the row carries
# the path's own sentence rather than a second copy of it: one writer per
# household sentence. Value: the row's key and severity.
_PATH_CODE_ROWS = {
    "path_stalled": ("path.fanin_watchdog_stale", "issue"),
    "output_deaf": ("path.outputd_content_deaf", "issue"),
    "output_stalled": ("path.outputd_watchdog_stale", "issue"),
    "output_backend_inactive": ("path.outputd_backend_inactive", "issue"),
    "tts_queue_full": ("path.tts_queue_full", "warn"),
}
# ...and the codes that name the active source's input instead of the path.
_INPUT_PATH_CODES = frozenset({"input_absent", "input_broken", "input_stalled"})


def _sentence(signal: Mapping[str, Any]) -> tuple[str, str]:
    """A signal's ``(headline, detail)``, as an incident row's title and detail."""
    return str(signal.get("headline")), str(signal.get("detail"))


def _path_row(
    key: str, title: str, detail: str, severity: str = "issue",
) -> dict[str, Any]:
    return issue_row(
        key,
        scope="path",
        impact="continuity",
        severity=severity,
        title=title,
        detail=detail,
    )


def _undeclared_or(
    undeclared_hardware: Mapping[str, Any] | None, title: str, detail: str,
) -> tuple[str, str]:
    """The setup hint's sentence when it fires, else ``(title, detail)``.

    When the setup hint fires for an outputd-absent condition the incident row
    must say what the headline says, or the household sees a friendly "finish
    setup" card next to a danger badge for the identical fact (#2812). Both
    read the one ``undeclared_hardware`` value the sampler passes in, so they
    cannot drift apart.
    """
    if undeclared_hardware is not None:
        return _sentence(undeclared_hardware)
    return title, detail


def _park_rows(
    coherence_park: Mapping[str, Any] | None, park_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """ADR-0178's named transport parks, one row per class so both surfaces
    see EVERY tracked issue this box waits on rather than a first-match
    verdict -- or, with no named park, the live coherence contradiction.

    The park CLASS rides the key; the row's detail is THIS class's household
    sentence. The operator's raw detail and the remedy command stay in doctor
    and ``/system/snapshot``'s ``transport_park``.
    """
    if park_state.get("status") != "parked":
        if coherence_park is None:
            return []
        return [_path_row("path.transport_parked", *_sentence(coherence_park))]
    rows: list[dict[str, Any]] = []
    for park in park_state.get("parks") or []:
        if isinstance(park, Mapping):
            park_class = str(park.get("park_class"))
            rows.append(_path_row(
                f"path.transport_park.{park_class}",
                PARKED_HEADLINE,
                _park_detail([park]),
            ))
    return rows


def _daemon_rows(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    service_states: Mapping[str, Any] | None,
    undeclared_hardware: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """The shared-path daemons that are not running or not reporting."""
    rows: list[dict[str, Any]] = []
    if not isinstance(mapping(airplay.get("current")).get("fanin"), Mapping):
        rows.append(_path_row(
            "path.fanin_unavailable", PATH_UNREPORTED_TITLE, PATH_UNREPORTED_DETAIL,
        ))
    camilla_stopped = camilla_stopped_verdict(
        mapping(service_states).get(CAMILLA_SERVICE)
    )
    if camilla_stopped is not None:
        rows.append(_path_row(
            "path.camilla_stopped", STOPPED_DSP_HEADLINE, camilla_stopped[1],
        ))
    if outputd is None:
        rows.append(_path_row(
            "path.outputd_unavailable",
            *_undeclared_or(
                undeclared_hardware, OUTPUT_ABSENT_TITLE, OUTPUT_ABSENT_DETAIL,
            ),
        ))
    return rows


def _signal_path_rows(
    signal_path: Mapping[str, Any],
    active_source: str | None,
    undeclared_hardware: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """The row a signal-path shape IS, carrying that shape's own sentence."""
    code = signal_path.get("code")
    if code in _INPUT_PATH_CODES:
        title, detail = _sentence(signal_path)
        return [issue_row(
            f"{active_source or 'source'}.input_unavailable",
            scope="source",
            source_id=active_source,
            impact="continuity",
            severity="issue",
            title=title,
            detail=detail,
        )]
    if code not in _PATH_CODE_ROWS:
        return []
    key, severity = _PATH_CODE_ROWS[code]
    title, detail = _sentence(signal_path)
    if code == "output_backend_inactive":
        title, detail = _undeclared_or(undeclared_hardware, title, detail)
    return [_path_row(key, title, detail, severity)]


def _usb_latency_row(key: str, title: str, detail: str) -> dict[str, Any]:
    return issue_row(
        key,
        scope="latency",
        source_id=Source.USBSINK.value,
        impact="latency",
        severity="warn",
        title=title,
        detail=detail,
    )


def _usb_latency_rows(latency: Mapping[str, Any]) -> list[dict[str, Any]]:
    """USB host-clock rows: the timing axis, never a continuity failure."""
    runtime = mapping(latency.get("runtime"))
    raw_mode = runtime.get("raw_mode")
    rows: list[dict[str, Any]] = []
    if raw_mode == "l2_fallback" and runtime.get("preset") != "high":
        rows.append(_usb_latency_row(
            "usbsink.latency_fallback",
            "USB switched to stable latency fallback",
            "Playback continues safely with more buffering.",
        ))
    elif raw_mode == "l1_warn":
        rows.append(_usb_latency_row(
            "usbsink.clock_tracking_warn",
            "USB clock tracking is under strain",
            "Playback is still in its low-delay mode.",
        ))
    if latency.get("status") == "unknown":
        rows.append(_usb_latency_row(
            "usbsink.latency_state_unavailable",
            "USB latency state unavailable",
            "JTS cannot check this computer's USB audio delay.",
        ))
    elif raw_mode not in {"l0_locked", "l1_warn", "l2_fallback", "probing"}:
        rows.append(_usb_latency_row(
            "usbsink.host_clock_unavailable",
            "USB low-latency clock mode unavailable",
            "Playback continues with standard buffering.",
        ))
    return rows


def _source_service_rows(
    service_states: Mapping[str, Any] | None,
    source_intents: Mapping[str, bool] | None,
) -> list[dict[str, Any]]:
    """A local source's failed unit, or one still running while it is Off."""
    rows: list[dict[str, Any]] = []
    for source_id, health_units in SOURCE_HEALTH_UNITS.items():
        label = SOURCE_LABELS.get(source_id, source_id)
        off = mapping(source_intents).get(source_id) is False
        units = SOURCE_OFF_DRIFT_UNITS.get(source_id, ()) if off else health_units
        for unit in units:
            unit_state = mapping(mapping(service_states).get(unit))
            if off and unit_state.get("active_state") == "active":
                rows.append(issue_row(
                    f"{source_id}.service.{unit}.off_drift",
                    scope="source",
                    source_id=source_id,
                    impact="availability",
                    severity="issue",
                    title=f"{label} is running while Off",
                    detail=SOURCE_OFF_DRIFT_DETAIL,
                ))
            elif not off and unit_failed(unit_state):
                rows.append(issue_row(
                    f"{source_id}.service.{unit}",
                    scope="source",
                    source_id=source_id,
                    impact="availability",
                    severity="issue",
                    title=f"{label} is unavailable",
                    detail=SOURCE_UNAVAILABLE_DETAIL,
                ))
    return rows


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
    # Ahead of the live-daemon rows and OUTSIDE the warmup gate: a park is
    # structural, true at boot, and never cleared by a restart.
    issues = _park_rows(coherence_park, mapping(transport_park))
    warmup = bool(airplay.get("warmup_active"))
    if activity_unknown:
        issues.append(issue_row(
            "monitor.mux_status_unavailable",
            scope="monitor",
            impact="observability",
            severity="warn",
            title="Playback activity unavailable",
            detail=ACTIVITY_UNKNOWN_DETAIL,
        ))
    if not warmup:
        issues.extend(
            _daemon_rows(airplay, outputd, service_states, undeclared_hardware)
        )
    issues.extend(_signal_path_rows(signal_path, active_source, undeclared_hardware))
    if active_source == Source.USBSINK.value:
        issues.extend(_usb_latency_rows(latency))
    issues.extend(_source_service_rows(service_states, source_intents))
    return issues
