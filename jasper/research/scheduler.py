"""Async research job scheduler and SQLite persistence.

Hardware-free Phase 1 foundation only: no voice tool registration, no
daemon wiring, and no audio/cue behavior. The scheduler owns bounded
background tasks and calls an injected ``on_done`` hook when a job
reaches a terminal state.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from .base import ResearchError, ResearchRequest, TextLLMClient

logger = logging.getLogger(__name__)


DEFAULT_DB_PATH = "/var/lib/jasper/research_jobs.db"
DEFAULT_MAX_RUNTIME_SEC = 300.0
DEFAULT_CONCURRENCY = 2

ResearchStatus = Literal["running", "done", "failed"]
RUNNING: ResearchStatus = "running"
DONE: ResearchStatus = "done"
FAILED: ResearchStatus = "failed"


@dataclass(frozen=True)
class ResearchJob:
    id: str
    query: str
    status: ResearchStatus
    result: str | None
    error: str | None
    created_at: float
    finished_at: float | None
    announced: bool
    read: bool


@dataclass(frozen=True)
class ResearchStartResult:
    accepted: bool
    job: ResearchJob | None
    message: str


class ResearchJobStore:
    """Fail-soft SQLite persistence for research jobs."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        conn: sqlite3.Connection | None = None
        try:
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS research_jobs ("
                "  id TEXT PRIMARY KEY,"
                "  query TEXT NOT NULL,"
                "  status TEXT NOT NULL,"
                "  result TEXT,"
                "  error TEXT,"
                "  created_at REAL NOT NULL,"
                "  finished_at REAL,"
                "  announced INTEGER NOT NULL DEFAULT 0,"
                "  read INTEGER NOT NULL DEFAULT 0"
                ")"
            )
        except (OSError, sqlite3.Error) as e:
            logger.warning("research store unavailable (%s): %s", db_path, e)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
        else:
            self._conn = conn

    @property
    def available(self) -> bool:
        return self._conn is not None

    def add(self, job: ResearchJob) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.execute(
                "INSERT INTO research_jobs (id, query, status, result, error, "
                "created_at, finished_at, announced, read) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _row_values(job),
            )
            return True
        except sqlite3.Error as e:
            logger.warning("research store add failed (id=%s): %s", job.id, e)
            return False

    def update(self, job: ResearchJob) -> bool:
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.execute(
                "UPDATE research_jobs SET query = ?, status = ?, result = ?, "
                "error = ?, created_at = ?, finished_at = ?, announced = ?, "
                "read = ? WHERE id = ?",
                (
                    job.query,
                    job.status,
                    job.result,
                    job.error,
                    job.created_at,
                    job.finished_at,
                    int(job.announced),
                    int(job.read),
                    job.id,
                ),
            )
            return True
        except sqlite3.Error as e:
            logger.warning("research store update failed (id=%s): %s", job.id, e)
            return False

    def get(self, job_id: str) -> ResearchJob | None:
        conn = self._conn
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT id, query, status, result, error, created_at, "
                "finished_at, announced, read FROM research_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning("research store get failed (id=%s): %s", job_id, e)
            return None
        return _job_from_row(row) if row is not None else None

    def all(self) -> list[ResearchJob]:
        conn = self._conn
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT id, query, status, result, error, created_at, "
                "finished_at, announced, read FROM research_jobs "
                "ORDER BY created_at"
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("research store all failed: %s", e)
            return []
        return [_job_from_row(row) for row in rows]

    def mark_done(self, job_id: str, result: str, *, finished_at: float | None = None) -> ResearchJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        updated = ResearchJob(
            id=job.id,
            query=job.query,
            status=DONE,
            result=result,
            error=None,
            created_at=job.created_at,
            finished_at=finished_at if finished_at is not None else time.time(),
            announced=job.announced,
            read=job.read,
        )
        self.update(updated)
        return updated

    def mark_failed(self, job_id: str, error: str, *, finished_at: float | None = None) -> ResearchJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        updated = ResearchJob(
            id=job.id,
            query=job.query,
            status=FAILED,
            result=job.result,
            error=error,
            created_at=job.created_at,
            finished_at=finished_at if finished_at is not None else time.time(),
            announced=job.announced,
            read=job.read,
        )
        self.update(updated)
        return updated

    def mark_announced(self, job_id: str) -> ResearchJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        updated = ResearchJob(
            id=job.id,
            query=job.query,
            status=job.status,
            result=job.result,
            error=job.error,
            created_at=job.created_at,
            finished_at=job.finished_at,
            announced=True,
            read=job.read,
        )
        self.update(updated)
        return updated

    def mark_read(self, job_id: str) -> ResearchJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        updated = ResearchJob(
            id=job.id,
            query=job.query,
            status=job.status,
            result=job.result,
            error=job.error,
            created_at=job.created_at,
            finished_at=job.finished_at,
            announced=job.announced,
            read=True,
        )
        self.update(updated)
        return updated

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass


class ResearchScheduler:
    """Owns one bounded asyncio task per in-flight research job."""

    def __init__(
        self,
        client: TextLLMClient,
        on_done: Callable[[ResearchJob], Awaitable[None] | None] | None = None,
        *,
        store: ResearchJobStore | None = None,
        db_path: str = DEFAULT_DB_PATH,
        max_runtime_sec: float = DEFAULT_MAX_RUNTIME_SEC,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._client = client
        self._on_done = on_done
        self._store = store if store is not None else ResearchJobStore(db_path)
        self._max_runtime_sec = float(max_runtime_sec)
        self._concurrency = max(1, int(concurrency))
        self._sem = asyncio.Semaphore(self._concurrency)
        self._jobs: dict[str, ResearchJob] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._started = False

    async def start(self) -> None:
        """Restore persisted terminal work without replaying running jobs."""
        if self._started:
            return
        self._started = True
        for job in self._store.all():
            if job.status == RUNNING:
                failed = self._store.mark_failed(
                    job.id,
                    "Research was interrupted by a restart. Ask me again.",
                )
                if failed is not None:
                    self._jobs[failed.id] = failed
                continue
            if job.status == DONE and not job.announced:
                self._jobs[job.id] = job
                self._tasks[job.id] = asyncio.create_task(
                    self._resurface(job),
                    name=f"research-resurface-{job.id}",
                )

    async def stop(self) -> None:
        """Cancel all in-flight jobs. Running rows remain for restart restore."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._started = False

    def submit(self, query: str) -> ResearchStartResult:
        query = (query or "").strip()
        if not query:
            return ResearchStartResult(
                accepted=False,
                job=None,
                message="I need a research question first.",
            )
        if self._running_task_count() >= self._concurrency:
            return ResearchStartResult(
                accepted=False,
                job=None,
                message="I'm still working on other research. Try again in a moment.",
            )
        now = time.time()
        job = ResearchJob(
            id=uuid.uuid4().hex[:8],
            query=query,
            status=RUNNING,
            result=None,
            error=None,
            created_at=now,
            finished_at=None,
            announced=False,
            read=False,
        )
        self._store.add(job)
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(
            self._run(job),
            name=f"research-{job.id}",
        )
        return ResearchStartResult(
            accepted=True,
            job=job,
            message="On it. I'll let you know when the research is ready.",
        )

    def list_jobs(self) -> list[ResearchJob]:
        return sorted(self._jobs.values(), key=lambda job: job.created_at)

    def get(self, job_id: str) -> ResearchJob | None:
        return self._jobs.get(job_id) or self._store.get(job_id)

    def _running_task_count(self) -> int:
        return sum(
            1
            for job_id in self._tasks
            if (job := self._jobs.get(job_id)) is not None and job.status == RUNNING
        )

    async def _resurface(self, job: ResearchJob) -> None:
        try:
            await self._notify_done(job)
        except asyncio.CancelledError:
            raise
        finally:
            self._tasks.pop(job.id, None)

    async def _run(self, job: ResearchJob) -> None:
        try:
            async with self._sem:
                result = await asyncio.wait_for(
                    self._client.complete(ResearchRequest(query=job.query)),
                    timeout=self._max_runtime_sec,
                )
        except asyncio.CancelledError:
            return
        except TimeoutError:
            done = self._finish_failed(
                job,
                f"Research timed out after {self._max_runtime_sec:g} seconds.",
            )
        except ResearchError as e:
            done = self._finish_failed(job, str(e))
        except Exception as e:  # noqa: BLE001
            done = self._finish_failed(job, str(e) or type(e).__name__)
        else:
            done = self._finish_done(job, result.text)
        finally:
            self._tasks.pop(job.id, None)

        await self._notify_done(done)

    def _finish_done(self, job: ResearchJob, result: str) -> ResearchJob:
        done = ResearchJob(
            id=job.id,
            query=job.query,
            status=DONE,
            result=result,
            error=None,
            created_at=job.created_at,
            finished_at=time.time(),
            announced=job.announced,
            read=job.read,
        )
        self._jobs[done.id] = done
        self._store.update(done)
        return done

    def _finish_failed(self, job: ResearchJob, error: str) -> ResearchJob:
        done = ResearchJob(
            id=job.id,
            query=job.query,
            status=FAILED,
            result=None,
            error=error,
            created_at=job.created_at,
            finished_at=time.time(),
            announced=job.announced,
            read=job.read,
        )
        self._jobs[done.id] = done
        self._store.update(done)
        return done

    async def _notify_done(self, job: ResearchJob) -> None:
        if self._on_done is None:
            return
        try:
            maybe_awaitable = self._on_done(job)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("research on_done failed (id=%s): %s", job.id, e)


def _row_values(job: ResearchJob) -> tuple:
    return (
        job.id,
        job.query,
        job.status,
        job.result,
        job.error,
        job.created_at,
        job.finished_at,
        int(job.announced),
        int(job.read),
    )


def _job_from_row(row: tuple) -> ResearchJob:
    return ResearchJob(
        id=row[0],
        query=row[1],
        status=row[2],
        result=row[3],
        error=row[4],
        created_at=row[5],
        finished_at=row[6],
        announced=bool(row[7]),
        read=bool(row[8]),
    )
