"""Unix-domain socket server: voice ↔ peering RPC.

Mirrors the existing voice_daemon.py UDS pattern (newline-delimited
ASCII commands, single JSON response per command). Owned by
jasper-control; jasper-voice connects as a client on every wake event
and on session lifecycle transitions.

Wire protocol (one line in, one JSON line out):

  ARBITRATE <json>   → {"result":"WIN"|"LOSE","epoch":"<uuid>"}
                       Long-running: blocks up to (arb_window_ms + 50)
                       before resolving. WIN means voice should
                       proceed with begin_turn; LOSE means abort.
  SESSION_STARTED <epoch>           → {"result":"ok"}
  SESSION_ENDED <epoch> <reason>    → {"result":"ok"}
  STATUS                             → {"result":"ok", "state":"...",
                                         "peers":[...], "mode":"on"}
  PING                               → {"result":"pong"} — used by
                                         doctor's liveness check.

The ARBITRATE call accepts a JSON arg of shape:
  {"score":0.87,"snr_db":18.5,"rms_dbfs":-22.3,"can_serve":true}

The daemon is responsible for dispatching the resulting LocalWake to
the state machine and resolving the Future when StartSession or
StandDown actions are emitted. See `daemon.py`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# Reader timeout. Should be > arb_window_ms + some headroom for the
# state machine to emit StartSession/StandDown. 1.0 s covers the max
# arb window (500 ms) + slack.
READ_TIMEOUT_SEC = 1.0


# Type alias for the daemon-supplied callbacks.
ArbitrateFn = Callable[[dict], Awaitable[dict]]
NotifyStartFn = Callable[[str], Awaitable[None]]
NotifyEndFn = Callable[[str, str], Awaitable[None]]
StatusFn = Callable[[], Awaitable[dict]]


async def serve(
    *,
    path: str,
    arbitrate: ArbitrateFn,
    notify_session_started: NotifyStartFn,
    notify_session_ended: NotifyEndFn,
    status: StatusFn,
) -> asyncio.AbstractServer:
    """Start a Unix-socket server. Caller is responsible for await
    server.close() / server.wait_closed() at shutdown.
    """
    # Remove stale socket file from a previous run (matches the pattern
    # used by jasper-voice's UDS at voice_daemon.py:2012).
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    os.makedirs(os.path.dirname(path), exist_ok=True)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_SEC)
            line = raw.decode("ascii", errors="replace").strip()
            parts = line.split(maxsplit=1)
            cmd = parts[0].upper() if parts else ""
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "ARBITRATE":
                req = _parse_json_arg(arg)
                if req is None:
                    response = {"result": "ERROR", "error": "bad arbitrate args"}
                else:
                    response = await arbitrate(req)
            elif cmd == "SESSION_STARTED":
                epoch = arg.strip()
                if not epoch:
                    response = {"result": "ERROR", "error": "missing epoch"}
                else:
                    await notify_session_started(epoch)
                    response = {"result": "ok"}
            elif cmd == "SESSION_ENDED":
                # arg is "<epoch> <reason>"
                pieces = arg.split(maxsplit=1)
                if not pieces:
                    response = {"result": "ERROR", "error": "missing epoch"}
                else:
                    epoch = pieces[0]
                    reason = pieces[1] if len(pieces) > 1 else ""
                    await notify_session_ended(epoch, reason)
                    response = {"result": "ok"}
            elif cmd == "STATUS":
                response = await status()
                response["result"] = "ok"
            elif cmd == "PING":
                response = {"result": "pong"}
            else:
                response = {"result": "ERROR", "error": f"unknown command: {cmd}"}

            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        except asyncio.TimeoutError:
            logger.warning("peering UDS: client read timed out")
        except Exception:  # noqa: BLE001
            logger.exception("peering UDS: handler failed")
            try:
                writer.write(b'{"result":"ERROR","error":"handler exception"}\n')
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    server = await asyncio.start_unix_server(handle, path)
    # Tighten permissions — root:root, group/other read+write so the
    # voice daemon (also root, same group) can talk to us. Matches the
    # mode used at voice_daemon.py:2019 for symmetry.
    try:
        os.chmod(path, 0o660)
    except OSError as e:
        logger.warning("peering UDS: chmod %s failed: %s", path, e)
    logger.info("event=peering.uds.listening path=%s", path)
    return server


def _parse_json_arg(arg: str) -> dict | None:
    if not arg:
        return None
    try:
        obj = json.loads(arg)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------- Client helper (used by voice_daemon in PR 2) ----------


async def send_request(
    path: str, cmd: str, *, timeout: float = 1.0,
) -> dict:
    """Connect to the peering UDS, send one command, read one response.

    Returns a dict (parsed JSON response). Raises:
      - FileNotFoundError if the socket doesn't exist (daemon down)
      - OSError / asyncio.TimeoutError for connect/read failures
      - RuntimeError if the server returned nothing

    Mirrors jasper.control.server._voice_socket_command's shape so
    voice_daemon's caller can use the same try/except idiom.
    """
    reader, writer = await asyncio.open_unix_connection(path)
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
        raise RuntimeError("peering daemon returned no response")
    return json.loads(line.decode("utf-8"))
