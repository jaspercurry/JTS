# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conversation-history dashboard and household controls at /assistant/chat/."""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..conversation_history import ConversationStore, read_settings, write_settings
from ._common import (
    JsonBodyError,
    begin_request,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    json_body,
    read_json_object,
    reject_csrf,
    route_path,
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
        '<p class="status-line status-line--boot">Loading conversation history...</p>'
        '</div>\n'
        '<script type="module" src="/assets/chat/js/main.js"></script>'
    )
    return canonical_page(
        "Chat history",
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


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict[str, Any] | None:
        """Parse the request body, answering 400 and returning None on a
        malformed one so `json_body` never dispatches it to a route."""
        try:
            return read_json_object(self, max_bytes=MAX_JSON_BYTES)
        except JsonBodyError as exc:
            if exc.code == "invalid_content_length":
                message = "invalid content length"
            elif exc.code in {"negative_content_length", "body_too_large"}:
                message = "request too large"
            elif exc.code == "non_object":
                message = "JSON body must be an object"
            else:
                message = "invalid JSON body"
            _json_response(self, {"error": message}, status=HTTPStatus.BAD_REQUEST)
            return None

    # nginx strips the /assistant/chat/ prefix so we see "/" and "/data.json".
    def do_GET(self) -> None:  # noqa: N802
        handler_fn = _GET_ROUTES.get(route_path(self.path))
        if handler_fn is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not guard_read_request(self):
            return
        handler_fn(self)

    def do_POST(self) -> None:  # noqa: N802
        handler_fn = _POST_ROUTES.get(route_path(self.path))
        if handler_fn is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not guard_mutating_request(self):
            reject_csrf(self)
            return
        handler_fn(self)


def _get_index(handler: _Handler) -> None:
    ctx = begin_request(handler)
    send_html_response(handler, _render_page(ctx["csrf_token"]))


def _get_data(handler: _Handler) -> None:
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(handler.path).query, keep_blank_values=True,
    )
    limit = _parse_limit(query)
    if limit is None:
        _json_response(
            handler,
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
        turns = store.recent(limit, since_ts=since) if stats is not None else []
        payload = {
            "schema_version": 1,
            "capture_enabled": settings.capture_enabled,
            "available": available,
            "limit": limit,
            "since": since,
            "stats": asdict(stats) if stats is not None else None,
            "retention": settings.retention,
            "turns": [asdict(turn) for turn in turns],
        }
    finally:
        store.close()
    _json_response(handler, payload)


@json_body
def _post_capture(handler: _Handler, body: dict[str, Any]) -> None:
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        _json_response(
            handler,
            {"error": "enabled must be true or false"},
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    stats = None
    if enabled:
        candidate = read_settings()
        store = ConversationStore(candidate.db_path)
        try:
            stats = store.stats()
            if not store.available or stats is None:
                _json_response(
                    handler,
                    {
                        "error": (
                            "conversation-history store could not be initialized"
                        ),
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
        finally:
            store.close()
    try:
        settings = write_settings(capture_enabled=enabled)
    except (OSError, ValueError) as e:
        logger.exception("could not write conversation-history settings")
        _json_response(
            handler,
            {"error": f"could not save settings: {e}"},
            status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return
    _json_response(
        handler,
        {
            "ok": True,
            "capture_enabled": settings.capture_enabled,
            "stats": asdict(stats) if stats is not None else None,
            "retention": settings.retention,
        },
    )


def _post_clear(handler: _Handler) -> None:
    settings = read_settings()
    store = ConversationStore(settings.db_path)
    try:
        if not store.available:
            _json_response(
                handler,
                {"error": "conversation-history store is unavailable"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        deleted = store.clear()
        stats = store.stats()
    finally:
        store.close()
    _json_response(
        handler,
        {
            "ok": True,
            "deleted": deleted,
            "capture_enabled": settings.capture_enabled,
            "stats": asdict(stats) if stats is not None else None,
        },
    )


# ORDERING IS LOAD-BEARING: each dispatcher looks the route up first, so an
# unknown path 404s before the read/CSRF guard runs.
_GET_ROUTES = {"/": _get_index, "/data.json": _get_data}
_POST_ROUTES = {"/capture": _post_capture, "/clear": _post_clear}


def _make_handler() -> type[BaseHTTPRequestHandler]:
    return _Handler


def make_server(target) -> ThreadingHTTPServer:
    """Build the /chat server.

    ``target`` is a socket / ``(host, port)`` tuple / int port per
    ``systemd.make_http_server``'s contract.
    """
    from ..platform import systemd

    return systemd.make_http_server(target, _make_handler())


def main(argv: list[str] | None = None) -> int:
    from . import _wizard_cli

    return _wizard_cli.run_wizard_cli(
        "jasper-chat-web",
        "Conversation history dashboard at /assistant/chat/ for JTS",
        8787,
        argv,
        make_server=make_server,
        detail=lambda _args: f"idle={int(IDLE_SHUTDOWN_SEC)}s",
        idle_threshold_sec=IDLE_SHUTDOWN_SEC,
    )


if __name__ == "__main__":
    raise SystemExit(main())
