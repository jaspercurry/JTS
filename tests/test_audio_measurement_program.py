# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Excitation-program composers + schedule model (crossover conductor W1).

Pins the pure-data half of the conductor flow
(docs/historical/crossover-measurement-productization-design.md §5.3):

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
    AMBIENT_SEGMENT_ID,
    BASE_STIMULUS_PEAK_DBFS,
    COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S,
    COURTESY_TONE_BEEP_COUNT,
    COURTESY_TONE_BEEP_DURATION_S,
    COURTESY_TONE_BEEP_GAP_S,
    COURTESY_TONE_MARGIN_DB,
    COURTESY_TONE_TRAILING_SILENCE_S,
    KIND_COURTESY_TONE,
    KIND_PILOT,
    KIND_SILENCE,
    KIND_SUMMED_SWEEP,
    KIND_SWEEP,
    KNOWN_AUDIBLE_KINDS,
    MEASURE_SWEEP_F_HI_HZ,
    MESM_GAP_FLOOR_S,
    PROGRAM_PHASE_CHECK,
    PROGRAM_PHASE_MEASURE,
    PROGRAM_PHASE_VERIFY,
    PILOT_AMBIENT_WINDOW_S,
    PROGRAM_SAMPLE_RATE_HZ,
    STIMULUS_KINDS,
    VERIFY_PILOT_F_HI_HZ,
    VERIFY_PILOT_F_LO_HZ,
    ExcitationProgram,
    ProgramSegment,
    RoleBand,
    build_check_program,
    build_measure_program,
    build_verify_program,
    courtesy_tone_gain_db,
    courtesy_tone_stimulus,
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
    assert prog.phase == PROGRAM_PHASE_CHECK
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


def test_measure_program_layout_is_n3_interleaved_repeats_bit_identical():
    """Sweep-composition PR-A (#1668): the default program (N=3, no explicit
    ``repeat_count``) is the fully interleaved w1,t1,w2,t2,w3,t3 shape, and
    EVERY later occurrence of a driver is a bit-identical stimulus to that
    driver's first occurrence — not just the woofer, as before."""
    prog = build_measure_program(
        _gain_plan(), _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
    )
    assert prog.phase == PROGRAM_PHASE_MEASURE
    assert prog.channels == 2
    ids = [s.segment_id for s in prog.segments]
    assert ids == [
        "guard",
        "sweep_w", "gap_w_t", "sweep_t", "gap_t_w",
        "sweep_w_rep", "gap_w_t_rep", "sweep_t_rep", "gap_t_w_rep",
        "sweep_w_rep2", "gap_w_t_rep2", "sweep_t_rep2",
        "tail",
    ]

    sweep_w = prog.segment("sweep_w")
    sweep_t = prog.segment("sweep_t")
    for w_rep_id, t_rep_id in (("sweep_w_rep", "sweep_t_rep"), ("sweep_w_rep2", "sweep_t_rep2")):
        w_rep = prog.segment(w_rep_id)
        t_rep = prog.segment(t_rep_id)
        assert w_rep.n_samples == sweep_w.n_samples
        assert w_rep.f1_hz == sweep_w.f1_hz and w_rep.f2_hz == sweep_w.f2_hz
        assert w_rep.gain_db == sweep_w.gain_db
        assert np.array_equal(segment_stimulus(sweep_w), segment_stimulus(w_rep))
        assert t_rep.n_samples == sweep_t.n_samples
        assert t_rep.f1_hz == sweep_t.f1_hz and t_rep.f2_hz == sweep_t.f2_hz
        assert t_rep.gain_db == sweep_t.gain_db
        assert np.array_equal(segment_stimulus(sweep_t), segment_stimulus(t_rep))
    # Woofer on ch0, tweeter on ch1 — every occurrence, not just the first.
    for seg_id in ("sweep_w", "sweep_w_rep", "sweep_w_rep2"):
        assert prog.segment(seg_id).channel == 0
    for seg_id in ("sweep_t", "sweep_t_rep", "sweep_t_rep2"):
        assert prog.segment(seg_id).channel == 1


def test_measure_repeat_count_is_configurable():
    # repeat_count=1: one cycle, no repeats at all — a degenerate but valid
    # composition (distinct from the pre-#1668 asymmetric "woofer repeats
    # once, tweeter never" shape, which no longer exists as any repeat_count
    # value: this composer is symmetric by construction).
    prog = build_measure_program(_gain_plan(), _roles(), repeat_count=1)
    assert [s.segment_id for s in prog.segments] == [
        "guard", "sweep_w", "gap_w_t", "sweep_t", "tail",
    ]
    with pytest.raises(ValueError):
        build_measure_program(_gain_plan(), _roles(), repeat_count=0)


def test_measure_sweeps_band_limited_to_measurement_window():
    prog = build_measure_program(_gain_plan(), _roles())
    # Tweeter declared band [300, 20000] ∩ [150, 23000] = [300, 20000].
    sweep_t = prog.segment("sweep_t")
    assert sweep_t.f1_hz == pytest.approx(300.0)
    assert sweep_t.f2_hz == pytest.approx(20000.0)
    # Woofer declared band [150, 6000] ∩ [150, 23000] = [150, 6000].
    sweep_w = prog.segment("sweep_w")
    assert sweep_w.f1_hz == pytest.approx(150.0)
    assert sweep_w.f2_hz == pytest.approx(6000.0)


def test_measure_sweep_window_widened_to_23khz():
    """Sweep-composition PR-A (#1668): a driver band topping above the OLD
    20 kHz ceiling now composes up to the new 23 kHz one. A fixture topping
    at exactly 20 000 would false-pass against the old ceiling too, so this
    uses 23 500 — comfortably above 20 kHz and only clamped by the NEW window."""
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 23_500.0)),
    ]
    prog = build_measure_program(_gain_plan(), roles)
    sweep_t = prog.segment("sweep_t")
    assert sweep_t.f2_hz == pytest.approx(23_000.0)


def test_measure_sweep_ceiling_constant_is_in_lockstep_with_test_signal_plan():
    """The desync trap named by the scoping pass: MEASURE_SWEEP_F_HI_HZ and
    test_signal_plan.MAX_DRIVER_TEST_FREQUENCY_HZ name the SAME "no driver
    test signal goes above this" global ceiling and must move together."""
    from jasper.active_speaker.test_signal_plan import MAX_DRIVER_TEST_FREQUENCY_HZ

    assert MEASURE_SWEEP_F_HI_HZ == MAX_DRIVER_TEST_FREQUENCY_HZ


def test_measure_composer_raises_clear_error_if_ceiling_ever_exceeded_nyquist(monkeypatch):
    """Defense in depth (design item 4): MEASURE_SWEEP_F_HI_HZ is always
    < Nyquist today, so this can't fire in production — but if a future edit
    ever raised the ceiling past Nyquist without noticing, the composer must
    fail with ITS OWN clear error rather than the sweep kernel's deep raise."""
    monkeypatch.setattr(
        "jasper.audio_measurement.program.MEASURE_SWEEP_F_HI_HZ", 25_000.0,
    )
    roles = [
        RoleBand("woofer", 0, FrequencyBand(150.0, 6000.0)),
        RoleBand("tweeter", 1, FrequencyBand(300.0, 30_000.0)),
    ]
    with pytest.raises(ValueError, match="Nyquist"):
        build_measure_program(_gain_plan(), roles)


def test_measure_takes_one_or_two_drivers_and_needs_every_gain():
    # One declared driver is a 1-way passive main, not a malformed 2-way: it
    # composes, with the single role keeping the ``sweep_w`` spelling the drift
    # anchor resolves on.
    one_way = build_measure_program(_gain_plan(), _roles()[:1])
    assert one_way.segment("sweep_w").role == "woofer"
    assert "sweep_t" not in {seg.segment_id for seg in one_way.segments}
    with pytest.raises(ValueError):
        build_measure_program(_gain_plan(), _roles() + _roles()[:1])
    with pytest.raises(ValueError):
        build_measure_program({"woofer": -11.0}, _roles())


# --------------------------------------------------------------------------- #
# VERIFY composer
# --------------------------------------------------------------------------- #


def test_verify_program_is_mono_full_band():
    prog = build_verify_program(1600.0, sweep_s=1.0)
    assert prog.phase == PROGRAM_PHASE_VERIFY
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
    rides its own flat mid-woofer band instead. At the reference rig's
    fc=2000 the Fc clamp (fc/2.5 = 800) coincides with the fixed hi bound —
    the band stays [200, 800]."""
    prog = build_verify_program(2000.0, sweep_s=1.0, leading_pilot_gains_db=(-20.0, -10.0))
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


def test_verify_pilot_hi_bound_tracks_a_low_fc_below_the_crossover_shoulder():
    """W6.7 gate N1: a low-Fc preset would bring the crossover overlap
    ([Fc/2, 2·Fc]) down into the fixed 200-800 Hz window, so the pilot's hi
    bound is clamped to fc/2.5 — below the Fc/2 shoulder with margin."""
    prog = build_verify_program(1000.0, sweep_s=1.0, leading_pilot_gains_db=(-20.0, -10.0))
    pilot = prog.segment("pilot_summed_hi")
    assert pilot.f1_hz == pytest.approx(VERIFY_PILOT_F_LO_HZ)
    assert pilot.f2_hz == pytest.approx(400.0)  # fc/2.5, not the fixed 800
    # Entirely below the crossover overlap's lower shoulder fc/2 = 500.
    assert pilot.f2_hz < 1000.0 / 2.0


def test_verify_pilot_falls_back_below_the_crossover_for_degenerate_low_fc():
    """When fc/2.5 collapses the [200, hi] band entirely (very low Fc), the
    composer falls back to [fc/8, fc/4] — still below the crossover region."""
    prog = build_verify_program(400.0, sweep_s=1.0, leading_pilot_gains_db=(-20.0, -10.0))
    pilot = prog.segment("pilot_summed_hi")
    assert pilot.f1_hz == pytest.approx(400.0 / 8.0)  # 50
    assert pilot.f2_hz == pytest.approx(400.0 / 4.0)  # 100
    assert pilot.f2_hz < 400.0 / 2.0


@pytest.mark.parametrize(
    ("band_hz", "expected_pilot"),
    [
        ((45.0, 18000.0), (VERIFY_PILOT_F_LO_HZ, VERIFY_PILOT_F_HI_HZ)),
        ((2000.0, 12000.0), (2000.0, 12000.0)),
    ],
    ids=["clamped_into_the_declaration", "the_declaration_is_its_own_hull"],
)
def test_a_no_crossover_verify_rides_the_declaration_instead_of_a_corner(
    band_hz, expected_pilot,
):
    """A speaker with no corner clamps the fixed pilot window INTO its declared
    band, and falls back to that band rather than to ``[fc/8, fc/4]``."""
    prog = build_verify_program(
        None, measurement_band_hz=band_hz, leading_pilot_gains_db=(-30.0, -20.0),
    )
    pilots = [s for s in prog.segments if s.segment_id.startswith("pilot_summed_")]

    assert pilots
    assert {(s.f1_hz, s.f2_hz) for s in pilots} == {expected_pilot}
    # The sweep's low edge widens off the declaration the way a corner widens it.
    assert prog.segment("sweep_verify").f1_hz == pytest.approx(min(150.0, band_hz[0]))
    # …and the declaration is required: there is nothing else to derive from.
    with pytest.raises(ValueError):
        build_verify_program(None)


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


# --------------------------------------------------------------------------- #
# courtesy-tone prelude (issue #1677)
# --------------------------------------------------------------------------- #


def test_segment_validation_accepts_courtesy_tone_kind():
    """The kind vocabulary widened to admit the new non-stimulus kind."""
    seg = ProgramSegment(
        segment_id="courtesy_tone_ch0", kind=KIND_COURTESY_TONE, role=None,
        channel=0, start_sample=0, n_samples=100, f1_hz=1000.0, f2_hz=1000.0,
        gain_db=-18.0, effective_peak_dbfs=-18.0,
    )
    assert seg.kind == KIND_COURTESY_TONE
    # Still not a STIMULUS_KIND -- the whole point (locate/analysis must
    # ignore it exactly like silence).
    assert KIND_COURTESY_TONE not in STIMULUS_KINDS
    assert KIND_COURTESY_TONE in KNOWN_AUDIBLE_KINDS


def test_courtesy_prelude_defaults_off_byte_identical_to_pre_1677():
    """Era/back-compat: omitting ``courtesy_prelude`` is IDENTICAL (same
    program_id, same segments) to passing it explicitly ``False``, and both
    match the pre-#1677 shape every other test in this file already pins."""
    check_default = build_check_program(_roles(), ambient_s=1.0, pilot_duration_s=0.5)
    check_explicit = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=False,
    )
    assert check_default.program_id == check_explicit.program_id
    assert check_default.segments == check_explicit.segments
    assert check_default.segments[0].segment_id == "ambient"

    measure_default = build_measure_program(_gain_plan(), _roles())
    measure_explicit = build_measure_program(
        _gain_plan(), _roles(), courtesy_prelude=False,
    )
    assert measure_default.program_id == measure_explicit.program_id
    assert measure_default.segments[0].segment_id == "guard"

    verify_default = build_verify_program(1600.0, sweep_s=1.0)
    verify_explicit = build_verify_program(1600.0, sweep_s=1.0, courtesy_prelude=False)
    assert verify_default.program_id == verify_explicit.program_id
    assert verify_default.segments[0].segment_id == "guard"


def test_check_courtesy_prelude_layout_and_gains():
    prog = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
    )
    ids = [s.segment_id for s in prog.segments]
    # Flow-simplification §2.5: the room-listening window is LEAD-IN, so the
    # beeps land after it and directly in front of the first stimulus. Before
    # this, a 12 s ambient window sat between the beeps and any sound.
    assert ids[:4] == [
        "ambient", "courtesy_tone_ch0", "courtesy_tone_ch1", "courtesy_gap",
    ]
    assert ids[4] == "pilot_woofer_lo"
    legacy_ids = [s.segment_id for s in build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5,
    ).segments]
    assert ids[:1] + ids[4:] == legacy_ids

    tone0 = prog.segment("courtesy_tone_ch0")
    tone1 = prog.segment("courtesy_tone_ch1")
    gap = prog.segment("courtesy_gap")
    ambient = prog.segment("ambient")
    assert tone0.kind == KIND_COURTESY_TONE and tone1.kind == KIND_COURTESY_TONE
    assert tone0.role is None and tone1.role is None
    assert tone0.channel == 0 and tone1.channel == 1
    # Both tones start together -- they play simultaneously, now after ambient.
    assert tone0.start_sample == tone1.start_sample == ambient.n_samples
    assert tone0.n_samples == tone1.n_samples
    expected_tone_s = (
        COURTESY_TONE_BEEP_COUNT * COURTESY_TONE_BEEP_DURATION_S
        + (COURTESY_TONE_BEEP_COUNT - 1) * COURTESY_TONE_BEEP_GAP_S
    )
    assert tone0.n_samples == pytest.approx(
        expected_tone_s * PROGRAM_SAMPLE_RATE_HZ, abs=2,
    )
    # The trailing gap follows immediately, sized to the fixed silence window.
    assert gap.kind == KIND_SILENCE
    assert gap.start_sample == tone0.start_sample + tone0.n_samples
    assert gap.n_samples == pytest.approx(
        COURTESY_TONE_TRAILING_SILENCE_S * PROGRAM_SAMPLE_RATE_HZ, abs=2,
    )
    # The first pilot -- CHECK's first stimulus -- starts right after the gap.
    assert prog.segment("pilot_woofer_lo").start_sample == (
        gap.start_sample + gap.n_samples
    )

    # Level derivation: CHECK's loudest per-channel stimulus is the "hi"
    # pilot (base_peak_dbfs + 0), so each channel's tone is exactly
    # COURTESY_TONE_MARGIN_DB below that channel's own hi pilot.
    woofer_hi = prog.segment("pilot_woofer_hi")
    tweeter_hi = prog.segment("pilot_tweeter_hi")
    assert tone0.gain_db == pytest.approx(woofer_hi.gain_db - COURTESY_TONE_MARGIN_DB)
    assert tone1.gain_db == pytest.approx(tweeter_hi.gain_db - COURTESY_TONE_MARGIN_DB)
    assert tone0.effective_peak_dbfs == pytest.approx(tone0.gain_db)


def test_measure_courtesy_prelude_leads_the_program_and_shifts_the_rest():
    """A MEASURE program with pilots opens on the beeps (issue #1812).

    #1771 spliced the prelude in front of the first SWEEP, leaving the
    leading pilot pair — two FULL-GAIN chirps — playing at t=0, ahead of the
    6 dB quieter beeps. The owner heard that on 2026-07-28 ("sweeps then beep
    beep beep"). The beeps now lead, and everything the legacy program had
    keeps its id/kind/gain/duration and moves by the prelude + the pre-pilot
    ambient window.
    """
    kwargs = dict(
        sweep_durations={"woofer": 0.6, "tweeter": 0.5},
        leading_pilot_gains_db=(-21.0, -11.0),
    )
    legacy = build_measure_program(_gain_plan(), _roles(), **kwargs)
    prog = build_measure_program(
        _gain_plan(), _roles(), courtesy_prelude=True, **kwargs,
    )
    ids = [s.segment_id for s in prog.segments]
    assert ids[:4] == [
        "courtesy_tone_ch0", "courtesy_tone_ch1", "courtesy_gap", "ambient",
    ]
    assert ids[4:9] == [
        "pilot_woofer_lo", "pilot_gap_woofer_lo",
        "pilot_woofer_hi", "pilot_gap_woofer_hi", "guard",
    ]
    # Nothing else was added or removed — the prelude-free schedule (which
    # carries the ambient window either way, since it carries pilots) is
    # exactly the tail once the three prelude segments are dropped.
    assert ids[3:] == [s.segment_id for s in legacy.segments]

    tone0 = prog.segment("courtesy_tone_ch0")
    gap = prog.segment("courtesy_gap")
    assert tone0.start_sample == 0
    head_n = tone0.n_samples + gap.n_samples
    for old_seg, new_seg in zip(legacy.segments, prog.segments[3:]):
        assert new_seg.kind == old_seg.kind
        assert new_seg.gain_db == old_seg.gain_db
        assert new_seg.n_samples == old_seg.n_samples
        assert new_seg.channel == old_seg.channel
        assert new_seg.role == old_seg.role
        assert new_seg.start_sample == old_seg.start_sample + head_n
    assert prog.total_samples == legacy.total_samples + head_n

    # Level derivation, per channel: ch0 (woofer) sees BOTH the leading pilot
    # hi (-11) and sweep_w (gain_plan woofer=-11) tie for loudest; ch1
    # (tweeter) only ever plays at gain_plan's tweeter level (-13).
    tone1 = prog.segment("courtesy_tone_ch1")
    assert tone0.gain_db == pytest.approx(-11.0 - COURTESY_TONE_MARGIN_DB)
    assert tone1.gain_db == pytest.approx(-13.0 - COURTESY_TONE_MARGIN_DB)


def test_measure_courtesy_prelude_without_leading_pilot_still_splices():
    """The prelude is independent of the (separate) leading-pilot opt-in --
    MEASURE's own reference is then each channel's sweep gain alone, and the
    splice point is simply the guard silence with no pilots ahead of it."""
    prog = build_measure_program(_gain_plan(), _roles(), courtesy_prelude=True)
    ids = [s.segment_id for s in prog.segments]
    assert ids[:5] == [
        "guard", "courtesy_tone_ch0", "courtesy_tone_ch1", "courtesy_gap", "sweep_w",
    ]
    tone0 = prog.segment("courtesy_tone_ch0")
    tone1 = prog.segment("courtesy_tone_ch1")
    assert tone0.gain_db == pytest.approx(-11.0 - COURTESY_TONE_MARGIN_DB)
    assert tone1.gain_db == pytest.approx(-13.0 - COURTESY_TONE_MARGIN_DB)


def test_verify_courtesy_prelude_layout_and_gain():
    prog = build_verify_program(
        1600.0, sweep_s=1.0, gain_db=-9.0,
        leading_pilot_gains_db=(-19.0, -9.0), courtesy_prelude=True,
    )
    assert prog.channels == 1
    ids = [s.segment_id for s in prog.segments]
    # Beeps → settle → ambient window → pilots → guard → sweep (#1810/#1812).
    assert ids == [
        "courtesy_tone_ch0", "courtesy_gap", "ambient",
        "pilot_summed_lo", "pilot_gap_summed_lo",
        "pilot_summed_hi", "pilot_gap_summed_hi",
        "guard", "sweep_verify", "tail",
    ]
    tone = prog.segment("courtesy_tone_ch0")
    assert tone.channel == 0 and tone.start_sample == 0
    # Loudest content on the (only) channel is the -9 dBFS pilot-hi/sweep tie.
    assert tone.gain_db == pytest.approx(-9.0 - COURTESY_TONE_MARGIN_DB)

    # Opt-out (default) stays the exact legacy mono layout.
    legacy = build_verify_program(1600.0, sweep_s=1.0)
    assert [s.segment_id for s in legacy.segments] == ["guard", "sweep_verify", "tail"]


def _prelude_programs() -> dict[str, ExcitationProgram]:
    """One composed, prelude-carrying program per phase — the production shape
    each composer produces when the conductor asks for it."""
    return {
        "check": build_check_program(
            _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
        ),
        "measure": build_measure_program(
            _gain_plan(), _roles(),
            sweep_durations={"woofer": 0.6, "tweeter": 0.5},
            leading_pilot_gains_db=(-21.0, -11.0),
            courtesy_prelude=True,
        ),
        "verify": build_verify_program(
            1600.0, sweep_s=1.0, leading_pilot_gains_db=(-19.0, -9.0),
            courtesy_prelude=True,
        ),
        # The legacy pilot-less shape still splices in front of its sweep.
        "measure_no_pilots": build_measure_program(
            _gain_plan(), _roles(),
            sweep_durations={"woofer": 0.6, "tweeter": 0.5},
            courtesy_prelude=True,
        ),
    }


@pytest.mark.parametrize(
    "phase", ["check", "measure", "verify", "measure_no_pilots"],
)
def test_the_beeps_are_followed_by_the_settle_and_nothing_else(phase):
    """The §2.5 acceptance criterion, DERIVED from the composed schedule.

    "Three beeps, then quite a long gap, then the sweep" was the owner's
    hardware observation. The beeps mean "the measurement is imminent, go
    quiet now", so the interval from the last beep to the first audible
    measurement content must be the settle the owner specified — plus, since
    issue #1810, at most the pre-pilot ambient window, which is silence and
    has to be measured inside the quiet the beeps just asked for. Nothing
    else: no room-listening lead-in, no pilot pair, no pre-stimulus guard.

    Derived rather than asserted against a hand-computed number, so a composer
    that quietly reinstates a lead-in between the beeps and the stimulus fails
    here instead of silently lengthening the wait again.
    """
    prog = _prelude_programs()[phase]
    beep_end = max(
        s.start_sample + s.n_samples
        for s in prog.segments if s.kind == KIND_COURTESY_TONE
    )
    first_stimulus = min(
        s.start_sample for s in prog.segments
        if s.kind in STIMULUS_KINDS and s.start_sample >= beep_end
    )
    gap_s = (first_stimulus - beep_end) / prog.sample_rate_hz
    # The single bound every composed program must respect.
    assert gap_s <= COURTESY_MAX_BEEP_TO_STIMULUS_GAP_S + 1e-3
    # …and the exact value, derived from whether an ambient window sits
    # between the beeps and the first stimulus rather than hand-tabulated
    # per phase.
    windowed = any(
        s.segment_id == AMBIENT_SEGMENT_ID
        and beep_end <= s.start_sample < first_stimulus
        for s in prog.segments
    )
    expected = COURTESY_TONE_TRAILING_SILENCE_S + (
        PILOT_AMBIENT_WINDOW_S if windowed else 0.0
    )
    assert gap_s == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize(
    "phase", ["check", "measure", "verify", "measure_no_pilots"],
)
def test_no_audible_content_precedes_the_first_courtesy_beep(phase):
    """Issue #1812's missing pin — the rule the §2.5 move violated.

    PR #1771 moved the prelude to sit directly in front of the first SWEEP,
    on the premise that a leading pilot pair is inert lead-in. It is not: a
    pilot is a full-gain chirp, so MEASURE and VERIFY shipped a program whose
    first sound was two chirps and whose "quiet please" warning arrived ~4 s
    LATER, 6 dB quieter. Nothing in the suite caught it, because the only
    acceptance test measured the FORWARD interval (beeps → stimulus) and had
    nothing to say about what came before.

    This is the backward half. It fails on the pre-fix composition.
    """
    prog = _prelude_programs()[phase]
    beeps = [s for s in prog.segments if s.kind == KIND_COURTESY_TONE]
    assert beeps, "every program in this fixture carries a prelude"
    first_beep = min(s.start_sample for s in beeps)
    earlier_audible = [
        s.segment_id for s in prog.segments
        if s.kind in KNOWN_AUDIBLE_KINDS
        and s.kind != KIND_COURTESY_TONE
        and s.start_sample < first_beep
    ]
    assert earlier_audible == []


@pytest.mark.parametrize("phase", ["measure", "verify"])
def test_pilot_pair_is_preceded_by_the_ambient_window(phase):
    """Issue #1810: the window that makes the pilot SNR guard live.

    Before this, MEASURE/VERIFY programs opened on the pilots with no silence
    of their own, so ``program_analysis`` had no noise floor to measure them
    against — its in-band SNR was ``+inf`` by construction and the
    ``PILOT_MIN_SNR_DB`` guard on the linearity verdict could never fire.
    Pinned by id (the analysis looks the window up by
    ``AMBIENT_SEGMENT_ID``), by kind, by duration, and by position: it must
    sit immediately before the first pilot, in the quiet the beeps asked for.
    """
    prog = _prelude_programs()[phase]
    ambient = prog.segment(AMBIENT_SEGMENT_ID)
    assert ambient.kind == KIND_SILENCE
    assert ambient.n_samples == pytest.approx(
        PILOT_AMBIENT_WINDOW_S * PROGRAM_SAMPLE_RATE_HZ, abs=2,
    )
    first_pilot = min(
        (s for s in prog.segments if s.kind == KIND_PILOT),
        key=lambda s: s.start_sample,
    )
    assert ambient.start_sample + ambient.n_samples == first_pilot.start_sample
    # …and it follows the courtesy settle rather than preceding the beeps:
    # a floor measured before the "go quiet" warning is not the floor the
    # pilots play into.
    gap = prog.segment("courtesy_gap")
    assert gap.start_sample + gap.n_samples == ambient.start_sample


def test_pilotless_program_has_no_ambient_window():
    """The window exists to serve a pilot pair, so a program composed without
    one is byte-identical to the pre-#1810 shape — no silent lengthening of
    the phone's recording budget for a capture that cannot use it."""
    prog = _prelude_programs()["measure_no_pilots"]
    assert AMBIENT_SEGMENT_ID not in {s.segment_id for s in prog.segments}
    with pytest.raises(KeyError):
        prog.segment(AMBIENT_SEGMENT_ID)


@pytest.mark.parametrize(
    "reference_gain_db,margin_db,expected",
    [
        # Normal case: margin_db below the reference.
        (-10.0, 6.0, -16.0),
        (-40.0, 6.0, -46.0),
        # margin=0 -> exactly the reference (the "<=" clamp allows equality,
        # never strictly forces the tone quieter than its own formula).
        (-10.0, 0.0, -10.0),
        # A defensively-wrong negative margin must not push the tone LOUDER
        # than the reference -- the "clamp <= reference" backstop binds.
        (-10.0, -100.0, -10.0),
        # A (should-never-happen) positive reference must never yield a
        # positive tone gain -- the "never positive" backstop binds.
        (5.0, -10.0, 0.0),
        # Reference already at 0 dBFS: still clamps to margin below.
        (0.0, 6.0, -6.0),
    ],
)
def test_courtesy_tone_gain_db_clamp_property(reference_gain_db, margin_db, expected):
    got = courtesy_tone_gain_db(reference_gain_db, margin_db=margin_db)
    assert got == pytest.approx(expected)
    # The two invariants the issue names, restated as properties:
    assert got <= reference_gain_db + 1e-9
    assert got <= 1e-9


def test_courtesy_tone_gain_db_default_margin_matches_module_constant():
    assert courtesy_tone_gain_db(-20.0) == pytest.approx(-20.0 - COURTESY_TONE_MARGIN_DB)


def test_courtesy_tone_stimulus_shape_and_level():
    prog = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
    )
    tone_seg = prog.segment("courtesy_tone_ch0")
    pcm = courtesy_tone_stimulus(tone_seg)
    assert pcm.dtype == np.float32
    assert pcm.size == tone_seg.n_samples

    # COURTESY_TONE_BEEP_COUNT beeps separated by exact-zero gaps: probe the
    # MIDPOINT of each expected beep/gap window directly (a raw zero-crossing
    # edge count over-counts because the sine itself crosses zero every
    # half-cycle within a single beep).
    beep_n = int(round(COURTESY_TONE_BEEP_DURATION_S * PROGRAM_SAMPLE_RATE_HZ))
    gap_n = int(round(COURTESY_TONE_BEEP_GAP_S * PROGRAM_SAMPLE_RATE_HZ))
    cursor = 0
    beep_rms = []
    for i in range(COURTESY_TONE_BEEP_COUNT):
        window = pcm[cursor:cursor + beep_n]
        beep_rms.append(float(np.sqrt(np.mean(np.square(window)))))
        cursor += beep_n
        if i < COURTESY_TONE_BEEP_COUNT - 1:
            gap_window = pcm[cursor:cursor + gap_n]
            assert np.max(np.abs(gap_window)) == 0.0
            cursor += gap_n
    assert cursor == pcm.size
    expected_peak = 10.0 ** (tone_seg.gain_db / 20.0)
    # Each beep's RMS should be a healthy fraction of its peak (a genuine
    # sine tone, not near-silent) and consistent across all three beeps.
    for rms in beep_rms:
        assert rms > expected_peak * 0.5
    assert max(beep_rms) == pytest.approx(min(beep_rms), rel=1e-3)

    assert float(np.max(np.abs(pcm))) == pytest.approx(expected_peak, rel=0.02)


def test_courtesy_tone_stimulus_rejects_non_courtesy_segment():
    prog = build_check_program(_roles(), ambient_s=1.0, pilot_duration_s=0.5)
    with pytest.raises(ValueError):
        courtesy_tone_stimulus(prog.segment("pilot_woofer_lo"))


def test_render_pcm_places_courtesy_tone_per_channel_only():
    prog = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
    )
    pcm = render_program_pcm(prog)
    tone0 = prog.segment("courtesy_tone_ch0")
    tone1 = prog.segment("courtesy_tone_ch1")
    window0 = pcm[tone0.start_sample:tone0.start_sample + tone0.n_samples]
    # ch0 carries the tone, ch1 stays silent during ch0's tone window --
    # confirms no cross-channel leakage even though both tones start at 0.
    assert np.max(np.abs(window0[:, 0])) > 0.0
    # (ch1 has its own, equal-length tone starting at the same sample, so
    # comparing against tone1's own window instead of asserting ch1 silence.)
    window1 = pcm[tone1.start_sample:tone1.start_sample + tone1.n_samples]
    assert np.max(np.abs(window1[:, 1])) > 0.0
    # The trailing silence gap is genuinely silent on every channel.
    gap = prog.segment("courtesy_gap")
    gap_window = pcm[gap.start_sample:gap.start_sample + gap.n_samples]
    assert np.max(np.abs(gap_window)) == 0.0


def test_courtesy_prelude_program_id_differs_from_legacy():
    legacy = build_check_program(_roles(), ambient_s=1.0, pilot_duration_s=0.5)
    prelude = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
    )
    assert legacy.program_id != prelude.program_id


def test_program_manifest_json_round_trip_with_courtesy_prelude():
    prog = build_measure_program(_gain_plan(), _roles(), courtesy_prelude=True)
    blob = json.dumps(prog.to_dict())
    restored = ExcitationProgram.from_dict(json.loads(blob))
    assert restored == prog
    assert restored.program_id == prog.program_id
    assert restored.segment("courtesy_tone_ch0").kind == KIND_COURTESY_TONE


def test_known_audible_segments_includes_courtesy_tone_excludes_silence():
    prog = build_check_program(
        _roles(), ambient_s=1.0, pilot_duration_s=0.5, courtesy_prelude=True,
    )
    known = prog.known_audible_segments()
    kinds = {s.kind for s in known}
    assert KIND_COURTESY_TONE in kinds
    assert KIND_SILENCE not in kinds
    # Superset of stimulus_segments() by exactly the two courtesy-tone segments.
    assert set(prog.stimulus_segments()) < set(known)
    assert set(known) - set(prog.stimulus_segments()) == {
        prog.segment("courtesy_tone_ch0"), prog.segment("courtesy_tone_ch1"),
    }


def test_write_program_wav_with_courtesy_prelude(tmp_path):
    from scipy.io import wavfile

    prog = build_measure_program(
        _gain_plan(), _roles(), sweep_durations={"woofer": 0.6, "tweeter": 0.5},
        courtesy_prelude=True,
    )
    path = tmp_path / "measure_prelude.wav"
    write_program_wav(path, prog)
    rate, data = wavfile.read(str(path))
    assert rate == PROGRAM_SAMPLE_RATE_HZ
    assert data.shape == (prog.total_samples, prog.channels)
    tone0 = prog.segment("courtesy_tone_ch0")
    window = data[tone0.start_sample:tone0.start_sample + tone0.n_samples, 0]
    assert np.max(np.abs(window)) > 0


def test_worst_case_measure_with_prelude_stays_under_capture_wav_cap():
    """Pin the 5 MiB capture-upload budget against the composed program.

    MEASURE is the longest capture; the #1668 N=3 repeats and the #1677
    courtesy prelude each silently spend headroom against
    ``CROSSOVER_CAPTURE_MAX_WAV_BYTES``.  Render the production worst-case
    shape (leading pilots + prelude + N=3 default) and assert the capture-side
    mono 16-bit byte count stays under the cap with real margin, so the next
    duration-growing change fails HERE instead of at upload time.
    """
    from jasper.active_speaker.test_signal_plan import CROSSOVER_CAPTURE_MAX_WAV_BYTES

    program = build_measure_program(
        _gain_plan(), _roles(),
        leading_pilot_gains_db=(-6.0, -6.0), leading_pilot_role="woofer",
        courtesy_prelude=True,
    )
    # Capture-side WAV: mono 16-bit, program length + the relay capture-plan
    # margin (2000 ms — lockstep with crossover_v2_flow.CAPTURE_ENTRY_MARGIN_MS;
    # mirrored, not imported, per the audio_measurement layering boundary).
    margin_samples = int(round(2.0 * program.sample_rate_hz))
    wav_bytes = 44 + 2 * (program.total_samples + margin_samples)
    assert wav_bytes < CROSSOVER_CAPTURE_MAX_WAV_BYTES
    assert CROSSOVER_CAPTURE_MAX_WAV_BYTES - wav_bytes > 512 * 1024


# --------------------------------------------------------------------------- #
# one vocabulary per question: stimulus phase vs journey phase (ticket 2.9)
# --------------------------------------------------------------------------- #


def _phase_names(module) -> set[str]:
    """Every module-level name carrying the word PHASE.

    A NAME scan, deliberately, and not a value scan: what collided was the
    SPELLING, and a value scan structurally cannot see it — the values are
    identical on purpose and must stay that way (the second test below).
    """
    return {name for name in vars(module) if "PHASE" in name}


def test_program_phase_names_stay_disjoint_from_journey_phase_names():
    """The two PHASE families must never spell a name the same way again.

    ``jasper.audio_measurement.program`` answers *which composer built this
    stimulus* (three phases). ``jasper.active_speaker.crossover_v2.journey``
    answers *where is the round in its walk* (eleven — ``PHASE_LATERAL``,
    ``PHASE_REVIEW``, ``PHASE_DONE``, …). Until master-plan ticket 2.9 both
    spelled ``PHASE_CHECK`` / ``PHASE_MEASURE`` / ``PHASE_VERIFY`` — identical
    names AND identical string values for different concepts — so an import
    site could take the wrong family and still typecheck, run, and agree.
    ``jasper.web.correction_crossover_v2_relay`` is the one production file
    that imports from BOTH (one stimulus name, ``PROGRAM_PHASE_CHECK``; two
    journey ones, ``PHASE_APPLYING`` and ``PHASE_DONE``), and is where a wrong
    pick would have been read first.

    **No positive control here, deliberately.** A name-set intersection can
    pass vacuously — if either module moved or lost its constants, both sets
    would come back empty and this would still be green. What rules that out
    is the test BELOW, which dereferences ``program.PROGRAM_PHASE_CHECK`` and
    ``journey.PHASE_CHECK`` by attribute: a moved module or a dropped constant
    hard-fails there with ``ImportError`` / ``AttributeError``. The two tests
    are a pair, and that is why this one needs no fixture of its own — do not
    "repair" the missing control by weakening either half.

    **The honest bound:** this compares NAMES on the two modules, so a third
    surface answering either question — under other names, or with bare
    ``"check"`` / ``"verify"`` literals — is invisible to it. What it catches
    is the collision returning where it actually lived.
    """
    from jasper.active_speaker.crossover_v2 import journey
    from jasper.audio_measurement import program

    collisions = _phase_names(program) & _phase_names(journey)
    assert not collisions, (
        f"PHASE vocabularies collided again: {sorted(collisions)} is declared "
        "by BOTH jasper.audio_measurement.program (the stimulus phase) and "
        "jasper.active_speaker.crossover_v2.journey (the session phase). Give "
        "one family a distinguishing prefix — and do NOT resolve it by "
        "changing a string value: both value sets are banked (see the note "
        "above PROGRAM_PHASE_CHECK)."
    )


def test_program_and_journey_phase_values_stay_identical():
    """The two families share three VALUES on purpose — neither may change.

    This is the other half of the rename, and it is a tripwire rather than a
    tidiness pin: the obvious way to "finish" ticket 2.9 is to make the string
    values differ too, and that would break banked data on BOTH sides.

    * **Stimulus side.** ``phase`` is hashed into ``program_id``
      (:func:`~jasper.audio_measurement.program._program_id`) and serialized by
      :meth:`~jasper.audio_measurement.program.ExcitationProgram.to_dict`;
      ``from_dict`` / ``__post_init__`` refuse a phase outside
      ``PROGRAM_PHASES``. A new value would re-fingerprint every program — and
      ``program_id`` equality IS #2291's before→after benefit check — and would
      stop every banked program JSON from loading.
    * **Journey side.** The values land in ``session_phases`` /
      ``accepted_phases`` in the on-disk flow state
      (``correction_crossover_v2.DEFAULT_V2_STATE_PATH``), which
      ``_phase_from_state`` reads back.

    So if this fails, the question is not "which spelling is nicer" but "what
    migrates the artifacts already on disk".
    """
    from jasper.active_speaker.crossover_v2 import journey
    from jasper.audio_measurement import program

    assert program.PROGRAM_PHASE_CHECK == journey.PHASE_CHECK
    assert program.PROGRAM_PHASE_MEASURE == journey.PHASE_MEASURE
    assert program.PROGRAM_PHASE_VERIFY == journey.PHASE_VERIFY
