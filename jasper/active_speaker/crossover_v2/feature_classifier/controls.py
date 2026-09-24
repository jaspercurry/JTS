# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Known-answer controls for the excess-group-delay test."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy.signal import group_delay as _scipy_group_delay

from jasper.audio_measurement.excess_phase import (
    ExcessPhase,
    _apply,
    add_delayed_copy,
    biquad_allpass,
    egd_excursion,
    excess_group_delay,
    gate,
    injection_excess_gd,
    tau_for_first_null_ms,
)
from jasper.audio_measurement.gating import PHASE_GATE_LEAD_MS

from ..feature_optics import biquad_peaking

CONTROL_PEAKING_GAIN_DB = 2.0
CONTROL_PEAKING_Q = 2.0
CONTROL_ALLPASS_HZ: tuple[float, ...] = (1000.0, 4500.0)
CONTROL_ALLPASS_Q = 1.0
CONTROL_ECHO_GAIN = -0.5
CONTROL_ECHO_MS = 0.4
CONTROL_COMB_NMP_GAIN = 1.2
CONTROL_COMB_MP_GAIN = 0.8

#: A known MINIMUM-phase magnitude change must move excess group delay by less
#: than this. It is the instrument's false-positive scale, and the 2026-08-19
#: passing run read 0.5 us against it.
CONTROL_MAX_FALSE_POSITIVE_US = 2.0

#: The quiet delayed copy is also minimum phase and must also read flat, at the
#: looser bar its much larger magnitude ripple earns.
CONTROL_MAX_ECHO_FALSE_POSITIVE_US = 10.0

#: The all-pass must recover its own group delay to within this band, read
#: against the exact digital filter's group delay over the same reference band
#: rather than against the raw 4Q/w0 peak — a 2nd-order all-pass sits on a
#: plateau below f0, so the raw peak marks a correct instrument wrong.
CONTROL_ALLPASS_RATIO_BAND = (0.85, 1.15)

#: What a failed control suite costs, published at the top of the artifact.
#: All four controls calibrate the EGD scale, so a failure disables the PHASE
#: discriminator and leaves the magnitude ladder — whose verdict is the gate
#: sweep's, calibrated by its own null model — untouched.
CONTROLS_FAILED_DISCLOSURE = (
    "the known-answer controls did not pass on this round's own capture, so "
    "every row's egd_verdict is withheld as ambiguous (the raw reading is kept "
    "in egd_verdict_raw) and no row can classify as a defect; gate_verdict and "
    "every magnitude fact are unaffected, the controls calibrate the "
    "excess-group-delay scale alone"
)

#: A genuine cancellation must separate from its minimum-phase twin by this
#: multiple of the worst false positive above. Without it the two verdicts
#: would be a coin toss dressed as a threshold.
CONTROL_SEPARATION_MARGIN = 5.0


def _run_controls(
    ir: np.ndarray,
    sample_rate: int,
    features: Sequence[float],
    *,
    gate_ms: float,
    trusted_band_hz: tuple[float, float],
) -> dict[str, Any]:
    """Push known answers through the identical pipeline on a measured IR.

    A real IR rather than a synthetic impulse, so every control inherits the
    round's actual noise floor, gate truncation and room content.
    """

    # ONE transform per perturbed signal, then every feature's excursion read
    # off it. Recomputing the excess phase per (signal, feature) would multiply
    # the run by the feature count for no new information.
    def phase_of(signal: np.ndarray) -> ExcessPhase:
        return excess_group_delay(
            gate(signal, sample_rate, gate_ms=gate_ms, lead_ms=PHASE_GATE_LEAD_MS),
            sample_rate,
            trusted_band_hz=trusted_band_hz,
        )

    def excursions(ep: ExcessPhase) -> dict[str, dict[str, Any]]:
        return {
            f"{fc:.0f}": egd_excursion(ep, fc, trusted_band_hz) for fc in features
        }

    keys = [f"{fc:.0f}" for fc in features]
    base_ep = phase_of(ir)
    baseline = excursions(base_ep)

    # C1 — a known MINIMUM-phase magnitude change must not move excess GD. It
    # is placed on the round's own largest feature rather than at a lab
    # frequency, so the false-positive scale is measured where it matters.
    anchor = max(features, key=lambda fc: abs(baseline[f"{fc:.0f}"]["excursion_us"]))
    peaking = _apply(
        ir,
        biquad_peaking(anchor, CONTROL_PEAKING_GAIN_DB, CONTROL_PEAKING_Q, sample_rate),
    )
    c1 = excursions(phase_of(peaking))
    c1_delta = {
        key: c1[key]["excursion_us"] - baseline[key]["excursion_us"] for key in keys
    }

    # C2 — an all-pass, read as a difference curve referenced far from f0. The
    # excess-GD curve carries an arbitrary constant, so a raw before/after
    # read at f0 would measure the detrend rather than the filter. The
    # expectation is the exact digital filter's own group delay over the
    # identical band, NOT the raw 4Q/w0 peak: a 2nd-order all-pass sits on a
    # plateau below f0.

    c2: dict[str, Any] = {}
    for f0 in CONTROL_ALLPASS_HZ:
        coeffs = biquad_allpass(f0, CONTROL_ALLPASS_Q, sample_rate)
        ep = phase_of(_apply(ir, coeffs))
        difference = ep.excess_gd_us - np.interp(
            ep.freqs, base_ep.freqs, base_ep.excess_gd_us
        )
        far = (ep.freqs < f0 * 2**-2) | (ep.freqs > f0 * 2**2)
        offset = float(np.nanmedian(difference[far])) if far.any() else 0.0
        recovered = float(np.interp(f0, ep.freqs, difference) - offset)
        _, filter_gd = _scipy_group_delay(coeffs, w=2 * np.pi * ep.freqs / sample_rate)
        filter_gd_us = filter_gd / sample_rate * 1e6
        expected = float(
            np.interp(f0, ep.freqs, filter_gd_us)
            - (np.nanmedian(filter_gd_us[far]) if far.any() else 0.0)
        )
        c2[f"{f0:.0f}"] = {
            "q": CONTROL_ALLPASS_Q,
            "recovered_us": recovered,
            "expected_same_reference_us": expected,
            "ratio": float(recovered / expected) if expected else float("nan"),
            "detrend_offset_removed_us": offset,
        }

    # C3 — the research spec's own interference case: |gain| = 0.5 is MINIMUM
    # phase, so a correct pipeline must read it flat, and flagging it would
    # be a false positive. "Flat" is the known answer for the FILTER, not for
    # this chain's reading of it: the declared edge hold discards the comb's
    # DC null and the reconstruction error lands in band.
    # :func:`injection_excess_gd` derives that from the injection and the edge
    # constants alone, so it is removed rather than tolerated by a wider bar
    # (#3493). Subtracted from the CURVE, because ``excursion_us`` peak-picks
    # and so does not carry an offset additively.
    echo = add_delayed_copy(ir, CONTROL_ECHO_GAIN, CONTROL_ECHO_MS, sample_rate)
    c3_bias = injection_excess_gd(
        CONTROL_ECHO_GAIN,
        CONTROL_ECHO_MS,
        sample_rate,
        trusted_band_hz=trusted_band_hz,
    )
    c3_ep = phase_of(echo)
    c3 = excursions(
        ExcessPhase(
            freqs=c3_ep.freqs,
            excess_phase=c3_ep.excess_phase - c3_bias.excess_phase,
            excess_gd_us=c3_ep.excess_gd_us - c3_bias.excess_gd_us,
            bulk_delay_us=c3_ep.bulk_delay_us,
        )
    )
    c3_delta = {
        key: c3[key]["excursion_us"] - baseline[key]["excursion_us"] for key in keys
    }
    c3_bias_removed = {
        key: reading["excursion_us"] for key, reading in excursions(c3_bias).items()
    }

    # C4 / C4b — the discriminating pair, at every feature. Same comb geometry,
    # opposite phase class. If the instrument cannot separate them it cannot
    # classify anything, and the separation is also the SCALE every verdict is
    # read against.
    pair: dict[str, Any] = {}
    for key, fc in zip(keys, features):
        tau_ms = tau_for_first_null_ms(fc)
        nmp = egd_excursion(
            phase_of(add_delayed_copy(ir, CONTROL_COMB_NMP_GAIN, tau_ms, sample_rate)),
            fc,
            trusted_band_hz,
        )
        mp = egd_excursion(
            phase_of(add_delayed_copy(ir, CONTROL_COMB_MP_GAIN, tau_ms, sample_rate)),
            fc,
            trusted_band_hz,
        )
        pair[key] = {
            "tau_us": tau_ms * 1e3,
            "nmp_excursion_us": nmp["excursion_us"],
            "mp_excursion_us": mp["excursion_us"],
            "nmp_delta_us": nmp["excursion_us"] - baseline[key]["excursion_us"],
            "mp_delta_us": mp["excursion_us"] - baseline[key]["excursion_us"],
            "separation_us": abs(nmp["excursion_us"] - mp["excursion_us"]),
        }

    c1_max = max(abs(v) for v in c1_delta.values())
    c3_max = max(abs(v) for v in c3_delta.values())
    ratio_lo, ratio_hi = CONTROL_ALLPASS_RATIO_BAND
    c2_ok = all(ratio_lo <= entry["ratio"] <= ratio_hi for entry in c2.values())
    separation = min(entry["separation_us"] for entry in pair.values())
    required = CONTROL_SEPARATION_MARGIN * max(c1_max, c3_max, 1.0)
    # WHICH control missed, named once and reduced to ``passes`` — the same four
    # terms the boolean was spelled from. #3480 met the same suite passing on
    # one verify round of a rig and failing on another with nothing saying
    # which moved.
    failed = [
        name
        for name, ok in (
            ("C1_min_phase_peaking", c1_max < CONTROL_MAX_FALSE_POSITIVE_US),
            ("C2_allpass", c2_ok),
            ("C3_min_phase_echo", c3_max < CONTROL_MAX_ECHO_FALSE_POSITIVE_US),
            ("C4_pair", separation > required),
        )
        if not ok
    ]
    verdict = {
        "C1_max_false_positive_us": c1_max,
        "C1_limit_us": CONTROL_MAX_FALSE_POSITIVE_US,
        "C2_recovers_theory": c2_ok,
        "C2_ratio_band": [ratio_lo, ratio_hi],
        "C3_max_false_positive_us": c3_max,
        "C3_limit_us": CONTROL_MAX_ECHO_FALSE_POSITIVE_US,
        # What the bar did NOT have to be widened by. Rides the verdict rather
        # than the block below because only the verdict reaches a refusal.
        "C3_injection_bias_removed_us": max(map(abs, c3_bias_removed.values())),
        "C4_min_separation_us": separation,
        "C4_required_separation_us": required,
        "failed": failed,
        "passes": not failed,
    }
    return {
        "C0_untouched": baseline,
        "C1_min_phase_peaking": {
            "spec": (
                f"+{CONTROL_PEAKING_GAIN_DB} dB Q{CONTROL_PEAKING_Q} at "
                f"{anchor:.0f} Hz (RBJ peaking, minimum phase)"
            ),
            "expect": "flat excess GD — delta from C0 near zero",
            "at_hz": anchor,
            "delta_us": c1_delta,
        },
        "C2_allpass": {
            "spec": "RBJ 2nd-order all-pass; magnitude unchanged, pure excess phase",
            "expect": "recovered peak excess GD matches the filter's own",
            "at": c2,
        },
        "C3_min_phase_echo": {
            "spec": (
                f"h(t) {CONTROL_ECHO_GAIN:+}*h(t-{CONTROL_ECHO_MS}ms) — |g| < 1, "
                "so MINIMUM phase"
            ),
            "expect": "flat; flagging it would be a false positive, not a pass",
            "injection_bias_removed_us": c3_bias_removed,
            "delta_us": c3_delta,
        },
        "C4_pair": {
            "spec": (
                f"h(t) + g*h(t-tau), tau = 1/(2*fc); g={CONTROL_COMB_NMP_GAIN} is "
                f"non-minimum phase, g={CONTROL_COMB_MP_GAIN} is minimum phase with "
                "the same comb geometry"
            ),
            "expect": "non-minimum-phase excursion far above its twin at every feature",
            "at": pair,
        },
        "verdict": verdict,
    }
