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
   program, so one ``rfftfreq`` grid) this is the identity. A grid finer
   than ``MAX_ANALYSIS_BINS`` is then block-averaged in linear power onto a
   coarser linear grid (see that constant for why, and why *averaging*
   rather than subsampling).
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
   impulse response, plus the derived :attr:`CombinedResponse.geometry`
   verdict (``geometry.locked`` is the single owner of that bit). The
   detector's search window is a **rejection** contract rather than a clamp
   — out-of-window candidates are refused, and so are candidates hugging its
   lower edge, where a below-window echo aliases up and is indistinguishable
   from a real one — and a malformed IR refuses one position rather than
   failing the combine. All of it so that a number this module reports is
   one it actually measured.
6. **Spread diagnostics** — cross-position level spread and worst-bin sigma
   in octave bands, the observable behind the research's 1/sqrt(N) accuracy
   story.

**Two blind spots, two instruments — read this before trusting either.**
The mean-vs-median screen and ``geometry.locked`` are complementary, and
neither alone is sufficient:

* The screen fires on *partially* aligned interference, where some
  positions are nulled at a bin and others are not, so the null-filling
  power mean and the outlier-rejecting median diverge.
* The screen is **blind to fully-aligned nulls**. When every position sees
  the same null, mean and median agree perfectly and the null passes the
  screen untouched — while surviving the average at full depth. This is
  not a defect of the screen; it is the physics of a position-stable
  interference pattern, and it is exactly what ``geometry.locked``
  reports. A consumer seeing ``geometry.locked`` must tell the user to
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

# Upper bound on the analysis grid. A canonical grid finer than this is
# block-averaged in linear power onto a coarser linear grid before anything
# is combined or smoothed.
#
# Why bound it at all: smooth_fractional_octave is an O(bins * window)
# Python loop, and the window itself grows with the grid, so its cost is
# effectively quadratic in bin count — 0.12 s at 16k bins, 0.88 s at 65k,
# 2.3 s at 131k on a laptop, and this module must eventually run on a Pi 5.
#
# Why this is not a loss of information: 16385 bins over a 24 kHz span is
# ~1.46 Hz spacing, and the narrowest smoothing window this module applies
# (1/6-octave at the 250 Hz spec edge) is ~29 Hz wide — ~20 bins, so even
# the *coarsest* retained resolution still oversamples the narrowest
# analysis window by an order of magnitude. The plan evaluates pass/fail at
# 1/3-octave, wider still.
#
# Why block *averaging* and never subsampling: subsampling a combed
# magnitude curve aliases — it would keep whichever bins happen to land on
# comb peaks or nulls and silently change the level. Averaging in linear
# power is the same estimator this module already uses everywhere else, so
# decimation composes with the power mean instead of biasing it.
MAX_ANALYSIS_BINS = 16385

# --------------------------------------------------------------------------- #
# Echo-detector tuning
#
# Every threshold below was calibrated on three populations: the 2026-07-24/25
# JTS3 corpus (captures/flat-linearization-20260725/cdhorn-live-session, runs 5
# and 7 VERIFY + run 7 tweeter-alone), synthetic impulse+echo pairs across
# tau in 240-700 us and r in 0.15-0.6, and negative controls (white noise,
# impulse+noise with no echo, clean impulse with no echo). The measured
# separation is reported in tests/test_spatial_combine.py.
#
# The synthetic population starts at 240 us rather than the 200 us it once
# did because the default window's bottom quefrency step is now refused
# outright (see WINDOW_EDGE_MARGIN_STEPS): a 200 us echo sits inside that
# dead zone for the weakest reflections, so it is no longer a member of the
# "detector reports a number" population these thresholds separate.
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
# 0.63-0.65, synthetic echoes 0.67-0.81, negative controls 0.23-0.50.
CONCENTRATION_LO = 0.30
CONCENTRATION_HI = 0.70

# Corroboration — relative disagreement between the two independent tau
# estimators (cepstral peak and analytic-envelope secondary arrival). Full
# credit at or below TIGHT, zero at or above LOOSE. Measured: real corpus
# 1.7-3.1%, synthetic echoes 0.1-1.9%, negative controls 4.4-73% (a lone
# estimator can find a peak in noise; two independent ones rarely agree).
CORROBORATION_TIGHT = 0.05
CORROBORATION_LOOSE = 0.30

# Reported strength when no credible secondary arrival was reported.
# Mirrors program_analysis.DBFS_FLOOR's role: a finite floor, not -inf, so
# the field stays arithmetic-safe. Meaningless unless confidence > 0.
STRENGTH_FLOOR_DB = -120.0

# Edge-proximity rejection margin, in quefrency steps
# (EchoDiagnostic.resolution_us), applied at the search window's **lower**
# edge only. A surviving candidate sitting this close to search_us[0] is
# refused rather than reported.
#
# Why: an echo *below* the window does not vanish from the estimators — it
# aliases upward onto the bottom of the window. The envelope's search starts
# at search_lo, so a below-window arrival's decaying skirt makes the argmax
# land a few microseconds above that floor; the cepstral peak lands on the
# lowest search bin for the same reason. Both then agree with each other, so
# corroboration is high and confidence is high, and the number is still
# wrong. Measured (10 positions, true delays 150-400 us): a 300-800 us window
# produced estimates 2-18 us above its lower edge from 150 and 178 us echoes,
# at confidence 0.68-1.00, and the cloud read as geometry_locked.
#
# Why 1.0 step and not more: one quefrency step is exactly the interval over
# which the cepstral estimator cannot distinguish "at the edge" from "below
# the edge", so it is the smallest margin that closes the aliasing path and
# the largest that does not manufacture a dead zone the instrument could have
# measured. A genuine echo at 1.5 steps above the edge is still reported.
#
# Why the lower edge only — the asymmetry is physical, not an oversight. A
# below-window echo aliases UP onto the lower edge because that is where both
# searches begin. There is no mirror mechanism at the top: an above-window
# echo is simply outside both searches, and what lands at the upper edge is
# either a genuine in-window arrival or a cepstral rahmonic, neither of which
# is an aliased copy of an out-of-window delay. The upper edge is already
# covered by the window-rejection contract (an above-window candidate is
# rejected outright), so an extra top-edge margin would refuse honest
# measurements to guard against a failure mode that does not exist.
WINDOW_EDGE_MARGIN_STEPS = 1.0

# Refusal vocabulary for EchoDiagnostic.refusal. Snake_case, self-
# identifying, and stable: an empty string means "the detector ran to
# completion and is reporting a measurement" (which may still be a
# zero-confidence "found nothing credible"); any non-empty value means the
# detector declined to measure and every estimate on the record is
# uninformative. Consumers gate on `refusal == ""`, never on the specific
# slug, so this vocabulary can grow without breaking them.
REFUSAL_LOW_ARRIVAL_CREST = "low_arrival_crest"
REFUSAL_WINDOW_TOO_SHORT = "analysis_window_too_short"
REFUSAL_BAND_TOO_NARROW = "analysis_band_too_narrow"
REFUSAL_SEARCH_OUTSIDE_CEPSTRUM = "search_window_outside_cepstrum"
REFUSAL_NO_IN_WINDOW_ECHO = "no_in_window_echo"
REFUSAL_TAU_AT_WINDOW_LOWER_EDGE = "tau_at_window_lower_edge"
REFUSAL_ALL_ZERO_IR = "all_zero_ir"
REFUSAL_MALFORMED_IR = "malformed_ir"
REFUSAL_BAD_SAMPLE_RATE = "bad_sample_rate"
REFUSAL_BAD_BAND_HZ = "bad_band_hz"
REFUSAL_BAD_SEARCH_US = "bad_search_us"
REFUSAL_DETECTOR_ERROR = "detector_error"

# --------------------------------------------------------------------------- #
# Geometry-lock tuning
# --------------------------------------------------------------------------- #

# An echo diagnostic counts toward the geometry verdict only at or above
# this confidence. Calibrated to sit in the empty gap between the measured
# populations: true positives (synthetic impulse+echo, tau 240-700 us,
# r 0.15-0.6) scored 0.916-1.000, while a 60-seed negative-control sweep --
# impulse-with-no-echo IRs, 30 seeds at noise sigma 0.02 and 30 at 0.001,
# the two families that clear the crest gate and therefore actually stress
# the score -- spanned 0.000-0.091, with 0/60 crossing this floor. Both
# populations are re-measured by
# test_echo_confidence_floor_sits_in_the_measured_gap in
# tests/test_spatial_combine.py, so the gap cannot rot silently.
ECHO_CONFIDENCE_FLOOR = 0.5

# An echo diagnostic also counts only when its tau is at least this many
# quefrency steps (EchoDiagnostic.resolution_us) above zero. Below ~3 steps
# both estimators are inside the direct pulse's own skirt, so a "delay" is
# whatever the noise floor put there — see detect_echo's search_us
# resolution-floor paragraph. Feeding those numbers to the clustering test
# is how a dispersed cloud can read as falsely locked: unresolvable
# estimates pile up near the bottom of the window and look like agreement.
GEOMETRY_MIN_RESOLUTION_STEPS = 3.0

# The speaker's interference pattern is "geometry locked" when at least
# this fraction of confident per-position tau estimates fall within
# +-GEOMETRY_CLUSTER_TOLERANCE of their median. Position-stable tau means
# position-stable nulls, which spatial averaging cannot fill — the cloud is
# not spread enough (or the bounce is speaker-fixed diffraction rather than
# a boundary, the plan's S0 prediction 5).
GEOMETRY_CLUSTER_FRACTION = 0.70
GEOMETRY_CLUSTER_TOLERANCE = 0.15

# A "cluster" needs at least two members to mean anything: with a single
# usable estimate, 100% of estimates trivially sit within any tolerance of
# their own median. Below this the verdict is "unknown", reported as not
# locked with an explicit reason — never as a lock, because locking on no
# evidence is the one failure mode that actively misleads a household
# ("spread the mic further" when nothing was actually measured).
GEOMETRY_MIN_CONFIDENT = 2

# Geometry verdict reason vocabulary. Snake_case, self-identifying values,
# mirroring the house style of linearization_envelope.ReasonCode.
GEOMETRY_LOCKED = "geometry_locked"
GEOMETRY_DISPERSED = "geometry_dispersed"
# "usable", not "confident": n_confident counts the set that survived all
# three admission rules (measured, confident, *and* resolvable), so a value
# naming only confidence would describe a different set than the one the
# verdict was computed from.
GEOMETRY_UNKNOWN = "geometry_insufficient_usable_estimates"


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

    Read ``refusal`` first, then ``confidence``. A non-empty ``refusal``
    means the detector declined to measure and *every* estimate below is
    uninformative; an empty ``refusal`` with ``confidence == 0.0`` means the
    detector ran and found nothing credible. ``tau_us`` and ``strength_db``
    carry information only when ``refusal == ""`` and ``confidence > 0``.
    The supporting fields exist so a verdict is auditable rather than a
    magic number.

    Args:
      tau_us: delay of the secondary arrival, microseconds — the reported
        answer, and the only tau field with a window guarantee: it is
        either 0.0 (nothing reported) or inside the caller's ``search_us``
        window, clear of its lower edge by ``WINDOW_EDGE_MARGIN_STEPS``.
        It is **always** the analytic-envelope estimate: sample resolution
        plus parabolic refinement, ~1-4% accurate on the calibration set
        (worst at the bottom of the window, ~1% by 700 us).
        It is never the cepstral peak and is therefore *not* quantised to
        ``resolution_us`` — the cepstrum's job here is corroboration, and a
        reported tau requires both estimators in-window (see
        :func:`detect_echo`), so the envelope's estimate is the only one
        that can survive to be reported.
      strength_db: secondary-arrival level relative to the direct arrival,
        from the band-limited analytic envelope. ``STRENGTH_FLOOR_DB``
        whenever no delay is reported.
      confidence: 0.0-1.0 credibility. Two multiplied factors — cepstral
        concentration and agreement between the two tau estimators —
        behind an arrival-crest gate that is an early return rather than a
        third factor (a signal with no identifiable direct arrival is
        refused outright, not scored down). 0.0 means no credible peak.
      refusal: ``""`` when the detector produced a measurement; otherwise
        one of the ``REFUSAL_*`` slugs saying why it declined.
      resolution_us: the **cepstral corroborator's** quefrency step,
        ``1 / band_width``, in microseconds — ~71 us for the 5-19 kHz
        default band. It is not the granularity of ``tau_us`` (which is
        sample-rate plus parabolic, and far finer); it is this module's
        *trust floor*, because the envelope estimate only counts once the
        cepstrum has corroborated it and the cepstrum cannot resolve
        better than one step. Two rules are written in these units: a
        candidate within ``WINDOW_EDGE_MARGIN_STEPS`` of the search
        window's lower edge is refused by :func:`detect_echo`, and a
        ``tau_us`` below roughly **3 * resolution_us** is "an early echo is
        present" rather than a trustworthy delay, which
        :func:`assess_geometry` enforces before clustering.
      tau_cepstral_us: the cepstral estimator's answer, in isolation. This
        field is *raw*: it may fall outside ``search_us``, which is
        precisely the condition that rejects the candidate and can produce
        the ``no_in_window_echo`` refusal — or it may sit just inside the
        lower edge, which produces the ``tau_at_window_lower_edge``
        refusal. Kept unclamped on purpose — a railed value hidden by a
        clamp is what made a dispersed cloud read as geometry-locked.
      tau_envelope_us: the envelope estimator's answer, in isolation, also
        raw and also not window-guaranteed. 0.0 when the search window was
        too narrow to hold a peak at all. The envelope always returns the
        largest value in its window — it does not decide whether that is a
        real arrival, which is what corroboration and the window check are
        for.
      concentration: bounded [0, 1] cepstral peak concentration.
      corroboration: relative disagreement between the two tau estimators
        (0.0 = identical). 1.0 whenever the two cannot be compared — the
        envelope found nothing, or either estimate fell outside the search
        window, since a rejected candidate corroborates nothing.
      arrival_crest_db: direct arrival level above the IR's median
        ``|sample|`` level — the "is there an arrival at all" gate.
    """

    tau_us: float
    strength_db: float
    confidence: float
    refusal: str
    resolution_us: float
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
      locked: the verdict. Never True on insufficient evidence.
      reason: one of ``GEOMETRY_LOCKED`` / ``GEOMETRY_DISPERSED`` /
        ``GEOMETRY_UNKNOWN``.
      n_confident: how many positions produced a *usable* echo diagnostic —
        no refusal, confidence at or above ``confidence_floor``, and a tau
        at least ``GEOMETRY_MIN_RESOLUTION_STEPS`` quefrency steps above
        zero. This is the set the clustering test actually ran on, not the
        raw count of non-``None`` diagnostics.
      n_positions: how many captures were combined.
      median_tau_us: median tau over the usable set (0.0 when none).
      clustered_fraction: fraction of the usable set within ``tolerance``
        of ``median_tau_us``.
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
    """Cross-position magnitude spread in one octave band — two numbers
    answering two different questions.

    ``sigma_db`` is the *level* spread: each position is first collapsed to
    one band level by averaging its bins in **linear power**, then the
    sample standard deviation (``ddof=1``, the unbiased estimator of the
    population spread from N positions) is taken across those N levels.
    That is the quantity the research's ``1/sqrt(N)`` accuracy law is
    about, and it is deliberately insensitive to comb structure: a band
    holds many comb periods, so power-averaging over it lands near
    ``1 + r**2`` whatever the per-position delay is. A healthy dispersed
    cloud therefore has a *small* ``sigma_db``; a large one means the
    positions genuinely disagree about how loud this part of the spectrum
    is (mic distance, gain, or directivity), which averaging will not fix.

    ``max_sigma_db`` is the *structure* spread: the worst single bin's
    cross-position sigma, computed per-bin without any smoothing. It rides
    comb nulls on purpose — that is its diagnostic point. A band whose
    ``max_sigma_db`` dwarfs its ``sigma_db`` is null-dominated at a few
    frequencies (positions disagree bin-by-bin but agree on the band's
    energy: decorrelation working), whereas a band where the two are
    comparable is broadly noisy.

    Args:
      center_hz: nominal ISO octave-band centre.
      f_lo: band lower edge, clipped to the shared grid's support.
      f_hi: band upper edge, clipped to the shared grid's support.
      sigma_db: cross-position sigma of the per-position band-power level,
        in dB.
      max_sigma_db: worst single bin's cross-position sigma, in dB.
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
      freqs_hz: the shared canonical linear grid every curve below lives on
        — after any ``MAX_ANALYSIS_BINS`` block-average decimation, so this
        is the grid the curves were actually computed on, not the captures'.
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
      per_position_echo: index-aligned with ``position_ids``. ``None``
        means *strictly* "no IR was supplied for this position" — the only
        thing it ever means. A capture that supplied an IR always gets an
        :class:`EchoDiagnostic`, including when the detector rejected the
        input outright (a non-empty ``refusal``) or ran and found nothing
        credible (empty ``refusal``, zero confidence). Those three states
        are deliberately distinguishable.
      geometry: the geometry verdict with its supporting numbers.
        ``geometry.locked`` is the single owner of that bit — there is no
        mirror field, so the two can never drift.
      band_spread: per-octave-band cross-position spread. Empty when fewer
        than two positions were supplied (spread is undefined for one).
      flag_threshold_db: the screen threshold actually applied.
      diag_fraction: the diagnostic smoothing fraction actually applied.
      spec_fraction: the spec smoothing fraction actually applied.
      echo_band_hz: the echo-detector analysis band actually applied.
      echo_search_us: the echo-detector search window actually applied.
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
    geometry: GeometryLock
    band_spread: tuple[BandSpread, ...]
    flag_threshold_db: float
    diag_fraction: int
    spec_fraction: int
    echo_band_hz: tuple[float, float]
    echo_search_us: tuple[float, float]


# --------------------------------------------------------------------------- #
# Grid / array helpers
#
# All private except :func:`merged_true_intervals`, which is shared with
# jasper.active_speaker.flat_spec so the interval-merge rule has one owner.
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


def _decimate_to_analysis_grid(
    grid: np.ndarray, stacked: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Block-average a too-fine analysis grid down to ``MAX_ANALYSIS_BINS``.

    Averaging is done in **linear power**, the same estimator the rest of
    this module uses, so decimation composes with the power mean instead of
    biasing it; subsampling would alias a combed curve onto whichever bins
    happened to land on peaks or nulls. See ``MAX_ANALYSIS_BINS`` for the
    cost/resolution argument.

    Blocks are a fixed width, so the decimated grid stays exactly linear
    (block centres are spaced by ``block * step``) and remains a legal input
    to :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`.
    A trailing partial block is dropped rather than averaged, because a
    short final block would sit at a different centre and break that
    uniformity; at most ``block - 1`` bins — under one part in ten thousand
    of the span — are lost off the top.

    Identity (same objects) when the grid is already within the bound.
    """
    n_bins = int(grid.size)
    if n_bins <= MAX_ANALYSIS_BINS:
        return grid, stacked
    block = -(-n_bins // MAX_ANALYSIS_BINS)  # ceil division
    n_blocks = n_bins // block
    kept = n_blocks * block
    coarse_grid = grid[:kept].reshape(n_blocks, block).mean(axis=1)
    power = 10.0 ** (stacked[:, :kept] / 10.0)
    coarse_stacked = 10.0 * np.log10(
        power.reshape(stacked.shape[0], n_blocks, block).mean(axis=2)
    )
    coarse_grid.flags.writeable = False
    return coarse_grid, coarse_stacked


def merged_true_intervals(
    freqs_hz: np.ndarray, mask: np.ndarray
) -> tuple[tuple[float, float], ...]:
    """Contiguous ``True`` runs of ``mask`` as merged ``(f_lo, f_hi)``
    intervals spanning each run's first and last flagged bin.

    The single owner of this rule. :mod:`jasper.active_speaker.flat_spec`
    imports it rather than keeping its own copy, so the exclusion intervals
    a spec report discloses and the ones the combiner reports are the same
    fact computed the same way — a second implementation is a second thing
    to drift.

    Adjacency is by **array index**, which is a proxy for frequency
    adjacency only when ``freqs_hz`` is ascending. Both callers validate
    that (``_validate_capture`` here, ``evaluate_flat_spec`` there), so the
    assumption is enforced upstream rather than re-checked per call. No
    gap-bridging: two runs separated by a single unflagged bin stay two
    intervals.
    """
    flagged = np.flatnonzero(mask)
    if flagged.size == 0:
        return ()
    breaks = np.flatnonzero(np.diff(flagged) > 1)
    starts = np.concatenate(([flagged[0]], flagged[breaks + 1]))
    ends = np.concatenate((flagged[breaks], [flagged[-1]]))
    return tuple(
        (float(freqs_hz[s]), float(freqs_hz[e]))
        for s, e in zip(starts, ends, strict=True)
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


class EchoInputError(ValueError):
    """A malformed :func:`detect_echo` input, carrying a stable slug.

    A ``ValueError`` subclass so every existing caller (and test) that
    expects ``ValueError`` keeps working, with a machine-readable ``slug``
    attached **at the raise site** — so :func:`combine_positions` can turn
    the failure into a self-describing refused diagnostic without matching
    on message text, which would rot the first time a message is reworded.
    """

    def __init__(self, slug: str, message: str) -> None:
        super().__init__(message)
        self.slug = slug


def _refused(
    refusal: str,
    *,
    resolution_us: float = 0.0,
    arrival_crest_db: float = 0.0,
    tau_cepstral_us: float = 0.0,
    tau_envelope_us: float = 0.0,
    concentration: float = 0.0,
) -> EchoDiagnostic:
    """A refused diagnostic: no delay reported, and a slug saying why.

    Every call site passes a non-empty ``REFUSAL_*`` slug — this helper is
    the "the detector declined" constructor, and only that. The other
    zero-confidence outcome, "the detector ran to completion and found
    nothing credible", carries ``refusal == ""`` **and** a real
    ``corroboration`` reading, so it is built inline in :func:`detect_echo`
    rather than here (this helper hard-codes ``corroboration=1.0``, which
    on a non-refused diagnostic would misreport a measured value).

    The raw estimator fields are carried through when they exist, because a
    railed or edge-hugging cepstral estimate is exactly the evidence a
    reader needs to understand a ``no_in_window_echo`` or
    ``tau_at_window_lower_edge`` refusal.
    """
    return EchoDiagnostic(
        tau_us=0.0,
        strength_db=STRENGTH_FLOOR_DB,
        confidence=0.0,
        refusal=refusal,
        resolution_us=resolution_us,
        tau_cepstral_us=tau_cepstral_us,
        tau_envelope_us=tau_envelope_us,
        concentration=concentration,
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

    ``tau_us`` is therefore **always** the envelope estimate — a reported
    delay requires both estimators in-window (below), so the cepstrum never
    stands in for it — and ``confidence`` is what makes it trustworthy:
    cepstral concentration (is the ripple energy at one quefrency) times
    corroboration (do the two estimators agree), behind an arrival-crest
    gate (is there a direct arrival at all) that refuses outright rather
    than scoring down. Requiring both factors to line up behind that gate is
    what separates the real corpus from every negative control — see the
    calibration figures on the module's threshold constants.

    **``search_us`` is a rejection contract, not a clamp.** A candidate
    whose *refined* delay lands outside the window is rejected, never pulled
    back to the edge. Clamping fabricates data: an echo just past the window
    edge would be reported as sitting exactly on the edge, and — because
    every position's railed estimate rails to the *same* edge — a cloud with
    genuinely dispersed delays would then cluster tightly enough to read as
    falsely ``geometry_locked``. So a rejected candidate cannot corroborate
    anything (``corroboration`` is forced to 1.0), which in practice means a
    non-zero ``confidence`` requires **both** estimators in-window; and if
    *neither* survives, the result is a ``no_in_window_echo`` refusal rather
    than a number. A caller who suspects the echo lies outside the window
    should widen the window, not trust an edge value.

    **The window's lower edge is refused, not merely bounded.** Rejecting
    out-of-window candidates is not enough on its own, because an echo
    *below* the window does not disappear from the estimators — it aliases
    upward onto the bottom of the window, where both searches begin, and the
    two aliased estimates then agree with each other. The result is a
    confident, in-window, wrong number, and a cloud of them piles up just
    above the same edge and reads as ``geometry_locked`` (measured: true
    delays of 150-400 us searched in 300-800 us produced estimates 2-18 us
    above the edge at confidence 0.68-1.00). So a surviving candidate within
    ``WINDOW_EDGE_MARGIN_STEPS`` quefrency steps of ``search_us[0]`` is
    refused with ``tau_at_window_lower_edge``: at that distance an in-window
    measurement is indistinguishable from a below-window echo aliasing up,
    so it cannot be trusted as either. The remedy is the same one the window
    contract always prescribes — widen ``search_us`` (or ``band_hz``, which
    shrinks the step) and measure again. **No equivalent margin exists at
    the upper edge**, and that asymmetry is physical: nothing aliases *down*
    onto the top of the window, so a candidate there is either a genuine
    in-window arrival or a cepstral rahmonic, and the plain rejection
    contract already handles what lies beyond it.

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

        **Resolution floor — the bottom of the window does not measure.**
        Both estimators degrade as tau approaches ``1 / bandwidth``
        (:attr:`EchoDiagnostic.resolution_us`, ~71 us for the 5-19 kHz
        default): the cepstral quefrency step *is* ``1 / bandwidth``, and the
        band-limited envelope cannot separate an arrival from the direct
        pulse's own skirt inside roughly one envelope width. Two consequences
        a caller has to plan the window around:

        * The bottom ``WINDOW_EDGE_MARGIN_STEPS`` of *whatever* window is
          asked for is refused outright (``tau_at_window_lower_edge``),
          because inside it a real echo and an aliased below-window one look
          identical. For the defaults that is roughly 120-191 us: the
          effective floor of the default window is ~191 us, not its stated
          120 us. The estimators' own near-floor bias stretches that a
          little further for weak reflections, since an under-read lands
          *inside* the margin — measured on the synthetic set, echoes at 150
          and 178 us are always refused, and at r=0.15 the refusal reaches
          ~210 us (1.26 steps) before clearing at ~220 us (1.40 steps).
        * Independently of the window, :func:`assess_geometry` will not
          cluster a ``tau_us`` below ``GEOMETRY_MIN_RESOLUTION_STEPS *
          resolution_us`` (~214 us for the defaults) — that floor is
          measured from zero delay, not from ``search_us[0]``, so it binds
          on a low window and is already satisfied by a high one.

        The plan's target bounce (~300 us) and the corpus's measured
        313-323 us sit comfortably above both floors. Widening ``band_hz``
        is what buys resolution below them: it shrinks ``resolution_us``,
        which lowers the edge margin and the clustering floor together.

    Returns:
      An :class:`EchoDiagnostic`. A non-empty ``refusal`` means the detector
      declined; ``confidence == 0.0`` with an empty ``refusal`` means it ran
      and found nothing credible. Either way the estimates carry no
      information, and ``tau_us`` is 0.0.

    Raises:
      EchoInputError: (a ``ValueError``) on an empty / non-finite IR, a
        non-positive sample rate, or a degenerate band / search window.
    """
    try:
        samples = _as_float_array(ir, "ir")
    except ValueError as exc:
        raise EchoInputError(REFUSAL_MALFORMED_IR, str(exc)) from exc
    if sample_rate <= 0:
        raise EchoInputError(
            REFUSAL_BAD_SAMPLE_RATE, f"sample_rate must be positive, got {sample_rate}"
        )

    lo_hz, hi_hz = float(band_hz[0]), min(float(band_hz[1]), sample_rate / 2.0)
    if not 0.0 < lo_hz < hi_hz:
        raise EchoInputError(
            REFUSAL_BAD_BAND_HZ,
            f"band_hz must satisfy 0 < lo < hi <= Nyquist, got {band_hz} at "
            f"sample_rate={sample_rate}",
        )
    search_lo_s = float(search_us[0]) * 1e-6
    search_hi_s = float(search_us[1]) * 1e-6
    if not 0.0 < search_lo_s < search_hi_s:
        raise EchoInputError(
            REFUSAL_BAD_SEARCH_US, f"search_us must satisfy 0 < lo < hi, got {search_us}"
        )

    # The quefrency step of the band actually used (after Nyquist clipping)
    # — reported on every diagnostic, including refusals, so a consumer can
    # judge whether a delay is resolvable without re-deriving the band.
    resolution_us = 1e6 / (hi_hz - lo_hz)

    # --- 1. Locate the direct arrival, and gate on it existing at all. ---
    # The coarse locate is argmax|ir|, which agrees with the band-limited
    # envelope peak to within a couple of samples on every real and
    # synthetic case measured; it diverges only for signals with no arrival,
    # which the crest gate rejects anyway.
    peak_index = int(np.argmax(np.abs(samples)))
    median_level = float(np.median(np.abs(samples)))
    peak_level = float(np.abs(samples[peak_index]))
    if peak_level <= 0.0:
        raise EchoInputError(
            REFUSAL_ALL_ZERO_IR, "ir is all zeros — no impulse response to analyse"
        )
    crest_db = (
        20.0 * float(np.log10(peak_level / median_level))
        if median_level > 0.0
        else float("inf")
    )
    if crest_db < ARRIVAL_CREST_FLOOR_DB:
        return _refused(
            REFUSAL_LOW_ARRIVAL_CREST,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
        )

    # --- 2. Window to the early-arrival region. ---
    pre = int(round(ECHO_WINDOW_PRE_S * sample_rate))
    span = int(round(ECHO_WINDOW_SPAN_FACTOR * search_hi_s * sample_rate))
    start = max(0, peak_index - pre)
    stop = min(samples.size, peak_index + span)
    segment = samples[start:stop]
    if segment.size < 8:
        return _refused(
            REFUSAL_WINDOW_TOO_SHORT,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
        )

    # --- 3. Cepstral estimator. ---
    n_fft = _n_fft_for(segment.size)
    spectrum = np.abs(np.fft.rfft(segment, n_fft)) + 1e-12
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    band = (freqs >= lo_hz) & (freqs <= hi_hz)
    n_band = int(np.count_nonzero(band))
    if n_band < MIN_ECHO_BAND_BINS:
        return _refused(
            REFUSAL_BAND_TOO_NARROW,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
        )

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
        return _refused(
            REFUSAL_SEARCH_OUTSIDE_CEPSTRUM,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
        )

    # Peak within the search window, but refined against the FULL cepstrum:
    # a peak sitting on the first or last search bin still has real
    # neighbours just outside the slice, and refining on the slice alone
    # would discard them and rail the estimate onto the boundary bin. The
    # refined value is NOT clamped back into the window — if refinement
    # walks it out, that is the honest answer ("the ripple's quefrency is
    # outside what you asked for") and the candidate is rejected below.
    search_indices = np.flatnonzero(in_search)
    peak = int(search_indices[np.argmax(cepstrum[in_search])])
    quefrency_step = float(quefrency[1] - quefrency[0])
    tau_cepstral = float(
        quefrency[peak] + _parabolic_offset(cepstrum, peak) * quefrency_step
    )
    cepstral_in_window = search_lo_s <= tau_cepstral <= search_hi_s
    baseline = float(np.linalg.norm(cepstrum[above_floor]))
    concentration = float(cepstrum[peak] / baseline) if baseline > 0.0 else 0.0

    # --- 4. Envelope estimator. ---
    envelope = _analytic_envelope(_bandpass(segment, lo_hz, hi_hz, sample_rate))
    main = int(np.argmax(envelope))
    first = main + int(round(search_lo_s * sample_rate))
    last = min(envelope.size - 2, main + int(round(search_hi_s * sample_rate)))
    tau_envelope = 0.0
    envelope_strength_db = STRENGTH_FLOOR_DB
    if last > first + 1:
        local = first + int(np.argmax(envelope[first : last + 1]))
        tau_envelope = (local + _parabolic_offset(envelope, local) - main) / sample_rate
        envelope_strength_db = 20.0 * float(
            np.log10(max(float(envelope[local]), 1e-15) / max(float(envelope[main]), 1e-15))
        )
    # Sub-sample refinement can push an edge peak a fraction of a sample
    # past the boundary, so the envelope needs the same window check.
    envelope_in_window = search_lo_s <= tau_envelope <= search_hi_s

    # --- 5. Fuse. ---
    if not cepstral_in_window and not envelope_in_window:
        return _refused(
            REFUSAL_NO_IN_WINDOW_ECHO,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
            tau_cepstral_us=tau_cepstral * 1e6,
            tau_envelope_us=tau_envelope * 1e6,
            concentration=concentration,
        )

    # A candidate that survived the window but hugs its lower edge is an
    # aliased below-window echo as readily as an in-window one, and the two
    # cannot be told apart from inside this band — refuse rather than pick.
    # Both surviving candidates are checked, not just the reported one: an
    # edge-hugging cepstral peak corroborates an edge-hugging envelope peak
    # into high confidence, which is the exact mechanism being closed.
    edge_margin_s = WINDOW_EDGE_MARGIN_STEPS * resolution_us * 1e-6
    at_lower_edge = (
        cepstral_in_window and tau_cepstral - search_lo_s <= edge_margin_s
    ) or (envelope_in_window and tau_envelope - search_lo_s <= edge_margin_s)
    if at_lower_edge:
        return _refused(
            REFUSAL_TAU_AT_WINDOW_LOWER_EDGE,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
            tau_cepstral_us=tau_cepstral * 1e6,
            tau_envelope_us=tau_envelope * 1e6,
            concentration=concentration,
        )

    if cepstral_in_window and envelope_in_window:
        corroboration = abs(tau_envelope - tau_cepstral) / max(tau_cepstral, 1e-12)
    else:
        # A rejected candidate corroborates nothing. Comparing against it
        # anyway is how a railed estimate manufactures agreement.
        corroboration = 1.0

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
            refusal="",
            resolution_us=resolution_us,
            tau_cepstral_us=tau_cepstral * 1e6,
            tau_envelope_us=tau_envelope * 1e6,
            concentration=concentration,
            corroboration=float(corroboration),
            arrival_crest_db=crest_db,
        )

    # The envelope estimate is the answer, unconditionally. Getting here
    # needs confidence > 0, which needs corroboration < CORROBORATION_LOOSE,
    # which only the both-in-window branch above can produce — so the
    # envelope is in-window by construction and the cepstrum never has to
    # stand in for it. (An earlier version had a `tau_cepstral` fallback
    # here; it was unreachable for exactly this reason, while documenting a
    # coarser, quantised tau that the field could never actually hold.)
    return EchoDiagnostic(
        tau_us=tau_envelope * 1e6,
        strength_db=envelope_strength_db,
        confidence=confidence,
        refusal="",
        resolution_us=resolution_us,
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

    ``locked`` when at least ``min_fraction`` of the *usable* per-position
    tau estimates (defined below; ``n_confident`` counts them) fall within
    ``tolerance`` (relative) of their median. A
    stable tau means a stable null ladder at ``(n+0.5)/tau``, which spatial
    averaging cannot fill however many positions are added — the honest
    consumer response is to ask the user to spread the mic further, or (the
    plan's S0 prediction 5) to conclude the bounce is speaker-fixed
    diffraction rather than a boundary, in which case the exclusion screen
    carries the weight instead.

    **What counts as usable evidence** — a diagnostic is admitted only when
    all three hold, because each excluded class produces a *false* lock in a
    different way:

    1. ``refusal == ""`` — the detector actually measured. A refusal's
       ``tau_us`` is 0.0, and a pile of zeros clusters perfectly.
    2. ``confidence >= confidence_floor`` — the long-standing gate.
    3. ``tau_us > 0`` **and** ``tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS *
       resolution_us`` — the delay is resolvable at all. Below ~3 quefrency
       steps both estimators sit inside the direct pulse's own skirt and
       read low, so a cloud whose true delays are all near the resolution
       floor collapses toward one unresolvable value and looks locked when
       it is merely unmeasurable. The explicit ``> 0`` is not redundant with
       the product: a diagnostic carrying ``resolution_us == 0`` (which
       :func:`detect_echo` never emits, but a hand-built or deserialised
       record can) would otherwise clear a threshold of zero with a
       ``tau_us`` of zero, and a pile of zeros clusters perfectly — the same
       false lock rule 1 exists to prevent, arriving by a different door.

    Fewer than ``GEOMETRY_MIN_CONFIDENT`` usable estimates is reported as
    not-locked with reason ``GEOMETRY_UNKNOWN`` — never as a lock. A single
    estimate sits within any tolerance of its own median, so "100%
    clustered" would be a vacuous lock, and locking on no evidence tells a
    household to go move the mic for nothing.

    Args:
      echoes: per-position diagnostics, ``None`` where no IR was supplied.
      confidence_floor: minimum :attr:`EchoDiagnostic.confidence` to count.
      tolerance: relative clustering tolerance about the median tau.
      min_fraction: fraction of the usable set that must cluster.

    Returns:
      A :class:`GeometryLock` carrying the verdict and its supporting
      numbers. ``n_confident`` is the size of the usable set defined above.
    """
    n_positions = len(echoes)
    taus = np.array(
        [
            e.tau_us
            for e in echoes
            if e is not None
            and e.refusal == ""
            and e.confidence >= confidence_floor
            and e.tau_us > 0.0
            and e.tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS * e.resolution_us
        ],
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


def _band_spread(freqs: np.ndarray, stacked: np.ndarray) -> tuple[BandSpread, ...]:
    """Octave-band cross-position spread from raw per-position curves.

    Deliberately **unsmoothed**. An earlier version 1/6-octave-smoothed
    every position first, which cost one ``smooth_fractional_octave`` pass
    per position — the dominant term in the combiner's runtime — to compute
    a band statistic that a band-power average already gives directly and
    exactly. Both numbers here come straight from the stacked curves: see
    :class:`BandSpread` for what each one means and why they answer
    different questions.
    """
    if stacked.shape[0] < 2:
        return ()
    power = 10.0 ** (stacked / 10.0)
    per_bin_sigma = np.std(stacked, axis=0, ddof=1)
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
        # One level per position: the band's energy, in power, then dB.
        band_level_db = 10.0 * np.log10(np.mean(power[:, mask], axis=1))
        bands.append(
            BandSpread(
                center_hz=float(center),
                f_lo=float(freqs[mask][0]),
                f_hi=float(freqs[mask][-1]),
                sigma_db=float(np.std(band_level_db, ddof=1)),
                max_sigma_db=float(np.max(per_bin_sigma[mask])),
                n_bins=n_bins,
            )
        )
    return tuple(bands)


def _echo_for(
    capture: PositionCapture,
    band_hz: tuple[float, float],
    search_us: tuple[float, float],
) -> EchoDiagnostic | None:
    """One position's echo diagnostic, or ``None`` when it supplied no IR.

    A malformed IR is one position's problem, never the whole cloud's: the
    detector's ``ValueError`` becomes a refused diagnostic carrying the
    reason, so ten good captures plus one all-zero IR still combine. The
    slug comes from :class:`EchoInputError` when the detector raised one,
    so this never parses a message string.

    What can still arrive here is per-*capture* trouble only —
    ``malformed_ir``, ``all_zero_ir``, and ``bad_band_hz`` when the band
    exceeds this capture's Nyquist. Config-shaped failures cannot: a
    malformed band or search window is rejected up front by
    :func:`combine_positions`, and a non-positive sample rate by
    ``_validate_capture``, both before any detection runs.
    """
    if capture.ir is None:
        return None
    try:
        return detect_echo(
            capture.ir, capture.sample_rate, band_hz=band_hz, search_us=search_us
        )
    except ValueError as exc:
        return _refused(getattr(exc, "slug", REFUSAL_DETECTOR_ERROR))


def combine_positions(
    captures: Sequence[PositionCapture],
    *,
    flag_threshold_db: float = DEFAULT_FLAG_THRESHOLD_DB,
    diag_fraction: int = DEFAULT_DIAG_FRACTION,
    spec_fraction: int = DEFAULT_SPEC_FRACTION,
    echo_band_hz: tuple[float, float] = DEFAULT_ECHO_BAND_HZ,
    echo_search_us: tuple[float, float] = DEFAULT_ECHO_SEARCH_US,
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
      diag_fraction: diagnostic 1/N-octave fraction (screen).
      spec_fraction: spec 1/N-octave fraction (pass/fail curve).
      echo_band_hz: analysis band handed to :func:`detect_echo`.
      echo_search_us: search window handed to :func:`detect_echo`. Both are
        echoed back on the result, because a per-position tau is only
        interpretable against the window it was searched in.

    Returns:
      A :class:`CombinedResponse`.

    Raises:
      ValueError: on no captures, a malformed capture (see
        :class:`PositionCapture`), captures sharing no frequency support, a
        non-positive smoothing fraction / threshold, or a malformed
        ``echo_band_hz`` / ``echo_search_us``.

        **Malformed config raises; malformed data refuses.** Every argument
        above is the caller's own configuration, wrong for every position at
        once and un-fixable by looking at the captures — so it fails loudly,
        the same posture ``flag_threshold_db`` has always had. A malformed
        *IR* is the opposite: one position's data problem, which becomes one
        refused diagnostic while the other positions combine normally.
        Silently turning a mistyped search window into N identical refusals
        would have reported "no echo found anywhere" for what is really
        "you asked the wrong question".

        One case sits deliberately on the data side of that line: a band
        that is well-formed but exceeds *a particular capture's* Nyquist.
        That is an interaction between the config and one capture's sample
        rate, not a property of the config, so it refuses that position
        rather than failing the combine.
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
    for name, bounds in (
        ("echo_band_hz", echo_band_hz),
        ("echo_search_us", echo_search_us),
    ):
        lo, hi = float(bounds[0]), float(bounds[1])
        if not (np.isfinite(lo) and np.isfinite(hi)) or not 0.0 < lo < hi:
            raise ValueError(
                f"{name} must be finite and satisfy 0 < lo < hi, got {bounds}"
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

    # Bound the analysis cost before anything expensive touches the grid.
    grid, stacked = _decimate_to_analysis_grid(grid, stacked)

    power_mean_db = 10.0 * np.log10(np.mean(10.0 ** (stacked / 10.0), axis=0))
    median_db = np.median(stacked, axis=0)

    power_mean_diag_db = smooth_fractional_octave(grid, power_mean_db, fraction=diag_fraction)
    power_mean_spec_db = smooth_fractional_octave(grid, power_mean_db, fraction=spec_fraction)
    median_diag_db = smooth_fractional_octave(grid, median_db, fraction=diag_fraction)

    excluded = np.abs(power_mean_diag_db - median_diag_db) > flag_threshold_db
    excluded.flags.writeable = False

    per_position_echo: tuple[EchoDiagnostic | None, ...] = tuple(
        _echo_for(capture, echo_band_hz, echo_search_us) for capture in captures
    )
    geometry = assess_geometry(per_position_echo)

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
        excluded_bands_hz=merged_true_intervals(grid, excluded),
        n_positions=len(captures),
        position_ids=tuple(c.position_id for c in captures),
        per_position_echo=per_position_echo,
        geometry=geometry,
        band_spread=_band_spread(grid, stacked),
        flag_threshold_db=flag_threshold_db,
        diag_fraction=diag_fraction,
        spec_fraction=spec_fraction,
        echo_band_hz=(float(echo_band_hz[0]), float(echo_band_hz[1])),
        echo_search_us=(float(echo_search_us[0]), float(echo_search_us[1])),
    )
