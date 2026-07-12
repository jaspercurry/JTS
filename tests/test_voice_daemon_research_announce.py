# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time
import types

import numpy as np
import pytest

from jasper.research import (
    DONE,
    FAILED,
    RUNNING,
    ResearchJob,
    ResearchJobStore,
    ResearchScheduler,
)

def _wake_loop():
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.WAKE
    return wl


def _job(
    *,
    id: str = "job12345",
    status=DONE,
    result: str | None = "Use induction if you want fast response.",
    error: str | None = None,
    created_at: float | None = None,
    announced: bool = False,
    read: bool = False,
) -> ResearchJob:
    now = created_at if created_at is not None else time.time()
    return ResearchJob(
        id=id,
        query="research cooktops",
        status=status,
        result=result,
        error=error,
        created_at=now,
        finished_at=None if status == RUNNING else now,
        announced=announced,
        read=read,
    )


class _MarkingScheduler:
    def __init__(self) -> None:
        self.announced: list[str] = []
        self.read: list[str] = []

    def mark_announced(self, job_id: str) -> None:
        self.announced.append(job_id)

    def mark_read(self, job_id: str) -> None:
        self.read.append(job_id)


class _FakeTurn:
    def __init__(self, *, bytes_sent: int = 0, chunks_received: int = 0) -> None:
        self.end_input_calls = 0
        self.release_calls = 0
        self._bytes_sent = bytes_sent
        self._chunks_received = chunks_received

    def last_chunk_at(self) -> float:
        return 0.0

    def last_activity_at(self) -> float:
        return 0.0

    async def end_input(self) -> None:
        self.end_input_calls += 1
        return None

    async def release(self) -> None:
        self.release_calls += 1
        return None

    def usage_tokens(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def usage_breakdown(self):
        return None

    def bytes_sent(self) -> int:
        return self._bytes_sent

    def chunks_received(self) -> int:
        return self._chunks_received

    def turn_lost(self) -> bool:
        return False


class _FakeUsageStore:
    def close_session(self, session_id, in_tokens, out_tokens, usage=None):
        assert session_id is not None
        return 0.0


class _UnusedClient:
    async def complete(self, _req):
        raise AssertionError("restart restore must not re-run research")


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _put_in_session(
    wl,
    *,
    bytes_sent: int = 0,
    chunks_received: int = 0,
) -> _FakeTurn:
    from jasper.voice_daemon import State

    turn = _FakeTurn(bytes_sent=bytes_sent, chunks_received=chunks_received)
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
    return turn


async def test_announce_research_ready_prompts_and_opens_confirmation_window():
    wl = _wake_loop()
    spoken: list[str] = []
    opened: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl._open_confirmation_window = _open
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == ["Your research is ready — want me to read it now?"]
    assert opened == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_announce_research_ready_failed_job_plays_failure_cue():
    wl = _wake_loop()
    cues: list[str] = []

    async def _play(slug: str) -> bool:
        cues.append(slug)
        return True

    scheduler = _MarkingScheduler()
    wl._play_cue = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(
        _job(status=FAILED, result=None, error="provider unavailable"),
    )

    assert cues == ["research_failed"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_announce_research_ready_does_not_mark_read_when_playback_fails():
    wl = _wake_loop()
    spoken: list[str] = []
    opened: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return False

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl._open_confirmation_window = _open
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == ["Your research is ready — want me to read it now?"]
    assert opened == []
    assert scheduler.announced == []
    assert scheduler.read == []


async def test_failed_research_cue_failure_does_not_mark_announced():
    wl = _wake_loop()
    cues: list[str] = []

    async def _play(slug: str) -> bool:
        cues.append(slug)
        return False

    scheduler = _MarkingScheduler()
    wl._play_cue = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(
        _job(status=FAILED, result=None, error="provider unavailable"),
    )

    assert cues == ["research_failed"]
    assert scheduler.announced == []
    assert scheduler.read == []
    assert wl._last_research_failure_announce_at is None


@pytest.mark.parametrize(
    "gate",
    ["mic_muted", "measurement_active", "spend_cap", "connection_paused"],
)
async def test_confirmation_guard_ladder_reads_immediately(gate: str):
    wl = _wake_loop()
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    if gate == "mic_muted":
        wl._mic_muted = True
    elif gate == "measurement_active":
        wl._measurement_active.set()
    elif gate == "spend_cap":
        wl._spend_cap = types.SimpleNamespace(allowed=lambda: False)
    elif gate == "connection_paused":
        wl._connection = types.SimpleNamespace(is_paused=lambda: True)

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == [
        "Your research is ready — want me to read it now?",
        "Use induction if you want fast response.",
    ]
    assert scheduler.read == ["job12345"]


async def test_confirmation_guard_session_active_holds_without_immediate_read():
    from jasper.voice_daemon import State

    wl = _wake_loop()
    wl._state = State.WAKE
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        wl._state = State.SESSION
        return True

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == ["Your research is ready — want me to read it now?"]
    assert [job.id for job in wl._pending_research] == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


def test_record_research_delivery_clears_stale_pending_job():
    wl = _wake_loop()
    job = _job()
    other = _job(id="other", result="Other result.")
    recorded: list[tuple[str | None, str | None]] = []
    wl._pending_research = [job, other]

    def _record(user_text, assistant_text, **_kwargs):
        recorded.append((user_text, assistant_text))

    wl._record_conversation_turn = _record

    wl.record_research_delivery(job, job.result, "yes")

    assert recorded == [("research cooktops", "Use induction if you want fast response.")]
    assert [pending.id for pending in wl._pending_research] == ["other"]


async def test_confirmation_silence_dismisses_without_model_commit(caplog):
    wl = _wake_loop()
    turn = _put_in_session(wl, bytes_sent=299_520)
    wl._user_speech_seen = False
    wl._input_ended = False
    job = _job()
    wl._research_window_active = True
    wl._research_window_job = job
    wl._research_window_decided = False
    wl._research_window_cancelled_by_wake = False
    scheduler = _MarkingScheduler()
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING, logger="jasper.voice_daemon"):
        await wl._end_turn_inner("no_speech")

    assert turn.end_input_calls == 0
    assert turn.release_calls == 1
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []
    assert wl._research_window_active is False
    assert "RECORDING TIMEOUT" not in caplog.text
    assert "SILENT RESPONSE" not in caplog.text


async def test_real_wake_during_confirmation_window_cancels_window_and_wins():
    wl = _wake_loop()
    turn = _put_in_session(wl)
    wl._user_speech_seen = False
    wl._input_ended = False
    job = _job()
    wl._research_window_active = True
    wl._research_window_job = job
    wl._research_window_decided = False
    wl._research_window_cancelled_by_wake = False
    wl._legs["on"].detector.score_frame = lambda _frame: 0.95
    wl._acquire_buffer = []
    acquired: list[dict] = []

    def _schedule(coro, *, name):
        acquired.append({"name": name, "coro": coro})
        coro.close()

    wl._create_fire_and_forget_task = _schedule

    await wl._handle_wake_frame(np.zeros(1280, dtype=np.int16), leg="on")

    assert turn.end_input_calls == 0
    assert turn.release_calls == 1
    assert wl._research_window_active is False
    assert wl._state.name == "WAKE"
    assert wl._acquiring is True
    assert [task["name"] for task in acquired] == ["wake-arbitrate-acquire-drain"]


async def test_real_wake_during_confirmation_opening_waits_then_wins():
    wl = _wake_loop()
    job = _job()
    wl._research_window_active = True
    wl._research_window_job = job
    wl._research_window_decided = False
    wl._research_window_cancelled_by_wake = False
    opening_done = asyncio.Event()
    wl._research_window_opening_done = opening_done
    wl._legs["on"].detector.score_frame = lambda _frame: 0.95
    acquired: list[dict] = []

    def _schedule(coro, *, name):
        acquired.append({"name": name, "coro": coro})
        coro.close()

    wl._create_fire_and_forget_task = _schedule

    task = asyncio.create_task(
        wl._handle_wake_frame(np.zeros(1280, dtype=np.int16), leg="on"),
    )
    await asyncio.sleep(0)

    assert wl._research_window_cancelled_by_wake is True
    assert acquired == []

    # Simulate the opener observing the cancellation, cleaning up the
    # confirmation turn, and releasing the normal wake path to continue.
    wl._research_window_active = False
    opening_done.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert wl._state.name == "WAKE"
    assert wl._acquiring is True
    assert [task["name"] for task in acquired] == ["wake-arbitrate-acquire-drain"]


async def test_confirmation_open_cancelled_after_begin_ends_turn_without_reading():
    wl = _wake_loop()
    job = _job()
    spoken: list[str] = []

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        _put_in_session(wl)
        wl._research_window_cancelled_by_wake = True

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl._begin_turn = _begin_turn
    wl._play_dynamic_text = _play

    await wl._open_confirmation_window(job)

    assert spoken == []
    assert wl._research_window_active is False
    assert wl._research_window_opening_done is None
    assert wl._state.name == "WAKE"


async def test_confirmation_open_cancelled_begin_failure_clears_without_reading():
    wl = _wake_loop()
    job = _job()
    spoken: list[str] = []

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        wl._research_window_cancelled_by_wake = True
        raise RuntimeError("turn already cancelled by wake")

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    wl._begin_turn = _begin_turn
    wl._play_dynamic_text = _play

    await wl._open_confirmation_window(job)

    assert spoken == []
    assert wl._research_window_active is False
    assert wl._research_window_job is None
    assert wl._research_window_opening_done is None
    assert wl._state.name == "WAKE"


async def test_confirmation_open_unexpected_begin_failure_resets_window_flags():
    wl = _wake_loop()
    job = _job()

    async def _begin_turn(*, pre_roll: bool, text_context: str | None) -> None:
        assert pre_roll is False
        assert text_context is not None
        raise AssertionError("unexpected begin failure")

    wl._begin_turn = _begin_turn

    with pytest.raises(AssertionError):
        await wl._open_confirmation_window(job)

    assert wl._research_window_active is False
    assert wl._research_window_job is None
    assert wl._research_window_decided is False
    assert wl._research_window_cancelled_by_wake is False
    assert wl._research_window_opening_done is None


async def test_research_done_during_session_is_held_then_drained_on_wake():
    from jasper.voice_daemon import State

    wl = _wake_loop()
    _put_in_session(wl)
    spoken: list[str] = []
    opened: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl._open_confirmation_window = _open
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == []
    assert [job.id for job in wl._pending_research] == ["job12345"]
    assert scheduler.announced == []

    await wl._end_turn_inner("test")

    assert wl._state is State.WAKE
    assert wl._pending_research == []
    assert spoken == ["Your research is ready — want me to read it now?"]
    assert opened == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_research_drain_never_speaks_while_session():
    from jasper.voice_daemon import State

    wl = _wake_loop()
    wl._state = State.SESSION
    wl._pending_research = [_job()]

    async def _play(_text: str) -> bool:
        raise AssertionError("research must not speak while SESSION")

    wl._play_dynamic_text = _play

    await wl._drain_pending_research()

    assert [job.id for job in wl._pending_research] == ["job12345"]


async def test_failed_research_cooldown_suppresses_burst_and_allows_later():
    wl = _wake_loop()
    cues: list[str] = []

    async def _play(slug: str) -> bool:
        cues.append(slug)
        return True

    scheduler = _MarkingScheduler()
    wl._play_cue = _play
    wl._research_failure_cooldown_sec = 10.0
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(
        _job(
            id="fail1",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )
    await wl.announce_research_ready(
        _job(
            id="fail2",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )

    assert cues == ["research_failed"]
    assert scheduler.announced == ["fail1", "fail2"]
    assert scheduler.read == []

    assert wl._last_research_failure_announce_at is not None
    wl._last_research_failure_announce_at -= 11.0
    await wl.announce_research_ready(
        _job(
            id="fail3",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )

    assert cues == ["research_failed", "research_failed"]
    assert scheduler.announced == ["fail1", "fail2", "fail3"]


async def test_research_announcements_do_not_overlap_during_drain():
    wl = _wake_loop()
    wl._pending_research = [_job(id="first", result="First.")]
    spoken: list[str] = []
    opened: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)
        if job.id == "first":
            first_started.set()
            await release_first.wait()

    wl._play_dynamic_text = _play
    wl._open_confirmation_window = _open

    drain_task = asyncio.create_task(wl._drain_pending_research())
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    announce_task = asyncio.create_task(
        wl.announce_research_ready(_job(id="second", result="Second.")),
    )
    await asyncio.sleep(0)

    assert spoken == ["Your research is ready — want me to read it now?"]
    assert opened == ["first"]

    release_first.set()
    await asyncio.wait_for(drain_task, timeout=1.0)
    await asyncio.wait_for(announce_task, timeout=1.0)

    assert spoken == [
        "Your research is ready — want me to read it now?",
        "Your research is ready — want me to read it now?",
    ]
    assert opened == ["first", "second"]


async def test_restart_restore_holds_unannounced_jobs_until_wake(tmp_path):
    from jasper.voice_daemon import State

    path = tmp_path / "research.db"
    store = ResearchJobStore(str(path))
    assert store.add(
        _job(id="done1", status=DONE, result="Ready.", created_at=1.0),
    )
    assert store.add(
        _job(id="run1", status=RUNNING, result=None, created_at=2.0),
    )
    store.close()

    wl = _wake_loop()
    wl._state = State.SESSION
    spoken: list[str] = []
    cues: list[str] = []
    opened: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _play_cue(slug: str) -> bool:
        cues.append(slug)
        return True

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    sched = ResearchScheduler(_UnusedClient(), db_path=str(path))
    wl._play_dynamic_text = _play
    wl._play_cue = _play_cue
    wl._open_confirmation_window = _open
    wl.set_research_scheduler(sched)
    sched.set_on_done(wl.announce_research_ready)

    await sched.start()
    await _wait_for(lambda: len(wl._pending_research) == 2)

    assert spoken == []
    assert [job.id for job in wl._pending_research] == ["done1", "run1"]

    wl._state = State.WAKE
    await wl._drain_pending_research()

    assert spoken == ["Your research is ready — want me to read it now?"]
    assert opened == ["done1"]
    assert cues == ["research_failed"]
    rows = {job.id: job for job in ResearchJobStore(str(path)).all()}
    assert rows["done1"].announced is True
    assert rows["done1"].read is False
    assert rows["run1"].status == FAILED
    assert rows["run1"].announced is True
    assert rows["run1"].read is False
    await sched.stop()


async def test_pending_research_queue_is_bounded_and_coalesces():
    from jasper.voice_daemon import State

    wl = _wake_loop()
    wl._state = State.SESSION
    wl._research_pending_cap = 3

    await wl.announce_research_ready(_job(id="same", result="old"))
    await wl.announce_research_ready(_job(id="same", result="new"))
    await wl.announce_research_ready(_job(id="two", result="2"))
    await wl.announce_research_ready(_job(id="three", result="3"))
    await wl.announce_research_ready(_job(id="four", result="4"))

    assert [(job.id, job.result) for job in wl._pending_research] == [
        ("two", "2"),
        ("three", "3"),
        ("four", "4"),
    ]


def test_system_instruction_includes_research_nudge_when_unconfigured():
    from jasper.voice_daemon import _build_system_instruction

    prompt = _build_system_instruction(
        location="",
        research_configured=False,
        hostname="jts2.local",
    )

    assert "jts2.local/voice" in prompt
    assert "If the user asks you to research" in prompt
    assert "Research isn't set up yet" in prompt


def test_system_instruction_omits_research_nudge_when_configured():
    from jasper.voice_daemon import _build_system_instruction

    prompt = _build_system_instruction(location="", research_configured=True)

    assert "Research isn't set up yet" not in prompt


def test_research_failed_cue_is_registered_provider_agnostic():
    from jasper.cues.registry import find

    cue = find("research_failed")

    assert cue is not None
    text = cue.template.lower()
    assert "research" in text
    assert "openai" not in text
    assert "gemini" not in text
    assert "grok" not in text
