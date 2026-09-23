# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Process-local, single-use OAuth state for the threaded setup wizards.

The nonce is the provider's OAuth ``state``: the only defence against a
login-CSRF callback, so it is unguessable, consumed once, and expires.
"""
from __future__ import annotations

import secrets
import time
from threading import Lock
from typing import Generic, TypeVar

_T = TypeVar("_T")
_FLOW_TTL_SEC = 600.0  # Google and Spotify auth codes live 10 min


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


class PendingFlows(Generic[_T]):
    def __init__(self) -> None:
        self._entries: dict[str, tuple[_T, float]] = {}
        self._lock = Lock()

    def add(self, state: str, payload: _T) -> None:
        with self._lock:
            now = time.monotonic()
            self._gc(now)
            self._entries[state] = (payload, now)

    def consume(self, state: str) -> _T | None:
        with self._lock:
            self._gc(time.monotonic())
            entry = self._entries.pop(state, None)
            return entry[0] if entry is not None else None

    def _gc(self, now: float) -> None:
        expired = [
            state for state, (_, created) in self._entries.items()
            if now - created > _FLOW_TTL_SEC
        ]
        for state in expired:
            del self._entries[state]
