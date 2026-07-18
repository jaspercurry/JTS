# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measurement-window coordinator.

`measurement_window()` is an async context manager that pauses
everything that would interfere with a clean room measurement, then
restores on exit. Used by the room-correction wizard:

    async with measurement_window():
        await play_sweep(...)
        # ... iPhone uploads capture, deconv, design, write YAML ...
        await camilla.set_config_path(out_path)

What gets paused (and why):

  - Every music lane at fan-in's existing diagnostic gate. ``jasper-mux`` is
    the sole owner of that gate: ``TEST_SELECT correction`` excludes AirPlay,
    Spotify, Bluetooth, and USB while continuing to admit the measurement
    lane. This avoids a second writer for USB's policy mute.
  - voice_daemon's WakeLoop + outputd content meter via the
    `MEASURE_PAUSE` UDS command (see jasper/voice_daemon.py). The
    WakeLoop drops mic frames during the window — no wake events
    fire, no Ducker calls happen, no TTS plays. The outputd content
    meter is paused so the sweep does not become the next assistant
    loudness baseline.

What does NOT get paused:

  - jasper-camilla itself. The sweep MUST go through CamillaDSP so
    the measurement reflects the same DSP path music takes. Any
    correction we generate then acts on the same chain we measured.
  - jasper-mux (the renderer arbiter). It remains alive and reasserts the
    diagnostic gate while the window is open.
  - jasper-aec-bridge (if enabled). It taps the music chain via
    dsnoop and the sweep going through the chain temporarily drives
    the AEC reference. The bridge re-converges in ~200 ms after the
    sweep ends; disabling+re-enabling the bridge would take longer.

Robustness:
  - Music daemons keep running and fan-in keeps draining their private lanes;
    only mux's selected-input gate changes. A web crash therefore cannot leave
    enabled household sources manually stopped.
  - Gate and voice restoration run in ``finally`` and both have independent
    crash-recovery leases.
  - The voice-daemon RESUME has a server-side 2-minute auto-clear safety timer
    (see voice_daemon.py). A healthy long-running window renews that lease every
    minute; a coordinator crash (kill -9) stops renewal and still recovers
    automatically.
  - A precondition check refuses to start if a voice session is
    currently active — yanking an in-flight session is worse than
    asking the user to wait or end it first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ..control.uds import _mux_socket_command
from ..log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_VOICE_SOCKET_PATH = "/run/jasper/voice.sock"
# Refresh the voice-side 120 s crash-recovery timer while a valid measurement
# window remains open. A relay setup may wait up to eight minutes for a human;
# renewal preserves that legitimate window without weakening crash recovery.
MEASUREMENT_LEASE_REFRESH_SEC = 60.0
MEASUREMENT_LEASE_RETRY_SEC = 5.0
MEASUREMENT_FANIN_LABEL = "correction"
MEASUREMENT_GATE_OWNER = "correction-measurement"
MEASUREMENT_GATE_REFRESH_SEC = 20.0
MEASUREMENT_GATE_RETRY_SEC = 0.1
# Abort the owning window before mux's 60 s availability lease can expire and
# reopen music. Each failed acquire is itself bounded at 3 s, leaving >15 s of
# recovery margin at the default.
MEASUREMENT_GATE_ABORT_SEC = 40.0

# Mutual-exclusion flag for measurement_window(). Only one window may be open
# at a time: a second concurrent window would let whichever exits FIRST send
# MEASURE_RESUME and release the mux gate while the other is still measuring,
# corrupting its capture. All callers run on jasper-web's single background
# event loop, so a plain check-and-set before the first await is atomic — no
# asyncio.Lock, which would bind to one loop and break the per-test
# asyncio.run() loops.
_window_active = False


class MeasurementWindowError(RuntimeError):
    """A precondition failed or isolation could not be proven/restored."""


class MeasurementAbortTarget:
    """Redirectable cancel target for the gate-lease abort (held windows).

    The refresh task's default isolation-loss abort cancels the task that
    ENTERED the window. That is right for the per-sweep flows (the entering
    task is the playing task), but a flow that holds one window for a whole
    multi-capture session (the v2 crossover conductor, W6.1) enters it from
    its long-lived session task while each play runs as its OWN task — the
    default cancel would not stop the actual in-flight sweep. Such a holder
    passes one of these to :func:`measurement_window`:

    * the per-play path ``register()``s the current play task while playing
      and ``clear()``s it after;
    * on a renew failure the refresh task calls :meth:`abort`, which latches
      ``failed`` (the holder's next play must check it and refuse honestly)
      and cancels the registered play task if one is live, else the fallback
      (the entering task — the pre-existing behavior).
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


def _measurement_gate_held(payload: dict) -> bool:
    return (
        payload.get("test_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("active_source") == MEASUREMENT_FANIN_LABEL
        and payload.get("test_owner") == MEASUREMENT_GATE_OWNER
    )


async def _acquire_measurement_gate() -> None:
    """Ask mux to exclude every music lane and verify the landed state."""

    try:
        payload = await _mux_socket_command(
            "TEST_SELECT "
            f"{MEASUREMENT_FANIN_LABEL} {MEASUREMENT_GATE_OWNER}",
            timeout=3.0,
        )
    except (OSError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
        raise MeasurementWindowError(
            f"Could not isolate the measurement lane: {exc}"
        ) from exc
    if not _measurement_gate_held(payload):
        raise MeasurementWindowError(
            "Mux did not confirm the isolated measurement lane."
        )
    log_event(logger, "correction.measurement_gate", action="acquire", result="ok")


async def _release_measurement_gate(*, allow_other_owner: bool = False) -> None:
    """Release our mux gate, retrying and requiring owner-aware landed state.

    ``allow_other_owner`` is only for cleanup after an indeterminate acquire.
    It permits a concurrently held commissioning gate to remain untouched.
    """

    last_error = "mux did not confirm release"
    for attempt in range(3):
        try:
            payload = await _mux_socket_command(
                f"TEST_RELEASE {MEASUREMENT_GATE_OWNER}", timeout=3.0,
            )
            if (
                payload.get("test_source") is None
                and payload.get("test_owner") is None
            ):
                log_event(
                    logger,
                    "correction.measurement_gate",
                    action="release",
                    result="ok",
                )
                return
            last_error = "mux still reports a selected test lane"
        except (OSError, RuntimeError, ValueError, asyncio.TimeoutError) as exc:
            last_error = str(exc)
            # A lost RELEASE response may still have landed. STATUS also lets
            # indeterminate-acquire cleanup distinguish another feature's
            # owner without ever releasing that owner's gate.
            try:
                status = await _mux_socket_command("STATUS", timeout=3.0)
            except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
                status = None
            if isinstance(status, dict):
                owner = status.get("test_owner")
                if owner is None and status.get("test_source") is None:
                    return
                if allow_other_owner and owner != MEASUREMENT_GATE_OWNER:
                    return
        if attempt < 2:
            await asyncio.sleep(MEASUREMENT_GATE_RETRY_SEC)
    log_event(
        logger,
        "correction.measurement_gate",
        action="release",
        result="failed",
        reason=last_error,
        level=logging.ERROR,
    )
    raise MeasurementWindowError(
        f"Could not release the isolated measurement lane: {last_error}"
    )


async def _voice_uds_command(
    socket_path: str, cmd: str, *, timeout: float = 5.0,
) -> dict:
    """Send one ASCII line to voice_daemon's control socket and
    parse the JSON response. Same wire format as
    jasper.control.server._voice_socket_command (which we don't
    import to avoid a circular dependency)."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
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
) -> None:
    """Refuse to open a measurement window while a voice session is
    in progress. Yanking the session would orphan the user's turn
    and look like a hang.

    Returns silently on success. Raises MeasurementWindowError if a
    session is active. Treats UDS-unreachable as "voice daemon down,
    so no session" — the measurement can proceed.
    """
    try:
        status = await _voice_uds_command(socket_path, "STATUS", timeout=2.0)
    except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
        # No daemon → no session to interrupt. Log + proceed.
        logger.info(
            "voice daemon not reachable for STATUS check (%s) — "
            "assuming no active session",
            e,
        )
        return
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
) -> AsyncIterator[None]:
    """Isolate fan-in's correction lane + pause voice, yield, restore.

    Args:
      voice_socket_path: voice_daemon's UDS path. Default matches
        what jasper-voice writes to.
      skip_voice_pause: don't send MEASURE_PAUSE/RESUME. For tests
        running without a voice daemon.
      skip_music_isolation: don't acquire mux's diagnostic gate. Tests only.
      abort_target: redirectable cancel target for the isolation-loss abort
        (see :class:`MeasurementAbortTarget`). ``None`` keeps the default —
        cancel the task that entered the window.

    Raises:
      MeasurementWindowError: a precondition failed or mux isolation could not
        be proven/restored.
    """
    # Mutual exclusion (see _window_active). Check-and-set BEFORE the first
    # await so it's atomic on the single background loop. A second concurrent
    # window fails fast rather than queueing — it means a racing
    # /start /verify /next-position, not work to serialize.
    global _window_active
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
    voice_paused = False
    lease_refresh_task: asyncio.Task[None] | None = None

    try:
        # Precondition: no active voice session. Inside the try so the
        # window flag is cleared even when this raises (nothing is paused
        # yet, so there is still nothing to restore — contract preserved).
        if not skip_voice_pause:
            await _check_no_active_voice_session(voice_socket_path)

        if not skip_music_isolation:
            # Gate first: even a renderer that races its subsequent stop cannot
            # enter the mix. Mux remains the single writer and reasserts this
            # diagnostic selection once per tick for the whole window.
            # Cleanup responsibility is established BEFORE the command: if the
            # selection lands but its response is lost, finally still releases
            # this exact owner and can never release commissioning's gate.
            measurement_gate_cleanup_required = True
            await _acquire_measurement_gate()
            measurement_gate_acquired = True
            measurement_owner_task = asyncio.current_task()

            async def _refresh_measurement_gate_lease() -> None:
                nonlocal measurement_gate_lease_error
                delay = MEASUREMENT_GATE_REFRESH_SEC
                last_confirmed = time.monotonic()
                while True:
                    await asyncio.sleep(delay)
                    try:
                        await _acquire_measurement_gate()
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
                                # Held-window holder (v2 session): cancel the
                                # ACTUAL in-flight play task (or latch for the
                                # next play) — cancelling the entering task
                                # would not stop the sweep (W6.1 gate fix).
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
            try:
                resp = await _voice_uds_command(
                    voice_socket_path, "MEASURE_PAUSE", timeout=3.0,
                )
                if resp.get("result") == "ok":
                    voice_paused = True

                    async def _refresh_voice_lease() -> None:
                        delay = MEASUREMENT_LEASE_REFRESH_SEC
                        while True:
                            await asyncio.sleep(delay)
                            try:
                                renewal = await _voice_uds_command(
                                    voice_socket_path,
                                    "MEASURE_PAUSE",
                                    timeout=3.0,
                                )
                            except (
                                FileNotFoundError,
                                OSError,
                                asyncio.TimeoutError,
                                RuntimeError,
                                ValueError,
                            ) as exc:
                                logger.warning(
                                    "measurement lease refresh failed: %s",
                                    exc,
                                )
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                                continue
                            if renewal.get("result") != "ok":
                                logger.warning(
                                    "measurement lease refresh returned non-ok: %s",
                                    renewal,
                                )
                                delay = MEASUREMENT_LEASE_RETRY_SEC
                            else:
                                delay = MEASUREMENT_LEASE_REFRESH_SEC

                    lease_refresh_task = asyncio.create_task(
                        _refresh_voice_lease()
                    )
                else:
                    logger.warning(
                        "MEASURE_PAUSE returned non-ok: %s — proceeding "
                        "anyway, but the WakeLoop may still consume mic "
                        "during the sweep",
                        resp,
                    )
            except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                logger.warning(
                    "voice_daemon MEASURE_PAUSE failed (%s) — proceeding "
                    "without WakeLoop pause. The voice loop will probably "
                    "still work fine if the daemon is simply down.",
                    e,
                )

        logger.info("measurement window OPEN (voice_paused=%s)", voice_paused)
        yield
    finally:
        # Release the mutex in an INNER finally — after the restore I/O, but
        # guaranteed even if it raises. Timing matters on the single
        # background loop: clearing the flag BEFORE these awaits would let a
        # queued second window run during gate/voice restoration (the
        # corruption the mutex exists to prevent). Clearing it AFTER but only
        # on the success
        # path would re-strand the flag True forever if a restore step raised
        # (e.g. systemctl missing). The inner finally gives both: serialized
        # against the restore, and never leaked.
        try:
            if measurement_gate_refresh_task is not None:
                measurement_gate_refresh_task.cancel()
                try:
                    await measurement_gate_refresh_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    logger.exception("measurement gate refresh task failed")
            if lease_refresh_task is not None:
                lease_refresh_task.cancel()
                try:
                    await lease_refresh_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001
                    # Renewal is resilience-only. Never let a dead background
                    # task bypass MEASURE_RESUME or mux-gate restoration.
                    logger.exception("measurement lease refresh task failed")
            # Restore voice first, then release mux's music-isolation gate.
            if voice_paused:
                try:
                    await _voice_uds_command(
                        voice_socket_path, "MEASURE_RESUME", timeout=3.0,
                    )
                except (FileNotFoundError, OSError, asyncio.TimeoutError) as e:
                    logger.error(
                        "voice_daemon MEASURE_RESUME failed: %s — daemon's "
                        "auto-clear safety timer will recover in ~2 min",
                        e,
                    )
            gate_release_error: MeasurementWindowError | None = None
            if measurement_gate_cleanup_required:
                try:
                    await _release_measurement_gate(
                        allow_other_owner=not measurement_gate_acquired,
                    )
                except MeasurementWindowError as exc:
                    # If release truly did not land, the still-held mux gate
                    # keeps music silent; surface the action required.
                    gate_release_error = exc
            logger.info("measurement window CLOSED")
            if measurement_gate_lease_error is not None:
                raise measurement_gate_lease_error
            if gate_release_error is not None:
                raise gate_release_error
        finally:
            _window_active = False
