# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Watched sweep wiring, session restoration, and level provenance."""

from __future__ import annotations

from jasper.active_speaker import baseline_record
from jasper.active_speaker import baseline_apply
from jasper.active_speaker.candidate_bank import bank_candidate

import asyncio
import hashlib
import json
import math
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

from jasper.active_speaker import seat_level_sweep as sweep
from jasper.active_speaker.auto_level import MAX_STEP_DB, MIC_RESPONSE_MIN_RISE_DB, reading_budget
from jasper.active_speaker.crossover_v2 import composition
from jasper.active_speaker.crossover_v2.program_transaction import StimulusCaptureStopped
from jasper.active_speaker.session_volume_plan import SessionVolumeOpenResult, SessionVolumeRestoreResult
from jasper.audio_measurement.calibration import MicSensitivity, resolve_mic_sensitivity
from jasper.audio_measurement.playback import PlaybackObservation
from jasper.audio_measurement.program import FrequencyBand, RoleBand, KIND_COURTESY_TONE
from jasper.audio_measurement.wired_capture import WiredCaptureError, WiredRecording, WiredSplCeilingExceeded
from jasper.cli import seat_level
from tests._log_events import event_fields
from tests.test_active_speaker_measured_crossover_candidate import _candidate
from tests.test_crossover_v2_tuning_scope import BASS_EXTENSION
from tests.test_rear_output_foundation import _rear_document, _rear_pair

CAL_WITH_SENS = '"Sens Factor =-12.07dB, AGain =18dB, SERNO: 8108494"\n10.0\t-6.6\n'
CAL_CURVE_ONLY = "10.0\t-6.6\n10.2\t-6.5\n"


def test_resolve_sensitivity_reads_an_explicit_calibration_file(tmp_path):
    path = tmp_path / "umik2.txt"
    path.write_text(CAL_WITH_SENS)
    assert resolve_mic_sensitivity(calibration_file=str(path)) == (
        MicSensitivity(sens_factor_db=-12.07, analog_gain_db=18.0, serial="8108494")
    )


@pytest.mark.parametrize("text", [CAL_CURVE_ONLY, None])
def test_resolve_sensitivity_is_none_without_absolute_reference(tmp_path, text):
    path = tmp_path / "mic.txt"
    if text is not None:
        path.write_text(text)
    assert resolve_mic_sensitivity(calibration_file=str(path)) is None


@pytest.mark.parametrize("argv", [["--calibration-file", "curve.txt"], ["--mic-serial", "no-such-serial"]])
def test_missing_calibration_refuses_before_hardware(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "curve.txt").write_text(CAL_CURVE_ONLY)
    hardware = Mock(side_effect=AssertionError("hardware touched"))
    monkeypatch.setattr(seat_level, "resolve_wired_mic", hardware)
    monkeypatch.setattr(seat_level, "primary_controller", hardware)
    assert seat_level.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == seat_level.REFUSE_MIC_CALIBRATION_UNAVAILABLE
    hardware.assert_not_called()


def test_missing_mic_has_its_own_refusal(monkeypatch, capsys):
    monkeypatch.setattr(seat_level, "resolve_wired_mic", lambda: None)
    assert seat_level.main([]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == seat_level.REFUSE_MIC_ABSENT


def test_defaults_are_the_operators_stated_band():
    args = seat_level.build_parser().parse_args([])
    target = seat_level.SeatLevelTarget(args.target_db_spl, args.tolerance_db)
    assert (target.low_db_spl, target.high_db_spl) == (74.0, 76.0)
    target.validate(ceiling_db_spl=85.0)
    with pytest.raises(seat_level.SeatLevelTargetError):
        seat_level.SeatLevelTarget(90.0, 2.5).validate(ceiling_db_spl=85.0)


@pytest.mark.parametrize("start,cap,duration", [(-40, 0, 8.0), (-55, -12, 11.6), (-40, 12, 10.0)])
def test_watchdog_covers_the_sweep_and_loop_budget(start, cap, duration):
    assert sweep.watchdog_seconds(start, cap, duration) == (
        math.ceil((min(cap, 0) - start) / MAX_STEP_DB) + 7
    ) * (duration + 6.0) + 2 * duration + 30.0


@pytest.fixture
def box(tmp_path, monkeypatch):
    state = SimpleNamespace(gain=-8.0, loudness=-15.0, level=75.0, ambient=35.0, events=[], programs=[], admissions=[],
                            playback_failure=None, ambient_failure=None, ambient_start_fails=True, bundle_failed=False,
                            missing_spl=False, loudness_failure=None, renders=[], room_floor=False,
                            ambient_spans=[], period_maxima=[], room_levels=[])
    async def get(**kwargs):
        return state.gain
    async def set_gain(gain):
        state.gain = gain
        return True
    async def get_loudness():
        return state.loudness
    async def set_loudness(gain, **kwargs):
        if state.loudness_failure and gain != -15.0:
            raise state.loudness_failure
        state.loudness = gain
        return True
    cam = SimpleNamespace(get_volume_db=get, set_volume_db=set_gain,
                          get_loudness_volume_db=get_loudness, set_loudness_volume_db=set_loudness)
    class Plan:
        def __init__(self, *, state_path):
            assert state_path == seat_level.DEFAULT_SESSION_VOLUME_STATE_PATH
        def set_wall_clock_ceiling_s(self, seconds):
            state.watchdog = seconds
        async def open(self, gain, door):
            self.entry = state.gain
            self.measurement_volume_db = gain
            state.events.append("open")
            await set_gain(gain)
            return SessionVolumeOpenResult.OPENED
        def assert_ready(self):
            assert "open" in state.events and "close" not in state.events
        async def close(self, door, *, reason):
            await set_gain(self.entry)
            state.events.append("close")
            return SessionVolumeRestoreResult.EXACT_RESTORED
    @asynccontextmanager
    async def isolation(**kwargs):
        assert kwargs == {"gate_owner": "seat-level"}
        state.events.append("gate")
        try:
            yield
        finally:
            state.events.append("ungate")
    @asynccontextmanager
    async def writer_lock(*args, **kwargs):
        yield
    async def install():
        state.events.append("graph")
    async def restore():
        state.events.append("restore_graph")
    graph = SimpleNamespace(installed_graph_yaml=lambda: "accepted graph", level_reference_yaml="accepted graph",
                            install=install, restore=restore)
    candidate = _candidate(preset=_rear_pair("mono")[0], bass_extension=BASS_EXTENSION,
                           rear_calibration=_rear_document())
    context = SimpleNamespace(topology=object(), preset=object(), role_channels={"woofer": 0, "tweeter": 1},
        role_targets={"woofer": "w", "tweeter": "t"}, safety_profile={}, declared_sensitivities={"tweeter": 94.1},
        playback_device="fake", roles_bands=(RoleBand("woofer", 0, FrequencyBand(20, 20000)),
                                             RoleBand("tweeter", 1, FrequencyBand(500, 20000))),
        driver_caps_dbfs={"woofer": -8, "tweeter": -12}, driver_sweep_duration_limits_s={}, fc_hz=1600.)
    def resolve(status, **kwargs):
        assert kwargs == {"topology": context.topology, "require_banked_level": False}
        return context
    class Store:
        bundle_dir = tmp_path
        def identify_artifact(self, relative):
            state.renders.append(relative)
            return SimpleNamespace(sha256=hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest(), path=relative)
        def bank(self, *args):
            raise AssertionError("a take was banked")
    def open_bundle(*args, **kwargs):
        assert state.events[:2] == ["gate", "open"]
        state.events.append("bundle")
        (tmp_path / "info.json").write_text('{"session_id": "level-bundle"}')
        (tmp_path / "artifacts.json").write_text('{"artifacts": []}')
        return None if state.bundle_failed else {"bundle_dir": tmp_path, "session_id": "level-bundle"}
    def observe(monitor, spl, frames=24000, *, tone_hz=0):
        amplitude = 10 ** ((spl - 94.0) / 20)
        signal = np.sqrt(2) * np.sin(2 * np.pi * tone_hz * np.arange(frames) / 48000) if tone_hz else np.ones(frames)
        data = np.asarray(signal * amplitude * np.iinfo(np.int32).max, dtype="<i4")
        monitor.observe([data.tobytes()], 1, sample_rate_hz=48000)
        return data.tobytes()
    class Recorder:
        failure = None
        def start(self):
            assert self.spl_monitor.max_window_db_spl == -math.inf
            assert self.spl_monitor.loudest_half_second_db_spl == -math.inf
            assert 'graph' in state.events and state.gain <= -40.0
            state.events.append("ambient")
            if state.room_floor:
                self.chunks = (observe(self.spl_monitor, 72.5, frames=1024),
                               observe(self.spl_monitor, state.ambient, frames=22976))
                assert self.spl_monitor.max_window_db_spl == pytest.approx(72.5, abs=.1)
                assert self.spl_monitor.loudest_half_second_db_spl == pytest.approx(61.3, abs=.1)
            else:
                self.chunks = (observe(self.spl_monitor, state.ambient, tone_hz=400),)
            self.failure = state.ambient_failure or self.spl_monitor.error
            if self.failure and state.ambient_start_fails:
                raise self.failure
        def finish(self, *, tail_s):
            assert tail_s == 0
            if self.failure:
                raise self.failure
            return WiredRecording(self.chunks, 24000, 0, 0, False, 48000, 1)
        def abort(self):
            state.events.append("abort")
    class Capture:
        def __init__(self, **kwargs):
            self.monitor = kwargs['spl_monitor']
            assert self.monitor.ceiling_db_spl == 85.0
            state.capture = self
        async def around(self, play, *, program):
            self.monitor.reset()
            try:
                await play()
            finally:
                state.events.append("capture_stopped")
        def take_answer(self):
            if state.missing_spl:
                return SimpleNamespace(capture_integrity={})
            period_max = self.monitor.max_window_db_spl
            if state.room_floor:
                period_max = max(state.gain + 93.36, 60.5 if len(state.programs) % 2 else 72.5)
            state.period_maxima.append(period_max)
            return SimpleNamespace(capture_integrity={"spl": {
                "max_window_db_spl": round(period_max, 2),
                "loudest_half_second_db_spl": round(self.monitor.loudest_half_second_db_spl, 2),
            }})
    def readmit(program, path, **kwargs):
        assert kwargs["graph_yaml"] == "accepted graph"
        assert kwargs["session_volume_db"] == state.gain
        assert kwargs["declared_sensitivities"] == context.declared_sensitivities
        assert kwargs["graph_evidence"] == {
            "bass_extension": candidate.bass_extension, "rear_calibration": candidate.rear_calibration,
        }
        state.admissions.append(kwargs)
        state.programs.append(program)
        return SimpleNamespace(allowed=True, refusals=())
    async def player(bundle_dir, artifact, **kwargs):
        assert state.loudness == state.gain
        state.artifact = artifact
        (tmp_path / "capture.wav").write_bytes(b"raw")
        state.room_levels.append(58 + .5 * (len(state.programs) % 2))
        observed = max(state.gain + 93.36, state.room_levels[-1]) if state.room_floor else state.level
        observe(state.capture.monitor, observed)
        if state.capture.monitor.error:
            raise StimulusCaptureStopped("spl_ceiling_exceeded", "stop", PlaybackObservation(emission="partial"))
        if state.playback_failure:
            raise state.playback_failure
        return SimpleNamespace()
    state.reference_path = tmp_path / "reference.json"
    bank = Mock(wraps=partial(seat_level.write_seat_level_reference, state_path=state.reference_path))
    monkeypatch.setattr(seat_level, 'write_seat_level_reference', bank)
    monkeypatch.setattr(seat_level, 'measurement_window', isolation)
    monkeypatch.setattr(seat_level, 'SessionVolumePlan', Plan)
    monkeypatch.setattr(seat_level, 'live_measurement_session', lambda **kw: None)
    monkeypatch.setattr(seat_level, 'resolve_wired_mic', lambda: object())
    monkeypatch.setattr(seat_level, 'resolved_household_sensitivity', lambda mic: MicSensitivity(0.0))
    monkeypatch.setattr(seat_level, 'primary_controller', lambda: cam)
    monkeypatch.setattr(seat_level, 'conductor_status', lambda: {})
    monkeypatch.setattr(seat_level, 'load_output_topology_strict', lambda path: context.topology)
    monkeypatch.setattr(seat_level, 'resolve_conductor_context', resolve)
    monkeypatch.setattr(seat_level, 'commissioning_spl_ceiling_db', lambda *a, **kw: 85.0)
    monkeypatch.setattr(seat_level, 'load_applied_baseline_profile_state', lambda: {})
    monkeypatch.setattr(seat_level, 'candidate_from_applied_profile', lambda *a: candidate)
    monkeypatch.setattr(seat_level, 'confirmed_protection_sections', lambda *a: {})
    monkeypatch.setattr(seat_level, 'bind_measurement_graph', lambda *a, **kw: graph)
    monkeypatch.setattr(seat_level, 'open_bundle', open_bundle)
    monkeypatch.setattr(seat_level.CommissioningEvidenceStore, 'open', lambda *a, **kw: Store())
    monkeypatch.setattr(seat_level, 'mark_state', lambda *a: state.events.append("closed_bundle"))
    monkeypatch.setattr(sweep, 'make_wired_recorder', lambda *a, **kw: Recorder())
    real_sleep = asyncio.sleep
    async def sleep(seconds):
        if state.events[-1:] == ['ambient']:
            state.ambient_spans.append(seconds)
            return
        await real_sleep(seconds)
    monkeypatch.setattr(sweep.asyncio, 'sleep', sleep)
    monkeypatch.setattr(sweep, 'WiredStimulusCapture', Capture)
    monkeypatch.setattr('jasper.active_speaker.program_admission.readmit_summed_program_from_wav', readmit)
    monkeypatch.setattr('jasper.active_speaker.program_playback.verified_program_aplay', player)
    monkeypatch.setattr('jasper.dsp_apply.dsp_writer_lock', writer_lock)
    monkeypatch.setattr(composition, 'confirm_graph_is_live', AsyncMock())
    state.bank, state.graph, state.context, state.bundle_dir = bank, graph, context, tmp_path
    return state


@pytest.mark.parametrize('outcome', ['converged', 'stop', 'ambient_stop', 'ambient_start_lost', 'ambient_reads_lost', 'missing_spl',
                                            'loudness_refused', 'cancelled', 'error', 'bundle_failed'])
def test_session_banks_only_a_level_and_always_restores(box, outcome, caplog):
    if outcome == 'stop':
        box.level = 86.0
    elif outcome == 'ambient_stop':
        box.ambient_failure = WiredSplCeilingExceeded(86.0, 85.0)
    elif outcome.startswith('ambient_') and outcome.endswith('_lost'):
        box.ambient_failure = WiredCaptureError("failed N consecutive reads")
        box.ambient_start_fails = outcome == 'ambient_start_lost'
    elif outcome == 'missing_spl':
        box.missing_spl = True
    elif outcome == 'loudness_refused':
        from jasper.active_speaker.crossover_v2.door import MeasurementDoorRefused
        box.loudness_failure = MeasurementDoorRefused("measurement_door_volume_not_open", "unconfirmed")
    elif outcome == 'cancelled':
        box.playback_failure = asyncio.CancelledError()
    elif outcome == 'error':
        box.playback_failure = OSError()
    elif outcome == 'bundle_failed':
        box.bundle_failed = True
    args = seat_level.build_parser().parse_args([])
    with caplog.at_level('INFO', logger='jasper.cli.seat_level'):
        if outcome == 'cancelled':
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(seat_level._run(args))
        else:
            result, _ = asyncio.run(seat_level._run(args))
            assert result['status'] == ('converged' if outcome == 'converged' else 'refused')
            assert result['restored'] is True
            if outcome in ('ambient_start_lost', 'ambient_reads_lost', 'missing_spl'):
                assert result['reason'] == 'mic_feed_lost'
            if outcome == 'loudness_refused':
                assert result['reason'] == 'measurement_door_volume_not_open'
            if 'stop' in outcome:

                assert result['reason'] == 'spl_ceiling_exceeded'
                assert result['readings'][-1][1] == pytest.approx(86.0)
                assert event_fields(caplog, 'active_speaker.seat_level_result')['reason'] == 'spl_ceiling_exceeded'
    assert box.gain == -8.0
    assert box.loudness == -15.0
    if outcome == 'bundle_failed':
        assert box.events[-3:] == ['restore_graph', 'close', 'ungate']
        assert not box.programs
    else:
        assert box.events[-4:] == ['restore_graph', 'close', 'closed_bundle', 'ungate']
        assert 'abort' in box.events
    assert box.bank.call_count == int(outcome == 'converged')
    assert not list(box.bundle_dir.rglob('*.wav'))
    assert json.loads((box.bundle_dir / 'info.json').read_text())['session_id'] == 'level-bundle'
    assert (box.bundle_dir / 'artifacts.json').is_file()
    if outcome == 'converged':
        assert len(box.programs) == len(box.admissions) == 2
        first, second = box.programs
        assert all((s.f1_hz, s.f2_hz) == (20.0, 20000.0)
                   for p in box.programs for s in p.stimulus_segments())
        assert box.ambient_spans == [first.total_samples / first.sample_rate_hz]
        assert any(s.kind == KIND_COURTESY_TONE for s in first.segments)
        assert not any(s.kind == KIND_COURTESY_TONE for s in second.segments)
        assert len(first.stimulus_segments()) == len(second.stimulus_segments()) == 1
        assert 7.5 <= second.total_samples / second.sample_rate_hz <= 10.0
        assert box.watchdog == sweep.watchdog_seconds(-40, 0, first.total_samples / first.sample_rate_hz) + 60
        provenance = box.bank.call_args.kwargs['stimulus'].to_dict()
        assert provenance == {'program_id': second.program_id, 'phase': second.phase,
            'wav_sha256': box.artifact.sha256, 'peak_dbfs': round(second.stimulus_segments()[0].gain_db, 2),
            'statistic': 'loudest_half_second_db_spl', 'graph_scope': 'candidate', 'bundle_id': 'level-bundle'}
        assert json.loads(box.reference_path.read_text())['stimulus'] == provenance
        assert box.bank.call_args.kwargs['measured_db_spl'] == 75.0
        ambient = json.loads(box.reference_path.read_text())['ambient_report']
        assert ambient == result['ambient_report']
        assert ambient['method'] == 'one_second_p95'
        transition, = [row for row in ambient['bands'] if row['band_id'] == 'transition']
        assert transition == {'band_id': 'transition', 'band_hz': [350, 1000], 'level_dbfs': pytest.approx(-59, abs=.01)}


def test_room_floor_with_scattered_period_maxima_converges_within_budget(box):
    box.room_floor, box.ambient = True, 58.0
    result, _ = asyncio.run(seat_level._run(seat_level.build_parser().parse_args([])))
    assert result['status'] == 'converged'
    readings = result['readings']
    assert readings[0][0] == -40
    assert result['leveled_db_spl'] == pytest.approx(75, abs=1)
    assert len(readings) <= reading_budget(-40, 0)
    assert box.period_maxima[:2] == [60.5, 72.5]
    assert all(abs(level - 58) <= 1 for level in box.room_levels)
    assert box.ambient_spans == [
        box.programs[0].total_samples / box.programs[0].sample_rate_hz,
        box.programs[1].total_samples / box.programs[1].sample_rate_hz,
    ]
    for (gain, level), (next_gain, _) in zip(readings, readings[1:]):
        if level < box.ambient + MIC_RESPONSE_MIN_RISE_DB:
            assert 0 < next_gain - gain <= MAX_STEP_DB
    assert box.bank.call_count == 1


@pytest.fixture
def commissioning_box(tmp_path, monkeypatch):
    from jasper.active_speaker.design_draft import load_design_draft
    from tests.test_correction_crossover_v2_endpoints import _seed_baseline_apply_environment, _FakeApplyCam

    topology, _ = _seed_baseline_apply_environment(monkeypatch, tmp_path)
    draft = load_design_draft()
    draft["manual_settings"]["drivers"][1]["gain_offset_db"] = -11.0
    draft["driver_research"]["crossover_candidates"][0].update(
        delay_target_role="woofer", delay_ms=0.35, upper_polarity="inverted",
    )
    (tmp_path / "design_draft.json").write_text(json.dumps(draft))
    monkeypatch.setattr("jasper.sound.settings.saved_sound_layers", lambda: ([], 0.0))
    monkeypatch.setattr("jasper.web.sound_active_speaker.mux_socket_command", AsyncMock(return_value={}))
    monkeypatch.setattr("jasper.web.sound_active_speaker.trigger_reconcile", lambda **kw: {"ok": True})
    return topology, _FakeApplyCam()


@pytest.mark.parametrize("applied", [False, True], ids=["declared", "banked"])
async def test_accepted_candidate_can_compile_without_a_banked_candidate_id(tmp_path, monkeypatch, commissioning_box, applied):
    from dataclasses import replace
    from jasper.active_speaker import baseline_profile
    from jasper.active_speaker.state_paths import config_text_sha256
    from jasper.active_speaker.candidate_bank import find_banked_candidate
    from jasper.active_speaker.candidate_parts import candidate_from_design_draft
    from jasper.active_speaker.crossover_v2 import door
    from jasper.active_speaker.crossover_v2.conductor_context import measurement_role_channels
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement_emit import compile_tuning_graph, load_tuning_declaration
    from jasper.web import sound_active_speaker as web

    topology, cam = commissioning_box
    draft = load_design_draft()
    profile = load_tuning_declaration(topology, design_draft=draft)
    profile = replace(profile, role_channels=measurement_role_channels(profile.preset))
    if applied:
        candidate = replace(candidate_from_design_draft(topology, draft), role_attenuations_db={"woofer": -3.0, "tweeter": -8.0},
                            blend_correction=[{"biquad_type": "Peaking", "freq": 2000, "q": 1.0, "gain": -2.0}])
        prepared = baseline_record.prepare_applied_baseline_profile(bank_candidate(candidate), declaration=profile, design_draft=draft)
        baseline_apply.persist_applied_baseline_profile(prepared, apply_state={"result": "success"})
    reviewed = web._active_speaker_baseline_profile_payload()
    assert reviewed["status"] == "ready_to_compile", reviewed["issues"]
    if applied:
        assert reviewed["source"]["measured_candidate_fingerprint"] == candidate.fingerprint
    candidate = find_banked_candidate(reviewed["source"]["measured_candidate_fingerprint"]).candidate
    if not applied:
        assert candidate.analysis["measurement_status"] == "unmeasured"
        assert not (candidate.linearization or candidate.blend_correction or candidate.room_correction or candidate.bass_extension)
        assert candidate.driver_corrections() == {
            "woofer": {"gain_db": 0.0, "delay_ms": 0.35, "inverted": False},
            "tweeter": {"gain_db": -11.0, "delay_ms": 0.0, "inverted": True},
        }
    lookup = Mock(side_effect=AssertionError("accepted candidate went to the bank"))
    monkeypatch.setattr(door, "find_banked_candidate", lookup)
    graph = door.bind_measurement_graph(profile, candidate=candidate, camilla_factory=Mock(), config_dir=tmp_path)
    assert graph.graph_yaml() == compile_tuning_graph(profile, scope="candidate", candidate=candidate)
    assert reviewed["config"]["sha256"] == config_text_sha256(graph.graph_yaml())
    result = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
    assert result["status"] == "applied", result
    assert Path(cam.path).read_text() == graph.graph_yaml()
    assert baseline_profile.load_applied_baseline_profile_state()["source"]["measured_candidate_fingerprint"] == candidate.fingerprint
    graph.select_scope("drivers")
    from jasper.active_speaker.measurement_emit import emit_measurement_graph
    assert graph.graph_yaml() == emit_measurement_graph(profile)
    lookup.assert_not_called()


async def test_commissioning_validates_then_verifies_under_lock_before_loading(monkeypatch, commissioning_box):
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    events = []
    validate = apply_host.validate_camilla_config
    load = cam.set_config_file_path
    def checked(path):
        events.append("validated")
        return validate(path)
    async def loaded(path, **kwargs):
        events.append("loaded")
        return await load(path, **kwargs)
    async def verified():
        from jasper.dsp_apply import _DSP_LOCK_OWNERSHIP
        assert _DSP_LOCK_OWNERSHIP.get() is not None
        events.append("verified")
    monkeypatch.setattr(apply_host, "validate_camilla_config", checked)
    monkeypatch.setattr(cam, "set_config_file_path", loaded)
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    assert result["status"] == "applied"
    assert events == ["validated", "verified", "loaded"]


def test_commissioning_review_compiles_without_writing_the_config(commissioning_box):
    from jasper.web.sound_active_speaker import _active_speaker_baseline_profile_payload

    profile = _active_speaker_baseline_profile_payload()
    assert profile["status"] == "ready_to_compile"
    assert profile["permissions"] == {"may_compile": True}
    assert not Path(profile["config"]["path"]).exists()


@pytest.mark.parametrize("change,code", [
    ("trim", None),
    ("protection", "tweeter:required_highpass_missing"),
    ("validation", "baseline_config_validation_failed"),
])
async def test_commissioning_uses_current_draft_and_checks_protection_before_cleanup(tmp_path, monkeypatch, commissioning_box, change, code):
    from jasper.active_speaker import baseline_profile
    from jasper.dsp_apply import CamillaConfigValidationResult, ValidationStatus
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    web._active_speaker_baseline_profile_payload()
    if change == "validation":
        monkeypatch.setattr(apply_host, "validate_camilla_config", lambda path:
                            CamillaConfigValidationResult(ValidationStatus.INVALID_CONFIG, str(path)))
    else:
        path = tmp_path / "design_draft.json"
        draft = json.loads(path.read_text())
        if change == "protection":
            draft["manual_settings"]["drivers"][1].pop("recommended_highpass_hz", None)
            draft["manual_settings"]["drivers"][1].pop("required_protection_filters", None)
        else:
            draft["manual_settings"]["drivers"][1]["gain_offset_db"] = -12.0
        path.write_text(json.dumps(draft))
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    if code is None:
        assert result["status"] == "applied"
        assert result["profile"]["recomposition_snapshot"]["corrections"]["tweeter"]["gain_db"] == -12.0
        verified.assert_awaited_once()
        return
    assert result["status"] == "blocked"
    assert code in {issue["code"] for issue in result["issues"]}
    assert cam.path is None
    assert baseline_profile.load_applied_baseline_profile_state() is None
    verified.assert_not_awaited()


@pytest.mark.parametrize("route,code", [
    ("narrow", "active_playback_route_too_narrow"),
    ("missing", "baseline_playback_device_missing"),
    ("direct", "baseline_output_handoff_not_supported"),
    ("saved_ring", None),
])
async def test_commissioning_and_declaration_refuse_unusable_routes(monkeypatch, commissioning_box, route, code):
    from dataclasses import replace
    from jasper import output_topology
    from jasper.active_speaker import playback_route
    from jasper.active_speaker.measurement_emit import load_tuning_declaration, MeasurementGraphRefused
    from jasper.web import sound_active_speaker as web

    topology, cam = commissioning_box
    declaration = load_tuning_declaration(topology)
    if route in {"narrow", "missing"}:
        dac = playback_route._dac_by_id(topology.hardware.device_id)
        dac = replace(dac, supports_active_outputd_lane=route == "narrow",
                      active_outputd_lane_channels=1 if route == "narrow" else None)
        monkeypatch.setattr(playback_route, "_dac_by_id", lambda _: dac)
    else:
        monkeypatch.setenv(output_topology.ACTIVE_PLAYBACK_DEVICE_ENV,
                          declaration.playback_device if route == "saved_ring" else "hw:CARD=DAC,DEV=0")
    if code:
        with pytest.raises(MeasurementGraphRefused) as exc:
            load_tuning_declaration(topology)
        assert exc.value.code == code
    else:
        assert load_tuning_declaration(topology).playback_device == declaration.playback_device
    reviewed = web._active_speaker_baseline_profile_payload()
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    if code:
        assert reviewed["status"] == result["status"] == "blocked"
        assert code in {issue["code"] for issue in result["issues"]}
        assert cam.path is None
        verified.assert_not_awaited()
    else:
        assert result["status"] == "applied"


@pytest.mark.parametrize("error_kind,code", [
    ("graph", "measurement_candidate_speaker_mismatch"),
    ("candidate", "attenuation_out_of_range"),
    ("bank", "not_found"),
    ("emitter", "compose_refused"),
])
@pytest.mark.parametrize("phase", ["review", "apply_preflight"])
async def test_commissioning_maps_composer_refusals(monkeypatch, commissioning_box, error_kind, code, phase):
    from jasper.active_speaker import measurement_emit
    from jasper.web import correction_crossover_v2_apply as apply_host
    from jasper.active_speaker.candidate_bank import CandidateBankRefusal
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError
    from jasper.active_speaker.profile import ActiveSpeakerConfigError
    from jasper.web import sound_active_speaker as web

    _, cam = commissioning_box
    error = {
        "graph": measurement_emit.MeasurementGraphRefused(code, {}),
        "candidate": MeasuredCrossoverCandidateError(code),
        "bank": CandidateBankRefusal(code, "candidate unavailable"),
        "emitter": ActiveSpeakerConfigError("invalid graph"),
    }[error_kind]
    if phase == "review":
        monkeypatch.setattr(measurement_emit, "compile_tuning_graph", Mock(side_effect=error))
        monkeypatch.setattr(apply_host, "compile_tuning_graph", Mock(side_effect=error))
        refused = web._active_speaker_baseline_profile_payload()
        assert refused["status"] == "blocked"
        assert refused["issues"][0]["code"] == code
    else:
        monkeypatch.setattr(apply_host, "load_tuning_declaration", Mock(side_effect=error))
    verified = AsyncMock()
    result = await web._active_speaker_baseline_profile_apply_payload(
        on_candidate_verified=verified, camilla_factory=lambda: cam,
    )
    assert result["status"] == "blocked"
    assert result["issues"][0]["code"] == code
    assert cam.path is None
    verified.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["applied", "apply_failed"])
async def test_commissioning_records_apply_outcomes(tmp_path, monkeypatch, caplog, commissioning_box, outcome):
    from jasper.active_speaker import baseline_profile
    from jasper.web import sound_active_speaker as web
    from tests._log_events import event_fields

    _, cam = commissioning_box
    if outcome == "apply_failed":
        first = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
        assert first["status"] == "applied"
        previous = baseline_profile.load_applied_baseline_profile_state()
        load = cam.set_config_file_path
        calls = 0
        async def fail_once(path, **kwargs):
            nonlocal calls
            calls += 1
            return False if calls == 1 else await load(path, **kwargs)
        monkeypatch.setattr(cam, "set_config_file_path", fail_once)
    reviewed = web._active_speaker_baseline_profile_payload()
    caplog.clear()
    caplog.set_level("INFO", logger=baseline_apply.__name__)
    result = await web._active_speaker_baseline_profile_apply_payload(camilla_factory=lambda: cam)
    assert result["status"] == outcome
    started = event_fields(caplog, "correction.crossover_apply_started")
    assert started["candidate_fingerprint"] == reviewed["candidate_fingerprint"]
    if outcome == "apply_failed":
        failed = json.loads((tmp_path / "baseline_profile.json").read_text())
        assert failed["status"] == "apply_failed"
        assert failed["apply"] == result["apply"]
        assert failed["issues"][-1]["code"] == "baseline_profile_apply_failed"
        assert baseline_profile.load_applied_baseline_profile_state() == previous
        rolled_back = event_fields(caplog, "correction.crossover_apply_rolled_back")
        assert rolled_back["rollback_attempted"] == rolled_back["rollback_succeeded"] == "true"
    else:
        succeeded = event_fields(caplog, "correction.crossover_apply_succeeded")
        assert succeeded["candidate_fingerprint"] == succeeded["applied_fingerprint"] == reviewed["candidate_fingerprint"]


def test_unapplied_baseline_refuses_with_its_code(box, monkeypatch, capsys):
    from jasper.active_speaker.candidate_parts import candidate_from_applied_profile
    monkeypatch.setattr(seat_level, 'candidate_from_applied_profile', candidate_from_applied_profile)
    assert seat_level.main([]) == 1
    assert json.loads(capsys.readouterr().out)['reason'] == 'applied_baseline_snapshot_unavailable'
    assert not box.events
    box.bank.assert_not_called()


def test_driver_caps_do_not_move_the_fader_cap(box):
    box.context.driver_caps_dbfs['tweeter'] = -65.0
    box.level = 35.0
    result, _ = asyncio.run(seat_level._run(seat_level.build_parser().parse_args([])))
    assert result['reason'] == 'mic_not_observing'
    assert result['gain_db'] == seat_level.HARD_CEILING_DBFS == 0.0
    box.bank.assert_not_called()


@pytest.mark.parametrize('gain,reason', [(None, 'volume_latch_unconfirmed'), (0.1, 'fader_above_cap')])
def test_sweep_fader_readback_refusals_are_distinct(box, monkeypatch, capsys, gain, reason):
    async def read_once(*args, read_level, **kwargs):
        monkeypatch.setattr(sweep, 'read_fader_db', AsyncMock(return_value=gain))
        await read_level()
        pytest.fail('invalid fader was accepted')
    monkeypatch.setattr(seat_level, 'level_to', read_once)
    assert seat_level.main([]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == reason
    assert result['detail']['restored'] is True
    assert reason in seat_level.REASON_REGISTRY
    assert not box.admissions


def test_repeated_sweep_reuses_render_but_installs_and_admits_each_time(box, monkeypatch):
    async def readings(*args, read_level, set_main_volume_db, **kwargs):
        for _ in range(3):
            assert await read_level() == 75.0
        await set_main_volume_db(-39.0)
        assert await read_level() == 75.0
        return seat_level.LevelResult('refused', 'level_unreachable')
    monkeypatch.setattr(seat_level, 'level_to', readings)
    asyncio.run(seat_level._run(seat_level.build_parser().parse_args([])))
    assert len(box.renders) == 3  # First prelude, no prelude, changed gain.
    assert len(box.admissions) == 4
    assert box.programs[1] is box.programs[2]
    assert box.events.count('graph') == 5  # Initial hold plus every reading.
    assert not list(box.bundle_dir.rglob('*.wav'))
