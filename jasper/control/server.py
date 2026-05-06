"""HTTP control surface for external clients (dial, future wall switches,
home automation). Bound to LAN so an ESP32 dial on the household network
can drive volume / transport / session.

Stack: stdlib http.server (ThreadingHTTPServer), pycamilladsp client.
Same dependency footprint as jasper-web — nothing new to install.

Phase 1 routes:
  GET  /healthz             — liveness check
  GET  /volume              — {"db": float, "percent": int}
  POST /volume/adjust       — body: {"delta_db": float} → new state
  POST /volume/set          — body: {"db": float} → new state

Future routes (phases 2-3, slot in beneath this file):
  POST /transport/toggle    — auto play↔pause based on moOde state
  POST /session/start       — manual wake bypass (long-press)
  POST /session/end         — finalize input (release)

Persistence: this daemon does NOT write the volume state file.
voice_daemon's debounced poller already catches external main_volume
changes (it watches Camilla's main_volume regardless of who set it),
so dial-driven changes converge into the same persistence path used
by voice tools and the moOde slider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

logger = logging.getLogger(__name__)
dial_log = logging.getLogger("jasper.dial")


# Same range jasper.tools.audio uses for the voice-driven volume tools.
# 0% = silent at the speaker; 100% = full digital scale.
VOLUME_MIN_DB = -50.0
VOLUME_MAX_DB = 0.0


def _clamp_db(db: float) -> float:
    return max(VOLUME_MIN_DB, min(VOLUME_MAX_DB, float(db)))


def _db_to_percent(db: float) -> int:
    span = VOLUME_MAX_DB - VOLUME_MIN_DB
    return max(0, min(100, round((float(db) - VOLUME_MIN_DB) / span * 100.0)))


class CamillaProxy:
    """Sync, thread-safe wrapper around pycamilladsp for the request
    handlers. pycamilladsp itself is sync; ThreadingHTTPServer hands
    each request its own thread, so we just need a lock to serialise
    websocket access (the underlying CamillaClient isn't reentrant).

    Reconnects on failure rather than raising — Camilla restarts
    happen out-of-band (e.g. moOde mode switches) and we don't want
    to bounce this daemon every time."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        # Imported lazily so tests using FakeCamilla don't need
        # pycamilladsp installed (it's a git dep, heavy to fetch).
        self._client: Any | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> Any:
        if self._client is None:
            from camilladsp import CamillaClient
            client = CamillaClient(self._host, self._port)
            client.connect()
            self._client = client
        return self._client

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        with self._lock:
            try:
                return fn(self._ensure())
            except Exception as e:  # noqa: BLE001
                logger.warning("camilla call failed, reconnecting: %s", e)
                self._client = None
                return fn(self._ensure())

    def get_volume_db(self) -> float:
        return float(self._call(lambda c: c.volume.main_volume()))

    def set_volume_db(self, db: float) -> float:
        clamped = _clamp_db(db)
        self._call(lambda c: c.volume.set_main_volume(clamped))
        return clamped

    def adjust_volume_db(self, delta_db: float) -> float:
        # Read-modify-write under one lock so concurrent encoder ticks
        # from a fast-spinning dial can't lose updates.
        def rmw(c: Any) -> float:
            current = float(c.volume.main_volume())
            target = _clamp_db(current + float(delta_db))
            c.volume.set_main_volume(target)
            return target
        return float(self._call(rmw))


async def _voice_socket_command(socket_path: str, cmd: str) -> dict:
    """Send one ASCII line to voice_daemon's control socket and return
    the parsed JSON response. Used by /session/start and /session/end
    so dial hold-to-talk drives the same session-state machine the
    wake word uses."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        writer.write((cmd + "\n").encode("ascii"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    if not line:
        raise RuntimeError("voice_daemon returned no response")
    return json.loads(line.decode("utf-8"))


async def _toggle_transport() -> dict:
    """Build moOde + Spotify-router clients in the current event loop,
    dispatch a 'toggle' transport action, then close. We rebuild per
    request because httpx's AsyncClient is loop-bound: a persistent
    instance would be tied to the first request's loop and error on
    every subsequent one. The cost is small (~50 ms) and dial clicks
    are rare."""
    # Import inside the function so jasper-control doesn't import the
    # full voice-daemon dependency tree at startup.
    from ..accounts import Registry, maybe_migrate_legacy
    from ..moode import MoodeClient
    from ..spotify_router import Router, build_clients
    from ..tools.transport import make_transport_dispatcher

    moode = MoodeClient(
        base_url=os.environ.get("MOODE_BASE_URL", "http://127.0.0.1"),
        mpd_host=os.environ.get("MPD_HOST", "127.0.0.1"),
        mpd_port=int(os.environ.get("MPD_PORT", "6600")),
    )
    router: Router | None = None
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if client_id and client_secret:
        accounts_path = os.environ.get(
            "JASPER_SPOTIFY_ACCOUNTS_PATH",
            "/var/lib/jasper/spotify/accounts.json",
        )
        legacy_cache = os.environ.get(
            "SPOTIFY_CACHE_PATH", "/var/lib/jasper/.spotify-cache",
        )
        redirect_uri = os.environ.get(
            "SPOTIFY_REDIRECT_URI",
            "https://jasper.local/spotify/callback",
        )
        accounts = Registry.load(accounts_path)
        maybe_migrate_legacy(accounts, legacy_cache, default_name="default")
        clients = build_clients(
            accounts,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
        if clients:
            router = Router(
                clients=clients, default_name=accounts.default_name,
            )

    try:
        dispatch = make_transport_dispatcher(moode, router)
        return await dispatch("toggle")
    finally:
        try:
            await moode.aclose()
        except Exception as e:  # noqa: BLE001
            logger.debug("moode.aclose() warning: %s", e)


def _make_handler(
    camilla: CamillaProxy, voice_socket_path: str,
) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return {}

        def _volume_payload(self, db: float) -> dict[str, Any]:
            return {"db": round(db, 3), "percent": _db_to_percent(db)}

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                self._send_json({"ok": True})
                return
            if self.path == "/volume":
                try:
                    db = camilla.get_volume_db()
                except Exception as e:  # noqa: BLE001
                    logger.exception("get volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json(self._volume_payload(db))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/volume/adjust":
                body = self._read_json()
                if "delta_db" not in body:
                    self._send_json({"error": "missing delta_db"}, status=400)
                    return
                try:
                    delta = float(body["delta_db"])
                except (TypeError, ValueError):
                    self._send_json({"error": "delta_db must be a number"}, status=400)
                    return
                try:
                    new_db = camilla.adjust_volume_db(delta)
                except Exception as e:  # noqa: BLE001
                    logger.exception("adjust volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json(self._volume_payload(new_db))
                return

            if self.path == "/volume/set":
                body = self._read_json()
                if "db" not in body:
                    self._send_json({"error": "missing db"}, status=400)
                    return
                try:
                    db = float(body["db"])
                except (TypeError, ValueError):
                    self._send_json({"error": "db must be a number"}, status=400)
                    return
                try:
                    new_db = camilla.set_volume_db(db)
                except Exception as e:  # noqa: BLE001
                    logger.exception("set volume failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                self._send_json(self._volume_payload(new_db))
                return

            if self.path == "/transport/toggle":
                try:
                    result = asyncio.run(_toggle_transport())
                except Exception as e:  # noqa: BLE001
                    logger.exception("transport toggle failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                if "error" in result:
                    self._send_json(result, status=502)
                    return
                self._send_json(result)
                return

            if self.path == "/session/start" or self.path == "/session/end":
                cmd = "START" if self.path.endswith("start") else "END"
                try:
                    result = asyncio.run(
                        _voice_socket_command(voice_socket_path, cmd),
                    )
                except FileNotFoundError:
                    self._send_json(
                        {"error": "voice_daemon not running (socket not found)"},
                        status=503,
                    )
                    return
                except (OSError, asyncio.TimeoutError) as e:
                    self._send_json(
                        {"error": f"voice_daemon unreachable: {e}"},
                        status=503,
                    )
                    return
                except Exception as e:  # noqa: BLE001
                    logger.exception("session %s failed", cmd)
                    self._send_json({"error": str(e)}, status=502)
                    return
                # Result codes from voice_daemon's manual_session_*:
                #   OK / BUSY / CAP / PAUSED / NO_SESSION / ALREADY_ENDED / ERROR
                # Map non-OK outcomes to non-2xx so the dial's HTTP
                # error path can show the right LED color.
                http_status = 200
                if result.get("result") not in ("OK", None):
                    if result.get("result") in ("CAP", "PAUSED"):
                        http_status = 503
                    elif result.get("result") in ("BUSY", "NO_SESSION", "ALREADY_ENDED"):
                        http_status = 409
                    else:
                        http_status = 502
                self._send_json(result, status=http_status)
                return

            self.send_error(HTTPStatus.NOT_FOUND)

    return Handler


def build_server(
    host: str,
    port: int,
    camilla: CamillaProxy,
    voice_socket_path: str = "/run/jasper/voice.sock",
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(
        (host, port),
        _make_handler(camilla, voice_socket_path),
    )


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

    t = threading.Thread(target=_loop, name="dial-log-listener", daemon=True)
    t.start()
    return t


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-control",
        description="HTTP control surface for the JTS speaker (dial, automation, etc.)",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_CONTROL_HOST", "0.0.0.0"),
        help="bind host (default 0.0.0.0 — LAN-reachable)",
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_CONTROL_PORT", "8780")),
    )
    parser.add_argument(
        "--camilla-host",
        default=os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--camilla-port", type=int,
        default=int(os.environ.get("JASPER_CAMILLA_PORT", "1234")),
    )
    parser.add_argument(
        "--dial-log-host",
        default=os.environ.get("JASPER_DIAL_LOG_HOST", "0.0.0.0"),
        help="bind host for the dial UDP log listener",
    )
    parser.add_argument(
        "--dial-log-port", type=int,
        default=int(os.environ.get("JASPER_DIAL_LOG_PORT", "5514")),
        help="UDP port for dial log datagrams (default 5514)",
    )
    parser.add_argument(
        "--voice-socket",
        default=os.environ.get(
            "JASPER_VOICE_CONTROL_SOCKET", "/run/jasper/voice.sock",
        ),
        help="path to voice_daemon's control UDS",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    camilla = CamillaProxy(args.camilla_host, args.camilla_port)
    server = build_server(args.host, args.port, camilla, args.voice_socket)
    run_dial_log_listener(args.dial_log_host, args.dial_log_port)
    logger.info(
        "jasper-control listening on http://%s:%d "
        "(camilla=%s:%d, dial-log=%s:%d/udp, voice=%s)",
        args.host, args.port,
        args.camilla_host, args.camilla_port,
        args.dial_log_host, args.dial_log_port,
        args.voice_socket,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
