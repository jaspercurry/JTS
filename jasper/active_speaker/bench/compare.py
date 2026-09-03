# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the DSP emitted, as a curve — and how far it is from what was claimed.

Stage: DIAGNOSE. Pure numpy: turns one branch's raw render channel into a magnitude
curve, and given a treated/control pair produces the realized transfer, the frame
against the fit's claim, error statistics, and a verdict. No subprocess, filesystem
writes, config knowledge, or orchestration.

Units: frequencies in hertz; level/error/budget in decibels; sample amplitudes linear
full-scale (``1.0`` = 0 dBFS); channel indices are zero-based frame positions in the
interleaved stream.

The A/B: ``realized_delta_db = treated_magnitude_db - control_magnitude_db``. The two
renders differ ONLY in the linearization filters, so crossover, delay, per-driver gain,
split mixer, ``--gain`` fader and stimulus cancel exactly -- nothing shared needs
modelling, and its fidelity cannot move the answer. The claim graded against is
:func:`jasper.active_speaker.linearization_fit.complex_correction_response`.

The soft clip is always on and accounted for: CamillaDSP's ``Limiter`` with ``soft_clip:
true`` is a memoryless cubic on every sample (``src/filters/limiter.rs``: ``s = v/clip;
s -= s^3/6.75; v = s*clip``), compressing both A/B arms by slightly different amounts
wherever linearization changes level. For amplitude ``a*clip`` the fundamental is ``a*(1
- a^2/9)``; :func:`soft_clip_fundamental_gain_db` computes it and
:func:`soft_clip_error_bound_db` bounds its contribution. The loop refuses a pair whose
bound exceeds :data:`SOFT_CLIP_BUDGET_DB` rather than report an unattributable residual.

On the verdict's sensitivity:
:func:`jasper.active_speaker.delta_probe.classify_delta_probe` is unchanged and
unretuned, calibrated for a MICROPHONE (1.5 dB below 10 kHz, 2.5 dB above -- below the
1.70 dB defect it catches, above real capture repeat spread). An offline render has none
of that uncertainty, so ``matched`` here means only "no defect the room-side probe would
catch" -- generous by one to two orders of magnitude, deliberately not re-tightened into
a second parallel classifier. Read :attr:`BranchComparison.band_max_error_db` instead:
the plain worst-case disagreement across the analysis band, reported whatever the
verdict says.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.delta_probe import (
    VERDICT_UNAVAILABLE,
    DeltaProbeMap,
    classify_delta_probe,
)
from jasper.audio_measurement.deconv import (
    DEFAULT_POST_ARRIVAL_MS,
    apply_arrival_window,
    direct_arrival_window,
    magnitude_response,
    regularized_deconvolution_full,
)
from jasper.audio_measurement.frame_fit import FrameComparison, fit_frame
from jasper.audio_measurement.sweep import SweepMeta
from jasper.bass_extension.bench.render import RenderError, extract_channel

#: The only render output precision this bench decodes: the deployed
#: ``linux_aarch64`` CamillaDSP v4.1.3 (built without the ``32bit`` Cargo
#: feature) writes 8-byte little-endian floats
#: (:data:`jasper.bass_extension.bench.render.DEPLOYED_PROCESSING_PRECISION`).
#: Anything else refuses rather than decoding at a guessed width.
DECODABLE_PRECISION = "F64_LE"
_PRECISION_BYTES = 8
_PRECISION_DTYPE = "<f8"

#: How far inside the sweep's own band the analysis band starts and ends:
#: one sixth of an octave, the finest fractional-octave window this flow
#: uses anywhere (``linearization_fit._ladder_smooth``'s bottom rung) --
#: backed off by one resolution element from each sweep edge's fade/rate
#: transition.
BAND_EDGE_GUARD_OCTAVES: float = 1.0 / 6.0

#: How much of the IR BEFORE the direct arrival the analysis window keeps,
#: ms. Far wider than the deconvolution module's 5 ms default, and
#: load-bearing: the regularized inversion's zero-phase weighting rings
#: symmetrically about the arrival on a ~1/f1 timescale (tens of ms for a
#: 20 Hz sweep start), and a 5 ms pre-window measured a floor at -83 dB
#: where the true response continues past -148 dB (error 0.005 -> 0.06 dB).
#: At 250 ms the recovered curve tracks to -147 dB. The stimulus lead-in
#: (:data:`jasper.active_speaker.bench.loop.STIMULUS_LEAD_IN_S`) must stay
#: wider than this.
ARRIVAL_PRE_MS: float = 250.0

#: How far below a branch's own in-band peak the comparison stops trusting
#: it: 60 dB down, the render's remaining content is numerical residue, so
#: a filter's claim there cannot be confirmed or denied. Deliberately
#: generous (outside any driver's radiating band); not load-bearing --
#: sweeping 30-80 dB moved the exact-render error 0.003-0.013 dB and the
#: shelf-Q defect's 1.705 dB not at all.
VALIDITY_FLOOR_DB: float = 60.0

#: Most the always-on soft clip may contribute to an A/B difference, dB:
#: 34x below the 1.70 dB realization defect this bench exists to catch,
#: 30x below :data:`jasper.active_speaker.delta_probe.DELTA_PROBE_TOLERANCE_LOW_DB`.
#: A REFUSAL bound, not a tolerance: a pair over it is not graded, its
#: residual unattributable.
SOFT_CLIP_BUDGET_DB: float = 0.05

#: The pinned build's soft-clip cube factor (``1/6.75``), from
#: ``src/filters/limiter.rs``; the fundamental-gain formula below is
#: derived from it.
_SOFT_CLIP_CUBEFACTOR = 1.0 / 6.75


class EmitComparisonError(ValueError):
    """A rendered stream could not be read, or a pair could not be compared."""


# --------------------------------------------------------------------------- #
# reading a render
# --------------------------------------------------------------------------- #


def decode_render_channel(
    raw_path: Path, *, channel_index: int, channel_count: int, precision: str
) -> np.ndarray:
    """One branch's samples, float64, out of an interleaved raw render output. Thin adapter
    over :func:`jasper.bass_extension.bench.render.extract_channel` (which owns the
    de-interleave); this owns only the byte width and dtype, refusing any precision it
    cannot decode exactly.
    """

    if precision != DECODABLE_PRECISION:
        raise EmitComparisonError(
            f"render precision {precision!r} is not {DECODABLE_PRECISION} — "
            "refusing rather than decoding at a guessed sample width"
        )
    try:
        payload = extract_channel(
            raw_path,
            channel_index=channel_index,
            channel_count=channel_count,
            bytes_per_sample=_PRECISION_BYTES,
        )
    except RenderError as exc:
        raise EmitComparisonError(str(exc)) from exc
    return np.frombuffer(payload, dtype=_PRECISION_DTYPE).astype(np.float64)


def analysis_band_hz(meta: SweepMeta) -> tuple[float, float]:
    """The band a render of ``meta``'s sweep can be graded over.

    The sweep's own ``[f1, f2]``, backed off by
    :data:`BAND_EDGE_GUARD_OCTAVES` at each end.
    """

    guard = 2.0**BAND_EDGE_GUARD_OCTAVES
    lo = float(meta.f1) * guard
    hi = float(meta.f2) / guard
    if not (lo < hi):
        raise EmitComparisonError(
            f"sweep band [{meta.f1}, {meta.f2}] Hz is too narrow to grade"
        )
    return lo, hi


# --------------------------------------------------------------------------- #
# the always-on soft clip
# --------------------------------------------------------------------------- #


def soft_clip_fundamental_gain_db(peak_linear: float, clip_limit_dbfs: float) -> float:
    """The pinned soft clip's fundamental-frequency gain, dB, at ``peak_linear``.

    ``Limiter::apply_soft_clip`` maps ``v`` to ``clip*(s - s^3/6.75)`` with ``s =
    v/clip``; for ``v(t) = a*clip*sin(theta)`` the fundamental leaves at ``a*clip*(1 -
    a^2/9)``, a gain of ``20*log10(1 - a^2/9)``, always <= 0 and monotone decreasing in
    ``a``. Returns 0.0 for silence. Amplitudes past the transform's own clamp (``|s| >
    1.5``) return ``-inf`` -- a caller must refuse there, not interpolate.
    """

    if clip_limit_dbfs > 0.0 or not math.isfinite(clip_limit_dbfs):
        raise EmitComparisonError("clip_limit must be a finite value at or below 0 dBFS")
    clip_linear = 10.0 ** (clip_limit_dbfs / 20.0)
    a = abs(float(peak_linear)) / clip_linear
    if a <= 0.0:
        return 0.0
    if a > 1.5:
        return float("-inf")
    return 20.0 * math.log10(1.0 - (a * a) * (3.0 / 4.0) * _SOFT_CLIP_CUBEFACTOR)


def soft_clip_error_bound_db(
    treated_peak_linear: float,
    control_peak_linear: float,
    *,
    clip_limit_dbfs: float,
) -> float:
    """Upper bound, dB, on what the soft clip can contribute to the A/B.

    Each arm's compression lies in ``[g(peak), 0]``, both <= 0, so the
    difference cannot exceed the larger magnitude; using PEAK rather than
    per-frequency amplitude is the conservative direction. Two opposite
    effects: reading the peak off the RENDER (the limiter's OUTPUT) makes
    the bound slightly OPTIMISTIC on that axis; but the factor used is the
    output-waveform PEAK one (``f(a) = a*(1 - a^2/6.75)``, the cubic at the
    sine's crest), not the fundamental-gain ``g`` (``1 - a^2/9``), and at
    :data:`SOFT_CLIP_BUDGET_DB` (``a = 0.22729``) that makes the bound
    1.53% low (0.000765 dB on a 0.05 dB budget; 0.148% at the bench's
    default stimulus level) -- pinned in
    ``tests/test_active_speaker_emit_bench_compare.py`` against
    :func:`jasper.bass_extension.bench.render.reference_soft_clip`.
    Disclosed rather than corrected: recovering the pre-clip peak would buy
    back eight ten-thousandths of a dB on a bound separating 0.05 dB from
    1.7 dB -- false precision, and the per-frequency-vs-peak conservatism
    above is larger than this in every real case.
    """

    treated = soft_clip_fundamental_gain_db(treated_peak_linear, clip_limit_dbfs)
    control = soft_clip_fundamental_gain_db(control_peak_linear, clip_limit_dbfs)
    if not (math.isfinite(treated) and math.isfinite(control)):
        return float("inf")
    return max(abs(treated), abs(control))


# --------------------------------------------------------------------------- #
# a render → a magnitude curve
# --------------------------------------------------------------------------- #


def deconvolved_ir(
    samples: np.ndarray, sweep: np.ndarray, sample_rate: int
) -> np.ndarray:
    """The full, unwindowed regularized impulse response of one rendered branch.

    Unwindowed on purpose: the window must be shared between the two A/B arms
    (:func:`shared_arrival_window`); per-arm ``argmax`` windowing would truncate
    differently since linearization biquads shift the peak, letting the difference carry
    that instead of the filters. The module's default capture cap (a real FFT-memory
    guard on the Pi) is left in place; the caller refuses a stimulus long enough to
    reach it rather than let it truncate silently
    (:data:`jasper.active_speaker.bench.loop.MAX_STIMULUS_SECONDS`).
    """

    try:
        return regularized_deconvolution_full(
            np.asarray(samples, dtype=np.float64),
            np.asarray(sweep, dtype=np.float64),
            int(sample_rate),
        )
    except ValueError as exc:
        raise EmitComparisonError(f"deconvolution refused: {exc}") from exc


def shared_arrival_window(
    reference_ir: np.ndarray, sample_rate: int
) -> tuple[int, int]:
    """The one arrival window BOTH arms of an A/B are truncated with.

    Derived from the CONTROL arm, so the subject of the measurement never chooses its
    own window. Windowing discards the synchronized sweep's harmonic images (wrapped to
    the buffer end in a circular full IR); the default 500 ms post-arrival span is
    orders of magnitude longer than any digital filter chain's impulse. Pre-arrival span
    is widened to :data:`ARRIVAL_PRE_MS` (see that constant for the leakage floor the 5
    ms default would impose).
    """

    try:
        return direct_arrival_window(
            np.asarray(reference_ir, dtype=np.float64),
            int(sample_rate),
            pre_arrival_ms=ARRIVAL_PRE_MS,
            post_arrival_ms=DEFAULT_POST_ARRIVAL_MS,
        )
    except ValueError as exc:
        raise EmitComparisonError(f"arrival window refused: {exc}") from exc


def windowed_magnitude_db(
    full_ir: np.ndarray,
    window: tuple[int, int],
    sample_rate: int,
    *,
    n_fft: int,
) -> tuple[np.ndarray, np.ndarray]:
    """``(freqs_hz, magnitude_db)`` for one arm, on a caller-fixed FFT length.
    ``normalize=False`` is load-bearing (normalizing would destroy the level
    relationship the A/B measures); ``n_fft`` is a parameter, not derived per-arm, so
    both arms land on one grid by construction.
    """

    try:
        ir = apply_arrival_window(np.asarray(full_ir, dtype=np.float64), window)
    except ValueError as exc:
        raise EmitComparisonError(f"arrival window could not be applied: {exc}") from exc
    return magnitude_response(ir, int(sample_rate), n_fft=int(n_fft), normalize=False)


def magnitude_fft_length(window: tuple[int, int]) -> int:
    """The FFT length both arms share -- the module's one grid decision. Mirrors
    :func:`~jasper.audio_measurement.deconv.magnitude_response`'s own default rule (next
    power of two at or above the windowed length, floored at 8192), computed once from
    the SHARED window.
    """

    length = max(1, int(window[1]) - int(window[0]))
    return max(8192, 1 << (length - 1).bit_length())


# --------------------------------------------------------------------------- #
# the comparison
# --------------------------------------------------------------------------- #


def branch_validity_mask(
    freqs_hz: np.ndarray,
    control_db: np.ndarray,
    *,
    band_hz: tuple[float, float],
    floor_db: float = VALIDITY_FLOOR_DB,
) -> np.ndarray:
    """The bins this branch's comparison trusts: in the sweep band, and audible.

    "Audible" means within ``floor_db`` of the CONTROL arm's own in-band peak -- the
    control is the right yardstick since using the treated arm would let a filter that
    cut a region deeply exclude the very bins it is graded on. Returns an all-``False``
    mask when nothing in ``band_hz`` is finite; callers must treat that as "not
    measured", not "measured, and empty".
    """

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    control = np.asarray(control_db, dtype=np.float64)
    in_band = (
        (freqs >= float(band_hz[0]))
        & (freqs <= float(band_hz[1]))
        & np.isfinite(control)
    )
    if not in_band.any():
        return np.zeros_like(freqs, dtype=bool)
    peak_db = float(np.max(control[in_band]))
    return in_band & (control >= peak_db - float(floor_db))


@dataclass(frozen=True)
class BranchComparison:
    """One branch's emitted-vs-claimed result: statistics, frame, and verdict.

    ``band_hz`` is the sweep's usable band (:func:`analysis_band_hz`), the outer bound
    the validity mask applied inside -- NOT the bin set statistics were taken over, NOT
    the verdict's own probe band. ``valid_band_hz`` is the lowest/highest frequency
    surviving :func:`branch_validity_mask` (``None`` if nothing did);
    ``validity_floor_db`` is the floor that mask used.

    ``band_max_error_db``/``band_rms_error_db`` (the HEADLINE numbers) are
    worst-case/rms ``|realized - claimed|`` across valid bins after removing
    ``expected_offset_db`` -- distinct from :attr:`verdict`'s own error fields, taken
    over the narrower set of bins where the correction commands at least
    ``DELTA_PROBE_MIN_COMMANDED_DB``. ``expected_offset_db`` is the level move the
    EMITTER knows it made between the two arms (program headroom gain difference),
    removed once and threaded identically to the classifier. ``frame`` is evidence about
    the shape of the disagreement, never a re-grade. ``soft_clip_bound_db`` is disclosed
    on every branch so a small residual can be read against its own instrument's noise
    floor.
    """

    role: str
    band_hz: tuple[float, float]
    valid_band_hz: tuple[float, float] | None
    validity_floor_db: float
    n_bins: int
    band_max_error_db: float
    band_rms_error_db: float
    band_worst_hz: float
    expected_offset_db: float
    frame: FrameComparison
    verdict: DeltaProbeMap
    soft_clip_bound_db: float

    @property
    def matched(self) -> bool:
        return self.verdict.matched

    @property
    def measured(self) -> bool:
        """Whether the instrument saw this branch at all: any bin survived the validity mask.
        Distinguishes the two situations both classifying as ``unavailable``: measured
        but nothing to verify (``measured`` True, :attr:`graded` False, benign) from the
        instrument seeing nothing (``measured`` False, a real problem).
        """

        return self.n_bins > 0

    @property
    def graded(self) -> bool:
        """Whether a verdict was reached -- ``unavailable`` means it was not.
        :mod:`jasper.active_speaker.delta_probe`'s own doctrine: not a pass, no evidence
        to refuse on either. A caller must not fold it into either side.
        """

        return self.verdict.verdict != VERDICT_UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "measured": self.measured,
            "graded": self.graded,
            "band_hz": list(self.band_hz),
            "valid_band_hz": (
                list(self.valid_band_hz) if self.valid_band_hz is not None else None
            ),
            "validity_floor_db": self.validity_floor_db,
            "n_bins": self.n_bins,
            "band_max_error_db": self.band_max_error_db,
            "band_rms_error_db": self.band_rms_error_db,
            "band_worst_hz": self.band_worst_hz,
            "expected_offset_db": self.expected_offset_db,
            "soft_clip_bound_db": self.soft_clip_bound_db,
            "frame": self.frame.to_dict(),
            "verdict": self.verdict.to_dict(),
        }


def compare_branch(
    freqs_hz: np.ndarray,
    treated_db: np.ndarray,
    control_db: np.ndarray,
    claimed_db: np.ndarray,
    *,
    role: str,
    band_hz: tuple[float, float],
    expected_offset_db: float = 0.0,
    soft_clip_bound_db: float = 0.0,
    validity_floor_db: float = VALIDITY_FLOOR_DB,
) -> BranchComparison:
    """Grade one branch's realized transfer against what the fit claimed.

    All four arrays share one grid. ``treated_db``/``control_db`` are the two arms'
    magnitudes from :func:`windowed_magnitude_db` (their difference is the realized
    transfer); ``claimed_db`` is ``20*log10(|complex_correction_response(filters,
    freqs)|)`` on the same grid. Taking the two arms, not a pre-differenced curve, lets
    this function own the validity decision (which needs the control arm's own level).

    Three readings answer three questions: ``band_*`` is the measurement (how far apart
    the curves are across trusted bins, known level move removed); ``frame`` is evidence
    about the SHAPE of the disagreement, never a re-grade; ``verdict`` is the
    classification from the same classifier the room-side probe uses, with bins outside
    the validity mask handed to it as ``NaN`` (that classifier's own way of saying "not
    a measurement here").

    Raises :class:`EmitComparisonError` on a grid mismatch -- a caller bug, not a
    measurement outcome, so it is loud rather than ``unavailable``.
    """

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    treated = np.asarray(treated_db, dtype=np.float64)
    control = np.asarray(control_db, dtype=np.float64)
    claimed = np.asarray(claimed_db, dtype=np.float64)
    if (
        not (freqs.shape == treated.shape == control.shape == claimed.shape)
        or freqs.ndim != 1
    ):
        raise EmitComparisonError(
            "freqs, treated, control, and claimed must be 1-D arrays of one shape"
        )

    offset = float(expected_offset_db)
    if not math.isfinite(offset):
        offset = 0.0
    realized = treated - control
    corrected = realized - offset

    lo_hz, hi_hz = float(band_hz[0]), float(band_hz[1])
    mask = (
        branch_validity_mask(
            freqs, control, band_hz=(lo_hz, hi_hz), floor_db=validity_floor_db
        )
        & np.isfinite(corrected)
        & np.isfinite(claimed)
    )
    n_bins = int(mask.sum())
    verdict = classify_delta_probe(
        freqs,
        np.where(mask, realized, np.nan),
        np.where(mask, claimed, np.nan),
        band_hz=(lo_hz, hi_hz),
        expected_offset_db=offset,
    )
    if n_bins == 0:
        return BranchComparison(
            role=str(role),
            band_hz=(lo_hz, hi_hz),
            valid_band_hz=None,
            validity_floor_db=float(validity_floor_db),
            n_bins=0,
            band_max_error_db=float("nan"),
            band_rms_error_db=float("nan"),
            band_worst_hz=float("nan"),
            expected_offset_db=offset,
            frame=FrameComparison(fit=fit_frame(freqs[:0], corrected[:0], claimed[:0])),
            verdict=verdict,
            soft_clip_bound_db=float(soft_clip_bound_db),
        )

    band_freqs = freqs[mask]
    error = corrected[mask] - claimed[mask]
    band_max_error_db = float(np.max(np.abs(error)))
    band_rms_error_db = float(np.sqrt(np.mean(error**2)))
    band_worst_hz = float(band_freqs[int(np.argmax(np.abs(error)))])

    fit = fit_frame(band_freqs, corrected[mask], claimed[mask])
    frame_removed = error - fit.frame_db(band_freqs)
    frame = FrameComparison(
        fit=fit,
        raw_rms_db=band_rms_error_db,
        raw_max_db=band_max_error_db,
        tilt_removed_rms_db=(
            float(np.sqrt(np.mean(frame_removed**2))) if fit.fitted else None
        ),
        tilt_removed_max_db=(
            float(np.max(np.abs(frame_removed))) if fit.fitted else None
        ),
    )
    return BranchComparison(
        role=str(role),
        band_hz=(lo_hz, hi_hz),
        valid_band_hz=(float(band_freqs[0]), float(band_freqs[-1])),
        validity_floor_db=float(validity_floor_db),
        n_bins=n_bins,
        band_max_error_db=band_max_error_db,
        band_rms_error_db=band_rms_error_db,
        band_worst_hz=band_worst_hz,
        expected_offset_db=offset,
        frame=frame,
        verdict=verdict,
        soft_clip_bound_db=float(soft_clip_bound_db),
    )


__all__ = [
    "ARRIVAL_PRE_MS",
    "BAND_EDGE_GUARD_OCTAVES",
    "DECODABLE_PRECISION",
    "SOFT_CLIP_BUDGET_DB",
    "VALIDITY_FLOOR_DB",
    "BranchComparison",
    "EmitComparisonError",
    "analysis_band_hz",
    "branch_validity_mask",
    "compare_branch",
    "decode_render_channel",
    "deconvolved_ir",
    "magnitude_fft_length",
    "shared_arrival_window",
    "soft_clip_error_bound_db",
    "soft_clip_fundamental_gain_db",
    "windowed_magnitude_db",
]
