"""Provider-agnostic helpers for the voice connection reconnect supervisor.

Each `LiveConnection` implementation runs its own reconnect supervisor
because the recovery details differ (Gemini drops a resumption handle on
1008; OpenAI just reopens the WebSocket). The retry-loop primitives —
backoff schedule, jitter, tight-retry-loop escalation, failure-shape
fingerprint comparison — are provider-agnostic and live here so behaviour
stays consistent across Gemini Live, OpenAI Realtime, and any future
addition.

What's NOT here: the supervisor task itself, exception classification
(409 / 1008 / etc.), and resumption-handle logic. Those are
provider-specific and stay in `gemini_session.py` / `openai_session.py`.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable


# Reconnect backoff: never give up. A smart speaker that goes
# permanently silent after a 15-second DNS blip is broken UX —
# the user has no way to know recovery requires a manual restart,
# so they just stop using the device. We retry forever, with
# exponential backoff capped at RECONNECT_MAX_BACKOFF_SEC and
# ±25% jitter so we don't hammer the API or accidentally
# self-synchronise with other speakers on the same outage.
#
# Schedule (jitter omitted): 1, 2, 4, 8, 16, 32, 60, 60, 60, …
RECONNECT_INITIAL_BACKOFF_SEC = 1.0
RECONNECT_MAX_BACKOFF_SEC = 60.0
RECONNECT_BACKOFF_JITTER_FRACTION = 0.25


# Tight-retry-loop escalation: when the supervisor reconnect loop keeps
# producing the SAME failure (same exception type, same close code, same
# reason text) in succession, that's a signal the user should know about
# — the speaker is broken and silent retries won't fix it on their own.
# Threshold of 5 was picked because the default backoff schedule is
# (1, 2, 4, 8, 16, 32, 60, 60…) seconds — 5 attempts ≈ 30 s of sustained
# identical failures before the cue. By that point we're well past
# transient-blip territory (DNS hiccup, momentary WS reset, etc.) and
# into real-outage territory. Rate-limited to once per hour to avoid
# spamming during long outages.
ESCALATION_REPEAT_THRESHOLD = 5
ESCALATION_RATE_LIMIT_SEC = 3600.0
ESCALATION_CUE_SLUG = "cant_reach_cloud"


def reconnect_backoff_delay(attempt: int) -> float:
    """Exponential delay with jitter for the supervisor reconnect loop.
    `attempt` is 1-indexed (first retry is attempt 1).

    The shift is saturated at 32 because Python ints don't overflow but
    `float(2 ** 1024)` does — and the supervisor retries forever, so
    we *will* see attempt > 1024 in a long outage (an early-2026 incident
    hit 798). Once `2 ** shift` exceeds the max-backoff/initial ratio
    the outer ``min()`` clamps it anyway, so the saturation is purely
    a numeric safety bound."""
    shift = min(attempt - 1, 32)
    base = min(
        RECONNECT_INITIAL_BACKOFF_SEC * (2 ** shift),
        RECONNECT_MAX_BACKOFF_SEC,
    )
    j = base * RECONNECT_BACKOFF_JITTER_FRACTION
    return base + random.uniform(-j, j)


@dataclass(frozen=True)
class FailureFingerprint:
    """Identity of a reconnect failure, for tight-loop detection.

    Two fingerprints compare equal iff they're the same shape of
    failure: same exception type, same WebSocket close code (if any),
    same reason text. Reason is truncated to 200 chars so jittery error
    messages with timestamps or other unique content don't pollute the
    "are these all identical?" check; the exception type + close code
    do most of the work anyway."""
    exc_type: str
    close_code: int | None
    reason: str

    @classmethod
    def from_exception(cls, exc: BaseException) -> "FailureFingerprint":
        # WebSocket exceptions from the underlying `websockets` library
        # carry the close frame on `.rcvd`. The `openai` SDK raises its
        # own typed errors with `.code` / `.reason`. Other exception
        # shapes (httpx errors, generic OSError) won't have either; fall
        # back to str(exc) for the reason field.
        rcvd = getattr(exc, "rcvd", None)
        close_code = (
            getattr(rcvd, "code", None)
            if rcvd is not None
            else getattr(exc, "code", None)
        )
        reason = (
            getattr(rcvd, "reason", None)
            if rcvd is not None
            else getattr(exc, "reason", None)
        )
        if reason is None:
            reason = str(exc)
        return cls(
            exc_type=type(exc).__name__,
            close_code=close_code,
            reason=str(reason)[:200],
        )


class DeferredReconnect:
    """Defer a mid-turn reconnect until the in-flight turn releases.

    Both real-time providers need to reconnect *during* a session but
    must not tear down the WebSocket while a turn is actively replying —
    doing so cuts the user off mid-sentence. The shared MECHANISM is a
    pending flag set by some trigger and fired from ``_on_turn_released``
    once the turn ends. Only the TRIGGER differs per provider:

      * OpenAI: the proactive pre-cap watchdog fires inside the 5-minute
        buffer before the 60-minute hard cap.
      * Gemini: a ``GoAway`` lands mid-turn with ample ``time_left``.

    This class owns the flag and its lifecycle so a future fourth
    provider reuses it instead of re-deriving the same three-state dance
    (request → defer → fire-on-release, plus clear-when-reconnect-starts
    so a later turn release can't fire a spurious second reconnect)."""

    def __init__(self) -> None:
        self._pending = False

    @property
    def pending(self) -> bool:
        """Whether a reconnect is currently deferred."""
        return self._pending

    def request(self) -> None:
        """A trigger fired mid-turn: mark a reconnect as deferred."""
        self._pending = True

    def clear(self) -> None:
        """A reconnect is now underway: drop any deferred request so a
        later turn release cannot fire a spurious second reconnect."""
        self._pending = False

    def fire_if_pending(self, fire: Callable[[], object]) -> bool:
        """Fire a deferred reconnect, if one is pending.

        Called from ``_on_turn_released``. If a reconnect was deferred,
        clear the flag, invoke ``fire`` (which sets the connection's
        reconnect event), and return ``True`` so the caller can log. The
        flag is cleared BEFORE ``fire`` so a re-entrant turn release
        can't double-fire. Returns ``False`` (and does nothing) when no
        reconnect is pending."""
        if not self._pending:
            return False
        self._pending = False
        fire()
        return True
