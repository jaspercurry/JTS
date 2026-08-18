# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Harmonic-distortion read-out from a synchronized-sweep capture.

The measurement program's sweeps are Novak-synchronized by construction
(:func:`jasper.audio_measurement.sweep.synchronized_sweep_metadata` rounds ``L``
so ``L·f1`` is an integer), which puts the order-``N`` harmonic image at exactly
``L·ln(N)`` seconds AHEAD of the linear impulse response in every
deconvolution. :mod:`jasper.audio_measurement.deconv` already owns the
primitives that window one image and re-map it onto the excitation-frequency
axis; :func:`jasper.audio_measurement.analysis.thd_curve` already owns the
ratio. This module is the composition of those kernels into a per-sweep
read-out, plus the one piece of arithmetic neither of them carries: **how much
pre-guard the deconvolution window needs so the images exist at all.**

Why that arithmetic matters (the trap)
--------------------------------------
:data:`jasper.audio_measurement.program_analysis.DECONV_PRE_GUARD_S` is 0.25 s.
That is the right value for its job — it only has to contain the linear IR's
non-causal shoulder plus the first driver's acoustic delay. It is far SMALLER
than the harmonic advances at measurement sweep rates (a 4 s / 150 Hz MEASURE
woofer sweep has ``L ≈ 1.55 s``, so H2 leads by ``≈ 1.07 s`` and H3 by
``≈ 1.70 s``). Deconvolution is circular, so at the production window every
harmonic image wraps off the front of the array. Analysis on this path
therefore re-deconvolves the SAME capture bytes at a pre-guard derived from the
sweep's own ``L`` — :func:`required_pre_guard_s`. Nothing about the production
window changes; this is an analysis-side parameter, and
``_deconvolve_window`` already takes it as one.

What a returned dB means
------------------------
``relative_db[order]`` is the harmonic's level MINUS the fundamental's level at
the same EXCITATION frequency — i.e. the conventional "HD2 is 46 dB below the
fundamental" reading, negative for a well-behaved driver. The absolute
magnitudes it is built from are deconvolution units (the mic's electrical
scale), so they are meaningful only as a difference; the drive level that
produced them is carried alongside in :class:`DriveLevel` rather than left for
the reader to assume. Distortion is a function of level, and a distortion
number with no level attached names nothing.

Microphone calibration is applied to each curve at **its own acoustic
frequency**: the fundamental at ``f``, the order-``N`` harmonic at ``N·f``. The
ratio is NOT cal-invariant — it carries an error of ``C(N·f) − C(f)``, which is
only negligible where the curve is flat across an octave — so a supplied curve
is used properly rather than argued away.

Bounded memory: one capture, one segment, one deconvolution at a time. The
dominant term is the FFT inside
:func:`~jasper.audio_measurement.deconv.regularized_deconvolution_full`, whose
size is set by the window this module asks for — the same order of magnitude as
the production window it mirrors.
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
# program actually schedules. `program.MESM_MAX_HARMONIC_ORDER` is 3 and sizes
# those gaps; asking for a 4th order here would read an image the schedule
# never guaranteed room for. Deliberately NOT imported from `program`: that
# constant sizes the STIMULUS, this one bounds the ANALYSIS, and a future
# program that widens its gaps should not silently widen this read-out.
DEFAULT_HARMONIC_ORDERS: tuple[int, ...] = (2, 3)

# Extra pre-guard beyond the predicted window, covering
# `extract_harmonic_ir`'s ±2 ms local-peak search: a center refined EARLIER
# than predicted drags the window's leading edge with it. The true worst case
# is 0.6× this (the shortened half-width gives some back); the full radius is
# the honest bound and costs ~96 samples.
PRE_GUARD_SEARCH_MARGIN_S = deconv.HARMONIC_PEAK_SEARCH_RADIUS_S

# Smoothing applied to every magnitude curve before the ratio. Distortion
# residues are noise-dominated between the harmonic's own peaks, and an
# unsmoothed ratio is a grass field rather than a reading. 1/12-octave is the
# conventional distortion-plot resolution — coarse enough to read, fine enough
# to keep a resonance-driven peak from being averaged into its neighbours.
DEFAULT_SMOOTHING_FRACTION = 12

# How close a harmonic may sit to the measured floor before the reading stops
# being about the driver. 6 dB is one doubling of amplitude: at that separation
# the floor contributes ~25% of the measured power, so the harmonic is still
# the dominant term but no longer cleanly so. Points inside this margin are
# reported, never silently dropped — the caller is told they are floor-limited.
FLOOR_LIMITED_MARGIN_DB = 6.0

# Shrink applied to the largest phantom window that clears both neighbouring
# harmonic images, so a sub-sample rounding cannot push its edge into one.
_PHANTOM_WINDOW_SAFETY = 0.9

# Trimmed off the BOTTOM of every order's band. The sweep's fade-in and the
# deconvolution's own band-edge shoulder put a spike right at f1 that no
# nonlinearity produced: on a provably linear synthetic path the artefact sits
# 25.8 dB above the measured floor at f1 and stays above floor+6 dB for 0.192
# octaves (woofer H2, 150-4000 Hz sweep). 0.25 octaves covers that with margin.
#
# The TOP of the band is deliberately NOT trimmed, and the asymmetry is the
# measurement rather than an oversight: an exponential sweep dwells far longer
# per octave at the bottom, so the fade-in smears across a wide frequency span
# while the fade-out barely moves. The same linear path shows no top-edge
# excursion above the floor at all, and
# `test_a_linear_path_reads_as_floor_not_as_distortion` asserts both halves so a
# top-edge artefact could not appear unnoticed.
BAND_EDGE_TRIM_OCTAVES = 0.25


@dataclass(frozen=True, slots=True)
class DriveLevel:
    """The level a distortion reading was taken at, in every reference it has.

    Distortion is a function of drive, so a curve without this is unreadable.
    Each field names its own reference rather than sharing a vague "level":

    ``stimulus_peak_dbfs`` is the sweep's digital peak on the program channel
    (``ProgramSegment.gain_db``); ``effective_peak_dbfs`` folds the declared
    downstream gain in (``ProgramSegment.effective_peak_dbfs``). Both are
    dBFS re digital full scale, and both describe what was PLAYED.

    ``capture_peak_dbfs`` / ``capture_rms_dbfs`` describe what was RECORDED,
    dBFS re the capture device's full scale, measured over the scheduled sweep
    window. They are not SPL: this path has no acoustic reference. Anything
    that does — a session's SPL estimate, a sidecar's declared level — belongs
    in ``notes`` verbatim, so a reader can see whose claim it is.
    """

    stimulus_peak_dbfs: float
    effective_peak_dbfs: float
    capture_peak_dbfs: float
    capture_rms_dbfs: float
    notes: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class HarmonicReading:
    """One sweep segment's harmonic-distortion read-out.

    ``freqs_hz`` is the EXCITATION-frequency grid, masked to ``band_hz``.
    ``relative_db[order]`` is that order's level below the fundamental, at the
    same excitation frequency (negative for a clean driver). ``thd_percent``
    is the root-sum-square of the requested orders over the fundamental,
    in percent.

    ``floor_relative_db[order]`` is the measured noise floor in the SAME units
    — what a phantom window between the images reads, so a point where
    ``relative_db`` approaches it is describing the instrument, not the driver.

    ``pre_guard_s`` is what the deconvolution actually got, and
    ``preceding_silence_s`` how much program silence sat in front of the sweep.
    ``clearance_s = preceding_silence_s − required_pre_guard_s`` is negative
    when the H-``N`` window reaches back into the previous segment's audio: the
    read is still returned (the Hann taper is near zero at that edge) but it is
    no longer clean, and the caller is told rather than left to assume.
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

        True means "this point describes the measurement, not the driver" — the
        harmonic is real only as an upper bound there. Points past the order's
        own band edge are NaN and count as True: a comparison against NaN is
        False in numpy, which would otherwise mark the one region that is
        certainly NOT a driver reading as the cleanest part of the curve.
        """
        values = np.asarray(self.relative_db[order], dtype=np.float64)
        floor = np.asarray(self.floor_relative_db[order], dtype=np.float64)
        separation = values - floor
        return ~(separation >= float(margin_db))

    def worst(
        self, order: int, *, above_floor: bool = True
    ) -> tuple[float, float]:
        """``(frequency_hz, relative_db)`` of this order's worst (highest) point.

        The single number a reader wants first: where the driver is dirtiest,
        and by how much. With ``above_floor`` (the default) only points clear of
        the measured floor are eligible, so the answer cannot be a noise spike;
        an order that never clears its floor returns ``(nan, nan)``, which is
        the honest reading of "nothing measurable here".
        """
        values = np.asarray(self.relative_db[order], dtype=np.float64)
        eligible = np.isfinite(values)
        if above_floor:
            eligible &= ~self.floor_limited(order)
        if not np.any(eligible):
            return float("nan"), float("nan")
        candidates = np.where(eligible, values, -np.inf)
        index = int(np.argmax(candidates))
        return float(self.freqs_hz[index]), float(values[index])


def validated_orders(orders: Sequence[int]) -> tuple[int, ...]:
    """The requested orders, or a refusal naming what is wrong with them.

    One owner for the rule, because both entry points enforce it and a caller
    that hit the pre-guard sizing first used to get an error about phantom
    windows rather than about its own argument.
    """
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

    Its rule is :data:`~.deconv.HARMONIC_WINDOW_GAP_FRACTION` of the distance to
    the NEAREST neighbouring order's centre, and for every ``N ≥ 1`` that
    neighbour is ``N+1`` — ``ln((N+1)/N) < ln(N/(N−1))`` for all ``N ≥ 2``, and
    order 1 has no lower neighbour at all.
    """
    return (
        deconv.HARMONIC_WINDOW_GAP_FRACTION
        * meta.L
        * math.log((order + 1) / order)
    )


def _phantom_window_s(meta: SweepMeta, order: int) -> tuple[float, float]:
    """``(centre_advance_s, half_width_s)`` of order ``order``'s phantom window.

    Centred at ``L·ln(order − ½)`` — the gap BELOW image ``order``, between it
    and image ``order−1`` — and widened until it just clears both of their
    windows.

    **Why the lower gap and not the upper one.** The floor is not stationary in
    time: the deconvolution's own artefacts (band-limit shoulders, the
    regularization residue, room decay smeared by the inversion) are strongest
    near the direct arrival and fall away from it. A phantom placed FURTHER from
    the arrival than the image it describes therefore under-reports that image's
    floor, and a floor that is too low turns artefact into "distortion". Measured
    on a perfectly linear synthetic path, the upper-gap placement left H2's pure
    artefact sitting more than 6 dB "clear" of its own floor — i.e. it would have
    reported distortion where there was provably none. The lower gap sits nearer
    the arrival than the image, so its artefact level is an OVER-estimate, and
    the resulting reading errs toward refusing to claim distortion. That is the
    bias a first measurement should carry.

    The one owner of this geometry: :func:`required_pre_guard_s` sizes the
    deconvolution from it and :func:`_phantom_floor_ir` cuts the window with it,
    because a window the pre-guard did not plan for is a window that wrapped.
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

    The order-``N`` image sits ``L·ln(N)`` ahead of the linear IR
    (:func:`~jasper.audio_measurement.deconv.harmonic_time_advance_s`) and is
    windowed to ``±`` :func:`_image_half_width_s`, so its leading edge sits at
    ``L·ln(N) + half_width`` before the direct arrival. The phantom floor window
    for each order sits FURTHER back still (between images ``N`` and ``N+1``),
    and is included here because a floor that wrapped is worse than no floor.
    :data:`PRE_GUARD_SEARCH_MARGIN_S` covers the local-peak search.

    The fundamental (order 1) is included unconditionally: it is windowed by the
    same rule and extends backwards too, so a pre-guard that ignored it could
    still fail. Returns the maximum over every window.
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

    The upper edge is the one that surprises. It is NOT Nyquist — it is the
    DECONVOLUTION'S OWN PASSBAND. The inversion divides by ``|X(f)|² + ε``, and
    the sweep puts no energy above ``f2``, so every captured component above
    ``f2`` is annihilated by the regularizer along with the noise it is there to
    suppress. The order-``N`` product of excitation ``f`` lands at ``N·f``, so it
    survives the inversion only while ``N·f ≤ f2``.

    Consequences worth naming, because both show up in the shipped programs:
    a woofer swept 150-4000 Hz yields honest H2 to 2000 Hz and H3 to 1333 Hz;
    a tweeter swept 1600-20000 Hz yields H2 to 10 kHz and H3 to 6667 Hz. Above
    those edges the images collapse into the regularization floor, which is
    exactly what the corpus shows and what a naive Nyquist bound would have
    reported as a preternaturally clean driver.

    ``SweepMeta`` already forbids ``f2 ≥ nyquist``, so this bound is always the
    binding one and Nyquist never needs its own term.

    The lower edge carries :data:`BAND_EDGE_TRIM_OCTAVES` of trim above ``f1``,
    where the sweep's fade-in produces an excursion that is not distortion.
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
    """The band over which EVERY requested order is real — ``[f1, f2/max]``.

    The all-orders band, which is what a summed figure such as THD needs: a
    total that silently dropped an order above its own edge would read as a
    falling distortion curve. Per-order curves reach further and use
    :func:`order_band_hz` instead.
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

    Calibration is applied at the ACOUSTIC frequency ``order · f`` — the
    frequency the microphone actually saw — and only then is the axis divided
    down to the excitation grid. Smoothing runs on the curve's own linear grid,
    which is what :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`
    requires, and before the ratio, so both sides of the ratio are smoothed
    identically.
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
    """A window where no harmonic image can be — the read's own noise floor.

    Returns ``(windowed_ir, length_ratio)``. The window is centred at the
    advance ``L·ln(order + ½)``, strictly between image ``order`` and image
    ``order+1``, and widened until it just clears BOTH of their windows (times
    :data:`_PHANTOM_WINDOW_SAFETY`). What it contains is therefore everything
    the deconvolution puts at a harmonic-like time offset EXCEPT a harmonic:
    capture noise, the Tikhonov regularization residue, and room decay
    smeared by the inversion. That is the floor the reading is up against.

    It is necessarily NARROWER than the image window it describes (the images
    are packed against each other), so its noise POWER is lower for the same
    density. ``length_ratio`` is ``image_half_width / phantom_half_width``; the
    caller adds ``10·log10(length_ratio)`` to bring the floor onto the image's
    scale. **That correction assumes the floor is spectrally flat across the
    window** — true for capture noise, approximate for the regularization
    residue, so the resulting floor is an estimate rather than a bound. It is
    reported as such and never used to modify a harmonic value.
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
    deconvolution whose pre-guard was at least :func:`required_pre_guard_s` —
    otherwise the images wrapped and
    :func:`~jasper.audio_measurement.deconv.extract_harmonic_ir` raises rather
    than returning a window of the wrong thing.

    ``band_hz`` defaults to :func:`analysis_band_hz` and is intersected with it
    when supplied, so a caller cannot widen the read past where the orders are
    real. ``pre_guard_s`` / ``preceding_silence_s`` are recorded, not used: the
    caller that windowed the capture is the only one that knows them, and a
    reading that could not state them would hide its own cleanliness.
    """
    orders = validated_orders(orders)

    # The reported GRID reaches as far as the LOWEST order does — H2 stays real
    # a half-octave past H3 and there is no reason to throw that away — while
    # each order is masked to its own edge below, and the summed THD to the
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
    all_orders_hi = min(band[1], order_band_hz(meta, max(orders))[1])

    # Every window shares ONE FFT length, derived from the widest of them (the
    # fundamental's). Per-window lengths would give each order a different bin
    # density, which is harmless for the deterministic image content but makes
    # the phantom floor's length-ratio correction meaningless — the floor and
    # the image it describes have to be estimated on the same grid.
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
    # nyquist/max(orders), so this interpolation never extrapolates inside the
    # band. Same grid transfer `thd_curve` performs, so the per-order curves
    # below and the THD it returns describe the same numbers.
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
    # and placed back on the shared grid — never extended past it with a total
    # that quietly lost a term.
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
    PCM, so ``L`` here is the ``L`` that was played. Raises for a segment that
    carries no sweep bounds.
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
    """The program sample rate, imported lazily to keep `program` off the
    import path of callers that only need the IR-level math."""
    from .program import PROGRAM_SAMPLE_RATE_HZ

    return int(PROGRAM_SAMPLE_RATE_HZ)


def preceding_silence_s(
    program: "ExcitationProgram", segment: "ProgramSegment"
) -> float:
    """Seconds of scheduled silence immediately before ``segment`` starts.

    The gap between the end of the last AUDIBLE segment and this segment's
    start — read off the schedule, never assumed from a default, because the
    MESM gaps are sized by the PRECEDING sweep's ``L`` while the harmonic
    windows of the FOLLOWING sweep are sized by its own. Those differ, and on
    the shipped MEASURE program the difference decides whether a woofer
    repeat's H3 window is clean.

    A segment with nothing audible before it returns its own start time.
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

    The recorded side is measured over the SCHEDULED sweep window (the same
    anchor the deconvolution uses), so it describes the response this reading
    was taken from. A window that falls outside the capture yields ``-inf``
    rather than raising: an empty level is a fact about the capture, and the
    deconvolution is what fails a truncated recording.
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
    scheduled (``global_offset + segment.start_sample``) — the caller locates
    the program, exactly as the production analyzer does, and hands the result
    in. ``epsilon`` is the measured clock drift, divided out of the reference
    before inversion so a drifted capture does not smear the images.

    This is the ONLY function here that windows a capture, and it does so
    through :func:`~jasper.audio_measurement.program_analysis._deconvolve_window`
    with a larger ``pre_guard_s`` — the production window's own parameter, at an
    analysis-side value. Production behaviour is untouched.
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
    # Compared in SAMPLES against the window's own rounding of the same
    # request, not in seconds: `_deconvolve_window` takes `int(round(...))`, so
    # a seconds-domain comparison fails by one sample's worth of float on a
    # window that clamped nothing. The only thing that makes these differ is
    # the head clamp — a capture that starts too close to the sweep — and that
    # is exactly the condition worth refusing, because the images then wrapped.
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
    # Searching the whole IR (rather than trusting `pre_effective`) keeps the
    # acoustic delay out of the caller's hands; the images are then found
    # relative to wherever it actually landed.
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
