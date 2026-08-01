# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the DSP emitted, as a curve — and how far it is from what was claimed.

**Stage: DIAGNOSE.** Pure numpy. Given the raw bytes a render produced, this
module turns one branch's channel into a magnitude curve; given two such curves
(the arm carrying the linearization and the arm carrying none) it produces the
realized transfer, the frame between it and the fit's claim, the error
statistics, and a verdict. No subprocess, no filesystem writes, no config
knowledge, no orchestration.

**Units.** Frequencies in hertz; every level, error, and budget in decibels;
sample amplitudes are linear full-scale (``1.0`` = 0 dBFS). Channel indices are
zero-based frame positions in the interleaved playback stream.

The A/B, and what it buys
-------------------------
``realized_delta_db = treated_magnitude_db − control_magnitude_db``. The two
renders come from configs that differ **only** in the linearization filters, so
the crossover, the delay, the per-driver gain, the split mixer, the ``--gain``
fader, and the stimulus are identical in both and cancel exactly. Nothing in
that list has to be modelled to grade the filters under test, and the fidelity
of anything shared — including a stand-in renderer in a test — cannot move the
answer. What survives the difference is the linearization stage's realized
transfer, and the claim it is graded against is the fit's own
:func:`jasper.active_speaker.linearization_fit.complex_correction_response`.

The soft clip is always on, and it is accounted for
---------------------------------------------------
CamillaDSP's ``Limiter`` with ``soft_clip: true`` is not a threshold that
engages above ``clip_limit``; it is a memoryless cubic applied to every sample
(``src/filters/limiter.rs``: ``s = v/clip; s -= s³/6.75; v = s·clip``). It
therefore compresses *both* arms of the A/B slightly, and by slightly different
amounts wherever the linearization changes the level — a systematic error that
would otherwise sit inside the residual with nothing naming it. For a sinusoid
of amplitude ``a·clip`` the fundamental comes out at ``a·(1 − a²/9)``, so the
compression is :func:`soft_clip_fundamental_gain_db`, and
:func:`soft_clip_error_bound_db` bounds what it can contribute to the
difference. The loop refuses a render pair whose bound exceeds
:data:`SOFT_CLIP_BUDGET_DB` rather than reporting a residual it cannot attribute.

On the verdict's sensitivity — read this before trusting a ``matched``
----------------------------------------------------------------------
The verdict comes from :func:`jasper.active_speaker.delta_probe.
classify_delta_probe`, unchanged and unretuned. That classifier's tolerances
(1.5 dB below 10 kHz, 2.5 dB above) are calibrated for a **microphone**: they
sit below the 1.70 dB shelf-Q defect they exist to catch, but above the repeat
spread and per-serial calibration uncertainty of a real capture. An offline
render has none of that uncertainty, so those tolerances are generous here by
one to two orders of magnitude, and ``matched`` means only "no defect of the
size the room-side probe would catch".

That is deliberate: a second, tighter, offline-only threshold would be a
parallel classifier, and the two would eventually disagree about what a model
error is. The number to read instead is
:attr:`BranchComparison.band_max_error_db` — the plain worst-case disagreement
in dB across the analysis band, reported on every branch whatever the verdict
says, with an exact render's own floor as the yardstick for what "small" is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from jasper.active_speaker.delta_probe import DeltaProbeMap, classify_delta_probe
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

#: The only render output precision this bench decodes. The deployed
#: ``linux_aarch64`` CamillaDSP v4.1.3 release is built without the ``32bit``
#: Cargo feature, so ``PrcFmt = f64`` and the ``File`` playback device writes
#: 8-byte little-endian floats
#: (:data:`jasper.bass_extension.bench.render.DEPLOYED_PROCESSING_PRECISION`).
#: Anything else refuses rather than being decoded at a guessed width.
DECODABLE_PRECISION = "F64_LE"
_PRECISION_BYTES = 8
_PRECISION_DTYPE = "<f8"

#: How far inside the sweep's own band the analysis band starts and ends.
#:
#: One sixth of an octave — the finest fractional-octave window this flow uses
#: anywhere (``linearization_fit._ladder_smooth``'s bottom rung). Regularized
#: deconvolution is only meaningful where the stimulus put energy, and the
#: transition at each sweep edge is set by the generator's 5 ms fade plus the
#: sweep rate. Backing off by a full smoothing window is the same margin the
#: rest of the flow already treats as "one resolution element", rather than a
#: new number invented here.
BAND_EDGE_GUARD_OCTAVES: float = 1.0 / 6.0

#: How much of the IR BEFORE the direct arrival the analysis window keeps, ms.
#:
#: Far wider than the deconvolution module's own 5 ms default, and that width is
#: load-bearing rather than cautious. The regularized inversion applies a
#: ZERO-PHASE weighting ``|X|²/(|X|² + ε)``, whose sharp edges at the sweep's own
#: band limits ring symmetrically about the arrival on a ~1/f1 timescale — tens
#: of milliseconds for a 20 Hz sweep start. Truncating that pre-ring puts a step
#: at the window's leading edge, and the rectangular window's leakage from that
#: step sets a floor across the whole spectrum: measured on this bench's own
#: exact-render case, a 5 ms pre-window floored a branch's stopband at about
#: −83 dB where its true response continues past −148 dB, and the error on an
#: exact render rose from 0.005 dB to 0.06 dB. At 250 ms the recovered curve
#: tracks the true response to −147 dB. The stimulus lead-in
#: (:data:`jasper.active_speaker.bench.loop.STIMULUS_LEAD_IN_S`) exists to give
#: this window somewhere to sit; it must stay wider than this.
ARRIVAL_PRE_MS: float = 250.0

#: How far below a branch's own in-band peak the comparison stops trusting it.
#:
#: A branch's stopband is not a measurement: 60 dB down, the render's remaining
#: content is numerical residue, and a linearization filter's claim there cannot
#: be confirmed or denied. Excluding those bins is the caller-owed "bins the
#: comparison trusts" that :mod:`jasper.audio_measurement.frame_fit` and
#: :func:`jasper.active_speaker.delta_probe.classify_delta_probe` both expect
#: someone to have resolved.
#:
#: 60 dB is deliberately generous — far outside any driver's radiating band, so
#: it can never clip the region a fit designs filters in — and the choice is not
#: load-bearing: on this bench's own cases, sweeping it from 30 dB to 80 dB moved
#: the exact-render error between 0.003 and 0.013 dB and moved the shelf-Q
#: defect's 1.705 dB not at all.
VALIDITY_FLOOR_DB: float = 60.0

#: Most the always-on soft clip may contribute to an A/B difference, dB.
#:
#: 0.05 dB, which is 34x below the 1.70 dB realization defect this bench exists
#: to catch and 30x below :data:`jasper.active_speaker.delta_probe.
#: DELTA_PROBE_TOLERANCE_LOW_DB`. Small enough that a residual at this scale
#: cannot be mistaken for a finding, and large enough that an ordinary stimulus
#: level clears it with an order of magnitude to spare (see
#: :data:`jasper.active_speaker.bench.loop.OFFLINE_STIMULUS_PEAK_DBFS`, which is
#: chosen against this budget). This is a REFUSAL bound, not a tolerance: a pair
#: over it is not graded, because its residual could not be attributed.
SOFT_CLIP_BUDGET_DB: float = 0.05

#: The pinned build's soft-clip cube factor (``1/6.75``), from
#: ``src/filters/limiter.rs``. Named here because the fundamental-gain formula
#: below is derived from it, not from the general soft-clip concept.
_SOFT_CLIP_CUBEFACTOR = 1.0 / 6.75


class EmitComparisonError(ValueError):
    """A rendered stream could not be read, or a pair could not be compared."""


# --------------------------------------------------------------------------- #
# reading a render
# --------------------------------------------------------------------------- #


def decode_render_channel(
    raw_path: Path, *, channel_index: int, channel_count: int, precision: str
) -> np.ndarray:
    """One branch's samples, float64, out of an interleaved raw render output.

    Thin adapter over :func:`jasper.bass_extension.bench.render.extract_channel`
    — that function owns the de-interleave and its frame-boundary refusal; this
    one owns only the byte width and the dtype, and refuses any precision it
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

    ``Limiter::apply_soft_clip`` maps ``v`` to ``clip·(s − s³/6.75)`` with
    ``s = v/clip``. For ``v(t) = a·clip·sin(θ)``, ``sin³θ = (3·sinθ − sin3θ)/4``,
    so the fundamental leaves at ``a·clip·(1 − a²/9)`` — a gain of
    ``20·log10(1 − a²/9)``, always ≤ 0 and monotone decreasing in ``a``.

    Returns 0.0 for a silent branch. Amplitudes past the transform's own clamp
    (``|s| > 1.5``) are outside the cubic's domain and are reported as ``-inf``
    — a caller must refuse there, not interpolate. Inside that domain the
    argument to the logarithm is at least 0.75, so no floor is needed on it.
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

    Each arm's compression lies in ``[g(peak), 0]`` and both are ≤ 0, so their
    difference cannot exceed the larger magnitude. Using each arm's PEAK rather
    than its per-frequency amplitude makes the bound conservative at every
    frequency, which is the direction a refusal bound must err in.
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

    Unwindowed on purpose: the window has to be shared between the two arms of
    an A/B (see :func:`shared_arrival_window`), and
    :func:`~jasper.audio_measurement.deconv.deconvolve` would pick a different
    one per arm from its own ``argmax`` — the linearization biquads shift the
    peak, so the two arms would be truncated differently and the difference
    would carry that instead of the filters.

    The module's default capture cap is left in place — it is a real FFT-memory
    guard on the Pi, and disabling it would only trade that protection for
    nothing. It would truncate silently if it ever fired, so the caller refuses
    a stimulus long enough to reach it rather than letting it (see
    :data:`jasper.active_speaker.bench.loop.MAX_STIMULUS_SECONDS`).
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

    Derived from the CONTROL arm — the render without the filters under test —
    so the subject of the measurement never chooses its own window. Windowing
    at all is what discards the synchronized sweep's harmonic images, which a
    circular full IR carries wrapped to the end of the buffer; the default
    500 ms post-arrival span is orders of magnitude longer than a digital
    filter chain's own impulse, so it truncates none of the response under test.
    The pre-arrival span is widened to :data:`ARRIVAL_PRE_MS` — see that
    constant for the leakage floor the module's 5 ms default would otherwise
    impose.
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

    ``normalize=False`` is load-bearing: normalizing subtracts each curve's own
    peak, which would destroy the very level relationship the A/B measures.
    ``n_fft`` is a parameter rather than derived per-arm so both arms land on
    one grid by construction.
    """

    try:
        ir = apply_arrival_window(np.asarray(full_ir, dtype=np.float64), window)
    except ValueError as exc:
        raise EmitComparisonError(f"arrival window could not be applied: {exc}") from exc
    return magnitude_response(ir, int(sample_rate), n_fft=int(n_fft), normalize=False)


def magnitude_fft_length(window: tuple[int, int]) -> int:
    """The FFT length both arms share — the module's one grid decision.

    Mirrors :func:`~jasper.audio_measurement.deconv.magnitude_response`'s own
    default rule (next power of two at or above the windowed length, floored at
    8192) so the grid is the flow's usual one; it is computed here, once, from
    the SHARED window rather than per-arm.
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

    "Audible" here means within ``floor_db`` of the CONTROL arm's own in-band
    peak. The control arm is the right yardstick because it is the branch
    without the filters under test — using the treated arm would let a filter
    that cut a region deeply exclude the very bins it is being graded on.

    Returns an all-``False`` mask when nothing in ``band_hz`` is finite, which
    callers must treat as "not measured" rather than "measured, and empty".
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

    Args:
      role: the driver role this branch belongs to.
      band_hz: the sweep's usable band (:func:`analysis_band_hz`) — the outer
        bound the validity mask was applied inside, NOT the bin set the
        statistics were taken over and NOT the verdict's own probe band.
      valid_band_hz: the lowest and highest frequency that survived
        :func:`branch_validity_mask`. ``None`` when nothing did.
      validity_floor_db: the floor that mask used, disclosed so a reader can
        see how much of the sweep the branch was trusted over.
      n_bins: how many grid bins survived the validity mask.
      band_max_error_db, band_rms_error_db: worst-case and rms
        ``|realized − claimed|`` across the valid bins, after removing
        ``expected_offset_db``. **The headline numbers.** They differ from
        :attr:`verdict`'s own ``max_error_db``/``rms_error_db``, which are taken
        over the narrower set of bins where the correction commands at least
        ``DELTA_PROBE_MIN_COMMANDED_DB`` — a different, deliberately narrower
        question ("where something was asked for, was it delivered?").
      band_worst_hz: where ``band_max_error_db`` occurred.
      expected_offset_db: the level move the EMITTER knows it made between the
        two arms — the difference in their program headroom gains, which a
        linearization carrying boosts causes and which the claimed curve does
        not contain. Removed from the realized curve before anything above was
        computed, and threaded to the classifier as its own
        ``expected_offset_db``, so both views account for it once and identically.
      frame: the fitted ``(offset, tilt)`` between the two curves and the grade
        pair either side of removing it. Evidence, never a re-grade: the
        verdict below is decided on the raw curves.
      verdict: :func:`jasper.active_speaker.delta_probe.classify_delta_probe`'s
        map, in that module's vocabulary. See this module's docstring on what
        its tolerances do and do not mean offline.
      soft_clip_bound_db: the bound on what the always-on soft clip could have
        contributed to this branch's difference. Disclosed on every branch so a
        small residual can be read against the noise floor of its own instrument.
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
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

    All four arrays share one grid. ``treated_db`` and ``control_db`` are the
    two arms' magnitudes from :func:`windowed_magnitude_db`; their difference is
    the realized transfer. ``claimed_db`` is
    ``20·log10(|complex_correction_response(filters, freqs)|)`` on the same grid.
    Taking the two arms rather than a pre-differenced curve is what lets this
    function own the validity decision — that decision needs the control arm's
    own level, and splitting it across two owners is how a caller ends up
    grading a stopband.

    Three readings come out, and they answer three different questions:

    1. ``band_*`` — how far apart the two curves are across the trusted bins,
       once the emitter's own known level move is removed. This is the
       measurement.
    2. ``frame`` — how much of that gap is one offset and one tilt. This is
       evidence about the *shape* of the disagreement; it never re-grades
       anything (:mod:`jasper.audio_measurement.frame_fit`'s own rule).
    3. ``verdict`` — the classification, decided on the raw curves by the same
       classifier the room-side probe uses. Bins outside the validity mask are
       handed to it as ``NaN``, which is that classifier's own established way
       of saying "not a measurement here": they leave the probe band, and they
       break a run in the exceedance-width rule, which is exactly right — a bin
       nothing can be seen at cannot corroborate a defect across it.

    Raises:
      EmitComparisonError: on a grid mismatch. That is a caller bug, not a
        measurement outcome, so it is loud rather than an ``unavailable``.
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
