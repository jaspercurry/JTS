# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-window coordinator.

`measurement_window()` is an async context manager that pauses everything that
would interfere with a clean room measurement, then restores on exit: mux's
diagnostic gate (every music lane), voice_daemon's WakeLoop plus outputd's
content meter via MEASURE_PAUSE, and jasper-control's source-observed volume
writes. CamillaDSP is NOT paused — the sweep must take the same DSP path music
takes. Each enforcement point keeps a self-expiring lease of its own.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

from ..control.uds import _mux_socket_command
from ..log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_VOICE_SOCKET_PATH = "/run/jasper/voice.sock"
# Renew the voice-side crash-recovery timer while a window is open; a relay
# setup may wait up to eight minutes for a human. Must stay under the daemon's
# auto-clear with room for a retry, and is also the volume hold's cadence.
MEASUREMENT_LEASE_REFRESH_SEC = 60.0
# Back-off before re-trying a failed lease acquire/renewal. One policy shared
# by all three leases an open window holds; retuning it is budgeted three times.
MEASUREMENT_LEASE_RETRY_SEC = 5.0
# Deadline for the daemon's MEASURE_PAUSE reply — a contract, not a local
# timeout: the daemon may hold it while draining in-flight assistant audio, so
# voice_daemon.MEASUREMENT_INFLIGHT_DRAIN_SEC must stay under this value.
VOICE_MEASURE_PAUSE_TIMEOUT_SEC = 3.0
MEASUREMENT_FANIN_LABEL = "correction"
MEASUREMENT_GATE_OWNER = "correction-measurement"
MEASUREMENT_GATE_REFRESH_SEC = 20.0
# Inter-attempt sleep for _release_measurement_gate's confirm loop on the way
# OUT, not the refresh loop above (which backs off on MEASUREMENT_LEASE_RETRY_SEC).
MEASUREMENT_GATE_RETRY_SEC = 0.1
# Deadline for one jasper-mux control round trip: connect, send, reply, close,
# so a wedged UDS cannot outlive it while mux's lease ages. NOT the voice
# daemon's VOICE_MEASURE_PAUSE_TIMEOUT_SEC, which merely shares a value today.
MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC = 3.0
# Abort the owning window before mux's availability lease
# (mux.FANIN_TEST_LEASE_SEC) can expire and reopen music into a live sweep. mux
# stamps its expiry before replying, so what has to clear that lease counts
# MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC twice.
MEASUREMENT_GATE_ABORT_SEC = 40.0
# jasper-control's volume-observation hold — the window's THIRD lease, stopping
# source-observed volume writes reaching the fader a measurement holds.
MEASUREMENT_HOLD_PATH = "/measurement/hold"
MEASUREMENT_HOLD_RELEASE_PATH = "/measurement/release"
# One jasper-control round trip. Loopback HTTP with no pooling, so this is a
# wedged-daemon bound, not a latency budget.
MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC = 3.0
# The isolation mode declared to jasper-control; only `gate` exists today. See
# jasper.control.measurement_hold.MEASUREMENT_HOLD_MODES.
MEASUREMENT_HOLD_MODE = "gate"

# Mutual-exclusion flag for measurement_window(): only one window may be open
# at a time. Per-PROCESS, guarded by a plain check-and-set before the first
# await. NOT an asyncio.Lock: a Lock binds to one loop and would break the
# per-test asyncio.run() loops. Cross-process exclusion is jasper-control's
# measurement hold.
_window_active = False


class MeasurementWindowError(RuntimeError):
    """A precondition failed or isolation could not be proven/restored."""


class MeasurementAbortTarget:
    """Redirectable cancel target for the gate-lease abort (held windows).

    A flow that holds one window for a whole multi-capture session enters it
    from its session task while each play runs as its OWN task, so the default
    "cancel the entering task" would not stop the in-flight sweep. Such a
    holder ``register()``s the current play task and ``clear()``s it after; on
    a renew failure :meth:`abort` latches ``failed`` and cancels it.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self.failed = False

    def register(self, task: asyncio.Task) -> None:
        self._task = task

    def clear(self) -> None:
        self._task = None

    def abort(self, fallback: asyncio.Task | None) -> None:
        self.failed = True
        task = self._task if self._task is not None else fallback
        if task is not None:
            task.cancel()


def _measurement_gate_held(
    payload: object,
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
) -> bool:
    if not isinstance(payload, Mapping):
        return False
    return (
        payload.get("test_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("active_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("test_owner") == gate_owner
    )


async def _acquire_measurement_gate(
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
) -> None:
    """Ask mux to exclude every music lane and verify the landed state."""

    try:
        payload = await _mux_socket_command(
            "TEST_SELECT "
            f"{MEASUREMENT_FANIN_LABEL} {gate_owner}",
            timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        raise MeasurementWindowError(
            f"Could not isolate the measurement lane: {exc}"
        ) from exc
    if not _measurement_gate_held(payload, gate_owner=gate_owner):
        raise MeasurementWindowError(
            "Mux did not confirm the isolated measurement lane."
        )
    log_event(
        logger,
        "correction.measurement_gate",
        action="acquire",
        owner=gate_owner,
        result="ok",
    )


async def _release_measurement_gate(
    *,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
    allow_other_owner: bool = False,
) -> None:
    """Release our mux gate, retrying and requiring owner-aware landed state.

    ``allow_other_owner`` is only for cleanup after an indeterminate acquire.
    It permits a concurrently held commissioning gate to remain untouched.
    """

    last_error = "mux did not confirm release"
    for attempt in range(3):
        try:
            payload = await _mux_socket_command(
                f"TEST_RELEASE {gate_owner}",
                timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
            )
            if isinstance(payload, Mapping) and (
                payload.get("test_source") is None
                and payload.get("test_owner") is None
            ):
                log_event(
                    logger,
                    "correction.measurement_gate",
                    action="release",
                    owner=gate_owner,
                    result="ok",
                )
                return
            last_error = "mux still reports a selected test lane"
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            UnicodeError,
            asyncio.TimeoutError,
        ) as exc:
            last_error = str(exc)
            # A lost RELEASE response may still have landed. STATUS also lets
            # cleanup tell another feature's owner apart without releasing it.
            try:
                status = await _mux_socket_command(
                    "STATUS", timeout=MEASUREMENT_GATE_COMMAND_TIMEOUT_SEC,
                )
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                UnicodeError,
                asyncio.TimeoutError,
            ):
                status = None
            if isinstance(status, Mapping):
                owner = status.get("test_owner")
                if owner is None and status.get("test_source") is None:
                    return
                if allow_other_owner and owner != gate_owner:
                    return
        if attempt < 2:
            await asyncio.sleep(MEASUREMENT_GATE_RETRY_SEC)
    log_event(
        logger,
        "correction.measurement_gate",
        action="release",
        owner=gate_owner,
        result="failed",
        reason=last_error,
        level=logging.ERROR,
    )
    raise MeasurementWindowError(
        f"Could not release the isolated measurement lane: {last_error}"
    )


# A module attribute on purpose: tests monkeypatch it the way they monkeypatch
# `_mux_socket_command`.
async def _measurement_hold_command(path: str, body: dict) -> tuple[int, dict]:
    """One POST to jasper-control's measurement-hold surface.

    Returns ``(status, decoded-body)``; transport failures propagate and the
    callers own the policy. The control token is read from this box's own token
    file and presented as ``X-JTS-Token``; ``current_token()`` returns ``""``
    when the gate is off or the file is unreadable, and the header is omitted.
    """
    from ..control import control_token
    from ..control.client import AsyncControlClient

    headers: dict[str, str] = {}
    token = control_token.current_token()
    if token:
        headers["X-JTS-Token"] = token
    client = AsyncControlClient(timeout=MEASUREMENT_HOLD_COMMAND_TIMEOUT_SEC)
    response = await client.post(path, body, headers=headers or None)
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        payload = None
    return response.status, payload if isinstance(payload, dict) else {}


async def _acquire_measurement_hold(owner: str) -> bool:
    """Take (or renew) jasper-control's volume-observation hold.

    Returns True when the hold is ours, False when it could not be taken.
    Fail-soft on unreachable — the only writer this lease holds back is served
    BY jasper-control, so a down daemon cannot cause the hazard — and
    fail-CLOSED on 409, which is a different measurement holding the speaker
    and the cross-process mutex ``_window_active`` cannot be. False means "not
    yet": the caller's refresh task retries unconditionally.
    """
    try:
        status, payload = await _measurement_hold_command(
            MEASUREMENT_HOLD_PATH,
            {"owner": owner, "mode": MEASUREMENT_HOLD_MODE},
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        # RuntimeError covers control.client's ControlError without importing it.
        logger.warning(
            "jasper-control measurement hold unavailable (%s) — proceeding "
            "without it; host-slider volume observations are not held off",
            exc,
        )
        return False
    if status == 409:
        raise MeasurementWindowError(
            payload.get("error")
            or "a measurement is already in progress on this speaker"
        )
    if status < 200 or status >= 300:
        logger.warning(
            "jasper-control refused the measurement hold (HTTP %s: %s) — "
            "proceeding without it; host-slider volume observations are not "
            "held off",
            status,
            payload.get("error", ""),
        )
        return False
    return True


async def _release_measurement_hold(owner: str) -> None:
    """Release jasper-control's hold. Never raises.

    A failed release is recoverable without an operator: the hold self-expires
    after ``measurement_hold.MEASUREMENT_HOLD_TTL_SEC``. Same reasoning — and
    the same ERROR-log-and-move-on shape — as the ``MEASURE_RESUME`` failure
    path below, and deliberately unlike the mux gate, whose failed release
    leaves household music silent and therefore must be surfaced.
    """
    try:
        status, payload = await _measurement_hold_command(
            MEASUREMENT_HOLD_RELEASE_PATH, {"owner": owner},
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        UnicodeError,
        asyncio.TimeoutError,
    ) as exc:
        logger.error(
            "measurement hold release failed (%s) — jasper-control's %.0fs "
            "TTL will recover it",
            exc,
            _measurement_hold_ttl_sec(),
        )
        return
    if status < 200 or status >= 300:
        logger.error(
            "measurement hold release refused (HTTP %s: %s) — "
            "jasper-control's %.0fs TTL will recover it",
            status,
            payload.get("error", ""),
            _measurement_hold_ttl_sec(),
        )


async def _stop_lease_refresh(
    task: "asyncio.Task[None] | None", label: str,
) -> None:
    """Cancel one lease-renewal task and absorb whatever it died of.

    Renewal is resilience-only: a dead background task must never bypass
    MEASURE_RESUME, the mux-gate release, or the volume-hold release, so its
    failure is logged and swallowed rather than propagated out of ``finally``.
    One helper for all three leases — three literal copies of this drifted the
    moment a third lease arrived.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("%s refresh task failed", label)


def _measurement_hold_ttl_sec() -> float:
    """The registrar's TTL, read from its owner so the log cannot drift."""
    from ..control.measurement_hold import MEASUREMENT_HOLD_TTL_SEC

    return MEASUREMENT_HOLD_TTL_SEC


async def _voice_uds_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket, parse the JSON.

    Same wire format as jasper.control.server._voice_socket_command, not
    imported here to avoid a circular dependency.
    """
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        # asyncio.timeout(), NOT asyncio.wait_for(): on CPython <= 3.11 wait_for
        # swallows a CancelledError arriving in the same tick its future
        # completes, which would make _refresh_voice_lease immortal and wedge
        # window teardown (#1952). Do not "simplify" while 3.11 is supported.
        async with asyncio.timeout(timeout):
            line = await reader.readline()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError(f"voice_daemon returned no response for {cmd!r}")
    return json.loads(line.decode("utf-8"))


async def _check_no_active_voice_session(
    socket_path: str,
    *,
    require_voice_pause: bool = False,
) -> None:
    """Refuse to open a measurement window while a voice session is in progress.

    Raises MeasurementWindowError if a session is active. UDS-unreachable is
    treated as "voice daemon down, so no session".
    """
    try:
        status = await _voice_uds_command(socket_path, "STATUS", timeout=2.0)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
        if require_voice_pause:
            raise MeasurementWindowError(
                f"Could not verify voice is idle: {e}"
            ) from e
        logger.info(
            "voice daemon not reachable for STATUS check (%s) — "
            "assuming no active session",
            e,
        )
        return
    except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
        if require_voice_pause:
            raise MeasurementWindowError(
                f"Could not verify voice is idle: {e}"
            ) from e
        raise
    if require_voice_pause and (
        not isinstance(status, dict)
        or status.get("state") not in {"WAKE", "SESSION"}
    ):
        raise MeasurementWindowError(
            "Voice STATUS did not provide a trustworthy WAKE/SESSION state."
        )
    if status.get("state") == "SESSION":
        raise MeasurementWindowError(
            "Voice session is currently active. End it (or wait for it "
            "to end) before starting a measurement."
        )


@asynccontextmanager
async def measurement_window(
    *,
    voice_socket_path: str = DEFAULT_VOICE_SOCKET_PATH,
    skip_voice_pause: bool = False,
    skip_music_isolation: bool = False,
    abort_target: MeasurementAbortTarget | None = None,
    gate_owner: str = MEASUREMENT_GATE_OWNER,
    require_voice_pause: bool = False,
) -> AsyncIterator[None]:
    """Isolate fan-in's correction lane and pause voice, yield, restore.

    ``skip_voice_pause`` / ``skip_music_isolation`` are for tests.
    ``abort_target`` redirects the isolation-loss abort (see
    :class:`MeasurementAbortTarget`); ``None`` cancels the entering task.
    ``gate_owner`` must be an owner mux has registered. ``require_voice_pause``
    fails closed unless voice STATUS and MEASURE_PAUSE are both trustworthy.
    Raises MeasurementWindowError when a precondition fails or mux isolation
    cannot be proven or restored.
    """
    # Mutual exclusion: check-and-set BEFORE the first await so it is atomic on
    # the single background loop. A second concurrent window fails fast.
    global _window_active
    if require_voice_pause and skip_voice_pause:
        raise MeasurementWindowError(
            "strict voice isolation cannot be combined with skip_voice_pause"
        )
    if _window_active:
        raise MeasurementWindowError(
            "a measurement is already in progress; wait for the current "
            "sweep to finish or reset before starting another"
        )
    _window_active = True

    measurement_gate_cleanup_required = False
    measurement_gate_acquired = False
    measurement_gate_refresh_task: asyncio.Task[None] | None = None
    measurement_gate_lease_error: MeasurementWindowError | None = None
    hold_acquired = False
    hold_refresh_task: asyncio.Task[None] | None = None
    voice_paused = False
    voice_pause_cleanup_required = False
    lease_refresh_task: asyncio.Task[None] | None = None
    voice_lease_error: MeasurementWindowError | None = None
    measurement_owner_task = asyncio.current_task()

    try:
        # Precondition: no active voice session. Inside the try so the window
        # flag is cleared even when this raises.
        if not skip_voice_pause:
            await _check_no_active_voice_session(
                voice_socket_path,
                require_voice_pause=require_voice_pause,
            )

        # Taken BEFORE the mux gate and the voice pause so a conflicting second
        # measurement is refused before this window disturbs anything, and
        # released LAST. `gate_owner` doubles as the hold owner so ONE name
        # identifies this measurement across mux, jasper-control and /state.
        hold_acquired = await _acquire_measurement_hold(gate_owner)

        async def _refresh_measurement_hold() -> None:
            # Started UNCONDITIONALLY, including when the first acquire did not
            # land, because this loop both renews and RETRIES: jasper-control is
            # restarted by every deploy and is not socket-activated. A renewal
            # that cannot land retries on the shared back-off and does NOT clear
            # `hold_acquired` (a lost response may still have landed). A 409 is
            # the one definitive answer — a different owner holds it — so
            # renewal stops and cleanup responsibility is dropped.
            nonlocal hold_acquired
            delay = (
                MEASUREMENT_LEASE_REFRESH_SEC
                if hold_acquired
                else MEASUREMENT_LEASE_RETRY_SEC
            )
            while True:
                await asyncio.sleep(delay)
                try:
                    landed = await _acquire_measurement_hold(gate_owner)
                except MeasurementWindowError as exc:
                    logger.warning(
                        "measurement hold lost to another owner (%s); "
                        "stopping renewal", exc,
                    )
                    hold_acquired = False
                    return
                if landed:
                    hold_acquired = True
                delay = (
                    MEASUREMENT_LEASE_REFRESH_SEC
                    if landed
                    else MEASUREMENT_LEASE_RETRY_SEC
                )

        hold_refresh_task = asyncio.create_task(_refresh_measurement_hold())

        if not skip_music_isolation:
            # Gate first: even a renderer that races its stop cannot enter the
            # mix. Cleanup responsibility is established BEFORE the command, so
            # a lost response still releases this exact owner and never
            # commissioning's gate.
            measurement_gate_cleanup_required = True
            if gate_owner == MEASUREMENT_GATE_OWNER:
                await _acquire_measurement_gate()
            else:
                await _acquire_measurement_gate(gate_owner=gate_owner)
            measurement_gate_acquired = True

            async def _refresh_measurement_gate_lease() -> None:
                nonlocal measurement_gate_lease_error
                delay = MEASUREMENT_GATE_REFRESH_SEC
                last_confirmed = time.monotonic()
                while True:
                    await asyncio.sleep(delay)
                    try:
                        if gate_owner == MEASUREMENT_GATE_OWNER:
                            await _acquire_measurement_gate()
                        else:
                            await _acquire_measurement_gate(
                                gate_owner=gate_owner,
                            )
                    except MeasurementWindowError as exc:
                        logger.warning(
                            "measurement gate lease refresh failed: %s",
                            exc,
                        )
                        if (
                            time.monotonic() - last_confirmed
                            >= MEASUREMENT_GATE_ABORT_SEC
                        ):
                            measurement_gate_lease_error = MeasurementWindowError(
                                "Measurement isolation could not be renewed; "
                                "the sweep was stopped before household music "
                                "could re-enter the mix. Check System status "
                                "and try again."
                            )
                            if abort_target is not None:
                                # Held-window holder: cancel the ACTUAL
                                # in-flight play task, or latch for the next
                                # play; cancelling the entering task would not
                                # stop the sweep.
                                abort_target.abort(measurement_owner_task)
                            elif measurement_owner_task is not None:
                                measurement_owner_task.cancel()
                            return
                        delay = MEASUREMENT_LEASE_RETRY_SEC
                    else:
                        last_confirmed = time.monotonic()
                        delay = MEASUREMENT_GATE_REFRESH_SEC

            measurement_gate_refresh_task = asyncio.create_task(
                _refresh_measurement_gate_lease()
            )

        if not skip_voice_pause:
            # Establish cleanup responsibility before PAUSE: a lost response may
            # still have armed the voice gate.
            voice_pause_cleanup_required = require_voice_pause
            try:
                resp = await _voice_uds_command(
                    voice_socket_path,
                    "MEASURE_PAUSE",
                    timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
                )
                if not isinstance(resp, dict):
                    if require_voice_pause:
                        raise MeasurementWindowError(
                            "MEASURE_PAUSE returned a malformed response."
                        )
                    raise TypeError("MEASURE_PAUSE response is not an object")
                pause_result = resp.get("result")
                if pause_result == "ok":
                    voice_paused = True
                    voice_pause_cleanup_required = True
                    drained = resp.get("drained")
                    if require_voice_pause and drained is not True:
                        raise MeasurementWindowError(
                            "Voice pause was armed, but the daemon did not "
                            "prove prior assistant audio drained."
                        )

                    async def _refresh_voice_lease() -> None:
                        nonlocal voice_lease_error

                        def _abort_strict_window() -> None:
                            nonlocal voice_lease_error
                            voice_lease_error = MeasurementWindowError(
                                "Voice isolation could not be renewed; "
                                "the measurement was stopped."
                            )
                            if abort_target is not None:
                                abort_target.abort(measurement_owner_task)
                            elif measurement_owner_task is not None:
                                measurement_owner_task.cancel()

                        delay = MEASUREMENT_LEASE_REFRESH_SEC
                        while True:
                            await asyncio.sleep(delay)
                            try:
                                renewal = await _voice_uds_command(
                                    voice_socket_path,
                                    "MEASURE_PAUSE",
                                    timeout=VOICE_MEASURE_PAUSE_TIMEOUT_SEC,
                                )
                            except (
                                FileNotFoundError,
                                OSError,
                                asyncio.TimeoutError,
                                RuntimeError,
                                ValueError,
                                TypeError,
                                UnicodeError,
                            ) as exc:
                                logger.warning(
                                    "measurement lease refresh failed: %s",
                                    exc,
                                )
                                if require_voice_pause:
                                    _abort_strict_window()
                                    return
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                                continue
                            renewal_ok = (
                                isinstance(renewal, dict)
                                and renewal.get("result") == "ok"
                                and (
                                    not require_voice_pause
                                    or renewal.get("drained") is True
                                )
                            )
                            if not renewal_ok:
                                logger.warning(
                                    "measurement lease refresh returned non-ok: %s",
                                    renewal,
                                )
                                if require_voice_pause:
                                    _abort_strict_window()
                                    return
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                            else:
                                delay = MEASUREMENT_LEASE_REFRESH_SEC

                    lease_refresh_task = asyncio.create_task(
                        _refresh_voice_lease()
                    )
                    if drained is False:
                        logger.warning(
                            "MEASURE_PAUSE timed out draining prior assistant "
                            "audio — proceeding under the historical permissive "
                            "correction policy; MEASURE_RESUME remains required"
                        )
                else:
                    if require_voice_pause:
                        raise MeasurementWindowError(
                            "Voice daemon refused MEASURE_PAUSE: "
                            f"{resp.get('result', 'missing result')}."
                        )
                    logger.warning(
                        "MEASURE_PAUSE returned non-ok: %s — proceeding "
                        "anyway, but the WakeLoop may still consume mic "
                        "during the sweep",
                        resp,
                    )
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                if require_voice_pause:
                    raise MeasurementWindowError(
                        f"Could not pause voice for measurement: {e}"
                    ) from e
                logger.warning(
                    "voice_daemon MEASURE_PAUSE failed (%s) — proceeding "
                    "without WakeLoop pause. The voice loop will probably "
                    "still work fine if the daemon is simply down.",
                    e,
                )
            except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
                if require_voice_pause:
                    raise MeasurementWindowError(
                        f"Could not pause voice for measurement: {e}"
                    ) from e
                raise

        logger.info(
            "measurement window OPEN (voice_paused=%s, volume_hold=%s)",
            voice_paused,
            hold_acquired,
        )
        yield
    finally:
        # Release the mutex in an INNER finally — after the restore I/O but
        # guaranteed even if it raises. Clearing it before these awaits would
        # let a queued second window run during restoration; clearing it only on
        # the success path would strand it True forever after a raising restore.
        try:
            await _stop_lease_refresh(
                measurement_gate_refresh_task, "measurement gate",
            )
            await _stop_lease_refresh(
                hold_refresh_task, "measurement hold",
            )
            await _stop_lease_refresh(
                lease_refresh_task, "measurement lease",
            )
            # Restore voice first, then release mux's music-isolation gate.
            if voice_pause_cleanup_required:
                try:
                    # A plain read deadline, NOT VOICE_MEASURE_PAUSE_TIMEOUT_SEC:
                    # RESUME has no in-playout drain to wait out, and giving up
                    # here is recoverable via the daemon's auto-clear.
                    await _voice_uds_command(
                        voice_socket_path, "MEASURE_RESUME", timeout=3.0,
                    )
                except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                    logger.error(
                        "voice_daemon MEASURE_RESUME failed: %s — the "
                        "daemon's auto-clear safety timer will recover",
                        e,
                    )
                except (RuntimeError, ValueError, TypeError, UnicodeError) as e:
                    if not require_voice_pause:
                        raise
                    logger.error(
                        "voice_daemon MEASURE_RESUME returned malformed data: %s; "
                        "the daemon's auto-clear safety timer will recover",
                        e,
                    )
            gate_release_error: MeasurementWindowError | None = None
            if measurement_gate_cleanup_required:
                try:
                    if gate_owner == MEASUREMENT_GATE_OWNER:
                        await _release_measurement_gate(
                            allow_other_owner=not measurement_gate_acquired,
                        )
                    else:
                        await _release_measurement_gate(
                            gate_owner=gate_owner,
                            allow_other_owner=not measurement_gate_acquired,
                        )
                except MeasurementWindowError as exc:
                    # If release truly did not land, the still-held mux gate
                    # keeps music silent; surface the action required.
                    gate_release_error = exc
            # Hold last: LIFO against the acquire order, so no second
            # measurement can take the speaker while this one still restores.
            if hold_acquired:
                await _release_measurement_hold(gate_owner)
            logger.info("measurement window CLOSED")
            if measurement_gate_lease_error is not None:
                raise measurement_gate_lease_error
            if voice_lease_error is not None:
                raise voice_lease_error
            if gate_release_error is not None:
                raise gate_release_error
        finally:
            _window_active = False


class HeldWindow:
    """A ``measurement_window`` held open for a caller that is not on its loop.

    ``hold`` is the plain park; ``holding`` is for a caller already on a loop
    that needs its own body and parking policy inside the window. A release
    arriving before the loop exists is honoured rather than lost, and
    ``entered`` is signalled from the one ``finally`` below, after
    ``on_failure`` has published what the failure means. Which phase ``error``
    belongs to is read off ``held`` and ``lost``.
    """

    def __init__(self, **window_kwargs: Any) -> None:
        self._window_kwargs = window_kwargs
        #: Unblocks a waiting caller: the window is up, or holding it failed.
        self.entered = threading.Event()
        #: True once the window actually opened.
        self.held = False
        #: The window ended before anyone released it — its isolation is gone
        #: and whatever the caller is doing has to stop.
        self.lost = threading.Event()
        #: The one failure this hold produced, if any.
        self.error: BaseException | None = None
        self._releasing = threading.Event()
        self._release: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def release(self) -> None:
        """Ask the hold to exit. Thread-safe, and safe before ``hold`` runs."""

        # Ordered before the wake-up: `hold` re-checks this flag once it owns
        # the asyncio.Event, so an early release is honoured there.
        self._releasing.set()
        if self._loop is None or self._release is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._release.set)
        except RuntimeError:
            pass  # the loop is already gone: the hold ended on its own

    @asynccontextmanager
    async def holding(
        self, *, on_failure: Callable[[BaseException], None] = lambda _e: None,
    ) -> AsyncIterator[asyncio.Event]:
        """Enter the window, yield the Event ``release`` sets. ON its loop.

        The failure is recorded here, handed to ``on_failure`` to publish, then
        re-raised for the caller to suppress or not.
        """

        try:
            self._loop = asyncio.get_running_loop()
            self._release = asyncio.Event()
            if self._releasing.is_set():
                self._release.set()
            async with measurement_window(**self._window_kwargs):
                self.held = True
                yield self._release
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            if self.held and not self._releasing.is_set():
                self.lost.set()
            on_failure(exc)
            raise
        finally:
            self.entered.set()

    async def hold(self) -> None:
        """Enter the window and park until released. Run this ON its loop."""

        # BaseException, not Exception: measurement_window aborts a lost window
        # by CANCELLING the task that entered it — this one. `holding` records
        # the failure, so swallowing it here just lets this loop finish.
        with suppress(BaseException):
            async with self.holding() as release:
                self.entered.set()
                await release.wait()
