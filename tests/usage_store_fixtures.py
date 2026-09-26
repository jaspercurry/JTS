# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared voice_daemon usage-store double: the session open/close pair the
daemon calls around a turn, plus the read-only degraded flag some callers
check."""

from __future__ import annotations


class FakeUsageStore:
    write_degraded = False

    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self._close_error = close_error
        self.open_calls = 0
        self.close_calls = 0

    def open_session(self, provider=None) -> int:
        self.open_calls += 1
        return 1

    def close_session(self, session_id, in_tokens, out_tokens, usage=None):
        # Traps a re-entrant close after _session_id was cleared.
        assert session_id is not None
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error
        return 0.0
