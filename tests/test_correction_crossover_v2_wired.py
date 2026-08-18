# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The WIRED capture provider's promises (#2662 W2b).

Four layers, least to most integrated:

1. **Source resolution** — auto picks wired exactly when a measurement-class
   mic is present; explicit overrides are honored, and an impossible override
   fails loudly rather than silently measuring on a source nobody chose.
2. **The host's fork** — one resolved decision drives the mint and the
   runner; the wired runner gets the wired extras, the relay runner keeps the
   phone-phase signal, and the host's refusal translation reaches the tap.
3. **The wired runner** — drives the conductor conversation (gate → admission
   → capture-while-play → consume) with the relay's own error mapping minus
   everything phone-shaped; the walked-away volume guarantee holds on every
   exit; wired deaths never persist a transport claim.
4. **The fake-ALSA end-to-end** — extends the #2701 wired-readiness pin from
   a contract-only answer to a REAL engine capture consumed through the REAL
   host path: a real ``CrossoverV2Session`` (recovery re-verify shape), the
   real ``bind_production_analyze`` binding decoding the provider's own
   32-bit 48 kHz WAV, the real calibration-resolver injection point, and the
   real durable-state persist.

Equal-or-more scrutiny is pinned here as behavior: every wired answer carries
all four frame-ledger counters with real values (so the analyzer's frame
checks EVALUATE — a wired capture can never pass on "not evaluated"), plus
the re-homed zero-run disclosure.
"""
from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace

import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
    CaptureAnswer,
    SOURCE_RELAY,
    SOURCE_WIRED,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_DONE
from jasper.active_speaker.crossover_v2.vocabulary import (
    REASON_INTERNAL_ERROR,
    REASON_SESSION_CEILING_EXPIRED,
    REASON_VERIFY_INCONCLUSIVE,
)
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError,
    WiredMicDevice,
    WiredRecorder,
)
from jasper.capture_relay.session import (
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureFailed,
    CaptureStopped,
)
from jasper.capture_relay.spec import CapturePlan, CapturePlanEntry
from jasper.web import correction_crossover_v2 as v2host
from jasper.web import correction_crossover_v2_wired as v2wired

from tests.test_wired_capture import FakePcm, UMIK2_USB_ID, _make_card

RATE = 48_000


@pytest.fixture(autouse=True)
def _fast_paced(monkeypatch):
    """Test pacing: the production post-roll (1.0 s) and retry settle (3.0 s)
    are budget allowances for a real room, not something a fake PCM needs."""
    monkeypatch.setattr(v2wired, "WIRED_POST_ROLL_S", 0.01)
    monkeypatch.setattr(v2wired, "WIRED_RETRY_SETTLE_S", 0.01)


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


def test_auto_selects_wired_when_a_measurement_mic_is_present(tmp_path):
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    source, device = v2wired.resolve_v2_capture_source(
        {}, proc_asound=tmp_path
    )
    assert source == SOURCE_WIRED
    assert device is not None and device.model_key == "minidsp_umik2"


def test_auto_selects_relay_when_no_measurement_mic_is_present(tmp_path):
    source, device = v2wired.resolve_v2_capture_source(
        {}, proc_asound=tmp_path
    )
    assert (source, device) == (SOURCE_RELAY, None)


def test_explicit_relay_wins_even_with_a_mic_present(tmp_path):
    """Disclose-and-recommend, never nanny: the phone flow stays fully
    usable when chosen."""
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    source, device = v2wired.resolve_v2_capture_source(
        {"JASPER_CAPTURE_SOURCE": "relay"}, proc_asound=tmp_path
    )
    assert (source, device) == (SOURCE_RELAY, None)


def test_explicit_wired_without_a_mic_refuses_loudly(tmp_path):
    with pytest.raises(WiredCaptureError, match="no measurement-class"):
        v2wired.resolve_v2_capture_source(
            {"JASPER_CAPTURE_SOURCE": "wired"}, proc_asound=tmp_path
        )


def test_an_unrecognized_override_refuses_rather_than_guessing(tmp_path):
    with pytest.raises(WiredCaptureError, match="unrecognized"):
        v2wired.resolve_v2_capture_source(
            {"JASPER_CAPTURE_SOURCE": "phone"}, proc_asound=tmp_path
        )


def test_auto_spelling_is_accepted(tmp_path):
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    source, _device = v2wired.resolve_v2_capture_source(
        {"JASPER_CAPTURE_SOURCE": "auto"}, proc_asound=tmp_path
    )
    assert source == SOURCE_WIRED


# --------------------------------------------------------------------------- #
# 2. the mint + the host fork
# --------------------------------------------------------------------------- #


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
    assert opened.tap_link == ""
    # The 48 kHz pin reaches the wired path through the same validate the
    # relay registration runs.
    assert opened.pi_session.spec.sample_rate_hz == RATE
    assert opened.pi_session.device.card_id == "UMIK2"


def test_open_wired_capture_refuses_an_invalid_spec():
    import dataclasses

    from jasper.capture_relay.spec import CaptureSpecError

    bad = dataclasses.replace(_real_verify_spec(), sample_rate_hz=44_100)
    with pytest.raises(CaptureSpecError):
        v2wired.open_wired_capture(bad, device=_device())


def test_two_wired_sessions_mint_distinct_identities():
    spec = _real_verify_spec()
    first = v2wired.open_wired_capture(spec, device=_device())
    second = v2wired.open_wired_capture(spec, device=_device())
    assert first.pi_session.session_id != second.pi_session.session_id


def test_the_wired_answer_satisfies_the_seam_contract():
    answer = v2wired.WiredCaptureAnswer(wav=b"")
    assert isinstance(answer, CaptureAnswer)


def test_resolve_prepare_capture_source_translates_to_a_refusal(monkeypatch):
    def _boom(*args, **kwargs):
        raise WiredCaptureError("JASPER_CAPTURE_SOURCE=wired but no mic")

    monkeypatch.setattr(v2wired, "resolve_v2_capture_source", _boom)
    with pytest.raises(v2host.CrossoverV2Refused, match="no mic"):
        v2host._resolve_prepare_capture_source()


def test_mint_source_session_forks_on_the_source(monkeypatch):
    minted = []
    monkeypatch.setattr(
        v2wired, "open_wired_capture",
        lambda spec, device: minted.append(("wired", spec, device)) or "wired-rc",
    )
    import jasper.capture_relay.correction_adapter as adapter

    monkeypatch.setattr(
        adapter, "open_capture",
        lambda client, spec, **kw: minted.append(("relay", kw["ttl_s"])) or "relay-rc",
    )
    device = _device()
    rc = v2host._mint_source_session(
        SOURCE_WIRED, device, None, "spec",
        base="", capture_origin="", return_url="", plan_shape=None,
        ceiling_s=100.0,
    )
    assert rc == "wired-rc"
    assert minted == [("wired", "spec", device)]

    minted.clear()
    rc = v2host._mint_source_session(
        SOURCE_RELAY, None, object(), "spec",
        base="https://relay.test", capture_origin="cap.test",
        return_url="", plan_shape=None, ceiling_s=100.0,
    )
    assert rc == "relay-rc"
    # The relay mint keeps its TTL policy (default for a shape-less session).
    from jasper.capture_relay.session import DEFAULT_TTL_S

    assert minted == [("relay", DEFAULT_TTL_S)]


def test_build_source_run_gives_each_provider_its_own_extras(monkeypatch):
    built = {}

    def _wired_builder(conductor, **kw):
        built["wired"] = kw
        return "wired-run"

    def _relay_builder(conductor, **kw):
        built["relay"] = kw
        return "relay-run"

    monkeypatch.setattr(v2wired, "build_v2_wired_run_and_consume", _wired_builder)
    monkeypatch.setattr(v2host, "build_v2_run_and_consume", _relay_builder)
    device = _device()
    complete = threading.Event()
    signal = object()
    common = dict(
        volume="vol", stop_event=threading.Event(), stop_lock=threading.Lock(),
        position_gate=None, evidence_refs={}, playback_started=signal,
        wired_device=device, ceiling_s=42.0, complete_event=complete,
    )
    assert v2host._build_source_run(SOURCE_WIRED, "conductor", **common) == "wired-run"
    assert built["wired"]["device"] is device
    assert built["wired"]["ceiling_s"] == 42.0
    assert built["wired"]["complete_event"] is complete
    assert "playback_started" not in built["wired"]

    assert v2host._build_source_run(SOURCE_RELAY, "conductor", **common) == "relay-run"
    assert built["relay"]["playback_started"] is signal
    assert "device" not in built["relay"]


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


class FakeConductor:
    """Records the provider conversation; scripts verdicts per consume."""

    def __init__(self, verdicts=None, *, awaiting_confirm=False,
                 on_armed=None, authorize=None):
        self.events: list = []
        self.answers: list = []
        self.last_failure_code = None
        self._verdicts = list(verdicts or [])
        self._awaiting = awaiting_confirm
        self._on_armed = on_armed
        self._authorize = authorize
        self._accepted = 0
        self._target = None

    def authorize_begin(self, index, attempt, entry=None):
        self.events.append(("authorize", index, attempt))
        if self._authorize is not None:
            self._authorize(index, attempt, entry)

    def on_armed(self, state=None):
        self.events.append(("on_armed",))
        if self._on_armed is not None:
            self._on_armed()

    def consume_capture(self, index, attempt, answer, entry=None):
        self.answers.append(answer)
        verdict = self._verdicts.pop(0) if self._verdicts else {"accepted": True}
        self.events.append(("consume", index, attempt, dict(verdict)))
        if verdict.get("accepted"):
            self._accepted += 1
        return verdict

    def cloud_measure_group_awaiting_confirm(self):
        return self._awaiting

    def note_group_close_started(self):
        self.events.append(("close_started",))

    def confirm_cloud_measure_group(self):
        self.events.append(("confirm",))
        self._awaiting = False
        return {"fit": "banked"}

    @property
    def current_phase(self):
        return PHASE_DONE if not self._verdicts and self._accepted else "check"


def _plan(target=1, max_attempts=3, duration_ms=200):
    return CapturePlan(
        capture_target=target,
        max_attempts=max_attempts,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(
                index=i, kind_label="verify", duration_ms=duration_ms,
            )
            for i in range(target)
        ),
    )


def _wired_session(plan):
    return SimpleNamespace(
        session_id="wired-test",
        spec=SimpleNamespace(capture_plan=plan, sample_rate_hz=RATE),
    )


def _recorder_factory(script=None):
    def _factory(max_capture_s):
        return WiredRecorder(
            "fake:pcm",
            sample_rate_hz=RATE,
            channels=2,
            max_capture_s=max_capture_s,
            pcm_factory=lambda: FakePcm(
                script or [(64, [(1000, 0)] * 64)]
            ),
        )

    return _factory


def _build(conductor, volume, *, plan=None, monkeypatch=None, persists=None,
           terminal=None, **kwargs):
    """The wired runner over a fake conductor, host persistence spied.

    Persistence is patched ON THE HOST MODULE — which is itself a pin of the
    provider's late-binding promise: a double patched there must be honored
    from this side of the seam.
    """
    if monkeypatch is not None:
        if persists is not None:
            monkeypatch.setattr(
                v2host, "persist_conductor_state",
                lambda c, *, failure_code=None, evidence=None:
                    persists.append(failure_code),
            )
        if terminal is not None:
            monkeypatch.setattr(
                v2host, "_persist_terminal_failure",
                lambda c, code, refusals=(): terminal.append(code) or False,
            )
        monkeypatch.setattr(
            v2host, "_start_speculative_group_close",
            lambda c: None,
        )
    kwargs.setdefault("stop_event", threading.Event())
    kwargs.setdefault("stop_lock", threading.Lock())
    kwargs.setdefault("device", _device())
    kwargs.setdefault("ceiling_s", 30.0)
    kwargs.setdefault("complete_event", threading.Event())
    kwargs.setdefault("recorder_factory", _recorder_factory())
    kwargs.setdefault("poll_interval_s", 0.01)
    return v2wired.build_v2_wired_run_and_consume(
        conductor, volume=volume.hooks(), **kwargs
    )


def _run(runner, plan=None):
    session = _wired_session(plan if plan is not None else _plan())
    return asyncio.run(runner(None, session))


def test_happy_walk_drives_the_conversation_in_order(monkeypatch):
    persists = []
    conductor = FakeConductor()
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch, persists=persists)
    _run(runner)
    # 1-based wire index space, admission before play, consume after.
    assert conductor.events == [
        ("authorize", 1, 1),
        ("on_armed",),
        ("consume", 1, 1, {"accepted": True}),
    ]
    # The §5.5 lifecycle: open before any capture, exact close on done.
    assert volume.events == ["open", "close"]
    # Persisted twice: once by consume (clean), once by the done path.
    assert persists == [None, None]


def test_the_recorder_is_live_before_the_program_plays(monkeypatch):
    """The pre-roll guarantee at the runner level: by the time on_armed runs
    (the host is about to play), the capture engine has already banked
    audio."""
    live_frames = []
    recorders = []

    factory = _recorder_factory()

    def _tracking_factory(max_capture_s):
        recorder = factory(max_capture_s)
        recorders.append(recorder)
        return recorder

    conductor = FakeConductor(
        on_armed=lambda: live_frames.append(recorders[-1]._frames)
    )
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        recorder_factory=_tracking_factory,
    )
    _run(runner)
    assert live_frames and live_frames[0] >= 64


def test_the_answer_is_a_capture_answer_with_graded_counters(monkeypatch):
    conductor = FakeConductor()
    runner = _build(conductor, VolumeRecorder(), monkeypatch=monkeypatch,
                    persists=[])
    _run(runner)
    (answer,) = conductor.answers
    assert isinstance(answer, CaptureAnswer)
    report = answer.capture_integrity
    # EQUAL-OR-MORE SCRUTINY: all four counters are REPORTED with real
    # values, so the analyzer's frame checks evaluate — never
    # "not evaluated" — and the clean chain balances the real ledger.
    for key in INTEGRITY_COUNTER_KEYS:
        assert isinstance(report[key], int)
    ledger = reconcile_capture_frames(
        report, received_frames=report["encoded_frames"]
    )
    assert ledger.balance_evaluated and ledger.render_gap_evaluated
    assert ledger.balanced
    assert report["zero_run_quantum"] == 128
    # Device identity for the calibration mismatch guard + forensics.
    assert answer.device["model_key"] == "minidsp_umik2"
    assert answer.device["wired"] is True
    assert answer.device["channel_selected"] == 0  # signal was on channel 0
    assert answer.device["usb_id"] == UMIK2_USB_ID


def test_a_dropout_in_the_capture_reaches_the_answer_as_a_zero_run(monkeypatch):
    """The re-homed #2557 detector is WIRED THROUGH: a ≥128-exact-zero hole
    in the selected channel's audio arrives on the answer's integrity report,
    where the retained sidecar (and any future grader) reads it."""
    hole = [(0, 0)] * 128
    signal = [(1000, 0)] * 64
    conductor = FakeConductor()
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        recorder_factory=_recorder_factory(
            [(64, signal), (128, hole), (64, signal)]
        ),
    )
    _run(runner)
    (answer,) = conductor.answers
    report = answer.capture_integrity
    # The idle tail is silence too, so the hole plus the tail yield >= 1 run;
    # the FIRST recorded run is the injected hole, at its exact offset.
    assert report["zero_run_count"] >= 1
    assert report["zero_runs"][0]["offset"] == 64
    assert report["zero_runs"][0]["len"] >= 128


def test_a_rejected_verdict_retries_the_same_index_next_attempt(monkeypatch):
    conductor = FakeConductor(verdicts=[
        {"accepted": False, "code": REASON_VERIFY_INCONCLUSIVE},
        {"accepted": True},
    ])
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch, persists=[])
    _run(runner)
    pairs = [(e[1], e[2]) for e in conductor.events if e[0] == "authorize"]
    assert pairs == [(1, 1), (1, 2)]
    assert volume.events == ["open", "close"]


def test_a_terminal_verdict_ends_the_walk_immediately(monkeypatch):
    persists = []
    conductor = FakeConductor(verdicts=[
        {"accepted": False, "code": "x", "terminal": True},
        {"accepted": True},  # must never be reached
    ])
    conductor.last_failure_code = "x"
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch, persists=persists,
                    plan=None)
    _run(runner, plan=_plan(target=2, max_attempts=5))
    consumed = [e for e in conductor.events if e[0] == "consume"]
    assert len(consumed) == 1
    # Not done ⇒ the walked-away volume is abandoned, never closed.
    assert volume.events == ["open", "abandon"]


def test_attempt_budget_exhaustion_ends_the_set_without_raising(monkeypatch):
    persists = []
    conductor = FakeConductor(verdicts=[
        {"accepted": False, "code": "locate_failed"},
        {"accepted": False, "code": "locate_failed"},
    ])
    conductor.last_failure_code = "locate_failed"
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch,
                    persists=persists)
    _run(runner, plan=_plan(target=1, max_attempts=2))
    assert volume.events == ["open", "abandon"]
    # The post-walk persist carries the conductor's own last failure.
    assert persists[-1] == "locate_failed"


def test_a_position_gate_hold_is_retried_until_released(monkeypatch):
    deferrals = {"left": 2}

    class Gate:
        def gate(self, index, attempt, entry):
            if deferrals["left"] > 0:
                deferrals["left"] -= 1
                raise CaptureBeginDeferred("awaiting_position", "hold")

    conductor = FakeConductor()
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=Gate(),
    )
    _run(runner)
    assert deferrals["left"] == 0
    assert ("authorize", 1, 1) in conductor.events


def test_a_conductor_deferral_is_bounded_by_the_session_ceiling(monkeypatch):
    """The retained VERIFY hold (D10) defers from the CONDUCTOR, which no
    gate bounds — the runner's own ceiling must end it, with the honest
    cumulative code, so no wait in this runner is unbounded."""
    terminal = []

    def _defer_forever(index, attempt, entry):
        raise CaptureBeginDeferred("awaiting_apply", "hold")

    conductor = FakeConductor(authorize=_defer_forever)
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch, persists=[],
                    terminal=terminal, ceiling_s=0.05)
    with pytest.raises(CaptureBeginRefused) as caught:
        _run(runner)
    assert caught.value.code == REASON_SESSION_CEILING_EXPIRED
    assert terminal == [REASON_SESSION_CEILING_EXPIRED]
    assert volume.events == ["open", "abandon"]


def test_stop_abandons_the_volume_and_raises_stopped(monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    volume = VolumeRecorder()
    runner = _build(
        FakeConductor(), volume, monkeypatch=monkeypatch, persists=[],
        stop_event=stop_event,
    )
    with pytest.raises(CaptureStopped):
        _run(runner)
    assert volume.events == ["open", "abandon"]


def test_a_refusal_persists_the_conductors_own_code(monkeypatch):
    terminal = []

    def _refuse(index, attempt, entry):
        raise CaptureBeginRefused("cloud_geometry_locked", "no")

    conductor = FakeConductor(authorize=_refuse)
    conductor.last_failure_code = "cloud_geometry_locked"
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch,
                    persists=[], terminal=terminal)
    with pytest.raises(CaptureBeginRefused):
        _run(runner)
    assert terminal == ["cloud_geometry_locked"]
    assert volume.events == ["open", "abandon"]


def test_a_wired_death_never_persists_a_transport_claim(monkeypatch):
    """The relay's REASON_RELAY_TIMEOUT fallback is deliberately not
    mirrored: an unregistered refusal with no conductor code degrades to
    internal_error — there is no link to blame."""
    terminal = []

    def _refuse(index, attempt, entry):
        raise CaptureBeginRefused("some_future_code", "no")

    conductor = FakeConductor(authorize=_refuse)
    runner = _build(conductor, VolumeRecorder(), monkeypatch=monkeypatch,
                    persists=[], terminal=terminal)
    with pytest.raises(CaptureBeginRefused):
        _run(runner)
    assert terminal == [REASON_INTERNAL_ERROR]


def test_a_local_seam_oserror_lands_in_the_internal_arm(monkeypatch):
    """Finding G's boundary, kept: a play-path OSError is wrapped as
    CrossoverV2LocalSeamError and persists internal_error."""
    terminal = []

    def _explode():
        raise OSError(30, "Read-only file system")

    conductor = FakeConductor(on_armed=_explode)
    volume = VolumeRecorder()
    runner = _build(conductor, volume, monkeypatch=monkeypatch,
                    persists=[], terminal=terminal)
    with pytest.raises(v2host.CrossoverV2LocalSeamError):
        _run(runner)
    assert terminal == [REASON_INTERNAL_ERROR]
    assert volume.events == ["open", "abandon"]


def test_a_capture_engine_failure_cleans_up_and_persists(monkeypatch):
    """The mic vanishing mid-session is a wired-specific death: the loud
    engine error re-raises through the catch-all with internal_error
    persisted and the volume drained (the specific cause stays on the
    journal; a named household code is W3's)."""
    terminal = []

    def _dead_factory(max_capture_s):
        raise WiredCaptureError("could not open fake:pcm")

    volume = VolumeRecorder()
    runner = _build(
        FakeConductor(), volume, monkeypatch=monkeypatch, persists=[],
        terminal=terminal, recorder_factory=_dead_factory,
    )
    with pytest.raises(WiredCaptureError):
        _run(runner)
    assert terminal == [REASON_INTERNAL_ERROR]
    assert volume.events == ["open", "abandon"]


def test_program_failures_keep_their_own_honest_code(monkeypatch):
    """The host's ONE classifier is consulted — a play refusal persists its
    program code, not internal_error."""
    from jasper.active_speaker.crossover_v2_flow import CrossoverV2FlowError

    terminal = []

    def _explode():
        raise CrossoverV2FlowError("no program for phase")

    conductor = FakeConductor(on_armed=_explode)
    runner = _build(conductor, VolumeRecorder(), monkeypatch=monkeypatch,
                    persists=[], terminal=terminal)
    with pytest.raises(CrossoverV2FlowError):
        _run(runner)
    classified = v2host.classify_program_failure(
        CrossoverV2FlowError("no program for phase")
    )
    assert classified is not None
    assert terminal == [classified[0]]


def test_volume_that_does_not_open_refuses_before_any_capture():
    conductor = FakeConductor()
    volume = VolumeRecorder(open_result="drained")
    runner = _build(conductor, volume)
    with pytest.raises(CaptureFailed, match="could not be confirmed"):
        _run(runner)
    assert conductor.events == []  # nothing was authorized, nothing played


def test_held_set_waits_for_the_completion_signal_then_closes(monkeypatch):
    """Work order D1 on the wired path: the walk holds the set open until the
    local completion signal, then drives the host's ONE group-close owner
    (the real drive_group_close — persist, confirm, persist)."""
    persists = []
    monkeypatch.setattr(
        v2host, "persist_conductor_state",
        lambda c, *, failure_code=None, evidence=None:
            persists.append(failure_code),
    )
    monkeypatch.setattr(v2host, "_start_speculative_group_close", lambda c: None)
    complete_event = threading.Event()
    conductor = FakeConductor(awaiting_confirm=True)
    volume = VolumeRecorder()
    runner = _build(
        conductor, volume, complete_event=complete_event,
    )

    async def _drive():
        session = _wired_session(_plan())
        task = asyncio.create_task(runner(None, session))
        # Let the walk reach the confirm hold, then send the signal.
        await asyncio.sleep(0.15)
        complete_event.set()
        await task

    asyncio.run(_drive())
    close_events = [e for e in conductor.events if e[0] in ("close_started", "confirm")]
    assert close_events == [("close_started",), ("confirm",)]
    assert volume.events == ["open", "close"]


def test_confirm_wait_expiry_persists_the_session_ceiling_code(monkeypatch):
    """The bounded confirm wait names the honest clock: the session's own
    wall-clock ceiling — never a transport claim."""
    terminal = []
    conductor = FakeConductor(awaiting_confirm=True)
    volume = VolumeRecorder()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        terminal=terminal, ceiling_s=0.05,
    )
    with pytest.raises(CaptureBeginRefused) as caught:
        _run(runner)
    assert caught.value.code == REASON_SESSION_CEILING_EXPIRED
    assert terminal == [REASON_SESSION_CEILING_EXPIRED]
    assert volume.events == ["open", "abandon"]


def test_cancellation_drains_the_walk_before_cleanup(monkeypatch):
    """Stop-by-cancel mirrors the relay's shielded drain: the worker walk
    finishes its capture before the volume is abandoned."""
    persists = []
    conductor = FakeConductor()
    volume = VolumeRecorder()
    started = threading.Event()
    release = threading.Event()

    def _slow_armed():
        started.set()
        release.wait(timeout=5)

    conductor._on_armed = _slow_armed
    runner = _build(conductor, volume, monkeypatch=monkeypatch, persists=persists)

    async def _drive():
        session = _wired_session(_plan())
        task = asyncio.create_task(runner(None, session))
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    # The capture that was in flight completed and was consumed before
    # cleanup — the drain, not a mid-capture abort.
    assert any(e[0] == "consume" for e in conductor.events)
    assert volume.events[-1] == "abandon"


# --------------------------------------------------------------------------- #
# 4. fake-ALSA end-to-end through the REAL host consume path
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _isolated_state(tmp_path, monkeypatch):
    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_MODEL_ERROR_PATH",
        str(tmp_path / "model_error.json"),
    )
    yield
    v2host.set_state_path_for_tests(None)


def test_end_to_end_wired_session_through_the_real_host_consume_path(
    _isolated_state, monkeypatch,
):
    """The #2701 wired-readiness pin, extended to a full fake-ALSA session.

    A REAL ``CrossoverV2Session`` in the recovery re-verify shape (the
    1-entry map ``prepare_v2_verify`` builds) is driven by the wired runner
    over a fake ALSA device. The analyze seam is the REAL
    ``bind_production_analyze`` — it decodes the provider's own 32-bit
    48 kHz WAV with the production decoder (proving the format path), threads
    the engine's integrity report as ``capture_report``, and resolves the
    calibration through the injected resolver with the WIRED setup/device
    pair. Durable state is the real persist to a tmp path.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        CrossoverV2Session,
        V2FlowSeams,
        build_v2_verify_index_phase_map,
    )
    from jasper.audio_measurement import program_analysis as pa_mod
    from tests.crossover_v2_fixtures import (
        CAPS,
        FC_HZ,
        SESSION_VOLUME_DB,
        _preset,
        _roles,
        _verify_analysis,
    )

    seen: dict = {}

    def _spy_analyze(program, samples, rate, *, calibration=None,
                     geometry=None, priors=None, capture_report=None):
        seen["rate"] = rate
        seen["received_frames"] = len(samples)
        seen["capture_report"] = capture_report
        seen["ledger"] = reconcile_capture_frames(
            capture_report, received_frames=len(samples)
        )
        return _verify_analysis(program)

    monkeypatch.setattr(pa_mod, "analyze_program_capture", _spy_analyze)

    resolved: list = []

    def _resolver(setup, device):
        resolved.append((setup, device))
        return None

    played: list = []
    opened = v2wired.open_wired_capture(_real_verify_spec(), device=_device())
    session = opened.pi_session
    conductor = CrossoverV2Session(
        session_id=session.session_id,
        source_preset=_preset(),
        roles_bands=_roles(),
        fc_hz=FC_HZ,
        driver_caps_dbfs=CAPS,
        session_volume_db=SESSION_VOLUME_DB,
        seams=V2FlowSeams(
            play=lambda phase, program: played.append(phase),
            analyze=v2host.bind_production_analyze(
                resolve_calibration=_resolver, meta={},
            ),
            publish_check=lambda plan, ambient: None,
            publish_candidate=lambda cand: None,
            apply_complete=v2host._applied_gate,
            apply_failed=v2host._apply_failure_gate,
            applied_boosts=lambda: False,
        ),
        index_phase_map=build_v2_verify_index_phase_map(),
        driver_spacing_m=0.15,
        accepted_phases=("check", "measure"),
        applied=True,
        post_apply_verifies=None,
    )
    volume = VolumeRecorder()
    runner = v2wired.build_v2_wired_run_and_consume(
        conductor,
        volume=volume.hooks(),
        stop_event=threading.Event(),
        stop_lock=threading.Lock(),
        device=session.device,
        ceiling_s=30.0,
        complete_event=threading.Event(),
        recorder_factory=_recorder_factory(),
        poll_interval_s=0.01,
    )
    asyncio.run(runner(None, session))

    # The program actually played while the recorder rolled.
    assert played == ["verify"]
    # The production decoder read the provider's own container: 32-bit PCM,
    # and the spec-pinned 48 kHz reached the analyzer.
    assert seen["rate"] == RATE
    assert seen["received_frames"] > 0
    # The engine's report crossed the seam and the REAL ledger evaluated it
    # clean — wired captures are graded, never waved through unevaluated.
    ledger = seen["ledger"]
    assert ledger.balance_evaluated and ledger.render_gap_evaluated
    assert ledger.balanced
    assert seen["capture_report"]["capture_chain"] == "alsa_s32le"
    # The calibration resolver — the seam's per-source injection point — got
    # the WIRED identity pair.
    (setup, device_meta), = resolved
    assert device_meta["model_key"] == "minidsp_umik2"
    # The session ran to done: exact volume close, durable state persisted.
    assert conductor.current_phase == PHASE_DONE
    assert volume.events == ["open", "close"]
    state = v2host.load_v2_state()
    assert state["session_id"] == session.session_id
    assert state["session_id"].startswith("wired-")


def test_wired_setup_reference_is_the_stored_household_shape(monkeypatch):
    hint = SimpleNamespace(
        resolvable=True, calibration_id="cal-123", model="minidsp_umik2",
    )
    monkeypatch.setattr(
        v2host, "default_setup_calibration_for_v2", lambda: hint,
    )
    setup = v2wired._wired_setup_reference(v2host)
    assert setup == {
        "calibration": {
            "mode": "stored",
            "calibration_id": "cal-123",
            "model": "minidsp_umik2",
        }
    }


def test_wired_setup_reference_is_none_when_unresolvable(monkeypatch):
    monkeypatch.setattr(
        v2host, "default_setup_calibration_for_v2",
        lambda: SimpleNamespace(resolvable=False, calibration_id="x", model="m"),
    )
    assert v2wired._wired_setup_reference(v2host) is None
    monkeypatch.setattr(
        v2host, "default_setup_calibration_for_v2", lambda: None,
    )
    assert v2wired._wired_setup_reference(v2host) is None


# --------------------------------------------------------------------------- #
# 5. hosting: the local kind + the completion endpoint
# --------------------------------------------------------------------------- #


def test_local_kind_never_constructs_a_relay_client(monkeypatch):
    from jasper.web import correction_setup
    from jasper.web._systemd import no_hold
    import jasper.capture_relay.client as relay_client_mod

    class Bomb:
        def __init__(self, *a, **k):
            raise AssertionError("a local kind must not build a RelayClient")

    monkeypatch.setattr(relay_client_mod, "RelayClient", Bomb)
    monkeypatch.setattr(correction_setup, "_ensure_loop", lambda: object())

    def _fake_run_coroutine_threadsafe(coro, loop):
        coro.close()
        return object()

    monkeypatch.setattr(
        correction_setup.asyncio,
        "run_coroutine_threadsafe",
        _fake_run_coroutine_threadsafe,
    )
    seen = {}

    def _open(client, relay_base, capture_origin, return_url):
        seen["client"] = client
        return SimpleNamespace(tap_link="", pi_session=object())

    async def _run_and_consume(client, pi_session):
        raise AssertionError("stubbed")

    correction_setup._set_relay_capture(None)
    try:
        result = correction_setup._run_relay_capture(
            correction_setup.RelayCaptureKind(
                label="crossover_v2:session",
                open=_open,
                run_and_consume=_run_and_consume,
                local=True,
            ),
            "",
            return_url="http://jts.local/correction/crossover/",
            idle_hold=no_hold,
        )
    finally:
        correction_setup._set_relay_capture(None)
    assert seen["client"] is None
    assert result["status"] == "awaiting_phone"


def _fake_handler(body: bytes = b"{}"):
    from email.message import Message

    headers = Message()
    headers["Content-Length"] = str(len(body))
    return SimpleNamespace(headers=headers, rfile=io.BytesIO(body))


def test_complete_endpoint_conflicts_when_nothing_is_waiting():
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    with pytest.raises(ValueError, match="no wired measurement"):
        correction_setup._handle_crossover_v2_complete(_fake_handler())


def test_complete_endpoint_fires_the_wired_sessions_signal():
    from jasper.web import correction_setup

    fired = []
    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture(
        "crossover_v2:session",
        request_complete=lambda: fired.append(True),
    )
    try:
        result = correction_setup._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_setup._set_relay_capture(None)
    assert result == {"ok": True}
    assert fired == [True]


def test_the_completion_signal_drops_with_the_slot():
    from jasper.web import correction_setup

    correction_setup._set_relay_capture(None)
    assert correction_setup._begin_relay_capture(
        "crossover_v2:session", request_complete=lambda: None,
    )
    correction_setup._set_relay_capture(
        {"status": "complete", "kind": "crossover_v2:session"}
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_setup._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_setup._set_relay_capture(None)


def test_prepared_session_defaults_to_the_relay_source():
    """The dataclass default keeps every existing constructor call an
    unchanged relay session."""
    prepared = v2host.V2PreparedSession(
        label="x", open=lambda *a: None,
        run_and_consume=lambda c, s: None, request_stop=lambda: None,
    )
    assert prepared.capture_source == SOURCE_RELAY
    assert prepared.request_complete is None


def test_the_wired_provider_reaches_the_host_only_at_call_time():
    """The relay provider's import-shape pin, mirrored for its sibling: the
    host imports NEITHER provider lazily by accident — it must stay free to
    import this module without a cycle, so the provider's host reach-backs
    are call-time only. Asserted on a real interpreter (catches indirect
    routes a source scan cannot); the find_spec line is the positive
    control proving the absent name is a real module."""
    import subprocess
    import sys

    probe = (
        "import importlib.util, sys\n"
        "import jasper.web.correction_crossover_v2_wired\n"
        "assert 'jasper.web.correction_crossover_v2' not in sys.modules, "
        "'the wired provider imported the host at module scope'\n"
        "assert importlib.util.find_spec("
        "'jasper.web.correction_crossover_v2') is not None\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
