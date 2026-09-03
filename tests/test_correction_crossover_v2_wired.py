# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The WIRED capture provider's promises (#2662 W2b).

Five layers, least to most integrated:

1. **Mic resolution** — the measurement-class mic is picked when present, and
   absence is disclosed rather than measured around.
2. **The host's wiring** — one resolved mic drives the mint and the runner,
   and the host's refusal translation reaches the tap.
3. **The wired runner** — drives the conductor conversation (gate → admission
   → capture-while-play → consume) with the host-owned error mapping; the
   walked-away volume guarantee holds on every exit; a death never persists a
   transport claim.
4. **The fake-ALSA end-to-end** — extends the #2701 wired-readiness pin from
   a contract-only answer to a REAL engine capture consumed through the REAL
   host path: a real ``CrossoverV2Session`` (recovery re-verify shape), the
   real ``bind_production_analyze`` binding decoding the provider's own
   32-bit 48 kHz WAV, the real calibration-resolver injection point, and the
   real durable-state persist.
5. **The play seam's capture half** — the same box plays and records, so one
   stimulus is one transaction: the recorder rolls before the first sample,
   stops after the last, and the bytes land in the bundle under a path the
   engine's record can carry.

Equal-or-more scrutiny is pinned here as behavior: every wired answer carries
all four frame-ledger counters with real values (so the analyzer's frame
checks EVALUATE — a wired capture can never pass on "not evaluated"), plus
the re-homed zero-run disclosure.
"""
from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from jasper.active_speaker.crossover_v2.capture_source import (
    INTEGRITY_COUNTER_KEYS,
    CaptureAnswer,
    CaptureBeginDeferred,
    CaptureBeginRefused,
    CaptureFailed,
    CaptureStopped,
)
from jasper.active_speaker.crossover_v2.journey import PHASE_DONE
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_INTERNAL_ERROR,
    REASON_SESSION_CEILING_EXPIRED,
    REASON_VERIFY_INCONCLUSIVE,
)
from jasper.audio_measurement.frame_ledger import reconcile_capture_frames
from jasper.audio_measurement.wired_capture import (
    WiredCaptureError,
    WiredMicDevice,
    WiredRecorder,
    decode_wav_to_mono,
)
from jasper.capture_protocol import CapturePlan, CapturePlanEntry
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


def test_the_registered_mic_is_resolved_when_one_is_present(tmp_path):
    _make_card(tmp_path, 0, usbid=UMIK2_USB_ID, card_id="UMIK2")
    device = v2wired.resolve_v2_wired_mic(proc_asound=tmp_path)
    assert device.model_key == "minidsp_umik2"


def test_no_mic_discloses_instead_of_measuring_anyway(tmp_path):
    """ADR-0188: wired is THE acoustic-measurement path, so absence is
    DISCLOSED and the session refuses to guess."""
    with pytest.raises(v2wired.WiredMicMissing) as caught:
        v2wired.resolve_v2_wired_mic(proc_asound=tmp_path)
    assert caught.value.code == v2wired.CODE_WIRED_MIC_MISSING


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
    answer = v2wired.WiredCaptureAnswer(wav=b"")
    assert isinstance(answer, CaptureAnswer)


@pytest.mark.parametrize(
    "raised, expected_code",
    [
        (v2wired.WiredMicMissing, v2wired.CODE_WIRED_MIC_MISSING),
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


@pytest.mark.parametrize("preparer", ["session", "verify"])
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
            raise v2wired.WiredMicMissing("no mic")

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
        assert caught.value.code == v2wired.CODE_WIRED_MIC_MISSING
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
    device = _device()
    complete = threading.Event()
    retake = threading.Event()

    assert v2host._build_wired_run(
        "conductor",
        volume="vol", stop_event=threading.Event(), stop_lock=threading.Lock(),
        position_gate=None, evidence_refs={}, wired_device=device,
        ceiling_s=42.0, complete_event=complete, retake_event=retake,
    ) == "wired-run"
    assert built["device"] is device
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

    def consume_capture(self, index, attempt, answer):
        self.answers.append(answer)
        verdict = self._verdicts.pop(0) if self._verdicts else {"accepted": True}
        self.events.append(("consume", index, attempt, dict(verdict)))
        if verdict.get("accepted"):
            self._accepted += 1
        elif verdict.get("code"):
            # The REAL conductor stamps every rejection's code (S1's probe
            # surfaced that the shipped fake hard-coded None here, hiding the
            # precedence hazard the stamp creates).
            self.last_failure_code = verdict["code"]
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
    return asyncio.run(runner(session))


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
    """The capture's REASON_CAPTURE_TIMEOUT fallback is deliberately not
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
        task = asyncio.create_task(runner(session))
        # Let the walk reach the confirm hold, then send the signal.
        await asyncio.sleep(0.15)
        complete_event.set()
        await task

    asyncio.run(_drive())
    close_events = [e for e in conductor.events if e[0] in ("close_started", "confirm")]
    assert close_events == [("close_started",), ("confirm",)]
    assert volume.events == ["open", "close"]


def test_a_ceiling_expiry_after_a_rejection_keeps_its_own_code(monkeypatch):
    """S1's probe scenario: a prior rejection stamps ``last_failure_code``,
    and the ceiling then expires. The persisted code must be the refusal's
    own — the clock that actually ended the session — never the stale
    capture-quality claim (the shadowing the capture-style precedence
    allowed)."""
    terminal = []
    conductor = FakeConductor(
        verdicts=[{"accepted": False, "code": REASON_VERIFY_INCONCLUSIVE}],
        awaiting_confirm=True,
    )
    volume = VolumeRecorder()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        terminal=terminal, ceiling_s=0.2,
        plan=None,
    )
    with pytest.raises(CaptureBeginRefused) as caught:
        _run(runner, plan=_plan(target=1, max_attempts=3))
    # The rejection really did stamp the conductor first...
    assert conductor.last_failure_code == REASON_VERIFY_INCONCLUSIVE
    # ...and the ceiling's own registered code still won the persist.
    assert caught.value.code == REASON_SESSION_CEILING_EXPIRED
    assert terminal == [REASON_SESSION_CEILING_EXPIRED]
    assert volume.events == ["open", "abandon"]


def test_an_unregistered_refusal_falls_back_to_the_conductors_stamp(monkeypatch):
    """The fallback direction of the S1 precedence: a refusal carrying no
    registered code defers to whatever the conductor stamped."""
    terminal = []

    def _refuse(index, attempt, entry):
        raise CaptureBeginRefused("some_future_code", "no")

    conductor = FakeConductor(authorize=_refuse)
    conductor.last_failure_code = "cloud_geometry_locked"
    runner = _build(conductor, VolumeRecorder(), monkeypatch=monkeypatch,
                    persists=[], terminal=terminal)
    with pytest.raises(CaptureBeginRefused):
        _run(runner)
    assert terminal == ["cloud_geometry_locked"]


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
    """Stop-by-cancel mirrors the capture's shielded drain: the worker walk
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
        task = asyncio.create_task(runner(session))
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
# 3b. the per-take RETAKE (#2879) — the capture's own §2.6 terms, locally
# --------------------------------------------------------------------------- #


class _RetakeOnHold:
    """A gate that holds ONE begin and asks for a retake while it holds.

    The wired analogue of the capture's retake window: the previous slot is
    accepted and this one has not started, so replacing the previous take is
    still meaningful. ``hold_index=None`` admits everything.
    """

    def __init__(self, retake_event, *, hold_index, refuse=None):
        self._retake = retake_event
        self._hold_index = hold_index
        self._refuse = refuse
        self.held = 0
        self.abandoned = 0

    def gate(self, index, attempt, entry):
        if self._refuse is not None and (index, attempt) == self._refuse:
            raise CaptureBeginRefused("locate_failed", "no")
        if index == self._hold_index and self.held == 0:
            self.held += 1
            self._retake.set()
            raise CaptureBeginDeferred("awaiting_position", "hold")

    def abandon_hold(self):
        """The real gate's method, so this double cannot hide the call."""
        self.abandoned += 1


def test_a_retake_asked_while_a_begin_is_held_re_opens_the_completed_slot(
    monkeypatch,
):
    """The whole contract, in one event list (the capture ``session.py`` §2.6).

    The retake names the slot that JUST COMPLETED (``index == accepted``,
    never ``accepted + 1``); it spends ONE ordinary attempt; and the accepted
    count never rewinds — capture 2 still runs afterwards and the walk
    finishes its target.

    The attempt numbers are the pin on the last of those: 1, 2, 3 with no gap.
    The held begin for slot 2 claimed attempt 2 and was never admitted, so the
    walk hands that number back; charging it would spell 1, 3, 4 and bill a
    household two attempts for one retake.
    """
    retake = threading.Event()
    conductor = FakeConductor()
    gate = _RetakeOnHold(retake, hold_index=2)
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=gate, retake_event=retake,
    )
    _run(runner, plan=_plan(target=2, max_attempts=5))

    # The abandoned begin was declared abandoned, so the gate stops
    # advertising a position nothing is measuring any more.
    assert gate.abandoned == 1
    assert [e for e in conductor.events if e[0] != "on_armed"] == [
        ("authorize", 1, 1),
        ("consume", 1, 1, {"accepted": True}),
        ("authorize", 1, 2),       # the retake: the slot that just completed
        ("consume", 1, 2, {"accepted": True}),
        ("authorize", 2, 3),       # ...and the walk carries on where it was
        ("consume", 2, 3, {"accepted": True}),
    ]


def test_a_rejected_retake_leaves_the_original_take_standing(monkeypatch):
    """"Nothing was dropped on its behalf" — the capture's own words.

    The replacement is REJECTED, so the host never replaces the retained
    position and the walk moves on to the next slot rather than re-running the
    one it already has a usable take for.
    """
    retake = threading.Event()
    conductor = FakeConductor(verdicts=[
        {"accepted": True},
        {"accepted": False, "code": "locate_failed"},
        {"accepted": True},
    ])
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=_RetakeOnHold(retake, hold_index=2), retake_event=retake,
    )
    _run(runner, plan=_plan(target=2, max_attempts=5))

    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (1, 2), (2, 3),
    ]


def test_a_retake_with_no_take_to_replace_is_dropped_by_name(monkeypatch, caplog):
    """An ask that reached a walk which has not measured anything yet.

    Never re-pointed at the capture about to run — that is a DIFFERENT spot,
    and silently retaking it would be the shape-change this ticket removed
    from the staged walk.
    """
    retake = threading.Event()
    conductor = FakeConductor()
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=_RetakeOnHold(retake, hold_index=1), retake_event=retake,
    )
    with caplog.at_level(logging.WARNING):
        _run(runner, plan=_plan(target=1, max_attempts=3))

    assert any(
        "crossover_v2_wired_retake_refused" in r.getMessage()
        and "reason=no_take_to_replace" in r.getMessage()
        for r in caplog.records
    )
    # ...and the walk it interrupted still ran, on its own first attempt.
    assert ("authorize", 1, 1) in conductor.events


def test_a_refused_retake_begin_does_not_end_the_session(monkeypatch):
    """The per-slot extras ledger running out is the ordinary way this arm is
    reached, and it must leave the household with the take they already had —
    a bonus was asked for, not a teardown."""
    terminal = []
    retake = threading.Event()
    conductor = FakeConductor()
    volume = VolumeRecorder()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        terminal=terminal, retake_event=retake,
        position_gate=_RetakeOnHold(retake, hold_index=2, refuse=(1, 2)),
    )
    _run(runner, plan=_plan(target=2, max_attempts=5))

    assert terminal == []
    assert volume.events == ["open", "close"]
    # The refused retake spent its attempt and nothing else; slot 2 followed.
    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (2, 3),
    ]


def test_a_retake_past_the_plans_attempt_budget_is_refused_not_fatal(
    monkeypatch, caplog,
):
    """The plan's own ``max_attempts`` is the only budget a retake spends —
    there is no second one to reason about, and running out keeps the set."""
    retake = threading.Event()
    conductor = FakeConductor(awaiting_confirm=True)
    volume = VolumeRecorder()
    complete_event = threading.Event()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        retake_event=retake, complete_event=complete_event,
    )

    async def _drive():
        session = _wired_session(_plan(target=1, max_attempts=1))
        task = asyncio.create_task(runner(session))
        await asyncio.sleep(0.15)
        retake.set()
        await asyncio.sleep(0.15)
        complete_event.set()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(_drive())

    assert any(
        "reason=plan_attempts_spent" in r.getMessage() for r in caplog.records
    )
    # The set still closed on the household's own signal.
    assert ("confirm",) in conductor.events


def test_a_retake_inside_the_held_set_window_re_opens_the_last_slot(monkeypatch):
    """The capture holds the set open so "the just-accepted slot is still
    retakeable on exactly the terms above" — the same sentence, locally."""
    retake = threading.Event()
    conductor = FakeConductor(awaiting_confirm=True)
    volume = VolumeRecorder()
    complete_event = threading.Event()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        retake_event=retake, complete_event=complete_event,
    )

    async def _drive():
        session = _wired_session(_plan(target=1, max_attempts=4))
        task = asyncio.create_task(runner(session))
        await asyncio.sleep(0.15)
        retake.set()
        await asyncio.sleep(0.2)
        complete_event.set()
        await task

    asyncio.run(_drive())
    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (1, 2),
    ]
    assert ("confirm",) in conductor.events


def test_the_real_gate_publishes_the_retakes_own_target(monkeypatch):
    """Against the REAL PositionGate, not a double — the join a fake hides.

    Abandoning a held begin to serve a retake leaves the gate holding state
    for the begin nobody is running any more. If that is not cleared, the gate
    treats the retake's own hold as a CONTINUATION: it publishes no new
    ``pending``, so the envelope keeps naming the abandoned position, the
    person is asked for the wrong spot, and a release for the retake's index
    is refused as stale — the hold then spins until the 600 s budget kills it.
    """
    from jasper.active_speaker.crossover_v2_flow import (
        POSITION_DEG_KEY, POSITION_ROLE_KEY,
    )
    from jasper.web.correction_crossover_v2 import PositionGate

    gate = PositionGate()
    retake = threading.Event()
    plan = CapturePlan(
        capture_target=2,
        max_attempts=5,
        schema_version=2,
        entries=tuple(
            CapturePlanEntry(
                index=i, kind_label="verify", duration_ms=200,
                screen={
                    POSITION_DEG_KEY: str(i * 22),
                    POSITION_ROLE_KEY: "onax",
                },
            )
            for i in range(2)
        ),
    )
    seen: list = []

    abandoned_hold_had_to_be_released = []

    def _release_in_the_background() -> None:
        """Play the person: release what the gate asks for, except once.

        At capture 2's hold — the moment capture 1 is banked and nothing new
        has started — they decide take 1 was bad and ask for a retake instead
        of walking on. They then refuse to release capture 2's position,
        because they are not standing there: they are walking back to take 1's
        spot. If the gate is still advertising the abandoned hold, that refusal
        is the deadlock, and the fallback below records it before breaking it
        so the test fails on the FACT rather than on a wall-clock timeout.
        """
        asked_at = None
        while not stop.is_set():
            pending = gate.pending()
            if pending is None:
                stop.wait(0.01)
                continue
            key = (pending["index"], pending["attempt"])
            if not seen or seen[-1] != key:
                seen.append(key)
            if key == (2, 2) and asked_at is None:
                asked_at = time.monotonic()
                retake.set()
                stop.wait(0.02)
                continue
            if key == (2, 2) and (1, 2) not in seen:
                if time.monotonic() - asked_at < 0.5:
                    stop.wait(0.02)
                    continue
                abandoned_hold_had_to_be_released.append(key)
            gate.release(pending["index"])
            stop.wait(0.01)

    stop = threading.Event()
    conductor = FakeConductor()
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=gate, retake_event=retake,
    )
    releaser = threading.Thread(target=_release_in_the_background, daemon=True)
    releaser.start()
    try:
        _run(runner, plan=plan)
    finally:
        stop.set()
        releaser.join(timeout=5)

    # The retake's own (index, attempt) reached the person, and it did so
    # WITHOUT them first having to release a position the walk had abandoned.
    assert abandoned_hold_had_to_be_released == []
    assert seen == [(1, 1), (2, 2), (1, 2), (2, 3)]
    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (1, 2), (2, 3),
    ]


def test_asking_again_while_the_retake_is_held_keeps_waiting(monkeypatch):
    """A double-POST during the retake's OWN hold names the same slot.

    It cannot mean anything else — the slot being re-opened is the only one a
    retake can name — so the walk keeps waiting for the release. Swallowed
    inside the retake rather than propagated: this path runs from inside the
    walk's own handler for that signal, so an escape would have nothing to
    catch it and would end a perfectly healthy session on ``internal_error``.
    """
    terminal = []
    retake = threading.Event()

    class Gate:
        def __init__(self):
            self.holds = 0
            self.abandoned = 0

        def gate(self, index, attempt, entry):
            if index == 2 and self.holds == 0:
                self.holds += 1
                retake.set()
                raise CaptureBeginDeferred("awaiting_position", "hold")
            if (index, attempt) == (1, 2) and self.holds == 1:
                # The retake's own begin, held once — and the household taps
                # again while it waits.
                self.holds += 1
                retake.set()
                raise CaptureBeginDeferred("awaiting_position", "hold")

        def abandon_hold(self):
            self.abandoned += 1

    conductor = FakeConductor()
    volume = VolumeRecorder()
    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=[],
        terminal=terminal, position_gate=Gate(), retake_event=retake,
    )
    _run(runner, plan=_plan(target=2, max_attempts=5))

    assert terminal == []
    assert volume.events == ["open", "close"]
    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (1, 2), (2, 3),
    ]


def test_a_session_with_no_retake_signal_is_byte_identical(monkeypatch):
    """The default: a runner built without the signal never looks for one, so
    every walk that shipped before this seam runs exactly as it did."""
    conductor = FakeConductor()
    runner = _build(
        conductor, VolumeRecorder(), monkeypatch=monkeypatch, persists=[],
        position_gate=_RetakeOnHold(threading.Event(), hold_index=2),
    )
    _run(runner, plan=_plan(target=2, max_attempts=5))
    assert [(e[1], e[2]) for e in conductor.events if e[0] == "consume"] == [
        (1, 1), (2, 2),
    ]


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
    1-entry map the verify-only prepare builds) is driven by the wired runner
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
    asyncio.run(runner(session))

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


def _fake_handler(body: bytes = b"{}"):
    from email.message import Message

    headers = Message()
    headers["Content-Length"] = str(len(body))
    return SimpleNamespace(headers=headers, rfile=io.BytesIO(body))


def test_complete_endpoint_conflicts_when_nothing_is_waiting():
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    with pytest.raises(ValueError, match="no wired measurement"):
        correction_setup._handle_crossover_v2_complete(_fake_handler())


def test_complete_endpoint_fires_the_wired_sessions_signal():
    from jasper.web import correction_setup

    fired = []
    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:session",
        request_complete=lambda: fired.append(True),
    )
    try:
        result = correction_setup._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_setup._set_capture_slot(None)
    assert result == {"ok": True}
    assert fired == [True]


def test_the_completion_signal_drops_with_the_slot():
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:session", request_complete=lambda: None,
    )
    correction_setup._set_capture_slot(
        {"status": "complete", "kind": "crossover_v2:session"}
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_setup._handle_crossover_v2_complete(_fake_handler())
    finally:
        correction_setup._set_capture_slot(None)


def test_retake_endpoint_conflicts_when_nothing_is_waiting():
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    with pytest.raises(ValueError, match="no wired measurement"):
        correction_setup._handle_crossover_v2_retake(_fake_handler())


def test_retake_endpoint_fires_the_wired_sessions_signal():
    from jasper.web import correction_setup

    fired = []
    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:session",
        request_retake=lambda: fired.append(True),
    )
    try:
        result = correction_setup._handle_crossover_v2_retake(_fake_handler())
    finally:
        correction_setup._set_capture_slot(None)
    assert result == {"ok": True}
    assert fired == [True]


def test_the_retake_signal_drops_with_the_slot():
    """A POST arriving after the walk must not re-open a slot nothing holds."""
    from jasper.web import correction_setup

    correction_setup._set_capture_slot(None)
    assert correction_setup._begin_capture_slot(
        "crossover_v2:session", request_retake=lambda: None,
    )
    correction_setup._set_capture_slot(
        {"status": "complete", "kind": "crossover_v2:session"}
    )
    try:
        with pytest.raises(ValueError, match="no wired measurement"):
            correction_setup._handle_crossover_v2_retake(_fake_handler())
    finally:
        correction_setup._set_capture_slot(None)


def test_the_wired_provider_reaches_the_host_only_at_call_time():
    """The capture provider's import-shape pin, mirrored for its sibling: the
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


def test_the_capture_half_records_into_this_sessions_bundle():
    store = SimpleNamespace(bundle_dir="/var/lib/jasper/bundle")

    half = v2host._wired_stimulus_capture(_device(), store)

    assert isinstance(half, v2wired.WiredStimulusCapture)
    assert half.device.model_key == "minidsp_umik2"
    # The bundle this session's evidence lands in, so the path the record
    # carries resolves against the same root `analyze` will be declared.
    assert half.bundle_dir == Path("/var/lib/jasper/bundle")


# --------------------------------------------------------------------------- #
# layer 6: the ENGINE MEASURE LEG
# --------------------------------------------------------------------------- #
#
# The wired walk's MEASURE capture runs through `TuningSession.measure()`: the
# engine plays the stimulus through the real play transaction, the shared
# capture half records across it and banks the path, and the walk's verdict
# grades the very take the engine banked. Every other index keeps the walk's
# own recorder + `on_armed` path, and every engine failure surfaces as the
# exception type the runner's arms already classify.


class _LegSession:
    """A `TuningSession` stand-in: scripted `measure`, recorded specs."""

    measurement_level_db = -22.0

    def __init__(self, outcome=None):
        self.specs: list = []
        self._outcome = outcome

    async def measure(self, spec):
        self.specs.append(spec)
        if self._outcome is not None:
            return self._outcome
        return SimpleNamespace(stimuli=(SimpleNamespace(
            record_id="rec-1", banked=True, incident="", level_db=-22.0,
        ),))


class _LegCaptureHalf:
    """The drained-answer side of `WiredStimulusCapture`, scripted."""

    def __init__(self, answer="the-engine-take"):
        self._answers = [answer] if answer is not None else []

    def take_answer(self):
        return self._answers.pop() if self._answers else None


@pytest.fixture()
def _held_window(monkeypatch):
    """The leg under the session-held window, nothing latched.

    The leg's measure runs under `_under_measurement_isolation`; taking the
    held-window arm keeps these pins off the real coordinator. Abort-latch
    pins override `_session_abort_target` themselves.
    """
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(v2host, "_session_abort_target", None)
    return monkeypatch


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


def _leg(tuning=None, half=None, phase_map=None):
    def _run_async(coro):
        return asyncio.run(coro)

    return v2host._bind_engine_measure_leg(
        tuning=tuning if tuning is not None else _LegSession(),
        stimulus_capture=half if half is not None else _LegCaptureHalf(),
        index_phase_map=phase_map if phase_map is not None else {
            1: "check", 2: "measure", 3: "cloud_measure",
        },
        run_async=_run_async,
    )


def test_the_engine_leg_claims_exactly_the_measure_indices(_held_window):
    """CHECK and the prompted walks stay on the flow callbacks; MEASURE is
    the one phase the engine can drive end-to-end today (kind exists, program
    routed and re-admittable, graph discipline matches install-at-open)."""
    tuning = _LegSession()
    leg = _leg(tuning=tuning)

    assert leg(1, 1, entry=None) is None
    assert leg(3, 1, entry=None) is None
    assert tuning.specs == [], "an unclaimed index must not reach the engine"

    answer = leg(2, 1, entry=None)

    assert answer == "the-engine-take"
    assert [spec.kind for spec in tuning.specs] == ["candidate"]


def test_a_stage_with_no_measure_index_binds_no_leg():
    """A verify-only stage's map has no MEASURE, so there is no closure at
    all — the walk's signature stays None and its behavior is untouched."""
    from jasper.active_speaker.crossover_v2.journey import PHASE_VERIFY

    assert _leg(phase_map={1: PHASE_VERIFY}) is None
    assert v2host._bind_engine_measure_leg(
        tuning=_LegSession(), stimulus_capture=None,
        index_phase_map={2: "measure"}, run_async=lambda c: c,
    ) is None


def test_the_walk_consumes_the_engine_take_for_the_claimed_index(monkeypatch):
    """The join pin: index 2's verdict grades the ENGINE's answer, index 1
    still runs the walk's own recorder, and the conversation order holds."""
    persists: list = []
    recorded: list = []
    conductor = FakeConductor()
    volume = VolumeRecorder()

    def _tracking_factory(max_capture_s):
        recorded.append(max_capture_s)
        return _recorder_factory()(max_capture_s)

    def _engine_leg(index, attempt, entry):
        return "the-engine-take" if index == 2 else None

    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, persists=persists,
        recorder_factory=_tracking_factory,
        capture_stimulus=_engine_leg,
    )
    _run(runner, plan=_plan(target=2, max_attempts=4))

    kinds = [event[0] for event in conductor.events]
    assert kinds == [
        "authorize", "on_armed", "consume", "authorize", "consume",
    ], "the engine leg replaces on_armed for its index and nothing else"
    assert conductor.answers[1] == "the-engine-take"
    assert isinstance(conductor.answers[0], v2wired.WiredCaptureAnswer)
    assert len(recorded) == 1, "the walk's recorder must not roll for the engine take"


def test_an_engine_leg_failure_lands_in_the_runners_existing_arms(monkeypatch):
    """The leg raises the same types the local leg raises, so the persisted
    terminal code comes out of the SAME classifier arm — internal_error for a
    capture-chain fault, exactly as a local recorder death lands today."""
    terminal: list = []
    conductor = FakeConductor()
    volume = VolumeRecorder()

    def _engine_leg(index, attempt, entry):
        raise WiredCaptureError("the capture half minted no evidence")

    runner = _build(
        conductor, volume, monkeypatch=monkeypatch, terminal=terminal,
        capture_stimulus=_engine_leg,
    )
    with pytest.raises(WiredCaptureError):
        _run(runner)

    assert terminal == [REASON_INTERNAL_ERROR]
    assert volume.events == ["open", "abandon"], "the walked-away drain held"


@pytest.mark.parametrize(
    ("incident", "expected_type"),
    [
        # program family -> the program-unplayable refusal, as today
        ("program_admission_refused", "ProgramAdmissionError"),
        ("program_play_failed", "ProgramPlaybackError"),
        # everything else -> internal_error (fix-and-retry), as today: a dead
        # aplay (`stimulus_emission_failed`) must never render safety copy,
        # and an incident this table does not know defaults there too.
        ("stimulus_emission_failed", "CrossoverV2LocalSeamError"),
        ("session_level_not_ready", "SessionVolumePlanError"),
        ("stimulus_not_captured", "WiredCaptureError"),
        ("program_not_composed", "CrossoverV2LocalSeamError"),
        ("some_future_incident", "CrossoverV2LocalSeamError"),
    ],
)
def test_each_engine_incident_reraises_todays_exception_type(
    _held_window, incident, expected_type,
):
    """Failure identity survives the leg, per FAMILY.

    The classifier and the frozen persisted codes must read the same
    whichever leg played the stimulus: only `play_program`'s own family may
    reach `program_unplayable`; the emission mechanics and every unknown
    incident keep today's `internal_error`. Mutation: collapse the mapping to
    one type and the rows of the other family red.
    """
    outcome = SimpleNamespace(stimuli=(SimpleNamespace(
        record_id="", banked=False, incident=incident, level_db=None,
    ),))
    leg = _leg(tuning=_LegSession(outcome=outcome))

    with pytest.raises(Exception) as caught:
        leg(2, 1, entry=None)

    assert type(caught.value).__name__ == expected_type


def test_an_unproven_level_is_not_a_walk_event(_held_window):
    """MS-14 is engine-internal honesty, never a new death trigger.

    The claim's single pre-play read is STRICTER than the #2925 hold (no
    independent re-read; None on preemption or one unreadable round-trip).
    The stimulus played, the hold held (a genuine drift raises out of the
    play itself, same terminal end as the flow leg), the answer was minted —
    so the walk grades it exactly as today. The engine's own refusal stands:
    nothing banked, disclosed in the outcome. Mutation: re-raising here reds
    this pin alone.
    """
    outcome = SimpleNamespace(stimuli=(SimpleNamespace(
        record_id="", banked=False, incident="unproven_level", level_db=None,
    ),))
    leg = _leg(tuning=_LegSession(outcome=outcome))

    assert leg(2, 1, entry=None) == "the-engine-take"


def test_a_multi_stimulus_outcome_is_refused_as_a_wiring_fault(_held_window):
    """The leg pairs THE stimulus with THE drained answer.

    `take_answer` drains the last mint, so the pairing is sound only while
    the leg's spec is single-stimulus — a checked assumption, not a comment.
    """
    outcome = SimpleNamespace(stimuli=(
        SimpleNamespace(record_id="a", banked=True, incident="", level_db=-22.0),
        SimpleNamespace(record_id="b", banked=True, incident="", level_db=-22.0),
    ))
    leg = _leg(tuning=_LegSession(outcome=outcome))

    with pytest.raises(v2host.CrossoverV2LocalSeamError):
        leg(2, 1, entry=None)


def test_a_latched_isolation_abort_refuses_the_engine_play(monkeypatch):
    """Parity with `_play_under_session_pause`: a failed gate lease refuses
    the NEXT play with the named error, before any audio — the engine leg
    included. Mutation: dropping the isolation wrapper reds this (the leg
    would play instead of refusing)."""
    from jasper.correction.coordinator import MeasurementWindowError

    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(
        v2host, "_session_abort_target", _FakeAbortTarget(failed=True)
    )
    tuning = _LegSession()
    leg = _leg(tuning=tuning)

    with pytest.raises(MeasurementWindowError):
        leg(2, 1, entry=None)

    assert tuning.specs == [], "a latched abort must refuse BEFORE any audio"


def test_the_engine_play_registers_as_the_windows_abort_target(monkeypatch):
    """The isolation-loss abort can stop an in-flight engine sweep only if
    the measure task is registered on the window's abort target, exactly as
    the flow leg registers its play task."""
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    target = _FakeAbortTarget()
    monkeypatch.setattr(v2host, "_session_abort_target", target)
    leg = _leg()

    assert leg(2, 1, entry=None) == "the-engine-take"
    assert len(target.registered) == 1, "the measure task must be registered"
    assert target.cleared == 1, "and cleared when the play ends"


def test_a_mid_measure_abort_surfaces_as_the_named_window_error(monkeypatch):
    """An isolation-loss cancel mid-sweep becomes MeasurementWindowError, so
    the runner's cleanup arm persists an honest failure — flow-leg parity."""
    from jasper.correction.coordinator import MeasurementWindowError

    target = _FakeAbortTarget()
    monkeypatch.setattr(v2host, "session_measurement_pause_held", lambda: True)
    monkeypatch.setattr(v2host, "_session_abort_target", target)

    class _AbortedSession(_LegSession):
        async def measure(self, spec):
            # The coordinator latches the failure and cancels the registered
            # task; the cancel lands inside the play body.
            target.failed = True
            raise asyncio.CancelledError()

    leg = _leg(tuning=_AbortedSession())

    with pytest.raises(MeasurementWindowError):
        leg(2, 1, entry=None)


def test_a_banked_take_whose_answer_was_not_minted_is_a_capture_fault(_held_window):
    """The leg's own honesty gate: a record with no drainable answer means
    the two halves disagree, and serving the previous take would grade the
    wrong audio."""
    leg = _leg(half=_LegCaptureHalf(answer=None))

    with pytest.raises(WiredCaptureError):
        leg(2, 1, entry=None)


def test_take_answer_is_take_and_clear(tmp_path):
    """An answer serves exactly one consume; a stale one is never re-served."""
    half = _capture_half(tmp_path)

    async def _play() -> None:
        return None

    asyncio.run(half.around(_play, program=_StimulusProgram()))

    first = half.take_answer()
    assert isinstance(first, v2wired.WiredCaptureAnswer)
    assert half.take_answer() is None, "the second ask must not re-serve the take"


def test_the_engine_leg_banks_real_evidence_end_to_end(_held_window, tmp_path):
    """The lane's landing pin, at test altitude: a REAL `TuningSession` over a
    REAL `ProgramPlaybackTransaction` with the REAL wired capture half (fake
    recorder, fake play seams) — driven exactly as the walk drives the leg.

    Three facts prove the take is real evidence and one answer:
    the banked record's `wav_path` names bytes that exist in the bundle; the
    record carries the PROVEN level (MS-14 live on the wired path); and the
    answer the leg hands `consume_capture` decodes to the same audio the
    banked file holds.
    """
    from jasper.active_speaker.crossover_v2.program_transaction import (
        ProgramForStimulus,
        ProgramPlaybackTransaction,
    )
    from tests.engine_twin import FakeSeams, tuning_session
    import dataclasses as _dc

    class _PassingPlan:
        measurement_volume_db = -22.0

        def assert_ready(self, now=None):
            return None

    class _PlaySeams:
        async def _readmit(self):
            return SimpleNamespace(allowed=True, refusals=())

        async def _play_wav(self):
            return object()

        def _writer_lock(self):
            class _Lock:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return None

            return _Lock()

        def as_kwargs(self):
            return {
                "readmit": self._readmit,
                "play_wav": self._play_wav,
                "writer_lock": self._writer_lock,
            }

    half = _capture_half(tmp_path)
    program = _StimulusProgram()

    def _compose(**_kw):
        return ProgramForStimulus(program=program, seams=_PlaySeams().as_kwargs())

    transaction = ProgramPlaybackTransaction(
        compose=_compose, session_volume_plan=_PassingPlan(), capture=half,
    )
    fakes = FakeSeams()
    fakes = _dc.replace(fakes, play=transaction)
    session, fakes = tuning_session(fakes)
    asyncio.run(session.open())

    leg = v2host._bind_engine_measure_leg(
        tuning=session,
        stimulus_capture=half,
        index_phase_map={1: "check", 2: "measure"},
        run_async=asyncio.run,
    )
    answer = leg(2, 1, entry=None)

    assert isinstance(answer, v2wired.WiredCaptureAnswer)
    [record] = fakes.records.banked
    assert record["kind"] == "candidate"
    assert record["level_db"] == session.measurement_level_db, (
        "the record carries the PROVEN level, not a declared one"
    )
    banked_wav = tmp_path / record["wav_path"]
    assert banked_wav.exists(), "the record points at bytes that exist"
    assert banked_wav.read_bytes() == answer.wav, (
        "the verdict grades the very take the engine banked"
    )
    asyncio.run(session.close())
