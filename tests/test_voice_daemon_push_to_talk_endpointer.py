# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""On a push-to-talk turn the button owns end-of-input — not local Silero.

Press calls ``manual_session_start``; release calls ``manual_session_end``,
which sets ``_input_ended`` and calls ``end_input()``. That is the exact
operation the Silero end-of-utterance detector performs, so running Silero
on the same turn makes it a *second writer of the same fact* — and the
faster of the two wins. Before this bypass, a user holding the button
through a 0.8 s thinking pause had their input closed underneath them, and
a user who held the button for 5 s before speaking had the turn ended
outright.

Each guard below is written as a mutation: the same frames are fed with
``_manual_endpoint_this_turn`` flipped, and the pre-fix behaviour is
asserted to come back. If the branch is ever deleted, these fail.
"""
from __future__ import annotations

import asyncio
import logging

import numpy as np
import pytest


class _SpyTurn:
    """LiveTurn stand-in exposing the forward + end-of-input surface."""

    def __init__(self) -> None:
        self.send_audio_calls = 0
        self.end_input_calls = 0

    async def send_audio(self, _data) -> None:
        self.send_audio_calls += 1

    async def end_input(self) -> None:
        self.end_input_calls += 1


class _SilentVad:
    """Silero stand-in that reports 'no speech' and counts every ask."""

    def __init__(self, score: float = 0.0) -> None:
        self.score = score
        self.predict_calls = 0

    def predict(self, _frame) -> float:
        self.predict_calls += 1
        return self.score

    def reset(self) -> None:
        return None


def _frame() -> np.ndarray:
    return np.zeros(1280, dtype=np.int16)


def _session_loop(*, manual: bool, vad=None, elapsed: float = 1.0):
    """A WakeLoop parked mid-turn with input still open.

    ``elapsed`` is how long the turn has been open, expressed by
    back-dating ``_turn_started_at_loop`` on the running loop's clock —
    the same clock ``_handle_session_frame`` reads.
    """
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.SESSION
    wl._turn = _SpyTurn()
    wl._vad = _SilentVad() if vad is None else vad
    wl._bg_tasks = set()
    wl._input_ended = False
    wl._barge_in_active = False
    wl._server_vad_this_turn = False
    wl._manual_endpoint_this_turn = manual
    wl._active_manual_source = "wiim_remote_2" if manual else None
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - elapsed
    return wl


# ---------------------------------------------------------------------------
# The defect: mid-hold silence closed the user's input
# ---------------------------------------------------------------------------


async def test_mid_hold_silence_does_not_end_input_on_a_button_turn():
    """User spoke, then paused mid-hold for longer than
    END_OF_UTTERANCE_SILENCE_SEC. The button is still down, so nothing
    may call end_input()."""
    from jasper.voice_daemon import END_OF_UTTERANCE_SILENCE_SEC

    wl = _session_loop(manual=True)
    now = asyncio.get_event_loop().time()
    wl._user_speech_seen = True
    wl._silence_started_at = now - (END_OF_UTTERANCE_SILENCE_SEC + 0.5)

    await wl._handle_session_frame(_frame())

    assert wl._turn.end_input_calls == 0
    assert wl._input_ended is False
    # The frame still reaches the provider — bypassing the endpointer is
    # not the same as dropping audio.
    assert wl._turn.send_audio_calls == 1
    # And Silero was never asked, so a Zero-class box pays nothing.
    assert wl._vad.predict_calls == 0


async def test_mid_hold_silence_DOES_end_input_without_the_bypass():
    """Mutation of the guard above: the identical frame with
    `_manual_endpoint_this_turn` cleared reproduces the reported defect,
    which is what makes the assertion above load-bearing."""
    from jasper.voice_daemon import END_OF_UTTERANCE_SILENCE_SEC

    wl = _session_loop(manual=False)
    now = asyncio.get_event_loop().time()
    wl._user_speech_seen = True
    wl._silence_started_at = now - (END_OF_UTTERANCE_SILENCE_SEC + 0.5)

    await wl._handle_session_frame(_frame())

    assert wl._turn.end_input_calls == 1
    assert wl._input_ended is True


# ---------------------------------------------------------------------------
# The defect: a held button with no speech yet ended the turn outright
# ---------------------------------------------------------------------------


async def test_no_speech_abort_does_not_fire_on_a_button_turn():
    """Held button, nothing said yet, past NO_SPEECH_ABORT_SEC. There was
    no wake to be false, so there is nothing to abort."""
    from jasper.voice_daemon import NO_SPEECH_ABORT_SEC

    wl = _session_loop(manual=True, elapsed=NO_SPEECH_ABORT_SEC + 1.0)
    ended: list[bool] = []

    async def _spy_end_turn(*_a, **_k) -> None:
        ended.append(True)

    wl._end_turn = _spy_end_turn

    await wl._handle_session_frame(_frame())

    assert ended == []
    assert wl._turn.send_audio_calls == 1


async def test_no_speech_abort_DOES_fire_without_the_bypass():
    """Mutation of the guard above."""
    from jasper.voice_daemon import NO_SPEECH_ABORT_SEC

    wl = _session_loop(manual=False, elapsed=NO_SPEECH_ABORT_SEC + 1.0)
    ended: list[bool] = []

    async def _spy_end_turn(*_a, **_k) -> None:
        ended.append(True)

    wl._end_turn = _spy_end_turn

    await wl._handle_session_frame(_frame())

    assert ended == [True]


# ---------------------------------------------------------------------------
# What must survive: the stuck-button defence
# ---------------------------------------------------------------------------


async def test_hard_cap_still_closes_a_button_turn(caplog):
    """A release that never arrives (BLE drop, remote under a cushion)
    would otherwise hold the duck, the mic, and a paid LLM session open
    forever. HARD_RECORDING_CAP_SEC is the only thing left that can end
    it, so it must survive the bypass."""
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    wl = _session_loop(manual=True, elapsed=HARD_RECORDING_CAP_SEC + 0.5)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await wl._handle_session_frame(_frame())

    assert wl._input_ended is True
    assert wl._turn.end_input_calls == 1
    # Audio for this frame is NOT forwarded — same shape as the Silero
    # path's cap, which returns after end_input.
    assert wl._turn.send_audio_calls == 0
    assert "event=manual_mic.hard_cap" in caplog.text
    assert "source=wiim_remote_2" in caplog.text


async def test_hard_cap_does_not_fire_early_on_a_button_turn():
    """Just under the cap the turn is still the user's."""
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    wl = _session_loop(manual=True, elapsed=HARD_RECORDING_CAP_SEC - 1.0)

    await wl._handle_session_frame(_frame())

    assert wl._input_ended is False
    assert wl._turn.end_input_calls == 0
    assert wl._turn.send_audio_calls == 1


async def test_hard_cap_fires_once_then_frames_are_dropped():
    """`_input_ended` gates re-entry, so a still-held button after the cap
    does not re-send end_input on every frame."""
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    wl = _session_loop(manual=True, elapsed=HARD_RECORDING_CAP_SEC + 0.5)

    for _ in range(5):
        await wl._handle_session_frame(_frame())

    assert wl._turn.end_input_calls == 1
    assert wl._turn.send_audio_calls == 0


# ---------------------------------------------------------------------------
# A push-to-talk-only speaker has no local VAD at all
# ---------------------------------------------------------------------------


def _remote_source():
    from jasper.voice_daemon import _ManualMicRuntime

    return [_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")]


def _ptt_only_loop():
    """The runtime shape a push-to-talk-only speaker resolves to: zero
    wake legs, one manual mic source, and the real VAD selection rather
    than an injected stub that would bypass it."""
    from jasper.voice_daemon import WakeLoop

    return WakeLoop.for_tests(
        legs=[], manual_mics=_remote_source(), vad=None,
    )


def test_push_to_talk_only_speaker_constructs_no_speech_vad(monkeypatch):
    """No turn on this speaker asks Silero a question, so SpeechVAD is
    never constructed — which is also why openwakeword/onnxruntime never
    load into the daemon on a 512 MB box."""
    import jasper.voice_daemon as vd

    def _boom():
        raise AssertionError("SpeechVAD must not be constructed on a PTT box")

    monkeypatch.setattr(vd, "SpeechVAD", _boom)

    wl = _ptt_only_loop()

    assert wl._push_to_talk_only is True
    assert wl._vad is None


def test_a_speaker_with_a_wake_leg_still_constructs_speech_vad(monkeypatch):
    """Mutation of the guard above: the same `vad=None` construction on a
    speaker that DID resolve a wake leg builds the VAD, so the skip is
    keyed on resolved runtime and not on the injection."""
    import jasper.voice_daemon as vd
    from jasper.voice_daemon import WakeLoop

    built = []
    monkeypatch.setattr(vd, "SpeechVAD", lambda: built.append(True) or "vad")

    wl = WakeLoop.for_tests(manual_mics=_remote_source(), vad=None)

    assert wl._push_to_talk_only is False
    assert built == [True]
    assert wl._vad == "vad"


def test_push_to_talk_only_speaker_has_no_primary_leg():
    """Pins the inherited claim that WakeLoop tolerates a missing "on"
    leg: the flat aliases every read site uses degrade to None / an empty
    ring rather than raising KeyError at construction."""
    wl = _ptt_only_loop()

    assert wl._legs == {}
    assert wl._mic is None
    assert wl._detector is None
    assert list(wl._capture_ring_on) == []


async def test_button_turn_survives_a_missing_vad():
    """The end-to-end invariant: on a PTT-only speaker `self._vad` is
    None, and the push-to-talk branch is what keeps any code path from
    dereferencing it."""
    from jasper.voice_daemon import State

    wl = _ptt_only_loop()
    wl._state = State.SESSION
    wl._turn = _SpyTurn()
    wl._bg_tasks = set()
    wl._input_ended = False
    wl._barge_in_active = False
    wl._server_vad_this_turn = False
    wl._manual_endpoint_this_turn = True
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - 1.0

    await wl._handle_session_frame(_frame())

    assert wl._turn.send_audio_calls == 1


async def test_missing_vad_would_crash_without_the_bypass():
    """Mutation of the guard above — proof that the branch, not luck, is
    what protects the None."""
    from jasper.voice_daemon import State

    wl = _ptt_only_loop()
    wl._state = State.SESSION
    wl._turn = _SpyTurn()
    wl._bg_tasks = set()
    wl._input_ended = False
    wl._barge_in_active = False
    wl._server_vad_this_turn = False
    wl._manual_endpoint_this_turn = False
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - 1.0

    with pytest.raises(AttributeError):
        await wl._handle_session_frame(_frame())


# ---------------------------------------------------------------------------
# The endpointer is decided once, at the top of _begin_turn
# ---------------------------------------------------------------------------


def test_endpointer_label_prefers_push_to_talk():
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()

    wl._manual_endpoint_this_turn = False
    wl._server_vad_this_turn = False
    assert wl._endpointer_label() == "silero_aec"

    wl._server_vad_this_turn = True
    assert wl._endpointer_label() == "server_vad"

    # A button turn is a button turn even if server VAD was negotiated:
    # the release is what actually closes input.
    wl._manual_endpoint_this_turn = True
    assert wl._endpointer_label() == "push_to_talk"


def test_session_status_reports_the_endpointer():
    """The daemon's own decision, on its own STATUS surface."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._manual_endpoint_this_turn = True

    assert wl.session_status()["endpointer"] == "push_to_talk"


def test_state_voice_section_pulls_the_endpointer_through():
    """jasper-control curates `/state.voice` field by field, so a new
    session_status key is invisible to every client until it is pulled
    through — the convention state_aggregate.py states three times in
    comments and nothing enforced. Pin it for this field: without the
    pull-through, "the remote cut me off" stays undiagnosable from
    outside the voice daemon.
    """
    import ast
    import inspect

    from jasper.control import state_aggregate

    tree = ast.parse(inspect.getsource(state_aggregate))
    voice_dicts = [
        value
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == "voice"
        and isinstance(value, ast.Dict)
    ]
    assert voice_dicts, "no `\"voice\": {...}` literal in state_aggregate"

    pulled = {
        call.args[0].value
        for voice in voice_dicts
        for call in ast.walk(voice)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    assert "endpointer" in pulled, (
        "/state.voice does not pull `endpointer` through from "
        f"session_status; it pulls {sorted(pulled)}"
    )


async def test_acquire_drain_skips_the_vad_pass_on_a_button_turn():
    """The acquire-buffer VAD pass exists only to pre-arm
    `_user_speech_seen` for the live silence detector. A button turn runs
    neither, so scoring those frames would cost a Silero pass each and
    change nothing."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._vad = _SilentVad(score=1.0)
    wl._acquire_buffer.extend(_frame() for _ in range(4))
    wl._manual_endpoint_this_turn = True

    drained, speech = await wl._drain_acquire_audio()

    assert drained == 4
    assert speech is False
    assert wl._vad.predict_calls == 0


async def test_acquire_drain_still_scores_on_a_wake_turn():
    """Mutation of the guard above."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._vad = _SilentVad(score=1.0)
    wl._acquire_buffer.extend(_frame() for _ in range(4))
    wl._manual_endpoint_this_turn = False

    drained, speech = await wl._drain_acquire_audio()

    assert drained == 4
    assert speech is True
    assert wl._vad.predict_calls == 4


# ---------------------------------------------------------------------------
# Barge-in on a button turn refuses loudly rather than going inert
# ---------------------------------------------------------------------------


def test_barge_in_refused_on_a_button_turn_and_says_why(
    monkeypatch, tmp_path, caplog,
):
    """Enabling barge-in on a push-to-talk speaker must not silently do
    nothing. `_barge_in_reference_available` was computed from
    cfg.mic_device, which is not the stream a button turn scores — so the
    self-interrupt guard has not cleared this audio."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True  # would otherwise enable
    wl._manual_endpoint_this_turn = True
    wl._active_manual_source = "wiim_remote_2"

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._resolve_barge_in_for_turn()
        first = [
            r for r in caplog.records
            if "barge.disabled_push_to_talk" in r.getMessage()
        ]
        wl._resolve_barge_in_for_turn()
        second = [
            r for r in caplog.records
            if "barge.disabled_push_to_talk" in r.getMessage()
        ]

    assert wl._barge_in_active is False
    # One-shot per daemon, like the no-reference WARN it sits beside.
    assert len(first) == 1
    assert len(second) == 1
    assert "source=wiim_remote_2" in caplog.text


def test_barge_in_refused_when_there_is_no_local_vad(monkeypatch, tmp_path):
    """Belt and braces for the None: even on a wake-shaped turn, a
    speaker with no local VAD can never arm barge-in — which is what
    keeps `_handle_playback_frame` from dereferencing it."""
    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = _ptt_only_loop()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True
    wl._manual_endpoint_this_turn = False

    wl._resolve_barge_in_for_turn()

    assert wl._barge_in_active is False


def test_push_to_talk_refusal_does_not_consume_the_no_reference_warning(
    monkeypatch, tmp_path, caplog,
):
    """Two distinct facts, two latches. On a speaker with both a room mic
    and a remote, a button turn's refusal must not swallow the
    no-reference WARN a later wake turn owes the operator."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._cfg.mic_device = "Array"
    wl._barge_in_reference_available = False

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._manual_endpoint_this_turn = True
        wl._resolve_barge_in_for_turn()
        # Now a wake turn on the same daemon.
        wl._manual_endpoint_this_turn = False
        wl._resolve_barge_in_for_turn()

    assert "event=barge.disabled_push_to_talk" in caplog.text
    assert "event=barge.disabled_no_reference" in caplog.text


# ---------------------------------------------------------------------------
# Corpus honesty
# ---------------------------------------------------------------------------


def test_button_turn_is_never_recorded_as_a_no_speech_abort():
    """`no_speech` is a verdict about listening. A button turn scores
    nothing, so `_user_speech_seen` stays False — and the corpus label
    must not read that as an abort the daemon never performed."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._user_speech_seen = False
    wl._server_vad_this_turn = False

    wl._manual_endpoint_this_turn = True
    assert wl._endpointer_label() == "push_to_talk"

    wl._manual_endpoint_this_turn = False
    assert wl._endpointer_label() == "silero_aec"


@pytest.mark.parametrize("manual", [True, False])
async def test_begin_turn_decides_the_endpointer_from_the_active_source(
    manual,
):
    """One decision, made once, from the same flag `_manual_mic_loop`
    gates its frames on — so "the button owns this turn" and
    "manual-source frames are this turn's audio" cannot disagree.

    Drives the real `_begin_turn` and lets it run into `for_tests`'
    deliberately-exploding `acquire_turn` stub. The decision is made
    before that point, so reaching the explosion with the flag already
    set is the evidence; a stale opposite value is seeded first so a
    no-op would fail.
    """
    from jasper.voice_daemon import WakeLoop

    async def _noop(*_a, **_k) -> None:
        return None

    wl = WakeLoop.for_tests()
    # Collaborators `for_tests` does not stub, filled in only so the
    # coroutine reaches its documented explosion point rather than an
    # incidental AttributeError on the way there.
    wl._content_activity.refresh_now = _noop
    wl._tts.pause_content_meter = _noop
    wl._active_manual_source = "wiim_remote_2" if manual else None
    wl._manual_endpoint_this_turn = not manual  # stale value from before

    with pytest.raises(AssertionError, match="acquire_turn stub"):
        await wl._begin_turn()

    assert wl._manual_endpoint_this_turn is manual
