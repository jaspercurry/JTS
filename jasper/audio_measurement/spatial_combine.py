# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spatial multi-capture combiner and interference honesty screen.

``combine_positions(captures) -> CombinedResponse`` power-averages N gated sweeps from a cloud
of mic positions onto one shared linear grid, and flags bins where the power mean and dB
median disagree by more than ``flag_threshold_db`` as interference-dominated. :func:`detect_echo`
stamps a per-capture tau/strength diagnostic; :func:`assess_geometry` reduces those to the
``geometry.locked`` verdict.

The screen and ``geometry.locked`` are complementary; neither alone is sufficient. The screen
fires on *partially* aligned interference (some positions nulled at a bin, others not); it is
blind to a fully-aligned null, where every position sees the same null and it survives the
average at full depth — the case ``geometry.locked`` reports, which no averaging over these
positions can fill. Silence from the screen is also the healthy outcome, since for uniformly
distributed comb phase the two estimators coincide analytically.

Detection only — nothing here removes an echo. Pure computation (numpy plus
:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`): no I/O, no logging, no
globals, no randomness, no product policy.

See docs/historical/linearization-campaign-2026-07.md.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from jasper.audio_measurement.analysis import smooth_fractional_octave

# --------------------------------------------------------------------------- #
# Combiner tuning
# --------------------------------------------------------------------------- #

# Power-mean vs median disagreement above this many dB flags a bin as interference-dominated.
DEFAULT_FLAG_THRESHOLD_DB = 2.0

# 1/6-octave for diagnostics, 1/3-octave for pass/fail. The screen runs at the diagnostic
# fraction because a 1/3-oct window is wide enough to smear a narrow null before comparison.
DEFAULT_DIAG_FRACTION = 6
DEFAULT_SPEC_FRACTION = 3

# ISO preferred octave-band centres for the cross-position spread diagnostic. Octave, not
# 1/3-octave: a legible ~10-number diagnostic, not a curve. Needs >= MIN_BAND_BINS grid bins.
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

# A capture grid must be uniformly spaced to this relative tolerance: smooth_fractional_octave
# binary-searches linear bins, so a log grid gets a silently wrong window width everywhere.
GRID_UNIFORMITY_RTOL = 1e-3

# Upper bound on the analysis grid; a finer canonical grid is block-averaged in linear power
# onto a coarser one first. smooth_fractional_octave's cost is effectively quadratic in bin
# count (0.12 s at 16k bins, 0.88 s at 65k, 2.3 s at 131k on a laptop; this must run on a Pi 5).
# No resolution lost: 16385 bins over 24 kHz is ~1.46 Hz spacing against a ~29 Hz narrowest
# window (1/6-octave at 250 Hz). Averaging, never subsampling — subsampling a combed curve
# aliases onto whichever bins land on peaks or nulls.
MAX_ANALYSIS_BINS = 16385

# --------------------------------------------------------------------------- #
# Echo-detector tuning — every threshold calibrated against three populations: a JTS3 cdhorn
# corpus, synthetic impulse+echo pairs (tau 240-700 us, r 0.15-0.6), and negative controls
# (white noise, impulse+noise, clean impulse with no echo).
# --------------------------------------------------------------------------- #

DEFAULT_ECHO_BAND_HZ = (5000.0, 19000.0)

# The default search window, and the only one whose false-lock behaviour has been swept.
# Prefer it. A window whose lower edge is 650 us or higher enters the rahmonic regime: a window
# excluding the true delay can still contain a rahmonic of it, so the envelope finds a matching
# peak and the two corroborate a delay ~3x too large. Measured on a 10-position cloud of true
# delays 150-400 us: (600, 1000) was clean, while (650, 1000), (700, 1000) and (800, 1200) each
# read ``geometry_locked`` at median tau 814/857/897 us — ``RAHMONIC_MARGIN`` refuses all of
# those, by 21.5-78.8x. That makes a raised window SCREENED, not VALIDATED: an honest in-window
# echo under a stronger EARLIER reflection is refused too (605/720 swept two-echo cases, vs
# 0/432 at the default window), always as a refusal rather than a wrong number.
DEFAULT_ECHO_SEARCH_US = (120.0, 800.0)

# Polynomial detrend order removed from the band's log-magnitude before the cepstral
# transform, so the driver's own broad shape does not leak into low quefrencies.
DETREND_ORDER = 3

# The direct arrival must stand at least this far above the IR's median |sample| level, else
# "echo level re main arrival" is undefined — confidence 0, no tau claimed. Measured: real
# corpus 93-112 dB, synthetic impulse+echo 83 dB, impulse buried in noise 37 dB, white noise
# 16 dB.
ARRIVAL_CREST_FLOOR_DB = 20.0

# Analysis window around the located direct arrival, as a multiple of the search window's
# upper edge (plus a short pre-arrival lead), keeping room decay out of the statistic. At the
# 800 us default this spans ~3.2 ms, inside the ~7 ms first-reflection gate of the JTS3 room.
ECHO_WINDOW_SPAN_FACTOR = 4.0
ECHO_WINDOW_PRE_S = 0.0005

# Minimum frequency bins in the analysis band; below this the detrend+cepstrum are not
# meaningful and the detector reports no confidence rather than a number.
MIN_ECHO_BAND_BINS = 16

# Cepstral concentration: search-window peak / L2 norm of the cepstrum above the search floor,
# bounded [0, 1] and scale-free. Maps linearly to a confidence factor between these two knots.
# Measured: real corpus 0.63-0.65, synthetic echoes 0.67-0.81, negative controls 0.23-0.50.
CONCENTRATION_LO = 0.30
CONCENTRATION_HI = 0.70

# Corroboration: relative disagreement between the two independent tau estimators (cepstral
# peak, analytic-envelope secondary arrival). Full credit at or below TIGHT, zero at or above
# LOOSE. Measured: real corpus 1.7-3.1%, synthetic echoes 0.1-1.9%, negative controls 4.4-73%.
CORROBORATION_TIGHT = 0.05
CORROBORATION_LOOSE = 0.30

# Reported strength when no credible secondary arrival was reported — a finite floor, not
# -inf, so the field stays arithmetic-safe. Meaningless unless confidence > 0.
STRENGTH_FLOOR_DB = -120.0

# Edge-proximity rejection margin, in quefrency steps (EchoDiagnostic.resolution_us), applied
# at the search window's **lower** edge only. A surviving candidate this close to search_us[0]
# is refused rather than reported.
#
# An echo *below* the window aliases upward onto the bottom of the window, where both searches
# begin, so the two aliased estimates agree with each other and the confident number is still
# wrong. Measured (10 positions, true delays 150-400 us): a 300-800 us window produced
# estimates 2-18 us above its lower edge from 150/178 us echoes at confidence 0.68-1.00, and
# the cloud read as geometry_locked.
#
# One quefrency step is the interval over which the cepstral estimator cannot distinguish "at
# the edge" from "below the edge" — the smallest margin closing the aliasing path, the largest
# not manufacturing a dead zone. No upper-edge equivalent: nothing aliases DOWN onto it, and an
# above-window echo is already rejected by the window contract.
#
# Does not make a raised window safe: a cepstral rahmonic can land anywhere inside one, at
# neither edge. See ``RAHMONIC_MARGIN``.
WINDOW_EDGE_MARGIN_STEPS = 1.0

# How close to a whole sample a window edge may sit before ``_ceil_samples``/``_floor_samples``
# treat it as *being* that sample, turning ``search_us`` into a closed range of sample delays.
#
# Not float noise: a caller expressing a sample-aligned edge in microseconds must write a
# decimal. Eight samples at 48 kHz is 166.6666...us, so a bare ``ceil`` on ``166.6667``
# (8.0000016 samples) excludes sample 8 for a 0.000033 us overshoot; ``floor`` mirrors at the
# upper edge.
#
# 1e-3 of a sample is 20.833 ns at 48 kHz — covers a two-decimal edge 4x over and a
# four-decimal one 400x over, while ~3400x finer than the detector's own ~71.43 us resolution.
WINDOW_EDGE_SNAP_SAMPLES = 1e-3

# Rahmonic screen — closes the mechanism the edge margin above cannot reach. A comb's cepstrum
# repeats at 2*tau, 3*tau, ..., so a window excluding the true delay can still contain a
# *rahmonic* of it, at an arbitrary place in the window rather than at an edge.
#
# A candidate surviving the window and edge checks is refused when the strongest
# detrended-cepstrum peak *below* it (from ``RAHMONIC_FLOOR_STEPS`` up to, not including, the
# candidate's own bin) exceeds it by more than ``RAHMONIC_MARGIN``. A rahmonic is by
# construction weaker than its fundamental, so testing the ratio directly needs no assumption
# it's an integer — worst measured case is 3.65x, not 3x, so a tau/2, tau/3 re-test would have
# missed it.
#
# Not *sufficient*: an honest in-window echo under a stronger unrelated *earlier* reflection
# presents the same picture, and the two populations' ratios interleave rather than banding
# apart (see ``DEFAULT_ECHO_SEARCH_US``). The screen resolves toward refusing — fail-safe: the
# caller loses a measurement rather than getting a delay ~3x wrong.
#
# 1.65 sits in a measured gap between two synthetic populations, by what the PRE-screen
# detector did:
#
# * **True positives — 2908 readings** (impulse+echo and shaped-response IRs, tau 200-770 us x
#   r 0.10-0.75, 13 windows, admitted unrefused within 15% of truth): ratio peaks at **0.9955**
#   (the candidate's own main-lobe shoulder, not low-quefrency leakage).
# * **Wrong readings — 439 readings** (same families, tau 100-455 us, 11 windows EXCLUDING the
#   true delay, admitted confident and >15% off): ratio bottoms out at **2.7899**.
#
# 1.65 is 1.66x above the ceiling and 1.69x below the floor (either side of the 1.667 geometric
# centre), rejecting 439/439 wrong and 0/2908 right. A margin of exactly 1.0 would separate
# them too, but with only 0.5% headroom over an ordinary sub-bin geometry.
#
# ``RAHMONIC_FLOOR_STEPS = 1``: excludes exactly the zero-lag bin, whose magnitude is residual
# DC rather than a ripple period. Not a rescue from leakage — a floor of 0 gives the same
# 0.9955 ceiling.
RAHMONIC_FLOOR_STEPS = 1
RAHMONIC_MARGIN = 1.65

# Signal-presence screen — how far the analysis band may sit below the caller's DECLARED
# passband before the detector refuses to read it. Nothing else checks ``band_hz`` contains
# signal at all: the arrival-crest gate passes on a band-limited driver's IR even in pure
# filter stopband, and the two estimators then agree on a "ripple" in quantisation noise
# (measured on an electrical loopback: the woofer branch, LR4 lowpass at 2 kHz, searched in the
# 5-19 kHz default band returned tau=323.3 us, confidence=0.275, refusal="").
#
# Fires when the declared passband's level exceeds the analysis band's by more than this margin
# (``EchoDiagnostic.band_deficit_db``). ``None`` leaves the screen off.
#
# 25.0 dB sits near-centred in a measured 28.36 dB gap at the default band/window, over 22
# records: honest acoustic captures (16 records, ceiling 12.07 dB), stopband residue (3
# records, floor 40.43 dB, the loopback woofer against its 200-2000 Hz passband), in-band
# control (3 records, -0.17 to -0.05 dB, the loopback tweeter). All 16 honest records are the
# SAME speaker, so 12.07 dB is the number to watch against different HF rolloff hardware.
#
# **A caller must keep the analysis band clear of the speaker's crossover** — re-measured
# across six bands, the residue side fails at (2000, 19000) (deficit collapses to
# 18.21-18.23 dB), which `crossover_v2.verification.ECHO_BAND_HF_REGIME_FLOOR_HZ` (4000 Hz)
# makes true by construction. Not calibrated for a passband narrower than or overlapping the
# analysis band.
BAND_BELOW_PASSBAND_MARGIN_DB = 25.0

# Earlier-dominant-arrival dominance floor — how loud a below-window arrival must be, relative
# to the direct arrival, before the detector calls it **dominant** and refuses in its name.
# Without a level test, an echo-free impulse's own ringing looks like a local maximum too.
#
# Measured at the default band on ``earlier_arrival_db``, eleven raised windows x the 60-member
# impulse-with-no-echo family: must-not-fire (echo-free ringing) spans -32.1297 to -17.1365 dB
# across 658/660 readings; must-fire (S0 ground plane, n=3) reads -0.64/-2.01/-2.57 dB at
# 125-146 us, a mic capsule left proud of the floor.
#
# -10.0 leaves 7.43 dB below the ground-plane floor and 7.14 dB above the ringing ceiling — a
# 14.58 dB gap. Not merely a noise gate: the S0 main-leg desk cloud has a real below-window
# arrival at -14.66 to -15.71 dB on 4/10 positions, and all ten still detected the rim wave.
EARLIER_ARRIVAL_DOMINANCE_DB = -10.0

# Refusal vocabulary for EchoDiagnostic.refusal. Empty means the detector ran to completion
# (which may be a zero-confidence "found nothing credible"); non-empty means every estimate on
# the record is uninformative. Consumers gate on `refusal == ""`, never a specific slug.
REFUSAL_LOW_ARRIVAL_CREST = "low_arrival_crest"
REFUSAL_WINDOW_TOO_SHORT = "analysis_window_too_short"
REFUSAL_BAND_TOO_NARROW = "analysis_band_too_narrow"
REFUSAL_BAND_BELOW_PASSBAND = "band_below_passband"
REFUSAL_SEARCH_OUTSIDE_CEPSTRUM = "search_window_outside_cepstrum"
REFUSAL_NO_IN_WINDOW_ECHO = "no_in_window_echo"
REFUSAL_TAU_AT_WINDOW_LOWER_EDGE = "tau_at_window_lower_edge"
REFUSAL_RAHMONIC_OF_LOWER_DELAY = "rahmonic_of_lower_delay"
REFUSAL_EARLIER_DOMINANT_ARRIVAL = "earlier_dominant_arrival"
REFUSAL_ALL_ZERO_IR = "all_zero_ir"
REFUSAL_MALFORMED_IR = "malformed_ir"
REFUSAL_BAD_SAMPLE_RATE = "bad_sample_rate"
REFUSAL_BAD_BAND_HZ = "bad_band_hz"
REFUSAL_BAD_SIGNAL_BAND_HZ = "bad_signal_band_hz"
REFUSAL_BAD_SEARCH_US = "bad_search_us"
REFUSAL_DETECTOR_ERROR = "detector_error"

# --------------------------------------------------------------------------- #
# Geometry-lock tuning
# --------------------------------------------------------------------------- #

# An echo diagnostic counts toward the geometry verdict only at or above this confidence.
# Calibrated to the empty gap between measured populations: true positives (synthetic
# impulse+echo, tau 240-700 us, r 0.15-0.6) scored 0.916-1.000; 60 impulse-with-no-echo
# negative controls spanned 0.000-0.091, with 0/60 crossing this floor.
ECHO_CONFIDENCE_FLOOR = 0.5

# An echo diagnostic also counts only when its tau is at least this many quefrency steps
# (EchoDiagnostic.resolution_us) above zero. Below ~3 steps both estimators are inside the
# direct pulse's own skirt — unresolvable estimates pile up near the bottom of the window and
# look like agreement, which is how a dispersed cloud can read as falsely locked.
GEOMETRY_MIN_RESOLUTION_STEPS = 3.0

# The speaker's interference pattern is "geometry locked" when at least this fraction of
# confident per-position tau estimates fall within +-GEOMETRY_CLUSTER_TOLERANCE of their
# median. Position-stable tau means position-stable nulls, which spatial averaging cannot fill.
GEOMETRY_CLUSTER_FRACTION = 0.70
GEOMETRY_CLUSTER_TOLERANCE = 0.15

# A "cluster" needs at least two members: with one usable estimate, 100% trivially sits within
# any tolerance of its own median. Below this the verdict is "unknown", reported as not locked
# with an explicit reason — never a lock, since locking on no evidence actively misleads a
# household ("spread the mic further" when nothing was measured).
GEOMETRY_MIN_CONFIDENT = 2

GEOMETRY_LOCKED = "geometry_locked"
GEOMETRY_DISPERSED = "geometry_dispersed"
#: "usable", not "confident": n_confident survived all three admission rules (measured,
#: confident, resolvable), so a value naming only confidence would describe a different set.
GEOMETRY_UNKNOWN = "geometry_insufficient_usable_estimates"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionCapture:
    """One gated sweep captured at one mic position in the cloud.

    ``freqs_hz`` must be **linear**-spaced (``GRID_UNIFORMITY_RTOL``) since the smoothing
    kernel assumes it. ``magnitude_db`` must be *unsmoothed* — smoothing is applied once, after
    combining, since a per-capture pre-smooth would blur the interference nulls the screen
    exists to find. ``ir`` is optional (``None`` means no echo diagnostic, distinct from
    "measured and found nothing"). ``role`` is carried, never read by the combination — there
    is no ``weights=`` argument anywhere in this module — it only labels a number when
    :func:`position_residuals` reports it.
    """

    position_id: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    sample_rate: int
    ir: np.ndarray | None = None
    role: str = ""


@dataclass(frozen=True)
class EchoDiagnostic:
    """Discrete-echo detection for one capture. Detection only.

    Read ``refusal`` first, then ``confidence``. Non-empty ``refusal`` means every estimate
    below is uninformative; empty with ``confidence == 0.0`` means the detector ran and found
    nothing credible. ``tau_us``/``strength_db`` carry information only when ``refusal == ""``
    and ``confidence > 0``.

    Every level is dB relative to the direct arrival, every delay microseconds.
    ``STRENGTH_FLOOR_DB`` (level) and 0.0 (delay) both mean **not measured** — finite sentinels
    no real reading can collide with.

    ``tau_us`` is the reported answer, the only tau field with a window guarantee (0.0 or
    inside ``search_us``, clear of its lower edge by ``WINDOW_EDGE_MARGIN_STEPS``) — **always**
    the analytic-envelope estimate (~1-4% accurate on the calibration set), not quantised to
    ``resolution_us``. ``tau_cepstral_us``/``tau_envelope_us`` are each estimator's answer in
    isolation, **raw** and not window-guaranteed — kept unclamped on purpose, since a railed
    value hidden by a clamp is what made a dispersed cloud read as geometry-locked.

    ``resolution_us`` is the **cepstral corroborator's** quefrency step, ``1 / band_width`` —
    not the (far finer) granularity of ``tau_us``, but this module's trust floor
    (``WINDOW_EDGE_MARGIN_STEPS``, ``RAHMONIC_FLOOR_STEPS``, and
    :func:`assess_geometry`'s clustering floor are all written in these units).

    ``confidence`` is cepstral concentration times tau-estimator agreement, behind an
    arrival-crest gate that refuses outright rather than scoring down. ``corroboration``'s
    ``1.0`` means exactly one thing — the two estimators could not be compared — never
    "measured, and they disagreed completely"; the two late refusals
    (``tau_at_window_lower_edge``, ``rahmonic_of_lower_delay``) don't read this field.

    ``lower_peak_ratio`` makes a ``rahmonic_of_lower_delay`` refusal recomputable
    (``lower_peak_ratio > RAHMONIC_MARGIN``); reported on every record that reached the scan
    (corpus detections sit at 0.329-0.387). ``effective_floor_us`` is the delay below which
    THIS window cannot report an arrival at all (~191.4 us for the defaults), populated on
    every record including refusals. ``earlier_arrival_us``/``_db`` are the strongest genuine
    local maximum below ``search_us[0]`` — a reading, never a candidate; the dominance test
    reads ``earlier_arrival_db`` (``earlier_dominant_arrival`` is exactly
    ``earlier_arrival_db > EARLIER_ARRIVAL_DOMINANCE_DB``). ``band_deficit_db`` is how far the
    analysis band sits BELOW the declared passband (``band_below_passband`` is exactly
    ``band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB``); not measured when no
    ``signal_band_hz`` was declared or the passband covered no spectrum bin.
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
    lower_peak_us: float = 0.0
    lower_peak_ratio: float = 0.0
    effective_floor_us: float = 0.0
    earlier_arrival_us: float = 0.0
    earlier_arrival_db: float = STRENGTH_FLOOR_DB
    band_deficit_db: float = STRENGTH_FLOOR_DB


@dataclass(frozen=True)
class GeometryLock:
    """Whether the cloud's interference pattern is position-stable.

    ``locked`` True is the actionable case: nulls do not move between positions, so spatial
    averaging cannot fill them. *Not* a measurement failure — on a corpus captured repeatedly
    from one place it is the detector working. Never True on insufficient evidence.

    ``n_confident`` counts positions with a *usable* diagnostic (no refusal, confidence at or
    above ``confidence_floor``, tau at least ``GEOMETRY_MIN_RESOLUTION_STEPS`` steps above
    zero) — the set the clustering test ran on, not the raw non-``None`` count.

    ``thin_evidence`` is exactly ``n_confident == GEOMETRY_MIN_CONFIDENT and n_positions >=
    2 * GEOMETRY_MIN_CONFIDENT`` — **a cliff, not a gradient** (two usable estimates out of ten
    is thin, three is not; read ``n_confident``/``n_positions`` directly for a gradient).
    Disclosure, not rejection — structurally unreachable on ``GEOMETRY_UNKNOWN``, so it always
    qualifies a ``GEOMETRY_LOCKED``/``GEOMETRY_DISPERSED`` verdict.
    """

    locked: bool
    reason: str
    n_confident: int
    n_positions: int
    median_tau_us: float
    clustered_fraction: float
    tolerance: float
    confidence_floor: float
    thin_evidence: bool = False


@dataclass(frozen=True)
class PositionResidual:
    """How far ONE position sat from the combined curve, over one band.

    A residual large at EVERY position is broadband and ours (a role-level trim or model
    error, fixable); large at a single position is the room or placement. ``rms_db`` is RMS of
    ``per_position_diag_db - power_mean_diag_db``, ``None`` when the band selected no usable
    bin (an absence, never a fabricated zero). **The DIAGNOSTIC-smoothed pair, not raw** — on
    raw curves each position's own interference comb dominates, hiding a broadband defect
    (measured: a healthy 4-position cloud's raw spread swamped a +4 dB single-position
    offset). ``n_bins == 0`` with ``rms_db is None`` says the band was empty, not that the
    arithmetic failed.
    """

    position_id: str
    role: str
    rms_db: float | None
    n_bins: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "role": self.role,
            "rms_db": self.rms_db,
            "n_bins": self.n_bins,
        }


@dataclass(frozen=True)
class BandSpread:
    """Cross-position magnitude spread in one octave band — two numbers, two questions.

    ``sigma_db`` is the *level* spread: each position collapsed to one band level (linear
    power mean), then sample std dev (``ddof=1``) across positions — insensitive to comb
    structure by construction, since a band holds many comb periods. Large means the positions
    genuinely disagree about loudness (mic distance, gain, directivity), which averaging won't
    fix. ``max_sigma_db`` is the *structure* spread: the worst single bin's cross-position
    sigma, unsmoothed, riding comb nulls on purpose. ``max_sigma_db`` dwarfing ``sigma_db``
    means null-dominated at a few frequencies (decorrelation working); comparable means
    broadly noisy.
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

    ``freqs_hz`` is the shared canonical linear grid every curve lives on, after any
    ``MAX_ANALYSIS_BINS`` decimation — the grid actually computed on, not the captures' own.
    ``power_mean_db`` is the primary direct-sound estimator; ``median_db`` is the robustness
    cross-check that exists only to disagree, never a correction input. ``per_position_db`` is
    ``(n_positions, len(freqs_hz))``, **unsmoothed**, row *i* being ``position_ids[i]``'s
    magnitude on the shared grid. ``per_position_diag_db`` mirrors it at the diagnostic
    fraction, row-for-row — the SAME smoothing construction as the combined curves, costing
    one ``smooth_fractional_octave`` pass per position (the combiner's dominant term, 40% of a
    3.45s call on a 10-position 16384-bin cloud on a laptop), paid unconditionally.

    ``excluded`` is a per-bin mask, True where the two diagnostic curves disagree by more than
    ``flag_threshold_db``; excluded from both correction and pass/fail. ``excluded_bands_hz``
    merges it into intervals with no gap-bridging.

    ``per_position_echo`` is index-aligned with ``position_ids``; ``None`` means *strictly* "no
    IR was supplied" — a capture that supplied one always gets an :class:`EchoDiagnostic`, even
    a refused or zero-confidence one, so all three states stay distinguishable. ``geometry``'s
    ``locked`` bit has no mirror field, so it can never drift from this record. ``band_spread``
    is empty below two positions. ``signal_band_hz`` is ``None`` when the signal-presence
    screen did not run — a ``band_below_passband`` refusal is only interpretable against it.
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
    signal_band_hz: tuple[float, float] | None = None
    # Empty is not a legal value from :func:`combine_positions`, which always populates both —
    # it's what a hand-built or deserialised record carries when curves were never retained.
    per_position_db: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    per_position_diag_db: np.ndarray = field(
        default_factory=lambda: np.empty((0, 0))
    )
    #: ``()`` on the same terms; ``combine_positions`` always populates it (``""`` for no role).
    position_roles: tuple[str, ...] = ()


def position_residuals(
    combined: CombinedResponse, *, band_hz: tuple[float, float] | None = None,
) -> tuple[PositionResidual, ...]:
    """One :class:`PositionResidual` per position, in input order.

    ``band_hz`` is the caller's trusted band — the trusted floor and mic-tier ceiling are
    session facts this module cannot know; ``None`` uses the whole shared grid, an honest
    default rather than a trust claim. Bins the combination EXCLUDED are dropped too, so
    interference structure doesn't read as a placement error. ``()`` when the record retained
    no per-position curves.
    """

    stacked = np.asarray(combined.per_position_diag_db, dtype=float)
    reference = np.asarray(combined.power_mean_diag_db, dtype=float)
    ids = tuple(combined.position_ids)
    if not ids or stacked.ndim != 2 or stacked.shape[0] != len(ids):
        return ()
    if stacked.shape[1] != reference.size:
        return ()

    grid = np.asarray(combined.freqs_hz, dtype=float)
    keep = np.ones(grid.size, dtype=bool)
    if band_hz is not None:
        keep &= (grid >= float(band_hz[0])) & (grid <= float(band_hz[1]))
    excluded = np.asarray(combined.excluded, dtype=bool)
    if excluded.size == grid.size:
        keep &= ~excluded

    roles = tuple(combined.position_roles)
    rows: list[PositionResidual] = []
    for index, position_id in enumerate(ids):
        deviation = stacked[index] - reference
        usable = keep & np.isfinite(deviation)
        n_bins = int(np.count_nonzero(usable))
        rows.append(PositionResidual(
            position_id=str(position_id),
            role=str(roles[index]) if index < len(roles) else "",
            rms_db=(
                float(np.sqrt(np.mean(deviation[usable] ** 2)))
                if n_bins else None
            ),
            n_bins=n_bins,
        ))
    return tuple(rows)


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
    """Coarsest spacing (interpolating finer would invent resolution the measurement never
    had), common support (``np.interp`` would otherwise flat-extrapolate past a band edge).
    Identity when every capture already shares one grid — the ordinary case."""
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
    # Float accumulation can push the last point past the common support; clamp rather than
    # let np.interp edge-hold silently.
    grid[-1] = min(grid[-1], f_hi)
    grid.flags.writeable = False
    return grid


def _decimate_to_analysis_grid(
    grid: np.ndarray, stacked: np.ndarray, *, max_bins: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Block-average a too-fine analysis grid down to ``max_bins`` in **linear power**, so
    decimation composes with the power mean instead of biasing it (subsampling would alias a
    combed curve onto whichever bins land on peaks or nulls; see ``MAX_ANALYSIS_BINS``).
    ``max_bins=None`` reads that constant fresh per call, staying monkeypatchable.

    Blocks are fixed-width so the decimated grid stays exactly linear; a trailing partial
    block is dropped (at most ``block - 1`` bins lost) rather than averaged at a different
    centre. Identity when the grid is already within the bound.
    """
    if max_bins is None:
        max_bins = MAX_ANALYSIS_BINS
    n_bins = int(grid.size)
    if n_bins <= max_bins:
        return grid, stacked
    block = -(-n_bins // max_bins)  # ceil division
    n_blocks = n_bins // block
    kept = n_blocks * block
    coarse_grid = grid[:kept].reshape(n_blocks, block).mean(axis=1)
    power = 10.0 ** (stacked[:, :kept] / 10.0)
    coarse_stacked = 10.0 * np.log10(
        power.reshape(stacked.shape[0], n_blocks, block).mean(axis=2)
    )
    coarse_grid.flags.writeable = False
    return coarse_grid, coarse_stacked


def decimate_curve_to_analysis_grid(
    grid: np.ndarray, magnitude_db: np.ndarray, *, max_bins: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """The 1-D public face of :func:`_decimate_to_analysis_grid` — same rule, one caller shape
    apart: the combiner decimates a STACK of positions, a caller holding a single curve needs
    the identical block-average at the same grid density. Identity when already within
    ``max_bins``."""
    coarse_grid, coarse_stacked = _decimate_to_analysis_grid(
        grid, np.asarray(magnitude_db, dtype=float).reshape(1, -1), max_bins=max_bins,
    )
    return coarse_grid, coarse_stacked[0]


def merged_true_intervals(
    freqs_hz: np.ndarray, mask: np.ndarray
) -> tuple[tuple[float, float], ...]:
    """Contiguous ``True`` runs of ``mask`` as merged ``(f_lo, f_hi)`` intervals. The single
    owner of this rule; :mod:`jasper.active_speaker.flat_spec` imports it rather than keeping
    its own copy. Adjacency is by **array index**, valid only when ``freqs_hz`` is ascending
    (enforced upstream by both callers, not re-checked here). No gap-bridging.
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
    """Textbook Hilbert construction (zero negative frequencies, double positive), numpy-only
    since scipy is not a JTS dependency."""
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
    """Mirrors ``program_analysis._n_fft_for`` with a lower floor: this module's analysis
    window is a few milliseconds, not a whole capture."""
    return max(4096, 1 << (max(length, 1) - 1).bit_length())


def _bandpass(signal: np.ndarray, lo_hz: float, hi_hz: float, sample_rate: int) -> np.ndarray:
    """Matches the reference forensics script's ``bp`` (comb_forensics3.py). Zero-phase, so
    it does not shift arrivals."""
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


def _ceil_samples(delay_s: float, sample_rate: int) -> int:
    """First whole sample whose delay is at or above ``delay_s``."""
    return int(math.ceil(delay_s * sample_rate - WINDOW_EDGE_SNAP_SAMPLES))


def _floor_samples(delay_s: float, sample_rate: int) -> int:
    """Last whole sample whose delay is at or below ``delay_s``."""
    return int(math.floor(delay_s * sample_rate + WINDOW_EDGE_SNAP_SAMPLES))


class EchoInputError(ValueError):
    """A ``ValueError`` subclass with a machine-readable ``slug`` attached at the raise site,
    so :func:`combine_positions` can turn the failure into a refused diagnostic without
    matching on message text."""

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
    corroboration: float = 1.0,
    lower_peak_us: float = 0.0,
    lower_peak_ratio: float = 0.0,
    effective_floor_us: float = 0.0,
    earlier_arrival_us: float = 0.0,
    earlier_arrival_db: float = STRENGTH_FLOOR_DB,
    band_deficit_db: float = STRENGTH_FLOOR_DB,
) -> EchoDiagnostic:
    """The "detector declined" constructor, only that — the other zero-confidence outcome,
    "ran and found nothing credible", carries ``refusal == ""`` and is built inline in
    :func:`detect_echo`.

    ``corroboration`` defaults to 1.0 because on most refusal paths the two estimators
    genuinely COULD NOT BE COMPARED; the two late refusals (edge-proximity, rahmonic) fire
    AFTER the comparison and pass the measured value through instead.
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
        corroboration=corroboration,
        arrival_crest_db=arrival_crest_db,
        lower_peak_us=lower_peak_us,
        lower_peak_ratio=lower_peak_ratio,
        effective_floor_us=effective_floor_us,
        earlier_arrival_us=earlier_arrival_us,
        earlier_arrival_db=earlier_arrival_db,
        band_deficit_db=band_deficit_db,
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
    signal_band_hz: tuple[float, float] | None = None,
) -> EchoDiagnostic:
    """Detect a discrete early echo in one impulse response. Detection only, never removal.

    Two independent estimators divide the labour: the **cepstrum** answers *is there a
    periodic ripple, at roughly what quefrency* (detects well, localises coarsely — resolution
    ~71 us for the 5-19 kHz default band); the **band-limited analytic envelope** answers
    *where and how loud* at sample resolution with parabolic refinement (localises well but
    alone will happily find a peak in noise). ``tau_us`` is therefore **always** the envelope
    estimate; ``confidence`` is cepstral concentration times corroboration, behind an
    arrival-crest gate that refuses outright rather than scoring down.

    **``search_us`` is a rejection contract, not a clamp.** A candidate whose refined delay
    lands outside the window is rejected, never pulled back to the edge — every position's
    railed estimate would rail to the *same* edge, falsely reading a dispersed cloud as
    ``geometry_locked``. A non-zero ``confidence`` requires **both** estimators in-window; if
    neither survives, ``no_in_window_echo``.

    Three further rules refuse candidates the window alone would admit: the **lower edge**
    (``WINDOW_EDGE_MARGIN_STEPS`` — an echo below the window aliases upward onto it, so a
    candidate within that margin of ``search_us[0]`` gets ``tau_at_window_lower_edge``; no
    upper-edge equivalent, nothing aliases down); the **rahmonic screen**
    (``RAHMONIC_MARGIN`` — a window excluding the true delay can still contain a RAHMONIC of
    it, refused as ``rahmonic_of_lower_delay`` with evidence in ``lower_peak_us``/
    ``lower_peak_ratio``; also catches an honest echo under a stronger unrelated EARLIER
    reflection, see ``DEFAULT_ECHO_SEARCH_US``); and the **signal-presence screen**
    (``BAND_BELOW_PASSBAND_MARGIN_DB`` — with no signal in ``band_hz`` the crest gate still
    passes on filter stopband residue, so a declared ``signal_band_hz`` enables
    ``band_below_passband``; ``None`` leaves it off).

    **The earlier-dominant-arrival disclosure** (``earlier_dominant_arrival``) replaces an
    uninformative zero with a named refusal when the envelope's own answer lands below
    ``search_us``, a genuine local maximum is measured there, and it beats
    ``EARLIER_ARRIVAL_DOMINANCE_DB`` re the direct arrival — a fallback after every other
    refusal; ``earlier_arrival_us``/``_db`` disclose it either way.

    The IR is windowed internally to the early-arrival region (``ECHO_WINDOW_SPAN_FACTOR``),
    so a full deconvolved IR may be passed.

    ``band_hz`` targets the HF region where a directivity-weighted bounce combs most visibly
    (upper edge clipped to Nyquist). ``signal_band_hz`` is the driver's **declared** passband,
    or ``None`` to skip the signal-presence screen. ``search_us`` defaults to an early boundary
    bounce (~120 us) up to ~800 us, the only window with a swept false-lock record — prefer it.

    **Resolution floor**: both estimators degrade as tau approaches
    :attr:`EchoDiagnostic.resolution_us` (~71 us default). The bottom
    ``WINDOW_EDGE_MARGIN_STEPS`` of any window is refused outright (default effective floor
    ~191 us, not the stated 120 us, disclosed as ``effective_floor_us``); independently,
    :func:`assess_geometry` will not cluster below ``GEOMETRY_MIN_RESOLUTION_STEPS *
    resolution_us`` (~214 us) measured from zero delay. The target bounce (~300 us) sits above
    both. Widening ``band_hz`` shrinks ``resolution_us``, lowering both floors together.

    Non-empty ``refusal`` means the detector declined; ``confidence == 0.0`` with empty
    ``refusal`` means it ran and found nothing credible — either way ``tau_us`` is 0.0. Raises
    ``EchoInputError`` on an empty/non-finite IR, non-positive sample rate, or a degenerate
    band/signal band/search window.
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
    signal_band: tuple[float, float] | None = None
    if signal_band_hz is not None:
        sig_lo_hz = float(signal_band_hz[0])
        sig_hi_hz = min(float(signal_band_hz[1]), sample_rate / 2.0)
        if not 0.0 < sig_lo_hz < sig_hi_hz:
            raise EchoInputError(
                REFUSAL_BAD_SIGNAL_BAND_HZ,
                f"signal_band_hz must satisfy 0 < lo < hi <= Nyquist, got "
                f"{signal_band_hz} at sample_rate={sample_rate}",
            )
        signal_band = (sig_lo_hz, sig_hi_hz)
    search_lo_s = float(search_us[0]) * 1e-6
    search_hi_s = float(search_us[1]) * 1e-6
    if not 0.0 < search_lo_s < search_hi_s:
        raise EchoInputError(
            REFUSAL_BAD_SEARCH_US, f"search_us must satisfy 0 < lo < hi, got {search_us}"
        )

    # Quefrency step of the band actually used (after Nyquist clipping); reported on every
    # diagnostic, including refusals.
    resolution_us = 1e6 / (hi_hz - lo_hz)
    # Edge-margin dead zone — one derivation, two consumers: effective_floor_us disclosure and
    # the tau_at_window_lower_edge check further down.
    edge_margin_us = WINDOW_EDGE_MARGIN_STEPS * resolution_us
    effective_floor_us = search_lo_s * 1e6 + edge_margin_us

    # --- 1. Locate the direct arrival, and gate on it existing at all. ---
    # argmax|ir| agrees with the band-limited envelope peak to within a couple of samples on
    # every measured case; diverges only for signals with no arrival, which the crest gate
    # rejects anyway.
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
            effective_floor_us=effective_floor_us,
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
            effective_floor_us=effective_floor_us,
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
            effective_floor_us=effective_floor_us,
        )

    # --- 3a. Signal-presence screen (only when a passband was declared). ---
    # Both levels come off ``spectrum`` (already computed above), costing no extra FFT, in
    # **linear power**. Placed before the cepstrum so stopband residue never dresses up as a
    # delay. See ``BAND_BELOW_PASSBAND_MARGIN_DB``.
    band_deficit_db = STRENGTH_FLOOR_DB
    if signal_band is not None:
        signal_mask = (freqs >= signal_band[0]) & (freqs <= signal_band[1])
        # A passband narrower than one FFT bin measures nothing; fail-open on purpose —
        # refusing on a band the detector couldn't evaluate would be a verdict about the
        # caller's arithmetic, not the capture.
        if np.any(signal_mask):
            band_power = spectrum**2
            band_deficit_db = 10.0 * float(
                np.log10(
                    np.mean(band_power[signal_mask]) / np.mean(band_power[band])
                )
            )
            if band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB:
                return _refused(
                    REFUSAL_BAND_BELOW_PASSBAND,
                    resolution_us=resolution_us,
                    arrival_crest_db=crest_db,
                    effective_floor_us=effective_floor_us,
                    band_deficit_db=band_deficit_db,
                )

    log_mag = 20.0 * np.log10(spectrum[band])
    # Fit against a conditioned [-1, 1] abscissa so the driver's own broad shape does not leak
    # into low quefrencies.
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
            effective_floor_us=effective_floor_us,
            band_deficit_db=band_deficit_db,
        )

    # Peak within the search window, refined against the FULL cepstrum: a peak on the first or
    # last search bin still has real neighbours outside the slice. NOT clamped back into the
    # window — if refinement walks it out, the candidate is honestly rejected below.
    search_indices = np.flatnonzero(in_search)
    peak = int(search_indices[np.argmax(cepstrum[in_search])])
    quefrency_step = float(quefrency[1] - quefrency[0])
    tau_cepstral = float(
        quefrency[peak] + _parabolic_offset(cepstrum, peak) * quefrency_step
    )
    cepstral_in_window = search_lo_s <= tau_cepstral <= search_hi_s
    baseline = float(np.linalg.norm(cepstrum[above_floor]))
    concentration = float(cepstrum[peak] / baseline) if baseline > 0.0 else 0.0

    # Rahmonic screen's evidence, measured here so every downstream record carries it. Extends
    # below ``search_lo_s`` on purpose: the point is to see the echo the window excluded.
    lower_peak_us = 0.0
    lower_peak_ratio = 0.0
    if peak > RAHMONIC_FLOOR_STEPS and cepstrum[peak] > 0.0:
        lower_index = RAHMONIC_FLOOR_STEPS + int(
            np.argmax(cepstrum[RAHMONIC_FLOOR_STEPS:peak])
        )
        lower_peak_us = float(quefrency[lower_index]) * 1e6
        lower_peak_ratio = float(cepstrum[lower_index] / cepstrum[peak])

    # --- 4. Envelope estimator. ---
    envelope = _analytic_envelope(_bandpass(segment, lo_hz, hi_hz, sample_rate))
    main = int(np.argmax(envelope))
    # Window's edges in samples, defined once: ``first``/``last`` are the closed window's
    # sample delays (ceil at bottom, floor at top). ``first`` is also the below-window scan's
    # stop, so no sample is simultaneously an in-window candidate and a "below-window arrival".
    first = main + _ceil_samples(search_lo_s, sample_rate)
    last = min(envelope.size - 2, main + _floor_samples(search_hi_s, sample_rate))
    tau_envelope = 0.0
    envelope_strength_db = STRENGTH_FLOOR_DB
    if last > first + 1:
        local = first + int(np.argmax(envelope[first : last + 1]))
        tau_envelope = (local + _parabolic_offset(envelope, local) - main) / sample_rate
        envelope_strength_db = 20.0 * float(
            np.log10(max(float(envelope[local]), 1e-15) / max(float(envelope[main]), 1e-15))
        )

    # Strongest genuine local maximum **below** the window — a distinct arrival the search
    # excludes. Purely a reading, never a candidate. Requiring a genuine local maximum is what
    # makes it "a separate arrival" rather than a point on the direct pulse's own decay:
    # measured at the default band/window, 0 of 60 impulse-with-no-echo negative controls has
    # one while all three S0 ground-plane positions do. Stops at ``first``, the same boundary
    # the envelope's candidate range starts at — no overlap, no gap.
    earlier_arrival_us = 0.0
    earlier_arrival_db = STRENGTH_FLOOR_DB
    below_stop = first
    if below_stop > main + 1:
        early = np.arange(main + 1, min(below_stop, envelope.size - 1))
        early = early[
            (envelope[early] >= envelope[early - 1])
            & (envelope[early] >= envelope[early + 1])
        ]
        if early.size:
            earlier_index = int(early[np.argmax(envelope[early])])
            earlier_arrival_us = (earlier_index - main) / sample_rate * 1e6
            earlier_arrival_db = 20.0 * float(
                np.log10(
                    max(float(envelope[earlier_index]), 1e-15)
                    / max(float(envelope[main]), 1e-15)
                )
            )
    # Sub-sample refinement can push an edge peak past the boundary; same window check as the
    # cepstrum's.
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
            lower_peak_us=lower_peak_us,
            lower_peak_ratio=lower_peak_ratio,
            effective_floor_us=effective_floor_us,
            earlier_arrival_us=earlier_arrival_us,
            earlier_arrival_db=earlier_arrival_db,
            band_deficit_db=band_deficit_db,
        )

    if cepstral_in_window and envelope_in_window:
        corroboration = abs(tau_envelope - tau_cepstral) / max(tau_cepstral, 1e-12)
    else:
        # A rejected candidate corroborates nothing. Comparing against it
        # anyway is how a railed estimate manufactures agreement.
        corroboration = 1.0

    # A candidate that survived the window but hugs its lower edge is an
    # aliased below-window echo as readily as an in-window one, and the two
    # cannot be told apart from inside this band — refuse rather than pick.
    # Both surviving candidates are checked, not just the reported one: an
    # edge-hugging cepstral peak corroborates an edge-hugging envelope peak
    # into high confidence, which is the exact mechanism being closed.
    # Corroboration is computed *above* this check so the refusal can carry
    # what was actually measured rather than the incomparable marker.
    #
    # The same ``edge_margin_us`` the disclosed ``effective_floor_us`` was
    # built from, in seconds — not a second derivation of it.
    edge_margin_s = edge_margin_us * 1e-6
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
            corroboration=float(corroboration),
            lower_peak_us=lower_peak_us,
            lower_peak_ratio=lower_peak_ratio,
            effective_floor_us=effective_floor_us,
            earlier_arrival_us=earlier_arrival_us,
            earlier_arrival_db=earlier_arrival_db,
            band_deficit_db=band_deficit_db,
        )

    # A candidate that survived the window *and* the edge margin can still be
    # a cepstral rahmonic of an echo below the window: a comb repeats at
    # 2*tau, 3*tau, ..., so the excluded fundamental puts a copy of itself at
    # an arbitrary place inside a raised window, where no edge rule can reach
    # it. A rahmonic is always weaker than its own fundamental, so the
    # signature is a much stronger peak at a lower quefrency — on the
    # documented false-lock windows the estimates this screen catches are
    # refused at 21.5-78.8x the candidate, with that stronger peak landing
    # within half a quefrency step of the position's true delay. See
    # ``RAHMONIC_MARGIN`` for the calibration.
    #
    # The signature is necessary but not sufficient, and this is where the
    # detector accepts a known cost: an honest in-window echo under a
    # stronger unrelated *earlier* reflection looks identical from one
    # record and is refused as well. That is bounded to raised windows and
    # fails safe — see ``DEFAULT_ECHO_SEARCH_US``. The refusal carries evidence
    # rather than asserting it: ``lower_peak_us`` says where the stronger
    # peak is, ``lower_peak_ratio`` says by how much it wins.
    if lower_peak_ratio > RAHMONIC_MARGIN:
        return _refused(
            REFUSAL_RAHMONIC_OF_LOWER_DELAY,
            resolution_us=resolution_us,
            arrival_crest_db=crest_db,
            tau_cepstral_us=tau_cepstral * 1e6,
            tau_envelope_us=tau_envelope * 1e6,
            concentration=concentration,
            corroboration=float(corroboration),
            lower_peak_us=lower_peak_us,
            lower_peak_ratio=lower_peak_ratio,
            effective_floor_us=effective_floor_us,
            earlier_arrival_us=earlier_arrival_us,
            earlier_arrival_db=earlier_arrival_db,
            band_deficit_db=band_deficit_db,
        )

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
        # "Ran, found nothing credible" is the wrong answer in one nameable state: the
        # envelope's answer landed BELOW the window, so three conditions (envelope below
        # search_lo_s; a genuine arrival measured there; loud enough per
        # EARLIER_ARRIVAL_DOMINANCE_DB — not decoration, else the envelope's own ringing
        # qualifies) name it instead of falling through to the honest nothing-found below.
        # Rejected alternative: let the envelope skip the interloper for the best IN-WINDOW
        # arrival — measured, and it re-opens the false-lock hazard via cepstral RAHMONICs.
        if (
            0.0 < tau_envelope < search_lo_s
            and earlier_arrival_us > 0.0
            and earlier_arrival_db > EARLIER_ARRIVAL_DOMINANCE_DB
        ):
            return _refused(
                REFUSAL_EARLIER_DOMINANT_ARRIVAL,
                resolution_us=resolution_us,
                arrival_crest_db=crest_db,
                tau_cepstral_us=tau_cepstral * 1e6,
                tau_envelope_us=tau_envelope * 1e6,
                concentration=concentration,
                corroboration=float(corroboration),
                lower_peak_us=lower_peak_us,
                lower_peak_ratio=lower_peak_ratio,
                effective_floor_us=effective_floor_us,
                earlier_arrival_us=earlier_arrival_us,
                earlier_arrival_db=earlier_arrival_db,
                band_deficit_db=band_deficit_db,
            )
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
            lower_peak_us=lower_peak_us,
            lower_peak_ratio=lower_peak_ratio,
            effective_floor_us=effective_floor_us,
            earlier_arrival_us=earlier_arrival_us,
            earlier_arrival_db=earlier_arrival_db,
            band_deficit_db=band_deficit_db,
        )

    # The envelope estimate is the answer, unconditionally: getting here needs confidence > 0,
    # which needs both-in-window, so the envelope is in-window by construction.
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
        lower_peak_us=lower_peak_us,
        lower_peak_ratio=lower_peak_ratio,
        effective_floor_us=effective_floor_us,
        earlier_arrival_us=earlier_arrival_us,
        earlier_arrival_db=earlier_arrival_db,
        band_deficit_db=band_deficit_db,
    )


# --------------------------------------------------------------------------- #
# Geometry lock
# --------------------------------------------------------------------------- #


def usable_echo_estimates(
    echoes: Sequence[EchoDiagnostic | None],
    *,
    confidence_floor: float = ECHO_CONFIDENCE_FLOOR,
) -> tuple[EchoDiagnostic, ...]:
    """The per-position diagnostics that count as evidence, in input order. The three
    admission rules (measured, confident, resolvable) are explained once, in
    :func:`assess_geometry`'s docstring; shared by that function and
    :mod:`jasper.audio_measurement.interference_nulls`'s arrival corroboration. Returns the
    diagnostics themselves, not just taus, since the null gate also needs ``strength_db``.
    """
    return tuple(
        e
        for e in echoes
        if e is not None
        and e.refusal == ""
        and e.confidence >= confidence_floor
        and e.tau_us > 0.0
        and e.tau_us >= GEOMETRY_MIN_RESOLUTION_STEPS * e.resolution_us
    )


def assess_geometry(
    echoes: Sequence[EchoDiagnostic | None],
    *,
    confidence_floor: float = ECHO_CONFIDENCE_FLOOR,
    tolerance: float = GEOMETRY_CLUSTER_TOLERANCE,
    min_fraction: float = GEOMETRY_CLUSTER_FRACTION,
) -> GeometryLock:
    """Are the cloud's interference nulls position-stable?

    ``locked`` when at least ``min_fraction`` of the *usable* per-position tau estimates
    (``n_confident`` counts them) fall within ``tolerance`` (relative) of their median. A
    stable tau means a stable null ladder that spatial averaging cannot fill — the honest
    consumer response is to ask the user to spread the mic, or conclude the bounce is
    speaker-fixed diffraction, letting the exclusion screen carry the weight instead.

    **What counts as usable evidence** (implemented once in :func:`usable_echo_estimates`) —
    each excluded class would otherwise produce a *false* lock a different way: (1)
    ``refusal == ""`` (a refusal's ``tau_us`` is 0.0, and zeros cluster perfectly); (2)
    ``confidence >= confidence_floor``; (3) ``tau_us > 0`` **and** ``tau_us >=
    GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us`` (below ~3 quefrency steps both estimators
    sit inside the direct pulse's own skirt and read low, collapsing a near-floor cloud toward
    one unresolvable value that looks locked but is merely unmeasurable; the explicit ``> 0``
    guards a hand-built record with ``resolution_us == 0``, which :func:`detect_echo` never
    emits but would otherwise clear a zero threshold with a zero tau).

    Fewer than ``GEOMETRY_MIN_CONFIDENT`` usable estimates is reported not-locked with
    ``GEOMETRY_UNKNOWN`` — never a lock, since a single estimate trivially "clusters" and
    locking on no evidence tells a household to move the mic for nothing.

    **This function is only as honest as the diagnostics it is handed** — the three rules
    screen UNUSABLE evidence, not usable-looking-and-wrong evidence. The worked example is the
    rahmonic class, which passes all three and clusters with itself; that fix lives in the
    detector (``RAHMONIC_MARGIN``).

    ``n_confident`` is the usable set's size; ``thin_evidence`` qualifies — never withholds — a
    verdict resting on the bare minimum of it.
    """
    n_positions = len(echoes)
    taus = np.array(
        [e.tau_us for e in usable_echo_estimates(echoes, confidence_floor=confidence_floor)],
        dtype=float,
    )
    n_confident = int(taus.size)
    # Evidence quality, independent of the verdict below (:attr:`GeometryLock.thin_evidence`).
    # Passed on the GEOMETRY_UNKNOWN return too (where it can't be True) for one derivation.
    thin_evidence = (
        n_confident == GEOMETRY_MIN_CONFIDENT
        and n_positions >= 2 * GEOMETRY_MIN_CONFIDENT
    )
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
            thin_evidence=thin_evidence,
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
        thin_evidence=thin_evidence,
    )


# --------------------------------------------------------------------------- #
# Combiner
# --------------------------------------------------------------------------- #


def _band_spread(freqs: np.ndarray, stacked: np.ndarray) -> tuple[BandSpread, ...]:
    """Octave-band cross-position spread from raw per-position curves. Deliberately
    **unsmoothed**: a band-power average gives the same statistic directly, without one
    ``smooth_fractional_octave`` pass per position. See :class:`BandSpread`."""
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
        # One level per position: band energy in power, then dB.
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
    signal_band_hz: tuple[float, float] | None,
) -> EchoDiagnostic | None:
    """One position's echo diagnostic, or ``None`` when it supplied no IR.

    A malformed IR is one position's problem, never the whole cloud's: the detector's
    ``ValueError`` becomes a refused diagnostic carrying the reason (from
    :class:`EchoInputError`'s slug, never a parsed message string), so ten good captures plus
    one all-zero IR still combine. Only per-*capture* trouble reaches here — config-shaped
    failures (band/search window/sample rate) are rejected up front by
    :func:`combine_positions`/``_validate_capture``. Reports ``resolution_us``/
    ``effective_floor_us`` as 0.0 — the detector raised before either was known.
    """
    if capture.ir is None:
        return None
    try:
        return detect_echo(
            capture.ir,
            capture.sample_rate,
            band_hz=band_hz,
            search_us=search_us,
            signal_band_hz=signal_band_hz,
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
    signal_band_hz: tuple[float, float] | None = None,
) -> CombinedResponse:
    """Combine a cloud of position captures into one direct-sound estimate.

    See the module docstring for the two complementary blind spots of the honesty screen and
    the geometry flag. The **power mean** is the primary estimator; the **dB median** exists
    to disagree with it — where they disagree by more than ``flag_threshold_db`` at the
    diagnostic fraction, the bin is interference-dominated and reported for exclusion.

    The power mean's known, deliberate bias: it *fills* moving nulls (desired here) at the
    cost of a systematic ``+10*log10(1 + r^2)`` energy offset from the echo. The spec is
    evaluated *relative* to a band reference, normalising that offset out; a consumer
    comparing absolute levels must account for it.

    ``echo_band_hz``/``echo_search_us``/``signal_band_hz`` are echoed back on the result,
    since a per-position tau is only interpretable against the window it was searched in.
    ``signal_band_hz`` defaults to ``None`` (screen off) — this module never derives a
    passband; the caller owns the driver contract.

    Raises ``ValueError`` on no captures, a malformed capture, captures sharing no frequency
    support, a non-positive smoothing fraction/threshold, or a malformed band/window
    (**shape**-checked by unpacking, so a wrong-length or non-iterable value raises this
    documented error rather than leaking an ``IndexError``/``TypeError``).

    **Malformed config raises; malformed data refuses.** Every argument above is caller
    configuration, wrong for every position at once. A malformed IR is one position's data
    problem and becomes one refused diagnostic while the others combine — except a well-formed
    band exceeding one PARTICULAR capture's Nyquist, which refuses that position too, since
    it's an interaction with that capture's own sample rate.
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
    # Shape-check by *unpacking* before indexing: turns a 1-/3-tuple or non-iterable into the
    # documented ValueError at one coercion point. ``signal_band_hz`` joins only when supplied.
    checked: dict[str, tuple[float, float]] = {}
    bounds_to_check: list[tuple[str, tuple[float, float]]] = [
        ("echo_band_hz", echo_band_hz),
        ("echo_search_us", echo_search_us),
    ]
    if signal_band_hz is not None:
        bounds_to_check.append(("signal_band_hz", signal_band_hz))
    for name, bounds in bounds_to_check:
        try:
            lo, hi = (float(value) for value in bounds)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} must be a pair of finite numbers with 0 < lo < hi, "
                f"got {bounds!r}"
            ) from exc
        if not (np.isfinite(lo) and np.isfinite(hi)) or not 0.0 < lo < hi:
            raise ValueError(
                f"{name} must be finite and satisfy 0 < lo < hi, got {bounds}"
            )
        checked[name] = (lo, hi)
    echo_band = checked["echo_band_hz"]
    echo_search = checked["echo_search_us"]
    signal_band = checked.get("signal_band_hz")

    validated = [_validate_capture(c) for c in captures]
    grid = _canonical_grid([freqs for freqs, _ in validated])

    # np.interp edge-holds rather than extrapolating, and _canonical_grid already confined the
    # grid to common support, so no capture is asked for a level it never measured.
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

    # One diagnostic-fraction pass per position — the dominant cost in this function.
    per_position_diag_db = np.vstack(
        [
            smooth_fractional_octave(grid, row, fraction=diag_fraction)
            for row in stacked
        ]
    )

    excluded = np.abs(power_mean_diag_db - median_diag_db) > flag_threshold_db
    excluded.flags.writeable = False

    per_position_echo: tuple[EchoDiagnostic | None, ...] = tuple(
        _echo_for(capture, echo_band, echo_search, signal_band)
        for capture in captures
    )
    geometry = assess_geometry(per_position_echo)

    for array in (
        power_mean_db,
        median_db,
        power_mean_diag_db,
        power_mean_spec_db,
        median_diag_db,
        stacked,
        per_position_diag_db,
    ):
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
        echo_band_hz=echo_band,
        echo_search_us=echo_search,
        signal_band_hz=signal_band,
        per_position_db=stacked,
        per_position_diag_db=per_position_diag_db,
        position_roles=tuple(str(c.role or "") for c in captures),
    )
