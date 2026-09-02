# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The provider's own reason for a reconnect failure reaches logs and /state.

`failure_detail` is provider-agnostic and imports no SDK, so these run
everywhere — which matters because redaction here is a non-negotiable
(a secret must never reach a log or /state).
"""
from __future__ import annotations

import pytest

from jasper.voice._supervisor import FAILURE_DETAIL_LIMIT, failure_detail


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.body = body


class _Rejected(Exception):
    """The shape websockets raises when a handshake is refused."""

    def __init__(self, status_code: int, body: object) -> None:
        super().__init__(
            f"server rejected WebSocket connection: HTTP {status_code}"
        )
        self.response = _Response(status_code, body)


# Captured verbatim from a live xAI 403 on the speaker.
_BILLING_BODY = (
    b'{"code":"The caller does not have permission to execute the specified'
    b' operation","error":"Your team 2f71172e has either used all available'
    b' credits or reached its monthly spending limit."}'
)


@pytest.mark.parametrize("body", [_BILLING_BODY, bytearray(_BILLING_BODY)])
def test_handshake_body_reaches_the_detail(body: object) -> None:
    """The reason is what str(exc) throws away — bytes or bytearray."""
    detail = failure_detail(_Rejected(403, body))
    assert "403" in detail
    assert "used all available credits" in detail


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("connection reset by peer"),
        _Rejected(500, b""),
        _Rejected(500, None),
    ],
    ids=["no-response", "empty-body", "null-body"],
)
def test_falls_back_to_str_without_a_usable_body(exc: BaseException) -> None:
    assert failure_detail(exc) == " ".join(str(exc).split())


@pytest.mark.parametrize(
    "secret",
    [
        "xai-abcdefgh1234567890abcd",
        "sk-abcdefgh1234567890abcd",
        "AIzaAbCdEfGh1234567890abcd",
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        '{{"error":"rejected","token":"{s}"}}',
        "api_key={s}",
        "Authorization: Bearer {s}",
    ],
)
def test_credentials_never_reach_the_detail(template: str, secret: str) -> None:
    """Non-negotiable: this string lands in the journal and /state."""
    detail = failure_detail(_Rejected(401, template.format(s=secret).encode()))
    assert secret not in detail


def test_detail_is_bounded() -> None:
    """A provider serving an HTML error page cannot flood the journal."""
    huge = b"<html>" + b"x" * 20_000 + b"</html>"
    assert len(failure_detail(_Rejected(502, huge))) <= FAILURE_DETAIL_LIMIT


def test_redaction_precedes_truncation() -> None:
    """Clipping a redacted tail can never leave half a credential behind."""
    secret = "xai-" + "a" * 40
    body = b'{"error":"' + b"padding " * 40 + b'","api_key":"' + secret.encode() + b'"}'
    assert secret not in failure_detail(_Rejected(401, body))
