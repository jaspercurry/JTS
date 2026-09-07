# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Status dashboard at /system/ (System) and /system/audio/ (Audio).

Read-only(ish) view of what the speaker is doing — RAM/CPU/temp/disk
with 60-min sparklines, software version, network + renderer state,
and a few action buttons (restart voice / audio / reboot, run
diagnostics). Voice spend status and cap settings live on /voice/.

Data comes from jasper-control:
  GET  /system/snapshot     metrics + build (5 s ring buffer)
  GET  /system/diagnostics  serves cached jasper-doctor JSON and
                             refreshes stale snapshots in the background
  GET  /aec/enhanced-aec     enhanced AEC installation state, proxied to the
                             browser as /optional-features/enhanced-aec
  POST /aec/enhanced-aec/install
                             start or retry the background installation,
                             proxied from the matching browser route
  POST /system/restart/*    restart voice / audio chain
  POST /usb-forensics       persistent sampler toggle / capture / USB repair
  POST /system/reboot       full Pi reboot

Wake detection lives on /wake/ — the model picker, the AEC + per-leg
toggles, and the sensitivity slider all share that page now since they
share a restart cycle. /system/ carries no operational AEC controls; its
Software card only offers the optional enhanced-engine installation.

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


def _render_page(csrf_token: str = "", *, view: str = "system") -> bytes:
    view = "audio" if view == "audio" else "system"
    # The page renders entirely client-side from /system/snapshot, so the
    # body is just a mount point plus the ES module entry. canonical_page
    # emits the shared app.css link, the CSRF meta tag (read by main.js for
    # mutating POSTs), and the icon sprite. The module graph is served
    # static + revalidated from /assets/system-status/js/ (see the
    # `location ~ \\.js$` block in deploy/nginx-jasper.conf).
    body = (
        # A visible placeholder inside the mount point: main.js replaces
        # it on first render, so if the ES module graph ever fails to load
        # the page shows "Loading…" rather than a silent blank.
        f'<div id="app" data-view="{view}" aria-busy="true">'
        '<p class="status-line status-line--boot">Loading the dashboard…</p>'
        '</div>\n'
        '<script type="module" src="/assets/system-status/js/main.js"></script>'
    )
    return canonical_page(
        "Status", body, csrf_token=csrf_token,
        page_css_href="/assets/system-status/system.css",
    )


def _make_handler(
    control_base: str = DEFAULT_CONTROL_BASE,
) -> type[BaseHTTPRequestHandler]:
    # do_GET / do_POST dispatch via the _GET_ROUTES / _POST_ROUTES tables
    # (exact path -> handler callable). The tables stay local to this
    # closure (rather than module-level) so the handlers can close over
    # `control_base`, same as this function has always done.
    def _get_index(handler: BaseHTTPRequestHandler, path: str) -> None:
        ctx = begin_request(handler)
        send_html_response(
            handler,
            _render_page(
                ctx["csrf_token"],
                view="audio" if path == "/audio" else "system",
            ),
        )

    def _get_data(handler: BaseHTTPRequestHandler) -> None:
        status, body = proxy_get("/system/snapshot", control_base=control_base)
        send_proxy_json(handler, body, status=status)

    def _get_diagnostics(handler: BaseHTTPRequestHandler) -> None:
        status, body = proxy_get(
            "/system/diagnostics", control_base=control_base, timeout=30.0,
        )
        send_proxy_json(handler, body, status=status)

    def _get_enhanced_aec(handler: BaseHTTPRequestHandler) -> None:
        status, body = proxy_get(
            "/aec/enhanced-aec", control_base=control_base, timeout=5.0,
        )
        send_proxy_json(handler, body, status=status)

    def _post_proxy(handler: BaseHTTPRequestHandler, path: str) -> None:
        body = None
        if path in (
            "/audio-quality", "/usb-latency", "/usb-forensics",
            "/optional-features/enhanced-aec/install",
        ):
            try:
                length = int(handler.headers.get("Content-Length") or "0")
            except ValueError:
                handler.send_error(HTTPStatus.BAD_REQUEST)
                return
            if length < 0 or length > 4096:
                handler.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = handler.rfile.read(length) if length else b"{}"
        # Forward a browser-supplied X-JTS-Token so the opt-in
        # control-token gate sees it on /system/reboot|poweroff (the
        # wizard proxies server-side; the header can't ride the browser
        # fetch otherwise).
        if path in (
            "/usb-forensics",
        ):
            control_path = path
        elif path == "/optional-features/enhanced-aec/install":
            control_path = "/aec/enhanced-aec/install"
        else:
            control_path = "/system" + path
        status, body = proxy_post(
            control_path, control_base=control_base, body=body,
            headers=forward_control_token_headers(handler),
            timeout=120.0 if path == "/usb-latency" else 5.0,
        )
        send_proxy_json(handler, body, status=status)

    _GET_ROUTES = {
        "/": _get_index,
        "/audio": _get_index,
        "/data.json": lambda h, p: _get_data(h),
        "/diagnostics.json": lambda h, p: _get_diagnostics(h),
        "/optional-features/enhanced-aec": lambda h, p: _get_enhanced_aec(h),
    }
    _POST_ROUTES = {
        "/restart/voice": _post_proxy,
        "/restart/audio": _post_proxy,
        "/reboot": _post_proxy,
        "/poweroff": _post_proxy,
        "/audio-quality": _post_proxy,
        "/usb-latency": _post_proxy,
        "/usb-forensics": _post_proxy,
        "/optional-features/enhanced-aec/install": _post_proxy,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /system/ prefix so we see paths like
            # "/" and "/data.json".
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            handler_fn = _GET_ROUTES.get(path)
            if handler_fn is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_read_request(self):
                return
            handler_fn(self, path)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            handler_fn = _POST_ROUTES.get(path)
            if handler_fn is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            handler_fn(self, path)

    return Handler


def make_server(target, *, control_base: str = DEFAULT_CONTROL_BASE) -> ThreadingHTTPServer:
    """Build the dashboard server. `target` is a socket / (host, port)
    tuple / int port per systemd.make_http_server's contract."""
    from ..platform import systemd
    return systemd.make_http_server(target, _make_handler(control_base))


def main(argv: list[str] | None = None) -> int:
    from . import _wizard_cli

    return _wizard_cli.run_wizard_cli(
        "jasper-system-web",
        "Status dashboard at /system/ and /system/audio/ for JTS",
        8772,
        argv,
        make_server=make_server,
        extra=lambda parser: parser.add_argument(
            "--control-base",
            default=os.environ.get(
                "JASPER_CONTROL_BASE", DEFAULT_CONTROL_BASE,
            ),
            help="jasper-control HTTP base URL (default 127.0.0.1:8780)",
        ),
        start=lambda args, _tracker: {"control_base": args.control_base},
        detail=lambda args: (
            f"control={args.control_base}, idle={int(IDLE_SHUTDOWN_SEC)}s"
        ),
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )


if __name__ == "__main__":
    raise SystemExit(main())
