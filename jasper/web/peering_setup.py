"""/peers/ — multi-device peering wizard.

A single toggle for the household-level question: "do these JTS
speakers know about each other?" When ON, this Pi participates in
the arbitration protocol so that simultaneous wake events on
multiple speakers resolve to exactly one responder. When OFF
(the default), this Pi is invisible to siblings.

Page layout:
  - Status card at top — current mode, this Pi's peer_id (read-only),
    this Pi's room label (editable), primary flag (small bias in
    the ranking function).
  - When peering is ON: list of currently-visible sibling peers,
    refreshed each time the page loads. Each row shows the
    sibling's room, peer_id (short form), and IP.
  - Single toggle to flip peering on/off.
  - Save button writes /var/lib/jasper/peering.env and restarts
    both jasper-voice and jasper-control (both daemons read the
    config on startup; live-toggle without restart would require
    much more coordination).

URL surface (after nginx strips the /peers/ prefix):
  GET  /         page render
  POST /save     write peering.env + restart daemons

Persistence: /var/lib/jasper/peering.env, mode 0644 (no secrets).
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..peering.config import default_room as _default_room_from_hostname
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_see_other,
    guard_mutating_request,
    write_env_file,
)

logger = logging.getLogger(__name__)


PEERING_ENV_FILE = "/var/lib/jasper/peering.env"
PEER_ID_FILE = "/var/lib/jasper/peer_id"
PEERING_UDS_PATH = "/run/jasper/peering.sock"
PEERING_PAGE_CSS_HREF = "/assets/peering/peering.css"


# ----------------------------------------------------------------------
# State helpers — pure where possible.
# ----------------------------------------------------------------------


def _load_state(path: str = PEERING_ENV_FILE) -> dict[str, str]:
    return read_env_file(path)


def _is_on(state: dict[str, str]) -> bool:
    raw = (
        state.get("JASPER_PEERING", "")
        or os.environ.get("JASPER_PEERING", "")
    ).strip().lower()
    return raw in ("on", "true", "1", "yes", "enabled")


def _room(state: dict[str, str]) -> str:
    raw = (
        state.get("JASPER_PEER_ROOM", "")
        or os.environ.get("JASPER_PEER_ROOM", "")
    ).strip()
    if raw:
        return raw
    return _default_room_from_hostname()


def _primary(state: dict[str, str]) -> bool:
    raw = (
        state.get("JASPER_PEER_PRIMARY", "")
        or os.environ.get("JASPER_PEER_PRIMARY", "")
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _peer_id(path: str = PEER_ID_FILE) -> str:
    """Read the stable peer_id installed by deploy/install.sh. If
    missing (fresh install, or wizard accessed before install.sh ran
    install_peering_template), return a placeholder so the page
    renders rather than 500ing."""
    try:
        return open(path).read().strip() or "(not yet generated)"
    except OSError:
        return "(not yet generated)"


def _fetch_peer_status(uds_path: str = PEERING_UDS_PATH, timeout: float = 1.0) -> dict | None:
    """Best-effort STATUS over the peering UDS. Returns None when the
    daemon isn't running (peering off, or jasper-control down). Page
    renders the "off" state on None."""

    async def _query() -> dict | None:
        try:
            reader, writer = await asyncio.open_unix_connection(uds_path)
        except (FileNotFoundError, OSError):
            return None
        try:
            writer.write(b"STATUS\n")
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        except (asyncio.TimeoutError, OSError):
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    try:
        return asyncio.run(_query())
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------
# HTML rendering.
# ----------------------------------------------------------------------
#
# Migrated to the canonical design system (canonical_page + canonical_header
# + canonical_banner + /assets/app.css). Page-specific visuals (the
# discovered-peer rows, the form's inline checkbox layout) live in
# deploy/assets/peering/peering.css, linked via page_css_href. The status
# summary reuses the shared .info-card / .deflist / .badge vocabulary; its
# ON/OFF tone rides the single --tone knob.
#
# This page has no inline JavaScript — the form is a plain server-rendered
# POST to /save — so there is intentionally no ES module and no <script>
# tag. The two boolean controls stay native checkboxes (name=enabled /
# name=primary) so the server-rendered POST still submits their values;
# the shared toggle_html() helper renders a bare checkbox with no
# name/value and so cannot drive a native form submission.


def _render_page(*, state_path: str, csrf_token: str, status_msg: str = "") -> bytes:
    state = _load_state(state_path)
    on = _is_on(state)
    room = _room(state)
    primary = _primary(state)
    pid = _peer_id()

    rendered_pid = html.escape(pid)
    rendered_room = html.escape(room)
    status_tone = "var(--status-ok)" if on else "var(--status-idle)"
    status_label = "On" if on else "Off"
    primary_text = (
        "Yes — small bias in tie-breakers" if primary else "No"
    )

    discovered_section = ""
    if on:
        status = _fetch_peer_status()
        peers = (status or {}).get("peers", [])
        # Drop self.
        peers = [p for p in peers if p.get("peer_id") != pid]
        if peers:
            rows = []
            for p in peers:
                short_id = html.escape((p.get("peer_id") or "?")[:8])
                pname = html.escape(p.get("room", "") or "(no room)")
                paddr = html.escape(p.get("address", "") or "?")
                ptag = (
                    '<span class="badge" style="--tone: var(--status-ok);">'
                    'primary</span>'
                    if p.get("primary") else ""
                )
                rows.append(
                    '<li class="peer-row">'
                    f'<span class="peer-row__name">{pname}</span>'
                    f'{ptag}'
                    f'<span class="peer-row__id">{short_id}… @ {paddr}</span>'
                    '</li>'
                )
            peers_html = f'<ul class="peer-list">{"".join(rows)}</ul>'
        else:
            peers_html = (
                '<p class="peer-empty">'
                'No sibling peers visible yet. Enable peering on another '
                'JTS speaker on the same network for it to appear here.'
                '</p>'
            )
        discovered_section = f"""
  <section class="section">
    <h2 class="section__title">Discovered peers</h2>
    {peers_html}
  </section>"""

    enabled_checked = " checked" if on else ""
    primary_checked = " checked" if primary else ""

    body = f"""
{canonical_header("Speaker peering")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">
    When multiple JTS speakers are on the same network, peering lets them
    coordinate so that only one device responds to each wake event. Off by
    default — turning it on costs nothing on a single-device setup, but the
    speakers don't even look for each other until you enable it.
  </p>

  <section class="info-card" style="--tone: {status_tone};">
    <dl class="deflist">
      <div><dt>Status</dt><dd>{status_label}</dd></div>
      <div><dt>Peer ID</dt><dd><code>{rendered_pid}</code></dd></div>
      <div><dt>Room</dt><dd>{rendered_room}</dd></div>
      <div><dt>Primary</dt><dd>{primary_text}</dd></div>
    </dl>
  </section>
{discovered_section}

  <form method="post" action="/save">
    {csrf_field_html(csrf_token)}

    <label class="peer-check">
      <input type="checkbox" name="enabled" value="1"{enabled_checked}>
      <span class="peer-check__text">
        <span class="peer-check__label">Enable peering on this speaker</span>
      </span>
    </label>

    <div class="field">
      <label for="room">Room name (shown to other speakers)</label>
      <input type="text" id="room" name="room" value="{rendered_room}"
             placeholder="kitchen, bedroom, …" maxlength="32"
             autocomplete="off" autocapitalize="words" spellcheck="false">
    </div>

    <label class="peer-check">
      <input type="checkbox" name="primary" value="1"{primary_checked}>
      <span class="peer-check__text">
        <span class="peer-check__label">Mark this as the primary speaker</span>
        <span class="peer-check__hint">Gives this speaker a small ranking
          bias in close calls. Useful when one Pi is in a louder / main
          room and you want it to win ties.</span>
      </span>
    </label>

    <div class="form-actions">
      <button type="submit" class="btn btn--primary">Save and restart</button>
    </div>
    <p class="form-hint">Saves to <code>/var/lib/jasper/peering.env</code>.
      Restarts jasper-voice and jasper-control so both daemons pick up the
      new config.</p>
  </form>
</main>
"""
    return canonical_page(
        "Speaker peering", body,
        csrf_token=csrf_token,
        page_css_href=PEERING_PAGE_CSS_HREF,
    )


# ----------------------------------------------------------------------
# Handlers.
# ----------------------------------------------------------------------


def _save(
    handler: BaseHTTPRequestHandler,
    state_path: str,
    *,
    form: dict[str, str] | None = None,
) -> None:
    # Form is passed in pre-read by the POST handler so it can verify the
    # CSRF token before we consume the request body. Falls back to
    # read_form for any direct caller (none today; defensive).
    if form is None:
        form = read_form(handler)
    enabled = form.get("enabled") == "1"
    room = (form.get("room") or "").strip()[:32]
    primary = form.get("primary") == "1"

    # Sanitize room to a small whitelist — anything mDNS-unfriendly
    # is rejected so the Avahi service file render doesn't fail.
    safe = []
    for c in room:
        if c.isalnum() or c in "-_":
            safe.append(c)
        elif c == " ":
            safe.append("-")
    cleaned_room = "".join(safe).strip("-")
    if enabled and not cleaned_room:
        # If they're turning peering on but didn't give a room name,
        # fall back to the auto-derived default.
        cleaned_room = _room({})

    # Preserve operator-set tuning knobs that the wizard doesn't
    # surface (JASPER_PEER_ARB_WINDOW_MS, JASPER_PEER_BREAK_THRESHOLD).
    # write_env_file does a full-file replacement, so without this read-
    # then-merge step a save would wipe out hand-tuned values.
    values: dict[str, str] = dict(_load_state(state_path))
    values["JASPER_PEERING"] = "on" if enabled else "off"
    if cleaned_room:
        values["JASPER_PEER_ROOM"] = cleaned_room
    elif "JASPER_PEER_ROOM" in values:
        # Room cleared in form → drop from file so the default-room
        # derivation kicks in next load.
        del values["JASPER_PEER_ROOM"]
    if primary:
        values["JASPER_PEER_PRIMARY"] = "1"
    elif "JASPER_PEER_PRIMARY" in values:
        del values["JASPER_PEER_PRIMARY"]

    try:
        # mode=0o644 — no secrets here, just config. Matches the
        # wake_model.env / sources_setup pattern.
        write_env_file(state_path, values, mode=0o644)
        logger.info(
            "event=peering.wizard.save mode=%s room=%s primary=%d",
            values["JASPER_PEERING"], cleaned_room, int(primary),
        )
    except OSError as e:
        logger.exception("peering save failed")
        # The 500-with-body path reuses begin_request's CSRF token (set
        # on the inbound POST cookie) so the rendered form keeps working.
        ctx = begin_request(handler)
        body = _render_page(
            state_path=state_path,
            csrf_token=ctx["csrf_token"],
            status_msg=f"Save failed: {e}",
        )
        send_html_response(
            handler, body, status=HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return

    # Restart both daemons so they pick up the new config. jasper-voice
    # reads JASPER_PEERING to know whether to call the peering UDS;
    # jasper-control reads it to know whether to start its peering
    # daemon thread.
    restart_voice_daemon()
    _restart_jasper_control()

    send_see_other(
        handler, "/",
        flash="Saved. Speakers restarting; refresh in a few seconds.",
    )


def _restart_jasper_control() -> None:
    """Best-effort restart of jasper-control so the peering daemon
    picks up the new mode. --no-block matches the rationale at
    _common.restart_voice_daemon — don't make the wizard's save
    handler block waiting for the daemon to come back up."""
    try:
        subprocess.run(
            ["systemctl", "restart", "--no-block", "jasper-control"],
            check=False, timeout=5,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("jasper-control restart failed: %s", e)


# ----------------------------------------------------------------------
# Server setup.
# ----------------------------------------------------------------------


def _make_handler(state_path: str):
    """Build a request handler class bound to `state_path`."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: ANN001, A003
            logger.info("peers-wizard: " + fmt, *args)

        def do_GET(self):  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                ctx = begin_request(self)
                body = _render_page(
                    state_path=state_path,
                    csrf_token=ctx["csrf_token"],
                    status_msg=ctx["flash"],
                )
                send_html_response(self, body)
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path != "/save":
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return
            _save(self, state_path, form=form)

    return _Handler


def make_server(target, *, state_path: str = PEERING_ENV_FILE) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer. `target` is either an (host, port)
    tuple (direct bind) or an already-bound socket (from systemd
    socket activation — see jasper/web/__main__.py)."""
    from ._systemd import make_http_server
    handler_cls = _make_handler(state_path)
    return make_http_server(target, handler_cls)


def main(argv: list[str] | None = None) -> int:
    """Direct CLI entrypoint — used for dev/testing outside systemd."""
    p = argparse.ArgumentParser(description="JTS peering wizard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_PEERS_WEB_PORT", "8776")),
    )
    p.add_argument(
        "--state-path",
        default=os.environ.get("JASPER_PEERING_FILE", PEERING_ENV_FILE),
    )
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server((args.host, args.port), state_path=args.state_path)
    logger.info("peering wizard listening on http://%s:%d/", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
