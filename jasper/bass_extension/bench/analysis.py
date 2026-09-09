# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Campaign verdicts, composed from the existing measurement kernels.

The runner does not reinvent measurement math. This module composes the
existing ``tracking_error_db`` (signal analysis) kernel into the pass/fail
verdicts the frozen bundle records, and adds the analyses the protocol names
but no kernel owns: the isolated digital-transfer SHA match, the paired
sweep-transparency comparison, and the sustain sag / corner-shift checks.

It also reads one acoustic capture into those verdicts —
:func:`analyze_sweep_capture` over the existing deconvolution / harmonic
kernels, :func:`analyze_sustain_capture` over :func:`assess_sustain` — so the
hardware seam (:mod:`~jasper.bass_extension.bench.wired_play`) plays and
records, and every number it reports is computed here.

Thresholds are never invented inside this module. The driver-protection bounds
(THD, compression, sustain sag, corner-shift) come from the selected
:class:`~jasper.bass_extension.targets.MarginPolicy`. The measurement-quality
bounds that ``MarginPolicy`` does not carry — the repeat-spread ceiling, the SNR
floor, and the transparency RMS bound — are **caller-supplied** parameters whose
provenance is the operator's measurement / transparency policy; this module
applies them but does not choose them.

Pure and deterministic: it operates on arrays and metrics, performs no I/O, and
opens no device. Only the *live acoustic capture* upstream of it is mocked in
tests — the math here is real.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from jasper.audio_measurement.alignment import correlation
from jasper.audio_measurement.analysis import (
    smooth_fractional_octave,
    tracking_error_db,
)
from jasper.audio_measurement.distortion import (
    DEFAULT_HARMONIC_ORDERS,
    HarmonicReading,
    read_segment_distortion,
    required_pre_guard_s,
    segment_sweep_meta,
)
from jasper.audio_measurement.program import ExcitationProgram, ProgramSegment
from jasper.audio_measurement.snr_policy import DBFS_FLOOR, band_levels_dbfs
from jasper.bass_extension.alignment import minus_six_corner_hz
from jasper.bass_extension.targets import MarginPolicy

Verdict = str  # "pass" | "fail"

_PASS = "pass"
_FAIL = "fail"

#: Start/end windows the sustain sag and corner shift are read over, bounded to
#: a quarter of the hold so a short hold still yields two disjoint windows.
SUSTAIN_EDGE_WINDOW_S = 5.0

#: Gap left in front of the onset when reading the noise floor, so playback
#: chain lead-in never lands inside the floor window.
NOISE_GUARD_S = 0.1

#: Recorded on top of :func:`sweep_pre_roll_s`'s harmonic requirement so the
#: analysis's own placement refusal cannot fire on a capture the seam sized.
PRE_GUARD_MARGIN_S = 0.5

#: How much of the stimulus the onset search correlates against: the head is
#: enough to place the onset, while the full stimulus would be a 2**24-point
#: FFT on a 90 s hold.
_HEAD_S = 2.0


class CaptureUnanalyzable(ValueError):
    """This capture cannot be windowed into a reading at all."""


@dataclass(frozen=True, slots=True)
class MeasurementPolicy:
    """Operator-authorized measurement-quality bounds, supplied by the caller;
    this module applies them and never chooses them."""

    min_snr_db: float
    max_tracking_rms_db: float


#: The campaign's measurement-quality bounds. Not an operator input: the SNR
#: floor is what the wired near-field capture clears on a quiet bench, and the
#: transparency bound is the paired-reference tracking limit the frozen
#: protocol's ``sweep_transparency`` role is graded on
#: (``docs/bass-extension-waves/limiter-evidence-protocol.md``). One value, so
#: every campaign's ``transparency_policy_fingerprint`` names the same policy.
WIRED_MEASUREMENT_POLICY = MeasurementPolicy(
    min_snr_db=25.0, max_tracking_rms_db=1.0
)


def sample_peak_dbfs(samples: np.ndarray) -> float:
    """Instantaneous float sample-peak dBFS re unity full scale.

    This is the frozen detector reference — the pre/post-limiter tap value.
    A silent buffer floors at :data:`~jasper.audio_measurement.snr_policy.DBFS_FLOOR`.
    """

    array = np.abs(np.asarray(samples, dtype=np.float64))
    peak = float(np.max(array)) if array.size else 0.0
    if peak <= 0.0:
        return DBFS_FLOOR
    return float(20.0 * np.log10(peak))


def digital_clamp_passed(pre_limiter_peak_dbfs: float, margin: MarginPolicy) -> bool:
    """True iff the pre-limiter peak keeps the policy's digital headroom.

    The arithmetic-headroom eligibility check (the merged Wave-1 digital
    margin): the pre-limiter sample peak must stay at least
    ``margin.digital_margin_db`` below unity full scale.
    """

    return pre_limiter_peak_dbfs <= -float(margin.digital_margin_db)


def transfer_match(
    *,
    deployed_sha256: str,
    deployed_byte_size: int,
    reference_sha256: str,
    reference_byte_size: int,
) -> Verdict:
    """Verdict for the isolated ``digital_transfer_probe``.

    Per the protocol, matching payload SHA and byte size between the deployed
    and reference post-limiter artifacts establishes the deployed transfer
    binding.
    """

    matched = (
        deployed_sha256 == reference_sha256
        and deployed_byte_size == reference_byte_size
    )
    return _PASS if matched else _FAIL


@dataclass(frozen=True, slots=True)
class SustainVerdicts:
    quality_verdict: Verdict
    protection_verdict: Verdict
    sag_db: float
    fc_shift_pct: float


def assess_sustain(
    *,
    start_level_db: float,
    end_level_db: float,
    start_corner_hz: float,
    end_corner_hz: float,
    snr_db: float,
    margin: MarginPolicy,
    min_snr_db: float,
) -> SustainVerdicts:
    """Sag + corner-shift verdicts for the sustain stress hold.

    Sag is how far the level drooped from the start to the end of the hold;
    corner-shift is the fractional shift of the low corner over the hold. Both
    are gated by the selected margin policy.
    """

    sag_db = float(start_level_db) - float(end_level_db)
    if start_corner_hz <= 0.0:
        fc_shift_pct = float("inf")
    else:
        fc_shift_pct = abs(float(end_corner_hz) - float(start_corner_hz)) / float(
            start_corner_hz
        ) * 100.0

    quality_ok = snr_db >= min_snr_db
    protection_ok = (
        sag_db <= float(margin.sustain_sag_fail_db)
        and fc_shift_pct <= float(margin.sustain_fc_shift_fail_pct)
    )
    return SustainVerdicts(
        quality_verdict=_PASS if quality_ok else _FAIL,
        protection_verdict=_PASS if protection_ok else _FAIL,
        sag_db=sag_db,
        fc_shift_pct=fc_shift_pct,
    )


def assess_transparency(
    *,
    freqs: np.ndarray,
    candidate_response_db: np.ndarray,
    reference_response_db: np.ndarray,
    band: tuple[float, float],
    max_tracking_rms_db: float,
) -> tuple[Verdict, float, float]:
    """Paired candidate-vs-reference transparency verdict.

    The candidate limiter is transparent when the candidate-graph response
    tracks the reference (baseline-limiter) response within the transparency
    policy's RMS bound over the band. Returns ``(verdict, rms_db, max_db)``.
    """

    rms_db, max_db = tracking_error_db(
        freqs, candidate_response_db, reference_response_db, band
    )
    verdict = _PASS if rms_db <= float(max_tracking_rms_db) else _FAIL
    return verdict, float(rms_db), float(max_db)


def stimulus_lag_samples(
    capture: np.ndarray, stimulus: np.ndarray, *, sample_rate_hz: int
) -> int:
    """The capture index of ``stimulus`` sample 0, by cross-correlation.

    Only non-negative lags are searched: the recorder is armed before anything
    plays, so the stimulus can never start before the capture does.
    """

    signal = np.asarray(capture, dtype=np.float64)
    reference = np.asarray(stimulus, dtype=np.float64)
    rate = int(sample_rate_hz)
    head = reference[: min(reference.size, int(_HEAD_S * rate))]
    searchable = signal.size - reference.size + 1
    if searchable <= 0:
        raise CaptureUnanalyzable(
            f"capture ({signal.size} samples) is shorter than the stimulus "
            f"({reference.size} samples)"
        )
    corr = correlation(
        signal[: head.size + searchable - 1],
        head,
        sample_rate=rate,
        max_capture_s=signal.size / float(rate),
    )
    if corr.size == 0:
        raise CaptureUnanalyzable(
            f"no correlation window over a {signal.size}-sample capture"
        )
    return int(np.argmax(corr))


def _anchor(
    capture: np.ndarray, stimulus_body: np.ndarray, rate: int
) -> tuple[np.ndarray, np.ndarray, int]:
    """``(signal, reference, onset)`` — the preamble both analyses share."""

    signal = np.asarray(capture, dtype=np.float64)
    reference = np.asarray(stimulus_body, dtype=np.float64)
    return signal, reference, stimulus_lag_samples(
        signal, reference, sample_rate_hz=rate
    )


def _band_level_dbfs(
    samples: np.ndarray, *, sample_rate_hz: int, band: tuple[float, float]
) -> float | None:
    """Band-integrated level of ``samples`` in dBFS, or ``None`` when the window
    is too short for :func:`band_levels_dbfs` to read one at all."""

    levels = band_levels_dbfs(
        np.asarray(samples, dtype=np.float64),
        int(sample_rate_hz),
        [("band", float(band[0]), float(band[1]))],
        window="rectangular",
    )
    return float(levels[0]["level_dbfs"]) if levels else None


def corner_hz(
    samples: np.ndarray,
    *,
    reference: np.ndarray,
    sample_rate_hz: int,
    band: tuple[float, float],
) -> float:
    """The -6 dB corner of the transfer from ``reference`` to ``samples``.

    Read off the RATIO spectrum, never off the capture alone: band-limited
    noise carries tens of dB of bin-to-bin structure that is one realization's
    accident, identical in both windows and cancelled here, where reading the
    capture alone reports it as a corner that moved. ``reference`` is the same
    window of the played body that ``samples`` is the capture of.

    The passband reference is the median of the third-octave-smoothed ratio
    over the band's upper octave-and-up (``[2*band[0], band[1]]``), so a
    rolled-off low end cannot drag the level it is compared against down with
    it. Raises :class:`CaptureUnanalyzable` when there is no in-band spectrum
    to read at all: an unread corner must never pass as an unmoved one.
    """

    values = np.asarray(samples, dtype=np.float64)
    played = np.asarray(reference, dtype=np.float64)
    lo, hi = float(band[0]), float(band[1])
    length = min(values.size, played.size)
    if length == 0:
        raise CaptureUnanalyzable("corner window is empty")
    values, played = values[:length], played[:length]
    freqs = np.fft.rfftfreq(length, 1.0 / float(sample_rate_hz))
    mask = (freqs >= lo) & (freqs <= hi)
    grid = freqs[mask]
    if grid.size == 0:
        raise CaptureUnanalyzable(
            f"corner window of {length} samples resolves no bin in {lo:g}-{hi:g} Hz"
        )
    captured_magnitude = np.abs(np.fft.rfft(values))[mask]
    played_magnitude = np.abs(np.fft.rfft(played))[mask]
    peak = float(np.max(played_magnitude))
    if peak <= 0.0:
        raise CaptureUnanalyzable("the played window carries no in-band energy")
    # A stimulus bin with no energy says nothing about the plant: its ratio is
    # capture noise over a near-zero denominator.
    readable = played_magnitude >= 1e-6 * peak
    grid = grid[readable]
    ratio_db = 20.0 * np.log10(
        np.maximum(captured_magnitude[readable], 1e-30)
    ) - 20.0 * np.log10(np.maximum(played_magnitude[readable], 1e-30))
    smoothed = smooth_fractional_octave(grid, ratio_db, fraction=3)
    passband = grid >= 2.0 * lo
    passband_db = float(
        np.median(smoothed[passband]) if np.any(passband) else np.median(smoothed)
    )
    return minus_six_corner_hz(grid, smoothed - passband_db)


def proven_thd_max_ratio(reading: HarmonicReading) -> float | None:
    """The largest THD ratio over the band points the measurement floor does
    not own, or ``None`` when it owns every one of them.

    A point where ANY requested order sits within
    :data:`~jasper.audio_measurement.distortion.FLOOR_LIMITED_MARGIN_DB` of the
    measured floor describes the measurement, not the driver, so its THD is not
    evidence about the driver either. A reading with no such point proves
    nothing about distortion and fails closed, exactly like an unclean image.
    """

    thd = np.asarray(reading.thd_percent, dtype=np.float64)
    usable = np.isfinite(thd)
    for order in reading.orders:
        usable &= ~np.asarray(reading.floor_limited(order), dtype=bool)
    if not np.any(usable):
        return None
    return float(np.max(thd[usable])) / 100.0


def sweep_pre_roll_s(segment: ProgramSegment) -> float:
    """Silence the seam records in front of ``segment`` before playing it."""

    return (
        required_pre_guard_s(segment_sweep_meta(segment), DEFAULT_HARMONIC_ORDERS)
        + PRE_GUARD_MARGIN_S
    )


def _band_snr_db(
    capture: np.ndarray,
    *,
    anchor: int,
    n_samples: int,
    sample_rate_hz: int,
    band: tuple[float, float],
) -> float:
    """In-band stimulus level over the pre-onset noise floor.

    ``-inf`` when the pre-roll leaves no floor window at all, or when either
    window is too short to read a level: an unmeasured floor must fail the
    quality verdict, never pass it on a floored reading.
    """

    noise_end = int(anchor) - int(round(NOISE_GUARD_S * float(sample_rate_hz)))
    if noise_end <= 0:
        return float("-inf")
    signal_db = _band_level_dbfs(
        capture[anchor : anchor + n_samples],
        sample_rate_hz=sample_rate_hz,
        band=band,
    )
    noise_db = _band_level_dbfs(
        capture[:noise_end], sample_rate_hz=sample_rate_hz, band=band
    )
    if signal_db is None or noise_db is None:
        return float("-inf")
    return signal_db - noise_db


def _finite_or_none(value: float) -> float | None:
    """A dB field for an artifact, ``None`` when the reading is not a number."""
    return float(value) if math.isfinite(value) else None


@dataclass(frozen=True, slots=True)
class SweepCaptureAnalysis:
    """One swept capture read into the frozen signal / protection evidence."""

    lag_samples: int
    snr_db: float
    min_snr_db: float
    freqs_hz: tuple[float, ...]
    fundamental_db: tuple[float, ...]
    orders: tuple[int, ...]
    thd_max_ratio: float
    thd_fail_ratio: float
    floor_limited_fraction: dict[int, float]
    images_clean: bool
    clearance_s: float
    pre_guard_s: float
    required_pre_guard_s: float
    quality_verdict: Verdict
    protection_verdict: Verdict

    def signal_dict(self) -> dict[str, object]:
        return {
            "lag_samples": int(self.lag_samples),
            "snr_db": _finite_or_none(self.snr_db),
            "min_snr_db": float(self.min_snr_db),
            "freqs_hz": [float(value) for value in self.freqs_hz],
            "fundamental_db": [float(value) for value in self.fundamental_db],
            "pre_guard_s": float(self.pre_guard_s),
            "required_pre_guard_s": float(self.required_pre_guard_s),
            "verdict": self.quality_verdict,
        }

    def protection_dict(self) -> dict[str, object]:
        return {
            "orders": [int(order) for order in self.orders],
            "thd_max_ratio": float(self.thd_max_ratio),
            "thd_fail_ratio": float(self.thd_fail_ratio),
            "floor_limited_fraction": {
                str(order): float(fraction)
                for order, fraction in sorted(self.floor_limited_fraction.items())
            },
            "images_clean": bool(self.images_clean),
            "clearance_s": float(self.clearance_s),
            "verdict": self.protection_verdict,
        }


def analyze_sweep_capture(
    *,
    capture: np.ndarray,
    program: ExcitationProgram,
    segment_id: str,
    stimulus_body: np.ndarray,
    band: tuple[float, float],
    margin: MarginPolicy,
    policy: MeasurementPolicy,
) -> SweepCaptureAnalysis:
    """Deconvolve one captured sweep against the bytes that were played.

    The deconvolution reference is ``stimulus_body`` — the exact artifact the
    executor padded and handed to the play seam (R6) — rather than a
    schedule-regenerated waveform, so a capture is only ever compared against
    what actually reached the speaker, and the pre-roll is the one MEASURED off
    the capture rather than the one the schedule declares.

    Protection fails closed on unclean images: a harmonic read whose windows
    reach back into prior audio is unproven, not passing.
    """

    rate = int(program.sample_rate_hz)
    signal, reference, anchor = _anchor(capture, stimulus_body, rate)
    segment = program.segment(segment_id)
    needed = required_pre_guard_s(
        segment_sweep_meta(segment), DEFAULT_HARMONIC_ORDERS
    )
    if anchor < int(round(needed * rate)):
        # The placement refusal `read_segment_distortion` makes, in samples
        # against the same rounding, so the reason names the capture placement
        # rather than the window geometry it would fail on.
        raise CaptureUnanalyzable(
            f"segment {segment_id!r}: capture has {anchor / rate:.3f} s before "
            f"the stimulus but orders {DEFAULT_HARMONIC_ORDERS} need "
            f"{needed:.3f} s"
        )
    reading = read_segment_distortion(
        program,
        signal,
        segment_id,
        anchor,
        band_hz=band,
        reference=reference,
        measured_pre_roll_s=anchor / float(rate),
    )

    proven_thd = proven_thd_max_ratio(reading)
    thd_max_ratio = 0.0 if proven_thd is None else proven_thd
    floor_limited_fraction = {
        int(order): float(np.mean(reading.floor_limited(order)))
        for order in reading.orders
    }
    snr_db = _band_snr_db(
        signal,
        anchor=anchor,
        n_samples=reference.size,
        sample_rate_hz=rate,
        band=band,
    )
    images_clean = bool(reading.images_clean)
    protection_ok = (
        images_clean
        and proven_thd is not None
        and thd_max_ratio <= float(margin.thd_fail_ratio)
    )
    return SweepCaptureAnalysis(
        lag_samples=anchor,
        snr_db=snr_db,
        min_snr_db=float(policy.min_snr_db),
        freqs_hz=tuple(float(value) for value in reading.freqs_hz),
        fundamental_db=tuple(float(value) for value in reading.fundamental_db),
        orders=tuple(reading.orders),
        thd_max_ratio=thd_max_ratio,
        thd_fail_ratio=float(margin.thd_fail_ratio),
        floor_limited_fraction=floor_limited_fraction,
        images_clean=images_clean,
        clearance_s=float(reading.clearance_s),
        pre_guard_s=float(reading.pre_guard_s),
        required_pre_guard_s=float(reading.required_pre_guard_s),
        quality_verdict=_PASS if snr_db >= float(policy.min_snr_db) else _FAIL,
        protection_verdict=_PASS if protection_ok else _FAIL,
    )


@dataclass(frozen=True, slots=True)
class SustainCaptureAnalysis:
    """One sustained hold read into the frozen signal / protection evidence."""

    lag_samples: int
    snr_db: float
    min_snr_db: float
    edge_window_s: float
    start_level_db: float
    end_level_db: float
    start_corner_hz: float
    end_corner_hz: float
    sag_db: float
    fc_shift_pct: float
    quality_verdict: Verdict
    protection_verdict: Verdict

    def signal_dict(self) -> dict[str, object]:
        return {
            "lag_samples": int(self.lag_samples),
            "snr_db": _finite_or_none(self.snr_db),
            "min_snr_db": float(self.min_snr_db),
            "edge_window_s": float(self.edge_window_s),
            "start_level_db": float(self.start_level_db),
            "end_level_db": float(self.end_level_db),
            "verdict": self.quality_verdict,
        }

    def protection_dict(self) -> dict[str, object]:
        return {
            "start_corner_hz": float(self.start_corner_hz),
            "end_corner_hz": float(self.end_corner_hz),
            "sag_db": float(self.sag_db),
            "fc_shift_pct": _finite_or_none(self.fc_shift_pct),
            "verdict": self.protection_verdict,
        }


def _sustain_edge(
    captured: np.ndarray,
    played: np.ndarray,
    *,
    sample_rate_hz: int,
    band: tuple[float, float],
) -> tuple[float, float]:
    """``(level_db, corner_hz)`` for one edge window of the hold.

    Both are read AGAINST the same window of the played body, so what the two
    edges are compared on is the plant between them and not the stimulus's own
    per-window realization.
    """

    captured_db = _band_level_dbfs(
        captured, sample_rate_hz=sample_rate_hz, band=band
    )
    played_db = _band_level_dbfs(played, sample_rate_hz=sample_rate_hz, band=band)
    if captured_db is None or played_db is None:
        raise CaptureUnanalyzable(
            f"a {int(captured.size)}-sample sustain edge window is too short to "
            "read a band level"
        )
    return captured_db - played_db, corner_hz(
        captured, reference=played, sample_rate_hz=sample_rate_hz, band=band
    )


def analyze_sustain_capture(
    *,
    capture: np.ndarray,
    sample_rate_hz: int,
    stimulus_body: np.ndarray,
    band: tuple[float, float],
    margin: MarginPolicy,
    policy: MeasurementPolicy,
) -> SustainCaptureAnalysis:
    """Read the hold's start/end level and corner, then :func:`assess_sustain`.

    Every reading is a RATIO against the played body's own matching window
    (:func:`_sustain_edge`), so sag and corner shift describe the plant rather
    than the noise realization the two edges happen to carry.
    """

    rate = int(sample_rate_hz)
    signal, reference, anchor = _anchor(capture, stimulus_body, rate)
    body = signal[anchor : anchor + reference.size]
    edge_s = min(SUSTAIN_EDGE_WINDOW_S, body.size / float(rate) / 4.0)
    edge_n = max(1, int(round(edge_s * rate)))

    start_level_db, start_corner = _sustain_edge(
        body[:edge_n], reference[:edge_n], sample_rate_hz=rate, band=band
    )
    end_level_db, end_corner = _sustain_edge(
        body[-edge_n:], reference[-edge_n:], sample_rate_hz=rate, band=band
    )
    # The SNR signal window is the START EDGE, never the whole hold: a long
    # hold would cross `deconv.cap_capture_length`'s 30 s FFT ceiling inside
    # `band_levels_dbfs` and be silently truncated (with a WARNING) on every
    # take. Swept captures keep the whole body, which is bounded by the sweep.
    snr_db = _band_snr_db(
        signal,
        anchor=anchor,
        n_samples=edge_n,
        sample_rate_hz=rate,
        band=band,
    )
    verdicts = assess_sustain(
        start_level_db=start_level_db,
        end_level_db=end_level_db,
        start_corner_hz=start_corner,
        end_corner_hz=end_corner,
        snr_db=snr_db,
        margin=margin,
        min_snr_db=float(policy.min_snr_db),
    )
    return SustainCaptureAnalysis(
        lag_samples=anchor,
        snr_db=snr_db,
        min_snr_db=float(policy.min_snr_db),
        edge_window_s=edge_n / float(rate),
        start_level_db=start_level_db,
        end_level_db=end_level_db,
        start_corner_hz=start_corner,
        end_corner_hz=end_corner,
        sag_db=verdicts.sag_db,
        fc_shift_pct=verdicts.fc_shift_pct,
        quality_verdict=verdicts.quality_verdict,
        protection_verdict=verdicts.protection_verdict,
    )
