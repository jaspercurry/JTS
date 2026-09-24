# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The speaker-wide fan-in view: jasper-fanin's STATUS shaped into the fixed,
source-neutral block the audio-health composer reads as the AirPlay
collector's ``snapshot()["current"]["fanin"]``.

Every declared music source gets an input slot, and the output ring, TTS lane
and host clock ride along, so the view serves every source, not only AirPlay.
:class:`~jasper.control.airplay_health.AirPlayHealthSampler` composes one and
records its ``fanin_airplay_xrun`` events in the AirPlay event bucket.
"""
from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from typing import Any

from jasper.control._health_fields import (
    as_int,
    as_int_or_none,
    nonneg_rate,
)
from jasper.fanin.status import fanin_inputs_by_label, read_fanin_status
from jasper.json_fields import as_float
from jasper.music_sources import MUSIC_SOURCE_SPECS, MusicSourceSpec

# Fallback mixer rate when fan-in STATUS omits output.sample_rate.
DEFAULT_MIXER_RATE_HZ = 48000

# The fields of each STATUS block the view keeps.
_DIRECT_KEYS = (
    "present",
    "health",
    "streaming",
    "stream_starts",
    "stream_stops",
    "retries",
    "reopen_pending",
    "reopens",
    "card_gen_reopens",
    "period_frames",
    "buffer_frames",
    "drain_avail",
)
_RESAMPLER_KEYS = (
    "health",
    "locked",
    "input_frames",
    "output_frames",
    "silence_frames",
    "overrun_frames",
    "ratio_ppm",
    # Inner-controller rail counters (#3464): the "ratio is railing" signal
    # the ratio_ppm gauge alone only shows if polled at the right moment.
    "clamp_count",
    "anti_windup_count",
    "lock_count",
    "unlock_count",
    "fill_frames",
    "target_fill_frames",
    "held_target_frames",
    "decay",
)
_INPUT_RING_KEYS = (
    "attached",
    "detach_reason",
    "writer_alive",
    "writer_pid",
    "occupancy",
    "empty_reads",
    "startup_empty_reads",
    "epoch_resets",
    "slot_frames",
    "n_slots",
)
# Ring A back-pressure: `occupancy` counts SLOTS (not frames), and
# `full_waits` climbs once per publish that had to wait for a live
# reader to drain one -- the "running too tight" signal (issue #4124).
_OUTPUT_RING_KEYS = (
    "occupancy",
    "slots",
    "published",
    "full_waits",
    "stuck_reader_drops",
    "drop_no_reader",
    "stall_active",
    "last_stall_ms",
)


def _sum_or_none(block: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    """Sum of the named counters, or ``None`` unless every one of them is present."""
    total = 0
    for key in keys:
        value = as_int_or_none(block.get(key))
        if value is None:
            return None
        total += value
    return total


def _block(parent: Any, key: str) -> dict[str, Any] | None:
    """``parent[key]`` when both are dicts, else None."""
    if not isinstance(parent, dict):
        return None
    child = parent.get(key)
    return child if isinstance(child, dict) else None


def _subset(block: dict[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any] | None:
    """The named fields ``block`` carries, or None without a block."""
    if block is None:
        return None
    return {key: block.get(key) for key in keys if key in block}


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _lane_counters(inputs_by_label: Mapping[str, Any], key: str) -> dict[str, int]:
    """Each declared source's ``key`` counter, 0 for a lane STATUS lacks."""
    return {
        spec.id.value: as_int(inputs_by_label.get(spec.fanin_label, {}).get(key))
        for spec in MUSIC_SOURCE_SPECS
    }


def _counts(
    now: float, inputs_by_label: Mapping[str, Any], output: dict[str, Any],
) -> dict[str, Any]:
    """The monotonic counters one STATUS carries, stamped ``now`` — what the
    next sample's rates delta against."""
    airplay = inputs_by_label.get("airplay")
    output_ring = _block(output, "ring")
    # empty_reads only exists on a ring-armed lane's optional "ring"
    # block (U3/P6, rust/jasper-fanin/src/state.rs); None on an unarmed
    # lane, never 0.
    input_empty_reads: dict[str, int | None] = {}
    for spec in MUSIC_SOURCE_SPECS:
        ring = _block(inputs_by_label.get(spec.fanin_label), "ring")
        input_empty_reads[spec.id.value] = (
            as_int_or_none(ring.get("empty_reads")) if ring is not None else None
        )
    return {
        "ts": now,
        "airplay_frames": as_int(airplay.get("frames_read")) if airplay else 0,
        "airplay_xruns": as_int(airplay.get("xrun_count")) if airplay else 0,
        "output_frames": as_int(output.get("frames_written")),
        "output_full_waits": (
            as_int_or_none(output_ring.get("full_waits"))
            if output_ring is not None else None
        ),
        # The ring's two loss counters, summed: both mean "a period the reader
        # never took". An absent counter stays None — "not observed", not zero.
        "output_ring_drops": (
            _sum_or_none(output_ring, ("stuck_reader_drops", "drop_no_reader"))
            if output_ring is not None else None
        ),
        "input_frames": _lane_counters(inputs_by_label, "frames_read"),
        "input_xruns": _lane_counters(inputs_by_label, "xrun_count"),
        "input_empty_reads": input_empty_reads,
    }


_SCALAR_COUNTERS = (
    "airplay_frames", "output_frames", "output_full_waits", "output_ring_drops",
)
_PER_SOURCE_COUNTERS = ("input_frames", "input_xruns", "input_empty_reads")


def _rates(counts: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    """Each counter's per-second rate since ``prev`` (the previous sample's
    ``_counts``), keyed as ``counts`` is.

    None throughout without a previous sample, and None for a counter that
    went backwards (a restart) or was not observed on both sides."""
    if prev is None:
        return {
            **dict.fromkeys(_SCALAR_COUNTERS),
            **{key: dict.fromkeys(counts[key]) for key in _PER_SOURCE_COUNTERS},
        }
    dt = max(0.001, counts["ts"] - prev["ts"])
    return {
        **{key: nonneg_rate(counts[key], prev[key], dt) for key in _SCALAR_COUNTERS},
        **{
            key: {
                source_id: nonneg_rate(value, prev[key][source_id], dt)
                for source_id, value in counts[key].items()
            }
            for key in _PER_SOURCE_COUNTERS
        },
    }


def _input_observation(
    spec: MusicSourceSpec,
    entry: dict[str, Any] | None,
    rates: dict[str, Any],
    mixer_rate_hz: int,
) -> dict[str, Any]:
    """One declared source's slot, present or not."""
    direct = _block(entry, "direct")
    ring = _block(entry, "ring")
    slot_frames = (
        as_int_or_none(ring.get("slot_frames")) if ring is not None else None
    )
    frames_rate = rates["input_frames"][spec.id.value]
    empty_reads_rate = rates["input_empty_reads"][spec.id.value]
    xrun_rate = rates["input_xruns"][spec.id.value]
    return {
        "label": spec.fanin_label,
        "present": isinstance(entry, dict),
        "source": entry.get("source") if isinstance(entry, dict) else None,
        "frames_read": as_int(entry.get("frames_read")) if isinstance(entry, dict) else 0,
        "frames_per_sec": _rounded(frames_rate, 1),
        "empty_reads_per_sec": _rounded(empty_reads_rate, 1),
        "silent_ms_per_sec": (
            round(empty_reads_rate * slot_frames / mixer_rate_hz * 1000.0, 1)
            if empty_reads_rate is not None and slot_frames else None
        ),
        "xrun_count": as_int(entry.get("xrun_count")) if isinstance(entry, dict) else 0,
        "xruns_per_sec": _rounded(xrun_rate, 3),
        "rms_dbfs": as_float(entry.get("rms_dbfs")) if isinstance(entry, dict) else None,
        "muted": (
            entry.get("muted")
            if isinstance(entry, dict)
            and isinstance(entry.get("muted"), bool)
            else None
        ),
        "health": direct.get("health") if direct is not None else None,
        "direct": _subset(direct, _DIRECT_KEYS),
        "resampler": _subset(_block(entry, "resampler"), _RESAMPLER_KEYS),
        "ring": _subset(ring, _INPUT_RING_KEYS),
    }


def _output_ring_observation(
    output_ring: dict[str, Any] | None, rates: dict[str, Any],
) -> dict[str, Any] | None:
    observation = _subset(output_ring, _OUTPUT_RING_KEYS)
    if observation is not None:
        observation["full_waits_per_sec"] = _rounded(rates["output_full_waits"], 2)
        observation["drops_per_sec"] = _rounded(rates["output_ring_drops"], 3)
    return observation


def _observation(
    status: dict[str, Any],
    inputs_by_label: Mapping[str, Any],
    output: dict[str, Any],
    counts: dict[str, Any],
    rates: dict[str, Any],
) -> dict[str, Any]:
    """The fixed-shape block :attr:`FaninView.current` publishes."""
    watchdog = status.get("watchdog")
    if not isinstance(watchdog, dict):
        watchdog = {}
    mixer_rate_hz = as_int(output.get("sample_rate")) or DEFAULT_MIXER_RATE_HZ
    return {
        "available": True,
        "input_buffer_frames": as_int(status.get("input_buffer_frames")),
        "selected_input": status.get("selected_input"),
        # Fixed-shape, source-neutral observations for the outer audio-health
        # composer. Keep only what explains health; /state retains the full
        # fan-in STATUS for deep debugging. Every declared source gets a slot,
        # even when its lane is absent, so adding a source extends the existing
        # metadata seam rather than another dashboard conditional.
        "inputs": {
            spec.id.value: _input_observation(
                spec, inputs_by_label.get(spec.fanin_label), rates, mixer_rate_hz,
            )
            for spec in MUSIC_SOURCE_SPECS
        },
        "host_clock": (
            copy.deepcopy(status.get("host_clock"))
            if isinstance(status.get("host_clock"), dict)
            else None
        ),
        "airplay": {
            "present": inputs_by_label.get("airplay") is not None,
            "frames_read": counts["airplay_frames"],
            "frames_per_sec": _rounded(rates["airplay_frames"], 1),
            "xrun_count": counts["airplay_xruns"],
        },
        "output": {
            "frames_written": counts["output_frames"],
            "frames_per_sec": _rounded(rates["output_frames"], 1),
            "sample_rate": as_int(output.get("sample_rate")),
            "period_frames": as_int(output.get("period_frames")),
            "ring": _output_ring_observation(_block(output, "ring"), rates),
        },
        "watchdog": {
            "last_progress_age_ms": as_int(watchdog.get("last_progress_age_ms")),
            "pings_skipped": as_int(watchdog.get("pings_skipped")),
        },
        "tts": (
            copy.deepcopy(status.get("tts"))
            if isinstance(status.get("tts"), dict) else None
        ),
    }


class FaninView:
    """One fan-in STATUS read per :meth:`sample`, shaped into :attr:`current`.
    Rates delta against the previous sample's counters.
    """

    def __init__(
        self, *, probe: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._probe = probe or read_fanin_status
        self._current: dict[str, Any] | None = None
        self._last_counts: dict[str, Any] | None = None

    @property
    def current(self) -> dict[str, Any] | None:
        """The latest observation, or None when STATUS was unreadable. The
        live dict, not a copy: readers must not mutate it."""
        return self._current

    def sample(
        self,
        now: float,
        *,
        record_event: Callable[..., None],
        suppress_events: bool = False,
    ) -> None:
        status = self._probe()
        if not isinstance(status, dict):
            self._current = None
            return

        inputs_by_label = fanin_inputs_by_label(status)
        output = status.get("output")
        if not isinstance(output, dict):
            output = {}
        counts = _counts(now, inputs_by_label, output)
        prev = self._last_counts
        rates = _rates(counts, prev)
        if prev is not None:
            airplay_delta = counts["airplay_xruns"] - prev["airplay_xruns"]
            if airplay_delta > 0 and not suppress_events:
                record_event(
                    now,
                    {
                        "type": "fanin_airplay_xrun",
                        "subsystem": "fanin",
                        "severity": "issue",
                        "title": "AirPlay fan-in xrun",
                        "detail": f"input recovered {airplay_delta} xrun(s)",
                    },
                    count=airplay_delta,
                )
        self._last_counts = counts
        self._current = _observation(status, inputs_by_label, output, counts, rates)
