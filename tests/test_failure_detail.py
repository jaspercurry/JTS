# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The provider's own reason for a reconnect failure reaches logs and /state.

`failure_detail` is provider-agnostic and imports no SDK, so these run
everywhere — which matters because redaction here is a non-negotiable
(a secret must never reach a log or /state).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from jasper.voice._supervisor import (
    FAILURE_DETAIL_LIMIT,
    failure_detail,
    hand_off_first_connect,
)
from tests._log_events import event_fields
from tests.failure_detail_fixtures import Rejected

# Captured verbatim from a live xAI 403 on the speaker.
_BILLING_BODY = (
    b'{"code":"The caller does not have permission to execute the specified'
    b' operation","error":"Your team 2f71172e has either used all available'
    b' credits or reached its monthly spending limit."}'
)


@pytest.mark.parametrize("body", [_BILLING_BODY, bytearray(_BILLING_BODY)])
def test_handshake_body_reaches_the_detail(body: object) -> None:
    """The reason is what str(exc) throws away — bytes or bytearray."""
    detail = failure_detail(Rejected(403, body))
    assert "403" in detail
    assert "used all available credits" in detail


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("connection reset by peer"),
        Rejected(500, b""),
        Rejected(500, None),
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
    detail = failure_detail(Rejected(401, template.format(s=secret).encode()))
    assert secret not in detail


def test_detail_is_bounded() -> None:
    """A provider serving an HTML error page cannot flood the journal."""
    huge = b"<html>" + b"x" * 20_000 + b"</html>"
    assert len(failure_detail(Rejected(502, huge))) <= FAILURE_DETAIL_LIMIT


def test_redaction_precedes_truncation() -> None:
    """Clipping a redacted tail can never leave half a credential behind."""
    secret = "xai-" + "a" * 40
    body = b'{"error":"' + b"padding " * 40 + b'","api_key":"' + secret.encode() + b'"}'
    assert secret not in failure_detail(Rejected(401, body))


def test_a_prefix_less_key_redacts_only_when_passed_as_a_literal() -> None:
    """`key` isn't one of the redactor's keywords and `plainvalue123`
    matches no provider prefix, so only a caller-supplied literal
    catches it — see ADR-0243."""
    body = b'{"error":"bad key","key":"plainvalue123"}'
    detail = failure_detail(Rejected(401, body), literals=("plainvalue123",))
    assert "plainvalue123" not in detail
    assert "bad key" in detail
    assert "401" in detail


class _FakeSupervisedConnection:
    """The handful of fields `hand_off_first_connect` and
    `request_unplanned_reopen` touch — not a full `SupervisedConnection`."""

    PROVIDER_NAME = "fake"
    _planned_rotate = False
    _reconnect_event = asyncio.Event()


def test_hand_off_first_connect_redacts_the_connections_own_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A prefix-less key the caller passes as a literal reaches neither
    the journal nor `/state` from the initial-connect path either —
    `hand_off_first_connect` takes `literals` as a parameter rather than
    reading it off `conn` so the `SupervisedConnection` Protocol stays
    narrow; this pins that the caller's value actually gets there."""
    caplog.set_level(logging.WARNING, logger="jasper.voice._supervisor")
    body = b'{"error":"bad key","key":"plainvalue123"}'
    hand_off_first_connect(
        _FakeSupervisedConnection(),  # type: ignore[arg-type]
        Rejected(401, body),
        literals=("plainvalue123",),
    )
    fields = event_fields(caplog, "voice.initial_connect.failed")
    assert "plainvalue123" not in fields["reason"]
    assert "bad key" in fields["reason"]
