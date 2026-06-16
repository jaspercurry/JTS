"""Tool catalog wizard at /tools/.

Browse + search the first-party voice tools and turn each on/off.
"Add" = enable, "remove" = disable. No install-from-store (that is the
later marketplace). A tool whose backend isn't configured shows a
"needs setup" state linking to its setup wizard.

This page READS the catalog jasper-voice wrote at /run/jasper/tools.json
and writes the disabled-set to /var/lib/jasper/tool_state.env. It does NOT
import jasper.tools / build the registry — the socket-activated wizard
stays light (the transit lazy-import lesson); it uses jasper.tool_catalog_view
(json + tool_state only) to read + overlay.

Toggle stages, Apply commits — two-step on purpose:
  * POST /toggle just writes the disabled-set. It does NOT restart voice:
    restarting the assistant drops any in-progress conversation and makes
    the speaker briefly deaf, so doing it per-toggle (silently, N times as
    you tick boxes) is user-hostile — and an unthrottled per-toggle restart
    can feed jasper-voice's StartLimitAction=reboot ladder. The page reads
    each tool's on/off back through the overlay (catalog_view) so the UI
    converges instantly without waiting on — or being raced by — a restart.
  * POST /apply restarts jasper-voice ONCE so the staged changes go live. It
    is rate-limited (and reports honestly when a restart won't happen — no
    provider / bonded follower) so a burst of Apply calls can't trip reboot.

Persistence: tool_state.env at mode 0644 (a list of names, not a secret).
Fail-safe: a missing/malformed file = nothing disabled (every tool ON).

URL surface (after nginx strips /tools/):
  GET  /             page render
  GET  /catalog.json catalog metadata + the fresh disabled-set overlaid
                     ({..., tools:[...], pending: bool})
  POST /toggle       body {name: str, enabled: bool} — write tool_state.env
                     (stage only, no restart)
  POST /apply        restart jasper-voice once to apply staged changes
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..log_event import log_event
from ..tool_catalog_view import catalog_view
from ..tool_state import DEFAULT_PATH as TOOL_STATE_FILE
from ..tool_state import read_disabled_tools, write_disabled_tools
from ._common import (
    begin_request,
    bonded_follower_active,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    read_active_provider,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_proxy_json,
)

logger = logging.getLogger(__name__)

CATALOG_FILE = "/run/jasper/tools.json"
TOOLS_PAGE_CSS_HREF = "/assets/tools/tools.css"
_TOGGLE_BODY_LIMIT = 4096

# Minimum seconds between /tools/-driven voice restarts. jasper-voice has
# StartLimitBurst=20 / StartLimitIntervalSec=300 / StartLimitAction=reboot
# (crash-loop guard); 20s caps Apply-driven restarts at ~15 per 300s, safely
# under that ladder, so neither a key-mashing household member nor a scripted
# LAN client can reboot the speaker by spamming Apply. The staged change is
# already persisted, so a throttled Apply loses nothing — the in-flight (or a
# later) restart picks it up.
_APPLY_MIN_INTERVAL_SEC = 20.0

# Serializes the read-modify-write of tool_state.env and the apply timestamp
# across the ThreadingHTTPServer's request threads, so two concurrent toggles
# can't lose an update (last-writer-wins on the unserialized RMW).
_STATE_LOCK = threading.Lock()


def _toggle_index(catalog_path: str, state_path: str) -> dict[str, dict[str, Any]]:
    """name -> overlaid catalog entry, for toggle validation. Uses the same
    overlay the page sees so 'is this toggleable?' matches the rendered UI."""
    view = catalog_view(catalog_path, state_path)
    return {
        t["name"]: t
        for t in view.get("tools", [])
        if isinstance(t.get("name"), str)
    }


def _read_apply_ts(path: str) -> float:
    """Last /apply restart time (epoch seconds). Missing/unreadable -> 0.0
    (never throttle on a bad read — the throttle is a safety cap, and a
    missing file means 'no recent restart')."""
    try:
        with open(path, encoding="utf-8") as fh:
            return float(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0.0


def _write_apply_ts(path: str, ts: float) -> None:
    from ..atomic_io import atomic_write_text
    try:
        atomic_write_text(path, f"{ts:.3f}\n", mode=0o644)
    except OSError as e:  # best-effort — the cap degrades open, never blocks
        logger.warning("could not write apply timestamp %s: %s", path, e)


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
  <div class="tools-apply" id="tools-apply" hidden>
    <span class="tools-apply__note">Changes are staged. Applying restarts the
      voice assistant briefly to pick them up.</span>
    <button type="button" class="btn" id="tools-apply-btn">Apply changes</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
</main>
<script type="module" src="/assets/tools/js/main.js"></script>
"""
    return canonical_page(
        "Tools", body, csrf_token=csrf_token, page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    cfg.setdefault(
        "apply_ts_path",
        os.path.join(os.path.dirname(cfg["state_path"]), "tools_apply.ts"),
    )

    class Handler(BaseHTTPRequestHandler):
        # Abort a connection whose client stalls mid-request (e.g. a lying
        # Content-Length that never sends the body) instead of pinning a
        # request thread indefinitely. Defense in depth — the wizard's idle
        # watchdog already reaps stuck threads, but this fails the read fast.
        timeout = 30

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
                view = catalog_view(cfg["catalog_path"], cfg["state_path"])
                send_proxy_json(self, json.dumps(view).encode(), status=200)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in ("/toggle", "/apply"):  # route BEFORE guard (404)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if path == "/toggle":
                self._handle_toggle()
            else:
                self._handle_apply()

        def _read_json_body(self) -> tuple[dict[str, Any] | None, bool]:
            """Parse the request body as a JSON object. Returns (obj, ok);
            on any framing/JSON error it has already sent the 400 and
            returns (None, False)."""
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                send_proxy_json(
                    self, b'{"error":"invalid body length"}', status=400,
                )
                return None, False
            if length < 0 or length > _TOGGLE_BODY_LIMIT:
                send_proxy_json(
                    self, b'{"error":"invalid body length"}', status=400,
                )
                return None, False
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                send_proxy_json(
                    self, b'{"error":"invalid JSON body"}', status=400,
                )
                return None, False
            return (body if isinstance(body, dict) else {}), True

        def _handle_toggle(self) -> None:
            body, ok = self._read_json_body()
            if not ok:
                return
            name = body.get("name")
            enabled = body.get("enabled")
            if not isinstance(name, str) or not isinstance(enabled, bool):
                send_proxy_json(
                    self,
                    b'{"error":"name (str) and enabled (bool) required"}',
                    status=400,
                )
                return
            # Only configured tools (active/off in the overlaid catalog) are
            # toggleable: reject unknown names (a crafted POST can't poison
            # the disabled-set with garbage) AND needs_setup tools (no UI
            # control — toggling one just writes a meaningless entry).
            index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
            entry = index.get(name)
            if entry is None:
                send_proxy_json(self, b'{"error":"unknown tool"}', status=400)
                return
            if entry.get("status") not in ("active", "off"):
                send_proxy_json(
                    self, b'{"error":"tool not configured"}', status=400,
                )
                return
            with _STATE_LOCK:
                disabled = set(read_disabled_tools(cfg["state_path"]))
                updated = set(disabled)
                if enabled:
                    updated.discard(name)
                else:
                    updated.add(name)
                if updated != disabled:
                    try:
                        write_disabled_tools(cfg["state_path"], updated)
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
                        name=name, enabled=enabled,
                        client=self.address_string(),
                    )
            # Staged only — no restart. The page re-reads the overlay so the
            # UI converges immediately; `pending` tells it to offer Apply.
            pending = bool(
                catalog_view(cfg["catalog_path"], cfg["state_path"]).get("pending")
            )
            send_proxy_json(
                self,
                json.dumps(
                    {"ok": True, "name": name, "enabled": enabled,
                     "pending": pending},
                ).encode(),
                status=200,
            )

        def _handle_apply(self) -> None:
            # Mirror restart_voice_daemon's skip conditions so the response is
            # HONEST about whether a restart will actually happen — never an
            # ok-banner promising an effect the server knowingly won't deliver.
            if not read_active_provider():
                send_proxy_json(self, json.dumps({
                    "restarted": False, "reason": "no_provider",
                    "message": "Saved. Choose a voice provider at "
                               "/voice/ to start the assistant.",
                }).encode(), status=200)
                return
            if bonded_follower_active():
                send_proxy_json(self, json.dumps({
                    "restarted": False, "reason": "bonded",
                    "message": "Saved. Changes apply when this speaker "
                               "leaves the stereo pair.",
                }).encode(), status=200)
                return
            now = time.time()
            with _STATE_LOCK:
                remaining = _APPLY_MIN_INTERVAL_SEC - (now - _read_apply_ts(
                    cfg["apply_ts_path"]))
                if remaining > 0:
                    send_proxy_json(self, json.dumps({
                        "restarted": False, "reason": "throttled",
                        "retry_after": int(remaining) + 1,
                        "message": "The assistant is already restarting — "
                                   "your changes are saved and will apply "
                                   "shortly.",
                    }).encode(), status=200)
                    return
                _write_apply_ts(cfg["apply_ts_path"], now)
            log_event(logger, "tools.apply", client=self.address_string())
            # jasper-voice re-filters the registry against tool_state.env on
            # restart (and re-writes the catalog JSON).
            restart_voice_daemon()
            send_proxy_json(self, json.dumps({
                "restarted": True,
                "message": "Restarting the assistant to apply your changes…",
            }).encode(), status=200)

    return Handler


def make_server(
    target,
    *,
    catalog_path: str = CATALOG_FILE,
    state_path: str = TOOL_STATE_FILE,
    apply_ts_path: str | None = None,
) -> ThreadingHTTPServer:
    """Build the tools wizard server. `target` is a socket / (host, port)
    tuple / int port per _systemd.make_http_server's contract."""
    from . import _systemd
    if apply_ts_path is None:
        apply_ts_path = os.path.join(os.path.dirname(state_path), "tools_apply.ts")
    return _systemd.make_http_server(
        target,
        _make_handler({
            "catalog_path": catalog_path,
            "state_path": state_path,
            "apply_ts_path": apply_ts_path,
        }),
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
