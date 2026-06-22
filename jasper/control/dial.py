# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Dial heartbeat persistence, liveness probing, and UDP log listener."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
from typing import Any

# Operational lines (listener bound, socket errors) stay under the
# jasper-control server logger so per-logger config and journal filters
# behave exactly as before this module was split out of server.py. The
# dial's own mirrored messages keep their dedicated `jasper.dial` logger.
logger = logging.getLogger("jasper.control.server")
dial_log = logging.getLogger("jasper.dial")

# Most-recent dial heartbeat. Updated by the UDP log listener every
# time a datagram arrives; read by GET /dial/status. Kept module-level
# so jasper-doctor can ask "is a dial actually talking to us?" without
# parsing the journal. Lock isn't needed — Python dict assignment is
# atomic and a stale read is harmless for a heartbeat.
#
# Persisted to disk so `last_seen_ip` survives a jasper-control
# restart. Without this, every restart (typically a deploy) leaves
# the in-memory dict empty until the next user-initiated dlog —
# encoder turn or button press — which makes /state.satellites.dial.online
# briefly inaccurate for any external consumer. The file is tiny
# (~150 B) and writes happen at dlog rate (a few per second under
# heavy dial use), well within SD-card tolerance.
DIAL_HEARTBEAT_PATH = os.environ.get(
    "JASPER_DIAL_HEARTBEAT_PATH",
    "/var/lib/jasper/dial_heartbeat.json",
)


def _load_dial_heartbeat() -> dict[str, Any]:
    """Read the persisted heartbeat dict. Returns the empty default
    on any error (missing file, malformed JSON, wrong types) — a
    corrupted persisted file should never block the daemon from
    starting. Field-level type checks prevent a stale or
    hand-edited file from injecting odd values into /state.
    """
    default: dict[str, Any] = {
        "last_seen_at": None,
        "last_seen_ip": None,
        "last_message": None,
    }
    try:
        with open(DIAL_HEARTBEAT_PATH) as f:
            blob = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return default
    if not isinstance(blob, dict):
        return default
    ts = blob.get("last_seen_at")
    ip = blob.get("last_seen_ip")
    msg = blob.get("last_message")
    return {
        "last_seen_at": ts if isinstance(ts, (int, float)) else None,
        "last_seen_ip": ip if isinstance(ip, str) else None,
        "last_message": msg if isinstance(msg, str) else None,
    }


def _persist_dial_heartbeat(snapshot: dict[str, Any]) -> None:
    """Atomically write the heartbeat snapshot. Fail-soft — a write
    error logs at WARN but never raises into the UDP listener's
    receive loop. tempfile+rename guarantees readers never see a
    half-written file."""
    try:
        directory = os.path.dirname(DIAL_HEARTBEAT_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = DIAL_HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp, DIAL_HEARTBEAT_PATH)
    except OSError as e:
        dial_log.warning(
            "dial heartbeat persistence: write to %s failed: %s",
            DIAL_HEARTBEAT_PATH, e,
        )


_dial_heartbeat: dict[str, Any] = _load_dial_heartbeat()


async def _probe_dial_reachable(ip: str, *, timeout: float = 0.5) -> bool:
    """Fast TCP probe for dial liveness. The dial firmware doesn't run
    a server on any TCP port, so any connect attempt resolves to:

    - ConnectionRefusedError (RST from a live host): online
    - asyncio.TimeoutError / OSError: unreachable

    Port 80 is arbitrary — closed-port RST behaviour is identical on
    any port. Replaces the prior activity-based `online` check, which
    flagged an idle-but-healthy dial offline because the dial only
    emits UDP dlogs on encoder/button events. The probe takes
    ~3-10 ms on a dial running the WiFi-sleep-disabled firmware (see
    firmware/dial/src/main.cpp `WiFi.setSleep(false)`); the 500 ms
    cap is the worst-case envelope for a still-sleeping dial."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, 80),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except ConnectionRefusedError:
        return True
    except (asyncio.TimeoutError, OSError):
        return False


def run_dial_log_listener(host: str, port: int) -> threading.Thread:
    """Listen for one-line UDP datagrams from the dial and re-emit them
    via the Python logger (so `journalctl -u jasper-control` shows them
    interleaved with the HTTP-side log). Fire-and-forget on the dial
    side — UDP loss is acceptable for diagnostic output, and the dial
    isn't blocked on a TCP handshake when the Pi is unreachable.

    The listener runs in a daemon thread so it doesn't block server
    shutdown."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.settimeout(1.0)

    def _loop() -> None:
        logger.info("dial-log UDP listener bound to %s:%d", host, port)
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                logger.warning("dial-log socket error: %s", e)
                return
            try:
                msg = data.decode("utf-8", errors="replace").rstrip()
            except Exception:  # noqa: BLE001
                msg = repr(data)
            # Tag with sender IP so multi-dial setups don't get confused.
            dial_log.info("[%s] %s", addr[0], msg)
            # Heartbeat for jasper-doctor's "is the dial talking?" check.
            _dial_heartbeat["last_seen_at"] = time.time()
            _dial_heartbeat["last_seen_ip"] = addr[0]
            _dial_heartbeat["last_message"] = msg
            # Persist so the next jasper-control restart inherits the
            # last-known IP instead of starting empty (which would leave
            # /state.satellites.dial.online as false until the next dlog).
            _persist_dial_heartbeat(dict(_dial_heartbeat))

    t = threading.Thread(target=_loop, name="dial-log-listener", daemon=True)
    t.start()
    return t
