"""Tool catalog wizard at /tools/.

Browse + search the first-party voice tools and turn each on/off.
"Add" = enable, "remove" = disable. No install-from-store (that is the
later marketplace). A tool whose backend isn't configured shows a
"needs setup" state linking to its setup wizard.

This page only READS /run/jasper/tools.json (written by jasper-voice at
startup) and writes the disabled-set to /var/lib/jasper/tool_state.env.
It does NOT import jasper.tools / build the registry — the socket-
activated wizard stays light (the transit lazy-import lesson). Saving a
toggle restarts jasper-voice, which re-filters the registry and re-writes
the catalog JSON.

Persistence: tool_state.env at mode 0644 (a list of names, not a secret).
Fail-safe: a missing/malformed file = nothing disabled (every tool ON).

URL surface (after nginx strips /tools/):
  GET  /             page render
  GET  /catalog.json read-through of /run/jasper/tools.json
  POST /toggle       body {name: str, enabled: bool} — write tool_state.env
                     + restart voice
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..log_event import log_event
from ..tool_state import DEFAULT_PATH as TOOL_STATE_FILE
from ..tool_state import read_disabled_tools, write_disabled_tools
from ._common import (
    begin_request,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_proxy_json,
)

logger = logging.getLogger(__name__)

CATALOG_FILE = "/run/jasper/tools.json"
TOOLS_PAGE_CSS_HREF = "/assets/tools/tools.css"
_TOGGLE_BODY_LIMIT = 4096


def _read_catalog(path: str) -> dict[str, Any]:
    """Read the /run catalog jasper-voice wrote. A missing / unreadable /
    malformed file resolves to an explicit `unavailable` empty catalog so
    the page can render an honest "not ready" state rather than erroring."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {"schema_version": 1, "tools": [], "unavailable": True}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("tools catalog read %s failed: %s", path, e)
        return {"schema_version": 1, "tools": [], "unavailable": True}


def _catalog_tool_names(path: str) -> set[str]:
    return {
        t.get("name")
        for t in _read_catalog(path).get("tools", [])
        if t.get("name")
    }


def _index_html(csrf_token: str = "") -> bytes:
    # The page renders client-side from /tools/catalog.json, so the body is
    # just a mount point plus the ES module entry. canonical_page emits the
    # shared app.css link, the CSRF meta tag (read by main.js for the POST),
    # and the icon sprite. The module graph is served static + revalidated
    # from /assets/tools/js/ (the `location ~ \\.js$` block in nginx).
    body = f"""
{canonical_header("Tools")}
<main class="page">
  <div class="tools-search">
    <input type="search" id="tools-search" placeholder="Search tools&hellip;"
           autocomplete="off" aria-label="Search tools">
  </div>
  <div id="tools-list" aria-busy="true">
    <div class="info-card tool-empty"><p>Loading the tool catalog&hellip;</p></div>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
<script type="module" src="/assets/tools/js/main.js"></script>
"""
    return canonical_page(
        "Tools", body, csrf_token=csrf_token, page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            # nginx strips the /tools/ prefix so we see paths like "/" and
            # "/catalog.json".
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _index_html(ctx["csrf_token"]))
                return
            if path == "/catalog.json":
                if not guard_read_request(self):
                    return
                body = json.dumps(_read_catalog(cfg["catalog_path"])).encode()
                send_proxy_json(self, body, status=200)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path != "/toggle":  # route-check BEFORE the guard (404 not 403)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            self._handle_toggle()

        def _handle_toggle(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                send_proxy_json(
                    self, b'{"error":"invalid body length"}', status=400,
                )
                return
            if length < 0 or length > _TOGGLE_BODY_LIMIT:
                send_proxy_json(
                    self, b'{"error":"invalid body length"}', status=400,
                )
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                send_proxy_json(
                    self, b'{"error":"invalid JSON body"}', status=400,
                )
                return
            name = body.get("name") if isinstance(body, dict) else None
            enabled = body.get("enabled") if isinstance(body, dict) else None
            if not isinstance(name, str) or not isinstance(enabled, bool):
                send_proxy_json(
                    self,
                    b'{"error":"name (str) and enabled (bool) required"}',
                    status=400,
                )
                return
            # Reject unknown names so a crafted POST can't poison the
            # disabled-set with garbage that would silently survive restarts.
            if name not in _catalog_tool_names(cfg["catalog_path"]):
                send_proxy_json(self, b'{"error":"unknown tool"}', status=400)
                return
            disabled = set(read_disabled_tools(cfg["state_path"]))
            if enabled:
                disabled.discard(name)
            else:
                disabled.add(name)
            try:
                write_disabled_tools(cfg["state_path"], disabled)
            except OSError as e:
                logger.exception("could not write tool_state.env")
                send_proxy_json(
                    self,
                    json.dumps({"error": f"save failed: {e}"}).encode(),
                    status=500,
                )
                return
            log_event(
                logger, "tools.toggle",
                name=name, enabled=enabled, client=self.address_string(),
            )
            # The disabled-set takes effect when jasper-voice re-filters the
            # registry on restart (which also re-writes the catalog JSON).
            restart_voice_daemon()
            send_proxy_json(
                self,
                json.dumps({"ok": True, "name": name, "enabled": enabled}).encode(),
                status=200,
            )

    return Handler


def make_server(
    target,
    *,
    catalog_path: str = CATALOG_FILE,
    state_path: str = TOOL_STATE_FILE,
) -> ThreadingHTTPServer:
    """Build the tools wizard server. `target` is a socket / (host, port)
    tuple / int port per _systemd.make_http_server's contract."""
    from . import _systemd
    return _systemd.make_http_server(
        target,
        _make_handler({"catalog_path": catalog_path, "state_path": state_path}),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-tools-web",
        description="Tool catalog wizard for the JTS speaker",
    )
    parser.add_argument(
        "--host", default=os.environ.get("JASPER_TOOLS_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_TOOLS_WEB_PORT", "8786")),
    )
    parser.add_argument(
        "--catalog",
        default=os.environ.get("JASPER_TOOLS_CATALOG_FILE", CATALOG_FILE),
    )
    parser.add_argument(
        "--state",
        default=os.environ.get("JASPER_TOOL_STATE_FILE", TOOL_STATE_FILE),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        catalog_path=args.catalog, state_path=args.state,
    )
    logger.info(
        "jasper-tools-web listening on http://%s:%d", args.host, args.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
