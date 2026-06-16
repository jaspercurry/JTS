"""Measurement-window coordinator.

`measurement_window()` is an async context manager that pauses
everything that would interfere with a clean room measurement, then
restores on exit. Used by the room-correction wizard:

    async with measurement_window():
        await play_sweep(...)
        # ... iPhone uploads capture, deconv, design, write YAML ...
        await camilla.set_config_path(out_path)

What gets paused (and why):

  - Music source daemons via `systemctl stop`. We need silence on the
    fan-in music chain so the sweep is the only signal hitting the
    loopback.
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
  - jasper-mux (the renderer arbiter). With the renderers stopped,
    there's nothing for mux to arbitrate — leaving it running is
    harmless and avoids one more thing to restart.
  - jasper-aec-bridge (if enabled). It taps the music chain via
    dsnoop and the sweep going through the chain temporarily drives
    the AEC reference. The bridge re-converges in ~200 ms after the
    sweep ends; disabling+re-enabling the bridge would take longer.

Robustness:
  - Restoration runs in `finally` — exceptions don't strand the
    speaker in "everything stopped" state.
  - The voice-daemon RESUME has a server-side 2-minute auto-clear
    safety timer (see voice_daemon.py), so even a coordinator crash
    (kill -9) is recovered automatically.
  - A precondition check refuses to start if a voice session is
    currently active — yanking an in-flight session is worse than
    asking the user to wait or end it first.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from ..control import restart_broker

logger = logging.getLogger(__name__)


# Renderers paused for the measurement window.
DEFAULT_RENDERERS_TO_PAUSE: tuple[str, ...] = (
    "librespot.service",
    "shairport-sync.service",
    "bluealsa-aplay.service",
    "jasper-usbsink.service",
)

DEFAULT_VOICE_SOCKET_PATH = "/run/jasper/voice.sock"

# Mutual-exclusion flag for measurement_window(). Only one window may be open
# at a time: a second concurrent window would let whichever exits FIRST send
# MEASURE_RESUME and restart the renderers while the other is still measuring,
# corrupting its capture. All callers run on jasper-web's single background
# event loop, so a plain check-and-set before the first await is atomic — no
# asyncio.Lock, which would bind to one loop and break the per-test
# asyncio.run() loops.
_window_active = False


class MeasurementWindowError(RuntimeError):
    """A precondition failed (active voice session, voice daemon
    unreachable, etc.). The window did not open and nothing was
    paused."""


async def _systemctl(action: str, service: str) -> None:
    """Stop/start a renderer for the measurement window via jasper-control's
    restart broker. Logs but doesn't raise on failure — we'd rather proceed
    with an imperfectly-paused chain than abort the measurement entirely.
    Failed pauses surface as audible artifacts in the captured sweep, which
    is the signal the user needs to investigate.

    WS1 Phase 3: routed through the broker (off-thread, the client is
    blocking) so the correction wizard needs no privilege of its own. While
    it is still root the broker client falls back to a direct systemctl if
    the broker is unreachable."""
    resp = await asyncio.to_thread(
        restart_broker.manage_units,
        service, verb=action, reason="room correction pause",
        no_block=False, timeout=10.0,
    )
    if not resp.get("ok"):
        logger.warning(
            "systemctl %s %s failed: %s",
            action, service, resp.get("error") or f"rc={resp.get('rc')}",
        )
    else:
        logger.debug("systemctl %s %s ok", action, service)


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
    renderers_to_pause: tuple[str, ...] = DEFAULT_RENDERERS_TO_PAUSE,
    skip_voice_pause: bool = False,
    skip_renderer_pause: bool = False,
) -> AsyncIterator[None]:
    """Pause renderers + voice loop, yield, restore.

    Args:
      voice_socket_path: voice_daemon's UDS path. Default matches
        what jasper-voice writes to.
      renderers_to_pause: systemd unit names to stop for the window.
        Stopped via `systemctl stop`, restarted via `systemctl start`.
      skip_voice_pause: don't send MEASURE_PAUSE/RESUME. For tests
        running without a voice daemon.
      skip_renderer_pause: don't touch systemctl. For tests / dev.

    Raises:
      MeasurementWindowError: a precondition failed before any
        services were touched. Nothing to restore.
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

    paused_services: list[str] = []
    voice_paused = False

    try:
        # Precondition: no active voice session. Inside the try so the
        # window flag is cleared even when this raises (nothing is paused
        # yet, so there is still nothing to restore — contract preserved).
        if not skip_voice_pause:
            await _check_no_active_voice_session(voice_socket_path)

        if not skip_renderer_pause:
            for svc in renderers_to_pause:
                await _systemctl("stop", svc)
                paused_services.append(svc)

        if not skip_voice_pause:
            try:
                resp = await _voice_uds_command(
                    voice_socket_path, "MEASURE_PAUSE", timeout=3.0,
                )
                if resp.get("result") == "ok":
                    voice_paused = True
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

        logger.info(
            "measurement window OPEN (renderers=%d voice_paused=%s)",
            len(paused_services), voice_paused,
        )
        yield
    finally:
        # Release the mutex in an INNER finally — after the restore I/O, but
        # guaranteed even if it raises. Timing matters on the single
        # background loop: clearing the flag BEFORE these awaits would let a
        # queued second window run during the restore, `systemctl stop` the
        # renderers this one is mid-`systemctl start` of (the corruption the
        # mutex exists to prevent). Clearing it AFTER but only on the success
        # path would re-strand the flag True forever if a restore step raised
        # (e.g. systemctl missing). The inner finally gives both: serialized
        # against the restore, and never leaked.
        try:
            # Restore voice FIRST so wake events can resume the moment the
            # user is ready to interact, even before the renderers have
            # fully come back. Then restart the renderers — they spin up
            # in parallel.
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
            # Restart renderers in parallel — `systemctl start` is fast,
            # the actual service startup is async on the systemd side.
            if paused_services:
                await asyncio.gather(*[
                    _systemctl("start", svc) for svc in paused_services
                ])
            logger.info("measurement window CLOSED")
        finally:
            _window_active = False
