# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the connection supervisor's tight-retry-loop escalation cue.

Covers `_FailureFingerprint`, `_maybe_fire_escalation_cue`, and the end-
to-end behaviour of `_reconnect_with_backoff` driving the escalation
through 5 identical failures. Reuses the test plumbing from
`test_gemini_connection.py` (`_FakeConnect`, `_make_conn`, `_wait_until`).
"""
from __future__ import annotations

import asyncio

import pytest

try:
    from jasper.voice.gemini_session import (
        ESCALATION_CUE_SLUG,
        ESCALATION_RATE_LIMIT_SEC,
        ESCALATION_REPEAT_THRESHOLD,
        GeminiLiveConnection,
        _FailureFingerprint,
    )
    from jasper.tools import ToolRegistry
    _HAVE_GENAI = True
except ImportError:
    _HAVE_GENAI = False

pytestmark = pytest.mark.skipif(
    not _HAVE_GENAI, reason="google-genai not installed in this environment"
)


# Reuse the well-tested fakes from the existing connection tests.
from tests.test_gemini_connection import (  # noqa: E402
    _FakeConnect,
    _make_conn,
    _wait_until,
)


# ---------------------------------------------------------------------------
# _FailureFingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_from_plain_exception():
    """Plain RuntimeError: exc_type is the class name, no close code,
    reason is str(exc)."""
    fp = _FailureFingerprint.from_exception(RuntimeError("boom"))
    assert fp.exc_type == "RuntimeError"
    assert fp.close_code is None
    assert fp.reason == "boom"


def test_fingerprint_from_websocket_exception():
    """websockets-style exception with `.rcvd.code` and `.rcvd.reason`:
    fingerprint pulls the close code and uses the reason text."""
    class _Rcvd:
        code = 1008
        reason = "BidiGenerateContent session expired"

    class _WSClosed(Exception):
        rcvd = _Rcvd()

    fp = _FailureFingerprint.from_exception(_WSClosed())
    assert fp.exc_type == "_WSClosed"
    assert fp.close_code == 1008
    assert fp.reason == "BidiGenerateContent session expired"


def test_fingerprint_truncates_long_reason():
    """A 500-char reason should be truncated to 200 so jittery error
    messages with timestamps don't make 'identical' failures look
    distinct on the equality check."""
    long_reason = "A" * 500
    fp = _FailureFingerprint.from_exception(RuntimeError(long_reason))
    assert len(fp.reason) == 200


def test_fingerprint_equality_by_shape():
    """Two fingerprints from differently-instantiated exceptions of the
    same shape compare equal — that's the whole point of the type."""
    fp1 = _FailureFingerprint.from_exception(RuntimeError("hello"))
    fp2 = _FailureFingerprint.from_exception(RuntimeError("hello"))
    assert fp1 == fp2


def test_fingerprint_distinct_when_close_codes_differ():
    """Same exception type, same reason, but different close codes →
    not equal. This is the case where the WS layer distinguishes
    failure modes (1006 abnormal vs 1008 policy violation) that look
    identical at the str(exc) level."""
    class _A(Exception):
        class _Rcvd:
            code = 1006
            reason = "abnormal"
        rcvd = _Rcvd()

    class _B(Exception):
        class _Rcvd:
            code = 1008
            reason = "abnormal"
        rcvd = _Rcvd()

    assert _FailureFingerprint.from_exception(_A()) != _FailureFingerprint.from_exception(_B())


# ---------------------------------------------------------------------------
# _maybe_fire_escalation_cue (direct unit tests, no real reconnect loop)
# ---------------------------------------------------------------------------


def _conn_for_unit_test():
    """Build a connection without starting it. The supervisor isn't
    running, so we can poke `_recent_failure_fingerprints` directly
    and call `_maybe_fire_escalation_cue` synchronously."""
    conn, _ = _make_conn()
    return conn


def _fp(exc_type="RuntimeError", code=None, reason="x"):
    return _FailureFingerprint(exc_type=exc_type, close_code=code, reason=reason)


@pytest.mark.asyncio
async def test_escalation_does_not_fire_below_threshold():
    """Buffer not yet full → no cue, no callback invocation."""
    conn = _conn_for_unit_test()
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    for _ in range(ESCALATION_REPEAT_THRESHOLD - 1):
        conn._recent_failure_fingerprints.append(_fp())
    conn._maybe_fire_escalation_cue()
    # Yield once so any erroneously-spawned task has a chance to run.
    await asyncio.sleep(0)
    assert calls == []


@pytest.mark.asyncio
async def test_escalation_fires_when_5_consecutive_identical():
    """5 identical failures → the callback fires with the
    cant_reach_cloud slug exactly once."""
    conn = _conn_for_unit_test()
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    for _ in range(ESCALATION_REPEAT_THRESHOLD):
        conn._recent_failure_fingerprints.append(_fp())
    conn._maybe_fire_escalation_cue()
    await _wait_until(lambda: len(calls) == 1, timeout=1.0)
    assert calls == [ESCALATION_CUE_SLUG]


@pytest.mark.asyncio
async def test_escalation_does_not_fire_with_mixed_failures():
    """Buffer has 5 entries but they're not all identical → no fire."""
    conn = _conn_for_unit_test()
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    fingerprints = [
        _fp(exc_type="RuntimeError"),
        _fp(exc_type="RuntimeError"),
        _fp(exc_type="ValueError"),  # the odd one out
        _fp(exc_type="RuntimeError"),
        _fp(exc_type="RuntimeError"),
    ]
    for fp in fingerprints:
        conn._recent_failure_fingerprints.append(fp)
    conn._maybe_fire_escalation_cue()
    await asyncio.sleep(0.05)
    assert calls == []


@pytest.mark.asyncio
async def test_escalation_rate_limited_within_window():
    """Once fired, a second call inside the rate-limit window doesn't
    re-fire even if the buffer is still all-identical."""
    conn = _conn_for_unit_test()
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    for _ in range(ESCALATION_REPEAT_THRESHOLD):
        conn._recent_failure_fingerprints.append(_fp())

    # First call fires.
    conn._maybe_fire_escalation_cue()
    await _wait_until(lambda: len(calls) == 1, timeout=1.0)

    # Second call within the rate-limit window: no new fire.
    conn._maybe_fire_escalation_cue()
    await asyncio.sleep(0.05)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_escalation_refires_after_rate_limit_window():
    """If the rate-limit window has elapsed, a still-identical buffer
    can fire the cue again. Simulated by rewinding `_last_escalation_at`."""
    conn = _conn_for_unit_test()
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    conn.set_failure_escalation_cb(cb)
    for _ in range(ESCALATION_REPEAT_THRESHOLD):
        conn._recent_failure_fingerprints.append(_fp())
    conn._maybe_fire_escalation_cue()
    await _wait_until(lambda: len(calls) == 1, timeout=1.0)

    # Pretend the last fire was 2 hours ago.
    conn._last_escalation_at -= 2 * ESCALATION_RATE_LIMIT_SEC
    conn._maybe_fire_escalation_cue()
    await _wait_until(lambda: len(calls) == 2, timeout=1.0)


@pytest.mark.asyncio
async def test_escalation_no_op_without_callback():
    """No callback wired (test/minimal-harness mode) → silent no-op,
    no crash. Also: rate-limit timer not consumed, so wiring the
    callback later still works.

    The sentinel for "never fired" is float('-inf') — picked so the
    very first eligible call passes the rate-limit window check
    even if the event loop's monotonic time is small."""
    conn = _conn_for_unit_test()
    assert conn._last_escalation_at == float("-inf")  # initial sentinel
    # Don't call set_failure_escalation_cb.
    for _ in range(ESCALATION_REPEAT_THRESHOLD):
        conn._recent_failure_fingerprints.append(_fp())
    conn._maybe_fire_escalation_cue()  # must not raise
    assert conn._last_escalation_at == float("-inf")  # not consumed


# ---------------------------------------------------------------------------
# Integration: full _reconnect_with_backoff with 5 identical failures
# ---------------------------------------------------------------------------


async def test_supervisor_fires_cue_after_5_identical_reconnect_failures():
    """End-to-end: feed 5 identical failures into the supervisor's
    reconnect loop and verify the wired callback was invoked once
    with `cant_reach_cloud`. Uses a 6th attempt that succeeds so the
    test terminates cleanly."""
    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        keepalive_period_sec=9999.0,
        # 6 attempts, all instant. First 5 will fail, 6th succeeds.
        backoff_schedule=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        connect_factory=factory,
    )

    cue_calls: list[str] = []

    async def cb(slug: str) -> None:
        cue_calls.append(slug)

    conn.set_failure_escalation_cb(cb)

    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        # Queue 5 identical exceptions on the next 5 opens; the 6th open
        # has nothing queued so it succeeds (the factory's default path).
        class _IdenticalDrop(Exception):
            class _Rcvd:
                code = 1008
                reason = "BidiGenerateContent session expired"
            rcvd = _Rcvd()

        factory.next_exceptions = [_IdenticalDrop() for _ in range(5)]

        # Drop the active session to wake the supervisor.
        sess = factory.sessions[0]

        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()

        sess.feed_error(_Drop())

        # After 5 identical failures + 6th success, the cue callback
        # should have fired exactly once.
        await _wait_until(lambda: len(cue_calls) == 1, timeout=3.0)
        assert cue_calls == [ESCALATION_CUE_SLUG]
        # And the buffer was cleared on the successful reconnect.
        assert len(conn._recent_failure_fingerprints) == 0
    finally:
        await conn.stop()


async def test_supervisor_does_not_fire_when_failures_recover_quickly():
    """3 failures then success: cue does not fire — we're below the
    5-identical threshold and the buffer is cleared on success."""
    factory = _FakeConnect()
    conn = GeminiLiveConnection(
        api_key="fake",
        model="fake-model",
        voice="Aoede",
        keepalive_period_sec=9999.0,
        backoff_schedule=(0.0, 0.0, 0.0, 0.0),
        connect_factory=factory,
    )

    cue_calls: list[str] = []

    async def cb(slug: str) -> None:
        cue_calls.append(slug)

    conn.set_failure_escalation_cb(cb)

    registry = ToolRegistry()
    await conn.start(registry, "system")
    try:
        factory.next_exceptions = [
            RuntimeError("transient 1"),
            RuntimeError("transient 2"),
            RuntimeError("transient 3"),
        ]
        sess = factory.sessions[0]

        class _Drop(Exception):
            class _Rcvd:
                code = 1006
                reason = "abnormal"
            rcvd = _Rcvd()

        sess.feed_error(_Drop())

        # Wait for the supervisor to reconnect successfully.
        await _wait_until(lambda: len(factory.sessions) >= 2, timeout=3.0)
        # Give any spurious cue task a moment to surface.
        await asyncio.sleep(0.1)
        assert cue_calls == []
        # Buffer cleared on success.
        assert len(conn._recent_failure_fingerprints) == 0
    finally:
        await conn.stop()
