# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Excitation-program composers + schedule model (crossover conductor W1).

Pins the pure-data half of the conductor flow
(docs/crossover-measurement-productization-design.md §5.3):

  - the three phase composers produce the design's segment layout
    (ambient + pilots for CHECK; woofer → tweeter → woofer-repeat for MEASURE;
    a mono summed sweep for VERIFY), with the repeat bit-identical to the first
    woofer sweep;
  - the MESM inter-sweep gap satisfies ``gap ≥ ir_tail + L·ln(order)`` with a
    conservative floor;
  - per-segment digital gains are recorded (``gain_db`` / ``effective_peak_dbfs``)
    from composer INPUT (no safety admission here);
  - the schedule renders to interleaved PCM and JSON round-trips.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.audio_measurement.program import (
    BASE_STIMULUS_PEAK_DBFS,
    KIND_SILENCE,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    MESM_GAP_FLOOR_S,
    PHASE_CHECK,
    PHASE_MEASURE,
    PHASE_VERIFY,
    PROGRAM_SAMPLE_RATE_HZ,
    VERIFY_PILOT_F_HI_HZ,
    VERIFY_PILOT_F_LO_HZ,
    ExcitationProgram,
    ProgramSegment,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
    mesm_gap_samples,
    render_program_pcm,
    segment_stimulus,
    write_program_wav,
)
from jasper.audio_measurement.sweep import synchronized_sweep_metadata


def _roles() -> list[RoleBand]:
    return [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 20000.0)),
    ]


def _gain_plan() -> dict[str, float]:
    return {"woofer": -11.0, "tweeter": -13.0}


# --------------------------------------------------------------------------- #
# CHECK composer
# --------------------------------------------------------------------------- #


def test_check_program_layout_and_gains():
    prog = build_check_program(
        _roles(), ambient_s=2.0, pilot_duration_s=0.6,
        pilot_levels_db=(-10.0, 0.0),
    )
    assert prog.phase == PHASE_CHECK
    assert prog.channels == 2
    assert prog.sample_rate_hz == PROGRAM_SAMPLE_RATE_HZ

    # Leading ambient silence, then two pilots per driver.
    assert prog.segments[0].segment_id == "ambient"
    assert prog.segments[0].kind == KIND_SILENCE
    pilots = prog.stimulus_segments()
    assert [p.segment_id for p in pilots] == [
        "pilot_woofer_lo", "pilot_woofer_hi",
        "pilot_tweeter_lo", "pilot_tweeter_hi",
    ]
    # Pilots ride their driver's channel; gains are base + relative level.
    woofer_lo = prog.segment("pilot_woofer_lo")
    woofer_hi = prog.segment("pilot_woofer_hi")
    assert woofer_lo.channel == 0 and woofer_hi.channel == 0
    assert prog.segment("pilot_tweeter_lo").channel == 1
    assert woofer_lo.gain_db == pytest.approx(BASE_STIMULUS_PEAK_DBFS - 10.0)
    assert woofer_hi.gain_db == pytest.approx(BASE_STIMULUS_PEAK_DBFS + 0.0)
    # The programmed pilot delta is exactly the 10 dB the linearity check expects.
    assert woofer_hi.gain_db - woofer_lo.gain_db == pytest.approx(10.0)
    # effective_peak folds the (default zero) downstream gain in.
    assert woofer_hi.effective_peak_dbfs == pytest.approx(woofer_hi.gain_db)


def test_check_gaps_are_at_least_half_second():
    prog = build_check_program(_roles(), ambient_s=1.0, pilot_gap_s=0.5)
    gaps = [s for s in prog.segments if s.kind == KIND_SILENCE and s.segment_id != "ambient"]
    assert gaps, "expected inter-pilot gaps"
    for gap in gaps:
        assert gap.n_samples >= 0.5 * PROGRAM_SAMPLE_RATE_HZ


# --------------------------------------------------------------------------- #
# MEASURE composer
# --------------------------------------------------------------------------- #


def test_measure_program_layout_repeat_bit_identical():
    prog = build_measure_program(
        _gain_plan(), _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    assert prog.phase == PHASE_MEASURE
    assert prog.channels == 2
    ids = [s.segment_id for s in prog.segments]
    assert ids == ["guard", "sweep_w", "gap_w_t", "sweep_t", "gap_t_w", "sweep_w_rep", "tail"]

    sweep_w = prog.segment("sweep_w")
    sweep_rep = prog.segment("sweep_w_rep")
    # The repeat is a bit-identical stimulus (same band/duration/gain).
    assert sweep_rep.n_samples == sweep_w.n_samples
    assert sweep_rep.f1_hz == sweep_w.f1_hz and sweep_rep.f2_hz == sweep_w.f2_hz
    assert sweep_rep.gain_db == sweep_w.gain_db
    assert np.array_equal(segment_stimulus(sweep_w), segment_stimulus(sweep_rep))
    # Woofer on ch0, tweeter on ch1.
    assert sweep_w.channel == 0
    assert prog.segment("sweep_t").channel == 1


def test_measure_sweeps_band_limited_to_measurement_window():
    prog = build_measure_program(_gain_plan(), _roles())
    # Tweeter declared band [300, 20000] ∩ [150, 20000] = [300, 20000].
    sweep_t = prog.segment("sweep_t")
    assert sweep_t.f1_hz == pytest.approx(300.0)
    assert sweep_t.f2_hz == pytest.approx(20000.0)
    # Woofer declared band [150, 6000] ∩ [150, 20000] = [150, 6000].
    sweep_w = prog.segment("sweep_w")
    assert sweep_w.f1_hz == pytest.approx(150.0)
    assert sweep_w.f2_hz == pytest.approx(6000.0)


def test_measure_requires_two_drivers_and_all_gains():
    with pytest.raises(ValueError):
        build_measure_program(_gain_plan(), _roles()[:1])
    with pytest.raises(ValueError):
        build_measure_program({"woofer": -11.0}, _roles())


# --------------------------------------------------------------------------- #
# VERIFY composer
# --------------------------------------------------------------------------- #


def test_verify_program_is_mono_full_band():
    prog = build_verify_program(1600.0, sweep_s=1.0)
    assert prog.phase == PHASE_VERIFY
    assert prog.channels == 1
    sweep = prog.segment("sweep_verify")
    assert sweep.kind == KIND_SUMMED_SWEEP
    assert sweep.channel == 0
    assert sweep.f2_hz == pytest.approx(20000.0)
    # Full-band low bound at 150 for a normal Fc; the shoulder is covered.
    assert sweep.f1_hz == pytest.approx(150.0)


def test_verify_widens_low_bound_for_low_fc():
    prog = build_verify_program(200.0, sweep_s=1.0)
    # f1 = min(150, fc/2) = 100 so the lower shoulder fc/2 is excited.
    assert prog.segment("sweep_verify").f1_hz == pytest.approx(100.0)


def test_verify_leading_pilot_rides_its_own_flat_band_not_the_notched_sweep_band():
    """W6.7 ruling 2: the leading VERIFY pilot pair must NOT share the summed
    sweep's full band — that band deliberately crosses the crossover overlap
    (the sweep needs to see the interference notch there), but a pilot chirp
    swept through that same notch goes noise-dominated and misfires the
    ±0.5 dB linearity ratio check (the W6 run-7 hardware bug). The pilot
    rides its own flat mid-woofer band instead."""
    prog = build_verify_program(1600.0, sweep_s=1.0, leading_pilot_gains_db=(-20.0, -10.0))
    sweep = prog.segment("sweep_verify")
    pilot_lo = prog.segment("pilot_summed_lo")
    pilot_hi = prog.segment("pilot_summed_hi")
    assert pilot_lo.f1_hz == pytest.approx(VERIFY_PILOT_F_LO_HZ)
    assert pilot_lo.f2_hz == pytest.approx(VERIFY_PILOT_F_HI_HZ)
    assert pilot_hi.f1_hz == pytest.approx(VERIFY_PILOT_F_LO_HZ)
    assert pilot_hi.f2_hz == pytest.approx(VERIFY_PILOT_F_HI_HZ)
    # The pilot band sits comfortably below the sweep's own low shoulder for
    # a normal (well above ~1 kHz) crossover -- it is NOT the sweep's band.
    assert VERIFY_PILOT_F_HI_HZ < sweep.f2_hz
    assert (pilot_lo.f1_hz, pilot_lo.f2_hz) != (sweep.f1_hz, sweep.f2_hz)


# --------------------------------------------------------------------------- #
# MESM gap rule
# --------------------------------------------------------------------------- #


def test_mesm_gap_rule():
    meta = synchronized_sweep_metadata(
        f1=150.0, f2=6000.0, duration_approx_s=4.0,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    )
    ir_tail_s = 0.5
    order = 3
    gap = mesm_gap_samples(meta, ir_tail_s=ir_tail_s, max_harmonic_order=order)
    expected_s = max(MESM_GAP_FLOOR_S, ir_tail_s + meta.L * math.log(order))
    assert gap == int(round(expected_s * PROGRAM_SAMPLE_RATE_HZ))
    # A long sweep pushes the harmonic pre-ring above the floor.
    assert gap >= int(round((ir_tail_s + meta.L * math.log(order)) * PROGRAM_SAMPLE_RATE_HZ))


def test_mesm_gap_respects_conservative_floor():
    # A very short sweep has a tiny pre-ring; the ~1 s floor still applies.
    meta = synchronized_sweep_metadata(
        f1=150.0, f2=6000.0, duration_approx_s=0.5,
        sample_rate=PROGRAM_SAMPLE_RATE_HZ,
    )
    gap = mesm_gap_samples(meta, ir_tail_s=0.1, max_harmonic_order=3)
    assert gap == int(round(MESM_GAP_FLOOR_S * PROGRAM_SAMPLE_RATE_HZ))


# --------------------------------------------------------------------------- #
# render + WAV + manifest
# --------------------------------------------------------------------------- #


def test_render_pcm_shape_and_channel_placement():
    prog = build_measure_program(
        _gain_plan(), _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    pcm = render_program_pcm(prog)
    assert pcm.shape == (prog.total_samples, prog.channels)
    assert pcm.dtype == np.float32
    # The woofer sweep energy is on ch0 only; ch1 is silent there.
    sweep_w = prog.segment("sweep_w")
    seg = pcm[sweep_w.start_sample:sweep_w.start_sample + sweep_w.n_samples]
    assert np.max(np.abs(seg[:, 0])) > 0.05
    assert np.max(np.abs(seg[:, 1])) == 0.0
    # Digital peak matches the woofer gain (unit-peak stimulus × 10**(gain/20)).
    assert 20.0 * math.log10(float(np.max(np.abs(seg[:, 0])))) == pytest.approx(
        sweep_w.gain_db, abs=0.2
    )


def test_write_program_wav_is_interleaved_s16(tmp_path):
    from scipy.io import wavfile

    prog = build_measure_program(
        _gain_plan(), _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    path = tmp_path / "measure.wav"
    write_program_wav(path, prog)
    rate, data = wavfile.read(str(path))
    assert rate == PROGRAM_SAMPLE_RATE_HZ
    assert data.dtype == np.int16
    assert data.shape == (prog.total_samples, prog.channels)


def test_program_manifest_json_round_trip():
    prog = build_check_program(_roles(), ambient_s=1.0, pilot_duration_s=0.5)
    blob = json.dumps(prog.to_dict())
    restored = ExcitationProgram.from_dict(json.loads(blob))
    assert restored == prog
    assert restored.program_id == prog.program_id
    # And a measure program too (different phase / channel routing).
    measure = build_measure_program(_gain_plan(), _roles())
    assert ExcitationProgram.from_dict(
        json.loads(json.dumps(measure.to_dict()))
    ) == measure


def test_program_id_is_content_addressed():
    a = build_measure_program(_gain_plan(), _roles())
    b = build_measure_program(_gain_plan(), _roles())
    assert a.program_id == b.program_id  # deterministic
    c = build_measure_program({"woofer": -12.0, "tweeter": -13.0}, _roles())
    assert c.program_id != a.program_id  # a gain change is a new identity


def test_tampered_manifest_is_rejected():
    prog = build_verify_program(1600.0, sweep_s=1.0)
    payload = prog.to_dict()
    payload["total_samples"] = payload["total_samples"] + 1
    with pytest.raises(ValueError):
        ExcitationProgram.from_dict(payload)


def test_segment_validation_rejects_bad_shapes():
    with pytest.raises(ValueError):
        ProgramSegment(
            segment_id="x", kind="bogus", role=None, channel=None,
            start_sample=0, n_samples=1, f1_hz=None, f2_hz=None,
            gain_db=0.0, effective_peak_dbfs=-120.0,
        )
    with pytest.raises(ValueError):
        # A stimulus segment must carry a band + channel.
        ProgramSegment(
            segment_id="s", kind=KIND_SWEEP, role="woofer", channel=None,
            start_sample=0, n_samples=10, f1_hz=None, f2_hz=None,
            gain_db=-12.0, effective_peak_dbfs=-12.0,
        )
