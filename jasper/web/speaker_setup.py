"""/speaker/ — user-facing renderer display name.

Single setting: the name shown in AirPlay, Spotify Connect, Bluetooth,
and USB Audio pickers. This is deliberately separate from
``JASPER_HOSTNAME``; the URL remains ``jts.local``.

URL surface (after nginx strips /speaker/):
  GET  /         page render
  POST /save     validate, duplicate-check, write state, restart services
"""
from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..speaker_name import (
    DEFAULT_SPEAKER_NAME,
    MAX_SPEAKER_NAME_CHARS,
    SpeakerNameError,
    read_state,
    validate_name,
    validate_room,
    write_state,
)
from ..speaker_name_discovery import NameConflict, find_name_conflicts
from ..log_event import log_event
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    read_form,
    reject_csrf,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
)

logger = logging.getLogger(__name__)

SPEAKER_NAME_FILE = "/var/lib/jasper/speaker_name.env"
BLUEZ_MAIN_CONF = "/etc/bluetooth/main.conf"

RESTART_UNITS = [
    "librespot.service",
    "shairport-sync.service",
    "jasper-voice.service",
    "jasper-control.service",
    "jasper-mux.service",
    "bluetooth.service",
    "bluealsa.service",
    "bluealsa-aplay.service",
    "bt-agent.service",
]


def _systemctl(*args: str, timeout: int = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["systemctl", *args],
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc.returncode, (proc.stdout or proc.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("systemctl %s failed: %s", " ".join(args), e)
        return 1, str(e)


def _unit_active(unit: str) -> bool:
    rc, _ = _systemctl("is-active", "--quiet", unit, timeout=3)
    return rc == 0


def _restart_units(units: list[str]) -> None:
    rc, out = _systemctl("restart", "--no-block", *units)
    if rc != 0:
        log_event(
            logger,
            "speaker_name.restart_failed",
            units=",".join(units),
            detail=out,
            level=logging.WARNING,
        )


def _write_bluez_main_conf_name(name: str, path: str = BLUEZ_MAIN_CONF) -> None:
    conf = Path(path)
    try:
        original = conf.read_text(encoding="utf-8")
    except FileNotFoundError:
        log_event(logger, "speaker_name.bluez_conf_missing", path=path, level=logging.WARNING)
        return

    replacement = f"Name = {name}"
    updated, count = re.subn(
        r"(?m)^#?\s*Name\s*=.*$",
        replacement,
        original,
        count=1,
    )
    if count == 0:
        updated = original.rstrip() + "\n" + replacement + "\n"
    if updated == original:
        return

    tmp = conf.with_name(conf.name + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, conf)
    log_event(logger, "speaker_name.bluez_conf", path=path, result="ok")


def _format_conflicts(conflicts: list[NameConflict]) -> str:
    if not conflicts:
        return ""
    first = conflicts[0]
    if len(conflicts) == 1:
        return (
            f'"{first.name}" is already in use on {first.protocol}. '
            "Choose a different speaker name."
        )
    protocols = ", ".join(sorted({c.protocol for c in conflicts}))
    return (
        f'That name is already in use ({protocols}). '
        "Choose a different speaker name."
    )


def _find_conflicts(name: str) -> list[NameConflict]:
    try:
        return asyncio.run(find_name_conflicts(name))
    except Exception as e:  # noqa: BLE001
        log_event(logger, "speaker_name.duplicate_check_failed", error=e, level=logging.WARNING)
        return []


def _apply_name(name: str) -> None:
    units = list(RESTART_UNITS)
    if _unit_active("jasper-usbsink.service") or _unit_active("jasper-usbsink-init.service"):
        units.append("jasper-usbsink-init.service")

    _write_bluez_main_conf_name(name)
    try:
        from ..bluetooth.adapter import set_alias as set_bluetooth_alias
        asyncio.run(set_bluetooth_alias(name))
        log_event(logger, "speaker_name.bluetooth_alias", name=repr(name), result="ok")
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "speaker_name.bluetooth_alias",
            name=repr(name),
            result="failed",
            error=e,
            level=logging.WARNING,
        )

    try:
        from ..control_advert import render_control_advert
        ok = render_control_advert(name)
        log_event(logger, "speaker_name.avahi", name=repr(name), result="ok" if ok else "soft_fail")
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "speaker_name.avahi",
            name=repr(name),
            result="failed",
            error=e,
            level=logging.WARNING,
        )

    log_event(logger, "speaker_name.restart", units=",".join(units))
    _restart_units(units)


def _index_html(
    *,
    current_name: str,
    current_room: str,
    csrf_token: str,
    status_msg: str = "",
) -> bytes:
    value = html.escape(current_name, quote=True)
    room_value = html.escape(current_room, quote=True)
    default_attr = html.escape(DEFAULT_SPEAKER_NAME, quote=True)
    default_text = html.escape(DEFAULT_SPEAKER_NAME)
    body = f"""
{canonical_header("Speaker name")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Change the name shown in AirPlay, Spotify Connect,
  Bluetooth, and USB Audio. The address stays <code>jts.local</code>.</p>

  <form method="post" action="./save" id="speaker-name-form"
        data-default="{default_attr}">
    {csrf_field_html(csrf_token)}
    <div class="field">
      <label for="speaker-name">Speaker name</label>
      <input id="speaker-name" type="text" name="name" value="{value}"
             maxlength="{MAX_SPEAKER_NAME_CHARS}"
             autocomplete="off" autocapitalize="words" spellcheck="false">
      <p class="form-hint">Default: {default_text}. Use {MAX_SPEAKER_NAME_CHARS}
      characters or fewer.</p>
    </div>
    <div class="field">
      <label for="speaker-room">Room (optional)</label>
      <input id="speaker-room" type="text" name="room" value="{room_value}"
             maxlength="{MAX_SPEAKER_NAME_CHARS}"
             autocomplete="off" autocapitalize="words" spellcheck="false">
      <p class="form-hint">Which room this speaker is in, e.g. Kitchen.
      Leave blank to clear.</p>
    </div>
    <div class="form-actions">
      <button type="submit" class="btn btn--primary">Save and restart</button>
    </div>
  </form>
</main>
<script type="module" src="/assets/speaker/js/main.js"></script>
"""
    return canonical_page("Speaker name", body, csrf_token=csrf_token)


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                state = read_state(cfg["state_path"])
                send_html_response(self, _index_html(
                    current_name=state.name,
                    current_room=state.room,
                    csrf_token=ctx["csrf_token"],
                    status_msg=ctx["flash"],
                ))
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path != "/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return

            try:
                requested = validate_name(form.get("name", ""))
            except SpeakerNameError as e:
                send_see_other(self, "./", flash=str(e))
                return

            try:
                # Room is optional; "" is a valid "unset" answer, not an error.
                requested_room = validate_room(form.get("room", ""))
            except SpeakerNameError as e:
                send_see_other(self, "./", flash=str(e))
                return

            state = read_state(cfg["state_path"])
            current = state.name
            if requested == current and requested_room == state.room:
                send_see_other(self, "./", flash="Name unchanged.")
                return

            # Conflict-check only the renderer-visible name. The room label
            # is local-only (no AirPlay/Bluetooth collision), so a room-only
            # edit skips the network probe.
            if requested != current:
                conflicts = _find_conflicts(requested)
                if conflicts:
                    log_event(
                        logger,
                        "speaker_name.conflict",
                        requested=repr(requested),
                        conflicts=",".join(f"{c.protocol}:{c.detail}" for c in conflicts),
                    )
                    send_see_other(self, "./", flash=_format_conflicts(conflicts))
                    return

            try:
                saved = write_state(
                    requested, requested_room,
                    path=cfg["state_path"], mode=0o644,
                )
            except (OSError, SpeakerNameError) as e:
                logger.exception("speaker name save failed")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return

            log_event(
                logger,
                "speaker_name.save",
                previous=repr(current),
                requested=repr(requested),
                saved=repr(saved),
                room=repr(requested_room),
            )
            _apply_name(saved)
            send_see_other(
                self, "./",
                flash=f'Saved. Speaker renamed to "{saved}". Services restarting.',
            )

    return Handler


def make_server(target, *, state_path: str = SPEAKER_NAME_FILE) -> ThreadingHTTPServer:
    from . import _systemd
    cfg = {"state_path": state_path}
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-speaker-web",
        description="Speaker display-name settings for the Jasper smart speaker",
    )
    parser.add_argument("--host", default=os.environ.get("JASPER_SPEAKER_WEB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_SPEAKER_WEB_PORT", "8783")),
    )
    parser.add_argument("--state", default=os.environ.get("JASPER_SPEAKER_NAME_FILE", SPEAKER_NAME_FILE))
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("JASPER_LOG_LEVEL", "INFO").upper())
    server = make_server((args.host, args.port), state_path=args.state)
    logger.info("jasper-speaker-web listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
