# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Excitation-program model + composers for the crossover session flow.

Pure data (design §5.3): :class:`ExcitationProgram` stores no PCM;
:func:`render_program_pcm` regenerates it from the schedule. Composers take
per-segment digital gains as input — safety admission lives elsewhere and
this module never imports ``jasper.active_speaker``.

Channel routing (§5.4): CHECK/MEASURE are 2-channel (ch0 woofer, ch1
tweeter); VERIFY is mono through the applied production graph.

Courtesy-tone prelude ordering (issues #1677, #1810, #1812): nothing audible
may precede the first beep; only the settle plus the pre-pilot ambient
window may follow it (:data:`COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S`). Its
kind (``KIND_COURTESY_TONE``) stays out of ``STIMULUS_KINDS`` so
``program_analysis.py`` never correlates/deconvolves against it, but it
joins ``KNOWN_AUDIBLE_KINDS`` since it is real audible content.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.sweep import (
    SweepMeta,
    phase_closing_duration_s,
    synchronized_sweep_metadata,
    synchronized_swept_sine,
)
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

PROGRAM_SCHEMA_VERSION = 1
PROGRAM_KIND = "jts_excitation_program"

# Fixed program sample rate — matches CamillaDSP / the sweep kernel.
PROGRAM_SAMPLE_RATE_HZ = 48_000

# Phase vocabulary, distinct from crossover_v2.journey's PHASE_* family
# — string VALUES must match between the two, NAMES must stay
# disjoint; test_audio_measurement_program.py pins this.
PROGRAM_PHASE_CHECK = "check"
PROGRAM_PHASE_MEASURE = "measure"
PROGRAM_PHASE_VERIFY = "verify"
PROGRAM_PHASES = frozenset(
    {PROGRAM_PHASE_CHECK, PROGRAM_PHASE_MEASURE, PROGRAM_PHASE_VERIFY}
)

KIND_SILENCE = "silence"
KIND_PILOT = "pilot"
KIND_SWEEP = "sweep"
KIND_SUMMED_SWEEP = "summed_sweep"
STIMULUS_KINDS = frozenset({KIND_PILOT, KIND_SWEEP, KIND_SUMMED_SWEEP})
# See the module docstring (courtesy-tone prelude ordering).
KIND_COURTESY_TONE = "courtesy_tone"
# Segments program_admission.py's energy check must expect as non-silent.
KNOWN_AUDIBLE_KINDS = STIMULUS_KINDS | frozenset({KIND_COURTESY_TONE})

# Looked up by program_analysis for the pilot SNR guard (issue #1810).
AMBIENT_SEGMENT_ID = "ambient"

# [150 Hz, 23 kHz] (design §5.2); upper edge pinned equal to
# test_signal_plan.MAX_DRIVER_TEST_FREQUENCY_HZ (PR-A, #1668) by a test.
MEASURE_SWEEP_F_LO_HZ = 150.0
MEASURE_SWEEP_F_HI_HZ = 23_000.0

# Per-driver occurrences in MEASURE (#1668): N-1 bit-identical repeats feed
# the drift/glitch estimator (§3.1); must stay under
# CROSSOVER_CAPTURE_MAX_WAV_BYTES (5 MiB), pinned by a test.
MEASURE_REPEAT_COUNT = 3

# Unit-peak reference level the per-segment digital gain applies ON TOP of.
BASE_STIMULUS_PEAK_DBFS = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

# Finite floor recorded as a silence segment's peak (JSON is nan/inf-free).
SILENCE_PEAK_DBFS = -120.0

# --- CHECK phase defaults ---
DEFAULT_CHECK_AMBIENT_S = 12.0
DEFAULT_PILOT_DURATION_S = 0.8
DEFAULT_PILOT_GAP_S = 0.5
# 10 dB apart, for the behavioral linearity check (design §3.4).
DEFAULT_PILOT_LEVELS_DB = (-10.0, 0.0)

# Pre-pilot ambient silence (issue #1810), 1 s: stable RMS estimate at the
# lowest pilot edge (150-200 Hz). NAMED RESIDUAL: more transient-sensitive
# than CHECK's estimator, accepted since the failure mode is a retryable
# false negative and jts3 hardware measured ~26-30 dB margin over the floor.
PILOT_AMBIENT_WINDOW_S = 1.0

# --- MEASURE phase defaults ---
DEFAULT_MEASURE_GUARD_S = 2.0
DEFAULT_WOOFER_SWEEP_S = 4.0
DEFAULT_TWEETER_SWEEP_S = 3.0
DEFAULT_MEASURE_TAIL_S = 0.5

# --- MESM inter-sweep gap rule (design §5.3), see mesm_gap_samples ---
DEFAULT_IR_TAIL_S = 0.5
MESM_MAX_HARMONIC_ORDER = 3
MESM_GAP_FLOOR_S = 1.0

# --- VERIFY phase defaults ---
DEFAULT_VERIFY_GUARD_S = 1.5
DEFAULT_VERIFY_SWEEP_S = 6.0
DEFAULT_VERIFY_TAIL_S = 0.5
VERIFY_F_LO_HZ = 150.0
VERIFY_F_HI_HZ = 20_000.0

# Run-up past each crossover shoulder (null-confirm); 1.25x (~1/3 octave)
# keeps both Fc/2, 2*Fc read points off the edge bin.
NULL_CONFIRM_SHOULDER_MARGIN = 1.25

#: Gate fade, in seconds — enough to not step the waveform into a click.
NULL_CONFIRM_GATE_FADE_S = 0.010

# Leading VERIFY pilot's OWN band:
# 200-800 Hz PROVISIONAL flat region, clamped to fc/VERIFY_PILOT_FC_CLEARANCE_RATIO,
# falling back to [fc/8, fc/4].
VERIFY_PILOT_F_LO_HZ = 200.0
VERIFY_PILOT_F_HI_HZ = 800.0
VERIFY_PILOT_FC_CLEARANCE_RATIO = 2.5

# --- courtesy-tone prelude (issue #1677): fixed shape, opt-in via courtesy_prelude=True ---
COURTESY_TONE_BEEP_COUNT = 3
COURTESY_TONE_BEEP_HZ = 1000.0
COURTESY_TONE_BEEP_DURATION_S = 0.12
COURTESY_TONE_BEEP_GAP_S = 0.12
COURTESY_TONE_TRAILING_SILENCE_S = 3.0  # "~3 s to go quiet" (owner spec).
COURTESY_TONE_MARGIN_DB = 6.0
# Longest gap between the last beep and the first audible content; pinned by tests.
COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S = (
    COURTESY_TONE_TRAILING_SILENCE_S + PILOT_AMBIENT_WINDOW_S
)


@dataclass(frozen=True)
class RoleBand:
    """One driver's routing + declared band, the composer's per-driver input.

    ``channel`` is the program-WAV channel (ch0 woofer, ch1 tweeter, §5.4).
    ``band`` is intersected with the phase's swept window at compose time.
    """

    role: str
    channel: int
    band: FrequencyBand

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("role must be a non-empty string")
        if type(self.channel) is not int or self.channel < 0:
            raise ValueError("channel must be a non-negative integer")
        if not isinstance(self.band, FrequencyBand):
            raise ValueError("band must be a FrequencyBand")


@dataclass(frozen=True)
class ProgramSegment:
    """One scheduled stimulus (or silence) inside an excitation program.

    A silence segment carries ``role``/``channel``/``f*_hz`` as ``None``.
    ``effective_peak_dbfs`` is ``gain_db + downstream_gain_db``, the
    admission input. The gate fields silence part of the sweep without
    changing the parent waveform (:func:`segment_emitted_band_hz` gives the
    actual emitted band); default is "no gate", omitted by :meth:`to_dict`
    for byte-identical ``program_id`` on pre-gate programs.
    """

    segment_id: str
    kind: str
    role: str | None
    channel: int | None
    start_sample: int
    n_samples: int
    f1_hz: float | None
    f2_hz: float | None
    gain_db: float
    effective_peak_dbfs: float
    gate_start_sample: int = 0
    gate_end_sample: int | None = None
    gate_fade_samples: int = 0

    def __post_init__(self) -> None:
        if self.kind not in (KNOWN_AUDIBLE_KINDS | {KIND_SILENCE}):
            raise ValueError(f"unknown segment kind: {self.kind!r}")
        if type(self.start_sample) is not int or self.start_sample < 0:
            raise ValueError("start_sample must be a non-negative integer")
        if type(self.n_samples) is not int or self.n_samples <= 0:
            raise ValueError("n_samples must be a positive integer")
        is_stimulus = self.kind in STIMULUS_KINDS
        if is_stimulus and (self.f1_hz is None or self.f2_hz is None):
            raise ValueError("a stimulus segment must carry f1_hz and f2_hz")
        if is_stimulus and self.channel is None:
            raise ValueError("a stimulus segment must carry a channel")
        gate_end = (
            self.n_samples if self.gate_end_sample is None else self.gate_end_sample
        )
        if type(self.gate_start_sample) is not int or self.gate_start_sample < 0:
            raise ValueError("gate_start_sample must be a non-negative integer")
        if type(gate_end) is not int or gate_end > self.n_samples:
            raise ValueError("gate_end_sample must not exceed n_samples")
        if gate_end <= self.gate_start_sample:
            raise ValueError("a gate must leave at least one sample audible")
        if type(self.gate_fade_samples) is not int or self.gate_fade_samples < 0:
            raise ValueError("gate_fade_samples must be a non-negative integer")
        if 2 * self.gate_fade_samples > gate_end - self.gate_start_sample:
            raise ValueError("the gate's fades do not fit inside its open window")

    @property
    def is_gated(self) -> bool:
        """Does this segment silence part of its own sweep?"""
        return (
            self.gate_start_sample != 0
            or self.gate_end_sample is not None
            or self.gate_fade_samples != 0
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "segment_id": self.segment_id,
            "kind": self.kind,
            "role": self.role,
            "channel": self.channel,
            "start_sample": self.start_sample,
            "n_samples": self.n_samples,
            "f1_hz": self.f1_hz,
            "f2_hz": self.f2_hz,
            "gain_db": self.gain_db,
            "effective_peak_dbfs": self.effective_peak_dbfs,
        }
        # Omitted when there is no gate, for byte-identical program_id on pre-gate programs.
        if self.is_gated:
            payload["gate_start_sample"] = self.gate_start_sample
            payload["gate_end_sample"] = self.gate_end_sample
            payload["gate_fade_samples"] = self.gate_fade_samples
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProgramSegment":
        required = {
            "segment_id", "kind", "role", "channel", "start_sample",
            "n_samples", "f1_hz", "f2_hz", "gain_db", "effective_peak_dbfs",
        }
        gate_keys = {"gate_start_sample", "gate_end_sample", "gate_fade_samples"}
        if not isinstance(value, Mapping):
            raise ValueError("program segment schema is invalid")
        # Gate keys travel as a SET or not at all — partial keys can't reconstruct a gate.
        present = set(value)
        if present != required and present != required | gate_keys:
            raise ValueError("program segment schema is invalid")
        channel = value["channel"]
        return cls(
            segment_id=str(value["segment_id"]),
            kind=str(value["kind"]),
            role=None if value["role"] is None else str(value["role"]),
            channel=None if channel is None else int(channel),
            start_sample=int(value["start_sample"]),
            n_samples=int(value["n_samples"]),
            f1_hz=None if value["f1_hz"] is None else float(value["f1_hz"]),
            f2_hz=None if value["f2_hz"] is None else float(value["f2_hz"]),
            gain_db=float(value["gain_db"]),
            effective_peak_dbfs=float(value["effective_peak_dbfs"]),
            gate_start_sample=int(value.get("gate_start_sample", 0)),
            gate_end_sample=(
                None if value.get("gate_end_sample") is None
                else int(value["gate_end_sample"])
            ),
            gate_fade_samples=int(value.get("gate_fade_samples", 0)),
        )


@dataclass(frozen=True)
class ExcitationProgram:
    """A pure-data schedule of stimuli the session plays as one stream.

    ``program_id`` is a content hash over the schedule, so a re-run with a
    different program can never be mistaken for a resume of the old one.
    """

    program_id: str
    phase: str
    sample_rate_hz: int
    channels: int
    segments: tuple[ProgramSegment, ...]
    total_samples: int

    def __post_init__(self) -> None:
        if self.phase not in PROGRAM_PHASES:
            raise ValueError(f"unknown phase: {self.phase!r}")
        if self.sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if self.channels <= 0:
            raise ValueError("channels must be positive")
        if not self.segments:
            raise ValueError("a program must have at least one segment")
        for seg in self.segments:
            if seg.channel is not None and not 0 <= seg.channel < self.channels:
                raise ValueError(
                    f"segment {seg.segment_id!r} channel {seg.channel} out of "
                    f"range for {self.channels} channels"
                )
            if seg.start_sample + seg.n_samples > self.total_samples:
                raise ValueError(
                    f"segment {seg.segment_id!r} overruns total_samples"
                )
        expected = _program_id(
            self.phase, self.sample_rate_hz, self.channels,
            self.segments, self.total_samples,
        )
        if self.program_id != expected:
            raise ValueError("program_id does not match the schedule content")

    def segment(self, segment_id: str) -> ProgramSegment:
        for seg in self.segments:
            if seg.segment_id == segment_id:
                return seg
        raise KeyError(segment_id)

    def stimulus_segments(self) -> tuple[ProgramSegment, ...]:
        return tuple(s for s in self.segments if s.kind in STIMULUS_KINDS)

    def known_audible_segments(self) -> tuple[ProgramSegment, ...]:
        """Stimulus segments plus the courtesy-tone prelude — see ``KNOWN_AUDIBLE_KINDS``."""
        return tuple(s for s in self.segments if s.kind in KNOWN_AUDIBLE_KINDS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROGRAM_SCHEMA_VERSION,
            "kind": PROGRAM_KIND,
            "program_id": self.program_id,
            "phase": self.phase,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "segments": [s.to_dict() for s in self.segments],
            "total_samples": self.total_samples,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExcitationProgram":
        required = {
            "schema_version", "kind", "program_id", "phase", "sample_rate_hz",
            "channels", "segments", "total_samples",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("excitation program schema is invalid")
        if value["schema_version"] != PROGRAM_SCHEMA_VERSION:
            raise ValueError("unsupported program schema version")
        if value["kind"] != PROGRAM_KIND:
            raise ValueError("artifact is not an excitation program")
        segments = tuple(
            ProgramSegment.from_dict(s) for s in value["segments"]
        )
        return cls(
            program_id=str(value["program_id"]),
            phase=str(value["phase"]),
            sample_rate_hz=int(value["sample_rate_hz"]),
            channels=int(value["channels"]),
            segments=segments,
            total_samples=int(value["total_samples"]),
        )


def _canonical_segment(seg: ProgramSegment) -> dict[str, Any]:
    return seg.to_dict()


def _program_id(
    phase: str,
    sample_rate_hz: int,
    channels: int,
    segments: Sequence[ProgramSegment],
    total_samples: int,
) -> str:
    payload = {
        "schema_version": PROGRAM_SCHEMA_VERSION,
        "kind": PROGRAM_KIND,
        "phase": phase,
        "sample_rate_hz": sample_rate_hz,
        "channels": channels,
        "segments": [_canonical_segment(s) for s in segments],
        "total_samples": total_samples,
    }
    blob = json.dumps(
        payload, allow_nan=False, ensure_ascii=True,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _finalize(
    phase: str, channels: int, segments: Sequence[ProgramSegment], total: int
) -> ExcitationProgram:
    seg_tuple = tuple(segments)
    program_id = _program_id(
        phase, PROGRAM_SAMPLE_RATE_HZ, channels, seg_tuple, total
    )
    return ExcitationProgram(
        program_id=program_id,
        phase=phase,
        sample_rate_hz=PROGRAM_SAMPLE_RATE_HZ,
        channels=channels,
        segments=seg_tuple,
        total_samples=total,
    )


def _seconds_to_samples(seconds: float, sample_rate: int) -> int:
    if not (seconds > 0) or not math.isfinite(seconds):
        raise ValueError("duration seconds must be finite and positive")
    return int(round(seconds * sample_rate))


def _intersect_band(band: FrequencyBand, lo_hz: float, hi_hz: float) -> tuple[float, float]:
    f1 = max(float(band.lower_hz), lo_hz)
    f2 = min(float(band.upper_hz), hi_hz)
    if not f1 < f2:
        raise ValueError(
            f"driver band [{band.lower_hz:g},{band.upper_hz:g}] does not "
            f"intersect [{lo_hz:g},{hi_hz:g}]"
        )
    return f1, f2


def _sweep_meta(
    f1_hz: float, f2_hz: float, duration_s: float, gain_db: float
) -> SweepMeta:
    """Realized synchronized-sweep metadata for a band/duration/gain triple.

    ``gain_db`` becomes the sweep's ``amplitude_dbfs`` and must be
    non-positive (:func:`synchronized_sweep_metadata` enforces this).
    """
    return synchronized_sweep_metadata(
        f1=f1_hz,
        f2=f2_hz,
        duration_approx_s=duration_s,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
        amplitude_dbfs=gain_db,
    )


def _silence(segment_id: str, start: int, n_samples: int) -> ProgramSegment:
    return ProgramSegment(
        segment_id=segment_id,
        kind=KIND_SILENCE,
        role=None,
        channel=None,
        start_sample=start,
        n_samples=n_samples,
        f1_hz=None,
        f2_hz=None,
        gain_db=0.0,
        effective_peak_dbfs=SILENCE_PEAK_DBFS,
    )


def _stimulus(
    *,
    segment_id: str,
    kind: str,
    role: str | None,
    channel: int,
    start: int,
    f1_hz: float,
    f2_hz: float,
    duration_s: float,
    gain_db: float,
    downstream_gain_db: float,
) -> ProgramSegment:
    meta = _sweep_meta(f1_hz, f2_hz, duration_s, gain_db)
    return ProgramSegment(
        segment_id=segment_id,
        kind=kind,
        role=role,
        channel=channel,
        start_sample=start,
        n_samples=meta.n_samples,
        f1_hz=meta.f1,
        f2_hz=meta.f2,
        gain_db=float(gain_db),
        effective_peak_dbfs=float(gain_db + downstream_gain_db),
    )


def _append_pilot_ambient_window(
    segments: list[ProgramSegment], cursor: int,
) -> int:
    """Append the pre-pilot ambient window (:data:`AMBIENT_SEGMENT_ID`, issue
    #1810); return the cursor."""
    n = _seconds_to_samples(PILOT_AMBIENT_WINDOW_S, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence(AMBIENT_SEGMENT_ID, cursor, n))
    return cursor + n


def _append_leading_pilot_pair(
    segments: list[ProgramSegment],
    cursor: int,
    *,
    role: str,
    channel: int,
    f1_hz: float,
    f2_hz: float,
    gains_db: tuple[float, float],
    pilot_duration_s: float,
    pilot_gap_s: float,
    downstream_gain_db: float,
) -> int:
    """Append a two-level pilot pair (lo then hi) + trailing gaps; return cursor.

    Same segment-id shape as CHECK's pilots (``pilot_{role}_lo``/``_hi``) so
    ``program_analysis`` reuses one pilot reader across all phases.
    ``gains_db`` is ``(lo, hi)`` absolute digital gains (dBFS, non-positive).
    """
    gap_n = _seconds_to_samples(pilot_gap_s, PROGRAM_SAMPLE_RATE_HZ)
    for suffix, gain_db in (("lo", gains_db[0]), ("hi", gains_db[1])):
        seg = _stimulus(
            segment_id=f"pilot_{role}_{suffix}",
            kind=KIND_PILOT,
            role=role,
            channel=channel,
            start=cursor,
            f1_hz=f1_hz,
            f2_hz=f2_hz,
            duration_s=pilot_duration_s,
            gain_db=gain_db,
            downstream_gain_db=downstream_gain_db,
        )
        segments.append(seg)
        cursor += seg.n_samples
        segments.append(_silence(f"pilot_gap_{role}_{suffix}", cursor, gap_n))
        cursor += gap_n
    return cursor


def _validate_roles(roles_bands: Sequence[RoleBand]) -> tuple[RoleBand, ...]:
    roles = tuple(roles_bands)
    if not roles:
        raise ValueError("roles_bands must be non-empty")
    channels = [rb.channel for rb in roles]
    if len(set(channels)) != len(channels):
        raise ValueError("each driver must own a distinct program channel")
    if len({rb.role for rb in roles}) != len(roles):
        raise ValueError("driver roles must be distinct")
    return roles


def mesm_gap_samples(
    preceding: SweepMeta,
    *,
    ir_tail_s: float = DEFAULT_IR_TAIL_S,
    max_harmonic_order: int = MESM_MAX_HARMONIC_ORDER,
    floor_s: float = MESM_GAP_FLOOR_S,
    sample_rate: int = PROGRAM_SAMPLE_RATE_HZ,
) -> int:
    """Inter-sweep gap that satisfies the MESM constraint for ``preceding``.

    ``gap_s = max(floor_s, ir_tail_s + L*ln(max_harmonic_order))`` — clears
    both the IR/reverb tail and the harmonic pre-ring (design §5.3).
    """
    if max_harmonic_order < 2:
        raise ValueError("max_harmonic_order must be at least 2")
    if not (ir_tail_s >= 0) or not math.isfinite(ir_tail_s):
        raise ValueError("ir_tail_s must be finite and non-negative")
    pre_ring_s = float(preceding.L) * math.log(max_harmonic_order)
    gap_s = max(floor_s, ir_tail_s + pre_ring_s)
    return _seconds_to_samples(gap_s, sample_rate)


# courtesy-tone prelude (issue #1677)


def courtesy_tone_gain_db(
    reference_gain_db: float, *, margin_db: float = COURTESY_TONE_MARGIN_DB,
) -> float:
    """Courtesy tone gain: ``margin_db`` below ``reference_gain_db``, clamped
    to never equal/exceed it and never positive."""
    return min(reference_gain_db - margin_db, reference_gain_db, 0.0)


def _courtesy_tone_n_samples() -> int:
    """Total samples of one courtesy-tone segment (beeps + inter-beep gaps,
    excluding the trailing silence)."""
    beep_n = _seconds_to_samples(COURTESY_TONE_BEEP_DURATION_S, PROGRAM_SAMPLE_RATE_HZ)
    gap_n = _seconds_to_samples(COURTESY_TONE_BEEP_GAP_S, PROGRAM_SAMPLE_RATE_HZ)
    return COURTESY_TONE_BEEP_COUNT * beep_n + (COURTESY_TONE_BEEP_COUNT - 1) * gap_n


def _courtesy_tone_burst(gain_db: float):
    """Synthesize the courtesy tone's float32 PCM: fixed beep count/frequency/gaps,
    deterministic from ``gain_db`` alone."""
    import numpy as np

    sr = PROGRAM_SAMPLE_RATE_HZ
    beep_n = _seconds_to_samples(COURTESY_TONE_BEEP_DURATION_S, sr)
    gap_n = _seconds_to_samples(COURTESY_TONE_BEEP_GAP_S, sr)
    amp = 10.0 ** (gain_db / 20.0)
    t = np.arange(beep_n, dtype=np.float64) / sr
    beep = (amp * np.sin(2.0 * np.pi * COURTESY_TONE_BEEP_HZ * t)).astype(np.float32)
    # Quadratic power-ramp fade in/out per beep — avoids a hard-edge click.
    fade_n = min(max(8, int(0.005 * sr)), beep_n // 2)
    if fade_n > 0:
        fade_in = (np.linspace(0.0, 1.0, fade_n, dtype=np.float64) ** 2).astype(np.float32)
        beep[:fade_n] *= fade_in
        beep[-fade_n:] *= fade_in[::-1]
    gap = np.zeros(gap_n, dtype=np.float32)
    parts = []
    for i in range(COURTESY_TONE_BEEP_COUNT):
        parts.append(beep)
        if i < COURTESY_TONE_BEEP_COUNT - 1:
            parts.append(gap)
    return np.concatenate(parts)


def courtesy_tone_stimulus(segment: ProgramSegment):
    """Regenerate the exact float32 courtesy-tone PCM for one prelude segment.
    A length mismatch means a corrupt schedule."""
    if segment.kind != KIND_COURTESY_TONE:
        raise ValueError(
            "courtesy_tone_stimulus is only defined for courtesy-tone segments"
        )
    tone = _courtesy_tone_burst(segment.gain_db)
    if tone.size != segment.n_samples:
        raise ValueError(
            f"segment {segment.segment_id!r} courtesy-tone reconstruction "
            f"produced {tone.size} samples, schedule says {segment.n_samples}"
        )
    return tone


def _insert_courtesy_prelude(
    segments: list[ProgramSegment],
    total_samples: int,
    *,
    at_sample: int,
    channels: int,
    downstream_gain_db: float,
) -> tuple[list[ProgramSegment], int]:
    """Splice the courtesy-tone prelude in at ``at_sample``, shifting the rest.

    One tone segment per channel, gain derived from that channel's own
    loudest scheduled stimulus (:func:`courtesy_tone_gain_db`), followed by
    :data:`COURTESY_TONE_TRAILING_SILENCE_S`.
    """
    tone_n = _courtesy_tone_n_samples()
    at = int(at_sample)
    tone_segments: list[ProgramSegment] = []
    for channel in range(channels):
        channel_gains = [
            seg.gain_db for seg in segments
            if seg.kind in STIMULUS_KINDS and seg.channel == channel
        ]
        if not channel_gains:
            continue
        gain_db = courtesy_tone_gain_db(max(channel_gains))
        tone_segments.append(ProgramSegment(
            segment_id=f"courtesy_tone_ch{channel}",
            kind=KIND_COURTESY_TONE,
            role=None,
            channel=channel,
            start_sample=at,
            n_samples=tone_n,
            f1_hz=COURTESY_TONE_BEEP_HZ,
            f2_hz=COURTESY_TONE_BEEP_HZ,
            gain_db=gain_db,
            effective_peak_dbfs=gain_db + downstream_gain_db,
        ))
    gap_n = _seconds_to_samples(COURTESY_TONE_TRAILING_SILENCE_S, PROGRAM_SAMPLE_RATE_HZ)
    gap_seg = _silence("courtesy_gap", at + tone_n, gap_n)
    prelude_n = tone_n + gap_n
    head = [seg for seg in segments if seg.start_sample < at]
    tail = [
        replace(seg, start_sample=seg.start_sample + prelude_n)
        for seg in segments
        if seg.start_sample >= at
    ]
    return [*head, *tone_segments, gap_seg, *tail], total_samples + prelude_n


def build_check_program(
    roles_bands: Sequence[RoleBand],
    *,
    ambient_s: float = DEFAULT_CHECK_AMBIENT_S,
    pilot_levels_db: tuple[float, float] = DEFAULT_PILOT_LEVELS_DB,
    pilot_duration_s: float = DEFAULT_PILOT_DURATION_S,
    pilot_gap_s: float = DEFAULT_PILOT_GAP_S,
    base_peak_dbfs: float = BASE_STIMULUS_PEAK_DBFS,
    downstream_gain_db: float = 0.0,
    role_base_peak_dbfs: Mapping[str, float] | None = None,
    courtesy_prelude: bool = False,
) -> ExcitationProgram:
    """Compose the CHECK program (design §5.2): ambient silence + per-driver pilots.

    Leading silence is the session ambient measurement. Then, per driver,
    two band-limited pilot ESS chirps at ``pilot_levels_db`` (RELATIVE
    offsets: pilot digital gain = ``base_peak_dbfs + level``).
    ``role_base_peak_dbfs`` opt-in overrides ``base_peak_dbfs`` PER ROLE.
    ``courtesy_prelude`` opt-in prepends the beep-beep-beep warning (module
    docstring); both opt-ins default to byte-identical without them.
    """
    roles = _validate_roles(roles_bands)
    if len(pilot_levels_db) != 2:
        raise ValueError("pilot_levels_db must be exactly two levels")
    channels = 1 + max(rb.channel for rb in roles)

    segments: list[ProgramSegment] = []
    cursor = 0
    ambient_n = _seconds_to_samples(ambient_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence(AMBIENT_SEGMENT_ID, cursor, ambient_n))
    cursor += ambient_n
    # The ambient window is silent, so it may sit between the beeps and the pilots.
    prelude_at = cursor

    gap_n = _seconds_to_samples(pilot_gap_s, PROGRAM_SAMPLE_RATE_HZ)
    for rb in roles:
        f1_hz, f2_hz = _intersect_band(
            rb.band, MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ
        )
        role_base = (
            role_base_peak_dbfs.get(rb.role, base_peak_dbfs)
            if role_base_peak_dbfs is not None
            else base_peak_dbfs
        )
        for suffix, level in (("lo", pilot_levels_db[0]), ("hi", pilot_levels_db[1])):
            gain_db = role_base + level
            seg = _stimulus(
                segment_id=f"pilot_{rb.role}_{suffix}",
                kind=KIND_PILOT,
                role=rb.role,
                channel=rb.channel,
                start=cursor,
                f1_hz=f1_hz,
                f2_hz=f2_hz,
                duration_s=pilot_duration_s,
                gain_db=gain_db,
                downstream_gain_db=downstream_gain_db,
            )
            segments.append(seg)
            cursor += seg.n_samples
            segments.append(_silence(f"gap_{rb.role}_{suffix}", cursor, gap_n))
            cursor += gap_n

    if courtesy_prelude:
        segments, cursor = _insert_courtesy_prelude(
            segments, cursor, at_sample=prelude_at, channels=channels,
            downstream_gain_db=downstream_gain_db,
        )
    return _finalize(PROGRAM_PHASE_CHECK, channels, segments, cursor)


def _occurrence_suffix(index: int) -> str:
    """Segment-ID suffix for the ``index``-th (0-based) occurrence of a
    repeated MEASURE sweep: bare for the first (``sweep_w``), ``_rep`` for
    the second, ``_rep{n}`` for the (n+1)-th thereafter.
    """
    if index <= 0:
        return ""
    if index == 1:
        return "_rep"
    return f"_rep{index}"


def build_measure_program(
    gain_plan: Mapping[str, float],
    roles_bands: Sequence[RoleBand],
    *,
    repeat_count: int = MEASURE_REPEAT_COUNT,
    sweep_durations: Mapping[str, float] | None = None,
    sweep_duration_limits_s: Mapping[str, float] | None = None,
    guard_s: float = DEFAULT_MEASURE_GUARD_S,
    tail_s: float = DEFAULT_MEASURE_TAIL_S,
    ir_tail_s: float = DEFAULT_IR_TAIL_S,
    downstream_gain_db: float = 0.0,
    leading_pilot_gains_db: tuple[float, float] | None = None,
    leading_pilot_role: str | None = None,
    pilot_duration_s: float = DEFAULT_PILOT_DURATION_S,
    pilot_gap_s: float = DEFAULT_PILOT_GAP_S,
    courtesy_prelude: bool = False,
) -> ExcitationProgram:
    """Compose the MEASURE program (design §5.2/§5.4): ``repeat_count``
    interleaved sweep cycles, one per declared driver. ``roles_bands[0]`` is
    the lower driver (woofer, ch0); on a 2-way ``roles_bands[1]`` is the
    upper (tweeter, ch1). Repeats beyond a driver's first are bit-identical
    (in-capture drift/glitch estimator, §3.1). ``gain_plan`` maps role to
    digital gain (dBFS, non-positive). ``sweep_duration_limits_s`` maps role
    to the longest ONE sweep its safety limits allow; a nominal duration can
    round up past it (150-4000 Hz asked for 4.0 s realizes 4.00577 s), so
    this fits the longest phase-closing sweep at or below it instead
    (:func:`~jasper.audio_measurement.sweep.phase_closing_duration_s`,
    logging ``event=measure_program.sweep_fitted``) at ~0.024 dB SNR cost.
    Segment IDs: each driver's first occurrence keeps exactly ``sweep_w``/
    ``sweep_t``; later ones follow :func:`_occurrence_suffix`.
    ``leading_pilot_gains_db`` and ``courtesy_prelude`` are opt-ins (module
    docstring), byte-identical to the pre-v2 shape when omitted.
    """
    roles = _validate_roles(roles_bands)
    if not 1 <= len(roles) <= 2:
        raise ValueError("MEASURE takes one or two drivers")
    woofer = roles[0]
    tweeter = roles[1] if len(roles) == 2 else None
    for rb in roles:
        if rb.role not in gain_plan:
            raise ValueError(f"gain_plan is missing role {rb.role!r}")
    if type(repeat_count) is not int or repeat_count < 1:
        raise ValueError("repeat_count must be a positive integer")
    durations = {woofer.role: DEFAULT_WOOFER_SWEEP_S}
    if tweeter is not None:
        durations[tweeter.role] = DEFAULT_TWEETER_SWEEP_S
    if sweep_durations:
        durations.update(sweep_durations)
    channels = 1 + max(rb.channel for rb in roles)

    def _band(rb: RoleBand) -> tuple[float, float]:
        f1, f2 = _intersect_band(rb.band, MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ)
        # Defense in depth: MEASURE_SWEEP_F_HI_HZ < Nyquist today (#1668).
        nyquist_hz = PROGRAM_SAMPLE_RATE_HZ / 2.0
        if not f2 < nyquist_hz:
            raise ValueError(
                f"{rb.role} MEASURE sweep upper edge {f2:g} Hz is not below "
                f"Nyquist ({nyquist_hz:g} Hz at {PROGRAM_SAMPLE_RATE_HZ} Hz "
                "sample rate)"
            )
        return f1, f2

    def _fitted_meta(rb: RoleBand, f1: float, f2: float) -> SweepMeta:
        """This role's sweep, shortened to its duration limit when it overruns.

        Writes the fitted length back into ``durations`` so every occurrence
        of this role composes at it.
        """
        gain_db = gain_plan[rb.role]
        meta = _sweep_meta(f1, f2, durations[rb.role], gain_db)
        limit = (
            None if sweep_duration_limits_s is None
            else sweep_duration_limits_s.get(rb.role)
        )
        if limit is None or meta.duration_s <= float(limit):
            return meta
        try:
            fitted_s = phase_closing_duration_s(
                f1, f2, at_or_below_s=float(limit),
                sample_rate=PROGRAM_SAMPLE_RATE_HZ,
            )
        except ValueError as exc:
            raise ValueError(
                f"{rb.role} MEASURE sweep over [{f1:g},{f2:g}] Hz cannot close "
                f"its phase within its {float(limit):g} s duration limit"
            ) from exc
        fitted = _sweep_meta(f1, f2, fitted_s, gain_db)
        durations[rb.role] = fitted.duration_s
        log_event(
            logger,
            "measure_program.sweep_fitted",
            role=rb.role,
            sweep_fitted_s=round(fitted.duration_s, 6),
            sweep_nominal_s=round(meta.duration_s, 6),
            limit_s=round(float(limit), 6),
        )
        return fitted

    w_f1, w_f2 = _band(woofer)
    w_meta = _fitted_meta(woofer, w_f1, w_f2)
    gap_w_n = mesm_gap_samples(w_meta, ir_tail_s=ir_tail_s)
    t_band: tuple[float, float] | None = None
    gap_t_n = 0
    if tweeter is not None:
        t_band = _band(tweeter)
        gap_t_n = mesm_gap_samples(
            _fitted_meta(tweeter, *t_band), ir_tail_s=ir_tail_s
        )

    segments: list[ProgramSegment] = []
    cursor = 0
    prelude_at: int | None = None
    if leading_pilot_gains_db is not None:
        if len(leading_pilot_gains_db) != 2:
            raise ValueError("leading_pilot_gains_db must be exactly two levels")
        pilot_rb = woofer
        if leading_pilot_role is not None:
            matches = [rb for rb in roles if rb.role == leading_pilot_role]
            if not matches:
                raise ValueError(
                    f"leading_pilot_role {leading_pilot_role!r} is not a declared role"
                )
            pilot_rb = matches[0]
        p_f1, p_f2 = _band(pilot_rb)
        # Full-gain audible stimulus: beeps go at sample 0 (#1812).
        prelude_at = cursor
        cursor = _append_pilot_ambient_window(segments, cursor)
        cursor = _append_leading_pilot_pair(
            segments, cursor,
            role=pilot_rb.role,
            channel=pilot_rb.channel,
            f1_hz=p_f1,
            f2_hz=p_f2,
            gains_db=leading_pilot_gains_db,
            pilot_duration_s=pilot_duration_s,
            pilot_gap_s=pilot_gap_s,
            downstream_gain_db=downstream_gain_db,
        )
    guard_n = _seconds_to_samples(guard_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("guard", cursor, guard_n))
    cursor += guard_n
    if prelude_at is None:
        # No leading pilot pair: beeps land directly in front of the sweep.
        prelude_at = cursor

    def _sweep(segment_id: str, rb: RoleBand, f1: float, f2: float, dur: float) -> ProgramSegment:
        seg = _stimulus(
            segment_id=segment_id,
            kind=KIND_SWEEP,
            role=rb.role,
            channel=rb.channel,
            start=cursor,
            f1_hz=f1,
            f2_hz=f2,
            duration_s=dur,
            gain_db=gain_plan[rb.role],
            downstream_gain_db=downstream_gain_db,
        )
        return seg

    for cycle in range(repeat_count):
        suffix = _occurrence_suffix(cycle)
        sweep_w = _sweep(f"sweep_w{suffix}", woofer, w_f1, w_f2, durations[woofer.role])
        segments.append(sweep_w)
        cursor += sweep_w.n_samples
        if tweeter is None or t_band is None:
            # One declared driver: only silence between cycles is the MESM settle.
            if cycle < repeat_count - 1:
                segments.append(_silence(f"gap_w_w{suffix}", cursor, gap_w_n))
                cursor += gap_w_n
            continue
        segments.append(_silence(f"gap_w_t{suffix}", cursor, gap_w_n))
        cursor += gap_w_n

        sweep_t = _sweep(
            f"sweep_t{suffix}", tweeter, t_band[0], t_band[1], durations[tweeter.role]
        )
        segments.append(sweep_t)
        cursor += sweep_t.n_samples
        if cycle < repeat_count - 1:
            segments.append(_silence(f"gap_t_w{suffix}", cursor, gap_t_n))
            cursor += gap_t_n

    tail_n = _seconds_to_samples(tail_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("tail", cursor, tail_n))
    cursor += tail_n

    if courtesy_prelude:
        segments, cursor = _insert_courtesy_prelude(
            segments, cursor, at_sample=prelude_at, channels=channels,
            downstream_gain_db=downstream_gain_db,
        )
    return _finalize(PROGRAM_PHASE_MEASURE, channels, segments, cursor)


VERIFY_PILOT_ROLE = "summed"


def build_verify_program(
    fc_hz: float | None,
    *,
    measurement_band_hz: tuple[float, float] | None = None,
    gain_db: float = BASE_STIMULUS_PEAK_DBFS,
    guard_s: float = DEFAULT_VERIFY_GUARD_S,
    sweep_s: float = DEFAULT_VERIFY_SWEEP_S,
    tail_s: float = DEFAULT_VERIFY_TAIL_S,
    downstream_gain_db: float = 0.0,
    leading_pilot_gains_db: tuple[float, float] | None = None,
    pilot_duration_s: float = DEFAULT_PILOT_DURATION_S,
    pilot_gap_s: float = DEFAULT_PILOT_GAP_S,
    courtesy_prelude: bool = False,
) -> ExcitationProgram:
    """Compose the VERIFY program (design §5.2): a mono full-band summed
    sweep played through the applied production graph. ``fc_hz`` widens the
    low bound when the crossover is low: ``f1 = min(VERIFY_F_LO_HZ, fc/2)``.
    ``fc_hz=None`` is the NO-CROSSOVER mode, requiring ``measurement_band_hz``.
    ``leading_pilot_gains_db`` and ``courtesy_prelude`` are opt-ins (module
    docstring); the pilot rides its own band to avoid the
    crossover notch. VERIFY has no program-admission gate, so the prelude's
    compose-time clamp is the only level guard for it.
    """
    if fc_hz is None:
        if measurement_band_hz is None:
            raise ValueError(
                "a no-crossover VERIFY requires the declared measurement_band_hz"
            )
        band_lo, band_hi = (float(measurement_band_hz[0]), float(measurement_band_hz[1]))
        if not (0 < band_lo < band_hi) or not math.isfinite(band_hi):
            raise ValueError("measurement_band_hz must be a positive ascending band")
        f1_hz = min(VERIFY_F_LO_HZ, band_lo)
    else:
        if not (fc_hz > 0) or not math.isfinite(fc_hz):
            raise NullConfirmUnavailable(
                NULL_REFUSE_FC_INVALID, "fc_hz must be finite and positive"
            )
        f1_hz = min(VERIFY_F_LO_HZ, fc_hz / 2.0)
    f2_hz = VERIFY_F_HI_HZ
    if not f1_hz < f2_hz:
        raise ValueError("verify sweep band collapsed")

    segments: list[ProgramSegment] = []
    cursor = 0
    prelude_at: int | None = None
    if leading_pilot_gains_db is not None:
        if len(leading_pilot_gains_db) != 2:
            raise ValueError("leading_pilot_gains_db must be exactly two levels")
        # See VERIFY_PILOT_* constants for the band-clamp rationale.
        if fc_hz is None:
            pilot_lo = max(VERIFY_PILOT_F_LO_HZ, band_lo)
            pilot_hi = min(VERIFY_PILOT_F_HI_HZ, band_hi)
            if not pilot_lo < pilot_hi:
                pilot_lo, pilot_hi = band_lo, band_hi
        else:
            pilot_lo = VERIFY_PILOT_F_LO_HZ
            pilot_hi = min(VERIFY_PILOT_F_HI_HZ, fc_hz / VERIFY_PILOT_FC_CLEARANCE_RATIO)
            if not pilot_lo < pilot_hi:
                pilot_lo, pilot_hi = fc_hz / 8.0, fc_hz / 4.0
        prelude_at = cursor
        cursor = _append_pilot_ambient_window(segments, cursor)
        cursor = _append_leading_pilot_pair(
            segments, cursor,
            role=VERIFY_PILOT_ROLE,
            channel=0,
            f1_hz=pilot_lo,
            f2_hz=pilot_hi,
            gains_db=leading_pilot_gains_db,
            pilot_duration_s=pilot_duration_s,
            pilot_gap_s=pilot_gap_s,
            downstream_gain_db=downstream_gain_db,
        )
    guard_n = _seconds_to_samples(guard_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("guard", cursor, guard_n))
    cursor += guard_n
    if prelude_at is None:
        prelude_at = cursor

    sweep = _stimulus(
        segment_id="sweep_verify",
        kind=KIND_SUMMED_SWEEP,
        role=None,
        channel=0,
        start=cursor,
        f1_hz=f1_hz,
        f2_hz=f2_hz,
        duration_s=sweep_s,
        gain_db=gain_db,
        downstream_gain_db=downstream_gain_db,
    )
    segments.append(sweep)
    cursor += sweep.n_samples

    tail_n = _seconds_to_samples(tail_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("tail", cursor, tail_n))
    cursor += tail_n

    if courtesy_prelude:
        segments, cursor = _insert_courtesy_prelude(
            segments, cursor, at_sample=prelude_at, channels=1,
            downstream_gain_db=downstream_gain_db,
        )
    return _finalize(PROGRAM_PHASE_VERIFY, 1, segments, cursor)


#: Why a null confirm could not be composed. ``reason`` is the contract a
#: grader keys on; the message is operator copy and may be reworded.
NULL_REFUSE_FC_INVALID = "null_confirm_fc_invalid"
NULL_REFUSE_NO_SHOULDER_RUN_UP = "null_confirm_no_shoulder_run_up"
NULL_REFUSE_ROLE_BAND_DISJOINT = "null_confirm_role_band_disjoint"
NULL_REFUSE_OVERLAP_EXCLUDES_FC = "null_confirm_overlap_excludes_fc"
NULL_REFUSE_DURATION_UNCLOSEABLE = "null_confirm_duration_uncloseable"
NULL_REFUSE_LIMITS_INCOMPLETE = "null_confirm_limits_incomplete"


class NullConfirmUnavailable(ValueError):
    """This speaker has no confirmable null at the requested corner;
    carries ``reason`` for callers that must tell the cases apart."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def null_confirm_band_hz(
    fc_hz: float,
    *,
    shoulder_margin: float = NULL_CONFIRM_SHOULDER_MARGIN,
) -> tuple[float, float]:
    """The band a null confirm sweeps: both shoulders, inside VERIFY's envelope.

    Derived, never declared: depth is read at ``Fc/2``, ``Fc`` and ``2*Fc``,
    so the sweep spans both shoulders with :data:`NULL_CONFIRM_SHOULDER_MARGIN`
    of run-up. Clamped to VERIFY's own envelope, so a confirm sweep is
    always a frequency subset of VERIFY's summed sweep; a corner whose
    shoulders fall outside it raises rather than reading a clamped edge bin.
    """
    if not (fc_hz > 0) or not math.isfinite(fc_hz):
        raise NullConfirmUnavailable(
            NULL_REFUSE_FC_INVALID, "fc_hz must be finite and positive"
        )
    if not shoulder_margin >= 1.0:
        raise ValueError("shoulder_margin must be at least 1.0")
    lower_shoulder_hz = fc_hz / 2.0
    upper_shoulder_hz = fc_hz * 2.0
    # VERIFY's own edges.
    lo = max(lower_shoulder_hz / shoulder_margin, min(VERIFY_F_LO_HZ, fc_hz / 2.0))
    hi = min(upper_shoulder_hz * shoulder_margin, VERIFY_F_HI_HZ)
    # Strict on both sides: an edge on a shoulder reads off its own bin.
    if lo >= lower_shoulder_hz or hi <= upper_shoulder_hz:
        raise NullConfirmUnavailable(
            NULL_REFUSE_NO_SHOULDER_RUN_UP,
            f"a null confirm at fc={fc_hz:g} Hz needs run-up PAST the shoulders "
            f"[{lower_shoulder_hz:g},{upper_shoulder_hz:g}] Hz, and the summed "
            f"sweep envelope only reaches [{lo:g},{hi:g}] Hz; a shoulder read at "
            "the band edge is a clamped endpoint, not a measurement",
        )
    return lo, hi


def null_confirm_sweep_duration_s(
    f1_hz: float,
    f2_hz: float,
    roles: Sequence[RoleBand],
    sweep_duration_limits_s: Mapping[str, float] | None,
    *,
    nominal_s: float = DEFAULT_VERIFY_SWEEP_S,
) -> float:
    """How long the confirm's parent sweep may run: the longest phase-closing
    duration at or below the tightest role limit, or ``nominal_s`` when
    nothing binds (#2921)."""
    if not sweep_duration_limits_s:
        return nominal_s
    # A PARTIAL mapping is refused: an unlisted role would be fitted to no limit.
    missing = sorted(rb.role for rb in roles if rb.role not in sweep_duration_limits_s)
    if missing:
        raise NullConfirmUnavailable(
            NULL_REFUSE_LIMITS_INCOMPLETE,
            f"sweep duration limits cover only part of the confirm's roles; "
            f"{', '.join(missing)} would be fitted to no limit at all",
        )
    binding = min(float(sweep_duration_limits_s[rb.role]) for rb in roles)
    # Compare the REALIZED length — the kernel can round UP past the limit.
    realized = _sweep_meta(f1_hz, f2_hz, nominal_s, BASE_STIMULUS_PEAK_DBFS)
    if realized.duration_s <= binding:
        return realized.duration_s
    try:
        return phase_closing_duration_s(
            f1_hz, f2_hz, at_or_below_s=binding,
            sample_rate=PROGRAM_SAMPLE_RATE_HZ,
        )
    except ValueError as exc:
        raise NullConfirmUnavailable(
            NULL_REFUSE_DURATION_UNCLOSEABLE,
            f"a null confirm over [{f1_hz:g},{f2_hz:g}] Hz cannot close its "
            f"phase within the {binding:g} s limit its drivers allow",
        ) from exc


@dataclass(frozen=True)
class NullConfirmPlan:
    """The shared sweep band, each role's gate, and the two-branch overlap.

    ``overlap_hz`` is the band every branch is open at full amplitude,
    fades excluded — where a null depth's shoulders may honestly sit.
    """

    band_hz: tuple[float, float]
    gates_by_role: Mapping[str, tuple[int, int | None]]
    overlap_hz: tuple[float, float]


def null_confirm_channel_plan(
    fc_hz: float,
    roles: Sequence[RoleBand],
    *,
    sweep_s: float = DEFAULT_VERIFY_SWEEP_S,
    fade_s: float = NULL_CONFIRM_GATE_FADE_S,
) -> NullConfirmPlan:
    """The shared sweep band, each role's gate, and where both branches are open.

    Every driver plays the same parent sweep; each role's gate silences the
    part it may not be driven over. ``gates_by_role`` is
    ``(gate_start_sample, gate_end_sample)`` per role. Refuses when the
    two-channel overlap cannot bracket Fc with the fades clear of it.
    """
    roles = _validate_roles(roles)
    f1_hz, f2_hz = null_confirm_band_hz(fc_hz)
    meta = _sweep_meta(f1_hz, f2_hz, sweep_s, BASE_STIMULUS_PEAK_DBFS)
    n = meta.n_samples
    fade_n = _seconds_to_samples(fade_s, PROGRAM_SAMPLE_RATE_HZ)
    span = math.log(f2_hz / f1_hz)

    def _sample_at(freq: float, *, inward: str) -> int:
        """Sample index for one frequency, rounded INTO the declared band
        (ceil the opening edge, floor the closing one) so the gate stays
        conservatively inside the declaration."""
        exact = n * math.log(freq / f1_hz) / span
        return math.ceil(exact) if inward == "up" else math.floor(exact)

    gates: dict[str, tuple[int, int | None]] = {}
    open_lo: dict[str, float] = {}
    open_hi: dict[str, float] = {}
    for rb in roles:
        lo_edge = max(f1_hz, float(rb.band.lower_hz))
        hi_edge = min(f2_hz, float(rb.band.upper_hz))
        if not lo_edge < hi_edge:
            raise NullConfirmUnavailable(
                NULL_REFUSE_ROLE_BAND_DISJOINT,
                f"the {rb.role}'s declared band "
                f"[{rb.band.lower_hz:g},{rb.band.upper_hz:g}] Hz does not "
                f"intersect the confirm sweep [{f1_hz:g},{f2_hz:g}] Hz",
            )
        start = _sample_at(lo_edge, inward="up") if lo_edge > f1_hz else 0
        end = _sample_at(hi_edge, inward="down") if hi_edge < f2_hz else None
        gates[rb.role] = (start, end)
        # Full-amplitude window, fades excluded; fades only exist where the gate cuts.
        stop = n if end is None else end
        open_lo[rb.role] = f1_hz * math.exp(
            span * (start + (fade_n if start else 0)) / n
        )
        open_hi[rb.role] = f1_hz * math.exp(
            span * (stop - (fade_n if end is not None else 0)) / n
        )

    overlap_lo = max(open_lo.values())
    overlap_hi = min(open_hi.values())
    if not overlap_lo < fc_hz < overlap_hi:
        raise NullConfirmUnavailable(
            NULL_REFUSE_OVERLAP_EXCLUDES_FC,
            f"the declared driver bands leave every branch open only over "
            f"[{overlap_lo:g},{overlap_hi:g}] Hz, which does not bracket "
            f"fc={fc_hz:g} Hz with the gate fades clear of it; there is no "
            "coordinate at which these branches can be measured cancelling",
        )
    return NullConfirmPlan(
        band_hz=(f1_hz, f2_hz),
        gates_by_role=gates,
        overlap_hz=(overlap_lo, overlap_hi),
    )


def build_null_confirm_program(
    fc_hz: float,
    roles: Sequence[RoleBand],
    *,
    gain_db: float,
    guard_s: float = DEFAULT_VERIFY_GUARD_S,
    sweep_s: float | None = None,
    sweep_duration_limits_s: Mapping[str, float] | None = None,
    tail_s: float = DEFAULT_VERIFY_TAIL_S,
    downstream_gain_db: float = 0.0,
    fade_s: float = NULL_CONFIRM_GATE_FADE_S,
) -> tuple[ExcitationProgram, NullConfirmPlan]:
    """The acoustic null confirm's stimulus: ONE sweep, played on BOTH branches.

    Every role's segment regenerates the same parent sweep and differs only
    in its gate, so the branches are sample-identical wherever both are
    open; composes under :data:`PROGRAM_PHASE_MEASURE` so it rides existing
    per-driver admission. ``gain_db`` is ONE level for both branches,
    required with no default (caller must clamp to the most restrictive
    role cap). Returns the program beside the :class:`NullConfirmPlan` it
    was composed from.
    """
    role_bands = _validate_roles(roles)
    if sweep_s is None:
        if sweep_duration_limits_s is None:
            # Omitting both asks for the nominal 6 s, which a real tweeter clamp refuses.
            raise ValueError(
                "a null confirm needs its roles' sweep duration limits (or an "
                "explicit sweep_s); composing at the nominal length is what "
                "admission refuses on every real 2-way"
            )
        probe_band = null_confirm_band_hz(fc_hz)
        sweep_s = null_confirm_sweep_duration_s(
            probe_band[0], probe_band[1], role_bands, sweep_duration_limits_s,
        )
    plan = null_confirm_channel_plan(
        fc_hz, role_bands, sweep_s=sweep_s, fade_s=fade_s,
    )
    f1_hz, f2_hz = plan.band_hz
    fade_n = _seconds_to_samples(fade_s, PROGRAM_SAMPLE_RATE_HZ)

    segments: list[ProgramSegment] = []
    guard_n = _seconds_to_samples(guard_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("guard", 0, guard_n))
    cursor = guard_n

    sweep_at = cursor
    sweep_n = 0
    for rb in role_bands:
        start, end = plan.gates_by_role[rb.role]
        seg = _stimulus(
            segment_id=f"sweep_null_{rb.role}",
            kind=KIND_SUMMED_SWEEP,
            role=rb.role,
            channel=rb.channel,
            start=sweep_at,
            f1_hz=f1_hz,
            f2_hz=f2_hz,
            duration_s=sweep_s,
            gain_db=gain_db,  # ONE gain for every branch — see docstring.
            downstream_gain_db=downstream_gain_db,
        )
        if not (start == 0 and end is None):
            seg = replace(
                seg,
                gate_start_sample=start,
                gate_end_sample=end,
                gate_fade_samples=fade_n,
            )
        segments.append(seg)
        sweep_n = max(sweep_n, seg.n_samples)
    cursor = sweep_at + sweep_n

    tail_n = _seconds_to_samples(tail_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("tail", cursor, tail_n))
    cursor += tail_n

    channels = 1 + max(rb.channel for rb in role_bands)
    return _finalize(PROGRAM_PHASE_MEASURE, channels, segments, cursor), plan


def segment_stimulus(segment: ProgramSegment):
    """Regenerate the exact float32 mono stimulus for one stimulus segment.
    Raises for a silence segment or a sample-count mismatch (a corrupt schedule)."""
    import numpy as np

    if segment.kind not in STIMULUS_KINDS:
        raise ValueError("segment_stimulus is only defined for stimulus segments")
    assert segment.f1_hz is not None and segment.f2_hz is not None
    duration_approx = segment.n_samples / PROGRAM_SAMPLE_RATE_HZ
    sweep, meta = synchronized_swept_sine(
        f1=segment.f1_hz,
        f2=segment.f2_hz,
        duration_approx_s=duration_approx,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
        amplitude_dbfs=segment.gain_db,
    )
    if meta.n_samples != segment.n_samples:
        raise ValueError(
            f"segment {segment.segment_id!r} stimulus reconstruction produced "
            f"{meta.n_samples} samples, schedule says {segment.n_samples}"
        )
    out = np.asarray(sweep, dtype=np.float32)
    if not segment.is_gated:
        return out
    # The gate silences samples; the parent waveform survives sample-for-sample.
    out = out.copy()
    start = segment.gate_start_sample
    end = (
        segment.n_samples if segment.gate_end_sample is None
        else segment.gate_end_sample
    )
    out[:start] = 0.0
    out[end:] = 0.0
    fade = segment.gate_fade_samples
    if fade:
        # Raised cosine, applied only where the gate cuts, to preserve sample-identity.
        ramp = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade, endpoint=False)))
        if start > 0:
            out[start:start + fade] *= ramp.astype(np.float32)
        if segment.gate_end_sample is not None:
            out[end - fade:end] *= ramp[::-1].astype(np.float32)
    return out


def segment_sweep_frequency_at(segment: ProgramSegment, sample: int) -> float:
    """The parent sweep's instantaneous frequency at one sample offset:
    the exponential sweep law ``f(t) = f1 * (f2/f1)**(t/T)`` inverted onto
    sample indices."""
    if segment.f1_hz is None or segment.f2_hz is None:
        raise ValueError("only a stimulus segment sweeps")
    if segment.n_samples <= 0:
        raise ValueError("a segment spans no samples")
    frac = min(max(sample / segment.n_samples, 0.0), 1.0)
    return float(segment.f1_hz) * (
        float(segment.f2_hz) / float(segment.f1_hz)
    ) ** frac


def segment_emitted_band_hz(segment: ProgramSegment) -> tuple[float, float]:
    """The SAFETY band: every frequency a gated segment puts on its driver,
    INCLUDING both fade ramps (attenuated, not silent). What admission
    judges a driver's permitted band against — the wider of two bands vs.
    the fades-excluded :class:`NullConfirmPlan.overlap_hz`.
    """
    if not segment.is_gated:
        assert segment.f1_hz is not None and segment.f2_hz is not None
        return float(segment.f1_hz), float(segment.f2_hz)
    end = (
        segment.n_samples if segment.gate_end_sample is None
        else segment.gate_end_sample
    )
    return (
        segment_sweep_frequency_at(segment, segment.gate_start_sample),
        segment_sweep_frequency_at(segment, end),
    )


def render_program_pcm(program: ExcitationProgram):
    """Regenerate the interleaved float32 PCM for a program, shape (N, channels)."""
    import numpy as np

    pcm = np.zeros((program.total_samples, program.channels), dtype=np.float32)
    for seg in program.segments:
        if seg.kind == KIND_COURTESY_TONE:
            stim = courtesy_tone_stimulus(seg)
        elif seg.kind in STIMULUS_KINDS:
            stim = segment_stimulus(seg)
        else:
            continue
        assert seg.channel is not None
        pcm[seg.start_sample:seg.start_sample + seg.n_samples, seg.channel] = stim
    return pcm


def write_program_wav(path: str | Path, program: ExcitationProgram) -> None:
    """Write a program as an interleaved S16_LE WAV at the program channel
    count (matches the sweep cache and ``aplay`` playback path)."""
    import numpy as np
    from scipy.io import wavfile

    pcm = render_program_pcm(program)
    clipped = np.clip(pcm, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    wavfile.write(str(path), program.sample_rate_hz, int16)
