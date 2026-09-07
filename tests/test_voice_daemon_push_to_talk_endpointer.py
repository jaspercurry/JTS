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

Each guard below is written as a mutation: the same inputs are replayed
with the guard's condition flipped, and the pre-fix behaviour is asserted
to come back. If a branch is ever deleted, these fail.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.voice.session import TurnUsage
from tests._live_turn_fake import silent_frame
from tests._log_events import event_field_maps, event_fields, event_records


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


def _shipped_idle_timeout_default() -> int:
    """The `JASPER_IDLE_TIMEOUT_SEC` default, taken from a real
    `Config.from_env` with the knob unset rather than restated here — a
    duplicated literal is exactly how the ordering these tests pin would
    silently stop holding.
    """
    import os
    from unittest import mock

    from jasper.config import Config

    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"JASPER_IDLE_TIMEOUT_SEC", "JASPER_VOICE_PROVIDER"}
    }
    env["JASPER_VOICE_PROVIDER"] = "gemini"
    env.setdefault("GEMINI_API_KEY", "test-key")
    with mock.patch.dict(os.environ, env, clear=True):
        return Config.from_env().idle_timeout_sec


def _session_loop(*, manual: bool, elapsed: float = 1.0, idle_timeout: int = 20):
    """A WakeLoop parked mid-turn with input still open.

    ``elapsed`` is how long the turn has been open, expressed by
    back-dating ``_turn_started_at_loop`` on the running loop's clock —
    the same clock ``_handle_session_frame`` reads.
    """
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = idle_timeout
    wl._state = State.SESSION
    wl._turn = _SpyTurn()
    wl._vad = _SilentVad()
    wl._bg_tasks = set()
    wl._input_ended = False
    wl._barge_in_active = False
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

    await wl._handle_session_frame(silent_frame())

    assert wl._turn.end_input_calls == 0
    assert wl._input_ended is False
    # The frame still reaches the provider — bypassing the endpointer is
    # not the same as dropping audio.
    assert wl._turn.send_audio_calls == 1
    # And Silero was never asked. This loop has one injected (it also has a
    # wake leg); the memory half is pinned separately, below.
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

    await wl._handle_session_frame(silent_frame())

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

    await wl._handle_session_frame(silent_frame())

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

    await wl._handle_session_frame(silent_frame())

    assert ended == [True]


# ---------------------------------------------------------------------------
# The bound that replaces them: it must fire BELOW the idle watchdog
# ---------------------------------------------------------------------------


def test_at_the_shipped_default_the_hold_cap_beats_the_idle_watchdog():
    """The load-bearing ordering *at the shipped default*, read from the
    config rather than hardcoded. Deliberately not a general property —
    it does not hold at every `idle_timeout_sec`, and the two degraded
    bands below are where it stops holding.

    `_idle_watchdog`'s pre-response timer is anchored at turn OPEN and
    fires at `JASPER_IDLE_TIMEOUT_SEC` when no model chunk has arrived —
    and none can while input is open, because `last_activity_at()` tracks
    *model* activity. `_end_turn` then cancels `_play_responses` BEFORE
    calling `end_input`, so losing this race means the user gets no answer
    at all. The hold cap must close input early enough that the model can
    still start speaking inside the same window.
    """
    from jasper.voice_daemon import (
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        WakeLoop,
    )

    shipped_idle_timeout = _shipped_idle_timeout_default()

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = shipped_idle_timeout
    cap = wl._ptt_input_cap_sec()

    assert cap < shipped_idle_timeout, (
        f"push-to-talk hold cap {cap}s does not fire before the idle "
        f"watchdog at {shipped_idle_timeout}s; a long hold loses its answer"
    )
    assert shipped_idle_timeout - cap >= PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC


def test_hard_recording_cap_alone_would_lose_the_race():
    """Why the cap is derived rather than just HARD_RECORDING_CAP_SEC.

    Pins the arithmetic that made the first version of this change wrong:
    at the shipped defaults the 30 s constant sits ABOVE the 20 s idle
    timeout, so on its own it can never fire.
    """
    from jasper.voice_daemon import HARD_RECORDING_CAP_SEC

    assert HARD_RECORDING_CAP_SEC > _shipped_idle_timeout_default()


def test_hold_cap_is_derived_from_the_operators_idle_timeout():
    """Retuning JASPER_IDLE_TIMEOUT_SEC moves the cap with it, so the two
    cannot drift apart."""
    from jasper.voice_daemon import (
        HARD_RECORDING_CAP_SEC,
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        WakeLoop,
    )

    wl = WakeLoop.for_tests()

    wl._cfg.idle_timeout_sec = 20
    assert wl._ptt_input_cap_sec() == 20 - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC

    wl._cfg.idle_timeout_sec = 30
    assert wl._ptt_input_cap_sec() == 30 - PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC

    # ...but never past the absolute stuck-button ceiling.
    wl._cfg.idle_timeout_sec = 600
    assert wl._ptt_input_cap_sec() == HARD_RECORDING_CAP_SEC


def test_hold_cap_warns_when_a_low_idle_timeout_squeezes_the_model(caplog):
    """`PTT_MIN_INPUT_CAP_SEC` keeps the button usable under a very low
    idle timeout, at the cost of the model's response allowance. That is a
    degraded configuration and must say so.

    The interesting case is NOT only "the cap can no longer win the race".
    At `idle_timeout_sec = 10` the cap still fires first (5 s < 10 s) but
    leaves the model 5 s where the allowance asks for 6 — a slow first
    chunk still loses the answer, silently, unless this warns.
    """
    from jasper.voice_daemon import (
        PTT_MIN_INPUT_CAP_SEC,
        PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC,
        WakeLoop,
    )

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = 10

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        cap = wl._ptt_input_cap_sec()
        wl._ptt_input_cap_sec()  # one-shot latch: no second WARN

    # The floor won, and it still beats the watchdog...
    assert cap == PTT_MIN_INPUT_CAP_SEC
    assert cap < wl._cfg.idle_timeout_sec
    # ...but the model is left less than its allowance, which is the point.
    assert (
        wl._cfg.idle_timeout_sec - cap < PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC
    )
    # Exactly one record is the one-shot latch; the fields are the verdict.
    fields = event_fields(caplog, "manual_mic.idle_timeout_too_low")
    assert float(fields["needs_sec"]) == 11.0
    assert float(fields["cap_sec"]) == float(PTT_MIN_INPUT_CAP_SEC)
    assert float(fields["idle_timeout_sec"]) == float(wl._cfg.idle_timeout_sec)
    # ...and NOT the louder band's event: here the cap does still fire.
    assert event_records(caplog, "manual_mic.hold_cap_unreachable") == []


@pytest.mark.parametrize(
    "idle_timeout, expected_event",
    [
        (3, "manual_mic.hold_cap_unreachable"),
        # 5 is the crossing: the watchdog has walked down TO the floor, so
        # this is the last timeout at which the cap cannot fire.
        (5, "manual_mic.hold_cap_unreachable"),
        # 6 is the first at which it can — one second either side of the
        # boundary must not be reported as the same verdict.
        (6, "manual_mic.idle_timeout_too_low"),
        (10, "manual_mic.idle_timeout_too_low"),
        # 11 = floor + allowance: the full allowance is restored, silence.
        (11, None),
        (20, None),  # the shipped default
    ],
)
def test_hold_cap_degraded_bands_are_reported_distinctly(
    caplog, idle_timeout, expected_event,
):
    """The band boundaries themselves, walked one second at a time.

    "The cap fires but the model is squeezed" and "the cap can never fire"
    are different verdicts with different remedies, and an off-by-one in
    the comparison that separates them silently reports one as the other.
    The spot-check tests below cover the middle of each band; this covers
    the edges, which is where a boundary bug actually lives.
    """
    from jasper.voice_daemon import WakeLoop

    both = {"manual_mic.hold_cap_unreachable", "manual_mic.idle_timeout_too_low"}

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = idle_timeout

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._ptt_input_cap_sec()

    fired = {name for name in both if event_records(caplog, name)}
    assert fired == ({expected_event} if expected_event else set()), (
        f"idle_timeout_sec={idle_timeout} should report "
        f"{expected_event or 'nothing'}, got {fired or 'nothing'}"
    )


def test_a_very_low_idle_timeout_makes_the_cap_unreachable_and_says_so(caplog):
    """The band the first version of this docstring got wrong.

    `PTT_MIN_INPUT_CAP_SEC` is a constant floor; the watchdog is not. So a
    low enough `idle_timeout_sec` walks the watchdog down *through* the
    floor, and below the crossing the cap can never fire at all — the
    original blocker's exact failure mode, surviving in a narrow band.
    That is worse than a squeezed allowance (there, only a slow first
    chunk loses the answer; here every hold does) and gets its own,
    louder event.
    """
    from jasper.voice_daemon import PTT_MIN_INPUT_CAP_SEC, WakeLoop

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = 3

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        cap = wl._ptt_input_cap_sec()
        wl._ptt_input_cap_sec()  # one shared latch: no second WARN

    assert cap == PTT_MIN_INPUT_CAP_SEC
    assert cap >= wl._cfg.idle_timeout_sec  # the watchdog gets there first
    # Exactly one record is the shared one-shot latch holding.
    fields = event_fields(caplog, "manual_mic.hold_cap_unreachable")
    assert float(fields["cap_sec"]) == float(PTT_MIN_INPUT_CAP_SEC)
    assert float(fields["idle_timeout_sec"]) == float(wl._cfg.idle_timeout_sec)
    # The two bands are distinct verdicts and must not be conflated: the
    # softer one would understate a cap that cannot fire at all.
    assert event_records(caplog, "manual_mic.idle_timeout_too_low") == []


def test_hold_cap_is_silent_when_the_allowance_is_actually_preserved(caplog):
    """Mutation of the warning above: at the shipped default the
    derivation does leave the full allowance, so the WARN must not fire —
    otherwise every household journal carries a permanent false alarm."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._cfg.idle_timeout_sec = _shipped_idle_timeout_default()

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._ptt_input_cap_sec()

    assert event_records(caplog, "manual_mic.idle_timeout_too_low") == []
    assert event_records(caplog, "manual_mic.hold_cap_unreachable") == []


async def test_hold_cap_closes_input_on_a_button_turn(caplog):
    """A release that never arrives (BLE drop, remote under a cushion)
    must still get the user an answer to what was said so far."""
    wl = _session_loop(manual=True)
    cap = wl._ptt_input_cap_sec()
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - (cap + 0.5)

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await wl._handle_session_frame(silent_frame())

    assert wl._input_ended is True
    assert wl._turn.end_input_calls == 1
    # Audio for this frame is NOT forwarded — same shape as the Silero
    # path's cap, which returns after end_input.
    assert wl._turn.send_audio_calls == 0
    fields = event_fields(caplog, "manual_mic.hold_cap")
    assert fields["source"] == "wiim_remote_2"
    assert float(fields["cap_sec"]) == float(cap)


async def test_hold_cap_does_not_fire_early_on_a_button_turn():
    """Just under the cap the turn is still the user's."""
    wl = _session_loop(manual=True)
    cap = wl._ptt_input_cap_sec()
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - (cap - 1.0)

    await wl._handle_session_frame(silent_frame())

    assert wl._input_ended is False
    assert wl._turn.end_input_calls == 0
    assert wl._turn.send_audio_calls == 1


async def test_hold_cap_fires_once_then_frames_are_dropped():
    """`_input_ended` gates re-entry, so a still-held button after the cap
    does not re-send end_input on every frame."""
    wl = _session_loop(manual=True)
    cap = wl._ptt_input_cap_sec()
    wl._turn_started_at_loop = asyncio.get_event_loop().time() - (cap + 0.5)

    for _ in range(5):
        await wl._handle_session_frame(silent_frame())

    assert wl._turn.end_input_calls == 1
    assert wl._turn.send_audio_calls == 0


# ---------------------------------------------------------------------------
# The endpointer is decided once, at the top of _begin_turn
# ---------------------------------------------------------------------------


def test_endpointer_label_prefers_push_to_talk():
    """The button owns both turn boundaries, so it names the endpointer
    whenever it is the source of the turn's audio."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()

    wl._manual_endpoint_this_turn = False
    assert wl._endpointer_label() == "silero_aec"

    wl._manual_endpoint_this_turn = True
    assert wl._endpointer_label() == "push_to_talk"


def test_session_status_reports_the_endpointer():
    """The daemon's own decision, on its own STATUS surface."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._manual_endpoint_this_turn = True

    assert wl.session_status()["endpointer"] == "push_to_talk"


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

    # `match=` stands: the stub raises a bare AssertionError, which carries
    # no code or structured attribute naming which stub it came from.
    with pytest.raises(AssertionError, match="acquire_turn stub"):
        await wl._begin_turn()

    assert wl._manual_endpoint_this_turn is manual


# ---------------------------------------------------------------------------
# Consequences of taking Silero off the button path
# ---------------------------------------------------------------------------


async def test_acquire_drain_skips_the_vad_pass_on_a_button_turn():
    """The acquire-buffer VAD pass exists only to pre-arm
    `_user_speech_seen` for the live silence detector. A button turn runs
    neither, so scoring those frames would cost a Silero pass each and
    change nothing."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    wl._turn = _SpyTurn()
    wl._vad = _SilentVad(score=1.0)
    wl._acquire_buffer.extend(silent_frame() for _ in range(4))
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
    wl._acquire_buffer.extend(silent_frame() for _ in range(4))
    wl._manual_endpoint_this_turn = False

    drained, speech = await wl._drain_acquire_audio()

    assert drained == 4
    assert speech is True
    assert wl._vad.predict_calls == 4


def test_corpus_label_never_records_a_button_turn_as_a_no_speech_abort():
    """`no_speech` is a verdict about listening. A button turn scores
    nothing, so `_user_speech_seen` stays False — and the label must not
    read that as an abort the daemon never performed.

    Exercises the real relabel expression (extracted from `_end_turn` so
    it is reachable at all: inline, its push-to-talk arm sat behind a
    wake-event id that a button turn never has).
    """
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()

    wl._manual_endpoint_this_turn = True
    assert wl._corpus_endpointer_label(user_speech_seen=False) == "push_to_talk"

    # Mutation: the same "no speech seen" on a Silero turn IS an abort.
    wl._manual_endpoint_this_turn = False
    assert wl._corpus_endpointer_label(user_speech_seen=False) == "no_speech_abort"
    assert wl._corpus_endpointer_label(user_speech_seen=True) == "silero_aec"


class _TeardownTurn:
    """The LiveTurn surface `_end_turn_inner` actually touches, so the
    real teardown can run start to finish without a provider."""

    def __init__(
        self,
        *,
        chunks: int = 3,
        turn_lost: bool = False,
        server_turn_complete: bool = False,
    ) -> None:
        self.end_input_calls = 0
        self.release_calls = 0
        self._chunks = chunks
        self._turn_lost = turn_lost
        self._server_turn_complete = server_turn_complete

    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    def turn_lost(self) -> bool:
        return self._turn_lost

    def server_turn_complete(self) -> bool:
        return self._server_turn_complete

    def bytes_sent(self) -> int:
        return 4096

    def chunks_received(self) -> int:
        return self._chunks

    def usage(self) -> TurnUsage:
        return TurnUsage()

    def capture(self) -> None:
        return None

    async def end_input(self) -> None:
        self.end_input_calls += 1

    async def release(self) -> None:
        self.release_calls += 1


class _SpyCues:
    """Recording cue manager, so the REAL `_play_cue` path runs end to end."""

    def __init__(self) -> None:
        self.played: list[str] = []

    async def play(self, slug: str) -> bool:
        self.played.append(slug)
        return True


def _teardown_loop():
    """A WakeLoop a caller can tear turns down on, with a cue manager so
    every failure cue the teardown plays is observable."""
    from jasper.voice_daemon import WakeLoop

    wl = WakeLoop.for_tests()
    # Only read by the no-audio diagnostics below; `for_tests`' cfg stub
    # does not carry it because nothing else in that seam reaches them.
    wl._cfg.active_voice_model = "test-model"
    wl._cues = _SpyCues()
    return wl


async def _torn_down_mid_hold(
    *,
    manual: bool,
    chunks: int = 3,
    input_ended: bool = False,
    user_speech: bool = False,
    turn_lost: bool = False,
    server_turn_complete: bool = False,
    reason: str = "test",
    paused: bool = False,
    wl=None,
) -> _TeardownTurn:
    """Run the REAL `_end_turn_inner` on a turn where nothing else in the
    end_input() gate is set — the mid-hold teardown shape."""
    from jasper.voice_daemon import State

    if wl is None:
        wl = _teardown_loop()
    if paused:
        wl._connection.is_paused = lambda: True
    wl._state = State.SESSION
    turn = _TeardownTurn(
        chunks=chunks,
        turn_lost=turn_lost,
        server_turn_complete=server_turn_complete,
    )
    wl._turn = turn
    wl._bg_tasks = set()
    wl._wake_telemetry.store = None
    wl._session_id = "sess-teardown"
    wl._input_ended = input_ended
    wl._user_speech_seen = user_speech
    wl._manual_endpoint_this_turn = manual

    await wl._end_turn_inner(reason)
    # The teardown must have completed, or "end_input was called" would be
    # an accident of where it stopped rather than of the gate.
    assert wl._state is State.WAKE
    assert turn.release_calls == 1
    return turn


@pytest.mark.parametrize(
    "turn, cue, counted, suppressed",
    [
        # Asked, and the model answered with nothing at all.
        pytest.param(
            {"chunks": 0, "input_ended": True, "user_speech": True},
            "internal_error", 1, None, id="silent_response",
        ),
        # The link went while the model was still speaking: the household
        # got half an answer and then the end chirp.
        pytest.param(
            {"chunks": 2, "input_ended": True, "user_speech": True,
             "turn_lost": True},
            "internal_error", 1, None, id="lost_mid_reply",
        ),
        # A button press proves intent, and nothing scores a button turn's
        # frames — a deaf press is exactly the symptom the cue exists for.
        pytest.param(
            {"chunks": 0, "input_ended": True, "manual": True},
            "internal_error", 1, None, id="push_to_talk_release",
        ),
        # A paused connection owns its own remedy cue; the two wake-path
        # failure sites pick it the same way.
        pytest.param(
            {"chunks": 0, "input_ended": True, "user_speech": True,
             "paused": True},
            "cant_connect", 1, None, id="connection_paused",
        ),
        # A turn the model answered: nothing to count, nothing to say.
        pytest.param(
            {"chunks": 3, "input_ended": True, "user_speech": True},
            None, 0, None, id="answered",
        ),
        # The link dropped only after the model had finished speaking.
        pytest.param(
            {"chunks": 3, "input_ended": True, "user_speech": True,
             "turn_lost": True, "server_turn_complete": True},
            None, 0, None, id="lost_after_the_answer",
        ),
        # Whoever muted the mic knows why the speaker went quiet, so this
        # is not "asked and got no answer": journalled, but not counted
        # and not spoken.
        pytest.param(
            {"chunks": 0, "input_ended": True, "user_speech": True,
             "reason": "mic_muted"},
            None, 0, "mic_muted", id="mic_muted",
        ),
        # Same for a shutdown, and for a wake taking the turn over.
        pytest.param(
            {"chunks": 0, "input_ended": True, "user_speech": True,
             "reason": "stopping"},
            None, 0, "stopping", id="shutdown",
        ),
        pytest.param(
            {"chunks": 0, "input_ended": True, "user_speech": True,
             "reason": "research_window_wake"},
            None, 0, "research_window_wake", id="wake_interruption",
        ),
    ],
)
async def test_a_turn_with_no_answer_is_heard_and_counted(
    turn, cue, counted, suppressed, caplog,
):
    """Non-negotiable 6: the household hears the listening chirp, silence,
    and the end chirp, and is told nothing. A turn that was asked a question
    and produced no answer is counted for /state, logged with that count,
    and spoken about — unless the ending was one the household or the daemon
    chose, which is neither a fault nor news to anyone, and which the
    journal records at INFO instead."""
    wl = _teardown_loop()
    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await _torn_down_mid_hold(wl=wl, **{"manual": False, **turn})

    assert wl._silent_responses_session == counted
    assert wl.session_status()["silent_responses_session"] == counted
    assert wl._cues.played == ([cue] if cue else [])
    records = event_records(caplog, "turn.silent_response")
    assert len(records) == (1 if counted or suppressed else 0)
    if not records:
        return
    # Only what the site can observe. It cannot tell a provider fault from
    # an idle-watchdog reap, so it carries fields, not a diagnosis.
    fields = event_fields(caplog, "turn.silent_response")
    assert fields["provider"] == "test"
    assert int(fields["chunks_received"]) == turn["chunks"]
    assert fields["turn_lost"] == ("true" if turn.get("turn_lost") else "false")
    if suppressed:
        # No count and no WARN, but the turn is not invisible.
        assert records[0].levelno == logging.INFO
        assert fields["suppressed"] == suppressed
        assert "count" not in fields
        return
    assert records[0].levelno == logging.WARNING
    assert int(fields["bytes_sent"]) == 4096
    assert int(fields["count"]) == counted
    assert fields["reason"] == turn.get("reason", "test")


@pytest.mark.parametrize("layer", ["cue_manager", "play_cue"])
async def test_a_failing_no_answer_cue_still_finishes_the_teardown(
    layer, caplog,
):
    """The state flip sits in a `finally` around the cue. Skipped, the
    daemon is left in State.SESSION on a released turn: every mic frame
    drops at `_handle_session_frame`'s input-closed branch — permanent
    deafness — and the next `_end_turn` trips the `_session_id` assert."""
    from jasper.voice_daemon import State

    wl = _teardown_loop()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("cue backend down")

    if layer == "cue_manager":
        wl._cues.play = _boom
    else:
        wl._play_cue = _boom

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await _torn_down_mid_hold(
            wl=wl, manual=False, chunks=0, input_ended=True, user_speech=True,
        )

    # `_torn_down_mid_hold` already pins State.WAKE; the released turn and a
    # usable `_end_turn` gate are the other half of "still able to listen".
    assert wl._state is State.WAKE
    assert wl._turn is None
    assert wl._session_id is None


async def test_the_no_answer_cue_waits_for_state_wake(caplog):
    """A cue is seconds of assistant speech. Played after State.WAKE it
    would be scored by every wake leg — the raw leg has no AEC reference
    for it — and WAKE_REFRACTORY_SEC (0.2 s) cannot cover it. The cue plays
    while the turn is still SESSION with input closed, the same regime that
    keeps the assistant's own reply off the detectors."""
    from jasper.voice_daemon import State

    wl = _teardown_loop()
    seen: list[State] = []

    async def _play(slug: str) -> bool:
        seen.append(wl._state)
        return True

    wl._cues.play = _play
    await _torn_down_mid_hold(
        wl=wl, manual=False, chunks=0, input_ended=True, user_speech=True,
    )

    assert seen == [State.SESSION]
    assert wl._state is State.WAKE


async def test_every_silent_response_is_logged_with_a_rising_count(caplog):
    """#2228 latched this WARN per daemon, so an operator whose journal
    window missed the first turn saw nothing at all. The count replaces the
    latch: repetition is now the signal, not the noise."""
    wl = _teardown_loop()
    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        for _ in range(2):
            await _torn_down_mid_hold(
                manual=False, chunks=0, input_ended=True,
                user_speech=True, wl=wl,
            )

    counts = [
        int(fields["count"])
        for fields in event_field_maps(caplog, "turn.silent_response")
    ]
    assert counts == [1, 2]
    assert wl._silent_responses_session == 2


async def test_teardown_still_calls_end_input_on_a_button_turn():
    """`_user_speech_seen` no longer flips on a button turn, so without
    `_manual_endpoint_this_turn` in the teardown gate a turn torn down
    mid-hold (idle watchdog, stop event, a release that never came) would
    stop calling end_input() — which it did before local VAD came off
    this path.

    Drives the real `_end_turn_inner` to completion, so deleting the term
    from the gate turns this red.
    """
    turn = await _torn_down_mid_hold(manual=True)
    assert turn.end_input_calls == 1


async def test_teardown_skips_end_input_when_nothing_in_the_gate_is_set():
    """The other half of the mutation: the identical teardown on a wake
    turn that saw no speech and never closed input does NOT finalise, so
    the manual term above is doing real work rather than riding along with
    a gate that was already true."""
    turn = await _torn_down_mid_hold(manual=False)
    assert turn.end_input_calls == 0


def test_teardown_gate_includes_the_manual_term():
    """Source-level pin for the mutation above: the gate expression in
    `_end_turn` must actually carry `_manual_endpoint_this_turn`. Without
    this, deleting that term leaves every behavioural test green."""
    import ast
    import inspect

    from jasper.voice_daemon import WakeLoop

    src = inspect.getsource(WakeLoop._end_turn_inner)
    gates = [
        node
        for node in ast.walk(ast.parse(src.lstrip()))
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        and {
            n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
        } >= {"_input_ended", "_user_speech_seen"}
    ]
    assert gates, "no `_input_ended or _user_speech_seen` gate in _end_turn"
    names = {
        n.attr for g in gates for n in ast.walk(g)
        if isinstance(n, ast.Attribute)
    }
    assert "_manual_endpoint_this_turn" in names, (
        "the teardown end_input() gate dropped _manual_endpoint_this_turn; "
        "a button turn torn down mid-hold would stop finalising its input"
    )


# ---------------------------------------------------------------------------
# Barge-in on a button turn refuses loudly rather than going inert
# ---------------------------------------------------------------------------


def test_barge_in_refused_on_a_button_turn_and_says_why(
    monkeypatch, tmp_path, caplog,
):
    """Enabling barge-in on a push-to-talk speaker must not silently do
    nothing. `_barge_in_reference_available` was computed from
    cfg.mic_device, which is not the stream a button turn scores — so the
    self-interrupt guard has not cleared that audio."""
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
        first = event_records(caplog, "barge.disabled_push_to_talk")
        wl._resolve_barge_in_for_turn()
        second = event_records(caplog, "barge.disabled_push_to_talk")

    assert wl._barge_in_active is False
    # One-shot per daemon, like the no-reference WARN it sits beside.
    assert len(first) == 1
    assert len(second) == 1
    fields = event_fields(caplog, "barge.disabled_push_to_talk")
    assert fields["source"] == "wiim_remote_2"
    assert fields["provider"] == "gemini"


def test_barge_in_still_enabled_on_a_wake_turn(monkeypatch, tmp_path):
    """Mutation of the guard above."""
    from jasper.voice_daemon import WakeLoop

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = WakeLoop.for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True
    wl._manual_endpoint_this_turn = False

    wl._resolve_barge_in_for_turn()

    assert wl._barge_in_active is True


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

    assert len(event_records(caplog, "barge.disabled_push_to_talk")) == 1
    assert event_fields(caplog, "barge.disabled_no_reference")["mic_device"] == (
        "Array"
    )


# ---------------------------------------------------------------------------
# Push-to-talk-only is reachable by PROFILE, not only by a missing room mic
# ---------------------------------------------------------------------------


def _wake_leg_cfg():
    """Streambox-shaped `_cfg` (tests/test_voice_daemon_wake_leg_planning.py):
    `mic_device` still carries the literal `"Array"` default and the AEC
    reconciler never ran there, so nothing in the config says "no
    always-listening mic", plus a paired remote's manual mic source."""
    from tests.test_voice_daemon_wake_leg_planning import _cfg
    return _cfg(
        mic_device="Array",
        manual_mic_sources={"wiim_remote_2": "udp:9892"},
    )


@pytest.mark.parametrize(
    "wake_detection_supported, expected_tokens, expected_ptt_only",
    [(False, [], True), (True, ["on"], False)],
)
def test_wake_legs_follow_the_profiles_wake_detection_grant(
    wake_detection_supported, expected_tokens, expected_ptt_only,
):
    """A board without the headroom for always-on inference plans NO legs,
    and the daemon it builds is push-to-talk-only by derivation.

    Config alone cannot reach that verdict on such a box: `mic_device` reads
    the shipped `"Array"` default and `local_mic_present` is None, so both
    of the older no-leg terms are silent. Opening `"Array"` there raises
    `InputDeviceUnavailable` and the daemon exits before it ever sees the
    remote. See ADR-0217.
    """
    from jasper.voice_daemon import (
        WakeLoop,
        _configured_wake_legs,
        _ManualMicRuntime,
        _UNSET,
    )

    plan = _configured_wake_legs(
        _wake_leg_cfg(), wake_detection_supported=wake_detection_supported,
    )
    assert [spec.token for spec, _device in plan] == expected_tokens

    wl = WakeLoop.for_tests(
        legs=_UNSET if plan else [],
        manual_mics=[_ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
    )
    assert wl._push_to_talk_only is expected_ptt_only


def test_wake_detection_supported_fails_open_on_an_unreadable_install_profile(
    monkeypatch, caplog,
):
    """A `ValueError` from `read_install_profile()` (a corrupt/unrecognized
    marker token) must not traceback out of `main()` — only
    `InputDeviceUnavailable`/`VoiceProviderNotConfigured`/
    `SpeechVADSetupError` are special-cased there, so anything else would
    exit 1 and climb `Restart=on-failure` to `StartLimitAction=reboot`.
    Fail OPEN: keep today's behaviour, planning wake legs as if the profile
    always granted WAKE_DETECTION. See ADR-0217.
    """
    from jasper.voice import daemon_main
    from jasper.voice_daemon import _configured_wake_legs

    reason = "invalid install profile 'bogus'"

    def _raise():
        raise ValueError(reason)

    monkeypatch.setattr(daemon_main, "read_install_profile", _raise)

    with caplog.at_level(logging.WARNING):
        supported = daemon_main._wake_detection_supported()

    assert supported is True
    # The unreadable marker's own reason travels into the event: a pass-through
    # of `str(e)`, not a pin on wording this test chose.
    assert event_fields(caplog, "voice.install_profile_unreadable")["detail"] == (
        reason
    )

    plan = _configured_wake_legs(_wake_leg_cfg(), wake_detection_supported=supported)
    assert [spec.token for spec, _device in plan] == ["on"]


@pytest.mark.parametrize("ptt_only, expect_vad", [(True, False), (False, True)])
def test_silero_is_built_only_where_a_turn_can_ever_read_it(
    monkeypatch, ptt_only, expect_vad,
):
    """Every `_vad` reader is already off on a button turn, so a
    push-to-talk-only daemon must not pay for the model at all —
    constructing `SpeechVAD` is what pulls openwakeword + onnxruntime into
    a 415 MB box's resident set."""
    from jasper import voice_daemon as vd

    built: list[object] = []

    class _CountingVad:
        def __init__(self) -> None:
            built.append(self)

        def predict(self, _frame) -> float:
            return 0.0

        def reset(self) -> None:
            return None

    monkeypatch.setattr(vd, "SpeechVAD", _CountingVad)

    wl = vd.WakeLoop.for_tests(
        legs=[] if ptt_only else vd._UNSET,
        manual_mics=[vd._ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
        vad=None,
    )

    assert (wl._vad is not None) is expect_vad
    assert len(built) == (0 if ptt_only else 1)


class _AcquiredTurn:
    """Enough of a LiveTurn for the real `_begin_turn` to run to the end."""

    async def send_audio(self, _data) -> None:
        return None

    def turn_lost(self) -> bool:
        return False

    def server_turn_complete(self) -> bool:
        return False

    def last_activity_at(self) -> float:
        return asyncio.get_event_loop().time()

    def last_chunk_at(self) -> float:
        return 0.0

    def audio_chunks_pending(self) -> int:
        return 0

    async def audio_out(self):
        await asyncio.sleep(3600)
        yield b""


async def _drive_begin_turn(wl):
    """Run the real `_begin_turn`, then tear down the tasks it spawned."""

    async def _noop(*_a, **_k) -> None:
        return None

    async def _acquire():
        return _AcquiredTurn()

    wl._connection.acquire_turn = lambda: _acquire()
    wl._content_activity.music_is_playing = lambda: True
    wl._content_activity.refresh_now = _noop
    wl._tts.pause_content_meter = _noop

    try:
        await wl._begin_turn()
    finally:
        for t in wl._bg_tasks:
            t.cancel()
        for t in wl._bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        wl._bg_tasks = set()


async def test_a_button_turn_begins_on_a_daemon_that_never_built_silero():
    """`_begin_turn`'s LSTM reset was the ONE `_vad` site the push-to-talk
    flags did not already gate, so it is the first thing a held button
    would hit on a speaker that has no model to reset."""
    from jasper import voice_daemon as vd

    wl = vd.WakeLoop.for_tests(
        legs=[],
        manual_mics=[vd._ManualMicRuntime("wiim_remote_2", object(), "udp:9892")],
        vad=None,
    )
    wl._active_manual_source = "wiim_remote_2"

    await _drive_begin_turn(wl)

    assert wl._vad is None
    assert wl._manual_endpoint_this_turn is True
