"""Timer scheduler with SQLite persistence.

Timers survive daemon restart — on startup, future timers are restored
to in-memory asyncio tasks; timers whose `fire_at` was during downtime
are dropped (logged) so the speaker doesn't replay
"your 6 a.m. alarm" at 6:05 after a restart.

Concurrency model: pure asyncio. One asyncio.Task per active timer
(asyncio.sleep(remaining) + on-fire callback). Cancellation is
task.cancel(). The store is touched on every CRUD operation
synchronously since SQLite calls are sub-millisecond locally.

Announcement is delegated to an `on_fire` callback so this module
stays free of cue / TTS / duck / wake-state dependencies — easier
to unit-test, and the daemon can layer in session-aware deferral
(don't speak over an active voice turn) on its side without bleeding
that into the scheduler.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# Default timer DB path — sits in the systemd StateDirectory used by
# jasper-voice (`StateDirectory=jasper` → /var/lib/jasper at mode 0700).
DEFAULT_DB_PATH = "/var/lib/jasper/timers.db"


@dataclass
class Timer:
    id: str
    label: str | None
    fire_at: float           # absolute unix timestamp
    total_seconds: int       # original duration the user requested
    created_at: float

    @property
    def remaining_seconds(self) -> int:
        return max(0, int(self.fire_at - time.time()))


def human_duration(seconds: int) -> str:
    """Format a duration in seconds as a natural-language phrase
    suitable for TTS playback.

    Examples:
      ``60``   → ``'1 minute'``
      ``90``   → ``'1 minute and 30 seconds'``
      ``3600`` → ``'1 hour'``
      ``5400`` → ``'1 hour and 30 minutes'``
      ``3661`` → ``'1 hour, 1 minute, and 1 second'``
      ``1``    → ``'1 second'``
    """
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds} second" + ("s" if seconds != 1 else "")
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h} hour" + ("s" if h != 1 else ""))
    if m:
        parts.append(f"{m} minute" + ("s" if m != 1 else ""))
    if s:
        parts.append(f"{s} second" + ("s" if s != 1 else ""))
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


class TimerStore:
    """SQLite-backed persistence for timers. Single-process,
    single-threaded use — concurrent CRUD is serialised at the
    asyncio scheduler level (the scheduler is the only caller)."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # isolation_level=None → autocommit. Each statement is its own
        # transaction; no manual BEGIN/COMMIT needed.
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS timers ("
            "  id TEXT PRIMARY KEY,"
            "  label TEXT,"
            "  fire_at REAL NOT NULL,"
            "  total_seconds INTEGER NOT NULL,"
            "  created_at REAL NOT NULL"
            ")"
        )

    def add(self, timer: Timer) -> None:
        self._conn.execute(
            "INSERT INTO timers (id, label, fire_at, total_seconds, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            (
                timer.id, timer.label, timer.fire_at,
                timer.total_seconds, timer.created_at,
            ),
        )

    def remove(self, timer_id: str) -> None:
        self._conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))

    def all(self) -> list[Timer]:
        rows = self._conn.execute(
            "SELECT id, label, fire_at, total_seconds, created_at "
            "FROM timers ORDER BY fire_at"
        ).fetchall()
        return [
            Timer(
                id=r[0], label=r[1], fire_at=r[2],
                total_seconds=r[3], created_at=r[4],
            )
            for r in rows
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


class TimerScheduler:
    """Manages active timers. Owns the asyncio task lifecycle for
    each timer and the SQLite store.

    Wired into the daemon at startup via `start()` (restores
    persisted timers, drops expired) and shutdown via `stop()`
    (cancels in-flight tasks; persistence is untouched so the next
    `start()` restores them).

    `on_fire(timer)` is invoked when a timer's deadline elapses —
    the daemon supplies a coroutine that handles announcement
    (TTS + ducking + session-state gating). Errors from on_fire
    are caught and logged; they don't abort the scheduler.
    """

    def __init__(
        self,
        on_fire: Callable[[Timer], Awaitable[None]] | None = None,
        pre_render: Callable[[Timer], Awaitable[None]] | None = None,
        *,
        store: TimerStore | None = None,
        db_path: str = DEFAULT_DB_PATH,
    ):
        self._on_fire = on_fire
        self._pre_render = pre_render
        self._store = store if store is not None else TimerStore(db_path)
        self._timers: dict[str, Timer] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._started = False

    def set_on_fire(
        self, on_fire: Callable[[Timer], Awaitable[None]] | None,
    ) -> None:
        """Wire the announcement callback. Useful when the scheduler
        is constructed before the daemon-side announcer (so timer
        tools can be registered with the model before the wake loop
        exists, then this wires the announcer once it does)."""
        self._on_fire = on_fire

    def set_pre_render(
        self, pre_render: Callable[[Timer], Awaitable[None]] | None,
    ) -> None:
        """Wire the optional pre-render callback. Called as a
        background task whenever a new timer is added (`add`) or a
        persisted timer is restored at startup (`start`). Lets the
        daemon synthesise + cache the fire-time announcement WAV
        ahead of time so the actual fire is a cache hit — no
        synthesis-attempt latency eating the user's expected
        timing. Errors from pre_render are caught and logged; they
        never abort timer scheduling."""
        self._pre_render = pre_render

    async def start(self) -> None:
        """Restore persisted timers, drop those whose `fire_at` was
        during downtime, schedule asyncio tasks for the rest."""
        if self._started:
            return
        self._started = True
        now = time.time()
        for t in self._store.all():
            if t.fire_at <= now:
                logger.info(
                    "timer: dropping expired timer id=%s label=%r "
                    "(fire_at was %.0fs ago — daemon was down at fire time)",
                    t.id, t.label, now - t.fire_at,
                )
                self._store.remove(t.id)
                continue
            self._timers[t.id] = t
            self._tasks[t.id] = asyncio.create_task(
                self._run(t), name=f"timer-{t.id}",
            )
            self._kick_pre_render(t)
            logger.info(
                "timer: restored id=%s label=%r remaining=%ds",
                t.id, t.label, t.remaining_seconds,
            )

    async def stop(self) -> None:
        """Cancel all in-flight tasks. Persisted records remain so the
        next `start()` restores them. Idempotent."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self._started = False

    def add(self, seconds: int, label: str | None = None) -> Timer:
        """Add a new timer firing `seconds` from now. Persists +
        schedules. Raises ValueError on non-positive duration."""
        seconds = int(seconds)
        if seconds <= 0:
            raise ValueError(
                f"timer duration must be positive, got {seconds}"
            )
        now = time.time()
        timer = Timer(
            id=uuid.uuid4().hex[:8],
            label=label.strip() if label and label.strip() else None,
            fire_at=now + seconds,
            total_seconds=seconds,
            created_at=now,
        )
        self._store.add(timer)
        self._timers[timer.id] = timer
        self._tasks[timer.id] = asyncio.create_task(
            self._run(timer), name=f"timer-{timer.id}",
        )
        self._kick_pre_render(timer)
        logger.info(
            "timer: added id=%s label=%r duration=%ds",
            timer.id, timer.label, timer.total_seconds,
        )
        return timer

    def _kick_pre_render(self, timer: Timer) -> None:
        """Fire-and-forget background task to ensure the fire-time
        announcement WAV is cached before fire_at. Safe to call when
        no pre_render callback is wired (no-op)."""
        if self._pre_render is None:
            return

        async def _wrapped() -> None:
            try:
                await self._pre_render(timer)  # type: ignore[misc]
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "timer pre_render failed (id=%s): %s",
                    timer.id, e,
                )

        asyncio.create_task(_wrapped(), name=f"timer-prerender-{timer.id}")

    def list_active(self) -> list[Timer]:
        """Active timers, sorted by remaining time (soonest first)."""
        return sorted(self._timers.values(), key=lambda t: t.fire_at)

    def cancel(self, query: str) -> "tuple[bool, list[Timer]]":
        """Cancel a timer by id or label. Returns
        ``(cancelled, matches)`` — `cancelled` is True only when
        exactly one timer matched and was cancelled; otherwise
        False, with `matches` listing the candidates so the caller
        can disambiguate.

        Match precedence: exact id → exact label (case-insensitive) →
        id prefix → label substring (case-insensitive). The first
        rule that matches at all decides — multiple matches at that
        rule means ambiguous (no cancellation, return all)."""
        query = (query or "").strip()
        if not query:
            return False, []
        q_lower = query.lower()

        if query in self._timers:
            t = self._timers[query]
            self._cancel_one(t)
            return True, [t]

        matched = [
            t for t in self._timers.values()
            if t.label and t.label.lower() == q_lower
        ]
        if matched:
            if len(matched) == 1:
                self._cancel_one(matched[0])
                return True, matched
            return False, matched

        matched = [
            t for t in self._timers.values() if t.id.startswith(query)
        ]
        if matched:
            if len(matched) == 1:
                self._cancel_one(matched[0])
                return True, matched
            return False, matched

        matched = [
            t for t in self._timers.values()
            if t.label and q_lower in t.label.lower()
        ]
        if matched and len(matched) == 1:
            self._cancel_one(matched[0])
            return True, matched
        return False, matched

    def _cancel_one(self, timer: Timer) -> None:
        task = self._tasks.pop(timer.id, None)
        if task is not None:
            task.cancel()
        self._timers.pop(timer.id, None)
        try:
            self._store.remove(timer.id)
        except sqlite3.Error as e:
            logger.warning(
                "timer: store.remove failed (id=%s): %s", timer.id, e,
            )
        logger.info(
            "timer: cancelled id=%s label=%r", timer.id, timer.label,
        )

    async def _run(self, timer: Timer) -> None:
        try:
            remaining = max(0.0, timer.fire_at - time.time())
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return
        # Pop state FIRST so a concurrent cancel() is a no-op rather
        # than a double-cancel race against in-flight announcement.
        self._timers.pop(timer.id, None)
        self._tasks.pop(timer.id, None)
        try:
            self._store.remove(timer.id)
        except sqlite3.Error as e:
            logger.warning(
                "timer: store.remove on fire failed (id=%s): %s — "
                "will replay if daemon restarts before announcement",
                timer.id, e,
            )
        if self._on_fire is None:
            logger.warning(
                "timer fired but no on_fire callback wired — silent "
                "fire id=%s label=%r", timer.id, timer.label,
            )
            return
        try:
            await self._on_fire(timer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "timer on_fire callback failed (id=%s): %s",
                timer.id, e,
            )


def announcement_text(timer: Timer) -> str:
    """Build the spoken phrase for a fired timer.

    Labelled timers say the label ("Your pasta timer is up");
    unlabelled timers say the duration in noun form ("Your timer
    for 5 minutes is up") rather than adjective form ("Your
    5-minute timer is up") because compound modifiers don't
    compose cleanly across multi-unit durations — "Your 1-hour
    and 30-minute timer is up" reads worse than "Your timer for
    1 hour and 30 minutes is up". Kept here, free of daemon
    dependencies, so it's testable in isolation."""
    if timer.label:
        return f"Your {timer.label} timer is up."
    return f"Your timer for {human_duration(timer.total_seconds)} is up."
