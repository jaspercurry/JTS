# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Complete-tune capture keeps one stimulus level and one clock."""

import numpy as np
import pytest

from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import build_verify_program, segment_stimulus
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry, MeasurementPriors, analyze_program_capture,
)
from tests.test_audio_measurement_program_analysis import SR, _band_impulse, _synthesize


@pytest.mark.parametrize("epsilon,polarity", [(0.0, 1), (80e-6, -1), (-60e-6, 1)])
@pytest.mark.parametrize("branches", [("woofer", "tweeter"), ("left:woofer", "left:woofer:rear")])
def test_solo_and_sum_keep_measured_level_phase_and_recording_clock(epsilon, polarity, branches):
    base = build_verify_program(1600, measurement_band_hz=(150, 20000), gain_db=-20, sweep_s=.6, courtesy_prelude=False)
    program = build_branch_program(base, dict(zip(branches, (0, 1))))
    sweeps = [s for s in program.stimulus_segments() if s.kind != "pilot"]
    assert len(sweeps) == 6
    assert {s.gain_db for s in sweeps} == {-20}
    for sweep in sweeps:
        np.testing.assert_array_equal(segment_stimulus(sweep), segment_stimulus(base.segment("sweep_verify")))
    capture = _synthesize(
        program, woofer_ir=_band_impulse(200, 150, 20000, 1),
        tweeter_ir=_band_impulse(212, 150, 20000, .7 * polarity),
        epsilon=epsilon, noise=1e-7,
    )
    result = analyze_program_capture(program, capture, SR, priors=MeasurementPriors(crossover_fc_hz=1600))
    assert result.candidate is None and result.alignment is None
    assert result.drift.epsilon_ppm == pytest.approx(epsilon * 1e6, abs=2)
    assert not result.glitch_detected
    lower, upper = result.driver_responses
    mask = (lower.freqs_hz >= 1200) & (lower.freqs_hz <= 4000)
    ratio = upper.complex_tf[mask] / lower.complex_tf[mask]
    phase = np.unwrap(np.angle(ratio / polarity))
    delay = -np.polyfit(lower.freqs_hz[mask], phase, 1)[0] / (2 * np.pi)
    assert delay * 1e6 == pytest.approx(250, abs=5)
    assert np.median(20 * np.log10(abs(ratio))) == pytest.approx(20 * np.log10(.7), abs=.08)
    predicted = lower.complex_tf[mask] + upper.complex_tf[mask]
    summed = result.summed_response.complex_tf[mask]
    assert np.percentile(abs(20 * np.log10(abs(predicted / summed))), 95) < .2
    assert {r["role"] for r in result.branch_diagnostic["responses"]} == {*branches, "summed"}
    assert all(len(r["impulse"]) <= round(.4 * SR) for r in result.branch_diagnostic["responses"])


def test_a_gate_exemption_reaches_every_branch_segment_of_one_capture():
    """The rear's whole job is below the reflection gate's trusted floor, so a
    rear pair take reads both woofers and their sum ungated (issue #5330); a
    take that states no exemption keeps today's gated result.
    """
    base = build_verify_program(1600, measurement_band_hz=(40, 20000), gain_db=-20,
                                sweep_s=.6, courtesy_prelude=False)
    program = build_branch_program(base, {"woofer": 0, "woofer:rear": 1})
    capture = _synthesize(program, woofer_ir=_band_impulse(200, 40, 20000, 1),
                          tweeter_ir=_band_impulse(212, 40, 20000, .7), noise=1e-7)
    priors = MeasurementPriors(crossover_fc_hz=1600)
    gated, exempt = (
        analyze_program_capture(program, capture, SR, priors=priors, geometry=geometry)
        for geometry in (None, MeasurementGeometry(gate_exempt_reason=SEAT_EXEMPT))
    )
    rows = {name: (*result.driver_responses, result.summed_response)
            for name, result in (("gated", gated), ("exempt", exempt))}

    assert [r.role for r in rows["exempt"]] == ["woofer", "woofer:rear", "summed"]
    assert [r.gating["applied"] for r in rows["exempt"]] == [False] * 3
    assert {r.gating["exempt_reason"] for r in rows["exempt"]} == {SEAT_EXEMPT}
    # No validity floor claimed: the whole measured band is the answer.
    assert [r.validity_floor_hz for r in rows["exempt"]] == [None] * 3
    assert [r.gating["applied"] for r in rows["gated"]] == [True] * 3
    assert {r.gating["exempt_reason"] for r in rows["gated"]} == {None}
    floor = rows["gated"][0].gating["f_trusted_hz"]
    assert floor > 300 and all(r.validity_floor_hz for r in rows["gated"])
    # One decade below that floor the flat synthetic woofer is read truly only
    # without the gate's short window.
    errors = [
        abs(float(r.magnitude_db[int(np.argmin(abs(r.freqs_hz - floor / 6)))]))
        for r in (rows["gated"][0], rows["exempt"][0])
    ]
    assert errors[0] - errors[1] > 2.0
