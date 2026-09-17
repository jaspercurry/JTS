# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Complete-tune capture keeps one stimulus level and one clock."""

from inspect import signature

import numpy as np
import pytest

from jasper.active_speaker.test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES
from jasper.audio_measurement.branch_program import build_branch_program
from jasper.audio_measurement.program import (
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    build_verify_program,
    render_program_pcm,
    segment_stimulus,
)
from jasper.audio_measurement.program_analysis import MeasurementPriors, analyze_program_capture
from tests.test_audio_measurement_program_analysis import SR, _band_impulse, _synthesize


@pytest.mark.parametrize("epsilon,polarity", [(0.0, 1), (80e-6, -1), (-60e-6, 1)])
@pytest.mark.parametrize("branches", [("woofer", "tweeter"), ("left:woofer", "left:woofer:rear")])
@pytest.mark.parametrize("cooldown_s", [0.0, 2.0])
def test_solo_and_sum_keep_measured_level_phase_and_recording_clock(epsilon, polarity, branches, cooldown_s):
    base = build_verify_program(1600, measurement_band_hz=(150, 20000), gain_db=-20, sweep_s=.6, courtesy_prelude=False)
    program = build_branch_program(base, dict(zip(branches, (0, 1))), cooldown_s=cooldown_s)
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


#: Longest declared cooldown the worst composable take still fits the capture
#: upload budget at. Past it ``bundles._guarded_capture_source`` drops the WAV
#: from the evidence bundle with a warning rather than refusing, so the ceiling
#: is worth a number: 11.2 s under per-target padding, ~5 s if a boundary ever
#: pads uniformly again. Nobody declares above 5 s (tests/test_bass_stimulus.py).
CAPTURE_BUDGET_COOLDOWN_CEILING_S = 11.2


@pytest.mark.parametrize("cooldown_s", [2.0, 5.0, CAPTURE_BUDGET_COOLDOWN_CEILING_S])
def test_declared_cooldown_spaces_same_driver_excitations_inside_the_capture_cap(cooldown_s):
    """A take loads each target three times, so the declared cooldown has to sit
    between consecutive excitations of ONE target -- the end-to-start gap
    ``program_admission`` grades, and only there, so the take grows by the least
    that satisfies it. Zero composes the unspaced program verbatim; the longest
    take a composer can build fits both the capture upload budget and the
    playback timeout its own door holds.
    """
    # The longest sweep any declaration can compose (no per-role limit).
    base = build_verify_program(1600, measurement_band_hz=(150, 20000), gain_db=-20)
    channels = {"woofer": 0, "tweeter": 1}
    unspaced = build_branch_program(base, channels)
    assert build_branch_program(base, channels, cooldown_s=0.0) == unspaced
    spaced = build_branch_program(base, channels, cooldown_s=cooldown_s)
    np.testing.assert_array_equal(
        render_program_pcm(build_branch_program(base, channels, cooldown_s=0.0)),
        render_program_pcm(unspaced),
    )
    for channel in channels.values():
        run = sorted((s for s in spaced.segments if s.channel == channel
                      and s.kind in (KIND_SWEEP, KIND_SUMMED_SWEEP)),
                     key=lambda s: s.start_sample)
        assert len(run) == 3
        assert min(b.start_sample - a.start_sample - a.n_samples
                   for a, b in zip(run, run[1:])) >= cooldown_s * SR
    # Mono 16-bit capture plus the capture-plan margin, measured the same way
    # test_audio_measurement_program.py pins the MEASURE budget.
    wav_bytes = 44 + 2 * (spaced.total_samples + 2 * SR)
    assert wav_bytes < CROSSOVER_CAPTURE_MAX_WAV_BYTES
    if cooldown_s < CAPTURE_BUDGET_COOLDOWN_CEILING_S:
        assert CROSSOVER_CAPTURE_MAX_WAV_BYTES - wav_bytes > 512 * 1024
    # The take's own playback door kills the player past this; read off the
    # owner rather than copied, so a change to it moves this pin with it.
    from jasper.active_speaker.crossover_v2.composition import bind_program_playback_seams

    timeout_s = signature(bind_program_playback_seams).parameters["timeout_s"].default
    assert spaced.total_samples / SR < timeout_s
