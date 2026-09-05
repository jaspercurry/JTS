# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared double for the exception shape ``websockets`` raises when a
handshake is refused, used by the failure-detail and supervisor-escalation
suites."""

from __future__ import annotations


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.body = body


class Rejected(Exception):
    """The shape websockets raises when a handshake is refused."""

    def __init__(self, status_code: int, body: object) -> None:
        super().__init__(
            f"server rejected WebSocket connection: HTTP {status_code}"
        )
        self.response = _Response(status_code, body)
