# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Spatial multi-capture combiner and interference honesty screen.

``combine_positions(captures) -> CombinedResponse`` power-averages N gated
sweeps from a cloud of mic positions onto one shared linear grid, and flags
the bins where that power mean and the dB median disagree by more than
``flag_threshold_db`` as interference-dominated. :func:`detect_echo` stamps a
per-capture tau/strength diagnostic; :func:`assess_geometry` reduces those to
the ``geometry.locked`` verdict.

The screen and ``geometry.locked`` are complementary and neither alone is
sufficient. The screen fires on *partially* aligned interference, where some
positions are nulled at a bin and others are not. It is blind to a
fully-aligned null: every position sees the same null, mean and median agree,
and the null survives the average at full depth — which is the case
``geometry.locked`` reports, and no averaging over *these* positions can fill
it. Silence from the screen is also the healthy outcome, since for uniformly
distributed comb phase the two estimators coincide analytically.

Detection only — nothing here removes an echo. Pure computation (numpy plus
:func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`): no I/O,
no logging, no globals, no randomness, no product policy.

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

# Power-mean vs median disagreement above this many dB flags a bin as
# interference-dominated.
DEFAULT_FLAG_THRESHOLD_DB = 2.0

# 1/6-octave for diagnostics, 1/3-octave for pass/fail. The screen runs at the
# diagnostic fraction because a 1/3-oct window is wide enough to smear a narrow
# interference null into its neighbours before the comparison happens.
DEFAULT_DIAG_FRACTION = 6
DEFAULT_SPEC_FRACTION = 3

# ISO preferred octave-band centres for the cross-position spread diagnostic.
# Octave rather than 1/3-octave: this is a legible ~10-number diagnostic, not a
# curve. A band needs at least MIN_BAND_BINS grid bins inside the shared
# support to be reported.
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

# A capture grid must be uniformly spaced to this relative tolerance:
# smooth_fractional_octave binary-searches linear bins, so a log grid gets a
# silently wrong window width at every frequency.
GRID_UNIFORMITY_RTOL = 1e-3

# Upper bound on the analysis grid; a finer canonical grid is block-averaged in
# linear power onto a coarser linear grid before anything is combined or
# smoothed. smooth_fractional_octave is an O(bins * window) Python loop whose
# window also grows with the grid, so its cost is effectively quadratic in bin
# count — 0.12 s at 16k bins, 0.88 s at 65k, 2.3 s at 131k on a laptop, and
# this must eventually run on a Pi 5. No resolution is lost: 16385 bins over a
# 24 kHz span is ~1.46 Hz spacing against a narrowest smoothing window
# (1/6-octave at the 250 Hz spec edge) of ~29 Hz. Averaging, never subsampling
# — subsampling a combed magnitude curve aliases onto whichever bins land on
# comb peaks or nulls, while a linear-power average is the same estimator this
# module uses everywhere else and composes with the power mean.
MAX_ANALYSIS_BINS = 16385

# --------------------------------------------------------------------------- #
# Echo-detector tuning
#
# Every threshold below is calibrated against three populations: a JTS3 cdhorn
# corpus, synthetic impulse+echo pairs (tau 240-700 us, r 0.15-0.6), and
# negative controls (white noise, impulse+noise with no echo, clean impulse
# with no echo).
# --------------------------------------------------------------------------- #

DEFAULT_ECHO_BAND_HZ = (5000.0, 19000.0)

# The default search window, and the only one whose false-lock behaviour has
# been swept. Prefer it.
#
# A window whose lower edge is 650 us or higher enters the rahmonic regime: a
# window excluding the true delay can still contain a rahmonic of it, and the
# envelope finds a matching peak in the same raised window, so the two
# corroborate a delay roughly 3x too large. Measured on a 10-position cloud of
# true delays 150-400 us: (600, 1000) was clean, while (650, 1000), (700, 1000)
# and (800, 1200) each read ``geometry_locked`` at median tau 814 / 857 /
# 897 us. ``RAHMONIC_MARGIN``'s screen refuses all of those, by 21.5-78.8x.
#
# That makes a raised window SCREENED, not VALIDATED, at a cost only a raised
# window pays: an honest in-window echo under a stronger EARLIER reflection
# presents the same evidence and is refused too — 605 of 720 swept two-echo
# cases, against 0 of 432 for the same geometries at the default window and
# 0 of 370 for single-echo raised-window cases. It always fails as a refusal
# rather than a wrong number.
#
# Every window figure here comes from synthetic one- and two-echo IRs plus a
# three-frame single-position corpus; real multi-bounce behaviour through a
# raised window has never been measured.
DEFAULT_ECHO_SEARCH_US = (120.0, 800.0)

# Order of the polynomial detrend removed from the band's log-magnitude
# before the cepstral transform, so the driver's own broad shape does not
# leak into low quefrencies. Fitted against a [-1, 1] abscissa rather than a
# raw bin index (better conditioned; mathematically equivalent).
DETREND_ORDER = 3

# The direct arrival must stand at least this far above the IR's median
# |sample| level, else the IR has no identifiable direct arrival and "echo
# level re main arrival" is undefined — confidence is 0 and no tau is
# claimed. Measured: real corpus 93-112 dB, synthetic impulse+echo 83 dB,
# impulse buried in noise 37 dB, white noise 16 dB.
ARRIVAL_CREST_FLOOR_DB = 20.0

# Analysis window around the located direct arrival, as a multiple of the
# search window's upper edge (plus a short pre-arrival lead), which keeps room
# decay out of the statistic. At the 800 us default this spans ~3.2 ms,
# comfortably inside the ~7 ms first-reflection gate of the JTS3 room, so the
# window sees the bounce but not the walls.
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
# An echo *below* the window does not vanish from the estimators — it aliases
# upward onto the bottom of the window, where both searches begin, so the two
# aliased estimates agree with each other and the confident number is still
# wrong. Measured (10 positions, true delays 150-400 us): a 300-800 us window
# produced estimates 2-18 us above its lower edge from 150 and 178 us echoes,
# at confidence 0.68-1.00, and the cloud read as geometry_locked.
#
# One quefrency step is exactly the interval over which the cepstral estimator
# cannot distinguish "at the edge" from "below the edge": the smallest margin
# that closes the aliasing path, and the largest that does not manufacture a
# dead zone the instrument could have measured. The upper edge needs no
# equivalent — nothing aliases DOWN onto it, and an above-window echo is
# already rejected outright by the window contract.
#
# This margin does not make a raised window safe: a cepstral rahmonic is a
# different mechanism and can land anywhere inside one, at neither edge. See
# ``RAHMONIC_MARGIN``.
WINDOW_EDGE_MARGIN_STEPS = 1.0

# How close to a whole sample a window edge may sit before ``_ceil_samples`` /
# ``_floor_samples`` treat it as *being* that sample, when turning the caller's
# ``search_us`` into the closed range of sample delays inside it.
#
# The tolerance is not float noise: a caller expressing a sample-aligned edge
# in microseconds has to write a decimal. Eight samples at 48 kHz is
# 166.6666...us, so a bare ``ceil`` on ``166.6667`` (8.0000016 samples)
# excludes sample 8 and costs the caller a whole sample (20.8 us) to honour a
# 0.000033 us overshoot; ``floor`` has the mirror exposure at the upper edge.
#
# 1e-3 of a sample is 20.833 ns at 48 kHz. A D-decimal microsecond value is
# off by at most 0.5e-D us, i.e. 2.4e-(D+2) samples at 48 kHz, so this covers
# a two-decimal edge 4x over and a four-decimal one 400x over — while being
# 1000x finer than the sample period it rounds to and ~3 400x finer than the
# detector's own ~71.43 us quefrency resolution.
WINDOW_EDGE_SNAP_SAMPLES = 1e-3

# Rahmonic screen — the rule that closes the mechanism the edge margin above
# cannot reach. A comb's cepstrum repeats at 2*tau, 3*tau, ..., so a window
# that excludes the true delay can still contain a *rahmonic* of it, at an
# arbitrary place in the window rather than at an edge.
#
# A candidate that has survived the window and edge checks is refused when the
# strongest detrended-cepstrum peak *below* it — over the analyzable region
# running from ``RAHMONIC_FLOOR_STEPS`` quefrency steps up to (but not
# including) the candidate's own bin — exceeds the candidate by more than
# ``RAHMONIC_MARGIN``. A rahmonic is by construction weaker than the
# fundamental that produced it, so a much stronger peak at a lower quefrency
# is *necessary* for the candidate to be one, and testing that condition
# directly needs no assumption that the ratio is an integer — the worst
# measured case is 3.65x, not 3x, so a tau/2, tau/3 submultiple re-test would
# have missed it.
#
# The condition is not *sufficient*: an honest in-window echo sitting under a
# stronger, unrelated *earlier* reflection presents exactly the same picture,
# and measured, the two populations' ratios interleave rather than sitting in
# different bands (see ``DEFAULT_ECHO_SEARCH_US`` for that sweep and the
# remedy). The screen resolves the ambiguity toward refusing, which is the
# fail-safe direction: the caller loses a measurement it could have had rather
# than being handed a delay roughly 3x wrong.
#
# 1.65 sits in a measured gap between two synthetic populations, each
# classified by what the PRE-screen detector did:
#
# * **True positives — 2908 readings** (impulse+echo and shaped-response IRs,
#   tau 200-770 us x r 0.10-0.75, 13 search windows, admitted unrefused and
#   within 15 % of truth). Their lower/candidate ratio peaks at **0.9955** —
#   not low-quefrency leakage but the candidate's own main-lobe shoulder,
#   whose true quefrency sits 0.36 of a step above a cepstral bin.
# * **Wrong readings — 439 readings** (same IR families, tau 100-455 us, 11
#   windows that EXCLUDE the true delay; admitted confident and >15 % off).
#   Their ratio bottoms out at **2.7899**.
#
# So 1.65 is 1.66x above the ceiling and 1.69x below the floor, either side of
# the 1.667 geometric centre, rejecting 439/439 wrong and 0/2908 right
# readings. A margin of exactly 1.0 would separate them too, but with 0.5 % of
# headroom over an entirely ordinary sub-bin geometry.
#
# **RAHMONIC_FLOOR_STEPS = 1** — whole quefrency steps, the cepstrum being
# defined only on them. It excludes exactly one bin, the zero-lag bin, whose
# magnitude is residual DC of the detrended windowed log-magnitude rather than
# a ripple period, so it is not a delay hypothesis a candidate may be judged
# against. Not a rescue from leakage: a floor of 0 gives the same 0.9955
# ceiling.
RAHMONIC_FLOOR_STEPS = 1
RAHMONIC_MARGIN = 1.65

# Signal-presence screen — how far the analysis band may sit below the
# caller's DECLARED passband before the detector refuses to read it.
#
# Nothing else checks that ``band_hz`` contains signal at all: the
# arrival-crest gate passes on a band-limited driver's IR even when the
# analysis band is pure filter stopband, and the two estimators then agree on
# a "ripple" in quantisation noise. Measured on an electrical loopback of the
# live JTS3 CamillaDSP graph, the woofer branch (LR4 lowpass at 2 kHz)
# searched in the 5-19 kHz default band returned tau = 323.3 us, strength =
# -13.13 dB, confidence = 0.275, refusal = "".
#
# The refusal fires when the declared passband's level exceeds the analysis
# band's by more than this margin, both as power-domain band means of the
# early-arrival segment's spectrum (``EchoDiagnostic.band_deficit_db``).
# ``None`` leaves the screen off; this module never guesses a driver's band.
#
# 25.0 dB sits near-centred in a measured 28.36 dB gap at the default band and
# window, over 22 records:
#
# * **Honest acoustic captures — 16 records, ceiling 12.07 dB** (a cdhorn
#   corpus, an S0 main-leg desk cloud, an S0 ground-plane leg whose readings
#   are the ceiling because tipping the cabinet cost top-octave level).
# * **Stopband residue — 3 records, floor 40.43 dB** (the loopback's woofer
#   branch against its 200-2000 Hz passband, on all three stimuli).
# * **In-band control — 3 records, -0.17 to -0.05 dB** (the same loopback's
#   TWEETER branch against its own passband), so the metric does not
#   manufacture a deficit out of an electrical IR as such.
#
# All 16 honest records are the SAME speaker, so that ceiling is the number to
# watch against hardware with a different HF rolloff.
#
# **A caller must keep the analysis band clear of the speaker's crossover.**
# Re-measured across six bands the honest side is comfortable everywhere
# (worst per-band ceiling 17.50 dB), but the residue side fails at
# (2000, 19000), where the deficit collapses to 18.21-18.23 dB and the screen
# silently degrades to pre-screen behaviour. The caller's own floor,
# `crossover_v2.verification.ECHO_BAND_HF_REGIME_FLOOR_HZ` (4000 Hz, anchored
# to this table), is what makes the clearance true by construction.
#
# Not calibrated: a passband narrower than the analysis band, or overlapping
# it. Such a caller gets a well-defined deficit and no measured basis for the
# threshold applied to it.
BAND_BELOW_PASSBAND_MARGIN_DB = 25.0

# Earlier-dominant-arrival dominance floor — how loud a below-window arrival
# must be, relative to the direct arrival, before the detector will call it
# **dominant** and refuse in its name. Without a level test, "is there any
# local maximum below the window" is the whole criterion, and the
# band-limited envelope of an echo-free impulse has plenty: its own ringing.
#
# Measured at the default 5-19 kHz band on ``earlier_arrival_db``, over eleven
# raised windows crossed with the 60-member impulse-with-no-echo family:
#
# * **Must not fire — echo-free ringing**: the envelope finds a below-window
#   local maximum on 658 of 660 readings, spanning -32.1297 to -17.1365 dB.
#   With the floor mutated to -120 dB those produce 6 refusals naming the
#   detector's own skirt as an arrival; with the shipped floor, 0 of 660.
# * **Must fire — the S0 ground plane, n=3: -0.64, -2.01, -2.57 dB** at
#   125-146 us, a mic capsule left centimetres proud of the floor.
#
# -10.0 leaves 7.43 dB below the ground-plane floor and 7.14 dB above the
# ringing ceiling — centred in a 14.58 dB gap. A positive control sits between
# the two, so this is not merely a noise gate: the S0 main-leg desk cloud has a
# real below-window arrival at 145.8 us on 4 of 10 positions, at -14.66 to
# -15.71 dB, and all ten still detected the ~320 us rim wave.
EARLIER_ARRIVAL_DOMINANCE_DB = -10.0

# Refusal vocabulary for EchoDiagnostic.refusal. An empty string means the
# detector ran to completion and is reporting a measurement (which may still
# be a zero-confidence "found nothing credible"); any non-empty value means it
# declined and every estimate on the record is uninformative. Consumers gate
# on `refusal == ""`, never on the specific slug, so this vocabulary can grow
# without breaking them. Listed in the order :func:`detect_echo` can emit
# them, which is also the order its returns appear in the source.
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

# An echo diagnostic counts toward the geometry verdict only at or above
# this confidence. Calibrated to sit in the empty gap between the measured
# populations: true positives (synthetic impulse+echo, tau 240-700 us,
# r 0.15-0.6) scored 0.916-1.000, while 60 impulse-with-no-echo negative
# controls (30 seeds at noise sigma 0.02 and 30 at 0.001 — the two families
# that clear the crest gate and therefore stress the score) spanned
# 0.000-0.091, with 0/60 crossing this floor.
ECHO_CONFIDENCE_FLOOR = 0.5

# An echo diagnostic also counts only when its tau is at least this many
# quefrency steps (EchoDiagnostic.resolution_us) above zero. Below ~3 steps
# both estimators are inside the direct pulse's own skirt, so a "delay" is
# whatever the noise floor put there. Feeding those numbers to the clustering
# test is how a dispersed cloud can read as falsely locked: unresolvable
# estimates pile up near the bottom of the window and look like agreement.
GEOMETRY_MIN_RESOLUTION_STEPS = 3.0

# The speaker's interference pattern is "geometry locked" when at least
# this fraction of confident per-position tau estimates fall within
# +-GEOMETRY_CLUSTER_TOLERANCE of their median. Position-stable tau means
# position-stable nulls, which spatial averaging cannot fill — the cloud is
# not spread enough, or the bounce is speaker-fixed diffraction rather than
# a boundary.
GEOMETRY_CLUSTER_FRACTION = 0.70
GEOMETRY_CLUSTER_TOLERANCE = 0.15

# A "cluster" needs at least two members to mean anything: with a single
# usable estimate, 100% of estimates trivially sit within any tolerance of
# their own median. Below this the verdict is "unknown", reported as not
# locked with an explicit reason — never as a lock, because locking on no
# evidence is the one failure mode that actively misleads a household
# ("spread the mic further" when nothing was actually measured).
GEOMETRY_MIN_CONFIDENT = 2

# Geometry verdict reason vocabulary. Snake_case, self-identifying values.
GEOMETRY_LOCKED = "geometry_locked"
GEOMETRY_DISPERSED = "geometry_dispersed"
# "usable", not "confident": n_confident counts the set that survived all
# three admission rules (measured, confident, *and* resolvable), so a value
# naming only confidence would describe a different set.
GEOMETRY_UNKNOWN = "geometry_insufficient_usable_estimates"


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionCapture:
    """One gated sweep captured at one mic position in the cloud.

    Args:
      position_id: caller's label for the position, carried through to
        :attr:`CombinedResponse.position_ids` so a flagged or outlying
        position can be named back to the user.
      freqs_hz: **linear**-spaced frequency grid, e.g. ``np.fft.rfftfreq``.
        Uniformity is enforced (see ``GRID_UNIFORMITY_RTOL``) because the
        smoothing kernel this module builds on assumes it.
      magnitude_db: matching magnitude in dB — calibrated, reflection-gated,
        and *unsmoothed*. Smoothing is applied once, after combining; a
        per-capture pre-smooth would blur the very interference nulls the
        screen exists to find.
      sample_rate: capture rate in Hz, used only by the echo detector.
      ir: optional time-domain impulse response for :func:`detect_echo`. May
        be the full deconvolved IR; the detector windows it to the
        early-arrival region itself. When ``None`` the position contributes
        no echo diagnostic (reported as ``None``, distinct from "measured
        and found nothing").
      role: what KIND of listening position this is, in the caller's own
        vocabulary (``onax`` / ``offax`` / ``xovr``), or ``""``. Carried,
        never read by the combination: the reduction stays an unweighted
        power mean and an unweighted dB median, and there is no ``weights=``
        argument anywhere in this module. It exists so a per-position number
        can be labelled when :func:`position_residuals` reports it.
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

    Read ``refusal`` first, then ``confidence``. A non-empty ``refusal``
    means the detector declined to measure and *every* estimate below is
    uninformative; an empty ``refusal`` with ``confidence == 0.0`` means the
    detector ran and found nothing credible. ``tau_us`` and ``strength_db``
    carry information only when ``refusal == ""`` and ``confidence > 0``.

    Every level is dB relative to the direct arrival and every delay is
    microseconds. ``STRENGTH_FLOOR_DB`` in a level field and 0.0 in a delay
    field both mean **not measured** — finite sentinels, so the record stays
    arithmetic-safe, and no real reading can collide with either.

    Args:
      tau_us: delay of the secondary arrival — the reported answer, and the
        only tau field with a window guarantee: either 0.0 or inside the
        caller's ``search_us`` window, clear of its lower edge by
        ``WINDOW_EDGE_MARGIN_STEPS``. **Always** the analytic-envelope
        estimate (sample resolution plus parabolic refinement, ~1-4% accurate
        on the calibration set), so it is not quantised to ``resolution_us``.
      strength_db: secondary-arrival level, from the band-limited analytic
        envelope.
      confidence: 0.0-1.0. Cepstral concentration times agreement between
        the two tau estimators, behind an arrival-crest gate that refuses
        outright rather than scoring down.
      refusal: ``""`` when the detector produced a measurement; otherwise
        one of the ``REFUSAL_*`` slugs saying why it declined.
      resolution_us: the **cepstral corroborator's** quefrency step,
        ``1 / band_width`` — ~71 us for the 5-19 kHz default band. Not the
        granularity of ``tau_us``, which is far finer; it is this module's
        trust floor, and three rules are written in these units
        (``WINDOW_EDGE_MARGIN_STEPS``, ``RAHMONIC_FLOOR_STEPS``, and
        :func:`assess_geometry`'s clustering floor).
      tau_cepstral_us: the cepstral estimator's answer in isolation, and
        **raw**: it may fall outside ``search_us``, which is precisely what
        rejects the candidate. Kept unclamped on purpose — a railed value
        hidden by a clamp is what made a dispersed cloud read as
        geometry-locked.
      tau_envelope_us: the envelope estimator's answer in isolation, also
        raw and not window-guaranteed. The envelope always returns the
        largest value in its window; it does not decide whether that is a
        real arrival.
      concentration: bounded [0, 1] cepstral peak concentration.
      corroboration: relative disagreement between the two tau estimators
        (0.0 = identical). **1.0 means exactly one thing: the two could not be
        compared** — a marker, never "measured, and they disagreed
        completely". The two late refusals (``tau_at_window_lower_edge``,
        ``rahmonic_of_lower_delay``) fire after the comparison and pass through
        whatever it produced, and neither rule reads this field.
      arrival_crest_db: direct arrival level above the IR's median
        ``|sample|`` level — the "is there an arrival at all" gate.
      lower_peak_us: quefrency of the strongest detrended-cepstrum peak
        *below* the cepstral candidate, over the rahmonic screen's analyzable
        region (see ``RAHMONIC_MARGIN``).
      lower_peak_ratio: that peak's magnitude divided by the candidate's, so a
        ``rahmonic_of_lower_delay`` refusal is exactly ``lower_peak_ratio >
        RAHMONIC_MARGIN``, recomputable from the record. Reported on EVERY
        record that reached the scan, so a consumer can watch the screen's
        headroom erode; corpus detections sit at 0.329-0.387. 0.0 in both
        fields is unambiguously "not measured" — a measured region starts at a
        strictly positive quefrency.
      effective_floor_us: the delay below which THIS window cannot report an
        arrival at all: ``search_us[0] + WINDOW_EDGE_MARGIN_STEPS *
        resolution_us``, ~191.4 us for the defaults. Populated on every record,
        refusals included. 0.0 only alongside ``resolution_us == 0.0``, on the
        records :func:`combine_positions` builds when :func:`detect_echo`
        raised before the band was known.
      earlier_arrival_us: delay of the strongest **genuine local maximum of the
        envelope below ``search_us[0]``**. A reading, never a candidate. The
        scan stops at the same index the envelope's candidate range starts at,
        so "below the window" and "in the window" partition the samples with no
        overlap and no gap.
      earlier_arrival_db: that arrival's level, from the same envelope. The
        field the dominance test reads, so an ``earlier_dominant_arrival``
        refusal is recomputable: it needs ``earlier_arrival_db >
        EARLIER_ARRIVAL_DOMINANCE_DB``. Reported whether or not it passes.
      band_deficit_db: how far the analysis band's level sits BELOW the
        caller's declared passband, both as power-domain means of the
        early-arrival segment's spectrum. Positive means the analysis band is
        the quieter one, so a ``band_below_passband`` refusal is exactly
        ``band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB``. Honest acoustic
        records measure 1.04 to 12.07 dB. Not measured when no
        ``signal_band_hz`` was declared, when the detector returned before the
        screen ran, or when the passband covered no bin of the segment's
        spectrum — the fail-open for a passband narrower than one FFT bin.
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
      thin_evidence: the verdict is real but rests on the bare minimum —
        exactly ``n_confident == GEOMETRY_MIN_CONFIDENT and n_positions >=
        2 * GEOMETRY_MIN_CONFIDENT``. **A cliff, not a gradient**: with
        ``GEOMETRY_MIN_CONFIDENT == 2``, two usable estimates out of ten is
        thin and **three out of ten is not**. A consumer wanting a gradient
        should read ``n_confident`` and ``n_positions`` directly.

        It is disclosure, not rejection: nothing here scales a threshold or
        withholds a verdict. It is structurally unreachable on
        ``GEOMETRY_UNKNOWN``, which fires exactly when ``n_confident <
        GEOMETRY_MIN_CONFIDENT``, so it always qualifies a
        ``GEOMETRY_LOCKED`` or ``GEOMETRY_DISPERSED`` verdict.
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

    A residual that is large at EVERY position is broadband and ours — a
    role-level trim or model error, which is fixable. One that is large at a
    single position is the room or the placement. Labelling it with the role
    is what makes it readable: "on-axis 0.4 dB, off-axis 2.9 dB" is an
    instruction; a bare spread number is a mood.

    Args:
      position_id: the position this describes.
      role: its :attr:`PositionCapture.role`, or ``""`` when it declared none.
      rms_db: root-mean-square of ``per_position_diag_db -
        power_mean_diag_db`` over the graded band, in dB. ``None`` when the
        band selected no usable bin — an absence, never a fabricated zero,
        which would read as "this position agreed perfectly".

        **The DIAGNOSTIC-smoothed pair, not the raw one.** On raw curves the
        dominant term is each position's own interference comb: a healthy
        dispersed four-position cloud reports ~2.5 dB everywhere, and a
        broadband +4 dB offset on ONE position moved its number by less than
        the spread between the untouched three — the metric could not see the
        defect class it exists for. Both operands come from the same fraction
        and construction, so this adds no smoothing pass.
      n_bins: how many bins the RMS was taken over. ``0`` with a ``None``
        ``rms_db`` says the band was empty rather than that the arithmetic
        failed.
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
    """Cross-position magnitude spread in one octave band — two numbers
    answering two different questions.

    ``sigma_db`` is the *level* spread: each position is first collapsed to
    one band level by averaging its bins in **linear power**, then the sample
    standard deviation (``ddof=1``) is taken across those N levels. It is
    insensitive to comb structure by construction, since a band holds many
    comb periods. A healthy dispersed cloud therefore has a *small*
    ``sigma_db``; a large one means the positions genuinely disagree about how
    loud this part of the spectrum is (mic distance, gain, or directivity),
    which averaging will not fix.

    ``max_sigma_db`` is the *structure* spread: the worst single bin's
    cross-position sigma, computed per-bin without any smoothing, so it rides
    comb nulls on purpose. A band whose ``max_sigma_db`` dwarfs its
    ``sigma_db`` is null-dominated at a few frequencies (positions disagree
    bin-by-bin but agree on the band's energy: decorrelation working), whereas
    a band where the two are comparable is broadly noisy.

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
        the pass/fail tolerance table is evaluated against.
      median_diag_db: ``median_db`` at the diagnostic fraction.
      per_position_db: ``(n_positions, len(freqs_hz))``, row *i* being
        ``position_ids[i]``'s magnitude resampled onto the shared grid —
        **unsmoothed**, and after any ``MAX_ANALYSIS_BINS`` decimation, so it
        is exactly the array ``power_mean_db`` and ``median_db`` are reduced
        from.
      per_position_diag_db: ``per_position_db`` at the diagnostic fraction,
        row-for-row, so a per-position feature uses the SAME smoothing
        construction as the combined curves. It costs one
        ``smooth_fractional_octave`` pass per position — the combiner's
        dominant term, 40 % of a 3.45 s call on a ten-position 16384-bin cloud
        — paid unconditionally because every shipped consumer needs it. Not
        re-measured on a Pi 5.
      excluded: per-bin mask, True where the two diagnostic curves disagree
        by more than ``flag_threshold_db``. Downstream: excluded from
        correction *and* from pass/fail, and reported to the user.
      excluded_bands_hz: ``excluded`` as merged ``(f_lo, f_hi)`` intervals,
        one per contiguous run. No gap-bridging is applied — two runs
        separated by a single unflagged bin stay two intervals.
      n_positions: number of captures combined.
      position_ids: their ids, in input order.
      position_roles: their :attr:`PositionCapture.role` values, index-aligned
        with ``position_ids``. ``""`` for a capture that declared none. Copied
        through untouched — nothing in the combination reads it.
      per_position_echo: index-aligned with ``position_ids``. ``None``
        means *strictly* "no IR was supplied for this position". A capture
        that supplied an IR always gets an :class:`EchoDiagnostic`, including
        when the detector rejected the input outright (a non-empty
        ``refusal``) or ran and found nothing credible (empty ``refusal``,
        zero confidence). Those three states are deliberately
        distinguishable.
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
      signal_band_hz: the declared passband actually handed to
        :func:`detect_echo`'s signal-presence screen, or ``None`` when the
        screen did not run. A ``band_below_passband`` refusal, or its
        absence, is only interpretable against the passband it was judged
        against.
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
    # An empty array is not a legal value from :func:`combine_positions`,
    # which always populates both — it is what a hand-built or deserialised
    # record carries when the per-position curves were never retained.
    per_position_db: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    per_position_diag_db: np.ndarray = field(
        default_factory=lambda: np.empty((0, 0))
    )
    #: ``()`` on the same terms as the two arrays above.
    #: :func:`combine_positions` always populates it, with ``""`` for a
    #: capture that declared no role.
    position_roles: tuple[str, ...] = ()


def position_residuals(
    combined: CombinedResponse, *, band_hz: tuple[float, float] | None = None,
) -> tuple[PositionResidual, ...]:
    """One :class:`PositionResidual` per position, in input order.

    ``band_hz`` is the trusted band the round graded in — the caller's, because
    the trusted floor and the mic-tier ceiling are session facts this module
    has no way to know. ``None`` uses the whole shared grid, which is the
    honest default for a caller that has not decided on one rather than a claim
    that the whole grid is trustworthy.

    Bins the combination EXCLUDED are dropped too. A bin the two diagnostic
    estimators disagree on is one no verdict is graded on, and letting it into
    a per-position number would put interference structure into a figure a
    reader will take for a placement error.

    ``()`` when the record retained no per-position curves (a hand-built or
    deserialised :class:`CombinedResponse`) — the same absent-vs-zero rule the
    dataclass follows.
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
    grid: np.ndarray, stacked: np.ndarray, *, max_bins: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """Block-average a too-fine analysis grid down to ``max_bins``.

    Averaging is done in **linear power**, the same estimator the rest of
    this module uses, so decimation composes with the power mean instead of
    biasing it; subsampling would alias a combed curve onto whichever bins
    happened to land on peaks or nulls. See ``MAX_ANALYSIS_BINS`` for the
    cost/resolution argument. ``max_bins=None`` reads that constant fresh on
    every call rather than baking it into a default value, so it stays
    monkeypatchable and a caller wanting a different ceiling passes one.

    Blocks are a fixed width, so the decimated grid stays exactly linear
    (block centres are spaced by ``block * step``) and remains a legal input
    to :func:`~jasper.audio_measurement.analysis.smooth_fractional_octave`.
    A trailing partial block is dropped rather than averaged, because a
    short final block would sit at a different centre and break that
    uniformity; at most ``block - 1`` bins — under one part in ten thousand
    of the span — are lost off the top.

    Identity (same objects) when the grid is already within the bound.
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
    """The 1-D public face of :func:`_decimate_to_analysis_grid`.

    Same rule, same owner, one caller shape apart: the combiner decimates a
    STACK of positions, while a caller holding a single curve needs the
    identical block-average so its curve is smoothed and evaluated at the same
    grid density as the measured one it will be compared against.

    ``max_bins`` of ``None`` reads :data:`MAX_ANALYSIS_BINS` fresh on every
    call; a caller persisting a curve passes its own smaller ceiling instead.

    Identity (same objects) when the grid is already within ``max_bins``.
    """
    coarse_grid, coarse_stacked = _decimate_to_analysis_grid(
        grid, np.asarray(magnitude_db, dtype=float).reshape(1, -1), max_bins=max_bins,
    )
    return coarse_grid, coarse_stacked[0]


def merged_true_intervals(
    freqs_hz: np.ndarray, mask: np.ndarray
) -> tuple[tuple[float, float], ...]:
    """Contiguous ``True`` runs of ``mask`` as merged ``(f_lo, f_hi)``
    intervals spanning each run's first and last flagged bin.

    The single owner of this rule; :mod:`jasper.active_speaker.flat_spec`
    imports it rather than keeping its own copy.

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


def _ceil_samples(delay_s: float, sample_rate: int) -> int:
    """First whole sample whose delay is at or above ``delay_s``."""
    return int(math.ceil(delay_s * sample_rate - WINDOW_EDGE_SNAP_SAMPLES))


def _floor_samples(delay_s: float, sample_rate: int) -> int:
    """Last whole sample whose delay is at or below ``delay_s``."""
    return int(math.floor(delay_s * sample_rate + WINDOW_EDGE_SNAP_SAMPLES))


class EchoInputError(ValueError):
    """A malformed :func:`detect_echo` input, carrying a stable slug.

    A ``ValueError`` subclass, with a machine-readable ``slug`` attached at
    the raise site so :func:`combine_positions` can turn the failure into a
    self-describing refused diagnostic without matching on message text.
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
    corroboration: float = 1.0,
    lower_peak_us: float = 0.0,
    lower_peak_ratio: float = 0.0,
    effective_floor_us: float = 0.0,
    earlier_arrival_us: float = 0.0,
    earlier_arrival_db: float = STRENGTH_FLOOR_DB,
    band_deficit_db: float = STRENGTH_FLOOR_DB,
) -> EchoDiagnostic:
    """A refused diagnostic: no delay reported, and a slug saying why.

    The "detector declined" constructor, and only that: the other
    zero-confidence outcome, "ran and found nothing credible", carries
    ``refusal == ""`` and is built inline in :func:`detect_echo`.

    ``corroboration`` defaults to 1.0 because on most refusal paths the two
    estimators genuinely COULD NOT BE COMPARED. The two late refusals —
    edge-proximity and rahmonic — fire AFTER the comparison, so both pass the
    measured value through. The raw estimator fields carry through when they
    exist: ``lower_peak_*`` is the only evidence ``rahmonic_of_lower_delay``
    has, ``earlier_arrival_*`` plays that role for
    ``earlier_dominant_arrival``.
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
    """Detect a discrete early echo in one impulse response.

    Detection only, never removal. Two independent estimators divide the
    labour:

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
    stands in for it — and ``confidence`` is cepstral concentration times
    corroboration, behind an arrival-crest gate that refuses outright rather
    than scoring down.

    **``search_us`` is a rejection contract, not a clamp.** A candidate whose
    *refined* delay lands outside the window is rejected, never pulled back
    to the edge: every position's railed estimate would rail to the *same*
    edge, so a genuinely dispersed cloud would cluster tightly enough to read
    as falsely ``geometry_locked``. A rejected candidate corroborates nothing
    (``corroboration`` is forced to 1.0), so a non-zero ``confidence``
    requires **both** estimators in-window; if neither survives the result is
    a ``no_in_window_echo`` refusal. Widen the window rather than trusting an
    edge value.

    Three further rules refuse candidates the window alone would admit, each
    calibrated on the constant it names:

    * **The lower edge** (``WINDOW_EDGE_MARGIN_STEPS``) — an echo BELOW the
      window aliases upward onto the bottom of it, where both searches begin,
      so the two aliased estimates agree on a confident, in-window, wrong
      number. A candidate within that margin of ``search_us[0]`` is refused
      with ``tau_at_window_lower_edge``. No equivalent margin at the upper
      edge: nothing aliases DOWN onto it.
    * **The rahmonic screen** (``RAHMONIC_MARGIN``) — a window excluding the
      true delay can still contain a RAHMONIC of it, anywhere inside rather
      than at an edge, so a candidate beaten by more than that margin by the
      strongest detrended-cepstrum peak below it is refused with
      ``rahmonic_of_lower_delay``, evidence in ``lower_peak_us`` /
      ``lower_peak_ratio``. It also refuses an honest in-window echo under a
      stronger unrelated EARLIER reflection, which presents the same evidence
      — see ``DEFAULT_ECHO_SEARCH_US``.
    * **The signal-presence screen** (``BAND_BELOW_PASSBAND_MARGIN_DB``) — the
      one gate about the caller's BAND. With no signal in ``band_hz`` the crest
      gate still passes and both estimators work on filter stopband residue, so
      a caller declaring ``signal_band_hz`` gets a ``band_below_passband``
      refusal before either runs. ``None`` leaves the screen off; this module
      never guesses a passband.

    **The earlier-dominant-arrival disclosure** replaces an uninformative zero
    with a named refusal, on three conditions: the envelope's own answer lands
    below ``search_us``; a genuine local maximum is measured down there to
    name; and it is louder than ``EARLIER_ARRIVAL_DOMINANCE_DB`` re the direct
    arrival. Strictly a fallback, returning after every other refusal. Because
    the third condition is a threshold there is a band — about 0.7 dB wide on
    the one geometry measured — where an interloper takes the envelope's answer
    unnamed and the record falls back to the empty-refusal outcome;
    ``earlier_arrival_us`` / ``earlier_arrival_db`` disclose it either way.

    The IR is windowed internally to the early-arrival region (see
    ``ECHO_WINDOW_SPAN_FACTOR``), so a full deconvolved IR may be passed.

    Args:
      ir: time-domain impulse response. May be the full deconvolved IR.
      sample_rate: in Hz.
      band_hz: analysis band. The default targets the HF region where a
        directivity-weighted bounce combs most visibly; the upper edge is
        clipped to Nyquist.
      signal_band_hz: the driver's **declared** passband, or ``None`` to
        skip the signal-presence screen entirely. Supplying it enables the
        ``band_below_passband`` refusal described above. The upper edge is
        clipped to Nyquist like ``band_hz``; a degenerate pair raises
        ``EchoInputError`` for the same reason ``band_hz`` does — it is
        caller configuration, wrong for every capture at once.
      search_us: bounds on the echo delay searched, microseconds. The
        default spans an early boundary bounce (~120 us ≈ 4 cm path delta)
        up to ~800 us (≈ 27 cm), below the room's first wall reflection, and
        is the only window with a swept false-lock record — prefer it.

        **Resolution floor — the bottom of the window does not measure.**
        Both estimators degrade as tau approaches ``1 / bandwidth``
        (:attr:`EchoDiagnostic.resolution_us`, ~71 us for the 5-19 kHz
        default). Two consequences to plan the window around:

        * The bottom ``WINDOW_EDGE_MARGIN_STEPS`` of WHATEVER window is asked
          for is refused outright, so the default window's effective floor is
          ~191 us, not its stated 120 us (disclosed as
          :attr:`EchoDiagnostic.effective_floor_us`). Near-floor bias stretches
          that a little for weak reflections: at r=0.15 the refusal reaches
          ~210 us before clearing at ~220 us.
        * Independently of the window, :func:`assess_geometry` will not cluster
          a ``tau_us`` below ``GEOMETRY_MIN_RESOLUTION_STEPS * resolution_us``
          (~214 us for the defaults) — measured from zero delay, not from
          ``search_us[0]``, so it binds on a low window only.

        The target bounce (~300 us) and the corpus's measured 313-323 us sit
        above both floors. Widening ``band_hz`` shrinks ``resolution_us``,
        lowering the edge margin and the clustering floor together.

    Returns:
      An :class:`EchoDiagnostic`. A non-empty ``refusal`` means the detector
      declined; ``confidence == 0.0`` with an empty ``refusal`` means it ran
      and found nothing credible. Either way the estimates carry no
      information, and ``tau_us`` is 0.0.

    Raises:
      EchoInputError: (a ``ValueError``) on an empty / non-finite IR, a
        non-positive sample rate, or a degenerate band / signal band /
        search window.
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

    # The quefrency step of the band actually used (after Nyquist clipping)
    # — reported on every diagnostic, including refusals, so a consumer can
    # judge whether a delay is resolvable without re-deriving the band.
    resolution_us = 1e6 / (hi_hz - lo_hz)
    # The edge-margin dead zone — one derivation, two consumers. It sets the
    # delay this window cannot report below (disclosed on every record as
    # ``effective_floor_us``) *and* it is the boundary the
    # ``tau_at_window_lower_edge`` check applies further down, which reads
    # ``edge_margin_s`` off this same value.
    edge_margin_us = WINDOW_EDGE_MARGIN_STEPS * resolution_us
    effective_floor_us = search_lo_s * 1e6 + edge_margin_us

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
    # Both levels come off ``spectrum``, the early-arrival segment's own
    # transform already computed above, so the screen reads exactly the data
    # the estimators are about to read and costs no extra FFT. Means are taken
    # in **linear power**, this module's estimator everywhere else. Placed
    # before the cepstrum so a stopband-residue signal never reaches an
    # estimator that could dress it up as a delay. See
    # ``BAND_BELOW_PASSBAND_MARGIN_DB``.
    band_deficit_db = STRENGTH_FLOOR_DB
    if signal_band is not None:
        signal_mask = (freqs >= signal_band[0]) & (freqs <= signal_band[1])
        # A declared passband narrower than one FFT bin measures nothing;
        # with n_fft >= 4096 that is a sub-12 Hz passband at 48 kHz. Leaving
        # the deficit unmeasured is the fail-open choice on purpose: refusing
        # on a band the detector could not evaluate would be a verdict about
        # the caller's arithmetic dressed up as one about the capture.
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
            effective_floor_us=effective_floor_us,
            band_deficit_db=band_deficit_db,
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

    # The rahmonic screen's evidence, measured here rather than at the screen
    # itself so that *every* record downstream of this point carries it,
    # refusals taken before the screen included. The region deliberately
    # extends below ``search_lo_s``: the point is to see the echo the
    # caller's window excluded.
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
    # **The window's edges in samples, defined once.** ``first`` is the first
    # sample whose delay is at or above ``search_lo_s`` and ``last`` the last
    # at or below ``search_hi_s``, so the candidate range is exactly the
    # sample delays inside the caller's closed window — ceil at the bottom,
    # floor at the top, the search_us rejection contract read at sample
    # resolution. ``first`` is also the below-window scan's stop below, which
    # is the point: one writer, so no sample can be simultaneously the
    # envelope's first in-window candidate and a "below-window arrival".
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

    # The strongest genuine local maximum whose delay is **below** the
    # caller's window — a distinct arrival the search excludes. Purely a
    # reading: it is never a candidate and never changes which sample the
    # envelope answers with. Requiring a genuine local maximum is what makes
    # it mean "a separate arrival" rather than "some point on the direct
    # pulse's own decay". Measured at the default band and window, 0 of the
    # 60 impulse-with-no-echo negative controls behind
    # ``ECHO_CONFIDENCE_FLOOR`` has one while all three S0 ground-plane
    # positions do, which keeps the refusal below off the found-nothing
    # population.
    #
    # The scan stops at ``first`` — the same value the envelope's candidate
    # range starts at, not a second derivation of the boundary — so "below
    # the window" and "in the window" partition the samples with no overlap
    # and no gap.
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
        # "Ran, found nothing credible" (empty refusal, zero confidence) is
        # the wrong answer in one nameable state: the envelope's own answer
        # landed BELOW the caller's window, so the window contract rejected it,
        # corroboration was forced to the incomparable marker, and the score is
        # zero by construction — a comparison simply did not happen.
        #
        # **Three conditions, all properties of this record.** The envelope's
        # answer below ``search_lo_s``; a genuine arrival measured down there
        # for the refusal to name; and that arrival loud enough to deserve the
        # word DOMINANT (``EARLIER_ARRIVAL_DOMINANCE_DB``). The third is not
        # decoration: without it the band-limited envelope's own ringing
        # qualifies, and at raised windows it flips echo-free readings into a
        # refusal naming the detector's own skirt as an arrival. Failing any
        # condition falls through to the honest nothing-found below, and this
        # branch can pre-empt no existing refusal — every one returns earlier.
        #
        # Rejected alternative: letting the envelope skip the interloper and
        # answer with the best IN-WINDOW arrival. Measured, and it re-opens the
        # false-lock hazard — hunting past an excluded arrival manufactures
        # agreement with the cepstral RAHMONIC of that same arrival, driving
        # the calibration sweep's ``lower_peak_ratio`` floor under the wall
        # ``RAHMONIC_MARGIN`` needs.
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

    # The envelope estimate is the answer, unconditionally. Getting here
    # needs confidence > 0, which needs corroboration < CORROBORATION_LOOSE,
    # which only the both-in-window branch above can produce — so the
    # envelope is in-window by construction and the cepstrum never has to
    # stand in for it.
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
    """The per-position diagnostics that count as evidence, in input order.

    The three admission rules — measured, confident, resolvable — are
    implemented here and explained once, in :func:`assess_geometry`'s
    docstring. Two consumers share this one implementation:
    :func:`assess_geometry` for the set it clusters, and
    :mod:`jasper.audio_measurement.interference_nulls` for the arrival
    candidates it corroborates a null ladder against.

    Returns the diagnostics themselves rather than their taus, because the
    null gate needs each estimate's ``strength_db`` (its time-domain
    reflection ratio) as well as its delay.
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

    ``locked`` when at least ``min_fraction`` of the *usable* per-position
    tau estimates (defined below; ``n_confident`` counts them) fall within
    ``tolerance`` (relative) of their median. A stable tau means a stable
    null ladder at ``(n+0.5)/tau``, which spatial averaging cannot fill
    however many positions are added — the honest consumer response is to ask
    the user to spread the mic further, or to conclude the bounce is
    speaker-fixed diffraction rather than a boundary, in which case the
    exclusion screen carries the weight instead.

    **What counts as usable evidence** — a diagnostic is admitted only when
    all three hold, because each excluded class produces a *false* lock in a
    different way. The rules are implemented once, in
    :func:`usable_echo_estimates`, and explained once, here:

    1. ``refusal == ""`` — the detector actually measured. A refusal's
       ``tau_us`` is 0.0, and a pile of zeros clusters perfectly.
    2. ``confidence >= confidence_floor`` — the credibility gate.
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

    **This function is only as honest as the diagnostics it is handed.** The
    three rules screen UNUSABLE evidence; they cannot screen evidence that is
    usable-looking and wrong. The worked example is the rahmonic class, which
    passes all three and clusters with itself, so a genuinely dispersed cloud
    returns ``GEOMETRY_LOCKED``; that fix lives in the detector
    (``RAHMONIC_MARGIN``).

    Args:
      echoes: per-position diagnostics, ``None`` where no IR was supplied.
      confidence_floor: minimum :attr:`EchoDiagnostic.confidence` to count.
      tolerance: relative clustering tolerance about the median tau.
      min_fraction: fraction of the usable set that must cluster.

    Returns:
      A :class:`GeometryLock` carrying the verdict and its supporting
      numbers. ``n_confident`` is the size of the usable set defined above,
      and ``thin_evidence`` qualifies — never withholds — a verdict resting
      on the bare minimum of it.
    """
    n_positions = len(echoes)
    taus = np.array(
        [e.tau_us for e in usable_echo_estimates(echoes, confidence_floor=confidence_floor)],
        dtype=float,
    )
    n_confident = int(taus.size)
    # Evidence quality, computed from the counts alone and independent of
    # the verdict below — see :attr:`GeometryLock.thin_evidence`. It is
    # passed on the ``GEOMETRY_UNKNOWN`` return too, where it cannot be True,
    # so the flag has one derivation rather than two.
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
    """Octave-band cross-position spread from raw per-position curves.

    Deliberately **unsmoothed**: a band-power average gives the same
    statistic directly and exactly, without one ``smooth_fractional_octave``
    pass per position. See :class:`BandSpread` for what each number means.
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
    signal_band_hz: tuple[float, float] | None,
) -> EchoDiagnostic | None:
    """One position's echo diagnostic, or ``None`` when it supplied no IR.

    A malformed IR is one position's problem, never the whole cloud's: the
    detector's ``ValueError`` becomes a refused diagnostic carrying the
    reason, so ten good captures plus one all-zero IR still combine. The
    slug comes from :class:`EchoInputError` when the detector raised one,
    so this never parses a message string.

    What can still arrive here is per-*capture* trouble only —
    ``malformed_ir``, ``all_zero_ir``, and ``bad_band_hz`` /
    ``bad_signal_band_hz`` when a band exceeds this capture's Nyquist.
    Config-shaped failures cannot: a malformed band or search window is
    rejected up front by :func:`combine_positions`, and a non-positive sample
    rate by ``_validate_capture``, both before any detection runs.

    A record built on this path reports ``resolution_us`` and
    ``effective_floor_us`` as 0.0 — the detector raised before either was
    known, and 0.0 is this module's "not measured" for both.
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

    See the module docstring for the two complementary blind spots of the
    honesty screen and the geometry flag.

    The **power mean** is the primary estimator; the **dB median** exists to
    disagree with it. Where they disagree by more than ``flag_threshold_db``
    at the diagnostic smoothing fraction, the bin is interference-dominated
    and is reported for exclusion from both correction and pass/fail.

    Note the power mean's known, deliberate bias: it *fills* moving nulls,
    which is the desired behaviour here, at the cost of a systematic
    ``+10·log10(1 + r²)`` energy offset from the echo itself. The spec is
    evaluated *relative* to a band reference, which normalises that offset
    out; a consumer comparing absolute levels must account for it.

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
      signal_band_hz: declared passband handed to :func:`detect_echo`'s
        signal-presence screen, or ``None`` (default) to leave the screen
        off. Echoed back for the same reason as the two above. This module
        never derives it — the caller owns the driver contract, and a pure
        combiner that guessed a passband would be product policy in a
        pure-DSP module.

    Returns:
      A :class:`CombinedResponse`.

    Raises:
      ValueError: on no captures, a malformed capture (see
        :class:`PositionCapture`), captures sharing no frequency support, a
        non-positive smoothing fraction / threshold, or a malformed
        ``echo_band_hz`` / ``echo_search_us`` / ``signal_band_hz``
        (the last only when it is not ``None``) — malformed in **shape**
        (anything that is not a pair of numbers: ``None``, a 1- or 3-tuple,
        a scalar, a non-numeric entry) as well as in value. Shape is checked
        by unpacking before any element is read, so a wrong-length or
        non-iterable window raises this documented ``ValueError`` rather
        than leaking an ``IndexError`` / ``TypeError``.

        **Malformed config raises; malformed data refuses.** Every argument
        above is caller configuration, wrong for every position at once, so it
        fails loudly. A malformed IR is one position's data problem and becomes
        one refused diagnostic while the others combine. One case sits
        deliberately on the data side: a well-formed band exceeding a
        PARTICULAR capture's Nyquist is an interaction with that capture's
        sample rate, so it refuses that position rather than failing the
        combine.
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
    # Shape-check by *unpacking*, before any indexing: unpacking a two-item
    # generator turns a 1-tuple, a 3-tuple and a non-iterable alike into the
    # documented ValueError at one coercion point, and the coerced pair is
    # what the rest of the function uses, so a list or a numpy pair reaches
    # :func:`detect_echo` and the result record as the same plain tuple.
    # ``signal_band_hz`` joins the loop only when supplied — ``None`` is not
    # a malformed pair but the documented "leave the screen off" value.
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

    # One diagnostic-fraction pass per position. The dominant cost in this
    # function — see CombinedResponse.per_position_diag_db for the measured
    # figure and why it is paid unconditionally.
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
