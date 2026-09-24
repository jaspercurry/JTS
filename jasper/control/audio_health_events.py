# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Per-tick counter-baseline bookkeeping for :class:`AudioHealthSampler`.

Two independent evidence sources feed the sampler's incident tracking:
one-shot raw events the AirPlay collector already timestamped
(:func:`record_raw_events`, deduped by fingerprint so a repeated read of the
same event never double-counts it), and monotonic counters this module
diffs against the previous tick's reading (:func:`record_counter_events`).
Both are pure with respect to :class:`CounterBaselines` -- the only mutable
state either function needs -- so the sampler, which owns the
:class:`~jasper.control.audio_incidents.IssueTracker` and
:class:`~jasper.control.audio_incidents.SessionRollup` these baselines feed,
applies the returned points itself rather than this leaf reaching up to call
either.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..music_sources import Source
from ._health_fields import as_int, finite_number, mapping
from ._health_sources import SOURCE_LABELS
from .audio_incidents import issue_row


@dataclass
class CounterBaselines:
    """One sampler's previous-tick counter readings and raw-event dedup set.

    The sampler keeps exactly one instance for its lifetime -- these fields
    lived directly on ``AudioHealthSampler`` before this leaf existed.
    """

    seen_raw_events: deque[tuple[Any, ...]] = field(
        default_factory=lambda: deque(maxlen=40)
    )
    seen_raw_event_set: set[tuple[Any, ...]] = field(default_factory=set)
    input_xruns: dict[str, int] | None = None
    usb_buffer_counts: tuple[int, int] | None = None
    fanin_pings_skipped: int | None = None
    outputd_xruns: dict[str, int] | None = None
    service_restarts: dict[str, int | None] | None = None
    outputd_clipped: int | None = None


def record_raw_events(
    baselines: CounterBaselines,
    airplay: Mapping[str, Any],
    *,
    active_source: str | None,
    now: float,
) -> list[tuple[dict[str, Any], float, int, Mapping[str, Any] | None, float | None]]:
    """One-shot events the collector already timestamped, deduped and
    classified into issue rows -- never carries context (that is frozen only
    for the counter-derived points below, which persist as incidents rather
    than momentary blips)."""
    points: list[
        tuple[dict[str, Any], float, int, Mapping[str, Any] | None, float | None]
    ] = []
    for raw in airplay.get("events") or []:
        if not isinstance(raw, Mapping):
            continue
        fingerprint = (
            raw.get("ts"), raw.get("type"), raw.get("count"), raw.get("detail")
        )
        if fingerprint in baselines.seen_raw_event_set:
            continue
        if len(baselines.seen_raw_events) == baselines.seen_raw_events.maxlen:
            oldest = baselines.seen_raw_events.popleft()
            baselines.seen_raw_event_set.discard(oldest)
        baselines.seen_raw_events.append(fingerprint)
        baselines.seen_raw_event_set.add(fingerprint)
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
        event_time = finite_number(raw.get("ts"))
        when = float(event_time) if event_time is not None else now
        points.append((candidate, when, as_int(raw.get("count"), 1), None, now))
    return points


def _nonnegative_counter(value: Any) -> int | None:
    """A monotonic counter's current reading, or ``None`` when unreadable.

    A negative value cannot be a counter (they only go up between resets);
    a bare ``float``/``str`` is rejected rather than coerced, since a counter
    field that is not already an ``int`` in the daemon's JSON is corrupt.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def record_counter_events(
    baselines: CounterBaselines,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    now: float,
    *,
    session_source_id: str | None,
    service_states: Mapping[str, Mapping[str, Any]],
    restart_watch_units: Mapping[str, str],
    context: Mapping[str, Any],
) -> tuple[
    list[tuple[dict[str, Any], float, int, Mapping[str, Any] | None, float | None]],
    dict[str, Any] | None,
    bool,
]:
    """Diff every monotonic counter this sampler watches against the last
    tick's baseline, returning ``(points, clipping_issue, preserve_clipping)``
    -- clipping is singled out because it alone must merge into the SAME
    tick's ``_state_issues`` rows rather than ride as an independent point,
    and ``preserve_clipping`` tells the caller when a clean baseline
    (first read, a gap, or a counter reset) must not be read as recovery."""
    points: list[
        tuple[dict[str, Any], float, int, Mapping[str, Any] | None, float | None]
    ] = []
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    watchdog = mapping(fanin.get("watchdog"))
    pings_skipped = as_int(watchdog.get("pings_skipped"))
    if baselines.fanin_pings_skipped is not None:
        skipped_delta = pings_skipped - baselines.fanin_pings_skipped
        if skipped_delta > 0:
            points.append((
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
                skipped_delta,
                context,
                None,
            ))
    baselines.fanin_pings_skipped = pings_skipped
    inputs = mapping(fanin.get("inputs"))
    input_counts = {
        source_id: as_int(mapping(observation).get("xrun_count"))
        for source_id, observation in inputs.items()
        if isinstance(source_id, str)
        and bool(mapping(observation).get("present"))
    }
    if baselines.input_xruns is not None:
        for source_id, count in input_counts.items():
            if (
                source_id == Source.AIRPLAY.value
                or source_id != session_source_id
            ):
                continue  # AirPlay has its own events; idle lanes are noise.
            previous = baselines.input_xruns.get(source_id, count)
            delta = count - previous
            if delta > 0:
                points.append((
                    issue_row(
                        f"{source_id}.input_xrun",
                        scope="source",
                        source_id=source_id,
                        impact="continuity",
                        severity="issue",
                        title=f"{SOURCE_LABELS.get(source_id, source_id)} input recovered",
                        detail=f"The input recovered {delta} interruption(s).",
                    ),
                    now,
                    delta,
                    context,
                    None,
                ))
    baselines.input_xruns = input_counts

    usb_input = mapping(inputs.get(Source.USBSINK.value))
    unlocks = _nonnegative_counter(
        mapping(usb_input.get("resampler")).get("unlock_count")
    )
    stream_stops = _nonnegative_counter(
        mapping(usb_input.get("direct")).get("stream_stops")
    )
    if unlocks is None or stream_stops is None:
        baselines.usb_buffer_counts = None
    else:
        previous_usb = baselines.usb_buffer_counts
        if previous_usb is not None:
            unlock_delta = unlocks - previous_usb[0]
            stop_delta = stream_stops - previous_usb[1]
            if (
                unlock_delta > 0
                and stop_delta >= 0
                and session_source_id == Source.USBSINK.value
            ):
                unexpected = max(0, unlock_delta - stop_delta)
                if unexpected:
                    points.append((
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
                        unexpected,
                        context,
                        None,
                    ))
        baselines.usb_buffer_counts = (unlocks, stream_stops)

    # None, not 0, for a unit systemd could not be asked about: a probe
    # that failed and recovered would otherwise read as a restart burst.
    restarts = {
        unit: _nonnegative_counter(
            mapping(service_states.get(unit)).get("n_restarts"),
        )
        for unit in restart_watch_units
    }
    if baselines.service_restarts is not None:
        for unit, stem in restart_watch_units.items():
            previous_restarts = baselines.service_restarts.get(unit)
            current_restarts = restarts[unit]
            if previous_restarts is None or current_restarts is None:
                continue
            delta = current_restarts - previous_restarts
            if delta > 0:
                points.append((
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
                    delta,
                    context,
                    None,
                ))
    baselines.service_restarts = restarts

    if outputd is None:
        baselines.outputd_xruns = None
        baselines.outputd_clipped = None
        return points, None, True
    outputd_map = mapping(outputd)
    clipping_issue: dict[str, Any] | None = None
    clipped_samples = _nonnegative_counter(
        mapping(outputd_map.get("mix")).get("clipped_samples"),
    )
    preserve_clipping = False
    if clipped_samples is None:
        baselines.outputd_clipped = None
        preserve_clipping = True
    elif baselines.outputd_clipped is None:
        baselines.outputd_clipped = clipped_samples
        preserve_clipping = True
    else:
        clipped_delta = clipped_samples - baselines.outputd_clipped
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
        baselines.outputd_clipped = clipped_samples
    outputd_counts = {
        "content": as_int(mapping(outputd_map.get("content")).get("xrun_count")),
        "dac": as_int(mapping(outputd_map.get("dac")).get("xrun_count")),
    }
    if baselines.outputd_xruns is not None:
        for stage, count in outputd_counts.items():
            previous = baselines.outputd_xruns.get(stage, count)
            delta = count - previous
            if delta > 0:
                title = (
                    "Sound to the speaker recovered"
                    if stage == "dac" else "Music path recovered"
                )
                points.append((
                    issue_row(
                        f"path.outputd_{stage}_xrun",
                        scope="path",
                        impact="continuity",
                        severity="issue",
                        title=title,
                        detail=f"Sound was interrupted {delta} time(s) and resumed.",
                    ),
                    now,
                    delta,
                    context,
                    None,
                ))
    baselines.outputd_xruns = outputd_counts
    return points, clipping_issue, preserve_clipping
