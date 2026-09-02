# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Impulse-response gating and the low-frequency validity floor.

A domestic room contaminates a far-field (reference-axis) capture with a
floor/wall/ceiling reflection a few milliseconds after the direct sound. Any
quantity derived from the deconvolved impulse response — magnitude, level,
polarity, delay — is only trustworthy above a low-frequency floor set by how
long the reflection-free window is: ``f_valid ~= 1 / window_seconds``. See
docs/active-crossover-information-design.md "Measurement validity: gating
and the low-frequency floor" (spec) and the P1a consult table (this module's
constants) for the full rationale.

This module does no I/O and holds no state:

* :func:`detect_first_reflection` finds the first strong reflection after
  the direct-arrival peak using an energy-envelope threshold with
  hysteresis (drop below the threshold, then rise back above it), and
  accepts a crossing only if it clears a prominence vote (see "The
  prominence vote" below).
* :func:`gate_impulse_response` windows an IR to its reflection-free span
  (rectangular head through the peak, half-Hann taper into the reflection)
  and returns the :data:`~jasper.active_speaker.driver_acoustics` SC-2
  gating-block fragment describing what it did.
* :func:`build_gate_window` is that window's SHAPE in isolation, for a
  caller that already knows the peak and the span — including a caller
  forcing a span this module would not have chosen (a gate ladder).
* :func:`exempt_gating_block` builds the SC-2 block for a capture that is
  exempt from gating (today's near-field driver capture, taken a few
  centimetres from the driver — too close for a room reflection to matter).
* :func:`f_valid_floor_hz` is the floor formula in isolation, for callers
  that already know the window length.
* :func:`f_trusted_floor_hz` is the *disclosed-beside-it* stricter floor
  (see "The gating contract" below). It sizes no window here; what reads it
  is :data:`TRUSTED_FLOOR_MULTIPLIER`'s note.
* :func:`f_entanglement_floor_hz` is the same multiplier over the room's
  first-bounce time rather than the window — the floor no window choice can
  lower (#3495). This module states the formula and the provenance words;
  :mod:`~jasper.audio_measurement.gate_disclosure` decides which source a
  given capture has.

Pipeline stage: **derive**. This module turns a deconvolved impulse
response into a windowed one plus the record of what it did. It owns
detection, the window, both floors, and the classification ledger. It
deliberately does NOT own the *proof* that the window helped — pricing the
gate against the band the DUT radiates needs program knowledge this module
does not have, and lives one stage out in
:mod:`~jasper.audio_measurement.gate_disclosure`.

Imported lazily by :mod:`jasper.active_speaker.driver_acoustics` (mirrors
that module's existing lazy-import discipline for the rest of the
measurement kernel) so the socket-activated ``/sound/`` wizard stays light
until a measurement actually runs. ``scipy`` is imported lazily *inside*
the two functions that need an analytic envelope, for the same reason.

The gating contract (R9, issue #1969)
-------------------------------------

The gate must be incapable of overstating what it measured. Every gating
block therefore carries, beside the window it chose:

``floor_source``
    WHY the window is what it is — and it is the same field the gating
    contract calls ``source_of_bound``. The contract brief's vocabulary
    (``detected|geometric|fallback``) and this module's
    (:data:`FLOOR_MEASURED` / :data:`FLOOR_SEARCH_BOUND` / ``None``) name
    the same fact, so only ONE of them is persisted — the one every
    existing consumer already reads. The banked E5 artifact
    (``captures/gating-experiments-20260731/analysis/gate_proof.json``)
    made the same reconciliation: it emits the key ``source_of_bound``
    carrying *this* module's values verbatim. There is deliberately no
    ``geometric`` value: JTS derives a bound from detection or from the
    search ceiling, never from assumed room geometry (owner's 2026-07-31
    abstraction ruling on #1966), and a vocabulary should not offer a
    value the code cannot produce.
``f_valid_floor_hz`` / ``f_trusted_hz``
    The nominal ``1/T`` floor and the stricter ``2.5/T`` floor, disclosed
    side by side. **The trusted floor changes nothing about the window or
    about what refuses** — migrating the refusal rule onto ``2.5/T`` is
    still a separate, ratified decision. What it does bound is
    :data:`TRUSTED_FLOOR_MULTIPLIER`'s note.
``internal_reflection_ledger``
    Candidate early features the gate found and *classified* rather than
    gated (see :func:`detect_first_reflection`).
``pre_post_gate_delta``
    What the gate did to the spectrum. Built one stage out by
    :mod:`~jasper.audio_measurement.gate_disclosure`, which is where the
    radiated-band knowledge lives, and merged into the block by the single
    caller that has both.

The sentence a household or an operator reads is
:func:`jasper.audio_measurement.gate_disclosure.describe_gate` — the only
place gating provenance becomes copy. A window length alone cannot
distinguish "a reflection was found at 4 ms and removed" from "nothing was
found; the window was capped at the 7 ms search ceiling"; those print
identically and mean opposite things, which is exactly the defect #1966
filed (the whole 2026-07-30 corpus sat at the ceiling while the record read
as "reflections removed").

The prominence vote (R9 WO-6, issue #1969)
------------------------------------------

A bare hysteresis crossing is a *confident* answer with no confidence
behind it, and measurement says that is where the detector actually fails:
on our own ESS chain, 18.1% of criteria-region positives fired EARLY —
typically at the search-window open — against 12.4% that found nothing
(``captures/detector-certification-20260801`` §WO-6.4's shipped-today row;
§R9.0 established the mechanism against the onset report, where the same
split reads 19.4 / 12.4). The product has met this in the field: the S0
gating incident (#1790) is one capture
"finding" a reflection 3 samples past the search-start offset and
collapsing its window to 1778 Hz.

So a crossing must now also *stand out*: its envelope peak must rise
:data:`REFLECTION_PROMINENCE_DB` above the envelope's own minimum between
the direct peak and the crossing. A candidate that fails does not end the
search — the scan resumes past that excursion and the next crossing is
voted on in turn. That resumption is what makes this a fix rather than a
trade: measured, it raises detection (P_D 0.674 -> 0.712) while cutting
early fires 31%, because the crossing a false early trigger was masking is
often the real reflection.

**This CHANGES gate decisions**, deliberately: it is the certified
early-fire mechanism fix R9 gated on measurement, and it is the first thing
to move a window since the module shipped. Per capture the direction is
one-way at a fixed threshold — a vote can only reject a candidate the old
scan accepted — but the scan's resumption means a capture can also end up
gating LATER rather than not at all.

**The operating point is bounded by hardware, not by the corpus.** Picked on
corpus statistics alone the vote wants ~13.5 dB, and 13.5 dB rejects this
speaker's one independently corroborated room reflection on 13 of 13 real
captures. :data:`REFLECTION_PROMINENCE_DB` carries that measurement and the
mechanism behind it; read it before moving the number.

Schema version 2 (was 1): ``first_reflection_ms`` now reports the
reflection's envelope PEAK rather than its onset, and three fields were
added. See :func:`detect_first_reflection` for the peak-reporting
rationale and :data:`GATING_SCHEMA_VERSION` for what a reader must do
with the difference.

The vote did NOT bump the schema, because this version tracks what a
persisted FIELD means and no field changed meaning. The cost of that
choice is real and is recorded rather than argued away: a pre-vote and a
post-vote block are indistinguishable in a bundle. `docs/gating-v2-plan.md`
D3 already owes the record a ``detector`` provenance field for exactly
this reason, and whether a behaviour change should also move the schema
version is that work order's decision to make; this change left the
version at 2 and said so in both places.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: Bumped 1 -> 2 by the R9 gating contract. Two things changed for a reader
#: of a persisted block: ``first_reflection_ms`` reports the reflection's
#: envelope PEAK where v1 reported its onset (a systematic +0.125 ms shift
#: on the measured path — see :func:`detect_first_reflection`), and
#: ``f_trusted_hz`` / ``reflection_onset_ms`` /
#: ``internal_reflection_ledger`` are new. A v1 block's
#: ``first_reflection_ms`` is a v2 ``reflection_onset_ms``; nothing else in
#: v1 changed meaning, and no code branches on this number — it exists so a
#: reader comparing records across the boundary can tell which convention a
#: ToA was written under.
GATING_SCHEMA_VERSION = 2

#: Schema at which ``first_reflection_ms`` began reporting the reflection's
#: ARRIVAL rather than its onset (schema 1 stored the onset). Pinned
#: independently of GATING_SCHEMA_VERSION on purpose: an unrelated future
#: bump to that version must not reinterpret an already-written v2 block.
ARRIVAL_REPORTED_SINCE_SCHEMA_VERSION = 2

WINDOW_KIND = "half_hann_tail"

# --- P1a consult table (see docs/active-crossover-information-design.md) ---
# K: how far below the direct peak's smoothed envelope the reflection must
# rise back above to count as "found". Tuned to reliably catch a domestic
# floor bounce (~-3..-10 dB at LF) while sitting just above a bandlimited
# driver's own first sinc sidelobe (~-13 dB), so driver ringing doesn't
# false-trigger.
#
# DO NOT RAISE K (issue #1983; measured, `captures/detector-certification-
# 20260801/` §WO-6, criteria frozen before the run). Raising K past the
# P_D maximum is STRICTLY DOMINATED — both error rates get worse together.
# Measured on our own ESS chain at the shipped vote (Q = 7.5 dB), over the
# complete frozen criteria region (12,750 positive / 6,000 negative):
#
#     K  (dB)    4      8     10     11   *12*    13     14     16     20
#     P_D     0.304  0.533  0.650  0.674  0.712  0.719  0.706  0.650  0.479
#     P_FA    0.008  0.096  0.162  0.210  0.268  0.304  0.331  0.417  0.492
#
# K = 13 buys 0.007 P_D for 0.036 more P_FA and is not worth it; above it,
# P_D falls while P_FA keeps climbing. (§4.2's original table is the same
# walk with the vote OFF, where the P_D maximum sat at K = 12 itself. The
# directive is unchanged by the vote.)
#
# K is ALSO bounded below by the hardware: at Q = 7.5 the real jts3 woofer
# reflection is found 13/13 at K = 12, but only 10/13 at K = 11 and 0/13 at
# K = 10 (§WO-6's `results-wo6-20260731/README.md`, "K is bounded from below
# by the same screen"). K = 12 is the FLOOR of the range that reproduces
# this speaker's established anatomy, not merely a corpus optimum — and it
# is the K whose reported delay matches §5's 1.2708 ms most exactly.
#
# (Earlier revisions of this comment advised "raise K, not lower it, if
# field corpora show misses". That is backwards above the shipped default
# and is corrected here.)
#
# The two failure directions are NOT symmetric, and the worse one is the one
# a more sensitive detector fails in:
#   * MISS  -> the window sits at the search ceiling and the record
#     over-claims low-frequency validity. Optimistic, and now disclosed
#     (``floor_source`` + :func:`describe_gate`).
#
#   * FALSE DETECT on a DUT-internal feature -> catastrophic. The kurtosis
#     challenger fired 13/13 on this speaker's horn feature at 646 us, which
#     would set the window to 0.646 ms: a 1548 Hz validity floor and a
#     3870 Hz trusted floor, destroying the entire evidence band around the
#     2 kHz crossover the tuner depends on (certification §5).
# Two guards encode exactly that: :data:`SEARCH_T_MIN_MS` classifies
# sub-minimum-gate features so they can never gate, and
# :data:`REFLECTION_PROMINENCE_DB` makes a crossing inside the span earn its
# window.
REFLECTION_THRESHOLD_DB = 12.0
# Q: how far a candidate's envelope peak must rise above the envelope's own
# minimum between the direct peak and the crossing, for that crossing to
# become a window bound. The early-fire mechanism fix (see the module
# docstring); a noise-floor or direct-tail excursion crosses the threshold
# without standing out, a real reflection does both.
#
# 7.5 dB is measured, not assumed: `captures/detector-certification-
# 20260801/` §WO-6, selected from a 315-cell (K, Q) grid on our own ESS chain
# over the complete frozen criteria region. Against the no-vote detector at
# the same K = 12 it is better on every measured axis, on two independent
# draws: P_D 0.674 -> 0.712, strict P_D 0.634 -> 0.673, P_FA 0.279 -> 0.268,
# and the early-fire rate it targets 0.181 -> 0.124 (-31%). Detection is not
# traded away to buy that — captures finding nothing move only 0.124 ->
# 0.130 — because a voted-down candidate does not end the search: the scan
# resumes and often finds the REAL reflection the early trigger was masking.
#
# DO NOT RAISE Q. This ceiling is set by hardware, not by the corpus, and
# the corpus alone points the wrong way — its best P_FA is at Q = 13.5, and
# 13.5 REJECTS this speaker's real 1.275 ms environmental reflection on
# 13/13 captures. Measured walk at the shipped K = 12, corpus beside the
# jts3 anatomy reproduction (§5's established 13/13 woofer / 0/13 tweeter):
#
#     Q  (dB)     0      3     6   *7.5*   9    10.5    12    13.5
#     P_D      0.674  0.678 0.693 0.712  0.727  0.724  0.702  0.667
#     P_FA     0.279  0.278 0.275 0.268  0.234  0.158  0.084  0.043
#     woofer   13/13  13/13 13/13 13/13  12/13   0/13   0/13   0/13
#
# 7.5 dB is the largest swept value that still reproduces the anatomy, and
# the margin is thin — the real reflection's own prominence is 9.25 dB
# median, 8.86 dB minimum across those 13 captures, so Q = 9 already loses
# one. Why the corpus disagrees, mechanically: a 150-2000 Hz branch has a
# ~500 us-wide direct impulse, so a 1.3 ms reflection sits on the direct
# arrival's own skirt and its envelope prominence is small. The corpus
# average runs 12-30 dB because it is dominated by longer delays and by
# narrow-direct-arrival templates; its own woofer-at-1.25 ms cell measures
# 9.1-11.0 dB and agrees with the hardware.
#
# Lower Q -> the early false detects come back (0.181 with no vote at all).
# Higher Q -> real reflections are rejected, their captures fall back to the
# 7 ms ceiling, and the record over-claims low-frequency validity. Read the
# asymmetry note above before moving this number in either direction.
REFLECTION_PROMINENCE_DB = 7.5
# Search span after the direct peak, in ms. t_min skips the direct arrival's
# own tail; t_max bounds the search (and is the fallback window when no
# reflection is found) and must stay >= a domestic floor-bounce arrival
# (~4-5 ms) so a present floor bounce is never truncated by the bound.
#
# t_min is ALSO the asymmetric-cost classification boundary: a candidate
# below it is DUT-internal by construction (a 0.5 ms path difference is
# 17 cm — horn/baffle scale, not room scale), it is recorded in the
# ``internal_reflection_ledger``, and it NEVER gates. See
# :data:`REFLECTION_THRESHOLD_DB`'s note on why a false detect there costs
# far more than a miss.
SEARCH_T_MIN_MS = 0.5
SEARCH_T_MAX_MS = 7.0
# Moving-RMS smoothing window for the detection envelope.
ENVELOPE_SMOOTH_MS = 0.20
# Fraction of the reflection-free span given to the half-Hann tail taper.
TAPER_FRACTION = 0.25
# Advisory (non-excluding) band above the floor: [floor, NEAR_FLOOR_RATIO *
# floor) marks a derived quantity "near_validity_floor" (reduced confidence)
# without excluding it. The hard exclusion (nothing below floor feeds a
# decision) is the load-bearing half; this ratio is a judgment call on top.
NEAR_FLOOR_RATIO = 1.25

# Stricter floor DISCLOSED beside the nominal 1/T one. 1/T is where a
# window has one full cycle of resolution; ~2.5/T is where a gated
# magnitude is actually trustworthy, and the E4 gate-stability sweep shows
# why the distinction is not academic: on jts3 the 1-4 kHz crossover-band
# magnitude MOVED 2.1 dB across 3/5/7/10 ms gates purely because 1-2 kHz
# sits below the trusted floor of the shorter ones, while everything above
# 2 kHz held to <=0.006 dB (captures/gating-experiments-20260731 §4).
#
# THIS NUMBER SIZES NO WINDOW AND NO REFUSAL RULE READS IT. It is reported
# so a reader can see how much of a band was merely "resolved" rather than
# trusted, and moving the gate's refusal rule onto it is still a separate,
# deliberate decision.
#
# It DOES bound a verdict, since #2551 (PR #2566): the flat spec's band
# clamps grade above it, reaching it through
# :func:`jasper.active_speaker.crossover_v2_flow.cloud_trusted_floor_hz`.
# That was the deliberate decision this comment used to reserve, taken for
# the spec and for nothing else — the mechanics live with the consumer, in
# :func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`, not here.
TRUSTED_FLOOR_MULTIPLIER = 2.5

# --- asymmetric-cost classification ledger ---------------------------------
# How far past the direct peak to enumerate candidate early features, the
# envelope prominence a candidate must clear, how far back the local
# minimum for that prominence is taken, and how many entries are kept
# (strongest first). Values are the banked E5 record's
# (captures/gating-experiments-20260731/kit/gate_proof.py), so a product
# ledger and that artifact enumerate the same features. The cap is what
# keeps a noisy IR from writing an unbounded list into every sidecar.
LEDGER_SPAN_MS = 1.6
LEDGER_PROMINENCE_DB = 6.0
LEDGER_PROMINENCE_LOOKBACK = 12
LEDGER_MAX_ENTRIES = 6
#: Classified, never gated: below :data:`SEARCH_T_MIN_MS`.
CLASS_DUT_INTERNAL = "DUT_internal_ungateable"
#: Inside the search span — the detector's own decision governs it.
CLASS_GATEABLE = "gateable"

# How far after the detected ONSET to look for the reflection's envelope
# peak. 0.5 ms is the certified variant's ``refine_ms``
# (captures/detector-certification-20260801/harness/detectors.py
# ``shipped_peak_refined``); reproducing its number requires reproducing
# this one.
#
# TWO CONSUMERS, one number, deliberately: this also bounds how far past a
# crossing :func:`_candidate_prominence_db` looks for the candidate's peak.
# The certified variant likewise uses one ``refine_ms`` for both, so
# splitting them would de-couple the product from §WO-6's measurement.
# Changing this for ToA reasons alone therefore moves the vote too.
TOA_REFINE_MS = 0.5

FLOOR_MEASURED = "measured_reflection"
FLOOR_SEARCH_BOUND = "search_span_bound"
# Entanglement-floor provenance (:func:`f_entanglement_floor_hz`), the
# vocabulary's single home (#3502). Measured is FLOOR_MEASURED's own word:
# the entanglement floor is measured exactly when the gate's bound was, off
# the same reflection. Unknown is NEVER read as clean: nothing was proven.
ENTANGLEMENT_SOURCE_MEASURED = FLOOR_MEASURED
ENTANGLEMENT_SOURCE_DECLARED = "declared_geometry"
ENTANGLEMENT_SOURCE_UNKNOWN = "unknown"
#: The closed set of provenance words. A value outside it is a caller bug,
#: not data: the field is a vocabulary, not free text.
ENTANGLEMENT_SOURCES = frozenset(
    {
        ENTANGLEMENT_SOURCE_MEASURED,
        ENTANGLEMENT_SOURCE_DECLARED,
        ENTANGLEMENT_SOURCE_UNKNOWN,
    }
)
NEAR_FIELD_EXEMPT = "near_field"


@dataclass(frozen=True)
class ReflectionDetection:
    """Result of searching an impulse response for its first strong reflection.

    ``floor_source`` is ``None`` when the IR is ungateable (silent/NaN
    capture, or too little room after the direct peak to search at all) —
    distinct from :data:`FLOOR_SEARCH_BOUND`, which means the search ran to
    its bound without finding a reflection (a real, reportable floor).

    ``reflection_idx`` is the detected ONSET and is what the gate windows
    to. ``reflection_peak_idx`` is the same reflection's envelope peak —
    the reported time of arrival — and is never used to size the window.
    ``internal_reflections`` is the classification ledger: early features
    the search enumerated but that are structurally un-gateable.
    """

    direct_peak_idx: int
    reflection_idx: int | None
    floor_source: str | None
    reflection_peak_idx: int | None = None
    internal_reflections: tuple[dict[str, Any], ...] = ()


def f_valid_floor_hz(window_s: float) -> float:
    """Low-frequency validity floor for a reflection-free window of ``window_s``.

    ``f_valid ~= 1 / window_s`` — a 4 ms window resolves nothing below
    250 Hz. A non-positive or non-finite window is not analyzable at any
    frequency, so this returns ``+inf``, which safely propagates to "no
    frequency clears the floor" in every downstream comparison.
    """
    if not (window_s > 0) or not math.isfinite(window_s):
        return float("inf")
    return 1.0 / window_s


def f_trusted_floor_hz(window_s: float) -> float:
    """The stricter floor DISCLOSED beside :func:`f_valid_floor_hz`.

    ``f_trusted ~= 2.5 / window_s``. Same ``+inf`` guard, same propagation.
    Nothing in the gate or the refusal rules reads this value; the flat
    spec's band clamps do — see :data:`TRUSTED_FLOOR_MULTIPLIER`.
    """
    return TRUSTED_FLOOR_MULTIPLIER * f_valid_floor_hz(window_s)


def f_entanglement_floor_hz(t_first_bounce_s: float) -> float:
    """The frequency below which speaker and room can no longer be separated.

    ``2.5 / t_first_bounce`` — the room's own floor, where
    :func:`f_trusted_floor_hz` is the gated window's (``2.5 / T``). Below it
    a window long enough to resolve the frequency already contains the
    reflection, so no gate length separates speaker from room.
    """
    if not math.isfinite(t_first_bounce_s) or not (t_first_bounce_s > 0):
        raise ValueError(
            f"t_first_bounce_s must be finite and positive (got {t_first_bounce_s!r})"
        )
    return TRUSTED_FLOOR_MULTIPLIER / t_first_bounce_s


def analytic_signal(x: np.ndarray) -> np.ndarray:
    """The complex analytic signal (scipy's Hilbert transform) of ``x``.

    ``scipy`` is imported here rather than at module scope so importing
    :mod:`gating` stays as cheap as it was when this module was pure numpy
    (the socket-activated wizard imports it on paths that never measure).
    :func:`analytic_envelope` is this function's magnitude; a caller that
    also needs the PHASE (e.g. an instantaneous-frequency read) uses this
    one directly instead.
    """
    from scipy.signal import hilbert

    return hilbert(np.asarray(x, dtype=np.float64))


def analytic_envelope(x: np.ndarray) -> np.ndarray:
    """Analytic-signal magnitude envelope (the ETC magnitude) of ``x``."""
    return np.abs(analytic_signal(x))


def _idx_to_ms(idx: int, sample_rate: float) -> float:
    """Sample index to milliseconds; guards the divide for a degenerate rate."""
    if sample_rate <= 0 or not math.isfinite(sample_rate):
        return 0.0
    return 1000.0 * idx / sample_rate


def _classification_ledger(
    envelope: np.ndarray,
    direct_peak_idx: int,
    sample_rate: float,
    *,
    t_min_ms: float,
) -> tuple[dict[str, Any], ...]:
    """Enumerate and CLASSIFY the early features after the direct peak.

    The asymmetric-cost guard's record (issue #1969, R9 deliverable 4). Every
    envelope local maximum in ``(peak, peak + LEDGER_SPAN_MS]`` that clears
    :data:`LEDGER_PROMINENCE_DB` above its own local minimum is listed, and
    each is classified :data:`CLASS_DUT_INTERNAL` when its delay is below
    ``t_min_ms`` (structurally un-gateable: it arrives before the search
    window opens, and at that scale it is the loudspeaker, not the room) or
    :data:`CLASS_GATEABLE` otherwise.

    **A ledger entry never gates.** Listing a feature is the opposite of
    acting on it: the whole point is that jts3's 271-292 us horn feature at
    -11.2 dB is real, is measured on every capture, and must NOT become a
    window bound — gating there would set the trusted floor to ~3870 Hz and
    destroy the 2 kHz crossover evidence band
    (``captures/detector-certification-20260801/`` §5).

    ``envelope`` is the UNGATED IR's analytic envelope, supplied by the
    caller so one transform serves both this and the ToA refinement.

    Units: ``tau_us`` microseconds after the direct peak; ``level_db`` and
    ``prominence_db`` decibels re the IR envelope's own maximum. Strongest
    first, capped at :data:`LEDGER_MAX_ENTRIES`.

    ``prominence_db`` IS the confidence evidence, and is deliberately a
    measured quantity rather than a fitted 0-1 probability: this detector
    has no calibrated likelihood model, so a probability here would be
    invented. Read it as "how far this feature stands above its own
    surroundings".

    **These are CANDIDATES, read against the capture's bandwidth.** An
    analytic envelope resolves structure only as finely as the signal's
    bandwidth allows, so a wideband capture lists more early candidates
    than a 150-2000 Hz one whose direct arrival is itself ~500 us wide.
    Taken to its limit this is visible in the test fixtures: an ideal
    delta has infinite bandwidth, and its own analytic-envelope sidelobes
    are enumerated here as ~-13 dB "features" with implausible prominences
    (the log floor under a numerically-perfect null is very deep). That is
    the ledger reporting truthfully about a physically unrealizable input,
    not a detection error — and it is why nothing downstream may treat a
    ledger entry as a confirmed reflection.
    """
    ref = float(envelope.max()) if envelope.size else 0.0
    if not math.isfinite(ref) or ref <= 0:
        return ()
    e = 20.0 * np.log10(np.maximum(envelope, ref * 1e-9) / ref)
    hi = min(e.size - 1, direct_peak_idx + int(round(LEDGER_SPAN_MS * 1e-3 * sample_rate)))
    entries: list[dict[str, Any]] = []
    for i in range(direct_peak_idx + 2, hi):
        if not (e[i] >= e[i - 1] and e[i] > e[i + 1]):
            continue
        local_min = float(
            e[max(direct_peak_idx + 1, i - LEDGER_PROMINENCE_LOOKBACK) : i].min()
        )
        prominence = float(e[i]) - local_min
        if prominence < LEDGER_PROMINENCE_DB:
            continue
        tau_ms = (i - direct_peak_idx) * 1000.0 / sample_rate
        entries.append({
            "tau_us": round(tau_ms * 1000.0, 1),
            "level_db": round(float(e[i]), 2),
            "prominence_db": round(prominence, 2),
            "classification": (
                CLASS_DUT_INTERNAL if tau_ms < t_min_ms else CLASS_GATEABLE
            ),
        })
    entries.sort(key=lambda r: r["level_db"], reverse=True)
    return tuple(entries[:LEDGER_MAX_ENTRIES])


def _refine_to_envelope_peak(
    envelope: np.ndarray,
    onset_idx: int,
    sample_rate: float,
    *,
    refine_ms: float = TOA_REFINE_MS,
) -> int:
    """Relocalise a detected ONSET to the reflection's envelope peak.

    Replicates the certified ``shipped_peak_refined`` variant
    (``captures/detector-certification-20260801/harness/detectors.py``): the
    analytic-envelope argmax within ``refine_ms`` after the onset. Measured
    there against exact synthetic ground truth, this moves the reported ToA
    from a systematic -0.125 ms onset bias to a 0.000 ms median error and
    lifts strict-tolerance detection 0.464 -> 0.609, with the detection
    decision byte-identical (§4.5).

    Owns the reported TIME only. It does not decide whether a reflection
    exists and it is never used to size the gate — the window still ends at
    the onset, which is the conservative choice (the onset precedes the
    peak, so the gate closes before the reflection's energy maximum). Falls
    back to ``onset_idx`` when there is no room to look.

    ``envelope`` is the UNGATED IR's analytic envelope, supplied by the
    caller (see :func:`_classification_ledger`).
    """
    n1 = min(
        envelope.size - 1,
        onset_idx + max(1, int(round(refine_ms * 1e-3 * sample_rate))),
    )
    if n1 <= onset_idx:
        return onset_idx
    return onset_idx + int(np.argmax(envelope[onset_idx : n1 + 1]))


def _candidate_prominence_db(
    envelope: np.ndarray,
    *,
    direct_peak_idx: int,
    crossing_idx: int,
    search_end_idx: int,
    refine_samples: int,
) -> float:
    """How far a threshold crossing stands above the valley it rose out of.

    The prominence vote's statistic, in decibels: the SMOOTHED envelope's
    maximum within ``refine_samples`` of the crossing, over that same
    envelope's minimum anywhere between the direct peak and the crossing.

    Reads the same smoothed RMS envelope the hysteresis search runs on —
    deliberately, not the analytic one: the vote's job is to price the
    detector's own decision surface, so a crossing and its prominence are
    computed from one signal. The certified variant
    (``captures/detector-certification-20260801/harness/ourchain/variants.py``
    ``shipped_family``) does exactly this, and reproducing its numbers
    requires reproducing that choice.

    Units: dB. Non-negative by construction — ``envelope[crossing_idx]``
    lies in both the top window and the valley window, so the maximum can
    never fall below the minimum. Owns the statistic only; the accept /
    reject decision and the scan belong to
    :func:`detect_first_reflection`.
    """
    hi = min(search_end_idx, crossing_idx + refine_samples)
    top = float(np.max(envelope[crossing_idx : hi + 1]))
    valley = envelope[direct_peak_idx + 1 : crossing_idx + 1]
    floor = (
        float(np.min(valley)) if valley.size else float(envelope[crossing_idx])
    )
    return 20.0 * np.log10(max(top, 1e-300) / max(floor, 1e-300))


def detect_first_reflection(
    ir: np.ndarray,
    sample_rate: int,
    *,
    direct_peak_idx: int | None = None,
    threshold_db: float = REFLECTION_THRESHOLD_DB,
    prominence_db: float = REFLECTION_PROMINENCE_DB,
    t_min_ms: float = SEARCH_T_MIN_MS,
    t_max_ms: float = SEARCH_T_MAX_MS,
    smooth_ms: float = ENVELOPE_SMOOTH_MS,
) -> ReflectionDetection:
    """Find the first strong reflection after the direct-arrival peak.

    Energy-envelope threshold with hysteresis, then a prominence vote. The
    smoothed envelope must first drop below ``peak - threshold_db`` (the end
    of the direct arrival's own tail) and then rise back above that same
    threshold (a candidate onset), searched only in
    ``[direct_peak + t_min_ms, direct_peak + t_max_ms]``. A candidate is
    accepted only if :func:`_candidate_prominence_db` clears
    ``prominence_db``; a rejected one does NOT end the search — the scan
    resumes past that above-threshold excursion and votes on the next
    crossing. ``prominence_db <= 0`` disables the vote, which recovers the
    pre-WO-6 first-crossing-wins behaviour exactly (that equivalence is
    what the certification harness asserts against this function).

    Bounded by construction: every iteration advances the cursor past a
    strictly longer prefix of the search span, so the scan runs at most
    ``t_max_ms`` samples' worth of iterations (336 at 48 kHz) and each does
    O(span) numpy work over that same span. It is a loop over CANDIDATES
    inside a bounded window, never over the IR.

    ``direct_peak_idx`` defaults to ``argmax(|ir|)``. Returns a
    :class:`ReflectionDetection` with ``floor_source=None`` (ungateable) for
    a silent/NaN capture or a search span with no room to search;
    :data:`FLOOR_SEARCH_BOUND` when the direct arrival never separates from
    the noise floor, or no crossing before the search bound survived the
    vote; :data:`FLOOR_MEASURED` when a reflection onset is accepted.

    Two things ride alongside that decision without changing it:
    ``reflection_peak_idx`` reports the reflection's envelope peak rather
    than its onset (:func:`_refine_to_envelope_peak`), and
    ``internal_reflections`` classifies the early features the search is
    structurally unable to gate (:func:`_classification_ledger`). Neither
    affects which captures gate or where a window ends.
    """
    ab = np.abs(np.asarray(ir, dtype=np.float64))
    n = ab.size
    if n == 0:
        return ReflectionDetection(0, None, None)

    sr = float(sample_rate)
    if sr <= 0 or not math.isfinite(sr):
        # Degenerate rate: nothing about ms-based windows is meaningful.
        p = int(direct_peak_idx) if direct_peak_idx is not None else int(np.argmax(ab))
        return ReflectionDetection(int(np.clip(p, 0, n - 1)), None, None)

    w = max(1, int(round(smooth_ms * 1e-3 * sr)))
    if w > 1:
        kernel = np.ones(w, dtype=np.float64) / w
        env = np.sqrt(np.convolve(ab**2, kernel, mode="same"))
    else:
        env = ab

    p = int(direct_peak_idx) if direct_peak_idx is not None else int(np.argmax(ab))
    p = int(np.clip(p, 0, n - 1))

    peak = float(env[p])
    if not math.isfinite(peak) or peak <= 0:
        return ReflectionDetection(p, None, None)

    thr = peak * (10.0 ** (-threshold_db / 20.0))
    t_min = max(1, int(round(t_min_ms * 1e-3 * sr)))
    t_max = int(round(t_max_ms * 1e-3 * sr))
    end = min(n - 1, p + t_max)
    if p + t_min >= end:
        # No usable room after the direct peak to search at all.
        return ReflectionDetection(p, None, None)

    # The analytic (ETC) envelope is a DIFFERENT quantity from the smoothed
    # RMS envelope the hysteresis search runs on, and is computed once here
    # for the two reporting products that need it. Neither reads `thr`,
    # `below`, or `after`, so neither can move a detection decision.
    analytic = analytic_envelope(ir)
    ledger = _classification_ledger(analytic, p, sr, t_min_ms=t_min_ms)

    seg = env[p + t_min : end + 1]
    seg_lo = p + t_min
    below = seg < thr
    refine_n = max(1, int(round(TOA_REFINE_MS * 1e-3 * sr)))
    bound = ReflectionDetection(
        p, None, FLOOR_SEARCH_BOUND, internal_reflections=ledger
    )

    cursor = 0
    while True:
        rest_below = below[cursor:]
        if not bool(np.any(rest_below)):
            # The direct arrival's tail never separates from threshold in
            # what is left of the span — report the span-bound floor.
            return bound
        first_below = cursor + int(np.argmax(rest_below))
        after = seg[first_below:] >= thr
        if not bool(np.any(after)):
            # Separated but nothing rose back above threshold before the bound.
            return bound
        crossing = first_below + int(np.argmax(after))
        reflection_idx = seg_lo + crossing

        # Extent of this contiguous at-or-above-threshold excursion; where
        # the scan resumes if the candidate is voted down. It is strictly
        # past `crossing` (whose sample is above threshold by definition),
        # which is what bounds the loop.
        run_end = crossing
        while run_end < seg.size and seg[run_end] >= thr:
            run_end += 1

        if prominence_db > 0.0 and _candidate_prominence_db(
            env,
            direct_peak_idx=p,
            crossing_idx=reflection_idx,
            search_end_idx=end,
            refine_samples=refine_n,
        ) < prominence_db:
            cursor = run_end
            continue

        return ReflectionDetection(
            p,
            reflection_idx,
            FLOOR_MEASURED,
            reflection_peak_idx=_refine_to_envelope_peak(
                analytic, reflection_idx, sr
            ),
            internal_reflections=ledger,
        )


def _fragment(
    *,
    direct_peak_ms: float,
    first_reflection_ms: float | None,
    reflection_onset_ms: float | None,
    window_ms: float | None,
    floor_hz: float | None,
    trusted_floor_hz: float | None,
    floor_source: str | None,
    internal_reflection_ledger: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """The SC-2 gating block's core fields (everything but ``applied`` /
    ``exempt_reason``, which the caller supplies — see
    :func:`gate_impulse_response` and :func:`exempt_gating_block`).

    THE single writer of a gating block's shape. Field meanings, all in
    milliseconds from the start of the analysed IR unless noted:

    ``first_reflection_ms``
        The reflection's time of ARRIVAL — its envelope peak (schema 2;
        schema 1 put the onset here).
    ``reflection_onset_ms``
        Where the detector's threshold crossing landed, which is where the
        window ENDS. ``window_ms == reflection_onset_ms - direct_peak_ms``
        on the measured path, by construction.
    ``f_valid_floor_hz`` / ``f_trusted_hz``
        ``1/T`` and ``2.5/T``. Only the first has ever sized a window; what
        the second bounds is :data:`TRUSTED_FLOOR_MULTIPLIER`'s note.
    ``internal_reflection_ledger``
        A list, possibly empty, never ``None`` — an empty ledger means "the
        search looked and found no prominent early feature", which is a
        different statement from "nothing looked".
    """
    return {
        "schema_version": GATING_SCHEMA_VERSION,
        "direct_peak_ms": direct_peak_ms,
        "first_reflection_ms": first_reflection_ms,
        "reflection_onset_ms": reflection_onset_ms,
        "window_ms": window_ms,
        "window": WINDOW_KIND,
        "f_valid_floor_hz": floor_hz,
        "f_trusted_hz": trusted_floor_hz,
        "floor_source": floor_source,
        "internal_reflection_ledger": [dict(e) for e in internal_reflection_ledger],
    }


def build_gate_window(
    n: int,
    *,
    peak_idx: int,
    span: int,
    taper_fraction: float = TAPER_FRACTION,
    lead: int | None = None,
) -> np.ndarray:
    """The gate's window shape alone: a float64 array of length ``n``.

    Unity from the head through ``peak_idx``, a flat plateau, then a
    half-Hann tail ``0.5 * (1 + cos(pi t))`` over the last
    ``taper_fraction`` of ``span`` samples, and zero past ``peak_idx +
    span``. This is the single owner of that shape — every window this
    module applies, and every window a forced-span caller builds, comes
    from here, so a ladder rung and a shipped gate cannot drift apart.

    ``lead`` bounds the head. ``None`` is the shipped path: rectangular all
    the way to index 0, which is correct when the array was already trimmed
    to a fixed pre-peak margin. An integer starts the window ``lead``
    samples before the peak with a raised-cosine fade into it, which is
    what a FORCED span needs — an unbounded head would add the whole
    pre-peak array to the window, so a 3 ms rung could not be built from an
    IR carrying a 5 ms lead, and a hard edge there would itself become a
    feature of the spectrum under test.

    Raises ``ValueError`` when the window would not fit inside ``n``:
    truncating it silently would publish a shorter window than the one the
    caller's numbers are stated against.
    """
    end = peak_idx + span
    if span <= 0 or peak_idx < 0 or end >= n:
        raise ValueError(
            f"gate window does not fit: n={n} peak_idx={peak_idx} span={span}"
        )
    win = np.zeros(n, dtype=np.float64)
    if lead is None:
        win[: peak_idx + 1] = 1.0
    else:
        start = max(0, peak_idx - lead)
        win[start : peak_idx + 1] = 1.0
        fade = peak_idx - start
        if fade:
            win[start:peak_idx] = np.hanning(fade * 2)[:fade]
    taper_len = max(1, int(round(taper_fraction * span)))
    flat_end = max(peak_idx, end - taper_len)
    win[peak_idx:flat_end] = 1.0
    tail_len = end - flat_end  # always > 0: taper_len >= 1 and span > 0
    idx = np.arange(flat_end, end + 1)
    t = (idx - flat_end) / tail_len
    win[flat_end : end + 1] = 0.5 * (1.0 + np.cos(np.pi * t))
    # win[end + 1:] stays 0 from initialization.
    return win


def gate_impulse_response(
    ir: np.ndarray,
    sample_rate: int,
    *,
    direct_peak_idx: int | None = None,
    taper_fraction: float = TAPER_FRACTION,
    threshold_db: float = REFLECTION_THRESHOLD_DB,
    prominence_db: float = REFLECTION_PROMINENCE_DB,
    t_min_ms: float = SEARCH_T_MIN_MS,
    t_max_ms: float = SEARCH_T_MAX_MS,
    smooth_ms: float = ENVELOPE_SMOOTH_MS,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Window an impulse response to its reflection-free span.

    Returns ``(gated_ir, fragment)``. ``gated_ir`` is the SAME length as
    ``ir``: 1.0 from the start through the direct peak, a flat plateau, a
    half-Hann taper down to 0 into the detected reflection (or the
    search-span bound when none is found), and 0 after. ``fragment`` is the
    SC-2 gating block MINUS ``applied``/``exempt_reason`` — the caller
    derives those with one rule: ``applied = fragment["floor_source"] is not
    None``, ``exempt_reason = None`` (this function is only called for
    reference-axis captures, which are never near-field-exempt).

    When the IR is ungateable (silent/NaN, or no room after the peak to
    search), the input is returned unchanged and every fragment field except
    ``direct_peak_ms`` and ``internal_reflection_ledger`` is ``None``.

    The window is sized from the detected ONSET (or the search bound), never
    from the reported time of arrival — see :func:`_refine_to_envelope_peak`.
    The fragment carries no ``pre_post_gate_delta``: pricing the gate needs
    the radiated band, which this stage does not know
    (:mod:`~jasper.audio_measurement.gate_disclosure`).
    """
    ir_arr = np.asarray(ir)
    n = ir_arr.shape[0] if ir_arr.ndim == 1 else 0
    sr = float(sample_rate)

    det = detect_first_reflection(
        ir_arr,
        sample_rate,
        direct_peak_idx=direct_peak_idx,
        threshold_db=threshold_db,
        prominence_db=prominence_db,
        t_min_ms=t_min_ms,
        t_max_ms=t_max_ms,
        smooth_ms=smooth_ms,
    )
    direct_peak_ms = _idx_to_ms(det.direct_peak_idx, sr)
    ungated_fragment = _fragment(
        direct_peak_ms=direct_peak_ms,
        first_reflection_ms=None,
        reflection_onset_ms=None,
        window_ms=None,
        floor_hz=None,
        trusted_floor_hz=None,
        floor_source=None,
        internal_reflection_ledger=det.internal_reflections,
    )

    if det.floor_source is None:
        logger.debug(
            "gating: ungateable IR (n=%d direct_peak_idx=%d) — no reflection-free "
            "span could be searched; leaving ungated",
            n, det.direct_peak_idx,
        )
        return np.asarray(ir_arr, dtype=np.float32), ungated_fragment

    p = det.direct_peak_idx
    if det.floor_source == FLOOR_MEASURED:
        end = int(det.reflection_idx)  # type: ignore[arg-type]
    else:
        t_max_samples = int(round(t_max_ms * 1e-3 * sr)) if sr > 0 else 0
        end = min(max(n - 1, 0), p + t_max_samples)

    span = end - p
    if span <= 0:
        # Defensive: detect_first_reflection's own p+t_min>=end guard should
        # already prevent this, but a caller-supplied direct_peak_idx near
        # the array end could in principle land past the search bound.
        logger.debug(
            "gating: non-positive reflection-free span (p=%d end=%d) — "
            "leaving ungated", p, end,
        )
        return np.asarray(ir_arr, dtype=np.float32), ungated_fragment

    window_ms = 1000.0 * span / sr
    floor_hz = f_valid_floor_hz(span / sr)
    trusted_floor_hz = f_trusted_floor_hz(span / sr)
    # `end` is the ONSET on the measured path (and the search bound
    # otherwise), so it stays the window's end and `reflection_onset_ms`.
    # The ARRIVAL reported as `first_reflection_ms` is the envelope peak,
    # which the detector already located and which sizes nothing.
    if det.floor_source == FLOOR_MEASURED:
        reflection_onset_ms: float | None = _idx_to_ms(end, sr)
        peak_idx = det.reflection_peak_idx
        first_reflection_ms: float | None = _idx_to_ms(
            peak_idx if peak_idx is not None else end, sr
        )
    else:
        reflection_onset_ms = None
        first_reflection_ms = None

    win = build_gate_window(n, peak_idx=p, span=span, taper_fraction=taper_fraction)

    gated = (ir_arr.astype(np.float64) * win).astype(np.float32)
    fragment = _fragment(
        direct_peak_ms=direct_peak_ms,
        first_reflection_ms=first_reflection_ms,
        reflection_onset_ms=reflection_onset_ms,
        window_ms=window_ms,
        floor_hz=floor_hz,
        trusted_floor_hz=trusted_floor_hz,
        floor_source=det.floor_source,
        internal_reflection_ledger=det.internal_reflections,
    )
    return gated, fragment


def apply_gate_fragment(
    ir: np.ndarray,
    sample_rate: int,
    fragment: dict[str, Any],
    *,
    taper_fraction: float = TAPER_FRACTION,
) -> np.ndarray:
    """Apply a signal-derived gate fragment to another equal-length IR.

    This is the paired-noise seam: reflection detection runs on the signal
    exactly once through :func:`gate_impulse_response`; the resulting integer
    peak/span (round-tripped through the fragment's millisecond fields) builds
    the same half-Hann operator for noise.  The noise IR is never inspected to
    choose a peak, reflection, or window.
    """

    ir_arr = np.asarray(ir)
    if ir_arr.ndim != 1:
        raise ValueError("paired gate input must be 1-D")
    if fragment.get("floor_source") is None:
        return np.asarray(ir_arr, dtype=np.float32)
    sr = float(sample_rate)
    direct_ms = fragment.get("direct_peak_ms")
    window_ms = fragment.get("window_ms")
    if not (
        sr > 0
        and isinstance(direct_ms, (int, float))
        and isinstance(window_ms, (int, float))
    ):
        raise ValueError("signal gate fragment is incomplete")
    p = int(round(float(direct_ms) * sr / 1000.0))
    span = int(round(float(window_ms) * sr / 1000.0))
    end = p + span
    if not (0 <= p < end < len(ir_arr)):
        raise ValueError("signal gate fragment is outside the paired IR")
    win = build_gate_window(
        len(ir_arr), peak_idx=p, span=span, taper_fraction=taper_fraction
    )
    return (ir_arr.astype(np.float64) * win).astype(np.float32)


def exempt_gating_block(
    ir: np.ndarray,
    sample_rate: int,
    *,
    reason: str = NEAR_FIELD_EXEMPT,
) -> dict[str, Any]:
    """Full SC-2 gating block for a capture that is exempt from gating.

    A near-field capture (today's shipped driver measurement, taken a few
    centimetres from the driver) is too close for a room reflection to
    contaminate it, so it is never gated — the caller uses the ungated IR
    for magnitude, byte-identical to before this module existed. This still
    records ``direct_peak_ms`` (a benign IR-peak position from the deconv
    window offset, not a room measurement) so the SC-2 invariant — a gating
    block is persisted whenever an IR exists — holds uniformly.
    """
    ir_arr = np.asarray(ir)
    n = ir_arr.shape[0] if ir_arr.ndim == 1 else 0
    peak_idx = int(np.argmax(np.abs(ir_arr))) if n else 0
    fragment = _fragment(
        direct_peak_ms=_idx_to_ms(peak_idx, float(sample_rate)),
        first_reflection_ms=None,
        reflection_onset_ms=None,
        window_ms=None,
        floor_hz=None,
        trusted_floor_hz=None,
        floor_source=None,
    )
    # Spread rather than re-listed field by field so :func:`_fragment` stays
    # the single writer of the block's shape — a new disclosure field must
    # not be able to reach the gated path and silently miss this one.
    # ``schema_version`` is named first only to keep the emitted key ORDER
    # identical to this function's pre-contract form (same value; the spread
    # re-assigns it without moving it).
    return {
        "schema_version": fragment["schema_version"],
        "applied": False,
        "exempt_reason": reason,
        **fragment,
    }
