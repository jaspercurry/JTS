# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Receiver session release and its mux-owned outcome (ADR-0270)."""
from __future__ import annotations

import logging
import time
from typing import Any

from .busctl import name_is_absent, run_busctl
from .log_event import log_event

logger = logging.getLogger(__name__)

REASONS = frozenset({
    "not_attempted", "cleanup_pending", "drop_acknowledged",
    "stop_unconfirmed", "cleanup_failed", "receiver_absent",
})


class AirplaySessionCleanup:
    def __init__(self) -> None:
        self._status = "unobserved"
        self._reason = "not_attempted"
        self._attempts = 0
        self._attempted_at: float | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "reason": self._reason,
            "attempts": self._attempts,
            "attempted_at": self._attempted_at,
        }

    async def release(self) -> None:
        # DropSession is unscoped: the caller must serialize it with selection.
        self._attempts += 1
        self._attempted_at = time.time()
        self._status, self._reason = "unobserved", "cleanup_pending"
        dropped = await run_busctl(
            "--auto-start=no", "call", "org.gnome.ShairportSync", "/org/gnome/ShairportSync",
            "org.gnome.ShairportSync", "DropSession",
        )
        if dropped is not None and dropped.returncode == 0:
            # Shairport replies after stop_play(), not after queuing a request.
            self._status, self._reason = "ok", "drop_acknowledged"
        elif dropped is not None and name_is_absent(dropped.stderr):
            self._status, self._reason = "ok", "receiver_absent"
        else:
            stopped = await run_busctl(
                "--auto-start=no", "call", "org.mpris.MediaPlayer2.ShairportSync",
                "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "Stop",
            )
            # Stop asks the sender to stop; it cannot prove session release.
            self._status = "degraded"
            self._reason = (
                "stop_unconfirmed"
                if stopped is not None and stopped.returncode == 0
                else "cleanup_failed"
            )
        log_event(
            logger, "airplay.session_cleanup", **self.snapshot(),
            level=logging.WARNING if self._status == "degraded" else logging.INFO,
        )
