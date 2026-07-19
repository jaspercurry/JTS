# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Excitation-program model + composers for the crossover conductor flow.

The v2 crossover measurement flow (docs/crossover-measurement-productization-design.md
§5.3) replaces a distributed transaction of per-sweep taps with a single
**excitation program**: a pure-data schedule of stimuli the Pi compiles once,
plays as one continuous stream, and analyzes as ``(program, capture) →
analysis`` (see :mod:`jasper.audio_measurement.program_analysis`). This module
owns the *program* half — the schedule dataclasses, the three phase composers,
and deterministic PCM rendering / WAV writing.

Design boundaries this module deliberately keeps:

* **Pure data + pure composers, no I/O beyond WAV write.** An
  :class:`ExcitationProgram` stores no PCM — :func:`render_program_pcm`
  regenerates the exact samples from the schedule (mirroring
  :mod:`jasper.audio_measurement.sweep`'s "regenerate deterministically per
  tuple" philosophy), so the schedule is small, hashable, and JSON round-trips.
* **Safety admission is Wave 2's job.** Composers take the per-segment digital
  gains as INPUT (pilot levels for CHECK, a solved ``gain_plan`` for MEASURE);
  this module does NOT import any ``jasper.active_speaker`` safety module and
  does NOT decide whether a level is admissible. ``effective_peak_dbfs`` is
  recorded (``gain_db + downstream_gain_db``) as the admission INPUT the
  playback layer re-admits from a fresh readback, exactly as today.
* **Dependency-clean under jasper.audio_measurement.** Only the kernel's own
  :mod:`~jasper.audio_measurement.sweep` /
  :mod:`~jasper.audio_measurement.excitation` /
  :mod:`~jasper.audio_measurement.excitation_admission` (for the pure-data
  :class:`~jasper.audio_measurement.excitation_admission.FrequencyBand`) are
  imported, plus numpy for PCM rendering.

Channel routing (design §5.4): CHECK/MEASURE programs are 2-channel WAVs
(ch0 → woofer output path, ch1 → tweeter output path); VERIFY is a mono summed
sweep through the applied production graph. Per-driver sequencing lives in the
WAV channels so the CamillaDSP commissioning graph stays static and provable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jasper.audio_measurement.excitation import (
    AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS,
)
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.sweep import (
    SweepMeta,
    synchronized_sweep_metadata,
    synchronized_swept_sine,
)

logger = logging.getLogger(__name__)

PROGRAM_SCHEMA_VERSION = 1
PROGRAM_KIND = "jts_excitation_program"

# Fixed program sample rate — matches CamillaDSP / the sweep kernel.
PROGRAM_SAMPLE_RATE_HZ = 48_000

# Phase vocabulary. One composer + one analysis entry point per phase.
PHASE_CHECK = "check"
PHASE_MEASURE = "measure"
PHASE_VERIFY = "verify"
PHASES = frozenset({PHASE_CHECK, PHASE_MEASURE, PHASE_VERIFY})

# Segment kinds.
KIND_SILENCE = "silence"
KIND_PILOT = "pilot"
KIND_SWEEP = "sweep"
KIND_SUMMED_SWEEP = "summed_sweep"
STIMULUS_KINDS = frozenset({KIND_PILOT, KIND_SWEEP, KIND_SUMMED_SWEEP})

# Measurement sweeps live in [150 Hz, 20 kHz]: long LF reach is not needed at a
# ~250 Hz gated validity floor, and bass belongs to the room / bass-extension
# passes (design §5.2). Each driver's swept band is its declared band
# intersected with this window.
MEASURE_SWEEP_F_LO_HZ = 150.0
MEASURE_SWEEP_F_HI_HZ = 20_000.0

# The unit-peak reference level the per-segment digital gain is applied ON TOP
# of. A pilot at relative level r has digital peak BASE + r dBFS. Shared with
# the ESS peak so a quiet/loud handoff can't creep in (see
# jasper.audio_measurement.excitation).
BASE_STIMULUS_PEAK_DBFS = AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS

# Finite floor recorded as a silence segment's peak (JSON is nan/inf-free).
SILENCE_PEAK_DBFS = -120.0

# --- CHECK phase defaults ---
DEFAULT_CHECK_AMBIENT_S = 12.0
DEFAULT_PILOT_DURATION_S = 0.8
DEFAULT_PILOT_GAP_S = 0.5
# Two known relative levels, 10 dB apart, for the behavioral linearity check
# (design §3.4): the captured level delta must match the programmed delta.
DEFAULT_PILOT_LEVELS_DB = (-10.0, 0.0)

# --- MEASURE phase defaults ---
DEFAULT_MEASURE_GUARD_S = 2.0
DEFAULT_WOOFER_SWEEP_S = 4.0
DEFAULT_TWEETER_SWEEP_S = 3.0
DEFAULT_MEASURE_TAIL_S = 0.5

# --- MESM inter-sweep gap rule (design §5.3) ---
# The gap between consecutive sweeps must clear (a) the preceding sweep's
# expected IR / reverb tail so it decays before the next response begins, and
# (b) the preceding synchronized sweep's harmonic pre-ring — the order-N
# harmonic image leads the linear IR by L·ln(N) (see
# jasper.audio_measurement.deconv.harmonic_time_advance_s), so up to
# MESM_MAX_HARMONIC_ORDER of that lead must be cleared too. A conservative
# ~1 s floor guards against under-sizing when both terms are small.
DEFAULT_IR_TAIL_S = 0.5
MESM_MAX_HARMONIC_ORDER = 3
MESM_GAP_FLOOR_S = 1.0

# --- VERIFY phase defaults ---
DEFAULT_VERIFY_GUARD_S = 1.5
DEFAULT_VERIFY_SWEEP_S = 6.0
DEFAULT_VERIFY_TAIL_S = 0.5
VERIFY_F_LO_HZ = 150.0
VERIFY_F_HI_HZ = 20_000.0

# The leading VERIFY pilot pair's OWN band (W6.7 ruling 2) — deliberately NOT
# the summed sweep's full band. The sweep spans the crossover overlap on
# purpose (it needs to see the interference notch there), but a pilot chirp
# swept through that same notch goes noise-dominated across the notched
# portion, and the ±0.5 dB behavioral-linearity ratio (`LINEARITY_TOLERANCE_DB`
# in program_analysis.py) misfires on that noise rather than on actual AGC/gain
# behavior — the W6 run-7 hardware bug this fixes. PROVISIONAL: 200-800 Hz is a
# flat region of the applied summed response for a typical 2-way crossover
# (Fc comfortably above ~1 kHz, e.g. the 2000 Hz reference rig); an unusually
# low Fc could bring the crossover notch back into this band, which this
# constant does not defend against.
VERIFY_PILOT_F_LO_HZ = 200.0
VERIFY_PILOT_F_HI_HZ = 800.0


@dataclass(frozen=True)
class RoleBand:
    """One driver's routing + declared band, the composer's per-driver input.

    ``channel`` is the program-WAV channel carrying this driver's stimulus
    (ch0 → woofer output path, ch1 → tweeter output path, per design §5.4).
    ``band`` is the driver's declared band; composers intersect it with the
    phase's swept window before generating a stimulus.
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

    ``start_sample`` / ``n_samples`` place the segment exactly inside the
    program WAV; a silence segment carries ``role``/``channel``/``f*_hz`` as
    ``None``. ``gain_db`` is the digital gain applied to the unit-peak
    stimulus; ``effective_peak_dbfs`` is ``gain_db + downstream_gain_db`` — the
    admission INPUT (session volume + graph gain fold in downstream, in Wave 2).
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

    def __post_init__(self) -> None:
        if self.kind not in (STIMULUS_KINDS | {KIND_SILENCE}):
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

    def to_dict(self) -> dict[str, Any]:
        return {
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProgramSegment":
        required = {
            "segment_id", "kind", "role", "channel", "start_sample",
            "n_samples", "f1_hz", "f2_hz", "gain_db", "effective_peak_dbfs",
        }
        if not isinstance(value, Mapping) or set(value) != required:
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
        )


@dataclass(frozen=True)
class ExcitationProgram:
    """A pure-data schedule of stimuli the conductor plays as one stream.

    ``program_id`` is a content hash over the schedule (phase, rate, channels,
    every segment, total length) — it fingerprints the analysis and the derived
    candidate, so a re-run with a different program can never be mistaken for a
    resume of the old one.
    """

    program_id: str
    phase: str
    sample_rate_hz: int
    channels: int
    segments: tuple[ProgramSegment, ...]
    total_samples: int

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
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

    ``gain_db`` becomes the sweep's ``amplitude_dbfs`` — a unit-peak sine scaled
    by ``10**(gain_db/20)`` has peak ``gain_db`` dBFS, so the digital gain IS
    the sweep amplitude. Must be non-positive (:func:`synchronized_sweep_metadata`
    enforces this).
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

    The v2 MEASURE/VERIFY programs open with this pair (design §5.2) so each
    capture carries its OWN behavioral-linearity evidence — CHECK-only
    verification cannot protect the later captures (browser AGC can silently
    return with a re-acquired stream). Same segment-id shape as CHECK's pilots
    (``pilot_{role}_lo`` / ``pilot_{role}_hi``) so
    :func:`jasper.audio_measurement.program_analysis` reuses one pilot reader
    across all three phases. ``gains_db`` is ``(lo, hi)`` ABSOLUTE digital
    gains (dBFS, non-positive); the caller supplies them (for MEASURE the CHECK
    gain solve's woofer gain and −10 dB below it) so the pilot rides the same
    admissible level as the measurement sweeps.
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

    The gap must clear the preceding sweep's expected IR / reverb tail
    (``ir_tail_s``) AND its harmonic pre-ring: for a synchronized ESS, the
    order-N harmonic image leads the linear IR by ``L·ln(N)`` seconds, so the
    largest considered order (``max_harmonic_order``) contributes ``L·ln(N)``.
    A conservative ``floor_s`` (~1 s) guards against under-sizing when both
    terms are small. Returned in samples::

        gap_s = max(floor_s, ir_tail_s + L·ln(max_harmonic_order))
    """
    if max_harmonic_order < 2:
        raise ValueError("max_harmonic_order must be at least 2")
    if not (ir_tail_s >= 0) or not math.isfinite(ir_tail_s):
        raise ValueError("ir_tail_s must be finite and non-negative")
    pre_ring_s = float(preceding.L) * math.log(max_harmonic_order)
    gap_s = max(floor_s, ir_tail_s + pre_ring_s)
    return _seconds_to_samples(gap_s, sample_rate)


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
) -> ExcitationProgram:
    """Compose the CHECK program (design §5.2): ambient silence + per-driver pilots.

    Leading silence is the session ambient measurement (reused by the ambient
    band-floor report). Then, per driver, two short band-limited pilot ESS
    chirps at ``pilot_levels_db`` (relative to ``base_peak_dbfs``, ≥0.5 s apart)
    — their captured level ratio drives the behavioral AGC/linearity verdict,
    and their band-concentrated energy drives channel-map sanity. ``pilot_levels_db``
    are RELATIVE offsets: pilot digital gain = ``base_peak_dbfs + level``.

    ``role_base_peak_dbfs`` (v2 conductor, Wave 6.1 — cap-aware composition)
    OPT-IN overrides ``base_peak_dbfs`` PER ROLE so a driver whose safety cap
    binds below the shared reference (e.g. a compression tweeter) rides a lower
    per-driver base. Because both pilots keep the same ``pilot_levels_db``
    offsets against the SAME per-role base, the pair's 10 dB relative delta is
    preserved regardless of how far the base is clamped; only the absolute
    level degrades, honestly recorded in the segments' gains. ``None`` (the
    default) is byte-identical to the pre-v2 composer.
    """
    roles = _validate_roles(roles_bands)
    if len(pilot_levels_db) != 2:
        raise ValueError("pilot_levels_db must be exactly two levels")
    channels = 1 + max(rb.channel for rb in roles)

    segments: list[ProgramSegment] = []
    cursor = 0
    ambient_n = _seconds_to_samples(ambient_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("ambient", cursor, ambient_n))
    cursor += ambient_n

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

    return _finalize(PHASE_CHECK, channels, segments, cursor)


def build_measure_program(
    gain_plan: Mapping[str, float],
    roles_bands: Sequence[RoleBand],
    *,
    sweep_durations: Mapping[str, float] | None = None,
    guard_s: float = DEFAULT_MEASURE_GUARD_S,
    tail_s: float = DEFAULT_MEASURE_TAIL_S,
    ir_tail_s: float = DEFAULT_IR_TAIL_S,
    downstream_gain_db: float = 0.0,
    leading_pilot_gains_db: tuple[float, float] | None = None,
    leading_pilot_role: str | None = None,
    pilot_duration_s: float = DEFAULT_PILOT_DURATION_S,
    pilot_gap_s: float = DEFAULT_PILOT_GAP_S,
) -> ExcitationProgram:
    """Compose the MEASURE program (design §5.2/§5.4): woofer, tweeter, woofer-repeat.

    Exactly two drivers (2-way): ``roles_bands[0]`` is the lower driver (woofer,
    ch0), ``roles_bands[1]`` is the upper (tweeter, ch1). Layout::

        [pilot lo → gap → pilot hi → gap →]  (v2, when leading pilots requested)
        guard silence → woofer sweep → MESM gap → tweeter sweep
                      → MESM gap → woofer sweep REPEAT → tail silence

    The repeat is a bit-identical stimulus to the first woofer sweep (same gain,
    band, duration ⇒ same PCM); the two form the in-capture drift estimator and
    the dropped-buffer/glitch detector (design §3.1). ``gain_plan`` maps role →
    digital gain (dBFS, non-positive); ``sweep_durations`` maps role → sweep
    duration (defaults: ~4 s woofer / ~3 s tweeter). Gaps come from
    :func:`mesm_gap_samples` sized to the PRECEDING sweep.

    ``leading_pilot_gains_db`` (v2 conductor, Wave 5a — design §5.2) OPT-IN
    prepends a two-level ``(lo, hi)`` pilot pair on ``leading_pilot_role``'s
    channel (default the lower/woofer driver) so this capture carries its own
    behavioral-linearity evidence. ``None`` (the default) is byte-identical to
    the pre-v2 composer — the legacy analysis fixtures and any caller that does
    not opt in see the exact original segment layout.
    """
    roles = _validate_roles(roles_bands)
    if len(roles) != 2:
        raise ValueError("MEASURE is a 2-way flow: exactly two drivers required")
    woofer, tweeter = roles[0], roles[1]
    for rb in roles:
        if rb.role not in gain_plan:
            raise ValueError(f"gain_plan is missing role {rb.role!r}")
    durations = {
        woofer.role: DEFAULT_WOOFER_SWEEP_S,
        tweeter.role: DEFAULT_TWEETER_SWEEP_S,
    }
    if sweep_durations:
        durations.update(sweep_durations)
    channels = 1 + max(rb.channel for rb in roles)

    def _band(rb: RoleBand) -> tuple[float, float]:
        return _intersect_band(rb.band, MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ)

    w_f1, w_f2 = _band(woofer)
    t_f1, t_f2 = _band(tweeter)
    w_meta = _sweep_meta(w_f1, w_f2, durations[woofer.role], gain_plan[woofer.role])
    t_meta = _sweep_meta(t_f1, t_f2, durations[tweeter.role], gain_plan[tweeter.role])

    segments: list[ProgramSegment] = []
    cursor = 0
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

    sweep_w = _sweep("sweep_w", woofer, w_f1, w_f2, durations[woofer.role])
    segments.append(sweep_w)
    cursor += sweep_w.n_samples
    gap_w = mesm_gap_samples(w_meta, ir_tail_s=ir_tail_s)
    segments.append(_silence("gap_w_t", cursor, gap_w))
    cursor += gap_w

    sweep_t = _sweep("sweep_t", tweeter, t_f1, t_f2, durations[tweeter.role])
    segments.append(sweep_t)
    cursor += sweep_t.n_samples
    gap_t = mesm_gap_samples(t_meta, ir_tail_s=ir_tail_s)
    segments.append(_silence("gap_t_w", cursor, gap_t))
    cursor += gap_t

    # The repeat is bit-identical to sweep_w (same band/duration/gain ⇒ same PCM).
    sweep_w_rep = _sweep("sweep_w_rep", woofer, w_f1, w_f2, durations[woofer.role])
    segments.append(sweep_w_rep)
    cursor += sweep_w_rep.n_samples

    tail_n = _seconds_to_samples(tail_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("tail", cursor, tail_n))
    cursor += tail_n

    return _finalize(PHASE_MEASURE, channels, segments, cursor)


VERIFY_PILOT_ROLE = "summed"


def build_verify_program(
    fc_hz: float,
    *,
    gain_db: float = BASE_STIMULUS_PEAK_DBFS,
    guard_s: float = DEFAULT_VERIFY_GUARD_S,
    sweep_s: float = DEFAULT_VERIFY_SWEEP_S,
    tail_s: float = DEFAULT_VERIFY_TAIL_S,
    downstream_gain_db: float = 0.0,
    leading_pilot_gains_db: tuple[float, float] | None = None,
    pilot_duration_s: float = DEFAULT_PILOT_DURATION_S,
    pilot_gap_s: float = DEFAULT_PILOT_GAP_S,
) -> ExcitationProgram:
    """Compose the VERIFY program (design §5.2): a mono full-band summed sweep.

    One channel: ``[pilot lo → gap → pilot hi → gap →]`` (v2, when leading
    pilots requested) guard silence + one full-band summed ESS (~6 s) + tail,
    played through the APPLIED production graph (the real system, not a
    commissioning construct). ``fc_hz`` widens the low bound when the crossover
    is low so the lower shoulder ``fc/2`` is always excited:
    ``f1 = min(VERIFY_F_LO_HZ, fc/2)``.

    ``leading_pilot_gains_db`` (v2 conductor, Wave 5a — design §5.2) OPT-IN
    prepends a two-level ``(lo, hi)`` mono pilot pair (role ``"summed"``) so
    VERIFY also carries its own behavioral-linearity evidence. The pilot rides
    its OWN band, ``[VERIFY_PILOT_F_LO_HZ, VERIFY_PILOT_F_HI_HZ]`` (W6.7 ruling
    2) — a flat mid-woofer region of the applied summed response — rather than
    the summed sweep's full band: the sweep deliberately crosses the crossover
    overlap (it needs to see the interference notch there), and a pilot swept
    through that same notch goes noise-dominated across the notched portion,
    misfiring the linearity ratio check on noise rather than on AGC/gain
    behavior. ``None`` is byte-identical to the pre-v2 composer.
    """
    if not (fc_hz > 0) or not math.isfinite(fc_hz):
        raise ValueError("fc_hz must be finite and positive")
    f1_hz = min(VERIFY_F_LO_HZ, fc_hz / 2.0)
    f2_hz = VERIFY_F_HI_HZ
    if not f1_hz < f2_hz:
        raise ValueError("verify sweep band collapsed")

    segments: list[ProgramSegment] = []
    cursor = 0
    if leading_pilot_gains_db is not None:
        if len(leading_pilot_gains_db) != 2:
            raise ValueError("leading_pilot_gains_db must be exactly two levels")
        cursor = _append_leading_pilot_pair(
            segments, cursor,
            role=VERIFY_PILOT_ROLE,
            channel=0,
            f1_hz=VERIFY_PILOT_F_LO_HZ,
            f2_hz=VERIFY_PILOT_F_HI_HZ,
            gains_db=leading_pilot_gains_db,
            pilot_duration_s=pilot_duration_s,
            pilot_gap_s=pilot_gap_s,
            downstream_gain_db=downstream_gain_db,
        )
    guard_n = _seconds_to_samples(guard_s, PROGRAM_SAMPLE_RATE_HZ)
    segments.append(_silence("guard", cursor, guard_n))
    cursor += guard_n

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

    return _finalize(PHASE_VERIFY, 1, segments, cursor)


def segment_stimulus(segment: ProgramSegment):
    """Regenerate the exact float32 mono stimulus for one stimulus segment.

    Deterministic from ``(f1_hz, f2_hz, n_samples, gain_db)`` — the sweep is
    regenerated with ``amplitude_dbfs = gain_db`` and the duration reconstructed
    from ``n_samples`` (the synchronized-sweep metadata round-trips). Raises for
    a silence segment (no stimulus) or if the reconstruction fails to reproduce
    the recorded sample count (a corrupt schedule).
    """
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
    return np.asarray(sweep, dtype=np.float32)


def render_program_pcm(program: ExcitationProgram):
    """Regenerate the interleaved float32 PCM for a program, shape (N, channels).

    Deterministic: each stimulus segment is regenerated via
    :func:`segment_stimulus` and placed on its channel at its scheduled offset;
    silence segments contribute nothing. No PCM is stored on the program — this
    is the single renderer both the WAV writer and the analysis fixtures use.
    """
    import numpy as np

    pcm = np.zeros((program.total_samples, program.channels), dtype=np.float32)
    for seg in program.segments:
        if seg.kind not in STIMULUS_KINDS:
            continue
        stim = segment_stimulus(seg)
        assert seg.channel is not None
        pcm[seg.start_sample:seg.start_sample + seg.n_samples, seg.channel] = stim
    return pcm


def write_program_wav(path: str | Path, program: ExcitationProgram) -> None:
    """Write a program as an interleaved S16_LE WAV at the program channel count.

    16-bit PCM matches the sweep cache and the ``aplay`` playback path (see
    :func:`jasper.audio_measurement.sweep.write_sweep_wav`); the sweep spans far
    less than 16-bit's dynamic range.
    """
    import numpy as np
    from scipy.io import wavfile

    pcm = render_program_pcm(program)
    clipped = np.clip(pcm, -1.0, 1.0)
    int16 = (clipped * 32767.0).astype(np.int16)
    wavfile.write(str(path), program.sample_rate_hz, int16)
