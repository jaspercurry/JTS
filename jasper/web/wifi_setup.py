# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/wifi/ — HTTP and page rendering for NetworkManager Wi-Fi operations."""
from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jasper.net import wifi
from jasper.platform import systemd
from jasper.platform.log_event import log_event
from ._common import (
    JsonBodyError, begin_request, dispatch_get, dispatch_post, json_body,
    read_json_object, route_path, send_html_response, send_json_response,
)
from .chrome import canonical_header, canonical_page

logger = logging.getLogger(__name__)

# Direct-socket request ceiling; nginx has its own body limit.
_JSON_BODY_LIMIT = 100_000


def _landing_html(csrf_token: str = "") -> bytes:
    """Render the /wifi/ page on the canonical design system.

    The page is live (fetch-driven): a 7 s ``./state`` poll, on-demand
    ``./scan``, and inline Connect / Forget panels that POST to
    ``./connect`` / ``./forget`` / ``./radio``. So this returns only the
    static shell — the current-network slot, the scan list, the join-by-name
    fields, and the saved-networks collapse — and the behaviour ships as the
    ES module ``/assets/wifi/js/main.js`` (which reads the CSRF token from the
    ``<meta name="jts-csrf">`` tag ``canonical_page()`` emits and attaches it
    to every mutating POST via the shared ``jsonHeaders()``).

    There is no server-rendered ``<form>`` here and no flash banner: status is
    surfaced inline by the module, not via the PRG flash cookie. Page-specific
    styling lives in ``/assets/wifi/wifi.css``; shared primitives come from
    ``app.css``."""
    body = f"""
{canonical_header("Wi-Fi")}
<main class="page">
  <p class="form-hint">Switch the speaker's Wi-Fi network or manage saved
  networks. Changes take effect immediately.</p>

  <div id="current"></div>

  <div class="wifi-region">
    <h2 class="eyebrow">Available networks</h2>
    <button id="scan-btn" class="btn btn--ghost" data-action="rescan">Scan</button>
  </div>
  <div id="scan-health"></div>
  <div class="net-list" id="avail-list">
    <div class="empty">Tap Scan to look for nearby networks.</div>
  </div>

  <details class="disclosure join-by-name">
    <summary>Join by name</summary>
    <div class="disclosure__body manual-fields">
      <div class="field">
        <label for="manual-ssid">Network name</label>
        <input id="manual-ssid" type="text" autocomplete="off"
               autocapitalize="off" spellcheck="false">
      </div>
      <div class="field">
        <label for="manual-password">Password</label>
        <input id="manual-password" type="password" autocomplete="off"
               autocapitalize="off" spellcheck="false">
        <span class="show-pw" data-action="toggle-manual-pw">Show password</span>
        <label class="manual-check" for="manual-hidden">
          <input id="manual-hidden" type="checkbox">
          Hidden network
        </label>
      </div>
      <div id="manual-result"></div>
      <div class="form-actions">
        <button id="manual-connect-btn" class="btn btn--primary"
                data-action="submit-manual">Connect</button>
      </div>
    </div>
  </details>

  <details class="disclosure saved-networks">
    <summary>Saved networks <span class="saved-count" id="saved-count"></span></summary>
    <div class="disclosure__body">
      <div class="net-list" id="saved-list">
        <div class="empty">Loading…</div>
      </div>
    </div>
  </details>
</main>
<script type="module" src="/assets/wifi/js/main.js"></script>
"""
    return canonical_page(
        "Wi-Fi", body,
        csrf_token=csrf_token,
        page_css_href="/assets/wifi/wifi.css",
    )




class _Handler(BaseHTTPRequestHandler):
    # Write-once latch for the POST failure fallback below: a route that has
    # already answered must never have a second body appended to it. Reset
    # per request (the handler instance is reused across keep-alive).
    _json_response_started = False

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_html(self, body: bytes, *, status: int = 200) -> None:
        send_html_response(self, body, status=status)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        if self._json_response_started:
            raise RuntimeError("response already committed")
        self._json_response_started = True
        send_json_response(self, payload, status=status)

    def _read_json(self) -> dict[str, Any]:
        try:
            return read_json_object(self, max_bytes=_JSON_BODY_LIMIT)
        except (JsonBodyError, OSError):
            return {}

    def do_GET(self) -> None:  # noqa: N802
        self._json_response_started = False
        dispatch_get(self, _GET_ROUTES)

    def do_POST(self) -> None:  # noqa: N802
        self._json_response_started = False
        try:
            dispatch_post(self, _POST_ROUTES, guard="header")
        except Exception as e:  # noqa: BLE001
            # A failure after the route answered is a transport failure on a
            # response already on the wire; re-raise rather than write a
            # second body over it.
            if self._json_response_started:
                raise
            log_event(
                logger,
                "wifi.post_dispatch_failed",
                action=route_path(self.path).removeprefix("/"),
                error=type(e).__name__,
                ok=False,
                client=self.address_string(),
                level=logging.ERROR,
            )
            self._send_json(
                {"ok": False, "message": "Wi-Fi action failed"}, status=502,
            )


def _get_index(handler: _Handler) -> None:
    ctx = begin_request(handler)
    handler._send_html(_landing_html(ctx["csrf_token"]))


def _get_state(handler: _Handler) -> None:
    try:
        payload = wifi.gather_state()
        status = 200
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "wifi.state_failed",
            error=type(e).__name__,
            level=logging.ERROR,
            exc_info=True,
        )
        payload = {"error": str(e)}
        status = 502
    handler._send_json(payload, status=status)


def _post_scan(handler: _Handler) -> None:
    handler._send_json(wifi.scan_networks_report())


@json_body
def _post_connect(handler: _Handler, body: dict[str, Any]) -> None:
    ssid = (body.get("ssid") or "").strip()
    name = (body.get("name") or "").strip()
    password = body.get("password")
    hidden = bool(body.get("hidden"))
    if ssid:
        try:
            ok, msg = wifi.connect_new(ssid, password, hidden=hidden)
        except wifi.InvalidPassword as exc:
            handler._send_json({"ok": False, "message": str(exc)}, status=400)
            return
    elif name:
        ok, msg = wifi.connect_saved(name)
    else:
        handler._send_json(
            {"ok": False, "message": "ssid or name required"}, status=400,
        )
        return
    log_event(
        logger,
        "wifi.connect",
        fields=(
            {"mode": "new", "ssid": ssid}
            if ssid
            else {"mode": "saved", "profile": name}
        ),
        ok=ok,
        client=handler.address_string(),
        level=logging.INFO if ok else logging.WARNING,
    )
    handler._send_json({"ok": ok, "message": msg}, status=200 if ok else 502)


@json_body
def _post_forget(handler: _Handler, body: dict[str, Any]) -> None:
    name = (body.get("name") or "").strip()
    if not name:
        handler._send_json({"ok": False, "message": "name required"}, status=400)
        return
    ok, msg = wifi.forget(name)
    log_event(
        logger,
        "wifi.forget",
        profile=name,
        ok=ok,
        client=handler.address_string(),
        level=logging.INFO if ok else logging.WARNING,
    )
    handler._send_json({"ok": ok, "message": msg}, status=200 if ok else 502)


@json_body
def _post_radio(handler: _Handler, body: dict[str, Any]) -> None:
    if type(body.get("on")) is not bool:
        handler._send_json(
            {"ok": False, "message": "on must be a boolean"}, status=400,
        )
        return
    on = body["on"]
    ok, msg = wifi.set_radio(on)
    log_event(
        logger,
        "wifi.radio",
        enabled=on,
        ok=ok,
        client=handler.address_string(),
        level=logging.INFO if ok else logging.WARNING,
    )
    handler._send_json({"ok": ok, "message": msg}, status=200 if ok else 502)


# The tables are module-level, since no per-server state is captured here.
_GET_ROUTES = {"/": _get_index, "/state": _get_state}
_POST_ROUTES = {
    "/scan": _post_scan,
    "/connect": _post_connect,
    "/forget": _post_forget,
    "/radio": _post_radio,
}


def _make_handler() -> type[BaseHTTPRequestHandler]:
    return _Handler


def make_server(target) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per systemd.make_http_server's contract."""
    return systemd.make_http_server(target, _make_handler())
