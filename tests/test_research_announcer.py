# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from jasper.research import (
    DONE,
    FAILED,
    RUNNING,
    ResearchJob,
    ResearchJobStore,
    ResearchScheduler,
)
from jasper.voice.research_announcer import ResearchAnnouncer
from tests._async_wait import wait_until as _wait_for
from tests._log_events import event_fields
from tests._turn_host_fake import FakeTurnHost

READY_PROMPT = "Your research is ready — want me to read it now?"


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


class _UnusedClient:
    async def complete(self, _req):
        raise AssertionError("restart restore must not re-run research")


def _pending_ids(announcer: ResearchAnnouncer) -> list[str]:
    return [job.id for job in announcer._pending]


async def test_announce_research_ready_prompts_and_opens_confirmation_window():
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host)
    opened: list[str] = []

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    announcer.open_confirmation_window = _open  # type: ignore[method-assign]
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == [READY_PROMPT]
    assert opened == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_announce_research_ready_failed_job_plays_failure_cue():
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(
        _job(status=FAILED, result=None, error="provider unavailable"),
    )

    assert host.cues == ["research_failed"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_announce_research_ready_does_not_mark_read_when_playback_fails():
    host = FakeTurnHost()
    host.play_result = False
    announcer = ResearchAnnouncer(host=host)
    opened: list[str] = []

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    announcer.open_confirmation_window = _open  # type: ignore[method-assign]
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == [READY_PROMPT]
    assert opened == []
    assert scheduler.announced == []
    assert scheduler.read == []


async def test_failed_research_cue_failure_does_not_mark_announced():
    host = FakeTurnHost()
    host.cue_result = False
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(
        _job(status=FAILED, result=None, error="provider unavailable"),
    )

    assert host.cues == ["research_failed"]
    assert scheduler.announced == []
    assert scheduler.read == []
    assert announcer._last_failure_announce_at is None


@pytest.mark.parametrize(
    "gate",
    ["mic_muted", "spend_cap", "connection_paused"],
)
async def test_confirmation_guard_ladder_reads_immediately(gate: str):
    # measurement_active is NOT in this ladder (issue #1786): unlike these
    # three "can't listen for a reply, but safe to speak" gates, an open
    # measurement window means "don't emit ANY audio" — see
    # test_confirmation_guard_measurement_active_holds_without_immediate_read
    # below, which pins the opposite (queued, not read immediately).
    host = FakeTurnHost()
    if gate == "mic_muted":
        host.mic_muted = True
    elif gate == "spend_cap":
        host.spend_allowed = False
    elif gate == "connection_paused":
        host.connection_paused = True
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == [
        READY_PROMPT,
        "Use induction if you want fast response.",
    ]
    assert scheduler.read == ["job12345"]


async def test_confirmation_guard_session_active_holds_without_immediate_read():
    host = FakeTurnHost()

    def _open_session(_text: str) -> None:
        host.in_session = True
        host.in_wake = False

    host.on_play = _open_session
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == [READY_PROMPT]
    assert _pending_ids(announcer) == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_confirmation_guard_measurement_active_holds_without_immediate_read():
    """issue #1786: a measurement window opening between the "ready?"
    prompt and the confirmation-window attempt must queue the job, not
    read the research result aloud into the live sweep.

    Before the fix, `open_confirmation_window` treated
    `_confirmation_guard_reason() == "measurement_active"` the same as
    mic_muted/spend_cap/connection_paused (safe to speak) instead of like
    session_active (must not speak) — this test pins the corrected
    dispatch by flipping the flag as a side effect of the first play,
    exactly as the sibling session_active test does above.
    """
    host = FakeTurnHost()

    def _open_measurement(_text: str) -> None:
        host.measurement_active = True

    host.on_play = _open_measurement
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == [READY_PROMPT]
    assert _pending_ids(announcer) == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_announce_research_ready_measurement_active_queues_without_speaking(
    caplog,
):
    """issue #1786: measurement already active when the job finishes ⇒
    nothing is spoken at all (not even the "ready?" prompt), the job is
    queued for the post-window drain, and a structured event line names
    the suppressed job and why."""
    host = FakeTurnHost(measurement_active=True)
    announcer = ResearchAnnouncer(host=host)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="jasper.voice_daemon"):
        await announcer.announce_ready(_job())

    assert host.spoken == []
    assert _pending_ids(announcer) == ["job12345"]
    assert scheduler.announced == []
    fields = event_fields(caplog, "research.announce_suppressed")
    assert fields["reason"] == "measurement_active"
    assert fields["job_id"] == "job12345"


async def test_research_drain_never_speaks_while_measurement_active():
    """issue #1786: `drain` (distinct from `announce_ready` — this is the
    path the turn teardown uses to flush jobs queued while busy) must also
    honor the flag, mirroring test_research_drain_never_speaks_while_session
    further below.

    Correction: this test arms the flag BEFORE calling `drain`, so it only
    exercises the two top-level guards (pre-lock and post-lock) — either
    one alone already keeps THIS test from hanging, so it does not by
    itself pin the per-iteration guard inside the for-loop. That guard is
    the one whose regression actually wedges the daemon (a mid-batch
    busy-spin with no sleep, for as long as the window stays open) — see
    test_research_drain_mid_batch_measurement_active_returns_without_hang
    below, which arms the flag mid-batch instead of before entry."""
    host = FakeTurnHost(measurement_active=True)

    def _must_not_speak(_text: str) -> None:
        raise AssertionError(
            "research must not speak during a measurement window",
        )

    host.on_play = _must_not_speak
    announcer = ResearchAnnouncer(host=host)
    announcer._pending = [_job()]

    await announcer.drain()

    assert _pending_ids(announcer) == ["job12345"]


async def test_research_drain_mid_batch_measurement_active_returns_without_hang():
    """issue #1786 (C-1 follow-up): the per-iteration guard inside
    `drain`'s for-loop — not just the two top-level guards pinned above —
    is what stops a mid-batch busy-spin. This test arms the flag as a side
    effect of speaking the FIRST job in a two-job batch, so the SECOND job
    is still sitting in `batch` when the per-iteration check runs: the only
    shape that actually exercises that guard.

    A regression here (removing the per-iteration check, or its
    `measurement_active` clause specifically) does not raise on its own —
    it spins `drain`'s `while` loop forever with no sleep, because
    `_speak`'s own measurement guard re-queues the job it was just handed
    via `_queue_pending`, and the `while` loop reads that refill as "more
    work" and retries immediately.

    Verified empirically while writing this test: wrapping the call in
    `asyncio.wait_for(..., timeout=...)` (the repo's usual bounded-wait
    convention, tests/test_async_wait_contract.py) does NOT catch this
    shape. That spin never awaits anything that actually suspends —
    `log_event` and `_queue_pending` are both plain sync calls — so the
    event loop never gets scheduled back to check `wait_for`'s own
    timeout; only pytest-timeout's OS-signal alarm can preempt a fully
    synchronous busy loop like this one, and only after the full 300s
    backstop. So instead this test counts re-queues directly:
    `_queue_pending` is wrapped to raise a plain, synchronous
    AssertionError once a job has clearly been handed back with no
    progress, which propagates immediately with no dependency on event-
    loop scheduling. `asyncio.wait_for` is kept around it anyway as a
    second, complementary net for a *different* regression shape (one
    that hangs on a genuine unresolved await instead of busy-spinning).
    """
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host)
    announcer._pending = [
        _job(id="first", result="First."),
        _job(id="second", result="Second."),
    ]
    requeue_count = 0
    real_queue_pending = announcer._queue_pending

    def _counting_queue_pending(job: ResearchJob) -> None:
        nonlocal requeue_count
        requeue_count += 1
        if requeue_count > 3:
            raise AssertionError(
                f"{job.id!r} was re-queued {requeue_count} times with no "
                "progress — the per-iteration measurement guard in "
                "drain likely regressed and is busy-spinning instead of "
                "returning"
            )
        real_queue_pending(job)

    def _open_measurement(_text: str) -> None:
        # Simulate a measurement window opening the instant the first
        # job's "ready?" prompt finishes, while job "second" is still
        # queued in the batch `drain` is iterating.
        host.measurement_active = True

    async def _open(_job: ResearchJob) -> None:
        return None

    host.on_play = _open_measurement
    announcer.open_confirmation_window = _open  # type: ignore[method-assign]
    announcer._queue_pending = _counting_queue_pending  # type: ignore[method-assign]

    await asyncio.wait_for(announcer.drain(), timeout=5.0)

    assert host.spoken == [READY_PROMPT]
    assert _pending_ids(announcer) == ["second"]
    # The correct per-iteration guard puts "second" back via direct list
    # concatenation (`batch[idx:] + self._pending`), never through
    # `_queue_pending` — so a healthy run never even reaches the counting
    # wrapper.
    assert requeue_count == 0


def test_record_research_delivery_clears_stale_pending_job():
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host)
    job = _job()
    other = _job(id="other", result="Other result.")
    announcer._pending = [job, other]

    announcer.record_delivery(job, job.result, "yes")

    assert host.conversation_turns == [
        (
            "research cooktops",
            "Use induction if you want fast response.",
            {"kind": "research", "job_id": "job12345"},
        ),
    ]
    assert _pending_ids(announcer) == ["other"]


async def test_research_done_during_session_is_held_then_drained_on_wake():
    host = FakeTurnHost(in_session=True, in_wake=False)
    announcer = ResearchAnnouncer(host=host)
    opened: list[str] = []

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    scheduler = _MarkingScheduler()
    announcer.open_confirmation_window = _open  # type: ignore[method-assign]
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(_job())

    assert host.spoken == []
    assert _pending_ids(announcer) == ["job12345"]
    assert scheduler.announced == []

    host.in_session = False
    host.in_wake = True
    await announcer.drain()

    assert announcer._pending == []
    assert host.spoken == [READY_PROMPT]
    assert opened == ["job12345"]
    assert scheduler.announced == ["job12345"]
    assert scheduler.read == []


async def test_research_drain_never_speaks_while_session():
    host = FakeTurnHost(in_session=True, in_wake=False)

    def _must_not_speak(_text: str) -> None:
        raise AssertionError("research must not speak while SESSION")

    host.on_play = _must_not_speak
    announcer = ResearchAnnouncer(host=host)
    announcer._pending = [_job()]

    await announcer.drain()

    assert _pending_ids(announcer) == ["job12345"]


async def test_failed_research_cooldown_suppresses_burst_and_allows_later():
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host, failure_cooldown_sec=10.0)
    scheduler = _MarkingScheduler()
    announcer.set_scheduler(scheduler)  # type: ignore[arg-type]

    await announcer.announce_ready(
        _job(
            id="fail1",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )
    await announcer.announce_ready(
        _job(
            id="fail2",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )

    assert host.cues == ["research_failed"]
    assert scheduler.announced == ["fail1", "fail2"]
    assert scheduler.read == []

    assert announcer._last_failure_announce_at is not None
    announcer._last_failure_announce_at -= 11.0
    await announcer.announce_ready(
        _job(
            id="fail3",
            status=FAILED,
            result=None,
            error="provider unavailable",
        ),
    )

    assert host.cues == ["research_failed", "research_failed"]
    assert scheduler.announced == ["fail1", "fail2", "fail3"]


async def test_research_announcements_do_not_overlap_during_drain():
    host = FakeTurnHost()
    announcer = ResearchAnnouncer(host=host)
    announcer._pending = [_job(id="first", result="First.")]
    opened: list[str] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)
        if job.id == "first":
            first_started.set()
            await release_first.wait()

    announcer.open_confirmation_window = _open  # type: ignore[method-assign]

    drain_task = asyncio.create_task(announcer.drain())
    await asyncio.wait_for(first_started.wait(), timeout=1.0)

    announce_task = asyncio.create_task(
        announcer.announce_ready(_job(id="second", result="Second.")),
    )
    await asyncio.sleep(0)

    assert host.spoken == [READY_PROMPT]
    assert opened == ["first"]

    release_first.set()
    await asyncio.wait_for(drain_task, timeout=1.0)
    await asyncio.wait_for(announce_task, timeout=1.0)

    assert host.spoken == [READY_PROMPT, READY_PROMPT]
    assert opened == ["first", "second"]


async def test_restart_restore_holds_unannounced_jobs_until_wake(tmp_path):
    path = tmp_path / "research.db"
    store = ResearchJobStore(str(path))
    assert store.add(
        _job(id="done1", status=DONE, result="Ready.", created_at=1.0),
    )
    assert store.add(
        _job(id="run1", status=RUNNING, result=None, created_at=2.0),
    )
    store.close()

    host = FakeTurnHost(in_session=True, in_wake=False)
    announcer = ResearchAnnouncer(host=host)
    opened: list[str] = []

    async def _open(job: ResearchJob) -> None:
        opened.append(job.id)

    sched = ResearchScheduler(_UnusedClient(), db_path=str(path))
    announcer.open_confirmation_window = _open  # type: ignore[method-assign]
    announcer.set_scheduler(sched)
    sched.set_on_done(announcer.announce_ready)

    await sched.start()
    await _wait_for(lambda: len(announcer._pending) == 2)

    assert host.spoken == []
    assert _pending_ids(announcer) == ["done1", "run1"]

    host.in_session = False
    host.in_wake = True
    await announcer.drain()

    assert host.spoken == [READY_PROMPT]
    assert opened == ["done1"]
    assert host.cues == ["research_failed"]
    rows = {job.id: job for job in ResearchJobStore(str(path)).all()}
    assert rows["done1"].announced is True
    assert rows["done1"].read is False
    assert rows["run1"].status == FAILED
    assert rows["run1"].announced is True
    assert rows["run1"].read is False
    await sched.stop()


async def test_pending_queue_is_bounded_and_coalesces():
    host = FakeTurnHost(in_session=True, in_wake=False)
    announcer = ResearchAnnouncer(host=host, pending_cap=3)

    await announcer.announce_ready(_job(id="same", result="old"))
    await announcer.announce_ready(_job(id="same", result="new"))
    await announcer.announce_ready(_job(id="two", result="2"))
    await announcer.announce_ready(_job(id="three", result="3"))
    await announcer.announce_ready(_job(id="four", result="4"))

    assert [(job.id, job.result) for job in announcer._pending] == [
        ("two", "2"),
        ("three", "3"),
        ("four", "4"),
    ]
