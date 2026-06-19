from __future__ import annotations

import sys
import time
import types

from jasper.research import DONE, FAILED, ResearchJob


if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = types.ModuleType("sounddevice")


def _wake_loop():
    from jasper.voice_daemon import State, WakeLoop

    wl = WakeLoop.for_tests()
    wl._state = State.WAKE
    return wl


def _job(
    *,
    status=DONE,
    result: str | None = "Use induction if you want fast response.",
    error: str | None = None,
) -> ResearchJob:
    now = time.time()
    return ResearchJob(
        id="job12345",
        query="research cooktops",
        status=status,
        result=result,
        error=error,
        created_at=now,
        finished_at=now,
        announced=False,
        read=False,
    )


class _MarkingScheduler:
    def __init__(self) -> None:
        self.announced: list[str] = []
        self.read: list[str] = []

    def mark_announced(self, job_id: str) -> None:
        self.announced.append(job_id)

    def mark_read(self, job_id: str) -> None:
        self.read.append(job_id)


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


async def test_announce_research_ready_failed_job_speaks_one_failure_line():
    wl = _wake_loop()
    spoken: list[str] = []

    async def _play(text: str) -> bool:
        spoken.append(text)
        return True

    scheduler = _MarkingScheduler()
    wl._play_dynamic_text = _play
    wl.set_research_scheduler(scheduler)  # type: ignore[arg-type]

    await wl.announce_research_ready(
        _job(status=FAILED, result=None, error="provider unavailable"),
    )

    assert spoken == [
        "Sorry, I couldn't finish that research. Please ask me again.",
    ]
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
