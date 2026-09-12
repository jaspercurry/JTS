# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Wired capture, host binding, record metadata, and frame integrity."""
from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    CaptureAnswer,
    CaptureStopped,
)
from jasper.audio_measurement.wired_capture import (
    CODE_WIRED_MIC_MISSING,
    WiredCaptureAnswer,
    WiredCaptureError,
    WiredMicDevice,
    WiredMicMissing,
    WiredRecorder,
    decode_wav_to_mono,
)
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_wired as v2wired
from jasper.active_speaker.crossover_v2 import wired_stimulus as core_capture

from tests.test_wired_capture import UMIK2_USB_ID, _make_card
from tests.wired_capture_fixtures import FakePcm
from tests._log_events import event_field_maps

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


# --------------------------------------------------------------------------- #
# 1. source resolution
# --------------------------------------------------------------------------- #


def test_the_registered_mic_is_resolved_when_one_is_present(tmp_path):
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    device = v2wired.resolve_v2_wired_mic(proc_asound=tmp_path)
    assert device.model_key == "minidsp_umik2"


def _real_verify_spec():
    from jasper.active_speaker.crossover_v2_flow import (
        build_v2_verify_session_spec,
    )

    return build_v2_verify_session_spec(
        1600.0, acknowledgement_binding="placement_abcdefghijklmnopqrstuv",
    )


def test_open_wired_capture_mints_identity_and_validates_the_spec():
    opened = v2wired.open_wired_capture(_real_verify_spec(), device=_device())
    assert opened.pi_session.session_id.startswith("wired-")
    # The 48 kHz pin reaches the wired path through the same validate the
    # capture registration runs.
    assert opened.pi_session.spec.sample_rate_hz == RATE
    assert opened.pi_session.device.card_id == "UMIK2"


def test_open_wired_capture_refuses_an_invalid_spec():
    import dataclasses

    from jasper.capture_protocol import CaptureSpecError

    bad = dataclasses.replace(_real_verify_spec(), sample_rate_hz=44_100)
    with pytest.raises(CaptureSpecError):
        v2wired.open_wired_capture(bad, device=_device())


def test_two_wired_sessions_mint_distinct_identities():
    spec = _real_verify_spec()
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
    with pytest.raises(v2host.CrossoverV2Refused) as caught:
        v2host._resolve_prepare_wired_mic()
    assert caught.value.code == expected_code


@pytest.mark.parametrize("preparer", ["verify"])
def test_a_refused_prepare_leaves_the_bundle_store_untouched(
    monkeypatch, tmp_path, preparer,
):
    """S3's behavioral pin, BOTH preparers: the missing-mic refusal fires
    BEFORE ``open_v2_evidence_store`` — a refused start must not abandon the
    prior bundle and write a new one on its way to the 400. The gates ahead
    of the mic resolution are stubbed to pass — for verify that is the
    recovery gate (needs_recovery False) and the applied-state gate (state
    applied True); the evidence store is a bomb."""
    import jasper.active_speaker.branch_chain as branch_chain
    from jasper.active_speaker.crossover_v2 import (
        alignment_prescription as prescription_mod,
    )

    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    try:
        def _no_mic():
            raise WiredMicMissing("no mic")

        monkeypatch.setattr(v2wired, "resolve_v2_wired_mic", _no_mic)
        monkeypatch.setattr(
            v2host, "session_volume_plan",
            lambda: SimpleNamespace(needs_recovery=False),
        )
        monkeypatch.setattr(
            v2host, "reconcile_session_volume_for_new_session",
            lambda run_async, camilla_factory: None,
        )
        monkeypatch.setattr(
            v2host, "resolve_conductor_context",
            lambda status: SimpleNamespace(
                safety_profile={}, role_targets={}, fc_hz=1600.0, preset=None,
            ),
        )
        monkeypatch.setattr(
            branch_chain, "confirmed_protection_sections",
            lambda safety_profile, role_targets: {},
        )
        monkeypatch.setattr(
            prescription_mod, "read_alignment_prescription",
            lambda raw, *, fc_hz, declared_bounds_us, way_count=None: None,
        )
        if preparer == "verify":
            # Stage 2's own preceding gate: an applied durable state.
            v2host.save_v2_state({"applied": True, "tier": ""})

        def _bomb(topology):
            raise AssertionError(
                "a refused prepare must not open an evidence bundle"
            )

        monkeypatch.setattr(v2host, "open_v2_evidence_store", _bomb)
        with pytest.raises(v2host.CrossoverV2Refused) as caught:
            v2host.prepare_v2_session(
                {}, status={}, run_async=None, camilla_factory=None,
                verify_only=preparer == "verify",
            )
        assert caught.value.code == CODE_WIRED_MIC_MISSING
    finally:
        v2host.set_state_path_for_tests(None)


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
    complete = threading.Event()
    retake = threading.Event()

    assert v2host._build_wired_run(
        "conductor",
        volume="vol", stop_event=threading.Event(), stop_lock=threading.Lock(),
        position_gate=None, evidence_refs={},
        ceiling_s=42.0, complete_event=complete, retake_event=retake,
    ) == "wired-run"
    assert built["ceiling_s"] == 42.0
    assert built["complete_event"] is complete
    assert built["retake_event"] is retake


# --------------------------------------------------------------------------- #
# 3. the wired runner (fake conductor)
# --------------------------------------------------------------------------- #

class VolumeRecorder:
    def __init__(self, open_result="opened"):
        self.events: list[str] = []
        self._open_result = open_result

    def hooks(self):
        async def _open():
            self.events.append("open")
            return self._open_result

        async def _close():
            self.events.append("close")

        async def _abandon():
            self.events.append("abandon")

        return v2host.V2VolumeHooks(open=_open, close=_close, abandon=_abandon)


# --------------------------------------------------------------------------- #
# 3b. the per-take RETAKE (#2879) — the capture's own §2.6 terms, locally
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 4. fake-ALSA end-to-end through the REAL host consume path
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 5. hosting: the local kind + the completion endpoint
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# layer 5: the PLAY SEAM's capture half
# --------------------------------------------------------------------------- #
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


def _capture_half(tmp_path, *, factory=None):
    def _factory(rate, budget_s):
        assert rate == RATE, "the rate comes from the program that plays"
        assert budget_s > 0
        return WiredRecorder(
            "fake:pcm",
            sample_rate_hz=rate,
            channels=2,
            max_capture_s=budget_s,
            pcm_factory=lambda: FakePcm([(64, [(1000, 0)] * 64)]),
        )

    if tmp_path.is_dir() and not (tmp_path / "info.json").exists():
        (tmp_path / "info.json").write_text('{"bundle_schema_version":1}')
    return v2wired.WiredStimulusCapture(
        device=_device(),
        bundle_dir=tmp_path,
        recorder_factory=factory or _factory,
    )


async def test_the_capture_half_records_across_the_play_and_places_the_bytes(
    tmp_path,
):
    """One transaction, one answer: the path names bytes that exist.

    The ordering is the pre-roll guarantee and the reason play and capture are
    not two seams here — a recorder armed after the first sample has already
    lost the part of the answer the analysis needs most.
    """
    order: list[str] = []
    half = _capture_half(tmp_path)

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


class _FakeAbortTarget:
    """The pause's abort target: latched or not, registrations recorded."""

    def __init__(self, failed=False):
        self.failed = failed
        self.registered: list = []
        self.cleared = 0

    def register(self, task):
        self.registered.append(task)

    def clear(self):
        self.cleared += 1


@pytest.mark.parametrize("when", ["before", "during", "none"])
def test_plan_host_registers_and_honors_the_window_abort(monkeypatch, when):
    from jasper.measurement_window import MeasurementWindowError

    target = _FakeAbortTarget(failed=when == "before")
    monkeypatch.setattr(v2host, "_session_abort_target", target)
    runner, session, fakes, _, _, _ = _plan_host(monkeypatch)
    if when == "during":
        async def aborted(spec):
            target.failed = True
            raise asyncio.CancelledError()
        monkeypatch.setattr(session, "measure", aborted)
    async def drive():
        if when == "none":
            await runner(session)
        else:
            with pytest.raises(MeasurementWindowError):
                await runner(session)
    asyncio.run(drive())
    assert not session.is_open
    if when == "before":
        assert fakes.play.calls == []
    else:
        assert target.registered
        assert target.cleared == len(target.registered)


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
    monkeypatch.setattr(v2host, "_state_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(v2host.time, "time", lambda: 200.0)
    v2host.save_v2_state({"session_id": "s1", "updated_at": 1.0,
                          "verify": {"outcome": "pass", "code": "verified"}})
    assert v2host.load_v2_state()["updated_at"] == 200.0
    assert v2host._persist_terminal_failure(SimpleNamespace(session_id="s1"), "internal_error")
    state = v2host.load_v2_state()
    assert state["verify"] == {"outcome": "pass", "code": "verified"}
    assert state["execution"]["cleanup_fault_code"] == "internal_error"


def _plan_host(monkeypatch, *, gate=None, signals=None):
    from dataclasses import replace
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.run_manifest import RunManifest
    from tests.engine_twin import FakeSeams as EngineSeams, tuning_session
    from tests.test_plan_run import _Store, _analysis, _walk, _SCOPES
    from tests.crossover_v2_fixtures import _conductor, FakeSeams as FlowSeams

    fakes, flow = EngineSeams(), FlowSeams()
    manifest = RunManifest("host-run", _Store(fakes.records))
    session, _ = tuning_session(replace(fakes, records=manifest), session_id=manifest.run_id)
    conductor = _conductor(flow)
    control = signals or plan_run.RunSignals()
    monkeypatch.setattr(v2host, "persist_conductor_state", lambda *a, **k: None)
    monkeypatch.setattr(v2host, "_persist_terminal_failure", lambda *a, **k: None)
    monkeypatch.setattr(v2host, "_persist_execution_result", lambda *a, **k: None)
    runner = v2wired.build_v2_wired_run_and_consume(
        conductor, volume=v2host.V2VolumeHooks(session.open, session.close, session.close),
        stop_event=control.stop, stop_lock=threading.Lock(), ceiling_s=30,
        complete_event=control.complete, retake_event=control.retake,
        tuning=session, manifest=manifest, request=_walk([0, 20]), captures=None,
        analyze=_analysis, assessor=None, candidate_scopes=_SCOPES, spl_monitor="test",
        position_gate=gate,
    )
    return runner, session, fakes, manifest, control, flow


def test_plan_host_banks_captures_and_publishes_no_candidate(monkeypatch):
    from tests.test_plan_run import AnsweredGate

    gate = AnsweredGate()
    runner, session, fakes, manifest, _, flow = _plan_host(monkeypatch, gate=gate)
    asyncio.run(runner(session))
    assert manifest.status == "complete"
    assert manifest.takes_measured == 2
    assert len(gate.grants) == 2
    assert fakes.graph.restores == fakes.volume.releases == 1
    assert flow.published_candidates == []


@pytest.mark.parametrize("signal", ["complete", "stop"])
def test_plan_host_controls_drain_the_session(monkeypatch, signal):
    runner, session, fakes, manifest, signals, _ = _plan_host(monkeypatch)
    getattr(signals, signal).set()
    async def drive():
        if signal == "stop":
            with pytest.raises(CaptureStopped):
                await runner(session)
        else:
            await runner(session)
    asyncio.run(drive())
    assert fakes.play.calls == []
    assert fakes.volume.releases == fakes.graph.restores == 1
    assert manifest.finalized


async def test_plan_host_waits_for_the_gate_before_admission_and_capture(monkeypatch):
    from jasper.active_speaker.crossover_v2.position_gate import PositionGate
    from jasper.active_speaker import plan_run

    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    gate = PositionGate()
    runner, session, fakes, manifest, signals, _ = _plan_host(monkeypatch, gate=gate)
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
    assert fakes.volume.releases == 1


async def test_host_retake_uses_the_run_ledger_once_and_returns_to_the_gate(monkeypatch):
    from jasper.active_speaker import plan_run
    from jasper.active_speaker.crossover_v2.capture_source import CaptureBeginDeferred
    from tests.test_plan_run import AnsweredGate

    signals = plan_run.RunSignals()
    monkeypatch.setattr(plan_run, "POSITION_HOLD_POLL_S", 0)
    class RetakingGate(AnsweredGate):
        requested = False
        def gate(self, index, attempt, entry):
            if index == 2 and not self.requested:
                self.requested = True
                signals.retake.set()
                raise CaptureBeginDeferred("awaiting_position", "placement")
            return super().gate(index, attempt, entry)
    gate = RetakingGate()
    runner, session, fakes, manifest, _, _ = _plan_host(monkeypatch, gate=gate, signals=signals)
    await runner(session)
    assert manifest.status == "complete"
    assert manifest.takes_measured == 3
    assert [call[0] for call in gate.grants] == [1, 1, 2]
    assert max(progress["budget"]["by_household"] for progress in gate.progress) == 1
    assert fakes.volume.releases == fakes.graph.restores == 1


@pytest.mark.parametrize("phase", ["check", "measure", "verify"])
@pytest.mark.parametrize("clipped_take", [False, True])
def test_host_binds_assessment_and_applies_its_retry_level(phase, clipped_take):
    from dataclasses import replace
    from jasper.active_speaker.crossover_v2.measure_spec import MeasureSpec
    from jasper.audio_measurement.program import STIMULUS_KINDS
    from jasper.web.correction_plan_capture import bind_plan_analysis, compose_plan_program
    from tests.crossover_v2_fixtures import FakeSeams, _conductor, _check_analysis, _measure_analysis, _verify_analysis

    factory = {"check": _check_analysis, "measure": _measure_analysis, "verify": _verify_analysis}[phase]
    def clipped(program):
        analysis = factory(program)
        return replace(analysis, locations=tuple(replace(loc, clipped=clipped_take) for loc in analysis.locations))
    fakes = FakeSeams(**{phase: clipped})
    conductor = _conductor(fakes, index_phase_map={1: phase},
                           gain_plan_db={"woofer": -11.0, "tweeter": -13.0})
    analyze, assessor = bind_plan_analysis(conductor, SimpleNamespace(enrich=None),
        manifest=SimpleNamespace(calibration={}), evidence={}, verify_only=phase == "verify")
    spec = MeasureSpec(kind="baseline", graph_scope="speaker_tune" if phase == "verify" else "drivers", program_phase=phase)
    gain = None
    for attempt in range(1, 4 if clipped_take else 2):
        program = compose_plan_program(conductor, spec, gain)
        peak = max(seg.gain_db for seg in program.segments if seg.kind in STIMULUS_KINDS)
        if gain is not None:
            assert peak == pytest.approx(gain)
        analysis = analyze({"index": 1, "attempt": attempt, "program": program.to_dict()}, "take")
        verdict = assessor(analysis, phase=phase, program=program)
        if clipped_take:
            assert verdict.fault == "clipped"
            assert verdict.next == "retake_quieter"
            assert verdict.charge == "speaker"
            gain = verdict.next_gain_db
            assert gain < peak
        else:
            assert verdict.ok
    assert fakes.published_candidates == []
