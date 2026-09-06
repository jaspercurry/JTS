# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unix-domain socket server: one ASCII command line in, one JSON line out
— external IPC (jasper-control, in particular) into the wake loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from ..voice_daemon import WakeLoop

logger = logging.getLogger("jasper.voice_daemon")


async def serve(
    wake_loop: WakeLoop, socket_path: str, *, mode: int = 0o660,
) -> asyncio.AbstractServer:
    """Listen for one-line commands on a Unix domain socket so external
    daemons (jasper-control, in particular) can drive voice-session
    state without going through the wake word.

    Wire format: line of ASCII, terminated by `\\n`. Response: a single
    JSON object terminated by `\\n`.

    Commands:
        START [source]      → manual_session_start  (long-press begin)
        END                 → manual_session_end    (long-press release)
        STATUS              → session_status        (diagnostic snapshot)
        CUE_PLAY <slug>     → play a registered audio cue through the
                              daemon's fan-in-backed TtsPlayout. Routed
                              here so a standalone CLI doesn't have to
                              recreate the output path or gain policy.
        MEASURE_PAUSE       → open a room-correction measurement
                              window. Drops mic frames, pauses the
                              outputd content meter, and reports additive
                              `drained` evidence while keeping the compatible
                              `result=ok` whenever cleanup is owned. Refuses
                              (BUSY) if a session is active. Auto-clears after
                              measurement_hold.MEASUREMENT_AUTOCLEAR_SEC
                              if RESUME is never sent.
        MEASURE_RESUME      → close the measurement window.
                              Idempotent.
        MUTE                → user-driven mic mute. Drops mic frames
                              at the wake-loop gate, ends any active
                              session, plays a low-pitch click. Runtime
                              state is persisted. Idempotent.
        UNMUTE              → resume listening. Plays a higher-pitch
                              click. Idempotent.

    The socket lives in /run (tmpfs) so it gets created fresh each boot
    via systemd's RuntimeDirectory=jasper."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("voice control socket: client read timed out")
                return
            line = raw.decode("ascii", errors="replace").strip()
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""
            result: dict[str, object]
            if cmd == "START":
                result = {
                    "result": await wake_loop.manual_session_start(arg or None),
                }
            elif cmd == "END":
                result = {"result": await wake_loop.manual_session_end()}
            elif cmd == "STATUS":
                result = wake_loop.session_status()
            elif cmd == "CUE_PLAY":
                result = {"result": await wake_loop.play_cue(arg)}
            elif cmd == "MEASURE_PAUSE":
                result = await wake_loop.measurement_hold.pause_response()
            elif cmd == "MEASURE_RESUME":
                result = {"result": await wake_loop.measurement_hold.resume()}
            elif cmd == "MUTE":
                result = {"result": await wake_loop.mute_mic()}
            elif cmd == "UNMUTE":
                result = {"result": await wake_loop.unmute_mic()}
            else:
                result = {"result": "UNKNOWN", "command": cmd}
            writer.write((json.dumps(result) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            logger.exception("voice control socket handler failed: %s", e)
            try:
                writer.write(b'{"result":"ERROR"}\n')
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # Unix-domain-socket: stale file from a crashed prior run blocks
    # bind(). Best-effort unlink first.
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    server = await asyncio.start_unix_server(handle, socket_path)
    try:
        os.chmod(socket_path, mode)
    except OSError as e:
        logger.warning("voice control socket chmod failed: %s", e)
    logger.info("voice control socket: %s", socket_path)
    return server


# Bounds Server.wait_closed() in the unwind: unbounded, a handler mid-command
# at SIGTERM keeps it pending behind jasper-voice.service's
# TimeoutStopSec=14s (MIC_LOSS_CUE_STOP_FLOOR_SEC), stalling every later
# teardown callback.
CONTROL_SOCKET_CLOSE_TIMEOUT_SEC = 2.0


async def close(server: asyncio.AbstractServer) -> None:
    """Bounded counterpart to `Server.__aexit__`, which awaits
    `wait_closed()` unbounded."""
    server.close()
    await asyncio.wait_for(server.wait_closed(), CONTROL_SOCKET_CLOSE_TIMEOUT_SEC)
