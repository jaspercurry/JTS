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

# ``(row, when, count, context, observed_at)``: one ``IssueTracker.record_point``.
Point = tuple[dict[str, Any], float, int, Mapping[str, Any] | None, float | None]
# A counter-derived row and how many times it happened since the last tick.
_Occurrence = tuple[dict[str, Any], int]


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
) -> list[Point]:
    """One-shot events the collector already timestamped, deduped and
    classified into issue rows -- never carries context (that is frozen only
    for the counter-derived points below, which persist as incidents rather
    than momentary blips)."""
    points: list[Point] = []
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


def _watchdog_recoveries(
    baselines: CounterBaselines, fanin: Mapping[str, Any],
) -> list[_Occurrence]:
    pings_skipped = as_int(mapping(fanin.get("watchdog")).get("pings_skipped"))
    previous = baselines.fanin_pings_skipped
    baselines.fanin_pings_skipped = pings_skipped
    if previous is None or pings_skipped <= previous:
        return []
    # No number in the sentence: the delta counts watchdog ticks missed — how
    # LONG one stall lasted, not how many stalls there were. It rides the
    # structured count instead.
    return [(
        issue_row(
            "path.fanin_watchdog_recovered",
            scope="path",
            impact="continuity",
            severity="issue",
            title="Sound recovered after a brief pause",
            detail="Sound stopped moving through the speaker briefly and resumed.",
        ),
        pings_skipped - previous,
    )]


def _input_xruns(
    baselines: CounterBaselines,
    inputs: Mapping[str, Any],
    session_source_id: str | None,
) -> list[_Occurrence]:
    counts = {
        source_id: as_int(mapping(observation).get("xrun_count"))
        for source_id, observation in inputs.items()
        if isinstance(source_id, str)
        and bool(mapping(observation).get("present"))
    }
    occurrences: list[_Occurrence] = []
    if baselines.input_xruns is not None:
        for source_id, count in counts.items():
            if (
                source_id == Source.AIRPLAY.value
                or source_id != session_source_id
            ):
                continue  # AirPlay has its own events; idle lanes are noise.
            delta = count - baselines.input_xruns.get(source_id, count)
            if delta > 0:
                occurrences.append((
                    issue_row(
                        f"{source_id}.input_xrun",
                        scope="source",
                        source_id=source_id,
                        impact="continuity",
                        severity="issue",
                        title=f"{SOURCE_LABELS.get(source_id, source_id)} input recovered",
                        detail=f"The input recovered {delta} interruption(s).",
                    ),
                    delta,
                ))
    baselines.input_xruns = counts
    return occurrences


def _usb_underfills(
    baselines: CounterBaselines,
    inputs: Mapping[str, Any],
    session_source_id: str | None,
) -> list[_Occurrence]:
    """USB resampler unlocks that no host stream stop explains."""
    usb_input = mapping(inputs.get(Source.USBSINK.value))
    unlocks = _nonnegative_counter(
        mapping(usb_input.get("resampler")).get("unlock_count")
    )
    stream_stops = _nonnegative_counter(
        mapping(usb_input.get("direct")).get("stream_stops")
    )
    previous = baselines.usb_buffer_counts
    if unlocks is None or stream_stops is None:
        baselines.usb_buffer_counts = None
        return []
    baselines.usb_buffer_counts = (unlocks, stream_stops)
    if previous is None or session_source_id != Source.USBSINK.value:
        return []
    unlock_delta = unlocks - previous[0]
    stop_delta = stream_stops - previous[1]
    unexpected = max(0, unlock_delta - stop_delta)
    if unlock_delta <= 0 or stop_delta < 0 or not unexpected:
        return []
    return [(
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
        unexpected,
    )]


def _service_restarts(
    baselines: CounterBaselines,
    service_states: Mapping[str, Mapping[str, Any]],
    restart_watch_units: Mapping[str, str],
) -> list[_Occurrence]:
    # None, not 0, for a unit systemd could not be asked about: a probe
    # that failed and recovered would otherwise read as a restart burst.
    restarts = {
        unit: _nonnegative_counter(
            mapping(service_states.get(unit)).get("n_restarts"),
        )
        for unit in restart_watch_units
    }
    occurrences: list[_Occurrence] = []
    if baselines.service_restarts is not None:
        for unit, stem in restart_watch_units.items():
            previous = baselines.service_restarts.get(unit)
            current = restarts[unit]
            if previous is None or current is None:
                continue
            delta = current - previous
            if delta > 0:
                occurrences.append((
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
                    delta,
                ))
    baselines.service_restarts = restarts
    return occurrences


def _clipping(
    baselines: CounterBaselines, outputd: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, bool]:
    """``(clipping_issue, preserve_clipping)`` for this tick's outputd read."""
    clipped_samples = _nonnegative_counter(
        mapping(outputd.get("mix")).get("clipped_samples"),
    )
    previous = baselines.outputd_clipped
    baselines.outputd_clipped = clipped_samples
    if clipped_samples is None or previous is None:
        return None, True
    clipped_delta = clipped_samples - previous
    if clipped_delta <= 0:
        # A daemon restart/reset establishes a new baseline; it does not
        # prove that an already-observed episode recovered.
        return None, clipped_delta < 0
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
    return clipping_issue, False


def _outputd_xruns(
    baselines: CounterBaselines, outputd: Mapping[str, Any],
) -> list[_Occurrence]:
    counts = {
        "content": as_int(mapping(outputd.get("content")).get("xrun_count")),
        "dac": as_int(mapping(outputd.get("dac")).get("xrun_count")),
    }
    occurrences: list[_Occurrence] = []
    if baselines.outputd_xruns is not None:
        for stage, count in counts.items():
            delta = count - baselines.outputd_xruns.get(stage, count)
            if delta > 0:
                occurrences.append((
                    issue_row(
                        f"path.outputd_{stage}_xrun",
                        scope="path",
                        impact="continuity",
                        severity="issue",
                        title=(
                            "Sound to the speaker recovered"
                            if stage == "dac" else "Music path recovered"
                        ),
                        detail=f"Sound was interrupted {delta} time(s) and resumed.",
                    ),
                    delta,
                ))
    baselines.outputd_xruns = counts
    return occurrences


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
) -> tuple[list[Point], dict[str, Any] | None, bool]:
    """Diff every monotonic counter this sampler watches against the last
    tick's baseline, returning ``(points, clipping_issue, preserve_clipping)``
    -- clipping is singled out because it alone must merge into the SAME
    tick's ``state_issues`` rows rather than ride as an independent point,
    and ``preserve_clipping`` tells the caller when a clean baseline
    (first read, a gap, or a counter reset) must not be read as recovery."""
    fanin = mapping(mapping(airplay.get("current")).get("fanin"))
    inputs = mapping(fanin.get("inputs"))
    occurrences = [
        *_watchdog_recoveries(baselines, fanin),
        *_input_xruns(baselines, inputs, session_source_id),
        *_usb_underfills(baselines, inputs, session_source_id),
        *_service_restarts(baselines, service_states, restart_watch_units),
    ]
    clipping_issue: dict[str, Any] | None = None
    preserve_clipping = True
    if outputd is None:
        baselines.outputd_xruns = None
        baselines.outputd_clipped = None
    else:
        outputd_map = mapping(outputd)
        clipping_issue, preserve_clipping = _clipping(baselines, outputd_map)
        occurrences.extend(_outputd_xruns(baselines, outputd_map))
    points: list[Point] = [
        (row, now, count, context, None) for row, count in occurrences
    ]
    return points, clipping_issue, preserve_clipping
