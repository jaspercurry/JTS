# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""A broken cloud connection is announced once, and only when a human must act.

Covers `is_transient` (which failures retrying can fix),
`is_network_down` (whether the household's own link is the fault),
`outage_cue` (which pre-baked remedy a failure names), `OutageTracker`
(the once-per-remedy-per-outage edge trigger and the network cue's
consecutive-failure debounce), and one end-to-end pass through the
Gemini supervisor's reconnect loop. See ADR-0215.
"""
from __future__ import annotations

import asyncio
import errno
import socket
import warnings

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from jasper.voice._supervisor import (
    NEEDS_ATTENTION_CUE_SLUG,
    NETWORK_DOWN_ATTEMPTS,
    NETWORK_DOWN_CUE_SLUG,
    OUT_OF_CREDIT_CUE_SLUG,
    OutageTracker,
    is_network_down,
    is_transient,
    outage_cue,
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


class _Coded:
    """A google-genai ``APIError``: the code is on ``.code``, never
    ``.status_code``."""

    def __init__(self, code: int) -> None:
        self.code = code


class _StatusAndCode(_Coded):
    def __init__(self, status_code: int, code: int) -> None:
        super().__init__(code)
        self.status_code = status_code


def _provider_closed(code: int) -> ConnectionClosedError:
    """A real close the provider opened: it sent ``code``, we echoed it."""
    close = Close(code, "")
    return ConnectionClosedError(close, close, rcvd_then_sent=True)


def _we_closed(code: int) -> ConnectionClosedError:
    """A real close OUR client opened — a truncated text frame from the
    provider's edge rejected with 1007, say — which the server echoed
    back as RFC 6455 requires. Both frames carry the same code, so
    ``.rcvd.code`` alone cannot tell this apart from a rejection."""
    close = Close(code, "")
    return ConnectionClosedError(close, close, rcvd_then_sent=False)


def _closed_abnormally() -> ConnectionClosedError:
    """A real close that exchanged no close frame in either direction."""
    return ConnectionClosedError(None, None, rcvd_then_sent=None)


# Captured verbatim from the live xAI 403 that motivated ADR-0215.
_CREDIT_BODY = (
    b'{"error":"Your team has either used all available credits or reached'
    b' its monthly spending limit."}'
)


def _dns() -> OSError:
    """The shape a DNS failure reaches the supervisor as in production."""
    return OSError(socket.EAI_AGAIN, "Temporary failure in name resolution")


def _wrapped(inner: BaseException) -> Exception:
    """The shape an SDK raises when it re-raises `from` a network error."""
    outer = RuntimeError("sdk connect failed")
    outer.__cause__ = inner
    return outer


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
        (_Coded(1002), False),
        (_Coded(1003), False),
        (_Coded(1007), False),
        (_Coded(1008), True),
        (_provider_closed(1007), False),
        (_we_closed(1007), True),
        (_we_closed(1002), True),
        (_provider_closed(1011), True),
        (_closed_abnormally(), True),
        (_Coded(400), False),
        (_Coded(409), True),
        (_Coded(429), True),
        (_Coded(503), True),
        (_StatusAndCode(500, 1007), True),
    ],
)
def test_is_transient_classifies_by_who_must_act(
    exc: object, transient: bool,
) -> None:
    """Terminal means a human must act; everything else retries in silence."""
    assert is_transient(exc) is transient  # type: ignore[arg-type]


def test_is_transient_never_reads_the_deprecated_close_code() -> None:
    """The RFC 6455 code must come from ``.rcvd.code``: reading the
    deprecated ``ConnectionClosed.code`` property emits a
    ``DeprecationWarning`` on every access, so a real close still has to
    classify without ever touching it."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert is_transient(_provider_closed(1007)) is False
        assert is_transient(_closed_abnormally()) is True


# ---------------------------------------------------------------------------
# is_network_down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "down"),
    [
        (_dns(), True),
        (OSError(socket.EAI_NONAME, "Name or service not known"), True),
        (OSError(errno.ENETUNREACH, "Network is unreachable"), True),
        (_wrapped(_dns()), True),
        (
            OSError(
                "Multiple exceptions: "
                f"[Errno {errno.ENETUNREACH}] Connect call failed ('1.2.3.4', 443), "
                f"[Errno {errno.ENETUNREACH}] Connect call failed ('::1', 443)"
            ),
            True,
        ),
        (ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), False),
        (OSError("no errno"), False),
        (TimeoutError(), False),
        (_Rejected(500, b""), False),
    ],
    ids=[
        "dns-eai-again",
        "dns-eai-noname",
        "no-route",
        "wrapped-by-sdk",
        "dual-stack-folded-by-asyncio",
        "refused",
        "errno-less",
        "timeout",
        "server-error",
    ],
)
def test_is_network_down_only_for_a_missing_link(
    exc: BaseException, down: bool,
) -> None:
    """Only a DNS or routing errno means the household's own link is the
    fault; a refusal, a reset or a timeout is an ordinary blip."""
    assert is_network_down(exc) is down


# ---------------------------------------------------------------------------
# outage_cue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "cue"),
    [
        (_Rejected(403, b""), NEEDS_ATTENTION_CUE_SLUG),
        (_Rejected(403, _CREDIT_BODY), OUT_OF_CREDIT_CUE_SLUG),
        (_Rejected(401, b""), NEEDS_ATTENTION_CUE_SLUG),
        (_Rejected(429, b""), None),
        (OSError("network blip"), None),
        (_dns(), NETWORK_DOWN_CUE_SLUG),
        (ValueError("bad config"), NEEDS_ATTENTION_CUE_SLUG),
        (_provider_closed(1007), NEEDS_ATTENTION_CUE_SLUG),
        (_we_closed(1007), None),
    ],
    ids=[
        "rejected-no-body",
        "rejected-out-of-credit",
        "bad-key",
        "rate-limited",
        "network-blip",
        "network-down",
        "local-config",
        "provider-refused-our-setup",
        "our-own-close-echoed-back",
    ],
)
def test_outage_cue_names_the_remedy(
    exc: BaseException, cue: str | None,
) -> None:
    """The provider's own rejection text picks between the two terminal
    cues, a missing link names the network, and an ordinary transient
    failure names no remedy at all."""
    assert outage_cue(exc) == cue


def test_outage_cue_scans_past_the_display_limit() -> None:
    """Classification reads the whole rejection body, not the clipped
    string `/state` shows: a marker beyond FAILURE_DETAIL_LIMIT still
    names the remedy."""
    body = b"x" * 400 + b' "used all available credits"'
    assert outage_cue(_Rejected(403, body)) == OUT_OF_CREDIT_CUE_SLUG


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
        ([_Terminal()], [NEEDS_ATTENTION_CUE_SLUG]),
        ([OSError("blip"), OSError("blip again")], []),
        ([_Terminal(), _Terminal()], [NEEDS_ATTENTION_CUE_SLUG]),
        ([_Terminal(), _OtherTerminal()], [NEEDS_ATTENTION_CUE_SLUG]),
        (
            [_Terminal(), _Rejected(403, _CREDIT_BODY)],
            [NEEDS_ATTENTION_CUE_SLUG, OUT_OF_CREDIT_CUE_SLUG],
        ),
        (
            [_Rejected(403, _CREDIT_BODY), _Rejected(403, _CREDIT_BODY)],
            [OUT_OF_CREDIT_CUE_SLUG],
        ),
        ([_Terminal(), None, _Terminal()], [NEEDS_ATTENTION_CUE_SLUG] * 2),
        ([_Terminal(), OSError("blip"), _Terminal()], [NEEDS_ATTENTION_CUE_SLUG]),
        ([_dns()] * (NETWORK_DOWN_ATTEMPTS - 1), []),
        ([_dns()] * NETWORK_DOWN_ATTEMPTS, [NETWORK_DOWN_CUE_SLUG]),
        (
            [*[_dns()] * NETWORK_DOWN_ATTEMPTS, None,
             *[_dns()] * NETWORK_DOWN_ATTEMPTS],
            [NETWORK_DOWN_CUE_SLUG] * 2,
        ),
        (
            [*[_dns()] * NETWORK_DOWN_ATTEMPTS, _Terminal()],
            [NETWORK_DOWN_CUE_SLUG, NEEDS_ATTENTION_CUE_SLUG],
        ),
        ([_dns(), _dns(), _Terminal()], [NEEDS_ATTENTION_CUE_SLUG]),
    ],
    ids=[
        "terminal-speaks",
        "transient-silent",
        "same-outage-speaks-once",
        "same-remedy-different-status-speaks-once",
        "changed-remedy-mid-outage-re-announces",
        "same-remedy-twice-speaks-once",
        "recovery-rearms",
        "transient-mid-outage-does-not-rearm",
        "dropped-link-retries-in-silence",
        "network-down-speaks-on-the-threshold",
        "recovery-rearms-the-network-cue",
        "wifi-back-into-a-blocked-account-speaks-both",
        "network-below-threshold-never-spoke",
    ],
)
async def test_escalation_speaks_once_per_remedy_per_outage(
    events: list[object], expected: list[str],
) -> None:
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    tracker = OutageTracker()
    tracker.set_callback(cb)
    await _drive(tracker, events)
    assert calls == expected


async def test_cue_tracks_the_latest_failure() -> None:
    """`cue` is what the wake path would play right now: the current
    remedy, and nothing once a retry might still fix it. The network
    cue's announcement debounce delays the proactive cue, not this
    answer — `wake_cue` names the network on the very first failure."""
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    tracker = OutageTracker()
    tracker.set_callback(cb)
    await _drive(tracker, [_Terminal()])
    assert tracker.cue == NEEDS_ATTENTION_CUE_SLUG

    await _drive(tracker, [OSError("blip")])
    assert tracker.cue is None

    await _drive(tracker, [_dns()])
    assert tracker.wake_cue == NETWORK_DOWN_CUE_SLUG
    assert calls == [NEEDS_ATTENTION_CUE_SLUG]

    await _drive(tracker, [_Rejected(403, _CREDIT_BODY), None])
    assert tracker.cue is None
    assert tracker.detail is None


async def test_a_transient_failure_restarts_the_network_streak() -> None:
    """Only *consecutive* network failures reach the threshold, so a
    link that flaps back to an ordinary error has to earn it again."""
    calls: list[str] = []

    async def cb(slug: str) -> None:
        calls.append(slug)

    tracker = OutageTracker()
    tracker.set_callback(cb)
    await _drive(tracker, [_dns(), RuntimeError("reset"), *[_dns()] * 3])
    assert calls == []

    await _drive(tracker, [_dns()])
    assert calls == [NETWORK_DOWN_CUE_SLUG]


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
        rotate_after_sec=0.0,
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
        assert cue_calls == [NEEDS_ATTENTION_CUE_SLUG]
        assert conn.last_failure_detail() is None
    finally:
        await conn.stop()
