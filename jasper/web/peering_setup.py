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
import socket
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..peering.config import default_room as _default_room_from_hostname
from ._common import (
    PAGE_STYLE,
    delete_env_file,
    read_env_file,
    read_form,
    restart_voice_daemon,
    wrap_page,
    write_env_file,
)

logger = logging.getLogger(__name__)


PEERING_ENV_FILE = "/var/lib/jasper/peering.env"
PEER_ID_FILE = "/var/lib/jasper/peer_id"
PEERING_UDS_PATH = "/run/jasper/peering.sock"


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


# Page-specific CSS appended into the body via an inline <style>.
# Avoids monkey-patching `_common.PAGE_STYLE` (which the wake wizard
# historically did) — the shared sheet stays untouched and these
# rules just cascade on top.
_PEERS_EXTRA_CSS = """
  .peer-status {
    background: #f4f4f4; border: 1px solid #e6e6e6;
    border-radius: 8px; padding: 0.9em 1em; margin-bottom: 1.2em;
  }
  .peer-status.on { background: #f0fff4; border-color: #1db954; }
  .peer-status .pair { display: flex; gap: 0.5em; margin: 0.3em 0; }
  .peer-status .pair .label { color: #666; min-width: 6em; }
  .peer-status .pair .val { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                            color: #222; font-size: 0.95em; word-break: break-all; }
  .peer-row {
    background: #fafafa; border: 1px solid #e6e6e6;
    border-radius: 6px; padding: 0.6em 0.8em; margin-bottom: 0.4em;
    display: flex; align-items: center; gap: 0.8em;
  }
  .peer-row .name { font-weight: 600; flex: 1; }
  .peer-row .id   { color: #888; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                    font-size: 0.85em; }
  .peer-row .badge { background: #4a8; color: white; padding: 0.1em 0.5em;
                     border-radius: 4px; font-size: 0.78em; }
  .peer-empty {
    color: #666; font-style: italic;
    padding: 0.6em 0.8em; background: #fafafa;
    border: 1px dashed #ddd; border-radius: 6px;
  }
  .toggle-row {
    display: flex; align-items: center; gap: 1em;
    margin: 1.4em 0;
  }
  .toggle-row label { margin: 0; cursor: pointer; }
  .checkbox-row input { width: auto; margin-right: 0.4em; }
"""


def _render_page(*, state_path: str, status_msg: str = "") -> bytes:
    state = _load_state(state_path)
    on = _is_on(state)
    room = _room(state)
    primary = _primary(state)
    pid = _peer_id()

    status_class = "peer-status on" if on else "peer-status"
    rendered_pid = html.escape(pid)
    rendered_room = html.escape(room)

    peer_rows_html = ""
    if on:
        status = _fetch_peer_status()
        peers = (status or {}).get("peers", [])
        # Drop self.
        peers = [p for p in peers if p.get("peer_id") != pid]
        if peers:
            for p in peers:
                short_id = (p.get("peer_id") or "?")[:8]
                pname = html.escape(p.get("room", "") or "(no room)")
                paddr = html.escape(p.get("address", "") or "?")
                ptag = '<span class="badge">primary</span>' if p.get("primary") else ""
                peer_rows_html += (
                    f'<div class="peer-row">'
                    f'<span class="name">{pname}</span>'
                    f'{ptag}'
                    f'<span class="id">{short_id}… @ {paddr}</span>'
                    f'</div>'
                )
        else:
            peer_rows_html = (
                '<div class="peer-empty">'
                'No sibling peers visible yet. Enable peering on another '
                'JTS speaker on the same network for it to appear here.'
                '</div>'
            )

    discovered_section = ""
    if on:
        discovered_section = f"""
        <h2>Discovered peers</h2>
        {peer_rows_html}
        """

    on_attr = "checked" if on else ""
    primary_attr = "checked" if primary else ""

    body = f"""
    <style>{_PEERS_EXTRA_CSS}</style>
    <p class="sub">
      When multiple JTS speakers are on the same network, peering lets
      them coordinate so that only one device responds to each wake
      event. Off by default — turning it on costs nothing on a
      single-device setup, but the speakers don't even look for each
      other until you enable it.
    </p>

    <div class="{status_class}">
      <div class="pair">
        <span class="label">Status:</span>
        <span class="val">{'ON' if on else 'OFF'}</span>
      </div>
      <div class="pair">
        <span class="label">Peer ID:</span>
        <span class="val">{rendered_pid}</span>
      </div>
      <div class="pair">
        <span class="label">Room:</span>
        <span class="val">{rendered_room}</span>
      </div>
      <div class="pair">
        <span class="label">Primary:</span>
        <span class="val">{'yes (small bias in tie-breakers)' if primary else 'no'}</span>
      </div>
    </div>

    {discovered_section}

    <form method="POST" action="/save">
      <div class="toggle-row checkbox-row">
        <label><input type="checkbox" name="enabled" value="1" {on_attr}>
          Enable peering on this speaker
        </label>
      </div>

      <label for="room">Room name (shown to other speakers)</label>
      <input type="text" id="room" name="room" value="{rendered_room}"
             placeholder="kitchen, bedroom, …" maxlength="32">

      <div class="toggle-row checkbox-row">
        <label><input type="checkbox" name="primary" value="1" {primary_attr}>
          Mark this as the primary speaker
          <small style="color:#666;">
            — gives this speaker a small ranking bias in close calls.
            Useful when one Pi is in a louder/main room and you want
            it to win ties.
          </small>
        </label>
      </div>

      <p style="margin-top: 1.6em;">
        <button type="submit">Save and restart</button>
        <small style="margin-left: 0.6em; color: #666;">
          Saves to /var/lib/jasper/peering.env. Restarts jasper-voice
          and jasper-control so both daemons pick up the new config.
        </small>
      </p>
    </form>
    """
    return wrap_page("Speaker peering", body, status_msg=status_msg)


# ----------------------------------------------------------------------
# Handlers.
# ----------------------------------------------------------------------


def _save(handler: BaseHTTPRequestHandler, state_path: str) -> None:
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
        body = _render_page(
            state_path=state_path,
            status_msg=f"Save failed: {e}",
        )
        handler.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return

    # Restart both daemons so they pick up the new config. jasper-voice
    # reads JASPER_PEERING to know whether to call the peering UDS;
    # jasper-control reads it to know whether to start its peering
    # daemon thread.
    restart_voice_daemon()
    _restart_jasper_control()

    handler.send_response(HTTPStatus.SEE_OTHER)
    handler.send_header("Location", "/?saved=1")
    handler.end_headers()


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
                status_msg = ""
                if "saved=1" in self.path:
                    status_msg = "Saved. Speakers restarting; refresh in a few seconds."
                body = _render_page(
                    state_path=state_path, status_msg=status_msg,
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            if self.path == "/save":
                _save(self, state_path)
                return
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

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
