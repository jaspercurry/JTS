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
from jasper.audio_measurement.gating import SEAT_EXEMPT
from jasper.audio_measurement.program_analysis import (
    MeasurementGeometry, MeasurementPriors, analyze_program_capture,
)
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
