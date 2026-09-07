# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single-flight TTL cache for expensive read-only jasper-control routes."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..log_event import log_event

logger = logging.getLogger(__name__)

_MISSING = object()


class SingleFlightTTLCache:
    """Small thread-safe cache for expensive read-only JSON routes."""

    def __init__(
        self,
        ttl_sec: float,
        wait_timeout_sec: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_sec = float(ttl_sec)
        self._wait_timeout_sec = float(wait_timeout_sec)
        self._clock = clock
        self._cond = threading.Condition()
        self._value: Any = _MISSING
        self._computed_at = 0.0
        self._expires_at = 0.0
        self._inflight = False

    def get_or_compute(self, compute: Callable[[], Any]) -> Any:
        """Return a fresh value, sharing one in-flight computation.

        Only successful computations are cached. If the compute raises,
        waiters are released and the next caller may retry.

        Blocks up to `wait_timeout_sec` per in-flight compute, then returns
        the stale value if there is one, else raises TimeoutError.
        """
        while True:
            with self._cond:
                now = self._clock()
                if self._value is not _MISSING and now < self._expires_at:
                    return self._value
                if not self._inflight:
                    self._inflight = True
                    break
                if not self._cond.wait(timeout=self._wait_timeout_sec):
                    if self._value is not _MISSING:
                        log_event(
                            logger,
                            "state.stale_value_served",
                            age_sec=round(self._clock() - self._computed_at, 1),
                            level=logging.WARNING,
                        )
                        return self._value
                    raise TimeoutError(
                        "state compute did not finish within its budget",
                    )

        computed = False
        try:
            value = compute()
            computed = True
        finally:
            if not computed:
                with self._cond:
                    self._inflight = False
                    self._cond.notify_all()

        with self._cond:
            self._value = value
            self._computed_at = self._clock()
            self._expires_at = self._computed_at + self._ttl_sec
            self._inflight = False
            self._cond.notify_all()
            return value
