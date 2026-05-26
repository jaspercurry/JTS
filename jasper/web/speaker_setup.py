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

from ..bluetooth.adapter import set_alias as set_bluetooth_alias
from ..speaker_name import (
    DEFAULT_SPEAKER_NAME,
    MAX_SPEAKER_NAME_CHARS,
    SpeakerNameError,
    read_state,
    validate_name,
    write_state,
)
from ..speaker_name_discovery import NameConflict, find_name_conflicts
from ._common import (
    begin_request,
    csrf_field_html,
    read_form,
    reject_csrf,
    send_html_response,
    send_see_other,
    verify_csrf,
    wrap_page,
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
        logger.warning(
            "event=speaker_name.restart_failed units=%s detail=%s",
            ",".join(units), out,
        )


def _write_bluez_main_conf_name(name: str, path: str = BLUEZ_MAIN_CONF) -> None:
    conf = Path(path)
    try:
        original = conf.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("event=speaker_name.bluez_conf_missing path=%s", path)
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
    logger.info("event=speaker_name.bluez_conf path=%s result=ok", path)


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
        logger.warning("event=speaker_name.duplicate_check_failed error=%s", e)
        return []


def _apply_name(name: str) -> None:
    units = list(RESTART_UNITS)
    if _unit_active("jasper-usbsink.service") or _unit_active("jasper-usbsink-init.service"):
        units.append("jasper-usbsink-init.service")

    _write_bluez_main_conf_name(name)
    try:
        asyncio.run(set_bluetooth_alias(name))
        logger.info("event=speaker_name.bluetooth_alias name=%r result=ok", name)
    except Exception as e:  # noqa: BLE001
        logger.warning("event=speaker_name.bluetooth_alias name=%r result=failed error=%s", name, e)

    logger.info("event=speaker_name.restart units=%s", ",".join(units))
    _restart_units(units)


def _index_html(
    *,
    current_name: str,
    csrf_token: str,
    status_msg: str = "",
) -> bytes:
    value = html.escape(current_name, quote=True)
    default = html.escape(DEFAULT_SPEAKER_NAME)
    body = f"""
<p class="sub">Change the name shown in AirPlay, Spotify Connect,
Bluetooth, and USB Audio. The address stays <code>jts.local</code>.</p>

<form method="post" action="./save" id="speaker-name-form">
  {csrf_field_html(csrf_token)}
  <label for="speaker-name">Speaker name</label>
  <input id="speaker-name" name="name" value="{value}" maxlength="{MAX_SPEAKER_NAME_CHARS}"
         autocomplete="off" autocapitalize="words" spellcheck="false">
  <p class="hint">Default: {default}. Use {MAX_SPEAKER_NAME_CHARS} characters or fewer.</p>
  <button type="submit">Save and restart</button>
</form>

<script>
const form = document.getElementById('speaker-name-form');
const input = document.getElementById('speaker-name');
form.addEventListener('submit', function(event) {{
  const name = input.value.trim() || '{default}';
  const ok = window.confirm(
    'Rename speaker to "' + name + '"? This restarts audio, Bluetooth, ' +
    'and voice services. You may need to reconnect from your phone or computer.'
  );
  if (!ok) event.preventDefault();
}});
</script>
"""
    return wrap_page("Speaker name", body, status_msg=status_msg)


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                ctx = begin_request(self)
                state = read_state(cfg["state_path"])
                send_html_response(self, _index_html(
                    current_name=state.name,
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
            if not verify_csrf(self, form):
                reject_csrf(self)
                return

            try:
                requested = validate_name(form.get("name", ""))
            except SpeakerNameError as e:
                send_see_other(self, "./", flash=str(e))
                return

            current = read_state(cfg["state_path"]).name
            if requested == current:
                send_see_other(self, "./", flash="Name unchanged.")
                return

            conflicts = _find_conflicts(requested)
            if conflicts:
                logger.info(
                    "event=speaker_name.conflict requested=%r conflicts=%s",
                    requested,
                    ",".join(f"{c.protocol}:{c.detail}" for c in conflicts),
                )
                send_see_other(self, "./", flash=_format_conflicts(conflicts))
                return

            try:
                saved = write_state(requested, cfg["state_path"], mode=0o644)
            except (OSError, SpeakerNameError) as e:
                logger.exception("speaker name save failed")
                send_see_other(self, "./", flash=f"Could not save: {e}")
                return

            logger.info(
                "event=speaker_name.save previous=%r requested=%r saved=%r",
                current, requested, saved,
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
