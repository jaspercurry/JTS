from __future__ import annotations

import asyncio
import sys
import time
import types

from jasper.research import (
    DONE,
    FAILED,
    RUNNING,
    ResearchJob,
    ResearchJobStore,
    ResearchScheduler,
)


if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")


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


def _put_in_session(wl) -> None:
    from jasper.voice_daemon import State

    wl._state = State.SESSION
    wl._turn = _FakeTurn()
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


async def test_announce_research_ready_reads_done_result_and_marks_announced():
    wl = _wake_loop()
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == [
        "Hey, your research is ready. Use induction if you want fast response.",
    ]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == ["job12345"]


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

    async def _play(text: str) -> bool:
        spoken.append(text)
        return False

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == [
        "Hey, your research is ready. Use induction if you want fast response.",
    ]
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


async def test_research_done_during_session_is_held_then_drained_on_wake():
    from jasper.voice_daemon import State

    wl = _wake_loop()
    _put_in_session(wl)
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(_job())

    assert spoken == []
    assert [job.id for job in wl._pending_research] == ["job12345"]
    assert scheduler.announced == []

    await wl._end_turn_inner("test")

    assert wl._state is State.WAKE
    assert wl._pending_research == []
    assert spoken == [
        "Hey, your research is ready. Use induction if you want fast response.",
    ]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == ["job12345"]


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
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _play(text: str) -> bool:
        spoken.append(text)
        if text.endswith("First."):
            first_started.set()
            await release_first.wait()
        return True

    wl._play_dynamic_text = _play

    drain_task = asyncio.create_task(wl._drain_pending_research())
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    announce_task = asyncio.create_task(
        wl.announce_research_ready(_job(id="second", result="Second.")),
    )
    await asyncio.sleep(0)

    assert spoken == ["Hey, your research is ready. First."]

    release_first.set()
    await asyncio.wait_for(drain_task, timeout=1.0)
    await asyncio.wait_for(announce_task, timeout=1.0)

    assert spoken == [
        "Hey, your research is ready. First.",
        "Hey, your research is ready. Second.",
    ]


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

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    async def _play_cue(slug: str) -> bool:
        cues.append(slug)
        return True

    sched = ResearchScheduler(_UnusedClient(), db_path=str(path))
    wl._play_dynamic_text = _play
    wl._play_cue = _play_cue
    wl.set_research_scheduler(sched)
    sched.set_on_done(wl.announce_research_ready)

    await sched.start()
    await _wait_for(lambda: len(wl._pending_research) == 2)

    assert spoken == []
    assert [job.id for job in wl._pending_research] == ["done1", "run1"]

    wl._state = State.WAKE
    await wl._drain_pending_research()

    assert spoken == ["Hey, your research is ready. Ready."]
    assert cues == ["research_failed"]
    rows = {job.id: job for job in ResearchJobStore(str(path)).all()}
    assert rows["done1"].announced is True
    assert rows["done1"].read is True
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
