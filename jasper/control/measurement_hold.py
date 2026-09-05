# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-control's copy of the open measurement window — the third lease.

``jasper.measurement_window.measurement_window()`` is the one writer of
"a measurement is live". It already holds two self-expiring copies of that
fact — jasper-voice's ``_measurement_active`` and jasper-mux's diagnostic-gate
lease — and this module is the third: the copy jasper-control needs so it can
stop applying **source-observed** volume changes (a host moving its USB
slider) into the fader a measurement is holding.

Why the fact has to live HERE and not in the volume coordinator:
``jasper.control.volume_ops._with_coordinator`` builds a **fresh**
``VolumeCoordinator`` per HTTP request and disposes it in ``finally``, so no
in-memory measurement state can survive between two requests there. This
module is process-scoped instead — the same lifetime as jasper-control
itself, and the same shape as ``server._grouping_reconciler_kick_coalescer``.
``jasper/volume_coordinator.py`` is deliberately untouched by this design.

Contract:

* One hold at a time, **owner-scoped**. A second owner's acquire is refused
  (the route turns that into a 409 naming the incumbent), which is what makes
  this the cross-process mutex ``measurement_window``'s in-process
  ``_window_active`` flag cannot be.
* Acquire is **idempotent for the incumbent** and doubles as renew — the same
  shape as jasper-mux's ``TEST_SELECT``, so the window's refresh loop simply
  re-issues it. The registrar still distinguishes the two in its logs
  (``hold_acquired`` vs ``hold_renewed``).
* The hold **self-expires** after :data:`MEASUREMENT_HOLD_TTL_SEC`. There is no
  reaper: expiry is evaluated lazily on every read, so a holder that is
  ``kill -9``'d frees the speaker with no process surviving to clean up. The
  window renews every ``measurement_window.MEASUREMENT_LEASE_REFRESH_SEC``
  (60 s), which leaves room for one failed renewal plus a retry inside the
  120 s TTL;
  ``tests/test_measurement_hold.py`` pins that arithmetic.
* Nothing is persisted. A reboot mid-measurement drops every copy, which is
  the intended crash-safety property — see the design brief's §4. Closing the
  ``<= 60 s`` restart hole (a jasper-control restart drops its copy until the
  next renewal) needs a ``/run`` deadline file and is deliberately deferred.

Modes: only the modes whose behaviour is implemented are accepted. Today that
is ``gate`` alone — voice keeps running and drops mic frames in-process. The
``evict`` mode (stop jasper-voice so its UDP sockets free, for the wake-corpus
recorder) is a later change and will add itself to
:data:`MEASUREMENT_HOLD_MODES` together with the behaviour, so this surface
can never advertise a mode it does not honour.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ..log_event import log_event

logger = logging.getLogger(__name__)

__all__ = [
    "MEASUREMENT_HOLD_MODES",
    "MEASUREMENT_HOLD_TTL_SEC",
    "MeasurementHold",
    "MeasurementHoldConflict",
    "MeasurementHoldRequestError",
    "acquire",
    "held",
    "read_measurement_hold",
    "record_declined_observation",
    "release",
    "reset_for_tests",
    "snapshot",
]

# How long one acquire keeps the hold alive without a renewal. Mirrors
# voice.measurement_hold.MEASUREMENT_AUTOCLEAR_SEC. Must stay comfortably
# above the window's renewal cadence
# (measurement_window.MEASUREMENT_LEASE_REFRESH_SEC) with room for one failed
# renewal and its MEASUREMENT_LEASE_RETRY_SEC back-off; a test pins the triple.
MEASUREMENT_HOLD_TTL_SEC = 120.0

# The isolation modes this registrar honours. See the module docstring: a mode
# is only listed once its behaviour exists.
MEASUREMENT_HOLD_MODES = frozenset({"gate"})


class MeasurementHoldConflict(RuntimeError):
    """Another owner holds the live measurement hold.

    Carries the incumbent's name so the HTTP layer can name it in the 409 and
    an operator learns WHICH measurement is in the way, not merely that one is.
    """

    def __init__(self, owner: str) -> None:
        super().__init__(
            f"a measurement is already in progress (owner={owner})"
        )
        self.owner = owner


class MeasurementHoldRequestError(ValueError):
    """The request itself is malformed — a nameless owner, or an isolation
    mode this registrar does not honour. Distinct from
    :class:`MeasurementHoldConflict`, which means the request was well-formed
    and somebody else holds the speaker; the HTTP layer maps this to 400 and
    that to 409.
    """


class MeasurementHold:
    """The process-scoped registrar. See the module docstring for the contract.

    ``clock`` is injectable so tests can pin TTL arithmetic without sleeping.
    Guarded by a lock because jasper-control serves requests on a
    ``ThreadingHTTPServer`` worker pool: acquire/release and the ``/state``
    read genuinely race.
    """

    def __init__(
        self,
        *,
        ttl_s: float = MEASUREMENT_HOLD_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._owner: str | None = None
        self._mode: str | None = None
        self._acquired_at: float = 0.0
        self._expires_at: float = 0.0
        # Declined source-observed volume writes against the CURRENT hold. Only
        # its first-or-not-ness is used (see record_declined_observation), and
        # it belongs here rather than in the HTTP handler because "the current
        # hold" is this object's lifecycle: it resets on a fresh acquire and on
        # every path that drops the hold, and a handler is per-request.
        self._declines: int = 0

    def reset_for_tests(self) -> None:
        with self._lock:
            self._clear_locked()

    def _clear_locked(self) -> None:
        """Drop the hold and everything scoped to it. Caller holds the lock."""
        self._owner = None
        self._mode = None
        self._acquired_at = 0.0
        self._expires_at = 0.0
        self._declines = 0

    # -- internals -------------------------------------------------------

    def _expire_locked(self, now: float) -> None:
        """Drop a lapsed hold. Caller holds the lock.

        This is the whole crash-recovery story: no reaper thread, no timer, no
        file. Every read runs it first, so a holder that died without
        releasing stops blocking the speaker the moment anybody looks.
        """
        if self._owner is None or now < self._expires_at:
            return
        expired_owner = self._owner
        expired_mode = self._mode
        held_for = now - self._acquired_at
        declines = self._declines
        self._clear_locked()
        log_event(
            logger,
            "measurement.hold_expired",
            declined_observations=declines,
            owner=expired_owner,
            mode=expired_mode,
            held_for_s=f"{held_for:.1f}",
            ttl_s=f"{self._ttl_s:.1f}",
            level=logging.WARNING,
        )

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        if self._owner is None:
            return {
                "active": False,
                "owner": None,
                "mode": None,
                "expires_in_s": None,
                "held_for_s": None,
            }
        return {
            "active": True,
            "owner": self._owner,
            "mode": self._mode,
            "expires_in_s": round(max(0.0, self._expires_at - now), 1),
            # Not cosmetic, and not derivable from `expires_in_s`: that value
            # resets on every renewal, so only this one can answer "has a hold
            # been held far past any plausible window?" — which is exactly what
            # jasper-doctor's check_measurement_hold asks.
            "held_for_s": round(max(0.0, now - self._acquired_at), 1),
        }

    # -- public surface --------------------------------------------------

    def acquire(self, owner: str, mode: str = "gate") -> dict[str, Any]:
        """Take the hold, or renew it when ``owner`` already holds it.

        Raises :class:`MeasurementHoldConflict` when a different, unexpired
        owner holds it, and :class:`MeasurementHoldRequestError` for a nameless
        owner or a mode this registrar does not honour.
        """
        owner = str(owner).strip()
        if not owner:
            raise MeasurementHoldRequestError("owner must be a non-empty string")
        mode = str(mode).strip()
        if mode not in MEASUREMENT_HOLD_MODES:
            raise MeasurementHoldRequestError(
                f"mode must be one of {sorted(MEASUREMENT_HOLD_MODES)}, "
                f"got {mode!r}"
            )
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if self._owner is not None and self._owner != owner:
                raise MeasurementHoldConflict(self._owner)
            renewed = self._owner == owner
            if not renewed:
                self._acquired_at = now
                # `_declines` deliberately is NOT reset here. It cannot be
                # non-zero at this point: it only counts while a hold is live,
                # a fresh acquire only happens when `_owner is None`, and EVERY
                # path that clears `_owner` goes through `_clear_locked`, which
                # zeroes it. Resetting again would be an unreachable line that
                # reads like a guard. What must never reset it is a RENEWAL — a
                # 30-minute session renews ~30 times and is still one hold, so
                # exactly one INFO line — and that falls out of this branch not
                # running.
            self._owner = owner
            self._mode = mode
            self._expires_at = now + self._ttl_s
            state = self._snapshot_locked(now)
        log_event(
            logger,
            "measurement.hold_renewed" if renewed else "measurement.hold_acquired",
            owner=owner,
            mode=mode,
            ttl_s=f"{self._ttl_s:.1f}",
            held_for_s=f"{state['held_for_s']:.1f}",
            level=logging.DEBUG if renewed else logging.INFO,
        )
        return state

    def release(self, owner: str) -> dict[str, Any]:
        """Release ``owner``'s hold.

        Releasing when nothing is held is a no-op success — a holder whose TTL
        already lapsed must still be able to finish its teardown cleanly.
        Releasing somebody else's hold raises :class:`MeasurementHoldConflict`:
        owner-scoped release is what stops one measurement's cleanup from
        un-gating another's live capture (the same rule jasper-mux applies to
        ``TEST_RELEASE``).
        """
        owner = str(owner).strip()
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if self._owner is None:
                return self._snapshot_locked(now)
            if self._owner != owner:
                raise MeasurementHoldConflict(self._owner)
            mode = self._mode
            held_for = now - self._acquired_at
            declines = self._declines
            self._clear_locked()
            state = self._snapshot_locked(now)
        log_event(
            logger,
            "measurement.hold_released",
            owner=owner,
            mode=mode,
            held_for_s=f"{held_for:.1f}",
            # The count the per-hold DEBUG demotion below suppressed, reported
            # once at the end so the volume the journal did NOT carry is still
            # a number an operator can see.
            declined_observations=declines,
        )
        return state

    def owner(self) -> str | None:
        """The incumbent's name, or ``None`` when nothing is held.

        Both the predicate and the name in one locked read: the volume decline
        needs both, and asking twice could name an owner that lapsed in
        between. :meth:`held` is the readable alias, not a second reader.
        """
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            return self._owner

    def held(self) -> bool:
        """True while an unexpired hold is live."""
        return self.owner() is not None

    def record_declined_observation(self) -> tuple[str, bool] | None:
        """Count one declined source-observed volume write against this hold.

        Returns ``(owner, is_first)`` while a hold is live, or ``None`` when
        nothing is held — so the caller's "should I decline?" and "how loudly
        should I say so?" are answered by ONE locked read. Deliberately a
        command and a query at once: the two must not be separable, or a hold
        that lapsed between them would name a stale owner or mis-rank the line.

        ``is_first`` exists because the transition is the signal and the
        repetition is not. The declined caller is the USB volume bridge, which
        re-presents the host's slider on a backoff schedule for as long as the
        decline lasts — up to ~720 posts/hour against the bridge's 5 s ceiling
        (``usbsink.volume_bridge.POST_RETRY_CEILING_SEC``), i.e. ~360 lines in
        one 30-minute measurement, which is this feature's ORDINARY case rather
        than an edge one. Logging every one at INFO would re-create the journal
        spam #2791 removed one commit earlier. The count is not lost: the
        release/expiry lines report it as ``declined_observations``.
        """
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            if self._owner is None:
                return None
            self._declines += 1
            return self._owner, self._declines == 1

    def snapshot(self) -> dict[str, Any]:
        """The ``/state.measurement`` projection."""
        with self._lock:
            now = self._clock()
            self._expire_locked(now)
            return self._snapshot_locked(now)


# The process-scoped singleton. Module-level for the same reason
# server._grouping_reconciler_kick_coalescer is: jasper-control's Handler is
# instantiated per request, so cross-request state cannot live on it.
_hold = MeasurementHold()


def acquire(owner: str, mode: str = "gate") -> dict[str, Any]:
    return _hold.acquire(owner, mode)


def release(owner: str) -> dict[str, Any]:
    return _hold.release(owner)


def held() -> bool:
    return _hold.held()


def owner() -> str | None:
    return _hold.owner()


def record_declined_observation() -> tuple[str, bool] | None:
    return _hold.record_declined_observation()


def snapshot() -> dict[str, Any]:
    return _hold.snapshot()


def reset_for_tests() -> None:
    _hold.reset_for_tests()


def read_measurement_hold() -> dict[str, Any] | None:
    """The hold as seen from ANOTHER process, or ``None`` when unreadable.

    The read half of this module: every process except jasper-control asks over
    HTTP, and it lives beside the registrar so one module owns the fact on both
    sides rather than each caller re-deriving a route literal and an error
    policy.

    ``None`` means "jasper-control could not be asked" and is deliberately
    distinct from ``{"active": False}`` ("asked, and nothing is held") —
    callers that must degrade conservatively can only do so if those two are
    not conflated. The client import is lazy so importing this module inside
    jasper-control stays free of it.
    """
    from .client import ControlError, get_measurement

    try:
        hold = get_measurement()
    except (ControlError, ValueError, UnicodeDecodeError):
        return None
    return hold or None
