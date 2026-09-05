# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared voice_daemon usage-store double: the one method the daemon calls
on session teardown, plus the read-only degraded flag some callers check."""

from __future__ import annotations


class FakeUsageStore:
    write_degraded = False

    def __init__(self) -> None:
        self.close_calls = 0

    def close_session(self, session_id, in_tokens, out_tokens, usage=None):
        # Mirrors the real store's own assert so a re-entrant call after
        # _session_id was cleared fails the same way it would in production.
        assert session_id is not None
        self.close_calls += 1
        return 0.0
