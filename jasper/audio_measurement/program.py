# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Excitation-program model + composers for the crossover session flow.

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

**Courtesy-tone prelude (issue #1677).** Each composer takes an opt-in
``courtesy_prelude`` flag that splices a short "beep beep beep" + ~3 s of
silence in AHEAD OF EVERY SOUND THE BEEPS ANNOUNCE -- a pre-capture
"quiet please" warning played from the speaker under test itself, once per
capture group (CHECK/MEASURE/VERIFY each get their own).

*Placement is the message* (flow-simplification plan §2.5, amended
2026-07-28 by issues #1810 / #1812). Two rules, in priority order:

1. **Nothing audible precedes the first beep.** PR #1771 read §2.5's "move
   every lead-in ahead of the beeps" as covering the leading pilot pair,
   and left MEASURE/VERIFY opening on two FULL-GAIN pilot chirps at t=0,
   with the (6 dB quieter) beeps at t≈4 s behind them. On hardware that is
   "chirp, chirp, then beep beep beep" -- the warning arriving after the
   sound it warns about, which the owner heard on 2026-07-28. A pilot is
   stimulus, not lead-in: it is as loud as the sweep and it is the whole
   input to the behavioural-linearity verdict. The beeps go first.
2. **The beeps are followed by the settle and nothing else** -- §2.5's own
   requirement, preserved. The single thing allowed to sit between the
   settle and the first stimulus is the pre-pilot ambient window
   (:data:`PILOT_AMBIENT_WINDOW_S`), which is silence by construction and
   which MUST be measured with the room already quiet, i.e. after the
   warning rather than before it. The bound on the interval is therefore
   :data:`COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S` (settle + that window, and
   nothing else); :func:`courtesy_beep_to_stimulus_gap_s` derives the
   actual interval from any composed program, and composition tests pin
   both rules.

CHECK is the one composer whose prelude does not sit at sample 0, and it
satisfies both rules anyway: its 12 s ``ambient`` window is the SESSION's
room-noise measurement (the ambient band-floor report and the gain solve
both read it), deliberately taken before the household is asked to go
quiet, and it is silence -- so nothing audible precedes its beeps, which
land directly in front of the pilots that ARE its measurement.

The prelude rides the SAME admitted
playback as the stimulus that follows it -- never a second, unguarded
playback path (see AGENTS.md's ``/sound/`` Combined-test-wedge cautionary
tale) -- because the prelude is just more segments on the one
``ExcitationProgram`` the session already composes, admits, and plays. Its
kind (``KIND_COURTESY_TONE``) is deliberately NOT in ``STIMULUS_KINDS``: the
locate/analysis machinery in ``program_analysis.py`` correlates against and
deconvolves only ``STIMULUS_KINDS`` segments, so the prelude is as
analysis-invisible as a silence segment, and the schedule shift it
introduces is absorbed by the existing relative-offset locate math (the same
mechanism that already tolerated sweep-composition PR-A lengthening
MEASURE). It IS real, audible content though, so it belongs to the broader
``KNOWN_AUDIBLE_KINDS`` set program-admission's out-of-segment-energy check
must expect rather than flag as a leak. See ``_insert_courtesy_prelude``
for the segment shape and ``courtesy_tone_gain_db`` for the level derivation
(never louder than the channel's own loudest scheduled stimulus, never
positive).
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

# Phase vocabulary. One composer + one analysis entry point per phase.
#
# **The ``PROGRAM_`` prefix is what keeps this vocabulary apart from the
# session's** (master plan ticket 2.9). A second, larger ``PHASE_*`` family
# lives in :mod:`jasper.active_speaker.crossover_v2.journey` and answers a
# different question: *where is the round in its walk* (eleven phases —
# ``PHASE_LATERAL``, ``PHASE_REVIEW``, ``PHASE_DONE``, …). This one answers
# *which composer built this stimulus*, and has exactly the three below.
# Until the prefix landed both families spelled all three names identically,
# so an import site could take the wrong one and still typecheck, run, and
# agree — see ``journey``'s own "Do not conflate the two vocabularies" note
# above ``PHASE_CLOUD_MEASURE``, which is the same warning from the other side.
#
# **The string VALUES stay identical on purpose, and neither family may change
# them.** They are not free to diverge, because both sets are banked:
#
# * here, ``phase`` is hashed into ``program_id`` (:func:`_program_id`) and
#   serialized by :meth:`ExcitationProgram.to_dict`, and ``from_dict`` /
#   ``__post_init__`` refuse a phase outside ``PROGRAM_PHASES`` — so a new
#   value would both re-fingerprint every program and stop every banked
#   program JSON from loading;
# * on the journey side the phase is written into retained position records
#   (``crossover_v2.spatial``) and the persisted ``session_phases``.
#
# So the collision was resolved in the NAMES, which nothing has banked, and
# ``tests/test_audio_measurement_program.py`` pins the two name sets disjoint
# so it cannot come back.
PROGRAM_PHASE_CHECK = "check"
PROGRAM_PHASE_MEASURE = "measure"
PROGRAM_PHASE_VERIFY = "verify"
PROGRAM_PHASES = frozenset(
    {PROGRAM_PHASE_CHECK, PROGRAM_PHASE_MEASURE, PROGRAM_PHASE_VERIFY}
)

# Segment kinds.
KIND_SILENCE = "silence"
KIND_PILOT = "pilot"
KIND_SWEEP = "sweep"
KIND_SUMMED_SWEEP = "summed_sweep"
STIMULUS_KINDS = frozenset({KIND_PILOT, KIND_SWEEP, KIND_SUMMED_SWEEP})
# The courtesy-tone prelude (issue #1677) — see the module docstring. Never a
# STIMULUS_KIND: it must stay invisible to the locate/correlation and
# deconvolution machinery in program_analysis.py, exactly like KIND_SILENCE.
KIND_COURTESY_TONE = "courtesy_tone"
# STIMULUS_KINDS plus the courtesy tone: the segments program_admission.py's
# out-of-segment-energy / declared-peak checks must treat as expected
# non-silent content. Anything outside this set (i.e. KIND_SILENCE) must
# render as true silence — that promise is what OUT_OF_SEGMENT_ENERGY
# polices, so a new audible-but-unanalyzed kind has to join this set or every
# program that uses it would be refused as if it leaked/tampered energy.
KNOWN_AUDIBLE_KINDS = STIMULUS_KINDS | frozenset({KIND_COURTESY_TONE})

# The one segment id a program's room-listening (noise-floor) window carries,
# on every phase that has one. Named once here rather than spelled as a
# literal in the composers and again in ``program_analysis``, which looks the
# window up by id to feed the pilot SNR guard — a rename in one place without
# the other would silently disable that guard (issue #1810's failure mode).
AMBIENT_SEGMENT_ID = "ambient"

# Measurement sweeps live in [150 Hz, 23 kHz]: long LF reach is not needed at a
# ~250 Hz gated validity floor, and bass belongs to the room / bass-extension
# passes (design §5.2). Each driver's swept band is its declared band
# intersected with this window. The upper edge is kept in lockstep with
# jasper.active_speaker.test_signal_plan.MAX_DRIVER_TEST_FREQUENCY_HZ
# (sweep-composition PR-A, #1668) — both name the same "no driver test signal
# goes above this" global ceiling, one for swept sweeps, one for single-tone
# commissioning plans; a test pins the two constants equal so they can't
# silently drift apart.
MEASURE_SWEEP_F_LO_HZ = 150.0
MEASURE_SWEEP_F_HI_HZ = 23_000.0

# Total sweep occurrences PER DRIVER in the interleaved MEASURE program
# (sweep-composition PR-A, #1668): the first is the primary, the remaining
# N-1 are bit-identical repeats used for the in-capture drift/glitch
# estimator (design §3.1) — now for BOTH drivers, not just the woofer.
# At the production defaults this composes a ~38.4 s MEASURE program; the
# capture-side mono 16-bit WAV (program + the 2 s relay entry margin) is
# ~3.70 MiB against the 5 MiB
# jasper.active_speaker.test_signal_plan.CROSSOVER_CAPTURE_MAX_WAV_BYTES
# upload cap, i.e. ~1.30 MiB of headroom — spent down from ~2.9 MB pre-#1668
# by the N=3 repeats, the #1677 courtesy prelude (~0.33 MB) and the #1810
# pre-pilot ambient window (~0.09 MiB). Raising repeat_count, any role's
# sweep_durations, or PILOT_AMBIENT_WINDOW_S must re-check that cap;
# ``test_worst_case_measure_with_prelude_stays_under_capture_wav_cap`` is
# where it fails if you don't.
MEASURE_REPEAT_COUNT = 3

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

# --- pre-pilot ambient window (issue #1810) ---
# A short silence immediately BEFORE a MEASURE/VERIFY leading pilot pair, so
# those pilots' own in-band noise floor is measurable on every phase instead
# of on CHECK alone. Until 2026-07-28 those programs opened directly on the
# pilots and had no such window: `program_analysis._pilot_observations` then
# had no ambient evidence, its in-band SNR was `+inf` BY DEFINITION, and the
# `PILOT_MIN_SNR_DB` guard on the behavioural-linearity verdict was
# unconditionally satisfied — so a pilot pair drowned in room noise (issue
# #1810: a freshly-applied correction dropped the pilot band 14-18 dB and
# left the quiet pilot ~5 dB over the room floor, compressing the captured
# two-pilot delta from 10 dB to 6 dB) failed the linearity ratio and was
# reported to the household as the PHONE's microphone misbehaving.
#
# 1 s: long enough for a stable in-band RMS estimate at the lowest pilot band
# edge (VERIFY_PILOT_F_LO_HZ = 200 Hz → ~200 cycles; a MEASURE pilot rides
# the driver band from MEASURE_SWEEP_F_LO_HZ = 150 Hz → ~150), short enough
# that the 15 windowed captures of a Full-tier cloud session cost ~15 s of
# session wall clock and ~94 KiB of the 5 MiB per-capture upload budget.
# Placed AFTER the courtesy settle (see the module docstring) so it samples
# the room the household has already been asked to quiet — the same room the
# pilots immediately play into.
#
# NAMED RESIDUAL — transient sensitivity (2026-07-28 review). The estimator
# reading this window (`program_analysis._band_power`) is a plain
# Hann-windowed in-band mean-square over the whole second, whereas CHECK's
# 12 s window is read through `snr_policy.framed_ambient_band_report`, a
# 95th-percentile-over-1-s-frames statistic. A short loud transient therefore
# weighs more here: 0.1 s at +20 dB over the floor inflates a 1 s mean-square
# by ~10.4 dB, where the same event spread over 12 s of mean-square is
# ~2.6 dB. Switching this consumer to the framed estimator is NOT the fix and
# would be a literal no-op: that function's frame length IS one second
# (`frame_len = sample_rate`), so at a 1 s window it yields exactly one frame
# and the percentile degenerates to that frame's own mean-square — and it
# reports the fixed CROSSOVER_SNR_BANDS_HZ set rather than the pilot's own
# declared band, which is what this guard needs. Getting a duration-
# independent statistic means a window of ≥2 frames, i.e. ≥2 s per capture.
# Accepted at 1 s because the residual's DIRECTION is safe and the margin
# covers it: an inflated ambient under-estimates SNR, so the failure mode is
# an honest, retryable `pilot_level_collapse` on a capture that was actually
# fine — never the mic accusation (`linearity_ok` is None, i.e. unknown,
# under the floor since #1838; it was forced True before), and never a pass
# for a genuinely collapsed pair. Margin: the
# 2026-07-20 jts3 hardware captures measured ~26-30 dB of quiet-pilot in-band
# SNR against a ~12.4 dB `PILOT_MIN_SNR_DB` floor, so ~14-18 dB of headroom
# absorbs a worst-case ~10 dB transient inflation — comfortably, but not by
# so much that the residual is worth leaving unnamed. Revisit (longer window,
# or a framed statistic over ≥2 frames) if bench data shows healthy captures
# landing inside ~10 dB of the floor.
PILOT_AMBIENT_WINDOW_S = 1.0

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
# flat mid-woofer region of the applied summed response for a typical 2-way
# crossover (e.g. the 2000 Hz reference rig). The hi bound is additionally
# clamped to fc/VERIFY_PILOT_FC_CLEARANCE_RATIO at compose time so a low-Fc
# preset can't bring the crossover overlap ([Fc/2, 2·Fc]) back into the pilot
# band: 2.5 keeps the pilot's top edge below the Fc/2 shoulder with margin
# (fc/2.5 < fc/2). When even that collapses the band (very low Fc), the
# composer falls back to [fc/8, fc/4] — still comfortably below the crossover
# region.
VERIFY_PILOT_F_LO_HZ = 200.0
VERIFY_PILOT_F_HI_HZ = 800.0
VERIFY_PILOT_FC_CLEARANCE_RATIO = 2.5

# --- courtesy-tone prelude (issue #1677) ---
# Three quick beeps ("beep, beep, beep") + a trailing silence, prepended
# ahead of a program's existing content when the caller opts in via
# ``courtesy_prelude=True``. Fixed, not configurable per call — this is the
# tone's SHAPE, analogous to DEFAULT_PILOT_LEVELS_DB's "10 dB apart" being a
# fixed property of the pilot pair rather than a per-call parameter.
COURTESY_TONE_BEEP_COUNT = 3
COURTESY_TONE_BEEP_HZ = 1000.0
COURTESY_TONE_BEEP_DURATION_S = 0.12
COURTESY_TONE_BEEP_GAP_S = 0.12
# The gap AFTER the last beep, before the program's existing content resumes
# — the "~3 seconds to go quiet" window the issue asks for.
COURTESY_TONE_TRAILING_SILENCE_S = 3.0
# How far below the reference stimulus gain the tone rides (see
# courtesy_tone_gain_db). 2026-07-23 owner spec: "derive the beep gain from
# the session's existing plan (e.g. the pilot gain − 6 dB)".
COURTESY_TONE_MARGIN_DB = 6.0
# §2.5's acceptance criterion as ONE number: the longest interval any composed
# program may leave between its last courtesy beep and its first audible
# measurement content. The settle the owner specified, plus (at most) the
# pre-pilot ambient window that has to be measured inside the quiet the beeps
# just asked for — and nothing else. A composer that reinstates a lead-in
# between the beeps and the stimulus fails the test that reads this.
COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S = (
    COURTESY_TONE_TRAILING_SILENCE_S + PILOT_AMBIENT_WINDOW_S
)


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
    """A pure-data schedule of stimuli the session plays as one stream.

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
        """Stimulus segments PLUS the courtesy-tone prelude (issue #1677) —
        every segment program-admission's out-of-segment-energy check must
        expect to be non-silent. See ``KNOWN_AUDIBLE_KINDS``."""
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


def _append_pilot_ambient_window(
    segments: list[ProgramSegment], cursor: int,
) -> int:
    """Append the pre-pilot ambient window (issue #1810); return the cursor.

    A plain silence segment named :data:`AMBIENT_SEGMENT_ID`, so
    ``program_analysis`` finds it by the SAME id it already reads on CHECK
    and the pilot SNR guard becomes live on every phase that has pilots.
    Emitted only alongside a leading pilot pair — it exists to give those
    pilots a noise floor, so a program with no pilots stays byte-identical
    to the pre-#1810 shape.

    See :data:`PILOT_AMBIENT_WINDOW_S` for the duration rationale and the
    module docstring for why it sits after the courtesy settle rather than
    before the beeps.
    """
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


# --------------------------------------------------------------------------- #
# courtesy-tone prelude (issue #1677)
# --------------------------------------------------------------------------- #


def courtesy_tone_gain_db(
    reference_gain_db: float, *, margin_db: float = COURTESY_TONE_MARGIN_DB,
) -> float:
    """The courtesy tone's digital gain, derived from ``reference_gain_db``
    (a program channel's own loudest scheduled stimulus gain).

    ``margin_db`` (default :data:`COURTESY_TONE_MARGIN_DB`, i.e. -6 dB) below
    the reference, clamped so the tone can never equal or exceed it (the
    ``min(..., reference_gain_db)`` term is defense in depth against a future
    zero/negative margin) and never positive. Both clamps mirror the issue's
    own wording: "clamp ≤ the stimulus gain, never positive." In practice
    every real stimulus gain is already ≤ 0 dBFS (``synchronized_sweep_metadata``
    enforces this at compose time for the segments the reference is drawn
    from), so neither clamp binds today — they exist as an explicit backstop,
    not because either is expected to fire.
    """
    return min(reference_gain_db - margin_db, reference_gain_db, 0.0)


def _courtesy_tone_n_samples() -> int:
    """Total sample count of one courtesy-tone segment (all beeps + the
    short inter-beep gaps, NOT the trailing silence that follows it)."""
    beep_n = _seconds_to_samples(COURTESY_TONE_BEEP_DURATION_S, PROGRAM_SAMPLE_RATE_HZ)
    gap_n = _seconds_to_samples(COURTESY_TONE_BEEP_GAP_S, PROGRAM_SAMPLE_RATE_HZ)
    return COURTESY_TONE_BEEP_COUNT * beep_n + (COURTESY_TONE_BEEP_COUNT - 1) * gap_n


def _courtesy_tone_burst(gain_db: float):
    """Synthesize the courtesy tone's float32 PCM: ``COURTESY_TONE_BEEP_COUNT``
    short sine beeps at ``COURTESY_TONE_BEEP_HZ``, separated by silent gaps.

    Deterministic from ``gain_db`` alone — beep count/duration/frequency/gap
    are fixed module constants, mirroring ``segment_stimulus``'s "regenerate
    from the schedule, store no PCM" philosophy.
    """
    import numpy as np

    sr = PROGRAM_SAMPLE_RATE_HZ
    beep_n = _seconds_to_samples(COURTESY_TONE_BEEP_DURATION_S, sr)
    gap_n = _seconds_to_samples(COURTESY_TONE_BEEP_GAP_S, sr)
    amp = 10.0 ** (gain_db / 20.0)
    t = np.arange(beep_n, dtype=np.float64) / sr
    beep = (amp * np.sin(2.0 * np.pi * COURTESY_TONE_BEEP_HZ * t)).astype(np.float32)
    # Short quadratic power-ramp fade in/out per beep (linspace**2, mirroring
    # the sweep fade) — avoids the click a
    # hard-edged tone burst would leave (mirrors sweep.py's fade-in/out).
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

    Mirrors :func:`segment_stimulus`'s contract (deterministic reconstruction,
    a length mismatch means a corrupt schedule) for the one non-``STIMULUS_KINDS``
    kind that still needs real audio rendered — see :func:`render_program_pcm`.
    """
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


def courtesy_beep_to_stimulus_gap_s(program: ExcitationProgram) -> float | None:
    """Seconds from the LAST courtesy beep to the first stimulus after it.

    The acceptance criterion of the §2.5 pacing fix, made computable. Since
    the 2026-07-28 reorder (issues #1810 / #1812) EVERY audible segment
    follows the beeps, so "the first stimulus after them" is simply the
    program's first stimulus: the leading pilot pair on MEASURE/VERIFY, the
    first pilot on CHECK, the sweep on a legacy pilot-less program. Two
    legitimate values, both bounded by
    :data:`COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S`:

    * :data:`COURTESY_TONE_TRAILING_SILENCE_S` — the settle alone, when the
      first stimulus follows the prelude directly (CHECK; a program composed
      without a leading pilot pair).
    * that plus :data:`PILOT_AMBIENT_WINDOW_S` — when the pre-pilot ambient
      window sits in between, which it must, because a noise floor measured
      before the "go quiet" warning is not the floor the pilots play into.

    Returns ``None`` for a program with no courtesy prelude, or one whose
    prelude has no stimulus after it at all (which would be a composition
    bug, not a pacing question).

    Derived from the schedule rather than from the constants, so a composer
    that quietly reinstated a lead-in between the beeps and the stimulus fails
    the test that reads this instead of silently lengthening the wait again.
    """
    beeps = [seg for seg in program.segments if seg.kind == KIND_COURTESY_TONE]
    if not beeps:
        return None
    beep_end = max(seg.start_sample + seg.n_samples for seg in beeps)
    starts = [
        seg.start_sample
        for seg in program.segments
        if seg.kind in STIMULUS_KINDS and seg.start_sample >= beep_end
    ]
    if not starts:
        return None
    return (min(starts) - beep_end) / program.sample_rate_hz


def _insert_courtesy_prelude(
    segments: list[ProgramSegment],
    total_samples: int,
    *,
    at_sample: int,
    channels: int,
    downstream_gain_db: float,
) -> tuple[list[ProgramSegment], int]:
    """Splice the courtesy-tone prelude in at ``at_sample``, shifting the rest.

    One tone segment per program channel (so the warning is audible on every
    driver path, not just one role's), all starting together and playing
    simultaneously, followed by one shared trailing-silence gap
    (:data:`COURTESY_TONE_TRAILING_SILENCE_S`) before the program's remaining
    content resumes untouched (same segment IDs, kinds, gains — only later).
    Everything scheduled BEFORE ``at_sample`` keeps its position.

    **Why an insertion point rather than a plain prepend (§2.5).** The #1677
    prelude originally went in front of the WHOLE program, so the beeps were
    followed by the ~3 s settle AND then everything the program needs before
    its first sweep — the CHECK ambient window (12 s), the behavioural-
    linearity pilot pair (~2.6 s), the pre-sweep guard. On hardware that read
    as "three beeps, a long gap, then the sweep", which is exactly what the
    beeps must not mean: their whole message is "the measurement is imminent,
    go quiet now". Each composer therefore hands the cursor that sits directly
    in front of the first thing it is announcing, so the interval from the
    last beep to the first stimulus is the settle (plus, where one exists, the
    pre-pilot ambient window — see :data:`COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S`)
    and nothing else.

    **What "the first thing it is announcing" means (issue #1812, 2026-07-28).**
    Between #1771 and this change it meant the first SWEEP, on the theory
    that a leading pilot pair is lead-in rather than measurement. That was
    wrong on hardware: a pilot is a full-gain chirp, and MEASURE/VERIFY
    therefore opened with two audible chirps at t=0 ahead of the quieter
    beeps. It now means the first audible content of any kind — so on a
    program with a leading pilot pair the cursor is sample 0, and nothing
    audible can precede the warning. CHECK still splices after its (silent)
    12 s session-ambient window; see the module docstring.

    Composition order is free for the analysis, which locates every segment by
    its recorded offset (``program_analysis._locate_segments``) and anchors on
    whichever stimulus segment comes first — still the pilot pair in every
    program, before and after this move.

    Each channel's tone gain is derived from THAT channel's own loudest
    scheduled stimulus (:func:`courtesy_tone_gain_db`), so the tone
    can never exceed what the channel is already about to play, independent
    of how the two drivers' levels relate to each other. A channel with no
    stimulus segments at all (should not happen for a real program) gets no
    tone rather than an undefined reference level.
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

    Leading silence is the session ambient measurement (reused by the ambient
    band-floor report). Then, per driver, two short band-limited pilot ESS
    chirps at ``pilot_levels_db`` (relative to ``base_peak_dbfs``, ≥0.5 s apart)
    — their captured level ratio drives the behavioral AGC/linearity verdict,
    and their band-concentrated energy drives channel-map sanity. ``pilot_levels_db``
    are RELATIVE offsets: pilot digital gain = ``base_peak_dbfs + level``.

    ``role_base_peak_dbfs`` (v2 session, Wave 6.1 — cap-aware composition)
    OPT-IN overrides ``base_peak_dbfs`` PER ROLE so a driver whose safety cap
    binds below the shared reference (e.g. a compression tweeter) rides a lower
    per-driver base. Because both pilots keep the same ``pilot_levels_db``
    offsets against the SAME per-role base, the pair's 10 dB relative delta is
    preserved regardless of how far the base is clamped; only the absolute
    level degrades, honestly recorded in the segments' gains. ``None`` (the
    default) is byte-identical to the pre-v2 composer.

    ``courtesy_prelude`` (issue #1677) OPT-IN prepends the "beep beep beep" +
    silence warning (see the module docstring); ``False`` (the default) is
    byte-identical to the pre-#1677 composer.
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
    # The room-listening window is a SILENT lead-in, so the courtesy beeps go
    # after it (§2.5): the ambient measurement is what the program needs
    # before its first stimulus, and putting 12 s of it between the beeps and
    # the pilots is precisely the "long gap" the beeps promised would not be
    # there. Nothing audible precedes the beeps either way (issue #1812's
    # rule) — this window is silence, and it is the SESSION's room-noise
    # measurement, deliberately taken before the household is asked to quiet.
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
    the second (``sweep_w_rep``), ``_rep{n}`` for the (n+1)-th thereafter
    (``sweep_w_rep2``, ``sweep_w_rep3``, …). Mirrors the pre-v2 single-repeat
    ``sweep_w``/``sweep_w_rep`` pair exactly at ``index`` 0/1, so existing
    first-occurrence lookups (``program.segment("sweep_w")``) keep working
    unmodified regardless of ``repeat_count``.
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
    interleaved woofer/tweeter sweep cycles.

    Exactly two drivers (2-way): ``roles_bands[0]`` is the lower driver (woofer,
    ch0), ``roles_bands[1]`` is the upper (tweeter, ch1). Layout for
    ``repeat_count`` cycles (default :data:`MEASURE_REPEAT_COUNT`)::

        [ambient window → pilot lo → gap → pilot hi → gap →]
                                     (v2, when leading pilots requested)
        guard silence
          → woofer sweep 1 → MESM gap → tweeter sweep 1 → MESM gap
          → woofer sweep 2 → MESM gap → tweeter sweep 2 → MESM gap
          → ...
          → woofer sweep N → MESM gap → tweeter sweep N
        → tail silence

    Every sweep after a driver's first is a bit-identical stimulus to that
    driver's first sweep (same gain, band, duration ⇒ same PCM) — the repeats
    form the in-capture drift estimator and the dropped-buffer/glitch detector
    (design §3.1), now for BOTH drivers rather than the woofer alone.
    ``gain_plan`` maps role → digital gain (dBFS, non-positive);
    ``sweep_durations`` maps role → sweep duration (defaults: ~4 s woofer /
    ~3 s tweeter), applied to EVERY occurrence of that role. Gaps come from
    :func:`mesm_gap_samples` sized to the PRECEDING sweep — a gap follows
    every sweep except the very last (tail silence follows directly instead).

    ``sweep_duration_limits_s`` maps role → the longest ONE sweep that role's
    admitted safety limits allow (the caller reads it from
    ``jasper.active_speaker.excitation_safety_plan.effective_sweep_duration_limit_s``,
    the one owner of that ``min``). A requested duration realizes at the NEAREST
    phase-closing length, which can land just ABOVE the number the admission
    gate then compares it against — 150–4000 Hz asked for 4.0 s realizes
    4.00577 s, 5.8 ms over a 4.0 s limit, and admission refuses the whole
    program (``program_segment_outside_limits``). So when the realized nominal
    would exceed a role's limit this composes the longest phase-closing sweep AT
    OR BELOW it instead (:func:`~jasper.audio_measurement.sweep.phase_closing_duration_s`)
    and logs ``event=measure_program.sweep_fitted``. A role absent from the
    mapping, or one with room to spare, keeps its nominal sweep byte for byte,
    so the fit is inert wherever it is not needed — including with the default
    ``None``.

    **Why this is not one box's problem.** The driver-research prompt
    (``jasper.active_speaker.driver_safety``) instructs the LLM to "Send
    max_sweep_duration_s 4, max_repeat_count 3, minimum_cooldown_s 2 unless a
    datasheet says stricter", and its RESULT SHAPE exemplar hard-codes the same
    triple — against a :data:`DEFAULT_WOOFER_SWEEP_S` of exactly 4.0. Over a
    grid of 1044 plausible woofer bands, 535 realize above that request, so
    roughly half of every box commissioned through the standard prompt was
    exposed. Tweeters escaped only because :data:`DEFAULT_TWEETER_SWEEP_S` is
    3 s under the same 4 s ceiling. That prompt guidance stays as it is: with
    this fit, a declared 4 is harmless by construction.

    **What the fit costs.** A fitted sweep is exactly one cycle at ``f1``
    shorter than the overshooting one — ``ln(f2/f1)/f1``, 21.89 ms on that
    150–4000 Hz woofer band, against a 4005.8 ms nominal — so it carries
    ``-10·log10(3.983876/4.005766)`` = 0.0238 dB less excitation energy.
    That is the whole SNR cost, and it is negligible
    against the 12 dB SNR floor the capture is graded on. A limit tight enough
    to cost real SNR is a tight DECLARATION, not this rounding, and it shows up
    as the existing ``snr_floor`` verdict rather than being hidden here. A limit
    too tight for even one cycle at ``f1`` raises: an unmeasurable band is a
    refusal, not a shorter sweep.

    Segment IDs: each driver's first occurrence keeps exactly ``sweep_w`` /
    ``sweep_t`` (existing lookups depend on these); later occurrences follow
    :func:`_occurrence_suffix` (``sweep_w_rep``, ``sweep_w_rep2``, … /
    ``sweep_t_rep``, ``sweep_t_rep2``, …). Gap IDs carry the SAME suffix as
    the sweep that sizes them: ``gap_w_t`` / ``gap_t_w`` for the first cycle,
    ``gap_w_t_rep`` / ``gap_t_w_rep`` for the second, and so on.

    ``leading_pilot_gains_db`` (v2 session, Wave 5a — design §5.2) OPT-IN
    prepends a two-level ``(lo, hi)`` pilot pair on ``leading_pilot_role``'s
    channel (default the lower/woofer driver) so this capture carries its own
    behavioral-linearity evidence, preceded by the short
    :data:`PILOT_AMBIENT_WINDOW_S` room-listening window that makes those
    pilots' SNR measurable (issue #1810). ``None`` (the default) omits both —
    the program then starts at ``guard``, byte-identical to the pre-v2 shape.

    ``courtesy_prelude`` (issue #1677) OPT-IN inserts the "beep beep beep" +
    silence warning (see the module docstring) in front of the first AUDIBLE
    content: sample 0 when a leading pilot pair is requested (the pilots are
    full-gain stimulus, so nothing audible may precede the warning — issue
    #1812, 2026-07-28), otherwise directly in front of the first sweep.
    ``False`` (the default) is byte-identical to the pre-#1677 composer.
    """
    roles = _validate_roles(roles_bands)
    if len(roles) != 2:
        raise ValueError("MEASURE is a 2-way flow: exactly two drivers required")
    woofer, tweeter = roles[0], roles[1]
    for rb in roles:
        if rb.role not in gain_plan:
            raise ValueError(f"gain_plan is missing role {rb.role!r}")
    if type(repeat_count) is not int or repeat_count < 1:
        raise ValueError("repeat_count must be a positive integer")
    durations = {
        woofer.role: DEFAULT_WOOFER_SWEEP_S,
        tweeter.role: DEFAULT_TWEETER_SWEEP_S,
    }
    if sweep_durations:
        durations.update(sweep_durations)
    channels = 1 + max(rb.channel for rb in roles)

    def _band(rb: RoleBand) -> tuple[float, float]:
        f1, f2 = _intersect_band(rb.band, MEASURE_SWEEP_F_LO_HZ, MEASURE_SWEEP_F_HI_HZ)
        # Defense in depth (sweep-composition PR-A, #1668): MEASURE_SWEEP_F_HI_HZ
        # is always < Nyquist today, so this can never fire in production — but
        # if a future edit ever raised the ceiling past Nyquist without noticing,
        # the sweep kernel's own raise (deep inside synchronized_sweep_metadata)
        # would still catch it, just with far less context. Fail loud, here,
        # with the composer's own frame of reference instead.
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

        Writes the fitted length back into ``durations`` so the schedule below
        composes EVERY occurrence of this role at it — the gap sized from the
        returned metadata and the segments built from the dict are then the same
        sweep. Feeding a phase-closing duration back as ``duration_approx_s``
        recovers the same cycle count, so the round-trip is exact.

        The fit lives here rather than in the caller because the ceiling has to
        be applied to the band the sweep is ACTUALLY composed over — the one
        ``_band`` intersected — and to the duration the kernel actually
        realizes. A caller fitting against its own copy of either would be a
        second owner of a number admission judges only one of.
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
    t_f1, t_f2 = _band(tweeter)
    w_meta = _fitted_meta(woofer, w_f1, w_f2)
    t_meta = _fitted_meta(tweeter, t_f1, t_f2)
    gap_w_n = mesm_gap_samples(w_meta, ir_tail_s=ir_tail_s)
    gap_t_n = mesm_gap_samples(t_meta, ir_tail_s=ir_tail_s)

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
        # The pilot pair is full-gain audible stimulus, so the beeps announce
        # it too and belong in front of it (issue #1812) — sample 0. The
        # ambient window then sits inside the settle, immediately before the
        # pilots whose noise floor it measures (issue #1810).
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
        # No leading pilot pair (the legacy shape): the first audible content
        # is the sweep, the guard silence ahead of it is ordinary lead-in, and
        # the beeps land directly in front of the sweep they announce (§2.5).
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
        segments.append(_silence(f"gap_w_t{suffix}", cursor, gap_w_n))
        cursor += gap_w_n

        sweep_t = _sweep(f"sweep_t{suffix}", tweeter, t_f1, t_f2, durations[tweeter.role])
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
    courtesy_prelude: bool = False,
) -> ExcitationProgram:
    """Compose the VERIFY program (design §5.2): a mono full-band summed sweep.

    One channel: ``[ambient window → pilot lo → gap → pilot hi → gap →]``
    (v2, when leading pilots requested) guard silence + one full-band summed
    ESS (~6 s) + tail,
    played through the APPLIED production graph (the real system, not a
    commissioning construct). ``fc_hz`` widens the low bound when the crossover
    is low so the lower shoulder ``fc/2`` is always excited:
    ``f1 = min(VERIFY_F_LO_HZ, fc/2)``.

    ``leading_pilot_gains_db`` (v2 session, Wave 5a — design §5.2) OPT-IN
    prepends a two-level ``(lo, hi)`` mono pilot pair (role ``"summed"``),
    preceded by the :data:`PILOT_AMBIENT_WINDOW_S` room-listening window
    (issue #1810), so VERIFY also carries its own behavioral-linearity
    evidence AND the noise floor needed to trust it. The pilot rides
    its OWN band (W6.7 ruling 2) — a flat mid-woofer region of the applied
    summed response, ``[VERIFY_PILOT_F_LO_HZ, min(VERIFY_PILOT_F_HI_HZ,
    fc/VERIFY_PILOT_FC_CLEARANCE_RATIO)]``, falling back to ``[fc/8, fc/4]``
    when a very low Fc collapses that band — rather than the summed sweep's
    full band: the sweep deliberately crosses the crossover overlap (it needs
    to see the interference notch there), and a pilot swept through that same
    notch goes noise-dominated across the notched portion, misfiring the
    linearity ratio check on noise rather than on AGC/gain behavior. ``None``
    is byte-identical to the pre-v2 composer.

    ``courtesy_prelude`` (issue #1677) OPT-IN inserts the "beep beep beep" +
    silence warning (see the module docstring) in front of the first AUDIBLE
    content: sample 0 when a leading pilot pair is requested (issue #1812,
    2026-07-28), otherwise directly in front of the summed sweep.
    ``False`` (the default) is byte-identical to the pre-#1677 composer. VERIFY has no
    program-admission gate (it rides the applied production graph — see
    ``jasper.active_speaker.program_admission``'s ``_validate_program``), so
    the prelude's compose-time clamp (``courtesy_tone_gain_db``) is the ONLY
    level guard here, exactly like the summed sweep itself.
    """
    if not (fc_hz > 0) or not math.isfinite(fc_hz):
        raise ValueError("fc_hz must be finite and positive")
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
        # Fc-aware pilot band (see the VERIFY_PILOT_* constants block): the
        # fixed 200-800 Hz window is only flat while the crossover overlap
        # sits above it, so the hi bound is clamped below the Fc/2 shoulder,
        # with an [fc/8, fc/4] fallback when the clamp collapses the band.
        pilot_lo = VERIFY_PILOT_F_LO_HZ
        pilot_hi = min(VERIFY_PILOT_F_HI_HZ, fc_hz / VERIFY_PILOT_FC_CLEARANCE_RATIO)
        if not pilot_lo < pilot_hi:
            pilot_lo, pilot_hi = fc_hz / 8.0, fc_hz / 4.0
        # Same ordering rule as MEASURE (issues #1810 / #1812): beeps first,
        # then the settle, then the ambient window, then the pilots.
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
        # Legacy pilot-less VERIFY: the beeps land in front of the sweep.
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
    :func:`segment_stimulus`, and each courtesy-tone segment (issue #1677) via
    :func:`courtesy_tone_stimulus`, then placed on its channel at its scheduled
    offset; silence segments contribute nothing. No PCM is stored on the
    program — this is the single renderer both the WAV writer and the analysis
    fixtures use.
    """
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
