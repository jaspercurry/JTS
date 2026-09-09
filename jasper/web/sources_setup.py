# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sources on/off page at /sources/.

Playback-source toggles:

  - AirPlay and Spotify Connect use ordinary systemd lifecycle operations.
  - Bluetooth uses RF-kill, BlueZ power, and its audio/pairing services.
  - USB Audio Input preserves the ordered composite-gadget transition that
    keeps the hardware-conditional USB management network up while
    adding/removing audio.

The web process owns none of those mechanisms. It records one desired source
state and kicks ``jasper-source-intent-reconcile``; that fixed root oneshot is
the sole lifecycle coordinator for all four sources. The state response keeps
desired intent separate from the observed effective state so a failed service
start cannot silently flip the user's choice back.

AirPlay, Bluetooth, and Spotify Connect default ON. USB Audio Input
defaults OFF so it has zero resident RAM cost until explicitly enabled.
The toggle is the only knob; there's no per-source settings on this page.

State polling: clients GET /state every few seconds to reflect external
changes (operator ran `systemctl stop shairport-sync` from SSH, etc.).
When a renderer unit or its hardware is not installed, the page is still
present and explains what is missing. An unavailable source that is already
Off cannot be turned On; a stale desired-On source can always be turned Off so
the safest recovery choice never depends on the missing component.

This page renders on the canonical design system (canonical_page); its
behaviour ships as the static ES module deploy/assets/sources/js/main.js,
not inline <script>. The routes, JSON shapes, CSRF gate, and fail-soft
logging are unchanged from the legacy look. Availability/enabled derivation
and the enable-time precondition checks live in
``jasper.local_sources.status``, the single owner both this page and
jasper-control's mux-status augmenter read.

URL surface (after nginx strips /sources/):
  GET  /         page render
  GET  /state    source → {enabled, desired, effective, available, ...}
  POST /set      {source, enabled} → same shape as /state on success
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..local_sources import status as source_status
from ..log_event import log_event
from ..music_sources import MUSIC_SOURCE_SPECS
from ..source_intent import request_source_intent, source_intent_enabled
from ._common import (
    JsonBodyError,
    begin_request,
    reject_csrf,
    read_json_object,
    route_path,
    send_html_response,
    send_json_response,
    guard_read_request,
    guard_mutating_request,
)
from .chrome import canonical_banner, canonical_header, canonical_page, toggle_html

logger = logging.getLogger(__name__)


# /set carries only a source key and boolean; reject larger direct-socket
# bodies before any source lifecycle mutation.
_JSON_BODY_LIMIT = 4096
VALID_SOURCES = tuple(spec.wizard_key for spec in MUSIC_SOURCE_SPECS)
SOURCE_BY_WIZARD_KEY = {spec.wizard_key: spec.id for spec in MUSIC_SOURCE_SPECS}


def _apply(source: str, enabled: bool) -> None:
    """Record desired state and ask the one root coordinator to converge it.

    Availability/precedence checks (profile, units, hardware) live in
    ``jasper.local_sources.status.enable_blocker``; they are consulted only
    when turning a source on, so a stale desired-On source already blocked
    from turning back on stays turn-offable.
    """
    target = SOURCE_BY_WIZARD_KEY[source]
    if enabled:
        blocker = source_status.enable_blocker(target)
        if blocker:
            raise RuntimeError(blocker)
    request_source_intent(target, enabled)


# Per-page CSS layered on app.css. Just the source-row layout + notes; the
# toggle, card, header, and banner are shared primitives in app.css. Status
# colour is the one knob: the unavailable note reuses --status-warn.
_PAGE_CSS = """
.sources { display: flex; flex-direction: column; }
.source-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; padding: 0.9rem 0;
  border-bottom: 1px solid var(--border);
}
.source-row:last-child { border-bottom: none; }
.source-text { min-width: 0; }
.source-name { font-weight: 600; color: var(--text); }
.source-note { color: var(--muted); font-size: 0.9rem; margin-top: 0.2rem; }
.source-note.warn { color: var(--status-warn); }
.source-note code {
  font-size: 0.95em; padding: 1px 5px;
  border-radius: var(--radius-sm); background: var(--foreground-005);
}
"""


def _source_row(
    *, name: str, input_id: str, note_html: str = "", unavailable_html: str = "",
) -> str:
    """One source row: name + optional notes on the left, toggle on the
    right. The toggle is disabled at first paint; the ES module's /state
    poll hydrates checked/disabled within a poll cycle (mirrors the
    legacy behaviour)."""
    notes = ""
    if note_html:
        notes += note_html
    if unavailable_html:
        notes += unavailable_html
    return f"""
    <div class="source-row">
      <div class="source-text">
        <div class="source-name">{name}</div>
        {notes}
      </div>
      {toggle_html(input_id, disabled=True)}
    </div>
    """


def _index_html(csrf_token: str = "", *, status_msg: str = "") -> bytes:
    """Render the sources page. Initial toggle state is loaded from the
    server on the first /state poll (one extra round trip on page load —
    keeps the HTML static and cache-friendly)."""
    pair_note = (
        '<div class="info-card info-card--accent" id="pair-note" '
        'hidden role="note">This speaker is part of a '
        "stereo pair — music plays through the pair leader, so local "
        "sources are parked. Unpair on "
        '<a href="/sound/pair/">the Speakers page</a> to use them again.'
        "</div>"
    )
    state_error = (
        '<div class="banner banner--danger" id="sources-state-error" '
        'hidden role="alert">Source settings could not be read. '
        "Controls are paused to avoid showing a false state. Run jasper-doctor "
        "or re-run install.sh, then retry.</div>"
    )
    rows = "".join([
        _source_row(
            name="AirPlay", input_id="t-airplay",
            unavailable_html=(
                '<div class="source-note warn" id="airplay-unavailable-note" '
                'hidden>AirPlay is not installed on this speaker. '
                "Re-run install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="Bluetooth", input_id="t-bluetooth",
            note_html=(
                '<div class="source-note warn" id="bt-note" hidden>'
                "Bluetooth adapter not available on this device.</div>"
            ),
        ),
        _source_row(
            name="Spotify Connect", input_id="t-spotify_connect",
            unavailable_html=(
                '<div class="source-note warn" '
                'id="spotify_connect-unavailable-note" hidden>'
                "Spotify Connect is not installed on this speaker. Re-run "
                "install.sh to set up the local renderer stack.</div>"
            ),
        ),
        _source_row(
            name="USB Audio Input", input_id="t-usbsink",
            note_html=(
                '<div class="source-note" id="usbsink-note">'
                "Plug a computer into the Pi's USB data/OTG port through a "
                "compatible power/data splitter or hub. Your computer sees "
                "the speaker as a USB audio output device. (The USB link also "
                "provides a management-network path to this speaker's web UI "
                "when gadget hardware is available; this source toggle does "
                "not switch that management link.) While Auto is selected, "
                "new computer audio takes over like any other newly started "
                "source; pin another source to prevent automatic switching."
                "</div>"
            ),
            unavailable_html=(
                '<div class="source-note warn" id="usbsink-unavailable-note" '
                'hidden>USB gadget support is unavailable for '
                "the current hardware configuration.</div>"
            ),
        ),
    ])
    body = f"""
{canonical_header("Playback sources")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Turn each playback source on or off. Every choice
  persists across reboots, including Bluetooth. USB Audio Input is off by
  default — flip it on to use JTS as a USB audio output
  for a computer plugged into the Pi's USB data/OTG port through a
  compatible power/data splitter or hub.</p>

  <section class="info-card">
    <h2 class="section__title">Sources</h2>
    <div class="sources" id="sources">
      {state_error}{pair_note}{rows}
    </div>
  </section>
</main>
<script type="module" src="/assets/sources/js/main.js"></script>
"""
    return canonical_page(
        "Playback sources", body, csrf_token=csrf_token, page_css=_PAGE_CSS,
    )


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict[str, Any]:
        try:
            return read_json_object(self, max_bytes=_JSON_BODY_LIMIT)
        except JsonBodyError:
            return {}

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
    send_html_response(
        handler,
        _index_html(ctx["csrf_token"], status_msg=ctx["flash"]),
    )


def _get_state(handler: _Handler) -> None:
    try:
        send_json_response(handler, source_status.read_source_status())
    except Exception as e:  # noqa: BLE001
        logger.exception("/state failed")
        send_json_response(handler, {"error": str(e)}, status=502)


def _post_set(handler: _Handler) -> None:
    body = handler._read_json()
    source = str(body.get("source") or "")
    if source not in VALID_SOURCES:
        send_json_response(handler, {"error": f"unknown source {source!r}"}, status=400)
        return
    enabled_value = body.get("enabled")
    if not isinstance(enabled_value, bool):
        send_json_response(
            handler, {"error": "enabled must be true or false"}, status=400,
        )
        return
    enabled = enabled_value
    if source_status.sources_parked():
        # The pair owns its input surface while bonded. Keep a follower
        # from accumulating hidden member-local desired changes that
        # would surprise the household on unpair.
        send_json_response(
            handler,
            {"error": "sources are managed by the stereo "
                      "pair while this speaker is a "
                      "follower — unpair on /sound/pair/ to "
                      "change local sources"},
            status=409,
        )
        return
    try:
        _apply(source, enabled)
    except Exception as e:  # noqa: BLE001
        logger.exception("toggle %s -> %s failed", source, enabled)
        # The intent write happens before reconciliation. If apply
        # fails, read it back so the client keeps the user's durable
        # choice checked and shows runtime degradation instead of
        # falsely rolling intent back to the old observed state.
        try:
            state = source_status.read_source_status()
        except (OSError, RuntimeError, ValueError):
            logger.exception("failed toggle state readback")
            payload: dict[str, Any] = {"error": str(e)}
            try:
                durable_desired = source_intent_enabled(
                    SOURCE_BY_WIZARD_KEY[source],
                )
            except (OSError, RuntimeError, ValueError):
                logger.exception("failed isolated intent readback")
            else:
                payload["desired"] = durable_desired
                payload["intentRecorded"] = durable_desired is enabled
            send_json_response(handler, payload, status=502)
        else:
            send_json_response(handler, {"error": str(e), "state": state}, status=502)
        return
    log_event(
        logger,
        "sources.set",
        source=source,
        enabled=enabled,
        client=handler.address_string(),
    )
    # Read-back the state we just applied so the client UI reconciles
    # against truth (in case systemctl no-op'd or DBus rejected the
    # property write).
    try:
        state = source_status.read_source_status()
    except Exception as e:  # noqa: BLE001
        logger.exception("/set readback failed")
        send_json_response(
            handler,
            {"error": str(e), "desired": enabled, "intentRecorded": True},
            status=502,
        )
        return
    send_json_response(handler, state)


# do_GET / do_POST dispatch via the _GET_ROUTES / _POST_ROUTES tables
# (exact path -> handler callable) — module-level (not class attributes)
# since no per-server state is captured here. ORDERING IS LOAD-BEARING:
# each method looks up the route first, so an unknown path 404s before
# the read/CSRF guard runs.
_GET_ROUTES = {"/": _get_index, "/state": _get_state}
_POST_ROUTES = {"/set": _post_set}


def _make_handler() -> type[BaseHTTPRequestHandler]:
    return _Handler


def make_server(target) -> ThreadingHTTPServer:
    """Used by jasper.web.__main__ to colocate this server with the
    other settings wizards inside one process. `target` is a
    socket/tuple/int per systemd.make_http_server's contract."""
    from ..platform import systemd
    return systemd.make_http_server(target, _make_handler())
