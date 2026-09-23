# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Turn a persisted issue record into the presentation-ready incident shape
the dashboard renders: impact sentence, likely area, evidence rows, and the
30-minute recurrence rollup.

:func:`~jasper.control.audio_health.compose_audio_health` and
:class:`~jasper.control.audio_health_sampler.AudioHealthSampler` are this
module's only callers; a raw ``IssueTracker``/``IncidentStore`` record never
reaches a management surface unmapped by :func:`_present_incident`.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..music_sources import Source
from ._health_fields import _as_int, _detail, _duration_label, _finite_number, mapping
from ._health_sources import SOURCE_LABELS


def _incident_impact(issue: Mapping[str, Any]) -> str:
    return {
        "continuity": "Audio may have briefly interrupted.",
        "latency": "Audio continued with higher latency.",
        "sync": "Playback may have briefly lost synchronization.",
        "quality": "Audio may have briefly distorted.",
        "availability": "This source may not be available.",
        "observability": "JTS could not confirm current audio health.",
    }.get(str(issue.get("impact")), "Audio quality may have been affected.")


_AIRPLAY_INPUT_UNAVAILABLE_KEY = f"{Source.AIRPLAY.value}.input_unavailable"
_LIKELY_AREA_BY_VERDICT = {
    "network": "Wi-Fi link to this speaker",
    "internal:receiver": "AirPlay receiver on this speaker",
}


def _likely_area(issue: Mapping[str, Any]) -> str:
    key = str(issue.get("key") or "")
    if key == _AIRPLAY_INPUT_UNAVAILABLE_KEY:
        attribution = mapping(
            mapping(mapping(issue.get("context")).get("started")).get(
                "attribution",
            ),
        )
        area = _LIKELY_AREA_BY_VERDICT.get(str(attribution.get("verdict")))
        if area is not None:
            return area
    if key.startswith("path.outputd"):
        return "Final output stage"
    if key.startswith(("path.fanin", "path.camilla", "path.transport")):
        return "Shared processing path"
    if key.startswith("airplay"):
        return "AirPlay transport and synchronization"
    if key.startswith("usbsink.latency") or key.startswith("usbsink.clock"):
        return "USB host timing"
    source_id = issue.get("source_id")
    if isinstance(source_id, str):
        return f"{SOURCE_LABELS.get(source_id, source_id)} source"
    return "Audio monitoring"


def _incident_evidence(issue: Mapping[str, Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    context = mapping(mapping(issue.get("context")).get("started"))
    if issue.get("key") == _AIRPLAY_INPUT_UNAVAILABLE_KEY:
        attribution_details = mapping(context.get("attribution")).get("details")
        if isinstance(attribution_details, list):
            evidence.extend(
                _detail(str(row["label"]), str(row["value"]))
                for row in attribution_details
                if isinstance(row, Mapping) and row.get("label") and row.get("value")
            )
    if context.get("clock_mode"):
        evidence.append(_detail("Clock mode", context["clock_mode"]))
    input_context = mapping(context.get("input"))
    if _finite_number(input_context.get("rms_dbfs")) is not None:
        evidence.append(_detail(
            "Input level",
            f"{float(input_context['rms_dbfs']):.1f} dBFS",
        ))
    output_context = mapping(context.get("output"))
    if _finite_number(output_context.get("snd_pcm_delay_ms")) is not None:
        evidence.append(_detail(
            "DAC queue",
            f"{float(output_context['snd_pcm_delay_ms']):.1f} ms",
        ))
    host = mapping(context.get("host"))
    # `throttled_history` never clears within a boot, so it must not be
    # rendered as a live condition (jasper/control/system_metrics.py).
    if _as_int(host.get("throttled_now")):
        evidence.append(_detail("Power or heat throttling", "Now"))
    elif _as_int(host.get("throttled_history")):
        evidence.append(_detail("Power or heat throttling", "Earlier this boot"))
    memory_pressure = _finite_number(host.get("mem_psi_some_avg60"))
    if memory_pressure is not None and memory_pressure > 0:
        evidence.append(_detail("Memory pressure", f"{float(memory_pressure):.0f}%"))
    return evidence


def _timestamp(value: Any, default: float) -> float:
    number = _finite_number(value)
    return float(number) if number is not None else default


def _incident_duration(issue: Mapping[str, Any], now: float) -> float:
    observed = _finite_number(issue.get("observed_seconds"))
    if observed is not None:
        return max(0.0, float(observed))
    started = _timestamp(issue.get("started_at"), now)
    end = _timestamp(issue.get("recovered_at"), now)
    return max(0.0, end - started)


def _present_incident(
    issue: Mapping[str, Any],
    now: float,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    started = _timestamp(issue.get("started_at"), now)
    duration = _incident_duration(issue, now)
    cutoff = now - 1800.0
    matching = [
        item for item in history
        if item.get("key") == issue.get("key")
        and _timestamp(
            item.get("last_occurrence_at")
            or item.get("last_seen_at")
            or item.get("started_at"),
            0.0,
        ) >= cutoff
    ]
    recurrence: dict[str, Any] | None = None
    if matching:
        # A coalesced record retains first/last occurrence plus total count,
        # not every timestamp. If it straddles the window boundary, only its
        # last occurrence is provably inside, so expose a lower bound.
        count = sum(
            max(1, _as_int(item.get("count"), 1))
            if _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                0.0,
            ) >= cutoff
            else 1
            for item in matching
        )
        known_firsts = []
        for item in matching:
            item_first = _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                now,
            )
            item_last = _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            known_firsts.append(item_first if item_first >= cutoff else item_last)
        first_at = min(known_firsts)
        last_at = max(
            _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            for item in matching
        )
        recurrence = {
            "count": count,
            "first_at": first_at,
            "last_at": last_at,
            "window_seconds": 1800.0,
            "count_is_lower_bound": True,
            "summary": (
                f"At least {count} occurrence"
                f"{'s' if count != 1 else ''} observed in 30 min"
            ),
        }
    key = str(issue.get("key") or "audio.issue")
    presented = {
        "id": f"{key}:{started:.3f}",
        "key": key,
        "status": issue.get("status"),
        "severity": issue.get("severity"),
        "title": issue.get("title"),
        "detail": issue.get("detail"),
        "source_id": issue.get("source_id"),
        "started_at": started,
        "last_seen_at": issue.get("last_seen_at"),
        "recovered_at": issue.get("recovered_at"),
        "count": max(1, _as_int(issue.get("count"), 1)),
        "impact": _incident_impact(issue),
        "observed": str(issue.get("detail") or "JTS observed an audio-path change."),
        "likely_area": _likely_area(issue),
        "evidence": _incident_evidence(issue),
    }
    if recurrence is not None and recurrence["count"] > 1:
        presented["recurrence"] = recurrence
    if issue.get("status") == "recovered" and duration > 0.0:
        presented["duration_seconds"] = round(duration, 1)
        presented["duration_label"] = _duration_label(duration)
    return presented


def _incident_priority(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> tuple[int, int, int, float]:
    relevant = _incident_is_relevant(issue, active_source)
    key = str(issue.get("key") or "")
    return (
        1 if relevant else 0,
        1 if issue.get("severity") == "issue" else 0,
        0
        if key == "path.transport_parked" or key.startswith("path.transport_park.")
        else 1,
        _timestamp(issue.get("last_seen_at"), 0.0),
    )


def _incident_is_relevant(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> bool:
    source_id = issue.get("source_id")
    return (
        issue.get("scope") in {"path", "monitor"}
        or source_id is None
        or (active_source is not None and source_id == active_source)
    )
