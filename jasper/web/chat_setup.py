# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conversation-history dashboard and household controls at /chat/."""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..conversation_history import ConversationStore, read_settings, write_settings
from ._common import (
    begin_request,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    reject_csrf,
    send_html_response,
    send_proxy_json,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_JSON_BYTES = 4096
IDLE_SHUTDOWN_SEC = 1800.0


def _render_page(csrf_token: str = "") -> bytes:
    body = (
        '<div id="app" aria-busy="true">'
        '<p class="boot-note">Loading conversation history...</p>'
        '</div>\n'
        '<script type="module" src="/assets/chat/js/main.js"></script>'
    )
    return canonical_page(
        "Chat",
        body,
        csrf_token=csrf_token,
        page_css_href="/assets/chat/chat.css",
    )


def _json_response(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: int = HTTPStatus.OK,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    send_proxy_json(handler, body, status=int(status))


def _parse_limit(query: dict[str, list[str]]) -> int | None:
    raw = query.get("limit", [""])[0].strip()
    if not raw:
        return DEFAULT_LIMIT
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    return max(0, min(value, MAX_LIMIT))


def _make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /chat/ prefix so we see "/" and "/data.json".
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
                self._send_data(url.query)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in ("/capture", "/clear"):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if path == "/capture":
                self._set_capture()
                return
            if path == "/clear":
                self._clear_history()
                return

        def _read_json(self) -> tuple[dict[str, Any] | None, str | None]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                return None, "invalid content length"
            if length < 0 or length > MAX_JSON_BYTES:
                return None, "request too large"
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, "invalid JSON body"
            if not isinstance(parsed, dict):
                return None, "JSON body must be an object"
            return parsed, None

        def _send_data(self, raw_query: str) -> None:
            query = urllib.parse.parse_qs(raw_query, keep_blank_values=True)
            limit = _parse_limit(query)
            if limit is None:
                _json_response(
                    self,
                    {"error": "limit must be an integer"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            since = (query.get("since", [""])[0].strip() or None)
            settings = read_settings()
            store = ConversationStore(
                settings.db_path,
                read_only=True,
                warn_unavailable=False,
            )
            try:
                stats = store.stats()
                available = store.available and stats is not None
                turns = (
                    store.recent(limit, since_ts=since)
                    if stats is not None
                    else []
                )
                payload = {
                    "schema_version": 1,
                    "capture_enabled": settings.capture_enabled,
                    "available": available,
                    "limit": limit,
                    "since": since,
                    "stats": (
                        asdict(stats)
                        if stats is not None
                        else None
                    ),
                    "retention": settings.retention,
                    "turns": [asdict(turn) for turn in turns],
                }
            finally:
                store.close()
            _json_response(self, payload)

        def _set_capture(self) -> None:
            body, err = self._read_json()
            if err is not None:
                _json_response(self, {"error": err}, status=HTTPStatus.BAD_REQUEST)
                return
            assert body is not None
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                _json_response(
                    self,
                    {"error": "enabled must be true or false"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                settings = write_settings(capture_enabled=enabled)
            except (OSError, ValueError) as e:
                logger.exception("could not write conversation-history settings")
                _json_response(
                    self,
                    {"error": f"could not save settings: {e}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            stats = None
            if enabled:
                store = ConversationStore(settings.db_path)
                try:
                    stats = store.stats()
                    if not store.available or stats is None:
                        _json_response(
                            self,
                            {
                                "error": (
                                    "conversation-history store could not be "
                                    "initialized"
                                ),
                            },
                            status=HTTPStatus.INTERNAL_SERVER_ERROR,
                        )
                        return
                finally:
                    store.close()
            _json_response(
                self,
                {
                    "ok": True,
                    "capture_enabled": settings.capture_enabled,
                    "stats": asdict(stats) if stats is not None else None,
                    "retention": settings.retention,
                },
            )

        def _clear_history(self) -> None:
            settings = read_settings()
            store = ConversationStore(settings.db_path)
            try:
                if not store.available:
                    _json_response(
                        self,
                        {"error": "conversation-history store is unavailable"},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                deleted = store.clear()
                stats = store.stats()
            finally:
                store.close()
            _json_response(
                self,
                {
                    "ok": True,
                    "deleted": deleted,
                    "capture_enabled": settings.capture_enabled,
                    "stats": asdict(stats) if stats is not None else None,
                },
            )

    return Handler


def make_server(target) -> ThreadingHTTPServer:
    """Build the /chat server.

    ``target`` is a socket / ``(host, port)`` tuple / int port per
    ``_systemd.make_http_server``'s contract.
    """
    from . import _systemd

    return _systemd.make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-chat-web",
        description="Conversation history dashboard at /chat/ for JTS",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_CHAT_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("JASPER_CHAT_WEB_PORT", "8787")),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from . import _systemd

    sockets = _systemd.adopt_systemd_sockets()
    target = sockets[0] if sockets else (args.host, args.port)
    server = make_server(target)

    handler_cls = server.RequestHandlerClass
    tracker = _systemd.IdleShutdownTracker(
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )
    _systemd.install_request_idle_bump(handler_cls, tracker)
    tracker.start()

    if sockets:
        logger.info(
            "jasper-chat-web adopting systemd fd (idle=%ds)",
            int(IDLE_SHUTDOWN_SEC),
        )
    else:
        logger.info(
            "jasper-chat-web listening on http://%s:%d",
            args.host,
            args.port,
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
