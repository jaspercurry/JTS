# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Click-track generator for the route-latency click/capture harness.

Produces two artifacts a human plays/uses together:

* a 48 kHz stereo WAV of short clicks at jittered intervals, played on the
  Mac/Windows host into the JTS USB audio device (no host-side software
  beyond a media player); and
* a JSON schedule recording the *planned* impulse times and count, so the
  analyze step can sanity-check the DETECTED tap-impulse count against what was
  meant to be played (`--expected-impulse-count`, wired automatically on
  `run`). A large shortfall usually means the tap window was truncated — the
  WAV wasn't played to completion, the daemon restarted, or the tap
  auto-disarmed mid-run — a failure mode match-rate cannot catch because a
  truncated run shrinks its denominator too. See
  `jasper.cli.route_latency_harness._warn_if_tap_count_far_below_schedule`.

Sizing is driven directly by the certification gates in
``jasper.audio_validation`` (``percentile_min_samples`` /
``ROUTE_LATENCY_P95_MIN_DURATION_SECONDS`` /
``ROUTE_LATENCY_P99_MIN_DURATION_SECONDS``) plus a fixed safety margin, so a
generated track always has enough impulses and enough wall-clock duration to
certify its target percentile even after some impulses are dropped during
pairing (a played-but-unmatched click, mic dropout, etc). The margin is
deliberately generous: pairing rejects ambiguous/double matches (see
``jasper.route_latency.pairing``), so headroom protects against real-world
attrition, not just rounding.

Audio-output safety: default click amplitude is -12 dBFS (a deliberately
modest level — see the safe-volume doctrine in AGENTS.md/README: CamillaDSP's
``volume_limit`` stays the 0 dB ceiling, and operators must start very quiet
and ramp up by ear). A click is a short raised-cosine-windowed tone burst,
not a full-scale impulse — full-scale square-wave clicks risk driver/DAC
strain and are unnecessary for a threshold-crossing detector.
"""
from __future__ import annotations

import json
import math
import random
import struct
import wave
from dataclasses import dataclass
from pathlib import Path

from jasper.audio_validation import (
    ROUTE_LATENCY_P95_MIN_DURATION_SECONDS,
    ROUTE_LATENCY_P99_MIN_DURATION_SECONDS,
    percentile_min_samples,
)


SAMPLE_RATE_HZ = 48_000
CHANNELS = 2
SAMPLE_WIDTH_BYTES = 2  # int16

# Click shape: a short raised-cosine-windowed tone burst. A pure impulse
# (single non-zero sample) carries too little energy to reliably cross a
# sane detector threshold after USB/ALSA/acoustic-path losses; a tone burst
# is easy for both the Rust ingress tap (post-conversion S16 samples) and
# the mic-side detector to see, while staying short enough that its onset
# is an unambiguous "instant" for latency purposes.
CLICK_TONE_HZ = 2_000.0
CLICK_DURATION_MS = 5.0
DEFAULT_AMPLITUDE_DBFS = -12.0

# Safety margin over the raw certification minimums (see module docstring):
# pairing can legitimately drop some impulses (mic dropout, an ambiguous
# double-match near the pairing window edge), so a generated track always
# clears the gate with room to spare rather than landing exactly on it.
_COUNT_MARGIN = 1.2
_DURATION_MARGIN = 1.2

QUICK_PRESET_NAME = "quick"
PROMOTION_PRESET_NAME = "promotion"


@dataclass(frozen=True)
class ClickTrackPreset:
    """One named impulse-count/duration/jitter target.

    ``impulse_count`` and ``duration_seconds`` are pre-margined: they already
    clear the corresponding certification gate in
    ``jasper.audio_validation`` by ``_COUNT_MARGIN`` / ``_DURATION_MARGIN``.
    """

    name: str
    impulse_count: int
    duration_seconds: float
    jittered: bool


def _preset(name: str, *, percentile: float, min_duration_seconds: float, jittered: bool) -> ClickTrackPreset:
    min_count = percentile_min_samples(percentile)
    count = math.ceil(min_count * _COUNT_MARGIN)
    duration = min_duration_seconds * _DURATION_MARGIN
    return ClickTrackPreset(
        name=name,
        impulse_count=count,
        duration_seconds=duration,
        jittered=jittered,
    )


# Quick gate targets p95 certification: >=200 impulses over >=300s.
# Promotion gate targets p99 certification (which also requires jittered
# spacing): >=1000 impulses over >=1800s.
PRESETS: dict[str, ClickTrackPreset] = {
    p.name: p
    for p in (
        _preset(
            QUICK_PRESET_NAME,
            percentile=95,
            min_duration_seconds=ROUTE_LATENCY_P95_MIN_DURATION_SECONDS,
            jittered=False,
        ),
        _preset(
            PROMOTION_PRESET_NAME,
            percentile=99,
            min_duration_seconds=ROUTE_LATENCY_P99_MIN_DURATION_SECONDS,
            jittered=True,
        ),
    )
}


@dataclass(frozen=True)
class ClickSchedule:
    """Planned impulse onset times (seconds from track start)."""

    preset_name: str
    impulse_count: int
    duration_seconds: float
    jittered: bool
    amplitude_dbfs: float
    onsets_seconds: tuple[float, ...]
    seed: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "preset": self.preset_name,
            "impulse_count": self.impulse_count,
            "duration_seconds": self.duration_seconds,
            "jittered": self.jittered,
            "amplitude_dbfs": self.amplitude_dbfs,
            "sample_rate_hz": SAMPLE_RATE_HZ,
            "click_tone_hz": CLICK_TONE_HZ,
            "click_duration_ms": CLICK_DURATION_MS,
            "seed": self.seed,
            "onsets_seconds": list(self.onsets_seconds),
        }


def _spacing_seconds(*, count: int, duration_seconds: float, jittered: bool, rng: random.Random) -> list[float]:
    """Return `count` onset times within `duration_seconds`, first and last
    pulled in slightly from the track edges so a click never lands exactly
    at t=0 or t=duration (leaves silent lead-in/lead-out for playback
    start/stop slop)."""

    lead_seconds = min(1.0, duration_seconds * 0.01)
    usable = duration_seconds - 2 * lead_seconds
    if usable <= 0:
        raise ValueError("duration_seconds too small for the requested lead-in/out margin")
    mean_spacing = usable / count
    if not jittered:
        return [lead_seconds + mean_spacing * (i + 0.5) for i in range(count)]

    # Jittered: each inter-impulse gap is mean_spacing scaled by a uniform
    # factor in [0.5, 1.5), then the whole sequence is rescaled to exactly
    # fill `usable` seconds. This guarantees N impulses fit the requested
    # duration exactly while satisfying the artifact's
    # `--impulse-spacing-jittered` promotion requirement with real variance
    # (not a fixed-then-rounded schedule).
    raw_gaps = [mean_spacing * rng.uniform(0.5, 1.5) for _ in range(count)]
    raw_total = sum(raw_gaps)
    scale = usable / raw_total
    onsets: list[float] = []
    cursor = lead_seconds
    for gap in raw_gaps:
        onsets.append(cursor)
        cursor += gap * scale
    return onsets


def build_schedule(
    preset_name: str,
    *,
    amplitude_dbfs: float = DEFAULT_AMPLITUDE_DBFS,
    seed: int = 0,
) -> ClickSchedule:
    """Build the planned impulse schedule for a named preset."""

    try:
        preset = PRESETS[preset_name]
    except KeyError:
        raise ValueError(
            f"unknown preset {preset_name!r}; choose one of {sorted(PRESETS)}"
        ) from None
    rng = random.Random(seed)
    onsets = _spacing_seconds(
        count=preset.impulse_count,
        duration_seconds=preset.duration_seconds,
        jittered=preset.jittered,
        rng=rng,
    )
    return ClickSchedule(
        preset_name=preset.name,
        impulse_count=preset.impulse_count,
        duration_seconds=preset.duration_seconds,
        jittered=preset.jittered,
        amplitude_dbfs=amplitude_dbfs,
        onsets_seconds=tuple(onsets),
        seed=seed,
    )


def _click_samples(amplitude_dbfs: float) -> list[int]:
    """One raised-cosine-windowed tone burst as int16 samples."""

    amplitude_lin = 10.0 ** (amplitude_dbfs / 20.0)
    peak = amplitude_lin * 32767.0
    n = round(CLICK_DURATION_MS / 1000.0 * SAMPLE_RATE_HZ)
    samples: list[int] = []
    for i in range(n):
        t = i / SAMPLE_RATE_HZ
        window = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / max(1, n - 1))
        value = peak * window * math.sin(2.0 * math.pi * CLICK_TONE_HZ * t)
        samples.append(max(-32768, min(32767, round(value))))
    return samples


# Write the WAV one second at a time so peak memory stays bounded regardless
# of track length. A materialized promotion track (~36 min stereo S16 at
# 48 kHz) is ~415 MB — too large to build in one bytearray (plus its copy) on
# the 1 GB Pi, which is also running the audio stack under test. Streaming
# `writeframes` per chunk caps the buffer at one chunk (~192 KB).
_RENDER_CHUNK_FRAMES = SAMPLE_RATE_HZ


def render_wav(schedule: ClickSchedule, path: Path) -> Path:
    """Render `schedule` to a 48 kHz stereo S16_LE WAV at `path`.

    Streams the file in fixed-size frame chunks (see ``_RENDER_CHUNK_FRAMES``)
    so peak memory is one chunk, not the whole track — important because the
    promotion preset's full track is hundreds of MB and this can run on the
    1 GB Pi.
    """

    total_frames = round(schedule.duration_seconds * SAMPLE_RATE_HZ)
    click = _click_samples(schedule.amplitude_dbfs)
    click_len = len(click)
    frame_stride = CHANNELS * SAMPLE_WIDTH_BYTES
    start_frames = sorted(round(onset * SAMPLE_RATE_HZ) for onset in schedule.onsets_seconds)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH_BYTES)
        w.setframerate(SAMPLE_RATE_HZ)

        # Only clicks whose span [start, start+click_len) can still overlap the
        # current chunk matter; advance a cursor over the sorted onsets so each
        # click is considered once. A click that straddles a chunk boundary is
        # written across both chunks by the per-frame overlap clamp below.
        onset_cursor = 0
        chunk_start = 0
        while chunk_start < total_frames:
            chunk_frames = min(_RENDER_CHUNK_FRAMES, total_frames - chunk_start)
            chunk = bytearray(chunk_frames * frame_stride)
            chunk_end = chunk_start + chunk_frames

            # Skip onsets whose entire click ends before this chunk begins.
            while onset_cursor < len(start_frames) and start_frames[onset_cursor] + click_len <= chunk_start:
                onset_cursor += 1
            # Paint every click that overlaps [chunk_start, chunk_end) without
            # advancing the cursor past clicks that also reach the next chunk.
            i = onset_cursor
            while i < len(start_frames) and start_frames[i] < chunk_end:
                start_frame = start_frames[i]
                lo = max(chunk_start, start_frame)
                hi = min(chunk_end, start_frame + click_len)
                for frame in range(lo, hi):
                    sample = click[frame - start_frame]
                    byte_off = (frame - chunk_start) * frame_stride
                    chunk[byte_off : byte_off + frame_stride] = struct.pack("<hh", sample, sample)
                i += 1

            w.writeframes(bytes(chunk))
            chunk_start = chunk_end
    return path


def write_schedule_json(schedule: ClickSchedule, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schedule.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_schedule_json(path: Path) -> ClickSchedule:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ClickSchedule(
        preset_name=str(payload["preset"]),
        impulse_count=int(payload["impulse_count"]),
        duration_seconds=float(payload["duration_seconds"]),
        jittered=bool(payload["jittered"]),
        amplitude_dbfs=float(payload["amplitude_dbfs"]),
        onsets_seconds=tuple(float(v) for v in payload["onsets_seconds"]),
        seed=int(payload["seed"]),
    )


__all__ = [
    "CHANNELS",
    "CLICK_DURATION_MS",
    "CLICK_TONE_HZ",
    "DEFAULT_AMPLITUDE_DBFS",
    "PROMOTION_PRESET_NAME",
    "PRESETS",
    "QUICK_PRESET_NAME",
    "SAMPLE_RATE_HZ",
    "SAMPLE_WIDTH_BYTES",
    "ClickSchedule",
    "ClickTrackPreset",
    "build_schedule",
    "load_schedule_json",
    "render_wav",
    "write_schedule_json",
]
