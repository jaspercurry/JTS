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
    _as_float,
    _as_int,
    _as_int_or_none,
    _nonneg_rate,
    _sum_or_none,
)
from jasper.fanin.status import fanin_inputs_by_label, read_fanin_status
from jasper.music_sources import MUSIC_SOURCE_SPECS

# Fallback mixer rate when fan-in STATUS omits output.sample_rate.
DEFAULT_MIXER_RATE_HZ = 48000


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
        airplay = inputs_by_label.get("airplay")
        output = status.get("output")
        if not isinstance(output, dict):
            output = {}
        watchdog = status.get("watchdog")
        if not isinstance(watchdog, dict):
            watchdog = {}

        airplay_frames = _as_int(airplay.get("frames_read")) if airplay else 0
        airplay_xruns = _as_int(airplay.get("xrun_count")) if airplay else 0
        output_frames = _as_int(output.get("frames_written"))
        output_ring = (
            output.get("ring") if isinstance(output.get("ring"), dict) else None
        )
        output_full_waits = (
            _as_int_or_none(output_ring.get("full_waits"))
            if output_ring is not None else None
        )
        # The ring's two loss counters, summed: both mean "a period the reader
        # never took". An absent counter stays None — "not observed", not zero.
        output_ring_drops = (
            _sum_or_none(output_ring, ("stuck_reader_drops", "drop_no_reader"))
            if output_ring is not None else None
        )

        prev = self._last_counts
        airplay_rate: float | None = None
        output_rate: float | None = None
        full_waits_rate: float | None = None
        ring_drops_rate: float | None = None
        input_rates: dict[str, float | None] = {
            spec.id.value: None for spec in MUSIC_SOURCE_SPECS
        }
        input_empty_reads_rates: dict[str, float | None] = {
            spec.id.value: None for spec in MUSIC_SOURCE_SPECS
        }
        input_xrun_rates: dict[str, float | None] = {
            spec.id.value: None for spec in MUSIC_SOURCE_SPECS
        }
        input_frames = {
            spec.id.value: (
                _as_int(inputs_by_label[spec.fanin_label].get("frames_read"))
                if spec.fanin_label in inputs_by_label else 0
            )
            for spec in MUSIC_SOURCE_SPECS
        }
        input_xruns = {
            spec.id.value: (
                _as_int(inputs_by_label[spec.fanin_label].get("xrun_count"))
                if spec.fanin_label in inputs_by_label else 0
            )
            for spec in MUSIC_SOURCE_SPECS
        }
        # empty_reads only exists on a ring-armed lane's optional "ring"
        # block (U3/P6, rust/jasper-fanin/src/state.rs); None on an unarmed
        # lane, never 0.
        input_empty_reads: dict[str, int | None] = {}
        for spec in MUSIC_SOURCE_SPECS:
            lane = inputs_by_label.get(spec.fanin_label)
            ring_block = lane.get("ring") if isinstance(lane, dict) else None
            input_empty_reads[spec.id.value] = (
                _as_int_or_none(ring_block.get("empty_reads"))
                if isinstance(ring_block, dict) else None
            )
        if prev is not None:
            dt = max(0.001, now - float(prev.get("ts", now)))
            prev_airplay_frames = _as_int(prev.get("airplay_frames"))
            prev_output_frames = _as_int(prev.get("output_frames"))
            if airplay_frames >= prev_airplay_frames:
                airplay_rate = (airplay_frames - prev_airplay_frames) / dt
            if output_frames >= prev_output_frames:
                output_rate = (output_frames - prev_output_frames) / dt
            previous_inputs = prev.get("input_frames")
            if isinstance(previous_inputs, Mapping):
                for source_id, frames in input_frames.items():
                    previous_frames = _as_int(previous_inputs.get(source_id))
                    if frames >= previous_frames:
                        input_rates[source_id] = (frames - previous_frames) / dt
            previous_empty_reads = prev.get("input_empty_reads")
            if isinstance(previous_empty_reads, Mapping):
                for source_id, empty_reads in input_empty_reads.items():
                    input_empty_reads_rates[source_id] = _nonneg_rate(
                        empty_reads, previous_empty_reads.get(source_id), dt,
                    )
            previous_input_xruns = prev.get("input_xruns")
            if isinstance(previous_input_xruns, Mapping):
                for source_id, xruns in input_xruns.items():
                    input_xrun_rates[source_id] = _nonneg_rate(
                        xruns, previous_input_xruns.get(source_id), dt,
                    )

            airplay_delta = airplay_xruns - _as_int(prev.get("airplay_xruns"))
            full_waits_rate = _nonneg_rate(
                output_full_waits, prev.get("output_full_waits"), dt,
            )
            ring_drops_rate = _nonneg_rate(
                output_ring_drops, prev.get("output_ring_drops"), dt,
            )
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

        self._last_counts = {
            "ts": now,
            "airplay_frames": airplay_frames,
            "airplay_xruns": airplay_xruns,
            "output_frames": output_frames,
            "output_full_waits": output_full_waits,
            "output_ring_drops": output_ring_drops,
            "input_frames": input_frames,
            "input_xruns": input_xruns,
            "input_empty_reads": input_empty_reads,
        }

        input_buffer_frames = _as_int(status.get("input_buffer_frames"))
        mixer_rate_hz = _as_int(output.get("sample_rate")) or DEFAULT_MIXER_RATE_HZ
        # Fixed-shape, source-neutral observations for the outer audio-health
        # composer. Keep only what explains health; /state retains the full
        # fan-in STATUS for deep debugging. Every declared source gets a slot,
        # even when its lane is absent, so adding a source extends the existing
        # metadata seam rather than another dashboard conditional.
        input_observations: dict[str, dict[str, Any]] = {}
        for spec in MUSIC_SOURCE_SPECS:
            entry = inputs_by_label.get(spec.fanin_label)
            resampler = (
                entry.get("resampler")
                if isinstance(entry, dict)
                and isinstance(entry.get("resampler"), dict)
                else None
            )
            direct = (
                entry.get("direct")
                if isinstance(entry, dict)
                and isinstance(entry.get("direct"), dict)
                else None
            )
            ring = (
                entry.get("ring")
                if isinstance(entry, dict) and isinstance(entry.get("ring"), dict)
                else None
            )
            slot_frames = (
                _as_int_or_none(ring.get("slot_frames")) if ring is not None else None
            )
            frames_rate = input_rates[spec.id.value]
            empty_reads_rate = input_empty_reads_rates[spec.id.value]
            xrun_rate = input_xrun_rates[spec.id.value]
            input_observations[spec.id.value] = {
                "label": spec.fanin_label,
                "present": isinstance(entry, dict),
                "source": entry.get("source") if isinstance(entry, dict) else None,
                "frames_read": (
                    _as_int(entry.get("frames_read"))
                    if isinstance(entry, dict) else 0
                ),
                "frames_per_sec": (
                    round(frames_rate, 1) if frames_rate is not None else None
                ),
                "empty_reads_per_sec": (
                    round(empty_reads_rate, 1)
                    if empty_reads_rate is not None else None
                ),
                "silent_ms_per_sec": (
                    round(
                        empty_reads_rate * slot_frames / mixer_rate_hz * 1000.0,
                        1,
                    )
                    if empty_reads_rate is not None and slot_frames else None
                ),
                "xrun_count": (
                    _as_int(entry.get("xrun_count"))
                    if isinstance(entry, dict) else 0
                ),
                "xruns_per_sec": (
                    round(xrun_rate, 3) if xrun_rate is not None else None
                ),
                "rms_dbfs": (
                    _as_float(entry.get("rms_dbfs"))
                    if isinstance(entry, dict) else None
                ),
                "muted": (
                    entry.get("muted")
                    if isinstance(entry, dict)
                    and isinstance(entry.get("muted"), bool)
                    else None
                ),
                "health": direct.get("health") if direct is not None else None,
                "direct": (
                    {
                        key: direct.get(key)
                        for key in (
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
                        if key in direct
                    }
                    if direct is not None else None
                ),
                "resampler": (
                    {
                        key: resampler.get(key)
                        for key in (
                            "health",
                            "locked",
                            "input_frames",
                            "output_frames",
                            "silence_frames",
                            "overrun_frames",
                            "ratio_ppm",
                            # Inner-controller rail counters (#3464): the
                            # "ratio is railing" signal the ratio_ppm gauge
                            # alone only shows if polled at the right moment.
                            "clamp_count",
                            "anti_windup_count",
                            "lock_count",
                            "unlock_count",
                            "fill_frames",
                            "target_fill_frames",
                            "held_target_frames",
                            "decay",
                        )
                        if key in resampler
                    }
                    if resampler is not None else None
                ),
                "ring": (
                    {
                        key: ring.get(key)
                        for key in (
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
                        if key in ring
                    }
                    if ring is not None else None
                ),
            }
        # Ring A back-pressure: `occupancy` counts SLOTS (not frames), and
        # `full_waits` climbs once per publish that had to wait for a live
        # reader to drain one -- the "running too tight" signal (issue #4124).
        ring_observation: dict[str, Any] | None = None
        if output_ring is not None:
            ring_observation = {
                key: output_ring.get(key)
                for key in (
                    "occupancy",
                    "slots",
                    "published",
                    "full_waits",
                    "stuck_reader_drops",
                    "drop_no_reader",
                    "stall_active",
                    "last_stall_ms",
                )
                if key in output_ring
            }
            ring_observation["full_waits_per_sec"] = (
                round(full_waits_rate, 2) if full_waits_rate is not None else None
            )
            ring_observation["drops_per_sec"] = (
                round(ring_drops_rate, 3) if ring_drops_rate is not None else None
            )
        current = {
            "available": True,
            "input_buffer_frames": input_buffer_frames,
            "selected_input": status.get("selected_input"),
            "inputs": input_observations,
            "host_clock": (
                copy.deepcopy(status.get("host_clock"))
                if isinstance(status.get("host_clock"), dict)
                else None
            ),
            "airplay": {
                "present": airplay is not None,
                "frames_read": airplay_frames,
                "frames_per_sec": (
                    round(airplay_rate, 1)
                    if airplay_rate is not None else None
                ),
                "xrun_count": airplay_xruns,
            },
            "output": {
                "frames_written": output_frames,
                "frames_per_sec": (
                    round(output_rate, 1)
                    if output_rate is not None else None
                ),
                "sample_rate": _as_int(output.get("sample_rate")),
                "period_frames": _as_int(output.get("period_frames")),
                "ring": ring_observation,
            },
            "watchdog": {
                "last_progress_age_ms": _as_int(
                    watchdog.get("last_progress_age_ms"),
                ),
                "pings_skipped": _as_int(watchdog.get("pings_skipped")),
            },
            "tts": (
                copy.deepcopy(status.get("tts"))
                if isinstance(status.get("tts"), dict) else None
            ),
        }
        self._current = current
