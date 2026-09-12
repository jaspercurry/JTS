"""In-session barge-in detection (the provider-agnostic spine, PR-2).

These pin the felt behaviour without hardware: while the assistant is
speaking (``_input_ended`` set), a sustained run of speech on the
AEC-cleaned mic leg flushes local TTS via the turn's interrupt event.

The safety contract under test:

  * DEFAULT OFF => byte-identical to the old "drop the mic during
    playback" behaviour: no VAD scoring, no interrupt, no audio forward.
  * Flag ON => synthetic high-Silero frames trip ``request_local_interrupt``
    once a sustained run accumulates, and only then.
  * Self-interrupt guard: barge-in requested on a profile with no AEC
    reference (direct_mic) hard-disables for the turn and WARNs once,
    rather than self-trip on un-cancelled TTS bleed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.config import Config

from tests._live_turn_fake import silent_frame
from tests._log_events import event_fields, event_records
from tests._wake_loop import wake_loop_for_tests


class _SpyTurn:
    """LiveTurn stand-in exposing just the barge-in + forward surface."""

    def __init__(
        self,
        *,
        continuous_input: bool = False,
        owns_interruption: bool = False,
        chunks_pending: int = 0,
    ) -> None:
        self.continuous_input = continuous_input
        self.owns_interruption = owns_interruption
        self._chunks_pending = chunks_pending
        self._interrupt_event = asyncio.Event()
        self._interrupted = False
        self.local_interrupt_calls = 0
        self.send_audio_calls = 0
        self.speech_run_marks = 0
        self.sent: list[bytes] = []

    def audio_chunks_pending(self) -> int:
        return self._chunks_pending

    def mark_user_speech_run(self) -> None:
        self.speech_run_marks += 1

    def request_local_interrupt(self) -> None:
        self.local_interrupt_calls += 1
        self._interrupted = True
        self._interrupt_event.set()

    async def wait_for_interrupt(self) -> None:
        await self._interrupt_event.wait()

    def clear_interrupted(self) -> None:
        self._interrupted = False
        self._interrupt_event.clear()

    async def send_audio(self, data) -> None:
        self.send_audio_calls += 1
        self.sent.append(data)


class _FixedVad:
    """Silero stand-in returning a fixed probability + a predict counter."""

    def __init__(self, score: float) -> None:
        self.score = score
        self.predict_calls = 0

    def predict(self, _frame) -> float:
        self.predict_calls += 1
        return self.score

    def reset(self) -> None:
        return None


def _playback_loop(*, score: float, active: bool, ref_ok: bool = True):
    """A WakeLoop parked mid-playback (``_input_ended`` set)."""
    from jasper.voice.turn_lifecycle import State

    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.turn = _SpyTurn()
    wl._vad = _FixedVad(score)
    wl._turns.bg_tasks = set()
    wl._turns.input_ended = True
    wl._turns.barge_in_active = active
    wl._barge_in_reference_available = ref_ok
    wl._barge_in_run_started_at = 0.0
    wl._barge_in_run_peak = 0.0
    wl._barge_in_signalled_this_run = False
    return wl


# --- DEFAULT OFF: byte-identical drop ----------------------------------


def test_flag_off_frame_after_input_ended_is_dropped_exactly():
    """Pinning test: with barge-in disabled, a frame arriving after
    ``_input_ended`` is dropped exactly as before — the VAD is never
    scored, no interrupt is raised, and nothing is forwarded."""
    wl = _playback_loop(score=0.99, active=False)
    turn = wl._turns.turn
    vad = wl._vad

    asyncio.run(wl._handle_session_frame(silent_frame()))

    assert vad.predict_calls == 0
    assert turn.local_interrupt_calls == 0
    assert turn.send_audio_calls == 0
    assert not turn._interrupt_event.is_set()
    # Run state untouched — the playback branch was never entered.
    assert wl._barge_in_run_started_at == 0.0


# --- Flag ON: sustained run trips the interrupt ------------------------


def test_flag_on_single_frame_does_not_trip():
    """One supra-threshold frame starts a run but does not (yet) flush —
    the sustained-arming window must elapse first."""
    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turns.turn

    asyncio.run(wl._handle_session_frame(silent_frame()))

    assert turn.local_interrupt_calls == 0
    assert not turn._interrupt_event.is_set()
    assert wl._barge_in_run_started_at != 0.0  # run armed


def test_flag_on_sustained_run_trips_interrupt():
    """Once the run has lasted >= the arming window, a further
    supra-threshold frame sets the turn's interrupt event exactly once."""
    from jasper.voice_daemon import BARGE_IN_SUSTAINED_SPEECH_SEC

    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turns.turn

    async def drive() -> None:
        await wl._handle_session_frame(silent_frame())  # arms the run
        # Simulate the arming window elapsing without real sleeps.
        wl._barge_in_run_started_at -= BARGE_IN_SUSTAINED_SPEECH_SEC + 0.05
        await wl._handle_session_frame(silent_frame())  # now sustained -> trip
        await wl._handle_session_frame(silent_frame())  # one-shot: no re-trigger

    asyncio.run(drive())

    assert turn.local_interrupt_calls == 1
    assert turn._interrupt_event.is_set()


def test_barge_in_telemetry_surfaces_through_session_status():
    """A fired barge-in increments the daemon-lifetime counters that
    /state.voice.barge_in pulls through from session_status."""
    from jasper.voice_daemon import BARGE_IN_SUSTAINED_SPEECH_SEC

    wl = _playback_loop(score=0.9, active=True)

    base = wl.session_status()
    assert base["barge_in_count_session"] == 0
    assert base["barge_in_last_at"] is None
    assert base["barge_in_last_leg"] is None

    async def drive() -> None:
        await wl._handle_session_frame(silent_frame())  # arm
        wl._barge_in_run_started_at -= BARGE_IN_SUSTAINED_SPEECH_SEC + 0.05
        await wl._handle_session_frame(silent_frame())  # trip

    asyncio.run(drive())

    fired = wl.session_status()
    assert fired["barge_in_count_session"] == 1
    assert fired["barge_in_last_leg"] == "on"
    assert isinstance(fired["barge_in_last_at"], str) and fired["barge_in_last_at"]


def test_flag_on_subthreshold_breaks_run():
    """A sub-threshold frame resets the run so a stale anchor can't trip
    later, and re-arms the one-shot for a fresh run."""
    wl = _playback_loop(score=0.9, active=True)
    turn = wl._turns.turn

    async def drive() -> None:
        await wl._handle_session_frame(silent_frame())  # arm
        wl._barge_in_run_started_at -= 1.0  # would trip on next supra frame
        wl._vad.score = 0.1  # ...but a quiet frame lands first
        await wl._handle_session_frame(silent_frame())

    asyncio.run(drive())

    assert turn.local_interrupt_calls == 0
    assert wl._barge_in_run_started_at == 0.0
    assert wl._barge_in_signalled_this_run is False


def test_flag_on_threshold_respected():
    """A frame just under the configured threshold never arms the run."""
    wl = _playback_loop(score=0.49, active=True)  # cfg threshold 0.5
    turn = wl._turns.turn

    asyncio.run(wl._handle_session_frame(silent_frame()))

    assert turn.local_interrupt_calls == 0
    assert wl._barge_in_run_started_at == 0.0


# --- The provider that owns interruption gets no host flush ------------


def _continuous_loop(*, owns_interruption: bool, score: float = 0.9, ref_ok: bool = True):
    """A WakeLoop mid-turn on a continuous-input provider that is speaking."""
    from jasper.voice.turn_lifecycle import State

    wl = wake_loop_for_tests()
    wl._turns.state = State.SESSION
    wl._turns.turn = _SpyTurn(
        continuous_input=True,
        owns_interruption=owns_interruption,
        # Assistant audio still queued => the daemon reads the turn as speaking.
        chunks_pending=1,
    )
    wl._vad = _FixedVad(score)
    wl._turns.bg_tasks = set()
    wl._turns.barge_in_active = True
    wl._barge_in_reference_available = ref_ok
    return wl


async def _drive_sustained_speech(wl) -> None:
    """Two frames either side of the sustained-arming window."""
    from jasper.voice_daemon import SUSTAINED_SPEECH_TO_ARM_SEC

    await wl._handle_session_frame(silent_frame())
    wl._speech_run_started_at -= SUSTAINED_SPEECH_TO_ARM_SEC + 0.05
    await wl._handle_session_frame(silent_frame())


@pytest.mark.parametrize("owns_interruption", [False, True])
def test_continuous_barge_in_flushes_only_when_the_host_owns_interruption(
    caplog, owns_interruption,
):
    """A provider that stops itself on the user's voice must not be flushed
    by the host: local detection scores the assistant's own echo too, and the
    flush chops the reply mid-word. One that does not stop itself keeps the
    flush AND becomes observable — the continuous path used to interrupt with
    no event and no counter at all."""
    wl = _continuous_loop(owns_interruption=owns_interruption)
    turn = wl._turns.turn

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        asyncio.run(_drive_sustained_speech(wl))

    expected = 0 if owns_interruption else 1
    assert turn.local_interrupt_calls == expected
    assert turn._interrupt_event.is_set() is (not owns_interruption)
    assert wl.session_status()["barge_in_count_session"] == expected
    assert len(event_records(caplog, "barge.detected")) == expected
    # Either way the user's audio keeps reaching the provider.
    assert turn.send_audio_calls == 2


def test_continuous_barge_in_reports_the_same_fields_as_the_playback_path(caplog):
    """One vocabulary for both endpointer paths, so /state and the journal
    describe a barge-in the same way whichever path detected it."""
    wl = _continuous_loop(owns_interruption=False)

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        asyncio.run(_drive_sustained_speech(wl))

    fields = event_fields(caplog, "barge.detected")
    assert fields["leg"] == "on"
    assert float(fields["silero"]) == pytest.approx(0.9, abs=0.005)
    assert int(fields["sustained_ms"]) >= 200
    assert fields["reconcile"] == "needs_client_truncate"
    status = wl.session_status()
    assert status["barge_in_last_leg"] == "on"
    assert isinstance(status["barge_in_last_at"], str) and status["barge_in_last_at"]


@pytest.mark.parametrize("owns_interruption", [False, True])
def test_unreferenced_speech_reaches_a_turn_that_owns_interruption(
    owns_interruption,
):
    """On a profile with no AEC reference the host substitutes digital
    silence so its own endpointer cannot score the echo. A turn whose
    provider owns interruption has no other stop path, so it gets the real
    room audio instead — otherwise the answer cannot be interrupted at all."""
    wl = _continuous_loop(owns_interruption=owns_interruption, ref_ok=False)
    frame = silent_frame()
    frame[:8] = 1000

    asyncio.run(wl._handle_session_frame(frame))

    (sent,) = wl._turns.turn.sent
    assert sent == (frame.tobytes() if owns_interruption else bytes(frame.nbytes))


def test_owning_interruption_does_not_outlive_conversation_end(monkeypatch):
    """The exemption covers the host's own barge-in flush and nothing else:
    a requested conversation end still ends the turn."""
    wl = _continuous_loop(owns_interruption=True)
    wl._turns.conversation_end_requested = True
    ended: list[str] = []

    async def _spy(reason: str = "ended") -> None:
        ended.append(reason)

    monkeypatch.setattr(wl._turns, "end", _spy)
    asyncio.run(wl._handle_session_frame(silent_frame()))

    assert ended == ["conversation_ended"]
    assert wl._turns.turn.send_audio_calls == 0


# --- Self-interrupt-loop guard -----------------------------------------


def test_resolve_disables_barge_in_without_aec_reference(monkeypatch, tmp_path, caplog):
    """Barge-in requested on a profile with no AEC reference is hard-
    disabled for the turn and WARNs once — the self-interrupt guard."""

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = wake_loop_for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._cfg.mic_device = "Array"
    wl._barge_in_reference_available = False
    wl._turns._barge_in_no_ref_warned = False

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        wl._turns._resolve_barge_in_for_turn()
        first = event_records(caplog, "barge.disabled_no_reference")
        # WARN is one-shot per daemon — a second turn does not re-spam.
        wl._turns._resolve_barge_in_for_turn()
        second = event_records(caplog, "barge.disabled_no_reference")

    assert wl._turns.barge_in_active is False
    assert len(first) == 1
    assert len(second) == 1


def test_resolve_enables_barge_in_with_reference(monkeypatch, tmp_path):
    """Flag on + AEC reference present => barge-in active for the turn,
    read fresh from the SSOT file."""

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_GEMINI=on\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = wake_loop_for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True

    wl._turns._resolve_barge_in_for_turn()

    assert wl._turns.barge_in_active is True


def test_resolve_defaults_off(monkeypatch, tmp_path):
    """No flag in the SSOT file => barge-in stays OFF even with a valid
    provider and a reference present."""

    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))

    wl = wake_loop_for_tests()
    wl._cfg.voice_provider = "gemini"
    wl._barge_in_reference_available = True

    wl._turns._resolve_barge_in_for_turn()

    assert wl._turns.barge_in_active is False


@pytest.mark.parametrize("chip", [False, True])
@pytest.mark.parametrize("device,port,eligible", [
    ("udp:9876", "9876", True),
    (" UDP:9876 ", "9876", True),
    ("udp://127.0.0.1:5555", "5555", True),
    ("udp:9876", "5555", False),
    ("udp:9877", "9876", False),
    ("udp:9999", "9876", False),
    ("udp://192.0.2.10:9876", "9876", False),
    ("udp://0.0.0.0:9876", "9876", True),
    ("Array", "9876", False),
])
def test_barge_in_requires_processing_on_the_selected_stream(
    monkeypatch, tmp_path, chip, device, port, eligible,
):
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_MIC_DEVICE", device)
    monkeypatch.setenv("JASPER_AEC_UDP_PORT", port)
    monkeypatch.delenv("JASPER_AEC_UDP_HOST", raising=False)
    monkeypatch.setenv("JASPER_AEC_CHIP_AEC_ENABLED", str(int(chip)))
    path = tmp_path / "voice_provider.env"
    path.write_text("JASPER_BARGE_IN_OPENAI=1\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_FILE", str(path))
    wl = wake_loop_for_tests(cfg=Config.from_env())
    wl._turns._resolve_barge_in_for_turn()
    assert wl._turns.barge_in_active is eligible


def test_disabled_branch_never_calls_playback_handler(monkeypatch):
    """Belt-and-suspenders for the pinning contract: the dispatch only
    enters the playback handler when the flag is active."""
    wl = _playback_loop(score=0.99, active=False)
    called = {"n": 0}

    async def _spy(_frame, *, captured_at):
        called["n"] += 1

    monkeypatch.setattr(wl, "_handle_playback_frame", _spy)
    asyncio.run(wl._handle_session_frame(silent_frame()))
    assert called["n"] == 0

    wl._turns.barge_in_active = True
    asyncio.run(wl._handle_session_frame(silent_frame()))
    assert called["n"] == 1
