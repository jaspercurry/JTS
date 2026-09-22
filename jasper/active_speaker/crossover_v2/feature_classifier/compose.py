# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Feature detection and classification verdicts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from scipy.signal import find_peaks

from jasper.audio_measurement.excess_phase import (
    FEATURE_HALF_OCT,
    NEIGHBOURHOOD_OCT,
)
from jasper.audio_measurement.quality_model import TrustLevel

from ..feature_classification import (
    DEFECT_BOOSTABLE,
    DEFECT_CUTTABLE,
    EGD_AMBIGUOUS,
    EGD_MIN_PHASE,
    EGD_NON_MIN_PHASE,
    GATE_MOVED,
    GATE_STABLE,
    INTERFERENCE_BARRED,
    ROOM,
    UNRESOLVED,
)

#: A feature must stand this many standard errors above the round's own
#: capture-to-capture scatter. Same z convention as :data:`Z_LOCAL_FLAT`: what
#: is being asked is whether the departure is a property of the speaker or of
#: which seat happened to be measured.
FEATURE_STABILITY_Z = 3.0

#: ...and this far from flat regardless. Below a tenth of a dB nothing
#: downstream can act on the departure: the per-driver seam's own narrowest
#: admissible filter is larger, so a "feature" this small can only produce a
#: refusal further down the line.
FEATURE_MIN_DEPARTURE_DB = 0.10

#: Two extrema closer together than half a feature band are one feature, not
#: two. Deliberately NOT
#: :data:`~.feature_classification.VERDICT_MATCH_TOLERANCE_OCTAVES`: the
#: 2026-08-19 record has peak/dip pairs 0.143 and 0.157 octaves apart,
#: inside that tolerance, and merging them would delete four genuine
#: features.
FEATURE_MIN_SEPARATION_OCT = FEATURE_HALF_OCT


#: Bounded work per round. The 2026-08-19 record found nine; a response that
#: offers more than this is reporting texture rather than features. Each
#: feature costs two full excess-group-delay runs in the C4 control pair,
#: so this is the instrument's CPU bound as well as its honesty bound.
MAX_FEATURES = 12


#: Fraction of a genuine same-frequency non-minimum-phase comb's excursion (the
#: C4 control, measured on this round's own capture) below which the feature is
#: called minimum-phase, and at or above which it is called non-minimum-phase.
#: C4 and its minimum-phase twin separate by one to two orders of magnitude, so
#: the gap between these two is wide and nothing lands in it by accident.
FRAC_NMP_MIN_PHASE = 0.25
FRAC_NMP_NON_MIN_PHASE = 0.50

#: How far above the feature's own +/-1/3-octave excess-GD scatter an excursion
#: must sit before it is a feature of the response rather than of the noise.
Z_LOCAL_FLAT = 3.0


def classifiable_band_hz(trusted_band_hz: tuple[float, float]) -> tuple[float, float]:
    """Where a verdict can be about the SPEAKER rather than about the band edge.

    A feature is read against its own +/-1/3-octave neighbourhood, so the
    neighbourhood has to fit inside the trusted band — otherwise
    :func:`egd_excursion` reports ``clean=False`` and the excursion is the
    edge of the measurement. This bounds detection AND an explicitly
    requested frequency.
    """
    lo, hi = trusted_band_hz
    return lo * 2**NEIGHBOURHOOD_OCT, hi * 2**-NEIGHBOURHOOD_OCT


def _detect_features(
    per_capture_detrended: np.ndarray,
    pooled_curve: np.ndarray,
    grid: np.ndarray,
    band_hz: tuple[float, float],
) -> list[float]:
    """Which frequencies this round has features at, from its own captures.

    Three things have to be true at once, and each rejects a different way of
    being wrong:

    * **Two-sided prominence in the RAW pooled curve.** A real peak falls
      away on BOTH sides and a real dip rises on both, where the shoulder of
      a large resonance does neither. Removing the broad tilt MANUFACTURES
      those shoulders (a 1-octave baseline under a +3 dB resonance leaves a
      −0.7 dB trough on each flank), so prominence is read before the
      detrend. ``scipy``'s topographic prominence, bounded to the
      neighbourhood width so a distant feature cannot supply the col.
    * **A trend-removed size of the same sign.** When the two disagree about
      which way the feature points, neither is trustworthy.
    * **Stability across the round's captures**, :data:`FEATURE_STABILITY_Z`
      standard errors above the capture-to-capture scatter: below that it is
      a property of where the microphone was.
    """

    pooled = per_capture_detrended.mean(axis=0)
    n = per_capture_detrended.shape[0]
    scatter = (
        per_capture_detrended.std(axis=0, ddof=1) / math.sqrt(n)
        if n > 1
        else np.zeros_like(pooled)
    )
    # The grid is geometric, so an octave is a constant number of samples.
    samples_per_octave = (grid.size - 1) / math.log2(grid[-1] / grid[0])
    wlen = max(3, int(round(2 * NEIGHBOURHOOD_OCT * samples_per_octave)))

    candidates: list[int] = []
    for sign, curve in ((+1.0, pooled_curve), (-1.0, -pooled_curve)):
        found, _ = find_peaks(
            curve, prominence=FEATURE_MIN_DEPARTURE_DB, wlen=wlen
        )
        candidates.extend(
            int(i) for i in found if np.sign(pooled[i]) == sign
        )

    magnitude = np.abs(pooled)
    admitted = [
        i
        for i in sorted(set(candidates))
        if band_hz[0] <= grid[i] <= band_hz[1]
        and magnitude[i] >= FEATURE_MIN_DEPARTURE_DB
        and magnitude[i] >= FEATURE_STABILITY_Z * scatter[i]
    ]
    if not admitted:
        return []

    kept: list[float] = []
    for index in sorted(admitted, key=lambda i: -magnitude[i]):
        fc = float(grid[index])
        if any(
            abs(math.log2(fc / other)) < FEATURE_MIN_SEPARATION_OCT for other in kept
        ):
            continue
        kept.append(fc)
        if len(kept) == MAX_FEATURES:
            break
    return sorted(kept)


def _compose(
    fc: float,
    egd: Mapping[str, Any],
    gate: Mapping[str, Any],
    control_pair: Mapping[str, Any],
    pooled_db: float,
    measured_q: float,
    *,
    controls_ok: bool,
    timing_available: bool,
) -> dict[str, Any]:
    """One feature's row: the two component verdicts, the composed one, and why."""
    excursion = egd["pooled_excursion_us"]
    nmp_scale = abs(control_pair["nmp_delta_us"])
    frac = abs(excursion) / nmp_scale if nmp_scale else float("nan")
    z_local = abs(excursion) / egd["nbhd_sd_us"] if egd["nbhd_sd_us"] else float("nan")

    if frac >= FRAC_NMP_NON_MIN_PHASE:
        raw_egd = EGD_NON_MIN_PHASE
    elif frac < FRAC_NMP_MIN_PHASE and z_local < Z_LOCAL_FLAT:
        raw_egd = EGD_MIN_PHASE
    else:
        raw_egd = EGD_AMBIGUOUS
    # An instrument that just failed its own known-answer check does not get to
    # assert a phase class. `egd_verdict_raw` keeps what the numbers said.
    egd_verdict = raw_egd if controls_ok else EGD_AMBIGUOUS

    gate_verdict = gate["gate_verdict"]
    is_dip = pooled_db < 0
    if egd_verdict == EGD_NON_MIN_PHASE:
        classification = INTERFERENCE_BARRED
    elif gate_verdict == GATE_MOVED:
        classification = ROOM
    elif egd_verdict == EGD_MIN_PHASE and gate_verdict == GATE_STABLE:
        # A defect verdict is what a filter is vouched by, so it needs BOTH
        # tests to have answered. A ladder that did not run is not a stable
        # one, and reading it as one would vouch for a filter aimed at a
        # feature nothing checked for the room.
        classification = DEFECT_BOOSTABLE if is_dip else DEFECT_CUTTABLE
    else:
        classification = UNRESOLVED

    # Confidence is deliberately NOT built on sign agreement across captures:
    # an excursion of essentially zero — the strongest possible minimum-phase
    # evidence — has random sign by construction, so a sign rule would punish
    # the cleanest results. Decisiveness is measured against the control scale.
    agree = gate_verdict == GATE_STABLE and egd_verdict in (
        EGD_MIN_PHASE,
        EGD_NON_MIN_PHASE,
    )
    decisive = (egd_verdict == EGD_MIN_PHASE and frac < 0.10 and z_local < 2.0) or (
        egd_verdict == EGD_NON_MIN_PHASE and frac > 0.75
    )
    resolved_gates = gate["resolved_gates"]
    # The three words are quality_model's shared TrustLevel — `medium` spelled in
    # full. An artifact banked before 2026-08-22 carries `med` and is
    # normalised on the way back in by
    # feature_classification.read_feature_verdicts.
    confidence: TrustLevel
    if classification == UNRESOLVED:
        confidence = "low"
    elif gate["tension"]:
        confidence = "medium"
    elif agree and decisive and resolved_gates >= 2 and timing_available:
        # A `high` reading is not earned while a corroborating test never ran.
        confidence = "high"
    elif agree:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "hz": fc,
        "classification": classification,
        "egd_verdict": egd_verdict,
        "egd_verdict_raw": raw_egd,
        "gate_verdict": gate_verdict,
        "confidence": confidence,
        "measured_q": measured_q,
        "depth_db": abs(pooled_db),
        "pooled_db": pooled_db,
        "is_dip": is_dip,
        "excursion_us": excursion,
        "excursion_sd_us": egd["sd_us"],
        "nbhd_sd_us": egd["nbhd_sd_us"],
        "p2p_us": egd["p2p_us"],
        "nmp_scale_us": nmp_scale,
        "frac_of_nmp": frac,
        "z_local": z_local,
        "lead_sensitivity_us": egd["lead_sensitivity_us"],
        "clean": egd["clean"],
        "resolved_gates": resolved_gates,
        "excess_loss_vs_null": gate["excess_loss_vs_null"],
        "gate_slack": gate["gate_slack"],
        "gate_notes": gate["gate_notes"],
        "controls_ok": controls_ok,
        "timing_corroborated": timing_available,
    }


def summary_lines(artifact: Mapping[str, Any]) -> list[str]:
    """One line per classified feature, under the round's controls disclosure.

    Here rather than in the CLI so the columns and the artifact they read stay
    in one module: an exit-0 round whose known-answer controls failed must not
    read as a clean one, which is what the first line says when there is one.
    """
    lines = []
    if artifact["controls_disclosure"] is not None:
        lines.append(f"  controls: {artifact['controls_disclosure']}")
    lines.extend(
        f"  {row['hz']:8.0f} Hz  {row['classification']:<34} "
        f"{row['confidence']:>6}  egd={row['egd_verdict']:<14} "
        f"gate={row['gate_verdict']:<7} depth={row['depth_db']:.2f} dB"
        for row in artifact["rows"]
    )
    return lines
