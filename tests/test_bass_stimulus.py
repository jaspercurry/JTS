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
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec, stubbed_capabilities
from jasper.cli.measure import _bind_compose
from jasper.active_speaker.crossover_v2.capture_plan import build_inline_session_spec
from jasper.active_speaker.measurement_analysis import analyzed_measurements
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ, bass_take
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.measurement_programs import load_programs, program, run_program, validated_capture_purpose
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_admission import ProgramAdmissionRefusal, readmit_summed_program_from_wav
from jasper.audio_measurement.distortion import required_pre_guard_s, segment_sweep_meta
from jasper.audio_measurement.program import KIND_SUMMED_SWEEP, _finalize, render_program_pcm, write_program_wav
from jasper.audio_measurement.program_analysis import analyze_program_capture
from jasper.audio_measurement.repeated_sweep import average_summed_capture, repeat_summed_program, summed_alignment_limit_samples, sweep_ambient_id
from jasper.audio_measurement.sweep_levels import sweep_band_levels
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
        assert row.stimulus == {"ceiling_hz": 1100.0}
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
        assert quiet.n_samples >= math.ceil(required_pre_guard_s(meta) * bass.sample_rate_hz)


@pytest.mark.parametrize("size", ["axis", "nearfield"])
def test_bass_capture_program_agrees_across_surfaces(bass_fixture, monkeypatch, size):
    topology, safety, targets, excitation = bass_fixture
    row = program("bass", size)
    request = request_for_program(row, mover=row.mover, candidates=("trial",))
    capture, = prepare_plan_captures(request)
    context = SimpleNamespace(safety_profile=safety, role_targets=targets)
    played = compose_plan_program(SimpleNamespace(_excitation=excitation), capture.spec, None, context=context)
    box = SimpleNamespace(topology=topology, safety_profile=safety, role_targets=targets,
                          roles_bands=excitation.roles, caps_dbfs=excitation.caps_dbfs,
                          session_volume_db=excitation.session_volume_db, fc_hz=excitation.fc_hz,
                          sweep_duration_limits_s=excitation.sweep_duration_limits_s, declared_sensitivities={})
    monkeypatch.setattr("jasper.active_speaker.crossover_v2.composition.bind_program_composer",
                        lambda **kw: kw["program_for_spec"])
    compose = _bind_compose(box=box, store=None, session_id="test", cam_factory=None,
                            config_dir="", graph=SimpleNamespace(installed_graph_yaml=None))
    assert compose(capture.spec, None).program_id == played.program_id
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
    averaged = average_summed_capture(bass, raw, delay,
                                     {s.segment_id: delay + s.start_sample for s in bass.segments})
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


@pytest.mark.parametrize("fault,refusal", [
    (None, None), ("duration", ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS),
    ("repeats", ProgramAdmissionRefusal.REPEAT_COUNT_OVER_CAP),
    ("cooldown", ProgramAdmissionRefusal.COOLDOWN_BELOW_MINIMUM),
])
def test_bass_admission_keeps_each_role_cap(bass_fixture, tmp_path, fault, refusal):
    topology, safety, targets, excitation = bass_fixture
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    graph = compile_tuning_graph(MeasurementGraphProfile(preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM),
                                 candidate=candidate_from_applied_profile(topology, applied))
    programs = [excitation.verify_program(), _bass(bass_fixture)]
    if fault == "duration":
        programs[-1] = replace(excitation, summed_sweep_band_hz=(20, 1100), sweep_duration_limits_s={}).verify_program(sweep_s=5)
    elif fault in ("repeats", "cooldown"):
        single = replace(excitation, summed_sweep_band_hz=(20, 1100)).verify_program()
        programs[-1] = repeat_summed_program(single, passes=4 if fault == "repeats" else 3,
                                             quiet_samples=48000, cooldown_s=2 if fault == "repeats" else 0)
    for index, stimulus in enumerate(programs):
        wav = tmp_path / f"program-{index}.wav"
        write_program_wav(wav, stimulus)
        admission = readmit_summed_program_from_wav(stimulus, wav, graph_yaml=graph, topology=topology,
                                                   safety_profile=safety, role_targets=targets,
                                                   session_volume_db=excitation.session_volume_db)
        assert admission.allowed is not (fault is not None and index == 1), admission.to_dict()
        if fault is not None and index == 1:
            assert admission.refusals == (refusal,)
        else:
            assert all(segment.execution_allowed for segment in admission.segments if segment.role == "tweeter")


@pytest.mark.parametrize("fault,code", [
    ("drift", "summed_pass_arrival_drift"),
    ("truncated", "summed_pass_capture_incomplete"),
    ("mismatch", "summed_pass_shape_mismatch"),
])
def test_unusable_passes_reach_capture_integrity(bass_fixture, tmp_path, monkeypatch, fault, code):
    bass = _bass(bass_fixture)
    if fault == "mismatch":
        bass = _finalize(bass.phase, bass.channels, [
            replace(s, gain_db=s.gain_db - 1) if s.segment_id == "sweep_verify_repeat_1" else s
            for s in bass.segments
        ], bass.total_samples)
    rate, delay = bass.sample_rate_hz, 800
    pcm = render_program_pcm(bass)[:, 0].astype(np.float64) * 0.1
    raw = np.pad(pcm, (delay, rate))
    if fault == "drift":
        for index, sweep in enumerate(s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP):
            start = delay + sweep.start_sample
            raw[start:start + sweep.n_samples] = 0
            start += index * round(0.0005 * rate)
            raw[start:start + sweep.n_samples] = pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples]
    elif fault == "truncated":
        raw = raw[:delay + bass.total_samples - rate // 10]
    wav = tmp_path / "capture.wav"
    wavfile.write(wav, rate, raw.astype(np.float32))
    record = {"program": bass.to_dict(), "graph_scope": "candidate", "candidate_id": "trial"}
    monkeypatch.setattr("jasper.active_speaker.measurement_analysis.reopen_measurement_capture", lambda *_: (record, wav.read_bytes()))
    take, = analyzed_measurements(tmp_path, paths=["capture"])
    assert code in take.analysis.capture_integrity.failed
    assert np.array_equal(take.samples, raw.astype(np.float32))
    located = {loc.segment_id: loc.located_start for loc in take.analysis.locations}
    assert average_summed_capture(bass, raw, delay, located) is raw
    assert not any(bass_take(take)["fundamental_qualified"])
    if fault == "drift":
        checks = {c.name: c.status for c in take.analysis.capture_integrity.checks}
        assert checks["repeat_epsilon"] == "pass"
        assert checks["repeat_level_agreement"] == "pass"
        assert checks["within_role_desync"] == "pass"


@pytest.mark.parametrize("spread,averaged", [(0, True), (1, True), (2, False)])
def test_coherence_bound_comes_from_the_ceiling(bass_fixture, spread, averaged):
    bass = _bass(bass_fixture)
    assert summed_alignment_limit_samples(bass) == 1
    raw = render_program_pcm(bass)[:, 0]
    located = {s.segment_id: s.start_sample for s in bass.segments}
    located["sweep_verify_repeat_2"] += spread
    assert (average_summed_capture(bass, raw, 0, located) is not raw) == averaged


@pytest.mark.parametrize("scope,expected", [("drivers", ("near_field_splice_not_implemented",)), ("candidate", ())])
def test_nearfield_splice_stub_only_applies_to_driver_captures(scope, expected):
    spec = MeasureSpec(kind="baseline", graph_scope=scope, regime="near_field",
                       candidate_id="trial" if scope == "candidate" else "")
    assert tuple(stub.code for stub in stubbed_capabilities(spec)) == expected


@pytest.mark.parametrize("passes,fault,check,status", [
    (3, None, "repeat_epsilon", "pass"),
    (3, "epsilon", "repeat_epsilon", "fail"),
    (3, "level", "repeat_level_agreement", "fail"),
    (3, "step", "within_role_desync", "fail"),
    (3, None, "discontinuity_step", "not_evaluated"),
    (5, None, "discontinuity_step", "pass"),
    (5, "step", "discontinuity_step", "fail"),
])
def test_verify_repeat_checks_use_the_captured_passes(bass_fixture, passes, fault, check, status):
    excitation = replace(bass_fixture[3], summed_sweep_band_hz=(20, 1100))
    bass = repeat_summed_program(excitation.verify_program(), passes=passes, quiet_samples=96000, cooldown_s=2)
    rate, delay = bass.sample_rate_hz, 800
    pcm = render_program_pcm(bass)[:, 0].astype(np.float64) * 0.1
    raw = np.pad(pcm, (delay, rate))
    for index, sweep in enumerate(s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP):
        start = delay + sweep.start_sample
        raw[start:start + sweep.n_samples] = 0
        if fault == "epsilon":
            start += index * round(0.006 * rate)
        elif fault == "step" and index >= passes // 2:
            start += round(0.0005 * rate)
        gain = 10 ** (1 / 20) if fault == "level" and index == passes - 1 else 1
        raw[start:start + sweep.n_samples] = gain * pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples]
    analysis = analyze_program_capture(bass, raw, rate)
    assert next(c.status for c in analysis.capture_integrity.checks if c.name == check) == status
