# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spatial multi-capture combiner + interference honesty screen (flat
linearization, fundamentals 1-2).

``combine_positions(captures) -> CombinedResponse`` is the pure-DSP core of
docs/flat-linearization-plan.md's first two fundamentals: **spatial
multi-capture is THE measurement** (N gated sweeps at a cloud of mic
positions, combined as a power average) and the **interference honesty
screen** (power-mean-vs-median disagreement flags interference-dominated
bins; a per-capture cepstral detector stamps tau/strength diagnostics).

Why this module exists: a tau ~= 0.3 ms early boundary bounce *cannot be
time-gated* — it arrives glued to the direct sound, and a gate short enough
to exclude it destroys all resolution below ~3 kHz (plan, "Evidence" §1).
Gating handles late wall reflections; only spatial diversity handles early
boundary interference. Every shipped consumer system averages the bounce
away rather than removing it (research artifact 01, Question 1), so this
module implements the industry-proven estimator and nothing more.

Pipeline (:func:`combine_positions`):

1. **Canonical grid** — every capture is resampled (``np.interp``) onto one
   shared *linear* grid: the coarsest bin spacing among the captures, over
   their common frequency support. Linear because
   :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`
   smooths in linear power over binary-searched linear bins and would
   silently mis-window a log grid. Coarsest-spacing because interpolating
   onto a *finer* grid than the source manufactures resolution the
   measurement never had. In the ordinary case (all captures from one
   program, so one ``rfftfreq`` grid) this is the identity.
2. **Estimators** — per-bin ``power_mean_db`` (10·log10 of the mean of
   linear power) is the primary direct-sound estimator; per-bin
   ``median_db`` (median in the dB domain) is the robustness cross-check.
   Research artifact 01 Question 2: power mean is the proven combiner,
   median is the sanity cross-check, max-hold is positively biased
   (rejected) and complex averaging needs phase coherence a hand-moved mic
   cannot give.
3. **Smoothing** — 1/6-octave diagnostic and 1/3-octave spec curves of the
   power mean (the plan evaluates pass/fail at 1/3-oct and retains 1/6-oct
   for diagnostics), plus 1/6-octave of the median for the screen.
4. **Exclusion mask** — bins where the two 1/6-oct curves disagree by more
   than ``flag_threshold_db`` are interference-dominated. Flagged bins are
   excluded from correction *and* pass/fail by downstream consumers, and
   reported. Exposed both per-bin and as merged ``(f_lo, f_hi)`` intervals.
5. **Echo diagnostics** — :func:`detect_echo` per capture that supplied an
   impulse response, plus the derived :attr:`CombinedResponse.geometry_locked`
   flag.
6. **Spread diagnostics** — cross-position sigma in octave bands, the
   observable behind the research's 1/sqrt(N) accuracy story.

**Two blind spots, two instruments — read this before trusting either.**
The mean-vs-median screen and ``geometry_locked`` are complementary, and
neither alone is sufficient:

* The screen fires on *partially* aligned interference, where some
  positions are nulled at a bin and others are not, so the null-filling
  power mean and the outlier-rejecting median diverge.
* The screen is **blind to fully-aligned nulls**. When every position sees
  the same null, mean and median agree perfectly and the null passes the
  screen untouched — while surviving the average at full depth. This is
  not a defect of the screen; it is the physics of a position-stable
  interference pattern, and it is exactly what ``geometry_locked``
  reports. A consumer seeing ``geometry_locked`` must tell the user to
  spread the mic further, because no amount of averaging over *these*
  positions can fill those nulls.
* Symmetrically, the screen is silent when decorrelation *succeeded* —
  for uniformly-distributed comb phase the power mean and the dB median
  coincide analytically (both land on 1+r² in linear power), so "no
  flags" is the healthy outcome, not a broken screen.

Detection only: there is **no echo removal anywhere in this module**, by
plan guardrail ("No cepstral or parametric echo *removal* in production").
Research artifact 01 Question 3 is explicit that cepstral/homomorphic
removal is academic-only and fails exactly on this shape — a
directivity-weighted (frequency-dependent) r, 2·tau/3·tau repeats, and
consumer SNR. The cepstrum is used to *detect and flag*, never to invert.

Pure computation: numpy plus
:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`. No
I/O, no logging, no globals, no randomness, no product policy — and, per
the house precedent set by
:mod:`jasper.active_speaker.linearization_envelope` (#1668 PR-B), **zero
production callers by design**. Wiring the combiner into the conductor's
position-group choreography is plan stage S1; the spec bands and gauges
that consume the exclusion mask are stage S2.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave

# --------------------------------------------------------------------------- #
# Combiner tuning
# --------------------------------------------------------------------------- #

# Power-mean vs median disagreement above this many dB flags a bin as
# interference-dominated. 2.0 dB is the plan's fundamental 2 threshold,
# itself from research artifact 01's recommendation 3 ("flag frequencies
# where the two disagree by >2 dB as interference-dominated and exclude
# them from correction and pass/fail").
DEFAULT_FLAG_THRESHOLD_DB = 2.0

# 1/6-octave for diagnostics, 1/3-octave for the spec — the plan's "spec"
# section: "Pass/fail is evaluated at 1/3-oct smoothing (1/6-oct retained
# for diagnostics)". The screen runs at the diagnostic fraction because a
# 1/3-oct window is wide enough to smear a narrow interference null into
# its neighbours before the comparison happens.
DEFAULT_DIAG_FRACTION = 6
DEFAULT_SPEC_FRACTION = 3

# Octave-band centres for the cross-position spread diagnostic (ISO
# preferred centres). Deliberately octave rather than 1/3-octave: this is a
# legible ~10-number diagnostic answering "how much do positions disagree
# in this part of the spectrum", the observable behind the research's
# 1/sqrt(N) accuracy story — not a curve. A band needs at least
# MIN_BAND_BINS grid bins inside the shared support to be reported.
OCTAVE_BAND_CENTERS_HZ: tuple[float, ...] = (
    31.5,
    63.0,
    125.0,
    250.0,
    500.0,
    1000.0,
    2000.0,
    4000.0,
    8000.0,
    16000.0,
)
MIN_BAND_BINS = 4

# A capture grid must be uniformly spaced to this relative tolerance.
# smooth_fractional_octave binary-searches linear bins; handing it a log
# grid produces a silently wrong window width at every frequency, so this
# is a hard contract, checked rather than trusted.
GRID_UNIFORMITY_RTOL = 1e-3

# --------------------------------------------------------------------------- #
# Echo-detector tuning
#
# Every threshold below was calibrated on three populations: the 2026-07-24/25
# JTS3 corpus (captures/flat-linearization-20260725/cdhorn-live-session, runs 5
# and 7 VERIFY + run 7 tweeter-alone), synthetic impulse+echo pairs across
# tau in 200-700 us and r in 0.15-0.6, and negative controls (white noise,
# impulse+noise with no echo, clean impulse with no echo). The measured
# separation is reported in tests/test_spatial_combine.py.
# --------------------------------------------------------------------------- #

DEFAULT_ECHO_BAND_HZ = (5000.0, 19000.0)
DEFAULT_ECHO_SEARCH_US = (120.0, 800.0)

# Order of the polynomial detrend removed from the band's log-magnitude
# before the cepstral transform, so the driver's own broad shape does not
# leak into low quefrencies. Matches the reference implementation
# (captures/flat-linearization-20260725/comb_forensics3.py, `cepstral_tau`).
# Fitted against a [-1, 1] abscissa rather than a raw bin index (better
# conditioned; mathematically equivalent).
DETREND_ORDER = 3

# The direct arrival must stand at least this far above the IR's median
# |sample| level, else the IR has no identifiable direct arrival and "echo
# level re main arrival" is undefined — confidence is 0 and no tau is
# claimed. Measured: real corpus 93-112 dB, synthetic impulse+echo 83 dB,
# impulse buried in noise 37 dB, white noise 16 dB.
ARRIVAL_CREST_FLOOR_DB = 20.0

# Analysis window around the located direct arrival, as a multiple of the
# search window's upper edge (plus a short pre-arrival lead). Scoping the
# cepstrum to the early-arrival region is what the detector is *for* —
# early boundary interference — and it keeps room decay out of the
# statistic. At the 800 us default this spans ~3.2 ms, comfortably inside
# the ~7 ms first-reflection gate of the JTS3 room, so the window sees the
# bounce but not the walls.
ECHO_WINDOW_SPAN_FACTOR = 4.0
ECHO_WINDOW_PRE_S = 0.0005

# Minimum number of frequency bins in the analysis band. Below this the
# detrend + cepstrum are not meaningful and the detector reports no
# confidence rather than a number.
MIN_ECHO_BAND_BINS = 16

# Cepstral concentration — the search-window peak divided by the L2 norm of
# the cepstrum above the search floor, so it is bounded in [0, 1] and scale
# free (unlike a peak-to-median ratio, which is numerically unstable once
# windowing drives the background toward zero). Maps linearly to a
# confidence factor between these two knots. Measured: real corpus
# 0.63-0.65, synthetic echoes 0.69-0.81, negative controls 0.23-0.50.
CONCENTRATION_LO = 0.30
CONCENTRATION_HI = 0.70

# Corroboration — relative disagreement between the two independent tau
# estimators (cepstral peak and analytic-envelope secondary arrival). Full
# credit at or below TIGHT, zero at or above LOOSE. Measured: real corpus
# 1.7-3.1%, synthetic echoes 0.2-9.1%, negative controls 4.4-73% (a lone
# estimator can find a peak in noise; two independent ones rarely agree).
CORROBORATION_TIGHT = 0.05
CORROBORATION_LOOSE = 0.30

# Reported strength when no secondary arrival was found. Mirrors
# program_analysis.DBFS_FLOOR's role: a finite floor, not -inf, so the
# field stays arithmetic-safe. Meaningless unless confidence > 0.
STRENGTH_FLOOR_DB = -120.0

# --------------------------------------------------------------------------- #
# Geometry-lock tuning
# --------------------------------------------------------------------------- #

# An echo diagnostic counts toward the geometry verdict only at or above
# this confidence. Calibrated to sit in the empty gap between the measured
# populations: true positives scored >= 0.83, every negative control 0.00.
ECHO_CONFIDENCE_FLOOR = 0.5

# The speaker's interference pattern is "geometry locked" when at least
# this fraction of confident per-position tau estimates fall within
# +-GEOMETRY_CLUSTER_TOLERANCE of their median. Position-stable tau means
# position-stable nulls, which spatial averaging cannot fill — the cloud is
# not spread enough (or the bounce is speaker-fixed diffraction rather than
# a boundary, the plan's S0 prediction 5).
GEOMETRY_CLUSTER_FRACTION = 0.70
GEOMETRY_CLUSTER_TOLERANCE = 0.15

# A "cluster" needs at least two members to mean anything: with a single
# confident estimate, 100% of estimates trivially sit within any tolerance
# of their own median. Below this the verdict is "unknown", reported as not
# locked with an explicit reason.
GEOMETRY_MIN_CONFIDENT = 2

# Geometry verdict reason vocabulary. Snake_case, self-identifying values,
# mirroring the house style of linearization_envelope.ReasonCode.
GEOMETRY_LOCKED = "geometry_locked"
GEOMETRY_DISPERSED = "geometry_dispersed"
GEOMETRY_UNKNOWN = "geometry_insufficient_confident_estimates"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionCapture:
    """One gated sweep captured at one mic position in the cloud.

    Args:
      position_id: caller's label for the position (a conductor
        position-group id, in the eventual S1 flow). Carried through to
        :attr:`CombinedResponse.position_ids` so a flagged or outlying
        position can be named back to the user.
      freqs_hz: **linear**-spaced frequency grid, e.g. ``np.fft.rfftfreq``.
        Uniformity is enforced (see ``GRID_UNIFORMITY_RTOL``) because the
        smoothing kernel this module builds on assumes it.
      magnitude_db: matching magnitude in dB — calibrated, reflection-gated,
        and *unsmoothed*. High resolution is the point: smoothing is applied
        once, after combining, so a per-capture pre-smooth would blur the
        very interference nulls the screen exists to find.
      sample_rate: capture rate in Hz, used only by the echo detector.
      ir: optional time-domain impulse response for :func:`detect_echo`. May
        be the full deconvolved IR; the detector windows it to the
        early-arrival region itself. When ``None`` the position contributes
        no echo diagnostic (reported as ``None``, distinct from "measured
        and found nothing").
    """

    position_id: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    sample_rate: int
    ir: np.ndarray | None = None


@dataclass(frozen=True)
class EchoDiagnostic:
    """Discrete-echo detection for one capture. Detection only — nothing in
    this module removes an echo (plan guardrail).

    ``tau_us`` and ``strength_db`` are meaningless when ``confidence`` is
    0.0; check confidence first. The supporting fields exist so a verdict is
    auditable rather than a magic number.

    Args:
      tau_us: delay of the secondary arrival, microseconds. The
        analytic-envelope estimate when one was found (sample resolution
        plus parabolic refinement, ~1-3% accurate on the calibration set),
        else the cepstral peak (quantised to the band-width-determined
        quefrency step, ~71 us for the default band).
      strength_db: secondary-arrival level relative to the direct arrival,
        from the band-limited analytic envelope. ``STRENGTH_FLOOR_DB`` when
        no secondary arrival was found.
      confidence: 0.0-1.0 credibility, the product of three independent
        factors — an arrival-crest gate, cepstral concentration, and
        agreement between the two tau estimators. 0.0 means no credible
        peak.
      tau_cepstral_us: the cepstral estimator's answer, in isolation.
      tau_envelope_us: the envelope estimator's answer, in isolation (0.0
        when no secondary arrival was found in the search window).
      concentration: bounded [0, 1] cepstral peak concentration.
      corroboration: relative disagreement between the two tau estimators
        (0.0 = identical). 1.0 when the envelope found nothing.
      arrival_crest_db: direct arrival level above the IR's median
        ``|sample|`` level — the "is there an arrival at all" gate.
    """

    tau_us: float
    strength_db: float
    confidence: float
    tau_cepstral_us: float
    tau_envelope_us: float
    concentration: float
    corroboration: float
    arrival_crest_db: float


@dataclass(frozen=True)
class GeometryLock:
    """Whether the cloud's interference pattern is position-stable.

    ``locked`` True is the actionable case: the nulls do not move between
    positions, so spatial averaging cannot fill them and the user needs to
    spread the mic further. It is *not* a measurement failure — on a corpus
    captured repeatedly from one place it is the detector working.

    Args:
      locked: the verdict.
      reason: one of ``GEOMETRY_LOCKED`` / ``GEOMETRY_DISPERSED`` /
        ``GEOMETRY_UNKNOWN``.
      n_confident: how many positions produced an echo diagnostic at or
        above ``confidence_floor``.
      n_positions: how many captures were combined.
      median_tau_us: median tau over the confident set (0.0 when none).
      clustered_fraction: fraction of the confident set within
        ``tolerance`` of ``median_tau_us``.
      tolerance: relative clustering tolerance actually applied.
      confidence_floor: echo confidence required to count.
    """

    locked: bool
    reason: str
    n_confident: int
    n_positions: int
    median_tau_us: float
    clustered_fraction: float
    tolerance: float
    confidence_floor: float


@dataclass(frozen=True)
class BandSpread:
    """Cross-position magnitude spread in one octave band.

    Computed on the *1/6-octave-smoothed* per-position curves, so it
    measures genuine position-to-position level disagreement rather than
    per-bin measurement noise or individual comb nulls — the quantity the
    research's ``1/sqrt(N)`` accuracy law is about. ``sigma`` is the sample
    standard deviation (``ddof=1``), the unbiased estimator of the
    underlying population spread from N positions.

    Args:
      center_hz: nominal ISO octave-band centre.
      f_lo: band lower edge, clipped to the shared grid's support.
      f_hi: band upper edge, clipped to the shared grid's support.
      sigma_db: mean over the band's bins of the per-bin cross-position
        sigma, in dB.
      max_sigma_db: worst single bin's cross-position sigma, in dB. A band
        whose max greatly exceeds its mean is null-dominated at a few
        frequencies rather than broadly noisy.
      n_bins: grid bins the band actually covers.
    """

    center_hz: float
    f_lo: float
    f_hi: float
    sigma_db: float
    max_sigma_db: float
    n_bins: int


@dataclass(frozen=True)
class CombinedResponse:
    """The spatially-averaged direct-sound estimate and its honesty screen.

    Args:
      freqs_hz: the shared canonical linear grid every curve below lives on.
      power_mean_db: per-bin power (energy) mean across positions — the
        primary direct-sound estimator.
      median_db: per-bin median in the dB domain — the robustness
        cross-check. Never the correction input; it exists to disagree.
      power_mean_diag_db: ``power_mean_db`` at the diagnostic fraction.
      power_mean_spec_db: ``power_mean_db`` at the spec fraction — the curve
        the plan's pass/fail tolerance table is evaluated against.
      median_diag_db: ``median_db`` at the diagnostic fraction.
      excluded: per-bin mask, True where the two diagnostic curves disagree
        by more than ``flag_threshold_db``. Downstream: excluded from
        correction *and* from pass/fail, and reported to the user.
      excluded_bands_hz: ``excluded`` as merged ``(f_lo, f_hi)`` intervals,
        one per contiguous run. No gap-bridging is applied — two runs
        separated by a single unflagged bin stay two intervals; a consumer
        wanting coarser reporting should post-process.
      n_positions: number of captures combined.
      position_ids: their ids, in input order.
      per_position_echo: index-aligned with ``position_ids``. ``None`` for a
        capture that supplied no IR — deliberately distinct from a
        zero-confidence :class:`EchoDiagnostic`, which means "measured, and
        found nothing credible".
      geometry_locked: convenience mirror of ``geometry.locked``.
      geometry: the full geometry verdict with its supporting numbers.
      band_spread: per-octave-band cross-position spread. Empty when fewer
        than two positions were supplied (spread is undefined for one).
      flag_threshold_db: the screen threshold actually applied.
      diag_fraction: the diagnostic smoothing fraction actually applied.
      spec_fraction: the spec smoothing fraction actually applied.
    """

    freqs_hz: np.ndarray
    power_mean_db: np.ndarray
    median_db: np.ndarray
    power_mean_diag_db: np.ndarray
    power_mean_spec_db: np.ndarray
    median_diag_db: np.ndarray
    excluded: np.ndarray
    excluded_bands_hz: tuple[tuple[float, float], ...]
    n_positions: int
    position_ids: tuple[str, ...]
    per_position_echo: tuple[EchoDiagnostic | None, ...]
    geometry_locked: bool
    geometry: GeometryLock
    band_spread: tuple[BandSpread, ...]
    flag_threshold_db: float
    diag_fraction: int
    spec_fraction: int


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #


def _as_float_array(values: np.ndarray, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1:
        raise ValueError(f"{name} must be 1-D, got shape {array.shape}")
    if array.size == 0:
        raise ValueError(f"{name} must not be empty")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be finite")
    return array


def _validate_capture(capture: PositionCapture) -> tuple[np.ndarray, np.ndarray]:
    """Shape / finiteness / linear-spacing contract for one capture."""
    freqs = _as_float_array(capture.freqs_hz, f"{capture.position_id}.freqs_hz")
    mags = _as_float_array(capture.magnitude_db, f"{capture.position_id}.magnitude_db")
    if freqs.size != mags.size:
        raise ValueError(
            f"{capture.position_id}: length mismatch — freqs_hz={freqs.size} "
            f"magnitude_db={mags.size}"
        )
    if freqs.size < 2:
        raise ValueError(f"{capture.position_id}: need at least 2 frequency bins")
    steps = np.diff(freqs)
    if np.any(steps <= 0):
        raise ValueError(f"{capture.position_id}: freqs_hz must be strictly increasing")
    if not np.allclose(steps, steps[0], rtol=GRID_UNIFORMITY_RTOL):
        raise ValueError(
            f"{capture.position_id}: freqs_hz must be linear-spaced (e.g. rfftfreq); "
            "smooth_fractional_octave assumes linear bins and would mis-window a "
            "log grid"
        )
    if capture.sample_rate <= 0:
        raise ValueError(
            f"{capture.position_id}: sample_rate must be positive, "
            f"got {capture.sample_rate}"
        )
    return freqs, mags


def _canonical_grid(grids: Sequence[np.ndarray]) -> np.ndarray:
    """The shared linear grid: coarsest spacing, common support.

    Coarsest spacing because interpolating onto a finer grid than the source
    invents resolution the measurement never had; common support because
    ``np.interp`` would otherwise silently flat-extrapolate past a capture's
    band edge. Identity when every capture already shares one grid, which is
    the ordinary case (one program, one ``rfftfreq``).
    """
    f_lo = max(float(g[0]) for g in grids)
    f_hi = min(float(g[-1]) for g in grids)
    step = max(float(g[1] - g[0]) for g in grids)
    if f_hi <= f_lo:
        raise ValueError(
            f"captures share no frequency support (common span {f_lo}-{f_hi} Hz)"
        )
    n_points = int(round((f_hi - f_lo) / step)) + 1
    if n_points < 2:
        raise ValueError(
            f"common frequency support {f_lo}-{f_hi} Hz is narrower than the "
            f"coarsest bin spacing {step} Hz"
        )
    grid = f_lo + step * np.arange(n_points, dtype=float)
    # Float accumulation can push the last point a hair past the common
    # support; clamp rather than let np.interp edge-hold silently.
    grid[-1] = min(grid[-1], f_hi)
    grid.flags.writeable = False
    return grid


def _merge_mask(freqs: np.ndarray, mask: np.ndarray) -> tuple[tuple[float, float], ...]:
    """Contiguous True runs of ``mask`` as ``(f_lo, f_hi)`` intervals."""
    flagged = np.flatnonzero(mask)
    if flagged.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(flagged) > 1)
    starts = np.concatenate(([flagged[0]], flagged[breaks + 1]))
    ends = np.concatenate((flagged[breaks], [flagged[-1]]))
    return tuple(
        (float(freqs[s]), float(freqs[e])) for s, e in zip(starts, ends, strict=True)
    )


def _analytic_envelope(signal: np.ndarray) -> np.ndarray:
    """Magnitude of the analytic signal (Hilbert envelope), numpy-only.

    Zeroes the negative-frequency half and doubles the positive half, the
    textbook Hilbert construction — scipy is not a JTS dependency.
    """
    n = signal.size
    spectrum = np.fft.fft(signal)
    weights = np.zeros(n)
    weights[0] = 1.0
    if n % 2 == 0:
        weights[n // 2] = 1.0
        weights[1 : n // 2] = 2.0
    else:
        weights[1 : (n + 1) // 2] = 2.0
    return np.abs(np.fft.ifft(spectrum * weights))


def _n_fft_for(length: int) -> int:
    """Next power of two at or above ``length``, floored at 4096.

    Mirrors ``program_analysis._n_fft_for``'s shape with a lower floor: this
    module's analysis window is a few milliseconds, not a whole capture.
    """
    return max(4096, 1 << (max(length, 1) - 1).bit_length())


def _bandpass(signal: np.ndarray, lo_hz: float, hi_hz: float, sample_rate: int) -> np.ndarray:
    """Raised-cosine band-limit, matching the reference forensics script's
    ``bp`` (comb_forensics3.py). Zero-phase, so it does not shift arrivals.
    """
    n_fft = _n_fft_for(signal.size)
    spectrum = np.fft.rfft(signal, n_fft)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    window = np.zeros_like(freqs)
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    window[band] = 0.5 - 0.5 * np.cos(2 * np.pi * (freqs[band] - lo_hz) / (hi_hz - lo_hz))
    return np.fft.irfft(spectrum * window, n_fft)[: signal.size]


def _parabolic_offset(values: np.ndarray, index: int) -> float:
    """Sub-bin peak offset from a 3-point parabolic fit, clamped to +-0.5."""
    if index <= 0 or index >= values.size - 1:
        return 0.0
    left, mid, right = (float(values[index - 1]), float(values[index]), float(values[index + 1]))
    denom = left - 2.0 * mid + right
    if denom == 0.0:
        return 0.0
    return float(np.clip(0.5 * (left - right) / denom, -0.5, 0.5))


def _no_echo(arrival_crest_db: float = 0.0) -> EchoDiagnostic:
    """The "nothing credible here" diagnostic."""
    return EchoDiagnostic(
        tau_us=0.0,
        strength_db=STRENGTH_FLOOR_DB,
        confidence=0.0,
        tau_cepstral_us=0.0,
        tau_envelope_us=0.0,
        concentration=0.0,
        corroboration=1.0,
        arrival_crest_db=arrival_crest_db,
    )


# --------------------------------------------------------------------------- #
# Echo detection
# --------------------------------------------------------------------------- #


def detect_echo(
    ir: np.ndarray,
    sample_rate: int,
    *,
    band_hz: tuple[float, float] = DEFAULT_ECHO_BAND_HZ,
    search_us: tuple[float, float] = DEFAULT_ECHO_SEARCH_US,
) -> EchoDiagnostic:
    """Detect a discrete early echo in one impulse response.

    Implements docs/flat-linearization-plan.md fundamental 2's per-capture
    half ("cepstral echo detection stamps tau/r diagnostics") and its
    guardrail: **detection only, no removal, ever**. Research artifact 01
    Question 3 establishes that cepstral removal is academic-only and fails
    precisely on this signal shape; recommendation 4 is to "use only to
    detect tau ... never as the primary corrector".

    Two independent estimators, with a clear division of labour:

    * The **cepstrum** answers *is there a periodic ripple in the band's
      log-magnitude, and at roughly what quefrency* — a comb from a single
      discrete echo maps to one isolated cepstral peak. Its resolution is
      set by the analysis band's width (~71 us for the 5-19 kHz default),
      so it detects well and localises coarsely.
    * The **band-limited analytic envelope** answers *where is the secondary
      arrival and how loud is it* at sample resolution with parabolic
      refinement — it localises well but, alone, will happily find a peak in
      noise.

    ``tau_us`` therefore reports the envelope estimate when one exists, and
    ``confidence`` is what makes it trustworthy: the product of an
    arrival-crest gate (is there a direct arrival at all), cepstral
    concentration (is the ripple energy at one quefrency), and corroboration
    (do the two estimators agree). Requiring all three to line up is what
    separates the real corpus from every negative control — see the
    calibration figures on the module's threshold constants.

    The IR is windowed internally to the early-arrival region (see
    ``ECHO_WINDOW_SPAN_FACTOR``), so a full deconvolved IR may be passed;
    room decay is excluded rather than diluting the statistic.

    Args:
      ir: time-domain impulse response. May be the full deconvolved IR.
      sample_rate: in Hz.
      band_hz: analysis band. The default targets the HF region where a
        directivity-weighted bounce combs most visibly; the upper edge is
        clipped to Nyquist.
      search_us: bounds on the echo delay searched, microseconds. The
        default spans an early boundary bounce (~120 us ≈ 4 cm path delta)
        up to ~800 us (≈ 27 cm), below the room's first wall reflection.

        **Resolution floor — the lower edge is optimistic.** Both estimators
        degrade as tau approaches ``1 / bandwidth`` (~71 us for the 5-19 kHz
        default): the cepstral quefrency step *is* ``1 / bandwidth``, and the
        band-limited envelope cannot separate an arrival from the direct
        pulse's own skirt inside roughly one envelope width. Measured on the
        synthetic set, tau is recovered to ~1-3% above ~240 us but can read
        ~9-14% low near 150-185 us. Delays in the bottom ~2-3 quefrency
        steps of the window should be treated as "an early echo is present"
        rather than as a trustworthy delay. The plan's target bounce
        (~300 us) and the corpus's measured 313-323 us sit comfortably above
        this floor; widening ``band_hz`` is what buys resolution below it.

    Returns:
      An :class:`EchoDiagnostic`. ``confidence == 0.0`` means no credible
      echo was found, and the other estimates carry no information.

    Raises:
      ValueError: on an empty / non-finite IR, a non-positive sample rate,
        or a degenerate band / search window.
    """
    samples = _as_float_array(ir, "ir")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be positive, got {sample_rate}")

    lo_hz, hi_hz = float(band_hz[0]), min(float(band_hz[1]), sample_rate / 2.0)
    if not 0.0 < lo_hz < hi_hz:
        raise ValueError(
            f"band_hz must satisfy 0 < lo < hi <= Nyquist, got {band_hz} at "
            f"sample_rate={sample_rate}"
        )
    search_lo_s = float(search_us[0]) * 1e-6
    search_hi_s = float(search_us[1]) * 1e-6
    if not 0.0 < search_lo_s < search_hi_s:
        raise ValueError(f"search_us must satisfy 0 < lo < hi, got {search_us}")

    # --- 1. Locate the direct arrival, and gate on it existing at all. ---
    # The coarse locate is argmax|ir|, which agrees with the band-limited
    # envelope peak to within a couple of samples on every real and
    # synthetic case measured; it diverges only for signals with no arrival,
    # which the crest gate rejects anyway.
    peak_index = int(np.argmax(np.abs(samples)))
    median_level = float(np.median(np.abs(samples)))
    peak_level = float(np.abs(samples[peak_index]))
    if peak_level <= 0.0:
        raise ValueError("ir is all zeros — no impulse response to analyse")
    crest_db = (
        20.0 * float(np.log10(peak_level / median_level))
        if median_level > 0.0
        else float("inf")
    )
    if crest_db < ARRIVAL_CREST_FLOOR_DB:
        return _no_echo(crest_db)

    # --- 2. Window to the early-arrival region. ---
    pre = int(round(ECHO_WINDOW_PRE_S * sample_rate))
    span = int(round(ECHO_WINDOW_SPAN_FACTOR * search_hi_s * sample_rate))
    start = max(0, peak_index - pre)
    stop = min(samples.size, peak_index + span)
    segment = samples[start:stop]
    if segment.size < 8:
        return _no_echo(crest_db)

    # --- 3. Cepstral estimator. ---
    n_fft = _n_fft_for(segment.size)
    spectrum = np.abs(np.fft.rfft(segment, n_fft)) + 1e-12
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    n_band = int(np.count_nonzero(band))
    if n_band < MIN_ECHO_BAND_BINS:
        return _no_echo(crest_db)

    log_mag = 20.0 * np.log10(spectrum[band])
    # Fit the slow trend against a conditioned [-1, 1] abscissa so the
    # driver's own broad shape does not leak into low quefrencies.
    abscissa = np.linspace(-1.0, 1.0, n_band)
    log_mag = log_mag - np.polyval(np.polyfit(abscissa, log_mag, DETREND_ORDER), abscissa)

    bin_width_hz = float(freqs[band][1] - freqs[band][0])
    cepstrum = np.abs(np.fft.rfft(log_mag * np.hanning(n_band)))
    quefrency = np.fft.rfftfreq(n_band, bin_width_hz)
    above_floor = quefrency >= search_lo_s
    in_search = above_floor & (quefrency <= search_hi_s)
    if not np.any(in_search):
        return _no_echo(crest_db)

    # Peak within the search window, but refined against the FULL cepstrum:
    # a peak sitting on the first or last search bin still has real
    # neighbours just outside the slice, and refining on the slice alone
    # would discard them and rail the estimate onto the boundary bin. The
    # refined value is then clamped back into the caller's window, so
    # refinement can sharpen an edge peak but never report outside the
    # window that was asked for.
    search_indices = np.flatnonzero(in_search)
    peak = int(search_indices[np.argmax(cepstrum[in_search])])
    quefrency_step = float(quefrency[1] - quefrency[0])
    tau_cepstral = float(
        np.clip(
            quefrency[peak] + _parabolic_offset(cepstrum, peak) * quefrency_step,
            search_lo_s,
            search_hi_s,
        )
    )
    baseline = float(np.linalg.norm(cepstrum[above_floor]))
    concentration = float(cepstrum[peak] / baseline) if baseline > 0.0 else 0.0

    # --- 4. Envelope estimator. ---
    envelope = _analytic_envelope(_bandpass(segment, lo_hz, hi_hz, sample_rate))
    main = int(np.argmax(envelope))
    first = main + int(round(search_lo_s * sample_rate))
    last = min(envelope.size - 2, main + int(round(search_hi_s * sample_rate)))
    tau_envelope = 0.0
    strength_db = STRENGTH_FLOOR_DB
    if last > first + 1:
        local = first + int(np.argmax(envelope[first : last + 1]))
        tau_envelope = (local + _parabolic_offset(envelope, local) - main) / sample_rate
        strength_db = 20.0 * float(
            np.log10(max(float(envelope[local]), 1e-15) / max(float(envelope[main]), 1e-15))
        )

    # --- 5. Fuse. ---
    if tau_envelope > 0.0:
        corroboration = abs(tau_envelope - tau_cepstral) / max(tau_cepstral, 1e-12)
        tau = tau_envelope
    else:
        corroboration = 1.0
        tau = tau_cepstral

    concentration_score = float(
        np.clip(
            (concentration - CONCENTRATION_LO) / (CONCENTRATION_HI - CONCENTRATION_LO),
            0.0,
            1.0,
        )
    )
    corroboration_score = float(
        np.clip(
            (CORROBORATION_LOOSE - corroboration)
            / (CORROBORATION_LOOSE - CORROBORATION_TIGHT),
            0.0,
            1.0,
        )
    )
    confidence = concentration_score * corroboration_score
    if confidence <= 0.0:
        return EchoDiagnostic(
            tau_us=0.0,
            strength_db=STRENGTH_FLOOR_DB,
            confidence=0.0,
            tau_cepstral_us=tau_cepstral * 1e6,
            tau_envelope_us=tau_envelope * 1e6,
            concentration=concentration,
            corroboration=float(corroboration),
            arrival_crest_db=crest_db,
        )

    return EchoDiagnostic(
        tau_us=tau * 1e6,
        strength_db=strength_db,
        confidence=confidence,
        tau_cepstral_us=tau_cepstral * 1e6,
        tau_envelope_us=tau_envelope * 1e6,
        concentration=concentration,
        corroboration=float(corroboration),
        arrival_crest_db=crest_db,
    )


# --------------------------------------------------------------------------- #
# Geometry lock
# --------------------------------------------------------------------------- #


def assess_geometry(
    echoes: Sequence[EchoDiagnostic | None],
    *,
    confidence_floor: float = ECHO_CONFIDENCE_FLOOR,
    tolerance: float = GEOMETRY_CLUSTER_TOLERANCE,
    min_fraction: float = GEOMETRY_CLUSTER_FRACTION,
) -> GeometryLock:
    """Are the cloud's interference nulls position-stable?

    ``locked`` when at least ``min_fraction`` of the *confident* per-position
    tau estimates fall within ``tolerance`` (relative) of their median. A
    stable tau means a stable null ladder at ``(n+0.5)/tau``, which spatial
    averaging cannot fill however many positions are added — the honest
    consumer response is to ask the user to spread the mic further, or (the
    plan's S0 prediction 5) to conclude the bounce is speaker-fixed
    diffraction rather than a boundary, in which case the exclusion screen
    carries the weight instead.

    Fewer than ``GEOMETRY_MIN_CONFIDENT`` confident estimates is reported as
    not-locked with reason ``GEOMETRY_UNKNOWN``: a single estimate sits
    within any tolerance of its own median, so "100% clustered" would be a
    vacuous lock.

    Args:
      echoes: per-position diagnostics, ``None`` where no IR was supplied.
      confidence_floor: minimum :attr:`EchoDiagnostic.confidence` to count.
      tolerance: relative clustering tolerance about the median tau.
      min_fraction: fraction of the confident set that must cluster.

    Returns:
      A :class:`GeometryLock` carrying the verdict and its supporting
      numbers.
    """
    n_positions = len(echoes)
    taus = np.array(
        [e.tau_us for e in echoes if e is not None and e.confidence >= confidence_floor],
        dtype=float,
    )
    n_confident = int(taus.size)
    if n_confident < GEOMETRY_MIN_CONFIDENT:
        return GeometryLock(
            locked=False,
            reason=GEOMETRY_UNKNOWN,
            n_confident=n_confident,
            n_positions=n_positions,
            median_tau_us=float(np.median(taus)) if n_confident else 0.0,
            clustered_fraction=0.0,
            tolerance=tolerance,
            confidence_floor=confidence_floor,
        )

    median_tau = float(np.median(taus))
    clustered = float(np.mean(np.abs(taus - median_tau) <= tolerance * median_tau))
    locked = clustered >= min_fraction
    return GeometryLock(
        locked=locked,
        reason=GEOMETRY_LOCKED if locked else GEOMETRY_DISPERSED,
        n_confident=n_confident,
        n_positions=n_positions,
        median_tau_us=median_tau,
        clustered_fraction=clustered,
        tolerance=tolerance,
        confidence_floor=confidence_floor,
    )


# --------------------------------------------------------------------------- #
# Combiner
# --------------------------------------------------------------------------- #


def _band_spread(
    freqs: np.ndarray, smoothed: np.ndarray
) -> tuple[BandSpread, ...]:
    """Octave-band cross-position sigma from stacked per-position curves."""
    if smoothed.shape[0] < 2:
        return ()
    sigma = np.std(smoothed, axis=0, ddof=1)
    grid_lo, grid_hi = float(freqs[0]), float(freqs[-1])
    bands: list[BandSpread] = []
    for center in OCTAVE_BAND_CENTERS_HZ:
        lo = max(center / np.sqrt(2.0), grid_lo)
        hi = min(center * np.sqrt(2.0), grid_hi)
        if hi <= lo:
            continue
        mask = (freqs >= lo) & (freqs <= hi)
        n_bins = int(np.count_nonzero(mask))
        if n_bins < MIN_BAND_BINS:
            continue
        bands.append(
            BandSpread(
                center_hz=float(center),
                f_lo=float(freqs[mask][0]),
                f_hi=float(freqs[mask][-1]),
                sigma_db=float(np.mean(sigma[mask])),
                max_sigma_db=float(np.max(sigma[mask])),
                n_bins=n_bins,
            )
        )
    return tuple(bands)


def combine_positions(
    captures: Sequence[PositionCapture],
    *,
    flag_threshold_db: float = DEFAULT_FLAG_THRESHOLD_DB,
    diag_fraction: int = DEFAULT_DIAG_FRACTION,
    spec_fraction: int = DEFAULT_SPEC_FRACTION,
) -> CombinedResponse:
    """Combine a cloud of position captures into one direct-sound estimate.

    Implements docs/flat-linearization-plan.md fundamentals 1-2. See the
    module docstring for the pipeline and — importantly — for the two
    complementary blind spots of the honesty screen and the geometry flag.

    The **power mean** is the primary estimator (research artifact 01
    Question 2: the proven combiner across every shipped consumer system);
    the **dB median** exists to disagree with it. Where they disagree by
    more than ``flag_threshold_db`` at the diagnostic smoothing fraction,
    the bin is interference-dominated and is reported for exclusion from
    both correction and pass/fail.

    Note the power mean's known, deliberate bias: it *fills* moving nulls,
    which is the desired behaviour here (it pushes the estimate toward the
    true direct response and away from position-specific cancellation), at
    the cost of a systematic ``+10·log10(1 + r²)`` energy offset from the
    echo itself. The plan's spec is evaluated *relative* to a band
    reference, which normalises that offset out; a consumer comparing
    absolute levels must account for it.

    Args:
      captures: one or more :class:`PositionCapture`. Order is preserved in
        ``position_ids`` and ``per_position_echo``.
      flag_threshold_db: mean-vs-median disagreement that flags a bin.
      diag_fraction: diagnostic 1/N-octave fraction (screen + spread).
      spec_fraction: spec 1/N-octave fraction (pass/fail curve).

    Returns:
      A :class:`CombinedResponse`.

    Raises:
      ValueError: on no captures, a malformed capture (see
        :class:`PositionCapture`), captures sharing no frequency support, or
        a non-positive smoothing fraction / threshold.
    """
    if not captures:
        raise ValueError("combine_positions needs at least one capture")
    if flag_threshold_db <= 0.0:
        raise ValueError(f"flag_threshold_db must be positive, got {flag_threshold_db}")
    if diag_fraction <= 0 or spec_fraction <= 0:
        raise ValueError(
            f"smoothing fractions must be positive, got diag={diag_fraction} "
            f"spec={spec_fraction}"
        )

    validated = [_validate_capture(c) for c in captures]
    grid = _canonical_grid([freqs for freqs, _ in validated])

    # Resample every capture onto the shared grid. np.interp does not
    # extrapolate (it edge-holds), and _canonical_grid already confined the
    # grid to the common support, so no capture is asked for a level it
    # never measured.
    stacked = np.vstack(
        [np.interp(grid, freqs, mags) for freqs, mags in validated]
    )

    power_mean_db = 10.0 * np.log10(np.mean(10.0 ** (stacked / 10.0), axis=0))
    median_db = np.median(stacked, axis=0)

    power_mean_diag_db = smooth_fractional_octave(grid, power_mean_db, fraction=diag_fraction)
    power_mean_spec_db = smooth_fractional_octave(grid, power_mean_db, fraction=spec_fraction)
    median_diag_db = smooth_fractional_octave(grid, median_db, fraction=diag_fraction)

    excluded = np.abs(power_mean_diag_db - median_diag_db) > flag_threshold_db
    excluded.flags.writeable = False

    per_position_echo: tuple[EchoDiagnostic | None, ...] = tuple(
        None
        if capture.ir is None
        else detect_echo(capture.ir, capture.sample_rate)
        for capture in captures
    )
    geometry = assess_geometry(per_position_echo)

    # Spread is measured on smoothed per-position curves: the diagnostic is
    # "how much do positions disagree about this band's level", not "how
    # noisy is a single bin".
    smoothed_positions = (
        np.vstack(
            [smooth_fractional_octave(grid, row, fraction=diag_fraction) for row in stacked]
        )
        if stacked.shape[0] >= 2
        else stacked
    )

    for array in (power_mean_db, median_db, power_mean_diag_db, power_mean_spec_db, median_diag_db):
        array.flags.writeable = False

    return CombinedResponse(
        freqs_hz=grid,
        power_mean_db=power_mean_db,
        median_db=median_db,
        power_mean_diag_db=power_mean_diag_db,
        power_mean_spec_db=power_mean_spec_db,
        median_diag_db=median_diag_db,
        excluded=excluded,
        excluded_bands_hz=_merge_mask(grid, excluded),
        n_positions=len(captures),
        position_ids=tuple(c.position_id for c in captures),
        per_position_echo=per_position_echo,
        geometry_locked=geometry.locked,
        geometry=geometry,
        band_spread=_band_spread(grid, smoothed_positions),
        flag_threshold_db=flag_threshold_db,
        diag_fraction=diag_fraction,
        spec_fraction=spec_fraction,
    )
