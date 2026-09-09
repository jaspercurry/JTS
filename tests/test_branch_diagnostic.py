# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Complete-tune capture keeps one stimulus level and one clock."""

import numpy as np
import pytest

from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import build_verify_program, segment_stimulus
from jasper.audio_measurement.program_analysis import MeasurementPriors, analyze_program_capture
from tests.test_audio_measurement_program_analysis import SR, _band_impulse, _synthesize


@pytest.mark.parametrize("epsilon,polarity", [(0.0, 1), (80e-6, -1), (-60e-6, 1)])
def test_solo_and_sum_keep_measured_level_phase_and_recording_clock(epsilon, polarity):
    base = build_verify_program(1600, measurement_band_hz=(150, 20000), gain_db=-20, sweep_s=.6, courtesy_prelude=False)
    program = build_branch_program(base, {"woofer": 0, "tweeter": 1})
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
    assert {r["role"] for r in result.branch_diagnostic["responses"]} == {"woofer", "tweeter", "summed"}
    assert all(len(r["impulse"]) <= round(.4 * SR) for r in result.branch_diagnostic["responses"])
