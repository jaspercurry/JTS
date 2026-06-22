# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""System dashboard at /system/.

Read-only(ish) view of what the speaker is doing — RAM/CPU/temp/disk
with 60-min sparklines, software version, network + renderer state,
and a few action buttons (restart voice / audio / reboot, run
diagnostics). Voice spend status and cap settings live on /voice/.

Data comes from jasper-control:
  GET  /system/snapshot     metrics + build (5 s ring buffer)
  GET  /system/diagnostics  serves cached jasper-doctor JSON and
                             refreshes stale snapshots in the background
  POST /system/restart/*    restart voice / audio chain
  POST /system/reboot       full Pi reboot

Wake detection lives on /wake/ — the model picker, the AEC + per-leg
toggles, and the sensitivity slider all share that page now since they
share a restart cycle. /system/ no longer carries an AEC card.

This wizard's job is to render the page shell and proxy the JSON. The UI
itself is the canonical design system: `canonical_page()` emits the shared
/assets/app.css link + CSRF meta + icon sprite, and the page's behaviour
lives in static ES modules under /assets/system-status/js/ (served +
revalidated by nginx). Polling is client-side (fetch /data.json every 5 s);
the server keeps a thin proxy connection to jasper-control on
127.0.0.1:8780.

Socket-activated like the other wizards, with a longer idle window
(30 min) since a power user may leave the dashboard open in a tab
for monitoring. Idle exit + cold-start still apply.
"""
from __future__ import annotations

import argparse
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ._common import (
    DEFAULT_CONTROL_BASE,
    begin_request,
    canonical_page,
    forward_control_token_headers,
    proxy_get,
    proxy_post,
    reject_csrf,
    send_html_response,
    send_proxy_json,
    guard_read_request,
    guard_mutating_request,
)

logger = logging.getLogger(__name__)


# Longer than the other wizards' 10-min default. The dashboard is a
# monitoring surface; some users will leave it open in a tab. 30 min
# strikes a balance between not respawning constantly + not lingering
# resident forever.
IDLE_SHUTDOWN_SEC = 1800.0


def _render_page(csrf_token: str = "") -> bytes:
    # The page renders entirely client-side from /system/snapshot, so the
    # body is just a mount point plus the ES module entry. canonical_page
    # emits the shared app.css link, the CSRF meta tag (read by main.js for
    # mutating POSTs), and the icon sprite. The module graph is served
    # static + revalidated from /assets/system-status/js/ (see the
    # `location ~ \\.js$` block in deploy/nginx-jasper.conf).
    body = (
        # A visible placeholder inside the mount point: buildPage() replaces
        # it on first render, so if the ES module graph ever fails to load
        # the page shows "Loading…" rather than a silent blank.
        '<div id="app" aria-busy="true">'
        '<p class="boot-note">Loading the dashboard…</p>'
        '</div>\n'
        '<script type="module" src="/assets/system-status/js/main.js"></script>'
    )
    return canonical_page(
        "System", body, csrf_token=csrf_token,
        page_css_href="/assets/system-status/system.css",
    )


def _make_handler(
    control_base: str = DEFAULT_CONTROL_BASE,
) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /system/ prefix so we see paths like
            # "/" and "/data.json".
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _render_page(ctx["csrf_token"]))
                return
            if path == "/data.json":
                if not guard_read_request(self):
                    return
                status, body = proxy_get(
                    "/system/snapshot", control_base=control_base,
                )
                send_proxy_json(self, body, status=status)
                return
            if path == "/diagnostics.json":
                if not guard_read_request(self):
                    return
                status, body = proxy_get(
                    "/system/diagnostics",
                    control_base=control_base, timeout=30.0,
                )
                send_proxy_json(self, body, status=status)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            POST_ROUTES = (
                "/restart/voice", "/restart/audio", "/reboot", "/poweroff",
                "/audio-quality",
            )
            if path not in POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            body = None
            if path == "/audio-quality":
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self.send_error(HTTPStatus.BAD_REQUEST)
                    return
                if length < 0 or length > 4096:
                    self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                body = self.rfile.read(length) if length else b"{}"
            # Forward a browser-supplied X-JTS-Token so the opt-in
            # control-token gate sees it on /system/reboot|poweroff (the
            # wizard proxies server-side; the header can't ride the browser
            # fetch otherwise).
            status, body = proxy_post(
                "/system" + path, control_base=control_base, body=body,
                headers=forward_control_token_headers(self),
            )
            send_proxy_json(self, body, status=status)

    return Handler


def make_server(target, *, control_base: str = DEFAULT_CONTROL_BASE) -> ThreadingHTTPServer:
    """Build the dashboard server. `target` is a socket / (host, port)
    tuple / int port per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(target, _make_handler(control_base))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-system-web",
        description="System dashboard at /system/ for the JTS speaker",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_SYSTEM_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SYSTEM_WEB_PORT", "8772")),
    )
    parser.add_argument(
        "--control-base",
        default=os.environ.get(
            "JASPER_CONTROL_BASE", DEFAULT_CONTROL_BASE,
        ),
        help="jasper-control HTTP base URL (default 127.0.0.1:8780)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from . import _systemd
    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target, control_base=args.control_base)

    handler_cls = server.RequestHandlerClass
    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-system-web adopting systemd fd (control=%s, idle=%ds)",
            args.control_base, int(IDLE_SHUTDOWN_SEC),
        )
    else:
        logger.info(
            "jasper-system-web listening on http://%s:%d (control=%s)",
            args.host, args.port, args.control_base,
        )

    _systemd.notify_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    _systemd.notify_stopping()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
