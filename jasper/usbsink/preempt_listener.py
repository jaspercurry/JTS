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

import concurrent.futures
import json
import logging
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Optional

from jasper.log_event import log_event

logger = logging.getLogger(__name__)


DEFAULT_PORT = 8781
DEFAULT_STATE_PATH = "/run/jasper-usbsink/preempt.state"

# Bounded concurrency for the preempt HTTP endpoint. The only known
# client is jasper-mux on the same host (one POST per source-state
# transition, well under 1 Hz). Stdlib ThreadingHTTPServer spawns a
# thread per request with no upper bound; a buggy or hostile client
# (or just a port-scanner) could exhaust the daemon's fd / thread
# budget. Four workers is generous defense in depth without adding
# meaningful RAM.
PREEMPT_MAX_WORKERS = 4

# Per-request socket timeout. A client that connects but never finishes
# sending a request body would otherwise tie up a worker until the OS
# closed the half-open socket (minutes). 2 s is far longer than mux's
# httpx call needs (it has its own 2 s timeout) but short enough to
# free up workers under attack/bug pressure.
PREEMPT_REQUEST_TIMEOUT_SEC = 2.0


class _BoundedThreadingHTTPServer(HTTPServer):
    """HTTP server with a bounded thread pool for request handling.

    Drop-in replacement for `ThreadingHTTPServer` that caps in-flight
    handlers at `max_workers` and applies a read timeout to each
    accepted socket. Excess concurrent connections queue on accept()
    until a worker is free.
    """

    daemon_threads = True

    def __init__(
        self,
        *args: Any,
        max_workers: int = PREEMPT_MAX_WORKERS,
        request_timeout_sec: float = PREEMPT_REQUEST_TIMEOUT_SEC,
        **kwargs: Any,
    ) -> None:
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="usbsink-preempt",
        )
        self._request_timeout_sec = request_timeout_sec
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            # HTTPServer.__init__ calls server_close() if bind/activate
            # fails. Since this subclass owns an executor, make sure a
            # bind error reports the original OSError rather than being
            # masked by cleanup.
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise

    def process_request(self, request: Any, client_address: Any) -> None:
        # Apply read timeout BEFORE submitting to the pool so a slow
        # client can't tie up its worker on the initial recv.
        try:
            request.settimeout(self._request_timeout_sec)
        except OSError:
            # Socket already closed between accept() and here. The
            # executor will hit the same error and shutdown_request
            # will clean up.
            pass
        self._executor.submit(self._handle_in_pool, request, client_address)

    def _handle_in_pool(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: BLE001
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def server_close(self) -> None:
        super().server_close()
        # Don't wait — daemon threads exit when the process does; we
        # don't want stop() to block on a slow client.
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False, cancel_futures=True)


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
        self._server: Optional[_BoundedThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        # Restore preempt state from prior run BEFORE binding the
        # port — that way the first incoming POST can't race with a
        # stale-state apply.
        prior = _read_persisted_preempt(self._state_path)
        if prior:
            self._bridge.set_preempted(True)
            log_event(
                logger,
                "usbsink.preempt_restored",
                silenced="true",
                **{"from": self._state_path},
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
                    log_event(
                        logger,
                        "usbsink.preempt_persist_failed",
                        error=e,
                        level=logging.WARNING,
                    )
                log_event(
                    logger,
                    "usbsink.preempt_received",
                    silenced=want,
                    **{"from": self.client_address[0]},
                )
                self._send_json({"silenced": want, "applied": True})

        # Bind with SO_REUSEADDR set so a previous crashed instance's
        # socket in TIME_WAIT doesn't keep us out for ~60 s after a
        # restart. Without this, systemd's Restart=on-failure loop
        # hits StartLimitBurst and parks the daemon as failed —
        # operator has to `systemctl reset-failed jasper-usbsink`.
        # _BoundedThreadingHTTPServer inherits `allow_reuse_address`
        # from HTTPServer, which sets SO_REUSEADDR in the parent's
        # server_bind. We just need to enable it (default is True on
        # http.server.HTTPServer since 3.x, but make it explicit so
        # future maintainers know we depend on it).
        _BoundedThreadingHTTPServer.allow_reuse_address = True

        try:
            self._server = _BoundedThreadingHTTPServer(
                (self._host, self._port), Handler,
            )
        except OSError as e:
            # Port still in use even with REUSEADDR (e.g. an actual
            # other process owns it). Log loudly so the operator sees
            # the diagnostic in `journalctl -u jasper-usbsink`, and
            # re-raise so the daemon's startup cleanups unwind. The
            # cleanups list pattern means the already-restored preempt
            # state via `_read_persisted_preempt` is harmless; nothing
            # else is leaked.
            log_event(
                logger,
                "usbsink.preempt_listener_bind_failed",
                host=self._host,
                port=self._port,
                error=e,
                note=(
                    "another process holds the port; check with "
                    f"`ss -tlnp sport = :{self._port}` then "
                    "`systemctl reset-failed jasper-usbsink` after freeing it"
                ),
                level=logging.ERROR,
            )
            raise

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="usbsink-preempt-http",
            daemon=True,
        )
        self._thread.start()
        log_event(
            logger,
            "usbsink.preempt_listener_started",
            host=self._host,
            port=self._port,
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
        log_event(logger, "usbsink.preempt_listener_stopped")
