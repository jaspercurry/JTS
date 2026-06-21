from __future__ import annotations

import json
import sys
import types

from jasper.conversation_history import (
    CAPTURE_ALIAS_ENV,
    ConversationStore,
    DB_PATH_ENV,
)
from jasper.research import DONE, ResearchJob


if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")


class _FakeTurn:
    def __init__(self, user_text: str | None, assistant_text: str | None) -> None:
        self._user_text = user_text
        self._assistant_text = assistant_text

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
        return 0

    def chunks_received(self) -> int:
        return 0

    def turn_lost(self) -> bool:
        return False

    def user_transcript(self) -> str | None:
        return self._user_text

    def assistant_transcript(self) -> str | None:
        return self._assistant_text


class _FakeUsageStore:
    write_degraded = False

    def close_session(self, session_id, _in_tokens, _out_tokens, usage=None):
        assert session_id is not None
        return 0.0


class _MarkingScheduler:
    def __init__(self) -> None:
        self.announced: list[str] = []
        self.read: list[str] = []

    def mark_announced(self, job_id: str) -> None:
        self.announced.append(job_id)

    def mark_read(self, job_id: str) -> None:
        self.read.append(job_id)


def _wake_loop(tmp_path, monkeypatch, *, capture: bool = True):
    from jasper.voice_daemon import WakeLoop

    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1" if capture else "0")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    store = ConversationStore(str(db_path))
    wl = WakeLoop.for_tests(conversation_store=store)
    return wl, store


def _put_in_session(wl, turn: _FakeTurn) -> None:
    from jasper.voice_daemon import State

    wl._state = State.SESSION
    wl._turn = turn
    wl._session_id = 7
    wl._usage_store = _FakeUsageStore()
    wl._user_speech_seen = True
    wl._server_vad_this_turn = False
    wl._input_ended = False

    async def _noop(*_args, **_kwargs):
        return None

    async def _noop_chirp(*, going_on):
        return None

    wl._telemetry_stage = _noop
    wl._telemetry_outcome = _noop
    wl._notify_peering_session_ended = _noop
    wl._play_listening_chirp = _noop_chirp


async def test_end_turn_records_transcripts_through_single_write_path(
    tmp_path,
    monkeypatch,
) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    _put_in_session(wl, _FakeTurn("what is the next train", "Four minutes."))

    await wl._end_turn_inner("test")

    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].provider == "test"
    assert rows[0].user_text == "what is the next train"
    assert rows[0].assistant_text == "Four minutes."
    assert rows[0].session_id == 7
    assert rows[0].data_json is None


def test_record_conversation_turn_is_gated_by_capture_env(tmp_path, monkeypatch) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch, capture=False)

    wl._record_conversation_turn("hello", "hi")

    assert store.recent(10) == []


def test_record_conversation_turn_lazily_opens_store_after_capture_enabled(
    tmp_path,
    monkeypatch,
) -> None:
    from jasper.voice_daemon import WakeLoop

    db_path = tmp_path / "conversation_history.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1")
    monkeypatch.setenv(DB_PATH_ENV, str(db_path))
    wl = WakeLoop.for_tests()

    wl._record_conversation_turn("hello", "hi")

    assert wl._conversation_store_path == str(db_path)
    assert wl._conversation_store is not None
    rows = wl._conversation_store.recent(10)
    assert len(rows) == 1
    assert rows[0].user_text == "hello"
    assert rows[0].assistant_text == "hi"


def test_record_conversation_turn_reopens_store_when_db_path_changes(
    tmp_path,
    monkeypatch,
) -> None:
    from jasper.voice_daemon import WakeLoop

    first_db = tmp_path / "first.db"
    second_db = tmp_path / "second.db"
    monkeypatch.setenv(CAPTURE_ALIAS_ENV, "1")
    monkeypatch.setenv(DB_PATH_ENV, str(first_db))
    wl = WakeLoop.for_tests()

    wl._record_conversation_turn("first", "one")
    monkeypatch.setenv(DB_PATH_ENV, str(second_db))
    wl._record_conversation_turn("second", "two")

    assert wl._conversation_store_path == str(second_db)
    first_reader = ConversationStore(str(first_db), read_only=True)
    second_reader = ConversationStore(str(second_db), read_only=True)
    try:
        assert [row.user_text for row in first_reader.recent(10)] == ["first"]
        assert [row.user_text for row in second_reader.recent(10)] == ["second"]
    finally:
        first_reader.close()
        second_reader.close()


def test_record_conversation_turn_skips_while_mic_muted(tmp_path, monkeypatch) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    wl._mic_muted = True

    wl._record_conversation_turn("hello", "hi")

    assert store.recent(10) == []


async def test_research_readback_records_query_report_and_data_json(
    tmp_path,
    monkeypatch,
) -> None:
    wl, store = _wake_loop(tmp_path, monkeypatch)
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]
    job = ResearchJob(
        id="research123",
        query="research induction cooktops",
        status=DONE,
        result="Induction is fast and efficient.",
        error=None,
        created_at=1.0,
        finished_at=2.0,
        announced=False,
        read=False,
    )

    await wl.announce_research_ready(job)

    assert spoken == [
        "Hey, your research is ready. Induction is fast and efficient.",
    ]
    assert scheduler.announced == ["research123"]
    assert scheduler.read == ["research123"]
    rows = store.recent(10)
    assert len(rows) == 1
    assert rows[0].user_text == "research induction cooktops"
    assert rows[0].assistant_text == "Induction is fast and efficient."
    assert json.loads(rows[0].data_json or "{}") == {
        "kind": "research",
        "job_id": "research123",
    }
