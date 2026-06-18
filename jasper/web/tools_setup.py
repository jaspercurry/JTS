"""Tool catalog wizard at /tools/.

Browse + search the first-party voice tool packs and turn packs/tools on/off.
"Add" = enable, "remove" = disable. No install-from-store (that is the
later marketplace). A pack whose backend is not configured starts off; when
the user turns it on, the detail page surfaces the setup wizard.

This page READS the catalog jasper-voice wrote at /run/jasper/tools.json
and writes tool UI state to /var/lib/jasper/tool_state.env plus prompt
overrides to /var/lib/jasper/tool_prompt_overrides.json. It does NOT
import jasper.tools / build the registry — the socket-activated wizard
stays light (the transit lazy-import lesson); it uses jasper.tool_catalog_view
(json + tool_state only) to read + overlay.

Toggle stages, Apply commits — two-step on purpose:
  * POST /toggle just writes staged tool UI state. It does NOT restart voice:
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
  GET  /pack/<id>    pack detail page render
  POST /toggle-pack  body {id: str, enabled: bool} — write pack state
  POST /toggle       body {name: str, enabled: bool} — write tool_state.env
                     (stage only, no restart)
  POST /prompt       body {name: str, prompt: str} — write prompt override
  POST /prompt-reset body {name: str} — delete prompt override
  POST /apply        restart jasper-voice once to apply staged changes
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..log_event import log_event
from ..tool_prompt_overrides import DEFAULT_PATH as PROMPT_OVERRIDES_FILE
from ..tool_prompt_overrides import read_prompt_overrides, write_prompt_overrides
from ..tool_catalog_view import catalog_view
from ..tool_state import DEFAULT_PATH as TOOL_STATE_FILE
from ..tool_state import ToolState, read_tool_state, write_tool_state
from ._common import (
    begin_request,
    bonded_follower_active,
    canonical_header,
    canonical_page,
    guard_mutating_request,
    guard_read_request,
    json_island,
    read_active_provider,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_proxy_json,
)

logger = logging.getLogger(__name__)

CATALOG_FILE = "/run/jasper/tools.json"
TOOLS_PAGE_CSS_HREF = "/assets/tools/tools.css"
_JSON_BODY_LIMIT = 65536

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

# In-memory fail-CLOSED floor for the Apply rate-limit. The persisted
# timestamp is the cross-restart source of truth, but if it can't be written
# (read-only rootfs after an unclean shutdown, disk full, bad perms) the file
# read would return 0.0 and the throttle would collapse — letting an Apply
# retry loop feed jasper-voice's StartLimitAction=reboot ladder. This module
# global holds the last apply time within the (long-lived, socket-activated)
# wizard process, so the throttle still bounds the rate across a burst even
# when the file write fails. A list so the nested handler can mutate it.
_LAST_APPLY = [0.0]


def _toggle_index(catalog_path: str, state_path: str) -> dict[str, dict[str, Any]]:
    """name -> overlaid catalog entry, for toggle validation. Uses the same
    overlay the page sees so 'is this toggleable?' matches the rendered UI."""
    view = catalog_view(catalog_path, state_path)
    return {
        t["name"]: t
        for t in view.get("tools", [])
        if isinstance(t.get("name"), str)
    }


def _pack_index(catalog_path: str, state_path: str) -> dict[str, dict[str, Any]]:
    """pack id -> overlaid catalog pack, for pack-toggle validation."""
    view = catalog_view(catalog_path, state_path)
    return {
        p["id"]: p
        for p in view.get("packs", [])
        if isinstance(p, dict) and isinstance(p.get("id"), str)
    }


def _read_apply_ts(path: str) -> float:
    """Last /apply restart time (epoch seconds). Missing/unreadable/non-finite
    -> 0.0 (a missing file means 'no recent restart'; the in-memory floor in
    _handle_apply guards the fail-open direction). Reject nan/inf explicitly:
    float('nan') parses fine but breaks the `remaining > 0` comparison (nan
    fails open, inf never fires)."""
    try:
        with open(path, encoding="utf-8") as fh:
            v = float(fh.read().strip() or "0")
    except (OSError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


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


def _tool_name_from_path(path: str) -> str | None:
    """Return the URL-decoded tool slug from /tool/<name>, if valid."""
    prefix = "/tool/"
    if not path.startswith(prefix):
        return None
    raw = path[len(prefix):].strip("/")
    if not raw or "/" in raw:
        return None
    return urllib.parse.unquote(raw)


def _pack_id_from_path(path: str) -> str | None:
    """Return the URL-decoded pack id from /pack/<id>, if valid."""
    prefix = "/pack/"
    if not path.startswith(prefix):
        return None
    raw = path[len(prefix):].strip("/")
    if not raw or "/" in raw:
        return None
    return urllib.parse.unquote(raw)


def _detail_html(pack_id: str, csrf_token: str = "") -> bytes:
    body = f"""
{canonical_header("Tool pack", back_href="/tools/", back_label="Tools")}
<main class="page">
  <div id="tool-detail" aria-busy="true">
    <div class="info-card tool-empty"><p>Loading the tool pack&hellip;</p></div>
  </div>
  <div class="tools-apply" id="tools-apply" hidden>
    <span class="tools-apply__note">Changes are staged. Applying restarts the
      voice assistant briefly to pick them up.</span>
    <button type="button" class="btn" id="tools-apply-btn">Apply changes</button>
  </div>
  <div class="status-line" id="status" role="status" aria-live="polite"></div>
  {json_island("tool-detail-data", {"pack_id": pack_id})}
</main>
<script type="module" src="/assets/tools/js/detail.js"></script>
"""
    return canonical_page(
        "Tool", body, csrf_token=csrf_token, page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _guide_html(csrf_token: str = "") -> bytes:
    body = f"""
{canonical_header("Tool authoring guide", back_href="/tools/", back_label="Tools")}
<main class="page">
  <article class="info-card tool-guide">
    <h2>Good tools are small, explicit, and tested</h2>
    <p>Use packs for user-facing capabilities and tools for the individual
      callable leaves the model can invoke. Keep model-facing prompts short
      enough to scan, but specific about when to call, when not to call,
      response style, and failure behavior.</p>
    <h3>Prompt copy</h3>
    <ul>
      <li>Start with the tool's purpose in one sentence.</li>
      <li>Name concrete call boundaries and common false positives.</li>
      <li>Describe the response shape the model should speak from.</li>
      <li>Keep implementation notes in code comments, not tool prompts.</li>
    </ul>
    <h3>Safety</h3>
    <ul>
      <li>Mark third-party text tools as untrusted output.</li>
      <li>Mark real-world action tools as consequential.</li>
      <li>Use a CapabilityPack as the copyable unit: metadata, setup,
        runtime dependencies, tools, and tests belong together.</li>
      <li>Ship a regression scenario under tests/voice_eval/regression/.</li>
    </ul>
  </article>
</main>
"""
    return canonical_page(
        "Tool authoring guide",
        body,
        csrf_token=csrf_token,
        page_css_href=TOOLS_PAGE_CSS_HREF,
    )


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    cfg.setdefault(
        "apply_ts_path",
        os.path.join(os.path.dirname(cfg["state_path"]), "tools_apply.ts"),
    )
    cfg.setdefault("prompt_overrides_path", PROMPT_OVERRIDES_FILE)

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
            pack_id = _pack_id_from_path(path)
            detail_name = _tool_name_from_path(path)
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _index_html(ctx["csrf_token"]))
                return
            if path == "/catalog.json":
                if not guard_read_request(self):
                    return
                view = catalog_view(
                    cfg["catalog_path"],
                    cfg["state_path"],
                    cfg["prompt_overrides_path"],
                )
                send_proxy_json(self, json.dumps(view).encode(), status=200)
                return
            if path == "/guide":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _guide_html(ctx["csrf_token"]))
                return
            if pack_id is not None:
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                send_html_response(self, _detail_html(pack_id, ctx["csrf_token"]))
                return
            if detail_name is not None:
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                # Backward-compatible fallback for old /tool/<name> links.
                send_html_response(self, _detail_html("tool:" + detail_name, ctx["csrf_token"]))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path not in (
                "/toggle", "/toggle-pack", "/prompt", "/prompt-reset", "/apply",
            ):  # route BEFORE guard (404)
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if not guard_mutating_request(self):
                reject_csrf(self)
                return
            if path == "/toggle":
                self._handle_toggle()
            elif path == "/toggle-pack":
                self._handle_toggle_pack()
            elif path == "/prompt":
                self._handle_prompt()
            elif path == "/prompt-reset":
                self._handle_prompt_reset()
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
            if length < 0 or length > _JSON_BODY_LIMIT:
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
            if entry.get("disabled_by_pack") is True:
                send_proxy_json(
                    self, b'{"error":"pack disabled"}', status=400,
                )
                return
            if entry.get("status") not in ("active", "off"):
                send_proxy_json(
                    self, b'{"error":"tool not configured"}', status=400,
                )
                return
            with _STATE_LOCK:
                state = read_tool_state(cfg["state_path"])
                disabled = set(state.disabled_tools)
                updated = set(disabled)
                if enabled:
                    updated.discard(name)
                else:
                    updated.add(name)
                if updated != disabled:
                    try:
                        write_tool_state(
                            cfg["state_path"],
                            ToolState(
                                disabled_tools=frozenset(updated),
                                disabled_packs=state.disabled_packs,
                                setup_enabled_packs=state.setup_enabled_packs,
                            ),
                        )
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
                catalog_view(
                    cfg["catalog_path"],
                    cfg["state_path"],
                    cfg["prompt_overrides_path"],
                ).get("pending")
            )
            send_proxy_json(
                self,
                json.dumps(
                    {"ok": True, "name": name, "enabled": enabled,
                     "pending": pending},
                ).encode(),
                status=200,
            )

        def _handle_toggle_pack(self) -> None:
            body, ok = self._read_json_body()
            if not ok:
                return
            pack_id = body.get("id")
            enabled = body.get("enabled")
            if not isinstance(pack_id, str) or not isinstance(enabled, bool):
                send_proxy_json(
                    self,
                    b'{"error":"id (str) and enabled (bool) required"}',
                    status=400,
                )
                return
            index = _pack_index(cfg["catalog_path"], cfg["state_path"])
            entry = index.get(pack_id)
            if entry is None:
                send_proxy_json(self, b'{"error":"unknown pack"}', status=400)
                return
            singleton_tool = entry.get("singleton_tool_name")
            setup_only = (
                int(entry.get("setup_required_count") or 0)
                >= int(entry.get("tool_count") or 0)
                and int(entry.get("tool_count") or 0) > 0
            )
            with _STATE_LOCK:
                state = read_tool_state(cfg["state_path"])
                if setup_only:
                    enabled_setup = set(state.setup_enabled_packs)
                    updated_setup = set(enabled_setup)
                    if enabled:
                        updated_setup.add(pack_id)
                    else:
                        updated_setup.discard(pack_id)
                    if updated_setup != enabled_setup:
                        try:
                            write_tool_state(
                                cfg["state_path"],
                                ToolState(
                                    disabled_tools=state.disabled_tools,
                                    disabled_packs=state.disabled_packs,
                                    setup_enabled_packs=frozenset(updated_setup),
                                ),
                            )
                        except OSError as e:
                            logger.exception("could not write tool_state.env")
                            send_proxy_json(
                                self,
                                json.dumps({"error": f"save failed: {e}"}).encode(),
                                status=500,
                            )
                            return
                elif isinstance(singleton_tool, str):
                    disabled_tools = set(state.disabled_tools)
                    updated_tools = set(disabled_tools)
                    if enabled:
                        updated_tools.discard(singleton_tool)
                    else:
                        updated_tools.add(singleton_tool)
                    if updated_tools != disabled_tools:
                        try:
                            write_tool_state(
                                cfg["state_path"],
                                ToolState(
                                    disabled_tools=frozenset(updated_tools),
                                    disabled_packs=state.disabled_packs,
                                    setup_enabled_packs=state.setup_enabled_packs,
                                ),
                            )
                        except OSError as e:
                            logger.exception("could not write tool_state.env")
                            send_proxy_json(
                                self,
                                json.dumps({"error": f"save failed: {e}"}).encode(),
                                status=500,
                            )
                            return
                else:
                    disabled_packs = set(state.disabled_packs)
                    updated_packs = set(disabled_packs)
                    if enabled:
                        updated_packs.discard(pack_id)
                    else:
                        updated_packs.add(pack_id)
                    if updated_packs != disabled_packs:
                        try:
                            write_tool_state(
                                cfg["state_path"],
                                ToolState(
                                    disabled_tools=state.disabled_tools,
                                    disabled_packs=frozenset(updated_packs),
                                    setup_enabled_packs=state.setup_enabled_packs,
                                ),
                            )
                        except OSError as e:
                            logger.exception("could not write tool_state.env")
                            send_proxy_json(
                                self,
                                json.dumps({"error": f"save failed: {e}"}).encode(),
                                status=500,
                            )
                            return
                log_event(
                    logger, "tools.toggle_pack",
                    pack=pack_id, enabled=enabled,
                    singleton_tool=singleton_tool if isinstance(singleton_tool, str) else None,
                    client=self.address_string(),
                )
            pending = bool(
                catalog_view(
                    cfg["catalog_path"],
                    cfg["state_path"],
                    cfg["prompt_overrides_path"],
                ).get("pending")
            )
            send_proxy_json(
                self,
                json.dumps(
                    {"ok": True, "id": pack_id, "enabled": enabled,
                     "pending": pending, "setup_required": setup_only},
                ).encode(),
                status=200,
            )

        def _handle_prompt(self) -> None:
            body, ok = self._read_json_body()
            if not ok:
                return
            name = body.get("name")
            prompt = body.get("prompt")
            if not isinstance(name, str) or not isinstance(prompt, str):
                send_proxy_json(
                    self,
                    b'{"error":"name (str) and prompt (str) required"}',
                    status=400,
                )
                return
            if not prompt.strip():
                send_proxy_json(self, b'{"error":"prompt cannot be blank"}', status=400)
                return
            index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
            if name not in index:
                send_proxy_json(self, b'{"error":"unknown tool"}', status=400)
                return
            with _STATE_LOCK:
                overrides = read_prompt_overrides(cfg["prompt_overrides_path"])
                if overrides.get(name) != prompt:
                    overrides[name] = prompt
                    try:
                        write_prompt_overrides(cfg["prompt_overrides_path"], overrides)
                    except OSError as e:
                        logger.exception("could not write tool prompt overrides")
                        send_proxy_json(
                            self,
                            json.dumps({"error": f"save failed: {e}"}).encode(),
                            status=500,
                        )
                        return
                    log_event(
                        logger, "tools.prompt_override_saved",
                        name=name, client=self.address_string(),
                    )
            pending = bool(catalog_view(
                cfg["catalog_path"], cfg["state_path"], cfg["prompt_overrides_path"],
            ).get("pending"))
            send_proxy_json(
                self,
                json.dumps({"ok": True, "name": name, "pending": pending}).encode(),
                status=200,
            )

        def _handle_prompt_reset(self) -> None:
            body, ok = self._read_json_body()
            if not ok:
                return
            name = body.get("name")
            if not isinstance(name, str):
                send_proxy_json(self, b'{"error":"name (str) required"}', status=400)
                return
            index = _toggle_index(cfg["catalog_path"], cfg["state_path"])
            if name not in index:
                send_proxy_json(self, b'{"error":"unknown tool"}', status=400)
                return
            with _STATE_LOCK:
                overrides = read_prompt_overrides(cfg["prompt_overrides_path"])
                if name in overrides:
                    del overrides[name]
                    try:
                        write_prompt_overrides(cfg["prompt_overrides_path"], overrides)
                    except OSError as e:
                        logger.exception("could not write tool prompt overrides")
                        send_proxy_json(
                            self,
                            json.dumps({"error": f"save failed: {e}"}).encode(),
                            status=500,
                        )
                        return
                    log_event(
                        logger, "tools.prompt_override_reset",
                        name=name, client=self.address_string(),
                    )
            pending = bool(catalog_view(
                cfg["catalog_path"], cfg["state_path"], cfg["prompt_overrides_path"],
            ).get("pending"))
            send_proxy_json(
                self,
                json.dumps({"ok": True, "name": name, "pending": pending}).encode(),
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
                # max(persisted, in-memory) so a failed ts write can't open
                # the throttle within this process (fail-closed floor).
                last = max(_read_apply_ts(cfg["apply_ts_path"]), _LAST_APPLY[0])
                remaining = _APPLY_MIN_INTERVAL_SEC - (now - last)
                if remaining > 0:
                    send_proxy_json(self, json.dumps({
                        "restarted": False, "reason": "throttled",
                        "retry_after": int(remaining) + 1,
                        "message": "The assistant is already restarting — "
                                   "your changes are saved and will apply "
                                   "shortly.",
                    }).encode(), status=200)
                    return
                _LAST_APPLY[0] = now
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
    prompt_overrides_path: str = PROMPT_OVERRIDES_FILE,
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
            "prompt_overrides_path": prompt_overrides_path,
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
