# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A broken cloud connection is announced once, and only when a human must act.

Covers `is_transient` (which failures retrying can fix), `OutageTracker`
(the once-per-outage edge trigger), and one end-to-end pass through the
Gemini supervisor's reconnect loop. See ADR-0215.
"""
from __future__ import annotations

import asyncio

import pytest

from jasper.voice._supervisor import (
    ESCALATION_CUE_SLUG,
    OutageTracker,
    is_transient,
)
from tests.test_failure_detail import _Rejected

try:
    import google.genai  # noqa: F401

    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

_needs_genai = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


class _Terminal(Exception):
    """A rejected handshake: retrying it forever changes nothing."""

    status_code = 403


class _OtherTerminal(Exception):
    status_code = 401


class _Status:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


# ---------------------------------------------------------------------------
# is_transient
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (_Status(401), False),
        (_Status(403), False),
        (_Status(404), False),
        (_Status(409), True),
        (_Status(429), True),
        (_Status(502), True),
        (_Rejected(403, b""), False),
        (_Rejected(500, b""), True),
        (OSError("network blip"), True),
        (ValueError("bad config"), False),
        (TypeError("wrong shape"), False),
    ],
)
def test_is_transient_classifies_by_who_must_act(
    exc: object, transient: bool,
) -> None:
    """Terminal means a human must act; everything else retries in silence."""
    assert is_transient(exc) is transient  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OutageTracker
# ---------------------------------------------------------------------------


async def _drive(tracker: OutageTracker, events: list[object]) -> None:
    for event in events:
        if event is None:
            tracker.on_recovery()
        else:
            tracker.on_failure(event)  # type: ignore[arg-type]
        # The cue is fire-and-forget; let the spawned task run.
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([_Terminal()], 1),
        ([OSError("blip"), OSError("blip again")], 0),
        ([_Terminal(), _Terminal()], 1),
        ([_Terminal(), _OtherTerminal()], 1),
        ([_Terminal(), None, _Terminal()], 2),
        ([_Terminal(), OSError("blip"), _Terminal()], 1),
    ],
    ids=[
        "terminal-speaks",
        "transient-silent",
        "same-outage-speaks-once",
        "changed-terminal-class-still-one-outage",
        "recovery-rearms",
        "transient-mid-outage-does-not-rearm",
    ],
)
async def test_escalation_speaks_once_per_outage(
    events: list[object], expected: int,
) -> None:
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    tracker = OutageTracker()
    tracker.set_callback(cb)
    await _drive(tracker, events)
    assert calls == [ESCALATION_CUE_SLUG] * expected


async def test_held_cue_is_dropped_when_the_outage_ended_first() -> None:
    """An outage that began before a cue player existed is announced when
    one is wired — but only if it is still going. Recovering first drops
    the held cue instead of speaking about a connection that works."""
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    tracker = OutageTracker()
    tracker.on_failure(_Terminal())
    tracker.on_recovery()
    tracker.set_callback(cb)
    await asyncio.sleep(0)
    assert calls == []


# ---------------------------------------------------------------------------
# Integration: the Gemini supervisor's reconnect loop drives the trigger
# ---------------------------------------------------------------------------


@_needs_genai
async def test_supervisor_speaks_once_then_recovers_silently() -> None:
    """A terminal reopen failure announces once; the next open recovers
    without a second cue and clears the /state failure detail."""
    from jasper.tools import ToolRegistry
    from jasper.voice.gemini_session import GeminiLiveConnection
    from tests.test_gemini_connection import _FakeConnect, _wait_until

    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0, 0.0, 0.0),
        connect_factory=factory,
    )

    cue_calls: list[str] = []

    async def cb(slug: str) -> None:
        cue_calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    await conn.start(ToolRegistry(), "system")
    try:
        factory.next_exceptions = [_Rejected(403, b"")]

        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"

            rcvd = _Rcvd()

        factory.sessions[0].feed_error(_Drop())

        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        await asyncio.sleep(0.05)
        assert cue_calls == [ESCALATION_CUE_SLUG]
        assert conn.last_failure_detail() is None
    finally:
        await conn.stop()
