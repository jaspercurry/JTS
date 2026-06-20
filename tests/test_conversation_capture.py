from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path


def _optional_module_available(name: str) -> bool:
    if name in sys.modules:
        return True
    if importlib.util.find_spec(name) is None:
        return False
    __import__(name)
    return True


if not _optional_module_available("httpx"):
    httpx = types.ModuleType("httpx")

    class _Timeout:
        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

    httpx.Timeout = _Timeout
    sys.modules["httpx"] = httpx
if not _optional_module_available("sounddevice"):
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")
if not _optional_module_available("rapidfuzz"):
    rapidfuzz = types.ModuleType("rapidfuzz")
    rapidfuzz.fuzz = types.SimpleNamespace()
    sys.modules["rapidfuzz"] = rapidfuzz


class _BaseTurn:
    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    async def end_input(self) -> None:
        return None

    async def release(self) -> None:
        return None

    def usage_tokens(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def usage_breakdown(self):
        return None

    def bytes_sent(self) -> int:
        return 1600

    def chunks_received(self) -> int:
        return 1

    def turn_lost(self) -> bool:
        return False


class _TranscriptTurn(_BaseTurn):
    def __init__(
        self,
        *,
        user_text: str | None = "turn on the kitchen lights",
        assistant_text: str | None = "Turning on the kitchen lights.",
    ) -> None:
        self._user_text = user_text
        self._assistant_text = assistant_text

    def user_transcript(self) -> str | None:
        return self._user_text

    def assistant_transcript(self) -> str | None:
        return self._assistant_text


class _RecordingStore:
    available = True

    def __init__(self) -> None:
        self.turns = []

    def add(self, turn):
        self.turns.append(turn)
        return True


class _UnavailableStore:
    available = False

    def __init__(self) -> None:
        self.add_calls = 0

    def add(self, turn):
        self.add_calls += 1
        return False


class _RaisingStore:
    available = True

    def add(self, turn):
        raise OSError("db is gone")


def _write_capture_flag(path: Path, enabled: bool) -> None:
    path.write_text(
        f"JASPER_CONVERSATION_CAPTURE={1 if enabled else 0}\n",
        encoding="utf-8",
    )


def _wake_loop(tmp_path: Path, *, turn, store, capture_enabled: bool = True):
    from jasper.voice_daemon import State, WakeLoop

    flag_path = tmp_path / "conversation_history.env"
    _write_capture_flag(flag_path, capture_enabled)
    wl = WakeLoop.for_tests(
        conversation_capture_path=str(flag_path),
        conversation_store=store,
    )
    wl._state = State.SESSION
    wl._turn = turn
    wl._session_id = 123

    async def _noop_stage(_stage):
        return None

    async def _noop_outcome(_outcome, _detail=None):
        return None

    async def _noop_peering(_reason):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._telemetry_stage = _noop_stage
    wl._telemetry_outcome = _noop_outcome
    wl._notify_peering_session_ended = _noop_peering
    wl._play_listening_chirp = _noop_chirp
    return wl


def test_capture_enabled_writes_turn_with_transcripts(tmp_path: Path) -> None:
    store = _RecordingStore()
    wl = _wake_loop(tmp_path, turn=_TranscriptTurn(), store=store)

    asyncio.run(wl._end_turn())

    assert len(store.turns) == 1
    turn = store.turns[0]
    assert turn.id.endswith("-123")
    assert turn.provider == "test"
    assert turn.user_text == "turn on the kitchen lights"
    assert turn.assistant_text == "Turning on the kitchen lights."
    assert turn.tool_calls_json is None
    assert turn.data_json is None
    assert turn.session_id == 123


def test_capture_flag_off_skips_write(tmp_path: Path) -> None:
    store = _RecordingStore()
    wl = _wake_loop(
        tmp_path,
        turn=_TranscriptTurn(),
        store=store,
        capture_enabled=False,
    )

    asyncio.run(wl._end_turn())

    assert store.turns == []


def test_mic_muted_skips_write(tmp_path: Path) -> None:
    store = _RecordingStore()
    wl = _wake_loop(tmp_path, turn=_TranscriptTurn(), store=store)
    wl._mic_muted = True

    asyncio.run(wl._end_turn())

    assert store.turns == []


def test_mute_triggered_teardown_skips_write(tmp_path: Path) -> None:
    store = _RecordingStore()
    wl = _wake_loop(tmp_path, turn=_TranscriptTurn(), store=store)

    asyncio.run(wl._end_turn("mic_muted"))

    assert store.turns == []


def test_absent_accessors_do_not_crash_or_write(tmp_path: Path) -> None:
    store = _RecordingStore()
    wl = _wake_loop(tmp_path, turn=_BaseTurn(), store=store)

    asyncio.run(wl._end_turn())

    assert store.turns == []


def test_store_add_exception_is_logged_and_does_not_escape(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="jasper.voice_daemon")
    wl = _wake_loop(tmp_path, turn=_TranscriptTurn(), store=_RaisingStore())

    asyncio.run(wl._end_turn())

    assert "event=conversation.capture_failed" in caplog.text
    assert "turn on the kitchen lights" not in caplog.text


def test_unavailable_store_is_logged_and_does_not_call_add(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="jasper.voice_daemon")
    store = _UnavailableStore()
    wl = _wake_loop(tmp_path, turn=_TranscriptTurn(), store=store)

    asyncio.run(wl._end_turn())

    assert store.add_calls == 0
    assert "event=conversation.capture_failed" in caplog.text
    assert "reason=store_unavailable" in caplog.text
