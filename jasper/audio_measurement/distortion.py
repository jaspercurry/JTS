# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Harmonic-distortion read-out from a synchronized-sweep capture.

The program's sweeps are Novak-synchronized by construction, so the order-``N``
harmonic image sits exactly ``L·ln(N)`` seconds AHEAD of the linear impulse
response in every deconvolution. :mod:`jasper.audio_measurement.deconv` owns
the windowing kernels and :func:`jasper.audio_measurement.analysis.thd_curve`
the ratio; this module composes them and adds how much pre-guard the
deconvolution needs so the images exist at all.
:data:`jasper.audio_measurement.program_analysis.DECONV_PRE_GUARD_S` (0.25 s)
is far SMALLER than those advances (the shipped 150-4000 Hz MEASURE woofer
sweep has ``L = 1.2200 s``: H2 leads by ≈0.85 s, H3 by ≈1.34 s) and
deconvolution is circular, so analysis re-deconvolves the SAME capture bytes at
:func:`required_pre_guard_s`. Production windows are untouched.

``relative_db[order]`` is the harmonic's level MINUS the fundamental's at the
same EXCITATION frequency, negative for a well-behaved driver. The absolute
magnitudes are deconvolution units, meaningful only as a difference, and the
drive level that produced them travels in :class:`DriveLevel`. Microphone
calibration is applied at each curve's own acoustic frequency, so the ratio is
NOT cal-invariant: it carries an error of ``C(N·f) − C(f)``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from . import deconv
from .analysis import smooth_fractional_octave, thd_curve
from .calibration import apply_calibration_curve
from .sweep import SweepMeta, synchronized_sweep_metadata

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .calibration import CalibrationCurve
    from .program import ExcitationProgram, ProgramSegment

# The orders a synchronized sweep separates cleanly at the gaps the MEASURE
# program actually schedules. Deliberately NOT imported from
# `program.MESM_MAX_HARMONIC_ORDER`: that constant sizes the STIMULUS, this one
# bounds the ANALYSIS.
DEFAULT_HARMONIC_ORDERS: tuple[int, ...] = (2, 3)

# Extra pre-guard beyond the predicted window, covering `extract_harmonic_ir`'s
# ±2 ms local-peak search: a centre refined EARLIER than predicted drags the
# window's leading edge with it. True worst case is 0.6× this; costs ~96 samples.
PRE_GUARD_SEARCH_MARGIN_S = deconv.HARMONIC_PEAK_SEARCH_RADIUS_S

# Smoothing applied to every magnitude curve before the ratio: distortion
# residues are noise-dominated between the harmonic's own peaks. 1/12 octave is
# the conventional distortion-plot resolution.
DEFAULT_SMOOTHING_FRACTION = 12

# How close a harmonic may sit to the measured floor before the reading stops
# being about the driver. At 6 dB the floor contributes ~25% of the measured
# power. Points inside the margin are reported as floor-limited, never dropped.
FLOOR_LIMITED_MARGIN_DB = 6.0

# Shrink applied to the largest phantom window that clears both neighbouring
# harmonic images, so a sub-sample rounding cannot push its edge into one.
_PHANTOM_WINDOW_SAFETY = 0.9

# Trimmed off the BOTTOM of every order's band: the sweep's fade-in and the
# deconvolution's own band-edge shoulder put a spike at f1 that no nonlinearity
# produced -- on a provably linear synthetic path it sits 25.8 dB above the
# measured floor at f1 and stays above floor+6 dB for 0.192 octaves (woofer H2,
# 150-4000 Hz sweep). The TOP is deliberately NOT trimmed: an exponential sweep
# dwells far longer per octave at the bottom, and the same linear path shows no
# top-edge excursion above the floor at all.
BAND_EDGE_TRIM_OCTAVES = 0.25


@dataclass(frozen=True, slots=True)
class DriveLevel:
    """The level a distortion reading was taken at, in every reference it has.

    ``stimulus_peak_dbfs`` is the sweep's digital peak on the program channel;
    ``effective_peak_dbfs`` folds the declared downstream gain in. Both are
    dBFS re digital full scale and describe what was PLAYED.
    ``capture_peak_dbfs`` / ``capture_rms_dbfs`` describe what was RECORDED,
    dBFS re the capture device's full scale, over the scheduled sweep window.
    They are NOT SPL -- this path has no acoustic reference; anything that does
    belongs in ``notes`` verbatim.
    """

    stimulus_peak_dbfs: float
    effective_peak_dbfs: float
    capture_peak_dbfs: float
    capture_rms_dbfs: float
    notes: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class HarmonicReading:
    """One sweep segment's harmonic-distortion read-out.

    ``freqs_hz`` is the EXCITATION-frequency grid masked to ``band_hz``;
    ``relative_db[order]`` that order's level below the fundamental at the same
    excitation frequency; ``thd_percent`` the root-sum-square of the requested
    orders over the fundamental, in percent. ``floor_relative_db[order]`` is the
    measured noise floor in the SAME units.

    ``pre_guard_s`` is what the deconvolution actually got and
    ``preceding_silence_s`` how much program silence sat in front of the sweep,
    so ``clearance_s = preceding_silence_s − required_pre_guard_s`` is negative
    when the H-``N`` window reaches back into the previous segment's audio: the
    read is still returned but is no longer clean.
    """

    segment_id: str
    role: str | None
    orders: tuple[int, ...]
    band_hz: tuple[float, float]
    freqs_hz: np.ndarray
    fundamental_db: np.ndarray
    relative_db: Mapping[int, np.ndarray]
    floor_relative_db: Mapping[int, np.ndarray]
    thd_percent: np.ndarray
    drive: DriveLevel
    sweep: SweepMeta
    pre_guard_s: float
    required_pre_guard_s: float
    preceding_silence_s: float
    smoothing_fraction: int

    @property
    def clearance_s(self) -> float:
        """Program silence in front of the sweep, minus what the images need."""
        return self.preceding_silence_s - self.required_pre_guard_s

    @property
    def images_clean(self) -> bool:
        """True when every harmonic window sits in silence, not in prior audio."""
        return self.clearance_s >= 0.0

    def floor_limited(
        self, order: int, margin_db: float = FLOOR_LIMITED_MARGIN_DB
    ) -> np.ndarray:
        """Boolean mask of points sitting within ``margin_db`` of the floor.

        True means the point describes the measurement, not the driver. Points
        past the order's own band edge are NaN and count as True: a comparison
        against NaN is False in numpy, which would otherwise mark the one region
        that is certainly NOT a driver reading as the cleanest of the curve.
        """
        values = np.asarray(self.relative_db[order], dtype=np.float64)
        floor = np.asarray(self.floor_relative_db[order], dtype=np.float64)
        separation = values - floor
        return ~(separation >= float(margin_db))

    def harmonic_db(self, order: int) -> np.ndarray:
        """Absolute level of order ``order`` -- ``fundamental_db + relative_db``.

        Consult this before believing a peak in ``relative_db``: the ratio also
        rises wherever the FUNDAMENTAL dips.
        """
        return np.asarray(self.fundamental_db, dtype=np.float64) + np.asarray(
            self.relative_db[order], dtype=np.float64
        )

    def worst(
        self, order: int, *, above_floor: bool = True
    ) -> tuple[float, float]:
        """``(frequency_hz, relative_db)`` of this order's worst (highest) point.

        With ``above_floor`` (the default) only points clear of the measured
        floor are eligible; an order that never clears its floor returns
        ``(nan, nan)``.
        """
        return worst_clear_of_floor(
            self.freqs_hz,
            self.relative_db[order],
            self.floor_limited(order) if above_floor else None,
        )


def worst_clear_of_floor(
    freqs_hz: np.ndarray,
    relative_db: np.ndarray,
    floor_limited: np.ndarray | None = None,
) -> tuple[float, float]:
    """``(frequency_hz, relative_db)`` of the worst (highest) eligible point.

    The one owner of the eligibility policy every summary shares: a point counts
    only while it is finite (inside the order's own band) and, when a mask is
    supplied, not floor-limited. No eligible point returns ``(nan, nan)``.
    """
    values = np.asarray(relative_db, dtype=np.float64)
    eligible = np.isfinite(values)
    if floor_limited is not None:
        eligible &= ~np.asarray(floor_limited, dtype=bool)
    if not np.any(eligible):
        return float("nan"), float("nan")
    index = int(np.argmax(np.where(eligible, values, -np.inf)))
    return (
        float(np.asarray(freqs_hz, dtype=np.float64)[index]),
        float(values[index]),
    )


def validated_orders(orders: Sequence[int]) -> tuple[int, ...]:
    """The requested orders, or a refusal naming what is wrong with them."""
    checked = tuple(int(order) for order in orders)
    if not checked:
        raise ValueError("at least one harmonic order is required")
    if any(order < 2 for order in checked):
        raise ValueError("harmonic orders must be integers of at least 2")
    if len(set(checked)) != len(checked):
        raise ValueError("harmonic orders must be distinct")
    return checked


def _image_half_width_s(meta: SweepMeta, order: int) -> float:
    """Half-width :func:`~.deconv.extract_harmonic_ir` gives order ``order``.

    Computed from the PREDICTED centre; the runtime function measures its gap
    from the SEARCHED one, so the two differ by at most 0.6× the ±2 ms search
    radius, which :data:`PRE_GUARD_SEARCH_MARGIN_S` absorbs.
    """
    return (
        deconv.HARMONIC_WINDOW_GAP_FRACTION
        * meta.L
        * math.log((order + 1) / order)
    )


def _phantom_window_s(meta: SweepMeta, order: int) -> tuple[float, float]:
    """``(centre_advance_s, half_width_s)`` of order ``order``'s phantom window.

    Centred at ``L·ln(order − ½)`` -- the gap BELOW image ``order`` -- and
    widened until it just clears both neighbouring image windows. The LOWER gap
    because the deconvolution's own artefacts are strongest near the direct
    arrival and fall away from it: an upper-gap phantom under-reports the floor,
    and on a provably linear synthetic path it left H2's pure artefact more than
    6 dB "clear" of its own floor. The lower gap over-estimates instead, so the
    reading errs toward refusing to claim distortion.

    The one owner of this geometry: :func:`required_pre_guard_s` sizes the
    deconvolution from it and :func:`_phantom_floor_ir` cuts the window with it.
    """
    if order < 2:
        raise ValueError("a phantom floor is only defined for orders above 1")
    advance = meta.L * math.log(order - 0.5)
    clearance = min(
        advance
        - deconv.harmonic_time_advance_s(meta, order - 1)
        - _image_half_width_s(meta, order - 1),
        deconv.harmonic_time_advance_s(meta, order)
        - _image_half_width_s(meta, order)
        - advance,
    )
    return advance, _PHANTOM_WINDOW_SAFETY * clearance


def required_pre_guard_s(
    meta: SweepMeta, orders: Sequence[int] = DEFAULT_HARMONIC_ORDERS
) -> float:
    """Seconds of pre-guard every window this reading cuts needs.

    The order-``N`` image sits ``L·ln(N)`` ahead of the linear IR and is
    windowed to ``±`` :func:`_image_half_width_s`, so its leading edge sits at
    ``L·ln(N) + half_width`` before the direct arrival; the maximum over the
    requested orders binds, plus :data:`PRE_GUARD_SEARCH_MARGIN_S` for the
    local-peak search. The fundamental's window and the phantom-floor windows
    are enumerated too, though neither binds, so the guard follows the window
    geometry if it ever moves.
    """
    orders = validated_orders(orders)
    edges = [
        deconv.harmonic_time_advance_s(meta, order) + _image_half_width_s(meta, order)
        for order in (1, *orders)
    ]
    edges += [sum(_phantom_window_s(meta, order)) for order in orders]
    return max(edges) + PRE_GUARD_SEARCH_MARGIN_S


def order_band_hz(meta: SweepMeta, order: int) -> tuple[float, float]:
    """The excitation band over which order ``order`` is real: ``[f1, f2/order]``.

    The upper edge is the DECONVOLUTION'S OWN passband, not Nyquist: the
    inversion divides by ``|X(f)|² + ε`` and the sweep puts no energy above
    ``f2``, so the order-``N`` product of excitation ``f`` survives only while
    ``N·f ≤ f2``. A woofer swept 150-4000 Hz yields honest H2 to 2000 Hz and H3
    to 1333 Hz; a tweeter swept 1600-20000 Hz, H2 to 10 kHz and H3 to 6667 Hz.
    ``SweepMeta`` already forbids ``f2 ≥ nyquist``, so this bound always binds.
    The lower edge carries :data:`BAND_EDGE_TRIM_OCTAVES` of trim above ``f1``.
    """
    if type(order) is not int or order < 1:
        raise ValueError("harmonic order must be a positive integer")
    lo = float(meta.f1) * 2.0**BAND_EDGE_TRIM_OCTAVES
    hi = float(meta.f2) / order
    if hi <= lo:
        raise ValueError(
            f"sweep {meta.f1:g}-{meta.f2:g} Hz is too narrow for order {order}: "
            f"nothing is left between the band-edge trim ({lo:g} Hz) and the "
            f"deconvolution passband limit ({hi:g} Hz)"
        )
    return lo, hi


def analysis_band_hz(
    meta: SweepMeta, orders: Sequence[int] = DEFAULT_HARMONIC_ORDERS
) -> tuple[float, float]:
    """The band over which EVERY requested order is real -- ``[f1, f2/max]``.

    What a summed figure such as THD needs: a total that silently dropped an
    order above its own edge would read as a falling distortion curve. Per-order
    curves reach further and use :func:`order_band_hz` instead.
    """
    return order_band_hz(meta, max(validated_orders(orders)))


def _magnitude_on_excitation_axis(
    harmonic_ir: np.ndarray,
    sample_rate: int,
    order: int,
    *,
    calibration: "CalibrationCurve | None",
    smoothing_fraction: int,
    n_fft: int,
) -> tuple[np.ndarray, np.ndarray]:
    """One windowed image's calibrated, smoothed magnitude on the excitation axis.

    Calibration is applied at the ACOUSTIC frequency ``order · f``, and only
    then is the axis divided down to the excitation grid. Smoothing runs on the
    curve's own linear grid and before the ratio, so both sides of the ratio are
    smoothed identically.
    """
    excitation_freqs, magnitude_db = deconv.harmonic_magnitude_response(
        harmonic_ir, sample_rate, order, n_fft
    )
    magnitude_db = apply_calibration_curve(
        excitation_freqs * order, magnitude_db, calibration
    )
    if smoothing_fraction > 0:
        magnitude_db = smooth_fractional_octave(
            excitation_freqs, magnitude_db, smoothing_fraction
        )
    return excitation_freqs, magnitude_db


def _phantom_floor_ir(
    full_ir: np.ndarray,
    sample_rate: int,
    direct_peak_idx: int,
    meta: SweepMeta,
    order: int,
) -> tuple[np.ndarray, float]:
    """A window where no harmonic image can be -- the read's own noise floor.

    Returns ``(windowed_ir, length_ratio)``. The window is centred at
    ``L·ln(order − ½)``, strictly between image ``order−1`` and image ``order``
    per :func:`_phantom_window_s`, and widened until it just clears BOTH of
    their windows (times :data:`_PHANTOM_WINDOW_SAFETY`). It is necessarily
    NARROWER than the image window it describes, so ``length_ratio`` is
    ``image_half_width / phantom_half_width`` and the caller adds
    ``10·log10(length_ratio)`` to bring the floor onto the image's scale. That
    correction assumes a spectrally flat floor, so the result is an estimate
    rather than a bound and is never used to modify a harmonic value.
    """
    if not 0 <= direct_peak_idx < len(full_ir):
        raise ValueError("direct peak is outside the impulse response")
    advance, half_width_s = _phantom_window_s(meta, order)
    half_width = int(round(half_width_s * sample_rate))
    image_half_width = int(round(_image_half_width_s(meta, order) * sample_rate))
    center = direct_peak_idx - int(round(advance * sample_rate))
    start, end = center - half_width, center + half_width + 1
    if half_width < 1 or start < 0 or end > len(full_ir):
        raise ValueError(
            f"phantom floor window for order {order} crosses the impulse "
            f"response boundary"
        )
    window = np.hanning(end - start)
    return full_ir[start:end] * window, image_half_width / max(half_width, 1)


def harmonic_reading_from_ir(
    full_ir: np.ndarray,
    sample_rate: int,
    direct_peak_idx: int,
    meta: SweepMeta,
    *,
    segment_id: str,
    role: str | None,
    drive: DriveLevel,
    orders: Sequence[int] = DEFAULT_HARMONIC_ORDERS,
    band_hz: tuple[float, float] | None = None,
    calibration: "CalibrationCurve | None" = None,
    smoothing_fraction: int = DEFAULT_SMOOTHING_FRACTION,
    pre_guard_s: float = float("nan"),
    preceding_silence_s: float = float("nan"),
    n_fft: int | None = None,
) -> HarmonicReading:
    """Read H-``orders`` out of one already-deconvolved impulse response.

    Pure: no I/O, no device, no schedule lookup. ``full_ir`` must come from a
    deconvolution whose pre-guard was at least :func:`required_pre_guard_s`.
    ``band_hz`` defaults to :func:`order_band_hz` of the LOWEST requested order
    (178-2000 Hz for the shipped woofer sweep at orders ``(2, 3)``) and a
    supplied band is intersected with that; each higher order is then NaN-masked
    to its own shorter edge, and THD to :func:`analysis_band_hz`.
    ``pre_guard_s`` / ``preceding_silence_s`` are recorded, not used.
    """
    orders = validated_orders(orders)

    # The reported GRID reaches as far as the LOWEST order does, while each
    # order is masked to its own edge below and the summed THD to the
    # all-orders band. One grid, three honest extents.
    derived_lo, derived_hi = order_band_hz(meta, min(orders))
    if band_hz is not None:
        derived_lo = max(derived_lo, float(band_hz[0]))
        derived_hi = min(derived_hi, float(band_hz[1]))
        if derived_hi <= derived_lo:
            raise ValueError(
                f"requested band {band_hz[0]:g}-{band_hz[1]:g} Hz does not "
                f"overlap the band where orders {orders} are real"
            )
    band = (derived_lo, derived_hi)
    all_orders_hi = min(band[1], analysis_band_hz(meta, orders)[1])

    # Every window shares ONE FFT length, derived from the widest of them (the
    # fundamental's). Per-window lengths would give each order a different bin
    # density, making the phantom floor's length-ratio correction meaningless.
    images = {
        order: deconv.extract_harmonic_ir(
            full_ir, sample_rate, direct_peak_idx, meta, order
        )
        for order in (1, *orders)
    }
    if n_fft is None:
        n_fft = max(8192, 1 << (max(len(images[1]) - 1, 1)).bit_length())

    fund_freqs, fund_db = _magnitude_on_excitation_axis(
        images[1], sample_rate, 1,
        calibration=calibration,
        smoothing_fraction=smoothing_fraction,
        n_fft=n_fft,
    )
    harmonics = {
        order: _magnitude_on_excitation_axis(
            images[order], sample_rate, order,
            calibration=calibration,
            smoothing_fraction=smoothing_fraction,
            n_fft=n_fft,
        )
        for order in orders
    }
    floors = {}
    for order in orders:
        phantom_ir, length_ratio = _phantom_floor_ir(
            full_ir, sample_rate, direct_peak_idx, meta, order
        )
        floor_freqs, floor_db = _magnitude_on_excitation_axis(
            phantom_ir, sample_rate, order,
            calibration=calibration,
            smoothing_fraction=smoothing_fraction,
            n_fft=n_fft,
        )
        floors[order] = (floor_freqs, floor_db + 10.0 * math.log10(length_ratio))

    grid_mask = (fund_freqs >= band[0]) & (fund_freqs <= band[1])
    freqs = fund_freqs[grid_mask]
    fundamental_db = fund_db[grid_mask]
    if freqs.size == 0:
        raise ValueError(
            f"band {band[0]:g}-{band[1]:g} Hz contains no FFT bins; "
            f"raise n_fft or widen the band"
        )
    # Every order's own axis spans [0, nyquist/order] and `band` is capped at
    # nyquist/max(orders), so this interpolation never extrapolates in-band.
    def _on_grid(order: int, source: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
        """One order's curve on the reported grid, NaN past its own edge.

        NaN rather than a clipped band because the grid is shared: a reader
        indexing by frequency must get "not measurable here" instead of a value
        borrowed from the regularization floor.
        """
        values = np.interp(freqs, source[0], source[1]) - fundamental_db
        return np.where(freqs <= order_band_hz(meta, order)[1], values, np.nan)

    relative_db = {
        order: _on_grid(order, source) for order, source in harmonics.items()
    }
    floor_relative_db = {
        order: _on_grid(order, source) for order, source in floors.items()
    }
    # THD needs every order present, so it is computed on the all-orders band
    # and placed back on the shared grid, never extended past it.
    thd_percent = np.full(freqs.shape, np.nan, dtype=np.float64)
    if all_orders_hi > band[0]:
        thd_freqs, ratio = thd_curve(
            fund_freqs, fund_db, harmonics, band=(band[0], all_orders_hi)
        )
        thd_percent[np.searchsorted(freqs, thd_freqs)] = ratio * 100.0
    return HarmonicReading(
        segment_id=segment_id,
        role=role,
        orders=orders,
        band_hz=band,
        freqs_hz=freqs,
        fundamental_db=fundamental_db,
        relative_db=relative_db,
        floor_relative_db=floor_relative_db,
        thd_percent=thd_percent,
        drive=drive,
        sweep=meta,
        pre_guard_s=float(pre_guard_s),
        required_pre_guard_s=required_pre_guard_s(meta, orders),
        preceding_silence_s=float(preceding_silence_s),
        smoothing_fraction=int(smoothing_fraction),
    )


def segment_sweep_meta(segment: "ProgramSegment") -> SweepMeta:
    """The synchronized-sweep metadata for one scheduled stimulus segment.

    Reconstructed from the schedule the same way
    :func:`jasper.audio_measurement.program.segment_stimulus` reconstructs the
    PCM, so ``L`` here is the ``L`` that was played.
    """
    if segment.f1_hz is None or segment.f2_hz is None:
        raise ValueError(
            f"segment {segment.segment_id!r} declares no sweep band"
        )
    meta = synchronized_sweep_metadata(
        f1=float(segment.f1_hz),
        f2=float(segment.f2_hz),
        duration_approx_s=segment.n_samples / float(_program_rate()),
        sample_rate=_program_rate(),
        amplitude_dbfs=float(segment.gain_db),
    )
    if meta.n_samples != segment.n_samples:
        raise ValueError(
            f"segment {segment.segment_id!r} sweep reconstruction produced "
            f"{meta.n_samples} samples, schedule says {segment.n_samples}"
        )
    return meta


def _program_rate() -> int:
    """The program sample rate, imported lazily to keep `program` off the import
    path of callers that only need the IR-level math."""
    from .program import PROGRAM_SAMPLE_RATE_HZ

    return int(PROGRAM_SAMPLE_RATE_HZ)


def preceding_silence_s(
    program: "ExcitationProgram", segment: "ProgramSegment"
) -> float:
    """Seconds of scheduled silence immediately before ``segment`` starts.

    Read off the schedule, never assumed from a default: the MESM gaps are sized
    by the PRECEDING sweep's ``L`` while the FOLLOWING sweep's harmonic windows
    are sized by its own, and on the shipped MEASURE program that difference
    decides whether a woofer repeat's H3 window is clean. A segment with nothing
    audible before it returns its own start time.
    """
    start = int(segment.start_sample)
    ends = [
        other.start_sample + other.n_samples
        for other in program.known_audible_segments()
        if other.segment_id != segment.segment_id
        and other.start_sample + other.n_samples <= start
    ]
    last_end = max(ends) if ends else 0
    return (start - last_end) / float(program.sample_rate_hz)


def capture_drive_level(
    capture: np.ndarray,
    segment: "ProgramSegment",
    anchor: int,
    *,
    notes: Mapping[str, object] | None = None,
) -> DriveLevel:
    """Level metadata for one sweep, played-side and recorded-side.

    The recorded side is measured over the SCHEDULED sweep window, the same
    anchor the deconvolution uses. A window that falls outside the capture
    yields ``-inf`` rather than raising.
    """
    lo = max(0, int(anchor))
    hi = min(int(capture.size), max(lo, int(anchor) + int(segment.n_samples)))
    window = np.asarray(capture[lo:hi], dtype=np.float64)
    if window.size == 0:
        peak_dbfs = rms_dbfs = float("-inf")
    else:
        peak = float(np.max(np.abs(window)))
        rms = float(np.sqrt(np.mean(window**2)))
        peak_dbfs = 20.0 * math.log10(peak) if peak > 0.0 else float("-inf")
        rms_dbfs = 20.0 * math.log10(rms) if rms > 0.0 else float("-inf")
    return DriveLevel(
        stimulus_peak_dbfs=float(segment.gain_db),
        effective_peak_dbfs=float(segment.effective_peak_dbfs),
        capture_peak_dbfs=peak_dbfs,
        capture_rms_dbfs=rms_dbfs,
        notes=notes,
    )


def read_segment_distortion(
    program: "ExcitationProgram",
    capture: np.ndarray,
    segment_id: str,
    anchor: int,
    *,
    orders: Sequence[int] = DEFAULT_HARMONIC_ORDERS,
    calibration: "CalibrationCurve | None" = None,
    epsilon: float = 0.0,
    band_hz: tuple[float, float] | None = None,
    smoothing_fraction: int = DEFAULT_SMOOTHING_FRACTION,
    tail_s: float = 0.5,
    level_notes: Mapping[str, object] | None = None,
    n_fft: int | None = None,
) -> HarmonicReading:
    """Deconvolve one scheduled sweep at a harmonic-safe pre-guard and read it.

    ``anchor`` is the capture-domain sample where the segment's stimulus was
    scheduled (``global_offset + segment.start_sample``). ``epsilon`` is the
    measured clock drift, divided out of the reference before inversion so a
    drifted capture does not smear the images. The ONLY function here that
    windows a capture; production behaviour is untouched.
    """
    from .program_analysis import _deconvolve_window

    segment = program.segment(segment_id)
    sample_rate = int(program.sample_rate_hz)
    meta = segment_sweep_meta(segment)
    needed = required_pre_guard_s(meta, orders)
    full_ir, pre_effective = _deconvolve_window(
        capture,
        segment,
        int(anchor),
        sample_rate,
        epsilon=epsilon,
        pre_guard_s=needed,
        tail_s=tail_s,
    )
    # Compared in SAMPLES against the window's own rounding of the same request,
    # not in seconds: `_deconvolve_window` takes `int(round(...))`, so a
    # seconds-domain comparison fails by one sample's worth of float on a window
    # that clamped nothing. Only the head clamp makes these differ, and that is
    # exactly the condition worth refusing -- the images then wrapped.
    requested_samples = int(round(needed * sample_rate))
    if pre_effective < requested_samples:
        raise ValueError(
            f"segment {segment_id!r}: capture has {pre_effective / sample_rate:.3f} s "
            f"before the sweep but orders {tuple(orders)} need "
            f"{needed:.3f} s — the harmonic images would wrap. The capture "
            f"starts too close to this sweep."
        )
    pre_guard_got_s = pre_effective / float(sample_rate)
    # The direct arrival, located the same way `deconv.deconvolve` locates it.
    # Searching the whole IR keeps the acoustic delay out of the caller's hands.
    direct_peak_idx = int(np.argmax(np.abs(full_ir)))
    return harmonic_reading_from_ir(
        full_ir,
        int(program.sample_rate_hz),
        direct_peak_idx,
        meta,
        segment_id=segment_id,
        role=segment.role,
        drive=capture_drive_level(
            capture, segment, int(anchor), notes=level_notes
        ),
        orders=orders,
        band_hz=band_hz,
        calibration=calibration,
        smoothing_fraction=smoothing_fraction,
        pre_guard_s=pre_guard_got_s,
        preceding_silence_s=preceding_silence_s(program, segment),
        n_fft=n_fft,
    )
