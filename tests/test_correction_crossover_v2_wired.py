# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wired capture, host binding, record metadata, and frame integrity."""

from __future__ import annotations

from tests.crossover_v2_fixtures import _inline_spec

from jasper.active_speaker.crossover_v2 import refusal_copy
from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
from jasper.active_speaker.crossover_v2.programs import predictive_program_for_spec
from jasper.web.correction_run_host import compose_plan_program
from jasper.web import correction_crossover_v2_evidence as v2evidence
from jasper.web import correction_crossover_v2_state as v2state

import asyncio
import io
import logging
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import numpy as np
from jasper.active_speaker.angle_capture import LevelPolicy, ResolvedLevel
from jasper.active_speaker.arm_walk import CAPTURE_CANCEL_PATH, LoopbackSession
from jasper.active_speaker import arm_walk
from tests.test_arm_walk import FakeMover, _walk as arm_run
from jasper.active_speaker.plan_run import RunSignals
from jasper.active_speaker.angle_capture import AngleCaptureRequest, AngleStop
from jasper.active_speaker.run_manifest import RunManifest
from jasper.active_speaker.session_volume_plan import SessionVolumeRestoreResult
from jasper.active_speaker.crossover_v2.program_transaction import ProgramPlaybackTransaction
from jasper.active_speaker import plan_run
from tests.test_active_speaker_measurement_door import box as box
from tests.test_plan_run import banked_program_baselines  # noqa: F401

from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureAnswer,
    CaptureBeginDeferred,
    CaptureStopped,
)
from jasper.active_speaker.crossover_v2.evidence_packet import build_crossover_evidence_packet
from jasper.audio_measurement.calibration import MicSensitivity
from jasper.audio_measurement.program_analysis.model import SWEEP_PEAK_TO_RMS_DB
from jasper.audio_measurement.program import build_measure_program
from jasper.audio_measurement.program_analysis.model import AppliedAlignment, SummedAlignmentReference
from jasper.audio_measurement.wired_capture import (
    CODE_WIRED_MIC_MISSING,
    WiredCaptureAnswer,
    WiredCaptureError,
    WiredMicDevice,
    WiredMicMissing,
    WiredRecorder,
    WiredSplMonitor,
    decode_wav_to_mono,
    encode_wav_s32,
)
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_wired as v2wired
from jasper.web import correction_run_host
from jasper.web import correction_capture, correction_setup
from jasper.active_speaker.crossover_v2 import wired_stimulus as core_capture
from jasper.active_speaker.crossover_v2 import summed_alignment

from tests.test_wired_capture import UMIK2_USB_ID, _Sensitivity, _make_card
from tests.test_plan_run import AnsweredGate, _Store, _walk
from tests.engine_twin import FakeSeams as EngineSeams
from jasper.web.correction_runtime import refusal_envelope
from tests.wired_capture_fixtures import FakePcm
from tests._log_events import event_field_maps
from tests.crossover_v2_banked_round import bank_executor_take
from tests.crossover_v2_fixtures import (
    HOUSEHOLD_DB, FakeSeams as FlowSeams, _check_analysis, _conductor, _verify_pilot, plan_context,
)
from tests.test_audio_measurement_program_analysis import _roles, _synthesize

RATE = 48_000


@pytest.fixture(autouse=True)
def _fast_paced(monkeypatch):
    """Test pacing: the production post-roll (1.0 s) and retry settle (3.0 s)
    are budget allowances for a real room, not something a fake PCM needs."""
    monkeypatch.setattr(core_capture, "WIRED_POST_ROLL_S", 0.01)


def _device() -> WiredMicDevice:
    return WiredMicDevice(
        card_id="UMIK2",
        card_index=2,
        usb_id=UMIK2_USB_ID,
        model_key="minidsp_umik2",
        model_label="miniDSP UMIK-2",
    )


# 1. source resolution


def test_the_registered_mic_is_resolved_when_one_is_present(tmp_path):
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    device = v2wired.resolve_v2_wired_mic(proc_asound=tmp_path)
    assert device.model_key == "minidsp_umik2"


def test_open_wired_capture_mints_identity_and_validates_the_spec():
    opened = v2wired.open_wired_capture(_inline_spec(), device=_device())
    assert opened.pi_session.session_id.startswith("wired-")
    # The 48 kHz pin reaches the wired path through the same validate the
    # capture registration runs.
    assert opened.pi_session.spec.sample_rate_hz == RATE
    assert opened.pi_session.device.card_id == "UMIK2"


def test_open_wired_capture_refuses_an_invalid_spec():
    import dataclasses

    from jasper.capture_protocol import CaptureSpecError

    bad = dataclasses.replace(_inline_spec(), sample_rate_hz=44_100)
    with pytest.raises(CaptureSpecError):
        v2wired.open_wired_capture(bad, device=_device())


def test_two_wired_sessions_mint_distinct_identities():
    spec = _inline_spec()
    first = v2wired.open_wired_capture(spec, device=_device())
    second = v2wired.open_wired_capture(spec, device=_device())
    assert first.pi_session.session_id != second.pi_session.session_id


def test_the_wired_answer_satisfies_the_seam_contract():
    answer = WiredCaptureAnswer(wav=b"")
    assert isinstance(answer, CaptureAnswer)


@pytest.mark.parametrize(
    "raised, expected_code",
    [
        (WiredMicMissing, CODE_WIRED_MIC_MISSING),
        (WiredCaptureError, ""),
    ],
)
def test_resolve_prepare_wired_mic_translates_to_a_refusal(
    monkeypatch, raised, expected_code,
):
    """The refusal reaches the tap CODED where the provider named it, so the
    journal and the 400 say which disclosure this was rather than quoting a
    sentence; a provider error with no code of its own carries none."""

    def _boom(*args, **kwargs):
        raise raised("no mic")

    monkeypatch.setattr(v2wired, "resolve_v2_wired_mic", _boom)
    with pytest.raises(refusal_copy.CrossoverV2Refused) as caught:
        v2host._resolve_prepare_wired_mic()
    assert caught.value.code == expected_code


def test_the_mint_opens_the_capture_on_the_resolved_mic(monkeypatch):
    minted = []
    monkeypatch.setattr(
        v2wired, "open_wired_capture",
        lambda spec, device: minted.append((spec, device)) or "wired-rc",
    )
    device = _device()

    assert v2host._mint_wired_session(device, "spec") == "wired-rc"
    assert minted == [("spec", device)]


def test_the_run_builder_hands_the_provider_its_extras(monkeypatch):
    built = {}

    def _wired_builder(conductor, **kw):
        built.update(kw)
        return "wired-run"

    monkeypatch.setattr(v2wired, "build_v2_wired_run_and_consume", _wired_builder)
    signals = RunSignals()

    assert v2host._build_wired_run(
        "conductor",
        signals=signals, position_gate=None, evidence_refs={}, ceiling_s=42.0,
    ) == "wired-run"
    assert built["ceiling_s"] == 42.0
    assert built["signals"] is signals


# 3. the wired runner (fake conductor)

# 5. hosting: the local kind + the completion endpoint


def _fake_handler(body: bytes = b"{}"):
    from email.message import Message

    headers = Message()
    headers["Content-Length"] = str(len(body))
    return SimpleNamespace(headers=headers, rfile=io.BytesIO(body))


def test_complete_endpoint_conflicts_when_nothing_is_waiting():
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    correction_capture._set_capture_slot(None)
    with pytest.raises(ValueError, match="no wired measurement"):
        correction_handlers._handle_crossover_v2_complete(_fake_handler())


def test_complete_endpoint_fires_the_wired_sessions_signal():
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    fired = []
    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot(
        "crossover_v2:session",
        request_complete=lambda: fired.append(True),
    )
    try:
        result = correction_handlers._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_capture._set_capture_slot(None)
    assert result == {"ok": True}
    assert fired == [True]


def test_the_completion_signal_drops_with_the_slot():
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot(
        "crossover_v2:session", request_complete=lambda: None,
    )
    correction_capture._set_capture_slot(
        {"status": "complete", "kind": "crossover_v2:session"}
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_handlers._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_capture._set_capture_slot(None)


def test_retake_endpoint_conflicts_when_nothing_is_waiting():
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    correction_capture._set_capture_slot(None)
    with pytest.raises(ValueError, match="no wired measurement"):
        correction_handlers._handle_crossover_v2_retake(_fake_handler())


def test_retake_endpoint_fires_the_wired_sessions_signal():
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    fired = []
    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot(
        "crossover_v2:session",
        request_retake=lambda: fired.append(True),
    )
    try:
        result = correction_handlers._handle_crossover_v2_retake(_fake_handler())
    finally:
        correction_capture._set_capture_slot(None)
    assert result == {"ok": True}
    assert fired == [True]


def test_the_retake_signal_drops_with_the_slot():
    """A POST arriving after the walk must not re-open a slot nothing holds."""
    from jasper.web import (
        correction_capture,
        correction_handlers,
    )

    correction_capture._set_capture_slot(None)
    assert correction_capture._begin_capture_slot(
        "crossover_v2:session", request_retake=lambda: None,
    )
    correction_capture._set_capture_slot(
        {"status": "complete", "kind": "crossover_v2:session"}
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_handlers._handle_crossover_v2_retake(_fake_handler())
    finally:
        correction_capture._set_capture_slot(None)


# layer 5: the PLAY SEAM's capture half
#
# The same box plays and records, so one stimulus is one transaction: the
# recorder rolls before the first sample, the program plays, the recorder stops
# after the last, and the bytes land in the bundle under a path the record can
# carry. These pins are what stop `TuningSession.measure` banking a record that
# points at nothing.


class _StimulusProgram:
    """What the play transaction hands the capture half: a real schedule."""

    program_id = "prog-verify"
    phase = "verify"
    sample_rate_hz = RATE
    total_samples = RATE // 10


def _capture_half(tmp_path, *, factory=None, script=None):
    def _factory(rate, budget_s):
        assert rate == RATE, "the rate comes from the program that plays"
        assert budget_s > 0
        return WiredRecorder(
            "fake:pcm",
            sample_rate_hz=rate,
            channels=2,
            max_capture_s=budget_s,
            pcm_factory=lambda: FakePcm(script or [(64, [(1000, 0)] * 64)]),
        )

    if tmp_path.is_dir() and not (tmp_path / "info.json").exists():
        (tmp_path / "info.json").write_text('{"bundle_schema_version":1}')
    return v2wired.WiredStimulusCapture(
        device=_device(),
        bundle_dir=tmp_path,
        recorder_factory=factory or _factory,
    )


@pytest.mark.parametrize("watched", [False, True])
async def test_the_capture_half_records_across_the_play_and_places_the_bytes(tmp_path, watched):
    """One transaction, one answer: the path names bytes that exist.

    The ordering is the pre-roll guarantee and the reason play and capture are
    not two seams here — a recorder armed after the first sample has already
    lost the part of the answer the analysis needs most.
    """
    order: list[str] = []
    half = _capture_half(tmp_path, script=[
        (RATE // 2, [(2 ** 26, 0)] * (RATE // 2)), (1024, [(2 ** 27, 0)] * 1024),
    ] if watched else None)
    if watched:
        half = replace(half, spl_monitor=WiredSplMonitor(_Sensitivity(), 85, 0))

    async def _play() -> None:
        order.append("played")

    relpath = await half.around(_play, program=_StimulusProgram())

    assert order == ["played"]
    assert relpath.startswith("summed/summed_verify_")
    written = tmp_path / relpath
    assert written.exists()
    samples, rate = decode_wav_to_mono(written.read_bytes())
    assert rate == RATE
    assert len(samples) > 0, "the placed capture is the audio that was heard"
    if watched:
        spl = half.take_answer().capture_integrity['spl']
        assert spl == {'weighting': 'Z', 'max_window_db_spl': pytest.approx(75.9, abs=.1),
                       'loudest_half_second_db_spl': pytest.approx(69.9, abs=.1), 'ceiling_db_spl': 85}


async def test_a_recorder_that_will_not_roll_refuses_before_any_excitation(
    tmp_path,
):
    """`StimulusCaptureError` BEFORE the play, so nothing was emitted.

    Wrapped rather than let through as its own `WiredCaptureError`: the play
    transaction classifies an escaping `OSError` as a failed emission, which
    would report a play that never happened.
    """
    played: list[str] = []

    def _dead_factory(rate, budget_s):
        return WiredRecorder(
            "fake:pcm",
            sample_rate_hz=rate,
            channels=2,
            max_capture_s=budget_s,
            pcm_factory=lambda: (_ for _ in ()).throw(
                WiredCaptureError("device is gone")
            ),
        )

    half = _capture_half(tmp_path, factory=_dead_factory)

    async def _play() -> None:
        played.append("played")

    with pytest.raises(v2wired.StimulusCaptureError):
        await half.around(_play, program=_StimulusProgram())

    assert played == [], "the stimulus must not reach a dead recorder"


async def test_the_plays_own_failure_reaches_the_transaction_unchanged(
    tmp_path,
):
    """A half that re-wrapped it would report a lost recording for a failed
    emission, and the transaction's whole classification would move here.

    An ``OSError`` on purpose: it is a type the half DOES wrap for its own
    faults, so a half that put the play inside that same guard would pass every
    weaker pin and fail this one. The live device is still released — an escape
    that left the recorder holding ALSA would wedge the next stimulus.
    """
    opened: list = []

    def _tracking_factory(rate, budget_s):
        pcm = FakePcm([(64, [(1000, 0)] * 64)])
        opened.append(pcm)
        return WiredRecorder(
            "fake:pcm", sample_rate_hz=rate, channels=2,
            max_capture_s=budget_s, pcm_factory=lambda: pcm,
        )

    half = _capture_half(tmp_path, factory=_tracking_factory)

    async def _play() -> None:
        raise OSError("aplay died")

    with pytest.raises(OSError):
        await half.around(_play, program=_StimulusProgram())

    assert not list(tmp_path.rglob("*.wav")), "nothing was heard, nothing placed"
    assert [pcm.closed for pcm in opened] == [True]


async def test_a_capture_that_cannot_be_placed_says_so_after_the_play(
    tmp_path,
):
    """The other side of the play: the room heard it and the bytes were lost.

    The transaction reads `played` off its own flag, so this becomes a banked
    record with an empty path and `stimulus_not_captured` on it — never a
    silent empty one.
    """
    played: list[str] = []
    # A bundle root that is a FILE: the capture's own `mkdir` then raises where
    # a full disk would, without needing one.
    blocked = tmp_path / "bundle"
    blocked.write_text("not a directory")
    half = _capture_half(blocked)

    async def _play() -> None:
        played.append("played")

    with pytest.raises(v2wired.StimulusCaptureError):
        await half.around(_play, program=_StimulusProgram())

    assert played == ["played"], "the stimulus really did play"


@pytest.mark.parametrize("source,level,expected,reason", [
    (source, level, expected, reason) for source in ("cli", "wizard") for level, expected, reason in (
        (-23.0, -23.0, ""), (None, None, "unavailable"),
        (RuntimeError("provider failed"), None, "read_failed"),
        (float("nan"), None, "invalid_value"), (float("inf"), None, "invalid_value"),
        (float("-inf"), None, "invalid_value"),
    )
] + [("unbound", None, None, "unavailable")])
@pytest.mark.parametrize("answer", [WiredCaptureAnswer(wav=b"heard", program={"program_id": "played"}), None])
async def test_the_capture_half_records_into_this_sessions_bundle(source, level, expected, reason, answer, caplog):
    store = SimpleNamespace(bundle_dir="/var/lib/jasper/bundle")
    banked = []

    async def bank(record):
        banked.append(record)
        return "record-id"

    provider = None if source == "unbound" else (AsyncMock if source == "wizard" else Mock)(side_effect=[level, -17.0])
    half = (
        v2host._wired_stimulus_capture(_device(), store, read_loudness_volume_db=provider)
        if source == "wizard" else core_capture.WiredStimulusCapture(
            _device(), Path(store.bundle_dir), read_loudness_volume_db=provider,
        )
    )

    assert isinstance(half, v2wired.WiredStimulusCapture)
    assert half.device.model_key == "minidsp_umik2"
    assert half.bundle_dir == Path("/var/lib/jasper/bundle")
    records = core_capture.CapturedRecordStore(
        SimpleNamespace(bank=bank), half, enrich=lambda *_: {"loudness_volume_db": -99},
    )
    for take_id in ("first", "next"):
        assert await records.bank_answer({"take_id": take_id, "loudness_volume_db": -88, "program_id": "stale"}, answer) == "record-id"
    assert [record["loudness_volume_db"] for record in banked] == [expected, None if source == "unbound" else -17.0]
    assert banked[0]["program_id"] == ("played" if answer else None)
    events = event_field_maps(caplog, "active_speaker.capture_loudness_unknown", take_id="first")
    assert [event["reason"] for event in events] == ([reason] if reason else [])


def test_take_answer_is_take_and_clear(tmp_path):
    """An answer serves exactly one consume; a stale one is never re-served."""
    half = _capture_half(tmp_path)

    async def _play() -> None:
        return None

    asyncio.run(half.around(_play, program=_StimulusProgram()))

    first = half.take_answer()
    assert isinstance(first, WiredCaptureAnswer)
    assert half.take_answer() is None, "the second ask must not re-serve the take"


@pytest.mark.asyncio
async def test_cancelling_recorder_start_drains_and_aborts_before_return(tmp_path):
    entered, release = threading.Event(), threading.Event()
    events = []
    class Recorder:
        def start(self):
            entered.set()
            assert release.wait(1)
            events.append("started")
        def abort(self):
            events.append("aborted")
    half = core_capture.WiredStimulusCapture(
        device=_device(), bundle_dir=tmp_path, recorder_factory=lambda *args: Recorder(),
    )
    async def play():
        events.append("played")
    task = asyncio.create_task(half.around(play, program=_StimulusProgram()))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert events == ["started", "aborted"]
    assert half.take_answer() is None


def test_state_save_refreshes_activity_and_keeps_cleanup_beside_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(v2state, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(v2state.time, "time", lambda: 200.0)
    v2state.save_v2_state({"session_id": "s1", "updated_at": 1.0,
                          "verify": {"outcome": "pass", "code": "verified"}})
    assert v2state.load_v2_state()["updated_at"] == 200.0
    assert v2state._persist_terminal_failure(SimpleNamespace(session_id="s1"), "internal_error")
    state = v2state.load_v2_state()
    assert state["verify"] == {"outcome": "pass", "code": "verified"}
    assert state["execution"]["cleanup_fault_code"] == "internal_error"


def _run_door(tmp_path, box, fakes, manifest, records=None):
    from jasper.active_speaker.crossover_v2.door import isolation_hold
    from jasper.active_speaker.plan_run import RunDoor
    from jasper.audio_measurement.calibration import MicSensitivity
    from tests.engine_twin import tuning_session

    fakes.graph.entry_scope_fingerprint = "entry"
    def build(door, allocate):
        return tuning_session(replace(fakes, graph=door.graph, volume=door.claim,
                                      records=manifest if records is None else records),
                              session_id=manifest.run_id, measurement_level_db=door.measurement_volume_db,
                              allocate_take_id=allocate)[0]
    return RunDoor(
        isolation_hold(graph=fakes.graph, camilla_factory=lambda: box, action="test",
                       volume_state_path=tmp_path / "volume.json"),
        build, MicSensitivity(-12, 18, "1234"), _device(), 85,
    )


def _plan_host(monkeypatch, tmp_path, box, *, gate=None, signals=None, phase=None, request=None):
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.run_manifest import RunManifest
    from tests.engine_twin import FakeSeams as EngineSeams
    from tests.test_plan_run import _Store, _analysis
    from tests.crossover_v2_fixtures import _conductor, FakeSeams as FlowSeams
    from jasper.active_speaker.plan_run import PlanCapture
    from jasper.active_speaker.angle_capture import LevelPolicy, ResolvedLevel
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec

    fakes, flow = EngineSeams(), FlowSeams()
    manifest = RunManifest("host-run", _Store(fakes.records))
    session = SimpleNamespace(session_id=manifest.run_id)
    door = _run_door(tmp_path, box, fakes, manifest)
    conductor = _conductor(flow)
    control = signals or plan_run.RunSignals()
    monkeypatch.setattr(v2state, "persist_conductor_state", lambda *a, **k: None)
    monkeypatch.setattr(v2state, "_persist_terminal_failure", lambda *a, **k: None)
    monkeypatch.setattr(v2state, "_persist_execution_result", lambda *a, **k: None)
    request = replace(request or _walk([0, 20]), level=LevelPolicy(resolved=ResolvedLevel(75, -20, "1234")))
    captures = tuple(PlanCapture(stop, MeasureSpec(kind="verify", graph_scope="candidate",
        candidate_id=stop.candidate_id, positions=(stop.angle_deg,), program_phase=phase))
        for stop in request.stops) if phase else None
    runner = v2wired.build_v2_wired_run_and_consume(
        conductor, door=door,
        signals=control, ceiling_s=30,
        manifest=manifest, request=request, captures=captures,
        analyze=_analysis, assessor=None,
        position_gate=gate,
    )
    return runner, session, fakes, manifest, control, flow


@pytest.mark.parametrize("phase", [None, "verify", "cloud_verify"])
def test_plan_host_completes_without_publishing_or_applying_a_candidate(monkeypatch, tmp_path, box, phase):
    from tests.test_plan_run import AnsweredGate
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict

    import jasper.dsp_apply as dsp_apply

    apply_route, apply_dsp = Mock(), AsyncMock()
    monkeypatch.setattr("jasper.web.correction_crossover_v2_apply.handle_v2_apply", apply_route)
    monkeypatch.setattr(dsp_apply, "apply_dsp_config", apply_dsp)
    gate = AnsweredGate()
    runner, session, fakes, manifest, _, flow = _plan_host(monkeypatch, tmp_path, box, gate=gate, phase=phase)
    def assessed(*args, **kwargs):
        assert fakes.graph.restores == 0
        assert box.volume_db == -20
        return TakeVerdict(True, next="accept")
    monkeypatch.setattr(plan_run, "assess", assessed)
    asyncio.run(runner(session))
    assert manifest.status == "complete"
    assert manifest.takes_measured == 2
    assert len(gate.grants) == 2
    assert fakes.graph.restores == 1
    assert box.volume_db == HOUSEHOLD_DB
    apply_route.assert_not_called()
    apply_dsp.assert_not_called()


@pytest.mark.parametrize("signal", ["complete", "stop"])
def test_plan_host_controls_drain_the_session(monkeypatch, tmp_path, box, signal):
    runner, session, fakes, manifest, signals, _ = _plan_host(monkeypatch, tmp_path, box)
    getattr(signals, signal).set()
    async def drive():
        if signal == "stop":
            with pytest.raises(CaptureStopped):
                await runner(session)
        else:
            await runner(session)
    asyncio.run(drive())
    assert fakes.play.calls == []
    assert fakes.graph.restores == 1
    assert box.volume_db == HOUSEHOLD_DB
    assert manifest.finalized


@pytest.mark.parametrize("reason, detail, code", [
    ("unregistered_capture_reason", "", "internal_error"),
    ("unregistered_capture_reason", "capture=4", "internal_error"),
    ("retries_spent", "", "retries_spent"),
])
def test_plan_host_preserves_refusal_reason(monkeypatch, tmp_path, box, reason, detail, code):
    runner, session, _, manifest, _, _ = _plan_host(monkeypatch, tmp_path, box)
    manifest.reason, manifest.detail = reason, detail
    monkeypatch.setattr(plan_run, "run_plan", AsyncMock(return_value=manifest))
    failures = []
    monkeypatch.setattr(v2state, "_persist_terminal_failure", lambda conductor, code, **kw: failures.append(code))
    with pytest.raises(refusal_copy.CrossoverV2Refused) as caught:
        asyncio.run(runner(session))
    envelope = refusal_envelope(caught.value)
    assert envelope["code"] == code
    copy = detail or refusal_copy.REASON_REGISTRY[code].message
    assert envelope["error"] == (copy if code == reason else f"{reason}: {copy}")
    assert failures == [code]


@pytest.mark.parametrize("step,code", [("capture", "internal_error"), ("compose", "program_not_composed"),
                                      ("analysis", "internal_error")])
async def test_capture_failure_keeps_exception_detail_in_the_round(monkeypatch, tmp_path, box, step, code):
    persist, terminal = v2state.persist_conductor_state, v2state._persist_terminal_failure
    runner, session, fakes, manifest, _, _ = _plan_host(monkeypatch, tmp_path, box)
    monkeypatch.setattr(v2state, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(v2state, "persist_conductor_state", persist)
    monkeypatch.setattr(v2state, "_persist_terminal_failure", terminal)
    failure = Mock(side_effect=ValueError("x"))
    if step == "compose":
        monkeypatch.setattr(fakes.play, "run", ProgramPlaybackTransaction(compose=failure, session_volume_plan=None).run)
    elif step == "analysis":
        monkeypatch.setattr(plan_run, "assess", failure)
    else:
        monkeypatch.setattr(fakes.play, "run", AsyncMock(side_effect=ValueError("x")))
    with pytest.raises((ValueError, refusal_copy.CrossoverV2Refused)):
        await runner(session)
    saved = v2state.load_v2_state()["failure"]
    assert saved["code"] == manifest.reason == code
    assert saved["detail"] == manifest.detail == "ValueError: x"
    assert fakes.graph.restores == 1


async def test_run_failure_without_result_keeps_detail_and_restore(monkeypatch, tmp_path):
    monkeypatch.setattr(v2state, "_state_path", lambda: tmp_path / "state.json")
    conductor = _conductor(FlowSeams())
    v2state.save_v2_state({"session_id": conductor.session_id, "execution": {"volume_restore": "stale"}})
    door = SimpleNamespace(opened=None)

    async def execute(*args, **kwargs):
        try:
            raise RuntimeError("x")
        finally:
            door.opened = SimpleNamespace(restore_result=SessionVolumeRestoreResult.EXACT_RESTORED)

    runner = v2wired.build_v2_wired_run_and_consume(
        conductor, door=door, signals=RunSignals(), ceiling_s=30,
        manifest=None, request=None, captures=None, analyze=None, assessor=None, execute=execute,
    )
    with pytest.raises(RuntimeError):
        await runner(SimpleNamespace(session_id=conductor.session_id))
    state = v2state.load_v2_state()
    assert state["failure"]["code"] == "internal_error"
    assert state["failure"]["detail"] == "RuntimeError: x"
    assert state["execution"]["volume_restore"] == "exact_restored"


@pytest.mark.parametrize("repeats", [1, 3])
@pytest.mark.parametrize("check_passes", [False, True])
async def test_check_exhaustion_before_timing_and_measure(monkeypatch, tmp_path, box, caplog, repeats, check_passes):
    checks = iter([False, False, False, check_passes])
    flow = FlowSeams(check=lambda program: _check_analysis(program, snr_floor_ok=next(checks)))
    fakes = EngineSeams()
    request = AngleCaptureRequest(stops=(AngleStop(0, "per_driver"),), repeats=repeats,
                                  level=LevelPolicy(resolved=ResolvedLevel(75, -20, "1234")))
    captures = plan_run.prepare_plan_captures(request)
    conductor = _conductor(flow, index_phase_map={i: c.spec.program_phase for i, c in enumerate(captures, 1)})
    manifest = RunManifest("check-exhaustion", _Store(fakes.records))
    programs, play = [], fakes.play.run

    async def compose_and_play(**kwargs):
        programs.append(correction_run_host.compose_plan_program(
            conductor, kwargs["spec"], kwargs["stimulus_dbfs"], context=plan_context()))
        return await play(**kwargs)

    monkeypatch.setattr(fakes.play, "run", compose_and_play)
    records = core_capture.CapturedRecordStore(manifest, SimpleNamespace(
        take_answer=lambda: WiredCaptureAnswer(wav=b"", program=programs[-1].to_dict())))
    analyze, assessor = correction_run_host.bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    door = _run_door(tmp_path, box, fakes, manifest, records)
    with caplog.at_level(logging.INFO):
        await plan_run.run_plan(request, door=door, manifest=manifest, captures=captures,
            analyze=analyze, assessor=assessor, gate=AnsweredGate(), aborts={},
            admit=lambda i, a, e, ledger: conductor.authorize_begin(i, a, e, executor_ledger=ledger))
    assert manifest.reason == ("" if check_passes else "snr_floor")
    assert bool(conductor._gain_plan_db) is check_passes
    events = event_field_maps(caplog, "correction.crossover_v2_authorized")
    expected = [("check", a, a - 1) for a in range(1, 5)]
    if check_passes:
        expected += [(phase, 1, 3) for phase in ["entry_baseline"] * repeats + ["measure"] * repeats]
    assert [(e["phase"], int(e["attempt"]), int(e["extra_used"])) for e in events] == expected
    assert len(programs) == manifest.takes_measured == len(expected)
    assert fakes.graph.restores == 1


async def test_host_retake_after_budget_exhaustion_keeps_its_code(monkeypatch, tmp_path, box):
    signals = RunSignals()

    class RetakingGate(AnsweredGate):
        def gate(self, index, attempt, entry):
            if index == 3:
                signals.retake.set()
                raise CaptureBeginDeferred("awaiting_position", "placement")
            return super().gate(index, attempt, entry)

    runner, session, _, manifest, _, _ = _plan_host(
        monkeypatch, tmp_path, box, gate=RetakingGate(), signals=signals,
        request=_walk([0], ("fp-a", "fp-b", "fp-c")),
    )
    verdicts = iter([
        *(refusal_copy.TakeVerdict(False, "snr_floor", next="fix_and_retake", charge="operator") for _ in range(4)),
        refusal_copy.TakeVerdict(True),
    ])
    monkeypatch.setattr(plan_run, "assess", lambda *a, **k: next(verdicts))
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    with pytest.raises(refusal_copy.CrossoverV2Refused) as caught:
        await runner(session)
    assert manifest.records.snapshots[-1]["reason"] == caught.value.code == "retries_spent"
    assert [take.get("fault") for take in manifest.takes] == ["snr_floor"] * 4 + [None]


@pytest.mark.parametrize("caller,code,template", [
    (arm_walk.EXIT_STUCK, "arm_host_stuck", "hard_stop"),
    (arm_walk.EXIT_MOVE_FAILED, "move_failed", "session_restart"),
    (arm_walk.EXIT_TERMINATED_PARKED, "terminated_parked", "session_restart"),
    (arm_walk.EXIT_INTERRUPTED_PARKED, "user_stopped", "session_restart"),
    ("human", "user_stopped", "session_restart"),
])
def test_capture_cancel_reason_reaches_the_executor_manifest(monkeypatch, tmp_path, box, caller, code, template):
    def dispatch(path, *, data=None, headers=None):
        if data is not None:
            handler = SimpleNamespace(path=path.removeprefix("/sound/speaker"), rfile=io.BytesIO(data),
                                      headers={"Content-Length": str(len(data))}, _send_json=Mock())
            correction_setup._dispatch_crossover(handler)
            assert handler._send_json.call_args.args[0]["capture"]["status"] == "stopping"
        return 200, "{}"

    client = LoopbackSession(host_header="jts3.local")
    monkeypatch.setattr(client, "open", dispatch)

    class CancellingGate(AnsweredGate):
        def gate(self, index, attempt, entry):
            if caller == "human":
                dispatch(CAPTURE_CANCEL_PATH, data=b"{}")
            else:
                arm = arm_run(FakeMover(move_ok=False), client)
                if caller == arm_walk.EXIT_MOVE_FAILED:
                    monkeypatch.setattr(client, "poll", lambda: arm_walk.Poll(
                        arm_walk.Pending(1, 1, 20, "summed"), True, mover="arm"))
                    assert arm.run() == caller
                elif caller >= arm_walk.SIGNAL_EXIT_BASE:
                    monkeypatch.setattr(arm, "_walk", Mock(side_effect=SystemExit(caller)))
                    with pytest.raises(SystemExit) as stopped:
                        arm.run()
                    assert stopped.value.code == caller
                else:
                    monkeypatch.setattr(arm, "_walk", lambda: caller)
                    assert arm.run() == caller
            super().gate(index, attempt, entry)

    runner, session, _, manifest, signals, _ = _plan_host(monkeypatch, tmp_path, box, gate=CancellingGate())
    monkeypatch.setattr(correction_capture, "_capture_slot", {
        "status": "awaiting_capture", "kind": "crossover_v2:session", "session_id": session.session_id,
    })
    monkeypatch.setattr(correction_capture, "_capture_stop_request", signals.request_stop)
    failures = []
    monkeypatch.setattr(v2state, "_persist_terminal_failure", lambda conductor, code, **kw: failures.append(code))
    with pytest.raises(CaptureStopped):
        asyncio.run(runner(session))
    assert manifest.records.snapshots[-1]["reason"] == code
    assert failures == [code]
    assert refusal_copy.REASON_REGISTRY[code].template == template
    assert all(stop["reason"] == code for stop in manifest.not_measured)


async def test_plan_host_waits_for_the_gate_before_admission_and_capture(monkeypatch, tmp_path, box):
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    from jasper.active_speaker import plan_run

    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    gate = PositionGate()
    runner, session, fakes, manifest, signals, _ = _plan_host(monkeypatch, tmp_path, box, gate=gate)
    task = asyncio.create_task(runner(session))
    for _ in range(100):
        if gate.published()["pending"]:
            break
        await asyncio.sleep(0)
    assert gate.published()["pending"]["index"] == 1
    assert not fakes.play.calls
    signals.complete.set()
    await task
    assert manifest.reason == "complete_requested"
    assert box.volume_db == HOUSEHOLD_DB


async def test_host_retake_uses_the_run_ledger_once_and_returns_to_the_gate(monkeypatch, tmp_path, box):
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
    from tests.test_plan_run import AnsweredGate

    signals = plan_run.RunSignals()
    gate = AnsweredGate()
    runner, session, fakes, manifest, _, _ = _plan_host(monkeypatch, tmp_path, box, gate=gate, signals=signals)
    def assessed(*args, **kwargs):
        if len(gate.grants) == 1:
            signals.retake.set()
        return TakeVerdict(True, next="accept")
    monkeypatch.setattr(plan_run, "assess", assessed)
    await runner(session)
    assert manifest.status == "complete"
    assert manifest.takes_measured == 3
    assert [call[0] for call in gate.grants] == [1, 1, 2]
    assert max(progress["budget"]["by_household"] for progress in gate.progress) == 1
    assert fakes.graph.restores == 1
    assert box.volume_db == HOUSEHOLD_DB


@pytest.mark.parametrize("banked,position,vertical,scope", [
    (True, 0, 0, "timing"), (True, 0, 0, "candidate"), (True, 0, 0, "applied"), (False, 0, 0, "timing"),
    (True, 20, 0, "timing"), (True, 0, 20, "timing"), (True, 0, 0, "drivers"),
])
async def test_executor_retains_summed_reference_before_measure(
    monkeypatch, caplog, banked, position, vertical, scope
):
    conductor = _conductor(FlowSeams(), index_phase_map={1: "check", 2: "entry_baseline", 3: "measure"},
                           measure_entry_baseline=None)
    monkeypatch.setattr(conductor, "_applied_alignment", lambda: AppliedAlignment(191.6, "normal", "authored_by_model"))
    conductor._check_ambient_report = {"bands": [{"band_id": "mid", "band_hz": [1000, 4000], "level_dbfs": -45}]}
    freqs = np.linspace(100, 20000, 100)
    reference = SummedAlignmentReference(freqs, np.zeros(100),
                                         {role: np.ones_like for role in ("woofer", "tweeter")}, (1200, 5000))
    build_reference = Mock(return_value=reference)
    monkeypatch.setattr(summed_alignment, "session_reference", build_reference)
    production = v2evidence.bind_production_analyze(resolve_calibration=None)
    conductor._seams = replace(conductor._seams, analyze=production,
        summed_alignment_reference=lambda b, p: summed_alignment.session_reference(Path("bundle"), b, p))
    saved = []

    async def bank(record):
        saved.append(record)
        return record["take_id"] + ".json"

    records = core_capture.CapturedRecordStore(SimpleNamespace(bank=bank), None)
    analyze, _ = correction_run_host.bind_plan_analysis(conductor, records,
        manifest=SimpleNamespace(calibration={}, capture_record=dict), evidence={})
    impulse = np.zeros(4096)
    impulse[200] = 1
    measure = build_measure_program({"woofer": -30, "tweeter": -30}, _roles(),
                                    sweep_durations={"woofer": .3, "tweeter": .3})
    captures = ([(2, "entry_baseline", conductor.program_for_phase("entry_baseline"))] if banked else [])
    captures.append((3, "measure", measure))
    caplog.set_level(logging.INFO)
    for index, phase, program in captures:
        samples = _synthesize(program, woofer_ir=impulse, tweeter_ir=impulse, noise=0)
        wav, _ = encode_wav_s32((samples * (2**31 - 1)).astype(np.int32), sample_rate_hz=RATE)
        record = {"take_id": f"wired-take-{index}", "index": index, "attempt": 1, "phase": phase,
                  "program_phase": phase, "position_deg": position, "vertical_deg": vertical,
                  "graph_scope": scope, "graph_fingerprint": "played-graph"}
        record_id = await records.bank_answer(record, WiredCaptureAnswer(wav=wav, program=program.to_dict()))
        analysis = analyze(saved[-1], record_id)
    available = banked and position == vertical == 0 and scope == "timing"
    assert conductor.measure_priors().summed_alignment is (
        reference if available else None
    )
    events = event_field_maps(caplog, "active_speaker.summed_reference_unreadable")
    if available:
        build_reference.assert_called_once()
        baseline = build_reference.call_args.args[1]
        assert baseline.artifact_ref == saved[0]["take_id"]
        assert baseline.graph_fingerprint == saved[0]["graph_fingerprint"]
        assert events == []
    else:
        build_reference.assert_not_called()
        reasons = (["entry_baseline_scope"] if banked and scope != "timing" else []) + ["no_entry_baseline"]
        assert events == [{"code": "summed_reference_unreadable", "reason": reason} for reason in reasons]
        assert analysis.candidate.alignment_objective == "saved_timing"


@pytest.mark.parametrize("responses", [(False, True, False), (True, True, False), (False, False, False)])
def test_executor_anchors_the_first_readable_summed_repeat(responses):
    conductor = _conductor(FlowSeams(), index_phase_map={1: "entry_baseline"}, measure_entry_baseline=None)
    records = SimpleNamespace(enrich=None, after_bank=None)
    correction_run_host.bind_plan_analysis(conductor, records,
        manifest=SimpleNamespace(calibration={}, capture_record=dict), evidence={})
    hz = np.linspace(1200, 5000, 100)
    anchor = None
    for index, readable in enumerate(responses):
        analysis = SimpleNamespace(program_id="sum", summed_response=(
            SimpleNamespace(freqs_hz=hz, magnitude_db=np.zeros_like(hz), gating=None, validity_floor_hz=None)
            if readable else None))
        conductor._seams = replace(conductor._seams, analyze=lambda *a, **kw: analysis)
        record = {"take_id": f"sum-{index}", "index": 1, "program_phase": "entry_baseline", "graph_scope": "timing",
                  "position_deg": 0, "vertical_deg": 0, "graph_fingerprint": "played",
                  "program": conductor.program_for_phase("entry_baseline").to_dict()}
        enriched = records.enrich(None, record)
        records.after_bank(enriched, record["take_id"] + ".json")
        anchor = anchor or (record["take_id"] if readable else None)
        baseline = conductor._measure_entry_baseline
        assert (baseline.artifact_ref if baseline else None) == anchor


@pytest.mark.parametrize("phase", ["check", "measure"])
@pytest.mark.parametrize("clipped_take", [False, True])
async def test_host_binds_assessment_and_applies_its_retry_level(monkeypatch, phase, clipped_take):
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.audio_measurement.program import STIMULUS_KINDS
    from jasper.web.correction_run_host import bind_plan_analysis, compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams, _conductor, _check_analysis, _measure_analysis, _verify_analysis

    factory = {"check": _check_analysis, "measure": _measure_analysis, "verify": _verify_analysis}[phase]
    def clipped(program):
        analysis = factory(program)
        return replace(analysis, locations=tuple(replace(loc, clipped=clipped_take) for loc in analysis.locations))
    fakes = FakeSeams(**{phase: clipped})
    conductor = _conductor(fakes, index_phase_map={1: phase},
                           gain_plan_db={"woofer": -11.0, "tweeter": -13.0})
    records = SimpleNamespace(enrich=None, after_bank=None)
    analyze, assessor = bind_plan_analysis(conductor, records,
        manifest=SimpleNamespace(calibration={}, capture_record=dict), evidence={})
    spec = MeasureSpec(kind="baseline", graph_scope="candidate" if phase == "verify" else "drivers",
                       candidate_id="baseline-room" if phase == "verify" else "", program_phase=phase)
    gain = None
    ceilings = conductor._measure_gain_ceiling_db
    for attempt in range(1, 4 if clipped_take else 2):
        program = compose_plan_program(conductor, spec, gain, context=plan_context())
        peak = max(seg.gain_db for seg in program.segments if seg.kind in STIMULUS_KINDS)
        if gain is not None:
            assert peak == pytest.approx(gain)
        record = {"take_id": "engine", "index": 1, "attempt": attempt, "program": program.to_dict()}
        records.enrich(None, record)
        records.after_bank(record, "take")
        analysis = await asyncio.to_thread(analyze, record, "take")
        verdict = await asyncio.to_thread(assessor, analysis, phase=phase, program=program, gain_ceiling_db=ceilings)
        if clipped_take:
            assert verdict.fault == "clipped"
            assert verdict.next == "retake_quieter"
            assert verdict.charge == "speaker"
            gain = verdict.next_gain_db
            assert gain < peak
        else:
            assert verdict.ok
    assert ceilings is conductor._measure_gain_ceiling_db


@pytest.mark.parametrize(("anchor", "sensitivity"), [
    (74.9, MicSensitivity(-12.07)),
    (0.0, MicSensitivity(-12.07)),
    (None, MicSensitivity(-12.07)),
    (74.9, None),
])
@pytest.mark.parametrize("offset", [0, -10, 2])
def test_host_binds_session_level_only_to_check_priors(
    monkeypatch, caplog, anchor, sensitivity, offset
):
    fakes = FlowSeams()
    conductor = _conductor(fakes, index_phase_map={1: "check", 2: "measure", 3: "verify"},
                           gain_plan_db={"woofer": -32.0, "tweeter": -38.0})
    records = SimpleNamespace(enrich=None, after_bank=None)
    resolve = Mock(return_value=sensitivity)
    monkeypatch.setattr(correction_run_host, "resolved_household_sensitivity", resolve)
    monkeypatch.setattr(correction_run_host, "CapturedRecordStore", lambda *_args: records)
    monkeypatch.setattr(correction_run_host, "isolation_hold", lambda **_kwargs: None)
    target = (sensitivity.dbfs_from_db_spl(anchor + offset) + SWEEP_PEAK_TO_RMS_DB
              if anchor is not None and sensitivity is not None else None)
    with caplog.at_level(logging.INFO):
        door, analyze, _assessor, _execute = correction_run_host.bind_run_door(
            host=SimpleNamespace(session_volume_plan=lambda: None),
            device=_device(), evidence_store=None, manifest=SimpleNamespace(calibration={}, capture_record=dict),
            production=SimpleNamespace(graph=None), conductor=conductor, refs={}, trims={},
            ceiling_s=30, ceiling_db_spl=85, camilla_factory=None,
            level=LevelPolicy(level_db=-15 + offset, resolved=ResolvedLevel(anchor, -15, "1234") if anchor is not None else None),
        )
        for index, phase in enumerate(("check", "measure", "verify"), 1):
            expected = (
                conductor.check_priors()
                if phase == "check"
                else conductor.measure_priors()
                if phase == "measure"
                else conductor.lateral_priors()
            )
            if phase == "check" and target is not None:
                expected = replace(expected, target_capture_dbfs=target)
            program = conductor.program_for_phase(phase)
            for attempt in (1, 2):
                record = {"take_id": "engine", "index": index, "attempt": attempt, "program": program.to_dict()}
                records.enrich(None, record)
                records.after_bank(record, "take")
                analyze(record, "take")
                assert fakes.analyzed[-1][3].target_capture_dbfs == pytest.approx(expected.target_capture_dbfs)
    resolve.assert_called_once_with(_device())
    assert door.sensitivity is sensitivity
    events = event_field_maps(caplog, "active_speaker.check_level_target")
    assert len(events) == (0 if target is None else 1)
    if target is not None:
        assert float(events[0]["anchor_db_spl"]) == anchor + offset
        assert float(events[0]["target_capture_dbfs"]) == pytest.approx(target)


@pytest.mark.parametrize("scope, phase", [
    ("drivers", "check"), ("drivers", "measure"), ("timing", "entry_baseline"),
    ("candidate", "verify"), ("candidate_branches", "lateral"),
])
def test_predictive_segment_count_survives_solved_gains_and_live_level(scope, phase):
    conductor = _conductor(FlowSeams(), gain_plan_db={"woofer": -50.0, "tweeter": -57.0})
    spec = MeasureSpec(kind="baseline", graph_scope=scope, program_phase=phase,
                       candidate_id=None if scope == "drivers" else "fp-a",
                       branch_target_ids=("woofer", "tweeter") if scope == "candidate_branches" else ())
    declaring = plan_context()
    context = SimpleNamespace(roles_bands=conductor._roles, driver_caps_dbfs=conductor._excitation.caps_dbfs,
                              fc_hz=conductor._excitation.fc_hz, safety_profile=declaring.safety_profile,
                              role_targets=declaring.role_targets,
                              driver_sweep_duration_limits_s=conductor._excitation.sweep_duration_limits_s,
                              driver_bands=conductor._excitation.target_bands)
    predicted = predictive_program_for_spec(context)(spec)
    for stimulus_dbfs in (None, -48.0):
        live = compose_plan_program(conductor, spec, stimulus_dbfs, context=declaring)
        assert len(live.stimulus_segments()) == len(predicted.stimulus_segments())


@pytest.mark.parametrize("target", [None, -48.0, -60.0])
def test_driver_retry_program_preserves_the_solved_role_levels(target):
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.web.correction_run_host import compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams, _conductor

    conductor = _conductor(FakeSeams(), gain_plan_db={"woofer": -50.0, "tweeter": -57.0})
    program = compose_plan_program(conductor, MeasureSpec(kind="baseline", graph_scope="drivers"), target, context=plan_context())
    delta = 0 if target is None else target + 50.0
    assert program.segment("sweep_w").gain_db == pytest.approx(-50.0 + delta)
    assert program.segment("sweep_t").gain_db == pytest.approx(-57.0 + delta)


@pytest.mark.parametrize("gain_plan", [None, {"woofer": -50.0, "tweeter": -57.0}])
def test_summed_takes_keep_the_session_backoff_with_a_check_gain_plan(gain_plan):
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.web.correction_run_host import compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams, _conductor

    conductor = _conductor(FakeSeams(), gain_plan_db=gain_plan)
    spec = MeasureSpec(kind="baseline", graph_scope="candidate", candidate_id="fp-a", program_phase="verify")
    program = compose_plan_program(conductor, spec, None, context=plan_context())
    expected = conductor._excitation.verify_program().segment("sweep_verify").gain_db
    assert program.segment("sweep_verify").gain_db == pytest.approx(expected)
    windowed = compose_plan_program(conductor, spec, -60.0, context=plan_context())
    assert windowed.segment("sweep_verify").gain_db == pytest.approx(-60.0)


async def test_host_analyzes_each_rung_with_its_own_capture(monkeypatch, tmp_path, box):
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.plan_run import PlanCapture
    from jasper.active_speaker.angle_capture import LevelPolicy, ResolvedLevel
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.active_speaker.run_manifest import RunManifest
    from jasper.web.correction_run_host import bind_plan_analysis, compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams as FlowSeams, _conductor
    from tests.engine_twin import FakeSeams
    from tests.test_plan_run import _Store, _walk

    flow, fakes = FlowSeams(), FakeSeams()
    conductor = _conductor(flow, index_phase_map={1: "verify"})
    request = replace(_walk([0]), level=LevelPolicy(resolved=ResolvedLevel(75, -20, "1234")))
    spec = MeasureSpec(kind="verify", graph_scope="candidate", candidate_id="fp-a",
                       program_phase="verify", level_ladder_dbfs=(-30.0, -24.0))
    manifest = RunManifest("two-rungs", _Store(fakes.records))
    def answer():
        rung = fakes.play.calls[-1]["stimulus_dbfs"]
        program = compose_plan_program(conductor, spec, rung, context=plan_context())
        return WiredCaptureAnswer(wav=b"", program=program.to_dict(), device={"rung_dbfs": rung})
    records = core_capture.CapturedRecordStore(manifest, SimpleNamespace(take_answer=answer))
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    session = SimpleNamespace(session_id=manifest.run_id)
    door = _run_door(tmp_path, box, fakes, manifest, records)
    monkeypatch.setattr(v2state, "persist_conductor_state", lambda *a, **k: None)
    monkeypatch.setattr(v2state, "_persist_execution_result", lambda *a, **k: None)
    signals = plan_run.RunSignals()
    run = v2wired.build_v2_wired_run_and_consume(
        conductor, door=door,
        signals=signals, ceiling_s=30,
        manifest=manifest, request=request, captures=(PlanCapture(request.stops[0], spec),),
        analyze=analyze, assessor=assessor,
    )
    await run(session)
    assert manifest.status == "complete"
    assert [row[2].device["rung_dbfs"] for row in flow.analyzed] == [-30.0, -24.0]


@pytest.mark.parametrize("analysis_error", [None, ValueError(), AttributeError(), TypeError()])
@pytest.mark.parametrize("pose,distance", [
    ({}, 1.0), ({"distance_m": 1.25}, 1.25),
    ({"kind": "seat", "seat_offset_m": (0.2, 0.0, 0.1)}, None),
])
def test_executor_banks_capture_provenance(tmp_path, monkeypatch, analysis_error, pose, distance):
    record = bank_executor_take(tmp_path, monkeypatch, analysis_error=analysis_error, pose=pose)
    assert record["side"] == "mono"
    assert record["mark_distance_m"] == distance
    assert record["pose_kind"] == pose.get("kind", "bearing")
    assert record["provenance"]["graph"]["speaker_candidate_id"] == "speaker-candidate"
    assert record["provenance"]["stimulus"]["wav_sha256"] == "a" * 64
    wav_path, = (tmp_path / "sessions").glob(f"*/{record['wav_path']}")
    raw = wav_path.read_bytes()
    assert len(raw) == record["wav_bytes"]
    assert decode_wav_to_mono(raw)[0].size == 32
    if analysis_error is not None:
        assert "capture_calibration" not in record
        assert record["analysis_error"] == {
            "code": refusal_copy.REASON_INTERNAL_ERROR, "error_type": type(analysis_error).__name__,
        }
        return
    assert "analysis_error" not in record
    calibration = record["capture_calibration"]
    assert calibration["applied"] is True
    assert isinstance(calibration["calibration_id"], str)
    assert isinstance(calibration["curve_fingerprint"], str) and len(calibration["curve_fingerprint"]) == 64
    assert type(record["gating_applied"]) is bool
    assert type(record["stimulus_dbfs"]) is float
    assert record["stimulus_dbfs"] == -30.0


def test_executor_banks_the_capture_snr_the_packet_reads(tmp_path, monkeypatch):
    record = bank_executor_take(tmp_path, monkeypatch, analysis_fields={
        "pilot_snr_ok": True, "pilots": (replace(_verify_pilot(-20.0), snr_db=41.7),),
        # The store refuses a non-finite number; an unmeasurable diagnostic must not cost the take.
        "verify_tracking": {"rms_db": float("nan")},
    })
    assert record["diagnostic"]["rms_db"] is None
    session, = {path.parent for path in (tmp_path / "sessions").glob("*/info.json")}
    block = build_crossover_evidence_packet(session)["capture_snr"]
    assert (block["available"], block["n_captures"], block["n_takes_seen"]) == (True, 1, 1)
    assert "status" not in block
    assert block["captures"] == [{
        "take_id": record["take_id"], "wav_sha256": record["wav_sha256"],
        "stimulus_wav_sha256": "a" * 64, "phase": record["phase"],
        "snr": {"pilot_ambient": "present", "pilot_snr_ok": True, "summed_pilot_snr_db": 41.7},
    }]


async def test_host_drift_preempts_consumption_and_reaches_the_manifest(monkeypatch):
    from jasper.active_speaker.crossover_v2.capture_dispatch import level_drift_verdict
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.active_speaker.run_manifest import RunManifest
    from jasper.web.correction_run_host import bind_plan_analysis, compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams, _conductor
    from tests.test_plan_run import _Store
    from tests.engine_twin import FakeSeams as EngineSeams

    conductor = _conductor(FakeSeams(), index_phase_map={1: "verify"})
    consume = Mock(side_effect=AssertionError("drifting take consumed"))
    monkeypatch.setattr(conductor, "check_verdict", consume)
    manifest = RunManifest("drift", _Store(EngineSeams().records))
    manifest.begin({"index": 1, "pose": {"kind": "bearing", "deg": 0}}, attempt=1, pose_index=0)
    records = SimpleNamespace(enrich=None, after_bank=None)
    analyze, assessor = bind_plan_analysis(conductor, records, manifest=manifest, evidence={})
    program = compose_plan_program(conductor, MeasureSpec(kind="verify", graph_scope="candidate", candidate_id="baseline-room", program_phase="verify"), None, context=plan_context())
    record = {"take_id": "drifting", "index": 1, "attempt": 1, "program": program.to_dict()}
    records.enrich(None, record)
    records.after_bank(record, "take")
    analysis = await asyncio.to_thread(analyze, record, "take")
    level = level_drift_verdict(loudest_half_second_db_spl=73, level_reference_db_spl=70, same_pose=True)
    verdict = await asyncio.to_thread(assessor, analysis, phase="verify", program=program, level_verdict=level)
    await manifest.append(record, "take", verdict, complete=True, started_s=0, ended_s=1, level_observation=level.evidence)
    consume.assert_not_called()
    row = manifest.takes[0]
    assert (row["fault"], row["next"], row["charge"]) == ("level_drift_at_session_gain", "retake_same", "none")
    assert row["level"]["level_delta_db"] == 3
