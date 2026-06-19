from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from jasper.research import (
    DONE,
    FAILED,
    RUNNING,
    ResearchJob,
    ResearchJobStore,
    ResearchResult,
    ResearchScheduler,
)


def _tmp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def _job(
    job_id: str,
    *,
    query: str = "query",
    status=RUNNING,
    result: str | None = None,
    error: str | None = None,
    announced: bool = False,
    read: bool = False,
) -> ResearchJob:
    now = time.time()
    return ResearchJob(
        id=job_id,
        query=query,
        status=status,
        result=result,
        error=error,
        created_at=now,
        finished_at=now if status != RUNNING else None,
        announced=announced,
        read=read,
    )


def test_store_crud_round_trip_and_flags():
    path = _tmp_db_path()
    try:
        store = ResearchJobStore(path)
        job = _job("abc12345", query="find ranges")

        assert store.add(job) is True
        assert store.get("abc12345") == job

        done = store.mark_done("abc12345", "Answer", finished_at=123.0)
        assert done is not None
        assert done.status == DONE
        assert done.result == "Answer"
        assert done.finished_at == 123.0

        announced = store.mark_announced("abc12345")
        assert announced is not None
        assert announced.announced is True

        read = store.mark_read("abc12345")
        assert read is not None
        assert read.read is True

        rows = store.all()
        assert len(rows) == 1
        assert rows[0].id == "abc12345"
        assert rows[0].status == DONE
        assert rows[0].announced is True
        assert rows[0].read is True
        store.close()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_store_fail_soft_when_sqlite_unavailable(tmp_path):
    bad_path = tmp_path / "not-a-db-dir"
    bad_path.mkdir()
    store = ResearchJobStore(str(bad_path))

    assert store.available is False
    assert store.add(_job("x")) is False
    assert store.get("x") is None
    assert store.all() == []
    assert store.mark_done("x", "done") is None
    assert store.mark_failed("x", "failed") is None
    store.close()


class BlockingClient:
    def __init__(self) -> None:
        self.started = 0
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def complete(self, _req):
        self.started += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return ResearchResult(text="done")


@pytest.mark.asyncio
async def test_concurrency_cap_rejects_cap_plus_one_with_speakable_busy_result():
    path = _tmp_db_path()
    client = BlockingClient()
    try:
        sched = ResearchScheduler(client, db_path=path, concurrency=2)

        first = sched.submit("one")
        second = sched.submit("two")
        third = sched.submit("three")

        assert first.accepted is True
        assert second.accepted is True
        assert third.accepted is False
        assert third.job is None
        assert "Try again in a moment" in third.message
        assert len(sched.list_jobs()) == 2

        await _wait_for(lambda: client.started == 2)
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_runtime_ceiling_marks_job_failed_and_fires_on_done():
    path = _tmp_db_path()
    done_jobs: list[ResearchJob] = []

    class SlowClient:
        async def complete(self, _req):
            await asyncio.sleep(10)
            return ResearchResult(text="too late")

    async def on_done(job: ResearchJob) -> None:
        done_jobs.append(job)

    try:
        sched = ResearchScheduler(
            SlowClient(),
            on_done=on_done,
            db_path=path,
            max_runtime_sec=0.05,
        )
        accepted = sched.submit("slow question")
        assert accepted.accepted is True
        assert accepted.job is not None

        await _wait_for(lambda: bool(done_jobs))

        failed = done_jobs[0]
        assert failed.id == accepted.job.id
        assert failed.status == FAILED
        assert failed.error is not None
        assert "timed out" in failed.error

        row = ResearchJobStore(path).get(failed.id)
        assert row is not None
        assert row.status == FAILED
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_restart_restore_resurfaces_done_unannounced_and_marks_running_failed():
    path = _tmp_db_path()
    surfaced: list[ResearchJob] = []

    async def on_done(job: ResearchJob) -> None:
        surfaced.append(job)

    try:
        store = ResearchJobStore(path)
        assert store.add(_job("done1", status=DONE, result="Ready", announced=False))
        assert store.add(_job("done2", status=DONE, result="Read", announced=True))
        assert store.add(_job("run1", status=RUNNING))
        store.close()

        sched = ResearchScheduler(
            BlockingClient(),
            on_done=on_done,
            db_path=path,
        )
        await sched.start()

        await _wait_for(lambda: [job.id for job in surfaced] == ["done1"])

        rows = {job.id: job for job in ResearchJobStore(path).all()}
        assert rows["done1"].status == DONE
        assert rows["done1"].announced is False
        assert rows["done2"].status == DONE
        assert rows["run1"].status == FAILED
        assert "interrupted by a restart" in (rows["run1"].error or "")
        await sched.stop()
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_without_calling_on_done():
    path = _tmp_db_path()
    done_jobs: list[ResearchJob] = []
    client = BlockingClient()

    async def on_done(job: ResearchJob) -> None:
        done_jobs.append(job)

    try:
        sched = ResearchScheduler(client, on_done=on_done, db_path=path)
        accepted = sched.submit("cancel me")
        assert accepted.accepted is True
        assert accepted.job is not None

        await _wait_for(lambda: client.started == 1)
        await sched.stop()

        assert client.cancelled.is_set()
        assert done_jobs == []
        row = ResearchJobStore(path).get(accepted.job.id)
        assert row is not None
        assert row.status == RUNNING
    finally:
        if os.path.exists(path):
            os.unlink(path)
