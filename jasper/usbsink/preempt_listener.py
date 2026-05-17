"""Localhost HTTP receiver for the mux preempt protocol.

jasper-mux POSTs to http://127.0.0.1:JASPER_USBSINK_PREEMPT_PORT/preempt
when it wants jasper-usbsink to silence/un-silence its output. We use
HTTP rather than a UNIX socket so the wire format is dead simple to
debug with curl and matches the existing jasper-control style.

Endpoint:

    POST /preempt   {"silenced": true|false}
        → 200  {"silenced": true|false, "applied": bool}

Persists the most-recent state to /run/jasper-usbsink/preempt.state so
a daemon restart picks back up where it left off (e.g. if AirPlay had
preempted USBSINK and the daemon was restarted mid-preempt, we don't
want USBSINK to flood audio on the next boot).

stdlib http.server in a worker thread — same pattern as the wizard
sub-processes. No async dance because the daemon's main loop is the
asyncio side and only needs to set/clear a bool on the bridge, which
is thread-safe.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


DEFAULT_PORT = 8781
DEFAULT_STATE_PATH = "/run/jasper-usbsink/preempt.state"


def _read_persisted_preempt(state_path: Path) -> bool:
    """Returns True if a previous run left us preempted. Missing or
    malformed file resolves to False — fail-safe direction: a missing
    persistence is "not preempted", because the user-visible failure
    mode of unintended silence after a restart is worse than a brief
    second of mixing during a preempt-then-restart."""
    try:
        with open(state_path) as f:
            data = json.load(f)
        return bool(data.get("silenced", False))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False


def _persist_preempt(state_path: Path, silenced: bool) -> None:
    """Atomic tempfile + os.replace, matching state_publisher.py's
    pattern."""
    payload = {"silenced": bool(silenced)}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".preempt.", suffix=".tmp",
        dir=str(state_path.parent),
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


class PreemptListener:
    """HTTP server in a worker thread. Exposes:

        POST /preempt   {"silenced": bool}   → bridge.set_preempted()
        GET  /preempt                        → current state (diagnostic)
    """

    def __init__(
        self,
        bridge,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        state_path: str = DEFAULT_STATE_PATH,
    ) -> None:
        self._bridge = bridge
        self._host = host
        self._port = port
        self._state_path = Path(state_path)
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        # Restore preempt state from prior run BEFORE binding the
        # port — that way the first incoming POST can't race with a
        # stale-state apply.
        prior = _read_persisted_preempt(self._state_path)
        if prior:
            self._bridge.set_preempted(True)
            logger.info(
                "event=usbsink.preempt_restored silenced=true "
                "from=%s", self._state_path,
            )

        bridge = self._bridge
        state_path = self._state_path

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                logger.debug("preempt http: %s", fmt % args)

            def _send_json(self, payload: dict, *, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") == "/preempt":
                    self._send_json({"silenced": bridge.is_preempted})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/preempt":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json({"error": "invalid JSON"}, status=400)
                    return
                if "silenced" not in body:
                    self._send_json(
                        {"error": "missing 'silenced' bool"},
                        status=400,
                    )
                    return
                want = bool(body["silenced"])
                bridge.set_preempted(want)
                try:
                    _persist_preempt(state_path, want)
                except OSError as e:
                    logger.warning(
                        "event=usbsink.preempt_persist_failed error=%s", e,
                    )
                logger.info(
                    "event=usbsink.preempt_received silenced=%s from=%s",
                    want, self.client_address[0],
                )
                self._send_json({"silenced": want, "applied": True})

        self._server = ThreadingHTTPServer((self._host, self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="usbsink-preempt-http",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "event=usbsink.preempt_listener_started host=%s port=%d",
            self._host, self._port,
        )

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
        logger.info("event=usbsink.preempt_listener_stopped")
