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
   for diagnostics), plus 1/6-octave of the median for the screen, plus the
   same 1/6-octave construction applied *per position*. This module is the
   single owner of that construction: a consumer needing one position's
   smoothed curve reads
   :attr:`CombinedResponse.per_position_diag_db` rather than re-smoothing
   :attr:`CombinedResponse.per_position_db`, which would be a second
   construction to drift. See that attribute for the runtime cost it adds.
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
   failing the combine, all of it so that a number this module reports is
   one it actually measured. A third rule, the **rahmonic screen**, closes
   the one path the first two could not reach: a comb's cepstrum repeats at
   2*tau, 3*tau, ..., so a below-window echo can put a peak *anywhere* in a
   raised window rather than at an edge, and until this screen landed such a
   peak was reported confidently at roughly 3x the true delay — enough to
   lock a genuinely dispersed cloud whenever ``search_us[0] >= 650``. The
   screen refuses a candidate that a stronger cepstral peak at lower
   quefrency dominates; on the three windows that used to false-lock it
   rejects every admitted estimate by 21.5-78.8x, with the stronger peak
   landing within half a quefrency step of the true delay. See
   ``RAHMONIC_MARGIN`` for the calibration and ``DEFAULT_ECHO_SEARCH_US``
   for the swept grid. Two further rules came out of the plan's S0 session
   (2026-07-25), both about signals the first three rules had no opinion
   on: the **signal-presence screen** refuses an analysis band sitting far
   below the caller's *declared* passband, where an electrical loopback of
   the live JTS3 graph had produced a confident-looking tau out of filter
   stopband residue (``BAND_BELOW_PASSBAND_MARGIN_DB``); and the
   **earlier-dominant-arrival** rule replaces an uninformative zero with a
   named refusal when the envelope's own answer landed below the window, a
   genuine arrival is measured down there, and that arrival is loud enough
   to have plausibly taken the answer (``EARLIER_ARRIVAL_DOMINANCE_DB`` —
   all three conditions, not the first alone). The same session's
   ground-plane leg is why every record now carries
   ``EchoDiagnostic.effective_floor_us``: its 125-146 us arrivals sit under
   the default window's ~191 us reporting floor, and the plan's response is
   to surface that floor rather than raise the window.
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
:mod:`jasper.active_speaker.linearization_envelope` (#1668 PR-B), **no
production callers when it shipped**. Both wirings have since landed: the v2
session's position-group choreography calls :func:`combine_positions`
(:mod:`jasper.active_speaker.crossover_v2_flow`), and the spec bands and gauges
consume the exclusion mask through
:mod:`jasper.active_speaker.flat_spec` / :mod:`~jasper.active_speaker.linearization_envelope`.
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

# The default search window — and the only window whose false-lock
# behaviour has actually been swept.
#
# **The default has no known false-lock regime.** No synthetic cloud in
# this module's test suite produces a geometry verdict through it that the
# captures did not earn, and no estimate it admits is a rahmonic. The
# evidence for that is deliberately *executable* rather than a number
# quoted here: the pinned per-window sweep rows in
# test_below_window_cloud_verdict_by_raised_search_window and the corpus
# tests in tests/test_spatial_combine.py. An earlier revision of this
# comment cited an aggregate ("63 usable estimates, all within 0.996-1.008
# of truth") from an ad-hoc sweep that was not reproducible from its own
# description and whose stated floor was rounded the wrong way — the true
# minimum was below 0.996, so the bound claimed was tighter than the one
# measured. A figure nobody can re-derive is not evidence; the sweep rows
# are, because they run.
#
# **A window whose lower edge is 650 us or higher used to be actively
# unsafe; the rahmonic screen is what closed that.** The cepstrum of a comb
# has rahmonics at 2*tau, 3*tau, ..., so a window that excludes the true
# delay can still contain a rahmonic of it. Below ~650 us the lower-edge
# margin (WINDOW_EDGE_MARGIN_STEPS) happened to catch them; at and above it
# the rahmonic cleared the margin, the envelope estimator found a matching
# peak in the same raised window, the two corroborated, and the result was a
# confident number at roughly 3x the true delay. Measured on the 150-400 us
# cloud: (600, 1000) was already clean, while (650, 1000), (700, 1000) and
# (800, 1200) each read ``geometry_locked`` at median tau 814 / 857 / 897
# us, at confidence up to 1.000. The admitted estimates' cepstral peaks sat
# at these ratios to their position's true delay, **per window** — the
# spread is not uniform, so it is not stated as one range:
#   (650, 1000): 2.992, 3.001, 3.648
#   (700, 1000): 2.992, 3.001
#   (800, 1200): 2.985, 2.994
#
# All three now refuse. ``RAHMONIC_MARGIN``'s screen rejects every one of
# those admitted estimates by 21.5-78.8x (weakest per window: 22.0, 22.0,
# 21.5 — i.e. at least 13.0x the margin itself; the ratios are cepstral and
# did not move under #1750, the multiple did, with the constant), and each
# window's verdict becomes not-locked with no usable estimates at all. The
# inverted
# assertions live in
# test_rahmonic_false_lock_under_a_raised_window_is_screened (renamed from
# ..._is_a_known_limitation, which is what it was while it asserted the bug)
# and in the per-window sweep rows of
# test_below_window_cloud_verdict_by_raised_search_window.
#
# **Why the screen is a "stronger peak below", not a tau/2, tau/3
# submultiple re-test** — the 3.648 outlier above is the reason, and an
# earlier revision of this comment prescribed the submultiple test anyway.
# That outlier — true delay 205.6 us, cepstral peak 749.8 us, reported
# envelope tau 814.4 us — **is** the 814.4 median that used to be pinned for
# (650, 1000), i.e. the module's own worst measured case. Its reported tau is
# 3.96x the truth and its cepstral peak 3.65x, so neither tau/2 (407.2 us)
# nor tau/3 (271.5 us) lands anywhere near 205.6 us: the prescribed screen
# would have missed the very case it was written for. The rule that shipped
# handles non-integer ratios because it never assumes one — it asks only
# whether a stronger peak exists at *any* lower quefrency, and for this case
# finds it at 214.2 us, 0.12 quefrency steps from the truth.
#
# **A raised window pays for the screen, in the fail-safe direction.** The
# screen asks only "is something much stronger below?", and an honest
# in-window echo sitting under a stronger *earlier* reflection answers yes
# just as a rahmonic does. Swept on two-echo IRs — an earlier 250-400 us
# reflection at r 0.4-0.6 below the window, a genuine 850-1000 us echo at
# r 0.15-0.25 inside it, five windows from (700, 1100) to (800, 1200) — 605
# of 720 cases are refused, at lower/candidate ratios 1.678-4.513, even though
# the envelope had located the real late echo to within 0.894% and the two
# estimators agreed well enough to score every time (corroboration at most
# 0.266, always below ``CORROBORATION_LOOSE``). That count was 482 until
# #1750 lowered ``RAHMONIC_MARGIN`` to 1.65; the readings themselves did not
# move (every ratio in every leg is bit-identical, because they are cepstral),
# so this is the price of the tighter screen, paid where the remedy below
# already applies. Three things bound the hazard:
#
# * It needs the stronger-earlier-echo geometry, not merely a raised window:
#   single-echo raised-window cases refuse 0 of 370, at ratios 0.217-0.942.
# * It does not arise at the default window, which contains the earlier
#   reflection instead of excluding it: the same 144 two-echo geometries
#   (432 readings over three noise seeds) refuse 0 at (120, 800), at ratios
#   0.166-0.945, and read the earlier echo to within 2.398%.
# * The failure is a refusal, never a wrong number.
#
# So the remedy is the default window rather than a screen exemption, and
# "prefer the default" is the rule this hazard argues for a second time.
#
# **That grid is committed, not described.** Every figure above is an
# attribute of ``_two_echo_hazard_sweep`` in tests/test_spatial_combine.py,
# whose grid literals are the ``_HAZARD_*`` constants beside it, re-derived by
# test_raised_window_two_echo_hazard_is_bounded_by_the_default_window
# (``JTS_RAHMONIC_CALIBRATION=1``, ~9 s), which also prints them under
# ``-s``. Re-measured 2026-08-02 on the unified window boundary and the
# re-derived ``RAHMONIC_MARGIN``; first committed 2026-07-25. An earlier
# revision quoted them from a scratch script instead, and a reader
# reconstructing that grid from this prose got 479 refusals rather than the
# 482 the script had found (both figures are from that pre-2026-07-25 era,
# when the margin was 2.0) — a described grid is not a grid. The single
# hand-picked case is pinned
# separately by
# test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one.
#
# **What is still only as good as the sweep behind it.** The screen is
# calibrated on a measured gap (see ``RAHMONIC_MARGIN``), not proved; the
# default window remains the one with the longest false-lock record, and a
# raised window is now *screened* rather than *validated*. Every window
# figure above — the false locks, the screen closing them, and the
# raised-window cost — comes from synthetic one- and two-echo IRs plus a
# three-frame single-position corpus. Real multi-bounce behaviour through a
# raised window has never been measured, and waits on the plan's S0 capture
# session. Prefer the default.
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
# Why the lower edge only — the *aliasing* asymmetry is physical. A
# below-window echo aliases UP onto the lower edge because that is where both
# searches begin; nothing aliases DOWN onto the top, since an above-window
# echo is simply outside both searches and is rejected outright by the window
# contract. A top-edge margin would therefore refuse honest measurements to
# guard against an aliasing path that genuinely does not exist there.
#
# **What that argument does NOT cover, and an earlier version of this comment
# wrongly implied it did: a cepstral rahmonic.** The cepstrum of a comb
# repeats at 2*tau, 3*tau, ..., so a below-window echo can put a peak
# *anywhere* in a raised window, not only at its bottom — which is a
# different mechanism from aliasing and is not addressed by any edge margin,
# at either edge. This rule catches those rahmonics only incidentally, and
# only while the window's lower edge stays under ~650 us. Above that they
# clear the margin, which is why the separate rahmonic screen below exists;
# do not read this margin as making a raised window safe on its own.
WINDOW_EDGE_MARGIN_STEPS = 1.0

# How close to a whole sample a window edge may sit before the envelope's
# index bounds treat it as *being* that sample. Used only by
# ``_ceil_samples`` / ``_floor_samples``, i.e. only to turn the caller's
# ``search_us`` into the closed range of sample delays inside it.
#
# **Why a tolerance is needed at all**, and it is not float noise: a caller
# expressing a sample-aligned edge in microseconds has to write a decimal.
# Eight samples at 48 kHz is 166.6666...us, and the window this module's own
# docstring uses for the sample-aligned case is written ``166.6667`` — which
# is 8.0000016 samples, so a bare ``ceil`` excludes sample 8 and silently
# costs the caller a whole sample (20.8 us) to honour a 0.000033 us
# overshoot. ``floor`` has the mirror exposure at the upper edge.
#
# **Why 1e-3 of a sample** (20.833 ns at 48 kHz): a D-decimal microsecond
# value is off by at most 0.5e-D us, i.e. 2.4e-(D+2) samples at 48 kHz, so
# this covers a two-decimal edge 4x over and the four decimals a
# sample-aligned edge actually needs (166.6667) 400x over — while being
# 1000x finer than the sample period it rounds to and ~3 400x finer than the
# detector's own ~71.43 us quefrency resolution. No window a caller can
# meaningfully distinguish moves because of it. Pinned by
# test_window_edges_snap_to_a_sample_aligned_request.
WINDOW_EDGE_SNAP_SAMPLES = 1e-3

# Rahmonic screen — the rule that closes the mechanism the edge margin above
# cannot reach. A comb's cepstrum repeats at 2*tau, 3*tau, ..., so a window
# that excludes the true delay can still contain a *rahmonic* of it, at an
# arbitrary place in the window rather than at an edge.
#
# **The rule.** A candidate that has survived the window and edge checks is
# refused when the strongest detrended-cepstrum peak *below* it — over the
# analyzable region running from ``RAHMONIC_FLOOR_STEPS`` quefrency steps up
# to (but not including) the candidate's own bin — exceeds the candidate by
# more than ``RAHMONIC_MARGIN``. A rahmonic is by construction weaker than the
# fundamental that produced it, so "a much stronger peak sits at a lower
# quefrency" is *necessary* for the candidate to be one — it is not
# sufficient, and the screen is that necessary condition tested directly. It
# needs no assumption about the ratio being an integer.
#
# **What the signature does not distinguish.** An honest in-window echo
# sitting under a stronger, unrelated *earlier* reflection presents exactly
# the same picture: something much stronger below, at a quefrency the
# window excluded. From one record the two are not separable — measured, the
# ratios interleave rather than sitting in different bands (see
# ``DEFAULT_ECHO_SEARCH_US`` for the sweep and the remedy, and
# test_rahmonic_screen_refuses_an_honest_late_echo_under_a_stronger_earlier_one
# for the pinned case). The screen resolves the ambiguity toward refusing,
# which is the fail-safe direction: the caller loses a measurement it could
# have had, rather than being handed a delay that is roughly 3x wrong.
#
# **Why not a tau/2, tau/3 submultiple re-test** (which an earlier revision of
# this file prescribed): the module's own worst measured case defeats it. On
# the 150-400 us cloud searched in (650, 1000), the position with a true delay
# of 205.6 us was reported at 814.4 us with a cepstral peak at 749.8 us —
# 3.96x and 3.65x the truth, i.e. not a submultiple relationship at all. The
# nearest the prescribed test could get is tau/3 of the reported value, 271.5
# us, which is 32% and 0.92 quefrency steps away from 205.6 us: it could only
# have caught this by accepting a whole step of slop, and a test with that
# much slop accepts nearly anything inside its own resolution. The rule above
# does not have to guess a ratio — it finds the fundamental directly, at 214.2
# us, 4.2% and 0.12 quefrency steps from the truth, winning by 78.8x. Pinned
# by test_rahmonic_screen_catches_the_non_integer_ratio_the_submultiple_test_missed.
#
# **RAHMONIC_MARGIN = 1.65, calibrated to sit in a measured gap** — the same
# posture as ECHO_CONFIDENCE_FLOOR. Two populations are swept, each on its own
# grid (they are looking for opposite things, so one grid could not have
# produced both), and each classified by what the *pre-screen* detector did
# (the sweep disables the screen and reads ``lower_peak_ratio`` off the
# record, so it measures the shipped code rather than a re-implementation).
#
# **The sweep is committed, not quoted.**
# test_rahmonic_margin_calibration_populations_bracket_the_constant in
# tests/test_spatial_combine.py regenerates both populations from the grids
# below and re-derives the gap. It is skipped unless
# ``JTS_RAHMONIC_CALIBRATION=1`` (~30 s), and prints its four figures on
# stdout under ``-s`` so this comment is one command away. Every figure here
# is that test's own output, re-measured 2026-08-02 on the unified window
# boundary (#1750):
#
# * **True positives — 2908 readings**, from bare impulse+echo IRs and
#   shaped-response IRs over tau 200-770 us x r 0.10-0.75, each crossed with
#   13 search windows from (120, 800) to (800, 1200); admitted when the
#   pre-screen detector was unrefused, at or above ECHO_CONFIDENCE_FLOOR, and
#   within 15% of truth. Their lower/candidate ratio peaks at **0.9955**
#   (0.995547). That ceiling is not low-quefrency leakage but the candidate's
#   own main-lobe shoulder: the worst case (tau 740 us, r 0.75, searched in
#   (200, 900)) has its true quefrency between two cepstral bins — 0.36 of a
#   step above the lower one — so the peak's energy straddles both and the bin
#   below the argmax reaches 0.9955 of it. **This population did not move at
#   all under #1750**, count or ceiling: its worst case is searched in
#   (200, 900) us, whose edges are 9.6 and 43.2 samples at 48 kHz, and
#   ``round`` and ``ceil``/``floor`` agree on both.
# * **Wrong readings — 439 readings**, from the same two IR families but with
#   tau 100-455 us, crossed with 11 windows from (400, 900) to (1000, 1600) —
#   i.e. windows that exclude the true delay, which is what makes a rahmonic
#   reachable; admitted when the pre-screen detector was confident and more
#   than 15% off truth. Their ratio bottoms out at **2.7899** (2.789915).
#
# **What #1750 cost, and why the constant moved.** Before the window boundary
# was unified, this population was 409 readings with a floor of 4.9192, and
# 2.0 sat 2.01x above the ceiling and 2.46x below the floor. Removing the
# half-sample leak below ``search_us[0]`` stops the envelope railing on an
# out-of-window sample, so 30 records that used to be saved by a railed
# estimate now find their real in-window maximum and *corroborate the cepstral
# rahmonic* instead — a weaker form of the mechanism the rejected
# "recover the measurement" alternative was rejected for (see the
# ``earlier_dominant_arrival`` site, which measured exactly this population as
# 439 / 2.7899 when it scoped that alternative down to the rounding defect).
# The gap is therefore narrower on its lower wall, and the constant is
# re-centred in what is left rather than left grazing it: 1.65 is 1.66x above
# the ceiling and 1.69x below the floor, either side of the 1.667 geometric
# centre, so the 1.5x bracket the sweep asserts still holds on both walls.
# **The verdicts are unchanged by the move**: at 1.65 as at 2.0 the screen
# still rejects 439/439 of the wrong readings and 0/2908 of the right ones —
# the constant moved to restore headroom, not to change an answer. Lowering it
# does make the screen refuse more at *raised* windows (see
# ``DEFAULT_ECHO_SEARCH_US``'s two-echo hazard, re-derived on the same tree);
# that cost lands only where the module already says to prefer the default,
# and the default window (5.76 / 38.4 samples) is untouched by #1750.
#
# A margin of exactly 1.0 ("any stronger peak") would also separate the two
# populations, but with only 0.5% of headroom over the shoulder case above —
# the sub-bin geometry that produces it is ordinary, so that headroom is not a
# margin.
#
# The regenerated populations are synthetic only; the three corpus IRs the
# original sweep folded into its true positives are absent in CI, and their
# headroom is pinned separately (0.329-0.387, by
# test_detect_echo_finds_the_corpus_bounce). A pre-2026-07-25 revision of this
# comment quoted 2925 / 528 readings and a floor of 3.531 from a sweep whose
# grid steps were never recorded, and was not diagnosable against the
# committed grid for exactly that reason — which is why the sweep is a test
# now, and why the two moves since are separable: the 2026-07-25 regeneration
# (grid written down) and the 2026-08-02 boundary unification (detector
# changed). Only the second is a real behaviour change, and it is the one that
# lowered the floor.
#
# **RAHMONIC_FLOOR_STEPS = 1** — whole quefrency steps, because the cepstrum
# is only defined on them. It excludes exactly one bin, the zero-lag bin,
# whose magnitude is the residual DC of the detrended, Hanning-windowed
# log-magnitude rather than a ripple period: a "peak" at zero quefrency is not
# a delay hypothesis, so a candidate must not be judged against it. That is a
# choice about meaning, not a rescue from leakage, and the measurement says
# so — a floor of 0 produces the same 0.9955 ceiling on the true-positive
# population and the same verdict on all 460 detector calls this module's test
# suite made at commit 6888ac15e (a count that moves with the suite, so it is
# stamped rather than maintained), because the cubic detrend does its job.
# Measured on the real
# corpus, the zero-lag bin is 0.120-0.154 of the candidate peak and the two
# bins above it 0.234-0.340, so the low-quefrency region is nowhere near able
# to auto-refuse an honest reading. That headroom is pinned on real data by
# test_detect_echo_finds_the_corpus_bounce.
RAHMONIC_FLOOR_STEPS = 1
RAHMONIC_MARGIN = 1.65

# Signal-presence screen — how far the analysis band may sit below the
# caller's *declared* passband before the detector refuses to read it.
#
# **The gap this closes.** Nothing above checks that ``band_hz`` contains
# signal at all. The arrival-crest gate asks whether the IR has a direct
# arrival, which a band-limited driver's IR does even when the analysis band
# is pure filter stopband; the cepstrum then finds a "ripple" in
# quantisation noise, the envelope finds a "peak" in it, and the two can
# agree. Measured on the 2026-07-25 electrical loopback of the live JTS3
# CamillaDSP graph (captures/flat-linearization-20260725/s0-analysis/
# loopback/LOOPBACK-REPORT.md § 5a): searched in the 5-19 kHz default band,
# the **woofer** branch — an LR4 lowpass at 2 kHz, so that band is stopband
# residue — returned ``tau = 323.3 us, strength = -13.13 dB, confidence =
# 0.275, refusal = ""`` on the sweep stimulus. It did not reproduce on the
# impulse or MLS stimuli of the same graph, which is what a real arrival
# would have done.
#
# **The rule.** The caller declares the passband (``signal_band_hz``); the
# detector refuses with ``band_below_passband`` when the declared passband's
# level exceeds the analysis band's by more than this margin. The module
# stays product-blind — it never guesses a driver's band — and a caller that
# passes ``None`` keeps the pre-existing behaviour exactly.
#
# **25.0 dB, calibrated to sit in a measured gap**, the same posture as
# ``RAHMONIC_MARGIN`` and ``ECHO_CONFIDENCE_FLOOR``. The statistic is the
# one defined at the gate — ``EchoDiagnostic.band_deficit_db``, power-domain
# band means of the early-arrival **windowed segment's** spectrum (see
# :func:`detect_echo`) — read off the shipped code at the default
# ``band_hz`` (5-19 kHz) and ``search_us`` (120, 800) us, on 2026-07-25.
# Three populations, 22 records, every figure re-derived by the corpus tests
# in tests/test_spatial_combine.py:
#
# * **Honest acoustic captures — 16 records, ceiling 12.07 dB.** The
#   2026-07-24/25 cdhorn corpus (3: 1.11 / 1.54 / 5.51 dB), the S0 main-leg
#   desk cloud (10: 1.04-6.56 dB), and the S0 ground-plane leg (3: 8.28 /
#   8.60 / 12.07 dB), each against its own declared passband — 150 Hz-20 kHz
#   for a summed capture, 2-20 kHz for the tweeter-alone one. The
#   ground-plane readings are the ceiling because tipping the cabinet at the
#   floor cost top-octave level; they are the honest worst case precisely
#   because that leg's protocol went wrong in every other way too.
# * **Stopband residue — 3 records, floor 40.43 dB.** The electrical
#   loopback's woofer branch against its 200-2000 Hz passband, on all three
#   stimuli (40.43 sweep / 41.98 impulse / 41.98 MLS). Only the sweep
#   produced the confident-looking tau; the screen refuses all three,
#   because what is wrong is the question, not the stimulus.
# * **In-band control — 3 records, -0.17 to -0.05 dB.** The same loopback's
#   *tweeter* branch against its own 2.5-20 kHz passband, i.e. the metric
#   does not manufacture a deficit out of an electrical IR as such.
#
# 25.0 leaves 12.93 dB of headroom above the honest ceiling and 15.43 dB
# below the residue floor — near-centred in the natural unit, which is dB.
# The two populations are 28.36 dB apart, so this is a wide gap rather than
# a tuned edge, and the closest any honest record comes to firing is that
# 12.93 dB (the ground-plane leg's worst position).
#
# **Whose speaker.** All 16 honest acoustic records are the **same
# speaker** — the JTS3 cdhorn — across three sessions and two mic
# mountings. The margin has never seen a driver with a genuinely different
# HF rolloff, so the honest population's *ceiling* is the number to watch
# when this ships against other hardware.
#
# **A hard constraint on the analysis band, which PR-4 derives from a
# driver contract rather than leaving at the default** (`_derive_cloud_echo_band_hz`
# in `jasper.active_speaker.crossover_v2_flow`, landed 2026-07-26). Re-measured on the 13
# S0 acoustic records and the 3 loopback residue records across six bands,
# the honest side is comfortable everywhere — individual readings span
# 0.40-17.50 dB and the worst *per-band ceiling* is 17.50 dB, at
# (8000, 16000), still 7.50 dB clear of the margin. The **residue** side is
# not, and it fails in the direction that matters:
#
#   band            residue deficit    screen catches it?
#   (5000, 19000)   40.43-41.98 dB     yes  (the default)
#   (10000, 19000)  47.47-63.30 dB     yes
#   (8000, 16000)   45.82-55.95 dB     yes
#   (4000, 20000)   35.46-35.58 dB     yes
#   (3000, 19000)   26.53-27.05 dB     yes, by 1.53 dB
#   (2000, 19000)   18.21-18.23 dB     **NO**
#
# At an analysis band whose lower edge sits *on* this speaker's 2 kHz
# crossover, the woofer branch's own passband is inside the band being
# analysed, its deficit collapses below the margin, and the screen stops
# catching the exact case it exists for. This is not a narrowed gap — it is
# a false negative, and it degrades silently to the pre-screen behaviour
# (the confident-looking tau out of stopband residue). The 3 kHz row shows
# the margin is already thin one octave up. **A caller must keep the
# analysis band clear of the crossover.** Deriving it from the tweeter's
# usable range does NOT do that on its own — corrected 2026-07-27 (issue
# #1763): JTS3's cdhorn tweeter correctly declares a measurement band
# starting at 2000 Hz, which is exactly this speaker's crossover, and the
# first real cloud session ran the detector on the (2000, 18000) row above.
# What makes it true by construction is the caller's own floor:
# `crossover_v2_flow.ECHO_BAND_HF_REGIME_FLOOR_HZ` (4000 Hz, anchored to
# this table) clamps the derived lower edge up and discloses the clamp.
# Re-derived by test_band_deficit_separation_depends_on_the_analysis_band.
#
# **This is not the 49.7 dB the loopback report quotes**, and the difference
# is entirely the metric, not the signal. Three changes separate them, and
# the decomposition is exact on the woofer sweep IR (re-derived by
# test_the_report_s_deficit_decomposes_into_the_shipped_statistic):
#
#   49.66 dB  the report: amplitude mean (20*log10), whole 65536-point IR
#             spectrum, passband 200-1500 Hz
#   42.97 dB  ...taking the *power* mean (10*log10) instead — this module's
#             estimator everywhere else, so the statistic composes
#   40.83 dB  ...over the early-arrival **windowed segment** the cepstral
#             stage already computes, rather than the whole IR — so the gate
#             costs no extra transform and reads exactly the data the
#             estimators are about to read
#   40.43 dB  ...against the passband the gate is actually given here,
#             200-2000 Hz rather than the report's 200-1500 Hz
#
# Both statistics separate the same populations; only the last is shipped,
# so only its regime is quoted above.
#
# **What is NOT calibrated: a passband narrower than the analysis band, or
# overlapping it.** Every record above declares a passband that contains or
# sits well away from the analysis band, which is the shape a driver
# contract produces. A caller that declares something else gets an
# arithmetically well-defined deficit and no measured basis for the
# threshold applied to it.
BAND_BELOW_PASSBAND_MARGIN_DB = 25.0

# Earlier-dominant-arrival dominance floor — how loud a below-window arrival
# must be, relative to the direct arrival, before the detector will call it
# **dominant** and refuse in its name.
#
# **The gap this closes.** Without a level test, "is there any local maximum
# below the window" is the whole criterion, and the band-limited envelope of
# an echo-free impulse has plenty: its own ringing.
#
# **The sweep is committed, not quoted.** Every figure below comes from one
# ladder — ``_CALIBRATION_WRONG_READING_WINDOWS`` in
# tests/test_spatial_combine.py, the eleven raised windows from (400, 900)
# to (1000, 1600) us that the file already commits for the rahmonic
# calibration, reused here rather than duplicated — crossed with the
# 60-member impulse-with-no-echo family behind ``ECHO_CONFIDENCE_FLOOR``
# (delta at unity plus white noise, sigma 0.02 and 0.001, 30 seeds each).
# 660 readings, re-derived on demand by
# test_earlier_arrival_dominance_floor_sits_in_the_measured_gap (~4 s, in
# the default lane). An earlier revision of this comment quoted counts from
# a mixture of ladders and could not be reproduced from any one of them,
# which is the failure mode ``RAHMONIC_MARGIN`` already names: a described
# grid is not a grid.
#
# Measured 2026-07-25, re-derived 2026-08-02 on the unified window boundary
# (#1750), at the default 5-19 kHz band, on the shipped statistic
# (``EchoDiagnostic.earlier_arrival_db``):
#
# * **Must not fire — echo-free ringing.** The envelope finds a below-window
#   local maximum on **658 of 660** readings, spanning **-32.1297 to
#   -17.1365 dB** — all three figures bit-identical across #1750, because
#   this scan's bound was already the correct one. The ceiling is the binding
#   figure. With the floor removed (mutated to -120 dB) those readings produce
#   **6** ``earlier_dominant_arrival`` refusals, at most **3 of 60** in a
#   single window, at (1000, 1600) — the detector's own skirt named as an
#   arrival, and the widen-the-window guidance a consumer builds on it firing
#   on nothing. With the shipped floor: **0 of 660**.
#
#   That mutation count was **21** at (400, 900) before #1750, and the drop
#   is the boundary fix rather than the floor: a flip needs the envelope's
#   answer *below* ``search_us[0]``, and the envelope can no longer select an
#   out-of-window sample — it reaches below only by parabolic refinement, so
#   at most half a sample past ``first``. The mutation still re-opens the
#   defect it exists to prove, on a population the shipped floor refuses 0
#   of, so the constant is unchanged and its gap is unchanged.
# * **Must fire — the S0 ground plane, n=3: -0.64, -2.01, -2.57 dB** at
#   125-146 us. A mic capsule left centimetres proud of the floor; the
#   loudest thing on those records, and the reason all three measured
#   nothing.
#
# -10.0 leaves **7.43 dB** of headroom below the ground-plane floor and
# **7.14 dB** above the ringing ceiling — centred in a 14.58 dB gap.
#
# **A positive control sits between the two, and it is why this floor is
# not merely a noise gate.** The S0 main-leg desk cloud has a real
# below-window arrival at 145.8 us on 4 of its 10 positions, at -14.66 to
# -15.71 dB — a genuine boundary bounce, weaker than this floor. All ten of
# those positions nonetheless produced a credible detection of the ~320 us
# rim wave (confidence 0.294-0.961; nine of the ten above 0.85, cloud_04 the
# outlier at 0.294 — see the pinned table in
# test_main_leg_is_unchanged_and_is_the_ground_plane_s_control), so none of
# them reaches this branch at all. That is the empirical content of the
# floor: an arrival that weak demonstrably does **not** take the envelope's
# answer, so declining to call it dominant matches what the instrument
# actually did. The guard does not lean on "they score above zero anyway" —
# it would exclude them on level too.
EARLIER_ARRIVAL_DOMINANCE_DB = -10.0

# Refusal vocabulary for EchoDiagnostic.refusal. Snake_case, self-
# identifying, and stable: an empty string means "the detector ran to
# completion and is reporting a measurement" (which may still be a
# zero-confidence "found nothing credible"); any non-empty value means the
# detector declined to measure and every estimate on the record is
# uninformative. Consumers gate on `refusal == ""`, never on the specific
# slug, so this vocabulary can grow without breaking them.
# Listed in the order :func:`detect_echo` can emit them, which is also the
# order its returns appear in the source — a reader following either can
# check the other.
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
      position_id: caller's label for the position (the v2 session's
        position-group id). Carried through to
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
      role: what KIND of listening position this is, in the caller's own
        vocabulary (the v2 cloud's ``onax`` / ``offax`` / ``xovr``). ``""``
        when the caller does not say, which every pre-existing caller does not.

        **Carried, never read by the combination.** The reduction stays an
        unweighted power mean and an unweighted dB median: no role is
        down-weighted, up-weighted, or excluded, and there is no ``weights=``
        argument anywhere in this module. The role exists so a per-position
        NUMBER can be labelled when it is reported — "off-axis 2.9 dB" is an
        instruction where "spread 1.7 dB" is a mood — which is exactly the
        one thing :func:`position_residuals` needs and the combiner does not.
    """

    position_id: str
    freqs_hz: np.ndarray
    magnitude_db: np.ndarray
    sample_rate: int
    ir: np.ndarray | None = None
    role: str = ""


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
        better than one step. Three rules are written in these units: a
        candidate within ``WINDOW_EDGE_MARGIN_STEPS`` of the search
        window's lower edge is refused by :func:`detect_echo`; the rahmonic
        screen's analyzable region starts ``RAHMONIC_FLOOR_STEPS`` steps
        above zero (counted in cepstral bins, so exactly rather than via
        this rounded figure); and a ``tau_us`` below roughly
        **3 * resolution_us** is "an early echo is present" rather than a
        trustworthy delay, which :func:`assess_geometry` enforces before
        clustering.
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
        (0.0 = identical). **1.0 means exactly one thing: the two could not
        be compared** — the envelope found nothing, either estimate fell
        outside the search window (a rejected candidate corroborates
        nothing), or the detector refused before both estimates existed. It
        is a marker, not a measurement, and must not be read as "measured,
        and they disagreed completely".

        On a *refused* record the distinction is load-bearing. Most
        refusals carry the 1.0 marker because nothing was comparable. The
        two late refusals — ``tau_at_window_lower_edge`` and
        ``rahmonic_of_lower_delay`` — are the exceptions: both fire after
        the estimators have been compared, so both carry whatever that
        comparison produced (the **measured** value when both candidates
        were in-window, the 1.0 marker when only one was).

        ``earlier_dominant_arrival`` is later still and is *not* a third
        exception. It requires — among other conditions — that the
        envelope's answer fell below the window, so only one candidate was
        ever in-window and the marker it carries is always the honest one. A
        1.0 there says what it always says: the two were never compared,
        which is exactly the condition that refusal exists to name. (The
        converse does not hold — an answer below the window is necessary for
        that refusal, not sufficient; see :func:`detect_echo`.)

        **Do not infer the refusal's character from that value — a late
        refusal may carry any corroboration at all.** Two scopes, kept
        separate on purpose. Narrowly: the ten-position 150-400 us cloud
        searched at (300, 800) — a fixed, seeded test — produces edge
        refusals whose measured corroboration spans 0.005-0.28, i.e. tight
        agreement is the common case there. Broadly: across the late
        refusals this module's tests produce, the measured value spans
        several orders of magnitude, from below 0.001 to above 3. The two
        late paths differ in that respect and the difference is worth
        knowing — among **edge** refusals, values above
        ``CORROBORATION_LOOSE`` are ordinary rather than exceptional, while
        no **rahmonic** refusal in this suite has yet carried one (that
        refusal usually fires on a candidate both estimators agreed about,
        which is exactly what made the false lock credible). Treat the
        second half as an observation to date, not a guarantee.
        So the field is **record-specific and never population-typical** —
        read it off the record in front of you, and do not carry a range
        quoted here into a judgement about a different record. (No census
        count is quoted for the broad population deliberately: it is a
        property of whatever tests happen to exist, so it would be stale
        the next time one is added.)

        So both directions occur and both are refusals. A *small* value
        means the two estimates were comparable and agreed, and the pair was
        refused for some other reason than disagreeing. A *large* value
        means they were compared and disagreed. Neither is evidence for or
        against the refusal — an edge refusal turns on distance to the
        window edge and a rahmonic refusal on ``lower_peak_ratio``, and
        neither rule reads this field. What the field guarantees is only
        that it preserves what was measured instead of substituting a
        fabricated marker.
      arrival_crest_db: direct arrival level above the IR's median
        ``|sample|`` level — the "is there an arrival at all" gate.
      lower_peak_us: quefrency of the strongest detrended-cepstrum peak
        *below* the cepstral candidate, over the rahmonic screen's analyzable
        region (see ``RAHMONIC_MARGIN``). 0.0 when there was no such region —
        the candidate sat at or below ``RAHMONIC_FLOOR_STEPS``, or the
        detector returned before the region was measured (the crest,
        window-length and band-width gates, and
        ``search_window_outside_cepstrum``, which fires after a cepstrum
        exists but before this region is scanned).
      lower_peak_ratio: that peak's magnitude divided by the candidate's, so
        the screen's verdict is recomputable from the record rather than
        asserted by it: a ``rahmonic_of_lower_delay`` refusal is exactly
        ``lower_peak_ratio > RAHMONIC_MARGIN``. It is reported on *every*
        record that got as far as that scan, not only refused ones, because
        the interesting reading is often the one that did **not** refuse — a
        consumer watching this number creep toward the margin on honest
        detections is watching the screen's headroom erode. The corpus
        detections sit at 0.329-0.387 against the shipped ``RAHMONIC_MARGIN``
        (pinned by
        test_detect_echo_finds_the_corpus_bounce); the widest reading in the
        calibration sweep's true-positive population is 0.9955 (see
        ``RAHMONIC_MARGIN``).

        **0.0 in both fields means "not measured", unambiguously.** A
        measured region starts at bin ``RAHMONIC_FLOOR_STEPS`` (>= 1), whose
        quefrency is strictly positive, so a measured record always reports a
        positive ``lower_peak_us`` — including the degenerate case where the
        cepstrum below the candidate is all zeros and the *ratio* is a
        genuine 0.0.
      effective_floor_us: the delay below which **this** window cannot
        report an arrival at all: ``search_us[0] + WINDOW_EDGE_MARGIN_STEPS
        * resolution_us``, the stated lower edge plus the edge-margin dead
        zone. Populated on **every** record the detector returns, refusals
        included, because a consumer disclosing "arrivals below ~X us are
        invisible to this window" needs X most when the window found
        nothing. For the defaults (120 us, 5-19 kHz) it is ~191.4 us — the
        number that makes the S0 ground plane's 125-146 us proud-capsule
        arrivals *structurally* unreportable rather than merely unreported,
        and the reason the plan surfaces this floor instead of raising the
        default lower edge (docs/flat-linearization-productization-plan.md,
        PR-2 item 4: disclosure over restriction).

        0.0 means "not computed": the only records carrying it are the ones
        :func:`combine_positions` builds when :func:`detect_echo` *raised*
        rather than returned, where the band — and so ``resolution_us`` — is
        unknown. Those records report ``resolution_us == 0.0`` too, so the
        two agree about what was not measured.
      earlier_arrival_us: delay of the strongest **genuine local maximum of
        the envelope at a delay below ``search_us[0]``**. It is a reading,
        never a candidate: the envelope's answer is chosen exactly as it
        always was, and this field changes nothing about it. 0.0 when there
        is none, which is unambiguous because a measured one has a strictly
        positive delay (the same convention as ``lower_peak_us``).

        **It is always an arrival the window strictly excluded, and there is
        one definition of "excluded" (#1750).** The scan that produces this
        field stops at the same index the envelope's own candidate range
        starts at — ``_ceil_samples(search_lo, rate)`` — so "below the
        window" and "in the window" partition the samples with no overlap.
        Until #1750 this bound was ``ceil`` while the envelope's was
        ``round``, and at any window whose lower edge is not sample-aligned
        the sample between them was simultaneously the envelope's first
        candidate and a below-window arrival by this field's reckoning. S0's
        ground_plane_01/02 were exactly that case: their
        ``earlier_arrival_us`` of 145.83 us was the *same envelope sample*
        that produced their ``tau_envelope_us`` of 137.5 / 139.4 us after
        parabolic refinement, through a (150, 1000) us window whose lower
        edge is 7.2 samples at 48 kHz.

        This field itself did not move when that was fixed — its bound was
        already the correct one — and neither did ``earlier_arrival_db``:
        across the 1027-record base-vs-branch differential behind #1750 both
        are bit-identical, including all three ground-plane positions
        (145.83 / 145.83 / 125.0 us). What moved is which refusal *names*
        them at a non-sample-aligned window; see
        ``REFUSAL_EARLIER_DOMINANT_ARRIVAL``'s site for that, and note the
        contrast with a *sample-aligned* lower edge (166.6667 us = 8 samples
        at 48 kHz), where all three positions are byte-identical to the
        pre-#1750 detector and still refuse ``earlier_dominant_arrival``.

        It is what an ``earlier_dominant_arrival`` refusal names, and it is
        reported on every record that reached the envelope stage — same
        reasoning as ``lower_peak_ratio``: the useful reading is often the
        one that did *not* refuse, and "there is something loud just under
        your window" is worth knowing about a successful measurement too.
      earlier_arrival_db: that arrival's level relative to the direct
        arrival, from the same envelope — ``strength_db``'s convention and
        units, for the same reason (a delay without a level is half a
        reading). ``STRENGTH_FLOOR_DB`` when ``earlier_arrival_us`` is 0.0.

        This is the field the dominance test reads, so an
        ``earlier_dominant_arrival`` refusal is recomputable from the
        record: it needs ``earlier_arrival_db >
        EARLIER_ARRIVAL_DOMINANCE_DB``. Reported whether or not it passes,
        because a below-window arrival that failed dominance is still worth
        seeing.

        Measured 2026-07-25 at the default 5-19 kHz band and the S0 leg-B
        (150, 1000) us protocol window: the three ground-plane positions
        read -2.57 / -2.01 / -0.64 dB at 145.8 / 145.8 / 125.0 us (the
        proud-capsule interloper, and the loudest thing any of those records
        saw), while on the ten-position main-leg desk cloud four positions
        carry one at 145.8 us and -14.66 to -15.71 dB and the other six have
        none at all. That contrast is the field earning its place: same
        speaker, same program, one changed mic mounting. Both figures are
        pinned by the S0 fixtures in tests/test_spatial_combine.py.
      band_deficit_db: how far the analysis band's level sits **below** the
        caller's declared passband, both as power-domain means of the
        early-arrival segment's spectrum. Positive means the analysis band
        is the quieter one, so a ``band_below_passband`` refusal is exactly
        ``band_deficit_db > BAND_BELOW_PASSBAND_MARGIN_DB`` — recomputable
        from the record, like every other refusal in this module.

        Reported on every record that got past the screen as well, for
        ``lower_peak_ratio``'s reason: a consumer watching this creep toward
        the margin on honest captures is watching the screen's headroom
        erode. Measured 2026-07-25 at the shipped defaults over the 16
        honest **acoustic** records the constant is calibrated on —
        **1.04 to 12.07 dB**, the minimum on the S0 main-leg desk cloud
        (cloud_06) and the maximum on its ground-plane leg
        (ground_plane_03), with the 2026-07-24/25 cdhorn corpus at
        1.11 / 1.54 / 5.51 dB in between. The electrical in-band control is
        a separate population reading -0.17 to -0.05 dB, and is not part of
        that range. See ``BAND_BELOW_PASSBAND_MARGIN_DB`` for the
        per-population breakdown.

        ``STRENGTH_FLOOR_DB`` means **not measured**, from any of three
        causes: no ``signal_band_hz`` was declared; the detector returned
        before the screen ran (the crest, window-length and band-width
        gates); or the screen ran but the declared passband covered no bin
        of the segment's spectrum, which is the documented fail-open for a
        passband narrower than one FFT bin. A finite sentinel rather than
        ``None`` so the field stays arithmetic-safe (the same choice
        ``strength_db`` makes), and one no real reading can collide with:
        -120 dB would mean an analysis band sitting a trillion times
        *above* its own passband.
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
      thin_evidence: the verdict is real but rests on the bare minimum.
        Exactly ``n_confident == GEOMETRY_MIN_CONFIDENT and n_positions >=
        2 * GEOMETRY_MIN_CONFIDENT`` — a cloud that supplied at least twice
        the minimum number of positions and still produced only the minimum
        number of usable estimates. A ten-position cloud whose verdict rests
        on two of them is the observed case (issue #1742 item 2).

        **It is a cliff, not a gradient, and a consumer phrasing a verdict
        from it must know that.** The equality is exact: with
        ``GEOMETRY_MIN_CONFIDENT == 2``, two usable estimates out of ten is
        thin and **three out of ten is not** — the flag goes False at three
        even though the evidence is barely better. It is deliberately the
        plan's rule verbatim rather than a smoothed version of it, so that
        the shipped behaviour and the specification cannot disagree; a
        UX layer wanting a gradient should read ``n_confident`` and
        ``n_positions`` directly rather than expect this bit to supply one.

        **Disclosure, not rejection.** The two estimates were honest, the
        clustering test ran on them correctly, and nothing here scales a
        threshold or withholds a verdict — a consumer is expected to phrase
        the same verdict more softly (PR-4's UX). Scaling
        ``GEOMETRY_CLUSTER_FRACTION`` or ``GEOMETRY_MIN_CONFIDENT`` with
        ``n_positions`` was the rejected alternative: it would have turned a
        measured verdict into no verdict, which is a worse answer than a
        qualified one.

        The rule is stated verdict-independently and computed in one
        expression. It is nonetheless unreachable on ``GEOMETRY_UNKNOWN``,
        structurally rather than by a branch: that reason fires exactly when
        ``n_confident < GEOMETRY_MIN_CONFIDENT``, which the equality above
        excludes. So in practice ``thin_evidence`` qualifies a
        ``GEOMETRY_LOCKED`` or ``GEOMETRY_DISPERSED`` verdict, and a reader
        does not have to check the reason before trusting the flag.
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

    The cheapest per-position trend surface this module can offer, and
    deliberately the only one: both operands are already on
    :class:`CombinedResponse` at group close, so it is one subtraction and one
    reduction — no new capture, no new pass, no new artifact.

    **What it separates, which is why it is worth banking per round.**
    :func:`~jasper.audio_measurement.interference_nulls.classify_dip_position_variance`
    already makes the position-invariant / position-dependent call at the level
    of one dip; this lifts the same distinction to the whole round. A residual
    that is large at EVERY position is broadband and ours — a role-level trim
    or model error, which is fixable. One that is large at a single position is
    the room or the placement, which is commanded for the achievable instead.
    Labelling it with the role is what makes it readable: "on-axis 0.4 dB,
    off-axis 2.9 dB" is an instruction; a bare spread number is a mood.

    It would NOT have caught the 2026-08-16 level-anchor incident, which was
    measured at the mark and had no spatial term. What it catches is the
    anchor-OUTLIER class, whose signature is exactly a broadband
    position-invariant residual.

    Args:
      position_id: the position this describes.
      role: its :attr:`PositionCapture.role`, or ``""`` when it declared none.
      rms_db: root-mean-square of ``per_position_diag_db -
        power_mean_diag_db`` over the graded band, in dB. ``None`` when the
        band selected no usable bin — an absence, never a fabricated zero,
        which would read as "this position agreed perfectly".

        **The DIAGNOSTIC-smoothed pair, not the raw one, and that choice was
        measured rather than assumed.** On the raw curves the dominant term is
        each position's own interference comb: a healthy dispersed
        four-position cloud reports ~2.5 dB at every position, and adding a
        broadband +4 dB offset to ONE of them moved its number by less than
        the spread between the untouched three — the metric could not see the
        very defect class it exists for. Smoothing averages the comb away and
        leaves the broadband disagreement, which is the signal. Both operands
        come from the same fraction and the same construction (see
        :attr:`CombinedResponse.per_position_diag_db`), so this adds no
        smoothing pass and no second convention.
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
      per_position_db: ``(n_positions, len(freqs_hz))``, row *i* being
        ``position_ids[i]``'s magnitude resampled onto the shared grid —
        **unsmoothed**, and after any ``MAX_ANALYSIS_BINS`` decimation, so it
        is exactly the array ``power_mean_db`` and ``median_db`` are reduced
        from. Retained (rather than left as a local) so a consumer that needs
        one position's curve reads the one this module built instead of
        rebuilding the canonical-grid + decimation chain.
      per_position_diag_db: ``per_position_db`` at the diagnostic fraction,
        row-for-row. Retained so a per-position feature can be measured with
        the *same* smoothing construction as the combined curves rather than
        a second one — the SSOT rule this module already applies to
        ``merged_true_intervals``. Its consumer today is
        :mod:`jasper.audio_measurement.interference_nulls`, whose
        ``position_invariant`` / ``position_dependent`` verdict is exactly
        "is this null present at this position", answerable only per
        position.

        **It costs one ``smooth_fractional_octave`` pass per position**, and
        that pass is the combiner's dominant term — the same cost
        :func:`_band_spread` was rewritten to *avoid* (see its docstring),
        which is why this is called out rather than left to be discovered.
        The pass count is exact and is what to reason from: on an
        N-position cloud this function makes ``3 + N`` smoothing passes where
        it used to make 3, so at the plan's N of 8-12 the per-position work
        is roughly three quarters of them. Measured 2026-07-25 on the S0
        ten-position desk cloud at the full 16384-bin analysis grid: 13
        passes, of which the 10 per-position ones took **40 %** of a 3.45 s
        call. (The share is quoted rather than the seconds because wall-clock
        on a shared laptop moved 2.9-3.5 s between runs of the same input;
        the ratio is measured within one run and is the stable figure.)

        Paid unconditionally because every shipped consumer of this module
        needs it — the null gate runs at every cloud-group end (plan PR-4) —
        so a default-off flag would be a footgun rather than a saving. The
        regime the cost was judged against is the plan's choreography, which
        runs this **once per position group** inside a multi-minute session
        (PR-3b), not per capture. That judgement has **not** been re-measured
        on a Pi 5.
      excluded: per-bin mask, True where the two diagnostic curves disagree
        by more than ``flag_threshold_db``. Downstream: excluded from
        correction *and* from pass/fail, and reported to the user.
      excluded_bands_hz: ``excluded`` as merged ``(f_lo, f_hi)`` intervals,
        one per contiguous run. No gap-bridging is applied — two runs
        separated by a single unflagged bin stay two intervals; a consumer
        wanting coarser reporting should post-process.
      n_positions: number of captures combined.
      position_ids: their ids, in input order.
      position_roles: their :attr:`PositionCapture.role` values, index-aligned
        with ``position_ids``. ``""`` for a capture that declared none. Copied
        through untouched — nothing in the combination reads it; see
        :attr:`PositionCapture.role`.
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
      signal_band_hz: the declared passband actually handed to
        :func:`detect_echo`'s signal-presence screen, or ``None`` when no
        passband was declared and the screen therefore did not run. Recorded
        under the same "actually applied" convention as the two fields above
        — and for the same reason: a ``band_below_passband`` refusal, or its
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
    # Defaulted so the two new arrays are additive: every existing
    # positional construction of this dataclass keeps working. An empty
    # array is not a legal value from :func:`combine_positions`, which
    # always populates both — it is the value a hand-built or deserialised
    # record carries when the per-position curves were never retained.
    per_position_db: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    per_position_diag_db: np.ndarray = field(
        default_factory=lambda: np.empty((0, 0))
    )
    #: Defaulted on the same terms as the two arrays above: additive, so every
    #: existing construction keeps working, and ``()`` is what a hand-built or
    #: deserialised record carries. :func:`combine_positions` always populates
    #: it, with ``""`` for a capture that declared no role.
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
    cost/resolution argument (``max_bins=None`` reads it fresh on every call
    rather than baking it into a default value, so
    ``monkeypatch.setattr(module, "MAX_ANALYSIS_BINS", ...)`` still works —
    the same late-binding contract the module-level free-variable lookup
    this replaced already had; issue #1858's persisted-prior caller passes
    an explicit smaller ceiling for a durable-state payload rather than an
    in-memory analysis pass, the same alias-free rule at a different target
    density).

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
    STACK of positions, and a caller holding a single curve (the v2 session's
    predicted branch sum, on its way to be spec-graded) needs the identical
    block-average so its curve is smoothed and evaluated at the same grid
    density as the measured one it will be compared against. Exported for the
    same reason :func:`merged_true_intervals` is: the alternative is a near-copy
    of a rule whose whole value is having one owner.

    ``max_bins`` (``None`` reads :data:`MAX_ANALYSIS_BINS` fresh on every
    call — see :func:`_decimate_to_analysis_grid`'s docstring for why not a
    baked-in default) is the second caller this owner grew it for:
    `jasper.web.correction_crossover_v2._decimate_sum` (issue #1858) needs
    the same alias-free block average, but bounded at the persisted-state
    ceiling (``MAX_PERSISTED_SUM_POINTS``, 512) rather than the in-memory
    analysis ceiling — a smaller cap on the SAME owner, not a second
    implementation.

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


def _ceil_samples(delay_s: float, sample_rate: int) -> int:
    """First whole sample whose delay is at or above ``delay_s``."""
    return int(math.ceil(delay_s * sample_rate - WINDOW_EDGE_SNAP_SAMPLES))


def _floor_samples(delay_s: float, sample_rate: int) -> int:
    """Last whole sample whose delay is at or below ``delay_s``."""
    return int(math.floor(delay_s * sample_rate + WINDOW_EDGE_SNAP_SAMPLES))


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
    corroboration: float = 1.0,
    lower_peak_us: float = 0.0,
    lower_peak_ratio: float = 0.0,
    effective_floor_us: float = 0.0,
    earlier_arrival_us: float = 0.0,
    earlier_arrival_db: float = STRENGTH_FLOOR_DB,
    band_deficit_db: float = STRENGTH_FLOOR_DB,
) -> EchoDiagnostic:
    """A refused diagnostic: no delay reported, and a slug saying why.

    Every call site passes a non-empty ``REFUSAL_*`` slug — this helper is
    the "the detector declined" constructor, and only that. The other
    zero-confidence outcome, "the detector ran to completion and found
    nothing credible", carries ``refusal == ""`` and is built inline in
    :func:`detect_echo`.

    ``corroboration`` defaults to 1.0 because on most refusal paths the two
    estimators genuinely **could not be compared** — the detector gave up
    before both existed, or one of them fell outside the search window — and
    1.0 is this module's encoding of exactly that (see
    :attr:`EchoDiagnostic.corroboration`). The two late refusals —
    **edge-proximity** and **rahmonic** — are where that default would lie:
    both fire *after* the candidates have been compared, so both pass the
    measured value through instead. **Earlier-dominant-arrival** is later
    than either and still takes the default honestly: it fires only when the
    envelope's answer was out of window, so the pair genuinely was not
    comparable. Reporting 1.0 on either claimed
    "incomparable" about a pair that had in fact been compared, and the
    readings it overwrote run the full width of the scale — this module's
    tests produce late refusals carrying everything from near-perfect
    agreement (below 0.001) to gross disagreement (above 3). The value is
    passed through to preserve the measurement, **not** because a small
    value argues for the refusal: an edge refusal turns on distance to the
    window edge and a rahmonic refusal on ``lower_peak_ratio``, neither rule
    reads this field, and among edge refusals readings above
    ``CORROBORATION_LOOSE`` are ordinary rather than exceptional.

    The raw estimator fields are carried through when they exist, because a
    railed or edge-hugging cepstral estimate is exactly the evidence a
    reader needs to understand a ``no_in_window_echo`` or
    ``tau_at_window_lower_edge`` refusal. ``lower_peak_*`` is the same idea
    for ``rahmonic_of_lower_delay``, and is the *only* evidence that refusal
    has: the slug says a stronger peak sits below the candidate, and these
    two fields say where it sits and by how much it wins.
    ``earlier_arrival_*`` plays that role for
    ``earlier_dominant_arrival``, and ``effective_floor_us`` is passed on
    every refusal taken after the band is known — a reader asking "could
    this window have seen it?" is asking that *hardest* about a refusal.
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
    the upper edge**, and that *aliasing* asymmetry is physical: nothing
    aliases *down* onto the top of the window, and what lies beyond it is
    already refused by the plain rejection contract.

    **The rahmonic screen — the third rule, and the one that reaches where
    the edge margin cannot.** The edge rule closes aliasing; it does not
    close the other way a below-window echo reaches an in-window verdict. A
    comb's cepstrum repeats at 2*tau, 3*tau, ..., so a window that excludes
    the true delay can still contain a *rahmonic* of it — anywhere in the
    window, not just at an edge, which is why no edge margin can catch it.
    Up to ``search_us[0] <= 600`` the margin happened to refuse them anyway;
    from ``search_us[0] >= 650`` it did not, and the detector reported a
    confident, in-window, roughly-3x-wrong delay. Measured on a 10-position
    cloud of true delays spanning 150-400 us: (600, 1000) refused
    everything, while (650, 1000), (700, 1000) and (800, 1200) each admitted
    2-3 estimates at confidence up to 1.000 — enough for
    :func:`assess_geometry` to return ``geometry_locked`` at a median tau of
    814 / 857 / 897 us. The admitted estimates' cepstral peaks sat at these
    ratios to their position's true delay, **per window**; the spread is not
    uniform, so it is not one range::

        (650, 1000): 2.992, 3.001, 3.648
        (700, 1000): 2.992, 3.001
        (800, 1200): 2.985, 2.994

    A surviving candidate is now refused with ``rahmonic_of_lower_delay``
    when the strongest detrended-cepstrum peak below it — over the region
    from ``RAHMONIC_FLOOR_STEPS`` quefrency steps up to the candidate's own
    bin, deliberately including quefrencies the caller's window excluded —
    beats it by more than ``RAHMONIC_MARGIN``. A rahmonic is always weaker
    than the fundamental that produced it, so that is the signature, and it
    assumes nothing about the ratio being an integer. On the three windows
    above it rejects every previously-admitted estimate by 21.5-78.8x, and
    all three now return not-locked with no usable estimates. The refusal
    carries its own evidence: :attr:`EchoDiagnostic.lower_peak_us` is where
    the stronger peak sits — within half a quefrency step of the position's
    true delay in every one of those cases — and
    :attr:`EchoDiagnostic.lower_peak_ratio` is by how much it wins.

    The 3.648 outlier is why the screen asks "is anything below stronger?"
    rather than re-testing a survivor against tau/2 and tau/3 (as an earlier
    revision of this docstring prescribed). That case — true delay 205.6 us,
    cepstral peak 749.8 us, reported tau 814.4 us — was the module's worst
    measured case; its reported tau is 3.96x truth and its cepstral peak
    3.65x, so there is no submultiple to find. The closest the prescribed
    test could come is tau/3 of the reported value, 271.5 us, 32% and 0.92
    quefrency steps off the truth — catchable only by a tolerance so wide it
    stops discriminating. The rule that shipped locates the fundamental
    itself, at 214.2 us (4.2%, 0.12 steps), and refuses at 78.8x.

    What the screen is **not** is a licence to raise the window freely: it
    is calibrated to sit in a measured gap (``RAHMONIC_MARGIN``), the
    default window still has by far the longest false-lock record, and a
    raised window is now screened rather than validated. Screening also has
    a cost only a raised window can pay. "Something much stronger sits
    below" is *necessary* for a candidate to be a rahmonic, not sufficient:
    an honest in-window echo under a stronger unrelated *earlier*
    reflection presents the same evidence and is refused as well. Measured
    on a committed two-echo grid, most raised-window cases refuse that
    way and none of the same geometries do at the default window (the
    counts, which move with ``RAHMONIC_MARGIN``, are quoted once — on
    ``DEFAULT_ECHO_SEARCH_US``), and the
    remedy is therefore to measure with the default window — which contains
    the earlier reflection rather than excluding it (see
    ``DEFAULT_ECHO_SEARCH_US`` for the figures and the test that re-derives
    them). It is a refusal, never a wrong number.
    Every one of these window claims rests on synthetic IRs plus a
    single-position three-frame corpus; real multi-bounce behaviour through
    a raised window awaits the plan's S0 capture session.

    **The signal-presence screen — the one gate that is about the caller's
    band rather than the caller's window.** Everything above assumes
    ``band_hz`` contains the driver's signal. When it does not, the crest
    gate still passes (a band-limited IR has a perfectly good direct
    arrival), and the two estimators go to work on filter stopband residue
    and quantisation noise. Measured on the 2026-07-25 electrical loopback
    of the live JTS3 graph: the woofer branch, read in the 5-19 kHz default
    band that its 2 kHz LR4 lowpass had already thrown away, returned
    ``tau = 323.3 us`` at ``confidence = 0.275`` with an empty refusal. So a
    caller that knows the driver's passband may declare it as
    ``signal_band_hz``, and a band sitting more than
    ``BAND_BELOW_PASSBAND_MARGIN_DB`` below it is refused with
    ``band_below_passband`` before either estimator runs. The declared
    passband comes from the caller because this module is product-blind: it
    has no driver contract to read and must never guess one. ``None`` — the
    default — leaves behaviour exactly as it was.

    **The earlier-dominant-arrival disclosure.** A record that reaches zero
    confidence honestly ("ran, found nothing credible", empty refusal) is a
    distinct and deliberate outcome, and it stays. But S0's ground-plane
    captures reached it for a nameable reason: an improvised ground plane
    left the mic capsule proud of the floor, adding a dominant arrival at
    125-146 us — below the leg-B protocol window — which became the
    envelope's answer, was rejected as out-of-window, and forced the
    incomparable marker, so the cepstrum's honest ~320 us reading was never
    compared to anything.

    **Three conditions, all of this record**: the envelope's own answer
    lands below ``search_us``; a genuine local maximum is measured down
    there for the refusal to name; and that arrival is louder than
    ``EARLIER_ARRIVAL_DOMINANCE_DB`` re the direct arrival, which is what
    earns it the word *dominant*. Then the zero is replaced by an
    ``earlier_dominant_arrival`` refusal carrying that arrival's delay and
    level. It is strictly a fallback — it returns after every other refusal,
    and a record that scores above zero is reported normally however loud
    the excluded arrival is.

    **There is a band where an interloper takes the answer and is still not
    named, and that is deliberate.** The third condition is a threshold, so
    between it and the level at which an arrival stops being able to steal
    the envelope's answer at all, a record falls back to the silent
    empty-refusal outcome. Measured on one synthetic geometry — an
    interloper at 145 us of varying strength against a real 320 us echo at
    r=0.3, searched (150, 1000) us in the default band — that band is about
    **0.7 dB wide**: r=0.34 reads -9.57 dB and refuses, r=0.32 and r=0.30
    read -10.11 and -10.69 dB and fall back, and by r=0.28 (-11.30 dB) the
    interloper no longer wins the envelope at all. The record is not silent
    about it either way: ``earlier_arrival_us`` and ``earlier_arrival_db``
    disclose the interloper on the fallen-back record too. One geometry is
    all that has been measured, so read the width as an order of magnitude
    rather than a boundary.

    The IR is windowed internally to the early-arrival region (see
    ``ECHO_WINDOW_SPAN_FACTOR``), so a full deconvolved IR may be passed;
    room decay is excluded rather than diluting the statistic.

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
        up to ~800 us (≈ 27 cm), below the room's first wall reflection.

        **The default window is still the one with a swept false-lock
        record.** Raising the lower edge to 650 us or above used to enter
        the rahmonic regime described above, where a below-window echo was
        reported confidently at ~3x its true delay; the rahmonic screen
        refuses those now, by a 21.5-78.8x margin on the estimates that
        used to be admitted. That makes a raised window *screened*, not
        *validated*, and screening it costs honest late echoes whenever a
        stronger reflection sits below the window — prefer the default.

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
          120 us. That number is on every record as
          :attr:`EchoDiagnostic.effective_floor_us`, so a consumer can
          disclose it rather than re-derive it — the S0 ground plane's
          125-146 us arrivals sit under it, which is why the plan surfaces
          the floor instead of raising the window. The estimators' own
          near-floor bias stretches that a
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
    # The edge-margin dead zone — **one derivation, two consumers**, which
    # is the point of computing it here rather than at either use site. It
    # sets the delay this window cannot report below (disclosed on every
    # record as ``effective_floor_us``) *and* it is the boundary the
    # ``tau_at_window_lower_edge`` check applies further down, which reads
    # ``edge_margin_s`` off this same value. Two independent derivations of
    # one decision boundary would agree only by coincidence, and a consumer
    # that disclosed the floor while the detector applied something else is
    # exactly the drift this module's SSOT rules exist to prevent. The
    # agreement is behavioural, not asserted: see
    # test_disclosed_floor_is_the_boundary_the_edge_check_applies.
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
    # Both levels come off ``spectrum`` — the early-arrival segment's own
    # transform, already computed above — so the screen reads exactly the
    # data the estimators are about to read and costs no extra FFT. Means
    # are taken in **linear power**, this module's estimator everywhere
    # else, so the statistic composes with the rest rather than introducing
    # a second convention. Placed after the band-coverage refusals (a band
    # too narrow to analyse is a different complaint, and answering it first
    # keeps each refusal about one thing) and before the cepstrum, so a
    # stopband-residue signal never reaches an estimator that could dress it
    # up as a delay. See ``BAND_BELOW_PASSBAND_MARGIN_DB``.
    band_deficit_db = STRENGTH_FLOOR_DB
    if signal_band is not None:
        signal_mask = (freqs >= signal_band[0]) & (freqs <= signal_band[1])
        # A declared passband narrower than one FFT bin measures nothing;
        # with n_fft >= 4096 that is a sub-12 Hz passband at 48 kHz, so this
        # is a degenerate-input guard rather than a regime. Leaving the
        # deficit unmeasured is the fail-open choice on purpose: refusing on
        # a band the detector could not evaluate would be a verdict about
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
    # itself so that *every* record downstream of this point carries it —
    # including the refusals taken before the screen runs, whose readers are
    # asking the same question ("is this candidate the real fundamental?").
    # The region deliberately extends below ``search_lo_s``: the whole point
    # is to see the echo the caller's window excluded.
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
    #
    # It was ``round()`` at both edges until #1750. Rounding the lower edge
    # *down* whenever ``frac(search_lo * rate) < 0.5`` searched up to half a
    # sample below the window the caller asked for — at (150, 1000) us and
    # 48 kHz, from 145.83 us — and that is not a rounding nicety: S0's
    # ground_plane_01/02 have a proud-capsule arrival at exactly 145.8 us,
    # which the leaked half-sample admitted as an in-window candidate while
    # the scan below simultaneously reported it as below-window.
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
    # pulse's own decay". Measured 2026-07-25 at the default band and
    # window: 0 of the 60 impulse-with-no-echo negative controls behind
    # ``ECHO_CONFIDENCE_FLOOR`` has one, while all three S0 ground-plane
    # positions do (both pinned in tests/test_spatial_combine.py). That is
    # what keeps the refusal below off the found-nothing population.
    #
    # The scan stops at ``first`` — the same value the envelope's candidate
    # range starts at, not a second derivation of the boundary — so "below
    # the window" and "in the window" partition the samples with no overlap
    # and no gap. Until #1750 this bound was ``ceil`` while ``first`` was
    # ``round``, and the two disagreed by a sample at any window whose lower
    # edge is not sample-aligned.
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
    #
    # Corroboration is computed *above* this check rather than below it so
    # the refusal can carry what was actually measured. When both candidates
    # were in-window they really were compared, and the readings this path
    # produces run from near-perfect agreement to gross disagreement — so
    # agreement is common here but never characterizes the refusal, which
    # turns on distance to the window edge alone. Reporting the
    # incomparable-marker 1.0 here would have contradicted the record the
    # refusal exists to preserve.
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
    # documented false-lock windows the *previously-admitted* estimates are
    # refused at 21.5-78.8x the candidate, with that stronger peak landing
    # within half a quefrency step of the position's true delay. (The full
    # rahmonic-refusal population on those same three windows spans
    # 5.91-624.29; the narrower range is the subset that used to be admitted,
    # which is the set the fix is measured against. That floor was 5.14
    # before #1750 — the *ratios* are cepstral and did not move, but the
    # population did: the lowest-ratio members are now reached by the edge
    # rule first.) See ``RAHMONIC_MARGIN``
    # for the calibration and for why an exact tau/2, tau/3 submultiple
    # re-test would have missed the worst measured case.
    #
    # The signature is necessary but not sufficient, and this is where the
    # detector accepts a known cost: an honest in-window echo under a
    # stronger unrelated *earlier* reflection looks identical from one
    # record and is refused too. That is bounded to raised windows and fails
    # safe — see ``DEFAULT_ECHO_SEARCH_US`` for the sweep, the remedy, and
    # the pinning test. The refusal carries the evidence rather than
    # asserting it: ``lower_peak_us`` says where the stronger peak is,
    # ``lower_peak_ratio`` says by how much it wins.
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
        # "Ran, found nothing credible" is a real and deliberate outcome —
        # empty refusal, zero confidence — and it stays. But it is the wrong
        # answer in one specific, nameable state: **the envelope's own
        # answer landed below the caller's window.** That answer is then
        # rejected by the window contract, corroboration is forced to the
        # incomparable marker, and the score is zero *by construction* —
        # the cepstrum's reading never got compared to anything. Nothing was
        # weighed and found wanting; a comparison simply did not happen, and
        # saying "found nothing credible" hides that.
        #
        # This is the S0 ground plane exactly. An improvised ground plane
        # left the mic capsule centimetres proud of the floor, adding an
        # arrival at 125-146 us — below the leg-B (150, 1000) us protocol
        # window — at -0.64 to -2.57 dB re direct (r = 0.74-0.93). The
        # envelope answered with that interloper or its skirt (137.5 /
        # 139.4 / 135.4 us), the window rejected it, and all three positions
        # reported zero with an empty refusal while the cepstrum was looking
        # straight at the real ~320 us comb (327.2 / 270.2 / 342.8 us). A
        # household told "nothing found" learns nothing; one told "the
        # loudest thing here arrives at 146 us, below the window you
        # searched" knows to widen it.
        #
        # **Which refusal names those three records now depends on the
        # window's alignment, and #1750 is why.** The envelope can no longer
        # *select* a sample below ``search_lo_s``; it reaches below only
        # through parabolic refinement, i.e. by at most half a sample from
        # ``first``. At the protocol window's 7.2-sample lower edge that is
        # not enough, so those three envelope answers now land at 156.25 us,
        # inside the window and inside the edge margin, and
        # ``tau_at_window_lower_edge`` returns first. At a *sample-aligned*
        # lower edge (166.6667 us = 8 samples at 48 kHz) it is enough, and
        # all three are byte-identical to the pre-#1750 detector, this
        # refusal included. Both outcomes are refusals, both carry
        # ``earlier_arrival_us`` / ``earlier_arrival_db`` unchanged
        # (145.83 / 145.83 / 125.0 us at -2.57 / -2.01 / -0.64 dB), and
        # neither reports a delay — so what the household is told about the
        # interloper survives; only the slug that leads with it moves. Both
        # halves are pinned on the real captures in section F of
        # tests/test_spatial_combine.py.
        #
        # **Three conditions, all properties of this record.** The
        # envelope's answer below ``search_lo_s``; a genuine arrival
        # measured down there for the refusal to name; and that arrival
        # loud enough to deserve the word *dominant*
        # (``EARLIER_ARRIVAL_DOMINANCE_DB``). The third is not decoration:
        # without it the band-limited envelope's own ringing qualifies, and
        # at raised windows it flipped 6 of 660 echo-free readings into a
        # refusal naming the detector's skirt as an arrival (21 before
        # #1750 — the count and the single figure live on that constant, and
        # an earlier revision of this line quoted 22 against the constant's
        # own 21, which is the drift a second copy always eventually is).
        # Failing any condition falls through to the honest nothing-found
        # below, whose verdict is then byte-identical to the pre-S0
        # detector's.
        #
        # It cannot pre-empt any existing refusal — every one of them
        # returns earlier — and it cannot pre-empt a credible reading,
        # because a credible reading never reaches this branch.
        #
        # **Rejected alternative: recovering the measurement instead of
        # naming the loss.** The obvious richer fix is to let the envelope
        # skip the interloper and answer with the best *in-window* arrival,
        # which on S0's ground plane recovers a real ~320 us detection on
        # gp_03. It was implemented and measured, and it re-opens the
        # false-lock hazard this module spent four review rounds closing:
        # letting the envelope hunt past an excluded arrival manufactures
        # agreement with the cepstral **rahmonic** of that same arrival. On
        # the committed ``RAHMONIC_MARGIN`` calibration sweep (the
        # ``JTS_RAHMONIC_CALIBRATION`` grids, windows from (400, 900) to
        # (1000, 1600) us), the wrong-reading population grew 409 -> 506
        # readings and its ``lower_peak_ratio`` floor fell **4.9192 ->
        # 2.7899**, under the 1.5x ``RAHMONIC_MARGIN`` wall that sweep
        # asserts — i.e. the screen's own headroom, not a test's opinion of
        # it. Those two figures are the pre-#1750 tree's; the recovery path
        # has not been re-measured against the unified boundary, and its
        # baseline has moved under it, so they are archaeology rather than a
        # current comparison. What HAS landed is the smaller half of that
        # measurement: the same sweep predicted "restricting the change to
        # the window-bounds rounding defect" at floor 2.7899 / population
        # 439, and #1750 did exactly that and re-derived exactly those
        # figures — the calibration was re-adjudicated with it, and
        # ``RAHMONIC_MARGIN`` moved 2.0 -> 1.65 to keep the wall. So the
        # detector still names what it lost rather than guessing better;
        # reviving the recovery path means re-running this sweep on the
        # current tree first, not reading the 506 above as still true.
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

    The three admission rules — measured, confident, resolvable — with the
    reasoning for each in :func:`assess_geometry`'s docstring, which is the
    one place they are explained. This function is where they are
    *implemented*: :func:`assess_geometry` calls it for the set it clusters,
    and :mod:`jasper.audio_measurement.interference_nulls` calls it for the
    arrival candidates it corroborates a null ladder against. Two consumers
    of one rule, so a second copy of the rule cannot drift from the first —
    the same argument :func:`merged_true_intervals` is shared under.

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
    ``tolerance`` (relative) of their median. A
    stable tau means a stable null ladder at ``(n+0.5)/tau``, which spatial
    averaging cannot fill however many positions are added — the honest
    consumer response is to ask the user to spread the mic further, or (the
    plan's S0 prediction 5) to conclude the bounce is speaker-fixed
    diffraction rather than a boundary, in which case the exclusion screen
    carries the weight instead.

    **What counts as usable evidence** — a diagnostic is admitted only when
    all three hold, because each excluded class produces a *false* lock in a
    different way. The rules are implemented once, in
    :func:`usable_echo_estimates`, and explained once, here:

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

    **This function is only as honest as the diagnostics it is handed.**
    The three rules screen *unusable* evidence; they cannot screen evidence
    that is usable-looking and wrong, and nothing here can be made to. The
    worked example is the rahmonic class: when the caller searched with
    ``search_us[0] >= 650``, :func:`detect_echo` used to report a cepstral
    rahmonic of a below-window echo at ~3x the true delay, confidently and
    in-window. Those estimates passed all three rules here and clustered
    with each other, so a genuinely dispersed cloud returned
    ``GEOMETRY_LOCKED``. The fix had to live in the detector, and does —
    ``RAHMONIC_MARGIN``'s screen refuses them at the source. Read that as
    the shape of the risk rather than as a closed file: any *future* way the
    detector produces a confident wrong number will reach this function the
    same way, and this function will cluster it the same way.

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
    # the verdict below — see :attr:`GeometryLock.thin_evidence`. It cannot
    # be True on the ``GEOMETRY_UNKNOWN`` return, because that return fires
    # exactly when ``n_confident < GEOMETRY_MIN_CONFIDENT``; it is passed
    # there anyway so the flag has one derivation rather than two.
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
    Config-shaped failures cannot: a
    malformed band or search window is rejected up front by
    :func:`combine_positions`, and a non-positive sample rate by
    ``_validate_capture``, both before any detection runs.

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
      signal_band_hz: declared passband handed to :func:`detect_echo`'s
        signal-presence screen, or ``None`` (default) to leave the screen
        off. Echoed back for the same reason as the two above. This module
        never derives it — the caller owns the driver contract (the wiring
        layer does so in plan PR-4); a pure combiner that guessed a passband
        would be product policy in a pure-DSP module.

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
    # Shape-check by *unpacking*, before any indexing. A 1-tuple used to
    # raise IndexError and a None TypeError — neither is the ValueError this
    # function documents — while a 3-tuple was accepted outright, silently
    # dropping the third element. Unpacking a two-item generator turns all
    # three into the documented ValueError at one coercion point, and the
    # coerced pair is what the rest of the function uses, so a list or a
    # numpy pair reaches :func:`detect_echo` and the result record as the
    # same plain tuple.
    #
    # ``signal_band_hz`` joins the same loop when it is supplied. ``None``
    # is not a malformed pair — it is the documented "no passband declared,
    # leave the screen off" value — so it is skipped rather than coerced.
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
