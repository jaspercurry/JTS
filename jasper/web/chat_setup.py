"""Conversation-history dashboard at /chat/.

Read-only page shell plus JSON data endpoint for captured voice turns.
The writer is jasper-voice; this socket-activated web process opens the
SQLite store read-only and never creates or mutates the database.
"""
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

from ..conversation_history import ConversationStore, read_settings
from ._common import (
    begin_request,
    canonical_page,
    guard_read_request,
    send_html_response,
    send_proxy_json,
)

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
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
                turns = (
                    store.recent(limit, since_ts=since)
                    if stats is not None
                    else []
                )
                payload = {
                    "schema_version": 1,
                    "available": store.available and stats is not None,
                    "limit": limit,
                    "since": since,
                    "turns": [asdict(turn) for turn in turns],
                }
            finally:
                store.close()
            _json_response(self, payload)

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
