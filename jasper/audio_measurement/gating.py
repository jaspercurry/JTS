# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Impulse-response gating and the low-frequency validity floor.

A domestic room contaminates a far-field (reference-axis) capture with a
floor/wall/ceiling reflection a few milliseconds after the direct sound, so any
quantity derived from the deconvolved impulse response — magnitude, level,
polarity, delay — is trustworthy only above ``f_valid ~= 1 / window_seconds``.
See docs/active-crossover-information-design.md "Measurement validity: gating
and the low-frequency floor" and the P1a consult table below for the rationale.

Pipeline stage: **derive**. This module does no I/O, holds no state, and owns
detection, the window, both floors and the classification ledger. It
deliberately does NOT own the *proof* that the window helped — pricing the gate
against the band the DUT radiates needs program knowledge that lives one stage
out, in :mod:`~jasper.audio_measurement.gate_disclosure`.

Imported lazily by :mod:`jasper.active_speaker.driver_acoustics`, and ``scipy``
is imported lazily inside :func:`analytic_signal`, so the socket-activated
``/sound/`` wizard stays light until a measurement actually runs.

The gating contract (R9, issue #1969)
-------------------------------------

The gate must be incapable of overstating what it measured, so every gating
block carries, beside the window it chose:

``floor_source``
    WHY the window is what it is — the same field the gating contract calls
    ``source_of_bound``, persisted once under this module's vocabulary
    (:data:`FLOOR_MEASURED` / :data:`FLOOR_SEARCH_BOUND` / ``None``). There is
    deliberately no ``geometric`` value: JTS derives a bound from detection or
    from the search ceiling, never from assumed room geometry (owner ruling on
    #1966), and a vocabulary should not offer a value the code cannot produce.
``f_valid_floor_hz`` / ``f_trusted_hz``
    The nominal ``1/T`` floor and the stricter ``2.5/T`` floor, side by side.
    The trusted floor changes nothing about the window or about what refuses;
    what it does bound is :data:`TRUSTED_FLOOR_MULTIPLIER`'s note.
``internal_reflection_ledger``
    Candidate early features the gate found and *classified* rather than gated
    (see :func:`detect_first_reflection`).
``pre_post_gate_delta``
    What the gate did to the spectrum, built one stage out by
    :mod:`~jasper.audio_measurement.gate_disclosure` and merged in by the
    single caller that has both.

A window length alone cannot distinguish "a reflection was found at 4 ms and
removed" from "nothing was found; the window was capped at the 7 ms search
ceiling" — they print identically and mean opposite things, which is the defect
#1966 filed. The sentence a household or operator reads is
:func:`jasper.audio_measurement.gate_disclosure.describe_gate`, the only place
gating provenance becomes copy.

The prominence vote (R9 WO-6, issue #1969)
------------------------------------------

A bare hysteresis crossing is a *confident* answer with no confidence behind
it, and that is where the detector actually fails: on our own ESS chain 18.1 %
of criteria-region positives fired EARLY — typically at the search-window open
— against 12.4 % that found nothing. So a crossing must also *stand out*: its
envelope peak must rise :data:`REFLECTION_PROMINENCE_DB` above the envelope's
own minimum between the direct peak and the crossing. A candidate that fails
does not end the search — the scan resumes past that excursion and votes on the
next crossing, which is what makes this a fix rather than a trade (P_D
0.674 -> 0.712 while early fires fall 31 %, because the crossing a false early
trigger was masking is often the real reflection).

**This CHANGES gate decisions**, deliberately, and **its operating point is
bounded by hardware, not by the corpus**: on corpus statistics alone the vote
wants ~13.5 dB, which rejects this speaker's one corroborated room reflection
on 13 of 13 real captures. Read :data:`REFLECTION_PROMINENCE_DB` before moving
the number.

Schema version 2 (was 1): ``first_reflection_ms`` reports the reflection's
envelope PEAK rather than its onset, and three fields were added — see
:data:`GATING_SCHEMA_VERSION` for what a reader must do with the difference.
The vote did not bump the version, because it tracks what a persisted FIELD
means and no field changed meaning; the cost, recorded rather than argued away,
is that a pre-vote and a post-vote block are indistinguishable in a bundle,
which `docs/gating-v2-plan.md` D3's ``detector`` provenance field owes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: What a reader of a persisted block must do with the difference: a v2
#: ``first_reflection_ms`` is the reflection's envelope PEAK where v1 was its
#: onset (a systematic +0.125 ms shift on the measured path), so a v1
#: ``first_reflection_ms`` is a v2 ``reflection_onset_ms``. ``f_trusted_hz`` /
#: ``reflection_onset_ms`` / ``internal_reflection_ledger`` are new. No code
#: branches on this number.
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

# Stricter floor DISCLOSED beside the nominal 1/T one. 1/T is where a window
# has one full cycle of resolution; ~2.5/T is where a gated magnitude is
# actually trustworthy. Measured: on jts3 the 1-4 kHz crossover-band magnitude
# MOVED 2.1 dB across 3/5/7/10 ms gates purely because 1-2 kHz sits below the
# trusted floor of the shorter ones, while everything above 2 kHz held to
# <=0.006 dB (captures/gating-experiments-20260731 §4).
#
# THIS NUMBER SIZES NO WINDOW AND NO REFUSAL RULE READS IT. It DOES bound one
# verdict (#2551): the flat spec's band clamps grade above it, reaching it
# through :func:`jasper.active_speaker.crossover_v2_flow.cloud_trusted_floor_hz`
# — for the spec and nothing else, with the mechanics at the consumer, in
# :func:`jasper.active_speaker.flat_spec.evaluate_flat_spec`.
TRUSTED_FLOOR_MULTIPLIER = 2.5

# --- asymmetric-cost classification ledger ---------------------------------
# How far past the direct peak to enumerate candidate early features, the
# envelope prominence a candidate must clear, how far back the local minimum
# for that prominence is taken, and how many entries are kept (strongest
# first). Values match captures/gating-experiments-20260731/kit/gate_proof.py,
# so a product ledger and that artifact enumerate the same features. The cap
# bounds what a noisy IR can write into a sidecar.
LEDGER_SPAN_MS = 1.6
LEDGER_PROMINENCE_DB = 6.0
LEDGER_PROMINENCE_LOOKBACK = 12
LEDGER_MAX_ENTRIES = 6
#: Classified, never gated: below :data:`SEARCH_T_MIN_MS`.
CLASS_DUT_INTERNAL = "DUT_internal_ungateable"
#: Inside the search span — the detector's own decision governs it.
CLASS_GATEABLE = "gateable"

# How far after the detected ONSET to look for the reflection's envelope peak.
# 0.5 ms is the certified variant's ``refine_ms``
# (captures/detector-certification-20260801/harness/detectors.py
# ``shipped_peak_refined``); reproducing its number requires reproducing this
# one. TWO CONSUMERS, one number, deliberately: it also bounds how far past a
# crossing :func:`_candidate_prominence_db` looks, exactly as the certified
# variant does, so changing this for ToA reasons alone moves the vote too.
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


def intersect_bands(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    """The band both of ``a`` and ``b`` cover, or ``None`` when they do not.

    Strict ``lo < hi``: a zero-width touch, and any non-finite edge, is ``None``.
    """
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return (lo, hi) if lo < hi else None


@dataclass(frozen=True)
class EntanglementFloor:
    """The room's floor and where it came from, as ONE value.

    The invariant lives HERE: a floor is known exactly when its source is not
    :data:`ENTANGLEMENT_SOURCE_UNKNOWN`, and a known floor is a finite,
    positive frequency. Constructing this type is the only place that rule is
    enforced. Strict by default; :meth:`coerce` is the ONE lenient door, for
    persisted data.
    """

    hz: float | None
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or self.source not in ENTANGLEMENT_SOURCES:
            raise ValueError(
                f"source must be one of {sorted(ENTANGLEMENT_SOURCES)} "
                f"(got {self.source!r})"
            )
        if (self.hz is None) != (self.source == ENTANGLEMENT_SOURCE_UNKNOWN):
            raise ValueError(
                f"floor and source disagree (got {self.hz!r} and {self.source!r}): "
                "a known floor names where it came from, and an unknown one "
                "carries no number"
            )
        if self.hz is not None:
            if isinstance(self.hz, bool):
                raise TypeError(f"floor must be a frequency, not a bool (got {self.hz!r})")
            object.__setattr__(self, "hz", float(self.hz))
        if self.hz is not None and not (math.isfinite(self.hz) and self.hz > 0.0):
            raise ValueError(f"floor must be finite and positive (got {self.hz!r})")

    @classmethod
    def unknown(cls) -> "EntanglementFloor":
        """Nothing measured and nothing declared — the ordinary state on this
        rig class (#3502), read as :data:`ENTANGLEMENT_SOURCE_UNKNOWN` says."""
        return cls(None, ENTANGLEMENT_SOURCE_UNKNOWN)

    @classmethod
    def from_bounce_s(cls, t_first_bounce_s: float, source: str) -> "EntanglementFloor":
        """:func:`f_entanglement_floor_hz` of ``t_first_bounce_s``, named.

        ``source`` says which bounce time this was and may not be
        :data:`ENTANGLEMENT_SOURCE_UNKNOWN`: a floor derived from a time is
        known by construction, so the unknown word would be a lie about the
        number beside it. A non-physical time raises, from the formula.
        """
        if source == ENTANGLEMENT_SOURCE_UNKNOWN:
            raise ValueError("a floor derived from a bounce time is never unknown")
        return cls(f_entanglement_floor_hz(t_first_bounce_s), source)

    @classmethod
    def coerce(cls, hz: Any, source: Any) -> "EntanglementFloor":
        """A PERSISTED pair, read leniently — anything inconsistent is unknown.

        A document is not a call site: it may have been written by a build
        without the field, and rehydrating a disagreement verbatim would raise
        the moment anything re-reads it. An unreadable word, a floor with no
        provenance, a provenance with no floor, and a non-physical number all
        become :meth:`unknown`. The only place that leniency lives.
        """
        try:
            return cls(None if hz is None else float(hz), source)
        except (TypeError, ValueError):
            return cls.unknown()


def analytic_signal(x: np.ndarray) -> np.ndarray:
    """The complex analytic signal (scipy's Hilbert transform) of ``x``.

    The module's only ``scipy`` import site, kept out of module scope so
    importing :mod:`gating` stays cheap for the socket-activated wizard, which
    imports it on paths that never measure. :func:`analytic_envelope` is this
    function's magnitude; a caller that also needs the PHASE uses this one.
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

    Every envelope local maximum in ``(peak, peak + LEDGER_SPAN_MS]`` clearing
    :data:`LEDGER_PROMINENCE_DB` above its own local minimum is listed, and
    classified :data:`CLASS_DUT_INTERNAL` when its delay is below ``t_min_ms``
    (structurally un-gateable — it arrives before the search window opens, and
    at that scale it is the loudspeaker, not the room) or
    :data:`CLASS_GATEABLE` otherwise.

    **A ledger entry never gates.** jts3's 271-292 us horn feature at -11.2 dB
    is real and is measured on every capture, and gating there would set the
    trusted floor to ~3870 Hz, destroying the 2 kHz crossover evidence band.

    ``envelope`` is the UNGATED IR's analytic envelope, supplied by the caller
    so one transform serves both this and the ToA refinement.

    Units: ``tau_us`` microseconds after the direct peak; ``level_db`` and
    ``prominence_db`` decibels re the IR envelope's own maximum. Strongest
    first, capped at :data:`LEDGER_MAX_ENTRIES`. ``prominence_db`` is a
    measured quantity, not a fitted 0-1 probability — this detector has no
    calibrated likelihood model — so read it as "how far this feature stands
    above its own surroundings".

    **These are CANDIDATES, read against the capture's bandwidth.** An analytic
    envelope resolves structure only as finely as the signal's bandwidth
    allows, so a wideband capture lists more early candidates than a
    150-2000 Hz one whose direct arrival is itself ~500 us wide — at the limit,
    an ideal delta's own envelope sidelobes are enumerated as ~-13 dB
    "features". Nothing downstream may treat a ledger entry as a confirmed
    reflection.
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

    The analytic-envelope argmax within ``refine_ms`` after the onset,
    replicating the certified ``shipped_peak_refined`` variant
    (``captures/detector-certification-20260801/harness/detectors.py``): a
    systematic -0.125 ms onset bias becomes a 0.000 ms median error against
    synthetic ground truth, with the detection decision byte-identical.

    Owns the reported TIME only: it never sizes the gate, whose window still
    ends at the onset — the conservative choice, since the onset precedes the
    peak. Falls back to ``onset_idx`` when there is no room to look.
    ``envelope`` is the UNGATED IR's analytic envelope, from the caller.
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

    The prominence vote's statistic, in dB: the SMOOTHED envelope's maximum
    within ``refine_samples`` of the crossing, over that same envelope's
    minimum anywhere between the direct peak and the crossing. The smoothed RMS
    envelope, deliberately, not the analytic one — the vote prices the
    detector's own decision surface, and the certified ``shipped_family``
    variant does the same, so reproducing its numbers requires this choice.

    Non-negative by construction: ``envelope[crossing_idx]`` lies in both
    windows. Owns the statistic only; the accept/reject decision and the scan
    belong to :func:`detect_first_reflection`.
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
    crossing. ``prominence_db <= 0`` disables the vote, recovering
    first-crossing-wins exactly (the equivalence the certification harness
    asserts against this function).

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

    ``reflection_peak_idx`` and ``internal_reflections`` ride alongside that
    decision and affect neither which captures gate nor where a window ends.
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
    ``exempt_reason``, which the caller supplies).

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

    Unity from the head through ``peak_idx``, a flat plateau, then a half-Hann
    tail ``0.5 * (1 + cos(pi t))`` over the last ``taper_fraction`` of ``span``
    samples, and zero past ``peak_idx + span``. The single owner of that shape,
    so a ladder rung and a shipped gate cannot drift apart.

    ``lead`` bounds the head. ``None`` is the shipped path: rectangular all the
    way to index 0, correct when the array was already trimmed to a fixed
    pre-peak margin. An integer starts the window ``lead`` samples before the
    peak with a raised-cosine fade into it, which a FORCED span needs — an
    unbounded head would add the whole pre-peak array to the window, and a hard
    edge there would itself become a feature of the spectrum under test.

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

    The paired-noise seam: detection runs on the signal exactly once, and the
    resulting integer peak/span (round-tripped through the fragment's
    millisecond fields) builds the same half-Hann operator for noise. The noise
    IR is never inspected to choose a peak, reflection, or window.
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

    A near-field capture (taken a few centimetres from the driver) is too close
    for a room reflection to contaminate it, so it is never gated and the
    caller uses the ungated IR for magnitude. ``direct_peak_ms`` is still
    recorded — a benign IR-peak position from the deconv window offset, not a
    room measurement — so the SC-2 invariant that a gating block is persisted
    whenever an IR exists holds uniformly.
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
    # the single writer of the block's shape. ``schema_version`` is named first
    # only to hold the emitted key ORDER; the spread re-assigns the same value.
    return {
        "schema_version": fragment["schema_version"],
        "applied": False,
        "exempt_reason": reason,
        **fragment,
    }
