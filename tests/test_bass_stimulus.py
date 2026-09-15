# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from dataclasses import replace
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker.angle_capture import request_for_program
from jasper.active_speaker.bass_stimulus import build_bass_program
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.crossover_v2.programs import SessionExcitation
from jasper.active_speaker.crossover_v2.capture_plan import build_inline_session_spec
from jasper.active_speaker.measurement_analysis import analyzed_measurements
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ, bass_take
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.measurement_programs import load_programs, program, run_program, validated_capture_purpose
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_admission import ProgramAdmissionRefusal, readmit_summed_program_from_wav
from jasper.audio_measurement.distortion import segment_sweep_meta
from jasper.audio_measurement.program import KIND_SUMMED_SWEEP, render_program_pcm, write_program_wav
from jasper.audio_measurement.repeated_sweep import average_summed_capture, sweep_ambient_id
from jasper.audio_measurement.sweep_levels import SweepNoiseWindowError, require_sweep_noise_window, sweep_band_levels
from jasper.web.correction_run_host import compose_plan_program
from tests.test_active_speaker_audition import ACTIVE_PCM, _applied_profile
from tests.test_active_speaker_program_admission import _profile_and_targets, _roles
from tests.active_speaker_fixtures import isolated_candidate_bank as isolated_candidate_bank

pytestmark = pytest.mark.usefixtures("isolated_candidate_bank")


@pytest.fixture
def bass_fixture():
    topology, safety, targets = _profile_and_targets(
        woofer_floor=20, woofer_upper=4000, woofer_peak=-8, tweeter_peak=-8, max_sweep_duration_s=4,
        minimum_cooldown_s=2,
    )
    excitation = SessionExcitation(tuple(_roles((20, 4000), (1600, 20000))),
                                   {"woofer": -8, "tweeter": -8}, -20, 1600,
                                   {"woofer": 4, "tweeter": 4}, (20, 20000))
    return topology, safety, targets, excitation


def _bass(fixture, **kwargs):
    _, safety, targets, excitation = fixture
    return build_bass_program(excitation, program("bass").stimulus,
                              safety_profile=safety, role_targets=targets, **kwargs)


def test_registry_stimulus_reaches_the_capture_spec():
    rows = load_programs().values()
    bass = [row for row in rows if row.purpose == "bass"]
    assert {row.layout for row in bass} == {"bass_axis", "seat_cloud", "room_quick", "bass_nearfield"}
    for row in rows:
        assert (row.stimulus is not None) == (row.purpose == "bass")
    for row in bass:
        assert row.stimulus == {"band_hz": ["woofer_floor", 1100.0], "passes": "max_repeat_count", "ambient": "derived"}
        request = request_for_program(row, mover=row.mover or "human", candidates=("trial",))
        assert all(capture.spec.stimulus == row.stimulus for capture in prepare_plan_captures(request))
    near = run_program("bass", "bass_nearfield")
    assert (near.regime, near.mover, near.capture_count) == ("near_field", "human", 1)
    capture, = prepare_plan_captures(request_for_program(near, candidates=("trial",)))
    assert (capture.spec.regime, capture.stop.kind, capture.stop.distance_m) == ("near_field", "close", 0.03)


@pytest.mark.parametrize("regime,allowed", [("summed", True), ("near_field", True), ("branches", False), ("per_driver", False), ("reference_axis", False)])
def test_bass_capture_regimes(regime, allowed):
    if allowed:
        assert validated_capture_purpose("bass", "close", regime) == "bass"
    else:
        with pytest.raises(ValueError):
            validated_capture_purpose("bass", "close", regime)


@pytest.mark.parametrize("floor", [20, 30])
def test_bass_schedule_fits_caps_and_noise_windows(bass_fixture, floor):
    next(target for target in bass_fixture[1]["targets"] if target["role"] == "woofer")["hard_excitation_band_hz"][0] = floor
    bass = _bass(bass_fixture)
    pcm = render_program_pcm(bass)[:, 0]
    sweeps = [s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP]
    assert len(sweeps) == 3
    for index, sweep in enumerate(sweeps):
        assert 0 < sweep.n_samples / bass.sample_rate_hz <= 4
        assert (sweep.f1_hz, sweep.f2_hz) == (floor, 1100)
        quiet = bass.segment(sweep_ambient_id(sweep.segment_id))
        assert quiet.start_sample + quiet.n_samples == sweep.start_sample
        assert quiet.n_samples >= bass.sample_rate_hz
        if index:
            previous = sweeps[index - 1]
            gap = sweep.start_sample - previous.start_sample - previous.n_samples
            assert gap >= max(2 * bass.sample_rate_hz, quiet.n_samples)
            assert np.count_nonzero(pcm[previous.start_sample + previous.n_samples:sweep.start_sample]) == 0
        assert np.array_equal(pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples],
                              pcm[sweeps[0].start_sample:sweeps[0].start_sample + sweep.n_samples])
        meta = segment_sweep_meta(sweep)
        noise = np.random.default_rng(index).normal(0, 1e-5, quiet.n_samples)
        bands = sweep_band_levels(pcm, noise, bass.sample_rate_hz, meta, sweep.start_sample, BASS_BANDS_HZ)
        assert len(bands) == sum(lo >= floor for lo, _ in BASS_BANDS_HZ)
        assert min(row["quiet_windows"] for row in bands) >= 5
        with pytest.raises(SweepNoiseWindowError) as caught:
            require_sweep_noise_window(meta, bass.sample_rate_hz, BASS_BANDS_HZ, 1)
        assert caught.value.code == "sweep_noise_window_too_short"


def test_nearfield_capture_budget_contains_the_played_program(bass_fixture):
    _, safety, targets, excitation = bass_fixture
    request = request_for_program(program("bass", "nearfield"), candidates=("trial",))
    capture, = prepare_plan_captures(request)
    context = SimpleNamespace(safety_profile=safety, role_targets=targets)
    played = compose_plan_program(SimpleNamespace(_excitation=excitation), capture.spec, None, context=context)
    plan = build_inline_session_spec(
        [(capture.spec, capture.resolved(request).prompt, "trial")],
        roles_bands=excitation.roles, fc_hz=excitation.fc_hz, safety_profile=safety, role_targets=targets,
        acknowledgement_binding="a" * 32, retries_per_pose=0,
    ).capture_plan
    assert plan.capture_target == 1
    assert plan.entries[0].duration_ms >= played.total_samples * 1000 / played.sample_rate_hz


@pytest.mark.parametrize("passes", [2, 3])
def test_coherent_noise_gain_and_replayed_fundamental(bass_fixture, passes, tmp_path, monkeypatch):
    _, safety, _, _ = bass_fixture
    for target in safety["targets"]:
        target["level_duration_limits"]["max_repeat_count"] = passes
    bass = _bass(bass_fixture)
    rate = bass.sample_rate_hz
    delay = 800
    pcm = render_program_pcm(bass)[:, 0]
    raw = np.pad(pcm.astype(np.float64) * 0.1, (delay, rate))
    raw += np.random.default_rng(19).normal(0, 0.015, raw.size)
    original = raw.copy()
    averaged = average_summed_capture(bass, raw, delay)
    assert np.array_equal(raw, original)
    sweep = bass.segment("sweep_verify")
    quiet = bass.segment(sweep_ambient_id(sweep.segment_id))
    start = delay + sweep.start_sample
    expected = np.mean([raw[delay + s.start_sample:delay + s.start_sample + s.n_samples]
                        for s in bass.segments if s.kind == KIND_SUMMED_SWEEP], axis=0)
    assert averaged[start:start + sweep.n_samples] == pytest.approx(expected, abs=1e-12)
    rows = [sweep_band_levels(samples, samples[start - quiet.n_samples:start], rate,
                             segment_sweep_meta(sweep), start, BASS_BANDS_HZ) for samples in (raw, averaged)]
    gain = np.median([b["estimated_snr_db"] - a["estimated_snr_db"] for a, b in zip(*rows)])
    assert gain == pytest.approx(10 * math.log10(passes), abs=1.5)
    wav = tmp_path / "capture.wav"
    wavfile.write(wav, rate, raw.astype(np.float32))
    record = {"program": bass.to_dict(), "graph_scope": "candidate", "candidate_id": "trial"}
    monkeypatch.setattr("jasper.active_speaker.measurement_analysis.reopen_measurement_capture", lambda *_: (record, wav.read_bytes()))
    take, = analyzed_measurements(tmp_path, paths=["capture"])
    result = bass_take(take)
    assert len(result["passes"]) == passes
    residuals = [loc.located_start - loc.scheduled_start for loc in take.analysis.locations if loc.kind == KIND_SUMMED_SWEEP]
    assert max(residuals) - min(residuals) <= 1
    frequencies = np.array(result["frequency_curve"]["freqs_hz"])
    reference = (frequencies >= 300) & (frequencies <= 1000)
    assert np.median(np.array(result["frequency_curve"]["magnitude_db"])[reference]) == pytest.approx(-20, abs=0.3)


@pytest.mark.parametrize("too_long", [False, True])
def test_bass_admission_keeps_each_role_cap(bass_fixture, tmp_path, too_long):
    topology, safety, targets, excitation = bass_fixture
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    graph = compile_tuning_graph(MeasurementGraphProfile(preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM),
                                 candidate=candidate_from_applied_profile(topology, applied))
    programs = [excitation.verify_program(), _bass(bass_fixture)]
    if too_long:
        programs[-1] = replace(excitation, summed_sweep_band_hz=(20, 1100), sweep_duration_limits_s={}).verify_program(sweep_s=5)
    for index, stimulus in enumerate(programs):
        wav = tmp_path / f"program-{index}.wav"
        write_program_wav(wav, stimulus)
        admission = readmit_summed_program_from_wav(stimulus, wav, graph_yaml=graph, topology=topology,
                                                   safety_profile=safety, role_targets=targets,
                                                   session_volume_db=excitation.session_volume_db)
        assert admission.allowed is not (too_long and index == 1), admission.to_dict()
        if too_long and index == 1:
            assert admission.refusals == (ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS,)
        else:
            assert all(segment.execution_allowed for segment in admission.segments if segment.role == "tweeter")
