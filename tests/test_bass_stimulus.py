# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

from dataclasses import asdict, replace
import json
import math
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker.angle_capture import request_for_program
from jasper.active_speaker.bass_stimulus import build_bass_program
from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
from jasper.active_speaker.crossover_v2.capture_dispatch import assess
from jasper.active_speaker.crossover_v2.programs import SessionExcitation
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec, stubbed_capabilities
from jasper.cli.measure import _bind_compose
from jasper.active_speaker.crossover_v2.capture_plan import CAPTURE_ENTRY_MARGIN_MS, build_inline_session_spec
from jasper.active_speaker.excitation_safety_plan import resolve_driver_excitation_ceilings
from jasper.active_speaker.measurement_analysis import analyzed_measurements
from jasper.active_speaker.measurement_bass import BASS_BANDS_HZ, bass_take
from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
from jasper.active_speaker.measurement_programs import gate_exemption, load_programs, program, run_program, validated_capture_purpose
from jasper.active_speaker.plan_run import prepare_plan_captures
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.program_admission import ProgramAdmissionRefusal, readmit_summed_program_from_wav
from jasper.audio_measurement.distortion import required_pre_guard_s, segment_sweep_meta
from jasper.audio_measurement.program import KIND_PILOT, KIND_SUMMED_SWEEP, _finalize, render_program_pcm, write_program_wav
from jasper.audio_measurement.program_analysis import MeasurementGeometry, SWEEP_SCHEDULE_RESIDUAL_CEILING_MS, analyze_program_capture
from jasper.audio_measurement.quality_model import DRIVER
from jasper.audio_measurement.repeated_sweep import align_summed_capture, average_summed_capture, repeat_summed_program, sweep_ambient_id
from jasper.audio_measurement.sweep_levels import sweep_band_levels
from jasper.audio_measurement.wired_capture import ZERO_RUN_MIN_SAMPLES
from jasper.web.correction_run_host import compose_plan_program
from tests.crossover_v2_fixtures import plan_context
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


def test_an_undeclared_cooldown_refuses_the_bass_stimulus(bass_fixture):
    """The cooldown comes from the shared resolver, which refuses rather than
    returning a zero. That refusal has to keep arriving as this builder's own
    code, not as the resolver's."""
    from copy import deepcopy

    from jasper.active_speaker.bass_stimulus import BassStimulusRefused

    _topology, safety, targets, excitation = bass_fixture
    broken = deepcopy(safety)
    for target in broken["targets"]:
        target["level_duration_limits"].pop("minimum_cooldown_s")
    with pytest.raises(BassStimulusRefused) as refused:
        build_bass_program(excitation, program("bass").stimulus,
                           safety_profile=broken, role_targets=targets)
    assert refused.value.code == "bass_stimulus_caps_missing"


def _replay(bass, raw, tmp_path, monkeypatch):
    wav = tmp_path / "capture.wav"
    wavfile.write(wav, bass.sample_rate_hz, raw.astype(np.float32))
    record = {"program": bass.to_dict(), "graph_scope": "candidate", "candidate_id": "trial"}
    monkeypatch.setattr("jasper.active_speaker.measurement_analysis.reopen_measurement_capture", lambda *_: (record, wav.read_bytes()))
    return next(analyzed_measurements(tmp_path, paths=["capture"]))


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
    assert (len(sweeps), sum(s.kind == KIND_PILOT for s in bass.segments)) == (3, 0)
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


def test_bass_program_charges_rear_target_caps():
    """A rear-declared box builds the bass program, and the rear target's
    declared repeat and cooldown caps bind the schedule."""
    topology, safety, targets = _profile_and_targets(
        rear=True, woofer_floor=20, woofer_upper=4000, woofer_peak=-8, tweeter_peak=-8,
        max_sweep_duration_s=4, minimum_cooldown_s=2,
    )
    assert set(targets) == {"woofer", "tweeter", "woofer:rear"}
    rear_limits = next(
        t for t in safety["targets"] if t["target_fingerprint"] == targets["woofer:rear"]
    )["level_duration_limits"]
    rear_limits["max_repeat_count"] = 2
    rear_limits["minimum_cooldown_s"] = 5
    excitation = SessionExcitation(tuple(_roles((20, 4000), (1600, 20000))),
                                   {"woofer": -8, "tweeter": -8}, -20, 1600,
                                   {"woofer": 4, "tweeter": 4}, (20, 20000))
    bass = _bass((topology, safety, targets, excitation))
    sweeps = [s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP]
    # min(builder max 3, front 3, front 3, rear 2) == 2.
    assert len(sweeps) == 2
    for previous, sweep in zip(sweeps, sweeps[1:]):
        gap = sweep.start_sample - previous.start_sample - previous.n_samples
        quiet = bass.segment(sweep_ambient_id(sweep.segment_id))
        # rear cooldown (5) > front (2).
        assert gap >= max(5 * bass.sample_rate_hz, quiet.n_samples)


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
    assert plan.entries[0].duration_ms == 20199 + CAPTURE_ENTRY_MARGIN_MS


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
    take = _replay(bass, raw, tmp_path, monkeypatch)
    anchor = take.analysis.anchor
    assert (anchor.anchor, anchor.witness) == ("sweep_verify", "sweep_verify_repeat_1")
    assert anchor.shift_ms == pytest.approx(delay / rate * 1000, abs=0.5)
    assert (anchor.corroborated, anchor.ambiguous, take.analysis.pilots, take.analysis.pilot_snr_ok) == (True, False, (), None)
    assert assess(take.analysis, phase="verify", program=bass).screens == []
    result = bass_take(take)
    assert len(result["passes"]) == passes
    frequencies = np.array(result["frequency_curve"]["freqs_hz"])
    reference = (frequencies >= 300) & (frequencies <= 1000)
    assert np.median(np.array(result["frequency_curve"]["magnitude_db"])[reference]) == pytest.approx(-20, abs=0.3)


@pytest.mark.parametrize("fault,refusal", [
    (None, None), ("duration", ProgramAdmissionRefusal.SEGMENT_OUTSIDE_LIMITS),
    ("repeats", ProgramAdmissionRefusal.REPEAT_COUNT_OVER_CAP),
    ("cooldown", ProgramAdmissionRefusal.COOLDOWN_BELOW_MINIMUM),
])
def test_bass_admission_keeps_jts3_role_caps(bass_fixture, tmp_path, fault, refusal):
    topology, safety, targets = _profile_and_targets(
        woofer_floor=20, woofer_upper=4000, woofer_peak=-8, tweeter_peak=-65,
        max_sweep_duration_s=4, minimum_cooldown_s=2)
    declared = {"woofer": 83.3, "tweeter": 108.5}
    excitation = replace(bass_fixture[3], caps_dbfs={r: resolve_driver_excitation_ceilings(
        safety, t, program_admission=True, declared_sensitivities=declared)[1] for r, t in targets.items()})
    applied = _applied_profile(topology)
    preset = ActiveSpeakerPreset.from_mapping(applied["recomposition_snapshot"]["preset"])
    graph = compile_tuning_graph(MeasurementGraphProfile(preset, topology, {"woofer": 0, "tweeter": 1}, ACTIVE_PCM),
                                 candidate=candidate_from_applied_profile(topology, applied))
    programs = [excitation.verify_program(), _bass((topology, safety, targets, excitation))]
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
                                                   session_volume_db=excitation.session_volume_db, declared_sensitivities=declared)
        assert admission.allowed is not (fault is not None and index == 1), admission.to_dict()
        assert admission.refusals == ((refusal,) if fault is not None and index == 1 else ())
        if admission.allowed:
            assert all(segment.execution_allowed for segment in admission.segments if segment.role == "tweeter")


@pytest.mark.parametrize("fault,code", [
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
    if fault == "truncated":
        raw = raw[:delay + bass.total_samples - rate // 10]
    take = _replay(bass, raw, tmp_path, monkeypatch)
    assert code in take.analysis.capture_integrity.failed
    assert np.array_equal(take.samples, raw.astype(np.float32))
    assert average_summed_capture(bass, raw, delay) is raw
    assert not any(bass_take(take)["fundamental_qualified"])


@pytest.mark.parametrize("drift_us", [-500, 500])
def test_pass_alignment_preserves_the_fundamental(bass_fixture, tmp_path, monkeypatch, drift_us):
    bass = _bass(bass_fixture)
    rate, delay = bass.sample_rate_hz, 800
    pcm = render_program_pcm(bass)[:, 0].astype(np.float64) * 0.1
    clean = np.pad(pcm, (delay, rate))
    raw = clean.copy()
    offsets = {}
    for index, sweep in enumerate(s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP):
        start = delay + sweep.start_sample
        raw[start:start + sweep.n_samples] = 0
        offsets[sweep.segment_id] = index * round(drift_us * 1e-6 * rate)
        start += offsets[sweep.segment_id]
        raw[start:start + sweep.n_samples] = pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples]
    take = _replay(bass, raw, tmp_path, monkeypatch)
    integrity = take.analysis.capture_integrity
    evidence = integrity.to_dict()
    assert evidence["pass_alignment"] == "correlated"
    assert evidence["pass_offsets_samples"] == offsets
    assert evidence["pass_alignment_residual_spread_samples"] == pytest.approx(0, abs=0.01)
    assert not integrity.failed
    view = bass_take(take)
    assert all(view["diagnostics"][key] == value for key, value in integrity.pass_alignment.to_dict().items())
    assert {row["segment_id"]: row["offset_samples"] for row in view["passes"]} == offsets
    assert all(row["pass_alignment"] == "correlated" and row["residual_spread_samples"] < 0.01 for row in view["passes"])
    baseline = analyze_program_capture(bass, clean, rate).summed_response
    reference = (baseline.freqs_hz >= 300) & (baseline.freqs_hz <= 1000)
    assert np.median(take.analysis.summed_response.magnitude_db[reference]) == pytest.approx(
        np.median(baseline.magnitude_db[reference]), abs=0.2)
    assert take.samples == pytest.approx(average_summed_capture(bass, raw.astype(np.float32), delay, integrity.pass_alignment))


@pytest.mark.parametrize("noise_rms,dropout", [(0.11, False), (0.01, False), (0.01, True)])
def test_noisy_passes_still_average_at_the_schedule(bass_fixture, tmp_path, monkeypatch, noise_rms, dropout):
    bass = _bass(bass_fixture)
    rate, delay = bass.sample_rate_hz, 800
    raw = np.pad(render_program_pcm(bass)[:, 0].astype(np.float64) * 0.01, (delay, rate))
    quiet = bass.segment(sweep_ambient_id("sweep_verify"))
    noise = np.random.default_rng(19).normal(0, noise_rms, raw.size)
    noise[:delay + quiet.start_sample] = 0
    raw += noise
    if dropout:
        at = delay + bass.segment("sweep_verify_repeat_1").start_sample + rate
        raw[at:at + ZERO_RUN_MIN_SAMPLES] = 0
    take = _replay(bass, raw, tmp_path, monkeypatch)
    integrity = take.analysis.capture_integrity
    assert (integrity.failed, take.analysis.glitch_detected) == (("zero_fill_runs",) if dropout else (), dropout)
    if noise_rms == 0.11:
        assert integrity.locate_confidence_min == pytest.approx(0.5, abs=0.05)
        assert (take.analysis.anchor.corroborated, take.analysis.anchor.ambiguous) == (True, False)
    evidence = integrity.to_dict()
    assert evidence["pass_alignment"] == "scheduled"
    assert set(evidence["pass_offsets_samples"].values()) == {0}
    assert all(peak < 1 - peak for peak in evidence["pass_correlation_peaks"].values() if peak is not None)
    content = evidence["repeat_content"]
    assert set(content) == {"repeat_epsilon_ppm", "repeat_level_delta_db", "within_role_desync_samples", "noise_consistency"}
    assert len(content["noise_consistency"]) == 2
    for pair in content["noise_consistency"]:
        assert pair["expected_rms"] == pytest.approx(math.sqrt(2) * noise_rms, rel=0.01)
        assert pair["observed_to_expected_rms_ratio"] == pytest.approx(1, abs=0.01)
    sweeps = [s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP]
    offset = take.analysis.locations[0].scheduled_start
    size = quiet.n_samples + sweeps[0].n_samples + bass.segment("tail").n_samples
    starts = [offset + s.start_sample - quiet.n_samples for s in sweeps]
    expected = np.mean([raw.astype(np.float32)[start:start + size].astype(np.float64) for start in starts], axis=0)
    assert np.array_equal(take.samples[starts[0]:starts[0] + size], expected)
    view = bass_take(take)
    assert all(row["pass_alignment"] == "scheduled" for row in view["passes"])
    assert view["diagnostics"]["pass_correlation_peaks"] == evidence["pass_correlation_peaks"]
    assert view["diagnostics"]["repeat_content"] == content
    qualified = []
    for band in view["bands"]:
        snr = band["estimated_snr_db"]
        assert band["fundamental_qualified"] == (not dropout and snr is not None and snr >= DRIVER.snr_warn_db)
        if band["fundamental_qualified"]:
            qualified.append(tuple(band["band_hz"]))
    assert qualified == ([BASS_BANDS_HZ[i] for i in (0, 1, 3, 4)] if noise_rms == 0.01 and not dropout else [])
    frequencies = np.array(view["freqs_hz"])
    assert np.array_equal(view["fundamental_qualified"],
        np.any([(frequencies >= lo) & (frequencies < hi) for lo, hi in qualified], axis=0)
        if qualified else np.zeros(frequencies.size, dtype=bool))


@pytest.mark.parametrize("edge", [-1, 0, 1])
def test_pass_alignment_peaks_and_edges(bass_fixture, edge):
    bass = _bass(bass_fixture)
    rate, delay = bass.sample_rate_hz, 800
    search = round(SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * rate / 1000)
    pcm = render_program_pcm(bass)[:, 0].astype(np.float64) * 0.1
    raw = np.pad(pcm, (delay, rate))
    sweeps = [s for s in bass.segments if s.kind == KIND_SUMMED_SWEEP]
    for sweep in sweeps[1:]:
        start = delay + sweep.start_sample
        raw[start:start + sweep.n_samples] = 0
        start += edge * search
        raw[start:start + sweep.n_samples] = pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples]
        quiet_start = start - bass.segment(sweep_ambient_id(sweep.segment_id)).n_samples
        raw[quiet_start - search:quiet_start] = 10
    _, alignment = align_summed_capture(bass, raw, delay, search_samples=search)
    assert alignment.method == ("scheduled" if edge else "correlated")
    assert alignment.edge_peaks == (tuple(s.segment_id for s in sweeps[1:]) if edge else ())
    assert list(alignment.correlation_peaks.values()) == [None, *([pytest.approx(1, abs=1e-9)] * (len(sweeps) - 1))]
    assert set(alignment.offsets_samples.values()) == {0}


@pytest.mark.parametrize("purpose", ["room", "speaker"])
def test_single_sweep_analysis_is_byte_identical(bass_fixture, monkeypatch, purpose):
    spec = MeasureSpec(kind="verify", graph_scope="candidate", candidate_id="trial",
                       program_phase="cloud_verify" if purpose == "room" else "verify")
    stimulus = compose_plan_program(SimpleNamespace(_excitation=bass_fixture[3]), spec, None, context=plan_context())
    raw = np.pad(render_program_pcm(stimulus)[:, 0].astype(np.float64) * 0.1, (800, 48000))
    raw += np.random.default_rng(19).normal(0, 0.001, raw.size)
    assert sum(s.kind == KIND_SUMMED_SWEEP for s in stimulus.segments) == 1
    assert average_summed_capture(stimulus, raw, 800) is raw
    analyze = lambda: analyze_program_capture(stimulus, raw, stimulus.sample_rate_hz,
                        geometry=MeasurementGeometry(gate_exempt_reason=gate_exemption(purpose)))
    result = analyze()
    monkeypatch.setattr("jasper.audio_measurement.program_analysis.dispatch.align_summed_capture",
                        lambda _program, capture, _offset, **kw: (capture, None))
    monkeypatch.setattr("jasper.audio_measurement.program_analysis.dispatch.average_summed_capture",
                        lambda _program, capture, _offset: capture)
    assert len({json.dumps(asdict(a), default=lambda array: array.tobytes().hex(), sort_keys=True)
                for a in (result, analyze())}) == 1


@pytest.mark.parametrize("scope,expected", [("drivers", ("near_field_splice_not_implemented",)), ("candidate", ())])
def test_nearfield_splice_stub_only_applies_to_driver_captures(scope, expected):
    spec = MeasureSpec(kind="baseline", graph_scope=scope, regime="near_field",
                       candidate_id="trial" if scope == "candidate" else "")
    assert tuple(stub.code for stub in stubbed_capabilities(spec)) == expected


@pytest.mark.parametrize("passes,fault,check,status", [
    (3, None, "repeat_epsilon_ppm", None),
    (3, "epsilon", "repeat_epsilon_ppm", None),
    (3, "level", "repeat_level_delta_db", None),
    (3, "step", "within_role_desync_samples", None),
    (3, "large_step", "within_role_desync_samples", None),
    (3, None, "discontinuity_step", "not_evaluated"),
    (5, None, "discontinuity_step", "pass"),
    (5, "step", "discontinuity_step", "pass"),
    (5, "large_step", "discontinuity_step", "fail"),
])
def test_verify_repeat_content_is_disclosed_and_discontinuities_are_checked(bass_fixture, passes, fault, check, status):
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
        elif fault == "large_step" and index >= passes // 2:
            start += round(2 * SWEEP_SCHEDULE_RESIDUAL_CEILING_MS * rate / 1000)
        gain = 10 ** (1 / 20) if fault == "level" and index == passes - 1 else 1
        raw[start:start + sweep.n_samples] = gain * pcm[sweep.start_sample:sweep.start_sample + sweep.n_samples]
    analysis = analyze_program_capture(bass, raw, rate)
    integrity = analysis.capture_integrity
    assert not {"repeat_epsilon", "repeat_level_agreement", "within_role_desync"}.intersection(c.name for c in integrity.checks)
    if status is None:
        assert math.isfinite(integrity.repeat_content[check])
        if fault == "level":
            assert integrity.repeat_content[check] == pytest.approx(1, abs=0.01)
            assert (integrity.failed, analysis.glitch_detected) == ((), False)
    else:
        assert next(c.status for c in integrity.checks if c.name == check) == status
        if status == "fail":
            assert check in integrity.failed and analysis.glitch_detected
