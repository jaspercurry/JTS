# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One placement grant per pose batch; retakes require a new grant."""

from __future__ import annotations

import logging
import threading
import time
from copy import deepcopy
from typing import Any, Callable

from jasper.log_event import log_event

from .capture_plan import (
    AUTO_ADVANCE_TAP,
    POSITION_BATCH_CONFIG_KEY,
    POSITION_BATCH_SIZE_KEY,
    POSITION_BATCH_START_KEY,
    POSITION_DEG_KEY,
    POSITION_HAND_RELEASED_KEY,
    POSITION_ROLE_KEY,
    POSITION_VERTICAL_DEG_KEY,
    elevation_clause,
)
from .capture_source import CaptureBeginDeferred, CaptureBeginRefused
from .refusal_copy import (
    REASON_POSITION_HOLD_EXPIRED,
    REASON_POSITION_TARGET_MISSING,
    REASON_SESSION_CEILING_EXPIRED,
)

logger = logging.getLogger(__name__)

# Per-hold limit; the session volume owner enforces the whole-operation limit.
REMOTE_POSITION_HOLD_BUDGET_S = 600.0

#: How often a held begin retries the gate. The phone re-posts its deferred
#: ``begin_capture`` every 1.5 s (capture-page wait screen); every local runner
#: keeps that cadence so gate logging and driver pacing see the same rhythm the
#: remote tier was built against.
POSITION_HOLD_POLL_S = 1.5
POSITION_HOLD_CODE = "awaiting_position"
POSITION_HOLD_EXPIRED_CODE = REASON_POSITION_HOLD_EXPIRED
POSITION_TARGET_MISSING_CODE = REASON_POSITION_TARGET_MISSING
SESSION_CEILING_EXPIRED_CODE = REASON_SESSION_CEILING_EXPIRED
POSITION_GATE_TERMINAL_CODES = frozenset({
    POSITION_HOLD_EXPIRED_CODE, POSITION_TARGET_MISSING_CODE, SESSION_CEILING_EXPIRED_CODE,
})
POSITION_READY_ENDPOINT = "/sound/speaker/crossover/v2/position-ready"


def _prompt_of(screen: dict[str, Any]) -> dict[str, str]:
    return {name: str(screen.get(name) or "") for name in ("progress", "title", "body")}


def _granted(
    index: int, attempt: int, screen: dict[str, Any], batch: tuple[int, int, int],
) -> dict[str, Any]:
    """The entry a grant is about to record: the only fact that moves while a pose
    batch's configs 2..N play under the first config's release."""
    batch_start, batch_size, config = batch
    return {
        "index": index, "attempt": attempt, "prompt": _prompt_of(screen),
        "batch": {"start": batch_start, "size": batch_size, "ordinal": config},
    }


class PositionGate:
    """Thread-safe capture admission shared by human and external movers.

    Reposted begins are idempotent. A placement grant carries only to the next
    capture and attempt within the same declared pose batch. A skipped index,
    repeated index, changed pose or abandoned hold needs a fresh grant.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic
        self._pending: dict[str, Any] | None = None
        self._current: dict[str, Any] | None = None
        self._released: set[tuple[int, int]] = set()
        self._opened_at: float | None = None
        self._session_ceiling_expired = False
        self._last: tuple[int, int, tuple[int, int, int, int]] | None = None

    def gate(self, index: int, attempt: int, entry: Any) -> None:
        screen = getattr(entry, "screen", None) or {}
        raw_degrees = screen.get(POSITION_DEG_KEY)
        if raw_degrees is None:
            self.abandon_hold()
            raise CaptureBeginRefused(
                POSITION_TARGET_MISSING_CODE,
                "This measurement did not say where the microphone should be.",
            )
        target = int(raw_degrees)
        vertical = int(screen.get(POSITION_VERTICAL_DEG_KEY) or 0)
        rise = f", {elevation_clause(vertical)}" if vertical else ""
        role = str(screen.get(POSITION_ROLE_KEY) or "")
        key = (int(index), int(attempt))
        batch_start = int(screen.get(POSITION_BATCH_START_KEY) or index)
        batch_size = int(screen.get(POSITION_BATCH_SIZE_KEY) or 1)
        config = int(screen.get(POSITION_BATCH_CONFIG_KEY) or 1)
        if not 1 <= config <= batch_size or batch_start + config - 1 != index:
            raise CaptureBeginRefused(POSITION_TARGET_MISSING_CODE, "Invalid pose batch identity.")
        batch = (batch_start, batch_size, target, vertical)
        now = self._clock()
        with self._lock:
            if key in self._released:
                if self._pending is None:
                    self._current = _granted(index, attempt, screen, (batch_start, batch_size, config))
                return
            opened = self._opened_at
            waited = 0.0 if opened is None else now - opened
            if waited > REMOTE_POSITION_HOLD_BUDGET_S or self._session_ceiling_expired:
                expired_hold = waited > REMOTE_POSITION_HOLD_BUDGET_S
                self._pending = None
                self._current = None
                self._opened_at = None
                self._last = None
                log_event(
                    logger,
                    "correction.crossover_v2_position_hold_expired" if expired_hold
                    else "correction.crossover_v2_session_ceiling_expired",
                    level=logging.WARNING, index=index, attempt=attempt,
                    degrees=target, waited_s=round(waited, 1),
                )
                raise CaptureBeginRefused(
                    POSITION_HOLD_EXPIRED_CODE if expired_hold else SESSION_CEILING_EXPIRED_CODE,
                    "Nothing reported the microphone in place, so the measurement stopped waiting."
                    if expired_hold else
                    "The measurement ran out of time before the microphone reached every position.",
                )
            if self._last == (index - 1, attempt - 1, batch) and self._pending is None:
                self._released.add(key)
                self._last = (index, attempt, batch)
                self._current = _granted(index, attempt, screen, (batch_start, batch_size, config))
                return
            if self._pending is None:
                self._opened_at = now
                self._current = None
                self._pending = {
                    "index": index, "attempt": attempt, "degrees": target,
                    "vertical_deg": vertical, "role": role,
                    "prompt": _prompt_of(screen),
                    "hand_released": (
                        screen[POSITION_HAND_RELEASED_KEY] == "true"
                        if POSITION_HAND_RELEASED_KEY in screen else
                        str(screen.get("auto_advance") or "") == AUTO_ADVANCE_TAP
                    ),
                    "action": {
                        "id": "crossover_v2_position_ready",
                        "label": ("Microphone is on the design axis (0°)" if target == 0
                                  else f"Microphone is at {target:+d}°") + rise,
                        "endpoint": POSITION_READY_ENDPOINT,
                        "body": {
                            "index": index, "attempt": attempt, "degrees": target,
                            "vertical_deg": vertical,
                        },
                    },
                }
                self._last = (index, attempt, batch)
                log_event(
                    logger, "correction.crossover_v2_position_pending",
                    index=index, attempt=attempt, degrees=target, vertical_deg=vertical, role=role,
                )
        raise CaptureBeginDeferred(
            POSITION_HOLD_CODE, f"Waiting for the microphone to reach {target:+d}°{rise}.",
        )

    def published(self) -> dict[str, dict[str, Any] | None]:
        """The hold awaiting a release and the entry a grant is executing.

        Never both set, and read under one acquisition: read separately, a
        runner re-entering its grant between them pairs a stale hold with a
        fresh executing entry, a state the gate itself never holds.
        """
        with self._lock:
            return {"pending": deepcopy(self._pending), "current": deepcopy(self._current)}

    def note_session_ceiling_expired(self) -> None:
        """Latch the session volume owner's ceiling finding, including after drain."""
        with self._lock:
            self._session_ceiling_expired = True

    def abandon_hold(self) -> None:
        """Clear the placement grant when recovery interrupts the capture sequence."""
        with self._lock:
            abandoned = self._pending
            self._pending = None
            self._current = None
            self._opened_at = None
            self._last = None
            self._released.clear()
        if abandoned:
            log_event(
                logger, "correction.crossover_v2_position_hold_abandoned",
                index=int(abandoned["index"]), attempt=int(abandoned["attempt"]),
                degrees=int(abandoned["degrees"]),
            )

    def release(self, index: int | None = None, attempt: int | None = None) -> dict[str, Any]:
        """Accept only the named pending capture attempt; stale actions raise ValueError."""
        with self._lock:
            pending = self._pending
            if not pending:
                raise ValueError("no measurement is waiting for the microphone right now")
            wanted = int(pending["index"])
            wanted_attempt = int(pending["attempt"])
            if index is None or attempt is None:
                raise ValueError("a placement grant must name both index and attempt")
            if int(index) != wanted:
                raise ValueError(f"measurement {wanted} is waiting, not {int(index)}")
            if int(attempt) != wanted_attempt:
                raise ValueError(f"attempt {wanted_attempt} is waiting, not {int(attempt)}")
            self._released.add((wanted, wanted_attempt))
            released = deepcopy(pending)
            self._pending = None
            self._opened_at = None
        log_event(
            logger, "correction.crossover_v2_position_released",
            index=wanted, attempt=wanted_attempt, degrees=int(released["degrees"]),
        )
        return released
