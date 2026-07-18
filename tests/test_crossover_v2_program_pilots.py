# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""W5a composer extension: leading pilot pairs + woofer-repeat level agreement.

docs/crossover-measurement-productization-design.md §5.2: every MEASURE and
VERIFY program also opens with a short two-level pilot pair so EACH capture
carries its own linearity evidence, and MEASURE acceptance additionally
requires the woofer repeat pair to agree in level within ±0.3 dB (a
gain-riding detector complementing the timing baselines). A repeat-level
failure REUSES the ``drift_baselines_disagree`` verdict (``glitch_detected``)
— never a new user-facing code.

Fixture style mirrors tests/test_audio_measurement_program_analysis.py:
captures are composed by convolving each program channel with a synthetic
band-passed driver IR, then perturbed (AGC on the hi pilot, a level step on
the repeat sweep).
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.signal import fftconvolve

from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    KIND_PILOT,
    KIND_SILENCE,
    RoleBand,
    build_measure_program,
    build_verify_program,
    render_program_pcm,
)
from jasper.audio_measurement.program_analysis import (
    REPEAT_LEVEL_TOLERANCE_DB,
    MeasurementGeometry,
    MeasurementPriors,
    analyze_program_capture,
)

SR = 48_000
FC_HZ = 1600.0
GLOBAL_OFFSET = 900


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _measure_program(with_pilots: bool = True):
    kwargs = {}
    if with_pilots:
        kwargs = {
            "leading_pilot_gains_db": (-22.0, -12.0),
            "pilot_duration_s": 0.5,
        }
    return build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 1.0, "tweeter": 0.8},
        **kwargs,
    )


def _band_impulse(delay: int, f_lo: float, f_hi: float, amp: float, n: int = 4096) -> np.ndarray:
    imp = np.zeros(n)
    imp[delay] = amp
    spectrum = np.fft.rfft(imp)
    freqs = np.fft.rfftfreq(n, 1.0 / SR)
    spectrum[(freqs < f_lo) | (freqs > f_hi)] = 0.0
    return np.fft.irfft(spectrum, n)


def _synthesize(program, *, noise: float = 1e-4, seed: int = 0) -> np.ndarray:
    pcm = render_program_pcm(program)
    woofer_ir = _band_impulse(200, 150.0, 6000.0, 1.0)
    tweeter_ir = _band_impulse(225, 300.0, 20000.0, 0.7)
    mono = np.zeros(pcm.shape[0], dtype=np.float64)
    mono += fftconvolve(pcm[:, 0], woofer_ir)[: pcm.shape[0]]
    if pcm.shape[1] >= 2:
        mono += fftconvolve(pcm[:, 1], tweeter_ir)[: pcm.shape[0]]
    cap = np.concatenate([np.zeros(GLOBAL_OFFSET), mono, np.zeros(20_000)])
    if noise:
        cap = cap + np.random.default_rng(seed).normal(0.0, noise, cap.size)
    return cap


# --- composer layout ----------------------------------------------------------


def test_measure_default_layout_is_unchanged_without_pilots():
    """Opt-out (the legacy composer call) keeps the exact pre-W5a layout."""
    prog = _measure_program(with_pilots=False)
    ids = [seg.segment_id for seg in prog.segments]
    assert ids == ["guard", "sweep_w", "gap_w_t", "sweep_t", "gap_t_w", "sweep_w_rep", "tail"]


def test_measure_leading_pilot_pair_layout_and_gains():
    prog = _measure_program()
    ids = [seg.segment_id for seg in prog.segments]
    assert ids[:5] == [
        "pilot_woofer_lo", "pilot_gap_woofer_lo",
        "pilot_woofer_hi", "pilot_gap_woofer_hi",
        "guard",
    ]
    assert ids[5:] == ["sweep_w", "gap_w_t", "sweep_t", "gap_t_w", "sweep_w_rep", "tail"]
    lo = prog.segment("pilot_woofer_lo")
    hi = prog.segment("pilot_woofer_hi")
    assert lo.kind == KIND_PILOT and hi.kind == KIND_PILOT
    assert lo.role == "woofer" and lo.channel == 0
    assert hi.gain_db - lo.gain_db == pytest.approx(10.0)
    # Pilots ride the woofer's measurement band.
    assert lo.f1_hz == pytest.approx(150.0)
    assert lo.f2_hz == pytest.approx(6000.0)


def test_measure_pilot_role_selectable_and_validated():
    prog = build_measure_program(
        {"woofer": -11.0, "tweeter": -13.0}, _roles(),
        sweep_durations={"woofer": 1.0, "tweeter": 0.8},
        leading_pilot_gains_db=(-22.0, -12.0),
        leading_pilot_role="tweeter",
    )
    assert prog.segment("pilot_tweeter_lo").channel == 1
    with pytest.raises(ValueError):
        build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0}, _roles(),
            leading_pilot_gains_db=(-22.0, -12.0),
            leading_pilot_role="mid",
        )
    with pytest.raises(ValueError):
        build_measure_program(
            {"woofer": -11.0, "tweeter": -13.0}, _roles(),
            leading_pilot_gains_db=(-22.0,),
        )


def test_verify_leading_pilot_pair_is_mono_summed():
    prog = build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )
    assert prog.channels == 1
    ids = [seg.segment_id for seg in prog.segments]
    assert ids[:4] == [
        "pilot_summed_lo", "pilot_gap_summed_lo",
        "pilot_summed_hi", "pilot_gap_summed_hi",
    ]
    pilot = prog.segment("pilot_summed_lo")
    assert pilot.channel == 0 and pilot.role == "summed"
    # Opt-out stays the legacy layout.
    legacy = build_verify_program(FC_HZ, sweep_s=1.5)
    assert [s.segment_id for s in legacy.segments] == ["guard", "sweep_verify", "tail"]


def test_pilot_gaps_are_silence():
    prog = _measure_program()
    for seg in prog.segments:
        if seg.segment_id.startswith("pilot_gap_"):
            assert seg.kind == KIND_SILENCE


# --- analysis: per-capture linearity ------------------------------------------


def test_measure_pilot_linearity_clean_capture_passes():
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR,
        priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
        geometry=MeasurementGeometry(),
    )
    assert res.linearity_ok is True
    assert len(res.pilots) == 1
    assert res.pilots[0].captured_delta_db == pytest.approx(10.0, abs=0.5)
    # The pilot pair does not perturb the sweep-anchored drift verdict.
    assert not res.glitch_detected
    assert res.candidate is not None


def test_measure_pilot_linearity_fails_under_simulated_agc():
    prog = _measure_program()
    cap = _synthesize(prog)
    # AGC-compress the HI pilot only: programmed 10 dB delta captured as ~4 dB.
    hi = prog.segment("pilot_woofer_hi")
    start = GLOBAL_OFFSET + hi.start_sample
    cap[start:start + hi.n_samples] *= 10.0 ** (-6.0 / 20.0)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.linearity_ok is False


def test_legacy_measure_program_reports_no_pilot_verdict():
    """No pilots ⇒ linearity is ``None`` (no evidence), not True/False."""
    prog = _measure_program(with_pilots=False)
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.linearity_ok is None
    assert res.pilots == ()


def test_verify_pilot_linearity_verdict_present():
    prog = build_verify_program(
        FC_HZ, sweep_s=1.5,
        leading_pilot_gains_db=(-22.0, -12.0), pilot_duration_s=0.5,
    )
    cap = _synthesize(prog)
    res = analyze_program_capture(prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ))
    assert res.linearity_ok is True
    assert res.summed_ripple_db is not None


# --- analysis: woofer-repeat level agreement ------------------------------------


def test_repeat_level_step_is_flagged_as_glitch():
    """A >±0.3 dB level step between the two woofer sweeps ⇒ glitch verdict.

    Timing is untouched (pure amplitude scale on the repeat window), so the
    drift baselines agree — the LEVEL check alone must trip, and it reuses the
    same ``glitch_detected`` verdict (⇒ ``drift_baselines_disagree`` reason,
    §5.2)."""
    prog = _measure_program()
    cap = _synthesize(prog)
    rep = prog.segment("sweep_w_rep")
    start = GLOBAL_OFFSET + rep.start_sample
    cap[start:start + rep.n_samples] *= 10.0 ** (-1.0 / 20.0)  # 1 dB quieter
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.drift is not None
    # The timing baselines still agree — the level detector is what fires.
    assert abs(res.drift.epsilon_ppm) < 100.0
    assert res.glitch_detected is True


def test_repeat_level_within_tolerance_is_clean():
    prog = _measure_program()
    cap = _synthesize(prog)
    rep = prog.segment("sweep_w_rep")
    start = GLOBAL_OFFSET + rep.start_sample
    delta_db = REPEAT_LEVEL_TOLERANCE_DB * 0.5
    cap[start:start + rep.n_samples] *= 10.0 ** (-delta_db / 20.0)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.glitch_detected is False


def test_measure_predicted_sum_travels_for_verify():
    """MEASURE now exports the predicted applied sum VERIFY compares against."""
    prog = _measure_program()
    cap = _synthesize(prog)
    res = analyze_program_capture(
        prog, cap, SR, priors=MeasurementPriors(crossover_fc_hz=FC_HZ),
    )
    assert res.predicted_sum is not None
    freqs, mag_db = res.predicted_sum
    assert freqs.shape == mag_db.shape
    assert freqs.size > 0
